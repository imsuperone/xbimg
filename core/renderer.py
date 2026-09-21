# -*- coding: utf-8 -*-
"""
高颜值消息转图片渲染引擎 (Text-to-Image Renderer) - V3 精准修复版
- 彻底修复中文/Emoji 乱码：统一 font.getlength() 推进宽度、批量文本段绘制
- 统一字体优先级：SourceHanSans → msyh 回退，杜绝宽度计算不一致
- Footer 署名左下角
- 字级精准半打码：仅对违规关键词的后半部分施加马赛克
- 全量 Numpy 矩阵加速渐变背景渲染（<5ms）
- 字体管理器：自定义字体 / 缺字自动下载到持久化目录 / Emoji 全自动管线
- Emoji 全自动：复杂序列全彩图，单个 emoji 优先 Noto 全彩字体，缺失自动云端补全
"""

import hashlib
import contextvars
import io
import json
import math
import os
import random
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import numpy as _np
except Exception:
    _np = None

try:
    from fontTools.ttLib import TTFont as _TTFONT
except Exception:
    _TTFONT = None

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
# 注意：仅做快速初筛；挂载目录 additionally 用 cmap 内容校验，不依赖文件名
_CJK_FILE_HINTS = (
    "wqy", "noto", "cjk", "yahei", "pingfang", "sourcehan", "hiragino",
    "simsun", "simhei", "simkai", "fandol", "uming", "ukai",
    "droidsansfallback", "fzsong", "fzhei", "stheiti", "heiti",
    "songti", "kaiti", "lantinghei", "arial unicode",
    "han", "wenquanyi", "lxgw", "zcool", "mashan", "sarasa",
)
_FONT_EXTS = (".ttf", ".ttc", ".otf", ".dfont")
# Docker 常见字体挂载点（用户 -v 挂进来的字库一般在这里；另支持环境变量追加）
_MOUNT_FONT_BASES = ("/fonts", "/app/fonts", "/AstrBot/fonts", "/data/fonts")
# cmap 内容校验抽查点（常用汉字，命中任一即视为含 CJK）
_CJK_PROBE_POINTS = (0x4E2D, 0x6587, 0x963F, 0x4E00, 0x9FFF)
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


def _has_cjk_cmap(path: str) -> bool:
    """内容校验：cmap 是否含常用 CJK 码位（文件名无特征的挂载字体靠它认出）"""
    try:
        _fc = globals().get("_file_cmap")
        if not callable(_fc):
            return False  # import 期 _file_cmap 尚未定义，降级为文件名判定
        cmap = _fc(path)
        if not cmap:
            return False
        return any(cp in cmap for cp in _CJK_PROBE_POINTS)
    except Exception:
        return False


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
# 相同文本渲染结果缓存（主链路与适配器钩子常对同一文本各渲染一次，命中直接复用）
_RENDER_CACHE: Dict[Tuple, List[Image.Image]] = {}
_RENDER_CACHE_LIMIT = 12

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
    global _FONT_DATA_DIR, _EMOJI_STYLE, _FONT_EPOCH
    try:
        _FONT_EPOCH += 1
    except Exception:
        pass
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
            _dd = _data_subdir("emoji")
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
        d = _data_subdir("emoji")
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
        d = _data_subdir("emoji")
        if d is not None:
            p = d / fname
            if p.is_file() and p.stat().st_size > _EMOJI_MIN_FONT_BYTES:
                return str(p)
    except Exception:
        pass
    return ""


# 样式彩字有效性结论缓存：style -> ((mtime, size), ok)，文件变化自动重验
_STYLE_FONT_VALID: Dict[str, Any] = {}


def _style_font_validated(style: str) -> str:
    """已下载字体的有效路径：存在 + 体积达标 + 真实可加载（含代表字形）。

    只看体积会把超时截断的残包误判为“已下载”（能过 1MB 门槛但 PIL 打不开），
    导致绘制每次回退网络。结论按 (mtime, size) 缓存，文件替换自动重验。
    """
    try:
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
        if f is not None:
            if str(style or "").lower() == "android":
                ok = bool(_font_covers(f, "\U0001f600"))
            else:
                # windows 包格式复杂，只要求可加载，不强求彩字形覆盖
                ok = True
    except Exception:
        ok = False
    try:
        if len(_STYLE_FONT_VALID) > 8:
            _STYLE_FONT_VALID.pop(next(iter(_STYLE_FONT_VALID)))
        _STYLE_FONT_VALID[style] = (key, ok)
    except Exception:
        pass
    return path if ok else ""


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
    """单字 emoji 是否可走本地彩字零网络绘制。

    仅 android：Noto CBDT 全彩字经 PIL embedded_color 绘制可靠；且 get_emoji_font
    本来就优先该文件。windows 彩字在 PIL 下未必真彩，仍走 PNG 优先保画质；
    ios 无字体文件，只能 PNG。复杂簇（ZWJ/键帽）不受影响，仍走全彩图。
    """
    try:
        if str(emoji_style or "").lower() != "android":
            return False
        return bool(_style_font_path("android"))
    except Exception:
        return False



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


def download_emoji_pack(style: str) -> Dict[str, Any]:
    """下载指定 Emoji 样式资源"""
    style = (style or "").lower()
    if style not in ("ios", "android", "windows"):
        return {"ok": False, "error": "未知样式"}
    d = _data_subdir("emoji")
    if d is None:
        return {"ok": False, "error": "持久化目录不可用"}

    if style == "ios":
        # 常用基础包（首屏/高频表情，一次下好；包外表情仍按需自动补全并缓存）
        base_emojis = [
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
        # 缺失项并行补齐（单文件 urllib 无复用，并行大幅缩短总耗时；失败静默按需再补）
        missing = [c for c in base_emojis if not (d / f"{c}.png").is_file()]
        pre_have = len(base_emojis) - len(missing)
        if missing:
            try:
                import concurrent.futures as _cfut
                with _cfut.ThreadPoolExecutor(max_workers=min(6, len(missing))) as _ex:
                    list(_ex.map(lambda _c: _fetch_remote_emoji(_c, d), missing))
            except Exception:
                pass
        have = sum(1 for code in base_emojis if (d / f"{code}.png").is_file())
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
        return {"ok": True, "downloaded": [f"iOS基础Emoji({have}/{len(base_emojis)}个常用{extra}，其余按需自动补全)"], "storage_kb": get_emoji_storage_kb("ios")}

    # android 或 windows（多直链容错，依次尝试；下载后校验体积，残包换源重试）
    fname = ANDROID_EMOJI_FILE if style == "android" else WINDOWS_EMOJI_FILE
    if style == "android":
        furls = list(ANDROID_EMOJI_URLS)
    else:
        furls = [WINDOWS_EMOJI_URL]
    target = d / fname
    ok, err = False, ""
    for furl in furls:
        ok, err = _download_file(furl, target)
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
        _style_font_forget("" if style == "all" else style)
        _emoji_storage_invalidate()
        return {"ok": True, "deleted": deleted, "storage_kb": get_emoji_storage_kb()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def clear_font_cache():
    """清除字体缓存（删除前调用，释放内存缓存）。

    注意：不要在这里 gc.collect()——字体经 BytesIO 加载，无常驻文件句柄；
    全量 GC 在渲染图片堆积的进程里可卡数秒，且会暂停整个事件循环。
    引用计数即时回收已足够，循环垃圾交给解释器自动 GC。
    """
    _FONT_CACHE.clear()
    _EMOJI_FONT_CACHE.clear()
    _FONT_BYTES_CACHE.clear()
    _EMOJI_IMG_CACHE.clear()
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
_EMOJI_FONT_CACHE: Dict[int, Tuple[Optional[ImageFont.FreeTypeFont], bool]] = {}
# 字体字节缓存：键带 (mtime, size)，文件被下载覆盖后旧字节自动失效；
# 上限 6 条（单文件可达数十 MB），防常驻膨胀
_FONT_BYTES_CACHE: Dict[Tuple, bytes] = {}
_FONT_BYTES_CACHE_LIMIT = 6
# 打码去混淆规则：与 core/moderation._CONDENSE_RE 同构（审查命中、绘制定位双边对齐）
_MOSAIC_CONDENSE_RE = re.compile(r"[\s\-_~`!@#$%^&*()+=|\\\[\]{};:'\",.<>?/]+")
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
    """获取 CJK 字体。优先级：单群覆盖（ContextVar）→ 配置生效集（自定义/下载/系统）→ import 时探测结果。"""
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
        base = _FONT_DATA_DIR
        if base is not None:
            p = Path(base) / "cmap_cache.json"
            if p.is_file() and p.stat().st_size < 8 * 1024 * 1024:
                _CMAP_DISK = json.loads(p.read_text(encoding="utf-8")).get("fonts", {}) or {}
    except Exception:
        _CMAP_DISK = {}
    return _CMAP_DISK


def _cmap_disk_save():
    try:
        base = _FONT_DATA_DIR
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
    """整串推进宽度：逐字缓存 advance 求和（PIL 整串 getbbox/getlength 在长串上极慢），
    缺字时逐字回退计量，与混排绘制一致"""
    try:
        total = 0.0
        for c in s:
            if c.isspace() or ord(c) < 0x20 or c in _BLANK_PASSTHROUGH:
                total += _char_advance(font, c)
                continue
            if not _font_covers(font, c):
                return float(sum(_adv_text(font, c) for c in s))
            total += _char_advance(font, c)
        return total
    except Exception:
        try:
            return _text_size(font, s)[0]
        except Exception:
            return 0.0


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
# 注意：上游 noto-emoji 仓库已改为源码仓，旧 main 直链永久 404；
# 改用 commit 锁定的 unicode16 版本（不可变，永久有效），jsdelivr 主 + raw 备用
ANDROID_EMOJI_FILE = "NotoColorEmoji.ttf"
_NOTO_PIN_SHA = "124c7d40bb51bc026b5407dfffd34e1e6843e1a8"
ANDROID_EMOJI_URLS = [
    f"https://cdn.jsdelivr.net/gh/googlefonts/noto-emoji@{_NOTO_PIN_SHA}/fonts/NotoColorEmoji.ttf",
    f"https://raw.githubusercontent.com/googlefonts/noto-emoji/{_NOTO_PIN_SHA}/fonts/NotoColorEmoji.ttf",
]
ANDROID_EMOJI_URL = ANDROID_EMOJI_URLS[0]
# EmojiOne/Twemoji 全彩字体（跨平台标准矢量风格）
WINDOWS_EMOJI_FILE = "EmojiOneColor.otf"
WINDOWS_EMOJI_URL = "https://raw.githubusercontent.com/adobe-fonts/emojione-color/master/EmojiOneColor.otf"
_EMOJI_MIN_FONT_BYTES = 1 * 1024 * 1024
# 历史遗留别名（外部可能仍在 import，保留只读兼容）
NOTO_EMOJI_FILE = ANDROID_EMOJI_FILE
NOTO_EMOJI_URL = ANDROID_EMOJI_URL
_NOTO_EMOJI_MIN_BYTES = _EMOJI_MIN_FONT_BYTES

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
                from .config import resolve_data_dir
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
    # 国旗 Regional Indicator 必须成对（单个 RI 无图无字必丢）
    if 0x1F1E6 <= ord(ch) <= 0x1F1FA and j < n and 0x1F1E6 <= ord(text[j]) <= 0x1F1FA:
        return text[i:j + 1], j + 1
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


def _emoji_http_client():
    """共享 httpx 同步客户端（连接复用 keep-alive，多 emoji 摊薄 TLS 握手）。

    线程安全（httpx 同步 Client 支持多线程），缺 httpx 时返回 None 走 urllib 兜底。
    """
    global _EMOJI_HTTP_CLIENT
    try:
        if _EMOJI_HTTP_CLIENT is None:
            import httpx as _hx
            _EMOJI_HTTP_CLIENT = _hx.Client(
                timeout=8.0, follow_redirects=True,
                headers={"User-Agent": "astrbot-msg2img/1.3"},
            )
        return _EMOJI_HTTP_CLIENT
    except Exception:
        return None


_EMOJI_HTTP_CLIENT = None


def _fetch_remote_emoji_urllib(code: str, dest_dir: Path, tmp: Path) -> bool:
    """urllib 兜底通道（无 httpx 环境），与旧行为一致"""
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
        # tmp 唯一命名：同 emoji 并发预取互不覆盖，失败只删自己的
        try:
            _uniq = f"{os.getpid()}_{threading.get_ident()}"
        except Exception:
            _uniq = str(os.getpid())
        tmp = dest_dir / f"{code}.{_uniq}.png.downloading"
        client = _emoji_http_client()
        if client is None:
            return _fetch_remote_emoji_urllib(code, dest_dir, tmp)
        try:
            with client.stream("GET", f"{_TWEMOJI_BASE}/{code}.png") as resp:
                if resp.status_code == 404:
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
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
        except ValueError:
            raise
        except Exception as e:
            try:
                import httpx as _hx
                _is_http = isinstance(e, _hx.HTTPError)
            except Exception:
                _is_http = False
            if _is_http or isinstance(e, (TimeoutError, OSError)):
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
            raise
        os.replace(tmp, dest_dir / f"{code}.png")
        return True
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
        d = _data_subdir("emoji")
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
    data_dir = _data_subdir("emoji")
    if data_dir is None:
        return {"ok": False, "downloaded": [], "error": "持久化目录不可用"}
    if _color_emoji_font_path():
        return {"ok": True, "downloaded": [], "error": ""}
    target = data_dir / ANDROID_EMOJI_FILE
    ok, err = _download_file(ANDROID_EMOJI_URL, target)
    if ok:
        try:
            if not _color_emoji_font_path():
                raise ValueError("字体校验未通过")
        except Exception as e:
            err = str(e)
            ok = False
    if ok:
        _EMOJI_FONT_CACHE.clear()
        return {"ok": True, "downloaded": [ANDROID_EMOJI_FILE], "error": ""}
    return {"ok": False, "downloaded": [], "error": f"{ANDROID_EMOJI_FILE} 下载失败: {err or '未知错误'}"}


def _prefetch_emoji_images(text: str, max_workers: int = 6, max_codes: int = 64,
                           skip_singles: bool = False) -> int:
    """批量预取文本中缺失的全彩 emoji 图（并行 IO）。

    绘制时缺图会逐个串行等待网络（最慢 8s 超时/个），预取后绘制零等待。
    多字符簇必走图片通道；单个 emoji 仅在无系统字体直绘时才需图片。已缓存/已落盘跳过。
    skip_singles 为 True 时单字直接跳过（android 本地彩字已装，绘制零网络）。
    返回本次新下载成功数。
    """
    if not text:
        return 0
    try:
        if time.time() < _EMOJI_REMOTE_DEAD_UNTIL:
            return 0
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
                key_hit = False
                try:
                    if (code, 32) in _EMOJI_IMG_CACHE:
                        key_hit = True
                except Exception:
                    pass
                if key_hit:
                    continue
                try:
                    if (EMOJI_ASSETS_DIR / f"{code}.png").is_file():
                        continue
                    d = _data_subdir("emoji")
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
        return 0
    if not todo:
        return 0
    try:
        data_dir = _data_subdir("emoji")
        if data_dir is None:
            return 0
        import concurrent.futures as _cf
        done = 0
        with _cf.ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(todo)))) as ex:
            futs = {ex.submit(_fetch_remote_emoji, code, data_dir): code for code in todo}
            for fut in _cf.as_completed(futs):
                try:
                    if fut.result():
                        done += 1
                except Exception:
                    pass
        return done
    except Exception:
        return 0


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
    try:
        _font_first = _singles_via_local_font(emoji_style)
    except Exception:
        _font_first = False
    cur_x = x
    i = 0
    text_len = len(text)
    char_positions: List[Tuple[float, float, float, str]] = []

    # 基线对齐：文本与 Emoji 共用同一基线（彩色 Emoji 字体的 ascent 远大于正文字体，
    # 若按同一 y 绘制会导致 Emoji 上浮；全彩图则底部坐到基线上，避免与文字不在一行）
    try:
        _text_asc, _text_desc = font.getmetrics()
    except Exception:
        _text_asc, _text_desc = (font_size, 0)
    try:
        _emo_asc, _emo_desc = emoji_font.getmetrics() if emoji_font is not None else (_text_asc, _text_desc)
    except Exception:
        _emo_asc, _emo_desc = (_text_asc, _text_desc)

    def _paste_image(cluster: str) -> bool:
        """全彩图绘制一簇，成功返回 True"""
        nonlocal cur_x
        emo_img = _get_emoji_image(cluster, font_size, emoji_remote)
        if emo_img is None:
            return False
        # 对齐到正文 CJK 墨迹垂直中心（底部坐基线会整体偏高约半个字距差）
        _h_img = emo_img.height or font_size
        _ink_c = _cjk_ink_center_y(font)
        if _ink_c is None:
            _py = y + _text_asc - _h_img
        else:
            _py = y + _ink_c - _h_img / 2.0
        canvas.paste(emo_img, (int(cur_x), int(round(_py))), emo_img)
        w = font_size + 2
        # 定位按字符拆分：串定位与条目定位必须一致，否则打码错位
        n_chars = len(cluster) or 1
        cw = w / n_chars
        for j, ch in enumerate(cluster):
            char_positions.append((cur_x + j * cw, cw, y, ch))
        cur_x += w
        return True

    def _draw_with_font(cluster: str) -> bool:
        """系统/全彩字体绘制单个 emoji，成功返回 True"""
        nonlocal cur_x
        if len(cluster) != 1 or not is_emoji_char(cluster) or emoji_font is None:
            return False
        emo_font_y = y + (_text_asc - _emo_asc)
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
                # 单个 emoji：android 且本地彩字已装 → 先字体直绘（零网络），
                # 字体缺该字形再回退全彩图；ios/其余情况沿用全彩图优先
                if _font_first and emoji_font is not None:
                    try:
                        _covered = _font_covers(emoji_font, cluster)
                    except Exception:
                        _covered = False
                    if _covered:
                        if _draw_with_font(cluster):
                            i = ni
                            continue
                    if _paste_image(cluster):
                        i = ni
                        continue
                elif emoji_style == "ios":
                    if _paste_image(cluster):
                        i = ni
                        continue
                    if _draw_with_font(cluster):
                        i = ni
                        continue
                else:
                    # 非 iOS（含 android 未装包 / windows）：全彩图优先。NotoColorEmoji 系单尺寸位图，
                    # PIL 无法按任意字号加载，全彩图是唯一可靠的彩色来源；无图再降级字体
                    if _paste_image(cluster):
                        i = ni
                        continue
                    if _draw_with_font(cluster):
                        i = ni
                        continue

        # 正文批量绘制（含无图无字 emoji 的单色回退）：_take_batch 必推进
        # 同字体连续段合并绘制；主字体缺字（装饰字体无 CJK 等）自动按链回退，不再满屏豆腐块
        batch_chars = _take_batch()
        if not batch_chars:
            # 理论上到不了：强制单簇推进，杜绝死循环
            batch_chars = [cluster]
            i = ni
        measured: List[Tuple[str, Any, float]] = []
        for bc in batch_chars:
            if len(bc) == 1:
                dc, bf = _draw_char_and_font(bc, font)
                measured.append((dc, bf, _char_advance(bf, dc)))
            else:
                # 多字符簇不断开，整体跟随主字体绘制；定位按字符拆分，保证打码不错位
                _cw = (sum(_char_advance(font, c) for c in bc) or float(font_size)) / len(bc)
                for c in bc:
                    measured.append((c, font, _cw))
        k2 = 0
        while k2 < len(measured):
            j2 = k2 + 1
            while j2 < len(measured) and measured[j2][1] is measured[k2][1]:
                j2 += 1
            draw.text((cur_x, y), "".join(m[0] for m in measured[k2:j2]), font=measured[k2][1], fill=fill)
            for bc, _bf, w in measured[k2:j2]:
                char_positions.append((cur_x, w, y, bc))
                cur_x += w
            k2 = j2

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
                    width_cache[c] = _adv_text(font, c)
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
    if _np is not None:
        start = _np.array(start_color, dtype=_np.float32)
        end = _np.array(end_color, dtype=_np.float32)
        alpha = _np.linspace(0, 1, height, dtype=_np.float32)[:, None]
        col = (start * (1 - alpha) + end * alpha).astype(_np.uint8)
        col_img = Image.fromarray(col.reshape(height, 1, 3), "RGB")
        return col_img.resize((width, height), Image.NEAREST).convert("RGBA")
    # 无 numpy 兜底：逐行填充（慢，仅极简环境）
    img = Image.new("RGB", (1, height))
    px = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        px[0, y] = tuple(int(round(s + (e - s) * t)) for s, e in zip(start_color, end_color))
    return img.resize((width, height), Image.NEAREST).convert("RGBA")


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


# 单页内容高度上限（超出则分页输出多图，不再丢弃截断）；页数上限防病态超长 OOM
MAX_CONTENT_PAGES = 10


def _render_cache_key(text: str, style: str, theme_mode: str, star_background: bool,
                      star_density: str, mosaic_mode: str, mosaic_type: str,
                      mosaic_half_pos: str, violation_words, font_scale: int,
                      emoji_style: str, page_max_h: int = 3000,
                      card_max_width: int = 680,
                      group_font: Tuple[str, str] = ("", "")) -> tuple:
    """渲染缓存键：文本哈希 + 全套渲染参数 + 当前分钟（顶栏时间参与输出）+ 字体代际"""
    try:
        h = hashlib.md5(str(text or "").encode("utf-8", "ignore")).hexdigest()
    except Exception:
        h = str(hash(text)) if text else ""
    try:
        vw = tuple(violation_words or [])
    except Exception:
        vw = ()
    try:
        minute = time.strftime("%H:%M")
    except Exception:
        minute = ""
    try:
        epoch = int(_FONT_EPOCH)
    except Exception:
        epoch = 0
    try:
        pmh = int(page_max_h or 3000)
    except Exception:
        pmh = 3000
    try:
        cmw = int(card_max_width or 640)
    except Exception:
        cmw = 640
    return (h, style, theme_mode, bool(star_background), str(star_density),
            mosaic_mode, mosaic_type, mosaic_half_pos, vw,
            int(font_scale or 100), str(emoji_style), minute, epoch, pmh, cmw,
            str((group_font or ("", ""))[0]), str((group_font or ("", ""))[1]))


def _continuation_line(idx: int, total: int, font_scale: int = 100):
    """非末页末尾的“未完待续”行"""
    fs = max(6, int(round(26 * int(font_scale or 100) / 100)))
    return (
        f"⬇️ 未完待续（{idx + 1}/{total}）",
        LineBlock(text="…", block_type="text", font_size=fs, is_bold=False),
        int(fs * 1.56),
    )


def _truncated_line(font_scale: int = 100):
    """病态超长（超页数上限）兜底截断行"""
    fs = max(6, int(round(26 * int(font_scale or 100) / 100)))
    return (
        "… 内容过长已截断 …",
        LineBlock(text="…", block_type="text", font_size=fs, is_bold=False),
        int(fs * 1.56),
    )


def _split_content_pages(rendered_lines, max_content: float, font_scale: int = 100, max_pages: int = MAX_CONTENT_PAGES):
    """按单页高度上限切分排版行；非末页追加未完待续行，超页数上限末页追加截断行"""
    pages: List[List[Tuple[str, LineBlock, int]]] = []
    cur: List[Tuple[str, LineBlock, int]] = []
    cur_h = 0
    for entry in rendered_lines:
        _, _, lh = entry
        if cur and cur_h + lh > max_content:
            pages.append(cur)
            cur = []
            cur_h = 0
        cur.append(entry)
        cur_h += lh
    if cur:
        pages.append(cur)
    if not pages:
        pages = [[]]
    total = len(pages)
    out = []
    for idx, pg in enumerate(pages[:max_pages]):
        last = idx == min(total, max_pages) - 1
        if not last:
            out.append(list(pg) + [_continuation_line(idx, min(total, max_pages), font_scale)])
        elif total > max_pages:
            out.append(list(pg) + [_truncated_line(font_scale)])
        else:
            out.append(pg)
    return out


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
        page_max_h: int = 3000,  # 单页总高度上限（点开看长图长度不限）
        card_max_width: int = 640,  # 卡片宽度基线（聊天气泡完整显示）
        custom_font_path: str = "",  # 单群专属常规字体（文件名/绝对路径），空则跟随全局
        custom_bold_font_path: str = "",  # 单群专属粗体，空则复用常规
        perf_out: Optional[Dict[str, float]] = None,  # 性能回填（仅 perf_log 开启时传入；None 则零开销）
    ) -> Image.Image:
        """渲染单图（长内容取第一页；完整多页请用 render_pages）"""
        pages = cls.render_pages(
            text=text, style=style, theme_mode=theme_mode,
            star_background=star_background, star_density=star_density,
            mosaic_mode=mosaic_mode, mosaic_type=mosaic_type,
            violation_words=violation_words, emoji_remote=emoji_remote,
            mosaic_half_pos=mosaic_half_pos, font_scale=font_scale,
            emoji_style=emoji_style, page_max_h=page_max_h,
            card_max_width=card_max_width,
            custom_font_path=custom_font_path,
            custom_bold_font_path=custom_bold_font_path,
            perf_out=perf_out,
        )
        return pages[0]

    @classmethod
    def _prepare_layout(
        cls,
        text: str = "",
        style: str = "ios",
        theme_mode: str = "light",
        mosaic_half_pos: str = "bottom",
        font_scale: int = 100,
        emoji_remote: bool = True,
        emoji_style: Optional[str] = None,
        page_max_h: int = 3000,
        card_max_width: int = 640,
    ) -> Dict[str, Any]:
        """排版（与 render 旧逻辑一致）：参数归一化→分块→自适应宽度→折行→度量。
        返回绘图上下文 ctx，供 render / render_pages / _draw_page 共用。"""
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
            w = _text_advance(font, b.text)
            if w > max_natural_w:
                max_natural_w = w

        # 自适应留白与宽度基准：随 font_scale 动态放缩，50% 小巧，500% 宽阔；
        # 卡片上限收敛保证聊天气泡内完整显示（超长单行宁可折行变高，不横向撑出被裁）；
        # 最后一步绝对上限：任何字号下卡片都不超过该宽度
        scale_ratio = font_scale / 100.0
        try:
            _cw_base = max(480, min(1200, int(card_max_width or 640)))
        except Exception:
            _cw_base = 640
        inner_pad_x = max(24, int(round(40 * min(1.8, max(0.7, scale_ratio)))))
        min_w = max(380, int(round(560 * min(2.5, max(0.65, scale_ratio)))))
        max_w = max(min_w + 120, int(round(_cw_base * min(2.8, max(0.75, scale_ratio)))))
        card_w = max(min_w, min(max_w, int(max_natural_w) + inner_pad_x * 2 + int(40 * scale_ratio)))
        card_w = min(card_w, _cw_base)
        content_w = max(200, card_w - inner_pad_x * 2)

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
        # 单页总高度上限（默认 3000，点开看长图长度不限；超限分页输出，不再丢弃截断）
        try:
            page_max_h = max(800, min(3800, int(page_max_h or 3000)))
        except Exception:
            page_max_h = 3000
        max_content = max(400, page_max_h - (card_inner_pad_y*2 + header_h + footer_gap + footer_block))
        return {
            "text": text, "style": style, "theme": theme, "theme_mode": theme_mode,
            "mosaic_half_pos": mosaic_half_pos, "font_scale": font_scale,
            "emoji_style": emoji_style, "emoji_remote_eff": emoji_remote_eff,
            "card_w": card_w, "content_w": content_w, "header_h": header_h,
            "card_inner_pad_y": card_inner_pad_y, "footer_gap": footer_gap,
            "footer_block": footer_block, "foot_fh": foot_fh,
            "inner_pad_x": inner_pad_x, "content_h": content_h, "card_h": card_h,
            "rendered_lines": rendered_lines, "max_content": max_content,
        }

    @classmethod
    def render_pages(
        cls,
        text: str = "",
        style: str = "ios",
        theme_mode: str = "light",
        star_background: bool = True,
        star_density: str = "medium",
        mosaic_mode: str = "none",
        mosaic_type: str = "pixel",
        violation_words: Optional[List[str]] = None,
        emoji_remote: bool = True,
        mosaic_half_pos: str = "bottom",
        font_scale: int = 100,
        emoji_style: Optional[str] = None,
        page_max_h: int = 3000,
        card_max_width: int = 640,
        custom_font_path: str = "",
        custom_bold_font_path: str = "",
        perf_out: Optional[Dict[str, float]] = None,
    ) -> List[Image.Image]:
        """长内容分页渲染：每页独立成卡（含顶栏/底栏），顺序返回图片列表。

        相同输入短时间内直接复用结果（主链路与适配器钩子常对同一文本各渲染一次）。
        perf_out 非 None 时回填 layout_ms / prefetch_ms / draw_ms / mosaic_ms（毫秒）；
        为 None 则不计时，零开销。缓存命中时各项为 0（确实没干活）。
        """
        group_font = resolve_group_font_override(custom_font_path, custom_bold_font_path)
        _tok = _GROUP_FONT_OVERRIDE.set(group_font if any(group_font) else None)
        try:
            return cls._render_pages_inner(
                text=text, style=style, theme_mode=theme_mode,
                star_background=star_background, star_density=star_density,
                mosaic_mode=mosaic_mode, mosaic_type=mosaic_type,
                violation_words=violation_words, emoji_remote=emoji_remote,
                mosaic_half_pos=mosaic_half_pos, font_scale=font_scale,
                emoji_style=emoji_style, page_max_h=page_max_h,
                card_max_width=card_max_width, group_font=group_font,
                perf_out=perf_out,
            )
        finally:
            try:
                _GROUP_FONT_OVERRIDE.reset(_tok)
            except Exception:
                pass

    @classmethod
    def _render_pages_inner(
        cls,
        text: str = "",
        style: str = "ios",
        theme_mode: str = "light",
        star_background: bool = True,
        star_density: str = "medium",
        mosaic_mode: str = "none",
        mosaic_type: str = "pixel",
        violation_words: Optional[List[str]] = None,
        emoji_remote: bool = True,
        mosaic_half_pos: str = "bottom",
        font_scale: int = 100,
        emoji_style: Optional[str] = None,
        page_max_h: int = 3000,
        card_max_width: int = 640,
        group_font: Tuple[str, str] = ("", ""),
        perf_out: Optional[Dict[str, float]] = None,
    ) -> List[Image.Image]:
        if perf_out is not None:
            for _k in ("layout_ms", "prefetch_ms", "draw_ms", "mosaic_ms"):
                try:
                    perf_out[_k] = 0.0
                except Exception:
                    break
        if perf_out is not None:
            _t_layout = time.perf_counter()
        ctx = cls._prepare_layout(
            text=text, style=style, theme_mode=theme_mode,
            mosaic_half_pos=mosaic_half_pos, font_scale=font_scale,
            emoji_remote=emoji_remote, emoji_style=emoji_style,
            page_max_h=page_max_h, card_max_width=card_max_width,
        )
        if perf_out is not None:
            try:
                perf_out["layout_ms"] = (time.perf_counter() - _t_layout) * 1000.0
            except Exception:
                pass
        try:
            cache_key = _render_cache_key(
                ctx["text"], ctx["style"], ctx["theme_mode"], star_background, star_density,
                mosaic_mode, mosaic_type, ctx["mosaic_half_pos"], violation_words,
                ctx["font_scale"], ctx["emoji_style"],
                page_max_h=page_max_h, card_max_width=card_max_width,
                group_font=group_font,
            )
        except Exception:
            cache_key = None
        if cache_key is not None:
            try:
                hit = _RENDER_CACHE.get(cache_key)
                if hit:
                    return list(hit)
            except Exception:
                pass
        # emoji 缺图并行预取（绘制时不再逐个串行等网络）
        # emoji 缺图并行预取（绘制时不再逐个串行等网络；键已在入口初始化）
        try:
            if ctx["emoji_remote_eff"]:
                try:
                    _skip_1 = _singles_via_local_font(ctx["emoji_style"])
                except Exception:
                    _skip_1 = False
                if perf_out is not None:
                    _t_pf = time.perf_counter()
                    _prefetch_emoji_images(ctx["text"], skip_singles=_skip_1)
                    perf_out["prefetch_ms"] = (time.perf_counter() - _t_pf) * 1000.0
                else:
                    _prefetch_emoji_images(ctx["text"], skip_singles=_skip_1)
        except Exception:
            pass
        pages = _split_content_pages(ctx["rendered_lines"], ctx["max_content"], ctx["font_scale"])
        total = len(pages)
        if perf_out is not None:
            _t_draw = time.perf_counter()
        images = [
            cls._draw_page(
                ctx, pg, idx, total,
                star_background=star_background, star_density=star_density,
                mosaic_mode=mosaic_mode, mosaic_type=mosaic_type,
                mosaic_half_pos=ctx["mosaic_half_pos"],
                violation_words=violation_words,
                emoji_remote_eff=ctx["emoji_remote_eff"],
                emoji_style=ctx["emoji_style"],
                perf_out=perf_out,
            )
            for idx, pg in enumerate(pages)
        ]
        if perf_out is not None:
            try:
                perf_out["draw_ms"] = float(perf_out.get("draw_ms", 0.0)) + (time.perf_counter() - _t_draw) * 1000.0
            except Exception:
                pass
        # 仅缓存小体量结果，避免大长图堆内存
        if cache_key is not None and len(images) <= 2:
            try:
                if len(_RENDER_CACHE) >= _RENDER_CACHE_LIMIT:
                    _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))
                _RENDER_CACHE[cache_key] = list(images)
            except Exception:
                pass
        return images

    @classmethod
    def _draw_page(
        cls,
        ctx: Dict[str, Any],
        page_lines: List[Tuple[str, LineBlock, int]],
        page_idx: int,
        total_pages: int,
        star_background: bool = True,
        star_density: str = "medium",
        mosaic_mode: str = "none",
        mosaic_type: str = "pixel",
        mosaic_half_pos: str = "bottom",
        violation_words: Optional[List[str]] = None,
        emoji_remote_eff: bool = True,
        emoji_style: str = "none",
        perf_out: Optional[Dict[str, float]] = None,
    ) -> Image.Image:
        """绘制单页卡片（render_pages 与 render 多页首屏使用）"""
        style = ctx["style"]
        theme = ctx["theme"]
        theme_mode = ctx["theme_mode"]
        text = ctx["text"]
        card_w = ctx["card_w"]
        header_h = ctx["header_h"]
        card_inner_pad_y = ctx["card_inner_pad_y"]
        footer_gap = ctx["footer_gap"]
        footer_block = ctx["footer_block"]
        inner_pad_x = ctx["inner_pad_x"]
        content_h = sum(lh for _, _, lh in page_lines)
        card_h = card_inner_pad_y + header_h + content_h + footer_gap + footer_block + card_inner_pad_y
        margin_x = 30
        margin_y = 30
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

        for wrap_line, block, line_h in page_lines:
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

        # 10. 绘制高雅极简 Footer（移动到左下角，多页时标注页码）
        # cur_y 此时为正文结束位置
        footer_line_y = cur_y + footer_gap
        c_draw.line([(inner_pad_x, footer_line_y), (card_w - inner_pad_x, footer_line_y)], fill=theme.divider, width=1)
        foot_font = get_font(11, bold=False)
        ft_text = "Generated by xbimg" + (f" · ({page_idx + 1}/{total_pages})" if total_pages > 1 else "")
        c_draw.text((inner_pad_x, footer_line_y + 8), ft_text, font=foot_font, fill=theme.text_muted)

        # 11. 处理违规马赛克（字级精准半打码：支持上/下/随机）
        if mosaic_mode in ("half", "full") and all_char_positions:
            if perf_out is not None:
                _t_mo = time.perf_counter()
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
                try:
                    perf_out["mosaic_ms"] = float(perf_out.get("mosaic_ms", 0.0)) + (time.perf_counter() - _t_mo) * 1000.0
                except Exception:
                    pass
            else:
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

        # 去混淆映射：与 moderation 侧 _CONDENSE_RE 同规则压缩后定位，
        # 使“赌-博/违 禁 词”类混淆命中也能打到正确字，而非退化整区遮挡
        _condensed_cache: dict = {}

        def _condensed_with_map(s: str):
            hit = _condensed_cache.get(s)
            if hit is not None:
                return hit
            chars: list = []
            idx_map: list = []
            for i, ch in enumerate(s):
                if not _MOSAIC_CONDENSE_RE.match(ch):
                    chars.append(ch)
                    idx_map.append(i)
            res = ("".join(chars), idx_map)
            _condensed_cache[s] = res
            return res

        for kw in violation_words:
            kw_lower = kw.strip().lower()
            if not kw_lower:
                continue
            spans: list = []
            start = 0
            while True:
                idx = chars_lower.find(kw_lower, start)
                if idx < 0:
                    break
                spans.append((idx, idx + len(kw_lower)))
                start = idx + 1
            if not spans:
                # 直接找不到时走去混淆定位（原文与关键词双边压缩）
                condensed_chars, idx_map = _condensed_with_map(chars_lower)
                if condensed_chars and idx_map:
                    for cand_kw in {kw_lower, _MOSAIC_CONDENSE_RE.sub("", kw_lower)}:
                        if not cand_kw:
                            continue
                        cstart = 0
                        while True:
                            cidx = condensed_chars.find(cand_kw, cstart)
                            if cidx < 0:
                                break
                            cend = min(cidx + len(cand_kw), len(idx_map))
                            if cend > cidx:
                                spans.append((idx_map[cidx], idx_map[cend - 1] + 1))
                            cstart = cidx + 1
                        if spans:
                            break
            for s, e in spans:
                for ci in range(max(0, s), min(e, len(all_char_positions))):
                    violation_char_indices.add(ci)

        if not violation_char_indices:
            cls._fallback_area_mosaic(card_surface, all_char_positions, mosaic_mode, mosaic_type, theme, card_w, mosaic_half_pos)
            return

        # 对每个违规字符施加马赛克（更精致：柔和像素/磨砂+主题色轻遮罩）
        # 同行连续且同半区的字符合并为一次区域操作，大幅减少逐字 resize 开销
        is_dark = theme.bg_gradient_start[0] < 80  # 简易深浅判断
        # 随机模式用稳定种子，保证同文本同效果但每字不同（先逐字决策再合并，随机序列不变）
        rand = random.Random(_stable_seed(text + "".join(violation_words)) ^ 0x9E3779B9) if mosaic_half_pos == "random" else None
        runs = []
        for ci in sorted(violation_char_indices):
            if mosaic_mode == "half":
                # 半打码：按配置选择上/下/随机
                if mosaic_half_pos == "top":
                    is_top = True
                elif mosaic_half_pos == "random":
                    is_top = rand.choice([True, False]) if rand else False  # type: ignore
                else:
                    is_top = False
            else:
                is_top = None
            if runs and runs[-1][0] == is_top and ci == runs[-1][1][-1] + 1:
                _, _, cy0, _, clh0 = all_char_positions[runs[-1][1][0]]
                _, _, cy1, _, clh1 = all_char_positions[ci]
                if cy0 == cy1 and clh0 == clh1:
                    runs[-1][1].append(ci)
                    continue
            runs.append([is_top, [ci]])
        for is_top, idxs in runs:
            cx, _cw0, cy, _ch, clh = all_char_positions[idxs[0]]
            _ex, _ew, _ey, _ech, _elh = all_char_positions[idxs[-1]]
            left = max(0, int(cx))
            right = min(card_surface.width, int(_ex + _ew + 1))

            if mosaic_mode == "half":
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
