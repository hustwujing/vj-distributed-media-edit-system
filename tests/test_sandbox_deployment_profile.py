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


if __name__=='__main__':unittest.main()
