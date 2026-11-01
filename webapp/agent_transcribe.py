"""Credential-free transcription client made available inside the sandbox."""
import argparse
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

parser = argparse.ArgumentParser()
parser.add_argument('asset', help='Asset ID from /workspace/assets.json')
parser.add_argument('--audio-track', type=int, default=0)
parser.add_argument('--language')
parser.add_argument('--speakers', type=int)
args = parser.parse_args()
request = Request('http://127.0.0.1:18766/transcribe',
                  data=json.dumps(vars(args)).encode(), headers={'Content-Type':'application/json'})
try:
    with urlopen(request, timeout=1900) as response:
        result=json.load(response)
except HTTPError as exc:
    message='转写网关拒绝请求，请检查已确认的能力和输入参数。'
    try:
        payload=json.loads(exc.read(8192))
        if isinstance(payload,dict) and isinstance(payload.get('error'),str):message=payload['error'][:2000]
    except (ValueError,OSError):pass
    raise SystemExit(f'转写请求失败（HTTP {exc.code}）：{message}') from None
print(json.dumps({'path':result['path'], 'text':result['transcript'].get('text','')}, ensure_ascii=False))
