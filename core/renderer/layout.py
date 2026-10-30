# -*- coding: utf-8 -*-
"""渲染引擎 · 排版层：Markdown 分块、emoji-aware 折行、按页高分页。"""
from dataclasses import dataclass
from typing import Dict, List, Tuple

from PIL import ImageFont

from .shared import _RE_BULLET, _RE_DIVIDER, _is_emoji_cluster, _take_cluster
from .fonts import _adv_text
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


# 单页内容高度上限（超出则分页输出多图，不再丢弃截断）；页数上限防病态超长 OOM
MAX_CONTENT_PAGES = 10

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


def _split_would_break_word(cur, nxt, violation_words) -> bool:
    """页尾恰好是某违规词的前缀、且下一行以该词余下部分开头 → 不在此切页。

    违规词的字级打码在单页内按整词匹配定位，跨页会两半都匹配不到而漏打。
    """
    if not violation_words:
        return False
    cur_tail = "".join(e[0] for e in cur)[-128:].lower()
    nxt_text = (nxt[0] or "").lower()
    if not cur_tail or not nxt_text:
        return False
    for kw in violation_words:
        k = (kw or "").strip().lower()
        if not k:
            continue
        for cut in range(1, len(k)):
            if cur_tail.endswith(k[:cut]) and nxt_text.startswith(k[cut:]):
                return True
    return False


def _split_content_pages(rendered_lines, max_content: float, font_scale: int = 100,
                         max_pages: int = MAX_CONTENT_PAGES, violation_words=None):
    """按单页高度上限切分排版行；非末页追加未完待续行，超页数上限末页追加截断行"""
    pages: List[List[Tuple[str, LineBlock, int]]] = []
    cur: List[Tuple[str, LineBlock, int]] = []
    cur_h = 0
    for entry in rendered_lines:
        _, _, lh = entry
        if cur and cur_h + lh > max_content:
            if _split_would_break_word(cur, entry, violation_words):
                pass  # 让违规词留在同一页，代价是本页略超高
            else:
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
