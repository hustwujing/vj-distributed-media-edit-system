from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from webapp.operations import get,validate_parameters
from webapp.workflows import Step
from webapp.workflow_inputs import image_task
from webapp.image_tasks import execute_image
from webapp.media_evidence import mask_change_evidence


class MaskOperationContractTests(unittest.TestCase):
    def test_registered_refinement_requires_only_an_explicit_mask(self):
        contract=get('image.refine_mask').public()['input_contract']
        self.assertEqual(contract['required_roles'],['mask']);self.assertEqual(contract['allowed_roles'],['mask'])
        for inputs in ({'source':{'asset':'photo'}},{'source':{'asset':'photo'},'mask':{'asset':'mask'}}):
            with self.assertRaises(ValueError):validate_parameters('image.refine_mask',{},inputs)
        step=Step(id='refine',name='refine',kind='operation',operation='image.refine_mask',inputs={'mask':{'asset':'mask'}})
        self.assertEqual(set(step.inputs),{'mask'})

    def test_worker_receives_mask_pixels_and_measurement_uses_the_same_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sources=root/'sources';sources.mkdir()
            mask=np.zeros((32,32),dtype=np.uint8);mask[8:24,8:24]=255
            Image.fromarray(mask).save(sources/'mask.png');Image.new('RGB',(32,32),(40,90,140)).save(sources/'photo.png')
            assets=[{'id':name,'filename':name+'.png','metadata':{'kind':'image','width':32,'height':32}} for name in ('mask','photo')]
            step=Step(id='refine',name='refine',kind='operation',operation='image.refine_mask',inputs={'mask':{'asset':'mask'}},
                      parameters={'parameters':{'feather':0,'grow':0,'threshold':128,'fill_holes':False,'remove_small':0}})
            with patch('webapp.workflow_inputs.staged_assets',return_value=(assets,{'mask':'mask','source':'photo'})):
                task,bound=image_task(None,{}, {'spec':step.model_dump()}, {})
            self.assertEqual(task['asset'],'mask');self.assertIsNone(task['mask'])
            execute_image(task,bound,root,root/'output')
            np.testing.assert_array_equal(np.array(Image.open(root/'output/mask.png')),mask)
            self.assertEqual(mask_change_evidence(sources/'mask.png',root/'output/mask.png')['changed_fraction'],0)
