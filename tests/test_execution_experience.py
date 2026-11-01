import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from webapp.safe_stream import SafeEvents,PrefixGuard
from webapp import agent_store,plan_readiness
from webapp.agent_text import TextPublisher
from webapp.store import Store
from webapp.task_leases import Coordinator


def frame(value):return ('data: '+json.dumps(value,ensure_ascii=True)+'\n\n').encode()

class SafeStreamTests(unittest.TestCase):
    def test_complete_safe_event_does_not_wait_for_following_chunk(self):
        s=SafeEvents(('secret-key-123456789',));data=frame({'type':'response.output_text.delta','delta':'完整句子。'})
        self.assertEqual(list(s.feed(data)),[data]);self.assertEqual(list(s.finish()),[])
    def test_real_http_sparse_event_is_forwarded_before_server_continues(self):
        import threading,time,requests
        from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
        from webapp.safe_stream import chunks
        for chunked in (False,True):
            with self.subTest(chunked=chunked):
                release=threading.Event();data=frame({'type':'response.output_text.delta','delta':'完整句子。'})
                class Handler(BaseHTTPRequestHandler):
                    protocol_version='HTTP/1.1'
                    def log_message(self,*args):pass
                    def do_GET(self):
                        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close')
                        if chunked:self.send_header('Transfer-Encoding','chunked')
                        self.end_headers()
                        self.wfile.write((('%x\r\n'%len(data)).encode()+data+b'\r\n') if chunked else data);self.wfile.flush()
                        release.wait(3)
                        if chunked:self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
                        self.close_connection=True
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    start=time.monotonic()
                    with requests.get('http://127.0.0.1:'+str(server.server_port),stream=True,timeout=5) as response:
                        stream=chunks(response);safe=b''.join(SafeEvents(('secret-key-123456789',)).feed(next(stream)))
                        self.assertEqual(safe,data);self.assertLess(time.monotonic()-start,1);release.set();list(stream)
                finally:release.set();server.shutdown();server.server_close();thread.join(2)

    def test_every_network_split_and_unicode(self):
        data=frame({'type':'response.output_text.delta','delta':'中文完整句子。'})
        for at in range(len(data)+1):
            s=SafeEvents(('secret-key-123456789',));self.assertEqual(b''.join([*s.feed(data[:at]),*s.feed(data[at:]),*s.finish()]),data)
    def test_secret_across_events_and_escaped_json_is_blocked_before_forwarding(self):
        key='secret-key-123456789'
        for cut in range(1,len(key)):
            s=SafeEvents((key,));first=frame({'type':'response.output_text.delta','item_id':'one','delta':key[:cut]})
            self.assertEqual(list(s.feed(first)),[])
            with self.assertRaises(ValueError):list(s.feed(frame({'type':'response.output_text.delta','item_id':'one','delta':key[cut:]})))
        s=SafeEvents((key,));escaped=('data: {"text":"'+''.join('\\u%04x'%ord(c) for c in key)+'"}\n\n').encode()
        with self.assertRaises(ValueError):list(s.feed(escaped))
    def test_prefix_mismatch_releases_frames_but_broken_stream_does_not(self):
        s=SafeEvents(('secret-key-123456789',));a=frame({'type':'response.output_text.delta','delta':'secret-'})
        self.assertEqual(list(s.feed(a)),[])
        with self.assertRaises(ValueError):list(s.finish())
        b=frame({'type':'response.output_text.delta','delta':'not-a-key。'})
        self.assertEqual(b''.join(s.feed(b)),a+b)
    def test_full_completion_and_error_fields_checked(self):
        for kind in ('response.completed','response.failed'):
            with self.assertRaises(ValueError):list(SafeEvents(('secret-key-123456789',)).feed(frame({'type':kind,'response':{'text':'secret-key-123456789'}})))
    def test_light_prefix_guard_does_not_hold_ordinary_tail(self):
        g=PrefixGuard(('secret-key-123456789',));self.assertEqual(g.feed('你好，完整文字。'),'你好，完整文字。')
        self.assertEqual(g.feed('secret-'),'');self.assertEqual(g.feed('不同'),'secret-不同')

class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(Path(self.tmp.name));agent_store.initialize(self.s)
        self.p=self.s.add_project('owner','experience')['id'];self.t=agent_store.add_turn(self.s,self.p,'plan','test');self.c=Coordinator(self.s,'test','node');self.lease=self.c.claim(self.t,'agent')
        self.s.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.t,))
    def test_final_text_preserves_public_process_without_affecting_consent_text(self):
        with self.lease.bind(),patch('webapp.agent_text.bus') as bus:
            bus.return_value.publish.return_value='1-1';p=TextPublisher(self.s,self.t)
            p.message('first','检查素材。',complete=True);p.message('second','预览已核实。',complete=True);p.message('last','最终方案。','final_answer',True)
            agent_store.finish(self.s,self.t,'succeeded','最终方案。')
        row=agent_store.turn(self.s,self.t);self.assertEqual(row['text'],'最终方案。')
        self.assertEqual(agent_store.public(row)['text'],'检查素材。\n\n预览已核实。\n\n最终方案。')
        self.assertEqual(len(row['messages']),3)
    def test_new_generation_keeps_previous_messages_and_cancellation_marks_partial(self):
        with self.lease.bind(),patch('webapp.agent_text.bus') as b:
            b.return_value.publish.return_value=None;p=TextPublisher(self.s,self.t);p.message('first','公开过程',complete=True)
        self.lease.release(interrupted=True);lease=self.c.claim(self.t,'agent');self.assertIsNotNone(lease)
        with lease.bind(),patch('webapp.agent_text.bus') as b:
            b.return_value.publish.return_value=None;p=TextPublisher(self.s,self.t);p.message('second','尚未完成');p.flush(complete=True);agent_store.finish(self.s,self.t,'cancelled','已取消')
        row=agent_store.turn(self.s,self.t);self.assertIn('公开过程',agent_store.public(row)['text']);self.assertIn('尚未完成',agent_store.public(row)['text']);self.assertEqual(row['messages'][-1]['status'],'interrupted')
    def test_legacy_public_notes_recover_without_rewriting_final(self):
        agent_store.event(self.s,self.t,'note',{'id':'one','text':'旧过程'});agent_store.finish(self.s,self.t,'succeeded','最终方案')
        self.assertEqual(agent_store.public(agent_store.turn(self.s,self.t))['text'],'旧过程\n\n最终方案')
    def test_terminal_activity_cannot_be_resurrected_even_with_later_clock(self):
        from webapp.live_activity import record,current
        p={'id':'one','actor':'host','phase':'checkpoint','label':'保存','started':1,'updated':2,'status':'completed','sequence':2}
        with self.s.connect() as db:
            record(db,self.t,p);record(db,self.t,{**p,'updated':99,'status':'running','sequence':1})
        self.assertEqual(current(self.s,self.t)[0]['status'],'completed')
    def test_activity_completion_survives_a_clock_earlier_than_start_receipt(self):
        from webapp.live_activity import record,current
        p={'id':'fast','actor':'sandbox_agent','phase':'executing','label':'read','started':1,'updated':2.025,'status':'running','sequence':1}
        with self.s.connect() as db:
            record(db,self.t,p);record(db,self.t,{**p,'updated':2.,'status':'completed','sequence':2})
        self.assertEqual(current(self.s,self.t)[0]['status'],'completed')

    def test_planning_limits_are_generic_and_do_not_limit_execute(self):
        from webapp.planning_budget import check,PlanningLimit
        from webapp.task_usage import start
        with self.s.connect() as db:start(db,self.t,'llm','test')
        with patch.dict('os.environ',{'VIDEO_USE_PLAN_MODELS':'1'}):
            with self.assertRaises(PlanningLimit):check(self.lease)
            check(self.lease,admission=False)  # The last allowed response may finish.
            self.s.execute("UPDATE agent_turns SET mode='execute' WHERE id=?",(self.t,));check(self.lease)
    def test_review_retry_preserves_unknown_receipt_and_is_version_bound(self):
        from webapp.execution_intent import declare
        from webapp.semantic_review import SemanticUnavailable
        declare(self.s,self.t,{'route':'single','operations':['script'],'goal':'test','estimated_seconds':30})
        with patch('webapp.semantic_review.classify',side_effect=SemanticUnavailable()):plan_readiness.review(self.s,self.t,'方案')
        agent_store.finish(self.s,self.t,'succeeded','方案');digest=plan_readiness.public(self.s,self.t)['digest']
        with self.assertRaises(ValueError):plan_readiness.retry_review(self.s,self.t,'0'*64)
        with patch('webapp.semantic_review.classify',side_effect=SemanticUnavailable()):
            plan_readiness.retry_review(self.s,self.t,digest);plan_readiness.retry_review(self.s,self.t,digest)
            with self.assertRaises(ValueError):plan_readiness.retry_review(self.s,self.t,digest)
        self.assertFalse(plan_readiness.public(self.s,self.t)['can_execute'])

    def test_fallback_review_requires_all_evidence_and_records_its_own_receipt(self):
        from webapp.semantic_review import fallback_plan,SemanticUnavailable
        from unittest.mock import Mock
        fields={'requested_work':True,'inputs_identified':True,'parameters_resolved':True,'deliverable_defined':False}
        response=Mock();response.status_code=200;response.content=b'{}';response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.json.return_value={'status':'completed','id':'resp_fixture','usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'decision':'ready','confidence':.99,**fields})}]}]}
        with patch.dict('os.environ',{'VIDEO_USE_MODEL_BASE_URL':'https://provider.invalid','VIDEO_USE_MODEL_API_KEY':'fixture-key'}),patch('webapp.egress_policy.session') as session:
            session.return_value.__enter__.return_value.post.return_value=response
            with self.assertRaises(SemanticUnavailable):fallback_plan(self.s,self.t,{})
        calls=self.s.query('SELECT operation,total_tokens FROM usage_calls WHERE task_id=?',(self.t,));self.assertEqual(len(calls),1);self.assertIn('plan_fallback',calls[0]['operation']);self.assertEqual(calls[0]['total_tokens'],15)

    def test_natural_language_confirmation_timeout_never_uses_fallback(self):
        import requests
        from webapp.semantic_review import classify,SemanticUnavailable
        with patch('webapp.semantic_review.requests.Session') as session,patch('webapp.semantic_review.fallback_plan') as fallback:
            session.return_value.__enter__.return_value.post.side_effect=requests.ReadTimeout()
            with self.assertRaises(SemanticUnavailable):classify(self.s,self.t,'confirmation',{})
            fallback.assert_not_called()


class ExperienceDeletionTests(unittest.TestCase):
    def test_project_deletion_removes_public_messages_and_render_observations(self):
        from fastapi.testclient import TestClient
        from webapp.app import create_app
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with TestClient(create_app(root,run_worker=False)) as client:
                project=client.post('/api/projects',json={'name':'delete-test'}).json()['id'];store=Store(root)
                task=agent_store.add_turn(store,project,'plan','hello')
                with patch('webapp.agent_text.bus') as bus:
                    bus.return_value.publish.return_value=None;p=TextPublisher(store,task);p.message('one','private explanation',complete=True)
                agent_store.finish(store,task,'succeeded','reply')
                store.execute('INSERT INTO agent_render_observations VALUES (?,1,1,2,?)',(task,store.now()))
                store.execute('INSERT INTO plan_review_retries VALUES (?,1)',(task,))
                response=client.delete('/api/projects/'+project);self.assertEqual(response.status_code,200,response.text)
                for table,key in [('agent_messages','task_id'),('agent_render_observations','task_id'),('plan_review_retries','turn_id')]:self.assertFalse(store.query(f'SELECT * FROM {table} WHERE {key}=?',(task,)))
