#!/usr/bin/env python3
"""Deterministic, extensible image editing plans for the video-use Agent.

python helpers/image_edit.py capabilities
python helpers/image_edit.py validate edit/image-plan.json --workspace .
python helpers/image_edit.py render edit/image-plan.json --workspace . -o edit/image-v1.png

The plan is deliberately small and safe. Agent-written Python/Pillow/OpenCV
scripts may be used when a custom effect is outside this schema.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageDraw


class ImagePlanError(ValueError): pass

FORMATS={'.png':'PNG','.jpg':'JPEG','.jpeg':'JPEG','.webp':'WEBP'}
CAPABILITIES={'version':1,'model_required':False,'operations':['resize','crop','rotate','flip','background','adjust','blur','pixelate','mask','text','overlay','format'],
              'description':'本地 Pillow 图片处理；保留原图，每次生成新版本。','reference':'references/image-editing.md'}


def finite(value,name,low=-1e9,high=1e9):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:raise ImagePlanError(f'{name} 参数无效')
    return value


def path_inside(value,root):
    if not isinstance(value,str) or '\x00' in value:raise ImagePlanError('文件路径无效')
    p=(root/value).resolve()
    if not p.is_relative_to(root) or not p.is_file():raise ImagePlanError(f'文件必须位于当前工作区：{value}')
    return p


def color(value):
    if not isinstance(value,str) or not value.startswith('#') or len(value) not in (7,9):raise ImagePlanError('颜色必须是 #RRGGBB 或 #RRGGBBAA')
    try:return tuple(bytes.fromhex(value[1:]))
    except ValueError as exc:raise ImagePlanError('颜色格式无效') from exc


def validate(raw,workspace):
    root=Path(workspace).resolve();p=copy.deepcopy(raw)
    if p.get('version')!=1:raise ImagePlanError('只支持图片计划 version 1')
    if not isinstance(p.get('sources'),dict) or not 1<=len(p['sources'])<=20:raise ImagePlanError('sources 需要 1–20 个图片别名')
    for key,value in p['sources'].items():
        if not isinstance(key,str) or not key.replace('_','').replace('-','').isalnum():raise ImagePlanError('图片别名无效')
        p['sources'][key]=str(path_inside(value,root))
        try:
            with Image.open(p['sources'][key]) as image:
                if image.width*image.height>80_000_000:raise ImagePlanError('图片像素过大')
        except OSError as exc:raise ImagePlanError('无法读取图片') from exc
    if not isinstance(p.get('operations',[]),list) or len(p['operations'])>80:raise ImagePlanError('operations 最多 80 项')
    for i,op in enumerate(p['operations']):
        if not isinstance(op,dict) or not isinstance(op.get('type'),str):raise ImagePlanError(f'operations[{i}] 无效')
        kind=op['type']
        if kind=='resize':
            finite(op.get('width'),f'operations[{i}].width',16,10000);finite(op.get('height'),f'operations[{i}].height',16,10000)
        elif kind=='crop':
            for key in ('x','y','width','height'):finite(op.get(key),f'operations[{i}].{key}',0 if key in ('x','y') else 2,10000)
        elif kind in ('rotate','flip'):
            if kind=='rotate' and op.get('degrees') not in (0,90,180,270):raise ImagePlanError('rotate 只支持 0/90/180/270')
            if kind=='flip' and op.get('axis') not in ('horizontal','vertical'):raise ImagePlanError('flip axis 无效')
        elif kind in ('adjust','blur','pixelate','mask'):
            if kind=='adjust':
                for key in ('brightness','contrast','saturation','sharpness'):finite(op.get(key,1),key,0,4)
            else:
                for key in ('x','y','width','height'):finite(op.get(key),f'{kind}.{key}',0 if key in ('x','y') else 2,10000)
                if kind=='blur':finite(op.get('radius',8),'blur.radius',1,100)
                if kind=='pixelate':finite(op.get('block',16),'pixelate.block',2,200)
                if kind=='mask' and op.get('mode') not in ('blur','pixelate','black'):raise ImagePlanError('mask.mode 无效')
        elif kind=='background':color(op.get('color','#FFFFFF'))
        elif kind=='text':
            if not isinstance(op.get('text'),str) or not 1<=len(op['text'])<=1000:raise ImagePlanError('text 需要 1–1000 个字符')
            finite(op.get('x',0),'text.x',0,10000);finite(op.get('y',0),'text.y',0,10000);finite(op.get('size',48),'text.size',8,400);color(op.get('color','#FFFFFF'))
        elif kind=='overlay':
            op['source']=str(path_inside(op.get('source'),root));finite(op.get('x',0),'overlay.x',-10000,10000);finite(op.get('y',0),'overlay.y',-10000,10000);finite(op.get('opacity',1),'overlay.opacity',0,1)
        elif kind=='format':
            if op.get('format') not in ('png','jpg','webp'):raise ImagePlanError('format 只支持 png/jpg/webp')
        else:raise ImagePlanError(f'不支持的图片操作：{kind}')
    output=p.setdefault('output',{});fmt=output.get('format','png');
    if fmt not in ('png','jpg','webp'):raise ImagePlanError('output.format 只支持 png/jpg/webp')
    p.setdefault('operations',[])
    p['_root']=str(root);p['_format']=fmt;return p


def font(size):
    from edit_captions import caption_font
    path,_=caption_font()
    return ImageFont.truetype(path,int(size))


def render_plan(p,output):
    root=Path(p['_root']);output=(root/output).resolve();edit=(root/'edit').resolve()
    if not output.is_relative_to(edit) or output.exists():raise ImagePlanError('输出必须位于 edit/ 且使用新文件名')
    if output.suffix.lower() not in FORMATS:raise ImagePlanError('输出扩展名必须为 png/jpg/webp')
    source_name=next(iter(p['sources']));image=ImageOps.exif_transpose(Image.open(p['sources'][source_name])).convert('RGBA')
    for op in p['operations']:
        kind=op['type']
        if kind=='resize':image=image.resize((int(op['width']),int(op['height'])),Image.Resampling.LANCZOS)
        elif kind=='crop':
            x,y,w,h=[int(op[k]) for k in ('x','y','width','height')]
            if x+w>image.width or y+h>image.height:raise ImagePlanError('crop 超出图片范围')
            image=image.crop((x,y,x+w,y+h))
        elif kind=='rotate':image=image.rotate(-op['degrees'],expand=True)
        elif kind=='flip':image=ImageOps.mirror(image) if op['axis']=='horizontal' else ImageOps.flip(image)
        elif kind=='background':
            bg=Image.new('RGBA',image.size,color(op.get('color','#FFFFFF')));bg.alpha_composite(image);image=bg
        elif kind=='adjust':
            for cls,key in ((ImageEnhance.Brightness,'brightness'),(ImageEnhance.Contrast,'contrast'),(ImageEnhance.Color,'saturation'),(ImageEnhance.Sharpness,'sharpness')):image=cls(image).enhance(float(op.get(key,1)))
        elif kind in ('blur','pixelate','mask'):
            x,y,w,h=[int(op[k]) for k in ('x','y','width','height')]
            if x+w>image.width or y+h>image.height:raise ImagePlanError(f'{kind} 区域超出图片范围')
            region=image.crop((x,y,x+w,y+h))
            mode=op.get('mode',kind)
            if kind=='blur' or mode=='blur':region=region.filter(ImageFilter.GaussianBlur(float(op.get('radius',8))))
            elif kind=='pixelate' or mode=='pixelate':region=region.resize((max(1,w//int(op.get('block',16))),max(1,h//int(op.get('block',16)))),Image.Resampling.BILINEAR).resize((w,h),Image.Resampling.NEAREST)
            else:region=Image.new('RGBA',(w,h),'black')
            image.alpha_composite(region,(x,y))
        elif kind=='text':
            draw=ImageDraw.Draw(image);f=font(op.get('size',48));x,y=op.get('x',0),op.get('y',0);fill=color(op.get('color','#FFFFFF'));draw.text((x,y),op['text'],font=f,fill=fill,stroke_width=int(op.get('stroke_width',1)),stroke_fill=color(op.get('stroke','#000000')))
        elif kind=='overlay':
            layer=ImageOps.exif_transpose(Image.open(op['source'])).convert('RGBA');layer.putalpha(layer.getchannel('A').point(lambda a:int(a*float(op.get('opacity',1)))));image.alpha_composite(layer,(int(op.get('x',0)),int(op.get('y',0))))
    output.parent.mkdir(parents=True,exist_ok=True);image.convert('RGB' if output.suffix.lower() in ('.jpg','.jpeg') else 'RGBA').save(output,format=FORMATS[output.suffix.lower()])
    return {'output':str(output.relative_to(root)),'width':image.width,'height':image.height,'format':p['_format'],'operations':len(p['operations'])}


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('command',choices=['capabilities','validate','render']);ap.add_argument('plan',nargs='?');ap.add_argument('--workspace',type=Path,default=Path.cwd());ap.add_argument('-o','--output',type=Path)
    a=ap.parse_args(argv)
    try:
        if a.command=='capabilities':print(json.dumps(CAPABILITIES,ensure_ascii=False,indent=2));return 0
        raw=json.loads(path_inside(a.plan,a.workspace).read_text());p=validate(raw,a.workspace)
        if a.command=='validate':print(json.dumps({'valid':True,'operations':len(p['operations']),'format':p['_format']},ensure_ascii=False));return 0
        if not a.output:raise ImagePlanError('render 需要 -o output')
        print(json.dumps({'stage':'complete',**render_plan(p,a.output)},ensure_ascii=False));return 0
    except (ImagePlanError,OSError,ValueError) as exc:
        record=json.dumps({'stage':'error','error':str(exc),'error_type':type(exc).__name__,
                           'code':'parameters' if isinstance(exc,ValueError) else 'environment'},ensure_ascii=False)
        print(record);print(record,file=sys.stderr,flush=True);return 1


if __name__=='__main__':sys.exit(main())
