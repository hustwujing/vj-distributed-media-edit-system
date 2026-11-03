"""Strict cross-plan reuse of committed operation results, never execution authority.

Receipts are immutable. A reuse request pins workflow/step/revision; matching
specifications permit copying a candidate, not inheriting its quality approval.
"""
import hashlib
import json
import os
from pathlib import Path
import uuid
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field


class ReuseRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workflow: str = Field(pattern=r'^[a-f0-9]{32}$')
    step: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    revision: int = Field(ge=0)


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS workflow_reuse_receipts(
        workflow TEXT NOT NULL,step TEXT NOT NULL,revision INTEGER NOT NULL,
        project TEXT NOT NULL,receipt TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(workflow,step,revision))''')


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@lru_cache(maxsize=1)
def server_contract():
    # The sandbox fingerprint covers fixed tools and dependencies. Also bind
    # the trusted host adapter/contract code, which is not in the tool package.
    root=Path(__file__).parent
    return digest({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
                   ('workflow_reuse.py','workflow_runtime.py','workflow_inputs.py','operations.py',
                    'operation_parameters.py','operation_lifecycle.py')})


def eligible(spec):
    from .operations import for_step
    from .operation_lifecycle import for_operation,Lifecycle
    op=for_step(spec)
    return bool(op and op.reuse_safe and not op.external_side_effects and op.model_profile=='none'
                and for_operation(op).lifecycle==Lifecycle.LOCAL
                and not spec.get('preflight') and not spec.get('config_files')
                # Ordering-only dependencies can carry undeclared semantics.
                and set(spec.get('depends_on',[]))=={r['step'] for r in spec.get('inputs',{}).values() if r.get('step')})


def specification(spec):
    from .operations import get,validate_parameters
    if not eligible(spec):raise ValueError('Operation does not support strict reuse')
    op=get(spec['operation'],spec['implementation'])
    return {'version':1,'operation':op.id,'implementation':spec['implementation'],
            'contract_version':spec.get('contract_version',1),
            'parameters':validate_parameters(op.id,spec.get('parameters',{}),spec['inputs'],spec['implementation']),
            'output_roles':list(op.outputs),'output_files':op.output_files,
            # Some media operations consume declared inputs in order.
            'input_roles':list(spec['inputs'])}


def compare_spec(spec,receipt):
    if canonical(specification(spec))!=canonical(receipt['specification']):
        raise ValueError('Reuse specification mismatch: operation, implementation, parameters or roles changed')


def input_signature(spec,bindings):
    result=[]
    for role in spec['inputs']:
        record=bindings.get(role)
        if not record or not record.get('sha256'):raise ValueError('Missing verified input binding: '+role)
        if record.get('asset') and not record.get('reuse_identity'):raise ValueError('Legacy input lacks fixed identity: '+role)
        metadata={k:v for k,v in record.get('metadata',{}).items() if k not in ('name','sha256','size')}
        result.append({'role':role,'sha256':record['sha256'],'metadata':metadata,
                       'suffix':Path(record['path']).suffix.lower(),
                       'identity':record.get('reuse_identity'),
                       'asset':record.get('asset'),'output_role':record.get('role')})
    return result


def source_receipt(store,project,reference):
    ref=ReuseRef.model_validate(reference)
    rows=store.query('''SELECT r.receipt,r.digest,s.status,s.revision,s.quality,s.result,s.spec,s.bindings
        FROM workflow_reuse_receipts r JOIN agent_workflows w ON w.id=r.workflow AND w.project=r.project
        JOIN agent_steps s ON s.workflow=r.workflow AND s.id=r.step
        WHERE r.workflow=? AND r.step=? AND r.revision=? AND r.project=?''',
        (ref.workflow,ref.step,ref.revision,project))
    if not rows:raise ValueError('No committed reuse receipt in this project; historical results are not backfilled')
    row=rows[0];receipt=json.loads(row['receipt']);quality=json.loads(row['quality'])
    if digest(receipt)!=row['digest']:raise ValueError('Reuse receipt integrity mismatch')
    if row['revision']!=ref.revision or row['status']!='succeeded':
        raise ValueError('Reuse source revision is superseded or no longer approved')
    if not quality.get('technical_valid') or not quality.get('passed') or quality.get('requires_agent') or quality.get('requires_review') or quality.get('review')=='rejected':
        raise ValueError('Reuse source has not passed acceptance')
    if json.loads(row['result'])!=receipt['files']:raise ValueError('Reuse source results changed')
    spec=json.loads(row['spec']);bindings=json.loads(row['bindings'])
    compare_spec(spec,receipt)
    if (input_signature(spec,bindings)!=receipt['inputs'] or bindings.get('_environment')!=receipt['environment']
            or bindings.get('_reuse_contract')!=receipt['server_contract']):
        raise ValueError('Reuse source execution bindings changed')
    return receipt


def bind_execution(store,w,s):
    """Pin trusted host semantics before processing, not at later acceptance."""
    from .workflows import change
    with store.connect() as db:
        row=db.execute('SELECT bindings,revision FROM agent_steps WHERE workflow=? AND id=?',(w['id'],s['id'])).fetchone()
        if row['revision']!=s['revision']:raise ValueError('Execution revision changed')
        bindings=json.loads(row['bindings']);current=server_contract()
        if bindings.get('_reuse_contract') and bindings['_reuse_contract']!=current:
            raise ValueError('Execution implementation changed; replan instead of rebinding')
        change(store,w['id'],s['id'],bindings={**bindings,'_reuse_contract':current})


def record_success(store,db,wid,sid):
    """Called in the same fenced transaction as success, including human review."""
    row=db.execute('''SELECT s.*,w.project,w.plan_id,w.run_id FROM agent_steps s
        JOIN agent_workflows w ON w.id=s.workflow WHERE s.workflow=? AND s.id=?''',(wid,sid)).fetchone()
    spec=json.loads(row['spec']);bindings=json.loads(row['bindings']);quality=json.loads(row['quality'])
    if not eligible(spec):return
    if spec.get('reuse_from'):
        source=source_receipt(store,row['project'],spec['reuse_from'])
        if bindings.get('_reuse',{}).get('receipt_digest')!=digest(source):raise ValueError('Reuse source binding changed')
    if not quality.get('technical_valid') or not quality.get('passed') or quality.get('requires_agent') or quality.get('requires_review'):
        return
    environment=bindings.get('_environment')
    execution_contract=bindings.get('_reuse_contract')
    if not environment or environment=='unbuilt' or not execution_contract:return
    existing=db.execute('SELECT digest FROM workflow_reuse_receipts WHERE workflow=? AND step=? AND revision=?',
                        (wid,sid,row['revision'])).fetchone()
    if existing:return
    checkpoints=db.execute('SELECT * FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=? AND project=?',
                          (wid,sid,row['revision'],row['project'])).fetchone()
    if not checkpoints:return
    files=json.loads(row['result']);manifest=json.loads(checkpoints['manifest'])
    verified={f['name']:f for f in quality.get('files',[])}
    if not files or any(f not in manifest or f not in verified or manifest[f]['sha256']!=verified[f].get('sha256') for f in files):return
    try:inputs=input_signature(spec,bindings)
    except ValueError:return
    receipt={'version':1,'project':row['project'],'plan_id':row['plan_id'],'run_id':row['run_id'],
             'workflow':wid,'step':sid,'revision':row['revision'],'attempt':checkpoints['attempt'],
             'specification':specification(spec),'environment':environment,'server_contract':execution_contract,'inputs':inputs,
             'files':files,'manifest':{f:manifest[f] for f in files},
             'output_roles':quality.get('output_roles',{}),'acceptance':spec['acceptance'],
             'reused_from':bindings.get('_reuse')}
    receipt['execution_digest']=digest({'specification':receipt['specification'],'environment':environment,'inputs':inputs})
    if store.shared_files:
        for index,f in enumerate(files):
            store.artifacts.publish(row['project'],f'reuse-receipts/{wid}/{sid}/{row["revision"]}/{index}',manifest[f]['artifact'])
    db.execute('INSERT INTO workflow_reuse_receipts VALUES (?,?,?,?,?,?,?)',
               (wid,sid,row['revision'],row['project'],canonical(receipt),digest(receipt),store.now()))


def materialize(store,w,s,env,cancelled):
    """Copy pinned outputs to this attempt, checkpoint, then run normal acceptance."""
    from .task_leases import CURRENT
    from .workflow_inputs import resolve
    from .runtime_image import fingerprint
    from .artifact_store import logical_path
    from .operation_result import ProducedFiles
    from .workflows import change
    lease=CURRENT.get()
    if not lease or not env.get('runtime'):raise ValueError('Reuse requires an isolated execution lease')
    def check():
        lease.check()
        if cancelled():raise ValueError('Reuse cancelled')
    check()
    receipt=source_receipt(store,w['project'],s['spec']['reuse_from'])
    if receipt['workflow']==w['id']:raise ValueError('Reuse must reference a previous workflow')
    compare_spec(s['spec'],receipt)
    environment=fingerprint()
    if environment=='unbuilt' or receipt['environment']!=environment:raise ValueError('Reuse runtime environment changed')
    if receipt.get('server_contract')!=server_contract():raise ValueError('Reuse server implementation changed')
    inputs=resolve(store,w,s,env)
    expected=digest({'specification':specification(s['spec']),'environment':environment,
                     'inputs':input_signature(s['spec'],inputs)})
    if expected!=receipt['execution_digest']:raise ValueError('Reuse input content or metadata changed')
    manifest=receipt['manifest'];workspace=env['workspace'];runtime=env['runtime']
    total=sum(r['size'] for r in manifest.values())
    existing=sum(p.stat().st_size for p in workspace.rglob('*') if p.is_file() and not p.is_symlink())
    if existing+total>runtime.limit:raise ValueError('Reuse exceeds workspace byte limit')
    output=f'edit/workflows/{w["id"]}/{s["id"]}/reuse-{uuid.uuid4().hex}'
    parent=workspace
    for part in Path(output).parts:
        parent=parent/part
        if parent.is_symlink():raise ValueError('Invalid reuse output directory')
        parent.mkdir(exist_ok=True,mode=0o700);os.chown(parent,env['uid'],env['gid'])
    mapped={}
    for old,record in manifest.items():
        check();logical_path(old)
        if not old.startswith(f'edit/workflows/{receipt["workflow"]}/{receipt["step"]}/'):
            raise ValueError('Reuse output escaped source step')
        target=parent/Path(old).name
        if target.exists():raise ValueError('Reuse outputs have duplicate filenames')
        if store.shared_files:
            source=store.artifacts.materialize(w['project'],record['artifact'])
        else:
            logical_path(record['location']);source=store.root/record['location']
            if source.is_symlink() or not source.resolve().is_relative_to(store.project_dir(w['project']).resolve()):
                raise ValueError('Invalid local reuse checkpoint')
        sha=hashlib.sha256();size=0
        fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        import stat
        with os.fdopen(fd,'rb') as src,target.open('xb') as dst:
            info=os.fstat(src.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Reuse source is not a private regular file')
            while chunk:=src.read(1024*1024):
                check();size+=len(chunk)
                if size>record['size']:raise ValueError('Reuse output exceeds recorded size')
                sha.update(chunk);dst.write(chunk)
        if size!=record['size'] or sha.hexdigest()!=record['sha256']:raise ValueError('Reuse output integrity mismatch')
        os.chown(target,env['uid'],env['gid']);target.chmod(0o600)
        mapped[old]=str(target.relative_to(workspace))
    files=ProducedFiles([mapped[f] for f in receipt['files']],{role:mapped[path] for role,path in receipt['output_roles'].items()})
    # No network/file transfers inside the control transaction. Persisting the
    # candidate checkpoint first makes a crash safe to retry without generation.
    runtime.checkpoint();check()
    with store.connect() as db:
        lease.validate(db)
        live=db.execute('SELECT revision,status,bindings FROM agent_steps WHERE workflow=? AND id=?',(w['id'],s['id'])).fetchone()
        if live['revision']!=s['revision'] or live['status']!='running':raise ValueError('Reuse target revision changed')
        if digest(source_receipt(store,w['project'],s['spec']['reuse_from']))!=digest(receipt):raise ValueError('Reuse source changed')
        bindings=json.loads(live['bindings'])
        provenance={**s['spec']['reuse_from'],'attempt':receipt['attempt'],'plan_id':receipt['plan_id'],
                    'receipt_digest':digest(receipt),'execution_digest':expected}
        change(store,w['id'],s['id'],bindings={**bindings,'_reuse':provenance,'_selected_roles':files.roles},
               candidate=list(files),status='verifying')
    return files
