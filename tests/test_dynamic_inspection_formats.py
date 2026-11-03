"""Real ZIP bytes are inspected as bounded metadata, never media or extracted content."""
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
from webapp.workflow_agents import inspect,InspectionInputError


class InspectionFormatTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def inspect_file(self,path):
        reference='edit/candidate/'+path.name
        record={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'size':path.stat().st_size,'location':path.name}
        return inspect(SimpleNamespace(shared_files=False,root=self.root),{'project_id':'owned'},
                       {reference:{'record':record}},reference)

    def test_real_segment_archive_lists_members_without_extracting_or_claiming_media(self):
        path=self.root/'segments.zip'
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
            for number in range(1,25):archive.writestr(f'segment_{number:02d}.mp4',b'opaque segment bytes')
        with patch.object(zipfile.ZipFile,'open',side_effect=AssertionError('Must not decompress members')):
            result=self.inspect_file(path)
        self.assertNotIn('media',result);self.assertEqual(result['coverage'],'archive_directory')
        self.assertEqual(result['member_count'],24);self.assertFalse(result['contents_verified'])
        self.assertEqual(result['members'][0]['name'],'segment_01.mp4')
        self.assertEqual(result['members'][-1]['name'],'segment_24.mp4')
        self.assertEqual(list(self.root.iterdir()),[path])

    def test_unsafe_or_oversized_archive_directory_is_rejected_without_extraction(self):
        for name in ['../escape.mp4','/absolute.mp4',r'windows\escape.mp4']:
            with self.subTest(name=name):
                path=self.root/'unsafe.zip'
                with zipfile.ZipFile(path,'w') as archive:archive.writestr(name,b'content')
                with self.assertRaises(InspectionInputError) as error:self.inspect_file(path)
                self.assertEqual(error.exception.code,'inspection_archive_member')
        path=self.root/'many.zip'
        with zipfile.ZipFile(path,'w') as archive:
            for index in range(1001):archive.writestr(str(index),b'')
        with self.assertRaises(InspectionInputError) as error:self.inspect_file(path)
        self.assertEqual(error.exception.code,'inspection_archive_limit')

    def test_invalid_archives_and_unsupported_documents_are_input_errors(self):
        for name,code in [('invalid.zip','inspection_archive_invalid'),('document.pdf','unsupported_inspection_type')]:
            with self.subTest(name=name):
                path=self.root/name;path.write_bytes(b'not media')
                with self.assertRaises(InspectionInputError) as error:self.inspect_file(path)
                self.assertEqual(error.exception.code,code)
