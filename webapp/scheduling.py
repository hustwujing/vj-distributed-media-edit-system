"""Fair candidate selection and explainable resource admission."""
import json
import os
from .execution_contracts import dispatch_kind
from .operations import for_step


def gpu_devices():
    from pathlib import Path
    configured=os.environ.get('VIDEO_USE_GPU_DEVICES')
    if configured is not None:
        devices=[int(value.strip()) for value in configured.split(',') if value.strip()]
        if any(value<0 or value>255 for value in devices):raise ValueError('Invalid configured GPU index')
        return sorted(set(devices))
    return sorted(int(path.name[6:]) for path in Path('/dev').glob('nvidia[0-9]*') if path.name[6:].isdigit())


def next_step(w,now):
    done={s['id'] for s in w['steps'] if s['status']=='succeeded'}
    return next((s for s in w['steps'] if s['status'] in ('pending','waiting','verifying')
                 and set(s['spec']['depends_on']).issubset(done) and s['next_poll']<=now),None)


def available_steps(store,run_id,now):
    from .workflow_graph import get as graph_get
    dynamic=graph_get(store,run_id)
    if dynamic:
        from .workflow_dispatch import available
        return available(store,dynamic,now)
    from .workflows import get
    workflow=get(store,run_id)
    if not workflow or workflow['status']!='ready':return []
    # Claims recheck this too: a candidate selected before a review/quality
    # transition cannot start new work before control-plane reconciliation.
    if any(step['status'] in ('diagnosing','agent_review','awaiting_review','paused') for step in workflow['steps']):return []
    if workflow.get('agent_review'):return []
    control=store.query('SELECT pause_requested FROM workflow_control WHERE run_id=?',(run_id,))
    if control and control[0]['pause_requested']:return []
    leases=store.query('SELECT slot FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(run_id,now))
    busy={row['slot'] for row in leases}
    if run_id in busy:return []
    done={step['id'] for step in workflow['steps'] if step['status']=='succeeded'}
    return [step['id'] for step in workflow['steps']
            if run_id+'|'+step['id'] not in busy
            and step['status'] in ('pending','waiting','verifying','running','submitting','collecting')
            and set(step['spec']['depends_on']).issubset(done) and step['next_poll']<=now]


def candidates(coordinator,model_available):
    store=coordinator.store
    health=store.query('SELECT next_retry FROM execution_model_health WHERE worker=?',(coordinator.worker,))
    scoped=store.query('SELECT digest,next_retry FROM execution_profile_health WHERE worker=?',(coordinator.worker,))
    profile_health={r['digest']:r['next_retry'] for r in scoped}
    legacy_available=model_available and not (health and health[0]['next_retry']>store.now())
    rows=[dict(r,manual=True) for r in coordinator.manual_candidates()]+coordinator.candidates()
    result=[]
    for row in rows:
        if row.get('dynamic'):
            detail=store.query('SELECT type,spec_json FROM run_work_items WHERE run_id=? AND id=?',(row['workflow'],row['step_id']))
            if not detail:continue
            if detail[0]['type']=='agent' and not model_available:continue
            if detail[0]['type']=='operation':
                from .operations import get
                spec=json.loads(detail[0]['spec_json']);op=get(spec['operation'],spec['implementation'])
                if not op.node_enabled() or not op.available():continue
            result.append(row);continue
        if dispatch_kind(row)=='agent' and row['status']!='cancelling':
            from .agent_profiles import for_task
            digest=for_task(store,row['id']).digest
            eligible=model_available and (profile_health.get(digest,0)<=store.now()) if scoped else legacy_available
            if not eligible:continue
        if row.get('step_id') and row['status']!='cancelling':
            details=store.query('SELECT spec,bindings,status FROM agent_steps WHERE workflow=? AND id=?',(row['workflow'],row['step_id']))
            if not details:continue
            detail=details[0];bindings=json.loads(detail['bindings'])
            if bindings.get('_environment'):
                from .runtime_image import fingerprint
                if bindings['_environment']!=fingerprint():continue
            try:
                operation=for_step(json.loads(detail['spec']))
                if operation and not operation.node_enabled():continue
                if operation and detail['status']=='pending' and not operation.available():continue
            except ValueError:continue
        result.append(row)
    # Ordering accounts for ALL dispatch classes and previous turns of an owner.
    # Old jobs cannot continually jump ahead merely because manual is interleaved first.
    tasks=store.query('SELECT t.id,p.owner,t.created FROM (SELECT id,project,created FROM execution_subjects) t JOIN projects p ON p.id=t.project')
    owners={r['id']:r['owner'] for r in tasks};created={r['id']:r['created'] for r in tasks}
    history=store.query('SELECT p.owner,MAX(a.started) AS recent FROM execution_attempts a JOIN (SELECT id,project FROM execution_subjects) t ON t.id=a.run_id JOIN projects p ON p.id=t.project GROUP BY p.owner')
    recent={r['owner']:r['recent'] for r in history}
    by_task={r['run_id']:r['recent'] for r in store.query('SELECT run_id,MAX(started) AS recent FROM execution_attempts GROUP BY run_id')}
    return sorted(result,key=lambda r:(r['status']!='cancelling',recent.get(owners.get(r['id']),0),by_task.get(r['id'],0),created.get(r['id'],0),r['id']))


def requirements(store,run_id,kind,step_id=None):
    from .workflow_graph import get as graph_get
    dynamic=graph_get(store,run_id)
    if dynamic and step_id:
        from .workflow_dispatch import requirements as dynamic_requirements
        return dynamic_requirements(store,dynamic,step_id)
    selected_step=None
    cpu=2;memory=4096;provider='';gpu=False
    if kind=='agent':
        from .plan_readiness import is_light
        if is_light(store,run_id):return {'cpu':1,'memory_mb':256,'gpu':False,'provider':'','step_id':None}
        from .execution_intent import get as get_intent
        intent=get_intent(store,run_id)
        reviews=store.query("SELECT id FROM agent_workflows WHERE run_id=? AND status IN ('reviewing','resuming')",(run_id,))
        if not reviews and intent and intent['route']=='single' and intent['operations'][0]!='script':
            from .operations import get
            op=get(intent['operations'][0]);memory=max(memory,op.memory_mb);gpu=op.gpu
    if kind=='manual':
        job=store.job(run_id)
        if job and job['kind']=='image' and job['payload'].get('image_task'):
            from .operations import for_image
            if job['payload']['image_task']['action']!='export':
                op=for_image(job['payload']['image_task']['action']);cpu=op.cpu;memory=op.memory_mb;provider=op.concurrency_group;gpu=op.gpu
                if op.concurrency_group:cpu=op.cpu;memory=op.memory_mb
    if kind=='workflow':
        from .workflows import get
        w=get(store,run_id)
        s=None
        if w and w['status']=='ready':
            if step_id:s=next((x for x in w['steps'] if x['id']==step_id),None)
            else:
                # A takeover must reserve the interrupted step before recovery
                # turns it back into pending/waiting; it cannot use a default cap.
                s=next((x for x in w['steps'] if x['status'] in ('running','submitting','collecting')),None) or next_step(w,store.now())
        selected_step=s['id'] if s else None
        if s:
            op=for_step(s['spec'])
            if op:
                provider=op.concurrency_group;gpu=op.gpu;cpu=op.cpu;memory=op.memory_mb
                if op.executor.endswith('async') or op.executor=='image_remote':
                    cpu=op.cpu;memory=op.memory_mb
            if s['status'] in ('waiting','verifying'):gpu=False
            if s['status']=='waiting':cpu=1;memory=256
        else:cpu=1;memory=2048 if w and w['status']=='ready' else 512
    return {'cpu':cpu,'memory_mb':memory,'gpu':gpu,'provider':provider,'step_id':selected_step}


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS execution_operation_catalog(worker TEXT PRIMARY KEY,catalogue TEXT NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS execution_scheduler_configs(worker TEXT PRIMARY KEY,settings TEXT NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS execution_model_health(worker TEXT PRIMARY KEY,state TEXT NOT NULL,next_retry REAL NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS execution_profile_health(worker TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,next_retry REAL NOT NULL,updated REAL NOT NULL,PRIMARY KEY(worker,digest));
    CREATE TABLE IF NOT EXISTS execution_waits(task_id TEXT PRIMARY KEY,reason TEXT NOT NULL,next_retry REAL NOT NULL,updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS execution_resource_claims(
      attempt TEXT PRIMARY KEY,task_id TEXT NOT NULL,node TEXT NOT NULL,cpu INTEGER NOT NULL,memory_mb INTEGER NOT NULL,
      provider TEXT NOT NULL,created REAL NOT NULL
    );''')


def admit(db,store,run_id,kind,node,timestamp,step_id=None):
    need=requirements(store,run_id,kind,step_id=step_id)
    if need['gpu'] and not gpu_devices():return None
    used=db.execute('''SELECT COALESCE(SUM(r.cpu),0) AS cpu,COALESCE(SUM(r.memory_mb),0) AS memory_mb
      FROM execution_resource_claims r WHERE node=? AND (
      EXISTS(SELECT 1 FROM execution_leases l WHERE l.attempt=r.attempt AND l.active=1 AND l.expires>?)
      OR EXISTS(SELECT 1 FROM execution_cleanup c WHERE c.attempt=r.attempt AND c.state<>'complete'))''',(node,timestamp)).fetchone()
    if used['cpu']+need['cpu']>int(os.environ.get('VIDEO_USE_NODE_CPU_SLOTS','4')):return None
    if used['memory_mb']+need['memory_mb']>int(os.environ.get('VIDEO_USE_NODE_MEMORY_MB','12288')):return None
    if need['provider']:
        # Count external waits, not only current HTTP requests. Unknown submission
        # outcomes remain counted until an operator or upstream can confirm the outcome.
        from .operations import get
        polling=False
        if kind=='workflow' and need['step_id']:
            selected=db.execute('SELECT s.phase FROM run_work_items s JOIN runs w ON w.id=s.run_id WHERE w.id=? AND s.id=?',(run_id,need['step_id'])).fetchone()
            if selected:polling=selected['phase'] in ('poll','collect','validate')
            else:
                selected=db.execute('SELECT s.status FROM agent_steps s JOIN agent_workflows w ON w.id=s.workflow WHERE w.run_id=? AND s.id=?',(run_id,need['step_id'])).fetchone()
                polling=bool(selected and selected['status']!='pending')
        elif kind=='manual':
            existing=db.execute('SELECT state FROM manual_operation_runs WHERE job_id=?',(run_id,)).fetchone()
            polling=bool(existing and existing['state']!='pending')
        if not polling:
            rows=db.execute("SELECT operation,implementation FROM workflow_provider_calls WHERE state IN ('submitting','waiting','queued','running','submission_unknown')").fetchall()
            count=0
            for row in rows:
                binding=json.loads(row['implementation'])
                category=binding['concurrency_group']
                if category==need['provider']:count+=1
            # Reserve a provider slot during claim, before submission intent is
            # written. Once an intent exists its state is counted above instead.
            reservations=db.execute('''SELECT r.provider FROM execution_resource_claims r
              JOIN execution_attempts a ON a.id=r.attempt
              JOIN execution_leases l ON l.attempt=r.attempt
              LEFT JOIN agent_workflows w ON w.run_id=r.task_id
              LEFT JOIN agent_steps s ON s.workflow=w.id AND s.id=a.step_id
              LEFT JOIN runs dw ON dw.id=r.task_id
              LEFT JOIN run_work_items ds ON ds.run_id=dw.id AND ds.id=a.step_id
              WHERE l.active=1 AND l.expires>? AND NOT EXISTS (
                SELECT 1 FROM workflow_provider_calls c
                WHERE c.workflow=COALESCE(dw.id,w.id,r.task_id)
                AND c.step=CASE WHEN a.step_id='' THEN 'image' ELSE a.step_id END
                AND c.revision=COALESCE(ds.revision,s.revision,0))''',(timestamp,)).fetchall()
            count+=sum(r['provider']==need['provider'] for r in reservations)
            if count>=int(os.environ.get('VIDEO_USE_PROVIDER_CONCURRENCY','4')):return None
    return need


def limits(*, include_runtime_profile=True):
    from .runtime_image import fingerprint
    return {'worker':int(os.environ.get('VIDEO_USE_CONCURRENCY','2')),
        'operations':os.environ.get('VIDEO_USE_NODE_OPERATIONS','*').split(','),
        'node':int(os.environ.get('VIDEO_USE_NODE_CONCURRENCY',os.environ.get('VIDEO_USE_CONCURRENCY','2'))),
        'user':int(os.environ.get('VIDEO_USE_USER_CONCURRENCY','2')),'cpu_slots':int(os.environ.get('VIDEO_USE_NODE_CPU_SLOTS','4')),
        'memory_mb':int(os.environ.get('VIDEO_USE_NODE_MEMORY_MB','12288')),'provider':int(os.environ.get('VIDEO_USE_PROVIDER_CONCURRENCY','4')),
        'gpu_devices':gpu_devices(),'runtime_profile':fingerprint() if include_runtime_profile else None}


def snapshot(store,run_id):
    from .workflows import get
    w=get(store,run_id);now=store.now()
    recorded=store.query('SELECT reason,updated,next_retry FROM execution_waits WHERE task_id=?',(run_id,))
    reason=recorded[0]['reason'] if recorded else ''
    task=store.query("SELECT t.status,t.created,t.updated,p.owner FROM (SELECT id,project,status,created,updated FROM execution_subjects) t JOIN projects p ON p.id=t.project WHERE t.id=?",(run_id,))
    workers=store.query('SELECT w.id,w.node,w.model,c.settings,h.next_retry FROM execution_workers w LEFT JOIN execution_scheduler_configs c ON c.worker=w.id LEFT JOIN execution_model_health h ON h.worker=w.id WHERE w.draining=0 AND w.heartbeat>? ORDER BY w.heartbeat DESC',(now-20,))
    models=[r for r in workers if r['model']]
    needs_model=bool((w and w['status'] in ('reviewing','resuming')) or (not w and store.query('SELECT id FROM agent_turns WHERE id=?',(run_id,))))
    if not workers and task and task[0]['status'] in ('queued','waiting'):reason='resource'
    if needs_model and task and task[0]['status']=='queued':
        if not workers:reason='resource'
        elif not models:reason='model_unconfigured'
        elif all((r['next_retry'] or 0)>now for r in models):reason='model_unavailable'
    if w:
        if w['status']=='paused':reason='user' if w.get('user_requests') or any(s['status']=='awaiting_review' for s in w['steps']) else 'paused'
        elif w['status']=='ready':
            step=next_step(w,now)
            if step:reason=step.get('wait_reason') or reason
            elif any(s['status']=='waiting' for s in w['steps']):reason='provider'
            elif any(s['status']=='pending' for s in w['steps']):reason='dependency'
    manual=store.query('SELECT state FROM manual_operation_runs WHERE job_id=?',(run_id,))
    if manual and manual[0]['state']=='waiting':reason='provider'
    attempts=store.query('SELECT COUNT(*) AS n,MIN(started) AS first_start,COALESCE(SUM(COALESCE(finished,?)-started),0) AS dispatch_seconds FROM execution_attempts WHERE run_id=?',(now,run_id))[0]
    occupied=store.query('SELECT COUNT(*) AS n FROM execution_leases WHERE active=1 AND expires>?',(now,))[0]['n']
    usage=store.query('SELECT COALESCE(SUM(cpu),0) AS cpu,COALESCE(SUM(memory_mb),0) AS memory_mb FROM execution_resource_claims r WHERE EXISTS(SELECT 1 FROM execution_leases l WHERE l.attempt=r.attempt AND l.active=1 AND l.expires>?)',(now,))[0]
    calls=store.query('SELECT COALESCE(SUM(network_seconds),0) AS network_seconds,COALESCE(SUM(poll_count),0) AS polls FROM workflow_provider_calls WHERE workflow=?',(w['id'] if w else run_id,))[0]
    elapsed=max(0,(task[0]['updated'] if task and task[0]['status'] in ('succeeded','failed','cancelled') else now)-task[0]['created']) if task else 0
    queued=max(0,(attempts['first_start'] if attempts['first_start'] is not None else task[0]['created']+elapsed)-task[0]['created']) if task else 0
    holds=store.query("SELECT COUNT(*) AS n FROM execution_cleanup WHERE state<>'complete'")[0]['n']
    lease=store.query('SELECT worker FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(run_id,now))
    if lease:workers.sort(key=lambda worker:worker['id']!=lease[0]['worker'])
    return {'initial_queue_seconds':round(queued,2),'non_dispatch_seconds_estimate':round(max(0,elapsed-attempts['dispatch_seconds']),2),'cleanup_holds_all_nodes':holds,
        'limits_scope':{'worker':workers[0]['id'],'node':workers[0]['node']} if workers else None,'network_seconds':round(calls['network_seconds'],2),'provider_polls':calls['polls'],'attempts':attempts['n'],'dispatch_seconds':round(attempts['dispatch_seconds'],2),
        'age_seconds':round(elapsed,2),'wait_reason':reason,'wait_recorded_at':recorded[0]['updated'] if recorded else None,'next_retry':recorded[0]['next_retry'] if recorded else None,
        'phase_metrics':store.query('SELECT phase,SUM(seconds) AS seconds,SUM(calls) AS calls,SUM(failures) AS failures FROM execution_phase_metrics WHERE task_id=? GROUP BY phase',(run_id,)),
        # Read-only API processes cannot access the privileged runtime manifest.
        # Defaults describe configured capacity, never an attested worker runtime.
        'limits':json.loads(workers[0]['settings']) if workers and workers[0]['settings'] else limits(include_runtime_profile=False),
        'limits_source':'worker' if workers and workers[0]['settings'] else 'defaults','active_leases_all_nodes':occupied,'resources_all_nodes':usage}


def defer_resource(lease,seconds=5):
    lease.waiting_for_resource=True
    if lease.step_id:
        lease.store.execute("UPDATE agent_steps SET wait_reason='resource',next_poll=? WHERE id=? AND workflow IN (SELECT id FROM agent_workflows WHERE run_id=?)",
                            (lease.store.now()+seconds,lease.step_id,lease.run_id))
        return
    lease.store.execute("INSERT INTO execution_waits VALUES (?,'resource',?,?) ON CONFLICT(task_id) DO UPDATE SET reason=excluded.reason,next_retry=excluded.next_retry,updated=excluded.updated",(lease.run_id,lease.store.now()+seconds,lease.store.now()))
