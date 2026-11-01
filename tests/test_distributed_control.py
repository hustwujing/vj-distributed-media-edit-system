"""Shared control-plane races on PostgreSQL; providers are deterministic fixtures."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch,MagicMock
from tests.distributed_support import PostgresServer
from webapp import agent_store,provider_calls,external_cleanup
from webapp.store import Store
from webapp.task_leases import Coordinator,LeaseLost
from webapp.task_lifecycle import cancel_task


class DistributedControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.pg=PostgresServer()
    @classmethod
    def tearDownClass(cls):cls.pg.close()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.a=Store(root/'a',dsn=self.pg.dsn);agent_store.initialize(self.a)
        self.b=Store(root/'b',dsn=self.pg.dsn)
        self.project=self.a.add_project(self.temp.name,'control')['id']
        self.task=agent_store.add_turn(self.a,self.project,'plan','control')
        self.lease=Coordinator(self.a,str(root),'test').claim(self.task,'agent')
        self.addCleanup(self.lease.release)
        self.w={'id':self.task,'turn_id':self.task};self.s={'id':'image','revision':0,'attempts':0}
        with self.lease.bind():
            self.key=provider_calls.intent(self.a,self.w,self.s,'https://fixture.example','image.edit',{'protocol':'test-control-v1'})
    def pending(self):
        provider_calls.update(self.a,self.w,self.s,'waiting','upstream-one')
        cancel_task(self.b,self.task)

    def test_cross_node_pause_is_visible_even_when_node_capacity_is_full(self):
        from tests import test_workflows as fixtures
        from webapp import workflows
        with patch.dict('os.environ',{'VIDEO_USE_DATABASE_URL':self.pg.dsn,'VIDEO_USE_NODE_CONCURRENCY':'1'}):
            f=fixtures.WorkflowTests();f.setUp()
            try:
                f.submit([fixtures.local_step()])
                node=self.a.query('SELECT node FROM execution_workers WHERE id=?',(self.lease.worker,))[0]['node']
                coordinator=Coordinator(f.store,node,'test')
                self.assertIsNone(coordinator.claim(f.turn,'workflow'))
                workflows.pause(self.b,f.turn)
                self.assertEqual(workflows.get(self.a,f.turn)['status'],'paused')
                self.assertFalse(self.b.query('SELECT id FROM execution_attempts WHERE turn_id=?',(f.turn,)))
                workflows.resume(self.a,f.turn)
                self.assertEqual(workflows.get(self.b,f.turn)['status'],'ready')
            finally:f.tearDown()

    def test_cancel_is_atomic_idempotent_and_fences_late_publication(self):
        self.pending();second=cancel_task(self.b,self.task)
        first=self.a.query('SELECT * FROM execution_cancel_requests WHERE task_id=?',(self.task,))
        self.assertEqual(len(first),1);self.assertEqual(first[0]['request_id'],second['request_id'])
        self.assertEqual(self.a.query('SELECT status FROM agent_turns WHERE id=?',(self.task,))[0]['status'],'cancelled')
        self.assertEqual(self.a.query('SELECT external_id FROM execution_external_cleanup WHERE id=?',(self.key,))[0]['external_id'],'upstream-one')
        with self.lease.bind(),self.assertRaises(LeaseLost):self.a.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(self.task,))

    def test_completed_task_cancel_does_not_revoke_result_or_create_cleanup(self):
        self.a.execute("UPDATE agent_turns SET status='succeeded',artifacts='[{}]' WHERE id=?",(self.task,))
        self.assertEqual(cancel_task(self.b,self.task),{'cancelled':False,'status':'succeeded'})
        self.assertFalse(self.a.query('SELECT * FROM execution_cancel_requests WHERE task_id=?',(self.task,)))
        self.assertFalse(self.a.query('SELECT * FROM execution_external_cleanup WHERE task_id=?',(self.task,)))

    def test_late_id_and_cookie_saved_together_after_revocation(self):
        cancel_task(self.b,self.task)
        with self.lease.bind(),self.assertRaises(LeaseLost):
            provider_calls.update(self.a,self.w,self.s,'waiting','late-id',cookies={'owner':'private-session'})
        row=self.b.query('SELECT external_id,cookies FROM workflow_provider_calls WHERE request_key=?',(self.key,))[0]
        self.assertEqual(row['external_id'],'late-id');self.assertEqual(json.loads(row['cookies']),{'owner':'private-session'})
        self.assertEqual(self.b.query('SELECT external_id,state FROM execution_external_cleanup WHERE id=?',(self.key,))[0],{'external_id':'late-id','state':'pending'})

    def test_terminal_receipt_keeps_usage_when_followed_by_empty_receipt(self):
        cancel_task(self.b,self.task)
        provider_calls.late_receipt(self.a,self.task,self.key,'succeeded','late-id',{'usage':{'seconds':15},'cost':2})
        provider_calls.late_receipt(self.b,self.task,self.key,'waiting','late-id',{})
        row=self.a.query('SELECT state,usage,cost FROM workflow_provider_calls WHERE request_key=?',(self.key,))[0]
        self.assertEqual(row['state'],'succeeded');self.assertEqual(json.loads(row['usage']),{'seconds':15});self.assertEqual(json.loads(row['cost']),2)

    def test_cleanup_poll_preserves_actual_success_usage_without_delivery(self):
        self.pending();self.a.execute("UPDATE execution_external_cleanup SET phase='poll' WHERE id=?",(self.key,))
        with patch.dict('webapp.generation_service.PROVIDERS',{'test-control-v1':MagicMock()}),patch('webapp.generation_service.poll',return_value={'status':'succeeded','usage':{'seconds':15},'cost':1.5}):
            external_cleanup.run(self.b,self.key)
        row=self.a.query('SELECT state,usage,cost FROM workflow_provider_calls WHERE request_key=?',(self.key,))[0]
        self.assertEqual(row['state'],'succeeded');self.assertEqual(json.loads(row['usage']),{'seconds':15})
        self.assertEqual(self.a.query('SELECT state FROM execution_external_cleanup WHERE id=?',(self.key,))[0]['state'],'complete')
        task=self.a.query('SELECT status,artifacts FROM agent_turns WHERE id=?',(self.task,))[0]
        self.assertEqual(task,{'status':'cancelled','artifacts':'[]'})

    def test_accepted_cancel_requires_poll_and_expired_cleaner_cannot_overwrite_success(self):
        self.pending();entered=threading.Event();release=threading.Event();errors=[]
        def late_cancel(*args):entered.set();release.wait(5);return {'status':'cancelled'}
        def old_worker():
            try:external_cleanup.run(self.a,self.key)
            except Exception as exc:errors.append(exc)
        with patch.dict('webapp.generation_service.PROVIDERS',{'test-control-v1':MagicMock()}),patch('webapp.generation_service.cancel',side_effect=late_cancel),patch('webapp.generation_service.poll',return_value={'status':'succeeded','usage':{'seconds':5}}):
            thread=threading.Thread(target=old_worker);thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.b.execute('UPDATE execution_external_cleanup SET lease_until=0 WHERE id=?',(self.key,))
                external_cleanup.run(self.b,self.key)
            finally:release.set();thread.join(timeout=5)
        self.assertFalse(errors);self.assertFalse(thread.is_alive())
        row=self.b.query('SELECT state,generation FROM execution_external_cleanup WHERE id=?',(self.key,))[0]
        self.assertEqual(row,{'state':'complete','generation':2})
        self.assertEqual(self.b.query('SELECT state FROM workflow_provider_calls WHERE request_key=?',(self.key,))[0]['state'],'succeeded')

    def test_cancel_accepted_is_pending_and_operator_reconcile_never_submits(self):
        self.pending()
        with patch.dict('webapp.generation_service.PROVIDERS',{'test-control-v1':MagicMock()}),patch('webapp.generation_service.cancel',return_value={'status':'accepted'}):
            external_cleanup.run(self.a,self.key)
        self.assertEqual(self.b.query('SELECT state,phase FROM execution_external_cleanup WHERE id=?',(self.key,))[0],{'state':'pending','phase':'poll'})
        self.b.execute("UPDATE execution_external_cleanup SET state='unsupported' WHERE id=?",(self.key,))
        external_cleanup.reconcile(self.b,self.key)
        with patch.dict('webapp.generation_service.PROVIDERS',{'test-control-v1':MagicMock()}),patch('webapp.generation_service.poll',return_value={'status':'failed'}),patch('webapp.generation_service.submit') as submit:
            external_cleanup.run(self.a,self.key);submit.assert_not_called()
        self.assertEqual(self.a.query('SELECT state,reconciliations FROM execution_external_cleanup WHERE id=?',(self.key,))[0],{'state':'complete','reconciliations':1})

    def test_cleanup_deadline_exhaustion_does_not_call_provider(self):
        self.pending();self.a.execute('UPDATE execution_external_cleanup SET deadline_at=1 WHERE id=?',(self.key,))
        with patch('webapp.external_cleanup.session') as client:external_cleanup.run(self.b,self.key);client.assert_not_called()
        self.assertEqual(self.a.query('SELECT state FROM execution_external_cleanup WHERE id=?',(self.key,))[0]['state'],'unconfirmed')
