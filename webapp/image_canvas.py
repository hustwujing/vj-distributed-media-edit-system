"""Transactional canvas edits shared by the browser and the scoped Agent tool."""
import json
import time

from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field

from .image_document import Document,Layer,check_document


class CanvasContext(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int=Field(ge=1)
    layer_id:str=Field(min_length=1,max_length=80)


def current(db,project,revision):
    row=db.execute('SELECT document FROM image_documents WHERE project=?',(project,)).fetchone()
    doc=Document.model_validate_json(row['document']) if row else Document()
    if doc.revision!=revision:
        raise HTTPException(409,'画布版本已更新，请回到图片工作台重新选择图层；原有编辑没有被覆盖。')
    return doc


def selected(doc,layer_id):
    layer=next((l for l in doc.layers if l.id==layer_id),None)
    if layer is None:raise HTTPException(404,'选中的图层不存在')
    return layer


def ensure_unlocked(layer):
    if layer.locked:raise HTTPException(409,'图层「'+layer.name+'」已锁定，请在图片工作台手动解锁。')


def check_locks(before,after):
    """Protect layer data and stacking relative to every surviving old layer."""
    new={l.id:l for l in after.layers}
    old_ids=[l.id for l in before.layers]
    new_ids=[l.id for l in after.layers]
    for layer in before.layers:
        if not layer.locked:continue
        if layer.id not in new or new[layer.id]!=layer:
            raise HTTPException(409,'已锁定的图层不能修改或删除：'+layer.name)
        for other in set(old_ids)&set(new_ids):
            if (old_ids.index(other)<old_ids.index(layer.id))!=(new_ids.index(other)<new_ids.index(layer.id)):
                raise HTTPException(409,'不能跨越锁定图层调整顺序：'+layer.name)


def write_version(db,project,doc,change):
    doc.revision+=1
    raw=doc.model_dump_json()
    db.execute('INSERT INTO image_documents VALUES (?,?,?) ON CONFLICT(project) DO UPDATE SET revision=excluded.revision,document=excluded.document',(project,doc.revision,raw))
    db.execute('INSERT INTO image_versions (project,revision,document,created,change) VALUES (?,?,?,?,?)',(project,doc.revision,raw,time.time(),json.dumps(change,ensure_ascii=False)))
    return doc


def bind_context(store,project,context):
    if context is None:return None
    value=CanvasContext.model_validate(context)
    with store.connect() as db:
        doc=current(db,project,value.revision)
        layer=selected(doc,value.layer_id);ensure_unlocked(layer)
    return {**value.model_dump(),'layer':layer.model_dump(),'width':doc.width,'height':doc.height,
            'locked_layers':[{'id':l.id,'name':l.name} for l in doc.layers if l.locked]}


def patch_layer(doc,layer_id,patch,assets):
    layer=selected(doc,layer_id);ensure_unlocked(layer)
    allowed=set(Layer.model_fields)-{'id','kind','locked'}
    if not isinstance(patch,dict) or not patch or set(patch)-allowed:
        raise HTTPException(422,'只允许修改选中图层的内容、位置、尺寸和样式；不允许更改 ID、类型或锁定状态。')
    irrelevant={'image':{'text','size','color','stroke_color','stroke_width','shadow_color','shadow_blur','shadow_x','shadow_y','text_align'},
                'text':{'asset'},'rectangle':{'text','size','asset','stroke_color','stroke_width','shadow_color','shadow_blur','shadow_x','shadow_y','text_align'},
                'adjustment':{'asset','text','size','color','stroke_color','stroke_width','shadow_color','shadow_blur','shadow_x','shadow_y','text_align','blend_mode','clip_to_below'}}[layer.kind]
    if set(patch)&irrelevant:
        raise HTTPException(422,'这些字段不适用于此图层类型；修改图片像素请先生成图片，再用 --image 应用。')
    if 'asset' in patch and layer.kind!='image':raise HTTPException(422,'只有图片图层可以替换图片')
    try:
        changed=Layer.model_validate({**layer.model_dump(),**patch})
        after=doc.model_copy(deep=True)
        after.layers=[changed if l.id==layer_id else l for l in after.layers]
        check_document(after,assets)
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    return after


def agent_patch(store,project,run_id,body,new_asset=None):
    """One atomic application per confirmed turn. No arbitrary project/layer selectors."""
    if not isinstance(body,dict) or set(body)-{'patch','apply','image'}:
        raise HTTPException(422,'画布操作参数无效')
    if 'apply' in body and not isinstance(body['apply'],bool):raise HTTPException(422,'apply 必须为布尔值')
    apply=body.get('apply',False)
    uploaded=None
    if new_asset and apply and store.shared_files:
        uploaded=store.artifacts.upload(project,store.project_dir(project)/'sources'/new_asset['filename'])
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM agent_turns WHERE id=? AND project=?',(run_id,project)).fetchone()
        if not row or row['status']!='running':raise HTTPException(409,'本轮已经停止，不能继续修改画布')
        context=json.loads(row['canvas_context'])
        if not context:raise HTTPException(403,'请先从图片工作台选中图层，再进入对话编辑')
        if apply and row['mode']!='execute':raise HTTPException(403,'讨论阶段仅可检查修改，确认执行后才能应用')
        if apply and row['canvas_result']!='null':raise HTTPException(409,'本轮已经应用过修改；请开始新一轮继续编辑')
        before=current(db,project,context['revision'])
        assets={a['id']:a for a in store.assets(project)}
        patch=body.get('patch',{})
        if new_asset:
            if not apply:raise HTTPException(422,'生成图片只能在确认执行后应用')
            assets[new_asset['id']]=new_asset
            patch={**patch,'asset':new_asset['id']}
        after=patch_layer(before,context['layer_id'],patch,assets)
        fields=[k for k in patch if getattr(selected(before,context['layer_id']),k)!=getattr(selected(after,context['layer_id']),k)]
        if not fields:raise HTTPException(422,'这次修改没有改变图层')
        change={'kind':'agent','turn':run_id,'layer_id':context['layer_id'],'name':context['layer']['name'],'fields':fields}
        if apply:
            if new_asset:
                store.check_storage(project,new_asset['metadata']['size'])
                a=new_asset;store.add_asset(project,a['id'],a['name'],a['filename'],a['metadata'],artifact=uploaded)
            write_version(db,project,after,change)
            result={**change,'before':before.revision,'after':after.revision}
            db.execute('UPDATE agent_turns SET canvas_result=?,updated=? WHERE id=?',(json.dumps(result,ensure_ascii=False),time.time(),run_id))
    return {'applied':apply,'before':before.model_dump(),'after':after.model_dump(),
            'assets':[{**a['metadata'],'id':a['id'],'name':a['name'],'filename':a['filename']} for a in assets.values()]}
