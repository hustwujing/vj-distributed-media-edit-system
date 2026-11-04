"""Owned canvas revisions, image tasks, and import of already-authorized results."""
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import HTTPException,Request
from pydantic import BaseModel,Field,ConfigDict
from PIL import Image,ImageOps

from .image_document import Document,ImageTask,check_document,check_task
from . import agent_store
from .api_execution import admission
from .image_canvas import current,selected,check_locks,write_version,patch_layer,CanvasContext

class LayerLock(CanvasContext):
    locked:bool

class ApplyAsset(CanvasContext):
    asset:str

class ImportResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    job:str|None=None
    turn:str|None=None
    names:list[str]=Field(min_length=1,max_length=20)

class ImageBatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    assets:list[str]=Field(min_length=1,max_length=20)
    width:int=Field(ge=16,le=4096);height:int=Field(ge=16,le=4096)
    fit:str=Field(default='contain',pattern=r'^(contain|cover)$')
    background:str=Field(default='#ffffff',pattern=r'^#[0-9a-fA-F]{6}$')
    format:str=Field(default='png',pattern=r'^(png|jpg|webp)$')
    quality:int=Field(default=90,ge=40,le=100)
    watermark:str=Field(default='',max_length=100)


def initialize(store):
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS image_documents(project TEXT PRIMARY KEY,revision INTEGER NOT NULL,document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS image_versions(project TEXT NOT NULL,revision INTEGER NOT NULL,document TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,revision));''')
        if 'change' not in {r['name'] for r in db.execute('PRAGMA table_info(image_versions)')}:
            db.execute("ALTER TABLE image_versions ADD COLUMN change TEXT NOT NULL DEFAULT '{}'")


def register(app,store,own_project,lock,wake,public_job):
    api_execution=app.state.api_execution
    initialize(store)

    @app.get('/api/projects/{project}/image-document')
    def document(project:str,request:Request):
        own_project(project,request)
        rows=store.query('SELECT revision,document FROM image_documents WHERE project=?',(project,))
        value=json.loads(rows[0]['document']) if rows else Document().model_dump()
        versions=store.query('SELECT revision,created,change FROM image_versions WHERE project=? ORDER BY revision DESC LIMIT 100',(project,))
        for version in versions:version['change']=json.loads(version['change'])
        return {'document':value,'versions':versions}

    @app.get('/api/projects/{project}/image-document/{revision}')
    def version(project:str,revision:int,request:Request):
        own_project(project,request);rows=store.query('SELECT document FROM image_versions WHERE project=? AND revision=?',(project,revision))
        if not rows:raise HTTPException(404,'版本不存在')
        return json.loads(rows[0]['document'])

    @app.put('/api/projects/{project}/image-document')
    def save(project:str,body:Document,request:Request):
        with lock:
            own_project(project,request)
            try:check_document(body,{a['id']:a for a in store.assets(project)})
            except ValueError as exc:raise HTTPException(422,str(exc)) from exc
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                before=current(db,project,body.revision)
                check_locks(before,body)
                write_version(db,project,body,{'kind':'manual'})
            return {'revision':body.revision}

    @app.post('/api/projects/{project}/image-layer-lock')
    def layer_lock(project:str,body:LayerLock,request:Request):
        with lock:
            own_project(project,request)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE');doc=current(db,project,body.revision)
                layer=selected(doc,body.layer_id)
                if layer.locked!=body.locked:
                    layer.locked=body.locked
                    write_version(db,project,doc,{'kind':'lock' if body.locked else 'unlock','layer_id':layer.id,'name':layer.name})
            return {'document':doc.model_dump()}

    @app.post('/api/projects/{project}/image-apply')
    def apply_asset(project:str,body:ApplyAsset,request:Request):
        with lock:
            own_project(project,request)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE');doc=current(db,project,body.revision)
                if selected(doc,body.layer_id).kind!='image':raise HTTPException(422,'请选择要替换的图片图层')
                after=patch_layer(doc,body.layer_id,{'asset':body.asset},{a['id']:a for a in store.assets(project)})
                write_version(db,project,after,{'kind':'candidate','layer_id':body.layer_id,'name':selected(doc,body.layer_id).name})
            return {'document':after.model_dump()}

    @app.post('/api/projects/{project}/image-jobs',status_code=202)
    def submit(project:str,body:ImageTask,request:Request):
        with lock,store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            own_project(project,request)
            try:store.admit_tasks(project)
            except ValueError as exc:raise HTTPException(429,str(exc)) from exc
            if store.query("SELECT id FROM agent_turns WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')",(project,)):raise HTTPException(409,'Agent 正在处理此项目，请先等待或停止')
            if len(store.query("SELECT id FROM jobs WHERE status IN ('queued','running')"))>=20:raise HTTPException(429,'任务队列已满')
            try:check_task(body,{a['id']:a for a in store.assets(project)})
            except ValueError as exc:raise HTTPException(422,str(exc)) from exc
            job=store.add_job(project,'image',{'kind':'image','tool':body.action,'image_task':body.model_dump()});wake.set()
            return public_job(job)

    @app.post('/api/projects/{project}/image-batch',status_code=202)
    def batch(project:str,body:ImageBatch,request:Request):
        with lock,store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            own_project(project,request);assets={a['id']:a for a in store.assets(project)};ids=list(dict.fromkeys(body.assets))
            try:store.admit_tasks(project,len(ids))
            except ValueError as exc:raise HTTPException(429,str(exc)) from exc
            if any(i not in assets or assets[i]['metadata']['kind']!='image' for i in ids):raise HTTPException(422,'批处理只接受当前项目图片')
            if len(store.query("SELECT id FROM jobs WHERE status IN ('queued','running')"))+len(ids)>20:raise HTTPException(429,'任务队列容量不足')
            if store.query("SELECT id FROM agent_turns WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')",(project,)):raise HTTPException(409,'Agent 正在处理当前项目')
            parameters=body.model_dump(exclude={'assets'});jobs=[]
            for asset_id in ids:
                task=ImageTask(action='batch_export',asset=asset_id,parameters=parameters)
                check_task(task,assets);jobs.append(store.add_job(project,'image',{'kind':'image','tool':'batch_export','image_task':task.model_dump()})['id'])
            wake.set();return {'jobs':jobs}

    @app.post('/api/projects/{project}/image-import',status_code=201)
    @admission('image_import')
    async def import_result(project:str,body:ImportResult,request:Request):
        from .api_staging import Staging
        def sources():
            own_project(project,request)
            if bool(body.job)==bool(body.turn):raise HTTPException(422,'请指定任务或 Agent 轮次')
            if body.job:
                job=store.job(body.job)
                if not job or job['project']!=project or job['status']!='succeeded':raise HTTPException(404,'结果不存在')
                available={name:name for name in job['artifacts']};base=store.project_dir(project)/'edit/jobs'/body.job
                published=store.query('SELECT attempt FROM execution_job_results WHERE job_id=?',(body.job,))
                if published:base=base/published[0]['attempt']
                identity={name:(str(base),name) for name in body.names}
            else:
                row=agent_store.turn(store,body.turn)
                if not row or row['project']!=project or (row['status']!='succeeded' and not row.get('workflow')):raise HTTPException(404,'结果不存在')
                available={a['name']:a['file'] for a in row['artifacts']};base=store.project_dir(project)/'edit/agent_exports'/body.turn
                identity={a['name']:tuple(a.get(k) for k in ('file','sha256','artifact_id','source_revision')) for a in row['artifacts'] if a['name'] in body.names}
            if any(n not in available for n in body.names):raise HTTPException(404,'结果图片不存在')
            return available,base,identity
        available,base,identity=await api_execution.run('read',sources)
        stage=Staging(store.root);result=[]
        try:
            for name in body.names:
                if Path(name).suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):raise ValueError('只能载入图片结果')
                def materialize():
                    path=base/available[name]
                    if store.shared_files:path=store.file(project,str(path.relative_to(store.project_dir(project))))
                    if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):raise ValueError('图片结果路径无效')
                    return path
                source=await api_execution.run('storage',materialize)
                from .media_isolation import normalize_image
                data,width,height=await api_execution.run('media',normalize_image,source)
                asset_id=uuid.uuid4().hex;filename=asset_id+'.png';size=len(data)
                dest=await api_execution.run('storage',stage.write,filename,data);del data
                meta={'kind':'image','width':width,'height':height,'has_alpha':True,'size':size,'mime':'image/png',
                      'derived_from':{'job':body.job,'turn':body.turn,'name':name}}
                uploaded=await api_execution.run('storage',store.artifacts.upload,project,dest,check=stage.check) if store.shared_files else None
                result.append({'id':asset_id,'name':Path(name).name,'filename':filename,'metadata':meta,'artifact':uploaded})
            def publish():
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if sources()[2]!=identity:raise HTTPException(409,'来源结果已变化，请重新选择。')
                    if len(store.assets(project))+len(result)>50:raise HTTPException(409,'每个项目最多 50 个素材。')
                    store.check_storage(project,sum(a['metadata']['size'] for a in result))
                    for a in result:
                        stage.move(a['filename'],store.project_dir(project)/'sources'/a['filename'])
                        store.add_asset(project,a['id'],a['name'],a['filename'],a['metadata'],artifact=a['artifact'])
                    stage.check()
                return {'assets':[{'id':a['id'],'name':a['name'],**a['metadata']} for a in result]}
            return await api_execution.run('write',stage.commit,publish)
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc
        except FileNotFoundError as exc:raise HTTPException(404,'结果图片不存在或已过期。') from exc
        except OSError as exc:raise HTTPException(503,'结果图片暂不可用，请稍后重试。') from exc
        finally:await stage.finish(api_execution)
