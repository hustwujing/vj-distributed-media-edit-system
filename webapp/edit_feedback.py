"""Private, revisioned editorial notes and lightweight version branches."""
import json,time,uuid
from pathlib import Path
from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse
from .api_execution import admission
from pydantic import BaseModel,ConfigDict,Field

class Note(BaseModel):
 model_config=ConfigDict(extra='forbid')
 start:float=Field(ge=0,le=86400);end:float|None=Field(default=None,ge=0,le=86400)
 text:str=Field(min_length=1,max_length=1200);kind:str=Field(default='note',pattern=r'^(note|remove|subtitle|audio|visual)$')
class Branch(BaseModel):
 model_config=ConfigDict(extra='forbid')
 name:str=Field(min_length=1,max_length=80);parent:str|None=Field(default=None,max_length=32)
class Chapter(BaseModel):
 model_config=ConfigDict(extra='forbid')
 asset:str=Field(min_length=1,max_length=32);start:float=Field(ge=0);end:float=Field(gt=0);title:str=Field(min_length=1,max_length=120);summary:str=Field(default='',max_length=500)
class Cue(BaseModel):
 model_config=ConfigDict(extra='forbid')
 start:float=Field(ge=0,le=86400);end:float=Field(gt=0,le=86400);text:str=Field(min_length=1,max_length=500)
class CaptionSave(BaseModel):
 model_config=ConfigDict(extra='forbid')
 name:str=Field(min_length=1,max_length=100);cues:list[Cue]=Field(min_length=1,max_length=2000)
class BrandTemplate(BaseModel):
 model_config=ConfigDict(extra='forbid')
 name:str=Field(min_length=1,max_length=80);width:int=Field(default=1280,ge=16,le=4096);height:int=Field(default=720,ge=16,le=4096)
 fit:str=Field(default='contain',pattern=r'^(contain|cover|blur)$');subtitle_size:int=Field(default=36,ge=10,le=200);subtitle_color:str=Field(default='#FFFFFF',pattern=r'^#[0-9a-fA-F]{6}$');subtitle_position:str=Field(default='bottom',pattern=r'^(top|center|bottom)$');normalize_audio:bool=True
class BatchRequest(BaseModel):
 model_config=ConfigDict(extra='forbid')
 template:str=Field(min_length=32,max_length=32);assets:list[str]=Field(min_length=1,max_length=20)

def register(app,store,own_project,lock,wake):
 api_execution=app.state.api_execution
 with store.connect() as db:
  db.executescript('''CREATE TABLE IF NOT EXISTS edit_notes(id TEXT PRIMARY KEY,project TEXT NOT NULL,start REAL NOT NULL,"end" REAL,text TEXT NOT NULL,kind TEXT NOT NULL,created REAL NOT NULL);
 CREATE INDEX IF NOT EXISTS edit_notes_project ON edit_notes(project,created);
 CREATE TABLE IF NOT EXISTS edit_branches(id TEXT PRIMARY KEY,project TEXT NOT NULL,name TEXT NOT NULL,parent TEXT,plan TEXT NOT NULL DEFAULT 'null',created REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS edit_chapters(id TEXT PRIMARY KEY,project TEXT NOT NULL,asset TEXT NOT NULL,start REAL NOT NULL,"end" REAL NOT NULL,title TEXT NOT NULL,summary TEXT NOT NULL,created REAL NOT NULL);''')
  db.execute('CREATE TABLE IF NOT EXISTS brand_templates(id TEXT PRIMARY KEY,project TEXT NOT NULL,name TEXT NOT NULL,template TEXT NOT NULL,created REAL NOT NULL)')
  if 'plan' not in {r['name'] for r in db.execute('PRAGMA table_info(edit_branches)')}:
   db.execute("ALTER TABLE edit_branches ADD COLUMN plan TEXT NOT NULL DEFAULT 'null'")
 def payload(project):
  return {'notes':store.query('SELECT * FROM edit_notes WHERE project=? ORDER BY start,created',(project,)),
   'branches':store.query('SELECT id,project,name,parent,created FROM edit_branches WHERE project=? ORDER BY created DESC',(project,)),
   'chapters':store.query('SELECT * FROM edit_chapters WHERE project=? ORDER BY asset,start',(project,)),
   'templates':[{**r,'template':json.loads(r['template'])} for r in store.query('SELECT * FROM brand_templates WHERE project=? ORDER BY created DESC',(project,))]}
 @app.get('/api/projects/{project}/editorial')
 def get(project:str,request:Request):own_project(project,request);return payload(project)
 @app.post('/api/projects/{project}/editorial/notes',status_code=201)
 def note(project:str,body:Note,request:Request):
  if body.end is not None and body.end<body.start:raise HTTPException(422,'批注结束时间必须晚于开始时间')
  with lock:
   own_project(project,request);id=uuid.uuid4().hex;store.execute('INSERT INTO edit_notes VALUES (?,?,?,?,?,?,?)',(id,project,body.start,body.end,body.text.strip(),body.kind,time.time()))
  return {'id':id}
 @app.delete('/api/projects/{project}/editorial/notes/{note_id}')
 def delete_note(project:str,note_id:str,request:Request):
  with lock:
   own_project(project,request)
   if not store.execute('DELETE FROM edit_notes WHERE project=? AND id=?',(project,note_id)):raise HTTPException(404,'批注不存在')
  return {'deleted':True}
 @app.post('/api/projects/{project}/editorial/branches',status_code=201)
 def branch(project:str,body:Branch,request:Request):
  with lock:
   own_project(project,request)
   if body.parent and not store.query('SELECT id FROM edit_branches WHERE project=? AND id=?',(project,body.parent)):raise HTTPException(422,'来源分支不存在')
   draft=store.draft(project)['preset']
   id=uuid.uuid4().hex;store.execute('INSERT INTO edit_branches (id,project,name,parent,plan,created) VALUES (?,?,?,?,?,?)',(id,project,body.name.strip(),body.parent,json.dumps(draft,ensure_ascii=False),time.time()))
  return {'id':id}
 @app.post('/api/projects/{project}/editorial/branches/{branch_id}/activate')
 def activate(project:str,branch_id:str,request:Request):
  with lock:
   own_project(project,request);rows=store.query('SELECT plan FROM edit_branches WHERE project=? AND id=?',(project,branch_id))
   if not rows:raise HTTPException(404,'版本分支不存在')
   plan=json.loads(rows[0]['plan'])
   if plan is None:raise HTTPException(409,'该分支创建时没有可恢复的编辑计划')
   store.save_draft(project,plan,store.draft(project)['prompt'],'tools')
  return {'activated':True}
 @app.post('/api/projects/{project}/editorial/chapters',status_code=201)
 def chapter(project:str,body:Chapter,request:Request):
  with lock:
   own_project(project,request);assets={a['id']:a for a in store.assets(project)}
   if body.asset not in assets or assets[body.asset]['metadata']['kind']!='video' or body.end>assets[body.asset]['metadata']['duration']+.02:raise HTTPException(422,'章节范围或素材无效')
   id=uuid.uuid4().hex;store.execute('INSERT INTO edit_chapters VALUES (?,?,?,?,?,?,?,?)',(id,project,body.asset,body.start,body.end,body.title.strip(),body.summary.strip(),time.time()))
  return {'id':id}
 @app.post('/api/projects/{project}/editorial/chapters/{chapter_id}/activate')
 def activate_chapter(project:str,chapter_id:str,request:Request):
  with lock:
   own_project(project,request);rows=store.query('SELECT * FROM edit_chapters WHERE project=? AND id=?',(project,chapter_id))
   if not rows:raise HTTPException(404,'章节不存在')
   c=rows[0];preset={'kind':'render','tool':'render','segments':[{'asset':c['asset'],'start':c['start'],'end':c['end'],'audio_track':0}],'quality':'draft','normalize_audio':True}
   store.save_draft(project,preset,'按章节“'+c['title']+'”制作独立短片，先给方案。','chat')
  return {'activated':True,'preset':preset}
 def asset(project,asset_id):
  return next((a for a in store.assets(project) if a['id']==asset_id),None)
 @app.get('/api/projects/{project}/editorial/captions/{asset_id}')
 async def captions(project:str,asset_id:str,request:Request,track:int=0):
  def lookup():
   own_project(project,request);a=asset(project,asset_id)
   if not a:raise HTTPException(404,'字幕来源不存在')
   return a
  a=await api_execution.run('read',lookup)
  def read_captions():
   from .api_staging import text_file
   if a['metadata']['kind']=='subtitle':
    from helpers.edit_captions import parse_srt_text
    cues=parse_srt_text(text_file(store,project,'sources/'+a['filename']))
   elif a['metadata']['kind']=='video':
    suffix=f'.track{track}' if track else ''
    value=json.loads(text_file(store,project,f'edit/transcripts/{asset_id}{suffix}.json'))
    if not isinstance(value,dict) or not isinstance(value.get('words',[]),list) or any(not isinstance(w,dict) for w in value.get('words',[])):
     raise ValueError('Invalid transcript data')
    words=[w for w in value.get('words',[]) if w.get('type')=='word' and w.get('start') is not None]
    cues=[];group=[]
    for w in words:
     if group and (len(group)>=8 or w['start']-group[-1]['end']>.5 or w.get('speaker_id')!=group[-1].get('speaker_id')):
      cues.append({'start':group[0]['start'],'end':group[-1]['end'],'text':''.join(x.get('text','') for x in group).strip()});group=[]
     group.append(w)
    if group:cues.append({'start':group[0]['start'],'end':group[-1]['end'],'text':''.join(x.get('text','') for x in group).strip()})
   else:raise HTTPException(422,'请选择视频转写或 SRT')
   return JSONResponse({'source':asset_id,'cues':[{'start':c['start'],'end':c['end'],'text':c['text']} for c in cues]})
  try:return await api_execution.run('storage',read_captions)
  except FileNotFoundError as exc:raise HTTPException(404,'字幕源不存在，请先转写对应音轨。') from exc
  except OverflowError as exc:raise HTTPException(413,'字幕源超过编辑器读取上限，请先分段处理。') from exc
  except (ValueError,TypeError,KeyError) as exc:raise HTTPException(422,'字幕源数据格式无效。') from exc
 @app.post('/api/projects/{project}/editorial/captions',status_code=201)
 @admission('upload')
 async def save_captions(project:str,body:CaptionSave,request:Request):
  from .api_staging import Staging
  await api_execution.run('read',own_project,project,request)
  stage=Staging(store.root);id=uuid.uuid4().hex;filename=id+'.srt'
  def prepare():
   previous=-1
   for cue in body.cues:
    if cue.end<=cue.start or cue.start<previous:raise HTTPException(422,'字幕时间必须递增且不能重叠')
    previous=cue.end
   from helpers.edit_captions import clock
   raw='\n\n'.join(f'{i+1}\n{clock(c.start)} --> {clock(c.end)}\n{c.text}' for i,c in enumerate(body.cues))+'\n'
   data=raw.encode('utf-8');stage.write(filename,data);return len(data)
  try:
   size=await api_execution.run('storage',prepare)
   uploaded=await api_execution.run('storage',store.artifacts.upload,project,stage.path/filename,check=stage.check) if store.shared_files else None
   def publish():
    with store.connect() as db:
     db.execute('BEGIN IMMEDIATE');own_project(project,request)
     if len(store.assets(project))>=50:raise HTTPException(409,'每个项目最多 50 个素材。')
     store.check_storage(project,size);stage.move(filename,store.project_dir(project)/'sources'/filename)
     store.add_asset(project,id,body.name.strip()+'.srt',filename,{'kind':'subtitle','size':size,'derived_from':'caption_editor'},artifact=uploaded)
     stage.check()
    return {'id':id,'name':body.name.strip()+'.srt'}
   return await api_execution.run('write',stage.commit,publish)
  except ValueError as exc:raise HTTPException(422,str(exc)) from exc
  finally:await stage.finish(api_execution)
 @app.post('/api/projects/{project}/editorial/templates',status_code=201)
 def template(project:str,body:BrandTemplate,request:Request):
  with lock:
   own_project(project,request)
   if body.width%2 or body.height%2:raise HTTPException(422,'模板画布尺寸必须为偶数')
   id=uuid.uuid4().hex;store.execute('INSERT INTO brand_templates VALUES (?,?,?,?,?)',(id,project,body.name.strip(),body.model_dump_json(),time.time()))
  return {'id':id}
 @app.post('/api/projects/{project}/editorial/batch',status_code=202)
 def batch(project:str,body:BatchRequest,request:Request):
  with lock:
   own_project(project,request);rows=store.query('SELECT template FROM brand_templates WHERE project=? AND id=?',(project,body.template,))
   if not rows:raise HTTPException(404,'品牌模板不存在')
   targets=list(dict.fromkeys(body.assets))
   if len(store.query("SELECT id FROM jobs WHERE status IN ('queued','running')"))+len(targets)>20:raise HTTPException(429,'任务队列容量不足，减少批量素材后重试')
   if store.query("SELECT id FROM agent_turns WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')",(project,)):raise HTTPException(409,'Agent 正在处理当前项目，请稍后提交批任务')
   t=json.loads(rows[0]['template']);assets={a['id']:a for a in store.assets(project)}
   if any(aid not in assets or assets[aid]['metadata']['kind']!='video' for aid in targets):raise HTTPException(422,'批处理只接受当前项目视频')
   jobs=[]
   for aid in targets:
    a=assets[aid]
    plan={'version':1,'clips':[{'asset':aid,'start':0,'end':a['metadata']['duration'],'fit':t['fit']}],'output':{'width':t['width'],'height':t['height'],'fps':30,'crf':20},'audio':{'normalize':t['normalize_audio']}}
    if store.has_file(project,'edit/transcripts/'+aid+'.json'):plan['subtitles']={'from_transcripts':True,'words_per_line':6,'size':t['subtitle_size'],'color':t['subtitle_color'],'position':t['subtitle_position'],'outline':2}
    jobs.append(store.add_job(project,'edit',{'kind':'edit','tool':'brand_batch','edit_plan':plan})['id'])
   wake.set()
  return {'jobs':jobs}
