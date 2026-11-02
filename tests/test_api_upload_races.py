import asyncio
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import httpx
from PIL import Image
from webapp.app import create_app
from webapp.api_staging import Staging


class APIUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=create_app(self.root,False);self.store=self.app.state.store
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')
        self.project=(await self.client.post('/api/projects',json={'name':'upload'})).json()['id']
        output=io.BytesIO();Image.new('RGB',(20,20),'red').save(output,format='PNG');self.data=output.getvalue()
        self.events=[];self.tasks=[]
    async def asyncTearDown(self):
        for event in self.events:event.set()
        for task in self.tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        await self.client.aclose()
        async with asyncio.timeout(3):
            while any(p['active'] for p in self.app.state.api_execution.snapshot().values()):await asyncio.sleep(.005)
        self.app.state.api_execution.close();self.temp.cleanup()
    def event(self):
        value=threading.Event();self.events.append(value);return value
    async def until(self,predicate):
        async with asyncio.timeout(3):
            while not predicate():await asyncio.sleep(.005)
    def upload(self):
        task=asyncio.create_task(self.client.post(f'/api/projects/{self.project}/assets',files={'file':('image.png',self.data,'image/png')}))
        self.tasks.append(task);return task
    def metadata(self,path):return {'kind':'image','width':20,'height':20,'size':path.stat().st_size,'mime':'image/png'}

    async def test_slow_upload_does_not_hold_global_lock_and_deleted_project_is_not_resurrected(self):
        started=self.event();release=self.event()
        def inspect(path):started.set();release.wait(2);return self.metadata(path)
        with patch('webapp.app.inspect_image',side_effect=inspect):
            uploading=self.upload();await self.until(started.is_set)
            other=await asyncio.wait_for(self.client.post('/api/projects',json={'name':'other'}),.3)
            self.assertEqual(other.status_code,201)
            deleted=await asyncio.wait_for(self.client.delete(f'/api/projects/{self.project}'),.3)
            self.assertEqual(deleted.status_code,200)
            release.set();response=await uploading
        self.assertEqual(response.status_code,404)
        self.assertEqual(self.store.query('SELECT id FROM assets WHERE project=?',(self.project,)),[])
        self.assertFalse(self.store.project_dir(self.project).exists())
        self.assertEqual(list((self.root/'api-incoming').iterdir()),[])

    async def test_concurrent_uploads_recheck_asset_limit_in_commit(self):
        for n in range(49):self.store.add_asset(self.project,str(n),'old.png',str(n)+'.png',{'kind':'image','size':0})
        barrier=threading.Barrier(2)
        def inspect(path):barrier.wait(timeout=2);return self.metadata(path)
        with patch('webapp.app.inspect_image',side_effect=inspect):results=await asyncio.gather(self.upload(),self.upload())
        self.assertEqual(sorted(r.status_code for r in results),[201,409])
        self.assertEqual(len(self.store.assets(self.project)),50)
        self.assertEqual(len(list((self.store.project_dir(self.project)/'sources').iterdir())),1)

    async def test_cancel_during_processing_never_publishes(self):
        started=self.event();release=self.event()
        def inspect(path):started.set();release.wait(2);return self.metadata(path)
        with patch('webapp.app.inspect_image',side_effect=inspect):
            task=self.upload();await self.until(started.is_set);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(self.app.state.api_execution.pools['media'].snapshot()['active'],1)
            release.set();await self.until(lambda:self.app.state.api_execution.pools['media'].snapshot()['active']==0)
        self.assertEqual(self.store.assets(self.project),[])
        self.assertEqual(list((self.root/'api-incoming').iterdir()),[])

    async def test_cancel_before_commit_rolls_back_but_after_commit_keeps_file(self):
        for after_commit in (False,True):
            with self.subTest(after_commit=after_commit):
                reached=self.event();release=self.event();cleanup_started=self.event()
                original=self.store.add_asset;cleanup=Staging.cleanup
                def blocked():reached.set();release.wait(2)
                def add(*args,**kwargs):
                    value=original(*args,**kwargs)
                    if after_commit:self.store.notify_after_commit(blocked)
                    else:blocked()
                    return value
                def clean(stage):cleanup_started.set();return cleanup(stage)
                with patch.object(self.store,'add_asset',side_effect=add),patch.object(Staging,'cleanup',clean),patch('webapp.app.inspect_image',side_effect=self.metadata):
                    task=self.upload();await self.until(reached.is_set);task.cancel()
                    await self.until(cleanup_started.is_set);release.set()
                    with self.assertRaises(asyncio.CancelledError):await task
                    await self.until(lambda:self.app.state.api_execution.pools['write'].snapshot()['active']==0)
                assets=self.store.assets(self.project)
                self.assertEqual(len(assets),int(after_commit))
                if after_commit:self.assertEqual((self.store.project_dir(self.project)/'sources'/assets[0]['filename']).read_bytes(),self.data)

    async def test_upload_admission_precedes_multipart_body_consumption(self):
        release=self.event();started=[];read=[]
        def inspect(path):started.append(True);release.wait(2);return self.metadata(path)
        boundary='fixture-upload-boundary'
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\nContent-Type: image/png\r\n\r\n'.encode()+self.data+f'\r\n--{boundary}--\r\n'.encode())
        async def stream(index):read.append(index);yield body
        with patch('webapp.app.inspect_image',side_effect=inspect):
            tasks=[asyncio.create_task(self.client.post(f'/api/projects/{self.project}/assets',content=stream(i),headers={'content-type':'multipart/form-data; boundary='+boundary})) for i in range(8)]
            self.tasks.extend(tasks);await self.until(lambda:len(started)==2 and sum(t.done() for t in tasks)>=2)
            self.assertEqual(len(read),2)
            rejected=[t.result() for t in tasks if t.done()];self.assertEqual([r.status_code for r in rejected],[503,503])
            for task in tasks:
                if not task.done():task.cancel()
            release.set();await asyncio.gather(*tasks,return_exceptions=True)
