"""ASGI control endpoint hosted inside an Agent sandbox.

The OpenSandbox proxy carries this WebSocket in both directions.  Harness
process ownership is supplied by a driver; this module owns only authenticated
transport, replay and one-controller-at-a-time semantics.
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI,WebSocket,WebSocketDisconnect

from .runtime_protocol import (
    AuthenticationError,
    DaemonProtocol,
    ProtocolError,
    RuntimeIdentity,
    decode_frame,
)


class RuntimeControlService:
    def __init__(self,identity,token,dispatch,health=None):
        self.protocol=DaemonProtocol(identity,token)
        self.dispatch=dispatch;self.health=health or (lambda:{'ready':True})
        self._outbound=None;self._loop=None

    def publish(self,event):
        frame=self.protocol.publish(event)
        loop,queue=self._loop,self._outbound
        if loop is not None and queue is not None:
            try:loop.call_soon_threadsafe(queue.put_nowait,frame)
            except RuntimeError:pass  # The frame remains in the replay buffer.
        return frame

    async def _send(self,websocket,queue):
        while True:await websocket.send_json(await queue.get())

    async def connect(self,websocket):
        await websocket.accept()
        if self._outbound is not None:
            await websocket.close(code=4429,reason='Runtime controller already connected')
            return
        queue=asyncio.Queue();self._outbound=queue;self._loop=asyncio.get_running_loop()
        sender=asyncio.create_task(self._send(websocket,queue))
        try:
            while True:
                frame=decode_frame(await websocket.receive_text())
                responses=await asyncio.to_thread(self.protocol.receive,frame,self.dispatch)
                for response in responses:queue.put_nowait(response)
        except WebSocketDisconnect:
            pass
        except AuthenticationError as exc:
            await websocket.close(code=4403,reason=str(exc)[:120])
        except ProtocolError as exc:
            await websocket.close(code=4400,reason=str(exc)[:120])
        finally:
            sender.cancel()
            try:await sender
            except (asyncio.CancelledError,WebSocketDisconnect):pass
            self.protocol.disconnect();self._outbound=None;self._loop=None


def create_app(identity:RuntimeIdentity,token,dispatch,health=None):
    service=RuntimeControlService(identity,token,dispatch,health)
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    app.state.runtime_service=service

    @app.get('/healthz')
    def healthz():
        return {**identity.envelope(),**service.health()}

    @app.websocket('/control')
    async def control(websocket:WebSocket):
        await service.connect(websocket)

    return app
