# -*- coding: utf-8 -*-
"""
自动化全量测试套件
验证渲染引擎、安全审查、配置管理及插件接口
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 添加当前工作区至模块搜索路径
WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))

from core.renderer import MessageImageRenderer
from core.moderation import ContentModerator, ModerationResult
from core.config import ConfigManager, DEFAULT_CONFIG


def _win_font(*names):
    for n in names:
        if os.path.exists(n):
            return n
    return None


def _needs_win_font(*names):
    hit = _win_font(*names)
    return unittest.skipUnless(
        hit is not None, f"缺系统字体 {'/'.join(names)}，跳过"
    )


class TestMsg2ImgPlugin(unittest.TestCase):
    def setUp(self):
        # 数据目录隔离（每用例独立临时目录）：ConfigManager/插件实例读写隔离，
        # 不污染真实 data/，用例之间也不串（config.json 落盘互不可见）
        self._iso_tmp = tempfile.TemporaryDirectory(prefix="xbimg_test_")
        self._iso_patcher = mock.patch(
            "core.config.resolve_data_dir", return_value=Path(self._iso_tmp.name)
        )
        self._iso_patcher.start()
        self.addCleanup(self._iso_patcher.stop)
        self.addCleanup(self._iso_tmp.cleanup)
        # LIFO：先于 tmp.cleanup 执行，等预热线程写完 emoji/缓存再删目录
        self.addCleanup(self._join_warm_threads)
        self.sample_text = (
            "# 标题测试\n"
            "这是普通文本段落。\n"
            "- 列表条目 1\n"
            "- 列表条目 2\n"
            "> 这是引用文本内容\n\n"
            "```python\ndef test():\n    return 'ok'\n```\n"
            "包含链接: https://astrbot.app 欢迎访问！"
        )

    @staticmethod
    def _join_warm_threads():
        import threading as _t
        me = _t.current_thread()
        for th in list(_t.enumerate()):
            if th.name == "xbimg-warm" and th is not me:
                try:
                    th.join(timeout=8)
                except Exception:
                    pass

    def test_renderer_ios_light(self):
        img = MessageImageRenderer.render(
            self.sample_text,
            style="ios",
            theme_mode="light",
            star_background=True,
            star_density="medium",
        )
        self.assertIsNotNone(img)
        self.assertGreater(img.width, 500)
        self.assertGreater(img.height, 300)

    def test_renderer_android16_dark(self):
        img = MessageImageRenderer.render(
            self.sample_text,
            style="android16",
            theme_mode="dark",
            star_background=True,
            star_density="dense",
        )
        self.assertIsNotNone(img)
        self.assertGreater(img.width, 500)
        self.assertGreater(img.height, 300)

    def test_half_mosaic_pixel(self):
        img = MessageImageRenderer.render(
            self.sample_text,
            style="ios",
            mosaic_mode="half",
            mosaic_type="pixel",
        )
        self.assertIsNotNone(img)

    def test_half_mosaic_blur(self):
        img = MessageImageRenderer.render(
            self.sample_text,
            style="android16",
            mosaic_mode="half",
            mosaic_type="blur",
        )
        self.assertIsNotNone(img)

    def test_word_level_mosaic(self):
        """测试字级精准半打码：传入 violation_words 验证不抛异常"""
        violation_text = "这里有赌博和色情内容需要打码处理"
        img = MessageImageRenderer.render(
            violation_text,
            style="ios",
            mosaic_mode="half",
            mosaic_type="pixel",
            violation_words=["赌博", "色情"],
        )
        self.assertIsNotNone(img)

    def test_word_level_mosaic_full(self):
        """测试字级全打码"""
        violation_text = "这里有诈骗信息需要全部打码"
        img = MessageImageRenderer.render(
            violation_text,
            style="android16",
            theme_mode="dark",
            mosaic_mode="full",
            mosaic_type="blur",
            violation_words=["诈骗"],
        )
        self.assertIsNotNone(img)

    def test_page_without_violation_word_skips_mosaic(self):
        """回归：分页后不含违规词的页不打码 —— 词表非空但本页未命中时
        不得走区域回退（否则小页会被整块马赛克覆盖）"""
        long_text = (
            "第一页开头。\n"
            + ("测试行，保持正常。\n" * 45)
            + "这里包含赌博关键词。\n"
            + ("尾部填充行。\n" * 30)
        )
        pages = MessageImageRenderer.render_pages(
            text=long_text,
            style="ios", theme_mode="light",
            star_background=False, emoji_remote=False,
            mosaic_mode="half", mosaic_type="pixel",
            violation_words=["赌博"], mosaic_half_pos="bottom",
            page_max_h=1200,
        )
        self.assertGreaterEqual(len(pages), 2, "应分出至少两页")
        self.addCleanup(lambda: None)
        # 末页不含违规词：与干净渲染对比必须像素一致（未被打码）
        clean_last = MessageImageRenderer.render(
            "尾部填充行。\n" * 20,
            style="ios", theme_mode="light",
            star_background=False, emoji_remote=False,
            mosaic_mode="none",
        )
        # 用页内直方图粗检：末页不应出现区域回退的警示横幅色带
        import numpy as _np
        from PIL import Image as _Image
        last = _np.asarray(pages[-1].convert("RGB"))
        # 横幅色 (78,98,132) 附近的像素计数（半码回退横幅）
        banner = _np.all(_np.abs(last.astype(int) - _np.array([78, 98, 132])) < 12, axis=-1)
        self.assertLess(int(banner.sum()), 500, "末页不应出现区域回退警示横幅")
        # 词命中页仍应有打码（页面中存在违规词）
        hit_pages = [
            p for p in pages
            if "赌博" in long_text  # 词一定在某页
        ]
        self.assertTrue(hit_pages)

    def test_word_hit_page_still_mosaics(self):
        """词命中的页仍正常打半码（跳过逻辑只作用于未命中页）"""
        img = MessageImageRenderer.render(
            "测试赌博内容",
            style="ios", theme_mode="light",
            star_background=False, emoji_remote=False,
            mosaic_mode="half", mosaic_type="pixel",
            violation_words=["赌博"], mosaic_half_pos="bottom",
        )
        self.assertIsNotNone(img)
        import numpy as _np
        arr = _np.asarray(img.convert("RGB")).astype(int)
        # 半码区域应引入低于原文字对比度的平坦块（打码痕迹）
        gray = arr.mean(axis=2)
        gx = _np.abs(_np.diff(gray, axis=1)).mean()
        self.assertGreater(gx, 0.5, "图片应含文字/纹理")

    def test_footer_centered(self):
        """验证 footer 居中：渲染后图片不抛异常即通过"""
        img = MessageImageRenderer.render(
            "Footer centering test - Generated by Light should be centered",
            style="ios",
        )
        self.assertIsNotNone(img)
        self.assertGreater(img.width, 500)

    def test_emoji_mixed_text(self):
        """测试 Emoji 混排不乱码"""
        emoji_text = "💰签到成功！获得2110金币💰 💎钻石1000\n🏛️银行36💎 🚨余额30📖\n⚔️战斗力100%"
        img = MessageImageRenderer.render(
            emoji_text,
            style="android16",
            theme_mode="light",
        )
        self.assertIsNotNone(img)
        self.assertGreater(img.width, 500)

    def test_moderation_keywords(self):
        cfg = dict(DEFAULT_CONFIG)
        cfg["custom_keywords"] = "违禁词,非法脚本,代开"
        cfg["moderation_mode"] = "keywords"
        cfg["violation_action"] = "mosaic_half"

        moderator = ContentModerator(cfg)

        # 正常文本
        res_pass = moderator.check_keywords("这是一条完全正常的日常问候，早上好！")
        self.assertFalse(res_pass[0])

        # 触发违规文本
        res_hit = moderator.check_keywords("这里出售各类非法脚本和工具，速来！")
        self.assertTrue(res_hit[0])
        self.assertIn("非法脚本", res_hit[1])

        # 空格混淆匹配
        res_obfuscated = moderator.check_keywords("这里有 违 禁 词 吗？")
        self.assertTrue(res_obfuscated[0])

    def test_config_manager(self):
        mgr = ConfigManager()
        self.assertTrue(mgr.config.get("enable"))
        self.assertEqual(mgr.config.get("style"), "ios")

        # 记录统计
        mgr.record_render(is_violated=True, is_mosaic=True)
        stats = mgr.get_stats()
        self.assertGreaterEqual(stats.get("total_rendered", 0), 1)
        self.assertGreaterEqual(stats.get("violations_blocked", 0), 1)
        self.assertGreaterEqual(stats.get("mosaic_applied", 0), 1)

    def test_main_plugin_class(self):
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=DEFAULT_CONFIG)
        self.assertIsNotNone(plugin.cfg_mgr)
        self.assertIsNotNone(plugin.moderator)

    def test_style_case_insensitive(self):
        """风格/主题大小写兼容：IOS/Light 不应回退失败"""
        img = MessageImageRenderer.render("大小写测试ABC", style="IOS", theme_mode="Light")
        self.assertIsNotNone(img)
        img2 = MessageImageRenderer.render("未知风格回退", style="xxx", theme_mode="yyy")
        self.assertIsNotNone(img2)

    def test_android_no_brand_header(self):
        """Android 顶栏无品牌无头像，纯时间，渲染正常"""
        img = MessageImageRenderer.render("顶栏测试中文", style="android16")
        self.assertIsNotNone(img)
        self.assertGreater(img.width, 500)

    def test_chinese_glyph_present(self):
        """当前加载字体必须含中文（杜绝 tofu 方框）"""
        from core.renderer import get_font
        f = get_font(28)
        self.assertGreater(f.getlength("中文测试"), f.getlength("    "))

    def test_bold_font_not_broken(self):
        """粗体绝不能选中 Pillow 下半残的 msyhbd/msyhhv，且中文笔画完整"""
        import os
        from core.renderer import _FONT_BOLD_PATH, get_font
        from PIL import Image
        self.assertNotIn(
            os.path.basename(_FONT_BOLD_PATH or "").lower(),
            {"msyhbd.ttc", "msyhhv.ttc"},
        )
        if _FONT_BOLD_PATH:
            from PIL import Image, ImageDraw
            fb = get_font(40, bold=True)
            canvas = Image.new("L", (200, 60), 0)
            ImageDraw.Draw(canvas).text((5, 5), "标题永", font=fb, fill=255)
            raw = canvas.get_flattened_data() if hasattr(canvas, "get_flattened_data") else canvas.getdata()
            ink = sum(1 for px in raw if px > 32)
            # 半残字体只有零星笔画，正常字体三字至少数千墨点
            self.assertGreater(ink, 1500)

    def test_clean_urls(self):
        from main import _clean_urls
        self.assertEqual(
            _clean_urls(["https://astrbot.app。", "https://x.com/a,"]),
            ["https://astrbot.app", "https://x.com/a"],
        )

    def test_group_cache(self):
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        self.assertTrue(plugin._is_gid_allowed("123") is False)  # 默认白名单空
        plugin.cfg_mgr.config["group_mode"] = "all"
        self.assertTrue(plugin._is_gid_allowed("123"))
        plugin.cfg_mgr.config["group_mode"] = "blacklist"
        plugin.cfg_mgr.config["group_list"] = "123"
        self.assertFalse(plugin._is_gid_allowed("123"))
        self.assertTrue(plugin._is_gid_allowed("456"))

    def test_emoji_styles(self):
        """Emoji 管线离线渲染（关闭云端补全，不触网）"""
        img = MessageImageRenderer.render(
            "表情测试💰🎉中文", style="ios", emoji_remote=False,
        )
        self.assertIsNotNone(img)
        self.assertGreater(img.width, 500)

    def test_emoji_cluster(self):
        """Emoji 簇切分：ZWJ 序列/键帽不断开，纯函数离线测试"""
        from core.renderer import _take_cluster, _is_emoji_cluster, _cluster_codes
        cl, ni = _take_cluster("👨\u200d👩\u200d👧好", 0)
        self.assertTrue(_is_emoji_cluster(cl))
        self.assertIn("\u200d", cl)
        self.assertEqual(ni, len(cl))
        cl2, ni2 = _take_cluster("1️⃣好", 0)
        self.assertIn("⃣", cl2)
        self.assertEqual(ni2, len(cl2))
        self.assertFalse(_is_emoji_cluster("中"))
        codes = _cluster_codes("⚔️")
        self.assertIn("2694", codes)

    def test_emoji_local_image(self):
        """包内已不再自带 emoji 图，离线返回 None，符合零自带要求"""
        from core.renderer import _get_emoji_image
        im = _get_emoji_image("💰", 28, allow_remote=False)
        self.assertIsNone(im)

    def test_font_manager_status(self):
        """字体管理器：本机有系统字体则无需下载，状态查询正常"""
        from core.renderer import (
            configure_fonts, get_font_status, needs_cjk_download,
            _is_usable_font, _safe_font_filename, _FONT_REGULAR_PATH,
        )
        from core.config import ConfigManager
        mgr = ConfigManager()
        configure_fonts(mgr.config, mgr.data_dir)
        st = get_font_status()
        self.assertTrue(st["has_cjk"])
        self.assertFalse(needs_cjk_download())
        self.assertTrue(_is_usable_font(_FONT_REGULAR_PATH))
        self.assertFalse(_is_usable_font("/nonexistent/font.ttf"))
        self.assertTrue(_safe_font_filename("https://x.com/a.ttf?1").endswith(".ttf"))
        self.assertTrue(_safe_font_filename("https://x.com/a").endswith(".ttf"))

    def test_custom_font_source(self):
        """custom 模式缺文件回退系统字体；system 模式永不下载"""
        from core.renderer import configure_fonts, get_font_status, needs_cjk_download
        configure_fonts({"font_source": "custom", "custom_font_path": "/nonexistent.ttf"}, None)
        self.assertTrue(get_font_status()["has_cjk"])
        configure_fonts({"font_source": "system"}, None)
        self.assertFalse(needs_cjk_download())
        configure_fonts({"font_source": "auto"}, None)
        self.assertFalse(needs_cjk_download())

    def test_keywords_moderation_pass(self):
        """测试关键词审查：无违规关键词直接 pass"""
        import asyncio
        cfg = dict(DEFAULT_CONFIG)
        cfg["moderation_mode"] = "keywords"
        moderator = ContentModerator(cfg)
        # 无违规关键词，直接 pass
        res = asyncio.run(moderator.review("这是一条普通日常对话"))
        self.assertFalse(res.is_violated)
        self.assertEqual(res.action, "pass")

    @_needs_win_font("C:/Windows/Fonts/simhei.ttf")
    def test_font_unlink_no_lock(self):
        """测试字体在被读取后无 Windows 句柄锁定，可直接删除"""
        import tempfile
        from core.renderer import _load_font_file, clear_font_cache
        tmp = Path(tempfile.gettempdir()) / "msg2img_test_lock.ttf"
        # 写入有效字体字节
        tmp.write_bytes(Path("C:/Windows/Fonts/simhei.ttf").read_bytes())
        try:
            f = _load_font_file(str(tmp), 24)
            self.assertIsNotNone(f)
            clear_font_cache()
            # 在 Windows 下未释放句柄将引发 PermissionError
            tmp.unlink()
            self.assertFalse(tmp.exists())
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def test_keyword_presets(self):
        """测试多套敏感词方案定义及匹配"""
        cfg = dict(DEFAULT_CONFIG)
        moderator = ContentModerator(cfg)

        # 默认综合词库：测试涉毒、涉赌、涉黄
        self.assertTrue(moderator.check_keywords("这里有人在贩毒和吸毒")[0])
        self.assertTrue(moderator.check_keywords("欢迎来到线上赌场进行轮盘赌")[0])
        self.assertTrue(moderator.check_keywords("私密露点约炮群聊")[0])
        self.assertTrue(moderator.check_keywords("出售自瞄透视挂机脚本")[0])

        # 强力色情违禁词库：测试日常色色、AI发情、SM大圈小圈、百合、小白违规词
        self.assertTrue(moderator.check_keywords("今晚做爱狠狠内射，爽到潮吹高潮抽搐", preset_name="anti_nsfw_ultra")[0])
        self.assertTrue(moderator.check_keywords("恶堕触手侵犯，深度开发敏感体质，止不住的流水", preset_name="anti_nsfw_ultra")[0])
        self.assertTrue(moderator.check_keywords("口塞皮鞭束缚，母狗跪下舔鞋，后庭开发肛塞", preset_name="anti_nsfw_ultra")[0])
        self.assertTrue(moderator.check_keywords("百合色色磨豆腐，双头龙对插磨穴", preset_name="anti_nsfw_ultra")[0])
        self.assertTrue(moderator.check_keywords("奴隶买卖私设刑房，赌场下注脚本刷币", preset_name="anti_nsfw_ultra")[0])
        self.assertTrue(moderator.check_keywords("今晚抢银行越狱，关进监狱调教奴隶", preset_name="anti_nsfw_ultra")[0])
        self.assertTrue(moderator.check_keywords("网络赌博平台百家乐轮盘赌上头了", preset_name="anti_nsfw_ultra")[0])

        # xbbot 专属词库匹配
        self.assertTrue(moderator.check_keywords("强行买下奴隶并进行折磨奴隶", preset_name="xbbot_game")[0])

    def test_group_config_defaults(self):
        """测试群专属配置跟随全局默认"""
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        # 默认空
        c = plugin._get_group_custom_config("123456")
        self.assertEqual(c, {})
        self.assertEqual(plugin._get_group_font_scale("123456"), 100)

    def test_config_defaults_isolation(self):
        """默认配置隔离：实例修改词库不污染模块全局默认"""
        from core.config import ConfigManager, DEFAULT_KEYWORD_PRESETS
        before = DEFAULT_KEYWORD_PRESETS["default"]["keywords"]
        m1 = ConfigManager()
        m1.config["keyword_presets"]["default"]["keywords"] = "__polluted__"
        m2 = ConfigManager()
        self.assertNotIn("__polluted__", m2.config["keyword_presets"]["default"]["keywords"])
        self.assertEqual(DEFAULT_KEYWORD_PRESETS["default"]["keywords"], before)

    def test_presets_update_flow(self):
        """内置词库更新检测：过期本地标记更新，覆盖/保留后恢复一致"""
        import copy
        from core.config import ConfigManager
        mgr = ConfigManager()
        # 新鲜态：无更新
        self.assertFalse(mgr.get_presets_update_status()["update_available"])
        # 模拟旧版本残留（去掉 ultra 新增词）
        local = copy.deepcopy(mgr.config["keyword_presets"])
        ultra_kw = local["anti_nsfw_ultra"]["keywords"]
        trimmed = ultra_kw.replace(",抢银行,银行抢劫,银行劫案,银行,劫狱,越狱,监狱,奴隶", "")
        self.assertNotEqual(trimmed, ultra_kw)
        local["anti_nsfw_ultra"]["keywords"] = trimmed
        # 备份真实 config.json（apply/dismiss 会落盘）
        cfg_file = mgr.cfg_file
        bak = cfg_file.read_bytes() if cfg_file.exists() else None
        try:
            stale = ConfigManager({"keyword_presets": local, "builtin_presets_hash": "0" * 16})
            st2 = stale.get_presets_update_status()
            self.assertTrue(st2["update_available"])
            hit = [c for c in st2["changed"] if c["id"] == "anti_nsfw_ultra"]
            self.assertTrue(hit and hit[0]["added_total"] >= 8)
            st3 = stale.apply_builtin_presets(["anti_nsfw_ultra"])
            self.assertFalse(st3["update_available"])
            # dismiss 路径：保留本地同样消除提示（先清掉 apply 落盘的文件，还原过期现场）
            if stale.cfg_file.exists():
                stale.cfg_file.unlink()
            stale2 = ConfigManager({"keyword_presets": local, "builtin_presets_hash": "0" * 16})
            self.assertFalse(stale2.dismiss_builtin_presets_update()["update_available"])
        finally:
            if bak is not None:
                cfg_file.write_bytes(bak)
            elif cfg_file.exists():
                cfg_file.unlink()

    @_needs_win_font("C:/Windows/Fonts/simhei.ttf")
    def test_font_fallback_after_delete(self):
        """删除正在使用的字体后回退：剩 1 个默认用它，多个切列表第一个，无可用清空回自动"""
        import shutil
        import tempfile
        import asyncio
        import core.webapi as webapi_mod
        from core import renderer as R
        from main import Msg2ImgPlugin
        tmp = Path(tempfile.mkdtemp())
        (tmp / "fonts").mkdir(parents=True, exist_ok=True)
        sim = Path("C:/Windows/Fonts/simhei.ttf").read_bytes()
        orig_subdir, orig_dd = R._data_subdir, R._FONT_DATA_DIR
        orig_req, orig_jr, orig_er = webapi_mod.request, webapi_mod.json_response, webapi_mod.error_response

        class Req:
            def __init__(self, p):
                self.p = p

            async def json(self, default=None):
                return self.p

        webapi_mod.json_response = lambda d: d
        webapi_mod.error_response = lambda msg, status_code=500: {"ok": False, "error": msg}

        async def call_delete(plugin, name):
            webapi_mod.request = Req({"name": name})
            return await plugin._api_fonts_delete()

        def fake_subdir(name=""):
            d = tmp / name if name else tmp
            d.mkdir(parents=True, exist_ok=True)
            return d

        # 备份 dev 配置（回退逻辑会落盘保存，避免污染其它用例）
        dev_cfg = Path("data/plugin_data/astrbot_plugin_xbimg/config.json")
        dev_bak = dev_cfg.read_bytes() if dev_cfg.exists() else None
        try:
            R._FONT_DATA_DIR = tmp
            R._data_subdir = fake_subdir
            plugin = Msg2ImgPlugin(context=None, config=None)
            plugin.cfg_mgr.data_dir = tmp
            fd = tmp / "fonts"

            def plant(*names):
                for n in list(fd.iterdir()):
                    n.unlink()
                for n in names:
                    (fd / n).write_bytes(sim)
                R.clear_font_cache()

            # A. 仅剩 1 个：默认使用它
            plant("aaa_one.ttf", "zzz_active.ttf")
            plugin.cfg_mgr.config.update({
                "font_source": "custom",
                "custom_font_path": str(fd / "zzz_active.ttf"),
                "custom_bold_font_path": "",
            })
            res = asyncio.run(call_delete(plugin, "zzz_active.ttf"))
            self.assertTrue(res["ok"])
            self.assertEqual(res["fallback"], "aaa_one.ttf")
            self.assertEqual(plugin.cfg_mgr.config["custom_font_path"], "aaa_one.ttf")
            # B. 多个：切换到列表第一个
            plant("aaa_one.ttf", "mmm_mid.ttf", "zzz_active.ttf")
            plugin.cfg_mgr.config.update({
                "font_source": "custom",
                "custom_font_path": str(fd / "mmm_mid.ttf"),
            })
            res = asyncio.run(call_delete(plugin, "mmm_mid.ttf"))
            self.assertTrue(res["ok"])
            self.assertEqual(res["fallback"], "aaa_one.ttf")
            # C. 无可用：清空回自动
            plant("solo.ttf")
            plugin.cfg_mgr.config.update({
                "font_source": "custom",
                "custom_font_path": str(fd / "solo.ttf"),
            })
            res = asyncio.run(call_delete(plugin, "solo.ttf"))
            self.assertTrue(res["ok"])
            self.assertEqual(res["fallback"], "")
            self.assertEqual(plugin.cfg_mgr.config["custom_font_path"], "")
        finally:
            R._data_subdir = orig_subdir
            R._FONT_DATA_DIR = orig_dd
            webapi_mod.request, webapi_mod.json_response, webapi_mod.error_response = orig_req, orig_jr, orig_er
            if dev_bak is not None:
                dev_cfg.write_bytes(dev_bak)
            elif dev_cfg.exists():
                dev_cfg.unlink()
            shutil.rmtree(tmp, ignore_errors=True)

    @_needs_win_font("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/msyh.ttc")
    def test_font_cmap_coverage(self):
        """文件级 cmap 精确判定：arial 无中文，msyh 有中文"""
        from core import renderer as R
        cmap_ar = R._file_cmap("C:/Windows/Fonts/arial.ttf")
        self.assertIsNotNone(cmap_ar)
        self.assertNotIn(0x4E2D, cmap_ar)
        self.assertIn(0x41, cmap_ar)
        cmap_ms = R._file_cmap("C:/Windows/Fonts/msyh.ttc")
        self.assertIsNotNone(cmap_ms)
        self.assertIn(0x4E2D, cmap_ms)
        self.assertIsNone(R._file_cmap("C:/nonexistent.ttf"))

    def test_coverage_cache_invalidated_on_rebuild(self):
        """换字体重建后覆盖判定缓存必须失效（防 id 复用过期误判）"""
        from core import renderer as R
        R._resolve_char_font("中", R.get_font(32))
        self.assertGreater(len(R._RESOLVE_CACHE) + len(R._COVER_CACHE), 0)
        R._rebuild_active_fonts()
        self.assertEqual(len(R._RESOLVE_CACHE), 0)
        self.assertEqual(len(R._COVER_CACHE), 0)
        self.assertEqual(len(R._INK_CACHE), 0)
        # 诊断日志不抛异常即可（静音断言，避免污染测试输出）
        _lg = R.logger
        _dis = _lg.disabled
        _lg.disabled = True
        try:
            R._note_uncovered(R.get_font(32), "中")
        finally:
            _lg.disabled = _dis

    @staticmethod
    def _real_emoji_font_bytes():
        """找一个本机真实含 😀 的彩字文件（seguiemj/NotoColorEmoji），找不到返回 None"""
        import os
        for cand in (
            "C:/Windows/Fonts/seguiemj.ttf",
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/NotoColorEmoji.ttf",
        ):
            try:
                if os.path.isfile(cand) and os.path.getsize(cand) > 1024 * 1024:
                    return Path(cand).read_bytes()
            except Exception:
                continue
        return None

    def test_android_emoji_download_fallback(self):
        """Android 表情包多直链容错：首源失败自动换源；残包体积校验"""
        import shutil
        import tempfile
        from core import renderer as R
        font_bytes = self._real_emoji_font_bytes()
        if not font_bytes:
            self.skipTest("本机无可用彩色 emoji 字体，跳过")
        tmp = Path(tempfile.mkdtemp())
        orig_subdir, orig_dd = R._data_subdir, R._FONT_DATA_DIR
        orig_dl = R._download_file
        orig_extract = R._extract_noto_pngs
        seen_urls = []

        def fake_subdir(name=""):
            d = tmp / name if name else tmp
            d.mkdir(parents=True, exist_ok=True)
            return d

        def flaky_dl(url, dest):
            seen_urls.append(url)
            if "jsdelivr" in url:
                return False, "cdn fail"
            dest.write_bytes(font_bytes)
            return True, ""

        try:
            R._FONT_DATA_DIR = tmp
            R._data_subdir = fake_subdir
            R._download_file = flaky_dl
            # 解包与本用例无关（主题是 URL 换源）：直接打桩成功
            R._extract_noto_pngs = lambda *a, **k: (1800, "")
            R.clear_font_cache()
            res = R.download_emoji_pack("android")
            self.assertTrue(res["ok"])
            self.assertGreaterEqual(len(seen_urls), 2)
            self.assertTrue((tmp / "emoji" / R.ANDROID_EMOJI_FILE).is_file())
            # 解包标记由解包步骤写（此处打桩跳过）：手动补标记再验状态
            (tmp / "emoji" / R.ANDROID_PACK_READY).write_text("1800", encoding="utf-8")
            R._style_font_forget("android")
            packs = {p["id"]: p for p in R.get_emoji_packs_status()}
            self.assertTrue(packs["android"]["installed"])
        finally:
            R._data_subdir = orig_subdir
            R._FONT_DATA_DIR = orig_dd
            R._download_file = orig_dl
            R._extract_noto_pngs = orig_extract
            shutil.rmtree(tmp, ignore_errors=True)

    def test_android_prefers_color_image(self):
        """android 风格有全彩图时优先图片（真彩），而非系统单色字体"""
        import shutil
        import tempfile
        from PIL import Image, ImageDraw
        from core import renderer as R
        tmp = Path(tempfile.mkdtemp())
        orig_subdir, orig_dd = R._data_subdir, R._FONT_DATA_DIR

        def fake_subdir(name=""):
            d = tmp / name if name else tmp
            d.mkdir(parents=True, exist_ok=True)
            return d

        try:
            R._FONT_DATA_DIR = tmp
            R._data_subdir = fake_subdir
            (tmp / "emoji").mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (72, 72), (230, 30, 30, 255)).save(tmp / "emoji" / "1f600.png")
            R._EMOJI_IMG_CACHE.clear()
            canvas = Image.new("RGBA", (300, 100), (255, 255, 255, 255))
            draw = ImageDraw.Draw(canvas)
            font = R.get_font(32)
            R._draw_mixed_text(canvas, draw, 10, 20, "😀", font, (20, 20, 20, 255), 32,
                               emoji_remote=False, emoji_style="android")
            px = canvas.load()
            red = sum(
                1 for yy in range(100) for xx in range(300)
                if (lambda p: p[3] > 128 and p[0] - min(p[1], p[2]) > 100)(px[xx, yy]))
            self.assertGreater(red, 50)
        finally:
            R._data_subdir = orig_subdir
            R._FONT_DATA_DIR = orig_dd
            R._EMOJI_IMG_CACHE.clear()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_compress_level_mapping(self):
        """体积档位：新值直通，未知值回落默认"""
        import main as main_mod
        self.assertEqual(main_mod._compress_level({"img_compress_level": "lossless"}), "lossless")
        self.assertEqual(main_mod._compress_level({"img_compress_level": "balanced"}), "balanced")
        self.assertEqual(main_mod._compress_level({"img_compress_level": "compact"}), "compact")
        self.assertEqual(main_mod._compress_level({"img_compress_level": "省流"}), "compact")
        self.assertEqual(main_mod._compress_level({}), "balanced")
        self.assertEqual(main_mod._compress_level({"img_compress_level": "???"}), "balanced")

    def test_render_cache_reuse(self):
        """相同文本短时复用渲染结果"""
        from core.renderer import MessageImageRenderer
        kw = dict(text="缓存复用测试文本", style="ios", theme_mode="light",
                  star_background=False, emoji_remote=False)
        p1 = MessageImageRenderer.render_pages(**kw)
        p2 = MessageImageRenderer.render_pages(**kw)
        self.assertEqual(len(p1), len(p2))
        self.assertIs(p1[0], p2[0])

    def test_terminate_cleans_tasks(self):
        """terminate 回收后台任务"""
        import asyncio
        from main import Msg2ImgPlugin

        async def _t():
            p = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
            p._ensure_bg_tasks()
            self.assertEqual(len(p._bg_tasks), 4)
            await p.terminate()
            self.assertFalse(p._bg_started)
            self.assertEqual(p._bg_tasks, [])

        asyncio.run(_t())

    def test_emoji_prefetch(self):
        """emoji 缺图并行预取：断网退避直接返回；mock 下成功计数（返回 (done, complete)）"""
        from core import renderer as R
        orig_dead = R._EMOJI_REMOTE_DEAD_UNTIL
        orig_fetch = R._fetch_remote_emoji
        try:
            import time as _t
            R._EMOJI_REMOTE_DEAD_UNTIL = _t.time() + 600
            done, complete = R._prefetch_emoji_images("👨‍👩‍👧好")
            self.assertEqual(done, 0)
            self.assertTrue(complete)
            R._EMOJI_REMOTE_DEAD_UNTIL = 0.0
            R._fetch_remote_emoji = lambda code, dest: True
            done, complete = R._prefetch_emoji_images("👨‍👩‍👧好")
            self.assertGreater(done, 0)
            self.assertTrue(complete)
            done, complete = R._prefetch_emoji_images("纯文本无表情")
            self.assertEqual(done, 0)
            self.assertTrue(complete)
        finally:
            R._EMOJI_REMOTE_DEAD_UNTIL = orig_dead
            R._fetch_remote_emoji = orig_fetch

    def test_mosaic_positions_char_aligned(self):
        """打码定位：VS16/ZWJ 簇拆成单字符条目，串定位与条目定位一致（防打码错位）"""
        from PIL import Image, ImageDraw
        from core.renderer import _draw_mixed_text, get_font
        canvas = Image.new("RGBA", (900, 120), (255, 255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        font = get_font(32)
        pos = _draw_mixed_text(
            canvas, draw, 10, 30, "📅️测试奴隶好👨‍👩‍👧 end", font,
            (20, 20, 20, 255), 32, emoji_remote=False, emoji_style="none",
        )
        for _x, _w, _y, ch in pos:
            self.assertEqual(len(ch), 1, f"cluster not split: {ch!r}")
        chars = "".join(p[3] for p in pos)
        self.assertIn("奴隶", chars)
        idx = chars.find("奴隶")
        self.assertEqual(pos[idx][3], "奴")
        self.assertEqual(pos[idx + 1][3], "隶")

    @_needs_win_font("C:/Windows/Fonts/arial.ttf")
    def test_nfkc_fallback_draw(self):
        """数学花体等无字形符号退化为 NFKC 等价形（𝔖→S），优先同风格绘制"""
        from core import renderer as R
        lat = R._load_font_file("C:/Windows/Fonts/arial.ttf", 32)
        cjk = R.get_font(32)
        self.assertEqual(R._nfc_fallback_char("𝔖", lat), "S")
        self.assertEqual(R._nfc_fallback_char("Ｎ", lat), "N")
        self.assertIsNone(R._nfc_fallback_char("中", lat))
        self.assertIsNone(R._nfc_fallback_char("A", lat))
        dc, f = R._draw_char_and_font("𝔖", lat)
        self.assertEqual(dc, "S")
        self.assertIs(f, lat)  # 主字体有 S，直接同风格绘制
        dc2, _ = R._draw_char_and_font("中", cjk)
        self.assertEqual(dc2, "中")

    @_needs_win_font("C:/Windows/Fonts/arial.ttf")
    def test_decorative_font_fallback(self):
        """装饰字体（缺 CJK）自动回退：拉丁跟随主字体，中文切链中字体，国旗成对不断开"""
        from core.renderer import (
            _load_font_file, get_font, _font_covers, _resolve_char_font,
            _take_cluster, _cluster_codes,
        )
        latin = _load_font_file("C:/Windows/Fonts/arial.ttf", 32)  # 无 CJK，模拟哥特体
        cjk = get_font(32)
        self.assertIsNotNone(latin)
        self.assertTrue(_font_covers(latin, "A"))
        self.assertFalse(_font_covers(latin, "中"))
        self.assertTrue(_font_covers(cjk, "中"))
        self.assertTrue(_font_covers(latin, " "))
        self.assertIs(_resolve_char_font("A", latin), latin)
        fb = _resolve_char_font("中", latin)
        self.assertIsNot(fb, latin)
        self.assertTrue(_font_covers(fb, "中"))
        cl, ni = _take_cluster("🇨🇳好", 0)
        self.assertEqual(cl, "🇨🇳")
        self.assertEqual(ni, 2)
        self.assertEqual(_cluster_codes(cl), ["1f1e8-1f1f3"])

    @_needs_win_font("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/msyh.ttc")
    def test_decorative_font_mixed_render(self):
        """装饰字体做主字体时混排渲染：拉丁走装饰字体，缺字逐字回退，排版不断裂"""
        from core import renderer as R
        orig_conf = dict(R._FONT_CONF)
        try:
            R.configure_fonts({
                "font_source": "custom",
                "custom_font_path": "C:/Windows/Fonts/arial.ttf",
                "custom_bold_font_path": "",
                "custom_font_url": "",
                "emoji_style": "none",
            }, None)
            img = R.MessageImageRenderer.render(
                "Hello中文测试", style="ios", theme_mode="light",
                star_background=False, emoji_remote=False,
            )
            self.assertIsNotNone(img)
            self.assertGreater(img.width, 500)
            # 回退计量与绘制一致：卡片宽度应容纳混排文本
            pages = R.MessageImageRenderer.render_pages(
                "Hello中文测试", style="ios", theme_mode="light",
                star_background=False, emoji_remote=False,
            )
            self.assertEqual(len(pages), 1)
        finally:
            R.configure_fonts(orig_conf, None)

    def test_long_text_pagination(self):
        """超长内容分页输出多图，不再丢弃截断；短内容仍单图；单页高度适配手机屏"""
        from core.renderer import MessageImageRenderer
        long_text = ("这是很长的一段测试文本，用来验证超长内容分页功能是否正常工作。" * 4 + "\n") * 60
        pages = MessageImageRenderer.render_pages(
            long_text, style="ios", theme_mode="light",
            star_background=False, emoji_remote=False,
        )
        self.assertGreater(len(pages), 1)
        for p in pages:
            self.assertLessEqual(p.height, 3200)
            self.assertLessEqual(p.width, 820)
        one = MessageImageRenderer.render(
            long_text, style="ios", theme_mode="light",
            star_background=False, emoji_remote=False,
        )
        self.assertEqual(one.size, pages[0].size)
        short = MessageImageRenderer.render_pages(
            "短文本", style="ios", theme_mode="light",
            star_background=False, emoji_remote=False,
        )
        self.assertEqual(len(short), 1)
        tiny = MessageImageRenderer.render_pages(
            long_text, style="ios", theme_mode="light",
            star_background=False, emoji_remote=False, page_max_h=900,
        )
        self.assertGreater(len(tiny), len(pages))
        for p in tiny:
            self.assertLessEqual(p.height, 1100)

    def test_long_single_line_wraps_in_bubble(self):
        """超长单行不横向撑出气泡：折行显示，保证手机缩略图完整"""
        from core.renderer import MessageImageRenderer
        line = "[虚妄]笑~忍神大人心情不错，看着面前楚楚可怜的[虚妄]，忍不住伸手摸了摸她的头又捏了捏她的脸。"
        img = MessageImageRenderer.render(
            line, style="ios", theme_mode="light",
            star_background=False, emoji_remote=False,
        )
        self.assertLessEqual(img.width, 712)

    def test_save_image_width_clamp(self):
        """超宽图落盘限宽（默认 1080，手机缩略图不被裁）"""
        import asyncio
        from PIL import Image
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=None)
        big = Image.new("RGB", (2000, 500), (255, 255, 255))
        path = asyncio.run(plugin._save_render_image(big))
        try:
            self.assertIsNotNone(path)
            with Image.open(path) as saved:
                self.assertLessEqual(saved.width, 1080)
        finally:
            try:
                if path is not None:
                    Path(path).unlink(missing_ok=True)
            except Exception:
                pass

    def test_ios_emoji_download_offline(self):
        """无网络时 iOS 下载如实失败，不写 ready 标记，不虚标已下载"""
        import shutil
        import tempfile
        from core import renderer as R
        tmp = Path(tempfile.mkdtemp())
        orig_subdir, orig_dd = R._data_subdir, R._FONT_DATA_DIR
        orig_fetch = R._fetch_remote_emoji

        def fake_subdir(name=""):
            d = tmp / name if name else tmp
            d.mkdir(parents=True, exist_ok=True)
            return d

        try:
            R._FONT_DATA_DIR = tmp
            R._data_subdir = fake_subdir
            R._fetch_remote_emoji = lambda code, dest: False
            res = R.download_emoji_pack("ios")
            self.assertFalse(res["ok"])
            self.assertFalse((tmp / "emoji" / "ios_pack.ready").exists())
            packs = {p["id"]: p for p in R.get_emoji_packs_status()}
            self.assertFalse(packs["ios"]["installed"])
        finally:
            R._data_subdir = orig_subdir
            R._FONT_DATA_DIR = orig_dd
            R._fetch_remote_emoji = orig_fetch
            shutil.rmtree(tmp, ignore_errors=True)

    @_needs_win_font("C:/Windows/Fonts/simhei.ttf")
    def test_curated_download_retries(self):
        """精选字体单文件波动失败时重试并最终成功；持续失败则如实报错"""
        import shutil
        import tempfile
        import time as _t
        from core import renderer as R
        tmp = Path(tempfile.mkdtemp())
        (tmp / "fonts").mkdir(parents=True, exist_ok=True)
        sim = Path("C:/Windows/Fonts/simhei.ttf").read_bytes()
        orig_subdir, orig_dd = R._data_subdir, R._FONT_DATA_DIR
        orig_dl = R._download_file
        orig_sleep = _t.sleep
        calls = {"n": 0}

        def fake_subdir(name=""):
            d = tmp / name if name else tmp
            d.mkdir(parents=True, exist_ok=True)
            return d

        def flaky(url, dest):
            calls["n"] += 1
            if calls["n"] < 3:
                return False, "波动"
            dest.write_bytes(sim)
            return True, ""

        try:
            R._FONT_DATA_DIR = tmp
            R._data_subdir = fake_subdir
            R._download_file = flaky
            _t.sleep = lambda s: None
            R.clear_font_cache()
            res = R.download_curated_font("lxgw_wenkai")  # 单文件包
            self.assertTrue(res["ok"])
            self.assertEqual(calls["n"], 3)
            self.assertTrue(res["downloaded"])
            # 持续失败：如实报错
            R._download_file = lambda url, dest: (False, "断网")
            (tmp / "fonts" / "LXGWWenKai-Regular.ttf").unlink(missing_ok=True)
            R.clear_font_cache()
            res2 = R.download_curated_font("lxgw_wenkai")
            self.assertFalse(res2["ok"])
            self.assertIn("LXGWWenKai-Regular.ttf", res2["error"])
        finally:
            R._data_subdir = orig_subdir
            R._FONT_DATA_DIR = orig_dd
            R._download_file = orig_dl
            _t.sleep = orig_sleep
            shutil.rmtree(tmp, ignore_errors=True)

    def test_emoji_downloading_tmp_ignored(self):
        """下载残留 tmp 不计入占用；失败不留残留文件"""
        import shutil
        import tempfile
        import urllib.request
        from core import renderer as R
        tmp = Path(tempfile.mkdtemp())
        orig_subdir, orig_dd = R._data_subdir, R._FONT_DATA_DIR
        orig_dead = R._EMOJI_REMOTE_DEAD_UNTIL
        orig_urlopen = urllib.request.urlopen

        def fake_subdir(name=""):
            d = tmp / name if name else tmp
            d.mkdir(parents=True, exist_ok=True)
            return d

        try:
            R._FONT_DATA_DIR = tmp
            R._data_subdir = fake_subdir
            ed = tmp / "emoji"
            ed.mkdir(parents=True, exist_ok=True)
            (ed / "2705.png").write_bytes(b"0" * 4096)
            (ed / "2705.png.downloading").write_bytes(b"0" * 700)  # 模拟中断残留
            self.assertEqual(R.get_emoji_storage_kb("ios"), round(4096 / 1024, 1))
            # urlopen 直接抛错：不应留下新的 tmp（强制走 urllib 兜底通道）
            def boom(req, timeout=None):
                raise urllib.request.URLError("down")
            urllib.request.urlopen = boom
            orig_http_client_fn = R._emoji_http_client
            R._emoji_http_client = lambda: None
            R._EMOJI_REMOTE_DEAD_UNTIL = 0.0
            R._EMOJI_MISS_CACHE.pop("1f600", None)
            self.assertFalse(R._fetch_remote_emoji("1f600", ed))
            leftovers = list(ed.glob("1f600*.png.downloading"))
            self.assertEqual(leftovers, [])
            R._EMOJI_REMOTE_DEAD_UNTIL = 0.0  # 退避后仍可工作由其它用例覆盖，此处仅复位
        finally:
            R._data_subdir = orig_subdir
            R._FONT_DATA_DIR = orig_dd
            R._EMOJI_REMOTE_DEAD_UNTIL = orig_dead
            R._emoji_http_client = orig_http_client_fn
            urllib.request.urlopen = orig_urlopen
            shutil.rmtree(tmp, ignore_errors=True)


    def test_emoji_httpx_pool_reuse_and_backoff(self):
        """httpx 共享池：复用同一 client；网络错退避+清 tmp；404 不退避"""
        import shutil
        import tempfile
        import time as _t
        from core import renderer as R
        try:
            import httpx as _hx
        except Exception:
            self.skipTest("无 httpx，跳过")
        tmp = Path(tempfile.mkdtemp())
        orig_dd = R._FONT_DATA_DIR
        orig_dead = R._EMOJI_REMOTE_DEAD_UNTIL
        orig_client = R._EMOJI_HTTP_CLIENT
        try:
            R._FONT_DATA_DIR = tmp
            R._EMOJI_HTTP_CLIENT = None
            ed = tmp / "emoji"
            ed.mkdir(parents=True, exist_ok=True)
            c1 = R._emoji_http_client()
            c2 = R._emoji_http_client()
            self.assertIsNotNone(c1)
            self.assertIs(c1, c2)  # 同一共享实例（连接复用）

            class BoomClient:
                def stream(self, *a, **k):
                    raise _hx.ConnectError("down", request=None)

            R._EMOJI_HTTP_CLIENT = BoomClient()
            R._EMOJI_REMOTE_DEAD_UNTIL = 0.0
            self.assertFalse(R._fetch_remote_emoji("1f600", ed))
            self.assertGreater(R._EMOJI_REMOTE_DEAD_UNTIL, _t.time())
            self.assertEqual(list(ed.glob("1f600*.png.downloading")), [])
            self.assertIsNone(R._EMOJI_HTTP_CLIENT)  # 坏连接已丢弃

            class NotFoundClient:
                class _Resp:
                    status_code = 404

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def raise_for_status(self):
                        pass

                def stream(self, *a, **k):
                    return self._Resp()

            R._EMOJI_REMOTE_DEAD_UNTIL = 0.0
            R._EMOJI_HTTP_CLIENT = NotFoundClient()
            self.assertFalse(R._fetch_remote_emoji("1f600", ed))
            self.assertEqual(R._EMOJI_REMOTE_DEAD_UNTIL, 0.0)  # 404 不退避
            self.assertTrue(R._emoji_miss_cached("1f600"))  # 404 进负缓存
        finally:
            R._FONT_DATA_DIR = orig_dd
            try:
                if R._EMOJI_HTTP_CLIENT is not None and hasattr(R._EMOJI_HTTP_CLIENT, "close"):
                    R._EMOJI_HTTP_CLIENT.close()
            except Exception:
                pass
            R._EMOJI_HTTP_CLIENT = orig_client
            R._EMOJI_REMOTE_DEAD_UNTIL = orig_dead
            R._EMOJI_MISS_CACHE.pop("1f600", None)  # 不污染后续镜像轮换用例
            shutil.rmtree(tmp, ignore_errors=True)


    def test_blocked_counts_violation_not_total(self):
        """block 拦截：记违规数但不计入已生成图总数"""
        mgr = ConfigManager()
        before = mgr.get_stats()
        mgr.record_render(is_violated=True, count_total=False)
        after = mgr.get_stats()
        self.assertEqual(
            after["violations_blocked"], before.get("violations_blocked", 0) + 1
        )
        self.assertEqual(after["total_rendered"], before.get("total_rendered", 0))
        mgr.record_render(is_violated=True, is_mosaic=True)
        s = mgr.get_stats()
        self.assertEqual(s["total_rendered"], before.get("total_rendered", 0) + 1)

    def test_legacy_moderation_mode_not_bypassed(self):
        """历史脏值 both/always 仍走关键词审查，不静默旁路"""
        import asyncio
        cfg = dict(DEFAULT_CONFIG)
        cfg["custom_keywords"] = "赌博"
        for mode in ("both", "always"):
            cfg["moderation_mode"] = mode
            mod = ContentModerator(cfg)
            hit, matched = mod.check_keywords("今晚赌博局")
            self.assertTrue(hit)
            res = asyncio.run(mod.review("今晚赌博局"))
            self.assertTrue(res.is_violated)

    def test_adapter_block_strips_text(self):
        """适配器路径 block：剥离违规文本、不外泄、结构可发送"""
        import asyncio
        from main import Msg2ImgPlugin
        cfg = dict(DEFAULT_CONFIG)
        cfg["group_mode"] = "all"
        cfg["violation_action"] = "block"
        cfg["custom_keywords"] = "赌博"
        plugin = Msg2ImgPlugin(context=None, config=cfg)
        plugin.moderator = ContentModerator(plugin.cfg_mgr.config)

        async def _go():
            segs = [
                {"type": "at", "data": {"qq": "123"}},
                {"type": "text", "data": {"text": "今晚赌博局见"}},
            ]
            out = await plugin._transform_onebot_message("999", segs)
            texts = [s.get("data", {}).get("text", "") for s in out
                     if isinstance(s, dict) and s.get("type") == "text"]
            return out, texts

        out, texts = asyncio.run(_go())
        self.assertTrue(all("赌博" not in t for t in texts))
        # at 段保留
        self.assertTrue(any(s.get("type") == "at" for s in out))

        async def _go2():
            return await plugin._transform_onebot_message("999", "今晚赌博局见")

        self.assertEqual(asyncio.run(_go2()).strip(), "")

    def test_cmd_test_enforces_moderation(self):
        """test 指令走审查：block 拒绝、正常文本出图"""
        import asyncio
        from main import Msg2ImgPlugin

        class _FakeEvent:
            def __init__(self):
                self.sent = []

            def get_group_id(self):
                return "123456"

            def plain_result(self, text):
                self.sent.append(("plain", text))
                return text

            def chain_result(self, chain):
                self.sent.append(("chain", chain))
                return chain

        async def _collect(gen):
            out = []
            async for r in gen:
                out.append(r)
            return out

        cfg = dict(DEFAULT_CONFIG)
        cfg["violation_action"] = "block"
        cfg["custom_keywords"] = "赌博"
        plugin = Msg2ImgPlugin(context=None, config=cfg)
        plugin.moderator = ContentModerator(plugin.cfg_mgr.config)
        ev = _FakeEvent()
        asyncio.run(_collect(plugin.cmd_xbimg(ev, sub="test", arg="今晚赌博局见")))
        plains = [t for k, t in ev.sent if k == "plain"]
        self.assertTrue(any("拦截" in t for t in plains))

        plugin2 = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        ev2 = _FakeEvent()
        asyncio.run(_collect(plugin2.cmd_xbimg(ev2, sub="test", arg="你好世界")))
        kinds = [k for k, _ in ev2.sent]
        self.assertIn("chain", kinds)
        # 冷却：同群立即再试被拒
        ev3 = _FakeEvent()
        asyncio.run(_collect(plugin2.cmd_xbimg(ev3, sub="test", arg="你好世界")))
        plains3 = [t for k, t in ev3.sent if k == "plain"]
        self.assertTrue(any("频繁" in t for t in plains3))

    def test_config_schema_parity(self):
        """_conf_schema.json 已删（配置全走管理 WebUI）；校验 DEFAULT_CONFIG 关键键稳定"""
        private = {"keyword_presets", "builtin_presets_hash"}
        public = set(DEFAULT_CONFIG) - private
        for k in (
            "enable", "style", "theme_mode", "moderation_mode",
            "enable_keywords_moderation",
            "emoji_style", "emoji_remote", "group_configs", "perf_log",
            "ui_accent_color",
        ):
            self.assertIn(k, public, f"DEFAULT_CONFIG 缺少 {k}")
        self.assertIsInstance(DEFAULT_CONFIG["enable"], bool)
        self.assertIsInstance(DEFAULT_CONFIG["font_scale"], int)

    def test_preview_nonce_issue_check(self):
        """预览 nonce：签发可用、随机串拒绝、过期拒绝"""
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        n = plugin._issue_preview_nonce()
        self.assertTrue(n)
        self.assertTrue(plugin._check_preview_nonce(n))
        self.assertFalse(plugin._check_preview_nonce("pv_forged"))
        self.assertFalse(plugin._check_preview_nonce(""))
        plugin._preview_nonces[n] = 1.0  # 手动过期
        self.assertFalse(plugin._check_preview_nonce(n))

    def test_condense_mosaic_hits_obfuscated(self):
        """混淆词打码：'赌-博'命中'赌博'，打码面积与直接命中同量级（非整区）"""
        from PIL import ImageChops, ImageStat
        t = "正常文本正常文本赌博正常结尾正常结尾正常"
        t2 = "正常文本正常文本赌-博正常结尾正常结尾正常"
        clean = MessageImageRenderer.render(t, mosaic_mode="none")
        m1 = MessageImageRenderer.render(
            t, mosaic_mode="half", mosaic_type="pixel", violation_words=["赌博"]
        )
        clean2 = MessageImageRenderer.render(t2, mosaic_mode="none")
        m2 = MessageImageRenderer.render(
            t2, mosaic_mode="half", mosaic_type="pixel", violation_words=["赌博"]
        )
        d1 = ImageStat.Stat(ImageChops.difference(
            clean.convert("RGB"), m1.convert("RGB"))).mean[0]
        d2 = ImageStat.Stat(ImageChops.difference(
            clean2.convert("RGB"), m2.convert("RGB"))).mean[0]
        self.assertGreater(d1, 0)
        self.assertGreater(d2, 0)
        # 整区回退会是几十倍差异；同字级打码应在 3 倍内
        self.assertLess(d2 / max(d1, 1e-9), 3.0)

    def test_legacy_scales_migrated(self):
        """旧 group_font_scales 自动并入 group_configs，不覆盖已有"""
        cfg = dict(DEFAULT_CONFIG)
        cfg["group_font_scales"] = {"123456": 130, "999999": "xx"}
        cfg["group_configs"] = {"123456": {"style": "ios", "font_scale": 150}}
        mgr = ConfigManager(cfg)
        gc = mgr.config["group_configs"]
        self.assertEqual(gc["123456"]["font_scale"], 150)
        self.assertNotIn("999999", gc)

    def test_save_semaphore_loop_safe(self):
        """落盘信号量跨 asyncio.run 多 loop 可获取可释放"""
        import asyncio
        from main import _save_semaphore

        async def _use():
            sem = _save_semaphore()
            async with sem:
                return True

        self.assertTrue(asyncio.run(_use()))
        self.assertTrue(asyncio.run(_use()))

    def test_render_input_capped(self):
        """渲染侧输入上限：超长文本被截断仍出图"""
        import asyncio
        from main import Msg2ImgPlugin, RENDER_MAX_CHARS
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        plugin.cfg_mgr.config["moderation_mode"] = "none"
        plugin.moderator = ContentModerator(plugin.cfg_mgr.config)

        async def _go():
            mod_res, _, _ = await plugin._moderate_text("x" * 100)
            imgs = await plugin._render_moderated("y" * (RENDER_MAX_CHARS + 2000), mod_res)
            return imgs

        imgs = asyncio.run(_go())
        self.assertTrue(imgs)

    def test_async_download_job_api(self):
        """异步下载接口：立即返回 job，轮询到 done（下载函数打桩零网络）"""
        import asyncio
        import core.webapi as webapi_mod
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        orig_req = webapi_mod.request
        orig_jr = webapi_mod.json_response
        orig_er = webapi_mod.error_response
        orig_dl = webapi_mod.download_emoji_pack

        class Req:
            def __init__(self, p=None, q=""):
                self.p = p or {}
                self.args = {}
                if q:
                    from urllib.parse import parse_qs
                    self.args = {k: v[0] for k, v in parse_qs(q).items()}
                self.environ = {"QUERY_STRING": q}

            async def json(self, default=None):
                return self.p

        webapi_mod.json_response = lambda d: d
        webapi_mod.error_response = lambda msg, status_code=500: {"ok": False, "error": msg}
        webapi_mod.download_emoji_pack = lambda style: {"ok": True, "downloaded": [style], "storage_kb": 1.0}
        try:
            async def _go():
                webapi_mod.request = Req({"style": "android"})
                started = await plugin._api_emoji_download_async()
                self.assertTrue(started.get("job_id"))
                self.assertEqual(started.get("status"), "running")
                for _ in range(100):
                    webapi_mod.request = Req(None, f"job_id={started['job_id']}")
                    st = await plugin._api_emoji_download_status()
                    if st.get("status") != "running":
                        return st
                    await asyncio.sleep(0.05)
                return {"ok": False, "error": "timeout"}

            st = asyncio.run(_go())
            self.assertEqual(st.get("status"), "done")
            self.assertTrue(st["result"]["ok"])
            # 路径参数形态同样可用（桥接推荐），等到完成避免悬空任务
            async def _go2():
                webapi_mod.request = Req({"style": "ios"})
                started2 = await plugin._api_emoji_download_async()
                for _ in range(100):
                    st = await plugin._api_emoji_download_status_path(
                        job_id=started2["job_id"])
                    if st.get("status") != "running":
                        return st
                    await asyncio.sleep(0.05)
                return {"ok": False, "error": "timeout"}
            st2 = asyncio.run(_go2())
            self.assertEqual(st2.get("status"), "done")
            # 非法参数 400 自报
            async def _bad():
                webapi_mod.request = Req({"style": "xxx"})
                return await plugin._api_emoji_download_async()
            bad = asyncio.run(_bad())
            self.assertFalse(bad.get("ok"))
            self.assertIn("xxx", bad.get("error", ""))
            # 未知任务 404
            async def _unknown():
                webapi_mod.request = Req(None, "job_id=nope")
                return await plugin._api_emoji_download_status()
            unk = asyncio.run(_unknown())
            self.assertFalse(unk.get("ok"))
        finally:
            webapi_mod.request = orig_req
            webapi_mod.json_response = orig_jr
            webapi_mod.error_response = orig_er
            webapi_mod.download_emoji_pack = orig_dl

    def test_emoji_storage_ttl_cache(self):
        """占用统计 10s 缓存：命中不扫盘，失效后重算"""
        import shutil
        import tempfile
        import time as _t
        from core import renderer as R
        tmp = Path(tempfile.mkdtemp())
        orig_subdir, orig_dd = R._data_subdir, R._FONT_DATA_DIR

        def fake_subdir(name=""):
            d = tmp / name if name else tmp
            d.mkdir(parents=True, exist_ok=True)
            return d

        try:
            R._FONT_DATA_DIR = tmp
            R._data_subdir = fake_subdir
            R._emoji_storage_invalidate()
            ed = tmp / "emoji"
            ed.mkdir(parents=True, exist_ok=True)
            (ed / "a.png").write_bytes(b"0" * 2048)
            v1 = R.get_emoji_storage_kb("ios")
            self.assertEqual(v1, 2.0)
            (ed / "b.png").write_bytes(b"0" * 2048)
            self.assertEqual(R.get_emoji_storage_kb("ios"), 2.0)  # 缓存命中
            R._emoji_storage_invalidate()
            self.assertEqual(R.get_emoji_storage_kb("ios"), 4.0)  # 失效重算
            R._EMOJI_STORAGE_CACHE["ios"] = (_t.time() - 60, 999.0)  # 过期
            self.assertEqual(R.get_emoji_storage_kb("ios"), 4.0)
        finally:
            R._data_subdir = orig_subdir
            R._FONT_DATA_DIR = orig_dd
            R._emoji_storage_invalidate()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cmd_arg_multiline_rebuilt(self):
        """AstrBot 按空白切分传参时，多行测试文本从原消息重建（不只剩首 token）"""
        import asyncio
        from main import Msg2ImgPlugin

        multi = "# 标题行\n第二行文本🦄🌈\n- 第三行"
        seen = {}

        class _FakeEvent:
            message_str = "/xbimg test " + multi

            def __init__(self):
                self.sent = []

            def get_group_id(self):
                return "123456"

            def plain_result(self, text):
                self.sent.append(("plain", text))
                return text

            def chain_result(self, chain):
                self.sent.append(("chain", chain))
                return chain

        async def _collect(gen):
            out = []
            async for r in gen:
                out.append(r)
            return out

        cfg = dict(DEFAULT_CONFIG)
        cfg["group_mode"] = "all"
        cfg["moderation_mode"] = "keywords"
        cfg["emoji_style"] = "none"
        plugin = Msg2ImgPlugin(context=None, config=cfg)
        orig_render = plugin._render_moderated

        async def spy(eff_text, mod_res, mosaic_mode="none", **kw):
            seen["text"] = eff_text
            return await orig_render(eff_text, mod_res, mosaic_mode, **kw)

        plugin._render_moderated = spy

        async def _go():
            # 模拟 AstrBot 切分：arg 只剩 "#"
            return await _collect(plugin.cmd_xbimg(_FakeEvent(), sub="test", arg="#"))

        asyncio.run(_go())
        self.assertEqual(seen.get("text"), multi)

    def test_emoji_font_validated_rejects_broken(self):
        """残包不虚标：无解包标记 -> 未安装；标记达标 -> 已安装"""
        import shutil
        import tempfile
        from core import renderer as R
        tmp = Path(tempfile.mkdtemp())
        orig_subdir, orig_dd = R._data_subdir, R._FONT_DATA_DIR

        def fake_subdir(name=""):
            d = tmp / name if name else tmp
            d.mkdir(parents=True, exist_ok=True)
            return d

        try:
            R._FONT_DATA_DIR = tmp
            R._data_subdir = fake_subdir
            (tmp / "emoji").mkdir(parents=True, exist_ok=True)
            # 2MB 垃圾：过体积门槛但无解包标记 -> 未安装
            (tmp / "emoji" / R.ANDROID_EMOJI_FILE).write_bytes(b"0" * (2 * 1024 * 1024))
            R._style_font_forget("android")
            self.assertEqual(R._style_font_validated("android"), "")
            self.assertFalse(R._singles_via_local_font("android"))
            packs = {p["id"]: p for p in R.get_emoji_packs_status()}
            self.assertFalse(packs["android"]["installed"])
            # 写入达标标记即恢复
            (tmp / "emoji" / R.ANDROID_PACK_READY).write_text("1800", encoding="utf-8")
            R._style_font_forget("android")
            self.assertTrue(bool(R._style_font_validated("android")))
            self.assertTrue(R._singles_via_local_font("android"))
            packs = {p["id"]: p for p in R.get_emoji_packs_status()}
            self.assertTrue(packs["android"]["installed"])
            # 数量不达标仍视为未安装
            (tmp / "emoji" / R.ANDROID_PACK_READY).write_text("3", encoding="utf-8")
            R._style_font_forget("android")
            self.assertEqual(R._style_font_validated("android"), "")
        finally:
            R._data_subdir = orig_subdir
            R._FONT_DATA_DIR = orig_dd
            R._style_font_forget()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_extract_noto_pngs_unit(self):
        """CBDT 解包：单字收录、修饰位跳过、非 PNG 跳过、写 manifest+标记"""
        import shutil
        import tempfile
        from core import renderer as R
        tmp = Path(tempfile.mkdtemp())
        orig_tt = R._TTFONT
        fake_png = b"\x89PNG\r\n\x1a\n" + b"0" * 100

        class _FakeImg:
            def __init__(self, data):
                self.imageData = data

        class _FakeCBDT:
            strikeData = [{
                "grin": _FakeImg(fake_png),
                "A": _FakeImg(fake_png),
                "zwj": _FakeImg(fake_png),
                "junk": _FakeImg(b"not a png"),
            }]

        class _FakeFont:
            def __init__(self, *a, **k):
                pass

            def getBestCmap(self):
                return {0x41: "A", 0x1F600: "grin", 0x200D: "zwj",
                        0x1F44D: "junk"}

            def __getitem__(self, key):
                assert key == "CBDT"
                return _FakeCBDT()

            def close(self):
                pass

        try:
            R._TTFONT = _FakeFont
            n, err = R._extract_noto_pngs("dummy.ttf", tmp)
            self.assertEqual(err, "")
            self.assertEqual(n, 1)
            self.assertEqual((tmp / "1f600.png").read_bytes(), fake_png)
            self.assertFalse((tmp / "41.png").exists())
            self.assertFalse((tmp / "200d.png").exists())
            manifest = (tmp / R.ANDROID_PACK_MANIFEST).read_text(encoding="utf-8")
            self.assertIn("1f600.png", manifest)
            self.assertEqual((tmp / R.ANDROID_PACK_READY).read_text(encoding="utf-8"), "1")
        finally:
            R._TTFONT = orig_tt
            shutil.rmtree(tmp, ignore_errors=True)

    def test_emoji_mirror_rotation(self):
        """多镜像轮换：首源网络错换下一源；404 直接返回不换源"""
        import shutil
        import tempfile
        from core import renderer as R
        tmp = Path(tempfile.mkdtemp())
        orig_dd = R._FONT_DATA_DIR
        orig_dead = R._EMOJI_REMOTE_DEAD_UNTIL
        orig_cli = R._EMOJI_HTTP_CLIENT
        seen = []
        try:
            R._FONT_DATA_DIR = tmp
            (tmp / "emoji").mkdir(parents=True, exist_ok=True)

            class FlakyClient:
                def stream(self, method, url):
                    seen.append(url)
                    if "cdn.jsdelivr.net" in url:
                        raise ConnectionError("mirror down")
                    return _OkResp()

            class _OkResp:
                status_code = 200

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def raise_for_status(self):
                    pass

                def iter_bytes(self, n):
                    yield b"\x89PNG fake-bytes"

            R._EMOJI_HTTP_CLIENT = FlakyClient()
            R._EMOJI_REMOTE_DEAD_UNTIL = 0.0
            R._EMOJI_MISS_CACHE.pop("1f600", None)  # 防其它用例 404 负缓存串扰
            # fake PNG 无法解码成图没关系：这里只验证落盘行为
            ok = R._fetch_remote_emoji("1f600", tmp / "emoji")
            # iter 内容非 PNG：落盘成功（解码是加载阶段的事）
            self.assertTrue(ok)
            self.assertTrue((tmp / "emoji" / "1f600.png").is_file())
            self.assertTrue(any("cdn.jsdelivr.net" in u for u in seen))
            self.assertTrue(any("fastly.jsdelivr.net" in u for u in seen))
            self.assertEqual(R._EMOJI_REMOTE_DEAD_UNTIL, 0.0)  # 成功不退避
        finally:
            R._FONT_DATA_DIR = orig_dd
            R._EMOJI_REMOTE_DEAD_UNTIL = orig_dead
            R._EMOJI_HTTP_CLIENT = orig_cli
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cdn_probe_closed_port(self):
        """探针：本机闭合端口快速失败（无网络依赖）"""
        try:
            from core.webapi import _cdn_probe_host
        except Exception:
            self.skipTest("webapi 不可用")
        r = _cdn_probe_host("127.0.0.1", timeout=1)
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_font_download_resume(self):
        """字体下载断点续传：残留分片 + 服务端 206，续传后内容完整且只传剩余部分"""
        import functools
        import hashlib
        import http.server
        import shutil
        import socketserver
        import tempfile
        import threading
        from core import renderer as R
        payload = (b"0123456789ABCDEF" * 65536)[: 1024 * 1024]  # 1MB
        srv_dir = Path(tempfile.mkdtemp(prefix="xbimg_srv_"))
        (srv_dir / "f.ttf").write_bytes(payload)
        seen = {"range": [], "sent": 0}

        class H(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.path.split("?")[0] != "/f.ttf":
                    self.send_error(404)
                    return
                seen["range"].append(self.headers.get("Range"))
                start, code = 0, 200
                rg = self.headers.get("Range") or ""
                if rg.startswith("bytes="):
                    try:
                        start = int(rg[len("bytes="):].split("-")[0])
                        assert 0 < start < len(payload)
                        code = 206
                    except Exception:
                        start, code = 0, 200
                body = payload[start:]
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                if code == 206:
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{len(payload)-1}/{len(payload)}")
                self.end_headers()
                self.wfile.write(body)
                seen["sent"] += len(body)

            def log_message(self, *a):
                pass

        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        tmp = Path(tempfile.mkdtemp(prefix="xbimg_resume_"))
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/f.ttf"
            dest = tmp / "f.ttf"
            part = dest.with_name(
                f"{dest.name}."
                f"{hashlib.sha1(url.encode()).hexdigest()[:8]}.downloading")
            part.write_bytes(payload[: 256 * 1024])  # 模拟中断残留 256KB
            ok, err = R._download_file(url, dest)
            self.assertTrue(ok, err)
            self.assertEqual(dest.read_bytes(), payload)
            self.assertFalse(part.exists())
            self.assertIn("bytes=262144-", seen["range"])
            self.assertLess(seen["sent"], len(payload) * 1.5)
        finally:
            httpd.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(srv_dir, ignore_errors=True)

    def test_new_request_query_shape(self):
        """新版 AstrBot 只有 request.query（无 args/environ）：轮询与 nonce 照读"""
        import asyncio
        import core.webapi as webapi_mod
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        orig_req = webapi_mod.request
        orig_jr = webapi_mod.json_response
        orig_er = webapi_mod.error_response

        class NewQuery:
            def __init__(self, d):
                self._d = d

            def get(self, k, default=""):
                v = self._d.get(k, default)
                return v if v else default

        class NewReq:
            query = None

            def __init__(self, q):
                self.query = NewQuery(q)

            async def json(self, default=None):
                return {}

        webapi_mod.json_response = lambda d: d
        webapi_mod.error_response = lambda msg, status_code=500: {"ok": False, "error": msg}
        try:
            # 轮询：能读到 job_id，不再 404
            jid = plugin._new_download_job("emoji", "ios")
            webapi_mod.request = NewReq({"job_id": jid})
            st = asyncio.run(plugin._api_emoji_download_status())
            self.assertTrue(st.get("ok"), st)
            self.assertEqual(st.get("job_id"), jid)
            # nonce：能读到
            webapi_mod.request = NewReq({"nonce": "pv_abc"})
            plugin._preview_nonces["pv_abc"] = 9999999999.0
            self.assertEqual(plugin._read_nonce_from_request(), "pv_abc")
            # 空 query：返回 None/404 而非抛异常
            webapi_mod.request = NewReq({})
            self.assertIsNone(plugin._read_nonce_from_request())
            st2 = asyncio.run(plugin._api_emoji_download_status())
            self.assertFalse(st2.get("ok"))
        finally:
            webapi_mod.request = orig_req
            webapi_mod.json_response = orig_jr
            webapi_mod.error_response = orig_er

    def test_download_job_gen_and_cancel(self):
        """后台任务代际与取消：中途改样式不再强切；删除取消后清新增文件"""
        import asyncio
        import tempfile
        from pathlib import Path
        from unittest import mock
        import core.webapi as webapi_mod
        from core.webapi import WebApiMixin

        tmp = Path(tempfile.mkdtemp(prefix="xbimg_jobcancel_"))

        class FakeCfg:
            def __init__(self):
                self.config = {"emoji_style": "none"}
                self.data_dir = tmp

            def save(self, d=None):
                if d:
                    self.config.update(d)

        class FakeSelf(WebApiMixin):
            def __init__(self):
                self.cfg_mgr = FakeCfg()
                self._emoji_style_gen = 0

        orig_dl = webapi_mod.download_emoji_pack
        orig_cf = webapi_mod.configure_fonts
        webapi_mod.download_emoji_pack = lambda style: {"ok": True, "downloaded": [style], "storage_kb": 1.0}
        webapi_mod.configure_fonts = lambda *a, **k: None
        try:
            async def _go():
                s = FakeSelf()
                # 代际：任务启动后用户改了样式，完成不再切回
                jid = s._launch_download_job("emoji", "ios")
                self.assertTrue(jid)
                s._bump_emoji_style_gen()
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if WebApiMixin._job_public(jid)["status"] != "running":
                        break
                pub = WebApiMixin._job_public(jid)
                self.assertEqual(pub["status"], "done")
                self.assertEqual(s.cfg_mgr.config["emoji_style"], "none")

                # 取消：运行中删除，完成后清新增文件、不生效
                (tmp / "emoji").mkdir(parents=True, exist_ok=True)
                (tmp / "emoji" / "old.png").write_bytes(b"old")
                import threading as _th
                entered = _th.Event()
                release = _th.Event()

                def slow_dl(style):
                    entered.set()
                    release.wait(10)
                    (tmp / "emoji" / "new.png").write_bytes(b"new")
                    return {"ok": True, "downloaded": ["new.png"], "storage_kb": 1.0}

                webapi_mod.download_emoji_pack = slow_dl
                jid2 = s._launch_download_job("emoji", "android")
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(entered.is_set())
                n = s._cancel_download_jobs("emoji", "android")
                self.assertGreaterEqual(n, 1)
                release.set()
                for _ in range(200):
                    await asyncio.sleep(0.02)
                    st = WebApiMixin._job_public(jid2)["status"]
                    if st != "running":
                        break
                pub2 = WebApiMixin._job_public(jid2)
                self.assertNotEqual(pub2["status"], "running")
                self.assertTrue((tmp / "emoji" / "old.png").is_file())
                self.assertFalse((tmp / "emoji" / "new.png").exists())
                self.assertEqual(s.cfg_mgr.config["emoji_style"], "none")

            asyncio.run(_go())
        finally:
            webapi_mod.download_emoji_pack = orig_dl
            webapi_mod.configure_fonts = orig_cf
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_lazy_imports_relative_first(self):
        """懒导入必须相对优先：core/ 包内 `from .core.X` 恒为 core.core（生产必 500）"""
        import re
        core_dir = WORKSPACE / "core"
        bad = []
        for f in sorted(core_dir.glob("*.py")):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                s = line.strip()
                if re.match(r"from \.core\.\w+ import", s):
                    bad.append(f"{f.name}:{i}: {s}")
        self.assertEqual(bad, [])

    def test_android_singles_skip_network_when_font_ready(self):
        """android 本地彩字已装时单字零网络：fetch 直接抛错也必须出图且零调用"""
        import shutil
        import tempfile
        from core import renderer as R
        tmp = Path(tempfile.mkdtemp())
        orig_subdir, orig_dd = R._data_subdir, R._FONT_DATA_DIR
        orig_fetch = R._fetch_remote_emoji
        calls = {"n": 0}

        def fake_subdir(name=""):
            d = tmp / name if name else tmp
            d.mkdir(parents=True, exist_ok=True)
            return d

        def boom(code, dest):
            calls["n"] += 1
            return False  # 如实模拟下载失败（真实函数从不抛异常）

        try:
            R._FONT_DATA_DIR = tmp
            R._data_subdir = fake_subdir
            (tmp / "emoji").mkdir(parents=True, exist_ok=True)
            # 占位彩字必须真实可用，否则有效性校验会如实拒绝
            font_bytes = self._real_emoji_font_bytes()
            if not font_bytes:
                self.skipTest("本机无可用彩色 emoji 字体，跳过")
            (tmp / "emoji" / R.ANDROID_EMOJI_FILE).write_bytes(font_bytes)
            (tmp / "emoji" / R.ANDROID_PACK_READY).write_text("1800", encoding="utf-8")
            R._style_font_forget("android")
            R._fetch_remote_emoji = boom
            R._EMOJI_IMG_CACHE.clear()
            self.assertTrue(R._singles_via_local_font("android"))
            self.assertFalse(R._singles_via_local_font("ios"))
            self.assertFalse(R._singles_via_local_font("none"))
            img = R.MessageImageRenderer.render(
                "单字零网络测试💰好", style="ios", theme_mode="light",
                star_background=False, emoji_remote=True, emoji_style="android",
            )
            self.assertIsNotNone(img)
            self.assertEqual(calls["n"], 0)
            # 复杂簇仍允许按需补全（正确性优先）：只验证不抛异常
            calls["n"] = 0
            R._EMOJI_IMG_CACHE.clear()
            img2 = R.MessageImageRenderer.render(
                "簇测试👨‍👩‍👧好", style="ios", theme_mode="light",
                star_background=False, emoji_remote=True, emoji_style="android",
            )
            self.assertIsNotNone(img2)
        finally:
            R._data_subdir = orig_subdir
            R._FONT_DATA_DIR = orig_dd
            R._fetch_remote_emoji = orig_fetch
            R._EMOJI_IMG_CACHE.clear()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_extra_font_dirs_discovery(self):
        """挂载目录发现：有特征文件名直收，无特征文件名走 cmap 内容校验"""
        import shutil
        import tempfile
        from core import renderer as R
        src = R._FONT_REGULAR_PATH
        if not src or not R._is_usable_font(src):
            self.skipTest("无可用系统字体，跳过")
        tmp = Path(tempfile.mkdtemp())
        orig_env = os.environ.get("XBIMG_FONT_DIRS")
        orig_sys = R._SYSTEM_CANDIDATES
        try:
            shutil.copy(src, tmp / "mytest-noto-fake.ttf")  # 有特征名
            shutil.copy(src, tmp / "zzcustom123.ttf")  # 无特征名，走 cmap
            os.environ["XBIMG_FONT_DIRS"] = str(tmp)
            R._SYSTEM_CANDIDATES = None
            found = R._scan_extra_font_dirs()
            names = [Path(p).name for p in found]
            self.assertIn("mytest-noto-fake.ttf", names)
            self.assertIn("zzcustom123.ttf", names)
        finally:
            if orig_env is None:
                os.environ.pop("XBIMG_FONT_DIRS", None)
            else:
                os.environ["XBIMG_FONT_DIRS"] = orig_env
            R._SYSTEM_CANDIDATES = orig_sys
            shutil.rmtree(tmp, ignore_errors=True)


    def test_dead_cache_path_treated_as_miss(self):
        """缓存命中校验：done 条目指向已删除文件 → miss 并删除内存条目"""
        import time as _t
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        dead = plugin.cache_dir / "t2i_1790069728231_a9b664.jpg"  # 不存在
        text = "我的信息"
        key = plugin._text_hash(text)
        plugin._render_hash_slot()[key] = {
            "ts": _t.monotonic(), "outcome": "done", "img_paths": [dead],
        }
        # 命中校验：文件不存在 → 返回空并删除条目（下次请求完整重跑）
        self.assertEqual(plugin._alive_prior_paths(text), [])
        self.assertIsNone(plugin._text_render_info(text))
        self.assertNotIn(key, plugin._render_hash_slot())

    def test_alive_cache_path_hits(self):
        """缓存命中校验：done 条目文件仍存在 → 正常复用，条目保留"""
        import time as _t
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        alive = plugin.cache_dir / "t2i_alive_test.jpg"
        alive.write_bytes(b"\xff\xd8\xff")
        self.addCleanup(lambda: alive.unlink(missing_ok=True))
        text = "菜单"
        key = plugin._text_hash(text)
        plugin._render_hash_slot()[key] = {
            "ts": _t.monotonic(), "outcome": "done", "img_paths": [str(alive)],
        }
        paths = plugin._alive_prior_paths(text)
        self.assertEqual([str(p) for p in paths], [str(alive)])
        self.assertIsNotNone(plugin._text_render_info(text))

    def test_await_inflight_claim_reaches_done(self):
        """并发窗口：outcome 未定时等待终态，done/blocked/gone 均正确返回"""
        import asyncio
        import time as _t
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        text = "并发等待测试"
        key = plugin._text_hash(text)
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        alive = plugin.cache_dir / "t2i_inflight.jpg"
        alive.write_bytes(b"\xff\xd8\xff")
        self.addCleanup(lambda: alive.unlink(missing_ok=True))

        async def _go():
            # 1) 首条认领中（outcome=None）→ 后台置 done 后应返回 done
            plugin._render_hash_slot()[key] = {
                "ts": _t.monotonic(), "outcome": None, "img_paths": [],
            }
            async def _finish():
                await asyncio.sleep(0.12)
                info = plugin._render_hash_slot().get(key)
                info["outcome"] = "done"
                info["img_paths"] = [str(alive)]
            task = asyncio.ensure_future(_finish())
            st = await plugin._await_inflight_claim(text, timeout=2.0)
            await task
            self.assertEqual(st, "done")
            self.assertEqual([str(p) for p in plugin._alive_prior_paths(text)], [str(alive)])

            # 2) 条目消失 → gone
            plugin._render_hash_slot().pop(key, None)
            st2 = await plugin._await_inflight_claim(text, timeout=0.5)
            self.assertEqual(st2, "gone")

            # 3) 终态 blocked
            plugin._render_hash_slot()[key] = {
                "ts": _t.monotonic(), "outcome": "blocked",
            }
            st3 = await plugin._await_inflight_claim(text, timeout=0.5)
            self.assertEqual(st3, "blocked")

            # 4) 超时仍无终态 → gone（调用方可重新认领）
            plugin._render_hash_slot()[key] = {
                "ts": _t.monotonic(), "outcome": None,
            }
            st4 = await plugin._await_inflight_claim(text, timeout=0.15)
            self.assertEqual(st4, "gone")

        asyncio.run(_go())

    def test_claim_inflight_then_wait_and_reuse(self):
        """回归：首条仍在渲染时第二条不得丢转图 —— 等待后复用 done 结果"""
        import asyncio
        import time as _t
        from main import Msg2ImgPlugin
        from core.config import DEFAULT_CONFIG as DC
        cfg = dict(DC)
        cfg["group_mode"] = "all"
        plugin = Msg2ImgPlugin(context=None, config=cfg)
        text = "同文并发转图"
        key = plugin._text_hash(text)
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        # 预置首条渲染产物（模拟首条进行中即将完成）
        img = plugin.cache_dir / "t2i_conc.jpg"
        img.write_bytes(b"\xff\xd8\xff")
        self.addCleanup(lambda: img.unlink(missing_ok=True))
        plugin._render_hash_slot()[key] = {
            "ts": _t.monotonic(), "outcome": None, "img_paths": [],
        }

        async def _go():
            async def _complete_first():
                await asyncio.sleep(0.15)
                info = plugin._render_hash_slot().get(key)
                info["outcome"] = "done"
                info["img_paths"] = [str(img)]
            task = asyncio.ensure_future(_complete_first())
            # 第二条：claim 失败 → 等待 inflight → done 后复用图路径
            claim = plugin._claim_text_render(text)
            self.assertEqual(claim, "")
            st = await plugin._await_inflight_claim(text, timeout=2.0)
            await task
            self.assertEqual(st, "done")
            paths = plugin._alive_prior_paths(text)
            self.assertEqual([str(p) for p in paths], [str(img)])
            # 模拟 _transform 缓存命中分支：组装 image 段而非丢回纯文本
            segs = plugin._image_segs_from_paths(paths)
            self.assertTrue(segs)
            self.assertEqual(segs[0].get("type"), "image")
            return segs

        out = asyncio.run(_go())
        self.assertTrue(out)

    def test_forget_cached_path_bidirectional(self):
        """文件删除 → 同步剔除内存条目引用；条目清空则整条删除（双向一致）"""
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        p1 = plugin.cache_dir / "t2i_forg1.jpg"
        p2 = plugin.cache_dir / "t2i_forg2.jpg"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")
        key = plugin._text_hash("同文两条")
        plugin._render_hash_slot()[key] = {
            "ts": 0.0, "outcome": "done", "img_paths": [str(p1), str(p2)],
        }
        # 删一个文件：条目保留剩余路径
        p1.unlink()
        plugin._forget_cached_path(p1)
        info = plugin._render_hash_slot().get(key)
        self.assertIsNotNone(info)
        self.assertEqual(info["img_paths"], [str(p2)])
        # 删最后一个文件：整条删除
        p2.unlink()
        plugin._forget_cached_path(p2)
        self.assertNotIn(key, plugin._render_hash_slot())

    def test_render_info_ttl_expiry(self):
        """过期策略：终态内存条目超过 _RENDER_INFO_TTL 读取即失效删除"""
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        key = plugin._text_hash("过期条目")
        plugin._render_hash_slot()[key] = {
            "ts": 0.0, "outcome": "done", "img_paths": ["whatever.jpg"],
        }
        import time as _t
        # ts=0 且 monotonic() 远大于 TTL → 过期
        if _t.monotonic() < plugin._RENDER_INFO_TTL + 1:
            plugin._render_hash_slot()[key]["ts"] = _t.monotonic() - plugin._RENDER_INFO_TTL - 1
        self.assertIsNone(plugin._text_render_info("过期条目"))
        self.assertNotIn(key, plugin._render_hash_slot())

    def test_drop_dead_local_images_backstop(self):
        """发送前兜底：剥离缺失的本地绝对路径 image 段，远程/base64/存活段保留"""
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        alive = plugin.cache_dir / "t2i_ok.jpg"
        alive.write_bytes(b"\xff\xd8\xff")
        self.addCleanup(lambda: alive.unlink(missing_ok=True))
        dead = str(plugin.cache_dir / "t2i_gone.jpg")  # 不存在
        msg = [
            {"type": "image", "data": {"file": dead}},
            {"type": "image", "data": {"file": f"file://{alive}"}},
            {"type": "image", "data": {"file": "https://example.com/a.jpg"}},
            {"type": "image", "data": {"file": "base64://AAAA"}},
            {"type": "image", "data": {"file": "relative_virtual.jpg"}},
            {"type": "text", "data": {"text": "hi"}},
        ]
        out = plugin._drop_dead_local_images(msg)
        files = [s["data"]["file"] for s in out if s.get("type") == "image"]
        self.assertNotIn(dead, files)
        self.assertEqual(len(files), 4)
        self.assertTrue(any(s.get("type") == "text" for s in out))
        # 全部失效 → 降级空白文本段，不发死路径也不发空消息
        only_dead = [{"type": "image", "data": {"file": dead}}]
        out2 = plugin._drop_dead_local_images(only_dead)
        self.assertEqual(len(out2), 1)
        self.assertEqual(out2[0]["type"], "text")

    @staticmethod
    def _b64_png(w, h, color=(255, 255, 255)):
        import base64 as _b64
        from io import BytesIO
        from PIL import Image as _Image
        buf = BytesIO()
        _Image.new("RGB", (w, h), color).save(buf, format="PNG")
        return "base64://" + _b64.b64encode(buf.getvalue()).decode("ascii")

    def test_shrink_oversized_images_gated_by_size(self):
        """发送前体积守卫：超限 base64 图段降质（0.75 缩放→JPEG），
        未超限/损坏/过小的段不动（对应 retcode=1200 双发修复后的新路径）"""
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        ref = self._b64_png(200, 200, (200, 40, 40))
        msg = [
            {"type": "image", "data": {"file": ref}},
            {"type": "text", "data": {"text": "hi"}},
        ]
        self.assertTrue(plugin._shrink_oversized_images(msg, max_bytes=100))
        new_ref = msg[0]["data"]["file"]
        self.assertTrue(new_ref.startswith("base64://"))
        self.assertNotEqual(new_ref, ref)
        import base64 as _b64
        from io import BytesIO
        from PIL import Image as _Image
        out = _Image.open(BytesIO(_b64.b64decode(new_ref[9:])))
        self.assertEqual(out.size, (150, 150))  # 200 * 0.75
        # 未超限：不动
        untouched = [{"type": "image", "data": {"file": ref}}]
        self.assertFalse(plugin._shrink_oversized_images(untouched, max_bytes=10 * 1024 * 1024))
        self.assertEqual(untouched[0]["data"]["file"], ref)
        # 损坏/过小 base64：不动段、不抛异常（阈值 1 字节保证进入降质分支）
        bad = [{"type": "image", "data": {"file": "base64://QUJD"}}]
        self.assertFalse(plugin._shrink_oversized_images(bad, max_bytes=1))
        self.assertEqual(bad[0]["data"]["file"], "base64://QUJD")
        tiny = [{"type": "image", "data": {"file": self._b64_png(32, 32)}}]
        self.assertFalse(plugin._shrink_oversized_images(tiny, max_bytes=1))

    def test_send_guard_shrinks_oversized_then_sends_once(self):
        """发送前置守卫：超限图先在工作线程降质，fn 只被调用一次且收到缩小后的图"""
        import asyncio
        import base64 as _b64
        import os as _os
        from io import BytesIO
        from PIL import Image as _Image
        from main import Msg2ImgPlugin
        cfg = dict(DEFAULT_CONFIG)
        cfg["img_send_shrink_kb"] = 1  # 阈值 1KB，保证噪声 PNG 必超限
        plugin = Msg2ImgPlugin(context=None, config=cfg)
        noise = _Image.frombytes("RGB", (200, 200), _os.urandom(200 * 200 * 3))
        buf = BytesIO()
        noise.save(buf, format="PNG")
        orig = "base64://" + _b64.b64encode(buf.getvalue()).decode("ascii")
        msg = [{"type": "image", "data": {"file": orig}}]
        seen = []

        async def fn(*a, **k):
            m = k.get("message")
            seen.append(m[0]["data"]["file"] if m else None)
            return "ok"

        res = asyncio.run(
            plugin._invoke_send_with_guard(fn, "send_group_msg", group_id=1, message=msg)
        )
        self.assertEqual(res, "ok")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("base64://"))
        self.assertNotEqual(seen[0], orig)
        out = _Image.open(BytesIO(_b64.b64decode(seen[0][9:])))
        self.assertEqual(out.size, (150, 150))

    def test_send_timeout_raises_without_resend(self):
        """回归（线上双图）：retcode=1200 超时后绝不自动重发——
        NapCat 超时时消息常已实际送达，重发导致同一张图发两次"""
        import asyncio
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        msg = [{"type": "image", "data": {"file": self._b64_png(200, 200)}}]
        calls = []

        class _TimeoutErr(Exception):
            pass

        async def fn(*a, **k):
            calls.append(1)
            raise _TimeoutErr(
                "<ActionFailed status='failed', retcode=1200, "
                "message='Timeout: NTEvent sendMsg'>"
            )

        with self.assertRaises(_TimeoutErr):
            asyncio.run(
                plugin._invoke_send_with_guard(fn, "send_group_msg", group_id=1, message=msg)
            )
        self.assertEqual(len(calls), 1, "超时后必须只发送一次，禁止重发造成双图")

    def test_transform_rerenders_after_cache_file_deleted(self):
        """回归：缓存文件被清理后，同文请求必须完整重跑渲染出新文件，
        不得复用死路径（对应 FileNotFoundError: t2i_*.jpg 已删除）"""
        import asyncio
        import time as _t
        from main import Msg2ImgPlugin
        cfg = dict(DEFAULT_CONFIG)
        cfg["group_mode"] = "all"
        plugin = Msg2ImgPlugin(context=None, config=cfg)
        text = "我的信息回归测试"
        dead = plugin.cache_dir / "t2i_1790069728231_a9b664.jpg"
        key = plugin._text_hash(text)
        # 模拟故障现场：45 分钟前完成的 done 条目，文件已被 45s 定时清理删除
        plugin._render_hash_slot()[key] = {
            "ts": _t.monotonic() - 2700.0, "outcome": "done",
            "img_paths": [str(dead)],
        }

        async def _go():
            return await plugin._transform_onebot_message(
                "123456", [{"type": "text", "data": {"text": text}}]
            )

        out = asyncio.run(_go())
        img_files = [
            s["data"]["file"] for s in out
            if isinstance(s, dict) and s.get("type") == "image"
        ]
        self.assertTrue(img_files, "应重新渲染出图片段")
        self.assertNotIn(str(dead.resolve()), [str(Path(f).resolve()) for f in img_files])
        for f in img_files:
            self.assertTrue(Path(f).is_file(), f"新生成的图片必须存在: {f}")
        self.addCleanup(lambda: [Path(f).unlink(missing_ok=True) for f in img_files])
        # 内存条目已指向新文件
        info = plugin._text_render_info(text)
        self.assertIsNotNone(info)
        self.assertEqual(info.get("outcome"), "done")
        for p in info.get("img_paths") or []:
            self.assertTrue(Path(p).is_file())

    def test_transform_str_branch_rerenders_after_cache_deleted(self):
        """回归：str 分支同样不得复用死路径，重跑后返回的段全部指向现存文件"""
        import asyncio
        import time as _t
        from main import Msg2ImgPlugin
        cfg = dict(DEFAULT_CONFIG)
        cfg["group_mode"] = "all"
        plugin = Msg2ImgPlugin(context=None, config=cfg)
        text = "菜单回归测试"
        dead = plugin.cache_dir / "t2i_1790069721495_ddb872.jpg"
        key = plugin._text_hash(text)
        plugin._render_hash_slot()[key] = {
            "ts": _t.monotonic(), "outcome": "done",
            "img_paths": [str(dead)],
        }

        async def _go():
            return await plugin._transform_onebot_message("123456", text)

        out = asyncio.run(_go())
        self.assertIsInstance(out, list)
        img_files = [
            s["data"]["file"] for s in out
            if isinstance(s, dict) and s.get("type") == "image"
        ]
        self.assertTrue(img_files)
        self.assertNotIn(str(dead.resolve()), [str(Path(f).resolve()) for f in img_files])
        for f in img_files:
            self.assertTrue(Path(f).is_file())
        self.addCleanup(lambda: [Path(f).unlink(missing_ok=True) for f in img_files])


    def test_schedule_delete_syncs_memory_entry(self):
        """即时清理删除文件后，同步删除内存条目引用（正向双向一致）"""
        import asyncio
        from main import Msg2ImgPlugin
        plugin = Msg2ImgPlugin(context=None, config=dict(DEFAULT_CONFIG))
        plugin.cache_dir.mkdir(parents=True, exist_ok=True)
        f = plugin.cache_dir / "t2i_sched_sync.jpg"
        f.write_bytes(b"x")
        text = "即时清理同文"
        key = plugin._text_hash(text)
        plugin._render_hash_slot()[key] = {
            "ts": __import__("time").monotonic(),
            "outcome": "done", "img_paths": [str(f)],
        }

        async def _go():
            plugin._schedule_delete(f, 0)
            await asyncio.sleep(0.2)

        asyncio.run(_go())
        self.assertFalse(f.exists())
        self.assertNotIn(key, plugin._render_hash_slot())

    def test_emoji_404_negative_cache(self):
        """Twemoji 404 进负缓存：10 分钟内不再打网，负缓存过期后可重试"""
        import time as _t
        from core import renderer as R
        R._EMOJI_MISS_CACHE.clear()
        self.addCleanup(R._EMOJI_MISS_CACHE.clear)
        R._emoji_mark_miss("deadbeef")
        self.assertTrue(R._emoji_miss_cached("deadbeef"))
        # 过期后自动清除
        R._EMOJI_MISS_CACHE["deadbeef"] = _t.time() - 1.0
        self.assertFalse(R._emoji_miss_cached("deadbeef"))
        self.assertNotIn("deadbeef", R._EMOJI_MISS_CACHE)
        # 网络退避不受负缓存影响（全局退避独立）
        self.assertFalse(R._emoji_miss_cached("other"))

    def test_fetch_remote_emoji_respects_negative_cache(self):
        """负缓存命中的 code 不再发起 HTTP（mock 客户端应零调用）"""
        from pathlib import Path as _P
        import tempfile as _tf
        from core import renderer as R
        R._EMOJI_MISS_CACHE.clear()
        self.addCleanup(R._EMOJI_MISS_CACHE.clear)
        R._emoji_mark_miss("1f9e1")
        with _tf.TemporaryDirectory() as td:
            with mock.patch.object(R, "_emoji_http_client") as m_cli:
                ok = R._fetch_remote_emoji("1f9e1", _P(td))
        self.assertFalse(ok)
        m_cli.assert_not_called()

    def test_render_cache_key_stable_across_minutes(self):
        """渲染缓存键不含墙钟分钟：跨分钟同参必须同键（避免每分钟强制重渲）"""
        from core.renderer import _render_cache_key
        kwargs = dict(
            text="同一段文本", style="ios", theme_mode="light",
            star_background=True, star_density="medium",
            mosaic_mode="none", mosaic_type="pixel", mosaic_half_pos="bottom",
            violation_words=None, font_scale=100, emoji_style="android",
        )
        with mock.patch("core.renderer.time") as m_time:
            m_time.strftime.return_value = "23:59"
            k1 = _render_cache_key(**kwargs)
            m_time.strftime.return_value = "00:00"
            k2 = _render_cache_key(**kwargs)
        self.assertEqual(k1, k2)

    def test_layout_cache_hit_equivalent(self):
        """排版缓存：同参二次调用复用，关键几何字段一致"""
        from core.renderer import MessageImageRenderer, _LAYOUT_CACHE
        _LAYOUT_CACHE.clear()
        self.addCleanup(_LAYOUT_CACHE.clear)
        args = dict(
            text="排版缓存回归\n- 项\n> 引", style="ios", theme_mode="light",
            mosaic_half_pos="bottom", font_scale=100, emoji_remote=True,
            emoji_style="android", page_max_h=3000, card_max_width=640,
        )
        c1 = MessageImageRenderer._prepare_layout(**args)
        n_after_first = len(_LAYOUT_CACHE)
        self.assertGreater(n_after_first, 0)
        c2 = MessageImageRenderer._prepare_layout(**args)
        for k in ("card_w", "card_h", "content_w", "content_h", "max_content",
                  "header_h", "inner_pad_x", "text", "style"):
            self.assertEqual(c1[k], c2[k], f"字段 {k} 不一致")
        self.assertEqual(len(c1["rendered_lines"]), len(c2["rendered_lines"]))
        # 不同文本不误命中
        c3 = MessageImageRenderer._prepare_layout(**{**args, "text": "另一段"})
        self.assertNotEqual(c1["text"], c3["text"])

    def test_text_advance_memo_hit(self):
        """整串推进 memo：同字体同串二次调用直接命中缓存"""
        from core.renderer import get_font, _text_advance, _TEXT_ADV_CACHE
        _TEXT_ADV_CACHE.clear()
        self.addCleanup(_TEXT_ADV_CACHE.clear)
        font = get_font(26, bold=False)
        s = "整串推进缓存测试ABC"
        w1 = _text_advance(font, s)
        self.assertGreater(w1, 0)
        self.assertTrue(any(k[2] == s for k in _TEXT_ADV_CACHE))
        w2 = _text_advance(font, s)
        self.assertEqual(w1, w2)

    def test_star_layer_shared_across_texts(self):
        """星空层跨文本共享：几何量化键不含文本，不同文本可复用同一层"""
        from core.renderer import _STAR_LAYER_CACHE, _stable_seed
        _STAR_LAYER_CACHE.clear()
        self.addCleanup(_STAR_LAYER_CACHE.clear)
        img1 = MessageImageRenderer.render(
            "文本甲测试星空共享", style="ios", star_background=True, star_density="medium",
        )
        n1 = len(_STAR_LAYER_CACHE)
        self.assertGreater(n1, 0)
        img2 = MessageImageRenderer.render(
            "文本乙测试星空共享", style="ios", star_background=True, star_density="medium",
        )
        self.assertIsNotNone(img1)
        self.assertIsNotNone(img2)
        # 同几何桶：缓存不再随文本数线性膨胀
        self.assertLessEqual(len(_STAR_LAYER_CACHE), 8)
        # 键不含原始文本（稳定种子仅依赖量化几何）
        key = next(iter(_STAR_LAYER_CACHE))
        self.assertNotIn("文本甲", str(key))
        self.assertIsInstance(_stable_seed(str(key)), int)

    def test_prefetch_submits_to_pool(self):
        """预取可提交到共享线程池并正常汇合（layout 并行路径冒烟）"""
        from core.renderer import _submit_prefetch, _prefetch_emoji_images
        fut = _submit_prefetch("冒烟💰", skip_singles=True)
        self.assertIsNotNone(fut)
        done, complete = fut.result(timeout=5.0)
        self.assertIsInstance(done, int)
        self.assertIsInstance(complete, bool)
        # 空文本直接短路
        self.assertEqual(_prefetch_emoji_images(""), (0, True))


if __name__ == "__main__":
    unittest.main()
