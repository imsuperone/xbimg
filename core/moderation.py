# -*- coding: utf-8 -*-
"""
内容安全与合规审查模块 (Content Moderation Module)
支持：
- 自定义关键词/正则屏蔽词审查
- 违规判定与处置动作分发（打一半马赛克 / 全文打码 / 拦截阻断 / 合规通知）
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 预编译正则：关键词切分 / 混淆符压缩（后者与打码侧共用 common 定义）
_SPLIT_KW_RE = re.compile(r"[,;，；\n]+")
try:
    from .common import _CONDENSE_CONFUSABLES_RE as _CONDENSE_RE
except (ImportError, ValueError):
    from core.common import _CONDENSE_CONFUSABLES_RE as _CONDENSE_RE


@dataclass
class ModerationResult:
    is_violated: bool = False
    reason: str = ""
    matched_keywords: List[str] = field(default_factory=list)
    action: str = "pass"  # "pass", "mosaic_half", "mosaic_full", "block", "notice"


class ContentModerator:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        # 关键词解析缓存：raw 字符串 -> (tokens, lowered)。配置保存时重建 Moderator，天然失效
        self._kw_cache: Dict[str, Tuple[List[str], List[str]]] = {}
        # 关键词正则预检缓存：raw -> compiled pattern（普通消息一次 search 即可排除，避免逐词 in）
        self._kw_pat_cache: Dict[str, Any] = {}

    def _resolve_kw_raw(self, preset_name: Optional[str] = None) -> str:
        """解析词库 raw 字符串（纯函数，无副作用，调用方线程本地持有）"""
        raw = ""
        presets = self.config.get("keyword_presets", {})
        if not isinstance(presets, dict):
            presets = {}

        # 1. 若指定群预设名，优先获取该预设
        if preset_name and preset_name in presets:
            item = presets[preset_name]
            raw = item.get("keywords", "") if isinstance(item, dict) else str(item)
        else:
            # 未指定或指定了未知预设名时，回退到激活预设合并：
            # 未知名绝不静默降级为裸 custom_keywords（会丢掉激活预设词）
            active_p = str(self.config.get("active_keyword_preset", "default") or "default")
            preset_raw = ""
            if active_p in presets:
                item = presets[active_p]
                preset_raw = item.get("keywords", "") if isinstance(item, dict) else str(item)
            custom_raw = str(self.config.get("custom_keywords", "") or "")
            raw = f"{custom_raw},{preset_raw}" if (preset_raw and custom_raw != preset_raw) else (custom_raw or preset_raw)

        # 兜底
        if not raw:
            raw = str(self.config.get("custom_keywords", "") or "")
        return raw

    def _get_keywords_with_lowered(self, preset_name: Optional[str] = None) -> Tuple[List[str], List[str], str]:
        raw = self._resolve_kw_raw(preset_name)

        # 按逗号、分号、换行符分割并去重（按 raw 字符串缓存：同词库不重复切分/lower）
        cached = self._kw_cache.get(raw)
        if cached is not None:
            return list(cached[0]), list(cached[1]), raw
        seen = set()
        tokens = []
        lowered = []
        for k in _SPLIT_KW_RE.split(raw):
            token = k.strip()
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
                lowered.append(token.lower())
        # 有界缓存：防止词库被频繁改动时内存膨胀
        if len(self._kw_cache) > 32:
            self._kw_cache.clear()
            self._kw_pat_cache.clear()
        self._kw_cache[raw] = (tokens, lowered)
        # 编译正则预检模式（按长度降序，命中优先长词；超大词库跳过防灾难回溯）
        try:
            if 0 < len(lowered) <= 2000:
                total_len = sum(len(w) for w in lowered)
                if total_len <= 30000:
                    alt = "|".join(sorted((re.escape(w) for w in lowered if w), key=len, reverse=True))
                    if alt:
                        self._kw_pat_cache[raw] = re.compile(alt)
        except Exception:
            pass
        return list(tokens), list(lowered), raw

    def check_keywords(self, text: str, preset_name: Optional[str] = None) -> Tuple[bool, List[str]]:
        """检查自定义屏蔽词（游戏词同样视为违规保留）"""
        keywords, lowered_kws, raw = self._get_keywords_with_lowered(preset_name)
        if not keywords or not text:
            return False, []

        lower_text = text.lower()
        # 正则预检：普通消息一次 search 排除，避免逐词 in（命中时再逐词收集明细）
        try:
            pat = self._kw_pat_cache.get(raw)
        except Exception:
            pat = None
        if pat is not None:
            try:
                if pat.search(lower_text):
                    pass
                else:
                    _cond = _CONDENSE_RE.sub("", lower_text)
                    if not pat.search(_cond):
                        return False, []
            except Exception:
                pass

        matched = []
        # 清除空格与无意混淆字符，增强匹配度
        condensed_text = _CONDENSE_RE.sub("", lower_text)

        for kw, kw_clean in zip(keywords, lowered_kws):
            if not kw_clean:
                continue
            if kw_clean in lower_text or kw_clean in condensed_text:
                matched.append(kw)

        return len(matched) > 0, matched

    async def review(self, text: str, context: Optional[Any] = None, preset_name: Optional[str] = None) -> ModerationResult:
        """
        全流程审查统一入口
        """
        mode = self.config.get("moderation_mode", "keywords")
        action_pref = self.config.get("violation_action", "mosaic_half")
        kw_enabled = bool(self.config.get("enable_keywords_moderation", True)) and (mode != "none")

        if not text.strip():
            return ModerationResult(is_violated=False, action="pass")

        violated = False
        reason = ""
        matched_kw: List[str] = []

        # 1. 关键词审查（both/always 为历史脏值，按 keywords 处理，绝不静默旁路）
        if kw_enabled and mode in ("keywords", "both", "always"):
            kw_hit, matched_kw = self.check_keywords(text, preset_name)
            if kw_hit:
                violated = True
                reason = f"触发敏感屏蔽词: {', '.join(matched_kw[:5])}"

        if not violated:
            return ModerationResult(is_violated=False, action="pass")

        # 映射最终处置动作
        final_action = action_pref if action_pref in ("mosaic_half", "mosaic_full", "block", "notice") else "mosaic_half"
        return ModerationResult(
            is_violated=True,
            reason=reason,
            matched_keywords=matched_kw,
            action=final_action,
        )
