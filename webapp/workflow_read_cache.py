"""Durable, snapshot-bound Planner read results; never an authorization source."""
import json
from . import workflow_graph as graph


MAX_REQUEST_BYTES=16*1024
MAX_RESULT_BYTES=1024*1024


def query_digest(kind,request):
    return graph.digest({'kind':kind,'request':request})


def list_cached(store,run_id,task_id,snapshot_digest):
    rows=store.query('''SELECT kind,query_digest,request_json,result_json,source_attempt,created_at
        FROM workflow_read_cache WHERE run_id=? AND task_id=? AND snapshot_digest=?
        ORDER BY created_at,kind,query_digest''',(run_id,task_id,snapshot_digest))
    result=[]
    for row in rows:
        try:request=json.loads(row.pop('request_json'));value=json.loads(row.pop('result_json'))
        except (TypeError,ValueError):continue
        result.append({**row,'request':request,'result':value})
    return result


def put(store,lease,run_id,task_id,snapshot_digest,kind,request,result):
    if kind not in ('contract','inspection'):raise ValueError('Unsupported Planner read cache kind')
    request_json=graph.encode(request);result_json=graph.encode(result)
    if len(request_json.encode())>MAX_REQUEST_BYTES or len(result_json.encode())>MAX_RESULT_BYTES:
        return False
    digest=query_digest(kind,request);now=store.now()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');lease.validate(db)
        db.execute('''INSERT INTO workflow_read_cache(
            run_id,task_id,snapshot_digest,kind,query_digest,request_json,result_json,source_attempt,created_at)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,task_id,snapshot_digest,kind,query_digest) DO NOTHING''',
            (run_id,task_id,snapshot_digest,kind,digest,request_json,result_json,lease.attempt,now))
    return True
