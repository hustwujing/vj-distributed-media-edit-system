"""Native tool envelopes; strict support negotiation never relaxes host validation."""
from copy import deepcopy
import json
from .model_adapters import PublicResponseStructureError


def request(adapter,body,definitions,*,strict):
    value=deepcopy(body);tools=[]
    for item in definitions:
        common={'name':item['name'],'description':item['description']}
        if strict:common['strict']=True
        elif adapter.protocol=='responses':
            # Omission lets Responses normalize compatible schemas into strict
            # mode. A downgrade must explicitly opt out; other protocols omit
            # the extension so gateways that do not recognize it can proceed.
            common['strict']=False
        if adapter.protocol=='responses':tools.append({'type':'function',**common,'parameters':item['schema']})
        elif adapter.protocol=='chat_completions':tools.append({'type':'function','function':{**common,'parameters':item['schema']}})
        elif adapter.protocol=='anthropic':tools.append({**common,'input_schema':item['schema']})
        else:raise ValueError('Native tool protocol is not implemented')
    value['tools']=tools
    # Some reasoning routes reject forced tool choice. Auto changes selection,
    # not permission: the host still requires exactly one valid native call.
    if adapter.protocol=='anthropic':value['tool_choice']={'type':'auto','disable_parallel_tool_use':True}
    else:value.update(tool_choice='required' if adapter.protocol=='responses' else 'auto',parallel_tool_calls=False)
    return value


def response(adapter,payload):
    if not isinstance(payload,dict):raise ValueError('Response must be an object')
    if adapter.protocol=='responses':
        if payload.get('status')!='completed':raise ValueError('Tool response is incomplete')
        calls=[x for x in payload.get('output',[]) if x.get('type')=='function_call']
        if any(x.get('status') not in (None,'completed') for x in calls):raise ValueError('Tool call is incomplete')
        rows=[(x.get('name'),x.get('arguments')) for x in calls]
    elif adapter.protocol=='anthropic':
        if payload.get('stop_reason')!='tool_use':raise ValueError('Response did not finish with a tool call')
        rows=[(x.get('name'),x.get('input')) for x in payload.get('content',[]) if x.get('type')=='tool_use']
    elif adapter.protocol=='chat_completions':
        choices=payload.get('choices',[])
        if len(choices)!=1 or choices[0].get('finish_reason')!='tool_calls':raise ValueError('Tool response is incomplete')
        rows=[(x.get('function',{}).get('name'),x.get('function',{}).get('arguments')) for x in choices[0].get('message',{}).get('tool_calls',[]) if x.get('type')=='function']
    else:raise ValueError('Native tool protocol is not implemented')
    if len(rows)!=1:raise PublicResponseStructureError('Exactly one tool call is required','ambiguous_tool_calls')
    name,arguments=rows[0]
    if not isinstance(name,str):raise ValueError('Tool name is missing')
    if isinstance(arguments,str):arguments=json.loads(arguments)
    if not isinstance(arguments,dict):raise ValueError('Tool arguments must be an object')
    return name,arguments


def strict_unsupported(status,payload):
    """Only explicit request rejection permits retry without strict. No text heuristics."""
    if status not in (400,422) or not isinstance(payload,dict):return False
    error=payload.get('error',{})
    if not isinstance(error,dict):return False
    code=error.get('code') or error.get('type');param=error.get('param') or error.get('parameter') or ''
    if code in ('strict_not_supported','unsupported_strict_mode','structured_outputs_not_supported'):return True
    if code not in ('unsupported_parameter','unknown_parameter','unsupported_value','invalid_request_error','extra_forbidden'):return False
    return isinstance(param,str) and (param=='strict' or param.endswith('.strict') or param in ('text.format','response_format'))
