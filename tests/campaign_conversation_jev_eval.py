import os,json,tempfile
from pathlib import Path
from deploy.dynamic_cluster import environment
os.environ.update(environment());os.environ.update(VIDEO_USE_DATABASE_URL='',VIDEO_USE_DISTRIBUTED='0',VIDEO_USE_OBJECT_BUCKET='')
from webapp import agent_store
from webapp.store import Store
from webapp.semantic_review import classify,SemanticUnavailable
proposal='输入图片a（128×96），用Pillow顺时针旋转90度，不缩放，交付96×128 PNG，保留原图，检查尺寸和可读性。失败时停止；本地处理不调用外部服务，预计20秒。等待用户批准执行。'
cases=[('valid','plan',{'user_request':'请将图片a顺时针旋转90度，先给方案。','proposal':proposal,'assets':[{'id':'a','kind':'image','width':128,'height':96}],'intent':{'goal':'旋转图片','operations':['script'],'estimated_seconds':20}},['ready']),
 ('greeting','plan',{'user_request':'你好','proposal':'你好！有什么需要帮助的吗？','intent':None},['discussion']),
 ('unsolicited','plan',{'user_request':'你好','proposal':proposal,'intent':{'goal':'旋转图片','operations':['script']}},['discussion','needs_information','uncertain']),
 ('missing_input','plan',{'user_request':'帮我剪视频','proposal':'请先上传视频，并说明要保留的内容与目标时长。','assets':[]},['needs_information','discussion','uncertain'])]
for name,message,allowed in [('confirm','就按这个方案开始执行吧',['confirm']),('modify','可以，但是改成逆时针旋转90度',['revise']),('reject','先不要执行这个方案了，我不需要处理',['reject']),('question','如果执行这个方案，会发生什么？',['discussion','uncertain']),('quote','只解释“开始执行”这几个字，不要执行任何操作',['discussion','reject','uncertain']),('ambiguous','嗯……我再看看',['discussion','uncertain'])]:cases.append((name,'confirmation',{'message':message,'proposal':proposal},allowed))
result=[]
with tempfile.TemporaryDirectory() as folder:
 s=Store(Path(folder));agent_store.initialize(s)
 for name,purpose,state,allowed in cases:
  p=s.add_project('qa','semantic')['id'];t=agent_store.add_turn(s,p,'plan','test')
  try:value=classify(s,t,purpose,state)
  except SemanticUnavailable:value='uncertain'
  row={'case':name,'decision':value,'passed':value in allowed};result.append(row);print(json.dumps(row),flush=True)
Path('.cluster/campaigns/20261101-conversation/jev-real-evaluation.json').write_text(json.dumps({'passed':all(r['passed'] for r in result),'cases':result},indent=2)+'\n')
assert all(r['passed'] for r in result)
