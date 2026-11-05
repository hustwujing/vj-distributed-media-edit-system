"""Output-time SRT/ASS captions for edit_media plans; no transcription calls."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from edit_plan import PlanError


def caption_font():
    """Select an installed CJK font, not fontconfig's silent Latin fallback."""
    listing=subprocess.check_output(['fc-list',':lang=zh','-f','%{file}\t%{family}\n'],text=True)
    fonts=[line.split('\t',1) for line in listing.splitlines() if '\t' in line]
    for preferred in ('Noto Sans CJK SC','WenQuanYi Zen Hei','Source Han Sans'):
        for path,family in fonts:
            if preferred in family:return path,family.split(',')[0]
    if fonts:return fonts[0][0],fonts[0][1].split(',')[0]
    raise PlanError('Install a CJK font such as Noto Sans CJK or WenQuanYi before rendering text')


def clock(t, ass=False):
    scale = 100 if ass else 1000
    value = max(0, round(t * scale))
    seconds, fraction = divmod(value, scale)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f'{hour}:{minute:02}:{second:02}.{fraction:02}' if ass else f'{hour:02}:{minute:02}:{second:02},{fraction:03}'


def parse_srt(path):
    return parse_srt_text(Path(path).read_text(encoding='utf-8-sig'))


def parse_srt_text(text):
    text = text.replace('\r\n','\n').strip()
    cues = []
    pattern = r'(\d+):(\d{2}):(\d{2})[,.](\d{3})'
    def seconds(groups):
        h,m,s,ms = map(int, groups)
        if m >= 60 or s >= 60: raise PlanError('Invalid SRT timestamp')
        return h*3600+m*60+s+ms/1000
    for block in re.split(r'\n\s*\n', text):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit(): lines.pop(0)
        match = re.fullmatch(pattern + r'\s*-->\s*' + pattern, lines[0].strip()) if lines else None
        if not match or len(lines)<2: raise PlanError('Invalid UTF-8 SRT block')
        a,b = seconds(match.groups()[:4]),seconds(match.groups()[4:])
        if b<=a: raise PlanError('SRT end must follow start')
        cues.append({'start':a,'end':b,'text':'\n'.join(lines[1:]),'words':[]})
    return cues


def join_words(words):
    result = ''
    for word in words:
        text = word['text'].strip()
        if not text: continue
        cjk = lambda c: '\u2e80' <= c <= '\u9fff'
        space = bool(result) and not cjk(result[-1]) and not cjk(text[0]) and text[0] not in '.,!?;:，。！？；：'
        result += (' ' if space else '') + text
    return result


def cues_for_plan(p):
    settings = p['subtitles']
    if 'file' in settings:
        cues = parse_srt(settings['file'])
        if settings.get('clip_to_output',True):
            cues=[{**cue,'end':min(cue['end'],p['_duration'])} for cue in cues if cue['start']<p['_duration']]
            cues=[cue for cue in cues if cue['end']>cue['start']]
            if not cues:raise PlanError('SRT has no cues within the rendered duration')
        elif any(c['end'] > p['_duration']+.05 for c in cues):
            raise PlanError('SRT must use output times and end within the rendered duration')
        return cues
    cues = []
    for c in p['clips']:
        for repeat in range(c['repeat']):
            offset = c['_offset'] + repeat*c['_duration']
            words = []
            for w in p['_words'][c['source']]:
                if w['start'] >= c['start']-.005 and w['end'] <= c['end']+.005:
                    a = offset + max(0,w['start']-c['start'])/c['speed']
                    b = min(p['_duration'],offset+(w['end']-c['start'])/c['speed'])
                    if b>a: words.append({'start':a,'end':b,'text':str(w.get('text','')),'speaker':w.get('speaker_id')})
            group=[]
            for w in words:
                if group and (len(group)>=settings['words_per_line'] or w['start']-group[-1]['end']>.5 or w['speaker']!=group[-1]['speaker']):
                    cues.append({'start':group[0]['start'],'end':group[-1]['end'],'text':join_words(group),'words':group});group=[]
                group.append(w)
            if group: cues.append({'start':group[0]['start'],'end':group[-1]['end'],'text':join_words(group),'words':group})
    return sorted(cues,key=lambda c:c['start'])


def ass_escape(text):
    # Prevent supplied subtitle text from injecting ASS override commands.
    return text.replace('\\','＼').replace('{','｛').replace('}','｝').replace('\n',r'\N')


def ass_color(value):
    return '&H00' + value[5:7] + value[3:5] + value[1:3]


def write_captions(p, directory):
    s=p['subtitles'];cues=cues_for_plan(p)
    srt=directory/'captions.srt';ass=directory/'captions.ass'
    srt.write_text('\n\n'.join(f'{i+1}\n{clock(c["start"])} --> {clock(c["end"])}\n{c["text"]}' for i,c in enumerate(cues))+'\n',encoding='utf-8')
    width,height=p['output']['width'],p['output']['height']
    _,family=caption_font()
    header=f'''[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{family},{s['size']},{ass_color(s['highlight'] or s['color'])},{ass_color(s['color'])},&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{s['outline']},0,{dict(bottom=2,center=5,top=8)[s['position']]},{width//20},{width//20},{max(12,height//16)},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    lines=[]
    for cue in cues:
        text=ass_escape(cue['text'])
        if s['highlight'] and cue['words']:
            parts=[];previous_end=cue['start'];previous_text=''
            for w in cue['words']:
                gap=max(0,round((w['start']-cue['start'])*100)-round((previous_end-cue['start'])*100))
                if gap:parts.append('{\\k'+str(gap)+'}\\h')
                combined=join_words([{'text':previous_text},{'text':w['text']}]) if previous_text else w['text']
                prefix=' ' if previous_text and combined.startswith(previous_text+' ') else ''
                length=max(1,round((w['end']-cue['start'])*100)-round((w['start']-cue['start'])*100))
                parts.append('{\\kf'+str(length)+'}'+prefix+ass_escape(w['text']))
                previous_end=w['end'];previous_text=w['text']
            text=''.join(parts)
        lines.append(f'Dialogue: 0,{clock(cue["start"],True)},{clock(cue["end"],True)},Default,,0,0,0,,{text}')
    ass.write_text(header+'\n'.join(lines)+'\n',encoding='utf-8')
    return srt,ass
