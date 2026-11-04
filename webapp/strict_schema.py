"""Lossless, bounded JSON-Schema transport for native structured tool arguments.

Dynamic maps use key/value entries; arbitrary JSON objects use an entries object.
This representation is explicit in the tool schema, never JSON inside a string.
Domain validation remains authoritative after decoding.
"""
from copy import deepcopy
from jsonschema import Draft202012Validator,ValidationError


def obj(properties, required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,
            'additionalProperties':False}


def _json_definition():
    ref={'$ref':'#/$defs/TransportJSON'}
    return {'anyOf':[{'type':kind} for kind in ('null','boolean','number','string')]+[
        {'type':'array','items':ref},obj({'entries':{'type':'array','items':obj({'key':{'type':'string'},'value':ref})}})]}


def _any(schema):
    return not any(key in schema for key in ('type','$ref','anyOf','oneOf','const','enum'))


def _map(schema):
    return schema.get('type')=='object' and 'properties' not in schema and schema.get('additionalProperties') is not False


def convert(schema):
    schema=deepcopy(schema)
    if _any(schema):return {'$ref':'#/$defs/TransportJSON'}
    if _map(schema):
        value=schema.get('additionalProperties',{})
        return {'type':'array','description':'Map entries. Keys must be unique.',
                'items':obj({'key':{'type':'string',**schema.get('propertyNames',{})},'value':convert(value if isinstance(value,dict) else {})})}
    result={key:value for key,value in schema.items() if key not in ('title','default','examples','discriminator','$defs')}
    if 'const' in result:result['enum']=[result.pop('const')]
    for union in ('anyOf','oneOf'):
        if union in result:
            result['anyOf']=[convert(item) for item in result.pop(union)];break
    if result.get('type')=='object':
        required=set(schema.get('required',[]));properties={}
        for key,item in schema.get('properties',{}).items():
            value=convert(item)
            if key not in required:value={'anyOf':[value,{'type':'null'}]}
            properties[key]=value
        result.update(properties=properties,required=list(properties),additionalProperties=False)
    if result.get('type')=='array':
        if 'prefixItems' in schema:
            items=[convert(item) for item in schema['prefixItems']]
            result.pop('prefixItems',None)
            result['items']=items[0] if all(item==items[0] for item in items) else {'anyOf':items}
        else:result['items']=convert(schema.get('items',{}))
    return result


def schema_for(source):
    result=convert(source)
    result['$defs']={key:convert(value) for key,value in source.get('$defs',{}).items()}
    result['$defs']['TransportJSON']=_json_definition()
    needed=set()
    def collect(node):
        if isinstance(node,dict):
            ref=node.get('$ref','')
            if ref.startswith('#/$defs/') and ref[8:] not in needed:
                name=ref[8:];needed.add(name);collect(result['$defs'][name])
            for key,value in node.items():
                if key!='$defs':collect(value)
        elif isinstance(node,list):
            for item in node:collect(item)
    collect(result)
    result['$defs']={key:value for key,value in result['$defs'].items() if key in needed}
    Draft202012Validator.check_schema(result)
    return result


def _wire_defaults(value,node,definitions,wire_definitions,depth=0):
    """Materialize nullable optionals omitted by a non-strict tool gateway."""
    if depth>40:raise ValueError('Tool arguments exceed schema depth limit')
    if '$ref' in node:
        prefix='#/$defs/'
        if not node['$ref'].startswith(prefix):return value
        return _wire_defaults(value,definitions[node['$ref'][len(prefix):]],definitions,wire_definitions,depth+1)
    for union in ('anyOf','oneOf'):
        if union in node:
            for child in node[union]:
                candidate=_wire_defaults(deepcopy(value),child,definitions,wire_definitions,depth+1)
                rule=convert(child);rule['$defs']=wire_definitions
                if Draft202012Validator(rule).is_valid(candidate):return candidate
            return value
    if _map(node):
        if not isinstance(value,list):return value
        child=node.get('additionalProperties',{});child=child if isinstance(child,dict) else {}
        return [{**item,'value':_wire_defaults(item.get('value'),child,definitions,wire_definitions,depth+1)}
                if isinstance(item,dict) and 'value' in item else item for item in value]
    if node.get('type')=='object' and isinstance(value,dict):
        required=set(node.get('required',[]));result=deepcopy(value)
        for key,child in node.get('properties',{}).items():
            if key in result:result[key]=_wire_defaults(result[key],child,definitions,wire_definitions,depth+1)
            elif key not in required:result[key]=None
        return result
    if node.get('type')=='array' and isinstance(value,list):
        positional=node.get('prefixItems',[])
        return [_wire_defaults(item,positional[index] if index<len(positional) else node.get('items',{}),definitions,wire_definitions,depth+1)
                for index,item in enumerate(value)]
    return value


def _pairs(items,decode_value):
    result={}
    for item in items:
        key=item['key']
        if key in result:raise ValueError('Duplicate map key in tool arguments')
        result[key]=decode_value(item['value'])
    return result


def _json(value,encode=False,depth=0):
    if depth>32:raise ValueError('Tool arguments exceed JSON depth limit')
    walk=lambda v:_json(v,encode,depth+1)
    if isinstance(value,list):return [walk(v) for v in value]
    if isinstance(value,dict):
        if encode:return {'entries':[{'key':key,'value':walk(v)} for key,v in value.items()]}
        return _pairs(value['entries'],walk)
    return value


def transform(value,source,*,encode=False):
    wire=schema_for(source);definitions=source.get('$defs',{})
    def validate(instance,schema,stage):
        try:Draft202012Validator(schema).validate(instance)
        except ValidationError as exc:
            exc.transport_stage=stage;exc.transport_root_schema=schema;exc.transport_value=instance
            raise
    if not encode:value=_wire_defaults(value,source,definitions,wire['$defs'])
    validate(value,source if encode else wire,'source' if encode else 'wire')
    def walk(value,node,depth=0):
        if depth>40:raise ValueError('Tool arguments exceed schema depth limit')
        if '$ref' in node:
            prefix='#/$defs/'
            if not node['$ref'].startswith(prefix):raise ValueError('Only local schema references are allowed')
            return walk(value,definitions[node['$ref'][len(prefix):]],depth+1)
        if _any(node):return _json(value,encode)
        for union in ('anyOf','oneOf'):
            if union in node:
                for child in node[union]:
                    candidate=deepcopy(child) if encode else convert(child)
                    candidate['$defs']=definitions if encode else wire['$defs']
                    if Draft202012Validator(candidate).is_valid(value):return walk(value,child,depth+1)
                raise ValueError('Tool argument does not match a declared union variant')
        if _map(node):
            child=node.get('additionalProperties',{});child=child if isinstance(child,dict) else {}
            if encode:return [{'key':key,'value':walk(item,child,depth+1)} for key,item in value.items()]
            return _pairs(value,lambda item:walk(item,child,depth+1))
        if node.get('type')=='object':
            required=set(node.get('required',[]));result={}
            for key,child in node.get('properties',{}).items():
                if key not in value:
                    if encode:result[key]=None
                    continue
                if value[key] is None and key not in required:
                    if encode:result[key]=None
                    continue
                result[key]=walk(value[key],child,depth+1)
            return result
        if node.get('type')=='array':
            positional=node.get('prefixItems',[])
            return [walk(item,positional[index] if index<len(positional) else node.get('items',{}),depth+1) for index,item in enumerate(value)]
        return value
    result=walk(value,source)
    if encode:validate(result,wire,'encoded')
    else:validate(result,source,'decoded')
    return result


def validation_feedback(error,limit=12):
    """Choose the matching discriminated branch and report paths, never input values."""
    root=getattr(error,'transport_root_schema',{});definitions=root.get('$defs',{})
    result=[]
    def resolve(node):
        while '$ref' in node and node['$ref'].startswith('#/$defs/'):node=definitions[node['$ref'][8:]]
        return node
    def render(err,prefix=()):
        if len(result)>=limit:return
        path=(*prefix,*err.absolute_path)
        if err.validator in ('anyOf','oneOf'):
            alternatives=[]
            for branch in err.schema[err.validator]:
                node=resolve(branch);score=0
                if node.get('type')=='null' and err.instance is not None:score+=1000
                if isinstance(err.instance,dict):
                    for key in ('type','kind','mode','task_id','operation'):
                        rule=node.get('properties',{}).get(key,{})
                        allowed=rule.get('enum',[rule['const']] if 'const' in rule else [])
                        if key in err.instance and allowed:score+=-10 if err.instance[key] in allowed else 1000
                candidate={**node,'$defs':definitions}
                children=list(Draft202012Validator(candidate).iter_errors(err.instance))
                alternatives.append((score+len(children),children))
            if alternatives:
                for child in min(alternatives,key=lambda item:item[0])[1]:render(child,path)
                return
        if err.validator=='required':
            for key in sorted(set(err.validator_value)-set(err.instance)):
                if len(result)<limit:result.append({'path':'.'.join(map(str,(*path,key))),'type':'required','message':'Required field is missing'})
        elif err.validator=='additionalProperties':
            for key in sorted(set(err.instance)-set(err.schema.get('properties',{}))):
                if len(result)<limit:result.append({'path':'.'.join(map(str,(*path,key))),'type':'extra_forbidden','message':'Field is not allowed for this command variant'})
        else:
            result.append({'path':'.'.join(map(str,path)),'type':str(err.validator),'message':'Value violates its declared '+str(err.validator)+' constraint'})
    render(error)
    return result or [{'path':'.'.join(map(str,error.absolute_path)),'type':str(error.validator),'message':'Tool arguments do not match their declared schema'}]
