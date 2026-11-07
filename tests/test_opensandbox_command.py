import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from webapp.opensandbox_command import OpenSandboxCommandBackend,OpenSandboxCommandRuntime
from webapp.sandbox_backends import BackendBinding,OPEN_SANDBOX


class Files:
    def __init__(self):self.data={};self.entries=[]
    def mkdir(self,path,mode):pass
    def write(self,path,data,mode):self.data[path]=data.read() if hasattr(data,'read') else data
    def read(self,path):return self.data[path]
    def read_chunks(self,path):yield self.data[path]
    def list(self,path,depth=100):return self.entries


class Commands:
    def __init__(self,files):self.files=files;self.calls=[]
    def run(self,command,opts=None,handlers=None):
        self.calls.append((command,opts))
        if 'webapp.quality_check' in command[-1]:
            message=SimpleNamespace(text=json.dumps({'version':'quality-v1','technical_valid':True,'passed':True,
                'requires_review':False,'requires_agent':False,'files':[],'checks':[]}))
            return SimpleNamespace(exit_code=0,logs=SimpleNamespace(stdout=[message]))
        self.files.data['/workspace/edit/out/result.txt']=b'result'
        self.files.entries=[{'path':'/workspace/edit/out/result.txt','type':'file'}]
        message=SimpleNamespace(text=json.dumps({'version':1,'returncode':0}))
        return SimpleNamespace(exit_code=0,logs=SimpleNamespace(stdout=[message]))
    def interrupt(self,identity):self.interrupted=identity


class FailingCommands(Commands):
    def run(self,command,opts=None,handlers=None):
        message=SimpleNamespace(text='bounded diagnostic')
        return SimpleNamespace(exit_code=7,logs=SimpleNamespace(stdout=[],stderr=[message]))


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
    def test_output_rejects_remote_traversal_and_sibling_paths_before_read(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace=Path(folder)/'workspace';workspace.mkdir()
            session=Session();runtime=OpenSandboxCommandRuntime(Lifecycle(),session,'s','a',SimpleNamespace(check=lambda:None))
            for remote in ('/workspace/../escape','/workspace/out/../../escape',
                           '/workspace/other/input','/workspace/outside/file','out/file'):
                with self.subTest(remote=remote):
                    session.files.entries=[{'path':remote,'type':'file'}]
                    with patch.object(session.files,'read_chunks',side_effect=AssertionError('Must validate before download')):
                        with self.assertRaisesRegex(ValueError,'escaped'):runtime._download_output({'workspace':workspace},'out')
            self.assertEqual(list(workspace.iterdir()),[])

    def test_output_never_follows_local_directory_or_file_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);workspace=root/'workspace';workspace.mkdir()
            outside=root/'outside';outside.mkdir();protected=outside/'value';protected.write_bytes(b'original')
            session=Session();runtime=OpenSandboxCommandRuntime(Lifecycle(),session,'s','a',SimpleNamespace(check=lambda:None))
            session.files.data['/workspace/out/value']=b'bad'
            session.files.entries=[{'path':'/workspace/out/value','type':'file'}]
            for kind in ('directory','symlink','hardlink'):
                with self.subTest(kind=kind):
                    out=workspace/'out'
                    if kind=='directory':out.symlink_to(outside,target_is_directory=True)
                    else:
                        out.mkdir()
                        if kind=='symlink':(out/'value').symlink_to(protected)
                        else:os.link(protected,out/'value')
                    with self.assertRaises((ValueError,OSError)):runtime._download_output({'workspace':workspace},'out')
                    self.assertEqual(protected.read_bytes(),b'original')
                    if out.is_symlink():out.unlink()
                    else:shutil.rmtree(out)

    def test_output_limits_stream_bytes_entries_and_removes_partial_files(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace=Path(folder);session=Session()
            runtime=OpenSandboxCommandRuntime(Lifecycle(),session,'s','a',SimpleNamespace(check=lambda:None))
            runtime.limit=4;closed=[]
            def chunks(path):
                try:yield b'123';yield b'45'
                finally:closed.append(True)
            session.files.entries=[{'path':'/workspace/out/file','type':'file','size':1}]
            with patch.object(session.files,'read_chunks',chunks):
                with self.assertRaisesRegex(ValueError,'byte limit'):runtime._download_output({'workspace':workspace},'out')
            self.assertTrue(closed);self.assertEqual(list((workspace/'out').iterdir()),[])
            session.files.entries=[{'path':'/workspace/out/a','type':'file'},{'path':'/workspace/out/b','type':'file'}]
            session.files.data={'/workspace/out/a':b'abc','/workspace/out/b':b'def'}
            with self.assertRaisesRegex(ValueError,'byte limit'):runtime._download_output({'workspace':workspace},'out')
            self.assertEqual((workspace/'out/a').read_bytes(),b'abc');self.assertFalse((workspace/'out/b').exists())
            runtime.limit=100;runtime.max_output_files=1
            with self.assertRaisesRegex(ValueError,'file limit'):runtime._download_output({'workspace':workspace},'out')

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

    def test_quality_validation_runs_in_same_command_sandbox(self):
        session=Session();lease=SimpleNamespace(check=lambda:None)
        runtime=OpenSandboxCommandRuntime(Lifecycle(),session,'command-1','attempt',lease)
        report=runtime.validate_payload({'files':[],'acceptance':[]})
        self.assertTrue(report['technical_valid'])
        self.assertIn('webapp.quality_check',session.commands.calls[0][0][-1])

    def test_checkpoint_uses_durable_step_checkpoint_protocol(self):
        runtime=OpenSandboxCommandRuntime(Lifecycle(),Session(),'command-1','attempt',SimpleNamespace())
        runtime.env={'workspace':Path('/workspace')}
        with patch('webapp.step_checkpoints.save',return_value='saved') as save:
            self.assertEqual(runtime.checkpoint(),'saved')
        save.assert_called_once_with(runtime)

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

    def test_workflow_adapter_delegates_validation_to_command_runtime(self):
        from webapp.operation_result import ProducedFiles
        from webapp.workflow_runtime import Adapter
        report={'version':'quality-v1','technical_valid':True,'passed':True,'requires_review':False,
            'requires_agent':False,'files':[{'name':'edit/final.wav','kind':'media','audio_track_present':True},
                                           {'name':'edit/audio-process-report.json','kind':'json'}],'checks':[]}
        runtime=SimpleNamespace(is_command=True,calls=[],validate_payload=lambda payload:runtime.calls.append(payload) or report)
        adapter=Adapter.__new__(Adapter);adapter.environment=lambda workflow:{'runtime':runtime}
        step={'quality':{},'semantics':{},'spec':{'kind':'operation','operation':'audio.process','acceptance':[]}}
        files=ProducedFiles(['edit/final.wav','edit/audio-process-report.json'],
            {'audio':'edit/final.wav','report':'edit/audio-process-report.json'})
        result=adapter.validate({},step,files)
        self.assertTrue(result['passed']);self.assertEqual(runtime.calls[0]['files'],files)

    def test_failed_command_retains_bounded_remote_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);workspace=root/'workspace';sources=root/'sources';workspace.mkdir();sources.mkdir()
            session=Session();session.commands=FailingCommands(session.files)
            lease=SimpleNamespace(check=lambda:None);runtime=OpenSandboxCommandRuntime(Lifecycle(),session,'command-1','attempt',lease)
            with self.assertRaisesRegex(RuntimeError,'bounded diagnostic') as failed:
                runtime.execute_payload({'workspace':workspace,'sources':sources},{'mode':'local'},30,lambda:False)
            self.assertEqual(failed.exception.diagnostic,
                {'code':'opensandbox_command_failed','exit_code':7,'log_tail':'bounded diagnostic'})


if __name__=='__main__':unittest.main()
