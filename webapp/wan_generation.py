"""Wan 2.6 image-to-video protocol; each call performs exactly one HTTP action."""
import base64
import hashlib
import io
import os
import re
from PIL import Image
from .egress_policy import provider_base
from .ark_generation import validate_download_url
from .generation_errors import raise_for_status
from .generation_auth import credential_binding,frozen_headers

PROTOCOL='wan-video-v1'
OPERATION='video.generate_from_image'


def configuration():
    base=provider_base(os.environ.get('VIDEO_USE_IMAGE_VIDEO_URL',''))
    key=os.environ.get('VIDEO_USE_IMAGE_VIDEO_KEY','')
    model=os.environ.get('VIDEO_USE_IMAGE_VIDEO_MODEL','')
    if not key or model!='wan2.6-i2v':raise ValueError('Wan 图生视频需要配置凭据和 wan2.6-i2v 模型')
    return base,key,model


def capabilities():
    configuration()
    return {'durations':[5,10,15],'sizes':[[1280,720],[720,1280]],'reference_image':True,
            'reference_transport':'inline_first_frame','reference_size_policy':'match_output',
            'reference_formats':['PNG','JPEG','WEBP'],'reference_max_bytes':10*1024**2,
            'camera_motion_control':'prompt_only','exact_motion_guaranteed':False,
            'audio':True,'cancellation':'unsupported',
            'duration_tolerance_seconds':0.05}


def validate(operation,parameters):
    spec=capabilities()
    if operation!=OPERATION:raise ValueError('Wan 适配器仅提供图生视频')
    if parameters['duration'] not in spec['durations']:raise ValueError('Wan 图生视频支持5、10、15秒')
    if [parameters['width'],parameters['height']] not in spec['sizes']:raise ValueError('Wan 图生视频支持1280×720或720×1280')
    return spec


def prepare(operation,parameters,reference=None):
    validate(operation,parameters);base,key,model=configuration()
    if reference is None or len(reference)>10*1024**2:raise ValueError('Wan 需要唯一且不超过10MB的参考图')
    with Image.open(io.BytesIO(reference)) as image:
        if image.format not in ('PNG','JPEG','WEBP'):raise ValueError('参考图需要PNG/JPEG/WebP')
        if image.size!=(parameters['width'],parameters['height']):raise ValueError('参考图尺寸必须与已确认视频尺寸一致，请先调整参考图')
        mime=Image.MIME[image.format];image.verify()
    payload={'model':model,'input':{'prompt':parameters['prompt'],'img_url':'data:'+mime+';base64,'+base64.b64encode(reference).decode()},
             'parameters':{'duration':int(parameters['duration']),'resolution':'720P','seed':parameters.get('seed',1),'prompt_extend':False}}
    prepared={'provider':base,'protocol':PROTOCOL,'model':model,'operation':operation,
              'reference_sha256':hashlib.sha256(reference).hexdigest(),'payload':payload}
    prepared['credential_binding']=credential_binding(prepared,key)
    return prepared


def auth(handle):
    return frozen_headers(handle,PROTOCOL,'VIDEO_USE_IMAGE_VIDEO_KEY')


def task_url(handle,identifier):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',identifier):raise ValueError('Invalid provider task ID')
    return handle['provider']+'/tasks/'+identifier


def submit(client,prepared,request_key):
    base,_,model=configuration()
    if (prepared['provider'],prepared['model'])!=(base,model):raise ValueError('Provider configuration changed before submission')
    response=client.post(prepared['provider']+'/services/aigc/video-generation/video-synthesis',json=prepared['payload'],
                         headers={**auth(prepared),'X-DashScope-Async':'enable'},timeout=(10,60),allow_redirects=False)
    raise_for_status(response);result=response.json();identifier=result.get('output',{}).get('task_id','')
    task_url(prepared,identifier)
    return {'id':identifier,'status':'waiting'}


def poll(client,handle,identifier):
    response=client.get(task_url(handle,identifier),headers=auth(handle),timeout=(5,20),allow_redirects=False)
    raise_for_status(response);result=response.json();output=result.get('output',{})
    mapping={'PENDING':'waiting','RUNNING':'running','SUCCEEDED':'succeeded','FAILED':'failed','CANCELED':'cancelled','CANCELLED':'cancelled','UNKNOWN':'failed'}
    state=output.get('task_status')
    if state not in mapping:raise ValueError('上游返回未知生成状态')
    record={'status':mapping[state],'usage':result.get('usage'),'cost':result.get('cost'),'artifacts':[]}
    if state=='SUCCEEDED':
        url=validate_download_url(output.get('video_url'),os.environ.get('VIDEO_USE_IMAGE_VIDEO_DOWNLOAD_HOSTS','aliyuncs.com'))
        record['artifacts']=[{'id':'video','name':'video.mp4','url':url}]
    if state in ('FAILED','UNKNOWN'):record['error']=re.sub(r'[^a-zA-Z0-9_.-]','',str(output.get('code','generation_failed')))[:120]
    return record


def download(client,artifact):
    url=validate_download_url(artifact.get('url'),os.environ.get('VIDEO_USE_IMAGE_VIDEO_DOWNLOAD_HOSTS','aliyuncs.com'))
    return client.get(url,stream=True,timeout=(10,60),allow_redirects=False)


def cancel(client,handle,identifier):
    raise NotImplementedError('该 Wan 接口没有已验证的取消能力，上游任务可能继续运行')
