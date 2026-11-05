import json
from pathlib import Path
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


class OpenSandboxCommandTests(unittest.TestCase):
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
        backend=OpenSandboxCommandBackend(lifecycle,sdk);lease=SimpleNamespace(attempt='a',generation=1,start_compute=lambda:None)
        binding=BackendBinding(OPEN_SANDBOX,'p','image@sha256:'+'a'*64,'sha256:'+'b'*64,'command_runtime')
        runtime=backend.start(lease,binding,{'cpu':1,'memory_mb':512});runtime.close()
        self.assertTrue(session.closed);self.assertTrue(lifecycle.deleted)

    def test_workflow_adapter_delegates_isolated_protocol_to_command_runtime(self):
        from webapp.workflow_runtime import Adapter
        runtime=SimpleNamespace(is_command=True,execute_payload=lambda env,payload,timeout,cancelled:'receipt')
        adapter=Adapter.__new__(Adapter);env={'runtime':runtime}
        result=adapter.isolated(env,{'mode':'local'},30,lambda:False)
        self.assertEqual(result,'receipt')


if __name__=='__main__':unittest.main()
