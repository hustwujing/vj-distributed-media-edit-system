"""Bounded, nonblocking structured telemetry. Never records user payloads."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import traceback
import uuid

CONTEXT = ContextVar('observation_context', default={})
IDENTIFIER = re.compile(r'^[A-Za-z0-9_.:@/-]{1,160}$')
FIELDS = set('event_sequence message_id actor activity_id level call_id run_id request_id trace_id span_id parent_span_id linked_request_id project_id task_id run_id step_id revision attempt_id generation worker node operation implementation implementation_version phase outcome error_code exception_type native_dependency stack service instance release method route status duration_seconds first_byte_seconds bytes_sent returncode signal oom_kill provider external_id provider_request_id request_key calls failures dropped reason old_state new_state entity entity_id event_id'.split())
from .api_capacity import FIELDS as API_CAPACITY_FIELDS
FIELDS.update(API_CAPACITY_FIELDS)
FIELDS.update({'harness','model','protocol','reasoning_effort','max_output_tokens','configuration_digest','switch_id'})
FIELDS.update({'structured_output_mode','schema_digest','tool_call_count','response_status','response_finish_reason','json_error_kind','repeated_error_count'})
FIELDS.update({'response_transport','requested_model','response_model','model_route_changed','headers_seconds','first_chunk_seconds','first_public_seconds','publisher_calls','publisher_seconds'})
FIELDS.update({'schema_bytes','schema_max_depth','schema_object_count','schema_union_branches','schema_enum_values','output_tokens_unknown','partially_settled'})
FIELDS.update({'transport_valid','schema_valid','compile_valid','semantic_valid','policy_valid','executable','retry_success','wrong_but_schema_valid'})
FIELDS.update({'graph_version','authorization_version','sealed','proposal_id','planner_run_id','reconcile_generation','profile_id','snapshot_digest','task_count','scan_lag_seconds'})
FIELDS.update({'input_id','input_sequence','through_sequence','next_barrier_sequence','control_job_id','result_kind','source','review_identity','previous_review_id','successor_run_id','payload_type','top_level_keys','text_characters','text_sha256','choices_items','output_items','content_items'})
FIELDS.update({'response_message_count','response_commentary_messages','response_final_messages','response_unphased_messages',
               'remaining_calls','remaining_inspections','remaining_corrections'})
FIELDS.update({'script_sha256','outputs_sha256','output_files','stdout_bytes','stdout_truncated'})
FIELDS.update({'native_input_tokens','image_envelope_tokens','framing_margin_tokens','input_tokens','output_tokens','image_count','vision_policy_digest'})
FIELDS.update({'estimated_input_tokens','reference_input_tokens','estimation_policy','estimation_policy_digest','estimate_multiplier',
               'reservation_id','reserved_tokens','input_reserved_tokens','output_reserved_tokens','actual_tokens',
               'input_underestimated','reservation_overrun_tokens'})
_QUEUE = queue.Queue(maxsize=1024)
_LOCK = threading.Lock()
_THREAD = None
_PID = None
_DROPPED = 0


def identifier(value):
    text = str(value)
    if not IDENTIFIER.fullmatch(text):return 'invalid'
    for key,value in os.environ.items():
        if len(value)>=8 and any(term in key.upper() for term in ('KEY','TOKEN','SECRET','PASSWORD','COOKIE')) and value in text:return 'redacted'
    return text


def release():
    return os.environ.get('VIDEO_USE_RELEASE', Path(__file__).resolve().parents[1].name)


def clean(fields):
    out = {}
    for key, value in fields.items():
        if key not in FIELDS or value is None:continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            if isinstance(value,float) and (value!=value or abs(value)>1e18):continue
            out[key]=value
        elif key=='stack' and isinstance(value,list):
            out[key]=[{k:identifier(v) for k,v in frame.items() if k in ('file','function','line','type')} for frame in value[:24] if isinstance(frame,dict)]
        elif key=='top_level_keys' and isinstance(value,list):
            out[key]=[identifier(item) for item in value[:30] if isinstance(item,str)]
        elif isinstance(value,str):
            # No free-text fields, source lines, messages, URLs, headers or locals.
            out[key]=identifier(value)
    return out


def context():
    result = dict(CONTEXT.get())
    module=sys.modules.get('webapp.task_leases');lease=module.CURRENT.get() if module else None
    if lease:
        result.update(task_id=lease.run_id,attempt_id=lease.attempt,worker=lease.worker,
                      generation=lease.generation,step_id=lease.step_id,
                      trace_id=result.get('trace_id') or hashlib.sha256(lease.run_id.encode()).hexdigest()[:32])
    if result.get('task_id') and not result.get('trace_id'):
        result['trace_id']=hashlib.sha256(result['task_id'].encode()).hexdigest()[:32]
    return result


@contextmanager
def bind(**fields):
    token=CONTEXT.set({**CONTEXT.get(),**clean(fields)})
    try:yield
    finally:CONTEXT.reset(token)


def exception_fields(exc):
    # Class + code location + cause classes suffice to locate code without
    # leaking exception messages, source expressions, inputs or credentials.
    frames=[];seen=set();current=exc
    while current is not None and id(current) not in seen and len(seen)<4:
        seen.add(id(current))
        for f in traceback.extract_tb(current.__traceback__)[-8:]:
            frames.append({'file':Path(f.filename).name,'function':f.name,'line':str(f.lineno),'type':type(current).__name__})
        current=current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    detail=getattr(exc,'diagnostic',{})
    code=detail.get('code') if isinstance(detail,dict) else None
    import errno
    sqlstate=getattr(exc,'sqlstate',None)
    system_code=errno.errorcode.get(getattr(exc,'errno',None)) if isinstance(exc,OSError) else None
    code=code or getattr(exc,'provider_error_code',None) or (('sqlstate_'+sqlstate) if isinstance(sqlstate,str) and re.fullmatch('[A-Z0-9]{5}',sqlstate) else None) or system_code or type(exc).__name__
    return {'exception_type':type(exc).__name__,'error_code':identifier(code),'stack':frames[-24:]}


def record(event,level='INFO',exc=None,**fields):
    body={'schema':1,'timestamp':datetime.now(timezone.utc).isoformat(),'time':time.time(),
          'event':identifier(event),'level':level if level in ('DEBUG','INFO','WARNING','ERROR','CRITICAL') else 'INFO',
          'event_id':uuid.uuid4().hex,'service':os.environ.get('VIDEO_USE_SERVICE','application'),
          'instance':os.environ.get('VIDEO_USE_INSTANCE',str(os.getpid())),'release':release()}
    body.update(clean({**context(),**fields,**(exception_fields(exc) if exc else {})}))
    return body


def _write():
    global _DROPPED
    while True:
        item=_QUEUE.get()
        try:
            stream=sys.stderr if os.environ.get('VIDEO_USE_TELEMETRY_STREAM')=='stderr' else sys.stdout
            stream.write(json.dumps(item,separators=(',',':'),ensure_ascii=True)+'\n');stream.flush()
            if _DROPPED:
                dropped=_DROPPED;_DROPPED=0
                stream.write(json.dumps(record('telemetry.dropped','WARNING',dropped=dropped))+'\n');stream.flush()
        except Exception:_DROPPED+=1
        finally:_QUEUE.task_done()


def emit(event,level='INFO',exc=None,**fields):
    global _THREAD,_PID,_QUEUE,_DROPPED
    if os.environ.get('VIDEO_USE_TELEMETRY_ENABLED')!='1':return
    try:
        if _PID!=os.getpid() or _THREAD is None:
            with _LOCK:
                if _PID!=os.getpid() or _THREAD is None:
                    _QUEUE=queue.Queue(maxsize=1024);_PID=os.getpid()
                    _THREAD=threading.Thread(target=_write,name='telemetry-writer',daemon=True);_THREAD.start()
        _QUEUE.put_nowait(record(event,level,exc,**fields))
    except Exception:_DROPPED+=1


@contextmanager
def span(phase,**fields):
    start=time.monotonic();parent=CONTEXT.get().get('span_id');outcome='succeeded'
    with bind(span_id=uuid.uuid4().hex[:16],parent_span_id=parent,phase=phase,**fields):
        emit('phase.started')
        try:yield
        except BaseException as exc:
            outcome='stopped' if type(exc).__name__ in ('LeaseLost','TaskLimitReached','Cancelled','Shutdown') else 'failed'
            emit('phase.error','INFO' if outcome=='stopped' else 'ERROR',exc=exc,outcome=outcome)
            raise
        finally:emit('phase.finished',outcome=outcome,duration_seconds=round(time.monotonic()-start,6))


class StructuredHandler(logging.Handler):
    def emit(self,entry):
        emit('python.'+entry.name,entry.levelname,exc=entry.exc_info[1] if entry.exc_info else None)


def child_env(service,task_id=None,attempt_id=None):
    result={key:os.environ[key] for key in ('VIDEO_USE_TELEMETRY_ENABLED','VIDEO_USE_RELEASE','VIDEO_USE_INSTANCE') if key in os.environ}
    result.update(VIDEO_USE_TELEMETRY_STREAM='stderr',VIDEO_USE_SERVICE=identifier(service))
    for key,value in (('TASK_ID',task_id),('ATTEMPT_ID',attempt_id)):
        if isinstance(value,str) and re.fullmatch('[a-f0-9]{32}',value):result['VIDEO_USE_OBS_'+key]=value
    return result


def flush(timeout=.2):
    end=time.monotonic()+timeout
    while _QUEUE.unfinished_tasks and time.monotonic()<end:time.sleep(.002)


def setup(service):
    os.environ.setdefault('VIDEO_USE_SERVICE',service)
    if os.environ.get('VIDEO_USE_TELEMETRY_ENABLED')=='1':
        import atexit
        atexit.register(flush)
        inherited={name:os.environ['VIDEO_USE_OBS_'+name.upper()] for name in ('task_id','attempt_id') if re.fullmatch('[a-f0-9]{32}',os.environ.get('VIDEO_USE_OBS_'+name.upper(),''))}
        CONTEXT.set({**CONTEXT.get(),**inherited})
        handler=StructuredHandler()
        logging.getLogger().handlers=[handler]
        for name in ('uvicorn','uvicorn.error','uvicorn.access'):
            log=logging.getLogger(name);log.handlers=[];log.propagate=True
        def uncaught(kind,value,tb):
            # Process is already exiting; retain a sanitized terminal record.
            try:sys.stderr.write(json.dumps(record('process.uncaught','CRITICAL',exc=value))+'\n');sys.stderr.flush()
            except Exception:pass
        sys.excepthook=uncaught
        threading.excepthook=lambda args:emit('thread.uncaught','ERROR',exc=args.exc_value)
        emit('service.started')


def response_identity(response):
    """Retain only an allowlisted provider request identifier, never headers."""
    for name in ('x-request-id','x-tt-logid','request-id'):
        value=response.headers.get(name)
        if isinstance(value,str) and IDENTIFIER.fullmatch(value):return value
    return None


def provider_response(response):
    try:
        if getattr(response,'_video_use_observed',False) is True:return
        response._video_use_observed=True
        emit('provider.http',status=response.status_code,provider_request_id=response_identity(response))
    except Exception:
        # Observing an unusual SDK response must never change its business result.
        emit('telemetry.provider_metadata_unavailable','WARNING')


def process_exit(process,groups=None):
    oom=None
    if groups:
        try:
            count=0;found=False
            for path in groups:
                path=Path(path);counter=path/'memory.events'
                if not counter.exists():counter=path/'memory.oom_control'
                if not counter.exists():continue
                found=True
                for line in counter.read_text().splitlines():
                    key,value=line.split()
                    if key=='oom_kill':count+=int(value)
            oom=count if found else None
        except (OSError,ValueError):pass
    code=process.returncode
    emit('process.exited','ERROR' if code else 'INFO',returncode=code,
         signal=-code if code is not None and code<0 else None,oom_kill=oom)


def write_child_error(exc):
    """Fixed tool error envelope, captured by its parent, never raw content."""
    sys.stderr.write(json.dumps({'schema':1,'event':'tool.failure',**exception_fields(exc)})+'\n')
    sys.stderr.flush()


def child_failure(path):
    """Read a bounded envelope; child cannot choose authoritative task context."""
    import stat
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):return
            stream.seek(max(0,info.st_size-65536));lines=stream.read(65536).decode(errors='replace').splitlines()
        for line in reversed(lines):
            if not line.startswith('{'):continue
            try:value=json.loads(line)
            except ValueError:continue
            if not isinstance(value,dict) or value.get('schema')!=1 or value.get('event')!='tool.failure':continue
            fields=clean({k:value[k] for k in ('exception_type','error_code','stack') if k in value})
            emit('process.child_failure','ERROR',reason='child_report',**fields);return
    except Exception:emit('telemetry.child_evidence_unavailable','WARNING')
