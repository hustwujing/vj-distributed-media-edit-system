"""Opt-in generation job protocol, never guessed from a provider URL.

The configured server MUST advertise operations and parameter limits at
GET /api/capabilities and implement /api/jobs + artifact IDs. Unsupported
providers are explicitly unavailable until a specific adapter is implemented.
"""
import os
from .egress_policy import provider_base,session


def endpoint():
    value=os.environ.get('VIDEO_USE_GENERATION_URL','')
    if not value:raise ValueError('文生图/图生视频服务未配置')
    return provider_base(value)


def headers():
    key=os.environ.get('VIDEO_USE_GENERATION_KEY','')
    return {'Authorization':'Bearer '+key} if key else {}


def capabilities():
    if os.environ.get('VIDEO_USE_GENERATION_BACKEND','jobs')=='ark':
        from .ark_generation import capabilities as ark_capabilities
        return ark_capabilities()
    with session() as client:
        response=client.get(endpoint()+'/api/capabilities',headers=headers(),timeout=(5,15),allow_redirects=False)
        response.raise_for_status();value=response.json()
    if value.get('protocol')!='video-use-jobs-v1':raise ValueError('生成服务协议尚未适配，不能提交')
    return value


def validate(operation,parameters):
    if operation=='video.generate_from_image' and os.environ.get('VIDEO_USE_IMAGE_VIDEO_BACKEND')=='wan':
        from .wan_generation import validate as wan_validate
        return wan_validate(operation,parameters)
    if os.environ.get('VIDEO_USE_GENERATION_BACKEND','jobs')=='ark':
        from .ark_generation import validate as ark_validate
        return ark_validate(operation,parameters)
    caps=capabilities();spec=caps.get('operations',{}).get(operation)
    if not spec:raise ValueError('生成服务不支持该操作')
    if operation.startswith('video.'):
        duration=parameters['duration']
        if not spec.get('durations') or duration not in spec['durations']:raise ValueError('服务不支持所需时长，请先调整确认方案')
        if operation=='video.generate_from_image' and spec.get('reference_image') is not True:raise ValueError('服务不支持参考图片')
    sizes=spec.get('sizes')
    if not sizes or [parameters['width'],parameters['height']] not in sizes:raise ValueError('服务不支持所需尺寸')
    return spec
