import unittest

from webapp.demo_agent_requirements import REQUIREMENTS,apply
from webapp.demo_library import conversation_prompt


class DemoAgentRequirementsTests(unittest.TestCase):
    def test_all_repaired_examples_are_self_contained_and_provider_neutral(self):
        catalog=[{'id':identifier,'prompt':'old','options':['old']} for identifier in REQUIREMENTS]
        normalized,changed=apply(catalog)
        self.assertEqual(set(changed),set(REQUIREMENTS))
        prompts={item['id']:conversation_prompt(item) for item in normalized}
        self.assertNotIn('Codex',prompts['agent-portrait'])
        self.assertIn('任务设置',prompts['agent-portrait'])
        self.assertIn('源素材没有音轨',prompts['local-split'])
        self.assertIn('自动换行标题示例',prompts['image-text-effects'])
        self.assertIn('复制成3个',prompts['image-layout-guides'])
        self.assertIn('版本1原片和版本2暖调成片',prompts['video-version-compare'])
        self.assertIn('3–6秒',prompts['video-shot-notes'])
        self.assertIn('对比度1.18',prompts['image-adjustment-layers'])
        self.assertIn('非生成式修补',prompts['image-erase'])
        self.assertIn('不需要再次询问',prompts['image-local'])
        self.assertIn('精确蒙版本地换色',prompts['image-local'])

    def test_application_is_idempotent_and_preserves_unrelated_examples(self):
        catalog=[{'id':'local-split','prompt':'old','options':[]},{'id':'other','prompt':'keep','options':['keep']}]
        first,changed=apply(catalog);second,again=apply(first)
        self.assertEqual(changed,['local-split']);self.assertEqual(again,[])
        self.assertEqual(first,second)
        self.assertEqual(first[1],catalog[1])


if __name__=='__main__':unittest.main()
