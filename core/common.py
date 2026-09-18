# -*- coding: utf-8 -*-
"""
消息转图助手插件 (astrbot_plugin_msg2img)
- 智能将机器人发送的纯文本消息转为高颜值图片
- 支持 iOS 与 Android 16 (Material 3 Expressive) 双风格
- 随机背景小星星星芒点缀
- 链接转图/保留文本策略
- 自定义关键词 + AI 大模型内容安全双轨审查
- 违规创意半马赛克（Half Mosaic）处置
- Android 16 WebUI 管理控制台
"""

import asyncio
import base64
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

try:
    from astrbot.api import logger
except Exception:
    import logging
    logger = logging.getLogger("msg2img")

try:
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star
except Exception:
    class Star:
        def __init__(self, context=None):
            self.context = context
    class Context:
        pass
    class AstrMessageEvent:
        pass
    class _Filter:
        class EventMessageType:
            ALL = 1
            GROUP_MESSAGE = 2
            PRIVATE_MESSAGE = 3
        @staticmethod
        def on_decorating_result(*args, **kwargs):
            return lambda fn: fn
        @staticmethod
        def command(*args, **kwargs):
            return lambda fn: fn
        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda fn: fn
    filter = _Filter()

try:
    from astrbot.api.message_components import Image as AstrImage, Plain
except Exception:
    try:
        from astrbot.core.message.components import Image as AstrImage, Plain
    except Exception:
        class Plain:
            def __init__(self, text=""):
                self.text = text
        class AstrImage:
            @classmethod
            def fromFileSystem(cls, path):
                return cls(path)
            def __init__(self, path=""):
                self.path = path

try:
    from astrbot.api.web import error_response, json_response, request
    _HAS_WEB_API = True
except Exception:
    _HAS_WEB_API = False
    json_response = None  # type: ignore
    error_response = None  # type: ignore
    request = None  # type: ignore

try:
    from starlette.responses import Response as FileResponse
except Exception:
    FileResponse = None  # type: ignore

try:
    from .config import PLUGIN_NAME
except (ImportError, ValueError):
    from core.config import PLUGIN_NAME


# 匹配 URL 链接的正则表达式（预编译，避免每条消息重复编译）
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'\u4e00-\u9fa5]+")
# 群号切分 / 非数字清洗（预编译）
_SPLIT_GROUP_RE = re.compile(r"[,;，；\n\s]+")
_NON_DIGIT_RE = re.compile(r"\D")
# URL 尾部误吞的标点（中文标点 + 英文标点 + 括号）
_URL_TRAILING_PUNCT = ".,;:!?)]}'\"，。；：！？、」』】）"


def _clean_urls(urls: List[str]) -> List[str]:
    """剥离 URL 尾部误吞的标点"""
    out = []
    for u in urls:
        u = u.rstrip(_URL_TRAILING_PUNCT)
        if u:
            out.append(u)
    return out


# 输出体积档位：lossless / balanced / compact（中文为输入别名）
_COMPRESS_CANON = {
    "lossless": "lossless", "balanced": "balanced", "compact": "compact",
    "无损": "lossless", "原画": "lossless", "png": "lossless",
    "均衡": "balanced", "标准": "balanced",
    "省流": "compact", "紧凑": "compact", "极小": "compact",
}


def _compress_level(cfg) -> str:
    """归一化体积档位，老配置无缝迁移"""
    try:
        v = str(cfg.get("img_compress_level", "balanced") or "balanced").strip().lower()
    except Exception:
        v = "balanced"
    return _COMPRESS_CANON.get(v, "balanced")


# 跨消息落盘限流槽：loop 变化时重建（asyncio.run 多 loop / 热重载安全）
_SAVE_SEM_SLOT: Dict[str, Any] = {"loop": None, "sem": None}

# 渲染侧输入上限（字符数）：只截渲染不截审查，防超长文本 DoS
RENDER_MAX_CHARS = 12000


def _save_semaphore() -> "asyncio.Semaphore":
    """返回跨消息共享的落盘信号量（4 并发），首次在 running loop 内创建"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    sem = _SAVE_SEM_SLOT.get("sem")
    if sem is None or (loop is not None and _SAVE_SEM_SLOT.get("loop") is not loop):
        sem = asyncio.Semaphore(4)
        _SAVE_SEM_SLOT["loop"] = loop
        _SAVE_SEM_SLOT["sem"] = sem
    return sem
