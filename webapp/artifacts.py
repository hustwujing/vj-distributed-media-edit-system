"""Consistent delivery metadata for current outputs and existing conversation history."""
import re
from pathlib import PurePosixPath


def is_process_file(item):
    if item.get('workflow_delivery') is False:return True
    if item.get('source_step') and item.get('workflow_delivery') is not True:return True
    if item.get('quality_state') in ('pending','superseded'):return True
    if item.get('role')=='deliverable':return False
    name=item.get('name','')
    return bool(re.search(r'^(?:base(?:_|\.)|source_|clips?_|animations/|tmp/|temp/)',name,re.I)
        or re.search(r'^(?:edl|edit_plan|project|title|(?:demo-)?plan|deliverables)\.(?:json|md|png)$',name,re.I)
        or re.search(r'\.py$|\.report\.json$|\.verify_\d+\.jpe?g$',name,re.I))


def revision(name):
    path=PurePosixPath(name);stem=path.stem
    match=re.search(r'(?:[-_. ]v(?:ersion)?[-_. ]?)(\d+)$',stem,re.I)
    return (str(path.with_name(stem[:match.start()]+path.suffix)).lower(),int(match[1])) if match else (name.lower(),1)


def artifact_key(item):
    kind=item.get('type','')
    media=0 if kind.startswith('video/') else 1 if kind.startswith('audio/') else 2 if kind.startswith('image/') else 3
    name=item.get('name','');_,version=revision(name)
    final=bool(re.match(r'^final(?:[-_. ]v(?:ersion)?[-_. ]?\d+)?\.',PurePosixPath(name).name,re.I))
    return (is_process_file(item),not item.get('is_primary',False),media,
            -item.get('source_modified',0),not final,-version,name.casefold())


def order_artifacts(artifacts,text='',primary=None):
    """Select only existing artifacts; old final.mp4 never gets unconditional priority."""
    items=[dict(a) for a in artifacts]
    candidates=[a for a in items if not is_process_file(a)]
    if primary is None:
        primary=next((a['name'] for a in candidates if a.get('is_primary')),None)
    if primary is None and text:
        mentioned=[a for a in candidates if re.search(r'(?<![\w.-])'+re.escape(PurePosixPath(a['name']).name)+r'(?![\w.-])',text,re.I)]
        if len(mentioned)==1:primary=mentioned[0]['name']
    if primary not in {a['name'] for a in candidates}:
        primary=min(candidates,key=artifact_key)['name'] if candidates else None
    family=revision(primary)[0] if primary else None
    for item in items:
        item['is_primary']=item['name']==primary
        item['is_process']=is_process_file(item)
        item['alternate_version']=bool(primary and item['name']!=primary and revision(item['name'])[0]==family
                                       and item.get('role')!='deliverable')
    return sorted(items,key=artifact_key)
