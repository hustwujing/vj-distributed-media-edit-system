"""Versioned text fan-out with bounded snapshots and immediate item completion."""
import os
import time
from . import agent_store
from .agent_stream import bus
from .task_leases import CURRENT


class TextPublisher:
    def __init__(self,store,task,redact=lambda text:text):
        self.store,self.task,self.redact=store,task,redact
        lease=CURRENT.get();self.generation=lease.generation if lease else 1
        self.attempt=lease.attempt if lease else ''
        from .public_messages import read
        self.history=read(store,task);self.messages={}
        self.sequence=0;self.published='';self.pending='';self.cursor='';self.started=False
        self.last_publish=0.;self.last_save=0.;self.phase='idle';self.dirty=False
        self.snapshot_seconds=max(.25,min(5.,float(os.environ.get('VIDEO_USE_AGENT_TEXT_SNAPSHOT_SECONDS','1'))))

    def message(self,identifier,text,phase='commentary',complete=False):
        if identifier not in self.messages and len(self.messages)>=200:raise ValueError('Public message limit reached')
        if len(text)>256000:raise ValueError('Public message exceeds output budget')
        self.message_id=identifier
        self.messages[identifier]={'text':self.redact(text),'phase':phase or 'commentary','status':'completed' if complete else 'streaming'}
        text='\n\n'.join([m['text'] for m in self.history]+[m['text'] for m in self.messages.values()])
        self.update(text,complete=complete)

    def update(self,text,complete=False):
        if len(text)>2*1024**2:raise ValueError('Public transcript exceeds output budget')
        self.pending=self.redact(text);self.phase='idle' if complete else 'streaming';self.dirty=True
        self.flush(complete=complete)

    def flush(self,complete=False):
        now=time.monotonic()
        if complete and self.started:self.dirty=True
        if not self.dirty:return
        prior=[self.generation,self.sequence];self.sequence+=1;version=[self.generation,self.sequence]
        payload={'text_version':version,'text_phase':self.phase,'published_at':time.time(),'message_id':getattr(self,'message_id','')}
        if complete or not self.started or not self.pending.startswith(self.published):
            kind='text_snapshot';payload['text']=self.pending
        else:
            kind='text_delta';payload.update(delta=self.pending[len(self.published):],base_version=prior)
        lease=CURRENT.get()
        if lease:payload['execution']={'attempt_id':lease.attempt,'generation':lease.generation}
        cursor=bus().publish(self.task,kind,payload)
        self.cursor=cursor or self.cursor;self.published=self.pending;self.started=True;self.dirty=False;self.last_publish=now
        if complete or now-self.last_save >= (self.snapshot_seconds if cursor else .5):
            from .public_messages import save
            with self.store.connect() as db:
                save(db,self.task,self.generation,self.attempt,self.messages,self.sequence,self.store.now())
                agent_store.save_text_snapshot(self.store,self.task,self.published,self.cursor,version,self.phase)
            from .telemetry import emit
            emit('stream.text_persisted',task_id=self.task,phase=self.phase,generation=self.generation,event_sequence=self.sequence,message_id=getattr(self,'message_id',''),bytes_sent=len(self.published.encode()))
            self.last_save=now
