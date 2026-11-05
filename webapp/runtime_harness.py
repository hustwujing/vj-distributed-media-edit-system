"""Harness process ownership for the sandbox Runtime Daemon."""
from __future__ import annotations

import collections
import json
import os
import signal
import subprocess
import threading
import time


class HarnessStateError(RuntimeError):
    pass


class StdioHarnessBridge:
    """Bounded JSON-RPC transport to one Harness process group."""
    def __init__(self,adapter,command,*,environment=None,timeout=30,process_factory=subprocess.Popen):
        self.adapter=adapter;self.command=list(command);self.environment=environment or {}
        self.timeout=timeout;self.process_factory=process_factory;self.process=None
        self.sequence=0;self.responses={};self.condition=threading.Condition()
        self.ended=False;self.on_event=None;self.diagnostics=collections.deque(maxlen=30)

    def start(self,on_event):
        if self.process is not None:raise HarnessStateError('Harness process is already started')
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8',**self.environment}
        self.process=self.process_factory(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,bufsize=1,start_new_session=True,env=env)
        self.on_event=on_event
        threading.Thread(target=self._read,daemon=True).start()
        threading.Thread(target=self._read_errors,daemon=True).start()

    def _read(self):
        try:
            for line in iter(lambda:self.process.stdout.readline(8*1024**2+1),''):
                if len(line.encode())>8*1024**2:raise ValueError('Harness message exceeds limit')
                try:message=json.loads(line)
                except ValueError:continue
                if not isinstance(message,dict):continue
                if 'id' in message and 'method' not in message:
                    with self.condition:
                        self.responses[message['id']]=message;self.condition.notify_all()
                elif 'id' in message:
                    self.adapter.respond(self.write,message)
                else:
                    for event in self.adapter.events(message):self.on_event(event)
        except Exception as exc:
            self.diagnostics.append(type(exc).__name__+': '+str(exc)[:1000])
        finally:
            with self.condition:self.ended=True;self.condition.notify_all()

    def _read_errors(self):
        for line in iter(lambda:self.process.stderr.readline(4096),''):
            self.diagnostics.append(line[:1500])

    def write(self,message):
        if self.process is None or self.ended:raise HarnessStateError('Harness process is not running')
        encoded=json.dumps(message,ensure_ascii=False,separators=(',',':'))
        if len(encoded.encode())>8*1024**2:raise ValueError('Harness control message exceeds limit')
        with self.condition:
            self.process.stdin.write(encoded+'\n');self.process.stdin.flush()

    def rpc(self,method,params):
        with self.condition:self.sequence+=1;identity=self.sequence
        self.write({'id':identity,'method':method,'params':params})
        deadline=time.monotonic()+self.timeout
        with self.condition:
            while identity not in self.responses and not self.ended:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('Harness RPC timed out')
                self.condition.wait(remaining)
            if identity not in self.responses:
                detail=''.join(self.diagnostics)[-1200:]
                raise HarnessStateError('Harness process ended before response'+(': '+detail if detail else ''))
            response=self.responses.pop(identity)
        if 'error' in response:raise HarnessStateError(str(response['error'].get('message','Harness RPC failed'))[:1600])
        return response.get('result',{})

    def close(self):
        process,self.process=self.process,None
        if process is None:return
        if process.poll() is None:
            try:
                os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=5)
            except (ProcessLookupError,subprocess.TimeoutExpired):
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=5)
        with self.condition:self.ended=True;self.condition.notify_all()
        for stream in (process.stdin,process.stdout,process.stderr):
            if stream:stream.close()


class HarnessController:
    """Map normalized Runtime RPCs onto one frozen HarnessAdapter."""
    def __init__(self,adapter,profile,bridge,publish,expected,exporter=None):
        self.adapter=adapter;self.profile=profile;self.bridge=bridge;self.publish=publish
        self.expected=dict(expected);self.state='created';self.session_id=None
        self.lock=threading.RLock()
        self.exporter=exporter

    def event(self,event):
        with self.lock:
            if event.kind=='turn.ended' and self.state!='closed':self.state='session'
        self.publish(event)

    def dispatch(self,method,params):
        if method=='runtime.initialize':
            if params!=self.expected:raise HarnessStateError('Runtime profile does not match frozen configuration')
            if self.state=='created':
                self.bridge.start(self.event)
                try:self.adapter.initialize(self.bridge.rpc,self.bridge.write)
                except Exception:
                    self.bridge.close();raise
                self.state='initialized'
            return {'state':self.state,'harness':self.adapter.id,'adapter_version':self.adapter.version}
        if self.state=='created':raise HarnessStateError('Runtime is not initialized')
        if method=='session.open':
            if self.state not in ('initialized','session'):raise HarnessStateError('Harness session is busy')
            instructions=params.get('instructions','');session_id=params.get('session_id')
            if not isinstance(instructions,str) or len(instructions.encode())>256*1024:
                raise ValueError('Harness instructions exceed limit')
            if self.session_id and session_id!=self.session_id:
                raise HarnessStateError('Harness session is already open')
            if not self.session_id:
                self.session_id=self.adapter.open_session(
                    self.bridge.rpc,self.profile,instructions,session_id)
            self.state='session';return {'session_id':self.session_id}
        if not self.session_id:raise HarnessStateError('Harness session is not open')
        if method=='turn.start':
            if self.state!='session':raise HarnessStateError('Harness turn is already running')
            if params.get('session_id')!=self.session_id:raise HarnessStateError('Harness session does not match')
            text=params.get('text','')
            if not isinstance(text,str) or len(text.encode())>1024*1024:raise ValueError('Harness turn input exceeds limit')
            self.state='running'
            try:return self.adapter.run_turn(self.bridge.rpc,self.session_id,text,self.profile)
            except Exception:self.state='session';raise
        if method=='turn.interrupt':
            if self.state!='running':return {'status':'not_running'}
            return self.adapter.interrupt(self.bridge.rpc,self.session_id)
        if method=='session.export':
            if self.state!='session':raise HarnessStateError('Cannot export a running Harness session')
            return {'session_id':self.session_id,'native_state':'owned_by_harness',
                    'manifest_ref':self.exporter.export_descriptor() if self.exporter else None}
        raise HarnessStateError('Unsupported Runtime method')

    def close(self):
        self.bridge.close()
        with self.lock:self.state='closed'
