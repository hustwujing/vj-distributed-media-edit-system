"""Durable public replies only; final task text remains the consent input."""
import json


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS agent_messages(
      task_id TEXT NOT NULL,generation INTEGER NOT NULL,message_id TEXT NOT NULL,
      attempt TEXT NOT NULL,position INTEGER NOT NULL,phase TEXT NOT NULL,text TEXT NOT NULL,
      status TEXT NOT NULL,sequence INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
      PRIMARY KEY(task_id,generation,message_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS agent_render_observations(task_id TEXT NOT NULL,generation INTEGER NOT NULL,sequence INTEGER NOT NULL,render_ms REAL NOT NULL,created REAL NOT NULL,PRIMARY KEY(task_id,generation,sequence))''')


def read(store,task):
    return store.query('SELECT * FROM agent_messages WHERE task_id=? ORDER BY generation,position',(task,))


def save(db,task,generation,attempt,messages,sequence,now):
    for position,(identifier,item) in enumerate(messages.items()):
        db.execute('''INSERT INTO agent_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(task_id,generation,message_id) DO UPDATE SET
          phase=excluded.phase,text=excluded.text,status=excluded.status,sequence=excluded.sequence,updated=excluded.updated
          WHERE agent_messages.sequence<excluded.sequence''',
          (task,generation,identifier,attempt,position,item['phase'],item['text'],item['status'],sequence,now,now))


def display(row):
    messages=row.get('messages',[])
    if not messages and row['status'] in ('queued','running','waiting','paused','cancelling'):return row['text']
    if messages:
        texts=[m['text'] for m in messages if m['text']]
    else:
        texts=[n['text'] for n in row.get('public_notes',[]) if n.get('text')]
    final=row.get('text','')
    joined='\n\n'.join(texts)
    if joined and (final==joined or final.startswith(joined+'\n\n')):return final
    if final and final not in texts:texts.append(final)
    return '\n\n'.join(texts)
