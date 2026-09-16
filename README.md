# astrbot_plugin_msg2img (消息转图助手)

🎨 一个精致美观、功能强大的 AstrBot 消息文本转图片（Text-to-Image）渲染插件。

- 📦 项目主页：https://github.com/imsuperone/textimg

---

## 🌟 核心特性

- 🍏 **iOS 风格**：高质感磨砂玻璃效果、Apple 连续曲率圆角（Squircle）、细腻多层投影。
- 🤖 **Android 16 风格**：Material 3 Expressive 胶囊容器、大胆圆角排版与动态色调。
- ✨ **随机星空背景**：动态在背景四周随机生成闪烁星芒、十字星、光晕微粒与菱形碎星。
- 🔗 **智能链接策略**：支持链接转图、包含链接保持纯文本发送、或转图并附带提取纯文本直达链接。
- 🛡️ **双轨安全审查**：内置屏蔽词库过滤与外接大模型 AI（OpenAI/DeepSeek 兼容）双重审查机制。
- 🎭 **违规半马赛克（Half Mosaic）**：命中敏感内容时，保留上半部分正常阅读，下半段自动施加真实像素块或高斯毛玻璃打码，并打上警示封条。
- 🔤 **字体自动补齐与自定义**：缺中文字体时自动从官方下载 Noto Sans SC 到插件数据目录；支持自定义字体文件/直链；WebUI 可查看已安装字体并随时删除。
- 😀 **Emoji 全自动**：复杂表情全彩图、单个表情优先 Noto 全彩字体，缺失自动云端补全并缓存（Twemoji CC-BY 4.0），零配置永不崩溃。
- 📱 **Android 16 WebUI 控制台**：配备 Hero 统计卡片、M3 分段按钮、深浅主题切换及实时交互预览台。

---

## 🚀 聊天指令

| 指令 | 说明 |
| :--- | :--- |
| `/msg2img` | 查看当前转图助手状态与功能菜单 |
| `/msg2img on` / `/msg2img off` | 快速开启或暂停消息转图功能 |
| `/msg2img style ios` | 切换为 iOS 磨砂玻璃风格 |
| `/msg2img style android16` | 切换为 Android 16 (M3 Expressive) 风格 |
| `/msg2img star on` / `off` | 开启或关闭背景随机小星星 |
| `/msg2img test [文本]` | 立即生成一张测试效果图片 |

---

## 📦 依赖环境

- `Pillow >= 9.1.0`
- `httpx >= 0.24.0`
- `numpy >= 1.22`

> 中文字体：Windows 开箱即用（微软雅黑/思源）；Linux 服务器请安装 `fonts-noto-cjk` 或 `wqy-microhei`，
> 或把 `.ttf/.ttc/.otf` 字体文件放入 `assets/fonts/` 目录，插件会自动优先加载。
