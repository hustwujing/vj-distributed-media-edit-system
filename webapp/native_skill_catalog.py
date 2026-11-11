"""Host-side view of the immutable Native Skill Bundle manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def root():
    candidates=(Path(__file__).resolve().parents[1]/'runtime/skills',Path('/opt/video-use/skills'))
    for candidate in candidates:
        if (candidate/'manifest.json').is_file():return candidate
    raise ValueError('Native Skill Bundle manifest is unavailable')


def manifest():
    value=json.loads((root()/'manifest.json').read_text())
    if value.get('schema_version')!=1 or not isinstance(value.get('skills'),list):
        raise ValueError('Invalid Native Skill Bundle manifest')
    result={}
    for item in value['skills']:
        identifier=item.get('id')
        if not isinstance(identifier,str) or not identifier or identifier in result:
            raise ValueError('Invalid or duplicate Native Skill ID')
        entry=root()/identifier/'SKILL.md'
        if not entry.is_file():raise ValueError('Native Skill entry is missing: '+identifier)
        result[identifier]={'id':identifier,'content_sha256':item.get('content_sha256'),
                            'supported_harnesses':item.get('supported_harnesses',[])}
    return {'bundle_version':value.get('bundle_version'),'skills':result}


def identifiers():return tuple(sorted(manifest()['skills']))


def validate(selected):
    if not isinstance(selected,(list,tuple)) or len(selected)!=len(set(selected)):
        raise ValueError('Native Skill selections must be unique')
    unknown=sorted(set(selected)-set(identifiers()))
    if unknown:raise ValueError('Unknown Native Skill: '+', '.join(unknown))
    return tuple(selected)


def records(selected,harness):
    aliases={'codex':'codex','claude_code':'claude','deepseek_harness':'deepseek'}
    if harness not in aliases:raise ValueError('Unsupported Native Skill Harness')
    value=manifest();validate(selected);result=[]
    for identifier in selected:
        item=value['skills'][identifier]
        if aliases[harness] not in item['supported_harnesses']:
            raise ValueError('Native Skill does not support selected Harness: '+identifier)
        entry=root()/identifier/'SKILL.md';raw=entry.read_bytes()
        result.append({'id':identifier,'entry_sha256':hashlib.sha256(raw).hexdigest(),
                       'content_sha256':item['content_sha256']})
    return result
