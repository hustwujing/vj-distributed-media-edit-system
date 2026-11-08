import json
import os
import unittest
from unittest.mock import patch
import requests

from tests import test_dynamic_graph as fixtures
from webapp import semantic_review,task_usage
from webapp.control_intent import ControlIntentClassifier,ControlUnavailable,CRITERIA


class AuxiliaryModelBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.task=self.f.run['id']
        self.usage={'input_tokens':100,'output_tokens':20,'total_tokens':120}

    def client(self,target,body=None,error=None):
        session=self.enterContext(patch(target))
        post=session.return_value.__enter__.return_value.post
        if error:post.side_effect=error
        else:
            response=post.return_value.__enter__.return_value
            response.status_code=200;response.content=b'{}';response.json.return_value=body
        return post

    def classified(self,purpose='plan',choice='discussion'):
        labels=semantic_review.CHOICES[purpose]
        return {'usage':self.usage,'answers':{'decision':{'type':'choice','choice':choice,'confidence':1.,
                'probabilities':{name:float(name==choice) for name in labels}}}}

    def used(self):
        rows=self.store.query("SELECT used FROM execution_budgets WHERE run_id=? AND category='model'",(self.task,))
        return rows[0]['used'] if rows else 0

    def test_semantic_api_call_reserves_counts_and_settles_real_usage(self):
        post=self.client('webapp.semantic_review.requests.Session',self.classified())
        self.assertEqual(semantic_review.classify(self.store,self.task,'plan',{}),'discussion')
        self.assertEqual(post.call_count,1);self.assertEqual(self.used(),1)
        daily=self.store.query("SELECT used FROM execution_daily_usage WHERE owner='owner' AND category='model'")
        self.assertEqual(daily[0]['used'],1)
        reservation=self.store.query('SELECT * FROM workflow_model_reservations WHERE run_id=?',(self.task,))[0]
        self.assertEqual(reservation['used_tokens'],120)
        self.assertEqual((reservation['input_settled_tokens'],reservation['output_settled_tokens']),(100,20))
        self.assertEqual(reservation['attempt_id'],'') # API calls are not fake worker attempts.
        usage=task_usage.snapshot(self.store,self.task)
        self.assertEqual(usage['counts']['llm']['calls'],1)
        self.assertEqual(usage['tokens']['total_tokens']['known'],120)
        self.assertIn({'purpose':'proposal_review','calls':1},usage['model_purposes'])

    def test_semantic_worker_call_keeps_real_attempt(self):
        from webapp.task_leases import Coordinator
        self.f.tick()
        task=self.store.query('SELECT task_id FROM planner_runs WHERE run_id=?',(self.task,))[0]['task_id']
        lease=Coordinator(self.store,'semantic-worker','test').claim(self.task,'workflow',step_id=task)
        self.assertIsNotNone(lease)
        self.client('webapp.semantic_review.requests.Session',self.classified())
        try:
            with lease.bind():semantic_review.classify(self.store,self.task,'plan',{})
        finally:lease.release()
        self.assertEqual(self.used(),1)
        self.assertEqual(self.store.query('SELECT attempt_id FROM workflow_model_reservations WHERE run_id=?',(self.task,))[0]['attempt_id'],lease.attempt)

    def test_daily_quota_blocks_semantic_without_fallback_or_http(self):
        with patch.dict(os.environ,{'VIDEO_USE_USER_MODEL_DAILY':'1'}):
            self.client('webapp.semantic_review.requests.Session',self.classified())
            semantic_review.classify(self.store,self.task,'plan',{})
            with patch('webapp.semantic_review.requests.Session') as network,patch('webapp.semantic_review.fallback_plan') as fallback:
                with self.assertRaises(semantic_review.SemanticUnavailable):semantic_review.classify(self.store,self.task,'plan',{})
                network.assert_not_called();fallback.assert_not_called()
        self.assertEqual(self.used(),1)

    def test_control_call_and_max_calls_share_run_budget_but_buttons_still_work(self):
        from webapp import workflow_graph as graph
        authorization={**self.f.run['authorization'],'max_model_calls':1}
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),self.task))
        value={'usage':self.usage,'answers':{'action':{'type':'choice','choice':'pause','confidence':1.,
               'probabilities':{name:float(name=='pause') for name in CRITERIA}}}}
        post=self.client('webapp.control_intent.requests.Session',value)
        classifier=ControlIntentClassifier()
        with task_usage.control_target(self.store,self.task):
            self.assertEqual(classifier.resolve('暂时不要接着做了','running'),'pause')
            with self.assertRaises(ControlUnavailable):classifier.resolve('暂时不要接着做了','running')
            self.assertEqual(classifier.resolve('取消任务','running'),'cancel')
        self.assertEqual(post.call_count,1);self.assertEqual(self.used(),1)

    def test_token_budget_rejection_does_not_spend_daily_or_task_call(self):
        from webapp import workflow_graph as graph
        authorization={**self.f.run['authorization'],'model_token_budget':1}
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),self.task))
        with patch('webapp.semantic_review.requests.Session') as network:
            with self.assertRaises(semantic_review.SemanticUnavailable):semantic_review.classify(self.store,self.task,'plan',{})
            network.assert_not_called()
        self.assertEqual(self.used(),0)
        self.assertEqual(self.store.query("SELECT * FROM execution_daily_usage WHERE category='model'"),[])

    def test_fallback_is_a_second_metered_call_and_preserves_unknown_first_usage(self):
        self.client('webapp.semantic_review.requests.Session',error=requests.Timeout())
        value={'decision':'discussion','confidence':1.,'requested_work':False,'inputs_identified':False,
               'parameters_resolved':False,'deliverable_defined':False}
        response={'status':'completed','usage':self.usage,'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]}
        post=self.client('webapp.egress_policy.session',response)
        with patch.dict(os.environ,{'VIDEO_USE_MODEL_BASE_URL':'https://provider.invalid','VIDEO_USE_MODEL_API_KEY':'fixture-key'}):
            self.assertEqual(semantic_review.classify(self.store,self.task,'plan',{}),'discussion')
        self.assertEqual(post.call_count,1);self.assertEqual(self.used(),2)
        rows=self.store.query('SELECT used_tokens FROM workflow_model_reservations WHERE run_id=? ORDER BY created_at',(self.task,))
        self.assertEqual([row['used_tokens'] for row in rows],[None,120])

    def test_missing_target_cannot_make_unmetered_control_request(self):
        with patch('webapp.control_intent.requests.Session') as network:
            with self.assertRaises(ControlUnavailable):ControlIntentClassifier().resolve('暂时不要接着做了','running')
            network.assert_not_called()

    def test_concurrent_api_checks_cannot_both_spend_the_last_daily_call(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier=threading.Barrier(2)
        post=self.client('webapp.semantic_review.requests.Session',self.classified())
        def classify():
            barrier.wait(timeout=5)
            try:return semantic_review.classify(self.store,self.task,'plan',{})
            except semantic_review.SemanticUnavailable:return 'blocked'
        with patch.dict(os.environ,{'VIDEO_USE_USER_MODEL_DAILY':'1'}),ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:classify(),range(2)))
        self.assertCountEqual(results,['discussion','blocked'])
        self.assertEqual(post.call_count,1);self.assertEqual(self.used(),1)
