import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from webapp import agent_store
from webapp.store import Store
from webapp.task_leases import Coordinator,LeaseLost
from webapp.task_lifecycle import TaskLimitReached,snapshot


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name));agent_store.initialize(self.store)
        self.project=self.store.add_project('owner','project')['id']
        self.turn=agent_store.add_turn(self.store,self.project,'plan','plan')
        self.a=Coordinator(self.store,'a','test');self.b=Coordinator(self.store,'b','test')
    def tearDown(self):self.tmp.cleanup()

    def test_queued_deadline_prevents_claim_without_allocating_an_attempt(self):
        self.store.execute('UPDATE execution_limits SET deadline_at=0 WHERE task_id=?',(self.turn,))
        self.assertIsNone(self.a.claim(self.turn,'agent'))
        self.assertEqual(agent_store.turn(self.store,self.turn)['status'],'failed')
        self.assertFalse(self.store.query('SELECT * FROM execution_attempts'))
        self.assertEqual(snapshot(self.store,self.turn)['termination_reason'],'deadline')

    def test_deadline_is_preserved_across_release_and_new_configuration(self):
        initial=snapshot(self.store,self.turn)['deadline_at']
        lease=self.a.claim(self.turn,'agent');lease.release(interrupted=True)
        with patch.dict(os.environ,{'VIDEO_USE_TASK_TTL_SECONDS':'999999'}):
            next_lease=self.b.claim(self.turn,'agent')
        self.assertTrue(next_lease.recovered)
        self.assertEqual(snapshot(self.store,self.turn)['deadline_at'],initial)

    def test_compute_budget_is_settled_once_and_not_reset_on_takeover(self):
        self.store.execute('UPDATE execution_limits SET budget_seconds=60 WHERE task_id=?',(self.turn,))
        first=self.a.claim(self.turn,'agent');first.start_compute()
        self.store.execute('UPDATE execution_leases SET expires=0 WHERE run_id=?',(self.turn,))
        second=self.b.claim(self.turn,'agent');second.start_compute()
        value=self.store.query('SELECT used_seconds FROM execution_limits')[0]['used_seconds']
        self.assertAlmostEqual(value,45,delta=.1)
        first.stop_compute(True)
        self.assertAlmostEqual(self.store.query('SELECT used_seconds FROM execution_limits')[0]['used_seconds'],45,delta=.1)
        slot=self.store.query('SELECT reserved FROM execution_time_slices WHERE attempt=?',(second.attempt,))[0]
        self.assertAlmostEqual(slot['reserved'],15,delta=.1)

    def test_renew_charges_elapsed_time_not_three_full_reservations(self):
        lease=self.a.claim(self.turn,'agent');lease.start_compute()
        self.store.execute('UPDATE execution_time_slices SET accounted_at=accounted_at-5 WHERE attempt=?',(lease.attempt,))
        lease.renew()
        used=self.store.query('SELECT used_seconds FROM execution_limits')[0]['used_seconds']
        self.assertGreaterEqual(used,5);self.assertLess(used,6)

    def test_deadline_blocks_late_success(self):
        lease=self.a.claim(self.turn,'agent')
        self.store.execute('UPDATE execution_limits SET deadline_at=0 WHERE task_id=?',(self.turn,))
        with lease.bind(),self.assertRaises(TaskLimitReached):agent_store.finish(self.store,self.turn,'succeeded','late')
        self.a.heartbeat()
        self.assertEqual(agent_store.turn(self.store,self.turn)['status'],'failed')

if __name__=='__main__':unittest.main()
