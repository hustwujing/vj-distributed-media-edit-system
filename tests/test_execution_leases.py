"""Multi-process ownership, fencing, budgets and restart recovery; no providers."""
import multiprocessing
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from webapp import agent_store
from webapp.store import Store
from webapp.task_leases import Coordinator, LeaseLost


def compete(root, turn, barrier, output):
    store = Store(Path(root))
    coordinator = Coordinator(store, 'node', 'test')
    barrier.wait()
    lease = coordinator.claim(turn, 'agent')
    output.put(lease.attempt if lease else None)


class ExecutionLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        agent_store.initialize(self.store)
        self.project = self.store.add_project('owner', 'project')['id']
        self.turn = agent_store.add_turn(self.store, self.project, 'plan', 'plan')
        self.a = Coordinator(self.store, 'node-a', 'test')
        self.b = Coordinator(self.store, 'node-b', 'test')

    def tearDown(self):
        self.temp.cleanup()

    def expire(self):
        self.store.execute('UPDATE execution_leases SET expires=0 WHERE run_id=?', (self.turn,))

    def test_two_processes_cannot_claim_the_same_task(self):
        ctx = multiprocessing.get_context('spawn')
        barrier, output = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=compete, args=(self.temp.name, self.turn, barrier, output)) for _ in range(2)]
        for p in processes:p.start()
        results = [output.get(timeout=20) for _ in processes]
        for p in processes:
            p.join(timeout=20)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_expired_worker_cannot_renew_write_or_release_successor(self):
        first = self.a.claim(self.turn, 'agent')
        with first.bind():
            self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?", (self.turn,))
        self.assertIsNone(self.b.claim(self.turn, 'agent'))
        self.expire()
        second = self.b.claim(self.turn, 'agent')
        self.assertTrue(second.recovered)
        self.assertEqual(second.generation, first.generation+1)
        for action in (first.renew, first.release):
            with self.assertRaises(LeaseLost):action()
        with first.bind(), self.assertRaises(LeaseLost):
            agent_store.finish(self.store, self.turn, 'succeeded', 'stale')
        with second.bind():
            agent_store.finish(self.store, self.turn, 'succeeded', 'current')
        self.assertEqual(agent_store.turn(self.store, self.turn)['text'], 'current')
        second.release()

    def test_expired_owner_cannot_resurrect_even_before_takeover(self):
        first = self.a.claim(self.turn, 'agent')
        self.expire()
        with self.assertRaises(LeaseLost):first.renew()

    def test_nested_transactions_are_fenced_and_rollback_together(self):
        lease = self.a.claim(self.turn, 'agent')
        with lease.bind(), self.assertRaises(ValueError):
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self.store.execute("UPDATE agent_turns SET text='uncommitted' WHERE id=?", (self.turn,))
                raise ValueError('rollback')
        self.assertEqual(agent_store.turn(self.store, self.turn)['text'], '')

    def test_call_budget_survives_worker_restart(self):
        first = self.a.claim(self.turn, 'agent')
        first.consume('image', 1)
        first.release(interrupted=True)
        second = self.b.claim(self.turn, 'agent')
        self.assertTrue(second.recovered)
        with self.assertRaises(ValueError):second.consume('image', 1)

    def test_new_worker_does_not_reset_healthy_tasks(self):
        first = self.a.claim(self.turn, 'agent')
        with first.bind():
            self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?", (self.turn,))
        fresh = Coordinator(self.store, 'new-node', 'test')
        self.assertIsNone(fresh.claim(self.turn, 'agent'))
        self.assertEqual(agent_store.turn(self.store, self.turn)['status'], 'running')

    def test_compatible_release_takeover_and_user_concurrency_are_enforced(self):
        first = self.a.claim(self.turn, 'agent', per_user=1)
        project = self.store.add_project('owner', 'second')['id']
        other = agent_store.add_turn(self.store, project, 'plan', 'plan')
        self.assertIsNone(self.b.claim(other, 'agent', per_user=1))
        first.release()
        different = Coordinator(self.store, 'new-node', 'other-release')
        upgraded=different.claim(self.turn,'agent')
        self.assertIsNotNone(upgraded)
        upgraded.release()
        self.assertIsNotNone(self.b.claim(other, 'agent', per_user=1))

    def test_replicas_share_the_node_capacity_limit(self):
        with patch.dict('os.environ',{'VIDEO_USE_NODE_CONCURRENCY':'1'}):
            self.a.claim(self.turn,'agent')
            project=self.store.add_project('other-owner','other')['id']
            turn=agent_store.add_turn(self.store,project,'plan','plan')
            same_node=Coordinator(self.store,'node-a','test')
            self.assertIsNone(same_node.claim(turn,'agent'))
            self.assertIsNotNone(self.b.claim(turn,'agent'))


if __name__ == '__main__':unittest.main()
