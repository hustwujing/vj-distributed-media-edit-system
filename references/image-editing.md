# 图片编辑能力

图片是项目的一等素材。Agent 可以阅读当前项目的 PNG/JPG/WebP，先提出方案或发送样式预览；用户确认后可使用 `/opt/video-use/helpers/image_edit.py` 生成新的图片版本，也可以编写项目内 Python/Pillow/OpenCV 脚本实现更特殊的效果。

## 统一流程

1. 读取 `assets.json`，确认图片尺寸、透明度和素材 ID。
2. 讨论自然语言需求，明确哪些区域必须保留。
3. 用户要求先看样式时，使用样式预览目录生成 1–3 张 PNG；此阶段不改原图。
4. 确认后生成 `edit/image-plan.json`，先 `validate` 再 `render`。
5. 生成前后对比图，查看文字是否缺字、边缘是否破损、透明度是否正确、主体是否被意外修改。
6. 每次输出使用新文件名并写入 `edit/deliverables.json`，原图永远只读。

## 本地计划支持

```bash
python /opt/video-use/helpers/image_edit.py capabilities
python /opt/video-use/helpers/image_edit.py validate edit/image-plan.json --workspace .
python /opt/video-use/helpers/image_edit.py render edit/image-plan.json --workspace . -o edit/image-v1.png
```

支持 `resize`、`crop`、`rotate`、`flip`、`background`、`adjust`、`blur`、`pixelate`、`mask`、`text`、`overlay` 和格式导出。脚本默认使用 Pillow，图片计划不调用模型；复杂需求可以在其基础上写 Python。

## AI 能力与可视化画布

已接入 Qwen图片编辑、Qwen图片分层、SAM2.1点选/框选、BiRefNet抠图、PaddleOCR识别和局部改字。详情、参数和脚本扩展见 [image-workbench.md](image-workbench.md)。重打光、妆容迁移和超分服务仍是独立项目，本工作台尚未接入这些入口。

选中图层后可以直接进入聊天修改；图层锁定、版本校验、脚本结果回写与前后对比见 [canvas-editing.md](canvas-editing.md)。这类任务通过 `/opt/canvas_edit.py` 应用到实际画布，不能只交付独立预览图。

不需要模型的精细能力包括调整图层、混合模式、剪贴蒙版、蒙版形态学精修、文字描边阴影与自动换行、参考线吸附和对齐分布、透视校正、商品构图、白平衡/色阶/色彩匹配以及批量规格导出。网页与 Agent 参数见 [image-workbench.md](image-workbench.md)。

## 图片计划示例

```json
{
  "version": 1,
  "sources": {"main": "sources/photo.png", "badge": "sources/logo.png"},
  "operations": [
    {"type":"resize", "width":1200, "height":900},
    {"type":"adjust", "brightness":1.03, "contrast":1.08, "saturation":1.04},
    {"type":"text", "text":"春季新品", "x":60, "y":60, "size":56, "color":"#FFFFFF"},
    {"type":"overlay", "source":"sources/logo.png", "x":1020, "y":40, "opacity":0.9}
  ],
  "output": {"format":"png"}
}
```

输入路径、输出路径和叠加图必须属于当前工作区；不允许覆盖输入文件或使用用户提供的任意滤镜命令。

## 保持原样式的文字替换

OCR 框是近似定位范围，不是字体字号，也未必覆盖旧字的全部抗锯齿边缘。保留原排版时，量测原字实际墨迹包围盒、字重和基线；对候选字体实际栅格化后选择字号，不能将框高直接作为字号或横向拉伸字形。替换范围应包含经像素确认的旧字边缘，在方案中声明必要的小幅留量，并检查该范围以外的像素不变。若用户明确限定的边界与清除旧字冲突，先说明冲突并请求范围调整。最终必须同时看整行观感和重新读取输出后的字形量测，不能仅检查新文字是否出现。
