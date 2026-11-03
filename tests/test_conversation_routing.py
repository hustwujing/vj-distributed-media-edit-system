import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch,Mock
from fastapi.testclient import TestClient
from webapp import agent_store,plan_readiness
from webapp.agent_host import AgentHost
from webapp.store import Store
from webapp.task_leases import Coordinator
from webapp.task_usage import snapshot
from webapp.light_conversation import ConversationReceipt
from tests.readiness_support import ready_plan


def output(text='你好！',tool=None):
    return {'id':'resp_fixture','status':'completed','usage':{'input_tokens':300,'output_tokens':20,'total_tokens':320},
            'output':[{'type':'function_call','call_id':'call_fixture','name':tool[0],'arguments':json.dumps(tool[1])}] if tool else [{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]}

class LightRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root);agent_store.initialize(self.store)
        self.project=self.store.add_project('owner','light')['id'];self.responses=[];self.requests=[]
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                value=outer.responses.pop(0)
                body=('data: '+json.dumps({'type':'response.completed','response':value},ensure_ascii=False)+'\n\n').encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.host=object.__new__(AgentHost);self.host.store=self.store;self.host.model='fixture-model';self.host.key='fixture-provider-secret';self.host.base_url=f'http://127.0.0.1:{self.http.server_port}'
        self.host.stop=threading.Event();self.host.identities=SimpleNamespace(node='light-node',root=self.root/'identities');self.host.lock=threading.Lock();self.host.active={}
        self.coordinator=Coordinator(self.store,'light-node','fixture')
    def tearDown(self):self.http.shutdown();self.http.server_close();self.thread.join();self.tmp.cleanup()
    def task(self,prompt='你好'):
        task=agent_store.add_turn(self.store,self.project,'plan',prompt);plan_readiness.register(self.store,task);return task
    def run_task(self,task):
        lease=self.coordinator.claim(task,'agent');self.assertIsNotNone(lease)
        with patch('webapp.agent_host.TaskRuntime',side_effect=AssertionError('Light task allocated a sandbox')) as runtime:self.host._execute_claim(lease)
        runtime.assert_not_called();return agent_store.turn(self.store,task)
    def test_daily_planning_quota_has_precise_reason_and_no_upstream_request(self):
        day=int(self.store.now()//86400)
        self.store.execute('INSERT INTO execution_daily_usage VALUES (?,?,?,?)',('owner',day,'planning_model',40))
        task=self.task('继续讨论');row=self.run_task(task)
        self.assertEqual(row['status'],'failed');self.assertIn('规划模型',row['error']);self.assertNotIn('请重试',row['error'])
        self.assertEqual(row['quota']['scope'],'daily');self.assertEqual(row['quota']['reset_at'],(day+1)*86400)
        self.assertEqual(self.requests,[]);self.assertEqual(snapshot(self.store,task)['counts']['llm']['calls'],0)
        self.assertEqual(self.store.query('SELECT runtime FROM execution_attempts')[0]['runtime'],'')
        self.store.execute("UPDATE execution_daily_usage SET used=0 WHERE category='planning_model'")
        self.assertIsNone(agent_store.turn(self.store,task)['quota'])
        self.responses=[output('已恢复')];next_task=self.task('继续讨论')
        self.assertEqual(self.run_task(next_task)['status'],'succeeded');self.assertEqual(len(self.requests),1)

    def test_greeting_one_model_request_no_sandbox_no_checkpoint(self):
        self.responses=[output()];task=self.task();row=self.run_task(task)
        self.assertEqual(row['status'],'succeeded');self.assertEqual(row['text'],'你好！');self.assertFalse(row['plan_readiness']['can_execute'])
        self.assertEqual(self.store.query('SELECT * FROM execution_checkpoints'),[]);self.assertEqual(self.store.query('SELECT * FROM execution_cleanup'),[])
        usage=snapshot(self.store,task);self.assertEqual(usage['counts']['llm']['calls'],1);self.assertEqual(usage['tokens']['total_tokens']['known'],320)
        self.assertLess(len(json.dumps(self.requests[0],ensure_ascii=False)),4000)
        self.assertEqual(self.store.query('SELECT runtime FROM execution_attempts')[0]['runtime'],'')
    def test_metadata_query_uses_owned_database_data_without_runtime(self):
        self.store.add_asset(self.project,'video-id','movie.mp4','movie.mp4',{'kind':'video','duration':12.5,'width':640,'height':360})
        self.responses=[output(tool=('project_metadata',{'asset_ids':['video-id'],'query':''})),output('视频长 12.5 秒。')]
        task=self.task('这个视频多长？');row=self.run_task(task)
        self.assertEqual(row['status'],'succeeded');self.assertIn('12.5',row['text'])
        second=self.requests[1]['input'];self.assertIn('12.5',json.dumps(second));self.assertEqual(snapshot(self.store,task)['counts']['tool']['calls'],1)
    def test_saved_outputs_are_discoverable_and_can_request_readonly_inspection(self):
        foreign=self.store.add_project('other-owner','foreign')['id']
        for project,files in ((self.project,{'edit/output.png':{'size':12},'edit/previews/private/preview.png':{'size':4}}),(foreign,{'edit/foreign-secret.png':{'size':9}})):
            self.store.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?,?)',(project,'fixture','checkpoint-'+project,json.dumps({'workspace':files,'state':{}}),self.store.now()))
        self.responses=[output(tool=('project_metadata',{'asset_ids':[],'query':''})),
                        output(tool=('request_capability',{'capability':'inspect_media','asset_ids':[],'reason':'读取已有工作成果 output.png'}))]
        task=self.task('检查刚才生成的图片');row=self.run_task(task)
        self.assertEqual(row['status'],'queued');self.assertEqual(row['mode'],'plan');self.assertEqual(row['routing']['capability'],'inspect_media')
        received=json.loads(self.requests[1]['input'][-1]['output']);self.assertEqual(received['source_assets'],[])
        self.assertEqual(received['saved_work_files']['files'],[{'name':'edit/output.png','bytes':12}])
        self.assertNotIn('foreign-secret',json.dumps(self.requests));self.assertNotIn('preview.png',json.dumps(self.requests))
        self.assertTrue(any('saved_work_file_count' in item.get('content','') for item in self.requests[0]['input']))

    def test_unknown_pixel_content_commits_handoff_then_releases_lease(self):
        self.store.add_asset(self.project,'image-id','photo','photo.png',{'kind':'image','width':10,'height':10})
        self.responses=[output(tool=('request_capability',{'capability':'inspect_media','asset_ids':['image-id'],'reason':'需要查看实际画面内容'}))]
        task=self.task('图片上有什么？');row=self.run_task(task)
        self.assertEqual(row['status'],'queued');self.assertEqual(row['mode'],'plan');self.assertEqual(row['routing']['stage'],'heavy')
        self.assertEqual(self.store.query('SELECT active FROM execution_leases WHERE run_id=?',(task,))[0]['active'],0)
        from webapp.scheduling import requirements
        self.assertEqual(requirements(self.store,task,'agent')['memory_mb'],4096)
        restored=Store(self.root);agent_store.initialize(restored);self.assertEqual(plan_readiness.route(restored,task)['capability'],'inspect_media')
    def test_general_analysis_handoff_is_durable_plan_only_and_same_run(self):
        self.responses=[output(tool=('request_capability',{'capability':'general_analysis','asset_ids':[],'reason':'需要核实项目能力及上下文'}))]
        task=self.task('请核实这个项目还能接着做什么');row=self.run_task(task)
        self.assertEqual((row['id'],row['status'],row['mode']),(task,'queued','plan'))
        self.assertIsNone(row['execution_intent']);self.assertFalse(row['plan_readiness']['can_execute'])
        restored=Store(self.root);agent_store.initialize(restored);route=plan_readiness.route(restored,task)
        self.assertEqual((route['stage'],route['capability']),('heavy','general_analysis'));self.assertIn('核实',route['reason'])
        self.assertEqual(snapshot(restored,task)['run_id'],task)
        self.assertEqual(restored.query('SELECT active FROM execution_leases WHERE run_id=?',(task,))[0]['active'],0)
        self.assertEqual(restored.query('SELECT runtime FROM execution_attempts WHERE run_id=?',(task,))[0]['runtime'],'')

    def test_valid_query_round_limit_hands_off_without_fifth_model_request(self):
        for n in range(4):
            response=output(tool=('project_metadata',{'asset_ids':[],'query':''}));response['output'][0]['call_id']='query-'+str(n);self.responses.append(response)
        task=self.task('需要综合分析项目');row=self.run_task(task)
        self.assertEqual(len(self.requests),4);self.assertEqual(row['status'],'queued');self.assertEqual(row['mode'],'plan')
        route=plan_readiness.route(self.store,task);self.assertEqual(route['capability'],'general_analysis');self.assertIn('四轮',route['reason'])
        usage=snapshot(self.store,task);self.assertEqual(usage['counts']['llm']['calls'],4);self.assertEqual(usage['counts']['tool']['calls'],4)

    def test_invalid_tool_or_arguments_fail_without_generic_handoff(self):
        for call in [('unknown_tool',{}),('project_metadata',{'asset_ids':[],'query':0}),
                     ('request_capability',{'capability':'general_analysis','asset_ids':['foreign'],'reason':'read'}),
                     ('request_capability',{'capability':'general_analysis','asset_ids':[],'reason':'read','mode':'execute'})]:
            with self.subTest(tool=call):
                self.responses=[output(tool=call)];task=self.task();row=self.run_task(task)
                self.assertEqual(row['status'],'failed');self.assertEqual(row['routing']['stage'],'light')
                self.assertEqual(row['mode'],'plan');self.assertIsNone(row['execution_intent'])

    def test_cancelled_or_repeated_handoff_cannot_requeue_task(self):
        from webapp.light_conversation import handoff
        from webapp.task_leases import LeaseLost
        task=self.task();lease=self.coordinator.claim(task,'agent');self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,));row=agent_store.turn(self.store,task)
        with lease.bind():
            handoff(self.host,row,'general_analysis',[],'核实能力')
            with self.assertRaises(LeaseLost):handoff(self.host,row,'preview',[],'重复交接')
        self.assertEqual(plan_readiness.route(self.store,task)['capability'],'general_analysis')
        self.store.execute("UPDATE agent_turns SET status='cancelling' WHERE id=?",(task,))
        with lease.bind(),self.assertRaises(LeaseLost):handoff(self.host,row,'general_analysis',[],'继续分析')
        self.assertEqual(agent_store.turn(self.store,task)['status'],'cancelling')

    def test_unknown_capability_or_foreign_asset_cannot_escalate(self):
        for args in ({'capability':'execute','asset_ids':[],'reason':'run code'}, {'capability':'inspect_media','asset_ids':['foreign'],'reason':'read image'}):
            with self.subTest(args=args):
                self.responses=[output(tool=('request_capability',args))];task=self.task();row=self.run_task(task)
                self.assertEqual(row['status'],'failed');self.assertEqual(row['routing']['stage'],'light')
    def test_natural_confirmation_consumes_exact_plan_and_joins_run(self):
        plan=agent_store.add_turn(self.store,self.project,'plan','生成本地图片');ready_plan(self.store,plan)
        task=self.task('就按这个做')
        with patch('webapp.semantic_review.classify',return_value='confirm'):row=self.run_task(task)
        self.assertEqual((row['mode'],row['plan_id'],row['status']),('execute',plan,'queued'));self.assertFalse(plan_readiness.public(self.store,plan)['can_execute'])
        self.assertEqual(snapshot(self.store,task)['run_id'],plan);self.assertEqual(self.requests,[])
    def test_revision_and_rejection_never_confirm_old_plan(self):
        for decision in ('revise','reject'):
            plan=agent_store.add_turn(self.store,self.project,'plan','旧方案');ready_plan(self.store,plan);task=self.task('修改要求或拒绝')
            self.responses=[output('请说明新的要求。')]
            with patch('webapp.semantic_review.classify',return_value=decision):row=self.run_task(task)
            self.assertEqual(row['mode'],'plan');self.assertFalse(plan_readiness.public(self.store,plan)['can_execute'])
    def test_semantic_failure_cannot_start_execution(self):
        from webapp.semantic_review import SemanticUnavailable
        plan=agent_store.add_turn(self.store,self.project,'plan','旧方案');ready_plan(self.store,plan);task=self.task('开始吧')
        with patch('webapp.semantic_review.classify',side_effect=SemanticUnavailable()):row=self.run_task(task)
        self.assertEqual(row['mode'],'plan');self.assertEqual(row['status'],'succeeded');self.assertIn('无法明确判断',row['text'])

class QuotaAdmissionTests(unittest.TestCase):
    def test_api_rejects_daily_block_before_task_creation_and_recovers(self):
        from webapp.app import create_app
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Path(directory),False)
            with TestClient(app) as client:
                store=app.state.store
                (store.root/'agent-health.json').write_text(json.dumps({'ready':True,'updated':__import__('time').time()}))
                project=client.post('/api/projects',json={'name':'quota admission'}).json()['id']
                owner=store.query('SELECT owner FROM projects WHERE id=?',(project,))[0]['owner'];day=int(store.now()//86400)
                store.execute('INSERT INTO execution_daily_usage VALUES (?,?,?,?)',(owner,day,'planning_model',40))
                for _ in range(2):
                    response=client.post(f'/api/projects/{project}/agent/messages',json={'message':'continue analysis'})
                    self.assertEqual(response.status_code,429);self.assertEqual(response.json()['detail']['category'],'planning_model')
                    self.assertEqual(response.json()['detail']['reset_at'],(day+1)*86400)
                self.assertFalse(store.query('SELECT id FROM agent_turns WHERE project=?',(project,)))
                store.execute("UPDATE execution_daily_usage SET day=day-1 WHERE owner=?",(owner,))
                response=client.post(f'/api/projects/{project}/agent/messages',json={'message':'continue analysis'})
                self.assertEqual(response.status_code,202,response.text)
                self.assertEqual(len(store.query('SELECT id FROM agent_turns WHERE project=?',(project,))),1)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name));agent_store.initialize(self.store);self.project=self.store.add_project('owner','p')['id']
    def tearDown(self):self.tmp.cleanup()
    def test_successful_chat_is_not_executable_even_via_direct_api(self):
        from webapp.app import create_app
        app=create_app(Path(self.tmp.name)/'api',False)
        with TestClient(app) as c:
            p=c.post('/api/projects',json={'name':'p'}).json()['id'];s=app.state.store
            (s.root/'agent-health.json').write_text(json.dumps({'ready':True,'updated':__import__('time').time()}))
            t=c.post(f'/api/projects/{p}/agent/messages',json={'message':'你好'}).json()['id'];agent_store.finish(s,t,'succeeded','你好')
            self.assertEqual(c.post(f'/api/projects/{p}/agent/messages',json={'message':'执行','mode':'execute','plan_id':t}).status_code,409)
            self.assertFalse(c.get(f'/api/projects/{p}/agent').json()['turns'][0]['plan_readiness']['can_execute'])
    def test_input_mutation_invalidates_audited_plan(self):
        task=agent_store.add_turn(self.store,self.project,'plan','edit');ready_plan(self.store,task);self.assertTrue(plan_readiness.public(self.store,task)['can_execute'])
        self.store.add_asset(self.project,'new','new','new.png',{'kind':'image','width':1,'height':1});self.assertFalse(plan_readiness.public(self.store,task)['can_execute'])
        with self.assertRaises(ValueError):plan_readiness.require(self.store,self.project,task)
    def test_pending_review_on_failed_or_cancelled_plan_is_not_still_running(self):
        from webapp.execution_intent import declare
        for status in ('failed','cancelled'):
            with self.subTest(status=status):
                task=agent_store.add_turn(self.store,self.project,'plan','make image')
                self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                declare(self.store,task,{'route':'single','operations':['script'],'goal':'make image','estimated_seconds':30})
                self.assertEqual(plan_readiness.public(self.store,task)['state'],'pending')
                agent_store.finish(self.store,task,status,'Stopped before review')
                result=plan_readiness.public(self.store,task)
                self.assertEqual(result['state'],'unavailable');self.assertIn('本轮已结束',result['reason'])
                self.assertFalse(result['can_execute']);self.assertFalse(result['retry_available'])
                with self.assertRaises(ValueError):plan_readiness.require(self.store,self.project,task)
                with self.assertRaises(ValueError):plan_readiness.retry_review(self.store,task,result['digest'])

    def test_pending_review_retry_on_successful_plan_remains_pending(self):
        from webapp.execution_intent import declare
        task=agent_store.add_turn(self.store,self.project,'plan','make image')
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
        declare(self.store,task,{'route':'single','operations':['script'],'goal':'make image','estimated_seconds':30})
        agent_store.finish(self.store,task,'succeeded','A retry may review this completed plan')
        result=plan_readiness.public(self.store,task)
        self.assertEqual(result['state'],'pending');self.assertFalse(result['can_execute'])
    def test_intent_change_stale_digest_and_duplicate_approval_rejected(self):
        t=agent_store.add_turn(self.store,self.project,'plan','edit');ready_plan(self.store,t);d=plan_readiness.public(self.store,t)['digest']
        with self.assertRaises(ValueError):plan_readiness.require(self.store,self.project,t,'0'*64)
        plan_readiness.consume(self.store,self.project,t,'execute-task',d)
        with self.assertRaises(ValueError):plan_readiness.consume(self.store,self.project,t,'second',d)
        self.assertEqual(self.store.query('SELECT consumed_by FROM plan_readiness')[0]['consumed_by'],'execute-task')
    def test_confirmation_cannot_switch_selected_inputs(self):
        plan=agent_store.add_turn(self.store,self.project,'plan','edit',attachments=[{'id':'a'}]);ready_plan(self.store,plan)
        task=agent_store.add_turn(self.store,self.project,'plan','开始吧',attachments=[{'id':'b'}]);plan_readiness.register(self.store,task)
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
        with self.assertRaises(ValueError):plan_readiness.promote_confirmation(self.store,task,plan)
        self.assertTrue(plan_readiness.public(self.store,plan)['can_execute']);self.assertEqual(agent_store.turn(self.store,task)['mode'],'plan')

    def test_review_only_runs_for_declared_intent_and_failed_service_hides_button(self):
        from webapp.execution_intent import declare
        from webapp.semantic_review import SemanticUnavailable
        t=agent_store.add_turn(self.store,self.project,'plan','hello')
        with patch('webapp.semantic_review.classify') as check:plan_readiness.review(self.store,t,'hello');check.assert_not_called()
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(t,));declare(self.store,t,{'route':'single','operations':['script'],'goal':'make image','estimated_seconds':30})
        with patch('webapp.semantic_review.classify',side_effect=SemanticUnavailable()):plan_readiness.review(self.store,t,'proposal')
        agent_store.finish(self.store,t,'succeeded','proposal');self.assertEqual(plan_readiness.public(self.store,t)['state'],'unavailable');self.assertFalse(plan_readiness.public(self.store,t)['can_execute'])
    def test_full_history_retrieval_preserves_late_constraints_and_owner_scope(self):
        from webapp.conversation_history import lookup,write_snapshot
        original='背景信息。'*400+'关键限制：只能本地处理，不得调用外部服务。'
        t=agent_store.add_turn(self.store,self.project,'plan',original);agent_store.finish(self.store,t,'failed','',error='previous failure')
        current=agent_store.add_turn(self.store,self.project,'plan','修复上一轮')
        rows=lookup(self.store,self.project,current,'oldest');self.assertEqual(rows[0]['prompt'],original);self.assertEqual(rows[0]['error'],'previous failure')
        other=self.store.add_project('other','foreign')['id']
        with self.assertRaises(ValueError):lookup(self.store,other,current,before_id=t)
        target=Path(self.tmp.name)/'history.jsonl';write_snapshot(self.store,self.project,current,target)
        self.assertEqual(json.loads(target.read_text())['prompt'],original)

    def test_stream_parser_handles_partial_deltas_and_final_tool_response(self):
        chunks=[];r=ConversationReceipt(chunks.append)
        encoded=('data: '+json.dumps({'type':'response.output_text.delta','delta':'你好'},ensure_ascii=False)+'\n\ndata: '+json.dumps({'type':'response.completed','response':output()})+'\n\n').encode()
        for byte in encoded:r.feed(bytes([byte]))
        r.finish();self.assertEqual(''.join(chunks),'你好');self.assertEqual(r.response['status'],'completed');self.assertEqual(r.usage['total_tokens'],320)

class SemanticDecisionTests(unittest.TestCase):
    def test_invalid_uncertain_or_negative_answers_cannot_be_ready(self):
        from webapp.semantic_review import classify,CHOICES,SemanticUnavailable,SemanticUncertain
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));agent_store.initialize(store);p=store.add_project('owner','semantic')['id'];t=agent_store.add_turn(store,p,'plan','input')
            def reply(choice,scores):
                response=Mock();response.status_code=200;response.content=b'{}';response.json.return_value={'answers':{'decision':{'type':'choice','choice':choice,'confidence':scores.get(choice,0),'probabilities':scores}}}
                response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);return response
            for scores in ({'ready':.6,'needs_information':.3,'discussion':.1},{'ready':1.,'other':0.}):
                with patch('webapp.semantic_review.requests.Session') as session:
                    session.return_value.__enter__.return_value.post.return_value=reply('ready',scores)
                    with self.assertRaises(SemanticUnavailable):classify(store,t,'plan',{})
            with patch('webapp.semantic_review.requests.Session') as session:
                session.return_value.__enter__.return_value.post.return_value=reply('needs_information',{'ready':.01,'needs_information':.98,'discussion':.01})
                self.assertEqual(classify(store,t,'plan',{}),'needs_information')

    def test_cancelled_light_http_is_interrupted_without_sandbox(self):
        import time
        from webapp.task_lifecycle import cancel_task
        fixture=LightRoutingTests();fixture.setUp()
        server=None;worker=None
        try:
            entered=threading.Event();release=threading.Event()
            class Slow(BaseHTTPRequestHandler):
                def log_message(self,*a):pass
                def do_POST(self):entered.set();release.wait(10)
            server=ThreadingHTTPServer(('127.0.0.1',0),Slow);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            fixture.host.base_url=f'http://127.0.0.1:{server.server_port}';t=fixture.task('long response');lease=fixture.coordinator.claim(t,'agent')
            worker=threading.Thread(target=fixture.host._execute_claim,args=(lease,));worker.start();self.assertTrue(entered.wait(5));start=__import__('time').monotonic();cancel_task(fixture.store,t);worker.join(4)
            self.assertFalse(worker.is_alive());self.assertLess(time.monotonic()-start,4);self.assertEqual(agent_store.turn(fixture.store,t)['status'],'cancelled')
            self.assertEqual(fixture.store.query('SELECT runtime FROM execution_attempts')[0]['runtime'],'');self.assertEqual(snapshot(fixture.store,t)['counts']['llm']['calls'],1)
        finally:
            if server:release.set();server.shutdown();server.server_close();thread.join()
            if worker:worker.join(5)
            fixture.tearDown()
