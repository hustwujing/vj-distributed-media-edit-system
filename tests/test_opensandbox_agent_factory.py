import unittest
from unittest.mock import Mock,patch

from tests import test_workflows as fixtures
from webapp.opensandbox_agent_factory import OpenSandboxAgentFactory
from webapp.sandbox_backends import BackendBinding,OPEN_SANDBOX
from webapp.task_leases import Coordinator


class OpenSandboxAgentFactoryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WorkflowTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.task=self.f.turn
        for asset in self.store.assets(self.f.project):
            path=self.store.project_dir(self.f.project)/'sources'/asset['filename']
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
        self.store.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.task,))
        self.lease=Coordinator(self.store,'remote-factory','test').claim(self.task,'agent')
        self.addCleanup(self.lease.release)

    def test_factory_rebuilds_context_and_sources_before_backend_start(self):
        runtime=Mock();backend=Mock();backend.start.return_value=runtime
        grants=[]
        factory=OpenSandboxAgentFactory(backend,lambda lease,identity:grants.append((lease,identity)))
        binding=BackendBinding(OPEN_SANDBOX,'agent-v1','registry/agent@sha256:'+'a'*64,'sha256:'+'b'*64,'agent_runtime')
        with self.lease.bind(),patch('webapp.opensandbox_agent_factory.SandboxWorkspaceRestorer.restore',return_value={'files':1}):
            result=factory(self.lease,binding)
        self.assertIs(result,runtime);self.assertTrue(backend.start.called)
        spec=backend.start.call_args.args[3]
        self.assertEqual(spec.harness,'codex');self.assertTrue(spec.tool_schema_digest.startswith('sha256:'))
        self.assertIn('workspace',runtime.prepared_env)


if __name__=='__main__':unittest.main()
