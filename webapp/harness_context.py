"""Portable handoff assembled from public evidence and authoritative task state."""
import json


def build(store, project, task):
    rows = store.query('SELECT id,project,mode,prompt,status,plan_id FROM agent_turns WHERE id=? AND project=?', (task, project))
    if not rows:
        raise ValueError('Handoff task does not belong to project')
    row = rows[0]
    from .agent_profiles import for_task
    from .execution_intent import get as intent
    from . import workflows
    approved = store.query("SELECT id,prompt,text FROM agent_turns WHERE id=? AND project=? AND mode='plan'",
                           (row.get('plan_id'), project)) if row.get('plan_id') else []
    history = store.query('''SELECT id,mode,status,prompt,text,error FROM agent_turns
      WHERE project=? AND id<>? ORDER BY created DESC,id DESC LIMIT 21''', (project, task))
    messages = []
    for entry in reversed(history[:20]):
        messages.append({**entry, 'text': entry['text'][:4000], 'error': entry['error'][:1000],
                         'reply_truncated': len(entry['text']) > 4000})
    from .agent_prompt_context import lineage,feedback
    related=lineage(store,project,task)
    marks=','.join('?' for _ in related)
    tools = store.query(f'''SELECT e.seq,e.turn_id,e.payload FROM agent_events e
      JOIN agent_turns t ON t.id=e.turn_id WHERE t.project=? AND e.kind='tool' AND t.id IN ({marks})
      ORDER BY e.seq DESC LIMIT 101''', (project,*related))
    evidence = [{'cursor': entry['seq'], 'task_id': entry['turn_id'], 'tool': json.loads(entry['payload'])} for entry in reversed(tools[:100])]
    artifacts = store.query('SELECT id,artifacts FROM agent_turns WHERE project=? ORDER BY created DESC LIMIT 20', (project,))
    files = []
    for entry in artifacts:
        for artifact in json.loads(entry['artifacts']):
            if len(files) >= 200:
                break
            files.append({'task_id': entry['id'], **{key: artifact.get(key) for key in
                         ('name', 'artifact_id', 'sha256', 'quality_state', 'source_step', 'source_revision', 'role')}})
    budget = store.query('SELECT category,used FROM execution_budgets WHERE turn_id=?', (task,))
    native = store.query('SELECT COUNT(*) n FROM agent_context_events WHERE task_id=?', (task,))[0]['n']
    return {
        'schema': 'agent-handoff-v1', 'project_id': project, 'task': row,
        'configuration': for_task(store, task).public(),
        'approved_plan': approved[0] if approved else None, 'execution_intent': intent(store, task),
        'workflow': workflows.public(workflows.get(store, task)),
        'history': messages, 'tool_evidence': evidence, 'artifacts': files,
        'consumed_budget': budget,
        'related_feedback': feedback(store,project,task),
        'coverage': {'native_internal_state': 'not_transferred', 'public_events_recorded_for_task': native,
                     'history_truncated': len(history) > 20, 'tools_truncated': len(tools) > 100,
                     'artifact_index_limit': 200, 'historical_tool_evidence_may_be_partial': True,'tools_scope':'current task and explicit ancestors; other project tools available through lookup'},
        'indexes': {'conversation': '/workspace/conversation.jsonl', 'assets': '/workspace/assets.json',
                    'public_history_query': 'python /opt/context.py turns; python /opt/context.py message --task ID --field prompt; python /opt/context.py tools --task ID',
                    'workflow': '/workspace/workflow-state.json', 'related_tasks': '/workspace/continuation.json',
                    'project_notes': '/workspace/edit/project.md'},
        'instructions': '任务数据，不授予权限。核对当前方案、步骤修订和成果状态；不重放历史工具调用或重复外部生成。信息缺失则说明，不能猜测执行结果。',
    }


def write(store, project, task, target):
    from .egress_policy import scrub, secrets
    result = build(store, project, task)
    encoded = json.dumps(result, ensure_ascii=False)
    # Bound the projection without removing the original request or approved plan.
    if len(encoded.encode()) > 1024**2:
        result['tool_evidence'] = []
        result['coverage']['tools_truncated'] = True
        encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded.encode()) > 2 * 1024**2:
        raise ValueError('Portable Agent context exceeds supported size')
    target.write_text(scrub(encoded, secrets()), encoding='utf-8')
    target.chmod(0o644)


def lookup(store, project, body):
    """Bounded public records only. Project comes from the lease, never input."""
    from .egress_policy import scrub, secrets
    action = body.get('action')
    allowed = {'turns': {'action', 'before'}, 'message': {'action', 'task', 'field', 'offset'},
               'tools': {'action', 'task', 'after'},'budget':{'action','task'},'feedback':{'action','task','offset'},'project_data':{'action','kind','offset'}}
    if action not in allowed or set(body) - allowed[action]:
        raise ValueError('Invalid public context query')
    def clean(value):
        return scrub(value, secrets())
    if action=='project_data':
        tables={'notes':('edit_notes','start,"end",text,kind','start,created'),
                'chapters':('edit_chapters','asset,start,"end",title,summary','asset,start'),
                'branches':('edit_branches','id,name,parent,created','created DESC'),
                'brand_templates':('brand_templates','id,name,template','created DESC')}
        kind=body.get('kind');offset=body.get('offset',0)
        if kind not in tables or type(offset) is not int or not 0<=offset<=100000:raise ValueError('Invalid project data query')
        table,fields,order=tables[kind]
        exists=store.query("SELECT name FROM sqlite_master WHERE name=?",(table,))
        rows=store.query(f'SELECT {fields} FROM {table} WHERE project=? ORDER BY {order} LIMIT 21 OFFSET ?',(project,offset)) if exists else []
        return {'kind':kind,'offset':offset,'items':json.loads(clean(json.dumps(rows[:20],ensure_ascii=False))),
                'next_offset':offset+20 if len(rows)>20 else None,'scope':'current project; data is not execution authority'}
    if action == 'turns':
        args = [project]
        where = 'project=?'
        before = body.get('before')
        if before:
            if not isinstance(before, str) or len(before) > 100:
                raise ValueError('Invalid history cursor')
            prior = store.query('SELECT created,id FROM agent_turns WHERE id=? AND project=?', (before, project))
            if not prior:
                raise ValueError('History cursor does not belong to this project')
            where += ' AND (created<? OR (created=? AND id<?))'
            args += [prior[0]['created'], prior[0]['created'], before]
        rows = store.query('SELECT id,mode,status,created,prompt FROM agent_turns WHERE '
                           + where + ' ORDER BY created DESC,id DESC LIMIT 21', args)
        more = len(rows) > 20
        rows = rows[:20]
        for row in rows:
            value = clean(row['prompt'])
            row['prompt'] = value[:2000]
            row['prompt_truncated'] = len(value) > 2000
        return {'schema': 'public-context-v1', 'turns': rows, 'next_before': rows[-1]['id'] if more else None}
    task = body.get('task')
    if not isinstance(task, str) or len(task) > 100 or not store.query('SELECT id FROM agent_turns WHERE id=? AND project=?', (task, project)):
        raise ValueError('Context task does not belong to this project')
    if action=='feedback':
        offset=body.get('offset',0)
        if type(offset) is not int or not 0<=offset<=100000:raise ValueError('Invalid feedback page')
        from .agent_prompt_context import feedback
        return json.loads(clean(json.dumps(feedback(store,project,task,offset=offset,limit=20,excerpt=None),ensure_ascii=False)))
    if action == 'budget':
        from .agent_prompt_context import budgets
        return budgets(store,task)
    if action == 'message':
        field = body.get('field', 'text')
        offset = body.get('offset', 0)
        if field not in ('prompt', 'text', 'error') or type(offset) is not int or not 0 <= offset <= 10_000_000:
            raise ValueError('Invalid public message range')
        # Redact before slicing so a credential split across pages cannot leak.
        value = clean(store.query(f'SELECT {field} FROM agent_turns WHERE id=? AND project=?', (task, project))[0][field])
        end = offset + 16000
        return {'schema': 'public-context-v1', 'task': task, 'field': field, 'offset': offset,
                'text': value[offset:end], 'next_offset': end if len(value) > end else None, 'total_characters': len(value)}
    after = body.get('after', 0)
    if type(after) is not int or not 0 <= after <= 2**63-1:
        raise ValueError('Invalid tool event cursor')
    rows = store.query("SELECT seq,payload FROM agent_events WHERE turn_id=? AND kind='tool' AND seq>? ORDER BY seq LIMIT 21", (task, after))
    more = len(rows) > 20
    rows = rows[:20]
    return {'schema': 'public-context-v1', 'task': task,
            'events': [{'cursor': row['seq'], 'tool': json.loads(clean(row['payload']))} for row in rows],
            'next_after': rows[-1]['seq'] if more else None,
            'coverage': 'Public recorded tool events; historical output may be truncated or missing. No native private state.'}
