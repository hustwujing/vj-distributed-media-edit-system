"""Independent node output snapshots; parallel steps never replace Agent state."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS execution_step_checkpoints(
      workflow TEXT NOT NULL,step TEXT NOT NULL,revision INTEGER NOT NULL,
      project TEXT NOT NULL,attempt TEXT NOT NULL,manifest TEXT NOT NULL,updated REAL NOT NULL,
      PRIMARY KEY(workflow,step,revision))''')


def save(runtime):
    from .workflows import get
    from .execution_runtime import copy_tree
    lease,store=runtime.lease,runtime.store
    workflow=get(store,lease.run_id)
    step=next(s for s in workflow['steps'] if s['id']==lease.step_id)
    runtime.quiesce();lease.check()
    prefix=f'edit/workflows/{workflow["id"]}/{step["id"]}'
    source=runtime.workspace/prefix
    names=[str(path.relative_to(runtime.workspace)) for path in source.rglob('*') if path.is_file() and not path.is_symlink()
           and str(path.relative_to(runtime.workspace)) not in getattr(runtime,'repair_input_paths',set())] if source.exists() else []
    target=runtime.project/'step-checkpoints'/workflow['id']/step['id']/str(step['revision'])/(lease.attempt+'-'+uuid.uuid4().hex)
    records=copy_tree(runtime.workspace,target,runtime.service_uid,runtime.service_gid,runtime.limit,include=set(names))
    prior=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                      (workflow['id'],step['id'],step['revision']))
    previous=json.loads(prior[0]['manifest']) if prior else {}
    try:
        for name,record in records.items():
            record['location']=str((target/name).relative_to(store.root))
            if store.shared_files:
                old=previous.get(name,{})
                if old.get('sha256')==record['sha256'] and old.get('size')==record['size'] and old.get('artifact'):
                    record['artifact']=old['artifact']
                else:
                    uploaded=store.artifacts.upload(runtime.project.name,target/name)
                    if uploaded['sha256']!=record['sha256']:raise ValueError('Step output changed during upload')
                    record['artifact']=uploaded['id']
        merged={**previous,**records}
        with store.connect() as db:
            lease.validate(db)
            store.check_storage(runtime.project.name,sum(item['size'] for item in merged.values()),
                                replace_step=(workflow['id'],step['id'],step['revision']))
            if store.shared_files:
                for name,record in records.items():
                    logical=f'step-checkpoints/{workflow["id"]}/{step["id"]}/{step["revision"]}/{name}'
                    store.artifacts.publish(runtime.project.name,logical,record['artifact'])
            db.execute('''INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)
              ON CONFLICT(workflow,step,revision) DO UPDATE SET attempt=excluded.attempt,
              manifest=excluded.manifest,updated=excluded.updated''',
              (workflow['id'],step['id'],step['revision'],runtime.project.name,lease.attempt,json.dumps(merged),store.now()))
            db.execute('UPDATE execution_attempts SET checkpoint=? WHERE id=?',
                       ('step:'+workflow['id']+':'+step['id']+':'+str(step['revision']),lease.attempt))
    except BaseException:
        shutil.rmtree(target,ignore_errors=True)
        raise


def current_agent_rows(store,project):
    """The same current, project-owned snapshots used for restore and consent."""
    if not store.query("SELECT name FROM sqlite_master WHERE name='execution_step_checkpoints'"):return []
    rows=store.query('''SELECT c.workflow,c.step,c.manifest FROM execution_step_checkpoints c
        JOIN agent_workflows w ON w.id=c.workflow AND w.project=c.project
        JOIN agent_steps s ON s.workflow=c.workflow AND s.id=c.step AND s.revision=c.revision
        WHERE c.project=? ORDER BY c.workflow,c.step''',(project,))
    if store.query("SELECT name FROM sqlite_master WHERE name='runs'"):
        rows+=store.query('''SELECT c.workflow,c.step,c.manifest FROM execution_step_checkpoints c
            JOIN runs w ON w.id=c.workflow AND w.project_id=c.project
            JOIN run_tasks s ON s.run_id=w.id AND s.id=c.step AND s.revision=c.revision
            WHERE c.project=? ORDER BY c.workflow,c.step''',(project,))
    return rows


def records_for(rows):
    from .artifact_store import logical_path
    records={}
    for row in rows:
        for relative,record in json.loads(row['manifest']).items():
            logical_path(relative)
            if not relative.startswith(f'edit/workflows/{row["workflow"]}/{row["step"]}/'):
                raise ValueError('Step checkpoint escaped its output scope')
            records[relative]=record
    return records


def restore(runtime):
    store,lease=runtime.store,runtime.lease
    if lease.kind=='agent':
        # Restore current retained outputs for visibility, without approving
        # candidates or inheriting execution authority from an earlier turn.
        rows=current_agent_rows(store,runtime.project.name)
    else:
        from .workflows import get
        workflow=get(store,lease.run_id)
        if not workflow:return
        by_id={s['id']:s for s in workflow['steps']}
        required=set(by_id) if not lease.step_id or workflow.get('dynamic') and self_control(store,lease) else {lease.step_id}
        pending=list(required)
        while pending:
            for parent in by_id[pending.pop()]['spec']['depends_on']:
                if parent not in required:required.add(parent);pending.append(parent)
        rows=[]
        for step_id in required:
            step=by_id[step_id]
            rows.extend(store.query('SELECT workflow,step,manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=? AND project=?',
                         (workflow['id'],step_id,step['revision'],runtime.project.name)))
        if workflow.get('dynamic'):
            from .workflow_graph import get as graph_get,work_item
            graph=graph_get(store,lease.run_id);selected=work_item(graph,lease.step_id)
            permitted=set()
            if selected.get('control_kind'):
                table='planner_runs' if selected['spec']['profile']=='media_planner' else 'workflow_reviews'
                request=store.query(f'SELECT snapshot_json FROM {table} WHERE id=?',(selected['spec']['request_id'],))[0]
                frozen=json.loads(request['snapshot_json'])
                for task in frozen.get('tasks',[])+frozen.get('upstream',[])+frozen.get('evidence_tasks',[]):
                    permitted.update(task.get('accepted_result',[]));permitted.update(task.get('candidate_result',[]))
                permitted.update(frozen.get('diagnostic_files',[]))
            else:
                for reference in selected['spec'].get('inputs',{}).values():
                    if reference.get('step'):
                        parent=by_id[reference['step']]
                        path=parent['quality'].get('output_roles',{}).get(reference['role'])
                        if path:permitted.add(path)
                for row in rows:
                    if row['step']==selected['id']:permitted.update(json.loads(row['manifest']))
            rows=[{**row,'manifest':json.dumps({name:record for name,record in json.loads(row['manifest']).items() if name in permitted})} for row in rows]
            from .repair_inputs import lookup
            runtime.repair_input_paths=set()
            if not selected.get('control_kind'):
                for ref in selected['spec'].get('inputs',{}).values():
                    if not ref.get('repair_candidate'):continue
                    candidate=lookup(store,graph,ref['repair_candidate'])
                    runtime.repair_input_paths.add(candidate['path'])
                    rows.append({'workflow':graph['id'],'step':candidate['reference']['logical_task_id'],
                        'manifest':json.dumps({candidate['path']:candidate['record']})})
    records=records_for(rows)
    if not records:return
    # Account for the whole restored workspace, not only this batch. The
    # existing bounded mount is also retained as the final hard limit.
    sizes={str(p.relative_to(runtime.workspace)):p.stat().st_size for p in runtime.workspace.rglob('*') if p.is_file() and not p.is_symlink()}
    sizes.update({name:record['size'] for name,record in records.items()})
    if len(sizes)>20000 or sum(sizes.values())>runtime.limit:
        raise RuntimeError('Checkpoint exceeds configured file or byte limit')
    for relative,record in records.items():
        target=runtime.workspace/relative
        for path in (target,*target.parents):
            if path==runtime.workspace:break
            if path.is_symlink():raise ValueError('Invalid checkpoint destination')
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.is_file() and target.stat().st_size==record['size']:
            with target.open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()==record['sha256']:continue
        if store.shared_files:
            store.artifacts.materialize(runtime.project.name,record['artifact'],target)
        else:
            source=store.root/record['location']
            with source.open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=record['sha256']:
                    raise ValueError('Step checkpoint integrity mismatch')
            shutil.copyfile(source,target)
        with target.open('rb') as stream:
            if target.stat().st_size!=record['size'] or hashlib.file_digest(stream,'sha256').hexdigest()!=record['sha256']:
                raise ValueError('Step checkpoint integrity mismatch')
        os.chown(target,runtime.uid,runtime.gid);target.chmod(0o600)
        parent=target.parent
        while parent!=runtime.workspace:
            os.chown(parent,runtime.uid,runtime.gid);parent.chmod(0o700);parent=parent.parent


def self_control(store,lease):
    from .workflow_graph import get,work_item
    run=get(store,lease.run_id)
    item=work_item(run,lease.step_id) if run else None
    return bool(item and item.get('control_kind'))
