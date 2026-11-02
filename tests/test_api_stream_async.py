import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import anyio
import httpx
import redis.asyncio as redis_async
from tests.distributed_support import free_port
from webapp import agent_store
from webapp.agent_stream import StreamBus
from webapp.app import create_app


class AsyncSSETests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.port=free_port()
        self.redis=subprocess.Popen(['redis-server','--bind','127.0.0.1','--port',str(self.port),
            '--save','','--appendonly','no','--dir',str(self.root)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.admin=redis_async.Redis(host='127.0.0.1',port=self.port)
        for _ in range(100):
            try:
                if await self.admin.ping():break
            except Exception:await asyncio.sleep(.02)
        else:self.fail('Redis fixture did not start')
        self.environment=patch.dict(os.environ,{'VIDEO_USE_REDIS_URL':f'redis://127.0.0.1:{self.port}/0','VIDEO_USE_API_STREAM_LIMIT':'2'});self.environment.start()
        self.transport=StreamBus();self.bus_patch=patch('webapp.agent_stream._bus',self.transport);self.bus_patch.start()
        self.app=create_app(self.root/'app',False);self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')
        self.project=(await self.client.post('/api/projects',json={'name':'async stream'})).json()['id']
        self.store=self.app.state.store;self.task=agent_store.add_turn(self.store,self.project,'plan','stream')
        self.url=f'/api/projects/{self.project}/agent/{self.task}/stream';self.requests=[]

    async def asyncTearDown(self):
        for request in self.requests:request.cancel()
        await asyncio.gather(*self.requests,return_exceptions=True)
        await self.client.aclose();self.app.state.api_execution.close()
        await self.admin.aclose()
        if self.transport._client is not None:self.transport._client.close()
        self.bus_patch.stop();self.environment.stop()
        if self.redis.poll() is None:self.redis.terminate();self.redis.wait(timeout=3)
        self.temp.cleanup()

    async def clients(self):return (await self.admin.info('clients'))['connected_clients']

    async def wait_for(self,predicate):
        async with asyncio.timeout(3):
            while not await predicate():await asyncio.sleep(.01)

    async def test_native_redis_replay_and_terminal_close_connection(self):
        agent_store.event(self.store,self.task,'progress',{'text':'step complete'})
        agent_store.finish(self.store,self.task,'succeeded','done')
        baseline=await self.clients()
        with (patch.object(self.transport,'read',side_effect=AssertionError('Synchronous Redis read used')),
              patch('asyncio.to_thread',side_effect=AssertionError('Default executor used'))):
            response=await asyncio.wait_for(self.client.get(self.url),3)
        self.assertEqual(response.status_code,200);self.assertIn('event: done',response.text)
        self.assertIn('step complete',response.text);self.assertIn('"stream_transport": "redis"',response.text)
        self.assertEqual(await self.clients(),baseline)
        self.assertEqual(self.app.state.api_execution.streams.snapshot()['active'],0)

    async def test_disconnect_releases_connections_and_stream_limit_preserves_health(self):
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.task,))
        baseline=await self.clients()
        self.requests=[asyncio.create_task(self.client.get(self.url)) for _ in range(2)]
        async def connected():return await self.clients()>=baseline+2
        await self.wait_for(connected)
        full=await self.client.get(self.url);self.assertEqual(full.status_code,503)
        self.assertEqual(full.json()['code'],'API_QUEUE_FULL');self.assertTrue(full.headers.get('x-request-id'))
        health=await asyncio.wait_for(self.client.get('/api/health'),.2);self.assertEqual(health.status_code,200)
        self.assertEqual(anyio.to_thread.current_default_thread_limiter().borrowed_tokens,0)
        for request in self.requests:request.cancel()
        await asyncio.gather(*self.requests,return_exceptions=True)
        async def closed():return await self.clients()==baseline
        await self.wait_for(closed);self.assertEqual(self.app.state.api_execution.streams.snapshot()['active'],0)

    async def test_redis_disconnect_falls_back_to_durable_events(self):
        agent_store.event(self.store,self.task,'progress',{'text':'durable event'})
        agent_store.finish(self.store,self.task,'succeeded','done')
        self.redis.terminate();self.redis.wait(timeout=3)
        response=await asyncio.wait_for(self.client.get(self.url),3)
        self.assertEqual(response.status_code,200);self.assertIn('durable event',response.text)
        self.assertIn('"stream_transport": "database"',response.text);self.assertIn('event: done',response.text)
        self.assertFalse(self.transport.enabled)
