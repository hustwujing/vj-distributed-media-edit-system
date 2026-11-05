"""Versioned admission estimates, never provider usage or an exact tokenizer claim."""
import base64
from copy import deepcopy
from functools import lru_cache
import hashlib
import io
import json
import math
from pathlib import Path
import sys

VOCAB_SHA256='223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7'
VOCAB_URL='https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken'
POLICIES={
    'claude_bpe_estimate_v1':frozenset(('claude-4.6-sonnet','claude-sonnet-5-5','claude-opus-5')),
    'deepseek_bpe_estimate_v1':frozenset(('deepseek-v4-flash','deepseek-v4.1-flash')),
}
MULTIPLIERS={'claude-4.6-sonnet':1.7,'claude-sonnet-5-5':2.4,'claude-opus-5':2.6,
             'deepseek-v4-flash':1.5,'deepseek-v4.1-flash':1.5}
PATTERN=r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"


def data_path():return Path(sys.prefix)/'share/video-use-tokenizers/cl100k_base.tiktoken'


def validate_policy(policy,model,protocol):
    if policy=='native':return
    if policy not in POLICIES or protocol!='anthropic' or model not in POLICIES[policy]:
        raise ValueError('Input token estimation policy does not match the frozen model/protocol')


@lru_cache(maxsize=1)
def tokenizer():
    import tiktoken
    content=data_path().read_bytes()
    if hashlib.sha256(content).hexdigest()!=VOCAB_SHA256:raise OSError('Budget tokenizer asset checksum differs')
    ranks={base64.b64decode(token):int(rank) for token,rank in (line.split() for line in content.splitlines() if line)}
    return tiktoken.Encoding(name='budget_cl100k_reference_v1',pat_str=PATTERN,mergeable_ranks=ranks,special_tokens={})


def estimate(body,policy,protocol='anthropic'):
    validate_policy(policy,body.get('model'),protocol)
    if policy=='native':raise ValueError('Native counting is not a local estimate')
    image_tokens=0;image_count=0
    def clean(value,depth=0):
        nonlocal image_tokens,image_count
        if depth>48:raise ValueError('Token estimate input nesting exceeds limit')
        if isinstance(value,list):return [clean(x,depth+1) for x in value]
        if not isinstance(value,dict):return value
        if value.get('type')=='image':
            from PIL import Image
            source=value.get('source',{})
            if source.get('type')!='base64' or source.get('media_type')!='image/jpeg':raise ValueError('Only authorized JPEG inspection images can be estimated')
            encoded=source.get('data','')
            if not isinstance(encoded,str) or len(encoded)>3*1024**2:raise ValueError('Inspection image exceeds estimate limit')
            raw=base64.b64decode(encoded,validate=True)
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format!='JPEG' or picture.width*picture.height>4_000_000 or getattr(picture,'n_frames',1)!=1:raise ValueError('Invalid inspection image')
                # Unscaled Claude patch grid conservatively covers both tiers;
                # DeepSeek documents an upper limit of 1024 visual tokens/image.
                image_tokens+=(math.ceil(picture.width/28)*math.ceil(picture.height/28) if policy.startswith('claude_') else 1024)+256
            image_count+=1
            if image_count>8:raise ValueError('Too many inspection images')
            return {'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':'[image counted separately]'}}
        if value.get('type') in ('document','file','input_image','image_url'):raise ValueError('Unsupported multimodal input for local estimation')
        if value.get('type')=='tool_result' and 'content' in value:
            return {**value,'content':clean(value['content'],depth+1)}
        # Tool arguments and JSON Schema examples are text, not image blocks.
        return value
    content=deepcopy({k:body[k] for k in ('system','messages','tools','tool_choice','thinking','output_config') if k in body})
    if 'system' in content:content['system']=clean(content['system'])
    for message in content.get('messages',[]):
        if 'content' in message:message['content']=clean(message['content'])
    text=json.dumps(content,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    reference=len(tokenizer().encode_ordinary(text))
    # This reference BPE is not Claude/DeepSeek's native tokenizer. The margin
    # and framing reserve must be calibrated against the configured route.
    multiplier=MULTIPLIERS[body['model']]
    margin=max(1024,math.ceil(reference*(multiplier-1)))+32*len(body.get('messages',[]))
    result={'reference_input_tokens':reference,'framing_margin_tokens':margin,
        'image_envelope_tokens':image_tokens,'image_count':image_count,'estimation_policy':policy,
        'estimate_multiplier':multiplier,
        'estimation_policy_digest':hashlib.sha256((policy+body['model']+VOCAB_SHA256+PATTERN+str(multiplier)+'+1024+32:images-cl28-ds1024-plus256-max8-jpeg4m').encode()).hexdigest()}
    result['estimated_input_tokens']=reference+margin+image_tokens
    return result
