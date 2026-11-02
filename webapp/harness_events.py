"""Public harness events; no dependency on native session file formats."""
from dataclasses import dataclass, field
import json


KINDS = {'message.delta', 'message.completed', 'tool.started', 'tool.completed', 'tool.output',
         'plan.updated', 'interaction.requested', 'usage.reported', 'turn.ended', 'error'}


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    id: str = ''
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in KINDS or len(self.id) > 256:
            raise ValueError('Invalid harness event')
        if len(json.dumps(self.data, ensure_ascii=False).encode()) > 8 * 1024**2:
            raise ValueError('Harness event exceeds size limit')


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS agent_context_events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL, generation INTEGER NOT NULL, harness TEXT NOT NULL,
        event_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload TEXT NOT NULL,
        created REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS agent_context_task ON agent_context_events(task_id,seq);
    ''')


def record(store, task, harness, event, sequence, redact):
    """Record only public, bounded evidence under the current business fence."""
    from .task_leases import CURRENT
    lease = CURRENT.get()
    if not lease:
        return
    # Streaming deltas remain in the existing text publisher, not duplicated here.
    if event.kind == 'message.delta':
        return
    data = dict(event.data)
    if event.kind == 'usage.reported':
        # SDK evidence is not another billable request or a second token counter.
        allowed = {'response_id', 'usage', 'scope'}
        data = {k: v for k, v in data.items() if k in allowed}
    encoded = redact(json.dumps(data, ensure_ascii=False))
    if len(encoded.encode()) > 65536:
        encoded = json.dumps({'truncated': True, 'text': encoded[:12000]})
    key = f'{lease.attempt}:{sequence}:{event.kind}'
    with store.connect() as db:
        lease.validate(db)
        db.execute('''INSERT OR IGNORE INTO agent_context_events
          (task_id,attempt_id,generation,harness,event_key,kind,payload,created)
          VALUES (?,?,?,?,?,?,?,?)''',
                   (task, lease.attempt, lease.generation, harness, key, event.kind, encoded, store.now()))
