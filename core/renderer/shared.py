# -*- coding: utf-8 -*-
"""渲染引擎 · 基础层：常量、字符/emoji 簇原语、文本度量、数据目录解析（全包最底层，不依赖兄弟模块）。"""
import re
from pathlib import Path
from typing import List, Optional, Tuple
from PIL import ImageFont
# 本地资源目录（需先于字体探测定义：支持用户在 assets/fonts 下自带 CJK 字体）
EMOJI_ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets" / "emojis"
BUNDLED_FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"

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
# 字体管理器：自定义字体 / 持久化目录 / 缺字自动下载
# 插件不自带字体文件（体积+版权），缺 CJK 时从 noto-cjk 官方下载到数据目录
# ==========================================
FONT_DATA_SUBDIR = "fonts"
_FONT_DATA_DIR: Optional[Path] = None
# 不独立成图的码位：ZWJ/变体选择符/肤色/键帽符/地区指示符（单独出现无意义，随簇处理）
_NON_SINGLETON_CPS = frozenset(
    [0x200D, 0x200C, 0xFE0E, 0xFE0F, 0x20E3]
    + list(range(0xFE00, 0xFE10))
    + list(range(0x1F3FB, 0x1F400))
    + list(range(0x1F1E6, 0x1F1FC))
)


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


def _data_subdir(name: str) -> Optional[Path]:
    global _FONT_DATA_DIR
    if _FONT_DATA_DIR is None:
        try:
            from ..config import resolve_data_dir
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
