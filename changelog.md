# 更新日志 (Changelog)

## v1.3.30 (2026-10-13)

- **真实送达校验与漏发重发兜底**：当 OneBot/NapCat 发送图片返回超时（`retcode=1200` / `timeout`）时，自动通过 `get_group_msg_history` 真实核验群内是否已发出该图片——若已送达则静默跳过防重发两张图；若确认未送达则剥离失效引用段自动重试补发，解决图片未发出却被误判为「已实际送达」的问题。
- **隔离未启用群聊与非转图 API**：未启用 `xbimg` 的群聊以及 `send_group_forward_msg` 等非本插件转图动作直接透传原方法，不再进入 `xbimg` 发送守卫。
- **跳过 WebChat 聊天端与流式结束帧**：避免在 AstrBot WebChat 聊天端及 `STREAMING_FINISH` 阶段误拦截文本转图。
