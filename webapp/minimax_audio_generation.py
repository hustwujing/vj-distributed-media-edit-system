"""MiniMax text-to-audio adapter with durable handles and host-held credentials."""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import tarfile
from urllib.parse import urlsplit

import requests

from .egress_policy import provider_base
from .generation_auth import credential_binding,frozen_headers
from .generation_errors import raise_for_status
from .operations import AudioGenerationParameters


PROTOCOL='minimax-t2a-v1'
OPERATION='audio.generate_from_text'


def configuration():
    base=provider_base(os.environ.get('VIDEO_USE_AUDIO_GENERATION_URL',''))
    key=os.environ.get('VIDEO_USE_AUDIO_GENERATION_KEY','')
    model=os.environ.get('VIDEO_USE_AUDIO_GENERATION_MODEL','')
    if not key or not model:raise ValueError('音频生成服务缺少凭据或模型配置')
    return base,key,model


def capabilities():
    base,_,model=configuration()
    return {'protocol':PROTOCOL,'provider':base,'model':model,'operation':OPERATION,
            'voices':['male-qn-qingse'],'formats':['mp3','wav','flac'],
            'speed':{'minimum':.5,'maximum':2},'max_characters':9999,
            'max_estimated_duration_seconds':180,'cancellation':'unsupported'}


def prepare(operation,parameters,reference=None):
    if operation!=OPERATION:raise ValueError('MiniMax 音频适配器不支持该操作')
    if reference is not None:raise ValueError('文本生成音频不接受输入素材')
    value=AudioGenerationParameters.model_validate(parameters).model_dump()
    base,key,model=configuration()
    payload={'model':model,'text':value['text'],
             'voice_setting':{'voice_id':value['voice_id'],'speed':value['speed'],'vol':1,'pitch':0},
             'audio_setting':{'format':value['audio_format'],'audio_sample_rate':32000,
                              'bitrate':128000,'channel':2}}
    prepared={'provider':base,'protocol':PROTOCOL,'model':model,'operation':operation,
              'audio_format':value['audio_format'],'payload':payload}
    prepared['credential_binding']=credential_binding(prepared,key)
    return prepared


def auth(handle):
    return {**frozen_headers(handle,PROTOCOL,'VIDEO_USE_AUDIO_GENERATION_KEY'),
            'Content-Type':'application/json'}


def identifier(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)):
        raise ValueError('Invalid audio provider task ID')
    value=str(value)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):raise ValueError('Invalid audio provider task ID')
    return value


def submit(client,prepared,request_key):
    base,_,model=configuration()
    if (prepared['provider'],prepared['model'])!=(base,model):
        raise ValueError('音频生成配置在提交前发生变化')
    response=client.post(base+'/t2a_async_v2',headers=auth(prepared),json=prepared['payload'],
                         timeout=(10,60),allow_redirects=False)
    raise_for_status(response)
    task=identifier(response.json().get('task_id',''))
    return {'id':task,'status':'waiting'}


def poll(client,handle,task):
    task=identifier(task)
    response=client.get(handle['provider']+'/query/t2a_async_query_v2',headers=auth(handle),
                        params={'task_id':task},timeout=(5,20),allow_redirects=False)
    raise_for_status(response);result=response.json();state=str(result.get('status','')).lower()
    if state in ('processing','pending','queued','running'):
        return {'status':'running','artifacts':[]}
    if state in ('failed','expired'):
        return {'status':'failed','error':'audio_'+state,'artifacts':[]}
    if state!='success':raise ValueError('上游返回未知音频生成状态')
    file_id=result.get('file_id')
    if isinstance(file_id,bool) or not isinstance(file_id,(str,int)) or not str(file_id):
        raise ValueError('音频生成结果缺少文件标识')
    file_response=client.get(handle['provider']+'/files/retrieve',headers=auth(handle),
                             params={'file_id':str(file_id)},timeout=(5,20),allow_redirects=False)
    raise_for_status(file_response)
    url=(file_response.json().get('file') or {}).get('download_url','')
    validate_download_url(url)
    extension={'mp3':'mp3','wav':'wav','flac':'flac'}[handle.get('audio_format','mp3')]
    return {'status':'succeeded','artifacts':[{'id':'audio','name':'narration.'+extension,'url':url}]}


def validate_download_url(url,allowed_hosts=None):
    if not isinstance(url,str):raise ValueError('音频生成服务未返回下载地址')
    parsed=urlsplit(url);configured=allowed_hosts or os.environ.get('VIDEO_USE_AUDIO_GENERATION_DOWNLOAD_HOSTS','')
    suffixes=[item.strip().lower() for item in configured.split(',') if item.strip()]
    host=(parsed.hostname or '').lower()
    if (not suffixes or parsed.scheme!='https' or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None,443) or not any(host==item or host.endswith('.'+item) for item in suffixes)):
        raise ValueError('上游音频下载地址不在已配置的存储域名范围内')
    return url


def _audio_from_archive(data):
    try:
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:*') as archive:
            members=archive.getmembers()
            if len(members)>64:raise ValueError('音频归档成员过多')
            extensions={'.mp3':'audio/mpeg','.wav':'audio/wav','.flac':'audio/flac',
                        '.m4a':'audio/mp4','.ogg':'audio/ogg','.webm':'audio/webm'}
            candidates=[item for item in members if item.isfile() and Path(item.name).suffix.lower() in extensions]
            if len(candidates)!=1:raise ValueError('音频归档必须只包含一个音频文件')
            member=candidates[0]
            if member.size<=0 or member.size>128*1024**2:raise ValueError('音频归档成员大小无效')
            stream=archive.extractfile(member)
            if stream is None:raise ValueError('音频归档成员不可读')
            payload=stream.read(128*1024**2+1)
            if len(payload)!=member.size:raise ValueError('音频归档成员大小不一致')
            return payload,extensions[Path(member.name).suffix.lower()]
    except tarfile.TarError as exc:raise ValueError('音频归档格式无效') from exc


def download(client,artifact):
    response=client.get(validate_download_url(artifact.get('url')),stream=True,
                        timeout=(10,60),allow_redirects=False)
    raise_for_status(response)
    content_type=response.headers.get('Content-Type','').split(';',1)[0].strip().lower()
    if content_type not in ('application/x-tar','application/tar'):return response
    try:
        data=bytearray()
        for chunk in response.iter_content(1024*1024):
            data.extend(chunk)
            if len(data)>128*1024**2:raise ValueError('音频归档超过大小限制')
    finally:response.close()
    payload,mime=_audio_from_archive(bytes(data))
    unwrapped=requests.Response();unwrapped.status_code=200;unwrapped._content=payload
    unwrapped._content_consumed=True;unwrapped.headers['Content-Type']=mime
    return unwrapped


def cancel(client,handle,task):
    identifier(task)
    raise NotImplementedError('MiniMax 音频生成接口不支持取消已提交任务')
