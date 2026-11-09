from __future__ import annotations

import hashlib
import asyncio
import html
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from PIL import Image, ImageOps, UnidentifiedImageError
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .models import JobInput, ProjectInput, BatchDeleteInput, DraftInput
from .media import thumbnail
from .store import Store
from .auth import Sessions
from . import agent_api, agent_store, image_api, edit_feedback
from .paths import data_root
from .edit_tools import bind_plan, edit_asset_ids, PlanError
from .api_execution import APIExecutors,APICapacityError,APIClientDisconnected,route_class,lane,admission

REPO = Path(__file__).resolve().parents[1]
from .deployment_identity import namespace
COOKIE = namespace().replace('-', '_')+'_session'
VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.avi', '.m4v', '.webm', '.ogv'}
AUDIO_EXTS = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.opus'}
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}
TEXT_DOCUMENT_EXTS = {'.txt', '.md', '.json', '.csv'}
OFFICE_DOCUMENT_EXTS = {'.docx', '.xlsx', '.pptx'}
DOCUMENT_EXTS = TEXT_DOCUMENT_EXTS | OFFICE_DOCUMENT_EXTS | {'.pdf'}
MAX_PROJECTS = max(1, int(os.environ.get('VIDEO_USE_MAX_PROJECTS', '100')))
DEMO_TO_TOOL = {
    'transcribe':'transcribe','batch':'pack','cuts':'render','grade':'grade','subtitles':'render',
    'overlay':'render','loudness':'render','timeline':'timeline','analysis':'analyze','multitrack':'transcribe',
    'local-speed':'speed','local-transition':'transition','local-reverse':'motion','local-geometry':'geometry',
    'local-split':'split','local-mask':'mask','local-music':'mix','local-audio-replace':'mix',
    'local-audio-clean':'audio','local-word-edit':'subtitles','video-quality-review':'review'
}


def inspect_media(path: Path, allow_audio=False) -> dict:
    from .media_isolation import run,ensure_available
    result = run(path, ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', '/input'])
    ensure_available(result)
    if result.returncode:
        raise ValueError('无法读取素材，请上传有效的音视频文件。')
    data = json.loads(result.stdout)
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    audio_tracks = [{'index': i, 'codec': s.get('codec_name', ''),
                     'channels': s.get('channels', 0),
                     'title': s.get('tags', {}).get('title', f'音轨 {i}')}
                    for i, s in enumerate(s for s in data.get('streams', []) if s.get('codec_type') == 'audio')]
    if not videos:
        if allow_audio and audio_tracks:
            duration = float(data.get('format', {}).get('duration', 0))
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError('无法读取有效的音频时长。')
            return {'kind':'audio','duration':duration,'size':path.stat().st_size,
                    'mime':mimetypes.guess_type(path.name)[0] or 'audio/wav','audio_tracks':audio_tracks}
        raise ValueError('文件没有视频轨道。')
    video = videos[0]
    duration = float(data.get('format', {}).get('duration', video.get('duration', 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('无法读取有效的视频时长。')
    width, height = int(video.get('width', 0)), int(video.get('height', 0))
    for side in video.get('side_data_list', []):
        if abs(float(side.get('rotation', 0))) % 180 == 90:
            width, height = height, width
    if min(width, height) <= 0:
        raise ValueError('视频尺寸无效。')
    return {'kind': 'video', 'duration': duration, 'width': width, 'height': height,
            'fps': video.get('avg_frame_rate', ''), 'size': path.stat().st_size,
            'audio_tracks': audio_tracks}


def inspect_image(path: Path) -> dict:
    from .media_isolation import inspect_image as isolated_inspect
    return isolated_inspect(path)


def inspect_document(path: Path, suffix: str) -> dict:
    size=path.stat().st_size
    if suffix in TEXT_DOCUMENT_EXTS:
        if size>20*1024**2:raise ValueError('文本资料不能超过 20 MB。')
        text=path.read_text(encoding='utf-8-sig')
        if '\x00' in text:raise ValueError('文本资料包含二进制内容。')
    elif suffix=='.pdf':
        if size>100*1024**2:raise ValueError('PDF 资料不能超过 100 MB。')
        with path.open('rb') as stream:
            if stream.read(8)[:5]!=b'%PDF-':raise ValueError('PDF 文件头无效。')
            stream.seek(max(0,size-2048));tail=stream.read()
        if b'%%EOF' not in tail:raise ValueError('PDF 文件结构不完整。')
    else:
        if size>100*1024**2:raise ValueError('Office 资料不能超过 100 MB。')
        try:
            with zipfile.ZipFile(path) as archive:
                members=archive.infolist()
                if '[Content_Types].xml' not in archive.namelist():raise ValueError('Office 文件缺少内容类型声明。')
                if len(members)>10000 or sum(item.file_size for item in members)>300*1024**2:raise ValueError('Office 文件展开后过大。')
                for item in members:
                    parts=Path(item.filename).parts
                    if item.flag_bits&1 or item.filename.startswith(('/', '\\')) or '..' in parts:
                        raise ValueError('Office 文件包含不安全的压缩成员。')
        except zipfile.BadZipFile as exc:raise ValueError('Office 文件结构无效。') from exc
    return {'kind':'document','size':size,'mime':mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}


def read_key() -> str:
    if os.environ.get('ELEVENLABS_API_KEY'):
        return os.environ['ELEVENLABS_API_KEY']
    path = REPO / '.env'
    if path.exists() and os.access(path, os.R_OK):
        for line in path.read_text().splitlines():
            if line.startswith('ELEVENLABS_API_KEY='):
                key = line.split('=', 1)[1].strip().strip('"\'')
                if key:
                    return key
    return os.environ.get('ELEVENLABS_API_KEY', '')


def create_app(data_dir: Path | None = None, run_worker=True) -> FastAPI:
    from .telemetry import setup
    setup('api')
    root = data_dir or data_root()
    database_seconds=float(os.environ.get('VIDEO_USE_API_DATABASE_SECONDS','5'))
    if not .1<=database_seconds<=30:raise ValueError('Invalid API database timeout')
    store = Store(root,timeout=database_seconds)
    agent_store.initialize(store)
    if store.shared_files:store.artifacts  # Initialize reusable storage client/schema before serving.
    sessions = Sessions(store)
    stop = threading.Event()
    wake = threading.Event()
    mutation_lock = threading.RLock()
    current: dict = {}
    api_execution=APIExecutors()
    lifespan_users=0
    lifespan_lock=threading.Lock()
    configured_transcription=bool(read_key())

    def redact(text):
        key = read_key()
        if key:
            text = text.replace(key, '[hidden]')
        return text.replace(str(REPO), '[app]').replace(str(store.root), '[data]')

    def output_dir(job):
        base = store.project_dir(job['project']) / 'edit' / 'jobs' / job['id']
        rows = store.query('SELECT attempt FROM execution_job_results WHERE job_id=?',(job['id'],))
        return base/rows[0]['attempt'] if rows else base

    def output_file(job,name):
        relative=str((output_dir(job)/name).relative_to(store.project_dir(job['project'])))
        return store.file(job['project'],relative)

    @asynccontextmanager
    async def lifespan(app):
        # API replicas never own task processes and never reset running jobs.
        # AgentHost handles both manual jobs and Agent turns through task leases.
        nonlocal lifespan_users
        with lifespan_lock:lifespan_users+=1
        from .api_maintenance import metrics,cleanup
        background=[asyncio.create_task(metrics(api_execution)),asyncio.create_task(cleanup(store,api_execution))]
        try:yield
        finally:
            for task in background:task.cancel()
            await asyncio.gather(*background,return_exceptions=True)
            with lifespan_lock:
                lifespan_users-=1
                if not lifespan_users:api_execution.close()

    app = FastAPI(title='updream-AI 编辑', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.router.route_class=route_class(api_execution)
    app.state.api_execution=api_execution

    def capacity_response(exc):
        from .telemetry import emit,context
        emit('api.pool.unavailable','WARNING',operation=exc.pool,reason=exc.reason)
        uncertain=exc.started or exc.prior_work
        message=('请求暂时无法继续，部分操作可能已完成，请先刷新页面确认状态。' if uncertain
                 else '当前请求较多，尚未开始处理，请稍后重试。')
        return JSONResponse({'detail':message,'code':'API_'+exc.reason.upper(),
                             'dependency':exc.pool,'outcome':'unknown' if uncertain else 'not_started',
                             'request_id':context().get('request_id')},status_code=503,headers={'Retry-After':'1'})

    @app.exception_handler(APICapacityError)
    async def api_unavailable(request,exc):return capacity_response(exc)

    @app.exception_handler(APIClientDisconnected)
    async def client_disconnected(request,exc):
        return JSONResponse({'code':'API_CLIENT_DISCONNECTED','outcome':'unknown'},status_code=499)

    from psycopg import OperationalError as DatabaseUnavailable
    @app.exception_handler(DatabaseUnavailable)
    async def database_unavailable(request,exc):
        from .telemetry import emit,context
        emit('api.database_unavailable','WARNING',exception_type=type(exc).__name__)
        return JSONResponse({'detail':'数据库暂不可用，请刷新确认状态后再操作。','code':'API_DATABASE_UNAVAILABLE',
                             'outcome':'unknown','request_id':context().get('request_id')},
                            status_code=503,headers={'Retry-After':'1'})
    from .media_isolation import MediaDecoderUnavailable
    @app.exception_handler(MediaDecoderUnavailable)
    async def decoder_unavailable(request,exc):
        from .telemetry import emit
        emit('media.environment_unavailable','ERROR',exc=exc)
        return JSONResponse({'detail':'媒体解码环境暂不可用，请凭查询编号排查。'},status_code=503)

    app.state.store = store
    app.state.wake = wake

    @app.middleware('http')
    async def browser_session(request: Request, call_next):
        # Mounted routes may change scope.root_path while serving the body.
        cookie_path=(request.scope.get('root_path','').rstrip('/') or '')+'/'
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': '不接受跨站操作。'}, status_code=403)
        cookie=request.cookies.get(COOKIE)
        if request.scope['path'].removeprefix(request.scope.get('root_path',''))=='/api/health':
            owner,token,new_session='',None,False
        else:
            try:
                if sessions.legacy_enabled and re.fullmatch(r'[a-f0-9]{64}',cookie or ''):
                    owner,token,new_session=await api_execution.run('auth',sessions.resolve,cookie)
                else:owner,token,new_session=sessions.resolve(cookie)
            except APICapacityError as exc:return capacity_response(exc)
            except DatabaseUnavailable as exc:return await database_unavailable(request,exc)
        request.state.owner = owner
        response = await call_next(request)
        if new_session:
            response.set_cookie(COOKIE, token, httponly=True, samesite='lax',
                                secure=request.url.scheme == 'https', max_age=365 * 86400,
                                path=cookie_path)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        if '/api/' in request.url.path:
            response.headers['Cache-Control'] = 'no-store'
        return response

    from .observation_http import install as install_observation_http
    install_observation_http(app)

    def own_project(project_id, request):
        rows = store.query('SELECT * FROM projects WHERE id=? AND owner=?',
                           (project_id, request.state.owner))
        if not rows:
            raise HTTPException(404, '项目不存在或不属于当前浏览器。')
        return rows[0]

    from .diagnostics_api import install as install_diagnostics
    install_diagnostics(app,store,own_project)

    def get_asset(project_id, asset_id):
        for asset in store.assets(project_id):
            if asset['id'] == asset_id:
                return asset
        raise HTTPException(404, '素材不存在。')

    def safe_job(project_id, job_id, request):
        own_project(project_id, request)
        job = store.job(job_id)
        if not job or job['project'] != project_id:
            raise HTTPException(404, '任务不存在。')
        return job

    def public_job(job):
        from .task_lifecycle import snapshot
        from .scheduling import snapshot as scheduling_snapshot
        execution=snapshot(store,job['id'])
        if execution:execution['scheduling']=scheduling_snapshot(store,job['id'])
        base = f"api/projects/{job['project']}/jobs/{job['id']}/files/"
        phases=store.query('SELECT state FROM manual_operation_runs WHERE job_id=?',(job['id'],))
        phase=phases[0]['state'] if phases and job['status'] not in ('failed','cancelled','succeeded') else job['status']
        return {key: job[key] for key in ('id', 'kind', 'status', 'created', 'updated', 'error')} | {
            'tool':job['payload'].get('tool'),'phase':phase,
            'source_asset':job['payload'].get('image_task',{}).get('asset'),
            'execution':execution,
            'artifacts': [{'name': name, 'url': base + name,
                           'poster_url':base+name+'/thumbnail' if (mimetypes.guess_type(name)[0] or '').startswith('video/') else None,
                           'type': mimetypes.guess_type(name)[0] or 'application/octet-stream'}
                          for name in job['artifacts']]}

    def page(filename, request, project_id=''):
        prefix=(request.scope.get('root_path','').rstrip('/') or '')+'/'
        content=(Path(__file__).parent/'static'/filename).read_text()
        content=content.replace('{{BASE}}',html.escape(prefix,quote=True)).replace('{{PROJECT}}',html.escape(project_id,quote=True))
        return HTMLResponse(content,headers={'Cache-Control':'no-cache'})

    @app.get('/')
    def index(request: Request):
        return page('home.html',request)

    @app.get('/tools/{project_id}')
    def tool_page(project_id: str, request: Request):
        own_project(project_id,request)
        return page('tools.html',request,project_id)

    @app.get('/images/{project_id}')
    def image_page(project_id: str, request: Request):
        own_project(project_id,request)
        return page('images.html',request,project_id)

    @app.get('/chat/{project_id}')
    def chat_page(project_id: str, request: Request):
        own_project(project_id,request)
        return page('dynamic.html',request,project_id)

    @app.get('/api/health')
    async def health():
        return {'status': 'ok', 'transcription_configured': configured_transcription,
                'max_upload_mb': int(os.environ.get('VIDEO_USE_MAX_UPLOAD_MB', '2048')),
                'max_projects': MAX_PROJECTS,
                'capabilities': ['transcribe', 'pack', 'timeline', 'analyze', 'review', 'render', 'edit']}

    @app.get('/api/projects')
    def projects(request: Request):
        return store.query("SELECT p.id,p.name,p.created,COALESCE(d.updated,p.created) AS updated,COALESCE(d.editor,'chat') AS editor FROM projects p LEFT JOIN project_drafts d ON d.project=p.id WHERE p.owner=? ORDER BY updated DESC",
                           (request.state.owner,))

    @app.post('/api/projects', status_code=201)
    def new_project(body: ProjectInput, request: Request):
        if not body.name.strip():
            raise HTTPException(422, '请输入项目名称。')
        with mutation_lock,store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count = store.query('SELECT count(*) AS n FROM projects WHERE owner=?', (request.state.owner,))[0]['n']
            if count >= MAX_PROJECTS:
                raise HTTPException(409, f'当前浏览器已保存 {count} 个项目，达到 {MAX_PROJECTS} 个上限。请在不再需要的项目中点击“删除项目”后重试。')
            project = store.add_project(request.state.owner, body.name.strip())
        return {k: project[k] for k in ('id', 'name', 'created')}

    @app.get('/api/projects/{project_id}')
    async def project_detail(project_id: str, request: Request):
        def read():
            project=own_project(project_id,request)
            names=None
            if store.shared_files:
                names=[Path(row['path']).name for row in store.query(
                    "SELECT f.path FROM artifact_files f JOIN artifact_objects o ON o.id=f.artifact AND o.project=f.project WHERE f.project=? AND f.path LIKE 'edit/transcripts/%' AND o.state IN ('staged','published')",(project_id,))]
            jobs=[public_job(store.job(row['id'])) for row in store.query(
                'SELECT id FROM jobs WHERE project=? ORDER BY created DESC',(project_id,))]
            return project,store.assets(project_id),jobs,store.draft(project_id),names
        project,records,jobs,draft,names=await api_execution.run('read',read)
        if names is None:
            names=await api_execution.run('storage',lambda:[p.name for p in (store.project_dir(project_id)/'edit/transcripts').glob('*.json') if p.is_file() and not p.is_symlink()])
        def response():
            import re
            from fastapi.responses import JSONResponse
            assets=[]
            for asset in records:
                pattern=re.compile(re.escape(asset['id'])+r'(?:\.track(\d+))?\.json')
                tracks=sorted({int(match[1] or 0) for name in names if (match:=pattern.fullmatch(name))})
                assets.append({'id':asset['id'],'name':asset['name'],**asset['metadata'],
                    'transcribed_tracks':tracks,
                    'poster_url':f"api/projects/{project_id}/assets/{asset['id']}/thumbnail" if asset['metadata']['kind'] in ('video','image') else None,
                    'url':f"api/projects/{project_id}/assets/{asset['id']}/file"})
            return JSONResponse({'id':project_id,'name':project['name'],'assets':assets,'jobs':jobs,'draft':draft})
        return await api_execution.run('read',response)

    @app.put('/api/projects/{project_id}/draft')
    def save_draft(project_id: str, body: DraftInput, request: Request):
        with mutation_lock:
            own_project(project_id,request)
            if body.preset:
                ids={a['id'] for a in store.assets(project_id)}
                refs=set(body.preset.assets)|{s.asset for s in body.preset.segments}|{s.asset for s in body.preset.overlays}
                if body.preset.subtitle_asset:refs.add(body.preset.subtitle_asset)
                if body.preset.kind=='edit':
                    try:refs.update(edit_asset_ids(body.preset.edit_plan))
                    except (PlanError,TypeError) as exc:raise HTTPException(422,str(exc)) from exc
                if not refs.issubset(ids):raise HTTPException(422,'草稿包含当前项目以外的素材。')
            store.save_draft(project_id,body.preset.model_dump() if body.preset else None,body.prompt,body.editor)
        return {'saved':True}

    def delete_project_data(project_id):
        from .task_leases import CLOCK
        tasks='SELECT id FROM execution_subjects WHERE project=? OR project=?'
        if store.query("SELECT attempt FROM execution_cleanup WHERE state<>'complete' AND task_id IN ("+tasks+")",(project_id,project_id)):
            raise HTTPException(409,'执行资源尚未回收完成，请稍后删除。')
        if store.query("SELECT id FROM execution_external_cleanup WHERE state IN ('pending','running') AND task_id IN ("+tasks+")",(project_id,project_id)):
            raise HTTPException(409,'正在确认外部任务停止，请等待收尾完成后删除。')
        if store.query('SELECT l.run_id FROM execution_leases l JOIN (SELECT id,project FROM execution_subjects) t ON t.id=l.run_id WHERE t.project=? AND l.active=1 AND l.expires>'+CLOCK,(project_id,)):
            raise HTTPException(409,'任务仍在退出和清理，请稍后删除。')
        active = store.job(current['id']) if current.get('id') else None
        if store.query("SELECT id FROM execution_subjects WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')", (project_id,)):
            raise HTTPException(409, '请先停止智能体任务，等待停止完成后再删除。')
        if (active and active['project'] == project_id) or store.query("SELECT id FROM jobs WHERE project=? AND status IN ('queued','running')", (project_id,)):
            raise HTTPException(409, '请等待或取消运行中的任务后再删除。')
        store.execute('DELETE FROM assets WHERE project=?', (project_id,))
        store.execute('DELETE FROM project_drafts WHERE project=?', (project_id,))
        store.execute('DELETE FROM project_demo_context WHERE project=?', (project_id,))
        store.execute('DELETE FROM image_documents WHERE project=?', (project_id,))
        store.execute('DELETE FROM image_versions WHERE project=?', (project_id,))
        for table in ('edit_notes','edit_branches','edit_chapters','brand_templates'):store.execute(f'DELETE FROM {table} WHERE project=?',(project_id,))
        for table in ('observation_audit','observation_tasks','execution_limits','execution_time_slices','execution_cleanup','execution_security_events','execution_external_cleanup','execution_waits','execution_cancel_requests','execution_phase_metrics'):
            store.execute(f'DELETE FROM {table} WHERE task_id IN ('+tasks+')',(project_id,project_id))
        for table in ('workflow_step_versions','workflow_quality_events','workflow_diagnostics','workflow_agent_reviews','workflow_user_requests','workflow_provider_calls','workflow_reuse_receipts'):
            store.execute(f'DELETE FROM {table} WHERE workflow IN (SELECT id FROM agent_workflows WHERE project=?)',(project_id,))
        for table in ('execution_checkpoint_versions','execution_session_profiles','execution_native_descriptors','agent_project_profiles','agent_native_sessions','agent_harness_switches'):
            store.execute(f'DELETE FROM {table} WHERE project=?',(project_id,))
        for table in ('execution_leases','execution_attempts','execution_budgets','execution_provider_calls'):
            store.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT id FROM jobs WHERE project=?)',(project_id,))
        store.execute('DELETE FROM workflow_provider_calls WHERE workflow IN ('+tasks+')',(project_id,project_id))
        store.execute('DELETE FROM manual_operation_runs WHERE job_id IN (SELECT id FROM jobs WHERE project=?)',(project_id,))
        store.execute('DELETE FROM execution_resource_claims WHERE task_id IN ('+tasks+')',(project_id,project_id))
        store.execute('DELETE FROM workflow_transcript_cache WHERE project=?',(project_id,))
        store.execute('DELETE FROM workflow_control WHERE run_id IN (SELECT id FROM agent_turns WHERE project=?)',(project_id,))
        store.execute('DELETE FROM execution_intents WHERE run_id IN (SELECT id FROM agent_turns WHERE project=?)',(project_id,))
        store.execute('DELETE FROM execution_job_results WHERE job_id IN (SELECT id FROM jobs WHERE project=?)',(project_id,))
        for table in ('agent_messages','agent_render_observations','execution_activities','agent_routes','agent_run_profiles','agent_run_role_profiles','agent_attempt_profiles','agent_context_events'):
            store.execute(f'DELETE FROM {table} WHERE task_id IN ('+tasks+')',(project_id,project_id))
        store.execute('DELETE FROM usage_calls WHERE task_id IN ('+tasks+')',(project_id,project_id))
        store.execute('DELETE FROM usage_tasks WHERE project_id=?',(project_id,))
        store.execute('DELETE FROM plan_review_retries WHERE run_id IN (SELECT id FROM agent_turns WHERE project=?)',(project_id,))
        store.execute('DELETE FROM plan_readiness WHERE project=?',(project_id,))
        store.execute('DELETE FROM agent_events WHERE run_id IN ('+tasks+')', (project_id,project_id))
        store.execute('DELETE FROM jobs WHERE project=?', (project_id,))
        store.execute('DELETE FROM agent_steps WHERE workflow IN (SELECT id FROM agent_workflows WHERE project=?)', (project_id,))
        store.execute('DELETE FROM agent_workflows WHERE project=?', (project_id,))
        for table in ('native_task_sessions','specialist_attempt_profiles','run_authorizations','run_inputs','run_input_drain','run_task_replacements','workflow_task_revisions','workflow_plan_tasks','workflow_reconcile_failures','control_jobs','workflow_dependencies','run_tasks','workflow_plan_drafts','planner_runs','task_results','workflow_commands',
                      'workflow_decisions','workflow_reviews','workflow_graph_events','workflow_model_reservations','workflow_inspections'):
            store.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE project_id=?)',(project_id,))
        store.execute('DELETE FROM workflow_agent_outputs WHERE run_id IN (SELECT id FROM runs WHERE project_id=?)',(project_id,))
        store.execute('DELETE FROM workflow_provider_calls WHERE workflow IN (SELECT id FROM runs WHERE project_id=?)',(project_id,))
        store.execute('DELETE FROM workflow_diagnostics WHERE workflow IN (SELECT id FROM runs WHERE project_id=?)',(project_id,))
        store.execute('DELETE FROM workflow_start_requests WHERE project_id=?',(project_id,))
        store.execute('DELETE FROM workflow_asset_reuse WHERE project_id=?',(project_id,))
        for table in ('execution_leases','execution_attempts','execution_budgets','execution_provider_calls'):
            store.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE project_id=?)',(project_id,))
        store.execute('DELETE FROM session_messages WHERE session_id IN (SELECT id FROM sessions WHERE project_id=?)',(project_id,))
        store.execute('DELETE FROM sessions WHERE project_id=?',(project_id,))
        store.execute('DELETE FROM runs WHERE project_id=?',(project_id,))
        for table in ('execution_leases','execution_attempts','execution_budgets','execution_provider_calls'):
            store.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT id FROM agent_turns WHERE project=?)',(project_id,))
        store.execute('DELETE FROM execution_checkpoints WHERE project=?',(project_id,))
        store.execute('DELETE FROM execution_step_checkpoints WHERE project=?',(project_id,))
        store.artifacts.release_prefix(project_id)
        store.execute('DELETE FROM agent_turns WHERE project=?', (project_id,))
        store.execute('DELETE FROM agent_sessions WHERE project=?', (project_id,))
        store.execute('DELETE FROM projects WHERE id=?', (project_id,))

    async def cleanup_deleted(project_ids):
        def remove():
            for project_id in project_ids:
                for path in (store.project_dir(project_id),store.root/'cache/objects'/project_id):
                    try:shutil.rmtree(path)
                    except FileNotFoundError:pass
        try:
            async with asyncio.timeout(2):await api_execution.run('cleanup',remove)
            return False
        except (APICapacityError,TimeoutError,OSError) as exc:
            from .telemetry import emit
            emit('api.cleanup_deferred','WARNING',reason='deleted_project',exception_type=type(exc).__name__)
            return True

    @app.post('/api/projects/batch-delete')
    @admission('delete')
    async def batch_delete_projects(body: BatchDeleteInput, request: Request):
        ids=list(dict.fromkeys(body.ids))
        def remove_metadata():
            with mutation_lock,store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for project_id in ids:own_project(project_id,request)
                deleted=[];blocked=[]
                for project_id in ids:
                    try:delete_project_data(project_id);deleted.append(project_id)
                    except HTTPException as exc:
                        if exc.status_code!=409:raise
                        blocked.append({'id':project_id,'detail':exc.detail})
            return {'deleted':deleted,'blocked':blocked}
        result=await api_execution.run('write',remove_metadata)
        result['cleanup_pending']=await cleanup_deleted(result['deleted']) if result['deleted'] else False
        return result

    @app.delete('/api/projects/{project_id}')
    @admission('delete')
    async def delete_project(project_id: str, request: Request):
        def remove_metadata():
            with mutation_lock,store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                own_project(project_id,request);delete_project_data(project_id)
        await api_execution.run('write',remove_metadata)
        return {'deleted':True,'cleanup_pending':await cleanup_deleted([project_id])}

    @app.post('/api/projects/{project_id}/assets', status_code=201)
    @admission('upload')
    async def upload(project_id: str, request: Request, file: UploadFile = File(...)):
        from .api_staging import Staging
        def authorize_upload():
            own_project(project_id, request)
            if len(store.assets(project_id)) >= 50:
                raise HTTPException(409, '每个项目最多 50 个素材。')
        await api_execution.run('read',authorize_upload)
        original=(file.filename or 'video').replace('\\','/').split('/')[-1][:180]
        suffix=Path(original).suffix.lower()
        if suffix not in VIDEO_EXTS|AUDIO_EXTS|IMAGE_EXTS|DOCUMENT_EXTS|{'.srt'}:
            raise HTTPException(415,'支持视频、音频、图片、SRT、PDF、TXT、Markdown、JSON、CSV 和 Office 文档。')
        limit=int(os.environ.get('VIDEO_USE_MAX_UPLOAD_MB','2048'))*1024**2
        asset_id=uuid.uuid4().hex;filename=asset_id+suffix;stage=Staging(store.root);path=stage.path/filename
        def copy_upload():
            if shutil.disk_usage(store.root).free < 2 * 1024**3:
                raise HTTPException(507,'剩余存储空间不足，请删除旧项目后再上传。')
            return stage.copy(file.file,filename,limit)
        try:
            size=await api_execution.run('storage',copy_upload)
            if suffix=='.srt':
                def subtitle():
                    if size>5*1024**2:raise ValueError('字幕文件不能超过 5 MB。')
                    if '-->' not in path.read_text(encoding='utf-8-sig'):raise ValueError('字幕文件缺少 SRT 时间戳。')
                    return {'kind':'subtitle','size':size}
                metadata=await api_execution.run('storage',subtitle)
            elif suffix in IMAGE_EXTS:metadata=await api_execution.run('media',inspect_image,path)
            elif suffix in DOCUMENT_EXTS:metadata=await api_execution.run('storage',inspect_document,path,suffix)
            else:metadata=await api_execution.run('media',inspect_media,path,allow_audio=suffix in AUDIO_EXTS)
            uploaded=await api_execution.run('storage',store.artifacts.upload,project_id,path,check=stage.check) if store.shared_files else None
            def publish_upload():
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    authorize_upload();stage.check()
                    store.check_storage(project_id,size)
                    stage.move(filename,store.project_dir(project_id)/'sources'/filename)
                    store.add_asset(project_id, asset_id, original, filename, metadata,artifact=uploaded)
                    stage.check()
                return {'id':asset_id,'name':original,**metadata}
            return await api_execution.run('write',stage.commit,publish_upload)
        except OverflowError as exc:raise HTTPException(413,'文件超过上传大小限制。') from exc
        except (ValueError,UnicodeError,subprocess.TimeoutExpired) as exc:
            await api_execution.run('read',own_project,project_id,request)
            raise HTTPException(422,str(exc)) from exc
        finally:await stage.finish(api_execution)

    @app.get('/api/projects/{project_id}/assets/{asset_id}/file')
    async def source_file(project_id: str, asset_id: str, request: Request, download: bool = False):
        def lookup():own_project(project_id,request);return get_asset(project_id,asset_id)
        asset=await api_execution.run('read',lookup)
        path=await api_execution.run('storage',store.file,project_id,'sources/'+asset['filename'])
        return FileResponse(path,filename=asset['name'] if download else None)

    @app.get('/api/projects/{project_id}/assets/{asset_id}/thumbnail')
    async def source_thumbnail(project_id: str, asset_id: str, request: Request):
        def lookup():own_project(project_id,request);return get_asset(project_id,asset_id)
        asset=await api_execution.run('read',lookup)
        if asset['metadata']['kind'] not in ('image','video'):raise HTTPException(404,'无图片封面。')
        source=await api_execution.run('storage',store.file,project_id,'sources/'+asset['filename'])
        if asset['metadata']['kind']=='image':
            return FileResponse(source,media_type=asset['metadata'].get('mime','image/png'))
        try:
            path=await api_execution.run('media',thumbnail,source,store.project_dir(project_id)/'edit/cache/posters')
        except (ValueError,subprocess.TimeoutExpired) as exc:raise HTTPException(422,'视频封面暂不可用。') from exc
        return FileResponse(path,media_type='image/jpeg')

    @app.post('/api/projects/{project_id}/jobs', status_code=202)
    def submit_job(project_id: str, body: JobInput, request: Request):
        with mutation_lock, store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            own_project(project_id, request)
            try:store.admit_tasks(project_id)
            except ValueError as exc:raise HTTPException(429,str(exc)) from exc
            if store.query("SELECT id FROM execution_subjects WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')", (project_id,)):
                raise HTTPException(409, '智能体正在操作此项目，请等待或停止后再提交手动任务。')
            active = store.query("SELECT id FROM jobs WHERE status IN ('queued','running')")
            if len(active) >= 20:
                raise HTTPException(429, '任务队列已满，请稍后重试。')
            assets = {asset['id']: asset for asset in store.assets(project_id)}
            def video(asset_id, track=None):
                kinds=('video','audio') if body.kind=='transcribe' else ('video',)
                if asset_id not in assets or assets[asset_id]['metadata']['kind'] not in kinds:
                    raise HTTPException(422, '请选择当前项目中适用的音视频素材。')
                meta = assets[asset_id]['metadata']
                if track is not None and track >= max(1, len(meta['audio_tracks'])):
                    raise HTTPException(422, '所选音轨不存在。')
                return meta
            def valid_range(start, end, meta):
                if end <= start or end > meta['duration'] + 0.02 or end - start < 0.05:
                    raise HTTPException(422, '片段起止时间无效或超过视频时长。')
            if body.kind in {'transcribe', 'timeline', 'analyze', 'review'}:
                if not body.assets or (body.kind != 'transcribe' and len(body.assets) != 1):
                    raise HTTPException(422, '请选择要处理的视频素材。')
                for asset_id in body.assets:
                    meta = video(asset_id, body.audio_track)
                    if body.kind == 'transcribe' and not meta['audio_tracks']:
                        raise HTTPException(422, '无声素材无法转写。')
                if body.kind != 'transcribe':
                    valid_range(body.start, body.end, meta)
                if body.kind == 'transcribe' and not read_key():
                    raise HTTPException(409, '服务器尚未配置 ElevenLabs 密钥。')
            if body.kind == 'render':
                if not body.segments:
                    raise HTTPException(422, '请添加至少一个剪辑片段。')
                ratio = None
                for seg in body.segments:
                    meta = video(seg.asset, seg.audio_track)
                    valid_range(seg.start, seg.end, meta)
                    current_ratio = meta['width'] / meta['height']
                    if ratio is not None and abs(current_ratio - ratio) > 0.02:
                        raise HTTPException(422, '当前合成需要素材画幅一致，请选择相同横竖比例的片段。')
                    ratio = current_ratio
                    if body.subtitles == 'transcript':
                        suffix = f'.track{seg.audio_track}' if seg.audio_track else ''
                        if not store.has_file(project_id,f'edit/transcripts/{seg.asset}{suffix}.json'):
                            raise HTTPException(422, '生成字幕前，请先转写每个片段使用的音轨。')
                total = sum(s.end - s.start for s in body.segments)
                for overlay in body.overlays:
                    meta = video(overlay.asset)
                    if overlay.duration > meta['duration'] + 0.02 or overlay.start + overlay.duration > total + 0.02:
                        raise HTTPException(422, '叠加时长超过素材或成片时长。')
                if body.subtitles == 'upload':
                    if body.subtitle_asset not in assets or assets[body.subtitle_asset]['metadata']['kind'] != 'subtitle':
                        raise HTTPException(422, '请选择已上传的 SRT 字幕。')
            if body.kind=='edit':
                try:bind_plan(body.edit_plan,assets,store.project_dir(project_id))
                except (PlanError,ValueError,TypeError,KeyError,OSError) as exc:
                    raise HTTPException(422,'请检查当前工具参数：'+str(exc)) from exc
            job = store.add_job(project_id, body.kind, body.model_dump())
            wake.set()
        return public_job(job)

    @app.post('/api/projects/{project_id}/jobs/{job_id}/cancel')
    @lane('critical')
    def cancel(project_id: str, job_id: str, request: Request):
        job = safe_job(project_id, job_id, request)
        from .task_lifecycle import cancel_task
        return cancel_task(store,job_id)

    @app.get('/api/projects/{project_id}/jobs/{job_id}/log')
    async def job_log(project_id: str, job_id: str, request: Request):
        job=await api_execution.run('read',safe_job,project_id,job_id,request)
        def read_log():
            try:path=output_file(job,'job.log')
            except FileNotFoundError:return {'text':'等待处理…'}
            if not path.exists():return {'text':'等待处理…'}
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-10000))
                return {'text':redact(stream.read().decode(errors='replace'))}
        return await api_execution.run('storage',read_log)

    @app.get('/api/projects/{project_id}/jobs/{job_id}/files/{name}')
    async def result_file(project_id: str, job_id: str, name: str, request: Request, download: bool = False):
        job=await api_execution.run('read',safe_job,project_id,job_id,request)
        if name not in job['artifacts'] or job['status'] != 'succeeded':
            raise HTTPException(404, '结果不存在。')
        path=await api_execution.run('storage',output_file,job,name)
        return FileResponse(path,filename=name if download else None)

    @app.get('/api/projects/{project_id}/jobs/{job_id}/files/{name}/thumbnail')
    async def job_thumbnail(project_id: str, job_id: str, name: str, request: Request):
        job=await api_execution.run('read',safe_job,project_id,job_id,request)
        if name not in job['artifacts'] or job['status']!='succeeded' or not (mimetypes.guess_type(name)[0] or '').startswith('video/'):
            raise HTTPException(404,'无视频封面。')
        source=await api_execution.run('storage',output_file,job,name)
        try:path=await api_execution.run('media',thumbnail,source,store.project_dir(project_id)/'edit/cache/posters')
        except (ValueError,subprocess.TimeoutExpired) as exc:raise HTTPException(422,'视频封面暂不可用。') from exc
        return FileResponse(path,media_type='image/jpeg')

    # Demo previews are intentionally public. User projects are isolated by session.
    from .demo_library import DemoLibrary,conversation_prompt
    demo_library=DemoLibrary(store)
    def catalog():return demo_library.snapshot().catalog

    @app.get('/api/demos')
    @lane('storage')
    def demos():
        return catalog()

    @app.get('/api/demos/{demo_id}/files/{name}')
    @lane('storage')
    def demo_file(demo_id: str, name: str):
        edition=demo_library.snapshot()
        demo = next((d for d in edition.catalog if d['id'] == demo_id), None)
        if not demo or name not in demo['files']:
            raise HTTPException(404, '示例文件不存在。')
        return FileResponse(edition.file(demo_id+'/'+name))

    @app.get('/api/demos/{demo_id}/sources/{key}')
    @lane('storage')
    def demo_source(demo_id: str, key: str):
        edition=demo_library.snapshot()
        demo=next((d for d in edition.catalog if d['id']==demo_id),None)
        source=next((s for s in demo.get('sources',[]) if s.get('key')==key),None) if demo else None
        if not source or Path(source.get('file','')).name!=source.get('file'):
            raise HTTPException(404,'示例素材不存在。')
        path=edition.file('sources/'+source['file'])
        if not path.is_file() or path.is_symlink():raise HTTPException(404,'示例素材不存在。')
        return FileResponse(path)

    @app.post('/api/demos/{demo_id}/load', status_code=201)
    @admission('demo')
    async def load_demo(demo_id: str, request: Request):
        from .api_execution import mark_request_reversible
        mark_request_reversible()
        from .api_staging import Staging
        edition=await api_execution.run('storage',demo_library.snapshot)
        demo=next((d for d in edition.catalog if d['id']==demo_id),None)
        if not demo:raise HTTPException(404,'示例不存在。')
        project=await api_execution.run('write',new_project,ProjectInput(name='示例 · '+demo['title']),request)
        project_id=project['id'];folder=store.project_dir(project_id);stage=Staging(store.root)
        mapping={};assets=[];auxiliary=[]
        def discard_empty_project():
            # Cancellation may race a still-running publisher. Wait for its
            # private lock and never remove a committed import or user work.
            with stage.lock:
                if stage.committed:return
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    rows=store.query('SELECT name FROM projects WHERE id=?',(project_id,))
                    if not rows or rows[0]['name']!=project['name']:return
                    tables=('assets','project_drafts','project_demo_context','image_documents','image_versions',
                            'jobs','agent_turns','edit_notes','edit_branches','edit_chapters','brand_templates',
                            'execution_checkpoints','execution_checkpoint_versions','artifact_files')
                    if any(store.query(f'SELECT project FROM {table} WHERE project=? LIMIT 1',(project_id,)) for table in tables):return
                    if store.query('SELECT id FROM sessions WHERE project_id=? LIMIT 1',(project_id,)):return
                    delete_project_data(project_id)
                for directory in (folder,store.root/'cache/objects'/project_id):
                    shutil.rmtree(directory,ignore_errors=True)
        limit=int(os.environ.get('VIDEO_USE_MAX_UPLOAD_MB','2048'))*1024**2
        def copy_source(source,name):
            with source.open('rb') as stream:return stage.copy(stream,name,limit)
        async def prepare_asset(relative,name,*,transcript_for=None):
            source=await api_execution.run('storage',edition.file,relative)
            asset_id=uuid.uuid4().hex;filename=asset_id+source.suffix
            await api_execution.run('storage',copy_source,source,filename)
            path=stage.path/filename
            if transcript_for is not None:
                def inspect_transcript():
                    if path.stat().st_size>2*1024**2:raise ValueError('示例转写文件过大')
                    value=json.loads(path.read_text())
                    if not isinstance(value,dict) or not isinstance(value.get('words'),list):raise ValueError('示例转写缺少逐词数组')
                    return {'kind':'json','size':path.stat().st_size,'format':'transcript',**transcript_for}
                metadata=await api_execution.run('storage',inspect_transcript)
            elif source.suffix.lower() in IMAGE_EXTS:metadata=await api_execution.run('media',inspect_image,path)
            else:metadata=await api_execution.run('media',inspect_media,path,allow_audio=source.suffix.lower() in AUDIO_EXTS)
            artifact=await api_execution.run('storage',store.artifacts.upload,project_id,path,check=stage.check) if store.shared_files else None
            assets.append({'id':asset_id,'name':name,'filename':filename,'metadata':metadata,'artifact':artifact})
            return asset_id
        async def prepare_auxiliary(logical,relative=None,data=None):
            filename=uuid.uuid4().hex+Path(logical).suffix
            if relative is not None:
                source=await api_execution.run('storage',edition.file,relative)
                await api_execution.run('storage',copy_source,source,filename)
            else:await api_execution.run('storage',stage.write,filename,data)
            artifact=await api_execution.run('storage',store.artifacts.upload,project_id,stage.path/filename,check=stage.check) if store.shared_files else None
            auxiliary.append({'logical':logical,'filename':filename,'artifact':artifact})
        try:
            for item in demo['sources']:
                asset_id=await prepare_asset('sources/'+item['file'],item['name']);mapping[item['key']]=asset_id
                for track,transcript in item.get('transcripts',{}).items():
                    suffix=f'.track{track}' if int(track) else ''
                    await prepare_auxiliary(f'edit/transcripts/{asset_id}{suffix}.json','sources/'+transcript)
                    await prepare_asset('sources/'+transcript,item['name']+f' · 已有转写（音轨 {int(track)+1}）.json',
                                        transcript_for={'source_asset':asset_id,'audio_track':int(track)})
            if demo_id=='video-version-compare':
                await prepare_asset(demo_id+'/'+demo['result'],'版本 2 · 暖调成片')
            preset=json.loads(json.dumps(demo['preset']))
            preset['assets']=[mapping[key] for key in preset.get('assets',[])]
            for seg in preset.get('segments',[]):seg['asset']=mapping[seg['asset']]
            for overlay in preset.get('overlays',[]):overlay['asset']=mapping[overlay['asset']]
            draft_preset=preset if preset.get('kind')!='agent' else {'kind':'render','assets':list(mapping.values()),
                'segments':[{'asset':a['id'],'start':0,'end':min(10,a['metadata']['duration'])} for a in assets if a['metadata']['kind']=='video']}
            if demo_id=='video-quality-review':
                video_asset=next((a for a in assets if a['metadata']['kind']=='video'),None)
                if video_asset:draft_preset={'kind':'review','tool':'review','assets':[video_asset['id']],'start':0,'end':video_asset['metadata']['duration']}
            elif demo_id in DEMO_TO_TOOL:draft_preset['tool']=DEMO_TO_TOOL[demo_id]
            if demo.get('renderer')=='edit_media':
                def read_plan():return json.loads(edition.file(demo_id+'/plan.json').read_text())
                original_plan=await api_execution.run('storage',read_plan)
                plan=json.loads(json.dumps(original_plan));asset_rows={a['id']:a for a in assets}
                plan['sources']={key:'sources/'+asset_rows[mapping[key]]['filename'] for key in plan['sources']}
                if plan.get('transcripts'):plan['transcripts']={key:'transcripts/'+mapping[key]+'.json' for key in plan['transcripts']}
                await prepare_auxiliary('agent/workspace/edit/demo-plan.json',data=json.dumps(plan,ensure_ascii=False,indent=2).encode())
                browser_plan=json.loads(json.dumps(original_plan));browser_plan.pop('sources',None);browser_plan.pop('transcripts',None)
                for clip in browser_plan.get('clips',[]):
                    source=clip.pop('source');clip['asset']=mapping[source]
                    clip.setdefault('start',0);clip.setdefault('end',asset_rows[mapping[source]]['metadata']['duration'])
                audio=browser_plan.get('audio',{})
                if audio.get('source'):audio['asset']=mapping[audio.pop('source')]
                draft_preset={'kind':'edit','tool':DEMO_TO_TOOL.get(demo_id,'render'),'edit_plan':browser_plan}
            draft=JobInput(**draft_preset).model_dump()
            editor='images' if demo.get('media')=='image' else 'chat' if demo.get('workflow')=='agent' else 'tools'
            prompt=conversation_prompt(demo)
            def publish():
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE');own_project(project_id,request)
                    changed=any(store.query(f'SELECT project FROM {table} WHERE project=? LIMIT 1',(project_id,)) for table in
                                ('assets','project_drafts','image_documents','jobs','agent_turns'))
                    if changed:raise HTTPException(409,'导入期间项目已被修改，未覆盖已有内容。')
                    if len(assets)>50:raise HTTPException(409,'每个项目最多 50 个素材。')
                    store.check_storage(project_id,sum(a['metadata']['size'] for a in assets))
                    for asset in assets:
                        stage.move(asset['filename'],folder/'sources'/asset['filename'])
                        store.add_asset(project_id,asset['id'],asset['name'],asset['filename'],asset['metadata'],artifact=asset['artifact'])
                    for item in auxiliary:
                        stage.move(item['filename'],folder/item['logical'])
                        if item['artifact']:store.artifacts.publish(project_id,item['logical'],item['artifact']['id'],expected_revision=0)
                    store.save_draft(project_id,draft,prompt,editor);store.set_demo_context(project_id,demo_id)
                    stage.check()
                return {'project':project,'preset':preset,'prompt':prompt}
            return await api_execution.run('write',stage.commit,publish)
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc
        except OverflowError as exc:raise HTTPException(413,'示例素材超过大小限制。') from exc
        except subprocess.TimeoutExpired as exc:raise HTTPException(504,'示例素材检查超时。') from exc
        finally:
            await stage.finish(api_execution)
            import anyio
            with anyio.CancelScope(shield=True):
                try:
                    async with asyncio.timeout(2):await api_execution.run('cleanup',discard_empty_project)
                except Exception as exc:
                    from .telemetry import emit
                    emit('api.cleanup_deferred','WARNING',reason='demo_project',exception_type=type(exc).__name__)

    image_api.register(app,store,own_project,mutation_lock,wake,public_job)
    edit_feedback.register(app,store,own_project,mutation_lock,wake)
    agent_api.register(app, store, own_project, mutation_lock)
    from .workflow_api import register as register_workflows
    register_workflows(app,store,own_project)
    from . import usage_api
    usage_api.register(app,store,own_project)
    app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
    return app


app = create_app()
