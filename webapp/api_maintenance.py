"""API capacity sampling and bounded cleanup, independent of request workers."""
import asyncio
import time
from .api_capacity import POOL_FIELDS,GATE_FIELDS


async def metrics(executors,interval=5.):
    import anyio
    from .telemetry import emit
    expected=time.monotonic()
    while True:
        now=time.monotonic();lag=max(0.,now-expected)
        limiter=anyio.to_thread.current_default_thread_limiter()
        emit('api.loop.snapshot',service='api',api_loop_lag_seconds=lag,
             api_loop_default_threads_used=limiter.borrowed_tokens,api_loop_default_threads_limit=limiter.total_tokens)
        for name,value in executors.snapshot().items():
            emit('api.pool.snapshot',service='api',operation=name,**{'api_pool_'+k:value[k] for k in POOL_FIELDS})
        for name,gate in {**executors.admissions,'stream':executors.streams}.items():
            value=gate.snapshot();emit('api.gate.snapshot',service='api',operation=name,**{'api_gate_'+k:value[k] for k in GATE_FIELDS})
        expected=time.monotonic()+interval
        await asyncio.sleep(interval)


def old_project_caches(root,older_than=300,state=None):
    """A grace period excludes directories from unfinished project creation."""
    from .api_staging import old_directories
    state=state if state is not None else {};result=set()
    for group in ('projects','cache/objects'):
        names,cursor=old_directories(root/group,older_than,state.get(group,''))
        state[group]=cursor;result.update(names)
    return sorted(result)


async def sweep_once(store,executors,state=None):
    from .api_staging import sweep
    from .telemetry import emit
    import shutil
    state=state if state is not None else {}
    removed=await executors.run('cleanup',sweep,store.root,older_than=max(3600,max(executors.request_seconds.values())*2),state=state)
    candidates=await executors.run('cleanup',old_project_caches,store.root,state=state)
    if candidates:
        def missing():
            rows=[]
            for start in range(0,len(candidates),200):
                group=candidates[start:start+200]
                rows+=store.query('SELECT id FROM projects WHERE id IN ('+','.join('?' for _ in group)+')',tuple(group))
            present={r['id'] for r in rows};return [name for name in candidates if name not in present]
        abandoned=await executors.run('read',missing)
        def remove():
            for project in abandoned:
                for path in (store.project_dir(project),store.root/'cache/objects'/project):
                    try:shutil.rmtree(path)
                    except FileNotFoundError:pass
        if abandoned:await executors.run('cleanup',remove)
        removed+=len(abandoned)
    if removed:emit('api.cache_cleanup',calls=removed)
    return removed


async def cleanup(store,executors,interval=60.):
    from .telemetry import emit
    state={}
    while True:
        try:await sweep_once(store,executors,state)
        except asyncio.CancelledError:raise
        except Exception as exc:emit('api.cleanup_deferred','WARNING',reason='maintenance',exception_type=type(exc).__name__)
        await asyncio.sleep(interval)
