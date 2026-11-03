"""Bounded server-profile Agent loops, using existing model and sandbox adapters."""
from __future__ import annotations
import base64
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import time
import uuid
from contextlib import contextmanager
from . import workflow_graph as graph,workflow_planning as planning
from .specialist_profiles import get as get_profile
from .workflow_dispatch import stage_result
from .task_leases import LeaseLost,TaskQuotaReached


class ModelServiceError(RuntimeError):
    def __init__(self,status,code='model_provider_http_error'):
        self.diagnostic={'code':code,'http_status':status}
        super().__init__('Model service could not complete this request')


class MediaInspectionError(RuntimeError):
    def __init__(self,code,returncode,reference=None):
        self.diagnostic={'code':code,'exit_code':returncode}
        if reference is not None:self.diagnostic['reference_sha256']=hashlib.sha256(reference.encode()).hexdigest()
        super().__init__('The fixed media inspector could not produce bounded evidence')


class AgentReplyError(ValueError):
    def __init__(self,reply,offset=0):
        self.reply=reply;self.offset=offset
        super().__init__('The public Agent reply is not one JSON object')


class InspectionInputError(ValueError):
    def __init__(self,message,code='inspection_input_rejected'):
        self.code=code
        super().__init__(message)


def decode_reply(text):
    if not text.strip():raise ModelServiceError(200,'model_response_empty')
    lines=text.strip().splitlines()
    if len(lines)>=3 and lines[0].strip().lower() in ('```','```json') and lines[-1].strip()=='```':
        text='\n'.join(lines[1:-1])
    try:reply=json.loads(text)
    except json.JSONDecodeError as exc:raise AgentReplyError(text,exc.pos) from None
    if not isinstance(reply,dict):raise AgentReplyError(text)
    return reply


def proposal_feedback(raw):
    """Validate before side effects; return bounded errors without copying input values."""
    from pydantic import ValidationError
    try:graph.ManagerResult.model_validate(raw)
    except ValidationError as exc:
        return [{'path':'.'.join(map(str,e['loc'])),'type':e['type'],
                 'message':e['msg'][:240]} for e in exc.errors(include_input=False,include_url=False)[:20]]
    return []


def reserve_tokens(store,lease,run,amount):
    with store.connect() as db:
        lease.validate(db)
        used=db.execute('SELECT COALESCE(SUM(COALESCE(used_tokens,reserved_tokens)),0) AS n FROM workflow_model_reservations WHERE run_id=?',(run['id'],)).fetchone()['n']
        if used+amount>run['authorization']['model_token_budget']:
            raise TaskQuotaReached('工作流模型 Token 预算不足，未发起新的请求。',category='model_tokens',scope='workflow',used=used,limit=run['authorization']['model_token_budget'])
        identifier=uuid.uuid4().hex
        db.execute("INSERT INTO workflow_model_reservations(id,run_id,attempt_id,reserved_tokens,state,created_at) VALUES (?,?,?,?,'reserved',?)",
                   (identifier,run['id'],lease.attempt,amount,store.now()))
        return identifier


@contextmanager
def model_budget(lease,run,amount,receipt):
    reservation=reserve_tokens(lease.store,lease,run,amount)
    try:yield reservation
    finally:
        receipt.finish();usage=receipt.usage or {};used=usage.get('total_tokens');state='settled'
        if used is None and type(usage.get('input_tokens'))==int and type(usage.get('output_tokens'))==int:
            used=usage['input_tokens']+usage['output_tokens'];state='settled_derived'
        if type(used)==int:
            from .task_lifecycle import administrative
            from .execution_metrics import measurement_connection
            from .store import Store
            ledger=Store(lease.store.root,timeout=2,initialize_schema=False,dsn=lease.store.dsn)
            try:
                with administrative(),measurement_connection(ledger) as db:
                    db.execute('UPDATE workflow_model_reservations SET used_tokens=?,state=? WHERE id=? AND attempt_id=?',
                               (used,state,reservation,lease.attempt))
            except Exception as exc:
                from .telemetry import emit
                emit('workflow.token_receipt_failed','WARNING',exc=exc,task_id=lease.turn_id,attempt_id=lease.attempt)


def model(host,lease,run,profile,conversation,images=(),deadline=None):
    from .agent_profiles import for_task
    from .model_adapters import get,PublicResponseStructureError
    from .egress_policy import provider_base,session,assert_no_credentials,secrets
    from .task_usage import model_call
    configuration=for_task(host.store,lease.turn_id)
    adapter=get(configuration.protocol);base,key=configuration.credentials()
    instructions=model_instructions(profile)
    body=adapter.conversation(instructions,conversation,images)
    body=adapter.prepare(body,configuration.model,profile.max_output_tokens,configuration.reasoning_effort)
    raw=graph.encode(body);assert_no_credentials(raw,(*secrets(),key))
    # UTF-8 bytes give a conservative textual token upper bound. Inline pixels
    # are bounded separately; unresolved receipts retain the whole reservation.
    lease.consume('model',configuration.budget['task_models'])
    receipt=adapter.receipt()
    from .telemetry import emit
    emit('model.request_configuration',model=configuration.model,protocol=configuration.protocol,
         harness=profile.harness,profile_id=profile.id,configuration_digest=graph.digest(profile.snapshot()),
         max_output_tokens=profile.max_output_tokens,reasoning_effort=configuration.reasoning_effort)
    deadline=deadline or time.monotonic()+profile.timeout_seconds
    with model_budget(lease,run,len(raw.encode())+profile.max_output_tokens,receipt),model_call(lease,configuration.model,actor=actor(profile),observer=receipt),session() as client:
        with client.post(provider_base(base)+adapter.path,json=body,headers=adapter.headers(key),stream=True,
                         timeout=(10,max(1,min(240,deadline-time.monotonic()))),allow_redirects=False) as response:
            if response.status_code!=200:receipt.status='failed';raise ModelServiceError(response.status_code)
            data=bytearray()
            from .safe_stream import chunks
            for chunk in chunks(response):
                lease.check()
                if host.stop.is_set():raise LeaseLost('Worker is stopping')
                if time.monotonic()>deadline:raise TimeoutError('Agent phase deadline reached')
                data.extend(chunk);receipt.feed(chunk)
                if len(data)>8*1024**2:raise ValueError('Model response exceeds limit')
            receipt.finish()
            try:payload=json.loads(data)
            except (ValueError,TypeError):raise ModelServiceError(200,'model_provider_invalid_response') from None
    try:text=adapter.response_text(payload)
    except PublicResponseStructureError:
        raise AgentReplyError('Provider returned multiple public text blocks; return one JSON action only') from None
    except (ValueError,KeyError,TypeError,IndexError):raise ModelServiceError(200,'model_response_incomplete') from None
    assert_no_credentials(text,(*secrets(),key))
    structure={'payload_type':type(payload).__name__,'top_level_keys':sorted(payload)[:30] if isinstance(payload,dict) else [],
               'text_characters':len(text),'text_sha256':hashlib.sha256(text.encode()).hexdigest()}
    if isinstance(payload,dict):
        for key in ('choices','output','content'):
            if isinstance(payload.get(key),list):structure[key+'_items']=len(payload[key])
    emit('workflow.model_response_structure',task_id=lease.turn_id,step_id=lease.step_id,operation=profile.id,**structure)
    return decode_reply(text)


def actor(profile):
    return 'manager_agent' if profile.id=='media_planner' else 'reviewer_agent' if profile.role in ('REVIEWER','DIAGNOSTICIAN') else 'specialist_agent'


def model_instructions(profile):
    from .specialist_profiles import skill_materials
    return profile.prompt+'\n每次只输出一个 JSON 对象，不使用 Markdown 围栏。'+CONTRACT+'\n'+\
        '\n'.join('Registered skill '+item['id']+' ('+item['sha256']+'):\n'+item['text'] for item in skill_materials(profile))


CONTRACT='''
可用动作由服务端 profile.tools 限定。每轮只能选一个动作：
{"action":"inspect","ref":"素材ID 或冻结快照中的产物路径","offset":0}
或 {"action":"inspect","refs":[{"ref":"...","offset":0}, ...]}
  只读取当前任务获准的输入/结果，返回有界文本、元数据或图片证据；不能提交任意宿主路径。
  refs 每轮最多8项，适合批量元数据或联系表取样；整批计一个检查轮，但每项仍独立记录证据。
  文本 offset 为整数的字节位置；视频 offset 为秒，支持小数，必须早于实际结尾。静态图片没有时间定位。ZIP仅提供有界目录元数据，offset须为0，不解包、不证明包内媒体内容或CRC；未知文件类型会拒绝并反馈。
{"action":"proposal","proposal":{...}}
  仅 Manager 使用，proposal 严格符合快照中的 proposal_schema。
{"action":"contract","operation":"登记的 operation ID"}
  仅 Manager 使用。按冻结能力摘要查询单个完整参数、输入输出和恢复契约；查询不授予执行权限。
{"action":"script","code":"Python 源码"}
  仅 editing_implementer 使用，在隔离沙盒运行，当前目录为任务输出目录。使用 json.loads(os.environ['VIDEO_USE_STEP_INPUTS']) 获取输入角色到绝对路径列表的映射，不能猜测素材路径。图片/音视频等二进制文件通过脚本生成；可用 Pillow、NumPy 和 FFmpeg，写入当前目录或 VIDEO_USE_STEP_OUTPUT。
{"action":"result","files":{"declared.json":"UTF-8内容"}}
  files 只写 UTF-8 文本内容，不是 base64 文件接口。执行过 script 时用 files:{} 引用任务输出目录内已经生成的声明文件。
{"action":"result","decision":{"snapshot_digest":"...","verdict":"pass 或 reject",
 "evidence":[{"criterion_index":0,"observation":"实际检查发现","inspection_id":"inspect返回的32位ID"}],
 "deliverables":["交付物的inspection_id"],"primary":"主交付的inspection_id"}}
  Reviewer 专用。pass 必须检查实际证据；criterion_index 对应冻结 review_criteria/delivery_targets 的序号。只能使用 inspect 返回的 inspection_id，服务端会解析完整不可变引用。最终交付多份文件时必须填写primary；中间步骤验收不填写primary。
不得猜测文件内容。工具错误不会扩大权限。输出 result/proposal 后本轮立即结束。
'''


def inputs(store,run,task,frozen):
    allowed={}
    assets={asset['id']:asset for asset in store.assets(run['project_id'])}
    if task.get('control_kind'):
        for identifier in run['authorization']['allowed_assets']:
            if identifier in assets:allowed[identifier]={'asset':assets[identifier]}
        visible=frozen.get('tasks',[])+frozen.get('upstream',[])
    else:
        visible=[]
        for ref in task['spec'].get('inputs',{}).values():
            if ref.get('asset'):allowed[ref['asset']]={'asset':assets[ref['asset']]}
            elif ref.get('step'):
                parent=next(t for t in graph.work_items(run) if t['id']==ref['step'] and t['status']=='succeeded')
                path=parent['quality'].get('output_roles',{}).get(ref['role'])
                visible.append({**parent,'accepted_result':[path] if path else [],'candidate_result':[]})
    for parent in visible:
        names=parent.get('accepted_result',[])+parent.get('candidate_result',[])
        if task.get('control_kind') and task['spec']['profile']=='media_failure_diagnostician':
            names+= [name for name in frozen.get('diagnostic_files',[]) if name.startswith(f'edit/workflows/{run["id"]}/{parent["id"]}/')]
        if not names:continue
        checkpoints=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                                (run['id'],parent['id'],parent['revision']))
        manifest=json.loads(checkpoints[0]['manifest']) if checkpoints else {}
        for name in names:
            if name in manifest:allowed[name]={'record':manifest[name]}
    return allowed


def archive_index(path):
    """Bounded ZIP metadata only: never extract or execute archive members."""
    import zipfile,stat
    from pathlib import PurePosixPath
    if path.stat().st_size>32*1024**2:
        raise InspectionInputError('ZIP exceeds the 32 MiB directory inspection limit; inspect its text manifest.', 'inspection_archive_limit')
    try:
        with zipfile.ZipFile(path) as archive:
            entries=archive.infolist()
            if len(entries)>1000 or sum(item.file_size for item in entries)>256*1024**2:
                raise InspectionInputError('ZIP directory exceeds member/expanded-size limits; inspect its text manifest.', 'inspection_archive_limit')
            members=[]
            for item in entries:
                name=item.filename;parts=PurePosixPath(name)
                if (not name or len(name)>500 or parts.is_absolute() or '..' in parts.parts or '\\' in name
                        or stat.S_ISLNK(item.external_attr>>16) or item.flag_bits&1):
                    raise InspectionInputError('ZIP contains unsafe, encrypted or unsupported member metadata.', 'inspection_archive_member')
                members.append({'name':name,'size':item.file_size,'compressed_size':item.compress_size,
                                'crc32':item.CRC,'is_directory':item.is_dir()})
    except zipfile.BadZipFile:
        raise InspectionInputError('Invalid ZIP directory; no archive evidence was recorded.', 'inspection_archive_invalid') from None
    if len(graph.encode(members).encode())>16384:
        raise InspectionInputError('ZIP directory exceeds the 16 KiB evidence limit; inspect its text manifest.', 'inspection_archive_limit')
    return {'coverage':'archive_directory','members':members,'member_count':len(members),'contents_verified':False,
            'limitation':'Only ZIP directory metadata was inspected. Members were not extracted, decoded, or checked for content/CRC correctness; use separate manifests and media evidence for those claims.'}


def inspect(store,run,allowed,reference,offset=0):
    if reference not in allowed:raise ValueError('Reference outside frozen task scope')
    if type(offset) not in (int,float) or not math.isfinite(offset) or offset<0:raise InspectionInputError('offset must be finite and nonnegative')
    item=allowed[reference]
    if 'asset' in item:
        asset=item['asset'];path=store.file(run['project_id'],'sources/'+asset['filename'])
        metadata=asset['metadata']
    else:
        record=item['record'];metadata={k:record[k] for k in ('sha256','size')}
        path=store.artifacts.materialize(run['project_id'],record['artifact']) if store.shared_files else store.root/record['location']
    suffix=Path(reference).suffix.lower() if 'asset' not in item else Path(item['asset']['filename']).suffix.lower()
    if suffix in ('.txt','.md','.json','.srt','.vtt','.ass','.csv','.py','.log'):
        if type(offset)!=int:raise InspectionInputError('Text offset is an integer byte offset, not a video timestamp')
        if path.stat().st_size and offset>=path.stat().st_size:raise InspectionInputError('Text offset is beyond EOF; use an earlier byte offset')
        with path.open('rb') as source:source.seek(offset);content=source.read(8192)
        return {'ref':reference,'metadata':metadata,'offset':offset,'text':content.decode('utf-8',errors='replace'),'next_offset':offset+len(content),'complete':offset+len(content)>=path.stat().st_size}
    if suffix=='.ora':
        import zipfile
        from xml.etree import ElementTree
        with zipfile.ZipFile(path) as archive:
            members=archive.infolist()
            if len(members)>1000 or sum(item.file_size for item in members)>256*1024**2:raise ValueError('Layered image exceeds inspection limit')
            with archive.open('stack.xml') as stream:raw=stream.read(1024**2+1)
            if len(raw)>1024**2 or b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:raise ValueError('Unsafe layer metadata')
            document=ElementTree.fromstring(raw)
            names=set(archive.namelist());layers=[dict(layer.attrib) for layer in document.iter('layer')]
            if not layers or any(layer.get('src') not in names for layer in layers):raise ValueError('Missing layer data')
            return {'ref':reference,'metadata':metadata,'coverage':'layer_structure','layers':layers,'dimensions':dict(document.attrib)}
    if suffix=='.zip':
        if offset!=0:raise InspectionInputError('ZIP directory inspection requires offset 0')
        return {'ref':reference,'metadata':metadata,**archive_index(path)}
    kind=mimetypes.guess_type('file'+suffix)[0] or ''
    if not kind.startswith(('image/','video/','audio/')):
        raise InspectionInputError('Unsupported inspection file type; inspect a supported text manifest or media file.', 'unsupported_inspection_type')
    # Media decoding/pixels are supplied by a fixed sandbox tool in the worker,
    # never by the control-plane service.
    still_image=suffix in ('.png','.jpg','.jpeg','.webp','.gif','.bmp')
    return {'ref':reference,'metadata':metadata,'media':True,'path':path,'still_image':still_image,
            'offset_seconds':0 if still_image else offset}


def execute(host,lease,run,task):
    store=host.store;profile=get_profile(task['spec']['profile'])
    if graph.digest(profile.snapshot())!=graph.digest(task['profile']):raise ValueError('Frozen Agent profile is unavailable; replan is required')
    lease.start_compute()
    if task.get('control_kind'):
        table='planner_runs' if profile.id=='media_planner' else 'workflow_reviews'
        row=store.query(f'SELECT snapshot_json,snapshot_digest FROM {table} WHERE id=?',(task['spec']['request_id'],))[0]
        frozen=json.loads(row['snapshot_json']);frozen['snapshot_digest']=row['snapshot_digest']
    else:
        frozen={'task':task['spec'],'goal':run['goal'],'graph_version':run['graph_version'],'repair_context':task['repair_context']}
        if profile.id=='media_failure_diagnostician':
            frozen['failure_evidence']=[{'task_id':row['step'],'diagnostic':json.loads(row['record'])} for row in
                store.query('SELECT step,record FROM workflow_diagnostics WHERE workflow=? ORDER BY created DESC LIMIT 30',(run['id'],))]
    allowed=inputs(store,run,task,frozen)
    if task.get('control_kind') and profile.id=='media_failure_diagnostician':
        from .workflow_agent_contracts import Diagnosis
        frozen['result_schema']=Diagnosis.model_json_schema()
        frozen['result_contract']='Return {"action":"result","snapshot_digest":the frozen digest,"diagnosis":{facts:[],uncertainties:[],recommendations:[]}}. Do not create files for this controller request.'
    elif task.get('control_kind') and profile.id!='media_planner':
        from .workflow_agent_contracts import ReviewDecision
        frozen['result_schema']=ReviewDecision.model_json_schema()
        if frozen.get('review_scope')=='task':
            frozen['result_contract']='Review only this step goal, outputs and review_criteria. This is not final delivery review. Do not reject an intermediate result for missing future steps, subtitles, logos or final packaging outside this step contract. Every evidence.criterion_index selects one review_criteria item; pass must cover all indexes. Use inspection_id from inspect and preserve all current step requirements.'
        else:
            frozen['review_criteria']=frozen['delivery_targets']
            frozen['result_contract']='Final delivery review: inspect every selected deliverable and satisfy all delivery_targets and the full goal. Use criterion_index and inspection_id only. deliverables and primary contain inspection_id values; the server resolves immutable references. Respect the requested main output; if none is requested, prefer main video over covers/reports and PNG over equivalent JPEG. A step report is not the main deliverable.'
    elif profile.output_schema=='visual-report-v1':
        from .workflow_agent_contracts import VisualReport
        frozen['result_schema']=VisualReport.model_json_schema()
    elif profile.output_schema=='diagnosis-v1':
        from .workflow_agent_contracts import Diagnosis
        frozen['result_schema']=Diagnosis.model_json_schema()
    if not task.get('control_kind'):
        frozen['result_contract']='This is a business Task, not an internal controller review. Submit action=result with files mapping declared output filenames to UTF-8 strings. JSON reports must be JSON-encoded strings matching result_schema. After a successful script action, files={} keeps the declared binary outputs. Do not use the internal decision envelope. inspect accepts only inspectable_refs; validate newly written outputs inside the script, then submit them for independent acceptance.'
    frozen['inspectable_refs']=list(allowed);frozen['profile']={'id':profile.id,'tools':profile.tools,
        'max_images':profile.max_images,'max_turns':profile.max_turns,
        'max_inspection_rounds':profile.max_inspection_rounds,'max_correction_rounds':profile.max_correction_rounds,
        'finalize_rounds':profile.finalize_rounds}
    conversation=graph.encode(frozen);seen=set();inspections={};inspection_keys=set();images=[];deadline=time.monotonic()+min(profile.timeout_seconds,task['spec'].get('timeout_seconds',profile.timeout_seconds))
    output=None;inspection_rounds=0;correction_rounds=0;finalize_started=False
    for turn in range(profile.max_turns):
        lease.check()
        if host.stop.is_set() or time.monotonic()>deadline:raise LeaseLost('Agent stopped or timed out')
        remaining_calls=profile.max_turns-turn
        if (remaining_calls<=profile.finalize_rounds or inspection_rounds>=profile.max_inspection_rounds
                or correction_rounds>=profile.max_correction_rounds):
            finalize_started=True
        phase='finalize' if finalize_started else 'inspect_or_prepare'
        budget={'phase':phase,'model_calls_remaining':remaining_calls,'inspection_rounds_remaining':max(0,profile.max_inspection_rounds-inspection_rounds),
                'correction_rounds_remaining':max(0,profile.max_correction_rounds-correction_rounds),'finalize_calls_reserved':min(profile.finalize_rounds,remaining_calls),
                'required_review_criteria':frozen.get('review_criteria',[])}
        if turn:
            conversation+='\nCurrent server-owned phase budget:'+graph.encode(budget)
            if finalize_started:conversation+='\nFinalize now. Submit proposal/result from existing evidence. inspect is disabled in finalize phase.'
        from .telemetry import emit
        emit('workflow.agent_phase_budget',task_id=lease.turn_id,step_id=lease.step_id,operation=phase,
             remaining_calls=remaining_calls,remaining_inspections=budget['inspection_rounds_remaining'],remaining_corrections=budget['correction_rounds_remaining'])
        try:reply=model(host,lease,run,profile,conversation,images,deadline=deadline)
        except AgentReplyError as exc:
            from .telemetry import emit
            emit('workflow.agent_contract_rejected','WARNING',task_id=lease.turn_id,step_id=lease.step_id,
                 operation='reply',error_code='invalid_json')
            store.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,run['id'],task['id'],task['revision'],task['attempts'],
                 graph.encode({'phase':'reply_validation','origin':'model_reply','classification':'parameters',
                               'attempt_id':lease.attempt,'code':'invalid_json','characters':len(exc.reply),'error_offset':exc.offset}),store.now()))
            conversation+='\nInvalid previous public reply (data, no action executed):'+graph.encode(exc.reply)
            conversation+='\nReturn exactly one JSON action object. Escape quotes/newlines inside files strings. Do not invent outcomes or broaden the task.'
            correction_rounds+=1
            continue
        action=reply.get('action')
        if action not in profile.tools:
            from .telemetry import emit
            emit('workflow.agent_contract_rejected','WARNING',task_id=lease.turn_id,step_id=lease.step_id,
                 operation='reply',error_code='unsupported_action')
            conversation+='\nRejected reply (no action executed):'+graph.encode(reply)
            conversation+='\nUse exactly one action from the fixed profile tools: '+graph.encode(profile.tools)
            correction_rounds+=1
            continue
        from .task_usage import execution_call
        labels={'inspect':'检查已授权输入','contract':'查询冻结能力契约','script':'执行受限脚本','proposal':'提交规划提案','result':'提交候选结果'}
        with execution_call(lease,'tool',action,revision=task['revision'],actor=actor(profile),label=labels[action]) as tool_receipt:
            if action=='contract':
                if profile.id!='media_planner':raise ValueError('Only Manager can query operation contracts')
                identifier=reply.get('operation')
                try:detail=planning.operation_contract(store,frozen,identifier)
                except ValueError as exc:
                    tool_receipt['status']='failed';correction_rounds+=1
                    conversation+='\nContract query rejected: '+str(exc);continue
                conversation+='\nFrozen operation contract (data, no authority granted):'+graph.encode(detail)
                continue
            if action=='proposal':
                if profile.id!='media_planner':raise ValueError('Only Manager can propose')
                errors=proposal_feedback(reply.get('proposal'))
                if errors:
                    tool_receipt['status']='failed'
                    from .telemetry import emit
                    emit('workflow.agent_contract_rejected','WARNING',task_id=lease.turn_id,step_id=lease.step_id,
                         operation=errors[0]['path'] or action,error_code=errors[0]['type'],failures=len(errors))
                    store.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex,run['id'],task['id'],task['revision'],task['attempts'],
                         graph.encode({'phase':'proposal_validation','origin':'model_proposal','classification':'parameters',
                                       'attempt_id':lease.attempt,'validation_errors':errors}),store.now()))
                    conversation+='\nRejected proposal (not applied):'+graph.encode(reply.get('proposal'))
                    conversation+='\nSchema validation errors. Correct the proposal within the same frozen schema and budget:'+graph.encode(errors)
                    correction_rounds+=1
                    continue
                try:planning.stage(store,lease,task['spec']['request_id'],reply['proposal'])
                except planning.ProposalReferenceError as exc:
                    tool_receipt['status']='failed'
                    store.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex,run['id'],task['id'],task['revision'],task['attempts'],
                         graph.encode({'phase':'proposal_validation','origin':'model_proposal','classification':'parameters',
                                       'attempt_id':lease.attempt,'code':'unknown_input_asset'}),store.now()))
                    conversation+='\nRejected proposal before staging: '+str(exc)+' Correct the reference within the frozen scope and budget.'
                    continue
                except planning.ProposalIdentityError:
                    tool_receipt['status']='failed'
                    from .telemetry import emit
                    emit('workflow.agent_contract_rejected','WARNING',task_id=lease.turn_id,step_id=lease.step_id,
                         operation='proposal',error_code='proposal_id_reused')
                    conversation+='\nProposal ID already used by another request. Submit the corrected proposal with proposal_id='+task['spec']['request_id']+'. Nothing was staged.'
                    continue
                return stage_result(store,lease,'succeeded',{'result_kind':reply['proposal']['kind']})
            if action=='inspect':
                if finalize_started:
                    tool_receipt['status']='failed';correction_rounds+=1
                    conversation+='\nInspection rejected: finalize phase has started. Submit the required result/proposal using existing evidence.'
                    continue
                raw_refs=reply.get('refs')
                requests=raw_refs if raw_refs is not None else [{'ref':reply.get('ref'),'offset':reply.get('offset',0)}]
                if (not isinstance(requests,list) or not 1<=len(requests)<=8 or
                        any(not isinstance(item,dict) or set(item)-{'ref','offset'} or not isinstance(item.get('ref'),str) or item['ref'] not in allowed for item in requests)):
                    tool_receipt['status']='failed'
                    from .telemetry import emit
                    emit('workflow.agent_tool_denied','WARNING',task_id=lease.turn_id,step_id=lease.step_id,
                         operation='inspect',error_code='outside_frozen_scope')
                    conversation+='\nInspection denied before reading: ref must exactly equal one of inspectable_refs (asset ID or full frozen result reference), not a role, filename, or invented path.'
                    correction_rounds+=1
                    continue
                keys=[(item['ref'],item.get('offset',0)) for item in requests]
                if len(set(keys))!=len(keys) or any(key in inspection_keys for key in keys):
                    tool_receipt['status']='failed';correction_rounds+=1
                    conversation+='\nInspection rejected: the same immutable ref and offset was already inspected. Use existing evidence, choose a new bounded offset, or finalize.'
                    continue
                batch=[];inspection_rounds+=1;inspection_keys.update(keys)
                for request in requests:
                    reference=request['ref']
                    try:
                        evidence=inspect(store,run,allowed,reference,request.get('offset',0))
                        if evidence.get('media'):evidence=inspect_media(host,lease,run,task,evidence,
                            remaining_images=profile.max_images-len(images))
                    except InspectionInputError as exc:
                        tool_receipt['status']='failed'
                        store.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',
                            (uuid.uuid4().hex,run['id'],task['id'],task['revision'],task['attempts'],
                             graph.encode({'phase':'inspect','origin':'inspection_tool','classification':'parameters',
                                           'attempt_id':lease.attempt,'code':exc.code,
                                           'reference_sha256':hashlib.sha256(reference.encode()).hexdigest()}),store.now()))
                        batch.append({'ref':reference,'error':str(exc),'code':exc.code});continue
                    if evidence.get('media') and 'image' in evidence:
                        images.append(evidence.pop('image'));evidence['image_index']=len(images)
                        evidence['remaining_images']=profile.max_images-len(images)
                    inspection_id=uuid.uuid4().hex
                    store.execute('INSERT INTO workflow_inspections VALUES (?,?,?,?,?,?,?,?)',
                        (inspection_id,run['id'],task['id'],lease.attempt,reference,frozen.get('snapshot_digest',''),
                         graph.encode({k:v for k,v in evidence.items() if k not in ('text','probe')}),store.now()))
                    seen.add(reference);inspections[inspection_id]=reference;batch.append({'inspection_id':inspection_id,**evidence})
                conversation+='\nInspection result (data):'+graph.encode(batch[0] if len(batch)==1 else batch)
            elif action=='script':
                if 'RUN_SCRIPT' not in profile.action_capabilities:raise ValueError('Profile is read-only')
                output=run_script(host,lease,run,task,reply.get('code'),output)
                conversation+='\nScript completed. Submit result with files={} for the declared outputs; independent acceptance will inspect their bytes.'
            elif action=='result':
                try:
                    if task.get('control_kind'):result=validated_internal_result(run,task,profile,reply,frozen,inspections)
                    else:validate_result_files(host.store,lease,task,reply.get('files',{}))
                except (ValueError,TypeError) as exc:
                    tool_receipt['status']='failed'
                    from pydantic import ValidationError
                    errors=([{'path':'.'.join(map(str,e['loc'])),'type':e['type'],'message':e['msg'][:240]}
                             for e in exc.errors(include_input=False,include_url=False)[:20]] if isinstance(exc,ValidationError)
                            else [{'path':'result','type':type(exc).__name__,'message':str(exc)[:400]}])
                    from .telemetry import emit
                    emit('workflow.agent_contract_rejected','WARNING',task_id=lease.turn_id,step_id=lease.step_id,
                         operation='result',error_code=errors[0]['type'],failures=len(errors))
                    store.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex,run['id'],task['id'],task['revision'],task['attempts'],
                         graph.encode({'phase':'result_validation','origin':'model_result','classification':'parameters',
                                       'attempt_id':lease.attempt,'validation_errors':errors}),store.now()))
                    conversation+='\nRejected result (not committed):'+graph.encode(reply)
                    conversation+='\nCorrect the result within the existing contract. Completed inspection IDs:'+graph.encode(sorted(inspections))+'; validation errors:'+graph.encode(errors)
                    correction_rounds+=1
                    continue
                if task.get('control_kind'):return stage_result(store,lease,'succeeded',result)
                return finish_files(host,lease,run,task,reply.get('files',{}),output)
    raise TaskQuotaReached('专业 Agent 已达到本轮操作上限。',category='agent_turns',scope='turn',used=profile.max_turns,limit=profile.max_turns)


def validated_internal_result(run,task,profile,reply,frozen,inspections):
    if profile.id=='media_failure_diagnostician':
        from .workflow_agent_contracts import Diagnosis
        if reply.get('snapshot_digest')!=frozen['snapshot_digest']:raise ValueError('Diagnostic snapshot mismatch')
        diagnosis=Diagnosis.model_validate(reply.get('diagnosis',{})).model_dump()
        return {'snapshot_digest':frozen['snapshot_digest'],'diagnosis':diagnosis}
    from .workflow_agent_contracts import ReviewDecision
    decision=ReviewDecision.model_validate(reply.get('decision',{})).model_dump()
    if decision['snapshot_digest']!=frozen['snapshot_digest']:raise ValueError('Review snapshot mismatch')
    from .workflow_agent_contracts import validate_review_scope
    validate_review_scope(decision,frozen)
    if decision['verdict']=='pass':
        evidence=decision['evidence']
        if not evidence:raise ValueError('Passing review requires evidence')
    from .workflow_agent_contracts import resolve_review
    return {'decision':resolve_review(decision,frozen,inspections)}


def output_directory(host,lease,run,task,output=None):
    if lease.runtime is None:lease.runtime=lease.runtime_factory()
    adapter=host.workflow_runner.adapter;view=graph.execution_view(run);env=adapter.environment(view)
    output=output or f'edit/workflows/{run["id"]}/{task["id"]}/{uuid.uuid4().hex}'
    folder=env['workspace']/output;folder.mkdir(parents=True,mode=0o700,exist_ok=True)
    parent=folder
    while parent!=env['workspace']:
        if parent.is_symlink():raise ValueError('Task output directory is a link')
        os.chown(parent,env['uid'],env['gid']);parent.chmod(0o700);parent=parent.parent
    return output,env


def run_script(host,lease,run,task,code,output=None):
    if not isinstance(code,str) or not 1<=len(code)<=64000:raise ValueError('Script exceeds profile limit')
    output,env=output_directory(host,lease,run,task,output)
    from .workflow_inputs import resolve
    view=graph.execution_view(run);step=next(s for s in view['steps'] if s['id']==task['id'])
    bound=resolve(host.store,view,step,env)
    argv=['python','-c','import os\nos.chdir(os.environ["VIDEO_USE_STEP_OUTPUT"])\n'+code]
    host.workflow_runner.adapter.isolated(dict(env,workspace_policy='TASK_OUTPUT_ONLY',step_output=output),
        {'mode':'local','output':output,'argv':argv,'outputs':list(task['spec']['output_roles'].values()),
         'inputs':{name:[record['path']] for name,record in bound.items()}},
        min(task['spec']['timeout_seconds'],600),lambda:host.stop.is_set() or lease.lost.is_set())
    return output


def validate_result_files(store,lease,task,files):
    if not isinstance(files,dict):raise ValueError('Result files must be a mapping')
    declared=set(task['spec']['output_roles'].values())
    if not set(files)<=declared:raise ValueError('Undeclared Agent output')
    profile=get_profile(task['spec']['profile'])
    if profile.output_schema=='diagnosis-v1':
        from .workflow_agent_contracts import Diagnosis
        if set(files)!=declared or any(not name.endswith('.json') for name in files):raise ValueError('Diagnosis requires the declared JSON outputs')
        for value in files.values():Diagnosis.model_validate(json.loads(value))
    if profile.output_schema=='visual-report-v1':
        from .workflow_agent_contracts import VisualReport
        if set(files)!=declared or any(not name.endswith('.json') for name in files):raise ValueError('Visual reports require declared JSON outputs')
        seen={row['id'] for row in store.query('SELECT id FROM workflow_inspections WHERE attempt_id=? AND task_id=?',(lease.attempt,task['id']))}
        for value in files.values():
            report=VisualReport.model_validate(json.loads(value))
            if report.verdict=='pass' and any(e.inspection_id not in seen for e in report.evidence):raise ValueError('Visual report evidence must use a completed inspection_id')
    for text in files.values():
        if not isinstance(text,str) or len(text.encode())>256000:raise ValueError('Agent result files must contain bounded UTF-8 strings')
    return declared


def finish_files(host,lease,run,task,files,output=None):
    declared=validate_result_files(host.store,lease,task,files)
    output,env=output_directory(host,lease,run,task,output)
    if files:
        records=[]
        for name,text in files.items():
            if not isinstance(text,str) or len(text.encode())>256000:raise ValueError('Agent file exceeds limit')
            records.append({'name':name,'data':base64.b64encode(text.encode()).decode()})
        host.workflow_runner.adapter.isolated(dict(env,workspace_policy='TASK_OUTPUT_ONLY',step_output=output),{'mode':'import','output':output,'files':records},60,lambda:lease.lost.is_set())
    paths=[output+'/'+name for name in declared]
    view=graph.execution_view(run);step=next(s for s in view['steps'] if s['id']==task['id'])
    from .workflow_executor import validate_candidate
    report=validate_candidate(host.workflow_runner.adapter,view,step,paths)
    lease.runtime.checkpoint()
    checkpoint=host.store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                                (run['id'],task['id'],task['revision']))[0]
    return stage_result(host.store,lease,'succeeded',{'files':paths,'quality':report,
        'output_roles':{role:output+'/'+name for role,name in task['spec']['output_roles'].items()}},json.loads(checkpoint['manifest']))


STILL_THUMBNAIL='''import sys
from PIL import Image,ImageOps
with Image.open(sys.argv[1]) as source:
    if source.width*source.height>32000000 or getattr(source,'n_frames',1)!=1:
        raise ValueError('Still image inspection limit')
    pixels=ImageOps.exif_transpose(source).convert('RGBA')
    pixels.thumbnail((256,256),Image.Resampling.LANCZOS)
    matte=Image.new('RGBA',pixels.size,(184,190,200,255))
    matte.alpha_composite(pixels)
    matte.convert('RGB').save(sys.stdout.buffer,format='JPEG',quality=80)
'''


def inspect_media(host,lease,run,task,evidence,remaining_images=None):
    # Pixel/video inspection must be performed by the existing fixed media
    # sandbox, never inferred from the filename or metadata.
    from . import agent_sandbox
    import subprocess
    if lease.runtime is None:lease.runtime=lease.runtime_factory()
    env=host.workflow_runner.adapter.environment(graph.execution_view(run))
    reference=evidence['ref'];path=evidence.pop('path')
    if reference in {a['id'] for a in env['assets']}:
        asset=next(a for a in env['assets'] if a['id']==reference)
        relative='sources/'+asset['filename']
    else:relative=reference
    result=subprocess.run(agent_sandbox.command(dict(env,workspace_policy='REVIEW_SCRATCH_ONLY'),None,
        ['/usr/bin/ffprobe','-v','error','-show_format','-show_streams','-of','json','/workspace/'+relative]),
        capture_output=True,timeout=30)
    if result.returncode:raise MediaInspectionError('inspection_probe_failed',result.returncode,reference)
    if len(result.stdout)>65536:raise ValueError('Media metadata exceeds limit')
    probe=json.loads(result.stdout)
    videos=[s for s in probe.get('streams',[]) if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')]
    if videos:
        if remaining_images is not None and remaining_images<=0:
            raise InspectionInputError('Inspection image budget exhausted; no image decoded or evidence recorded. '
                'Use the already inspected images, inspect text/JSON/audio metadata, or report inconclusive. '
                'Never claim that an uninspected image was checked.', 'inspection_image_budget')
        if not evidence.get('still_image'):
            video=videos[0]
            try:duration=float(video.get('duration') or probe.get('format',{}).get('duration') or 0)
            except (ValueError,TypeError):duration=0
            if duration>0 and evidence['offset_seconds']>=duration:
                raise InspectionInputError(f'Video timestamp must be below duration {duration:g} seconds; choose an earlier or fractional timestamp')
        # Seeking even to zero can produce no frame for JPEG image2 inputs.
        # Still images have one view; timestamp seeking applies only to video.
        seek=[] if evidence.get('still_image') else ['-ss',str(evidence['offset_seconds'])]
        arguments=(['/opt/venv/bin/python','-c',STILL_THUMBNAIL,'/workspace/'+relative]
            if evidence.get('still_image') else
            ['/usr/bin/ffmpeg','-v','error','-threads','1','-filter_threads','1',*seek,'-i','/workspace/'+relative,
             '-frames:v','1','-vf','scale=256:256:force_original_aspect_ratio=decrease','-q:v','5',
             '-threads','1','-f','image2pipe','-vcodec','mjpeg','pipe:1'])
        sampled=subprocess.run(agent_sandbox.command(dict(env,workspace_policy='REVIEW_SCRATCH_ONLY'),None,arguments),capture_output=True,timeout=30)
        if not sampled.returncode and not sampled.stdout and not evidence.get('still_image'):
            raise InspectionInputError('No frame at that timestamp; inspect an earlier position, not the exact end of the video')
        if sampled.returncode or not sampled.stdout or len(sampled.stdout)>65536:
            raise MediaInspectionError('inspection_frame_failed' if sampled.returncode else 'inspection_frame_empty' if not sampled.stdout else 'inspection_frame_oversized',sampled.returncode,reference)
        return {**evidence,'probe':probe,'coverage':'still_image' if evidence.get('still_image') else 'sampled_frame',
                'preview_background':'#b8bec8' if evidence.get('still_image') else None,'timestamp_seconds':evidence['offset_seconds'],
                'image':'data:image/jpeg;base64,'+base64.b64encode(sampled.stdout).decode()}
    return {**evidence,'probe':probe,'coverage':'technical_metadata_only',
            'limitation':'Audio content has not been listened to; use an authorized transcript for dialogue criteria.'}
