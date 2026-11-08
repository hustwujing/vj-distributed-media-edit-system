"""Server database transport for the application's parameterized SQL.

PostgreSQL transactions serialize short control-plane mutations using one
transaction-scoped advisory lock. This preserves cross-table quota and fencing
invariants. Read-only observations never acquire that lock. No network or tool
work belongs inside a control transaction.
"""
import re

SQLITE_CLOCK = "((julianday('now') - 2440587.5) * 86400.0)"
CONTROL_LOCK = 0x564944454F


def parameters(sql, named=False):
    """Translate placeholders, never SQL string literals or identifiers."""
    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
    for i in range(0, len(parts), 2):
        parts[i] = (re.sub(r'(?<!:):([a-zA-Z_]\w*)', r'%(\1)s', parts[i])
                    if named else parts[i].replace('?', '%s'))
    return ''.join(parts)


def sql_for_postgres(sql, named=False):
    sql = sql.strip().rstrip(';')
    if sql.upper() == 'BEGIN IMMEDIATE':
        return 'SELECT 1'
    if sql.upper().startswith('PRAGMA TABLE_INFO'):
        table = re.fullmatch(r'PRAGMA table_info\((\w+)\)', sql, re.I).group(1)
        return ("SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='" + table + "'")
    if sql.upper().startswith('PRAGMA JOURNAL_MODE'):
        return 'SELECT 1'
    sql = sql.replace(SQLITE_CLOCK, 'CAST(EXTRACT(EPOCH FROM clock_timestamp()) AS DOUBLE PRECISION)')
    sql = sql.replace('sqlite_master', "(SELECT tablename AS name, 'table' AS type FROM pg_tables WHERE schemaname=current_schema()) AS catalog")
    sql = re.sub(r'INTEGER PRIMARY KEY AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY', sql, flags=re.I)
    if re.match(r'(CREATE|ALTER)\s+TABLE', sql, re.I):
        sql = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', sql, flags=re.I)
    sql = sql.replace('MIN(expires,?)', 'LEAST(expires,?)')
    sql = sql.replace('MAX(execution_workers.draining,excluded.draining)',
                      'GREATEST(execution_workers.draining,excluded.draining)')
    if re.match(r'INSERT OR IGNORE\b', sql, re.I):
        sql = re.sub(r'^INSERT OR IGNORE', 'INSERT', sql, flags=re.I) + ' ON CONFLICT DO NOTHING'
    pieces = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
    for i in range(0, len(pieces), 2):
        pieces[i] = re.sub(r'\bauthorization\b', '"authorization"', pieces[i])
    sql = ''.join(pieces)
    return parameters(sql.replace('%', '%%'), named)


class PostgresConnection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        return self.raw.execute(sql_for_postgres(sql, isinstance(args, dict)), args)

    def executescript(self, script):
        for sql in script.split(';'):
            if sql.strip():
                self.execute(sql)


def connect(dsn, timeout, *, readonly=False, control_lock=True, snapshot=False):
    import psycopg
    from psycopg.rows import dict_row
    # A separate bounded observation connection is used by cancellation monitors.
    raw = psycopg.connect(dsn, connect_timeout=max(1, int(timeout)), row_factory=dict_row)
    try:
        if snapshot:
            if not readonly:raise ValueError('Observation snapshots must be read-only')
            raw.isolation_level=psycopg.IsolationLevel.REPEATABLE_READ
        raw.read_only=readonly
        raw.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, int(timeout * 1000))),))
        raw.execute("SELECT set_config('lock_timeout', %s, true)", (str(max(1, int(timeout * 1000))),))
        if not readonly and control_lock:
            raw.execute('SELECT pg_advisory_xact_lock(%s)', (CONTROL_LOCK,))
        return raw
    except BaseException:
        raw.close()
        raise
