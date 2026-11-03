"""Structured specialist outputs, independent of model prose and provider wire format."""
from typing import Literal
from pydantic import Field,model_validator
from .workflow_graph import Contract


def review_criteria(snapshot):
    return snapshot.get('review_criteria') or snapshot.get('delivery_targets') or []


def validate_review_scope(decision,snapshot):
    """A step verdict cannot introduce requirements from later delivery stages."""
    if snapshot.get('review_scope')=='final' and decision['verdict']=='pass':
        selected=set(decision['deliverables'])
        if not selected:raise ValueError('Final review must select deliverables')
        if len(selected)>1 and not decision.get('primary'):
            raise ValueError('Final review must explicitly select primary from deliverables; respect the user primary requirement')
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
    deliverables:list[str]=Field(default_factory=list,max_length=100,description='inspection_id values for selected deliverables')
    primary:str=Field(default='',max_length=500,
        description='For final passing review, select the primary deliverable by inspection_id. Leave empty for step reviews.')
    feedback:str=Field(default='',max_length=4000)
    @model_validator(mode='after')
    def supported(self):
        if self.verdict=='pass' and not self.evidence:raise ValueError('Passing review requires evidence')
        if self.primary and self.primary not in self.deliverables:
            raise ValueError('Primary must be one of the selected deliverables')
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


def resolve_review(decision,snapshot,inspections):
    criteria=review_criteria(snapshot)
    if any(item['criterion_index']>=len(criteria) for item in decision['evidence']):raise ValueError('Unknown frozen criterion index')
    ids={item['inspection_id'] for item in decision['evidence']}|set(decision['deliverables'])|({decision['primary']} if decision.get('primary') else set())
    if not ids<=set(inspections):raise ValueError('Review must use completed inspection_id values from this attempt')
    return ResolvedReviewDecision.model_validate({**decision,
        'evidence':[{'criterion':criteria[item['criterion_index']],'observation':item['observation'],
                     'ref':inspections[item['inspection_id']],'inspection_id':item['inspection_id']} for item in decision['evidence']],
        'deliverables':[inspections[item] for item in decision['deliverables']],
        'primary':inspections[decision['primary']] if decision.get('primary') else ''}).model_dump()


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
        approved={ref for task in snapshot['tasks'] if task['spec']['delivery_stage'] for ref in task['accepted_result']}
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
        video_role=task['quality'].get('output_roles',{}).get('video')
        video_paths={video_role} if video_role in files else {item['name'] for item in task['quality'].get('files',[]) if item.get('video') and item['name'] in files}
        if snapshot.get('review_scope')=='final':video_paths &= set(decision['deliverables'])
        if not video_paths:raise ValueError('Temporal review requires verified video candidates')
        for reference in video_paths:
            samples=[];durations=[];source_intervals=[]
            for row in records:
                evidence=json.loads(row['evidence_json'])
                proof=evidence.get('verified_media')
                if proof and proof.get('source_reference')==reference:
                    expected=next((item.get('sha256') for item in task['quality'].get('files',[]) if item['name']==reference),None)
                    if proof['source_sha256']!=expected:continue
                    evidence=proof
                elif row['reference']!=reference:continue
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
