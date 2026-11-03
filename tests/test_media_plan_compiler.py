import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from webapp.media_plan_compiler import compile_sequence,probe,resource_plan
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

    def test_incompatible_inputs_are_normalized_sequentially(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);first=root/'a.mp4';second=root/'b.mp4';make_clip(first);make_clip(second,'80x60',660)
            report=compile_sequence([first,second],root/'final.mp4',root/'work',width=64,height=48,fps='10',threads=1)
            self.assertEqual(report['strategy'],'normalize_then_concat')
            output=probe(root/'final.mp4')
            self.assertEqual(output['signature']['video']['width'],64)
            self.assertEqual(output['signature']['video']['height'],48)
            self.assertEqual(len([item for item in report['commands'] if item['phase']=='normalize']),2)

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


if __name__=='__main__':unittest.main()
