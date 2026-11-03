# 固定媒体 operation 与本地 helper

这些是两种调用入口，不是两套媒体效果。不要混用参数格式。能力目录中 `media.edit.parameters.edit_plan` 是自由对象；存在 schema 不代表其所有嵌套字段已列出或静态校验已覆盖媒体输入。

## media.edit 工作流节点

- 每个 `parameters.edit_plan.clips` 元素必须有 `asset`，指向本节点已声明的输入角色或素材 ID。
- 工作流运行时将角色转换为受控素材 ID，再由 `bind_plan` 转成内部 helper 的 source 别名。不要提交 `sources`、`transcripts` 路径映射，也不要在 clips 或 edit_plan 里猜测 `transcript` 字段。
- 使用 `words` 或 `subtitles.from_transcripts` 时，转写 JSON 也必须显式绑定。单一素材可使用 `inputs.transcript`；多个素材用 `inputs.transcript_ROLE` 与对应视频角色配对。
- `words` 与 `start/end` 互斥；完整编辑字段与媒体约束见 `references/edit-plan.md`，其中 helper 的 `source` 在此入口替换为 `asset`。
- 以前存在于工作区的转写不会自动成为固定节点输入。若已有转写只有工作区路径，可在方案中选择读取已有数据的受限 script/helper；或先由获准步骤产出转写 JSON，再将该角色显式传给 media.edit。不能为修复绑定错误擅自发起新转写。

输入示意（占位符必须替换为已确认的真实引用）：

```json
{
  "inputs": {
    "main": {"asset": "VIDEO_ASSET_ID"},
    "transcript_main": {"step": "existing_transcript_output", "role": "transcript"}
  },
  "depends_on": ["existing_transcript_output"],
  "parameters": {
    "edit_plan": {
      "version": 1,
      "clips": [{"asset": "main", "words": [0, 3], "padding": 0.08}],
      "subtitles": {"from_transcripts": true, "words_per_line": 4}
    }
  }
}
```

这只是参数/输入片段，不是可直接提交的完整步骤或完整计划。未知字段与缺失输入仍可能在运行时被拒绝；修复时应先核对绑定证据，不能反复猜字段。

## helpers/edit_media.py

本地 helper 接受 `sources` 与 `transcripts` 路径映射、`clips.source` 别名，读取当前获准工作区；具体参数见 `references/local-editing.md` 和 `references/edit-plan.md`。调用现有 helper 仍属于 `script` 路径，确认前声明正确执行意图；工作流 script 按其输入、输出目录和产物契约执行。

## 中间格式与验收

将尺寸、编码、颜色模式等要求施加到负责实现该要求的步骤。例如后续步骤会转换 RGB，前面的合法 RGBA 中间图不应仅因含 Alpha 被拒绝，除非用户或明确技术依赖要求此阶段必须 RGB。

`has_alpha` 检查通道是否存在；Alpha 全为255的 RGBA 图也有 Alpha 通道。检查“不透明”应核对 Alpha 像素或透明比例，不能将其等同于 `has_alpha=false`。已确认验收不能在执行中降低，发现计划条件不合理仍走现有用户决策流程。
