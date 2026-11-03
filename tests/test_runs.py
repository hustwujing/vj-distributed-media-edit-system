"""A Run is the execution root; control work is outside the business DAG."""
from pathlib import Path
import tempfile
import unittest
from webapp import agent_store,workflow_graph as graph
from webapp.store import Store
from webapp.task_leases import Coordinator,LeaseLost
from webapp.workflow_orchestrator import Orchestrator


class RunIdentityTests(unittest.TestCase):
    def setUp(self):
        from tests.isolation import temporary_root
        self.folder=tempfile.TemporaryDirectory(dir=temporary_root())
        self.store=Store(Path(self.folder.name));agent_store.initialize(self.store)
        self.project=self.store.add_project('run-owner','single identity')['id']
        self.authorization={'goal':'Create a report','allowed_assets':[],
            'allowed_operations':['script'],'delivery_targets':['report'],
            'content_fingerprint':graph.content_fingerprint(self.store,self.project)}
        self.run=graph.create(self.store,self.project,None,self.authorization)
        self.controller=Orchestrator(self.store,'run-test')

    def tearDown(self):self.folder.cleanup()

    def tick(self):
        self.store.execute('UPDATE runs SET next_reconcile_at=0 WHERE id=?',(self.run['id'],))
        for claim in self.controller.claim_due():self.controller.reconcile(*claim)
        return graph.get(self.store,self.run['id'])

    def test_root_has_session_and_no_agent_turn_or_internal_task(self):
        self.assertEqual(self.run['status'],'ACTIVE')
        self.assertEqual(self.run['activity'],'IDLE')
        self.assertNotIn('run_id',self.run)
        self.assertFalse(self.store.query('SELECT id FROM agent_turns'))
        run=self.tick()
        self.assertEqual(run['activity'],'PLANNING')
        self.assertEqual(run['tasks'],[])
        self.assertEqual([job['control_kind'] for job in run['control_jobs']],['PLAN'])
        self.assertNotIn('internal',{row['name'] for row in self.store.query('PRAGMA table_info(run_tasks)')})
        self.assertEqual(self.store.query('SELECT run_id FROM usage_tasks')[0]['run_id'],run['id'])

    def test_control_job_lease_is_fenced_by_its_own_record(self):
        run=self.tick();job=run['control_jobs'][0]
        coordinator=Coordinator(self.store,'run-worker','test')
        lease=coordinator.claim(run['id'],'workflow',step_id=job['id'])
        self.assertIsNotNone(lease)
        try:
            lease.check()
            self.store.execute('UPDATE control_jobs SET revision=revision+1 WHERE id=?',(job['id'],))
            with self.assertRaises(LeaseLost):lease.check()
        finally:lease.release()

    def test_session_is_serial_and_successor_has_distinct_usage_root(self):
        with self.assertRaisesRegex(ValueError,'active Run'):
            graph.create(self.store,self.project,None,self.authorization,session_id=self.run['session_id'])
        self.store.execute("UPDATE runs SET status='SUCCEEDED' WHERE id=?",(self.run['id'],))
        successor=graph.create(self.store,self.project,None,self.authorization,parent_run_id=self.run['id'])
        self.assertEqual(successor['session_id'],self.run['session_id'])
        self.assertEqual(successor['parent_run_id'],self.run['id'])
        self.assertNotEqual(successor['id'],self.run['id'])
        self.assertEqual(self.store.query('SELECT run_id FROM usage_tasks WHERE task_id=?',(successor['id'],))[0]['run_id'],successor['id'])
