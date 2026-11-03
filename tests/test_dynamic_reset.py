"""Exercise destructive reset guards against a disposable PostgreSQL server."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.distributed_support import PostgresServer
from deploy import dynamic_cluster as cluster


class DedicatedResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg=PostgresServer()
        cls.pg.command('psql','-h','127.0.0.1','-p',str(cls.pg.port),'-U','postgres','-d','postgres',
            '-c','CREATE ROLE distributed_agent_system; CREATE ROLE distributed_agent_orchestrator;')
        cls.pg.command('createdb','-h','127.0.0.1','-p',str(cls.pg.port),'-U','postgres','distributed_agent_system')

    @classmethod
    def tearDownClass(cls):cls.pg.close()

    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        folder=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        values={'VIDEO_USE_DATABASE_URL':f'postgresql://distributed_agent_system@127.0.0.1:{self.pg.port}/distributed_agent_system',
                'VIDEO_USE_DEPLOYMENT':'dynamic-agent','VIDEO_USE_OBJECT_PREFIX':'distributed-agent-system',
                'VIDEO_USE_AGENT_STREAM_PREFIX':'distributed-agent-system:events:'}
        for name,value in {'environment':lambda:values,'active':lambda _:False,'PG_SOCKET':'127.0.0.1','PG_PORT':self.pg.port,
                           'PG_BIN':self.pg.bin,'ROOT':folder/'cluster','EXECUTORS':folder/'exec','CONF':folder/'config'}.items():
            self.stack.enter_context(patch.object(cluster,name,value))
        self.sql('DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE TABLE keep(id integer); INSERT INTO keep VALUES (1);')

    def sql(self,text):
        return self.pg.command('psql','-X','-v','ON_ERROR_STOP=1','-h','127.0.0.1','-p',str(self.pg.port),
                               '-U','postgres','-d','distributed_agent_system','-At','-c',text).stdout.decode().strip()

    def test_unknown_provider_receipt_prevents_reset_and_keeps_data(self):
        self.sql("CREATE TABLE workflow_provider_calls(state text); INSERT INTO workflow_provider_calls VALUES ('submission_unknown');")
        with self.assertRaises(RuntimeError):cluster.reset_database()
        self.assertEqual(self.sql('SELECT COUNT(*) FROM keep'),'1')
        self.assertEqual(self.sql('SELECT state FROM workflow_provider_calls'),'submission_unknown')

    def test_pending_cleanup_and_active_service_each_prevent_reset(self):
        self.sql("CREATE TABLE execution_cleanup(state text); INSERT INTO execution_cleanup VALUES ('pending');")
        with self.assertRaises(RuntimeError):cluster.reset_database()
        self.assertEqual(self.sql('SELECT COUNT(*) FROM keep'),'1')
        with patch.object(cluster,'active',lambda _:True),self.assertRaisesRegex(ValueError,'Stop all'):
            cluster.reset_database()
        with patch.object(cluster,'DATABASE','main'),self.assertRaisesRegex(ValueError,'restricted'):
            cluster.reset_database()

    def test_completed_receipts_allow_fresh_schema_only_in_dedicated_database(self):
        self.sql("CREATE TABLE workflow_provider_calls(state text); INSERT INTO workflow_provider_calls VALUES ('succeeded');")
        cluster.reset_database()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"),'0')
        self.assertEqual(self.sql("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public'"),'distributed_agent_system')

    def test_unconfirmed_external_cleanup_cannot_be_erased_by_reset(self):
        self.sql("CREATE TABLE execution_external_cleanup(state text); INSERT INTO execution_external_cleanup VALUES ('unconfirmed');")
        with self.assertRaises(RuntimeError):cluster.reset_database()
        self.assertEqual(self.sql('SELECT COUNT(*) FROM keep'),'1')
