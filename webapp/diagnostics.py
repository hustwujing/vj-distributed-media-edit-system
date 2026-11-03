"""Read-only bounded task diagnosis; all fields are explicit safe projections."""
import json
import os
import re
import time
import urllib.request
from .telemetry import release,identifier

ID=re.compile(r'^[a-f0-9]{32}$')


def observation(path,params=None):
    from urllib.parse import urlencode
    base=os.environ.get('VIDEO_USE_OBSERVATION_URL','http://127.0.0.1:18768')
    token=os.environ.get('VIDEO_USE_OBSERVATION_TOKEN','')
    if not token:raise RuntimeError('ObservationNotConfigured')
    request=urllib.request.Request(base+path+('?' + urlencode(params) if params else ''),headers={'Authorization':'Bearer '+token})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=2) as response:return json.loads(response.read(2*1024**2))


def diagnose(store,task_id,after=0,limit=100,include_logs=True):
    if not ID.fullmatch(task_id):raise ValueError('Invalid task identifier')
    limit=max(1,min(int(limit),200));after=max(0,int(after))
    report={'schema':1,'task_id':task_id,'generated_at':time.time(),'observer_release':release(),
            'complete':True,'gaps':[],'read_only':True,'facts':{},'suggestions':[]}
    def query(name,sql,args=()):
        from .api_execution import check_cancelled
        check_cancelled()
        try:return store.query(sql,args)
        except Exception as exc:
            report['complete']=False;report['gaps'].append({'source':name,'error_code':type(exc).__name__});return []
    tasks=query('task',"SELECT id,project,status,created,updated,'agent' AS kind FROM agent_turns WHERE id=? UNION ALL SELECT id,project,status,created,updated,kind FROM jobs WHERE id=?",(task_id,task_id))
    task=tasks[0] if tasks else None;report['task']=task
    report['correlation']=query('correlation','SELECT * FROM observation_tasks WHERE task_id=?',(task_id,))
    if task and not report['correlation']:
        report['complete']=False;report['gaps'].append({'source':'correlation','error_code':'PreInstrumentationOrMissing'})
    report['timeline']=query('audit','SELECT seq,entity,entity_id,old_state,new_state,created FROM observation_audit WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?',(task_id,after,limit+1))
    report['has_more']=len(report['timeline'])>limit;report['timeline']=report['timeline'][:limit]
    report['next_after']=report['timeline'][-1]['seq'] if report['timeline'] else after
    report['attempts']=query('attempts','''SELECT a.id,a.step_id,a.kind,a.generation,a.status,a.started,a.finished,a.exit_reason,
      w.node,w.id AS worker,w.release,w.heartbeat,w.draining FROM execution_attempts a
      LEFT JOIN execution_workers w ON w.id=a.worker WHERE a.turn_id=? ORDER BY a.started DESC LIMIT ?''',(task_id,limit))
    report['workflow']=query('workflow','SELECT id,status,updated FROM agent_workflows WHERE turn_id=?',(task_id,))
    report['steps']=query('steps','''SELECT s.id,s.status,s.revision,s.attempts,s.next_poll,s.wait_reason,s.error_code,s.updated FROM agent_steps s
      JOIN agent_workflows w ON s.workflow=w.id WHERE w.turn_id=? ORDER BY s.id LIMIT ?''',(task_id,limit))
    report['step_versions']=query('step_versions','SELECT v.step,v.revision,v.created FROM workflow_step_versions v JOIN agent_workflows w ON w.id=v.workflow WHERE w.turn_id=? ORDER BY v.created DESC LIMIT ?',(task_id,limit))
    report['quality']=query('quality','SELECT q.step,q.revision,q.actor,q.action,q.created FROM workflow_quality_events q JOIN agent_workflows w ON w.id=q.workflow WHERE w.turn_id=? ORDER BY q.created DESC LIMIT ?',(task_id,limit))
    report['step_checkpoints']=query('step_checkpoints','SELECT c.step,c.revision,c.attempt,c.updated FROM execution_step_checkpoints c JOIN agent_workflows w ON w.id=c.workflow WHERE w.turn_id=? ORDER BY c.updated DESC LIMIT ?',(task_id,limit))
    report['provider_calls']=query('providers','''SELECT request_key,step,revision,attempt_id,generation,provider,operation,state,external_id,
      network_seconds,poll_count,failure,created,updated FROM workflow_provider_calls WHERE workflow IN
      (SELECT id FROM agent_workflows WHERE turn_id=?) OR workflow=? ORDER BY updated DESC LIMIT ?''',(task_id,task_id,limit))
    for call in report['provider_calls']:
        try:call['failure']=json.loads(call['failure'])
        except (TypeError,ValueError):
            call['failure']={};report['complete']=False
            report['gaps'].append({'source':'provider_failure','error_code':'InvalidMetadata'})
    report['local_cleanup']=query('cleanup','SELECT attempt,node,state,retries,next_retry,updated FROM execution_cleanup WHERE task_id=? ORDER BY updated DESC LIMIT ?',(task_id,limit))
    report['external_cleanup']=query('external_cleanup','SELECT id,provider,external_id,state,phase,attempts,next_retry FROM execution_external_cleanup WHERE task_id=? LIMIT ?',(task_id,limit))
    report['cancellation']=query('cancellation','SELECT request_id,scope,reason,created FROM execution_cancel_requests WHERE task_id=?',(task_id,))
    report['metrics']=query('phases','SELECT phase,SUM(seconds) AS cumulative_seconds,SUM(calls) AS calls,SUM(failures) AS failures FROM execution_phase_metrics WHERE task_id=? GROUP BY phase',(task_id,))
    report['waits']=query('waits','SELECT reason,next_retry,updated FROM execution_waits WHERE task_id=?',(task_id,))
    report['checkpoints']=query('checkpoints','SELECT project,attempt,updated FROM execution_checkpoints WHERE project=(SELECT project FROM agent_turns WHERE id=?)',(task_id,))
    # Hash and logical artifact identity, never object keys or signed URLs.
    report['artifacts']=query('artifacts','''SELECT f.artifact,f.revision FROM artifact_files f WHERE f.project=
      (SELECT project FROM agent_turns WHERE id=? UNION ALL SELECT project FROM jobs WHERE id=?) LIMIT ?''',(task_id,task_id,limit))
    try:
        from .task_usage import snapshot
        report['usage']=snapshot(store,task_id,limit=limit)
    except Exception as exc:
        report['gaps'].append({'source':'usage','error_code':type(exc).__name__})
        report['complete']=False
    report['plan_readiness']=query('plan_readiness','SELECT state,digest,updated FROM plan_readiness WHERE turn_id=?',(task_id,))
    report['plan_reviews']=[]
    for item in query('plan_reviews',"SELECT seq,payload,created FROM agent_events WHERE turn_id=? AND kind='plan_review' ORDER BY seq DESC LIMIT 3",(task_id,)):
        try:
            value=json.loads(item['payload']);evidence=value.get('evidence',{})
            def scores(source):
                result={k:source[k] for k in ('source','choice','confidence','threshold','required_margin','error') if k in source}
                result['scores']={k:v for k,v in source.get('scores',{}).items() if k in ('ready','needs_information','discussion')}
                result['checks']={k:v for k,v in source.get('checks',{}).items() if k in ('requested_work','inputs_identified','parameters_resolved','deliverable_defined')}
                return result
            report['plan_reviews'].append({'seq':item['seq'],'created':item['created'],
                **{k:value[k] for k in ('state','digest','input_digest','saved_file_count','coverage') if k in value},
                'reviewer':scores(evidence),'primary':scores(evidence.get('primary',{}))})
        except (ValueError,TypeError,AttributeError):
            report['complete']=False;report['gaps'].append({'source':'plan_reviews','error_code':'InvalidMetadata'})
    report['control_classifications']=[]
    for item in query('control_classifications',"SELECT seq,payload,created FROM agent_events WHERE turn_id=? AND kind='control_classification' ORDER BY seq DESC LIMIT 10",(task_id,)):
        try:
            value=json.loads(item['payload']);evidence=value.get('evidence',{})
            report['control_classifications'].append({'seq':item['seq'],'created':item['created'],
                'outcome':value.get('outcome'),'binding_digest':value.get('binding_digest'),
                'evidence':{k:evidence[k] for k in ('source','choice','confidence','scores','threshold','required_margin') if k in evidence}})
        except (ValueError,TypeError,AttributeError):
            report['complete']=False;report['gaps'].append({'source':'control_classifications','error_code':'InvalidMetadata'})
    report['prompt_contexts']=[]
    for item in query('prompt_contexts',"SELECT seq,payload,created FROM agent_events WHERE turn_id=? AND kind='prompt_context' ORDER BY seq DESC LIMIT 20",(task_id,)):
        try:
            value=json.loads(item['payload'])
            report['prompt_contexts'].append({'seq':item['seq'],'created':item['created'],
                **{k:value[k] for k in ('schema','phase','characters','native_resume','related_tasks','feedback_items','operation_contracts') if k in value}})
        except (ValueError,TypeError):
            report['complete']=False;report['gaps'].append({'source':'prompt_contexts','error_code':'InvalidMetadata'})
    report['browser_render']=query('browser_render','SELECT generation,sequence,render_ms,created FROM agent_render_observations WHERE task_id=? ORDER BY created DESC LIMIT ?',(task_id,limit))
    report['logs']=[]
    if include_logs:
        try:
            logs=observation('/logs',{'task_id':task_id,'limit':limit,'newest':1});report['logs']=logs['events']
            if logs.get('has_more'):
                report['complete']=False;report['gaps'].append({'source':'logs','error_code':'PageTruncated'})
        except Exception as exc:
            report['complete']=False;report['gaps'].append({'source':'collector','error_code':type(exc).__name__})
    now=time.time()
    if task:
        active=[a for a in report['attempts'] if a['status']=='running']
        latest=max([task['updated']]+[e['created'] for e in report['timeline']]+[c['updated'] for c in report['provider_calls']])
        report['facts'].update(status=task['status'],wall_seconds=round(max(0,(now if task['status'] not in ('succeeded','failed','cancelled') else task['updated'])-task['created']),3),
                               latest_recorded_progress_at=latest,current_attempts=active)
        reasons=[w['reason'] for w in report['waits']]+[s['wait_reason'] for s in report['steps'] if s['wait_reason']]
        if any(p['state'] in ('submitting','unknown') for p in report['provider_calls']):
            report['suggestions'].append({'code':'verify_upstream','text':'上游提交结果未确认；先核实已保存句柄，不要重新生成。'})
        if task['status']=='cancelled':
            report['suggestions'].append({'code':'check_cleanup','text':'业务已停止；分别检查本地清理和上游取消，不能据此认定上游已停止。'})
        if any(a['status']=='expired' for a in report['attempts']):
            report['suggestions'].append({'code':'inspect_takeover','text':'存在过期尝试；对照新尝试和节点心跳核实接管，过期不等于物理资源已清理。'})
        report['facts']['latest_attempts']=report['attempts'][:3]
        report['facts']['wait_reasons']=reasons
        report['facts']['latest_errors']=[e for e in report['logs'] if e.get('level') in ('ERROR','CRITICAL')][:5]
        if task['status']=='failed' and not report['facts']['latest_errors'] and not any(s.get('error_code') for s in report['steps']):
            report['complete']=False;report['gaps'].append({'source':'failure_detail','error_code':'NoStructuredFailureEvidence'})
        report['facts']['next_actions_at']=[s['next_poll'] for s in report['steps'] if s['next_poll']>now]+[w['next_retry'] for w in report['waits'] if w['next_retry']>now]
        if not report['suggestions']:report['suggestions'].append({'code':'inspect_evidence','text':'按最近错误及对应步骤查看日志；证据不足时保持未知，不自动重试。'})
    if any(len(report[key])>=limit for key in ('attempts','steps','provider_calls','local_cleanup','external_cleanup','artifacts','step_versions','quality','step_checkpoints')):
        report['complete']=False;report['gaps'].append({'source':'related_records','error_code':'LimitReached'})
    report['evidence_scope']={'audit':'committed database state changes','logs':'best-effort, retention-limited telemetry','ordering':'audit sequence within committed history; cross-source clocks are not a total causal order','limit':limit}
    return report


def resolve_request(store,request_id):
    if not ID.fullmatch(request_id):raise ValueError('Invalid request identifier')
    rows=store.query('SELECT task_id FROM observation_tasks WHERE request_id=? ORDER BY created DESC LIMIT 50',(request_id,))
    if rows:return [r['task_id'] for r in rows]
    logs=observation('/logs',{'request_id':request_id,'limit':100})
    return sorted({r['task_id'] for r in logs['events'] if ID.fullmatch(r.get('task_id',''))})
