"""Delivery-list amendments require durable user approval, not model prose."""
import json


def confirmed_scope(store,run):
    targets=run['authorization']['delivery_targets']
    if not targets:return None
    grants={row['version']:json.loads(row['authorization_json']) for row in store.query(
        'SELECT version,authorization_json FROM run_authorizations WHERE run_id=?',(run['id'],))}
    contract=run['authorization'].get('delivery_contract',{})
    for row in store.query("SELECT id,kind,request_json,authorization_version FROM workflow_decisions WHERE run_id=? AND status='applied' AND kind IN ('delivery_scope_change','plan_change','plan_confirmation') ORDER BY authorization_version DESC,created_at DESC,id DESC",(run['id'],)):
        request=json.loads(row['request_json']);version=row['authorization_version']+1
        if row['kind']!='delivery_scope_change' and not request.get('delivery_scope_changed'):continue
        grant=grants.get(version,{})
        if set(grant.get('delivery_targets',[]))!=set(targets) or grant.get('delivery_contract',{})!=contract:continue
        return {'confirmed':True,'authorization_version':version,'decision_id':row['id'],'targets':targets,
                'delivery_contract':contract}
    return None


def change_details(before,contract):
    from .workflow_model_protocol import required_delivery_names,required_delivery_extensions,required_delivery_roles
    after={'delivery_contract':contract}
    result={}
    for key,extract in (('filenames',required_delivery_names),('formats',required_delivery_extensions),('roles',required_delivery_roles)):
        old,new=extract(before),extract(after)
        result[key]={'removed':sorted(old-new),'added':sorted(new-old)}
    return result
