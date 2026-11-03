import multiprocessing
from pathlib import Path
import tempfile
import unittest

from tests.distributed_support import PostgresServer
from webapp.store import Store
from webapp import agent_store
from webapp.task_leases import Coordinator, LeaseLost


def claim_on_node(root, dsn, task, barrier, output):
    store = Store(Path(root), dsn=dsn)
    coordinator = Coordinator(store, root, 'distributed-test')
    barrier.wait(timeout=10)
    lease = coordinator.claim(task, 'agent', 2)
    output.put(lease.attempt if lease else None)


def hold_uncommitted(root,dsn,task,ready):
    import time
    store=Store(Path(root),dsn=dsn,initialize_schema=False)
    with store.connect() as db:
        db.execute("UPDATE agent_turns SET text='must rollback' WHERE id=?",(task,))
        ready.set()
        time.sleep(60)


class DistributedDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = PostgresServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.a = Store(Path(self.temp.name)/'a', dsn=self.server.dsn)
        agent_store.initialize(self.a)
        self.b = Store(Path(self.temp.name)/'b', dsn=self.server.dsn)
        self.project = self.a.add_project(self.temp.name, 'distributed')['id']
        self.task = agent_store.add_turn(self.a, self.project, 'plan', 'test')

    def tearDown(self):
        self.temp.cleanup()

    def test_separate_node_directories_share_state_and_atomic_claim(self):
        self.assertEqual(self.b.query('SELECT id FROM projects WHERE id=?', (self.project,))[0]['id'], self.project)
        ctx = multiprocessing.get_context('spawn')
        barrier, output = ctx.Barrier(2), ctx.Queue()
        children = [ctx.Process(target=claim_on_node, args=(str(s.root), self.server.dsn, self.task, barrier, output)) for s in (self.a, self.b)]
        try:
            for child in children:child.start()
            results = [output.get(timeout=20) for _ in children]
            self.assertEqual(sum(r is not None for r in results), 1)
            for child in children:
                child.join(timeout=10)
                self.assertEqual(child.exitcode, 0)
        finally:
            for child in children:
                if child.is_alive():child.kill();child.join(timeout=5)

    def test_takeover_fences_old_node_and_nested_transaction_rolls_back(self):
        ca, cb = [Coordinator(s, str(s.root), 'distributed-test') for s in (self.a, self.b)]
        first = ca.claim(self.task, 'agent')
        self.a.execute('UPDATE execution_leases SET expires=0 WHERE run_id=?', (self.task,))
        second = cb.claim(self.task, 'agent')
        self.assertEqual(second.generation, first.generation+1)
        with first.bind(), self.assertRaises(LeaseLost):
            self.a.execute("UPDATE agent_turns SET text='stale' WHERE id=?", (self.task,))
        with second.bind(), self.assertRaises(ValueError):
            with self.b.connect():
                self.b.execute("UPDATE agent_turns SET text='rollback' WHERE id=?", (self.task,))
                raise ValueError('abort')
        self.assertEqual(self.a.query('SELECT text FROM agent_turns WHERE id=?', (self.task,))[0]['text'], '')

    def test_database_time_has_subsecond_precision(self):
        value = self.a.now()
        self.assertGreater(value, 1_700_000_000)
        self.assertNotEqual(value, int(value))

    def test_sigkill_rolls_back_transaction_and_releases_database_lock(self):
        ctx=multiprocessing.get_context('spawn');ready=ctx.Event()
        child=ctx.Process(target=hold_uncommitted,args=(str(self.a.root),self.server.dsn,self.task,ready))
        try:
            child.start();self.assertTrue(ready.wait(5))
            child.kill();child.join(timeout=5)
            self.assertIsNotNone(child.exitcode)
            self.assertEqual(self.b.query('SELECT text FROM agent_turns WHERE id=?',(self.task,))[0]['text'],'')
            lease=Coordinator(self.b,str(self.b.root),'distributed-test').claim(self.task,'agent')
            self.assertIsNotNone(lease);lease.release()
        finally:
            if child.is_alive():child.kill();child.join(timeout=5)


if __name__ == '__main__':unittest.main()
