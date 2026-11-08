"""Meter classification and plan checks without granting an execution lease."""
from contextlib import contextmanager
import json
from types import SimpleNamespace


@contextmanager
def model_request(store,task,model,body,*,output_tokens=800):
    from .task_leases import CURRENT,model_call_limit
    from .workflow_agents import model_budget
    from .workflow_graph import get,Authorization
    from .task_usage import model_call
    from .usage_protocol import Receipt
    if store is None or not task:raise ValueError('Auxiliary model call requires an owned task target')
    lease=CURRENT.get()
    if lease and (lease.run_id!=task or lease.store.identity!=store.identity):
        raise ValueError('Auxiliary model target differs from current execution lease')
    run=get(store,task)
    # Legacy discussion turns have no Run authorization. Still apply the same
    # model/daily counters and a finite token envelope in the shared ledger.
    if not run:
        run={'id':task,'authorization':{'model_token_budget':Authorization.model_fields['model_token_budget'].default}}
    # These short classification payloads contain text/schema, never media bytes.
    # UTF-8 bytes plus framing are a conservative admission estimate, not usage.
    input_tokens=len(json.dumps(body,ensure_ascii=False).encode())+256
    observer=Receipt()
    target=lease or SimpleNamespace(store=store,run_id=task,attempt='',step_id='')
    with model_budget(lease,run,input_tokens+output_tokens,observer,store=store,
                      input_reserved_tokens=input_tokens,output_reserved_tokens=output_tokens,
                      method='auxiliary_text_byte_envelope_v1',model_call_limit=model_call_limit()):
        with model_call(target,model,actor='host_classifier',observer=observer) as receipt:
            yield receipt
