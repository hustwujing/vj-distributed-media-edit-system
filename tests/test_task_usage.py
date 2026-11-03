import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from fastapi.testclient import TestClient
from webapp import agent_store,task_usage,workflows
from webapp.store import Store
from webapp.task_leases import Coordinator,LeaseLost
from webapp.usage_protocol import Receipt,normalize

USAGE={'input_tokens':123,'output_tokens':27,'total_tokens':150,'input_tokens_details':{'cached_tokens':80},'output_tokens_details':{'reasoning_tokens':9}}

def frame(usage=USAGE,status='completed'):
    return json.dumps({'type':'response.'+status,'response':{'id':'resp_test','status':status,'usage':usage,'output':[{'text':'秘密中文'}]}},ensure_ascii=False).encode()

class ReceiptTests(unittest.TestCase):
    def test_sse_at_every_byte_boundary_preserves_unicode_and_crlf(self):
        data=b'event: response.completed\r\ndata: '+frame()+b'\r\n\r\ndata: [DONE]\r\n\r\n'
        for split in range(len(data)+1):
            r=Receipt();r.feed(data[:split]);r.feed(data[split:]);r.finish()
            self.assertEqual(r.usage,normalize(USAGE));self.assertEqual(r.status,'succeeded')
        r=Receipt()
        for byte in data:r.feed(bytes([byte]))
        r.finish();self.assertEqual(r.usage['total_tokens'],150)
    def test_json_failed_and_incomplete_receipts_keep_usage(self):
        for status in ('completed','failed','incomplete','cancelled'):
            r=Receipt();r.feed(frame(status=status));r.finish()
            self.assertEqual(r.usage['input_tokens'],123)
    def test_deltas_missing_usage_and_malformed_data_are_not_fabricated(self):
        r=Receipt();r.feed(b'data: {"type":"response.output_text.delta","delta":"usage 1000"}\n\n');r.finish()
        self.assertIsNone(r.usage);self.assertIsNone(r.status)
        for value in (None,[],{}, {'input_tokens':-1,'output_tokens':True,'total_tokens':'3'}):
            self.assertTrue(all(v is None for v in normalize(value).values()))
    def test_bounded_parser_and_duplicate_frames(self):
        r=Receipt();data=b'data: '+frame()+b'\n\n';r.feed(data+data);r.finish();self.assertEqual(r.usage['total_tokens'],150)
        r=Receipt();r.limit=20;r.feed(b'x'*21);r.finish();self.assertTrue(r.truncated);self.assertIsNone(r.usage)

class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name));agent_store.initialize(self.store)
        self.project=self.store.add_project('owner','usage')['id'];self.task=agent_store.add_turn(self.store,self.project,'plan','private prompt')
    def tearDown(self):self.tmp.cleanup()
    def call(self):
        with self.store.connect() as db:return task_usage.start(db,self.task,'llm','model')
    def test_receipts_idempotent_conflicts_visible_no_double_count(self):
        call=self.call()
        for _ in range(3):
            with self.store.connect() as db:task_usage.complete(db,self.task,call,'succeeded',normalize(USAGE))
        with self.store.connect() as db:task_usage.complete(db,self.task,call,'unknown')
        s=task_usage.snapshot(self.store,self.task);self.assertEqual(s['counts']['llm']['calls'],1);self.assertEqual(s['tokens']['total_tokens']['known'],150)
        with self.store.connect() as db:task_usage.complete(db,self.task,call,'succeeded',normalize({**USAGE,'total_tokens':151}))
        s=task_usage.snapshot(self.store,self.task);self.assertEqual(s['calls'][0]['receipt'],'conflict');self.assertEqual(s['tokens']['total_tokens']['known'],150)
    def test_completed_tool_then_late_started_is_one_call(self):
        payload={'id':'one','status':'completed','tool_type':'commandExecution','execution':{'attempt_id':'attempt-one'}}
        agent_store.event(self.store,self.task,'tool',payload)
        agent_store.event(self.store,self.task,'tool',{**payload,'status':'running'})
        agent_store.event(self.store,self.task,'tool',{**payload,'execution':{'attempt_id':'attempt-two'}})
        s=task_usage.snapshot(self.store,self.task);self.assertEqual(s['counts']['tool'],{'calls':2,'states':{'succeeded':2}})
    def test_run_lineage_explicit_and_new_demands_separate(self):
        agent_store.finish(self.store,self.task,'succeeded')
        execution=agent_store.add_turn(self.store,self.project,'execute','confirmed',plan_id=self.task)
        agent_store.finish(self.store,execution,'succeeded')
        continuation=agent_store.add_turn(self.store,self.project,'plan','change',parent_task_id=execution)
        agent_store.finish(self.store,continuation,'succeeded')
        new=agent_store.add_turn(self.store,self.project,'plan','new request')
        self.assertEqual(len(task_usage.snapshot(self.store,execution,'run')['members']),3)
        self.assertEqual(len(task_usage.snapshot(self.store,new,'run')['members']),1)
        other=self.store.add_project('owner','other')['id']
        with self.assertRaises(ValueError):agent_store.add_turn(self.store,other,'plan','cross',parent_task_id=self.task)
        self.assertEqual(self.store.query('SELECT id FROM agent_turns WHERE project=?',(other,)),[])
    def test_pagination_does_not_limit_summary_and_no_private_payloads(self):
        for _ in range(4):
            call=self.call()
            with self.store.connect() as db:task_usage.complete(db,self.task,call,'succeeded',normalize(USAGE))
        first=task_usage.snapshot(self.store,self.task,limit=2);second=task_usage.snapshot(self.store,self.task,after=first['next_after'],limit=2)
        self.assertEqual(first['tokens']['total_tokens']['known'],600);self.assertEqual(len(second['calls']),2)
        self.assertNotIn('private prompt',json.dumps(first));self.assertIsNone(second['next_after'])
    def test_missing_usage_is_unknown_and_quotas_separate(self):
        self.call();self.store.execute('INSERT INTO execution_budgets VALUES (?,?,?)',(self.task,'model',7))
        s=task_usage.snapshot(self.store,self.task);self.assertEqual(s['counts']['llm']['calls'],1)
        self.assertEqual(s['quota_consumption'],[{'category':'model','used':7}]);self.assertEqual(s['tokens']['total_tokens'],{'known':None,'reported_calls':0,'missing_calls':1})
    def test_cancelled_lease_can_only_record_prior_receipt(self):
        from webapp.task_lifecycle import cancel_task
        lease=Coordinator(self.store,'a','test').claim(self.task,'agent')
        with lease.bind():
            self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.task,))
            with self.assertRaises(LeaseLost):
                with task_usage.model_call(lease,'model') as r:
                    r.feed(b'data: '+frame()+b'\n\n')
                    from webapp.task_lifecycle import administrative
                    with administrative():cancel_task(self.store,self.task)
                    raise LeaseLost('revoked')
        self.assertEqual(agent_store.turn(self.store,self.task)['status'],'cancelled')
        self.assertEqual(task_usage.snapshot(self.store,self.task)['tokens']['total_tokens']['known'],150)
        with self.store.connect() as db:
            with self.assertRaises(ValueError):task_usage.complete(db,'other',self.store.query('SELECT call_id FROM usage_calls')[0]['call_id'],'succeeded',normalize(USAGE))
    def test_interrupted_stream_survives_store_reopen_without_zero_tokens(self):
        lease=Coordinator(self.store,'a','test').claim(self.task,'agent')
        with lease.bind():
            with self.assertRaises(ConnectionError):
                with task_usage.model_call(lease,'model') as r:r.feed(b'data: {"type":"response.created"}\n\n');raise ConnectionError()
        lease.release(interrupted=True)
        reopened=Store(Path(self.tmp.name));agent_store.initialize(reopened)
        s=task_usage.snapshot(reopened,self.task);self.assertEqual(s['calls'][0]['status'],'unknown');self.assertIsNone(s['tokens']['total_tokens']['known'])
    def test_restart_detects_unfinished_request_without_mutating_task(self):
        lease=Coordinator(self.store,'a','test').claim(self.task,'agent')
        with self.store.connect() as db:task_usage.start(db,self.task,'llm','model',attempt=lease.attempt)
        lease.release(interrupted=True);s=task_usage.snapshot(self.store,self.task)
        self.assertEqual(s['calls'][0]['status'],'unknown');self.assertEqual(s['unfinished_calls_on_finished_attempts'],1)
    def test_historical_mapping_and_unknown_tokens(self):
        self.store.execute('DELETE FROM usage_tasks');agent_store.initialize(self.store)
        s=task_usage.snapshot(self.store,self.task);self.assertEqual(s['coverage']['historical_tasks'],[self.task]);self.assertIsNone(s['tokens']['total_tokens']['known'])
    def test_provider_numbers_only_and_poll_not_generation(self):
        from webapp import provider_calls
        w={'id':self.task,'run_id':self.task,'manual':True};step={'id':'image','revision':0,'attempts':0}
        provider_calls.intent(self.store,w,step,'https://private.invalid','image.edit')
        for phase in ('submit','poll','poll'):
            with provider_calls.request_time(self.store,w,step,phase):pass
        provider_calls.update(self.store,w,step,'succeeded','id',{'usage':{'total_tokens':12,'url':'https://secret.invalid'},'cost':{'amount':1.2,'currency':'USD','token':'secret'}})
        s=task_usage.snapshot(self.store,self.task);self.assertEqual(s['provider']['actual_submissions'],1);self.assertEqual(s['provider']['actual_polls'],2)
        self.assertNotIn('secret',json.dumps(s));self.assertNotIn('private.invalid',json.dumps(s));self.assertEqual(s['provider']['details'][0]['cost'],{'amount':1.2,'currency':'USD'})
    def test_control_classifier_requests_count_but_exact_controls_do_not(self):
        from webapp.control_intent import ControlIntentClassifier,CRITERIA
        classifier=ControlIntentClassifier()
        result={'answers':{'action':{'type':'choice','choice':'status','confidence':1.,'probabilities':{k:float(k=='status') for k in CRITERIA}}},'usage':USAGE}
        response=Mock();response.status_code=200;response.json.return_value=result;response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch('webapp.control_intent.requests.Session') as session,task_usage.control_target(self.store,self.task):
            session.return_value.__enter__.return_value.post.return_value=response
            self.assertEqual(classifier.resolve('查进度','running'),'status')
            session.assert_not_called()
            self.assertEqual(classifier.resolve('现在进展到哪个阶段了','running'),'status')
        usage=task_usage.snapshot(self.store,self.task)
        self.assertEqual(usage['counts']['llm']['calls'],1);self.assertEqual(usage['tokens']['total_tokens']['known'],150)

    def test_api_owner_cross_project_and_input_validation(self):
        from webapp.app import create_app
        app=create_app(Path(self.tmp.name)/'api',False)
        with TestClient(app) as a,TestClient(app) as b:
            p=a.post('/api/projects',json={'name':'usage'}).json()['id'];s=app.state.store;t=agent_store.add_turn(s,p,'plan','private')
            url=f'/api/projects/{p}/tasks/{t}/usage'
            self.assertEqual(a.get(url).status_code,200);self.assertEqual(b.get(url).status_code,404)
            self.assertEqual(a.get(url+'?scope=project').status_code,200);self.assertEqual(b.get(url+'?scope=project').status_code,404)
            for query in ('limit=1000','after=-1','scope=all'):self.assertEqual(a.get(url+'?'+query).status_code,422)
            q=a.post('/api/projects',json={'name':'other'}).json()['id'];self.assertEqual(a.get(f'/api/projects/{q}/tasks/{t}/usage').status_code,404)
    def test_project_scope_includes_independent_runs_but_never_other_projects(self):
        first=self.call()
        agent_store.finish(self.store,self.task,'succeeded','reply')
        second=agent_store.add_turn(self.store,self.project,'plan','independent')
        with self.store.connect() as db:task_usage.start(db,second,'llm','model')
        foreign=self.store.add_project('owner','other')['id'];other=agent_store.add_turn(self.store,foreign,'plan','foreign')
        with self.store.connect() as db:task_usage.start(db,other,'llm','private-model')
        self.assertEqual(task_usage.snapshot(self.store,second,'run')['counts']['llm']['calls'],1)
        project=task_usage.snapshot(self.store,second,'project');self.assertEqual(project['counts']['llm']['calls'],2)
        self.assertEqual({m['task_id'] for m in project['members']},{self.task,second});self.assertNotIn('private-model',json.dumps(project))

    def test_calculated_totals_preserve_receipts_missing_values_and_later_enrichment(self):
        calls=[self.call() for _ in range(3)]
        with self.store.connect() as db:
            task_usage.complete(db,self.task,calls[0],'succeeded',{'input_tokens':12,'output_tokens':3})
            task_usage.complete(db,self.task,calls[1],'succeeded',{'input_tokens':0,'output_tokens':0,'total_tokens':0})
            task_usage.complete(db,self.task,calls[2],'unknown',{'input_tokens':7})
        snap=task_usage.snapshot(self.store,self.task);self.assertEqual(snap['tokens']['total_tokens']['known'],0)
        self.assertEqual(snap['computed_total'],{'known':15,'derived_calls':1,'missing_calls':1,'inconsistent_calls':0})
        self.assertIsNone(self.store.query('SELECT total_tokens FROM usage_calls WHERE call_id=?',(calls[0],))[0]['total_tokens'])
        with self.store.connect() as db:task_usage.complete(db,self.task,calls[0],'succeeded',{'total_tokens':15})
        fresh=task_usage.snapshot(self.store,self.task);self.assertEqual(fresh['computed_total']['known'],15);self.assertEqual(fresh['computed_total']['derived_calls'],0)

    def test_conflicting_totals_are_not_silently_replaced(self):
        call=self.call()
        with self.store.connect() as db:task_usage.complete(db,self.task,call,'succeeded',{'input_tokens':10,'output_tokens':2,'total_tokens':20})
        result=task_usage.snapshot(self.store,self.task);self.assertEqual(result['computed_total']['known'],20);self.assertEqual(result['computed_total']['inconsistent_calls'],1)

    def test_workflow_retries_revision_changes_and_control_not_counted(self):
        from tests.test_workflows import WorkflowTests,local_step
        fixture=WorkflowTests();fixture.setUp()
        try:
            fixture.submit([local_step()]);fixture.adapter.local_error=True
            for _ in range(3):fixture.tick()
            s=task_usage.snapshot(fixture.store,fixture.turn)
            self.assertEqual(s['counts']['workflow']['calls'],3);self.assertEqual(s['workflow_retries'],2)
            w=fixture.get();workflows.change(fixture.store,w['id'],'package',status='pending',revision=1,attempts=0,repair_count=1)
            workflows.state(fixture.store,fixture.turn,'ready');fixture.adapter.local_error=False;fixture.tick()
            s=task_usage.snapshot(fixture.store,fixture.turn);self.assertEqual(s['workflow_retries'],2);self.assertEqual(s['repairs'],1)
        finally:fixture.tearDown()

class DistributedUsageTests(unittest.TestCase):
    def test_two_nodes_duplicate_receipt_and_database_restart(self):
        from tests.distributed_support import PostgresServer
        pg=PostgresServer()
        try:
            with tempfile.TemporaryDirectory() as folder:
                a=Store(Path(folder)/'a',dsn=pg.dsn);agent_store.initialize(a)
                b=Store(Path(folder)/'b',dsn=pg.dsn);project=a.add_project('owner','pg')['id'];task=agent_store.add_turn(a,project,'plan','private')
                with a.connect() as db:call=task_usage.start(db,task,'llm','model')
                errors=[]
                def receive(store):
                    try:
                        for _ in range(5):
                            with store.connect() as db:task_usage.complete(db,task,call,'succeeded',normalize(USAGE))
                    except Exception as exc:errors.append(type(exc).__name__)
                threads=[threading.Thread(target=receive,args=(store,)) for store in (a,b)]
                for t in threads:t.start()
                for t in threads:t.join()
                self.assertFalse(errors)
                pg.command('pg_ctl','-D',str(pg.root/'data'),'-m','fast','-w','stop');pg.start()
                result=task_usage.snapshot(b,task);self.assertEqual(result['tokens']['total_tokens']['known'],150);self.assertEqual(result['counts']['llm']['calls'],1)
                self.assertIs(type(result['tokens']['total_tokens']['known']),int)
                self.assertNotIn('private',json.dumps(result))
                from webapp.diagnostics import diagnose
                from fastapi.responses import JSONResponse
                self.assertEqual(JSONResponse(diagnose(b,task,include_logs=False)).status_code,200)
        finally:pg.close()


class PostgresLiveUsageTests(unittest.TestCase):
    def test_active_sandbox_usage_has_a_database_clock_and_project_totals(self):
        from tests.distributed_support import PostgresServer
        pg=PostgresServer()
        try:
            with tempfile.TemporaryDirectory() as folder:
                store=Store(Path(folder),dsn=pg.dsn);agent_store.initialize(store);project=store.add_project('owner','live')['id'];task=agent_store.add_turn(store,project,'plan','read')
                coordinator=Coordinator(store,'stats-node','test');lease=coordinator.claim(task,'agent')
                try:
                    store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                    store.execute("UPDATE execution_attempts SET runtime='fixture-runtime' WHERE id=?",(lease.attempt,))
                    with store.connect() as db:
                        call=task_usage.start(db,task,'llm','fixture',attempt=lease.attempt)
                        task_usage.complete(db,task,call,'succeeded',{'input_tokens':9,'output_tokens':2})
                    result=task_usage.snapshot(store,task,scope='project')
                    self.assertGreaterEqual(result['planning_budget']['used']['seconds'],0)
                    self.assertEqual(result['computed_total']['known'],11)
                    self.assertEqual(result['execution_counts']['sandboxes'],1)
                finally:lease.release()
        finally:pg.close()
