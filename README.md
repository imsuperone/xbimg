# 消息转图助手（astrbot_plugin_xbimg）

将机器人发送的纯文本消息自动渲染为高品质图片，提供内容安全审查与 Web 可视化控制台。

本插件基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 开源的一站式 Agent 聊天机器人平台，支持主流 IM 平台与多种大模型接入，内置 WebUI 与插件扩展体系。使用文档见 [docs.astrbot.app](https://docs.astrbot.app)。

- 项目主页：https://github.com/imsuperone/xbimg
- 插件 ID：`astrbot_plugin_xbimg`
- 当前版本：`v1.3.20`（要求 AstrBot `>=3.4.0`，平台 `aiocqhttp`）

## 功能特性

- 双主题视觉：iOS 磨砂玻璃质感 / Android 16 胶囊容器，浅色、深色双主题，Apple 连续曲率圆角与多层投影。
- Markdown 排版：标题、列表、引用、代码块（含高亮）、链接、Emoji 混排自动美化。
- 星空背景：四周随机星芒与微光粒子，卡片内无噪点，`sparse / medium / dense` 三档密度。
- 体积优化：均衡 JPEG / 极小 JPEG / 原画 PNG 三档，缓存 90 秒自动清理；发送前超限图片自动降质。
- 链接策略：直接转图 / 含链接保持纯文本可点击 / 转图后附带纯文本直达链接。
- 敏感词审查：6 套内置词库，支持空格混淆命中，可全局切换与单群绑定。
- 违规处置：半字打码 / 全文打码 / 拦截 / 合规警示卡片；马赛克支持像素块与高斯模糊，半码位置可选。
- 群组精细化：`whitelist / all / blacklist` 三种生效模式，单群独立设置字体、风格、主题、词库。
- 字体与 Emoji 管理：6 款精选中文字体一键下载卸载，`ios / android / windows / none` 四种 Emoji 样式按需下载。
- WebUI 控制台：全屏仪表盘，含实时渲染测试台、字体 / Emoji / 词库管理，修改即自动保存；预览打码为独立开关（默认不打码），与正式违规处置配置解耦。
- 高优先级无损拦截：保留 At / Reply / 图片音视频段，与 `xbbot` 等业务插件兼容。

## 安装

1. 在 AstrBot 插件市场搜索「消息转图助手」安装，或手动克隆：
   ```bash
   git clone https://github.com/imsuperone/xbimg.git data/plugins/astrbot_plugin_xbimg
   ```
2. 重启 AstrBot，插件自动加载。
3. 聊天发送 `/xbimg` 查看控制台，在 AstrBot 后台打开「消息转图」页面进行可视化配置。

## 聊天指令（`/xbimg` 全局管理，仅管理员可修改）

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
| `/xbimg quality lossless` / `balanced` / `compact` | 原画无损 / 均衡 / 极小文件 |
| `/xbimg link image` / `text` / `append` | 链接转图 / 保持文本 / 转图+附链接 |
| `/xbimg mod on` / `off` | 屏蔽词审查开关 |
| `/xbimg action half` / `full` / `block` / `notice` | 违规处置动作 |
| `/xbimg mosaic pixel` / `blur` | 像素块 / 毛玻璃打码 |
| `/xbimg mosaicpos bottom` / `top` / `random` | 半字打码位置 |
| `/xbimg groupmode whitelist` / `all` / `blacklist` | 仅白名单 / 全部 / 黑名单排除 |
| `/xbimg kwset <方案ID>` | 全局词库切换 |
| `/xbimg kwupdate [apply\|keep]` | 官方词库更新检测与覆盖 |
| `/xbimg size [群号] <50-500>` | 全局或指定群字体（`100` 恢复默认） |
| `/xbimg test [文本]` | 生成测试效果图 |

## 单群指令（`/xbimg group`，仅管理员，群内执行或追加工号异地操作）

| 指令 | 说明 |
| :--- | :--- |
| `/xbimg group` | 本群专属配置卡片与指南（可附群号查看异地群） |
| `/xbimg group set <50-500> [群号]` | 单群字体大小 |
| `/xbimg group style ios` / `android16` / `default` `[群号]` | 单群风格（`default` 恢复跟随全局） |
| `/xbimg group theme light` / `dark` / `default` `[群号]` | 单群主题（`default` 恢复跟随全局） |
| `/xbimg group kw <方案ID>` / `default` `[群号]` | 单群绑定敏感词方案 |
| `/xbimg group ttf <字体id> [群号]` | 单群精选字体切换 |
| `/xbimg group list` | 可用字体与词库列表 |
| `/xbimg group reset [群号]` | 单群全部恢复跟随全局 |
| `/xbimg group test [群号]` | 单群专属效果测试图 |

## 依赖与字体

- `Pillow >= 9.1.0`、`httpx >= 0.24.0`、`numpy >= 1.22`；
- Windows 开箱即用（微软雅黑 / 思源）；Linux 请安装 `fonts-noto-cjk` 或 `wqy-microhei`，或把字体文件放入 `assets/fonts/`，或在 WebUI 精选字体中一键下载。

## 备注

- 私聊默认生效；群聊受 `group_mode + group_list` 控制。
- `violation_only` 模式下普通消息保持纯文本，仅违规转图 / 打码 / 拦截。
- 渲染失败自动回落原文，不影响正常发送。

## 更新日志

见 [changelog.md](./changelog.md)。
