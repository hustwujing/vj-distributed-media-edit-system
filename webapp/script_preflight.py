"""Bounded static checks in the SAME sandbox as local execution. Never runs code."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import sys

resource.setrlimit(resource.RLIMIT_CPU,(15,15))
request=json.load(sys.stdin)
files=request['files'];result={};issues=[]
if len(files)>40:raise ValueError('Too many preflight inputs')
for filename in files:
    if len(files)>40:raise ValueError('Too many script dependencies')
    path=Path(filename)
    if path.is_absolute() or '..' in path.parts or not filename.startswith('edit/') or path.is_symlink() or not path.is_file() or path.stat().st_size>2*1024**2:
        issues.append({'file':filename,'code':'missing','message':'脚本或配置缺失/路径无效/文件过大'});continue
    data=path.read_bytes();result[filename]=hashlib.sha256(data).hexdigest()
    if path.suffix=='.py':
        try:
            tree=ast.parse(data,filename=filename)
            local={p.stem for p in path.parent.glob('*.py')}
            for node in ast.walk(tree):
                names=[alias.name.split('.')[0] for alias in node.names] if isinstance(node,ast.Import) else [node.module.split('.')[0]] if isinstance(node,ast.ImportFrom) and node.module and not node.level else []
                for name in names:
                    if name in local:
                        dependency=str(path.parent/(name+'.py'))
                        if dependency not in files:files.append(dependency)
                        continue
                    if name in sys.builtin_module_names:continue
                    if importlib.util.find_spec(name) is None:issues.append({'file':filename,'code':'environment','message':'缺少模块: '+name})
        except (SyntaxError,ValueError) as exc:issues.append({'file':filename,'code':'code','message':str(exc)[:1000]})
    elif path.suffix=='.json':
        try:json.loads(data)
        except ValueError as exc:issues.append({'file':filename,'code':'config','message':str(exc)[:1000]})
print(json.dumps({'passed':not issues,'files':result,'issues':issues,'coverage':'静态语法、顶层依赖和 JSON 格式；未执行业务逻辑'},ensure_ascii=False))
