"""Per-tool, fixed-destination image relay. No general network proxy.

Only the trusted image worker receives this socket. Requests are scoped to one
validated task and one upstream job; arbitrary URLs and job IDs are rejected.
"""
import contextlib
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler
import json
import os
import re
import socketserver
import threading
import uuid
import socket
import subprocess
import sys
import pwd
from pathlib import Path

import requests
from .egress_policy import session as provider_session,provider_base

from .image_providers import ENDPOINTS


class ImageRelay:
    def __init__(self, path, task, lease, uid, gid,listener_fd=None,call_id=None,base_override=None):
        self.path, self.task, self.lease = path, task, lease
        self.job = None
        self.submitted = False
        self.call = call_id or uuid.uuid4().hex
        self.process=None
        self.lock = threading.Lock()
        self.total = 0
        self.record_w={'id':lease.turn_id,'turn_id':lease.turn_id,'manual':lease.kind=='manual','origin':'direct'}
        self.record_s={'id':'direct-image','revision':0,'attempts':0,'bindings':{'asset':task.asset,'references':task.references}}
        self.base, self.field = ENDPOINTS['decompose' if task.action=='decompose' else 'edit']
        if base_override:self.base=base_override
        self.base=provider_base(self.base)
        if lease.kind=='manual':
            previous=lease.store.query('SELECT * FROM execution_provider_calls WHERE turn_id=?',(lease.turn_id,))
            if previous:
                if len(previous)!=1 or previous[0]['status'] not in ('waiting','succeeded') or not previous[0]['external_id'] or previous[0]['provider']!=self.base:
                    raise RuntimeError('Previous provider submission cannot be safely resumed')
                self.job=previous[0]['external_id']
                self.call=previous[0]['id']
                self.submitted=True
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):pass
            def setup(self):
                super().setup()
                self.connection.settimeout(15)

            def reply(self, status, body, mime='application/json'):
                if not isinstance(body, bytes):body=json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Type', mime)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):self.handle_request('POST')
            def do_GET(self):self.handle_request('GET')

            def handle_request(self, method):
                try:
                    self.connection.settimeout(30)
                    with outer.lease.bind():
                        outer.lease.check()
                        if method=='POST' and self.path=='/api/jobs':
                            length=int(self.headers.get('Content-Length','0'))
                            if not 0<length<=32*1024**2:raise ValueError('Invalid request size')
                            raw=self.rfile.read(length)
                            with outer.lock:
                                if outer.submitted:
                                    self.reply(202 if outer.job else 409, {'id':outer.job} if outer.job else {'error':'Submission outcome unknown'})
                                    return
                                outer.submitted=True
                            outer.lease.consume('image_provider',4)
                            outer.lease.store.execute('INSERT INTO execution_provider_calls VALUES (?,?,?,?,?,?)',
                                (outer.call,outer.lease.turn_id,outer.lease.attempt,'submitting','',outer.base))
                            content_type=self.headers.get('Content-Type','')
                            if '\r' in content_type or '\n' in content_type:raise ValueError('Invalid multipart header')
                            message=BytesParser(policy=default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                            if not message.is_multipart():raise ValueError('Expected multipart')
                            files=[]
                            for part in message.iter_parts():
                                name=part.get_param('name',header='content-disposition')
                                if name in (outer.field,'reference_images') and part.get_filename():
                                    data=part.get_payload(decode=True)
                                    if not data or len(data)>20*1024**2:raise ValueError('Invalid image')
                                    files.append((name,('image.png',data,'image/png')))
                            if sum(name==outer.field for name,_ in files)!=1 or len(files)>3:
                                raise ValueError('Invalid image inputs')
                            task=outer.task
                            if task.action=='decompose':
                                params={'prompt':task.prompt,'layer_count':str(task.layer_count),'resolution':'640','quality':'balanced','seed':'777','reconstruction_guard':'true'}
                            else:
                                from .image_document import edit_prompt
                                prompt=edit_prompt(task.action,task.prompt,selected=bool(task.action in ('erase','local_edit') and (task.mask or task.box)))
                                params={'prompt':prompt,'quality':task.quality,'output_megapixels':'0.6','seed':'2511','preserve_details':'true','auto_retry':'false'}
                            from .provider_calls import intent,save_session,update
                            from .operations import for_image
                            intent(outer.lease.store,outer.record_w,outer.record_s,outer.base,for_image(task.action).id)
                            with provider_session() as session:
                                session.trust_env=False
                                response=session.post(outer.base+'/api/jobs',files=files,data=params,
                                    headers={'Idempotency-Key':outer.call},timeout=(10,120),allow_redirects=False)
                                save_session(outer.lease.store,outer.record_w,outer.record_s,session)
                            if response.status_code in (400,401,403,404,422):
                                update(outer.lease.store,outer.record_w,outer.record_s,'rejected')
                                outer.lease.store.execute("UPDATE execution_provider_calls SET status='failed' WHERE id=?",(outer.call,))
                            if response.status_code!=202:raise RuntimeError('Provider did not acknowledge submission')
                            job=response.json()['id']
                            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',job):raise ValueError('Invalid job ID')
                            outer.lease.check()
                            outer.lease.store.execute("UPDATE execution_provider_calls SET status='waiting',external_id=? WHERE id=?",(job,outer.call))
                            update(outer.lease.store,outer.record_w,outer.record_s,'waiting',job)
                            outer.job=job
                            self.reply(202,{'id':job})
                            return
                        expected='/api/jobs/'+str(outer.job)
                        allowed=bool(outer.job) and ((method=='GET' and self.path==expected)
                            or (method=='POST' and self.path==expected+'/cancel')
                            or (method=='GET' and re.fullmatch(re.escape(expected)+r'/artifacts/[A-Za-z0-9_-]{1,100}',self.path)))
                        if not allowed:
                            self.reply(403,{'error':'Endpoint or job is outside this task'})
                            return
                        with provider_session() as session:
                            from .provider_calls import restore_session,update
                            restore_session(outer.lease.store,outer.record_w,outer.record_s,session)
                            session.trust_env=False
                            with session.request(method,outer.base+self.path,timeout=(10,60),allow_redirects=False,stream=True) as response:
                                data=bytearray()
                                for chunk in response.iter_content(1024*1024):
                                    outer.lease.check()
                                    data.extend(chunk)
                                    if len(data)>64*1024**2:raise ValueError('Provider response too large')
                                if '/artifacts/' in self.path:
                                    outer.total+=len(data)
                                    if outer.total>128*1024**2:raise ValueError('Provider output quota exceeded')
                                elif method=='GET' and self.path==expected:
                                    record=json.loads(data)
                                    update(outer.lease.store,outer.record_w,outer.record_s,record.get('status','waiting'),record=record)
                                    if record.get('status') in ('succeeded','failed','cancelled'):
                                        outer.lease.store.execute('UPDATE execution_provider_calls SET status=? WHERE id=?',(record['status'],outer.call))
                                self.reply(response.status_code,bytes(data),response.headers.get('Content-Type','application/octet-stream'))
                except Exception:
                    if outer.submitted and not outer.job:
                        with contextlib.suppress(Exception):
                            outer.lease.store.execute("UPDATE workflow_provider_calls SET state='submission_unknown' WHERE request_key=? AND state='submitting'",(outer.lease.turn_id+':direct-image:0',))
                    with contextlib.suppress(OSError):self.reply(502,{'error':'Image tool relay failed; submission will not be repeated automatically'})
        self.server=socketserver.UnixStreamServer(str(path),Handler,bind_and_activate=listener_fd is None)
        if listener_fd is not None:
            self.server.socket.close();self.server.socket=socket.socket(fileno=listener_fd)
        self.server.timeout=1
        if listener_fd is None:os.chown(path,uid,gid);path.chmod(0o600)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        if os.geteuid()==0 and lease.runtime and listener_fd is None:
            from .gateway_process import grant_database,await_ready
            grant_database(lease.store)
            account=pwd.getpwnam('video-use')
            config={'root':str(lease.store.root),'path':str(path),'task':task.model_dump(),'uid':uid,'gid':gid,
                'call':self.call,'base':self.base,'parent':os.getpid(),
                'lease':{key:getattr(lease,key) for key in ('turn_id','worker','attempt','generation','kind','recovered','prior_kind','ttl')}}
            env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1',
                 'VIDEO_USE_INTERNAL_PROVIDER_HOSTS':os.environ.get('VIDEO_USE_INTERNAL_PROVIDER_HOSTS','localhost,127.0.0.1,::1')}
            env.update({k:v for k,v in os.environ.items() if k.startswith('VIDEO_USE_USER_')})
            for name in ('VIDEO_USE_DATABASE_URL','VIDEO_USE_DISTRIBUTED','VIDEO_USE_OBJECT_BUCKET'):
                if name in os.environ:env[name]=os.environ[name]
            from .telemetry import child_env
            env.update(child_env('provider_relay',lease.turn_id,lease.attempt))
            self.process=subprocess.Popen(['/usr/bin/setpriv',f'--reuid={account.pw_uid}',f'--regid={account.pw_gid}',
                '--clear-groups','--no-new-privs','--bounding-set=-all',sys.executable,'-m','webapp.gateway_service','relay',str(self.server.fileno())],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None,env=env,pass_fds=(self.server.fileno(),))
            self.process.stdin.write(json.dumps(config).encode());self.process.stdin.close()
            try:
                start=Path(f'/proc/{self.process.pid}/stat').read_text().rsplit(')',1)[1].split()[19]
                guards=lease.runtime.identity.record.get('guard_processes',[])
                lease.runtime.identity.save(guard_processes=guards+[{'pid':self.process.pid,'start':start}])
            except FileNotFoundError:pass
            await_ready(self.process)
        else:self.thread.start()

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
        else:self.server.shutdown()
        self.server.server_close()
        if os.geteuid()==0:self.path.unlink(missing_ok=True)
