import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import threading
import sys
import wave
import array
import unittest
from pathlib import Path
from tests.isolation import temporary_root as isolated_tmp_root
from unittest.mock import Mock
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import agent_store
from webapp.agent_gateway import Gateway
from webapp.agent_sandbox import command, export_results, export_previews, prepare
from webapp.app import COOKIE, create_app, inspect_media
from webapp.store import Store
from webapp.paths import DEFAULT_DATA_DIR, SANDBOX_WORKSPACE

TEST_TMP = isolated_tmp_root()
TEST_TMP.mkdir(parents=True, exist_ok=True)


class AgentModelConfigurationTests(unittest.TestCase):
    def test_reasoning_configuration_defaults_and_explicit_high(self):
        from webapp.agent_sandbox import reasoning_effort
        with patch.dict(os.environ,{},clear=True):self.assertEqual(reasoning_effort(),'medium')
        with patch.dict(os.environ,{'VIDEO_USE_MODEL_REASONING_EFFORT':'high'}):self.assertEqual(reasoning_effort(),'high')

    def test_invalid_reasoning_configuration_is_rejected_before_runtime_setup(self):
        for value in ('','unknown','high\n[features]'):
            with self.subTest(value=value),patch.dict(os.environ,{'VIDEO_USE_MODEL_REASONING_EFFORT':value}):
                with self.assertRaisesRegex(ValueError,'VIDEO_USE_MODEL_REASONING_EFFORT'):
                    prepare(None,'unused','gpt-5.6','plan')


class AgentAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='agent-api-',dir=TEST_TMP)
        self.root=Path(self.temp.name)
        self.app=create_app(self.root,run_worker=False)
        self.a=TestClient(self.app);self.b=TestClient(self.app)
        self.a.__enter__();self.b.__enter__()
        self.pid=self.a.post('/api/projects',json={'name':'User A private project'}).json()['id']
        self.other=self.b.post('/api/projects',json={'name':'User B private project'}).json()['id']
        (self.root/'agent-health.json').write_text(json.dumps({'ready':True,'updated':time.time(),'model':'test'}))

    def tearDown(self):
        self.b.__exit__(None,None,None);self.a.__exit__(None,None,None);self.temp.cleanup()

    def plan(self):
        response=self.a.post(f'/api/projects/{self.pid}/agent/messages',json={'message':'Make a private edit plan'})
        self.assertEqual(response.status_code,202,response.text)
        return response.json()['id']

    def test_other_browser_cannot_read_send_cancel_or_download(self):
        tid=self.plan()
        agent_store.event(self.app.state.store,tid,'progress',{'text':'private progress'})
        agent_store.finish(self.app.state.store,tid,'succeeded',text='private content',
                           artifacts=[{'file':'final.mp4','name':'final.mp4','type':'video/mp4'}])
        out=self.app.state.store.project_dir(self.pid)/'edit'/'agent_exports'/tid
        out.mkdir(parents=True);(out/'final.mp4').write_bytes(b'private bytes')
        for method,path,body in [
            ('GET',f'/api/projects/{self.pid}/agent',None),
            ('POST',f'/api/projects/{self.pid}/agent/messages',{'message':'steal'}),
            ('GET',f'/api/projects/{self.pid}/agent/{tid}/events',None),
            ('POST',f'/api/projects/{self.pid}/agent/{tid}/cancel',None),
            ('GET',f'/api/projects/{self.pid}/agent/{tid}/files/final.mp4',None),
            ('GET',f'/api/projects/{self.other}/agent/{tid}/events',None),
            ('GET',f'/api/projects/{self.other}/agent/{tid}/files/final.mp4',None),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.b.request(method,path,json=body).status_code,404)
        self.assertEqual(self.a.get(f'/api/projects/{self.pid}/agent/{tid}/files/final.mp4').content,b'private bytes')

    def test_signed_identity_rejects_forgery_and_stripped_signature(self):
        signed=self.a.cookies.get(COOKIE)
        self.assertIn('.',signed)
        for cookie in [signed[:-1]+('a' if signed[-1]!='a' else 'b'),signed.split('.')[0],'0'*64+'.'+'0'*64]:
            attacker=TestClient(self.app)
            attacker.cookies.set(COOKIE,cookie)
            self.assertEqual(attacker.get(f'/api/projects/{self.pid}/agent').status_code,404)
        self.assertEqual(self.b.get(f'/api/projects/{self.pid}/agent',headers={'X-User-ID':self.pid}).status_code,404)

    def test_plan_confirmation_is_bound_to_project_and_latest_turn(self):
        endpoint=f'/api/projects/{self.pid}/agent/messages'
        self.assertEqual(self.a.post(endpoint,json={'message':'execute','mode':'execute'}).status_code,409)
        tid=self.plan()
        self.assertEqual(self.a.post(endpoint,json={'message':'second'}).status_code,409)
        self.assertEqual(self.a.delete(f'/api/projects/{self.pid}').status_code,409)
        from tests.readiness_support import ready_plan
        ready_plan(self.app.state.store,tid)
        self.assertEqual(self.b.post(f'/api/projects/{self.other}/agent/messages',json={'message':'execute','mode':'execute','plan_id':tid}).status_code,409)
        accepted=self.a.post(endpoint,json={'message':'execute','mode':'execute','plan_id':tid})
        self.assertEqual(accepted.status_code,202,accepted.text)
        self.assertEqual(self.a.post(f'/api/projects/{self.pid}/jobs',json={'kind':'pack'}).status_code,409)
        self.assertEqual(self.a.post(endpoint,json={'message':'raw','threadId':'foreign-thread'}).status_code,422)

    def test_cancellation_prevents_project_deletion_until_process_stops(self):
        tid=self.plan()
        from webapp.task_leases import Coordinator
        store=self.app.state.store
        lease=Coordinator(store,'cancel-test-node','test').claim(tid,'agent')
        store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(tid,))
        store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'allocated',?)",
                      (lease.attempt,tid,'cancel-test-node',store.now()))
        self.a.post(f'/api/projects/{self.pid}/agent/{tid}/cancel')
        self.assertEqual(agent_store.turn(self.app.state.store,tid)['status'],'cancelled')
        self.assertEqual(self.a.delete(f'/api/projects/{self.pid}').status_code,409)
        lease.release()
        store.execute("UPDATE execution_cleanup SET state='complete' WHERE attempt=?",(lease.attempt,))
        self.assertEqual(self.a.delete(f'/api/projects/{self.pid}').status_code,200)

    def test_no_arbitrary_rpc_or_secret_endpoint(self):
        for path in ('/api/agent/rpc','/.agent.env','/api/agent/config','/agent/state/codex/auth.json'):
            self.assertEqual(self.a.get(path).status_code,404)
        self.assertNotIn('API_KEY',self.a.get('/api/agent/health').text)


class SessionMigrationTests(unittest.TestCase):
    def test_existing_random_cookie_upgrades_once_without_losing_project(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            store=Store(Path(directory));token=secrets.token_hex(32)
            project=store.add_project(hashlib.sha256(token.encode()).hexdigest(),'old project')
            app=create_app(Path(directory),run_worker=False)
            with TestClient(app) as client:
                client.cookies.set(COOKIE,token,domain='testserver.local',path='/')
                response=client.get('/api/projects')
                self.assertEqual(response.json()[0]['id'],project['id'])
                self.assertIn('.',client.cookies.get(COOKIE))
                self.assertEqual(client.get('/api/projects').json()[0]['id'],project['id'])


class OutputIsolationTests(unittest.TestCase):
    def test_export_rejects_links_and_pipes_and_copies_results(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root=Path(directory);workspace=root/'workspace';(workspace/'edit').mkdir(parents=True)
            secret=root/'other-user.txt';secret.write_text('other user private content')
            (workspace/'edit'/'steal.txt').symlink_to(secret)
            os.mkfifo(workspace/'edit'/'hang.mp4')
            (workspace/'edit'/'final.txt').write_text('own result')
            env={'workspace':workspace,'project':root/'project','uid':os.getuid(),'gid':os.getgid()}
            artifacts=export_results(env,'test-turn')
            self.assertEqual([a['name'] for a in artifacts],['final.txt'])
            (workspace/'edit'/'final.txt').write_text('changed later')
            exported=env['project']/'edit'/'agent_exports'/'test-turn'/artifacts[0]['file']
            self.assertEqual(exported.read_text(),'own result')

    def test_transcription_gateway_rejects_foreign_asset_and_plan_mode(self):
        gateway=object.__new__(Gateway)
        gateway.assets={'own':{'metadata':{'kind':'video','audio_tracks':[{}]}}}
        gateway.mode='plan'
        self.assertEqual(gateway.transcribe_result({'asset':'/etc/passwd'}).status,404)
        self.assertEqual(gateway.transcribe_result({'asset':'own'}).status,403)

    def test_cached_transcription_does_not_decode_or_spend_credits(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root=Path(directory);(root/'edit/transcripts').mkdir(parents=True)
            (root/'edit/transcripts/own.json').write_text(json.dumps({'text':'cached','words':[]}))
            gateway=object.__new__(Gateway)
            gateway.assets={'own':{'id':'own','name':'own.mp4','filename':'own.mp4','metadata':{'kind':'video','audio_tracks':[{}]}}}
            gateway.project_dir=root;gateway.mode='execute';gateway.transcribe_lock=threading.Lock()
            gateway.notify=Mock();gateway.extract_audio=Mock()
            sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
            with patch.dict(os.environ,{'ELEVENLABS_API_KEY':'test-only-key'}),patch('transcribe.call_scribe') as call:
                result=gateway.transcribe_result({'asset':'own'})
                call.assert_not_called();gateway.extract_audio.assert_not_called()
            self.assertEqual(result.status,200)
            value=json.loads(b''.join(result.chunks))
            self.assertEqual(value['path'],SANDBOX_WORKSPACE+'/transcripts/own.json')
            self.assertNotIn('test-only-key',str(value))

    def test_new_transcription_uses_isolated_extractor_and_caches_result(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as directory:
            root=Path(directory);(root/'edit/transcripts').mkdir(parents=True)
            gateway=object.__new__(Gateway)
            gateway.assets={'own':{'id':'own','name':'own.mp4','filename':'own.mp4','metadata':{'kind':'video','audio_tracks':[{}]}}}
            gateway.project_dir=root;gateway.mode='execute';gateway.transcribe_lock=threading.Lock();gateway.notify=Mock()
            def extract(filename,track,path):
                self.assertEqual((filename,track),('own.mp4',0))
                with wave.open(str(path),'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
                    wav.writeframes(array.array('h',[1000,-1000]*8000).tobytes())
            gateway.extract_audio=Mock(side_effect=extract)
            sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
            with patch.dict(os.environ,{'ELEVENLABS_API_KEY':'test-only-key'}),patch('transcribe.call_scribe',return_value={'text':'real response shape','words':[]}) as call:
                result=gateway.transcribe_result({'asset':'own'})
                call.assert_called_once();gateway.extract_audio.assert_called_once()
            self.assertEqual(result.status,200)
            self.assertTrue((root/'edit/transcripts/own.json').is_file())


@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'OS sandbox checks require the namespace launcher environment')
class NamespaceIsolationTests(unittest.TestCase):
    def test_configured_model_and_high_effort_reach_readonly_sandbox_config(self):
        with tempfile.TemporaryDirectory(prefix='vua-',dir=TEST_TMP) as directory,patch.dict(os.environ,{'VIDEO_USE_MODEL_REASONING_EFFORT':'high'}):
            root=Path(directory);store=Store(root);project=store.add_project('model-settings','configuration')
            env=prepare(store,project['id'],'gpt-5.6','plan')
            with socket.socket(socket.AF_UNIX) as sock:
                path=root/'gateway.sock';sock.bind(str(path));os.chown(path,env['uid'],env['gid']);path.chmod(0o600)
                script="import json,tomllib;from pathlib import Path;p=Path('/state/codex/config.toml');c=tomllib.loads(p.read_text());print(json.dumps({'model':c['model'],'effort':c['model_reasoning_effort']}))"
                result=subprocess.run(command(env,path,['python','-c',script]),capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout),{'model':'gpt-5.6','effort':'high'})

    def test_plan_can_publish_only_preview_images_and_cannot_change_existing_edit(self):
        with tempfile.TemporaryDirectory(prefix='vua-',dir=TEST_TMP) as directory:
            root=Path(directory);store=Store(root);project=store.add_project('a','A')
            previous=prepare(store,project['id'],'test-model','plan','1'*32)
            old=previous['preview']/'previous.txt';old.write_text('previous preview remains read only')
            os.chown(old,previous['uid'],previous['gid'])
            env=prepare(store,project['id'],'test-model','plan','2'*32)
            sockpath=root/'gateway.sock'
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(sockpath));os.chown(sockpath,env['uid'],env['gid']);sockpath.chmod(0o600)
                script=f'''from pathlib import Path
import subprocess,os
preview=Path({env['preview_path']!r})
subprocess.run(['python','/opt/video-use/helpers/preview_style.py','--text','先看样式','--font-size','24','--width','320','--height','180','-o',str(preview/'style.png')],check=True)
(preview/'not-an-image.png').write_text('not an image')
(preview/'unconfirmed.mp4').write_bytes(b'not publishable during planning')
(preview/'link.png').symlink_to(preview/'style.png')
os.mkfifo(preview/'pipe.png')
for path in [Path('edit/final.mp4'),Path('edit/project.md'),Path('edit/previews/{'1'*32}/previous.txt')]:
 try:path.write_text('bad')
 except OSError:pass
 else:raise AssertionError('Read-only protection failed: '+str(path))
'''
                run=subprocess.run(command(env,sockpath,['python','-c',script]),capture_output=True,text=True,timeout=30)
                self.assertEqual(run.returncode,0,run.stdout+'\n'+run.stderr)
                artifacts=export_previews(env,'2'*32,sockpath)
                self.assertEqual(len(artifacts),1,artifacts)
                self.assertEqual(artifacts[0]['name'],'style.png')
                self.assertEqual(artifacts[0]['type'],'image/png')
                exported=env['project']/'edit/agent_exports'/('2'*32)/artifacts[0]['file']
                self.assertTrue(exported.read_bytes().startswith(b'\x89PNG'))
                self.assertEqual(old.read_text(),'previous preview remains read only')
                self.assertFalse((env['workspace']/'edit/final.mp4').exists())

    def test_edit_helper_and_reference_run_inside_private_namespace(self):
        with tempfile.TemporaryDirectory(prefix='vua-',dir=TEST_TMP) as directory:
            root=Path(directory);store=Store(root);project=store.add_project('a','A')
            src=store.project_dir(project['id'])/'sources'/'sample.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=s=160x120:r=20:d=1',
                            '-c:v','libx264','-threads','1',str(src)],check=True)
            store.add_asset(project['id'],'sample','sample.mp4','sample.mp4',inspect_media(src))
            env=prepare(store,project['id'],'test-model','execute')
            (env['workspace']/'edit/plan.json').write_text(json.dumps({'version':1,'sources':{'a':'sources/sample.mp4'},
                'clips':[{'source':'a','speed':2,'freeze_end':.5}], 'output':{'width':160,'height':120,'fps':20},
                'titles':[{'text':'沙箱剪辑','size':16}]}))
            os.chown(src,env['uid'],env['gid'])
            os.chown(src.parent,env['uid'],env['gid'])
            os.chown(env['workspace']/'edit/plan.json',env['uid'],env['gid'])
            sockpath=root/'gateway.sock'
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(sockpath));os.chown(sockpath,env['uid'],env['gid']);sockpath.chmod(0o600)
                script='''from pathlib import Path
import subprocess
assert Path('/opt/video-use/references/local-editing.md').is_file()
subprocess.run(['python','/opt/video-use/helpers/edit_media.py','validate','edit/plan.json'],check=True)
subprocess.run(['python','/opt/video-use/helpers/edit_media.py','render','edit/plan.json','-o','edit/final.mp4'],check=True)
'''
                run=subprocess.run(command(env,sockpath,['/opt/venv/bin/python','-c',script]),capture_output=True,text=True,timeout=30)
                self.assertEqual(run.returncode,0,run.stdout+'\n'+run.stderr)
                self.assertTrue((env['workspace']/'edit/final.mp4').is_file())
                env['mode']='plan'
                run=subprocess.run(command(env,sockpath,['python','/opt/video-use/helpers/edit_media.py','render','edit/plan.json','-o','edit/forbidden.mp4']),capture_output=True,text=True,timeout=10)
                self.assertNotEqual(run.returncode,0)
                self.assertFalse((env['workspace']/'edit/forbidden.mp4').exists())

    def test_prepare_initializes_project_memory_and_preserves_history(self):
        with tempfile.TemporaryDirectory(prefix='vua-',dir=TEST_TMP) as directory:
            store=Store(Path(directory));project=store.add_project('a','A')
            env=prepare(store,project['id'],'test-model','plan')
            memory=env['workspace']/'edit'/'project.md'
            self.assertIn('暂无剪辑历史',memory.read_text())
            self.assertEqual((memory.stat().st_uid,memory.stat().st_gid),(env['uid'],env['gid']))
            self.assertEqual(memory.stat().st_mode & 0o777,0o600)
            history='# 项目剪辑记录\n\n## Session 1\n\n已执行：保留原声，竖屏模糊背景。\n'
            memory.write_text(history)
            before=memory.stat()
            prepare(store,project['id'],'test-model','execute')
            self.assertEqual(memory.read_text(),history)
            self.assertEqual((memory.stat().st_ino,memory.stat().st_mtime_ns),
                             (before.st_ino,before.st_mtime_ns))
            memory.unlink()
            prepare(store,project['id'],'test-model','plan')
            self.assertIn('暂无剪辑历史',memory.read_text())

    def test_prepare_rejects_non_regular_project_memory(self):
        with tempfile.TemporaryDirectory(prefix='vua-',dir=TEST_TMP) as directory:
            root=Path(directory);store=Store(root)
            foreign=root/'other-project.md';foreign.write_text('OTHER_PROJECT_HISTORY')
            for kind in ('symlink','dangling-symlink','pipe','directory'):
                with self.subTest(kind=kind):
                    project=store.add_project('a','A')
                    memory=store.project_dir(project['id'])/'agent'/'workspace'/'edit'/'project.md'
                    memory.parent.mkdir(parents=True)
                    if kind=='symlink':memory.symlink_to(foreign)
                    elif kind=='dangling-symlink':memory.symlink_to(root/'missing.md')
                    elif kind=='pipe':os.mkfifo(memory)
                    else:memory.mkdir()
                    with self.assertRaisesRegex(RuntimeError,'Project memory must be a regular file'):
                        prepare(store,project['id'],'test-model','plan')
                    self.assertEqual(foreign.read_text(),'OTHER_PROJECT_HISTORY')
                    self.assertFalse((root/'missing.md').exists())

    def test_private_files_process_environment_network_and_plan_permissions(self):
        with tempfile.TemporaryDirectory(prefix='vua-',dir=TEST_TMP) as directory:
            root=Path(directory);store=Store(root)
            a=store.add_project('a','A');b=store.add_project('b','B')
            secret=store.project_dir(b['id'])/'private.txt';secret.write_text('USER_B_PRIVATE')
            env=prepare(store,a['id'],'test-model','execute')
            sockpath=root/'gateway.sock'
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(sockpath));os.chown(sockpath,env['uid'],env['gid']);sockpath.chmod(0o600)
                script=f'''import pathlib,os,socket,subprocess
assert os.getuid()!=0
assert os.getcwd()=={SANDBOX_WORKSPACE!r}
assert pathlib.Path('/workspace').resolve()==pathlib.Path({SANDBOX_WORKSPACE!r})
assert not pathlib.Path({str(DEFAULT_DATA_DIR/'state.sqlite3')!r}).exists()
status=pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status
assert 'CapEff:\t0000000000000000' in status
for path in ['/root','/data1/wujing03/video-use/.env','/data1/wujing03/video-use/.agent.env',{str(secret)!r}]:
 assert not pathlib.Path(path).exists(),path
assert 'ISOLATION_SECRET_MARKER' not in os.environ
assert 'VIDEO_USE_MODEL_API_KEY' not in os.environ
memory=pathlib.Path({SANDBOX_WORKSPACE+'/edit/project.md'!r})
assert '暂无剪辑历史' in memory.read_text()
with memory.open('a') as output:output.write('\\n## Session 1\\nVerified edit.\\n')
pathlib.Path({str(DEFAULT_DATA_DIR/'edit/ok.txt')!r}).write_text('OK')
try:
 socket.create_connection(('127.0.0.1',18765),timeout=.5)
 raise AssertionError('Host service reachable')
except OSError:pass
subprocess.run(['ffprobe','-version'],stdout=subprocess.DEVNULL,check=True)
print('isolated')
'''
                inherited=dict(os.environ,ISOLATION_SECRET_MARKER='must-not-leak')
                run=subprocess.run(command(env,sockpath,['/opt/venv/bin/python','-c',script]),
                                   capture_output=True,text=True,timeout=20,env=inherited)
                self.assertEqual(run.returncode,0,run.stderr)
                self.assertIn('isolated',run.stdout)
                env['mode']='plan'
                run=subprocess.run(command(env,sockpath,['/bin/sh','-c',
                                   'ls -l edit/project.md && cat edit/project.md']),
                                   capture_output=True,text=True,timeout=10)
                self.assertEqual(run.returncode,0,run.stderr)
                self.assertIn('Verified edit.',run.stdout)
                run=subprocess.run(command(env,sockpath,['/bin/sh','-c','echo bad > edit/project.md']),
                                   capture_output=True,text=True,timeout=10)
                self.assertNotEqual(run.returncode,0)
                self.assertIn('Verified edit.',(env['workspace']/'edit'/'project.md').read_text())


if __name__=='__main__':unittest.main()
