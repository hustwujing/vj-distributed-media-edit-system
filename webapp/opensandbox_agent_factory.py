"""Build a disabled-by-default OpenSandbox Agent runtime from task state."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import agent_sandbox
from .opensandbox_backend import AgentRuntimeSpec
from .sandbox_workspace import SandboxWorkspaceRestorer


class OpenSandboxAgentFactory:
    def __init__(self,backend,gateway_grant_factory,*,tool_schema_digest=None,
                 gateway_protocol='gateway-v1'):
        self.backend=backend;self.gateway_grant_factory=gateway_grant_factory
        capabilities=Path(__file__).resolve().parents[1]/'runtime/public/capabilities.json'
        self.tool_schema_digest=tool_schema_digest or 'sha256:'+hashlib.sha256(capabilities.read_bytes()).hexdigest()
        self.gateway_protocol=gateway_protocol

    @staticmethod
    def _record(path,key,sources):
        path=Path(path);data=path.read_bytes();sources[key]=path
        return {'artifact':key,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                'executable':bool(path.stat().st_mode&0o100)}

    def workspace(self,lease,profile):
        store=lease.store;turn=store.query('SELECT project,mode FROM agent_turns WHERE id=?',(lease.run_id,))[0]
        project=turn['project']
        env=agent_sandbox.prepare(store,project,profile.model,turn['mode'],lease.run_id)
        sources={};manifest={'workspace':{},'state':{}}
        rows=store.query('SELECT path,manifest FROM execution_checkpoints WHERE project=?',(project,))
        if rows:
            prior=json.loads(rows[0]['manifest']);root=store.root/rows[0]['path']
            for group in ('workspace','state'):
                for name,record in prior.get(group,{}).items():
                    key='checkpoint:'+group+':'+name
                    if record.get('artifact'):
                        path=store.artifacts.materialize(project,record['artifact'])
                    else:path=root/group/name
                    manifest[group][name]=self._record(path,key,sources)
        for path in env['workspace'].rglob('*'):
            if path.is_file() and not path.is_symlink():
                name=path.relative_to(env['workspace']).as_posix();key='context:'+name
                manifest['workspace'][name]=self._record(path,key,sources)
        for asset in env['assets']:
            path=store.file(project,'sources/'+asset['filename']);name='sources/'+asset['filename']
            manifest['workspace'][name]=self._record(path,'asset:'+asset['id'],sources)
        return env,manifest,sources

    def __call__(self,lease,binding):
        from .agent_profiles import for_task
        from .scheduling import requirements
        profile=for_task(lease.store,lease.run_id)
        env,manifest,sources=self.workspace(lease,profile)
        spec=AgentRuntimeSpec(profile.harness,profile.model,profile.reasoning_effort,
            self.tool_schema_digest,self.gateway_protocol,
            {'model':profile.model,'reasoning_effort':profile.reasoning_effort,
             'max_output_tokens':profile.budget['max_output_tokens']},
            lambda identity:self.gateway_grant_factory(lease,identity))
        restorer=SandboxWorkspaceRestorer(sources.__getitem__)
        runtime=self.backend.start(lease,binding,
            requirements(lease.store,lease.run_id,lease.kind,step_id=lease.step_id),spec,
            lambda files:restorer.restore(files,manifest),lambda event:None)
        runtime.prepared_env=env
        return runtime
