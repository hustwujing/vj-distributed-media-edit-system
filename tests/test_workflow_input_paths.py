"""Real nested subprocesses must retain bound inputs after changing cwd."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

REPO=Path(__file__).resolve().parents[1]


class WorkflowInputPathTests(unittest.TestCase):
    def test_accepted_audio_remains_audio_when_bound_to_registered_evidence(self):
        import wave,os
        from tests.test_workflows import WorkflowTests,local_step
        from webapp import workflows
        from webapp.workflow_inputs import staged_assets
        from webapp.media_handlers import execute
        fixture=WorkflowTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        producer=local_step('produce');producer.update(outputs=['audio.wav'],output_roles={'audio':'audio.wav'})
        consumer={'id':'measure','name':'Measure accepted audio','kind':'operation','operation':'media.evidence',
            'depends_on':['produce'],'inputs':{'source':{'step':'produce','role':'audio'}},'parameters':{'mode':'audio'},
            'goal':'Measure the accepted audio','on_failure':'pause','acceptance':[{'kind':'json','file':'evidence-report.json'}]}
        fixture.submit([producer,consumer]);run=fixture.get()
        workspace=fixture.store.root/'workspace';sources=workspace/'sources';sources.mkdir(parents=True)
        relative=f'edit/workflows/{run["id"]}/produce/output/audio.wav';path=workspace/relative;path.parent.mkdir(parents=True)
        with wave.open(str(path),'wb') as audio:
            audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000);audio.writeframes(b'\x00\x10'*8000)
        checked=subprocess.run([sys.executable,str(REPO/'webapp/quality_check.py')],cwd=workspace,
            input=json.dumps({'files':[relative],'acceptance':[{'kind':'media','audio_track_present':True}]}),
            capture_output=True,text=True,check=True)
        quality=json.loads(checked.stdout);self.assertTrue(quality['passed']);quality['output_roles']={'audio':relative}
        self.assertNotIn('audio',quality['files'][0])
        workflows.change(fixture.store,run['id'],'produce',status='succeeded',result=[relative],quality=quality)
        run=fixture.get();task=next(t for t in run['steps'] if t['id']=='measure')
        environment={'workspace':workspace,'sources':sources,'assets':[],'uid':os.getuid(),'gid':os.getgid()}
        assets,roles=staged_assets(fixture.store,run,task,environment)
        source=next(a for a in assets if a['id']==roles['source'])
        self.assertEqual(source['metadata']['kind'],'audio')
        output=workspace/'evidence'
        execute({'project_dir':str(workspace),'output_dir':str(output),'assets':assets,'input_roles':roles,
            'explicit_inputs':True,'payload':{'kind':'evidence','assets':[roles['source']],'mode':'audio'}})
        report=json.loads((output/'evidence-report.json').read_text())
        self.assertEqual(report['source_sha256'],quality['files'][0]['sha256'])
        # A correct type must not weaken the immutable-input hash guard.
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'验收版本'):
            staged_assets(fixture.store,run,task,environment)

    def test_raw_environment_and_sdk_inputs_survive_output_directory_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'sources').mkdir();(root/'edit').mkdir()
            source=root/'sources'/'input.png';Image.new('RGB',(17,13),'blue').save(source)
            script=root/'edit'/'copy.py'
            script.write_text(
                'import os,sys,json,shutil\nfrom pathlib import Path\n'
                f'sys.path.insert(0,{str(REPO/"webapp")!r})\nfrom step_sdk import input_file\n'
                'os.chdir(os.environ["VIDEO_USE_STEP_OUTPUT"])\n'
                'raw=Path(json.loads(os.environ["VIDEO_USE_STEP_INPUTS"])["image"][0])\n'
                'sdk=input_file("image")\nassert raw.is_absolute() and sdk==raw\n'
                'scratch=Path(os.environ["VIDEO_USE_STEP_SCRATCH"]);(scratch/"temporary.bin").write_bytes(b"discard")\n'
                'shutil.copyfile(sdk,"copy.png")\n'
                'Path("paths.json").write_text(json.dumps({"raw":str(raw),"sdk":str(sdk)}))\n')
            for index,binding in enumerate(['sources/input.png',str(source)]):
                with self.subTest(binding=binding):
                    output=f'edit/workflows/copy-{index}'
                    request={'mode':'local','output':output,'argv':[sys.executable,str(script)],
                             'inputs':{'image':[binding]},'outputs':['copy.png','paths.json']}
                    result=subprocess.run([sys.executable,str(REPO/'webapp/workflow_io.py')],
                        input=json.dumps(request),cwd=root,capture_output=True,text=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual((root/output/'copy.png').read_bytes(),source.read_bytes())
                    self.assertEqual(json.loads((root/output/'paths.json').read_text())['raw'],str(source))
                    self.assertFalse((root/output/'.scratch').exists())
