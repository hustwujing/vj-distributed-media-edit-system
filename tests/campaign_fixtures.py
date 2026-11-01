"""Deterministic QA media; only invoked explicitly, never by unit discovery."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from PIL import Image,ImageDraw,ImageFont

ROOT=Path(sys.argv[1]) if len(sys.argv)>1 else Path('.cluster/campaigns/20261031-real60/fixtures')
ROOT.mkdir(parents=True,exist_ok=True)


def ffmpeg(*args):
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',*map(str,args)],check=True,timeout=180)


def main():
    samples=Path('/data/wujing03/miniforge3/envs/reference_video_remix_ocr/lib/python3.10/site-packages/skimage/data')
    shutil.copyfile(samples/'astronaut.png',ROOT/'portrait.png')
    Image.open(samples/'rocket.jpg').convert('RGB').save(ROOT/'rocket.png')
    logo=Image.new('RGBA',(512,512),(0,0,0,0));draw=ImageDraw.Draw(logo)
    draw.rounded_rectangle((90,90,422,422),radius=70,fill=(0,92,230,255))
    draw.polygon([(145,165),(205,165),(256,290),(307,165),(367,165),(286,355),(226,355)],fill=(255,255,255,255))
    draw.ellipse((350,335,420,405),fill=(20,215,140,255));logo.save(ROOT/'logo_rgba.png')
    white=Image.new('RGBA',logo.size,'white');white.alpha_composite(logo);white.convert('RGB').save(ROOT/'logo_white.png')
    mask=Image.new('L',(512,512),0);ImageDraw.Draw(mask).ellipse((80,80,430,430),fill=255);mask.save(ROOT/'mask.png')
    poster=Image.new('RGB',(1024,768),'white');draw=ImageDraw.Draw(poster)
    font='/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    for y,text,size in ((80,'SUMMER RUN',100),(270,'BEACH CLUB',90),(470,'15 SECONDS',95)):
        draw.text((70,y),text,font=ImageFont.truetype(font,size),fill='black')
    poster.save(ROOT/'poster.png')
    import numpy as np,cv2
    source=np.asarray(poster);corners=np.float32([[100,80],[700,40],[650,520],[160,550]])
    transform=cv2.getPerspectiveTransform(np.float32([[0,0],[1023,0],[1023,767],[0,767]]),corners)
    warped=cv2.warpPerspective(source,transform,(800,600),borderValue=(128,128,128));Image.fromarray(warped).save(ROOT/'card_skew.png')
    calibration=Image.new('RGB',(640,360));draw=ImageDraw.Draw(calibration)
    for box,color in (((0,0,319,179),'red'),((320,0,639,179),'green'),((0,180,319,359),'blue'),((320,180,639,359),'yellow')):draw.rectangle(box,fill=color)
    calibration.save(ROOT/'color_grid.png')
    sentences=['One. The blue square moves from left to right.','Two. The green circle stays on the right.','Three. Keep the logo in the top right corner.','Four. The final video should end smoothly.']
    for i,(text,length) in enumerate(zip(sentences,[7,7,8,8])):
        txt=ROOT/f'voice-{i}.txt';txt.write_text(text)
        ffmpeg('-f','lavfi','-i',f'flite=textfile={txt.resolve()}:voice=slt','-af','apad','-t',length,'-ar','16000','-ac','1',ROOT/f'voice-{i}.wav')
    inputs=[]
    for i in range(4):inputs+=['-i',ROOT/f'voice-{i}.wav']
    ffmpeg(*inputs,'-filter_complex','[0:a][1:a][2:a][3:a]concat=n=4:v=0:a=1[a]','-map','[a]','-c:a','pcm_s16le',ROOT/'speech.wav')
    ffmpeg('-i',ROOT/'speech.wav','-f','lavfi','-i','anoisesrc=color=white:amplitude=0.018:sample_rate=16000:duration=30:seed=42','-filter_complex','[0:a][1:a]amix=inputs=2:normalize=0[a]','-map','[a]','-c:a','pcm_s16le',ROOT/'speech_noisy.wav')
    ffmpeg('-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=30','-af','volume=0.3','-c:a','pcm_s16le',ROOT/'reference_tone.wav')
    import wave
    sr=48000;audio=np.zeros(sr*30,dtype=np.float64);notes=[261.6256,329.6276,391.9954,329.6276,293.6648,349.2282,440.0,349.2282]
    for k in range(60):
        start=k*sr//2;length=min(sr,len(audio)-start);t=np.arange(length)/sr;frequency=notes[k%len(notes)]
        envelope=np.minimum(t/.015,1)*np.exp(-t*4)
        audio[start:start+length]+=.15*envelope*(np.sin(2*np.pi*frequency*t)+.25*np.sin(4*np.pi*frequency*t)+.08*np.sin(6*np.pi*frequency*t))
    with wave.open(str(ROOT/'music.wav'),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(sr);stream.writeframes((np.clip(audio,-1,1)*32767).astype('<i2').tobytes())
    ffmpeg('-i',ROOT/'speech.wav','-i',ROOT/'reference_tone.wav','-filter_complex','[0:a]aresample=48000,aformat=channel_layouts=mono[v];[1:a]aformat=channel_layouts=mono[m];[v][m]amerge=inputs=2[a]','-map','[a]','-c:a','pcm_s16le',ROOT/'stereo.wav')
    ffmpeg('-i',ROOT/'portrait.png','-i',ROOT/'stereo.wav','-vf',"scale=1280:1280,crop=1280:720:0:0,zoompan=z='min(zoom+0.0002,1.15)':d=750:s=640x360:fps=25",'-t','30','-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-movflags','+faststart',ROOT/'clip_a.mp4')
    ffmpeg('-i',ROOT/'rocket.png','-vf',"scale=1280:854,crop=1280:720:0:30,zoompan=z='min(zoom+0.0003,1.1)':d=300:s=640x360:fps=25",'-t','12','-an','-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-movflags','+faststart',ROOT/'clip_b.mp4')
    ffmpeg('-i',ROOT/'portrait.png','-vf',"scale=1024:1024,crop=576:1024:224:0,zoompan=z='min(zoom+0.0003,1.1)':d=300:s=360x640:fps=25",'-t','12','-an','-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-movflags','+faststart',ROOT/'portrait_clip.mp4')
    captions='\n\n'.join(f'{i+1}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{text}' for i,(start,end,text) in enumerate(zip([0,7,14,22],[6,13,21,29],sentences)))+'\n'
    (ROOT/'captions.srt').write_text(captions)
    record={'scope':'Real processing of public sample photos, generated motion videos and Flite speech; no simulated provider results',
       'photo_source':'https://scikit-image.org/docs/stable/api/skimage.data.html','astronaut_source':'NASA, public-domain sample distributed by scikit-image',
       'rocket_source':'SpaceX DSCOVR launch sample distributed by scikit-image; see data documentation',
       'speech_text':sentences,'files':[]}
    names=['portrait.png','rocket.png','card_skew.png','speech_noisy.wav','logo_rgba.png','logo_white.png','mask.png','poster.png','color_grid.png','speech.wav','music.wav','reference_tone.wav','stereo.wav','clip_a.mp4','clip_b.mp4','portrait_clip.mp4','captions.srt']
    for name in names:
        path=ROOT/name;record['files'].append({'name':name,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    (ROOT/'manifest.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'fixtures':len(names),'directory':str(ROOT),'bytes':sum(x['bytes'] for x in record['files'])}))


if __name__=='__main__':main()
