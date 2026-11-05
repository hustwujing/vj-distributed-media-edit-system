"""Executor-side client for the sandbox Runtime Daemon control channel."""
from __future__ import annotations

from .runtime_protocol import ExecutorProtocol,ProtocolError,decode_frame,encode_frame


class RuntimeTransportError(ConnectionError):
    pass


class RemoteRuntimeError(RuntimeError):
    def __init__(self,error):
        self.error=error
        super().__init__(error.get('message','Runtime request failed'))


class WebSocketTransport:
    """Production transport, imported lazily so protocol tests need no socket."""
    def __init__(self,url,*,headers=None,timeout=10):
        try:
            from websockets.sync.client import connect
            self.socket=connect(url,additional_headers=headers or {},open_timeout=timeout,
                close_timeout=timeout,max_size=1024*1024,proxy=None)
        except Exception as exc:raise RuntimeTransportError('Runtime WebSocket connection failed') from exc

    def send(self,frame):
        try:self.socket.send(encode_frame(frame).decode())
        except Exception as exc:raise RuntimeTransportError('Runtime WebSocket send failed') from exc

    def receive(self,timeout):
        try:return decode_frame(self.socket.recv(timeout=timeout))
        except Exception as exc:raise RuntimeTransportError('Runtime WebSocket receive failed') from exc

    def close(self):
        try:self.socket.close()
        except Exception:pass


class RuntimeClient:
    def __init__(self,url,identity,token,on_event,*,headers=None,api_key='',timeout=30,connector=None):
        self.url=url;self.protocol=ExecutorProtocol(identity,token);self.on_event=on_event
        self.timeout=timeout;self.headers=dict(headers or {});self.api_key=api_key
        self.connector=connector or self._connect_transport
        self.connection=None

    def _connect_transport(self):
        headers=dict(self.headers)
        if self.api_key:headers['OPEN-SANDBOX-API-KEY']=self.api_key
        return WebSocketTransport(self.url,headers=headers,timeout=self.timeout)

    def connect(self):
        self.close();self.connection=self.connector()
        try:
            self.connection.send(self.protocol.hello())
            acknowledgement=self.connection.receive(self.timeout)
            if acknowledgement.get('type')!='hello.ack':raise ProtocolError('Runtime did not acknowledge handshake')
            target=acknowledgement.get('next_seq',0)-1
            self.protocol.receive(acknowledgement)
            while self.protocol.last_seq<target:self._receive()
            return acknowledgement
        except Exception:
            self.close();raise

    def _receive(self):
        frame=self.connection.receive(self.timeout)
        previous_sequence=self.protocol.last_seq
        event=self.protocol.receive(frame)
        if frame.get('type')=='event':
            try:
                if event is not None:self.on_event(event)
            except Exception:
                self.protocol.last_seq=previous_sequence
                self.close()
                raise
            # ACK only after the durable event callback succeeds.
            self.connection.send(self.protocol.acknowledge())
        return frame

    def receive_once(self):
        if self.connection is None:self.connect()
        return self._receive()

    def _wait(self,request_id):
        while request_id not in self.protocol.responses:self._receive()
        response=self.protocol.responses.pop(request_id)
        if 'error' in response:raise RemoteRuntimeError(response['error'])
        return response.get('result',{})

    def call(self,method,params):
        request=self.protocol.request(method,params)
        for attempt in range(2):
            try:
                if self.connection is None:self.connect()
                self.connection.send(request)
                return self._wait(request['id'])
            except RuntimeTransportError:
                self.close()
                if attempt:raise
        raise AssertionError('unreachable')

    def close(self):
        connection,self.connection=self.connection,None
        self.protocol.connected=False
        if connection is not None:connection.close()
