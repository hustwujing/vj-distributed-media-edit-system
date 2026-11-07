"""Local journal collector, bounded indexed history, metrics and operator views.

Read-only access to business state. Separate storage survives business DB outage.
HTTP binds loopback and every data endpoint requires a deployment-only token.
"""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from urllib.parse import parse_qs,urlsplit
from .telemetry import clean,identifier,record,release

FILTERS={'task_id','request_id','node','release','operation','error_code','service','trace_id','attempt_id','level'}


class Archive:
    def __init__(self,root,max_rows=200000,retention=7*86400):
        self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=self.root/'observations.sqlite3';self.max_rows=max_rows;self.retention=retention;self.dropped=0;self.last_drop=0
        self.started=time.time();self.last_ingest=0;self.last_scan=0;self.scan_error='';self.storage_probe_at=0;self.storage_state='unverified';self.snapshot={};self.lock=threading.RLock()
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            db.executescript('''CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,time REAL NOT NULL,body TEXT NOT NULL,
                task_id TEXT,request_id TEXT,node TEXT,release TEXT,operation TEXT,error_code TEXT,service TEXT,trace_id TEXT,attempt_id TEXT,level TEXT);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts(rule TEXT PRIMARY KEY,state TEXT NOT NULL,since REAL NOT NULL,updated REAL NOT NULL,detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alert_history(seq INTEGER PRIMARY KEY AUTOINCREMENT,rule TEXT,state TEXT,time REAL,detail TEXT);
                CREATE TABLE IF NOT EXISTS api_capacity(instance TEXT NOT NULL,metric TEXT NOT NULL,time REAL NOT NULL,body TEXT NOT NULL,PRIMARY KEY(instance,metric));
                CREATE INDEX IF NOT EXISTS events_time ON events(time,seq);''')
            for field in FILTERS:db.execute(f'CREATE INDEX IF NOT EXISTS events_{field} ON events({field},seq)')
        self.path.chmod(0o600)

    def drop(self):
        self.dropped+=1;self.last_drop=time.time()

    def connect(self):
        db=sqlite3.connect(self.path,timeout=.2);db.row_factory=sqlite3.Row;return db

    def ingest(self,raw,cursor=''):
        try:
            if len(raw)>32768:raise ValueError('OversizedRecord')
            if not raw.lstrip().startswith('{'):return False
            value=json.loads(raw)
            if not isinstance(value,dict) or value.get('schema')!=1:return False
            event=identifier(value.get('event','invalid'))
            body=clean(value);body.update(schema=1,event=event,time=float(value['time']))
            if abs(body['time']-time.time())>365*86400:raise ValueError('InvalidTimestamp')
            from datetime import datetime,timezone
            body['timestamp']=datetime.fromtimestamp(body['time'],timezone.utc).isoformat()
            body['event_id']=body.get('event_id') or __import__('uuid').uuid4().hex
            encoded=json.dumps(body,separators=(',',':'))
            fields=sorted(FILTERS)
            with self.lock,self.connect() as db:
                inserted=db.execute('INSERT OR IGNORE INTO events(event_id,time,body,'+','.join(fields)+') VALUES ('+','.join('?' for _ in range(3+len(fields)))+')',
                           (body['event_id'],body['time'],encoded,*(body.get(k) for k in fields))).rowcount
                from .api_capacity import metric_key
                metric=metric_key(body)
                if metric and inserted and body['time']<=time.time()+5:
                    db.execute('INSERT INTO api_capacity VALUES (?,?,?,?) ON CONFLICT(instance,metric) DO UPDATE SET time=excluded.time,body=excluded.body WHERE excluded.time>=api_capacity.time',
                               (body['instance'],metric,body['time'],encoded))
                if cursor:db.execute("INSERT INTO metadata VALUES ('cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(cursor,))
            self.last_ingest=time.time();return True
        except Exception:
            self.drop();return False

    def search(self,parameters):
        limit=max(1,min(int(parameters.get('limit',100)),200));after=max(0,int(parameters.get('after',0)))
        newest=parameters.get('newest') in ('1',1,True)
        terms=['seq<?' if newest and after else 'seq>?'];args=[after if after else 0]
        if newest and not after:terms=['seq>?']
        for key in sorted(FILTERS):
            if parameters.get(key):terms.append(key+'=?');args.append(identifier(parameters[key]))
        for key,operator in (('since','>='),('until','<=')):
            if parameters.get(key):terms.append('time'+operator+'?');args.append(float(parameters[key]))
        with self.lock,self.connect() as db:
            rows=db.execute('SELECT seq,body FROM events WHERE '+' AND '.join(terms)+(' ORDER BY seq DESC LIMIT ?' if newest else ' ORDER BY seq LIMIT ?'),(*args,limit+1)).fetchall()
        events=[dict(json.loads(r['body']),seq=r['seq']) for r in rows[:limit]]
        return {'events':events,'has_more':len(rows)>limit,'next_after':events[-1]['seq'] if events else after,
                'retention_seconds':self.retention,'collector_dropped':self.dropped}

    def prune(self):
        with self.lock,self.connect() as db:
            db.execute('DELETE FROM api_capacity WHERE time<?',(time.time()-300,))
            db.execute('DELETE FROM events WHERE time<?',(time.time()-self.retention,))
            row=db.execute('SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET ?',(self.max_rows,)).fetchone()
            if row:db.execute('DELETE FROM events WHERE seq<=?',(row['seq'],))
            db.execute('DELETE FROM alert_history WHERE time<?',(time.time()-30*86400,))
            db.execute('PRAGMA incremental_vacuum(1000)')
        # Physical hard ceiling; oldest telemetry can be sacrificed, business data cannot.
        ceiling=int(os.environ.get('VIDEO_USE_OBSERVATION_MAX_BYTES',str(512*1024**2)))
        if sum(p.stat().st_size for p in self.root.glob('observations.sqlite3*'))>ceiling:
            with self.lock,self.connect() as db:
                db.execute('DELETE FROM events WHERE seq IN (SELECT seq FROM events ORDER BY seq LIMIT ?)',(max(1,self.max_rows//2),))
                db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.execute('VACUUM')
            self.drop()

    def alerts(self):
        with self.lock,self.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM alerts ORDER BY rule')]

    def evaluate(self,conditions,now=None,hold=30,maintenance=False):
        now=time.time() if now is None else now
        with self.lock,self.connect() as db:
            for rule,detail in conditions.items():
                old=db.execute('SELECT * FROM alerts WHERE rule=?',(rule,)).fetchone()
                since=old['since'] if old and old['state'] in ('pending','firing','suppressed') and detail else now
                state='suppressed' if detail and maintenance else 'firing' if detail and now-since>=hold else 'pending' if detail else 'resolved'
                encoded=json.dumps(detail or {},sort_keys=True)
                if not old or old['state']!=state:
                    db.execute('INSERT INTO alert_history(rule,state,time,detail) VALUES (?,?,?,?)',(rule,state,now,encoded))
                db.execute('INSERT INTO alerts VALUES (?,?,?,?,?) ON CONFLICT(rule) DO UPDATE SET state=excluded.state,since=excluded.since,updated=excluded.updated,detail=excluded.detail',(rule,state,since,now,encoded))

    def health(self):
        return {'alive':True,'ready':bool(self.last_scan and time.time()-self.last_scan<30),'last_scan':self.last_scan,'scan_error':self.scan_error,
                'last_ingest':self.last_ingest,'dropped':self.dropped,'last_drop_at':self.last_drop,'notification_channel':'not_configured',
                'business_dependencies':self.snapshot.get('dependencies',{}),'maintenance':(self.root/'maintenance').exists()}


def storage_health(store,archive):
    if time.time()-archive.storage_probe_at<60:return archive.storage_state
    archive.storage_probe_at=time.time()
    if not os.environ.get('VIDEO_USE_OBJECT_BUCKET'):
        archive.storage_state='not_configured';return archive.storage_state
    try:
        # A metadata-only HEAD of an existing committed object: no upload, billing
        # generation, content retrieval or user payload in telemetry.
        rows=store.query("SELECT object_key FROM artifact_objects WHERE state='published' LIMIT 1")
        import boto3
        from botocore.config import Config
        client=boto3.client('s3',endpoint_url=os.environ.get('VIDEO_USE_OBJECT_ENDPOINT'),
             config=Config(signature_version='s3v4',connect_timeout=1,read_timeout=1,retries={'max_attempts':0},s3={'addressing_style':'path'}))
        try:
            if rows:client.head_object(Bucket=os.environ['VIDEO_USE_OBJECT_BUCKET'],Key=rows[0]['object_key'])
            else:client.head_bucket(Bucket=os.environ['VIDEO_USE_OBJECT_BUCKET'])
        finally:client.close()
        archive.storage_state='available'
    except Exception:archive.storage_state='unavailable'
    return archive.storage_state


def gpu_health():
    try:
        result=subprocess.run(['nvidia-smi','--query-gpu=index,utilization.gpu,memory.used,memory.total','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=2)
        if result.returncode:return {'state':'unavailable','devices':[]}
        devices=[]
        for line in result.stdout.splitlines()[:64]:
            index,util,used,total=map(int,line.split(','));devices.append({'index':index,'utilization_percent':util,'memory_used_mb':used,'memory_total_mb':total})
        return {'state':'available','devices':devices}
    except (OSError,ValueError,subprocess.TimeoutExpired):return {'state':'unavailable','devices':[]}


def sample(store,archive):
    now=time.time();data={'at':now,'dependencies':{},'gauges':{},'views':{}}
    def query(key,sql,args=()):
        try:
            rows=store.query(sql,args);data['dependencies'].setdefault('database','available');return rows
        except Exception as exc:data['dependencies']['database']='unavailable';data['views'][key]={'error_code':type(exc).__name__};return []
    workers=query('nodes','SELECT id,node,release,heartbeat,draining FROM execution_workers ORDER BY heartbeat DESC LIMIT 100')
    data['views']['nodes']=workers
    import urllib.request
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    api_states=[]
    for port in os.environ.get('VIDEO_USE_OBSERVATION_API_PORTS','18766,18767').split(',')[:10]:
        if not port.isdigit() or not 1<=int(port)<=65535:continue
        try:
            with opener.open('http://127.0.0.1:'+port+'/api/health',timeout=.5) as response:available=response.status==200
        except Exception:available=False
        api_states.append({'port':int(port),'available':available})
    data['views']['apis']=api_states
    data['gauges']['api_unavailable']=sum(not a['available'] for a in api_states)
    data['dependencies']['object_storage']=storage_health(store,archive)
    data['views']['gpu']=gpu_health()
    active=[w for w in workers if now-w['heartbeat']<30 and not w['draining']]
    data['gauges']['workers_available']=len({w['node'] for w in active})
    queue=query('queue',"SELECT status,COUNT(*) AS n,MIN(created) AS oldest FROM execution_subjects GROUP BY status")
    data['views']['queue']=queue
    orchestrators=query('orchestrators','SELECT id,release,heartbeat,draining,reconciles,failures,last_duration FROM workflow_orchestrators ORDER BY heartbeat DESC LIMIT 20')
    data['views']['orchestrators']=orchestrators
    data['gauges']['orchestrators_available']=sum(now-r['heartbeat']<15 and not r['draining'] for r in orchestrators)
    workflows=query('dynamic_workflows',"SELECT status,COUNT(*) AS n,MIN(next_reconcile_at) AS due FROM runs GROUP BY status")
    data['views']['dynamic_workflows']=workflows
    data['gauges']['workflow_scan_lag_seconds']=max([max(0,now-r['due']) for r in workflows if r['status'] not in ('SUCCEEDED','FAILED','CANCELLED')]+[0])
    data['gauges']['workflow_reconcile_failures']=sum(r['failures'] for r in orchestrators)
    data['gauges']['workflow_reconcile_last_seconds']=max([r['last_duration'] for r in orchestrators]+[0])
    stuck=query('workflow_stuck_states','''SELECT
        SUM(CASE WHEN status='WAITING_USER' AND NOT EXISTS(SELECT 1 FROM workflow_decisions d WHERE d.run_id=runs.id AND d.status='pending') THEN 1 ELSE 0 END) AS waiting_without_decision,
        SUM(CASE WHEN status='ACTIVE' AND NOT EXISTS(SELECT 1 FROM run_work_items w WHERE w.run_id=runs.id AND w.status IN ('ready','running','leased','waiting','retry_wait','reviewing')) THEN 1 ELSE 0 END) AS active_without_work,
        COALESCE(SUM(reconcile_error_count),0) AS repeated_reconcile_errors FROM runs WHERE status IN ('ACTIVE','WAITING_USER')''')
    if stuck:
        data['gauges']['workflow_waiting_without_decision']=stuck[0]['waiting_without_decision'] or 0
        data['gauges']['workflow_active_without_work']=stuck[0]['active_without_work'] or 0
        data['gauges']['workflow_reconcile_repeated_errors']=stuck[0]['repeated_reconcile_errors'] or 0
    reservations=query('model_reservation_recovery','''SELECT COUNT(*) AS n,COALESCE(SUM(r.reserved_tokens),0) AS tokens
        FROM workflow_model_reservations r JOIN execution_attempts a ON a.id=r.attempt_id
        WHERE r.state='reserved' AND a.status IN ('interrupted','expired','finished')''')
    data['gauges']['model_orphan_reservations']=(reservations[0]['n'] or 0) if reservations else 0
    data['gauges']['model_orphan_reserved_tokens']=(reservations[0]['tokens'] or 0) if reservations else 0
    data['views']['dynamic_tasks']=query('dynamic_tasks','SELECT type,status,COUNT(*) AS n FROM run_tasks WHERE selected=1 GROUP BY type,status')
    data['views']['control_jobs']=query('control_jobs','SELECT control_kind,status,COUNT(*) AS n FROM control_jobs GROUP BY control_kind,status')
    data['views']['planning']=query('planning','SELECT status,COUNT(*) AS n FROM planner_runs GROUP BY status')
    data['views']['plan_drafts']=query('plan_drafts','SELECT status,schema_version,COUNT(*) AS n FROM workflow_plan_drafts GROUP BY status,schema_version')
    late=query('late_results',"SELECT COUNT(*) AS n FROM task_results WHERE status='late'")
    data['gauges']['workflow_late_results_total']=late[0]['n'] if late else 0
    for r in queue:
        if r['status'] in ('queued','running','waiting','paused','cancelled','succeeded','failed'):
            data['gauges']['tasks_'+r['status']]=r['n']
            if r['status']=='queued':data['gauges']['queue_oldest_seconds']=max(0,now-r['oldest'])
    cleanup=query('cleanup',"SELECT COUNT(*) AS n FROM execution_cleanup WHERE state NOT IN ('complete','allocated') AND (state='quarantined' OR deadline_at<? OR updated<?)",(now,now-120))
    data['gauges']['cleanup_overdue']=cleanup[0]['n'] if cleanup else 0
    external=query('external_cleanup',"SELECT state,COUNT(*) AS n FROM execution_external_cleanup GROUP BY state")
    data['views']['external_cleanup']=external
    data['gauges']['external_cleanup_unconfirmed']=sum(r['n'] for r in external if r['state'] in ('unknown','unconfirmed'))
    data['views']['affected_tasks']=query('affected_tasks',"SELECT id,status,updated FROM execution_subjects WHERE status IN ('failed','paused','queued') ORDER BY updated DESC LIMIT 5")
    providers=query('providers','SELECT provider,operation,state,COUNT(*) AS n,SUM(network_seconds) AS seconds,SUM(poll_count) AS polls,MIN(created) AS oldest FROM workflow_provider_calls GROUP BY provider,operation,state')
    data['views']['providers']=providers
    data['gauges']['provider_unknown']=sum(r['n'] for r in providers if r['state'] in ('unknown','submission_unknown') or (r['state']=='submitting' and now-r['oldest']>600))
    attempts=query('attempt_totals',"SELECT status,COUNT(*) AS n FROM execution_attempts GROUP BY status")
    data['gauges']['attempts_total']=sum(r['n'] for r in attempts)
    data['gauges']['expired_attempts_total']=sum(r['n'] for r in attempts if r['status']=='expired')
    retries=query('step_retries',"SELECT SUM(CASE WHEN attempts>1 THEN attempts-1 ELSE 0 END) AS n FROM agent_steps")
    data['gauges']['step_retries_total']=(retries[0]['n'] or 0) if retries else 0
    cancellations=query('cancel_latency',"SELECT r.created,MAX(c.updated) AS finished FROM execution_cancel_requests r JOIN execution_cleanup c ON c.task_id=r.task_id WHERE NOT EXISTS (SELECT 1 FROM execution_cleanup p WHERE p.task_id=r.task_id AND p.state<>'complete') GROUP BY r.task_id,r.created ORDER BY r.created DESC LIMIT 1000")
    durations=sorted(max(0,r['finished']-r['created']) for r in cancellations)
    data['gauges']['cancel_local_cleanup_p95_seconds']=durations[min(len(durations)-1,int(len(durations)*.95))] if durations else 0
    data['gauges']['cancel_local_cleanup_samples']=len(durations)
    data['views']['phases']=query('phases','SELECT phase,SUM(seconds) AS seconds,SUM(calls) AS calls,SUM(failures) AS failures FROM execution_phase_metrics GROUP BY phase')
    data['views']['claims']=query('claims','SELECT SUM(cpu) AS cpu,SUM(memory_mb) AS memory_mb FROM execution_resource_claims r WHERE EXISTS (SELECT 1 FROM execution_leases l WHERE l.attempt=r.attempt AND active=1 AND expires>?)',(now,))
    usage=shutil.disk_usage(archive.root)
    data['gauges'].update(disk_free_bytes=usage.free,disk_used_ratio=usage.used/usage.total,host_load_1m=os.getloadavg()[0],collector_dropped=archive.dropped)
    with archive.lock,archive.connect() as db:
        rows=db.execute("SELECT body FROM events WHERE time>? AND service='api' ORDER BY seq DESC LIMIT 10000",(now-300,)).fetchall()
    http=[json.loads(r['body']) for r in rows if json.loads(r['body']).get('event')=='http.finished' and json.loads(r['body']).get('route') not in ('/api/health','/api/agent/health')]
    data['gauges']['http_requests_5m']=len(http)
    data['gauges']['http_stream_failures_5m']=sum(1 for r in http if r.get('outcome')=='failed' and r.get('status',0)<500)
    data['gauges']['http_5xx_5m']=sum(1 for r in http if r.get('status',0)>=500)
    cancel_http=[r.get('first_byte_seconds') or r.get('duration_seconds',0) for r in http if r.get('method')=='POST' and r.get('route','').endswith('/cancel')]
    data['gauges']['cancel_http_latency_sum_5m']=sum(cancel_http)
    data['gauges']['cancel_http_requests_5m']=len(cancel_http)
    data['gauges']['http_latency_sum_5m']=sum(r.get('first_byte_seconds') or r.get('duration_seconds',0) for r in http)
    durations=sorted(r.get('first_byte_seconds') or r.get('duration_seconds',0) for r in http)
    data['gauges']['http_latency_p95_5m']=durations[min(len(durations)-1,int(len(durations)*.95))] if durations else 0
    with archive.lock,archive.connect() as db:
        recent=db.execute("SELECT body FROM events WHERE time>? AND level IN ('ERROR','WARNING') ORDER BY seq DESC LIMIT 1000",(now-300,)).fetchall()
    errors=[json.loads(r['body']) for r in recent]
    data['gauges']['telemetry_dropped_5m']=sum(e.get('dropped',0) if e.get('event')=='telemetry.dropped' else 1 for e in errors if e.get('event') in ('telemetry.dropped','telemetry.phase_accounting_failed'))
    # Accounting warnings inherit the measured phase, but do not mean that the
    # actual transfer failed. They belong only to the telemetry-gap alert.
    data['gauges']['storage_errors_5m']=sum(e.get('event')=='phase.error' and e.get('phase') in ('input_download','output_upload') for e in errors)
    try:
        mem={line.split()[0].rstrip(':'):int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split())>=2}
        data['gauges'].update(host_memory_available_bytes=mem['MemAvailable'],host_memory_total_bytes=mem['MemTotal'])
    except (OSError,ValueError,KeyError):pass
    data['views']['http']={'window_seconds':300,'capped_at':10000,'requests':len(http),'errors':data['gauges']['http_5xx_5m']}
    from .api_capacity import views
    with archive.lock,archive.connect() as db:
        capacities=db.execute('SELECT body FROM api_capacity WHERE time>? ORDER BY instance,metric',(now-20,)).fetchall()
    capacity=views([json.loads(r['body']) for r in capacities]);data['views']['api_capacity']=capacity
    observed=set(capacity['complete_instances'])
    data['gauges']['api_capacity_observed_instances']=len(observed)
    data['gauges']['api_capacity_missing_instances']=max(0,len(api_states)-len(observed))
    data['gauges']['api_pools_saturated']=sum(r['active']>=r['workers'] and r['queued']>=r['queue_capacity'] for r in capacity['pools'])
    if capacity['loops']:data['gauges']['api_loop_lag_max_seconds']=max(r['lag_seconds'] for r in capacity['loops'])
    data['gauges']['api_overload_rejections_5m']=sum(e.get('event')=='api.pool.unavailable' and e.get('reason') in ('queue_full','queue_timeout') for e in errors)
    data['gauges']['api_cleanup_deferred_5m']=sum(e.get('event')=='api.cleanup_deferred' for e in errors)
    return data


def prometheus(archive):
    values=dict(archive.snapshot.get('gauges',{}));values['collector_dropped']=archive.dropped
    values['collector_last_scan_seconds']=archive.last_scan
    values['collector_database_available']=int(archive.snapshot.get('dependencies',{}).get('database')=='available')
    lines=[]
    for key,value in sorted(values.items()):
        if isinstance(value,(int,float)):
            name='video_use_'+key;lines += ['# TYPE '+name+' gauge',name+' '+str(value)]
    # Finite phase vocabulary; no task/user/request/provider task labels.
    allowed={'prepare_environment','input_download','output_upload','submit','remote_sync','poll','local','collect','validate'}
    for row in archive.snapshot.get('views',{}).get('phases',[]):
        if row['phase'] in allowed:
            for field in ('seconds','calls','failures'):lines.append(f'video_use_phase_{field}{{phase="{row["phase"]}"}} {row[field]}')
    from .api_capacity import exposition
    lines.extend(exposition(archive.snapshot.get('views',{}).get('api_capacity',{})))
    return '\n'.join(lines)+'\n'


def monitor(archive,store,stop):
    while not stop.is_set():
        try:
            archive.snapshot=sample(store,archive);archive.last_scan=time.time();archive.scan_error=''
            gauges=archive.snapshot['gauges'];dependencies=archive.snapshot['dependencies']
            conditions={
                'orchestrator_unavailable':{'available':gauges['orchestrators_available']} if os.environ.get('VIDEO_USE_EXPECT_ORCHESTRATOR')=='1' and not gauges['orchestrators_available'] else None,
                'workflow_reconcile_stalled':{'seconds':gauges['workflow_scan_lag_seconds']} if gauges['workflow_scan_lag_seconds']>30 else None,
                'api_unavailable':{'count':gauges['api_unavailable']} if gauges['api_unavailable'] else None,
                'database_unavailable':{'scope':'database'} if dependencies.get('database')=='unavailable' else None,
                'workers_unavailable':{'available':gauges['workers_available']} if gauges['workers_available']<int(os.environ.get('VIDEO_USE_EXPECTED_WORKERS','2')) else None,
                'queue_stalled':{'oldest_seconds':gauges.get('queue_oldest_seconds',0)} if gauges.get('queue_oldest_seconds',0)>float(os.environ.get('VIDEO_USE_ALERT_QUEUE_SECONDS','300')) else None,
                'cleanup_overdue':{'count':gauges['cleanup_overdue']} if gauges['cleanup_overdue'] else None,
                'external_cleanup_unconfirmed':{'count':gauges['external_cleanup_unconfirmed']} if gauges['external_cleanup_unconfirmed'] else None,
                'provider_unknown':{'count':gauges['provider_unknown']} if gauges['provider_unknown'] else None,
                'workflow_waiting_without_decision':{'count':gauges.get('workflow_waiting_without_decision',0)} if gauges.get('workflow_waiting_without_decision',0) else None,
                'workflow_active_without_work':{'count':gauges.get('workflow_active_without_work',0)} if gauges.get('workflow_active_without_work',0) else None,
                'model_orphan_reservations':{'count':gauges.get('model_orphan_reservations',0),'tokens':gauges.get('model_orphan_reserved_tokens',0)} if gauges.get('model_orphan_reservations',0) else None,
                'http_errors':{'count':gauges['http_5xx_5m']+gauges['http_stream_failures_5m']} if gauges['http_5xx_5m']+gauges['http_stream_failures_5m']>=3 else None,
                'object_storage_unavailable':{'scope':'object_storage'} if dependencies.get('object_storage')=='unavailable' else None,
                'storage_errors':{'count':gauges['storage_errors_5m']} if gauges['storage_errors_5m'] else None,
                'collector_stale':{'seconds':time.time()-archive.last_ingest} if archive.last_ingest and time.time()-archive.last_ingest>120 else None,
                'disk_capacity':{'used_ratio':gauges['disk_used_ratio']} if gauges['disk_used_ratio']>.9 else None,
                'telemetry_dropped':{'count':archive.dropped,'producers':gauges['telemetry_dropped_5m']} if (archive.dropped and time.time()-archive.last_drop<300) or gauges['telemetry_dropped_5m'] else None,
                'api_capacity_telemetry_missing':{'instances':gauges['api_capacity_missing_instances']} if gauges['api_capacity_missing_instances'] else None,
                'api_dependency_overloaded':{'pools':gauges['api_pools_saturated'],'rejections':gauges['api_overload_rejections_5m']} if gauges['api_pools_saturated'] or gauges['api_overload_rejections_5m']>=5 else None,
                'api_event_loop_delayed':{'seconds':gauges.get('api_loop_lag_max_seconds')} if gauges.get('api_loop_lag_max_seconds',0)>.5 else None,
                'api_cache_cleanup_delayed':{'events':gauges['api_cleanup_deferred_5m']} if gauges['api_cleanup_deferred_5m'] else None,
            }
            for detail in conditions.values():
                if detail:
                    detail.update(release=release(),observed_at=time.time(),runbook='docs/observability-operations.md',task_samples=archive.snapshot['views'].get('affected_tasks',[]),diagnostics='deploy.sh observe task TASK_ID')
            archive.evaluate(conditions,hold=float(os.environ.get('VIDEO_USE_ALERT_HOLD_SECONDS','30')),maintenance=(archive.root/'maintenance').exists());archive.prune()
        except Exception as exc:
            archive.scan_error=type(exc).__name__
            # Separate stderr path, no DB dependency and no exception contents.
            print(json.dumps(record('collector.scan_failed','ERROR',exc=exc)),flush=True)
        stop.wait(5)


def journal(archive,stop):
    while not stop.is_set():
        with archive.connect() as db:row=db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()
        from .deployment_identity import namespace
        command=['journalctl','--namespace='+namespace(),'--follow','--output=json','--no-pager']
        command+=['--after-cursor',row['value']] if row else ['--since','-5min']
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        try:
            for line in process.stdout:
                if stop.is_set():break
                try:
                    entry=json.loads(line);message=entry.get('MESSAGE','')
                    if isinstance(message,str):archive.ingest(message,entry.get('__CURSOR',''))
                except Exception:archive.drop()
        finally:
            process.terminate();process.wait(timeout=5)
            process.stdout.close()
            # SIGTERM/SIGINT and requested shutdown are normal lifecycle
            # events, not evidence that the durable resume cursor is invalid.
            if not stop.is_set() and process.returncode>0 and row:
                with archive.connect() as db:db.execute("DELETE FROM metadata WHERE key='cursor'")
                archive.drop()
        stop.wait(2)


def handler(archive,token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.connection.settimeout(5)
            parsed=urlsplit(self.path)
            if parsed.path=='/dashboard':
                body=(Path(__file__).parent/'static/observability.html').read_bytes();return self.respond(body,'text/html')
            if not token or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                return self.respond(b'{"error":"unauthorized"}',status=401)
            try:
                params={k:v[0] for k,v in parse_qs(parsed.query).items()}
                if parsed.path=='/logs':value=archive.search(params)
                elif parsed.path=='/health':value=archive.health()
                elif parsed.path=='/snapshot':value=archive.snapshot
                elif parsed.path=='/alerts':value={'alerts':archive.alerts(),'notification_channel':'not_configured'}
                elif parsed.path=='/metrics':return self.respond(prometheus(archive).encode(),'text/plain; version=0.0.4')
                else:return self.respond(b'{"error":"not_found"}',status=404)
                return self.respond(json.dumps(value).encode())
            except (ValueError,TypeError):return self.respond(b'{"error":"invalid_query"}',status=400)
            except Exception:return self.respond(b'{"error":"collector_unavailable"}',status=503)
        def respond(self,body,kind='application/json',status=200):
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.end_headers()
            try:self.wfile.write(body)
            except OSError:pass
    return Handler


class BoundedServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(16)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        request.settimeout(5)
        if not self.slots.acquire(blocking=False):self.shutdown_request(request);return
        super().process_request(request,address)
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


def main():
    from .telemetry import setup
    setup('observer')
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--port',type=int,default=18768);args=parser.parse_args()
    from .store import Store
    archive=Archive(args.root,max_rows=int(os.environ.get('VIDEO_USE_OBSERVATION_MAX_ROWS','200000')),retention=int(os.environ.get('VIDEO_USE_OBSERVATION_RETENTION_SECONDS','604800')))
    store=Store(Path(os.environ['VIDEO_USE_DATA_DIR']),initialize_schema=False,timeout=1)
    stop=threading.Event()
    for worker in (journal,):threading.Thread(target=worker,args=(archive,stop),daemon=True).start()
    threading.Thread(target=monitor,args=(archive,store,stop),daemon=True).start()
    server=BoundedServer(('127.0.0.1',args.port),handler(archive,os.environ['VIDEO_USE_OBSERVATION_TOKEN']))
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:(stop.set(),threading.Thread(target=server.shutdown,daemon=True).start()))
    server.serve_forever();server.server_close()


if __name__=='__main__':main()
