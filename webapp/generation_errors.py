"""Bounded provider error codes, without response bodies, signed URLs or secrets."""
import re
import requests


def failure_info(record):
    """Normalize provider fields; free text never determines failure classification."""
    from .egress_policy import scrub,secrets
    error=record.get('error')
    message=error.get('message',record.get('message','')) if isinstance(error,dict) else error if isinstance(error,str) else record.get('message','')
    code=error.get('code',record.get('error_code')) if isinstance(error,dict) else record.get('error_code')
    protected=secrets()
    structured=bool(isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',code) and scrub(code,protected)==code)
    status='cancelled' if record.get('status')=='cancelled' else 'failed'
    code=code if structured else 'provider_cancelled' if status=='cancelled' else 'provider_task_failed'
    # Diagnostic text stays in the owning task's records; telemetry only carries
    # the bounded code. These patterns redact syntax, not business-error phrases.
    if not isinstance(message,str):message=''
    if len(message)>8192:message='[provider message omitted: too long]'
    message=scrub(message,protected)
    message=re.sub(r'https?://\S+','[provider-url]',message)
    message=re.sub(r'(?i)\b(?:Bearer|Basic)\s+\S+','[redacted authorization]',message)
    message=re.sub(r'''(?i)\b(?:api[_-]?key|access[_-]?token|token|secret|password|cookie|authorization|signature)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)''','[redacted credential]',message)
    message=re.sub(r'(?<!\w)/(?:[^/\s]+/)+[^\s]*','[provider-path]',message)
    message=' '.join(message.split())[:1000]
    return {'code':code,'code_source':'structured' if structured else 'fallback',
            'provider_status':status,'provider_message':message,'provider_message_untrusted':True}


def task_failure(record):
    info=failure_info(record)
    reason='上游任务已取消，不会自动重新生成。' if info['provider_status']=='cancelled' else '上游任务失败，不会自动重新生成；详情见本任务的供应商诊断记录。'
    code=info['code']
    failure=RuntimeError(reason);failure.provider_error_code=code
    failure.diagnostic={'message':reason,**info}
    return failure


def raise_for_status(response):
    from .telemetry import provider_response
    provider_response(response)
    if response.status_code < 400:
        return
    code='http_error'
    try:
        body=response.json()
        error=body.get('error') or body
        code=re.sub(r'[^A-Za-z0-9_.-]','',str(error.get('code',code)))[:120]
    except (ValueError,AttributeError,TypeError):
        pass
    failure=requests.HTTPError('Generation provider HTTP '+str(response.status_code)+' ('+code+')',response=response)
    failure.provider_error_code=code
    raise failure
