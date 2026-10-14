# -*- coding: utf-8 ---
"""群聊维度逻辑：白名单、专属配置、群号提取。"""

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


    @staticmethod
    def _platform_of(event: Any) -> str:
        """提取事件所属平台（onebot/matrix/telegram/...）。"""
        try:
            gpn = getattr(event, "get_platform_name", None)
            if callable(gpn):
                p = str(gpn() or "").strip().lower()
                if p:
                    return p
            for attr in ("platform_id", "platform"):
                p = str(getattr(event, attr, "") or "").strip().lower()
                if p:
                    return p
            mo = getattr(event, "message_obj", None)
            if mo:
                p = str(getattr(mo, "platform_id", "") or getattr(mo, "platform", "") or "").strip().lower()
                if p:
                    return p
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if umo:
                return umo.split("/")[0].split(":")[0].strip().lower()
        except Exception:
            pass
        return ""

    def _seen_file(self) -> Path:
        return self.cfg_mgr.data_dir / "seen_groups.json"

    def _load_seen_groups(self) -> None:
        try:
            f = self._seen_file()
            if f.is_file():
                import json
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict):
                    self._seen_groups.update(data)
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 读取 seen_groups.json 异常: {e}")

    def _save_seen_groups(self) -> None:
        try:
            f = self._seen_file()
            import json
            temp = f.with_suffix(".tmp")
            with open(temp, "w", encoding="utf-8") as fp:
                json.dump(self._seen_groups, fp, ensure_ascii=False, indent=2)
            temp.replace(f)
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 写入 seen_groups.json 异常: {e}")

    def _remember_group(self, event: AstrMessageEvent):
        """记录已见群聊（支持各平台，自动落盘 seen_groups.json）。"""
        gid = self._extract_group_id(event)
        if not gid:
            return gid
        try:
            platform = self._platform_of(event)
            grp_name = ""
            mo = getattr(event, "message_obj", None)
            if mo:
                grp = getattr(mo, "group", None)
                if grp:
                    for attr in ("group_name", "name", "title"):
                        gn = str(getattr(grp, attr, "") or "").strip()
                        if gn:
                            grp_name = gn
                            break
                if not grp_name:
                    for attr in ("group_name", "group_title"):
                        gn = str(getattr(mo, attr, "") or "").strip()
                        if gn:
                            grp_name = gn
                            break
            if not grp_name:
                sess = getattr(event, "session", None)
                if sess:
                    gn = str(getattr(sess, "session_name", "") or "").strip()
                    if gn:
                        grp_name = gn

            prev_info = self._seen_groups.get(gid)
            prev_name = str(prev_info.get("group_name", "") or "").strip() if isinstance(prev_info, dict) else ""
            if not grp_name and prev_name and prev_name != gid:
                grp_name = prev_name
            if not platform and isinstance(prev_info, dict) and prev_info.get("platform"):
                platform = prev_info.get("platform")

            now = int(time.time())
            updated = False
            cur_entry = self._seen_groups.get(gid)
            if not isinstance(cur_entry, dict):
                self._seen_groups[gid] = {
                    "gid": gid,
                    "group_name": grp_name or gid,
                    "platform": platform,
                    "last_seen": now,
                    "msg_count": 1,
                }
                updated = True
            else:
                cur_entry["last_seen"] = now
                cur_entry["msg_count"] = int(cur_entry.get("msg_count", 0)) + 1
                if grp_name and grp_name != cur_entry.get("group_name"):
                    cur_entry["group_name"] = grp_name
                    updated = True
                if platform and not cur_entry.get("platform"):
                    cur_entry["platform"] = platform
                    updated = True

            while len(self._seen_groups) > 2000:
                self._seen_groups.pop(next(iter(self._seen_groups)))

            last_save = getattr(self, "_seen_last_save_ts", 0.0)
            if updated or (now - last_save > 30.0):
                self._seen_last_save_ts = now
                self._save_seen_groups()
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
        mode = str(cfg.get("group_mode", "all") or "all")
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
        gid_lower = gid.lower()
        for t, clean_t in zip(tokens, cleaned):
            if gid == t or gid_lower == t.lower():
                return True
            if t.endswith(":" + gid) or t.lower().endswith(":" + gid_lower):
                return True
            if clean_t and clean_gid and clean_gid == clean_t:
                return True
        return False



    def _is_gid_allowed(self, gid: str) -> bool:
        """群号检查是否允许生效（all 全群 / whitelist 白名单 / disabled 全群关闭）"""
        if not gid:
            return bool(self.cfg_mgr.config.get("enable_private", True))
        cfg = self.cfg_mgr.config
        mode = str(cfg.get("group_mode", "all") or "all").lower()
        if mode in ("disabled", "off", "none", "closed"):
            return False
        if mode == "all":
            return True
        if mode == "whitelist":
            tokens = self._parsed_group_list()
            return self._match_group_tokens(gid, tokens)
        if mode == "blacklist":
            tokens = self._parsed_group_list()
            return not self._match_group_tokens(gid, tokens)
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
        raw = self.cfg_mgr._group_dict("group_configs")
        if not raw:
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
        raw = self.cfg_mgr._group_dict("group_font_scales")
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
                tail = umo.split(":")[-1].strip()
                if tail:
                    return tail
            elif "!" in umo:
                # 兼容 Matrix 房间 ID 格式（以 ! 开头的 room id）
                for tok in umo.replace(":", "/").split("/"):
                    tok_s = tok.strip()
                    if tok_s.startswith("!"):
                        return tok_s
        except Exception:
            pass
        return ""



    def is_platform_allowed(self, platform: str = "") -> bool:
        """检查指定平台是否允许运行本插件（支持 platform_preset 下拉预设与 allowed_platforms 自定义）。"""
        cfg = self.cfg_mgr.config
        preset = str(cfg.get("platform_preset", "") or "").strip().lower()
        p = (platform or "").strip().lower()
        if preset == "all":
            return True
        if preset in ("onebot", "qq"):
            if not p:
                return True
            return p in ("onebot", "aiocqhttp", "aihttpcq", "cqhttp", "onebot11", "onebot_v11")
        if preset in ("onebot_matrix", "onebot,matrix", "matrix_onebot"):
            if not p:
                return True
            return p in ("onebot", "aiocqhttp", "aihttpcq", "cqhttp", "onebot11", "onebot_v11", "matrix")

        allowed = str(cfg.get("allowed_platforms", "onebot, aiocqhttp") or "onebot, aiocqhttp").strip()
        if not allowed or allowed.lower() in ("all", "*", "any"):
            return True
        import re as _re
        tokens = [t.strip().lower() for t in _re.split(r"[,，、;；\s]+", allowed) if t.strip()]
        if not p:
            return True
        if "all" in tokens or "*" in tokens:
            return True
        if p in tokens:
            return True
        if p in ("aiocqhttp", "aihttpcq", "cqhttp", "onebot11", "onebot_v11") and ("onebot" in tokens or "aiocqhttp" in tokens):
            return True
        if p == "onebot" and "aiocqhttp" in tokens:
            return True
        return False

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        pname = self._platform_of(event)
        if pname == "webchat":
            return False

        if not self.is_platform_allowed(pname):
            return False

        gid = self._remember_group(event)
        if not gid:
            # 私聊是否生效由 enable_private 控制（默认开启）
            return bool(self.cfg_mgr.config.get("enable_private", True))

        cfg = self.cfg_mgr.config
        mode = str(cfg.get("group_mode", "all") or "all")
        # 判定唯一实现是 _is_gid_allowed（all/whitelist/disabled 同口径）
        allowed = self._is_gid_allowed(gid)
        logger.debug(f"[{PLUGIN_NAME}] 群聊 {gid} 群过滤({mode}) -> {allowed}")
        return allowed



    # /text、/style、/theme 已合并至 /xbimg group（见 cmd_xbimg），此处不再保留重复指令

    def _update_group_custom(self, gid: str, patch: Dict[str, Any]):
        """辅助方法：合并更新指定群专属配置并持久化保存"""
        if not gid:
            return
        cfg = self.cfg_mgr.config
        raw = self.cfg_mgr._group_dict("group_configs")
        gid_str = str(gid)
        cur = dict(raw.get(gid_str, {})) if isinstance(raw.get(gid_str), dict) else {}
        cur.update(patch)
        raw[gid_str] = cur
        cfg["group_configs"] = raw
        self.cfg_mgr.save({"group_configs": raw})
