"""Project-scoped history retrieval; history is evidence, never execution consent."""
import json


def lookup(store,project,current,selection='recent',query='',before_id=''):
    if selection not in ('recent','oldest','matching') or not isinstance(query,str) or len(query)>200 or not isinstance(before_id,str) or len(before_id)>100:raise ValueError('Invalid history query')
    where="project=? AND id<>? AND status IN ('succeeded','failed','cancelled')";args=[project,current]
    if before_id:
        previous=store.query('SELECT created FROM agent_turns WHERE id=? AND project=?',(before_id,project))
        if not previous:raise ValueError('历史消息不属于当前项目')
        where+=' AND created<?';args.append(previous[0]['created'])
    if selection=='matching':where+=' AND (prompt LIKE ? OR text LIKE ?)';args.extend(['%'+query+'%','%'+query+'%'])
    order='ASC' if selection=='oldest' else 'DESC'
    rows=store.query('SELECT id,mode,status,prompt,text,error,created FROM agent_turns WHERE '+where+' ORDER BY created '+order+',id '+order+' LIMIT 5',args)
    return [{**r,'text':r['text'][:16000],'error':r['error'][:2000],'reply_truncated':len(r['text'])>16000} for r in rows]


def write_snapshot(store,project,current,path,limit=16*1024**2):
    """Read-only on-demand context for the sandbox, rebuilt from PostgreSQL."""
    size=0;after_time=-1.;after_id=''
    with path.open('w') as output:
        if not store.query("SELECT name FROM sqlite_master WHERE name='agent_turns'"):return
        while True:
            rows=store.query("SELECT id,mode,status,prompt,text,error,created FROM agent_turns WHERE project=? AND id<>? AND status IN ('succeeded','failed','cancelled') AND (created>? OR (created=? AND id>?)) ORDER BY created,id LIMIT 100",
                             (project,current or '',after_time,after_time,after_id))
            if not rows:return
            for row in rows:
                data=json.dumps(row,ensure_ascii=False)+'\n';size+=len(data.encode())
                if size>limit:
                    output.write(json.dumps({'history_truncated':True,'reason':'Historical content exceeds snapshot limit'})+'\n');return
                output.write(data)
            after_time=rows[-1]['created'];after_id=rows[-1]['id']
