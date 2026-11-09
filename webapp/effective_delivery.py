"""Delivery-list amendments require durable user approval, not model prose."""
import json


def confirmed_scope(store,run):
    targets=run['authorization']['delivery_targets']
    if not targets:return None
    grants={row['version']:json.loads(row['authorization_json'])['delivery_targets'] for row in store.query(
        'SELECT version,authorization_json FROM run_authorizations WHERE run_id=?',(run['id'],))}
    for row in store.query("SELECT id,kind,request_json,authorization_version FROM workflow_decisions WHERE run_id=? AND status='applied' AND kind IN ('delivery_scope_change','plan_change','plan_confirmation') ORDER BY authorization_version DESC,created_at DESC,id DESC",(run['id'],)):
        request=json.loads(row['request_json']);version=row['authorization_version']+1
        if row['kind']!='delivery_scope_change' and not request.get('delivery_scope_changed'):continue
        if set(grants.get(version,[]))!=set(targets):continue
        return {'confirmed':True,'authorization_version':version,'decision_id':row['id'],'targets':targets}
    return None


def change_details(before,targets):
    from .workflow_model_protocol import required_delivery_names,required_delivery_extensions,required_delivery_roles
    after={'delivery_targets':targets,'effective_delivery_scope':{'confirmed':False,'proposed':True,'targets':targets}}
    result={}
    for key,extract in (('filenames',required_delivery_names),('formats',required_delivery_extensions),('roles',required_delivery_roles)):
        old,new=extract(before),extract(after)
        result[key]={'removed':sorted(old-new),'added':sorted(new-old)}
    return result
