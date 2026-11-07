import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp import agent_store
from webapp.store import Store
from webapp.task_leases import Coordinator,TaskQuotaReached
from webapp.workflow_runtime import Adapter


class ImageWorkReached(Exception):pass


class FixedImageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name));agent_store.initialize(self.store)
        self.project=self.store.add_project('owner','quota')['id']
        self.turn=agent_store.add_turn(self.store,self.project,'plan','test')
        self.lease=Coordinator(self.store,'node','test').claim(self.turn,'agent');self.addCleanup(self.lease.release)
        self.adapter=Adapter(self.store,'test',self.store.root)
        self.workflow={'id':self.turn,'run_id':self.turn,'project':self.project}
        self.step={'id':'image','revision':0,'spec':{'timeout':10}}

    def test_one_fixed_image_step_consumes_once_and_respects_configured_limit(self):
        with self.lease.bind(),patch.dict(os.environ,{'VIDEO_USE_TASK_LOCAL_IMAGE_CALLS':'25','VIDEO_USE_USER_LOCAL_IMAGE_DAILY':'40'}), \
             patch.object(self.adapter,'environment',return_value={}), \
             patch('webapp.workflow_inputs.image_task',side_effect=ImageWorkReached):
            for index in range(25):
                with self.assertRaises(ImageWorkReached):self.adapter.fixed_image(self.workflow,self.step,lambda:False)
                self.assertEqual(self.store.query("SELECT used FROM execution_budgets WHERE category='local_image'")[0]['used'],index+1)
                self.assertEqual(self.store.query("SELECT used FROM execution_daily_usage WHERE category='local_image'")[0]['used'],index+1)
            with self.assertRaises(TaskQuotaReached) as raised:self.adapter.fixed_image(self.workflow,self.step,lambda:False)
            self.assertEqual(raised.exception.diagnostic['limit'],25)
            self.assertEqual(raised.exception.diagnostic['scope'],'task')
            self.assertEqual(self.store.query("SELECT used FROM execution_daily_usage WHERE category='local_image'")[0]['used'],25)

    def test_missing_lease_rejects_before_materializing_environment(self):
        with patch.object(self.adapter,'environment') as environment:
            with self.assertRaisesRegex(RuntimeError,'有效执行租约'):
                self.adapter.fixed_image(self.workflow,self.step,lambda:False)
            environment.assert_not_called()
