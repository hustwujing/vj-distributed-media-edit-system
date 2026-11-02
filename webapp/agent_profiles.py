"""Administrator-owned, versioned Agent configurations frozen on each task.

Credentials are environment references, never values in snapshots or events.
Changing the default profile does not change an existing task's configuration.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
import re


HARNESS_PROTOCOLS = {
    'codex': {'responses'},
    'claude_code': {'anthropic'},
    # The pinned DeepSeek SDK uses the Messages wire protocol for V4 models.
    'deepseek_harness': {'anthropic'},
}


def integer(name, default, maximum=10_000_000):
    value = int(os.environ.get(name, str(default)))
    if not 1 <= value <= maximum:
        raise ValueError('Invalid budget setting: ' + name)
    return value


def configured_budget():
    return {
        'plan_seconds': integer('VIDEO_USE_PLAN_SECONDS', 240),
        'plan_models': integer('VIDEO_USE_PLAN_MODELS', 14),
        'plan_tools': integer('VIDEO_USE_PLAN_TOOLS', 24),
        'plan_inspections': integer('VIDEO_USE_PLAN_INSPECTIONS', 8),
        'run_seconds': integer('VIDEO_USE_AGENT_RUN_SECONDS', 900),
        'run_models': integer('VIDEO_USE_AGENT_RUN_MODEL_CALLS', 160),
        'task_models': integer('VIDEO_USE_TASK_MODEL_CALLS', 40),
        'max_output_tokens': integer('VIDEO_USE_MODEL_OUTPUT_TOKENS', 16000),
    }


@dataclass(frozen=True)
class AgentProfile:
    name: str = 'default'
    harness: str = 'codex'
    model: str = 'gpt-6-astra'
    protocol: str = 'responses'
    base_url: str = ''
    credential_env: str = 'VIDEO_USE_MODEL_API_KEY'
    reasoning_effort: str = 'medium'
    adapter_version: int = 1
    tool_policy: str = 'media-restricted-v1'
    budget: dict = field(default_factory=configured_budget)

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.name):
            raise ValueError('Invalid Agent profile name')
        if self.harness not in HARNESS_PROTOCOLS or self.protocol not in HARNESS_PROTOCOLS[self.harness]:
            raise ValueError('Unsupported harness/model protocol combination')
        if self.adapter_version != 1 or self.tool_policy != 'media-restricted-v1':
            raise ValueError('Unsupported Agent adapter or tool policy version')
        if not self.model or len(self.model) > 200 or any(ord(c) < 32 for c in self.model):
            raise ValueError('Invalid model identifier')
        if not re.fullmatch(r'(?:VIDEO_USE_|ANTHROPIC_|DEEPSEEK_|OPENAI_)[A-Z0-9_]*(?:KEY|TOKEN)', self.credential_env):
            raise ValueError('Credential must be an approved server environment reference')
        if self.base_url:
            from .egress_policy import provider_base
            provider_base(self.base_url)
        efforts = {'codex': {'minimal', 'low', 'medium', 'high', 'xhigh'},
                   'claude_code': {'low', 'medium', 'high', 'max'},
                   'deepseek_harness': {'none', 'low', 'medium', 'high', 'max'}}
        if self.reasoning_effort not in efforts[self.harness]:
            raise ValueError('Reasoning setting is not supported by the selected harness')
        expected = {'plan_seconds','plan_models','plan_tools','plan_inspections','run_seconds','run_models','task_models','max_output_tokens'}
        if set(self.budget) != expected or any(type(v) is not int or not 1 <= v <= 10_000_000 for v in self.budget.values()):
            raise ValueError('Invalid Agent budget policy')

    def snapshot(self):
        return asdict(self)

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.snapshot(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def public(self):
        return {k: v for k, v in self.snapshot().items() if k not in ('base_url', 'credential_env')} | {'digest': self.digest}

    def credentials(self):
        key = os.environ.get(self.credential_env, '')
        if not self.base_url or not key:
            raise ValueError('Selected Agent profile has no configured model service')
        return self.base_url.rstrip('/'), key


def profiles():
    default = AgentProfile(model=os.environ.get('VIDEO_USE_MODEL', 'gpt-6-astra'),
                           base_url=os.environ.get('VIDEO_USE_MODEL_BASE_URL', '').rstrip('/'),
                           reasoning_effort=os.environ.get('VIDEO_USE_MODEL_REASONING_EFFORT', 'medium'))
    raw = os.environ.get('VIDEO_USE_AGENT_PROFILES_JSON', '{}')
    if len(raw) > 65536:
        raise ValueError('Agent profile configuration exceeds limit')
    values = json.loads(raw)
    if not isinstance(values, dict) or len(values) > 32:
        raise ValueError('Invalid Agent profile registry')
    result = {'default': default}
    for name, value in values.items():
        if not isinstance(value, dict) or 'name' in value:
            raise ValueError('Invalid Agent profile declaration')
        result[name] = AgentProfile(name=name, **value)
    return result


def selected(name=None):
    name = name or os.environ.get('VIDEO_USE_AGENT_PROFILE', 'default')
    try:
        return profiles()[name]
    except KeyError:
        raise ValueError('Unknown Agent profile') from None


def for_task(store, task):
    if not store.query("SELECT name FROM sqlite_master WHERE name='agent_run_profiles'"):
        return selected()
    rows = store.query('SELECT configuration FROM agent_run_profiles WHERE task_id=?', (task,))
    return AgentProfile(**json.loads(rows[0]['configuration'])) if rows else selected()


def current():
    from .task_leases import CURRENT
    lease = CURRENT.get()
    if not lease:
        return selected()
    cached = getattr(lease, 'agent_profile', None)
    if cached is None:
        lease.agent_profile = cached = for_task(lease.store, lease.turn_id)
    return cached


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS agent_run_profiles(
        task_id TEXT PRIMARY KEY, configuration TEXT NOT NULL, digest TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS agent_project_profiles(
        project TEXT PRIMARY KEY, name TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS agent_attempt_profiles(
        attempt_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,configuration TEXT NOT NULL,
        digest TEXT NOT NULL,created REAL NOT NULL,native_session_id TEXT NOT NULL DEFAULT '',
        restore_mode TEXT NOT NULL DEFAULT '');
    ''')


def freeze(db, task, project, created, parent=None):
    old = db.execute('SELECT configuration FROM agent_run_profiles WHERE task_id=?', (parent,)).fetchone() if parent else None
    preferred = db.execute('SELECT name FROM agent_project_profiles WHERE project=?', (project,)).fetchone()
    profile = selected(preferred['name']) if preferred else AgentProfile(**json.loads(old['configuration'])) if old else selected()
    db.execute('INSERT INTO agent_run_profiles(task_id,configuration,digest,created) VALUES (?,?,?,?)',
               (task, json.dumps(profile.snapshot(), sort_keys=True), profile.digest, created))
    return profile


def record_attempt(store,lease,profile):
    with store.connect() as db:
        lease.validate(db)
        old=db.execute('SELECT digest FROM agent_attempt_profiles WHERE attempt_id=?',(lease.attempt,)).fetchone()
        if old and old['digest']!=profile.digest:raise ValueError('Running attempt cannot change Agent configuration')
        db.execute('''INSERT OR IGNORE INTO agent_attempt_profiles
          (attempt_id,task_id,configuration,digest,created) VALUES (?,?,?,?,?)''',
                   (lease.attempt,lease.turn_id,json.dumps(profile.snapshot()),profile.digest,store.now()))


def public_for_task(store,task):
    profile=for_task(store,task)
    recorded=bool(store.query('SELECT task_id FROM agent_run_profiles WHERE task_id=?',(task,)))
    result=profile.public()|{'configuration_known':recorded}
    if not recorded:result.update(harness='历史未记录',model='历史未记录',reasoning_effort='历史未记录')
    return result
