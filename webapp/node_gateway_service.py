"""Internal HTTP application for Node Gateway; TLS is owned by deployment."""
from __future__ import annotations

from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse,StreamingResponse
from starlette.concurrency import run_in_threadpool

from .node_gateway import GatewayAuthorizationError,GatewayBudgetError,GrantReaper


def create_app(core,store,*,source_ip=None,max_body=32*1024*1024,reaper_interval=1,
               require_https=True):
    if not 1<=max_body<=64*1024*1024:raise ValueError('Invalid Node Gateway body limit')
    reaper=GrantReaper(store,reaper_interval)
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    app.state.grant_reaper=reaper
    app.router.add_event_handler('startup',reaper.start)
    app.router.add_event_handler('shutdown',reaper.close)

    @app.get('/healthz')
    def health():return {'ready':True}

    @app.post('/{route:path}')
    async def dispatch(route,request:Request):
        if require_https and request.url.scheme!='https':
            return JSONResponse({'error':'https_required'},426)
        if request.url.query:return JSONResponse({'error':'query_not_allowed'},400)
        try:size=int(request.headers.get('content-length',''))
        except ValueError:return JSONResponse({'error':'invalid_content_length'},400)
        if not 0<size<=max_body:return JSONResponse({'error':'invalid_request_size'},413)
        body=await request.body()
        if len(body)!=size:return JSONResponse({'error':'request_size_mismatch'},400)
        peer=(source_ip(request) if source_ip else request.client.host if request.client else '')
        try:
            result=await run_in_threadpool(core.dispatch,dict(request.headers),'/'+route,peer,body)
        except GatewayAuthorizationError:return JSONResponse({'error':'gateway_forbidden'},403)
        except GatewayBudgetError:return JSONResponse({'error':'gateway_budget_exhausted'},429)
        except (TypeError,ValueError):return JSONResponse({'error':'gateway_request_invalid'},422)
        except Exception:return JSONResponse({'error':'gateway_handler_failed'},502)
        headers={name:value for name,value in result.headers.items()
                 if name.lower() in ('content-type','cache-control')}
        return StreamingResponse(result,status_code=result.status,headers=headers)

    return app
