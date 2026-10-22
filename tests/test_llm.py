import asyncio
import json
import time

import httpx
import pytest
from starlette.testclient import TestClient

from iacpaas_mcp import llm
from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.knowledge import KnowledgePlan
from iacpaas_mcp.server import build_server
from iacpaas_mcp.web import create_app


KEY = "test-cloud-credential-never-in-output"


@pytest.fixture
def saved(monkeypatch):
    # Exercise storage and HTTP on all OSes without depending on a real DPAPI key.
    monkeypatch.setattr(llm, "protect", lambda value, decrypt=False: value[::-1])


def test_settings_keep_keys_per_provider_and_never_disclose_them(saved):
    llm.save_settings({"provider":"openai", "model":"gpt-4.1-mini", "api_key":KEY})
    assert KEY.encode() not in llm.SETTINGS_FILE.read_bytes()
    llm.save_settings({"provider":"deepseek", "model":"deepseek-flash", "api_key":"second-test-key"})
    llm.save_settings({"provider":"openai", "model":"gpt-4.1", "api_key":""})
    selected=llm.get_config()
    assert selected.api_key == KEY and selected.model == "gpt-4.1"
    assert KEY not in repr(selected) and KEY not in json.dumps(llm.public_settings())
    llm.save_settings({"provider":"openai", "model":"gpt-4.1", "delete_key":True})
    assert not llm.get_config().api_key
    assert llm.get_config(provider="deepseek").api_key == "second-test-key"


@pytest.mark.parametrize("data", [
    {"provider":"evil", "model":"x"},
    {"provider":"openai", "model":"https://evil.example/?token=key"},
    {"provider":"openai", "model":"x", "api_key":"bad\nkey"},
    {"provider":"openai", "model":"x", "base_url":"https://evil.example"},
    {"provider":"openai", "model":"x", "api_key":"a", "delete_key":True},
])
def test_reject_untrusted_endpoints_and_bad_configuration(data, saved):
    with pytest.raises(ValueError): llm.save_settings(data)
    assert not llm.SETTINGS_FILE.exists()


def test_portable_schema_requires_every_property_without_mutating_original():
    schema=KnowledgePlan.model_json_schema()
    wire=llm.wire_schema(schema)
    assert wire["required"] == list(wire["properties"])
    assert wire["additionalProperties"] is False
    assert "maximum" not in wire["properties"]["limit"]
    assert "maximum" in schema["properties"]["limit"]


@pytest.mark.parametrize("provider,path",[("openai","/v1/responses"),("anthropic","/v1/messages"),
    ("gemini","/v1beta/openai/chat/completions"),("deepseek","/chat/completions"),("openrouter","/api/v1/chat/completions")])
def test_cloud_request_contracts_and_usage(provider,path,monkeypatch):
    plan=json.dumps(KnowledgePlan(operation="resources").model_dump())
    def respond(request):
        assert request.url.path == path and request.url.scheme == "https"
        payload=json.loads(request.content)
        assert KEY not in request.content.decode()
        assert payload["model"] == "example-model"
        assert "keep_alive" not in payload and "options" not in payload
        if provider == "openai":
            assert payload["store"] is False and payload["text"]["format"]["strict"]
            assert request.headers["authorization"] == "Bearer "+KEY
            result={"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":plan}]}],"usage":{"input_tokens":17,"output_tokens":9}}
        elif provider == "anthropic":
            assert request.headers["x-api-key"] == KEY and request.headers["anthropic-version"] == "2023-06-01"
            assert payload["system"] == "system\n\nmemory"
            assert [m["role"] for m in payload["messages"]] == ["user"]
            assert payload["output_config"]["format"]["type"] == "json_schema"
            result={"stop_reason":"end_turn","content":[{"type":"text","text":plan}],"usage":{"input_tokens":17,"output_tokens":9}}
        else:
            assert request.headers["authorization"] == "Bearer "+KEY
            assert payload["response_format"]["type"] == ("json_schema" if provider=="gemini" else "json_object")
            result={"choices":[{"finish_reason":"stop","message":{"content":plan}}],"usage":{"prompt_tokens":17,"completion_tokens":9}}
        return httpx.Response(200,json=result)
    original=httpx.AsyncClient
    monkeypatch.setattr(llm.httpx,"AsyncClient",lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    reply=asyncio.run(llm.complete(llm.ModelConfig(provider,"example-model",KEY),[{"role":"system","content":"system"},{"role":"system","content":"memory"},{"role":"user","content":"question"}],KnowledgePlan.model_json_schema(),2048))
    assert reply["prompt_eval_count"] == 17 and reply["eval_count"] == 9
    assert KnowledgePlan.model_validate_json(reply["message"]["content"]).operation == "resources"


@pytest.mark.parametrize("status",[301,400,401,403,404,429,500])
def test_cloud_errors_hide_provider_body_and_key(status,monkeypatch):
    original=httpx.AsyncClient
    monkeypatch.setattr(llm.httpx,"AsyncClient",lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(status,json={"error":KEY})),**kw))
    with pytest.raises(ValueError) as error:
        asyncio.run(llm.complete(llm.ModelConfig("openai","gpt-4.1-mini",KEY),[],{},2048))
    assert KEY not in str(error.value) and str(status) in str(error.value)


@pytest.mark.parametrize("payload",[
    {"status":"incomplete","output":[]},
    {"status":"completed","output":[{"type":"message","content":[{"type":"refusal","refusal":KEY}]}]},
    {"status":"completed","output":[]},
])
def test_refusal_and_truncation_fail_closed(payload,monkeypatch):
    original=httpx.AsyncClient
    monkeypatch.setattr(llm.httpx,"AsyncClient",lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload)),**kw))
    with pytest.raises(ValueError) as error:
        asyncio.run(llm.complete(llm.ModelConfig("openai","gpt-4.1-mini",KEY),[],{},2048))
    assert KEY not in str(error.value)


def test_web_cloud_settings_csrf_status_and_missing_key(tmp_path,saved,monkeypatch):
    original=httpx.AsyncClient
    def forbidden(request):raise AssertionError("Cloud status must not contact Ollama or a paid API")
    monkeypatch.setattr(llm.httpx,"AsyncClient",lambda **kw:original(transport=httpx.MockTransport(forbidden),**kw))
    with TestClient(create_app(tmp_path/"web.sqlite3",server=build_server(backend=MockBackend())),base_url="http://127.0.0.1") as client:
        # Middleware requires the status-generated token; fake local tags first.
        monkeypatch.setattr(llm.httpx,"AsyncClient",lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"models":[]})),**kw))
        h={"X-CSRF-Token":client.get('/api/status').json()['csrf']}
        data={"provider":"openai","model":"gpt-4.1-mini","api_key":KEY}
        assert client.post('/api/llm',json=data).status_code == 403
        assert client.post('/api/llm',json=data,headers=h).status_code == 200
        monkeypatch.setattr(llm.httpx,"AsyncClient",lambda **kw:original(transport=httpx.MockTransport(forbidden),**kw))
        assert KEY not in client.get('/api/llm').text
        state=client.get('/api/status').json()
        assert state['cloud'] and state['model_available'] and state['provider']=='openai'
        assert client.post('/api/llm',json={**data,"api_key":"","delete_key":True},headers=h).status_code==200
        chat=client.post('/api/chats',json={},headers=h).json()
        assert client.post('/api/chat',json={'chat_id':chat['id'],'message':'Вопрос'},headers=h).status_code==400
        assert not client.get('/api/chats/'+chat['id']).json()['messages']


def test_cloud_start_never_starts_or_prewarms_ollama(monkeypatch):
    from iacpaas_mcp import lifecycle
    monkeypatch.setattr(lifecycle,"get_config",lambda:llm.ModelConfig('openai','gpt-4.1-mini',KEY))
    monkeypatch.setattr(lifecycle,"healthy",lambda:True)
    monkeypatch.setattr(lifecycle.httpx,"post",lambda *a,**k:pytest.fail('Ollama must not be loaded'))
    assert 'облачная' in lifecycle.start_app(open_browser=False)


def test_web_cloud_answer_records_actual_model_without_exposing_key(tmp_path,saved,monkeypatch):
    seen=[]
    def reply(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200,json={"models":[]})
        seen.append(request)
        assert request.url.host == 'api.openai.com'
        decision={'action':'call','call':{'tool':'query_knowledge','arguments':{'plan':{'operation':'resources'}}},'evidence_ids':[],'question':'','sufficient':False,'assessment':'Нужен список баз'} if len(seen)==1 else {'action':'finish','call':None,'evidence_ids':['S1'],'question':'','sufficient':True,'assessment':'Получен список баз'}
        plan=json.dumps(decision)
        return httpx.Response(200,json={"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":plan}]}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(llm.httpx,"AsyncClient",lambda **kw:original(transport=httpx.MockTransport(reply),**kw))
    # Keep all test traces off the user's logs folder too.
    monkeypatch.setattr('iacpaas_mcp.web.PROJECT',tmp_path)
    with TestClient(create_app(tmp_path/'web.sqlite3',server=build_server(backend=MockBackend())),base_url='http://127.0.0.1') as client:
        h={'X-CSRF-Token':client.get('/api/status').json()['csrf']}
        assert client.post('/api/llm',headers=h,json={'provider':'openai','model':'gpt-4.1-mini','api_key':KEY}).status_code==200
        chat=client.post('/api/chats',headers=h,json={}).json()
        run=client.post('/api/chat',headers=h,json={'chat_id':chat['id'],'message':'Какие базы доступны?'}).json()
        for _ in range(100):
            if client.get('/api/runs/'+run['run_id']).json()['done']:break
            time.sleep(.01)
        result=client.get('/api/chats/'+chat['id'])
        answer=result.json()['messages'][-1]
        assert answer['status']=='done' and answer['provider']=='openai' and answer['model']=='gpt-4.1-mini'
        assert len(seen)==2 and answer['steps'][0]['name']=='query_knowledge'
        assert KEY not in result.text
        assert KEY not in ''.join(p.read_text(encoding='utf-8') for p in (tmp_path/'logs').rglob('*.jsonl'))
