import asyncio
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import httpx
from PIL import Image

from webapp.app import create_app
from webapp.api_maintenance import sweep_once
from webapp.demo_library import DemoLibrary
from tests.test_demo_library import source_library


class APIPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=create_app(self.root/'node',False);self.store=self.app.state.store
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')
        self.project=(await self.client.post('/api/projects',json={'name':'publication'})).json()['id']
        self.tasks=[];self.events=[]
    async def asyncTearDown(self):
        for event in self.events:event.set()
        for task in self.tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        await self.client.aclose()
        await self.until(lambda:all(not p['active'] for p in self.app.state.api_execution.snapshot().values()))
        self.app.state.api_execution.close();self.temp.cleanup()
    async def until(self,predicate):
        async with asyncio.timeout(4):
            while not predicate():await asyncio.sleep(.005)
    def barrier(self):
        started=threading.Event();release=threading.Event();self.events += [started,release]
        return started,release
    def launch(self,request):
        task=asyncio.create_task(request);self.tasks.append(task);return task

    async def test_image_import_rechecks_source_and_deletion_before_publication(self):
        for change in ('source','delete','cancel'):
            with self.subTest(change=change):
                pid=(await self.client.post('/api/projects',json={'name':change})).json()['id']
                job=self.store.add_job(pid,'image',{})
                out=self.store.project_dir(pid)/'edit/jobs'/job['id'];out.mkdir(parents=True)
                Image.new('RGBA',(16,16),'red').save(out/'foreground.png')
                self.store.finish(job['id'],'succeeded',['foreground.png'])
                started,release=self.barrier();data=(out/'foreground.png').read_bytes()
                def normalize(source):started.set();release.wait(3);return data,16,16
                with patch('webapp.media_isolation.normalize_image',side_effect=normalize):
                    task=self.launch(self.client.post(f'/api/projects/{pid}/image-import',json={'job':job['id'],'names':['foreground.png']}))
                    await self.until(started.is_set)
                    if change=='source':self.store.finish(job['id'],'failed',[])
                    elif change=='delete':self.assertEqual((await self.client.delete(f'/api/projects/{pid}')).status_code,200)
                    else:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):await task
                    release.set()
                    if change!='cancel':self.assertIn((await task).status_code,(404,409))
                    await self.until(lambda:self.app.state.api_execution.pools['media'].snapshot()['active']==0)
                self.assertEqual(self.store.assets(pid),[])
                self.assertEqual(list((self.store.root/'api-incoming').iterdir()),[])

    async def test_demo_import_does_not_overwrite_concurrent_user_draft(self):
        source=self.root/'library';source_library(source);DemoLibrary(self.store).publish(source)
        started,release=self.barrier()
        from webapp.app import inspect_media
        def slow(*args,**kwargs):started.set();release.wait(3);return inspect_media(*args,**kwargs)
        with patch('webapp.app.inspect_media',side_effect=slow):
            task=self.launch(self.client.post('/api/demos/tiny/load'))
            await self.until(started.is_set)
            pid=next(p['id'] for p in self.store.query('SELECT id FROM projects') if p['id']!=self.project and p['id']!=DemoLibrary(self.store).snapshot().record['project'])
            response=await self.client.put(f'/api/projects/{pid}/draft',json={'prompt':'user changed this while importing'})
            self.assertEqual(response.status_code,200,response.text)
            release.set();response=await task
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.store.draft(pid)['prompt'],'user changed this while importing')
        self.assertEqual(self.store.assets(pid),[])
        self.assertEqual(list((self.store.root/'api-incoming').iterdir()),[])

    async def test_total_request_deadline_does_not_publish_late_result(self):
        self.app.state.api_execution.request_seconds['upload']=.05
        started,release=self.barrier();data=io.BytesIO();Image.new('RGB',(16,16),'blue').save(data,format='PNG')
        def slow(path):started.set();release.wait(3);return {'kind':'image','width':16,'height':16,'size':path.stat().st_size}
        with patch('webapp.app.inspect_image',side_effect=slow):
            task=self.launch(self.client.post(f'/api/projects/{self.project}/assets',files={'file':('image.png',data.getvalue(),'image/png')}))
            await self.until(started.is_set);response=await task
            self.assertEqual(response.status_code,503,response.text)
            self.assertEqual(response.json()['code'],'API_REQUEST_TIMEOUT')
            self.assertEqual(response.json()['outcome'],'unknown')
            release.set();await self.until(lambda:self.app.state.api_execution.pools['media'].snapshot()['active']==0)
        self.assertEqual(self.store.assets(self.project),[])

    async def test_maintenance_retains_live_projects_and_refuses_cleanup_when_database_unknown(self):
        root=self.store.root
        orphan='d'*32;recent='e'*32;incoming=root/'api-incoming'/('a'*32)
        for path in (self.store.project_dir(orphan),self.store.project_dir(recent),incoming):path.mkdir(parents=True)
        for path in (self.store.project_dir(orphan),self.store.project_dir(self.project),incoming):os.utime(path,(time.time()-10000,)*2)
        with patch.object(self.store,'query',side_effect=RuntimeError('database unavailable')):
            with self.assertRaises(RuntimeError):await sweep_once(self.store,self.app.state.api_execution)
        self.assertTrue(self.store.project_dir(orphan).exists())
        await sweep_once(self.store,self.app.state.api_execution)
        self.assertFalse(self.store.project_dir(orphan).exists());self.assertFalse(incoming.exists())
        self.assertTrue(self.store.project_dir(self.project).exists());self.assertTrue(self.store.project_dir(recent).exists())
