#!/usr/bin/env python3
"""Local editing helper. Read references/local-editing.md before composing plans.

python helpers/edit_media.py capabilities
python helpers/edit_media.py validate edit/plan.json --workspace /path/to/footage
python helpers/edit_media.py render edit/plan.json --workspace /path/to/footage -o edit/final.mp4
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from edit_plan import PlanError, load_plan, probe
from edit_captions import write_captions, cues_for_plan, caption_font
from grade import get_preset

CAPABILITIES = {
    'version':1, 'model_required':False,
    'operations':['trim','word_selection','concat','speed','reverse','freeze_end','repeat',
                  'crop','rotate','flip','contain','cover','blur_background','split_screen',
                  'transition','pixelate','blur_region','black_region','title','subtitle_style',
                  'word_highlight','audio_extract','audio_replace','audio_mix','music_duck',
                  'audio_offset','audio_loop','audio_fades','denoise','loudness_normalize','highpass','grade'],
    'reference':'references/local-editing.md', 'plan_reference':'references/edit-plan.md',
    'limitations':['No model inference or new transcription.',
                   'Region masks use fixed output coordinates, not object tracking.',
                   'Reverse clips are limited to 60 seconds; clip speeds are constant per clip.',
                   'Supplied SRT uses output times; cached word captions are retimed automatically.']}


def emit(stage, **values):
    record=json.dumps({'stage':stage,**values},ensure_ascii=False)
    print(record,flush=True)
    # Callers often capture stdout as data and preserve only stderr on failure.
    if stage=='error':print(record,file=sys.stderr,flush=True)


def run_ffmpeg(args, stage, cwd, duration=0):
    """Argument vectors only; progress is real encoded output time, not a timer."""
    command=['ffmpeg','-hide_banner','-v','error','-nostdin','-y','-threads','2',
             '-filter_threads','1','-filter_complex_threads','1',*map(str,args),
             '-progress','pipe:1','-nostats']
    emit(stage,status='running')
    # File stderr prevents a long diagnostic from blocking the progress pipe.
    with tempfile.TemporaryFile(dir=cwd) as errors:
        process=subprocess.Popen(command,cwd=cwd,stdout=subprocess.PIPE,stderr=errors,text=True)
        last=0
        try:
            for line in process.stdout:
                if line.startswith('out_time_us=') and time.monotonic()-last>.7:
                    try:seconds=max(0,int(line.strip().split('=',1)[1])/1e6)
                    except ValueError:continue
                    emit(stage,seconds=round(seconds,3),duration=duration)
                    last=time.monotonic()
            code=process.wait()
        finally:
            if process.poll() is None:process.kill();process.wait()
            process.stdout.close()
        if code:
            errors.seek(0,2);errors.seek(max(0,errors.tell()-5000))
            raise RuntimeError(f'{stage}: '+errors.read().decode(errors='replace'))
    emit(stage,status='completed')


def encoding(p, video=True):
    return (['-c:v','libx264','-preset','veryfast','-crf',str(p['output']['crf']),
             '-pix_fmt','yuv420p','-threads','2'] if video else ['-vn'])+['-c:a','pcm_s16le','-ar','48000','-ac','2']


def tempo(rate):
    # Unity WSOLA processing can shift samples even without changing duration.
    if rate==1:return []
    # atempo within 0.5–2 keeps every factor in its high-quality range.
    filters=[]
    while rate>2:filters.append('atempo=2');rate/=2
    while rate<.5:filters.append('atempo=0.5');rate/=.5
    filters.append(f'atempo={rate:.10f}')
    return filters


def cell_size(p):
    w,h=p['output']['width'],p['output']['height']
    if p['layout'] in ('horizontal','grid'):w//=2
    if p['layout'] in ('vertical','grid'):h//=2
    return w,h


def extract(p,c,index,work):
    video=not p['output']['audio_only'];meta=p['_metadata'][c['source']]
    length=c['end']-c['start'];duration=c['_duration'];fps=p['output']['fps']
    args=['-ss',c['start'],'-t',length,'-i',p['sources'][c['source']]]
    silent=not meta['audio_tracks'] or c['mute'] or (c['reverse'] and not c['reverse_audio'])
    if silent:args+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
    filters=[]
    if video:
        w,h=cell_size(p)
        vf=['setpts=PTS-STARTPTS','scale=trunc(iw*sar/2)*2:ih','setsar=1']
        if 'crop' in c:
            x,y,cw,ch=c['crop'];vf.append(f'crop={cw}:{ch}:{x}:{y}')
        vf+= {0:[],90:['transpose=clock'],180:['hflip','vflip'],270:['transpose=cclock']}[c['rotate']]
        if c['flip']!='none':vf.append('hflip' if c['flip']=='horizontal' else 'vflip')
        if c['reverse']:vf.append('reverse')
        vf.append(f'setpts=(PTS-STARTPTS)/{c["speed"]}')
        if c['grade']!='none':vf.append(get_preset(c['grade']))
        filters.append(f'[0:{meta["video_stream"]}]'+','.join(vf)+'[shape]')
        if c['fit']=='blur':
            filters+=['[shape]split[bg][fg]',
                      f'[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[blur]',
                      f'[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2[front]',
                      '[blur][front]overlay=(W-w)/2:(H-h)/2[fit]']
        elif c['fit']=='cover':filters.append(f'[shape]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}[fit]')
        else:filters.append(f'[shape]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black[fit]')
        filters.append(f'[fit]fps={fps},tpad=stop_mode=clone:stop_duration={c["freeze_end"]+1/fps:.9f},trim=duration={duration:.9f},setpts=N/({fps}*TB),setsar=1,format=yuv420p[v]')
    af=['atrim=duration='+str(length),'asetpts=PTS-STARTPTS','aresample=48000',
        'aformat=sample_fmts=fltp:channel_layouts=stereo']
    if c['reverse'] and c['reverse_audio'] and not silent:af.append('areverse')
    if not silent:af+=tempo(c['speed'])
    af += [f'volume={c["volume"]}',f'apad=whole_dur={duration:.9f}',f'atrim=duration={duration:.9f}',
           'asetpts=N/SR/TB','afade=t=in:d=0.03',f'afade=t=out:st={max(0,duration-.03):.9f}:d=0.03']
    filters.append(('[1:a:0]' if silent else f'[0:a:{c["audio_track"]}]')+','.join(af)+'[a]')
    target=work/f'clip_{index:03}.mkv'
    args+=['-filter_complex',';'.join(filters)]+(['-map','[v]'] if video else [])+['-map','[a]',*encoding(p,video),'-t',duration,target]
    run_ffmpeg(args,f'clip:{index+1}',work,duration)
    if c['repeat']>1:
        repeated=work/f'repeated_{index:03}.mkv'
        concatenate([target]*c['repeat'],repeated,work,video)
        target=repeated
    return target


def concatenate(paths,target,work,video=True):
    # Generated basenames only: source names never enter concat's mini-language.
    listing=work/'concat.txt'
    listing.write_text(''.join(f"file '{path.name}'\n" for path in paths))
    run_ffmpeg(['-f','concat','-safe','1','-i',listing,'-map','0','-c','copy',target],'concat',work)


def assemble(p,paths,work):
    target=work/'assembled.mkv';video=not p['output']['audio_only'];duration=p['_duration']
    if len(paths)==1:return paths[0]
    if p['layout']=='sequence' and p['transition']['type']=='cut':
        concatenate(paths,target,work,video);return target
    args=[]
    for path in paths:args+=['-i',path]
    filters=[]
    if p['layout']!='sequence':
        w,h=cell_size(p)
        positions={'horizontal':f'0_0|{w}_0','vertical':f'0_0|0_{h}','grid':f'0_0|{w}_0|0_{h}|{w}_{h}'}[p['layout']]
        filters.append(''.join(f'[{i}:v]' for i in range(len(paths)))+f'xstack=inputs={len(paths)}:layout={positions}:shortest=1,setsar=1[v]')
        filters.append(f'[{p["split_audio"]}:a]atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[a]')
    else:
        d=p['transition']['duration'];total=p['clips'][0]['_duration']*p['clips'][0]['repeat']
        for i in range(len(paths)):
            if video:filters.append(f'[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[v{i}]')
            filters.append(f'[{i}:a]asetpts=PTS-STARTPTS[a{i}]')
        lastv,lasta='v0','a0'
        for i in range(1,len(paths)):
            if video:
                filters.append(f'[{lastv}][v{i}]xfade=transition={p["transition"]["type"]}:duration={d}:offset={total-d:.9f}[vx{i}]');lastv=f'vx{i}'
            filters.append(f'[{lasta}][a{i}]acrossfade=d={d}:c1=tri:c2=tri[ax{i}]');lasta=f'ax{i}'
            total+=p['clips'][i]['_duration']*p['clips'][i]['repeat']-d
        if video:filters.append(f'[{lastv}]null[v]')
        filters.append(f'[{lasta}]anull[a]')
    run_ffmpeg(args+['-filter_complex',';'.join(filters)]+(['-map','[v]'] if video else [])+
               ['-map','[a]',*encoding(p,video),'-t',duration,target],'transition' if p['layout']=='sequence' else 'split-screen',work,duration)
    return target


def title_image(item,width,height,path):
    from PIL import Image,ImageDraw,ImageFont
    fontpath,_=caption_font()
    font=ImageFont.truetype(fontpath,item['size'])
    image=Image.new('RGBA',(width,height));draw=ImageDraw.Draw(image)
    # Wrap by measured width, including Chinese and long unbroken words.
    lines=[]
    for paragraph in item['text'].split('\n'):
        line=''
        for char in paragraph:
            if line and draw.textlength(line+char,font=font)>width*.9:lines.append(line);line=''
            line+=char
        lines.append(line)
    spacing=max(3,item['size']//5);lineheight=item['size']+spacing
    blockheight=len(lines)*lineheight
    if blockheight>height*.85:raise PlanError('Title is too tall: reduce size/text or split it into timed titles')
    top={'top':height*.07,'center':(height-blockheight)/2,'bottom':height*.93-blockheight}[item['position']]
    if item['background']:draw.rounded_rectangle((width*.03,top-6,width*.97,top+blockheight+6),radius=5,fill=(0,0,0,170))
    for i,line in enumerate(lines):
        x=(width-draw.textlength(line,font=font))/2
        draw.text((x,top+i*lineheight),line,font=font,fill=item['color'],stroke_width=1,stroke_fill='black',anchor='lt')
    image.save(path,format='PNG')


def finish(p,base,work):
    video=not p['output']['audio_only'];duration=p['_duration'];out=p['output'];audio=p['audio']
    args=['-i',base];filters=[];nextinput=1;current='0:v';audio_label='0:a'
    if 'source' in audio:
        # First trim the requested music range. Loop this exact range, not the whole file.
        music=work/'music.wav';length=audio['end']-audio['start']
        run_ffmpeg(['-ss',audio['start'],'-t',length,'-i',p['sources'][audio['source']],'-map',f'0:a:{audio["audio_track"]}',
                    '-vn','-ar','48000','-ac','2','-c:a','pcm_s16le',music],'prepare-audio',work,length)
        if audio['loop']:args+=['-stream_loop','-1']
        args+=['-i',music];music_input=nextinput;nextinput+=1
        filters.append(f'[{music_input}:a]atrim=duration={duration-audio["offset"]:.9f},asetpts=PTS-STARTPTS,volume={audio["volume"]},adelay={round(audio["offset"]*1000)}:all=1,apad=whole_dur={duration:.9f},atrim=duration={duration:.9f}[music]')
        if audio['mode']=='replace':audio_label='music'
        else:
            background='music'
            if audio['duck']:
                filters+=['[0:a]asplit[voice][trigger]',
                          '[music][trigger]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=250:makeup=1[ducked]']
                audio_label='voice';background='ducked'
            filters.append(f'[{audio_label}][{background}]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixed]');audio_label='mixed'
    af=[]
    if 'source' not in audio:af.append(f'volume={audio["volume"]}')
    if audio['denoise']:af.append('afftdn=nf=-25')
    if audio['highpass']:af.append(f'highpass=f={audio["highpass"]}')
    if audio['normalize']:af.append('loudnorm=I=-16:TP=-1.5:LRA=11')
    af += [f'apad=whole_dur={duration:.9f}',f'atrim=duration={duration:.9f}',
           f'afade=t=in:d={audio["fade_in"]}',f'afade=t=out:st={duration-audio["fade_out"]:.9f}:d={audio["fade_out"]}',
           'alimiter=limit=0.95:level=false:latency=true','aresample=48000','asetpts=N/SR/TB']
    filters.append(f'[{audio_label}]'+','.join(af)+'[audio]')
    if video:
        for i,m in enumerate(p['masks']):
            x,y,w,h=m['x'],m['y'],m['width'],m['height'];enabled=f"between(t,{m['start']},{m['end']})"
            label=f'mask{i}'
            if m['mode']=='black':filters.append(f'[{current}]drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill:enable=\'{enabled}\'[{label}]')
            else:
                effect='pixelize=w=16:h=16' if m['mode']=='pixelate' else f'boxblur={max(1,min(w,h)//8)}:2'
                filters += [f'[{current}]split[original{i}][region{i}]',
                            f'[region{i}]crop={w}:{h}:{x}:{y},{effect}[treated{i}]',
                            f'[original{i}][treated{i}]overlay={x}:{y}:enable=\'{enabled}\'[{label}]']
            current=label
        for i,title in enumerate(p['titles']):
            path=work/f'title_{i}.png';title_image(title,out['width'],out['height'],path)
            args+=['-loop','1','-framerate',out['fps'],'-i',path]
            filters.append(f'[{nextinput}:v]setpts=PTS-STARTPTS+{title["start"]}/TB[title{i}]')
            filters.append(f'[{current}][title{i}]overlay=0:0:enable=\'between(t,{title["start"]},{title["end"]})\':eof_action=pass[text{i}]')
            current=f'text{i}';nextinput+=1
        if 'subtitles' in p:
            write_captions(p,work)
            # Relative generated ASCII filename avoids filtergraph path injection.
            filters.append(f'[{current}]ass=filename=captions.ass[subbed]');current='subbed'
    target=work/('result.'+out['format'])
    args+=['-filter_complex',';'.join(filters)]
    if video:
        args+=['-map',f'[{current}]' if current!='0:v' else '0:v','-map','[audio]']
        if current=='0:v':args+=['-c:v','copy']
        else:args+=['-c:v','libx264','-preset','veryfast','-crf',out['crf'],'-pix_fmt','yuv420p','-threads','2']
        # Matroska intermediates round timestamps to milliseconds. Quantize the
        # MP4 video track back onto the already-normalized CFR frame grid; this
        # preserves stream-copy quality and exact rates such as 24/60 fps.
        args+=['-video_track_timescale',out['fps'],'-c:a','aac','-b:a','192k','-movflags','+faststart']
    else:
        args+=['-map','[audio]','-vn']
        args+= {'wav':['-c:a','pcm_s16le'],'mp3':['-c:a','libmp3lame','-b:a','192k'],'m4a':['-c:a','aac','-b:a','192k','-movflags','+faststart']}[out['format']]
    run_ffmpeg(args+['-t',duration,target],'final-composite',work,duration)
    return target


def render_plan(p,output,keep_work=False):
    root=Path(p['_root']);edit=(root/'edit').resolve()
    if not edit.is_relative_to(root):raise PlanError('edit cannot point outside the workspace')
    output=(root/output).resolve()
    if not output.is_relative_to(edit) or output==edit:raise PlanError('Output must be inside workspace/edit/')
    if output.suffix.lower()!='.'+p['output']['format']:raise PlanError('Output extension must match output.format')
    protected={Path(v) for v in p['sources'].values()}|{Path(v) for v in p['transcripts'].values()}
    if 'file' in p.get('subtitles',{}):protected.add(Path(p['subtitles']['file']))
    if output in protected:raise PlanError('Cannot overwrite an input file')
    if output.exists():raise PlanError('Output already exists; choose a new filename for this iteration')
    sidecars=[output.with_suffix('.report.json')]
    if not p['output']['audio_only']:
        sidecars += [output.with_name(output.stem+f'.verify_{i}.jpg') for i in range(3)]
    if 'subtitles' in p:sidecars += [output.with_suffix(ext) for ext in ('.srt','.ass')]
    if any(path.exists() or path.is_symlink() for path in sidecars):raise PlanError('A sidecar exists; choose a new output name')
    if 'subtitles' in p:cues_for_plan(p)  # Fail before rendering for bad SRT.
    output.parent.mkdir(parents=True,exist_ok=True)
    work=Path(tempfile.mkdtemp(prefix='.media-edit-',dir=output.parent))
    try:
        clips=[extract(p,c,i,work) for i,c in enumerate(p['clips'])]
        base=assemble(p,clips,work)
        result=finish(p,base,work)
        actual=probe(result);fps=p['output']['fps']
        if abs(actual['duration']-p['_duration'])>max(.15,2/fps):raise RuntimeError('Output duration differs from the validated timeline')
        if not actual['audio_tracks']:raise RuntimeError('Output audio track is missing')
        if not p['output']['audio_only']:
            if (actual['width'],actual['height'])!=(p['output']['width'],p['output']['height']):raise RuntimeError('Output dimensions do not match the plan')
            from fractions import Fraction
            if abs(float(Fraction(actual['fps']))-fps)>.01:raise RuntimeError('Output frame rate does not match the plan')
            for i,t in enumerate((0,p['_duration']/2,max(0,p['_duration']-1/fps))):
                frame=work/f'verify_{i}.jpg'
                # Seeking to duration - 1/fps can round past the final PTS at
                # fractional frame intervals. Decode a bounded tail and select
                # its actual last frame; scale first to bound reverse's memory.
                seek=(['-sseof',-min(actual['duration'],max(1,3/fps))] if i==2 else ['-ss',t])
                filters='scale=640:-2'+(',reverse' if i==2 else '')
                run_ffmpeg([*seek,'-i',result,'-frames:v','1','-vf',filters,'-q:v','3',frame],f'verify-frame:{i+1}',work)
                if not frame.is_file():raise RuntimeError('Could not decode a verification frame')
        report={'status':'verified','duration_expected':p['_duration'],'media':actual,
                'checks':['duration','audio_track']+([] if p['output']['audio_only'] else ['dimensions','frame_rate','decoded_first_middle_last_frames']),
                'visual_review_required':not p['output']['audio_only']}
        (work/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        # Publish only verified files. No replacement of existing renders or sidecars.
        publications=[(result,output),(work/'report.json',output.with_suffix('.report.json'))]
        publications += [(path,output.with_name(output.stem+'.'+path.name)) for path in work.glob('verify_*.jpg')]
        publications += [(path,output.with_suffix(path.suffix)) for path in (work/'captions.srt',work/'captions.ass') if path.exists()]
        if any(dest.exists() or dest.is_symlink() for _,dest in publications):raise PlanError('A result or sidecar already exists; use a new output name')
        published=[]
        try:
            for source,dest in publications:
                # Hard-link within the same filesystem fails atomically if destination exists.
                os.link(source,dest);published.append(dest)
        except BaseException:
            for dest in published:dest.unlink(missing_ok=True)
            raise
        emit('complete',output=str(output.relative_to(root)),artifacts=[str(dest.relative_to(root)) for _,dest in publications],report=report)
        return report
    finally:
        if keep_work:emit('work-files',path=str(work))
        else:shutil.rmtree(work,ignore_errors=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['capabilities','validate','render'])
    parser.add_argument('plan',nargs='?')
    parser.add_argument('--workspace',type=Path,default=Path.cwd())
    parser.add_argument('-o','--output',type=Path)
    parser.add_argument('--keep-work',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.command=='capabilities':print(json.dumps(CAPABILITIES,ensure_ascii=False,indent=2));return 0
        if not args.plan:raise PlanError('A JSON plan is required')
        p=load_plan(args.plan,args.workspace)
        if 'subtitles' in p:cues_for_plan(p)
        if args.command=='validate':
            emit('validated',duration=p['_duration'],clips=[{'source':c['source'],'start':c['start'],'end':c['end'],
                 'output_start':c['_offset'],'duration':c['_duration']*c['repeat']} for c in p['clips']],output=p['output'])
        else:
            render_plan(p,args.output or Path('edit/final.'+p['output']['format']),args.keep_work)
        return 0
    except (PlanError,ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        emit('error',error=str(exc),error_type=type(exc).__name__,code='parameters' if isinstance(exc,ValueError) else 'execution');return 1


if __name__=='__main__':sys.exit(main())
