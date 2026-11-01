"""Redis Streams transport for live Agent output.

The database remains the durable source of truth. Redis is only a bounded,
expiring fan-out path for SSE clients, so the application still works when
Redis is not configured or temporarily unavailable.
"""
from __future__ import annotations

import json
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
        self._publish_lock=threading.Lock();self._publisher=None;self._events=None

    @property
    def enabled(self) -> bool:
        return bool(self.url) and time.monotonic() >= self._retry_after

    def failed(self,phase,exc):
        from .telemetry import emit
        emit('stream.transport_failed','WARNING',reason=phase,exception_type=type(exc).__name__)
        self._retry_after=time.monotonic()+5
        self._client=None

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

    def key(self, turn_id: str) -> str:
        return self.prefix + turn_id

    def publish(self, turn_id: str, kind: str, payload: dict[str, Any]) -> str | None:
        client = self._get()
        if client is None:
            return None
        try:
            message = client.xadd(self.key(turn_id), {
                "kind": kind,
                "payload": json.dumps(payload, ensure_ascii=False),
            }, maxlen=self.maxlen, approximate=True)
            client.expire(self.key(turn_id), self.ttl)
            return str(message)
        except Exception as exc:
            # A Redis outage must not stop a task or make the durable DB path
            # unavailable. The next process/request will retry from config.
            self.failed('publish',exc)
            return None

    def publish_async(self,turn_id,kind,payload):
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
                        finally:self._events.task_done()
                self._publisher=threading.Thread(target=dispatch,daemon=True);self._publisher.start()
            try:self._events.put_nowait((turn_id,kind,payload))
            except queue.Full:
                from .telemetry import emit
                emit('stream.activity_dropped','WARNING',task_id=turn_id,reason='queue_full')

    def read(self, turn_id: str, after: str, count: int = 200,
             block_ms: int = 1000) -> list[dict[str, Any]] | None:
        client = self._get()
        if client is None:
            return None
        try:
            rows = client.xread({self.key(turn_id): after}, count=count, block=block_ms)
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


def bus() -> StreamBus:
    global _bus
    with _lock:
        if _bus is None:
            _bus = StreamBus()
        return _bus
