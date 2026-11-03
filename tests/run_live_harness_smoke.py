"""Explicit real SDK/model smoke on disposable PostgreSQL, S3 and two nodes.

Run as the privileged test operator. Credentials are read from a private JSON
file, never copied into the report or the sandboxes. This is not unit discovery.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
import uuid
import hashlib
import subprocess
import threading

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--credentials-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--claude-model',default='claude-4.6-sonnet')
    parser.add_argument('--deepseek-model',default='deepseek-v4-flash')
    parser.add_argument('--stream-only',action='store_true',help='Verify initial/native Claude stream plus cancellation for both Harnesses')
    args=parser.parse_args()
    credentials=json.loads(args.credentials_file.read_text())
    root=args.output.resolve();root.mkdir(mode=0o755,parents=True,exist_ok=False)
    profiles={name:{'harness':harness,'protocol':'anthropic','model':model,
                    'base_url':credentials['base_url'].rstrip('/'),'credential_env':'VIDEO_USE_HARNESS_TEST_API_KEY',
                    'reasoning_effort':'high'} for name,harness,model in
              [('claude','claude_code',args.claude_model),('deepseek','deepseek_harness',args.deepseek_model)]}
    os.environ.update(VIDEO_USE_AGENT_PROFILES_JSON=json.dumps(profiles),VIDEO_USE_AGENT_PROFILE='claude',
                      VIDEO_USE_HARNESS_TEST_API_KEY=credentials['key'],VIDEO_USE_DISTRIBUTED='1',
                      VIDEO_USE_WORKSPACE_STORAGE='tmpfs',VIDEO_USE_TASK_MODEL_CALLS='100',
                      VIDEO_USE_AGENT_RUN_SECONDS='300',VIDEO_USE_PLAN_SECONDS='300',VIDEO_USE_PLAN_MODELS='60',
                      VIDEO_USE_PLAN_TOOLS='100',VIDEO_USE_INTERNAL_PROVIDER_HOSTS='127.0.0.1,localhost,llmapi.bilibili.co',
                      VIDEO_USE_TELEMETRY_STREAM='stderr',AWS_EC2_METADATA_DISABLED='true',
                      AWS_CONFIG_FILE='/dev/null',AWS_SHARED_CREDENTIALS_FILE='/dev/null')
    for key in ('VIDEO_USE_REDIS_URL','VIDEO_USE_OBSERVATION_TOKEN','VIDEO_USE_OBSERVATION_URL'):
        os.environ.pop(key,None)
    from tests.distributed_support import PostgresServer,S3Server
    from webapp.agent_host import AgentHost
    from webapp import agent_store
    from webapp.plan_readiness import register
    from webapp.agent_profiles import for_task
    from webapp.harness_switching import request
    from webapp.artifact_store import S3Objects
    pg=PostgresServer();s3=None;children=[];logs=[];report={'scope':'Real Claude and DeepSeek SDKs and model services; disposable real PostgreSQL, Redis, encrypted HTTP S3 fixture, two distinct local execution nodes. No production data or services changed.','turns':[],'switches':[],'cancellations':[]}
    from webapp.agent_text import TextPublisher
    original_message=TextPublisher.message;stream_events={}
    def observe_message(publisher,identifier,text,phase='commentary',complete=False):
        stream_events.setdefault(publisher.task,[]).append({'time':time.monotonic(),'message_id':identifier,'characters':len(text),'complete':complete})
        return original_message(publisher,identifier,text,phase,complete)
    TextPublisher.message=observe_message
    from tests.run_workflow_suite import source_digest
    report['source_sha256']=source_digest()
    try:
        s3=S3Server()
        os.environ.update(VIDEO_USE_DATABASE_URL=pg.dsn,VIDEO_USE_OBJECT_BUCKET='harness-test',
                          VIDEO_USE_OBJECT_ENDPOINT=s3.endpoint,AWS_ACCESS_KEY_ID='test',AWS_SECRET_ACCESS_KEY='test',
                          AWS_DEFAULT_REGION='us-east-1',VIDEO_USE_OBJECT_ENCRYPTION_KEY=base64.b64encode(os.urandom(32)).decode(),
                          VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION='1')
        backend=S3Objects('harness-test',s3.endpoint);backend.client.create_bucket(Bucket='harness-test')
        from tests.distributed_support import free_port
        redis_port=free_port()
        children.append(subprocess.Popen(['redis-server','--bind','127.0.0.1','--port',str(redis_port),'--save','','--appendonly','no'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        os.environ.update(VIDEO_USE_REDIS_URL=f'redis://127.0.0.1:{redis_port}/0',VIDEO_USE_SESSION_SECRET=os.urandom(32).hex())
        import redis
        redis_client=redis.Redis(host='127.0.0.1',port=redis_port)
        for _ in range(100):
            try:
                if redis_client.ping():break
            except redis.ConnectionError:time.sleep(.05)
        else:raise RuntimeError('Isolated Redis did not start')
        hosts=[]
        for i in range(2):
            os.environ.update(VIDEO_USE_EXECUTOR_ROOT=str(root/f'executor-{i}'),VIDEO_USE_UID_START=str(834000+100*i),VIDEO_USE_UID_COUNT='16')
            hosts.append(AgentHost(root/f'data-{i}'))
        browser_identity=os.urandom(32).hex()
        project=hosts[0].store.add_project(hashlib.sha256(browser_identity.encode()).hexdigest(),'Cross harness checkpoint smoke')['id'];report['project']=project
        marker='HANDOFF_'+uuid.uuid4().hex[:12]
        prompts=[f'Remember this exact marker: {marker}. Reply with it only. No editing or external generation.',
                 'What exact marker did I ask you to remember? Reply with it only.',
                 'Retrieve the exact marker from our previous conversation. Use the command tool to print that marker with printf, then give its output. Do not ask me to repeat it.',
                 'What exact marker did I ask you to remember? Reply with it only.',
                 'Retrieve the exact marker from our previous conversation. Use the command tool to print that marker with printf, then give its output. Do not ask me to repeat it.',
                 '请先写出我之前的标记，再用中文连续解释音视频剪辑中如何检查字幕、画面、声音与导出质量，至少600字。只使用普通段落，不用Markdown、列表或工具，不制定执行方案。最后一句必须是：流式输出检查结束。']
        previous=None
        report['selection']='stream_and_cancellation' if args.stream_only else 'cross_harness_stream_and_cancellation'
        for index in ((0,5) if args.stream_only else range(len(prompts))):
            prompt=prompts[index]
            host=hosts[index%2]
            nonce=None
            if index in (2,4):
                nonce='READ_'+uuid.uuid4().hex[:12]
                filename='handoff-check-'+str(index)+'.txt'
                directory=host.store.project_dir(project)/'sources';directory.mkdir(parents=True,exist_ok=True)
                (directory/filename).write_text(nonce+'\n')
                host.store.add_asset(project,uuid.uuid4().hex,filename,filename,{'kind':'text','mime':'text/plain'})
                prompt=('Retrieve the marker from our earlier conversation. Also read the NEW file /workspace/sources/'+filename+
                        ' using a file or command tool; its content was never in the conversation. Reply with both exact values. Do not guess the file contents.')
            if index in (2,4):
                target='deepseek' if index==2 else 'claude'
                switch=request(host.store,project,previous,target,uuid.uuid4().hex,for_task(host.store,previous).digest)
                assert switch['state']=='next_turn',switch
                report['switches'].append({k:switch[k] for k in ('id','state','task_id')})
            task=agent_store.add_turn(host.store,project,'plan',prompt);register(host.store,task,'heavy')
            host.coordinator.heartbeat();lease=host.coordinator.claim(task,'agent');assert lease is not None
            browser=None
            if index==5:
                port=free_port();signals=root/'signals';signals.mkdir()
                log=(root/'browser-api.log').open('wb');logs.append(log)
                process=subprocess.Popen([sys.executable,'-m','tests.api_pressure_server'],env={**os.environ,'VU_PRESSURE_ROOT':str(root/'browser-api'),
                    'VU_PRESSURE_SIGNALS':str(signals),'VU_PRESSURE_PORT':str(port)},stdout=log,stderr=log);children.append(process)
                import requests
                base=f'http://127.0.0.1:{port}'
                for _ in range(150):
                    try:
                        if requests.get(base+'/api/health',timeout=.2).status_code==200:break
                    except requests.RequestException:pass
                    time.sleep(.1)
                else:raise RuntimeError('Isolated browser API did not start')
                from webapp.auth import Sessions
                config={'base':base,'project':project,'task':task,'output':str(root),
                        'cookies':[{'name':'video_use_session','value':Sessions(host.store).sign(browser_identity),'domain':'127.0.0.1','path':'/'}]}
                config_file=root/'browser-private.json';config_file.write_text(json.dumps(config));config_file.chmod(0o600)
                log=(root/'browser-stream.log').open('wb');logs.append(log)
                browser=subprocess.Popen(['node','tests/browser_live_stream.cjs',str(config_file)],stdout=log,stderr=log);children.append(browser)
                for _ in range(150):
                    if (root/'browser-ready').exists():break
                    if browser.poll() is not None:raise RuntimeError('Live browser exited before observation')
                    time.sleep(.1)
                else:raise RuntimeError('Live browser did not become ready')
            started=time.monotonic();host.execute_claim(lease);row=agent_store.turn(host.store,task)
            descriptor=host.store.query('SELECT harness,restore_mode FROM agent_native_sessions WHERE project=?',(project,))[0]
            usage=host.store.query('SELECT category,status,COUNT(*) n FROM usage_calls WHERE task_id=? GROUP BY category,status',(task,))
            tools=[json.loads(r['payload']) for r in host.store.query("SELECT payload FROM agent_events WHERE run_id=? AND kind='tool' ORDER BY seq",(task,))]
            latest_tools={v['id']:v for v in tools}
            stdout_proof=any(marker in v.get('output','') and v['status']=='completed' for v in latest_tools.values())
            item={'task':task,'node':host.identities.node,'status':row['status'],'error':row['error'],'public_reply':row['text'],
                  'marker_retained':marker in row['text'] or stdout_proof,'marker_in_actual_tool_output':stdout_proof,
                  'marker_in_reply':marker in row['text'],'session':descriptor,'calls':usage,
                  'tool_count':len(latest_tools),'tools_closed':all(v['status']!='running' for v in latest_tools.values()),
                  'successful_tools':sum(v['status']=='completed' for v in latest_tools.values()),
                  'new_file_observed':nonce is None or any(nonce in v.get('output','') and v['status']=='completed' for v in latest_tools.values()),
                  'tool_evidence':[{k:v.get(k) for k in ('id','tool_type','status','command','output')} for v in latest_tools.values()],
                  'seconds':round(time.monotonic()-started,2)}
            stream=stream_events.get(task,[]);deltas=[e for e in stream if not e['complete']]
            item['stream']={'incremental_updates':len(deltas),'completed_updates':len(stream)-len(deltas),
                            'first_text_seconds':round(stream[0]['time']-started,3) if stream else None,
                            'incremental_span_seconds':round(deltas[-1]['time']-deltas[0]['time'],3) if deltas else None,
                            'max_incremental_gap_seconds':round(max((b['time']-a['time'] for a,b in zip(deltas,deltas[1:])),default=0),3),
                            'native_incremental_supported':descriptor['harness']!='deepseek_harness'}
            report['turns'].append(item);(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(item,ensure_ascii=False),flush=True)
            assert row['status']=='succeeded' and item['marker_retained'],item
            assert descriptor['restore_mode']==('native_resume' if index in (1,3,5) else 'public_context'),descriptor
            if index in (2,4):assert item['successful_tools']>=1 and item['tools_closed'] and item['new_file_observed'],item
            if browser:
                (root/'browser-done.json').write_text(json.dumps({'text':row['text']}))
                assert browser.wait(timeout=30)==0,'Live browser stream assertion failed; inspect browser-stream.log'
                item['browser']=json.loads((root/'browser-stream-report.json').read_text())
                assert item['stream']['incremental_updates']>=3,item
            previous=task
        from webapp.task_lifecycle import cancel_task
        for name in ('claude','deepseek'):
            host=hosts[0];host.store.execute('INSERT INTO agent_project_profiles VALUES (?,?) ON CONFLICT(project) DO UPDATE SET name=excluded.name',(project,name))
            task=agent_store.add_turn(host.store,project,'plan','请详细解释视频编码原理，至少2000字。不要调用工具。');register(host.store,task,'heavy')
            lease=host.coordinator.claim(task,'agent');assert lease
            runner=threading.Thread(target=host.execute_claim,args=(lease,));runner.start()
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if host.store.query("SELECT call_id FROM usage_calls WHERE task_id=? AND category='llm'",(task,)):break
                if not runner.is_alive():raise AssertionError('Agent ended before cancellation probe')
                time.sleep(.1)
            else:raise AssertionError('No actual model request before cancellation probe')
            began=time.monotonic();cancel_task(hosts[1].store,task);runner.join(timeout=20)
            assert not runner.is_alive(),'Cancelled native process did not stop'
            cleanup=host.store.query('SELECT state FROM execution_cleanup WHERE task_id=?',(task,))
            assert cleanup and all(r['state']=='complete' for r in cleanup),cleanup
            assert not host.store.query('SELECT attempt FROM execution_leases WHERE run_id=? AND active=1',(task,))
            report['cancellations'].append({'harness':name,'task':task,'seconds':round(time.monotonic()-began,3),
                                           'status':agent_store.turn(host.store,task)['status'],'cleanup':cleanup,
                                           'upstream_cancellation_confirmed':False})
        assert len({t['node'] for t in report['turns']})==2
        assert source_digest()==report['source_sha256'],'Source changed during live verification'
        report['passed']=True
    except BaseException as exc:
        report['passed']=False;report['error_type']=type(exc).__name__
        raise
    finally:
        TextPublisher.message=original_message
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=5)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        for log in logs:log.close()
        if s3:s3.close()
        pg.close()
    print(json.dumps({'passed':True,'report':str(root/'report.json'),'turns':len(report['turns'])}))


if __name__=='__main__':main()
