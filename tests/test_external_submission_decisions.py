"""User-authorized recovery of an actual persisted, uncertain submission."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
import unittest
import requests

from tests import test_run_recovery as fixtures
from webapp import workflow_graph as graph,provider_calls
from webapp.task_leases import Coordinator
from webapp.workflow_executor import execute
from webapp.workflow_runtime import Adapter


class ExternalSubmissionDecisionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RunRecoveryTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.store=self.f.store;self.run=self.f.run;self.posts=[];self.gets=[]
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                outer.posts.append(self.path)
                self.reply(202,{'id':'job-'+str(len(outer.posts))})
            def do_GET(self):
                outer.gets.append(self.path);self.reply(200,{'status':'running'})
            def reply(self,status,value):
                body=json.dumps(value).encode();self.send_response(status)
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.base=f'http://127.0.0.1:{server.server_port}'
        spec=graph.Task.model_validate({'id':'render','type':'operation','operation':'image.edit','name':'image','objective':'Edit approved image',
            'inputs':{'source':{'asset':'asset'}},'parameters':{'prompt':'blue'},'acceptance':[{'kind':'file'}],
            'basic_reason':'Provider recovery fixture'}).model_dump()
        authorization=graph.bind_authorization({**self.run['authorization'],'allowed_operations':['script','image.edit'],
            'provider_call_budget':{'image.edit':2}}).model_dump()
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(graph.encode(authorization),self.run['id']))
        self.store.execute("UPDATE run_tasks SET type='operation',spec_json=?,phase='submit',status='ready' WHERE run_id=?",(graph.encode(spec),self.run['id']))
        adapter=Adapter(self.store,'fixture',self.store.root/'sockets')
        def submit(view,step):
            provider_calls.intent(self.store,view,step,self.base,'image.edit')
            with requests.Session() as client:
                response=client.post(self.base+'/api/jobs',json={'prompt':'blue'},timeout=2)
            if len(self.posts)==1:raise requests.Timeout('Receipt lost after request was sent')
            identifier=response.json()['id']
            provider_calls.update(self.store,view,step,'waiting',identifier)
            return identifier
        adapter.submit=submit
        self.host=SimpleNamespace(store=self.store,stop=threading.Event(),workflow_runner=SimpleNamespace(adapter=adapter))
        self.dispatch()
        row=self.store.query('SELECT state FROM workflow_provider_calls WHERE workflow=?',(self.run['id'],))[0]
        self.assertEqual(row['state'],'submitting')
        self.assertEqual(self.posts,['/api/jobs'])
        self.decision=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND kind='external_submission' AND status='pending'",(self.run['id'],))[0]

    def dispatch(self):
        lease=Coordinator(self.store,'submission-recovery','test').claim(self.run['id'],'workflow',step_id='render')
        self.assertIsNotNone(lease)
        try:
            with lease.bind():execute(self.host,lease)
        finally:lease.release()
        self.f.f.orchestrator.accept_results(graph.get(self.store,self.run['id']))

    def answer(self,action,expected_error='',**extra):
        command=graph.command(self.store,self.run['id'],'owner','answer',{'decision_id':self.decision['id'],
            'snapshot_digest':self.decision['snapshot_digest'],'action':action,**extra})
        self.f.f.tick()
        record=self.store.query('SELECT status,error_code FROM workflow_commands WHERE id=?',(command['id'],))[0]
        self.assertEqual(record,{'status':'rejected' if expected_error else 'applied','error_code':expected_error})
        return command

    def test_attach_id_persists_provider_identity_and_queries_without_resubmitting(self):
        self.answer('approve',external_id='job-1')
        self.dispatch()
        row=self.store.query('SELECT state,external_id FROM workflow_provider_calls WHERE workflow=?',(self.run['id'],))[0]
        self.assertEqual(row,{'state':'running','external_id':'job-1'})
        self.assertEqual(self.posts,['/api/jobs']);self.assertEqual(self.gets,['/api/jobs/job-1'])
        task=graph.get(self.store,self.run['id'])['tasks'][0]
        self.assertEqual((task['status'],task['phase']),('waiting','poll'))

    def test_explicit_resubmit_can_claim_and_keeps_both_intents_and_budget(self):
        self.answer('reject') # This decision's explicit action confirms resubmission.
        self.dispatch()
        rows=self.store.query('SELECT request_key,state,external_id FROM workflow_provider_calls WHERE workflow=? ORDER BY created',(self.run['id'],))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['state'],'superseded');self.assertIn(':superseded:',rows[0]['request_key'])
        self.assertEqual(rows[1]['state'],'waiting');self.assertEqual(rows[1]['external_id'],'job-2')
        self.assertEqual(self.posts,['/api/jobs','/api/jobs'])
        self.assertEqual(self.store.query("SELECT used FROM execution_budgets WHERE run_id=? AND category='provider:image.edit'",(self.run['id'],))[0]['used'],2)

    def test_recovery_waits_for_attempt_cleanup_and_can_be_retried_afterward(self):
        attempt=self.store.query('SELECT attempt_id FROM workflow_provider_calls WHERE workflow=?',(self.run['id'],))[0]['attempt_id']
        self.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'pending',?)",
            (attempt,self.run['id'],'worker',self.store.now()))
        self.answer('reject',expected_error='external_recovery_cleanup_pending')
        self.assertEqual(self.store.query('SELECT state FROM workflow_provider_calls WHERE workflow=?',(self.run['id'],))[0]['state'],'submitting')
        self.assertEqual(len(self.posts),1)
        self.store.execute("UPDATE execution_cleanup SET state='complete' WHERE attempt=?",(attempt,))
        self.answer('reject');self.dispatch()
        self.assertEqual(len(self.posts),2)

    def test_active_attempt_cannot_be_replaced_by_submission_decision(self):
        attempt=self.store.query('SELECT attempt_id FROM workflow_provider_calls WHERE workflow=?',(self.run['id'],))[0]['attempt_id']
        self.store.execute('UPDATE execution_leases SET active=1,expires=? WHERE attempt=?',(self.store.now()+60,attempt))
        self.answer('approve',expected_error='external_recovery_attempt_active',external_id='job-1')
        self.assertEqual(self.store.query('SELECT external_id FROM workflow_provider_calls WHERE workflow=?',(self.run['id'],))[0]['external_id'],'')
        self.assertEqual(self.gets,[])

    def test_resubmission_cannot_bypass_exhausted_provider_budget(self):
        run=graph.get(self.store,self.run['id'])
        authorization={**run['authorization'],'provider_call_budget':{'image.edit':1}}
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(graph.encode(authorization),run['id']))
        self.answer('reject');self.dispatch()
        self.assertEqual(len(self.posts),1)
        self.assertEqual(len(self.store.query('SELECT request_key FROM workflow_provider_calls WHERE workflow=?',(run['id'],))),1)
        self.assertEqual(self.store.query("SELECT used FROM execution_budgets WHERE run_id=? AND category='provider:image.edit'",(run['id'],))[0]['used'],1)

    def test_replayed_decision_request_does_not_archive_or_charge_twice(self):
        command=self.answer('reject')
        replay=graph.command(self.store,self.run['id'],'owner','answer',{'decision_id':self.decision['id'],
            'snapshot_digest':self.decision['snapshot_digest'],'action':'reject'},request_id=command['id'])
        self.assertEqual(replay['id'],command['id'])
        self.dispatch()
        self.assertEqual(len(self.posts),2)
        self.assertEqual(len(self.store.query('SELECT request_key FROM workflow_provider_calls WHERE workflow=?',(self.run['id'],))),2)
