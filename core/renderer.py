# -*- coding: utf-8 -*-
"""
高颜值消息转图片渲染引擎 (Text-to-Image Renderer) - V3 精准修复版
- 彻底修复中文/Emoji 乱码：统一 font.getlength() 推进宽度、批量文本段绘制
- 统一字体优先级：SourceHanSans → msyh 回退，杜绝宽度计算不一致
- Footer 署名水平居中
- 字级精准半打码：仅对违规关键词的后半部分施加马赛克
- 全量 Numpy 矩阵加速渐变背景渲染（<5ms）
- 字体管理器：自定义字体 / 缺字自动下载到持久化目录 / Emoji 全自动管线
- Emoji 全自动：复杂序列全彩图，单个 emoji 优先 Noto 全彩字体，缺失自动云端补全
"""

import hashlib
import io
import math
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    from astrbot.api import logger
except Exception:
    import logging
    logger = logging.getLogger("msg2img")

# 本地资源目录（需先于字体探测定义：支持用户在 assets/fonts 下自带 CJK 字体）
EMOJI_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "emojis"
BUNDLED_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# 预编译正则（避免每行文本重复编译）
_RE_DIVIDER = re.compile(r"^[-*_]{3,}$")
_RE_BULLET = re.compile(r"^(\*|-|\d+\.)\s+")

# CJK 字体文件名特征（用于 Linux/mac 全盘扫描时识别）
_CJK_FILE_HINTS = (
    "wqy", "noto", "cjk", "yahei", "pingfang", "sourcehan", "hiragino",
    "simsun", "simhei", "simkai", "fandol", "uming", "ukai",
    "droidsansfallback", "fzsong", "fzhei", "stheiti", "heiti",
    "songti", "kaiti", "lantinghei", "arial unicode",
)
_FONT_EXTS = (".ttf", ".ttc", ".otf", ".dfont")
# Pillow/FreeType 下渲染半残（笔画缺失）的坏字体，直接拉黑，宁可降级用常规字重
_BROKEN_FONT_FILES = {"msyhbd.ttc", "msyhhv.ttc"}


def _is_bold_font_name(filename: str) -> bool:
    n = filename.lower()
    if any(k in n for k in ("bold", "heavy", "black", "semibold", "msyhsb")):
        return True
    return n.endswith(("bd.ttc", "bd.ttf", "b.ttf", "-bold.otf", "-heavy.otf"))


# ==========================================
# 字体探测与管理（CJK 优先：Windows / 自带 / Linux / fc-list 全覆盖）
# ==========================================
def _scan_linux_cjk_fonts() -> List[str]:
    """扫描 Linux/mac 常见字体目录，用文件名特征识别 CJK 字体"""
    found: List[str] = []
    bases = [
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.expanduser("~/.fonts"),
        os.path.expanduser("~/.local/share/fonts"),
        "/System/Library/Fonts",
        "/System/Library/Fonts/Supplemental",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
    ]
    for base in bases:
        try:
            root = Path(base)
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                try:
                    if not p.is_file():
                        continue
                except Exception:
                    continue
                name_l = p.name.lower()
                if not name_l.endswith(_FONT_EXTS):
                    continue
                if any(h in name_l for h in _CJK_FILE_HINTS):
                    found.append(str(p))
                if len(found) > 60:
                    return found
        except Exception:
            continue
    return found


def _query_fontconfig_cjk() -> List[str]:
    """用 fc-list 查询系统中文 fontconfig 字体"""
    try:
        import shutil
        import subprocess
        if not shutil.which("fc-list"):
            return []
        out = subprocess.run(
            ["fc-list", ":lang=zh", "file"],
            capture_output=True, text=True, timeout=8,
        ).stdout or ""
        files = []
        for line in out.splitlines():
            f = line.split(":")[0].strip().strip('"')
            if f and os.path.isfile(f) and f.lower().endswith(_FONT_EXTS):
                files.append(f)
        return files
    except Exception:
        return []


def _collect_font_candidates(extra_first: Optional[List[str]] = None) -> List[str]:
    """收集全来源有序字体候选：额外优先 → Windows 精选 → 自带 → Linux/mac 精确 → fc-list → 全盘扫描"""
    ordered: List[str] = []

    def _add(p: str):
        if not p or p in ordered:
            return
        if os.path.basename(p).lower() in _BROKEN_FONT_FILES:
            return
        if os.path.isfile(p):
            ordered.append(p)

    for p in extra_first or []:
        _add(p)

    windir = os.environ.get("WINDIR", "C:\\Windows")
    fonts_dir = os.path.join(windir, "Fonts")
    # Windows：微软雅黑优先（圆润耐看）> 思源 > Noto > 等线 > 黑体/宋体兜底
    # 注意 msyhbd/msyhhv 已拉黑（Pillow 下笔画残缺），粗体用 msyhsb（半粗）或思源 Bold 承担
    for n in [
        "msyh.ttc", "msyhsb.ttc",
        "SourceHanSansCN-Medium.otf", "SourceHanSansCN-Regular.otf",
        "SourceHanSansCN-Bold.otf", "SourceHanSansCN-Heavy.otf",
        "NotoSansSC-VF.ttf",
        "Deng.ttf", "Dengb.ttf",
        "simhei.ttf", "simsun.ttc",
    ]:
        _add(os.path.join(fonts_dir, n))

    # 随插件携带的字体（用户可把 .ttf/.ttc/.otf 丢进 assets/fonts/）
    try:
        if BUNDLED_FONTS_DIR.is_dir():
            for p in sorted(BUNDLED_FONTS_DIR.iterdir()):
                if p.is_file() and p.suffix.lower() in _FONT_EXTS:
                    _add(str(p))
    except Exception:
        pass

    # Linux / macOS 精确路径
    for p in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansSC-Bold.otf",
        # macOS 原生中文字体（PingFang > Hiragino > Heiti > Song）
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/PingFang SC.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Hiragino Sans GB W3.otf",
        "/System/Library/Fonts/Hiragino Sans GB W6.otf",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/System/Library/Fonts/Supplemental/Songti SC.ttc",
        "/System/Library/Fonts/Supplemental/Heiti TC.ttc",
        "/System/Library/Fonts/Supplemental/Heiti SC.ttc",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/NotoSansSC-Regular.otf",
    ]:
        _add(p)

    # fontconfig + 全盘扫描兜底（解决 Docker 精简镜像无字库导致的中文 tofu）
    for p in _query_fontconfig_cjk():
        _add(p)
    for p in _scan_linux_cjk_fonts():
        _add(p)

    return ordered


def _split_reg_bold(ordered: List[str]) -> Tuple[str, str]:
    # 按文件名区分 regular / bold
    regulars = [p for p in ordered if not _is_bold_font_name(os.path.basename(p))]
    bolds = [p for p in ordered if _is_bold_font_name(os.path.basename(p))]
    # msyh.ttc 本身含多字重，单文件时可兼任
    reg = regulars[0] if regulars else (ordered[0] if ordered else "")
    bold = bolds[0] if bolds else reg
    return reg, bold


def _find_fonts() -> Tuple[str, str]:
    """查找系统可用的中文字体，返回 (regular, bold)。找不到则返回空并打日志。"""
    ordered = _collect_font_candidates()
    reg, bold = _split_reg_bold(ordered)

    if not reg:
        logger.warning("[msg2img] 未找到任何中文字体！中文将显示为方框。请安装 fonts-noto-cjk / wqy-microhei，或把字体放入 assets/fonts/。")
    else:
        logger.info(f"[msg2img] 中文字体 regular={reg} bold={bold}")
    return reg, bold


_FONT_REGULAR_PATH, _FONT_BOLD_PATH = _find_fonts()
# 有序候选全集（get_font 回退时按此顺序尝试）
_CANDIDATE_FONTS: List[str] = []
for _p in [_FONT_REGULAR_PATH, _FONT_BOLD_PATH]:
    if _p and _p not in _CANDIDATE_FONTS:
        _CANDIDATE_FONTS.append(_p)


# ==========================================
# 字体管理器：自定义字体 / 持久化目录 / 缺字自动下载
# 插件不自带字体文件（体积+版权），缺 CJK 时从 noto-cjk 官方下载到数据目录
# ==========================================
FONT_DATA_SUBDIR = "fonts"
_NOTO_CJK_BASE = "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/SC"
BUNDLED_NOTO_FONTS = (
    ("NotoSansSC-Regular.otf", f"{_NOTO_CJK_BASE}/NotoSansSC-Regular.otf"),
    ("NotoSansSC-Bold.otf", f"{_NOTO_CJK_BASE}/NotoSansSC-Bold.otf"),
)
# 精选好看字体（开源可商用，一键下载到持久化目录，自动切换生效）
# 全部直链已校验 HEAD 200，仅保留可稳定下载的字体
CURATED_FONTS = [
    {
        "id": "noto_sans_sc",
        "name": "思源黑体",
        "desc": "现代黑体 · 干练清晰",
        "files": [
            ("NotoSansSC-Regular.otf", f"{_NOTO_CJK_BASE}/NotoSansSC-Regular.otf"),
            ("NotoSansSC-Bold.otf", f"{_NOTO_CJK_BASE}/NotoSansSC-Bold.otf"),
        ],
    },
    {
        "id": "noto_serif_sc",
        "name": "思源宋体",
        "desc": "优雅宋体 · 书卷气质",
        "files": [
            ("NotoSerifSC-Regular.otf", "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/SubsetOTF/SC/NotoSerifSC-Regular.otf"),
            ("NotoSerifSC-Bold.otf", "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/SubsetOTF/SC/NotoSerifSC-Bold.otf"),
        ],
    },
    {
        "id": "lxgw_wenkai",
        "name": "霞鹜文楷",
        "desc": "手写文楷 · 温润人文",
        "files": [
            ("LXGWWenKai-Regular.ttf", "https://raw.githubusercontent.com/lxgw/LxgwWenKai/main/fonts/TTF/LXGWWenKai-Regular.ttf"),
        ],
    },
    {
        "id": "zcool_kuaile",
        "name": "站酷快乐体",
        "desc": "卡通手写 · 活泼有力",
        "files": [
            ("ZCOOLKuaiLe-Regular.ttf", "https://cdn.jsdelivr.net/gh/googlefonts/zcool-kuaile@main/fonts/ttf/ZCOOLKuaiLe-Regular.ttf"),
        ],
    },
    {
        "id": "mashan_zheng",
        "name": "马善政毛笔",
        "desc": "毛笔楷书 · 个性张扬",
        "files": [
            ("MaShanZheng-Regular.ttf", "https://cdn.jsdelivr.net/gh/googlefonts/mashanzheng@master/fonts/ttf/MaShanZheng-Regular.ttf"),
        ],
    },
    {
        "id": "macos_pack",
        "name": "macOS 苹方",
        "desc": "苹方替代 · 优雅圆润 (思源)",
        "files": [
            ("MacOS-PingFang-Regular.otf", f"{_NOTO_CJK_BASE}/NotoSansSC-Regular.otf"),
            ("MacOS-PingFang-Bold.otf", f"{_NOTO_CJK_BASE}/NotoSansSC-Bold.otf"),
        ],
    },
]
_FONT_DL_TIMEOUT = 60
_FONT_DL_MAX_BYTES = 80 * 1024 * 1024
_FONT_MIN_BYTES = 50 * 1024

# Emoji 样式包（按需下载，默认不下载）
EMOJI_PACKS = [
    {"id": "ios", "name": "iOS 最新", "desc": "苹果全彩 · 拟真", "need_font": False},
    {"id": "android", "name": "Android 最新", "desc": "Noto 彩字 · 矢量", "need_font": True},
    {"id": "windows", "name": "Windows 最新", "desc": "Segoe 风格 · 清晰", "need_font": True},
]

# 生效中的配置（configure_fonts 写入）：font_source / custom 路径 / emoji
_FONT_CONF: Dict[str, str] = {
    "font_source": "auto",
    "custom_font_path": "",
    "custom_bold_font_path": "",
    "custom_font_url": "",
    "emoji_style": "none",
}
_FONT_DATA_DIR: Optional[Path] = None
# 生效中的有序候选（configure 后重建，get_font 优先使用）
_ACTIVE_ORDERED: List[str] = []
_ACTIVE_REGULAR = ""
_ACTIVE_BOLD = ""
_EMOJI_STYLE = "none"
# 下载状态（ Def 重复并发）
_FONT_DL_RUNNING = False


def _font_data_dir() -> Optional[Path]:
    return _data_subdir(FONT_DATA_SUBDIR)


def _is_usable_font(path: str) -> bool:
    """字体可用性校验：存在 + 体积合理 + PIL 可加载 + 中文出字"""
    try:
        if not path or not os.path.isfile(path):
            return False
        if os.path.basename(path).lower() in _BROKEN_FONT_FILES:
            return False
        if os.path.getsize(path) < _FONT_MIN_BYTES:
            return False
        f = _load_font_file(path, 30)
        if f is None:
            return False
        bbox = f.getbbox("永")
        return bool(bbox) and (bbox[2] - bbox[0]) > 0
    except Exception:
        return False


def _resolve_custom_font(raw: str) -> str:
    """解析自定义字体路径：绝对路径 / 数据目录相对路径，返回可用路径或空串"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    cands = [raw]
    if not os.path.isabs(raw):
        if _FONT_DATA_DIR is not None:
            cands.append(str(_FONT_DATA_DIR / raw))
        cands.append(str(BUNDLED_FONTS_DIR / raw))
    for c in cands:
        if _is_usable_font(c):
            return c
    return ""


def _rebuild_active_fonts():
    """按当前配置重建生效候选集（附带清空字体缓存，避免下载/删除后用旧字）"""
    global _ACTIVE_ORDERED, _ACTIVE_REGULAR, _ACTIVE_BOLD
    src = (_FONT_CONF.get("font_source") or "auto").lower()
    extra: List[str] = []

    if src == "custom":
        # 自定义优先：常规 + 粗体（粗体留空则复用常规）
        reg = _resolve_custom_font(_FONT_CONF.get("custom_font_path", ""))
        bold = _resolve_custom_font(_FONT_CONF.get("custom_bold_font_path", "")) or reg
        for p in (reg, bold):
            if p and p not in extra:
                extra.append(p)

    data_dir = _font_data_dir()
    data_fonts: List[str] = []
    if data_dir is not None:
        try:
            for p in sorted(data_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in _FONT_EXTS:
                    data_fonts.append(str(p))
        except Exception:
            pass

    if src == "system":
        ordered = _collect_font_candidates()
    else:
        # auto/custom：数据目录（含自动下载/URL 下载）优先于系统
        ordered = _collect_font_candidates(extra_first=extra + data_fonts)

    _ACTIVE_ORDERED = ordered
    _ACTIVE_REGULAR, _ACTIVE_BOLD = _split_reg_bold(ordered)
    # 同步旧全局变量（兼容直接读 _FONT_* 的外部代码）
    global _FONT_REGULAR_PATH, _FONT_BOLD_PATH, _CANDIDATE_FONTS
    if _ACTIVE_REGULAR:
        _FONT_REGULAR_PATH = _ACTIVE_REGULAR
    if _ACTIVE_BOLD:
        _FONT_BOLD_PATH = _ACTIVE_BOLD
    _CANDIDATE_FONTS = list(ordered[:2]) if ordered else list(_CANDIDATE_FONTS)
    _FONT_CACHE.clear()


def configure_fonts(config: Optional[Dict[str, Any]], data_dir: Optional[Path] = None):
    """插件/配置变更入口：同步字体与 emoji 配置并重建候选集（只读文件，不下载）"""
    global _FONT_DATA_DIR, _EMOJI_STYLE
    if data_dir is not None:
        try:
            _FONT_DATA_DIR = Path(data_dir)
        except Exception:
            pass
    if isinstance(config, dict):
        for k in _FONT_CONF:
            v = config.get(k, None)
            if isinstance(v, str):
                _FONT_CONF[k] = v.strip()
        # 兼容旧版 emoji_remote bool
        if "emoji_style" not in config and "emoji_remote" in config:
            _FONT_CONF["emoji_style"] = "android" if bool(config.get("emoji_remote")) else "none"
        _EMOJI_STYLE = (_FONT_CONF.get("emoji_style") or "none").lower()
        if _EMOJI_STYLE not in ("none", "ios", "android", "windows"):
            _EMOJI_STYLE = "none"
    _rebuild_active_fonts()


def get_emoji_storage_kb(style: Optional[str] = None) -> float:
    """计算 emoji 持久化目录占用（KB），可按样式精确过滤"""
    try:
        d = _data_subdir("emoji")
        if d is None or not d.is_dir():
            return 0.0
        total = 0
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            try:
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
        d = _data_subdir("emoji")
        if d is not None:
            p = d / fname
            if p.is_file() and p.stat().st_size > _EMOJI_MIN_FONT_BYTES:
                return str(p)
    except Exception:
        pass
    return ""


def get_emoji_packs_status() -> List[Dict[str, Any]]:
    """返回 Emoji 三样式状态（是否已下载、占用）"""
    out = []
    for pack in EMOJI_PACKS:
        sid = pack["id"]
        need_font = pack.get("need_font", False)
        if sid == "ios":
            d = _data_subdir("emoji")
            has = False
            if d and d.is_dir():
                if (d / "ios_pack.ready").is_file() or any(d.glob("*.png")):
                    has = True
            storage = get_emoji_storage_kb("ios")
            out.append({"id": sid, "name": pack["name"], "desc": pack["desc"], "installed": has, "storage_kb": storage, "need_font": need_font})
        else:
            has = bool(_style_font_path(sid))
            storage = get_emoji_storage_kb(sid)
            out.append({"id": sid, "name": pack["name"], "desc": pack["desc"], "installed": has, "storage_kb": storage, "need_font": need_font})
    return out


def download_emoji_pack(style: str) -> Dict[str, Any]:
    """下载指定 Emoji 样式资源"""
    style = (style or "").lower()
    if style not in ("ios", "android", "windows"):
        return {"ok": False, "error": "未知样式"}
    d = _data_subdir("emoji")
    if d is None:
        return {"ok": False, "error": "持久化目录不可用"}

    if style == "ios":
        base_emojis = ["1f600", "1f602", "2764", "1f44d", "2728", "1f389", "1f525", "1f4b0", "1f4ac", "2705"]
        dl_cnt = 0
        for code in base_emojis:
            p = d / f"{code}.png"
            if not p.is_file():
                if _fetch_remote_emoji(code, d):
                    dl_cnt += 1
        try:
            (d / "ios_pack.ready").write_text("ok", encoding="utf-8")
        except Exception:
            pass
        return {"ok": True, "downloaded": [f"iOS基础Emoji({dl_cnt}个)"], "storage_kb": get_emoji_storage_kb("ios")}

    # android 或 windows
    fname = ANDROID_EMOJI_FILE if style == "android" else WINDOWS_EMOJI_FILE
    furl = ANDROID_EMOJI_URL if style == "android" else WINDOWS_EMOJI_URL
    target = d / fname
    ok, err = _download_file(furl, target)
    if ok:
        _EMOJI_FONT_CACHE.clear()
        return {"ok": True, "downloaded": [fname], "storage_kb": get_emoji_storage_kb(style)}
    return {"ok": False, "error": f"{fname} 下载失败: {err or '未知错误'}", "storage_kb": 0.0}


def delete_emoji_pack(style: str) -> Dict[str, Any]:
    """删除指定 Emoji 样式资源及缓存"""
    style = (style or "").lower()
    if style not in ("ios", "android", "windows", "all"):
        return {"ok": False, "error": "未知样式"}
    clear_font_cache()
    d = _data_subdir("emoji")
    deleted = []
    search_dirs = [d] if d and d.is_dir() else []
    if _FONT_DATA_DIR and _FONT_DATA_DIR.is_dir() and _FONT_DATA_DIR not in search_dirs:
        search_dirs.append(_FONT_DATA_DIR)

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
        return {"ok": True, "deleted": deleted, "storage_kb": get_emoji_storage_kb()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def clear_font_cache():
    """清除字体缓存（删除前调用，释放 Windows 文件句柄与内存缓存）"""
    _FONT_CACHE.clear()
    _EMOJI_FONT_CACHE.clear()
    _FONT_BYTES_CACHE.clear()
    _EMOJI_IMG_CACHE.clear()
    try:
        import gc
        gc.collect()
    except Exception:
        pass


def get_font_status() -> Dict[str, Any]:
    """供 WebUI / API 查询的字体状态"""
    return {
        "has_cjk": bool(_ACTIVE_REGULAR or _FONT_REGULAR_PATH),
        "regular": _ACTIVE_REGULAR or _FONT_REGULAR_PATH,
        "bold": _ACTIVE_BOLD or _FONT_BOLD_PATH,
        "active_count": len(_ACTIVE_ORDERED),
        "font_source": _FONT_CONF.get("font_source", "auto"),
        "emoji_style": _EMOJI_STYLE,
        "emoji_font": _color_emoji_font_path(),
        "emoji_cached": len(_EMOJI_IMG_CACHE),
        "emoji_storage_kb": get_emoji_storage_kb(),
        "data_dir": str((_FONT_DATA_DIR / FONT_DATA_SUBDIR) if _FONT_DATA_DIR else ""),
    }


def needs_cjk_download() -> bool:
    """是否需要自动补字体：auto 模式且当前无可用 CJK，或 custom_url 待下载"""
    src = (_FONT_CONF.get("font_source") or "auto").lower()
    if src == "system":
        return False
    custom_url = (_FONT_CONF.get("custom_font_url") or "").strip()
    if custom_url and _FONT_DATA_DIR is not None:
        target = _font_data_dir() / _safe_font_filename(custom_url)
        if not _is_usable_font(str(target)):
            return True
    if src == "custom":
        return False
    # auto：无可用 CJK 才下载
    return not any(_is_usable_font(p) for p in _ACTIVE_ORDERED)


def _safe_font_filename(url: str) -> str:
    name = (url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "custom-font.ttf").strip()
    name = "".join(c for c in name if c.isalnum() or c in ("-", "_", "."))
    if not name.lower().endswith(_FONT_EXTS):
        name += ".ttf"
    return name or "custom-font.ttf"


def _download_file(url: str, dest: Path) -> Tuple[bool, str]:
    """标准库下载单个文件：超时 + 体积上限 + 原子落盘"""
    try:
        import urllib.request
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }
        req = urllib.request.Request(url, headers=headers)
        tmp = dest.with_suffix(dest.suffix + f".dl_{int(time.time()*1000)}")
        total = 0
        with urllib.request.urlopen(req, timeout=_FONT_DL_TIMEOUT) as resp:
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _FONT_DL_MAX_BYTES:
                        raise ValueError("文件超过体积上限，已中止")
                    f.write(chunk)
        if total < 1024:
            tmp.unlink(missing_ok=True)
            return False, "下载文件体积过小或为空"
        # 原子落盘前清缓存释放潜在读句柄
        try:
            clear_font_cache()
        except Exception:
            pass
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        os.replace(tmp, dest)
        return True, ""
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False, str(e)


def download_missing_fonts() -> Dict[str, Any]:
    """同步补齐缺失字体（调用方负责放线程池）：Noto 双字重 + custom_url。返回结果摘要。"""
    global _FONT_DL_RUNNING
    if _FONT_DL_RUNNING:
        return {"ok": False, "downloaded": [], "error": "已有下载任务在进行中"}
    _FONT_DL_RUNNING = True
    try:
        data_dir = _font_data_dir()
        if data_dir is None:
            return {"ok": False, "downloaded": [], "error": "持久化目录不可用"}
        downloaded: List[str] = []
        errors: List[str] = []
        src = (_FONT_CONF.get("font_source") or "auto").lower()

        # 1. custom_url（各模式都支持：填了就下载启用）
        custom_url = (_FONT_CONF.get("custom_font_url") or "").strip()
        if custom_url:
            if not custom_url.lower().split("?")[0].endswith(_FONT_EXTS):
                errors.append("自定义链接必须指向 .ttf/.ttc/.otf 文件")
            else:
                target = data_dir / _safe_font_filename(custom_url)
                if not _is_usable_font(str(target)):
                    ok, err = _download_file(custom_url, target)
                    if ok and _is_usable_font(str(target)):
                        downloaded.append(target.name)
                    else:
                        errors.append(f"自定义字体下载失败: {err or '校验未通过'}")

        # 2. auto 模式缺 CJK → 补 Noto Sans SC 双字重
        if src == "auto" and not any(_is_usable_font(p) for p in _ACTIVE_ORDERED):
            for fname, url in BUNDLED_NOTO_FONTS:
                target = data_dir / fname
                if _is_usable_font(str(target)):
                    continue
                ok, err = _download_file(url, target)
                if ok and _is_usable_font(str(target)):
                    downloaded.append(fname)
                else:
                    errors.append(f"{fname} 下载失败: {err or '校验未通过'}")

        _rebuild_active_fonts()
        ok_all = not errors or bool(downloaded)
        return {"ok": ok_all, "downloaded": downloaded, "error": "; ".join(errors)}
    finally:
        _FONT_DL_RUNNING = False


def delete_curated_font(cid: str) -> Dict[str, Any]:
    """删除指定精选字体包中的所有已下载文件"""
    cid = (cid or "").strip().lower()
    hit = next((x for x in CURATED_FONTS if x["id"].lower() == cid), None)
    if not hit:
        return {"ok": False, "error": f"未知字体 ID: {cid}"}
    d = _font_data_dir()
    clear_font_cache()
    deleted = []
    search_dirs = [d] if d and d.is_dir() else []
    if _FONT_DATA_DIR and _FONT_DATA_DIR.is_dir() and _FONT_DATA_DIR not in search_dirs:
        search_dirs.append(_FONT_DATA_DIR)

    for fname, _ in hit.get("files", []):
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
    clear_font_cache()
    _rebuild_active_fonts()
    return {"ok": True, "deleted": deleted}


def get_curated_fonts_status() -> List[Dict[str, Any]]:
    """返回精选字体列表及安装/激活状态，供 WebUI 展示"""
    data_dir = _font_data_dir()
    active_reg = (_ACTIVE_REGULAR or _FONT_REGULAR_PATH or "").lower()
    out = []
    for entry in CURATED_FONTS:
        files = entry.get("files", [])
        installed = 0
        total = len(files)
        for fname, _url in files:
            if data_dir is not None and _is_usable_font(str(data_dir / fname)):
                installed += 1
            elif any(_is_usable_font(p) for p in _ACTIVE_ORDERED if p.lower().endswith(fname.lower())):
                installed += 1
        # 是否当前激活（任一文件与 _ACTIVE_REGULAR 匹配即视为激活）
        active = any(active_reg.endswith(fname.lower()) for fname, _ in files) if active_reg else False
        out.append({
            "id": entry["id"],
            "name": entry["name"],
            "desc": entry["desc"],
            "installed": installed,
            "total": total,
            "active": active,
            "ready": installed == total and total > 0,
        })
    return out


def download_curated_font(curated_id: str) -> Dict[str, Any]:
    """下载指定精选字体到持久化目录（仅下载，不自动切换；切换请用持久化目录选择）"""
    global _FONT_DL_RUNNING
    if _FONT_DL_RUNNING:
        return {"ok": False, "downloaded": [], "error": "已有下载任务在进行中"}
    target_entry = next((e for e in CURATED_FONTS if e["id"] == curated_id), None)
    if target_entry is None:
        return {"ok": False, "downloaded": [], "error": "未找到该字体"}
    _FONT_DL_RUNNING = True
    try:
        data_dir = _font_data_dir()
        if data_dir is None:
            return {"ok": False, "downloaded": [], "error": "持久化目录不可用"}
        downloaded = []
        errors = []
        for fname, url in target_entry["files"]:
            target = data_dir / fname
            if _is_usable_font(str(target)):
                downloaded.append(fname)
                continue
            ok, err = _download_file(url, target)
            if ok and _is_usable_font(str(target)):
                downloaded.append(fname)
            else:
                errors.append(f"{fname} 失败: {err or '校验未通过'}")
        # 仅下载，不自动切换（由用户在持久化目录下拉选择切换）
        if downloaded:
            _rebuild_active_fonts()
        ok_all = not errors or bool(downloaded)
        return {"ok": ok_all, "downloaded": downloaded, "error": "; ".join(errors), "active": target_entry["id"]}
    finally:
        _FONT_DL_RUNNING = False

_FONT_CACHE: Dict[Tuple, Any] = {}
_EMOJI_FONT_CACHE: Dict[int, Tuple[Optional[ImageFont.FreeTypeFont], bool]] = {}
_FONT_BYTES_CACHE: Dict[str, bytes] = {}
_EMOJI_IMG_CACHE: Dict[Tuple, Optional[Image.Image]] = {}
# emoji 图片缓存上限（防止超长刷屏消息撑爆内存）
_EMOJI_IMG_CACHE_LIMIT = 400


def is_emoji_char(char: str) -> bool:
    if not char:
        return False
    cp = ord(char[0])
    return (
        0x1F300 <= cp <= 0x1FAFF or
        0x2600 <= cp <= 0x27BF or
        0x1F1E0 <= cp <= 0x1F1FF or
        0x1F600 <= cp <= 0x1F64F or
        0x1F680 <= cp <= 0x1F6FF or
        0x2300 <= cp <= 0x23FF or
        0x2B00 <= cp <= 0x2BFF or
        0x1F900 <= cp <= 0x1F9FF or
        0x1F000 <= cp <= 0x1F2FF or
        0xFE00 <= cp <= 0xFE0F or
        0x1F3FB <= cp <= 0x1F3FF or  # 肤色修饰符
        0x200D == cp or  # ZWJ 连接符
        0x20E3 == cp or  # 键帽组合符
        0x2C00 <= cp <= 0x2C5F or
        cp in (0x00A9, 0x00AE)
    )


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


def _load_font_file(path: str, size: int) -> Optional[ImageFont.FreeTypeFont]:
    """加载单个字体文件；使用内存 BytesIO，避免 Windows 下文件句柄常驻锁定导致无法删除"""
    try:
        if not path or not os.path.isfile(path):
            return None
        data = _FONT_BYTES_CACHE.get(path)
        if data is None:
            try:
                with open(path, "rb") as fp:
                    data = fp.read()
                # 仅对小于 60MB 的字体文件缓存内存字节，避免过多消耗内存
                if len(data) <= 60 * 1024 * 1024:
                    _FONT_BYTES_CACHE[path] = data
            except Exception:
                return None
        if not data:
            return None
        if path.lower().endswith((".ttc", ".otc")):
            for idx in range(4):
                try:
                    return ImageFont.truetype(io.BytesIO(data), size, index=idx)
                except Exception:
                    continue
            return None
        return ImageFont.truetype(io.BytesIO(data), size)
    except Exception:
        return None


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """获取 CJK 字体。优先级：配置生效集（自定义/下载/系统）→ import 时探测结果。"""
    cache_key = (size, bold)
    if cache_key in _FONT_CACHE:
        return _FONT_CACHE[cache_key]

    primary = (_ACTIVE_BOLD if bold else _ACTIVE_REGULAR) or (_FONT_BOLD_PATH if bold else _FONT_REGULAR_PATH)
    targets = [primary] if primary else []
    for fb in _ACTIVE_ORDERED + _CANDIDATE_FONTS:
        if fb and fb not in targets:
            targets.append(fb)

    for target in targets:
        f = _load_font_file(target, size)
        if f is not None:
            _FONT_CACHE[cache_key] = f
            return f

    f = ImageFont.load_default()
    _FONT_CACHE[cache_key] = f
    return f


def _char_advance(font: ImageFont.FreeTypeFont, char: str) -> float:
    """获取单个字符的推进宽度（advance width），使用 getlength 而非 getbbox"""
    try:
        return font.getlength(char)
    except Exception:
        # 极旧 Pillow 回退
        try:
            bbox = font.getbbox(char)
            return float(bbox[2] - bbox[0]) if bbox else 14.0
        except Exception:
            return 14.0


# ==========================================
# 高雅现代配色体系
# ==========================================
@dataclass
class ThemeColors:
    bg_gradient_start: Tuple[int, int, int]
    bg_gradient_end: Tuple[int, int, int]
    card_bg: Tuple[int, int, int, int]
    card_border: Tuple[int, int, int, int]
    card_shadow: Tuple[int, int, int, int]
    text_primary: Tuple[int, int, int]
    text_secondary: Tuple[int, int, int]
    text_muted: Tuple[int, int, int]
    accent: Tuple[int, int, int]
    accent_bg: Tuple[int, int, int, int]
    code_bg: Tuple[int, int, int, int]
    code_border: Tuple[int, int, int, int]
    code_text: Tuple[int, int, int]
    divider: Tuple[int, int, int, int]
    star_colors: List[Tuple[int, int, int]]


THEMES = {
    ("ios", "light"): ThemeColors(
        bg_gradient_start=(242, 245, 250),
        bg_gradient_end=(228, 235, 246),
        card_bg=(255, 255, 255, 245),
        card_border=(255, 255, 255, 255),
        card_shadow=(18, 38, 70, 22),
        text_primary=(28, 32, 40),
        text_secondary=(82, 92, 108),
        text_muted=(140, 150, 168),
        accent=(0, 122, 255),
        accent_bg=(0, 122, 255, 18),
        code_bg=(244, 247, 252, 255),
        code_border=(220, 226, 238, 200),
        code_text=(32, 42, 58),
        divider=(232, 238, 246, 200),
        star_colors=[(255, 214, 102), (160, 200, 255), (255, 195, 240), (140, 235, 245), (255, 255, 255)],
    ),
    ("ios", "dark"): ThemeColors(
        bg_gradient_start=(18, 20, 26),
        bg_gradient_end=(10, 12, 16),
        card_bg=(30, 33, 42, 235),
        card_border=(255, 255, 255, 32),
        card_shadow=(0, 0, 0, 80),
        text_primary=(240, 242, 248),
        text_secondary=(155, 165, 180),
        text_muted=(100, 110, 126),
        accent=(10, 132, 255),
        accent_bg=(10, 132, 255, 36),
        code_bg=(22, 24, 32, 255),
        code_border=(50, 56, 72, 180),
        code_text=(215, 224, 240),
        divider=(46, 52, 66, 160),
        star_colors=[(255, 218, 120), (170, 210, 255), (255, 200, 245), (150, 245, 255), (245, 248, 255)],
    ),
    ("android16", "light"): ThemeColors(
        bg_gradient_start=(248, 245, 240),
        bg_gradient_end=(238, 232, 224),
        card_bg=(255, 255, 255, 248),
        card_border=(228, 220, 210, 180),
        card_shadow=(65, 50, 35, 20),
        text_primary=(32, 26, 20),
        text_secondary=(96, 85, 75),
        text_muted=(148, 136, 124),
        accent=(125, 82, 18),
        accent_bg=(245, 225, 195, 180),
        code_bg=(246, 242, 236, 255),
        code_border=(226, 218, 206, 200),
        code_text=(45, 38, 30),
        divider=(232, 225, 215, 180),
        star_colors=[(255, 185, 70), (130, 205, 140), (180, 160, 225), (255, 210, 130), (255, 255, 255)],
    ),
    ("android16", "dark"): ThemeColors(
        bg_gradient_start=(22, 22, 25),
        bg_gradient_end=(14, 14, 16),
        card_bg=(32, 33, 38, 240),
        card_border=(255, 255, 255, 26),
        card_shadow=(0, 0, 0, 85),
        text_primary=(238, 236, 230),
        text_secondary=(165, 160, 152),
        text_muted=(112, 108, 102),
        accent=(238, 196, 140),
        accent_bg=(238, 196, 140, 32),
        code_bg=(24, 25, 28, 255),
        code_border=(58, 60, 68, 180),
        code_text=(228, 224, 216),
        divider=(50, 52, 58, 180),
        star_colors=[(255, 210, 130), (165, 215, 170), (210, 150, 220), (255, 225, 140), (248, 248, 248)],
    ),
}


# ==========================================
# 文本结构化解析与美学折行
# ==========================================
@dataclass
class LineBlock:
    text: str
    block_type: str  # "heading1", "heading2", "heading3", "code", "quote", "bullet", "divider", "text"
    font_size: int
    is_bold: bool


def _parse_content_blocks(raw_text: str) -> List[LineBlock]:
    lines = raw_text.strip().splitlines()
    blocks: List[LineBlock] = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            blocks.append(LineBlock(text=line, block_type="code", font_size=24, is_bold=False))
            continue

        if not stripped:
            blocks.append(LineBlock(text="", block_type="text", font_size=26, is_bold=False))
            continue

        if _RE_DIVIDER.match(stripped):
            blocks.append(LineBlock(text="", block_type="divider", font_size=16, is_bold=False))
            continue

        if stripped.startswith("### "):
            blocks.append(LineBlock(text=stripped[4:], block_type="heading3", font_size=28, is_bold=True))
            continue
        elif stripped.startswith("## "):
            blocks.append(LineBlock(text=stripped[3:], block_type="heading2", font_size=32, is_bold=True))
            continue
        elif stripped.startswith("# "):
            blocks.append(LineBlock(text=stripped[2:], block_type="heading1", font_size=36, is_bold=True))
            continue

        if stripped.startswith("> "):
            blocks.append(LineBlock(text=stripped[2:], block_type="quote", font_size=25, is_bold=False))
            continue

        if _RE_BULLET.match(stripped):
            blocks.append(LineBlock(text=stripped, block_type="bullet", font_size=26, is_bold=False))
            continue

        blocks.append(LineBlock(text=line, block_type="text", font_size=26, is_bold=False))

    return blocks


# ==========================================
# Emoji 全自动管线：本地 PNG → 数据目录 → 云端补全 → 系统字体 → 正文单色
# 用户零选择：有图用图，无图自动降级，永不崩溃
# 云端图源 Twemoji (CC-BY 4.0)，按需下载单个 72x72 PNG 并持久缓存
# ==========================================
_TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
_EMOJI_DL_TIMEOUT = 8
_EMOJI_DL_MAX_BYTES = 2 * 1024 * 1024
# Noto 全彩 Emoji 字体（Android 原生风格）
ANDROID_EMOJI_FILE = "NotoColorEmoji.ttf"
ANDROID_EMOJI_URL = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/fonts/NotoColorEmoji.ttf"
# EmojiOne/Twemoji 全彩字体（跨平台标准矢量风格）
WINDOWS_EMOJI_FILE = "EmojiOneColor.otf"
WINDOWS_EMOJI_URL = "https://raw.githubusercontent.com/adobe-fonts/emojione-color/master/EmojiOneColor.otf"
_EMOJI_MIN_FONT_BYTES = 1 * 1024 * 1024

# CDN 不可达时退避（DNS 黑洞等只挡一次，10 分钟内不再尝试，避免消息延迟）
_EMOJI_REMOTE_DEAD_UNTIL = 0.0


def _data_subdir(name: str) -> Optional[Path]:
    global _FONT_DATA_DIR
    if _FONT_DATA_DIR is None:
        try:
            from .config import resolve_data_dir
            _FONT_DATA_DIR = resolve_data_dir()
        except Exception:
            try:
                from core.config import resolve_data_dir
                _FONT_DATA_DIR = resolve_data_dir()
            except Exception:
                pass
    if _FONT_DATA_DIR is None:
        return None
    try:
        d = _FONT_DATA_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def _font_data_dir() -> Optional[Path]:
    return _data_subdir(FONT_DATA_SUBDIR)


def _take_cluster(text: str, i: int) -> Tuple[str, int]:
    """取一个显示单元：Emoji 簇（含 VS16/肤色/ZWJ 连接/键帽）或单个普通字符，保证不断开"""
    ch = text[i]
    # 键帽序列起点：# * 0-9，且 2 步内出现 20E3
    if ch in "#*0123456789":
        j = i + 1
        if j < len(text) and ord(text[j]) == 0xFE0F:
            j += 1
        if j < len(text) and ord(text[j]) == 0x20E3:
            return text[i:j + 1], j + 1
        return ch, i + 1
    if not is_emoji_char(ch):
        return ch, i + 1
    parts = [ch]
    j = i + 1
    n = len(text)
    while j < n:
        cp = ord(text[j])
        if cp == 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF:
            parts.append(text[j])
            j += 1
        elif cp == 0x200D and j + 1 < n:
            parts.append(text[j])
            parts.append(text[j + 1])
            j += 2
        else:
            break
    return "".join(parts), j


def _is_emoji_cluster(cluster: str) -> bool:
    return len(cluster) > 1 or is_emoji_char(cluster[:1])


def _cluster_codes(cluster: str) -> List[str]:
    """簇对应的图文件名候选：完整码位优先，去 FE0F 兜底（兼容 Twemoji 命名）"""
    full = "-".join(f"{ord(c):x}" for c in cluster)
    stripped = "-".join(f"{ord(c):x}" for c in cluster if ord(c) != 0xFE0F)
    return [full] if full == stripped else [full, stripped]


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


def _fetch_remote_emoji(code: str, dest_dir: Path) -> bool:
    """从 Twemoji CDN 按需下载单个 emoji，成功返回 True；网络不可达时全局退避"""
    global _EMOJI_REMOTE_DEAD_UNTIL
    try:
        import time as _time
        import urllib.error
        import urllib.request
        if _time.time() < _EMOJI_REMOTE_DEAD_UNTIL:
            return False
        url = f"{_TWEMOJI_BASE}/{code}.png"
        req = urllib.request.Request(url, headers={"User-Agent": "astrbot-msg2img/1.3"})
        tmp = dest_dir / f"{code}.png.downloading"
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
                return False  # CDN 可达只是没这个文件，不退避
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            _EMOJI_REMOTE_DEAD_UNTIL = _time.time() + 600
            return False
        os.replace(tmp, dest_dir / f"{code}.png")
        return True
    except Exception:
        try:
            (dest_dir / f"{code}.png.downloading").unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _color_emoji_font_path() -> str:
    """Noto 全彩字体路径：数据目录优先，其次系统已装"""
    try:
        d = _data_subdir("emoji")
        if d is not None:
            p = d / NOTO_EMOJI_FILE
            if p.is_file() and p.stat().st_size > _NOTO_EMOJI_MIN_BYTES:
                return str(p)
    except Exception:
        pass
    for cand in (
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/NotoColorEmoji.ttf",
        "C:/Windows/Fonts/NotoColorEmoji.ttf",
    ):
        try:
            if os.path.isfile(cand) and os.path.getsize(cand) > _NOTO_EMOJI_MIN_BYTES:
                return cand
        except Exception:
            continue
    return ""


def ensure_emoji_assets(allow_remote: bool = True) -> Dict[str, Any]:
    """补齐 Emoji 资产（调用方放线程池）：缺 Noto 全彩字体则下载。返回摘要。"""
    if not allow_remote:
        return {"ok": True, "downloaded": [], "error": ""}
    data_dir = _data_subdir("emoji")
    if data_dir is None:
        return {"ok": False, "downloaded": [], "error": "持久化目录不可用"}
    if _color_emoji_font_path():
        return {"ok": True, "downloaded": [], "error": ""}
    target = data_dir / NOTO_EMOJI_FILE
    ok, err = _download_file(NOTO_EMOJI_URL, target)
    if ok:
        try:
            if not _color_emoji_font_path():
                raise ValueError("字体校验未通过")
        except Exception as e:
            err = str(e)
            ok = False
    if ok:
        _EMOJI_FONT_CACHE.clear()
        return {"ok": True, "downloaded": [NOTO_EMOJI_FILE], "error": ""}
    return {"ok": False, "downloaded": [], "error": f"{NOTO_EMOJI_FILE} 下载失败: {err or '未知错误'}"}


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
    data_dir = _data_subdir("emoji")
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


def _draw_mixed_text(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Any,
    font_size: int = 26,
    emoji_remote: bool = True,
    emoji_style: str = "android",
) -> List[Tuple[float, float, float, str]]:
    """
    绘制混合文字：复杂簇（ZWJ/键帽/多码点）优先全彩图；
    单个 emoji 优先全彩字体（Noto 真彩 / 系统），缺图缺字再降级；
    都没有则随正文字体单色绘制。连续普通字符批量绘制。
    根据字体基线精准对齐 Emoji 垂直居中位置。
    返回每个字符的 (x_start, char_width, y, char) 列表，供打码定位使用。
    """
    emoji_font, emoji_is_color = get_emoji_font(font_size)
    cur_x = x
    i = 0
    text_len = len(text)
    char_positions: List[Tuple[float, float, float, str]] = []

    # 精确计算 Emoji 垂直居中偏移量，与中文正文字形高度完美对齐
    emo_offset_y = max(0, int(round(font_size * 0.08)))

    def _paste_image(cluster: str) -> bool:
        """全彩图绘制一簇，成功返回 True"""
        nonlocal cur_x
        emo_img = _get_emoji_image(cluster, font_size, emoji_remote)
        if emo_img is None:
            return False
        canvas.paste(emo_img, (int(cur_x), int(y + emo_offset_y)), emo_img)
        w = font_size + 2
        char_positions.append((cur_x, w, y, cluster))
        cur_x += w
        return True

    def _draw_with_font(cluster: str) -> bool:
        """系统/全彩字体绘制单个 emoji，成功返回 True"""
        nonlocal cur_x
        if len(cluster) != 1 or not is_emoji_char(cluster) or emoji_font is None:
            return False
        emo_font_y = y + max(0, int(round(font_size * 0.04)))
        try:
            draw.text((cur_x, emo_font_y), cluster, font=emoji_font, fill=fill,
                      embedded_color=emoji_is_color)
        except TypeError:
            draw.text((cur_x, emo_font_y), cluster, font=emoji_font, fill=fill)
        w = max(_char_advance(emoji_font, cluster), float(font_size + 2))
        char_positions.append((cur_x, w, y, cluster))
        cur_x += w
        return True

    def _take_batch() -> List[str]:
        """收集一批正文（含无图无字 emoji 的单色回退），必推进"""
        nonlocal i
        batch_chars = []
        while i < text_len:
            c2, n2 = _take_cluster(text, i)
            if _is_emoji_cluster(c2):
                if len(c2) > 1:
                    if _get_emoji_image(c2, font_size, emoji_remote) is not None:
                        break
                elif emoji_font is not None:
                    break
                elif _get_emoji_image(c2, font_size, emoji_remote) is not None:
                    break
            batch_chars.append(c2)
            i = n2
        return batch_chars

    while i < text_len:
        cluster, ni = _take_cluster(text, i)

        if _is_emoji_cluster(cluster):
            if len(cluster) > 1:
                # 复杂簇：全彩图最稳（字体塑形不可靠），无图则整体单色绘制
                if _paste_image(cluster):
                    i = ni
                    continue
            else:
                # 单个 emoji：按风格决定优先
                if emoji_style == "ios":
                    if _paste_image(cluster):
                        i = ni
                        continue
                    if _draw_with_font(cluster):
                        i = ni
                        continue
                else:
                    if _draw_with_font(cluster):
                        i = ni
                        continue
                    if _paste_image(cluster):
                        i = ni
                        continue

        # 正文批量绘制（含无图无字 emoji 的单色回退）：_take_batch 必推进
        batch_chars = _take_batch()
        if not batch_chars:
            # 理论上到不了：强制单簇推进，杜绝死循环
            batch_chars = [cluster]
            i = ni
        batch_str = "".join(batch_chars)
        draw.text((cur_x, y), batch_str, font=font, fill=fill)
        for bc in batch_chars:
            if len(bc) == 1:
                w = _char_advance(font, bc)
            else:
                w = sum(_char_advance(font, c) for c in bc) or float(font_size)
            char_positions.append((cur_x, w, y, bc))
            cur_x += w

    return char_positions


def _wrap_text_line(text: str, font: ImageFont.FreeTypeFont, max_width: int, font_size: int = 26) -> List[str]:
    """Emoji-aware 文本折行：按显示簇切分（不断开 ZWJ/键帽序列），同簇去重预量宽度"""
    if not text:
        return [""]

    units: List[Tuple[str, float]] = []
    width_cache: Dict[str, float] = {}
    i, n = 0, len(text)
    while i < n:
        cluster, i = _take_cluster(text, i)
        if _is_emoji_cluster(cluster):
            w = float(font_size + 2)
        else:
            w = 0.0
            for c in cluster:
                if c not in width_cache:
                    width_cache[c] = _char_advance(font, c)
                w += width_cache[c]
        units.append((cluster, w))

    lines = []
    current_line = ""
    current_width = 0.0
    for unit, w in units:
        if current_width + w <= max_width:
            current_line += unit
            current_width += w
        else:
            if current_line:
                lines.append(current_line)
            current_line = unit
            current_width = w

    if current_line:
        lines.append(current_line)

    return lines if lines else [""]


# ==========================================
# 矢量星芒绘制与平滑渐变加速
# ==========================================
def _draw_star_sparkle(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    radius: float,
    color: Tuple[int, int, int],
    alpha: int,
    star_type: str = "sparkle",
):
    fill_rgba = (*color, alpha)
    # 柔和外发光微晕，消除像素锯齿感
    glow_r = radius * 1.6
    draw.ellipse([cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r], fill=(*color, max(8, int(alpha * 0.18))))

    if star_type == "sparkle":
        points = []
        r_inner = radius * 0.24
        r_outer = radius
        for i in range(8):
            angle = i * (math.pi / 4)
            r = r_outer if i % 2 == 0 else r_inner
            points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        draw.polygon(points, fill=fill_rgba)
        core_r = max(1.2, radius * 0.22)
        draw.ellipse([cx - core_r, cy - core_r, cx + core_r, cy + core_r], fill=(255, 255, 255, min(255, alpha + 70)))

    elif star_type == "cross":
        thick = max(1.2, radius * 0.16)
        draw.rectangle([cx - radius, cy - thick / 2, cx + radius, cy + thick / 2], fill=fill_rgba)
        draw.rectangle([cx - thick / 2, cy - radius, cx + thick / 2, cy + radius], fill=fill_rgba)
        core_r = max(1.0, radius * 0.22)
        draw.ellipse([cx - core_r, cy - core_r, cx + core_r, cy + core_r], fill=(255, 255, 255, alpha))

    else:
        # diamond
        draw.polygon([(cx, cy - radius), (cx + radius * 0.65, cy), (cx, cy + radius), (cx - radius * 0.65, cy)], fill=fill_rgba)
        core_r = max(1.0, radius * 0.18)
        draw.ellipse([cx - core_r, cy - core_r, cx + core_r, cy + core_r], fill=(255, 255, 255, min(255, alpha + 50)))


def _fast_linear_gradient(
    width: int,
    height: int,
    start_color: Tuple[int, int, int],
    end_color: Tuple[int, int, int],
) -> Image.Image:
    """极速生成平滑双向渐变底图（1D 生成后水平拉伸，< 4ms）"""
    import numpy as np
    start = np.array(start_color, dtype=np.float32)
    end = np.array(end_color, dtype=np.float32)
    alpha = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    col = (start * (1 - alpha) + end * alpha).astype(np.uint8)
    col_img = Image.fromarray(col.reshape(height, 1, 3), "RGB")
    return col_img.resize((width, height), Image.NEAREST).convert("RGBA")


# ==========================================
# 核心渲染主类
# ==========================================
# 卡片投影缓存（同尺寸/圆角/颜色直接复用，省掉每次高斯模糊）
_SHADOW_CACHE: Dict[Tuple, Image.Image] = {}
_SHADOW_CACHE_LIMIT = 12


def _get_card_shadow(card_w: int, card_h: int, radius: int,
                     color: Tuple[int, int, int, int], blur_rad: int = 14) -> Image.Image:
    key = (card_w, card_h, radius, color, blur_rad)
    hit = _SHADOW_CACHE.get(key)
    if hit is not None:
        return hit
    pad = blur_rad * 2
    layer = Image.new("RGBA", (card_w + pad * 2, card_h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(
        [pad, pad + 4, pad + card_w, pad + card_h + 4],
        radius=radius,
        fill=color,
    )
    shadow_img = layer.filter(ImageFilter.GaussianBlur(blur_rad))
    if len(_SHADOW_CACHE) >= _SHADOW_CACHE_LIMIT:
        _SHADOW_CACHE.pop(next(iter(_SHADOW_CACHE)))
    _SHADOW_CACHE[key] = shadow_img
    return shadow_img


def _stable_seed(text: str) -> int:
    """跨进程稳定的星空随机种子（hash() 每次启动盐值不同，不可用）"""
    digest = hashlib.md5(text.encode("utf-8", errors="ignore")).digest()
    return int.from_bytes(digest[:8], "little") ^ 1024


def _text_size(font: ImageFont.FreeTypeFont, s: str) -> Tuple[float, float]:
    """兼容多版本 Pillow 的文本宽高测量"""
    try:
        w = font.getlength(s)
    except Exception:
        try:
            bbox = font.getbbox(s)
            w = float(bbox[2] - bbox[0]) if bbox else 0.0
        except Exception:
            w = 0.0
    try:
        bbox = font.getbbox(s)
        h = float(bbox[3] - bbox[1]) if bbox else 0.0
    except Exception:
        h = 0.0
    return w, h


class MessageImageRenderer:
    @classmethod
    def render(
        cls,
        text: str,
        style: str = "ios",
        theme_mode: str = "light",
        star_background: bool = True,
        star_density: str = "medium",
        mosaic_mode: str = "none",  # "none", "half", "full"
        mosaic_type: str = "pixel",  # "pixel", "blur"
        violation_words: Optional[List[str]] = None,
        emoji_remote: bool = True,  # 缺失 emoji 是否云端自动补全
        mosaic_half_pos: str = "bottom",  # half 模式打码位置: bottom/top/random
        font_scale: int = 100,  # 字体百分比 70-150
        emoji_style: Optional[str] = None,  # none/ios/android/windows，None 则用全局配置
    ) -> Image.Image:
        # 1. 参数归一化（先 lower 再校验，兼容 "IOS"/"Light" 等大小写）
        text = str(text or "").strip()
        style = str(style or "ios").lower()
        if style not in ("ios", "android16"):
            style = "ios"
        theme_mode = str(theme_mode or "light").lower()
        if theme_mode not in ("light", "dark"):
            theme_mode = "light"
        theme = THEMES.get((style, theme_mode), THEMES[("ios", "light")])
        mosaic_half_pos = str(mosaic_half_pos or "bottom").lower()
        if mosaic_half_pos not in ("top", "bottom", "random"):
            mosaic_half_pos = "bottom"
        try:
            font_scale = int(font_scale)
        except Exception:
            font_scale = 100
        font_scale = max(50, min(500, font_scale))
        _sc = lambda v: max(6, int(round(v * font_scale / 100)))
        # emoji 样式归一化（兼容旧 bool）
        if emoji_style is None:
            # 兼容旧调用：emoji_remote bool
            if isinstance(emoji_remote, str):
                emoji_style = str(emoji_remote).lower()
                if emoji_style not in ("none", "ios", "android", "windows"):
                    emoji_style = _EMOJI_STYLE
            else:
                emoji_style = _EMOJI_STYLE if _EMOJI_STYLE in ("none", "ios", "android", "windows") else ("android" if bool(emoji_remote) else "none")
        else:
            emoji_style = str(emoji_style).lower()
            if emoji_style not in ("none", "ios", "android", "windows"):
                emoji_style = "none"
        # 是否允许云端
        emoji_remote_eff = emoji_style != "none"

        # 2. 动态自适应卡片宽度（支持字体百分比缩放自适应撑缩）
        blocks = _parse_content_blocks(text)
        if font_scale != 100:
            for b in blocks:
                b.font_size = _sc(b.font_size)

        max_natural_w = 0.0
        for b in blocks:
            if not b.text or b.block_type == "divider":
                continue
            font = get_font(b.font_size, bold=b.is_bold)
            w, _ = _text_size(font, b.text)
            if w > max_natural_w:
                max_natural_w = w

        # 自适应留白与宽度基准：随 font_scale 动态放缩，50% 小巧，500% 宽阔
        scale_ratio = font_scale / 100.0
        inner_pad_x = max(24, int(round(40 * min(1.8, max(0.7, scale_ratio)))))
        min_w = max(380, int(round(560 * min(2.5, max(0.65, scale_ratio)))))
        max_w = max(min_w + 120, int(round(960 * min(2.8, max(0.75, scale_ratio)))))
        card_w = max(min_w, min(max_w, int(max_natural_w) + inner_pad_x * 2 + int(40 * scale_ratio)))
        content_w = card_w - inner_pad_x * 2

        rendered_lines: List[Tuple[str, LineBlock, int]] = []
        for b in blocks:
            if b.block_type == "divider":
                rendered_lines.append(("", b, 24))
                continue
            font = get_font(b.font_size, bold=b.is_bold)
            if not b.text:
                rendered_lines.append(("", b, int(b.font_size * 0.8)))
                continue

            sub_lines = _wrap_text_line(b.text, font, content_w, b.font_size)
            line_h = int(b.font_size * 1.56)
            for sl in sub_lines:
                rendered_lines.append((sl, b, line_h))

        # 3. 计算高度与留白（上下对齐：顶部 header 与底部 footer 对称留白）
        content_h = sum(lh for _, _, lh in rendered_lines)
        header_h = 52 if style == "ios" else 40
        card_inner_pad_y = 24
        footer_gap = 14  # 正文与底部分割线间距（与顶部 header->正文 10px 对称）
        foot_font_tmp = get_font(11, bold=False)
        _, foot_fh = _text_size(foot_font_tmp, "Ag")
        foot_fh = max(12, int(foot_fh) or 12)
        footer_block = 1 + 8 + foot_fh  # 分割线(1) + 间距(8) + 文字高度
        card_h = card_inner_pad_y + header_h + content_h + footer_gap + footer_block + card_inner_pad_y
        # 500% 极限防护：卡片过高直接截断，避免 OOM/浏览器卡死
        if card_h > 3800:
            # 按比例压缩内容高度（保留头部/底部）
            max_content = 3800 - (card_inner_pad_y*2 + header_h + footer_gap + footer_block)
            if content_h > max_content:
                # 截断 rendered_lines
                acc = 0
                cut_idx = 0
                for i, (_, _, lh) in enumerate(rendered_lines):
                    if acc + lh > max_content:
                        cut_idx = i
                        break
                    acc += lh
                if cut_idx:
                    rendered_lines = rendered_lines[:cut_idx]
                    # 追加省略提示
                    ellipsis_font = get_font(_sc(26), bold=False)
                    rendered_lines.append(("… 内容过长已截断 …", LineBlock(text="…", block_type="text", font_size=_sc(26), is_bold=False), int(_sc(26)*1.56)))
                    content_h = sum(lh for _, _, lh in rendered_lines)
                    card_h = card_inner_pad_y + header_h + content_h + footer_gap + footer_block + card_inner_pad_y

        margin_x = 36
        margin_y = 36
        canvas_w = card_w + margin_x * 2
        canvas_h = card_h + margin_y * 2

        # 4. 极速生成背景渐变
        canvas = _fast_linear_gradient(canvas_w, canvas_h, theme.bg_gradient_start, theme.bg_gradient_end)

        # 5. 星空微粒背景 (主要散布在卡片外部留白与边缘四周，避免在卡片文字区域形成杂乱噪点)
        if star_background:
            star_counts = {"sparse": 18, "medium": 36, "dense": 60}
            count = star_counts.get(star_density, 36)
            star_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
            s_draw = ImageDraw.Draw(star_layer)

            rng = random.Random(_stable_seed(text))
            for _ in range(count):
                zone = rng.choice(["top", "bottom", "left", "right", "corner", "bg"])
                if zone == "top":
                    sx = rng.uniform(8, canvas_w - 8)
                    sy = rng.uniform(8, max(margin_y + 12, 40))
                elif zone == "bottom":
                    sx = rng.uniform(8, canvas_w - 8)
                    sy = rng.uniform(min(margin_y + card_h - 12, canvas_h - 40), canvas_h - 8)
                elif zone == "left":
                    sx = rng.uniform(8, max(margin_x + 12, 40))
                    sy = rng.uniform(8, canvas_h - 8)
                elif zone == "right":
                    sx = rng.uniform(min(margin_x + card_w - 12, canvas_w - 40), canvas_w - 8)
                    sy = rng.uniform(8, canvas_h - 8)
                else:
                    sx = rng.uniform(8, canvas_w - 8)
                    sy = rng.uniform(8, canvas_h - 8)

                s_radius = rng.uniform(2.8, 8.5)
                s_color = rng.choice(theme.star_colors)
                s_alpha = rng.randint(85, 200)
                st_type = rng.choice(["sparkle", "cross", "diamond"])
                _draw_star_sparkle(s_draw, sx, sy, s_radius, s_color, s_alpha, st_type)

            canvas = Image.alpha_composite(canvas, star_layer)

        # 6. 轻量柔和卡片投影（同尺寸直接复用缓存）
        corner_radius = 24 if style == "ios" else 32
        blur_rad = 14
        pad = blur_rad * 2
        shadow_img = _get_card_shadow(card_w, card_h, corner_radius, theme.card_shadow, blur_rad)
        canvas.paste(shadow_img, (margin_x - pad, margin_y - pad), shadow_img)

        # 7. 绘制卡片底板
        card_surface = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        c_draw = ImageDraw.Draw(card_surface)

        c_draw.rounded_rectangle(
            [0, 0, card_w, card_h],
            radius=corner_radius,
            fill=theme.card_bg,
            outline=theme.card_border,
            width=2 if style == "ios" else 1,
        )

        # 8. 绘制顶部栏
        cur_y = card_inner_pad_y

        if style == "ios":
            dot_y = cur_y + 12
            dot_r = 5.5
            c_draw.ellipse([inner_pad_x, dot_y - dot_r, inner_pad_x + dot_r * 2, dot_y + dot_r], fill=(255, 95, 87))
            c_draw.ellipse([inner_pad_x + 18, dot_y - dot_r, inner_pad_x + 18 + dot_r * 2, dot_y + dot_r], fill=(254, 188, 46))
            c_draw.ellipse([inner_pad_x + 36, dot_y - dot_r, inner_pad_x + 36 + dot_r * 2, dot_y + dot_r], fill=(40, 200, 64))

            time_font = get_font(13, bold=False)
            time_str = time.strftime("%H:%M")
            t_w, _ = _text_size(time_font, time_str)
            c_draw.text((card_w - inner_pad_x - t_w, cur_y + 4), time_str, font=time_font, fill=theme.text_muted)

            c_draw.line([(inner_pad_x, cur_y + header_h - 10), (card_w - inner_pad_x, cur_y + header_h - 10)], fill=theme.divider, width=1)
            cur_y += header_h

        else:
            # Android 16：无品牌顶栏，仅右侧时间（头像/账号已移除）
            time_font = get_font(12, bold=False)
            time_str = time.strftime("%m-%d %H:%M")
            t_w, _ = _text_size(time_font, time_str)
            c_draw.text((card_w - inner_pad_x - t_w, cur_y + 10), time_str, font=time_font, fill=theme.text_muted)

            c_draw.line([(inner_pad_x, cur_y + header_h - 10), (card_w - inner_pad_x, cur_y + header_h - 10)], fill=theme.divider, width=1)
            cur_y += header_h

        # 9. 绘制正文内容
        content_x = inner_pad_x
        # 所有已绘制字符的位置信息 [(char_x, char_w, char_y, char_str, line_h), ...]
        all_char_positions: List[Tuple[float, float, float, str, int]] = []

        for wrap_line, block, line_h in rendered_lines:
            if block.block_type == "divider":
                div_y = cur_y + line_h // 2
                c_draw.line([(content_x, div_y), (card_w - content_x, div_y)], fill=theme.divider, width=1)
                cur_y += line_h
                continue

            if not wrap_line:
                cur_y += line_h
                continue

            font = get_font(block.font_size, bold=block.is_bold)

            if block.block_type == "code":
                c_draw.rounded_rectangle(
                    [content_x - 6, cur_y - 2, card_w - content_x + 6, cur_y + line_h - 2],
                    radius=6,
                    fill=theme.code_bg,
                    outline=theme.code_border,
                    width=1,
                )
                positions = _draw_mixed_text(card_surface, c_draw, content_x + 6, cur_y, wrap_line, font, theme.code_text, block.font_size, emoji_remote_eff, emoji_style)
                for px, pw, py, pc in positions:
                    all_char_positions.append((px, pw, py, pc, line_h))
            elif block.block_type == "quote":
                c_draw.rounded_rectangle([content_x, cur_y, content_x + 3, cur_y + line_h - 4], radius=2, fill=theme.accent)
                positions = _draw_mixed_text(card_surface, c_draw, content_x + 14, cur_y, wrap_line, font, theme.text_secondary, block.font_size, emoji_remote_eff, emoji_style)
                for px, pw, py, pc in positions:
                    all_char_positions.append((px, pw, py, pc, line_h))
            elif block.block_type == "bullet":
                positions = _draw_mixed_text(card_surface, c_draw, content_x + 4, cur_y, wrap_line, font, theme.text_primary, block.font_size, emoji_remote_eff, emoji_style)
                for px, pw, py, pc in positions:
                    all_char_positions.append((px, pw, py, pc, line_h))
            elif block.block_type.startswith("heading"):
                heading_color = theme.accent if (style == "android16" and block.block_type == "heading1") else theme.text_primary
                positions = _draw_mixed_text(card_surface, c_draw, content_x, cur_y, wrap_line, font, heading_color, block.font_size, emoji_remote_eff, emoji_style)
                for px, pw, py, pc in positions:
                    all_char_positions.append((px, pw, py, pc, line_h))
            else:
                positions = _draw_mixed_text(card_surface, c_draw, content_x, cur_y, wrap_line, font, theme.text_primary, block.font_size, emoji_remote_eff, emoji_style)
                for px, pw, py, pc in positions:
                    all_char_positions.append((px, pw, py, pc, line_h))

            cur_y += line_h

        # 10. 绘制高雅极简 Footer（移动到左下角）
        # cur_y 此时为正文结束位置
        footer_line_y = cur_y + footer_gap
        c_draw.line([(inner_pad_x, footer_line_y), (card_w - inner_pad_x, footer_line_y)], fill=theme.divider, width=1)
        foot_font = get_font(11, bold=False)
        ft_text = "Generated by xbimg"
        c_draw.text((inner_pad_x, footer_line_y + 8), ft_text, font=foot_font, fill=theme.text_muted)

        # 11. 处理违规马赛克（字级精准半打码：支持上/下/随机）
        if mosaic_mode in ("half", "full") and all_char_positions:
            cls._apply_word_level_mosaic(
                card_surface=card_surface,
                all_char_positions=all_char_positions,
                text=text,
                violation_words=violation_words or [],
                mosaic_mode=mosaic_mode,
                mosaic_type=mosaic_type,
                theme=theme,
                card_w=card_w,
                mosaic_half_pos=mosaic_half_pos,
            )

        # 12. 将卡片合成到画布上
        canvas.paste(card_surface, (margin_x, margin_y), card_surface)

        return canvas.convert("RGB")

    @classmethod
    def _apply_word_level_mosaic(
        cls,
        card_surface: Image.Image,
        all_char_positions: List[Tuple[float, float, float, str, int]],
        text: str,
        violation_words: List[str],
        mosaic_mode: str,
        mosaic_type: str,
        theme: "ThemeColors",
        card_w: int,
        mosaic_half_pos: str = "bottom",
    ):
        """
        字级精准半打码（支持上/下/随机）：
        - mosaic_mode="half": 每个违规字只打一半（上或下或随机），另一半保留可读
        - mosaic_mode="full": 每个违规字整个打码
        - 如果没有传入 violation_words 或找不到匹配，则回退到区域打码
        """
        if not violation_words:
            cls._fallback_area_mosaic(card_surface, all_char_positions, mosaic_mode, mosaic_type, theme, card_w, mosaic_half_pos)
            return

        # 将所有字符位置拼成一个字符串，用于关键词定位
        chars_str = "".join(pc[3] for pc in all_char_positions)
        chars_lower = chars_str.lower()

        # 收集所有违规字符的索引（去重）
        violation_char_indices: set = set()

        for kw in violation_words:
            kw_lower = kw.strip().lower()
            if not kw_lower:
                continue
            start = 0
            while True:
                idx = chars_lower.find(kw_lower, start)
                if idx < 0:
                    break
                for ci in range(idx, min(idx + len(kw_lower), len(all_char_positions))):
                    violation_char_indices.add(ci)
                start = idx + 1

        if not violation_char_indices:
            cls._fallback_area_mosaic(card_surface, all_char_positions, mosaic_mode, mosaic_type, theme, card_w, mosaic_half_pos)
            return

        # 对每个违规字符施加马赛克（更精致：柔和像素/磨砂+主题色轻遮罩）
        is_dark = theme.bg_gradient_start[0] < 80  # 简易深浅判断
        # 随机模式用稳定种子，保证同文本同效果但每字不同
        rand = random.Random(_stable_seed(text + "".join(violation_words)) ^ 0x9E3779B9) if mosaic_half_pos == "random" else None
        for ci in sorted(violation_char_indices):
            cx, cw, cy, _ch, clh = all_char_positions[ci]
            left = max(0, int(cx))
            right = min(card_surface.width, int(cx + cw + 1))

            if mosaic_mode == "half":
                # 半打码：按配置选择上/下/随机
                if mosaic_half_pos == "top":
                    is_top = True
                elif mosaic_half_pos == "random":
                    is_top = rand.choice([True, False]) if rand else False  # type: ignore
                else:
                    is_top = False
                mid_y = int(cy + clh * 0.48)
                if is_top:
                    top = max(0, int(cy))
                    bottom = min(card_surface.height, mid_y)
                else:
                    top = max(0, mid_y)
                    bottom = min(card_surface.height, int(cy + clh))
            else:
                # 全打码：整个字
                top = max(0, int(cy))
                bottom = min(card_surface.height, int(cy + clh))

            if right <= left or bottom <= top:
                continue

            region = card_surface.crop((left, top, right, bottom))
            rw, rh = region.size
            if rw < 2 or rh < 2:
                continue

            if mosaic_type == "pixel":
                # 更细腻的像素颗粒：随字高自适应，避免过大块
                pix = max(3, min(rw, rh) // 4)
                pix = min(pix, 8)
                small = region.resize((max(1, rw // pix), max(1, rh // pix)), Image.NEAREST)
                mosaic_region = small.resize((rw, rh), Image.NEAREST)
                # 轻微去饱和 + 主题色柔光，降低刺眼红
                if is_dark:
                    overlay = Image.new("RGBA", mosaic_region.size, (*theme.accent[:3], 22))
                else:
                    overlay = Image.new("RGBA", mosaic_region.size, (120, 115, 110, 26))
                # 叠加细腻噪点质感（1px 棋盘微暗）
                mosaic_region = Image.alpha_composite(mosaic_region.convert("RGBA"), overlay)
            else:
                # 磨砂更通透：加大模糊半径 + 柔白/柔黑半透明遮罩
                mosaic_region = region.filter(ImageFilter.GaussianBlur(radius=7))
                if is_dark:
                    overlay = Image.new("RGBA", mosaic_region.size, (36, 38, 46, 58))
                else:
                    overlay = Image.new("RGBA", mosaic_region.size, (250, 248, 245, 72))
                mosaic_region = Image.alpha_composite(mosaic_region.convert("RGBA"), overlay)
            card_surface.paste(mosaic_region, (left, top), mosaic_region if mosaic_region.mode == "RGBA" else None)

    @classmethod
    def _fallback_area_mosaic(
        cls,
        card_surface: Image.Image,
        all_char_positions: List[Tuple[float, float, float, str, int]],
        mosaic_mode: str,
        mosaic_type: str,
        theme: "ThemeColors",
        card_w: int,
        mosaic_half_pos: str = "bottom",
    ):
        """回退的区域级打码：对内容区域的指定半段施加马赛克（支持上/下/随机）"""
        if not all_char_positions:
            return

        # 计算内容区域的上下边界
        min_y = int(min(p[2] for p in all_char_positions))
        max_y = int(max(p[2] + p[4] for p in all_char_positions))
        content_h = max(20, max_y - min_y)

        if mosaic_mode == "half":
            # 修正为“单个字的一半”描述，区域回退也按半区处理
            if mosaic_half_pos == "top":
                m_top = min_y - 4
                m_bottom = int(min_y + content_h * 0.54)
            elif mosaic_half_pos == "random":
                # 随机选上或下（稳定随机）
                rnd = random.Random(_stable_seed(f"{min_y}{max_y}{card_w}") ^ 0x517CC1B7)
                if rnd.choice([True, False]):
                    m_top = min_y - 4
                    m_bottom = int(min_y + content_h * 0.54)
                else:
                    m_top = int(min_y + content_h * 0.46)
                    m_bottom = max_y + 10
            else:
                m_top = int(min_y + content_h * 0.46)
                m_bottom = max_y + 10
            banner_text = "⚠️ 已对违规词逐字半遮 · 上/下随机打码"
            if mosaic_half_pos == "top":
                banner_text = "⚠️ 已对违规词上半部分打码"
            elif mosaic_half_pos == "bottom":
                banner_text = "⚠️ 已对违规词下半部分打码"
        else:
            m_top = min_y - 4
            m_bottom = max_y + 10
            banner_text = "⛔ 存在违规内容 · 已完全屏蔽处理"

        m_left = 24
        m_right = card_w - 24

        # 安全边界
        m_top = max(0, m_top)
        m_bottom = min(card_surface.height, m_bottom)
        m_left = max(0, m_left)
        m_right = min(card_surface.width, m_right)

        if m_right <= m_left or m_bottom <= m_top:
            return

        region = card_surface.crop((m_left, m_top, m_right, m_bottom))
        rw, rh = region.size

        is_dark = theme.bg_gradient_start[0] < 80
        if mosaic_type == "pixel":
            # 细腻像素：块稍大但不过粗，保留质感
            scale = max(5, min(rw, rh) // 18)
            scale = min(scale, 12)
            small = region.resize((max(1, rw // scale), max(1, rh // scale)), Image.NEAREST)
            mosaic_region = small.resize((rw, rh), Image.NEAREST)
            if is_dark:
                overlay = Image.new("RGBA", mosaic_region.size, (44, 46, 56, 44))
            else:
                overlay = Image.new("RGBA", mosaic_region.size, (128, 122, 116, 30))
            mosaic_region = Image.alpha_composite(mosaic_region.convert("RGBA"), overlay)
        else:
            mosaic_region = region.filter(ImageFilter.GaussianBlur(radius=18))
            if is_dark:
                overlay = Image.new("RGBA", mosaic_region.size, (32, 34, 42, 68))
            else:
                overlay = Image.new("RGBA", mosaic_region.size, (248, 246, 242, 82))
            mosaic_region = Image.alpha_composite(mosaic_region.convert("RGBA"), overlay)
        card_surface.paste(mosaic_region, (m_left, m_top), mosaic_region)

        # 绘制更精致的警示横幅（胶囊 pill，柔和阴影，非刺眼红）
        m_draw = ImageDraw.Draw(card_surface)
        banner_h = 28
        banner_y = max(m_top - 10, 0)
        # 横幅背景：半打码用主题 accent 柔和色，全打码用低饱和暖灰红
        if mosaic_mode == "half":
            if is_dark:
                banner_fill = (*theme.accent[:3], 205)
            else:
                # 浅色下用更优雅的 slate-blue 胶囊
                banner_fill = (78, 98, 132, 215)
            banner_outline = (255, 255, 255, 38)
        else:
            banner_fill = (118, 72, 68, 218) if not is_dark else (92, 58, 56, 218)
            banner_outline = (255, 255, 255, 28)
        # 轻微投影
        m_draw.rounded_rectangle(
            [m_left + 10, banner_y + 2, m_right - 10, banner_y + banner_h + 2],
            radius=banner_h // 2,
            fill=(0, 0, 0, 22),
        )
        m_draw.rounded_rectangle(
            [m_left + 8, banner_y, m_right - 8, banner_y + banner_h],
            radius=banner_h // 2,
            fill=banner_fill,
            outline=banner_outline,
            width=1,
        )
        warn_font = get_font(11, bold=True)
        bw, bh = _text_size(warn_font, banner_text)
        # 文字垂直居中
        tx = m_left + 8 + (m_right - m_left - 16 - bw) / 2
        ty = banner_y + (banner_h - bh) / 2 - 1
        m_draw.text(
            (tx, ty),
            banner_text,
            font=warn_font,
            fill=(255, 255, 255),
        )
