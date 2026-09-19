# -*- coding: utf-8 ---
"""渲染管线与消息钩子：审查+渲染+落盘、适配器补丁、装饰器钩子。"""

import asyncio
import io
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

try:
    from .common import (
        AstrImage, AstrMessageEvent, PLUGIN_NAME, Plain, logger,
        RENDER_MAX_CHARS, _URL_PATTERN, _clean_urls, _compress_level,
        _save_semaphore,
    )
    from .renderer import MessageImageRenderer
except (ImportError, ValueError):
    from core.common import (
        AstrImage, AstrMessageEvent, PLUGIN_NAME, Plain, logger,
        RENDER_MAX_CHARS, _URL_PATTERN, _clean_urls, _compress_level,
        _save_semaphore,
    )
    from core.renderer import MessageImageRenderer


class HandlersMixin:
    """HandlersMixin：由 Msg2ImgPlugin 多继承组合，依赖其 __init__ 初始化的属性。"""


    # ==========================================
    # 消息入口优先拦截（针对 xbbot 等由 @event_message_type 驱动的业务插件）
    # 设置高优先级 priority=100，确保在其它插件之前检查并打标
    # ==========================================
    async def on_all_message_entry(self, event: AstrMessageEvent):
        # 懒启动后台任务 + 记录已见群聊 + 顺手给本次事件的 bot 打补丁
        # （event.bot 就是发送用的同一实例，零发现延迟；后台巡检兜底重连的新实例）
        self._ensure_bg_tasks()
        self._remember_group(event)
        self._patch_event_bot(event)

    def _patch_event_bot(self, event: AstrMessageEvent) -> None:
        """顺手给本次事件的 bot 挂载发送补丁：幂等，仅 aiocqhttp/OneBot 系。

        后台巡检有 2s×15 的发现延迟，首条消息常赶在挂载成功前发出；
        event.bot 与发送是同一实例，在这里顺手挂载可关闭该时间窗。
        """
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return
            try:
                _fn = getattr(event, "get_platform_name", None)
                pname = str(_fn() or "").lower() if callable(_fn) else ""
            except Exception:
                pname = ""
            if pname:
                if pname != "aiocqhttp":
                    return
            else:
                # 拿不到平台名时用启发式，避免误伤其他平台
                try:
                    t = type(bot)
                    hay = f"{getattr(t, '__name__', '')} {getattr(t, '__module__', '')}".lower()
                except Exception:
                    return
                if "cqhttp" not in hay and "onebot" not in hay:
                    return
            self._patch_bot_send(bot)
        except Exception:
            pass



    async def _hook_adapters_when_ready(self):
        """等待适配器就绪后挂载钩子；之后每 60 秒巡检补扫，重连/晚连的适配器也能挂上"""
        stop = self._cache_stop_event
        for _ in range(15):
            if stop is not None and stop.is_set():
                return
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
        while stop is None or not stop.is_set():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            if stop is not None and stop.is_set():
                break
            try:
                for b in self._find_all_bots(max_age=0):
                    self._patch_bot_send(b)
            except Exception:
                pass



    @staticmethod
    def _is_bot_patched(bot_inst: Any) -> bool:
        """是否已挂载补丁。注意：aiocqhttp 的 Api.__getattr__ 会给任意属性名返回
        truthy 的 partial Phantom，getattr 判定永远为真，必须查实例 __dict__
        （或 `is True` 精确比对）才能得到真实结论。"""
        try:
            return vars(bot_inst).get("_msg2img_patched", False) is True
        except TypeError:
            return getattr(bot_inst, "_msg2img_patched", False) is True

    @staticmethod
    def _take_bot_orig(bot_inst: Any, name: str) -> Any:
        """取出并移除存档的原函数（仅在确认已挂载后调用）"""
        try:
            return vars(bot_inst).pop(name, None)
        except (TypeError, AttributeError):
            return getattr(bot_inst, name, None)

    @staticmethod
    def _is_bind_error(exc: BaseException) -> bool:
        """是否为参数绑定期 TypeError（网络 I/O 之前抛出，重试不会导致重复发送）"""
        try:
            msg = str(exc)
        except Exception:
            return False
        return ("takes " in msg and "positional argument" in msg) or (
            "unexpected keyword argument" in msg
        ) or ("missing " in msg and "required positional argument" in msg)

    @staticmethod
    def _describe_callable(fn: Any, _depth: int = 0) -> str:
        """调用链点名：类型/qualname/模块/partial 链（最多 3 层），用于定位中间包装器"""
        try:
            if _depth > 3:
                return "..."
            import functools as _ft
            if isinstance(fn, _ft.partial):
                inner = HandlersMixin._describe_callable(fn.func, _depth + 1)
                try:
                    keys = sorted((fn.keywords or {}).keys())
                except Exception:
                    keys = []
                return f"partial({inner}, keywords={keys})"
            qn = getattr(fn, "__qualname__", None) or type(fn).__name__
            mod = getattr(fn, "__module__", "?")
            return f"{type(fn).__name__}:{mod}.{qn}"
        except Exception:
            try:
                return repr(fn)[:120]
            except Exception:
                return "<?>"

    @classmethod
    def _resolve_genuine_call(cls, bot_inst: Any) -> Any:
        """沿 MRO 找真正的 call_action：跳过我们标记的包装器，解开 __wrapped__ 链，
        返回绑定到实例的 genuine 方法；找不到返回 None。"""
        try:
            mro = type(bot_inst).__mro__
        except Exception:
            return None
        for klass in mro:
            try:
                fn = klass.__dict__.get("call_action", None)
            except Exception:
                continue
            if fn is None:
                continue
            if isinstance(fn, (staticmethod, classmethod)):
                try:
                    fn = fn.__func__
                except Exception:
                    continue
            cur, bad, depth = fn, False, 0
            while depth < 8:
                try:
                    if getattr(cur, "_xbimg_wrapper_", False):
                        bad = True
                        break
                    nxt = getattr(cur, "__wrapped__", None)
                except Exception:
                    break
                if nxt is None:
                    break
                cur, depth = nxt, depth + 1
            if bad:
                continue
            try:
                return cur.__get__(bot_inst, type(bot_inst))
            except Exception:
                continue
        return None

    def _patch_bot_send(self, bot_inst: Any):
        """为 OneBot/aiocqhttp 等适配器实例挂载无损转图补丁。

        包装器必须签名透明 (*args/**kwargs 原样转发)：CQHttp 系客户端内部多为
        `self.call_action(action, params)` 位置参数调用，写死签名会直接炸掉全量发送。
        首次原样转发若抛参数绑定型 TypeError（I/O 之前失败，不会重复发送），
        则归一化为关键字形式重试一次，适配各种历史客户端签名。
        """
        import functools as _ft

        def _safe_wraps(orig):
            try:
                return _ft.wraps(orig)
            except Exception:
                return lambda fn: fn

        if self._is_bot_patched(bot_inst):
            return

        # 1. 拦截 bot.send_group_msg（存原函数，重载时可还原）
        if hasattr(bot_inst, "send_group_msg") and callable(getattr(bot_inst, "send_group_msg")):
            orig_send_group = bot_inst.send_group_msg

            @_safe_wraps(orig_send_group)
            async def patched_send_group(*args, **kwargs):
                try:
                    # 兼容位置/关键字两种调用：send_group_msg(group_id, message[, ...])
                    _gid = kwargs.get("group_id", args[0] if len(args) > 0 else None)
                    _msg = kwargs.get("message", args[1] if len(args) > 1 else None)
                    if _gid is not None and _msg is not None:
                        _new = await self._transform_onebot_message(str(_gid), _msg)
                        if "message" in kwargs:
                            kwargs["message"] = _new
                        elif len(args) > 1:
                            args = (args[0], _new) + tuple(args[2:])
                        else:
                            kwargs["message"] = _new
                except Exception as e:
                    logger.debug(f"[{PLUGIN_NAME}] 拦截 send_group_msg 失败: {e}")
                try:
                    return await orig_send_group(*args, **kwargs)
                except TypeError as e:
                    if not self._is_bind_error(e):
                        raise
                    # 归一化重试：位置参数按 OneBot 顺序具名化（group_id, message, auto_escape）
                    _rk = dict(kwargs)
                    if "group_id" not in _rk and len(args) > 0:
                        _rk["group_id"] = args[0]
                    if "message" not in _rk and len(args) > 1:
                        _rk["message"] = args[1]
                    if len(args) > 2 and "auto_escape" not in _rk and isinstance(args[2], bool):
                        _rk["auto_escape"] = args[2]
                    try:
                        logger.warning(
                            f"[{PLUGIN_NAME}] send_group_msg 原样转发失败({e})，"
                            f"已归一化为关键字重试 args={len(args)} keys={sorted(_rk)}"
                        )
                        return await orig_send_group(**_rk)
                    except TypeError as e2:
                        if not self._is_bind_error(e2):
                            raise
                        # 终极兜底：orig 链下游仍有位置委托中间件时，绕过它直调 genuine；
                        # 找不到 genuine 则抛归一化错误（附调用链点名供定位）
                        _genuine = self._resolve_genuine_call(bot_inst)
                        try:
                            _same = bool(_genuine is not None and _genuine == orig_send_group)
                        except Exception:
                            _same = True
                        if _genuine is None or _same:
                            logger.error(
                                f"[{PLUGIN_NAME}] send_group_msg 归一化重试仍失败({e2})，"
                                f"orig={self._describe_callable(orig_send_group)}"
                            )
                            raise
                        logger.warning(
                            f"[{PLUGIN_NAME}] send_group_msg 归一化重试仍失败({e2})，"
                            f"orig={self._describe_callable(orig_send_group)}，"
                            f"直调 genuine 兜底"
                        )
                        try:
                            return await _genuine("send_group_msg", **_rk)
                        except TypeError as e3:
                            logger.error(
                                f"[{PLUGIN_NAME}] send_group_msg genuine 直调仍失败({e3})，"
                                f"orig={self._describe_callable(orig_send_group)} "
                                f"genuine={self._describe_callable(_genuine)}"
                            )
                            raise

            patched_send_group._xbimg_wrapper_ = True
            bot_inst.send_group_msg = patched_send_group
            try:
                bot_inst._msg2img_orig_send_group_msg = orig_send_group
            except Exception:
                pass

        # 2. 拦截通用 call_action("send_group_msg", ...) 或 call_action("send_msg", ...)
        if hasattr(bot_inst, "call_action") and callable(getattr(bot_inst, "call_action")):
            orig_call_action = bot_inst.call_action

            @_safe_wraps(orig_call_action)
            async def patched_call_action(action, *args, **kwargs):
                try:
                    if action in ("send_group_msg", "send_msg"):
                        # 形式 A：call_action(action, group_id=.., message=..)
                        # 形式 B：call_action(action, {"group_id":.., "message":..})
                        _params = None
                        if args and isinstance(args[0], dict):
                            _params = args[0]
                        _gid = kwargs.get("group_id", (_params.get("group_id") if _params else None))
                        _msg = kwargs.get("message", (_params.get("message") if _params else None))
                        if _gid and _msg is not None:
                            _new = await self._transform_onebot_message(str(_gid), _msg)
                            if "message" in kwargs:
                                kwargs["message"] = _new
                            elif _params is not None:
                                _params = dict(_params)
                                _params["message"] = _new
                                args = (_params,) + tuple(args[1:])
                            else:
                                kwargs["message"] = _new
                except Exception as e:
                    logger.debug(f"[{PLUGIN_NAME}] 拦截 call_action 失败: {e}")
                try:
                    return await orig_call_action(action, *args, **kwargs)
                except TypeError as e:
                    if not self._is_bind_error(e):
                        raise
                    # 归一化重试：位置 params 字典并入关键字（调用方关键字优先）；
                    # 无可归一化的位置参数时直接抛原错，避免静默丢参数
                    _rk = dict(kwargs)
                    _merged = False
                    for _a in args:
                        if isinstance(_a, dict):
                            for _k, _v in _a.items():
                                _rk.setdefault(_k, _v)
                            _merged = True
                    if not _merged:
                        raise
                    try:
                        logger.warning(
                            f"[{PLUGIN_NAME}] call_action({action}) 原样转发失败({e})，"
                            f"已归一化为关键字重试 keys={sorted(_rk)}"
                        )
                        return await orig_call_action(action, **_rk)
                    except TypeError as e2:
                        if not self._is_bind_error(e2):
                            raise
                        _genuine = self._resolve_genuine_call(bot_inst)
                        try:
                            _same = bool(_genuine is not None and _genuine == orig_call_action)
                        except Exception:
                            _same = True
                        if _genuine is None or _same:
                            logger.error(
                                f"[{PLUGIN_NAME}] call_action({action}) 归一化重试仍失败({e2})，"
                                f"orig={self._describe_callable(orig_call_action)}"
                            )
                            raise
                        logger.warning(
                            f"[{PLUGIN_NAME}] call_action({action}) 归一化重试仍失败({e2})，"
                            f"orig={self._describe_callable(orig_call_action)}，"
                            f"直调 genuine 兜底"
                        )
                        try:
                            return await _genuine(action, **_rk)
                        except TypeError as e3:
                            logger.error(
                                f"[{PLUGIN_NAME}] call_action({action}) genuine 直调仍失败({e3})，"
                                f"orig={self._describe_callable(orig_call_action)} "
                                f"genuine={self._describe_callable(_genuine)}"
                            )
                            raise

            patched_call_action._xbimg_wrapper_ = True
            bot_inst.call_action = patched_call_action
            try:
                bot_inst._msg2img_orig_call_action = orig_call_action
            except Exception:
                pass

        setattr(bot_inst, "_msg2img_patched", True)
        try:
            logger.info(
                f"[{PLUGIN_NAME}] 成功挂载适配器群消息转图拦截钩子 "
                f"({type(bot_inst).__name__}@{type(bot_inst).__module__} "
                f"orig_send={self._describe_callable(getattr(bot_inst, '_msg2img_orig_send_group_msg', None))} "
                f"orig_call={self._describe_callable(getattr(bot_inst, '_msg2img_orig_call_action', None))})"
            )
        except Exception:
            logger.info(f"[{PLUGIN_NAME}] 成功挂载适配器群消息转图拦截钩子 (send_group_msg & call_action)")



    @staticmethod
    def _unpatch_bot_send(bot_inst: Any) -> None:
        """还原单个适配器实例的补丁（热重载时让新实例能重新挂载）"""
        try:
            if not HandlersMixin._is_bot_patched(bot_inst):
                return
            orig_send = HandlersMixin._take_bot_orig(bot_inst, "_msg2img_orig_send_group_msg")
            if callable(orig_send):
                try:
                    bot_inst.send_group_msg = orig_send
                except Exception:
                    pass
            orig_call = HandlersMixin._take_bot_orig(bot_inst, "_msg2img_orig_call_action")
            if callable(orig_call):
                try:
                    bot_inst.call_action = orig_call
                except Exception:
                    pass
            try:
                vars(bot_inst).pop("_msg2img_patched", None)
            except (TypeError, AttributeError):
                try:
                    if getattr(bot_inst, "_msg2img_patched", False) is True:
                        delattr(bot_inst, "_msg2img_patched")
                except Exception:
                    pass
        except Exception:
            pass



    def _perf_enabled(self) -> bool:
        """性能日志开关（关闭时调用方不计时，零开销）。

        兼容原生配置侧可能落下的字符串/数字形态（"true"/"1"/1 等）。
        """
        try:
            v = self.cfg_mgr.config.get("perf_log", False)
        except Exception:
            return False
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    @staticmethod
    def _perf_vals(perf_out) -> tuple:
        """从渲染回填字典取值 (mosaic, prefetch, draw, layout)，缺省全 0"""
        try:
            d = perf_out or {}
            return (
                float(d.get("mosaic_ms", 0.0)),
                float(d.get("prefetch_ms", 0.0)),
                float(d.get("draw_ms", 0.0)),
                float(d.get("layout_ms", 0.0)),
            )
        except Exception:
            return (0.0, 0.0, 0.0, 0.0)

    def _emit_perf_log(
        self,
        session: str,
        total_ms: float,
        mod_ms: float,
        mosaic_ms: float,
        prefetch_ms: float,
        imgs,
        img_paths,
        blocked: bool = False,
        draw_ms: float = 0.0,
        layout_ms: float = 0.0,
        save_ms: float = 0.0,
    ) -> None:
        """单行性能日志：grep `\\[性能\\]` 可直接捞出（仅 perf_log 开启时调用）"""
        try:
            total = len(img_paths) if img_paths else 0
            if blocked or total == 0:
                logger.info(
                    f"[xbimg] [性能] 会话{session}：总耗时 {total_ms:.0f}ms"
                    f"（安全审查 {mod_ms:.0f}ms · 打码 {mosaic_ms:.0f}ms · 下载 {prefetch_ms:.0f}ms）"
                    f"· blocked={str(bool(blocked))}"
                )
                return
            for idx, (im, p) in enumerate(zip(imgs, img_paths)):
                try:
                    w, h = int(getattr(im, "width", 0)), int(getattr(im, "height", 0))
                except Exception:
                    w, h = 0, 0
                try:
                    kb = int(Path(p).stat().st_size // 1024)
                except Exception:
                    kb = -1
                page = f"· 第{idx + 1}/{total}页" if total > 1 else ""
                logger.info(
                    f"[xbimg] [性能] 会话{session}：总耗时 {total_ms:.0f}ms"
                    f"（安全审查 {mod_ms:.0f}ms · 打码 {mosaic_ms:.0f}ms · 下载 {prefetch_ms:.0f}ms"
                    f" · 绘制 {draw_ms:.0f}ms · 排版 {layout_ms:.0f}ms · 落盘 {save_ms:.0f}ms）"
                    f"{page}· 图片 {w}x{h}/{kb}KB · blocked=False"
                )
        except Exception:
            pass


    async def _moderate_text(self, full_text: str, keyword_preset: Optional[str] = None):
        """纯审查步骤（不渲染）：返回 (mod_res, eff_text, mosaic_mode)。

        violation_only 门控用它先判断，非违规直接返回，避免付 PIL 渲染成本。
        """
        mod_res = await self.moderator.review(full_text, self.context, preset_name=keyword_preset)
        eff_text = full_text
        mosaic_mode = "none"
        if mod_res.is_violated:
            if mod_res.action == "block":
                return mod_res, eff_text, mosaic_mode
            if mod_res.action == "notice":
                eff_text = f"⚠️【内容安全提示】\n原消息触发安全审核：{mod_res.reason}。\n根据群聊合规要求，此条回复已被过滤拦截。"
            elif mod_res.action == "mosaic_half":
                mosaic_mode = "half"
            elif mod_res.action == "mosaic_full":
                mosaic_mode = "full"
        return mod_res, eff_text, mosaic_mode



    async def _render_moderated(
        self,
        eff_text: str,
        mod_res,
        mosaic_mode: str = "none",
        font_scale: Optional[int] = None,
        style: Optional[str] = None,
        theme_mode: Optional[str] = None,
        custom_font_path: str = "",
        custom_bold_font_path: str = "",
        perf_out: Optional[Dict] = None,
    ):
        """纯渲染步骤（审查已完成）：返回 List[Image]，失败返回 []。
        perf_out 非 None 时透传给 render_pages 回填 mosaic_ms/prefetch_ms；为 None 则零开销。"""
        # 超长输入只截渲染侧：审查已用全文判过违规，渲染截断不影响安全；
        # 避免几万字长文一次性画 10+ 张大图打满线程池与内存
        try:
            if len(eff_text) > RENDER_MAX_CHARS:
                eff_text = eff_text[:RENDER_MAX_CHARS] + "\n…内容过长已截断…"
        except Exception:
            pass
        cfg = self.cfg_mgr.config
        if font_scale is None:
            try:
                font_scale = int(cfg.get("font_scale", 100) or 100)
            except Exception:
                font_scale = 100
        eff_style = str(style or cfg.get("style", "ios")).lower()
        eff_theme = str(theme_mode or cfg.get("theme_mode", "light")).lower()
        try:
            # 兼容 emoji_style / emoji_remote
            _es = str(cfg.get("emoji_style", "") or "").strip().lower()
            if _es not in ("none", "ios", "android", "windows"):
                _es = "android" if bool(cfg.get("emoji_remote", True)) else "none"
                if _es == "android" and not str(cfg.get("emoji_style", "")):
                    _es = "none"
            try:
                page_max_h = int(cfg.get("page_max_height", 3000) or 3000)
            except Exception:
                page_max_h = 3000
            try:
                card_max_w = int(cfg.get("card_max_width", 640) or 640)
            except Exception:
                card_max_w = 640
            imgs = await asyncio.to_thread(
                MessageImageRenderer.render_pages,
                text=eff_text,
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
                page_max_h=page_max_h,
                card_max_width=card_max_w,
                custom_font_path=custom_font_path or "",
                custom_bold_font_path=custom_bold_font_path or "",
                perf_out=perf_out,
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 渲染失败: {e}")
            return []
        return imgs or []



    async def _review_and_render(
        self,
        full_text: str,
        font_scale: Optional[int] = None,
        style: Optional[str] = None,
        theme_mode: Optional[str] = None,
        keyword_preset: Optional[str] = None,
        custom_font_path: str = "",
        custom_bold_font_path: str = "",
    ):
        """统一审查+渲染管线：返回 (List[Image], violated, mosaic, blocked)。

        主路径与适配器劫持路径共用，消灭重复代码，保证统计口径一致。
        支持传入群级别覆盖的 style / theme_mode / font_scale / keyword_preset。
        超长内容分页输出多图，不再丢弃截断部分。
        """
        mod_res, eff_text, mosaic_mode = await self._moderate_text(
            full_text, keyword_preset=keyword_preset
        )
        if mod_res.is_violated and mod_res.action == "block":
            return [], True, False, True
        imgs = await self._render_moderated(
            eff_text, mod_res, mosaic_mode,
            font_scale=font_scale, style=style, theme_mode=theme_mode,
            custom_font_path=custom_font_path,
            custom_bold_font_path=custom_bold_font_path,
        )
        if not imgs:
            return [], mod_res.is_violated, mosaic_mode != "none", False
        return imgs, mod_res.is_violated, mosaic_mode != "none", False



    async def _save_render_image(self, img) -> Optional[Path]:
        """保存渲染图到缓存并返回路径（按三档力度压缩，用完即删：45 秒后自动清理）

        体积兜底：img_max_kb>0 且编码后超限，自动逐档降质重编码压到上限内
        （JPEG 降 quality/抽样，PNG 无损档只提高压缩比、画质不变）；
        未超限时输出与原来逐行执行时字节一致。
        """
        cfg = self.cfg_mgr.config
        lvl = _compress_level(cfg)
        # 手机屏适配：超宽图等比限宽（默认 1080，小米 13 这类 1080p 手机缩略图不再被裁）
        try:
            max_w = int(cfg.get("img_max_width", 1080) or 0)
        except Exception:
            max_w = 1080
        try:
            max_kb = int(cfg.get("img_max_kb", 800) or 0)
        except Exception:
            max_kb = 0
        cap_bytes = max_kb * 1024 if max_kb > 0 else 0

        # 三档压缩策略（首选项与原来完全一致；后备仅超限时启用）
        if lvl == "compact":
            plans = [
                (".jpg", {"format": "JPEG", "quality": 86, "subsampling": 0}, True),
                (".jpg", {"format": "JPEG", "quality": 80, "subsampling": 2}, True),
            ]
        elif lvl == "lossless":
            plans = [
                (".png", {"format": "PNG", "compress_level": 1}, False),
                (".png", {"format": "PNG", "compress_level": 6}, False),
            ]
        else:
            plans = [
                (".jpg", {"format": "JPEG", "quality": 94, "subsampling": 0}, True),
                (".jpg", {"format": "JPEG", "quality": 85, "subsampling": 1}, True),
                (".jpg", {"format": "JPEG", "quality": 80, "subsampling": 2}, True),
            ]

        def _do_encode() -> tuple:
            # 同步 worker 内完成限宽缩放 + 色彩转换 + 编码，避免大图操作阻塞事件循环
            nonlocal img
            try:
                if max_w > 0 and getattr(img, "width", 0) > max_w:
                    ratio = max_w / float(img.width)
                    img = img.resize((max_w, max(1, int(round(img.height * ratio)))), Image.LANCZOS)
            except Exception:
                pass
            fallback = (b"", ".jpg")
            for suffix, kwargs, need_rgb in plans:
                save_img = img.convert("RGB") if (need_rgb and img.mode != "RGB") else img
                buf = io.BytesIO()
                try:
                    save_img.save(buf, **kwargs)
                    data = buf.getvalue()
                finally:
                    try:
                        buf.close()
                    except Exception:
                        pass
                fallback = (data, suffix)
                if not cap_bytes or len(data) <= cap_bytes:
                    return data, suffix
            try:
                logger.warning(
                    f"[{PLUGIN_NAME}] 图片体积仍超上限({max_kb}KB)，已用最低画质输出"
                )
            except Exception:
                pass
            return fallback

        try:
            data, suffix = await asyncio.to_thread(_do_encode)
            if not data:
                return None
            img_filename = f"t2i_{int(time.time() * 1000)}_{os.urandom(3).hex()}{suffix}"
            img_path = self.cache_dir / img_filename
            await asyncio.to_thread(img_path.write_bytes, data)
            # 即时清理：45 秒后删除，避免堆积（必须在事件循环线程调度）
            self._schedule_delete(img_path, 45)
            return img_path
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 写入图片失败: {e}")
            return None



    async def _save_render_images(self, imgs) -> List[Path]:
        """批量落盘（超长分页多图）：多页并行编码落盘，返回成功路径列表（保序）"""
        imgs = list(imgs or [])
        if not imgs:
            return []
        if len(imgs) == 1:
            try:
                p = await self._save_render_image(imgs[0])
            except Exception:
                p = None
            return [p] if p is not None else []
        sem = _save_semaphore()

        async def _one(idx, im):
            async with sem:
                try:
                    return await self._save_render_image(im)
                except Exception as e:
                    logger.warning(f"[{PLUGIN_NAME}] 第 {idx + 1}/{len(imgs)} 页落盘失败: {e}")
                    return None

        # gather 保序：paths 顺序与输入页序一致
        results = await asyncio.gather(*(_one(i, im) for i, im in enumerate(imgs)), return_exceptions=True)
        ok = [p for p in results if isinstance(p, Path)]
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning(f"[{PLUGIN_NAME}] 第 {i + 1}/{len(imgs)} 页落盘异常: {r}")
            elif r is None:
                logger.warning(f"[{PLUGIN_NAME}] 第 {i + 1}/{len(imgs)} 页落盘返回空")
        return ok



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

            try:
                min_threshold = int(cfg.get("min_length_threshold", 1) or 1)
            except Exception:
                min_threshold = 1
            if len(full_text) < min_threshold:
                return message

            # 链接策略与主路径保持一致
            if _clean_urls(_URL_PATTERN.findall(full_text)):
                if str(cfg.get("link_mode", "as_image") or "as_image") == "keep_text":
                    return message

            # 仅违规时转图：AI 关闭时关键词预检未命中直接返回（省一次完整审查）；
            # AI 开启时跳过预检，直接走完整审查（关键词只扫一遍）
            render_trigger = str(cfg.get("render_trigger", "always") or "always")
            grp_custom = self._get_group_custom_config(gid)
            if render_trigger == "violation_only" and not bool(cfg.get("enable_ai_moderation", False)):
                _qp = grp_custom.get("keyword_preset") if isinstance(grp_custom, dict) else None
                quick_hit, _ = self.moderator.check_keywords(full_text, _qp)
                if not quick_hit:
                    return message

            # 转为图片（群组单独字体大小与样式，超长分页多图）
            eff_scale = self._get_group_font_scale(gid, grp_custom)
            _perf = self._perf_enabled()
            _t0 = time.perf_counter()
            _t_mod = _t0 if _perf else 0.0
            _perf_out: Optional[Dict] = {} if _perf else None
            # 先审查：仅违规时转图且无违规 → 直接返回，不付 PIL 渲染成本
            mod_res, eff_text, mosaic_mode = await self._moderate_text(
                full_text, keyword_preset=grp_custom.get("keyword_preset"),
            )
            _mod_ms = (time.perf_counter() - _t_mod) * 1000.0 if _perf else 0.0
            if render_trigger == "violation_only" and not mod_res.is_violated:
                return message
            if mod_res.is_violated and mod_res.action == "block":
                # 真拦截：记违规但不计总数；剥离文本段身体（保留 at/reply/媒体），
                # 剥空则留一个空文本段，保证发送结构有效且无违规内容外泄
                self.cfg_mgr.record_render(is_violated=True, count_total=False)
                if _perf:
                    try:
                        _total_ms = (time.perf_counter() - _t0) * 1000.0
                        self._emit_perf_log(f"群{gid}", _total_ms, _mod_ms, 0.0, 0.0, [], [], blocked=True)
                    except Exception:
                        pass
                kept = [
                    seg for seg in message
                    if isinstance(seg, dict) and seg.get("type") in ("at", "reply")
                ]
                media = [
                    seg for seg in message
                    if isinstance(seg, dict) and seg.get("type") in ("image", "record", "video", "file")
                ]
                kept.extend(media)
                if not kept:
                    kept = [{"type": "text", "data": {"text": ""}}]
                return kept
            imgs = await self._render_moderated(
                eff_text, mod_res, mosaic_mode,
                font_scale=eff_scale,
                style=grp_custom.get("style"),
                theme_mode=grp_custom.get("theme_mode"),
                custom_font_path=str(grp_custom.get("custom_font_path", "") or ""),
                custom_bold_font_path=str(grp_custom.get("custom_bold_font_path", "") or ""),
                perf_out=_perf_out,
            )
            _elapsed = int((time.perf_counter() - _t0) * 1000)
            violated, mosaic = mod_res.is_violated, mosaic_mode != "none"
            self.cfg_mgr.record_render(
                is_violated=violated, is_mosaic=mosaic,
                elapsed_ms=_elapsed if imgs else None,
            )
            if not imgs:
                return message
            if _perf:
                _t_save = time.perf_counter()
            img_paths = await self._save_render_images(imgs)
            if _perf:
                try:
                    _total_ms = (time.perf_counter() - _t0) * 1000.0
                    _mo, _pf, _dr, _la = self._perf_vals(_perf_out)
                    self._emit_perf_log(
                        f"群{gid}", _total_ms, _mod_ms, _mo, _pf,
                        imgs, img_paths,
                        draw_ms=_dr, layout_ms=_la,
                        save_ms=(time.perf_counter() - _t_save) * 1000.0,
                    )
                except Exception:
                    pass
            if not img_paths:
                return message

            # 保留前置 at / reply（与主路径一致，不再丢弃 Reply）
            new_segs = [
                seg for seg in message
                if isinstance(seg, dict) and seg.get("type") in ("at", "reply")
            ]
            for img_path in img_paths:
                new_segs.append({"type": "image", "data": {"file": str(img_path.resolve())}})
            return new_segs

        elif isinstance(message, str) and message.strip():
            # 纯文本字符串
            text = message.strip()
            try:
                min_threshold = int(cfg.get("min_length_threshold", 1) or 1)
            except Exception:
                min_threshold = 1
            if len(text) < min_threshold:
                return message
            if _clean_urls(_URL_PATTERN.findall(text)):
                if str(cfg.get("link_mode", "as_image") or "as_image") == "keep_text":
                    return message
            render_trigger2 = str(cfg.get("render_trigger", "always") or "always")
            grp_custom2 = self._get_group_custom_config(gid)
            if render_trigger2 == "violation_only" and not bool(cfg.get("enable_ai_moderation", False)):
                _qp2 = grp_custom2.get("keyword_preset") if isinstance(grp_custom2, dict) else None
                quick_hit2, _ = self.moderator.check_keywords(text, _qp2)
                if not quick_hit2:
                    return message
            eff_scale2 = self._get_group_font_scale(gid, grp_custom2)
            _perf2 = self._perf_enabled()
            _t0b = time.perf_counter()
            _t_mod2 = _t0b if _perf2 else 0.0
            _perf_out2: Optional[Dict] = {} if _perf2 else None
            mod_res2, eff_text2, mosaic_mode2 = await self._moderate_text(
                text, keyword_preset=grp_custom2.get("keyword_preset"),
            )
            _mod_ms2 = (time.perf_counter() - _t_mod2) * 1000.0 if _perf2 else 0.0
            if render_trigger2 == "violation_only" and not mod_res2.is_violated:
                return message
            if mod_res2.is_violated and mod_res2.action == "block":
                self.cfg_mgr.record_render(is_violated=True, count_total=False)
                if _perf2:
                    try:
                        _total_ms2 = (time.perf_counter() - _t0b) * 1000.0
                        self._emit_perf_log(f"群{gid}", _total_ms2, _mod_ms2, 0.0, 0.0, [], [], blocked=True)
                    except Exception:
                        pass
                # 纯文本分支：返回空白（结构有效、无内容外泄；平台侧不再收到违规文本）
                return " "
            imgs2 = await self._render_moderated(
                eff_text2, mod_res2, mosaic_mode2,
                font_scale=eff_scale2,
                style=grp_custom2.get("style"),
                theme_mode=grp_custom2.get("theme_mode"),
                custom_font_path=str(grp_custom2.get("custom_font_path", "") or ""),
                custom_bold_font_path=str(grp_custom2.get("custom_bold_font_path", "") or ""),
                perf_out=_perf_out2,
            )
            _elapsed2 = int((time.perf_counter() - _t0b) * 1000)
            violated2, mosaic2 = mod_res2.is_violated, mosaic_mode2 != "none"
            self.cfg_mgr.record_render(
                is_violated=violated2, is_mosaic=mosaic2,
                elapsed_ms=_elapsed2 if imgs2 else None,
            )
            if not imgs2:
                return message
            if _perf2:
                _t_save2 = time.perf_counter()
            img_paths = await self._save_render_images(imgs2)
            if _perf2:
                try:
                    _total_ms2 = (time.perf_counter() - _t0b) * 1000.0
                    _mo2, _pf2, _dr2, _la2 = self._perf_vals(_perf_out2)
                    self._emit_perf_log(
                        f"群{gid}", _total_ms2, _mod_ms2, _mo2, _pf2,
                        imgs2, img_paths,
                        draw_ms=_dr2, layout_ms=_la2,
                        save_ms=(time.perf_counter() - _t_save2) * 1000.0,
                    )
                except Exception:
                    pass
            if img_paths:
                return [{"type": "image", "data": {"file": str(p.resolve())}} for p in img_paths]

        return message



    # ==========================================
    # 消息转图片核心钩子 (设置最高优先级 priority=99999)
    # ==========================================
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
        try:
            min_threshold = int(cfg.get("min_length_threshold", 1) or 1)
        except Exception:
            min_threshold = 1
        if len(full_text) < min_threshold:
            return

        # 4. 链接处理策略判断
        link_mode = str(cfg.get("link_mode", "as_image") or "as_image")
        urls_found = _clean_urls(_URL_PATTERN.findall(full_text))
        if urls_found and link_mode == "keep_text":
            # 用户选择包含链接时保持纯文本，方便群友点击
            return

        # 5. 仅违规时转图：非违规直接保留纯文本（放在审查前快速判断，避免无谓渲染）
        # 先取群专属词库，保证预检与正式审查同口径；AI 开启时跳过预检，
        # 直接走一次完整审查（避免关键词扫两遍），AI 关闭时预检未命中直接返回
        gid_main = self._extract_group_id(event)
        grp_c_main = self._get_group_custom_config(gid_main)
        render_trigger = str(cfg.get("render_trigger", "always") or "always")
        if render_trigger == "violation_only":
            _ai_on = bool(cfg.get("enable_ai_moderation", False))
            if not _ai_on:
                _qp = grp_c_main.get("keyword_preset") if isinstance(grp_c_main, dict) else None
                quick_hit, _ = self.moderator.check_keywords(full_text, _qp)
                if not quick_hit:
                    return

        # 6-8. 先审查再渲染 + 落盘（群组单独字体大小与专属风格/主题，超长分页多图）
        eff_scale_main = self._get_group_font_scale(gid_main, grp_c_main)
        _perf_m = self._perf_enabled()
        _t0m = time.perf_counter()
        _t_mod_m = _t0m if _perf_m else 0.0
        _perf_out_m: Optional[Dict] = {} if _perf_m else None
        mod_main, eff_main, mosaic_main = await self._moderate_text(
            full_text, keyword_preset=grp_c_main.get("keyword_preset"),
        )
        _mod_ms_m = (time.perf_counter() - _t_mod_m) * 1000.0 if _perf_m else 0.0
        # 仅违规触发门控：无违规则不转图（审查之后、渲染之前，不付 PIL 成本）
        if render_trigger == "violation_only" and not mod_main.is_violated:
            return
        if mod_main.is_violated and mod_main.action == "block":
            self.cfg_mgr.record_render(is_violated=True, count_total=False)
            logger.info(f"[{PLUGIN_NAME}] 触发安全审查 -> blocked=True")
            if _perf_m:
                try:
                    _sess = f"群{gid_main}" if gid_main else "私聊"
                    self._emit_perf_log(_sess, (time.perf_counter() - _t0m) * 1000.0,
                                        _mod_ms_m, 0.0, 0.0, [], [], blocked=True)
                except Exception:
                    pass
            # 直接拦截不发送
            event.stop_event()
            return
        violated_m, mosaic_m = mod_main.is_violated, mosaic_main != "none"
        imgs = await self._render_moderated(
            eff_main, mod_main, mosaic_main,
            font_scale=eff_scale_main,
            style=grp_c_main.get("style"),
            theme_mode=grp_c_main.get("theme_mode"),
            custom_font_path=str(grp_c_main.get("custom_font_path", "") or ""),
            custom_bold_font_path=str(grp_c_main.get("custom_bold_font_path", "") or ""),
            perf_out=_perf_out_m,
        )
        _elapsed_m = int((time.perf_counter() - _t0m) * 1000)
        self.cfg_mgr.record_render(
            is_violated=violated_m, is_mosaic=mosaic_m,
            elapsed_ms=_elapsed_m if imgs else None,
        )
        if violated_m:
            logger.info(f"[{PLUGIN_NAME}] 触发安全审查 -> blocked=False mosaic={mosaic_m}")
        if not imgs:
            return
        if _perf_m:
            _t_savem = time.perf_counter()
        img_paths = await self._save_render_images(imgs)
        if _perf_m:
            try:
                _sess = f"群{gid_main}" if gid_main else "私聊"
                _mom, _pfm, _drm, _lam = self._perf_vals(_perf_out_m)
                self._emit_perf_log(
                    _sess, (time.perf_counter() - _t0m) * 1000.0, _mod_ms_m,
                    _mom, _pfm, imgs, img_paths,
                    draw_ms=_drm, layout_ms=_lam,
                    save_ms=(time.perf_counter() - _t_savem) * 1000.0,
                )
            except Exception:
                pass
        if not img_paths:
            return

        # 8. 组装新消息链并替换
        new_chain = []
        # 保留原链中的 At、Reply 等前置修饰段
        for comp in result.chain:
            if comp.__class__.__name__ in ("At", "AtAll", "Reply"):
                new_chain.append(comp)

        # 插入渲染出的图片段（超长分页一次性发出，不再丢弃截断部分）
        for img_path in img_paths:
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
    # 缓存周期清理（可停止 + 数量上限兜底）
    # ==========================================
    async def _clean_old_cache_periodic(self):
        if self._cache_stop_event is None:
            return
        while not self._cache_stop_event.is_set():
            try:
                await asyncio.wait_for(self._cache_stop_event.wait(), timeout=1800)
                break  # 被置位说明插件卸载，直接退出
            except asyncio.TimeoutError:
                pass
            try:
                now = time.time()

                def _sweep_names():
                    # t2i_/test_ 成图 + preview_tmp_/preview_ 独立预览 + 下载/落盘 tmp
                    for p in self.cache_dir.iterdir():
                        if not p.is_file():
                            continue
                        n = p.name
                        if (n.startswith("t2i_") or n.startswith("test_")
                                or n.startswith("preview_tmp_") or n.startswith("preview_")
                                or n.endswith(".downloading")
                                or ".tmp." in n):
                            # preview_latest.jpg 常驻复用，不参与清扫
                            if n == "preview_latest.jpg":
                                continue
                            yield p

                cached_files = sorted(_sweep_names(), key=lambda p: p.stat().st_mtime)
                for p in cached_files:
                    try:
                        if now - p.stat().st_mtime > 3600:  # 超过 1 小时清除
                            p.unlink(missing_ok=True)
                    except Exception:
                        pass
                # 数量兜底：超过 300 张删最旧的（防止高频群聊打爆磁盘）
                remain = sorted(_sweep_names(), key=lambda p: p.stat().st_mtime)
                for p in remain[:-300]:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
            except Exception:
                pass
