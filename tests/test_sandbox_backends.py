import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs,parse_qsl,urlsplit
from unittest.mock import Mock,patch

from webapp.sandbox_backends import (
    AmbiguousProvisioning,
    BackendBinding,
    BackendConfigurationError,
    BackendNotReady,
    DeletionPending,
    DuplicateProvisioning,
    LOCAL_BWRAP,
    OPEN_SANDBOX,
    OpenSandboxClient,
    RuntimeBackends,
    select_backend,
)


IMAGE='registry.example/agent@sha256:'+'a'*64


class FakeTransport:
    def __init__(self,*responses):
        self.responses=list(responses)
        self.calls=[]

    def request(self,method,path,**kwargs):
        self.calls.append((method,path,kwargs))
        result=self.responses.pop(0)
        if isinstance(result,BaseException):raise result
        return result


class BackendSelectionTests(unittest.TestCase):
    def test_default_cpu_backend_preserves_local_runtime(self):
        binding=select_backend({'cpu':2,'memory_mb':1024,'gpu':False},environ={},local_profile='local-v1')
        self.assertEqual(binding,BackendBinding(LOCAL_BWRAP,'local-v1'))

    def test_gpu_never_routes_to_opensandbox(self):
        binding=select_backend({'cpu':2,'memory_mb':4096,'gpu':True},
            environ={'VIDEO_USE_CPU_SANDBOX_BACKEND':OPEN_SANDBOX},local_profile='cuda-v1')
        self.assertEqual(binding,BackendBinding(LOCAL_BWRAP,'cuda-v1'))

    def test_cpu_opensandbox_binding_is_fully_frozen(self):
        binding=select_backend({'cpu':2,'memory_mb':2048,'gpu':False},environ={
            'VIDEO_USE_CPU_SANDBOX_BACKEND':OPEN_SANDBOX,
            'VIDEO_USE_OPEN_SANDBOX_RUNTIME_PROFILE':'agent-v3',
            'VIDEO_USE_OPEN_SANDBOX_IMAGE_DIGEST':IMAGE,
            'VIDEO_USE_OPEN_SANDBOX_SKILL_DIGEST':'skills-sha256-123',
        })
        self.assertEqual(binding,BackendBinding(OPEN_SANDBOX,'agent-v3',IMAGE,'skills-sha256-123'))

    def test_opensandbox_does_not_accept_missing_or_mutable_image(self):
        base={'VIDEO_USE_CPU_SANDBOX_BACKEND':OPEN_SANDBOX,
              'VIDEO_USE_OPEN_SANDBOX_RUNTIME_PROFILE':'agent-v3',
              'VIDEO_USE_OPEN_SANDBOX_SKILL_DIGEST':'skills-v1'}
        with self.assertRaisesRegex(BackendConfigurationError,'IMAGE_DIGEST'):
            select_backend({'gpu':False},environ=base)
        with self.assertRaisesRegex(BackendConfigurationError,'image@sha256'):
            select_backend({'gpu':False},environ={**base,
                'VIDEO_USE_OPEN_SANDBOX_IMAGE_DIGEST':'agent:latest'})

    def test_registry_refuses_remote_to_local_fallback(self):
        factory=Mock(return_value='runtime');registry=RuntimeBackends(factory);lease=object()
        self.assertEqual(registry.create(lease,BackendBinding(LOCAL_BWRAP,'local')),'runtime')
        factory.assert_called_once_with(lease)
        with self.assertRaisesRegex(BackendNotReady,'refusing local fallback'):
            registry.create(lease,BackendBinding(OPEN_SANDBOX,'p',IMAGE,'s'))

    def test_registry_dispatches_configured_remote_factory(self):
        local=Mock();remote=Mock(return_value='remote-runtime');lease=object()
        registry=RuntimeBackends(local,remote);binding=BackendBinding(OPEN_SANDBOX,'p',IMAGE,'s')
        self.assertEqual(registry.create(lease,binding),'remote-runtime')
        remote.assert_called_once_with(lease,binding);local.assert_not_called()

    def test_agent_host_freezes_selection_on_first_runtime_request(self):
        from webapp.agent_host import AgentHost
        binding=BackendBinding(LOCAL_BWRAP,'local-v1')
        host=AgentHost.__new__(AgentHost)
        host.store=object();host.runtime_backends=Mock()
        host.runtime_backends.create.return_value='runtime'
        lease=SimpleNamespace(run_id='task',kind='agent',step_id=None)
        with patch('webapp.scheduling.requirements',return_value={
                'cpu':1,'memory_mb':512,'gpu':False}) as requirements, \
             patch('webapp.agent_host.select_backend',return_value=binding) as select:
            self.assertEqual(host._runtime(lease),'runtime')
            self.assertEqual(host._runtime(lease),'runtime')
        requirements.assert_called_once_with(host.store,'task','agent',step_id=None)
        select.assert_called_once()
        self.assertIs(lease.backend_binding,binding)
        host.runtime_backends.create.assert_called_with(lease,binding)


class OpenSandboxClientTests(unittest.TestCase):
    def setUp(self):
        self.lease=SimpleNamespace(attempt='a'*32,run_id='task-1',generation=3)
        self.binding=BackendBinding(OPEN_SANDBOX,'agent-v3',IMAGE,'skills-v2')
        self.resources={'cpu':2,'memory_mb':1536}

    def test_create_uses_immutable_artifacts_identity_and_default_deny_network(self):
        transport=FakeTransport((202,{'id':'sbx-1'}))
        result=OpenSandboxClient(transport).create(self.lease,self.binding,self.resources,timeout=90)
        self.assertEqual(result['id'],'sbx-1')
        method,path,kwargs=transport.calls[0]
        self.assertEqual((method,path),('POST','/v1/sandboxes'))
        body=kwargs['body']
        self.assertEqual(body['image']['uri'],IMAGE)
        self.assertEqual(body['resourceLimits'],{'cpu':'2','memory':'1536Mi'})
        self.assertEqual(body['networkPolicy'],{'defaultAction':'deny','egress':[]})
        self.assertEqual(body['metadata']['video-use.io/attempt'],self.lease.attempt)
        for value in body['metadata'].values():self.assertLessEqual(len(value),63)
        self.assertEqual(kwargs['headers']['X-Request-ID'],self.lease.attempt)

    def test_ambiguous_create_reconciles_by_attempt(self):
        transport=FakeTransport(AmbiguousProvisioning('timeout'),
            (200,{'items':[{'id':'sbx-existing'}]}))
        result=OpenSandboxClient(transport).create(self.lease,self.binding,self.resources)
        self.assertEqual(result['id'],'sbx-existing')
        query=parse_qs(urlsplit(transport.calls[1][1]).query)
        self.assertEqual(dict(parse_qsl(query['metadata'][0])),
            {'video-use.io/attempt':self.lease.attempt})

    def test_ambiguous_create_never_blindly_retries(self):
        transport=FakeTransport(AmbiguousProvisioning('timeout'),(200,{'items':[]}))
        with self.assertRaises(AmbiguousProvisioning):
            OpenSandboxClient(transport).create(self.lease,self.binding,self.resources)
        self.assertEqual([call[0] for call in transport.calls],['POST','GET'])

        transport=FakeTransport(AmbiguousProvisioning('timeout'),
            (200,{'items':[{'id':'one'},{'id':'two'}]}))
        with self.assertRaises(DuplicateProvisioning):
            OpenSandboxClient(transport).create(self.lease,self.binding,self.resources)

    def test_delete_checks_ownership_and_confirms_absence(self):
        metadata={'metadata':{'video-use.io/attempt':self.lease.attempt}}
        transport=FakeTransport((200,metadata),(204,None),(404,None))
        OpenSandboxClient(transport).delete_confirmed('sbx/1',expected_attempt=self.lease.attempt)
        self.assertEqual([call[:2] for call in transport.calls],[
            ('GET','/v1/sandboxes/sbx%2F1'),
            ('DELETE','/v1/sandboxes/sbx%2F1'),
            ('GET','/v1/sandboxes/sbx%2F1')])

    def test_delete_rejects_foreign_identity_and_pending_cleanup(self):
        transport=FakeTransport((200,{'metadata':{'video-use.io/attempt':'other'}}))
        with self.assertRaisesRegex(BackendConfigurationError,'another attempt'):
            OpenSandboxClient(transport).delete_confirmed('sbx',expected_attempt=self.lease.attempt)
        self.assertEqual(len(transport.calls),1)

        metadata={'metadata':{'video-use.io/attempt':self.lease.attempt}}
        transport=FakeTransport((200,metadata),(204,None),(200,metadata))
        with self.assertRaises(DeletionPending):
            OpenSandboxClient(transport).delete_confirmed('sbx',expected_attempt=self.lease.attempt)


if __name__=='__main__':unittest.main()
