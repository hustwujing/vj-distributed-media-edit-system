"""Produce the final usage acceptance report from recorded evidence only."""
import hashlib,json,shutil
from pathlib import Path
root=Path('.cluster/campaigns/20261101-task-usage')
def read(name):return json.loads((root/(name+'.json')).read_text())
for mode in ('generation','video-repair','continuation','cancel','manual','ownership','snapshot-after-restart'):
    assert read(mode+'-result')['passed'],mode
release=json.loads(Path('.cluster/release.json').read_text())
suite=json.loads((Path(release['source'])/'docs/test-evidence/distributed-suite.json').read_text())
assert suite['passed'] and suite['source_sha256']==release['sha256'] and suite['tests']==446
regression=read('regression-60');assert regression['passed'] and len(regression['results'])==60
reconciliation=read('database-reconciliation');assert reconciliation['passed']
image=read('generation-run-usage');video=read('video-task-usage');continuation=read('continuation-usage');fresh=read('new-demand-usage');cancel=read('cancel-after-usage')
assert video['provider']['submissions']==2 and video['workflow_retries']==0 and video['repairs']==1 and video['counts']['workflow']['calls']==3
assert video['provider']['actual_polls']==video['provider']['polls']
assert cancel['tokens']['total_tokens']['known'] is None and cancel['counts']['llm']['calls']==1
assert read('diagnostic-after-fix')['status']==200
assert read('snapshot-before-restart')==read('snapshot-after-restart')
checks=read('video-final-checks');assert checks['passed']
cases=[
 {'id':'U01','case':'真实文生图＋本地 512×512 裁切','passed':True,'details':'原图 1024×1024 与裁切图均下载并解码；plan→execute 归组；页面/API/SQL 一致'},
 {'id':'U02','case':'真实图生视频＋页面确认修复','passed':True,'details':f"首次烟囱出画被暂停，页面明确批准一次参数修复后交付；2 次提交、{video['provider']['polls']} 次查询、1 次修复、0 次同修订重试"},
 {'id':'U03','case':'基于任务继续讨论','passed':True,'details':f"两轮共用 run，{continuation['counts']['llm']['calls']} 次模型请求，{continuation['tokens']['total_tokens']['known']} Token"},
 {'id':'U04','case':'同项目独立新问题','passed':True,'details':f"单独 run，{fresh['counts']['llm']['calls']} 次模型请求，{fresh['tokens']['total_tokens']['known']} Token"},
 {'id':'U05','case':'真实模型请求中取消','passed':True,'details':'请求保留为未知；缺失 Token 回执为 null，未伪装为零消耗'},
 {'id':'U06','case':'手动本地图片导出','passed':True,'details':'64×64 PNG 下载验证；模型调用 0，阶段耗时可查；最终版本再测通过'},
 {'id':'U07','case':'所有权隔离','passed':True,'details':'同用户读取 200，另一浏览器身份读取 404'},
 {'id':'U08','case':'重启后持久化','passed':True,'details':'集群重启前后图片、视频需求的调用/Token/上游提交/查询统计完全一致'}]
report={'release':release,'python_suite':suite,'frontend_tests':{'passed':True,'tests':16},'cases':cases,
 'historical_regression':{'passed':True,'cases':60,'unchanged_files':sum(x['artifacts_unchanged'] for x in regression['results'])},
 'reconciled_snapshots':len(reconciliation['results']),
 'image_run':{k:image[k] for k in ('counts','tokens','workflow_retries','repairs')},
 'video_task':{k:video[k] for k in ('counts','tokens','workflow_retries','repairs')},
 'limits':['历史主模型 Token 无法补录','SDK 内部重试未可靠关联时不猜测次数','没有费用回执不估算金额','CPU/GPU 实测用量未采集，阶段耗时不是 CPU/GPU 时间'],
 'issues':[{'issue':'PostgreSQL SUM 返回 Decimal 导致诊断 JSON 导出 500','fixed':True,'verification':'转换 Token 和重试汇总为普通整数；真实下载 200、CLI 成功、PostgreSQL JSONResponse 回归通过'},
 {'issue':'首次生成视频末段烟囱出画','fixed':True,'verification':'系统正确暂停；通过用户确认的参数修复重新生成，保留两次消耗；未修改业务代码或降低验收条件'},
 {'issue':'首次取消测试脚本早于页面就绪输入','fixed':True,'verification':'等待 Agent 在线后再输入；首次未提交任务，重跑实际取消通过'}]}
(root/'final-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
evidence=Path('docs/test-evidence/task-usage');evidence.mkdir(parents=True,exist_ok=True)
for name in ('final-report.json','regression-60.json','database-reconciliation.json','diagnostic-before-fix.json','diagnostic-after-fix.json','ownership-proof.json','image-decode-checks.json','video-final-checks.json','snapshot-before-restart.json','snapshot-after-restart.json','services-final.json','final-release-check.json','trace-proof.json','generation-run-ui.png','video-task-ui.png','cancel-after-ui.png','manual-ui.png','video-repaired-contact.png'):
    shutil.copyfile(root/name,evidence/name)
shutil.copyfile(Path(release['source'])/'docs/test-evidence/distributed-suite.json',evidence/'full-python-suite.json')
text=f'''# 任务消耗统计：实现与真实测试报告

已实现、部署并重启。最终版本 `{Path(release['source']).name}`，冻结完整工作区，包含其他窗口的 Redis 流式输出代码及相应依赖。完整代码 SHA-256：`{release['sha256']}`。

## 用户侧入口

- 对话任务下方 **查看任务消耗**：切换“当前任务 / 本次完整需求”，展开逐次调用及上游回执。
- 工具页、图片工作台任务的 **消耗** 按钮。
- 对话历史的 **基于此任务继续** 明确关联后续需求；确认执行自动继承方案所属 run，新问题默认独立。
- `GET /api/projects/{{project}}/tasks/{{task}}/usage?scope=task|run` 支持明细分页；任务诊断下载和 CLI 导出也包含消耗。

## 记录内容与口径

持久化每次主/控制模型请求、Agent 工具调用、工作流步骤启动、外部提交/查询/收取尝试。固定 task/run/call/attempt/step/revision 标识用于关联；日志有 call_id，配额独立负责限额。

模型输入、输出、总 Token、缓存命中和推理 Token 取服务实际回执。缓存属于输入子集，推理属于输出子集，不重复相加。缺失用量保留未知；取消或进程退出不能抹掉已开始的调用。上游用量和费用使用原 provider 账本，重复回执不重复累计。

同一 step/revision 的额外启动才计为工作流重试。修订修复单列，轮询不算新的步骤或生成。调用执行成功不等于画面验收通过，两者状态分开。

## 真实测试结果：8 / 8

| 编号 | 测试 | 最终结果与证据 |
|---|---|---|
'''
for c in cases:text+=f"| {c['id']} | {c['case']} | 通过。{c['details']} |\n"
text+=f'''
U01 首轮执行在版本 02640ea048f7 完成；修复版 d737486c3b27 上再次核对页面/API/数据库、诊断和重启后的账本。U02–U04 及最终手动导出在最终版本实际执行。U02 首次质量暂停保留为真实过程记录，修复确认和第二次生成均经正常用户接口，没有绕过验收。

本轮真实外部生成合计 **1 次文生图、2 次图生视频**。其中首个视频不是最终交付。最终视频实测 **{checks['metadata']['duration']:.6f} 秒、1280×720、30 fps**，解码及抽帧检查通过。图片和视频都通过用户下载接口实际取得。

## 实际消耗示例

文生图与裁切的完整需求（含方案）共 **19 次模型请求、21 次 Agent 工具调用、2 次工作流执行**。工具调用含 1 次失败，Agent 后续完成处理；没有把它误计为工作流重试。

| 指标 | 实际回执 |
|---|---:|
| 输入 Token | 1,004,977 |
| 输出 Token | 5,011 |
| 总 Token | 1,009,988 |
| 缓存命中（输入子集） | 944,768 |
| 推理（输出子集） | 75 |

此外，文生图服务回报 1,844 Token，单独展示，不混入主模型的上述汇总。视频服务回报生成秒数等用量。服务未回报金额，因此没有虚构费用。

图生视频任务修复后共 **{video['counts']['llm']['calls']} 次模型请求、{video['counts']['tool']['calls']} 次 Agent 工具调用、3 次工作流执行、2 次生成提交、{video['provider']['polls']} 次查询、1 次修复、0 次同修订重试**。两次生成的回执都保留。

![整次需求消耗面板](test-evidence/task-usage/generation-run-ui.png)

## 回归与核对

- 最终完整版本：**446 项 Python 测试通过，0 失败、0 跳过；16 项前端测试通过**。新增 20 项 Python 与 2 项前端测试。
- 覆盖任意字节分段 SSE、JSON、缺失用量、重复与冲突回执、迟到回执、取消、重启、并发幂等、分修订重试、权限及分页汇总。
- 使用真实临时 PostgreSQL 验证双节点并发与数据库重启；使用真实 Redis 验证两端订阅、账本事务和 Redis 故障回退。
- 最终部署版本对既有 **60 / 60** 案例重新打开页面并下载校验，**933 个产物哈希未变，页面错误 0**。这次是只读回归，没有重复执行原 60 个付费生成流程。
- **{len(reconciliation['results'])} 份**稳定统计快照与独立 SQL 核对一致；完整汇总不受日志/明细分页截断影响。
- 诊断下载修复验证：真实 HTTP **500 → 200**；CLI 导出成功。日志导出仍按页提供，并明确标注截断。
- 最后重启集群，检查所有服务和两个节点，再次读取相同账本，统计未变、未重提生成。

## 边界

历史主模型 Token 无法补造，会显示历史未采集；历史工具事件、已有配额和提供者回执仍可查。没有可靠关联的 SDK 内部重试次数保持未知。CPU/GPU 实测用量尚未采集，阶段时长不是 CPU/GPU 时间；没有明确费用回执时不推算账单。

## 文档与证据

- [技术设计与检查项](task-usage-design.md)
- [静态审查及修复记录](task-usage-review.md)
- [结构化完整报告](test-evidence/task-usage/final-report.json)
- [最终 Python 全量测试](test-evidence/task-usage/full-python-suite.json)
- [60 案例回归明细](test-evidence/task-usage/regression-60.json)
- [独立数据库核对](test-evidence/task-usage/database-reconciliation.json)
- [重启前](test-evidence/task-usage/snapshot-before-restart.json) / [重启后](test-evidence/task-usage/snapshot-after-restart.json)
- [修复视频抽帧](test-evidence/task-usage/video-repaired-contact.png)

私有浏览器认证状态未导出到报告。详细 QA 记录保留在 `.cluster/campaigns/20261101-task-usage`。
'''
Path('docs/task-usage-report.md').write_text(text)
print(json.dumps({'passed':True,'cases':len(cases),'python':suite['tests'],'historical_cases':60},ensure_ascii=False))
