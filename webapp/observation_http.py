"""ASGI correlation includes response bodies, stream failures and disconnects."""
import asyncio
import re
import time
import uuid
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from .telemetry import bind,emit


class ObservationMiddleware:
    def __init__(self,app):self.app=app

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        request_id=uuid.uuid4().hex;trace=uuid.uuid4().hex;span=uuid.uuid4().hex[:16]
        incoming=Headers(scope=scope).get('x-request-id','')
        incoming=incoming if re.fullmatch('[a-f0-9]{32}',incoming) else None
        match=re.search(r'/projects/([a-f0-9]{32})/(?:agent|jobs)/([a-f0-9]{32})(?:/|$)',scope['path'])
        project=re.search(r'/projects/([a-f0-9]{32})(?:/|$)',scope['path'])
        fields={'project_id':project[1]} if project else {}
        if match:fields.update(task_id=match[2])
        workflow=re.search(r'/projects/[a-f0-9]{32}/workflows/([a-f0-9]{32})(?:/|$)',scope['path'])
        if workflow:fields['workflow_id']=workflow[1]
        started=time.monotonic();status=500;began=False;complete=False;sent=0;first_byte=None;outcome='succeeded'
        async def observed_send(message):
            nonlocal status,began,complete,sent,first_byte
            if message['type']=='http.response.start':
                began=True;status=message['status'];first_byte=time.monotonic()-started
                headers=[(k,v) for k,v in message.get('headers',[]) if k.lower() not in (b'x-request-id',b'traceparent')]
                headers.extend([(b'x-request-id',request_id.encode()),(b'traceparent',f'00-{trace}-{span}-01'.encode())])
                message={**message,'headers':headers}
            await send(message)
            if message['type']=='http.response.body':
                sent+=len(message.get('body',b''));complete=not message.get('more_body',False)
            elif message['type']=='http.response.pathsend':complete=True
        with bind(request_id=request_id,trace_id=trace,span_id=span,linked_request_id=incoming,**fields):
            try:
                await self.app(scope,receive,observed_send)
                if not complete or status==499:outcome='disconnected'
            except asyncio.CancelledError:
                outcome='disconnected'
                if not began:status=499
                raise
            except Exception as exc:
                outcome='failed';emit('http.exception','ERROR',exc=exc,status=status)
                if began:raise  # Headers are already on the wire; never fabricate another response.
                response=JSONResponse({'detail':'服务暂时异常，请凭查询编号排查。','request_id':request_id},status_code=500)
                await response(scope,receive,observed_send)
            finally:
                route=re.sub(r'\{[^}]+\}',':id',getattr(scope.get('route'),'path','unmatched'))
                resolved={key:value for key,value in scope.get('state',{}).get('workflow_observation',{}).items()
                          if key in ('task_id','workflow_id') and isinstance(value,str) and re.fullmatch('[a-f0-9]{32}',value)}
                emit('http.finished','ERROR' if outcome=='failed' or status>=500 else 'INFO',method=scope['method'],route=route,status=status,
                     outcome=outcome,bytes_sent=sent,first_byte_seconds=round(first_byte,6) if first_byte is not None else None,
                     duration_seconds=round(time.monotonic()-started,6),**resolved)


def install(app):app.add_middleware(ObservationMiddleware)
