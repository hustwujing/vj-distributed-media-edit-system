"""One declared source/track -> transcript rule for planning and execution."""


def resolve(action,parameters,inputs):
    if action=='edit':
        plan=parameters['edit_plan'];clips=plan.get('clips',[])
        needs_all=bool((plan.get('subtitles') or {}).get('from_transcripts'))
        required={(clip['asset'],clip.get('audio_track',0)) for clip in clips if needs_all or clip.get('words') is not None}
        pairs={(clip['asset'],clip.get('audio_track',0)) for clip in clips}
    elif action=='render':
        pairs={(clip['asset'],clip.get('audio_track',0)) for clip in parameters.get('segments',[])}
        required=pairs if parameters.get('subtitles')=='transcript' else set()
    elif action=='timeline':
        sources=[role for role in inputs if role!='transcript' and not role.startswith('transcript_')]
        pairs={(sources[0],parameters.get('audio_track',0))} if len(sources)==1 else set();required=set()
    else:return {}
    def role_for(value):
        if value in inputs:return value
        aliases=[role for role,ref in inputs.items() if (ref.get('asset') if isinstance(ref,dict) else getattr(ref,'asset',None))==value]
        if len(aliases)!=1:raise ValueError('媒体片段必须引用唯一的已声明输入角色：'+value)
        return aliases[0]
    pairs={(role_for(source),track) for source,track in pairs}
    required={(role_for(source),track) for source,track in required}
    bindings={}
    for source,track in sorted(pairs):
        names=[f'transcript_{source}_t{track}']
        if sum(role==source for role,_ in pairs)==1:names.append('transcript_'+source)
        if len(pairs)==1:names.append('transcript')
        selected=next((name for name in names if name in inputs),None)
        if selected:bindings[(source,track)]=selected
        elif (source,track) in required:
            raise ValueError('字幕或逐词剪辑缺少已声明的转写输入：'+ ' / '.join(names)+
                             '。多素材、多音轨必须分别绑定；不能依赖未声明的项目缓存。')
    return bindings


def materialized(action,parameters,inputs,identifiers):
    result={}
    for (source,track),transcript in resolve(action,parameters,inputs).items():
        source_id=identifiers[source];target=identifiers[transcript]
        tracks=result.setdefault(source_id,{})
        if str(track) in tracks and tracks[str(track)]!=target:raise ValueError('同一素材音轨绑定了不同的转写输入')
        tracks[str(track)]=target
    return result
