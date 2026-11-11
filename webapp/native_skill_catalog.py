"""Host-side view of the immutable Native Skill Bundle manifest."""
from __future__ import annotations

import json
from pathlib import Path
import re


def bundle():
    candidates=(Path(__file__).resolve().parents[1]/'runtime/public/bundle.json',Path('/opt/video-use/bundle.json'))
    for candidate in candidates:
        try:
            if candidate.is_file():return json.loads(candidate.read_text())
        except PermissionError:continue
    raise ValueError('Native Skill Bundle index is unavailable')


def manifest():
    value=bundle()
    if value.get('schema_version')!=1 or not isinstance(value.get('skill_digest'),str) or not isinstance(value.get('files'),list):
        raise ValueError('Invalid Native Skill Bundle index')
    result={}
    for item in value['files']:
        match=re.fullmatch(r'skills/([a-z0-9][a-z0-9_-]{0,79})/SKILL\.md',str(item.get('path','')))
        if not match:continue
        identifier=match.group(1)
        if identifier in result or not re.fullmatch(r'[a-f0-9]{64}',str(item.get('sha256',''))):
            raise ValueError('Invalid or duplicate Native Skill entry')
        result[identifier]={'id':identifier,'entry_sha256':item['sha256']}
    if len(result)!=25:raise ValueError('Native Skill Bundle must expose exactly 25 entries')
    return {'bundle_digest':value['skill_digest'],'skills':result}


def identifiers():return tuple(sorted(manifest()['skills']))


def validate(selected):
    if not isinstance(selected,(list,tuple)) or len(selected)!=len(set(selected)):
        raise ValueError('Native Skill selections must be unique')
    unknown=sorted(set(selected)-set(identifiers()))
    if unknown:raise ValueError('Unknown Native Skill: '+', '.join(unknown))
    return tuple(selected)


def records(selected,harness):
    if harness not in {'codex','claude_code','deepseek_harness'}:raise ValueError('Unsupported Native Skill Harness')
    value=manifest();validate(selected);result=[]
    for identifier in selected:
        item=value['skills'][identifier]
        result.append({'id':identifier,'entry_sha256':item['entry_sha256'],
                       'bundle_digest':value['bundle_digest']})
    return result
