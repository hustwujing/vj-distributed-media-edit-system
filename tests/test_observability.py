import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from webapp import agent_store,telemetry
from webapp.store import Store
from webapp.app import create_app
from webapp.observation_collector import Archive,handler,prometheus
from webapp.diagnostics import diagnose
from webapp.task_leases import Coordinator


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root/'data');agent_store.initialize(self.store)
        self.project=self.store.add_project('owner','private-name')['id']
        with telemetry.bind(request_id='a'*32):self.task=agent_store.add_turn(self.store,self.project,'plan','SECRET PRIVATE PROMPT')
    def tearDown(self):self.temp.cleanup()

    def test_provider_failure_evidence_survives_without_driving_business_state(self):
        from webapp import provider_calls
        w={'id':self.task,'run_id':self.task,'manual':True};step={'id':'image','revision':0,'attempts':0}
        provider_calls.intent(self.store,w,step,'https://fixture.invalid','image.edit')
        with patch.dict(os.environ,{'PROVIDER_TEST_KEY':'private-provider-key-123'}):
            provider_calls.update(self.store,w,step,'failed','job-one',record={'error':{'code':'InvalidInput','message':'Input rejected private-provider-key-123'}})
        provider_calls.update(self.store,w,step,'failed')
        before=self.store.query('SELECT * FROM agent_turns')
        report=diagnose(self.store,self.task,include_logs=False)
        failure=report['provider_calls'][0]['failure']
        self.assertEqual(failure['code'],'InvalidInput');self.assertEqual(failure['code_source'],'structured')
        self.assertIn('Input rejected',failure['provider_message']);self.assertNotIn('private-provider-key-123',json.dumps(report))
        self.assertEqual(before,self.store.query('SELECT * FROM agent_turns'))
        self.assertEqual(self.store.query('SELECT COUNT(*) AS n FROM workflow_provider_calls')[0]['n'],1)

    def test_audit_is_transactional_and_correlation_survives_rollback(self):
        before=self.store.query('SELECT * FROM observation_audit')
        with self.assertRaises(RuntimeError):
            with self.store.connect() as db:
                db.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(self.task,));raise RuntimeError('rollback')
        self.assertEqual(self.store.query('SELECT * FROM observation_audit'),before)
        self.store.execute("UPDATE agent_turns SET status='paused' WHERE id=?",(self.task,))
        row=self.store.query('SELECT * FROM observation_audit ORDER BY seq DESC LIMIT 1')[0]
        self.assertEqual((row['old_state'],row['new_state']),('queued','paused'))
        self.assertEqual(self.store.query('SELECT request_id FROM observation_tasks')[0]['request_id'],'a'*32)

    def test_context_is_thread_local_and_nested_spans_restore(self):
        output=[]
        def run(value):
            with telemetry.bind(task_id=value):output.append(telemetry.record('test')['task_id'])
        a=threading.Thread(target=run,args=('a'*32,));b=threading.Thread(target=run,args=('b'*32,));a.start();b.start();a.join();b.join()
        self.assertCountEqual(output,['a'*32,'b'*32]);self.assertNotIn('task_id',telemetry.context())
        with telemetry.bind(task_id=self.task):
            with telemetry.span('local'):self.assertIn('span_id',telemetry.context())
            self.assertNotIn('span_id',telemetry.context())

    def test_payloads_credentials_exception_messages_and_source_never_exported(self):
        with patch.dict(os.environ,{'TEST_API_KEY':'secret-token-123'}):
            try:raise ValueError('secret-token-123 user-content https://signed.example/?token=x')
            except ValueError as exc:r=telemetry.record('failed','ERROR',exc=exc,prompt='user-content',provider_request_id='secret-token-123',url='https://signed.example/?token=x')
        body=json.dumps(r)
        for value in ('secret-token-123','user-content','signed.example','raise ValueError'):self.assertNotIn(value,body)
        self.assertEqual(r['exception_type'],'ValueError');self.assertTrue(r['stack'])
        self.assertEqual(telemetry.record('newline\nattack')['event'],'invalid')
        validation=telemetry.record('workflow.plan_compile_rejected',validation_path='proposal.add_tasks.0',
                                    validation_type='missing',validation_error_count=1)
        self.assertEqual(validation['validation_path'],'proposal.add_tasks.0')
        self.assertEqual(validation['validation_type'],'missing')

    def test_model_transport_latency_and_route_fields_are_retained(self):
        row=telemetry.record('workflow.model_latency',response_transport='buffered',requested_model='claude-sonnet-5-5',
            response_model='claude-sonnet-5-5',model_route_changed=False,headers_seconds=1.2,first_chunk_seconds=1.3,
            first_public_seconds=1.4,publisher_calls=2,publisher_seconds=.01,duration_seconds=1.5)
        self.assertEqual(row['response_transport'],'buffered');self.assertEqual(row['response_model'],'claude-sonnet-5-5')
        self.assertEqual((row['headers_seconds'],row['publisher_calls'],row['model_route_changed']),(1.2,2,False))

    def test_http_request_ids_500_and_owner_authorization(self):
        app=create_app(self.root/'api',False)
        @app.get('/test-failure')
        def fail():raise RuntimeError('PRIVATE CONTENT')
        with TestClient(app) as c,TestClient(app) as other:
            r=c.get('/test-failure',headers={'X-Request-ID':'untrusted'});self.assertEqual(r.status_code,500)
            self.assertRegex(r.headers['x-request-id'],'^[a-f0-9]{32}$');self.assertNotIn('PRIVATE',r.text)
            p=c.post('/api/projects',json={'name':'p'}).json()['id']
            with telemetry.bind(request_id='b'*32):t=agent_store.add_turn(app.state.store,p,'plan','secret')
            url=f'/api/projects/{p}/diagnostics/{t}'
            with patch('webapp.diagnostics.observation',return_value={'events':[]}):
                self.assertEqual(c.get(url).status_code,200);self.assertEqual(other.get(url).status_code,404)
                self.assertIn('attachment',c.get(url+'?download=true').headers['content-disposition'])
                self.assertEqual(c.get(f'/api/projects/{p}/diagnostic-requests/'+('b'*32)).json()['task_ids'],[t])

    def test_diagnosis_does_not_mutate_or_expose_user_data(self):
        before=self.store.query('SELECT * FROM agent_turns')
        with patch('webapp.diagnostics.observation',return_value={'events':[]}):report=diagnose(self.store,self.task)
        self.assertEqual(before,self.store.query('SELECT * FROM agent_turns'));self.assertNotIn('SECRET',json.dumps(report))
        self.assertEqual(report['task']['id'],self.task);self.assertTrue(report['read_only']);self.assertTrue(report['timeline'])

    def test_database_outage_still_returns_collector_evidence(self):
        with patch.object(self.store,'query',side_effect=ConnectionError('secret')),patch('webapp.diagnostics.observation',return_value={'events':[{'event':'phase.error','task_id':self.task}]}):
            report=diagnose(self.store,self.task)
        self.assertFalse(report['complete']);self.assertTrue(report['logs']);self.assertTrue(report['gaps']);self.assertNotIn('secret',json.dumps(report))

    def test_real_takeover_history_identifies_old_and_new_nodes(self):
        a=Coordinator(self.store,'node-a','release-a',ttl=3);b=Coordinator(self.store,'node-b','release-b',ttl=3)
        lease=a.claim(self.task,'agent');self.assertIsNotNone(lease)
        self.store.execute('UPDATE execution_leases SET expires=0 WHERE attempt=?',(lease.attempt,))
        newer=b.claim(self.task,'agent');self.assertIsNotNone(newer)
        report=diagnose(self.store,self.task,include_logs=False)
        self.assertEqual({x['node'] for x in report['attempts']},{'node-a','node-b'})
        self.assertTrue(any(x['status']=='expired' for x in report['attempts']))

    def test_archive_pagination_retention_and_safe_ingestion(self):
        archive=Archive(self.root/'archive',max_rows=3)
        for i in range(5):self.assertTrue(archive.ingest(json.dumps(telemetry.record('test',task_id=self.task,generation=i))))
        first=archive.search({'task_id':self.task,'limit':2});second=archive.search({'task_id':self.task,'after':first['next_after'],'limit':2})
        self.assertTrue(first['has_more']);self.assertGreater(second['events'][0]['seq'],first['next_after'])
        archive.prune();self.assertEqual(len(archive.search({})['events']),3)
        self.assertFalse(archive.ingest('Started the service'));self.assertEqual(archive.dropped,0)
        self.assertFalse(archive.ingest('x'*40000));self.assertEqual(archive.dropped,1)

    def test_collector_authenticated_loopback_http_and_prometheus_no_high_cardinality(self):
        from http.server import ThreadingHTTPServer
        import requests
        archive=Archive(self.root/'archive');archive.snapshot={'gauges':{'workers_available':2},'views':{'phases':[{'phase':'local','seconds':1,'calls':1,'failures':0}]}}
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(archive,'private-token'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            session=requests.Session();session.trust_env=False;base='http://127.0.0.1:'+str(server.server_port)
            self.assertEqual(session.get(base+'/logs').status_code,401)
            self.assertEqual(session.get(base+'/logs',headers={'Authorization':'Bearer private-token'}).status_code,200)
            metrics=session.get(base+'/metrics',headers={'Authorization':'Bearer private-token'}).text
            self.assertIn('workers_available 2',metrics)
            for label in ('task_id','request_id','owner','external_id'):self.assertNotIn(label,metrics)
        finally:server.shutdown();server.server_close();thread.join()

    def test_alert_dedup_hold_recovery_and_maintenance(self):
        archive=Archive(self.root/'archive');archive.evaluate({'database_unavailable':{'scope':'database'}},now=100,hold=5)
        self.assertEqual(archive.alerts()[0]['state'],'pending')
        archive.evaluate({'database_unavailable':{'scope':'database'}},now=106,hold=5)
        archive.evaluate({'database_unavailable':{'scope':'database'}},now=107,hold=5)
        with archive.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM alert_history').fetchone()[0],2)
        archive.evaluate({'database_unavailable':None},now=108);self.assertEqual(archive.alerts()[0]['state'],'resolved')
        archive.evaluate({'database_unavailable':{'scope':'database'}},now=109,maintenance=True);self.assertEqual(archive.alerts()[0]['state'],'suppressed')

    def test_storage_full_and_collector_failure_do_not_mutate_business(self):
        archive=Archive(self.root/'archive')
        with patch.object(archive,'connect',side_effect=sqlite3.OperationalError('disk full')):
            self.assertFalse(archive.ingest(json.dumps(telemetry.record('test'))))
        self.assertEqual(archive.dropped,1)
        self.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(self.task,))
        self.assertEqual(self.store.query('SELECT status FROM agent_turns WHERE id=?',(self.task,))[0]['status'],'succeeded')

    def test_cluster_snapshot_queries_existing_schema_without_mutation(self):
        from webapp.observation_collector import sample
        archive=Archive(self.root/'archive')
        before=self.store.query('SELECT * FROM agent_turns')
        with patch('webapp.observation_collector.storage_health',return_value='available'),patch('webapp.observation_collector.gpu_health',return_value={'state':'available','devices':[]}):
            value=sample(self.store,archive)
        self.assertEqual(value['dependencies']['database'],'available')
        self.assertEqual(value['gauges']['tasks_queued'],1)
        self.assertEqual(before,self.store.query('SELECT * FROM agent_turns'))
        self.assertFalse(any(isinstance(v,dict) and 'error_code' in v for v in value['views'].values()))

    def test_submission_unknown_is_visible_in_cluster_gauge_and_task_diagnosis(self):
        from webapp.observation_collector import sample
        archive=Archive(self.root/'unknown-provider')
        self.store.execute('''INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,inputs,created,updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',('unknown-call',self.task,'render',0,1,'provider','video.generate_from_text',
            'submission_unknown','{}',self.store.now(),self.store.now()))
        with patch('webapp.observation_collector.storage_health',return_value='available'),patch('webapp.observation_collector.gpu_health',return_value={}):
            value=sample(self.store,archive)
        self.assertEqual(value['gauges']['provider_unknown'],1)
        with patch('webapp.diagnostics.observation',return_value={'events':[]}):report=diagnose(self.store,self.task)
        self.assertIn('verify_upstream',{item['code'] for item in report['suggestions']})

    def test_capacity_snapshot_expires_instead_of_reporting_stale_capacity_as_healthy(self):
        from webapp.observation_collector import sample
        from tests.test_api_capacity import snapshot
        archive=Archive(self.root/'capacity')
        row=snapshot('api.pool.snapshot','read',active=6,workers=6,queued=12,queue_capacity=12)
        archive.ingest(json.dumps(row))
        stale=dict(snapshot('api.pool.snapshot','storage',active=4),time=time.time()-25)
        archive.ingest(json.dumps(stale))
        with patch.dict(os.environ,{'VIDEO_USE_OBSERVATION_API_PORTS':''}),patch('webapp.observation_collector.storage_health',return_value='available'),patch('webapp.observation_collector.gpu_health',return_value={}):
            value=sample(self.store,archive)
        self.assertEqual([p['name'] for p in value['views']['api_capacity']['pools']],['read'])
        self.assertEqual(value['gauges']['api_pools_saturated'],1)
        self.assertEqual(value['gauges']['api_capacity_observed_instances'],0)
        self.assertTrue(value['views']['api_capacity']['gaps'])

    def test_metric_write_failure_keeps_completed_span_correlation(self):
        from types import SimpleNamespace
        from webapp.execution_metrics import phase
        from webapp.task_leases import CURRENT
        events=[]
        def collect(event,level='INFO',exc=None,**fields):events.append(telemetry.record(event,level,exc,**fields))
        token=CURRENT.set(SimpleNamespace(store=self.store,run_id=self.task,attempt='metric-attempt',worker='w',generation=1,step_id=None))
        try:
            with telemetry.bind(task_id=self.task,span_id='1'*16),patch('webapp.telemetry.emit',side_effect=collect),patch('webapp.store.Store',side_effect=TimeoutError('metrics budget')):
                with phase('input_download'):business_result='downloaded'
                self.assertEqual(telemetry.context()['span_id'],'1'*16)
        finally:CURRENT.reset(token)
        self.assertEqual(business_result,'downloaded')
        finished=next(e for e in events if e['event']=='phase.finished')
        warning=next(e for e in events if e['event']=='telemetry.phase_accounting_failed')
        self.assertEqual(finished['outcome'],'succeeded')
        self.assertEqual(warning['span_id'],finished['span_id'])
        self.assertEqual(warning['parent_span_id'],'1'*16)

    def test_accounting_warning_does_not_imply_failed_storage_transfer(self):
        from webapp.observation_collector import sample
        archive=Archive(self.root/'archive')
        archive.ingest(json.dumps(telemetry.record('telemetry.phase_accounting_failed','WARNING',phase='input_download')))
        with patch.dict(os.environ,{'VIDEO_USE_OBSERVATION_API_PORTS':''}),patch('webapp.observation_collector.storage_health',return_value='available'),patch('webapp.observation_collector.gpu_health',return_value={'state':'available','devices':[]}):
            gauges=sample(self.store,archive)['gauges']
            self.assertEqual(gauges['storage_errors_5m'],0)
            self.assertEqual(gauges['telemetry_dropped_5m'],1)
            for phase in ('input_download','output_upload'):
                archive.ingest(json.dumps(telemetry.record('phase.error','ERROR',phase=phase)))
            gauges=sample(self.store,archive)['gauges']
            self.assertEqual(gauges['storage_errors_5m'],2)
            self.assertEqual(gauges['telemetry_dropped_5m'],1)

    def test_newest_log_page_and_truncation_are_explicit(self):
        archive=Archive(self.root/'archive')
        for i in range(5):archive.ingest(json.dumps(telemetry.record('test',task_id=self.task,generation=i)))
        first=archive.search({'task_id':self.task,'newest':1,'limit':2})
        second=archive.search({'task_id':self.task,'newest':1,'limit':2,'after':first['next_after']})
        self.assertEqual([x['generation'] for x in first['events']],[4,3])
        self.assertEqual([x['generation'] for x in second['events']],[2,1])
        with patch('webapp.diagnostics.observation',return_value=first):report=diagnose(self.store,self.task,limit=2)
        self.assertFalse(report['complete']);self.assertIn('PageTruncated',[g['error_code'] for g in report['gaps']])

    def test_child_error_cannot_spoof_context_or_export_raw_output(self):
        path=self.root/'child.log'
        path.write_text(json.dumps({'schema':1,'event':'tool.failure','task_id':'b'*32,
             'exception_type':'ValueError','error_code':'InvalidInput','message':'PRIVATE USER CONTENT',
             'stack':[{'file':'tool.py','function':'run','line':'3','locals':'PRIVATE USER CONTENT'}]})+'\n')
        records=[]
        def collect(event,level='INFO',exc=None,**fields):records.append(telemetry.record(event,level,exc,**fields))
        with telemetry.bind(task_id=self.task),patch('webapp.telemetry.emit',side_effect=collect):telemetry.child_failure(path)
        self.assertEqual(records[0]['task_id'],self.task);self.assertNotIn('PRIVATE',json.dumps(records))
        linked=self.root/'link';linked.symlink_to(path)
        with patch('webapp.telemetry.emit') as emit:telemetry.child_failure(linked);self.assertEqual(emit.call_args.args[0],'telemetry.child_evidence_unavailable')

    def test_response_body_failure_retains_request_id_and_failed_outcome(self):
        import asyncio
        from webapp.observation_http import ObservationMiddleware
        messages=[];events=[]
        async def application(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[(b'content-length',b'99')]})
            await send({'type':'http.response.body','body':b'partial','more_body':True})
            raise PermissionError(13,'PRIVATE USER PATH')
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):messages.append(message)
        def collect(event,level='INFO',exc=None,**fields):events.append(telemetry.record(event,level,exc,**fields))
        with patch('webapp.telemetry.emit',side_effect=collect),patch('webapp.observation_http.emit',side_effect=collect):
            with self.assertRaises(PermissionError):asyncio.run(ObservationMiddleware(application)({'type':'http','path':'/static/test.js','method':'GET','headers':[]},receive,send))
        request_id=dict(messages[0]['headers'])[b'x-request-id'].decode()
        self.assertTrue(all(e['request_id']==request_id for e in events))
        self.assertEqual(events[-1]['outcome'],'failed');self.assertEqual(events[-1]['bytes_sent'],7)
        self.assertTrue(any(e.get('error_code')=='EACCES' for e in events));self.assertNotIn('PRIVATE USER PATH',json.dumps(events))

    def test_static_mount_cannot_narrow_new_session_cookie_path(self):
        app=create_app(self.root/'cookie-api',False)
        with TestClient(app,root_path='/video-use') as client:
            r=client.get('/video-use/static/task_progress.js')
            self.assertEqual(r.status_code,200)
            self.assertIn('Path=/video-use/;',r.headers['set-cookie'])
            self.assertNotIn('Path=/video-use/static',r.headers['set-cookie'])

    def test_provider_metadata_is_optional_and_cannot_break_sdk_response(self):
        from unittest.mock import Mock
        for response in (Mock(),type('Response',(),{'status_code':200,'headers':None})()):
            telemetry.provider_response(response)
        response=Mock();response.headers={'x-request-id':'verified-id'}
        self.assertEqual(telemetry.response_identity(response),'verified-id')

    def test_process_exit_oom_requires_actual_cgroup_counter(self):
        from types import SimpleNamespace
        group=self.root/'group';group.mkdir();(group/'memory.events').write_text('oom 1\noom_kill 1\n')
        with patch('webapp.telemetry.emit') as emit:
            telemetry.process_exit(SimpleNamespace(returncode=-9),[group]);self.assertEqual(emit.call_args.kwargs['oom_kill'],1)
            telemetry.process_exit(SimpleNamespace(returncode=-9),[self.root/'missing']);self.assertIsNone(emit.call_args.kwargs['oom_kill'])


class PostgresObservationTests(unittest.TestCase):
    def test_real_postgres_transactional_audit_and_cross_node_read(self):
        from tests.distributed_support import PostgresServer
        pg=PostgresServer()
        try:
            with tempfile.TemporaryDirectory() as folder:
                a=Store(Path(folder)/'a',dsn=pg.dsn);agent_store.initialize(a)
                p=a.add_project('owner','test')['id'];t=agent_store.add_turn(a,p,'plan','private')
                b=Store(Path(folder)/'b',dsn=pg.dsn)
                with self.assertRaises(RuntimeError):
                    with a.connect() as db:db.execute("UPDATE agent_turns SET status='failed' WHERE id=?",(t,));raise RuntimeError('rollback')
                self.assertFalse(b.query("SELECT seq FROM observation_audit WHERE task_id=? AND new_state='failed'",(t,)))
                b.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(t,))
                self.assertTrue(a.query("SELECT seq FROM observation_audit WHERE task_id=? AND new_state='succeeded'",(t,)))
                # Observational writes cannot inherit the long business-lock
                # timeout or turn lock contention into a business exception.
                from webapp.execution_metrics import phase
                from types import SimpleNamespace
                from webapp.task_leases import CURRENT
                a.timeout=1
                lease=SimpleNamespace(store=a,attempt='metric-attempt',run_id=t,step_id=None,worker='w',generation=1)
                with b.connect():
                    token=CURRENT.set(lease);started=time.monotonic()
                    try:
                        with phase('poll'):pass
                    finally:CURRENT.reset(token)
                    self.assertLess(time.monotonic()-started,.5)
                    # Independent metrics must commit even while an unrelated
                    # transaction holds the global business control lock.
                    self.assertEqual(a.query("SELECT calls FROM execution_phase_metrics WHERE attempt='metric-attempt' AND phase='poll'")[0]['calls'],1)

                # A real conflict on the metric row still has a short budget;
                # missing accounting is signalled without failing the task.
                with b.connect() as db:
                    db.execute("UPDATE execution_phase_metrics SET calls=calls WHERE attempt='metric-attempt'")
                    token=CURRENT.set(lease);started=time.monotonic()
                    try:
                        with patch('webapp.telemetry.emit') as emit:
                            with phase('poll'):business_result='completed'
                            self.assertTrue(any(c.args[0]=='telemetry.phase_accounting_failed' for c in emit.call_args_list))
                    finally:CURRENT.reset(token)
                    self.assertEqual(business_result,'completed')
                    self.assertLess(time.monotonic()-started,.5)
                self.assertEqual(a.query("SELECT calls FROM execution_phase_metrics WHERE attempt='metric-attempt' AND phase='poll'")[0]['calls'],1)

                # Re-initializing an installed schema must not seek exclusive
                # locks on task tables held by an ordinary diagnostic reader.
                from webapp.observation_schema import initialize
                errors=[]
                def reopen():
                    try:initialize(Store(Path(folder)/'restart',dsn=pg.dsn,timeout=.2))
                    except Exception as exc:errors.append(type(exc).__name__)
                with a.connect(readonly=True) as db:
                    db.execute('SELECT id FROM agent_turns LIMIT 1').fetchone()
                    thread=threading.Thread(target=reopen);thread.start();thread.join(timeout=3)
                    self.assertFalse(thread.is_alive());self.assertEqual(errors,[])

        finally:pg.close()
