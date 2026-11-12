import datetime
import os
import unittest
from unittest.mock import patch

from webapp import ark_reference_assets as assets


class ArkReferenceAssetTests(unittest.TestCase):
    def test_signature_is_deterministic_and_does_not_expose_secret(self):
        query,headers=assets._headers('open.example','access','private-secret','GetAsset',b'{}',
            datetime.datetime(2026,1,2,3,4,5,tzinfo=datetime.timezone.utc))
        self.assertEqual(query,'Action=GetAsset&Version=2024-01-01')
        self.assertIn('Credential=access/20260102/cn-beijing/ark/request',headers['Authorization'])
        self.assertNotIn('private-secret',str(headers))

    def test_reference_capability_requires_all_host_credentials(self):
        names=('VIDEO_USE_ARK_ACCESS_KEY','VIDEO_USE_ARK_SECRET_KEY','VIDEO_USE_ARK_PROJECT_NAME',
            'VIDEO_USE_ARK_ASSET_GROUP_ID','VIDEO_USE_OBJECT_BUCKET','VIDEO_USE_OBJECT_ENDPOINT')
        with patch.dict(os.environ,{name:'configured' for name in names},clear=True):
            self.assertTrue(assets.configured())
            for missing in names:
                with self.subTest(missing=missing),patch.dict(os.environ,{missing:''}):
                    self.assertFalse(assets.configured())

    def test_asset_names_are_content_addressed(self):
        self.assertEqual(assets._asset_name('a'*64),'distributed-agent-'+'a'*40)


if __name__=='__main__':unittest.main()
