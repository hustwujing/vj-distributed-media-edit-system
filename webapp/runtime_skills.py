"""Install the image-pinned Skill Bundle into each native Harness home."""
from __future__ import annotations

import json
from pathlib import Path
import shutil


SKILL_ROOT=Path('/opt/video-use/skills')
DESTINATIONS={
    'codex':Path('/state/codex/skills'),
    'claude_code':Path('/state/claude/skills'),
    'deepseek_harness':Path('/state/dsh/skills'),
}


def _remove(path):
    if path.is_symlink() or path.is_file():path.unlink(missing_ok=True)
    elif path.exists():shutil.rmtree(path)


def materialize_skills(harness,*,source=None,destinations=None):
    source=SKILL_ROOT if source is None else source
    destinations=DESTINATIONS if destinations is None else destinations
    try:destination=Path(destinations[harness])
    except KeyError as exc:raise ValueError('Unsupported native Harness Skill root') from exc
    source=Path(source);manifest=json.loads((source/'manifest.json').read_text())
    if (not isinstance(manifest,dict) or manifest.get('schema_version')!=1
            or not isinstance(manifest.get('bundle_version'),str)
            or not isinstance(manifest.get('skills'),list)):
        raise ValueError('Invalid native Skill Bundle manifest')
    aliases={'codex':'codex','claude_code':'claude','deepseek_harness':'deepseek'}
    selected=[]
    for row in manifest['skills']:
        if (not isinstance(row,dict) or not isinstance(row.get('id'),str)
                or '/' in row['id'] or '..' in row['id']):
            raise ValueError('Invalid native Skill Bundle entry')
        if aliases[harness] in row.get('supported_harnesses',[]):selected.append(row['id'])
    destination.mkdir(parents=True,exist_ok=True)
    marker=destination/'.image-bundle.json';previous=set()
    if marker.is_file():
        try:previous=set(json.loads(marker.read_text()).get('skills',[]))
        except (ValueError,TypeError):previous=set()
    current=set(selected)
    for skill_id in previous-current:_remove(destination/skill_id)
    for skill_id in selected:
        target=source/skill_id
        if not (target/'SKILL.md').is_file():raise ValueError('Native Skill is missing its entry: '+skill_id)
        link=destination/skill_id
        if link.is_symlink() and link.resolve()==target.resolve():continue
        _remove(link);link.symlink_to(target,target_is_directory=True)
    temporary=destination/'.image-bundle.json.tmp'
    temporary.write_text(json.dumps({'bundle_version':manifest['bundle_version'],
        'skills':sorted(current)},sort_keys=True,separators=(',',':'))+'\n')
    temporary.replace(marker)
    return {'bundle_version':manifest['bundle_version'],'skill_count':len(selected),
        'root':str(destination)}
