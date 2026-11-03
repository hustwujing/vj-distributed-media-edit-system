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
    store.execute("UPDATE plan_readiness SET state='stale',reason=?,updated=? WHERE project=? AND turn_id<>? AND state IN ('pending','ready','unavailable','uncertain','needs_information')",
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
    # An interrupted plan can retain its declared intent without ever reaching
    # the reviewer. Derive the public state for old records as well as new ones.
    if state=='pending' and turn['status'] in ('failed','cancelled'):
        state='unavailable';reason='本轮已结束，方案检查未完成，请重新讨论'
    intent=store.query('SELECT digest FROM execution_intents WHERE turn_id=?',(task,))
    if state in ('ready','uncertain','unavailable','needs_information') and (not intent or intent[0]['digest']!=row['digest'] or fingerprint(store,row['project'])!=row['fingerprint']):
        state='stale';reason='方案或项目内容已变化，请重新讨论方案'
    return {'state':state,'can_execute':state=='ready' and turn['status']=='succeeded',
            'plan_id':task,'digest':row['digest'],'reason':reason,'retry_available':state in ('unavailable','uncertain','needs_information') and turn['status']=='succeeded' and retry_count(store,task)<2}


def candidate(store,project,before=None):
    params=[project]
    clause=''
    if before:
        clause=' AND t.created<(SELECT created FROM agent_turns WHERE id=? AND project=?)';params.extend([before,project])
    rows=store.query("SELECT p.turn_id,p.state FROM plan_readiness p JOIN agent_turns t ON t.id=p.turn_id WHERE p.project=?"+clause+" ORDER BY t.created DESC LIMIT 1",tuple(params))
    if rows and rows[0]['state']!='consumed':
        return rows[0]['turn_id']
    return None


def current(store,project):
    task=candidate(store,project)
    return task if task and public(store,task)['can_execute'] else None


def handle_confirmation(host,row):
    """Resolve against an existing proposal; uncertain review never consumes consent."""
    from . import agent_store
    from .semantic_review import classify,SemanticUnavailable
    store=host.store;task=row['id']
    target=candidate(store,row['project'],before=task)
    if not target:return False,None
    plan=agent_store.turn(store,target)
    if not selection_matches(plan,row):
        invalidate(store,row['project'],'所选素材或画布目标已变化');return False,None
    try:
        decision=classify(store,task,'confirmation',{'message':row['prompt'],'proposal':plan['text'][:14000],
                         'original_request':plan['prompt'][:8000],'intent':plan['execution_intent'],
                         'proposal_readiness':plan['plan_readiness']['state'],'selected_assets':plan.get('attachments',[]),
                         'canvas_target':{k:plan['canvas_context'].get(k) for k in ('revision','layer_id')} if plan.get('canvas_context') else None})
    except SemanticUnavailable:
        state=public(store,target)
        message='暂时无法明确判断这条消息是否确认原方案，本轮没有执行。'
        message+=('可点击已就绪方案的确认按钮，或说明修改要求。' if state['can_execute'] else '原方案尚未通过检查，请先查看方案检查状态；也可说明修改要求。')
        # This failed classification does not establish a safe retry association.
        agent_store.finish(store,task,'succeeded',message);return True,target
    if decision=='confirm':
        store.execute('UPDATE agent_routes SET candidate=? WHERE task_id=?',(target,task))
        state=public(store,target)
        if state['can_execute']:
            promote_confirmation(store,task,target)
            host.notify(task,'progress',{'text':'已确认当前方案，正在安排执行。'})
        else:
            message='本次确认尚未生效，本轮没有执行。'+state.get('reason','原方案检查尚未完成。')
            message+=(' 请在原方案卡片点击“重新检查方案”；检查通过后需要再次确认。' if state.get('retry_available') else ' 请先核对原方案状态，必要时补充需求或重新讨论。')
            agent_store.finish(store,task,'succeeded',message)
        return True,target
    if decision in ('revise','reject'):
        invalidate(store,row['project'],'用户修改或撤回了方案')
    return False,target


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
    from .plan_review_context import build,digest
    context=build(store,task,text,intent)
    evidence={}
    try:
        answer=classify(store,task,'plan',context)
        evidence=getattr(answer,'evidence',{})
        # A categorical "missing" answer alone cannot tell users what to supply.
        if answer=='needs_information' and not evidence.get('missing_items'):
            from .semantic_review import secondary
            answer=secondary(store,task,context,evidence)
            evidence=getattr(answer,'evidence',{})
        state={'ready':'ready','needs_information':'needs_information','discussion':'discussion'}.get(answer,'uncertain')
        reason='' if state=='ready' else ('请补充：'+'；'.join(evidence['missing_items']) if state=='needs_information' else '本轮尚未形成可供确认的执行方案')
    except SemanticUncertain as exc:
        state='uncertain';reason='方案审核暂不能确定，请重新检查；当前没有已确认的缺失项'
        evidence=exc.evidence
    except SemanticUnavailable as exc:evidence=exc.evidence
    started_fingerprint=row['fingerprint']
    current_fingerprint=fingerprint(store,row['project'])
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        lease=CURRENT.get()
        if lease:lease.validate(db)
        status=db.execute('SELECT status FROM agent_turns WHERE id=?',(task,)).fetchone()
        if not status or status['status'] not in ('running','succeeded'):return
        newer=db.execute('SELECT t.id,r.candidate FROM agent_turns t LEFT JOIN agent_routes r ON r.task_id=t.id WHERE t.project=? AND t.created>(SELECT created FROM agent_turns WHERE id=?)',(row['project'],task)).fetchall()
        if any(r['candidate']!=task for r in newer):
            state='stale';reason='审核期间有新的需求或对话，请重新核对方案'
        if current_fingerprint!=started_fingerprint or fingerprint(store,row['project'])!=started_fingerprint:
            state='stale';reason='审核期间项目内容已变化'
        applied=db.execute("UPDATE plan_readiness SET state=?,reason=?,updated=? WHERE turn_id=? AND digest=? AND updated=? AND state IN ('pending','unavailable')",
                          (state,reason,store.now(),task,row['digest'],row['updated'])).rowcount
        if applied:
            payload={'state':state,'reason':reason,'digest':row['digest'],'input_digest':digest(context),'coverage':context['coverage'],
                     'saved_file_count':len(context['saved_files']),'history_tasks':[r['task_id'] for r in context['prior_conversation']],'evidence':evidence}
            db.execute('INSERT INTO agent_events(turn_id,kind,payload,created) VALUES (?,?,?,?)',(task,'plan_review',json.dumps(payload,ensure_ascii=False),store.now()))


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
        if not row or row['status']!='succeeded' or not ready or ready['state'] not in ('unavailable','uncertain','needs_information') or ready['digest']!=digest:
            raise ValueError('当前方案不支持重试检查')
        if fingerprint(store,row['project'])!=ready['fingerprint']:raise ValueError('项目内容已变化，请重新讨论方案')
        if candidate(store,row['project'])!=task:raise ValueError('已有更新的方案，请使用最新方案')
        newer=db.execute('SELECT t.id,r.candidate FROM agent_turns t LEFT JOIN agent_routes r ON r.task_id=t.id WHERE t.project=? AND t.created>?',(row['project'],row['created'])).fetchall()
        if any(r['candidate']!=task for r in newer):raise ValueError('已有更新的对话，请先核对最新需求')
        if retry_count(store,task)>=2:raise ValueError('方案检查已达到两次重试上限，请稍后重新讨论')
        db.execute('INSERT INTO plan_review_retries VALUES (?,1) ON CONFLICT(turn_id) DO UPDATE SET used=plan_review_retries.used+1',(task,))
        db.execute("UPDATE plan_readiness SET state='pending',reason='',updated=? WHERE turn_id=?",(store.now(),task))
    review(store,task,row['text'])
