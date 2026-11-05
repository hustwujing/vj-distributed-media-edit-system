"""Adapters exposing existing Gateway business semantics to new transports."""
import json


class GatewayBusinessCore:
    def __init__(self,gateway):self.gateway=gateway

    def model(self,body,permit):
        try:value=json.loads(body)
        except (UnicodeDecodeError,ValueError) as exc:raise ValueError('Invalid model request JSON') from exc
        if not isinstance(value,dict):raise ValueError('Model request must be an object')
        return self.gateway.forward_result(value)

    def handlers(self):
        protocol=self.gateway.model_adapter.protocol
        routes=({'/responses','/v1/responses'} if protocol=='responses' else
                {'/messages','/v1/messages'} if protocol=='anthropic' else
                {'/chat/completions','/v1/chat/completions'} if protocol=='chat_completions' else set())
        return {route:self.model for route in routes}
