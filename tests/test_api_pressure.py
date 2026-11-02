"""Real two-process HTTP/PG/S3/Redis pressure and recovery, on private ports."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import requests
from PIL import Image
from tests.distributed_support import PostgresServer,S3Server,free_port
from webapp.artifact_store import S3Objects
from webapp.store import Store
from webapp import agent_store


class APIPressureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='api-pressure-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.signals=self.root/'signals';self.signals.mkdir()
        self.pg=PostgresServer();self.addCleanup(self.pg.close)
        self.s3=S3Server();self.addCleanup(self.s3.close)
        port=free_port()
        self.redis=subprocess.Popen(['redis-server','--bind','127.0.0.1','--port',str(port),'--save','','--appendonly','no'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop,self.redis)
        self.env={**os.environ,'VIDEO_USE_DATABASE_URL':self.pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':self.s3.endpoint,
            'VIDEO_USE_OBJECT_BUCKET':'pressure','AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1',
            'VIDEO_USE_SESSION_SECRET':'a1'*32,'VIDEO_USE_DISTRIBUTED':'1',
            'VIDEO_USE_OBJECT_ENCRYPTION_KEY':base64.b64encode(b'p'*32).decode(),'VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION':'1',
            'VIDEO_USE_REDIS_URL':f'redis://127.0.0.1:{port}/0','VIDEO_USE_API_DATABASE_SECONDS':'.5',
            'VU_PRESSURE_SIGNALS':str(self.signals),'VIDEO_USE_INSTANCE':'pressure-test','VIDEO_USE_TELEMETRY_ENABLED':'1'}
        for name in ('READ','STORAGE','MEDIA','DIAGNOSTICS','CONTROL'):
            self.env.update({f'VIDEO_USE_API_{name}_WORKERS':'1',f'VIDEO_USE_API_{name}_QUEUE':'1',f'VIDEO_USE_API_{name}_QUEUE_SECONDS':'.5'})
        self.config=patch.dict(os.environ,self.env);self.config.start();self.addCleanup(self.config.stop)
        S3Objects('pressure',self.s3.endpoint).client.create_bucket(Bucket='pressure')
        self.store=Store(self.root/'operator');agent_store.initialize(self.store)
        self.bases=[];self.logs=[]
        for instance in ('a','b'):
            port=free_port();base=f'http://127.0.0.1:{port}';self.bases.append(base)
            log=(self.root/f'api-{instance}.log').open('wb');self.logs.append(log);self.addCleanup(log.close)
            process=subprocess.Popen([sys.executable,'-m','tests.api_pressure_server'],env={**self.env,'VU_PRESSURE_ROOT':str(self.root/instance),'VU_PRESSURE_PORT':str(port),'VIDEO_USE_INSTANCE':'pressure-'+instance},stdout=log,stderr=log)
            self.addCleanup(self.stop,process)
            self.wait(lambda:self.healthy(base),timeout=15)
        port=free_port();self.proxy=f'http://127.0.0.1:{port}'
        conf=self.root/'nginx.conf'
        conf.write_text(f'pid {self.root}/nginx.pid; error_log {self.root}/nginx.log; events {{ worker_connections 128; }} http {{ access_log off; upstream pressure {{ least_conn; '+''.join('server '+base.split('://')[1]+';' for base in self.bases)+f' }} server {{ listen 127.0.0.1:{port}; location / {{ proxy_pass http://pressure; proxy_set_header Host $http_host; proxy_buffering off; }} }} }}')
        nginx=subprocess.Popen([shutil.which('nginx'),'-c',str(conf),'-g','daemon off;'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop,nginx);self.wait(lambda:self.healthy(self.proxy))
        self.session=requests.Session();self.session.trust_env=False;self.addCleanup(self.session.close)
        response=self.session.post(self.bases[0]+'/api/projects',json={'name':'pressure'},timeout=5)
        self.assertEqual(response.status_code,201,response.text);self.project=response.json()['id']
        output=io.BytesIO();Image.new('RGB',(20,20),'blue').save(output,format='PNG')
        response=self.session.post(self.bases[0]+f'/api/projects/{self.project}/assets',files={'file':('blue.png',output.getvalue(),'image/png')},timeout=10)
        self.assertEqual(response.status_code,201,response.text);self.asset=response.json()['id']
        video=self.root/'fixture.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=32x32:d=1','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(video)],check=True,capture_output=True,timeout=10)
        with video.open('rb') as stream:
            response=self.session.post(self.bases[0]+f'/api/projects/{self.project}/assets',files={'file':('fixture.mp4',stream,'video/mp4')},timeout=10)
        self.assertEqual(response.status_code,201,response.text);self.video=response.json()['id']
        self.owner=self.store.query('SELECT owner FROM projects WHERE id=?',(self.project,))[0]['owner']
        self.evidence=[]

    @staticmethod
    def stop(process):
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    @staticmethod
    def wait(predicate,timeout=4):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            if predicate():return
            time.sleep(.02)
        raise AssertionError('Timed out waiting for pressure state')
    @staticmethod
    def healthy(base):
        try:return requests.get(base+'/api/health',timeout=.2).status_code==200
        except requests.RequestException:return False
    def request(self,base,path,method='GET',body=None):
        with requests.Session() as session:
            session.trust_env=False;session.cookies.update(self.session.cookies)
            return session.request(method,base+path,json=body,timeout=10)
    def state(self,base):return self.request(base,'/__test/capacity').json()
    def task(self,terminal=False):
        turn=agent_store.add_turn(self.store,self.project,'plan','pressure fixture')
        if terminal:agent_store.finish(self.store,turn,'succeeded','fixture complete')
        return turn

    def test_two_apis_preserve_control_and_bound_all_slow_dependencies(self):
        for dependency in ('storage','media','diagnostics','control','read'):
            with self.subTest(dependency=dependency):
                turn=self.task(terminal=True);prefix=f'/api/projects/{self.project}'
                routes={'storage':(f'{prefix}/assets/{self.asset}/file','GET',None),
                        'media':(f'{prefix}/assets/{self.video}/thumbnail','GET',None),
                        'diagnostics':(f'{prefix}/diagnostics/{turn}','GET',None),
                        'control':(f'{prefix}/agent/{turn}/control','POST',{'message':'fixture ambiguous control'}),
                        'read':('/api/projects','GET',None)}
                path,method,body=routes[dependency];signal=self.signals/dependency;signal.touch()
                with ThreadPoolExecutor(max_workers=12) as clients:
                    futures=[clients.submit(self.request,base,path,method,body) for base in self.bases for _ in range(5)]
                    try:
                        self.wait(lambda:all(self.state(base)['pools'][dependency]['active']==1 for base in self.bases))
                        self.wait(lambda:sum(f.done() for f in futures)>=6)
                        states=[self.state(base) for base in self.bases]
                        for state in states:
                            pool=state['pools'][dependency]
                            self.assertEqual(pool['active'],1);self.assertLessEqual(pool['queued'],1)
                            self.assertEqual(state['default_threads'],0)
                        latencies=[]
                        for base in [*self.bases,self.proxy]:
                            started=time.monotonic();health=self.request(base,'/api/health');latencies.append(time.monotonic()-started)
                            self.assertEqual(health.status_code,200)
                            if dependency!='read':self.assertEqual(self.request(base,'/api/projects').status_code,200)
                            target=self.task();started=time.monotonic()
                            cancelled=self.request(base,f'{prefix}/agent/{target}/control','POST',{'message':'取消'})
                            latencies.append(time.monotonic()-started)
                            self.assertEqual(cancelled.status_code,200,cancelled.text)
                            self.assertEqual(self.store.query('SELECT status FROM agent_turns WHERE id=?',(target,))[0]['status'],'cancelled')
                        self.assertLess(max(latencies),1.)
                        rejected=[f.result() for f in futures if f.done()]
                        self.assertTrue(any(r.status_code==503 for r in rejected))
                        for response in rejected:
                            if response.status_code==503:
                                self.assertEqual(response.headers.get('retry-after'),'1');self.assertTrue(response.headers.get('x-request-id'))
                        self.evidence.append({'dependency':dependency,'max_health_cancel_seconds':round(max(latencies),4),'states':states})
                    finally:signal.unlink(missing_ok=True)
                    responses=[f.result() for f in futures]
                    self.assertTrue(all(r.status_code in (200,503) for r in responses),[(r.status_code,r.text[:100]) for r in responses])
                self.wait(lambda:all(self.state(base)['pools'][dependency]['active']==0 for base in self.bases))
                self.assertEqual(self.request(self.bases[0],path,method,body).status_code,200)
        target=Path('docs/test-evidence/api-pressure.json');target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps({'passed':True,'instances':2,'dependencies':self.evidence,'infrastructure':'real HTTP API processes, PostgreSQL, encrypted HTTP S3 fixture, Redis; dependency delays injected only in test processes'},indent=2)+'\n')

    def test_database_failure_is_bounded_and_does_not_block_health_or_leak_ownership(self):
        import psycopg
        with psycopg.connect(self.pg.dsn) as lock:
            lock.execute('LOCK TABLE projects IN ACCESS EXCLUSIVE MODE')
            with ThreadPoolExecutor(max_workers=2) as clients:
                pending=[clients.submit(self.request,base,f'/api/projects/{self.project}') for base in self.bases]
                for base in self.bases:
                    self.assertEqual(self.request(base,'/api/health').status_code,200)
                for future in pending:
                    response=future.result(timeout=3);self.assertEqual(response.status_code,503,response.text)
                    self.assertEqual(response.json()['code'],'API_DATABASE_UNAVAILABLE')
                    self.assertNotIn(self.owner,response.text)
        for base in self.bases:self.assertEqual(self.request(base,f'/api/projects/{self.project}').status_code,200)

    def test_physical_cleanup_is_outside_transaction_and_recovers(self):
        signal=self.signals/'cleanup';signal.touch()
        try:
            response=self.request(self.bases[0],f'/api/projects/{self.project}','DELETE')
            self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['cleanup_pending'])
            self.assertEqual(self.store.query('SELECT id FROM projects WHERE id=?',(self.project,)),[])
            self.assertEqual(self.state(self.bases[0])['pools']['cleanup']['active'],1)
            for base in self.bases:
                response=self.request(base,'/api/projects','POST',{'name':'while cleanup blocked'})
                self.assertEqual(response.status_code,201,response.text)
        finally:signal.unlink(missing_ok=True)
        self.wait(lambda:self.state(self.bases[0])['pools']['cleanup']['active']==0)
        self.assertFalse((self.root/'a/projects'/self.project).exists())

    def test_real_upload_disconnect_never_publishes_late_decoder_result(self):
        signal=self.signals/'media';signal.touch()
        output=io.BytesIO();Image.new('RGB',(16,16),'red').save(output,format='PNG')
        boundary='private-pressure-boundary'
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="cancelled.png"\r\nContent-Type: image/png\r\n\r\n'.encode()+output.getvalue()+f'\r\n--{boundary}--\r\n'.encode())
        port=int(self.bases[0].rsplit(':',1)[1]);connection=socket.create_connection(('127.0.0.1',port),timeout=3)
        cookie='; '.join(f'{c.name}={c.value}' for c in self.session.cookies)
        headers=f'POST /api/projects/{self.project}/assets HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nCookie: {cookie}\r\nContent-Type: multipart/form-data; boundary={boundary}\r\nContent-Length: {len(body)}\r\n\r\n'
        before=len(self.store.assets(self.project))
        try:
            connection.sendall(headers.encode()+body)
            self.wait(lambda:self.state(self.bases[0])['pools']['media']['active']==1)
            connection.shutdown(socket.SHUT_RDWR);connection.close()
            self.wait(lambda:self.state(self.bases[0])['pools']['media']['cancelled']>=1,timeout=2)
            self.assertEqual(self.state(self.bases[0])['pools']['media']['active'],1)
        finally:connection.close();signal.unlink(missing_ok=True)
        self.wait(lambda:self.state(self.bases[0])['pools']['media']['active']==0)
        self.assertEqual(len(self.store.assets(self.project)),before)
        self.assertEqual(list((self.root/'a/api-incoming').iterdir()),[])
        def reported_disconnect():
            rows=[]
            for line in (self.root/'api-a.log').read_text().splitlines():
                try:rows.append(json.loads(line))
                except ValueError:pass
            return any(r.get('event')=='http.finished' and r.get('status')==499 and r.get('outcome')=='disconnected' for r in rows)
        self.wait(reported_disconnect)
