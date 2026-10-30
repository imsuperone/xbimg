# -*- coding: utf-8 -*-
"""渲染引擎 · 编排层：渲染缓存、参数归一化、分页渲染主类 MessageImageRenderer。"""
import copy
import hashlib
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter

try:
    from astrbot.api import logger
except Exception:
    import logging
    logger = logging.getLogger("msg2img")

try:
    from ..common import _CONDENSE_CONFUSABLES_RE as _MOSAIC_CONDENSE_RE
except (ImportError, ValueError):
    from core.common import _CONDENSE_CONFUSABLES_RE as _MOSAIC_CONDENSE_RE

from . import shared, fonts, emoji
from .shared import _RE_BULLET, _text_size
from .fonts import (
    _GROUP_FONT_OVERRIDE, _text_advance, get_font, resolve_group_font_override,
)
from .emoji import (
    _EMOJI_PREFETCH_BUDGET_S, _singles_via_local_font, _submit_prefetch,
)
from .theme import THEMES, ThemeColors, _apply_theme_accent, _blend_rgb, _parse_hex_color
from .layout import (
    LineBlock, _parse_content_blocks, _split_content_pages, _wrap_text_line,
)
from .painter import (
    _STAR_LAYER_CACHE, _STAR_LAYER_CACHE_LIMIT, _draw_mixed_text,
    _draw_star_sparkle, _fast_linear_gradient, _get_card_shadow, _stable_seed,
)
# 相同文本渲染结果缓存（主链路与适配器钩子常对同一文本各渲染一次，命中直接复用）
_RENDER_CACHE: Dict[Tuple, List[Image.Image]] = {}
_RENDER_CACHE_LIMIT = 12
# 排版结果缓存：同文同参数跳过分块/折行/度量（fonts._FONT_EPOCH 变更自动作废）
_LAYOUT_CACHE: Dict[Tuple, Dict[str, Any]] = {}
_LAYOUT_CACHE_LIMIT = 32

def _norm_cache_params(text, style, theme_mode, mosaic_half_pos, font_scale,
                       emoji_style, emoji_remote=True, theme_color=""):
    """与 _prepare_layout 一致的轻量归一化（缓存键前置计算用，跳过昂贵排版）"""
    text = str(text or "").strip()
    style = str(style or "ios").lower()
    if style in ("android", "android16", "安卓"):
        style = "android16"
    elif style in ("dragon", "金龙", "国风", "golden_dragon"):
        style = "dragon"
    else:
        style = "ios"
    theme_mode = str(theme_mode or "light").lower()
    if theme_mode not in ("light", "dark"):
        theme_mode = "light"
    mosaic_half_pos = str(mosaic_half_pos or "bottom").lower()
    if mosaic_half_pos not in ("top", "bottom", "random"):
        mosaic_half_pos = "bottom"
    try:
        font_scale = int(font_scale)
    except Exception:
        font_scale = 100
    font_scale = max(50, min(500, font_scale))
    if emoji_style is None:
        if isinstance(emoji_remote, str):
            emoji_style = str(emoji_remote).lower()
            if emoji_style not in ("none", "ios", "android", "windows"):
                emoji_style = fonts._EMOJI_STYLE
        else:
            emoji_style = fonts._EMOJI_STYLE if fonts._EMOJI_STYLE in ("none", "ios", "android", "windows") else ("android" if bool(emoji_remote) else "none")
    else:
        emoji_style = str(emoji_style).lower()
        if emoji_style not in ("none", "ios", "android", "windows"):
            emoji_style = "none"
    theme_color = str(theme_color or "").strip().lower()
    return text, style, theme_mode, mosaic_half_pos, font_scale, emoji_style, theme_color


def _render_cache_key(text: str, style: str, theme_mode: str, star_background: bool,
                      star_density: str, mosaic_mode: str, mosaic_type: str,
                      mosaic_half_pos: str, violation_words, font_scale: int,
                      emoji_style: str, page_max_h: int = 3000,
                      card_max_width: int = 680,
                      group_font: Tuple[str, str] = ("", ""),
                      theme_color: str = "") -> tuple:
    """渲染缓存键：文本哈希 + 全套渲染参数 + 字体代际。
    不含墙钟分钟：顶栏时间陈旧可接受，换来跨分钟同参直接命中（避免每分钟强制重渲）。"""
    try:
        h = hashlib.md5(str(text or "").encode("utf-8", "ignore")).hexdigest()
    except Exception:
        h = str(hash(text)) if text else ""
    try:
        vw = tuple(violation_words or [])
    except Exception:
        vw = ()
    try:
        epoch = int(fonts._FONT_EPOCH)
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
            int(font_scale or 100), str(emoji_style), epoch, pmh, cmw,
            str((group_font or ("", ""))[0]), str((group_font or ("", ""))[1]), str(theme_color or "").strip().lower())


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
        theme_color: str = "",  # 转图自定义主题色 (#HEX)
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
            theme_color=theme_color,
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
        theme_color: str = "",
    ) -> Dict[str, Any]:
        """排版（与 render 旧逻辑一致）：参数归一化→分块→自适应宽度→折行→度量。
        返回绘图上下文 ctx，供 render / render_pages / _draw_page 共用。"""
        # 1. 参数归一化：唯一实现在 _norm_cache_params（缓存键与排版同源，杜绝两套规则漂移）
        norm_res = _norm_cache_params(
            text, style, theme_mode, mosaic_half_pos, font_scale,
            emoji_style=emoji_style, emoji_remote=emoji_remote,
            theme_color=theme_color,
        )
        text, style, theme_mode, mosaic_half_pos, font_scale, emoji_style = norm_res[0], norm_res[1], norm_res[2], norm_res[3], norm_res[4], norm_res[5]
        eff_theme_color = norm_res[6] if len(norm_res) > 6 else ""
        base_theme = THEMES.get((style, theme_mode), THEMES[("ios", "light")])
        theme = copy.copy(base_theme)
        if eff_theme_color:
            c_rgb = _parse_hex_color(eff_theme_color)
            if c_rgb:
                _apply_theme_accent(theme, base_theme, c_rgb, theme_mode == "light")
        _sc = lambda v: max(6, int(round(v * font_scale / 100)))
        # 是否允许云端
        emoji_remote_eff = emoji_style != "none"

        # 排版缓存：同文同参同字体代际直接复用（分块/自适应宽/折行/度量全跳过）
        try:
            _grp = _GROUP_FONT_OVERRIDE.get() or ("", "")
            _lkey = (
                hashlib.md5(text.encode("utf-8", "ignore")).hexdigest(),
                style, theme_mode, mosaic_half_pos, font_scale, emoji_style,
                int(page_max_h or 3000), int(card_max_width or 640),
                str(_grp[0]), str(_grp[1]), int(fonts._FONT_EPOCH),
                eff_theme_color,
            )
        except Exception:
            _lkey = None
        if _lkey is not None:
            try:
                _lhit = _LAYOUT_CACHE.get(_lkey)
                if _lhit is not None:
                    return dict(_lhit)
            except Exception:
                pass

        # 2. 动态自适应卡片宽度（支持字体百分比缩放自适应撑缩）
        if style == "dragon":
            # 国风金龙模板：红框可用宽达 1720，自适应字号与紧凑排版
            scale_ratio = font_scale / 100.0
            card_w = 2208
            inner_pad_x = 240
            content_w = 1720
            blocks = _parse_content_blocks(text)
            raw_lines = len([b for b in blocks if b.text])
            if raw_lines <= 8:
                fs_body = max(28, int(round(56 * scale_ratio)))
                fs_title = max(32, int(round(70 * scale_ratio)))
            elif raw_lines <= 14:
                fs_body = max(24, int(round(48 * scale_ratio)))
                fs_title = max(28, int(round(62 * scale_ratio)))
            else:
                fs_body = max(22, int(round(42 * scale_ratio)))
                fs_title = max(26, int(round(52 * scale_ratio)))

            for b in blocks:
                b.is_bold = True
                if b.block_type == "heading1":
                    b.font_size = fs_title
                elif b.block_type in ("heading2", "heading3"):
                    b.font_size = int(round(fs_title * 0.9))
                else:
                    b.font_size = fs_body

            rendered_lines: List[Tuple[str, LineBlock, int]] = []
            for b in blocks:
                if b.block_type == "divider":
                    rendered_lines.append(("", b, 30))
                    continue
                font = get_font(b.font_size, bold=True)
                if not b.text:
                    rendered_lines.append(("", b, int(b.font_size * 0.7)))
                    continue
                sub_lines = _wrap_text_line(b.text, font, content_w, b.font_size)
                line_h = int(b.font_size * 1.6)
                for sl in sub_lines:
                    rendered_lines.append((sl, b, line_h))

            header_h = 100
            card_inner_pad_y = 20
            footer_gap = 20
            footer_block = 40
            foot_fh = 24
            content_h = sum(lh for _, _, lh in rendered_lines)
            card_h = 2080
            max_content = 1440
        else:
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
        ctx = {
            "text": text, "style": style, "theme": theme, "theme_mode": theme_mode,
            "mosaic_half_pos": mosaic_half_pos, "font_scale": font_scale,
            "emoji_style": emoji_style, "emoji_remote_eff": emoji_remote_eff,
            "card_w": card_w, "content_w": content_w, "header_h": header_h,
            "card_inner_pad_y": card_inner_pad_y, "footer_gap": footer_gap,
            "footer_block": footer_block, "foot_fh": foot_fh,
            "inner_pad_x": inner_pad_x, "content_h": content_h, "card_h": card_h,
            "rendered_lines": rendered_lines, "max_content": max_content,
            "eff_theme_color": eff_theme_color,
        }
        if _lkey is not None:
            try:
                if len(_LAYOUT_CACHE) >= _LAYOUT_CACHE_LIMIT:
                    _LAYOUT_CACHE.pop(next(iter(_LAYOUT_CACHE)), None)
                _LAYOUT_CACHE[_lkey] = dict(ctx)
            except Exception:
                pass
        return ctx

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
        theme_color: str = "",
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
                theme_color=theme_color,
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
        theme_color: str = "",
        perf_out: Optional[Dict[str, float]] = None,
    ) -> List[Image.Image]:
        if perf_out is not None:
            for _k in ("layout_ms", "prefetch_ms", "draw_ms", "mosaic_ms"):
                try:
                    perf_out[_k] = 0.0
                except Exception:
                    break
        # 缓存键前置：命中直接返回，跳过昂贵排版+预取+绘制
        try:
            _norm = _norm_cache_params(
                text, style, theme_mode, mosaic_half_pos, font_scale,
                emoji_style, emoji_remote, theme_color,
            )
            _nt, _ns, _ntm, _nmhp, _nfs, _nes = _norm[0], _norm[1], _norm[2], _norm[3], _norm[4], _norm[5]
            _ntc = _norm[6] if len(_norm) > 6 else ""
            cache_key = _render_cache_key(
                _nt, _ns, _ntm, star_background, star_density,
                mosaic_mode, mosaic_type, _nmhp, violation_words,
                _nfs, _nes,
                page_max_h=page_max_h, card_max_width=card_max_width,
                group_font=group_font,
                theme_color=_ntc,
            )
        except Exception:
            cache_key = None
            _nt, _nes = None, None
        if cache_key is not None:
            try:
                hit = _RENDER_CACHE.get(cache_key)
                if hit:
                    return list(hit)
            except Exception:
                pass
        # emoji 缺图预取与排版并行：先提交线程池，layout 跑完再汇合，
        # prefetch_ms 只计阻塞等待（与 layout 重叠的时间不计入本次延迟）
        # _pf_complete：预取是否在预算内取完全部 todo（返回元组第二位即 complete）
        _pf_fut = None
        _pf_complete = True
        try:
            if _nes is not None and _nes != "none":
                try:
                    _skip_1 = _singles_via_local_font(_nes)
                except Exception:
                    _skip_1 = False
                _pf_fut = _submit_prefetch(_nt or "", _skip_1)
        except Exception:
            _pf_fut = None
        if perf_out is not None:
            _t_layout = time.perf_counter()
        ctx = cls._prepare_layout(
            text=text, style=style, theme_mode=theme_mode,
            mosaic_half_pos=mosaic_half_pos, font_scale=font_scale,
            emoji_remote=emoji_remote, emoji_style=emoji_style,
            page_max_h=page_max_h, card_max_width=card_max_width,
            theme_color=theme_color,
        )
        if perf_out is not None:
            try:
                perf_out["layout_ms"] = (time.perf_counter() - _t_layout) * 1000.0
            except Exception:
                pass
        # 汇合预取（带时间预算：超时则本次绘制关掉云端补全，后台续跑剩余）
        try:
            if _pf_fut is not None:
                if perf_out is not None:
                    _t_pf = time.perf_counter()
                _, _pf_complete = _pf_fut.result(timeout=max(1.0, _EMOJI_PREFETCH_BUDGET_S + 1.0))
                if perf_out is not None:
                    perf_out["prefetch_ms"] = (time.perf_counter() - _t_pf) * 1000.0
            elif ctx["emoji_remote_eff"]:
                # 归一化失败等回退：保持原串行预取路径
                try:
                    _skip_1 = _singles_via_local_font(ctx["emoji_style"])
                except Exception:
                    _skip_1 = False
                if perf_out is not None:
                    _t_pf = time.perf_counter()
                    _, _pf_complete = emoji._prefetch_emoji_images(ctx["text"], skip_singles=_skip_1)
                    perf_out["prefetch_ms"] = (time.perf_counter() - _t_pf) * 1000.0
                else:
                    _, _pf_complete = emoji._prefetch_emoji_images(ctx["text"], skip_singles=_skip_1)
        except Exception:
            pass
        # 预取没在预算内取完：本次禁止绘制路径串行拉网（否则每个缺图又是 3s 超时），
        # 且本张是缺 emoji 的降级图，不入渲染缓存
        _draw_remote = ctx["emoji_remote_eff"] and _pf_complete
        # 绘制参数注入 ctx（ctx 每次调用都是新 dict：命中取 dict(_lhit) 副本、
        # 未命中建新 dict 后以 dict(ctx) 入缓存，注入不会污染 _LAYOUT_CACHE）
        ctx.update(
            star_background=star_background, star_density=star_density,
            mosaic_mode=mosaic_mode, mosaic_type=mosaic_type,
            violation_words=violation_words,
            emoji_remote_eff=_draw_remote, perf_out=perf_out,
        )
        pages = _split_content_pages(
            ctx["rendered_lines"], ctx["max_content"], ctx["font_scale"],
            violation_words=violation_words,
        )
        total = len(pages)
        if perf_out is not None:
            _t_draw = time.perf_counter()
        images = [
            cls._draw_page(ctx, pg, idx, total)
            for idx, pg in enumerate(pages)
        ]
        if perf_out is not None:
            try:
                perf_out["draw_ms"] = float(perf_out.get("draw_ms", 0.0)) + (time.perf_counter() - _t_draw) * 1000.0
            except Exception:
                pass
        # 仅缓存小体量结果，避免大长图堆内存；
        # 预算内没下完 emoji 时本次是降级图（缺 emoji 未补全），也不入缓存
        if cache_key is not None and len(images) <= 2 and _pf_complete:
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
    ) -> Image.Image:
        """绘制单页卡片（render_pages 与 render 多页首屏使用）。

        绘制参数（星空/打码/emoji/性能回填）全部取自 ctx，由
        _render_pages_inner 在排版后注入，避免逐层抄送十几个形参。
        """
        style = ctx["style"]
        if style == "dragon":
            dragon_bg = shared.get_dragon_template_bg()
            if dragon_bg is not None:
                return cls._draw_dragon_page(ctx, page_lines, page_idx, total_pages, dragon_bg)

        theme = ctx["theme"]
        text = ctx["text"]
        card_w = ctx["card_w"]
        header_h = ctx["header_h"]
        card_inner_pad_y = ctx["card_inner_pad_y"]
        footer_gap = ctx["footer_gap"]
        footer_block = ctx["footer_block"]
        inner_pad_x = ctx["inner_pad_x"]
        star_background = ctx["star_background"]
        star_density = ctx["star_density"]
        mosaic_mode = ctx["mosaic_mode"]
        mosaic_type = ctx["mosaic_type"]
        mosaic_half_pos = ctx["mosaic_half_pos"]
        violation_words = ctx["violation_words"]
        emoji_style = ctx["emoji_style"]
        emoji_remote_eff = ctx["emoji_remote_eff"]
        eff_theme_color = ctx.get("eff_theme_color", "")
        perf_out = ctx["perf_out"]
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

            def _q32(v: int) -> int:
                return (int(v) // 32) * 32

            try:
                # 几何量化键（±16px 桶）+ 去文本种子：跨消息/跨页共享星空层
                _skey = (_q32(canvas_w), _q32(canvas_h), _q32(card_w), _q32(card_h),
                         margin_x, margin_y, count, tuple(theme.star_colors))
            except Exception:
                _skey = None
            star_layer = _STAR_LAYER_CACHE.get(_skey) if _skey is not None else None
            if star_layer is not None and star_layer.size != (canvas_w, canvas_h):
                try:
                    star_layer = star_layer.resize((canvas_w, canvas_h), Image.BILINEAR)
                except Exception:
                    star_layer = None
            if star_layer is None:
                star_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
                s_draw = ImageDraw.Draw(star_layer)

                rng = random.Random(_stable_seed(str(_skey) if _skey is not None else "stars"))
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
                if _skey is not None:
                    try:
                        if len(_STAR_LAYER_CACHE) >= _STAR_LAYER_CACHE_LIMIT:
                            _STAR_LAYER_CACHE.pop(next(iter(_STAR_LAYER_CACHE)), None)
                        # 存量化桶原图（resize 前尺寸），命中不符再缩放
                        _STAR_LAYER_CACHE[_skey] = star_layer
                    except Exception:
                        pass

            canvas = Image.alpha_composite(canvas, star_layer)

        # 6. 轻量柔和卡片投影（同尺寸直接复用缓存）
        if style == "ios":
            corner_radius = 24
            card_border_w = 2
        else:
            corner_radius = 30
            card_border_w = 1
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
            width=card_border_w,
        )

        # 8. 绘制顶部栏（极简、纯粹，杜绝任何人工合成英文标签）
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
            # Android 原生 M3 胶囊微标志（使用克制中性色，避免左上角突兀主题色块）
            pill_w = 28
            pill_h = 4
            pill_y = cur_y + 10
            c_draw.rounded_rectangle(
                [inner_pad_x, pill_y, inner_pad_x + pill_w, pill_y + pill_h],
                radius=2,
                fill=_blend_rgb(theme.text_muted, theme.accent, 0.45) if eff_theme_color else theme.text_muted,
            )

            time_font = get_font(12, bold=False)
            time_str = time.strftime("%H:%M")
            t_w, _ = _text_size(time_font, time_str)
            c_draw.text((card_w - inner_pad_x - t_w, cur_y + 4), time_str, font=time_font, fill=theme.text_muted)

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
                # 前景色彩对齐：引导符号/项目圆点以主题色点缀
                m = _RE_BULLET.match(wrap_line)
                if m and (eff_theme_color or style == "android16"):
                    prefix = m.group(0)
                    rest = wrap_line[len(prefix):]
                    p_pos = _draw_mixed_text(card_surface, c_draw, content_x + 4, cur_y, prefix, font, theme.accent, block.font_size, emoji_remote_eff, emoji_style)
                    for px, pw, py, pc in p_pos:
                        all_char_positions.append((px, pw, py, pc, line_h))
                    prefix_w = sum(pw for _, pw, _, _ in p_pos) if p_pos else _text_size(font, prefix)[0]
                    r_pos = _draw_mixed_text(card_surface, c_draw, content_x + 4 + prefix_w, cur_y, rest, font, theme.text_primary, block.font_size, emoji_remote_eff, emoji_style)
                    for px, pw, py, pc in r_pos:
                        all_char_positions.append((px, pw, py, pc, line_h))
                else:
                    positions = _draw_mixed_text(card_surface, c_draw, content_x + 4, cur_y, wrap_line, font, theme.text_primary, block.font_size, emoji_remote_eff, emoji_style)
                    for px, pw, py, pc in positions:
                        all_char_positions.append((px, pw, py, pc, line_h))
            elif block.block_type.startswith("heading"):
                # 前景色彩对齐：设置主题色时所有标题对齐主题色体系，层级清晰
                if eff_theme_color:
                    if block.block_type == "heading1":
                        heading_color = theme.accent
                    elif block.block_type == "heading2":
                        heading_color = _blend_rgb(theme.text_primary, theme.accent, 0.72)
                    else:
                        heading_color = _blend_rgb(theme.text_primary, theme.accent, 0.48)
                elif style == "android16" and block.block_type == "heading1":
                    heading_color = theme.accent
                else:
                    heading_color = theme.text_primary
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
    def _draw_dragon_page(
        cls,
        ctx: Dict[str, Any],
        page_lines: List[Tuple[str, LineBlock, int]],
        page_idx: int,
        total_pages: int,
        dragon_base: Image.Image,
    ) -> Image.Image:
        """国风金龙模板单页绘制：完整金龙画幅、烈焰金色外发光字体、标准卡片尺寸输出"""
        theme = ctx["theme"]
        emoji_remote_eff = ctx["emoji_remote_eff"]
        emoji_style = ctx["emoji_style"]
        mosaic_mode = ctx["mosaic_mode"]
        mosaic_type = ctx["mosaic_type"]
        mosaic_half_pos = ctx["mosaic_half_pos"]
        violation_words = ctx["violation_words"]

        # 使用无损金龙全尺寸底板（2208 x 2080），双龙身姿浑然天成
        canvas = dragon_base.copy()
        w, h = canvas.size

        # 1. 计算文本总高度与最宽宽度
        text_h = sum(lh for _, _, lh in page_lines)
        max_w = 0.0
        for wrap_line, block, _ in page_lines:
            if wrap_line:
                font = get_font(block.font_size, bold=True)
                lw = _text_advance(font, wrap_line)
                if lw > max_w:
                    max_w = lw

        # 2. 水平与垂直在图一红框（x: 240..1968, y: 380..1820）内自适应居中平衡
        center_x = w // 2
        content_x = max(240, int(center_x - max_w / 2))

        # 垂直居中于可用高度（1440px）
        avail_box_h = 1440
        cur_y = int(380 + max(0, (avail_box_h - text_h) / 2))

        all_char_positions: List[Tuple[float, float, float, str, int]] = []
        draw = ImageDraw.Draw(canvas)

        # 3. 金色烈焰发光文字绘制辅助函数
        def _draw_glowing_line(tx, ty, line_str, font, text_color, font_size, glow_color=(255, 140, 30), glow_radius=4):
            # 辉光层：在透明层上绘制柔和金橙色轮廓并模糊
            glow_im = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow_im)
            gd.text((tx, ty), line_str, font=font, fill=(*glow_color, 200))
            blurred = glow_im.filter(ImageFilter.GaussianBlur(glow_radius))
            canvas.paste(blurred, (0, 0), blurred)
            # 主文字层：绘制清晰主体与全彩 Emoji
            return _draw_mixed_text(canvas, draw, tx, ty, line_str, font, text_color, font_size, emoji_remote_eff, emoji_style)

        # 4. 原汁原味排版，无任何多余伪标题，无任何右上角时间
        for wrap_line, block, line_h in page_lines:
            if block.block_type == "divider":
                div_y = cur_y + line_h // 2
                draw.line([(content_x, div_y), (min(1940, content_x + int(max_w) + 40), div_y)], fill=theme.divider, width=2)
                cur_y += line_h
                continue

            if not wrap_line:
                cur_y += int(line_h * 0.7)
                continue

            font = get_font(block.font_size, bold=True)
            if block.block_type == "code":
                draw.rounded_rectangle(
                    [content_x - 10, cur_y - 4, min(1940, content_x + int(max_w) + 20), cur_y + line_h + 4],
                    radius=8, fill=theme.code_bg, outline=theme.code_border, width=1,
                )
                positions = _draw_mixed_text(canvas, draw, content_x + 10, cur_y, wrap_line, font, theme.code_text, block.font_size, emoji_remote_eff, emoji_style)
            elif block.block_type == "quote":
                draw.rounded_rectangle([content_x, cur_y, content_x + 5, cur_y + line_h - 4], radius=3, fill=theme.accent)
                positions = _draw_glowing_line(content_x + 22, cur_y, wrap_line, font, theme.text_secondary, block.font_size, glow_color=(255, 120, 20), glow_radius=4)
            elif block.block_type.startswith("heading"):
                positions = _draw_glowing_line(content_x, cur_y, wrap_line, font, (255, 220, 90), block.font_size, glow_color=(255, 150, 40), glow_radius=6)
            else:
                positions = _draw_glowing_line(content_x, cur_y, wrap_line, font, (255, 252, 246), block.font_size, glow_color=(255, 130, 20), glow_radius=4)

            for px, pw, py, pc in positions:
                all_char_positions.append((px, pw, py, pc, line_h))

            cur_y += line_h

        # 5. 底栏水印：完全落在图二红框标记的最外层底边中央 (y: h - 55)
        wm_text = "by xbimg" + (f" · ({page_idx + 1}/{total_pages})" if total_pages > 1 else "")
        wm_font = get_font(32, bold=False)
        wm_w, _ = _text_size(wm_font, wm_text)
        wm_x = (w - wm_w) // 2
        wm_y = h - 55
        draw.text((wm_x, wm_y), wm_text, font=wm_font, fill=(210, 155, 75))

        # 6. 字级打码
        if mosaic_mode in ("half", "full") and all_char_positions:
            cls._apply_word_level_mosaic(
                card_surface=canvas,
                all_char_positions=all_char_positions,
                text=ctx["text"],
                violation_words=violation_words or [],
                mosaic_mode=mosaic_mode,
                mosaic_type=mosaic_type,
                theme=theme,
                card_w=canvas.width,
                mosaic_half_pos=mosaic_half_pos,
            )

        # 7. 尺寸适配：等比缩放到标准 680px 卡片宽度（高度约为 641px），完美对齐 iOS / Android 卡片规格！
        out_rgb = canvas.convert("RGB")
        target_w = 680
        target_h = int(round(target_w * h / w))
        return out_rgb.resize((target_w, target_h), Image.LANCZOS)

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
            # 违规词表非空但本图未命中任何违规字符：
            # 说明违规内容不在此图（分页拆词/关键词在其他页），本图无违规内容，
            # 直接跳过打码 —— 不做区域回退，否则小页会被整块马赛克覆盖。
            # violation_words 为空（AI 判定无具体词）的情况已在函数入口回退区域打码。
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


def preload_font_sizes() -> None:
    """预载常用字号字体（后台预热线程调用，保证首条消息 get_font 命中）。"""
    for size in (11, 12, 13, 16, 18, 20, 24, 25, 26, 28, 30, 32, 36, 40, 48):
        get_font(size, bold=False)
        get_font(size, bold=True)


def get_font_status() -> Dict[str, Any]:
    """供 WebUI / API 查询的字体状态（聚合字体层与 emoji 层的当前生效值）"""
    return {
        "has_cjk": bool(fonts._ACTIVE_REGULAR or fonts._FONT_REGULAR_PATH),
        "regular": fonts._ACTIVE_REGULAR or fonts._FONT_REGULAR_PATH,
        "bold": fonts._ACTIVE_BOLD or fonts._FONT_BOLD_PATH,
        "active_count": len(fonts._ACTIVE_ORDERED),
        "font_source": fonts._FONT_CONF.get("font_source", "auto"),
        "emoji_style": fonts._EMOJI_STYLE,
        "emoji_font": emoji._color_emoji_font_path(),
        "emoji_cached": len(emoji._EMOJI_IMG_CACHE),
        "emoji_storage_kb": emoji.get_emoji_storage_kb(),
        "data_dir": str((shared._FONT_DATA_DIR / shared.FONT_DATA_SUBDIR) if shared._FONT_DATA_DIR else ""),
    }
