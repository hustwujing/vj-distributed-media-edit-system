"""Real, bounded fault actions confined to the dedicated dynamic QA deployment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from campaign_dynamic_probe import configure,save
from deploy import dynamic_cluster as cluster
from deploy import systemd_templates


def protected_snapshot(root,runtime=cluster):
    """Read a before/after baseline of the explicitly protected deployment."""
    import re
    from urllib.parse import urlsplit
    root=Path(root).resolve(strict=True)
    if root==runtime.REPO.resolve() or root==runtime.ROOT.resolve():raise ValueError('Protected deployment must be distinct')
    record=json.loads((root/'.cluster/release.json').read_text())
    namespace=record['namespace']
    if not re.fullmatch(r'[a-z][a-z0-9-]*',namespace) or namespace==runtime.NAMESPACE:raise ValueError('Invalid protected namespace')
    config=Path(record['config_root']).resolve(strict=True);source=Path(record['source']).resolve(strict=True)
    if config==runtime.CONF.resolve() or not source.is_relative_to(root):raise ValueError('Protected paths are not isolated')
    names=('.env','.agent.env','.storage.env','.distributed.env')
    database=urlsplit(runtime.env_file(config/'.distributed.env')['VIDEO_USE_DATABASE_URL']).path.lstrip('/')
    if not database or database==runtime.DATABASE:raise ValueError('Protected database must be distinct')
    units=[f'{namespace}-{role}@{node}.service' for role in ('api','executor','watchdog','media') for node in ('a','b')]
    units.extend(f'{namespace}-{role}.service' for role in ('orchestrator','observer'))
    return {'root':str(root),'namespace':namespace,'source':str(source),'config_root':str(config),
        'release_sha256':record['sha256'],'database':database,
        'config_hashes':{name:hashlib.sha256((config/name).read_bytes()).hexdigest() for name in names},
        'workspace_config_hashes':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names},
        'units':{name:runtime.run(['systemctl','show',name,'-p','MainPID','-p','ExecMainStartTimestamp','-p','ActiveState']).stdout.strip() for name in units}}


def dependency_probe():
    """Exercise the generated dependency relation using two disposable units."""
    import uuid
    prefix=cluster.NAMESPACE+'-qa-dependency-'+uuid.uuid4().hex[:12]
    provider=prefix+'-db.service';consumer=prefix+'-client.service'
    paths=[Path('/run/systemd/system')/name for name in (provider,consumer)]
    header=systemd_templates.unit_header(cluster.template_config(cluster.release()),'Isolated QA dependency client').split('[Service]',1)[0]
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
    result={'attempts':rows,'at':time.time(),'worker_services':{unit:cluster.active(unit) for unit in cluster.WORKERS}}
    result['business_results']=store.query('''SELECT x.run_id,x.task_id,x.task_revision,x.lease_id,x.outcome,x.status,x.committed_at
        FROM task_results x JOIN runs r ON r.id=x.run_id JOIN run_tasks t ON t.run_id=x.run_id AND t.id=x.task_id
        WHERE r.project_id=? ORDER BY x.created_at LIMIT 400''',(state['project'],))
    result['rejected_task_reviews']=[]
    for row in store.query('''SELECT v.run_id,v.subject_task,v.snapshot_json FROM workflow_reviews v JOIN runs r ON r.id=v.run_id
        WHERE r.project_id=? AND v.purpose='task' AND v.status='rejected' ORDER BY v.created_at LIMIT 200''',(state['project'],)):
        subject=next((task for task in json.loads(row['snapshot_json']).get('tasks',[]) if task['id']==row['subject_task']),None)
        if subject:result['rejected_task_reviews'].append({'run_id':row['run_id'],'task_id':subject['id'],'revision':subject['revision']})
    result['provider_calls']=[]
    for row in store.query('SELECT p.request_key,p.step,p.attempt_id,p.generation,p.operation,p.state,p.external_id,p.implementation FROM workflow_provider_calls p JOIN runs r ON r.id=p.workflow WHERE r.project_id=?',(state['project'],)):
        binding=json.loads(row.pop('implementation'))
        result['provider_calls'].append({**row,**{name:binding.get(name) for name in ('provider_identity','route_adapter','budget_group','concurrency_group')},
            'implementation_digest':hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()})
    result['submitting']=store.query("SELECT p.request_key,p.attempt_id,p.operation FROM workflow_provider_calls p JOIN runs w ON w.id=p.workflow WHERE w.project_id=? AND p.state='submitting'",(state['project'],))
    result['staged_results']=store.query("SELECT COUNT(*) AS n FROM task_results WHERE run_id IN (SELECT id FROM runs WHERE project_id=?) AND status='staged'",(state['project'],))[0]['n']
    result['staged_business_success']=store.query("SELECT COUNT(*) AS n FROM task_results r JOIN run_tasks t ON t.run_id=r.run_id AND t.id=r.task_id JOIN runs w ON w.id=r.run_id WHERE w.project_id=? AND r.status='staged' AND r.outcome='succeeded' AND t.selected=1",(state['project'],))[0]['n']
    result['quiescent']=not store.query("SELECT l.attempt FROM execution_leases l JOIN runs w ON w.id=l.run_id WHERE w.project_id=? AND l.active=1 AND l.expires>?",(state['project'],store.now())) and not store.query("SELECT c.attempt FROM execution_cleanup c JOIN runs w ON w.id=c.task_id WHERE w.project_id=? AND c.state<>'complete'",(state['project'],))
    result['diagnostics']=[json.loads(row['record']) for row in store.query('SELECT d.record FROM workflow_diagnostics d JOIN runs w ON w.id=d.workflow WHERE w.project_id=? ORDER BY d.created DESC LIMIT 20',(state['project'],))]
    result['model_reservations']=store.query('''SELECT m.attempt_id,m.reserved_tokens,m.used_tokens,m.state
        FROM workflow_model_reservations m JOIN runs r ON r.id=m.run_id WHERE r.project_id=? ORDER BY m.created_at''',(state['project'],))
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


def frozen_attempt_status(store,project,unit,attempt):
    # No Store.now(), initialization or global progress probe while a worker
    # may be suspended holding the write-transaction advisory lock.
    rows=store.query('''SELECT a.id,a.status FROM execution_attempts a
        JOIN runs r ON r.id=a.run_id WHERE a.id=? AND r.project_id=?''',(attempt,project))
    return {'attempts':[{**row,'unit':unit} for row in rows]}


def kill_frozen_attempt(unit,attempt,observe,runtime=cluster):
    """Freeze the worker before rechecking: a completed poll is not a kill."""
    if unit not in runtime.WORKERS:raise ValueError('Worker outside dedicated deployment')
    runtime.run(['systemctl','kill','--kill-who=main','--signal=SIGSTOP',unit])
    try:
        assigned=next((row for row in observe()['attempts'] if row['unit']==unit
            and row['id']==attempt and row['status']=='running'),None)
        if not assigned:raise ValueError('No replay-safe QA task is running on this worker')
        runtime.run(['systemctl','kill','--kill-who=main','--signal=SIGKILL',unit])
        runtime.run(['systemctl','stop',unit],timeout=150)
        return assigned
    finally:
        runtime.run(['systemctl','kill','--kill-who=main','--signal=SIGCONT',unit],check=False)


def act(directory,action,unit=None,attempt=None):
    directory,state,release,store=configure(directory)
    restoring=action.startswith('start_') or action in ('stop_second_orchestrator','restore_object','release_workers','restore_provider_route')
    if not restoring:
        other=store.query("SELECT id FROM runs WHERE project_id<>? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')",(state['project'],))
        if other:raise ValueError('Fault injection requires other workflows to be quiescent')
        if (release['sha256'],release.get('config_sha256'))!=(state['release']['sha256'],state['release'].get('config_sha256')):
            raise ValueError('Deployment source or configuration changed during this case')
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
    elif action in ('stop_worker','kill_attempt_worker','start_worker'):
        if unit not in cluster.WORKERS:raise ValueError('Worker outside dedicated deployment')
        if action=='kill_attempt_worker':
            observed=progress(directory)
            if not any(row['unit']==unit and row['id']==attempt and row['status']=='running' for row in observed['attempts']):
                raise ValueError('No replay-safe QA task is running on this worker')
            kill_frozen_attempt(unit,attempt,lambda:frozen_attempt_status(store,state['project'],unit,attempt))
        elif action=='stop_worker':
            observed=progress(directory);submitting={p['attempt_id'] for p in observed['submitting']}
            assigned=[row for row in observed['attempts'] if row['unit']==unit and row['status']=='running' and not row['control_kind']
                      and (row['retry_safe'] or row['id'] in submitting)]
            if not assigned:raise ValueError('No replay-safe QA task is running on this worker')
            cluster.run(['systemctl','kill','--kill-who=main','--signal=SIGKILL',unit])
            cluster.run(['systemctl','stop',unit],timeout=150)
        else:
            cluster.run(['systemctl','start',unit])
            for _ in range(20):
                if cluster.active(unit):
                    time.sleep(1)
                    if cluster.active(unit):break
                time.sleep(.25)
            if not cluster.active(unit):raise RuntimeError('Worker did not remain active after restart: '+unit)
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
    elif action=='delete_input_asset':
        if state['case']!='D48':raise ValueError('Attachment deletion is reserved for D48')
        rows=store.query("""SELECT i.id,i.attachments_json FROM run_inputs i JOIN runs r ON r.id=i.run_id
            WHERE r.project_id=? AND i.status='ACCEPTED' ORDER BY i.sequence DESC LIMIT 1""",(state['project'],))
        if not rows:raise ValueError('No accepted input attachment to delete')
        attachments=json.loads(rows[0]['attachments_json'])
        if len(attachments)!=1:raise ValueError('D48 requires exactly one accepted attachment')
        asset=attachments[0]['id']
        if not store.execute('DELETE FROM assets WHERE id=? AND project=?',(asset,state['project'])):
            raise ValueError('Attachment is no longer owned by this QA project')
        result={'action':action,'input_id':rows[0]['id'],'asset_id':asset}
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
        prefix=os.environ.get('DYNAMIC_EXPECT_OBJECT_PREFIX','distributed-agent-system')
        if not key.startswith(prefix+'/projects/'+state['project']+'/'):raise ValueError('Object outside QA scope')
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
        prefix=os.environ.get('DYNAMIC_EXPECT_OBJECT_PREFIX','distributed-agent-system')
        if hashlib.sha256(raw).hexdigest()!=info['sha256'] or not info['object_key'].startswith(prefix+'/projects/'+state['project']+'/'):raise ValueError('Invalid QA backup')
        backend=store.artifacts.backend;backend.client.put_object(Bucket=backend.bucket,Key=info['object_key'],Body=raw)
        (directory/'private-object-backup.json').unlink();(directory/'private-object-backup.bin').unlink()
        result={'action':action,'artifact_id':info['artifact_id']}
    elif action=='isolation':
        import psycopg
        protected=os.environ.get('DYNAMIC_PROTECTED_ROOT')
        if not protected:raise ValueError('DYNAMIC_PROTECTED_ROOT must identify the protected source deployment')
        baseline=protected_snapshot(protected)
        baseline_file=directory/'protected-baseline.json'
        if baseline_file.exists():raise ValueError('Do not replace an existing isolation-check baseline')
        save(baseline_file,{'scope':'current_check_only','captured_at':time.time(),**baseline})
        config=Path(baseline['config_root'])
        other=psycopg.conninfo.conninfo_to_dict(cluster.env_file(config/'.distributed.env')['VIDEO_USE_DATABASE_URL'])
        own=psycopg.conninfo.conninfo_to_dict(store.dsn)
        other.update(user=own['user'],password=own['password'],connect_timeout=5,
                     options='-c default_transaction_read_only=on -c statement_timeout=5000')
        denied=False
        try:
            with psycopg.connect(**other) as connection:connection.execute('SELECT 1 FROM public.projects LIMIT 0')
        except psycopg.Error as exc:denied=exc.sqlstate in ('42501','28000','28P01')
        own_storage=cluster.env_file(Path(release['config_root'])/'.storage.env');protected_storage=cluster.env_file(config/'.storage.env')
        result={'action':action,'protected_root':baseline['root'],'baseline_scope':'current_check_only','protected_database_read_denied':denied,
                'separate_encryption_key':own_storage['VIDEO_USE_OBJECT_ENCRYPTION_KEY']!=protected_storage['VIDEO_USE_OBJECT_ENCRYPTION_KEY'],
                'separate_object_prefix':own_storage['VIDEO_USE_OBJECT_PREFIX']!=protected_storage.get('VIDEO_USE_OBJECT_PREFIX')}
        result['shared_db_dependencies']={}
        for name in cluster.APIS+cluster.WORKERS+cluster.REAPERS:
            props=cluster.run(['systemctl','show',name,'-p','Requires','-p','Wants']).stdout
            values=dict(line.split('=',1) for line in props.splitlines() if '=' in line)
            result['shared_db_dependencies'][name]={'requires':values.get('Requires','').split(),'wants':values.get('Wants','').split()}
        result['shared_db_dependency_independent']=all('video-use-db.service' not in p['requires'] and 'video-use-db.service' in p['wants'] for p in result['shared_db_dependencies'].values())
        result['dependency_lifecycle_probe']=dependency_probe()
        after=protected_snapshot(protected)
        result['protected_config_unchanged']=all(after[key]==baseline[key] for key in ('source','config_root','release_sha256','config_hashes','workspace_config_hashes','database'))
        result['protected_processes_unchanged_during_check']=after['units']==baseline['units']
    else:raise ValueError('Unknown fault action')
    result.update(before=before,after={name:cluster.active(name) for name in cluster.SERVICES},at=time.time())
    target=directory/('fault-'+str(time.time_ns())+'.json');save(target,result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action');parser.add_argument('directory',type=Path);parser.add_argument('--unit');parser.add_argument('--attempt')
    args=parser.parse_args()
    print(json.dumps(progress(args.directory) if args.action=='progress' else act(args.directory,args.action,args.unit,args.attempt),ensure_ascii=False))


if __name__=='__main__':main()
