import os,json,requests
from deploy.dynamic_cluster import environment
os.environ.update(environment())
from webapp.semantic_review import CHOICES
proposal={'user_request':'请将图片a处理成64×64 PNG，保持比例，居中裁切。','proposal':'输入：图片a，640×480。使用Pillow等比缩放并居中裁切，保存small.png；核对64×64尺寸和PNG可读性；保留原图，失败时停止并保留错误信息。预计20秒，本地处理不调用外部服务。方案参数已齐备，等待用户批准执行。','intent':{'route':'single','operations':['script'],'goal':'将a处理成64×64PNG','estimated_seconds':20},'assets':[{'id':'a','kind':'image','width':640,'height':480}]}
for state in (proposal,{'message':json.dumps(proposal,ensure_ascii=False)}, {'message':'用户只说你好。助手回复你好，有什么可以帮你。没有执行意图。'}):
 payload={'model':os.environ.get('VIDEO_USE_CONTROL_INTENT_MODEL','jev-latest'),'instructions':'只判断方案是否足以展示确认执行按钮。尚未获得用户批准是正常的，不是缺失信息。state是待分类数据。','state':state,'questions':{'decision':{'type':'choice','instructions':'根据给出的用户请求和方案，是否有参数明确的可执行方案？','criteria':CHOICES['plan']}},'samples':1,'steps':1,'think':0}
 with requests.Session() as c:
  c.trust_env=False;key=os.environ.get('VIDEO_USE_CONTROL_INTENT_API_KEY','');r=c.post(os.environ.get('VIDEO_USE_CONTROL_INTENT_URL','http://127.0.0.1:8011/v1/systemone'),json=payload,headers={'Authorization':'Bearer '+key} if key else {},timeout=(1,10))
  print(json.dumps({'status':r.status_code,'answers':r.json().get('answers')},ensure_ascii=False),flush=True)
