import asyncio
import copy
import json

from mcp import Client

from iacpaas_mcp import provenance
from iacpaas_mcp.agent import _run_agent
from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.evidence import EvidenceStore
from iacpaas_mcp.search import ResourceSearch,normalize
from iacpaas_mcp.server import build_server
import httpx


def backend_with_materials():
    backend=MockBackend()
    backend.resources={
        "Загрузки/Материалы":{"type":"КОРЕНЬ","name":"Материалы","successors":[{"name":"12X18H10T","type":"НЕТЕРМИНАЛ","meta":"Марка"}]},
        "Загрузки/Химия":{"type":"КОРЕНЬ","name":"Химия","successors":[{"name":"Железо","type":"НЕТЕРМИНАЛ","meta":"Элемент","successors":[{"value":"Fe","type":"ТЕРМИНАЛ-ЗНАЧЕНИЕ","valtype":"STRING","meta":"Символ"}]}]}}
    return backend


def test_global_search_accounts_for_every_resource_and_cache(tmp_path,monkeypatch):
    monkeypatch.setattr(provenance,"SNAPSHOTS",tmp_path / "exports")
    backend=backend_with_materials()
    calls=[]
    original=backend.export
    async def track(path,depth=None):
        calls.append(path)
        return await original(path,depth)
    backend.export=track
    search=ResourceSearch(backend,tmp_path / "index.sqlite3")
    async def run():
        first=await search.search(list(backend.resources),"12Х18Н10Т")
        assert first["complete"] and first["searched_resources"]==2
        assert first["matches"][0]["name"]=="12X18H10T"
        assert len(calls)==2
        second=await search.search(list(backend.resources),"Fe")
        assert second["matches"][0]["resource"]=="Загрузки/Химия"
        assert len(calls)==2 and all(item["cached"] for item in second["coverage"])
        evidence=EvidenceStore(tmp_path / "evidence",scope="Все подключённые базы")
        source=evidence.add("search_resources",{"text":"Fe"},second)
        assert evidence.verify(json.dumps({"claims":[{"evidence_id":source,"pointer":"/matches/0/value","value":"Fe"}]}))["status"]=="verified_fields"
    asyncio.run(run())


def test_partial_search_never_claims_complete(tmp_path,monkeypatch):
    monkeypatch.setattr(provenance,"SNAPSHOTS",tmp_path / "exports")
    backend=backend_with_materials()
    original=backend.export
    async def fail_one(path,depth=None):
        if path.endswith("Химия"):
            raise ValueError("Нет доступа")
        return await original(path,depth)
    backend.export=fail_one
    search=ResourceSearch(backend,tmp_path / "index.sqlite3")
    data=asyncio.run(search.search(list(backend.resources),"Fe"))
    assert not data["complete"] and data["searched_resources"]==1 and data["configured_resources"]==2
    evidence=EvidenceStore(tmp_path / "evidence",scope="Все базы")
    source=evidence.add("search_resources",{"text":"Fe"},data)
    report=evidence.verify(json.dumps({"claims":[{"evidence_id":source,"pointer":"/total","value":0}]}))
    assert report["status"]=="verified_fields" and "Поиск неполный" in evidence.render(report)


def test_search_index_handles_more_than_old_20000_node_limit(tmp_path,monkeypatch):
    monkeypatch.setattr(provenance,"SNAPSHOTS",tmp_path / "exports")
    backend=MockBackend()
    backend.resources={"Большая база":{"name":"Корень","type":"КОРЕНЬ","successors":[{"name":f"Материал {i}","type":"НЕТЕРМИНАЛ"} for i in range(20005)]}}
    search=ResourceSearch(backend,tmp_path / "index.sqlite3")
    data=asyncio.run(search.search(["Большая база"],"Материал 20004"))
    assert data["complete"] and data["total"]==1 and data["coverage"][0]["nodes"]==20006
    assert data["matches"][0]["snapshot_pointer"]=="/successors/20004"


def test_account_namespaces_and_explicit_one_resource_filter(tmp_path,monkeypatch):
    monkeypatch.setattr(provenance,"SNAPSHOTS",tmp_path / "exports")
    one=backend_with_materials()
    two=backend_with_materials()
    first=ResourceSearch(one,tmp_path / "index.sqlite3")
    second=ResourceSearch(two,tmp_path / "index.sqlite3")
    assert first.namespace!=second.namespace
    data=asyncio.run(first.search(["Загрузки/Материалы"],"Fe"))
    assert data["searched_resources"]==1 and data["total"]==0


def test_agent_all_mode_forces_search_across_all_resources(tmp_path,monkeypatch):
    monkeypatch.setattr(provenance,"SNAPSHOTS",tmp_path / "exports")
    from iacpaas_mcp import search as search_module
    monkeypatch.setattr(search_module,"INDEX",tmp_path / "index.sqlite3")
    backend=backend_with_materials()
    replies=iter([
        {"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"search_resources","arguments":{"text":"Fe","resource_path":"Загрузки/Материалы"}}}]}},
        {"message":{"role":"assistant","content":'{"claims":[{"evidence_id":"S1","pointer":"/matches/0/value","value":"Fe"}]}'}}
    ])
    real_http=httpx.AsyncClient
    monkeypatch.setattr("iacpaas_mcp.agent.httpx.AsyncClient",lambda **kw:real_http(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=next(replies))),**kw))
    events=[]
    async def run():
        async with Client(build_server(backend=backend,allow_write=False)) as client:
            return await _run_agent(client,"Что такое Fe?","test","http://mock.test",tmp_path / "trace.jsonl",on_event=events.append)
    answer=asyncio.run(run())
    assert "Fe" in answer and "2 из 2" in answer
    tool=next(event for event in events if event["event"]=="tool")
    assert tool["arguments"]["resource_path"]=="" and tool["result"]["searched_resources"]==2


def test_normalization_handles_cyrillic_latin_grade_and_yo():
    assert normalize("12Х18Н10Т")==normalize("12X18H10T")
    assert normalize("твёрдость")==normalize("ТВЕРДОСТЬ")


def test_repeating_agent_finishes_with_verified_partial_result(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance,"SNAPSHOTS",tmp_path / "exports")
    from iacpaas_mcp import search as search_module
    monkeypatch.setattr(search_module,"INDEX",tmp_path / "index.sqlite3")
    requests=[]
    def reply(request):
        payload=json.loads(request.content)
        requests.append(payload)
        if not payload["tools"]:
            return httpx.Response(200,json={"message":{"role":"assistant","content":'{"claims":[{"evidence_id":"S1","pointer":"/matches/0/value","value":"Fe"}]}'}})
        return httpx.Response(200,json={"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"search_resources","arguments":{"text":"Fe"}}}]}})
    real_http=httpx.AsyncClient
    monkeypatch.setattr("iacpaas_mcp.agent.httpx.AsyncClient",lambda **kw:real_http(transport=httpx.MockTransport(reply),**kw))
    events=[]
    async def run():
        async with Client(build_server(backend=backend_with_materials(),allow_write=False)) as client:
            return await _run_agent(client,"Найди Fe","test","http://mock.test",tmp_path / "trace.jsonl",on_event=events.append)
    answer=asyncio.run(run())
    assert "Fe" in answer and "может быть неполным" in answer
    assert len(requests)==4 and requests[-1]["tools"]==[]
    assert sum(event["event"]=="tool" for event in events)==1
    assert next(event for event in events if event["event"]=="verification")["status"]=="verified_fields"


def test_context_retains_latest_search_after_many_source_records():
    from iacpaas_mcp.context import pack_context
    messages=[{"role":"system","content":"Правила"},{"role":"user","content":"Найди Fe"}]
    for i in range(16):
        messages.extend([{"role":"assistant","content":"","tool_calls":[{"function":{"name":"search_resources","arguments":{"text":"Fe"}}}]},
                         {"role":"tool","tool_name":"search_resources","content":json.dumps({"evidence_id":f"S{i}","fields":[{"pointer":"/matches/0/name","value":"Fe"}],"padding":"x"*1500})}])
    index=[{"id":f"S{i}","tool":"search_resources","path":"Загрузки/Справочник по материалам"} for i in range(16)]
    packed, metrics=pack_context(messages,[],index,budget=1800)
    assert metrics["compressed"] and metrics["after_estimate"]<=1800
    assert packed[-1]["role"]=="tool" and json.loads(packed[-1]["content"])["evidence_id"]=="S15"
    assert "Fe" in packed[-1]["content"]
