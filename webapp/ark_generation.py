"""Seedance protocol only. Scheduling, intent, budgets and recovery stay in workflow."""
import hashlib
import io
import os
import re
from urllib.parse import urlsplit

import av
from PIL import Image
from .ark_reference_assets import configured as references_configured,prepare as prepare_reference_assets
from .egress_policy import provider_base
from .generation_errors import raise_for_status
from .generation_auth import credential_binding,frozen_headers

PROTOCOL = 'ark-video-v1'
OPERATIONS = ('video.generate_from_image', 'video.generate_from_text',
    'video.generate_from_frames', 'video.generate_from_references', 'video.edit', 'video.extend')
SIZES = ((1280, 720), (720, 1280), (960, 960))
MODES = {'video.generate_from_text':'text_to_video','video.generate_from_image':'first_frame',
    'video.generate_from_frames':'first_last_frame','video.generate_from_references':'multimodal_reference',
    'video.edit':'edit_video','video.extend':'extend_video'}


def configuration():
    base = provider_base(os.environ.get('VIDEO_USE_GENERATION_URL', ''))
    key = os.environ.get('VIDEO_USE_GENERATION_KEY', '')
    model = os.environ.get('VIDEO_USE_GENERATION_MODEL', '')
    if not key or not model:
        raise ValueError('Seedance 服务缺少凭据或模型配置')
    return base, key, model


def capabilities():
    base, _, model = configuration()
    return {'protocol': PROTOCOL, 'model': model, 'operations': {
        name: {'durations': [-1] if name == 'video.edit' else list(range(4, 16)),
               'sizes': [] if name in ('video.edit','video.extend') else [list(s) for s in SIZES],
               'generation_mode': MODES[name],
               'reference_image': name == 'video.generate_from_image',
               'reference_inputs': name not in ('video.generate_from_text',),
               'reference_inputs_available': references_configured() if name != 'video.generate_from_text' else True,
               'audio': True, 'reference_transport': 'ark_asset' if name != 'video.generate_from_text' else 'none',
               'reference_restrictions': '当前接入点拒绝写实人物参考图；人物图生视频请使用已配置的 Wan 适配器',
               'duration_tolerance_seconds': 0.05,
               'cancellation': 'queued_only'} for name in OPERATIONS}}


def validate(operation, parameters):
    spec = capabilities()['operations'].get(operation)
    if not spec:
        raise ValueError('Seedance 适配器不支持该操作')
    duration = -1 if operation == 'video.edit' else parameters['duration']
    if duration not in spec['durations']:
        raise ValueError('当前 Seedance 接口支持 4–15 秒整数时长')
    if spec['sizes'] and [parameters['width'], parameters['height']] not in spec['sizes']:
        raise ValueError('当前 Seedance 接口支持 1280×720、720×1280 或 960×960')
    if operation == 'video.extend' and parameters.get('extend_direction') not in ('forward','backward'):
        raise ValueError('视频延长需要 forward 或 backward 方向')
    return spec


def _media(reference, role):
    data = reference['bytes']
    if not isinstance(data,bytes) or not data or len(data)>200*1024**2:
        raise ValueError('参考素材为空或超过 200MB')
    kind = 'image' if role in ('first_frame','last_frame') or role.startswith('reference_image_') else (
        'video' if role == 'source' or role.startswith('reference_video_') else 'audio')
    digest = hashlib.sha256(data).hexdigest()
    supplied = reference.get('sha256')
    if supplied and supplied != digest:raise ValueError('参考素材摘要不一致')
    if kind == 'image':
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('PNG','JPEG','WEBP'):raise ValueError('参考图必须是 PNG/JPEG/WebP')
            width,height=image.size;mime=Image.MIME[image.format];image.verify()
        if len(data)>=30*1024**2 or not 300<width<6000 or not 300<height<6000 or not .4<width/height<2.5:
            raise ValueError('Seedance 参考图尺寸或大小超出限制')
        return {'bytes':data,'sha256':digest,'size':len(data),'kind':kind,'mime':mime,
            'width':width,'height':height,'duration_seconds':None,'fps':None,'codec':'','audio_codec':''}
    try:
        with av.open(io.BytesIO(data)) as container:
            video=next((stream for stream in container.streams if stream.type=='video'),None)
            audio=next((stream for stream in container.streams if stream.type=='audio'),None)
            stream=video if kind=='video' else audio
            if stream is None:raise ValueError('参考素材缺少声明的媒体轨道')
            duration=float(container.duration/av.time_base) if container.duration else (
                float(stream.duration*stream.time_base) if stream.duration is not None else 0)
            fps=float(video.average_rate) if video is not None and video.average_rate else None
            codec=stream.codec_context.name or ''
            audio_codec=audio.codec_context.name if audio is not None else ''
            width,height=(video.codec_context.width,video.codec_context.height) if video is not None else (None,None)
            format_names=set(container.format.name.split(','))
    except av.error.FFmpegError as exc:
        raise ValueError('参考音视频无法解码') from exc
    if kind=='video':
        if not 2<=duration<=15 or len(data)>200*1024**2:raise ValueError('参考视频必须为 2–15 秒且不超过 200MB')
        if (not width or not height or not 300<=width<=6000 or not 300<=height<=6000
                or not .4<=width/height<=2.5 or not 407696<=width*height<=8295044):
            raise ValueError('参考视频尺寸超出限制')
        if fps is None or not 24<=fps<=60:raise ValueError('参考视频帧率必须为 24–60 FPS')
        if codec not in ('h264','hevc'):raise ValueError('参考视频必须使用 H.264 或 H.265')
        if audio_codec and audio_codec not in ('aac','mp3'):raise ValueError('参考视频音频必须使用 AAC 或 MP3')
        mime='video/mp4' if format_names.intersection({'mov','mp4','m4a','3gp','3g2','mj2'}) else 'video/quicktime'
    else:
        if not 2<=duration<=15 or len(data)>15*1024**2:raise ValueError('参考音频必须为 2–15 秒且不超过 15MB')
        if codec not in ('mp3','pcm_s16le','pcm_s24le','pcm_f32le'):
            raise ValueError('参考音频必须为 MP3 或 PCM WAV')
        mime='audio/mpeg' if codec=='mp3' else 'audio/wav'
    return {'bytes':data,'sha256':digest,'size':len(data),'kind':kind,'mime':mime,
        'width':width,'height':height,'duration_seconds':duration,'fps':fps,
        'codec':codec,'audio_codec':audio_codec}


def _references(operation, reference):
    if operation=='video.generate_from_text':
        if reference is not None:raise ValueError('文生视频不能夹带参考素材')
        return []
    if isinstance(reference,bytes):reference={'source':{'bytes':reference}}
    if not isinstance(reference,dict):raise ValueError('该视频生成模式需要声明参考素材')
    expected={'video.generate_from_image':{'source'},'video.generate_from_frames':{'first_frame','last_frame'}}.get(operation)
    if expected is not None and set(reference)!=expected:raise ValueError('视频生成参考角色不完整或包含多余角色')
    values=[]
    for role,item in reference.items():
        if not isinstance(item,dict) or 'bytes' not in item:raise ValueError('参考素材必须由已绑定项目文件提供')
        media_role='first_frame' if operation=='video.generate_from_image' else role
        media=_media(item,media_role)
        if operation=='video.generate_from_image':provider_role,usage='first_frame','composition'
        elif operation=='video.generate_from_frames':provider_role,usage=role,'composition'
        elif role=='source':provider_role,usage='reference_video',('edit_source' if operation=='video.edit' else 'extend_source')
        elif role.startswith('reference_image_'):provider_role,usage='reference_image','subject'
        elif role.startswith('reference_video_'):provider_role,usage='reference_video','action'
        elif role.startswith('reference_audio_'):provider_role,usage='reference_audio','audio'
        else:raise ValueError('未知视频参考角色')
        values.append({**media,'input_role':role,'role':provider_role,'usage':usage})
    images=[item for item in values if item['kind']=='image'];videos=[item for item in values if item['kind']=='video'];audios=[item for item in values if item['kind']=='audio']
    if len(images)>9 or len(videos)>3 or len(audios)>3:raise ValueError('参考素材数量超过 Seedance 限制')
    if sum(item['size'] for item in images+audios)>45*1024**2:raise ValueError('图片和音频参考合计不能超过 45MB')
    if sum(item['duration_seconds'] for item in videos)>15:raise ValueError('参考视频总时长不能超过 15 秒')
    if sum(item['duration_seconds'] for item in audios)>15:raise ValueError('参考音频总时长不能超过 15 秒')
    if operation=='video.generate_from_references' and audios and not (images or videos):
        raise ValueError('音频参考必须同时包含图片或视频参考')
    source_usage={'video.edit':'edit_source','video.extend':'extend_source'}.get(operation)
    if source_usage:
        sources=[item for item in values if item['usage']==source_usage]
        if len(sources)!=1 or sources[0]['kind']!='video':raise ValueError('视频编辑或延长需要唯一 source 视频')
        values.sort(key=lambda item:0 if item['usage']==source_usage else 1)
    return values


def prepare(operation, parameters, reference=None):
    validate(operation, parameters)
    base, key, model = configuration()
    references=_references(operation,reference);mode=MODES[operation]
    prompt=parameters['prompt']
    if mode=='edit_video':prompt='严格编辑视频1，未提及的内容保持不变。'+prompt
    elif mode=='extend_video':prompt=('向前' if parameters['extend_direction']=='forward' else '向后')+'延长视频1。'+prompt
    ratio='adaptive' if mode in ('edit_video','extend_video') else ('16:9' if parameters['width'] > parameters['height'] else '9:16' if parameters['width'] < parameters['height'] else '1:1')
    payload = {'model': model, 'content': [{'type':'text','text':prompt}],
               'duration': -1 if mode=='edit_video' else int(parameters['duration']),
               'ratio': ratio, 'resolution': '720p', 'generate_audio': bool(parameters.get('generate_audio',False)),
               'watermark': False, 'seed': parameters.get('seed', 1)}
    prepared={'provider': base, 'protocol': PROTOCOL, 'model': model, 'operation': operation,
              'generation_mode':mode,'reference_sha256':references[0]['sha256'] if references else None,
              'reference_sha256s':{item['input_role']:item['sha256'] for item in references},
              'references':references,'payload':payload}
    prepared['credential_binding']=credential_binding(prepared,key)
    return prepared


def auth(handle):
    return frozen_headers(handle,PROTOCOL,'VIDEO_USE_GENERATION_KEY')


def task_url(handle, identifier):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identifier):
        raise ValueError('Invalid provider task ID')
    return handle['provider'] + '/contents/generations/tasks/' + identifier


def submit(client, prepared, request_key):
    # No implicit network retries; a client request key is NOT an idempotency guarantee.
    base,_,model=configuration()
    if (prepared['provider'],prepared['model'])!=(base,model):raise ValueError('Provider configuration changed before submission')
    payload={**prepared['payload']}
    content=list(payload['content'])
    for reference in prepare_reference_assets(client,prepared.get('references') or []):
        uri=reference['asset_uri'];role=reference['role']
        if role in ('reference_image','first_frame','last_frame'):
            content.append({'type':'image_url','image_url':{'url':uri},'role':role})
        elif role=='reference_video':content.append({'type':'video_url','video_url':{'url':uri},'role':role})
        else:content.append({'type':'audio_url','audio_url':{'url':uri},'role':role})
    payload['content']=content
    response = client.post(prepared['provider'] + '/contents/generations/tasks',
                           json=payload, headers=auth(prepared),
                           timeout=(10, 60), allow_redirects=False)
    raise_for_status(response)
    identifier = response.json().get('id', '')
    task_url(prepared, identifier)
    return {'id': identifier, 'status': 'waiting'}


def poll(client, handle, identifier):
    response = client.get(task_url(handle, identifier), headers=auth(handle),
                          timeout=(5, 20), allow_redirects=False)
    raise_for_status(response)
    result = response.json()
    state = result.get('status')
    mapping = {'queued': 'waiting', 'running': 'running', 'succeeded': 'succeeded',
               'failed': 'failed', 'cancelled': 'cancelled', 'canceled': 'cancelled', 'expired': 'failed'}
    if state not in mapping:
        raise ValueError('上游返回未知生成状态')
    record = {'status': mapping[state], 'usage': result.get('usage'), 'cost': result.get('cost'), 'artifacts': []}
    if state == 'succeeded':
        url = (result.get('content') or {}).get('video_url') or (result.get('output') or {}).get('video_url')
        validate_download_url(url)
        record['artifacts'] = [{'id': 'video', 'name': 'video.mp4', 'url': url}]
    if state in ('failed', 'expired'):
        # Provider messages may contain signed URLs or input details; expose only a bounded code.
        code = str((result.get('error') or {}).get('code', 'generation_failed'))
        record['error'] = re.sub(r'[^a-zA-Z0-9_.-]', '', code)[:120]
    return record


def validate_download_url(url, allowed_hosts=None):
    if not isinstance(url, str):
        raise ValueError('生成服务未返回视频地址')
    parsed = urlsplit(url)
    suffixes = [s.strip().lower() for s in (allowed_hosts or os.environ.get('VIDEO_USE_GENERATION_DOWNLOAD_HOSTS', 'volces.com')).split(',') if s.strip()]
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443) or not any(host == s or host.endswith('.' + s) for s in suffixes):
        raise ValueError('上游视频下载地址不在已配置的存储域名范围内')
    return url


def download(client, artifact):
    # Signed object URL: never forward the API bearer credential to storage.
    return client.get(validate_download_url(artifact.get('url')), stream=True,
                      timeout=(10, 60), allow_redirects=False)


def cancel(client, handle, identifier):
    response = client.delete(task_url(handle, identifier), headers=auth(handle),
                             timeout=(3, 5), allow_redirects=False)
    if response.status_code == 400:
        try:code=(response.json().get('error') or {}).get('code')
        except (ValueError,AttributeError):code=None
        if code=='InvalidAction.RunningTaskDeletion':
            raise NotImplementedError('Seedance 已进入运行状态，当前接口不允许取消运行中的任务')
    raise_for_status(response)
    # Ark DELETE acknowledges deletion/cancellation; errors (e.g. running job)
    # remain unconfirmed and are handled by the bounded cleanup coordinator.
    if response.status_code != 204:
        raise ValueError('上游未明确确认任务删除或取消')
    return {'status': 'cancelled'}
