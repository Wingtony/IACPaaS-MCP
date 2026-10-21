import asyncio
import json

import httpx
import pytest
from mcp import Client

from iacpaas_mcp import provenance
from iacpaas_mcp.agent import _run_agent
from iacpaas_mcp.auth import signin
from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.context import pack_context, estimate_tokens
from iacpaas_mcp.evidence import EvidenceStore
from iacpaas_mcp.graph import counts
from iacpaas_mcp.server import build_server

PATH = "Учебные/База наблюдений"


def source_store(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "SNAPSHOTS", tmp_path / "exports")
    graph = asyncio.run(MockBackend().export(PATH))
    store = EvidenceStore(tmp_path / "proof", scope=PATH)
    store.add("count_graph", {"path": PATH}, {**counts(graph), "_source":graph["_source"]})
    return store


def test_exact_claims_and_archive(tmp_path, monkeypatch):
    store = source_store(tmp_path, monkeypatch)
    result = store.verify('{"claims":[{"evidence_id":"S1","pointer":"/nodes","value":8}]}')
    assert result["status"] == "verified_fields" and result["meaning_verified"] is False
    assert "8" in store.render(result)
    assert (tmp_path / "proof" / "S1.json").exists()
    assert store.read("S1", "/edges")["value"] == 8


@pytest.mark.parametrize("claim", [
    {"evidence_id":"S1","pointer":"/nodes","value":9},
    {"evidence_id":"S1","pointer":"/nodes","value":"8"},
    {"evidence_id":"S9","pointer":"/nodes"},
    {"evidence_id":"S1","pointer":"/invented"},
    {"evidence_id":"S1","pointer":"/_source/path"},
    {"evidence_id":"S1","pointer":7},
])
def test_hallucinations_blocked(tmp_path, monkeypatch, claim):
    store = source_store(tmp_path, monkeypatch)
    assert store.verify(json.dumps({"claims":[claim]}))["status"] == "blocked"
    assert store.verify("У нас точно 999 вершин")["status"] == "blocked"


def test_snapshot_tampering_blocked(tmp_path, monkeypatch):
    store = source_store(tmp_path, monkeypatch)
    for file in (tmp_path / "exports").glob("*.json"):
        file.write_text('{}', encoding="utf-8")
    assert store.verify('{"claims":[{"evidence_id":"S1","pointer":"/nodes"}]}')["status"] == "blocked"


def test_incorrect_calculation_blocked_even_with_valid_hash(tmp_path, monkeypatch):
    store = source_store(tmp_path, monkeypatch)
    graph = asyncio.run(MockBackend().export(PATH))
    source = store.add("count_graph", {"path": PATH}, {"nodes":999,"edges":8,"links":1,"_source":graph["_source"]})
    assert store.verify(json.dumps({"claims":[{"evidence_id":source,"pointer":"/nodes","value":999}]}))["status"] == "blocked"


@pytest.mark.parametrize("tool,args,pointer", [
    ("get_inforesource", {"path":PATH}, "/name"),
    ("get_subgraph", {"path":PATH,"root":"Основные жалобы","depth":2}, "/graph/name"),
    ("query_graph", {"path":PATH,"root":"Основные жалобы","meta":"Наблюдение"}, "/total"),
])
def test_derived_read_tools_match_export(tmp_path, monkeypatch, tool, args, pointer):
    monkeypatch.setattr(provenance,"SNAPSHOTS",tmp_path / "exports")
    async def run():
        async with Client(build_server(backend=MockBackend(),allow_write=False)) as client:
            return (await client.call_tool(tool,args)).structured_content
    data=asyncio.run(run())
    store=EvidenceStore(tmp_path / "evidence",scope=PATH)
    source=store.add(tool,args,data)
    assert store.verify(json.dumps({"claims":[{"evidence_id":source,"pointer":pointer}]}))["status"] == "verified_fields"


def test_context_compaction_preserves_current_question_and_tool_groups():
    history = [{"role":"system","content":"Не менять выбранную базу X"}]
    for i in range(30):
        history += [{"role":"user","content":f"Вопрос {i} " + "старые слова " * 80},
                    {"role":"assistant","content":"Старый ответ " * 80}]
    history += [{"role":"user","content":"Текущий вопрос о температуре"},
                {"role":"assistant","content":"","tool_calls":[{"function":{"name":"get_subgraph","arguments":{}}}]},
                {"role":"tool","tool_name":"get_subgraph","content":json.dumps({"evidence_id":"S1","fields":[{"pointer":"/name","value":"Температура"}],"padding":"x"*10000})}]
    packed, info = pack_context(history, [], [{"id":"S1","path":PATH}], budget=2000)
    assert info["compressed"] and info["after_estimate"] <= 2000
    assert packed[0]["content"] == history[0]["content"]
    assert any(message.get("content") == "Текущий вопрос о температуре" for message in packed)
    for index,message in enumerate(packed):
        if message["role"] == "tool":
            assert packed[index-1].get("tool_calls") or packed[index-1]["role"] == "tool"
    assert any("S1" in message.get("content", "") for message in packed)


def test_context_does_not_silently_cut_current_question():
    with pytest.raises(ValueError):
        pack_context([{"role":"system","content":"system"},{"role":"user","content":"x"*20000}], [], [], budget=200)


def test_verified_agent_without_real_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance,"SNAPSHOTS",tmp_path / "exports")
    responses = iter([
        {"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"count_graph","arguments":{"path":PATH}}}]}},
        {"message":{"role":"assistant","content":'{"claims":[{"evidence_id":"S1","pointer":"/nodes","value":8},{"evidence_id":"S1","pointer":"/edges","value":8}]}'}},
    ])
    payloads=[]
    real_http=httpx.AsyncClient
    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200,json=next(responses))
    monkeypatch.setattr("iacpaas_mcp.agent.httpx.AsyncClient",lambda **kw:real_http(transport=httpx.MockTransport(handler),**kw))
    events=[]
    async def run():
        async with Client(build_server(backend=MockBackend(),allow_write=False)) as client:
            return await _run_agent(client,"Посчитать", "test", "http://mock.test",tmp_path / "trace.jsonl",on_event=events.append)
    answer=asyncio.run(run())
    assert "Вершины" in answer and "| 8 |" in answer
    assert any(event["event"]=="verification" and event["status"]=="verified_fields" for event in events)
    assert len(payloads)==2


def test_signin_sends_only_to_official_endpoint():
    def handle(request):
        assert str(request.url)=="https://iacpaas.dvo.ru/api/signin"
        assert json.loads(request.content)=={"username":"test-user","password":"test-password"}
        return httpx.Response(200,json={"accessToken":"test-jwt"})
    assert signin("test-user","test-password",httpx.MockTransport(handle))=="test-jwt"


def test_signin_does_not_return_error_body_secrets():
    with pytest.raises(ValueError) as exc:
        signin("user","password",httpx.MockTransport(lambda request:httpx.Response(401,json={"secret":"private"})))
    assert "private" not in str(exc.value)


def test_context_configuration_and_reserved_output(monkeypatch):
    from iacpaas_mcp.context import context_settings
    monkeypatch.setenv("AGENT_CONTEXT_LENGTH", "16384")
    monkeypatch.setenv("AGENT_CONTEXT_BUDGET", "12000")
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "1000")
    assert context_settings() == (16384, 12000, 1000)
    monkeypatch.setenv("AGENT_CONTEXT_BUDGET", "16000")
    with pytest.raises(ValueError, match="AGENT_CONTEXT_BUDGET"):
        context_settings()


def test_32k_profile_ignores_inherited_ollama_default(monkeypatch):
    from iacpaas_mcp.context import context_settings
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH","8192")
    monkeypatch.setenv("AGENT_CONTEXT_LENGTH","32768")
    monkeypatch.setenv("AGENT_CONTEXT_BUDGET","24000")
    monkeypatch.setenv("OLLAMA_NUM_PREDICT","2048")
    assert context_settings()==(32768,24000,2048)
