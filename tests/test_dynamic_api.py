import tempfile
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from webapp.app import create_app
from webapp.workflow_orchestrator import Orchestrator


class DynamicAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=create_app(Path(self.temp.name),False)
        self.a=TestClient(self.app);self.b=TestClient(self.app)
        self.a.__enter__();self.b.__enter__()
        self.addCleanup(self.a.__exit__,None,None,None);self.addCleanup(self.b.__exit__,None,None,None)
        self.project=self.a.post('/api/projects',json={'name':'dynamic API'}).json()['id']
        self.prefix='/api/projects/'+self.project+'/runs'
    def body(self):
        import uuid
        capabilities=self.a.get(self.prefix+'/capabilities').json()
        return {'request_id':uuid.uuid4().hex,'confirmed':True,'authorization':{'goal':'Produce a report',
            'allowed_assets':[],'allowed_operations':['script'],'delivery_targets':['report'],
            'content_fingerprint':capabilities['content_fingerprint']}}
    def test_owned_explicit_confirmation_and_idempotent_start(self):
        body=self.body()
        self.assertEqual(self.b.post(self.prefix,json=body).status_code,404)
        self.assertEqual(self.a.post(self.prefix,json={**body,'confirmed':False}).status_code,422)
        first=self.a.post(self.prefix,json=body)
        self.assertEqual(first.status_code,202,first.text)
        controller=Orchestrator(self.app.state.store)
        for claim in controller.claim_due():controller.reconcile(*claim)
        snapshot=self.a.get(self.prefix+'/'+first.json()['id']).json()
        self.assertEqual(snapshot['control_jobs'][0]['created_graph_version'],0)
        self.assertEqual(snapshot['control_jobs'][0]['execution_location'],'agent_worker')
        self.assertEqual(snapshot['control_jobs'][0]['tool_location'],'bwrap_sandbox')
        self.assertEqual(snapshot['control_jobs'][0]['display_name'],'任务规划师')
        self.assertEqual(snapshot['planners'][0]['task_id'],snapshot['control_jobs'][0]['id'])
        self.assertEqual(snapshot['planners'][0]['trigger_task_ids'],[])
        self.assertEqual(snapshot['planners'][0]['trigger_control_job_ids'],[])
        second=self.a.post(self.prefix,json=body)
        self.assertEqual(second.json()['id'],first.json()['id'])
        self.assertEqual(self.a.get(self.prefix).json()['runs'][0]['graph_version'],0)
        self.assertEqual(self.b.get(self.prefix+'/'+first.json()['id']).status_code,404)
        self.assertEqual(self.b.get(self.prefix+'/'+first.json()['id']+'/events').status_code,404)
        self.assertEqual(len(self.app.state.store.query('SELECT id FROM agent_turns')),0)

    def test_run_snapshot_exposes_attempt_status_for_activity_recovery(self):
        run=self.a.post(self.prefix,json=self.body()).json()
        self.app.state.store.execute(
            "INSERT INTO execution_attempts(id,run_id,worker,generation,kind,started,finished,status) VALUES (?,?,?,?,?,?,?,?)",
            ('expired-attempt',run['id'],'worker-a',0,'agent',self.app.state.store.now()-10,
             self.app.state.store.now(),'expired'))
        snapshot=self.a.get(self.prefix+'/'+run['id']).json()
        self.assertEqual(snapshot['activity_attempts'],[{'id':'expired-attempt','status':'expired','generation':0}])

    def test_daily_quota_display_and_idempotent_create_replay(self):
        with patch.dict(os.environ,{'VIDEO_USE_USER_MODEL_DAILY':'37'}):
            quota=self.a.get(self.prefix+'/capabilities').json()['daily_quota']
            self.assertEqual((quota['used'],quota['limit'],quota['blocked']),(0,37,False))
            body=self.body();created=self.a.post(self.prefix,json=body)
            self.assertEqual(created.status_code,202,created.text)
            store=self.app.state.store
            owner=store.query('SELECT owner FROM projects WHERE id=?',(self.project,))[0]['owner']
            store.execute('INSERT INTO execution_daily_usage VALUES (?,?,?,?)',(owner,int(store.now()//86400),'model',37))
            quota=self.a.get(self.prefix+'/capabilities').json()['daily_quota']
            self.assertEqual((quota['used'],quota['limit'],quota['blocked']),(37,37,True))
            replay=self.a.post(self.prefix,json=body)
            self.assertEqual(replay.status_code,202,replay.text)
            self.assertEqual(replay.json()['id'],created.json()['id'])
            denied=self.a.post(self.prefix,json=self.body())
            self.assertEqual(denied.status_code,409,denied.text)
            self.assertEqual(len(store.query('SELECT id FROM runs WHERE project_id=?',(self.project,))),1)

    def test_terminal_snapshot_exposes_user_readable_reason(self):
        run=self.a.post(self.prefix,json=self.body()).json()
        self.app.state.store.execute("UPDATE runs SET status='FAILED',terminal_reason='planning_budget_exhausted' WHERE id=?",(run['id'],))
        snapshot=self.a.get(self.prefix+'/'+run['id']).json()
        self.assertEqual(snapshot['terminal_notice'],'规划轮次已耗尽，任务未继续执行。')

    def test_system_contract_failures_are_not_presented_as_missing_user_information(self):
        run=self.a.post(self.prefix,json=self.body()).json()
        for code,title in [('planning_contract_exhausted','规划未完成'),('review_contract_exhausted','审查未完成'),
                           ('invalid_delivery_manifest','交付校验失败')]:
            with self.subTest(code=code):
                self.app.state.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason=? WHERE id=?",(code,run['id']))
                action=self.a.get(self.prefix+'/'+run['id']).json()['blocking_action']
                self.assertEqual(action['title'],title)
                self.assertIn('诊断',action['message'])
                self.assertNotIn('请确认下面列出的事项',action['message'])
                self.assertIsNone(action['decision_id'])

    def test_budget_exhausted_waiting_run_rejects_new_input_with_recovery_guidance(self):
        import uuid
        run=self.a.post(self.prefix,json=self.body()).json()
        self.app.state.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='planning_budget_exhausted' WHERE id=?",(run['id'],))
        snapshot=self.a.get(self.prefix+'/'+run['id']).json()
        self.assertEqual(snapshot['blocking_action']['title'],'本轮规划轮次已耗尽')
        self.assertIn('新建一轮',snapshot['blocking_action']['message'])
        response=self.a.post(self.prefix+'/'+run['id']+'/inputs',json={'request_id':uuid.uuid4().hex,'content':'继续修改','attachments':[],'targets':[]})
        self.assertEqual(response.status_code,409,response.text)
        self.assertIn('新建一轮',response.json()['detail'])

    def test_run_snapshot_reports_control_plane_liveness(self):
        run=self.a.post(self.prefix,json=self.body()).json()
        unavailable=self.a.get(self.prefix+'/'+run['id']).json()['control_plane']
        self.assertFalse(unavailable['available'])
        self.app.state.store.execute('INSERT INTO workflow_orchestrators(id,release,heartbeat,draining) VALUES (?,?,?,0)',
                                     ('orchestrator-a','test',self.app.state.store.now()))
        available=self.a.get(self.prefix+'/'+run['id']).json()['control_plane']
        self.assertTrue(available['available'])
        self.assertLess(available['stale_seconds'],15)
    def test_stale_content_creates_no_task(self):
        body=self.body();body['authorization']['content_fingerprint']='0'*64
        response=self.a.post(self.prefix,json=body)
        self.assertEqual(response.status_code,409)
        self.assertFalse(self.app.state.store.query('SELECT id FROM agent_turns'))

    def test_project_allows_independent_runs_but_enforces_owner_execution_quota(self):
        with patch.dict(os.environ,{'VIDEO_USE_USER_QUEUE_LIMIT':'2'}):
            first=self.a.post(self.prefix,json=self.body())
            second=self.a.post(self.prefix,json=self.body())
            third=self.a.post(self.prefix,json=self.body())
        self.assertEqual(first.status_code,202,first.text)
        self.assertEqual(second.status_code,202,second.text)
        self.assertNotEqual(first.json()['session_id'],second.json()['session_id'])
        self.assertEqual(third.status_code,409,third.text)
        self.assertIn('待处理任务已达上限',third.json()['detail'])

    def test_waiting_user_runs_use_open_run_limit_instead_of_execution_quota(self):
        first=self.a.post(self.prefix,json=self.body()).json()
        second=self.a.post(self.prefix,json=self.body()).json()
        self.app.state.store.execute("UPDATE runs SET status='WAITING_USER' WHERE id IN (?,?)",(first['id'],second['id']))
        with patch.dict(os.environ,{'VIDEO_USE_USER_QUEUE_LIMIT':'1','VIDEO_USE_USER_OPEN_RUN_LIMIT':'2'}):
            response=self.a.post(self.prefix,json=self.body())
        self.assertEqual(response.status_code,409,response.text)
        self.assertIn('待确认任务过多',response.json()['detail'])
        # The Run-retention ceiling does not block unrelated manual/image/legacy
        # queue classes that use the shared execution admission function.
        with patch.dict(os.environ,{'VIDEO_USE_USER_QUEUE_LIMIT':'1','VIDEO_USE_USER_OPEN_RUN_LIMIT':'2'}):
            self.app.state.store.admit_tasks(self.project)
    def test_cancel_command_then_project_delete_removes_dynamic_records(self):
        import uuid
        run=self.a.post(self.prefix,json=self.body()).json()
        request={'request_id':uuid.uuid4().hex,'action':'cancel'}
        self.assertEqual(self.b.post(self.prefix+'/'+run['id']+'/commands',json=request).status_code,404)
        self.assertEqual(self.a.post(self.prefix+'/'+run['id']+'/commands',json=request).status_code,202)
        self.assertEqual(self.app.state.store.query('SELECT status FROM runs')[0]['status'],'ACTIVE')
        controller=Orchestrator(self.app.state.store)
        for claim in controller.claim_due():controller.reconcile(*claim)
        self.assertEqual(self.a.get(self.prefix+'/'+run['id']).json()['state'],'CANCELLED')
        result=self.a.delete('/api/projects/'+self.project)
        self.assertEqual(result.status_code,200,result.text)
        for table in ('runs','run_tasks','planner_runs','workflow_commands','workflow_graph_events','workflow_start_requests'):
            self.assertFalse(self.app.state.store.query('SELECT * FROM '+table),table)

    def test_project_delete_releases_local_artifact_references_for_gc(self):
        source=Path(self.temp.name)/'published.bin';source.write_bytes(b'published bytes')
        artifact=self.app.state.store.artifacts.upload(self.project,source)
        self.app.state.store.artifacts.publish(self.project,'edit/published.bin',artifact['id'])
        self.assertTrue(self.app.state.store.query('SELECT artifact FROM artifact_files WHERE project=?',(self.project,)))
        result=self.a.delete('/api/projects/'+self.project)
        self.assertEqual(result.status_code,200,result.text)
        self.assertFalse(self.app.state.store.query('SELECT artifact FROM artifact_files WHERE project=?',(self.project,)))
        row=self.app.state.store.query('SELECT state FROM artifact_objects WHERE id=?',(artifact['id'],))[0]
        self.assertEqual(row['state'],'staged')
        with self.assertRaisesRegex(ValueError,'deleted'):
            self.app.state.store.artifacts.publish(self.project,'edit/late.bin',artifact['id'])
    def test_chat_page_has_project_and_deployment_prefix(self):
        with TestClient(self.app,root_path='/distributed-agent-system') as client:
            project=client.post('/distributed-agent-system/api/projects',json={'name':'prefix'}).json()['id']
            response=client.get('/distributed-agent-system/chat/'+project)
            self.assertIn('<base href="/distributed-agent-system/">',response.text)
            self.assertIn('data-project="'+project+'"',response.text)
            self.assertIn('static/dynamic.js',response.text)

    def test_legacy_control_cannot_start_a_second_planner_for_dynamic_runs(self):
        run=self.a.post(self.prefix,json=self.body()).json()
        response=self.a.post('/api/projects/'+self.project+'/agent/'+run['id']+'/control',json={'action':'replan','message':'change the goal'})
        self.assertEqual(response.status_code,404)
        self.assertEqual(len(self.app.state.store.query('SELECT id FROM agent_turns')),0)
        self.assertFalse(self.app.state.store.query('SELECT id FROM workflow_commands'))
