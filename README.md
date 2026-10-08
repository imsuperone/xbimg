# 消息转图助手（astrbot_plugin_xbimg）

## 版本公告

当前版本：**v1.3.20**（要求 AstrBot `>=3.4.0`，平台 `aiocqhttp`）

本插件用于将机器人发送的纯文本消息自动渲染为高品质图片，并提供内容安全审查与 Web 可视化控制台。

完整变更记录见 [changelog.md](./changelog.md)。项目主页：https://github.com/imsuperone/xbimg

---

## 功能说明

- **双主题视觉**：iOS 磨砂玻璃质感、Apple 连续曲率圆角、多层投影，浅色 / 深色双主题；Android 16（Material 3 Expressive）胶囊容器、大圆角排版。
- **Markdown 排版**：标题、列表、引用、代码块（含高亮）、链接、Emoji 混排自动美化。
- **星空背景**：背景四周随机散落十字星芒与微光粒子，卡片内无噪点，`sparse / medium / dense` 三档密度。
- **体积优化**：均衡 JPEG Q94 / 极小 JPEG Q86 / 原画无损 PNG 三档，全程 4:4:4 无抽样，缓存 90 秒自动清理；发送前超限图片自动降质（`img_send_shrink_kb`，默认 250KB）。
- **链接策略**：`as_image` 直接转图 / `keep_text` 含链接保持纯文本可点击 / `extract_append` 转图后附带纯文本直达链接（自动剥离中英文尾部标点）。
- **敏感词审查**：6 套内置词库，支持空格混淆命中，可全局切换与单群绑定。
- **违规处置**：`half` 打一半马赛克 / `full` 全文打码 / `block` 彻底拦截 / `notice` 替换为合规警示卡片；马赛克支持 `pixel` 像素块 / `blur` 高斯模糊，半码位置 `bottom / top / random`，字级精准打码。
- **群组精细化**：`whitelist / all / blacklist` 三种生效模式，单群可独立设置字体、风格、主题、词库、精选字体。
- **字体与 Emoji 管理**：6 款精选中文字体（思源黑体 / 思源宋体 / 霞鹜文楷 / 站酷快乐体 / 马善政毛笔 / macOS 苹方）一键下载卸载无锁定；`ios / android / windows / none` 四种 Emoji 样式按需下载，支持云端补全与本地缓存。
- **WebUI 控制台**：AstrBot 后台单列全屏仪表盘，含实时文本渲染测试台、字体 / Emoji / 词库管理，手机电脑自适应，修改即自动保存。测试台的打码由预览页「不打码 / 半字遮蔽 / 全文遮蔽」独立开关控制，与正式违规处置配置解耦（默认不打码）。
- **高优先级无损拦截**：`on_decorating_result(priority=99999)` 与适配器 `send_group_msg / call_action` 双钩子，保留 At / Reply / 图片音视频段，与 `xbbot` 等业务插件兼容。

---

## 安装说明

1. 在 AstrBot 插件市场搜索 `消息转图助手` 安装，或手动克隆：
   ```bash
   git clone https://github.com/imsuperone/xbimg.git data/plugins/astrbot_plugin_xbimg
   ```
2. 重启 AstrBot，插件自动加载。
3. 聊天发送 `/xbimg` 查看控制台，在 AstrBot 后台打开 `消息转图` 页面进行可视化配置。

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

---

## 架构与功能接口梳理

按代码实际实现汇总：**一个功能只认一个实现入口**（v1.3.19 已完成冗余收敛，历史结论与不改项理由见「冗余与层次审查」）。

### 分层

```
main.py            装配层：Msg2ImgPlugin(Star + Groups/Handlers/Commands/WebApi 四个 mixin)
  ├─ core/common.py      跨模块常量与小工具（URL 清洗、压缩档位、信号量、RENDER_MAX_CHARS）
  ├─ core/config.py      ConfigManager：配置真源 + 统计记账 + 原子落盘
  ├─ core/moderation.py  ContentModerator：关键词预检 + AI 全量审查
  ├─ core/renderer.py    MessageImageRenderer：排版 / 分页 / 绘制 / 马赛克 / 字体与 emoji
  ├─ core/groups.py      群黑白名单、单群配置读取
  ├─ core/handlers.py    消息钩子 + 共用渲染管线 + 适配器补丁 + 落盘清扫
  ├─ core/commands.py    `/xbimg` 聊天指令
  └─ core/webapi.py      29 个 WebUI 端点
```

### 功能 → 唯一实现

| 功能 | 唯一实现 | 调用方 |
| :--- | :--- | :--- |
| 入站消息登记群名 | `on_all_message_entry` | AstrBot 消息事件 |
| 文本转图（主路径） | `on_decorating_result` → `_pipeline_text_to_images` | AstrBot 装饰钩子 |
| 文本转图（OneBot 适配器路径） | `_transform_onebot_message` → 同上管线 | `send_group_msg` / `call_action` 补丁 |
| 渲染管线（认领/复用/审查/渲染/落盘/记账） | `_pipeline_text_to_images` | 上述两个入口 |
| 三入口准入判断（字数 / keep_text / violation_only 预检） | `_should_transform(full_text, gid)` | list 分支、str 分支、`on_decorating_result` |
| 同文终态复用判断（blocked / ready / 源源重跑） | `_reuse_prior(full_text)` | 管线内三处调用点 |
| 内容审查 | `ContentModerator.review`（全量）、`check_keywords`（预检） | 管线 |
| 渲染出图 | `MessageImageRenderer.render_pages` → `_prepare_layout` + `_split_content_pages` + `_draw_page` | 管线、WebUI 预览 |
| 单图便捷入口 | `render()`（`render_pages()[0]`） | WebUI 预览台 |
| 链接块文案产出 | `_link_append_text` | 三入口输出组装、链接块识别 |
| 链接有无判断 | `_has_link` | `_should_transform` |
| 参数归一化（text/style/theme/mosaic/font/emoji） | `_norm_cache_params` | `_prepare_layout`、缓存键前置 |
| 群是否生效 | `_is_gid_allowed(gid)`（event 级入口 `_is_group_allowed` 取群号后调它） | 主钩子 |
| 单群配置 | `_get_group_custom_config` / `_get_group_font_scale` | 管线、指令 |
| 配置读写 | `ConfigManager.config` / `save()` | 全部模块 |
| 统计记账 | `record_render` / `get_stats` | 管线 |
| 聊天指令 | `CommandsMixin.cmd_xbimg` | `/xbimg` |
| 落盘图片 | `_save_render_images`（90 秒即时清理 + 周期清扫） | 管线 |
| 缓存图片段组装 | `_image_segs_from_paths` | 三路径输出 |
| 缓存命中校验 | `_alive_prior_paths`（文件↔内存双向一致） | 管线 |

WebUI 端点（`webapi.py`，均以 `/{插件名}` 为前缀）：`config`(GET/POST)、`stats`、`groups`、`groups/fetch`、`preview`、`preview_img`、`reset`、`fonts/{status,download,files,delete}`、`fonts/curated{,_install,_delete}`、`emoji/{packs,cdn_check,download,download_async,download_status(×2),delete}`、`fonts/curated_install{,_async,_status(×2)}`、`presets/{update_status,apply_update,dismiss_update}`。

### 冗余与层次审查（v1.3.19 已收敛）

**已收敛（一个功能一个实现）**

1. **三入口准入判断 → `_should_transform`**：`min_length_threshold` 解析、`link_mode==keep_text` 放行、`violation_only` 关键词预检原在 list 分支、str 分支、`on_decorating_result` 各写一遍（约 20 行 ×3），且三处需同步维护顺序。现合并为 `_should_transform(full_text, gid) -> (render_trigger, grp_custom) | None`，通过返回供管线直用，不通过返回 `None` 由调用方原样放行。入口特有的 cmd 回执跳过、链接块识别、媒体占比检查留在调用方——它们才是三入口真正的差异。
2. **链接判断 → `_has_link`**：`_clean_urls(_URL_PATTERN.findall(text))` 的「判断」用途收敛为 `_has_link(text) -> bool`；`_link_append_text` 保留 findall（文案产出需要 URL 列表本身，不是重复）。
3. **群过滤 → 复用 `_is_gid_allowed`**：`_is_group_allowed(event)` 原把 `whitelist/all/blacklist` 分支完整重写一遍只为了加日志；现取群号后直接调 `_is_gid_allowed`，模式分支只留一处，日志保留 mode 与结论。
4. **参数归一化 → `_norm_cache_params` 唯一实现**：`_prepare_layout` 原是它的逐字副本，靠注释「与 _norm_cache_params 一致」维系，规则改动只改一边即缓存键与实际排版失配。现 `_prepare_layout` 直接调它。
5. **同文复用三段式 → `_reuse_prior`**：`_pipeline_text_to_images` 头部原是三段几乎逐字相同的「blocked 放行 / 存活路径 ready / 否则重跑」，现合并为一个返回 `("blocked", [])` / `("ready", paths)` / `None` 的 helper，管线三处调用。
6. **绘制参数并入 ctx**：`_draw_page` 原收 10 个形参，其中 `mosaic_half_pos`/`emoji_style` 本就在 ctx 里被取出来又传回去，其余绘制期参数（`star_*`、`mosaic_*`、`violation_words`、`perf_out`）由 `_render_pages_inner` 逐个抄送。现签名收敛为 `(ctx, page_lines, page_idx, total_pages)`，绘制参数在排版完成后一次性 `ctx.update(...)` 注入。ctx 每次调用都是新 dict（命中取 `dict(_lhit)` 副本、未命中以 `dict(ctx)` 入缓存），注入不污染 `_LAYOUT_CACHE`。

**复核后判定为非问题（原文档误报，已改正）**

- ~~状态查询 query/path 双版本~~：`download_status` 与 `download_status/<job_id>` 只是同一个 `_download_status_for(job_id)` 的两行路由薄壳，前端 `pollDownloadJob` 走 path 版。路由层各一行不算双实现。
- ~~单复数落盘两接口~~：`_save_render_images` 是 `_save_render_image` 的多图循环，属正常的「单件/批量」分层；外部调用方只见复数版（单图也走它，内部短路）。

**评估后不改（记录理由，避免反复重议）**

- **同文本重复哈希**：一次管线内 `_text_hash` 确实算 4～6 次，但 sha256 对 ≤12KB 文本是数十微秒量级，4～6 次合计约 0.15ms，相对渲染（100ms+）不可测。给 4 个已稳定的 slot 助手加 `key=` 形参反而增接口面。**不改。**
- **`perf_out` 字典透传**：零开销 out-param 设计（`None` 时每层 `if` 短路，无分配无锁），是显式的取舍而非隐藏成本；改为 contextvar/模块全局会让回填点与读取点失去可追溯性。绘制层已因上文第 6 项少抄一层。**保留。**
- **同文两份缓存**：`_RENDER_CACHE`（成品图）与 `_LAYOUT_CACHE`（排版 ctx）是有意的两级缓存——换页/改马赛克时只重画不重排。代价是双份内存与双份键计算，收益是 WebUI 换页预览不必重排。**保留为设计取舍。**
- **handlers.py / renderer.py 拆文件**：补丁与落盘、字体/emoji IO 与排版绘制确实是两类职责，但两者都是热路径相邻代码，拆分要移动大量被测试直接引用的成员（`test_plugin.py` 经 plugin 实例访问），风险与收益不匹配。**另开任务按需做。**
- **`render_pages` 与 `_render_pages_inner` 参数抄送**：测试直接调 `_render_pages_inner`，合入需改测试签名，收益低。**保留。**

以上为审查结论；已收敛项的逐条说明见 v1.3.19 提交记录（changelog 按惯例只保留最新版）。

---

## 依赖与字体

- `Pillow >= 9.1.0`、`httpx >= 0.24.0`、`numpy >= 1.22`
- Windows 开箱即用（微软雅黑 / 思源）；Linux 请安装 `fonts-noto-cjk` 或 `wqy-microhei`，或把 `.ttf/.ttc/.otf` 放入 `assets/fonts/`，或在 WebUI 精选字体中一键下载。
- 测试：`python test_plugin.py`（渲染 / 审查 / 配置 / Emoji / 字体全量用例）。

## 备注

- 私聊默认生效；群聊受 `group_mode + group_list` 控制。
- `violation_only` 模式下普通消息保持纯文本，仅违规转图 / 打码 / 拦截。
- 渲染失败自动回落原文，不影响正常发送。
