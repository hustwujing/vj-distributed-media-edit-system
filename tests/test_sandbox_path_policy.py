import unittest

from webapp.sandbox_path_policy import contract,is_reserved_scratch_path,is_scratch_path,scratch_directory


class SandboxPathPolicyTests(unittest.TestCase):
    def test_task_roots_are_canonical_and_scratch_is_reserved(self):
        output='/workspace/edit/workflows/run/task/attempt'
        self.assertEqual(scratch_directory(output),output+'/.scratch')
        self.assertEqual(contract(output)['shell_working_directory'],'/workspace')
        self.assertTrue(is_scratch_path('edit/workflows/run/task/attempt/.scratch/a.tmp',output))
        self.assertTrue(is_reserved_scratch_path('edit/workflows/run/task/attempt/.scratch/a.tmp'))
        self.assertFalse(is_scratch_path('edit/workflows/run/task/attempt/result.mp4',output))
        self.assertFalse(is_reserved_scratch_path('edit/project/.scratch/notes.txt'))

    def test_outputs_cannot_escape_the_task_namespace(self):
        for value in ('/tmp/work','/workspace','/workspace/edit/workflows','/workspace/edit/workflows/run',
                      '/workspace/edit/workflows/../escape'):
            with self.subTest(value=value),self.assertRaises(ValueError):scratch_directory(value)


if __name__=='__main__':unittest.main()
