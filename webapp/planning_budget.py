"""Generic plan-only limits; workflow review and confirmed execution are separate."""
import os

class PlanningLimit(RuntimeError):pass


def limits(profile=None):
    from .agent_profiles import current
    budget=(profile or current()).budget
    return {name:budget['plan_'+name] for name in ('seconds','models','tools','inspections')}


def guidance():
    return {'preview_variants':max(1,min(20,int(os.environ.get('VIDEO_USE_PLAN_PREVIEW_VARIANTS','2')))),
            'correction_rounds':max(0,min(10,int(os.environ.get('VIDEO_USE_PLAN_CORRECTIONS','1'))))}


def snapshot(store,task):
    row=store.query('SELECT mode,status,updated FROM agent_turns WHERE id=?',(task,))
    if not row or row[0]['mode']!='plan':return None
    counts=store.query("SELECT category,operation,COUNT(*) n FROM usage_calls WHERE task_id=? GROUP BY category,operation",(task,))
    started=store.query("SELECT MIN(started) started FROM execution_attempts WHERE run_id=? AND runtime<>''",(task,))[0]['started']
    used={'seconds':max(0,(store.now() if row[0]['status'] in ('queued','running','cancelling') else row[0]['updated'])-started) if started else 0,'models':sum(r['n'] for r in counts if r['category']=='llm' and not r['operation'].startswith('review:')),
          'tools':sum(r['n'] for r in counts if r['category']=='tool'),'inspections':sum(r['n'] for r in counts if r['category']=='tool' and r['operation']=='imageView')}
    from .agent_profiles import for_task
    cap=limits(for_task(store,task));return {'limits':cap,'guidance':guidance(),'used':used,'exhausted':[k for k in cap if used[k]>=cap[k]]}


def check(lease,admission=True):
    if not lease:return
    state=snapshot(lease.store,lease.run_id)
    blocked=[k for k in state['exhausted'] if admission or k=='seconds' or state['used'][k]>state['limits'][k]] if state else []
    if blocked:
        from .telemetry import emit
        emit('planning.budget_exhausted',task_id=lease.run_id,reason=blocked[0])
        raise PlanningLimit('本轮规划已达到预览与分析预算，已停止继续自检；已有公开说明和可导出的预览会保留。正式任务尚未执行，请缩小规划范围或明确仍需解决的问题。')


def instruction():
    c=limits();g=guidance()
    return ('规划预算（服务端强制限制）：'+str(c)+f"。最多制作{g['preview_variants']}版必要的静态预览，仅对明确发现的问题追加{g['correction_rounds']}次修正；素材和参数足以决策后立即给出方案并登记意图。"
            '不要反复读取同一图或仅为提高把握继续自检。预留最后两次模型请求用于收束回复和登记意图。'
            '预算不足时明确已完成、未解决的问题，不宣称正式交付。已确认多阶段任务中的受支持操作必须提交为 workflow 节点；直接工具仅用于独立短小单次处理。')


def reserve_model(lease):
    rows=lease.store.query('SELECT mode FROM agent_turns WHERE id=?',(lease.run_id,)) if lease else []
    if rows and rows[0]['mode']=='plan':
        lease.consume('planning_model',limits()['models'])
