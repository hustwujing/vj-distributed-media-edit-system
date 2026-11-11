import json
from tests.run_input_fixture import accept_change
import uuid
import unittest
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph,workflow_planning as planning,run_inputs
from webapp.task_leases import Coordinator


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.run=self.f.run
        authorization={**self.run['authorization'],'require_plan_confirmation':True,'delivery_targets':[]}
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(graph.encode(authorization),self.run['id']))

    def stage(self,kind='APPLY_PLAN',targets=None,review_policy=None,delivery_contract=None):
        self.f.tick()
        request=self.store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        lease=Coordinator(self.store,'conversation-test','test').claim(self.run['id'],'workflow',step_id=request['task_id'])
        self.assertIsNotNone(lease)
        proposal=None if kind!='APPLY_PLAN' else {'proposal_id':request['id'],'base_graph_version':0,'reason':'Write approved report','add_tasks':[fixtures.script()],'replan_after':['first']}
        reply=fixtures.envelope(self.store,request['id'],proposal,kind=kind,
            delivery_targets=['report.txt'] if targets is None else targets)
        if review_policy is not None:reply['review_policy']=review_policy
        if delivery_contract is not None:reply['delivery_contract']=delivery_contract
        reply['reason']='上一轮成果仍可查看；本次仅回答，没有重新处理素材。' if kind=='RESPOND' else '生成报告，确认后执行'
        try:
            with lease.bind():planning.stage(self.store,lease,request['id'],reply)
        finally:lease.release()
        return self.f.tick()

    def decision(self):return self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
    def answer(self,decision,action,**extra):
        return self.f.orchestrator.answer(graph.get(self.store,self.run['id']),
            {'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':action,**extra},
            request_id=uuid.uuid4().hex,actor='owner')

    def test_plan_creates_no_business_work_until_versioned_approval_and_fills_targets(self):
        from webapp.workflow_history import plan_confirmation_evidence
        run=self.stage();self.assertEqual(run['status'],'WAITING_USER');self.assertEqual(run['tasks'],[])
        self.assertEqual(plan_confirmation_evidence(self.store,run)['approved_plans'],[])
        decision=self.decision();self.assertEqual(decision['kind'],'plan_confirmation')
        self.assertEqual(json.loads(decision['request_json'])['delivery_targets'],['report.txt'])
        self.assertIsNone(Coordinator(self.store,'business-test','test').claim(run['id'],'workflow',step_id='first'))
        self.answer(decision,'approve')
        run=graph.get(self.store,run['id'])
        self.assertEqual(run['graph_version'],1)
        self.assertEqual(run['authorization']['delivery_targets'],['report.txt'])
        self.assertTrue(run['authorization']['require_plan_confirmation'])
        self.assertEqual(len(run['tasks']),1)
        self.assertEqual(run['tasks'][0]['authorization_version'],run['authorization_version'])
        proof=plan_confirmation_evidence(self.store,run)
        self.assertEqual(proof['approved_plans'][0]['decision_id'],decision['id'])
        self.assertEqual(proof['approved_plans'][0]['delivery_targets'],['report.txt'])
        self.f.orchestrator.queue_review(run,subject='first')
        self.f.orchestrator.queue_review(run)
        for row in self.store.query('SELECT snapshot_json FROM workflow_reviews WHERE run_id=?',(run['id'],)):
            self.assertEqual(json.loads(row['snapshot_json'])['plan_confirmation_evidence'],proof)
        with self.assertRaises(ValueError):self.answer(decision,'approve')

    def test_manager_proposes_and_confirmation_freezes_publication_checkpoint_policy(self):
        policy={'technical_review':'required','user_checkpoint':{
            'enabled':True,'stage':'before_publish','reason':'请在正式发布前确认最终报告'}}
        run=self.stage(review_policy=policy);decision=self.decision()
        request=json.loads(decision['request_json'])
        self.assertEqual(request['review_policy'],policy)
        self.assertNotIn('review',{item['kind'] for item in request['steps'][0]['acceptance']})
        self.answer(decision,'approve');run=graph.get(self.store,run['id'])
        self.assertEqual(run['authorization']['review_policy'],policy)
        self.assertGreater(run['authorization_version'],1)

    def test_checkpoint_policy_revision_preserves_an_existing_delivery_list(self):
        existing={**graph.get(self.store,self.run['id'])['authorization'],'delivery_targets':['report.txt']}
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(existing),self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(graph.encode(existing),self.run['id']))
        accept_change(self.store,self.run['id'],'owner',{'request_id':uuid.uuid4().hex,
            'content':'这次需要在正式发布前由我确认最终成果'})
        policy={'technical_review':'required','user_checkpoint':{
            'enabled':True,'stage':'before_publish','reason':'正式发布前确认最终成果'}}
        self.stage(targets=[],review_policy=policy);self.answer(self.decision(),'approve')
        run=graph.get(self.store,self.run['id'])
        self.assertEqual(run['authorization']['delivery_targets'],['report.txt'])
        self.assertEqual(run['authorization']['review_policy'],policy)

    def test_chat_revision_invalidates_confirmation_and_preserves_both_inputs(self):
        self.stage();old=self.decision()
        accept_change(self.store,self.run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'先改变报告的标题，不要执行旧方案'})
        self.assertEqual(self.store.query('SELECT status FROM workflow_decisions WHERE id=?',(old['id'],))[0]['status'],'superseded')
        with self.assertRaises(ValueError):self.answer(old,'approve')
        run=self.f.tick();self.assertEqual(run['tasks'],[])
        pending=self.store.query("SELECT snapshot_json FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],))
        self.assertIn('不要执行旧方案',pending[0]['snapshot_json'])
        self.assertIn('superseded_plan',pending[0]['snapshot_json'])
        self.assertIn('report.txt',pending[0]['snapshot_json'])

    def test_reject_plan_does_not_replan_without_new_input(self):
        self.stage();self.answer(self.decision(),'reject')
        from webapp.workflow_history import plan_confirmation_evidence
        self.assertEqual(plan_confirmation_evidence(self.store,self.run)['approved_plans'],[])
        count=len(self.store.query('SELECT id FROM planner_runs WHERE run_id=?',(self.run['id'],)))
        for _ in range(3):run=self.f.tick()
        self.assertEqual((run['status'],run['terminal_reason']),('WAITING_USER','user_rejected'))
        self.assertEqual(len(self.store.query('SELECT id FROM planner_runs WHERE run_id=?',(run['id'],))),count)
        self.assertEqual(run['tasks'],[])
        command=graph.command(self.store,run['id'],'owner','resume',request_id=uuid.uuid4().hex)
        self.f.tick()
        receipt=self.store.query('SELECT status,error_code FROM workflow_commands WHERE id=?',(command['id'],))[0]
        self.assertEqual(receipt,{'status':'rejected','error_code':'replan_required'})

    def test_changed_assets_reject_old_approval_without_partial_grant(self):
        self.stage();decision=self.decision()
        self.store.add_asset(self.run['project_id'],'new-asset','new.png','new.png',{'kind':'image','width':2,'height':2})
        with self.assertRaises(ValueError):self.answer(decision,'approve')
        run=graph.get(self.store,self.run['id'])
        self.assertEqual(run['tasks'],[]);self.assertEqual(run['authorization_version'],1)
        self.assertEqual(self.decision()['id'],decision['id'])

    def test_remote_parameter_failure_is_not_retried_as_environment_failure(self):
        self.stage();self.answer(self.decision(),'approve')
        run_id=self.run['id']
        self.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='execution_failed' WHERE id=?",(run_id,))
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='execution_failed',error_detail=? WHERE run_id=? AND id='first'",
                           ('OpenSandbox command operation failed: transcript binding is missing',run_id))
        self.store.execute('INSERT INTO workflow_diagnostics(id,workflow,step,revision,attempt,record,created) VALUES(?,?,?,?,?,?,?)',
                           (uuid.uuid4().hex,run_id,'first',0,1,graph.encode({'classification':'parameters','error_type':'PlanError',
                            'message':'Transcript subtitles require declared words'}),self.store.now()))
        from webapp.workflow_api import public
        shown=public(self.store,graph.get(self.store,run_id))
        self.assertEqual(shown['blocking_action']['action'],'replan')
        self.assertIn('transcript binding is missing',shown['blocking_action']['message'])
        command=graph.command(self.store,run_id,'owner','resume',request_id=uuid.uuid4().hex)
        self.f.orchestrator.commands(graph.get(self.store,run_id))
        receipt=self.store.query('SELECT status,error_code FROM workflow_commands WHERE id=?',(command['id'],))[0]
        self.assertEqual(receipt,{'status':'rejected','error_code':'replan_required'})
        self.assertEqual(graph.get(self.store,run_id)['tasks'][0]['status'],'paused')

    def test_new_input_during_replanning_is_included_before_confirmation(self):
        self.stage()
        accept_change(self.store,self.run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'先修改标题'})
        self.f.tick()
        request=self.store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        lease=Coordinator(self.store,'race-test','test').claim(self.run['id'],'workflow',step_id=request['task_id'])
        proposal={'proposal_id':request['id'],'base_graph_version':0,'reason':'Revised report','add_tasks':[fixtures.script()],'replan_after':['first']}
        try:
            with lease.bind():planning.stage(self.store,lease,request['id'],fixtures.envelope(self.store,request['id'],proposal,delivery_targets=['report.txt']))
        finally:lease.release()
        accept_change(self.store,self.run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'还要增加结论'})
        run=self.f.tick();self.assertEqual(run['tasks'],[])
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],)))
        pending=self.store.query("SELECT snapshot_json FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],))[0]
        batch=json.loads(pending['snapshot_json'])['input_batch'];self.assertEqual(batch['through_sequence'],2)
        self.assertEqual([item['content'] for item in batch['inputs']],['先修改标题','还要增加结论'])

    def test_respond_finishes_only_conversation_and_accepts_subsequent_questions(self):
        run=self.stage('RESPOND',targets=[])
        self.assertEqual((run['status'],run['terminal_reason']),('SUCCEEDED','conversation_answer'))
        self.assertEqual(run['tasks'],[]);self.assertEqual(run['delivery'],[])
        result=accept_change(self.store,run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'你刚才说了什么？'})
        successor=graph.get(self.store,result['run_id']);self.assertFalse(successor['scope_confirmed'])
        from webapp.workflow_conversation import context
        facts=context(self.store,successor)
        self.assertTrue(any('没有重新处理素材' in item['content'] and item['role']=='assistant'
                            for item in facts['memory']['messages_after_cursor']))

    def test_respond_cannot_bypass_business_review(self):
        self.stage();self.answer(self.decision(),'approve')
        from webapp.workflow_conversation import respond
        from types import SimpleNamespace
        with self.assertRaisesRegex(ValueError,'business work'):
            respond(self.store,graph.get(self.store,self.run['id']),{'id':'fake'},SimpleNamespace(reason='done',input_through_sequence=0))

    def test_parent_context_is_same_project_and_read_only(self):
        from webapp.workflow_conversation import context
        original=self.run
        self.store.execute("UPDATE runs SET status='SUCCEEDED',delivered_at=? WHERE id=?",(self.store.now(),original['id']))
        successor=graph.create(self.store,original['project_id'],None,{**original['authorization'],'goal':'结果在哪里'},parent_run_id=original['id'],scope_confirmed=False)
        facts=context(self.store,successor)
        self.assertEqual(facts['previous_runs'][0]['goal'],original['goal'])
        self.assertEqual(facts['previous_runs'][0]['delivery'],[])
        foreign=self.store.add_project('foreign','private project')['id']
        self.store.execute('UPDATE runs SET project_id=? WHERE id=?',(foreign,original['id']))
        self.assertEqual(context(self.store,successor)['previous_runs'],[])

    def test_plain_chat_answers_question_without_granting_permissions(self):
        self.f.orchestrator.decision(graph.get(self.store,self.run['id']),'input_question',{'question':'需要怎样的报告？','input_through_sequence':0})
        old=self.decision()
        accepted=accept_change(self.store,self.run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'只是问上一轮的结果在哪里'})
        self.assertEqual(self.store.query('SELECT status FROM workflow_decisions WHERE id=?',(old['id'],))[0]['status'],'superseded')
        self.assertEqual(accepted['context']['responding_to']['decision_id'],old['id'])
        self.assertEqual(graph.get(self.store,self.run['id'])['authorization_version'],1)

    def test_reject_empty_clarification_does_not_generate_same_question(self):
        accept_change(self.store,self.run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'停止这个方向'})
        self.f.orchestrator.decision(graph.get(self.store,self.run['id']),'input_question',{'question':'想做什么？','input_through_sequence':1})
        self.answer(self.decision(),'reject')
        run=self.f.tick();self.assertEqual(run['terminal_reason'],'user_rejected')
        self.assertFalse(self.store.query('SELECT id FROM planner_runs WHERE run_id=?',(run['id'],)))

    def test_repeated_empty_clarifications_have_a_bounded_stop(self):
        for index in range(3):
            self.f.orchestrator.decision(graph.get(self.store,self.run['id']),'input_question',{'question':f'问题 {index}'})
            self.store.execute("UPDATE workflow_decisions SET status='applied' WHERE run_id=?",(self.run['id'],))
        self.store.execute("UPDATE runs SET status='ACTIVE',terminal_reason='' WHERE id=?",(self.run['id'],))
        self.f.tick()
        request=self.store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        lease=Coordinator(self.store,'question-test','test').claim(self.run['id'],'workflow',step_id=request['task_id'])
        try:
            with lease.bind():planning.stage(self.store,lease,request['id'],fixtures.envelope(self.store,request['id'],kind='REQUEST_USER_INPUT',question='又问一次'))
        finally:lease.release()
        run=self.f.tick();self.assertEqual(run['terminal_reason'],'clarification_exhausted')
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],)))
        self.f.tick();self.assertEqual(len(self.store.query('SELECT id FROM planner_runs WHERE run_id=?',(run['id'],))),1)
