#!/usr/bin/env python3
"""Create a still-frame title/layout preview during discussion; no video render.

Read references/style-previews.md. --output must be in the current workspace's
edit/previews directory. The web launcher supplies the writable turn subdirectory.
"""
import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageOps, ImageFilter
from edit_plan import PlanError,local_file,probe,color
from edit_media import title_image


def create_preview(args):
    root=args.workspace.resolve();output=(root/args.output).resolve();base=(root/'edit/previews').resolve()
    if not base.is_relative_to(root) or not output.is_relative_to(base) or output.suffix.lower()!='.png':
        raise PlanError('Output must be a PNG inside workspace/edit/previews/')
    if output.exists():raise PlanError('Use a new filename for each style preview')
    w,h=args.width,args.height
    if not 64<=w<=4096 or not 64<=h<=4096 or w%2 or h%2:raise PlanError('Preview dimensions must be even, between 64 and 4096')
    if not 10<=args.font_size<=200 or not 1<=len(args.text)<=500:raise PlanError('Use 1–500 text characters and font size 10–200')
    color(args.color)
    if args.input:
        source=local_file(args.input,root)
        try:
            with Image.open(source) as source_image:
                image=ImageOps.exif_transpose(source_image).convert('RGBA')
            if args.fit=='cover':image=ImageOps.fit(image,(w,h))
            else:
                front=ImageOps.contain(image,(w,h));background=ImageOps.fit(image,(w,h)).filter(ImageFilter.GaussianBlur(20)) if args.fit=='blur' else Image.new('RGBA',(w,h),'black')
                background.alpha_composite(front,((w-front.width)//2,(h-front.height)//2));image=background
        except OSError:
            meta=probe(source)
            if not meta['video'] or not 0<=args.time<meta['duration']:raise PlanError('Choose a video frame within the source duration')
            shape='scale=trunc(iw*sar/2)*2:ih,setsar=1'
            if args.fit=='blur':
                vf=f'{shape},split[bg][fg];[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[blur];[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2[front];[blur][front]overlay=(W-w)/2:(H-h)/2'
            elif args.fit=='cover':vf=f'{shape},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}'
            else:vf=f'{shape},scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black'
            decoded=subprocess.run(['ffmpeg','-v','error','-nostdin','-ss',str(args.time),'-i',str(source),
                '-filter_complex_threads','1','-filter_complex',vf,'-frames:v','1','-threads','1','-f','image2pipe','-c:v','png','-'],
                capture_output=True,timeout=45,check=True)
            image=Image.open(io.BytesIO(decoded.stdout)).convert('RGBA')
    else:image=Image.new('RGBA',(w,h),'#27352E')
    output.parent.mkdir(parents=True,exist_ok=True)
    # Reuse the same title layout and font selection as the actual video renderer.
    layer=io.BytesIO()
    item={'text':args.text,'size':args.font_size,'color':args.color,'position':args.position,'background':not args.no_box}
    # title_image writes a PNG; use a file-like object with a format-bearing name.
    layer.name='title.png'
    title_image(item,w,h,layer)
    layer.seek(0);image=Image.alpha_composite(image,Image.open(layer).convert('RGBA'))
    with output.open('xb') as target:image.convert('RGB').save(target,format='PNG')
    return {'output':str(output.relative_to(root)),'width':w,'height':h,'preview_only':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input');parser.add_argument('--time',type=float,default=0)
    parser.add_argument('--text',required=True);parser.add_argument('--font-size',type=int,default=48)
    parser.add_argument('--color',default='#FFFFFF');parser.add_argument('--position',choices=['top','center','bottom'],default='bottom')
    parser.add_argument('--fit',choices=['contain','cover','blur'],default='contain')
    parser.add_argument('--width',type=int,default=1280);parser.add_argument('--height',type=int,default=720)
    parser.add_argument('--no-box',action='store_true');parser.add_argument('--workspace',type=Path,default=Path.cwd())
    parser.add_argument('-o','--output',type=Path,required=True)
    try:print(json.dumps(create_preview(parser.parse_args()),ensure_ascii=False));return 0
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False));return 1


if __name__=='__main__':sys.exit(main())
