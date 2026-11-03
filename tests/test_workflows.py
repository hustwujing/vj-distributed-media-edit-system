"""Recovery and side-effect tests; no real model or paid provider calls."""
import io
import json
import os
import queue
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from webapp import agent_store, workflows
from webapp.app import create_app
from webapp.store import Store
from webapp.workflow_runtime import Adapter


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.record = {'status':'running'}
        self.submit_error = False
        self.local_error = False
        self.poll_error = False

    def submit(self, w, s):
        self.calls.append(('submit', s['id']))
        if self.submit_error:raise TimeoutError('response lost after provider accepted')
        return 'job-'+s['id']

    def poll(self, w, s):
        self.calls.append(('poll', s['external_id']))
        if self.poll_error:raise TimeoutError('temporarily offline')
        return self.record

    def collect(self, w, s, record, cancelled):
        self.calls.append(('collect', s['id']))
        return ['edit/workflows/'+s['id']+'/image.png']

    def local(self, w, s, cancelled):
        self.calls.append(('local', s['id']))
        if self.local_error:raise RuntimeError('script failed')
        return ['edit/workflows/'+s['id']+'/final.png']

    def preflight(self,w,s):return {'passed':True,'files':{}}

    def validate(self,w,s,files):
        return {'technical_valid':True,'passed':True,'requires_review':False,'requires_agent':False,'files':[],'checks':[]}

    def publish(self, w, files):
        self.calls.append(('publish', tuple(files)))

    def cancel(self, w, s):
        self.calls.append(('cancel', s['external_id']))


def local_step(id='package', dependencies=None, safe=True):
    return {'id':id, 'name':id, 'kind':'script', 'depends_on':dependencies or [],
            'argv':['python', 'edit/package.py'], 'outputs':['final.png'], 'retry_safe':safe,'goal':'Produce the requested result',
            'acceptance':[{'kind':'file'}],'basic_reason':'This fixture tests orchestration only','on_failure':'pause'}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        from tests.isolation import temporary_root
        temp_root = temporary_root()
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.store = Store(Path(self.tmp.name))
        agent_store.initialize(self.store)
        self.project = self.store.add_project('owner', 'test')['id']
        self.plan = agent_store.add_turn(self.store, self.project, 'plan', 'Generate and package')
        agent_store.finish(self.store, self.plan, 'succeeded', 'approved scope')
        self.turn = agent_store.add_turn(self.store, self.project, 'execute', 'Confirmed', plan_id=self.plan)
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?", (self.turn,))
        self.store.add_asset(self.project, 'asset', 'source.png', 'source.png', {'kind':'image', 'width':8, 'height':8})
        self.adapter = FakeAdapter()
        self.stop = threading.Event()
        self.runner = workflows.Runner(self.store, self.adapter, self.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, steps=None):
        definition = {'key':'approved-v1', 'name':'Generate and package', 'steps':steps or [
            {'id':'generate', 'name':'generate', 'kind':'operation','operation':'image.edit','inputs':{'source':{'asset':'asset'}},'parameters':{'prompt':'blue'},'goal':'Generate the approved image','on_failure':'pause','acceptance':[{'kind':'image','width':8,'height':8}]},
            local_step(dependencies=['generate'])]}
        result = workflows.submit(self.store, self.turn, definition)
        workflows.state(self.store, self.turn, 'ready')
        return result, definition

    def tick(self):
        self.store.execute('UPDATE agent_steps SET next_poll=0')
        self.runner.tick(self.turn)

    def test_large_workflow_snapshot_has_bounded_queries_and_per_step_diagnostics(self):
        self.submit([local_step('s'+str(i)) for i in range(28)])
        wid=self.get()['id']
        for i in range(5):
            self.store.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',
                ('diagnostic-'+str(i),wid,'s0',0,i,json.dumps({'attempt':i}),float(i)))
        with patch.object(self.store,'query',wraps=self.store.query) as query:
            result=self.get()
        self.assertLessEqual(query.call_count,7)
        self.assertEqual([r['attempt'] for r in result['steps'][0]['diagnostics']],[4,3,2])
        self.assertFalse(result['steps'][1]['diagnostics'])

    def test_restart_polls_existing_job_and_automatically_resumes_original_turn(self):
        self.submit()
        self.tick()
        self.assertEqual(self.get()['steps'][0]['external_id'], 'job-generate')
        self.runner = workflows.Runner(Store(Path(self.tmp.name)), self.adapter, self.stop)
        self.runner.recover()
        self.tick()
        self.assertNotIn(('local', 'package'), self.adapter.calls)
        self.adapter.record = {'status':'succeeded', 'artifacts':[]}
        self.tick()
        self.tick()
        self.tick()
        self.assertEqual(self.adapter.calls.count(('submit', 'generate')), 1)
        self.assertEqual(self.adapter.calls.count(('local', 'package')), 1)
        turn = agent_store.turn(self.store, self.turn)
        self.assertEqual(turn['status'], 'queued')
        self.assertEqual(turn['plan_id'], self.plan)
        self.assertEqual(self.get()['status'], 'resuming')
        self.assertEqual(len(self.store.query("SELECT id FROM agent_turns WHERE mode='execute'")), 1)

    def get(self):
        return workflows.get(self.store, self.turn)

    def test_unknown_submission_never_retries_even_after_restart(self):
        self.submit()
        self.adapter.submit_error = True
        self.tick()
        self.runner.recover()
        self.tick()
        self.assertEqual(self.adapter.calls.count(('submit', 'generate')), 1)
        self.assertEqual(self.get()['status'], 'paused')
        self.assertEqual(self.get()['steps'][0]['error_code'], 'submission_unknown')
        with self.assertRaises(ValueError):workflows.resume(self.store, self.turn)

    def test_crash_during_submit_intent_is_ambiguous(self):
        self.submit()
        workflows.change(self.store, self.get()['id'], 'generate', status='submitting', attempts=1)
        self.runner.recover()
        self.tick()
        self.assertEqual(self.get()['status'], 'paused')
        self.assertFalse(any(c[0]=='submit' for c in self.adapter.calls))

    def test_cancel_between_scheduling_and_claim_prevents_submission(self):
        self.submit()
        stale = self.get()
        workflows.cancel(self.store, self.turn)
        self.runner.step(stale, stale['steps'][0])
        self.assertFalse(any(c[0]=='submit' for c in self.adapter.calls))

    def test_recovery_of_crash_between_step_pause_and_workflow_pause(self):
        self.submit()
        workflows.change(self.store, self.get()['id'], 'generate', status='paused', error_code='submission_unknown')
        self.runner.recover()
        self.assertEqual(self.get()['status'], 'paused')
        self.assertFalse(workflows.public(self.get())['resumable'])

    def test_readonly_final_review_recovers_without_replaying_outputs(self):
        self.submit([local_step()])
        self.tick();self.tick()
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?", (self.turn,))
        self.runner.recover()
        self.assertEqual(self.get()['status'], 'resuming')
        self.tick()
        self.assertEqual(self.adapter.calls.count(('local', 'package')), 1)
        self.assertEqual(agent_store.turn(self.store, self.turn)['status'], 'queued')

    def test_duplicate_submission_returns_same_plan_and_rejects_replacement(self):
        first, definition = self.submit()
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?", (self.turn,))
        same = workflows.submit(self.store, self.turn, definition)
        self.assertEqual(first['id'], same['id'])
        definition['name'] = 'replacement'
        with self.assertRaises(ValueError):workflows.submit(self.store, self.turn, definition)

    def test_completed_checkpoint_survives_stop_and_blocks_downstream(self):
        self.submit([local_step('first'), local_step('second', ['first'])])
        self.tick()
        workflows.cancel(self.store, self.turn)
        self.tick()
        self.assertEqual(self.get()['steps'][0]['status'], 'succeeded')
        self.assertTrue(self.get()['steps'][0]['result'])
        self.assertEqual(self.get()['steps'][1]['status'], 'cancelled')
        self.assertNotIn(('local', 'second'), self.adapter.calls)
        self.assertEqual(agent_store.turn(self.store, self.turn)['status'], 'cancelled')

    def test_stop_cancels_known_external_job_and_never_submits_dependents(self):
        self.submit()
        from webapp import provider_calls
        workflow=self.get();step=workflow['steps'][0]
        provider_calls.intent(self.store,workflow,step,'http://127.0.0.1:9998',step['spec']['operation'])
        self.tick()
        provider_calls.update(self.store,workflow,step,'waiting','job-generate')
        workflows.cancel(self.store, self.turn)
        self.tick()
        self.assertTrue(self.store.query("SELECT id FROM execution_external_cleanup WHERE external_id='job-generate'"))
        self.assertNotIn(('local', 'package'), self.adapter.calls)

    def test_safe_local_recovery_replays_only_interrupted_step(self):
        self.submit([local_step('first'), local_step('second', ['first'])])
        self.tick()
        workflows.change(self.store, self.get()['id'], 'second', status='running', attempts=1)
        self.runner.recover()
        self.tick()
        self.assertEqual(self.adapter.calls.count(('local', 'first')), 1)
        self.assertEqual(self.get()['steps'][1]['attempts'], 2)

    def test_unsafe_local_recovery_pauses_and_bounded_retries_stop(self):
        self.submit([local_step(safe=False)])
        workflows.change(self.store, self.get()['id'], 'package', status='running', attempts=1)
        self.runner.recover()
        self.assertEqual(self.get()['status'], 'paused')
        self.assertFalse(workflows.public(self.get())['resumable'])

    def test_local_retry_limit(self):
        self.submit([local_step()])
        self.adapter.local_error = True
        for _ in range(4):self.tick()
        self.assertEqual(self.adapter.calls.count(('local', 'package')), 3)
        self.assertFalse(workflows.public(self.get())['resumable'])

    def test_poll_failures_resume_query_without_paid_resubmission(self):
        self.submit()
        self.tick()
        self.adapter.poll_error = True
        for _ in range(5):self.tick()
        self.assertEqual(self.get()['status'], 'paused')
        self.assertTrue(workflows.public(self.get())['resumable'])
        workflows.resume(self.store, self.turn)
        self.adapter.poll_error = False
        self.adapter.record = {'status':'succeeded'}
        self.tick()
        self.assertEqual(self.adapter.calls.count(('submit', 'generate')), 1)

    def test_provider_failure_blocks_downstream_without_retries(self):
        self.submit()
        self.tick()
        self.adapter.record = {'status':'failed'}
        self.tick()
        self.assertEqual(self.get()['status'], 'paused')
        self.assertNotIn(('local', 'package'), self.adapter.calls)
        self.assertFalse(workflows.public(self.get())['resumable'])

    def test_dag_validation_and_confirmed_plan_required(self):
        with self.assertRaises(ValueError):self.submit([local_step('first', ['later']), local_step('later')])
        self.store.execute('UPDATE agent_turns SET plan_id=NULL WHERE id=?', (self.turn,))
        with self.assertRaises(ValueError):self.submit()

    def test_paused_turn_blocks_new_work_and_foreign_users(self):
        self.submit()
        self.adapter.submit_error = True
        self.tick()
        app = create_app(Path(self.tmp.name), run_worker=False)
        with TestClient(app) as owner, TestClient(app) as other:
            owner.get('/')
            token = owner.cookies.get('video_use_session')
            from webapp.auth import Sessions
            identity = Sessions(self.store).resolve(token)[0]
            self.store.execute('UPDATE projects SET owner=? WHERE id=?', (identity, self.project))
            (self.store.root/'agent-health.json').write_text(json.dumps({'ready':True,'updated':time.time()}))
            prefix = f'/api/projects/{self.project}/agent/{self.turn}'
            self.assertEqual(other.post(prefix+'/resume').status_code, 404)
            self.assertEqual(other.post(prefix+'/cancel').status_code, 404)
            self.assertEqual(owner.post(f'/api/projects/{self.project}/agent/messages', json={'message':'new'}).status_code, 409)
            self.assertEqual(owner.delete(f'/api/projects/{self.project}').status_code, 409)
            self.assertEqual(owner.post(prefix+'/resume').status_code, 409)
            self.assertEqual(owner.post(prefix+'/cancel').status_code, 200)
            self.tick()
            self.assertEqual(owner.delete(f'/api/projects/{self.project}').status_code, 200)
            self.assertFalse(self.store.query('SELECT id FROM agent_workflows'))
            self.assertFalse(self.store.query('SELECT id FROM agent_steps'))


class RuntimeIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid()==0, 'AgentHost needs its launcher directory')
    def test_host_yields_to_workflow_then_resumes_same_conversation(self):
        from webapp.agent_host import AgentHost
        settings=patch.dict(os.environ,{'VIDEO_USE_MODEL_REASONING_EFFORT':'high'})
        settings.start();self.addCleanup(settings.stop)
        fixture=WorkflowTests();fixture.setUp()
        try:
            host=AgentHost(fixture.store.root)
            host.workflow_runner.adapter=fixture.adapter
            requests=[];submissions=[]
            class Output:
                def __init__(self):self.queue=queue.Queue()
                def __iter__(self):
                    while (line:=self.queue.get()) is not None:yield line
                def close(self):pass
            class Process:
                def __init__(self):
                    self.stdout=Output();self.stderr=io.StringIO('');self.stdin=self;self.returncode=None
                def write(self, line):
                    message=json.loads(line);requests.append(message)
                    if 'id' not in message:return
                    result={}
                    if message['method'].startswith('thread/'):
                        result={'thread':{'id':'same-thread'}}
                    self.stdout.queue.put(json.dumps({'id':message['id'],'result':result}))
                    if message['method']=='turn/start':
                        if not submissions:
                            submissions.append(workflows.submit(fixture.store,fixture.turn,
                                {'key':'host-test','name':'Local steps','steps':[local_step()]}))
                        else:
                            from webapp import workflow_review
                            w=workflows.get(fixture.store,fixture.turn)
                            request=w['agent_review']
                            workflow_review.stage(fixture.store,fixture.turn,{'request_id':request['id'],'action':'pass','reason':'Checked actual result','deliveries':w['steps'][0]['result'],'evidence':[{'step_id':'package','files':w['steps'][0]['result'],'observation':'Result meets goal'}]})
                        self.stdout.queue.put(json.dumps({'method':'item/completed','params':{'item':{'id':'answer','type':'agentMessage','text':'后台已接收任务' if len([r for r in requests if r.get('method')=='turn/start'])==1 else '结果已检查'}}}))
                        self.stdout.queue.put(json.dumps({'method':'turn/completed','params':{'turn':{'status':'completed'}}}))
                        self.returncode=0;self.stdout.queue.put(None)
                def flush(self):pass
                def close(self):pass
                def poll(self):return self.returncode
                def wait(self,timeout=None):return self.returncode
            from webapp.execution_runtime import TaskRuntime
            from webapp.execution_resources import IdentityPool
            pool=IdentityPool(Path(fixture.tmp.name)/'host-identities',start=835000,size=2)
            def run_host():
                lease=host.coordinator.claim(fixture.turn,'agent');self.assertIsNotNone(lease)
                runtime=None
                try:
                    with lease.bind():
                        # Native RPC remains a fixture, but yielding/resuming must
                        # cross a real committed checkpoint and a new attempt.
                        runtime=TaskRuntime(lease,pool)
                        with patch('webapp.agent_host.Gateway'),patch('webapp.agent_host.subprocess.Popen',side_effect=lambda *a,**kw:Process()):
                            host.run_turn(fixture.turn)
                finally:
                    if runtime:runtime.close()
                    lease.release()
            run_host()
            self.assertEqual(agent_store.turn(fixture.store,fixture.turn)['status'],'waiting')
            checkpoint=fixture.store.query('SELECT path FROM execution_checkpoints WHERE project=?',(fixture.project,))[0]
            from webapp.checkpoints import native_descriptor
            self.assertEqual(native_descriptor(fixture.store,fixture.project,checkpoint['path'])['native']['native_session_id'],'same-thread')
            fixture.tick();fixture.tick()
            fixture.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(fixture.turn,))
            run_host()
            self.assertEqual(agent_store.turn(fixture.store,fixture.turn)['status'],'succeeded')
            self.assertEqual(workflows.get(fixture.store,fixture.turn)['status'],'succeeded')
            self.assertEqual([r['method'] for r in requests if r.get('method','').startswith('thread/')],['thread/start','thread/resume'])
            self.assertEqual([r['params']['effort'] for r in requests if r.get('method')=='turn/start'],['high','high'])
            stage=[r for r in requests if r.get('method')=='turn/start'][-1]['params']['input'][0]['text']
            self.assertIn('自动续跑',stage)
            self.assertEqual(fixture.adapter.calls.count(('local','package')),1)
        finally:fixture.tearDown()

    @unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'), 'Requires namespace launcher')
    def test_mock_http_provider_then_real_sandbox_script_and_partial_download(self):
        fixture = WorkflowTests()
        fixture.setUp()
        server = None
        try:
            store, tid = fixture.store, fixture.turn
            project = store.project_dir(fixture.project)
            Image.new('RGB', (256,256), '#2277aa').save(project/'sources/source.png')
            store.execute('UPDATE assets SET metadata=? WHERE id=?',(json.dumps({'kind':'image','width':256,'height':256}),'asset'))
            buf = io.BytesIO();Image.new('RGB', (8,8), '#ee9933').save(buf, format='PNG')
            image_bytes = buf.getvalue();posts=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):pass
                def do_POST(self):
                    self.rfile.read(int(self.headers.get('Content-Length',0)))
                    posts.append(self.path)
                    body=json.dumps({'id':'mock-job'}).encode()
                    self.send_response(202);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                def do_GET(self):
                    body=(image_bytes if self.path.endswith('/artifacts/image') else json.dumps(
                        {'status':'succeeded','artifacts':[{'id':'image','name':'image.png','kind':'image','media_type':'image/png'}]}).encode())
                    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            sockets=store.root/'sockets';sockets.mkdir()
            adapter=Adapter(store,'test-model',sockets)
            from webapp.agent_sandbox import prepare
            env=prepare(store,fixture.project,'test-model','execute',tid)
            script=env['workspace']/'edit/package.py'
            script.write_text("import json,os\nfrom pathlib import Path\nfrom PIL import Image\np=json.loads(os.environ['VIDEO_USE_STEP_INPUTS'])['generate'][0]\nwith Image.open(p) as im: im.resize((16,16)).save(Path(os.environ['VIDEO_USE_STEP_OUTPUT'])/'final.png')\n")
            script.chmod(0o644)
            fixture.submit()
            runner=workflows.Runner(store,adapter,threading.Event())
            with patch.dict('webapp.workflow_runtime.ENDPOINTS', {'edit':(f'http://127.0.0.1:{server.server_port}','target_image')}):
                runner.tick(tid)
                runner=workflows.Runner(store,adapter,threading.Event());runner.recover()
                store.execute('UPDATE agent_steps SET next_poll=0')
                runner.tick(tid)
                runner.tick(tid)
                runner.tick(tid)
            w=workflows.get(store,tid)
            self.assertEqual(w['status'],'resuming',w)
            self.assertEqual(posts,['/api/jobs'])
            final=env['workspace']/w['steps'][1]['result'][0]
            with Image.open(final) as image:self.assertEqual(image.size,(16,16))
            # Stopping during final review keeps both completed checkpoints downloadable.
            workflows.cancel(store,tid);runner.tick(tid)
            self.assertEqual(agent_store.turn(store,tid)['status'],'cancelled')
            app=create_app(store.root,run_worker=False)
            with TestClient(app) as browser:
                browser.get('/')
                from webapp.auth import Sessions
                owner=Sessions(store).resolve(browser.cookies.get('video_use_session'))[0]
                store.execute('UPDATE projects SET owner=? WHERE id=?',(owner,fixture.project))
                artifacts=agent_store.turn(store,tid)['artifacts']
                self.assertGreaterEqual(len(artifacts),2)
                self.assertEqual(browser.get('/'+artifacts[-1]['url']).status_code,200)
        finally:
            if server:server.shutdown();server.server_close()
            fixture.tearDown()


if __name__=='__main__':unittest.main()
