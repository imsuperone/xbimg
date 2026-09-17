# astrbot_plugin_xbimg (消息转图助手)

🎨 一个精致美观、功能强大的 AstrBot 消息文本转图片渲染插件。

- 📦 项目主页：https://github.com/imsuperone/xbimg

---

## 🌟 核心特性

- 🍏 **iOS 风格**：高质感磨砂玻璃效果、Apple 连续曲率圆角（Squircle）、细腻多层投影。
- 🤖 **Android 16 风格**：Material 3 Expressive 胶囊容器、大胆圆角排版与动态色调。
- ✨ **优化星空背景**：柔和微晕星芒，边缘散布不形成卡片杂乱噪点。
- 💾 **无损/极小体积**：支持 4:4:4 色度无噪点极速编码，兼具超清与极限省流。
- 🔗 **智能链接策略**：支持链接转图、包含链接保持纯文本发送、或转图并附带提取纯文本直达链接。
- 🛡️ **多套敏感词库与独立 AI 审查**：内置涉毒、涉赌、涉黄、黑产与互动娱乐等多套方案，支持单群绑定。
- 🎭 **违规半马赛克（Half Mosaic）**：命中敏感内容时，保留上半部分正常阅读，下半段自动施加真实像素块或高斯毛玻璃打码，并打上警示封条。
- 🔤 **字体与 Emoji 自由管理**：精选字体一键安装与彻底卸载，Emoji 样式按需下载与清空，零残留不锁盘。
- 📱 **纯正 Android 16 风格 WebUI**：全新单列全屏响应式设计，配备独立交互预览台，手机电脑完美自适应。

---

## 🚀 聊天指令

| 指令 | 说明 |
| :--- | :--- |
| `/xbimg` | 查看当前完整控制台状态与功能菜单 |
| `/xbimg on` / `/xbimg off` | 快速开启或暂停消息转图功能 |
| `/xbimg trigger always` / `violation` | 切换转图触发时机（始终转图 / 仅违规转图） |
| `/xbimg minlen <字数>` | 设定触发转图最小字数门槛 |
| `/xbimg style ios` / `android16` | 切换全局视觉风格 |
| `/xbimg theme light` / `dark` | 切换全局浅色/深色主题 |
| `/xbimg star on` / `off` | 开启或关闭背景随机小星星 |
| `/xbimg density sparse` / `medium` / `dense` | 调节背景小星星密度 |
| `/xbimg quality low` / `medium` / `high` | 切换输出文件体积优化档位 |
| `/xbimg mod on` / `off` | 开启或关闭敏感屏蔽词过滤 |
| `/xbimg ai on` / `off` | 开启或关闭独立 AI 大模型审查 |
| `/xbimg action half` / `full` / `block` / `notice` | 设定违规处置动作 |
| `/xbimg size [群号] 50-500` | 设置字体百分比（例如 `/xbimg size 120`） |
| `/xbimg test [文本]` | 立即生成一张测试效果图片 |
| `/text` | 查看当前群专属配置卡片与指令指南 |
| `/text set <50-500>` | 调整当前群专属字体大小 |
| `/text style ios` / `android16` / `default` | 调整当前群风格（`default` 恢复跟随全局） |
| `/text theme light` / `dark` / `default` | 调整当前群主题（`default` 恢复跟随全局） |
| `/text kw <方案ID>` / `default` | 绑定当前群专属敏感词方案 |
| `/text reset` | 一键恢复当前群所有配置跟随全局默认 |
| `/text test` | 发送当前群专属效果测试图 |

---

## 📦 依赖环境

- `Pillow >= 9.1.0`
- `httpx >= 0.24.0`
- `numpy >= 1.22`

> 中文字体：Windows 开箱即用（微软雅黑/思源）；Linux 服务器请安装 `fonts-noto-cjk` 或 `wqy-microhei`，
> 或把 `.ttf/.ttc/.otf` 字体文件放入 `assets/fonts/` 目录，插件会自动优先加载。
