import unittest
import threading

from webapp.gateway_contracts import GatewayResult
from webapp.node_gateway import (
    GatewayAuthorizationError,GatewayBudgetError,GrantAuthority,
    InMemoryGrantStore,NodeGatewayAuthorizer,NodeGatewayCore,RuntimeGrantFactory,
)


class NodeGatewayTests(unittest.TestCase):
    def setUp(self):
        self.now=[100.0];self.store=InMemoryGrantStore(lambda:self.now[0])
        self.authority=GrantAuthority(self.store);self.authorizer=NodeGatewayAuthorizer(self.store)
        self.issued=self.authority.issue(task_id='task',attempt_id='attempt',generation=2,
            sandbox_id='sandbox',source_ip='172.30.0.8',routes={'/v1/responses','/context'},
            expires_at=200,max_calls=2,max_request_bytes=10,max_response_bytes=12)
        self.headers={'Authorization':'Bearer '+self.issued.token,
            'X-Video-Use-Attempt':'attempt','X-Video-Use-Generation':'2',
            'X-Video-Use-Sandbox':'sandbox'}

    def test_store_never_keeps_raw_token_and_identity_is_fully_bound(self):
        grant=self.store.by_id[self.issued.id]
        self.assertNotEqual(grant.token_hash,self.issued.token)
        self.assertNotIn(self.issued.token,repr(grant));self.assertNotIn(self.issued.token,repr(self.issued))
        permit=self.authorizer.authorize(self.headers,'/context','172.30.0.8',4)
        permit.consume_output(b'hello');permit.close()
        self.assertEqual((grant.calls,grant.request_bytes,grant.response_bytes),(1,4,5))
        for field,value in [('X-Video-Use-Attempt','other'),('X-Video-Use-Generation','3'),
                            ('X-Video-Use-Sandbox','other')]:
            with self.subTest(field=field),self.assertRaises(GatewayAuthorizationError):
                self.authorizer.authorize({**self.headers,field:value},'/context','172.30.0.8',1)
        with self.assertRaises(GatewayAuthorizationError):
            self.authorizer.authorize(self.headers,'/context','172.30.0.9',1)

    def test_route_call_request_and_streaming_response_budgets_are_atomic(self):
        with self.assertRaises(GatewayAuthorizationError):
            self.authorizer.authorize(self.headers,'/admin','172.30.0.8',1)
        first=self.authorizer.authorize(self.headers,'/context','172.30.0.8',6)
        with self.assertRaises(GatewayBudgetError):first.consume_output(13)
        first.consume_output(12);first.close()
        second=self.authorizer.authorize(self.headers,'/context','172.30.0.8',4);second.close()
        with self.assertRaises(GatewayBudgetError):
            self.authorizer.authorize(self.headers,'/context','172.30.0.8',1)

    def test_revoke_closes_active_connection_and_blocks_new_work(self):
        permit=self.authorizer.authorize(self.headers,'/v1/responses','172.30.0.8',1)
        closed=[];permit.register_connection(lambda:closed.append(True))
        self.assertTrue(self.authority.revoke(self.issued.id));self.assertEqual(closed,[True])
        with self.assertRaises(GatewayAuthorizationError):permit.consume_output(b'x')
        with self.assertRaises(GatewayAuthorizationError):
            self.authorizer.authorize(self.headers,'/context','172.30.0.8',1)

    def test_expiration_and_attempt_cleanup_fail_closed(self):
        self.now[0]=201
        with self.assertRaises(GatewayAuthorizationError):
            self.authorizer.authorize(self.headers,'/context','172.30.0.8',1)
        self.now[0]=100
        second=self.authority.issue(task_id='task',attempt_id='attempt',generation=3,
            sandbox_id='new',source_ip='172.30.0.9',routes={'/context'},expires_at=200,
            max_calls=1,max_request_bytes=1,max_response_bytes=1)
        self.assertEqual(self.store.revoke_attempt('attempt'),2)
        self.assertTrue(self.store.by_id[second.id].revoked)

    def test_parallel_admission_never_exceeds_call_budget(self):
        issued=self.authority.issue(task_id='parallel',attempt_id='parallel-attempt',generation=1,
            sandbox_id='parallel-sandbox',source_ip='172.30.0.10',routes={'/context'},
            expires_at=200,max_calls=20,max_request_bytes=100,max_response_bytes=100)
        headers={'authorization':'Bearer '+issued.token,'x-video-use-attempt':'parallel-attempt',
            'x-video-use-generation':'1','x-video-use-sandbox':'parallel-sandbox'}
        accepted=[];lock=threading.Lock()
        def admit():
            try:permit=self.authorizer.authorize(headers,'/context','172.30.0.10',1)
            except GatewayBudgetError:return
            with lock:accepted.append(permit)
        threads=[threading.Thread(target=admit) for _ in range(80)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len(accepted),20)
        self.assertEqual(self.store.by_id[issued.id].calls,20)

    def test_invalid_size_and_route_declarations_fail_closed(self):
        with self.assertRaises(GatewayBudgetError):
            self.authorizer.authorize(self.headers,'/context','172.30.0.8',-1)
        with self.assertRaises(ValueError):
            self.authority.issue(task_id='task',attempt_id='attempt',generation=1,
                sandbox_id='sandbox',source_ip='not-an-ip',routes={'https://other'},
                expires_at=200,max_calls=1,max_request_bytes=1,max_response_bytes=1)

    def test_business_handler_stream_is_counted_and_closed(self):
        closed=[]
        core=NodeGatewayCore(self.authorizer,{'/context':lambda body,permit:
            GatewayResult(200,{'Content-Type':'application/json'},[b'one',b'two'],lambda:closed.append(True))})
        result=core.dispatch(self.headers,'/context','172.30.0.8',b'{}')
        self.assertEqual((result.status,b''.join(result)),(200,b'onetwo'))
        self.assertEqual(closed,[True])
        self.assertEqual(self.store.by_id[self.issued.id].response_bytes,6)
        self.assertEqual(self.store.by_id[self.issued.id].active,{})

    def test_revoke_or_response_overflow_closes_stream_before_more_output(self):
        closed=[]
        core=NodeGatewayCore(self.authorizer,{'/context':lambda body,permit:
            GatewayResult(200,{},[b'x'*13],lambda:closed.append(True))})
        result=core.dispatch(self.headers,'/context','172.30.0.8',b'{}')
        with self.assertRaises(GatewayBudgetError):list(result)
        self.assertEqual(closed,[True])

        # A fresh grant demonstrates active-connection revocation independently.
        issued=self.authority.issue(task_id='revoke',attempt_id='revoke-attempt',generation=1,
            sandbox_id='revoke-sandbox',source_ip='172.30.0.11',routes={'/context'},
            expires_at=200,max_calls=1,max_request_bytes=10,max_response_bytes=10)
        headers={'Authorization':'Bearer '+issued.token,'X-Video-Use-Attempt':'revoke-attempt',
            'X-Video-Use-Generation':'1','X-Video-Use-Sandbox':'revoke-sandbox'}
        second_closed=[]
        second=NodeGatewayCore(self.authorizer,{'/context':lambda body,permit:
            GatewayResult(200,{},[b'ok'],lambda:second_closed.append(True))}).dispatch(
                headers,'/context','172.30.0.11',b'{}')
        self.authority.revoke(issued.id);self.assertEqual(second_closed,[True])
        with self.assertRaises(GatewayAuthorizationError):list(second)

    def test_runtime_factory_issues_only_after_sandbox_identity_exists(self):
        from webapp.runtime_protocol import RuntimeIdentity
        factory=RuntimeGrantFactory(self.authority,task_id='task',
            source_ip=lambda identity:'172.30.0.12',endpoint='https://172.30.0.1:18880',
            ca=b'ca',routes=('/context',),ttl=50,max_calls=3,max_request_bytes=20,
            max_response_bytes=30)
        runtime=factory(RuntimeIdentity('late-attempt',4,'late-sandbox'))
        grants=[grant for grant in self.store.by_id.values() if grant.attempt_id=='late-attempt']
        self.assertEqual(len(grants),1);grant=grants[0]
        self.assertEqual((grant.sandbox_id,grant.source_ip,grant.expires_at),
            ('late-sandbox','172.30.0.12',150))
        token=runtime.material.token
        self.assertGreaterEqual(len(token),32);self.assertNotEqual(grant.token_hash,token)
        runtime.revoke();self.assertTrue(grant.revoked)


if __name__=='__main__':unittest.main()
