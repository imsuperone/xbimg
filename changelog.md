# 更新日志 (Changelog)

## v1.3.31 (2026-10-13)

- **完整恢复并加固发送前体积缩图与超时送达核验链路**：发送前自动按 `send_shrink_max_mb` 对超体积本地图片与 `base64://` 图片执行 JPEG 压缩降级；当发送快速报错（`<4.5s`）时自动剥离失效 `reply` 引用段重发；当发送超时（`>=4.5s` 或 `retcode=1200`）时通过 `get_group_msg_history` 真实核验是否送达，未送达时剥离引用自动补发一次，彻底兼顾防重发两张图与防漏发。
- **隔离未启用群聊与非转图 API**：未启用 `xbimg` 的群聊以及 `send_group_forward_msg` 等非本插件转图动作直接透传原方法，不再进入 `xbimg` 发送守卫。
- **清理冗余代码与跳过 WebChat/流式帧**：移除前端无用遗留函数 `getGroupScales`，并在 WebChat 聊天端及 `STREAMING_FINISH` 阶段跳过文本转图拦截。
