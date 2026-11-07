import json
import unittest
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.f.tick()
        self.f.propose({'proposal_id':'initial','base_graph_version':0,'reason':'Required report chain',
            'add_tasks':[fixtures.script('first'),fixtures.script('child',inputs={'source':{'step':'first','role':'report'}}),fixtures.script('unrelated')],
            'add_dependencies':[{'upstream':'first','downstream':'child'}],'sealed':True})
        self.run=self.f.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='execution_failed' WHERE run_id=? AND id='first'",(self.run['id'],))
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='unrelated'",(self.run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(self.run['id'],))
        self.f.orchestrator.open_repair(graph.get(self.store,self.run['id']),'first','diagnosis')
        self.f.tick()

    def test_replacement_preserves_failure_and_revises_downstream_binding(self):
        self.f.propose({'proposal_id':'replacement','base_graph_version':1,'reason':'Replace failed implementation',
            'add_tasks':[fixtures.script('replacement',argv=['python','-c','print("repaired report")'])],'replacements':{'first':'replacement'},'sealed':True})
        run=self.f.tick();tasks={task['id']:task for task in run['tasks']}
        self.assertEqual((tasks['first']['selected'],tasks['first']['status']),(0,'paused'))
        self.assertEqual((tasks['replacement']['selected'],tasks['replacement']['repair_count']),(1,1))
        self.assertEqual(tasks['child']['revision'],1)
        self.assertEqual(tasks['child']['spec']['inputs']['source']['step'],'replacement')
        self.assertEqual((tasks['unrelated']['revision'],tasks['unrelated']['status']),(0,'succeeded'))
        selected=self.store.query('SELECT task_id FROM workflow_plan_tasks WHERE run_id=? AND graph_version=2',(run['id'],))
        self.assertNotIn('first',{row['task_id'] for row in selected})
        old=self.store.query("SELECT retired_graph_version FROM workflow_dependencies WHERE upstream_task_id='first'")[0]
        self.assertEqual(old['retired_graph_version'],2)
        self.assertEqual(self.store.query('SELECT new_task_id FROM run_task_replacements')[0]['new_task_id'],'replacement')
        from webapp.workflow_api import public
        response=public(self.store,run)
        retained=next(task for task in response['tasks'] if task['id']=='first')
        self.assertFalse(retained['selected'])
        self.assertEqual(retained['spec']['id'],'first')
        self.assertEqual(retained['candidate_result'],[])
        self.assertEqual(response['replacements'][0]['new_task_id'],'replacement')
        self.assertNotIn('first',{task['id'] for task in graph.work_items(run)})
        from webapp.workflow_history import snapshot
        history=snapshot(self.store,run)
        original=next(item for item in history['task_revisions'] if item['task_id']=='first')
        self.assertEqual(original['spec']['objective'],tasks['first']['spec']['objective'])
        self.assertTrue(any(edge['upstream_task_id']=='first' and edge['retired_graph_version']==2 for edge in history['dependencies']))
        self.assertTrue(any(edge['upstream_task_id']=='replacement' and edge['retired_graph_version'] is None for edge in history['dependencies']))
        self.assertEqual(history['retired_tasks'][0]['id'],'first')
        self.assertFalse(history['truncated'])

    def test_similar_new_node_does_not_erase_the_failed_requirement(self):
        self.f.propose({'proposal_id':'not-a-replacement','base_graph_version':1,'reason':'Merely append a node',
            'add_tasks':[fixtures.script('lookalike')],'sealed':True})
        run=self.f.tick()
        self.assertEqual(run['graph_version'],1)
        self.assertTrue(next(task for task in run['tasks'] if task['id']=='first')['selected'])

    def test_replacement_cannot_weaken_acceptance_or_reset_repair_limit(self):
        run=graph.get(self.store,self.run['id'])
        raw={'proposal_id':'bad','base_graph_version':1,'reason':'Replacement',
             'add_tasks':[fixtures.script('replacement',objective='An easier unrelated goal')],
             'replacements':{'first':'replacement'},'sealed':True}
        with self.assertRaisesRegex(ValueError,'requirement'):graph.validate_proposal(self.store,run,raw,repair_scope='first')
        self.store.execute("UPDATE run_tasks SET repair_count=2 WHERE id='first'")
        raw['add_tasks']=[fixtures.script('replacement')]
        with self.assertRaisesRegex(ValueError,'budget'):
            graph.validate_proposal(self.store,graph.get(self.store,run['id']),raw,repair_scope='first')

    def test_user_replacement_cannot_weaken_the_original_requirement(self):
        run=graph.get(self.store,self.run['id'])
        raw={'proposal_id':'bad-user-replacement','base_graph_version':1,'reason':'Change implementation',
             'add_tasks':[fixtures.script('replacement',objective='A weaker unrelated goal')],
             'replacements':{'first':'replacement'},'sealed':True}
        with self.assertRaisesRegex(ValueError,'requirement'):
            graph.validate_proposal(self.store,run,raw,user_revision_scope={'first','child'})

    def test_raw_proposal_cannot_bind_new_work_to_a_retired_replacement_source(self):
        run=graph.get(self.store,self.run['id'])
        raw={'proposal_id':'stale-replacement-reference','base_graph_version':1,'reason':'Replace and continue',
             'add_tasks':[fixtures.script('replacement'),fixtures.script('new-child',inputs={'source':{'step':'first','role':'report'}})],
             'add_dependencies':[{'upstream':'first','downstream':'new-child'}],
             'replacements':{'first':'replacement'},'sealed':True}
        with self.assertRaisesRegex(ValueError,'replacement Task|retired Task'):
            graph.validate_proposal(self.store,run,raw,user_revision_scope={'first','child'})


class ReviewIdentityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.run=self.f.review_fixture()
        self.f.orchestrator.queue_review(self.run,'first')
        self.store.execute("UPDATE workflow_reviews SET status='rejected'")
        self.store.execute("UPDATE control_jobs SET status='succeeded'")

    def test_renaming_or_empty_graph_version_does_not_reroll_a_verdict(self):
        task=self.run['tasks'][0];task['spec']['name']='Renamed task'
        self.store.execute('UPDATE run_tasks SET spec_json=? WHERE id=?',(graph.encode(task['spec']),'first'))
        self.store.execute('UPDATE runs SET graph_version=graph_version+1')
        self.f.orchestrator.queue_review(graph.get(self.store,self.run['id']),'first')
        self.assertEqual(len(self.store.query('SELECT id FROM workflow_reviews')),1)
        self.assertEqual(self.store.query("SELECT kind FROM workflow_decisions WHERE status='pending'")[0]['kind'],'quality_feedback')

    def test_new_revision_number_without_new_content_does_not_reroll(self):
        self.store.execute("UPDATE run_tasks SET revision=1 WHERE id='first'")
        self.f.orchestrator.queue_review(graph.get(self.store,self.run['id']),'first')
        self.assertEqual(len(self.store.query('SELECT id FROM workflow_reviews')),1)

    def test_unrelated_authorization_content_does_not_reroll_a_verdict(self):
        authorization={**self.run['authorization'],'content_fingerprint':'f'*64}
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),self.run['id']))
        self.f.orchestrator.queue_review(graph.get(self.store,self.run['id']),'first')
        self.assertEqual(len(self.store.query('SELECT id FROM workflow_reviews')),1)

    def test_new_content_can_be_reviewed(self):
        self.store.execute("UPDATE run_tasks SET revision=1 WHERE id='first'")
        self.store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
            (self.run['id'],'first',1,self.run['project_id'],'new-content',
             graph.encode({'edit/report.txt':{'sha256':'a'*64}}),self.store.now()))
        self.f.orchestrator.queue_review(graph.get(self.store,self.run['id']),'first')
        self.assertEqual(len(self.store.query('SELECT id FROM workflow_reviews')),2)
