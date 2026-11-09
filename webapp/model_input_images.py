"""Locate actual wire image blocks without treating tool arguments as images."""


def images(body):
    result=[]
    def visit(content):
        if isinstance(content,list):
            for value in content:visit(value)
        elif isinstance(content,dict):
            kind=content.get('type')
            if kind=='input_image' and isinstance(content.get('image_url'),str):
                result.append(content['image_url'])
            elif kind=='image':
                source=content.get('source',{})
                if source.get('type')=='base64':
                    result.append('data:'+source['media_type']+';base64,'+source['data'])
            elif kind=='image_url':
                value=content.get('image_url')
                result.append(value.get('url') if isinstance(value,dict) else value)
            elif kind in ('tool_result','message') or 'role' in content:
                visit(content.get('content'))
    visit(body.get('messages',[]));visit(body.get('input',[]))
    return result
