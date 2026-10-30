# -*- coding: utf-8 -*-
"""渲染引擎 · 绘制层：渐变/星空/投影/正文批量段缓存/图文混排绘制。"""
import hashlib
import math
from typing import Any, Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import numpy as _np
except Exception:
    _np = None

from .shared import _is_emoji_cluster, _take_cluster, is_emoji_char
from .fonts import (
    _char_advance, _cjk_ink_center_y, _draw_char_and_font, _font_covers, _font_key,
)
from .emoji import _get_emoji_image, _singles_via_local_font, get_emoji_font
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
        _emo_asc, _ = emoji_font.getmetrics() if emoji_font is not None else (_text_asc, _text_desc)
    except Exception:
        _emo_asc, _ = (_text_asc, _text_desc)

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
            _draw_text_run_cached(canvas, draw, cur_x, y, "".join(m[0] for m in measured[k2:j2]), measured[k2][1], fill, font_size)
            for bc, _bf, w in measured[k2:j2]:
                char_positions.append((cur_x, w, y, bc))
                cur_x += w
            k2 = j2

    return char_positions


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


_GRADIENT_CACHE: Dict[Tuple, Image.Image] = {}
_GRADIENT_CACHE_LIMIT = 8
_STAR_LAYER_CACHE: Dict[Tuple, Image.Image] = {}
_STAR_LAYER_CACHE_LIMIT = 8


def _fast_linear_gradient(
    width: int,
    height: int,
    start_color: Tuple[int, int, int],
    end_color: Tuple[int, int, int],
) -> Image.Image:
    """极速生成平滑双向渐变底图（1D 生成后水平拉伸，< 4ms；同尺寸缓存）"""
    try:
        gkey = (int(width), int(height), tuple(start_color), tuple(end_color))
    except Exception:
        gkey = None
    if gkey is not None:
        hit = _GRADIENT_CACHE.get(gkey)
        if hit is not None:
            # 调用方可能就地 paste 修改画布，命中返回副本防串染
            return hit.copy()
    if _np is not None:
        start = _np.array(start_color, dtype=_np.float32)
        end = _np.array(end_color, dtype=_np.float32)
        alpha = _np.linspace(0, 1, height, dtype=_np.float32)[:, None]
        col = (start * (1 - alpha) + end * alpha).astype(_np.uint8)
        col_img = Image.fromarray(col.reshape(height, 1, 3), "RGB")
        out = col_img.resize((width, height), Image.NEAREST).convert("RGBA")
    else:
        # 无 numpy 兜底：逐行填充（慢，仅极简环境）
        img = Image.new("RGB", (1, height))
        px = img.load()
        for y in range(height):
            t = y / max(1, height - 1)
            px[0, y] = tuple(int(round(s + (e - s) * t)) for s, e in zip(start_color, end_color))
        out = img.resize((width, height), Image.NEAREST).convert("RGBA")
    if gkey is not None:
        try:
            if len(_GRADIENT_CACHE) >= _GRADIENT_CACHE_LIMIT:
                _GRADIENT_CACHE.pop(next(iter(_GRADIENT_CACHE)))
            # 缓存只存“未绘制”的纯净副本：调用方拿到 out 后会就地 paste 阴影与整张
            # 卡片（含正文），直接存 out 会把上一条消息烙进缓存，之后同尺寸的每张图
            # 都会透出它的残影（卡片底半透明 245，残影约 4% 可见度，即“淡淡的印记”）
            _GRADIENT_CACHE[gkey] = out.copy()
        except Exception:
            pass
    return out


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


# 正文批量段位图缓存（同串+同字体+同字号+同色免重复 Font.render）
_TEXT_RUN_CACHE: Dict[Tuple, Image.Image] = {}
_TEXT_RUN_CACHE_LIMIT = 512


def _draw_text_run_cached(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Any,
    font_size: int,
) -> None:
    """正文段 mask 缓存：与 ImageDraw.text 同路径 getmask2+draw_bitmap，像素等价"""
    if not text:
        return
    try:
        ix = int(x)
        iy = int(y)
        start = (math.modf(x)[0], math.modf(y)[0])
        ink, fill_ink = draw._getink(fill)
        if ink is None:
            ink = fill_ink
        mode = draw.fontmode
        key = (text, _font_key(font), int(getattr(font, "size", font_size) or font_size),
               ink, mode, start)
    except Exception:
        key = None
        ix, iy, ink, mode, start = int(x), int(y), None, None, None
    cached = _TEXT_RUN_CACHE.get(key) if key is not None else None
    if cached is None:
        try:
            if ink is None or mode is None:
                draw.text((x, y), text, font=font, fill=fill)
                return
            mask, offset = font.getmask2(
                text, mode, None, None, None, 0, None, ink or 0, start, stroke_filled=True
            )
            cached = (mask, offset)
            if key is not None:
                try:
                    if len(_TEXT_RUN_CACHE) >= _TEXT_RUN_CACHE_LIMIT:
                        _TEXT_RUN_CACHE.pop(next(iter(_TEXT_RUN_CACHE)))
                    _TEXT_RUN_CACHE[key] = cached
                except Exception:
                    pass
        except Exception:
            draw.text((x, y), text, font=font, fill=fill)
            return
    try:
        mask, offset = cached
        draw.draw.draw_bitmap((ix + offset[0], iy + offset[1]), mask, ink)
    except Exception:
        draw.text((x, y), text, font=font, fill=fill)
