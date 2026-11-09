"""Read-only public HTTPS capability for one frozen Agent Task."""
from __future__ import annotations

import json

from .gateway_contracts import GatewayResult,json_result


ROUTE='/public-http'


def urls_for_lease(lease):
    if not lease or lease.kind!='workflow' or not lease.step_id:return ()
    from .workflow_graph import get,work_item
    run=get(lease.store,lease.run_id);task=work_item(run,lease.step_id) if run else None
    if not task or task['type']!='agent':return ()
    return tuple((task['spec'].get('runtime_requirements') or {}).get('network_urls') or ())


class PublicHTTPGateway:
    def __init__(self,lease,allowed_urls):
        from .agent_runtime_capabilities import validate_public_url
        self.lease=lease;self.allowed=frozenset(validate_public_url(url) for url in allowed_urls)
        if len(self.allowed)!=len(tuple(allowed_urls)):raise ValueError('Duplicate public HTTPS grant')

    def handle(self,body,permit=None):
        if isinstance(body,dict):value=body
        else:
            try:value=json.loads(body)
            except (UnicodeDecodeError,ValueError):return json_result(400,{'error':'invalid_json'})
        if not isinstance(value,dict) or set(value)-{'url'} or value.get('url') not in self.allowed:
            from .egress_policy import denial
            denial(self.lease,'public_http_url_denied')
            return json_result(403,{'error':'public_https_url_not_granted'})
        self.lease.check()
        from .egress_policy import session
        try:
            with session() as client:
                response=client.get(value['url'],stream=False,timeout=(10,60),allow_redirects=False,
                    headers={'Accept':'*/*','User-Agent':'video-use-sandbox-fetch/1'})
                data=response.content
                if len(data)>32*1024**2:raise ValueError('Public HTTPS response exceeds limit')
                content_type=response.headers.get('Content-Type','application/octet-stream')
                if not isinstance(content_type,str) or len(content_type)>200 or any(ord(char)<32 for char in content_type):
                    content_type='application/octet-stream'
                headers={'Content-Type':content_type,
                    'Cache-Control':'no-store','Content-Length':str(len(data))}
                return GatewayResult(response.status_code,headers,[data],response.close)
        except ValueError:
            return json_result(413,{'error':'public_https_response_too_large'})
        except Exception:
            return json_result(502,{'error':'public_https_fetch_failed'})
