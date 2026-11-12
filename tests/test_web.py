import asyncio
import json
import time

import httpx
import pytest
from starlette.testclient import TestClient

from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.server import build_server
from iacpaas_mcp.web import Store, create_app


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("IACPAAS_BACKEND", "mock")
    monkeypatch.setenv("IACPAAS_ALLOW_WRITE", "true")
    real_http = httpx.AsyncClient
    monkeypatch.setattr("iacpaas_mcp.web.httpx.AsyncClient", lambda **kw: real_http(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [{"name": "qwen-agent:latest"}]})), **kw))
    app = create_app(tmp_path / "web.sqlite3", server=build_server(backend=MockBackend(), allow_write=True))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def headers(client):
    return {"x-csrf-token": client.get("/api/status").json()["csrf"]}


def completed_review_chat(client,tmp_path):
    chat=client.post('/api/chats',headers=headers(client),json={}).json()
    chat['messages']=[{'role':'user','content':'Сколько?'},{'role':'assistant','content':'8 вершин','status':'done','steps':[]}]
    Store(tmp_path/'web.sqlite3').save(chat)
    return chat['id']


def test_review_timeout_is_not_saved_as_a_verdict(web_client,tmp_path,monkeypatch):
    chat_id=completed_review_chat(web_client,tmp_path)
    async def timeout(*args):raise httpx.ReadTimeout('test timeout')
    monkeypatch.setattr('iacpaas_mcp.verifier.verify_answer',timeout)
    response=web_client.post('/api/chats/'+chat_id+'/verify',headers=headers(web_client),json={'index':1})
    assert response.status_code==408
    assert 'claim_review' not in web_client.get('/api/chats/'+chat_id).json()['messages'][1]


def test_review_can_be_cancelled_without_saving_partial_result(web_client,tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    started=threading.Event();chat_id=completed_review_chat(web_client,tmp_path)
    async def slow(*args):started.set();await asyncio.sleep(10)
    monkeypatch.setattr('iacpaas_mcp.verifier.verify_answer',slow)
    h=headers(web_client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(web_client.post,'/api/chats/'+chat_id+'/verify',headers=h,json={'index':1})
        assert started.wait(3)
        assert web_client.post('/api/chats/'+chat_id+'/verify/cancel',headers=h,json={}).status_code==200
        assert future.result(timeout=3).status_code==409
    assert 'claim_review' not in web_client.get('/api/chats/'+chat_id).json()['messages'][1]


def test_pages_and_local_security(web_client):
    client = web_client
    page = client.get("/")
    assert page.status_code == 200 and 'id="question"' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert len(client.get("/static/questions.json").json()) >= 31
    assert client.get("/api/status").json()["context_length"] == 32768
    assert client.get("/api/status").json()["tools"] == 16
    assert client.get('/api/status').json()['review_version']==3
    assert client.get('/api/status').json()['dialogue_version']==1
    assert client.get("/api/status", headers={"Host":"evil.example"}).status_code == 403
    assert client.post("/api/chats", json={}).status_code == 403
    assert client.post("/api/chats", json={}, headers={**headers(client), "Origin":"https://evil.example"}).status_code == 403


def test_chat_crud_and_persistence(web_client, tmp_path):
    client = web_client
    h = headers(client)
    chat = client.post("/api/chats", json={}, headers=h).json()
    assert len(client.get("/api/chats").json()) == 1
    assert client.get("/api/chats/"+chat["id"]).json()["messages"] == []
    assert Store(tmp_path / "web.sqlite3").get(chat["id"])["title"] == "Новый диалог"
    assert client.delete("/api/chats/"+chat["id"], headers=h).status_code == 200
    assert client.get("/api/chats/"+chat["id"]).status_code == 404


def test_resources_via_mcp(web_client):
    resources = web_client.get("/api/resources").json()["resources"]
    assert resources[0]["path"] == "Учебные/База наблюдений"
    graph = web_client.get("/api/resource", params={"path":resources[0]["path"]}).json()["graph"]
    assert graph["successors"][0]["name"] == "Основные жалобы"


def wait_done(client, run_id):
    for _ in range(200):
        if client.get("/api/runs/"+run_id).json()["done"]:
            return
        time.sleep(.01)
    pytest.fail("Background run did not finish")


def test_streamed_answer_and_history(web_client, monkeypatch):
    histories = []
    async def fake(client, question, model, url, trace, history, on_event, **kwargs):
        histories.append(history)
        result = (await client.call_tool("count_graph", {"path":"Учебные/База наблюдений"})).structured_content
        await on_event({"event":"tool_start", "name":"count_graph", "arguments":{}})
        await on_event({"event":"tool", "name":"count_graph", "arguments":{}, "result":result, "seconds":.01})
        await on_event({"event":"answer", "answer":"В графе 8 вершин", "tokens_in":100, "tokens_out":20})
        return "В графе 8 вершин"
    monkeypatch.setattr("iacpaas_mcp.web.run_agent", fake)
    client = web_client
    h = headers(client)
    chat = client.post("/api/chats", json={}, headers=h).json()
    for text in ["Сколько вершин?", "А сколько дуг?"]:
        response = client.post("/api/chat", json={"chat_id":chat["id"],"message":text}, headers=h)
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        wait_done(client, run_id)
        stream = client.get(f"/api/runs/{run_id}/events").text
        assert '"event": "tool"' in stream and '"event": "done"' in stream
        assert "confirm_token" not in stream
    saved = client.get("/api/chats/"+chat["id"]).json()
    assert len(saved["messages"]) == 4
    assert saved["messages"][1]["metrics"]["tokens_out"] == 20
    assert histories[0] == [] and len(histories[1]) == 2


@pytest.mark.parametrize("decision", [False,True])
def test_browser_approval(web_client, monkeypatch, decision):
    choices = []
    async def fake(client, question, model, url, trace, history, on_event, approve, **kwargs):
        choices.append(await approve({"operation":"update_concept", "preview":{"mark":"Новое имя"}}))
        return "Подтверждено" if choices[-1] else "Отменено"
    monkeypatch.setattr("iacpaas_mcp.web.run_agent", fake)
    client = web_client
    h = headers(client)
    chat = client.post("/api/chats", json={}, headers=h).json()
    run_id = client.post("/api/chat", json={"chat_id":chat["id"],"message":"Измени"}, headers=h).json()["run_id"]
    for _ in range(100):
        pending = client.get("/api/runs/"+run_id).json()["pending_approval"]
        if pending:
            break
        time.sleep(.01)
    assert pending and "confirm_token" not in json.dumps(pending)
    assert client.delete("/api/chats/"+chat["id"], headers=h).status_code == 409
    assert client.post(f"/api/runs/{run_id}/approve", json={"approval_id":"wrong","approved":True}, headers=h).status_code == 409
    assert client.post(f"/api/runs/{run_id}/approve", json={"approval_id":pending["approval_id"],"approved":decision}, headers=h).status_code == 200
    wait_done(client, run_id)
    assert choices == [decision]


def test_cancel_run(web_client, monkeypatch):
    async def fake(*args, **kwargs):
        await asyncio.sleep(60)
    monkeypatch.setattr("iacpaas_mcp.web.run_agent", fake)
    client=web_client
    h=headers(client)
    chat=client.post("/api/chats",json={},headers=h).json()
    run_id=client.post("/api/chat",json={"chat_id":chat["id"],"message":"Подожди"},headers=h).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/cancel",json={},headers=h).status_code == 200
    wait_done(client,run_id)
    assert client.get("/api/chats/"+chat["id"]).json()["messages"][-1]["status"] == "cancelled"


@pytest.mark.parametrize("body", [{"message":""}, {"message":"x"*4001}, []])
def test_bad_chat_request(web_client,body):
    assert web_client.post("/api/chat",json=body,headers=headers(web_client)).status_code == 400


def test_question_edit_creates_branch_without_erasing_original(web_client, monkeypatch):
    async def fake(*args, **kwargs):
        return "Ответ"
    monkeypatch.setattr("iacpaas_mcp.web.run_agent",fake)
    client=web_client
    chat=client.post("/api/chats",json={},headers=headers(client)).json()
    assert client.post("/api/chats/"+chat["id"]+"/branch",json={"index":0},headers=headers(client)).status_code==400
    for question in ["Первый вопрос", "Второй вопрос"]:
        run=client.post("/api/chat",json={"chat_id":chat["id"],"message":question},headers=headers(client)).json()
        wait_done(client,run["run_id"])
    original=client.get("/api/chats/"+chat["id"]).json()
    branch=client.post("/api/chats/"+chat["id"]+"/branch",json={"index":2},headers=headers(client)).json()
    assert branch["id"]!=chat["id"] and len(branch["messages"])==2
    assert client.get("/api/chats/"+chat["id"]).json()["messages"]==original["messages"]
    assert client.post("/api/chats/"+chat["id"]+"/branch",json={"index":1},headers=headers(client)).status_code==400


def test_shutdown_endpoint_needs_csrf_and_stops_owned_app(tmp_path,monkeypatch):
    stopped=[];released=[]
    async def unload(*args):released.append(args)
    monkeypatch.setattr("iacpaas_mcp.web.unload_model",unload)
    app=create_app(tmp_path/"app.sqlite3",server=build_server(backend=MockBackend()),shutdown_callback=lambda:stopped.append(True))
    with TestClient(app,base_url="http://127.0.0.1") as client:
        assert client.get("/api/health").json()["running"]
        assert client.post("/api/app/stop",json={}).status_code==403
        response=client.post("/api/app/stop",json={},headers=headers(client))
        assert response.json()=={"stopping":True}
        time.sleep(.4)
        assert stopped==[True]
    assert len(released)==1
