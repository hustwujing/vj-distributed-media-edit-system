"""Portable, fenced native sessions scoped to one immutable delegated Task."""
import json
from pathlib import Path,PurePosixPath
import shutil
import uuid

from . import workflow_graph as graph
from .session_checkpoint import persistent


def identity(task,configuration,binding):
    from dataclasses import asdict
    return graph.digest({'task':task['spec'],'policy':task['profile'],
                         'model':configuration.snapshot(),'runtime':asdict(binding)})


def load(store,run,task,configuration_digest,input_digest):
    rows=store.query('SELECT * FROM native_task_sessions WHERE run_id=? AND task_id=? AND revision=?',
        (run['id'],task['id'],task['revision']))
    if not rows:return None
    row=rows[0]
    if row['configuration_digest']!=configuration_digest or row['input_digest']!=input_digest:
        raise ValueError('Native session configuration or frozen inputs changed; replan is required')
    value={**row,'manifest':json.loads(row['manifest_json'])}
    output=PurePosixPath(value['output_directory'])
    prefix=PurePosixPath('edit/workflows')/run['id']/task['id']
    if '..' in output.parts or not output.is_relative_to(prefix) or output==prefix:
        raise ValueError('Native session output escaped its Task')
    return value


def restore(runtime,saved,files):
    """Restore only server-committed hashes; never project-wide SDK state."""
    from .sandbox_workspace import SandboxWorkspaceRestorer
    store=runtime.store;project=runtime.project.name;records=saved['manifest'];sources={}
    for group,items in records.items():
        if group not in ('workspace','state'):raise ValueError('Invalid native session group')
        for name,record in items.items():
            path=PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts:raise ValueError('Invalid native session path')
            if group=='state' and not persistent(name):raise ValueError('Runtime credentials cannot be restored')
            if group=='workspace' and not path.is_relative_to(PurePosixPath(saved['output_directory'])):
                raise ValueError('Native workspace escaped its Task')
            source=store.artifacts.materialize(project,record['artifact']) if store.shared_files else store.root/record['location']
            sources[record['artifact']]=source
    portable={group:{name:{key:value for key,value in record.items() if key!='location'}
        for name,record in items.items()} for group,items in records.items()}
    SandboxWorkspaceRestorer(sources.__getitem__,max_bytes=runtime.limit).restore(files,portable)


def save(runtime,run,task,configuration_digest,input_digest,session_id,output,manifest):
    """Commit after native children stop; session completion is not publication."""
    from .runtime_workspace import WorkspaceCollector
    store=runtime.store;lease=runtime.lease
    if not runtime.frozen:raise ValueError('Cannot checkpoint a running native session')
    folder=store.project_dir(run['project_id'])/'native-sessions'/run['id']/task['id']/str(task['revision'])/(lease.attempt+'-'+uuid.uuid4().hex)
    folder.mkdir(parents=True,mode=0o700)
    selected={'workspace':{},'state':{}}
    for group,items in manifest.items():
        if group not in selected:raise ValueError('Invalid exported session group')
        for name,record in items.items():
            path=PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts:raise ValueError('Invalid exported session path')
            if group=='state' and persistent(name) or group=='workspace' and path.is_relative_to(PurePosixPath(output)):
                selected[group][name]=record
    records={'workspace':{},'state':{}}
    def sink(group,name,data,record):
        path=folder/group/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with path.open('xb') as stream:stream.write(data)
        path.chmod(0o600)
        item={**record,'location':str(path.relative_to(store.root))}
        if store.shared_files:item['artifact']=store.artifacts.upload(run['project_id'],path)['id']
        else:item['artifact']=item['location']
        records[group][name]=item
    try:
        WorkspaceCollector(runtime.session.files,max_bytes=runtime.limit,max_files=20000).collect(selected,sink)
        with store.connect() as db:
            lease.validate(db)
            store.check_storage(run['project_id'],sum(record['size'] for items in records.values() for record in items.values()),
                replace_native_session=(run['id'],task['id'],task['revision']))
            if store.shared_files:
                for group,items in records.items():
                    for name,record in items.items():
                        store.artifacts.publish(run['project_id'],f'native-sessions/{run["id"]}/{task["id"]}/{task["revision"]}/{group}/{name}',record['artifact'])
            db.execute('''INSERT INTO native_task_sessions VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,task_id,revision) DO UPDATE SET
                attempt_id=excluded.attempt_id,configuration_digest=excluded.configuration_digest,
                input_digest=excluded.input_digest,session_id=excluded.session_id,
                output_directory=excluded.output_directory,manifest_json=excluded.manifest_json,updated=excluded.updated''',
                (run['id'],task['id'],task['revision'],lease.attempt,configuration_digest,input_digest,
                 session_id,output,graph.encode(records),store.now()))
    except BaseException:
        shutil.rmtree(folder,ignore_errors=True);raise
