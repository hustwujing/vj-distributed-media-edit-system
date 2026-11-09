"""Adapters exposing existing Gateway business semantics to new transports."""
import contextlib
import json

from .gateway_contracts import json_result


class GatewayBusinessCore:
    def __init__(self,gateway):self.gateway=gateway

    @staticmethod
    def decode(body):
        try:value=json.loads(body)
        except (UnicodeDecodeError,ValueError) as exc:raise ValueError('Invalid Gateway request JSON') from exc
        if not isinstance(value,dict):raise ValueError('Gateway request must be an object')
        return value

    def model(self,value):return self.gateway.forward_result(value)

    def context(self,value):
        lease=self.gateway.lease
        if not lease or lease.kind!='agent':
            return json_result(403,{'error':'Public context requires an active Agent task'})
        from .harness_context import lookup
        row=lease.store.query('SELECT project FROM agent_turns WHERE id=?',(lease.run_id,))[0]
        result=lookup(lease.store,row['project'],value);self.gateway.check()
        return json_result(200,result)

    def workflow_submit(self,value,kind):
        from .workflow_gateway import submit
        return json_result(200,submit(self.gateway.lease,kind,value))

    def workflow_inspect(self,value):
        if self.gateway.broker:result=self.gateway.broker.request('workflow_inspect',value)
        else:
            from .workflow_gateway import inspect
            result=inspect(self.gateway,value)
        return json_result(200,result)

    def workflow(self,value):
        allowed=('submit','status','validate','decision','capabilities','intent','preflight')
        if value.get('action') not in allowed:return json_result(422,{'error':'Invalid workflow action'})
        if value['action']=='submit' and self.gateway.mode!='execute':
            return json_result(403,{'error':'请先确认方案'})
        try:return json_result(200,self.gateway.workflow_callback(value))
        except ValueError as exc:return json_result(422,{'error':str(exc)[:8000]})

    def routes(self):
        protocol=self.gateway.model_adapter.protocol
        model=({'/responses','/v1/responses'} if protocol=='responses' else
               {'/messages','/v1/messages'} if protocol=='anthropic' else
               {'/chat/completions','/v1/chat/completions'} if protocol=='chat_completions' else set())
        routes={route:self.model for route in model}
        routes.update({'/context':self.context,
            '/workflow/proposal':lambda value:self.workflow_submit(value,'proposal'),
            '/workflow/agent-result':lambda value:self.workflow_submit(value,'result'),
            '/workflow/inspect':self.workflow_inspect})
        if self.gateway.workflow_callback:routes['/workflow']=self.workflow
        if self.gateway.canvas_callback:routes['/canvas-edit']=self.gateway.canvas_result
        from .public_http_gateway import urls_for_lease,PublicHTTPGateway,ROUTE
        public_urls=urls_for_lease(self.gateway.lease)
        if public_urls:routes[ROUTE]=PublicHTTPGateway(self.gateway.lease,public_urls).handle
        routes['/transcribe']=self.gateway.transcribe_result
        routes['/image-edit']=self.gateway.image_result
        routes['/image-tools']=self.gateway.image_result
        return routes

    def supports(self,path):return path in self.routes()

    def dispatch_value(self,path,value):
        handler=self.routes().get(path)
        if handler is None:raise ValueError('Unsupported Gateway route')
        lease=self.gateway.lease
        with lease.bind() if lease else contextlib.nullcontext():
            self.gateway.check()
            return handler(value)

    def dispatch(self,path,body,permit):return self.dispatch_value(path,self.decode(body))

    def handlers(self):
        return {route:(lambda body,permit,route=route:self.dispatch(route,body,permit))
                for route in self.routes()}
