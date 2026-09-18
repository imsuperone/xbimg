# -*- coding: utf-8 ---
"""群聊维度逻辑：白名单、专属配置、群号提取。"""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .common import (
        AstrMessageEvent, PLUGIN_NAME, logger, _NON_DIGIT_RE, _SPLIT_GROUP_RE,
    )
except (ImportError, ValueError):
    from core.common import (
        AstrMessageEvent, PLUGIN_NAME, logger, _NON_DIGIT_RE, _SPLIT_GROUP_RE,
    )


class GroupsMixin:
    """GroupsMixin：由 Msg2ImgPlugin 多继承组合，依赖其 __init__ 初始化的属性。"""


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
            # 有界增长：超限淘汰最早记录，避免常年运行内存膨胀
            while len(self._seen_groups) > 2000:
                self._seen_groups.pop(next(iter(self._seen_groups)))
        except Exception:
            pass
        return gid

    # ==========================================
    # 消息入口优先拦截（针对 xbbot 等由 @event_message_type 驱动的业务插件）
    # 设置高优先级 priority=100，确保在其它插件之前检查并打标
    # ==========================================



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
        # 同步缓存清洗后的纯数字 token，命中判断时不再逐条做正则
        try:
            self._group_cache_cleaned = [_NON_DIGIT_RE.sub("", t) for t in tokens]
        except Exception:
            self._group_cache_cleaned = [""] * len(tokens)
        return tokens



    def _match_group_tokens(self, gid: str, tokens: List[str]) -> bool:
        clean_gid = _NON_DIGIT_RE.sub("", gid)
        cleaned = getattr(self, "_group_cache_cleaned", None)
        if not isinstance(cleaned, list) or len(cleaned) != len(tokens):
            try:
                cleaned = [_NON_DIGIT_RE.sub("", t) for t in tokens]
            except Exception:
                cleaned = [""] * len(tokens)
        for t, clean_t in zip(tokens, cleaned):
            if gid == t:
                return True
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
                raw = json.loads(raw) if raw.strip() else {}
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



    def _get_group_font_scale(self, gid: str, grp_c: Optional[Dict[str, Any]] = None) -> int:
        """获取群组单独字体大小，若无则返回全局；可传入已查到的专属配置避免重复扫描"""
        try:
            base = int(self.cfg_mgr.config.get("font_scale", 100) or 100)
        except Exception:
            base = 100
        if not gid:
            return base
        # 优先读取 group_configs 中的配置
        if grp_c is None:
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
                raw = json.loads(raw) if raw.strip() else {}
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



    # /text、/style、/theme 已合并至 /xbimg group（见 cmd_xbimg），此处不再保留重复指令

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
