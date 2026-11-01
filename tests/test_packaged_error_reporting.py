"""Exercise failure entrypoints using only the staged tool package, no app fallback."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from webapp.runtime_image import stage_tools

class PackagedErrorReportingTests(unittest.TestCase):
    def run_failure(self,module,request):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);package=stage_tools(root);path=root/'request.json';path.write_text(json.dumps(request(root)))
            run=subprocess.run([sys.executable,'-m',module,str(path)],cwd=package,
                env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'PYTHONPATH':str(package),'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True,timeout=30)
            self.assertNotEqual(run.returncode,0)
            self.assertNotIn("No module named 'webapp.telemetry'",run.stderr)
            records=[]
            for line in run.stderr.splitlines():
                if line.startswith('{'):
                    try:records.append(json.loads(line))
                    except ValueError:pass
            errors=[r for r in records if r.get('event')=='tool.failure']
            self.assertEqual(len(errors),1,run.stderr)
            self.assertNotIn('private-test-input',json.dumps(errors))
            self.assertTrue(errors[0]['stack'])
            return errors[0]
    def test_image_worker_records_original_validation_failure(self):
        result=self.run_failure('webapp.image_tasks_cli',lambda root:{'task':{'action':'private-test-input'},'assets':[],'project':str(root),'output':str(root/'out')})
        self.assertEqual(result['exception_type'],'ValidationError')
    def test_media_worker_records_original_dispatch_failure(self):
        result=self.run_failure('webapp.worker',lambda root:{'project_dir':str(root),'output_dir':str(root/'out'),'assets':[],'payload':{'kind':'private-test-input'}})
        self.assertEqual(result['exception_type'],'ValueError')
