"""Build real, cached examples. Run only after downloading and transcribing sources.

No synthetic ASR output and no network calls: all transcripts are derived from a
single real, previously cached Scribe result. Source URLs and licenses live in
the catalog returned to the browser.
"""
from __future__ import annotations

import copy
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

from .app import inspect_media
from .models import JobInput
from .worker import execute
from .paths import data_root

ROOT = data_root() / 'demos'
SOURCES = ROOT / 'sources'


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *map(str, args)], check=True)


def cut_transcript(data, start, end):
    if not all(math.isfinite(value) for value in (start,end)) or start<0 or end<=start:
        raise ValueError('Invalid transcript clip interval')
    result = copy.deepcopy(data)
    result['source_clip'] = {'start':start,'end':end,'word_timebase':'relative_to_clip_start',
                             'audio_duration_secs':data.get('audio_duration_secs')}
    result['audio_duration_secs'] = end-start
    result['words'] = []
    for word in data['words']:
        a, b = word.get('start'), word.get('end')
        if a is None or b is None or b <= start or a >= end:
            continue
        word = dict(word)
        word['start'], word['end'] = max(0, a-start), min(end-start, b-start)
        result['words'].append(word)
    result['text'] = ''.join(w.get('text', '') for w in result['words'])
    return result


def prepare():
    original = json.loads((ROOT / 'edit' / 'transcripts' / 'dialogue.json').read_text())
    ffmpeg('-ss', 8.8, '-i', SOURCES/'dialogue.mp4', '-t', 9, '-c:v', 'libx264', '-preset', 'fast',
           '-crf', 20, '-c:a', 'aac', '-movflags', '+faststart', SOURCES/'speech.mp4')
    speech = cut_transcript(original, 8.8, 17.8)
    (SOURCES/'speech.json').write_text(json.dumps(speech, ensure_ascii=False, indent=2))
    for name, a, b in [('question',0,4.4),('answer',4.4,9)]:
        ffmpeg('-ss', a, '-i', SOURCES/'speech.mp4', '-t', b-a, '-c:v', 'libx264', '-preset', 'fast',
               '-crf', 20, '-c:a', 'aac', '-movflags', '+faststart', SOURCES/f'{name}.mp4')
        (SOURCES/f'{name}.json').write_text(json.dumps(cut_transcript(speech,a,b), ensure_ascii=False))
    ffmpeg('-i', SOURCES/'speech.mp4', '-af', 'volume=0.12', '-c:v', 'copy', '-c:a', 'aac',
           '-movflags', '+faststart', SOURCES/'quiet.mp4')
    ffmpeg('-i', SOURCES/'speech.mp4', '-f', 'lavfi', '-i', 'sine=frequency=160:sample_rate=48000',
           '-map', '0:v:0', '-map', '1:a:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac',
           '-filter:a:0', 'volume=0.08', '-t', 9, '-metadata:s:a:0', 'title=Demo background tone',
           '-metadata:s:a:1', 'title=Original dialogue', '-movflags', '+faststart', SOURCES/'multitrack.mp4')
    ffmpeg('-i', SOURCES/'speech.mp4', '-vf', 'scale=320:-2,pad=340:200:10:10:color=white',
           '-an', '-c:v', 'libx264', '-crf', 20, '-movflags', '+faststart', SOURCES/'inset.mp4')
    ffmpeg('-i', SOURCES/'nature.mp4', '-vf', 'eq=brightness=-0.06:contrast=0.94',
           '-an', '-c:v', 'libx264', '-crf', 20, '-movflags', '+faststart', SOURCES/'dim.mp4')


BUNNY = {'name': 'Big Buck Bunny · © 2008 Blender Foundation',
         'url': 'https://peach.blender.org/about/', 'license': 'CC BY 3.0',
         'license_url': 'https://creativecommons.org/licenses/by/3.0/',
         'download_url': 'https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/720/Big_Buck_Bunny_720_10s_1MB.mp4'}
SINTEL = {'name': 'Sintel · © 2010 Blender Foundation',
          'url': 'https://durian.blender.org/', 'license': 'CC BY 3.0',
          'license_url': 'https://creativecommons.org/licenses/by/3.0/',
          'download_url': 'https://media.w3.org/2010/05/sintel/trailer.mp4'}
SOURCE_MAP = {
    'speech': {'name':'Sintel · 双人对白.mp4', 'file':'speech.mp4','transcripts':{'0':'speech.json'}},
    'question': {'name':'对白片段 A · 提问.mp4','file':'question.mp4','transcripts':{'0':'question.json'}},
    'answer': {'name':'对白片段 B · 回答.mp4','file':'answer.mp4','transcripts':{'0':'answer.json'}},
    'nature': {'name':'Big Buck Bunny · 森林.mp4','file':'nature.mp4'},
    'quiet': {'name':'Sintel · 降低音量的演示副本.mp4','file':'quiet.mp4','transcripts':{'0':'speech.json'}},
    'multi': {'name':'Sintel · 双音轨演示.mp4','file':'multitrack.mp4','transcripts':{'1':'speech.json'}},
    'inset': {'name':'Sintel · 小窗叠加素材.mp4','file':'inset.mp4'},
    'dim': {'name':'森林 · 降低曝光的演示副本.mp4','file':'dim.mp4'},
}


def segment(asset, start, end, audio_track=0):
    return {'asset':asset,'start':start,'end':end,'audio_track':audio_track}


def definitions():
    return [
      dict(id='transcribe',title='把对白变成逐词文字稿',category='语音转写',
           description='识别两位角色的对白，保留每个词的时间与说话人。',tags=['双人对白','逐词时间戳'],
           prompt='请转写这段英文对白，区分两位说话人，保留逐词时间戳和声音事件。',
           options=['语言：英语','说话人数：2','音轨：0','复用已完成的真实转写'],
           preset={'kind':'transcribe','assets':['speech'],'language':'en','speakers':2},
           keys=['speech'],result='transcript.txt',result_type='text/plain',credits=[SINTEL]),
      dict(id='batch',title='多段素材，整理成一份笔记',category='批量整理',
           description='将提问与回答两段素材整理成带时间坐标的文字笔记。',tags=['两段素材','停顿分段'],
           prompt='把两段已转写的素材整理为一份文字稿，按说话人和超过 0.5 秒的停顿分段。',
           options=['素材：提问、回答，共 2 段','分段停顿：0.5 秒','输出：Markdown'],
           preset={'kind':'pack','assets':['question','answer'],'silence':.5},
           keys=['question','answer'],result='takes_packed.md',result_type='text/markdown',credits=[SINTEL]),
      dict(id='cuts',title='去掉停顿，让对白更紧凑',category='片段剪切',
           description='保留完整问答，去掉中间等待，观察剪切点的声音衔接。',tags=['两段拼接','切点淡入淡出'],
           prompt='保留完整提问和回答，删掉中间的停顿，给每个切点加短暂的声音淡入淡出。',
           options=['保留：0.36–2.64 秒、6.80–8.25 秒','逐词边界外保留约 80 毫秒','720p 草稿 · 统一音量'],
           preset={'kind':'render','segments':[segment('speech',.36,2.64),segment('speech',6.8,8.25)]},
           keys=['speech'],result='final.mp4',result_type='video/mp4',credits=[SINTEL]),
      dict(id='grade',title='给森林加一点电影感',category='色彩风格',
           description='对比原片与暖色调结果，查看色彩、反差和暗部变化。',tags=['暖调电影感','原片对比'],
           prompt='把这段森林画面调成暖调电影风格，保留自然层次，输出 8 秒预览。',
           options=['调色：暖调电影感','片段：0–8 秒','720p 草稿 · 30 fps'],
           preset={'kind':'render','segments':[segment('nature',0,8)],'grade':'warm_cinematic','fps':'30','normalize_audio':False},
           keys=['nature'],result='final.mp4',result_type='video/mp4',credits=[BUNNY]),
      dict(id='subtitles',title='让字幕跟上每一句对白',category='自动字幕',
           description='用真实转写生成字幕，并按剪辑后的时间线烧录到视频。',tags=['双词字幕','自动对齐'],
           prompt='给这段对白加英文字幕，每次显示两个词，按说话时间对齐，并导出字幕文件。',
           options=['字幕来源：缓存的逐词转写','内置双词大写样式','输出：视频 + SRT','片段：0–8.8 秒'],
           preset={'kind':'render','segments':[segment('speech',0,8.8)],'subtitles':'transcript'},
           keys=['speech'],result='final.mp4',result_type='video/mp4',credits=[SINTEL]),
      dict(id='overlay',title='把第二个画面叠加进来',category='视频叠加',
           description='主画面播放森林，小窗在第 1 秒出现，持续展示 5 秒。',tags=['定时出现','小窗叠加'],
           prompt='在森林画面左上角叠加一段对白小窗，从第 1 秒出现，持续 5 秒。',
           options=['主片段：0–8 秒','叠加素材：340 × 200 小窗','出场：1 秒，持续：5 秒','叠加素材音频不混入主音轨'],
           preset={'kind':'render','segments':[segment('nature',0,8)],'overlays':[{'asset':'inset','start':1,'duration':5}],'normalize_audio':False},
           keys=['nature','inset'],result='final.mp4',result_type='video/mp4',credits=[BUNNY,SINTEL]),
      dict(id='loudness',title='把过小的声音调整清楚',category='音量处理',
           description='将特意降低音量的对白副本进行响度归一化，可试听前后效果。',tags=['−14 LUFS','两遍分析'],
           prompt='这段对白音量太小，请统一到 −14 LUFS，并限制峰值，保持视频内容不变。',
           options=['输入：原对白音量 × 0.12 的演示副本','目标响度：−14 LUFS','真峰值：−1 dBTP','片段：0–8.8 秒'],
           preset={'kind':'render','segments':[segment('quiet',0,8.8)],'normalize_audio':True},
           keys=['quiet'],result='final.mp4',result_type='video/mp4',credits=[SINTEL]),
      dict(id='timeline',title='把画面、波形和词放在一起',category='时间线预览',
           description='一张图看清两位角色的对白、声音停顿与对应画面。',tags=['6 帧采样','语音波形'],
           prompt='生成 0 到 8.8 秒的时间线，采样 6 帧，并叠加转写中的词和静音区间。',
           options=['范围：0–8.8 秒','画面采样：6 帧','自动读取缓存转写','输出：PNG'],
           preset={'kind':'timeline','assets':['speech'],'start':0,'end':8.8,'frames':6},
           keys=['speech'],result='timeline.png',result_type='image/png',credits=[SINTEL]),
      dict(id='analysis',title='先分析曝光，再决定调色',category='画面分析',
           description='测量亮度、反差和饱和度，为偏暗的画面给出轻量校正建议。',tags=['亮度分析','自动校正建议'],
           prompt='分析这段偏暗素材的曝光、对比度和饱和度，给出自动校正建议，暂不渲染。',
           options=['输入：人为降低曝光的演示副本','分析范围：0–8 秒','输出：测量数据与建议滤镜'],
           preset={'kind':'analyze','assets':['dim'],'start':0,'end':8},
           keys=['dim'],result='analysis.json',result_type='application/json',credits=[BUNNY]),
      dict(id='multitrack',title='选对音轨，只转写人声',category='多音轨选择',
           description='演示素材含背景音与对白两条音轨，选择第二条识别人声。',tags=['指定音轨 1','跳过背景音'],
           prompt='这段视频的第一条音轨是演示背景音，请选择第二条对白音轨转写，并区分两位说话人。',
           options=['音轨 0：生成的背景测试音','音轨 1：原片对白','语言：英语，说话人：2','复用相同对白的转写缓存'],
           preset={'kind':'transcribe','assets':['multi'],'audio_track':1,'language':'en','speakers':2},
           keys=['multi'],result='transcript.txt',result_type='text/plain',credits=[SINTEL]),
    ]


def main():
    if hasattr(os, 'sched_getaffinity'):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:4])
    prepare()
    catalog = []
    for demo in definitions():
        print('Building demo:', demo['id'], flush=True)
        output = ROOT / demo['id']
        output.mkdir(exist_ok=True)
        workspace = ROOT / '_build' / demo['id']
        (workspace/'sources').mkdir(parents=True, exist_ok=True)
        (workspace/'edit'/'transcripts').mkdir(parents=True, exist_ok=True)
        assets = []
        demo['sources'] = []
        for key in demo.pop('keys'):
            item = dict(SOURCE_MAP[key], key=key)
            src = SOURCES / item['file']
            filename = key + src.suffix
            shutil.copyfile(src,workspace/'sources'/filename)
            for track, tr in item.get('transcripts',{}).items():
                suffix = f'.track{track}' if int(track) else ''
                shutil.copyfile(SOURCES/tr,workspace/'edit'/'transcripts'/f'{key}{suffix}.json')
            assets.append({'id':key,'name':item['name'],'filename':filename,'metadata':inspect_media(src)})
            demo['sources'].append(item)
        payload = JobInput(**demo['preset']).model_dump()
        artifacts = execute({'project_dir':str(workspace),'output_dir':str(output),'payload':payload,'assets':assets})
        assert demo['result'] in artifacts
        original = SOURCES / demo['sources'][0]['file']
        shutil.copyfile(original,output/'before.mp4')
        poster_source = output/demo['result'] if demo['result_type'].startswith('video/') else original
        frame_time = min(1.2,inspect_media(poster_source)['duration'] / 2)
        ffmpeg('-ss',frame_time,'-i',poster_source,'-frames:v',1,'-vf','scale=640:-2','-q:v',3,output/'poster.jpg')
        if demo['id']=='timeline':
            # Keep the grid readable while the full waveform opens in the detail view.
            from PIL import Image
            with Image.open(output/'timeline.png') as image:
                image.resize((960,270)).save(output/'poster.jpg',quality=90)
        demo['files'] = [*artifacts,'before.mp4','poster.jpg']
        demo['poster'],demo['before']='poster.jpg','before.mp4'
        demo['changes']='为功能演示截取、缩放或重新编码；调色、降低音量、叠加等修改见本示例参数。对白时间戳来自一次真实的 ElevenLabs 转写。'
        catalog.append(demo)
        shutil.rmtree(workspace)
    target=ROOT/'catalog.json'
    # Preserve separately built chat-workflow examples (agent renders and local
    # edit_media examples) when rebuilding the original helper demonstrations.
    if target.exists():
        catalog = [d for d in json.loads(target.read_text()) if d.get('workflow') == 'agent'] + catalog
    temp=ROOT/'catalog.new.json'
    temp.write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(target)
    print(f'Published {len(catalog)} demos to {target}',flush=True)


if __name__=='__main__':
    main()
