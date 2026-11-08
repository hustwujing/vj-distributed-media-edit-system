import base64
from contextlib import chdir
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp import workflow_io


class WorkflowImportTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        self.output=self.root/'edit/workflows/result';self.output.mkdir(parents=True)
        self.enterContext(chdir(self.root))

    def request(self,name='report.json',data=b'{"ok":true}'):
        return {'mode':'import','output':'edit/workflows/result','files':[{'name':name,'data':base64.b64encode(data).decode()}]}

    def test_script_written_same_content_import_is_idempotent_and_replaceable(self):
        target=self.output/'report.json';target.write_bytes(b'{"ok":true}')
        for _ in range(2):workflow_io.execute(self.request())
        self.assertEqual(target.read_bytes(),b'{"ok":true}')
        workflow_io.execute(self.request(data=b'{"ok":false}'))
        self.assertEqual(target.read_bytes(),b'{"ok":false}')

    def test_symlink_and_hardlink_targets_cannot_overwrite_other_files(self):
        outside=self.root/'owned.txt';outside.write_bytes(b'original')
        for link in ('symlink','hardlink'):
            with self.subTest(link=link):
                target=self.output/'report.json'
                if link=='symlink':target.symlink_to(outside)
                else:os.link(outside,target)
                with self.assertRaises(ValueError):workflow_io.execute(self.request())
                self.assertEqual(outside.read_bytes(),b'original')
                target.unlink()

    def test_target_replaced_by_symlink_during_import_does_not_escape(self):
        outside=self.root/'owned.txt';outside.write_bytes(b'original')
        target=self.output/'report.json';target.write_bytes(b'old')
        replace=os.replace
        def raced(source,dest,**kwargs):
            target.unlink();target.symlink_to(outside)
            return replace(source,dest,**kwargs)
        with patch('webapp.workflow_io.os.replace',side_effect=raced):workflow_io.execute(self.request())
        self.assertEqual(outside.read_bytes(),b'original')
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_bytes(),b'{"ok":true}')

    def test_invalid_image_does_not_replace_previous_output(self):
        from PIL import UnidentifiedImageError
        target=self.output/'image.png';target.write_bytes(b'previous')
        with self.assertRaises(UnidentifiedImageError):workflow_io.execute(self.request('image.png',b'not an image'))
        self.assertEqual(target.read_bytes(),b'previous')
        self.assertFalse(list(self.output.glob('.import-*')))


class FinishFilesTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from tests import test_dynamic_graph as fixtures
        from webapp import workflow_graph as graph,step_checkpoints
        from webapp.task_leases import Coordinator
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store
        self.f.tick()
        self.f.propose({'proposal_id':'file-import','base_graph_version':0,'reason':'test output import',
            'add_tasks':[fixtures.script(outputs=['report.txt','notes.txt'],output_roles={'report':'report.txt','notes':'notes.txt'})],
            'replan_after':['first']})
        self.run=self.f.tick()
        self.lease=Coordinator(self.store,'file-import','test').claim(self.run['id'],'workflow',step_id='first')
        self.assertIsNotNone(self.lease);self.addCleanup(self.lease.release)
        self.task=graph.work_item(graph.get(self.store,self.run['id']),'first')
        self.task['spec']['profile']='editing_implementer'
        self.workspace=self.store.root/'isolated-workspace';self.workspace.mkdir()
        runtime=SimpleNamespace(lease=self.lease,store=self.store,workspace=self.workspace,
            project=self.store.project_dir(self.f.f.project),service_uid=os.getuid(),service_gid=os.getgid(),
            limit=1024**2,quiesce=lambda:None,revoked=lambda:False)
        runtime.checkpoint=lambda:step_checkpoints.save(runtime)
        self.lease.runtime=runtime
        env={'workspace':self.workspace,'uid':os.getuid(),'gid':os.getgid()}
        def isolated(env,payload,*args):
            with chdir(self.workspace):return workflow_io.execute(payload)
        def validate(view,step,files):
            from webapp.workflow_inputs import digest
            return {'technical_valid':True,'passed':True,'checks':[],
                    'files':[{'name':name,'kind':'text','sha256':digest(self.workspace/name),
                              'size':(self.workspace/name).stat().st_size} for name in files]}
        adapter=SimpleNamespace(environment=lambda view:env,isolated=isolated,validate=validate)
        self.host=SimpleNamespace(store=self.store,workflow_runner=SimpleNamespace(adapter=adapter))
        self.output=f'edit/workflows/{self.run["id"]}/first/result'

    def test_inline_result_is_imported_before_presence_check_and_stages_candidate(self):
        from webapp.workflow_agents import finish_files
        with self.lease.bind():finish_files(self.host,self.lease,self.run,self.task,{'report.txt':'report','notes.txt':'notes'},self.output)
        self.assertEqual((self.workspace/self.output/'report.txt').read_text(),'report')
        rows=self.store.query('SELECT * FROM task_results WHERE run_id=?',(self.run['id'],))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'staged')
        self.assertEqual(len(self.store.query('SELECT * FROM execution_step_checkpoints WHERE workflow=?',(self.run['id'],))),1)

    def test_missing_result_returns_structured_feedback_and_can_be_completed(self):
        from webapp.workflow_agents import finish_files
        from webapp.workflow_runtime import StepFailure
        with self.lease.bind(),self.assertRaises(StepFailure) as failure:
            finish_files(self.host,self.lease,self.run,self.task,{'report.txt':'report'},self.output)
        self.assertEqual(failure.exception.diagnostic['code'],'output_missing')
        self.assertEqual(failure.exception.diagnostic['missing'],['notes.txt'])
        self.assertFalse(self.store.query('SELECT * FROM task_results WHERE run_id=?',(self.run['id'],)))
        with self.lease.bind():finish_files(self.host,self.lease,self.run,self.task,{'report.txt':'report','notes.txt':'notes'},self.output)
        self.assertEqual(len(self.store.query('SELECT * FROM task_results WHERE run_id=?',(self.run['id'],))),1)
