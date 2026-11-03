"""Owner-scoped, read-only usage endpoints for agent and manual tasks."""
from typing import Literal
from fastapi import HTTPException, Query, Request
from .task_usage import snapshot


def register(app,store,own_project):
    @app.get('/api/projects/{project_id}/runs/{task_id}/usage')
    @app.get('/api/projects/{project_id}/tasks/{task_id}/usage')
    def usage(project_id:str,task_id:str,request:Request,scope:Literal['task','run','project']='task',
              after:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=200),provider_after:int=Query(default=0,ge=0)):
        own_project(project_id,request)
        rows=store.query('SELECT project_id FROM usage_tasks WHERE task_id=?',(task_id,))
        if not rows or rows[0]['project_id']!=project_id:raise HTTPException(404,'任务不存在')
        return snapshot(store,task_id,scope,after,limit,provider_after)
