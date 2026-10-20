# 更新日志 (Changelog)

## v1.3.32 (2026-10-21)

- **彻底终结超时/网络延迟触发重发两张图问题**：
  - 精准识别与接管 OneBot NTEvent 发送超时（`retcode=1200`、`NodeIKernelMsgService` 及各类 Socket/HTTP timeout 异常），识别腾讯后台 NTQQ 实际已接管投递的状态；
  - 剔除因 NTQQ 本地历史消息落盘存在延迟而误判为“未送达”进而触发重发原消息的二次重发分支；
  - 守卫捕获超时后向外层优雅返回状态良好的响应字典（`status: ok`），彻底杜绝向上层抛出异常导致 `xbnext` / AstrBot 框架降级触发原消息回退重发，确保任何高延迟或超时场景下图片绝不再发两张。

## v1.3.31 (2026-10-13)

- **完整恢复并加固发送前体积缩图与超时送达核验链路**：发送前自动按 `send_shrink_max_mb` 对超体积本地图片与 `base64://` 图片执行 JPEG 压缩降级；当发送快速报错（`<4.5s`）时自动剥离失效 `reply` 引用段重发；当发送超时（`>=4.5s` 或 `retcode=1200`）时通过 `get_group_msg_history` 真实核验是否送达，未送达时剥离引用自动补发一次，彻底兼顾防重发两张图与防漏发。
- **隔离未启用群聊与非转图 API**：未启用 `xbimg` 的群聊以及 `send_group_forward_msg` 等非本插件转图动作直接透传原方法，不再进入 `xbimg` 发送守卫。
- **清理冗余代码与跳过 WebChat/流式帧**：移除前端无用遗留函数 `getGroupScales`，并在 WebChat 聊天端及 `STREAMING_FINISH` 阶段跳过文本转图拦截。
