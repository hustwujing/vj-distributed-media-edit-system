"""Explicit ancestor context for planning; never restores execution authority."""
import json


def snapshot(store,project,task):
    from . import workflows
    result={'read_only':True,'execution_authorized':False,'parent_task_id':None,'workflow':None}
    if not store.query("SELECT name FROM sqlite_master WHERE type='table' AND name='usage_tasks'"):return result
    current=task;seen=set()
    for _ in range(64):
        if current in seen:break
        seen.add(current)
        rows=store.query('SELECT parent_task_id FROM usage_tasks WHERE task_id=? AND project_id=?',(current,project))
        parent=rows[0]['parent_task_id'] if rows else None
        if not parent:break
        if not store.query('SELECT id FROM agent_turns WHERE id=? AND project=?',(parent,project)):break
        if result['parent_task_id'] is None:result['parent_task_id']=parent
        workflow=workflows.get(store,parent)
        if workflow:
            receipts={(r['step'],r['revision']) for r in store.query(
                'SELECT step,revision FROM workflow_reuse_receipts WHERE workflow=? AND project=?',(workflow['id'],project))}
            result['source_task_id']=parent
            result['workflow']={'id':workflow['id'],'status':workflow['status'],'error':workflow['error'],
                'steps':[{'id':s['id'],'revision':s['revision'],'status':s['status'],'goal':s['spec']['goal'],
                    'acceptance':s['spec']['acceptance'],'spec':s['spec'],
                    'reuse_from':{'workflow':workflow['id'],'step':s['id'],'revision':s['revision']} if s['status']=='succeeded' and (s['id'],s['revision']) in receipts else None,
                    'files':s['result'],'candidate_files':s['candidate'],
                    'output_roles':s['quality'].get('output_roles',{}),'checks':s['quality'].get('checks',[])} for s in workflow['steps']]}
            result['decisions']=[{**r,'answer':json.loads(r['answer'])} for r in store.query(
                'SELECT kind,question,revision,status,answer FROM workflow_user_requests WHERE workflow=? ORDER BY created',(workflow['id'],))]
            return result
        current=parent
    return result
