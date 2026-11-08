"""Bounded text and subtitle validation shared by admission and the sandbox."""
import re
from pathlib import Path

TEXT_SUFFIXES=frozenset({'.txt','.md','.json','.srt','.vtt','.ass','.ssa','.csv','.log','.svg'})
SUBTITLE_SUFFIXES=frozenset({'.srt','.vtt','.ass','.ssa'})
MAX_TEXT_BYTES=2*1024**2

class FormatError(ValueError):
    def __init__(self,code,message):
        super().__init__(message);self.code=code

def read_text(path,suffixes=TEXT_SUFFIXES):
    path=Path(path)
    if path.suffix.lower() not in suffixes:raise FormatError('unsupported_format','不支持此文本或字幕格式')
    return _read_utf8(path)


def _read_utf8(path):
    with path.open('rb') as stream:data=stream.read(MAX_TEXT_BYTES+1)
    if len(data)>MAX_TEXT_BYTES:raise FormatError('max_bytes','文本过大')
    try:text=data.decode('utf-8-sig')
    except UnicodeError as exc:raise FormatError('encoding','文本不是有效的 UTF-8 编码') from exc
    if any(ord(c)<32 and c not in '\n\r\t' for c in text):raise FormatError('encoding','文本包含无效控制字符')
    return text


def read_svg(path):
    """Bounded UTF-8 SVG source; declarations/entities are never interpreted."""
    from xml.etree import ElementTree
    # The caller selected SVG by its authorized logical reference. Object-store
    # cache paths are content-addressed and need not have any file extension.
    text=_read_utf8(Path(path))
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise FormatError('unsupported_format','SVG不支持声明或实体引用')
    try:root=ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:raise FormatError('unsupported_format','SVG XML结构无效') from exc
    if root.tag.rsplit('}',1)[-1]!='svg':raise FormatError('unsupported_format','文档根元素必须是SVG')
    if sum(1 for _ in root.iter())>2000:raise FormatError('max_bytes','SVG元素数量超过限制')
    return root

def _time(value,ass=False):
    pattern=r'(\d+):(\d{2}):(\d{2})\.(\d{2})' if ass else r'(?:(\d{2,}):)?(\d{2}):(\d{2})[.,](\d{3})'
    match=re.fullmatch(pattern,value.strip())
    if not match:raise FormatError('subtitle','字幕时间格式无效')
    h,m,s,f=match.groups();h=int(h or 0);m=int(m);s=int(s)
    if m>=60 or s>=60:raise FormatError('subtitle','字幕时间范围无效')
    return h*3600+m*60+s+int(f)/(100 if ass else 1000)

def validate_subtitle(path):
    text=read_text(path,SUBTITLE_SUFFIXES);suffix=Path(path).suffix.lower();cues=[]
    if suffix in ('.ass','.ssa'):
        section='';fields=None
        for raw in text.splitlines():
            line=raw.strip()
            if not line or line.startswith(';'):continue
            if line.startswith('[') and line.endswith(']'):
                section=line.casefold();continue
            if section!='[events]':continue
            key,sep,value=line.partition(':')
            if not sep:raise FormatError('subtitle','ASS 事件格式无效')
            if key.casefold()=='format':
                fields=[x.strip().casefold() for x in value.split(',')]
                if len(fields)!=len(set(fields)) or not {'start','end','text'}.issubset(fields) or fields[-1]!='text':
                    raise FormatError('subtitle','ASS 事件字段无效')
            elif key.casefold()=='dialogue':
                if not fields:raise FormatError('subtitle','ASS 缺少事件格式声明')
                values=value.split(',',len(fields)-1)
                if len(values)!=len(fields):raise FormatError('subtitle','ASS 事件字段数量不符')
                event=dict(zip(fields,values));cues.append((_time(event['start'],True),_time(event['end'],True)))
                if not event['text'].strip():raise FormatError('subtitle','字幕内容为空')
    else:
        normalized=text.replace('\r\n','\n').replace('\r','\n').strip()
        blocks=re.split(r'\n\s*\n',normalized)
        if suffix=='.vtt':
            if not blocks or not re.match(r'^WEBVTT(?:[ \t\n]|$)',blocks[0]):raise FormatError('subtitle','VTT 缺少 WEBVTT 头')
            blocks=blocks[1:]
        for block in blocks:
            lines=block.splitlines()
            if suffix=='.vtt' and re.match(r'^(NOTE(?:\s|$)|STYLE$|REGION$)',lines[0]):continue
            index=next((i for i,l in enumerate(lines[:2]) if '-->' in l),None)
            if index is None or index+1>=len(lines):raise FormatError('subtitle','字幕条目缺少时间或正文')
            timing=lines[index].split('-->')
            if len(timing)!=2:raise FormatError('subtitle','字幕时间格式无效')
            cues.append((_time(timing[0]),_time(timing[1].strip().split()[0])))
            if suffix=='.srt' and (index!=1 or not lines[0].isdigit()):raise FormatError('subtitle','SRT 序号无效')
    if not cues or any(a>=b for a,b in cues):raise FormatError('subtitle','字幕时间范围无效')
    # Overlapping subtitle events are valid, notably ASS layered effects.
    return {'format':suffix[1:],'cues':len(cues)}
