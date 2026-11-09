import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from tests.test_dynamic_graph import DynamicGraphTests
from webapp import native_sessions


class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=DynamicGraphTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.store=self.fixture.store;self.run=self.fixture.run
        self.task={'id':'code','revision':1}
        self.output=f'edit/workflows/{self.run["id"]}/code/output'
        self.files=Mock();self.data={};self.manifest={'workspace':{},'state':{}}
        for group,name,data in [('workspace',self.output+'/result.txt',b'approved scoped output'),
                                ('workspace','edit/other-task.txt',b'must not restore'),
                                ('state','codex/sessions/thread.jsonl',b'conversation'),
                                ('state','codex/state_5.sqlite-wal',b'wal'),
                                ('state','codex/auth.json',b'forbidden'),
                                ('state','claude/settings.json',b'forbidden')]:
            self.data['/'+group+'/'+name]=data
            self.manifest[group][name]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'executable':False}
        self.files.read.side_effect=self.data.__getitem__
        self.runtime=SimpleNamespace(store=self.store,lease=Mock(attempt='attempt'),frozen=True,
            session=SimpleNamespace(files=self.files),limit=1024**2,project=self.store.project_dir(self.run['project_id']))

    def test_session_is_task_revision_config_and_input_bound_and_credentials_are_excluded(self):
        native_sessions.save(self.runtime,self.run,self.task,'config','inputs','session',self.output,self.manifest)
        saved=native_sessions.load(self.store,self.run,self.task,'config','inputs')
        self.assertEqual(set(saved['manifest']['workspace']),{self.output+'/result.txt'})
        self.assertEqual(set(saved['manifest']['state']),{'codex/sessions/thread.jsonl','codex/state_5.sqlite-wal'})
        restored={}
        sink=Mock();sink.write.side_effect=lambda path,data,mode:restored.__setitem__(path,data.read())
        native_sessions.restore(self.runtime,saved,sink)
        self.assertEqual(restored['/workspace/'+self.output+'/result.txt'],b'approved scoped output')
        self.assertEqual(restored['/state/codex/state_5.sqlite-wal'],b'wal')
        self.assertIsNone(native_sessions.load(self.store,self.run,{**self.task,'revision':2},'config','inputs'))
        for config,inputs in [('changed','inputs'),('config','changed')]:
            with self.assertRaisesRegex(ValueError,'changed'):native_sessions.load(self.store,self.run,self.task,config,inputs)
        self.assertIsNone(native_sessions.load(self.store,{**self.run,'id':'another-run'},self.task,'config','inputs'))

    def test_restore_checks_all_hashes_before_writing_any_file(self):
        native_sessions.save(self.runtime,self.run,self.task,'config','inputs','session',self.output,self.manifest)
        saved=native_sessions.load(self.store,self.run,self.task,'config','inputs')
        record=saved['manifest']['state']['codex/state_5.sqlite-wal']
        (self.store.root/record['location']).write_bytes(b'bad')
        sink=Mock()
        with self.assertRaisesRegex(ValueError,'digest|size'):native_sessions.restore(self.runtime,saved,sink)
        sink.write.assert_not_called()

    def test_fence_failure_keeps_previous_session_and_cleans_partial_local_export(self):
        native_sessions.save(self.runtime,self.run,self.task,'config','inputs','session',self.output,self.manifest)
        before=self.store.query('SELECT manifest_json FROM native_task_sessions')[0]['manifest_json']
        self.runtime.lease.validate.side_effect=ValueError('stale generation')
        with self.assertRaisesRegex(ValueError,'stale generation'):
            native_sessions.save(self.runtime,self.run,self.task,'config','inputs','new-session',self.output,self.manifest)
        self.assertEqual(self.store.query('SELECT manifest_json FROM native_task_sessions')[0]['manifest_json'],before)
        parent=self.runtime.project/'native-sessions'/self.run['id']/'code'/'1'
        self.assertEqual(len(list(parent.iterdir())),1)

    def test_running_session_cannot_be_exported_and_retained_bytes_count_against_quota(self):
        self.runtime.frozen=False
        with self.assertRaisesRegex(ValueError,'running'):native_sessions.save(self.runtime,self.run,self.task,'c','i','s',self.output,self.manifest)
        self.runtime.frozen=True
        native_sessions.save(self.runtime,self.run,self.task,'c','i','s',self.output,self.manifest)
        with patch.dict('os.environ',{'VIDEO_USE_USER_STORAGE_BYTES':'1'}):
            with self.assertRaisesRegex(ValueError,'存储额度'):self.store.check_storage(self.run['project_id'])


if __name__=='__main__':unittest.main()
