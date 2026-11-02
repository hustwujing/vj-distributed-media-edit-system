"""Finite API capacity telemetry schema shared by producers and the collector."""
import math

POOL_FIELDS=('active','queued','workers','queue_capacity','submitted','completed','failed','rejected',
             'queue_timeout','execution_timeout','cancelled','wait_seconds','run_seconds',
             'wait_max_seconds','run_max_seconds','oldest_active_seconds','oldest_queued_seconds')
GATE_FIELDS=('active','queued','capacity','rejected')
LOOP_FIELDS=('lag_seconds','default_threads_used','default_threads_limit')
COUNTERS={'submitted','completed','failed','rejected','queue_timeout','execution_timeout','cancelled','wait_seconds','run_seconds'}
GATES={'upload','image_import','demo','delete','stream'}
FIELDS={*(f'api_pool_{f}' for f in POOL_FIELDS),*(f'api_gate_{f}' for f in GATE_FIELDS),*(f'api_loop_{f}' for f in LOOP_FIELDS)}


def metric_key(body):
    if body.get('service')!='api' or not body.get('instance'):return None
    event=body.get('event');name=body.get('operation')
    if event=='api.pool.snapshot':
        from .api_execution import DEFAULTS
        if name not in DEFAULTS:return None
        prefix,fields,key='api_pool_',POOL_FIELDS,'pool:'+name
    elif event=='api.gate.snapshot' and name in GATES:prefix,fields,key='api_gate_',GATE_FIELDS,'gate:'+name
    elif event=='api.loop.snapshot':prefix,fields,key='api_loop_',LOOP_FIELDS,'loop'
    else:return None
    if any(type(body.get(prefix+field)) not in (int,float) or not math.isfinite(body[prefix+field]) or body[prefix+field]<0 for field in fields):return None
    return key


def views(records):
    result={'pools':[],'gates':[],'loops':[]}
    for body in records:
        key=metric_key(body)
        if not key:continue
        family=key.split(':')[0];prefix,fields={'pool':('api_pool_',POOL_FIELDS),'gate':('api_gate_',GATE_FIELDS),'loop':('api_loop_',LOOP_FIELDS)}[family]
        value={'instance':body['instance'],'release':body.get('release'),'at':body['time'],**{f:body[prefix+f] for f in fields}}
        if family!='loop':value['name']=body['operation']
        result[family+'s'].append(value)
    result['scope']='Current API instance snapshots; counters/sums/maxima are since process start. Missing or stale instances are unknown.'
    from .api_execution import DEFAULTS
    instances={row['instance'] for key in ('pools','gates','loops') for row in result[key]}
    result['gaps']=[];result['complete_instances']=[]
    for instance in sorted(instances):
        pools=set(DEFAULTS)-{r['name'] for r in result['pools'] if r['instance']==instance}
        gates=GATES-{r['name'] for r in result['gates'] if r['instance']==instance}
        loop=any(r['instance']==instance for r in result['loops'])
        if pools or gates or not loop:result['gaps'].append({'instance':instance,'missing_pools':sorted(pools),'missing_gates':sorted(gates),'missing_loop':not loop})
        else:result['complete_instances'].append(instance)
    return result


def exposition(capacity):
    from .telemetry import identifier
    lines=[]
    for row in capacity.get('pools',[]):
        labels=f'api="{identifier(row["instance"])}",pool="{identifier(row["name"])}"'
        for field in POOL_FIELDS:
            suffix=field+('_total' if field in COUNTERS else '')
            lines.append(f'video_use_api_pool_{suffix}{{{labels}}} {row[field]}')
    for row in capacity.get('gates',[]):
        labels=f'api="{identifier(row["instance"])}",gate="{identifier(row["name"])}"'
        for field in GATE_FIELDS:lines.append(f'video_use_api_gate_{field}{{{labels}}} {row[field]}')
    for row in capacity.get('loops',[]):
        labels=f'api="{identifier(row["instance"])}"'
        for field in LOOP_FIELDS:lines.append(f'video_use_api_loop_{field}{{{labels}}} {row[field]}')
    return lines
