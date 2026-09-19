# -*- coding: utf-8 ---
"""WebUI 后端 API。"""

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
    from .common import (
        FileResponse, PLUGIN_NAME, _HAS_WEB_API, error_response,
        json_response, logger, request,
    )
    from .config import DEFAULT_CONFIG
    from .moderation import ContentModerator
    from .renderer import (
        MessageImageRenderer, _FONT_CONF, _is_usable_font,
        _resolve_custom_font, clear_font_cache, configure_fonts,
        delete_curated_font, delete_emoji_pack, download_curated_font,
        download_emoji_pack, download_missing_fonts, ensure_emoji_assets,
        get_curated_fonts_status, get_emoji_packs_status, get_font_status,
    )
except (ImportError, ValueError):
    from core.common import (
        FileResponse, PLUGIN_NAME, _HAS_WEB_API, error_response,
        json_response, logger, request,
    )
    from core.config import DEFAULT_CONFIG
    from core.moderation import ContentModerator
    from core.renderer import (
        MessageImageRenderer, _FONT_CONF, _is_usable_font,
        _resolve_custom_font, clear_font_cache, configure_fonts,
        delete_curated_font, delete_emoji_pack, download_curated_font,
        download_emoji_pack, download_missing_fonts, ensure_emoji_assets,
        get_curated_fonts_status, get_emoji_packs_status, get_font_status,
    )


class WebApiMixin:
    """WebApiMixin：由 Msg2ImgPlugin 多继承组合，依赖其 __init__ 初始化的属性。"""


    def _issue_preview_nonce(self) -> str:
        """签发预览图一次性 nonce（10 分钟有效，防未鉴权直读 preview_img）"""
        try:
            now = time.time()
            # 顺手清理过期
            for k, exp in list(self._preview_nonces.items()):
                if exp <= now:
                    self._preview_nonces.pop(k, None)
            while len(self._preview_nonces) >= 32:
                self._preview_nonces.pop(next(iter(self._preview_nonces)), None)
            nonce = f"pv_{int(now * 1000):x}_{os.urandom(8).hex()}"
            self._preview_nonces[nonce] = now + 600
            return nonce
        except Exception:
            return ""



    def _check_preview_nonce(self, nonce: str) -> bool:
        """校验预览 nonce（有效期内可用；读不到 query 接口时放行保兼容并记日志）"""
        try:
            if not nonce:
                return False
            exp = self._preview_nonces.get(str(nonce), 0)
            if exp and float(exp) > time.time():
                return True
        except Exception:
            pass
        return False



    def _read_nonce_from_request(self) -> Optional[str]:
        """从当前请求读 nonce：Quart 兼容 args → environ QUERY_STRING；都没有则返回 None（调用方保兼容）"""
        try:
            if request is None:
                return None
        except Exception:
            return None
        try:
            args = getattr(request, "args", None)
            if args is not None:
                try:
                    v = args.get("nonce", "")
                except Exception:
                    v = ""
                if v:
                    return str(v)
        except Exception:
            pass
        try:
            env = getattr(request, "environ", None) or {}
            qs = str(env.get("QUERY_STRING", "") or "")
            if qs:
                from urllib.parse import parse_qs
                vals = parse_qs(qs).get("nonce", [])
                if vals:
                    return str(vals[0])
        except Exception:
            pass
        return None

    # ==========================================
    # WebUI 后端 API 接口
    # ==========================================



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
        reg(f"/{pfx}/presets/update_status", self._api_presets_update_status, ["GET"], "查询官方词库更新")
        reg(f"/{pfx}/presets/apply_update", self._api_presets_apply_update, ["POST"], "覆盖更新官方词库")
        reg(f"/{pfx}/presets/dismiss_update", self._api_presets_dismiss_update, ["POST"], "保留本地词库不再提示")
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
            # 旧字段归一：只认 emoji_style；老客户端若只发 emoji_remote 则折算
            try:
                if "emoji_style" not in payload and "emoji_remote" in payload:
                    payload["emoji_style"] = (
                        "none" if str(payload.get("emoji_remote")).lower() in ("0", "false", "no", "none", "") else "android"
                    )
            except Exception:
                pass
            self.cfg_mgr.save(payload)
            self._group_cache_sig = None  # 群名单可能变化，清缓存
            try:
                configure_fonts(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 字体配置同步异常: {e}")
            self.moderator = ContentModerator(self.cfg_mgr.config)
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
                        text = base64.b64decode(b64_t).decode("utf-8", errors="ignore").strip()
                    except Exception:
                        pass

            text_provided = bool(text)
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

            _t_prev = time.perf_counter()
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
            _prev_ms = int((time.perf_counter() - _t_prev) * 1000)
            # 预览不计入业务渲染统计（预览是缩略 JPEG 管线，与真实落盘口径不同，
            # 计入会拉偏最快/平均耗时；如需观测预览性能请看返回的 render_ms）

            # 优化预览图：等比缩放后单次 JPEG 编码，文件与 Base64 复用同一份字节；
            # 缩放/转码/写盘整体在工作线程执行，不阻塞事件循环（编码参数不变，输出字节一致）
            def _encode_preview() -> bytes:
                thumb = img
                if thumb.width > 680:
                    scale = 680 / thumb.width
                    thumb = thumb.resize(
                        (680, int(thumb.height * scale)),
                        Image.LANCZOS,
                    )
                rgb = thumb.convert("RGB")
                buf = io.BytesIO()
                try:
                    rgb.save(buf, format="JPEG", quality=92, optimize=True, subsampling=0)
                    return buf.getvalue()
                finally:
                    try:
                        buf.close()
                    except Exception:
                        pass

            jpeg_bytes = await asyncio.to_thread(_encode_preview)
            preview_path = self.cache_dir / "preview_latest.jpg"
            tmp_path = self.cache_dir / f"preview_tmp_{os.getpid()}_{int(time.time()*1000)}_{os.urandom(2).hex()}.jpg"
            try:
                # 原子发布：先写唯一 tmp 再替换，多人同时预览互不覆盖
                tmp_path.write_bytes(jpeg_bytes)
                os.replace(str(tmp_path), str(preview_path))
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 预览图落盘失败: {e}")
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            b64_str = base64.b64encode(jpeg_bytes).decode("utf-8")

            t_stamp = int(time.time() * 1000)
            _nonce = self._issue_preview_nonce()
            # 按 nonce 落一份独立文件：多人同时预览各读各的，不再串图；
            # 10 分钟后自动清理（与 nonce 有效期一致），读不到则回退 preview_latest
            if _nonce:
                try:
                    _safe = "".join(c for c in str(_nonce) if c.isalnum() or c in ("_", "-"))[:64]
                    if _safe:
                        (self.cache_dir / f"preview_{_safe}.jpg").write_bytes(jpeg_bytes)
                        try:
                            self._schedule_delete(self.cache_dir / f"preview_{_safe}.jpg", 600)
                        except Exception:
                            pass
                except Exception:
                    pass
            _nonce_qs = f"&nonce={_nonce}" if _nonce else ""
            return json_response({
                "ok": True,
                "image_url": f"/{PLUGIN_NAME}/preview_img?t={t_stamp}{_nonce_qs}",
                "image_base64": f"data:image/jpeg;base64,{b64_str}",
                "width": img.width,
                "height": img.height,
                "render_ms": _prev_ms,
            })
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 生成预览失败: {e}")
            return error_response(f"生成预览失败: {e}", status_code=500)



    async def _api_get_preview_img(self):
        """流式输出最新预览图文件，避免 IPC 传输大 JSON 字符串（需预览接口签发的 nonce）"""
        _nonce = self._read_nonce_from_request()
        if _nonce is None:
            # 读不到 query（极老版本请求对象）：拒绝直读，引导用 JSON 内 base64 字段；
            # 之前“放行保兼容”等于无鉴权，已收紧
            return error_response("预览凭证缺失，请重新生成预览（前端请使用 image_base64 字段兜底）", status_code=403)
        elif not self._check_preview_nonce(_nonce):
            return error_response("预览凭证无效或已过期，请重新生成预览", status_code=403)
        try:
            _safe = "".join(c for c in str(_nonce) if c.isalnum() or c in ("_", "-"))[:64]
            _per = self.cache_dir / f"preview_{_safe}.jpg" if _safe else None
        except Exception:
            _per = None
        img_path = _per if (_per is not None and _per.is_file()) else (self.cache_dir / "preview_latest.jpg")
        if not img_path.exists():
            return error_response("预览图不存在", status_code=404)
        try:
            # 读盘放工作线程，不阻塞事件循环
            data = await asyncio.to_thread(img_path.read_bytes)
        except Exception:
            return error_response("预览图读取失败", status_code=500)
        if FileResponse is not None:
            try:
                return FileResponse(content=data, media_type="image/jpeg")
            except Exception:
                pass
        try:
            from astrbot.api.web import file_response
            return file_response(img_path)
        except Exception:
            return error_response("输出图片流失败", status_code=500)



    async def _api_reset_config(self):
        from copy import deepcopy
        try:
            from .config import DEFAULT_CONFIG
        except (ImportError, ValueError):
            from core.config import DEFAULT_CONFIG
        self.cfg_mgr.config = deepcopy(DEFAULT_CONFIG)
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
        """持久化字体目录：直接复用 renderer._font_data_dir（唯一来源，避免双份逻辑漂移）"""
        try:
            try:
                from .renderer import _font_data_dir
            except (ImportError, ValueError):
                from core.renderer import _font_data_dir
            d = _font_data_dir()
            if d is not None:
                return d
        except Exception:
            pass
        try:
            try:
                from .renderer import FONT_DATA_SUBDIR
            except (ImportError, ValueError):
                from core.renderer import FONT_DATA_SUBDIR
            d = self.cfg_mgr.data_dir / FONT_DATA_SUBDIR
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return d
        except Exception:
            d = self.cfg_mgr.data_dir / "fonts"
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return d



    def _usable_data_fonts(self) -> List[str]:
        """持久化目录可用字体文件名（排序，与 WebUI 列表同序），供删除后回退选用"""
        try:
            from .renderer import _is_usable_font
        except (ImportError, ValueError):
            from core.renderer import _is_usable_font
        out: List[str] = []
        try:
            d = self._fonts_data_dir()
            if d.is_dir():
                for p in sorted(d.iterdir()):
                    if p.is_file() and p.suffix.lower() in (".ttf", ".ttc", ".otf"):
                        try:
                            if bool(_is_usable_font(str(p))):
                                out.append(p.name)
                        except Exception:
                            continue
        except Exception:
            pass
        return out



    def _fallback_font_after_delete(self) -> str:
        """删除字体后回退：当前自定义字体失效时，仅剩 1 个可用则默认用它，
        多个则用列表第一个；无可用则清空回自动。返回选中的文件名（无改动返回空串）。"""
        try:
            from .renderer import _resolve_custom_font
        except (ImportError, ValueError):
            from core.renderer import _resolve_custom_font
        cfg = self.cfg_mgr.config
        try:
            src = str(cfg.get("font_source", "auto") or "auto").lower()
            cur = str(cfg.get("custom_font_path", "") or "")
            if (src != "custom" and not cur) or (cur and _resolve_custom_font(cur)):
                return ""
        except Exception:
            pass
        usable = self._usable_data_fonts()
        if not usable:
            try:
                cfg["custom_font_path"] = ""
                cfg["custom_bold_font_path"] = ""
                if str(cfg.get("font_source", "")) == "custom":
                    cfg["font_source"] = "auto"
                self.cfg_mgr.save()
                configure_fonts(cfg, self.cfg_mgr.data_dir)
            except Exception:
                pass
            return ""
        pick = usable[0]
        try:
            cfg["custom_font_path"] = pick
            cfg["custom_bold_font_path"] = ""
            cfg["font_source"] = "custom"
            self.cfg_mgr.save({
                "custom_font_path": pick,
                "custom_bold_font_path": "",
                "font_source": "custom",
            })
            configure_fonts(cfg, self.cfg_mgr.data_dir)
        except Exception:
            pass
        return pick



    async def _api_fonts_files(self):
        """列出持久化目录中的字体文件（可删除的只有这些，随包/系统字体只读）"""
        try:
            from .renderer import _is_usable_font
        except (ImportError, ValueError):
            from core.renderer import _is_usable_font
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
                from .renderer import clear_font_cache
            except (ImportError, ValueError):
                from core.renderer import clear_font_cache
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

            # 删除后回退：直链引用顺手清理，群专属失效引用清空，当前字体失效则按剩余列表回退
            try:
                cfg = self.cfg_mgr.config
                need_save = False
                v = str(cfg.get("custom_font_url", "") or "")
                if v and (v == name or v.endswith("/" + name) or v.endswith("\\" + name)):
                    cfg["custom_font_url"] = ""
                    need_save = True
                # 同时清空群聊专属配置中引用该字体的项（回退跟随全局）
                raw_grp = cfg.get("group_configs", {})
                if isinstance(raw_grp, str):
                    try:
                        raw_grp = json.loads(raw_grp) if raw_grp.strip() else {}
                        cfg["group_configs"] = raw_grp
                    except Exception:
                        raw_grp = {}
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
            picked = self._fallback_font_after_delete()
            try:
                configure_fonts(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception:
                pass
            return json_response({"ok": True, "deleted": name, "fallback": picked, "fonts": get_font_status()})
        except Exception as e:
            return error_response(f"删除失败: {e}", status_code=500)



    async def _api_fonts_curated_delete(self):
        try:
            payload = await request.json(default={})
            cid = str(payload.get("id", "") or "").strip()
            if not cid:
                return error_response("缺少字体 ID", status_code=400)
            res = await asyncio.to_thread(delete_curated_font, cid)
            picked = self._fallback_font_after_delete()
            try:
                configure_fonts(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception:
                pass
            return json_response({"ok": res.get("ok", False), **res, "fallback": picked, "fonts": get_font_status()})
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
            # 仅下载不自动切换（切换请走 group ttf / 字体选择），此处不再回写 _FONT_CONF
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
                from .renderer import configure_fonts as _cf
            except (ImportError, ValueError):
                from core.renderer import configure_fonts as _cf
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
            if style not in ("ios", "android", "windows", "all"):
                return error_response("未知样式", status_code=400)
            res = await asyncio.to_thread(delete_emoji_pack, style)
            # 若删除的是当前样式则切回 none
            try:
                cur = str(self.cfg_mgr.config.get("emoji_style", "") or "").lower()
                if cur == style:
                    self.cfg_mgr.config["emoji_style"] = "none"
                    self.cfg_mgr.save({"emoji_style": "none"})
                    from .renderer import configure_fonts as _cf2
                else:
                    from .renderer import configure_fonts as _cf2
            except (ImportError, ValueError):
                from core.renderer import configure_fonts as _cf2
            try:
                _cf2(self.cfg_mgr.config, self.cfg_mgr.data_dir)
            except Exception:
                pass
            return json_response({"ok": res.get("ok", False), **res})
        except Exception as e:
            return error_response(f"删除失败: {e}", status_code=500)



    async def _api_presets_update_status(self):
        """查询内置官方词库相对本地是否有更新（只读）"""
        try:
            st = self.cfg_mgr.get_presets_update_status()
            return json_response({"ok": True, **st})
        except Exception as e:
            return error_response(f"查询失败: {e}", status_code=500)



    async def _api_presets_apply_update(self):
        """用内置官方词库覆盖本地指定方案（ids 为空则全部覆盖），用户自建方案不受影响"""
        try:
            payload = await request.json(default={})
            ids = payload.get("ids", "") if isinstance(payload, dict) else ""
            if isinstance(ids, list):
                pass
            elif isinstance(ids, str):
                ids = [i.strip() for i in re.split(r"[,;\s]+", ids) if i.strip()]
            else:
                ids = []
            st = self.cfg_mgr.apply_builtin_presets(ids or None)
            # 仅当覆盖了当前激活方案时，才同步 custom_keywords 并重建审查器
            try:
                cfg = self.cfg_mgr.config
                active = str(cfg.get("active_keyword_preset", "default") or "default")
                presets = cfg.get("keyword_presets", {})
                if (not ids or active in ids) and active in presets:
                    item = presets[active]
                    cfg["custom_keywords"] = item.get("keywords", "") if isinstance(item, dict) else str(item)
                    self.cfg_mgr.save()
                    self.moderator = ContentModerator(cfg)
            except Exception:
                pass
            return json_response({"ok": True, "applied": True, **st})
        except Exception as e:
            return error_response(f"覆盖更新失败: {e}", status_code=500)



    async def _api_presets_dismiss_update(self):
        """保留本地词库不再提示"""
        try:
            st = self.cfg_mgr.dismiss_builtin_presets_update()
            return json_response({"ok": True, **st})
        except Exception as e:
            return error_response(f"操作失败: {e}", status_code=500)



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
