"""Regression tests for the comparison checklist; all platform/model I/O is mocked."""
import asyncio
import copy
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from iacpaas_mcp import provenance, search, verifier
from iacpaas_mcp.backend import MockBackend, PlatformError
from iacpaas_mcp.conditions import CompositionCondition, satisfies
from iacpaas_mcp.confirmation import elicitation_handler
from iacpaas_mcp.knowledge import KnowledgePlan, query_graphs, execute_plan, verify_result
from iacpaas_mcp.llm import ModelConfig
from iacpaas_mcp.ontology import build_resource
from iacpaas_mcp.planner_agent import guard_plan
from iacpaas_mcp.server import build_server
from iacpaas_mcp.write_scope import WriteScope

MATERIALS = "Загрузки/Справочник по материалам"
OWN = "Мои проекты"
ONTOLOGY = "Загрузки/Онтология проектов"


@pytest.fixture(autouse=True)
def local_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "SNAPSHOTS", tmp_path / "exports")
    monkeypatch.setattr(search, "INDEX", tmp_path / "index.sqlite3")


def component(symbol, low, high):
    def bound(name, value):
        return {"name": name, "successors": [{"meta": "Числовое значение", "value": value}]}
    return {"meta": "Компонент", "name": symbol, "successors": [{"meta": "Химический элемент", "name": symbol,
        "successors": [{"meta": "%", "value": "%"}, {"name": "Числовой интервал", "successors": [bound("Нижняя граница", low), bound("Верхняя граница", high)]}]}]}


def materials():
    def mark(name, cr, ni=None):
        return {"name": name, "meta": "Материал", "successors": [{"name": "Элементный состав", "meta": "Элементный состав", "successors": [component("Cr", *cr)] + ([component("Ni", *ni)] if ni else [])}]}
    return {MATERIALS: {"type": "КОРЕНЬ", "name": "Справочник по материалам", "date": "2026-11-11",
        "successors": [{"name": "Стали", "successors": [mark("A", (17, 19), (9, 11)), mark("B", (18, 20), (10, 12)), mark("C", (0, 19)), mark("D", (0, 0), (0, 0))]}]}}


def run_query(**plan):
    data = materials()
    return query_graphs(plan, data, list(data))


def test_and_or_and_absence_apply_to_complete_dataset():
    conditions = [{"element": "Cr", "op": "le", "value": 19}, {"element": "Ni", "op": "ge", "value": 9}]
    both = run_query(operation="filter", conditions=conditions)
    assert [r[0] for r in both["rows"]] == ["A"]
    assert all(len(row) == 3 for row in both["rows"])  # no fabricated numeric bound
    either = run_query(operation="filter", conditions=conditions, condition_mode="any")
    assert either["total"] == 4
    absent = run_query(operation="materials", conditions=[{"element":"Cr", "op":"present"}, {"element":"Ni", "op":"absent"}])
    assert [r[0] for r in absent["rows"]] == ["C"]  # D records Ni=0; it is present.


@pytest.mark.parametrize("op,value,expected", [("lt",19,False),("le",19,True),("ge",17,True),("gt",17,False),("present",None,True),("absent",None,False)])
def test_interval_boundary_semantics(op, value, expected):
    assert satisfies({"lower":Decimal(17), "upper":Decimal(19), "unit":"%"}, CompositionCondition(element="Cr",op=op,value=value)) is expected


@pytest.mark.parametrize("condition", [{"op":"le"}, {"op":"present","value":0}, {"op":"absent","value":1}, {"op":"ge","value":101}, {"op":"le","value":float("nan")}, {"op":"ge","value":9,"include_unlisted":True}])
def test_invalid_predicates_fail_closed(condition):
    with pytest.raises(ValueError): CompositionCondition(element="Cr", **condition)


def test_unknown_unit_or_bound_is_not_a_match():
    c = CompositionCondition(element="Cr",op="le",value=19)
    for component in [None, {"upper":None,"unit":"%"}, {"upper":Decimal(1),"unit":"г"}]:
        assert not satisfies(component,c)
    assert satisfies(None,CompositionCondition(element="Cr",op="absent"))


@pytest.mark.parametrize('question',['Cr ≤ 19% и Ni ≥ 9%','Есть Cr, но Ni не указан'])
def test_guard_does_not_silently_drop_a_second_condition(question):
    wrong=KnowledgePlan(operation='filter',element='Cr',threshold=19)
    assert guard_plan(wrong,question,[]).clarification=='compound'


def test_guard_keeps_complete_compound_conditions():
    plan=KnowledgePlan(operation='filter',conditions=[{'element':'Cr','op':'le','value':19},{'element':'Ni','op':'ge','value':9}])
    actual=guard_plan(plan,'Cr ≤ 19% и Ni ≥ 9%',[])
    assert actual.operation=='filter' and actual.conditions==plan.conditions


def test_pagination_is_complete_stable_and_preserves_conditions():
    plan = {"operation":"filter", "limit":2, "conditions":[{"element":"Cr","op":"present"}]}
    first = run_query(**plan)
    previous = {**first["plan"], "dataset": first["dataset"]}
    following = guard_plan(KnowledgePlan(operation="resources"), "Покажи следующие десять", [{"query_plan":previous}])
    assert following.conditions == KnowledgePlan(**plan).conditions and following.offset == 2
    second = run_query(**following.model_dump())
    assert not second["has_more"] and second["next_offset"] is None
    assert len({row[0] for row in first["rows"]+second["rows"]}) == 4
    assert first["total"] == second["total"] == 4
    changed = materials(); changed[MATERIALS]["date"] = "2026-11-12"
    refused = query_graphs(following, changed, list(changed))
    assert refused["status"] == "clarify" and not refused["rows"]


@pytest.mark.parametrize("path", ["Загрузки/X", "ЗАГРУЗКИ/X", "Мой Фонд/Загрузки/X", "user/Мой Фонд/Загрузки/X$;", "Мои проекты/../Загрузки/X", "Мои проекты/%2e%2e/X", "", "Чужие/X", "Мои проекты"])
def test_server_scope_rejects_protected_paths_and_aliases(path):
    with pytest.raises(ValueError): WriteScope(roots=[OWN]).check(path)


def ontology():
    return {"title":"Онтология проектов", "type":"КОРЕНЬ", "successors":[{"name":"Проект", "type":"НЕТЕРМИНАЛ", "specifier":"непустое множество", "successors":[{"name":"Сценариев", "type":"ТЕРМИНАЛ-СОРТ", "valtype":"INTEGER", "specifier":"в точности один"}]}]}


ITEMS = [{"concept":"Проект", "name":"Web", "children":[{"concept":"Сценариев", "value":3}]}]


def test_ontology_builder_enforces_schema_cardinality_and_types():
    graph = build_resource(ontology(),ONTOLOGY,"Моя база",ITEMS)
    assert graph["successors"][0]["successors"][0]["value"] == 3
    for items in [[], [{"concept":"Неизвестное"}], [{"concept":"Проект","name":"Web"}], ITEMS+ITEMS,
                  [{"concept":"Проект","children":[{"concept":"Сценариев","value":True}]}]]:
        with pytest.raises(ValueError): build_resource(ontology(),ONTOLOGY,"Моя база",items)
    linked=ontology();linked["successors"][0]["link"]="foreign"
    with pytest.raises(ValueError): build_resource(linked,ONTOLOGY,"Моя база",ITEMS)


@pytest.mark.parametrize("accept", [True,False])
def test_own_folder_create_readback_delete_requires_human(accept):
    backend=MockBackend(); backend.resources[ONTOLOGY]=ontology(); before=copy.deepcopy(backend.resources)
    prompts=[]
    def approve(preview): prompts.append(preview);return accept
    async def run():
        async with Client(build_server(backend=backend,known_paths=[ONTOLOGY],allow_write=True,write_roots=[OWN]),elicitation_callback=elicitation_handler(approve)) as client:
            folder=await client.call_tool("create_folder",{"parent_path":"","name":OWN})
            assert not folder.is_error
            dry=await client.call_tool("validate_tree",{"ontology_path":ONTOLOGY,"title":"Моя база","items":ITEMS})
            assert not dry.is_error and dry.structured_content["valid"]
            result=await client.call_tool("create_resource",{"folder_path":OWN,"ontology_path":ONTOLOGY,"title":"Моя база","items":ITEMS})
            assert not result.is_error, result
            assert result.structured_content["status"] == ("done" if accept else "cancelled")
            if accept:
                assert result.structured_content["result"]["verified_nodes"]==3
                duplicate=await client.call_tool("create_resource",{"folder_path":OWN,"ontology_path":ONTOLOGY,"title":"Моя база","items":ITEMS})
                assert duplicate.is_error
                deleted=await client.call_tool("delete_inforesource",{"path":OWN+"/Моя база"})
                assert not deleted.is_error and deleted.structured_content["status"]=="done"
            blocked=await client.call_tool("delete_inforesource",{"path":ONTOLOGY})
            assert blocked.is_error
    asyncio.run(run())
    assert len(prompts)==(3 if accept else 2)  # dry-run, duplicate and protected path do not ask.
    assert backend.resources==before


def test_noninteractive_clients_cannot_write():
    backend=MockBackend()
    async def run():
        async with Client(build_server(backend=backend,allow_write=True,write_roots=[OWN])) as client:
            result=await client.call_tool("create_folder",{"parent_path":"","name":OWN})
            assert result.is_error
    asyncio.run(run());assert not backend.folders


@pytest.mark.parametrize('accept',[True,False])
def test_stdio_write_confirmation_with_container_environment(tmp_path,accept):
    """Exercise the real subprocess confirmation roundtrip, not only in-process MCP."""
    env={**os.environ,'ATLAS_DOCKER':'true','IACPAAS_BACKEND':'mock','IACPAAS_ALLOW_WRITE':'true',
         'IACPAAS_WRITE_ROOTS':json.dumps([OWN]),'IACPAAS_ENV_FILE':str(tmp_path/'runtime.env'),
         'IACPAAS_LOG_DIR':str(tmp_path/'logs'),'IACPAAS_DATA_DIR':str(tmp_path/'state')}
    params=StdioServerParameters(command=sys.executable,args=['-m','iacpaas_mcp.server'],cwd=str(Path(__file__).resolve().parents[1]),env=env)
    approvals=[]
    def approve(preview):approvals.append(preview);return accept
    async def run():
        async with Client(params,elicitation_callback=elicitation_handler(approve)) as client:
            result=await client.call_tool('create_folder',{'parent_path':'','name':OWN})
            assert not result.is_error, result
            assert result.structured_content['status']==('done' if accept else 'cancelled')
    asyncio.run(run());assert len(approvals)==1


def test_container_stdio_loads_persisted_platform_selection(tmp_path):
    runtime=tmp_path/'runtime.env'
    runtime.write_text('IACPAAS_BACKEND=http\nIACPAAS_API_KEY=test-only-key\nIACPAAS_KNOWN_IRS=["Свои/Сохранённая база"]\n',encoding='utf-8')
    env={**os.environ,'ATLAS_DOCKER':'true','IACPAAS_BACKEND':'mock','IACPAAS_ENV_FILE':str(runtime),'IACPAAS_LOG_DIR':str(tmp_path/'logs')}
    params=StdioServerParameters(command=sys.executable,args=['-m','iacpaas_mcp.server'],cwd=str(Path(__file__).resolve().parents[1]),env=env)
    async def run():
        async with Client(params) as client:
            result=await client.call_tool('list_inforesources',{})
            assert not result.is_error and result.structured_content['paths']==['Свои/Сохранённая база']
            assert result.structured_content['source']=='configuration'
    asyncio.run(run())  # Only the configured list is read; no HTTP request is sent.


def test_index_checks_dates_and_marks_offline_fallback(tmp_path):
    backend=MockBackend();backend.resources=materials(); calls=[];original=backend.export
    async def track(path,depth=None): calls.append(depth);return await original(path,depth)
    backend.export=track
    index=search.ResourceSearch(backend,tmp_path/"cache.sqlite3",ttl=0)
    async def run():
        await index.full_export(MATERIALS)
        await index.full_export(MATERIALS)
        assert calls==[None,0]
        backend.resources[MATERIALS]["date"]="2026-11-12"
        await index.full_export(MATERIALS)
        assert calls==[None,0,0,None]
        async def unavailable(*args,**kwargs): raise PlatformError("offline","platform_unavailable")
        backend.export=unavailable
        result=await execute_plan(index,[MATERIALS],KnowledgePlan(operation="materials"))
        assert result["_sources"][0]["stale"] and "резервный снимок" in result["answer"]
        assert verify_result(result,[MATERIALS])
        async def forbidden(*args,**kwargs): raise PlatformError("forbidden","permission_denied",False)
        backend.export=forbidden
        with pytest.raises(PlatformError): await index.full_export(MATERIALS)
    asyncio.run(run())


def count_step():
    from iacpaas_mcp.graph import counts
    path='Учебные/База наблюдений'
    graph=asyncio.run(MockBackend().export(path))
    return {'name':'count_graph','arguments':{'path':path},'result':{**counts(graph),'_source':graph['_source']}}


@pytest.mark.parametrize("pointer,expected", [("/nodes","confirmed"),("/missing","reference_error"),("/answer","reference_error")])
def test_claim_verifier_downgrades_invented_source_bindings(monkeypatch,pointer,expected):
    async def complete(*args):
        return {"message":{"content":json.dumps({"claims":[{"segment_id":"A1", "status":"confirmed", "fact_id":"T0"+pointer}]})}}
    monkeypatch.setattr(verifier,"complete",complete)
    report=asyncio.run(verifier.verify_answer("Сколько?","8 вершин",[count_step()],ModelConfig("deepseek","test","fake")))
    assert report["claims"][0]["status"]==expected
    assert report["claims"][0]["binding_verified"] is (expected=="confirmed")


def test_claim_verifier_reports_unknown_fragment_without_crashing(monkeypatch):
    async def complete(*args):
        return {"message":{"content":json.dumps({"claims":[{"segment_id":"A999", "status":"not_in_data"}]})}}
    monkeypatch.setattr(verifier,"complete",complete)
    report=asyncio.run(verifier.verify_answer("Сколько?","8 вершин",[count_step()],ModelConfig("deepseek","test","fake")))
    assert report['confirmed']==0 and report['reference_errors']==1
    assert report['claims'][0]['status']=='reference_error'
