"""Database-enforced observation connections, including nested callers."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from webapp.store import Store
from webapp import agent_store
from webapp.task_leases import Coordinator


class ReadonlyStoreTests(unittest.TestCase):
    dsn=''
    write_error=sqlite3.OperationalError

    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store=Store(Path(folder.name),dsn=self.dsn)
        agent_store.initialize(self.store)
        self.project=self.store.add_project('readonly-test','original')['id']

    def project_row(self):
        return self.store.query('SELECT * FROM projects WHERE id=?',(self.project,))[0]

    def test_read_connection_rejects_writes_and_keeps_data(self):
        with self.assertRaises(self.write_error):
            with self.store.connect(readonly=True) as db:
                db.execute("UPDATE projects SET name='changed' WHERE id=?",(self.project,))
        self.assertEqual(self.project_row()['name'],'original')
        self.store.execute("UPDATE projects SET name='writable' WHERE id=?",(self.project,))
        self.assertEqual(self.project_row()['name'],'writable')

    def test_nested_write_cannot_escape_outer_readonly_transaction(self):
        with self.assertRaises(self.write_error):
            with self.store.connect(readonly=True):
                self.store.execute("DELETE FROM projects WHERE id=?",(self.project,))
        self.assertIsNotNone(self.project_row())

    def test_read_in_write_transaction_observes_uncommitted_state_and_rolls_back(self):
        with self.assertRaisesRegex(ValueError,'rollback'):
            with self.store.connect():
                self.store.execute("UPDATE projects SET name='uncommitted' WHERE id=?",(self.project,))
                self.assertEqual(self.project_row()['name'],'uncommitted')
                raise ValueError('rollback')
        self.assertEqual(self.project_row()['name'],'original')

    def test_readonly_query_under_real_lease_preserves_validation(self):
        turn=agent_store.add_turn(self.store,self.project,'plan','read')
        lease=Coordinator(self.store,'readonly-worker','test').claim(turn,'agent')
        self.assertIsNotNone(lease)
        try:
            with lease.bind():
                self.assertEqual(self.project_row()['name'],'original')
            self.store.execute('UPDATE execution_leases SET expires=0 WHERE attempt=?',(lease.attempt,))
            from webapp.task_leases import LeaseLost
            with lease.bind(),self.assertRaises(LeaseLost):self.project_row()
        finally:lease.release()


class PostgresReadonlyStoreTests(ReadonlyStoreTests):
    @classmethod
    def setUpClass(cls):
        from tests.distributed_support import PostgresServer
        from psycopg.errors import ReadOnlySqlTransaction
        cls.server=PostgresServer()
        cls.addClassCleanup(cls.server.close)
        cls.dsn=cls.server.dsn
        cls.write_error=ReadOnlySqlTransaction
