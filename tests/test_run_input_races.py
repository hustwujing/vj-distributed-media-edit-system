"""Two independent database clients compete on real PostgreSQL transactions."""
from concurrent.futures import ThreadPoolExecutor
import json
from tests.run_input_fixture import accept_change
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from tests.distributed_support import PostgresServer
from tests import test_dynamic_graph as fixtures
from webapp import agent_store,workflow_graph as graph,run_inputs
from webapp.store import Store
from webapp.task_leases import Coordinator
from webapp.workflow_orchestrator import Orchestrator


class RunInputRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.pg=PostgresServer()
    @classmethod
    def tearDownClass(cls):cls.pg.close()

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();root=Path(self.folder.name)
        self.a=Store(root/'a',dsn=self.pg.dsn);agent_store.initialize(self.a)
        self.b=Store(root/'b',dsn=self.pg.dsn)
        self.project=self.a.add_project(root.name,'input race')['id']
        self.run=graph.create(self.a,self.project,None,{'goal':'Write a report','allowed_assets':[],
            'allowed_operations':['script'],'delivery_targets':['report'],
            'content_fingerprint':graph.content_fingerprint(self.a,self.project)})
        self.spec=graph.Task.model_validate(fixtures.script('report')).model_dump()
        self.a.execute("INSERT INTO run_tasks(run_id,id,type,spec_json,status,created_graph_version,created_at,updated_at) VALUES (?,?,'script',?,'ready',1,?,?)",
            (self.run['id'],'report',graph.encode(self.spec),self.a.now(),self.a.now()))
        self.a.execute('UPDATE runs SET graph_version=1,sealed=1 WHERE id=?',(self.run['id'],))

    def tearDown(self):
        self.a.execute("UPDATE runs SET status='CANCELLED' WHERE project_id=?",(self.project,))
        self.folder.cleanup()

    def message(self,key=None):
        return {'request_id':key or uuid.uuid4().hex,'content':'Use a concise report layout'}

    def race(self,left,right):
        barrier=threading.Barrier(2)
        def execute(fn):barrier.wait(timeout=5);return fn()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(execute,fn) for fn in (left,right)]
            return [future.result(timeout=20) for future in futures]

    def test_claim_and_input_commit_have_one_consistent_order(self):
        coordinator=Coordinator(self.a,'race-'+uuid.uuid4().hex,'test')
        lease,accepted=self.race(lambda:coordinator.claim(self.run['id'],'workflow',step_id='report'),
            lambda:accept_change(self.b,self.run['id'],'owner',self.message()))
        run=graph.get(self.b,self.run['id'])
        self.assertEqual(run['input_barrier_sequence'],accepted['sequence'])
        if lease:
            try:
                lease.check()
                self.assertEqual([(entry['task_id'],entry['revision']) for entry in run['drain_set']],[('report',0)])
            finally:lease.release()
        else:self.assertEqual(run['drain_set'],[])

    def test_duplicate_input_across_nodes_creates_one_sequence(self):
        request=self.message()
        first,second=self.race(lambda:accept_change(self.a,self.run['id'],'owner',request),
            lambda:accept_change(self.b,self.run['id'],'owner',request))
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(graph.get(self.a,self.run['id'])['input_sequence'],1)

    def test_publication_and_input_cannot_publish_an_obsolete_graph(self):
        source=Path(self.folder.name)/'report.txt';source.write_text('Approved report')
        record=self.a.artifacts.upload(self.project,source)
        # Each client has a local backend, while publication itself uses immutable
        # metadata only. Real object transfers are covered by the execution suite.
        self.b.artifacts
        filename='edit/workflows/'+self.run['id']+'/report/output/report.txt'
        self.a.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
            (self.run['id'],'report',0,self.project,'finished',graph.encode({filename:{'artifact':record['id'],'sha256':record['sha256'],'size':record['size']}}),self.a.now()))
        self.a.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=? WHERE run_id=?",(graph.encode([filename]),self.run['id']))
        review_id=uuid.uuid4().hex;now=self.a.now()
        self.a.execute('''INSERT INTO workflow_reviews(id,run_id,task_id,purpose,subject_task,graph_version,review_identity,
            snapshot_json,snapshot_digest,status,decision_json,created_at,updated_at)
            VALUES (?,?,'final-review','final','',?,'fixture','{}',?,'passed',?,?,?)''',
            (review_id,self.run['id'],self.run['graph_version'],'f'*64,
             graph.encode({'deliverables':[filename],'primary':filename}),now,now))
        review={'id':review_id,'decision_json':'{}'}
        run=graph.get(self.a,self.run['id']);request=self.message()
        with patch.dict('os.environ',{'VIDEO_USE_OBJECT_BUCKET':'metadata-race-fixture'}):
            _,accepted=self.race(lambda:Orchestrator(self.a).publish(run,review),
                lambda:accept_change(self.b,run['id'],'owner',request))
            again=accept_change(self.a,run['id'],'owner',request)
        self.assertEqual(accepted['id'],again['id'])
        original=graph.get(self.a,run['id'])
        if accepted['run_id']==run['id']:
            self.assertIsNone(original['delivered_at'])
            self.assertEqual(original['delivery'],[])
        else:
            self.assertIsNotNone(original['delivered_at'])
            successor=graph.get(self.a,accepted['run_id'])
            self.assertEqual(successor['parent_run_id'],run['id'])
            self.assertEqual(successor['session_id'],run['session_id'])
            self.assertFalse(successor['scope_confirmed']);self.assertEqual(successor['tasks'],[])
        self.assertLessEqual(len(self.a.query("SELECT id FROM runs WHERE parent_run_id=?",(run['id'],))),1)
