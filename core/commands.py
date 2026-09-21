# -*- coding: utf-8 ---
"""/xbimg 聊天指令。"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .common import (
        AstrImage, AstrMessageEvent, PLUGIN_NAME, filter, logger,
        _COMPRESS_CANON, _compress_level,
    )
    from .moderation import ContentModerator
    from .renderer import MessageImageRenderer
except (ImportError, ValueError):
    from core.common import (
        AstrImage, AstrMessageEvent, PLUGIN_NAME, filter, logger,
        _COMPRESS_CANON, _compress_level,
    )
    from core.moderation import ContentModerator
    from core.renderer import MessageImageRenderer


class CommandsMixin:
    """CommandsMixin：由 Msg2ImgPlugin 多继承组合，依赖其 __init__ 初始化的属性。"""


    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        """尽力判定是否为管理员。平台不支持判定时拒绝变更（fail-closed，防普通群员改配置）。

        例外：私聊（无群号）默认放行——私聊对象通常即机器人主人；群聊未知身份一律拒绝。
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
        try:
            if not self._extract_group_id(event):
                return True
        except Exception:
            pass
        return False

    # ==========================================
    # 管理指令交互 (仅保留纯净的 /xbimg 指令)
    # ==========================================



    # ==========================================
    # 管理指令交互 (仅保留纯净的 /xbimg 指令)
    # ==========================================
    async def cmd_xbimg(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        """消息转图助手管理指令"""
        sub = sub.strip().lower()
        arg = arg.strip()
        cfg = self.cfg_mgr.config

        comp_map = {
            "compact": "⚡ 极小文件 (省流紧凑)",
            "balanced": "⚖️ 均衡适中 (推荐)",
            "lossless": "💎 原画无损 (高清大图)",
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
            "sparse": "稀疏 (~18颗)",
            "medium": "标准 (~36颗)",
            "dense": "星海 (~60颗)",
        }

        if not sub or sub in ("help", "status", "菜单"):
            yield event.plain_result(
                "🎨【消息转图助手 · 控制台】\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"• 总开关状态：{'🟢 运行中' if cfg.get('enable', True) else '🔴 已暂停'}\n"
                f"• 触发时机：{'🔄 始终转图' if cfg.get('render_trigger') == 'always' else '🛡️ 仅违规时转图'}\n"
                f"• 最少字数：{cfg.get('min_length_threshold', 1)} 字\n"
                f"• 视觉风格：{cfg.get('style', 'ios').upper()}\n"
                f"• 配色主题：{'🌞 浅色明亮' if cfg.get('theme_mode') == 'light' else '🌙 深色暗黑'}\n"
                f"• 星空背景：{'✨ 开启' if cfg.get('star_background') else '❌ 关闭'} ({density_map.get(cfg.get('star_density', 'medium'), '标准')})\n"
                f"• 文件体积：{comp_map.get(_compress_level(cfg), '均衡适中')}\n"
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
                "• /xbimg quality lossless / balanced / compact - 输出文件大小档位\n"
                "• /xbimg maxkb 800 - 单张体积上限KB（超限自动降质，0 关闭）\n"
                "• /xbimg link image / text / append - 链接策略\n"
                "• /xbimg mod on / off - 屏蔽词审查开关\n"
                "• /xbimg ai on / off - AI 审查独立开关\n"
                "• /xbimg action half / full / block / notice - 违规处置\n"
                "• /xbimg mosaic pixel / blur - 马赛克颗粒/模糊\n"
                "• /xbimg mosaicpos bottom / top / random - 半字打码位置\n"
                "• /xbimg groupmode whitelist / all / blacklist - 群生效模式\n"
                "• /xbimg kwset <方案ID> - 全局词库切换\n"
                "• /xbimg kwupdate [apply|keep] - 官方词库更新检测与覆盖\n"
                "• /xbimg size [群号] 50-500 - 字体百分比\n"
                "• /xbimg group - 本群专属配置卡片\n"
                "• /xbimg group set <50-500> [群号] - 单群字体大小\n"
                "• /xbimg group style ios / android16 / default [群号] - 单群风格\n"
                "• /xbimg group theme light / dark / default [群号] - 单群主题\n"
                "• /xbimg group kw <方案ID> / default [群号] - 单群词库\n"
                "• /xbimg group ttf <字体id> [群号] - 单群精选字体\n"
                "• /xbimg group list - 字体与词库列表\n"
                "• /xbimg group reset [群号] - 单群恢复跟随全局\n"
                "• /xbimg group test [群号] - 单群效果测试图\n"
                "• /xbimg test [文本] - 立即生成测试效果图"
            )
            return

        # 只读子指令不过鉴权：test 生成测试图、group 的查看类动作
        _readonly_group = {"", "info", "show", "status", "card", "查看", "状态",
                           "list", "列表", "test", "测试"}
        try:
            _first_tok = [p for p in re.split(r"[\s,]+", arg) if p]
            _first_act = _first_tok[0].lower() if _first_tok else ""
        except Exception:
            _first_act = ""
        _readonly = (sub == "test") or (sub == "group" and _first_act in _readonly_group)
        if not _readonly and not self._is_admin_event(event):
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
            else:
                yield event.plain_result("用法：/xbimg star on 或 /xbimg star off")
                return
            self.cfg_mgr.save()
            yield event.plain_result(f"✨ 星空背景已设置为：{'开启' if cfg.get('star_background') else '关闭'}")
        elif sub in ("density", "stardensity"):
            if arg in ("sparse", "稀疏"):
                cfg["star_density"] = "sparse"
                self.cfg_mgr.save()
                yield event.plain_result("✨ 小星星密度已设为：稀疏 (~18颗)")
            elif arg in ("dense", "星海", "密集"):
                cfg["star_density"] = "dense"
                self.cfg_mgr.save()
                yield event.plain_result("✨ 小星星密度已设为：星海 (~60颗)")
            elif arg in ("medium", "标准"):
                cfg["star_density"] = "medium"
                self.cfg_mgr.save()
                yield event.plain_result("✨ 小星星密度已设为：标准 (~36颗)")
            else:
                yield event.plain_result("用法：/xbimg density sparse / medium / dense")
        elif sub in ("quality", "compress", "压缩", "文件大小"):
            canon = _COMPRESS_CANON.get(arg.strip().lower(), "")
            if canon == "lossless":
                cfg["img_compress_level"] = "lossless"
                self.cfg_mgr.save()
                yield event.plain_result("💾 文件体积优化已设为：原画无损 (PNG · 最高画质)")
            elif canon == "compact":
                cfg["img_compress_level"] = "compact"
                self.cfg_mgr.save()
                yield event.plain_result("💾 文件体积优化已设为：极小文件 (JPEG 86% · 极致省流量)")
            elif canon == "balanced":
                cfg["img_compress_level"] = "balanced"
                self.cfg_mgr.save()
                yield event.plain_result("💾 文件体积优化已设为：均衡适中 (JPEG 94% · 推荐)")
            else:
                yield event.plain_result("用法：/xbimg quality lossless / balanced / compact")
        elif sub in ("maxkb", "max_kb", "体积上限"):
            try:
                num = int(re.sub(r"\D", "", arg) or "")
                if num < 0 or num > 5120:
                    raise ValueError()
            except Exception:
                yield event.plain_result("用法：/xbimg maxkb 800（单张上限KB，0 为关闭，最大 5120）")
                return
            cfg["img_max_kb"] = num
            self.cfg_mgr.save()
            yield event.plain_result(
                f"💾 单张图片体积上限已设为：{'关闭' if num == 0 else f'{num}KB（超限自动降质）'}"
            )
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
        elif sub in ("kwupdate", "词库更新"):
            # /xbimg kwupdate [apply [方案ID]|keep]：检测官方词库更新并确认覆盖
            parts = [p for p in re.split(r"[\s,]+", arg) if p]
            action = parts[0].lower() if parts else ""
            rest = parts[1:] if parts else []
            if action in ("apply", "update", "覆盖", "更新"):
                ids = [i for i in rest if i] or None
                st = self.cfg_mgr.apply_builtin_presets(ids)
                try:
                    cfg = self.cfg_mgr.config
                    active = str(cfg.get("active_keyword_preset", "default") or "default")
                    presets2 = cfg.get("keyword_presets", {})
                    if (not ids or active in ids) and active in presets2:
                        item = presets2[active]
                        cfg["custom_keywords"] = item.get("keywords", "") if isinstance(item, dict) else str(item)
                        self.cfg_mgr.save()
                    self.moderator = ContentModerator(cfg)
                except Exception:
                    pass
                if st.get("update_available"):
                    left = [f"• {c['id']} - {c['name']}" for c in st.get("changed", [])]
                    yield event.plain_result("✅ 已覆盖更新指定方案，剩余差异：\n" + "\n".join(left))
                else:
                    yield event.plain_result("✅ 官方词库已是最新，本地已同步。")
                return
            if action in ("keep", "dismiss", "保留", "忽略"):
                self.cfg_mgr.dismiss_builtin_presets_update()
                yield event.plain_result("✅ 已保留本地词库，不再提示本次更新。")
                return
            st = self.cfg_mgr.get_presets_update_status()
            if not st.get("update_available"):
                yield event.plain_result("✅ 本地词库与官方一致，无需更新。")
                return
            lines = ["🎨【官方词库更新检测】以下方案与官方不一致："]
            for c in st.get("changed", []):
                flag = "（本地缺失）" if c.get("missing") else ""
                lines.append(f"• {c['id']} - {c['name']}{flag}：官方新增 {c['added_total']} 词，本地独有 {c['removed_total']} 词")
                if c.get("added_sample"):
                    lines.append("  新增示例：" + "、".join(c["added_sample"][:5]))
            lines.append("💡 /xbimg kwupdate apply [方案ID] - 覆盖更新（不填则全部）\n• /xbimg kwupdate keep - 保留本地不再提示")
            yield event.plain_result("\n".join(lines))
            return
        elif sub in ("fontsize", "groupfont", "gscale", "font", "size", "字号", "文字大小"):
            # /xbimg size <gid> <scale>  或  /xbimg size <scale>（当前群）
            parts = [p for p in re.split(r"[\s,]+", arg) if p]
            gid = ""
            scale = None
            if len(parts) >= 2:
                _gid_cand = re.sub(r"\D", "", parts[0])
                # 短数字不是群号：按“单 scale”理解，避免写垃圾 key 污染配置
                if _gid_cand and len(_gid_cand) >= 5:
                    gid = _gid_cand
                    try:
                        scale = int(parts[1])
                    except Exception:
                        scale = None
                else:
                    try:
                        scale = int(parts[0])
                    except Exception:
                        scale = None
                    if scale is not None and not (50 <= scale <= 500):
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
                # 只写新位置 group_configs.font_scale（旧 group_font_scales 仅读兼容，不再写入）
                if scale == 100:
                    raw_gc = cfg.get("group_configs", {})
                    if isinstance(raw_gc, str):
                        try:
                            raw_gc = json.loads(raw_gc) if raw_gc.strip() else {}
                        except Exception:
                            raw_gc = {}
                    if isinstance(raw_gc, dict):
                        cur_gc = raw_gc.get(str(target_gid))
                        if isinstance(cur_gc, dict):
                            cur_gc.pop("font_scale", None)
                            if not cur_gc:
                                raw_gc.pop(str(target_gid), None)
                            self.cfg_mgr.save({"group_configs": raw_gc})
                        else:
                            raw_gc.pop(str(target_gid), None)
                            raw_gc.pop(target_gid, None)
                            self.cfg_mgr.save({"group_configs": raw_gc})
                else:
                    self._update_group_custom(str(target_gid), {"font_scale": scale})
                yield event.plain_result(f"✅ 群 {target_gid} 字体已设为 {scale}%（100% 为默认，自动移除）")
            return
        elif sub in ("group", "群组", "群"):
            # /xbimg group 单群专属配置（原 /text、/style、/theme 已合并至此）
            # 用法：/xbimg group [info|set|style|theme|kw|ttf|list|reset|test] [值] [群号]
            parts = [p for p in re.split(r"[\s,]+", arg) if p]
            cur_gid = self._extract_group_id(event)
            action = parts[0].lower() if parts else ""
            rest = parts[1:] if parts else []

            def _target_gid(tokens):
                for tok in tokens:
                    d = re.sub(r"\D", "", tok)
                    if d and len(d) >= 5:
                        return d
                return cur_gid

            def _gname(gid):
                if gid == cur_gid:
                    return self._get_group_display_name(event, gid)
                try:
                    hist = self._seen_groups.get(gid, {}).get("group_name", "")
                    if hist:
                        return hist
                except Exception:
                    pass
                return f"群 {gid}" if gid else "全局"

            if action in ("", "info", "show", "status", "card", "查看", "状态"):
                target = cur_gid
                if rest:
                    d = re.sub(r"\D", "", rest[0])
                    if d:
                        target = d
                if not target:
                    yield event.plain_result("当前不在群聊中，请附带群号查看，例如：/xbimg group 123456")
                    return
                cur_scale = self._get_group_font_scale(target)
                grp_c = self._get_group_custom_config(target)
                cur_style = grp_c.get("style", cfg.get("style", "ios"))
                cur_theme = grp_c.get("theme_mode", cfg.get("theme_mode", "light"))
                cur_kw = grp_c.get("keyword_preset", "")
                kw_name = "跟随全局默认" if not cur_kw else cur_kw
                yield event.plain_result(
                    f"🎨【{_gname(target)} · 转图专属设置】\n"
                    "--------------------\n"
                    f"当前字体大小：{cur_scale}%\n"
                    f"当前视觉风格：{str(cur_style).upper()}\n"
                    f"当前配色主题：{'浅色' if cur_theme == 'light' else '深色'}\n"
                    f"绑定词库方案：{kw_name}\n"
                    "--------------------\n"
                    "💡 单群指令（均可追加工号操作异地群）：\n"
                    "• /xbimg group set <50-500> [群号] - 单群字体\n"
                    "• /xbimg group style ios / android16 / default [群号]\n"
                    "• /xbimg group theme light / dark / default [群号]\n"
                    "• /xbimg group kw <词库ID> / default [群号]\n"
                    "• /xbimg group ttf <字体id> [群号]\n"
                    "• /xbimg group list - 字体与词库列表\n"
                    "• /xbimg group reset [群号] - 恢复跟随全局\n"
                    "• /xbimg group test [群号] - 单群效果测试图"
                )
                return
            if action in ("list", "列表"):
                try:
                    from .renderer import CURATED_FONTS as _CF
                except Exception:
                    try:
                        from core.renderer import CURATED_FONTS as _CF
                    except Exception:
                        _CF = []
                presets = cfg.get("keyword_presets", {})
                if not isinstance(presets, dict):
                    presets = {}
                lines = [f"🔤【可用字体列表】（共 {len(_CF)} 款）:"]
                for idx, item in enumerate(_CF, 1):
                    lines.append(f"{idx}. {item['name']} - id: {item['id']}")
                lines.append("\n🛡️【可用敏感词库方案】:")
                for pk, pv in presets.items():
                    pname = pv.get("name", pk) if isinstance(pv, dict) else pk
                    lines.append(f"• {pk} - {pname}")
                lines.append("\n💡 切换指令：\n• /xbimg group ttf <字体id> [群号]\n• /xbimg group kw <词库id> [群号]")
                yield event.plain_result("\n".join(lines))
                return
            if action in ("set", "size", "font", "fontsize", "字号"):
                ints = []
                for tok in rest:
                    d = re.sub(r"\D", "", tok)
                    if d:
                        try:
                            ints.append(int(d))
                        except Exception:
                            pass
                scale = next((n for n in ints if 50 <= n <= 500), None)
                gid_tok = next((str(n) for n in ints if len(str(n)) >= 5 and n != scale), "")
                target = gid_tok or cur_gid
                if scale is None:
                    cur = self._get_group_font_scale(target) if target else int(cfg.get("font_scale", 100) or 100)
                    yield event.plain_result(f"用法：/xbimg group set <50-500> [群号]\n当前{('群 ' + target) if target else '全局'}字体：{cur}%\n示例：/xbimg group set 120")
                    return
                if not target:
                    cfg["font_scale"] = scale
                    self.cfg_mgr.save({"font_scale": scale})
                    yield event.plain_result(f"✅ 全局字体已设为 {scale}%")
                else:
                    self._update_group_custom(target, {"font_scale": scale})
                    yield event.plain_result(f"✅ 已将【{_gname(target)}】的字体大小设置为 {scale}%")
                return
            if action in ("style", "风格"):
                value = (rest[0].lower() if rest else "")
                target = _target_gid(rest[1:])
                if value in ("default", "reset", "跟随", "默认", ""):
                    if not target:
                        yield event.plain_result("当前不在群聊中，请附带群号，例如：/xbimg group style default 123456")
                        return
                    self._update_group_custom(target, {"style": ""})
                    yield event.plain_result(f"✅ 已将【{_gname(target)}】的视觉风格恢复为跟随全局默认。")
                    return
                if value in ("ios", "android16", "android"):
                    st = "android16" if "android" in value else "ios"
                    if not target:
                        yield event.plain_result("当前不在群聊中，请附带群号。")
                        return
                    self._update_group_custom(target, {"style": st})
                    yield event.plain_result(f"✅ 已将【{_gname(target)}】的视觉风格设置为：{st.upper()}")
                else:
                    yield event.plain_result("用法：/xbimg group style ios / android16 / default [群号]")
                return
            if action in ("theme", "配色"):
                value = (rest[0].lower() if rest else "")
                target = _target_gid(rest[1:])
                if value in ("default", "reset", "跟随", "默认", ""):
                    if not target:
                        yield event.plain_result("当前不在群聊中，请附带群号，例如：/xbimg group theme default 123456")
                        return
                    self._update_group_custom(target, {"theme_mode": ""})
                    yield event.plain_result(f"✅ 已将【{_gname(target)}】的配色主题恢复为跟随全局默认。")
                    return
                if value in ("light", "dark", "浅色", "深色"):
                    tm = "dark" if value in ("dark", "深色") else "light"
                    if not target:
                        yield event.plain_result("当前不在群聊中，请附带群号。")
                        return
                    self._update_group_custom(target, {"theme_mode": tm})
                    yield event.plain_result(f"✅ 已将【{_gname(target)}】的配色主题设置为：{'深色暗黑' if tm == 'dark' else '浅色明亮'}")
                else:
                    yield event.plain_result("用法：/xbimg group theme light / dark / default [群号]")
                return
            if action in ("kw", "preset", "词库"):
                value = (rest[0] if rest else "").strip()
                target = _target_gid(rest[1:])
                presets = cfg.get("keyword_presets", {})
                if not isinstance(presets, dict):
                    presets = {}
                if value.lower() in ("default", "reset", "跟随", "默认", ""):
                    if not target:
                        yield event.plain_result("当前不在群聊中，请附带群号。")
                        return
                    self._update_group_custom(target, {"keyword_preset": ""})
                    yield event.plain_result(f"✅ 已将【{_gname(target)}】绑定的敏感词库恢复为：跟随全局默认。")
                    return
                if value in presets:
                    if not target:
                        yield event.plain_result("当前不在群聊中，请附带群号。")
                        return
                    self._update_group_custom(target, {"keyword_preset": value})
                    pname = presets[value].get("name", value) if isinstance(presets[value], dict) else value
                    yield event.plain_result(f"✅ 已将【{_gname(target)}】绑定的敏感词库切换为：{pname}")
                else:
                    names = [f"• {k} - {(v.get('name') if isinstance(v, dict) else k)}" for k, v in presets.items()]
                    yield event.plain_result("用法：/xbimg group kw <词库方案ID> / default [群号]\n当前可用词库：\n" + "\n".join(names))
                return
            if action == "ttf":
                fid = (rest[0] if rest else "").strip()
                target = _target_gid(rest[1:])
                if not fid:
                    yield event.plain_result("用法：/xbimg group ttf <字体id> [群号]\n可发送 /xbimg group list 查看可用 id。")
                    return
                try:
                    from .renderer import CURATED_FONTS as _CF2, download_curated_font as _dl2
                except Exception:
                    try:
                        from core.renderer import CURATED_FONTS as _CF2, download_curated_font as _dl2
                    except Exception:
                        _CF2 = []
                        _dl2 = None
                if _dl2 is None:
                    yield event.plain_result("❌ 字体模块不可用，无法准备字体包。")
                    return
                hit = next((x for x in _CF2 if x["id"].lower() == fid.lower()), None)
                if not hit:
                    yield event.plain_result(f"❌ 未找到字体 id「{fid}」，请发送 /xbimg group list 查看支持的列表。")
                    return
                yield event.plain_result(f"⏳ 正在检查【{hit['name']}】字体包…")
                res = await asyncio.to_thread(_dl2, hit["id"])
                if not res.get("ok"):
                    yield event.plain_result(f"❌ 字体准备失败: {res.get('error', '网络异常')}")
                    return
                fname = res.get("downloaded", [""])[0] or hit["files"][0][0]
                if target:
                    self._update_group_custom(target, {"custom_font_path": fname})
                    yield event.plain_result(f"✅ 成功将【{_gname(target)}】的字体切换为：{hit['name']}")
                else:
                    self.cfg_mgr.save({"font_source": "custom", "custom_font_path": fname})
                    yield event.plain_result(f"✅ 全局字体已切换为：{hit['name']}")
                return
            if action in ("reset", "default", "恢复默认", "重置"):
                target = _target_gid(rest) or cur_gid
                if not target:
                    yield event.plain_result("当前不在群聊中，请附带群号，例如：/xbimg group reset 123456")
                    return
                raw = cfg.get("group_configs", {})
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw) if raw.strip() else {}
                    except Exception:
                        raw = {}
                if isinstance(raw, dict) and (str(target) in raw or target in raw):
                    raw.pop(str(target), None)
                    raw.pop(target, None)
                    self.cfg_mgr.save({"group_configs": raw})
                raw_sc = cfg.get("group_font_scales", {})
                if isinstance(raw_sc, str):
                    try:
                        raw_sc = json.loads(raw_sc) if raw_sc.strip() else {}
                    except Exception:
                        raw_sc = {}
                if isinstance(raw_sc, dict) and (str(target) in raw_sc or target in raw_sc):
                    raw_sc.pop(str(target), None)
                    raw_sc.pop(target, None)
                    self.cfg_mgr.save({"group_font_scales": raw_sc})
                yield event.plain_result(f"✅ 已恢复【{_gname(target)}】的所有专属设置，完全跟随全局默认。")
                return
            if action == "test":
                if not self._test_cooldown_ok(event, "grouptest"):
                    yield event.plain_result("⏳ 测试太频繁，请 15 秒后再试。")
                    return
                target = _target_gid(rest) or cur_gid
                eff_scale = self._get_group_font_scale(target) if target else int(cfg.get("font_scale", 100) or 100)
                grp_c = self._get_group_custom_config(target) if target else {}
                style_eff = grp_c.get("style", cfg.get("style", "ios"))
                theme_eff = grp_c.get("theme_mode", cfg.get("theme_mode", "light"))
                tname = _gname(target) if target else "全局"
                _perf_g = self._perf_enabled()
                _t0g = time.perf_counter() if _perf_g else 0.0
                _perf_out_g = {} if _perf_g else None
                test_content = (
                    f"# 🎨【{tname}】专属渲染效果测试\n"
                    "这是一段用于测试消息转图排版与美观度的标准文本。\n\n"
                    "- 链接测试：https://astrbot.app 欢迎访问官方网站\n"
                    "- 模拟打码：本行包含测试词展示半马赛克精美效果\n"
                    "- 代码展示：\n"
                    "```python\n"
                    "def hello_world():\n"
                    "    print('Msg2Img by xbimg - Test Passed!')\n"
                    "```"
                )
                try:
                    img = await asyncio.to_thread(
                        MessageImageRenderer.render_pages,
                        text=test_content,
                        style=style_eff,
                        theme_mode=theme_eff,
                        star_background=bool(cfg.get("star_background", True)),
                        star_density=str(cfg.get("star_density", "medium")),
                        mosaic_mode="half",
                        mosaic_type=str(cfg.get("mosaic_type", "pixel")),
                        violation_words=["测试词"],
                        emoji_remote=True,
                        mosaic_half_pos=str(cfg.get("mosaic_half_pos", "bottom")),
                        font_scale=eff_scale,
                        custom_font_path=str(grp_c.get("custom_font_path", "") or ""),
                        custom_bold_font_path=str(grp_c.get("custom_bold_font_path", "") or ""),
                        perf_out=_perf_out_g,
                    )
                except Exception as e:
                    yield event.plain_result(f"❌ 测试图生成失败: {e}")
                    return
                chain_imgs = []
                chain_pairs = []
                if _perf_g:
                    _t_saveg = time.perf_counter()
                for _idx, _im in enumerate(img or []):
                    try:
                        img_filename = f"test_{int(time.time()*1000)}_{_idx}_{os.urandom(2).hex()}.png"
                        img_path = self.cache_dir / img_filename
                        await asyncio.to_thread(_im.save, str(img_path), "PNG")
                        self._schedule_delete(img_path, 45)
                        chain_pairs.append((_im, img_path))
                        chain_imgs.append(AstrImage.fromFileSystem(str(img_path)))
                    except Exception as e:
                        logger.warning(f"[{PLUGIN_NAME}] 测试图落盘失败: {e}")
                if _perf_g:
                    try:
                        _mo, _pf, _dr, _la = self._perf_vals(_perf_out_g)
                        self._emit_perf_log(
                            tname, (time.perf_counter() - _t0g) * 1000.0,
                            0.0, _mo, _pf,
                            [p[0] for p in chain_pairs], [p[1] for p in chain_pairs],
                            draw_ms=_dr, layout_ms=_la,
                            save_ms=(time.perf_counter() - _t_saveg) * 1000.0,
                        )
                    except Exception:
                        pass
                if not chain_imgs:
                    yield event.plain_result("❌ 测试图生成失败：图片为空。")
                    return
                yield event.chain_result(chain_imgs)
                return
            yield event.plain_result("未知 group 子指令，请输入 /xbimg group 查看单群菜单。")
            return
        elif sub == "test":
            test_content = arg or "这是一条来自 AstrBot 消息转图助手的测试消息！✨\n祝您使用愉快~"
            if not self._test_cooldown_ok(event, "test"):
                yield event.plain_result("⏳ 测试太频繁，请 15 秒后再试。")
                return
            _perf_t = self._perf_enabled()
            _t0t = time.perf_counter() if _perf_t else 0.0
            _t_modt = _t0t if _perf_t else 0.0
            _perf_out_t = {} if _perf_t else None
            try:
                # 测试文本同样过审查：block 直接拒绝，mosaic 则如实打出马赛克效果
                _tmod, _teff, _tmosaic = await self._moderate_text(test_content)
            except Exception as e:
                yield event.plain_result(f"❌ 测试图生成失败: {e}")
                return
            _mod_ms_t = (time.perf_counter() - _t_modt) * 1000.0 if _perf_t else 0.0
            if _tmod.is_violated and _tmod.action == "block":
                self.cfg_mgr.record_render(is_violated=True, count_total=False)
                if _perf_t:
                    try:
                        _gid_t = self._extract_group_id(event)
                        self._emit_perf_log(
                            f"群{_gid_t}" if _gid_t else "私聊",
                            (time.perf_counter() - _t0t) * 1000.0,
                            _mod_ms_t, 0.0, 0.0, [], [], blocked=True,
                        )
                    except Exception:
                        pass
                yield event.plain_result("🚫 测试文本触发安全审查，已拦截不生成。")
                return
            try:
                _tfs = int(cfg.get("font_scale", 100) or 100)
            except Exception:
                _tfs = 100
            try:
                imgs = await self._render_moderated(
                    _teff, _tmod, _tmosaic, font_scale=_tfs,
                    perf_out=_perf_out_t,
                )
            except Exception as e:
                yield event.plain_result(f"❌ 测试图生成失败: {e}")
                return
            test_imgs = []
            test_pairs = []
            _t_savet = time.perf_counter() if _perf_t else 0.0
            for _idx, _im in enumerate(imgs or []):
                # 毫秒+序号+随机：同秒并发 test 不互相覆盖；45 秒后自动清理
                try:
                    test_path = self.cache_dir / f"test_{int(time.time()*1000)}_{_idx}_{os.urandom(2).hex()}.png"
                    await asyncio.to_thread(_im.save, str(test_path), "PNG")
                    self._schedule_delete(test_path, 45)
                    test_pairs.append((_im, test_path))
                    test_imgs.append(AstrImage.fromFileSystem(str(test_path)))
                except Exception as e:
                    logger.warning(f"[{PLUGIN_NAME}] 测试图落盘失败: {e}")
            if _perf_t:
                try:
                    _gid_t2 = self._extract_group_id(event)
                    _mo, _pf, _dr, _la = self._perf_vals(_perf_out_t)
                    self._emit_perf_log(
                        f"群{_gid_t2}" if _gid_t2 else "私聊",
                        (time.perf_counter() - _t0t) * 1000.0, _mod_ms_t,
                        _mo, _pf, [p[0] for p in test_pairs], [p[1] for p in test_pairs],
                        draw_ms=_dr, layout_ms=_la,
                        save_ms=(time.perf_counter() - _t_savet) * 1000.0,
                    )
                except Exception:
                    pass
            if not test_imgs:
                yield event.plain_result("❌ 测试图生成失败：图片为空。")
                return
            yield event.chain_result(test_imgs)
        else:
            yield event.plain_result("未知子指令，请输入 /xbimg 查看指令菜单。")

    # /text、/style、/theme 已合并至 /xbimg group（见 cmd_xbimg），此处不再保留重复指令



    def _test_cooldown_ok(self, event: AstrMessageEvent, name: str, seconds: int = 15) -> bool:
        """测试类指令冷却（按群+人分别限频，一人刷不影响他人），通过返回 True 并打点"""
        try:
            gid = self._extract_group_id(event) or "private"
        except Exception:
            gid = "private"
        try:
            uid = ""
            fn = getattr(event, "get_sender_id", None)
            if callable(fn):
                uid = str(fn() or "")
            if not uid:
                uid = str(getattr(event, "sender_id", "") or "")
            if not uid:
                mo = getattr(event, "message_obj", None)
                uid = str(getattr(mo, "sender_id", "") or getattr(getattr(mo, "sender", None), "user_id", "") or "")
            uid = uid.strip() or "unknown"
        except Exception:
            uid = "unknown"
        key = f"{name}:{gid}:{uid}"
        now = time.time()
        try:
            if len(self._test_cooldowns) > 2000:
                # 定向淘汰过期项，避免全清放过正在冷却的用户
                try:
                    for k, ts in list(self._test_cooldowns.items()):
                        if now - float(ts or 0) >= seconds:
                            self._test_cooldowns.pop(k, None)
                    while len(self._test_cooldowns) > 2000:
                        self._test_cooldowns.pop(next(iter(self._test_cooldowns)), None)
                except Exception:
                    self._test_cooldowns.clear()
            last = float(self._test_cooldowns.get(key, 0) or 0)
            if now - last < seconds:
                return False
            self._test_cooldowns[key] = now
            return True
        except Exception:
            return True
