"""Read-only, immutable candidate references; never an acceptance transition."""
import json
from .operations import RepairCandidateRef


def lookup(store,run,raw):
    ref=RepairCandidateRef.model_validate(raw)
    revisions=store.query('SELECT spec_json FROM workflow_task_revisions WHERE run_id=? AND task_id=? AND revision=?',
        (run['id'],ref.logical_task_id,ref.revision))
    if not revisions:raise ValueError('Repair candidate is outside this Run and revision history')
    from .workflow_graph import Task
    from .operations import get
    spec=Task.model_validate(json.loads(revisions[0]['spec_json']))
    roles=get(spec.operation,spec.implementation).outputs if spec.type=='operation' else spec.output_roles
    if ref.role not in roles:raise ValueError('Repair candidate role is outside its original contract')
    rows=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=? AND project=?',
        (run['id'],ref.logical_task_id,ref.revision,run['project_id']))
    manifest=json.loads(rows[0]['manifest']) if rows else {}
    matches=[(path,record) for path,record in manifest.items() if record.get('artifact')==ref.artifact_id and record.get('sha256')==ref.sha256]
    if len(matches)!=1:raise ValueError('Repair candidate object or content hash does not match its checkpoint')
    path,record=matches[0]
    results=store.query("SELECT result_json FROM task_results WHERE run_id=? AND task_id=? AND task_revision=? AND outcome='succeeded' AND status='committed' ORDER BY created_at DESC LIMIT 32",
        (run['id'],ref.logical_task_id,ref.revision))
    for row in results:
        result=json.loads(row['result_json']);quality=result.get('quality',{})
        if not quality.get('technical_valid') or result.get('output_roles',{}).get(ref.role)!=path:continue
        metadata=next((item for item in quality.get('files',[]) if item['name']==path and item.get('sha256')==ref.sha256),None)
        if metadata:
            obj=store.artifacts.record(run['project_id'],ref.artifact_id)
            if obj['sha256']!=ref.sha256 or obj['size']!=record['size']:raise ValueError('Repair candidate object metadata changed')
            return {'path':path,'record':record,'metadata':metadata,'reference':ref.model_dump()}
    raise ValueError('Repair-only reads require a program-verified candidate, not an unverified partial file')


def descriptors(store,run):
    result=[]
    for task in run['tasks']:
        if not task['quality'].get('technical_valid'):continue
        rows=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
            (run['id'],task['id'],task['revision']))
        manifest=json.loads(rows[0]['manifest']) if rows else {}
        for role,path in task['quality'].get('output_roles',{}).items():
            record=manifest.get(path)
            if record and record.get('artifact') and path in task['candidate_result']:
                result.append({'logical_task_id':task['id'],'revision':task['revision'],'role':role,
                    'artifact_id':record['artifact'],'sha256':record['sha256']})
    return result


def source_assets(store,run,inputs,specs,*,plan_version=None,trail=()):
    """Resolve data lineage, including pinned historical candidate revisions."""
    result=set()
    if len(trail)>256:raise ValueError('Repair input lineage exceeds the graph bound')
    for ref in inputs.values():
        ref=ref.model_dump() if hasattr(ref,'model_dump') else ref
        if ref.get('asset'):result.add(ref['asset']);continue
        candidate=ref.get('repair_candidate')
        if candidate:
            key=(candidate['logical_task_id'],candidate['revision'])
            rows=store.query('SELECT spec_json,created_graph_version FROM workflow_task_revisions WHERE run_id=? AND task_id=? AND revision=?',(run['id'],*key))
            if not rows:raise ValueError('Candidate lineage revision is missing')
            spec=json.loads(rows[0]['spec_json']);version=rows[0]['created_graph_version']
        elif plan_version is not None:
            selected=store.query('SELECT task_revision FROM workflow_plan_tasks WHERE run_id=? AND graph_version=? AND task_id=?',(run['id'],plan_version,ref['step']))
            if not selected:raise ValueError('Historical input lineage is missing')
            key=(ref['step'],selected[0]['task_revision'])
            rows=store.query('SELECT spec_json,created_graph_version FROM workflow_task_revisions WHERE run_id=? AND task_id=? AND revision=?',(run['id'],*key))
            if not rows:raise ValueError('Historical input revision is missing')
            spec=json.loads(rows[0]['spec_json']);version=rows[0]['created_graph_version']
        else:
            key=(ref['step'],'current');item=specs.get(ref['step'])
            if item is None:raise ValueError('Unknown derived repair input')
            spec=item.model_dump() if hasattr(item,'model_dump') else item;version=None
        if key in trail:raise ValueError('Repair input lineage cycle')
        result.update(source_assets(store,run,spec['inputs'],specs,plan_version=version,trail=(*trail,key)))
    return result
