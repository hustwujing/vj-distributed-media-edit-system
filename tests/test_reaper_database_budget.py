"""A real control transaction longer than 100 ms must not abort reaper startup."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.distributed_support import PostgresServer
from webapp import agent_store
from webapp.store import Store
from webapp.execution_reaper import Reaper,DATABASE_TIMEOUT


class ReaperDatabaseBudgetTests(unittest.TestCase):
    def test_reaper_sets_budget_before_first_connection_and_waits_for_normal_transaction(self):
        server=PostgresServer();self.addCleanup(server.close)
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        root=Path(folder.name)
        holder=Store(root/'holder',dsn=server.dsn);agent_store.initialize(holder)
        observer=Store(root/'observer',timeout=.1,initialize_schema=False,dsn=server.dsn)
        started=threading.Event();errors=[]
        def initialize():
            started.set()
            try:Reaper(observer,SimpleNamespace(root=root/'pool'))
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=initialize)
        with patch('webapp.telemetry.emit') as emit:
            with holder.connect():
                thread.start();self.assertTrue(started.wait(2))
                thread.join(timeout=.25)
                self.assertTrue(thread.is_alive(),'Reaper gave up before the normal control transaction finished')
                self.assertEqual(observer.timeout,DATABASE_TIMEOUT)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
            self.assertFalse(any(call.args[0]=='reaper.observation_failed' for call in emit.call_args_list))
        with observer.connect() as db:
            self.assertEqual(db.execute('SHOW lock_timeout').fetchone()['lock_timeout'],'1s')
