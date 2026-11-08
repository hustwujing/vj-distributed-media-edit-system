"""Observable media checks using tiny deterministic fixtures, never model calls."""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tests.isolation import temporary_root

import numpy as np

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'helpers'))
from edit_plan import validate,PlanError,probe
from edit_captions import cues_for_plan,parse_srt,caption_font
from edit_media import render_plan

TMP=temporary_root()
TMP.mkdir(parents=True,exist_ok=True)


def ffmpeg(*args):
    subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-filter_threads','1',*map(str,args)],check=True,capture_output=True)


def frame(path,t):
    data=subprocess.check_output(['ffmpeg','-v','error','-ss',str(t),'-i',str(path),'-frames:v','1',
                                  '-f','rawvideo','-pix_fmt','rgb24','-threads','1','-'])
    meta=probe(path)
    return np.frombuffer(data,np.uint8).reshape(meta['height'],meta['width'],3).astype(float)


def samples(path,start=0,length=None):
    args=['ffmpeg','-v','error','-ss',str(start),'-i',str(path)]
    if length is not None:args+=['-t',str(length)]
    data=subprocess.check_output(args+['-vn','-ac','1','-ar','48000','-f','f32le','-'])
    return np.frombuffer(data,np.float32)


@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
class LocalEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='media-ops-',dir=TMP);cls.root=Path(cls.temp.name)
        (cls.root/'sources').mkdir();(cls.root/'edit').mkdir()
        for color,frequency in [('red',440),('blue',880)]:
            ffmpeg('-f','lavfi','-i',f'color={color}:s=160x120:r=20:d=3','-f','lavfi','-i',f'sine=frequency={frequency}:sample_rate=48000:duration=3',
                   '-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac','-shortest',cls.root/'sources'/f'{color}.mp4')
        ffmpeg('-f','lavfi','-i','testsrc2=s=160x120:r=20:d=3','-c:v','libx264','-threads','1',cls.root/'sources/pattern.mp4')
        ffmpeg('-f','lavfi','-i','sine=frequency=1200:sample_rate=48000:duration=1','-c:a','pcm_s16le',cls.root/'sources/music.wav')
        ffmpeg('-f','lavfi','-i','aevalsrc=0.2*sin(2*PI*30*t)+0.1*sin(2*PI*440*t):s=48000:d=3',cls.root/'sources/hum.wav')
        # Voice alternates between silence and a tone. This is a signal-processing fixture.
        ffmpeg('-f','lavfi','-i',r"aevalsrc=if(between(t\,1\,2)\,0.35*sin(2*PI*440*t)\,0):s=48000:d=3",cls.root/'sources/voice.wav')
        (cls.root/'sources/words.json').write_text(json.dumps({'words':[
            {'type':'word','text':'One','start':.3,'end':.7,'speaker_id':'a'},
            {'type':'spacing','text':' '},
            {'type':'word','text':'two','start':.9,'end':1.2,'speaker_id':'a'},
            {'type':'word','text':'three','start':1.8,'end':2.2,'speaker_id':'b'}]}))

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def plan(self,**extra):
        return {'version':1,'sources':{'a':'sources/red.mp4','b':'sources/blue.mp4','pattern':'sources/pattern.mp4','music':'sources/music.wav'},
                'clips':[{'source':'a','start':0,'end':2}],
                'output':{'width':160,'height':120,'fps':20},**extra}

    def render(self,raw,name):
        p=validate(raw,self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            report=render_plan(p,Path('edit')/name)
        self.assertAlmostEqual(report['media']['duration'],p['_duration'],delta=.15)
        return self.root/'edit'/name,p

    def test_normal_speed_preserves_audio_samples_away_from_edge_fades(self):
        import wave
        # A varying waveform exposes unnecessary time-stretch alignment, unlike
        # a stationary sine that may accidentally correlate after a cycle shift.
        t=np.arange(3*48000)/48000
        signal=.18*np.sin(2*np.pi*(180*t+170*t*t))+.04*np.sin(2*np.pi*440*t)
        path=self.root/'sources/unity.wav'
        with wave.open(str(path),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(48000)
            output.writeframes(np.rint(signal*32767).astype('<i2').tobytes())
        out,_=self.render(self.plan(sources={'a':'sources/unity.wav'},clips=[{'source':'a','end':3}],
                                  output={'audio_only':True,'format':'wav'}),'unity.wav')
        before=samples(path,.1,2.8);after=samples(out,.1,2.8)
        self.assertEqual(len(before),len(after))
        self.assertLess(float(np.max(np.abs(before-after))),.0001)
        self.assertGreater(float(np.corrcoef(before,after)[0,1]),.99999)

    def test_report_exposes_actual_per_clip_fades_separately_from_master(self):
        raw=self.plan(sources={'a':'sources/music.wav'},
            clips=[{'source':'a','end':1},{'source':'a','end':1}],
            output={'audio_only':True,'format':'wav'},audio={'fade_in':.03,'fade_out':.03})
        out,_=self.render(raw,'cut-fade-audit.wav')
        # Continuous input tone has a clear dip only because both clip edges
        # fade. The internal cut is far from either global edge fade.
        signal=samples(out)
        def rms(a,b):return float(np.sqrt(np.mean(signal[int(a*48000):int(b*48000)]**2)))
        self.assertLess(rms(.995,1.005),rms(.90,.94)*.25)
        audit=json.loads(out.with_suffix('.report.json').read_text())['audio_processing']
        import hashlib
        self.assertEqual(audit['output_sha256'],hashlib.sha256(out.read_bytes()).hexdigest())
        self.assertEqual(len(audit['clip_filters']),2)
        for clip in audit['clip_filters']:
            self.assertIn('afade=t=in:d=0.03',clip['filters'])
            self.assertTrue(any(f.startswith('afade=t=out:st=0.970') for f in clip['filters']))
        self.assertFalse(any('loudnorm' in f for f in audit['master_filters']))

    def test_verification_decodes_actual_final_frame_at_fractional_timestamps(self):
        from PIL import Image
        for fps in (24,30,60):
            with self.subTest(fps=fps):
                source=self.root/'sources'/f'last-frame-{fps}.mp4'
                ffmpeg('-f','lavfi','-i',f'color=red:s=160x120:r={fps}:d=2',
                       '-vf',f"drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='eq(n,{2*fps-1})'",
                       '-c:v','libx264','-threads','1','-pix_fmt','yuv420p',source)
                out,_=self.render(self.plan(sources={'a':str(source)},clips=[{'source':'a','end':2}],
                    output={'width':160,'height':120,'fps':fps}),f'last-frame-{fps}.mp4')
                self.assertEqual(probe(out)['fps'],f'{fps}/1')
                first=np.asarray(Image.open(out.with_name(out.stem+'.verify_0.jpg')).convert('RGB')).mean(axis=(0,1))
                last=np.asarray(Image.open(out.with_name(out.stem+'.verify_2.jpg')).convert('RGB')).mean(axis=(0,1))
                self.assertGreater(first[0]-first[2],150)
                self.assertGreater(last[2]-last[0],150,'Verification skipped the final blue frame')

    def test_speed_preserves_pitch_and_av_duration(self):
        for speed in (.25,.5,2,4):
            with self.subTest(speed=speed):
                out,p=self.render(self.plan(clips=[{'source':'a','start':0,'end':2,'speed':speed}]),f'speed{speed}.mp4')
                self.assertAlmostEqual(probe(out)['duration'],2/speed,delta=.1)
                audio=samples(out,.1,min(.3,2/speed-.2))
                peak=np.fft.rfftfreq(len(audio),1/48000)[np.argmax(abs(np.fft.rfft(audio)))]
                self.assertAlmostEqual(peak,440,delta=12)

    def test_hard_cuts_and_transition_order(self):
        raw=self.plan(clips=[{'source':'a','end':1},{'source':'b','end':1}])
        out,_=self.render(raw,'cuts.mp4')
        self.assertGreater(frame(out,.4)[:,:,0].mean(),200)
        self.assertGreater(frame(out,1.4)[:,:,2].mean(),200)
        raw['transition']={'type':'fade','duration':.4}
        out,_=self.render(raw,'crossfade.mp4')
        middle=frame(out,.8).mean(axis=(0,1))
        self.assertGreater(middle[0],50);self.assertGreater(middle[2],50)
        self.assertAlmostEqual(probe(out)['duration'],1.6,delta=.1)

    def test_crossfade_retains_exact_frozen_frame_rate(self):
        raw=self.plan(clips=[{'source':'a','end':3},{'source':'b','end':3}],
            output={'width':160,'height':120,'fps':25},transition={'type':'fade','duration':1})
        out,p=self.render(raw,'crossfade-25.mp4')
        self.assertEqual(probe(out)['fps'],'25/1')
        frames=int(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
            '-show_entries','stream=nb_frames','-of','default=nw=1:nk=1',out],text=True).strip())
        self.assertEqual(frames,round(p['_duration']*25))

    def test_split_layout_uses_correct_cells_and_audio(self):
        for layout,n in [('horizontal',2),('vertical',2),('grid',4)]:
            raw=self.plan(layout=layout,split_audio=1,clips=[{'source':'a' if i%2==0 else 'b','end':1,'fit':'cover'} for i in range(n)])
            out,_=self.render(raw,layout+'.mp4');img=frame(out,.4)
            self.assertGreater(img[20,20,0],200)
            y,x=(90,80) if layout=='vertical' else (20,120)
            self.assertGreater(img[y,x,2],200)
            sound=samples(out,.2,.4)
            peak=np.fft.rfftfreq(len(sound),1/48000)[np.argmax(abs(np.fft.rfft(sound)))]
            self.assertAlmostEqual(peak,880,delta=10)

    def test_reverse_hold_repeat_and_silent_source(self):
        normal,_=self.render(self.plan(clips=[{'source':'pattern','end':1}]),'normal.mp4')
        reverse,_=self.render(self.plan(clips=[{'source':'pattern','end':1,'reverse':True,'freeze_end':.5,'repeat':2}]),'reverse.mp4')
        expected=frame(normal,.9);actual=frame(reverse,0)
        self.assertLess(np.abs(expected-actual).mean(),15)
        self.assertLess(np.abs(frame(reverse,1.1)-frame(reverse,1.35)).mean(),2)
        self.assertEqual(probe(reverse)['audio_tracks'],0)
        self.assertAlmostEqual(probe(reverse)['duration'],3,delta=.1)

    def test_all_muted_video_clips_produce_no_audio_track(self):
        out,_=self.render(self.plan(clips=[{'source':'a','end':1,'mute':True}]),'muted.mp4')
        self.assertEqual(probe(out)['audio_tracks'],0)

    def test_crop_rotate_flip_background_and_mask_timing(self):
        raw=self.plan(clips=[{'source':'pattern','end':1,'crop':[20,0,120,120],'rotate':90,'flip':'horizontal','fit':'blur'}],
                      output={'width':120,'height':160,'fps':20})
        base,_=self.render(raw,'geometry.mp4')
        raw['masks']=[{'x':20,'y':40,'width':50,'height':40,'start':.3,'end':.7,'mode':'black'}]
        out,_=self.render(raw,'masked.mp4')
        self.assertLess(frame(out,.5)[45:75,25:65].mean(),3)
        self.assertLess(np.abs(frame(out,.1)-frame(base,.1)).mean(),4)
        for mode in ('blur','pixelate'):
            raw['masks'][0]['mode']=mode
            processed,_=self.render(raw,mode+'.mp4')
            self.assertGreater(np.abs(frame(processed,.5)[40:80,20:70]-frame(base,.5)[40:80,20:70]).mean(),4)

    def test_subtitles_retime_speed_repeat_and_transition(self):
        raw=self.plan(transcripts={'a':'sources/words.json'},
            clips=[{'source':'a','words':[0,1],'speed':2,'repeat':2},{'source':'a','words':[2,2]}],
            transition={'type':'fade','duration':.1},
            subtitles={'from_transcripts':True,'words_per_line':2,'highlight':'#FFE066','size':16},
            titles=[{'text':'中文标题','position':'top','size':16}])
        p=validate(raw,self.root);cues=cues_for_plan(p)
        self.assertAlmostEqual(cues[0]['start'],.04,places=4)
        self.assertAlmostEqual(cues[1]['start']-cues[0]['start'],p['clips'][0]['_duration'])
        self.assertAlmostEqual(cues[-1]['start'],p['clips'][1]['_offset']+.08)
        out,_=self.render(raw,'captions.mp4')
        self.assertIn('One two',out.with_suffix('.srt').read_text())
        self.assertIn('\\kf',out.with_suffix('.ass').read_text())
        self.assertEqual(probe(out)['width'],160)

    def test_input_srt_is_last_and_timed(self):
        srt=self.root/'edit/input.srt';srt.write_text(
            '1\n00:00:00,300 --> 00:00:00,700\nhello\n你好\n\n'
            '2\n00:00:00,900 --> 00:00:01,300\nclipped\n\n'
            '3\n00:00:01,400 --> 00:00:01,600\noutside\n')
        raw=self.plan(clips=[{'source':'a','end':1}],subtitles={'file':'edit/input.srt','size':16},
                      masks=[{'x':0,'y':0,'width':160,'height':120,'mode':'black'}])
        out,_=self.render(raw,'srt.mp4')
        self.assertLess(frame(out,.1).max(),5)
        self.assertGreater(frame(out,.5).max(),150)
        cues=parse_srt(out.with_suffix('.srt'))
        self.assertEqual(cues[0]['text'],'hello\n你好')
        self.assertEqual(cues[1]['text'],'clipped')
        self.assertEqual(cues[1]['end'],1)
        self.assertNotIn('outside',out.with_suffix('.srt').read_text())
        strict=self.plan(clips=[{'source':'a','end':1}],subtitles={'file':'edit/input.srt','clip_to_output':False})
        with self.assertRaisesRegex(Exception,'end within'):cues_for_plan(validate(strict,self.root))

    def test_selected_font_has_distinct_chinese_glyphs(self):
        from PIL import ImageFont
        font=ImageFont.truetype(caption_font()[0],32)
        self.assertNotEqual(bytes(font.getmask('中')),bytes(font.getmask('文')))

    def test_audio_export_replace_loop_delay_and_denoise(self):
        for fmt in ('wav','mp3','m4a'):
            raw=self.plan(output={'audio_only':True,'format':fmt},audio={'source':'music','mode':'replace','volume':.8,'offset':.4,'loop':True})
            out,_=self.render(raw,'audio-'+fmt+'.'+fmt)
            self.assertFalse(probe(out)['video'])
            self.assertLess(np.abs(samples(out,.05,.2)).mean(),.001)
            self.assertGreater(np.abs(samples(out,1.5,.3)).mean(),.02)
        raw=self.plan(sources={'a':'sources/hum.wav'},output={'audio_only':True},audio={'denoise':True,'highpass':100})
        out,_=self.render(raw,'clean.wav')
        before=samples(self.root/'sources/hum.wav',.5,1);after=samples(out,.5,1)
        self.assertLess(abs(np.fft.rfft(after))[30],abs(np.fft.rfft(before))[30]*.2)

    def test_audio_source_window_and_timeline_placement_are_independent(self):
        ffmpeg('-f','lavfi','-i',r'aevalsrc=if(lt(t\,0.4)\,sin(2*PI*400*t)\,sin(2*PI*1200*t)):s=48000:d=1',
               self.root/'sources/offset-tones.wav')
        raw=self.plan(sources={'a':'sources/red.mp4','music':'sources/offset-tones.wav'},
            output={'audio_only':True,'format':'wav'},
            audio={'source':'music','mode':'replace','start':.5,'end':.8,'offset':1})
        out,_=self.render(raw,'source-window-placement.wav')
        self.assertLess(float(np.max(np.abs(samples(out,.1,.7)))),.001)
        active=samples(out,1.05,.2)
        frequency=np.fft.rfftfreq(len(active),1/48000)[np.argmax(np.abs(np.fft.rfft(active)))]
        self.assertAlmostEqual(frequency,1200,delta=5)
        self.assertLess(float(np.max(np.abs(samples(out,1.5,.3)))),.001)

    def test_music_ducking_reduces_music_during_voice(self):
        raw=self.plan(sources={'a':'sources/voice.wav','music':'sources/music.wav'},clips=[{'source':'a','end':3,'repeat':3}],
                      output={'audio_only':True},audio={'source':'music','volume':1,'loop':True,'duck':True})
        out,_=self.render(raw,'duck.wav')
        def tone_level(t):
            data=samples(out,t,.25);spectrum=np.abs(np.fft.rfft(data))/len(data)
            return spectrum[round(1200*.25)]
        self.assertLess(tone_level(1.5),tone_level(.5)*.65)
        report=json.loads(out.with_suffix('.report.json').read_text())['audio_audit']
        self.assertEqual(report['origin'],'fixed_ffmpeg_filter_branches')
        import hashlib
        self.assertEqual(report['output_sha256'],hashlib.sha256(out.read_bytes()).hexdigest())
        signal=report['signals']['background_ducked']
        during=next(item['rms_dbfs'] for item in signal['windows'] if 1.4<=item['time_seconds']<=1.6)
        quiet=next(item['rms_dbfs'] for item in signal['windows'] if .4<=item['time_seconds']<=.6)
        self.assertLess(during,quiet-3)
        self.assertAlmostEqual(report['gain_measurement_db'],0,delta=.01)
        from types import SimpleNamespace
        from webapp.workflow_agents import inspect
        report_file=out.with_suffix('.report.json')
        preview=inspect(SimpleNamespace(root=self.root,shared_files=False),{'project_id':'test'},
            {'report.json':{'record':{'location':str(report_file.relative_to(self.root)),
             'sha256':hashlib.sha256(report_file.read_bytes()).hexdigest(),'size':report_file.stat().st_size}}},'report.json')
        self.assertFalse(preview['complete'])
        self.assertIn('signal_summary',preview['text']);self.assertIn('background_ducked',preview['text'])
        self.assertIn(report['output_sha256'],preview['text'])

    def test_audio_meter_reports_actual_gain_and_preserves_silence_without_nan(self):
        for volume in (.3,0):
            raw=self.plan(output={'audio_only':True},audio={'source':'music','mode':'replace','volume':volume,'loop':True})
            out,_=self.render(raw,f'meter-{volume}.wav')
            report=json.loads(out.with_suffix('.report.json').read_text())['audio_audit']
            self.assertEqual(report['parameters']['volume'],volume)
            self.assertFalse(report['signals']['background_scaled']['windows_truncated'])
            if volume:self.assertAlmostEqual(report['gain_measurement_db'],20*np.log10(volume),delta=.01)
            else:
                self.assertIsNone(report['signals']['background_scaled']['rms_dbfs'])
                self.assertEqual(float(np.max(np.abs(samples(out)))),0)
            json.dumps(report,allow_nan=False)

    def test_audio_meter_bounds_windows_without_losing_global_peaks(self):
        from edit_media import audio_meter_report
        path=self.root/'meter-long.txt'
        with path.open('w') as stream:
            for i in range(300):
                level=-1 if i==150 else -20
                stream.write(f'frame:{i} pts:{i*12000} pts_time:{i/4}\nlavfi.astats.Overall.RMS_level={level}\nlavfi.astats.Overall.Peak_level={level}\nlavfi.astats.Overall.Number_of_samples=12000\n')
        report=audio_meter_report(path)
        self.assertEqual(len(report['windows']),128);self.assertTrue(report['windows_truncated'])
        self.assertEqual(report['windows_total'],300);self.assertEqual(report['peak_dbfs'],-1)
        self.assertGreater(report['rms_dbfs'],-20)

    def test_invalid_plans_fail_before_output(self):
        examples=[]
        p=self.plan();p['clips'][0]['speeed']=2;examples.append(p)
        p=self.plan();p['clips'][0]['speed']=float('nan');examples.append(p)
        p=self.plan();p['sources']['a']='/etc/passwd';examples.append(p)
        p=self.plan();p['clips'][0]['crop']=[150,0,30,30];examples.append(p)
        p=self.plan();p['clips'][0]['end']=99;examples.append(p)
        p=self.plan();p['audio']={'duck':True};examples.append(p)
        p=self.plan();p['transcripts']={'a':'sources/words.json'};p['clips'][0]['start']=.5;examples.append(p)
        for p in examples:
            with self.subTest(plan=p),self.assertRaises(PlanError):validate(p,self.root)
        p=validate(self.plan(),self.root)
        with self.assertRaises(PlanError):render_plan(p,Path('sources/replaced.mp4'))
        out=self.root/'edit/keep.mp4';out.write_bytes(b'previous-render')
        with self.assertRaises(PlanError):render_plan(p,Path('edit/keep.mp4'))
        self.assertEqual(out.read_bytes(),b'previous-render')


if __name__=='__main__':unittest.main()
