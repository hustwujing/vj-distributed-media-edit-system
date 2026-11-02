import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import anyio
import httpx
from PIL import Image
from webapp.app import create_app


class APIIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_database_failure_returns_correlated_503_and_health_survives(self):
        from psycopg import OperationalError
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(Path(folder),False)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    with patch('webapp.auth.Sessions.resolve',side_effect=OperationalError('private database detail')):
                        response=await client.get('/api/projects')
                        self.assertEqual(response.status_code,503)
                        self.assertEqual(response.json()['code'],'API_DATABASE_UNAVAILABLE')
                        self.assertTrue(response.headers.get('x-request-id'))
                        self.assertNotIn('private database detail',response.text)
                        self.assertEqual((await client.get('/api/health')).status_code,200)
            finally:app.state.api_execution.close()

    async def test_slow_thumbnails_do_not_consume_default_workers_or_block_health(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(Path(folder),False);stop=threading.Event();started=[]
            thumbnail=Path(folder)/'thumb.jpg';Image.new('RGB',(20,20),'blue').save(thumbnail)
            def slow(*args,**kwargs):started.append(True);stop.wait(2);return thumbnail
            transport=httpx.ASGITransport(app=app)
            limiter=anyio.to_thread.current_default_thread_limiter();old=limiter.total_tokens;limiter.total_tokens=4
            async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
                project=(await client.post('/api/projects',json={'name':'isolation'})).json()['id']
                store=app.state.store;source=store.project_dir(project)/'sources/clip.mp4';source.write_bytes(b'fixture')
                store.add_asset(project,'clip','clip.mp4','clip.mp4',{'kind':'video','width':20,'height':20,'duration':1})
                tasks=[]
                try:
                    with patch('webapp.app.thumbnail',side_effect=slow):
                        tasks=[asyncio.create_task(client.get(f'/api/projects/{project}/assets/clip/thumbnail')) for _ in range(8)]
                        async with asyncio.timeout(1):
                            while not started:await asyncio.sleep(.005)
                        await asyncio.sleep(.05)
                        health=await asyncio.wait_for(client.get('/api/health'),.2)
                        self.assertEqual(health.status_code,200)
                        self.assertEqual(limiter.borrowed_tokens,0,'Dependency wait occupied the default thread pool')
                        listing=await asyncio.wait_for(client.get('/api/projects'),.2)
                        self.assertEqual(listing.status_code,200)
                finally:
                    stop.set();await asyncio.gather(*tasks,return_exceptions=True);limiter.total_tokens=old
                    if hasattr(app.state,'api_execution'):app.state.api_execution.close()
