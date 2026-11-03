"""Retained step outputs stay inspectable by later conversations without approval."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from webapp import agent_store,workflows,step_checkpoints,plan_readiness
from webapp.store import Store
from tests.test_workflows import local_step


class RetainedStepVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'store');agent_store.initialize(self.store)
        self.project=self.store.add_project('owner','retained')['id']
        self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.runtime=SimpleNamespace(store=self.store,lease=SimpleNamespace(kind='agent'),
            project=self.store.project_dir(self.project),workspace=self.workspace,uid=os.getuid(),gid=os.getgid(),limit=1024)

    def snapshot(self,project,payload=b'candidate',revision=0):
        plan=agent_store.add_turn(self.store,project,'plan','inspect retained output');agent_store.finish(self.store,plan,'succeeded','scope')
        task=agent_store.add_turn(self.store,project,'execute','confirmed',plan_id=plan)
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
        w=workflows.submit(self.store,task,{'key':'retained','name':'retained','steps':[local_step('save')]})
        wid=w['id'];relative=f'edit/workflows/{wid}/save/version-{revision}/final.png'
        source=self.store.root/('saved-'+wid);source.write_bytes(payload)
        record={'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),'location':source.name}
        self.store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
            (wid,'save',revision,project,'old-attempt',json.dumps({relative:record}),self.store.now()))
        self.store.execute("UPDATE agent_steps SET revision=?,status='agent_review',candidate=? WHERE workflow=? AND id='save'",(revision,json.dumps([relative]),wid))
        return wid,relative,source,record

    def test_new_conversation_sees_current_owned_candidate_without_accepting_it(self):
        wid,relative,source,record=self.snapshot(self.project,revision=1)
        stale=f'edit/workflows/{wid}/save/version-0/old.png'
        self.store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
            (wid,'save',0,self.project,'older',json.dumps({stale:record}),self.store.now()))
        other=self.store.add_project('different-owner','other')['id'];_,foreign,_,_=self.snapshot(other,b'private')
        step_checkpoints.restore(self.runtime)
        self.assertEqual((self.workspace/relative).read_bytes(),b'candidate')
        self.assertFalse((self.workspace/stale).exists());self.assertFalse((self.workspace/foreign).exists())
        self.assertEqual(self.store.query('SELECT status FROM agent_steps WHERE workflow=?',(wid,))[0]['status'],'agent_review')

    def test_replanning_checkpoint_does_not_invalidate_restored_step_content(self):
        # Only one project and one thread: no concurrent browser or writer.
        wid,relative,_,record=self.snapshot(self.project)
        workflows.cancel(self.store,self.store.query('SELECT run_id FROM agent_workflows WHERE id=?',(wid,))[0]['run_id'])
        task=agent_store.add_turn(self.store,self.project,'plan','resize retained output')
        from tests.readiness_support import ready_plan
        ready_plan(self.store,task)
        before=plan_readiness.fingerprint(self.store,self.project)
        step_checkpoints.restore(self.runtime)
        self.store.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?,?)',
            (self.project,'planning-attempt','checkpoint',json.dumps({'workspace':{relative:record},'state':{}}),self.store.now()))
        self.assertEqual(plan_readiness.fingerprint(self.store,self.project),before)
        self.assertTrue(plan_readiness.public(self.store,task)['can_execute'])

    def test_new_step_content_invalidates_proposal_but_foreign_project_does_not(self):
        wid,relative,_,record=self.snapshot(self.project)
        before=plan_readiness.fingerprint(self.store,self.project)
        foreign=self.store.add_project('different-owner','parallel project')['id'];self.snapshot(foreign,b'foreign')
        self.assertEqual(plan_readiness.fingerprint(self.store,self.project),before)
        changed={**record,'sha256':hashlib.sha256(b'changed').hexdigest()}
        self.store.execute('UPDATE execution_step_checkpoints SET manifest=? WHERE workflow=?',(json.dumps({relative:changed}),wid))
        self.assertNotEqual(plan_readiness.fingerprint(self.store,self.project),before)

    def test_restore_accounts_for_existing_workspace_and_rejects_before_copy(self):
        _,relative,_,_=self.snapshot(self.project,b'12345678');(self.workspace/'existing').write_bytes(b'1234');self.runtime.limit=10
        with self.assertRaisesRegex(RuntimeError,'limit'):step_checkpoints.restore(self.runtime)
        self.assertFalse((self.workspace/relative).exists())

    def test_restore_rejects_destination_symlink_and_corrupt_snapshot(self):
        _,relative,source,_=self.snapshot(self.project)
        outside=self.root/'outside';outside.mkdir();(self.workspace/'edit').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'destination'):step_checkpoints.restore(self.runtime)
        self.assertEqual(list(outside.iterdir()),[])
        (self.workspace/'edit').unlink();source.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'integrity'):step_checkpoints.restore(self.runtime)

    def test_restore_rejects_checkpoint_path_outside_its_declared_step(self):
        wid,relative,_,record=self.snapshot(self.project)
        self.store.execute('UPDATE execution_step_checkpoints SET manifest=? WHERE workflow=?',
            (json.dumps({'edit/other-step/result.png':record}),wid))
        with self.assertRaisesRegex(ValueError,'scope'):step_checkpoints.restore(self.runtime)
