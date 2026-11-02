"""Owner-scoped diagnosis; inability to verify ownership fails closed."""
from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse
from .diagnostics import diagnose,resolve_request
from .api_execution import lane


def install(app,store,own_project):
    @app.get('/api/projects/{project_id}/diagnostics/{task_id}')
    @lane('diagnostics')
    def task_diagnosis(project_id:str,task_id:str,request:Request,after:int=0,limit:int=100,download:bool=False):
        own_project(project_id,request)
        rows=store.query('SELECT id FROM agent_turns WHERE project=? AND id=? UNION ALL SELECT id FROM jobs WHERE project=? AND id=?',(project_id,task_id,project_id,task_id))
        if not rows:raise HTTPException(404,'任务不存在或不属于此项目。')
        from .store import Store
        diagnostic_store=Store(store.root,timeout=1,initialize_schema=False,dsn=store.dsn)
        report=diagnose(diagnostic_store,task_id,after,limit)
        # Operator-level topology is only available through the protected CLI.
        for attempt in report['attempts']:
            for key in ('node','worker'):attempt.pop(key,None)
        for item in report['local_cleanup']:item.pop('node',None)
        for item in report['logs']:
            for key in ('node','worker','instance','stack'):item.pop(key,None)
        for collection in ('provider_calls','external_cleanup'):
            for row in report[collection]:
                if '://' in row.get('provider',''):row['provider']='configured_provider'
        report['facts']['current_attempts']=[a for a in report['attempts'] if a['status']=='running']
        return JSONResponse(report,headers={'Content-Disposition':'attachment; filename="diagnosis-'+task_id+'.json"'} if download else {})

    @app.get('/api/projects/{project_id}/diagnostic-requests/{request_id}')
    @lane('diagnostics')
    def request_diagnosis(project_id:str,request_id:str,request:Request):
        own_project(project_id,request)
        try:ids=resolve_request(store,request_id)
        except ValueError:raise HTTPException(400,'无效查询编号。')
        allowed=[]
        for task in ids:
            if store.query('SELECT id FROM agent_turns WHERE project=? AND id=? UNION ALL SELECT id FROM jobs WHERE project=? AND id=?',(project_id,task,project_id,task)):allowed.append(task)
        if not allowed:raise HTTPException(404,'此项目没有可关联的任务。')
        return {'task_ids':allowed}
