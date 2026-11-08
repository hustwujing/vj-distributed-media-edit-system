"""Projection schema guards and rollback on both supported databases."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp import agent_store, workflow_graph as graph
from webapp.store import Store


class WorkItemSchemaTests(unittest.TestCase):
    dsn=''

    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store=Store(Path(folder.name),dsn=self.dsn)
        agent_store.initialize(self.store)
        graph.initialize(self.store)

    def test_projection_accepts_control_only_columns_and_repeated_initialization(self):
        graph.initialize(self.store)
        with self.store.connect() as db:
            task={row['name'] for row in db.execute('PRAGMA table_info(run_tasks)')}
            control={row['name'] for row in db.execute('PRAGMA table_info(control_jobs)')}
        self.assertEqual(control-task,{'control_kind','snapshot_json','snapshot_digest'})
        self.store.query('SELECT * FROM run_work_items')

    def test_drift_reports_missing_column_and_preserves_existing_view(self):
        self.store.execute('ALTER TABLE run_tasks ADD COLUMN drift_probe TEXT')
        try:
            with self.assertRaisesRegex(ValueError,'columns diverged.*drift_probe'):
                graph.initialize(self.store)
            self.store.query('SELECT * FROM run_work_items')
        finally:
            self.store.execute('ALTER TABLE run_tasks DROP COLUMN drift_probe')

    def test_failed_view_recreation_rolls_back_drop(self):
        with self.store.connect() as db:
            connection_type=type(db)
        original=connection_type.execute
        def fail_create(connection,sql,args=()):
            if sql.startswith('CREATE') and 'run_work_items AS' in sql:
                raise RuntimeError('injected view creation failure')
            return original(connection,sql,args)
        with patch.object(connection_type,'execute',new=fail_create):
            with self.assertRaisesRegex(RuntimeError,'injected view creation failure'):
                graph.initialize(self.store)
        self.store.query('SELECT * FROM run_work_items')


class PostgresWorkItemSchemaTests(WorkItemSchemaTests):
    @classmethod
    def setUpClass(cls):
        from tests.distributed_support import PostgresServer
        cls.server=PostgresServer()
        cls.addClassCleanup(cls.server.close)
        cls.dsn=cls.server.dsn
