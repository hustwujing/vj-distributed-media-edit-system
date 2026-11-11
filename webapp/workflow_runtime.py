"""Production adapters for durable image jobs and isolated local processing."""
from __future__ import annotations

import base64
import contextlib
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
import stat
import socket
import subprocess
import tempfile
import time
import uuid

import requests

from . import agent_sandbox, agent_store
from .image_providers import ENDPOINTS
from .provider_calls import request_time
from .execution_metrics import measured
from .paths import temporary_socket


class StepFailure(RuntimeError):
    def __init__(self, diagnostic):
        self.diagnostic=diagnostic
        super().__init__(diagnostic.get('message','本地步骤失败'))


def resolve_acceptance_output_roles(acceptance,roles):
    """Bind registered operation output roles to this attempt's actual files."""
    roles=roles or {};result=[]
    for rule in acceptance:
        target=roles.get(rule.get('file'))
        result.append({**rule,'file':str(target)} if target else rule)
    return result


class Adapter:
    def __init__(self, store, model, socket_dir):
        self.store, self.model, self.socket_dir = store, model, Path(socket_dir)

    def environment(self, w):
        from .task_leases import CURRENT
        lease = CURRENT.get()
        if lease and lease.runtime is None:
            lease.runtime = lease.runtime_factory()
        if lease and getattr(lease.runtime,'is_command',False):
            return lease.runtime.environment(self.store,w,self.model)
        if w.get('dynamic'):
            from .execution_contracts import WorkspacePolicy
            return agent_sandbox.prepare(self.store,w['project'],self.model,run_id=w['run_id'],
                                         workspace_policy=WorkspacePolicy.TASK_OUTPUT_ONLY)
        return agent_sandbox.prepare(self.store, w['project'], self.model, 'execute', w['run_id'])

    def session(self):
        from .egress_policy import session
        return session()

    def reuse(self,w,s,cancelled):
        from .workflow_reuse import materialize
        return materialize(self.store,w,s,self.environment(w),cancelled)

    @contextlib.contextmanager
    def input_environment(self,w,s):
        """Materialize declared inputs for a host request, without a sandbox/UID."""
        import shutil
        from .workflow_inputs import digest
        from .artifact_store import logical_path
        temporary=self.store.root/'tmp';temporary.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='provider-inputs-',dir=temporary) as folder:
            workspace=Path(folder);sources=workspace/'sources';sources.mkdir()
            assets=self.store.assets(w['project'])
            by_id={asset['id']:asset for asset in assets}
            for ref in s['spec'].get('inputs',{}).values():
                if ref.get('repair_candidate'):
                    if not w.get('dynamic') or s.get('purpose') not in ('repair','evidence'):raise ValueError('Repair candidate outside scoped task')
                    from .workflow_graph import get as get_run
                    from .repair_inputs import lookup
                    candidate=lookup(self.store,get_run(self.store,w['id']),ref['repair_candidate'])
                    source=self.store.artifacts.materialize(w['project'],candidate['reference']['artifact_id'])
                    target=workspace/candidate['path']
                elif ref.get('asset'):
                    asset=by_id.get(ref['asset'])
                    if not asset:raise ValueError('Input asset outside project')
                    source=self.store.file(w['project'],'sources/'+asset['filename'])
                    target=sources/asset['filename']
                else:
                    parent=next((x for x in w['steps'] if x['id']==ref['step']),None)
                    if not parent or parent['status']!='succeeded' or parent['id'] not in s['spec']['depends_on']:
                        raise ValueError('Input dependency is not approved')
                    relative=parent['quality'].get('output_roles',{}).get(ref['role'])
                    if not relative or relative not in parent['result']:raise ValueError('Missing approved output role')
                    logical_path(relative)
                    rows=self.store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                        (w['id'],parent['id'],parent['revision']))
                    record=json.loads(rows[0]['manifest']).get(relative) if rows else None
                    if not record:raise ValueError('Dependency has no durable file reference')
                    source=(self.store.artifacts.materialize(w['project'],record['artifact']) if self.store.shared_files
                            else self.store.root/record['location'])
                    if digest(source)!=record['sha256']:raise ValueError('Dependency integrity mismatch')
                    target=workspace/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():shutil.copyfile(source,target)
            yield {'workspace':workspace,'sources':sources,'assets':assets,
                   'uid':os.getuid(),'gid':os.getgid(),'project':self.store.project_dir(w['project'])}

    def endpoint(self, s):
        if s.get('workflow'):
            saved=self.store.query('SELECT provider FROM workflow_provider_calls WHERE request_key=?',(s['workflow']+':'+s['id']+':'+str(s['revision']),))
            if saved:return saved[0]['provider'],'source_image'
        from .operations import for_step
        op=for_step(s['spec'])
        from .provider_metadata import route
        return route(op.route_adapter).endpoint()

    @measured('submit')
    def submit(self,w,s):
        try:return self._submit(w,s)
        except Exception as exc:
            from .task_leases import LeaseLost,TaskQuotaReached
            from .task_lifecycle import TaskLimitReached
            if isinstance(exc,(LeaseLost,TaskQuotaReached,TaskLimitReached)):raise
            rows=self.store.query('SELECT state FROM workflow_provider_calls WHERE request_key=?',(w['id']+':'+s['id']+':'+str(s['revision']),))
            if not rows:raise StepFailure({'message':'提交前检查失败，未发起生成请求','code':'pre_submit',
                'request_sent':False,'cause_type':type(exc).__name__,'detail':str(exc)[-1500:]}) from exc
            raise

    def _submit(self, w, s):
        from .task_leases import CURRENT
        lease = CURRENT.get()
        from .operations import for_step
        operation=for_step(s['spec'])
        if lease:lease.check()
        if operation.executor=='generation_async':return self.submit_generation(w,s)
        if operation.action in ('erase','local_edit'):
            return self.submit_image(w,s,self.environment(w))
        with self.input_environment(w,s) as env:
            return self.submit_image(w,s,env)

    def submit_image(self,w,s,env):
        from .operations import for_step
        operation=for_step(s['spec'])
        from .workflow_inputs import image_task
        task,items=image_task(self.store,w,s,env)
        assets={a['id']:a for a in items}
        sources=env['sources']
        base, field = self.endpoint(s)
        if task['action']!='decompose':
            from .image_document import edit_prompt
            from .image_providers import check_image_request,EDITOR_CONTRACT
            selected=task['action'] in ('erase','local_edit')
            ids=[task['asset'],*task['references']]+([task['asset']] if selected else [])
            check_image_request(EDITOR_CONTRACT,[(assets[aid]['metadata']['width'],assets[aid]['metadata']['height']) for aid in ids],
                                edit_prompt(task['action'],task['prompt'],selected=selected))
        params = ({'prompt': task['prompt'], 'layer_count': str(task['layer_count']),
                   'resolution': '640', 'quality': 'balanced', 'seed': '777', 'reconstruction_guard': 'true'}
                  if task['action'] == 'decompose' else
                  {'prompt': task['prompt'], 'quality': task['quality'], 'output_megapixels': '0.6',
                   'seed': '2511', 'preserve_details': 'true', 'auto_retry': 'false'})
        if task['action'] in ('erase','local_edit'):
            folder='edit/workflows/'+w['id']+'/'+s['id']+'/prepare-'+uuid.uuid4().hex
            self.isolated(dict(env,mode='plan',step_output=folder),{'mode':'prepare_image','output':folder,'task':task,
                'source':'sources/'+assets[task['asset']]['filename'],
                'mask':'sources/'+assets[task['mask']]['filename'] if task.get('mask') else None},60,lambda:False)
            prepared={'source':folder+'/source.png','mask':folder+'/mask.png','guide':folder+'/selection.png'}
            from .workflows import change,get
            if w.get('manual'):
                latest=self.store.query('SELECT bindings FROM manual_operation_runs WHERE job_id=?',(w['run_id'],))[0]
                self.store.execute('UPDATE manual_operation_runs SET bindings=? WHERE job_id=?',(json.dumps({**json.loads(latest['bindings']),'_prepared':prepared}),w['run_id']))
            else:
                latest=next(x for x in get(self.store,w['run_id'])['steps'] if x['id']==s['id'])
                change(self.store,w['id'],s['id'],bindings={**latest['bindings'],'_prepared':prepared})
            if env.get('runtime'):env['runtime'].checkpoint()
            from .image_document import edit_prompt
            params['prompt']=edit_prompt(task['action'],task['prompt'],selected=True)
        with self.session() as session, contextlib.ExitStack() as stack, request_time(self.store,w,s,'submit'):
            files = []
            for key, aid in [(field, task['asset']), *[('reference_images', a) for a in task['references']]]:
                source = sources/assets[aid]['filename']
                files.append((key, (source.name, stack.enter_context(source.open('rb')),
                                    mimetypes.guess_type(source.name)[0] or 'application/octet-stream')))
            if task['action'] in ('erase','local_edit'):
                files.insert(1,('reference_images',('selection.png',stack.enter_context((env['workspace']/prepared['guide']).open('rb')),'image/png')))
            # A stable key helps providers that support it, but is NOT assumed to
            # guarantee deduplication. Unknown submission outcomes still pause.
            from .provider_calls import intent
            request_key=intent(self.store,w,s,base,operation.id)
            response = session.post(base+'/api/jobs', files=files, data=params,
                                    headers={'Idempotency-Key':request_key},
                                    timeout=(10, 120), allow_redirects=False)
            if response.status_code in (400,401,403,404,422):
                from .provider_calls import update
                update(self.store,w,s,'rejected')
                raise StepFailure({'message':'上游拒绝请求，请核对能力、参数或服务授权','code':'provider_rejected','http_status':response.status_code})
            if response.status_code != 202:
                raise RuntimeError('Provider submission was not acknowledged')
            identifier=response.json()['id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',identifier):raise ValueError('Invalid provider ID')
            from .provider_calls import update
            update(self.store,w,s,'waiting',identifier,cookies=session.cookies.get_dict())
            return identifier

    def submit_generation(self,w,s):
        if s['spec']['operation']=='audio.generate_from_text' or os.environ.get('VIDEO_USE_GENERATION_BACKEND','jobs')=='ark' or (s['spec']['operation']=='video.generate_from_image' and os.environ.get('VIDEO_USE_IMAGE_VIDEO_BACKEND')=='wan'):
            return self.submit_uniform_generation(w,s)
        with self.input_environment(w,s) as env:
            return self.submit_generation_job(w,s,env)

    def submit_generation_job(self,w,s,env):
        from .generation_provider import endpoint,headers,validate
        from .workflow_inputs import resolve
        from .provider_calls import intent,update
        spec=s['spec'];validate(spec['operation'],spec['parameters'])
        bindings=resolve(self.store,w,s,env)
        base=endpoint()
        from .workflows import get
        current=next(x for x in get(self.store,w['run_id'])['steps'] if x['id']==s['id'])
        with self.session() as client,contextlib.ExitStack() as stack,request_time(self.store,w,s,'submit'):
            files=[]
            if bindings:
                ref=bindings['source']
                source=(env['sources']/Path(ref['path']).name) if ref.get('asset') else env['workspace']/ref['path']
                if ref['metadata'].get('kind')!='image':raise ValueError('图生视频需要图片参考')
                files=[('source_image',(source.name,stack.enter_context(source.open('rb')),mimetypes.guess_type(source.name)[0] or 'application/octet-stream'))]
            key=intent(self.store,w,current,base,spec['operation'])
            response=client.post(base+'/api/jobs',data={'operation':spec['operation'],**spec['parameters']},files=files or None,
                headers={**headers(),'Idempotency-Key':key},timeout=(10,120),allow_redirects=False)
            if response.status_code in (400,401,403,404,422):
                update(self.store,w,current,'rejected')
                raise StepFailure({'message':'生成服务拒绝请求','code':'provider_rejected','http_status':response.status_code})
            if response.status_code!=202:raise RuntimeError('生成服务未确认提交')
            identifier=response.json()['id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',identifier):raise ValueError('Invalid provider task ID')
            update(self.store,w,current,'waiting',identifier,cookies=client.cookies.get_dict())
            return identifier

    def submit_uniform_generation(self,w,s):
        with self.input_environment(w,s) as env:
            return self.submit_uniform_prepared(w,s,env)

    def submit_uniform_prepared(self,w,s,env):
        from . import generation_service
        from .workflow_inputs import resolve
        from .provider_calls import intent,update
        from .workflows import get
        spec=s['spec'];bindings=resolve(self.store,w,s,env)
        reference=None
        if bindings:
            ref=bindings['source']
            if ref['metadata'].get('kind')!='image':raise ValueError('图生视频需要图片参考')
            source=(env['sources']/Path(ref['path']).name) if ref.get('asset') else env['workspace']/ref['path']
            if source.stat().st_size>20*1024**2:raise ValueError('参考图片过大')
            reference=source.read_bytes()
        prepared=generation_service.prepare(spec['operation'],spec['parameters'],reference)
        current=next(x for x in get(self.store,w['run_id'])['steps'] if x['id']==s['id'])
        key=intent(self.store,w,current,prepared['provider'],spec['operation'],generation_service.implementation(prepared))
        try:
            with self.session() as client,request_time(self.store,w,s,'submit'):
                result=generation_service.submit(client,prepared,key)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (400,401,403,404,422,429):
                update(self.store,w,current,'rejected')
                raise StepFailure({'message':'生成服务拒绝请求','code':'provider_rejected','http_status':exc.response.status_code,
                                   'provider_error_code':getattr(exc,'provider_error_code','http_error')}) from exc
            raise
        update(self.store,w,current,'waiting',result['id'])
        return result['id']

    @measured('remote_sync')
    def generate_image(self,w,s,cancelled):
        from . import generation_service
        from .provider_calls import intent,update
        from .task_leases import CURRENT
        lease=CURRENT.get()
        env=self.environment(w)
        prepared=generation_service.prepare('image.generate_from_text',s['spec']['parameters'])
        intent(self.store,w,s,prepared['provider'],'image.generate_from_text',generation_service.implementation(prepared))
        try:
            with self.session() as client,request_time(self.store,w,s,'submit'):
                result=generation_service.submit(client,prepared,w['id']+':'+s['id']+':'+str(s['revision']))
                raw=result['artifacts'][0]['bytes'];metadata={'usage':result.get('usage'),'cost':result.get('cost')}
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (400,401,403,404,422):
                update(self.store,w,s,'rejected')
                raise StepFailure({'message':'文生图服务拒绝请求','code':'provider_rejected','http_status':exc.response.status_code}) from exc
            raise
        update(self.store,w,s,'succeeded',record=metadata)
        if cancelled():raise RuntimeError('生成结果返回时任务已停止')
        output='edit/workflows/'+w['id']+'/'+s['id']+'/'+uuid.uuid4().hex
        params=s['spec']['parameters']
        native=output+'/native' if params.get('fit','native')!='native' else output
        self.isolated(dict(env,mode='plan',step_output=native),{'mode':'import','output':native,'files':[{'name':'image.png','data':base64.b64encode(raw).decode()}]},60,cancelled)
        if native!=output:
            self.isolated(dict(env,mode='plan',step_output=output),{'mode':'fit_image','source':native+'/image.png','output':output,
                'width':params['width'],'height':params['height'],'fit':params['fit'],'background':params['background']},60,cancelled)
        if env.get('runtime'):env['runtime'].checkpoint()
        with self.store.connect() as db:
            update(self.store,w,s,'succeeded',record=metadata)
            db.execute('UPDATE workflow_provider_calls SET result=? WHERE request_key=?',(json.dumps([output+'/image.png']),w['id']+':'+s['id']+':'+str(s['revision'])))
        return [output+'/image.png']

    @measured('remote_sync')
    def transcribe(self,w,s,cancelled):
        from .workflow_inputs import resolve
        from .provider_calls import intent,update
        from .workflows import get
        env=self.environment(w);bindings=resolve(self.store,w,s,env)
        if set(bindings)!={'source'}:raise ValueError('转写需要唯一 source 输入')
        source=bindings['source'];params=s['spec']['parameters']
        if source['metadata'].get('kind') not in ('audio','video'):raise ValueError('转写需要音视频输入')
        if source['metadata'].get('duration',0)>int(os.environ.get('VIDEO_USE_MAX_TRANSCRIBE_SECONDS','7200')):raise ValueError('素材过长，请先分段')
        output='edit/workflows/'+w['id']+'/'+s['id']+'/'+uuid.uuid4().hex
        import hashlib
        from .workflow_inputs import digest
        cache_key=hashlib.sha256(json.dumps({'source':source['sha256'],'parameters':params,'model':'scribe_v1'},sort_keys=True).encode()).hexdigest()
        cached=self.store.query('SELECT path,sha256 FROM workflow_transcript_cache WHERE project=? AND cache_key=?',(w['project'],cache_key))
        if cached:
            path=env['workspace']/cached[0]['path']
            if not path.is_file() or digest(path)!=cached[0]['sha256']:raise StepFailure({'message':'已记录转写缓存不可用，未自动重提','code':'environment'})
            if path.stat().st_size>2*1024**2:raise ValueError('转写缓存过大')
            self.isolated(dict(env,mode='plan',step_output=output+'/result'),{'mode':'import','output':output+'/result','files':[{'name':'transcript.json','data':base64.b64encode(path.read_bytes()).decode()}]},60,cancelled)
            if env.get('runtime'):env['runtime'].checkpoint()
            return [output+'/result/transcript.json']
        self.isolated(dict(env,mode='plan',step_output=output),{'mode':'local','output':output,
            'argv':['ffmpeg','-v','error','-i',source['path'],'-map','0:a:'+str(params['audio_track']),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le', '{output}/audio.wav'],
            'outputs':['audio.wav']},min(300,s['spec']['timeout']),cancelled)
        audio=env['workspace']/output/'audio.wav'
        if audio.stat().st_size>256*1024**2:raise ValueError('转写音频超过上传限制')
        import sys
        helpers=Path(__file__).resolve().parents[1]/'helpers'
        if str(helpers) not in sys.path:sys.path.insert(0,str(helpers))
        from transcribe import call_scribe,peak_dbfs
        if peak_dbfs(audio)<-60:raise ValueError('音频为空或静音，未提交收费请求')
        current=next(x for x in get(self.store,w['run_id'])['steps'] if x['id']==s['id'])
        intent(self.store,w,current,'https://api.elevenlabs.io','audio.transcribe')
        from .task_leases import CURRENT
        lease=CURRENT.get()
        with request_time(self.store,w,s,'submit'):
            record=call_scribe(audio,os.environ.get('ELEVENLABS_API_KEY',''),params['language'] or None,params['speakers'])
        update(self.store,w,current,'succeeded',record={'usage':record.get('usage')})
        if cancelled():raise RuntimeError('转写完成前任务已停止')
        data=json.dumps(record,ensure_ascii=False).encode()
        if len(data)>2*1024**2:raise ValueError('转写响应过大')
        self.isolated(dict(env,mode='plan',step_output=output+'/result'),{'mode':'import','output':output+'/result','files':[{'name':'transcript.json','data':base64.b64encode(data).decode()}]},60,cancelled)
        if env.get('runtime'):env['runtime'].checkpoint()
        result_path=output+'/result/transcript.json'
        with self.store.connect() as db:
            self.store.execute('INSERT INTO workflow_transcript_cache VALUES (?,?,?,?,?) ON CONFLICT(project,cache_key) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,created=excluded.created',
                (w['project'],cache_key,result_path,digest(env['workspace']/result_path),self.store.now()))
            update(self.store,w,current,'succeeded',record={'usage':record.get('usage')})
            db.execute('UPDATE workflow_provider_calls SET result=? WHERE request_key=?',(json.dumps([result_path]),w['id']+':'+s['id']+':'+str(s['revision'])))
        return [result_path]

    def provider_headers(self,s):
        from .operations import for_step
        if for_step(s['spec']).route_adapter=='generation_jobs':
            from .generation_provider import headers,endpoint
            if s.get('workflow'):
                saved=self.store.query('SELECT provider FROM workflow_provider_calls WHERE request_key=?',(s['workflow']+':'+s['id']+':'+str(s['revision']),))
                if saved and saved[0]['provider']!=endpoint():raise ValueError('Provider configuration changed; restore the original credential route')
            return headers()
        return {}

    @measured('poll')
    def poll(self, w, s):
        from .provider_calls import implementation,update
        from . import generation_service
        handle=implementation(self.store,w,s)
        if handle.get('protocol') in generation_service.PROVIDERS:
            with self.session() as client,request_time(self.store,w,s,'poll'):
                record=generation_service.poll(client,handle,s['external_id'])
            update(self.store,w,s,record['status'],record=record)
            return record
        base, _ = self.endpoint(s)
        with self.session() as session,request_time(self.store,w,s,'poll'):
            from .provider_calls import restore_session
            restore_session(self.store,w,s,session)
            response = session.get(base+'/api/jobs/'+s['external_id'],headers=self.provider_headers(s), timeout=(10, 30), allow_redirects=False)
            response.raise_for_status()
            record=response.json()
            from .provider_calls import update
            # The jobs API calls its accepted queue state "queued"; the
            # provider ledger calls it "waiting". Preserve the raw response.
            state=record.get('status','waiting')
            update(self.store,w,s,'waiting' if state=='queued' else state,record=record)
            return record

    def cancel(self, w, s):
        from .provider_calls import implementation
        from . import generation_service
        handle=implementation(self.store,w,s)
        if handle.get('protocol') in generation_service.PROVIDERS:
            with self.session() as client:return generation_service.cancel(client,handle,s['external_id'])
        base, _ = self.endpoint(s)
        with self.session() as session:
            from .provider_calls import restore_session
            restore_session(self.store,w,s,session)
            response = session.post(base+'/api/jobs/'+s['external_id']+'/cancel',headers=self.provider_headers(s), timeout=(10, 15), allow_redirects=False)
            response.raise_for_status()

    def isolated(self, env, payload, timeout, cancelled):
        """No model/provider gateway is exposed to background local scripts."""
        runtime=env.get('runtime')
        if runtime and getattr(runtime,'is_command',False):
            return runtime.execute_payload(env,payload,timeout,cancelled)
        relative=env.get('step_output')
        if relative:
            parent=env['workspace']
            for part in Path(relative).parts:
                parent=parent/part
                if parent.is_symlink():raise ValueError('Unsafe step output')
                parent.mkdir(exist_ok=True,mode=0o700);os.chown(parent,env['uid'],env['gid'])
        path = temporary_socket(self.socket_dir)
        temporary = self.store.root/'tmp'
        temporary.mkdir(exist_ok=True)
        process = None
        monitored = None
        try:
            with socket.socket(socket.AF_UNIX) as sock, tempfile.TemporaryFile(dir=temporary) as request, tempfile.TemporaryFile(dir=temporary) as errors, tempfile.TemporaryFile(dir=temporary) as diagnostic:
                sock.bind(str(path))
                os.chown(path, env['uid'], env['gid'])
                path.chmod(0o600)
                request.write(json.dumps(payload, ensure_ascii=False).encode())
                request.seek(0)
                from .task_leases import CURRENT
                from .cancellation import for_store, stop_process
                lease = CURRENT.get()
                if lease:
                    monitored = for_store(self.store).register_lease(lease, 'local')
                    monitored.check()
                process = subprocess.Popen(agent_sandbox.command(env, path,
                    ['/opt/venv/bin/python', '/opt/operation_io.py' if payload.get('mode') in ('prepare_image','composite_image','fit_image') else '/opt/workflow_io.py']), stdin=request,
                    stdout=diagnostic, stderr=errors, start_new_session=True,
                    env={'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8'})
                if monitored:monitored.install(lambda: stop_process(process))
                deadline = time.monotonic()+timeout
                last_progress=''
                while process.poll() is None:
                    from .task_leases import CURRENT
                    lease = CURRENT.get()
                    if monitored:monitored.check()
                    elif lease:lease.check()
                    relative=payload.get('output')
                    if relative:
                        logfile=env['workspace']/relative/'execution.log'
                        try:
                            fd=os.open(logfile,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                            with os.fdopen(fd,'rb') as log:
                                info=os.fstat(log.fileno())
                                if stat.S_ISREG(info.st_mode):
                                    log.seek(max(0,info.st_size-4096));line=log.read(4096).decode(errors='replace').splitlines()[-1:]
                                    if line and line[0]!=last_progress:
                                        last_progress=line[0]
                                        from .egress_policy import scrub,secrets
                                        note=json.loads(line[0])
                                        if note.get('type')=='step_progress' and lease:agent_store.event(self.store,lease.run_id,'progress',{'text':scrub(str(note.get('text',''))[:500],secrets())})
                        except (OSError,ValueError,TypeError,AttributeError):pass
                    if cancelled():raise StepFailure({'message':'本地步骤已停止','code':'cancelled'})
                    if time.monotonic() > deadline:
                        raise StepFailure({'message':'本地步骤超过执行时限','code':'timeout','timeout':timeout})
                    time.sleep(.2)
                from .telemetry import process_exit
                process_exit(process, getattr(getattr(lease.runtime,'groups',None),'paths',None) if lease and lease.runtime else None)
                if process.returncode:
                    diagnostic.seek(0)
                    try:detail=json.loads(diagnostic.read(32768))
                    except (ValueError,UnicodeError):detail={}
                    errors.seek(0)
                    raise StepFailure({'message':'本地步骤未生成有效结果','exit_code':process.returncode,
                                       'runtime_error':errors.read(3000).decode(errors='replace'),**detail})
                if payload.get('mode')=='local':
                    diagnostic.seek(0)
                    raw=diagnostic.read(131073)
                    if len(raw)>131072:raise StepFailure({'message':'本地脚本回执超过上限','code':'script_receipt_invalid'})
                    receipt=json.loads(raw)
                    if not isinstance(receipt,dict) or receipt.get('version')!=1 or receipt.get('returncode')!=0:
                        raise StepFailure({'message':'本地脚本回执不完整','code':'script_receipt_invalid'})
                    return receipt
        finally:
            if process and process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            path.unlink(missing_ok=True)
            if monitored:monitored.close()

    def preflight(self,w,s):
        env=self.environment(w)
        spec=s['spec']
        import hashlib
        from .runtime_image import fingerprint
        environment_profile=fingerprint()
        previous_environment=s.get('bindings',{}).get('_environment')
        if previous_environment and previous_environment!=environment_profile:raise StepFailure({'message':'执行环境版本已变化，需要检查后修订','code':'code_version'})
        files=list(dict.fromkeys(spec.get('preflight',[])+spec.get('config_files',[])+[c['checker'] for c in spec.get('acceptance',[]) if c.get('checker')]+
            [a for a in spec.get('argv',[]) if a.startswith('edit/') and a.endswith(('.py','.json','.sh'))]))
        if not files:return {'passed':True,'files':{},'issues':[],'coverage':'No script inputs'}
        path=temporary_socket(self.socket_dir)
        try:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(path));os.chown(path,env['uid'],env['gid']);path.chmod(0o600)
                result=subprocess.run(agent_sandbox.command(dict(env,mode='plan'),path,
                    ['/opt/venv/bin/python','/opt/script_preflight.py']),input=json.dumps({'files':files}).encode(),
                    capture_output=True,timeout=30)
            if result.returncode:raise StepFailure({'message':'脚本预检查未能完成','log_tail':result.stderr.decode(errors='replace')[-2000:]})
            report=json.loads(result.stdout)
            if not report['passed']:raise StepFailure({'message':'脚本预检查不通过','code':'preflight','report':report})
            previous=s.get('bindings',{}).get('_code')
            if previous and previous!=report['files']:raise StepFailure({'message':'脚本/配置版本变化，需要显式修订','code':'code_version'})
            from .workflows import change,get
            existing=get(self.store,w['run_id'])
            if existing and existing['id']==w['id']:
                current=next(x for x in existing['steps'] if x['id']==s['id'])
                change(self.store,w['id'],s['id'],bindings={**current['bindings'],'_code':report['files'],'_environment':environment_profile})
            report['environment_profile']=environment_profile
            if existing and existing['id']==w['id']:
                latest=next(x for x in get(self.store,w['run_id'])['steps'] if x['id']==s['id'])
                change(self.store,w['id'],s['id'],bindings={**latest['bindings'],'_preflight':report})
            return report
        finally:path.unlink(missing_ok=True)

    @measured('local')
    def local(self, w, s, cancelled):
        self.preflight(w,s)
        env = self.environment(w)
        output = 'edit/workflows/'+w['id']+'/'+s['id']+'/'+uuid.uuid4().hex
        inputs = {step['id']: step['result'] for step in w['steps'] if step['id'] in s['spec']['depends_on']}
        from .workflow_inputs import resolve
        bound=resolve(self.store,w,s,env)
        inputs.update({name:[record['path']] for name,record in bound.items()})
        folder = env['workspace']/output
        parent = env['workspace']
        for part in Path(output).parts:
            parent = parent/part
            if parent.is_symlink():raise RuntimeError('Workflow output cannot contain links')
            if not parent.exists():
                parent.mkdir(mode=0o700)
                os.chown(parent, env['uid'], env['gid'])
        # Safe replay is enforced by mounts, not just by the Agent's retry_safe
        # assertion: only this invocation's output is writable; no model gateway.
        confined = dict(env, mode='plan', step_output=output)
        try:
            self.isolated(confined, {'mode':'local', 'output':output, 'argv':s['spec']['argv'],
                               'outputs':s['spec']['outputs'], 'inputs':inputs}, s['spec']['timeout'], cancelled)
        except StepFailure as exc:
            # The sandbox is stopped before reading its bounded, regular log.
            try:
                fd=os.open(folder/'execution.log',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                with os.fdopen(fd,'rb') as log:
                    info=os.fstat(log.fileno())
                    if stat.S_ISREG(info.st_mode):
                        log.seek(max(0,info.st_size-8000))
                        exc.diagnostic['log_tail']=log.read(8000).decode(errors='replace')
            except OSError:pass
            raise
        if env.get('runtime'):env['runtime'].checkpoint()
        return [output+'/'+name for name in s['spec']['outputs']]

    @measured('local')
    def fixed_media(self,w,s,cancelled):
        from .workflow_inputs import staged_assets
        from .operations import for_step
        from .paths import SANDBOX_WORKSPACE
        env=self.environment(w);assets,ids=staged_assets(self.store,w,s,env)
        op=for_step(s['spec']);payload={'kind':op.action,**s['spec']['parameters'],'assets':list(ids.values())}
        if op.id=='media.evidence' and payload.get('mode')=='audio_compare' and 'reference' not in ids:
            raise StepFailure({'message':'音频对比证据缺少 reference 输入','code':'operation_input_contract'})
        assets=[a for a in assets if a['id'] in ids.values()]
        media_assets=[a for a in assets if a['metadata']['kind']!='json']
        from .media_transcript_inputs import materialized
        transcript_inputs=materialized(op.action,s['spec']['parameters'],s['spec'].get('inputs',{}),ids)
        if op.action!='pack':payload['assets']=[a['id'] for a in media_assets]
        if op.action in ('timeline','analyze','review') and (len(media_assets)!=1 or media_assets[0]['metadata']['kind']!='video'):
            raise ValueError('该媒体检查操作需要唯一视频输入')
        def replace(value):
            if isinstance(value,dict):
                if isinstance(value.get('asset'),str) and value['asset'] not in ids and value['asset'] not in ids.values():raise ValueError('媒体参数引用了未声明输入')
                return {k:ids.get(v,v) if k=='asset' and isinstance(v,str) else replace(v) for k,v in value.items()}
            if isinstance(value,list):return [replace(v) for v in value]
            return value
        payload=replace(payload)
        if payload.get('subtitle_asset'):
            supplied=payload['subtitle_asset']
            if supplied not in ids and supplied not in ids.values():raise ValueError('字幕引用不属于声明输入')
            payload['subtitle_asset']=ids.get(supplied,supplied)
        output='edit/workflows/'+w['id']+'/'+s['id']+'/'+uuid.uuid4().hex
        folder=env['workspace']/output;parent=env['workspace']
        for part in Path(output).parts:
            parent=parent/part
            if parent.is_symlink():raise ValueError('Invalid output path')
            parent.mkdir(exist_ok=True,mode=0o700);os.chown(parent,env['uid'],env['gid'])
        manifest=folder/'request.json'
        manifest.write_text(json.dumps({'project_dir':SANDBOX_WORKSPACE,'output_dir':SANDBOX_WORKSPACE+'/'+output,
                                       'payload':payload,'assets':assets,'input_roles':ids,'transcript_inputs':transcript_inputs,'explicit_inputs':True}))
        os.chown(manifest,env['uid'],env['gid']);manifest.chmod(0o400)
        self.isolated(dict(env,mode='plan',step_output=output,manual=True),{'mode':'local','output':output,
            'argv':['python','-P','-m','webapp.worker',output+'/request.json'],'outputs':['result.json']},s['spec']['timeout'],cancelled)
        result=folder/'result.json'
        if result.is_symlink() or result.stat().st_size>65536:raise ValueError('Invalid result manifest')
        from .operation_result import Result,ProducedFiles
        result_record=Result.model_validate_json(result.read_text())
        names=[item.path for item in result_record.outputs]
        declared_roles={item.role:output+'/'+item.path for item in result_record.outputs if item.role!='auxiliary'}
        if set(declared_roles)-set(op.outputs):raise ValueError('Worker returned an undeclared output role')
        if env.get('runtime'):env['runtime'].checkpoint()
        return ProducedFiles([output+'/'+name for name in names],declared_roles)

    @measured('local')
    def fixed_image(self,w,s,cancelled):
        """Fixed offline image worker; local argv never receives a model gateway."""
        from .image_sandbox import image_command,gpu_slot
        from .task_leases import CURRENT
        lease=CURRENT.get()
        if not lease:raise RuntimeError('图片后台步骤需要有效执行租约')
        lease.consume('local_image',int(os.environ.get('VIDEO_USE_TASK_LOCAL_IMAGE_CALLS','20')))
        from .paths import SANDBOX_WORKSPACE
        env=self.environment(w)
        from .workflow_inputs import image_task
        from .operations import for_step
        task,assets=image_task(self.store,w,s,env)
        operation=for_step(s['spec'])
        output='edit/workflows/'+w['id']+'/'+s['id']+'/'+uuid.uuid4().hex
        work=env['workspace']/output
        parent=env['workspace']
        for part in Path(output).parts:
            parent=parent/part
            if parent.is_symlink():raise ValueError('Invalid image output directory')
            parent.mkdir(exist_ok=True,mode=0o700);os.chown(parent,env['uid'],env['gid'])
        (work/'output').mkdir(mode=0o700);os.chown(work/'output',env['uid'],env['gid'])
        request=work/'request.json'
        request.write_text(json.dumps({'task':task,'assets':assets,
            'project':SANDBOX_WORKSPACE,'output':'/tool/output'}))
        os.chown(request,env['uid'],env['gid']);request.chmod(0o600)
        class Cancellation:
            def wait(self,seconds):
                lease.check()
                if cancelled():return True
                time.sleep(seconds)
                return cancelled()
        path=temporary_socket(self.socket_dir)
        process=None
        deadline=time.monotonic()+s['spec']['timeout']
        try:
            agent_store.event(self.store,w['run_id'],'progress',{'text':s['spec']['name']+('：等待 GPU' if operation.gpu else '：准备处理')})
            with socket.socket(socket.AF_UNIX) as sock,contextlib.ExitStack() as stack:
                scope=lease.runtime.scope()
                try:gpu=stack.enter_context(gpu_slot(task['action'],lease,Cancellation(),scope,timeout=min(300,s['spec']['timeout'])))
                except Exception:
                    lease.runtime.close_scope(scope)
                    raise
                stack.callback(lease.runtime.close_scope,scope)
                sock.bind(str(path));os.chown(path,env['uid'],env['gid']);path.chmod(0o600)
                with (work/'execution.log').open('w+b') as log:
                    agent_store.event(self.store,w['run_id'],'progress',{'text':s['spec']['name']+'：正在处理图片'})
                    process=subprocess.Popen(image_command(dict(env,image_action=task['action']),path,work,gpu,scope,timeout=s['spec']['timeout']),stdout=log,stderr=subprocess.STDOUT,
                        start_new_session=True,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
                    scope['stopped']=False
                    while process.poll() is None:
                        lease.check()
                        if cancelled() or time.monotonic()>deadline:
                            log.seek(max(0,os.fstat(log.fileno()).st_size-4000))
                            tail=log.read(4000).decode(errors='replace')
                            model_log=work/'output/model.log'
                            if model_log.is_file() and not model_log.is_symlink():
                                with model_log.open('rb') as source:
                                    source.seek(max(0,model_log.stat().st_size-4000));tail+='\n'+source.read(4000).decode(errors='replace')
                            raise StepFailure({'message':'抠图步骤已停止或超过时限','code':'timeout','log_tail':tail})
                        time.sleep(.2)
                    from .telemetry import process_exit
                    process_exit(process,scope['paths'])
                    if process.returncode:
                        log.seek(max(0,os.fstat(log.fileno()).st_size-8000))
                        raise StepFailure({'message':'固定图片处理失败','exit_code':process.returncode,'log_tail':log.read(8000).decode(errors='replace')})
            record=work/'output/provider-result.json'
            if record.is_symlink() or record.stat().st_size>4096:raise ValueError('无效图片结果清单')
            names=json.loads(record.read_text())
            if not isinstance(names,list) or not names or len(names)>40 or any(not isinstance(n,str) or Path(n).name!=n for n in names):raise ValueError('图片结果清单无效')
            if task['action'] in ('remove_bg','segment','segment_sam3','refine_zim') and not {'foreground.png','mask.png'}.issubset(names):raise ValueError('未生成前景与蒙版')
            lease.runtime.checkpoint()
            return [output+'/output/'+name for name in names]
        finally:
            if process and process.poll() is None:
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
                    process.wait()
            path.unlink(missing_ok=True)

    @measured('collect')
    def collect(self, w, s, record, cancelled):
        from .provider_calls import implementation
        from . import generation_service
        handle=implementation(self.store,w,s)
        base, _ = self.endpoint(s)
        env = self.environment(w)
        output = 'edit/workflows/'+w['id']+'/'+s['id']+'/'+uuid.uuid4().hex
        files, total = [], 0
        with self.session() as session,request_time(self.store,w,s,'collect'):
            from .provider_calls import restore_session
            restore_session(self.store,w,s,session)
            for i, item in enumerate(record.get('artifacts', [])[:30]):
                aid = item.get('id', '')
                extension = Path(item.get('name', '')).suffix.lower()
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', aid):
                    continue
                if extension not in ('.png','.jpg','.jpeg','.webp','.psd','.ora','.json','.zip','.mp4','.webm','.mov','.mkv',
                                      '.mp3','.wav','.flac','.m4a','.aac','.ogg','.opus'):
                    continue
                if handle.get('protocol') in generation_service.PROVIDERS:
                    response=generation_service.download(session,item,handle)
                else:
                    response=session.get(base+f'/api/jobs/{s["external_id"]}/artifacts/{aid}',
                                         headers=self.provider_headers(s),stream=True,timeout=(10,60),allow_redirects=False)
                with response:
                    if handle.get('protocol') in generation_service.PROVIDERS:
                        from .generation_errors import raise_for_status
                        raise_for_status(response)
                    else:response.raise_for_status()
                    chunks = []
                    for chunk in response.iter_content(1024*1024):
                        if cancelled():
                            raise RuntimeError('Cancelled')
                        total += len(chunk)
                        if total > int(os.environ.get('VIDEO_USE_PROVIDER_RESULT_BYTES',str(512*1024**2))):
                            raise ValueError('Provider results exceed 64 MB')
                        chunks.append(chunk)
                    name=Path(item.get('name','')).name
                    if not re.fullmatch(r'[A-Za-z0-9_\u3400-\u9fff][A-Za-z0-9_.\u3400-\u9fff-]{0,99}',name):name=str(i).zfill(2)+'-'+aid+extension
                    if any(f['name']==name for f in files):raise ValueError('上游返回重复产物名称')
                    files.append({'name':name,
                                  'data':base64.b64encode(b''.join(chunks)).decode()})
        media_extensions=('.png','.jpg','.jpeg','.webp','.mp4','.webm','.mov','.mkv','.mp3','.wav','.flac','.m4a','.aac','.ogg','.opus')
        if not files or not any(Path(f['name']).suffix.lower() in media_extensions for f in files):
            raise ValueError('Provider returned no supported media artifact')
        self.isolated(dict(env,mode='plan',step_output=output), {'mode':'import', 'output':output, 'files':files}, 60, cancelled)
        paths=[output+'/'+f['name'] for f in files]
        prepared=s.get('bindings',{}).get('_prepared')
        if prepared:
            images=[p for p in paths if Path(p).suffix.lower() in ('.png','.jpg','.webp')]
            if len(images)!=1:raise ValueError('选区编辑需要唯一生成图片')
            final=output+'/composite'
            self.isolated(dict(env,mode='plan',step_output=final),{'mode':'composite_image','output':final,
                'source':prepared['source'],'mask':prepared['mask'],'generated':images[0]},60,cancelled)
            paths=[final+'/image.png']
        if env.get('runtime'):env['runtime'].checkpoint()
        return paths

    @measured('validate')
    def validate(self,w,s,files):
        env=self.environment(w)
        role_map=getattr(files,'roles',None) or s.get('quality',{}).get('output_roles') or {}
        payload={'files':files,'acceptance':resolve_acceptance_output_roles(s['spec'].get('acceptance',[]),role_map)}
        from .operations import for_step
        operation=for_step(s['spec'])
        if operation and operation.evidence_only:
            from .workflow_inputs import resolve,roles
            from .operation_parameters import EvidenceParameters
            from .operation_lifecycle import for_operation
            bindings=resolve(self.store,w,s,env);bound=bindings['source'];outputs=roles(s['spec'],files,getattr(files,'roles',None))
            payload['evidence_report']={'report_file':outputs['report'],'preview_file':outputs['preview'],
                'source_sha256':bound['sha256'],'source_reference':bound.get('asset') or bound['path'],
                'reference_sha256':bindings.get('reference',{}).get('sha256'),
                'parameters':EvidenceParameters.model_validate(s['spec']['parameters']).model_dump(),
                'implementation_id':for_operation(operation).id}
        mask=s.get('semantics',{}).get('mask_repair')
        if mask and mask['level'] in ('A','B'):
            from .workflow_inputs import resolve,roles
            bound=resolve(self.store,w,s,env)
            outputs=roles(s['spec'],files,getattr(files,'roles',None))
            payload['mask_repair']={'reference':bound[mask['reference_role']]['path'],
                'candidate':outputs[mask['output_role']],'max_changed_fraction':mask['max_changed_fraction']}
        runtime=env.get('runtime')
        if runtime and getattr(runtime,'is_command',False):
            report=runtime.validate_payload(payload)
            from .operation_result import validate_contract
            return validate_contract(for_step(s['spec']),report,role_map)
        path=temporary_socket(self.socket_dir)
        try:
            with socket.socket(socket.AF_UNIX) as sock,tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
                sock.bind(str(path));os.chown(path,env['uid'],env['gid']);path.chmod(0o600)
                result=subprocess.run(agent_sandbox.command(dict(env,mode='plan'),path,
                    ['/opt/venv/bin/python','/opt/quality_check.py']),
                    input=json.dumps(payload).encode(),
                    stdout=output,stderr=errors,timeout=90)
                if result.returncode:
                    errors.seek(0)
                    raise StepFailure({'message':'质量检查未能完成','exit_code':result.returncode,
                                       'log_tail':errors.read(4000).decode(errors='replace')})
                output.seek(0);report=json.loads(output.read(2*1024**2))
                from .operations import for_step
                from .operation_result import validate_contract
                return validate_contract(for_step(s['spec']),report,role_map)
        finally:path.unlink(missing_ok=True)

    def publish_candidate(self,w,files):
        self.publish(w,files,candidate=True)

    def publish(self, w, files, candidate=False):
        turn=agent_store.turn(self.store,w['run_id'])
        names={str(Path(name).relative_to('edit')) for name in files}
        if not candidate:
            changed=False
            for item in turn['artifacts']:
                if item['name'] in names and item.get('quality_state')=='pending':
                    item.pop('purpose',None);item['quality_state']='approved';changed=True
            if changed:self.store.execute('UPDATE agent_turns SET artifacts=?,updated=? WHERE id=?',
                (json.dumps(turn['artifacts']),self.store.now(),w['run_id']))
        published = {a['name'] for a in turn['artifacts']}
        files = [name for name in files if str(Path(name).relative_to('edit')) not in published]
        if not files:
            return
        env = self.environment(w)
        baseline = agent_sandbox.snapshot_outputs(env)
        for name in files:
            baseline.pop(str(Path(name).relative_to('edit')), None)
        env['output_baseline'] = baseline
        artifacts = agent_sandbox.export_results(env, w['run_id'])
        from .workflows import get
        current=get(self.store,w['run_id'])
        for item in artifacts:
            item['quality_state']='pending' if candidate else 'approved'
            if candidate:item['purpose']='quality_candidate'
            if current:
                for step in current['steps']:
                    if 'edit/'+item['name'] in step['result']+step['candidate']:
                        item['source_step']=step['id'];item['source_revision']=step['revision'];item['workflow_delivery']=False
                        item['output_roles']=[role for role,path in step['quality'].get('output_roles',{}).items() if path=='edit/'+item['name']]
                        break
        # Publishing a checkpoint does not mark the whole turn complete.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT artifacts FROM agent_turns WHERE id=?', (w['run_id'],)).fetchone()
            prior = json.loads(row['artifacts'])
            names = {a['name'] for a in artifacts}
            merged = [a for a in prior if a['name'] not in names]+artifacts
            self.store.check_storage(w['project'],sum(a.get('size',0) for a in merged),replace_turn=w['run_id'])
            db.execute('UPDATE agent_turns SET artifacts=?,updated=? WHERE id=?',
                       (json.dumps(merged, ensure_ascii=False), time.time(), w['run_id']))
