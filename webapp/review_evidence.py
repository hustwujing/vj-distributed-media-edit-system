"""Shared eligibility for candidate evidence and the candidate's actual inputs."""


def relationship(subject,evidence):
    source=evidence['spec'].get('inputs',{}).get('source',{})
    candidate=source.get('repair_candidate')
    if candidate:
        return 'candidate' if candidate['logical_task_id']==subject['id'] and candidate['revision']==subject['revision'] else None
    asset=source.get('asset')
    if not asset:return None
    verified=evidence.get('quality',{}).get('verified_evidence')
    bound=evidence.get('bindings',{}).get('source',{})
    if (not isinstance(verified,dict) or verified.get('source_reference')!=asset or
        bound.get('asset')!=asset or not bound.get('sha256') or verified.get('source_sha256')!=bound['sha256']):return None
    # Same ID alone is insufficient: both executions must have consumed the
    # same immutable bytes. A source report never proves the output's content.
    for role,reference in subject['spec'].get('inputs',{}).items():
        original=subject.get('bindings',{}).get(role,{})
        if reference.get('asset')==asset and original.get('asset')==asset and original.get('sha256')==bound['sha256']:
            return 'source_asset'
    return None
