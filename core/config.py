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

DEFAULT_KEYWORD_PRESETS: Dict[str, Dict[str, str]] = {
    "default": {
        "name": "标准涉敏与违禁词库 (默认综合)",
        "keywords": (
            "贩毒,吸毒,冰毒,海洛因,大麻,摇头丸,麻古,K粉,可卡因,芬太尼,毒瘾,溜冰,毒品,迷药,迷奸,下药,"
            "赌博,澳门赌场,澳门首家,轮盘赌,百家乐,外围赌球,六合彩,跑分,地下钱庄,洗钱,庄家,出千,赌场返水,"
            "色情,黄片,幼女,萝莉岛,援交,约炮,裸聊,偷拍走光,露点,私密照,迷奸水,强奸,催情春药,乱伦换妻,卖淫嫖娼,包养模特,"
            "操逼,肏逼,插穴,口交,射精,潮吹,自慰,后庭爆菊,群交淫趴,成人网站,无码av,三级片,"
            "诈骗,杀猪盘,刷单返利,挂机脚本,游戏外挂,透视自瞄,辅助破解,代开增值税发票,办假证,开盒人肉,人肉搜索,查户籍,呼死你,"
            "枪支弹药,管制刀具,自制炸药,氰化钾,高利贷催收,绑架勒索,买卖人口,拐卖儿童,暗网交易"
        ),
    },
    "anti_porn": {
        "name": "严格涉黄与低俗色情库",
        "keywords": (
            "色情,黄片,幼女,萝莉岛,援交,约炮,裸聊,裸照,偷拍,露点,原味内衣,迷药,强奸,催情春药,乱伦,换妻,卖淫,嫖娼,包养,"
            "操逼,肏逼,插穴,口交,吞精,射精,高潮,阴道,肉缝,龟头,阴茎,肉棒,潮吹,后庭,爆菊,群交,淫趴,三级毛片,av女优,番号,无码,成人片"
        ),
    },
    "anti_gambling": {
        "name": "严格涉赌涉诈与黑产库",
        "keywords": (
            "赌博,澳门赌场,澳门首家,线上赌场,轮盘赌,百家乐,跑分洗钱,地下钱庄,炸金花,六合彩特码,外围赌球,菠菜平台,出千,赌场抽头,"
            "诈骗,杀猪盘,刷单兼职,挂机脚本,游戏透视自瞄,开专用发票,代办假证,查档查户籍,开盒挂人,呼死你轰炸,出售银行卡四件套,购买实名微信号"
        ),
    },
    "anti_drugs": {
        "name": "严格涉毒与违禁违禁品库",
        "keywords": (
            "贩毒,吸毒,冰毒,海洛因,大麻,摇头丸,麻古,K粉,氯胺酮,可卡因,吗啡,芬太尼,毒瘾,溜冰,飞行员,毒品交易,丧尸药,蓝精灵,聪明药,上头电子烟,罂粟"
        ),
    },
    "xbbot_game": {
        "name": "互动娱乐/涉黑调教过滤库",
        "keywords": (
            "奴隶买卖,折磨奴隶,买下奴隶,皮鞭调教,关小黑屋,滴蜡酷刑,逼良为娼,强行卖身,拐卖人口,抢劫金币,偷窃财产,赌场下注,赌庄出千,脚本刷币,辅助刷钱,私下交易"
        ),
    },
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "enable": True,
    "style": "ios",
    "theme_mode": "light",
    "star_background": True,
    "star_density": "medium",
    "min_length_threshold": 1,
    "link_mode": "as_image",
    "enable_keywords_moderation": True,
    "moderation_mode": "keywords",
    "enable_ai_moderation": False,
    "violation_action": "mosaic_half",
    "mosaic_type": "pixel",
    "active_keyword_preset": "default",
    "keyword_presets": DEFAULT_KEYWORD_PRESETS,
    "custom_keywords": DEFAULT_KEYWORD_PRESETS["default"]["keywords"],
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
