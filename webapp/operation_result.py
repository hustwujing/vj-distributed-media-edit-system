"""Untrusted worker result manifest; paths and roles are validated by the host."""
from pathlib import PurePosixPath
from pydantic import BaseModel,ConfigDict,Field,model_validator


class Output(BaseModel):
    model_config=ConfigDict(extra='forbid')
    role:str=Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    path:str=Field(min_length=1,max_length=100)
    @model_validator(mode='after')
    def safe(self):
        if PurePosixPath(self.path).name!=self.path or self.path in ('.','..') or '\\' in self.path or '\x00' in self.path:
            raise ValueError('Output must be a basename')
        return self


class Result(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:int=Field(default=1,ge=1,le=1)
    outputs:list[Output]=Field(min_length=1,max_length=40)
    @model_validator(mode='after')
    def unique(self):
        paths=[x.path for x in self.outputs]
        roles=[x.role for x in self.outputs if x.role!='auxiliary']
        if len(set(paths))!=len(paths) or len(set(roles))!=len(roles):raise ValueError('Duplicate output path or role')
        return self

    @classmethod
    def media(cls,action,names):
        known={'edit_plan.json':'plan','master.srt':'subtitle','timeline.png':'preview',
               'analysis.json':'report','quality-review.json':'report','takes_packed.md':'text',
               'final.report.json':'report'}
        outputs=[]
        for name in names:
            role=known.get(name,'auxiliary')
            if name.startswith('final.'):
                suffix=PurePosixPath(name).suffix.lower()
                if suffix in ('.mp4','.mov','.webm','.mkv','.gif'):role='video'
                elif suffix in ('.wav','.mp3','.m4a','.flac','.aac','.ogg'):role='audio'
            outputs.append(Output(role=role,path=name))
        return cls(outputs=outputs)


class ProducedFiles(list):
    def __init__(self,paths,roles):
        super().__init__(paths)
        self.roles=roles


def validate_contract(operation,report,selected=None):
    """An Agent's acceptance rules cannot waive the registered output modality."""
    if operation is None:return report
    records={item['name']:item for item in report.get('files',[])}
    def matches(role,item):
        if role=='video':return bool(item.get('video'))
        if role=='audio':return bool(item.get('audio'))
        if role in ('primary_image','foreground','mask','preview'):return item.get('kind')=='image'
        if role in ('transcript','report','plan'):return item.get('kind')=='json'
        return True
    failures=[]
    for role,path in (selected or {}).items():
        if role not in operation.outputs or path not in records or not matches(role,records[path]):
            failures.append('输出角色与实际文件不符: '+role)
    # Some capabilities have optional outputs; media.edit may produce audio OR
    # video. Validate its alternatives rather than demanding both streams.
    primary=operation.outputs[0] if operation.outputs else None
    if operation.id=='media.edit':
        present=any(item.get('audio') or item.get('video') for item in records.values())
    else:present=bool(records) and (primary is None or any(matches(primary,item) for item in records.values()))
    if not present:failures.append('缺少 operation 声明类型的主要产物')
    for reason in failures:
        report['checks'].append({'passed':False,'code':'output_contract','reason':reason})
    if failures:report['technical_valid']=False;report['passed']=False
    return report
