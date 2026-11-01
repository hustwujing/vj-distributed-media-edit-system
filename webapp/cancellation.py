"""Process-local cancellation registry backed by shared, bounded observations.

One database observer and one local deadline watchdog per executor instance.
The watchdog never waits on database/network I/O. Late handle installation is
safe: cancellation is sticky until the registration is closed.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import signal
import socket
import threading
import time
import uuid


@dataclass
class Call:
    monitor: object
    task_id: str
    attempt_id: str
    generation: int
    kind: str
    deadline: float
    hard_deadline: float = float('inf')
    step_id: str | None = None
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancelled: threading.Event = field(default_factory=threading.Event)
    lock: object = field(default_factory=threading.Lock)
    reason: str = ''
    closed: bool = False
    callback: object = None
    dispatched: bool = False

    def install(self, callback):
        with self.lock:
            if self.closed:raise RuntimeError('Cannot install a handle after unregistering')
            if self.callback is not None:raise RuntimeError('Handle already installed')
            self.callback = callback
            self._dispatch()

    def _dispatch(self):
        if self.cancelled.is_set() and self.callback and not self.dispatched:
            self.dispatched = True
            self.monitor.dispatch(self)

    def cancel(self, reason):
        with self.lock:
            if self.closed:return
            if not self.cancelled.is_set():
                self.reason = reason
                self.cancelled.set()
            self._dispatch()

    def check(self):
        if self.cancelled.is_set():
            from .task_leases import LeaseLost
            raise LeaseLost('Execution stopped: '+self.reason)

    def close(self):
        with self.lock:self.closed = True
        with self.monitor.lock:self.monitor.calls.pop(self.call_id, None)

    def __enter__(self):return self
    def __exit__(self, *_):self.close()


class Monitor:
    def __init__(self, store, interval=.5, read_timeout=1):
        from .store import Store
        self.store = Store(store.root, timeout=read_timeout, initialize_schema=False, dsn=store.dsn)
        self.interval = interval
        self.lock = threading.Lock()
        self.calls = {}
        self.stop = threading.Event()
        self.stoppers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='execution-stop')
        self.observer = threading.Thread(target=self.observe_loop, daemon=True, name='execution-observer')
        self.watchdog = threading.Thread(target=self.deadline_loop, daemon=True, name='execution-watchdog')
        self.observer.start()
        self.watchdog.start()

    def register(self, task_id, attempt_id, generation, seconds, kind):
        if seconds <= 0:raise ValueError('An unexpired local permit is required')
        with self.lock:
            if self.stop.is_set():raise RuntimeError('Cancellation monitor closed')
            if len(self.calls) >= 1024:raise RuntimeError('Too many active local calls')
            call = Call(self, task_id, attempt_id, generation, kind, time.monotonic()+seconds)
            self.calls[call.call_id] = call
            return call

    def register_lease(self, lease, kind):
        # Validate before launch, then observe the same attempt independently.
        started = time.monotonic()
        lease.check()
        from .task_leases import CLOCK
        rows = self.store.query('SELECT expires,'+CLOCK+' AS observed FROM execution_leases WHERE turn_id=? AND attempt=? AND generation=? AND active=1',
                                (lease.turn_id, lease.attempt, lease.generation))
        remaining = (rows[0]['expires']-rows[0]['observed'])-(time.monotonic()-started) if rows else 0
        call=self.register(lease.turn_id, lease.attempt, lease.generation, remaining, kind)
        call.step_id=lease.step_id
        return call

    def snapshot(self):
        with self.lock:return list(self.calls.values())

    def dispatch(self, call):
        def invoke():
            # The callback owns the concrete process/socket object, never a
            # looked-up replacement. Unregister cannot redirect it elsewhere.
            with call.lock:
                if call.closed:return
                callback = call.callback
            try:callback()
            except Exception:pass  # Local reaper remains the final resource fence.
        self.stoppers.submit(invoke)

    def observe_once(self):
        calls = self.snapshot()
        if not calls:return
        tasks = list({c.task_id for c in calls})
        from .task_leases import CLOCK
        started = time.monotonic()
        rows = self.store.query('''SELECT l.turn_id,l.attempt,l.generation,l.active,l.expires,
          t.status,x.termination_reason,x.deadline_at,'''+CLOCK+''' AS observed
          FROM execution_leases l JOIN (
            SELECT id,status FROM agent_turns UNION ALL SELECT id,status FROM jobs
          ) t ON t.id=l.turn_id LEFT JOIN execution_limits x ON x.task_id=l.turn_id
          WHERE l.turn_id IN ('''+','.join('?' for _ in tasks)+')', tasks)
        by_attempt = {r['attempt']:r for r in rows}
        for call in calls:
            row = by_attempt.get(call.attempt_id)
            if not row or not row['active'] or row['attempt'] != call.attempt_id or row['generation'] != call.generation:
                call.cancel('ownership_lost')
            elif row['status'] in ('cancelled','cancelling','failed') or row['termination_reason']:
                call.cancel(row['termination_reason'] or row['status'])
            else:
                remaining = min(row['expires'], row['deadline_at'] or row['expires'])-row['observed']
                with call.lock:
                    if not call.cancelled.is_set():call.deadline = min(call.hard_deadline, started+max(0, remaining))

    def observe_loop(self):
        while not self.stop.is_set():
            try:self.observe_once()
            except Exception:pass  # Never extend permits when the database is unreachable.
            self.stop.wait(self.interval)

    def deadline_loop(self):
        while not self.stop.wait(min(.1, self.interval)):
            now = time.monotonic()
            for call in self.snapshot():
                if now >= call.deadline:call.cancel('permit_expired')

    def close(self):
        for call in self.snapshot():call.cancel('executor_shutdown')
        self.stop.set()
        self.observer.join(timeout=2)
        self.watchdog.join(timeout=2)
        self.stoppers.shutdown(wait=False)


_monitors = {}
_lock = threading.Lock()


def for_store(store):
    key = (os.getpid(), store.identity)
    with _lock:
        if key not in _monitors:
            _monitors[key] = Monitor(store, float(os.environ.get('VIDEO_USE_CANCEL_POLL_SECONDS', '.5')))
        return _monitors[key]


def stop_process(process, grace=5):
    """Own Popen until reaped; its PID cannot be reused while it is alive."""
    if process.poll() is not None:return
    try:os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:return
    try:process.wait(timeout=grace)
    except __import__('subprocess').TimeoutExpired:
        if process.poll() is None:
            try:os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:pass
        try:process.wait(timeout=5)
        except __import__('subprocess').TimeoutExpired:pass


@contextmanager
def process_call(lease, kind='local'):
    if lease is None:
        yield None
        return
    with for_store(lease.store).register_lease(lease, kind) as call:
        call.check()
        yield call


def interrupt_socket(sock):
    try:sock.shutdown(socket.SHUT_RDWR)
    except OSError:pass
