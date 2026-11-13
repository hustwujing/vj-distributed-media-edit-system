"""Inbox ordering and barriers; no model or provider substitutions in production."""
import json
from tests.run_input_fixture import accept_change
import unittest
import uuid
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph,workflow_planning as planning,run_inputs
from webapp.task_leases import Coordinator


class RunInputTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.f.tick()
        self.f.propose({'proposal_id':'initial','base_graph_version':0,'reason':'Two independent reports',
            'add_tasks':[fixtures.script('left'),fixtures.script('right')],'sealed':True})
        self.run=self.f.tick();self.coordinator=Coordinator(self.store,'input-test','test')

    def send(self,text='Use a simpler layout',key=None):
        return accept_change(self.store,self.run['id'],'owner',{'request_id':key or uuid.uuid4().hex,'content':text})

    def reply(self,kind='CONTINUE',**extra):
        request=self.store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id=request['task_id'])
        self.assertIsNotNone(lease)
        try:
            with lease.bind():planning.stage(self.store,lease,request['id'],fixtures.envelope(self.store,request['id'],kind=kind,**extra))
        finally:lease.release()

    def test_input_first_blocks_previously_ready_task_and_is_idempotent(self):
        key=uuid.uuid4().hex;first=self.send(key=key)
        self.assertEqual(first['id'],self.send(key=key)['id'])
        with self.assertRaises(ValueError):self.send('Different request',key)
        self.assertIsNone(self.coordinator.claim(self.run['id'],'workflow',step_id='left'))
        run=self.f.tick()
        self.assertEqual(run['input_sequence'],1)
        self.assertEqual([job['control_kind'] for job in run['control_jobs'] if job['status']=='ready'],['PLAN'])
        self.assertEqual(run['drain_set'],[])

    def test_claim_first_drains_original_revision_and_holds_sibling(self):
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id='left');self.assertIsNotNone(lease)
        try:
            self.send();lease.check()
            run=graph.get(self.store,self.run['id'])
            self.assertEqual([(x['task_id'],x['revision']) for x in run['drain_set']],[('left',0)])
            self.assertIsNone(self.coordinator.claim(run['id'],'workflow',step_id='right'))
            self.f.tick()
            self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='pending'"))
            # Protocol fixture represents a normal completed drain result.
            self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='left'",(run['id'],))
        finally:lease.release()
        self.f.tick();self.reply();run=self.f.tick()
        self.assertIsNone(run['input_barrier_sequence'])
        self.assertEqual(run['graph_version'],1)
        self.assertTrue(run['sealed'])
        self.assertEqual(run['tasks'][0]['revision'],0)
        next_lease=self.coordinator.claim(run['id'],'workflow',step_id='right')
        self.assertIsNotNone(next_lease);next_lease.release()

    def test_paused_sibling_does_not_strand_recovered_drain_task(self):
        from webapp import workflow_dispatch
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id='left')
        self.assertIsNotNone(lease)
        self.send()
        lease.release()
        # Model recovery has requeued the previously-started drain task;
        # an independent task reached a paused content-review boundary.
        self.store.execute("UPDATE run_tasks SET status='ready',lease_id='' WHERE run_id=? AND id='left'",(self.run['id'],))
        self.store.execute("UPDATE run_tasks SET status='paused',lease_id='' WHERE run_id=? AND id='right'",(self.run['id'],))
        for state,reason in [('ACTIVE','input_draining'),('WAITING_USER','input_waiting_user_decision')]:
            self.store.execute('UPDATE runs SET status=?,terminal_reason=? WHERE id=?',(state,reason,self.run['id']))
            run=graph.get(self.store,self.run['id'])
            self.assertIn('left',workflow_dispatch.available(self.store,run,self.store.now()))
            self.assertIn('left',[row['step_id'] for row in workflow_dispatch.candidates(self.store)])
        self.store.execute("UPDATE runs SET status='ACTIVE',terminal_reason='' WHERE id=?",(self.run['id'],))
        recovered=self.coordinator.claim(self.run['id'],'workflow',step_id='left')
        self.assertIsNotNone(recovered)
        self.assertIsNone(self.coordinator.claim(self.run['id'],'workflow',step_id='right'))
        recovered.release()
        self.store.execute("UPDATE run_tasks SET status='succeeded',lease_id='' WHERE run_id=? AND id='left'",(self.run['id'],))
        self.assertEqual(run_inputs.safe_point_reason(self.store,graph.get(self.store,self.run['id'])),'')
        self.f.tick()
        self.assertTrue(self.store.query("SELECT id FROM planner_runs WHERE status='pending'"))
        self.assertEqual(self.store.query('SELECT status FROM run_inputs')[0]['status'],'PLANNING')

    def test_nondispatchable_drain_task_does_not_hold_input_barrier_forever(self):
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id='right');self.assertIsNotNone(lease)
        self.send();lease.release()
        self.store.execute('INSERT INTO workflow_dependencies(run_id,upstream_task_id,downstream_task_id,created_graph_version) VALUES (?,?,?,1)',
                           (self.run['id'],'left','right'))
        self.store.execute("UPDATE run_tasks SET status='paused',lease_id='',error_code='quality_failed' WHERE run_id=? AND id='left'",(self.run['id'],))
        self.store.execute("UPDATE run_tasks SET status='ready',lease_id='' WHERE run_id=? AND id='right'",(self.run['id'],))
        run=graph.get(self.store,self.run['id'])
        self.assertEqual(run_inputs.safe_point_reason(self.store,run),'')
        self.f.orchestrator.process_inputs(run)
        self.assertTrue(self.store.query("SELECT id FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],)))

    def test_dispatchable_retry_wait_remains_a_real_drain_condition(self):
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id='left');self.assertIsNotNone(lease)
        self.send();lease.release()
        self.store.execute("UPDATE run_tasks SET status='retry_wait',lease_id='',next_run_at=? WHERE run_id=? AND id='left'",
                           (self.store.now()+60,self.run['id']))
        self.assertEqual(run_inputs.safe_point_reason(self.store,graph.get(self.store,self.run['id'])),'draining_tasks')

    def test_input_during_planning_stays_in_the_next_batch(self):
        first=self.send();self.f.tick();second=self.send('Also keep the original dimensions')
        self.reply();run=self.f.tick()
        rows=self.store.query('SELECT sequence,status FROM run_inputs ORDER BY sequence')
        self.assertEqual(rows[0],{'sequence':1,'status':'APPLIED'})
        self.assertEqual(rows[1],{'sequence':2,'status':'PLANNING'})
        self.assertEqual(run['input_barrier_sequence'],2)
        request=self.store.query("SELECT snapshot_json FROM planner_runs WHERE status='pending'")[0]
        batch=json.loads(request['snapshot_json'])['input_batch']
        self.assertEqual([item['id'] for item in batch['inputs']],[second['id']])
        self.assertIsNone(self.coordinator.claim(run['id'],'workflow',step_id='right'))

    def test_applied_input_remains_in_later_manager_snapshots(self):
        accepted=self.send('Change left brightness to 1.35')
        self.f.tick();self.reply();run=self.f.tick()
        self.assertEqual(self.store.query('SELECT status FROM run_inputs')[0]['status'],'APPLIED')
        frozen=planning.snapshot(self.store,run)
        self.assertFalse(frozen['user_changes_truncated'])
        self.assertEqual(frozen['user_changes'],[{
            'sequence':1,'content':'Change left brightness to 1.35','source':'user',
            'context':accepted['context']}])
        from webapp.workflow_model_protocol import scopes,available_changes
        self.assertEqual(scopes(frozen),(None,None))
        self.assertEqual(available_changes(frozen),{})
        # Even bypassing the command compiler cannot turn applied history into
        # a fresh controller-side grant to rewrite the task's objective.
        revised={**run['tasks'][0]['spec'],'objective':'Unrequested weaker result'}
        raw={'proposal_id':'stale-history','base_graph_version':run['graph_version'],
             'reason':'Attempt stale revision','revise_tasks':[revised],'sealed':True}
        result={'kind':'COMPLETE','reason':'Attempt stale revision','proposal':raw,
                'base_graph_version':run['graph_version'],'authorization_version':run['authorization_version'],
                'snapshot_digest':graph.digest(frozen),'input_through_sequence':0}
        request={'status':'staged','authorization_version':run['authorization_version'],
                 'snapshot_digest':graph.digest(frozen),'snapshot_json':graph.encode(frozen),
                 'trigger_json':'{}','proposal_json':graph.encode(result)}
        with self.assertRaisesRegex(ValueError,'frozen repair trigger'):
            planning.apply(self.store,{**run,'sealed':False},request)
        self.assertEqual(graph.get(self.store,run['id'])['tasks'],run['tasks'])

    def test_pending_user_decision_prevents_safe_point(self):
        self.f.orchestrator.decision(self.run,'quality',{'task_id':'left','revision':0})
        self.send();run=self.f.tick()
        self.assertEqual(run['status'],'WAITING_USER')
        self.assertEqual(run['terminal_reason'],'input_waiting_user_decision')
        self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='pending'"))

    def test_pending_input_prevents_old_graph_publication(self):
        self.send()
        self.f.orchestrator.publish(self.run,{'decision_json':'{"deliverables":[]}'} )
        run=graph.get(self.store,self.run['id'])
        self.assertIsNone(run['delivered_at'])
        self.assertNotEqual(run['status'],'SUCCEEDED')

    def test_manager_question_does_not_mutate_the_graph_or_consume_input(self):
        self.send();self.f.tick()
        self.reply('REQUEST_USER_INPUT',question='Which background should be used?',options=['white','black'])
        run=self.f.tick()
        self.assertEqual(run['graph_version'],1)
        self.assertEqual(run['status'],'WAITING_USER')
        self.assertEqual(self.store.query('SELECT status FROM run_inputs')[0]['status'],'ACCEPTED')
        self.assertEqual(self.store.query("SELECT kind FROM workflow_decisions WHERE status='pending'")[0]['kind'],'input_question')

    def test_rejecting_manager_question_supersedes_batch_and_restores_old_graph(self):
        accepted=self.send('Use a different background')
        self.f.tick();self.reply('REQUEST_USER_INPUT',question='Which background?',options=['white','black'])
        run=self.f.tick();decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]
        self.f.orchestrator.answer(run,{'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'reject'})
        current=graph.get(self.store,self.run['id'])
        self.assertEqual(self.store.query('SELECT status,error_code FROM run_inputs WHERE id=?',(accepted['id'],))[0],
                         {'status':'SUPERSEDED','error_code':'user_rejected_input'})
        self.assertIsNone(current['input_barrier_sequence'])
        self.assertTrue(current['sealed'])
        self.assertEqual((current['status'],current['terminal_reason']),('ACTIVE','input_draining'))

    def test_changed_attachment_can_be_rejected_without_a_barrier_loop(self):
        asset_id='old-reference'
        self.store.add_asset(self.run['project_id'],asset_id,'old.png','old.png',{'kind':'image','width':8,'height':8})
        accepted=accept_change(self.store,self.run['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Use this reference','attachments':[asset_id]})
        self.store.execute('DELETE FROM assets WHERE id=?',(asset_id,))
        run=self.f.tick();decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]
        self.assertEqual(decision['kind'],'input_question')
        self.f.orchestrator.answer(run,{'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'reject'})
        current=graph.get(self.store,self.run['id'])
        self.assertEqual(self.store.query('SELECT status,error_code FROM run_inputs WHERE id=?',(accepted['id'],))[0],
                         {'status':'SUPERSEDED','error_code':'user_rejected_input'})
        self.assertIsNone(current['input_barrier_sequence'])
        self.assertTrue(current['sealed'])

    def test_changed_attachment_replacement_advances_barrier_to_new_input(self):
        old_id='old-reference'
        self.store.add_asset(self.run['project_id'],old_id,'old.png','old.png',{'kind':'image','width':8,'height':8})
        original=accept_change(self.store,self.run['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Use this reference','attachments':[old_id]})
        self.store.execute('DELETE FROM assets WHERE id=?',(old_id,))
        run=self.f.tick();decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]
        replacement_id='new-reference'
        self.store.add_asset(self.run['project_id'],replacement_id,'new.png','new.png',{'kind':'image','width':8,'height':8})
        request_id=uuid.uuid4().hex
        self.f.orchestrator.answer(run,{'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],
            'action':'revise','feedback':'Use the replacement reference','attachments':[replacement_id]},request_id=request_id)
        rows=self.store.query('SELECT id,sequence,status,error_code FROM run_inputs ORDER BY sequence')
        self.assertEqual(rows[0],{'id':original['id'],'sequence':1,'status':'SUPERSEDED','error_code':'attachment_replaced_by_user'})
        self.assertEqual((rows[1]['sequence'],rows[1]['status']),(2,'ACCEPTED'))
        self.assertEqual(graph.get(self.store,self.run['id'])['input_barrier_sequence'],2)

    def test_explicit_attachment_authorizes_unconfirmed_successor_scope(self):
        asset_id='successor-reference'
        self.store.add_asset(self.run['project_id'],asset_id,'reference.png','reference.png',
                             {'kind':'image','width':8,'height':8})
        self.store.execute("UPDATE runs SET status='FAILED',terminal_reason='test_failure' WHERE id=?",(self.run['id'],))
        successor=graph.create(self.store,self.run['project_id'],None,
            {**self.run['authorization'],'allowed_assets':[],
             'allowed_asset_digests':{},
             'content_fingerprint':graph.content_fingerprint(self.store,self.run['project_id'])},
            parent_run_id=self.run['id'],scope_confirmed=False,record_message=False)
        request=accept_change(self.store,successor['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Retry using the original reference',
            'attachments':[asset_id]})
        current=graph.get(self.store,successor['id'])
        self.assertEqual([item['id'] for item in request['attachments']],[asset_id])
        self.assertTrue(current['scope_confirmed'])
        self.assertEqual(current['authorization']['allowed_assets'],[asset_id])
        asset=next(item for item in self.store.assets(self.run['project_id']) if item['id']==asset_id)
        self.assertEqual(current['authorization']['allowed_asset_digests'][asset_id],graph.asset_digest(asset))

    def test_first_delivered_run_input_authorizes_its_new_successor_attachment(self):
        asset_id=self.run['authorization']['allowed_assets'][0]
        self.store.execute("UPDATE runs SET status='SUCCEEDED',delivered_at=?,delivery_json='[]' WHERE id=?",
                           (self.store.now(),self.run['id']))
        received=accept_change(self.store,self.run['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Continue with the selected original input',
            'attachments':[asset_id]})
        successor=graph.get(self.store,received['run_id'])
        self.assertEqual(successor['parent_run_id'],self.run['id'])
        self.assertTrue(successor['scope_confirmed'])
        self.assertIn(asset_id,successor['authorization']['allowed_assets'])
        self.assertTrue(received['attachments'][0]['authorized'])

    def test_succeeded_run_without_a_published_delivery_rejects_input(self):
        self.store.execute("UPDATE runs SET status='SUCCEEDED',delivered_at=NULL WHERE id=?",(self.run['id'],))
        with self.assertRaisesRegex(ValueError,'cannot accept'):
            self.send('This input cannot have a recovery path')

    def test_targeted_input_freezes_descendant_scope_and_cannot_revise_sibling(self):
        self.store.execute('INSERT INTO workflow_dependencies(run_id,upstream_task_id,downstream_task_id,created_graph_version) VALUES (?,?,?,1)',
                           (self.run['id'],'left','right'))
        result=accept_change(self.store,self.run['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Change only the final report','targets':['right']})
        self.assertEqual(result['context']['allowed_revision_tasks'],['right'])
        self.assertEqual(result['context']['base_task_revisions'],{'left':0,'right':0})
        self.f.tick()
        revision={**next(task['spec'] for task in self.run['tasks'] if task['id']=='left'),'argv':['python','-c','print("changed")']}
        self.f.propose({'proposal_id':'wrong-branch','base_graph_version':1,'reason':'Change unrelated upstream',
                        'revise_tasks':[revision],'sealed':True})
        run=self.f.tick()
        self.assertEqual(run['graph_version'],1)
        self.assertEqual([task['revision'] for task in run['tasks']],[0,0])
        self.assertIsNotNone(run['input_barrier_sequence'])

    def test_successor_input_deduplicates_and_requires_a_fresh_scope_confirmation(self):
        original=self.run
        self.store.execute("UPDATE runs SET status='SUCCEEDED',delivered_at=? WHERE id=?",(self.store.now(),original['id']))
        first=accept_change(self.store,original['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Make a second report','targets':['left']})
        second=accept_change(self.store,original['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Keep it concise'})
        self.assertEqual(first['run_id'],second['run_id'])
        self.assertNotEqual(first['run_id'],original['id'])
        self.assertEqual(first['context']['allowed_revision_tasks'],[])
        self.assertEqual(first['context']['predecessor_targets'][0]['task_id'],'left')
        self.run=graph.get(self.store,first['run_id']);self.f.run=self.run
        self.assertFalse(self.run['scope_confirmed'])
        self.assertEqual(self.run['tasks'],[])
        self.f.tick()
        request=self.store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id=request['task_id'])
        self.assertIsNotNone(lease)
        try:
            with lease.bind():
                with self.assertRaisesRegex(ValueError,'scope|Scope|confirmation'):
                    planning.stage(self.store,lease,request['id'],fixtures.envelope(self.store,request['id'],kind='CONTINUE'))
                planning.stage(self.store,lease,request['id'],fixtures.envelope(self.store,request['id'],
                    kind='REQUEST_AUTHORIZATION',authorization=self.run['authorization']))
        finally:lease.release()
        run=self.f.tick()
        self.assertEqual(run['status'],'WAITING_USER')
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))[0]
        self.assertEqual(decision['kind'],'successor_authorization')
        graph.command(self.store,run['id'],'owner','answer',{
            'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],
            'action':'approve','authorization':run['authorization']})
        run=self.f.tick()
        self.assertTrue(run['scope_confirmed'])
        self.assertEqual(run['authorization_version'],2)
        self.assertEqual(self.store.query('SELECT COUNT(*) AS n FROM run_authorizations WHERE run_id=?',(run['id'],))[0]['n'],2)
        self.assertEqual(graph.get(self.store,original['id'])['status'],'SUCCEEDED')

    def test_rejecting_plan_change_supersedes_its_input_and_clears_barrier(self):
        accepted=self.send('Request a scope change')
        run=graph.get(self.store,self.run['id'])
        self.f.orchestrator.decision(run,'plan_change',{
            'reason':'The input needs renewed scope','input_through_sequence':accepted['sequence']})
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))[0]
        self.f.orchestrator.answer(graph.get(self.store,run['id']),{
            'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'reject'})
        current=graph.get(self.store,run['id'])
        item=self.store.query('SELECT status,error_code FROM run_inputs WHERE id=?',(accepted['id'],))[0]
        self.assertEqual(item,{'status':'SUPERSEDED','error_code':'authorization_rejected'})
        self.assertIsNone(current['input_barrier_sequence'])
        self.assertEqual((current['status'],current['terminal_reason']),('ACTIVE','authorization_rejected'))

    def test_reject_batch_repairs_a_stale_barrier_when_input_was_already_resolved(self):
        accepted=self.send('Already resolved elsewhere')
        self.store.execute("UPDATE run_inputs SET status='APPLIED',applied_at=? WHERE id=?",(self.store.now(),accepted['id']))
        run=graph.get(self.store,self.run['id']);self.assertEqual(run['input_barrier_sequence'],accepted['sequence'])
        run_inputs.reject_batch(self.store,run,accepted['sequence'],'stale_decision_recovery')
        current=graph.get(self.store,self.run['id'])
        self.assertIsNone(current['input_barrier_sequence']);self.assertEqual(current['drain_set'],[])
        self.assertTrue(current['sealed'])

    def test_scope_approval_rebinds_server_owned_policy_digests(self):
        run=graph.get(self.store,self.run['id'])
        self.f.orchestrator.decision(run,'plan_change',{'reason':'Policy changed'})
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))[0]
        stale={**run['authorization'],'agent_profile_digests':{name:'0'*64 for name in run['authorization']['allowed_agent_profiles']},
               'operation_policy_digests':{name:'0'*64 for name in run['authorization']['allowed_operations'] if name!='script'}}
        self.f.orchestrator.answer(run,{'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],
            'action':'approve','authorization':stale})
        current=graph.get(self.store,run['id'])
        self.assertEqual(current['authorization_version'],1)
        self.assertNotIn('0'*64,current['authorization']['agent_profile_digests'].values())
