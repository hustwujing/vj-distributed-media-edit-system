"""Per-turn Unix-socket gateway. Provider secrets never enter the agent sandbox."""
import json
import os
import socketserver
import sys
import threading
import tempfile
import time
import subprocess
import signal
import base64
import contextlib
import uuid
import struct
import socket
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

import requests
from .paths import SANDBOX_WORKSPACE
from .task_leases import CURRENT, LeaseLost,TaskQuotaReached,model_call_limit,quota_status,quota_error


from .live_activity import observed

class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self,*args,**kwargs):
        self.connections=threading.BoundedSemaphore(16)
        super().__init__(*args,**kwargs)

    def process_request(self,request,client_address):
        request.settimeout(15)
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:super().process_request(request,client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.connections.release()


class Gateway:
    lease = None
    broker = None
    dynamic_task = False

    def __init__(self, socket_path, project_dir, assets, mode, model, base_url, key, uid, gid, notify, extract_audio, workspace=None,canvas_callback=None,workflow_callback=None,listener_fd=None,broker_client=None,profile=None):
        self.socket_path = Path(socket_path)
        self.project_dir = Path(project_dir)
        self.assets = {a['id']: a for a in assets}
        self.mode, self.model = mode, model
        self.uid, self.gid = uid, gid
        self.lease = CURRENT.get()
        from .agent_profiles import current
        from .model_adapters import get as model_adapter
        self.profile=profile or current()
        self.model_adapter=model_adapter(self.profile.protocol)
        self.broker=broker_client
        self.gateway_process=None
        self.allowed_paths=None
        self.dynamic_task=False
        if self.lease and self.lease.kind=='workflow':
            from .workflow_graph import get as graph_get
            if graph_get(self.lease.store,self.lease.run_id):
                from .workflow_gateway import allowed
                self.allowed_paths=allowed(self.lease);self.dynamic_task=True
        if self.lease and self.lease.kind=='manual':
            job=self.lease.store.job(self.lease.run_id)
            self.allowed_paths=({'/transcribe'} if job['kind']=='transcribe' else
                                {'/image-tools','/image-edit'} if job['kind']=='image' else set())
        self.request_slots = threading.BoundedSemaphore(8)
        self.base_url, self.key = base_url.rstrip('/'), key
        self.notify = notify
        self.extract_audio = extract_audio
        self.workspace = Path(workspace) if workspace else self.project_dir/'agent'/'workspace'
        self.canvas_callback=canvas_callback
        self.workflow_callback=workflow_callback
        self.image_cancel=threading.Event()
        self.image_lock=threading.Lock()
        self.image_calls=0
        self.local_image_calls=0
        self.calls = 0
        self.transcribe_lock = threading.Lock()
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def reply(self, status, payload):
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.reply(404, {'error':'Unsupported gateway endpoint'})

            def do_POST(self):
                if not outer.request_slots.acquire(blocking=False):
                    self.reply(429, {'error':'Too many simultaneous tool requests'})
                    self.close_connection = True
                    return
                try:
                    self.connection.settimeout(15)
                    with outer.lease.bind() if outer.lease else contextlib.nullcontext():
                        outer.check()
                        self.dispatch()
                except LeaseLost:
                    self.reply(409, {'error':'Task execution ownership expired'})
                finally:
                    outer.request_slots.release()

            def dispatch(self):
                path=None
                try:
                    try:size = int(self.headers.get('Content-Length', '0'))
                    except ValueError:
                        self.reply(400,{'error':{'code':'invalid_content_length','message':'Invalid request length'}});return
                    if size <= 0 or size > 32 * 1024 * 1024:
                        self.reply(413, {'error':'Invalid request size'})
                        return
                    try:body = json.loads(self.rfile.read(size))
                    except (json.JSONDecodeError,UnicodeDecodeError):
                        self.reply(400,{'error':{'code':'invalid_json','message':'Invalid JSON request'}});return
                    if not isinstance(body, dict):
                        self.reply(422,{'error':{'code':'invalid_request','message':'Expected a JSON object'}});return
                    outer.check()
                    path = urlsplit(self.path).path
                    if outer.allowed_paths is not None and path not in outer.allowed_paths:
                        self.reply(403,{'error':'This operation is not authorized for this task'})
                        return
                    if path == '/context':
                        if not outer.lease or outer.lease.kind != 'agent':
                            self.reply(403, {'error':'Public context requires an active Agent task'})
                        else:
                            from .harness_context import lookup
                            row=outer.lease.store.query('SELECT project FROM agent_turns WHERE id=?',(outer.lease.run_id,))[0]
                            result=lookup(outer.lease.store,row['project'],body)
                            outer.check()
                            self.reply(200,result)
                    elif path in ('/workflow/proposal','/workflow/agent-result'):
                        from .workflow_gateway import submit
                        self.reply(200,submit(outer.lease,'proposal' if path.endswith('/proposal') else 'result',body))
                    elif path=='/workflow/inspect':
                        if outer.broker:self.reply(200,outer.broker.request('workflow_inspect',body))
                        else:
                            from .workflow_gateway import inspect
                            self.reply(200,inspect(outer,body))
                    elif path == '/transcribe':
                        outer.transcribe(self, body)
                    elif path in ('/image-edit','/image-tools'):
                        outer.image_edit(self, body)
                    elif path == '/canvas-edit':
                        outer.canvas_edit(self,body)
                    elif path == '/workflow' and outer.workflow_callback:
                        if not isinstance(body, dict) or body.get('action') not in ('submit', 'status','validate','decision','capabilities','intent','preflight'):
                            self.reply(422, {'error':'Invalid workflow action'})
                        elif body['action'] == 'submit' and outer.mode != 'execute':
                            self.reply(403, {'error':'请先确认方案'})
                        else:
                            try:self.reply(200, outer.workflow_callback(body))
                            except TaskQuotaReached:raise
                            except ValueError as exc:self.reply(422, {'error':str(exc)[:8000]})
                    elif path in ('/responses', '/v1/responses'):
                        if outer.model_adapter.protocol!='responses':self.reply(400,{'error':'Model protocol does not match frozen task configuration'})
                        else:outer.forward(self, body)
                    elif path in ('/messages','/v1/messages','/chat/completions','/v1/chat/completions'):
                        expected='anthropic' if path.endswith('/messages') else 'chat_completions'
                        if outer.model_adapter.protocol!=expected:self.reply(400,{'error':'Model protocol does not match frozen task configuration'})
                        else:outer.forward(self, body)
                    else:
                        self.reply(404, {'error':'Unsupported gateway endpoint'})
                except TaskQuotaReached as exc:
                    from .task_leases import record_quota
                    if outer.lease:record_quota(outer.lease.store,outer.lease.run_id,exc)
                    from .telemetry import emit
                    emit('gateway.quota_exhausted','WARNING',exc=exc,operation=exc.diagnostic.get('category'),calls=exc.diagnostic.get('used'))
                    self.reply(403,{'error':{'message':str(exc),**exc.diagnostic}})
                except (ValueError, TypeError) as exc:
                    from .telemetry import emit
                    emit('gateway.validation_failed','WARNING',exc=exc)
                    self.reply(422,{'error':{'code':'request_validation_failed','message':'请求参数或处理校验未通过，请检查任务诊断。'}})
                except (requests.ConnectionError,requests.Timeout) as exc:
                    from .telemetry import emit
                    if path in ('/responses','/v1/responses','/messages','/v1/messages','/chat/completions','/v1/chat/completions'):outer.note_model_unavailable()
                    emit('gateway.transport_failed','ERROR',exc=exc)
                    self.reply(502,{'error':{'code':'provider_transport_failed','message':'服务连接暂时不可用。'}})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except PermissionError as exc:
                    from .telemetry import emit
                    from .gateway_process import FILE_PERMISSION_ERROR
                    emit('gateway.filesystem_permission_failed','ERROR',exc=exc)
                    self.reply(500,{'error':FILE_PERMISSION_ERROR})
                except Exception as exc:
                    from .telemetry import emit
                    emit('gateway.request_failed','ERROR',exc=exc)
                    # Never return upstream headers or credentials in error messages.
                    try:
                        self.reply(502, {'error':'Gateway request failed; retry or ask the user to inspect the service.'})
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        if listener_fd is None:
            self.socket_path.unlink(missing_ok=True)
            self.server = UnixHTTPServer(str(self.socket_path), Handler)
            os.chown(self.socket_path, uid, gid)
            self.socket_path.chmod(0o600)
        else:
            self.server=UnixHTTPServer(str(self.socket_path),Handler,bind_and_activate=False)
            self.server.socket.close();self.server.socket=socket.socket(fileno=listener_fd)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        if self.lease and os.geteuid()==0:
            from .gateway_process import GatewayProcess
            self.gateway_process=GatewayProcess(self);self.gateway_process.start()
        else:self.thread.start()

    def check(self):
        if self.lease:
            self.lease.check()
            table = 'jobs' if self.lease.kind == 'manual' else 'agent_turns'
            row = self.lease.store.query(f'SELECT status FROM {table} WHERE id=?', (self.lease.run_id,))[0]
            if row['status'] != 'running' and not (self.dynamic_task and row['status']=='waiting'):raise LeaseLost('Task is no longer running')

    def canvas_edit(self,handler,body):
        from fastapi import HTTPException
        if not self.canvas_callback:
            handler.reply(403,{'error':'当前对话没有绑定画布图层'});return
        dest=None;committed=False
        try:
            if not isinstance(body,dict):raise HTTPException(422,'参数应为 JSON 对象')
            if body.get('apply') and self.mode!='execute':raise HTTPException(403,'确认执行后才能应用画布修改')
            new_asset=None
            if body.get('image'):
                if self.mode!='execute' or body.get('apply') is not True:raise HTTPException(403,'确认执行后才能替换图片')
                raw=base64.b64decode(body['image'],validate=True)
                if len(raw)>20*1024**2:raise HTTPException(422,'图片不能超过 20 MB')
                repo=Path(__file__).resolve().parents[1]
                if self.broker:
                    decoded=self.broker.request('decode',{'image':body['image']})
                    result=subprocess.CompletedProcess([],0,base64.b64decode(decoded['image'],validate=True))
                elif self.lease and self.lease.runtime:
                    from .agent_sandbox import command as sandbox_command
                    command = sandbox_command(self.lease.runtime.env, None,
                                              ['/opt/venv/bin/python', '/opt/canvas_pixels.py'])
                else:
                    command=[str(repo/'.venv/bin/python'),'-m','webapp.canvas_pixels']
                    if os.geteuid()==0:command=['/usr/bin/setpriv','--reuid='+str(self.uid),'--regid='+str(self.gid),'--clear-groups','--no-new-privs','--bounding-set=-all',*command]
                # Decode bytes without privileges; no generated path is ever opened by root.
                if not self.broker:
                    temporary=self.lease.runtime.path/'data' if self.lease and self.lease.runtime else None
                    with tempfile.TemporaryFile(dir=temporary) as decoded, tempfile.TemporaryFile(dir=temporary) as errors:
                        result=subprocess.run(command,input=raw,stdout=decoded,stderr=errors,timeout=30,cwd=repo,
                            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(repo)})
                        decoded.seek(0);result.stdout=decoded.read(128*1024**2)
                if result.returncode or result.stdout[:8]!=b'\x89PNG\r\n\x1a\n':raise HTTPException(422,'无法读取图片，请使用有效的 PNG/JPG/WebP')
                width,height=struct.unpack('>II',result.stdout[16:24])
                asset_id=uuid.uuid4().hex;filename=asset_id+'.png';dest=self.project_dir/'sources'/filename
                with dest.open('xb') as f:f.write(result.stdout)
                if self.lease and self.lease.runtime:
                    os.chown(dest,self.lease.runtime.service_uid,self.lease.runtime.service_gid)
                elif not self.broker:os.chown(dest,self.uid,self.gid)
                dest.chmod(0o600)
                new_asset={'id':asset_id,'filename':filename,'name':'Agent 图层结果.png',
                    'metadata':{'kind':'image','mime':'image/png','width':width,'height':height,'has_alpha':True,'size':len(result.stdout)}}
            result=self.canvas_callback(body,new_asset)
            committed=result['applied']
            if committed:self.notify('progress',{'text':f'选中图层已更新，画布已保存为版本 {result["after"]["revision"]}'})
            handler.reply(200,result)
        except HTTPException as exc:handler.reply(exc.status_code,{'error':exc.detail})
        except (ValueError,TypeError,subprocess.SubprocessError):handler.reply(422,{'error':'图层参数或图片数据无效'})
        finally:
            if dest and not committed:dest.unlink(missing_ok=True)

    def close(self):
        self.image_cancel.set()
        with self.image_lock:pass
        if self.gateway_process:self.gateway_process.close()
        else:self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():self.thread.join(timeout=3)
        self.socket_path.unlink(missing_ok=True)

    def forward(self, handler, body):
        self.check()
        role=None;dynamic=None
        if self.dynamic_task:
            from .workflow_gateway import context
            dynamic,_,role=context(self.lease)
        from .egress_policy import model_input,provider_base,session,assert_no_credentials,denial
        try:
            output_limit=min(self.profile.budget['max_output_tokens'],role.max_output_tokens) if role else self.profile.budget['max_output_tokens']
            body=self.model_adapter.prepare(body,self.model,output_limit,self.profile.reasoning_effort)
            provider_base(self.base_url);assert_no_credentials(json.dumps(body),[self.key])
        except ValueError:
            denial(self.lease,'model_input_denied')
            handler.reply(403,{'error':'模型输入中的远程地址或外部能力不符合任务网络策略。'})
            return
        if self.broker and self.model_adapter.protocol=='responses':
            body=self.broker.request('normalize',body)
        elif self.lease and self.lease.runtime and self.model_adapter.protocol=='responses':
            self.normalize_model_images(body)
        if self.model_adapter.protocol!='responses':self.normalize_native_images(body)
        with self.lock:
            if self.calls>=(min(self.profile.budget['run_models'],role.max_turns) if role else self.profile.budget['run_models']):
                if self.lease:
                    status=quota_status(self.lease.store,self.lease.run_id,'model',model_call_limit())
                    if status['blocked']:raise quota_error(status)
                raise quota_error({'category':'model','scope':'turn','used':self.calls,'limit':self.profile.budget['run_models']})
            from .planning_budget import check as check_plan
            check_plan(self.lease)
            from .planning_budget import reserve_model
            reserve_model(self.lease)
            if self.lease:self.lease.consume('model',model_call_limit())
            self.calls += 1
        # The sandbox may only call the administrator-selected model and API path.
        body['model'] = self.model
        from .telemetry import emit
        emit('model.request_configuration',model=self.model,harness=self.profile.harness,
             protocol=self.profile.protocol,reasoning_effort=self.profile.reasoning_effort,
             max_output_tokens=body.get('max_output_tokens',body.get('max_tokens')))
        from .task_usage import model_call
        from .workflow_agents import model_budget
        observer=self.model_adapter.receipt()
        budget=model_budget(self.lease,dynamic,len(json.dumps(body).encode())+output_limit,observer) if dynamic else contextlib.nullcontext()
        with budget, model_call(self.lease,self.model,actor='sandbox_agent',observer=observer) as receipt, session() as client, client.post(self.base_url + self.model_adapter.path, json=body,
                           headers=self.model_adapter.headers(self.key),
                           stream=True, timeout=(15, 240),allow_redirects=False) as response:
            if response.status_code != 200:
                receipt.status='failed'
                if response.status_code in (401,403,404,408,429) or response.status_code>=500:
                    self.note_model_unavailable()
                handler.reply(response.status_code, {'error':{'message':f'Model provider HTTP {response.status_code}',
                                                             'type':'provider_error','code':'model_provider_http_error','http_status':response.status_code}})
                return
            handler.send_response(200)
            handler.send_header('Content-Type', response.headers.get('Content-Type', 'text/event-stream'))
            handler.send_header('Connection', 'close')
            handler.end_headers()
            handler.close_connection = True
            from .egress_policy import secrets
            from .safe_stream import SafeEvents,chunks
            guard=SafeEvents((*secrets(),self.key))
            for chunk in chunks(response):
                if chunk:
                    receipt.feed(chunk)
                    self.check()
                    for safe in guard.feed(chunk):
                        handler.wfile.write(safe);handler.wfile.flush()
            for safe in guard.finish():handler.wfile.write(safe);handler.wfile.flush()

    def normalize_native_images(self,body):
        """Normalize native inline media through the same isolated decoder."""
        slots=[]
        def visit(value):
            if isinstance(value,list):
                for child in value:visit(child)
            elif isinstance(value,dict):
                if value.get('type')=='image':
                    source=value['source'];slots.append((value,'anthropic',{'type':'input_image','image_url':'data:'+source['media_type']+';base64,'+source['data']}))
                elif value.get('type')=='image_url':
                    url=value['image_url'];slots.append((value,'chat',{'type':'input_image','image_url':url['url'] if isinstance(url,dict) else url}))
                else:
                    for child in value.values():visit(child)
        visit(body)
        if not slots:return
        canonical={'input':[slot[2] for slot in slots]}
        if self.broker:canonical=self.broker.request('normalize',canonical)
        elif self.lease and self.lease.runtime:self.normalize_model_images(canonical)
        for (target,protocol,_),image in zip(slots,canonical['input']):
            url=image['image_url']
            if protocol=='anthropic':
                mime,encoded=url.split(';base64,',1);target['source']={'type':'base64','media_type':mime[5:],'data':encoded}
            else:target['image_url']={'url':url}

    def note_model_unavailable(self):
        if not self.lease:return
        from .task_lifecycle import TaskLimitReached
        try:
            self.lease.check()
            self.lease.store.execute("INSERT INTO execution_profile_health VALUES (?,?,'unavailable',?,?) ON CONFLICT(worker,digest) DO UPDATE SET state='unavailable',next_retry=excluded.next_retry,updated=excluded.updated",
                (self.lease.worker,self.profile.digest,self.lease.store.now()+30,self.lease.store.now()))
        except (LeaseLost,TaskLimitReached):pass
        except Exception as exc:
            from .telemetry import emit
            emit('gateway.health_update_failed','WARNING',exc=exc)

    def normalize_model_images(self,body):
        from .agent_sandbox import command
        nodes=[body]
        while nodes:
            value=nodes.pop()
            if isinstance(value,list):nodes.extend(value)
            elif isinstance(value,dict):
                if value.get('type')=='input_image':
                    raw=base64.b64decode(value['image_url'].split(',',1)[1],validate=True)
                    runtime=self.lease.runtime
                    with tempfile.TemporaryFile(dir=runtime.path/'data') as output,tempfile.TemporaryFile(dir=runtime.path/'data') as errors:
                        result=subprocess.run(command(runtime.env,None,
                            ['/opt/venv/bin/python','/opt/canvas_pixels.py','--model']),input=raw,
                            stdout=output,stderr=errors,timeout=30)
                        if result.returncode:raise ValueError('模型图片未通过安全解码')
                        output.seek(0);normalized=output.read(20*1024**2+1)
                        if len(normalized)>20*1024**2:raise ValueError('模型图片过大')
                        value['image_url']='data:image/png;base64,'+base64.b64encode(normalized).decode()
                else:nodes.extend(value.values())
        if len(json.dumps(body).encode())>32*1024**2:raise ValueError('模型输入总大小超限')

    def transcribe(self, handler, body):
        asset = self.assets.get(body.get('asset'))
        if not asset or asset['metadata'].get('kind') not in ('video','audio'):
            handler.reply(404, {'error':'Only assets from this project can be transcribed.'})
            return
        if self.mode != 'execute':
            handler.reply(403, {'error':'当前是方案讨论阶段；请提出转写方案，由用户确认执行后调用。'})
            return
        if asset['metadata'].get('duration',0)>int(os.environ.get('VIDEO_USE_MAX_TRANSCRIBE_SECONDS','7200')):
            handler.reply(422,{'error':'音频超过单次转写时长限制，请先分段。'})
            return
        track = body.get('audio_track', 0)
        language = body.get('language')
        speakers = body.get('speakers')
        if not isinstance(track, int) or track < 0 or track >= len(asset['metadata'].get('audio_tracks', [])):
            handler.reply(422, {'error':'Invalid audio track'})
            return
        if language is not None and (not isinstance(language, str) or len(language)>12):
            handler.reply(422, {'error':'Invalid language'})
            return
        if speakers is not None and (not isinstance(speakers, int) or not 1 <= speakers <= 32):
            handler.reply(422, {'error':'Invalid speaker count'})
            return
        from .execution_intent import enforce_direct
        try:enforce_direct(getattr(self,'lease',None),'audio.transcribe')
        except ValueError as exc:
            handler.reply(422, {'error':str(exc)})
            return
        with self.transcribe_lock:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helpers'))
            from transcribe import call_scribe, peak_dbfs, transcript_path
            self.notify('progress', {'text':'正在转写当前项目素材：'+asset['name']})
            key = os.environ.get('ELEVENLABS_API_KEY', '')
            if not key:
                handler.reply(503, {'error':'ElevenLabs is not configured'})
                return
            source = self.project_dir/'sources'/asset['filename']
            path = transcript_path(self.project_dir/'edit', source, track)
            if not path.exists():
                if self.lease:self.lease.consume('transcribe',50)
                # The native media decoder runs as the unprivileged sandbox user.
                # The service broker uploads the bounded normalized audio.
                # Never fall back to the host's root-backed /tmp. A leased call
                # owns its runtime data directory; a direct project call owns
                # only the transcript directory containing the target file.
                temporary=self.lease.runtime.path/'data' if self.lease and self.lease.runtime else path.parent
                with tempfile.TemporaryDirectory(dir=temporary) as directory:
                    audio = Path(directory)/'audio.wav'
                    self.extract_audio(asset['filename'], track, audio)
                    import wave
                    if audio.stat().st_size>256*1024**2:raise ValueError('Extracted audio is too large')
                    with wave.open(str(audio),'rb') as wav:
                        if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate())!=(1,2,16000):
                            raise ValueError('Invalid extracted audio format')
                    if peak_dbfs(audio) < -60:
                        handler.reply(422, {'error':'所选音轨几乎无声，请选择其他音轨。'})
                        return
                    from .provider_calls import intent, update, request_time
                    call_workflow={'id':self.lease.run_id,'run_id':self.lease.run_id,'origin':'direct','manual':self.lease.kind=='manual'} if self.lease else None
                    call_step={'id':'direct-transcribe-'+str(body['asset'])+'-'+str(track),'revision':0,'attempts':0,
                               'bindings':{'asset':asset['id']},'spec':{'parameters':{
                                   'audio_track':track,'language':language,'speakers':speakers}}}
                    if call_workflow:
                        intent(self.lease.store,call_workflow,call_step,'https://api.elevenlabs.io','audio.transcribe')
                    with request_time(self.lease.store,call_workflow,call_step,'submit') if call_workflow else contextlib.nullcontext():
                        payload = call_scribe(audio, key, language, speakers)
                    if call_workflow:
                        update(self.lease.store,call_workflow,call_step,'succeeded',record={'usage':payload.get('usage')})
                temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
                try:
                    temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2));temporary.chmod(0o640)
                    with self.lease.store.connect() if self.lease else contextlib.nullcontext():
                        self.check()
                        temporary.replace(path)
                finally:temporary.unlink(missing_ok=True)
            if self.broker:
                # Restored caches belong to the executor, not this unprivileged
                # process. Only the lease-checked broker manages their mode.
                self.broker.request('refresh_transcript',{'asset':asset['id'],'track':track})
            else:
                os.chmod(path, 0o640)
                if self.lease and self.lease.store.shared_files:
                    self.lease.store.artifacts.save_file(self.project_dir.name,'edit/transcripts/'+path.name)
            if self.lease and self.lease.runtime:
                runtime = self.lease.runtime
                os.chown(path,runtime.service_uid,runtime.service_gid)
                target = runtime.transcripts/path.name
                if not target.exists():
                    with target.open('x') as f:f.write(path.read_text())
                    os.chown(target,self.uid,self.gid);target.chmod(0o600)
            handler.reply(200, {'transcript':json.loads(path.read_text()),
                               'path':SANDBOX_WORKSPACE+'/transcripts/'+path.name})

    @observed('host','operation','处理图片工具请求')
    def image_edit(self,handler,body):
        if self.broker:return self.broker.relay('image',body,handler)
        from .image_document import ImageTask,check_task
        if self.mode!='execute':
            handler.reply(403,{'error':'请先确认图片处理方案，再执行模型任务。'});return
        try:
            if not isinstance(body,dict):raise ValueError('请求必须为对象')
            if 'target_asset' in body:
                task=ImageTask(action='generate',asset=body.get('target_asset'),references=body.get('reference_assets',[]),prompt=body.get('prompt',''),quality=body.get('quality','fast'))
            else:task=ImageTask.model_validate(body)
            if task.action=='export':raise ValueError('画布导出请使用图片工作台，或在沙箱内编写 Python')
            check_task(task,self.assets)
            from .execution_intent import enforce_direct
            from .operations import for_image
            if task.action!='export':enforce_direct(getattr(self,'lease',None),for_image(task.action).id)
            if task.action in ('generate','decompose') and self.workflow_callback and self.workflow_callback({'action':'status'}):
                raise ValueError('本轮生成已由后台计划管理，请读取已有结果；新增生成需求需另行确认方案')
        except (ValueError,TypeError) as exc:
            from .telemetry import emit
            emit('image.validation_failed','ERROR',exc=exc)
            handler.reply(422,{'error':str(exc)[:600]});return
        if not self.image_lock.acquire(blocking=False):
            handler.reply(409,{'error':'本轮已有图片任务执行中'});return
        process=None
        try:
            self.count_image_call(task.action)
            repo=Path(__file__).resolve().parents[1]
            # This host-owned task directory is never mounted into the Agent.
            runtime=self.lease.runtime if self.lease else None
            provider_root=runtime.path/'data'/'image-tools' if runtime else self.project_dir/'edit'/'image_provider'
            provider_root.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='task-',dir=provider_root) as tmp,contextlib.ExitStack() as stack:
                work=Path(tmp);os.chown(work,self.uid,self.gid);work.chmod(0o700)
                output=work/'output';output.mkdir();os.chown(output,self.uid,self.gid)
                request=work/'request.json';request.write_text(json.dumps({'task':task.model_dump(),'assets':list(self.assets.values()),'project':SANDBOX_WORKSPACE if runtime else str(self.project_dir),'output':'/tool/output' if runtime else str(output)}));os.chown(request,self.uid,self.gid);request.chmod(0o600)
                if runtime:
                    from .provider_relay import ImageRelay
                    from .image_sandbox import image_command, gpu_slot
                    from .paths import temporary_socket
                    relay=ImageRelay(temporary_socket(runtime.path),task,self.lease,self.uid,self.gid)
                    stack.callback(relay.close)
                    scope=runtime.scope()
                    gpu=stack.enter_context(gpu_slot(task.action,self.lease,self.image_cancel,scope))
                    stack.callback(runtime.close_scope,scope)
                    command=image_command(dict(runtime.env,image_action=task.action),relay.path,work,gpu,scope)
                else:
                    command=[str(repo/'.venv/bin/python'),'-u','-m','webapp.image_tasks_cli',str(request)]
                    if os.geteuid()==0:command=['/usr/bin/setpriv','--reuid='+str(self.uid),'--regid='+str(self.gid),'--clear-groups','--no-new-privs','--bounding-set=-all',*command]
                childenv={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','TMPDIR':str(work),'XDG_CACHE_HOME':str(work/'cache'),'PYTHONPATH':str(repo),'HOME':str(work),'OPENBLAS_NUM_THREADS':'4'}
                with (work/'worker.log').open('w') as log:
                    process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=repo,env=childenv,start_new_session=True)
                    if runtime:scope['stopped']=False
                    deadline=time.monotonic()+780;last=''
                    while process.poll() is None:
                        self.check()
                        if self.image_cancel.wait(.5) or time.monotonic()>deadline:raise RuntimeError('图片任务已取消或超时')
                        with (work/'worker.log').open('rb') as tail:
                            tail.seek(max(0,os.fstat(tail.fileno()).st_size-65536))
                            lines=tail.read(65536).decode(errors='replace').splitlines()
                        if lines and lines[-1]!=last:
                            last=lines[-1];self.notify('progress',{'text':last[-350:]})
                    from .telemetry import process_exit,child_failure
                    process_exit(process,scope['paths'] if runtime else None)
                    if process.returncode:
                        child_failure(work/'worker.log')
                        raise RuntimeError('图片处理失败，请查看工具日志或缩小任务范围')
                names=json.loads((output/'provider-result.json').read_text())
                files=[];total=0
                for name in names:
                    if Path(name).name!=name or Path(name).suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.json','.psd','.ora'}:continue
                    path=output/name
                    if path.is_symlink() or not path.is_file():continue
                    total+=path.stat().st_size
                    if total>64*1024**2:raise RuntimeError('结果超过单轮传输限制，请减小分辨率')
                    files.append({'name':name,'data':base64.b64encode(path.read_bytes()).decode()})
                handler.reply(200,{'files':files})
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
            from .telemetry import emit
            emit('image.execution_failed','ERROR',exc=exc)
            handler.reply(502,{'error':str(exc)[:600]})
        finally:
            if process and process.poll() is None:
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
                    process.wait()
            self.image_lock.release()

    def count_image_call(self,action):
        self.check()
        if self.lease:
            self.lease.consume('image' if action in {'generate','decompose','segment','segment_sam3','refine_zim','remove_bg','erase','local_edit','ocr'} else 'local_image',
                               4 if action in {'generate','decompose','segment','segment_sam3','refine_zim','remove_bg','erase','local_edit','ocr'} else 20)
        if action in {'generate','decompose','segment','segment_sam3','refine_zim','remove_bg','erase','local_edit','ocr'}:
            self.image_calls+=1
            if self.image_calls>4:raise RuntimeError('每轮最多执行4次图片模型任务')
        else:
            self.local_image_calls+=1
            if self.local_image_calls>20:raise RuntimeError('每轮最多执行20次本地图片任务')
