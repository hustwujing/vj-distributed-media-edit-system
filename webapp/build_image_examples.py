"""Publish already verified, original image fixtures without another model run."""
import json
import os
import pwd
import shutil
from pathlib import Path
from PIL import Image
from .paths import data_root

def main():
    root=data_root();samples=root/'setup/image-workbench-samples';demos=root/'demos';sources=demos/'sources'
    shutil.copyfile(samples/'cup.png',sources/'image-cup.png')
    with Image.open(samples/'cup.png') as original:
        foreground=original.convert('RGBA')
        with Image.open(samples/'remove_bg/mask.png') as mask:foreground.putalpha(mask.convert('L'))
        foreground.save(samples/'foreground.png')
    definitions=[
      ('image-cutout','抠出主体，换一个背景','抠图与透明前景','foreground.png','请先给方案，再把杯子抠成透明 PNG，保留原图。','BiRefNet 实际生成的前景蒙版'),
      ('image-select','框选杯子，精确选择主体','主体选择','segment/mask.png','框选杯子范围 x=110,y=50,宽200,高170，用 SAM2 生成主体蒙版和透明前景。先给方案。','SAM2.1 实际框选结果'),
      ('image-local','只改杯子的颜色','局部重绘','real-local_edit/image.png','把杯子改为蓝色，保留上方文字和背景；范围 x=110,y=50,宽200,高170。先给方案。','Qwen 编辑后按原分辨率选区合成，选区外像素保留'),
      ('image-layers','拆成可编辑的透明图层','图片分层','real-decompose/layer-overview.png','将这张图片拆成3个 RGBA 图层，提供合成预览和 PSD/ORA。先给方案。','Qwen-Layered 实际分层；生成式重建可能含重复内容'),
      ('image-erase','移除文字，补全背景','局部消除','real-erase/image.png','消除上方标题，范围 x=26,y=13,宽123,高38，其余原图像素保留。先给方案。','Qwen 消除选区内容并以蒙版合成结果')]
    catalog=json.loads((demos/'catalog.json').read_text());ids={x[0] for x in definitions};catalog=[x for x in catalog if x['id'] not in ids]
    for id,title,category,result,prompt,description in definitions:
        folder=demos/id;folder.mkdir(exist_ok=True);shutil.copyfile(samples/result,folder/'result.png');shutil.copyfile(samples/'cup.png',folder/'before.png')
        with Image.open(folder/'result.png') as img:
            img.thumbnail((640,480));bg=Image.new('RGBA',img.size,'#e8ede3');bg.alpha_composite(img.convert('RGBA'));bg.convert('RGB').save(folder/'poster.jpg')
        catalog.append({'id':id,'title':title,'category':category,'description':description,'tags':['图片编辑','真实模型结果'],
            'media':'image','workflow':'agent','renderer':'image-workbench','prompt':prompt,
            'options':[description,'可在图片工作台手动框选和调整图层','原图保留，结果可重新载入图层'],
            'sources':[{'key':'main','name':'原创杯子示例.png','file':'image-cup.png'}],
            'preset':{'kind':'agent','assets':['main']},'result':'result.png','result_type':'image/png',
            'poster':'poster.jpg','before':'before.png','before_type':'image/png','files':['result.png','before.png','poster.jpg'],
            'credits':[{'name':'本项目绘制的原创杯子与文字测试图','url':'/video-use/','license':'原创演示素材，可用于本项目编辑示例'}],
            'changes':description+'。模型推理记录保存在本项目验收目录，不把静态示例当成用户新任务结果。'})
    from .demo_agent_requirements import apply as apply_agent_requirements
    catalog,_=apply_agent_requirements(catalog)
    new=demos/'catalog.image-new.json';new.write_text(json.dumps(catalog,ensure_ascii=False,indent=2));new.replace(demos/'catalog.json')
    if os.geteuid()==0:
        user=pwd.getpwnam('video-use')
        for path in [demos/'catalog.json',sources/'image-cup.png',*[p for id,*_ in definitions for p in (demos/id,*list((demos/id).iterdir()))]]:
            os.chown(path,user.pw_uid,user.pw_gid);path.chmod(0o750 if path.is_dir() else 0o640)
    print('Published 5 image examples; total',len(catalog))

if __name__=='__main__':main()
