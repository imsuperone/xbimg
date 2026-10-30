# -*- coding: utf-8 -*-
"""渲染引擎 · 字体层：探测/加载/缺字回退/cmap/精选字体与下载（配置生效唯一入口 configure_fonts）。"""
import contextvars
import io
import json
import os
import threading
import time
import unicodedata
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

from . import shared
from .shared import (
    BUNDLED_FONTS_DIR, _BROKEN_FONT_FILES, _CJK_FILE_HINTS, _CJK_PROBE_POINTS,
    _FONT_EXTS, _MOUNT_FONT_BASES, _font_data_dir, _is_bold_font_name,
    _text_size, is_emoji_char,
)
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


def _extra_font_dirs() -> List[str]:
    """用户挂载目录：环境变量 XBIMG_FONT_DIRS（os.pathsep 分隔）+ 常见挂载点"""
    dirs: List[str] = []
    try:
        raw = os.environ.get("XBIMG_FONT_DIRS", "") or ""
        for part in raw.split(os.pathsep):
            part = part.strip()
            if part and part not in dirs:
                dirs.append(part)
    except Exception:
        pass
    for base in _MOUNT_FONT_BASES:
        if base not in dirs:
            dirs.append(base)
    return dirs


def _scan_extra_font_dirs() -> List[str]:
    """扫描挂载目录：文件名命中直接收录，无特征文件名用 cmap 内容校验（各有上限防慢启动）"""
    found: List[str] = []
    checked = 0
    for base in _extra_font_dirs():
        try:
            root = Path(base)
            if not root.is_dir():
                continue
            try:
                files = sorted(p for p in root.rglob("*") if p.suffix.lower() in _FONT_EXTS)
            except Exception:
                continue
            for p in files[:300]:
                try:
                    if not p.is_file():
                        continue
                except Exception:
                    continue
                name_l = p.name.lower()
                if any(h in name_l for h in _CJK_FILE_HINTS):
                    found.append(str(p))
                elif checked < 30:
                    # 无特征文件名：cmap 校验（fontTools 解析，有上限）
                    checked += 1
                    try:
                        if _has_cjk_cmap(str(p)):
                            found.append(str(p))
                    except Exception:
                        pass
                if len(found) >= 40:
                    return found
        except Exception:
            continue
    return found


def _collect_font_candidates(extra_first: Optional[List[str]] = None) -> List[str]:
    """收集全来源有序字体候选：额外优先 → Windows 精选 → 自带 → 用户挂载 → Linux/mac 精确 → fc-list → 全盘扫描"""
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

    # 用户挂载目录（Docker -v /宿主字体:/fonts 等 + XBIMG_FONT_DIRS），意图明确优先于系统兜底
    for p in _scan_extra_font_dirs():
        _add(p)

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


_SYSTEM_CANDIDATES: Optional[List[str]] = None
# 渲染缓存失效代际：任何字体/emoji 配置变更即递增，旧缓存自动作废
_FONT_EPOCH = 0
def _system_font_candidates() -> List[str]:
    """系统字体候选（目录扫描 + fc-list，开销大，进程内缓存；
    数据目录字体仍每次新鲜扫描，不影响下载/删除即时生效）"""
    global _SYSTEM_CANDIDATES
    if _SYSTEM_CANDIDATES is None:
        _SYSTEM_CANDIDATES = _collect_font_candidates()
    return list(_SYSTEM_CANDIDATES)


def _split_reg_bold(ordered: List[str]) -> Tuple[str, str]:
    # 按文件名区分 regular / bold
    regulars = [p for p in ordered if not _is_bold_font_name(os.path.basename(p))]
    bolds = [p for p in ordered if _is_bold_font_name(os.path.basename(p))]
    # msyh.ttc 本身含多字重，单文件时可兼任
    reg = regulars[0] if regulars else (ordered[0] if ordered else "")
    bold = bolds[0] if bolds else reg
    return reg, bold


def _find_fonts() -> Tuple[str, str]:
    """查找系统可用的中文字体，返回 (regular, bold)。

    注意：import 期只做候选收集、不报“缺字体”警告——数据目录/用户挂载在
    configure_fonts 之后才完整，真正的缺字判定在 _rebuild_active_fonts 里做，
    避免用户明明挂了字体却被误报。
    """
    ordered = _system_font_candidates()
    reg, bold = _split_reg_bold(ordered)

    if reg:
        logger.info(f"[msg2img] 中文字体 regular={reg} bold={bold}")
    else:
        logger.debug("[msg2img] 系统目录未发现中文字体，等待数据目录/挂载目录生效")
    return reg, bold


# import 时不再跑 fc-list/目录扫描：首次 configure_fonts/get_font 时懒探测，结果进程内缓存
_FONT_REGULAR_PATH, _FONT_BOLD_PATH = "", ""
# 有序候选全集（get_font 回退时按此顺序尝试）
_CANDIDATE_FONTS: List[str] = []
_FONT_PATHS_INIT = False


def _ensure_font_paths() -> None:
    """懒探测系统字体（替代 import 时的 _find_fonts）；结果缓存，重复调用零开销"""
    global _FONT_REGULAR_PATH, _FONT_BOLD_PATH, _FONT_PATHS_INIT
    if _FONT_PATHS_INIT:
        return
    _FONT_PATHS_INIT = True
    try:
        _FONT_REGULAR_PATH, _FONT_BOLD_PATH = _find_fonts()
        for _p in [_FONT_REGULAR_PATH, _FONT_BOLD_PATH]:
            if _p and _p not in _CANDIDATE_FONTS:
                _CANDIDATE_FONTS.append(_p)
    except Exception:
        pass


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

# 生效中的配置（configure_fonts 写入）：font_source / custom 路径 / emoji
_FONT_CONF: Dict[str, str] = {
    "font_source": "auto",
    "custom_font_path": "",
    "custom_bold_font_path": "",
    "custom_font_url": "",
    "emoji_style": "none",
}
# 生效中的有序候选（configure 后重建，get_font 优先使用）
_ACTIVE_ORDERED: List[str] = []
_ACTIVE_REGULAR = ""
_ACTIVE_BOLD = ""
_EMOJI_STYLE = "none"
# 下载状态（ Def 重复并发）
_FONT_DL_RUNNING = False


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
        if shared._FONT_DATA_DIR is not None:
            cands.append(str(shared._FONT_DATA_DIR / raw))
        cands.append(str(BUNDLED_FONTS_DIR / raw))
    for c in cands:
        if _is_usable_font(c):
            return c
    return ""


# 缺字体警告进程内只打一次（见 _rebuild_active_fonts）
_WARNED_NO_CJK = False


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

    base_system = _system_font_candidates()
    if src == "system":
        ordered = base_system
    else:
        # auto/custom：自定义 + 数据目录（含自动下载/URL 下载）优先于系统
        ordered = []
        for p in list(extra) + data_fonts + base_system:
            if not p or p in ordered:
                continue
            if os.path.basename(p).lower() in _BROKEN_FONT_FILES:
                continue
            if os.path.isfile(p):
                ordered.append(p)

    _ACTIVE_ORDERED = ordered
    _ACTIVE_REGULAR, _ACTIVE_BOLD = _split_reg_bold(ordered)
    # 缺字判定只在这里做一次（进程内）：此时数据目录/挂载目录/自定义已全部就位，
    # 结论准确；字体出现后复位标记，下次再缺才提醒
    global _WARNED_NO_CJK
    try:
        if _ACTIVE_REGULAR and _is_usable_font(_ACTIVE_REGULAR):
            if _WARNED_NO_CJK:
                logger.info(f"[msg2img] 中文字体已就绪：{_ACTIVE_REGULAR}")
            _WARNED_NO_CJK = False
        elif not _WARNED_NO_CJK:
            _WARNED_NO_CJK = True
            logger.warning(
                "[msg2img] 未找到可用中文字体，中文将显示为方框。请按需处理："
                "① WebUI 精选字体一键下载；② Docker 用 -v 挂字体目录到 /fonts 并重启；"
                "③ 设置环境变量 XBIMG_FONT_DIRS 指向字体目录；④ 安装 fonts-noto-cjk / wqy-microhei。"
            )
    except Exception:
        pass
    # 候选集变化后，覆盖判定缓存必须失效：旧字体对象地址会被新对象复用(id 一致)，
    # 否则换字体后仍命中过期结论导致 tofu
    try:
        _COVER_CACHE.clear()
    except Exception:
        pass
    try:
        _RESOLVE_CACHE.clear()
    except Exception:
        pass
    try:
        _INK_CACHE.clear()
    except Exception:
        pass
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
    # 首次配置即触发系统字体懒探测（import 时已不再扫描）
    _ensure_font_paths()
    global _EMOJI_STYLE, _FONT_EPOCH
    try:
        _FONT_EPOCH += 1
    except Exception:
        pass
    if data_dir is not None:
        try:
            shared._FONT_DATA_DIR = Path(data_dir)
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


def needs_cjk_download() -> bool:
    """是否需要自动补字体：auto 模式且当前无可用 CJK，或 custom_url 待下载"""
    src = (_FONT_CONF.get("font_source") or "auto").lower()
    if src == "system":
        return False
    custom_url = (_FONT_CONF.get("custom_font_url") or "").strip()
    _dir = _font_data_dir()
    if custom_url and _dir is not None:
        target = _dir / _safe_font_filename(custom_url)
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


# 同目标并发下载互斥（字典 + 全局守卫，避免两线程写同一 tmp 交错损坏）
_DL_LOCKS: Dict[str, "threading.Lock"] = {}
_DL_LOCKS_GUARD = threading.Lock()


def _download_lock_for(dest: Path):
    try:
        key = str(dest)
        with _DL_LOCKS_GUARD:
            lk = _DL_LOCKS.get(key)
            if lk is None:
                lk = threading.Lock()
                _DL_LOCKS[key] = lk
                if len(_DL_LOCKS) > 64:
                    try:
                        _DL_LOCKS.pop(next(iter(_DL_LOCKS)))
                    except Exception:
                        pass
            return lk
    except Exception:
        return threading.Lock()


def _download_file(url: str, dest: Path) -> Tuple[bool, str]:
    """标准库下载单个文件：超时 + 体积上限 + 断点续传 + 原子落盘。

    抖动链路上大文件（10MB+ 彩字）一次下完不现实：tmp 按 URL 哈希固定命名，
    中断后下次从断点 Range 续传；服务器不支持 Range 则从头重下。
    同目标并发由锁串行化；成功后清理同目标其他 URL 的残留分片。
    """
    import hashlib as _hl
    from urllib.parse import urlsplit as _urlsplit

    # SSRF 护栏：只放行 http/https——file:// 能读本地文件，ftp/data 等亦一律拒绝。
    # custom_font_url 是本函数唯一的用户可配来源，其余调用方均为内置固定链接
    try:
        _scheme = _urlsplit(str(url or "")).scheme.lower()
    except Exception:
        _scheme = ""
    if _scheme not in ("http", "https"):
        return False, f"不支持的链接协议「{_scheme or '空'}」，仅允许 http/https"

    try:
        url_hash = _hl.sha1(str(url or "").encode("utf-8", "ignore")).hexdigest()[:8]
    except Exception:
        url_hash = "x"
    tmp = dest.with_name(f"{dest.name}.{url_hash}.downloading")
    lock = _download_lock_for(dest)
    try:
        with lock:
            return _download_file_locked(url, dest, tmp)
    except Exception as e:
        return False, str(e)


def _download_file_locked(url: str, dest: Path, tmp: Path) -> Tuple[bool, str]:
    try:
        import urllib.request
        start = 0
        try:
            if tmp.is_file():
                start = max(0, int(tmp.stat().st_size))
        except Exception:
            start = 0
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }
        if start > 0:
            headers["Range"] = f"bytes={start}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=_FONT_DL_TIMEOUT)
        except Exception as e:
            # 建连/握手失败：保留分片供下次续传（不删 tmp）
            return False, str(e)
        with resp:
            try:
                code = getattr(resp, "status", None) or resp.getcode()
            except Exception:
                code = 200
            if code == 416:
                # 分片已超长/服务端无此范围：删分片，下次从头来
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                return False, "服务器拒绝断点续传，已清理请重试"
            if code not in (200, 206):
                return False, f"HTTP {code}"
            if code == 200 and start > 0:
                # 服务端无视 Range：分片内容不可续，截断重下
                start = 0
            total = start
            try:
                with open(tmp, "ab" if (code == 206 and start > 0) else "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _FONT_DL_MAX_BYTES:
                            raise ValueError("文件超过体积上限，已中止")
                        f.write(chunk)
            except Exception as e:
                # 传输中断：保留分片供续传
                return False, str(e)
        if total < 1024:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
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
        # 成功后清理同目标其他 URL 的残留分片
        try:
            for p in dest.parent.glob(f"{dest.name}.*.downloading"):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass
        return True, ""
    except Exception as e:
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
    if shared._FONT_DATA_DIR and shared._FONT_DATA_DIR.is_dir() and shared._FONT_DATA_DIR not in search_dirs:
        search_dirs.append(shared._FONT_DATA_DIR)

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
            # 大包直链易波动：单文件最多重试 3 次
            ok, err = False, ""
            for attempt in range(3):
                ok, err = _download_file(url, target)
                if ok and _is_usable_font(str(target)):
                    break
                ok = False
                try:
                    time.sleep(1.5 * (attempt + 1))
                except Exception:
                    pass
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
# 字体字节缓存：键带 (mtime, size)，文件被下载覆盖后旧字节自动失效；
# 上限 6 条（单文件可达数十 MB），防常驻膨胀
_FONT_BYTES_CACHE: Dict[Tuple, bytes] = {}
_FONT_BYTES_CACHE_LIMIT = 6
# 单群字体覆盖：(常规路径, 粗体路径)，按次渲染设置，线程/协程安全（ContextVar）。
# 为空/None 时走全局生效集；缺字仍由 _resolve_char_font 逐字回退补齐。
_GROUP_FONT_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar(
    "xbimg_group_font_override", default=None
)


def resolve_group_font_override(custom_font_path: str = "", custom_bold_font_path: str = "") -> Tuple[str, str]:
    """解析单群字体覆盖：校验可用性，返回 (常规, 粗体) 绝对路径；不可用返回 ("", "")。"""
    try:
        reg = _resolve_custom_font(custom_font_path or "")
        bold = _resolve_custom_font(custom_bold_font_path or "") or reg
        return reg, bold
    except Exception:
        return "", ""
def _load_font_file(path: str, size: int) -> Optional[ImageFont.FreeTypeFont]:
    """加载单个字体文件；使用内存 BytesIO，避免 Windows 下文件句柄常驻锁定导致无法删除"""
    try:
        if not path or not os.path.isfile(path):
            return None
        try:
            _st = os.stat(path)
            _bkey = (path, _st.st_mtime_ns, _st.st_size)
        except Exception:
            return None
        data = _FONT_BYTES_CACHE.get(_bkey)
        if data is None:
            try:
                with open(path, "rb") as fp:
                    data = fp.read()
                # 仅对小于 60MB 的字体文件缓存内存字节，避免过多消耗内存
                if len(data) <= 60 * 1024 * 1024:
                    try:
                        while len(_FONT_BYTES_CACHE) >= _FONT_BYTES_CACHE_LIMIT:
                            _FONT_BYTES_CACHE.pop(next(iter(_FONT_BYTES_CACHE)))
                    except Exception:
                        pass
                    _FONT_BYTES_CACHE[_bkey] = data
            except Exception:
                return None
        if not data:
            return None
        if path.lower().endswith((".ttc", ".otc")):
            for idx in range(4):
                try:
                    f = ImageFont.truetype(io.BytesIO(data), size, index=idx)
                    try:
                        f._src_path = path
                    except Exception:
                        pass
                    return f
                except Exception:
                    continue
            return None
        f = ImageFont.truetype(io.BytesIO(data), size)
        try:
            f._src_path = path
        except Exception:
            pass
        return f
    except Exception:
        return None


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """获取 CJK 字体。优先级：单群覆盖（ContextVar）→ 配置生效集（自定义/下载/系统）→ 懒探测结果（首次使用时扫描并缓存）。"""
    # 懒探测兜底：未走 configure_fonts 即直接渲染时（如单测），此处补齐系统字体候选
    _ensure_font_paths()
    try:
        _ov = _GROUP_FONT_OVERRIDE.get()
    except Exception:
        _ov = None
    if _ov:
        _ov_reg, _ov_bold = _ov
        _ov_path = _ov_bold if bold else _ov_reg
        if _ov_path:
            try:
                _ov_key = (size, bold, _ov_path)
                hit = _FONT_CACHE.get(_ov_key)
                if hit is not None:
                    return hit
                f = _load_font_file(_ov_path, size)
                if f is not None and _font_covers(f, "永"):
                    _FONT_CACHE[_ov_key] = f
                    return f
            except Exception:
                pass
        # 覆盖字体不可用/缺 CJK：回退全局链（缺字仍由 _resolve_char_font 逐字回退）
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
    """获取单个字符的推进宽度（advance width），使用 getlength 而非 getbbox；结果按字体缓存"""
    try:
        key = (_font_key(font), getattr(font, "size", 0), char)
    except Exception:
        key = None
    if key is not None:
        try:
            hit = _ADV_CACHE.get(key)
            if hit is not None:
                return hit
        except Exception:
            pass
    try:
        w = float(font.getlength(char))
    except Exception:
        # 极旧 Pillow 回退
        try:
            bbox = font.getbbox(char)
            w = float(bbox[2] - bbox[0]) if bbox else 14.0
        except Exception:
            w = 14.0
    if key is not None:
        _cache_put(_ADV_CACHE, key, w, 20000)
    return w


# 无墨字符（空格/控制符/连接符等）永远跟随主字体，不参与回退
_BLANK_PASSTHROUGH = frozenset(["\u200b", "\u200d", "\ufe0f", "\u00ad"])
_COVER_CACHE: Dict[Tuple, bool] = {}
_RESOLVE_CACHE: Dict[Tuple, Any] = {}
_CMAP_CACHE: Dict[str, Optional[frozenset]] = {}
_CMAP_PARSE_LOCK = threading.Lock()
_UNCOVERED_LOG_AT: Dict[str, float] = {}
_ADV_CACHE: Dict[Tuple, float] = {}


def _cache_put(cache: Dict, key, value, limit: int):
    """有界缓存写入：超限淘汰最早插入项（dict 保序），避免全清导致的缓存踩踏与突发回暖"""
    try:
        if key in cache:
            cache[key] = value
            return
        if len(cache) >= limit:
            cache.pop(next(iter(cache)))
        cache[key] = value
    except Exception:
        pass


def _font_key(font):
    """字体缓存键：优先用加载来源路径（跨重载稳定），否则退化到 id"""
    try:
        sp = getattr(font, "_src_path", None)
        if isinstance(sp, str) and sp:
            return sp
        fp = getattr(font, "path", None)
        if isinstance(fp, (str, bytes)):
            return fp
    except Exception:
        pass
    try:
        return id(font)
    except Exception:
        return 0


def _mask_bytes(core) -> bytes:
    """ImagingCore 位图转字节（Pillow 12 的 getmask 返回无 tobytes 的 core 对象）"""
    try:
        c = Image.new("L", core.size, 0)
        c.im.paste(core, (0, 0) + core.size)
        return c.tobytes()
    except Exception:
        return b""


def _font_covers(font: ImageFont.FreeTypeFont, ch: str) -> bool:
    """判定字体是否真含该字符字形（而非豆腐块占位）。

    原理：缺字时 FreeType 渲染 .notdef 占位块；与 U+FFFF（必缺字，渲染结果即该字体的
    .notdef 长相）逐像素比对可精准识别。误判方向是安全的：把有字判成缺字只会多用
    回退字体渲染（字形依然正确），不会制造新的豆腐块。
    """
    if not ch or ch.isspace() or ord(ch) < 0x20 or ch in _BLANK_PASSTHROUGH:
        return True
    try:
        key = (_font_key(font), getattr(font, "size", 0), ch)
    except Exception:
        return True
    hit = _COVER_CACHE.get(key)
    if hit is not None:
        return hit
    ok = True
    try:
        ref = font.getmask("\uffff")
        m = font.getmask(ch)
        if m.getbbox() is None:
            ok = False
        elif ref.getbbox() is None:
            ok = True  # 该字体没有 .notdef，有墨即视为覆盖
        elif m.size != ref.size:
            ok = True  # 外框不同必为不同字形
        else:
            ok = _mask_bytes(m) != _mask_bytes(ref)
        if ok:
            # 启发式通过后再用 cmap 复核（有 fonttools 时）：文件级缺字则必缺
            try:
                sp = getattr(font, "_src_path", None)
                if isinstance(sp, str) and sp:
                    cmap = _file_cmap(sp)
                    if cmap is not None and ord(ch) not in cmap:
                        ok = False
            except Exception:
                pass
    except Exception:
        ok = True
    _cache_put(_COVER_CACHE, key, ok, 6000)
    return ok


def _fallback_font_paths() -> List[str]:
    """回退候选字体路径（已按优先级排序，去重）"""
    seen = set()
    out: List[str] = []
    for cand in list(_ACTIVE_ORDERED) + [p for p in _CANDIDATE_FONTS if p]:
        try:
            if cand and cand not in seen:
                seen.add(cand)
                out.append(cand)
        except Exception:
            continue
    return out[:16]


def _resolve_char_font(ch: str, primary: ImageFont.FreeTypeFont) -> ImageFont.FreeTypeFont:
    """单字符字体解析：主字体有字就用主字体；缺字则按链回退到首个有字的字体。

    解决用户自定义装饰字体（如哥特体）缺 CJK/符号字形导致的满屏豆腐块：
    拉丁字母仍用装饰字体渲染保持风格，中文与符号自动用链中字体补齐。
    """
    if not ch or ch.isspace() or ord(ch) < 0x20 or ch in _BLANK_PASSTHROUGH:
        return primary
    try:
        key = (_font_key(primary), getattr(primary, "size", 0), ch)
    except Exception:
        return primary
    hit = _RESOLVE_CACHE.get(key)
    if hit is not None:
        return hit
    res = primary
    try:
        if not _font_covers(primary, ch):
            size = getattr(primary, "size", 26) or 26
            for cand in _fallback_font_paths():
                try:
                    f = _load_font_file(cand, size)
                except Exception:
                    continue
                if f is not None and _font_covers(f, ch):
                    res = f
                    break
    except Exception:
        res = primary
    _cache_put(_RESOLVE_CACHE, key, res, 8000)
    return res


_NFKC_MISSING = object()
_NFKC_CACHE: Dict[str, Any] = {}


def _nfc_fallback_char(ch: str, primary: ImageFont.FreeTypeFont) -> Optional[str]:
    """数学花体/全角等特殊符号无字形时，退化为 NFKC 等价单字符（如 𝔖→S、Ｎ→N、①→1）。

    等价字符优先用主字体绘制（风格统一，如哥特体 S），主字体也没有则链中字体兜底。
    无等价形或等价形依然无字形时返回 None（保留原文，不乱改）。
    """
    if not ch or len(ch) != 1:
        return None
    try:
        hit = _NFKC_CACHE.get(ch, _NFKC_MISSING)
    except Exception:
        hit = _NFKC_MISSING
    if hit is not _NFKC_MISSING:
        return hit
    res = None
    try:
        norm = unicodedata.normalize("NFKC", ch)
        if norm and len(norm) == 1 and norm != ch:
            if _font_covers(primary, norm):
                res = norm
            else:
                size = getattr(primary, "size", 26) or 26
                for cand in _fallback_font_paths():
                    try:
                        f = _load_font_file(cand, size)
                    except Exception:
                        continue
                    if f is not None and _font_covers(f, norm):
                        res = norm
                        break
    except Exception:
        res = None
    _cache_put(_NFKC_CACHE, ch, res, 2000)
    return res


def _draw_char_and_font(ch: str, primary: ImageFont.FreeTypeFont) -> Tuple[str, ImageFont.FreeTypeFont]:
    """返回 (实际绘制字符, 字体)：缺字先按链回退字体；链中全无则 NFKC 等价形兜底；
    仍无解才保留原字（tofu），并记一条诊断日志方便定位缺字库。"""
    try:
        f = _resolve_char_font(ch, primary)
    except Exception:
        return ch, primary
    try:
        if f is not primary or len(ch) != 1:
            return ch, f
        if _font_covers(primary, ch):
            return ch, primary
        alt = _nfc_fallback_char(ch, primary)
        if alt is not None:
            try:
                return alt, _resolve_char_font(alt, primary)
            except Exception:
                return alt, primary
        _note_uncovered(primary, ch)
        return ch, primary
    except Exception:
        return ch, primary


_CMAP_DISK: Optional[Dict] = None


def _cmap_disk_load() -> Dict:
    """cmap 磁盘缓存（data 目录 cmap_cache.json），TTFont 全量解析只付一次"""
    global _CMAP_DISK
    if _CMAP_DISK is not None:
        return _CMAP_DISK
    _CMAP_DISK = {}
    try:
        base = shared._FONT_DATA_DIR
        if base is not None:
            p = Path(base) / "cmap_cache.json"
            if p.is_file() and p.stat().st_size < 8 * 1024 * 1024:
                _CMAP_DISK = json.loads(p.read_text(encoding="utf-8")).get("fonts", {}) or {}
    except Exception:
        _CMAP_DISK = {}
    return _CMAP_DISK


def _cmap_disk_save():
    try:
        base = shared._FONT_DATA_DIR
        if base is None:
            return
        p = Path(base) / "cmap_cache.json"
        p.write_text(json.dumps({"fonts": _CMAP_DISK}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _file_cmap(path: str) -> Optional[frozenset]:
    """文件级 cmap 码位集合（需 fonttools；多字重取并集）。不可用返回 None（仅用启发式）。
    磁盘缓存命中时免 TTFont 全量解析（msyh 级别约省 1 秒冷启动）。
    内存键带 (mtime, size)：文件被下载覆盖后旧结论自动失效，不会沿用残包结论。
    锁内 double-check：预热线程与首条消息并发时只解析一次 TTFont。
    """
    try:
        st0 = os.stat(path)
        ckey = (path, st0.st_mtime, st0.st_size)
    except Exception:
        ckey = None
    try:
        hit = _CMAP_CACHE.get(ckey) if ckey is not None else None
    except Exception:
        hit = None
    if hit is not None:
        return hit
    # miss 路径（磁盘读/TTFont）持锁，防并发重复解析
    with _CMAP_PARSE_LOCK:
        try:
            hit = _CMAP_CACHE.get(ckey) if ckey is not None else None
        except Exception:
            hit = None
        if hit is not None:
            return hit
        return _file_cmap_locked(path, ckey)


def _file_cmap_locked(path: str, ckey) -> Optional[frozenset]:
    res = None
    try:
        if path and os.path.isfile(path):
            st = os.stat(path)
            disk = _cmap_disk_load()
            entry = disk.get(path) if isinstance(disk, dict) else None
            if (isinstance(entry, dict) and entry.get("mtime") == st.st_mtime
                    and entry.get("size") == st.st_size
                    and isinstance(entry.get("codes"), list)):
                res = frozenset(entry["codes"])
    except Exception:
        res = None
    if res is not None:
        if ckey is not None:
            _cache_put(_CMAP_CACHE, ckey, res, 64)
        return res
    try:
        if _TTFONT is not None and path and os.path.isfile(path):
            try:
                with open(path, "rb") as fp:
                    data = fp.read()
            except Exception:
                data = b""
            if data:
                codes: set = set()
                ok_faces = 0
                for i in range(4):
                    try:
                        ff = _TTFONT(io.BytesIO(data), fontNumber=i, lazy=True)
                    except Exception:
                        break
                    try:
                        codes |= set(ff.getBestCmap().keys())
                        ok_faces += 1
                    except Exception:
                        pass
                    finally:
                        try:
                            ff.close()
                        except Exception:
                            pass
                # 健全性门槛：正常字体至少数百码位，否则视为不可信
                if ok_faces and len(codes) > 100:
                    res = frozenset(codes)
    except Exception:
        res = None
    if ckey is not None:
        _cache_put(_CMAP_CACHE, ckey, res, 64)
    if res is not None:
        try:
            st = os.stat(path)
            disk = _cmap_disk_load()
            if isinstance(disk, dict):
                disk[path] = {"mtime": st.st_mtime, "size": st.st_size,
                              "codes": sorted(res)}
                _cmap_disk_save()
        except Exception:
            pass
    return res


def _has_cjk_cmap(path: str) -> bool:
    """内容校验：cmap 是否含常用 CJK 码位（文件名无特征的挂载字体靠它认出）"""
    try:
        cmap = _file_cmap(path)
        if not cmap:
            return False
        return any(cp in cmap for cp in _CJK_PROBE_POINTS)
    except Exception:
        return False


def _note_uncovered(font, ch: str):
    """链中无任何字体含该字符：限频打日志，帮助定位是缺字库还是真生僻字。
    Emoji 走专属全彩管线，不在此提示，避免噪音。"""
    try:
        if is_emoji_char(ch):
            return
        sp = getattr(font, "_src_path", None) or getattr(font, "path", None) or "当前字体"
        if not isinstance(sp, str):
            sp = "当前字体"
        now = time.time()
        last = _UNCOVERED_LOG_AT.get(sp, 0)
        if now - last < 300:
            return
        _UNCOVERED_LOG_AT[sp] = now
        logger.warning(f"[msg2img] 字体缺字形（将显示为方框）：{sp} 缺 {ch!r}，链中字体均无该字；请补充含该字符的常规字体")
    except Exception:
        pass


def _adv_text(font: ImageFont.FreeTypeFont, ch: str) -> float:
    """正文字符推进宽度（含缺字回退与 NFKC 兜底，与实际绘制一致）"""
    try:
        dc, f = _draw_char_and_font(ch, font)
        return _char_advance(f, dc)
    except Exception:
        return _char_advance(font, ch)


def _text_advance(font: ImageFont.FreeTypeFont, s: str) -> float:
    """整串推进宽度：整串 memo（同字体同串直接命中）→ 逐字缓存 advance 求和
    （PIL 整串 getbbox/getlength 在长串上极慢），缺字时逐字回退计量，与混排绘制一致"""
    try:
        tkey = (_font_key(font), getattr(font, "size", 0), s)
    except Exception:
        tkey = None
    if tkey is not None:
        try:
            hit = _TEXT_ADV_CACHE.get(tkey)
            if hit is not None:
                return hit
        except Exception:
            pass
    try:
        total = 0.0
        for c in s:
            if c.isspace() or ord(c) < 0x20 or c in _BLANK_PASSTHROUGH:
                total += _char_advance(font, c)
                continue
            if not _font_covers(font, c):
                total = float(sum(_adv_text(font, c2) for c2 in s))
                break
            total += _char_advance(font, c)
        if tkey is not None:
            _cache_put(_TEXT_ADV_CACHE, tkey, total, 8192)
        return total
    except Exception:
        try:
            return _text_size(font, s)[0]
        except Exception:
            return 0.0


_TEXT_ADV_CACHE: Dict[Tuple, float] = {}


_INK_CACHE: Dict[Tuple, float] = {}


def _cjk_ink_center_y(font: ImageFont.FreeTypeFont) -> Optional[float]:
    """CJK 代表字墨迹垂直中心（相对行顶 y），全彩 emoji 以此为中心对齐正文。

    用实际绘制中文的字体（经缺字回退解析）量取，装饰字体做主字体时依然对得准。
    """
    try:
        base = _resolve_char_font("永", font)
        key = (_font_key(base), getattr(base, "size", 0))
    except Exception:
        return None
    hit = _INK_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        bbox = base.getbbox("永")
        if not bbox:
            return None
        c = (bbox[1] + bbox[3]) / 2.0
    except Exception:
        return None
    if len(_INK_CACHE) > 200:
        _INK_CACHE.clear()
    _INK_CACHE[key] = c
    return c


def clear_font_cache():
    """清除字体缓存（删除前调用，释放内存缓存）。

    注意：不要在这里 gc.collect()——字体经 BytesIO 加载，无常驻文件句柄；
    全量 GC 在渲染图片堆积的进程里可卡数秒，且会暂停整个事件循环。
    引用计数即时回收已足够，循环垃圾交给解释器自动 GC。

    渲染/正文段缓存一并失效：键里含字体度量，删字体后不清会继续用旧字形绘制。
    跨模块缓存在函数内延迟导入：fonts 处于包内底层，顶层互导会成环。
    """
    from .emoji import _EMOJI_FONT_CACHE, _EMOJI_IMG_CACHE, _EMOJI_MISS_CACHE
    from .painter import _TEXT_RUN_CACHE
    from .engine import _RENDER_CACHE, _LAYOUT_CACHE

    _FONT_CACHE.clear()
    _EMOJI_FONT_CACHE.clear()
    _FONT_BYTES_CACHE.clear()
    _EMOJI_IMG_CACHE.clear()
    try:
        _RENDER_CACHE.clear()
    except Exception:
        pass
    try:
        _TEXT_RUN_CACHE.clear()
    except Exception:
        pass
    try:
        _EMOJI_MISS_CACHE.clear()
    except Exception:
        pass
    try:
        _LAYOUT_CACHE.clear()
    except Exception:
        pass
    try:
        _TEXT_ADV_CACHE.clear()
    except Exception:
        pass
    try:
        _COVER_CACHE.clear()
    except Exception:
        pass
    try:
        _RESOLVE_CACHE.clear()
    except Exception:
        pass
    try:
        _INK_CACHE.clear()
    except Exception:
        pass
    try:
        _ADV_CACHE.clear()
    except Exception:
        pass
