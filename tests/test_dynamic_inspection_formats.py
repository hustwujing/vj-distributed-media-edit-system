"""Real ZIP bytes are inspected as bounded metadata, never media or extracted content."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
from PIL import Image
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

    def test_svg_exposes_editable_text_and_embedded_source_without_execution(self):
        import base64
        png=io.BytesIO();Image.new('RGB',(2,2),'blue').save(png,format='PNG');data=png.getvalue()
        path=self.root/'editable.svg';path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="384" height="256">'
            '<defs><filter id="shadow"><feDropShadow dx="2" dy="2" stdDeviation="2"/></filter></defs>'
            '<image href="data:image/png;base64,'+base64.b64encode(data).decode()+'"/>'
            '<text x="192" text-anchor="middle" fill="white" stroke="black" filter="url(#shadow)"><tspan>可编辑标题</tspan></text></svg>')
        with patch('subprocess.run',side_effect=AssertionError('SVG inspection must not execute a renderer')):
            result=self.inspect_file(path)
        self.assertEqual(result['coverage'],'svg_structure');self.assertTrue(result['vector_image'])
        self.assertEqual(result['images'][0]['sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(result['text_nodes'][1]['text'],'可编辑标题')
        self.assertEqual(result['text_nodes'][0]['attributes']['text-anchor'],'middle')
        self.assertEqual(result['effects'][1]['tag'],'feDropShadow')
        self.assertNotIn(base64.b64encode(data).decode(),json.dumps(result,default=str))
        path.write_text('<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>')
        with self.assertRaises(InspectionInputError):self.inspect_file(path)

    def test_svg_text_acceptance_uses_real_xml_integrity_and_shared_format_support(self):
        import subprocess,sys
        from webapp.quality_policy import Acceptance,validate_typed_output_targets
        checker=Path(__file__).resolve().parents[1]/'webapp/quality_check.py'
        rule={'kind':'text','file':'title.svg','contains':['可编辑标题','<text']}
        validate_typed_output_targets([Acceptance.model_validate(rule)],['title.svg'])
        for content,passed in [('<svg xmlns="http://www.w3.org/2000/svg"><text>可编辑标题</text></svg>',True),
                               ('<svg><text>可编辑标题',False),
                               ('<!DOCTYPE svg [<!ENTITY x "可编辑标题">]><svg><text>&x;</text></svg>',False)]:
            path=self.root/'title.svg';path.write_text(content)
            result=subprocess.run([sys.executable,str(checker)],cwd=self.root,
                input=json.dumps({'files':[path.name],'acceptance':[rule]}),capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout)['passed'],passed,result.stdout)

    def test_svg_format_comes_from_logical_reference_not_object_cache_filename(self):
        path=self.root/('c'*64);path.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>缓存标题</text></svg>')
        reference='edit/candidate/title.svg'
        record={'artifact':'owned-object','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'size':path.stat().st_size,'location':path.name}
        for shared in (False,True):
            store=SimpleNamespace(shared_files=shared,root=self.root,
                artifacts=SimpleNamespace(materialize=lambda project,artifact:path))
            evidence=inspect(store,{'project_id':'owned'},{reference:{'record':record}},reference)
            self.assertEqual(evidence['coverage'],'svg_structure')
            self.assertEqual(evidence['text_nodes'][0]['text'],'缓存标题')

    def test_svg_preserves_parent_filters_paint_order_and_filter_parameters(self):
        path=self.root/'effects.svg';path.write_text('<svg xmlns="http://www.w3.org/2000/svg" xmlns:c="urn:custom">'
            '<defs><filter id="shadow"><feComponentTransfer><feFuncA type="linear" slope="0.65"/></feComponentTransfer></filter></defs>'
            '<g filter="url(#shadow)"><text paint-order="stroke fill" stroke-linejoin="round" c:fill="unimplemented">标题</text></g></svg>')
        with patch('subprocess.run',side_effect=AssertionError('Structural inspection cannot execute SVG')):
            evidence=self.inspect_file(path)
        text=evidence['text_nodes'][0];parent=evidence['elements'][text['parent_index']]
        self.assertEqual(text['attributes']['paint-order'],'stroke fill')
        self.assertEqual(text['attributes']['stroke-linejoin'],'round')
        self.assertEqual(parent['tag'],'g');self.assertEqual(parent['attributes']['filter'],'url(#shadow)')
        self.assertNotIn('fill',text['attributes']);self.assertEqual(text['attributes']['{urn:custom}fill'],'unimplemented')
        transfer=next(item for item in evidence['effects'] if item['tag']=='feFuncA')
        self.assertEqual(transfer['attributes'],{'type':'linear','slope':'0.65'})

    def test_svg_external_or_active_content_is_structure_only(self):
        for content in ('<image href="file:///private.png"/>','<script>ignored()</script>',
                        '<style>@import url(https://example.invalid/x.css);</style>',
                        '<rect fill="url(https://example.invalid/paint.svg#x)" width="10" height="10"/>'):
            path=self.root/'external.svg';path.write_text('<svg xmlns="http://www.w3.org/2000/svg">'+content+'</svg>')
            result=self.inspect_file(path)
            self.assertFalse(result['static_rendering_eligible']);self.assertNotIn('media',result)

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

    def test_openraster_evidence_binds_mimetype_structure_and_member_dimensions(self):
        path=self.root/'editable.ora'
        def png(width,height):
            output=io.BytesIO();Image.new('RGBA',(width,height),(255,255,255,255)).save(output,format='PNG');return output.getvalue()
        stack=b'<?xml version="1.0"?><image w="512" h="512" xmlns:custom="urn:unimplemented-adjustment"><stack><layer name="Contrast adjustment" src="data/one.png" custom:contrast="1.5"/><layer name="two" src="data/two.png"/></stack></image>'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('mimetype',b'image/openraster',compress_type=zipfile.ZIP_STORED)
            archive.writestr('stack.xml',stack)
            archive.writestr('data/one.png',png(512,512));archive.writestr('data/two.png',png(512,512))
            archive.writestr('mergedimage.png',png(512,512));archive.writestr('Thumbnails/thumbnail.png',png(128,128))
        result=self.inspect_file(path)
        self.assertEqual(result['coverage'],'layer_structure');self.assertTrue(result['mimetype_valid'])
        self.assertTrue(result['mergedimage_present']);self.assertTrue(result['thumbnail_present'])
        self.assertTrue(result['all_layer_dimensions_match_canvas']);self.assertFalse(result['contents_verified'])
        self.assertFalse(result['editing_semantics']['parametric_adjustment_editability_verified'])
        self.assertFalse(result['editing_semantics']['custom_attributes_interpreted'])
        self.assertEqual(result['editing_semantics']['uninterpreted_layer_attributes'],['{urn:unimplemented-adjustment}contrast'])
        self.assertEqual(result['archive_members'][0]['compression'],'stored')
        self.assertEqual({item['name']:(item['width'],item['height']) for item in result['member_images']}['data/one.png'],(512,512))
