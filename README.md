# 消息转图助手

🎨 AstrBot 高颜值消息文本转图片渲染插件，自动把机器人发送的纯文本渲染为 iOS / Android 16 双风格图片，附带内容安全审查与 Web 可视化控制台。

- 📦 项目主页：https://github.com/imsuperone/xbimg
- 🔌 插件 ID：`astrbot_plugin_xbimg`
- 📌 版本：`v1.0.5`，要求 AstrBot `>=3.4.0`，平台 `aiocqhttp`

---

## 🌟 核心特性

- 🍏 **iOS 风格**：磨砂玻璃质感、Apple 连续曲率圆角、多层细腻投影，浅色 / 深色双主题。
- 🤖 **Android 16 风格**：Material 3 Expressive 胶囊容器、大圆角排版，顶栏纯时间显示。
- 📝 **Markdown 排版**：标题、列表、引用、代码块（含高亮）、链接、Emoji 混排自动美化。
- ✨ **星空背景**：背景四周随机散落十字星芒与微光粒子，卡片内无噪点，`sparse / medium / dense` 三档密度。
- 💾 **三档体积优化**：`low` 原画无损 PNG / `medium` 均衡 JPEG Q94 / `high` 极小 JPEG Q86，全程 4:4:4 无抽样，缓存 45 秒自动清理。
- 🔗 **链接策略**：`as_image` 直接转图 / `keep_text` 含链接保持纯文本可点击 / `extract_append` 转图后附带纯文本直达链接（自动剥离中英文尾部标点）。
- 🛡️ **敏感词审查**：6 套内置词库，支持空格混淆命中，可全局切换 + 单群绑定。
- 🤖 **AI 独立审查**：关键词未命中时可调用大模型复审，支持 `astrbot` 已接入模型与自定义 OpenAI 兼容接口，自定义提示词。
- 🎭 **违规处置**：`half` 打一半马赛克 / `full` 全文打码 / `block` 彻底拦截 / `notice` 替换为合规警示卡片；马赛克支持 `pixel` 像素块 / `blur` 高斯模糊，半码位置 `bottom / top / random`，字级精准打码。
- 👥 **群组精细化**：`whitelist / all / blacklist` 三种生效模式，单群可独立设置字体、风格、主题、词库、精选字体。
- 🔤 **字体与 Emoji 管理**：6 款精选中文字体（思源黑体 / 思源宋体 / 霞鹜文楷 / 站酷快乐体 / 马善政毛笔 / macOS 苹方）一键下载卸载无锁定；`ios / android / windows / none` 四种 Emoji 样式按需下载，支持云端补全与本地缓存。
- 📱 **WebUI 控制台**：AstrBot 后台单列全屏仪表盘，含状态统计、实时文本渲染测试台、字体 / Emoji / 词库管理，手机电脑自适应，修改即自动保存。
- ⚡ **高优先级无损拦截**：`on_decorating_result(priority=99999)` + 适配器 `send_group_msg / call_action` 双钩子，保留 At / Reply / 图片音视频段，`xbbot` 等业务插件兼容。

## 📚 内置词库（6 套）

| ID | 名称 | 用途 |
| :--- | :--- | :--- |
| `default` | 标准涉敏与违禁词库（默认综合） | 涉毒 / 涉赌 / 涉黄 / 黑产 / 诈骗，默认全局生效 |
| `anti_nsfw_ultra` | 强力综合违禁与色情库 | 日常 / AI 发情 / SM / 玩法 / 百合等强力色情 |
| `anti_porn` | 严格涉黄与低俗色情库 | 单群严管色情时绑定 |
| `anti_gambling` | 严格涉赌涉诈与黑产库 | 单群严管赌诈时绑定 |
| `anti_drugs` | 严格涉毒与违禁品库 | 单群严管涉毒时绑定 |
| `xbbot_game` | 互动娱乐 / 涉黑调教过滤库 | 互动娱乐玩法违规词过滤 |

切换：`/xbimg kwset <方案ID>`（全局），`/xbimg group kw <方案ID> / default [群号]`（单群，可异地操作）。

---

## 🚀 安装

1. AstrBot 插件市场搜索 `消息转图助手` 安装，或手动克隆：
   ```bash
   git clone https://github.com/imsuperone/xbimg.git data/plugins/astrbot_plugin_xbimg
   ```
2. 重启 AstrBot，插件自动加载。
3. 聊天发送 `/xbimg` 查看控制台，AstrBot 后台打开 `消息转图` 页面做可视化配置。

## 🚀 聊天指令（`/xbimg` 全局管理，仅管理员可修改）

| 指令 | 说明 |
| :--- | :--- |
| `/xbimg` | 控制台状态与完整菜单 |
| `/xbimg on` / `off` | 总开关 |
| `/xbimg trigger always` / `violation` | 始终转图 / 仅违规时转图 |
| `/xbimg minlen <1-1000>` | 最小触发字数 |
| `/xbimg style ios` / `android16` | 全局视觉风格 |
| `/xbimg theme light` / `dark` | 全局浅色 / 深色 |
| `/xbimg star on` / `off` | 星空背景开关 |
| `/xbimg density sparse` / `medium` / `dense` | 星星密度 |
| `/xbimg quality low` / `medium` / `high` | 原画无损 / 均衡 / 极小文件 |
| `/xbimg link image` / `text` / `append` | 链接转图 / 保持文本 / 转图+附链接 |
| `/xbimg mod on` / `off` | 屏蔽词审查开关 |
| `/xbimg ai on` / `off` | AI 审查独立开关 |
| `/xbimg action half` / `full` / `block` / `notice` | 违规处置动作 |
| `/xbimg mosaic pixel` / `blur` | 像素块 / 毛玻璃打码 |
| `/xbimg mosaicpos bottom` / `top` / `random` | 半码遮下半 / 上半 / 随机 |
| `/xbimg groupmode whitelist` / `all` / `blacklist` | 仅白名单 / 全部 / 黑名单排除 |
| `/xbimg kwset <方案ID>` | 全局词库切换 |
| `/xbimg size [群号] <50-500>` | 全局或指定群字体（`100` 恢复默认） |
| `/xbimg test [文本]` | 生成测试效果图 |

## 👥 单群指令（`/xbimg group`，仅管理员，群内执行或追加工号异地操作）

| 指令 | 说明 |
| :--- | :--- |
| `/xbimg group` | 本群专属配置卡片与指南（可附群号查看异地群） |
| `/xbimg group set <50-500> [群号]` | 单群字体大小 |
| `/xbimg group style ios` / `android16` / `default` `[群号]` | 单群风格（`default` 恢复跟随全局） |
| `/xbimg group theme light` / `dark` / `default` `[群号]` | 单群主题（`default` 恢复跟随全局） |
| `/xbimg group kw <方案ID>` / `default` `[群号]` | 单群绑定敏感词方案 |
| `/xbimg group ttf <字体id> [群号]` | 单群精选字体切换 |
| `/xbimg group list` | 可用字体 + 词库列表 |
| `/xbimg group reset [群号]` | 单群全部恢复跟随全局 |
| `/xbimg group test [群号]` | 单群专属效果测试图 |

---

## 🖥️ WebUI

AstrBot 后台 `消息转图` 页面提供：运行统计（累计渲染 / 违规拦截 / 打码数）、触发与视觉设置、链接与审查设置、群生效名单、精选字体与 Emoji 包管理、词库预览切换、实时文本渲染测试台（含普通 / 半遮蔽 / 链接三种示例）。

## ⚙️ 主要配置项

`_conf_schema.json` / WebUI 可调：`enable`、`style`、`theme_mode`、`star_background`、`star_density`、`min_length_threshold`、`render_trigger`、`link_mode`、`moderation_mode`、`enable_ai_moderation`（`ai_provider_mode: astrbot/custom`、`ai_api_base/key/model`、`custom_ai_prompt`）、`violation_action`、`mosaic_type`、`mosaic_half_pos`、`img_compress_level`、`font_scale`、`group_font_scales`、`group_configs`、`group_mode`、`group_list`、`font_source`、`custom_font_path/url`、`emoji_style`、`emoji_remote`。

## 📦 依赖与字体

- `Pillow >= 9.1.0`、`httpx >= 0.24.0`、`numpy >= 1.22`
- Windows 开箱即用（微软雅黑 / 思源）；Linux 请安装 `fonts-noto-cjk` 或 `wqy-microhei`，或把 `.ttf/.ttc/.otf` 放入 `assets/fonts/`，或在 WebUI 精选字体中一键下载。
- 测试：`python test_plugin.py`（渲染 / 审查 / 配置 / Emoji / 字体全量用例）。

## 🧾 备注

- 私聊默认生效；群聊受 `group_mode + group_list` 控制。
- `violation_only` 模式下普通消息保持纯文本，仅违规转图 / 打码 / 拦截。
- 渲染失败自动回落原文，不影响正常发送。
