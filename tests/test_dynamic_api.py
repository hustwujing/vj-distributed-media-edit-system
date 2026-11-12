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

    def test_detail_never_mixes_waiting_state_with_newly_applied_decision(self):
        from webapp import workflow_api,workflow_graph as graph
        from webapp.store import Store
        response=self.a.post(self.prefix,json=self.body());identifier=response.json()['id'];store=self.app.state.store
        controller=Orchestrator(store);run=graph.get(store,identifier)
        controller.decision(run,'input_question',{'question':'Which interval?'})
        store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='input_question' WHERE id=?",(identifier,))
        writer=Store(store.root,dsn=store.dsn,initialize_schema=False);original=workflow_api.public
        def interleaved(store,run):
            with writer.connect() as db:
                db.execute("UPDATE workflow_decisions SET status='applied' WHERE run_id=?",(identifier,))
                db.execute("UPDATE runs SET status='ACTIVE',terminal_reason='' WHERE id=?",(identifier,))
            return original(store,run)
        with patch.object(workflow_api,'public',side_effect=interleaved):
            frozen=self.a.get(self.prefix+'/'+identifier).json()
        self.assertEqual(frozen['state'],'WAITING_USER');self.assertEqual(len(frozen['decisions']),1)
        current=self.a.get(self.prefix+'/'+identifier).json()
        self.assertEqual(current['state'],'ACTIVE');self.assertEqual(current['decisions'],[])

    def test_pending_plan_change_exposes_its_real_reason(self):
        from webapp import workflow_graph as graph
        identifier=self.a.post(self.prefix,json=self.body()).json()['id'];store=self.app.state.store
        reason='代码编辑策略发生变化，需要确认当前修复任务。'
        Orchestrator(store).decision(graph.get(store,identifier),'plan_change',{'reason':reason})
        current=self.a.get(self.prefix+'/'+identifier).json()
        self.assertEqual(current['state'],'WAITING_USER')
        self.assertEqual(current['blocking_action']['title'],'请确认执行范围')
        self.assertEqual(current['blocking_action']['message'],reason)
        self.assertNotIn('未登记',current['blocking_action']['message'])

    def test_final_review_pause_exposes_actual_feedback_without_task_errors(self):
        from webapp import workflow_graph as graph
        identifier=self.a.post(self.prefix,json=self.body()).json()['id'];store=self.app.state.store
        Orchestrator(store).queue_review(graph.get(store,identifier))
        feedback='无法确认黑色描边和2.0秒边界，请提供放大后的边界帧证据。'
        store.execute("UPDATE workflow_reviews SET status='rejected',decision_json=? WHERE run_id=? AND purpose='final'",
                      (graph.encode({'verdict':'inconclusive','feedback':feedback}),identifier))
        store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='review_incomplete' WHERE id=?",(identifier,))
        current=self.a.get(self.prefix+'/'+identifier).json()
        self.assertEqual(current['failure_details'],[])
        action=current['blocking_action'];self.assertIn(feedback,action['message'])
        self.assertEqual(action['details'][0]['message'],feedback);self.assertEqual(action['action'],'replan')
        self.assertNotIn('查看下方',action['message'])
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
        self.assertEqual(snapshot['control_jobs'][0]['execution_location'],'unallocated')
        self.assertEqual(snapshot['control_jobs'][0]['tool_location'],'unallocated')
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
        self.assertEqual([{k:a[k] for k in ('id','status','generation')} for a in snapshot['activity_attempts']],
                         [{'id':'expired-attempt','status':'expired','generation':0}])

    def test_execution_locations_follow_actual_attempts_and_not_declared_bindings(self):
        import json
        run=self.a.post(self.prefix,json=self.body()).json()
        store=self.app.state.store
        controller=Orchestrator(store)
        for claim in controller.claim_due():controller.reconcile(*claim)
        def snapshot():return self.a.get(self.prefix+'/'+run['id']).json()
        control=snapshot()['control_jobs'][0]['id']
        store.execute('''INSERT INTO run_tasks
            (run_id,id,type,spec_json,status,created_graph_version,created_at,updated_at,bindings_json)
            VALUES (?,?,?,?,?,0,?,?,?)''',(run['id'],'mix','operation',
            json.dumps({'operation':'media.edit','name':'Mix music'}),'ready',store.now(),store.now(),
            json.dumps({'_backend':'local_bwrap'})))
        self.assertEqual(snapshot()['tasks'][0]['execution_location'],'unallocated')
        self.assertIs(snapshot()['tasks'][0]['selected'],True)
        store.execute("UPDATE run_tasks SET selected=0 WHERE run_id=? AND id='mix'",(run['id'],))
        self.assertIs(snapshot()['tasks'][0]['selected'],False)
        store.execute("UPDATE run_tasks SET selected=1 WHERE run_id=? AND id='mix'",(run['id'],))
        def attempt(identifier,step,generation,revision=0,runtime='',uid=None):
            store.execute('''INSERT INTO execution_attempts
                (id,run_id,worker,generation,kind,started,status,step_id,task_revision,runtime,uid)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (identifier,run['id'],'worker',generation,'workflow',store.now(),'running',step,revision,runtime,uid))
        attempt('remote-mix','mix',1)
        store.execute('''INSERT INTO execution_external_cleanup
            (id,task_id,provider,external_id,state,phase,error) VALUES (?,?,?,?,?,?,?)''',
            ('opensandbox:remote-mix',run['id'],'opensandbox:remote-mix','','dormant','delete',''))
        self.assertEqual(snapshot()['tasks'][0]['execution_location'],'opensandbox_unconfirmed')
        store.execute("UPDATE execution_external_cleanup SET external_id='sandbox-id',state='complete',phase='deleted' WHERE id=?",
                      ('opensandbox:remote-mix',))
        task=snapshot()['tasks'][0]
        self.assertEqual((task['execution_location'],task['execution_attempt_id']),('opensandbox','remote-mix'))
        self.assertNotIn('sandbox-id',json.dumps(task))
        # Experts run on the worker. A local tool environment is only claimed
        # once TaskRuntime has persisted its allocation, never from the type.
        attempt('expert',control,2)
        job=snapshot()['control_jobs'][0]
        self.assertEqual((job['execution_location'],job['tool_location']),('agent_worker','unrecorded'))
        store.execute('UPDATE execution_attempts SET runtime=?,uid=? WHERE id=?',('/private/node/runs/expert',720100,'expert'))
        job=snapshot()['control_jobs'][0]
        self.assertEqual((job['execution_location'],job['tool_location']),('agent_worker','bwrap_sandbox'))
        self.assertNotIn('/private/node',json.dumps(job))
        # The next attempt must not borrow the first attempt's sandbox.
        attempt('retry-mix','mix',3)
        self.assertEqual(snapshot()['tasks'][0]['execution_location'],'unrecorded')
        store.execute('UPDATE execution_attempts SET runtime=?,uid=? WHERE id=?',('/private/node/runs/retry',720101,'retry-mix'))
        self.assertEqual(snapshot()['tasks'][0]['execution_location'],'bwrap_sandbox')
        store.execute("UPDATE run_tasks SET revision=1,attempts=0 WHERE run_id=? AND id='mix'",(run['id'],))
        self.assertEqual(snapshot()['tasks'][0]['execution_location'],'unallocated')
        store.execute("UPDATE run_tasks SET attempts=1 WHERE run_id=? AND id='mix'",(run['id'],))
        self.assertEqual(snapshot()['tasks'][0]['execution_location'],'unrecorded')
        attempt('revision-one','mix',4,revision=1)
        store.execute('''INSERT INTO workflow_provider_calls
            (request_key,workflow,step,revision,attempt,provider,operation,state,inputs,created,updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',('old-provider',run['id'],'mix',0,1,'provider','image.edit_region','succeeded','{}',store.now(),store.now()))
        self.assertNotIn('provider_execution_state',snapshot()['tasks'][0],'Old revision provider receipt is not current execution')
        store.execute("UPDATE workflow_provider_calls SET revision=1,state='unknown' WHERE request_key='old-provider'")
        self.assertEqual(snapshot()['tasks'][0]['provider_execution_state'],'unknown')
        # Even a matching receipt ID must belong to this Run.
        store.execute('''INSERT INTO execution_external_cleanup
            (id,task_id,provider,external_id,state,phase,error) VALUES (?,?,?,?,?,?,?)''',
            ('opensandbox:revision-one','foreign-run','opensandbox:revision-one','foreign-sandbox','complete','deleted',''))
        self.assertEqual(snapshot()['tasks'][0]['execution_location'],'unrecorded')

    def test_public_execution_clocks_and_unicode_transcript_owner_survive_reentry(self):
        from webapp.public_messages import save
        run=self.a.post(self.prefix,json=self.body()).json();store=self.app.state.store
        store.execute('''INSERT INTO execution_attempts
            (id,run_id,worker,generation,kind,started,finished,status,step_id,task_revision,runtime,uid)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('clock',run['id'],'private-worker',1,'workflow',100.,125.,'finished','step',0,'/private/runtime',720001))
        texts=['代码编辑师：🙂第一段','代码编辑师：第二段']
        with store.connect() as db:
            save(db,run['id'],1,'clock',{str(i):{'text':text,'phase':'commentary','status':'completed'} for i,text in enumerate(texts)},1,125.)
        for _ in range(2):
            value=self.a.get(self.prefix+'/'+run['id']).json()
            attempt=value['activity_attempts'][0]
            self.assertEqual((attempt['started'],attempt['finished'],attempt['step_id'],attempt['task_revision']),(100.,125.,'step',0))
            self.assertNotIn('runtime',attempt);self.assertNotIn('worker',attempt)
            spans=value['agent_text_spans'];length=len(texts[0].encode('utf-16-le'))//2
            self.assertEqual(spans[0],{'start':0,'end':length,'attempt_id':'clock'})
            self.assertEqual(spans[1]['start'],length+2)
        self.assertEqual(self.b.get(self.prefix+'/'+run['id']).status_code,404)

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

    def test_chat_starts_with_confirmation_required_and_prior_delivery_survives_readonly_rounds(self):
        import json
        first=self.a.post(self.prefix,json=self.body()).json();store=self.app.state.store
        self.assertTrue(first['authorization']['require_plan_confirmation'])
        delivery=[{'file':'final.wav','name':'final.wav','type':'audio/wav','size':12}]
        store.execute("UPDATE runs SET status='SUCCEEDED',delivery_json=?,delivered_at=? WHERE id=?",(json.dumps(delivery),store.now(),first['id']))
        second=self.a.post(self.prefix,json=self.body()).json()
        store.execute("UPDATE runs SET parent_run_id=?,status='SUCCEEDED',terminal_reason='conversation_answer' WHERE id=?",(first['id'],second['id']))
        third=self.a.post(self.prefix,json=self.body()).json()
        store.execute('UPDATE runs SET parent_run_id=? WHERE id=?',(second['id'],third['id']))
        result=self.a.get(self.prefix+'/'+third['id']).json()
        self.assertEqual(result['parent_result']['id'],first['id'])
        self.assertIn(first['id'],result['parent_result']['delivery'][0]['url'])
        self.assertEqual(self.b.get(self.prefix+'/'+third['id']).status_code,404)

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
        self.app.state.store.execute("UPDATE runs SET status='FAILED',terminal_reason='planning_contract_exhausted' WHERE id=?",(run['id'],))
        terminal=self.a.get(self.prefix+'/'+run['id']).json()
        self.assertNotIn('blocking_action',terminal)
        self.assertIn('不是缺少用户确认或素材',terminal['terminal_notice'])

    def test_planner_compiler_errors_are_visible_without_downloading_diagnostics(self):
        import json
        run=self.a.post(self.prefix,json=self.body()).json();store=self.app.state.store
        controller=Orchestrator(store)
        for claim in controller.claim_due():controller.reconcile(*claim)
        job=store.query('SELECT id FROM control_jobs WHERE run_id=?',(run['id'],))[0]['id']
        store.execute("UPDATE control_jobs SET status='paused',error_code='planning_contract_exhausted',error_detail='规划草稿重复失败' WHERE id=?",(job,))
        store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='planning_contract_exhausted' WHERE id=?",(run['id'],))
        record={'code':'missing_wait_work','classification':'parameters','validation_errors':[{'path':'plan.review_again','message':'没有可用的证据复审对象'}]}
        store.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',('compile-error',run['id'],job,0,1,json.dumps(record),store.now()))
        snapshot=self.a.get(self.prefix+'/'+run['id']).json()
        self.assertIn('没有可用的证据复审对象',snapshot['blocking_action']['details'][0]['message'])
        self.assertEqual(snapshot['failure_details'][0]['name'],'任务规划师')
        self.assertEqual(snapshot['control_jobs'][0]['error_detail'],'规划草稿重复失败')

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
        for table in ('runs','run_tasks','planner_runs','workflow_commands','workflow_graph_events','workflow_start_requests',
                      'workflow_read_cache','session_messages','conversation_summaries','conversation_summary_jobs'):
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
