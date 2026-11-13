"""State protocol tests; no model or paid provider calls."""
import json
from tests.run_input_fixture import accept_change
import unittest
from tests import test_workflows as fixtures
from webapp import workflow_graph as graph,workflow_planning as planning
from webapp.workflow_orchestrator import Orchestrator,ReconcileLost


def script(identifier='first',**overrides):
    return {'id':identifier,'type':'script','name':identifier,'objective':'Write the approved report',
            'argv':['python','-c','print("report")'],'outputs':['report.txt'],
            'output_roles':{'report':'report.txt'},'acceptance':[{'kind':'file'}],
            'basic_reason':'Local deterministic report','retry_policy':'safe_once','delivery_stage':True,**overrides}


def envelope(store,request_id,proposal=None,kind='APPLY_PLAN',**extra):
    request=store.query('SELECT * FROM planner_runs WHERE id=?',(request_id,))[0]
    frozen=json.loads(request['snapshot_json'])
    result={'kind':kind,'snapshot_digest':request['snapshot_digest'],
            'base_graph_version':proposal.get('base_graph_version',request['base_graph_version']) if proposal else request['base_graph_version'],
            'authorization_version':request['authorization_version'],
            'input_through_sequence':(frozen.get('input_batch') or {}).get('through_sequence',0),
            'reason':'Fixture control result',**extra}
    if proposal is not None:result['proposal']=proposal
    return result


class DynamicGraphTests(unittest.TestCase):
    def test_native_runtime_configuration_failure_is_visible_fatal_and_never_asks_a_model_to_repair_the_script(self):
        self.tick()
        self.propose({'proposal_id':'runtime-failure','reason':'Create report','base_graph_version':0,
            'add_tasks':[script(on_failure='diagnose')],'replan_after':['first']})
        run=self.tick();task=run['tasks'][0]
        before=len(run['control_jobs'])
        self.orchestrator.task_failure(run,task,{'error_code':'agent_runtime_unavailable','phase':'local',
            'error_detail':'执行器缺少 websockets 15','transient':False})
        current=graph.get(self.store,run['id'])
        self.assertEqual(current['tasks'][0]['status'],'failed')
        self.assertEqual(len(current['control_jobs']),before)
        from webapp.workflow_api import public
        value=public(self.store,current)
        self.assertFalse(value['decisions'])
        self.assertIn('不需要补充任务信息',value['failure_details'][0]['message'])
        self.assertIn('websockets',value['failure_details'][0]['message'])
        self.assertEqual(current['terminal_reason'],'agent_runtime_unavailable')

    def test_native_runtime_transport_loss_retries_twice_then_fails_visibly(self):
        authorization=graph.bind_authorization({**self.run['authorization'],
            'allowed_agent_profiles':['editing_implementer','media_failure_diagnostician']})
        encoded=graph.encode(authorization.model_dump())
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(encoded,self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(encoded,self.run['id']))
        self.tick()
        self.propose({'proposal_id':'runtime-interruption','reason':'Create report','base_graph_version':0,
            'add_tasks':[script(type='agent',profile='editing_implementer',argv=[],inputs={},on_failure='diagnose')],
            'replan_after':['first']})
        run=self.tick();task=run['tasks'][0]
        failure={'error_code':'agent_runtime_unavailable','phase':'local',
                 'error_detail':'Runtime WebSocket send failed','runtime_transient':True}
        for interruption in (1,2):
            self.orchestrator.task_failure(graph.get(self.store,run['id']),task,failure)
            current=graph.get(self.store,run['id']);task=current['tasks'][0]
            self.assertEqual((task['status'],task['infrastructure_interruptions']),('retry_wait',interruption))
            self.assertIn(f'自动恢复（{interruption}/2）',task['error_detail'])
            self.assertEqual(current['status'],'ACTIVE')
            self.store.execute("UPDATE run_tasks SET status='running' WHERE run_id=? AND id=?",(run['id'],task['id']))
            task=graph.get(self.store,run['id'])['tasks'][0]
        self.orchestrator.task_failure(graph.get(self.store,run['id']),task,failure)
        current=graph.get(self.store,run['id']);task=current['tasks'][0]
        self.assertEqual((task['status'],task['infrastructure_interruptions']),('failed',2))
        self.assertEqual(current['terminal_reason'],'agent_runtime_unavailable')
        self.assertNotIn('补充任务信息',task['error_detail'].replace('不需要补充任务信息',''))

    def test_provider_failure_shows_saved_actionable_reason_without_resubmission_prompt(self):
        self.tick()
        self.propose({'proposal_id':'provider-failure','reason':'Create report','base_graph_version':0,
            'add_tasks':[script(on_failure='pause')],'replan_after':['first']})
        run=self.tick();task=run['tasks'][0]
        self.orchestrator.task_failure(run,task,{'error_code':'provider_failed','phase':'poll',
            'error_detail':'上游返回的错误：GPU 显存不足，请降低质量后重试'})
        current=graph.get(self.store,run['id'])
        self.assertEqual(current['status'],'WAITING_USER')
        from webapp.workflow_api import public
        value=public(self.store,current)
        self.assertEqual(value['blocking_action']['title'],'上游处理失败')
        self.assertIn('GPU 显存不足',value['blocking_action']['message'])
        self.assertIn('未自动重复提交',value['blocking_action']['message'])

    def test_native_task_timeout_never_replays_same_revision_as_transient_transport(self):
        self.tick()
        self.propose({'proposal_id':'native-timeout','reason':'Create report','base_graph_version':0,
            'add_tasks':[script(on_failure='diagnose')],'replan_after':['first']})
        run=self.tick();task=run['tasks'][0]
        self.orchestrator.task_failure(run,task,{'error_code':'agent_timeout','phase':'local',
            'error_detail':'代码编辑师已达到运行时限','transient':False})
        current=graph.get(self.store,run['id']);failed=current['tasks'][0]
        self.assertEqual(failed['status'],'paused');self.assertEqual(failed['error_code'],'agent_timeout')
        self.assertNotEqual(failed['status'],'retry_wait')
        self.assertTrue(any(job['spec']['profile']=='media_failure_diagnostician' for job in current['control_jobs']))
        self.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='agent_timeout' WHERE id=?",(run['id'],))
        from webapp.workflow_api import public
        shown=public(self.store,graph.get(self.store,run['id']))
        self.assertEqual(shown['blocking_action']['title'],'代码编辑师运行超时')
        self.assertEqual(shown['blocking_action']['failure_kind'],'timeout')
        self.assertEqual(shown['blocking_action']['retry_policy'],'never_same_revision')
        self.assertIn('达到运行时限',shown['blocking_action']['message'])
        self.assertNotIn('补充信息',shown['blocking_action']['message'])

    def test_initialize_upgrades_legacy_dynamic_schema_without_losing_rows(self):
        with self.store.connect() as db:
            db.execute('DROP VIEW run_work_items')
            db.execute('ALTER TABLE runs DROP COLUMN reconcile_error_signature')
            db.execute('ALTER TABLE runs DROP COLUMN reconcile_error_count')
            db.execute('ALTER TABLE run_tasks DROP COLUMN infrastructure_interruptions')
            db.execute('ALTER TABLE control_jobs DROP COLUMN infrastructure_interruptions')
            db.execute('ALTER TABLE task_results DROP COLUMN error_json')
            db.execute('ALTER TABLE workflow_plan_drafts DROP COLUMN last_error_json')
            db.execute('ALTER TABLE workflow_dependencies RENAME TO workflow_dependencies_current')
            db.execute('''CREATE TABLE workflow_dependencies(
                run_id TEXT NOT NULL,upstream_task_id TEXT NOT NULL,downstream_task_id TEXT NOT NULL,
                created_graph_version INTEGER NOT NULL,retired_graph_version INTEGER,
                PRIMARY KEY(run_id,upstream_task_id,downstream_task_id))''')
            db.execute('INSERT INTO workflow_dependencies VALUES (?,?,?,?,NULL)',
                       (self.run['id'],'legacy_parent','legacy_child',1))
            db.execute('DROP TABLE workflow_dependencies_current')
            db.execute('CREATE VIEW run_work_items AS SELECT run_id,id FROM run_tasks')

        graph.initialize(self.store)
        expected={
            'runs':{'reconcile_error_signature','reconcile_error_count'},
            'run_tasks':{'infrastructure_interruptions'},
            'control_jobs':{'infrastructure_interruptions'},
            'task_results':{'error_json'},
            'workflow_plan_drafts':{'last_error_json'},
        }
        with self.store.connect() as db:
            for table,names in expected.items():
                self.assertTrue(names<={row['name'] for row in db.execute(f'PRAGMA table_info({table})')})
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM workflow_dependencies').fetchone()['n'],1)
            db.execute('INSERT INTO workflow_dependencies VALUES (?,?,?,?,NULL)',
                       (self.run['id'],'legacy_parent','legacy_child',2))
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM workflow_dependencies').fetchone()['n'],2)

    def test_default_run_limits_are_generous_but_remain_bounded(self):
        authorization=graph.Authorization(goal='bounded',allowed_assets=[],allowed_operations=[],
            delivery_targets=['result'],content_fingerprint='a'*64)
        self.assertEqual((authorization.model_token_budget,authorization.wall_time_budget,
            authorization.max_tasks,authorization.max_planning_rounds),(4000000,86400,256,64))
        with self.assertRaises(ValueError):
            graph.Authorization.model_validate({**authorization.model_dump(),'model_token_budget':4000001})

    def test_resolution_guidance_leads_to_input_replanning_not_resume(self):
        import uuid
        from webapp import run_inputs
        from webapp.workflow_api import public
        self.tick()
        self.propose({'proposal_id':'resolution-plan','reason':'Create report','base_graph_version':0,'add_tasks':[script()], 'replan_after':['first']})
        self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed' WHERE run_id=?",(self.run['id'],))
        self.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='task_requires_resolution' WHERE id=?",(self.run['id'],))
        notice=public(self.store,graph.get(self.store,self.run['id']))['blocking_action']
        self.assertIn('提交补充要求',notice['message'])
        self.assertIn('不会自动重新规划',notice['message'])
        command=graph.command(self.store,self.run['id'],'owner','resume',request_id=uuid.uuid4().hex)
        self.tick()
        self.assertEqual(self.store.query('SELECT error_code FROM workflow_commands WHERE id=?',(command['id'],))[0]['error_code'],'replan_required')
        request=accept_change(self.store,self.run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'请根据失败反馈修改报告'})
        self.tick()
        row=self.store.query('SELECT status FROM run_inputs WHERE id=?',(request['id'],))[0]
        self.assertEqual(row['status'],'PLANNING')
        pending=self.store.query("SELECT snapshot_json FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))
        self.assertEqual(len(pending),1)
        self.assertIn('请根据失败反馈修改报告',pending[0]['snapshot_json'])

    def setUp(self):
        self.f=fixtures.WorkflowTests();self.f.setUp();self.store=self.f.store
        self.authorization={'goal':'Make a report','allowed_assets':['asset'],'allowed_operations':['script'],
            'allowed_agent_profiles':['media_failure_diagnostician'],'delivery_targets':['report'],
            'content_fingerprint':graph.content_fingerprint(self.store,self.f.project)}
        self.run=graph.create(self.store,self.f.project,self.f.turn,self.authorization)
        self.orchestrator=Orchestrator(self.store,'test-orchestrator')
    def tearDown(self):self.f.tearDown()
    def tick(self):
        self.store.execute('UPDATE runs SET next_reconcile_at=0 WHERE id=?',(self.run['id'],))
        claims=self.orchestrator.claim_due()
        self.assertEqual(len(claims),1)
        self.orchestrator.reconcile(*claims[0])
        return graph.get(self.store,self.run['id'])
    def propose(self,raw):
        request=self.store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        from webapp.task_leases import Coordinator
        coordinator=Coordinator(self.store,'planner-test','test')
        lease=coordinator.claim(self.f.turn,'workflow',step_id=request['task_id'])
        self.assertIsNotNone(lease)
        try:
            with lease.bind():planning.stage(self.store,lease,request['id'],envelope(self.store,request['id'],raw))
        finally:lease.release()
        return request['id']

    def test_creation_is_idempotent_and_requires_content_identity(self):
        self.assertEqual(graph.create(self.store,self.f.project,self.f.turn,self.authorization)['id'],self.run['id'])
        with self.assertRaises(ValueError):graph.create(self.store,self.f.project,self.f.turn,{**self.authorization,'goal':'different'})
        with self.assertRaises(ValueError):graph.create(self.store,self.f.project,self.f.turn,{**self.authorization,'content_fingerprint':'0'*64})

    def test_terminal_run_provider_uncertainty_does_not_hold_global_capacity(self):
        from webapp.scheduling import provider_inflight
        from webapp import agent_store,workflows
        legacy_project=self.store.add_project('owner','legacy')['id']
        legacy_plan=agent_store.add_turn(self.store,legacy_project,'plan','legacy plan')
        agent_store.finish(self.store,legacy_plan,'succeeded','approved')
        legacy_turn=agent_store.add_turn(self.store,legacy_project,'execute','legacy',plan_id=legacy_plan)
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(legacy_turn,))
        legacy=workflows.submit(self.store,legacy_turn,{'key':'legacy','name':'legacy','steps':[fixtures.local_step('legacy')]})
        self.store.execute('''INSERT INTO workflow_provider_calls(
          request_key,workflow,step,revision,attempt,provider,operation,state,inputs,created,updated,implementation)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?), (?,?,?,?,?,?,?,?,?,?,?,?)''',
          ('stale-call',self.run['id'],'image',0,1,'https://provider.invalid','image.generate_from_text',
           'submission_unknown','{}',self.store.now(),self.store.now(),json.dumps({'concurrency_group':'images'}),
           'legacy-call',legacy['id'],'generate',0,1,'https://provider.invalid','image.generate_from_text',
           'waiting','{}',self.store.now(),self.store.now(),json.dumps({'concurrency_group':'images'})))
        with self.store.connect() as db:self.assertEqual(len(provider_inflight(db)),2)
        self.store.execute("UPDATE agent_turns SET status='failed' WHERE id=?",(legacy_turn,))
        with self.store.connect() as db:self.assertEqual(len(provider_inflight(db)),1)
        self.store.execute("UPDATE runs SET status='FAILED',terminal_reason='provider_failed' WHERE id=?",(self.run['id'],))
        with self.store.connect() as db:self.assertEqual(provider_inflight(db),[])
        self.assertEqual(self.store.query("SELECT state FROM workflow_provider_calls WHERE request_key='stale-call'")[0]['state'],
                         'submission_unknown')

    def test_planning_snapshot_preserves_operation_output_alternatives(self):
        frozen=planning.snapshot(self.store,self.run)
        edit=next(item for item in frozen['operations'] if item['id']=='media.edit')
        self.assertEqual(edit['output_contract']['alternatives'],['audio','video'])
        self.assertIn('requires_gpu',edit['execution_requirements'])
        gpu=[item for item in frozen['operations'] if item['execution_requirements']['requires_gpu']]
        self.assertTrue(gpu);self.assertTrue(all(item['execution_requirements']['execution_mode'] for item in gpu))

    def test_public_https_agent_task_requires_exact_authorization(self):
        import uuid
        url='https://example.com/data.json?version=1'
        raw={**self.authorization,'allowed_agent_profiles':['editing_implementer'],
            'allowed_network_urls':[]}
        task={'id':'networked','type':'agent','name':'Networked code','objective':'Read approved data',
            'profile':'editing_implementer','inputs':{},'output_roles':{'report':'result.json'},
            'acceptance':[{'kind':'json','required_keys':['value']}],
            'basic_reason':'Requires a generated local implementation','delivery_stage':True,
            'runtime_requirements':{'requires_gpu':False,'python_modules':['jsonschema'],
                'system_tools':['python'],'network_urls':[url]}}
        proposal={'proposal_id':'network-scope','base_graph_version':0,'reason':'Read approved data',
            'add_tasks':[task],'sealed':True}
        denied=graph.create(self.store,self.f.project,uuid.uuid4().hex,graph.bind_authorization(raw))
        with self.assertRaisesRegex(graph.AuthorizationRequired,'HTTPS URL'):
            graph.validate_proposal(self.store,denied,proposal)
        allowed=graph.create(self.store,self.f.project,uuid.uuid4().hex,
            graph.bind_authorization({**raw,'allowed_network_urls':[url]}))
        _,tasks,_,_=graph.validate_proposal(self.store,allowed,proposal)
        self.assertEqual(tasks[0].runtime_requirements.network_urls,[url])

    def test_unchanged_agent_keeps_frozen_policy_while_repair_uses_current_policy(self):
        import copy
        authorization=graph.bind_authorization({**self.run['authorization'],
            'allowed_agent_profiles':['editing_implementer','media_failure_diagnostician']})
        encoded=graph.encode(authorization.model_dump())
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(encoded,self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=? AND version=1',(encoded,self.run['id']))
        self.tick()
        old_agent=script('old-agent',type='agent',profile='editing_implementer',argv=[],inputs={})
        subject=script('subject')
        self.propose({'proposal_id':'old-policy','base_graph_version':0,'reason':'Initial work',
            'add_tasks':[old_agent,subject],'sealed':False,'replan_after':['subject']})
        run=self.tick();self.assertEqual(run['graph_version'],1)
        current=copy.deepcopy(run['authorization']);old=copy.deepcopy(current)
        old['agent_profile_digests']['editing_implementer']='frozen-old-policy'
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=? AND version=1',
                           (graph.encode(old),run['id']))
        self.store.execute('INSERT INTO run_authorizations VALUES (?,?,?,?)',(run['id'],2,graph.encode(current),self.store.now()))
        self.store.execute('UPDATE runs SET authorization_json=?,authorization_version=2 WHERE id=?',
                           (graph.encode(current),run['id']))
        self.store.execute("UPDATE run_tasks SET status=CASE id WHEN 'old-agent' THEN 'succeeded' ELSE 'paused' END WHERE run_id=?",
                           (run['id'],))
        run=graph.get(self.store,run['id']);source=next(t['spec'] for t in run['tasks'] if t['id']=='subject')
        replacement={**source,'id':'repair','type':'agent','profile':'editing_implementer','argv':[]}
        proposal={'proposal_id':'current-policy-repair','base_graph_version':1,'reason':'Repair failed task',
            'add_tasks':[replacement],'replacements':{'subject':'repair'},'sealed':False,'replan_after':['repair']}
        _,tasks,_,_=graph.validate_proposal(self.store,{**run,'status':'ACTIVE'},proposal,repair_scope='subject')
        self.assertEqual([task.id for task in tasks],['repair'])

    def test_unstarted_agent_rebinds_execution_identity_only_when_semantics_match(self):
        authorization=graph.bind_authorization({**self.run['authorization'],
            'allowed_agent_profiles':['editing_implementer','media_failure_diagnostician']})
        encoded=graph.encode(authorization.model_dump())
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(encoded,self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(encoded,self.run['id']))
        self.tick();self.propose({'proposal_id':'agent-policy','base_graph_version':0,'reason':'Create report',
            'add_tasks':[script(type='agent',profile='editing_implementer',argv=[],inputs={})],'sealed':True})
        run=self.tick();task=run['tasks'][0];frozen={**task['profile'],'harness_digest':'0'*64}
        self.store.execute('UPDATE run_tasks SET profile_json=? WHERE run_id=? AND id=?',(graph.encode(frozen),run['id'],task['id']))
        self.orchestrator.rebind_pending_agent_policies(graph.get(self.store,run['id']))
        rebound=graph.get(self.store,run['id'])['tasks'][0]
        self.assertNotEqual(rebound['profile']['harness_digest'],'0'*64)
        self.assertTrue(self.store.query("SELECT seq FROM workflow_graph_events WHERE run_id=? AND kind='task_execution_policy_rebound'",(run['id'],)))
        changed={**rebound['profile'],'prompt':rebound['profile']['prompt']+' changed','harness_digest':'1'*64}
        self.store.execute('UPDATE run_tasks SET profile_json=? WHERE run_id=? AND id=?',(graph.encode(changed),run['id'],task['id']))
        self.orchestrator.rebind_pending_agent_policies(graph.get(self.store,run['id']))
        self.assertEqual(graph.get(self.store,run['id'])['tasks'][0]['profile']['harness_digest'],'1'*64)

    def test_authorized_rejected_proposal_is_restaged_without_another_model_turn(self):
        self.tick();request_id=self.propose({'proposal_id':'reuse-approved','base_graph_version':0,
            'reason':'Create report','add_tasks':[script()],'sealed':True})
        request=self.store.query('SELECT task_id FROM planner_runs WHERE id=?',(request_id,))[0]
        self.store.execute("UPDATE planner_runs SET status='rejected',error_code='authorization_required' WHERE id=?",(request_id,))
        self.store.execute("UPDATE control_jobs SET status='failed',error_code='authorization_required' WHERE run_id=? AND id=?",
                           (self.run['id'],request['task_id']))
        self.assertTrue(planning.restage_after_authorization(self.store,graph.get(self.store,self.run['id']),request_id))
        row=self.store.query('SELECT status,error_code FROM planner_runs WHERE id=?',(request_id,))[0]
        self.assertEqual(row,{'status':'staged','error_code':''})
        applied=self.tick();self.assertEqual(applied['graph_version'],1)
        self.assertEqual(len(self.store.query('SELECT id FROM planner_runs WHERE run_id=?',(self.run['id'],))),1)

    def test_plan_change_answer_applies_same_proposal_without_replanning(self):
        self.tick();request_id=self.propose({'proposal_id':'approved-in-place','base_graph_version':0,
            'reason':'Create report','add_tasks':[script()],'sealed':True})
        request=self.store.query('SELECT task_id,proposal_json FROM planner_runs WHERE id=?',(request_id,))[0]
        self.store.execute("UPDATE planner_runs SET status='awaiting_authorization',error_code='authorization_required' WHERE id=?",(request_id,))
        self.store.execute("UPDATE control_jobs SET status='waiting',error_code='authorization_required' WHERE run_id=? AND id=?",
                           (self.run['id'],request['task_id']))
        self.orchestrator.decision(graph.get(self.store,self.run['id']),'plan_change',{
            'planner_run':request_id,'proposal':json.loads(request['proposal_json'])['proposal'],
            'reason':'Agent policy changed and requires renewed authorization'})
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        current=graph.get(self.store,self.run['id'])
        self.orchestrator.answer(current,{'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],
            'action':'approve','authorization':current['authorization']})
        self.assertEqual(self.store.query('SELECT status FROM planner_runs WHERE id=?',(request_id,))[0]['status'],'staged')
        self.assertEqual(len(self.store.query('SELECT id FROM planner_runs WHERE run_id=?',(self.run['id'],))),1)
        self.assertEqual(self.tick()['graph_version'],1)

    def test_authorization_failure_preserves_staged_proposal_and_digest(self):
        authorization=graph.bind_authorization({**self.run['authorization'],
            'allowed_agent_profiles':['editing_implementer','media_failure_diagnostician']}).model_dump()
        authorization['agent_profile_digests']['editing_implementer']='stale-policy'
        encoded=graph.encode(authorization)
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(encoded,self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(encoded,self.run['id']))
        self.tick();request_id=self.propose({'proposal_id':'needs-current-policy','base_graph_version':0,
            'reason':'Create report','add_tasks':[script(type='agent',profile='editing_implementer',argv=[],inputs={})],
            'sealed':True})
        current=self.tick();self.assertEqual(current['status'],'WAITING_USER')
        planner=self.store.query('SELECT status,error_code,proposal_json FROM planner_runs WHERE id=?',(request_id,))[0]
        self.assertEqual((planner['status'],planner['error_code']),('awaiting_authorization','authorization_required'))
        self.assertIn(self.store.query('SELECT status FROM workflow_plan_drafts WHERE planning_request_id=?',(request_id,))[0]['status'],('OPEN','COMMITTED'))
        decision=self.store.query("SELECT request_json FROM workflow_decisions WHERE run_id=? AND kind='plan_change' AND status='pending'",(self.run['id'],))[0]
        request=json.loads(decision['request_json'])
        self.assertEqual(request['proposal_digest'],graph.digest(json.loads(planner['proposal_json'])))

    def test_pending_duplicate_recovers_last_approved_proposal(self):
        self.tick();request_id=self.propose({'proposal_id':'recover-approved','base_graph_version':0,
            'reason':'Create report','add_tasks':[script()],'sealed':True})
        request=self.store.query('SELECT task_id,proposal_json FROM planner_runs WHERE id=?',(request_id,))[0]
        self.store.execute("UPDATE planner_runs SET status='rejected',error_code='authorization_required' WHERE id=?",(request_id,))
        self.store.execute("UPDATE control_jobs SET status='failed',error_code='authorization_required' WHERE run_id=? AND id=?",
                           (self.run['id'],request['task_id']))
        approved_request={'planner_run':request_id,'proposal':json.loads(request['proposal_json'])['proposal'],
            'reason':'Agent policy changed and requires renewed authorization'}
        self.orchestrator.decision(graph.get(self.store,self.run['id']),'plan_change',approved_request)
        approved=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        authorization=graph.get(self.store,self.run['id'])['authorization']
        self.store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                           (graph.encode({'action':'approve','authorization':authorization}),self.store.now(),approved['id']))
        self.orchestrator.decision(graph.get(self.store,self.run['id']),'plan_change',approved_request)
        waiting=graph.get(self.store,self.run['id']);self.orchestrator.advance(waiting)
        self.assertEqual(self.store.query('SELECT status FROM planner_runs WHERE id=?',(request_id,))[0]['status'],'staged')
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(self.run['id'],)))
        self.assertEqual(graph.get(self.store,self.run['id'])['terminal_reason'],'authorization_proposal_resumed')

    def test_approving_identical_authorization_is_idempotent(self):
        from webapp.run_authorization import approve
        before=len(self.store.query('SELECT version FROM run_authorizations WHERE run_id=?',(self.run['id'],)))
        version=approve(self.store,self.run,graph.Authorization.model_validate(self.run['authorization']))
        self.assertEqual(version,self.run['authorization_version'])
        self.assertEqual(len(self.store.query('SELECT version FROM run_authorizations WHERE run_id=?',(self.run['id'],))),before)

    def test_waiting_user_continues_only_existing_external_poll(self):
        from webapp.workflow_dispatch import available,candidates
        from webapp.workflow_state_axes import project as state_axes
        self.tick();self.propose({'proposal_id':'poll-during-decision','base_graph_version':0,'reason':'Create work',
            'add_tasks':[script('external',outputs=['external.txt'],output_roles={'report':'external.txt'},delivery_stage=False),
                         script('new-submit',outputs=['new.txt'],output_roles={'report':'new.txt'},delivery_stage=False)],
            'sealed':False,'replan_after':['external']})
        run=self.tick();self.assertTrue(run['tasks'],run['repair_context']);now=self.store.now()
        self.store.execute("UPDATE run_tasks SET status='waiting',phase='poll',external_id='provider-job',next_run_at=0 WHERE run_id=? AND id='external'",(run['id'],))
        self.store.execute("UPDATE run_tasks SET status='ready',phase='local',next_run_at=0 WHERE run_id=? AND id='new-submit'",(run['id'],))
        self.orchestrator.decision(graph.get(self.store,run['id']),'plan_change',{'reason':'Confirm repair'})
        waiting=graph.get(self.store,run['id']);self.orchestrator.advance(waiting)
        current=graph.get(self.store,run['id'])
        self.assertEqual(next(t for t in current['tasks'] if t['id']=='external')['status'],'ready')
        self.assertEqual(available(self.store,current,now),['external'])
        self.assertEqual([item['step_id'] for item in candidates(self.store)],['external'])
        self.assertEqual(state_axes(self.store,current),{
            'business_admission':'BLOCKED','user_decision':'PENDING','external_reconcile':'ACTIVE',
            'control_activity':'IDLE','input_barrier':'NONE'})

    def test_frozen_model_profile_rejects_too_short_code_editor_task_before_confirmation(self):
        import uuid
        from dataclasses import replace
        from webapp.agent_profiles import AgentProfile
        configuration=replace(AgentProfile(),specialist_minimum_seconds={'editing_implementer':600})
        authorization=graph.bind_authorization({**self.authorization,
            'allowed_agent_profiles':['editing_implementer']})
        run=graph.create(self.store,self.f.project,uuid.uuid4().hex,authorization,
            model_configuration=configuration)
        task=script(type='agent',profile='editing_implementer',argv=[],inputs={},
            outputs=['report.txt'],output_roles={'report':'report.txt'},timeout_seconds=300,
            runtime_requirements={'requires_gpu':False,'python_modules':[],
                                  'system_tools':['python'],'network_urls':[]})
        proposal={'proposal_id':'model-timeout','base_graph_version':0,'reason':'Create report',
                  'add_tasks':[task],'sealed':True}
        with self.assertRaisesRegex(graph.ProposalFieldError,'at least 600 seconds'):
            graph.validate_proposal(self.store,run,proposal)
        proposal['add_tasks'][0]['timeout_seconds']=600
        _,tasks,_,_=graph.validate_proposal(self.store,run,proposal)
        self.assertEqual(tasks[0].timeout_seconds,600)

    def test_planning_snapshot_freezes_both_task_revision_budgets(self):
        from webapp.workflow_model_protocol import available_changes,automatic_replacement_tasks
        self.tick()
        self.propose({'proposal_id':'budget-projection','reason':'Create report','base_graph_version':0,
            'add_tasks':[script()],'replan_after':['first']})
        self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',repair_count=2,user_revision_count=4 WHERE run_id=? AND id='first'",(self.run['id'],))
        run=graph.get(self.store,self.run['id']);frozen=planning.snapshot(self.store,run)
        self.assertEqual((frozen['tasks'][0]['repair_count'],frozen['tasks'][0]['user_revision_count']),(2,4))
        frozen['trigger']={'task_id':'first'}
        self.assertFalse(available_changes(frozen));self.assertFalse(automatic_replacement_tasks(frozen))
        frozen['input_batch']={'inputs':[{'context':{'allowed_revision_tasks':['first']}}]}
        self.assertIn('first',available_changes(frozen))

    def test_invalid_or_conflicting_catalogue_defers_planning_and_recovers(self):
        from webapp.operations import catalog
        from unittest.mock import patch
        operation=catalog()['operations'][0];good={'operations':[operation]}
        for worker in ('catalogue-a','catalogue-b'):
            self.store.execute('INSERT INTO execution_workers VALUES (?,?,?,?,?,0)',
                               (worker,worker,'release','',self.store.now()))
            self.store.execute('INSERT INTO execution_operation_catalog VALUES (?,?,?)',
                               (worker,graph.encode(good),self.store.now()))
        malformed=['{invalid','[]','null','{}',graph.encode({'operations':{}}),
                   graph.encode({'operations':[operation,operation]}),
                   graph.encode({'operations':[{'id':None}]}),
                   graph.encode({'operations':[{**operation,'implementation':'conflicting@1'}]}),
                   graph.encode({'operations':[]})]
        for value in malformed:
            with self.subTest(catalogue=value):
                self.store.execute('UPDATE execution_operation_catalog SET catalogue=? WHERE worker=?',(value,'catalogue-b'))
                with patch('webapp.telemetry.emit') as emitted:
                    self.assertIsNone(planning.available_operations(self.store))
                    self.assertTrue(emitted.called)
                self.assertIsNone(planning.queue(self.store,graph.get(self.store,self.run['id']),{}))
                self.assertFalse(self.store.query('SELECT id FROM planner_runs'))
                self.assertFalse(self.store.query('SELECT id FROM control_jobs'))
                state=graph.get(self.store,self.run['id'])
                self.assertEqual((state['status'],state['terminal_reason']),('ACTIVE','workers_unavailable'))
                self.assertGreater(state['next_reconcile_at'],self.store.now())
        self.store.execute('UPDATE execution_operation_catalog SET catalogue=? WHERE worker=?',(graph.encode(good),'catalogue-b'))
        self.assertIsNotNone(planning.queue(self.store,graph.get(self.store,self.run['id']),{}))
        self.assertEqual(len(self.store.query('SELECT id FROM planner_runs')),1)
        self.assertEqual(len(self.store.query('SELECT id FROM control_jobs')),1)
        self.assertEqual(graph.get(self.store,self.run['id'])['terminal_reason'],'')

    def test_frozen_manager_schema_exposes_real_acceptance_target_domain(self):
        from pydantic import ValidationError
        from webapp.quality_policy import Acceptance
        self.tick()
        frozen=json.loads(self.store.query('SELECT snapshot_json FROM planner_runs WHERE run_id=?',(self.run['id'],))[0]['snapshot_json'])
        target=frozen['proposal_schema']['$defs']['Acceptance']['properties']['target']
        values=next(item['enum'] for item in target['anyOf'] if 'enum' in item)
        self.assertEqual(set(values),{'file','image','media','text','json','subtitle'})
        for value in values:
            with self.subTest(target=value):
                extra={'contains':['approved text']} if value=='text' else {}
                self.assertEqual(Acceptance(target=value,**extra).kind,value)
                self.assertEqual(Acceptance(target=value,reviewer='agent',criterion='Inspect this output').kind,'agent')
                self.assertEqual(Acceptance(target=value,reviewer='user',criterion='Confirm this output').kind,'review')
        for invalid in ('final.png','primary_image','audio','video','unknown'):
            with self.subTest(invalid=invalid),self.assertRaises(ValidationError) as error:
                Acceptance(target=invalid)
            failure=error.exception.errors(include_input=False)[0]
            self.assertEqual(failure['loc'],('target',))
            self.assertEqual(failure['type'],'literal_error')
            self.assertIn('media',failure['msg'])
        with self.assertRaises(ValidationError):Acceptance(kind='image',target='media')

    def test_script_requires_executable_not_flags_for_an_imaginary_tool(self):
        with self.assertRaisesRegex(ValueError,'executable'):
            graph.Task.model_validate(script(argv=['--input-role','source','--output','report.txt']))
        self.assertEqual(graph.Task.model_validate(script()).argv[0],'python')

    def test_specialist_report_contract_is_visible_and_cannot_require_impossible_fields(self):
        from webapp.specialist_profiles import catalogue
        profile=next(p for p in catalogue() if p['id']=='visual_quality_reviewer')
        self.assertIn('verdict',profile['result_schema']['properties'])
        task=script(type='agent',profile='visual_quality_reviewer',argv=[],outputs=['report.json'],
                    output_roles={'report':'report.json'},acceptance=[{'kind':'json','required_keys':['verdict']}],delivery_stage=False)
        for control_profile in ('visual_quality_reviewer','media_failure_diagnostician','media_planner','delivery_reviewer'):
            with self.subTest(control_profile=control_profile),self.assertRaisesRegex(ValueError,'Control profiles'):
                graph.Task.model_validate({**task,'profile':control_profile})

    def test_agent_acceptance_cannot_reference_an_undeclared_output(self):
        task=script(type='agent',profile='editing_implementer',argv=[],outputs=['final.png'],
                    output_roles={'primary_image':'final.png'},acceptance=[{'kind':'image','file':'final.jpg'}])
        with self.assertRaisesRegex(ValueError,'declared output'):graph.Task.model_validate(task)

    def test_rolling_plan_waits_for_trigger_and_never_delivers_unsealed(self):
        run=self.tick();self.assertEqual(run['status'],'ACTIVE')
        self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),1)
        self.tick();self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),1)
        request=self.propose({'proposal_id':'round-one','base_graph_version':0,'reason':'First inspect','add_tasks':[script()],
                             'replan_after':['first']})
        run=self.tick();self.assertEqual(run['graph_version'],1)
        task=next(t for t in graph.work_items(run) if t['id']=='first');self.assertEqual(task['status'],'ready')
        self.tick();self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),1)
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='first'",(run['id'],))
        run=self.tick();self.assertFalse(run['sealed']);self.assertIsNone(run['delivered_at'])
        self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),2)
        applied=self.store.query('SELECT * FROM planner_runs WHERE id=?',(request,))[0]
        with self.store.connect():self.assertEqual(planning.apply(self.store,run,applied),1)
        self.assertEqual(graph.get(self.store,run['id'])['graph_version'],1)
        self.propose({'proposal_id':'round-two','base_graph_version':1,'reason':'Append final processing',
                      'add_tasks':[script('second')],'add_dependencies':[{'upstream':'first','downstream':'second'}],'sealed':True})
        run=self.tick();self.assertEqual(run['graph_version'],2);self.assertTrue(run['sealed'])
        self.assertIsNone(run['delivered_at'])

    def test_invalid_graph_has_no_partial_task_inserts(self):
        self.tick()
        request=self.propose({'proposal_id':'cycle','base_graph_version':0,'reason':'bad cycle','add_tasks':[script('a'),script('b')],
            'add_dependencies':[{'upstream':'a','downstream':'b'},{'upstream':'b','downstream':'a'}],'sealed':True})
        self.store.execute("UPDATE workflow_plan_drafts SET status='COMMITTED' WHERE planning_request_id=?",(request,))
        run=self.tick()
        self.assertEqual(run['graph_version'],0)
        self.assertFalse([t for t in graph.work_items(run) if not t.get('control_kind')])
        self.assertEqual(self.store.query("SELECT error_code FROM planner_runs WHERE proposal_id='cycle'")[0]['error_code'],'invalid_proposal')
        self.assertEqual(self.store.query('SELECT status FROM workflow_plan_drafts WHERE planning_request_id=?',(request,))[0]['status'],'REJECTED')

    def test_scope_expansion_requires_user_decision_and_cannot_be_resumed(self):
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',
            (graph.encode({**self.run['authorization'],'allowed_operations':[]}),self.run['id']))
        self.tick();self.propose({'proposal_id':'unauthorized','base_graph_version':0,'reason':'Request script',
                                'add_tasks':[script()],'sealed':True})
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER')
        self.assertEqual(run['graph_version'],0)
        graph.command(self.store,run['id'],'owner','resume')
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER')
        self.assertEqual(self.store.query('SELECT status FROM workflow_commands')[0]['status'],'rejected')

    def test_missing_asset_is_invalid_but_existing_unapproved_asset_requires_authorization(self):
        self.tick();run=graph.get(self.store,self.run['id'])
        raw={'proposal_id':'asset-reference','base_graph_version':0,'reason':'Use source',
             'add_tasks':[script(inputs={'source':{'asset':'not-in-this-project'}})],'sealed':True}
        with self.assertRaisesRegex(ValueError,'does not exist'):
            graph.validate_proposal(self.store,run,raw)
        self.assertFalse(self.store.query('SELECT id FROM workflow_decisions'))
        self.store.add_asset(self.f.project,'unapproved','other.png','other.png',{'kind':'image','width':8,'height':8})
        raw['add_tasks'][0]['inputs']['source']['asset']='unapproved'
        self.propose(raw);current=self.tick()
        self.assertEqual(current['status'],'WAITING_USER')
        self.assertEqual(current['graph_version'],0)

    def test_pre_submit_failure_requires_changed_plan_and_never_claims_unknown_charge(self):
        self.tick();self.propose({'proposal_id':'pre-submit','base_graph_version':0,'reason':'Run',
            'add_tasks':[script()],'sealed':True});run=self.tick()
        task=next(t for t in graph.work_items(run) if t['id']=='first')
        self.orchestrator.task_failure(run,task,{'error_code':'pre_submit','phase':'execute','classification':'pre_submit'})
        current=graph.get(self.store,run['id']);task=next(t for t in graph.work_items(current) if t['id']=='first')
        self.assertEqual(task['status'],'paused');self.assertEqual(task['error_code'],'pre_submit')
        self.assertIn('尚未发起外部请求',task['error_detail'])
        graph.command(self.store,run['id'],'owner','resume');self.tick()
        self.assertEqual(self.store.query('SELECT error_code FROM workflow_commands')[0]['error_code'],'replan_required')
        self.assertFalse(self.store.query('SELECT request_key FROM workflow_provider_calls'))

    def test_planner_can_discover_unapproved_contract_without_obtaining_authority(self):
        frozen=planning.snapshot(self.store,self.run)
        operation=next(op for op in frozen['operations'] if op['id']=='image.generate_from_text')
        self.assertFalse(operation['authorized'])
        self.assertNotIn('parameters_schema',operation)
        detail=planning.operation_contract(self.store,frozen,'image.generate_from_text')
        self.assertIn('parameters_schema',detail)
        self.assertEqual(detail['contract_digest'],operation['contract_digest'])
        self.assertNotIn('image.generate_from_text',frozen['authorization']['allowed_operations'])
        self.assertNotIn('media_planner',[p['id'] for p in frozen['profiles']])

    def test_reconcile_lease_fences_expired_owner(self):
        first=self.orchestrator.claim_due()[0]
        self.store.execute('UPDATE runs SET reconcile_until=0 WHERE id=?',(self.run['id'],))
        other=Orchestrator(self.store,'other');second=other.claim_due()[0]
        with self.assertRaises(ReconcileLost):self.orchestrator.reconcile(*first)
        other.reconcile(*second)
        self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),1)

    def test_interrupted_planner_rebinds_current_policy_instead_of_failing_run(self):
        self.tick()
        current=graph.get(self.store,self.run['id']);old=current['control_jobs'][0]
        frozen=dict(old['profile']);frozen['harness_digest']='0'*64
        self.store.execute("UPDATE control_jobs SET status='running',profile_json=?,lease_id='dead-planner',lease_generation=1 WHERE run_id=? AND id=?",
            (graph.encode(frozen),self.run['id'],old['id']))
        self.orchestrator.recover(graph.get(self.store,self.run['id']))
        run=graph.get(self.store,self.run['id']);active=[job for job in run['control_jobs'] if job['status']=='ready']
        self.assertEqual(run['status'],'ACTIVE');self.assertEqual(len(active),1)
        self.assertNotEqual(active[0]['id'],old['id'])
        self.assertNotEqual(active[0]['profile']['harness_digest'],'0'*64)
        retired=next(job for job in run['control_jobs'] if job['id']==old['id'])
        self.assertEqual(retired['status'],'cancelled')
        self.assertEqual(self.store.query('SELECT status,error_code FROM planner_runs WHERE id=?',(old['id'],))[0],
            {'status':'rejected','error_code':'policy_rebound'})
        self.assertTrue(self.store.query("SELECT seq FROM workflow_graph_events WHERE run_id=? AND kind='control_policy_rebound'",(run['id'],)))

    def test_policy_rebind_does_not_consume_business_planning_round(self):
        self.tick()
        current=graph.get(self.store,self.run['id']);old=current['control_jobs'][0]
        authorization=dict(current['authorization']);authorization['max_planning_rounds']=1
        frozen=dict(old['profile']);frozen['harness_digest']='0'*64
        self.store.execute("UPDATE runs SET authorization_json=? WHERE id=?",(graph.encode(authorization),self.run['id']))
        self.store.execute("UPDATE control_jobs SET status='running',profile_json=?,lease_id='dead-planner',lease_generation=1 WHERE run_id=? AND id=?",
            (graph.encode(frozen),self.run['id'],old['id']))
        self.orchestrator.recover(graph.get(self.store,self.run['id']))
        run=graph.get(self.store,self.run['id'])
        self.assertEqual(run['status'],'ACTIVE')
        self.assertEqual(len([job for job in run['control_jobs'] if job['status']=='ready']),1)
        self.assertEqual(len(self.store.query("SELECT id FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],))),1)

    def test_cancel_command_is_idempotent_and_blocks_planning(self):
        first=graph.command(self.store,self.run['id'],'owner','cancel',request_id='same-command')
        self.assertEqual(graph.command(self.store,self.run['id'],'owner','cancel',request_id='same-command')['id'],first['id'])
        with self.assertRaises(ValueError):graph.command(self.store,self.run['id'],'owner','pause',request_id='same-command')
        run=self.tick();self.assertEqual(run['status'],'CANCELLED')
        self.assertFalse(self.store.query('SELECT * FROM planner_runs'))
        rejected=graph.command(self.store,self.run['id'],'owner','resume')
        self.assertEqual(rejected['status'],'rejected')
        self.assertEqual(rejected['error_code'],'workflow_terminal')

    def test_cancel_waits_for_active_external_cleanup_before_terminal_state(self):
        graph.command(self.store,self.run['id'],'owner','cancel',request_id='stop-project')
        self.store.execute("INSERT INTO execution_external_cleanup(id,task_id,provider,external_id,state) VALUES (?,?,?,?,?)",
            ('provider-cleanup',self.run['id'],'https://provider.invalid','job-1','pending'))
        current=self.tick();self.assertEqual(current['status'],'CANCELLING')
        from webapp.workflow_api import public
        shown=public(self.store,current);self.assertEqual(shown['cleanup_pending'],1)
        self.store.execute("UPDATE execution_external_cleanup SET state='complete' WHERE id='provider-cleanup'")
        current=self.tick();self.assertEqual(current['status'],'CANCELLED')
        self.assertEqual(current['terminal_reason'],'user_cancelled')

    def test_pause_cannot_undo_an_earlier_cancel_command(self):
        graph.command(self.store,self.run['id'],'owner','cancel')
        graph.command(self.store,self.run['id'],'owner','pause')
        run=self.tick();self.assertEqual(run['status'],'CANCELLED')
        row=self.store.query("SELECT status,error_code FROM workflow_commands WHERE action='pause'")[0]
        self.assertEqual(row['status'],'rejected');self.assertEqual(row['error_code'],'cancellation_in_progress')

    def test_cancel_rejects_committed_pending_draft_but_preserves_applied_history(self):
        self.tick()
        applied=self.propose({'proposal_id':'first-plan','base_graph_version':0,'reason':'First batch',
            'add_tasks':[script()],'replan_after':['first']})
        self.tick()
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='first'",(self.run['id'],))
        self.tick()
        pending=self.propose({'proposal_id':'second-plan','base_graph_version':1,'reason':'Second batch',
            'add_tasks':[script('second')],'replan_after':['second']})
        self.store.execute("UPDATE workflow_plan_drafts SET status='COMMITTED' WHERE planning_request_id=?",(pending,))
        graph.command(self.store,self.run['id'],'owner','cancel')
        run=self.tick()
        self.assertEqual(run['status'],'CANCELLED')
        self.assertEqual(run['graph_version'],1)
        self.assertFalse(any(task['id']=='second' for task in run['tasks']))
        self.assertEqual(self.store.query('SELECT status FROM planner_runs WHERE id=?',(pending,))[0]['status'],'rejected')
        self.assertEqual(self.store.query('SELECT status FROM workflow_plan_drafts WHERE planning_request_id=?',(pending,))[0]['status'],'REJECTED')
        self.assertEqual(self.store.query('SELECT status FROM workflow_plan_drafts WHERE planning_request_id=?',(applied,))[0]['status'],'COMMITTED')

    def test_failed_planner_rejects_its_already_committed_unapplied_draft(self):
        self.tick()
        request=self.propose({'proposal_id':'failed-after-stage','base_graph_version':0,'reason':'Staged work',
            'add_tasks':[script()],'replan_after':['first']})
        self.store.execute("UPDATE workflow_plan_drafts SET status='COMMITTED' WHERE planning_request_id=?",(request,))
        run=graph.get(self.store,self.run['id']);task=next(t for t in run['control_jobs'] if t['id']==request)
        self.orchestrator.task_failure(run,task,{'error_code':'planning_contract_exhausted','phase':'local'})
        self.assertEqual(self.store.query('SELECT status FROM planner_runs WHERE id=?',(request,))[0]['status'],'rejected')
        self.assertEqual(self.store.query('SELECT status FROM workflow_plan_drafts WHERE planning_request_id=?',(request,))[0]['status'],'REJECTED')
        self.assertEqual(graph.get(self.store,self.run['id'])['graph_version'],0)
        self.assertFalse(graph.get(self.store,self.run['id'])['tasks'])
        self.assertEqual(graph.get(self.store,self.run['id'])['status'],'CANCELLING')
        self.tick()
        failed=graph.get(self.store,self.run['id'])
        self.assertEqual((failed['status'],failed['terminal_reason']),('FAILED','planning_contract_exhausted'))
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(self.run['id'],)))

    def test_existing_tasks_cannot_gain_dependencies_or_change_contracts(self):
        self.tick();self.propose({'proposal_id':'initial','base_graph_version':0,'reason':'Initial',
                                'add_tasks':[script()],'replan_after':['first']})
        run=self.tick()
        for change in ({'add_tasks':[script()]},
                       {'add_tasks':[script('later')],'add_dependencies':[{'upstream':'later','downstream':'first'}]}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                graph.validate_proposal(self.store,run,{'proposal_id':'illegal','base_graph_version':1,'reason':'Invalid','sealed':True,**change})

    def test_successful_user_revision_is_a_new_replan_trigger(self):
        self.tick();self.propose({'proposal_id':'initial-revision','base_graph_version':0,'reason':'Initial',
                                'add_tasks':[script()],'replan_after':['first']})
        run=self.tick();self.store.execute("UPDATE run_tasks SET status='succeeded',lease_id='' WHERE run_id=? AND id='first'",(run['id'],))
        run=graph.get(self.store,run['id']);current=next(task for task in graph.work_items(run) if task['id']=='first')['spec']
        revised={**current,'name':'Revised visible title','objective':'Write the revised approved report','argv':['python','-c','print("revised")']}
        proposal,_,revisions,_=graph.validate_proposal(self.store,run,{
            'proposal_id':'user-revision','base_graph_version':run['graph_version'],'reason':'Apply user revision',
            'revise_tasks':[revised],'replan_after':['first']},user_revision_scope={'first'})
        self.assertFalse(proposal.sealed)
        self.assertEqual(set(revisions),{'first'})
        self.assertEqual((revisions['first'].name,revisions['first'].objective),('Revised visible title','Write the revised approved report'))

    def test_dependency_can_be_removed_and_readded_as_a_new_graph_generation(self):
        from tests.run_input_fixture import accept_change as accept
        self.tick();self.propose({'proposal_id':'initial-edge','base_graph_version':0,'reason':'Initial dependency',
            'add_tasks':[script('first'),script('child',inputs={'source':{'step':'first','role':'report'}})],
            'add_dependencies':[{'upstream':'first','downstream':'child'}],'sealed':True})
        run=self.tick()
        accept(self.store,run['id'],'owner',{'request_id':'1'*32,'content':'Make child independent','targets':['child']})
        run=self.tick();child=next(task['spec'] for task in run['tasks'] if task['id']=='child')
        self.propose({'proposal_id':'remove-edge','base_graph_version':1,'reason':'Remove dependency',
            'revise_tasks':[{**child,'inputs':{}}],
            'remove_dependencies':[{'upstream':'first','downstream':'child'}],'sealed':True})
        run=self.tick();self.assertEqual(run['graph_version'],2);self.assertEqual(run['dependencies'],[])
        accept(self.store,run['id'],'owner',{'request_id':'2'*32,'content':'Restore the source dependency','targets':['child']})
        run=self.tick();child=next(task['spec'] for task in run['tasks'] if task['id']=='child')
        self.propose({'proposal_id':'restore-edge','base_graph_version':2,'reason':'Restore dependency',
            'revise_tasks':[{**child,'inputs':{'source':{'step':'first','role':'report'}}}],
            'add_dependencies':[{'upstream':'first','downstream':'child'}],'sealed':True})
        run=self.tick();self.assertEqual(run['graph_version'],3)
        self.assertEqual(run['dependencies'],[{'upstream_task_id':'first','downstream_task_id':'child'}])
        rows=self.store.query('''SELECT created_graph_version,retired_graph_version FROM workflow_dependencies
            WHERE run_id=? AND upstream_task_id='first' AND downstream_task_id='child' ORDER BY created_graph_version''',(run['id'],))
        self.assertEqual(rows,[{'created_graph_version':1,'retired_graph_version':2},
                               {'created_graph_version':3,'retired_graph_version':None}])
        view=graph.execution_view(run);child_view=next(step for step in view['steps'] if step['id']=='child')
        self.assertEqual(child_view['spec']['depends_on'],['first'])

    def test_scheduler_cannot_replay_before_orchestrator_accepts_a_result(self):
        from webapp.task_leases import Coordinator
        from webapp.workflow_dispatch import stage_result
        self.tick();self.propose({'proposal_id':'one','base_graph_version':0,'reason':'run once','add_tasks':[script()],'sealed':True})
        self.tick();coordinator=Coordinator(self.store,'execution-test','test')
        lease=coordinator.claim(self.f.turn,'workflow',step_id='first');self.assertIsNotNone(lease)
        try:
            with lease.bind():stage_result(self.store,lease,'failed',{'error_code':'execution_failed','phase':'execute'})
        finally:lease.release()
        self.assertIsNone(coordinator.claim(self.f.turn,'workflow',step_id='first'))
        self.assertFalse([r for r in coordinator.candidates() if r.get('step_id')=='first'])
        self.assertEqual(self.store.query("SELECT COUNT(*) AS n FROM execution_attempts WHERE step_id='first'")[0]['n'],1)

    def test_result_staging_rejects_malformed_envelopes_without_a_partial_record(self):
        from webapp.task_leases import Coordinator
        from webapp.workflow_dispatch import stage_result
        self.tick();self.propose({'proposal_id':'result-envelope','base_graph_version':0,'reason':'Run once',
                                  'add_tasks':[script()],'sealed':True})
        self.tick();lease=Coordinator(self.store,'result-envelope','test').claim(self.run['id'],'workflow',step_id='first')
        self.assertIsNotNone(lease)
        try:
            with self.assertRaisesRegex(ValueError,'outcome'):stage_result(self.store,lease,'unknown',{})
            with self.assertRaisesRegex(ValueError,'data'):stage_result(self.store,lease,'succeeded',[])
            with self.assertRaisesRegex(ValueError,'manifest'):stage_result(self.store,lease,'succeeded',{},[])
            with self.assertRaisesRegex(ValueError,'manifest'):stage_result(self.store,lease,'succeeded',{}, {'output.txt':[]})
            self.assertFalse(self.store.query('SELECT id FROM task_results WHERE run_id=?',(self.run['id'],)))
        finally:lease.release()

    def test_revised_repair_can_run_while_another_business_task_is_paused(self):
        from webapp.workflow_dispatch import candidates,available
        self.tick();self.propose({'proposal_id':'parallel-repair','base_graph_version':0,'reason':'Two independent tasks',
            'add_tasks':[script('failed'),script('revised')],'sealed':False,'replan_after':['failed','revised']})
        run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='agent_failed' WHERE run_id=? AND id='failed'",(run['id'],))
        self.store.execute("UPDATE run_tasks SET status='ready',repair_count=1 WHERE run_id=? AND id='revised'",(run['id'],))
        self.store.execute("UPDATE runs SET status='ACTIVE' WHERE id=?",(run['id'],))
        run=graph.get(self.store,run['id'])
        self.assertIn('revised',available(self.store,run,self.store.now()))
        self.assertIn('revised',{row['step_id'] for row in candidates(self.store)})
        self.store.execute("UPDATE run_tasks SET repair_count=0 WHERE run_id=? AND id='revised'",(run['id'],))
        run=graph.get(self.store,run['id'])
        self.assertNotIn('revised',available(self.store,run,self.store.now()))
        self.assertNotIn('revised',{row['step_id'] for row in candidates(self.store)})

    def test_late_diagnosis_cannot_stop_a_newer_running_revision(self):
        self.tick();task=script(retry_policy='never');task['on_failure']='diagnose'
        self.propose({'proposal_id':'stale-diagnosis','base_graph_version':0,'reason':'diagnose failure',
                      'add_tasks':[task],'sealed':True});run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='agent_failed' WHERE run_id=? AND id='first'",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run,'first',purpose='diagnose')
        run=graph.get(self.store,run['id']);diagnosis=next(t for t in graph.work_items(run) if t.get('control_kind')=='DIAGNOSE')
        row=self.store.query("SELECT snapshot_digest FROM workflow_reviews WHERE id=?",(diagnosis['id'],))[0]
        self.store.execute("UPDATE run_tasks SET revision=1,status='running',error_code='',lease_id='new-attempt' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE runs SET status='ACTIVE',terminal_reason='' WHERE id=?",(run['id'],))
        self.orchestrator.accept_diagnosis(graph.get(self.store,run['id']),diagnosis,{
            'snapshot_digest':row['snapshot_digest'],'diagnosis':{'facts':['old failure'],'uncertainties':[],
            'recommendations':['repair old revision'],'error_code':'agent_failed',
            'recommended_action':'stop','target_task_id':'first'}})
        current=graph.get(self.store,run['id']);subject=next(t for t in current['tasks'] if t['id']=='first')
        self.assertEqual((current['status'],current['terminal_reason']),('ACTIVE',''))
        self.assertEqual((subject['revision'],subject['status'],subject['lease_id']),(1,'running','new-attempt'))
        self.assertTrue(self.store.query("SELECT seq FROM workflow_graph_events WHERE kind='diagnosis_stale'"))

    def test_internal_model_transport_failure_retries_same_frozen_request_only(self):
        run=self.tick();manager=next(task for task in graph.work_items(run) if task.get('control_kind'))
        self.store.execute("UPDATE run_tasks SET status='running',attempts=1 WHERE run_id=? AND id=?",(run['id'],manager['id']))
        manager=next(task for task in graph.work_items(graph.get(self.store,run['id'])) if task['id']==manager['id'])
        self.orchestrator.task_failure(graph.get(self.store,run['id']),manager,
            {'error_code':'agent_failed','phase':'execute','classification':'execution','transient':True})
        current=graph.get(self.store,run['id']);retried=next(task for task in graph.work_items(current) if task['id']==manager['id'])
        self.assertEqual(retried['status'],'retry_wait')
        self.assertEqual(self.store.query('SELECT status FROM planner_runs WHERE task_id=?',(manager['id'],))[0]['status'],'pending')
        self.assertNotEqual(current['status'],'WAITING_USER')
        from webapp.task_leases import Coordinator
        self.store.execute("UPDATE control_jobs SET status='ready',next_run_at=0 WHERE run_id=? AND id=?",(run['id'],manager['id']))
        lease=Coordinator(self.store,'retry-control','test').claim(run['id'],'workflow',step_id=manager['id'])
        self.assertIsNotNone(lease)
        claimed=next(task for task in graph.work_items(graph.get(self.store,run['id'])) if task['id']==manager['id'])
        self.assertEqual((claimed['error_code'],claimed['error_detail'],claimed['wait_reason']),('','',''))
        lease.release()

    def test_model_transport_failure_is_not_presented_as_a_script_error(self):
        self.tick();self.propose({'proposal_id':'model-reset','reason':'Create report','base_graph_version':0,
            'add_tasks':[script(on_failure='pause')],'replan_after':['first']})
        run=self.tick();task=run['tasks'][0]
        self.store.execute("UPDATE run_tasks SET status='running',attempts=3 WHERE run_id=? AND id=?",(run['id'],task['id']))
        task=graph.get(self.store,run['id'])['tasks'][0]
        self.orchestrator.task_failure(graph.get(self.store,run['id']),task,
            {'error_code':'agent_model_transport_failed','phase':'local','classification':'execution',
             'error_detail':"Connection reset by peer",'error_type':'ProtocolError','transient':True})
        failed=graph.get(self.store,run['id'])['tasks'][0]
        self.assertEqual(failed['error_code'],'agent_model_transport_failed')
        self.assertIn('模型连接中断',failed['error_detail'])
        self.assertNotIn('脚本执行失败',failed['error_detail'])

    def test_sealed_failure_keeps_ready_diagnosis_dispatchable(self):
        self.tick();task=script(retry_policy='never');task['on_failure']='diagnose'
        self.propose({'proposal_id':'diagnose-failure','base_graph_version':0,'reason':'diagnose a known failure',
                      'add_tasks':[task],'sealed':True});run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='running',attempts=1 WHERE run_id=? AND id='first'",(run['id'],))
        current=graph.get(self.store,run['id']);failed=next(t for t in graph.work_items(current) if t['id']=='first')
        self.orchestrator.task_failure(current,failed,{'error_code':'execution_failed','phase':'local','classification':'execution'})
        current=graph.get(self.store,run['id']);diagnosis=next(t for t in graph.work_items(current) if t.get('control_kind')=='DIAGNOSE')
        self.assertEqual((current['status'],current['terminal_reason']),('ACTIVE','failure_diagnosis'))
        self.assertEqual(diagnosis['status'],'ready')
        self.orchestrator.advance_graph(current,{t['id'] for t in graph.work_items(current) if t['status']=='succeeded'})
        current=graph.get(self.store,run['id'])
        self.assertEqual((current['status'],current['terminal_reason']),('ACTIVE','failure_diagnosis'))

    def test_interrupted_nonretryable_task_requires_controller_recovery(self):
        from webapp.task_leases import Coordinator
        self.tick();self.propose({'proposal_id':'one','base_graph_version':0,'reason':'no replay',
                                 'add_tasks':[script(retry_policy='never')],'sealed':True})
        self.tick();coordinator=Coordinator(self.store,'execution-test','test')
        lease=coordinator.claim(self.f.turn,'workflow',step_id='first');self.assertIsNotNone(lease)
        lease.release(interrupted=True)
        self.assertIsNone(coordinator.claim(self.f.turn,'workflow',step_id='first'))
        run=self.tick()
        self.assertEqual((run['status'],run['terminal_reason']),('FAILED','result_unavailable'))
        self.assertEqual(next(t for t in graph.work_items(run) if t['id']=='first')['error_code'],'result_unavailable')
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],)))

    def test_fatal_failure_cancels_siblings_and_waits_for_cleanup_before_failed_terminal(self):
        from webapp.task_leases import Coordinator
        self.tick();self.propose({'proposal_id':'fatal-cleanup','base_graph_version':0,'reason':'Parallel work',
            'add_tasks':[script('first'),script('sibling')],'sealed':True})
        run=self.tick();coordinator=Coordinator(self.store,'fatal-cleanup','test')
        lease=coordinator.claim(run['id'],'workflow',step_id='sibling');self.assertIsNotNone(lease)
        self.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'pending',?)",
                           (lease.attempt,run['id'],'fatal-cleanup',self.store.now()))
        first=next(task for task in graph.get(self.store,run['id'])['tasks'] if task['id']=='first')
        self.orchestrator.task_failure(graph.get(self.store,run['id']),first,{'error_code':'result_unavailable'})
        current=graph.get(self.store,run['id']);self.assertEqual((current['status'],current['terminal_reason']),('CANCELLING','result_unavailable'))
        self.orchestrator.cancel(current)
        current=graph.get(self.store,run['id']);self.assertEqual(current['status'],'CANCELLING')
        self.assertEqual(next(task for task in current['tasks'] if task['id']=='sibling')['status'],'cancelled')
        lease.release(interrupted=True)
        self.store.execute("UPDATE execution_cleanup SET state='complete' WHERE attempt=?",(lease.attempt,))
        self.orchestrator.cancel(graph.get(self.store,run['id']))
        current=graph.get(self.store,run['id']);self.assertEqual((current['status'],current['terminal_reason']),('FAILED','result_unavailable'))

    def test_pause_does_not_apply_a_finished_manager_proposal(self):
        self.tick();self.propose({'proposal_id':'paused-plan','base_graph_version':0,'reason':'initial',
                                'add_tasks':[script()],'sealed':True})
        graph.command(self.store,self.run['id'],'owner','pause')
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER');self.assertEqual(run['graph_version'],0)
        graph.command(self.store,self.run['id'],'owner','resume')
        run=self.tick();self.assertEqual(run['graph_version'],1)

    def test_revision_change_fences_the_old_executor_before_result_write(self):
        from webapp.task_leases import Coordinator,LeaseLost
        from webapp.workflow_dispatch import stage_result
        self.tick();self.propose({'proposal_id':'one','base_graph_version':0,'reason':'owned revision','add_tasks':[script()],'sealed':True})
        self.tick();lease=Coordinator(self.store,'revision-test','test').claim(self.f.turn,'workflow',step_id='first')
        self.assertIsNotNone(lease)
        try:
            self.store.execute("UPDATE run_tasks SET revision=revision+1 WHERE run_id=? AND id='first'",(self.run['id'],))
            with self.assertRaises(LeaseLost):stage_result(self.store,lease,'succeeded',{})
            self.assertFalse(self.store.query('SELECT id FROM task_results WHERE lease_id=?',(lease.attempt,)))
        finally:lease.release(interrupted=True)

    def test_invalid_candidate_is_rejected_without_reconcile_loop_or_delivery(self):
        from webapp.task_leases import Coordinator
        from webapp.workflow_dispatch import stage_result
        self.tick();self.propose({'proposal_id':'one','base_graph_version':0,'reason':'validate output','add_tasks':[script()],'sealed':True})
        self.tick();lease=Coordinator(self.store,'candidate-test','test').claim(self.f.turn,'workflow',step_id='first')
        self.assertIsNotNone(lease)
        try:
            with lease.bind():stage_result(self.store,lease,'succeeded',{'files':['edit/other-project/report.txt'],'quality':{'technical_valid':True,'passed':True,'files':[]}})
        finally:lease.release()
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER');self.assertIsNone(run['delivered_at'])
        row=self.store.query('SELECT status,error_json FROM task_results')[0]
        self.assertEqual(row['status'],'rejected')
        error=json.loads(row['error_json']);self.assertEqual(error['error_code'],'invalid_result_manifest')
        self.assertIn('escaped task output scope',error['message'])
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER')

    def test_metadata_controller_cannot_read_or_upload_object_bytes(self):
        from unittest.mock import patch
        from webapp.artifact_store import ArtifactStore
        source=self.store.root/'file.txt';source.write_text('actual bytes')
        objects=ArtifactStore(self.store);record=objects.upload(self.f.project,source)
        objects.publish(self.f.project,'edit/original.txt',record['id'])
        with patch.dict('os.environ',{'VIDEO_USE_STORAGE_METADATA_ONLY':'1','VIDEO_USE_OBJECT_BUCKET':'private'}),patch('webapp.artifact_store.S3Objects',side_effect=AssertionError('Controller must not construct a credentialed backend')):
            metadata=ArtifactStore(self.store)
            self.assertEqual(metadata.record(self.f.project,record['id'])['sha256'],record['sha256'])
            metadata.publish(self.f.project,'edit/reference.txt',record['id'])
            with self.assertRaises(PermissionError):metadata.materialize(self.f.project,record['id'])
            with self.assertRaises(PermissionError):metadata.upload(self.f.project,source)
            with self.assertRaises(PermissionError):metadata.collect_garbage()
        self.assertEqual(len(self.store.query('SELECT id FROM artifact_objects')),1)

    def publish_metadata_fixture(self,names,primary_index=0,expect_success=True,selected_indices=None,
                                 dependencies=None,delivery_indices=None,before_publish=None,review_current_scope=True,expect_rereview=False):
        """Exercise publication of previously accepted metadata, not decoding."""
        from unittest.mock import patch
        delivery_indices=set(range(len(names))) if delivery_indices is None else set(delivery_indices)
        self.tick();self.propose({'proposal_id':'publish-order','base_graph_version':0,'reason':'Deliver selected files',
            'add_tasks':[script('part_'+str(i),outputs=[name],output_roles={'result':name},delivery_stage=i in delivery_indices)
                         for i,name in enumerate(names)],
            'add_dependencies':dependencies or [],'sealed':True})
        self.tick();selected=[];hashes={}
        for index,name in enumerate(names):
            task='part_'+str(index);logical=f'edit/runs/{self.run["id"]}/{task}/output/{name}'
            source=self.store.root/f'accepted-{index}';source.write_bytes(('accepted-content-'+str(index)).encode())
            record=self.store.artifacts.upload(self.f.project,source);selected.append(logical);hashes[name]=record['sha256']
            manifest={logical:{'artifact':record['id'],'sha256':record['sha256'],'size':record['size']}}
            self.store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
                (self.run['id'],task,0,self.f.project,'accepted-attempt',graph.encode(manifest),self.store.now()))
            self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=? WHERE run_id=? AND id=?",
                (graph.encode([logical]),self.run['id'],task))
        run=graph.get(self.store,self.run['id'])
        with patch.dict('os.environ',{'VIDEO_USE_OBJECT_BUCKET':'publication-metadata-fixture'}):
            reviewed=selected if selected_indices is None else [selected[index] for index in selected_indices]
            decision={'deliverables':reviewed}
            if primary_index is not None:decision['primary']=selected[primary_index]
            import uuid
            review_id=uuid.uuid4().hex;now=self.store.now()
            self.store.execute('''INSERT INTO workflow_reviews(id,run_id,task_id,purpose,subject_task,graph_version,review_identity,
                snapshot_json,snapshot_digest,status,decision_json,created_at,updated_at)
                VALUES (?,?,'final-review','final','',?,'fixture','{}',?,'passed',?,?,?)''',
                (review_id,run['id'],run['graph_version'],'f'*64,graph.encode(decision),now,now))
            if before_publish:before_publish(run,review_id)
            if review_current_scope:
                from webapp.review_identity import final_identity
                self.store.execute('UPDATE workflow_reviews SET review_identity=? WHERE id=?',
                    (final_identity(self.store,graph.get(self.store,run['id'])),review_id))
            self.orchestrator.publish(run,{'id':review_id,'decision_json':'{}'})
            if not expect_success:
                result=graph.get(self.store,run['id'])
                self.assertEqual(result['status'],'ACTIVE' if expect_rereview else 'WAITING_USER');self.assertIsNone(result['delivered_at']);return result['delivery'],hashes
            if primary_index is None:
                result=graph.get(self.store,run['id'])
                self.assertEqual(result['status'],'WAITING_USER');self.assertIsNone(result['delivered_at'])
                self.assertEqual(result['delivery'],[])
                self.assertFalse(self.store.query("SELECT seq FROM workflow_graph_events WHERE kind='delivered'"))
                return result['delivery'],hashes
            self.orchestrator.publish(graph.get(self.store,run['id']),{'id':review_id,'decision_json':'{}'})
        result=graph.get(self.store,run['id'])
        self.assertEqual(result['status'],'SUCCEEDED')
        self.assertEqual(len(self.store.query("SELECT seq FROM workflow_graph_events WHERE kind='delivered'")),1)
        return result['delivery'],hashes

    def test_forged_review_payload_cannot_publish_without_a_passed_final_review(self):
        run=self.review_fixture()
        self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=?",(run['id'],))
        run=graph.get(self.store,run['id'])
        self.orchestrator.publish(run,{'decision_json':graph.encode({'deliverables':['edit/report.txt'],'primary':'edit/report.txt'})})
        current=graph.get(self.store,run['id'])
        self.assertEqual((current['status'],current['terminal_reason']),('WAITING_USER','invalid_delivery_manifest'))
        self.assertIsNone(current['delivered_at'])
        self.assertFalse(self.store.query("SELECT id FROM workflow_reviews WHERE run_id=? AND purpose='final'",(run['id'],)))

    def test_storage_quota_recovery_retries_publication_without_reexecuting_tasks(self):
        from unittest.mock import patch
        from webapp.workflow_api import public
        def block(run,review_id):
            guard=patch.object(self.store,'check_storage',side_effect=ValueError('存储额度不足，请清理旧项目'))
            guard.start();self.addCleanup(guard.stop)
            self.storage_guard=guard
        self.publish_metadata_fixture(['report.txt'],before_publish=block,expect_success=False)
        run=graph.get(self.store,self.run['id'])
        self.assertEqual(run['terminal_reason'],'storage_quota_exceeded')
        self.assertIn('存储额度',public(self.store,run)['blocking_action']['message'])
        before=[(t['id'],t['revision'],t['status'],t['attempts']) for t in run['tasks']]
        self.storage_guard.stop()
        command=graph.command(self.store,run['id'],'owner','resume')
        with patch.dict('os.environ',{'VIDEO_USE_OBJECT_BUCKET':'publication-metadata-fixture'}):run=self.tick()
        self.assertEqual(run['status'],'SUCCEEDED')
        self.assertEqual(self.store.query('SELECT status FROM workflow_commands WHERE id=?',(command['id'],))[0]['status'],'applied')
        self.assertEqual([(t['id'],t['revision'],t['status'],t['attempts']) for t in run['tasks']],before)
        self.assertEqual(len(run['delivery']),1)

    def test_publication_checkpoint_is_durable_and_only_orchestrator_can_release_delivery(self):
        from unittest.mock import patch
        def require_checkpoint(run,review_id):
            authorization={**run['authorization'],'review_policy':{
                'technical_review':'required','user_checkpoint':{
                    'enabled':True,'stage':'before_publish','reason':'发布前核对最终报告'}}}
            self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),run['id']))
            self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=? AND version=?',
                               (graph.encode(authorization),run['id'],run['authorization_version']))
        self.publish_metadata_fixture(['report.txt'],before_publish=require_checkpoint,expect_success=False)
        run=graph.get(self.store,self.run['id']);self.assertIsNone(run['delivered_at'])
        row=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND kind='publication_checkpoint' AND status='pending'",
                             (run['id'],))[0]
        request=json.loads(row['request_json'])
        self.assertEqual(request['deliverables'],['report.txt']);self.assertEqual(request['primary'],'report.txt')
        self.orchestrator.answer(run,{'decision_id':row['id'],'snapshot_digest':row['snapshot_digest'],'action':'approve'})
        review=self.store.query("SELECT * FROM workflow_reviews WHERE run_id=? AND purpose='final' AND status='passed'",(run['id'],))[0]
        with patch.dict('os.environ',{'VIDEO_USE_OBJECT_BUCKET':'publication-metadata-fixture'}):
            self.orchestrator.publish(graph.get(self.store,run['id']),review)
        current=graph.get(self.store,run['id'])
        self.assertEqual(current['status'],'SUCCEEDED');self.assertEqual(len(current['delivery']),1)
        self.assertEqual(self.store.query('SELECT status FROM workflow_decisions WHERE id=?',(row['id'],))[0]['status'],'applied')

    def test_passed_final_review_can_be_reused_after_graph_only_change(self):
        from webapp.review_identity import identity,final_criteria
        def reseal(run,review_id):
            frozen={'review_scope':'final','goal':run['goal'],'delivery_targets':run['authorization']['delivery_targets'],
                    'tasks':graph.active_tasks(run),'review_criteria':final_criteria(run,graph.active_tasks(run))}
            key=identity(self.store,run,frozen)
            self.store.execute('UPDATE workflow_reviews SET review_identity=? WHERE id=?',(key,review_id))
            self.store.execute('UPDATE runs SET graph_version=graph_version+1 WHERE id=?',(run['id'],))
        delivered,_=self.publish_metadata_fixture(['report.txt'],before_publish=reseal)
        self.assertEqual(len(delivered),1)
        self.assertEqual(len(self.store.query("SELECT id FROM workflow_reviews WHERE purpose='final'")),1)
        self.assertEqual(self.store.query("SELECT graph_version FROM workflow_reviews WHERE purpose='final'")[0]['graph_version'],1)

    def test_cross_graph_review_reuse_rejects_changed_requirements(self):
        from webapp.review_identity import identity
        def change_goal(run,review_id):
            frozen={'review_scope':'final','goal':run['goal'],'delivery_targets':run['authorization']['delivery_targets'],
                    'tasks':graph.active_tasks(run)}
            self.store.execute('UPDATE workflow_reviews SET review_identity=? WHERE id=?',(identity(self.store,run,frozen),review_id))
            self.store.execute("UPDATE runs SET graph_version=graph_version+1,goal='Different required contents' WHERE id=?",(run['id'],))
        delivered,_=self.publish_metadata_fixture(['report.txt'],before_publish=change_goal,expect_success=False,
                                                 review_current_scope=False,expect_rereview=True)
        self.assertEqual(delivered,[])
        self.assertTrue(self.store.query("SELECT id FROM workflow_reviews WHERE run_id=? AND purpose='final' AND status='pending'",(self.run['id'],)))

    def test_publisher_rejects_explicitly_excluded_files_even_if_review_passed(self):
        def scope(run,review_id):
            authorization={**run['authorization'],'delivery_contract':graph.DeliveryContract(
                filenames=['final.txt'],excluded_filenames=['internal.json']).model_dump()}
            self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
                ('保留内部记录，只交付最终文本',graph.encode(authorization),run['id']))
        delivered,_=self.publish_metadata_fixture(['final.txt','internal.json'],before_publish=scope,expect_success=False)
        self.assertEqual(delivered,[])

    def test_excluded_delivery_stage_does_not_force_publication_of_internal_file(self):
        def scope(run,review_id):
            authorization={**run['authorization'],'delivery_contract':graph.DeliveryContract(
                filenames=['final.txt'],excluded_filenames=['internal.json']).model_dump()}
            self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
                ('保留内部记录，只交付最终文本',graph.encode(authorization),run['id']))
        delivered,_=self.publish_metadata_fixture(['final.txt','internal.json'],selected_indices=[0],before_publish=scope)
        self.assertEqual([item['name'] for item in delivered],['final.txt'])

    def test_publisher_rejects_preview_only_selection_even_with_a_passed_review(self):
        run=self.review_fixture()
        self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=?,quality_json=? WHERE run_id=? AND id='first'",
            (graph.encode(['edit/movie.mp4','edit/thumb.png']),graph.encode({'output_roles':{
                'video':'edit/movie.mp4','preview':'edit/thumb.png'}}),run['id']))
        run=graph.get(self.store,run['id']);now=self.store.now()
        self.store.execute('''INSERT INTO workflow_reviews(id,run_id,task_id,purpose,subject_task,graph_version,review_identity,
            snapshot_json,snapshot_digest,status,decision_json,created_at,updated_at)
            VALUES ('preview-review',?,'final-review','final','',?,'preview-only','{}',?,'passed',?,?,?)''',
            (run['id'],run['graph_version'],'f'*64,graph.encode({'deliverables':['edit/thumb.png'],'primary':'edit/thumb.png'}),now,now))
        from webapp.review_identity import final_identity
        self.store.execute("UPDATE workflow_reviews SET review_identity=? WHERE id='preview-review'",(final_identity(self.store,run),))
        self.orchestrator.publish(run,{'id':'preview-review'})
        current=graph.get(self.store,run['id'])
        self.assertEqual(current['terminal_reason'],'invalid_delivery_manifest')
        self.assertEqual(current['delivery'],[]);self.assertIsNone(current['delivered_at'])

    def test_video_remains_primary_when_review_lists_cover_first(self):
        delivered,hashes=self.publish_metadata_fixture(['cover.png','final.mp4','review.json'],primary_index=1)
        self.assertEqual([a['name'] for a in delivered if a['is_primary']],['final.mp4'])
        self.assertEqual(delivered[0]['name'],'final.mp4')
        self.assertEqual({a['name']:a['sha256'] for a in delivered},hashes)

    def test_duplicate_display_names_do_not_create_multiple_primary_deliveries(self):
        delivered,_=self.publish_metadata_fixture(['image.png','image.png'])
        self.assertEqual(sum(a['is_primary'] for a in delivered),1)
        self.assertEqual(len({a['file'] for a in delivered}),2)

    def test_lossless_final_image_precedes_cover_and_compressed_rendition(self):
        delivered,_=self.publish_metadata_fixture(['cover.png','final.jpg','final.png'],primary_index=2)
        self.assertEqual([a['name'] for a in delivered if a['is_primary']],['final.png'])

    def test_explicit_primary_overrides_final_filename_and_duplicate_names(self):
        delivered,hashes=self.publish_metadata_fixture(['final.mp4','final.mp4','master_8s_horizontal.mp4'],primary_index=2)
        self.assertEqual(delivered[0]['name'],'master_8s_horizontal.mp4')
        self.assertEqual(delivered[0]['sha256'],hashes['master_8s_horizontal.mp4'])
        self.assertEqual(sum(a['is_primary'] for a in delivered),1)
        self.assertEqual(len({a['file'] for a in delivered}),3)

    def test_multiple_deliverables_without_primary_are_not_published(self):
        self.publish_metadata_fixture(['master_8s_horizontal.mp4','final.mp4'],primary_index=None)

    def test_explicit_file_delivery_targets_must_all_be_selected(self):
        authorization={**self.run['authorization'],'delivery_targets':['最终 PNG 及原始 TXT 记录'],
            'delivery_contract':graph.DeliveryContract(filenames=['final.png','record.txt']).model_dump()}
        self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
            ('交付 final.png 和 record.txt',graph.encode(authorization),self.run['id']))
        self.publish_metadata_fixture(['final.png'],expect_success=False)
        self.assertEqual(graph.get(self.store,self.run['id'])['terminal_reason'],'delivery_targets_incomplete')
        payload=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE kind='delivery_blocked'")[0]['payload_json'])
        self.assertEqual(payload['missing_extensions'],['txt'])
        self.assertEqual(payload['missing_task_ids'],[])
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND kind='delivery_scope_change' AND status='pending'",(self.run['id'],)))
        from webapp.workflow_api import public
        shown=public(self.store,graph.get(self.store,self.run['id']))
        self.assertEqual(shown['blocking_action']['kind'],'delivery_error')
        self.assertIn('TXT',shown['blocking_action']['message'])
        self.assertIn('无需缩小需求',shown['blocking_action']['message'])

    def test_explicit_delivery_filename_cannot_be_replaced_by_same_format(self):
        authorization={**self.run['authorization'],'delivery_targets':['final.mp4 and cover.png'],
            'delivery_contract':graph.DeliveryContract(filenames=['final.mp4','cover.png']).model_dump()}
        self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
            ('Deliver final.mp4 and cover.png',graph.encode(authorization),self.run['id']))
        self.publish_metadata_fixture(['clip_128x64.mp4','cover.png'],primary_index=0,expect_success=False)
        payload=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE kind='delivery_blocked'")[0]['payload_json'])
        self.assertEqual(payload['missing_names'],['final.mp4'])

    def test_explicit_report_role_must_be_selected(self):
        authorization={**self.run['authorization'],'delivery_targets':['final report'],
            'delivery_contract':graph.DeliveryContract(roles=['report']).model_dump()}
        self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
            ('Deliver final.png and an objective report',graph.encode(authorization),self.run['id']))
        self.publish_metadata_fixture(['final.png','report.json'],expect_success=False)
        payload=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE kind='delivery_blocked'")[0]['payload_json'])
        self.assertEqual(payload['missing_roles'],['report'])

    def test_input_asset_extension_is_not_treated_as_a_delivery_format(self):
        authorization={**self.run['authorization'],'delivery_targets':['符合需求的 512x512 图片'],
            'delivery_contract':graph.DeliveryContract(formats=['jpeg']).model_dump()}
        self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
            ('将 source.png 转为 JPEG，保持内容不变',graph.encode(authorization),self.run['id']))
        delivered,_=self.publish_metadata_fixture(['final.jpg'])
        self.assertEqual([item['name'] for item in delivered],['final.jpg'])

    def test_each_delivery_stage_leaf_must_contribute_a_selected_file(self):
        self.publish_metadata_fixture(['landscape.mp4','portrait.mp4'],primary_index=0,
                                      expect_success=False,selected_indices=[0])
        self.assertEqual(graph.get(self.store,self.run['id'])['terminal_reason'],'delivery_targets_incomplete')
        payload=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE kind='delivery_blocked'")[0]['payload_json'])
        self.assertEqual(payload['missing_task_ids'],['part_1'])
        self.assertEqual(payload['missing_extensions'],[])

    def test_delivery_leaf_detection_traverses_non_delivery_tasks(self):
        delivered,_=self.publish_metadata_fixture(['transparent.png','layout.json','poster.png'],primary_index=2,
            selected_indices=[2],delivery_indices=[0,2],dependencies=[
                {'upstream':'part_0','downstream':'part_1'},
                {'upstream':'part_1','downstream':'part_2'}])
        self.assertEqual([item['name'] for item in delivered],['poster.png'])

    def test_primary_selection_is_validated_by_worker_and_controller(self):
        from webapp.workflow_agents import validated_internal_result
        from webapp.specialist_profiles import get
        run=self.review_fixture()
        self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=? WHERE run_id=? AND id='first'",
            (graph.encode(['edit/master.mp4','edit/final.mp4']),run['id']))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run)
        row=self.store.query("SELECT * FROM workflow_reviews WHERE purpose='final'")[0]
        task=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']==row['task_id'])
        frozen={**json.loads(row['snapshot_json']),'snapshot_digest':row['snapshot_digest']}
        selected=['edit/master.mp4','edit/final.mp4']
        ids=['a'*32,'b'*32];inspections=dict(zip(ids,selected));keys={'delivery_1':ids[0],'delivery_2':ids[1]}
        decision={'snapshot_digest':row['snapshot_digest'],'verdict':'pass','deliverables':list(keys),
                  'evidence':[{'criterion_index':index,'method_use':'not_applicable','observation':'Checked both variants against this requirement','inspection_id':ids[0]}
                              for index in range(len(frozen['review_criteria']))]}
        with self.assertRaisesRegex(ValueError,'[Pp]rimary'):
            validated_internal_result(run,task,get('delivery_reviewer'),{'decision':decision},frozen,inspections,keys)
        with self.assertRaisesRegex(ValueError,'candidate_key'):
            validated_internal_result(run,task,get('delivery_reviewer'),{'decision':{**decision,'primary':'f'*32}},frozen,inspections,keys)
        self.assertEqual(self.store.query('SELECT status FROM workflow_reviews')[0]['status'],'pending')
        valid={**decision,'primary':'delivery_1'}
        with self.assertRaisesRegex(ValueError,'inspection_id'):
            validated_internal_result(run,task,get('delivery_reviewer'),{'decision':valid},frozen,{ids[1]:selected[1]},keys)
        for identifier,reference in inspections.items():
            self.store.execute('INSERT INTO workflow_inspections VALUES (?,?,?,?,?,?,?,?)',
                (identifier,run['id'],task['id'],task['lease_id'],reference,row['snapshot_digest'],'{}',self.store.now()))
        resolved=validated_internal_result(run,task,get('delivery_reviewer'),{'decision':valid},frozen,inspections,keys)['decision']
        self.assertEqual(resolved['primary'],selected[0])
        by_ref={**valid,'deliverables':selected,'primary':selected[0]}
        self.assertEqual(validated_internal_result(run,task,get('delivery_reviewer'),{'decision':by_ref},frozen,inspections,keys)['decision']['primary'],selected[0])
        by_name={**valid,'deliverables':['master.mp4','final.mp4'],'primary':'master.mp4'}
        self.assertEqual(validated_internal_result(run,task,get('delivery_reviewer'),{'decision':by_name},frozen,inspections,keys)['decision']['primary'],selected[0])
        exact_name={**frozen,'delivery_contract':graph.DeliveryContract(filenames=['requested.mp4']).model_dump()}
        with self.assertRaisesRegex(ValueError,'required filenames'):
            validated_internal_result(run,task,get('delivery_reviewer'),{'decision':valid},exact_name,inspections,keys)
        only={'delivery_1':ids[0]};automatic={**valid,'deliverables':[],'primary':''}
        resolved=validated_internal_result(run,task,get('delivery_reviewer'),{'decision':automatic},frozen,{ids[0]:selected[0]},only)['decision']
        self.assertEqual((resolved['deliverables'],resolved['primary']),([selected[0]],selected[0]))
        self.orchestrator.accept_review(run,task,{'decision':resolved})
        accepted=self.store.query('SELECT status,decision_json FROM workflow_reviews')[0]
        self.assertEqual(accepted['status'],'passed');self.assertEqual(json.loads(accepted['decision_json'])['primary'],selected[0])

    def review_fixture(self,user_review=False):
        self.tick();self.propose({'proposal_id':'review-source','base_graph_version':0,'reason':'Source',
                                'add_tasks':[script(acceptance=[{'kind':'file'}]+([{'kind':'review','criterion':'User content confirmation'}] if user_review else []))],'sealed':True})
        self.tick()
        self.store.execute("UPDATE run_tasks SET status='reviewing',phase='validate',candidate_result_json=?,quality_json=? WHERE run_id=? AND id='first'",
            (graph.encode(['edit/report.txt']),graph.encode({'technical_valid':True,'passed':True}),self.run['id']))
        return graph.get(self.store,self.run['id'])

    def test_intermediate_review_is_bound_to_step_criteria_not_final_delivery(self):
        run=self.review_fixture();subject=next(t for t in graph.work_items(run) if t['id']=='first')
        criteria=['自然暖色，保持主体完整','中间文件声音保留']
        spec={**subject['spec'],'objective':'生成暖色中间母版；字幕在后续步骤添加',
              'acceptance':[{'kind':'agent','criterion':c} for c in criteria]}
        self.store.execute('UPDATE run_tasks SET spec_json=? WHERE run_id=? AND id=?',
            (graph.encode(spec),run['id'],'first'))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run,'first')
        row=self.store.query("SELECT * FROM workflow_reviews WHERE purpose='task'")[0]
        frozen=json.loads(row['snapshot_json']);self.assertEqual(frozen['goal'],spec['objective'])
        self.assertEqual(frozen['review_criteria'],criteria);self.assertNotIn('delivery_targets',frozen)
        task=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']==row['task_id'])
        self.assertEqual(task['spec']['objective'],spec['objective'])
        from webapp.workflow_agents import validated_internal_result
        from webapp.specialist_profiles import get
        inspection='c'*32
        invalid={'snapshot_digest':row['snapshot_digest'],'verdict':'reject','evidence':[
            {'criterion_index':99,'method_use':'not_applicable','observation':'Final subtitles are missing','inspection_id':inspection}]}
        with self.assertRaisesRegex(ValueError,'Step review evidence'):
            validated_internal_result(run,task,get('visual_quality_reviewer'),{'decision':invalid},
                {**frozen,'snapshot_digest':row['snapshot_digest']},{inspection:'edit/report.txt'})
        self.assertEqual(self.store.query('SELECT status FROM workflow_reviews')[0]['status'],'pending')
        valid={**invalid,'verdict':'pass','evidence':[{'criterion_index':index,'method_use':'not_applicable','observation':'Inspected this step','inspection_id':inspection} for index in range(len(criteria))]}
        with self.assertRaisesRegex(ValueError,'every frozen'):
            validated_internal_result(run,task,get('visual_quality_reviewer'),{'decision':{**valid,'evidence':valid['evidence'][:1]}},
                {**frozen,'snapshot_digest':row['snapshot_digest']},{inspection:'edit/report.txt'})
        self.store.execute('INSERT INTO workflow_inspections VALUES (?,?,?,?,?,?,?,?)',
            (inspection,run['id'],task['id'],task['lease_id'],'edit/report.txt',row['snapshot_digest'],'{}',self.store.now()))
        resolved=validated_internal_result(run,task,get('visual_quality_reviewer'),{'decision':valid},
            {**frozen,'snapshot_digest':row['snapshot_digest']},{inspection:'edit/report.txt'})['decision']
        self.orchestrator.accept_review(run,task,{'decision':resolved})
        current=graph.get(self.store,run['id']);self.assertEqual(next(t for t in graph.work_items(current) if t['id']=='first')['status'],'succeeded')
        self.orchestrator.queue_review(current)
        final=json.loads(self.store.query("SELECT snapshot_json FROM workflow_reviews WHERE purpose='final'")[0]['snapshot_json'])
        self.assertEqual(final['review_scope'],'final');self.assertEqual(final['goal'],run['goal'])
        self.assertEqual(final['delivery_targets'],run['authorization']['delivery_targets'])

    def test_agent_review_cannot_half_commit_before_program_acceptance_check(self):
        run=self.review_fixture()
        self.store.execute('UPDATE run_tasks SET quality_json=? WHERE run_id=? AND id=?',
            (graph.encode({'technical_valid':False,'passed':False}),run['id'],'first'))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run,'first')
        row=self.store.query("SELECT * FROM workflow_reviews WHERE purpose='task'")[0]
        task=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']==row['task_id'])
        inspection='d'*32
        self.store.execute('INSERT INTO workflow_inspections VALUES (?,?,?,?,?,?,?,?)',
            (inspection,run['id'],task['id'],task['lease_id'],'edit/report.txt',row['snapshot_digest'],'{}',self.store.now()))
        decision={'snapshot_digest':row['snapshot_digest'],'verdict':'pass','evidence':[
            {'criterion':'满足当前步骤目标及输出契约','method_use':'not_applicable','observation':'Content appears acceptable','ref':'edit/report.txt','inspection_id':inspection}],
            'deliverables':[],'primary':'','feedback':''}
        with self.assertRaisesRegex(ValueError,'program acceptance'):
            self.orchestrator.accept_review(graph.get(self.store,run['id']),task,{'decision':decision})
        self.assertEqual(self.store.query('SELECT status FROM workflow_reviews WHERE id=?',(row['id'],))[0]['status'],'pending')
        self.assertEqual(self.store.query('SELECT status FROM control_jobs WHERE id=?',(task['id'],))[0]['status'],'ready')
        subject=next(t for t in graph.get(self.store,run['id'])['tasks'] if t['id']=='first')
        self.assertEqual(subject['status'],'reviewing');self.assertNotIn('agent_review_passed',subject['quality'])

    def test_rejected_step_review_routes_to_manager_and_cannot_be_resumed_as_retry(self):
        run=self.review_fixture();self.orchestrator.queue_review(run,'first')
        current=graph.get(self.store,run['id']);review=next(t for t in graph.work_items(current) if t.get('control_kind') and t['spec'].get('profile')=='visual_quality_reviewer')
        self.orchestrator.accept_review(current,review,{'decision':{'snapshot_digest':review['spec']['snapshot_digest'],
            'verdict':'reject','feedback':'The actual evidence does not meet the criterion.'}})
        rejected=graph.get(self.store,run['id']);subject=next(t for t in rejected['tasks'] if t['id']=='first')
        self.assertEqual(subject['status'],'paused');self.assertEqual(subject['error_code'],'quality_failed')
        self.assertFalse(subject['accepted_result'])
        graph.command(self.store,run['id'],'owner','resume')
        self.assertEqual(self.tick()['status'],'ACTIVE')
        self.assertEqual(self.store.query("SELECT status FROM workflow_commands WHERE action='resume'")[0]['status'],'rejected')
        self.assertEqual(len(self.store.query("SELECT id FROM planner_runs WHERE status='pending'")),1)

    def test_blocked_descendant_does_not_deadlock_repair_planning(self):
        self.tick();self.propose({'proposal_id':'blocked-repair','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first'),script('child')],
            'add_dependencies':[{'upstream':'first','downstream':'child'}],
            'sealed':True,'replan_after':['child']})
        run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE run_tasks SET status='blocked' WHERE run_id=? AND id='child'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        self.store.execute("UPDATE planner_runs SET status='rejected' WHERE run_id=? AND status='pending'",(run['id'],))
        self.store.execute("UPDATE control_jobs SET status='cancelled' WHERE run_id=? AND control_kind='PLAN' AND status='ready'",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.open_repair(run,'first','task_review','review-id','quality_failed')
        before={row['id'] for row in self.store.query("SELECT id FROM planner_runs WHERE run_id=?",(run['id'],))}
        self.orchestrator.advance(graph.get(self.store,run['id']))
        planner=[row for row in self.store.query("SELECT id,trigger_json FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],)) if row['id'] not in before]
        self.assertEqual(len(planner),1)
        self.assertEqual(json.loads(planner[0]['trigger_json'])['task_id'],'first')

    def test_ready_descendant_with_paused_parent_does_not_deadlock_repair_planning(self):
        self.tick();self.propose({'proposal_id':'ready-repair','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first'),script('child')],
            'add_dependencies':[{'upstream':'first','downstream':'child'}],
            'sealed':True,'replan_after':['child']})
        run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE run_tasks SET status='ready' WHERE run_id=? AND id='child'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        self.store.execute("UPDATE planner_runs SET status='rejected' WHERE run_id=? AND status='pending'",(run['id'],))
        self.store.execute("UPDATE control_jobs SET status='cancelled' WHERE run_id=? AND control_kind='PLAN' AND status='ready'",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.open_repair(run,'first','task_review','review-id','quality_failed')
        self.orchestrator.advance(graph.get(self.store,run['id']))
        planner=self.store.query("SELECT trigger_json FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],))
        self.assertEqual(len(planner),1);self.assertEqual(json.loads(planner[0]['trigger_json'])['task_id'],'first')

    def test_liveness_guard_surfaces_unknown_active_stall(self):
        self.store.execute("UPDATE planner_runs SET status='rejected' WHERE run_id=?",(self.run['id'],))
        self.store.execute("UPDATE control_jobs SET status='cancelled' WHERE run_id=?",(self.run['id'],))
        self.store.execute("UPDATE runs SET status='ACTIVE',sealed=0,terminal_reason='' WHERE id=?",(self.run['id'],))
        self.orchestrator.ensure_liveness(graph.get(self.store,self.run['id']))
        current=graph.get(self.store,self.run['id'])
        self.assertEqual((current['status'],current['terminal_reason']),('WAITING_USER','workflow_stalled'))
        event=self.store.query("SELECT payload_json FROM workflow_graph_events WHERE run_id=? AND kind='workflow_stalled'",(self.run['id'],))
        self.assertEqual(len(event),1);self.assertIn('repair_context',json.loads(event[0]['payload_json']))
        from webapp.workflow_api import public
        shown=public(self.store,current)['blocking_action']
        self.assertEqual((shown['code'],shown['title']),('workflow_stalled','工作流无法继续推进'))
        self.assertIn('无需猜测补充信息',shown['message'])

    def test_liveness_guard_preserves_worker_recovery_wait(self):
        self.store.execute("UPDATE runs SET status='ACTIVE',terminal_reason='workers_unavailable' WHERE id=?",(self.run['id'],))
        self.orchestrator.ensure_liveness(graph.get(self.store,self.run['id']))
        current=graph.get(self.store,self.run['id'])
        self.assertEqual((current['status'],current['terminal_reason']),('ACTIVE','workers_unavailable'))

    def test_inconclusive_step_review_preserves_candidate_and_requests_evidence(self):
        run=self.review_fixture();self.orchestrator.queue_review(run,'first')
        current=graph.get(self.store,run['id']);review=next(t for t in graph.work_items(current) if t.get('control_kind') and t['spec'].get('profile')=='visual_quality_reviewer')
        self.orchestrator.accept_review(current,review,{'decision':{
            'snapshot_digest':review['spec']['snapshot_digest'],'verdict':'inconclusive',
            'feedback':'The candidate has no confirmed defect, but deterministic evidence is incomplete.',
            'missing_evidence':[{'fact':'Inspect the candidate pixels','mode':'image'}]}})
        current=graph.get(self.store,run['id']);subject=next(t for t in current['tasks'] if t['id']=='first')
        self.assertEqual((subject['status'],subject['error_code']),('paused','review_incomplete'))
        self.assertEqual(subject['candidate_result'],['edit/report.txt'])
        self.assertEqual(current['repair_context']['source'],'review_evidence')
        planner=self.store.query("SELECT trigger_json FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],))[0]
        self.assertIn('review_incomplete',json.loads(planner['trigger_json']))

    def test_exhausted_step_review_stops_with_feedback_without_impossible_planning(self):
        run=self.review_fixture()
        self.store.execute("UPDATE run_tasks SET review_again_count=? WHERE run_id=? AND id='first'",
                           (graph.CANDIDATE_REVIEW_LIMIT,run['id']))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run,'first')
        current=graph.get(self.store,run['id'])
        review=next(t for t in graph.work_items(current) if t.get('control_kind')=='REVIEW')
        feedback='The two bounded evidence rounds still cannot distinguish the source interval.'
        self.orchestrator.accept_review(current,review,{'decision':{
            'snapshot_digest':review['spec']['snapshot_digest'],'verdict':'inconclusive','feedback':feedback,
            'missing_evidence':[{'fact':'Compare one more source interval','mode':'image'}]}})
        current=graph.get(self.store,run['id'])
        self.assertEqual((current['status'],current['terminal_reason']),('WAITING_USER','review_incomplete'))
        self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],)))
        from webapp.workflow_api import public
        self.assertIn(feedback,public(self.store,current)['blocking_action']['message'])

    def test_revised_upstream_can_wait_on_fresh_revision_of_paused_descendant(self):
        self.tick();self.propose({'proposal_id':'paused-chain','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first'),script('child'),script('unrelated')],
            'add_dependencies':[{'upstream':'first','downstream':'child'}],'sealed':True})
        run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed' WHERE run_id=?",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        planning.queue(self.store,graph.get(self.store,run['id']),{'task_id':'first','repair_policy':'manager_revision'})
        self.propose({'proposal_id':'repair-chain','base_graph_version':1,'reason':'Repair source of downstream defect',
            'revise_tasks':[script('first',argv=['python','-c','print("fixed")'])],
            'sealed':False,'replan_after':['child']})
        run=self.tick();tasks={t['id']:t for t in run['tasks']}
        self.assertEqual(run['replan_after'],['child'])
        self.assertEqual((tasks['first']['revision'],tasks['first']['status']),(1,'ready'))
        self.assertEqual((tasks['child']['revision'],tasks['child']['status']),(1,'blocked'))
        self.assertEqual((tasks['unrelated']['revision'],tasks['unrelated']['status']),(0,'paused'))

    def test_video_operation_replacement_preserves_selected_roles_without_requiring_audio_only_role(self):
        self.authorization['allowed_operations'].append('media.edit')
        encoded=graph.encode(planning.bind_scope(self.store,self.authorization).model_dump())
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(encoded,self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(encoded,self.run['id']))
        source={'id':'edit','type':'operation','operation':'media.edit','name':'edit','objective':'Create the requested video',
                'inputs':{'source':{'asset':'asset'}},'parameters':{'edit_plan':{'clips':[{'asset':'source','end':1}]}},
                'acceptance':[{'kind':'file'}],'basic_reason':'Local media integrity','delivery_stage':True}
        self.tick();self.propose({'proposal_id':'initial-edit','base_graph_version':0,'reason':'Initial edit','add_tasks':[source],'sealed':True})
        run=self.tick()
        self.assertTrue(run['tasks'],{'status':run['status'],'terminal_reason':run['terminal_reason'],
            'diagnostics':self.store.query('SELECT record FROM workflow_diagnostics WHERE workflow=?',(run['id'],))})
        current=run['tasks'][0]['spec']
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed' WHERE run_id=? AND id='edit'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        planning.queue(self.store,graph.get(self.store,run['id']),{'task_id':'edit','repair_policy':'manager_revision'})
        replacement=script('fixed',inputs={'source':{'asset':'asset'}},outputs=['final.mp4','plan.json','report.json'],
                           output_roles={'video':'final.mp4','plan':'plan.json','report':'report.json'})
        replacement.update({key:current[key] for key in ('objective','acceptance','semantics','delivery_stage')})
        invalid={**replacement,'output_roles':{'video':'final.mp4','report':'report.json'}}
        proposal={'proposal_id':'replacement','base_graph_version':1,'reason':'Repair implementation','add_tasks':[invalid],
                  'replacements':{'edit':'fixed'},'sealed':True}
        with self.assertRaisesRegex(ValueError,'required output role: plan'):
            graph.validate_proposal(self.store,graph.get(self.store,run['id']),proposal,repair_scope='edit')
        self.propose({**proposal,'add_tasks':[replacement]})
        repaired=self.tick();self.assertEqual(next(t for t in repaired['tasks'] if t['id']=='fixed')['status'],'ready')

    def test_repair_revision_invalidates_descendants_and_preserves_unrelated_branch(self):
        self.tick();self.propose({'proposal_id':'initial-repair','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first'),script('child'),script('unrelated')],
            'add_dependencies':[{'upstream':'first','downstream':'child'}],'sealed':True})
        run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='execution_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id IN ('child','unrelated')",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        planning.queue(self.store,graph.get(self.store,run['id']),{'task_id':'first','repair_policy':'manager_revision'})
        replacement=script('first',argv=['python','-c','print("repaired")'])
        self.propose({'proposal_id':'repair-first','base_graph_version':1,'reason':'replace failed local implementation',
            'revise_tasks':[replacement],'sealed':True})
        repaired=self.tick();tasks={task['id']:task for task in graph.work_items(repaired) if not task.get('control_kind')}
        self.assertEqual((tasks['first']['revision'],tasks['first']['repair_count'],tasks['first']['status']),(1,1,'ready'))
        self.assertEqual((tasks['child']['revision'],tasks['child']['repair_count'],tasks['child']['status']),(1,0,'blocked'))
        self.assertEqual((tasks['unrelated']['revision'],tasks['unrelated']['repair_count'],tasks['unrelated']['status']),(0,0,'succeeded'))
        history=self.store.query('SELECT task_id,revision,repair_root FROM workflow_task_revisions WHERE run_id=? ORDER BY task_id,revision',(run['id'],))
        self.assertEqual([(row['task_id'],row['revision'],row['repair_root']) for row in history],
            [('child',0,0),('child',1,0),('first',0,0),('first',1,1),('unrelated',0,0)])
        selected=self.store.query('SELECT task_id,task_revision FROM workflow_plan_tasks WHERE run_id=? AND graph_version=2 ORDER BY task_id',(run['id'],))
        self.assertEqual([(row['task_id'],row['task_revision']) for row in selected],[('child',1),('first',1),('unrelated',0)])

    def test_revision_and_new_descendant_commit_atomically_without_revising_new_task(self):
        self.tick();self.propose({'proposal_id':'mixed-initial','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first'),script('unrelated')],'sealed':True})
        run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='execution_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='unrelated'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        planning.queue(self.store,graph.get(self.store,run['id']),{'task_id':'first'})
        self.propose({'proposal_id':'mixed-repair','base_graph_version':1,'reason':'repair and add dependent',
            'revise_tasks':[script('first',argv=['python','-c','print("fixed")'])],
            'add_tasks':[script('new_child',inputs={'source':{'step':'first','role':'report'}})],
            'add_dependencies':[{'upstream':'first','downstream':'new_child'}],'sealed':True})
        result=self.tick();tasks={t['id']:t for t in result['tasks']}
        self.assertEqual(result['graph_version'],2)
        self.assertEqual(tasks['first']['revision'],1)
        self.assertEqual((tasks['new_child']['revision'],tasks['new_child']['status']),(0,'blocked'))
        self.assertEqual((tasks['unrelated']['revision'],tasks['unrelated']['status']),(0,'succeeded'))
        history=self.store.query("SELECT revision FROM workflow_task_revisions WHERE run_id=? AND task_id='new_child'",(run['id'],))
        self.assertEqual([row['revision'] for row in history],[0])
        self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE run_id=? AND status='staged'",(run['id'],)))

    def test_repair_context_survives_an_intermediate_diagnostic_planning_round(self):
        self.tick();self.propose({'proposal_id':'initial-durable-repair','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first'),script('child')],'add_dependencies':[{'upstream':'first','downstream':'child'}],
            'sealed':True});run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.open_repair(run,'first','task_review','review-id','quality_failed')
        planning.queue(self.store,graph.get(self.store,run['id']),{'task_id':'first','repair_policy':'manager_revision'})
        self.propose({'proposal_id':'diagnose-before-repair','base_graph_version':1,'reason':'collect bounded evidence',
            'add_tasks':[script('diagnosis')],'replan_after':['diagnosis']})
        intermediate=self.tick();self.assertTrue(intermediate['repair_context']['active'])
        diagnosis=next(task for task in intermediate['tasks'] if task['id']=='diagnosis')
        self.assertEqual(diagnosis['spec']['purpose'],'business')
        from webapp.workflow_dispatch import candidates
        self.assertIn('diagnosis',{row['step_id'] for row in candidates(self.store)})
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='diagnosis'",(run['id'],))
        self.tick()
        replacement=script('first',argv=['python','-c','print("repaired after diagnosis")'])
        self.propose({'proposal_id':'repair-after-diagnosis','base_graph_version':2,'reason':'apply diagnosed repair',
            'revise_tasks':[replacement],'sealed':True})
        repaired=self.tick();tasks={task['id']:task for task in graph.work_items(repaired) if not task.get('control_kind')}
        self.assertEqual((tasks['first']['revision'],tasks['child']['revision']),(1,1))
        self.assertEqual(repaired['repair_context'],{})

    def test_sealed_repair_cannot_abandon_a_paused_required_task(self):
        self.tick();self.propose({'proposal_id':'initial-required','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first')],'sealed':True});run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.open_repair(run,'first','task_review','review-id','quality_failed')
        with self.assertRaisesRegex(ValueError,'resolve paused tasks'):
            graph.validate_proposal(self.store,graph.get(self.store,run['id']),{'proposal_id':'abandon-paused','base_graph_version':1,
                'reason':'add a replacement without repairing the required task','add_tasks':[script('replacement')],'sealed':True},repair_scope='first')

    def test_open_repair_cannot_wait_on_the_unchanged_paused_subject(self):
        self.tick();self.propose({'proposal_id':'initial-paused-trigger','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first')],'sealed':True});run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='review_incomplete' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0 WHERE id=?",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.open_repair(run,'first','review_evidence','review-id','review_incomplete')
        with self.assertRaisesRegex(ValueError,'cannot wait on an unresolved'):
            graph.validate_proposal(self.store,graph.get(self.store,run['id']),{
                'proposal_id':'wait-paused','base_graph_version':1,'reason':'invalid wait',
                'replan_after':['first'],'sealed':False},repair_scope='first')

    def test_rejected_final_review_is_visible_in_the_next_frozen_planning_input(self):
        run=self.review_fixture()
        self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=? AND id='first'",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run)
        current=graph.get(self.store,run['id']);review=next(t for t in graph.work_items(current) if t.get('control_kind') and t['spec'].get('profile')=='delivery_reviewer')
        self.orchestrator.accept_review(current,review,{'decision':{'snapshot_digest':review['spec']['snapshot_digest'],
            'verdict':'reject','feedback':'Required ending is missing.'}})
        row=self.store.query("SELECT snapshot_json FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],))[0]
        frozen=json.loads(row['snapshot_json'])
        self.assertEqual(frozen['trigger']['decision']['feedback'],'Required ending is missing.')
        self.assertEqual(frozen['review_feedback'][0]['decision']['feedback'],'Required ending is missing.')
        self.assertIsNone(graph.get(self.store,run['id'])['delivered_at'])
        self.store.execute("""INSERT INTO workflow_reviews
            SELECT ?,run_id,?,purpose,subject_task,graph_version+1,?,snapshot_json,snapshot_digest,
                   'passed',decision_json,created_at+1,updated_at+1
            FROM workflow_reviews WHERE run_id=? AND status='rejected'""",
            ('f'*32,'passed-review','passed-identity',run['id']))
        from webapp.workflow_planning import snapshot
        self.assertEqual(snapshot(self.store,graph.get(self.store,run['id']))['review_feedback'],[])

    def test_inconclusive_final_review_requests_evidence_without_rerendering_delivery(self):
        run=self.review_fixture()
        self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=? AND id='first'",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run)
        current=graph.get(self.store,run['id']);review=next(t for t in graph.work_items(current) if t.get('control_kind') and t['spec'].get('profile')=='delivery_reviewer')
        self.orchestrator.accept_review(current,review,{'decision':{'snapshot_digest':review['spec']['snapshot_digest'],
            'verdict':'inconclusive','evidence':[],'deliverables':[],'primary':'','feedback':'Need denser pixel frame evidence.',
            'missing_evidence':[{'fact':'Inspect denser candidate pixels','mode':'image'}]}})
        updated=graph.get(self.store,run['id'])
        self.assertEqual((updated['status'],updated['terminal_reason']),('ACTIVE','evidence_planning'))
        self.assertEqual(updated['repair_context']['error_code'],'review_incomplete')
        request=self.store.query("SELECT snapshot_json FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],))[0]
        from webapp.workflow_model_protocol import repair_options
        options=repair_options(json.loads(request['snapshot_json']))
        self.assertIn('first',{item['task_id'] for item in options if item['action']=='collect_delivery_evidence'})
        subject=next(task for task in updated['tasks'] if task['id']=='first')
        self.assertEqual((subject['revision'],subject['status']),(0,'succeeded'))

    def test_successor_applies_consumed_inputs_and_keeps_late_inputs_pending(self):
        from tests.run_input_fixture import accept_change as accept
        first=accept(self.store,self.run['id'],'owner',{'request_id':'1'*32,'content':'New overall goal'})
        second=accept(self.store,self.run['id'],'owner',{'request_id':'2'*32,'content':'Late planning detail'})
        authorization={**self.authorization,'goal':'New overall goal'}
        request={'new_goal':'New overall goal','authorization':authorization,'input_through_sequence':first['sequence']}
        self.orchestrator.decision(graph.get(self.store,self.run['id']),'new_run',request)
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE kind='new_run'")[0]
        self.orchestrator.answer(graph.get(self.store,self.run['id']),{'decision_id':decision['id'],
            'snapshot_digest':decision['snapshot_digest'],'action':'approve','authorization':authorization})
        successor=graph.get(self.store,self.store.query('SELECT id FROM runs WHERE parent_run_id=?',(self.run['id'],))[0]['id'])
        carried=self.store.query('SELECT sequence,status,applied_graph_version,context_json FROM run_inputs WHERE run_id=? ORDER BY sequence',(successor['id'],))
        self.assertEqual([(row['sequence'],row['status']) for row in carried],[(1,'APPLIED'),(2,'ACCEPTED')])
        self.assertEqual(carried[0]['applied_graph_version'],0)
        self.assertEqual(successor['input_barrier_sequence'],2)
        event=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE run_id=? AND kind='input_batch_applied'",(successor['id'],))[0]['payload_json'])
        self.assertEqual(event,{'through_sequence':1,'next_barrier_sequence':2})
        original=self.store.query('SELECT status,successor_run_id FROM run_inputs WHERE run_id=? ORDER BY sequence',(self.run['id'],))
        self.assertTrue(all(row['status']=='SUPERSEDED' and row['successor_run_id']==successor['id'] for row in original))

    def test_user_rejecting_quality_also_pauses_the_subject(self):
        run=self.review_fixture();self.orchestrator.decision(run,'quality',{'task_id':'first','revision':0})
        decision=self.store.query('SELECT * FROM workflow_decisions')[0]
        self.orchestrator.answer(graph.get(self.store,run['id']),{'decision_id':decision['id'],
            'snapshot_digest':decision['snapshot_digest'],'action':'reject'})
        subject=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']=='first')
        self.assertEqual(subject['status'],'paused');self.assertEqual(subject['error_code'],'quality_failed')

    def test_error_pause_keeps_its_reason_without_a_user_pause_command(self):
        self.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='budget_exhausted' WHERE id=?",(self.run['id'],))
        self.assertEqual(self.tick()['terminal_reason'],'budget_exhausted')
        self.assertEqual(self.tick()['terminal_reason'],'budget_exhausted')

    def test_resume_from_user_pause_returns_to_pending_confirmation_without_dispatch(self):
        run=self.review_fixture();self.orchestrator.decision(run,'quality',{'task_id':'first','revision':0})
        graph.command(self.store,run['id'],'owner','pause');self.assertEqual(self.tick()['status'],'WAITING_USER')
        graph.command(self.store,run['id'],'owner','resume');self.assertEqual(self.tick()['status'],'WAITING_USER')
        self.assertEqual(self.store.query("SELECT status FROM workflow_commands WHERE action='resume'")[0]['status'],'applied')
        from webapp.workflow_dispatch import candidates
        self.assertFalse(candidates(self.store))

    def test_resume_cannot_loop_a_state_that_requires_replanning_or_a_new_run(self):
        self.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='model_token_budget_exhausted' WHERE id=?",(self.run['id'],))
        graph.command(self.store,self.run['id'],'owner','resume',request_id='e'*32)
        current=self.tick();receipt=self.store.query("SELECT status,error_code FROM workflow_commands WHERE id=?",('e'*32,))[0]
        self.assertEqual(receipt,{'status':'rejected','error_code':'replan_required'})
        self.assertEqual((current['status'],current['terminal_reason']),('WAITING_USER','model_token_budget_exhausted'))

    def test_parallel_manual_reviews_are_presented_after_each_prior_confirmation(self):
        self.tick();self.propose({'proposal_id':'manual-two','base_graph_version':0,'reason':'Two independent candidates',
            'add_tasks':[script(name,acceptance=[{'kind':'file'},{'kind':'review','criterion':'User content confirmation'}]) for name in ('first','second')],'sealed':True});self.tick()
        self.store.execute("UPDATE run_tasks SET status='reviewing',phase='validate',candidate_result_json=?,quality_json=? WHERE run_id=?",
            (graph.encode(['edit/report.txt']),graph.encode({'technical_valid':True,'passed':True,'requires_review':True}),self.run['id']))
        run=graph.get(self.store,self.run['id']);self.orchestrator.advance(run)
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]
        first=json.loads(decision['request_json'])['task_id']
        self.orchestrator.answer(graph.get(self.store,run['id']),{'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'approve'})
        self.orchestrator.advance(graph.get(self.store,run['id']))
        pending=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")
        self.assertEqual(len(pending),1)
        self.assertNotEqual(json.loads(pending[0]['request_json'])['task_id'],first)
        self.assertEqual(graph.get(self.store,run['id'])['status'],'WAITING_USER')
        self.assertIsNone(graph.get(self.store,run['id'])['delivered_at'])

    def test_agent_and_user_criteria_both_hold_before_accepting_the_candidate(self):
        run=self.review_fixture(user_review=True)
        self.store.execute('UPDATE run_tasks SET quality_json=? WHERE run_id=? AND id=?',
            (graph.encode({'technical_valid':True,'passed':True,'requires_review':True,'requires_agent':True}),run['id'],'first'))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run,'first');self.orchestrator.advance(run)
        self.assertFalse(self.store.query('SELECT id FROM workflow_decisions'))
        current=graph.get(self.store,run['id']);review=next(t for t in graph.work_items(current) if t.get('control_kind') and t['spec'].get('profile')=='visual_quality_reviewer')
        self.store.execute('INSERT INTO workflow_inspections VALUES (?,?,?,?,?,?,?,?)',('d'*32,run['id'],review['id'],review['lease_id'],'edit/report.txt',review['spec']['snapshot_digest'],'{}',self.store.now()))
        self.orchestrator.accept_review(current,review,{'decision':{'snapshot_digest':review['spec']['snapshot_digest'],'verdict':'pass',
            'evidence':[{'criterion':'满足当前步骤目标及输出契约','method_use':'not_applicable','observation':'Checked actual candidate','ref':'edit/report.txt','inspection_id':'d'*32}]}})
        subject=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']=='first')
        self.assertEqual(subject['status'],'reviewing');self.assertFalse(subject['accepted_result'])
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]
        self.orchestrator.answer(graph.get(self.store,run['id']),{'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'approve'})
        subject=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']=='first')
        self.assertEqual(subject['status'],'succeeded');self.assertEqual(subject['accepted_result'],['edit/report.txt'])

    def test_stale_agent_pass_cannot_bypass_an_outstanding_review(self):
        run=self.review_fixture(user_review=True)
        quality={'technical_valid':True,'passed':True,'requires_review':True,'requires_agent':True,'agent_review_passed':True}
        self.store.execute('UPDATE run_tasks SET quality_json=? WHERE run_id=? AND id=?',
                           (graph.encode(quality),run['id'],'first'))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run,'first')
        self.orchestrator.advance(graph.get(self.store,run['id']))
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE status='pending'"))
        # Simulate a previously issued decision while the new agent review is
        # still pending: the approval boundary independently checks that fact.
        self.orchestrator.decision(graph.get(self.store,run['id']),'quality',{'task_id':'first','revision':0,'quality':quality})
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]
        with self.assertRaisesRegex(ValueError,'review is still pending'):
            self.orchestrator.answer(graph.get(self.store,run['id']),{
                'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'approve'})
        task=graph.get(self.store,run['id'])['tasks'][0]
        self.assertEqual(task['status'],'reviewing');self.assertEqual(task['accepted_result'],[])
        self.assertEqual(self.store.query("SELECT status FROM workflow_reviews WHERE subject_task='first'")[0]['status'],'pending')

    def test_scope_approval_uses_worker_implementation_without_controller_credentials(self):
        from unittest.mock import patch
        from webapp.operations import get
        worker_operation=get('image.generate_from_text','image_sync@1').public()
        raw={**self.authorization,'allowed_operations':['image.generate_from_text']}
        with patch.dict('os.environ',{'VIDEO_USE_STORAGE_METADATA_ONLY':'1'},clear=True), \
             patch('webapp.workflow_planning.available_operations',return_value=[worker_operation]):
            bound=planning.bind_scope(self.store,raw)
        self.assertEqual(bound.operation_implementations,{'image.generate_from_text':'image_sync@1'})
        with patch('webapp.workflow_planning.available_operations',return_value=None),self.assertRaises(ValueError):
            planning.bind_scope(self.store,raw)

    def test_malformed_decision_command_cannot_block_later_cancellation(self):
        bad=graph.command(self.store,self.run['id'],'owner','answer',{'decision_id':{},'action':'approve'})
        graph.command(self.store,self.run['id'],'owner','cancel')
        run=self.tick();self.assertEqual(run['status'],'CANCELLED')
        row=self.store.query('SELECT status,error_code FROM workflow_commands WHERE id=?',(bad['id'],))[0]
        self.assertEqual(row['status'],'rejected');self.assertEqual(row['error_code'],'invalid_or_stale_decision')

    def test_rejected_proposal_id_cannot_turn_a_new_request_into_database_failure(self):
        self.tick()
        self.propose({'proposal_id':'reused-id','base_graph_version':0,'reason':'cycle','add_tasks':[script('a'),script('b')],
          'add_dependencies':[{'upstream':'a','downstream':'b'},{'upstream':'b','downstream':'a'}],'sealed':True})
        self.tick()
        request=self.store.query("SELECT * FROM planner_runs WHERE status='pending'")[0]
        from webapp.task_leases import Coordinator
        lease=Coordinator(self.store,'new-plan','test').claim(self.f.turn,'workflow',step_id=request['task_id'])
        try:
            with lease.bind(),self.assertRaises(planning.ProposalIdentityError):
                planning.stage(self.store,lease,request['id'],envelope(self.store,request['id'],{'proposal_id':'reused-id','base_graph_version':0,'reason':'corrected','add_tasks':[script()],'sealed':True}))
            self.assertEqual(self.store.query('SELECT status FROM planner_runs WHERE id=?',(request['id'],))[0]['status'],'running')
            frozen=json.loads(request['snapshot_json']);self.assertIn('reused-id',frozen['used_proposal_ids'])
            self.assertEqual(frozen['planning_request_id'],request['id'])
        finally:lease.release()
