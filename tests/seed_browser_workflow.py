import sys
from pathlib import Path
from webapp.store import Store
from webapp import agent_store,workflows,user_decisions
store=Store(Path(sys.argv[1]));project=sys.argv[2]
plan=agent_store.add_turn(store,project,'plan','测试：选择横屏或竖屏')
agent_store.finish(store,plan,'succeeded','请确认画幅')
turn=agent_store.add_turn(store,project,'execute','执行',plan_id=plan)
store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(turn,))
workflows.submit(store,turn,{'key':'browser','name':'画幅决策测试','steps':[{'id':'compose','name':'合成视频','kind':'script','argv':['python','edit/compose.py'],'outputs':['final.mp4'],'goal':'按选择合成视频','on_failure':'diagnose','acceptance':[{'kind':'media','video':True}]}]})
w=workflows.get(store,turn)
workflows.change(store,w['id'],'compose',status='awaiting_review',error_code='quality',error='请选择画幅')
workflows.state(store,turn,'paused',error='需要选择画幅，质量确认不能代替此决定。')
user_decisions.create(store,w,w['steps'][0],{'kind':'selection','question':'最终视频使用哪种画幅？','options':['横屏','竖屏']})
