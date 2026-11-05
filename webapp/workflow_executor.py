"""One leased execution phase. Returns durable candidates, never business success."""
import json
import uuid
from . import workflow_graph as graph
from .workflow_dispatch import stage_result
from .task_leases import LeaseLost,TaskQuotaReached
from .task_lifecycle import TaskLimitReached


def quota_error_code(task,exc):
    category=exc.diagnostic.get('category')
    if category=='model_tokens':return 'model_token_budget_exhausted'
    if task.get('control_kind')=='REVIEW' and category in ('agent_turns','agent_seconds','agent_finalize'):
        return 'review_contract_exhausted'
    return 'budget_exhausted'


def failure_error_code(task,phase,calls,classification):
    if classification in ('pre_submit','model_result','result_unavailable'):return classification
    if task['type']=='agent':return 'agent_failed'
    if phase in ('submit','sync') and calls:
        return 'provider_failed' if calls[0]['state'] in ('failed','rejected') else 'submission_unknown'
    if phase=='poll':return 'poll_failed'
    if phase=='collect':return 'collect_failed'
    if phase=='validate':return 'technical_validation_failed'
    return 'execution_failed'


def bind_output_roles(step,files):
    from .workflow_inputs import roles
    role_map=roles(step['spec'],files,getattr(files,'roles',None))
    if not getattr(files,'roles',None):
        from .operation_result import ProducedFiles
        files=ProducedFiles(files,role_map)
    return files,role_map


def validate_candidate(adapter,workflow,step,files):
    """Model-authored acceptance cannot remove minimum media integrity checks."""
    from pathlib import Path
    report=adapter.validate(workflow,step,files)
    media={'.mp4','.mov','.webm','.mkv','.mp3','.wav','.m4a','.flac','.ogg','.aac','.opus'}
    if any(Path(name).suffix.lower() in media for name in files):
        from .quality_policy import Acceptance
        basic={**step,'spec':{**step['spec'],'kind':'script','acceptance':[Acceptance(kind='media',decode=True).model_dump()]}}
        checked=adapter.validate(workflow,basic,files)
        report['technical_valid']=bool(report['technical_valid'] and checked['technical_valid'] and checked['passed'])
        report['passed']=bool(report['passed'] and checked['passed'])
        report['checks'] += [{**check,'origin':'minimum_media_integrity'} for check in checked['checks']]
    return report


def record_phase(lease,task,name,files=None):
    if name not in ('local','sync','submit','poll','collect','validate'):raise ValueError('Unknown Task lifecycle phase')
    with lease.bind(),lease.store.connect() as db:
        lease.validate(db)
        if not db.execute(f'UPDATE {graph.work_table(task)} SET phase=?,updated_at=? WHERE run_id=? AND id=? AND revision=? AND lease_id=? AND lease_generation=?',
            (name,lease.store.now(),lease.run_id,task['id'],task['revision'],lease.attempt,lease.generation)).rowcount:
            raise LeaseLost('Lifecycle phase belongs to a replaced attempt')
        if files is not None:
            db.execute(f'UPDATE {graph.work_table(task)} SET candidate_result_json=? WHERE run_id=? AND id=?',
                (graph.encode(list(files)),lease.run_id,task['id']))


def execute(host,lease):
    from .task_usage import execution_call
    run=graph.get(host.store,lease.run_id);task=next(t for t in graph.work_items(run) if t['id']==lease.step_id)
    operation=task['spec'].get('operation') or task['spec'].get('profile') or 'script'
    with execution_call(lease,'control' if task.get('control_kind') else 'workflow',operation,revision=task['revision'],phase=task['phase'],actor='manager_agent' if task.get('control_kind')=='PLAN' else 'reviewer_agent' if task.get('control_kind') else 'specialist_agent' if task['type']=='agent' else 'workflow_sandbox',label='执行控制任务' if task.get('control_kind') else '执行业务任务阶段') as receipt:
        result=_execute(host,lease)
        rows=host.store.query('SELECT outcome FROM task_results WHERE lease_id=?',(lease.attempt,))
        receipt['status']='failed' if rows and rows[0]['outcome']=='failed' else 'succeeded'
        return result


def _execute(host,lease):
    store=lease.store;run=graph.get(store,lease.run_id)
    task=next(t for t in graph.work_items(run) if t['id']==lease.step_id)
    phase=task['phase'];adapter=host.workflow_runner.adapter
    try:
        if task['type']=='agent':
            from .profile_harnesses import get as harness
            return harness(task['profile']['harness']).run(host,lease,run,task)
        view=graph.execution_view(run);step=next(s for s in view['steps'] if s['id']==task['id'])
        from .operation_lifecycle import for_step
        implementation=for_step(step['spec'])
        cancelled=lambda:host.stop.is_set() or lease.lost.is_set()
        if phase=='validate':files=task['candidate_result']
        elif phase in ('poll','collect'):
            record=implementation.invoke(adapter,'poll',view,step)
            lease.check()
            if record['status'] in ('failed','cancelled'):
                return stage_result(store,lease,'failed',{'error_code':'provider_failed','phase':'poll'})
            if record['status']!='succeeded':
                return stage_result(store,lease,'waiting',{'external_id':task['external_id'],'poll_after':5})
            phase='collect'
            record_phase(lease,task,phase)
            files=implementation.invoke(adapter,'collect',view,step,record=record,cancelled=cancelled)
        else:
            phase=implementation.phase(step)
            if phase=='submit':
                handle=implementation.invoke(adapter,'submit',view,step)
                return stage_result(store,lease,'waiting',{'external_id':handle,'poll_after':2})
            if phase not in ('local','sync'):raise ValueError('Unsupported execution phase')
            files=implementation.invoke(adapter,phase,view,step,cancelled=cancelled)
        phase='validate'
        if lease.runtime:lease.runtime.checkpoint()
        record_phase(lease,task,phase,files)
        files,role_map=bind_output_roles(step,files)
        from .workflow_runtime import resolve_acceptance_output_roles
        validation_step={**step,'spec':{**step['spec'],'acceptance':resolve_acceptance_output_roles(
            step['spec'].get('acceptance',[]),role_map)}}
        report=validate_candidate(adapter,view,validation_step,files)
        report['input_versions']={s['id']:s['revision'] for s in view['steps'] if s['id'] in step['spec']['depends_on']}
        if lease.runtime:lease.runtime.checkpoint()
        checkpoints=store.query('SELECT manifest,attempt FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                                (run['id'],task['id'],task['revision']))
        if not checkpoints:raise ValueError('Result has no durable checkpoint')
        return stage_result(store,lease,'succeeded',{'files':list(files),'quality':report,'output_roles':role_map},json.loads(checkpoints[0]['manifest']))
    except (LeaseLost,TaskLimitReached):raise
    except Exception as exc:
        current=graph.work_item(graph.get(store,run['id']),task['id'])
        if current:phase=current['phase']
        from .telemetry import emit
        emit('workflow.execution_failed','ERROR',exc=exc,task_id=lease.run_id,step_id=lease.step_id,attempt_id=lease.attempt)
        if host.stop.is_set():raise LeaseLost('Executor is stopping') from exc
        from .execution_contracts import classify_error
        detail=getattr(exc,'diagnostic',{'error_type':type(exc).__name__})
        classification=classify_error(detail,phase)
        diagnostic={'phase':phase,'error_type':type(exc).__name__,'classification':classification,
                    'origin':'model_script' if task['type']=='script' else 'agent' if task['type']=='agent' else 'registered_operation',
                    'attempt_id':lease.attempt,'generation':lease.generation,
                    'returncode':detail.get('exit_code') if isinstance(detail,dict) else None,
                    'code':detail.get('code','') if isinstance(detail,dict) else ''}
        if isinstance(detail,dict) and type(detail.get('http_status'))==int:diagnostic['http_status']=detail['http_status']
        runtime_tail=detail.get('log_tail') or detail.get('runtime_error') or detail.get('stderr_tail') if isinstance(detail,dict) else None
        if isinstance(runtime_tail,str):
            from .egress_policy import scrub,secrets
            diagnostic['log_tail']=scrub(runtime_tail[-4000:],secrets())
        reference_hash=detail.get('reference_sha256') if isinstance(detail,dict) else None
        if isinstance(reference_hash,str) and len(reference_hash)==64 and all(c in '0123456789abcdef' for c in reference_hash):
            diagnostic['reference_sha256']=reference_hash
        if classification=='pre_submit':
            diagnostic.update(request_sent=False,cause_type=detail.get('cause_type',''))
        store.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',
                      (uuid.uuid4().hex,run['id'],task['id'],task['revision'],task['attempts'],graph.encode(diagnostic),store.now()))
        if lease.runtime and lease.runtime.env is not None and not task.get('control_kind'):
            try:lease.runtime.checkpoint()
            except (LeaseLost,TaskLimitReached):raise
            except Exception as checkpoint_error:emit('workflow.failure_checkpoint_failed','WARNING',exc=checkpoint_error,task_id=lease.run_id,step_id=lease.step_id)
        calls=store.query('SELECT state FROM workflow_provider_calls WHERE request_key=?',(run['id']+':'+task['id']+':'+str(task['revision']),))
        from .workflow_agents import ModelServiceError
        try:
            import requests
            transient_model=isinstance(exc,(requests.ConnectionError,requests.Timeout,TimeoutError))
        except ImportError:transient_model=isinstance(exc,TimeoutError)
        if isinstance(exc,ModelServiceError):
            status=exc.diagnostic.get('http_status');transient_model=status==429 or isinstance(status,int) and status>=500
        transient_model=bool(task.get('control_kind') and task['type']=='agent' and transient_model)
        if isinstance(exc,TaskQuotaReached):
            code=quota_error_code(task,exc)
        else:code=failure_error_code(task,phase,calls,classification)
        stage_result(store,lease,'failed',{'error_code':code,'phase':phase,
                                         'classification':classification,'transient':transient_model})
