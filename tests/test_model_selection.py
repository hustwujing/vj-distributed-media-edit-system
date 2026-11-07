import copy
import json
import os
import unittest
import uuid
from unittest.mock import patch
from tests.test_dynamic_api import DynamicAPITests
from webapp import agent_profiles,workflow_graph as graph


class ModelSelectionTests(unittest.TestCase):
    body=DynamicAPITests.body

    def setUp(self):
        DynamicAPITests.setUp(self)
        self.registry={
            'claude':{'harness':'claude_code','model':'claude-4.6-sonnet','protocol':'anthropic',
                'base_url':'https://models.example/v1','credential_env':'ANTHROPIC_API_KEY','reasoning_effort':'high','input_token_policy':'claude_bpe_estimate_v1'},
            'claude55':{'harness':'claude_code','model':'claude-sonnet-5-5','protocol':'anthropic',
                'base_url':'https://models.example/v1','credential_env':'ANTHROPIC_API_KEY','reasoning_effort':'high','input_token_policy':'claude_bpe_estimate_v1'},
            'deepseek41':{'harness':'deepseek_harness','model':'deepseek-v4.1-flash','protocol':'anthropic',
                'base_url':'https://models.example/v1','credential_env':'ANTHROPIC_API_KEY','reasoning_effort':'high','input_token_policy':'deepseek_bpe_estimate_v1'}}
        active=patch.dict(os.environ,{'VIDEO_USE_AGENT_PROFILES_JSON':json.dumps(self.registry),'VIDEO_USE_AGENT_PROFILE':'claude',
            'VIDEO_USE_USER_AGENT_PROFILE':'claude55','ANTHROPIC_API_KEY':'test-model-selection-secret'})
        active.start();self.addCleanup(active.stop)

    def choice(self,name='claude',effort='high'):
        options=self.a.get(self.prefix+'/capabilities').json()['model_choices']
        profile=next(p for p in options['profiles'] if p['id']==name)
        return {'profile':name,'reasoning_effort':effort,'profile_digest':profile['profile_digest']}

    def test_defaults_and_public_options_do_not_expose_routes_or_credentials(self):
        result=self.a.get(self.prefix+'/capabilities').json();options=result['model_choices']
        self.assertEqual((options['default_profile'],options['default_effort']),('claude55','high'))
        raw=json.dumps(options);self.assertNotIn('models.example',raw);self.assertNotIn('test-model-selection-secret',raw);self.assertNotIn('credential_env',raw)
        self.assertEqual(self.b.get(self.prefix+'/capabilities').status_code,404)
        run=self.a.post(self.prefix,json={**self.body(),'model_choice':self.choice('claude55')}).json()
        self.assertEqual((run['model_configuration']['model'],run['model_configuration']['reasoning_effort']),('claude-sonnet-5-5','high'))
        self.assertIn('editing_implementer',run['model_configuration']['roles'])

    def test_sandbox_choice_and_system_expert_models_are_frozen_separately(self):
        response=self.a.post(self.prefix,json={**self.body(),'model_choice':self.choice('deepseek41','low')})
        self.assertEqual(response.status_code,202,response.text);run=response.json();frozen=run['model_configuration']['roles']
        self.assertEqual(frozen['media_planner']['model'],'claude-4.6-sonnet')
        self.assertEqual(frozen['editing_implementer']['model'],'deepseek-v4.1-flash')
        self.assertEqual(frozen['editing_implementer']['reasoning_effort'],'low')
        self.assertEqual(frozen['media_planner']['response_transport'],'buffered')
        self.assertEqual(frozen['visual_quality_reviewer']['response_transport'],'buffered')
        self.assertEqual(frozen['editing_implementer']['response_transport'],'stream')
        self.assertEqual(frozen['media_failure_diagnostician']['model'],'claude-4.6-sonnet')
        self.assertEqual(agent_profiles.for_specialist(self.app.state.store,run['id'],'editing_implementer').model,'deepseek-v4.1-flash')
        rows=self.app.state.store.query('SELECT specialist_profile,digest FROM agent_run_role_profiles WHERE task_id=?',(run['id'],))
        self.assertEqual(len(rows),5);self.assertTrue(all(len(row['digest'])==64 for row in rows))

    def test_expert_models_are_system_config_only(self):
        configured=json.dumps({'media_planner':'deepseek41','visual_quality_reviewer':'claude',
            'media_failure_diagnostician':'deepseek41','delivery_reviewer':'claude'})
        with patch.dict(os.environ,{'VIDEO_USE_SPECIALIST_MODEL_PROFILES_JSON':configured}):
            run=self.a.post(self.prefix,json={**self.body(),'model_choice':self.choice('claude','low')}).json()
        roles=run['model_configuration']['roles']
        self.assertEqual(roles['media_planner']['model'],'deepseek-v4.1-flash')
        self.assertEqual(roles['media_failure_diagnostician']['model'],'deepseek-v4.1-flash')
        self.assertEqual(roles['editing_implementer']['model'],'claude-4.6-sonnet')
        forged={**self.body(),'role_model_choices':{'media_planner':self.choice('claude')}}
        self.assertEqual(self.a.post(self.prefix,json=forged).status_code,422)

    def test_choice_is_frozen_and_idempotent_replay_survives_registry_change(self):
        body={**self.body(),'model_choice':self.choice('deepseek41','low')}
        response=self.a.post(self.prefix,json=body);self.assertEqual(response.status_code,202,response.text);run=response.json()
        frozen=run['model_configuration'];self.assertEqual((frozen['model'],frozen['reasoning_effort']),('deepseek-v4.1-flash','low'))
        self.registry['deepseek41']['reasoning_effort']='max';os.environ['VIDEO_USE_AGENT_PROFILES_JSON']=json.dumps(self.registry)
        replay=self.a.post(self.prefix,json=body);self.assertEqual(replay.status_code,202,replay.text)
        self.assertEqual(replay.json()['model_configuration'],frozen)
        self.assertEqual(self.a.get(self.prefix+'/'+run['id']).json()['model_configuration'],frozen)
        changed=copy.deepcopy(body);changed['model_choice']['reasoning_effort']='high'
        self.assertEqual(self.a.post(self.prefix,json=changed).status_code,409)
        self.assertEqual(len(self.app.state.store.query('SELECT id FROM runs')),1)

    def test_stale_unavailable_and_unregistered_choices_create_nothing(self):
        choice=self.choice()
        invalid=[{**choice,'profile':'unknown'},{**choice,'profile_digest':'0'*64},{**choice,'reasoning_effort':'unlimited'}]
        for value in invalid:
            response=self.a.post(self.prefix,json={**self.body(),'model_choice':value})
            self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.a.post(self.prefix,json={**self.body(),'model_choice':self.choice('deepseek41','medium')}).status_code,409)
        with patch.dict(os.environ,{'ANTHROPIC_API_KEY':''}):
            self.assertEqual(self.a.post(self.prefix,json={**self.body(),'model_choice':choice}).status_code,409)
        for field in ('base_url','api_key','harness','model'):
            self.assertEqual(self.a.post(self.prefix,json={**self.body(),'model_choice':{**choice,field:'forged'}}).status_code,422)
        self.assertFalse(self.app.state.store.query('SELECT id FROM runs'))
        self.assertFalse(self.app.state.store.query('SELECT task_id FROM agent_run_profiles'))

    def test_automatic_successor_inherits_frozen_choice_despite_new_project_preference(self):
        first=self.a.post(self.prefix,json={**self.body(),'model_choice':self.choice('deepseek41','low')}).json()
        store=self.app.state.store;store.execute("UPDATE runs SET status='SUCCEEDED' WHERE id=?",(first['id'],))
        store.execute('INSERT INTO agent_project_profiles VALUES (?,?)',(self.project,'claude'))
        self.registry['deepseek41']['reasoning_effort']='max';os.environ['VIDEO_USE_AGENT_PROFILES_JSON']=json.dumps(self.registry)
        child=graph.create(store,self.project,uuid.uuid4().hex,first['authorization'],parent_run_id=first['id'])
        self.assertEqual(agent_profiles.for_task(store,child['id']).digest,first['model_configuration']['configuration_digest'])

    def test_explicit_new_run_can_choose_a_different_model_than_its_parent(self):
        first=self.a.post(self.prefix,json={**self.body(),'model_choice':self.choice('deepseek41','low')}).json()
        self.app.state.store.execute("UPDATE runs SET status='SUCCEEDED' WHERE id=?",(first['id'],))
        response=self.a.post(self.prefix,json={**self.body(),'parent_run_id':first['id'],'model_choice':self.choice('claude','low')})
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual((response.json()['model_configuration']['model'],response.json()['model_configuration']['reasoning_effort']),('claude-4.6-sonnet','low'))

    def test_missing_historical_configuration_is_not_replaced_with_current_default(self):
        run=self.a.post(self.prefix,json=self.body()).json()
        self.app.state.store.execute('DELETE FROM agent_run_profiles WHERE task_id=?',(run['id'],))
        self.assertIsNone(self.a.get(self.prefix+'/'+run['id']).json()['model_configuration'])
