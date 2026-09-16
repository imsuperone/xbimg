# -*- coding: utf-8 -*-
"""
插件配置管理与持久化模块
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

PLUGIN_NAME = "astrbot_plugin_msg2img"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enable": True,
    "style": "ios",
    "theme_mode": "light",
    "star_background": True,
    "star_density": "medium",
    "min_length_threshold": 1,
    "link_mode": "as_image",
    "moderation_mode": "keywords",
    "enable_ai_moderation": False,
    "violation_action": "mosaic_half",
    "mosaic_type": "pixel",
    "custom_keywords": "违禁,赌博,色情,暴力,诈骗,挂机脚本,发票代开",
    "img_compress_level": "medium",
    "custom_ai_prompt": "",
    "group_configs": {},
    "ai_provider_mode": "astrbot",
    "ai_astrbot_model": "",
    "ai_api_base": "",
    "ai_api_key": "",
    "ai_model": "gpt-4o-mini",
    "group_mode": "whitelist",
    "group_list": "",
    "render_trigger": "always",
    "mosaic_half_pos": "bottom",
    "font_scale": 100,
    "group_font_scales": {},
    "emoji_style": "none",
    "font_source": "auto",
    "custom_font_path": "",
    "custom_bold_font_path": "",
    "custom_font_url": "",
    "emoji_remote": True,
}


def resolve_data_dir() -> Path:
    """解析持久化数据目录"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        p = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        pass

    for cand in [
        Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME,
        Path(__file__).resolve().parent.parent / "data" / "plugin_data" / PLUGIN_NAME,
    ]:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            return cand.resolve()
        except Exception:
            continue
    return Path.cwd()


class ConfigManager:
    # 统计落盘节流：高频群聊下避免每条消息都读写 stats.json
    STATS_FLUSH_INTERVAL = 10.0

    def __init__(self, raw_cfg: Dict[str, Any] = None):
        self.raw_cfg = raw_cfg
        self.data_dir = resolve_data_dir()
        self.cfg_file = self.data_dir / "config.json"
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stats_file = self.data_dir / "stats.json"
        # 统计内存缓存（首读后常驻内存，写节流落盘）
        self._stats: Optional[Dict[str, Any]] = None
        self._stats_last_flush: float = 0.0

        self.config = dict(DEFAULT_CONFIG)
        if raw_cfg:
            try:
                if isinstance(raw_cfg, dict):
                    self.config.update(raw_cfg)
                else:
                    # AstrBot 原生配置对象：按 key 逐个读取
                    for k in DEFAULT_CONFIG:
                        try:
                            v = raw_cfg[k]
                            if v is not None:
                                self.config[k] = v
                        except Exception:
                            continue
            except Exception:
                pass
        self._load()

    def _load(self):
        if self.cfg_file.exists():
            try:
                with open(self.cfg_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    if isinstance(saved, dict):
                        self.config.update(saved)
            except Exception:
                pass

    def save(self, new_cfg: Dict[str, Any] = None):
        if new_cfg:
            self.config.update(new_cfg)
        try:
            with open(self.cfg_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        # save 时顺带把节流中的统计落盘，避免进程退出丢数
        self._flush_stats()

        # 同步回写至 AstrBot 原生配置对象
        if hasattr(self, "raw_cfg") and self.raw_cfg is not None:
            try:
                for k, v in self.config.items():
                    if hasattr(self.raw_cfg, "__setitem__"):
                        self.raw_cfg[k] = v
                if hasattr(self.raw_cfg, "save") and callable(self.raw_cfg.save):
                    self.raw_cfg.save()
            except Exception:
                pass

    def get_stats(self) -> Dict[str, Any]:
        if self._stats is not None:
            return dict(self._stats)
        stats = {
            "total_rendered": 0,
            "violations_blocked": 0,
            "mosaic_applied": 0,
            "last_active": 0,
        }
        if self.stats_file.exists():
            try:
                with open(self.stats_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        stats.update(data)
            except Exception:
                pass
        self._stats = stats
        return dict(stats)

    def _flush_stats(self):
        if self._stats is None:
            return
        try:
            with open(self.stats_file, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
            self._stats_last_flush = time.time()
        except Exception:
            pass

    def record_render(self, is_violated: bool = False, is_mosaic: bool = False):
        stats = self.get_stats()
        stats["total_rendered"] = int(stats.get("total_rendered", 0)) + 1
        stats["last_active"] = int(time.time())
        if is_violated:
            stats["violations_blocked"] = int(stats.get("violations_blocked", 0)) + 1
        if is_mosaic:
            stats["mosaic_applied"] = int(stats.get("mosaic_applied", 0)) + 1
        self._stats = stats
        # 违规立即落盘保证不丢，普通渲染节流落盘
        if is_violated or (time.time() - self._stats_last_flush) >= self.STATS_FLUSH_INTERVAL:
            self._flush_stats()
