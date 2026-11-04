"""Explicit fixture rebinding for isolated model evaluations, never live Runs."""
import copy
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def facts_digest(dataset):
    value=copy.deepcopy(dataset)
    for case in value['cases']:
        frozen=case['frozen']
        frozen.pop('snapshot_digest',None)
        frozen['authorization'].pop('agent_profile_digests',None)
    return digest(value)


def validate(dataset,policy_digest):
    for case in dataset['cases']:
        frozen=case['frozen'];auth=frozen['authorization']
        expected={name:policy_digest(name) for name in auth['allowed_agent_profiles']}
        if auth['agent_profile_digests']!=expected:
            raise ValueError('Fixture policy binding differs from evaluator: '+case['id'])
        if frozen['snapshot_digest']!=digest({k:v for k,v in frozen.items() if k!='snapshot_digest'}):
            raise ValueError('Fixture snapshot digest is invalid: '+case['id'])


def rebind(dataset,policy_digest):
    result=copy.deepcopy(dataset)
    for case in result['cases']:
        frozen=case['frozen'];auth=frozen['authorization']
        auth['agent_profile_digests']={name:policy_digest(name) for name in auth['allowed_agent_profiles']}
        frozen['snapshot_digest']=digest({k:v for k,v in frozen.items() if k!='snapshot_digest'})
    if facts_digest(result)!=facts_digest(dataset):raise ValueError('Rebinding changed fixture facts or scope')
    validate(result,policy_digest)
    return result
