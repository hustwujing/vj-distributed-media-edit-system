"""Bounded API work with independent capacity and honest cancellation.

Admission happens before submitting to ThreadPoolExecutor. A caller leaving or
timing out does not free a running worker: only the actual completion does.
"""
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import contextvars
from dataclasses import dataclass,field
import functools
import inspect
import os
import threading
import time


CURRENT_WORK=contextvars.ContextVar('api_work',default=None)
REQUEST_WORK=contextvars.ContextVar('api_request_work',default=None)


class APICapacityError(RuntimeError):
    def __init__(self,pool,reason,started=False,prior_work=False):
        self.pool=pool;self.reason=reason;self.started=started;self.prior_work=prior_work
        super().__init__('API dependency unavailable')


class APIWorkCancelled(RuntimeError):pass
class APIClientDisconnected(RuntimeError):pass


async def while_connected(request,invoke):
    """FastAPI has parsed the body before calling this endpoint wrapper.

    Uvicorn does not cancel ordinary handlers when the peer leaves. Observe the
    disconnect here, without competing with multipart/body parsing or SSE's own
    response lifecycle. Cancellation cannot undo a completed database commit.
    """
    if request is None:return await invoke()
    if not getattr(request,'_stream_consumed',False):
        # No-body routes still have one empty ASGI request frame. Drain it
        # before a cancellable receive; interrupting that first receive can
        # leave a middleware's request generator suspended. Do not consume an
        # unsupported, potentially unbounded body on a no-body endpoint.
        if request.headers.get('transfer-encoding') or request.headers.get('content-length','0') not in ('','0'):
            return await invoke()
        await request.body()
    async def disconnected():
        while True:
            message=await request.receive()
            if message['type']=='http.disconnect':return
    work=asyncio.create_task(invoke());watcher=asyncio.create_task(disconnected())
    try:
        await asyncio.wait((work,watcher),return_when=asyncio.FIRST_COMPLETED)
        if work.done():return work.result()
        watcher.result()
        raise APIClientDisconnected('HTTP client disconnected')
    finally:
        for task in (work,watcher):
            if not task.done():task.cancel()
        # A surrounding ASGI cancellation scope must not repeatedly cancel the
        # child's bounded staging cleanup while we are joining it.
        import anyio
        with anyio.CancelScope(shield=True):
            await asyncio.shield(asyncio.gather(work,watcher,return_exceptions=True))


def check_cancelled():
    work=CURRENT_WORK.get()
    if work and work.abandoned.is_set():raise APIWorkCancelled('API request no longer waiting')


def install_interrupt(callback):
    """Install a nonblocking local handle interruption (for example shutdown)."""
    work=CURRENT_WORK.get()
    if work is None:return lambda:None
    with work.interrupt_lock:
        stopped=work.abandoned.is_set()
        if not stopped:work.interrupts.append(callback)
    if stopped:callback()
    def remove():
        with work.interrupt_lock:
            if callback in work.interrupts:work.interrupts.remove(callback)
    return remove


class Permit:
    def __init__(self,gate,loop):
        self.gate=gate;self.loop=loop;self.future=loop.create_future();self.granted=False;self.released=False
    def release(self):self.gate.release(self)


class AsyncAdmission:
    """Bound large request bodies and live streams without a waiting thread."""
    def __init__(self,name,capacity,queue=0,timeout=1.):
        self.name=name;self.capacity=capacity;self.queue_limit=queue;self.timeout=timeout
        self.lock=threading.RLock();self.active=0;self.waiting=deque();self.rejected=0
    def _pump(self):
        while self.waiting and self.active<self.capacity:
            permit=self.waiting.popleft()
            if permit.released:continue
            permit.granted=True;self.active+=1
            def grant(permit=permit):
                if not permit.future.done():permit.future.set_result(permit)
            try:permit.loop.call_soon_threadsafe(grant)
            except RuntimeError:self.release(permit)
    async def acquire(self):
        permit=Permit(self,asyncio.get_running_loop())
        with self.lock:
            if self.active>=self.capacity and len(self.waiting)>=self.queue_limit:
                self.rejected+=1;raise APICapacityError(self.name,'queue_full')
            self.waiting.append(permit);self._pump()
        try:return await asyncio.wait_for(asyncio.shield(permit.future),self.timeout)
        except asyncio.TimeoutError:
            with self.lock:self.rejected+=1
            permit.release();permit.future.cancel();raise APICapacityError(self.name,'queue_timeout') from None
        except asyncio.CancelledError:
            permit.release();permit.future.cancel();raise
    def release(self,permit):
        with self.lock:
            if permit.released:return
            permit.released=True
            if permit.granted:self.active-=1
            else:
                try:self.waiting.remove(permit)
                except ValueError:pass
            self._pump()
    def snapshot(self):
        with self.lock:return dict(active=self.active,queued=len(self.waiting),capacity=self.capacity,rejected=self.rejected)


@dataclass(eq=False)
class Work:
    loop:object
    started:object
    result:object
    context:object
    function:object
    prior_work:bool=False
    created:float=field(default_factory=time.monotonic)
    started_at:float|None=None
    abandoned:object=field(default_factory=threading.Event)
    interrupt_lock:object=field(default_factory=threading.Lock)
    interrupts:list=field(default_factory=list)


class BoundedExecutor:
    def __init__(self,name,workers,queue,queue_timeout=1.,execution_timeout=30.):
        if workers<1 or queue<0 or queue_timeout<=0 or execution_timeout<=0:raise ValueError('Invalid API pool limits')
        self.name=name;self.workers=workers;self.queue_limit=queue
        self.queue_timeout=queue_timeout;self.execution_timeout=execution_timeout
        self.executor=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='api-'+name)
        self.lock=threading.RLock();self.waiting=deque();self.running=set();self.active=0;self.closed=False
        self.metrics={k:0 for k in ('submitted','completed','failed','rejected','queue_timeout','execution_timeout','cancelled')}
        self.wait_seconds=0.;self.run_seconds=0.
        self.wait_max_seconds=0.;self.run_max_seconds=0.

    def snapshot(self):
        with self.lock:
            return dict(pool=self.name,workers=self.workers,queue_capacity=self.queue_limit,
                        active=self.active,queued=len(self.waiting),closed=self.closed,
                        wait_seconds=self.wait_seconds,run_seconds=self.run_seconds,
                        wait_max_seconds=self.wait_max_seconds,run_max_seconds=self.run_max_seconds,
                        oldest_active_seconds=max((time.monotonic()-w.started_at for w in self.running),default=0.),
                        oldest_queued_seconds=max((time.monotonic()-w.created for w in self.waiting),default=0.),**self.metrics)

    @staticmethod
    def deliver(work,future,value=None,error=None):
        def settle():
            if future.done():return
            if error is not None:future.set_exception(error)
            else:future.set_result(value)
        try:work.loop.call_soon_threadsafe(settle)
        except RuntimeError:pass  # An exited client loop cannot receive a late result.

    def _execute(self,work):
        def invoke():
            token=CURRENT_WORK.set(work)
            try:
                check_cancelled()
                return work.function()
            finally:CURRENT_WORK.reset(token)
        return work.context.run(invoke)

    def _pump(self):
        # Called with the short bookkeeping lock, never around user work.
        while self.waiting and self.active<self.workers and not self.closed:
            work=self.waiting.popleft()
            if work.abandoned.is_set():continue
            work.started_at=time.monotonic();wait=work.started_at-work.created
            self.wait_seconds+=wait;self.wait_max_seconds=max(self.wait_max_seconds,wait)
            self.active+=1;self.running.add(work)
            self.deliver(work,work.started,True)
            try:
                future=self.executor.submit(self._execute,work)
                future.add_done_callback(functools.partial(self._done,work))
            except Exception as exc:
                self.active-=1;self.running.discard(work);self.metrics['failed']+=1
                self.deliver(work,work.result,error=exc)

    def _done(self,work,future):
        value=None;error=None
        try:value=future.result()
        except BaseException as exc:error=exc
        with self.lock:
            self.active-=1;self.running.discard(work);self.metrics['completed']+=1
            elapsed=time.monotonic()-work.started_at;self.run_seconds+=elapsed
            self.run_max_seconds=max(self.run_max_seconds,elapsed)
            if error is not None and not isinstance(error,APIWorkCancelled):self.metrics['failed']+=1
            self._pump()
        self.deliver(work,work.result,value,error)

    def _abandon(self,work):
        with self.lock:
            first=not work.abandoned.is_set()
            work.abandoned.set()
            if first and work.started_at is None:
                try:self.waiting.remove(work)
                except ValueError:pass
                waited=time.monotonic()-work.created;self.wait_seconds+=waited
                self.wait_max_seconds=max(self.wait_max_seconds,waited)
            # Running work remains in active until _done, even after a deadline.
        with work.interrupt_lock:callbacks=list(work.interrupts);work.interrupts.clear()
        for callback in callbacks:
            try:callback()
            except Exception:pass  # A closed local handle is already interrupted.
        work.started.cancel();work.result.cancel()
        return first

    async def run(self,function,*args,**kwargs):
        loop=asyncio.get_running_loop()
        progress=REQUEST_WORK.get();prior_work=bool(progress and progress['submitted'])
        work=Work(loop,loop.create_future(),loop.create_future(),contextvars.copy_context(),functools.partial(function,*args,**kwargs),prior_work=prior_work)
        with self.lock:
            if self.closed or (self.active>=self.workers and len(self.waiting)>=self.queue_limit):
                self.metrics['rejected']+=1
                raise APICapacityError(self.name,'closed' if self.closed else 'queue_full',prior_work=prior_work)
            self.metrics['submitted']+=1;self.waiting.append(work);self._pump()
            if progress is not None:progress['submitted']+=1
        deadline='queue_timeout'
        timer=None
        try:
            async with asyncio.timeout(self.queue_timeout) as timer:await asyncio.shield(work.started)
            deadline='execution_timeout'
            remaining=max(.001,work.started_at+self.execution_timeout-time.monotonic())
            async with asyncio.timeout(remaining) as timer:return await asyncio.shield(work.result)
        except asyncio.TimeoutError:
            if timer is None or not timer.expired():raise  # The function itself can raise TimeoutError.
            started=work.started_at is not None
            first=self._abandon(work)
            if first:
                with self.lock:self.metrics[deadline]+=1
            raise APICapacityError(self.name,deadline,started,prior_work) from None
        except asyncio.CancelledError:
            if self._abandon(work):
                with self.lock:self.metrics['cancelled']+=1
            raise

    def close(self):
        with self.lock:
            self.closed=True;waiting=list(self.waiting);self.waiting.clear()
            self.metrics['cancelled']+=len(waiting)
            for work in waiting:
                work.abandoned.set()
                waited=time.monotonic()-work.created;self.wait_seconds+=waited;self.wait_max_seconds=max(self.wait_max_seconds,waited)
                self.deliver(work,work.started,error=APICapacityError(self.name,'closed',prior_work=work.prior_work))
        self.executor.shutdown(wait=False,cancel_futures=False)


# Database work and critical controls have separate capacities. Media workers
# match the existing decoder slots; this is isolation, not extra decoder load.
DEFAULTS={
    'read':(6,12,1.,15.),'write':(4,8,1.,30.),'critical':(2,8,1.,15.),
    'storage':(4,8,1.,120.),'media':(2,4,1.,100.),
    'diagnostics':(2,4,1.,15.),'control':(2,4,1.,45.),
    'cleanup':(1,8,2.,120.),'auth':(2,4,.5,5.),'stream_read':(2,8,1.,15.),'health':(2,4,.5,5.),
}


class APIExecutors:
    def __init__(self):
        self.pools={}
        streams=int(os.environ.get('VIDEO_USE_API_STREAM_LIMIT','64'))
        if not 1<=streams<=1024:raise ValueError('Invalid API stream limit')
        self.streams=AsyncAdmission('stream',streams)
        self.admissions={name:AsyncAdmission(name,capacity,queue) for name,capacity,queue in
                         (('upload',2,4),('image_import',2,4),('demo',1,2),('delete',2,4))}
        self.request_seconds={name:float(os.environ.get('VIDEO_USE_API_'+name.upper()+'_SECONDS','600' if name!='delete' else '30')) for name in self.admissions}
        if any(not .01<=value<=3600 for value in self.request_seconds.values()):raise ValueError('Invalid API request deadline')
        for name,defaults in DEFAULTS.items():
            prefix='VIDEO_USE_API_'+name.upper()+'_'
            workers=int(os.environ.get(prefix+'WORKERS',defaults[0]))
            queued=int(os.environ.get(prefix+'QUEUE',defaults[1]))
            wait=float(os.environ.get(prefix+'QUEUE_SECONDS',defaults[2]))
            run=float(os.environ.get(prefix+'RUN_SECONDS',defaults[3]))
            if not 1<=workers<=64 or not 0<=queued<=256 or not 0<wait<=30 or not 0<run<=600:
                raise ValueError('Invalid API capacity configuration: '+name)
            if name=='media' and workers>2:raise ValueError('API media workers cannot exceed decoder capacity')
            self.pools[name]=BoundedExecutor(name,workers,queued,wait,run)
        if sum(p.workers for p in self.pools.values())>128:raise ValueError('API thread capacities exceed the process budget')

    async def run(self,pool,function,*args,**kwargs):return await self.pools[pool].run(function,*args,**kwargs)
    def snapshot(self):return {name:pool.snapshot() for name,pool in self.pools.items()}
    def close(self):
        for pool in self.pools.values():pool.close()


def lane(name):
    if name not in DEFAULTS:raise ValueError('Unknown API pool')
    def decorate(function):function.api_pool=name;return function
    return decorate


def admission(name):
    def decorate(function):function.api_admission=name;return function
    return decorate


def route_class(executors):
    from fastapi.routing import APIRoute
    class IsolatedRoute(APIRoute):
        def __init__(self,path,endpoint,**kwargs):
            original=endpoint;is_async=inspect.iscoroutinefunction(original)
            pool=getattr(original,'api_pool',None) or ('read' if set(kwargs.get('methods') or ['GET'])<={'GET','HEAD','OPTIONS'} else 'write')
            @functools.wraps(original)
            async def endpoint(*args,**values):
                async def run():
                    if is_async:return await original(*args,**values)
                    def invoke():
                        result=original(*args,**values)
                        from fastapi.encoders import jsonable_encoder
                        from starlette.responses import JSONResponse
                        response_type=getattr(self.response_class,'value',self.response_class)
                        if (self.response_field is None and not self.dependant.response_param_name
                            and isinstance(result,(dict,list)) and isinstance(response_type,type) and issubclass(response_type,JSONResponse)):
                            return response_type(jsonable_encoder(result),status_code=self.status_code or 200)
                        return result
                    return await executors.run(pool,invoke)
                from starlette.responses import StreamingResponse
                response_type=getattr(self.response_class,'value',self.response_class)
                # Streaming responses own receive/disconnect for their entire
                # body; do not consume their channel during response setup.
                if isinstance(response_type,type) and issubclass(response_type,StreamingResponse):return await run()
                return await while_connected(values.get(self.dependant.request_param_name),run)
            # Forward-reference annotations belong to the original module.
            endpoint.__signature__=inspect.signature(original,eval_str=True)
            super().__init__(path,endpoint=endpoint,**kwargs)
        def get_route_handler(self):
            from fastapi.params import File
            handle=super().get_route_handler()
            name=getattr(self.endpoint,'api_admission',None)
            file_upload=bool(self.body_field and isinstance(self.body_field.field_info,File))
            async def bounded_handle(request):
                # Only the registered multipart upload route needs the large
                # streaming limit. Count bytes before JSON parsing, including
                # chunked requests and bodies with a false Content-Length.
                if not file_upload:
                    from fastapi import HTTPException
                    limit=1024*1024
                    length=request.headers.get('content-length')
                    if length is not None:
                        try:size=int(length)
                        except ValueError:raise HTTPException(400,'请求体长度无效。') from None
                        if size<0:raise HTTPException(400,'请求体长度无效。')
                        if size>limit:raise HTTPException(413,'请求体超过 1 MiB 限制。')
                    body=bytearray()
                    async for chunk in request.stream():
                        if len(body)+len(chunk)>limit:raise HTTPException(413,'请求体超过 1 MiB 限制。')
                        body.extend(chunk)
                    request._body=bytes(body)
                return await handle(request)
            async def admitted(request):
                token=REQUEST_WORK.set({'submitted':0});permit=None
                try:
                    if name is None:return await bounded_handle(request)
                    permit=await executors.admissions[name].acquire()
                    timer=None
                    try:
                        async with asyncio.timeout(executors.request_seconds[name]) as timer:return await bounded_handle(request)
                    except TimeoutError:
                        if timer is None or not timer.expired():raise
                        progress=REQUEST_WORK.get()
                        raise APICapacityError(name,'request_timeout',prior_work=bool(progress and progress['submitted'])) from None
                finally:
                    if permit:permit.release()
                    REQUEST_WORK.reset(token)
            return admitted
    return IsolatedRoute
