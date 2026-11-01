"""Owned, revision-pinned inputs and stable output roles."""
import hashlib
import os
from pathlib import Path
import stat
import uuid
from .operations import for_step


def digest(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('输入必须为独立普通文件')
        value=hashlib.sha256()
        while chunk:=stream.read(1024*1024):value.update(chunk)
    return value.hexdigest()


def roles(spec,files,selected=None):
    result=dict(selected or {})
    if any(path not in files for path in result.values()):raise ValueError('选择的产物不属于当前结果')
    for role,name in spec.get('output_roles',{}).items():
        found=[f for f in files if Path(f).name==name]
        if len(found)!=1:raise ValueError('输出角色缺失或不唯一: '+role)
        result[role]=found[0]
    op=for_step(spec)
    if not op:return result
    for role in op.outputs:
        if role in result:continue
        names={'foreground':['foreground.png'],'mask':['mask.png'],'transcript':['transcript.json'],
               'report':['ocr.json','layer-report.json','image.report.json','analysis.json','quality-review.json','final.report.json'], 'preview':['ocr-preview.png','timeline.png'],
               'plan':['edit_plan.json'],'subtitle':['master.srt'],'text':['takes_packed.md'],
               'layers':['editable-layers.ora','editable-layers.psd']} .get(role,[])
        candidates=[f for f in files if Path(f).name in names]
        if not candidates and role=='primary_image':
            candidates=[f for f in files if Path(f).suffix.lower() in ('.png','.jpg','.jpeg','.webp') and Path(f).name not in ('mask.png','ocr-preview.png','layer-overview.png')]
            preferred=[f for f in candidates if Path(f).name in ('image.png','composite.png')]
            if preferred:candidates=preferred
        if not candidates and role=='video':
            candidates=[f for f in files if Path(f).suffix.lower() in ('.mp4','.mov','.webm','.mkv')]
            primary=[f for f in candidates if Path(f).name=='final.mp4']
            if primary:candidates=primary
        if not candidates and role=='audio':candidates=[f for f in files if Path(f).suffix.lower() in ('.wav','.mp3','.flac','.m4a')]
        if not candidates and role=='report':candidates=[f for f in files if Path(f).suffix=='.json' and 'report' in Path(f).name]
        if not candidates and role=='preview':candidates=[f for f in files if Path(f).suffix.lower() in ('.png','.jpg')]
        if role=='layers' and len(candidates)>1:candidates=[next(f for f in candidates if f.endswith('.ora'))]
        if len(candidates)==1:result[role]=candidates[0]
        elif len(candidates)>1 and role=='preview' and op.executor=='fixed_media':continue
        elif len(candidates)>1:raise ValueError('多个候选产物无法唯一选择角色: '+role)
    return result


def resolve(store,w,s,env):
    assets={a['id']:a for a in env['assets']}
    result={}
    for name,ref in s['spec'].get('inputs',{}).items():
        if ref.get('asset'):
            asset=assets.get(ref['asset'])
            if not asset:raise ValueError('输入素材不属于当前项目')
            path=env['sources']/asset['filename']
            record={'asset':asset['id'],'path':'sources/'+asset['filename'],'sha256':digest(path),'metadata':asset['metadata']}
        else:
            if ref['step'] not in s['spec']['depends_on']:raise ValueError('输入缺少依赖声明')
            parent=next((x for x in w['steps'] if x['id']==ref['step']),None)
            if not parent or parent['status']!='succeeded':raise ValueError('依赖结果尚未验收')
            names=parent['quality'].get('output_roles') or roles(parent['spec'],parent['result'])
            relative=names.get(ref['role'])
            if not relative or relative not in parent['result']:raise ValueError('依赖输出角色不存在')
            path=env['workspace']/relative
            if not path.resolve().is_relative_to(env['workspace'].resolve()) or any(p.is_symlink() for p in [path,*path.parents] if p!=env['workspace'].parent):raise ValueError('非法产物路径')
            expected=next((f for f in parent['quality'].get('files',[]) if f['name']==relative),None)
            sha=digest(path)
            if not expected or sha!=expected.get('sha256'):raise ValueError('输入文件与验收版本不一致')
            record={'step':parent['id'],'revision':parent['revision'],'role':ref['role'],'path':relative,'sha256':sha,
                    'metadata':{**expected,'kind':'video' if expected.get('video') else 'audio' if expected.get('audio') else expected.get('kind')}}
        previous=s.get('bindings',{}).get(name)
        operation=for_step(s['spec'])
        if operation and record['metadata'].get('kind') not in operation.input_kinds:
            raise ValueError('输入类型不符合 operation 契约: '+name)
        if previous and previous!=record:raise ValueError('本次步骤输入版本发生变化，需要显式修订')
        result[name]=record
    if result:
        if w.get('manual'):
            import json
            store.execute('UPDATE manual_operation_runs SET bindings=? WHERE job_id=?',(json.dumps(result),w['turn_id']))
        else:
            from .workflows import change
            import json
            with store.connect() as db:
                latest=db.execute('SELECT bindings,revision FROM agent_steps WHERE workflow=? AND id=?',(w['id'],s['id'])).fetchone()
                if not latest or latest['revision']!=s['revision']:raise ValueError('Input revision changed')
                bindings=json.loads(latest['bindings'])
                for name,record in result.items():
                    if name in bindings and bindings[name]!=record:raise ValueError('Input binding changed')
                change(store,w['id'],s['id'],bindings={**bindings,**result})
    return result


def staged_assets(store,w,s,env):
    bound=resolve(store,w,s,env)
    assets=list(env['assets']);ids={}
    for role,record in bound.items():
        if record.get('asset'):ids[role]=record['asset'];continue
        source=env['workspace']/record['path']
        name='workflow-'+uuid.uuid4().hex+source.suffix.lower();destination=env['sources']/name
        # Bytes are verified before becoming a trusted input in this attempt.
        fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        sha=hashlib.sha256()
        with os.fdopen(fd,'rb') as src,destination.open('xb') as dst:
            while chunk:=src.read(1024*1024):sha.update(chunk);dst.write(chunk)
        if sha.hexdigest()!=record['sha256']:destination.unlink();raise ValueError('输入复制期间发生变化')
        os.chown(destination,env['uid'],env['gid']);destination.chmod(0o400)
        aid='wf_'+uuid.uuid4().hex
        assets.append({'id':aid,'filename':name,'name':role,'metadata':record['metadata']});ids[role]=aid
    return assets,ids


def image_task(store,w,s,env):
    op=for_step(s['spec'])
    assets,ids=staged_assets(store,w,s,env)
    from .image_document import ImageTask,check_task
    task=ImageTask.model_validate({'action':op.action,**s['spec']['parameters'],'asset':ids.get('source'),
        'mask':ids.get('mask'),'references':[ids[k] for k in ('reference','reference2') if k in ids]})
    check_task(task,{a['id']:a for a in assets})
    return task.model_dump(),assets
