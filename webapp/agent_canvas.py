"""Scoped layer editing client; all file access here runs inside the sandbox."""
import argparse
import base64
import json
import sys
import uuid
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError

parser=argparse.ArgumentParser(description='检查或应用本轮选中图层的修改；不能修改其他图层或解锁')
parser.add_argument('--patch',type=Path,help='图层字段 JSON，例如 {"text":"夏日新品","color":"#ffffff"}')
parser.add_argument('--image',type=Path,help='用 Python 或图片工具生成的图片，替换选中图片图层')
parser.add_argument('--apply',action='store_true',help='确认执行轮才可使用，每轮一次原子修改')
parser.add_argument('--preview-dir',type=Path,help='检查阶段可写的本轮预览目录')
a=parser.parse_args()
payload={'patch':json.loads(a.patch.read_text()) if a.patch else {},'apply':a.apply}
if a.image:
    if not a.apply:parser.error('--image 只能在确认执行轮配合 --apply 使用')
    if a.image.stat().st_size>20*1024**2:parser.error('图片不能超过 20 MB')
    payload['image']=base64.b64encode(a.image.read_bytes()).decode()
try:
    with urlopen(Request('http://127.0.0.1:18766/canvas-edit',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=60) as response:
        result=json.load(response)
except HTTPError as exc:
    print(exc.read().decode(),file=sys.stderr);sys.exit(1)
output=Path('edit')/('canvas-'+uuid.uuid4().hex[:12]) if result['applied'] else a.preview_dir
if output:
    output.mkdir(parents=True,exist_ok=True)
    (output/'change.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    sys.path.insert(0,'/opt/video-use/helpers')
    from canvas_render import render_canvas
    assets={x['id']:x for x in result['assets']}
    for name in ('before','after'):
        render_canvas(result[name],assets,Path.cwd())[0].save(output/(name+'.png'))
    print(json.dumps({'applied':result['applied'],'revision':result['after']['revision'],
                      'files':[str(output/name) for name in ('after.png','before.png','change.json')]},ensure_ascii=False))
else:print(json.dumps(result,ensure_ascii=False))
