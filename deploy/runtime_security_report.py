#!/usr/bin/env python3
"""Summarize local Syft and Grype evidence without hiding raw findings."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime,timezone
import json
from pathlib import Path

try:
    from deploy.runtime_release import DEFAULT_OUTPUT,ROOT,sha256,write_json
except ModuleNotFoundError:
    from runtime_release import DEFAULT_OUTPUT,ROOT,sha256,write_json


SBOM=ROOT/'runtime/build/sbom'
VULN=ROOT/'runtime/build/vulnerability'
REPORT=ROOT/'runtime/build/runtime-security-summary.json'


def vulnerability_summary(value):
    matches=value.get('matches',[]);severity=Counter();fixable=Counter();packages=Counter();actionable=[]
    unique=set()
    for match in matches:
        vulnerability=match['vulnerability'];artifact=match['artifact'];level=vulnerability.get('severity','Unknown')
        key=(vulnerability['id'],artifact.get('name'),artifact.get('version'),artifact.get('type'))
        if key in unique:continue
        unique.add(key);severity[level]+=1;packages[artifact.get('name','unknown')]+=1
        fixes=vulnerability.get('fix',{}).get('versions') or []
        if fixes:
            fixable[level]+=1;actionable.append({'id':vulnerability['id'],'severity':level,
                'package':artifact.get('name'),'installed':artifact.get('version'),
                'type':artifact.get('type'),'fix_versions':fixes[:10]})
    order={'Critical':0,'High':1,'Medium':2,'Low':3,'Negligible':4,'Unknown':5}
    actionable.sort(key=lambda row:(order.get(row['severity'],99),row['package'] or '',row['id']))
    descriptor=value.get('descriptor',{})
    return {'raw_matches':len(matches),'unique_package_vulnerabilities':len(unique),
        'severity':dict(severity),'fixable_severity':dict(fixable),
        'top_affected_packages':[{'package':name,'findings':count} for name,count in packages.most_common(20)],
        'actionable':actionable,'scanner':{'name':descriptor.get('name'),'version':descriptor.get('version'),
            'database':descriptor.get('db'),'timestamp':descriptor.get('timestamp')}}


def license_summary(value):
    licenses=Counter();components=value.get('components',[]);unknown=0
    for component in components:
        values=component.get('licenses') or []
        if not values:unknown+=1;continue
        for entry in values:
            license_value=entry.get('license') or entry.get('expression') or {}
            if isinstance(license_value,str):name=license_value
            else:name=license_value.get('id') or license_value.get('name') or 'unknown'
            licenses[name]+=1
    copyleft={name:count for name,count in licenses.items()
        if any(marker in name.upper() for marker in ('GPL','AGPL','LGPL','MPL','EPL','CDDL','SSPL'))}
    return {'components':len(components),'components_without_declared_license':unknown,
        'declared_licenses':dict(licenses.most_common()),'copyleft_observations':copyleft}


def build(sbom=SBOM,vulnerability=VULN):
    names=('operation-runtime','agent-runtime-codex','agent-runtime-claude','agent-runtime-deepseek')
    release=json.loads(DEFAULT_OUTPUT.read_text())
    images={}
    for name in names:
        tag=name+':build';cyclonedx=sbom/(name+'.cdx.json');spdx=sbom/(name+'.spdx.json')
        grype=vulnerability/(name+'.grype.json')
        images[name]={'vulnerabilities':vulnerability_summary(json.loads((vulnerability/(name+'.grype.json')).read_text())),
            'licenses':license_summary(json.loads(cyclonedx.read_text())),
            'local_image_id':release['images'][tag]['local_image_id'],
            'evidence':{'cyclonedx':{'path':str(cyclonedx),'sha256':sha256(cyclonedx)},
                'spdx':{'path':str(spdx),'sha256':sha256(spdx)},
                'grype':{'path':str(grype),'sha256':sha256(grype)}}}
    return {'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),
        'policy_note':'Raw matches are retained. Fixable findings require review before publication; copyleft observations are not automatic violations.',
        'images':images}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=REPORT);args=parser.parse_args()
    print(write_json(args.output,build()))


if __name__=='__main__':main()
