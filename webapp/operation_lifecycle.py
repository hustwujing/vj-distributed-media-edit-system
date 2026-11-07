"""Server-registered execution protocols. Business operations select an implementation.

The runner owns transitions and fencing; implementations only invoke adapters.
Names and versions are persisted before execution and cannot be arbitrary URLs
or executable strings supplied by a task.
"""
from dataclasses import dataclass,field,replace
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping
import json


class Lifecycle(StrEnum):
    LOCAL='local'
    SYNC='remote_sync'
    ASYNC='remote_async'


class LifecycleError(ValueError):
    """An unsupported protocol or inconsistent durable state must not execute."""


class RecoveryAction(StrEnum):
    RETRY_LOCAL='retry_isolated_if_declared_safe'
    RECONCILE='reconcile_or_pause_unknown'
    QUERY='query_existing'
    COLLECT='collect_existing'
    VERIFY='verify_existing'


@dataclass(frozen=True)
class Protocol:
    phases: tuple[str,...]
    state_phases: Mapping[str,str]
    recovery_actions: Mapping[str,RecoveryAction]

    def __post_init__(self):
        phases=set(self.phases)
        object.__setattr__(self,'phases',tuple(self.phases))
        if not phases or not phases<={'local','sync','submit','poll','collect'}:
            raise LifecycleError('Unsupported Runner phases')
        if set(self.recovery_actions)!=phases|{'validate'}:
            raise LifecycleError('Every phase requires an executable recovery action')
        if 'pending' not in self.state_phases or self.state_phases.get('verifying')!='validate':
            raise LifecycleError('Protocol requires entry and validation states')
        if set(self.state_phases.values())!=phases|{'validate'}:
            raise LifecycleError('Protocol state/phase mismatch')
        object.__setattr__(self,'state_phases',MappingProxyType(dict(self.state_phases)))
        object.__setattr__(self,'recovery_actions',MappingProxyType({key:RecoveryAction(value) for key,value in self.recovery_actions.items()}))


# No fallback: a new enum must explicitly define its executable protocol.
PROTOCOLS=MappingProxyType({
    Lifecycle.LOCAL:Protocol(('local',),{'pending':'local','running':'local','verifying':'validate'},
        {'local':RecoveryAction.RETRY_LOCAL,'validate':RecoveryAction.VERIFY}),
    Lifecycle.SYNC:Protocol(('sync',),{'pending':'sync','submitting':'sync','verifying':'validate'},
        {'sync':RecoveryAction.RECONCILE,'validate':RecoveryAction.VERIFY}),
    Lifecycle.ASYNC:Protocol(('submit','poll','collect'),
        {'pending':'submit','submitting':'submit','waiting':'poll','collecting':'collect','verifying':'validate'},
        {'submit':RecoveryAction.RECONCILE,'poll':RecoveryAction.QUERY,'collect':RecoveryAction.COLLECT,'validate':RecoveryAction.VERIFY}),
})


def protocol_for(lifecycle):
    try:return PROTOCOLS[lifecycle]
    except (KeyError,TypeError):raise LifecycleError('Unsupported lifecycle protocol: '+str(lifecycle)) from None


@dataclass(frozen=True)
class Implementation:
    id: str
    lifecycle: Lifecycle
    execute_method: str = ''
    submit_method: str = 'submit'
    poll_method: str = 'poll'
    collect_method: str = 'collect'
    cancellation: str = 'provider_dependent'
    handlers: dict = field(default_factory=dict,compare=False,repr=False)
    # Only implementations that persist intent BEFORE every external effect
    # may treat an absent receipt as proof that submission never happened.
    records_calls: bool = False

    @property
    def protocol(self):return protocol_for(self.lifecycle)

    def phase(self,step):
        protocol=self.protocol
        try:phase=protocol.state_phases[step['status']]
        except KeyError:raise LifecycleError('Invalid lifecycle state: '+str(step.get('status'))) from None
        external_id=step.get('external_id')
        if external_id and ('poll' not in protocol.phases or phase=='submit'):
            raise LifecycleError('Unexpected external task ID for lifecycle state')
        if phase in ('poll','collect') and not external_id:
            raise LifecycleError('External task ID required for lifecycle phase')
        return phase

    def invoke(self, adapter, phase, workflow, step, *, cancelled=None, record=None):
        if phase not in self.protocol.phases:raise LifecycleError('Invalid lifecycle phase')
        if phase in self.handlers:
            return self.handlers[phase](adapter,workflow,step,cancelled=cancelled,record=record)
        if phase in ('local','sync'):
            if not self.execute_method:raise ValueError('Implementation has no execute phase')
            return getattr(adapter,self.execute_method)(workflow,step,cancelled)
        if phase=='submit':return getattr(adapter,self.submit_method)(workflow,step)
        if phase=='poll':return getattr(adapter,self.poll_method)(workflow,step)
        if phase=='collect':return getattr(adapter,self.collect_method)(workflow,step,record,cancelled)
        raise ValueError('Unknown execution phase')

    def recovery(self,phase):
        try:return self.protocol.recovery_actions[phase]
        except KeyError:raise LifecycleError('Invalid recovery phase') from None

    def recover(self,step,*,receipt=None):
        """Return fenced state changes for Runner; never perform external work.

        The same typed recovery actions are advertised in the catalogue and
        executed here. Runner owns the transaction/lease and applies changes.
        """
        phase=self.phase(step)
        action=self.recovery(phase)
        if action==RecoveryAction.RETRY_LOCAL:
            if step['spec']['retry_safe'] and step['attempts']<3:
                return {'status':'pending','error_code':'','error':''}
            return {'status':'paused','error_code':'local','error':'本地步骤中断，未声明可安全重试或已达到重试上限。'}
        if action==RecoveryAction.RECONCILE:
            if phase=='sync' and self.records_calls:
                if receipt is None:return {'status':'pending'}
                if receipt['state']=='succeeded':
                    try:files=json.loads(receipt['result'])
                    except (TypeError,ValueError):files=None
                    if isinstance(files,list) and files and all(isinstance(path,str) and path for path in files):
                        return {'status':'verifying','candidate':files}
                    return {'status':'paused','error_code':'result_unavailable',
                            'error':'上游已返回，但结果尚未持久化；未自动再次调用。'}
            return {'status':'paused','error_code':'submission_unknown',
                    'error':'外部提交结果不确定，未自动重提；请核实服务端任务。'}
        if action==RecoveryAction.QUERY:return {'status':'waiting'}
        if action==RecoveryAction.COLLECT:
            # Re-query the saved task to refresh collection metadata, never submit.
            return {'status':'waiting','next_poll':0}
        if action==RecoveryAction.VERIFY:return {'status':'verifying'}
        raise LifecycleError('Unsupported recovery action')

    def resume_state(self,step):
        """Validate an explicit user resume without guessing from an ID alone."""
        code=step.get('error_code','')
        if code=='validate':status='verifying'
        elif code in ('poll','collect'):
            if code not in self.protocol.phases:raise LifecycleError('Invalid resume phase')
            status='waiting'
        elif code=='local':
            if not step['spec']['retry_safe'] or step['attempts']>=3:
                raise LifecycleError('Unsafe local retry')
            # A recorded synchronous call may fail before making any request.
            if 'local' not in self.protocol.phases and not ('sync' in self.protocol.phases and self.records_calls):
                raise LifecycleError('Invalid local retry for lifecycle')
            status='pending'
        elif code in ('','pre_submit'):status='waiting' if step.get('external_id') else 'pending'
        else:raise LifecycleError('Lifecycle cannot safely resume')
        self.phase({**step,'status':status})
        return status


IMPLEMENTATIONS={}


def register(implementation):
    if implementation.id in IMPLEMENTATIONS:raise ValueError('Duplicate implementation: '+implementation.id)
    if '@' not in implementation.id:raise ValueError('An implementation version is required')
    protocol=implementation.protocol
    if any(not callable(handler) for handler in implementation.handlers.values()):raise ValueError('Implementation handlers must be callable')
    if set(implementation.handlers)-set(protocol.phases):raise LifecycleError('Unknown implementation handler phase')
    methods={'local':implementation.execute_method,'sync':implementation.execute_method,
             'submit':implementation.submit_method,'poll':implementation.poll_method,'collect':implementation.collect_method}
    if any(phase not in implementation.handlers and not methods[phase] for phase in protocol.phases):
        raise LifecycleError('Executable lifecycle requires a handler or adapter method for every phase')
    if type(implementation.records_calls) is not bool:raise LifecycleError('records_calls must be boolean')
    # Copy handler bindings so a caller cannot change the registered protocol.
    implementation=replace(implementation,handlers=MappingProxyType(dict(implementation.handlers)))
    IMPLEMENTATIONS[implementation.id]=implementation


for name, lifecycle, method, cancellation in (
    ('fixed_media',Lifecycle.LOCAL,'fixed_media','terminate_scope'),
    ('fixed_image',Lifecycle.LOCAL,'fixed_image','terminate_scope'),
    ('transcribe',Lifecycle.SYNC,'transcribe','abort_wait_upstream_unknown'),
    ('image_sync',Lifecycle.SYNC,'generate_image','abort_wait_upstream_unknown'),
    ('image_async',Lifecycle.ASYNC,'','provider_dependent'),
    ('image_remote',Lifecycle.ASYNC,'','provider_dependent'),
    ('generation_async',Lifecycle.ASYNC,'','provider_dependent'),
):register(Implementation(name+'@1',lifecycle,method,cancellation=cancellation,
                         records_calls=name in ('transcribe','image_sync')))


def get(identifier):
    try:implementation=IMPLEMENTATIONS[identifier]
    except KeyError:raise ValueError('Unknown implementation version: '+str(identifier)) from None
    implementation.protocol
    return implementation


def for_operation(operation):
    return get(operation.implementation_id or operation.executor+'@1')


SCRIPT=Implementation('script@1',Lifecycle.LOCAL,'local')


def for_step(spec):
    from .operations import for_step as operation_for_step
    operation=operation_for_step(spec)
    return for_operation(operation) if operation is not None else SCRIPT


def recover_dynamic(implementation,task,call=None):
    """Return a deterministic recovery action for a unified Run work item.

    This function only interprets durable facts. It never invokes a provider or
    mutates task state, so the Orchestrator can apply it under its reconcile fence.
    """
    phase=task['phase'];action=implementation.recovery(phase)
    if action==RecoveryAction.VERIFY:
        return {'action':'ready','phase':'validate'} if task.get('candidate_result') else {'action':'unavailable'}
    if action==RecoveryAction.RETRY_LOCAL:
        return {'action':'ready','phase':'local'} if task['spec'].get('retry_safe') else {'action':'unavailable'}
    state=(call or {}).get('state','');external_id=(call or {}).get('external_id','')
    try:result=json.loads((call or {}).get('result') or '[]')
    except (TypeError,ValueError):result=[]
    if state=='succeeded':
        return {'action':'validate','candidate':result} if result else {'action':'unavailable'}
    if state in ('failed','rejected'):
        return {'action':'failed','error_code':'provider_failed'}
    if state=='cancelled':
        return {'action':'failed','error_code':'provider_cancelled'}
    if action in (RecoveryAction.QUERY,RecoveryAction.COLLECT) and external_id:
        return {'action':'poll','external_id':external_id}
    if action==RecoveryAction.RECONCILE:
        if external_id:return {'action':'poll','external_id':external_id}
        if call:return {'action':'unknown'}
        if implementation.records_calls or task['spec'].get('retry_safe'):
            return {'action':'ready','phase':phase}
        return {'action':'unavailable'}
    return {'action':'unknown'}
