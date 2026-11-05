"""Structured specialist outputs, independent of model prose and provider wire format."""
from typing import Literal
from pydantic import Field,model_validator
from .workflow_graph import Contract


def review_criteria(snapshot):
    return snapshot.get('review_criteria') or snapshot.get('delivery_targets') or []


def delivery_candidate_refs(task):
    """Return authoritative outputs, excluding auxiliary evidence when real outputs exist."""
    accepted=set(task.get('accepted_result',[]));roles=task.get('quality',{}).get('output_roles',{})
    if not roles:return accepted
    if task.get('spec',{}).get('delivery_stage'):
        requested={check.get('file') for check in task['spec'].get('acceptance',[])
                   if check.get('source')=='user' and check.get('stage')=='final' and check.get('file') not in (None,'*')}
        exact={path for path in roles.values() if path in accepted and (path in requested or path.rsplit('/',1)[-1] in requested)}
        if exact:
            auxiliary={'report','plan','manifest','evidence','preview'}
            return exact|{path for role,path in roles.items() if role not in auxiliary and path in accepted}
    auxiliary={'report','plan','manifest','evidence'}
    primary={path for role,path in roles.items() if role not in auxiliary and path in accepted}
    return primary or {path for path in roles.values() if path in accepted}


def validate_review_scope(decision,snapshot):
    """A step verdict cannot introduce requirements from later delivery stages."""
    if snapshot.get('review_scope')!='task':return
    if decision.get('primary'):raise ValueError('Step review cannot declare a final primary deliverable')
    expected=set(range(len(review_criteria(snapshot))))
    actual={item['criterion_index'] for item in decision.get('evidence',[])}
    if not actual<=expected:raise ValueError('Step review evidence must use frozen criterion indexes')
    if decision['verdict']=='pass' and not expected<=actual:
        raise ValueError('Step review must cover every frozen review_criteria item')


class Evidence(Contract):
    criterion_index:int=Field(ge=0,le=99,description='Zero-based index in the frozen review_criteria/delivery_targets list.')
    observation:str=Field(min_length=1,max_length=4000)
    inspection_id:str=Field(pattern=r'^[a-f0-9]{32}$',description='Copy the short inspection_id returned by inspect. The server resolves its immutable ref.')


class ResolvedEvidence(Contract):
    criterion:str=Field(min_length=1,max_length=2000)
    observation:str=Field(min_length=1,max_length=4000)
    ref:str=Field(min_length=1,max_length=500)
    inspection_id:str=Field(pattern=r'^[a-f0-9]{32}$')


class ReviewDecision(Contract):
    snapshot_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
    verdict:Literal['pass','reject','inconclusive']
    evidence:list[Evidence]=Field(default_factory=list,max_length=100)
    deliverables:list[str]=Field(default_factory=list,max_length=100,description='candidate_key values returned by inspect for selected deliverables')
    primary:str=Field(default='',max_length=500,
        description='For final passing review, select the primary deliverable by candidate_key. Leave empty for step reviews.')
    feedback:str=Field(default='',max_length=4000)
    @model_validator(mode='after')
    def supported(self):
        if self.verdict=='pass' and not self.evidence:raise ValueError('Passing review requires evidence')
        return self


class ResolvedReviewDecision(Contract):
    snapshot_digest:str=Field(pattern=r'^[a-f0-9]{64}$')
    verdict:Literal['pass','reject','inconclusive']
    evidence:list[ResolvedEvidence]=Field(default_factory=list,max_length=100)
    deliverables:list[str]=Field(default_factory=list,max_length=100)
    primary:str=Field(default='',max_length=500)
    feedback:str=Field(default='',max_length=4000)
    @model_validator(mode='after')
    def supported(self):
        if self.verdict=='pass' and not self.evidence:raise ValueError('Passing review requires evidence')
        if self.primary and self.primary not in self.deliverables:raise ValueError('Primary must be selected')
        return self


def review_candidate_aliases(snapshot,inspections,candidate_keys):
    from pathlib import PurePosixPath
    choices={};ambiguous=set()
    def add(alias,identifier):
        if not alias or alias in ambiguous:return
        if alias in choices and choices[alias]!=identifier:choices.pop(alias);ambiguous.add(alias)
        else:choices[alias]=identifier
    for key,identifier in (candidate_keys or {}).items():
        reference=inspections.get(identifier)
        if not reference:continue
        add(key,identifier);add(reference,identifier);add(PurePosixPath(reference).name,identifier)
        for task in snapshot.get('tasks',[]):
            if not task['spec'].get('delivery_stage'):continue
            for role,path in task.get('quality',{}).get('output_roles',{}).items():
                if path==reference:add(role,identifier)
    return choices


def resolve_review(decision,snapshot,inspections,candidate_keys=None):
    criteria=review_criteria(snapshot)
    if any(item['criterion_index']>=len(criteria) for item in decision['evidence']):raise ValueError('Unknown frozen criterion index')
    evidence_ids={item['inspection_id'] for item in decision['evidence']}
    if not evidence_ids<=set(inspections):raise ValueError('Review evidence must use completed inspection_id values from this attempt')
    candidate_keys=candidate_keys or {};aliases=review_candidate_aliases(snapshot,inspections,candidate_keys)
    def select(value):
        if value in aliases:return aliases[value]
        raise ValueError('Final selection must use a current candidate_key or another unique alias from the same inspect receipt')
    selected=[select(value) for value in decision['deliverables']]
    if snapshot.get('review_scope')=='final' and decision['verdict']=='pass' and not selected:
        unique=list(dict.fromkeys(candidate_keys.values()))
        if len(unique)==1:selected=unique
    primary=select(decision['primary']) if decision.get('primary') else ''
    if not primary and len(set(selected))==1:primary=selected[0]
    if snapshot.get('review_scope')=='final' and decision['verdict']=='pass':
        if not selected:raise ValueError('Final review must select an inspected eligible deliverable')
        if len(set(selected))>1 and not primary:raise ValueError('Multiple final deliverables require an explicit primary candidate_key')
    if primary and primary not in selected:raise ValueError('Primary candidate_key must be selected')
    return ResolvedReviewDecision.model_validate({**decision,
        'evidence':[{'criterion':criteria[item['criterion_index']],'observation':item['observation'],
                     'ref':inspections[item['inspection_id']],'inspection_id':item['inspection_id']} for item in decision['evidence']],
        'deliverables':[inspections[item] for item in selected],
        'primary':inspections[primary] if primary else ''}).model_dump()


def validate_resolved_review(decision, snapshot, inspections):
    """Controller-side check; worker-resolved refs are still untrusted input."""
    criteria=set(review_criteria(snapshot))
    actual={item['criterion'] for item in decision['evidence']}
    if not actual<=criteria:
        raise ValueError('Review evidence introduces an unfrozen criterion')
    for item in decision['evidence']:
        if inspections.get(item['inspection_id']) != item['ref']:
            raise ValueError('Inspection identity does not match this attempt and reference')
    if decision['verdict']=='pass' and not criteria<=actual:
        raise ValueError('Review must cover every frozen criterion')
    if snapshot['review_scope']=='task':
        if decision['deliverables'] or decision['primary']:
            raise ValueError('Task review cannot select final deliverables')
    elif snapshot['review_scope']=='final' and decision['verdict']=='pass':
        selected=set(decision['deliverables'])
        approved={ref for task in snapshot['tasks'] if task['spec']['delivery_stage'] for ref in delivery_candidate_refs(task)}
        if not selected or not selected<=approved or not selected<=set(inspections.values()):
            raise ValueError('Final selection must contain inspected approved candidates')
        if len(selected)>1 and not decision['primary']:
            raise ValueError('Multiple final deliverables require an explicit primary')


def validate_temporal_evidence(decision,snapshot,records):
    """Verify sampling coverage, not the semantic truth of a model observation."""
    if decision['verdict']!='pass':return
    import json,math
    cited={item['inspection_id'] for item in decision['evidence']}
    records=[row for row in records if row['id'] in cited]
    for task in snapshot.get('tasks',[]):
        semantics=task['spec'].get('semantics',{}).get('temporal',[])
        if not semantics or snapshot.get('review_scope')=='final' and not task['spec'].get('delivery_stage'):continue
        files=set(task['candidate_result']+task['accepted_result'])
        if snapshot.get('review_scope')=='final' and not files.intersection(decision['deliverables']):
            continue
        video_role=task['quality'].get('output_roles',{}).get('video')
        video_paths={item['name'] for item in task['quality'].get('files',[]) if item.get('video') and item['name'] in files}
        if video_role in files:video_paths.add(video_role)
        if snapshot.get('review_scope')=='final':video_paths &= set(decision['deliverables'])
        if not video_paths:raise ValueError('Temporal review requires verified video candidates')
        hashes={item['name']:item.get('sha256') for item in task['quality'].get('files',[]) if item['name'] in files}
        for reference in video_paths:
            samples=[];durations=[];source_intervals=[]
            expected=hashes.get(reference)
            for row in records:
                evidence=json.loads(row['evidence_json'])
                proof=evidence.get('verified_media')
                if proof and (proof.get('source_reference')==reference or
                              expected and hashes.get(proof.get('source_reference'))==expected):
                    if proof['source_sha256']!=expected:continue
                    evidence=proof
                elif row['reference']!=reference and not (expected and hashes.get(row['reference'])==expected):continue
                if evidence.get('coverage') not in ('sampled_frame','contact_sheet'):continue
                duration=evidence.get('duration_seconds');interval=evidence.get('source_frame_interval_seconds')
                if not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<=0:continue
                if not isinstance(interval,(int,float)) or not math.isfinite(interval) or interval<=0:continue
                times=evidence.get('frame_timestamps',[evidence.get('timestamp_seconds')])
                if not all(type(value) in (int,float) and math.isfinite(value) and 0<=value<duration for value in times):continue
                samples.extend(times);durations.append(duration);source_intervals.append(interval)
            samples=sorted(set(samples))
            if len(samples)<2 or not durations:raise ValueError('Temporal review requires timestamped video evidence')
            duration=max(durations)
            for rule in semantics:
                deadline=min(rule['start_by_seconds'],duration*rule['start_by_fraction'])
                gap=rule['max_interruption_seconds']
                if samples[0]>deadline or (samples[-1]-samples[0])/duration<rule['required_coverage']:
                    raise ValueError('Temporal evidence does not cover onset and required duration')
                if max(source_intervals)>gap or max(b-a for a,b in zip(samples,samples[1:]))>gap+1e-6:
                    raise ValueError('Temporal evidence is too sparse to assess interruptions')



class Diagnosis(Contract):
    facts:list[str]=Field(max_length=40)
    uncertainties:list[str]=Field(max_length=40)
    recommendations:list[str]=Field(max_length=20)
    error_code:str=Field(default='',pattern=r'^[a-z_]{0,80}$')
    recommended_action:Literal['retry_same_revision','manager_revision','external_reconcile','user_decision','stop']='manager_revision'
    target_task_id:str=Field(default='',pattern=r'^$|^[a-z][a-z0-9_-]{0,39}$')


class VisualReport(Contract):
    verdict:Literal['pass','reject','inconclusive']
    evidence:list[Evidence]=Field(default_factory=list,max_length=100)
    feedback:str=Field(default='',max_length=4000)
    @model_validator(mode='after')
    def supported(self):
        if self.verdict=='pass' and not self.evidence:raise ValueError('Passing visual report requires evidence')
        return self
