"""Run admission remains atomic across independent API/database nodes."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from tests.distributed_support import PostgresServer
from webapp import agent_store,workflow_graph as graph
from webapp.store import Store


class RunAdmissionRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.pg=PostgresServer()

    @classmethod
    def tearDownClass(cls):cls.pg.close()

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();root=Path(self.folder.name)
        self.a=Store(root/'a',dsn=self.pg.dsn);agent_store.initialize(self.a)
        self.b=Store(root/'b',dsn=self.pg.dsn)
        self.project=self.a.add_project(root.name,'run admission race')['id']
        self.authorization={'goal':'Create a report','allowed_assets':[],
            'allowed_operations':['script'],'delivery_targets':['report'],
            'content_fingerprint':graph.content_fingerprint(self.a,self.project)}

    def tearDown(self):
        self.a.execute("UPDATE runs SET status='CANCELLED' WHERE project_id=?",(self.project,))
        self.folder.cleanup()

    def race(self,left,right):
        barrier=threading.Barrier(2)
        def invoke(fn):
            barrier.wait(timeout=5)
            try:return ('ok',fn()['id'])
            except ValueError as exc:return ('error',str(exc))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(invoke,fn) for fn in (left,right)]
            return [future.result(timeout=20) for future in futures]

    def test_owner_execution_quota_is_atomic_across_nodes(self):
        with patch.dict(os.environ,{'VIDEO_USE_USER_QUEUE_LIMIT':'1'}):
            results=self.race(
                lambda:graph.create(self.a,self.project,uuid.uuid4().hex,self.authorization),
                lambda:graph.create(self.b,self.project,uuid.uuid4().hex,self.authorization))
        self.assertEqual([kind for kind,_ in results].count('ok'),1,results)
        self.assertEqual([kind for kind,_ in results].count('error'),1,results)
        self.assertIn('待处理任务已达上限',next(value for kind,value in results if kind=='error'))
        self.assertEqual(self.a.query('SELECT COUNT(*) AS n FROM runs WHERE project_id=?',(self.project,))[0]['n'],1)

    def test_session_single_active_run_is_atomic_across_nodes(self):
        session=uuid.uuid4().hex
        results=self.race(
            lambda:graph.create(self.a,self.project,uuid.uuid4().hex,self.authorization,session_id=session),
            lambda:graph.create(self.b,self.project,uuid.uuid4().hex,self.authorization,session_id=session))
        self.assertEqual([kind for kind,_ in results].count('ok'),1,results)
        self.assertEqual([kind for kind,_ in results].count('error'),1,results)
        self.assertIn('Session already has an active Run',next(value for kind,value in results if kind=='error'))
