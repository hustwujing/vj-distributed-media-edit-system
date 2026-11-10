from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy import dynamic_cluster as cluster


class SandboxDeploymentProfileTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        values={
            'NAMESPACE':'sandbox-agent','ROOT':root/'cluster','CONF':root/'conf',
            'EXECUTORS':root/'exec','PORTS':{'a':20766,'b':20767},
            'UIDS':{'a':720000,'b':720100},'ENTRY':'/distributed-agent-sandbox',
            'DATABASE':'distributed_agent_sandbox','ROLE':'distributed_agent_sandbox',
            'ORCHESTRATOR_ROLE':'distributed_agent_sandbox_orchestrator',
            'OBJECT_PREFIX':'distributed-agent-sandbox',
            'STREAM_PREFIX':'distributed-agent-sandbox:events:',
            'MEDIA_RUNTIME_PREFIX':'das-sandbox-media-','OBS_PORT':20768,'PROXY_PORT':20765,
        }
        for name,value in values.items():self.stack.enter_context(patch.object(cluster,name,value))
        self.environment={'VIDEO_USE_DATABASE_URL':
                'postgresql://distributed_agent_sandbox@127.0.0.1:15432/distributed_agent_sandbox',
            'VIDEO_USE_DEPLOYMENT':'sandbox-agent','VIDEO_USE_OBJECT_PREFIX':'distributed-agent-sandbox',
            'VIDEO_USE_AGENT_STREAM_PREFIX':'distributed-agent-sandbox:events:'}

    def test_profile_is_independent_and_templates_use_its_namespace(self):
        cluster.validate_isolation(self.environment)
        config=cluster.template_config(cluster.REPO)
        self.assertEqual((config.namespace,config.root_path,config.media_runtime_prefix),
            ('sandbox-agent','/distributed-agent-sandbox','das-sandbox-media-'))
        self.assertIn('127.0.0.1:20766',cluster.nginx_config())
        self.assertIn('127.0.0.1:20767',cluster.nginx_config())

    def test_separate_dirty_checkout_is_rejected_before_freezing_or_touching_deployment_data(self):
        import subprocess
        source=cluster.ROOT.parent/'source';source.mkdir()
        subprocess.run(['git','init','-q',str(source)],check=True)
        (source/'uncommitted.py').write_text('unreviewed = True\n')
        with self.assertRaisesRegex(ValueError,'clean committed checkout'):cluster.freeze(source)
        self.assertFalse(cluster.ROOT.exists())
        self.assertEqual(cluster.NAMESPACE,'sandbox-agent')
        self.assertEqual(cluster.DATABASE,'distributed_agent_sandbox')

    def test_reusing_any_source_identity_is_rejected(self):
        cases=(('DATABASE','distributed_agent_system'),
               ('OBJECT_PREFIX','distributed-agent-system'),
               ('STREAM_PREFIX','distributed-agent-system:events:'),
               ('MEDIA_RUNTIME_PREFIX','da-media-'),
               ('PROXY_PORT',19765))
        for name,value in cases:
            with self.subTest(name=name),patch.object(cluster,name,value),self.assertRaises(ValueError):
                cluster.validate_isolation(self.environment)

    def test_committed_wrapper_declares_every_isolation_dimension(self):
        path=Path(__file__).resolve().parents[1]/'deploy/sandbox_cluster.sh';text=path.read_text()
        for key in ('NAMESPACE','ROOT','CONF','EXECUTORS','API_A_PORT','API_B_PORT','OBS_PORT',
                    'PROXY_PORT','UID_A','UID_B','ENTRY','DATABASE','ROLE','ORCHESTRATOR_ROLE',
                    'OBJECT_PREFIX','STREAM_PREFIX','GPU_LEASE_ROOT','MEDIA_RUNTIME_PREFIX','CONTROL_LOCK'):
            self.assertIn('DAS_DEPLOY_'+key+'=',text)

    def test_native_unit_installation_generates_guard_before_executor_without_host_mutation(self):
        cluster.CONF.mkdir(parents=True)
        (cluster.CONF/'opensandbox-runtime.env').write_text('fixture')
        values={'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'opensandbox','VIDEO_USE_NODE_GATEWAY_DOCKER':'/opt/tools/docker'}
        with patch.object(cluster,'env_file',return_value=values),patch.object(cluster,'run') as run, \
             patch.object(Path,'mkdir'),patch.object(Path,'write_text',autospec=True) as write:
            cluster.install_units(cluster.REPO)
        units={str(call.args[0]):call.args[1] for call in write.call_args_list}
        guard=units['/etc/systemd/system/sandbox-agent-native-network.service']
        self.assertIn('-m webapp.native_network --docker /opt/tools/docker',guard)
        self.assertIn('Requires=sandbox-agent-native-network.service',units['/etc/systemd/system/sandbox-agent-executor@.service'])
        run.assert_called_once_with(['systemctl','daemon-reload'])

    def test_runtime_public_metadata_is_readable_by_both_service_identities(self):
        source=cluster.ROOT.parent/'release'
        (source/'runtime/public').mkdir(parents=True)
        (source/'.venv').mkdir()
        commands=[]
        with patch.object(cluster,'run',side_effect=lambda command,**kwargs:commands.append(command)):
            cluster.prepare_runtime(source)
        for account in ('sandbox-agent','sandbox-agent-orch'):
            self.assertIn(['setfacl','-m','u:'+account+':--x',source/'runtime'],commands)
            self.assertIn(['setfacl','-R','-m','u:'+account+':rX',source/'runtime/public'],commands)

    def test_frozen_release_includes_runtime_asset_manifest(self):
        self.assertIn('runtime/assets-manifest.json',cluster.FROZEN_FILES)


if __name__=='__main__':unittest.main()
