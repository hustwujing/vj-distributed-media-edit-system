"""Prompt authoring guidance derived from the selected operation, not user keywords."""

def guidance(operation):
    if operation not in ('image.generate_from_text','video.generate_from_text','video.generate_from_image','image.edit','image.erase','image.edit_region','image.decompose'):
        return None
    result={'sections':['主体与场景','需要发生的动作或变化','构图与风格（仅用户要求或已确认选项）','参考输入职责','必须保留项与禁止变化项'],
            'rules':['用户原始要求不可因整理提示而丢失；不擅自增加主体、镜头、风格或收费操作。',
                     '尺寸、时长、数量、seed等使用工具参数，不仅写在自然语言中。',
                     '提示要求不是能力保证；按 capability constraints 规划并验收实际产物。'],
            'reference':'references/generation-prompts.md'}
    if operation=='video.generate_from_image':
        result['reference_role']='source 是实际首帧，核对人物/物体身份、边界和目标尺寸；动作与镜头描述分开，不要求模型从参考图恢复画外内容。'
    elif operation in ('image.edit','image.erase','image.edit_region'):
        result['reference_role']='区分原图、选区指示图和风格参考；有蒙版时只改变选区，用户明确要求保留的主体、位置、形状不可擅改。'
    elif operation=='image.decompose':result['reference_role']='原图用于分层重建；层数由参数控制，不额外重绘原图。'
    else:result['reference_role']='纯文本生成不虚构已有参考图。'
    return result
