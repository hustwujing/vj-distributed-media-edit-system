import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from webapp import agent_store,plan_readiness
from webapp.store import Store
from webapp.execution_intent import declare,get
from webapp.semantic_review import Decision,SemanticUncertain,SemanticUnavailable


class PlanReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name));agent_store.initialize(self.store)
        self.project=self.store.add_project('owner','review')['id']
        self.task=agent_store.add_turn(self.store,self.project,'plan','整理已保存资料，输出带来源的文字稿')
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.task,))
        declare(self.store,self.task,{'route':'single','operations':['script'],'goal':'整理已有资料并交付文字稿','estimated_seconds':20})
        self.host=SimpleNamespace(store=self.store,notify=lambda *_:None)

    def finish_review(self,answer):
        with patch('webapp.semantic_review.classify',return_value=answer):plan_readiness.review(self.store,self.task,'按来源排序已有资料，交付 notes.txt')
        agent_store.finish(self.store,self.task,'succeeded','按来源排序已有资料，交付 notes.txt')

    def uncertain(self):
        with patch('webapp.semantic_review.classify',side_effect=SemanticUncertain(evidence={'source':'independent'})):
            plan_readiness.review(self.store,self.task,'按来源排序已有资料，交付 notes.txt')
        agent_store.finish(self.store,self.task,'succeeded','按来源排序已有资料，交付 notes.txt')

    def message(self,text):
        t=agent_store.add_turn(self.store,self.project,'plan',text);plan_readiness.register(self.store,t)
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(t,));return t

    def test_uncertainty_is_retryable_and_has_audit_without_claiming_missing_input(self):
        self.uncertain();p=plan_readiness.public(self.store,self.task)
        self.assertEqual(p['state'],'uncertain');self.assertTrue(p['retry_available']);self.assertFalse(p['can_execute'])
        audit=json.loads(self.store.query("SELECT payload FROM agent_events WHERE kind='plan_review'")[0]['payload'])
        self.assertEqual(audit['evidence']['source'],'independent');self.assertEqual(len(audit['input_digest']),64)

    def test_confirm_unreviewed_plan_keeps_original_and_does_not_create_new_intent(self):
        self.uncertain();t=self.message('请按原方案开始')
        with patch('webapp.semantic_review.classify',return_value='confirm') as classify:
            handled,target=plan_readiness.handle_confirmation(self.host,agent_store.turn(self.store,t))
        self.assertTrue(handled);self.assertEqual(target,self.task)
        self.assertEqual(classify.call_args.args[3]['original_request'],'整理已保存资料，输出带来源的文字稿')
        row=agent_store.turn(self.store,t);self.assertEqual((row['mode'],row['status']),('plan','succeeded'));self.assertIsNone(get(self.store,t))
        digest=plan_readiness.public(self.store,self.task)['digest']
        with patch('webapp.semantic_review.classify',return_value='ready'):plan_readiness.retry_review(self.store,self.task,digest)
        self.assertTrue(plan_readiness.public(self.store,self.task)['can_execute'])
        self.assertEqual(agent_store.turn(self.store,t)['mode'],'plan')  # Must confirm again.
        confirm=self.message('执行原方案')
        with patch('webapp.semantic_review.classify',return_value='confirm'):plan_readiness.handle_confirmation(self.host,agent_store.turn(self.store,confirm))
        self.assertEqual(agent_store.turn(self.store,confirm)['mode'],'execute')
        self.assertFalse(plan_readiness.public(self.store,self.task)['can_execute'])

    def test_revised_request_invalidates_uncertain_plan(self):
        self.uncertain();t=self.message('输出改为CSV')
        with patch('webapp.semantic_review.classify',return_value='revise'):
            handled,_=plan_readiness.handle_confirmation(self.host,agent_store.turn(self.store,t))
        self.assertFalse(handled);self.assertEqual(plan_readiness.public(self.store,self.task)['state'],'stale')
        with self.assertRaises(ValueError):plan_readiness.retry_review(self.store,self.task,plan_readiness.public(self.store,self.task)['digest'])

    def test_low_confidence_confirmation_never_consumes_plan(self):
        self.finish_review('ready');t=self.message('要不还是做一下？')
        with patch('webapp.semantic_review.classify',side_effect=SemanticUncertain()):
            handled,_=plan_readiness.handle_confirmation(self.host,agent_store.turn(self.store,t))
        self.assertTrue(handled);self.assertEqual(agent_store.turn(self.store,t)['mode'],'plan')
        self.assertTrue(plan_readiness.public(self.store,self.task)['can_execute'])

    def test_retry_budget_and_new_unrelated_message_are_enforced(self):
        self.uncertain();digest=plan_readiness.public(self.store,self.task)['digest']
        with patch('webapp.semantic_review.classify',side_effect=SemanticUncertain()):
            plan_readiness.retry_review(self.store,self.task,digest);plan_readiness.retry_review(self.store,self.task,digest)
        with self.assertRaises(ValueError):plan_readiness.retry_review(self.store,self.task,digest)
        self.assertFalse(plan_readiness.public(self.store,self.task)['retry_available'])

    def test_late_review_cannot_override_new_declaration_or_cancel(self):
        def replace(*_):
            declare(self.store,self.task,{'route':'single','operations':['script'],'goal':'新版不同交付','estimated_seconds':30});return 'ready'
        with patch('webapp.semantic_review.classify',side_effect=replace):plan_readiness.review(self.store,self.task,'旧方案')
        self.assertEqual(plan_readiness.public(self.store,self.task)['state'],'pending')
        def cancel(*_):
            self.store.execute("UPDATE agent_turns SET status='cancelled' WHERE id=?",(self.task,));return 'ready'
        with patch('webapp.semantic_review.classify',side_effect=cancel):plan_readiness.review(self.store,self.task,'新版方案')
        self.assertFalse(plan_readiness.public(self.store,self.task)['can_execute'])

    def test_changed_assets_stale_review(self):
        def change(*_):
            self.store.add_asset(self.project,'new','new','new.png',{'kind':'image'});return 'ready'
        with patch('webapp.semantic_review.classify',side_effect=change):plan_readiness.review(self.store,self.task,'方案')
        self.assertEqual(plan_readiness.public(self.store,self.task)['state'],'stale')

    def test_missing_information_requires_specific_items(self):
        with patch('webapp.semantic_review.classify',return_value='needs_information'),patch('webapp.semantic_review.fallback_plan',return_value=Decision('needs_information',missing_items=['请选择需要处理的输入素材'],source='independent')):
            plan_readiness.review(self.store,self.task,'缺少输入')
        self.assertIn('请选择需要处理的输入素材',plan_readiness.public(self.store,self.task)['reason'])

    def test_saved_file_evidence_and_original_plan_in_confirmation_context(self):
        from webapp.plan_review_context import build
        files={'edit/transcripts/asset.json':{'sha256':'a'*64,'size':123}}
        with patch('webapp.plan_review_context.effective_manifest',return_value=files):
            context=build(self.store,self.task,'完整方案',get(self.store,self.task))
        self.assertEqual(context['saved_files'][0]['name'],'edit/transcripts/asset.json')
        self.assertFalse(context['coverage']['file_contents_checked'])
        self.finish_review('ready');t=self.message('确认方案')
        context=build(self.store,t,'简短跟进',get(self.store,self.task))
        self.assertEqual(context['prior_conversation'][0]['request'],'整理已保存资料，输出带来源的文字稿')
        self.assertIn('notes.txt',context['prior_conversation'][0]['proposal'])

    def test_newest_unready_proposal_never_selects_older_ready_plan(self):
        self.finish_review('ready');t=self.message('换一个方案')
        declare(self.store,t,{'route':'single','operations':['script'],'goal':'另一个输出','estimated_seconds':30})
        self.assertIsNone(plan_readiness.current(self.store,self.project))

    def test_low_confidence_plan_invokes_exactly_one_independent_review(self):
        from webapp.semantic_review import classify
        response=Mock();response.status_code=200;response.content=b'{}'
        response.json.return_value={'answers':{'decision':{'type':'choice','choice':'ready','confidence':.57,'probabilities':{'ready':.57,'needs_information':.42,'discussion':.01}}}}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        with patch('webapp.semantic_review.requests.Session') as client,patch('webapp.semantic_review.fallback_plan',return_value=Decision('ready',source='independent',confidence=.98)) as fallback:
            client.return_value.__enter__.return_value.post.return_value=response
            result=classify(self.store,self.task,'plan',{})
        self.assertEqual(result,'ready');fallback.assert_called_once();self.assertEqual(result.evidence['primary']['confidence'],.57)
        self.assertEqual(len(self.store.query("SELECT * FROM usage_calls WHERE task_id=?",(self.task,))),1)

    def test_new_message_during_retry_stales_late_review(self):
        self.uncertain();digest=plan_readiness.public(self.store,self.task)['digest']
        def newer(*_):
            self.message('稍等，改成另一个交付');return 'ready'
        with patch('webapp.semantic_review.classify',side_effect=newer):plan_readiness.retry_review(self.store,self.task,digest)
        self.assertEqual(plan_readiness.public(self.store,self.task)['state'],'stale')

    def test_unrelated_followup_prevents_retry_of_old_uncertain_plan(self):
        self.uncertain();t=self.message('先讨论别的事情');agent_store.finish(self.store,t,'succeeded','请说明')
        with self.assertRaises(ValueError):plan_readiness.retry_review(self.store,self.task,plan_readiness.public(self.store,self.task)['digest'])

    def test_diagnosis_exposes_review_scores_without_proposal_or_model_reason(self):
        from webapp.diagnostics import diagnose
        self.finish_review(Decision('ready',source='jev',choice='ready',confidence=.99,threshold=.9,reason='PRIVATE REVIEW TEXT'))
        report=diagnose(self.store,self.task,include_logs=False)
        self.assertEqual(report['plan_reviews'][0]['reviewer']['confidence'],.99)
        self.assertNotIn('PRIVATE REVIEW TEXT',json.dumps(report))
        self.assertEqual(report['plan_readiness'][0]['state'],'ready')

    def test_non_light_harness_confirmation_uses_same_gate_before_sandbox(self):
        from tests.test_conversation_routing import LightRoutingTests
        from webapp.agent_profiles import for_task,AgentProfile
        fixture=LightRoutingTests();fixture.setUp()
        try:
            plan=fixture.task('缩小已上传图片')
            fixture.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(plan,))
            declare(fixture.store,plan,{'route':'single','operations':['script'],'goal':'缩小图片','estimated_seconds':20})
            with patch('webapp.semantic_review.classify',side_effect=SemanticUncertain()):plan_readiness.review(fixture.store,plan,'输出128像素PNG')
            agent_store.finish(fixture.store,plan,'succeeded','输出128像素PNG')
            for harness in ('claude_code','deepseek_harness'):
                task=fixture.task('确认方案')
                config=for_task(fixture.store,task).snapshot();config.update(harness=harness,protocol='anthropic',model='fixture')
                profile=AgentProfile(**config)
                fixture.store.execute('UPDATE agent_run_profiles SET configuration=?,digest=? WHERE task_id=?',(json.dumps(profile.snapshot()),profile.digest,task))
                with patch('webapp.semantic_review.classify',return_value='confirm'):
                    row=fixture.run_task(task)
                self.assertEqual((row['mode'],row['status']),('plan','succeeded'))
                self.assertIsNone(get(fixture.store,task))
        finally:fixture.tearDown()

    def test_unavailable_reviewer_does_not_claim_missing_inputs(self):
        with patch('webapp.semantic_review.classify',side_effect=SemanticUnavailable()):plan_readiness.review(self.store,self.task,'方案')
        self.assertEqual(plan_readiness.public(self.store,self.task)['state'],'unavailable')
