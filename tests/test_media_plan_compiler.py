import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from webapp.media_plan_compiler import compile_sequence,probe,resource_plan,MediaCompileError
from webapp.operations import catalog,validate_parameters


def make_clip(path,size='64x48',frequency=440):
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'color=c=blue:s={size}:r=10:d=0.4',
                    '-f','lavfi','-i',f'sine=frequency={frequency}:sample_rate=48000:duration=0.4',
                    '-c:v','libx264','-pix_fmt','yuv420p','-threads','1','-c:a','aac','-shortest',str(path)],check=True)


class CompilerTests(unittest.TestCase):
    def test_compatible_inputs_use_concat_demuxer(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);first=root/'a.mp4';second=root/'b.mp4';make_clip(first);make_clip(second)
            report=compile_sequence([first,second],root/'final.mp4',root/'work',threads=1)
            self.assertEqual(report['strategy'],'concat_demuxer')
            self.assertGreater(probe(root/'final.mp4')['duration'],.7)
            self.assertLessEqual(report['resources']['estimated_peak_tasks'],16)

    def test_incompatible_inputs_require_independent_normalization_before_concat(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);first=root/'a.mp4';second=root/'b.mp4';make_clip(first);make_clip(second,'80x60',660)
            with self.assertRaises(MediaCompileError) as rejected:
                compile_sequence([first,second],root/'final.mp4',root/'work',threads=1)
            self.assertEqual(rejected.exception.diagnostic['code'],'media_normalization_required')
            self.assertFalse((root/'final.mp4').exists())
            normalized=[]
            for index,source in enumerate((first,second)):
                output=root/f'normalized-{index}.mp4'
                clip=compile_sequence([source],output,root/f'work-{index}',width=64,height=48,fps='25',threads=1)
                self.assertEqual(clip['strategy'],'normalize_single_clip');normalized.append(output)
            report=compile_sequence(normalized,root/'final.mp4',root/'work',threads=1)
            self.assertEqual(report['strategy'],'concat_demuxer')
            output=probe(root/'final.mp4')
            self.assertEqual(output['signature']['video']['width'],64)
            self.assertEqual(output['signature']['video']['height'],48)
            self.assertEqual([item['phase'] for item in report['commands']],['concat'])

    def test_normalization_has_a_single_typed_input_and_explicit_profile(self):
        scope={'source':{'asset':'clip'}}
        params={'width':64,'height':48,'fps':'25'}
        self.assertEqual(validate_parameters('media.normalize_clip',params,scope)['threads'],2)
        with self.assertRaises(ValueError):validate_parameters('media.normalize_clip',params,{**scope,'extra':{'asset':'other'}})
        with self.assertRaises(ValueError):validate_parameters('media.normalize_clip',{'width':64,'height':48},scope)
        with self.assertRaises(ValueError):validate_parameters('media.concat_sequence',{'roles':['source'],'width':64},scope)

    def test_aac_priming_does_not_accumulate_in_normalized_checkpoints(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);source=root/'source.mp4';make_clip(source)
            normalized=root/'normalized.mp4'
            compile_sequence([source],normalized,root/'normalize',width=64,height=48,fps='25',threads=1)
            self.assertAlmostEqual(probe(normalized)['duration'],probe(source)['duration'],delta=.01)
            joined=root/'joined.mp4';compile_sequence([normalized]*24,joined,root/'join',threads=1)
            self.assertAlmostEqual(probe(joined)['duration'],24*probe(source)['duration'],delta=.04)

    def test_operation_roles_must_match_inputs(self):
        inputs={'first':{'asset':'a'},'second':{'asset':'b'}}
        value=validate_parameters('media.concat_sequence',{'roles':['first','second']},inputs)
        self.assertEqual(value['roles'],['first','second'])
        with self.assertRaises(ValueError):validate_parameters('media.concat_sequence',{'roles':['first']},inputs)
        operation=next(item for item in catalog()['operations'] if item['id']=='media.concat_sequence')
        self.assertEqual(operation['output_contract']['filenames'],['final.mp4','compiler-report.json'])

    def test_twenty_four_declared_inputs_fit_the_sequence_contract(self):
        from webapp.workflow_graph import Task
        inputs={f'clip_{index:02d}':{'asset':f'asset-{index}'} for index in range(24)}
        task=Task.model_validate({'id':'concat','type':'operation','name':'concat','objective':'ordered sequence',
            'operation':'media.concat_sequence','inputs':inputs,'parameters':{'roles':list(inputs)},
            'acceptance':[{'kind':'media','file':'final.mp4','decode':True}],'delivery_stage':True})
        self.assertEqual(len(task.inputs),24)

    def test_controlled_audio_process_writes_measurement_report(self):
        from webapp.media_handlers import execute
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);project=root/'project';sources=project/'sources';sources.mkdir(parents=True);output=root/'output'
            source=sources/'tone.wav'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=1',str(source)],check=True)
            manifest={'project_dir':str(project),'output_dir':str(output),'assets':[{'id':'bound','filename':'tone.wav'}],
                'input_roles':{'source':'bound'},'explicit_inputs':True,'payload':{'kind':'audio_process','assets':['bound'],
                'role':'source','format':'wav','gain_db':-3,'target_lufs':-18,'true_peak_db':-1.5,'fade_in':.05,'fade_out':.05,
                'highpass_hz':0,'denoise':False,'threads':1}}
            names=execute(manifest);self.assertEqual(names,['final.wav','audio-process-report.json'])
            report=json.loads((output/'audio-process-report.json').read_text())
            self.assertEqual(report['status'],'succeeded');self.assertIsNotNone(report['measurement'])
            self.assertFalse(report['output']['audio_silent'])

    def test_gain_only_preserves_channels_sample_grid_and_exact_gain(self):
        import numpy as np
        import wave
        from webapp.media_handlers import execute
        for channels in (1,2):
            with self.subTest(channels=channels),tempfile.TemporaryDirectory() as root:
                root=Path(root);project=root/'project';sources=project/'sources';sources.mkdir(parents=True)
                rate=32000;t=np.arange(rate)/rate
                samples=np.stack([np.sin(2*np.pi*(440+index*220)*t)*12000 for index in range(channels)],axis=1).astype('<i2')
                with wave.open(str(sources/'source.wav'),'wb') as audio:
                    audio.setnchannels(channels);audio.setsampwidth(2);audio.setframerate(rate);audio.writeframes(samples.tobytes())
                output=root/'output';parameters=validate_parameters('audio.process',{'gain_db':-6},{'source':{'asset':'bound'}})
                execute({'project_dir':str(project),'output_dir':str(output),'assets':[{'id':'bound','filename':'source.wav'}],
                    'input_roles':{'source':'bound'},'explicit_inputs':True,'payload':{'kind':'audio_process','assets':['bound'],**parameters}})
                with wave.open(str(output/'final.wav'),'rb') as audio:
                    self.assertEqual((audio.getnchannels(),audio.getframerate(),audio.getnframes()),(channels,rate,rate))
                    actual=np.frombuffer(audio.readframes(rate),dtype='<i2').reshape(-1,channels)
                expected=samples.astype(float)*10**(-6/20)
                self.assertLessEqual(float(np.max(np.abs(actual-expected))),1.1)
                report=json.loads((output/'audio-process-report.json').read_text())
                self.assertFalse(report['normalization_applied']);self.assertEqual(report['filters'],['volume=-6dB'])
                self.assertAlmostEqual(report['gain_measurement']['mean_delta_db'],-6,delta=.11)
                self.assertEqual(report['source_audio']['channels'],channels)

    def test_audio_fade_uses_decoded_samples_not_longer_video_duration(self):
        import numpy as np
        import wave
        from webapp.media_handlers import execute
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);sources=root/'sources';sources.mkdir();source=sources/'source.mkv'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=s=64x48:r=10:d=2',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1.25',
                '-c:v','libx264','-threads','1','-c:a','pcm_s16le',str(source)],check=True)
            output=root/'output';params=validate_parameters('audio.process',{'fade_out':.2},{'source':{'asset':'bound'}})
            execute({'project_dir':str(root),'output_dir':str(output),'assets':[{'id':'bound','filename':source.name}],
                'input_roles':{'source':'bound'},'explicit_inputs':True,'payload':{'kind':'audio_process','assets':['bound'],**params}})
            with wave.open(str(output/'final.wav'),'rb') as wav:
                self.assertEqual(wav.getnframes(),60000)
                samples=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(float)
            rms=lambda values:float(np.sqrt(np.mean(values**2)))
            self.assertLess(rms(samples[-480:])/rms(samples[24000:24480]),.04)
            report=json.loads((output/'audio-process-report.json').read_text())
            self.assertEqual(report['source_audio']['duration'],1.25)
            self.assertIn('afade=t=out:st=1.05:d=0.2',report['filters'])


if __name__=='__main__':unittest.main()
