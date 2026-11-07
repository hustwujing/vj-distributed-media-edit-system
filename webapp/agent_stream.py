"""Redis Streams transport for live Agent output.

The database remains the durable source of truth. Redis is only a bounded,
expiring fan-out path for SSE clients, so the application still works when
Redis is not configured or temporarily unavailable.
"""
from __future__ import annotations

import json
import asyncio
import os
import queue
import threading
import time
from typing import Any


_lock = threading.Lock()
_bus: "StreamBus | None" = None


class StreamBus:
    def __init__(self) -> None:
        self.url = os.environ.get("VIDEO_USE_REDIS_URL", "").strip()
        self.prefix = os.environ.get("VIDEO_USE_AGENT_STREAM_PREFIX", "agent:events:")
        self.ttl = int(os.environ.get("VIDEO_USE_AGENT_STREAM_TTL", "3600"))
        self.maxlen = int(os.environ.get("VIDEO_USE_AGENT_STREAM_MAXLEN", "10000"))
        self._client = None
        self._retry_after = 0.0
        self._publish_lock=threading.Lock();self._publisher=None;self._events=None;self._expires={}

    @property
    def enabled(self) -> bool:
        return bool(self.url) and time.monotonic() >= self._retry_after

    def failed(self,phase,exc):
        from .telemetry import emit
        emit('stream.transport_failed','WARNING',reason=phase,exception_type=type(exc).__name__)
        self._retry_after=time.monotonic()+5
        self._client=None;self._expires={}

    def _get(self):
        if not self.enabled:
            return None
        if self._client is None:
            try:
                import redis
                self._client = redis.Redis.from_url(self.url, decode_responses=True,
                                                    socket_connect_timeout=1,
                                                    socket_timeout=2)
                self._client.ping()
            except Exception as exc:
                self.failed('connect',exc)
        return self._client

    def key(self, run_id: str) -> str:
        return self.prefix + run_id

    def publish(self, run_id: str, kind: str, payload: dict[str, Any]) -> str | None:
        client = self._get()
        if client is None:
            return None
        try:
            key=self.key(run_id);message = client.xadd(key, {
                "kind": kind,
                "payload": json.dumps(payload, ensure_ascii=False),
            }, maxlen=self.maxlen, approximate=True)
            now=time.monotonic()
            if now>=self._expires.get(key,0):
                client.expire(key,self.ttl);self._expires[key]=now+max(1,min(60,self.ttl/4))
                if len(self._expires)>10000:self._expires={name:expiry for name,expiry in self._expires.items() if expiry>now}
            return str(message)
        except Exception as exc:
            # A Redis outage must not stop a task or make the durable DB path
            # unavailable. The next process/request will retry from config.
            self.failed('publish',exc)
            return None

    def publish_async(self,run_id,kind,payload):
        if not self.url:return
        with self._publish_lock:
            if self._publisher is None or not self._publisher.is_alive():
                self._events=queue.Queue(maxsize=256)
                def dispatch():
                    from .telemetry import bind
                    while True:
                        task,event,body=self._events.get()
                        try:
                            with bind(task_id=task,attempt_id=(body.get('execution') or {}).get('attempt_id')):
                                self.publish(task,event,body)
                        except Exception as exc:
                            self.failed('publish_queue',exc)
                        finally:self._events.task_done()
                self._publisher=threading.Thread(target=dispatch,daemon=True);self._publisher.start()
            try:self._events.put_nowait((run_id,kind,payload))
            except queue.Full:
                from .telemetry import emit
                emit('stream.activity_dropped','WARNING',task_id=run_id,reason='queue_full')

    def flush_async(self,timeout=5):
        events=self._events
        if events is None:return True
        deadline=time.monotonic()+timeout
        while events.unfinished_tasks and time.monotonic()<deadline:time.sleep(.01)
        return not events.unfinished_tasks

    def read(self, run_id: str, after: str, count: int = 200,
             block_ms: int = 1000) -> list[dict[str, Any]] | None:
        client = self._get()
        if client is None:
            return None
        try:
            rows = client.xread({self.key(run_id): after}, count=count, block=block_ms)
            result: list[dict[str, Any]] = []
            for _key, records in rows:
                for message_id, fields in records:
                    try:
                        payload = json.loads(fields.get("payload", "{}"))
                    except (TypeError, ValueError):
                        payload = {"text": "消息内容无法解析。"}
                    result.append({"id": str(message_id), "kind": fields.get("kind", "activity"),
                                   "payload": payload})
            return result
        except Exception as exc:
            self.failed('read',exc)
            return None


class AsyncStreamReader:
    """One bounded native asyncio connection owned by a live SSE response."""
    def __init__(self,transport):
        self.transport=transport;self.url=transport.url;self._client=None

    @property
    def enabled(self):return self.transport.enabled

    async def read(self,run_id,after,count=200,block_ms=200):
        if not self.enabled:return None
        try:
            if self._client is None:
                import redis.asyncio as redis_async
                self._client=redis_async.Redis.from_url(self.url,decode_responses=True,
                    socket_connect_timeout=1,socket_timeout=2,max_connections=1)
            rows=await self._client.xread({self.transport.key(run_id):after},count=count,block=block_ms)
            result=[]
            for _,records in rows:
                for message_id,fields in records:
                    try:payload=json.loads(fields.get('payload','{}'))
                    except (TypeError,ValueError):payload={'text':'消息内容无法解析。'}
                    result.append({'id':str(message_id),'kind':fields.get('kind','activity'),'payload':payload})
            return result
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.transport.failed('async_read',exc)
            await self.aclose()
            return None

    async def latest_id(self,run_id):
        if not self.enabled:return None
        try:
            if self._client is None:
                import redis.asyncio as redis_async
                self._client=redis_async.Redis.from_url(self.url,decode_responses=True,
                    socket_connect_timeout=1,socket_timeout=2,max_connections=1)
            rows=await self._client.xrevrange(self.transport.key(run_id),count=1)
            return str(rows[0][0]) if rows else '0-0'
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.transport.failed('async_latest',exc);await self.aclose();return None

    async def aclose(self):
        client,self._client=self._client,None
        if client is not None:
            try:
                async with asyncio.timeout(2):await client.aclose()
            except Exception as exc:
                from .telemetry import emit
                emit('stream.transport_failed','WARNING',reason='async_close',exception_type=type(exc).__name__)


def bus() -> StreamBus:
    global _bus
    with _lock:
        if _bus is None:
            _bus = StreamBus()
        return _bus
