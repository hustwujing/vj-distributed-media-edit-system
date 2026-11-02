"""Immutable planning intent inherited by the confirmed execution turn."""
import hashlib
import json
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator

class Intent(BaseModel):
    model_config=ConfigDict(extra='forbid')
    route:Literal['single','workflow']
    operations:list[str]=Field(min_length=1,max_length=32)
    implementations:dict[str,str]=Field(default_factory=dict,max_length=32)
    goal:str=Field(min_length=1,max_length=2000)
    estimated_seconds:int=Field(ge=1,le=86400)
    @model_validator(mode='after')
    def validate_route(self):
        from .operations import get
        from .operation_lifecycle import for_operation
        if not self.goal.strip():raise ValueError('执行目标不能为空')
        if set(self.implementations)-set(self.operations) or 'script' in self.implementations:
            raise ValueError('实现声明必须对应本次意图中的 operation')
        for operation in self.operations:
            if operation!='script':
                selected=get(operation,self.implementations.get(operation))
                self.implementations[operation]=for_operation(selected).id
        if self.route=='single' and (len(self.operations)!=1 or self.estimated_seconds>120):
            raise ValueError('多阶段或预计超过 120 秒的任务必须使用 workflow')
        return self


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS execution_intents(
      turn_id TEXT PRIMARY KEY,intent TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL
    );''')


def declare(store,turn_id,raw):
    value=Intent.model_validate(raw)
    encoded=value.model_dump_json()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT mode,status FROM agent_turns WHERE id=?',(turn_id,)).fetchone()
        if not row or row['mode']!='plan' or row['status']!='running':raise ValueError('执行意图只能在正在运行的讨论轮声明')
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        db.execute('INSERT INTO execution_intents VALUES (?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET intent=excluded.intent,digest=excluded.digest,created=excluded.created',(turn_id,encoded,digest,store.now()))
        from .plan_readiness import declared
        declared(store,turn_id,digest)
    return {'intent':value.model_dump(),'digest':digest}


def get(store,turn_id):
    rows=store.query('SELECT mode,plan_id FROM agent_turns WHERE id=?',(turn_id,))
    if not rows:return None
    ref=rows[0]['plan_id'] if rows[0]['mode']=='execute' else turn_id
    records=store.query('SELECT intent,digest FROM execution_intents WHERE turn_id=?',(ref,))
    if not records:return None
    value=json.loads(records[0]['intent'])
    labels={'script':'本地处理','image.remove_background':'图片去背景','image.edit':'生成式图片编辑（外部）','image.generate_from_text':'图片生成',
        'video.generate_from_image':'参考图生成视频','video.generate_from_text':'文字生成视频','audio.transcribe':'语音转写','image.segment':'主体分割','image.segment_sam3':'主体分割与精修',
        'image.refine_zim':'边缘精修','image.ocr':'文字识别','image.decompose':'图片分层','image.erase':'选区擦除','image.edit_region':'选区重绘',
        'media.render':'视频合成','media.edit':'音视频编辑','media.timeline':'时间线预览','media.analyze':'画面分析','media.review':'成片检查','media.pack':'整理转写'}
    from .operations import get as operation
    external=sum(name!='script' and bool(operation(name,value.get('implementations',{}).get(name)).provider) for name in value['operations'])
    return {**value,'digest':records[0]['digest'],'summary':{'operation_count':len(value['operations']),'external_calls':external,
        'labels':[labels.get(name,'图片处理' if name.startswith('image.') else '媒体处理') for name in value['operations']]}}


def enforce_direct(lease,operation):
    if not lease or lease.kind=='manual':return
    def denied(message):
        from .agent_store import event
        from .telemetry import emit
        event(lease.store,lease.turn_id,'operation_denied',{'operation':operation,'reason':message})
        emit('operation.intent_denied','WARNING',operation=operation,task_id=lease.turn_id)
        raise ValueError(message)
    value=get(lease.store,lease.turn_id)
    if not value:denied('已确认方案未声明直接处理意图，请改用持久化工作流或重新规划独立短任务')
    if value['route']!='single':
        denied('该能力属于已确认的多阶段计划，请作为工作流 operation 步骤提交')
    if value['operations']!=[operation]:
        denied('请求能力 '+operation+' 与已确认意图不符；请重新声明实际能力并由用户确认，或按已确认工作流提交')
    from .operations import get as get_operation
    from .operation_lifecycle import for_operation
    approved=value.get('implementations',{}).get(operation)
    if approved and approved!=for_operation(get_operation(operation)).id:
        denied('直接入口的实现与已确认意图不一致，请通过工作流使用已批准实现')
    lease.consume('direct:'+operation,1)


def completion_problem(store,turn_id,artifacts,canvas_result=None,*,delivery_error=None):
    """A completed model reply is not proof that the declared work was done."""
    value=get(store,turn_id)
    if not value:return None  # Legacy conversations retain their existing contract.
    if value['route']=='workflow':return ('paused','已确认的多阶段方案尚未提交工作流，未标记完成。')
    if store.query("SELECT seq FROM agent_events WHERE turn_id=? AND kind='operation_denied' LIMIT 1",(turn_id,)):
        return ('paused','执行使用了已确认方案之外的能力，未标记完成；请修订方案并重新确认。')
    pending=store.query("SELECT request_key FROM workflow_provider_calls WHERE workflow=? AND state NOT IN ('succeeded','failed','cancelled','rejected')",(turn_id,))
    if pending:return ('paused','外部处理尚未确认完成，任务记录已保留，请核实结果或停止后重新规划。')
    if delivery_error:return ('failed','交付声明未通过检查，生成文件已保留供后续修正：'+delivery_error)
    from .artifacts import is_process_file
    deliverable=any(not is_process_file(item) and item.get('purpose')!='style_preview' for item in artifacts)
    if not deliverable and not canvas_result:return ('failed','本轮没有产生可交付成果，未标记业务完成。')
    return None
