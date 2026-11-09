"""Validate local edit plans before rendering. No model calls or network access."""
from __future__ import annotations

import copy
import json
import math
import re
import subprocess
from fractions import Fraction
from pathlib import Path


class PlanError(ValueError):
    pass


def fields(value, allowed, where):
    if not isinstance(value, dict):
        raise PlanError(f'{where} must be an object')
    unknown = set(value) - set(allowed.split())
    if unknown:
        raise PlanError(f'{where}: unknown fields: {", ".join(sorted(unknown))}')


def number(value, low, high, where, integer=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise PlanError(f'{where} must be a finite number')
    if not low <= value <= high or (integer and int(value) != value):
        raise PlanError(f'{where} must be {"an integer " if integer else ""}between {low} and {high}')
    return int(value) if integer else float(value)


def choice(value, allowed, where):
    if value not in allowed:
        raise PlanError(f'{where} must be one of {allowed}')
    return value


def flag(obj, key, default=False):
    value = obj.setdefault(key, default)
    if not isinstance(value, bool):
        raise PlanError(f'{key} must be true or false')
    return value


def local_file(value, root):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise PlanError('Expected a local file path')
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise PlanError(f'File must exist inside the current workspace: {value}')
    return path


def probe(path):
    run = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                          '-of', 'json', str(path)], capture_output=True, text=True, timeout=45)
    if run.returncode:
        raise PlanError(f'Cannot inspect media: {Path(path).name}')
    data = json.loads(run.stdout)
    video = next((s for s in data.get('streams', []) if s['codec_type'] == 'video'
                  and not s.get('disposition', {}).get('attached_pic')), None)
    audio = [s for s in data.get('streams', []) if s['codec_type'] == 'audio']
    duration = float(data.get('format', {}).get('duration') or (video or {}).get('duration') or 0)
    number(duration, .001, 86400, 'source duration')
    if not video and not audio:
        raise PlanError('Source has no usable audio or video stream')
    width, height = (video or {}).get('width', 0), (video or {}).get('height', 0)
    if video:
        sar = video.get('sample_aspect_ratio', '1:1').replace(':', '/')
        try:
            width = round(width * float(Fraction(sar)))
        except (ValueError, ZeroDivisionError):
            pass
        rotation = next((s.get('rotation', 0) for s in video.get('side_data_list', []) if 'rotation' in s), 0)
        if abs(float(rotation)) % 180 == 90:
            width, height = height, width
    return {'duration': duration, 'width': width, 'height': height, 'audio_tracks': len(audio),
            'video': bool(video), 'fps': (video or {}).get('avg_frame_rate', '0/0'),
            'video_stream': (video or {}).get('index', 0)}


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
        raise PlanError('Colors must use #RRGGBB')
    return value


def load_plan(path, workspace):
    root = Path(workspace).resolve()
    path = local_file(str(path), root)
    if path.stat().st_size > 2 * 1024**2:
        raise PlanError('Plan is too large')
    return validate(json.loads(path.read_text(encoding='utf-8')), root)


def validate(raw, workspace):
    """Return a normalized plan. Validation never creates output directories."""
    root = Path(workspace).resolve()
    p = copy.deepcopy(raw)
    fields(p, 'version sources transcripts clips output transition layout split_audio audio masks titles subtitles', 'plan')
    if p.get('version') != 1:
        raise PlanError('Expected plan version 1')
    sources = p.get('sources')
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 50:
        raise PlanError('sources must contain 1–50 local files')
    metadata = {}
    for key, value in sources.items():
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', key):
            raise PlanError('Source aliases may contain letters, digits, underscores and hyphens')
        sources[key] = str(local_file(value, root))
        metadata[key] = probe(sources[key])
    transcripts = p.setdefault('transcripts', {})
    if not isinstance(transcripts, dict) or set(transcripts) - set(sources):
        raise PlanError('transcripts must map known source aliases to cached JSON files')
    words = {}
    for key, value in transcripts.items():
        path = local_file(value, root)
        transcripts[key] = str(path)
        data = json.loads(path.read_text(encoding='utf-8'))
        words[key] = [w for w in data.get('words', []) if w.get('type') == 'word']
        for w in words[key]:
            number(w.get('start'), 0, metadata[key]['duration'] + 1, 'word.start')
            number(w.get('end'), w['start'], metadata[key]['duration'] + 1, 'word.end')
        if any(a['start'] > b['start'] for a, b in zip(words[key], words[key][1:])):
            raise PlanError('Transcript words must be in time order')
    out = p.setdefault('output', {})
    fields(out, 'width height fps crf audio_only format', 'output')
    audio_only = flag(out, 'audio_only')
    out['format'] = choice(out.get('format', 'wav' if audio_only else 'mp4'),
                           ('wav', 'mp3', 'm4a') if audio_only else ('mp4',), 'output.format')
    out['fps'] = number(out.get('fps', 30), 1, 60, 'output.fps', True)
    out['crf'] = number(out.get('crf', 20), 12, 35, 'output.crf', True)
    layout = choice(p.setdefault('layout', 'sequence'), ('sequence', 'horizontal', 'vertical', 'grid'), 'layout')
    if audio_only and layout != 'sequence':
        raise PlanError('Audio-only output requires sequence layout')
    clips = p.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= 100:
        raise PlanError('clips must contain 1–100 clips')
    if layout != 'sequence' and len(clips) != (4 if layout == 'grid' else 2):
        raise PlanError('horizontal/vertical need 2 clips; grid needs 4')
    first_key = clips[0].get('source') if isinstance(clips[0], dict) else None
    first = metadata.get(first_key) if isinstance(first_key, str) else None
    if not first:
        raise PlanError('Unknown clip source')
    for key, default in [('width', first['width'] or 1280), ('height', first['height'] or 720)]:
        out[key] = number(out.get(key, default + default % 2), 16, 4096, f'output.{key}', True)
        if out[key] % 2:
            raise PlanError('Output dimensions must be even')
    if (layout in ('horizontal', 'grid') and out['width'] % 4) or (layout in ('vertical', 'grid') and out['height'] % 4):
        raise PlanError('Split-screen cell dimensions must be even')
    fps = out['fps']
    for i, c in enumerate(clips):
        fields(c, 'source start end words padding audio_track speed reverse reverse_audio freeze_end repeat crop rotate flip fit mute volume grade', f'clips[{i}]')
        key = c.get('source')
        if not isinstance(key, str) or key not in metadata:
            raise PlanError(f'Unknown source in clip {i}')
        meta = metadata[key]
        if not audio_only and not meta['video']:
            raise PlanError('Video clips require a video source; use audio.source for music')
        if 'words' in c:
            selection = c['words']
            if 'start' in c or 'end' in c or not isinstance(selection, list) or len(selection) != 2 or not words.get(key):
                raise PlanError('words requires a cached transcript and replaces start/end')
            a, b = [number(v, 0, len(words[key])-1, 'word index', True) for v in selection]
            if b < a:
                raise PlanError('Last word index must follow first word index')
            pad = number(c.get('padding', .08), .03, .2, 'padding')
            c['start'] = max(0, words[key][a]['start'] - pad)
            c['end'] = min(meta['duration'], words[key][b]['end'] + pad)
            # Padding may reach a neighbouring word: stop in the available gap.
            if a: c['start'] = max(c['start'], words[key][a-1]['end'])
            if b+1 < len(words[key]): c['end'] = min(c['end'], words[key][b+1]['start'])
        c['start'] = number(c.get('start', 0), 0, meta['duration'], 'clip.start')
        c['end'] = number(c.get('end', meta['duration']), c['start'] + .04, meta['duration'] + .001, 'clip.end')
        for edge in (c['start'], c['end']):
            if any(w['start'] + .005 < edge < w['end'] - .005 for w in words.get(key, [])):
                raise PlanError(f'Clip {i} cuts inside a word; use words or move the edge into a gap')
        c['audio_track'] = number(c.get('audio_track', 0), 0, max(0, meta['audio_tracks']-1), 'audio_track', True)
        c['speed'] = number(c.get('speed', 1), .25, 4, 'speed')
        c['freeze_end'] = number(c.get('freeze_end', 0), 0, 30, 'freeze_end')
        c['repeat'] = number(c.get('repeat', 1), 1, 20, 'repeat', True)
        c['volume'] = number(c.get('volume', 1), 0, 4, 'clip.volume')
        for key in ('reverse', 'reverse_audio', 'mute'): flag(c, key)
        if c['reverse'] and c['end'] - c['start'] > 60:
            raise PlanError('Reverse buffers frames: limit each reversed clip to 60 seconds')
        if c['reverse_audio'] and not c['reverse']:
            raise PlanError('reverse_audio requires reverse')
        c['rotate'] = choice(c.get('rotate', 0), (0, 90, 180, 270), 'rotate')
        c['flip'] = choice(c.get('flip', 'none'), ('none', 'horizontal', 'vertical'), 'flip')
        c['fit'] = choice(c.get('fit', 'contain'), ('contain', 'cover', 'blur'), 'fit')
        c['grade'] = choice(c.get('grade', 'none'), ('none', 'subtle', 'neutral_punch', 'warm_cinematic'), 'grade')
        if 'crop' in c:
            crop = c['crop']
            if not isinstance(crop, list) or len(crop) != 4:
                raise PlanError('crop must be [x, y, width, height] in displayed source pixels')
            x, y, w, h = [number(v, 0 if n < 2 else 2, 16384, 'crop', True) for n, v in enumerate(crop)]
            if x+w > meta['width'] or y+h > meta['height'] or any(v % 2 for v in (x,y,w,h)):
                raise PlanError('Crop must use even coordinates and fit within the displayed source')
        if audio_only and any(k in c for k in ('crop',)):
            raise PlanError('Audio-only clips do not accept crop')
        c['_duration'] = (c['end'] - c['start']) / c['speed']
        if not audio_only: c['_duration'] = max(1, round(c['_duration'] * fps)) / fps
        c['_duration'] += round(c['freeze_end'] * fps) / fps
    transition = p.setdefault('transition', {'type':'cut', 'duration':0})
    fields(transition, 'type duration', 'transition')
    transition['type'] = choice(transition.get('type', 'cut'), ('cut','fade','pixel_dissolve','wipeleft','wiperight','slideleft','slideright','fadeblack'), 'transition.type')
    transition['duration'] = number(transition.get('duration', 0 if transition['type']=='cut' else .4), 0, 3, 'transition.duration')
    if transition['type']=='cut':
        if transition['duration'] != 0: raise PlanError('cut duration must be zero')
    else:
        if len(clips) < 2 or layout != 'sequence': raise PlanError('Transitions require a sequence of at least two clips')
        transition['duration'] = round(transition['duration'] * fps) / fps
        if not 0 < transition['duration'] <= min(c['_duration'] * c['repeat'] for c in clips)/2:
            raise PlanError('Transition must be positive and no longer than half the shortest clip')
    offset = 0
    for c in clips:
        c['_offset'] = offset if layout == 'sequence' else 0
        offset += c['_duration'] * c['repeat'] - transition['duration']
    total = offset + transition['duration'] if layout == 'sequence' else min(c['_duration'] * c['repeat'] for c in clips)
    p['_duration'] = number(total, .06, 3600, 'output duration')
    p['split_audio'] = number(p.get('split_audio', 0), 0, len(clips)-1, 'split_audio', True)
    audio = p.setdefault('audio', {})
    fields(audio, 'source mode audio_track start end offset volume duck loop denoise normalize highpass fade_in fade_out', 'audio')
    audio['mode'] = choice(audio.get('mode', 'mix'), ('mix', 'replace'), 'audio.mode')
    audio['volume'] = number(audio.get('volume', .2 if 'source' in audio else 1), 0, 4, 'audio.volume')
    audio['offset'] = number(audio.get('offset', 0), 0, max(0,total-.04), 'audio.offset')
    for key in ('duck', 'loop', 'denoise', 'normalize'): flag(audio, key)
    audio['highpass'] = number(audio.get('highpass', 0), 0, 300, 'audio.highpass')
    for key in ('fade_in','fade_out'): audio[key] = number(audio.get(key, .03), .03, total/2, f'audio.{key}')
    if 'source' in audio:
        meta = metadata.get(audio['source']) if isinstance(audio['source'], str) else None
        if not meta or not meta['audio_tracks']: raise PlanError('audio.source must contain an audio track')
        audio['audio_track'] = number(audio.get('audio_track', 0), 0, meta['audio_tracks']-1, 'audio.audio_track', True)
        audio['start'] = number(audio.get('start', 0), 0, meta['duration'], 'audio.start')
        audio['end'] = number(audio.get('end', meta['duration']), audio['start']+.04, meta['duration']+.001, 'audio.end')
        if audio['mode']=='replace' and audio['duck']: raise PlanError('Ducking requires mix mode')
    elif audio['duck'] or audio['loop'] or audio['mode']=='replace':
        raise PlanError('Replacement, ducking and looping require audio.source')
    for name in ('masks', 'titles'):
        items = p.setdefault(name, [])
        if not isinstance(items, list) or len(items) > 30: raise PlanError(f'{name} accepts at most 30 items')
        if audio_only and items: raise PlanError('Audio-only output cannot have masks or titles')
        for item in items:
            fields(item, 'x y width height start end mode' if name=='masks' else 'text size color background position start end', name)
            item['start'] = number(item.get('start', 0), 0, total, f'{name}.start')
            item['end'] = number(item.get('end', total), item['start']+.001, total+.001, f'{name}.end')
            if name=='masks':
                item['mode'] = choice(item.get('mode','pixelate'), ('pixelate','blur','black'), 'mask.mode')
                for key in ('x','y','width','height'): item[key] = number(item.get(key), 0 if key in ('x','y') else 4, 4096, f'mask.{key}', True)
                if item['x']+item['width'] > out['width'] or item['y']+item['height'] > out['height']:
                    raise PlanError('Mask lies outside output canvas')
                if any(item[key] % 2 for key in ('x','y','width','height')):
                    raise PlanError('Mask coordinates and dimensions must be even')
            else:
                if not isinstance(item.get('text'),str) or not 1 <= len(item['text']) <= 500: raise PlanError('title.text needs 1–500 characters')
                item['size'] = number(item.get('size', max(16,out['height']//20)), 10, 200, 'title.size', True)
                item['color'] = color(item.get('color','#FFFFFF'))
                item['background'] = flag(item, 'background', True)
                item['position'] = choice(item.get('position','bottom'), ('top','center','bottom'), 'title.position')
    if 'subtitles' in p:
        s = p['subtitles']
        fields(s, 'file from_transcripts clip_to_output words_per_line uppercase size color highlight position outline', 'subtitles')
        if audio_only: raise PlanError('Audio-only output cannot burn subtitles')
        from_transcripts = flag(s, 'from_transcripts')
        s['clip_to_output'] = flag(s, 'clip_to_output', True)
        s['uppercase'] = flag(s, 'uppercase')
        if ('file' in s) == from_transcripts: raise PlanError('Choose subtitles.file OR from_transcripts')
        if 'file' in s:
            s['file'] = str(local_file(s['file'], root))
            if Path(s['file']).suffix.lower() != '.srt': raise PlanError('Use an output-timeline UTF-8 SRT file')
        elif layout != 'sequence' or any(c['reverse'] or not words.get(c['source']) for c in clips):
            raise PlanError('Transcript subtitles require forward sequence clips with cached words for every source')
        s['words_per_line'] = number(s.get('words_per_line', 6), 1, 20, 'words_per_line', True)
        s['size'] = number(s.get('size', max(16,out['height']//24)), 10, 200, 'subtitle.size', True)
        s['outline'] = number(s.get('outline',2), 0, 8, 'subtitle.outline', True)
        s['color'] = color(s.get('color','#FFFFFF'))
        s['highlight'] = color(s['highlight']) if s.get('highlight') else None
        if s['highlight'] and not from_transcripts: raise PlanError('Word highlighting needs cached word timestamps')
        s['position'] = choice(s.get('position','bottom'), ('top','center','bottom'), 'subtitle.position')
    p['_metadata'], p['_words'], p['_root'] = metadata, words, str(root)
    return p
