# 图片画布、AI 工具与 Python 扩展

图片工作台 `/images/<project_id>` 与聊天、视频工具共用项目归属校验。首页「图片编辑」创建空白项目；编辑页导航可进入同一项目的图片工作台。

## 已实现的操作

- 画布：透明/纯色背景、尺寸、适应/百分比缩放、原图查看。
- 图层：图片、中文字层、色块；拖动位置、尺寸、旋转、透明度、显隐、顺序和删除。
- 选层对话：保存版本、选中图层后直接进入聊天；Agent 可修改文字与样式，也能将自己的 Python 图片结果应用回该层。规则与脚本见 [canvas-editing.md](canvas-editing.md)。
- 图层锁定：保护图层内容、几何参数和相对顺序；只能在工作台手动解锁，Agent 无权解锁。
- 效果对比：前后滑块、同步缩放、多候选预览及选择应用；画布版本也可对比和恢复。
- 历史：本地撤销/重做；保存项目版本，刷新恢复，旧版本可重新保存为新版本。并发保存检查 revision，冲突返回409，不静默覆盖。
- 选区：点选、框选、涂抹蒙版。坐标从画布映射到源图片；旋转图层须先导出再选区。
- 工具输入：每个工具只显示自己的参数与选择方式。SAM3 提供文字/点/框，透视固定四点，局部重绘/消除/改字提供框/涂抹/已有蒙版，ZIM 只接受已有蒙版。虚线框、四角控制点、点编号、画布坐标与源图坐标持续显示在主画布上。添加点或调整框不会自动发起推理；用户确认后点击“识别并显示选区”只提交一次，任务完成后将真实蒙版作为半透明填充和高亮轮廓覆盖到当前图层。工具栏可以隐藏或显示覆盖层；页面刷新时按图层素材恢复最近一次成功的主体选择蒙版。
- 主体选择：本地 SAM2.1 点/框提示生成蒙版与透明前景。
- 自动抠图：本地 BiRefNet；前景可以载入为图层，另放纯色或上传的背景图。
- 局部重绘/消除：Qwen-Image-Edit 主图与选区参考，生成后按原分辨率蒙版合成。选区外像素保留；选区内生成内容要人工查看。
- OCR：本地 PaddleOCR 中文检测/识别；选择识别文本回填选区和新文字。替换文字使用 OpenCV 局部背景修补＋中文字层栅格化，复杂纹理无法保证完全恢复原背景。
- 图片分层：Qwen-Image-Layered，返回 RGBA 图层、合成图、PSD、ORA与完整包；可一键载入所有生成图层。分层属于模型重建，不等同恢复原设计稿。
- 导出：PNG/JPG/WebP；PSD/ORA保留合成后的像素图层。PSD文字作为像素层导出，应用内部的文字层仍保持可编辑。
- 非破坏性调整：图片层与调整层支持亮度、对比度、饱和度、色温、模糊、锐化和不透明度；图层支持正常、正片叠底、滤色、叠加、变暗、变亮及剪贴到下方图层。
- 排版：画布参考线和吸附、画布对齐、多个可编辑图层等距分布；文字自动换行、左中右对齐、描边与阴影。
- 蒙版精修：阈值、扩张/收缩、羽化、去除小区域和填补孔洞，复用已有 SAM 或用户蒙版。
- 本地生产工具：四点透视/文档校正、自动白平衡、自动色阶、本地图像增强、参考图色彩统计匹配、商品统一构图和自然投影。
- 图片批处理：最多 20 张图片统一尺寸、contain/cover、背景、PNG/JPG/WebP、质量和文字水印；每张图片独立任务、结果和日志。

## Agent 的受控图片工具

先读 `assets.json`，输入素材 ID 必须属于当前项目。只在确认执行阶段调用模型工具：

```bash
python /opt/image_ai.py ASSET_ID --action remove_bg
python /opt/image_ai.py ASSET_ID --action segment --box 100 60 240 300
python /opt/image_ai.py ASSET_ID --action segment --point 180 200
python /opt/image_ai.py ASSET_ID --action segment_sam3 --prompt 'coffee cup' --parameters '{"refine":true,"confidence":0.35}'
python /opt/image_ai.py ASSET_ID --action segment_sam3 --box 100 60 240 300 --parameters '{"refine":true}'
python /opt/image_ai.py ASSET_ID --action refine_zim --mask MASK_ASSET_ID
python /opt/image_ai.py ASSET_ID --action ocr
python /opt/image_ai.py ASSET_ID --action replace_text --box 40 30 300 60 --text '春季新品'
python /opt/image_ai.py ASSET_ID --action local_edit --box 100 60 240 300 --prompt '把杯子改为蓝色'
python /opt/image_ai.py ASSET_ID --action erase --box 100 60 240 300
python /opt/image_ai.py ASSET_ID --action generate --reference-asset REF_ID --prompt '参考图2的颜色，保留图1构图' --quality fast
python /opt/image_ai.py ASSET_ID --action decompose --layer-count 3 --prompt '人物、前景与背景'
python /opt/image_ai.py MASK_ID --action refine_mask --parameters '{"grow":3,"feather":2,"fill_holes":true}'
python /opt/image_ai.py ASSET_ID --action perspective --point 0 0 --point 999 0 --point 999 799 --point 0 799
python /opt/image_ai.py ASSET_ID --action local_adjust --parameters '{"auto_white_balance":true,"auto_levels":true}'
python /opt/image_ai.py ASSET_ID --action color_match --reference-asset REF_ID --parameters '{"strength":0.7}'
python /opt/image_ai.py ASSET_ID --action product_layout --parameters '{"width":1200,"height":1200,"background":"#ffffff","margin":80,"shadow":true}'
```

工具返回本次 `edit/image-<id>/` 下的文件列表。查看图片与报告，选定主结果写入 `edit/deliverables.json`。蒙版可从工作台导入为项目素材，再用 `--mask MASK_ASSET_ID` 传入。不要调用外部服务地址或把模型权重挂到沙箱中。

每轮最多调用 4 次需要模型的图片任务；`refine_mask`、`perspective`、`local_adjust`、`color_match`、`product_layout` 和 `batch_export` 属于本地确定性任务，单轮上限 20 次，不占模型调用次数。

`segment_sam3` 和 `refine_zim` 属于模型任务。SAM3 负责按文字、点或框找到目标，ZIM 根据初始蒙版生成软 Alpha，适合头发、毛绒和半透明边缘。OpenCV `refine_mask` 只负责扩缩、羽化、孔洞与小区域清理。推荐链路为 `SAM3 → ZIM → OpenCV 微调 → 抠图/替换/消除`。

本机 SAM3 已实测英文 `coffee cup` 能正确选择杯子，中文 `咖啡杯` 返回空实例。用户可以全程用中文沟通，但 Agent 调用文字提示时必须翻译为简短英文；手动工具直接使用英文，或使用不依赖语言的点/框提示。ZIM 只处理图片和蒙版，与提示语言无关。

图片方案需要把模型链路拆开说明。以精细替换为例，应分别写明：SAM3 如何用英文目标词/点/框产生初始蒙版；ZIM 如何生成软 Alpha；OpenCV 是否扩缩或羽化；Qwen 图片编辑如何只生成选区内容；最后如何按原分辨率蒙版合成并比较选区外像素。每一步都列出预期文件（如 `mask.png`、`foreground.png`、预览图和最终图片）、检查方法与回退策略。

## 自己编写 Python

Agent 保留完整的沙箱 Python 文件读写与执行能力；固定工具没覆盖的效果，保存为 `edit/<功能>.py`，使用 Pillow/numpy 或已安装包处理。Python脚本、输入、参数与最终结果应一起保留以便复现。

```python
from pathlib import Path
from PIL import Image, ImageOps
source = Path('sources/从assets.json读取的图片.png')
with Image.open(source) as image:
    result = ImageOps.exif_transpose(image).convert('RGBA')
    # 在这里组合工具未覆盖的效果，例如特殊拼贴、局部颜色映射或自定义排版。
    result.save('edit/custom-v1.png')
```

源图只读。讨论阶段样式图仅写本轮 `edit/previews/<turn_id>`；正式编辑输出写 `edit/` 新文件名。必须查看输出图片，尤其检查字体、透明边缘、选区内外、尺寸和主体完整性。需要生成式工具时使用受控图片接口，常规精确排版由 Python 完成。

## 执行边界

网页任务通过合法的素材 ID 引用输入，不接受任意路径、Python代码或工作流 JSON。Agent 自定义 Python 在原有 OS 沙箱执行。模型任务由可信固定代码在独立、降权进程执行，共用 GPU 文件租约；独立服务会话只属于本次任务，取消会传递给远端任务。

模型输出不直接写入 Agent 可控制的宿主路径。宿主先写入临时任务区，再把有大小上限的结果返回给沙箱内客户端；客户端以非 root 用户写文件。所有预览、模型结果、导入、版本和下载接口均验证项目归属。

画布结构与导出在 `webapp/image_document.py`，共享像素渲染在 `helpers/canvas_render.py`，版本和图层保护在 `webapp/image_canvas.py`。任务路由在 `webapp/image_tasks.py`，模型适配在 `webapp/image_providers.py`、`webapp/image_models.py`。界面使用原生 Canvas 与统一图层结构，不引入远端运行时资源。
