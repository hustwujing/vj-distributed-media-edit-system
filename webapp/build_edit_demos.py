"""Render local editing examples from existing licensed sources; no model calls.

Run: python -m webapp.build_edit_demos
Publishes only after every helper render has passed its media checks.
"""
from __future__ import annotations

import copy
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .paths import data_root
from .build_demos import BUNNY,SINTEL,SOURCE_MAP

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'helpers'))
from edit_plan import validate
from edit_media import render_plan


def definitions():
    return [
      dict(id='local-speed',title='快慢切换，让节奏有变化',category='变速与定格',
           description='同一段画面先加速再慢放，末尾停留一秒。',keys=['nature'],
           prompt='把森林素材 0–4 秒变为 2 倍速，4–6 秒变为半速，最后定格 1 秒，输出 960×540、30fps。先给方案。',
           options=['前段 2 倍速','后段 0.5 倍速','末帧定格 1 秒；总计 7 秒'],
           plan={'clips':[{'source':'nature','start':0,'end':4,'speed':2},{'source':'nature','start':4,'end':6,'speed':.5,'freeze_end':1}]}),
      dict(id='local-transition',title='用叠化连接两个片段',category='画面转场',
           description='前后镜头交叠过渡，声音同时交叉淡化。',keys=['nature'],
           prompt='选森林素材 0–3 秒和 6–9 秒，两段之间加 0.6 秒叠化，输出 960×540、30fps。先给方案。',
           options=['两段各 3 秒','转场重叠 0.6 秒','成片 5.4 秒'],
           plan={'clips':[{'source':'nature','start':0,'end':3},{'source':'nature','start':6,'end':9}],
                 'transition':{'type':'fade','duration':.6}}),
      dict(id='local-reverse',title='倒放，再循环一次',category='倒放与循环',
           description='观察动作倒放，两轮之间保留短暂停格。',keys=['nature'],
           prompt='将森林素材 2–5 秒倒放，末尾定格 0.5 秒，完整重复两遍，保持静音。先给方案。',
           options=['倒放 2–5 秒','每轮定格 0.5 秒','循环两次，共 7 秒'],
           plan={'clips':[{'source':'nature','start':2,'end':5,'reverse':True,'freeze_end':.5,'repeat':2}]}),
      dict(id='local-geometry',title='裁剪镜像，重新安排画幅',category='裁剪与旋转',
           description='取画面中部，旋转镜像后，用模糊背景填充竖屏。',keys=['nature'],
           prompt='取森林素材 0–5 秒，裁取 x=160、y=0、宽640、高540 区域，顺时针旋转90度后水平镜像，完整保留处理后的画面，用模糊背景填满540×960竖屏。先给方案。',
           options=['中部 640×540 裁剪','旋转 90°＋水平镜像','540×960 竖屏、模糊填充'],
           plan={'clips':[{'source':'nature','end':5,'crop':[160,0,640,540],'rotate':90,'flip':'horizontal','fit':'blur'}],
                 'output':{'width':540,'height':960,'fps':30}}),
      dict(id='local-split',title='左右分屏，对比两种色彩',category='分屏合成',
           description='左右同步播放原色与暖调版本，一眼比较差异。',keys=['nature'],
           prompt='将森林素材 0–5 秒做左右分屏，同步播放；左侧保持原色，右侧暖调电影感，各自裁满区域，输出960×540。先给方案。',
           options=['左右分屏、同步播放','左原色，右暖调','每格居中裁满，保留左侧音轨'],
           plan={'clips':[{'source':'nature','end':5,'fit':'cover'},
                          {'source':'nature','end':5,'fit':'cover','grade':'warm_cinematic'}],'layout':'horizontal'}),
      dict(id='local-mask',title='按时段打码，添加中文说明',category='局部打码与标题',
           description='固定矩形在指定时段出现马赛克，标题全程显示。',keys=['nature'],
           prompt='截取森林素材0–5秒，输出960×540。在成片 x=360、y=120、宽240、高160 的固定区域，从第1秒到第4秒加马赛克；底部全程显示“局部遮挡示例”。先给方案。',
           options=['固定矩形：360,120,240,160','马赛克：第 1–4 秒','中文标题全程显示；不跟踪目标'],
           plan={'clips':[{'source':'nature','end':5}],
                 'masks':[{'x':360,'y':120,'width':240,'height':160,'start':1,'end':4,'mode':'pixelate'}],
                 'titles':[{'text':'局部遮挡示例','size':34}]}),
      dict(id='local-music',title='配乐遇到对白，自动轻一点',category='配乐与混音',
           description='添加生成的测试旋律，对白期间压低配乐，停顿时恢复。',keys=['speech','music'],
           prompt='给完整9秒对白加入所附测试配乐，配乐音量0.3并循环，有人讲话时自动压低，开头淡入0.3秒，结尾淡出0.8秒。先给方案。',
           options=['保留对白＋测试旋律','配乐音量 0.3、循环','讲话时自动压低配乐'],
           plan={'clips':[{'source':'speech','end':9}],
                 'audio':{'source':'music','volume':.3,'loop':True,'duck':True,'fade_in':.3,'fade_out':.8}}),
      dict(id='local-audio-replace',title='换掉原声，安排新的声音',category='音频替换',
           description='移除对白原声，新音轨延迟一秒开始并循环到结束。',keys=['speech','music'],
           prompt='将9秒对白视频的声音替换为所附测试旋律，音乐延迟1秒开始，循环至结束，音量0.5、结尾淡出0.5秒。先给方案。',
           options=['替换原声','音乐延迟 1 秒、循环','结尾淡出 0.5 秒'],
           plan={'clips':[{'source':'speech','end':9}],
                 'audio':{'source':'music','mode':'replace','offset':1,'loop':True,'volume':.5,'fade_out':.5}}),
      dict(id='local-audio-clean',title='提取对白，减轻持续底噪',category='音频提取与降噪',
           description='对人为添加底噪的对白做基础处理，直接试听导出的音频。',keys=['noisy'],
           prompt='从这份人为添加底噪的演示视频提取完整9秒音频，做基础降噪和80Hz高通，首尾各淡化0.1秒，导出WAV。先给方案。',
           options=['演示输入含人为添加的底噪','FFT 基础降噪＋80 Hz 高通','输出 WAV；可对比试听'],
           plan={'clips':[{'source':'noisy','end':9}],
                 'output':{'audio_only':True,'format':'wav'},'audio':{'denoise':True,'highpass':80,'fade_in':.1,'fade_out':.1}}),
      dict(id='local-word-edit',title='按文字剪辑，让字幕逐词亮起',category='文字剪辑与字幕',
           description='复用真实逐词转写，剪去问答间长停顿，按新时间线高亮字幕。',keys=['speech'],
           prompt='使用已有转写，完整保留“What brings you to the land of the Gatekeepers?”和“I\'m searching for someone.”，去掉中间停顿，词边界留80毫秒；每行4词，字幕白色、已说到的词高亮黄色，输出960×540。先给方案。',
           options=['真实逐词缓存，无新转写','按词索引 0–8、9–12 保留两句','四词字幕，逐词黄色填色'],
           plan={'clips':[{'source':'speech','words':[0,8]},{'source':'speech','words':[9,12]}],
                 'transcripts':{'speech':'sources/speech.json'},
                 'subtitles':{'from_transcripts':True,'words_per_line':4,'size':32,'highlight':'#FFE066'}}),
    ]


def main():
    root=data_root()/'demos';sources=root/'sources'
    if hasattr(os,'sched_getaffinity'):os.sched_setaffinity(0,sorted(os.sched_getaffinity(0))[:4])
    def ffmpeg(*args):subprocess.run(['ffmpeg','-v','error','-y','-filter_threads','1',*map(str,args)],check=True)
    # Original generated test melody; no downloaded commercial music or model output.
    melody=sources/'local-test-melody.wav'
    ffmpeg('-f','lavfi','-i','aevalsrc=0.16*sin(2*PI*220*t)+0.10*sin(2*PI*330*t)+0.08*sin(2*PI*440*t):s=48000:d=4',
           '-af','afade=t=in:d=0.05,afade=t=out:st=3.9:d=0.1','-c:a','pcm_s16le',melody)
    noisy=sources/'local-noisy-speech.mp4'
    ffmpeg('-i',sources/'speech.mp4','-f','lavfi','-i','anoisesrc=color=white:amplitude=0.025:sample_rate=48000:duration=9:seed=7',
           '-filter_complex','[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]',
           '-map','0:v:0','-map','[a]','-c:v','copy','-c:a','aac',noisy)
    source_map={**SOURCE_MAP,'music':{'name':'生成的测试配乐.wav','file':melody.name},
                'noisy':{'name':'Sintel · 人为添加底噪的对白.mp4','file':noisy.name}}
    rendered=[]
    with tempfile.TemporaryDirectory(prefix='local-demo-build-',dir=data_root()/'tmp') as temporary:
        staging=Path(temporary)
        for demo in definitions():
            work=staging/demo['id'];(work/'edit').mkdir(parents=True);(work/'sources').mkdir()
            plan={'version':1,'output':{'width':960,'height':540,'fps':30},**demo.pop('plan')}
            keys=demo.pop('keys');plan['sources']={};demo['sources']=[]
            for key in keys:
                info=copy.deepcopy(source_map[key]);info['key']=key
                shutil.copyfile(sources/info['file'],work/'sources'/info['file'])
                plan['sources'][key]='sources/'+info['file'];demo['sources'].append(info)
                for transcript in info.get('transcripts',{}).values():
                    shutil.copyfile(sources/transcript,work/'sources'/transcript)
            (work/'edit/plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2))
            extension=plan.get('output',{}).get('format','mp4');filename='final.'+extension
            render_plan(validate(plan,work),Path('edit')/filename)
            public=work/'public';public.mkdir()
            for path in (work/'edit').iterdir():
                if path.is_file():shutil.copyfile(path,public/path.name)
            original=sources/source_map[keys[0]]['file'];shutil.copyfile(original,public/'before.mp4')
            poster_source=public/filename if extension=='mp4' else original
            ffmpeg('-ss','1','-i',poster_source,'-frames:v','1','-vf','scale=640:-2','-q:v','3',public/'poster.jpg')
            demo.update(workflow='agent',renderer='edit_media',preset={'kind':'agent','assets':keys},
                        result=filename,result_type='video/mp4' if extension=='mp4' else 'audio/wav',
                        poster='poster.jpg',before='before.mp4',tags=['本地处理','可组合编辑'],
                        credits=[BUNNY] if keys[0]=='nature' else [SINTEL],
                        changes='使用本机 edit_media.py 按随附 plan.json 实际生成。素材为已缓存的开放授权短片；测试配乐为本地合成音，降噪示例人为加入噪声；未调用模型或新转写。')
            demo['files']=[p.name for p in public.iterdir() if p.is_file()]
            rendered.append((demo,public))
        # Do not replace the catalogue until all examples have rendered successfully.
        catalog_path=root/'catalog.json';catalog=json.loads(catalog_path.read_text())
        ids={d['id'] for d,_ in rendered};catalog=[d for d in catalog if d['id'] not in ids]
        for demo,public in rendered:
            destination=root/demo['id'];destination.mkdir(exist_ok=True)
            for path in public.iterdir():
                temp=destination/(path.name+'.new');shutil.copyfile(path,temp);temp.replace(destination/path.name)
            catalog.append(demo)
        from .demo_agent_requirements import apply as apply_agent_requirements
        catalog,_=apply_agent_requirements(catalog)
        temp=catalog_path.with_suffix('.new.json');temp.write_text(json.dumps(catalog,ensure_ascii=False,indent=2));temp.replace(catalog_path)
        if os.geteuid()==0:
            account=pwd.getpwnam('video-use')
            paths=[catalog_path,melody,noisy]
            for demo,_ in rendered:
                folder=root/demo['id'];paths.extend([folder,*folder.iterdir()])
            for path in paths:
                os.chown(path,account.pw_uid,account.pw_gid)
                path.chmod(0o750 if path.is_dir() else 0o640)
    print(f'Published {len(rendered)} local editing examples; total {len(catalog)}.')


if __name__=='__main__':main()
