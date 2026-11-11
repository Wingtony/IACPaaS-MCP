import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from mcp import Client, StdioServerParameters
from iacpaas_mcp.confirmation import elicitation_handler

from iacpaas_mcp.backend import HttpBackend, MockBackend
from iacpaas_mcp.graph import counts, select, validate
from iacpaas_mcp.policy import WritePolicy
from iacpaas_mcp.server import build_server

PATH = "Учебные/База наблюдений"


def test_counts_links():
    graph = asyncio.run(MockBackend().export(PATH))
    assert counts(graph) == {"nodes": 8, "edges": 8, "links": 1}
    assert counts(select(graph, "Основные жалобы")) == {"nodes": 3, "edges": 2, "links": 0}


def test_quoted_slash():
    assert select({"successors": [{"name": "г/л"}]}, "‴г/л‴")["name"] == "г/л"


def test_validate_export():
    graph = asyncio.run(MockBackend().export(PATH))
    assert validate(json.dumps(graph))["title"] == "База наблюдений"


@pytest.mark.parametrize("document", ['{}', '[]', '{', '{"title":"a","type":"bad"}',
    '{"title":"a","type":"КОРЕНЬ","successors":[{"type":"ТЕРМИНАЛ-ЗНАЧЕНИЕ"}]}'])
def test_invalid_document(document):
    with pytest.raises(ValueError):
        validate(document)


def test_policy_disabled():
    with pytest.raises(ValueError):
        WritePolicy().issue("write", {})


def test_policy_one_time_and_binding():
    policy = WritePolicy(True)
    token = policy.issue("update", {"mark": "A"})
    with pytest.raises(ValueError):
        policy.consume("update", {"mark": "B"}, token)
    token = policy.issue("update", {"mark": "A"})
    policy.consume("update", {"mark": "A"}, token)
    with pytest.raises(ValueError):
        policy.consume("update", {"mark": "A"}, token)


def test_policy_expires():
    clock = [10]
    policy = WritePolicy(True, lambda: clock[0])
    token = policy.issue("write", {})
    clock[0] += 301
    with pytest.raises(ValueError):
        policy.consume("write", {}, token)


@pytest.mark.parametrize("failed", [False, True])
def test_agent_unloads_model_after_request(monkeypatch, tmp_path, failed):
    monkeypatch.setenv("AGENT_UNLOAD_AFTER_REQUEST","true")
    from iacpaas_mcp.agent import run_agent
    requests = []
    real_http = httpx.AsyncClient
    def handle(request):
        requests.append(request)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        assert request.url.path == "/api/generate"
        assert json.loads(request.content) == {"model": "qwen-agent", "keep_alive": 0}
        return httpx.Response(200, json={"done": True})
    async def fake_run(*args, **kwargs):
        if failed:
            raise ValueError("test failure")
        return "Готово"
    monkeypatch.setattr("iacpaas_mcp.agent._run_agent", fake_run)
    monkeypatch.setattr("iacpaas_mcp.agent.httpx.AsyncClient", lambda **kw: real_http(transport=httpx.MockTransport(handle), **kw))
    if failed:
        with pytest.raises(ValueError):
            asyncio.run(run_agent(None, "test", "qwen-agent", "http://ollama.test", tmp_path / "trace", mode="raw"))
    else:
        assert asyncio.run(run_agent(None, "test", "qwen-agent", "http://ollama.test", tmp_path / "trace", mode="raw")) == "Готово"
    assert [request.url.path for request in requests] == ["/api/generate", "/api/ps"]


def test_agent_keeps_model_loaded_when_application_mode_is_enabled(monkeypatch,tmp_path):
    from iacpaas_mcp.agent import run_agent
    monkeypatch.setenv("AGENT_UNLOAD_AFTER_REQUEST","false")
    async def fake(*args,**kwargs):return "Готово"
    async def forbidden(*args,**kwargs):raise AssertionError("Model must remain loaded")
    monkeypatch.setattr("iacpaas_mcp.agent._run_agent",fake)
    monkeypatch.setattr("iacpaas_mcp.agent.unload_model",forbidden)
    assert asyncio.run(run_agent(None,"test","test","http://test",tmp_path/"trace",mode="raw"))=="Готово"


def test_mcp_read_and_errors():
    async def run():
        async with Client(build_server(backend=MockBackend(), allow_write=False)) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 16
            assert all(tool.description and tool.input_schema for tool in tools)
            result = await client.call_tool("count_graph", {"path": PATH})
            assert not result.is_error
            assert result.structured_content["nodes"] == 8
            query = await client.call_tool("query_graph", {"path": PATH, "meta": "Наблюдение", "root": "Основные жалобы"})
            assert query.structured_content["total"] == 2
            assert query.structured_content["matches"][0]["path"].startswith("Основные жалобы/")
            for name, args in [("count_graph", {"path": "missing"}),
                               ("get_subgraph", {"path": PATH, "depth": 6}),
                               ("update_concept", dict(path=PATH, concept_path="Основные жалобы", metaconcept_path="Группа", mark="X"))]:
                assert (await client.call_tool(name, args)).is_error
    asyncio.run(run())


def test_write_preview_then_commit():
    async def run():
        backend = MockBackend()
        async with Client(build_server(backend=backend, allow_write=True), elicitation_callback=elicitation_handler(lambda preview: True)) as client:
            args = dict(path=PATH, concept_path="Основные жалобы", metaconcept_path="Группа", mark="Изменено")
            preview = (await client.call_tool("update_concept", args)).structured_content
            assert preview["status"] == "pending_confirmation"
            assert select(await backend.export(PATH), "Основные жалобы")
            result = await client.call_tool("update_concept", {**args, "confirm_token": preview["confirm_token"]})
            assert result.structured_content["status"] == "done"
            assert select(await backend.export(PATH), "Изменено")
            assert (await client.call_tool("update_concept", {**args, "confirm_token": preview["confirm_token"]})).is_error
    asyncio.run(run())


def test_import_preview_then_commit():
    async def run():
        backend = MockBackend()
        async with Client(build_server(backend=backend, allow_write=True), elicitation_callback=elicitation_handler(lambda preview: True)) as client:
            args = {"folder_path": "Учебные", "ir_json": '{"title":"Новая база","type":"КОРЕНЬ"}'}
            preview = (await client.call_tool("import_inforesource", args)).structured_content
            assert "Учебные/Новая база" not in backend.resources
            result = await client.call_tool("import_inforesource", {**args, "confirm_token": preview["confirm_token"]})
            assert not result.is_error
            assert "Учебные/Новая база" in backend.resources
    asyncio.run(run())


def test_http_backend_contract(monkeypatch):
    monkeypatch.setenv("IACPAAS_API_KEY", "test-key")
    requests = []
    def handle(request):
        requests.append(request)
        assert request.headers["X-API-KEY"] == "test-key"
        if request.method == "GET":
            assert request.url.params["path"] == PATH
            assert request.url.params["export-depth"] == "0"
            assert request.url.params["json-type"] == "universal"
            return httpx.Response(200, json={"success": True, "result": '{"type":"КОРЕНЬ"}'})
        payload = json.loads(request.content)
        assert payload["path"] == "Учебные" and isinstance(payload["json"], str)
        assert payload["clearIfExists"] is False
        return httpx.Response(200, json={"success": True})
    async def run():
        backend = HttpBackend(httpx.MockTransport(handle))
        assert (await backend.export(PATH, 0))["type"] == "КОРЕНЬ"
        await backend.import_resource(dict(folder_path="Учебные", ir_json="{}", clear_if_exists=False))
    asyncio.run(run())
    assert len(requests) == 2


@pytest.mark.parametrize("status,payload", [(401, {}), (200, {"success": False}), (200, {"error": "secret"})])
def test_platform_errors(monkeypatch, status, payload):
    monkeypatch.setenv("IACPAAS_API_KEY", "test")
    backend = HttpBackend(httpx.MockTransport(lambda r: httpx.Response(status, json=payload)))
    with pytest.raises(ValueError) as exc:
        asyncio.run(backend.export(PATH))
    assert "secret" not in str(exc.value)


def test_stdio_real_subprocess():
    async def run():
        env = {**os.environ, "IACPAAS_BACKEND": "mock", "IACPAAS_ALLOW_WRITE": "false"}
        params = StdioServerParameters(command=sys.executable, args=["-m", "iacpaas_mcp.server"],
                                       cwd=str(Path(__file__).resolve().parent.parent), env=env)
        async with Client(params) as client:
            assert len((await client.list_tools()).tools) == 16
            assert (await client.call_tool("count_graph", {"path": PATH})).structured_content["edges"] == 8
    asyncio.run(run())


@pytest.mark.parametrize("approved", [False, True])
def test_agent_human_confirmation(monkeypatch, tmp_path, approved):
    from iacpaas_mcp.agent import run_agent
    backend = MockBackend()
    args = dict(path=PATH, concept_path="Основные жалобы", metaconcept_path="Группа", mark="Изменено",
                confirm_token="MODEL_CANNOT_APPROVE_ITSELF")
    replies = iter([
        {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "update_concept", "arguments": args}}]}},
        {"message": {"role": "assistant", "content": "Готово"}}
    ])
    real_client = httpx.AsyncClient
    def handler(request):
        return httpx.Response(200, json=next(replies))
    monkeypatch.setattr("iacpaas_mcp.agent.httpx.AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    previews = []
    def approve(data):
        previews.append(data)
        return approved
    async def run():
        async with Client(build_server(backend=backend, allow_write=True), elicitation_callback=elicitation_handler(approve)) as client:
            answer = await run_agent(client, "Переименовать", "test", "http://ollama.test", tmp_path / "trace.jsonl", approve=approve, mode="raw")
            assert answer == ("Изменение выполнено." if approved else "Изменение отменено пользователем.")
        graph = await backend.export(PATH)
        assert select(graph, "Изменено" if approved else "Основные жалобы")
    asyncio.run(run())
    assert len(previews) == 1
    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "confirm_token" not in trace
    assert "MODEL_CANNOT_APPROVE_ITSELF" not in trace


def test_http_transport_auth():
    import socket
    import subprocess
    import time
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = "test-local-token"
    env = {**os.environ, "IACPAAS_BACKEND": "mock", "IACPAAS_ALLOW_WRITE": "false", "MCP_BEARER_TOKEN": token}
    process = subprocess.Popen([sys.executable, "-m", "iacpaas_mcp.server", "--transport", "streamable-http", "--port", str(port)],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               cwd=Path(__file__).resolve().parent.parent)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("HTTP MCP server exited")
            try:
                response = httpx.get(url, timeout=.2)
                break
            except httpx.TransportError:
                time.sleep(.1)
        else:
            pytest.fail("HTTP server did not start")
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers
        assert httpx.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
        async def run():
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}) as http:
                async with Client(streamable_http_client(url, http_client=http)) as client:
                    assert (await client.call_tool("count_graph", {"path": PATH})).structured_content["nodes"] == 8
        asyncio.run(run())
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
