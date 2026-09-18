# -*- coding: utf-8 -*-
"""
插件配置管理与持久化模块
"""

import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_NAME = "astrbot_plugin_xbimg"

DEFAULT_KEYWORD_PRESETS: Dict[str, Dict[str, str]] = {
    "anti_nsfw_ultra": {
        "name": "🔥 强力综合违禁与色情库 (日常/AI发情/SM/玩法/百合/小白)",
        "keywords": (
            "色情,黄色,搞黄色,做爱,性交,房事,野战,车震,口交,深喉,吞精,颜射,口爆,射精,射在里面,内射,中出,精液,精子,浓精,精液狂飙,射爆,灌满,"
            "自慰,手淫,撸管,打飞机,飞机杯,扣逼,扣穴,手交,足交,乳交,舔逼,舔阴,舔穴,舔肛,毒龙钻,潮吹,喷水,绝顶,高潮,高潮抽搐,呻吟,娇喘,发情,发骚,"
            "骚逼,骚货,骚母狗,肉便器,性奴,母狗调教,母猪,发情母狗,荡妇,淫妇,小骚货,小淫娃,阴道,小穴,肉穴,肉缝,花蕊,花缝,蜜穴,蜜汁,淫水,紧致,湿透,"
            "鸡巴,肉棒,大肉棒,巨根,阴茎,龟头,屌,抽插,猛烈抽插,狠狠干,狠狠操,操死你,肏逼,操逼,干死你,干烂,破处,开苞,破身,处女膜,落红,"
            "奶子,大胸,大波,巨乳,揉奶,抓奶,揉胸,乳头,乳晕,凸点,露点,偷拍,走光,私密照,裸照,裸聊,裸体,无码,无修正,步兵,骑兵,av女优,番号,三级片,黄片,成人网站,"
            "援交,约炮,卖淫,嫖娼,包养,外围女,楼凤,上门服务,洗浴暗娼,站街女,强奸,轮奸,迷奸,下药,听话水,催情春药,乱伦,换妻,绿帽,ntr,幼女,萝莉岛,"
            "催眠调教,恶堕,触手侵犯,产卵,强制高潮,绝顶升天,肉体契约,深度开发,敏感体质,敏感点,肆意玩弄,玩坏,肉体玩物,榨干精气,榨精,榨汁机,雌堕,雌犬化,"
            "身体渴望,止不住的流水,承受不住,狠狠进入,深入到底,抵到花心,顶到宫颈,疯狂顶撞,贯穿到底,填满肉穴,塞满小穴,注入爱液,注入浓精,无法合拢,瘫软无力,痉挛颤抖,"
            "翻白眼吐舌头,失去理智,彻底沦陷,放荡浪叫,淫语连篇,求你干我,求你给我,想要大肉棒,被玩弄得神志不清,沦为欲望的奴隶,身体诚实地迎合,"
            "SM,BDSM,主奴,字母圈,dom,sub,sp,绳艺,紧缚,龟甲缚,五花大绑,吊缚,束缚,口塞,口枷,环口,开口器,鼻钩,拘束带,拘束衣,贞操带,贞操锁,锁精环,"
            "狗链,项圈,牵引绳,狗爬,狗奴,母狗认主,专属奴隶,私奴,男奴,女奴,滴蜡,皮鞭,散鞭,鞭打,掌掴,抽耳光,抽屁股,打屁股,戒尺,红肿破皮,羞辱惩罚,精神控制,"
            "跪下舔鞋,舔脚趾,踩踏,重度踩踏,圣水,黄金,饮尿,灌肠,后庭爆菊,爆菊,肛交,后庭开发,肛塞,狐尾肛塞,震动棒,跳蛋,遥控跳蛋,假阳具,双头龙,穿刺,乳夹,阴蒂夹,电击,"
            "拘禁,关笼子,小黑屋拘禁,人体盛,私调,公调,绳师,重口调教,"
            "百合色色,磨豆腐,磨逼,磨穴,互相摩擦,指交,百合高潮,蕾丝边,拉拉色情,双头龙对插,互相舔穴,穿戴假阳具,百合捆绑,姐妹百合调教,"
            "奴隶买卖,折磨奴隶,买下奴隶,皮鞭调教,关小黑屋,逼良为娼,强行卖身,拐卖人口,抢劫金币,偷窃财产,赌场下注,赌庄出千,挂机刷币,脚本刷币,辅助刷钱,私下交易,"
            "充值漏洞,破解脚本,绑架勒索,撕票,下毒暗算,强夺奴隶,奴隶市场,奴隶逃跑,烙印惩罚,私设刑房,奴隶契约,"
            "抢银行,银行抢劫,银行劫案,银行,劫狱,越狱,监狱,奴隶"
        ),
    },
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
            "操逼,肏逼,插穴,口交,吞精,射精,高潮,阴道,肉缝,龟头,阴茎,肉棒,潮吹,后庭,爆菊,群交,淫趴,三级毛片,av女优,番号,无码,成人片,性奴,调教,主奴,母狗,自慰,飞机杯"
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

# 关键词切分（与 moderation 保持一致：逗号/分号/换行分隔）
_SPLIT_KW_RE = re.compile(r"[,;，；\n]+")


def _preset_keywords(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("keywords", "") or "")
    return str(entry or "")


def _split_kw_set(raw: str) -> set:
    return {t for t in (x.strip() for x in _SPLIT_KW_RE.split(raw or "")) if t}


def builtin_presets_hash() -> str:
    """当前代码内置词库快照哈希（插件更新改了词库即变化）"""
    try:
        raw = json.dumps(DEFAULT_KEYWORD_PRESETS, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def _presets_equal_builtin(local: Any) -> bool:
    """本地词库与内置是否完全一致（词+方案名）"""
    if not isinstance(local, dict):
        return False
    for pid, pentry in DEFAULT_KEYWORD_PRESETS.items():
        if pid not in local:
            return False
        if _split_kw_set(_preset_keywords(local[pid])) != _split_kw_set(_preset_keywords(pentry)):
            return False
        lname = local[pid].get("name", "") if isinstance(local[pid], dict) else ""
        if lname != pentry.get("name", ""):
            return False
    return True

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
    "builtin_presets_hash": "",
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

        # 深拷贝默认配置：嵌套词库 dict 与模块常量隔离，避免原地修改污染全局默认
        self.config = copy.deepcopy(DEFAULT_CONFIG)
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
        # 内置词库更新检测：历史配置无哈希且本地与内置一致时直接对齐（内存态，下次保存落盘）；
        # 不一致则保留本地内容，仅标记待确认，绝不自动覆盖用户词库
        try:
            if not self.config.get("builtin_presets_hash"):
                if _presets_equal_builtin(self.config.get("keyword_presets", {})):
                    self.config["builtin_presets_hash"] = builtin_presets_hash()
        except Exception:
            pass

    def _load(self):
        if self.cfg_file.exists():
            try:
                with open(self.cfg_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    if isinstance(saved, dict):
                        # 确保新版本的默认预设不丢失，同时保留用户自定义修改；
                        # 内置条目深拷贝，与模块常量隔离
                        old_presets = saved.get("keyword_presets")
                        if isinstance(old_presets, dict):
                            merged = copy.deepcopy(DEFAULT_KEYWORD_PRESETS)
                            merged.update(old_presets)
                            saved["keyword_presets"] = merged
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

    def get_presets_update_status(self) -> Dict[str, Any]:
        """检测内置官方词库相对本地是否有更新，返回差异明细（只读，不改配置）"""
        cur_hash = builtin_presets_hash()
        stored = str(self.config.get("builtin_presets_hash", "") or "")
        local = self.config.get("keyword_presets", {})
        if not isinstance(local, dict):
            local = {}
        items: List[Dict[str, Any]] = []
        for pid, pentry in DEFAULT_KEYWORD_PRESETS.items():
            bset = _split_kw_set(_preset_keywords(pentry))
            pname = pentry.get("name", pid)
            if pid not in local or not isinstance(local[pid], (dict, str)):
                items.append({
                    "id": pid, "name": pname, "missing": True,
                    "builtin_total": len(bset), "local_total": 0,
                    "added_total": len(bset), "removed_total": 0,
                    "added_sample": sorted(bset)[:8], "removed_sample": [],
                })
                continue
            lset = _split_kw_set(_preset_keywords(local[pid]))
            if bset != lset:
                items.append({
                    "id": pid, "name": pname, "missing": False,
                    "builtin_total": len(bset), "local_total": len(lset),
                    "added_total": len(bset - lset), "removed_total": len(lset - bset),
                    "added_sample": sorted(bset - lset)[:8],
                    "removed_sample": sorted(lset - bset)[:8],
                })
        if not items and stored != cur_hash:
            # 词无差异（仅方案改名等）：内存态对齐哈希，下次保存落盘，不打扰用户
            self.config["builtin_presets_hash"] = cur_hash
            stored = cur_hash
        # 仅当内置版本变化（哈希不一致）且词有差异时才提示；
        # 用户点保留/覆盖后哈希对齐，当前版本不再打扰，下次内置变更再提示
        return {
            "update_available": bool(items) and stored != cur_hash,
            "stored_hash": stored,
            "current_hash": cur_hash,
            "changed": items,
        }

    def apply_builtin_presets(self, ids: Any = None) -> Dict[str, Any]:
        """用内置官方词库覆盖本地指定方案（ids 为空则覆盖全部内置方案），用户自建方案不受影响"""
        if isinstance(ids, str):
            ids = [i.strip() for i in re.split(r"[,;\s]+", ids) if i.strip()]
        wanted = [i for i in (ids or []) if i in DEFAULT_KEYWORD_PRESETS] or list(DEFAULT_KEYWORD_PRESETS.keys())
        local = self.config.get("keyword_presets", {})
        if not isinstance(local, dict):
            local = {}
        else:
            local = dict(local)
        for pid in wanted:
            src = DEFAULT_KEYWORD_PRESETS[pid]
            local[pid] = {"name": src.get("name", pid), "keywords": src.get("keywords", "")}
        self.config["keyword_presets"] = local
        self.config["builtin_presets_hash"] = builtin_presets_hash()
        self.save()
        return self.get_presets_update_status()

    def dismiss_builtin_presets_update(self) -> Dict[str, Any]:
        """保留本地词库不再提示（记录当前内置哈希，下次内置变更时再提示）"""
        self.config["builtin_presets_hash"] = builtin_presets_hash()
        self.save()
        return self.get_presets_update_status()

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
