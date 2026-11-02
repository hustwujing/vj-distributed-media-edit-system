"""Loopback-only disposable pressure fixture. Never imported by production."""
import asyncio
import os
from pathlib import Path
import time


def create():
    from webapp.app import create_app
    import webapp.app as routes
    import webapp.agent_api as agents
    import webapp.diagnostics_api as diagnostics
    root=Path(os.environ['VU_PRESSURE_ROOT']);signals=Path(os.environ['VU_PRESSURE_SIGNALS'])
    app=create_app(root,False)
    def delay(name):
        stop=time.monotonic()+12
        while (signals/name).exists() and time.monotonic()<stop:time.sleep(.01)
    def wrap(target,name):
        def call(*args,**kwargs):delay(name);return target(*args,**kwargs)
        return call
    app.state.store.file=wrap(app.state.store.file,'storage')
    routes.thumbnail=wrap(routes.thumbnail,'media')
    routes.inspect_image=wrap(routes.inspect_image,'media')
    diagnostics.diagnose=wrap(diagnostics.diagnose,'diagnostics')
    original_query=app.state.store.query
    def query(sql,*args,**kwargs):
        if sql.startswith('SELECT p.id,p.name,p.created,COALESCE'):delay('read')
        return original_query(sql,*args,**kwargs)
    app.state.store.query=query
    def classify(*args):delay('control');return 'status'
    app.state.control_intents.resolve=classify
    original_remove=routes.shutil.rmtree
    def remove(path,*args,**kwargs):
        if Path(path).parent==root/'projects':delay('cleanup')
        return original_remove(path,*args,**kwargs)
    routes.shutil.rmtree=remove
    @app.get('/__test/capacity')
    async def capacity():
        import anyio
        limiter=anyio.to_thread.current_default_thread_limiter()
        return {'pools':app.state.api_execution.snapshot(),'default_threads':limiter.borrowed_tokens,'instance':os.environ['VIDEO_USE_INSTANCE']}
    return app


if __name__=='__main__':
    import uvicorn
    uvicorn.run(create(),host='127.0.0.1',port=int(os.environ['VU_PRESSURE_PORT']),access_log=False,log_level='warning')
