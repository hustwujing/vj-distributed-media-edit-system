"""Uniform server-side generation boundary; never owns a task pool or poll loop.

Three operation IDs share submit -> normalized result and durable video handles.
The workflow caller must persist intent before submit and own retries/cancellation.
"""
import os
import requests
from . import ark_generation, image_generation,wan_generation

TEXT_IMAGE = 'image.generate_from_text'
IMAGE_VIDEO = 'video.generate_from_image'
TEXT_VIDEO = 'video.generate_from_text'
PROVIDERS={'ark-video-v1':ark_generation,'wan-video-v1':wan_generation}


def register_provider(protocol,provider):
    if protocol in PROVIDERS:raise ValueError('Duplicate provider protocol')
    if not all(callable(getattr(provider,name,None)) for name in ('submit','poll','download')):
        raise ValueError('Provider requires submit, poll and download handlers')
    PROVIDERS[protocol]=provider


def prepare(operation, parameters, reference=None):
    if operation == TEXT_IMAGE:
        if reference is not None:
            raise ValueError('文生图不接受参考图')
        parameters = image_generation.Parameters.model_validate(parameters).model_dump()
        base, _, model = image_generation.configuration()
        return {'provider': base, 'protocol': 'openai-image-v1', 'model': model,
                'operation': operation, 'parameters': parameters}
    if operation==IMAGE_VIDEO and os.environ.get('VIDEO_USE_IMAGE_VIDEO_BACKEND')=='wan':
        return wan_generation.prepare(operation,parameters,reference)
    return ark_generation.prepare(operation, parameters, reference)


def implementation(prepared):
    """Persist routing and model, never credentials or inline image contents."""
    return {k: prepared[k] for k in ('provider', 'protocol', 'model', 'operation', 'reference_sha256') if k in prepared}


def submit(client, prepared, request_key):
    if prepared['protocol'] == 'openai-image-v1':
        base,_,model=image_generation.configuration()
        if (base,model)!=(prepared['provider'],prepared['model']):raise ValueError('文生图配置在提交前发生变化')
        raw, metadata = image_generation.generate(prepared['parameters'])
        return {'status': 'succeeded', 'id': None, 'artifacts': [{'name': 'image.png', 'bytes': raw}], **metadata}
    return adapter(prepared).submit(client, prepared, request_key)


def adapter(handle):
    if handle.get('protocol') not in PROVIDERS:raise ValueError('Unknown generation protocol')
    return PROVIDERS[handle['protocol']]


def poll(client, handle, identifier):
    return adapter(handle).poll(client, handle, identifier)


def download(client, artifact,handle=None):
    try:return adapter(handle or {'protocol':'ark-video-v1'}).download(client, artifact)
    except requests.RequestException:
        raise RuntimeError('生成产物下载连接失败，请查询原任务后重新下载') from None


def cancel(client, handle, identifier):
    provider=adapter(handle)
    if not callable(getattr(provider,'cancel',None)):raise NotImplementedError('Provider does not support cancellation')
    return provider.cancel(client, handle, identifier)
