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
    from .core.config import ConfigManager, PLUGIN_NAME
    from .core.moderation import ContentModerator
    from .core.renderer import MessageImageRenderer
    from .core.renderer import configure_fonts, download_missing_fonts, get_font_status, needs_cjk_download
    from .core.renderer import ensure_emoji_assets, get_curated_fonts_status, download_curated_font, delete_curated_font, get_emoji_packs_status, download_emoji_pack, delete_emoji_pack
except (ImportError, ValueError):
    import sys as _sys
    _plugin_dir = str(Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
    from core.config import ConfigManager, PLUGIN_NAME
    from core.moderation import ContentModerator
    from core.renderer import MessageImageRenderer
    from core.renderer import configure_fonts, download_missing_fonts, get_font_status, needs_cjk_download
    from core.renderer import ensure_emoji_assets, get_curated_fonts_status, download_curated_font, delete_curated_font, get_emoji_packs_status, download_emoji_pack, delete_emoji_pack

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


class Msg2ImgPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.cfg_mgr = ConfigManager(config)
        self.moderator = ContentModerator(self.cfg_mgr.config)
        self.cache_dir = self.cfg_mgr.cache_dir
        self._seen_groups: Dict[str, Dict[str, Any]] = {}
        # 后台任务状态（__init__ 时往往没有 running loop，改为首次事件时懒启动）
        self._bg_started = False
        self._cache_stop_event: Optional[asyncio.Event] = None
        # 群白名单解析缓存：(mode, raw_list) -> tokens
        self._group_cache_sig: Optional[tuple] = None
        self._group_cache_tokens: List[str] = []
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
        asyncio.create_task(self._clean_old_cache_periodic())
        asyncio.create_task(self._hook_adapters_when_ready())
        asyncio.create_task(self._ensure_fonts_once())

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

    def _remember_group(self, event: AstrMessageEvent):
        """记录已见群聊（入口与过滤两处共用，消灭重复代码）"""
        gid = self._extract_group_id(event)
        if not gid:
            return gid
        try:
            grp_name = ""
            grp = getattr(getattr(event, "message_obj", None), "group", None)
            if grp:
                grp_name = str(getattr(grp, "group_name", "") or "")
            self._seen_groups[gid] = {"gid": gid, "group_name": grp_name or gid}
        except Exception:
            pass
        return gid

    # ==========================================
    # 消息入口优先拦截（针对 xbbot 等由 @event_message_type 驱动的业务插件）
    # 设置高优先级 priority=100，确保在其它插件之前检查并打标
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_all_message_entry(self, event: AstrMessageEvent):
        # 懒启动后台任务 + 记录已见群聊
        self._ensure_bg_tasks()
        self._remember_group(event)

    async def _hook_adapters_when_ready(self):
        """等待 AstrBot 平台适配器就绪后，无损挂载 send 拦截钩子"""
        for _ in range(15):
            await asyncio.sleep(2)
            try:
                # 重试循环内强制新鲜扫描（不用 60s 缓存，否则首次空结果会堵住后续重试）
                bots = self._find_all_bots(max_age=0)
                if bots:
                    for b in bots:
                        self._patch_bot_send(b)
                    break
            except Exception:
                pass

    def _patch_bot_send(self, bot_inst: Any):
        """为 OneBot/aiocqhttp 等适配器实例挂载无损转图补丁"""
        if getattr(bot_inst, "_msg2img_patched", False):
            return

        # 1. 拦截 bot.send_group_msg
        if hasattr(bot_inst, "send_group_msg") and callable(getattr(bot_inst, "send_group_msg")):
            orig_send_group = bot_inst.send_group_msg

            async def patched_send_group(group_id, message, **kwargs):
                try:
                    message = await self._transform_onebot_message(str(group_id), message)
                except Exception as e:
                    logger.debug(f"[{PLUGIN_NAME}] 拦截 send_group_msg 失败: {e}")
                return await orig_send_group(group_id=group_id, message=message, **kwargs)

            bot_inst.send_group_msg = patched_send_group

        # 2. 拦截通用 call_action("send_group_msg", ...) 或 call_action("send_msg", ...)
        if hasattr(bot_inst, "call_action") and callable(getattr(bot_inst, "call_action")):
            orig_call_action = bot_inst.call_action

            async def patched_call_action(action, **kwargs):
                try:
                    if action in ("send_group_msg", "send_msg"):
                        gid = kwargs.get("group_id")
                        msg = kwargs.get("message")
                        if gid and msg is not None:
                            kwargs["message"] = await self._transform_onebot_message(str(gid), msg)
                except Exception as e:
                    logger.debug(f"[{PLUGIN_NAME}] 拦截 call_action 失败: {e}")
                return await orig_call_action(action, **kwargs)

            bot_inst.call_action = patched_call_action

        setattr(bot_inst, "_msg2img_patched", True)
        logger.info(f"[{PLUGIN_NAME}] 成功挂载适配器群消息转图拦截钩子 (send_group_msg & call_action)")

    async def _review_and_render(
        self,
        full_text: str,
        font_scale: Optional[int] = None,
        style: Optional[str] = None,
        theme_mode: Optional[str] = None,
        keyword_preset: Optional[str] = None,
    ):
        """统一审查+渲染管线：返回 (Image|None, violated, mosaic, blocked)。

        主路径与适配器劫持路径共用，消灭重复代码，保证统计口径一致。
        支持传入群级别覆盖的 style / theme_mode / font_scale / keyword_preset。
        """
        cfg = self.cfg_mgr.config
        if font_scale is None:
            try:
                font_scale = int(cfg.get("font_scale", 100) or 100)
            except Exception:
                font_scale = 100
        eff_style = str(style or cfg.get("style", "ios")).lower()
        eff_theme = str(theme_mode or cfg.get("theme_mode", "light")).lower()

        mod_res = await self.moderator.review(full_text, self.context, preset_name=keyword_preset)
        if mod_res.is_violated and mod_res.action == "block":
            return None, True, False, True
        mosaic_mode = "none"
        if mod_res.is_violated:
            if mod_res.action == "notice":
                full_text = f"⚠️【内容安全提示】\n原消息触发安全审核：{mod_res.reason}。\n根据群聊合规要求，此条回复已被过滤拦截。"
            elif mod_res.action == "mosaic_half":
                mosaic_mode = "half"
            elif mod_res.action == "mosaic_full":
                mosaic_mode = "full"
        try:
            # 兼容 emoji_style / emoji_remote
            _es = str(cfg.get("emoji_style", "") or "").strip().lower()
            if _es not in ("none", "ios", "android", "windows"):
                _es = "android" if bool(cfg.get("emoji_remote", True)) else "none"
                if _es == "android" and not str(cfg.get("emoji_style", "")):
                    _es = "none"
            img = await asyncio.to_thread(
                MessageImageRenderer.render,
                text=full_text,
                style=eff_style,
                theme_mode=eff_theme,
                star_background=bool(cfg.get("star_background", True)),
                star_density=str(cfg.get("star_density", "medium")),
                mosaic_mode=mosaic_mode,
                mosaic_type=str(cfg.get("mosaic_type", "pixel")),
                violation_words=mod_res.matched_keywords if mod_res.is_violated else [],
                emoji_remote=_es != "none",
                mosaic_half_pos=str(cfg.get("mosaic_half_pos", "bottom")),
                font_scale=font_scale,
                emoji_style=_es,
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 渲染失败: {e}")
            return None, mod_res.is_violated, mosaic_mode != "none", False
        return img, mod_res.is_violated, mosaic_mode != "none", False

    async def _save_render_image(self, img) -> Optional[Path]:
        """保存渲染图到缓存并返回路径（按三档力度压缩，用完即删：45 秒后自动清理）"""
        cfg = self.cfg_mgr.config
        lvl = str(cfg.get("img_compress_level", "medium") or "medium").lower()

        # 三档压缩策略（全部确保清晰阅读，仅在体积与无损之间平衡，4:4:4 无色度抽样噪点，极速编码）
        if lvl == "high":
            # 极小文件：高质量紧凑 JPEG (Q86) + 4:4:4 无抽样，体积极小无噪点
            img_filename = f"t2i_{int(time.time() * 1000)}_{os.urandom(3).hex()}.jpg"
            img_path = self.cache_dir / img_filename
            save_img = img.convert("RGB") if img.mode != "RGB" else img
            save_kwargs = {"format": "JPEG", "quality": 86, "subsampling": 0}
        elif lvl == "low":
            # 原画无损：无损 PNG，低压缩比最高画质与极快保存
            img_filename = f"t2i_{int(time.time() * 1000)}_{os.urandom(3).hex()}.png"
            img_path = self.cache_dir / img_filename
            save_img = img
            save_kwargs = {"format": "PNG", "compress_level": 1}
        else:
            # 均衡适中：超清 JPEG (Q94)，4:4:4 锐利无杂色，毫秒级快速生图
            img_filename = f"t2i_{int(time.time() * 1000)}_{os.urandom(3).hex()}.jpg"
            img_path = self.cache_dir / img_filename
            save_img = img.convert("RGB") if img.mode != "RGB" else img
            save_kwargs = {"format": "JPEG", "quality": 94, "subsampling": 0}

        try:
            await asyncio.to_thread(save_img.save, str(img_path), **save_kwargs)
            # 即时清理：45 秒后删除，避免堆积
            self._schedule_delete(img_path, 45)
            return img_path
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 写入图片失败: {e}")
            return None

    async def _transform_onebot_message(self, gid: str, message: Any) -> Any:
        """将 OneBot 协议格式的文本消息转图（与主路径同策略：链接模式/审查/统计）"""
        cfg = self.cfg_mgr.config
        if not bool(cfg.get("enable", True)):
            return message

        # 检查该群聊是否在白名单中
        if not self._is_gid_allowed(gid):
            return message

        if isinstance(message, list):
            # 提取纯文本
            plain_texts = []
            has_media = False
            for seg in message:
                if isinstance(seg, dict):
                    st = seg.get("type")
                    if st == "text":
                        plain_texts.append(seg.get("data", {}).get("text", ""))
                    elif st in ("image", "record", "video", "file"):
                        has_media = True

            full_text = "".join(plain_texts).strip()
            if not full_text or (has_media and len(full_text) < 10):
                return message

            min_threshold = int(cfg.get("min_length_threshold", 1) or 1)
            if len(full_text) < min_threshold:
                return message

            # 链接策略与主路径保持一致
            if _clean_urls(_URL_PATTERN.findall(full_text)):
                if str(cfg.get("link_mode", "as_image") or "as_image") == "keep_text":
                    return message

            # 仅违规时转图：非违规直接保持纯文本
            render_trigger = str(cfg.get("render_trigger", "always") or "always")
            if render_trigger == "violation_only":
                quick_hit, _ = self.moderator.check_keywords(full_text)
                # 快速关键词未命中且配置为仅关键词/AI 单项时，再做一次完整审查避免误放；已命中则直接走完整流程
                if not quick_hit and str(cfg.get("moderation_mode", "keywords")) == "none":
                    return message

            # 转为图片（群组单独字体大小与样式）
            grp_custom = self._get_group_custom_config(gid)
            eff_scale = self._get_group_font_scale(gid)
            img, violated, mosaic, blocked = await self._review_and_render(
                full_text,
                font_scale=eff_scale,
                style=grp_custom.get("style"),
                theme_mode=grp_custom.get("theme_mode"),
                keyword_preset=grp_custom.get("keyword_preset"),
            )
            # 仅违规触发：无违规则不转图（保留纯文本）
            if render_trigger == "violation_only" and not violated:
                return message
            self.cfg_mgr.record_render(is_violated=violated, is_mosaic=mosaic)
            if blocked or img is None:
                return message
            img_path = await self._save_render_image(img)
            if not img_path:
                return message

            # 保留前置 at / reply（与主路径一致，不再丢弃 Reply）
            new_segs = [
                seg for seg in message
                if isinstance(seg, dict) and seg.get("type") in ("at", "reply")
            ]
            new_segs.append({"type": "image", "data": {"file": str(img_path.resolve())}})
            return new_segs

        elif isinstance(message, str) and message.strip():
            # 纯文本字符串
            text = message.strip()
            min_threshold = int(cfg.get("min_length_threshold", 1) or 1)
            if len(text) < min_threshold:
                return message
            if _clean_urls(_URL_PATTERN.findall(text)):
                if str(cfg.get("link_mode", "as_image") or "as_image") == "keep_text":
                    return message
            render_trigger2 = str(cfg.get("render_trigger", "always") or "always")
            if render_trigger2 == "violation_only":
                quick_hit2, _ = self.moderator.check_keywords(text)
                if not quick_hit2 and str(cfg.get("moderation_mode", "keywords")) == "none":
                    return message
            grp_custom2 = self._get_group_custom_config(gid)
            eff_scale2 = self._get_group_font_scale(gid)
            img, violated, mosaic, blocked = await self._review_and_render(
                text,
                font_scale=eff_scale2,
                style=grp_custom2.get("style"),
                theme_mode=grp_custom2.get("theme_mode"),
                keyword_preset=grp_custom2.get("keyword_preset"),
            )
            if render_trigger2 == "violation_only" and not violated:
                return message
            self.cfg_mgr.record_render(is_violated=violated, is_mosaic=mosaic)
            if blocked or img is None:
                return message
            img_path = await self._save_render_image(img)
            if img_path:
                return [{"type": "image", "data": {"file": str(img_path.resolve())}}]

        return message

    def _parsed_group_list(self) -> List[str]:
        """解析群号白名单（带缓存：配置不变时不重复切分清洗）"""
        cfg = self.cfg_mgr.config
        mode = str(cfg.get("group_mode", "whitelist") or "whitelist")
        raw_list = str(cfg.get("group_list", "") or "")
        sig = (mode, raw_list)
        if self._group_cache_sig == sig:
            return self._group_cache_tokens
        tokens = [g.strip() for g in _SPLIT_GROUP_RE.split(raw_list) if g.strip()]
        self._group_cache_sig = sig
        self._group_cache_tokens = tokens
        return tokens

    def _match_group_tokens(self, gid: str, tokens: List[str]) -> bool:
        clean_gid = _NON_DIGIT_RE.sub("", gid)
        for t in tokens:
            if gid == t:
                return True
            clean_t = _NON_DIGIT_RE.sub("", t)
            if clean_t and clean_gid and clean_gid == clean_t:
                return True
        return False

    def _is_gid_allowed(self, gid: str) -> bool:
        """纯群号检查是否在白名单中"""
        cfg = self.cfg_mgr.config
        mode = str(cfg.get("group_mode", "whitelist") or "whitelist")
        if mode == "all":
            return True
        tokens = self._parsed_group_list()
        is_in = self._match_group_tokens(gid, tokens)
        if mode == "whitelist":
            return is_in
        elif mode == "blacklist":
            return not is_in
        return False

    def _get_group_display_name(self, event: AstrMessageEvent, gid: str) -> str:
        """获取群名称，若无法获取则友好返回'本群'"""
        try:
            mo = getattr(event, "message_obj", None)
            if mo:
                grp = getattr(mo, "group", None)
                if grp:
                    gn = str(getattr(grp, "group_name", "") or "").strip()
                    if gn:
                        return gn
            # 查历史已见
            if gid in self._seen_groups:
                gn = str(self._seen_groups[gid].get("group_name", "") or "").strip()
                if gn:
                    return gn
        except Exception:
            pass
        return "本群"

    def _get_group_custom_config(self, gid: str) -> Dict[str, Any]:
        """获取指定群的专属配置字典：style, theme_mode, font, font_scale"""
        if not gid:
            return {}
        raw = self.cfg_mgr.config.get("group_configs", {})
        if isinstance(raw, str):
            try:
                import json as _js
                raw = _js.loads(raw) if raw.strip() else {}
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            return {}
        clean_gid = _NON_DIGIT_RE.sub("", str(gid))
        for k, v in raw.items():
            ck = _NON_DIGIT_RE.sub("", str(k))
            if str(k) == str(gid) or (ck and ck == clean_gid):
                return v if isinstance(v, dict) else {}
        return {}

    def _get_group_font_scale(self, gid: str) -> int:
        """获取群组单独字体大小，若无则返回全局"""
        try:
            base = int(self.cfg_mgr.config.get("font_scale", 100) or 100)
        except Exception:
            base = 100
        if not gid:
            return base
        # 优先读取 group_configs 中的配置
        grp_c = self._get_group_custom_config(gid)
        if "font_scale" in grp_c:
            try:
                return max(50, min(500, int(grp_c["font_scale"])))
            except Exception:
                pass
        # 兼容旧 group_font_scales
        raw = self.cfg_mgr.config.get("group_font_scales", {})
        if isinstance(raw, str):
            try:
                import json as _js
                raw = _js.loads(raw) if raw.strip() else {}
            except Exception:
                raw = {}
        if isinstance(raw, dict):
            clean_gid = _NON_DIGIT_RE.sub("", str(gid))
            for k, v in raw.items():
                ck = _NON_DIGIT_RE.sub("", str(k))
                if str(k) == str(gid) or (ck and ck == clean_gid):
                    try:
                        sc = int(v)
                        return max(50, min(500, sc))
                    except Exception:
                        pass
        return base

    # ==========================================
    # 消息转图片核心钩子 (设置最高优先级 priority=99999)
    # ==========================================
    @filter.on_decorating_result(priority=99999)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息发送前拦截文本并转为高颜值图片 (最高优先级，抢在 AstrNa 等插件之前处理)"""
        self._ensure_bg_tasks()
        cfg = self.cfg_mgr.config
        if not bool(cfg.get("enable", True)):
            return

        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return

        # 1. 检查群组黑白名单过滤
        if not self._is_group_allowed(event):
            return

        # 2. 提取文本内容与组件分类
        plain_texts: List[str] = []
        has_other_media = False

        for comp in result.chain:
            if isinstance(comp, Plain):
                plain_texts.append(comp.text)
            elif comp.__class__.__name__ in ("Image", "Record", "Video", "File"):
                has_other_media = True

        full_text = "".join(plain_texts).strip()
        if not full_text:
            return

        # 如果已有图片/音视频且没有明显的说明文本，不作处理
        if has_other_media and len(full_text) < 10:
            return

        # 3. 最小字数门槛过滤
        min_threshold = int(cfg.get("min_length_threshold", 1) or 1)
        if len(full_text) < min_threshold:
            return

        # 4. 链接处理策略判断
        link_mode = str(cfg.get("link_mode", "as_image") or "as_image")
        urls_found = _clean_urls(_URL_PATTERN.findall(full_text))
        if urls_found and link_mode == "keep_text":
            # 用户选择包含链接时保持纯文本，方便群友点击
            return

        # 5. 仅违规时转图：非违规直接保留纯文本（放在审查前快速判断，避免无谓渲染）
        render_trigger = str(cfg.get("render_trigger", "always") or "always")
        if render_trigger == "violation_only":
            # 快速关键词预检：moderation none 且无命中则直接跳过
            quick_hit, _ = self.moderator.check_keywords(full_text)
            if not quick_hit and str(cfg.get("moderation_mode", "keywords")) == "none":
                return

        # 6-8. 统一审查 + 渲染 + 落盘（群组单独字体大小与专属风格/主题）
        gid_main = self._extract_group_id(event)
        grp_c_main = self._get_group_custom_config(gid_main)
        eff_scale_main = self._get_group_font_scale(gid_main)
        img, violated, mosaic, blocked = await self._review_and_render(
            full_text,
            font_scale=eff_scale_main,
            style=grp_c_main.get("style"),
            theme_mode=grp_c_main.get("theme_mode"),
            keyword_preset=grp_c_main.get("keyword_preset"),
        )
        # 仅违规触发二次门控：无违规则不转图
        if render_trigger == "violation_only" and not violated:
            return
        self.cfg_mgr.record_render(is_violated=violated, is_mosaic=mosaic)
        if violated:
            logger.info(f"[{PLUGIN_NAME}] 触发安全审查 -> blocked={blocked} mosaic={mosaic}")
        if blocked:
            # 直接拦截不发送
            event.stop_event()
            return
        if img is None:
            return
        img_path = await self._save_render_image(img)
        if img_path is None:
            return

        # 8. 组装新消息链并替换
        new_chain = []
        # 保留原链中的 At、Reply 等前置修饰段
        for comp in result.chain:
            if comp.__class__.__name__ in ("At", "AtAll", "Reply"):
                new_chain.append(comp)

        # 插入渲染出的图片段
        new_chain.append(AstrImage.fromFileSystem(str(img_path)))

        # 保留原链中的多媒体段（长文本配图时不再丢弃原图/音视频）
        for comp in result.chain:
            if comp.__class__.__name__ in ("Image", "Record", "Video", "File"):
                new_chain.append(comp)

        # 若配置为提取并附带可点击纯文本链接
        if link_mode == "extract_append" and urls_found:
            links_text = "\n🔗 快捷直达链接：\n" + "\n".join(urls_found[:5])
            new_chain.append(Plain(links_text))

        result.chain = new_chain

    # ==========================================
    # 群聊过滤判断
    # ==========================================
    def _extract_group_id(self, event: AstrMessageEvent) -> str:
        """多重兼容提取群号"""
        try:
            gid = str(event.get_group_id() or "").strip()
            if gid and gid not in ("None", "0"):
                return gid
        except Exception:
            pass
        try:
            mo = getattr(event, "message_obj", None)
            if mo:
                gid = str(getattr(mo, "group_id", "") or "").strip()
                if gid and gid not in ("None", "0"):
                    return gid
                grp = getattr(mo, "group", None)
                if grp:
                    gid = str(getattr(grp, "group_id", "") or "").strip()
                    if gid and gid not in ("None", "0"):
                        return gid
        except Exception:
            pass
        try:
            sess = getattr(event, "session", None)
            if sess:
                msg_type = str(getattr(sess, "message_type", ""))
                if "group" in msg_type.lower():
                    gid = str(getattr(sess, "session_id", "") or "").strip()
                    if gid and gid not in ("None", "0"):
                        return gid
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if ":group:" in umo.lower():
                return umo.split(":")[-1].strip()
        except Exception:
            pass
        return ""

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        gid = self._remember_group(event)
        if not gid:
            # 私聊默认生效
            return True

        cfg = self.cfg_mgr.config
        mode = str(cfg.get("group_mode", "whitelist") or "whitelist")
        if mode == "all":
            logger.debug(f"[{PLUGIN_NAME}] 群聊 {gid} 匹配通过 (模式: 全部群生效)")
            return True

        tokens = self._parsed_group_list()
        is_in = self._match_group_tokens(gid, tokens)

        if mode == "whitelist":
            if is_in:
                logger.debug(f"[{PLUGIN_NAME}] 群聊 {gid} 命中白名单，允许转图")
                return True
            else:
                logger.debug(f"[{PLUGIN_NAME}] 群聊 {gid} 未在白名单中，跳过转图")
                return False
        elif mode == "blacklist":
            if is_in:
                logger.debug(f"[{PLUGIN_NAME}] 群聊 {gid} 命中黑名单，跳过转图")
                return False
            return True
        return False

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        """尽力判定是否为管理员。平台不支持判定则放行（兼容旧版本 AstrBot）。

        变更类子指令（on/off/style/star）需要管理员，状态查看与 test 不限。
        """
        try:
            fn = getattr(event, "is_admin", None)
            if callable(fn):
                res = fn()
                if isinstance(res, bool):
                    return res
        except Exception:
            pass
        try:
            role = str(getattr(event, "role", "") or "").lower()
            if role in ("admin", "owner"):
                return True
            if role in ("member", "user"):
                return False
        except Exception:
            pass
        return True

    # ==========================================
    # 管理指令交互 (仅保留纯净的 /xbimg 指令)
    # ==========================================
    @filter.command("xbimg")
    async def cmd_xbimg(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        """消息转图助手管理指令"""
        sub = sub.strip().lower()
        arg = arg.strip()
        cfg = self.cfg_mgr.config

        comp_map = {
            "high": "⚡ 极小文件 (省流紧凑)",
            "medium": "⚖️ 均衡适中 (推荐)",
            "low": "💎 原画无损 (高清大图)",
        }
        action_map = {
            "mosaic_half": "🎭 打一半马赛克",
            "mosaic_full": "🔒 全图正文打码",
            "block": "🚫 彻底拦截静默",
            "notice": "⚠️ 替换合规警示卡片",
        }
        group_map = {
            "whitelist": "🛡️ 仅白名单群生效",
            "all": "🌐 全部所有群直接生效",
            "blacklist": "🚫 黑名单排除模式",
        }
        link_map = {
            "as_image": "图片渲染",
            "keep_text": "保持纯文本直接发送",
            "extract_append": "转图并附带纯文本链接",
        }
        density_map = {
            "sparse": "稀疏 (~25颗)",
            "medium": "标准 (~50颗)",
            "dense": "星海 (~80颗)",
        }

        if not sub or sub in ("help", "status", "菜单"):
            yield event.plain_result(
                "🎨【xbimg 消息转图助手 · 控制台】\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"• 总开关状态：{'🟢 运行中' if cfg.get('enable', True) else '🔴 已暂停'}\n"
                f"• 触发时机：{'🔄 始终转图' if cfg.get('render_trigger') == 'always' else '🛡️ 仅违规时转图'}\n"
                f"• 最少字数：{cfg.get('min_length_threshold', 1)} 字\n"
                f"• 视觉风格：{cfg.get('style', 'ios').upper()}\n"
                f"• 配色主题：{'🌞 浅色明亮' if cfg.get('theme_mode') == 'light' else '🌙 深色暗黑'}\n"
                f"• 星空背景：{'✨ 开启' if cfg.get('star_background') else '❌ 关闭'} ({density_map.get(cfg.get('star_density', 'medium'), '标准')})\n"
                f"• 文件体积：{comp_map.get(cfg.get('img_compress_level', 'medium'), '均衡适中')}\n"
                f"• 链接策略：{link_map.get(cfg.get('link_mode', 'as_image'), '图片渲染')}\n"
                f"• 屏蔽词审查：{'🟢 开启' if cfg.get('enable_keywords_moderation', True) and cfg.get('moderation_mode') != 'none' else '⚪ 关闭'}\n"
                f"• AI 审查开关：{'🤖 开启' if cfg.get('enable_ai_moderation') else '⚪ 关闭'}\n"
                f"• 违规处置：{action_map.get(cfg.get('violation_action', 'mosaic_half'), '打一半马赛克')}\n"
                f"• 全局字体：{cfg.get('font_scale', 100)}%\n"
                f"• 群生效模式：{group_map.get(cfg.get('group_mode', 'whitelist'), '仅白名单群生效')}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💡 常用修改指令：\n"
                "• /xbimg on / off - 总开关\n"
                "• /xbimg trigger always / violation - 触发时机\n"
                "• /xbimg minlen <字数> - 最小字数门槛\n"
                "• /xbimg style ios / android16 - 全局风格\n"
                "• /xbimg theme light / dark - 全局配色\n"
                "• /xbimg star on / off - 背景星空开关\n"
                "• /xbimg density sparse / medium / dense - 星星密度\n"
                "• /xbimg quality low / medium / high - 输出文件大小档位\n"
                "• /xbimg link image / text / append - 链接策略\n"
                "• /xbimg mod on / off - 屏蔽词审查开关\n"
                "• /xbimg ai on / off - AI 审查独立开关\n"
                "• /xbimg action half / full / block / notice - 违规处置\n"
                "• /xbimg mosaic pixel / blur - 马赛克颗粒/模糊\n"
                "• /xbimg mosaicpos bottom / top / random - 半字打码位置\n"
                "• /xbimg groupmode whitelist / all / blacklist - 群生效模式\n"
                "• /xbimg size [群号] 50-500 - 字体百分比\n"
                "• /xbimg test [文本] - 立即生成测试效果图"
            )
            return

        if not self._is_admin_event(event):
            yield event.plain_result("⛔ 仅群管理员可修改转图配置。")
            return

        if sub in ("on", "start", "开启"):
            cfg["enable"] = True
            self.cfg_mgr.save()
            yield event.plain_result("✅ 消息转图助手已开启！后续文本消息将自动渲染为美观图片。")
        elif sub in ("off", "stop", "关闭"):
            cfg["enable"] = False
            self.cfg_mgr.save()
            yield event.plain_result("⏸️ 消息转图助手已暂停。")
        elif sub in ("trigger", "trig"):
            if arg in ("violation", "violation_only", "仅违规", "违规"):
                cfg["render_trigger"] = "violation_only"
                self.cfg_mgr.save()
                yield event.plain_result("✅ 转图触发时机已设为：🛡️ 仅违规时转图（普通消息保持纯文本）")
            elif arg in ("always", "所有", "始终", "全部"):
                cfg["render_trigger"] = "always"
                self.cfg_mgr.save()
                yield event.plain_result("✅ 转图触发时机已设为：🔄 始终转图")
            else:
                yield event.plain_result("用法：/xbimg trigger always 或 /xbimg trigger violation")
        elif sub in ("minlen", "minlength", "len"):
            try:
                num = max(1, min(1000, int(arg)))
                cfg["min_length_threshold"] = num
                self.cfg_mgr.save()
                yield event.plain_result(f"✅ 触发转图字数门槛已设为：{num} 字")
            except Exception:
                yield event.plain_result("用法：/xbimg minlen <1-1000>")
        elif sub == "style":
            if arg in ("ios", "android16"):
                cfg["style"] = arg
                self.cfg_mgr.save()
                yield event.plain_result(f"🎨 全局视觉风格已切换为：{arg.upper()}")
            else:
                yield event.plain_result("用法：/xbimg style ios 或 /xbimg style android16")
        elif sub in ("theme", "配色"):
            if arg in ("light", "浅色"):
                cfg["theme_mode"] = "light"
                self.cfg_mgr.save()
                yield event.plain_result("🎨 全局配色已设为：☀️ 浅色明亮")
            elif arg in ("dark", "深色"):
                cfg["theme_mode"] = "dark"
                self.cfg_mgr.save()
                yield event.plain_result("🎨 全局配色已设为：🌙 深色暗黑")
            else:
                yield event.plain_result("用法：/xbimg theme light 或 /xbimg theme dark")
        elif sub == "star":
            if arg in ("on", "开启", "1"):
                cfg["star_background"] = True
            elif arg in ("off", "关闭", "0"):
                cfg["star_background"] = False
            self.cfg_mgr.save()
            yield event.plain_result(f"✨ 星空背景已设置为：{'开启' if cfg.get('star_background') else '关闭'}")
        elif sub in ("density", "stardensity"):
            if arg in ("sparse", "稀疏"):
                cfg["star_density"] = "sparse"
                self.cfg_mgr.save()
                yield event.plain_result("✨ 小星星密度已设为：稀疏 (~25颗)")
            elif arg in ("dense", "星海", "密集"):
                cfg["star_density"] = "dense"
                self.cfg_mgr.save()
                yield event.plain_result("✨ 小星星密度已设为：星海 (~80颗)")
            elif arg in ("medium", "标准"):
                cfg["star_density"] = "medium"
                self.cfg_mgr.save()
                yield event.plain_result("✨ 小星星密度已设为：标准 (~50颗)")
            else:
                yield event.plain_result("用法：/xbimg density sparse / medium / dense")
        elif sub in ("quality", "compress", "压缩", "文件大小"):
            if arg in ("low", "原画", "无损", "png"):
                cfg["img_compress_level"] = "low"
                self.cfg_mgr.save()
                yield event.plain_result("💾 文件体积优化已设为：原画无损 (PNG · 最高画质)")
            elif arg in ("high", "紧凑", "极小", "省流"):
                cfg["img_compress_level"] = "high"
                self.cfg_mgr.save()
                yield event.plain_result("💾 文件体积优化已设为：极小文件 (JPEG 82% · 极致省流量)")
            elif arg in ("medium", "标准", "均衡"):
                cfg["img_compress_level"] = "medium"
                self.cfg_mgr.save()
                yield event.plain_result("💾 文件体积优化已设为：均衡适中 (JPEG 92% · 推荐)")
            else:
                yield event.plain_result("用法：/xbimg quality low / medium / high")
        elif sub in ("link", "linkmode", "链接"):
            if arg in ("image", "as_image", "图", "图片"):
                cfg["link_mode"] = "as_image"
                self.cfg_mgr.save()
                yield event.plain_result("🔗 链接策略已设为：链接一并转为图片")
            elif arg in ("text", "keep_text", "纯文本"):
                cfg["link_mode"] = "keep_text"
                self.cfg_mgr.save()
                yield event.plain_result("🔗 链接策略已设为：包含链接时保持纯文本直接发送")
            elif arg in ("append", "extract_append", "兼得", "提取"):
                cfg["link_mode"] = "extract_append"
                self.cfg_mgr.save()
                yield event.plain_result("🔗 链接策略已设为：转图后附带提取纯文本链接")
            else:
                yield event.plain_result("用法：/xbimg link image / text / append")
        elif sub in ("mod", "审查"):
            if arg in ("on", "开启", "1"):
                cfg["enable_keywords_moderation"] = True
                cfg["moderation_mode"] = "keywords"
                self.cfg_mgr.save()
                self.moderator = ContentModerator(cfg)
                yield event.plain_result("🛡️ 敏感屏蔽词库审查已开启。")
            elif arg in ("off", "关闭", "0"):
                cfg["enable_keywords_moderation"] = False
                cfg["moderation_mode"] = "none"
                self.cfg_mgr.save()
                self.moderator = ContentModerator(cfg)
                yield event.plain_result("⚪ 敏感屏蔽词库审查已关闭。")
            else:
                yield event.plain_result("用法：/xbimg mod on 或 /xbimg mod off")
        elif sub in ("ai", "aimod"):
            if arg in ("on", "开启", "1"):
                cfg["enable_ai_moderation"] = True
                self.cfg_mgr.save()
                self.moderator = ContentModerator(cfg)
                yield event.plain_result("🤖 AI 大模型内容安全审查已开启！")
            elif arg in ("off", "关闭", "0"):
                cfg["enable_ai_moderation"] = False
                self.cfg_mgr.save()
                self.moderator = ContentModerator(cfg)
                yield event.plain_result("⚪ AI 大模型内容安全审查已关闭。")
            else:
                yield event.plain_result("用法：/xbimg ai on 或 /xbimg ai off")
        elif sub in ("action", "处置"):
            if arg in ("half", "mosaic_half", "半打码", "半马赛克"):
                cfg["violation_action"] = "mosaic_half"
                self.cfg_mgr.save()
                yield event.plain_result("🎭 违规处置动作已设为：打一半马赛克 (Half Mosaic)")
            elif arg in ("full", "mosaic_full", "全打码"):
                cfg["violation_action"] = "mosaic_full"
                self.cfg_mgr.save()
                yield event.plain_result("🔒 违规处置动作已设为：全图正文打码")
            elif arg in ("block", "拦截", "静默"):
                cfg["violation_action"] = "block"
                self.cfg_mgr.save()
                yield event.plain_result("🚫 违规处置动作已设为：彻底拦截静默不发")
            elif arg in ("notice", "警示", "卡片"):
                cfg["violation_action"] = "notice"
                self.cfg_mgr.save()
                yield event.plain_result("⚠️ 违规处置动作已设为：替换为合规警示卡片")
            else:
                yield event.plain_result("用法：/xbimg action half / full / block / notice")
        elif sub in ("mosaic", "打码类型"):
            if arg in ("pixel", "像素"):
                cfg["mosaic_type"] = "pixel"
                self.cfg_mgr.save()
                yield event.plain_result("🧱 马赛克类型已设为：复古像素颗粒块")
            elif arg in ("blur", "模糊", "毛玻璃"):
                cfg["mosaic_type"] = "blur"
                self.cfg_mgr.save()
                yield event.plain_result("🌫️ 马赛克类型已设为：高斯磨砂毛玻璃")
            else:
                yield event.plain_result("用法：/xbimg mosaic pixel 或 /xbimg mosaic blur")
        elif sub in ("mosaicpos", "半码位置"):
            if arg in ("bottom", "下半", "下"):
                cfg["mosaic_half_pos"] = "bottom"
                self.cfg_mgr.save()
                yield event.plain_result("⬇️ 半字打码位置已设为：遮下半")
            elif arg in ("top", "上半", "上"):
                cfg["mosaic_half_pos"] = "top"
                self.cfg_mgr.save()
                yield event.plain_result("⬆️ 半字打码位置已设为：遮上半")
            elif arg in ("random", "随机"):
                cfg["mosaic_half_pos"] = "random"
                self.cfg_mgr.save()
                yield event.plain_result("🎲 半字打码位置已设为：每字随机上下")
            else:
                yield event.plain_result("用法：/xbimg mosaicpos bottom / top / random")
        elif sub in ("groupmode", "群模式"):
            if arg in ("whitelist", "白名单"):
                cfg["group_mode"] = "whitelist"
                self._group_cache_sig = None
                self.cfg_mgr.save()
                yield event.plain_result("🛡️ 群生效模式已设为：仅白名单群生效")
            elif arg in ("all", "全群", "全部"):
                cfg["group_mode"] = "all"
                self._group_cache_sig = None
                self.cfg_mgr.save()
                yield event.plain_result("🌐 群生效模式已设为：所有群聊直接生效")
            elif arg in ("blacklist", "黑名单"):
                cfg["group_mode"] = "blacklist"
                self._group_cache_sig = None
                self.cfg_mgr.save()
                yield event.plain_result("🚫 群生效模式已设为：黑名单排除模式")
            else:
                yield event.plain_result("用法：/xbimg groupmode whitelist / all / blacklist")
        elif sub in ("kwset", "preset", "词库"):
            presets = cfg.get("keyword_presets", {})
            if not isinstance(presets, dict):
                presets = {}
            if arg in presets:
                cfg["active_keyword_preset"] = arg
                item = presets[arg]
                cfg["custom_keywords"] = item.get("keywords", "") if isinstance(item, dict) else str(item)
                self.cfg_mgr.save()
                self.moderator = ContentModerator(cfg)
                name = item.get("name", arg) if isinstance(item, dict) else arg
                yield event.plain_result(f"✅ 全局词库方案已切换为：{name}")
            else:
                names = [f"• {k} - {(v.get('name') if isinstance(v, dict) else k)}" for k, v in presets.items()]
                yield event.plain_result("用法：/xbimg kwset <方案ID>\n当前可用词库：\n" + "\n".join(names))
        elif sub in ("fontsize", "groupfont", "gscale", "font", "size", "字号", "文字大小"):
            # /xbimg size <gid> <scale>  或  /xbimg size <scale>（当前群）
            parts = [p for p in re.split(r"[\s,]+", arg) if p]
            gid = ""
            scale = None
            if len(parts) >= 2:
                gid = re.sub(r"\D", "", parts[0])
                try:
                    scale = int(parts[1])
                except Exception:
                    scale = None
            elif len(parts) == 1:
                try:
                    scale = int(parts[0])
                    gid = self._extract_group_id(event)
                except Exception:
                    scale = None
            if scale is None or not (50 <= scale <= 500):
                cur = self._get_group_font_scale(self._extract_group_id(event)) if not gid else self._get_group_font_scale(gid)
                yield event.plain_result(f"用法：/xbimg size [群号] <50-500>\n当前{'群 '+gid if gid else '全局'}字体：{cur}%\n示例：/xbimg size 120  或  /xbimg size 123456 130")
                return
            target_gid = gid or self._extract_group_id(event)
            if not target_gid:
                # 私聊则改全局
                cfg["font_scale"] = scale
                self.cfg_mgr.save({"font_scale": scale})
                yield event.plain_result(f"✅ 全局字体已设为 {scale}%")
            else:
                raw = cfg.get("group_font_scales", {})
                if isinstance(raw, str):
                    try:
                        import json as _js
                        raw = _js.loads(raw) if raw.strip() else {}
                    except Exception:
                        raw = {}
                if not isinstance(raw, dict):
                    raw = {}
                if scale == 100:
                    raw.pop(str(target_gid), None)
                    raw.pop(target_gid, None)
                else:
                    raw[str(target_gid)] = scale
                cfg["group_font_scales"] = raw
                self.cfg_mgr.save({"group_font_scales": raw})
                yield event.plain_result(f"✅ 群 {target_gid} 字体已设为 {scale}%（100% 为默认，自动移除）")
            return
        elif sub == "test":
            test_content = arg or "这是一条来自 AstrBot 消息转图助手的测试消息！✨\n祝您使用愉快~"
            img = await asyncio.to_thread(
                MessageImageRenderer.render,
                text=test_content,
                style=str(cfg.get("style", "ios")),
                theme_mode=str(cfg.get("theme_mode", "light")),
                star_background=bool(cfg.get("star_background", True)),
                star_density=str(cfg.get("star_density", "medium")),
                mosaic_mode="none",
                mosaic_type=str(cfg.get("mosaic_type", "pixel")),
                emoji_remote=bool(cfg.get("emoji_remote", True)),
            )
            test_path = self.cache_dir / f"test_{int(time.time())}.png"
            await asyncio.to_thread(img.save, str(test_path), "PNG")
            yield event.chain_result([AstrImage.fromFileSystem(str(test_path))])
        else:
            yield event.plain_result("未知子指令，请输入 /xbimg 查看指令菜单。")

    @filter.command("text")
    async def cmd_text(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        """群专属快捷配置指令：/text set | /text ttf | /text list | /text test"""
        sub = sub.strip().lower()
        arg = arg.strip()
        gid = self._extract_group_id(event)
        gname = self._get_group_display_name(event, gid)

        if not sub:
            cur_scale = self._get_group_font_scale(gid)
            grp_c = self._get_group_custom_config(gid)
            cur_style = grp_c.get("style", self.cfg_mgr.config.get("style", "ios"))
            cur_theme = grp_c.get("theme_mode", self.cfg_mgr.config.get("theme_mode", "light"))
            cur_kw = grp_c.get("keyword_preset", "")
            kw_name = "跟随全局默认" if not cur_kw else cur_kw
            yield event.plain_result(
                f"🎨【{gname} · 转图专属设置】\n"
                "--------------------\n"
                f"当前字体大小：{cur_scale}%\n"
                f"当前视觉风格：{cur_style.upper()}\n"
                f"当前配色主题：{'浅色' if cur_theme == 'light' else '深色'}\n"
                f"绑定词库方案：{kw_name}\n"
                "--------------------\n"
                "💡 专属群指令：\n"
                "• /text set <50-500> - 设置当前群字体大小\n"
                "• /text style ios / android16 / default - 单群风格\n"
                "• /text theme light / dark / default - 单群配色\n"
                "• /text kw <词库方案ID> - 单群绑定指定词库\n"
                "• /text ttf <id> - 切换当前群精选字体\n"
                "• /text list - 查看可用字体与词库列表\n"
                "• /text reset - 恢复该群所有设置跟随全局\n"
                "• /text test - 发送当前群专属测试图"
            )
            return

        if not self._is_admin_event(event):
            yield event.plain_result("⛔ 仅管理员可配置转图设置。")
            return

        if sub == "set":
            # 调整字体大小
            try:
                val = int(arg)
            except Exception:
                val = None
            if val is None or not (50 <= val <= 500):
                yield event.plain_result(f"用法：/text set <50-500>\n示例：/text set 120 （将【{gname}】字体调为 120%）")
                return
            if not gid:
                self.cfg_mgr.config["font_scale"] = val
                self.cfg_mgr.save({"font_scale": val})
                yield event.plain_result(f"✅ 全局字体已设为 {val}%")
            else:
                self._update_group_custom(gid, {"font_scale": val})
                yield event.plain_result(f"✅ 已将【{gname}】的字体大小设置为 {val}%")

        elif sub == "list":
            # 查看字体列表与词库列表
            try:
                from core.renderer import CURATED_FONTS as _CF
            except Exception:
                _CF = []
            presets = self.cfg_mgr.config.get("keyword_presets", {})
            if not isinstance(presets, dict):
                presets = {}
            lines = [f"🔤【可用字体列表】（共 {len(_CF)} 款）:"]
            for idx, item in enumerate(_CF, 1):
                lines.append(f"{idx}. {item['name']} - id: {item['id']}")
            lines.append("\n🛡️【可用敏感词库方案】:")
            for pk, pv in presets.items():
                pname = pv.get("name", pk) if isinstance(pv, dict) else pk
                lines.append(f"• {pk} - {pname}")
            lines.append("\n💡 切换指令：\n• /text ttf <字体id>\n• /text kw <词库id>\n• /text reset (恢复全部默认)")
            yield event.plain_result("\n".join(lines))

        elif sub in ("kw", "preset", "词库"):
            presets = self.cfg_mgr.config.get("keyword_presets", {})
            if not isinstance(presets, dict):
                presets = {}
            if arg in ("default", "reset", "跟随", "默认", ""):
                if gid:
                    self._update_group_custom(gid, {"keyword_preset": ""})
                    yield event.plain_result(f"✅ 已将【{gname}】绑定的敏感词库恢复为：跟随全局默认。")
                return
            if arg in presets:
                if gid:
                    self._update_group_custom(gid, {"keyword_preset": arg})
                    pname = presets[arg].get("name", arg) if isinstance(presets[arg], dict) else arg
                    yield event.plain_result(f"✅ 已将【{gname}】绑定的敏感词库切换为：{pname}")
            else:
                names = [f"• {k} - {(v.get('name') if isinstance(v, dict) else k)}" for k, v in presets.items()]
                yield event.plain_result(f"用法：/text kw <词库方案ID>  或  /text kw default(恢复跟随全局)\n当前可用词库：\n" + "\n".join(names))

        elif sub in ("style", "风格"):
            if arg in ("default", "reset", "跟随", "默认", ""):
                if gid:
                    self._update_group_custom(gid, {"style": ""})
                    yield event.plain_result(f"✅ 已将【{gname}】的视觉风格恢复为跟随全局默认。")
                return
            if arg in ("ios", "android16", "android"):
                st = "android16" if "android" in arg else "ios"
                if gid:
                    self._update_group_custom(gid, {"style": st})
                    yield event.plain_result(f"✅ 已将【{gname}】的视觉风格设置为：{st.upper()}")
            else:
                yield event.plain_result("用法：/text style ios 或 /text style android16 或 /text style default")

        elif sub in ("theme", "配色"):
            if arg in ("default", "reset", "跟随", "默认", ""):
                if gid:
                    self._update_group_custom(gid, {"theme_mode": ""})
                    yield event.plain_result(f"✅ 已将【{gname}】的配色主题恢复为跟随全局默认。")
                return
            if arg in ("light", "dark", "浅色", "深色"):
                tm = "dark" if arg in ("dark", "深色") else "light"
                if gid:
                    self._update_group_custom(gid, {"theme_mode": tm})
                    yield event.plain_result(f"✅ 已将【{gname}】的配色主题设置为：{'深色暗黑' if tm == 'dark' else '浅色明亮'}")
            else:
                yield event.plain_result("用法：/text theme light 或 /text theme dark 或 /text theme default")

        elif sub == "ttf":
            # 切换当前群字体
            if not arg:
                yield event.plain_result(f"用法：/text ttf <字体id>\n可发送 /text list 查看可用 id。")
                return
            try:
                from core.renderer import CURATED_FONTS as _CF, download_curated_font
            except Exception:
                _CF = []
            hit = next((x for x in _CF if x["id"].lower() == arg.lower()), None)
            if not hit:
                yield event.plain_result(f"❌ 未找到字体 id「{arg}」，请发送 /text list 查看支持的列表。")
                return
            # 确保下载
            yield event.plain_result(f"⏳ 正在检查【{hit['name']}】字体包…")
            res = await asyncio.to_thread(download_curated_font, hit["id"])
            if not res.get("ok"):
                yield event.plain_result(f"❌ 字体准备失败: {res.get('error', '网络异常')}")
                return
            # 绑定到当前群专属配置
            fname = res.get("downloaded", [""])[0] or hit["files"][0][0]
            if gid:
                self._update_group_custom(gid, {"custom_font_path": fname})
                yield event.plain_result(f"✅ 成功将【{gname}】的字体切换为：{hit['name']}")
            else:
                self.cfg_mgr.save({"font_source": "custom", "custom_font_path": fname})
                yield event.plain_result(f"✅ 全局字体已切换为：{hit['name']}")

        elif sub in ("reset", "default", "恢复默认", "重置"):
            if gid:
                raw = self.cfg_mgr.config.get("group_configs", {})
                if isinstance(raw, dict) and (str(gid) in raw or gid in raw):
                    raw.pop(str(gid), None)
                    raw.pop(gid, None)
                    self.cfg_mgr.save({"group_configs": raw})
                raw_sc = self.cfg_mgr.config.get("group_font_scales", {})
                if isinstance(raw_sc, dict) and (str(gid) in raw_sc or gid in raw_sc):
                    raw_sc.pop(str(gid), None)
                    raw_sc.pop(gid, None)
                    self.cfg_mgr.save({"group_font_scales": raw_sc})
                yield event.plain_result(f"✅ 已恢复【{gname}】的所有专属设置，完全跟随全局默认。")
            else:
                yield event.plain_result("当前不在群聊中。")

        elif sub == "test":
            # 发送测试图：包含链接、代码、假装违规词（无害日常词伪装）
            eff_scale = self._get_group_font_scale(gid)
            grp_c = self._get_group_custom_config(gid)
            style_eff = grp_c.get("style", self.cfg_mgr.config.get("style", "ios"))
            theme_eff = grp_c.get("theme_mode", self.cfg_mgr.config.get("theme_mode", "light"))
            # 构造测试文本：假装"测试词"是违规词
            test_content = (
                f"# 🎨【{gname}】专属渲染效果测试\n"
                "这是一段用于测试消息转图排版与美观度的标准文本。\n\n"
                "- 链接测试：https://astrbot.app 欢迎访问官方网站\n"
                "- 模拟打码：本行包含测试词展示半马赛克精美效果\n"
                "- 代码展示：\n"
                "```python\n"
                "def hello_world():\n"
                "    print('Msg2Img by xbimg - Test Passed!')\n"
                "```"
            )
            img = await asyncio.to_thread(
                MessageImageRenderer.render,
                text=test_content,
                style=style_eff,
                theme_mode=theme_eff,
                star_background=bool(self.cfg_mgr.config.get("star_background", True)),
                star_density=str(self.cfg_mgr.config.get("star_density", "medium")),
                mosaic_mode="half",
                mosaic_type=str(self.cfg_mgr.config.get("mosaic_type", "pixel")),
                violation_words=["测试词"],
                emoji_remote=True,
                mosaic_half_pos=str(self.cfg_mgr.config.get("mosaic_half_pos", "bottom")),
                font_scale=eff_scale,
            )
            img_filename = f"test_{int(time.time()*1000)}.png"
            img_path = self.cache_dir / img_filename
            await asyncio.to_thread(img.save, str(img_path), "PNG")
            self._schedule_delete(img_path, 45)
            yield event.chain_result([AstrImage.fromFileSystem(str(img_path))])
        else:
            yield event.plain_result(f"未知子指令，请输入 /text 查看【{gname}】指令菜单。")

    @filter.command("style")
    async def cmd_group_style(self, event: AstrMessageEvent, style_name: str = ""):
        """切换当前群的专属风格：/style ios 或 /style android16 或 /style default(跟随全局)"""
        gid = self._extract_group_id(event)
        gname = self._get_group_display_name(event, gid)
        style_name = style_name.strip().lower()
        if style_name in ("default", "reset", "跟随", "默认", "auto"):
            if not self._is_admin_event(event):
                yield event.plain_result("⛔ 仅管理员可配置转图样式。")
                return
            if gid:
                self._update_group_custom(gid, {"style": ""})
                yield event.plain_result(f"✅ 已将【{gname}】的视觉风格恢复为跟随全局默认。")
            else:
                yield event.plain_result("当前不在群聊中。")
            return

        if style_name not in ("ios", "android16", "android"):
            yield event.plain_result(f"用法：/style ios  或  /style android16  或  /style default(恢复跟随全局)\n仅对【{gname}】单独生效。")
            return
        if not self._is_admin_event(event):
            yield event.plain_result("⛔ 仅管理员可配置转图样式。")
            return
        st = "android16" if "android" in style_name else "ios"
        if gid:
            self._update_group_custom(gid, {"style": st})
            yield event.plain_result(f"✅ 已将【{gname}】的视觉风格设置为：{st.upper()}")
        else:
            self.cfg_mgr.save({"style": st})
            yield event.plain_result(f"✅ 全局风格已设置为：{st.upper()}")

    @filter.command("theme")
    async def cmd_group_theme(self, event: AstrMessageEvent, theme_name: str = ""):
        """切换当前群的专属配色主题：/theme light 或 /theme dark 或 /theme default(跟随全局)"""
        gid = self._extract_group_id(event)
        gname = self._get_group_display_name(event, gid)
        theme_name = theme_name.strip().lower()
        if theme_name in ("default", "reset", "跟随", "默认", "auto"):
            if not self._is_admin_event(event):
                yield event.plain_result("⛔ 仅管理员可配置转图主题。")
                return
            if gid:
                self._update_group_custom(gid, {"theme_mode": ""})
                yield event.plain_result(f"✅ 已将【{gname}】的配色主题恢复为跟随全局默认。")
            else:
                yield event.plain_result("当前不在群聊中。")
            return

        if theme_name not in ("light", "dark", "浅色", "深色"):
            yield event.plain_result(f"用法：/theme light  或  /theme dark  或  /theme default(恢复跟随全局)\n仅对【{gname}】单独生效。")
            return
        if not self._is_admin_event(event):
            yield event.plain_result("⛔ 仅管理员可配置转图主题。")
            return
        tm = "dark" if theme_name in ("dark", "深色") else "light"
        if gid:
            self._update_group_custom(gid, {"theme_mode": tm})
            yield event.plain_result(f"✅ 已将【{gname}】的配色主题设置为：{'深色暗黑' if tm == 'dark' else '浅色明亮'}")
        else:
            self.cfg_mgr.save({"theme_mode": tm})
            yield event.plain_result(f"✅ 全局主题已设置为：{'深色暗黑' if tm == 'dark' else '浅色明亮'}")

    def _update_group_custom(self, gid: str, patch: Dict[str, Any]):
        """辅助方法：合并更新指定群专属配置并持久化保存"""
        if not gid:
            return
        cfg = self.cfg_mgr.config
        raw = cfg.get("group_configs", {})
        if isinstance(raw, str):
            try:
                import json as _js
                raw = _js.loads(raw) if raw.strip() else {}
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        gid_str = str(gid)
        cur = dict(raw.get(gid_str, {})) if isinstance(raw.get(gid_str), dict) else {}
        cur.update(patch)
        raw[gid_str] = cur
        cfg["group_configs"] = raw
        self.cfg_mgr.save({"group_configs": raw})

    def _schedule_delete(self, path: Path, delay: int = 45):
        """生成图片即时自动清理（默认 45 秒后删除，用完即删）"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        async def _del():
            try:
                await asyncio.sleep(delay)
                path.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            loop.create_task(_del())
        except Exception:
            pass

    # ==========================================
    # WebUI 后端 API 接口
    # ==========================================
    def _register_web_apis(self) -> None:
        if not _HAS_WEB_API:
            return
        reg = self.context.register_web_api
        pfx = PLUGIN_NAME
        reg(f"/{pfx}/config", self._api_get_config, ["GET"], "获取插件配置")
        reg(f"/{pfx}/config", self._api_save_config, ["POST"], "保存插件配置")
        reg(f"/{pfx}/config/get", self._api_get_config, ["GET"], "获取插件配置(别名)")
        reg(f"/{pfx}/config/save", self._api_save_config, ["POST"], "保存插件配置(别名)")
        reg(f"/{pfx}/stats", self._api_get_stats, ["GET"], "获取插件统计数据")
        reg(f"/{pfx}/groups", self._api_fetch_groups, ["GET"], "列出群聊列表")
        reg(f"/{pfx}/groups/fetch", self._api_fetch_groups, ["POST", "GET"], "主动拉取机器人所有群")
        reg(f"/{pfx}/preview", self._api_render_preview, ["POST"], "实时渲染预览图")
        reg(f"/{pfx}/preview_img", self._api_get_preview_img, ["GET"], "获取最新预览图图片")
        reg(f"/{pfx}/reset", self._api_reset_config, ["POST"], "重置默认配置")
        reg(f"/{pfx}/fonts/status", self._api_fonts_status, ["GET"], "查询字体状态")
        reg(f"/{pfx}/fonts/download", self._api_fonts_download, ["POST"], "下载缺失字体")
        reg(f"/{pfx}/fonts/files", self._api_fonts_files, ["GET"], "列出持久化目录字体")
        reg(f"/{pfx}/fonts/delete", self._api_fonts_delete, ["POST"], "删除持久化目录字体")
        reg(f"/{pfx}/fonts/curated", self._api_fonts_curated, ["GET"], "精选字体列表")
        reg(f"/{pfx}/fonts/curated_install", self._api_fonts_curated_install, ["POST"], "安装精选字体")
        reg(f"/{pfx}/fonts/curated_delete", self._api_fonts_curated_delete, ["POST"], "删除精选字体")
        reg(f"/{pfx}/emoji/packs", self._api_emoji_packs, ["GET"], "Emoji 样式列表")
        reg(f"/{pfx}/emoji/download", self._api_emoji_download, ["POST"], "下载 Emoji 样式")
        reg(f"/{pfx}/emoji/delete", self._api_emoji_delete, ["POST"], "删除 Emoji 样式")
        reg(f"/{pfx}/ai/providers", self._api_ai_providers, ["GET"], "列出 AstrBot 已接入模型")

    async def _api_get_config(self):
        return json_response({
            "ok": True,
            "config": self.cfg_mgr.config,
            "stats": self.cfg_mgr.get_stats(),
        })

    async def _api_save_config(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("无效的配置参数", status_code=400)
            self.cfg_mgr.save(payload)
            self._group_cache_sig = None  # 群名单可能变化，清缓存
            try:
                configure_fonts(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 字体配置同步异常: {e}")
            self.moderator = ContentModerator(self.cfg_mgr.config)
            # 自定义直链或 emoji 样式变更则后台触发下载（不阻塞）
            try:
                need_emoji = str(payload.get("emoji_style", "") or "").strip().lower() not in ("", "none")
                need_font = bool(str(payload.get("custom_font_url") or "").strip()) or need_emoji
                if need_font:
                    self._fonts_ensured = False
                    asyncio.create_task(self._ensure_fonts_once())
                    if need_emoji:
                        # 额外确保 emoji 资产
                        try:
                            asyncio.create_task(asyncio.to_thread(ensure_emoji_assets, True))
                        except Exception:
                            pass
            except RuntimeError:
                pass
            return json_response({"ok": True, "config": self.cfg_mgr.config})
        except Exception as e:
            return error_response(f"保存失败: {e}", status_code=500)

    async def _api_get_stats(self):
        return json_response({
            "ok": True,
            "stats": self.cfg_mgr.get_stats(),
        })

    def _find_all_bots(self, max_age: float = 60.0) -> List[Any]:
        """全量递归挖掘 context 中的所有 Bot / PlatformAdapter 实例（带 60s 缓存去重）"""
        now = time.time()
        if self._bots_cache and (now - self._bots_cache_time) < max_age:
            return list(self._bots_cache)
        targets = []
        visited = set()

        def _traverse(obj, depth=0):
            if depth > 4 or obj is None:
                return
            oid = id(obj)
            if oid in visited:
                return
            visited.add(oid)

            try:
                if any(callable(getattr(obj, m, None)) for m in ("call_action", "call_api", "get_group_list")):
                    targets.append(obj)
            except Exception:
                pass

            for attr in dir(obj):
                if attr.startswith("__"):
                    continue
                lower = attr.lower()
                if any(k in lower for k in ("platform", "adapter", "bot", "client", "connection", "ws", "manager")):
                    try:
                        val = getattr(obj, attr, None)
                        if val is None or callable(val):
                            continue
                        if isinstance(val, (list, tuple, set)):
                            for item in val:
                                _traverse(item, depth + 1)
                        elif isinstance(val, dict):
                            for item in val.values():
                                _traverse(item, depth + 1)
                        else:
                            _traverse(val, depth + 1)
                    except Exception:
                        pass

        try:
            _traverse(self.context, 0)
        except Exception:
            pass
        # 按 id 去重
        deduped = list({id(b): b for b in targets}.values())
        self._bots_cache = deduped
        self._bots_cache_time = now
        return list(deduped)

    async def _call_bot_group_list(self, bot: Any) -> List[Dict[str, str]]:
        """单个 Bot 上尝试多种方式拉取群列表（call_api / 直接方法 / call_action）"""
        # 1. 直接方法 get_group_list()
        try:
            fn = getattr(bot, "get_group_list", None)
            if callable(fn):
                res = await asyncio.wait_for(fn(), timeout=5.0)
                parsed = self._parse_group_list_res(res)
                if parsed:
                    return parsed
        except Exception:
            pass
        # 2. 通用调用器
        for caller_name in ("call_api", "call_action"):
            try:
                caller = getattr(bot, caller_name, None)
                if not callable(caller):
                    continue
                for act in ("get_group_list", "getGroupList", "get_groups", "list_groups"):
                    try:
                        res = await asyncio.wait_for(caller(act), timeout=5.0)
                        parsed = self._parse_group_list_res(res)
                        if parsed:
                            return parsed
                    except Exception:
                        continue
            except Exception:
                continue
        return []

    @staticmethod
    def _parse_group_list_res(res: Any) -> List[Dict[str, str]]:
        """解析各适配器返回的群列表（兼容 list / {data:[...]} 包裹格式）"""
        if isinstance(res, dict):
            for key in ("data", "groups", "list"):
                if isinstance(res.get(key), list):
                    res = res[key]
                    break
            else:
                return []
        if not isinstance(res, list):
            return []
        out = []
        for g in res:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("group_id") or g.get("id") or "")
            gname = str(g.get("group_name") or g.get("name") or gid)
            if gid:
                out.append({"gid": gid, "group_name": gname})
        return out

    async def _api_fetch_groups(self):
        """主动向适配器与已记录消息拉取所有群组信息"""
        groups: List[Dict[str, str]] = []
        seen_gids = set()

        # 1. 尝试从 Bot 实例获取真实群列表（并发拉取，3s 超时）
        try:
            bots = self._find_all_bots()
            if bots:
                results = await asyncio.gather(
                    *(self._call_bot_group_list(b) for b in bots[:4]),
                    return_exceptions=True,
                )
                for res in results:
                    if isinstance(res, list):
                        for g in res:
                            if g["gid"] not in seen_gids:
                                seen_gids.add(g["gid"])
                                groups.append(g)
                        if groups:
                            break
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 探测平台群列表异常: {e}")

        # 2. 合并历史已捕获群聊
        for gid, info in self._seen_groups.items():
            if gid not in seen_gids:
                seen_gids.add(gid)
                groups.append({"gid": gid, "group_name": str(info.get("group_name") or gid)})

        return json_response({"ok": True, "groups": groups})

    async def _api_render_preview(self):
        """实时渲染预览图片"""
        try:
            payload = await request.json(default={})

            # 优先从普通 text 获取，防止 Base64 编解码不一致造成乱码
            text = str(payload.get("text", "") or "").strip()
            if not text:
                b64_t = str(payload.get("text_base64", "") or "").strip()
                if b64_t:
                    try:
                        text = base64.b64decode(b64_t).decode("utf-8", errors="ignore")
                    except Exception:
                        pass

            if not text:
                text = (
                    "# 实时预览测试\n"
                    "这是一个用于测试消息转图片效果的示例。\n"
                    "- 精致的卡片与柔和阴影\n"
                    "- 随机闪烁星芒点缀背景\n"
                    "> 追求极致交互与美感！\n\n"
                    "```python\nprint('Hello World!')\n```"
                )

            style = str(payload.get("style") or self.cfg_mgr.config.get("style", "ios"))
            theme_mode = str(payload.get("theme_mode") or self.cfg_mgr.config.get("theme_mode", "light"))
            star_background = bool(payload.get("star_background", self.cfg_mgr.config.get("star_background", True)))
            star_density = str(payload.get("star_density") or self.cfg_mgr.config.get("star_density", "medium"))
            mosaic_mode = str(payload.get("mosaic_mode", "none"))
            mosaic_type = str(payload.get("mosaic_type") or self.cfg_mgr.config.get("mosaic_type", "pixel"))
            emoji_remote = bool(payload.get("emoji_remote", self.cfg_mgr.config.get("emoji_remote", True)))
            mosaic_half_pos = str(payload.get("mosaic_half_pos") or self.cfg_mgr.config.get("mosaic_half_pos", "bottom"))
            try:
                font_scale = int(payload.get("font_scale", self.cfg_mgr.config.get("font_scale", 100)) or 100)
            except Exception:
                font_scale = 100
            _es_prev = str(payload.get("emoji_style", self.cfg_mgr.config.get("emoji_style", "none")) or "none").lower()
            if _es_prev not in ("none", "ios", "android", "windows"):
                _es_prev = "none"
            emoji_remote_prev = _es_prev != "none"
            # 预览的违规词：前端显式传则用，否则尝试从文本自动提取（使半字马赛克展示为逐字效果，而非回退区域）
            vw_raw = payload.get("violation_words")
            violation_words = []
            if isinstance(vw_raw, list):
                violation_words = [str(x).strip() for x in vw_raw if str(x).strip()]
            elif isinstance(vw_raw, str) and vw_raw.strip():
                violation_words = [s.strip() for s in re.split(r"[,;，；\n]+", vw_raw) if s.strip()]
            if not violation_words and mosaic_mode in ("half", "full"):
                try:
                    _hit, _matched = self.moderator.check_keywords(text)
                    if _matched:
                        violation_words = _matched[:5]
                    elif "赌博" in text or "诈骗" in text:
                        violation_words = [w for w in ["赌博", "诈骗"] if w in text]
                except Exception:
                    pass

            img = await asyncio.to_thread(
                MessageImageRenderer.render,
                text=text,
                style=style,
                theme_mode=theme_mode,
                star_background=star_background,
                star_density=star_density,
                mosaic_mode=mosaic_mode,
                mosaic_type=mosaic_type,
                mosaic_half_pos=mosaic_half_pos,
                violation_words=violation_words,
                font_scale=font_scale,
                emoji_style=_es_prev,
                emoji_remote=emoji_remote_prev,
            )

            # 优化预览图：等比缩放后单次 JPEG 编码，文件与 Base64 复用同一份字节
            preview_img = img.copy()
            if preview_img.width > 680:
                scale = 680 / preview_img.width
                preview_img = preview_img.resize(
                    (680, int(preview_img.height * scale)),
                    Image.LANCZOS,
                )

            preview_path = self.cache_dir / "preview_latest.jpg"
            preview_rgb = preview_img.convert("RGB")
            buf = io.BytesIO()
            preview_rgb.save(buf, format="JPEG", quality=92, optimize=True, subsampling=0)
            jpeg_bytes = buf.getvalue()
            try:
                preview_path.write_bytes(jpeg_bytes)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 预览图落盘失败: {e}")
            b64_str = base64.b64encode(jpeg_bytes).decode("utf-8")

            t_stamp = int(time.time() * 1000)
            return json_response({
                "ok": True,
                "image_url": f"/{PLUGIN_NAME}/preview_img?t={t_stamp}",
                "image_base64": f"data:image/jpeg;base64,{b64_str}",
                "width": img.width,
                "height": img.height,
            })
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 生成预览失败: {e}")
            return error_response(f"生成预览失败: {e}", status_code=500)

    async def _api_get_preview_img(self):
        """流式输出最新预览图文件，避免 IPC 传输大 JSON 字符串"""
        img_path = self.cache_dir / "preview_latest.jpg"
        if not img_path.exists():
            return error_response("预览图不存在", status_code=404)
        if FileResponse is not None:
            try:
                return FileResponse(content=img_path.read_bytes(), media_type="image/jpeg")
            except Exception:
                pass
        try:
            from astrbot.api.web import file_response
            return file_response(img_path)
        except Exception:
            return error_response("输出图片流失败", status_code=500)

    async def _api_reset_config(self):
        from .core.config import DEFAULT_CONFIG
        self.cfg_mgr.config = dict(DEFAULT_CONFIG)
        self.cfg_mgr.save()
        self._group_cache_sig = None
        try:
            configure_fonts(self.cfg_mgr.config, self.cfg_mgr.data_dir)
        except Exception:
            pass
        self.moderator = ContentModerator(self.cfg_mgr.config)
        return json_response({"ok": True, "config": self.cfg_mgr.config})

    async def _api_fonts_status(self):
        try:
            return json_response({"ok": True, "fonts": get_font_status()})
        except Exception as e:
            return error_response(f"查询字体状态失败: {e}", status_code=500)

    async def _api_fonts_download(self):
        """手动触发下载（中文字体 + Emoji 全彩字体 + 自定义直链），放线程池执行"""
        try:
            res = await asyncio.to_thread(download_missing_fonts)
            dl = list(res.get("downloaded", []))
            err = str(res.get("error", "") or "")
            try:
                if bool(self.cfg_mgr.config.get("emoji_remote", True)):
                    eres = await asyncio.to_thread(ensure_emoji_assets, True)
                    dl.extend(eres.get("downloaded", []))
                    if eres.get("error"):
                        err = (err + "; " + eres["error"]).strip("; ")
            except Exception as e:
                err = (err + f"; Emoji: {e}").strip("; ")
            try:
                configure_fonts(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception:
                pass
            ok = bool(dl) or not err
            return json_response({"ok": ok, "downloaded": dl, "error": err})
        except Exception as e:
            return error_response(f"字体下载失败: {e}", status_code=500)

    def _fonts_data_dir(self):
        try:
            from core.renderer import FONT_DATA_SUBDIR
        except (ImportError, ValueError):
            from .core.renderer import FONT_DATA_SUBDIR
        d = self.cfg_mgr.data_dir / FONT_DATA_SUBDIR
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    async def _api_fonts_files(self):
        """列出持久化目录中的字体文件（可删除的只有这些，随包/系统字体只读）"""
        try:
            from core.renderer import _is_usable_font
        except (ImportError, ValueError):
            from .core.renderer import _is_usable_font
        try:
            d = self._fonts_data_dir()
            files = []
            if d.is_dir():
                for p in sorted(d.iterdir()):
                    if p.is_file() and p.suffix.lower() in (".ttf", ".ttc", ".otf"):
                        try:
                            size_kb = round(p.stat().st_size / 1024, 1)
                        except Exception:
                            size_kb = 0
                        files.append({
                            "name": p.name,
                            "size_kb": size_kb,
                            "usable": bool(_is_usable_font(str(p))),
                        })
            return json_response({"ok": True, "dir": str(d), "files": files})
        except Exception as e:
            return error_response(f"列出字体失败: {e}", status_code=500)

    async def _api_fonts_delete(self):
        """删除持久化目录中的指定字体文件（防穿越校验）"""
        try:
            payload = await request.json(default={})
            name = str(payload.get("name", "") or "").strip()
            # 仅允许纯文件名，拒绝任何路径成分
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                return error_response("非法文件名", status_code=400)
            if not name.lower().endswith((".ttf", ".ttc", ".otf", ".dfont")):
                return error_response("仅允许删除字体文件", status_code=400)
            # 先清缓存释放句柄
            try:
                from core.renderer import clear_font_cache
            except (ImportError, ValueError):
                from .core.renderer import clear_font_cache
            try:
                clear_font_cache()
            except Exception:
                pass
            target = self._fonts_data_dir() / name
            if not target.is_file():
                alt = self.cfg_mgr.data_dir / name
                if alt.is_file():
                    target = alt
                else:
                    return error_response("文件不存在", status_code=404)
            
            deleted_ok = False
            for _ in range(3):
                try:
                    target.unlink(missing_ok=True)
                    deleted_ok = True
                    break
                except Exception:
                    clear_font_cache()
                    await asyncio.sleep(0.05)
            if not deleted_ok:
                return error_response(f"文件正在被占用，删除失败", status_code=500)

            # 若删除的是当前自定义字体，自动清空配置避免指向不存在的文件
            try:
                cfg = self.cfg_mgr.config
                need_save = False
                for k in ("custom_font_path", "custom_bold_font_path", "custom_font_url"):
                    v = str(cfg.get(k, "") or "")
                    if v and (v == name or v.endswith("/" + name) or v.endswith("\\" + name)):
                        cfg[k] = ""
                        need_save = True
                if cfg.get("font_source") == "custom" and not cfg.get("custom_font_path"):
                    cfg["font_source"] = "auto"
                    need_save = True
                # 同时清空群聊专属配置中引用该字体的项
                raw_grp = cfg.get("group_configs", {})
                if isinstance(raw_grp, dict):
                    for _, gcfg in raw_grp.items():
                        if isinstance(gcfg, dict):
                            gfont = str(gcfg.get("custom_font_path", "") or "")
                            if gfont and (gfont == name or gfont.endswith("/" + name) or gfont.endswith("\\" + name)):
                                gcfg["custom_font_path"] = ""
                                need_save = True
                if need_save:
                    self.cfg_mgr.save()
            except Exception:
                pass
            try:
                configure_fonts(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception:
                pass
            return json_response({"ok": True, "deleted": name, "fonts": get_font_status()})
        except Exception as e:
            return error_response(f"删除失败: {e}", status_code=500)

    async def _api_fonts_curated_delete(self):
        try:
            payload = await request.json(default={})
            cid = str(payload.get("id", "") or "").strip()
            if not cid:
                return error_response("缺少字体 ID", status_code=400)
            res = await asyncio.to_thread(delete_curated_font, cid)
            try:
                configure_fonts(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception:
                pass
            return json_response({"ok": res.get("ok", False), **res, "fonts": get_font_status()})
        except Exception as e:
            return error_response(f"删除失败: {e}", status_code=500)

    async def _api_fonts_curated(self):
        try:
            return json_response({"ok": True, "curated": get_curated_fonts_status()})
        except Exception as e:
            return error_response(f"获取精选列表失败: {e}", status_code=500)

    async def _api_fonts_curated_install(self):
        try:
            payload = await request.json(default={})
            cid = str(payload.get("id", "") or "").strip()
            if not cid:
                return error_response("缺少字体 id", status_code=400)
            res = await asyncio.to_thread(download_curated_font, cid)
            if res.get("ok"):
                try:
                    from core.renderer import _FONT_CONF
                except (ImportError, ValueError):
                    from .core.renderer import _FONT_CONF
                try:
                    self.cfg_mgr.save({
                        "font_source": _FONT_CONF.get("font_source", "custom"),
                        "custom_font_path": _FONT_CONF.get("custom_font_path", ""),
                        "custom_bold_font_path": _FONT_CONF.get("custom_bold_font_path", ""),
                    })
                except Exception:
                    pass
            return json_response({"ok": res.get("ok", False), **res})
        except Exception as e:
            return error_response(f"安装失败: {e}", status_code=500)

    async def _api_emoji_packs(self):
        try:
            return json_response({"ok": True, "packs": get_emoji_packs_status()})
        except Exception as e:
            return error_response(f"获取 Emoji 列表失败: {e}", status_code=500)

    async def _api_emoji_download(self):
        try:
            payload = await request.json(default={})
            style = str(payload.get("style", "") or payload.get("id", "") or "").strip().lower()
            if style not in ("ios", "android", "windows"):
                return error_response("未知样式", status_code=400)
            res = await asyncio.to_thread(download_emoji_pack, style)
            # 保存选择
            try:
                self.cfg_mgr.config["emoji_style"] = style
                self.cfg_mgr.save({"emoji_style": style})
                from core.renderer import configure_fonts as _cf
            except (ImportError, ValueError):
                from .core.renderer import configure_fonts as _cf
            try:
                _cf(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception:
                pass
            return json_response({"ok": res.get("ok", False), **res})
        except Exception as e:
            return error_response(f"下载失败: {e}", status_code=500)

    async def _api_emoji_delete(self):
        try:
            payload = await request.json(default={})
            style = str(payload.get("style", "") or payload.get("id", "") or "").strip().lower()
            if style not in ("ios", "android", "windows"):
                return error_response("未知样式", status_code=400)
            res = await asyncio.to_thread(delete_emoji_pack, style)
            # 若删除的是当前样式则切回 none
            try:
                cur = str(self.cfg_mgr.config.get("emoji_style", "") or "").lower()
                if cur == style:
                    self.cfg_mgr.config["emoji_style"] = "none"
                    self.cfg_mgr.save({"emoji_style": "none"})
                    from core.renderer import configure_fonts as _cf2
                else:
                    from core.renderer import configure_fonts as _cf2
            except (ImportError, ValueError):
                from .core.renderer import configure_fonts as _cf2
            try:
                _cf2(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception:
                pass
            return json_response({"ok": res.get("ok", False), **res})
        except Exception as e:
            return error_response(f"删除失败: {e}", status_code=500)

    async def _api_ai_providers(self):
        try:
            providers = []
            # 尝试从 context 获取已接入模型
            try:
                # 常见接口：context.providers / context.llm_providers
                for attr in ("providers", "llm_providers", "provider_manager", "model_list"):
                    mgr = getattr(self.context, attr, None)
                    if mgr:
                        try:
                            # 若为 dict
                            if isinstance(mgr, dict):
                                for k, v in mgr.items():
                                    providers.append(str(k))
                            elif hasattr(mgr, "__iter__"):
                                for p in mgr:
                                    name = str(getattr(p, "model_name", "") or getattr(p, "id", "") or getattr(p, "name", "") or str(p))
                                    if name and name not in providers:
                                        providers.append(name)
                        except Exception:
                            continue
                # 兜底：get_using_provider
                if not providers:
                    try:
                        p = self.context.get_using_provider()
                        if p:
                            name = str(getattr(p, "model_name", "") or getattr(p, "id", "") or "default")
                            if name not in providers:
                                providers.append(name)
                    except Exception:
                        pass
            except Exception:
                pass
            # 去重并限制
            providers = [p for p in providers if p][:20]
            return json_response({"ok": True, "providers": providers})
        except Exception as e:
            return error_response(f"获取失败: {e}", status_code=500)

    # ==========================================
    # 缓存周期清理（可停止 + 数量上限兜底）
    # ==========================================
    async def _clean_old_cache_periodic(self):
        assert self._cache_stop_event is not None
        while not self._cache_stop_event.is_set():
            try:
                await asyncio.wait_for(self._cache_stop_event.wait(), timeout=1800)
                break  # 被置位说明插件卸载，直接退出
            except asyncio.TimeoutError:
                pass
            try:
                now = time.time()
                cached_files = sorted(
                    [p for p in self.cache_dir.iterdir() if p.is_file() and (p.name.startswith("t2i_") or p.name.startswith("test_"))],
                    key=lambda p: p.stat().st_mtime,
                )
                for p in cached_files:
                    try:
                        if now - p.stat().st_mtime > 3600:  # 超过 1 小时清除
                            p.unlink(missing_ok=True)
                    except Exception:
                        pass
                # 数量兜底：超过 300 张删最旧的（防止高频群聊打爆磁盘）
                remain = sorted(
                    [p for p in self.cache_dir.iterdir() if p.is_file() and (p.name.startswith("t2i_") or p.name.startswith("test_"))],
                    key=lambda p: p.stat().st_mtime,
                )
                for p in remain[:-300]:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
            except Exception:
                pass


__all__ = ["Msg2ImgPlugin"]
