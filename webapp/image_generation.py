"""Synchronous Image API adapter for the existing operator-configured gateway.

Protocol verified against the local common/image/providers/openai_image.py.
No reference image is fabricated, no provider URLs are accepted from the Agent.
"""
import base64
import os
from pydantic import BaseModel,ConfigDict,Field,model_validator
from typing import Literal
from .egress_policy import provider_base,session

class Parameters(BaseModel):
    model_config=ConfigDict(extra='forbid')
    prompt:str=Field(min_length=1,max_length=5000)
    width:int=Field(default=1280,ge=16,le=3840)
    height:int=Field(default=720,ge=16,le=3840)
    quality:Literal['low','medium','high','auto']='low'
    fit:Literal['native','contain','cover']='native'
    background:str=Field(default='#ffffff',pattern=r'^#[0-9a-fA-F]{6}$')
    @model_validator(mode='after')
    def size(self):
        if not self.prompt.strip():raise ValueError('生成描述不能为空')
        if self.width%16 or self.height%16 or max(self.width,self.height)/min(self.width,self.height)>3 or not 655360<=self.width*self.height<=8294400:
            raise ValueError('文生图尺寸必须为16的倍数、长短边比不超过3，总像素655360–8294400')
        return self


def configuration():
    base=os.environ.get('VIDEO_USE_IMAGE_GENERATION_URL') or os.environ.get('VIDEO_USE_MODEL_BASE_URL','')
    key=os.environ.get('VIDEO_USE_IMAGE_GENERATION_KEY') or os.environ.get('VIDEO_USE_MODEL_API_KEY','')
    if not base or not key:raise ValueError('文生图网关未配置')
    return provider_base(base),key,os.environ.get('VIDEO_USE_IMAGE_GENERATION_MODEL','gpt-image-2')


def generate(parameters):
    value=Parameters.model_validate(parameters)
    base,key,model=configuration()
    payload={'model':model,'prompt':value.prompt,'size':f'{value.width}x{value.height}','n':1,'quality':value.quality,'output_format':'png','output_compression':100}
    with session() as client:
        response=client.post(base+'/images/generations',json=payload,headers={'Authorization':'Bearer '+key},timeout=(10,240),allow_redirects=False)
        response.raise_for_status();result=response.json()
    images=result.get('data',[])
    if len(images)!=1 or not isinstance(images[0].get('b64_json'),str):raise ValueError('生成服务未返回唯一内联图片')
    encoded=images[0]['b64_json']
    if len(encoded)>48*1024**2:raise ValueError('生成图片过大')
    raw=base64.b64decode(encoded,validate=True)
    return raw,{'usage':result.get('usage'),'cost':result.get('cost')}
