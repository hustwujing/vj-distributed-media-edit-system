"""Integration checks for web isolation, inputs and the real local video pipeline."""
import json
import subprocess
import tempfile
import time
import unittest
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from tests.isolation import temporary_root as isolated_tmp_root
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp.app import create_app


@contextmanager
def executor_app(root):
    from webapp.agent_host import AgentHost
    # A short private registry avoids both production reaper interference and
    # Linux's 108-byte UNIX socket path limit in nested test directories.
    with tempfile.TemporaryDirectory(prefix='vw-') as node:
        with patch.dict(os.environ,{'VIDEO_USE_EXECUTOR_ROOT':node,
                                    'VIDEO_USE_UID_START':'620000','VIDEO_USE_UID_COUNT':'16'}):
            host=AgentHost(root)
        thread=threading.Thread(target=host.run,daemon=True)
        thread.start()
        try:
            with TestClient(create_app(root)) as client:yield client
        finally:
            host.draining.set()
            thread.join(timeout=15)
            if thread.is_alive():
                host.stop.set();thread.join(timeout=15)
            if thread.is_alive():raise AssertionError('Executor did not drain')


class WebTests(unittest.TestCase):
    def test_project_deletion_waits_for_physical_cleanup_and_removes_lifecycle_records(self):
        from webapp import agent_store
        from webapp.task_leases import Coordinator
        store=self.app.state.store
        turn=agent_store.add_turn(store,self.project,'plan','test')
        lease=Coordinator(store,'test-delete','test').claim(turn,'agent')
        store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'quarantined',?)",(lease.attempt,turn,'test-delete',store.now()))
        store.execute("UPDATE agent_turns SET status='cancelled' WHERE id=?",(turn,))
        store.execute('UPDATE execution_leases SET active=0,expires=0 WHERE turn_id=?',(turn,))
        endpoint=f'/api/projects/{self.project}'
        self.assertEqual(self.client.delete(endpoint).status_code,409)
        store.execute("UPDATE execution_cleanup SET state='complete' WHERE task_id=?",(turn,))
        store.execute("INSERT INTO execution_external_cleanup(id,task_id,provider,external_id,state) VALUES (?,?,?,'known','pending')",('cancel',turn,'http://localhost'))
        self.assertEqual(self.client.delete(endpoint).status_code,409)
        store.execute("UPDATE execution_external_cleanup SET state='complete'")
        response=self.client.delete(endpoint)
        self.assertEqual(response.status_code,200,response.text)
        for table in ('execution_limits','execution_cleanup','execution_external_cleanup'):
            self.assertFalse(store.query(f'SELECT * FROM {table} WHERE task_id=?',(turn,)))

    def test_progress_and_cancel_control_preserve_turn_and_enforce_owner(self):
        from webapp import agent_store
        store=self.app.state.store
        turn=agent_store.add_turn(store,self.project,'plan','制作视频')
        base=f'/api/projects/{self.project}/agent/{turn}'
        before=len(store.query('SELECT id FROM agent_turns'))
        with patch('webapp.agent_host.AgentHost.run_turn') as run:
            response=self.client.post(base+'/control',json={'message':'查进度'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertIn('排队',response.json()['summary'])
            run.assert_not_called()
        self.assertEqual(len(store.query('SELECT id FROM agent_turns')),before)
        with TestClient(self.app) as other:
            self.assertEqual(other.get(base+'/status').status_code,404)
            self.assertEqual(other.post(base+'/control',json={'message':'取消任务'}).status_code,404)
        response=self.client.post(base+'/control',json={'message':'取消任务'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['turn']['status'],'cancelled')
        self.assertEqual(response.json()['turn']['execution']['termination_reason'],'cancelled')

    @classmethod
    def setUpClass(cls):
        temporary_root = isolated_tmp_root()
        temporary_root.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='video-use-web-tests-', dir=temporary_root)
        cls.root = Path(cls.temp.name)
        cls.clip = cls.root / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=24',
                        '-f', 'lavfi', '-i', 'sine=f=440:r=48000', '-t', '2', '-c:v', 'libx264',
                        '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(cls.clip)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=self.root)
        self.app = create_app(Path(self.directory.name), run_worker=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.project = self.client.post('/api/projects', json={'name':'测试项目'}).json()['id']

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def upload(self, name='source.mp4'):
        response = self.client.post(f'/api/projects/{self.project}/assets',
                                    files={'file':(name, self.clip.read_bytes(), 'video/mp4')})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def test_browser_sessions_isolate_projects_and_media(self):
        asset = self.upload()
        with TestClient(self.app) as other:
            self.assertEqual(other.get('/api/projects').json(), [])
            for path in [f'/api/projects/{self.project}', f'/api/projects/{self.project}/assets/{asset}/file']:
                self.assertEqual(other.get(path).status_code, 404)

    def test_upload_sanitizes_names_and_rejects_nonvideo(self):
        self.upload('../../escape.mp4')
        files = list((Path(self.directory.name) / 'projects' / self.project / 'sources').iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent.name, 'sources')
        self.assertEqual(self.client.post(f'/api/projects/{self.project}/assets',
                         files={'file':('bad.mp4', b'not a video')}).status_code, 422)
        self.assertEqual(self.client.post(f'/api/projects/{self.project}/assets',
                         files={'file':('.env', b'fake')}).status_code, 415)

    def test_clip_bounds_tracks_and_caption_prerequisites(self):
        asset = self.upload()
        endpoint = f'/api/projects/{self.project}/jobs'
        segment = {'asset':asset,'start':0,'end':1}
        for body in [
            {'kind':'render','segments':[{**segment,'end':5}]},
            {'kind':'render','segments':[{**segment,'audio_track':3}]},
            {'kind':'render','segments':[segment],'subtitles':'transcript'},
            {'kind':'timeline','assets':[asset],'start':1,'end':.5},
            {'kind':'render','segments':[segment],'grade':'movie=/etc/passwd'},
            {'kind':'render','segments':[{**segment,'asset':'/etc/passwd'}]},
        ]:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(endpoint,json=body).status_code, 422)
        response = self.client.post(endpoint,json={'kind':'render','segments':[segment]})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'],'queued')
        self.assertNotIn('payload',response.json())

    def test_cancel_delete_and_cross_site_request(self):
        asset=self.upload()
        response=self.client.post(f'/api/projects/{self.project}/jobs',json={'kind':'timeline','assets':[asset],'end':1})
        job=response.json()['id']
        self.assertEqual(self.client.delete(f'/api/projects/{self.project}').status_code,409)
        self.client.post(f'/api/projects/{self.project}/jobs/{job}/cancel')
        self.assertEqual(self.client.get(f'/api/projects/{self.project}').json()['jobs'][0]['status'],'cancelled')
        self.assertEqual(self.client.post('/api/projects',json={'name':'bad'},headers={'Origin':'https://unrelated.example'}).status_code,403)
        self.assertEqual(self.client.delete(f'/api/projects/{self.project}').status_code,200)

    def test_batch_delete_isolated_and_partially_blocks_active_projects(self):
        normal=self.client.post('/api/projects',json={'name':'可删除'}).json()['id']
        active=self.client.post('/api/projects',json={'name':'运行中'}).json()['id']
        self.app.state.store.add_job(active,'pack',{'kind':'pack'})
        response=self.client.post('/api/projects/batch-delete',json={'ids':[normal,active,normal]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['deleted'],[normal])
        self.assertEqual(response.json()['blocked'][0]['id'],active)
        self.assertEqual(self.client.get(f'/api/projects/{normal}').status_code,404)
        self.assertEqual(self.client.get(f'/api/projects/{active}').status_code,200)
        with TestClient(self.app) as other:
            foreign=other.post('/api/projects',json={'name':'其他用户'}).json()['id']
            denied=self.client.post('/api/projects/batch-delete',json={'ids':[self.project,foreign]})
            self.assertEqual(denied.status_code,404)
            self.assertEqual(self.client.get(f'/api/projects/{self.project}').status_code,200)
            self.assertEqual(other.get(f'/api/projects/{foreign}').status_code,200)

    def test_credentials_and_private_job_files_are_not_exposed(self):
        self.assertNotIn('sk_',self.client.get('/api/health').text)
        self.assertEqual(self.client.get('/.env').status_code,404)
        job=self.app.state.store.add_job(self.project,'pack',{'kind':'pack'})
        out=self.app.state.store.project_dir(self.project)/'edit'/'jobs'/job['id']
        out.mkdir(parents=True)
        (out/'job.log').write_text('error: private-test-key')
        with patch('webapp.app.read_key',return_value='private-test-key'):
            text=self.client.get(f'/api/projects/{self.project}/jobs/{job["id"]}/log').text
            self.assertNotIn('private-test-key',text)
        self.assertEqual(self.client.get(f'/api/projects/{self.project}/jobs/{job["id"]}/files/job.json').status_code,404)

    def test_api_restart_does_not_modify_an_executors_running_task(self):
        job=self.app.state.store.add_job(self.project,'transcribe',{'kind':'transcribe'})
        self.app.state.store.execute("UPDATE jobs SET status='running' WHERE id=?",(job['id'],))
        app=create_app(Path(self.directory.name),run_worker=False)
        with TestClient(app):
            self.assertEqual(app.state.store.job(job['id'])['status'],'running')

    @unittest.skipUnless(os.geteuid()==0,'Requires the privileged execution service')
    def test_full_local_jobs_produce_playable_video_and_timeline(self):
        asset=self.upload()
        with executor_app(Path(self.directory.name)) as runner:
            runner.cookies.update(self.client.cookies)
            def job(body):
                response=runner.post(f'/api/projects/{self.project}/jobs',json=body)
                self.assertEqual(response.status_code,202,response.text)
                job_id=response.json()['id']
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    record=next(j for j in runner.get(f'/api/projects/{self.project}').json()['jobs'] if j['id']==job_id)
                    if record['status'] in {'succeeded','failed'}:
                        self.assertEqual(record['status'],'succeeded',record.get('error'))
                        return record
                    time.sleep(.2)
                self.fail('job timed out: '+json.dumps(record,ensure_ascii=False))
            render=job({'kind':'render','segments':[{'asset':asset,'start':0,'end':.8}, {'asset':asset,'start':1,'end':1.8}],
                        'grade':'neutral_punch','fps':'30','normalize_audio':True})
            artifact=next(a for a in render['artifacts'] if a['name']=='final.mp4')
            response=runner.get('/'+artifact['url'],headers={'Range':'bytes=0-99'})
            self.assertEqual(response.status_code,206)
            self.assertEqual(len(response.content),100)
            local=self.root/'render-check.mp4'
            local.write_bytes(runner.get('/'+artifact['url']).content)
            info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(local)]))
            video=next(s for s in info['streams'] if s['codec_type']=='video')
            self.assertEqual(video['avg_frame_rate'],'30/1')
            self.assertEqual(video['width'],1280)
            timeline=job({'kind':'timeline','assets':[asset],'end':1.8,'frames':3})
            self.assertEqual(runner.get('/'+timeline['artifacts'][0]['url']).content[:8],b'\x89PNG\r\n\x1a\n')


if __name__ == '__main__':
    unittest.main()
