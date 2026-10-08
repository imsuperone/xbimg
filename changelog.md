# 更新日志

## v1.3.20

- ✨ **实时预览打码独立开关**：预览页新增「预览打码：不打码 / 半字遮蔽 / 全文遮蔽」分段控件，默认**不打码**。原先 `resolvePreviewMosaicMode()` 在未显式选择时回退读取正式配置 `violation_action`（默认 `mosaic_half`），导致测试台无论怎么改字都一直打码。现预览打码只由本页开关决定，与正式配置完全解耦——预览只是浏览测试，不该被生产策略牵着走。
- ✨ **切换开关即重渲染**：已有预览图时切分段立即重新生成，无需再点「生成预览」；该选择为临时浏览态，不写入配置（跳过 `triggerAutoSave`）。
- ♻️ **删除失效的模板 pin 机制**：`_activeMosaicMode` / `_mosaicUserPinned` 及「手动改字清除 pin」分支随开关显式化一并移除，`applyPreset` 改为直接设置分段值（「半字遮蔽效果」模板仍按 `violation_action` 自动选 half/full，其余模板还原为不打码）。
- 📄 后端无需改动：`/preview` 已由 payload 的 `mosaic_mode` 驱动，传 `none` 即完全不打码、不检索违规词。
