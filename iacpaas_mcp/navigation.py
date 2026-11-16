"""Shallow, paged graph navigation. No hidden depth reduction or recursive DOM export."""
from .graph import select


def metadata(node):
    value={k:node[k] for k in ('name','meta','type','id','valtype','value') if k in node}
    if isinstance(value.get('value'),str) and len(value['value'])>300:
        value['value_preview']=value.pop('value')[:300]
        value['value_truncated']=True
    return value


def children_page(graph,root='',offset=0,limit=20):
    node=select(graph,root)
    children=node.get('successors',[])
    records=[]
    for child in children[offset:offset+limit]:
        label=str(child.get('name',child.get('value','')))
        if '/' in label:label='‴'+label+'‴'
        path='/'.join(filter(None,[root,label]))
        records.append({'graph':metadata(child),'path':path,'has_children':bool(child.get('successors')) and 'link' not in child,'is_link':'link' in child})
    return {'parent':metadata(node),'root':root,'children':records,'total':len(children),'offset':offset,'limit':limit,
            'has_more':offset+len(records)<len(children),'next_offset':offset+len(records) if offset+len(records)<len(children) else None}
