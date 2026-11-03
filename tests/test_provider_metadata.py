"""Independent budget, concurrency, routing, identity and cleanup contracts."""
from dataclasses import replace
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from webapp import operations
from webapp.provider_metadata import metadata
from tests.test_external_cleanup import ExternalCleanupTests


class ProviderMetadataTests(unittest.TestCase):
    def test_local_contract_has_no_network_or_external_quota(self):
        local=operations.get('media.concat_sequence')
        public=local.public()
        self.assertNotIn('provider',public)
        self.assertFalse(public['external_side_effects'])
        self.assertEqual(public['permissions']['network'],'none')
        self.assertTrue(local.reuse_safe)
        with self.assertRaises(ValueError):replace(local,route_adapter='image_edit')

    def test_policy_fields_are_independent_and_unknown_routes_fail_closed(self):
        external=operations.get('image.edit')
        alternate=replace(external,budget_group='shared-image-budget',concurrency_group='separate-pool',provider_identity='other-editor')
        self.assertEqual(alternate.route_adapter,'image_edit')
        self.assertEqual(alternate.budget_group,'shared-image-budget')
        self.assertNotEqual(alternate.concurrency_group,external.concurrency_group)
        for change in ({'route_adapter':'unregistered'}, {'cleanup_category':'unqueryable_sync'},
                       {'budget_group':''}, {'reuse_safe':True}):
            with self.assertRaises(ValueError):replace(external,**change)
        with self.assertRaises(TypeError):replace(external,provider='edit')

    def test_wan_ark_and_jobs_have_distinct_identity_and_cleanup(self):
        for backend,identity,cleanup in [('wan','wan','generation_protocol'),('ark','ark','generation_protocol'),('jobs','generation-jobs','job_api')]:
            with patch.dict(os.environ,{'VIDEO_USE_IMAGE_VIDEO_BACKEND':backend,'VIDEO_USE_GENERATION_BACKEND':backend}):
                op=operations.get('video.generate_from_image')
                self.assertEqual(op.provider_identity,identity)
                self.assertEqual(op.cleanup_category,cleanup)
                self.assertEqual(op.budget_group,'video.generate_from_image')
        with patch.dict(os.environ,{'VIDEO_USE_IMAGE_GENERATION_BACKEND':'jobs'}):
            asynchronous=operations.get('image.generate_from_text','generation_async@1')
            synchronous=operations.get('image.generate_from_text','image_sync@1')
            self.assertEqual(asynchronous.budget_group,synchronous.budget_group)
            self.assertNotEqual(asynchronous.concurrency_group,synchronous.concurrency_group)
            self.assertEqual(synchronous.cleanup_category,'unqueryable_sync')


class FrozenCleanupMetadataTests(unittest.TestCase):
    setUp=ExternalCleanupTests.setUp
    tearDown=ExternalCleanupTests.tearDown

    def test_unknown_frozen_route_never_falls_back_to_image_api(self):
        from webapp.external_cleanup import run
        self.store.execute('INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,inputs,created,updated,implementation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            ('one','run','image',0,1,'http://127.0.0.1:9898','image.edit','waiting','{}',0,0,json.dumps({'route_adapter':'missing','cleanup_category':'job_api'})))
        with patch('webapp.external_cleanup.session') as factory:
            run(self.store,'one')
            factory.assert_not_called()
        self.assertEqual(self.store.query('SELECT state FROM execution_external_cleanup')[0]['state'],'pending')

    def test_saved_wan_route_is_used_after_configuration_changes(self):
        from webapp.external_cleanup import run
        with patch.dict(os.environ,{'VIDEO_USE_IMAGE_VIDEO_BACKEND':'wan'}):
            binding={**metadata(operations.get('video.generate_from_image')),'protocol':'wan-video-v1','provider':'http://127.0.0.1:9898'}
        self.store.execute('INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,inputs,created,updated,implementation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            ('one','run','video',0,1,binding['provider'],'video.generate_from_image','waiting','{}',0,0,json.dumps(binding)))
        client=MagicMock();client.__enter__.return_value=client
        with patch.dict(os.environ,{'VIDEO_USE_IMAGE_VIDEO_BACKEND':'ark'}), patch('webapp.external_cleanup.session',return_value=client):
            run(self.store,'one')
        self.assertEqual(self.store.query('SELECT state FROM execution_external_cleanup')[0]['state'],'unsupported')
        client.request.assert_not_called()
