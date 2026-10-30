# -*- coding: utf-8 -*-
"""渲染引擎 · 主题层：四套配色（ios/android16 × light/dark）、自定义主题色派生混合。"""
from dataclasses import dataclass
from typing import List, Tuple
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
    ("dragon", "dark"): ThemeColors(
        bg_gradient_start=(22, 14, 10),
        bg_gradient_end=(14, 8, 5),
        card_bg=(26, 16, 10, 245),
        card_border=(235, 175, 75, 255),
        card_shadow=(0, 0, 0, 100),
        text_primary=(255, 252, 245),
        text_secondary=(245, 210, 150),
        text_muted=(200, 150, 75),
        accent=(255, 195, 70),
        accent_bg=(255, 195, 70, 32),
        code_bg=(22, 14, 9, 240),
        code_border=(180, 130, 50, 180),
        code_text=(255, 235, 190),
        divider=(210, 155, 60, 200),
        star_colors=[(255, 215, 100), (255, 180, 50), (255, 240, 180), (255, 255, 255)],
    ),
    ("dragon", "light"): ThemeColors(
        bg_gradient_start=(22, 14, 10),
        bg_gradient_end=(14, 8, 5),
        card_bg=(26, 16, 10, 245),
        card_border=(235, 175, 75, 255),
        card_shadow=(0, 0, 0, 100),
        text_primary=(255, 252, 245),
        text_secondary=(245, 210, 150),
        text_muted=(200, 150, 75),
        accent=(255, 195, 70),
        accent_bg=(255, 195, 70, 32),
        code_bg=(22, 14, 9, 240),
        code_border=(180, 130, 50, 180),
        code_text=(255, 235, 190),
        divider=(210, 155, 60, 200),
        star_colors=[(255, 215, 100), (255, 180, 50), (255, 240, 180), (255, 255, 255)],
    ),
}

def _parse_hex_color(hex_str: str):
    """解析十六进制颜色 (#RRGGBB 或 #RGB)，返回 (r, g, b) 或 None"""
    if not hex_str:
        return None
    s = str(hex_str).strip().lstrip("#")
    if len(s) == 6:
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except Exception:
            return None
    elif len(s) == 3:
        try:
            return (int(s[0]*2, 16), int(s[1]*2, 16), int(s[2]*2, 16))
        except Exception:
            return None
    return None


def _blend_rgb(c1: Tuple[int, int, int], c2: Tuple[int, int, int], ratio: float) -> Tuple[int, int, int]:
    """按比例混合两种颜色：c1 * (1 - ratio) + c2 * ratio"""
    ratio = max(0.0, min(1.0, float(ratio)))
    r = int(round(c1[0] * (1.0 - ratio) + c2[0] * ratio))
    g = int(round(c1[1] * (1.0 - ratio) + c2[1] * ratio))
    b = int(round(c1[2] * (1.0 - ratio) + c2[2] * ratio))
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def _apply_theme_accent(theme: "ThemeColors", base: "ThemeColors",
                        c: Tuple[int, int, int], is_light: bool) -> None:
    """自定义主题色覆盖：accent 直换，派生色按固定配比混合（浅色/深色各一套配比）"""
    theme.accent = c
    theme.accent_bg = (*c, 28)
    if is_light:
        theme.bg_gradient_start = _blend_rgb(base.bg_gradient_start, c, 0.12)
        theme.bg_gradient_end = _blend_rgb(base.bg_gradient_end, c, 0.18)
        theme.card_border = (*_blend_rgb((255, 255, 255), c, 0.18), 255)
        theme.divider = (*_blend_rgb(base.divider[:3], c, 0.35), 200)
        theme.code_border = (*_blend_rgb(base.code_border[:3], c, 0.35), 200)
        theme.code_bg = (*_blend_rgb(base.code_bg[:3], c, 0.08), 255)
        theme.star_colors = [c, _blend_rgb((255, 214, 102), c, 0.35),
                             _blend_rgb((160, 200, 255), c, 0.35), (255, 255, 255)]
    else:
        theme.bg_gradient_start = _blend_rgb(base.bg_gradient_start, c, 0.14)
        theme.bg_gradient_end = _blend_rgb(base.bg_gradient_end, c, 0.08)
        theme.divider = (*_blend_rgb(base.divider[:3], c, 0.35), 180)
        theme.code_border = (*_blend_rgb(base.code_border[:3], c, 0.35), 180)
        theme.code_bg = (*_blend_rgb(base.code_bg[:3], c, 0.12), 255)
        theme.star_colors = [_blend_rgb((255, 255, 255), c, 0.6), c,
                             _blend_rgb((170, 210, 255), c, 0.35), (245, 248, 255)]
