import asyncio
import copy

import pytest
from mcp import Client
from starlette.testclient import TestClient

from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.evidence import verify_derivation
from iacpaas_mcp.navigation import children_page
from iacpaas_mcp.server import build_server
from iacpaas_mcp.web import create_app

PATH='Учебные/Большая база'


def backend():
    result=MockBackend()
    result.resources={PATH:{'name':'Большая база','type':'КОРЕНЬ','successors':[
        {'name':f'Раздел {i}','type':'НЕТЕРМИНАЛ','successors':[
            {'name':'г/л','type':'ТЕРМИНАЛ-ЗНАЧЕНИЕ','value':'x'*500}]} for i in range(110)]}}
    return result


def test_wide_tree_is_paged_without_descendants_and_keeps_quoted_paths():
    graph=backend().resources[PATH]
    first=children_page(graph,limit=20)
    second=children_page(graph,offset=20,limit=20)
    assert first['total']==110 and first['next_offset']==20 and second['children'][0]['path']=='Раздел 20'
    assert all('successors' not in item['graph'] and item['has_children'] for item in first['children'])
    leaf=children_page(graph,'Раздел 0')['children'][0]
    assert leaf['path']=='Раздел 0/‴г/л‴' and not leaf['has_children']
    assert leaf['graph']['value_truncated'] and len(leaf['graph']['value_preview'])==300


def test_mcp_navigation_replays_snapshot_and_detects_tampering_and_stale_page():
    async def run():
        async with Client(build_server(backend=backend())) as client:
            args={'path':PATH,'limit':20}
            result=await client.call_tool('get_children',args)
            assert not result.is_error
            data=result.structured_content
            record={'tool':'get_children','arguments':args,'data':data}
            assert verify_derivation(record)
            altered=copy.deepcopy(record);altered['data']['total']=111
            assert not verify_derivation(altered)
            changed=await client.call_tool('get_children',{**args,'offset':20,'expected_snapshot':'0'*64})
            assert changed.is_error and changed.structured_content['error']['code']=='snapshot_changed'
            too_large=await client.call_tool('get_subgraph',{'path':PATH,'depth':1})
            assert too_large.is_error and too_large.structured_content['error']['code']=='subgraph_too_large'
    asyncio.run(run())


def test_navigation_http_errors_are_actionable_and_do_not_hide_depth(tmp_path):
    with TestClient(create_app(tmp_path/'web.sqlite3',server=build_server(backend=backend())),base_url='http://127.0.0.1') as client:
        first=client.get('/api/resource/children',params={'path':PATH})
        assert first.status_code==200 and len(first.json()['children'])==20
        assert client.get('/api/resource/children',params={'path':PATH,'snapshot':'invalid'}).status_code==400
        assert client.get('/api/resource/children').status_code==400
        assert client.get('/api/resource/children',params={'path':PATH,'limit':21}).status_code==400
        assert client.get('/api/resource/children',params={'path':PATH,'snapshot':'0'*64}).status_code==409
        wide=client.get('/api/resource',params={'path':PATH,'depth':1})
        assert wide.status_code==413 and 'get_children' in wide.json()['suggestion']
        shallow=client.get('/api/resource',params={'path':PATH,'depth':0})
        assert shallow.status_code==200 and not shallow.json()['graph'].get('successors')
