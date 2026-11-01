"""Content identity must survive first checkpoint and node/cache changes."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from webapp import agent_store,plan_readiness
from webapp.store import Store
from webapp.workspace_state import seed_manifest,effective_manifest
from tests.readiness_support import ready_plan

class WorkspaceReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(Path(self.tmp.name));agent_store.initialize(self.s);self.p=self.s.add_project('owner','seed')['id']
    def seed(self,name,content):
        p=self.s.project_dir(self.p)/'agent/workspace'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content);return p
    def checkpoint(self,manifest):
        self.s.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET manifest=excluded.manifest',
                       (self.p,'attempt','checkpoint',json.dumps({'workspace':manifest,'state':{}}),self.s.now()))
    def test_arbitrary_seed_files_keep_identity_when_checkpointed(self):
        for name in ('edit/config.json','edit/嵌套目录/layout.txt'):
            self.seed(name,'initial '+name)
        task=agent_store.add_turn(self.s,self.p,'plan','prepare');ready_plan(self.s,task);fingerprint=plan_readiness.fingerprint(self.s,self.p)
        manifest=seed_manifest(self.s,self.p);self.checkpoint(manifest)
        self.assertEqual(plan_readiness.fingerprint(self.s,self.p),fingerprint);self.assertTrue(plan_readiness.public(self.s,task)['can_execute'])
        execution=agent_store.add_turn(self.s,self.p,'plan','执行吧');self.s.execute("UPDATE agent_turns SET status='running' WHERE id=?",(execution,));plan_readiness.register(self.s,execution)
        plan_readiness.promote_confirmation(self.s,execution,task)
        self.assertEqual(agent_store.turn(self.s,execution)['mode'],'execute')
        self.assertEqual(self.s.query('SELECT parent_task_id FROM usage_tasks WHERE task_id=?',(execution,))[0]['parent_task_id'],task)
    def test_content_change_invalidates_and_empty_checkpoint_does_not_resurrect_seed(self):
        file=self.seed('edit/settings.txt','before');task=agent_store.add_turn(self.s,self.p,'plan','prepare');ready_plan(self.s,task)
        file.write_text('after');self.assertFalse(plan_readiness.public(self.s,task)['can_execute'])
        self.checkpoint(seed_manifest(self.s,self.p));before=plan_readiness.fingerprint(self.s,self.p);self.checkpoint({})
        self.assertEqual(effective_manifest(self.s,self.p),{});self.assertNotEqual(before,plan_readiness.fingerprint(self.s,self.p))
        empty=plan_readiness.fingerprint(self.s,self.p);file.write_text('another stale local copy')
        self.assertEqual(empty,plan_readiness.fingerprint(self.s,self.p))
    def test_no_seed_checkpoint_and_reordered_manifest_are_equivalent(self):
        empty=plan_readiness.fingerprint(self.s,self.p);self.checkpoint({});self.assertEqual(empty,plan_readiness.fingerprint(self.s,self.p))
        self.s.execute('DELETE FROM execution_checkpoints WHERE project=?',(self.p,))
        self.seed('edit/z.txt','z');self.seed('edit/a.txt','a');before=plan_readiness.fingerprint(self.s,self.p)
        self.checkpoint(dict(reversed(list(seed_manifest(self.s,self.p).items()))));self.assertEqual(before,plan_readiness.fingerprint(self.s,self.p))
    def test_ignored_previews_and_session_notes_do_not_change_proposal(self):
        before=plan_readiness.fingerprint(self.s,self.p);self.seed('edit/previews/turn/view.png','preview');self.seed('edit/project.md','notes')
        self.assertEqual(before,plan_readiness.fingerprint(self.s,self.p));self.checkpoint(seed_manifest(self.s,self.p));self.assertEqual(before,plan_readiness.fingerprint(self.s,self.p))
    def test_empty_shared_project_does_not_require_materialized_storage(self):
        before=plan_readiness.fingerprint(self.s,self.p)
        with patch.dict('os.environ',{'VIDEO_USE_OBJECT_BUCKET':'empty-fixture'}):
            self.assertEqual(seed_manifest(self.s,self.p),{})
            self.assertEqual(before,plan_readiness.fingerprint(self.s,self.p))

    def test_local_links_are_not_part_of_restorable_seed(self):
        secret=Path(self.tmp.name)/'outside';secret.write_text('not workspace');p=self.seed('edit/regular','safe');(p.parent/'link').symlink_to(secret)
        self.assertEqual(set(seed_manifest(self.s,self.p)),{'edit/regular'})
