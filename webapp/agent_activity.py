"""Turn App Server events into observable work, never hidden reasoning."""
import re
import time
from pathlib import PurePosixPath


class ActivityTracker:
    def __init__(self, emit, assets=()):
        self.emit=emit
        self.names={a['filename']:a['name'] for a in assets}
        self.items={}
        self.last_output={}

    def display(self,text):
        text=str(text)
        for filename,name in self.names.items():text=text.replace(filename,name)
        return re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',text)

    def describe(self,item):
        kind=item.get('type')
        command=item.get('command','')
        lower=command.lower()
        category,title='command','运行处理命令'
        if kind=='imageView':
            return 'inspect','检查画面',self.display(PurePosixPath(item.get('path','')).name)
        if kind=='fileChange':
            files=[PurePosixPath(change.get('path','')).name for change in item.get('changes',[])]
            return 'write','更新剪辑文件','、'.join(files[:5])
        if kind in ('mcpToolCall','dynamicToolCall'):
            return 'tool','调用工具',str(item.get('tool',''))
        if 'workflow.py' in lower:
            category='inspect'
            title='检查脚本与运行环境' if 'preflight' in lower else '校验工作流方案' if 'validate' in lower else '提交验收结论' if 'decision' in lower else '保存执行方案' if 'intent' in lower else '提交后台处理' if 'submit' in lower else '读取工作流状态'
        elif 'canvas_edit.py' in lower:
            category,title=('write','应用图层修改并保存版本') if '--apply' in lower else ('inspect','检查图层修改与生成前后预览')
        elif any(term in lower for term in ('write_text','write_bytes','apply_patch','cat >','cat <<')):
            category,title='write','编写剪辑脚本'
        elif 'transcribe' in lower:
            category,title='transcribe','转写与整理对白'
        elif 'ffprobe' in lower:
            category,title='inspect','检查视频信息'
        elif 'edit_media.py' in lower:
            category,title=('render','执行音视频剪辑计划') if re.search(r'\brender\b',lower) else ('inspect','检查剪辑能力与计划')
        elif 'ffmpeg' in lower and any(x in lower for x in ('frames:v','tile=','image2pipe','fps=')):
            category,title='inspect','提取检查画面'
        elif 'ffmpeg' in lower or re.search(r'python\S*\s+[^\n ]*render[^\n ]*\.py',lower):
            category,title='render','渲染视频'
        elif 'timeline_view' in lower:
            category,title='inspect','生成画面与波形预览'
        elif re.search(r'\b(cat|sed|head|rg|ls)\b',lower) or any(x in lower for x in ('read_text','json.load')):
            category,title='read','读取素材与项目信息'
        files=re.findall(r'(?:[A-Za-z0-9_\-]+\.(?:mp4|mov|webm|py|json|srt|png|jpg|md))',command)
        summary='、'.join(dict.fromkeys(self.display(name) for name in files))[:220]
        return category,title,summary

    def handle(self,method,params):
        if method=='turn/plan/updated':
            self.emit('plan',{'steps':[{'step':str(s.get('step',''))[:500],'status':s.get('status','pending')} for s in params.get('plan',[])[:20]]})
            return
        if method in ('item/commandExecution/outputDelta','item/fileChange/outputDelta'):
            item_id=params.get('itemId')
            item=self.items.get(item_id)
            if not item:return
            item['output']=(item.get('output','')+self.display(params.get('delta','')))[-4000:]
            now=time.monotonic()
            if now-self.last_output.get(item_id,0)>.4:
                self.last_output[item_id]=now
                self.emit('tool_output',{'id':item_id,'output':item['output']})
            return
        if method not in ('item/started','item/completed'):return
        item=params.get('item',{});kind=item.get('type');item_id=item.get('id')
        if kind=='agentMessage' and method=='item/completed':
            if item.get('phase')=='commentary' and item.get('text'):
                self.emit('note',{'id':item_id,'text':str(item['text'])[:5000]})
            return
        if kind not in ('commandExecution','fileChange','imageView','mcpToolCall','dynamicToolCall'):return
        category,title,detail=self.describe(item)
        record=self.items.get(item_id,{})
        record.update({'id':item_id,'tool_type':kind,'category':category,'title':title,'detail':detail,
                       'command':self.display(item.get('command',''))[:2400],
                       'status':'running' if method=='item/started' else 'completed'})
        if method=='item/started':record['started']=params.get('startedAtMs',int(time.time()*1000))/1000
        else:
            record['completed']=params.get('completedAtMs',int(time.time()*1000))/1000
            record['exit_code']=item.get('exitCode')
            if item.get('status') in ('failed','declined') or item.get('exitCode') not in (None,0):record['status']='failed'
            if item.get('aggregatedOutput'):record['output']=self.display(item['aggregatedOutput'])[-4000:]
            if item.get('durationMs') is not None:record['duration_ms']=item['durationMs']
        self.items[item_id]=record
        self.emit('tool',dict(record))
