"""Project-scoped image tools. Write returned bytes inside the sandbox as its user."""
import argparse,base64,json,sys,uuid
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
parser=argparse.ArgumentParser()
parser.add_argument('target_asset');parser.add_argument('--action',choices=['generate','decompose','segment','segment_sam3','refine_zim','remove_bg','erase','local_edit','ocr','replace_text','refine_mask','perspective','product_layout','color_match','local_adjust','batch_export'],default='generate')
parser.add_argument('--reference-asset',action='append',default=[]);parser.add_argument('--prompt',default='');parser.add_argument('--quality',choices=['fast','standard','high'],default='fast')
parser.add_argument('--box',nargs=4,type=int);parser.add_argument('--point',action='append',nargs=2,type=int,default=[]);parser.add_argument('--mask');parser.add_argument('--text',default='');parser.add_argument('--layer-count',type=int,default=3)
parser.add_argument('--parameters',default='{}',help='Validated JSON parameters for local image tools')
a=parser.parse_args()
payload={'action':a.action,'asset':a.target_asset,'references':a.reference_asset,'prompt':a.prompt,'quality':a.quality,'box':a.box,'points':a.point,'mask':a.mask,'text':a.text,'layer_count':a.layer_count,'parameters':json.loads(a.parameters)}
request=Request('http://127.0.0.1:18766/image-tools',data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
try:
    with urlopen(request,timeout=850) as response:result=json.load(response)
    root=Path.cwd().resolve();output=root/'edit'/('image-'+uuid.uuid4().hex[:12]);output.mkdir(mode=0o700)
    for item in result['files']:
        if Path(item['name']).name!=item['name']:raise ValueError('Invalid output name')
        with (output/item['name']).open('xb') as f:f.write(base64.b64decode(item['data'],validate=True))
    print(json.dumps({'directory':str(output.relative_to(root)),'files':[str((output/i['name']).relative_to(root)) for i in result['files']]},ensure_ascii=False))
except HTTPError as e:
    print(e.read().decode(),file=sys.stderr);sys.exit(1)
