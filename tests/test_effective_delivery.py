"""Confirmed amendments replace delivery requirements, not content or grants."""
import copy
import json
import uuid
import unittest
from tests import test_dynamic_graph as fixtures
from tests import test_workflow_conversation as conversation
from tests.run_input_fixture import accept_change
from webapp import workflow_graph as graph,workflow_planning as planning
from webapp.effective_delivery import confirmed_scope,change_details
from webapp.run_authorization import approve
from webapp.workflow_model_protocol import required_delivery_names,required_delivery_extensions,required_delivery_roles
from webapp.workflow_agent_contracts import approved_delivery_refs,delivery_leaf_refs,delivery_publication_names


class EffectiveDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.run=self.f.run
        auth={**self.run['authorization'],'goal':'交付 old.mp4、字幕 captions.srt 和 JSON 报告。视频必须为960x540、30fps。',
              'delivery_targets':['old.mp4','captions.srt','JSON 报告']}
        self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',(auth['goal'],graph.encode(auth),self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(graph.encode(auth),self.run['id']))
        self.run=graph.get(self.store,self.run['id'])

    def snapshot(self):
        from webapp.workflow_orchestrator import delivery_scope
        return delivery_scope(self.store,graph.get(self.store,self.run['id']))

    def amend(self,targets):
        run=graph.get(self.store,self.run['id'])
        item=accept_change(self.store,run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'确认将完整交付清单改为 '+json.dumps(targets,ensure_ascii=False)})
        authorization={**run['authorization'],'delivery_targets':targets}
        self.f.orchestrator.decision(graph.get(self.store,run['id']),'plan_change',{
            'authorization':authorization,'delivery_scope_changed':True,'input_through_sequence':item['sequence']})
        row=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND status='pending' ORDER BY created_at DESC",(run['id'],))[0]
        self.f.orchestrator.answer(graph.get(self.store,run['id']),{'decision_id':row['id'],
            'snapshot_digest':row['snapshot_digest'],'action':'approve','authorization':authorization})
        return self.snapshot()

    def test_grant_text_without_an_approved_delivery_amendment_is_not_a_waiver(self):
        approve(self.store,self.run,graph.Authorization.model_validate({**self.run['authorization'],'delivery_targets':['new.mp4']}))
        self.assertIsNone(self.snapshot()['effective_delivery_scope'])

    def test_unconfirmed_inherited_targets_can_be_deferred_without_waiving_a_confirmed_list(self):
        self.store.execute('UPDATE runs SET scope_confirmed=0 WHERE id=?',(self.run['id'],))
        run=graph.get(self.store,self.run['id'])
        approve(self.store,run,graph.Authorization.model_validate({**run['authorization'],'delivery_targets':[]}))
        self.assertIsNone(self.snapshot()['effective_delivery_scope'])

    def test_unconfirmed_text_never_waives_initial_delivery_requirements(self):
        value=self.snapshot();self.assertIsNone(value['effective_delivery_scope'])
        self.assertEqual(required_delivery_extensions(value),{'mp4','srt','json'})
        self.assertEqual(required_delivery_roles(value),{'report'})
        accept_change(self.store,self.run['id'],'owner',{'request_id':'a'*32,'content':'只要MP4，不要字幕和报告'})
        self.assertIsNone(confirmed_scope(self.store,graph.get(self.store,self.run['id'])))
        self.assertEqual(required_delivery_extensions(self.snapshot()),{'mp4','srt','json'})

    def test_confirmed_shrink_and_later_expansion_are_shared_by_planner_and_publisher(self):
        value=self.amend(['renamed.mp4'])
        self.assertEqual(required_delivery_extensions(value),{'mp4'});self.assertEqual(required_delivery_names(value),{'renamed.mp4'})
        self.assertEqual(required_delivery_roles(value),set())
        planner=planning.snapshot(self.store,graph.get(self.store,self.run['id']))
        self.assertEqual(planner['effective_delivery_scope'],value['effective_delivery_scope'])
        self.assertIn('960x540',value['goal']);self.assertIn('30fps',value['goal'])
        value=self.amend(['renamed.mp4','新JSON报告'])
        self.assertEqual(required_delivery_extensions(value),{'mp4','json'});self.assertEqual(required_delivery_roles(value),{'report'})
        self.assertNotIn('captions.srt',required_delivery_names(value))

    def test_format_replacement_does_not_keep_old_format_and_budget_change_does_not_change_scope_identity(self):
        value=self.amend(['JPEG 图片'])
        self.assertEqual(required_delivery_extensions(value),{'jpeg'})
        proof=copy.deepcopy(value['effective_delivery_scope']);run=graph.get(self.store,self.run['id'])
        approve(self.store,run,graph.Authorization.model_validate({**run['authorization'],'max_tasks':max(1,run['authorization']['max_tasks']-1)}))
        self.assertEqual(self.snapshot()['effective_delivery_scope'],proof)
        with self.assertRaisesRegex(ValueError,'cannot be cleared'):self.amend([])

    def test_removed_old_outputs_are_optional_but_new_scope_tasks_and_content_are_not_waived(self):
        value=self.amend(['renamed.mp4']);version=value['effective_delivery_scope']['authorization_version']
        def task(identifier,filename,role,grant=1):return {'id':identifier,'authorization_version':grant,
            'spec':{'type':'operation','delivery_stage':True},'accepted_result':['edit/'+filename],
            'quality':{'output_roles':{role:'edit/'+filename}}}
        value['tasks']=[task('video','final.mp4','video'),task('caption','captions.srt','subtitle'),task('report','report.json','report')]
        self.assertEqual(approved_delivery_refs(value),{'edit/final.mp4'})
        self.assertEqual(delivery_leaf_refs(value),{})
        self.assertEqual(delivery_publication_names(value),{'edit/final.mp4':'renamed.mp4'})
        value['tasks'].append(task('new-required','new.mp4','video',version))
        self.assertEqual(delivery_leaf_refs(value),{'new-required':{'edit/new.mp4'}})
        # This projection cannot alter Task state, acceptance, input or grant.
        self.assertEqual(value['tasks'][0]['authorization_version'],1)

    def test_full_goal_stays_in_final_review_and_confirmation_diff_exposes_removed_requirements(self):
        from webapp.review_identity import final_criteria
        before=self.snapshot();delta=change_details(before,['renamed.mp4'])
        self.assertEqual(delta['formats']['removed'],['json','srt'])
        self.assertEqual(delta['roles']['removed'],['report'])
        self.amend(['renamed.mp4']);run=graph.get(self.store,self.run['id'])
        self.assertTrue(any('960x540' in x for x in final_criteria(run,[])))

    def test_repeated_inconclusive_review_exposes_the_actual_blocking_feedback(self):
        self.f.orchestrator.queue_review(self.run)
        review=self.store.query("SELECT * FROM workflow_reviews WHERE run_id=? AND purpose='final'",(self.run['id'],))[0]
        feedback='当前旧名称与已确认新名称冲突，不能按旧清单发布。'
        self.store.execute("UPDATE workflow_reviews SET status='rejected',decision_json=? WHERE id=?",(graph.encode({'verdict':'inconclusive','feedback':feedback}),review['id']))
        self.store.execute("UPDATE control_jobs SET status='succeeded' WHERE run_id=? AND id=?",(self.run['id'],review['task_id']))
        self.f.orchestrator.queue_review(graph.get(self.store,self.run['id']))
        decision=self.store.query("SELECT request_json FROM workflow_decisions WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        self.assertIn(feedback,json.loads(decision['request_json'])['question'])


class DeliveryConfirmationTests(unittest.TestCase):
    def test_revised_first_confirmation_replaces_prior_proposed_list_and_records_approval(self):
        f=conversation.ConversationTests();f.setUp();self.addCleanup(f.doCleanups)
        f.stage(targets=['old.txt','JSON 报告']);first=f.decision()
        accept_change(f.store,f.run['id'],'owner',{'request_id':'b'*32,'content':'只交付 new.txt，不再交付JSON报告，先更新方案。'})
        f.stage(targets=['new.txt']);decision=f.decision();request=json.loads(decision['request_json'])
        self.assertNotEqual(first['id'],decision['id']);self.assertTrue(request['delivery_scope_changed'])
        self.assertEqual(request['delivery_targets'],['new.txt']);self.assertIn('json',request['delivery_changes']['formats']['removed'])
        self.assertIsNone(confirmed_scope(f.store,graph.get(f.store,f.run['id'])))
        f.answer(decision,'approve');run=graph.get(f.store,f.run['id'])
        self.assertEqual(run['authorization']['delivery_targets'],['new.txt'])
        self.assertEqual(confirmed_scope(f.store,run)['targets'],['new.txt'])

    def test_nonempty_existing_list_changes_only_after_current_plan_confirmation(self):
        f=conversation.ConversationTests();f.setUp();self.addCleanup(f.doCleanups)
        # Supply the initially user-selected list; it must not permanently win
        # over a subsequent explicitly requested and confirmed list.
        auth={**graph.get(f.store,f.run['id'])['authorization'],'delivery_targets':['old.txt','JSON 报告']}
        f.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(auth),f.run['id']))
        f.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(graph.encode(auth),f.run['id']))
        accept_change(f.store,f.run['id'],'owner',{'request_id':'c'*32,'content':'交付改为 new.txt，不要JSON报告，先给新方案。'})
        f.stage(targets=['new.txt']);decision=f.decision()
        self.assertEqual(graph.get(f.store,f.run['id'])['authorization']['delivery_targets'],auth['delivery_targets'])
        f.answer(decision,'approve');run=graph.get(f.store,f.run['id'])
        self.assertEqual(run['authorization']['delivery_targets'],['new.txt'])
        self.assertEqual(run['authorization_version'],2)
        self.assertEqual(run['tasks'][0]['authorization_version'],2)
