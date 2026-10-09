# 消息转图助手 v1.3.23

> 将机器人发出的纯文本消息渲染为高品质图片，附内容审查与 Web 控制台。

## 简介

机器人发出的文本消息可在本地渲染为图片发送，支持 Markdown 排版与多种主题；渲染失败自动回落原文，不影响正常发送。

本插件基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 开发。AstrBot 是一个松耦合、异步、支持多消息平台部署，具有易用的插件系统和完善的大语言模型（LLM）接入功能的聊天机器人及开发框架，使用文档见 [docs.astrbot.app](https://docs.astrbot.app)。

- 插件 ID：`astrbot_plugin_xbimg`
- 当前版本：`v1.3.23`
- 运行要求：AstrBot `>=3.4.0`，平台 `aiocqhttp`
- 仓库：https://github.com/imsuperone/xbimg

## 功能

- 双主题：iOS 磨砂玻璃风格与 Android 16 胶囊卡片风格。
- 星空背景：随机十字星、星芒与光晕粒子，密度可调。
- 画质档位：原画、均衡、极小三档可选。
- 链接策略：遇 URL 转图、保留纯文本或附提取后的纯文本链接。
- 敏感词审查：多套违规词方案按群绑定，支持 AI 双轨审查。
- 违规处置：打码、拦截、警示卡片，打码样式与位置可调。
- Web 控制台：全屏仪表盘与实时文本渲染测试台。

## 安装

1. AstrBot 插件市场搜索「消息转图助手」安装，或执行 `git clone https://github.com/imsuperone/xbimg.git data/plugins/astrbot_plugin_xbimg`；
2. 重启 AstrBot，在后台「消息转图」页面进行可视化配置。

## 指令

全局管理（`/xbimg`，仅管理员）：

| 指令 | 说明 |
| :--- | :--- |
| `/xbimg` | 控制台状态与完整菜单 |
| `/xbimg on` / `off` | 总开关 |
| `/xbimg trigger always` / `violation` | 始终转图 / 仅违规时转图 |
| `/xbimg minlen <1-1000>` | 最小触发字数 |
| `/xbimg style ios` / `android16` | 全局视觉风格 |
| `/xbimg theme light` / `dark` | 全局浅色 / 深色 |
| `/xbimg star on` / `off`、`density sparse\|medium\|dense` | 星空背景与密度 |
| `/xbimg quality lossless` / `balanced` / `compact` | 原画 / 均衡 / 极小 |
| `/xbimg link image` / `text` / `append` | 链接转图 / 保持文本 / 转图附链接 |
| `/xbimg mod on` / `off`、`kwset <方案ID>` | 审查开关与词库切换 |
| `/xbimg action half\|full\|block\|notice` | 违规处置动作 |
| `/xbimg mosaic pixel` / `blur`、`mosaicpos bottom\|top\|random` | 打码样式与位置 |
| `/xbimg groupmode whitelist` / `all` / `blacklist` | 生效模式 |
| `/xbimg size [群号] <50-500>` | 字体大小（`100` 恢复默认） |
| `/xbimg test [文本]` | 生成测试效果图 |

单群配置（`/xbimg group`，仅管理员，可附群号异地操作）：

| 指令 | 说明 |
| :--- | :--- |
| `/xbimg group` | 本群配置卡片与指南 |
| `/xbimg group set <50-500> [群号]` | 单群字体大小 |
| `/xbimg group style / theme / kw ... [群号]` | 单群风格、主题、词库（`default` 跟随全局） |
| `/xbimg group ttf <字体id> [群号]` | 单群精选字体 |
| `/xbimg group list` / `reset [群号]` / `test [群号]` | 列表 / 恢复跟随全局 / 效果测试 |

## 说明

- 依赖 `Pillow >= 9.1.0`、`httpx >= 0.24.0`、`numpy >= 1.22`。
- Windows 自带字体开箱即用；Linux 安装 `fonts-noto-cjk`，或在 WebUI 精选字体中一键下载。
- 私聊默认生效；群聊受 `group_mode + group_list` 控制。
- `violation_only` 模式下普通消息保持纯文本，仅违规转图、打码或拦截。
