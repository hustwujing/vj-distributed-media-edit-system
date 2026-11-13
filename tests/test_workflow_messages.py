"""Manager routing against real isolated storage, claims and reconciliation."""
import json
import uuid
import unittest
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph,workflow_messages as messages,run_inputs
from webapp.workflow_dispatch import stage_result
from webapp.task_leases import Coordinator


class WorkflowMessageTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.f.tick()
        self.f.propose({'proposal_id':'initial','base_graph_version':0,'reason':'Two reports',
                        'add_tasks':[fixtures.script('left'),fixtures.script('right')],'sealed':True})
        self.run=self.f.tick();self.coordinator=Coordinator(self.store,'message-test','test')

    def send(self,content='你好，进展怎样？',key=None):
        return run_inputs.accept(self.store,self.run['id'],'owner',{'request_id':key or uuid.uuid4().hex,'content':content})

    def job(self):
        self.f.tick()
        return next(t for t in graph.get(self.store,self.run['id'])['control_jobs'] if t['control_kind']=='MESSAGE' and t['status']=='ready')

    def complete(self,job,action='reply',answer='任务仍在进行，两个步骤尚未完成。',change='',before_release=None,retry_option_id=''):
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id=job['id']);self.assertIsNotNone(lease)
        try:
            if before_release:before_release()
            stage_result(self.store,lease,'succeeded',{'input_id':job['spec']['message_input_id'],
                'snapshot_digest':job['snapshot_digest'],'message_decision':{'action':action,'answer':answer,'change':change,
                    'retry_option_id':retry_option_id}})
        finally:lease.release()
        self.f.tick()
        return graph.get(self.store,self.run['id'])

    def test_plain_message_does_not_create_barrier_or_block_claim_and_reply_is_durable(self):
        key=uuid.uuid4().hex;message=self.send(key=key);self.assertEqual(message['status'],'ROUTING')
        self.assertEqual(self.send(key=key)['id'],message['id'])
        run=graph.get(self.store,self.run['id']);self.assertIsNone(run['input_barrier_sequence']);self.assertEqual(run['input_sequence'],0)
        job=self.job();lease=self.coordinator.claim(run['id'],'workflow',step_id='left');self.assertIsNotNone(lease);lease.release()
        run=self.complete(job)
        self.assertEqual((run['status'],run['graph_version'],run['sealed']),('ACTIVE',1,True))
        self.assertEqual(self.store.query('SELECT status FROM run_inputs WHERE id=?',(message['id'],))[0]['status'],'REPLIED')
        from webapp.workflow_api import public
        value=public(self.store,run);self.assertEqual(value['message_pending'],0);self.assertIn('两个步骤',value['inputs'][0]['reply'])
        self.assertEqual(run['authorization'],self.run['authorization'])

    def test_only_manager_change_establishes_barrier_and_preserves_inflight_work(self):
        self.send('请改变右侧报告标题');job=self.job()
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id='left');self.assertIsNotNone(lease)
        try:
            run=self.complete(job,'replan','收到，将修改右侧报告标题。','修改右侧报告标题')
            lease.check();self.assertIsNotNone(run['input_barrier_sequence'])
            self.assertEqual([x['task_id'] for x in run['drain_set']],['left'])
            self.assertIsNone(self.coordinator.claim(run['id'],'workflow',step_id='right'))
            self.assertEqual(run['graph_version'],1)
        finally:lease.release()

    def test_reply_preserves_pending_confirmation_and_original_business_sequence(self):
        self.f.orchestrator.decision(self.run,'plan_confirmation',{'question':'确认已有方案？','planning_request_id':'unused'})
        before=self.store.query("SELECT id,status,snapshot_digest FROM workflow_decisions WHERE run_id=?",(self.run['id'],))
        self.send('你能解释一下吗？');job=self.job();run=self.complete(job,answer='这是现有方案的解释，请仍使用原确认按钮。')
        self.assertEqual(run['status'],'WAITING_USER');self.assertEqual(run['input_sequence'],0)
        self.assertEqual(before,self.store.query("SELECT id,status,snapshot_digest FROM workflow_decisions WHERE run_id=?",(self.run['id'],)))

    def test_messages_are_serial_and_read_only_reply_does_not_consume_later_change(self):
        first=self.send('谢谢');second=self.send('把标题变为蓝色');job=self.job()
        self.assertEqual(job['spec']['message_input_id'],first['id'])
        self.complete(job,answer='不客气，任务会继续。');next_job=self.job()
        self.assertEqual(next_job['spec']['message_input_id'],second['id'])
        run=self.complete(next_job,'replan','收到蓝色标题要求。','标题改为蓝色')
        self.assertEqual(run['input_sequence'],2)
        self.assertEqual(self.store.query('SELECT status FROM run_inputs WHERE id=?',(first['id'],))[0]['status'],'REPLIED')

    def test_business_completion_during_reply_does_not_lose_answer_or_reopen_run(self):
        self.send();job=self.job()
        def finish():self.store.execute("UPDATE runs SET status='SUCCEEDED',delivered_at=? WHERE id=?",(self.store.now(),self.run['id']))
        run=self.complete(job,before_release=finish)
        self.assertEqual(run['status'],'SUCCEEDED');self.assertFalse(messages.pending(self.store,run['id']))
        self.assertTrue(self.store.query("SELECT id FROM session_messages WHERE id LIKE 'message:%'"))

    def test_completed_run_can_finish_queued_message_and_change_creates_unapproved_successor(self):
        original=self.send('另外请制作一个不同的报告');job=self.job()
        self.store.execute("UPDATE runs SET status='SUCCEEDED',delivered_at=? WHERE id=?",(self.store.now(),self.run['id']))
        run=self.complete(job,'replan','收到新的报告要求。','制作不同的报告')
        self.assertEqual(run['status'],'SUCCEEDED')
        row=self.store.query('SELECT * FROM run_inputs WHERE id=?',(original['id'],))[0]
        successor=graph.get(self.store,row['successor_run_id']);self.assertFalse(successor['scope_confirmed'])
        self.assertEqual(successor['goal'],original['content']);self.assertEqual(successor['tasks'],[])

    def test_message_failure_does_not_pause_business_and_is_visible(self):
        original=self.send();job=self.job();messages.fail(self.store,self.run,job,'model_unavailable')
        run=graph.get(self.store,self.run['id']);self.assertEqual(run['status'],'ACTIVE');self.assertIsNone(run['input_barrier_sequence'])
        from webapp.workflow_api import public
        data=public(self.store,run);self.assertIn('model_unavailable',data['inputs'][0]['reply']);self.assertEqual(data['message_pending'],0)
        self.assertEqual(self.store.query('SELECT status FROM run_inputs WHERE id=?',(original['id'],))[0]['status'],'FAILED')

    def test_message_retry_is_bounded_and_cannot_bypass_snapshot_identity(self):
        self.send();job=self.job()
        with self.assertRaisesRegex(ValueError,'frozen input'):
            messages.apply(self.store,self.run,job,{'input_id':'wrong','snapshot_digest':job['snapshot_digest'],
                'message_decision':{'action':'reply','answer':'hello','change':''}})
        for _ in range(4):
            job=next(t for t in graph.get(self.store,self.run['id'])['control_jobs'] if t['id']==job['id'])
            self.store.execute('UPDATE control_jobs SET next_run_at=0 WHERE run_id=? AND id=?',(self.run['id'],job['id']))
            lease=self.coordinator.claim(self.run['id'],'workflow',step_id=job['id']);self.assertIsNotNone(lease);lease.release()
            messages.recover(self.store,graph.get(self.store,self.run['id']))
        job=next(t for t in graph.get(self.store,self.run['id'])['control_jobs'] if t['id']==job['id'])
        self.assertEqual(job['status'],'failed');self.assertEqual(job['error_code'],'message_recovery_exhausted')
        self.assertEqual(graph.get(self.store,self.run['id'])['status'],'ACTIVE')

    def test_protocol_exposes_no_media_or_plan_mutation_tools(self):
        protocol=messages.MessageProtocol();self.assertEqual(set(protocol.schemas),{'reply_message','replan_message','new_task_message'})
        self.assertTrue(protocol.definitions())
        with self.assertRaises(ValueError):protocol.decode('script',{'code':'bad'})
        with self.assertRaises(ValueError):messages.MessageDecision(action='reply',answer='hello',change='silently edit')

    def test_retry_current_planning_failure_is_server_bound_and_applies_resume(self):
        plan=next(job for job in graph.get(self.store,self.run['id'])['control_jobs'] if job['control_kind']=='PLAN')
        self.store.execute('DELETE FROM workflow_dependencies WHERE run_id=?',(self.run['id'],))
        self.store.execute('DELETE FROM run_tasks WHERE run_id=?',(self.run['id'],))
        self.store.execute("UPDATE planner_runs SET status='rejected',error_code='agent_model_transport_failed' WHERE run_id=?",(self.run['id'],))
        self.store.execute("UPDATE control_jobs SET status='paused',error_code='agent_model_transport_failed',error_detail='RemoteDisconnected' WHERE run_id=? AND id=?",(self.run['id'],plan['id']))
        self.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='agent_model_transport_failed',graph_version=0,sealed=0 WHERE id=?",(self.run['id'],))
        run=graph.get(self.store,self.run['id']);options=messages.retry_options(self.store,run)
        self.assertEqual(len(options),1)
        from webapp.workflow_api import public
        self.assertEqual(public(self.store,run)['blocking_action']['action'],'retry')
        protocol=messages.MessageProtocol(options);self.assertIn('retry_current_failure',protocol.schemas)
        incoming=self.send('继续重试');messages.queue(self.store,graph.get(self.store,self.run['id']))
        job=next(item for item in graph.get(self.store,self.run['id'])['control_jobs'] if item['control_kind']=='MESSAGE' and item['status']=='ready')
        frozen=json.loads(job['snapshot_json'])
        self.assertEqual(frozen['current_failure']['terminal_reason'],'agent_model_transport_failed')
        self.assertEqual(frozen['retry_options'][0]['id'],options[0]['id'])
        self.complete(job,'retry','收到，重试当前规划。',retry_option_id=options[0]['id'])
        self.f.tick();command=self.store.query("SELECT action,status,error_code FROM workflow_commands WHERE run_id=? ORDER BY created_at DESC LIMIT 1",(self.run['id'],))[0]
        self.assertEqual(command,{'action':'resume','status':'applied','error_code':''})
        self.assertEqual(self.store.query('SELECT status FROM run_inputs WHERE id=?',(incoming['id'],))[0]['status'],'REPLIED')
        retried=graph.get(self.store,self.run['id'])
        self.assertTrue(any(job['control_kind']=='PLAN' and job['status']=='ready' and job['id']!=plan['id'] for job in retried['control_jobs']))

    def test_retry_tool_is_absent_without_a_current_server_option(self):
        protocol=messages.MessageProtocol();self.assertNotIn('retry_current_failure',protocol.schemas)
        with self.assertRaises(ValueError):protocol.decode('retry_current_failure',{'answer':'retry','retry_option_id':'invented'})

    def test_read_only_message_lease_does_not_block_final_business_gate(self):
        self.send();job=self.job();lease=self.coordinator.claim(self.run['id'],'workflow',step_id=job['id'])
        self.assertIsNotNone(lease)
        try:
            self.assertEqual(messages.business_inflight(self.store,self.run['id']),([],[]))
            business=self.coordinator.claim(self.run['id'],'workflow',step_id='left');self.assertIsNotNone(business)
            try:self.assertEqual([r['attempt'] for r in messages.business_inflight(self.store,self.run['id'])[0]],[business.attempt])
            finally:business.release()
        finally:lease.release()

    def test_undecided_routing_message_blocks_terminal_publication(self):
        message=self.send('最终发布前我可能要修改交付，请先判断这条消息。');self.job()
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=?",(self.run['id'],))
        current=graph.get(self.store,self.run['id'])
        self.f.orchestrator._publish(current,{'id':'not-used-before-routing-settles'})
        blocked=graph.get(self.store,self.run['id'])
        self.assertIsNone(blocked['delivered_at']);self.assertNotEqual(blocked['status'],'SUCCEEDED')
        self.assertEqual(self.store.query('SELECT status FROM run_inputs WHERE id=?',(message['id'],))[0]['status'],'ROUTING')

    def test_message_model_runner_uses_existing_manager_and_stages_only_bound_decision(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        self.send();job=self.job();lease=self.coordinator.claim(self.run['id'],'workflow',step_id=job['id'])
        self.assertIsNotNone(lease)
        def model(host,claimed,run,profile,conversation,*args,**kwargs):
            self.assertEqual(profile.id,'media_planner');self.assertEqual(claimed.attempt,lease.attempt)
            self.assertIn('snapshot_at',conversation);self.assertEqual(set(kwargs['protocol'].schemas),{'reply_message','replan_message','new_task_message'})
            return {'action':'reply','answer':'正在执行两个报告任务。','change':''}
        try:
            current=graph.get(self.store,self.run['id']);task=graph.work_item(current,job['id'])
            with patch('webapp.workflow_agents.model',side_effect=model):execute(SimpleNamespace(store=self.store),lease,current,task)
        finally:lease.release()
        run=self.f.tick();self.assertEqual(run['status'],'ACTIVE');self.assertEqual(run['graph_version'],1)
        self.assertEqual(self.store.query('SELECT profile_id FROM specialist_attempt_profiles WHERE attempt_id=?',(lease.attempt,))[0]['profile_id'],'media_planner')

    def test_followup_message_receives_the_previous_clarification_and_cancel_settles_queue(self):
        first=self.send('不太对');job=self.job();self.complete(job,answer='你想调整标题还是画面？当前任务继续。')
        second=self.send('是标题');self.send('再等等');job=self.job()
        frozen=json.loads(job['snapshot_json']);history=frozen['conversation_memory']['messages_after_cursor']
        self.assertTrue(any('调整标题' in item['content'] and item['role']=='assistant' for item in history))
        self.store.execute("UPDATE runs SET status='CANCELLING',terminal_reason='user_cancelled' WHERE id=?",(self.run['id'],))
        self.f.tick();self.assertFalse(messages.pending(self.store,self.run['id']))
        self.assertEqual(self.store.query('SELECT status FROM run_inputs WHERE id=?',(first['id'],))[0]['status'],'REPLIED')
        self.assertEqual(self.store.query('SELECT status FROM run_inputs WHERE id=?',(second['id'],))[0]['status'],'FAILED')
