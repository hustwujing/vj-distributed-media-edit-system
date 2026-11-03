import os
import unittest
from unittest.mock import patch
from webapp.operations import for_step
from webapp.operation_lifecycle import Implementation,Lifecycle,register,IMPLEMENTATIONS
from webapp.operation_result import Result
from webapp.workflows import Step


class OperationLifecycleTests(unittest.TestCase):
    def test_workflow_cannot_swap_approved_implementation_or_expand_paid_calls(self):
        import hashlib
        from webapp import operations,workflows
        from webapp.execution_intent import Intent
        from tests.test_workflows import WorkflowTests
        identifier='intent_image_variant@2'
        register(Implementation(identifier,Lifecycle.ASYNC))
        operations.register_implementation('image.edit',identifier,retry_safe=True)
        fixture=WorkflowTests();fixture.setUp()
        try:
            def approve(implementation):
                value=Intent(route='workflow',operations=['image.edit'],implementations={'image.edit':implementation},
                             goal='one approved image',estimated_seconds=300).model_dump_json()
                fixture.store.execute('''INSERT INTO execution_intents VALUES (?,?,?,?)
                  ON CONFLICT(run_id) DO UPDATE SET intent=excluded.intent,digest=excluded.digest''',
                  (fixture.plan,value,hashlib.sha256(value.encode()).hexdigest(),fixture.store.now()))
            step={'id':'first','name':'first','kind':'operation','operation':'image.edit','implementation':identifier,
                  'inputs':{'source':{'asset':'asset'}},'parameters':{'prompt':'blue'},'goal':'approved image',
                  'acceptance':[{'kind':'image','width':8}],'on_failure':'pause'}
            approve('image_async@1')
            with self.assertRaisesRegex(ValueError,'实现与已确认'):
                workflows.submit(fixture.store,fixture.turn,{'key':'one','name':'one','steps':[step]})
            approve(identifier)
            with self.assertRaisesRegex(ValueError,'外部调用次数'):
                workflows.submit(fixture.store,fixture.turn,{'key':'two','name':'two','steps':[step,{**step,'id':'second'}]})
            self.assertIsNone(workflows.get(fixture.store,fixture.turn))
        finally:
            fixture.tearDown()
            IMPLEMENTATIONS.pop(identifier,None)
            operations._IMPLEMENTATION_BINDINGS.get('image.edit',{}).pop(identifier,None)
            operations._VARIANT_CONTRACTS.pop(('image.edit',identifier),None)

    def test_intent_pins_implementation_before_default_backend_changes(self):
        from webapp.execution_intent import Intent
        with patch.dict(os.environ,{'VIDEO_USE_IMAGE_GENERATION_BACKEND':'jobs'}):
            intent=Intent(route='workflow',operations=['image.generate_from_text'],goal='image',estimated_seconds=300)
        with patch.dict(os.environ,{'VIDEO_USE_IMAGE_GENERATION_BACKEND':'auto','VIDEO_USE_MODEL_BASE_URL':'https://another.invalid'}):
            self.assertEqual(intent.implementations,{'image.generate_from_text':'generation_async@1'})
            self.assertEqual(for_step({'kind':'operation','operation':'image.generate_from_text','implementation':intent.implementations['image.generate_from_text']}).executor,'generation_async')
        with self.assertRaises(ValueError):
            Intent(route='single',operations=['image.edit'],implementations={'audio.transcribe':'transcribe@1'},goal='image',estimated_seconds=30)

    def test_builtin_alternative_keeps_business_parameter_contract(self):
        from webapp import operations
        identifier='media_alternative_contract@2'
        register(Implementation(identifier,Lifecycle.LOCAL,'fixed_media'))
        try:
            original=operations.get('media.render').public()['parameters_schema']
            operations.register_implementation('media.render',identifier,cpu=1)
            spec=Step(id='render',name='render',kind='operation',operation='media.render',implementation=identifier,
                      inputs={'source':{'asset':'a'}},parameters={'segments':[{'asset':'source','start':0,'end':1}]})
            self.assertEqual(spec.implementation,identifier)
            selected=operations.for_step(spec.model_dump())
            self.assertEqual(selected.public()['parameters_schema'],original)
            self.assertEqual(selected.cpu,1)
            with self.assertRaises(ValueError):
                operations.validate_parameters('media.render',{'segments':[{'asset':'source','start':2,'end':1}]},
                                               {'source':{'asset':'a'}},identifier)
        finally:
            IMPLEMENTATIONS.pop(identifier,None)
            operations._IMPLEMENTATION_BINDINGS.get('media.render',{}).pop(identifier,None)
            operations._VARIANT_CONTRACTS.pop(('media.render',identifier),None)

    def test_primary_implementation_may_start_at_a_version_other_than_one(self):
        from pydantic import BaseModel,ConfigDict
        from webapp import operations
        identifier='test_primary_revision@2';operation='media.test_primary_revision'
        class Parameters(BaseModel):
            model_config=ConfigDict(extra='forbid')
            value:int=1
        register(Implementation(identifier,Lifecycle.LOCAL,'fixed_media'))
        try:
            operations.register(operations.Operation(operation,'test','test_primary_revision',implementation_id=identifier),
                                operations.OperationContract(Parameters))
            spec=Step(id='test',name='test',kind='operation',operation=operation)
            self.assertEqual(spec.implementation,identifier)
            self.assertEqual(operations.for_step(spec.model_dump()).public()['implementations'],[identifier])
        finally:
            IMPLEMENTATIONS.pop(identifier,None)
            operations.REGISTRY.pop(operation,None);operations._CONTRACTS.pop(operation,None)

    def test_selected_implementation_survives_configuration_change(self):
        with patch.dict(os.environ,{'VIDEO_USE_IMAGE_GENERATION_BACKEND':'jobs'}):
            spec=Step(id='image',name='image',kind='operation',operation='image.generate_from_text',parameters={'prompt':'blue icon'}).model_dump()
        self.assertEqual(spec['implementation'],'generation_async@1')
        with patch.dict(os.environ,{'VIDEO_USE_IMAGE_GENERATION_BACKEND':'auto','VIDEO_USE_MODEL_BASE_URL':'https://different.invalid'}):
            self.assertEqual(for_step(spec).executor,'generation_async')

    def test_caller_cannot_swap_implementation_to_another_capability(self):
        for identifier in ('fixed_media@1','image_sync@9','https://arbitrary.invalid'):
            with self.assertRaises(ValueError):
                Step(id='remove',name='remove',kind='operation',operation='image.remove_background',
                     implementation=identifier,inputs={'source':{'asset':'owned'}})
        with self.assertRaises(ValueError):Step(id='remove',name='remove',kind='operation',operation='image.remove_background',contract_version=2,inputs={'source':{'asset':'owned'}})

    def test_new_registered_handler_uses_existing_protocol(self):
        # This exercises protocol extension without adding a Runner branch.
        implementation=Implementation('test_extractor@1',Lifecycle.LOCAL,'extract',cancellation='terminate_scope')
        register(implementation)
        try:
            from pydantic import BaseModel,ConfigDict
            from webapp import operations
            class Parameters(BaseModel):
                model_config=ConfigDict(extra='forbid')
                sample_rate:int=16000
            operations.register(operations.Operation('audio.test_extract','extract','test_extractor',('video',),('audio',)),
                operations.OperationContract(Parameters,frozenset({'source'}),frozenset({'source'})))
            spec=Step(id='extract',name='extract',kind='operation',operation='audio.test_extract',inputs={'source':{'asset':'video'}})
            self.assertEqual(spec.parameters,{'sample_rate':16000})
            self.assertEqual(spec.implementation,implementation.id)
            self.assertEqual(operations.get('audio.test_extract').public()['lifecycle'],'local')
            class Adapter:
                def extract(self,w,s,cancelled):
                    return [w['id'],s['id'],cancelled()]
            self.assertEqual(implementation.invoke(Adapter(),'local',{'id':'w'},{'id':'s'},cancelled=lambda:False),['w','s',False])
            with self.assertRaises(ValueError):implementation.invoke(Adapter(),'submit',{}, {})
            with self.assertRaises(ValueError):register(implementation)
        finally:
            IMPLEMENTATIONS.pop(implementation.id,None)
            from webapp import operations
            operations.REGISTRY.pop('audio.test_extract',None);operations._CONTRACTS.pop('audio.test_extract',None)

    def test_result_rejects_ambiguous_roles_and_unsafe_paths(self):
        for outputs in (
            [{'role':'video','path':'../outside.mp4'}],
            [{'role':'video','path':'a.mp4'},{'role':'video','path':'b.mp4'}],
            [{'role':'video','path':'a.mp4'},{'role':'preview','path':'a.mp4'}],
        ):
            with self.assertRaises(ValueError):Result(outputs=outputs)

    def test_alternative_implementation_keeps_contract_and_pins_version(self):
        from webapp import operations
        from webapp.operation_parameters import Parameters
        operations.register(operations.Operation('test.multiple','multiple','fixed_image',(),('report',)),operations.OperationContract(Parameters))
        implementation=Implementation('test_alternative@2',Lifecycle.LOCAL,handlers={'local':lambda *a,**kw:[]})
        register(implementation)
        try:
            operations.register_implementation('test.multiple',implementation.id,memory_mb=1024)
            spec=Step(id='multi',name='multi',kind='operation',operation='test.multiple',implementation='test_alternative@2')
            self.assertEqual(spec.implementation,'test_alternative@2')
            self.assertEqual(for_step(spec.model_dump()).memory_mb,1024)
            self.assertEqual(operations.contract(for_step(spec.model_dump())).parameters,Parameters)
            self.assertIn('test_alternative@2',operations.get('test.multiple').public()['implementations'])
            with self.assertRaises(ValueError):operations.register_implementation('test.multiple',implementation.id)
            with self.assertRaises(ValueError):operations.register_implementation('image.ocr',implementation.id,outputs=('video',))
        finally:
            IMPLEMENTATIONS.pop(implementation.id,None)
            operations.REGISTRY.pop('test.multiple',None);operations._CONTRACTS.pop('test.multiple',None)
            operations._IMPLEMENTATION_BINDINGS.pop('test.multiple',None)

    def test_worker_rejects_unknown_action_before_running_media(self):
        import tempfile
        from webapp.worker import execute
        with tempfile.TemporaryDirectory() as root,self.assertRaisesRegex(ValueError,'Unknown worker action'):
            execute({'project_dir':root,'output_dir':root+'/out','payload':{'kind':'typo'},'assets':[]})

    def test_media_schema_and_validation_share_contract(self):
        from webapp.operations import get,validate_parameters
        schema=get('media.render').public()['parameters_schema']
        self.assertIn('segments',schema['required'])
        self.assertNotIn('language',schema['properties'])
        with self.assertRaises(ValueError):validate_parameters('media.render',{'segments':[]},{'source':{'asset':'a'}})
        with self.assertRaises(ValueError):validate_parameters('media.analyze',{'start':10,'end':5},{'source':{'asset':'a'}})

    def test_file_existence_cannot_waive_video_output_contract(self):
        from webapp.operation_result import validate_contract
        from webapp.operations import get
        report={'technical_valid':True,'passed':True,'checks':[],
                'files':[{'name':'result.txt','kind':'file','size':10}]}
        checked=validate_contract(get('video.generate_from_image'),report)
        self.assertFalse(checked['technical_valid'])
        self.assertFalse(checked['passed'])


if __name__=='__main__':unittest.main()
