"""Explicit real BOSS file/checkpoint verification in an isolated prefix and DB.

Reads private .storage.env. Deletes only object keys recorded by this test's DB.
Never installs configuration or changes the running application's database.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import uuid
from unittest.mock import patch


def main():
    config={}
    for line in Path('.storage.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1);parts=shlex.split(value);config[key]=parts[0] if parts else ''
    prefix='video-use/integration/'+uuid.uuid4().hex
    config['VIDEO_USE_OBJECT_PREFIX']=prefix
    from tests.distributed_support import PostgresServer
    from webapp.store import Store
    from webapp import agent_store,agent_sandbox
    from webapp.execution_resources import IdentityPool
    from webapp.execution_runtime import TaskRuntime
    from webapp.task_leases import Coordinator
    pg=PostgresServer();report={'passed':False,'backend':'BOSS','prefix':prefix,'postgres':'real isolated server','nodes':'distinct local roots; not separate physical hosts'}
    try:
        with tempfile.TemporaryDirectory(prefix='vb-') as directory,patch.dict(os.environ,{**config,
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_DISTRIBUTED':'1',
                'VIDEO_USE_TASK_MEMORY_BYTES':str(512*1024**2),'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2)}):
            root=Path(directory);a=Store(root/'a');agent_store.initialize(a);b=Store(root/'b')
            project=a.add_project('storage-smoke','encrypted checkpoint')['id'];backend=a.artifacts.backend
            try:
                source=a.project_dir(project)/'sources/probe.txt';source.write_bytes(b'private distributed file probe')
                a.artifacts.save_file(project,'sources/probe.txt')
                reference=a.artifacts.resolve(project,'sources/probe.txt')
                wire=backend.client.get_object(Bucket=backend.bucket,Key=reference['object_key'])
                try:ciphertext=wire['Body'].read()
                finally:wire['Body'].close()
                assert b'private distributed file probe' not in ciphertext and ciphertext.startswith(b'VUOBJ01\0')
                assert b.file(project,'sources/probe.txt').read_bytes()==source.read_bytes()
                report['encrypted_file_roundtrip']=True
                task=agent_store.add_turn(a,project,'plan','checkpoint test');ca=Coordinator(a,'boss-node-a','test',ttl=120)
                lease=ca.claim(task,'agent');runtime=None
                try:
                    with lease.bind():
                        a.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                        runtime=TaskRuntime(lease,IdentityPool(root/'node-a',start=660000,size=4))
                        env=agent_sandbox.prepare(a,project,'test','execute',task)
                        for name,data in (('edit/script.py',b'print("checkpoint")\n'),('edit/config.json',b'{"width":1280}')):
                            path=runtime.workspace/name;path.write_bytes(data);os.chown(path,runtime.uid,runtime.gid)
                        state=runtime.state/'session.json';state.write_text('{"test_session":"restorable"}');os.chown(state,runtime.uid,runtime.gid)
                        from webapp.checkpoints import profile
                        a.execute('INSERT INTO execution_session_profiles VALUES (?,?)',(project,profile()))
                        runtime.checkpoint()
                finally:
                    if runtime:runtime.close()
                    lease.release(interrupted=True)
                shutil.rmtree(a.root)
                lease=Coordinator(b,'boss-node-b','test',ttl=120).claim(task,'agent');runtime=None
                try:
                    with lease.bind():
                        runtime=TaskRuntime(lease,IdentityPool(root/'node-b',start=660100,size=4))
                        assert (runtime.workspace/'edit/script.py').read_bytes()==b'print("checkpoint")\n'
                        assert json.loads((runtime.workspace/'edit/config.json').read_text())=={'width':1280}
                        assert json.loads((runtime.state/'session.json').read_text())=={'test_session':'restorable'}
                        agent_store.finish(b,task,'succeeded','Real encrypted BOSS checkpoint restored')
                finally:
                    if runtime:runtime.close()
                    lease.release()
                report.update(checkpoint_restored_after_source_disk_removal=True,agent_session_restored=True,script_and_config_restored=True)
                records=b.query('SELECT object_key FROM artifact_objects WHERE project=?',(project,))
                report['objects']=len(records);assert report['objects']>2
                report['passed']=True
            finally:
                # Only this test's random prefix/project is authorized for cleanup.
                records=b.query('SELECT object_key FROM artifact_objects WHERE project=?',(project,))
                for record in records:
                    assert record['object_key'].startswith(prefix+'/projects/'+project+'/')
                    backend.delete(record['object_key'])
                report['deleted_test_objects']=len(records)
    except Exception as exc:
        import traceback
        report['error_location']=[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(exc.__traceback__)]
        report['error_type']=type(exc).__name__
        report['passed']=False
    finally:pg.close()
    from tests.run_workflow_suite import source_digest
    report['source_sha256']=source_digest()
    Path('docs/test-evidence/live-boss-storage.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    return not report['passed']


if __name__=='__main__':raise SystemExit(main())
