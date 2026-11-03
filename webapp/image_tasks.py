"""Image task dispatcher reused by manual jobs and the Agent gateway."""
import contextlib
import json
import os
import shutil
import signal
import threading
from pathlib import Path

from PIL import Image,ImageDraw,ImageEnhance,ImageFilter,ImageFont,ImageOps
from .image_document import ImageTask,check_task,export_document,FONT,edit_prompt
from .image_providers import remote_images,model_image


def execute_image(raw,assets,project,output,cancel=None,progress=None):
    task=ImageTask.model_validate(raw);assets={a['id']:a for a in assets} if isinstance(assets,list) else assets
    check_task(task,assets);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    cancel=cancel or threading.Event();progress=progress or (lambda text:print(text,flush=True))
    if task.action=='export':return export_document(task.document,assets,project,output,task.format)
    source=Path(project)/'sources'/assets[task.asset]['filename']
    with Image.open(source) as im:image=ImageOps.exif_transpose(im).convert('RGBA')
    normalized=output/'.input.png';image.save(normalized)
    mask=None
    if task.mask:
        with Image.open(Path(project)/'sources'/assets[task.mask]['filename']) as im:mask=im.convert('L')
    elif task.box:
        mask=Image.new('L',image.size);x,y,w,h=task.box;ImageDraw.Draw(mask).rectangle((x,y,x+w-1,y+h-1),fill=255)
    previous_handler=None
    # Cancelling the job's process group propagates to the upstream model task.
    if threading.current_thread() is threading.main_thread():
        previous_handler=signal.signal(signal.SIGTERM,lambda *_:cancel.set())
    try:
        if task.action in ('segment_sam3','refine_zim'):
            parameters={'prompt':task.prompt,'box':task.box,'points':task.points,'refine':task.parameters.get('refine',True),'confidence':task.parameters.get('confidence',.35),'metadata':str(output/'sam3-zim.json')}
            if task.action=='refine_zim':mask.save(output/'.mask.png');parameters['mask']=str(output/'.mask.png')
            progress(('正在使用 SAM3 选择并用 ZIM 精修边缘' if parameters['refine'] else '正在使用 SAM3 选择主体') if task.action=='segment_sam3' else '正在使用 ZIM 精修蒙版边缘')
            model_image(task.action,normalized,output/'mask.png',parameters,output,cancel)
            with Image.open(output/'mask.png') as im:refined=im.convert('L')
            foreground=image.copy();foreground.putalpha(refined);foreground.save(output/'foreground.png')
            files=['foreground.png','mask.png','sam3-zim.json']
            for name in ('sam3-preview.jpg','zim-preview.jpg'):
                if (output/name).is_file():files.append(name)
            return files
        if task.action=='refine_mask':
            import cv2,numpy as np
            p=task.parameters;array=np.asarray(image.convert('L'));_,array=cv2.threshold(array,int(p.get('threshold',128)),255,cv2.THRESH_BINARY)
            grow=max(-100,min(100,int(p.get('grow',0))));kernel=np.ones((abs(grow)*2+1,)*2,np.uint8) if grow else None
            if grow>0:array=cv2.dilate(array,kernel)
            elif grow<0:array=cv2.erode(array,kernel)
            if p.get('fill_holes'):
                flood=array.copy();cv2.floodFill(flood,np.zeros((array.shape[0]+2,array.shape[1]+2),np.uint8),(0,0),255);array=cv2.bitwise_or(array,cv2.bitwise_not(flood))
            minimum=max(0,int(p.get('remove_small',0)))
            if minimum:
                count,labels,stats,_=cv2.connectedComponentsWithStats(array)
                array=np.where(np.isin(labels,[i for i in range(1,count) if stats[i,cv2.CC_STAT_AREA]>=minimum]),255,0).astype('uint8')
            result=Image.fromarray(array,'L');feather=max(0,min(100,float(p.get('feather',0))))
            if feather:result=result.filter(ImageFilter.GaussianBlur(feather))
            result.save(output/'mask.png');return ['mask.png']
        if task.action=='perspective':
            import cv2,numpy as np
            pts=np.float32(task.points);top=max(np.linalg.norm(pts[1]-pts[0]),np.linalg.norm(pts[2]-pts[3]));side=max(np.linalg.norm(pts[3]-pts[0]),np.linalg.norm(pts[2]-pts[1]));w,h=max(2,round(top)),max(2,round(side))
            matrix=cv2.getPerspectiveTransform(pts,np.float32([[0,0],[w-1,0],[w-1,h-1],[0,h-1]]));result=cv2.warpPerspective(np.asarray(image),matrix,(w,h),borderMode=cv2.BORDER_REPLICATE);Image.fromarray(result).save(output/'image.png');return ['image.png']
        if task.action=='local_adjust':
            import numpy as np
            p=task.parameters;result=image
            for key,klass in [('brightness',ImageEnhance.Brightness),('contrast',ImageEnhance.Contrast),('saturation',ImageEnhance.Color),('sharpen',ImageEnhance.Sharpness)]:
                if key in p:result=klass(result).enhance(float(p[key])+(1 if key=='sharpen' else 0))
            if p.get('auto_white_balance') or p.get('temperature'):
                array=np.asarray(result).astype('float32');rgb=array[...,:3];means=rgb.reshape(-1,3).mean(0);target=means.mean();rgb*=target/np.maximum(means,1);temp=float(p.get('temperature',0));rgb[...,0]+=45*temp;rgb[...,2]-=45*temp;array[...,:3]=np.clip(rgb,0,255);result=Image.fromarray(array.astype('uint8'),'RGBA')
            if p.get('auto_levels'):
                array=np.asarray(result).copy();rgb=array[...,:3];lo=np.percentile(rgb,1,(0,1));hi=np.percentile(rgb,99,(0,1));array[...,:3]=np.clip((rgb-lo)*255/np.maximum(1,hi-lo),0,255);result=Image.fromarray(array.astype('uint8'),'RGBA')
            black=float(p.get('black_point',0));white=float(p.get('white_point',255));gamma=float(p.get('gamma',1))
            if black or white!=255 or gamma!=1:
                array=np.asarray(result).copy().astype('float32');rgb=np.clip((array[...,:3]-black)/max(1,white-black),0,1);array[...,:3]=np.power(rgb,1/gamma)*255;result=Image.fromarray(array.astype('uint8'),'RGBA')
            if p.get('blur'):result=result.filter(ImageFilter.GaussianBlur(float(p['blur'])))
            result.save(output/'image.png');return ['image.png']
        if task.action=='color_match':
            import numpy as np
            reference=Path(project)/'sources'/assets[task.references[0]]['filename']
            with Image.open(reference) as im:ref=np.asarray(ImageOps.exif_transpose(im).convert('RGB')).astype('float32')
            src=np.asarray(image.convert('RGB')).astype('float32');sm,ss=src.reshape(-1,3).mean(0),src.reshape(-1,3).std(0);rm,rs=ref.reshape(-1,3).mean(0),ref.reshape(-1,3).std(0);matched=(src-sm)*rs/np.maximum(ss,1)+rm;strength=max(0,min(1,float(task.parameters.get('strength',1))));result=np.clip(src*(1-strength)+matched*strength,0,255).astype('uint8');Image.fromarray(result).save(output/'image.png');return ['image.png']
        if task.action in ('product_layout','batch_export'):
            p=task.parameters;w,h=p['width'],p['height'];background=p.get('background','#ffffff');canvas=Image.new('RGBA',(w,h),background)
            # Export fits the complete canvas. Product-layout padding must not
            # rescale an already composed image or move its labels on export.
            margin=max(0,min(w//3,h//3,int(p.get('margin',round(min(w,h)*.08))))) if task.action=='product_layout' else 0
            subject=(ImageOps.fit(image,(w,h),Image.Resampling.LANCZOS) if task.action=='batch_export' and p.get('fit')=='cover' else ImageOps.contain(image,(max(1,w-2*margin),max(1,h-2*margin)),Image.Resampling.LANCZOS));x=(w-subject.width)//2;y=(h-subject.height)//2
            if task.action=='product_layout' and p.get('shadow',True):
                alpha=subject.getchannel('A');shadow=Image.new('RGBA',(w,h));mask=Image.new('L',(w,h));mask.paste(alpha,(x+int(p.get('shadow_x',8)),y+int(p.get('shadow_y',12))));mask=mask.filter(ImageFilter.GaussianBlur(float(p.get('shadow_blur',18))));shadow.putalpha(mask.point(lambda v:round(v*.35)));canvas=Image.alpha_composite(canvas,shadow)
            canvas.alpha_composite(subject,(x,y))
            if task.action=='batch_export' and p.get('watermark'):
                draw=ImageDraw.Draw(canvas);font=ImageFont.truetype(FONT,max(12,round(h*.035)));draw.text((w-20,h-20),str(p['watermark'])[:100],font=font,fill='#ffffff',stroke_width=2,stroke_fill='#000000',anchor='rb')
            fmt=p.get('format','png').lower();name='image.'+fmt;target=output/name
            if fmt in ('jpg','jpeg'):canvas.convert('RGB').save(target,quality=int(p.get('quality',90)))
            else:canvas.save(target,quality=int(p.get('quality',90)))
            return [name]
        if task.action in ('remove_bg','segment'):
            progress('正在生成主体蒙版')
            model_image(task.action,normalized,output/'mask.png',{'box':task.box,'points':task.points},output,cancel)
            with Image.open(output/'mask.png') as im:mask=im.convert('L')
            foreground=image.copy();foreground.putalpha(mask);foreground.save(output/'foreground.png')
            return ['foreground.png','mask.png']
        if task.action=='ocr':
            progress('正在识别图片文字')
            model_image('ocr',normalized,output/'ocr.json',{},output,cancel)
            data=json.loads((output/'ocr.json').read_text());preview=image.copy();draw=ImageDraw.Draw(preview)
            for line in data['lines']:
                x,y,w,h=line['box'];draw.rectangle((x,y,x+w,y+h),outline='#45b977',width=2)
            preview.save(output/'ocr-preview.png');return ['ocr-preview.png','ocr.json']
        if task.action=='replace_text':
            # Reuse available OpenCV environment through a fixed script (no browser code).
            request=output/'.replace.json';mask.save(output/'.mask.png')
            request.write_text(json.dumps({'source':str(normalized),'mask':str(output/'.mask.png'),'output':str(output/'image.png'),
                'text':task.text,'size':task.size,'color':task.color,'font':FONT}))
            import subprocess
            foreign_env={key:value for key,value in os.environ.items() if key not in ('PYTHONHOME','PYTHONPATH','VIRTUAL_ENV')}
            foreign_env['PYTHONNOUSERSITE']='1'
            subprocess.run(['/data/wujing03/miniforge3/envs/reference_video_remix_ocr/bin/python',str(Path(__file__).with_name('image_replace.py')),str(request)],check=True,timeout=60,env=foreign_env)
            return ['image.png']
        if task.action=='decompose':
            params={'prompt':task.prompt,'layer_count':str(task.layer_count),'resolution':'640','quality':'balanced','seed':'777','reconstruction_guard':'true'}
        else:
            prompt=edit_prompt(task.action,task.prompt)
            # Send a second reference marking the region; the final composite below
            # enforces untouched pixels outside the exact original-resolution mask.
            params={'prompt':prompt,'quality':task.quality,'output_megapixels':'0.6','seed':'2511','preserve_details':'true','auto_retry':'false'}
        refs=[Path(project)/'sources'/assets[a]['filename'] for a in task.references]
        if mask is not None and task.action in ('erase','local_edit'):
            guide=image.copy();tint=Image.new('RGBA',image.size,(255,32,32,0));tint.putalpha(mask.point(lambda p:round(p*.4)));guide=Image.alpha_composite(guide,tint);guide.save(output/'.selection.png')
            refs=[output/'.selection.png',*refs][:2]
            params['prompt']=edit_prompt(task.action,task.prompt,selected=True)
        remote=remote_images('decompose' if task.action=='decompose' else 'edit',normalized,output/'remote',params,refs,cancel,progress)
        if task.action=='decompose':
            files=[]
            for item in remote:
                name=item['name']
                if name not in ['composite.png','layer-overview.png','editable-layers.psd','editable-layers.ora','layer-delivery.zip','layer-report.json'] and not name.startswith('layer-'):continue
                if Path(name).suffix.lower() not in ('.png','.psd','.ora','.zip','.json'):continue
                shutil.copyfile(item['path'],output/name);files.append(name)
            if 'composite.png' not in files:raise RuntimeError('分层服务没有返回合成预览')
            files.sort(key=lambda n:(n!='composite.png',n));return files
        candidate=next((a for a in remote if a['kind']=='image' or a['type'].startswith('image/')),None)
        if not candidate:raise RuntimeError('图片服务没有返回图片')
        with Image.open(candidate['path']) as im:
            result=ImageOps.exif_transpose(im).convert('RGBA')
        if mask is not None:
            result=result.resize(image.size,Image.Resampling.LANCZOS)
            result=Image.composite(result,image,mask)
        result.save(output/'image.png')
        (output/'image.report.json').write_text(json.dumps({'action':task.action,'width':result.width,'height':result.height,'masked_composite':mask is not None},ensure_ascii=False))
        return ['image.png','image.report.json']
    finally:
        if previous_handler is not None:signal.signal(signal.SIGTERM,previous_handler)
        normalized.unlink(missing_ok=True)
