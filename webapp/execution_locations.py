"""Public execution locations derived from durable attempt/resource evidence."""


def for_run(store,run):
    # One query for the whole Run. Match the revision and latest generation so
    # a previous sandbox cannot be presented as the new attempt's environment.
    records=store.query('''WITH ranked AS (
        SELECT id,step_id,task_revision,kind,runtime,uid,
               ROW_NUMBER() OVER (PARTITION BY step_id,task_revision
                                  ORDER BY generation DESC,started DESC,id DESC) AS position
        FROM execution_attempts WHERE run_id=?
    ) SELECT a.*,c.id AS cleanup_id,c.external_id AS sandbox_id,p.harness_id AS actual_harness
      FROM ranked a LEFT JOIN execution_external_cleanup c
        ON c.id=? || a.id AND c.task_id=?
      LEFT JOIN specialist_attempt_profiles p ON p.attempt_id=a.id
      WHERE a.position=1''',(run['id'],'opensandbox:',run['id']))
    attempts={(row['step_id'],row['task_revision']):row for row in records}
    providers={(row['step'],row['revision']):row['state'] for row in store.query(
        'SELECT step,revision,state FROM workflow_provider_calls WHERE workflow=?',(run['id'],))}
    locations={}
    for item in [*run['tasks'],*run['control_jobs']]:
        attempt=attempts.get((item['id'],item['revision']))
        agent=bool(item.get('control_kind') or item['type']=='agent')
        location='unallocated' if not item['attempts'] else 'unrecorded'
        tool='unrecorded'
        if attempt:
            location='unrecorded'
            # The cleanup receipt survives deletion; it proves an instance was
            # allocated even when no container is currently running.
            if attempt['sandbox_id']:
                location=tool='opensandbox'
            elif attempt['cleanup_id']:
                location=tool='opensandbox_unconfirmed'
            elif attempt['runtime'] and attempt['uid'] is not None:
                location=tool='bwrap_sandbox'
            if agent and attempt['kind']=='workflow' and (item.get('profile') or {}).get('harness')!='native-implementer-v1':
                # Structured experts execute on the worker; only their tools
                # enter the allocated runtime (including remote command tools).
                location='agent_worker'
            elif not agent and location=='unrecorded' and item['type']=='operation':
                from .operations import get
                try:operation=get(item['spec']['operation'],item['spec'].get('implementation'))
                except (KeyError,ValueError):pass
                else:
                    if operation.external_side_effects or operation.execution_mode=='host_control':
                        location='executor_host'
        value={'execution_location':location,'execution_attempt_id':attempt['id'] if attempt else None}
        if attempt and attempt.get('actual_harness'):value['execution_harness']=attempt['actual_harness']
        selected=item.get('spec',{}).get('runtime_requirements',{}).get('native_skills',[])
        if selected and attempt and attempt.get('actual_harness') and location=='opensandbox':
            from .native_skill_catalog import manifest,validate
            validate(selected);value['native_skills']=list(selected)
            value['native_skill_bundle_digest']=manifest()['bundle_digest']
        if agent and item.get('control_kind')!='MESSAGE':value['tool_location']=tool if attempt else location
        provider=providers.get((item['id'],item['revision']))
        if provider:value['provider_execution_state']=provider
        locations[item['id']]=value
    return locations
