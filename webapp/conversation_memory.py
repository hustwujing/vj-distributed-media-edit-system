"""Durable Session conversation memory with non-destructive compaction.

`session_messages` is the append-only ledger. Summaries are derived indexes:
they never authorize work and can always be rebuilt from the raw ledger.
"""
import json
import os
import unicodedata
from . import workflow_graph as graph

DEFAULT_COMPACT_TOKENS=12000
DEFAULT_TAIL_TOKENS=4000
MAX_DIGEST_MESSAGES=40
MAX_DIGEST_CHARS=240


def estimate_tokens(value):
    """Conservative tokenizer-free estimate suitable for routing thresholds."""
    if not isinstance(value,str):value=graph.encode(value)
    tokens=0;ascii_run=0
    for char in value:
        if ord(char)<128 and (char.isalnum() or char in "_-'\""):
            ascii_run+=1;continue
        if ascii_run:tokens+=(ascii_run+3)//4;ascii_run=0
        if char.isspace():continue
        category=unicodedata.category(char)
        tokens+=1 if category[0] in ('L','N','S') else 1
    if ascii_run:tokens+=(ascii_run+3)//4
    return max(1,tokens)


def _limits():
    threshold=max(2000,int(os.environ.get('VIDEO_USE_CONVERSATION_COMPACT_TOKENS',DEFAULT_COMPACT_TOKENS)))
    tail=max(500,int(os.environ.get('VIDEO_USE_CONVERSATION_TAIL_TOKENS',DEFAULT_TAIL_TOKENS)))
    return threshold,min(tail,threshold//2)


def _rows(store,session_id,through=None):
    sql='SELECT id,run_id,role,content,created_at FROM session_messages WHERE session_id=?'
    args=[session_id]
    if through:
        sql+=' AND (created_at<? OR (created_at=? AND id<=?))'
        args.extend((through[0],through[0],through[1]))
    return store.query(sql+' ORDER BY created_at,id',tuple(args))


def _source_digest(rows):
    return graph.digest([{key:row.get(key) for key in ('id','run_id','role','content','created_at')} for row in rows])


def schedule(store,session_id,run_id):
    """Schedule compaction without putting it on the user request path."""
    rows=_rows(store,session_id)
    current=store.query('SELECT through_created_at,through_message_id,source_digest,summary_json FROM conversation_summaries WHERE session_id=?',(session_id,))
    cursor=(current[0]['through_created_at'],current[0]['through_message_id']) if current else None
    summary_tokens=estimate_tokens(json.loads(current[0]['summary_json']).get('summary_message','')) if current else 0
    unsummarized=[row for row in rows if not cursor or row['created_at']>cursor[0] or row['created_at']==cursor[0] and row['id']>cursor[1]]
    threshold,tail_tokens=_limits()
    if summary_tokens+sum(estimate_tokens(row['content']) for row in unsummarized)<threshold:return False
    remaining=0;target=None
    for row in reversed(unsummarized):
        size=estimate_tokens(row['content'])
        if remaining+size>tail_tokens:
            target=row;break
        remaining+=size
    if target is None or cursor and target['created_at']==cursor[0] and target['id']==cursor[1]:return False
    old=[row for row in rows if row['created_at']<target['created_at'] or row['created_at']==target['created_at'] and row['id']<=target['id']]
    digest=_source_digest(old)
    if current and current[0]['through_created_at']==target['created_at'] and current[0]['through_message_id']==target['id'] and current[0]['source_digest']==digest:
        return False
    now=store.now()
    store.execute('''INSERT INTO conversation_summary_jobs(session_id,run_id,target_created_at,target_message_id,
        source_digest,status,attempts,next_run_at,error_code,created_at,updated_at)
        VALUES (?,?,?,?,?,'pending',0,?,'',?,?)
        ON CONFLICT(session_id) DO UPDATE SET run_id=excluded.run_id,target_created_at=excluded.target_created_at,
        target_message_id=excluded.target_message_id,source_digest=excluded.source_digest,status='pending',
        attempts=0,next_run_at=excluded.next_run_at,error_code='',updated_at=excluded.updated_at''',
        (session_id,run_id,target['created_at'],target['id'],digest,now,now,now))
    return True


def message_recorded(store,run):
    try:schedule(store,run['session_id'],run['id'])
    except (KeyError,TypeError,ValueError):
        # Raw ledger writes are authoritative; a derived summary can wait.
        return False
    return True


def _authoritative(store,run):
    runs=store.query('SELECT id,goal,status,terminal_reason,delivery_json,updated_at FROM runs WHERE session_id=? ORDER BY created_at,id',(run['session_id'],))
    completed=[];failures=[]
    for item in runs:
        delivery=json.loads(item.pop('delivery_json'))
        fact={'run_id':item['id'],'goal':item['goal'],'state':item['status'],
              'deliveries':[{key:value.get(key) for key in ('name','file','type','size','sha256')} for value in delivery[:20]]}
        if item['status'] in ('SUCCEEDED','FAILED','CANCELLED'):completed.append(fact)
        if item['terminal_reason'] and item['status'] in ('WAITING_USER','FAILED','CANCELLED'):
            failures.append({'run_id':item['id'],'state':item['status'],'code':item['terminal_reason']})
    pending=[]
    for row in store.query("SELECT id,run_id,kind,request_json FROM workflow_decisions WHERE run_id IN (SELECT id FROM runs WHERE session_id=?) AND status='pending' ORDER BY created_at",(run['session_id'],)):
        request=json.loads(row.pop('request_json'))
        pending.append({**row,'question':request.get('question') or request.get('message') or '',
                        'required_fields':request.get('required_fields',[])})
    authorization=run['authorization']
    return {'current_goal':run['goal'],
        'confirmed_constraints':{'delivery_targets':authorization.get('delivery_targets',[]),
            'delivery_contract':authorization.get('delivery_contract',{}),'review_policy':authorization.get('review_policy',{})},
        'completed_facts':completed[-12:],'failures':failures[-12:],'pending_questions':pending,
        'preferences':[],'latest_recovery_intent':'','authoritative_at':store.now()}


def process_pending(store,session_id):
    """Build one queued summary. This is called by reconciliation, not HTTP input."""
    jobs=store.query("SELECT * FROM conversation_summary_jobs WHERE session_id=? AND status IN ('pending','failed') AND next_run_at<=?",(session_id,store.now()))
    if not jobs:return False
    job=jobs[0];through=(job['target_created_at'],job['target_message_id'])
    try:
        rows=_rows(store,session_id,through)
        if _source_digest(rows)!=job['source_digest']:raise ValueError('conversation_ledger_changed')
        run_rows=store.query('SELECT id FROM runs WHERE id=? AND session_id=?',(job['run_id'],session_id))
        if not run_rows:raise ValueError('summary_run_missing')
        run=graph.get(store,job['run_id'])
        facts=_authoritative(store,run)
        previous=store.query("SELECT summary_json,through_created_at,through_message_id FROM conversation_summaries WHERE session_id=? AND status='committed'",(session_id,))
        prior=json.loads(previous[0]['summary_json']) if previous else {}
        prior_digest=prior.get('history_digest',[])
        prior_cursor=(previous[0]['through_created_at'],previous[0]['through_message_id']) if previous else None
        delta=[row for row in rows if not prior_cursor or row['created_at']>prior_cursor[0] or row['created_at']==prior_cursor[0] and row['id']>prior_cursor[1]]
        digest_rows=(prior_digest+[{'message_id':row['id'],'run_id':row['run_id'],'role':row['role'],
                    'content':row['content'][:MAX_DIGEST_CHARS],'created_at':row['created_at']} for row in delta])[-MAX_DIGEST_MESSAGES:]
        summary_text='\n'.join(('用户：' if item['role']=='user' else '系统：')+item['content'] for item in digest_rows)
        summary_budget=_limits()[1]
        while len(digest_rows)>1 and estimate_tokens(summary_text)>summary_budget:
            digest_rows=digest_rows[1:]
            summary_text='\n'.join(('用户：' if item['role']=='user' else '系统：')+item['content'] for item in digest_rows)
        summary={**facts,'history_digest':digest_rows,
                 'summary_message':summary_text,'history_message_count':len(rows),
                 'history_truncated':len(rows)>len(digest_rows),
                 'estimated_tokens':estimate_tokens(summary_text),
                 'scope':'Historical context only. It never grants permission or overrides current server state.'}
        now=store.now()
        store.execute('''INSERT INTO conversation_summaries(session_id,through_created_at,through_message_id,source_digest,
            summary_json,revision,status,error_code,created_at,updated_at) VALUES (?,?,?,?,?,1,'committed','',?,?)
            ON CONFLICT(session_id) DO UPDATE SET through_created_at=excluded.through_created_at,
            through_message_id=excluded.through_message_id,source_digest=excluded.source_digest,
            summary_json=excluded.summary_json,revision=conversation_summaries.revision+1,status='committed',
            error_code='',updated_at=excluded.updated_at''',
            (session_id,job['target_created_at'],job['target_message_id'],job['source_digest'],graph.encode(summary),now,now))
        store.execute("UPDATE conversation_summary_jobs SET status='committed',updated_at=?,error_code='' WHERE session_id=? AND source_digest=?",
                      (now,session_id,job['source_digest']))
        return True
    except (KeyError,TypeError,ValueError,json.JSONDecodeError) as exc:
        attempts=job['attempts']+1;now=store.now()
        store.execute("UPDATE conversation_summary_jobs SET status='failed',attempts=?,next_run_at=?,error_code=?,updated_at=? WHERE session_id=?",
                      (attempts,now+min(300,2**min(attempts,8)),str(exc)[:200],now,session_id))
        return False


def snapshot(store,run):
    """Return compact history plus current facts with explicit precedence."""
    summaries=store.query("SELECT * FROM conversation_summaries WHERE session_id=? AND status='committed'",(run['session_id'],))
    stored=json.loads(summaries[0]['summary_json']) if summaries else {}
    summary=({'message':stored.get('summary_message',''),'history_message_count':stored.get('history_message_count',0),
              'history_truncated':stored.get('history_truncated',False),'estimated_tokens':stored.get('estimated_tokens',0),
              'revision':summaries[0]['revision'],'through_created_at':summaries[0]['through_created_at'],
              'through_message_id':summaries[0]['through_message_id']}
             if summaries else {})
    through=(summaries[0]['through_created_at'],summaries[0]['through_message_id']) if summaries else None
    rows=_rows(store,run['session_id'])
    if through:
        recent=[row for row in rows if row['created_at']>through[0] or row['created_at']==through[0] and row['id']>through[1]]
    else:recent=rows
    authoritative=_authoritative(store,run)
    return {'summary':summary,'messages_after_cursor':recent,**authoritative,
        'ledger_message_count':len(rows),'summary_pending':bool(store.query(
            "SELECT session_id FROM conversation_summary_jobs WHERE session_id=? AND status IN ('pending','failed')",(run['session_id'],))),
        'precedence':['current_server_state','current_user_message','recent_raw_messages','historical_summary'],
        'scope':'Read-only conversation context. Authorization, contracts and decisions remain server-owned.'}
