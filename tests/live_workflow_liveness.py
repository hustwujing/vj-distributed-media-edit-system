"""Real-cluster workflow liveness fault injection; no model or scheduler mocks."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import time
import uuid

import httpx

from webapp import workflow_graph as graph,workflow_planning as planning
from webapp.paths import data_root
from webapp.store import Store
from webapp.workflow_orchestrator import Orchestrator
from webapp.workflow_api import public


UNIT='sandbox-agent-orchestrator.service'


def wait(label,probe,timeout=180):
    deadline=time.time()+timeout;last=None
    while time.time()<deadline:
        last=probe()
        if last:return last
        time.sleep(.5)
    raise TimeoutError(f'{label} timed out; last={last!r}')


def script(identifier,*,delivery=False,inputs=None):
    name='final.txt' if delivery else 'upstream.txt'
    return {'id':identifier,'type':'script','name':'QA '+identifier,
        'objective':'Write deterministic QA output','argv':['python','-c',f"from pathlib import Path;Path('{name}').write_text('qa')"],
        'inputs':inputs or {},'outputs':[name],'output_roles':{'report':name},
        'acceptance':[{'kind':'file','file':name,'stage':'final' if delivery else 'current'}],
        'basic_reason':'Dedicated liveness fault injection','retry_policy':'safe_once','delivery_stage':delivery}


def agent(identifier):
    return {'id':identifier,'type':'agent','profile':'editing_implementer','name':'QA '+identifier,
        'objective':'Write upstream.txt as a deterministic liveness marker','inputs':{},
        'outputs':['upstream.txt'],'output_roles':{'report':'upstream.txt'},
        'acceptance':[{'kind':'file','file':'upstream.txt','stage':'current'}],
        'basic_reason':'Dedicated liveness fault injection','retry_policy':'never','on_failure':'diagnose',
        'delivery_stage':False,'runtime_requirements':{'requires_gpu':False,'python_modules':[],'node_modules':[],'system_tools':[],'network_urls':[]}}


def create_run(store,project,label):
    authorization={'goal':'QA workflow liveness '+label,'allowed_assets':[],'allowed_operations':['script'],
        'allowed_agent_profiles':['editing_implementer','media_failure_diagnostician'],
        'delivery_targets':['final.txt'],
        'require_plan_confirmation':False,'content_fingerprint':graph.content_fingerprint(store,project)}
    run=graph.create(store,project,None,authorization,session_id=uuid.uuid4().hex,record_message=False)
    frozen=planning.snapshot(store,run)
    if frozen is None:raise RuntimeError('Real worker catalogue unavailable before liveness case')
    request_id='seed_'+uuid.uuid4().hex
    proposal={'proposal_id':request_id,'base_graph_version':0,'reason':'Seed isolated liveness dependency',
        'add_tasks':[agent('upstream'),script('downstream',delivery=True,inputs={'source':{'step':'upstream','role':'report'}})],
        'add_dependencies':[{'upstream':'upstream','downstream':'downstream'}],
        'replan_after':['downstream'],'sealed':False}
    result={'kind':'APPLY_PLAN','reason':proposal['reason'],'proposal':proposal,
        'snapshot_digest':graph.digest(frozen),'base_graph_version':0,
        'authorization_version':run['authorization_version'],'input_through_sequence':0}
    request={'id':request_id,'task_id':request_id,'status':'staged','authorization_version':run['authorization_version'],
        'snapshot_digest':graph.digest(frozen),'snapshot_json':graph.encode(frozen),
        'trigger_json':'{}','proposal_json':graph.encode(result)}
    planning.apply(store,run,request)
    return graph.get(store,run['id'])


def inject(store,run,*,repair):
    store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed',error_detail='QA injected review rejection',lease_id='' WHERE run_id=? AND id='upstream'",(run['id'],))
    store.execute("UPDATE run_tasks SET status='blocked',lease_id='' WHERE run_id=? AND id='downstream'",(run['id'],))
    store.execute("UPDATE runs SET sealed=0,status='ACTIVE',terminal_reason='',next_reconcile_at=? WHERE id=?",(store.now(),run['id']))
    if repair:Orchestrator(store,'qa-injector').open_repair(graph.get(store,run['id']),'upstream','task_review','qa-review','quality_failed')


def cancel_and_wait(store,identifier):
    graph.command(store,identifier,'qa:liveness','cancel',request_id=uuid.uuid4().hex)
    store.execute('UPDATE runs SET next_reconcile_at=? WHERE id=?',(store.now(),identifier))
    return wait('cancel '+identifier,lambda:(row if (row:=graph.get(store,identifier))['status']=='CANCELLED' else None),60)


def force_cleanup_project(store,project):
    rows=store.query('SELECT name FROM projects WHERE id=?',(project,))
    if not rows or not rows[0]['name'].startswith('QA workflow liveness '):raise RuntimeError('Refusing to purge a non-QA project')
    if store.query("SELECT id FROM runs WHERE project_id=? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')",(project,)):raise RuntimeError('QA project still has a live Run')
    run_ids=[row['id'] for row in store.query('SELECT id FROM runs WHERE project_id=?',(project,))]
    if run_ids:
        placeholders=','.join('?' for _ in run_ids);args=tuple(run_ids)
        if store.query(f"SELECT attempt FROM execution_leases WHERE run_id IN ({placeholders}) AND active=1",args):raise RuntimeError('QA project still has an active lease')
        if store.query(f"SELECT attempt FROM execution_cleanup WHERE task_id IN ({placeholders}) AND state<>'complete'",args):raise RuntimeError('QA project cleanup is incomplete')
        for table in ('execution_limits','execution_time_slices','execution_cleanup','execution_security_events','execution_external_cleanup','execution_waits','execution_cancel_requests','execution_phase_metrics'):
            store.execute(f'DELETE FROM {table} WHERE task_id IN ({placeholders})',args)
        for table in ('execution_leases','execution_attempts','execution_budgets','execution_provider_calls'):
            store.execute(f'DELETE FROM {table} WHERE run_id IN ({placeholders})',args)
        for table in ('native_task_sessions','specialist_attempt_profiles','run_authorizations','run_inputs','run_input_drain','run_task_replacements','workflow_task_revisions','workflow_plan_tasks','workflow_reconcile_failures','control_jobs','workflow_dependencies','run_tasks','workflow_plan_drafts','planner_runs','task_results','workflow_commands','workflow_decisions','workflow_reviews','workflow_graph_events','workflow_model_reservations','workflow_inspections'):
            store.execute(f'DELETE FROM {table} WHERE run_id IN ({placeholders})',args)
        for table,column in (('workflow_agent_outputs','run_id'),('workflow_provider_calls','workflow'),('workflow_diagnostics','workflow'),('agent_run_profiles','task_id'),('agent_run_role_profiles','task_id'),('usage_calls','task_id')):
            store.execute(f'DELETE FROM {table} WHERE {column} IN ({placeholders})',args)
    store.execute('DELETE FROM usage_tasks WHERE project_id=?',(project,))
    store.execute('DELETE FROM session_messages WHERE session_id IN (SELECT id FROM sessions WHERE project_id=?)',(project,))
    store.execute('DELETE FROM sessions WHERE project_id=?',(project,));store.execute('DELETE FROM runs WHERE project_id=?',(project,))
    store.execute('DELETE FROM projects WHERE id=?',(project,));shutil.rmtree(store.project_dir(project),ignore_errors=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path)
    parser.add_argument('--base',default='http://127.0.0.1/distributed-agent-sandbox')
    parser.add_argument('--cleanup-project')
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Live liveness case requires root for bounded Orchestrator fault setup')
    release=json.loads((Path(__file__).resolve().parents[1]/'.sandbox-cluster/release.json').read_text())
    if release.get('namespace')!='sandbox-agent' or release.get('state')!='ready':raise RuntimeError('Refusing a non-dedicated or incomplete deployment')
    store=Store(data_root())
    if args.cleanup_project:
        force_cleanup_project(store,args.cleanup_project);print(json.dumps({'cleaned':args.cleanup_project}));return
    if args.output is None:parser.error('--output is required unless --cleanup-project is used')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    report={'started_at':time.time(),'release':release,'passed':False}
    if store.query("SELECT attempt FROM execution_leases WHERE active=1 AND expires>?",(store.now(),)):raise RuntimeError('Live liveness case requires zero active leases')
    if store.query("SELECT id FROM runs WHERE status IN ('ACTIVE','CANCELLING')"):raise RuntimeError('Live liveness case requires no running workflows')
    client=httpx.Client(base_url=args.base,timeout=30,trust_env=False);project=None;runs=[];stopped=False
    try:
        response=client.post('/api/projects',json={'name':'QA workflow liveness '+uuid.uuid4().hex[:8]});response.raise_for_status();project=response.json()['id']
        subprocess.run(['systemctl','stop',UNIT],check=True,timeout=30);stopped=True
        repair=create_run(store,project,'repair');runs.append(repair['id'])
        stalled=create_run(store,project,'guard');runs.append(stalled['id'])
        inject(store,repair,repair=True);inject(store,stalled,repair=False)
        subprocess.run(['systemctl','start',UNIT],check=True,timeout=30);stopped=False
        planner=wait('repair planner',lambda:next((row for row in store.query("SELECT id,status,attempts,trigger_json FROM planner_runs WHERE run_id=? ORDER BY created_at DESC",(repair['id'],)) if json.loads(row['trigger_json']).get('repair_resume')),None),30)
        attempted=wait('real planner lease',lambda:next((row for row in store.query("SELECT id,status,attempts,error_code FROM planner_runs WHERE run_id=? ORDER BY created_at DESC",(repair['id'],)) if row['id']==planner['id'] and row['attempts']>0),None),90)
        finished=wait('bounded planner result',lambda:next((row for row in store.query("SELECT id,status,attempts,error_code FROM planner_runs WHERE id=?",(planner['id'],)) if row['status'] in ('applied','awaiting_confirmation','rejected')),None),240)
        if finished['status'] not in ('applied','awaiting_confirmation'):raise AssertionError('Repair Planner did not produce an executable result: '+json.dumps(finished))
        stalled_run=wait('liveness guard',lambda:(row if (row:=graph.get(store,stalled['id']))['status']=='WAITING_USER' and row['terminal_reason']=='workflow_stalled' else None),30)
        shown=public(store,stalled_run).get('blocking_action') or {}
        if shown.get('code')!='workflow_stalled' or '无需猜测补充信息' not in shown.get('message',''):raise AssertionError(shown)
        report.update(repair_run=repair['id'],stalled_run=stalled['id'],planner=planner,attempted=attempted,
            planner_result=finished,stalled_action=shown,
            checks=['repair_resume_created','real_planner_attempted','planner_bounded_result','unknown_stall_visible'])
        report['passed']=True
    finally:
        if stopped:subprocess.run(['systemctl','start',UNIT],check=False,timeout=30)
        for identifier in runs:
            try:cancel_and_wait(store,identifier)
            except Exception as exc:report.setdefault('cleanup_errors',[]).append(type(exc).__name__+': '+str(exc))
        if runs:
            deadline=time.time()+60
            while time.time()<deadline:
                leases=store.query("SELECT attempt FROM execution_leases WHERE run_id IN (?,?) AND active=1",tuple(runs))
                cleanup=store.query("SELECT attempt FROM execution_cleanup WHERE task_id IN (?,?) AND state<>'complete'",tuple(runs))
                if not leases and not cleanup:break
                time.sleep(.5)
            report['active_leases_after']=len(leases);report['pending_cleanup_after']=len(cleanup)
            if leases or cleanup:report['passed']=False
        if project:
            response=client.delete('/api/projects/'+project);report['delete_status']=response.status_code
            if response.status_code not in (200,204):
                try:force_cleanup_project(store,project);report['forced_cleanup']=True;report['delete_status']=204
                except Exception as exc:report.setdefault('cleanup_errors',[]).append(type(exc).__name__+': '+str(exc));report['passed']=False
        report['finished_at']=time.time();args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        client.close()
    if not report['passed']:raise RuntimeError('Live workflow liveness case failed: '+json.dumps(report,ensure_ascii=False))
    print(json.dumps({'passed':True,'planner_status':report['planner_result']['status'],'delete_status':report['delete_status']}))


if __name__=='__main__':main()
