import json
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from webapp.opensandbox_command import OpenSandboxCommandBackend,OpenSandboxCommandRuntime
from webapp.sandbox_backends import BackendBinding,OPEN_SANDBOX


class Files:
    def __init__(self):self.data={};self.entries=[]
    def mkdir(self,path,mode):pass
    def write(self,path,data,mode):self.data[path]=data.read() if hasattr(data,'read') else data
    def read(self,path):return self.data[path]
    def list(self,path,depth=100):return self.entries


class Commands:
    def __init__(self,files):self.files=files;self.calls=[]
    def run(self,command,opts=None,handlers=None):
        self.calls.append((command,opts));self.files.data['/workspace/edit/out/result.txt']=b'result'
        self.files.entries=[{'path':'/workspace/edit/out/result.txt','type':'file'}]
        message=SimpleNamespace(text=json.dumps({'version':1,'returncode':0}))
        return SimpleNamespace(exit_code=0,logs=SimpleNamespace(stdout=[message]))
    def interrupt(self,identity):self.interrupted=identity


class Session:
    def __init__(self):self.files=Files();self.commands=Commands(self.files);self.closed=False
    def close(self):self.closed=True


class Lifecycle:
    def __init__(self):self.deleted=[]
    def create(self,*args,**kwargs):return {'id':'command-1'}
    def delete_confirmed(self,*args,**kwargs):self.deleted.append((args,kwargs))


class FlakyLifecycle(Lifecycle):
    def __init__(self):super().__init__();self.failures=1
    def delete_confirmed(self,*args,**kwargs):
        self.deleted.append((args,kwargs))
        if self.failures:self.failures-=1;raise RuntimeError('temporary delete failure')


class NoopCleanup:
    def reserve(self,lease):pass
    def attach(self,lease,sandbox_id):pass
    def complete(self,store,attempt):pass


class FlakyCleanup(NoopCleanup):
    def __init__(self):self.calls=0
    def complete(self,store,attempt):
        self.calls+=1
        if self.calls==1:raise RuntimeError('temporary registry failure')


class OpenSandboxCommandTests(unittest.TestCase):
    def test_environment_materializes_only_frozen_asset_without_node_local_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'object';source.write_bytes(b'input-bytes')
            record={'id':'object-1','artifact':'object-1','size':11,
                    'sha256':'8f68d66c4a8aa3c8d6c5fb50d2cf96a6f8712d1334c879d204e4f52579d1b189'}
            class Artifacts:
                def resolve(self,project,path):self.path=path;return record
                def materialize(self,project,identifier,target):shutil.copyfile(source,target);return target
            store=SimpleNamespace(root=root/'node',shared_files=True,artifacts=Artifacts(),
                assets=lambda project:[{'id':'asset-1','filename':'input.wav','name':'input','metadata':{'kind':'audio'}},
                                       {'id':'secret','filename':'secret.wav','name':'secret','metadata':{'kind':'audio'}}],
                query=lambda *args:[],project_dir=lambda project:root/'missing-project')
            store.root.mkdir();lease=SimpleNamespace(step_id='process',runtime=None,check=lambda:None)
            runtime=OpenSandboxCommandRuntime(Lifecycle(),Session(),'command-1','attempt',lease);lease.runtime=runtime
            workflow={'id':'run','run_id':'run','project':'project','steps':[{'id':'process','status':'running','revision':0,
                'quality':{},'spec':{'kind':'operation','inputs':{'source':{'asset':'asset-1'}},'argv':[]}}]}
            env=runtime.environment(store,workflow,'unused')
            self.assertEqual((env['sources']/'input.wav').read_bytes(),b'input-bytes')
            self.assertFalse((env['sources']/'secret.wav').exists())
            self.assertEqual([asset['id'] for asset in env['assets']],['asset-1'])
            runtime.close();self.assertFalse((store.root/'command-attempts'/'attempt').exists())

    def test_fixed_payload_runs_without_harness_and_collects_declared_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);workspace=root/'workspace';sources=root/'sources';workspace.mkdir();sources.mkdir()
            (workspace/'edit').mkdir();(sources/'input.txt').write_text('input')
            lease=SimpleNamespace(runtime=None,check=lambda:None);session=Session();runtime=OpenSandboxCommandRuntime(
                Lifecycle(),session,'command-1','attempt',lease);lease.runtime=runtime
            env={'workspace':workspace,'sources':sources,'runtime':runtime}
            receipt=runtime.execute_payload(env,{'mode':'local','output':'edit/out'},30,lambda:False)
            self.assertEqual(receipt,{'version':1,'returncode':0})
            self.assertEqual((workspace/'edit/out/result.txt').read_bytes(),b'result')
            self.assertIn('webapp.workflow_io',session.commands.calls[0][0][-1])

    def test_backend_requires_command_binding_and_deletes_on_close(self):
        lifecycle=Lifecycle();session=Session();sdk=SimpleNamespace(connect=lambda _:session)
        backend=OpenSandboxCommandBackend(lifecycle,sdk,NoopCleanup());lease=SimpleNamespace(attempt='a',generation=1,store=None,start_compute=lambda:None)
        binding=BackendBinding(OPEN_SANDBOX,'p','image@sha256:'+'a'*64,'sha256:'+'b'*64,'command_runtime')
        runtime=backend.start(lease,binding,{'cpu':1,'memory_mb':512});runtime.close()
        self.assertTrue(session.closed);self.assertTrue(lifecycle.deleted)

    def test_delete_failure_remains_retryable_after_local_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            lifecycle=FlakyLifecycle();session=Session();lease=SimpleNamespace(check=lambda:None)
            runtime=OpenSandboxCommandRuntime(lifecycle,session,'command-1','attempt',lease)
            runtime.local_root=Path(folder)/'attempt';runtime.local_root.mkdir()
            with self.assertRaisesRegex(RuntimeError,'temporary delete failure'):runtime.close()
            self.assertTrue(runtime.session_closed);self.assertFalse(runtime.sandbox_deleted)
            self.assertFalse(runtime.closed);self.assertFalse(runtime.local_root.exists())
            runtime.close()
            self.assertTrue(runtime.closed);self.assertTrue(runtime.sandbox_deleted)
            self.assertEqual(len(lifecycle.deleted),2)

    def test_registry_completion_retries_without_redeleting_sandbox(self):
        cleanup=FlakyCleanup();lifecycle=Lifecycle();session=Session();lease=SimpleNamespace(store=None,check=lambda:None)
        runtime=OpenSandboxCommandRuntime(lifecycle,session,'command-1','attempt',lease,cleanup)
        with self.assertRaisesRegex(RuntimeError,'temporary registry failure'):runtime.close()
        self.assertTrue(runtime.sandbox_deleted);self.assertFalse(runtime.closed)
        runtime.close()
        self.assertTrue(runtime.closed);self.assertEqual(cleanup.calls,2);self.assertEqual(len(lifecycle.deleted),1)

    def test_workflow_adapter_delegates_isolated_protocol_to_command_runtime(self):
        from webapp.workflow_runtime import Adapter
        runtime=SimpleNamespace(is_command=True,execute_payload=lambda env,payload,timeout,cancelled:'receipt')
        adapter=Adapter.__new__(Adapter);env={'runtime':runtime}
        result=adapter.isolated(env,{'mode':'local'},30,lambda:False)
        self.assertEqual(result,'receipt')


if __name__=='__main__':unittest.main()
