import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

from webapp.runtime_bootstrap import create_runtime,load_config,verify_runtime_bundle


class RuntimeBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.token=self.root/'control.token';self.token.write_text('s'*48);self.token.chmod(0o600)
        self.harness=self.root/'harness.json';self.harness.write_text('{}');self.harness.chmod(0o600)
        self.config=self.root/'runtime.json'
        repo=Path(__file__).resolve().parents[1];self.bundle_root=self.root/'bundle';self.bundle_root.mkdir()
        self.bundle=json.loads((repo/'runtime/public/bundle.json').read_text())
        spec=json.loads((repo/'runtime/opensandbox/release-spec.json').read_text())
        shutil.copyfile(repo/'runtime/public/bundle.json',self.bundle_root/'bundle.json')
        for item in spec['skill_bundle']:
            target=self.bundle_root/item['target'];target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(repo/item['source'],target)

    def write(self,harness='codex'):
        value={'identity':{'attempt_id':'attempt-1','generation':1,'sandbox_id':'sandbox-1'},
            'harness':harness,'profile':{'model':'model-1','reasoning_effort':'high'},
            'binding':{'harness':harness,'runtime_profile':'agent-v1',
                'image_digest':'registry/agent@sha256:'+'a'*64,
                'skill_digest':self.bundle['skill_digest'],
                'tool_schema_digest':self.bundle['tool_schema_digest'],'gateway_protocol':'gateway-v1'},
            'control_token_file':str(self.token),'harness_config_file':str(self.harness)}
        self.config.write_text(json.dumps(value));self.config.chmod(0o600);return value

    def test_bootstrap_wires_registered_harness_without_starting_it(self):
        self.write();app=create_runtime(self.config,bundle_root=self.bundle_root)
        with TestClient(app) as client:
            health=client.get('/healthz').json()
            self.assertEqual((health['ready'],health['state'],health['harness']),
                (True,'created','codex'))
            self.assertNotIn('s'*16,json.dumps(health))
            command=app.state.harness_controller.bridge.command
            self.assertEqual(command,['/opt/codex','app-server','--listen','stdio://'])

    def test_sdk_harness_uses_fixed_bridge_command(self):
        self.write('claude_code');config=load_config(self.config,bundle_root=self.bundle_root)
        command=config['adapter'].runtime_launch(config['harness_config'])
        self.assertEqual(command[:4],['/opt/venv/bin/python','-m','webapp.harness_bridge','claude_code'])
        self.assertEqual(command[-1],str(self.harness))

    def test_native_task_gets_the_same_declared_script_environment(self):
        self.write('claude_code')
        workspace={'output':'/workspace/edit/workflows/run/task/attempt','inputs':{'source':['/workspace/sources/a.mp4']}}
        self.harness.write_text(json.dumps({'task_workspace':workspace}))
        app=create_runtime(self.config,bundle_root=self.bundle_root)
        environment=app.state.harness_controller.bridge.environment
        self.assertEqual(environment['VIDEO_USE_STEP_OUTPUT'],workspace['output'])
        self.assertEqual(environment['VIDEO_USE_STEP_SCRATCH'],workspace['output']+'/.scratch')
        self.assertEqual(json.loads(environment['VIDEO_USE_STEP_INPUTS']),workspace['inputs'])
        self.assertIn('/opt/video-use/helpers',environment['PYTHONPATH'])
        self.assertIn('/opt/harness',environment['PYTHONPATH'].split(':'))
        for value in ('/etc/passwd','/workspace/../outside'):
            self.harness.write_text(json.dumps({'task_workspace':{**workspace,'inputs':{'source':[value]}}}))
            with self.assertRaisesRegex(ValueError,'workspace contract'):create_runtime(self.config,bundle_root=self.bundle_root)

    def test_gateway_grant_starts_only_a_loopback_client(self):
        value=self.write();gateway_token=self.root/'gateway.token';gateway_ca=self.root/'gateway-ca.pem'
        gateway_config=self.root/'gateway.json'
        gateway_token.write_text('g'*48);gateway_ca.write_text('public-ca')
        gateway_token.chmod(0o600);gateway_ca.chmod(0o600)
        gateway_config.write_text(json.dumps({'identity':value['identity'],
            'endpoint':'https://172.30.0.1:18880','allowed_routes':['/v1/responses'],
            'token_file':str(gateway_token),'ca_file':str(gateway_ca)}));gateway_config.chmod(0o600)
        value['gateway_config_file']=str(gateway_config)
        self.config.write_text(json.dumps(value));self.config.chmod(0o600)
        app=create_runtime(self.config,gateway_port=0,bundle_root=self.bundle_root)
        with TestClient(app) as client:
            self.assertEqual(app.state.gateway_server.address[0],'127.0.0.1')
            self.assertEqual(client.get('/healthz').status_code,200)

    def test_group_readable_or_outside_files_are_rejected(self):
        self.write();self.token.chmod(0o640)
        with self.assertRaisesRegex(ValueError,'owner-only'):load_config(self.config,bundle_root=self.bundle_root)
        self.token.chmod(0o600)
        outside=Path(self.temporary.name).parent/'outside-runtime-token'
        try:
            outside.write_text('x'*48);outside.chmod(0o600)
            value=self.write();value['control_token_file']=str(outside)
            self.config.write_text(json.dumps(value));self.config.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'outside'):load_config(self.config,bundle_root=self.bundle_root)
        finally:
            if outside.exists():outside.unlink()

    def test_binding_and_identity_must_match_registered_harness(self):
        value=self.write();value['binding']['harness']='claude_code'
        self.config.write_text(json.dumps(value));self.config.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'frozen'):load_config(self.config,bundle_root=self.bundle_root)

    def test_skill_bundle_content_is_bound_to_runtime_configuration(self):
        value=self.write();value['binding']['skill_digest']='sha256:'+'d'*64
        self.config.write_text(json.dumps(value));self.config.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'Skill Bundle'):
            load_config(self.config,bundle_root=self.bundle_root)


if __name__=='__main__':unittest.main()
