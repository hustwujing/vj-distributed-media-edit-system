"""Executor-side Agent loop for an OpenSandbox Runtime Daemon."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path,PurePosixPath
import shutil
import time
import uuid

from .agent_activity import ActivityTracker
from .agent_text import TextPublisher
from . import agent_store,workflows


class RemoteCheckpointSink:
    def __init__(self,store,lease,project,runtime_profile):
        self.store=store;self.lease=lease;self.project=project;self.runtime_profile=runtime_profile
        self.target=store.project_dir(project)/'execution-checkpoints'/(lease.attempt+'-'+uuid.uuid4().hex)
        self.target.mkdir(parents=True,mode=0o700);self.records={'workspace':{},'state':{}}
        self.deliveries=[]

    def write(self,group,name,data,record):
        relative=PurePosixPath(name)
        if group not in self.records or relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid remote checkpoint path')
        target=self.target/group/relative
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with target.open('xb') as stream:stream.write(data)
        target.chmod(0o700 if record['executable'] else 0o600)
        saved={'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),
               'executable':record['executable']}
        if self.store.shared_files:
            uploaded=self.store.artifacts.upload(self.project,target);saved['artifact']=uploaded['id']
        self.records[group][name]=saved
        if group=='workspace' and name.startswith('edit/') and not name.startswith('edit/previews/'):
            relative=PurePosixPath(name).relative_to('edit')
            destination=self.store.project_dir(self.project)/'edit/agent_exports'/self.lease.run_id/relative
            destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            temporary=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
            temporary.write_bytes(data);temporary.chmod(0o600);temporary.replace(destination)
            artifact={'name':relative.name,'file':str(destination.relative_to(self.store.project_dir(self.project)))}
            if self.store.shared_files:
                artifact['artifact_id']=self.store.artifacts.save_file(self.project,artifact['file'])['id']
            self.deliveries.append(artifact)

    def commit(self,session_id):
        relative=str(self.target.relative_to(self.store.root));encoded=json.dumps(self.records,sort_keys=True)
        if self.store.shared_files:
            project_root=self.store.project_dir(self.project)
            for group,records in self.records.items():
                for name,record in records.items():
                    logical=str((self.target/group/name).relative_to(project_root))
                    self.store.artifacts.publish(self.project,logical,record['artifact'],expected_revision=0)
        with self.store.connect() as db:
            self.lease.validate(db)
            timestamp=self.store.now()
            db.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET attempt=excluded.attempt,path=excluded.path,manifest=excluded.manifest,updated=excluded.updated',
                       (self.project,self.lease.attempt,relative,encoded,timestamp))
            db.execute('UPDATE execution_attempts SET checkpoint=? WHERE id=?',(relative,self.lease.attempt))
            db.execute('INSERT INTO agent_sessions VALUES (?,?) ON CONFLICT(project) DO UPDATE SET thread_id=excluded.thread_id',(self.project,session_id))
            db.execute('INSERT INTO execution_session_profiles VALUES (?,?) ON CONFLICT(project) DO UPDATE SET profile=excluded.profile',(self.project,self.runtime_profile))
        return self.deliveries

    def abort(self):shutil.rmtree(self.target,ignore_errors=True)


@dataclass
class RemoteTurnResult:
    text: str
    artifacts: list
    session_id: str


class RemoteAgentRunner:
    def __init__(self,host):self.host=host;self.store=host.store

    def run(self,lease,row,profile,*,text=None):
        runtime=lease.runtime;run_id=lease.run_id;project=row['project']
        messages={};completed=None;event_sequence=0;final=''
        text=text or TextPublisher(self.store,run_id,self.host.redact)
        deadline=time.monotonic()+profile.budget['run_seconds']
        def check():
            if self.host.stop.is_set():
                from .agent_host import Shutdown
                try:runtime.interrupt()
                finally:raise Shutdown()
            lease.check()
            if time.monotonic()>deadline:
                raise TimeoutError('本轮已达到 Agent 运行时限，已停止；已有任务状态保留。')
        check()
        tracker=ActivityTracker(lambda kind,payload:self.host.notify(run_id,kind,payload),
            self.store.assets(project))
        def event_received(event):
            nonlocal completed,event_sequence,final
            event_sequence+=1
            from .harness_events import record
            record(self.store,run_id,profile.harness,event,event_sequence,self.host.redact)
            tracker.handle_event(event)
            if event.kind=='message.delta':
                messages[event.id]=messages.get(event.id,'')+event.data.get('delta','')
                if len(messages[event.id])>256000:raise ValueError('Remote public message exceeds limit')
                text.message(event.id,messages[event.id])
            elif event.kind=='message.completed':
                messages[event.id]=event.data.get('text','')[:256000]
                if event.data.get('phase','final_answer')=='final_answer':final=messages[event.id]
                text.message(event.id,messages[event.id],event.data.get('phase','final_answer'),complete=True)
            elif event.kind=='turn.ended':completed=event.data
        runtime.set_event_handler(event_received)
        runtime_profile=getattr(lease.backend_binding,'runtime_profile','opensandbox')
        checkpoint=self.store.query('SELECT path FROM execution_checkpoints WHERE project=?',(project,))
        session=self.store.query('SELECT thread_id FROM agent_sessions WHERE project=?',(project,))
        compatible=self.store.query('SELECT profile FROM execution_session_profiles WHERE project=?',(project,))
        prior=session[0]['thread_id'] if checkpoint and session and compatible and compatible[0]['profile']==runtime_profile else None
        opened=runtime.open_session(__import__('webapp.agent_context',fromlist=['INSTRUCTIONS']).INSTRUCTIONS,prior)
        session_id=opened['session_id']
        from .harness_switching import record_session
        record_session(self.store,project,profile,session_id,'native_resume' if prior else 'public_context',runtime_profile)
        phase='方案讨论' if row['mode']=='plan' else '已确认执行'
        prompt=f'当前阶段：{phase}。\n用户消息：{row["prompt"]}'
        runtime.run_turn(session_id,prompt)
        while completed is None:
            check()
            runtime.receive_once()
        check()
        if completed.get('status')!='completed':raise RuntimeError(str(completed.get('error') or 'Remote Agent turn failed'))
        exported=runtime.export_session();sink=RemoteCheckpointSink(
            self.store,lease,project,runtime_profile)
        try:
            runtime.collect(exported['manifest'],sink.write)
            artifacts=sink.commit(exported.get('session_id') or session_id)
        except Exception:
            sink.abort();raise
        final=final or next((value for key,value in reversed(messages.items()) if value),None) or '本轮已结束。'
        text.flush(complete=True)
        if row['mode']=='plan':
            from .plan_readiness import review
            review(self.store,run_id,final)
        workflow=workflows.get(self.store,run_id)
        if workflow:
            self.store.execute('UPDATE agent_turns SET artifacts=? WHERE id=?',(json.dumps(artifacts,ensure_ascii=False),run_id))
            workflows.state(self.store,run_id,'ready' if workflow['status']=='staged' else 'paused',
                self.host.redact(final),error='' if workflow['status']=='staged' else '等待后续工作流状态确认。')
        else:agent_store.finish(self.store,run_id,'succeeded',self.host.redact(final),artifacts)
        return RemoteTurnResult(final,artifacts,session_id)
