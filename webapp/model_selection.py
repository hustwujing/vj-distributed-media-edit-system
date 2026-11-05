"""User choices select registered model settings, never routes or credentials."""
from dataclasses import replace
import json
import os
from .agent_profiles import profiles,selected


def efforts(profile):
    # DeepSeek's "medium" alias actually requests high effort. Expose native
    # levels so the UI does not promise a distinct level that is not applied.
    if profile.model.startswith('deepseek-'):return ['low','high','max']
    return list(dict.fromkeys(['low','medium','high',profile.reasoning_effort]))


def options(store,project):
    registry=profiles()
    preferred=store.query('SELECT name FROM agent_project_profiles WHERE project=?',(project,))
    default=preferred[0]['name'] if preferred and preferred[0]['name'] in registry else selected().name
    default_effort=lambda p:p.reasoning_effort if p.reasoning_effort in efforts(p) else 'high'
    from .agent_profiles import role_defaults
    defaults=role_defaults()
    return {'default_profile':default,'default_effort':default_effort(registry[default]),
        'profiles':[{'id':name,'model':p.model,'reasoning_efforts':efforts(p),'default_effort':default_effort(p),
            'profile_digest':p.digest,'harness':p.harness,'available':bool(p.base_url and os.environ.get(p.credential_env))}
            for name,p in registry.items()]}


def resolve(choice):
    try:profile=profiles()[choice.profile]
    except KeyError:raise ValueError('所选模型配置不存在，请刷新后重新选择') from None
    if profile.digest!=choice.profile_digest:raise ValueError('模型配置已更新，请核对后再次确认')
    if choice.reasoning_effort not in efforts(profile):raise ValueError('该模型不支持所选推理强度')
    profile.credentials()
    return replace(profile,reasoning_effort=choice.reasoning_effort)


def resolve_roles(sandbox_configuration=None):
    from .specialist_profiles import PROFILES
    from .agent_profiles import role_defaults
    registry=profiles();defaults=role_defaults();result={}
    for identifier,profile in PROFILES.items():
        result[identifier]=(sandbox_configuration or selected()) if str(profile.role)=='IMPLEMENTER' else registry[defaults[identifier]]
    return result


def for_run(store,identifier):
    rows=store.query('SELECT configuration,digest FROM agent_run_profiles WHERE task_id=?',(identifier,))
    if not rows:return None
    value=json.loads(rows[0]['configuration'])
    from .agent_profiles import public_roles
    return {'profile':value['name'],'model':value['model'],'reasoning_effort':value['reasoning_effort'],
            'configuration_digest':rows[0]['digest'],'input_token_policy':value.get('input_token_policy','unrecorded'),
            'roles':public_roles(store,identifier)}
