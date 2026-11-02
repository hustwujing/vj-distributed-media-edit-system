"""Logical workspace content, independent of its cache or checkpoint location."""
import hashlib
import json
import os
import stat
from .artifact_store import logical_path


def checkpoint_rows(store,project):
    return store.query('SELECT * FROM execution_checkpoints WHERE project=?',(project,))


def seed_manifest(store,project):
    prefix='agent/workspace/'
    if store.shared_files:
        if not store.query("SELECT name FROM sqlite_master WHERE name='artifact_files'"):return {}
        rows=store.query('''SELECT f.path,f.artifact,o.project,o.state,o.sha256,o.size
          FROM artifact_files f LEFT JOIN artifact_objects o ON o.id=f.artifact
          WHERE f.project=? AND f.path LIKE ? ORDER BY f.path''',(project,prefix+'%'))
        result={}
        for row in rows:
            if row['project']!=project or row['state']!='published':raise ValueError('Initial workspace reference is unavailable')
            name=logical_path(row['path'][len(prefix):])
            result[name]={k:row[k] for k in ('artifact','sha256','size')}
        return result
    source=store.project_dir(project)/'agent/workspace'
    if source.is_symlink():raise ValueError('Invalid initial workspace directory')
    if not source.exists():return {}
    result={}
    for directory,dirs,names,fd in os.fwalk(source,follow_symlinks=False):
        relative=os.path.relpath(directory,source)
        dirs[:]=[name for name in dirs if stat.S_ISDIR(os.stat(name,dir_fd=fd,follow_symlinks=False).st_mode)]
        for name in names:
            info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:continue
            if len(result)>=20000:raise ValueError('Initial workspace file limit exceeded')
            with os.fdopen(os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd),'rb') as stream:
                before=os.fstat(stream.fileno());digest=hashlib.file_digest(stream,'sha256').hexdigest();after=os.fstat(stream.fileno())
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns) or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Initial workspace changed while reading')
            key=name if relative=='.' else relative+'/'+name
            result[key]={'size':info.st_size,'sha256':digest}
    return result


def effective_manifest(store,project):
    rows=checkpoint_rows(store,project)
    # Even an empty checkpoint is authoritative; deleted seed files stay deleted.
    return json.loads(rows[0]['manifest']).get('workspace',{}) if rows else seed_manifest(store,project)


def proposal_content(store,project):
    # Background steps commit independent snapshots. A later Agent restores
    # them before writing its project checkpoint; moving identical bytes into
    # that checkpoint must not invalidate the proposal's content identity.
    from .step_checkpoints import current_agent_rows,records_for
    manifest={**effective_manifest(store,project),**records_for(current_agent_rows(store,project))}
    return {name:record['sha256'] for name,record in manifest.items()
            if name.startswith('edit/') and not name.startswith('edit/previews/') and name!='edit/project.md'}
