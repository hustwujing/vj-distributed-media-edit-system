import unittest
from types import SimpleNamespace

from webapp.opensandbox_backend import (AgentRuntimeSpec,BOOTSTRAP_ENTRYPOINT,OpenSandboxBackend,
    OpenSandboxProvisioningError,RuntimeGatewayGrant)
from webapp.runtime_injection import GatewayGrant,RUNTIME_CONFIG
from webapp.sandbox_backends import BackendBinding,OPEN_SANDBOX


class Files:
    def __init__(self):self.data={};self.operations=[]
    def mkdir(self,path,mode):self.operations.append(('mkdir',path,mode))
    def write(self,path,data,mode):self.data[path]=data;self.operations.append(('write',path,mode))
    def read(self,path):return self.data[path]
    def delete(self,path,missing_ok=False):self.data.pop(path,None);self.operations.append(('delete',path))
    def move(self,source,target):self.data[target]=self.data.pop(source);self.operations.append(('move',source,target))


class Session:
    def __init__(self):self.files=Files();self.closed=False
    def websocket_endpoint(self,port):return ('wss://sandbox/control',{'X-Route':'one'})
    def close(self):self.closed=True


class SDK:
    def __init__(self):self.session=Session();self.connected=[]
    def connect(self,sandbox_id):self.connected.append(sandbox_id);return self.session


class Lifecycle:
    def __init__(self):self.created=[];self.deleted=[]
    def create(self,*args,**kwargs):self.created.append((args,kwargs));return {'id':'sbx-1'}
    def delete_confirmed(self,sandbox_id,**kwargs):self.deleted.append((sandbox_id,kwargs))


class Runtime:
    def __init__(self,*args,**kwargs):self.args=args;self.kwargs=kwargs;self.calls=[];self.closed=False
    def connect(self):self.calls.append(('connect',))
    def call(self,method,params):self.calls.append((method,params));return {'accepted':True,'session_id':'s1'}
    def receive_once(self):self.calls.append(('receive',));return 'frame'
    def close(self):self.closed=True


class OpenSandboxBackendTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle=Lifecycle();self.sdk=SDK();self.runtimes=[]
        def factory(*args,**kwargs):
            runtime=Runtime(*args,**kwargs);self.runtimes.append(runtime);return runtime
        self.backend=OpenSandboxBackend(self.lifecycle,self.sdk,runtime_client_factory=factory)
        self.lease=SimpleNamespace(attempt='a'*32,generation=2,run_id='task')
        self.binding=BackendBinding(OPEN_SANDBOX,'agent-v1',
            'registry/agent@sha256:'+'a'*64,'sha256:'+'b'*64)
        self.revoked=[]
        def grant(identity):
            self.grant_identity=identity
            material=GatewayGrant('https://172.30.0.1:18880','g'*48,b'ca',('/v1/responses','/context'))
            return RuntimeGatewayGrant(material,lambda:self.revoked.append(True))
        self.spec=AgentRuntimeSpec('codex','model-1','high','sha256:'+'c'*64,
            'gateway-v1',{'model':'model-1'},grant)

    def test_start_wires_create_files_proxy_and_runtime_initialization(self):
        restored=[]
        runtime=self.backend.start(self.lease,self.binding,{'cpu':2,'memory_mb':2048},self.spec,
            lambda files:restored.append(files),lambda event:None)
        create=self.lifecycle.created[0][1]
        self.assertEqual(create['entrypoint'],BOOTSTRAP_ENTRYPOINT)
        self.assertTrue(restored);self.assertIn(RUNTIME_CONFIG,self.sdk.session.files.data)
        client=self.runtimes[0]
        self.assertEqual(client.args[0],'wss://sandbox/control')
        self.assertEqual(client.kwargs['headers'],{'X-Route':'one'})
        self.assertEqual(client.calls[0],('connect',))
        self.assertEqual(client.calls[1][0],'runtime.initialize')
        self.assertEqual(self.grant_identity.sandbox_id,'sbx-1')
        opened=runtime.open_session('rules')['session_id'];runtime.run_turn(opened,'work')
        self.assertEqual(runtime.receive_once(),'frame')
        runtime.cleanup();self.assertTrue(client.closed);self.assertTrue(self.sdk.session.closed)
        self.assertEqual(self.revoked,[True])
        self.assertEqual(self.lifecycle.deleted[-1][0],'sbx-1')

    def test_restore_failure_closes_and_deletes_partial_sandbox(self):
        with self.assertRaises(OpenSandboxProvisioningError) as failed:
            self.backend.start(self.lease,self.binding,{'cpu':1,'memory_mb':512},self.spec,
                lambda files:(_ for _ in ()).throw(IOError('restore failed')),lambda event:None)
        self.assertIsInstance(failed.exception.cause,IOError)
        self.assertTrue(self.sdk.session.closed)
        self.assertEqual(self.revoked,[True])
        self.assertEqual(self.lifecycle.deleted,[('sbx-1',{'expected_attempt':self.lease.attempt})])
        self.assertEqual(self.runtimes,[])


if __name__=='__main__':unittest.main()
