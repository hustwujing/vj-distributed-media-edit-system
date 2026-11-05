import hashlib
from pathlib import Path
import tempfile
import unittest

from webapp.sandbox_workspace import SandboxWorkspaceRestorer,WorkspaceManifestError


class Files:
    def __init__(self):self.calls=[];self.data={}
    def mkdir(self,path,mode):self.calls.append(('mkdir',path,mode))
    def write(self,path,data,mode):
        value=data.read() if hasattr(data,'read') else data;self.calls.append(('write',path,mode));self.data[path]=value


class WorkspaceRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.sources={}
    def artifact(self,identity,data):
        path=self.root/identity;path.write_bytes(data);self.sources[identity]=path
        return {'artifact':identity,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}

    def test_verifies_all_artifacts_then_restores_paths_and_modes(self):
        first=self.artifact('a',b'hello');second=self.artifact('b',b'echo ok')
        manifest={'workspace':{'input.txt':first,'tools/run.sh':{**second,'executable':True}},'state':{}}
        files=Files();result=SandboxWorkspaceRestorer(self.sources.__getitem__).restore(files,manifest)
        self.assertEqual(result,{'files':2,'bytes':12})
        self.assertEqual(files.calls[0],('mkdir','/workspace/tools',0o700))
        self.assertEqual(files.data['/workspace/input.txt'],b'hello')
        self.assertIn(('write','/workspace/tools/run.sh',0o700),files.calls)

    def test_digest_failure_causes_no_sandbox_mutation(self):
        record=self.artifact('a',b'actual');record['sha256']='0'*64;files=Files()
        with self.assertRaisesRegex(WorkspaceManifestError,'digest'):
            SandboxWorkspaceRestorer(self.sources.__getitem__).restore(files,{'workspace':{'a':record}})
        self.assertEqual(files.calls,[])

    def test_paths_symlinks_and_limits_are_rejected(self):
        record=self.artifact('a',b'x')
        for name in ('../escape','/absolute','a/../../b'):
            with self.subTest(name=name),self.assertRaises(WorkspaceManifestError):
                SandboxWorkspaceRestorer(self.sources.__getitem__).validate({'workspace':{name:record}})
        link=self.root/'link';link.symlink_to(self.sources['a']);self.sources['link']=link
        with self.assertRaisesRegex(WorkspaceManifestError,'safely'):
            SandboxWorkspaceRestorer(self.sources.__getitem__).restore(
                Files(),{'workspace':{'link':{**record,'artifact':'link'}}})
        with self.assertRaisesRegex(WorkspaceManifestError,'limits'):
            SandboxWorkspaceRestorer(self.sources.__getitem__,max_bytes=0).validate({'workspace':{'a':record}})


if __name__=='__main__':unittest.main()
