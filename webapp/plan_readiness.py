"""Execution requires an audited proposal and an atomic, version-bound consent."""
import hashlib
import json
from .task_leases import CURRENT


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS plan_readiness(
      turn_id TEXT PRIMARY KEY,project TEXT NOT NULL,digest TEXT NOT NULL,fingerprint TEXT NOT NULL,
      state TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',consumed_by TEXT NOT NULL DEFAULT '',updated REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS plan_readiness_project ON plan_readiness(project,state);
      CREATE TABLE IF NOT EXISTS plan_review_retries(turn_id TEXT PRIMARY KEY,used INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS agent_routes(
      task_id TEXT PRIMARY KEY,stage TEXT NOT NULL,capability TEXT NOT NULL DEFAULT '',
      reason TEXT NOT NULL DEFAULT '',candidate TEXT NOT NULL DEFAULT '',context TEXT NOT NULL DEFAULT '[]',updated REAL NOT NULL);
    ''')


def fingerprint(store,project):
    assets=store.query('SELECT id,filename,metadata FROM assets WHERE project=? ORDER BY id',(project,))
    canvas=store.query('SELECT document FROM image_documents WHERE project=?',(project,)) if store.query("SELECT name FROM sqlite_master WHERE name='image_documents'") else []
    from .workspace_state import proposal_content
    workspace=proposal_content(store,project)
    return hashlib.sha256(json.dumps([assets,canvas,workspace],sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def invalidate(store,project,reason,except_turn=''):
    store.execute("UPDATE plan_readiness SET state='stale',reason=?,updated=? WHERE project=? AND turn_id<>? AND state IN ('pending','ready','unavailable')",
                  (reason,store.now(),project,except_turn))


def declared(store,task,digest):
    project=store.query('SELECT project FROM agent_turns WHERE id=?',(task,))[0]['project']
    invalidate(store,project,'已有更新的方案',task)
    store.execute('''INSERT INTO plan_readiness(turn_id,project,digest,fingerprint,state,updated) VALUES (?,?,?,?,'pending',?)
      ON CONFLICT(turn_id) DO UPDATE SET digest=excluded.digest,fingerprint=excluded.fingerprint,state='pending',reason='',consumed_by='',updated=excluded.updated''',
      (task,project,digest,fingerprint(store,project),store.now()))


def public(store,task):
    rows=store.query('SELECT * FROM plan_readiness WHERE turn_id=?',(task,))
    if not rows:return {'state':'none','can_execute':False}
    row=rows[0];state=row['state'];reason=row['reason']
    turn=store.query('SELECT status FROM agent_turns WHERE id=?',(task,))[0]
    intent=store.query('SELECT digest FROM execution_intents WHERE turn_id=?',(task,))
    if state=='ready' and (not intent or intent[0]['digest']!=row['digest'] or fingerprint(store,row['project'])!=row['fingerprint']):
        state='stale';reason='方案或项目内容已变化，请重新讨论方案'
    return {'state':state,'can_execute':state=='ready' and turn['status']=='succeeded',
            'plan_id':task,'digest':row['digest'],'reason':reason,'retry_available':state=='unavailable' and turn['status']=='succeeded' and retry_count(store,task)<2}


def current(store,project):
    rows=store.query("SELECT turn_id FROM plan_readiness WHERE project=? AND state='ready' ORDER BY updated DESC LIMIT 1",(project,))
    if rows and public(store,rows[0]['turn_id'])['can_execute']:return rows[0]['turn_id']
    return None


def require(store,project,task,digest=None):
    rows=store.query('SELECT project,mode FROM agent_turns WHERE id=?',(task,))
    if not rows or rows[0]!={'project':project,'mode':'plan'}:raise ValueError('请先形成并确认当前项目的可执行方案')
    value=public(store,task)
    if not value['can_execute'] or (digest is not None and digest!=value['digest']):
        raise ValueError('当前方案尚未就绪、已执行或已变化，请重新讨论并确认')
    return value


def consume(store,project,plan,task,digest=None):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');require(store,project,plan,digest)
        if not db.execute("UPDATE plan_readiness SET state='consumed',consumed_by=?,updated=? WHERE turn_id=? AND state='ready'",
                          (task,store.now(),plan)).rowcount:raise ValueError('该方案已经确认过')


def review(store,task,text):
    from .execution_intent import get
    intent=get(store,task)
    if not intent:return
    rows=store.query('SELECT * FROM plan_readiness WHERE turn_id=?',(task,))
    if not rows:return
    row=rows[0]
    from .semantic_review import classify,SemanticUnavailable,SemanticUncertain
    state='unavailable';reason='方案检查暂不可用，请重新核对方案后再执行'
    try:
        answer=classify(store,task,'plan',{
            'user_request':store.query('SELECT prompt FROM agent_turns WHERE id=?',(task,))[0]['prompt'],
            'proposal':text[:16000],'intent':intent,
            'available_results':[a.get('name') for r in store.query("SELECT artifacts FROM agent_turns WHERE project=? AND status='succeeded' ORDER BY created DESC LIMIT 6",(row['project'],)) for a in json.loads(r['artifacts']) if a.get('quality_state') not in ('pending','superseded')][:80],
            'assets':[{'id':a['id'],'name':a['name'],**{k:v for k,v in a['metadata'].items() if k in ('kind','width','height','duration')}} for a in store.assets(row['project'])][:100]})
        state={'ready':'ready','needs_information':'needs_information','discussion':'discussion'}.get(answer,'needs_information')
        reason='' if state=='ready' else '尚需补充明确的目标、输入、关键参数或交付内容'
    except SemanticUncertain:
        state='needs_information';reason='方案是否完整尚不明确，请补充输入、参数或交付内容'
    except SemanticUnavailable:pass
    if fingerprint(store,row['project'])!=row['fingerprint']:state='stale';reason='审核期间项目内容已变化'
    store.execute('UPDATE plan_readiness SET state=?,reason=?,updated=? WHERE turn_id=? AND digest=? AND state IN (\'pending\',\'unavailable\')',
                  (state,reason,store.now(),task,row['digest']))


def register(store,task,stage='light'):
    store.execute('INSERT OR IGNORE INTO agent_routes(task_id,stage,updated) VALUES (?,?,?)',(task,stage,store.now()))


def route(store,task):
    rows=store.query('SELECT * FROM agent_routes WHERE task_id=?',(task,))
    return rows[0] if rows else None


def is_light(store,task):
    row=route(store,task)
    return bool(row and row['stage']=='light')



def selection_matches(plan,request):
    requested={a['id'] for a in request.get('attachments',[])}
    original={a['id'] for a in plan.get('attachments',[])}
    if requested and requested!=original:return False
    selected=request.get('canvas_context')
    expected=plan.get('canvas_context')
    if selected and (not expected or any(selected.get(k)!=expected.get(k) for k in ('revision','layer_id'))):return False
    return True


def promote_confirmation(store,task,plan):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT project,mode,status,attachments,canvas_context FROM agent_turns WHERE id=?',(task,)).fetchone()
        if not row or row['mode']!='plan' or row['status']!='running':raise ValueError('确认消息已失效')
        approved=db.execute('SELECT canvas_context,attachments FROM agent_turns WHERE id=?',(plan,)).fetchone()
        if not approved or not selection_matches({k:json.loads(approved[k]) for k in ('attachments','canvas_context')},{k:json.loads(row[k]) for k in ('attachments','canvas_context')}):raise ValueError('确认时选择的素材或画布目标已变化，请重新讨论')
        consume(store,row['project'],plan,task)
        db.execute("UPDATE agent_turns SET mode='execute',plan_id=?,canvas_context=?,status='queued',text='',updated=? WHERE id=?",
                   (plan,approved['canvas_context'],store.now(),task))
        ancestry=db.execute('SELECT run_id FROM usage_tasks WHERE task_id=?',(plan,)).fetchone()
        db.execute('UPDATE usage_tasks SET run_id=?,parent_task_id=? WHERE task_id=?',(ancestry['run_id'],plan,task))
        db.execute("UPDATE agent_routes SET stage='heavy',capability='confirmed_execution',candidate=?,updated=? WHERE task_id=?",(plan,store.now(),task))


def retry_count(store,task):
    rows=store.query('SELECT used FROM plan_review_retries WHERE turn_id=?',(task,))
    return rows[0]['used'] if rows else 0


def retry_review(store,task,digest):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM agent_turns WHERE id=?',(task,)).fetchone()
        ready=db.execute('SELECT * FROM plan_readiness WHERE turn_id=?',(task,)).fetchone()
        if not row or row['status']!='succeeded' or not ready or ready['state']!='unavailable' or ready['digest']!=digest:
            raise ValueError('当前方案不支持重试检查')
        if fingerprint(store,row['project'])!=ready['fingerprint']:raise ValueError('项目内容已变化，请重新讨论方案')
        if db.execute('SELECT id FROM agent_turns WHERE project=? AND created>? LIMIT 1',(row['project'],row['created'])).fetchone():raise ValueError('已有更新的对话，请使用最新方案')
        if retry_count(store,task)>=2:raise ValueError('方案检查已达到两次重试上限，请稍后重新讨论')
        db.execute('INSERT INTO plan_review_retries VALUES (?,1) ON CONFLICT(turn_id) DO UPDATE SET used=plan_review_retries.used+1',(task,))
        db.execute("UPDATE plan_readiness SET state='pending',reason='',updated=? WHERE turn_id=?",(store.now(),task))
    review(store,task,row['text'])
