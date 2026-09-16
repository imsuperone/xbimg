# -*- coding: utf-8 -*-
"""
内容安全与合规审查模块 (Content Moderation Module)
支持：
- 自定义关键词/正则屏蔽词审查
- 接入 AI 大模型实时审查（支持 OpenAI/DeepSeek 兼容接口或 AstrBot 默认会话大模型）
- 违规判定与处置动作分发（打一半马赛克 / 全文打码 / 拦截阻断 / 合规通知）
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    from astrbot.api import logger
except Exception:
    import logging
    logger = logging.getLogger("msg2img")

# 预编译正则：关键词切分 / 混淆符压缩 / AI 返回 JSON 提取
_SPLIT_KW_RE = re.compile(r"[,;，；\n]+")
_CONDENSE_RE = re.compile(r"[\s\-_~`!@#$%^&*()+=|\\\[\]{};:'\",.<>?/]+")
_JSON_EXTRACT_RE = re.compile(r"\{[\s\S]*\}")
_STRIP_FENCE_HEAD_RE = re.compile(r"^```json\s*", re.IGNORECASE)
_STRIP_FENCE_TAIL_RE = re.compile(r"\s*```$")

DEFAULT_AI_SYS_PROMPT = (
    "你是一个严格而专业的内容安全审核员。请审查以下文本是否包含违法犯罪、色情低俗、恶意辱骂、暴恐危害、欺诈谣言等违规内容。\n"
    "请直接输出且仅输出合法的 JSON 格式，严禁添加任何 Markdown 格式或额外解释：\n"
    '{"violated": true 或 false, "reason": "违规简短原因，无违规填空字符串"}'
)


def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=8.0)
    return _shared_client


# 复用 HTTP 连接池：AI 审查高频调用不再每次新建 client
_shared_client: Optional[httpx.AsyncClient] = None

@dataclass
class ModerationResult:
    is_violated: bool = False
    reason: str = ""
    matched_keywords: List[str] = field(default_factory=list)
    action: str = "pass"  # "pass", "mosaic_half", "mosaic_full", "block", "notice"


class ContentModerator:
    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def _get_keywords(self) -> List[str]:
        raw = str(self.config.get("custom_keywords", "") or "")
        # 按逗号、分号、换行符分割
        tokens = [k.strip() for k in _SPLIT_KW_RE.split(raw) if k.strip()]
        return tokens

    def check_keywords(self, text: str) -> Tuple[bool, List[str]]:
        """检查自定义屏蔽词"""
        keywords = self._get_keywords()
        if not keywords or not text:
            return False, []

        matched = []
        lower_text = text.lower()
        # 清除空格与无意混淆字符，增强匹配度
        condensed_text = _CONDENSE_RE.sub("", lower_text)

        for kw in keywords:
            kw_clean = kw.strip().lower()
            if not kw_clean:
                continue
            if kw_clean in lower_text or kw_clean in condensed_text:
                matched.append(kw)

        return len(matched) > 0, matched

    async def check_ai(self, text: str, context: Optional[Any] = None) -> Tuple[bool, str]:
        """
        调用 AI 进行内容违规审查
        模式 astrbot：直接使用 AstrBot 已接入模型（可指定 ai_astrbot_model）
        模式 custom：使用自定义 OpenAI 兼容接口
        """
        provider_mode = str(self.config.get("ai_provider_mode", "astrbot") or "astrbot").strip().lower()
        if provider_mode not in ("astrbot", "custom"):
            provider_mode = "astrbot"
        astrbot_model = str(self.config.get("ai_astrbot_model", "") or "").strip()
        api_base = str(self.config.get("ai_api_base", "") or "").strip()
        api_key = str(self.config.get("ai_api_key", "") or "").strip()
        model = str(self.config.get("ai_model", "") or "gpt-4o-mini").strip()

        custom_prompt = str(self.config.get("custom_ai_prompt", "") or "").strip()
        sys_prompt = custom_prompt if custom_prompt else DEFAULT_AI_SYS_PROMPT
        user_prompt = f"待审核文本内容如下：\n\"\"\"\n{text[:1200]}\n\"\"\""

        # 1. 自定义模式：仅走外接 API
        if provider_mode == "custom":
            if not api_key:
                logger.warning("[msg2img] 自定义 AI 模式未配置 API Key")
                return False, ""
            if not api_base:
                api_base = "https://api.openai.com/v1"
            endpoint = api_base.rstrip("/") + "/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 150,
            }
            try:
                client = _get_shared_client()
                resp = await client.post(endpoint, headers=headers, json=body)
                if resp.status_code == 200:
                    data = resp.json()
                    reply_content = data["choices"][0]["message"]["content"].strip()
                    return self._parse_ai_decision(reply_content)
                else:
                    logger.warning(f"[msg2img] 自定义 AI 返回异常 {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                logger.warning(f"[msg2img] 自定义 AI 请求失败: {e}")
            return False, ""

        # 2. AstrBot 模式：使用已接入模型
        if context:
            try:
                provider = None
                # 若指定模型则尝试按模型名查找
                if astrbot_model:
                    try:
                        # 尝试常见接口：get_provider_by_model / get_provider
                        for attr in ("get_provider_by_model", "get_provider_by_id", "get_provider"):
                            fn = getattr(context, attr, None)
                            if callable(fn):
                                try:
                                    provider = fn(astrbot_model)
                                    if provider:
                                        break
                                except Exception:
                                    continue
                        # 兜底遍历已注册 providers
                        if not provider:
                            for cand_attr in ("providers", "llm_providers", "provider_manager"):
                                mgr = getattr(context, cand_attr, None)
                                if mgr and hasattr(mgr, "__iter__"):
                                    for p in mgr:
                                        pid = str(getattr(p, "model_name", "") or getattr(p, "id", "") or "")
                                        if pid and pid.lower() == astrbot_model.lower():
                                            provider = p
                                            break
                                if provider:
                                    break
                    except Exception:
                        pass
                if not provider:
                    provider = context.get_using_provider()
                if provider and hasattr(provider, "text_chat"):
                    res = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=user_prompt,
                            system_prompt=sys_prompt,
                            temperature=0.1,
                            max_tokens=150,
                        ),
                        timeout=8.0,
                    )
                    content = getattr(res, "completion_text", "") or str(res)
                    return self._parse_ai_decision(content)
            except Exception as e:
                logger.warning(f"[msg2img] 调用 AstrBot 默认大模型审查异常: {e}")

        return False, ""

    @staticmethod
    def _parse_ai_decision(raw_reply: str) -> Tuple[bool, str]:
        """解析 AI 返回的审核 JSON 结果"""
        try:
            # 剥离可能出现的 Markdown 代码块包裹
            cleaned = _STRIP_FENCE_HEAD_RE.sub("", raw_reply.strip())
            cleaned = _STRIP_FENCE_TAIL_RE.sub("", cleaned.strip())
            # 正则提取首个匹配的大括号
            match = _JSON_EXTRACT_RE.search(cleaned)
            if match:
                cleaned = match.group()
            obj = json.loads(cleaned)
            is_violated = bool(obj.get("violated", False))
            reason = str(obj.get("reason", "") or "").strip()
            return is_violated, reason
        except Exception:
            # 如果不是标准 JSON，做基础意图推断（大小写不敏感）
            low = raw_reply.lower()
            if any(w in low for w in ["true", "违规", "不合规", "敏感", "禁止"]):
                return True, "AI 判定可能违规"
            return False, ""

    async def review(self, text: str, context: Optional[Any] = None) -> ModerationResult:
        """
        全流程审查统一入口
        """
        mode = self.config.get("moderation_mode", "keywords")
        action_pref = self.config.get("violation_action", "mosaic_half")

        if mode == "none" or not text.strip():
            return ModerationResult(is_violated=False, action="pass")

        violated = False
        reason = ""
        matched_kw: List[str] = []

        # 1. 关键词审查
        if mode in ("keywords", "both"):
            kw_hit, matched_kw = self.check_keywords(text)
            if kw_hit:
                violated = True
                reason = f"触发敏感屏蔽词: {', '.join(matched_kw[:5])}"

        # 2. AI 审查（必须开启 enable_ai_moderation 开关或模式设为 ai/both，且关键词未命中时才调用）
        ai_enabled = bool(self.config.get("enable_ai_moderation", False)) or (mode in ("ai", "both"))
        if "enable_ai_moderation" in self.config:
            ai_enabled = bool(self.config.get("enable_ai_moderation", False))
        if not violated and ai_enabled and mode != "none":
            ai_hit, ai_reason = await self.check_ai(text, context)
            if ai_hit:
                violated = True
                reason = f"AI 安全审核拦截: {ai_reason or '检测到违规涉敏内容'}"

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
