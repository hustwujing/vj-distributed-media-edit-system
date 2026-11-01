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
    workflow=get(store,lease.turn_id)
    step=next(s for s in workflow['steps'] if s['id']==lease.step_id)
    runtime.quiesce();lease.check()
    prefix=f'edit/workflows/{workflow["id"]}/{step["id"]}'
    source=runtime.workspace/prefix
    names=[str(path.relative_to(runtime.workspace)) for path in source.rglob('*') if path.is_file() and not path.is_symlink()] if source.exists() else []
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


def restore(runtime):
    store,lease=runtime.store,runtime.lease
    if lease.kind=='agent':
        # A new conversation has no workflow of its own. Its project checkpoint
        # can predate the last completed background step (for example, when
        # semantic review was interrupted). Restore retained current revisions
        # from the project's independent step snapshots as well. This grants
        # visibility only; candidate/acceptance status is not changed.
        rows=store.query('''SELECT c.workflow,c.step,c.manifest FROM execution_step_checkpoints c
            JOIN agent_workflows w ON w.id=c.workflow AND w.project=c.project
            JOIN agent_steps s ON s.workflow=c.workflow AND s.id=c.step AND s.revision=c.revision
            WHERE c.project=? ORDER BY c.workflow,c.step''',(runtime.project.name,))
    else:
        from .workflows import get
        workflow=get(store,lease.turn_id)
        if not workflow:return
        by_id={s['id']:s for s in workflow['steps']}
        required=set(by_id) if not lease.step_id else {lease.step_id}
        pending=list(required)
        while pending:
            for parent in by_id[pending.pop()]['spec']['depends_on']:
                if parent not in required:required.add(parent);pending.append(parent)
        rows=[]
        for step_id in required:
            step=by_id[step_id]
            rows.extend(store.query('SELECT workflow,step,manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=? AND project=?',
                         (workflow['id'],step_id,step['revision'],runtime.project.name)))
    records={}
    from .artifact_store import logical_path
    for row in rows:
        for relative,record in json.loads(row['manifest']).items():
            logical_path(relative)
            if not relative.startswith(f'edit/workflows/{row["workflow"]}/{row["step"]}/'):
                raise ValueError('Step checkpoint escaped its output scope')
            records[relative]=record
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
