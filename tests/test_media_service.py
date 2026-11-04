import io
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from PIL import Image
from webapp import media_service


@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Requires fixed decoder namespace privileges')
class MediaBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='vm-',dir=tempfile.gettempdir());self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o755)
        self.input=self.root/'input.png';Image.new('RGB',(16,16),'blue').save(self.input)
        u=pwd.getpwnam('video-use');os.chown(self.input,u.pw_uid,u.pw_gid);self.input.chmod(0o600)
        self.path=self.root/'decoder.sock';self.server=media_service.Server(self.path)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.config=patch.dict(os.environ,{'VIDEO_USE_MEDIA_SOCKET':str(self.path)});self.config.start();self.addCleanup(self.config.stop)
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
    def test_fixed_image_and_normalization_use_only_passed_file(self):
        result=media_service.request(self.input,media_service.command('image'),10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn(b'"width": 16',result.stdout)
        normalized=media_service.request(self.input,media_service.command('normalize'),10)
        self.assertEqual(normalized.returncode,0,normalized.stderr)
        self.assertEqual(Image.open(io.BytesIO(normalized.stdout)).size,(16,16))
    def test_unprivileged_api_process_can_use_fixed_broker(self):
        code="from pathlib import Path; from webapp.media_isolation import inspect_image; assert inspect_image(Path("+repr(str(self.input))+"))['width']==16"
        result=subprocess.run(['runuser','-u','video-use','--',str(media_service.REPO/'.venv/bin/python'),'-c',code],cwd=media_service.REPO,env={**os.environ,'PYTHONPATH':str(media_service.REPO)},capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_unregistered_commands_and_non_owned_files_are_rejected(self):
        with self.assertRaises(ValueError):media_service.request(self.input,['bash','-c','id'],10)
        foreign=self.root/'foreign.png';foreign.write_bytes(self.input.read_bytes());foreign.chmod(0o644)
        result=media_service.request(foreign,media_service.command('image'),10)
        self.assertNotEqual(result.returncode,0)
        with self.assertRaises(ValueError):media_service.command('thumbnail','file:/etc/passwd')

    def test_request_context_crosses_fd_broker_without_expanding_command_permissions(self):
        from webapp import telemetry
        captured=[]
        def collect(event,level='INFO',exc=None,**fields):captured.append(telemetry.record(event,level,exc,**fields))
        with telemetry.bind(request_id='a'*32,trace_id='b'*32,span_id='c'*16),patch('webapp.telemetry.emit',side_effect=collect):
            result=media_service.request(self.input,media_service.command('image'),10)
        self.assertEqual(result.returncode,0)
        records=[r for r in captured if r.get('phase')=='media_decode']
        self.assertTrue(records)
        self.assertTrue(all(r['request_id']=='a'*32 and r['trace_id']=='b'*32 and r['parent_span_id']=='c'*16 for r in records))

    def test_strict_service_umask_keeps_native_probe_and_thumbnail_working(self):
        import wave
        audio=self.root/'audio.wav'
        with wave.open(str(audio),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\0\0'*1600)
        account=pwd.getpwnam('video-use');os.chown(audio,account.pw_uid,account.pw_gid);audio.chmod(0o600)
        code='''import os,pwd,sys,json
from pathlib import Path
from webapp.media_isolation import run
from webapp.media_service import command
os.umask(0o077);account=pwd.getpwnam('video-use')
for source,operation in ((sys.argv[1],'probe'),(sys.argv[2],'thumbnail')):
 result=run(Path(source),command(operation),10,decode_uid=account.pw_uid,decode_gid=account.pw_gid)
 assert result.returncode==0,result.stderr
 if operation=='probe':assert json.loads(result.stdout)['streams'][0]['codec_type']=='audio'
 else:assert result.stdout.startswith(b'\\xff\\xd8')
'''
        result=subprocess.run([str(media_service.REPO/'.venv/bin/python'),'-c',code,str(audio),str(self.input)],capture_output=True,text=True,timeout=30,cwd=media_service.REPO)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_decoder_dependency_failure_is_server_error_and_keeps_request_correlation(self):
        from webapp import telemetry
        events=[]
        def collect(event,level='INFO',exc=None,**fields):events.append(telemetry.record(event,level,exc,**fields))
        failed=subprocess.CompletedProcess([],127,b'',b'ffprobe: error while loading shared libraries: libblas.so.3: cannot open shared object file')
        with telemetry.bind(request_id='e'*32),patch('webapp.media_isolation.run',return_value=failed),patch('webapp.telemetry.emit',side_effect=collect):
            result=media_service.request(self.input,media_service.command('probe'),10)
        self.assertEqual(result.returncode,127)
        self.assertTrue(any(e.get('native_dependency')=='libblas.so.3' and e.get('request_id')=='e'*32 for e in events))
        self.assertTrue(any(e.get('event')=='phase.finished' and e.get('outcome')=='failed' for e in events))
        from webapp.app import create_app
        from webapp.media_isolation import MediaDecoderUnavailable
        from fastapi.testclient import TestClient
        app=create_app(self.root/'api',False)
        with TestClient(app) as client:
            project=client.post('/api/projects',json={'name':'decoder outage'}).json()['id']
            with patch('webapp.app.inspect_media',side_effect=MediaDecoderUnavailable('decoder unavailable')):
                response=client.post(f'/api/projects/{project}/assets',files={'file':('test.wav',b'input','audio/wav')})
            self.assertEqual(response.status_code,503,response.text)
            self.assertTrue(response.headers.get('x-request-id'))
            self.assertFalse(list((app.state.store.project_dir(project)/'sources').iterdir()))
