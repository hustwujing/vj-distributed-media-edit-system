"""Explicit ready-plan fixtures. Semantic network behavior is tested separately."""
from unittest.mock import patch
from webapp import agent_store
from webapp.execution_intent import declare
from webapp.plan_readiness import review

def ready_plan(store,task,text='明确处理目标，使用本地脚本，生成 PNG 并检查尺寸。'):
    store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
    declare(store,task,{'route':'single','operations':['script'],'goal':'生成并检查已指定结果','estimated_seconds':30})
    with patch('webapp.semantic_review.classify',return_value='ready'):review(store,task,text)
    agent_store.finish(store,task,'succeeded',text)
