import asyncio
import contextvars
import threading
import time
import unittest
from webapp.api_execution import BoundedExecutor,APICapacityError,AsyncAdmission


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():await asyncio.sleep(.005)


class APIExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.pools=[];self.events=[]
    async def asyncTearDown(self):
        for event in self.events:event.set()
        for pool in self.pools:pool.close()
        await until(lambda:all(p.snapshot()['active']==0 for p in self.pools))
    def pool(self,name='media',workers=1,queue=1,**kwargs):
        pool=BoundedExecutor(name,workers,queue,**kwargs);self.pools.append(pool);return pool
    def blocker(self):
        event=threading.Event();self.events.append(event);return event

    async def test_queue_bound_and_dependency_capacity_are_independent(self):
        pool=self.pool();health=self.pool('critical');event=self.blocker()
        first=asyncio.create_task(pool.run(event.wait));await until(lambda:pool.snapshot()['active']==1)
        second=asyncio.create_task(pool.run(lambda:'next'));await until(lambda:pool.snapshot()['queued']==1)
        with self.assertRaises(APICapacityError) as rejected:await pool.run(lambda:'must not run')
        self.assertEqual(rejected.exception.reason,'queue_full');self.assertFalse(rejected.exception.started)
        self.assertEqual(await asyncio.wait_for(health.run(lambda:'ok'),.2),'ok')
        event.set();await first;self.assertEqual(await second,'next')
        self.assertEqual(pool.snapshot()['rejected'],1)

    async def test_cancelling_running_work_keeps_real_slot_and_drops_queued_work(self):
        pool=self.pool();event=self.blocker();called=[]
        first=asyncio.create_task(pool.run(event.wait));await until(lambda:pool.snapshot()['active']==1)
        queued=asyncio.create_task(pool.run(lambda:called.append(True)));await until(lambda:pool.snapshot()['queued']==1)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):await queued
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):await first
        self.assertEqual(pool.snapshot()['active'],1);self.assertEqual(pool.snapshot()['queued'],0)
        event.set();await until(lambda:pool.snapshot()['active']==0);self.assertEqual(called,[])

    async def test_execution_timeout_does_not_release_unfinished_work(self):
        pool=self.pool(queue=0,execution_timeout=.03);event=self.blocker()
        with self.assertRaises(APICapacityError) as timed:await pool.run(event.wait)
        self.assertTrue(timed.exception.started);self.assertEqual(timed.exception.reason,'execution_timeout')
        self.assertEqual(pool.snapshot()['active'],1)
        with self.assertRaises(APICapacityError):await pool.run(lambda:None)
        event.set();await until(lambda:pool.snapshot()['active']==0)
        self.assertEqual(await pool.run(lambda:42),42)

    async def test_queue_timeout_never_invokes_function_and_context_is_preserved(self):
        pool=self.pool(queue_timeout=.03);event=self.blocker();called=[]
        first=asyncio.create_task(pool.run(event.wait));await until(lambda:pool.snapshot()['active']==1)
        with self.assertRaises(APICapacityError) as timed:await pool.run(lambda:called.append(True))
        self.assertFalse(timed.exception.started);self.assertEqual(pool.snapshot()['queued'],0)
        event.set();await first;self.assertEqual(called,[])
        marker=contextvars.ContextVar('test_marker');marker.set('request-context')
        self.assertEqual(await pool.run(marker.get),'request-context')

    async def test_closing_rejects_queued_work_and_waits_for_real_completion(self):
        pool=self.pool();event=self.blocker();first=asyncio.create_task(pool.run(event.wait))
        await until(lambda:pool.snapshot()['active']==1)
        queued=asyncio.create_task(pool.run(lambda:None));await until(lambda:pool.snapshot()['queued']==1)
        pool.close()
        with self.assertRaises(APICapacityError):await queued
        self.assertEqual(pool.snapshot()['active'],1)
        event.set();await first

    async def test_request_admission_bounds_waiters_and_releases_cancelled_grants(self):
        gate=AsyncAdmission('upload',1,1,.1);first=await gate.acquire()
        queued=asyncio.create_task(gate.acquire());await until(lambda:gate.snapshot()['queued']==1)
        with self.assertRaises(APICapacityError):await gate.acquire()
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):await queued
        self.assertEqual(gate.snapshot()['active'],1);self.assertEqual(gate.snapshot()['queued'],0)
        first.release();first.release();self.assertEqual(gate.snapshot()['active'],0)
        next_permit=await gate.acquire();next_permit.release()

    async def test_dependency_timeout_is_not_reported_as_executor_deadline(self):
        pool=self.pool()
        def fail():raise TimeoutError('dependency timeout')
        with self.assertRaises(TimeoutError):await pool.run(fail)
        self.assertEqual(pool.snapshot()['execution_timeout'],0)
        self.assertEqual(pool.snapshot()['failed'],1)

    async def test_disconnect_before_headers_is_not_a_server_error(self):
        from unittest.mock import patch
        from webapp.observation_http import ObservationMiddleware
        async def blocked(scope,receive,send):await asyncio.Future()
        async def receive():return {'type':'http.disconnect'}
        async def send(message):self.fail('Disconnected request sent a response')
        with patch('webapp.observation_http.emit') as emit:
            task=asyncio.create_task(ObservationMiddleware(blocked)({'type':'http','path':'/blocked','method':'GET','headers':[]},receive,send))
            await asyncio.sleep(0);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        event=emit.call_args;self.assertEqual(event.kwargs['status'],499);self.assertEqual(event.kwargs['outcome'],'disconnected')
