"""Self-contained requests for chat examples that are expected to execute."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path

from .paths import data_root


REQUIREMENTS = {
    'agent-portrait': {
        'prompt': '请由代码编辑师把这段完整9秒对白制作成720×1280、24fps竖屏视频，完整保留人物画面，用原视频放大模糊填满背景；底部全程添加中文标题“寻找旅伴”，保留原声音，只交付portrait.mp4。代码编辑Harness和模型以任务设置为准，不限定供应商。先给方案，等我确认后执行。',
        'options': ['输出：portrait.mp4，720×1280、24fps、完整9秒', '原画面完整保留，同步模糊背景并保留原声音', '底部全程显示中文标题“寻找旅伴”', '由代码编辑师执行，Harness和模型使用任务设置中的选择'],
    },
    'local-split': {
        'prompt': '将森林素材0–5秒做左右分屏并同步播放；左侧保持原色，右侧使用暖调电影感，两侧各自居中裁满区域，输出960×540、30fps的split-screen.mp4。源素材没有音轨，成片保持静音。先给方案。',
        'options': ['左右分屏并同步播放0–5秒', '左侧原色，右侧暖调电影感', '每格居中裁满，输出960×540、30fps', '源素材无音轨，split-screen.mp4保持静音'],
    },
    'image-text-effects': {
        'prompt': '在原图上新增一个可编辑文字图层，标题文字固定为“自动换行标题示例”，不替换原图已有内容；文字在画布中居中，宽324像素、高160像素，字号52，白色文字、3像素黑色描边，并添加黑色阴影。交付text-effects.png。',
        'options': ['新增标题“自动换行标题示例”，不替换原图内容', '文字图层宽324、高160、字号52并居中自动换行', '白色文字、3像素黑色描边和黑色阴影', '交付text-effects.png'],
    },
    'image-layout-guides': {
        'prompt': '把当前这一张图片复制成3个独立图片图层，不需要补充其他图片；在720×300画布中水平等距排列并垂直居中，每个图层180×120，左边距40像素、相邻图层起点间距230像素。交付layout-guides.png。',
        'options': ['将同一张输入图片复制为3个独立图层', '画布720×300，每个图层180×120', '左边距40像素，图层起点间距230像素并垂直居中', '交付layout-guides.png'],
    },
    'video-version-compare': {
        'prompt': '输入中包含版本1原片和版本2暖调成片。取两者共同的前8秒同步对齐，生成960×540、30fps的左右对比视频version-compare.mp4，左侧标注“版本1”，右侧标注“版本2”；同时把色调、亮度、对比度和细节差异及保留建议写入version-comparison.md。交付这两个文件。',
        'options': ['版本1为原片，版本2为暖调成片，比较共同的前8秒', '输出960×540、30fps左右同步对比视频', '左右分别标注“版本1”和“版本2”', '交付version-compare.mp4和version-comparison.md'],
    },
    'video-shot-notes': {
        'prompt': '按明确的时间线批注返工这条对白视频：0–3秒保持不变；3–6秒仅将画面调整为轻微暖调并提高10%对比度，声音保持不变；6秒至结尾保持不变。保留原分辨率、帧率、完整时长和原声音，只交付shot-notes-revision.mp4。先给方案。',
        'options': ['0–3秒保持不变', '3–6秒仅做轻微暖调并提高10%对比度，声音不变', '6秒至结尾保持不变', '保留原规格、完整时长和原声音，交付shot-notes-revision.mp4'],
    },
    'image-adjustment-layers': {
        'prompt': '在原图上新增一个可关闭的调整图层，不修改原始图片图层：对比度1.18、饱和度0.72、暖色色温0.25、Gamma 1.08、不透明度0.90。按384×256原画布渲染并交付adjustment-layers.png。',
        'options': ['原始图片图层保持不变', '调整图层参数：对比度1.18、饱和度0.72、色温0.25、Gamma 1.08', '调整图层不透明度0.90且可关闭', '交付384×256的adjustment-layers.png'],
    },
    'image-erase': {
        'prompt': '消除上方标题，选区固定为x=26、y=13、宽123、高38，选区外像素必须保持不变，交付erase-result.png。补全区域不得出现任何文字；如果生成式候选仍含文字，改用选区外邻域像素进行非生成式修补，不要重复相同的生成请求。先给方案。',
        'options': ['固定选区：x=26、y=13、宽123、高38', '选区外像素保持不变，补全区域不得出现文字', '生成式候选不合格时改用非生成式邻域修补', '交付erase-result.png'],
    },
    'image-local': {
        'prompt': '把杯子改为蓝色，保留上方文字和背景；固定选区为x=110、y=50、宽200、高170，选区外像素必须与原图完全一致。优先使用Qwen局部编辑；如果生成候选改变杯子的轮廓、尺寸、位置或扁平风格，则直接改用杯子精确蒙版做本地色相替换，不需要再次询问。交付blue-mug.png。先给方案。',
        'options': ['杯子变为蓝色，轮廓、尺寸、位置和扁平风格保持不变', '固定选区：x=110、y=50、宽200、高170，选区外逐像素保持原样', '优先Qwen局部编辑；重绘失真时已授权精确蒙版本地换色', '保留原图并交付blue-mug.png'],
    },
}


def apply(catalog):
    result=deepcopy(catalog);changed=[]
    for demo in result:
        requirement=REQUIREMENTS.get(demo.get('id'))
        if not requirement:continue
        before=(demo.get('prompt'),demo.get('options'))
        demo.update(deepcopy(requirement))
        if before!=(demo.get('prompt'),demo.get('options')):changed.append(demo['id'])
    return result,changed


def publish(path=None):
    target=Path(path) if path else data_root()/'demos/catalog.json'
    catalog=json.loads(target.read_text())
    normalized,changed=apply(catalog)
    if changed:
        metadata=target.stat()
        temporary=target.with_name(target.name+'.requirements-new')
        temporary.write_text(json.dumps(normalized,ensure_ascii=False,indent=2))
        os.chmod(temporary,metadata.st_mode)
        if os.geteuid()==0:os.chown(temporary,metadata.st_uid,metadata.st_gid)
        temporary.replace(target)
    return changed


if __name__=='__main__':
    changed=publish()
    print(json.dumps({'changed':changed,'count':len(changed)},ensure_ascii=False))
