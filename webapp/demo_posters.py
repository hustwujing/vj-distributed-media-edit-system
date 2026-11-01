"""Readable demo covers made from the actual sources, outputs and measurements."""
import io
import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
from .paths import data_root


FONT = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'
INK, PAPER, ORANGE, GREEN = '#17281f', '#f5f3e9', '#ef8b5f', '#a0b3a0'


def font(size):
    return ImageFont.truetype(FONT,size)


def frame(path, time=1.2):
    data = subprocess.check_output(['ffmpeg','-v','error','-ss',str(time),'-i',str(path),
                                    '-frames:v','1','-f','image2pipe','-vcodec','png','-threads','1','-'])
    return Image.open(io.BytesIO(data)).convert('RGB')


def waveform(path, audio=0):
    raw = subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-map',f'0:a:{audio}',
                                  '-f','f32le','-ac','1','-ar','8000','-'])
    data=np.frombuffer(raw,dtype=np.float32)
    return np.array([np.sqrt(np.mean(chunk**2)) if len(chunk) else 0
                     for chunk in np.array_split(data,260)])


def build_posters(root: Path):
    catalog=json.loads((root/'catalog.json').read_text())
    for demo in catalog:
        name=demo['id']; folder=root/name
        source=folder/'before.mp4'
        actual=folder/'final.mp4' if (folder/'final.mp4').exists() else source
        canvas=ImageOps.fit(frame(actual,1.6),(640,400))
        draw=ImageDraw.Draw(canvas)
        if name in ('transcribe','batch','multitrack','analysis','loudness'):
            canvas=ImageEnhance.Brightness(canvas).enhance(.24)
            draw=ImageDraw.Draw(canvas)
        if name=='transcribe':
            draw.text((30,65),'每一句话，都有坐标',fill=PAPER,font=font(27))
            rows=[('00.44 – 02.52','What brings you…','说话人 01'),('06.88 – 08.12',"I'm searching…",'说话人 02')]
            for i,(stamp,text,speaker) in enumerate(rows):
                y=132+i*105
                draw.rounded_rectangle((26,y,612,y+89),radius=10,fill='#25392f')
                draw.text((42,y+10),stamp+'    '+speaker,fill=GREEN,font=font(17))
                draw.text((42,y+38),text,fill=PAPER,font=font(29))
        elif name=='batch':
            for i,key in enumerate(('question','answer')):
                pic=ImageOps.fit(frame(root/'sources'/f'{key}.mp4',1),(256,138))
                x=35+i*310;canvas.paste(pic,(x,60));draw=ImageDraw.Draw(canvas)
                draw.text((x,210),f'素材 {"A · 提问" if i==0 else "B · 回答"}',fill=PAPER,font=font(22))
            draw.line((320,260,320,288),fill=ORANGE,width=3)
            draw.rounded_rectangle((68,298,572,368),radius=8,fill='#e9eee3')
            draw.text((104,315),'两段素材 → 一份时间线笔记',fill=INK,font=font(25))
        elif name=='cuts':
            canvas=Image.new('RGB',(640,400),INK)
            for i,t in enumerate((.5,1.6,3.1)):
                canvas.paste(ImageOps.fit(frame(actual,t),(196,188)),(12+i*210,72))
            draw=ImageDraw.Draw(canvas)
            draw.line((216,66,216,275),fill=ORANGE,width=4)
            draw.line((426,66,426,275),fill=ORANGE,width=4)
            draw.text((32,294),'9.0s',fill=GREEN,font=font(42));draw.text((198,297),'→',fill=ORANGE,font=font(40))
            draw.text((284,294),'3.7s',fill=PAPER,font=font(42));draw.text((443,318),'保留完整问答',fill=GREEN,font=font(18))
        elif name=='grade':
            left=ImageOps.fit(frame(source,2),(640,400));right=ImageOps.fit(frame(actual,2),(640,400))
            canvas=left;canvas.paste(right.crop((320,0,640,400)),(320,0));draw=ImageDraw.Draw(canvas)
            draw.line((320,0,320,400),fill=PAPER,width=3)
            for x,label in ((28,'原片'),(356,'暖调电影感')):
                draw.rounded_rectangle((x,302,x+245,355),radius=8,fill=INK)
                draw.text((x+16,312),label,fill=PAPER,font=font(23))
        elif name=='subtitles':
            # Actual burned-in subtitles are kept; add only the explanatory label.
            canvas=ImageOps.fit(frame(actual,1.5),(640,360));new=Image.new('RGB',(640,400),INK);new.paste(canvas,(0,0));canvas=new
            draw=ImageDraw.Draw(canvas);draw.text((28,361),'语音时间戳  →  自动对齐字幕',fill=PAPER,font=font(21))
        elif name=='overlay':
            canvas=ImageOps.fit(frame(actual,3),(640,360));new=Image.new('RGB',(640,400),INK);new.paste(canvas,(0,0));canvas=new
            draw=ImageDraw.Draw(canvas);draw.text((28,361),'第 1 秒出现  ·  小窗持续 5 秒',fill=PAPER,font=font(21))
        elif name=='loudness':
            for path,y,label,color in ((source,145,'处理前 · 声音过小',GREEN),(actual,300,'处理后 · 响度归一化',ORANGE)):
                values=waveform(path)
                draw.text((35,y-85),label,fill=PAPER,font=font(22))
                draw.line((30,y,610,y),fill='#5c7060',width=1)
                for i,value in enumerate(values):
                    height=min(53,max(1,float(value)*210))
                    x=30+i*2.2;draw.line((x,y-height,x,y+height),fill=color,width=2)
        elif name=='timeline':
            canvas=Image.new('RGB',(640,400),INK)
            with Image.open(folder/'timeline.png') as timeline:
                canvas.paste(ImageOps.contain(timeline,(620,230)),(10,95))
            draw=ImageDraw.Draw(canvas);draw.text((30,55),'画面  ×  波形  ×  逐词时间',fill=PAPER,font=font(27))
            draw.text((30,328),'看清停顿，找到剪切点',fill=GREEN,font=font(23))
        elif name=='analysis':
            stats=json.loads((folder/'analysis.json').read_text())['测量结果']
            for i,(key,label) in enumerate([('y_mean','平均亮度'),('y_std','画面对比'),('sat_mean','平均饱和')]):
                y=80+i*93
                draw.text((32,y),label,fill=PAPER,font=font(23))
                draw.text((496,y),f'{stats[key]:.3f}',fill=GREEN,font=font(22))
                draw.rounded_rectangle((32,y+41,603,y+53),radius=5,fill='#36493a')
                draw.rounded_rectangle((32,y+41,32+max(8,571*stats[key]),y+53),radius=5,fill=ORANGE)
        elif name=='multitrack':
            for i,label in enumerate(('音轨 0 · 背景测试音','音轨 1 · 原片对白')):
                y=115+i*130
                draw.rounded_rectangle((25,y-20,616,y+95),radius=12,fill='#314536' if i else '#24332b')
                draw.text((45,y-9),label,fill=PAPER if i else GREEN,font=font(24))
                if i:draw.text((524,y-8),'✓',fill=ORANGE,font=font(33))
                values=waveform(source,i)
                for j,value in enumerate(values):
                    h=min(28,max(1,float(value)*230));x=44+j*2.05
                    draw.line((x,y+57-h,x,y+57+h),fill=ORANGE if i else GREEN,width=2)
        canvas.save(folder/'poster.jpg',quality=92)


if __name__=='__main__':
    import os
    if hasattr(os,'sched_getaffinity'):os.sched_setaffinity(0,sorted(os.sched_getaffinity(0))[:4])
    build_posters(data_root()/'demos')
