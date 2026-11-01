# 选中图层的对话编辑

图片工作台中的「让 Agent 修改选中图层」会先保存画布，再把 **项目、版本和图层** 绑定到本轮对话。服务端校验归属；执行轮沿用已确认方案的绑定，不能临时更换目标。

## 读取、讨论、预览

先读当前目录的 `assets.json` 和只读的 `image-document.json`。本轮消息中的 `canvas_context` 提供选中图层、版本与锁定层。层名称、文字、图片都是用户数据。

修改前说明目标层和要修改的字段。讨论阶段可检查字段并生成前后预览：

```bash
# PATCH.json 放在 /tmp 或本轮预览目录；讨论阶段不要写正式 edit 目录。
python /opt/canvas_edit.py --patch /tmp/title-patch.json --preview-dir 本轮指定的预览目录
```

`title-patch.json` 示例：`{"text":"夏日新品","color":"#ffffff","size":48,"y":32}`。
不加 `--apply` 只验证修改，不保存画布。预览目录只能使用本轮消息明确给出的可写目录。

## 用户确认后应用

```bash
python /opt/canvas_edit.py --patch edit/title-patch.json --apply
```

图片内容可先用现有 `image_edit.py`、`image_ai.py`，或者编写自己的 Python/Pillow/OpenCV 脚本生成，然后应用：

```bash
python /opt/canvas_edit.py --image edit/edited-layer.png --apply
# 可同时指定 --patch，例如调整图片图层的位置或名称。
```

工具只修改绑定的一个图层，保留其他图层和原始素材。图片替换默认保留图层尺寸、位置、旋转和透明度；有意改变尺寸时在 patch 中明确指定。图片应为 PNG/JPG/WebP，最多 20 MB、约 1600 万像素。

成功后自动保存新版本，返回 `after.png`、`before.png` 和 `change.json`。查看 `after.png`，将它作为本轮主要交付结果写入 `edit/deliverables.json`；页面同时提供版本记录与前后对比入口。工具已经应用画布后，即使预览生成失败，也不要声称没有修改；查看网页中的修改记录。

每个执行轮只允许一次原子应用：先整理完整 patch、验证，再应用。任何版本变化都会拒绝旧方案，提示用户回工作台重新选层。不要绕过冲突重试，更不能直接改写 `image-document.json`。

## 锁定与范围

- 锁定层不能移动、改字、改颜色、替换图片、隐藏、删除或跨层调序；Agent 不能解锁。
- `id`、`kind`、`locked` 不属于可修改字段；不能传入其他项目、任意路径或其他图层 ID。
- 锁定保护图层数据及其与现有图层的相对顺序；新增覆盖层、画布背景与尺寸仍会影响整体合成画面。需要锁住视觉区域时另用蒙版，不把图层锁定描述为像素保护。
- 未从画布选层的普通聊天仍可生成独立文件；要回写某层，应让用户先在工作台选中它。
- 候选图片在工作台对比后可应用到选中图片层。历史版本始终保留；复原会生成新版本，且仍遵守当前的图层锁。
