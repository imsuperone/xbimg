# -*- coding: utf-8 -*-
"""
消息转图助手插件 (astrbot_plugin_xbimg)
- 智能将机器人发送的纯文本消息转为高颜值图片
- 支持 iOS 与 Android 16 (Material 3 Expressive) 双风格
- 随机背景小星星星芒点缀
- 链接转图/保留文本策略
- 自定义关键词 + AI 大模型内容安全双轨审查
- 违规创意半马赛克（Half Mosaic）处置
- Android 16 WebUI 管理控制台
-
- 文件结构：编排层（本文件）+ core/common（垫片/常量）
-   + core/groups（群逻辑）+ core/handlers（渲染管线/钩子）
-   + core/commands（/xbimg）+ core/webapi（WebUI 接口）
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .core.common import (
        AstrMessageEvent, Context, Star, PLUGIN_NAME, _HAS_WEB_API, filter, logger,
        RENDER_MAX_CHARS, _clean_urls, _compress_level, _save_semaphore,
    )
    from .core.config import ConfigManager, PLUGIN_NAME as _PN
    from .core.moderation import ContentModerator
    from .core.renderer import MessageImageRenderer, configure_fonts, needs_cjk_download
    from .core.groups import GroupsMixin
    from .core.handlers import HandlersMixin
    from .core.commands import CommandsMixin
    from .core.webapi import WebApiMixin
except (ImportError, ValueError):
    import sys as _sys
    _plugin_dir = str(Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
    from core.common import (
        AstrMessageEvent, Context, Star, PLUGIN_NAME, _HAS_WEB_API, filter, logger,
        RENDER_MAX_CHARS, _clean_urls, _compress_level, _save_semaphore,
    )
    from core.config import ConfigManager, PLUGIN_NAME as _PN
    from core.moderation import ContentModerator
    from core.renderer import MessageImageRenderer, configure_fonts, needs_cjk_download
    from core.groups import GroupsMixin
    from core.handlers import HandlersMixin
    from core.commands import CommandsMixin
    from core.webapi import WebApiMixin

assert PLUGIN_NAME == _PN


class Msg2ImgPlugin(Star, GroupsMixin, HandlersMixin, CommandsMixin, WebApiMixin):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.cfg_mgr = ConfigManager(config)
        self.moderator = ContentModerator(self.cfg_mgr.config)
        self.cache_dir = self.cfg_mgr.cache_dir
        self._seen_groups: Dict[str, Dict[str, Any]] = {}
        # 预览图 nonce 锁：nonce -> 过期时间戳（10 分钟），防 preview_img 被任意人直读
        self._preview_nonces: Dict[str, float] = {}
        # 测试指令冷却：name:gid -> 上次时间戳，防任意成员刷测试图
        self._test_cooldowns: Dict[str, float] = {}
        # emoji 样式代际：每次用户显式改样式/删除即 +1，旧下载任务完成不再强行切回
        self._emoji_style_gen: int = 0
        # 后台任务状态（__init__ 时往往没有 running loop，改为首次事件时懒启动）
        self._bg_started = False
        self._cache_stop_event: Optional[asyncio.Event] = None
        self._bg_tasks: List[asyncio.Task] = []
        # 群白名单解析缓存：(mode, raw_list) -> tokens
        self._group_cache_sig: Optional[tuple] = None
        self._group_cache_tokens: List[str] = []
        self._group_cache_cleaned: List[str] = []
        # Bot 实例缓存（避免每次全量 dir() 遍历）
        self._bots_cache: List[Any] = []
        self._bots_cache_time: float = 0.0
        # 字体补齐任务状态
        self._fonts_ensured = False

        # 同步字体/emoji 配置（自定义字体、持久化目录下载字体即时生效）
        try:
            configure_fonts(self.cfg_mgr.config, self.cfg_mgr.data_dir)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 字体配置初始化异常: {e}")

        if _HAS_WEB_API:
            try:
                self._register_web_apis()
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 注册 Web API 异常: {e}")

        # 开关确认行：perf_log 开启时打一行，关着则零日志（用户可据此确认开关是否真正生效）
        try:
            _pv = self.cfg_mgr.config.get("perf_log", False)
            _on = _pv is True or _pv == 1 or str(_pv).strip().lower() == "true"
        except Exception:
            _on = False
        if _on:
            logger.info("[xbimg] [性能] 性能日志已开启，每张图片一行分段计时（grep [性能] 捞取）")



    def _ensure_bg_tasks(self):
        """首次事件触发时启动后台任务（兼容 __init__ 无 running loop 的情况）"""
        if self._bg_started:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._bg_started = True
        self._cache_stop_event = asyncio.Event()
        self._bg_tasks = [
            asyncio.create_task(self._clean_old_cache_periodic()),
            asyncio.create_task(self._hook_adapters_when_ready()),
            asyncio.create_task(self._ensure_fonts_once()),
            asyncio.create_task(self._prewarm_render()),
        ]



    async def _prewarm_render(self):
        """后台预热一次渲染（字体/字形/度量缓存就绪），首条真实消息不再付冷启动费"""
        try:
            await asyncio.sleep(3)
            if self._cache_stop_event is not None and self._cache_stop_event.is_set():
                return
            await asyncio.to_thread(
                MessageImageRenderer.render_pages,
                # 带 3 个高频单字：顺手暖 emoji 通道（PNG/字体/cmap/覆盖判定），
                # 首条真实消息不再付冷启动；后台线程执行，不阻塞事件循环
                text="预热Abc123中文测试💰✨✅",
                style=str(self.cfg_mgr.config.get("style", "ios")),
                theme_mode=str(self.cfg_mgr.config.get("theme_mode", "light")),
                star_background=False,
                emoji_remote=True,
            )
        except Exception:
            pass



    async def terminate(self):
        """插件卸载/停用时回收后台任务、还原适配器补丁，避免重载后旧闭包残留导致新配置不生效"""
        try:
            if self._cache_stop_event is not None:
                self._cache_stop_event.set()
        except Exception:
            pass
        for t in list(getattr(self, "_bg_tasks", []) or []):
            try:
                if not t.done():
                    t.cancel()
            except Exception:
                pass
        self._bg_started = False
        self._bg_tasks = []
        # 还原适配器补丁：新实例重载后才能重新挂载（旧闭包捕获已销毁实例）
        try:
            for bot in self._find_all_bots(max_age=0) or []:
                try:
                    self._unpatch_bot_send(bot)
                except Exception:
                    continue
        except Exception:
            pass
        # 关闭 AI 审查共享连接池
        try:
            from .core.moderation import close_shared_client
            await close_shared_client()
        except Exception:
            try:
                from core.moderation import close_shared_client as _csc2
                await _csc2()
            except Exception:
                pass



    async def _ensure_fonts_once(self):
        """首次进入仅引导，不自动下载（按用户要求默认不下载）"""
        if self._fonts_ensured:
            return
        self._fonts_ensured = True
        # 仅检测并日志提示，不自动下载；由 WebUI 引导用户手动选择
        try:
            if needs_cjk_download():
                logger.info(f"[{PLUGIN_NAME}] 未检测到可用中文字体，请在 WebUI 精选字体中选择下载")
        except Exception:
            pass
        try:
            _es_bg = str(self.cfg_mgr.config.get("emoji_style", "none") or "none").lower()
            if _es_bg not in ("none", "ios", "android", "windows"):
                _es_bg = "none"
            if _es_bg == "none":
                logger.info(f"[{PLUGIN_NAME}] Emoji 未启用，首次进入请在 WebUI 选择样式")
        except Exception:
            pass


    # ---- AstrBot 注册包装（装饰器必须作用于最终 Star 子类方法，逻辑在各 Mixin） ----
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_all_message_entry(self, event: AstrMessageEvent):
        await super().on_all_message_entry(event)

    @filter.on_decorating_result(priority=99999)
    async def on_decorating_result(self, event: AstrMessageEvent):
        await super().on_decorating_result(event)

    @filter.command("xbimg")
    async def cmd_xbimg(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        try:
            event._xbimg_cmd_reply = True
        except Exception:
            pass
        async for item in super().cmd_xbimg(event, sub=sub, arg=arg):
            yield item

__all__ = ["Msg2ImgPlugin"]
