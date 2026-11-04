"""Real, bounded fault actions confined to the dedicated dynamic QA deployment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from campaign_dynamic_probe import configure,save
from deploy import dynamic_cluster as cluster


def dependency_probe():
    """Exercise the generated dependency relation using two disposable units."""
    import uuid
    prefix=cluster.NAMESPACE+'-qa-dependency-'+uuid.uuid4().hex[:12]
    provider=prefix+'-db.service';consumer=prefix+'-client.service'
    paths=[Path('/run/systemd/system')/name for name in (provider,consumer)]
    header=cluster.unit_text(cluster.templates.unit_header('Isolated QA dependency client')).split('[Service]',1)[0]
    header=header.replace('video-use-db.service',provider)
    service='[Service]\nType=exec\nExecStart=/usr/bin/sleep 120\nDynamicUser=yes\nPrivateNetwork=yes\nNoNewPrivileges=yes\nProtectSystem=strict\n'
    try:
        for path,text in zip(paths,('[Unit]\nDescription=Isolated QA dependency provider\n'+service,header+service)):
            with path.open('x') as stream:stream.write(text)
            path.chmod(0o644)
        cluster.run(['systemctl','daemon-reload'])
        cluster.run(['systemctl','start',provider,consumer])
        pid=cluster.run(['systemctl','show',consumer,'-p','MainPID','--value']).stdout.strip()
        cluster.run(['systemctl','stop',provider])
        survived=cluster.active(consumer)
        after_stop=cluster.run(['systemctl','show',consumer,'-p','MainPID','--value']).stdout.strip()
        cluster.run(['systemctl','start',provider])
        after_start=cluster.run(['systemctl','show',consumer,'-p','MainPID','--value']).stdout.strip()
        return {'provider':provider,'consumer':consumer,'consumer_pid_before':pid,
                'consumer_survived_dependency_stop':survived,
                'consumer_pid_after_stop':after_stop,'consumer_pid_after_recovery':after_start,
                'consumer_pid_unchanged':pid!='0' and pid==after_stop==after_start,
                'provider_recovered':cluster.active(provider),'main_database_touched':False}
    finally:
        cluster.run(['systemctl','stop',consumer,provider],check=False)
        for path in paths:path.unlink(missing_ok=True)
        cluster.run(['systemctl','daemon-reload'])
        cluster.run(['systemctl','reset-failed',consumer,provider],check=False)


def progress(directory):
    directory,state,release,store=configure(directory)
    rows=store.query('''SELECT a.id,a.run_id,a.step_id,a.task_revision,a.generation,a.status,a.started,a.finished,a.uid,w.node,
        t.type,t.control_kind,t.spec_json FROM execution_attempts a JOIN execution_workers w ON w.id=a.worker
        JOIN runs r ON r.id=a.run_id JOIN run_work_items t ON t.run_id=r.id AND t.id=a.step_id
        WHERE r.project_id=? ORDER BY a.started DESC LIMIT 200''',(state['project'],))
    for row in rows:
        spec=json.loads(row.pop('spec_json'));row['retry_safe']=spec.get('retry_policy')=='safe_once'
        row['unit']=None
        for node in cluster.PORTS:
            path=cluster.registry(node)/'node-id'
            if path.exists() and path.read_text().strip()==row['node']:row['unit']=f'{cluster.NAMESPACE}-executor@{node}.service'
    result={'attempts':rows,'at':time.time()}
    result['business_results']=store.query('''SELECT x.run_id,x.task_id,x.task_revision,x.lease_id,x.outcome,x.status,x.committed_at
        FROM task_results x JOIN runs r ON r.id=x.run_id JOIN run_tasks t ON t.run_id=x.run_id AND t.id=x.task_id
        WHERE r.project_id=? ORDER BY x.created_at LIMIT 400''',(state['project'],))
    result['rejected_task_reviews']=[]
    for row in store.query('''SELECT v.run_id,v.subject_task,v.snapshot_json FROM workflow_reviews v JOIN runs r ON r.id=v.run_id
        WHERE r.project_id=? AND v.purpose='task' AND v.status='rejected' ORDER BY v.created_at LIMIT 200''',(state['project'],)):
        subject=next((task for task in json.loads(row['snapshot_json']).get('tasks',[]) if task['id']==row['subject_task']),None)
        if subject:result['rejected_task_reviews'].append({'run_id':row['run_id'],'task_id':subject['id'],'revision':subject['revision']})
    result['provider_calls']=[]
    for row in store.query('SELECT p.request_key,p.operation,p.state,p.external_id,p.implementation FROM workflow_provider_calls p JOIN runs r ON r.id=p.workflow WHERE r.project_id=?',(state['project'],)):
        binding=json.loads(row.pop('implementation'))
        result['provider_calls'].append({**row,**{name:binding.get(name) for name in ('provider_identity','route_adapter','budget_group','concurrency_group')},
            'implementation_digest':hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()})
    result['submitting']=store.query("SELECT p.request_key,p.attempt_id,p.operation FROM workflow_provider_calls p JOIN runs w ON w.id=p.workflow WHERE w.project_id=? AND p.state='submitting'",(state['project'],))
    result['staged_results']=store.query("SELECT COUNT(*) AS n FROM task_results WHERE run_id IN (SELECT id FROM runs WHERE project_id=?) AND status='staged'",(state['project'],))[0]['n']
    result['staged_business_success']=store.query("SELECT COUNT(*) AS n FROM task_results r JOIN run_tasks t ON t.run_id=r.run_id AND t.id=r.task_id JOIN runs w ON w.id=r.run_id WHERE w.project_id=? AND r.status='staged' AND r.outcome='succeeded' AND t.selected=1",(state['project'],))[0]['n']
    result['quiescent']=not store.query("SELECT l.attempt FROM execution_leases l JOIN runs w ON w.id=l.run_id WHERE w.project_id=? AND l.active=1 AND l.expires>?",(state['project'],store.now())) and not store.query("SELECT c.attempt FROM execution_cleanup c JOIN runs w ON w.id=c.task_id WHERE w.project_id=? AND c.state<>'complete'",(state['project'],))
    result['diagnostics']=[json.loads(row['record']) for row in store.query('SELECT d.record FROM workflow_diagnostics d JOIN runs w ON w.id=d.workflow WHERE w.project_id=? ORDER BY d.created DESC LIMIT 20',(state['project'],))]
    result['orchestrators']=store.query('SELECT id,heartbeat,draining FROM workflow_orchestrators WHERE heartbeat>?',(store.now()-20,))
    if state['case']=='D14':
        from webapp.diagnostics import diagnose
        result['integrity_errors']=[event for item in store.query('SELECT id AS run_id FROM runs WHERE project_id=?',(state['project'],)) for event in diagnose(store,item['run_id'],include_logs=True).get('facts',{}).get('latest_errors',[]) if any(frame.get('file')=='object_encryption.py' and frame.get('function')=='decrypt' for frame in event.get('stack',[]))]

    result['output_ready_events']=store.query("SELECT seq,run_id,created,payload FROM agent_events WHERE run_id IN (SELECT id FROM runs WHERE project_id=?) AND kind='progress' AND payload LIKE ? ORDER BY seq",
        (state['project'],'%QA_EXPORT_OUTPUT_READY%'))
    result['output_ready_checkpoints']=[]
    if state['case']=='R14':
        for checkpoint in store.query('''SELECT c.workflow,c.step,c.revision,c.manifest FROM execution_step_checkpoints c
            JOIN runs r ON r.id=c.workflow JOIN run_tasks t ON t.run_id=r.id AND t.id=c.step AND t.revision=c.revision
            WHERE r.project_id=? AND t.id='export' AND t.status='running' ''',(state['project'],)):
            for name,record in json.loads(checkpoint['manifest']).items():
                if Path(name).name=='final.png' and record.get('artifact') and record.get('size',0)>0:
                    result['output_ready_checkpoints'].append({key:checkpoint[key] for key in ('workflow','step','revision')}|
                        {'path':name,'artifact_id':record['artifact'],'sha256':record['sha256'],'size':record['size']})
    return result


def act(directory,action,unit=None):
    directory,state,release,store=configure(directory)
    restoring=action.startswith('start_') or action in ('stop_second_orchestrator','restore_object','release_workers','restore_provider_route')
    if not restoring:
        other=store.query("SELECT id FROM runs WHERE project_id<>? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')",(state['project'],))
        if other:raise ValueError('Fault injection requires other workflows to be quiescent')
        if release['sha256']!=state['release']['sha256']:raise ValueError('Deployment changed during this case')
    before={name:cluster.active(name) for name in cluster.SERVICES}
    identifier=hashlib.sha256(state['run_id'].encode()).hexdigest()[:12]
    second=cluster.NAMESPACE+'-qa-orchestrator-'+identifier+'.service'
    actions={'stop_orchestrator':('stop',cluster.ORCHESTRATOR),'start_orchestrator':('start',cluster.ORCHESTRATOR),
             'restart_orchestrator':('restart',cluster.ORCHESTRATOR),'stop_observer':('stop',cluster.OBSERVER),
             'start_observer':('start',cluster.OBSERVER),'restart_observer':('restart',cluster.OBSERVER),
             'restart_api':('restart',cluster.APIS[0]),'stop_api':('stop',cluster.APIS[0]),'start_api':('start',cluster.APIS[0]),
             'stop_second_orchestrator':('stop',second)}
    if action in actions:
        command,target=actions[action];cluster.run(['systemctl',command,target],timeout=150)
        result={'action':action,'unit':target}
    elif action in ('drain_workers','release_workers'):
        if action=='drain_workers' and not progress(directory)['quiescent']:raise ValueError('Worker hold requires completed lease cleanup')
        cluster.run(['systemctl','stop' if action=='drain_workers' else 'start',*cluster.WORKERS],timeout=150)
        result={'action':action,'units':cluster.WORKERS}
    elif action=='start_second_orchestrator':
        source=Path(release['source'])
        cluster.run(['systemd-run','--unit',second,'--property=User='+cluster.NAMESPACE+'-orch',
            '--property=Group='+cluster.NAMESPACE+'-orch','--property=WorkingDirectory='+str(source),
            '--property=EnvironmentFile='+str(cluster.CONF/'orchestrator.env'),'--property=LogNamespace='+cluster.NAMESPACE,
            '--property=NoNewPrivileges=yes','--property=ProtectSystem=strict',
            '--property=ReadWritePaths='+str(cluster.ROOT/'orchestrator'),
            '--setenv=VIDEO_USE_RELEASE='+source.name,'--setenv=VIDEO_USE_INSTANCE='+second,
            source/'.venv/bin/python','-m','webapp.workflow_orchestrator'])
        result={'action':action,'unit':second}
    elif action in ('stop_worker','start_worker'):
        if unit not in cluster.WORKERS:raise ValueError('Worker outside dedicated deployment')
        if action=='stop_worker':
            observed=progress(directory);submitting={p['attempt_id'] for p in observed['submitting']}
            assigned=[row for row in observed['attempts'] if row['unit']==unit and row['status']=='running' and not row['control_kind']
                      and (row['retry_safe'] or state['case'] in ('D16','D27') and row['id'] in submitting)]
            if not assigned:raise ValueError('No replay-safe QA task is running on this worker')
            cluster.run(['systemctl','kill','--kill-who=main','--signal=SIGKILL',unit])
            cluster.run(['systemctl','stop',unit],timeout=150)
        else:cluster.run(['systemctl','start',unit])
        result={'action':action,'unit':unit}
    elif action in ('switch_provider_route','restore_provider_route'):
        if state['case']!='D35':raise ValueError('Route fault is reserved for the isolated frozen-route case')
        env=directory/'private-route-override.env'
        paths=[Path('/etc/systemd/system')/(unit+'.d')/'90-dynamic-qa-route.conf' for unit in cluster.WORKERS]
        expected='[Service]\nEnvironmentFile='+str(env)+'\n'
        if action=='switch_provider_route':
            calls=progress(directory)['provider_calls']
            if len(calls)!=1 or calls[0]['provider_identity']!='wan' or not calls[0]['external_id']:
                raise ValueError('Route switch requires one already-submitted actual Wan request')
            if any(path.exists() for path in paths):raise ValueError('A route override already exists')
            env.write_text('VIDEO_USE_IMAGE_VIDEO_URL=http://127.0.0.1:1\n');env.chmod(0o600)
            created=[]
            try:
                for path in paths:
                    path.parent.mkdir(exist_ok=True);path.write_text(expected);path.chmod(0o644);created.append(path)
                cluster.run(['systemctl','daemon-reload'])
                cluster.run(['systemctl','restart',*cluster.WORKERS],timeout=150)
            except BaseException:
                for path in created:path.unlink(missing_ok=True)
                cluster.run(['systemctl','daemon-reload']);cluster.run(['systemctl','restart',*cluster.WORKERS],timeout=150)
                raise
        else:
            if any(path.exists() and path.read_text()!=expected for path in paths):raise ValueError('Route override ownership changed')
            for path in paths:path.unlink(missing_ok=True)
            env.unlink(missing_ok=True)
            cluster.run(['systemctl','daemon-reload']);cluster.run(['systemctl','restart',*cluster.WORKERS],timeout=150)
        result={'action':action,'units':cluster.WORKERS,'override':'unreachable_loopback','original_provider_receipt_retained':True}
    elif action=='corrupt_object':
        rows=store.query("""SELECT c.manifest,t.id,t.accepted_result_json FROM execution_step_checkpoints c JOIN run_tasks t
            ON t.run_id=c.workflow AND t.id=c.step AND t.revision=c.revision
            JOIN runs w ON w.id=t.run_id WHERE w.project_id=? AND t.status='succeeded'
            AND t.selected=1 ORDER BY t.updated_at LIMIT 1""",(state['project'],))
        if not rows:raise ValueError('No accepted QA object to corrupt')
        records=json.loads(rows[0]['manifest'])
        accepted=json.loads(rows[0]['accepted_result_json'])
        path,record=next((name,record) for name,record in records.items() if name in accepted and name.endswith(('.png','.txt','.json')))
        artifact=store.artifacts.record(state['project'],record['artifact']);key=artifact['object_key']
        if not key.startswith('distributed-agent-system/projects/'+state['project']+'/'):raise ValueError('Object outside QA scope')
        backend=store.artifacts.backend;response=backend.client.get_object(Bucket=backend.bucket,Key=key)
        try:raw=response['Body'].read(16*1024**2+1)
        finally:response['Body'].close()
        if len(raw)>16*1024**2 or not raw:raise ValueError('QA corruption target exceeds size limit')
        backup=directory/'private-object-backup.bin';backup.write_bytes(raw);backup.chmod(0o600)
        save(directory/'private-object-backup.json',{'artifact_id':artifact['id'],'object_key':key,'sha256':hashlib.sha256(raw).hexdigest()})
        backend.client.put_object(Bucket=backend.bucket,Key=key,Body=raw[:-1]+bytes([raw[-1]^1]))
        # Remove only this project's materialized copies; the next node must
        # verify bytes from the real encrypted object service.
        import shutil
        for node in cluster.PORTS:
            for part in ('cache/objects','projects'):
                cache=cluster.data('node',node)/part/state['project']
                if cache.exists():shutil.rmtree(cache)
        result={'action':action,'artifact_id':artifact['id'],'logical_path':path,'task_id':rows[0]['id'],'corruption':'one bit in the final GCM authentication tag; header/length unchanged','original_cipher_sha256':hashlib.sha256(raw).hexdigest(),'corrupt_cipher_sha256':hashlib.sha256(raw[:-1]+bytes([raw[-1]^1])).hexdigest()}
    elif action=='restore_object':
        info=json.loads((directory/'private-object-backup.json').read_text());raw=(directory/'private-object-backup.bin').read_bytes()
        if hashlib.sha256(raw).hexdigest()!=info['sha256'] or not info['object_key'].startswith('distributed-agent-system/projects/'+state['project']+'/'):raise ValueError('Invalid QA backup')
        backend=store.artifacts.backend;backend.client.put_object(Bucket=backend.bucket,Key=info['object_key'],Body=raw)
        (directory/'private-object-backup.json').unlink();(directory/'private-object-backup.bin').unlink()
        result={'action':action,'artifact_id':info['artifact_id']}
    elif action=='isolation':
        from urllib.parse import urlsplit,urlunsplit
        import psycopg
        baseline=json.loads((cluster.ROOT/'main-baseline.json').read_text())
        main_before={name:cluster.run(['systemctl','show',name,'-p','MainPID','-p','ExecMainStartTimestamp','-p','ActiveState']).stdout.strip() for name in baseline['units']}
        original=urlsplit(store.dsn);other=urlunsplit(original._replace(path='/video_use'))
        denied=False
        try:
            with psycopg.connect(other,connect_timeout=5) as connection:connection.execute('SELECT count(*) FROM projects')
        except psycopg.Error as exc:denied=exc.sqlstate in ('42501','28000','28P01')
        main=Path('/data2/wujing03/video-use')
        current={name:hashlib.sha256((main/name).read_bytes()).hexdigest() for name in ('.env','.agent.env','.storage.env','.distributed.env')}
        own_storage=cluster.env_file(cluster.REPO/'.storage.env');main_storage=cluster.env_file(main/'.storage.env')
        result={'action':action,'main_database_read_denied':denied,'main_config_unchanged':current==baseline['config_hashes'],
                'separate_encryption_key':own_storage['VIDEO_USE_OBJECT_ENCRYPTION_KEY']!=main_storage['VIDEO_USE_OBJECT_ENCRYPTION_KEY'],
                'separate_object_prefix':own_storage['VIDEO_USE_OBJECT_PREFIX']!=main_storage.get('VIDEO_USE_OBJECT_PREFIX'),
                'main_services':{name:cluster.active(name) for name in baseline['units']}}
        result['main_processes_unchanged']=all(cluster.run(['systemctl','show',name,'-p','MainPID','-p','ExecMainStartTimestamp','-p','ActiveState']).stdout.strip()==value for name,value in baseline['units'].items())
        result['main_processes_unchanged_during_check']=all(cluster.run(['systemctl','show',name,'-p','MainPID','-p','ExecMainStartTimestamp','-p','ActiveState']).stdout.strip()==value for name,value in main_before.items())
        result['shared_db_dependencies']={}
        for name in cluster.APIS+cluster.WORKERS+cluster.REAPERS:
            props=cluster.run(['systemctl','show',name,'-p','Requires','-p','Wants']).stdout
            values=dict(line.split('=',1) for line in props.splitlines() if '=' in line)
            result['shared_db_dependencies'][name]={'requires':values.get('Requires','').split(),'wants':values.get('Wants','').split()}
        result['shared_db_dependency_independent']=all('video-use-db.service' not in p['requires'] and 'video-use-db.service' in p['wants'] for p in result['shared_db_dependencies'].values())
        result['dependency_lifecycle_probe']=dependency_probe()
    else:raise ValueError('Unknown fault action')
    result.update(before=before,after={name:cluster.active(name) for name in cluster.SERVICES},at=time.time())
    target=directory/('fault-'+str(time.time_ns())+'.json');save(target,result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action');parser.add_argument('directory',type=Path);parser.add_argument('--unit')
    args=parser.parse_args()
    print(json.dumps(progress(args.directory) if args.action=='progress' else act(args.directory,args.action,args.unit),ensure_ascii=False))


if __name__=='__main__':main()
