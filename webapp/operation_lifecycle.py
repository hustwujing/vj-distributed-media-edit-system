"""Server-registered execution protocols. Business operations select an implementation.

The runner owns transitions and fencing; implementations only invoke adapters.
Names and versions are persisted before execution and cannot be arbitrary URLs
or executable strings supplied by a task.
"""
from dataclasses import dataclass,field
from enum import StrEnum


class Lifecycle(StrEnum):
    LOCAL='local'
    SYNC='remote_sync'
    ASYNC='remote_async'


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

    def invoke(self, adapter, phase, workflow, step, *, cancelled=None, record=None):
        permitted={'local'} if self.lifecycle==Lifecycle.LOCAL else {'sync'} if self.lifecycle==Lifecycle.SYNC else {'submit','poll','collect'}
        if phase not in permitted:raise ValueError('Invalid lifecycle phase')
        if phase in self.handlers:
            return self.handlers[phase](adapter,workflow,step,cancelled=cancelled,record=record)
        if phase in ('local','sync'):
            if not self.execute_method:raise ValueError('Implementation has no execute phase')
            return getattr(adapter,self.execute_method)(workflow,step,cancelled)
        if self.lifecycle!=Lifecycle.ASYNC:raise ValueError('Invalid lifecycle phase')
        if phase=='submit':return getattr(adapter,self.submit_method)(workflow,step)
        if phase=='poll':return getattr(adapter,self.poll_method)(workflow,step)
        if phase=='collect':return getattr(adapter,self.collect_method)(workflow,step,record,cancelled)
        raise ValueError('Unknown execution phase')

    def recovery(self,phase):
        if phase=='local':return 'retry_isolated_if_declared_safe'
        if phase in ('submit','sync'):return 'reconcile_or_pause_unknown'
        if phase=='poll':return 'query_existing'
        if phase=='collect':return 'collect_existing'
        if phase=='validate':return 'verify_existing'
        raise ValueError('Unknown execution phase')


IMPLEMENTATIONS={}


def register(implementation):
    if implementation.id in IMPLEMENTATIONS:raise ValueError('Duplicate implementation: '+implementation.id)
    if '@' not in implementation.id:raise ValueError('An implementation version is required')
    Lifecycle(implementation.lifecycle)
    if any(not callable(handler) for handler in implementation.handlers.values()):raise ValueError('Implementation handlers must be callable')
    if implementation.lifecycle!=Lifecycle.ASYNC and not implementation.execute_method and str(implementation.lifecycle).removeprefix('remote_') not in implementation.handlers:
        raise ValueError('Executable lifecycle requires a handler')
    IMPLEMENTATIONS[implementation.id]=implementation


for name, lifecycle, method, cancellation in (
    ('fixed_media',Lifecycle.LOCAL,'fixed_media','terminate_scope'),
    ('fixed_image',Lifecycle.LOCAL,'fixed_image','terminate_scope'),
    ('transcribe',Lifecycle.SYNC,'transcribe','abort_wait_upstream_unknown'),
    ('image_sync',Lifecycle.SYNC,'generate_image','abort_wait_upstream_unknown'),
    ('image_async',Lifecycle.ASYNC,'','provider_dependent'),
    ('image_remote',Lifecycle.ASYNC,'','provider_dependent'),
    ('generation_async',Lifecycle.ASYNC,'','provider_dependent'),
):register(Implementation(name+'@1',lifecycle,method,cancellation=cancellation))


def get(identifier):
    try:return IMPLEMENTATIONS[identifier]
    except KeyError:raise ValueError('Unknown implementation version: '+str(identifier)) from None


def for_operation(operation):
    return get(operation.implementation_id or operation.executor+'@1')
