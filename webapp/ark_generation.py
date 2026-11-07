"""Seedance protocol only. Scheduling, intent, budgets and recovery stay in workflow."""
import base64
import hashlib
import io
import os
import re
from urllib.parse import urlsplit

from PIL import Image
from .egress_policy import provider_base
from .generation_errors import raise_for_status
from .generation_auth import credential_binding,frozen_headers

PROTOCOL = 'ark-video-v1'
OPERATIONS = ('video.generate_from_image', 'video.generate_from_text')
SIZES = ((1280, 720), (720, 1280), (960, 960))


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
        name: {'durations': list(range(4, 16)), 'sizes': [list(s) for s in SIZES],
               'reference_image': name == 'video.generate_from_image',
               'audio': False, 'reference_transport': 'inline_first_frame',
               'reference_restrictions': '当前接入点拒绝写实人物参考图；人物图生视频请使用已配置的 Wan 适配器',
               'duration_tolerance_seconds': 0.05,
               'cancellation': 'queued_only'} for name in OPERATIONS}}


def validate(operation, parameters):
    spec = capabilities()['operations'].get(operation)
    if not spec:
        raise ValueError('Seedance 适配器不支持该操作')
    if parameters['duration'] not in spec['durations']:
        raise ValueError('当前 Seedance 接口支持 4–15 秒整数时长')
    if [parameters['width'], parameters['height']] not in spec['sizes']:
        raise ValueError('当前 Seedance 接口支持 1280×720、720×1280 或 960×960')
    return spec


def prepare(operation, parameters, reference=None):
    validate(operation, parameters)
    base, key, model = configuration()
    if (reference is not None) != (operation == 'video.generate_from_image'):
        raise ValueError('图生视频必须有唯一参考图；文生视频不能夹带参考图')
    content = [{'type': 'text', 'text': parameters['prompt']}]
    reference_hash = None
    if reference is not None:
        if len(reference) > 20 * 1024**2:
            raise ValueError('参考图片过大')
        with Image.open(io.BytesIO(reference)) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('参考图必须是 PNG/JPEG/WebP')
            if min(image.size) < 300 or max(image.size) > 6000:
                raise ValueError('参考图边长必须在 300–6000 像素')
            mime = Image.MIME[image.format]
            image.verify()
        reference_hash = hashlib.sha256(reference).hexdigest()
        content.append({'type': 'image_url', 'role': 'first_frame',
                        'image_url': {'url': 'data:' + mime + ';base64,' + base64.b64encode(reference).decode()}})
    ratio = '16:9' if parameters['width'] > parameters['height'] else '9:16' if parameters['width'] < parameters['height'] else '1:1'
    payload = {'model': model, 'content': content, 'duration': int(parameters['duration']),
               'ratio': ratio, 'resolution': '720p', 'generate_audio': False,
               'watermark': False, 'seed': parameters.get('seed', 1)}
    prepared={'provider': base, 'protocol': PROTOCOL, 'model': model, 'operation': operation,
              'reference_sha256': reference_hash, 'payload': payload}
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
    response = client.post(prepared['provider'] + '/contents/generations/tasks',
                           json=prepared['payload'], headers=auth(prepared),
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
