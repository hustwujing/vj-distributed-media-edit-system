import hashlib
from pathlib import Path
import tempfile
import unittest

from webapp.runtime_workspace import RuntimeWorkspaceError,WorkspaceCollector,WorkspaceExporter


class Files:
    def __init__(self,data):self.data=data
    def read(self,path):return self.data[path]


class RuntimeWorkspaceTests(unittest.TestCase):
    def test_export_is_complete_and_collector_verifies_same_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);workspace=root/'workspace';state=root/'state';workspace.mkdir();state.mkdir()
            (workspace/'edit').mkdir();(workspace/'edit/same.txt').write_text('same');(workspace/'edit/new.txt').write_text('new')
            (workspace/'sources').mkdir();(workspace/'sources/input.bin').write_bytes(b'input')
            (state/'cache').mkdir();(state/'cache/ignored').write_bytes(b'cache')
            (state/'session.bin').write_bytes(b'session')
            manifest=WorkspaceExporter(workspace,state).export()
            self.assertIn('edit/same.txt',manifest['workspace']);self.assertIn('edit/new.txt',manifest['workspace'])
            self.assertNotIn('sources/input.bin',manifest['workspace']);self.assertNotIn('cache/ignored',manifest['state'])
            remote={
                '/workspace/edit/same.txt':b'same','/workspace/edit/new.txt':b'new','/state/session.bin':b'session'}
            collected=[];result=WorkspaceCollector(Files(remote)).collect(manifest,
                lambda group,path,data,record:collected.append((group,path,data)))
            self.assertEqual(result,{'files':3,'bytes':14})
            self.assertEqual({row[:2] for row in collected},
                {('workspace','edit/same.txt'),('workspace','edit/new.txt'),('state','session.bin')})
            descriptor=WorkspaceExporter(workspace,state).export_descriptor(root/'manifest.json')
            self.assertEqual(descriptor['size'],(root/'manifest.json').stat().st_size)
            self.assertEqual(descriptor['sha256'],hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest())

    def test_export_and_collect_reject_links_escape_and_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);workspace=root/'workspace';state=root/'state';workspace.mkdir();state.mkdir()
            (workspace/'edit').mkdir();(workspace/'edit/target').write_text('x');(workspace/'edit/link').symlink_to(workspace/'edit/target')
            with self.assertRaises(RuntimeWorkspaceError):WorkspaceExporter(workspace,state).export()
        record={'size':1,'sha256':hashlib.sha256(b'x').hexdigest(),'executable':False}
        with self.assertRaises(RuntimeWorkspaceError):
            WorkspaceCollector(Files({'/workspace/a':b'y'})).collect(
                {'workspace':{'a':record}},lambda *args:None)
        with self.assertRaises(RuntimeWorkspaceError):
            WorkspaceCollector(Files({})).collect(
                {'workspace':{'../escape':record}},lambda *args:None)


if __name__=='__main__':unittest.main()
