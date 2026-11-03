import json
import unittest
from unittest.mock import patch
from webapp import agent_store,workflows,workflow_review
from webapp.agent_prompt_context import lineage,feedback,budgets,selected_contracts
from webapp.agent_context import role_instructions
from tests import test_workflows as fixtures

class PromptContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.WorkflowTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.s=self.fixture.store;self.p=self.fixture.project;self.t=self.fixture.turn

    def test_lineage_does_not_import_unrelated_or_foreign_tasks(self):
        linked=lineage(self.s,self.p,self.t);self.assertIn(self.fixture.plan,linked)
        agent_store.finish(self.s,self.t,'succeeded','done')
        other=agent_store.add_turn(self.s,self.p,'plan','unrelated');agent_store.finish(self.s,other,'succeeded','other')
        self.assertNotIn(other,lineage(self.s,self.p,self.t))
        foreign=self.s.add_project('someone','private')['id'];f=agent_store.add_turn(self.s,foreign,'plan','secret')
        self.s.execute('UPDATE usage_tasks SET parent_task_id=? WHERE task_id=?',(f,self.t))
        self.assertNotIn(f,lineage(self.s,self.p,self.t))

    def test_feedback_is_linked_versioned_and_excludes_other_project_work(self):
        self.fixture.submit([fixtures.local_step('target')]);w=self.fixture.get()
        self.s.execute('INSERT INTO workflow_quality_events VALUES (?,?,?,?,?,?,?,?)',('q',w['id'],'target',0,'owner','reject','old issue',1.))
        self.s.execute("UPDATE agent_steps SET status='succeeded',revision=1,updated=2 WHERE workflow=?",(w['id'],))
        own=feedback(self.s,self.p,self.t)['items'];self.assertEqual(own[0]['feedback'],'old issue');self.assertTrue(own[0]['step_succeeded_since_feedback'])
        workflows.state(self.s,self.t,'succeeded')
        new=agent_store.add_turn(self.s,self.p,'plan','unrelated')
        self.assertEqual(feedback(self.s,self.p,new)['items'],[])
        agent_store.finish(self.s,new,'succeeded','done')
        child=agent_store.add_turn(self.s,self.p,'plan','change',parent_task_id=self.t)
        self.assertEqual(feedback(self.s,self.p,child)['items'][0]['workflow'],w['id'])

    def test_budget_reads_frozen_limit_and_actual_consumption_without_reset(self):
        self.s.execute('INSERT INTO execution_budgets VALUES (?,?,?)',(self.t,'model',3))
        before=budgets(self.s,self.t)
        with patch.dict('os.environ',{'VIDEO_USE_TASK_MODEL_CALLS':'9999'}):after=budgets(self.s,self.t)
        self.assertEqual(before['task_models'],after['task_models']);self.assertEqual(after['task_models']['used'],3)
        self.assertEqual(after['task_models']['remaining'],after['task_models']['limit']-3)

    def test_budget_tool_rejects_foreign_task(self):
        from webapp.harness_context import lookup
        other=self.s.add_project('other','private')['id'];foreign=agent_store.add_turn(self.s,other,'plan','secret')
        with self.assertRaises(ValueError):lookup(self.s,self.p,{'action':'budget','task':foreign})
        self.assertEqual(lookup(self.s,self.p,{'action':'budget','task':self.t})['task_id'],self.t)

    def test_filtered_capabilities_use_live_nested_schema(self):
        from webapp.operations import catalog
        result=catalog(['media.edit']);self.assertEqual(len(result['operations']),1)
        schema=result['operations'][0]['parameters_schema'];self.assertIn('edit_plan',schema['properties'])
        for names in (['invented'],[],['media.edit']*9,'media.edit'):
            with self.assertRaises(ValueError):catalog(names)

    def test_intent_contracts_include_only_declared_operations(self):
        self.s.execute('INSERT INTO execution_intents VALUES (?,?,?,?)',(self.fixture.plan,json.dumps({'route':'single','operations':['media.edit'],'implementations':{},'goal':'edit','estimated_seconds':20}),'d'*64,1.))
        contract=selected_contracts(self.s,self.t);self.assertEqual(len(contract),1)
        self.assertEqual(contract[0]['id'],'media.edit');self.assertIn('edit_plan',contract[0]['parameters_schema']['properties'])

    def test_single_script_does_not_receive_workflow_only_environment_contract(self):
        for route in ('single','workflow'):
            self.s.execute('INSERT INTO execution_intents VALUES (?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET intent=excluded.intent',
                (self.fixture.plan,json.dumps({'route':route,'operations':['script'],'implementations':{},'goal':'edit','estimated_seconds':20}),'d'*64,1.))
            item=selected_contracts(self.s,self.t)[0]
            if route=='single':
                self.assertIn('assets.json',item['inputs']);self.assertNotIn('VIDEO_USE_STEP_INPUTS',item['inputs']);self.assertIn('deliverables.json',item['outputs'])
            else:self.assertIn('VIDEO_USE_STEP_INPUTS',item['inputs']);self.assertIn('VIDEO_USE_STEP_OUTPUT',item['outputs'])

    def test_role_selection_rejects_unknown_phase_and_keeps_final_protocol_distinct(self):
        with self.assertRaises(ValueError):role_instructions('guess')
        self.assertIn('只读',role_instructions('plan'))
        self.assertIn('request_id',role_instructions('execute','review'))
        self.assertIn('deliveries',role_instructions('execute','final'))
        self.assertNotIn('本轮角色：执行已确认方案',role_instructions('execute','diagnose'))

    def test_target_review_omits_unrelated_details_but_query_does_not_expand_repair_scope(self):
        steps=[fixtures.local_step('up'),fixtures.local_step('target',['up']),fixtures.local_step('unrelated')]
        self.fixture.submit(steps);w=self.fixture.get();workflow_review.queue(self.s,w,w['steps'][1],'diagnose')
        c=workflow_review.context(self.s,self.t)
        self.assertEqual({s['id'] for s in c['steps']},{'up','target'})
        self.assertEqual([s['id'] for s in c['other_steps']],['unrelated'])
        full=workflow_review.dispatch(self.s,self.t,{'action':'status','steps':['unrelated']},'plan')['review_context']
        self.assertEqual([s['id'] for s in full['steps']],['unrelated']);self.assertNotIn('unrelated',full['repairable_steps'])
        with self.assertRaises(ValueError):workflow_review.dispatch(self.s,self.t,{'action':'status','steps':['missing']},'plan')

    def test_final_review_retains_all_step_evidence(self):
        self.fixture.submit([fixtures.local_step('a'),fixtures.local_step('b')]);w=self.fixture.get();workflow_review.queue(self.s,w,purpose='final')
        c=workflow_review.context(self.s,self.t);self.assertEqual({s['id'] for s in c['steps']},{'a','b'});self.assertEqual(c['other_steps'],[])
        self.assertIn('decision_schema',c);self.assertIn('budget',c)

    def test_diagnostic_truncation_preserves_structured_exit_and_does_not_invent_oom(self):
        self.fixture.submit([fixtures.local_step('target')]);w=self.fixture.get()
        self.s.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',('d',w['id'],'target',0,1,json.dumps({'phase':'local','exit_code':-9,'log_tail':'x'*10000}),1.))
        workflow_review.queue(self.s,w,w['steps'][0],'diagnose');c=workflow_review.context(self.s,self.t);target=c['steps'][0]
        self.assertEqual(target['failure_evidence'][0]['exit_code'],-9);self.assertNotIn('oom_kill',target['failure_evidence'][0])
        self.assertTrue(target['diagnostics'][0]['log_tail_truncated']);self.assertEqual(len(target['diagnostics'][0]['log_tail']),4000)

    def test_media_guidance_does_not_change_user_prompt_or_claim_exact_motion(self):
        from webapp.operations import catalog
        from webapp.image_document import edit_prompt
        info=catalog(['video.generate_from_image'])['operations'][0]['prompt_guidance']
        self.assertIn('首帧',info['reference_role'])
        prompt='保留杯子，删除标签，背景改为浅蓝色'
        self.assertEqual(edit_prompt('generate',prompt),prompt)
        compiled=edit_prompt('erase',prompt,selected=True)
        self.assertEqual(compiled.count(prompt),1);self.assertIn('图2',compiled);self.assertIn('只修改选区',compiled)

    def test_diagnosis_records_context_size_but_not_raw_prompt(self):
        from webapp.diagnostics import diagnose
        agent_store.event(self.s,self.t,'prompt_context',{'schema':'agent-context-v2','phase':'plan','characters':456,'native_resume':False,'prompt':'PRIVATE_TEXT'})
        report=diagnose(self.s,self.t,include_logs=False)
        self.assertEqual(report['prompt_contexts'][0]['characters'],456)
        self.assertNotIn('PRIVATE_TEXT',json.dumps(report))

    def test_project_notes_pagination_keeps_full_constraints_and_owner_scope(self):
        from webapp.harness_context import lookup
        foreign=self.s.add_project('foreign','private')['id']
        self.s.execute('CREATE TABLE IF NOT EXISTS edit_notes(project TEXT,start REAL,"end" REAL,text TEXT,kind TEXT,created REAL)')
        for i in range(25):self.s.execute('INSERT INTO edit_notes(project,start,"end",text,kind,created) VALUES (?,?,?,?,?,?)',(self.p,i,i+1,'constraint-'+str(i),'note',i))
        self.s.execute('INSERT INTO edit_notes(project,start,"end",text,kind,created) VALUES (?,?,?,?,?,?)',(foreign,0,1,'FOREIGN_SECRET','note',0))
        first=lookup(self.s,self.p,{'action':'project_data','kind':'notes'})
        second=lookup(self.s,self.p,{'action':'project_data','kind':'notes','offset':first['next_offset']})
        self.assertEqual(len(first['items']),20);self.assertEqual(len(second['items']),5)
        self.assertNotIn('FOREIGN_SECRET',json.dumps([first,second]));self.assertEqual(second['items'][-1]['text'],'constraint-24')
        with self.assertRaises(ValueError):lookup(self.s,self.p,{'action':'project_data','kind':'agent_turns'})

    def test_full_feedback_is_retrievable_when_inline_excerpt_is_truncated(self):
        from webapp.harness_context import lookup
        self.fixture.submit([fixtures.local_step('target')]);w=self.fixture.get()
        text='x'*1600+'KEEP THE FINAL CONSTRAINT'
        self.s.execute('INSERT INTO workflow_quality_events VALUES (?,?,?,?,?,?,?,?)',('long',w['id'],'target',0,'owner','reject',text,1.))
        short=feedback(self.s,self.p,self.t);self.assertTrue(short['items'][0]['feedback_truncated'])
        complete=lookup(self.s,self.p,{'action':'feedback','task':self.t,'offset':0})
        self.assertEqual(complete['items'][0]['feedback'],text)
        self.assertFalse(complete['items'][0]['feedback_truncated'])

    def test_handoff_tool_evidence_scopes_to_explicit_ancestry(self):
        from webapp.harness_context import build
        agent_store.event(self.s,self.fixture.plan,'tool',{'id':'linked','status':'completed','output':'linked result'})
        agent_store.finish(self.s,self.t,'succeeded','done');unrelated=agent_store.add_turn(self.s,self.p,'plan','other')
        agent_store.event(self.s,unrelated,'tool',{'id':'unrelated','status':'completed','output':'unrelated instruction'})
        c=build(self.s,self.p,self.t);text=json.dumps(c['tool_evidence']);self.assertIn('linked result',text);self.assertNotIn('unrelated instruction',text)
