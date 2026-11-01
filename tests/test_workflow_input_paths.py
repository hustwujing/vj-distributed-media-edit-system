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
