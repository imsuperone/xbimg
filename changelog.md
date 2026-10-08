# 更新日志

## v1.3.19

- ♻️ **三入口准入收敛为 `_should_transform`**：`_transform_onebot_message` 的 list 分支、str 分支、`on_decorating_result` 原各写一遍「字数门槛 → keep_text 放行 → violation_only 关键词预检」，顺序与判断逻辑三处同步维护。现合并为单一 helper，通过返回 `(render_trigger, grp_custom)`，入口特有的 cmd 回执跳过 / 链接块识别 / 媒体占比检查仍留在各调用方。
- ♻️ **链接判定收敛为 `_has_link`**：keep_text 放行原在三入口各跑一次 `_clean_urls(_URL_PATTERN.findall(...))`，现统一为 `_has_link`；`_link_append_text` 仍保留 findall（文案产出需要 URL 列表）。
- ♻️ **管线三段同文复用块收敛为 `_reuse_prior`**：命中复用、认领失败复用、等待终态后复用原是三段几乎逐字相同的「blocked → ready → 源源重跑」判断，现合并为一个返回 `("blocked" | "ready", ...)` 或 `None` 的 helper。
- ♻️ **群过滤判定收敛为 `_is_gid_allowed`**：`_is_group_allowed(event)` 原自行按 mode 重写一遍白/黑名单判断，与 `_is_gid_allowed` 同口径并存。现改为取群号后复用后者，debug 日志保留 mode 与结论。
- ♻️ **排版参数归一化唯一实现**：`_prepare_layout` 的归一化段（text/style/theme_mode/mosaic_half_pos/font_scale/emoji_style）原是 `_norm_cache_params` 的逐字副本，两套规则漂移会让缓存键与实际排版不一致。现 `_prepare_layout` 直接调 `_norm_cache_params`。
- ♻️ **绘制参数并入 ctx**：`_draw_page` 原收 10 个形参，其中 `mosaic_half_pos`/`emoji_style` 本就在 ctx 里仍被重新传入，其余绘制期参数由 `_render_pages_inner` 逐个抄送。现签名收敛为 `(ctx, page_lines, page_idx, total_pages)`，绘制参数在排版完成后一次性 `ctx.update(...)` 注入（ctx 每次调用均为新 dict，注入不污染 `_LAYOUT_CACHE`）。
- ✅ `python -X utf8 test_plugin.py` 100 tests OK，与 v1.3.18 持平（纯重构，行为等价）。
