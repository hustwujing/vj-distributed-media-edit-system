"""Small local-script API. No network, credentials or runtime-selection interface."""
import json
import os
from pathlib import Path


def inputs():return json.loads(os.environ.get('VIDEO_USE_STEP_INPUTS','{}'))


def input_file(role,index=0):
    values=inputs().get(role)
    if not values or not 0<=index<len(values):raise ValueError('缺少步骤输入: '+role)
    return Path(values[index])


def output_file(name):
    if not isinstance(name,str) or Path(name).name!=name or name in ('.','..'):raise ValueError('输出只能使用文件名')
    return Path(os.environ['VIDEO_USE_STEP_OUTPUT'])/name


def progress(text,current=None,total=None):
    if not isinstance(text,str) or not text.strip():raise ValueError('进度说明不能为空')
    if current is not None or total is not None:
        if not isinstance(current,(int,float)) or not isinstance(total,(int,float)) or not 0<=current<=total or total<=0:raise ValueError('进度必须来自有效工作量')
    print(json.dumps({'type':'step_progress','text':text[:500],'current':current,'total':total},ensure_ascii=False),flush=True)


def report(name,passed,reason,**observations):
    if not isinstance(passed,bool):raise ValueError('passed 必须是布尔值')
    output_file(name).write_text(json.dumps({'passed':passed,'reason':reason,'observations':observations},ensure_ascii=False,indent=2))
