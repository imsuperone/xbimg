# -*- coding: utf-8 -*-
"""渲染引擎 · Emoji 层：三样式资源包/下载/解包/Twemoji 按需补全/簇级图片缓存与并行预取。"""
import io
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageFont

try:
    from fontTools.ttLib import TTFont as _TTFONT
except Exception:
    _TTFONT = None

try:
    from astrbot.api import logger
except Exception:
    import logging
    logger = logging.getLogger("msg2img")

from . import shared, fonts
from .fonts import _load_font_file, clear_font_cache
from .shared import (
    EMOJI_ASSETS_DIR, _NON_SINGLETON_CPS, _cluster_codes, _is_emoji_cluster,
    _take_cluster, is_emoji_char,
)
# Emoji 样式包（按需下载，默认不下载）
EMOJI_PACKS = [
    {"id": "ios", "name": "iOS 最新", "desc": "苹果全彩 · 拟真", "need_font": False},
    {"id": "android", "name": "Android 最新", "desc": "Noto 全彩 · 本地离线", "need_font": True},
    {"id": "windows", "name": "Windows 最新", "desc": "Segoe 风格 · 清晰", "need_font": True},
]

# emoji 占用统计缓存：style -> (ts, kb)，页面一次加载触发 4 次全盘 rglob，10s 内复用
_EMOJI_STORAGE_CACHE: Dict[str, Any] = {}
_EMOJI_STORAGE_TTL = 10.0


def _emoji_storage_invalidate() -> None:
    """下载/删除完成后失效占用缓存，下次查询重新统计"""
    try:
        _EMOJI_STORAGE_CACHE.clear()
    except Exception:
        pass


def get_emoji_storage_kb(style: Optional[str] = None) -> float:
    """计算 emoji 持久化目录占用（KB），可按样式精确过滤（10s TTL 缓存）"""
    try:
        # 缓存键必须带目录：测试/多数据目录场景下同 style 不同目录互不串扰
        try:
            _dd = shared._data_subdir("emoji")
            _dkey = str(_dd) if _dd is not None else ""
        except Exception:
            _dkey = ""
        key = f"{style or ''}\0{_dkey}"
        now = time.time()
        hit = _EMOJI_STORAGE_CACHE.get(key)
        if hit is not None:
            try:
                if now - float(hit[0]) < _EMOJI_STORAGE_TTL:
                    return float(hit[1])
            except Exception:
                pass
        total = _get_emoji_storage_kb_nocache(style)
        try:
            if len(_EMOJI_STORAGE_CACHE) > 16:
                _EMOJI_STORAGE_CACHE.pop(next(iter(_EMOJI_STORAGE_CACHE)))
            _EMOJI_STORAGE_CACHE[key] = (now, total)
        except Exception:
            pass
        return total
    except Exception:
        return 0.0


def _get_emoji_storage_kb_nocache(style: Optional[str] = None) -> float:
    try:
        d = shared._data_subdir("emoji")
        if d is None or not d.is_dir():
            return 0.0
        total = 0
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            try:
                if p.name.endswith(".downloading"):
                    continue  # 下载中的临时残留不计入占用
                if style == "ios" and p.suffix.lower() != ".png" and p.name != "ios_pack.ready":
                    continue
                if style == "android" and p.name != ANDROID_EMOJI_FILE:
                    continue
                if style == "windows" and p.name != WINDOWS_EMOJI_FILE:
                    continue
                total += p.stat().st_size
            except Exception:
                pass
        return round(total / 1024, 1)
    except Exception:
        return 0.0


def _style_font_path(style: str) -> str:
    """根据具体样式查找已下载字体路径"""
    fname = ANDROID_EMOJI_FILE if style == "android" else WINDOWS_EMOJI_FILE
    try:
        d = shared._data_subdir("emoji")
        if d is not None:
            p = d / fname
            if p.is_file() and p.stat().st_size > _EMOJI_MIN_FONT_BYTES:
                return str(p)
    except Exception:
        pass
    return ""


# android 本地离线包标记：解包成功后写入，含解出数量
ANDROID_PACK_READY = "android_pack.ready"
ANDROID_PACK_MANIFEST = "android_pack.files"
# 解包成功的最低门槛（单字形数量；正常 Noto 包数千个，远高于此）
ANDROID_PACK_MIN_GLYPHS = 500
def _extract_noto_pngs(font_path: str, dest_dir: Path) -> Tuple[int, str]:
    """从 NotoColorEmoji.ttf 的 CBDT 表解出单字 PNG（文件名兼容 Twemoji 命名）。

    背景：PIL 打不开 CBDT 位图字（invalid pixel size），字体文件本身永远画不出；
    但表里躺着现成的 PNG 裸数据，解出来走已有 PNG 管道即得完整离线包。
    返回 (解出数量, 错误信息)。只收录可独立成图的单码位，跳过 ZWJ/肤色等修饰位。
    """
    try:
        if _TTFONT is None:
            return 0, "缺少 fonttools，无法解包"
        try:
            ff = _TTFONT(str(font_path), lazy=True)
        except Exception as e:
            return 0, f"字体解析失败: {e}"
        try:
            try:
                cmap = ff.getBestCmap()
            except Exception:
                cmap = {}
            try:
                strikes = ff["CBDT"].strikeData
            except Exception:
                return 0, "字体无 CBDT 位图包"
            # 取字形最多的 strike（多尺寸时选最全的）
            best = None
            for st in strikes or []:
                try:
                    if best is None or len(st) > len(best):
                        best = st
                except Exception:
                    continue
            if not best:
                return 0, "位图包为空"
            inv: Dict[str, List[int]] = {}
            for cp, gname in (cmap or {}).items():
                try:
                    inv.setdefault(str(gname), []).append(int(cp))
                except Exception:
                    continue
            names: List[str] = []
            done = 0
            for gname, members in best.items():
                try:
                    cps = inv.get(str(gname)) or []
                    picked = None
                    for cp in cps:
                        if cp in _NON_SINGLETON_CPS:
                            continue
                        try:
                            if not is_emoji_char(chr(cp)):
                                continue
                        except Exception:
                            continue
                        picked = cp
                        break
                    if picked is None:
                        continue
                    raw = None
                    try:
                        raw = getattr(members, "imageData", None)
                    except Exception:
                        raw = None
                    if not raw or bytes(raw[:8]) != b"\x89PNG\r\n\x1a\n":
                        continue
                    fname = f"{picked:x}.png"
                    try:
                        with open(dest_dir / fname, "wb") as f:
                            f.write(bytes(raw))
                        names.append(fname)
                        done += 1
                    except Exception:
                        continue
                    if done >= 4000:
                        break
                except Exception:
                    continue
            try:
                with open(dest_dir / ANDROID_PACK_MANIFEST, "w", encoding="utf-8") as f:
                    f.write("\n".join(names))
                with open(dest_dir / ANDROID_PACK_READY, "w", encoding="utf-8") as f:
                    f.write(str(done))
            except Exception:
                pass
            return done, ""
        finally:
            try:
                ff.close()
            except Exception:
                pass
    except Exception as e:
        return 0, f"解包异常: {e}"


# 样式彩字有效性结论缓存：style -> ((mtime, size), ok)，文件变化自动重验
_STYLE_FONT_VALID: Dict[str, Any] = {}


def _style_font_validated(style: str) -> str:
    """已下载字体的有效路径（分样式判定）。

    - android：以解包标记为准（PIL 打不开 CBDT 位图字，看体积/加载都是误判；
      标记含解出数量，文件替换/删除后自动失效）。
    - windows：文件存在 + 体积达标 + 可加载即算（包格式复杂，不强求字形覆盖）。
    只看体积会把超时截断的残包误判为“已下载”，导致绘制每次回退网络。
    """
    try:
        sid = str(style or "").lower()
        if sid == "android":
            return _android_pack_validated()
        path = _style_font_path(style)
    except Exception:
        return ""
    if not path:
        return ""
    try:
        st = os.stat(path)
        key = (st.st_mtime, st.st_size)
    except Exception:
        return ""
    try:
        hit = _STYLE_FONT_VALID.get(style)
        if hit is not None and hit[0] == key:
            return path if hit[1] else ""
    except Exception:
        pass
    ok = False
    try:
        f = _load_font_file(path, 32)
        ok = f is not None
    except Exception:
        ok = False
    try:
        if len(_STYLE_FONT_VALID) > 8:
            _STYLE_FONT_VALID.pop(next(iter(_STYLE_FONT_VALID)))
        _STYLE_FONT_VALID[style] = (key, ok)
    except Exception:
        pass
    return path if ok else ""


def _android_pack_validated() -> str:
    """android 离线包有效性：字体文件存在 + 解包标记存在且数量达标（结论缓存）。"""
    try:
        path = _style_font_path("android")
        if not path:
            return ""
        d = shared._data_subdir("emoji")
        if d is None:
            return ""
        rp = d / ANDROID_PACK_READY
        try:
            st_f = os.stat(path)
            st_r = os.stat(str(rp))
            key = (st_f.st_mtime, st_f.st_size, st_r.st_mtime, st_r.st_size)
        except Exception:
            return ""
        try:
            hit = _STYLE_FONT_VALID.get("android")
            if hit is not None and hit[0] == key:
                return path if hit[1] else ""
        except Exception:
            pass
        ok = False
        try:
            n = int(rp.read_text(encoding="utf-8").strip())
            ok = n >= ANDROID_PACK_MIN_GLYPHS
        except Exception:
            ok = False
        try:
            if len(_STYLE_FONT_VALID) > 8:
                _STYLE_FONT_VALID.pop(next(iter(_STYLE_FONT_VALID)))
            _STYLE_FONT_VALID["android"] = (key, ok)
        except Exception:
            pass
        return path if ok else ""
    except Exception:
        return ""


def _style_font_forget(style: str = "") -> None:
    """删除/下载后清验证缓存（mtime 键本就会失效，这里是显式保险）"""
    try:
        if style:
            _STYLE_FONT_VALID.pop(style, None)
        else:
            _STYLE_FONT_VALID.clear()
    except Exception:
        pass


def _singles_via_local_font(emoji_style: str) -> bool:
    """单字 emoji 是否可走本地彩字零网络绘制。

    仅 android：要求字体真实可加载（含代表字形），残包不再虚标；
    windows 彩字在 PIL 下未必真彩，仍走 PNG 优先保画质；
    ios 无字体文件，只能 PNG。复杂簇（ZWJ/键帽）不受影响，仍走全彩图。
    """
    try:
        if str(emoji_style or "").lower() != "android":
            return False
        return bool(_style_font_validated("android"))
    except Exception:
        return False


def get_emoji_packs_status() -> List[Dict[str, Any]]:
    """返回 Emoji 三样式状态（是否已下载、占用）"""
    out = []
    for pack in EMOJI_PACKS:
        sid = pack["id"]
        need_font = pack.get("need_font", False)
        if sid == "ios":
            d = shared._data_subdir("emoji")
            has = False
            if d and d.is_dir():
                # 以实际 PNG 为准：空目录即使残留 ready 标记也不算已下载
                if any(d.glob("*.png")):
                    has = True
            storage = get_emoji_storage_kb("ios")
            out.append({"id": sid, "name": pack["name"], "desc": pack["desc"], "installed": has, "storage_kb": storage, "need_font": need_font})
        else:
            has = bool(_style_font_validated(sid))
            storage = get_emoji_storage_kb(sid)
            out.append({"id": sid, "name": pack["name"], "desc": pack["desc"], "installed": has, "storage_kb": storage, "need_font": need_font})
    return out


# 高频基础表情（首屏常用，一次下好；包外按需自动补全并缓存）；
# ios 与 windows 共用（两者都是 Twemoji PNG 管道，windows 无离线解包源）
BASE_EMOJIS = [
    # 笑脸
    "1f600", "1f602", "1f603", "1f604", "1f605", "1f609", "1f60d",
    "1f618", "1f61c", "1f622", "1f62d", "1f630", "1f62e", "1f60e",
    "1f610", "1f914", "1f917",
    # 手势
    "1f44d", "1f44e", "1f44f", "1f450", "1f446", "1f447", "270a",
    "270c", "1f44c", "1f4aa", "1f485",
    # 心与符号
    "2764", "1f495", "1f494", "1f49b", "2728", "2b50", "1f31f",
    "274c", "2705", "26a0",
    # 物件（含游戏/资产高频：钱袋货币骰子）
    "1f389", "1f38a", "1f525", "1f4b0", "1f4b5", "1f4b8", "1f4a1",
    "1f4a9", "1f4ac", "1f4f7", "23e9", "1f552", "1f6a7", "1f52e",
    "1f3af", "1f3b0", "1f3b2", "1f3b3",
]


def _ensure_base_pngs(d: Path) -> Tuple[int, int, int]:
    """补齐基础 PNG 包。返回 (补前现有数, 本次缺失数, 补后总数)。失败静默（按需再补）。"""
    try:
        missing = [c for c in BASE_EMOJIS if not (d / f"{c}.png").is_file()]
        pre_have = len(BASE_EMOJIS) - len(missing)
        if missing:
            try:
                import concurrent.futures as _cfut
                with _cfut.ThreadPoolExecutor(max_workers=min(6, len(missing))) as _ex:
                    list(_ex.map(lambda _c: _fetch_remote_emoji(_c, d), missing))
            except Exception:
                pass
        have = sum(1 for code in BASE_EMOJIS if (d / f"{code}.png").is_file())
        return pre_have, len(missing), have
    except Exception:
        return 0, 0, 0


def download_emoji_pack(style: str) -> Dict[str, Any]:
    """下载指定 Emoji 样式资源"""
    style = (style or "").lower()
    if style not in ("ios", "android", "windows"):
        return {"ok": False, "error": "未知样式"}
    d = shared._data_subdir("emoji")
    if d is None:
        return {"ok": False, "error": "持久化目录不可用"}

    if style == "ios":
        pre_have, _, have = _ensure_base_pngs(d)
        total_base = len(BASE_EMOJIS)
        if not have:
            # 一个都没下来：不写 ready 标记，如实报错（否则会虚标“已下载”）
            try:
                (d / "ios_pack.ready").unlink(missing_ok=True)
            except Exception:
                pass
            return {"ok": False, "error": "iOS Emoji 下载失败，请检查服务器网络后重试", "storage_kb": 0.0}
        try:
            (d / "ios_pack.ready").write_text("ok", encoding="utf-8")
        except Exception:
            pass
        extra = "（已齐全）" if have <= pre_have else ""
        _emoji_storage_invalidate()
        return {"ok": True, "downloaded": [f"iOS基础Emoji({have}/{total_base}个常用{extra}，其余按需自动补全)"], "storage_kb": get_emoji_storage_kb("ios")}

    # android 或 windows（多直链容错，依次尝试；下载后校验体积，残包换源重试）
    fname = ANDROID_EMOJI_FILE if style == "android" else WINDOWS_EMOJI_FILE
    if style == "android":
        furls = list(ANDROID_EMOJI_URLS)
    else:
        furls = list(WINDOWS_EMOJI_URLS)
    target = d / fname
    ok, err = False, ""
    for furl in furls:
        ok, err = fonts._download_file(furl, target)
        if not ok:
            continue
        try:
            if target.stat().st_size < _EMOJI_MIN_FONT_BYTES:
                ok, err = False, "文件不完整，已换源重试"
                continue
        except Exception:
            ok, err = False, "文件校验失败"
            continue
        break
    if ok:
        _EMOJI_FONT_CACHE.clear()
        _style_font_forget(style)
        if style == "android":
            # 解包成本地 PNG 离线包（PIL 打不开 CBDT 位图字，字体只当解包源）；
            # 解包不足量即失败并清理，不虚标
            try:
                n, emsg = _extract_noto_pngs(str(target), d)
            except Exception as e:
                n, emsg = 0, f"解包异常: {e}"
            if n < ANDROID_PACK_MIN_GLYPHS:
                for _p in (target, d / ANDROID_PACK_MANIFEST, d / ANDROID_PACK_READY):
                    try:
                        _p.unlink(missing_ok=True)
                    except Exception:
                        pass
                _style_font_forget(style)
                return {"ok": False, "error": f"{fname} 解包失败（仅解出 {n} 个，{emsg or '文件异常'}），已清理请重试", "storage_kb": 0.0}
            _emoji_storage_invalidate()
            return {"ok": True, "downloaded": [fname, f"离线表情图{n}个"], "storage_kb": get_emoji_storage_kb(style)}
        # 落盘后即验即报：残包直接失败并清理，不虚标“已下载”
        try:
            if not _style_font_validated(style):
                try:
                    target.unlink(missing_ok=True)
                except Exception:
                    pass
                _style_font_forget(style)
                return {"ok": False, "error": f"{fname} 校验未通过（文件不完整），已清理请重试", "storage_kb": 0.0}
        except Exception:
            pass
        _emoji_storage_invalidate()
        if style == "windows":
            # windows 无离线解包源：顺手补一份基础 PNG，首屏不再现下
            try:
                _ensure_base_pngs(d)
                _emoji_storage_invalidate()
            except Exception:
                pass
            return {"ok": True, "downloaded": [fname, "基础表情图（首屏常用）"], "storage_kb": get_emoji_storage_kb(style)}
        return {"ok": True, "downloaded": [fname], "storage_kb": get_emoji_storage_kb(style)}
    return {"ok": False, "error": f"{fname} 下载失败: {err or '未知错误'}", "storage_kb": 0.0}


def delete_emoji_pack(style: str) -> Dict[str, Any]:
    """删除指定 Emoji 样式资源及缓存"""
    style = (style or "").lower()
    if style not in ("ios", "android", "windows", "all"):
        return {"ok": False, "error": "未知样式"}
    clear_font_cache()
    d = shared._data_subdir("emoji")
    deleted = []
    search_dirs = [d] if d and d.is_dir() else []
    if shared._FONT_DATA_DIR and shared._FONT_DATA_DIR.is_dir() and shared._FONT_DATA_DIR not in search_dirs:
        search_dirs.append(shared._FONT_DATA_DIR)

    try:
        if style in ("ios", "all") and d and d.is_dir():
            for p in list(d.iterdir()):
                if p.is_file() and (p.suffix.lower() == ".png" or p.name.startswith("ios_pack") or p.name.endswith(".downloading")):
                    try:
                        p.unlink(missing_ok=True)
                        deleted.append(p.name)
                    except Exception:
                        pass
        if style in ("android", "all"):
            for fname in (ANDROID_EMOJI_FILE, "NotoColorEmoji.ttf"):
                for sdir in search_dirs:
                    target = sdir / fname
                    if target.is_file():
                        for _ in range(3):
                            try:
                                target.unlink(missing_ok=True)
                                deleted.append(fname)
                                break
                            except Exception:
                                clear_font_cache()
                                time.sleep(0.05)
            # 解包清单内的离线 PNG + 标记一并清除（按需缓存的 PNG 不在此列，不动）
            if d and d.is_dir():
                try:
                    manifest = d / ANDROID_PACK_MANIFEST
                    if manifest.is_file():
                        try:
                            listed = manifest.read_text(encoding="utf-8").split()
                        except Exception:
                            listed = []
                        for _nm in listed[:5000]:
                            try:
                                _pp = d / _nm
                                if _pp.is_file() and _pp.suffix.lower() == ".png":
                                    _pp.unlink(missing_ok=True)
                                    deleted.append(_nm)
                            except Exception:
                                continue
                except Exception:
                    pass
                for _mark in (ANDROID_PACK_MANIFEST, ANDROID_PACK_READY):
                    try:
                        (d / _mark).unlink(missing_ok=True)
                    except Exception:
                        pass
        if style in ("windows", "all"):
            for fname in (WINDOWS_EMOJI_FILE, "EmojiOneColor.otf"):
                for sdir in search_dirs:
                    target = sdir / fname
                    if target.is_file():
                        for _ in range(3):
                            try:
                                target.unlink(missing_ok=True)
                                deleted.append(fname)
                                break
                            except Exception:
                                clear_font_cache()
                                time.sleep(0.05)
        # 清理所有残留的 .del_* 临时文件
        if d and d.is_dir():
            for p in list(d.glob("*.del_*")) + list(d.glob("*.downloading")):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        clear_font_cache()
        _style_font_forget("" if style == "all" else style)
        _emoji_storage_invalidate()
        return {"ok": True, "deleted": deleted, "storage_kb": get_emoji_storage_kb()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_EMOJI_FONT_CACHE: Dict[int, Tuple[Optional[ImageFont.FreeTypeFont], bool]] = {}
_EMOJI_IMG_CACHE: Dict[Tuple, Optional[Image.Image]] = {}
# emoji 图片缓存上限（防止超长刷屏消息撑爆内存）
_EMOJI_IMG_CACHE_LIMIT = 400


def get_emoji_font(size: int) -> Tuple[Optional[ImageFont.FreeTypeFont], bool]:
    """跨平台 Emoji 字体，返回 (font, 是否真彩)。
    顺序：数据目录 NotoColorEmoji（真彩，需 embedded_color=True 绘制）
    → Linux NotoColorEmoji → Windows seguiemj → macOS（后三者按单色绘制）。"""
    if size in _EMOJI_FONT_CACHE:
        return _EMOJI_FONT_CACHE[size]
    cands: List[Tuple[str, bool]] = []
    col = _color_emoji_font_path()
    if col:
        cands.append((col, True))
    cands += [
        ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", True),
        ("/usr/share/fonts/NotoColorEmoji.ttf", True),
        ("C:/Windows/Fonts/seguiemj.ttf", False),
        ("/System/Library/Fonts/Apple Color Emoji.ttc", False),
    ]
    for cand, is_color in cands:
        if os.path.isfile(cand):
            try:
                f = _load_font_file(cand, size)
                if f is not None:
                    _EMOJI_FONT_CACHE[size] = (f, is_color)
                    return f, is_color
            except Exception:
                continue
    _EMOJI_FONT_CACHE[size] = (None, False)
    return None, False


# ==========================================
# Emoji 全自动管线：本地 PNG → 数据目录 → 云端补全 → 系统字体 → 正文单色
# 用户零选择：有图用图，无图自动降级，永不崩溃
# 云端图源 Twemoji (CC-BY 4.0)，按需下载单个 72x72 PNG 并持久缓存
# ==========================================
_TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
# 同源镜像（内容一致，链路故障时轮换；404 表示文件真没有，无需换源）
_TWEMOJI_BASES = [
    _TWEMOJI_BASE,
    "https://fastly.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72",
    "https://gcore.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72",
]
_EMOJI_DL_TIMEOUT = 3
# 预取等待预算：超时即放行绘制（后台继续下完），避免冷缓存把发送拖到秒级
_EMOJI_PREFETCH_BUDGET_S = 0.5
_EMOJI_DL_MAX_BYTES = 2 * 1024 * 1024
# Noto 全彩 Emoji 字体（Android 原生风格）
# 注意：上游 noto-emoji 仓库已改为源码仓，旧 main 直链永久 404；
# 改用 commit 锁定的 unicode16 版本（不可变，永久有效），多镜像轮换
ANDROID_EMOJI_FILE = "NotoColorEmoji.ttf"
_NOTO_PIN_SHA = "124c7d40bb51bc026b5407dfffd34e1e6843e1a8"
ANDROID_EMOJI_URLS = [
    f"https://cdn.jsdelivr.net/gh/googlefonts/noto-emoji@{_NOTO_PIN_SHA}/fonts/NotoColorEmoji.ttf",
    f"https://fastly.jsdelivr.net/gh/googlefonts/noto-emoji@{_NOTO_PIN_SHA}/fonts/NotoColorEmoji.ttf",
    f"https://gcore.jsdelivr.net/gh/googlefonts/noto-emoji@{_NOTO_PIN_SHA}/fonts/NotoColorEmoji.ttf",
    f"https://raw.githubusercontent.com/googlefonts/noto-emoji/{_NOTO_PIN_SHA}/fonts/NotoColorEmoji.ttf",
]
ANDROID_EMOJI_URL = ANDROID_EMOJI_URLS[0]
# EmojiOne/Twemoji 全彩字体（跨平台标准矢量风格）
WINDOWS_EMOJI_FILE = "EmojiOneColor.otf"
WINDOWS_EMOJI_URLS = [
    "https://cdn.jsdelivr.net/gh/adobe-fonts/emojione-color@master/EmojiOneColor.otf",
    "https://raw.githubusercontent.com/adobe-fonts/emojione-color/master/EmojiOneColor.otf",
]
WINDOWS_EMOJI_URL = WINDOWS_EMOJI_URLS[0]
_EMOJI_MIN_FONT_BYTES = 1 * 1024 * 1024

# CDN 不可达时退避（DNS 黑洞等只挡一次，10 分钟内不再尝试，避免消息延迟）
_EMOJI_REMOTE_DEAD_UNTIL = 0.0
# Twemoji 404 负缓存：code -> 过期时间戳。CDN 可达但文件真没有时不再反复打网。
_EMOJI_MISS_CACHE: Dict[str, float] = {}
_EMOJI_MISS_CACHE_TTL_S = 600.0
_EMOJI_MISS_CACHE_LIMIT = 512


def _emoji_miss_cached(code: str) -> bool:
    """404 负缓存命中（过期自动清除）"""
    try:
        exp = _EMOJI_MISS_CACHE.get(code)
        if exp is None:
            return False
        if time.time() >= float(exp):
            _EMOJI_MISS_CACHE.pop(code, None)
            return False
        return True
    except Exception:
        return False


def _emoji_mark_miss(code: str) -> None:
    """记录 Twemoji 404（仅 CDN 可达且文件不存在；网络错误走全局退避，不进负缓存）"""
    try:
        if len(_EMOJI_MISS_CACHE) >= _EMOJI_MISS_CACHE_LIMIT:
            _EMOJI_MISS_CACHE.pop(next(iter(_EMOJI_MISS_CACHE)), None)
        _EMOJI_MISS_CACHE[code] = time.time() + _EMOJI_MISS_CACHE_TTL_S
    except Exception:
        pass


def _load_emoji_png(path: Path, size: int) -> Optional[Image.Image]:
    try:
        raw_bytes = path.read_bytes()
        with Image.open(io.BytesIO(raw_bytes)) as src:
            src.load()
            im = src.convert("RGBA")
        if im.width < 8 or im.height < 8:
            return None
        return im.resize((size, size), Image.LANCZOS)
    except Exception:
        return None


def _emoji_http_client():
    """共享 httpx 同步客户端（连接复用 keep-alive，多 emoji 摊薄 TLS 握手）。

    线程安全（httpx 同步 Client 支持多线程），缺 httpx 时返回 None 走 urllib 兜底。
    """
    global _EMOJI_HTTP_CLIENT
    try:
        if _EMOJI_HTTP_CLIENT is None:
            import httpx as _hx
            _EMOJI_HTTP_CLIENT = _hx.Client(
                timeout=3.0, follow_redirects=True,
                headers={"User-Agent": "astrbot-msg2img/1.3"},
            )
        return _EMOJI_HTTP_CLIENT
    except Exception:
        return None


_EMOJI_HTTP_CLIENT = None
# emoji 预取线程池：与 layout 并行提交，模块级复用（不随每次渲染重建）
_PREFETCH_POOL = None
_PREFETCH_POOL_LOCK = threading.Lock()
# emoji 缺图抓取 worker 池：模块级复用（与上面 2-worker 提交池区分，这里是抓取并发，
# 宽度 6 与 _prefetch_emoji_images 默认 max_workers 一致；不随每次渲染重建/销毁）
_FETCH_POOL = None
_FETCH_POOL_LOCK = threading.Lock()


def _fetch_pool():
    """返回共享抓取池（懒创建）；创建失败返回 None（调用方直接短路，不断言）"""
    global _FETCH_POOL
    try:
        with _FETCH_POOL_LOCK:
            if _FETCH_POOL is None:
                import concurrent.futures as _cf
                _FETCH_POOL = _cf.ThreadPoolExecutor(max_workers=6, thread_name_prefix="xbimg-emoji-fetch")
            return _FETCH_POOL
    except Exception:
        return None


def _submit_prefetch(text: str, skip_singles: bool = False):
    """把缺图预取提交到共享线程池，返回 Future（调用方在 layout 后 result() 汇合）"""
    global _PREFETCH_POOL
    try:
        with _PREFETCH_POOL_LOCK:
            if _PREFETCH_POOL is None:
                import concurrent.futures as _cf
                _PREFETCH_POOL = _cf.ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="xbimg-emoji-pf"
                )
            return _PREFETCH_POOL.submit(_prefetch_emoji_images, text, skip_singles=skip_singles)
    except Exception:
        return None


def _fetch_remote_emoji_urllib(code: str, dest_dir: Path, tmp: Path) -> bool:
    """urllib 兜底通道（无 httpx 环境），与旧行为一致"""
    global _EMOJI_REMOTE_DEAD_UNTIL  # 退避必须写到模块级，否则 600s 防抖永不生效
    import time as _time
    import urllib.error
    import urllib.request
    url = f"{_TWEMOJI_BASE}/{code}.png"
    req = urllib.request.Request(url, headers={"User-Agent": "astrbot-msg2img/1.3"})
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=_EMOJI_DL_TIMEOUT) as resp:
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _EMOJI_DL_MAX_BYTES:
                        raise ValueError("emoji 超体积")
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            _emoji_mark_miss(code)
            return False  # CDN 可达只是没这个文件，不退避
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        _EMOJI_REMOTE_DEAD_UNTIL = _time.time() + 600
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    os.replace(tmp, dest_dir / f"{code}.png")
    return True


def _fetch_remote_emoji(code: str, dest_dir: Path) -> bool:
    """从 Twemoji CDN 按需下载单个 emoji，成功返回 True；网络不可达时全局退避"""
    global _EMOJI_REMOTE_DEAD_UNTIL
    tmp = None
    try:
        import time as _time
        if _time.time() < _EMOJI_REMOTE_DEAD_UNTIL:
            return False
        if _emoji_miss_cached(code):
            return False
        # tmp 唯一命名：同 emoji 并发预取互不覆盖，失败只删自己的
        try:
            _uniq = f"{os.getpid()}_{threading.get_ident()}"
        except Exception:
            _uniq = str(os.getpid())
        tmp = dest_dir / f"{code}.{_uniq}.png.downloading"
        client = _emoji_http_client()
        if client is None:
            return _fetch_remote_emoji_urllib(code, dest_dir, tmp)
        # 多镜像轮换：404 表示文件真没有（直接返回不退避）；网络错换下一个镜像，
        # 全部失败才全局退避（各镜像故障域独立，不要一个挂掉就全退）
        _net_failed = False
        for _base in _TWEMOJI_BASES:
            try:
                with client.stream("GET", f"{_base}/{code}.png") as resp:
                    if resp.status_code == 404:
                        try:
                            tmp.unlink(missing_ok=True)
                        except Exception:
                            pass
                        _emoji_mark_miss(code)
                        return False  # CDN 可达只是没这个文件，不退避
                    resp.raise_for_status()
                    total = 0
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_bytes(64 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > _EMOJI_DL_MAX_BYTES:
                                raise ValueError("emoji 超体积")
                            f.write(chunk)
                os.replace(tmp, dest_dir / f"{code}.png")
                return True
            except ValueError:
                raise
            except Exception as e:
                try:
                    import httpx as _hx
                    _is_http = isinstance(e, _hx.HTTPError)
                except Exception:
                    _is_http = False
                if not (_is_http or isinstance(e, (TimeoutError, OSError))):
                    raise
                _net_failed = True
                continue
        if _net_failed:
            _EMOJI_REMOTE_DEAD_UNTIL = _time.time() + 600
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            # 连接池异常则丢弃重建，避免坏连接常驻
            try:
                global _EMOJI_HTTP_CLIENT
                if _EMOJI_HTTP_CLIENT is not None:
                    try:
                        _EMOJI_HTTP_CLIENT.close()
                    except Exception:
                        pass
                _EMOJI_HTTP_CLIENT = None
            except Exception:
                pass
            return False
        return False
    except Exception:
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _color_emoji_font_path() -> str:
    """Noto 全彩字体路径：数据目录优先，其次系统已装"""
    try:
        d = shared._data_subdir("emoji")
        if d is not None:
            p = d / ANDROID_EMOJI_FILE
            if p.is_file() and p.stat().st_size > _EMOJI_MIN_FONT_BYTES:
                return str(p)
    except Exception:
        pass
    for cand in (
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/NotoColorEmoji.ttf",
        "C:/Windows/Fonts/NotoColorEmoji.ttf",
    ):
        try:
            if os.path.isfile(cand) and os.path.getsize(cand) > _EMOJI_MIN_FONT_BYTES:
                return cand
        except Exception:
            continue
    return ""


def ensure_emoji_assets(allow_remote: bool = True) -> Dict[str, Any]:
    """补齐 Emoji 资产（调用方放线程池）：缺 Noto 全彩字体则下载。返回摘要。"""
    if not allow_remote:
        return {"ok": True, "downloaded": [], "error": ""}
    data_dir = shared._data_subdir("emoji")
    if data_dir is None:
        return {"ok": False, "downloaded": [], "error": "持久化目录不可用"}
    if _color_emoji_font_path():
        return {"ok": True, "downloaded": [], "error": ""}
    target = data_dir / ANDROID_EMOJI_FILE
    ok, err = fonts._download_file(ANDROID_EMOJI_URL, target)
    if ok:
        try:
            if not _color_emoji_font_path():
                raise ValueError("字体校验未通过")
        except Exception as e:
            err = str(e)
            ok = False
    if ok:
        _EMOJI_FONT_CACHE.clear()
        _style_font_forget("android")
        # 字体本身画不出（CBDT 位图字），顺手解包成本地 PNG 离线包
        try:
            n, emsg = _extract_noto_pngs(str(target), data_dir)
            if n < ANDROID_PACK_MIN_GLYPHS:
                return {"ok": False, "downloaded": [],
                        "error": f"解包失败（仅解出 {n} 个，{emsg or '文件异常'}）"}
        except Exception as e:
            return {"ok": False, "downloaded": [], "error": f"解包异常: {e}"}
        _emoji_storage_invalidate()
        return {"ok": True, "downloaded": [ANDROID_EMOJI_FILE], "error": ""}
    return {"ok": False, "downloaded": [], "error": f"{ANDROID_EMOJI_FILE} 下载失败: {err or '未知错误'}"}


def _prefetch_emoji_images(text: str, max_workers: int = 6, max_codes: int = 64,
                           skip_singles: bool = False,
                           budget_s: float = _EMOJI_PREFETCH_BUDGET_S) -> Tuple[int, bool]:
    """批量预取文本中缺失的全彩 emoji 图（并行 IO，带时间预算）。

    绘制时缺图会逐个串行等待网络（最慢 3s 超时/个），预取后绘制零等待。
    多字符簇必走图片通道；单个 emoji 仅在无系统字体直绘时才需图片。已缓存/已落盘跳过。
    skip_singles 为 True 时单字直接跳过（android 本地彩字已装，绘制零网络）。
    返回 (本次新下载成功数, 是否在预算内取完全部 todo)。
    预算耗时则不再阻塞本次绘制；未完成任务留在执行器里后台跑完，供下次渲染命中。
    """
    if not text:
        return 0, True
    try:
        if time.time() < _EMOJI_REMOTE_DEAD_UNTIL:
            return 0, True
    except Exception:
        pass
    try:
        try:
            _ef, _ = get_emoji_font(32)
        except Exception:
            _ef = None
        seen = set()
        todo = []
        i, n = 0, len(text)
        while i < n:
            cluster, i = _take_cluster(text, i)
            if not _is_emoji_cluster(cluster):
                continue
            if len(cluster) < 2 and (skip_singles or _ef is not None):
                continue
            for code in _cluster_codes(cluster):
                if code in seen:
                    continue
                seen.add(code)
                # 键归一化：内存图缓存按实际 font_size 存，任意 size 命中即视作已有
                key_hit = False
                try:
                    for k in _EMOJI_IMG_CACHE:
                        if k[0] == code and _EMOJI_IMG_CACHE[k] is not None:
                            key_hit = True
                            break
                except Exception:
                    pass
                if key_hit:
                    continue
                if _emoji_miss_cached(code):
                    continue
                try:
                    if (EMOJI_ASSETS_DIR / f"{code}.png").is_file():
                        continue
                    d = shared._data_subdir("emoji")
                    if d is not None and (d / f"{code}.png").is_file():
                        continue
                except Exception:
                    pass
                todo.append(code)
                if len(todo) >= max_codes:
                    break
            if len(todo) >= max_codes:
                break
    except Exception:
        return 0, True
    if not todo:
        return 0, True
    try:
        data_dir = shared._data_subdir("emoji")
        if data_dir is None:
            return 0, True
        import concurrent.futures as _cf
        done = 0
        # 共享抓取池复用：不随每次渲染重建/销毁；退出时不等全部完成，
        # 预算外任务在池里后台续跑，下次渲染直接落盘命中
        ex = _fetch_pool()
        if ex is None:
            return 0, True
        futs = {ex.submit(_fetch_remote_emoji, code, data_dir): code for code in todo}
        _done_set, not_done = _cf.wait(futs, timeout=max(0.0, float(budget_s)))
        complete = not not_done
        for fut in _done_set:
            try:
                if fut.result():
                    done += 1
            except Exception:
                pass
        return done, complete
    except Exception:
        return 0, True


def _get_emoji_image(cluster: str, size: int, allow_remote: bool = True) -> Optional[Image.Image]:
    """取簇的全彩图：本地 → 数据目录 → 云端（可选）→ None（调用方降级字体绘制）"""
    codes = _cluster_codes(cluster)
    cache_key = (codes[0], size)
    if cache_key in _EMOJI_IMG_CACHE:
        return _EMOJI_IMG_CACHE[cache_key]

    def _store(im: Optional[Image.Image]):
        if len(_EMOJI_IMG_CACHE) >= _EMOJI_IMG_CACHE_LIMIT:
            _EMOJI_IMG_CACHE.pop(next(iter(_EMOJI_IMG_CACHE)))
        _EMOJI_IMG_CACHE[cache_key] = im

    # 1. 本地随包图库
    for code in codes:
        p = EMOJI_ASSETS_DIR / f"{code}.png"
        if p.is_file():
            im = _load_emoji_png(p, size)
            if im is not None:
                _store(im)
                return im

    # 2. 数据目录（历史云端下载 + 用户自放）
    data_dir = shared._data_subdir("emoji")
    if data_dir is not None:
        for code in codes:
            p = data_dir / f"{code}.png"
            if p.is_file():
                im = _load_emoji_png(p, size)
                if im is not None:
                    _store(im)
                    return im

        # 3. 云端按需补全（失败静默，降级字体绘制）
        if allow_remote:
            for code in codes:
                if _emoji_miss_cached(code):
                    continue
                if _fetch_remote_emoji(code, data_dir):
                    im = _load_emoji_png(data_dir / f"{code}.png", size)
                    if im is not None:
                        _store(im)
                        return im
                    try:
                        (data_dir / f"{code}.png").unlink(missing_ok=True)
                    except Exception:
                        pass

    _store(None)
    return None
