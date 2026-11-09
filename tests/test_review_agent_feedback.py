import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_dynamic_gateway as gateway_fixtures,test_dynamic_graph as graph_fixtures
from webapp import workflow_graph as graph
from webapp.workflow_agents import execute
from webapp.task_leases import Coordinator,TaskQuotaReached


class DraftAndOutputFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.f=gateway_fixtures.DynamicGatewayTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.store=self.f.store;self.lease=self.f.lease;self.run=self.f.run;self.task=self.f.task
        self.host=SimpleNamespace(store=self.store,stop=threading.Event())

    def duplicate(self):
        task={'id':'same','type':'agent','profile':'editing_implementer','name':'report','objective':'Write report',
              'files':[{'path':'report.txt','roles':['report']}],'acceptance':[{'kind':'file'}],'basic_reason':'Local report'}
        return {'action':'draft_add_tasks','command':{'tasks':[task,task]}}

    def test_duplicate_draft_id_feedback_allows_corrected_plan(self):
        good=self.f.bind_reply({'action':'proposal','proposal':{'proposal_id':'corrected','base_graph_version':0,
            'reason':'Correct duplicate','add_tasks':[graph_fixtures.script()],'sealed':True}})
        replies=[self.duplicate(),good]
        def respond(*args,**kwargs):
            if len(replies)==1:
                self.assertIn('duplicate_task_id',args[4])
                self.assertFalse(self.store.query('SELECT id FROM run_tasks WHERE run_id=?',(self.run['id'],)))
                draft=self.store.query('SELECT draft_json FROM workflow_plan_drafts')[0]
                self.assertEqual(json.loads(draft['draft_json'])['new_tasks'],[])
            return replies.pop(0)
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond):
            execute(self.host,self.lease,self.run,self.task)
        self.assertEqual(self.store.query('SELECT status FROM planner_runs')[0]['status'],'staged')

    def test_second_identical_duplicate_draft_error_is_bounded(self):
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=[self.duplicate(),self.duplicate()]) as model:
            with self.assertRaises(TaskQuotaReached) as blocked:execute(self.host,self.lease,self.run,self.task)
        self.assertEqual(model.call_count,2)
        self.assertIn('duplicate_task_id',model.call_args_list[1].args[4])
        self.assertEqual(blocked.exception.diagnostic['category'],'agent_contract')
        self.assertEqual(self.store.query('SELECT repeated_error_count FROM workflow_plan_drafts')[0]['repeated_error_count'],2)
        self.assertFalse(self.store.query('SELECT id FROM run_tasks WHERE run_id=?',(self.run['id'],)))

    def test_partial_script_output_names_reach_next_model_input(self):
        from webapp.specialist_profiles import get
        policy=get('editing_implementer')
        task={**self.task,'control_kind':'','profile':policy.snapshot(),'spec':{'profile':policy.id,
            'inputs':{'source':{'asset':'asset'}},'output_roles':{'report':'report.txt','notes':'notes.txt'},'timeout_seconds':900}}
        receipt={'returncode':0,'outputs':[{'name':'report.txt','state':'present','size':6},{'name':'notes.txt','state':'missing'}]}
        replies=[{'action':'script','code':'partial output'},{'action':'result','files':{'notes.txt':'complete notes'}}]
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=replies) as model, \
             patch('webapp.workflow_agents.run_script',return_value=('edit/workflows/output',receipt)), \
             patch('webapp.workflow_agents.finish_files',return_value='candidate'):
            execute(self.host,self.lease,self.run,task)
        self.assertEqual(model.call_count,2)
        self.assertIn('output_missing',model.call_args_list[1].args[4])
        self.assertIn('notes.txt',model.call_args_list[1].args[4])


class FinalReviewerExecuteTests(unittest.TestCase):
    def test_reference_only_method_pass_is_corrected_and_controller_rejects_forgery(self):
        f=graph_fixtures.DynamicGraphTests();f.setUp();self.addCleanup(f.tearDown)
        store=f.store;run=f.review_fixture()
        store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=?",(run['id'],))
        store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
            (run['id'],'first',0,f.f.project,'completed',graph.encode({'edit/report.txt':{'sha256':'a'*64,'size':6}}),store.now()))
        f.orchestrator.queue_review(graph.get(store,run['id']))
        review=store.query("SELECT * FROM workflow_reviews WHERE purpose='final'")[0]
        lease=Coordinator(store,'method-review-loop','test').claim(run['id'],'workflow',step_id=review['task_id'])
        self.assertIsNotNone(lease);current=graph.get(store,run['id']);task=graph.work_item(current,review['task_id']);calls=[]
        def respond(*args,**kwargs):
            calls.append(args[4])
            if len(calls)==1:return {'action':'inspect','ref':'edit/report.txt'}
            if len(calls)==3:self.assertIn('方法契约未满足',args[4])
            inspection=store.query('SELECT id FROM workflow_inspections WHERE attempt_id=?',(lease.attempt,))[0]['id']
            return {'action':'result','decision':{'snapshot_digest':review['snapshot_digest'],
                'verdict':'pass' if len(calls)==2 else 'reject',
                'evidence':[{'criterion_index':0,'inspection_id':inspection,'method_use':'reference_only',
                             'observation':'The method output was used only to verify the extent, not as output content.'}],
                'deliverables':[],'primary':'','feedback':'Required method output was not used.'}}
        try:
            with lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond), \
                 patch('webapp.workflow_agents.inspect',return_value={'ref':'edit/report.txt','text':'report'}):
                execute(SimpleNamespace(store=store,stop=threading.Event()),lease,current,task)
            result=json.loads(store.query('SELECT result_json FROM task_results WHERE task_id=?',(task['id'],))[0]['result_json'])
            forged={**result['decision'],'verdict':'pass','deliverables':['edit/report.txt'],'primary':'edit/report.txt'}
            with self.assertRaisesRegex(ValueError,'方法契约未满足'):
                f.orchestrator.accept_review(graph.get(store,run['id']),task,{'decision':forged})
            self.assertNotEqual(store.query('SELECT status FROM workflow_reviews WHERE id=?',(review['id'],))[0]['status'],'passed')
        finally:lease.release()
        self.assertEqual(len(calls),3)
        f.orchestrator.accept_results(graph.get(store,run['id']))
        saved=store.query('SELECT status,decision_json FROM workflow_reviews WHERE id=?',(review['id'],))[0]
        self.assertEqual(saved['status'],'rejected')
        self.assertEqual(json.loads(saved['decision_json'])['evidence'][0]['method_use'],'reference_only')
        self.assertFalse(graph.get(store,run['id'])['delivery'])

    def test_empty_delivery_targets_pass_via_real_agent_loop_and_controller(self):
        f=graph_fixtures.DynamicGraphTests();f.setUp();self.addCleanup(f.tearDown)
        store=f.store;run=f.review_fixture()
        authorization={**run['authorization'],'delivery_targets':[]}
        store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),run['id']))
        store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(graph.encode(authorization),run['id']))
        store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=?",(run['id'],))
        store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
            (run['id'],'first',0,f.f.project,'completed',graph.encode({'edit/report.txt':{'sha256':'a'*64,'size':6}}),store.now()))
        run=graph.get(store,run['id']);f.orchestrator.queue_review(run)
        review=store.query("SELECT * FROM workflow_reviews WHERE purpose='final'")[0]
        lease=Coordinator(store,'final-review-loop','test').claim(run['id'],'workflow',step_id=review['task_id'])
        self.assertIsNotNone(lease)
        current=graph.get(store,run['id']);task=graph.work_item(current,review['task_id'])
        calls=[]
        def respond(*args,**kwargs):
            calls.append(args[4])
            if len(calls)==1:return {'action':'inspect','ref':'edit/report.txt'}
            inspection=store.query('SELECT id FROM workflow_inspections WHERE attempt_id=?',(lease.attempt,))[0]['id']
            return {'action':'result','decision':{'snapshot_digest':review['snapshot_digest'],'verdict':'pass',
                'evidence':[{'criterion_index':0,'inspection_id':inspection,'method_use':'not_applicable','observation':'The report meets the goal'}],
                'deliverables':['delivery_1'],'primary':'delivery_1','feedback':''}}
        try:
            with lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond), \
                 patch('webapp.workflow_agents.inspect',return_value={'ref':'edit/report.txt','text':'report'}):
                execute(SimpleNamespace(store=store,stop=threading.Event()),lease,current,task)
        finally:lease.release()
        self.assertEqual(len(calls),2)
        f.orchestrator.accept_results(graph.get(store,run['id']))
        self.assertEqual(store.query('SELECT status FROM workflow_reviews WHERE id=?',(review['id'],))[0]['status'],'passed')
        result=json.loads(store.query('SELECT result_json FROM task_results WHERE task_id=?',(review['task_id'],))[0]['result_json'])
        self.assertIn(run['goal'],result['decision']['evidence'][0]['criterion'])
