import asyncio
import copy
import json

import pytest

from iacpaas_mcp import demo
from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.graph import counts, validate


def test_demo_uses_own_ontology_and_distinct_synthetic_data():
    plan=demo.bundle('test-run')
    ontology,data=[validate(args['ir_json']) for args in plan['imports']]
    assert data['ontology']==ontology['path']
    assert data['meta']==ontology['title']
    assert counts(ontology)=={'nodes':4,'edges':3,'links':0}
    assert counts(data)=={'nodes':7,'edges':6,'links':0}
    assert all(not args['clear_if_exists'] for args in plan['imports'])
    assert plan['update']['path']==plan['paths'][1]


def test_demo_blocks_changes_outside_the_exact_manifest():
    plan=demo.bundle('test-run');backend=MockBackend()
    guarded=demo.RestrictedDemoBackend(backend,plan)
    async def run():
        with pytest.raises(ValueError):await guarded.update({**plan['update'],'path':'Загрузки/Справочник по материалам'})
        with pytest.raises(ValueError):await guarded.import_resource({**plan['imports'][0],'clear_if_exists':True})
        with pytest.raises(ValueError):await guarded.export('Загрузки/Справочник по материалам')
    asyncio.run(run())
    assert not guarded.mutations and list(backend.resources)==['Учебные/База наблюдений']


def test_full_mock_demo_preserves_original_fixture_and_no_capabilities_in_report(tmp_path,monkeypatch):
    backend=MockBackend();before=copy.deepcopy(backend.resources)
    monkeypatch.setattr(demo,'LOGS',tmp_path/'logs')
    monkeypatch.setattr(demo,'MockBackend',lambda:backend)
    directory=asyncio.run(demo.demonstrate(accept_mock=True))
    report=json.loads((directory/'report.json').read_text(encoding='utf-8'))
    assert report['status']=='passed' and report['mode']=='mock'
    assert backend.resources['Учебные/База наблюдений']==before['Учебные/База наблюдений']
    assert len(backend.resources)==3
    assert 'confirm_token' not in json.dumps(report)
    assert [e['event'] for e in report['events']][-2:]==['updated_and_read_back','completed']


def test_noninteractive_mock_consent_is_never_accepted_for_live():
    with pytest.raises(ValueError,match='accept-mock'):
        asyncio.run(demo.demonstrate(live=True,accept_mock=True))


def test_demo_reports_import_success_but_never_claims_failed_update_passed(tmp_path,monkeypatch):
    class FailingUpdate(MockBackend):
        async def update(self,args):raise ValueError('Platform update unavailable')
    monkeypatch.setattr(demo,'LOGS',tmp_path/'logs')
    monkeypatch.setattr(demo,'MockBackend',FailingUpdate)
    directory=asyncio.run(demo.demonstrate(accept_mock=True))
    report=json.loads((directory/'report.json').read_text(encoding='utf-8'))
    assert report['status']=='created_update_unavailable'
    assert len([e for e in report['events'] if e['event']=='created'])==2
    assert report['events'][-1]['current_status']==['учебный пример']


def test_inspect_existing_demo_only_reads_without_recreating_or_updating(tmp_path,monkeypatch):
    plan=demo.bundle('inspect-test');backend=MockBackend()
    for path,args in zip(plan['paths'],plan['imports']):backend.resources[path]=json.loads(args['ir_json'])
    before=copy.deepcopy(backend.resources)
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps(plan),encoding='utf-8')
    monkeypatch.setattr(demo,'HttpBackend',lambda:backend)
    results=asyncio.run(demo.inspect_resources(manifest))
    assert [item['counts']['nodes'] for item in results]==[4,7]
    assert backend.resources==before
