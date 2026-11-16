"""Meaningful read progress, excluding transport timestamps and generated captions."""
import hashlib
import json


def signature(tool,args,data):
    ignored={'_source','_sources','evidence_id','retrieved_at','checked_at','age_seconds','seconds','offset','limit','next_offset'}
    if tool=='query_knowledge':ignored|={'plan','answer','dataset'}
    def clean(value):
        if isinstance(value,dict):return {k:clean(v) for k,v in value.items() if k not in ignored}
        if isinstance(value,list):return [clean(v) for v in value]
        return value
    sources=[data.get('_source',{}),*data.get('_sources',[])]
    source_ids=[(s.get('path'),s.get('export_sha256'),s.get('stale',False)) for s in sources if s]
    scope={k:args[k] for k in ('path','root','evidence_id','pointer') if k in args}
    return hashlib.sha256(json.dumps([tool,scope,source_ids,clean(data)],ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def fallback_ids(records):
    useful=[(i,r) for i,r in records.items() if not (r['tool']=='query_knowledge' and r['data'].get('plan',{}).get('operation')=='catalog')]
    domain=[(i,r) for i,r in useful if r['tool']=='query_knowledge' and r['data'].get('plan',{}).get('operation')!='resources']
    if domain:useful=domain
    elif any(r['tool']!='list_inforesources' for _,r in useful):useful=[(i,r) for i,r in useful if r['tool']!='list_inforesources']
    unique={}
    for i,r in useful:unique[signature(r['tool'],r['arguments'],r['data'])]=i
    return list(unique.values())[-6:]
