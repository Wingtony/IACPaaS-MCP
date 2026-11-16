"""Docker-hosted Atlas and an independently managed Ollama on another machine."""
import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from iacpaas_mcp import llm, paths, credentials
from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.server import build_server
from iacpaas_mcp.web import create_app

PASSWORD='test-only-strong-password'
REAL_HTTP_CLIENT=httpx.AsyncClient


@pytest.fixture
def remote_client(tmp_path,monkeypatch):
    monkeypatch.setenv('ATLAS_ALLOWED_HOSTS','["atlas.lan"]')
    monkeypatch.setenv('ATLAS_PASSWORD',PASSWORD)
    monkeypatch.setenv('ATLAS_DOCKER','true')
    monkeypatch.setenv('ATLAS_MANAGE_OLLAMA','false')
    monkeypatch.setattr(llm,'protect',lambda data,decrypt=False:data[::-1])
    monkeypatch.setattr('iacpaas_mcp.web.httpx.AsyncClient',lambda **kw:REAL_HTTP_CLIENT(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'models':[]})),**kw))
    app=create_app(tmp_path/'history.sqlite3',server=build_server(backend=MockBackend()))
    with TestClient(app,base_url='http://atlas.lan') as client:
        yield client


def test_remote_ui_requires_password_and_keeps_host_origin_csrf_guards(remote_client):
    client=remote_client
    assert client.get('/').status_code==401
    assert client.get('/api/resources').status_code==401
    assert client.get('/api/health').status_code==200
    assert client.get('/',auth=('atlas','wrong')).status_code==401
    client.auth=('atlas',PASSWORD)
    assert client.get('/').status_code==200
    assert client.get('/',headers={'host':'evil.example'}).status_code==403
    assert client.post('/api/chats',json={}).status_code==403
    csrf=client.get('/api/status').json()['csrf']
    assert client.post('/api/chats',json={},headers={'x-csrf-token':csrf,'origin':'http://evil.example'}).status_code==403
    assert client.post('/api/chats',json={},headers={'x-csrf-token':csrf,'origin':'http://atlas.lan'}).status_code==201
    assert client.post('/api/app/stop',json={},headers={'x-csrf-token':csrf}).status_code==409


def test_allowed_hosts_are_site_addresses_not_client_ip_acl(remote_client):
    with TestClient(remote_client.app,base_url='http://atlas.lan',client=('10.0.0.5',60000)) as visitor:
        assert visitor.get('/').status_code==401
        assert visitor.get('/',auth=('atlas',PASSWORD)).status_code==200
        assert visitor.get('/',auth=('atlas',PASSWORD),headers={'Host':'10.0.0.5'}).status_code==403


def test_external_ollama_url_is_saved_and_probed_from_server(remote_client,monkeypatch):
    client=remote_client;client.auth=('atlas',PASSWORD)
    csrf=client.get('/api/status').json()['csrf'];headers={'x-csrf-token':csrf}
    response=client.post('/api/llm',headers=headers,json={'provider':'ollama','model':'qwen3:8b','ollama_url':'http://192.168.1.50:11434/'})
    assert response.status_code==200 and response.json()['ollama_url']=='http://192.168.1.50:11434'
    seen=[];original=REAL_HTTP_CLIENT
    def reply(request):
        seen.append(str(request.url));return httpx.Response(200,json={'models':[{'name':'qwen3:8b'},{'name':'another:latest'}]})
    monkeypatch.setattr('iacpaas_mcp.web.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(reply),**kw))
    checked=client.post('/api/llm/check',headers=headers,json={})
    assert checked.status_code==200 and checked.json()['available']
    assert checked.json()['models']==['qwen3:8b','another:latest']
    status=client.get('/api/status').json()
    assert status['model_available'] and status['docker']
    assert seen==['http://192.168.1.50:11434/api/tags']*2
    # A new get_config (and app restart) uses persisted settings, not container localhost.
    assert llm.get_config().ollama_url=='http://192.168.1.50:11434'


def test_agent_uses_saved_endpoint_without_reloading_model_after_answer(tmp_path,monkeypatch):
    from iacpaas_mcp.agent import run_agent
    seen=[]
    async def agent(*args): seen.append(args[3]);return 'ok'
    async def forbidden(*args):pytest.fail('Remote Ollama must remain resident')
    monkeypatch.setattr('iacpaas_mcp.adaptive_agent.run_adaptive_agent',agent)
    monkeypatch.setattr('iacpaas_mcp.agent.unload_model',forbidden)
    monkeypatch.setenv('AGENT_UNLOAD_AFTER_REQUEST','false')
    config=llm.ModelConfig('ollama','qwen3:8b',endpoint='http://192.168.1.50:11434')
    assert asyncio.run(run_agent(None,'test','unused','http://localhost:11434',tmp_path/'trace',llm_config=config))=='ok'
    assert seen==['http://192.168.1.50:11434']


@pytest.mark.parametrize('url',['file:///etc/passwd','http://user:password@host:11434','http://host:11434/api/chat','http://host?x=1','http://host#x','http://host:99999','http://host/../api',''])
def test_invalid_ollama_addresses_are_rejected(url):
    with pytest.raises(ValueError):llm.validate_ollama_url(url)


def test_remote_access_cannot_start_without_strong_password(tmp_path,monkeypatch):
    monkeypatch.setenv('ATLAS_ALLOWED_HOSTS','["192.168.1.10"]')
    with pytest.raises(ValueError,match='ATLAS_PASSWORD'):
        create_app(tmp_path/'history.sqlite3',server=build_server(backend=MockBackend()))


def test_runtime_config_does_not_override_docker_security_settings(tmp_path,monkeypatch):
    env=tmp_path/'runtime.env'
    env.write_text('ATLAS_PASSWORD=\nATLAS_ALLOWED_HOSTS=["localhost"]\nIACPAAS_BACKEND=http\nIACPAAS_KNOWN_IRS=["Моя база"]\n',encoding='utf-8')
    monkeypatch.setattr(paths,'ENV_FILE',env)
    monkeypatch.setenv('ATLAS_DOCKER','true');monkeypatch.setenv('ATLAS_PASSWORD',PASSWORD)
    monkeypatch.setenv('ATLAS_ALLOWED_HOSTS','["atlas.lan"]')
    monkeypatch.setenv('IACPAAS_BACKEND','mock');monkeypatch.setenv('IACPAAS_KNOWN_IRS','[]')
    paths.load_environment(override=True)
    assert os.environ['ATLAS_PASSWORD']==PASSWORD and os.environ['ATLAS_ALLOWED_HOSTS']=='["atlas.lan"]'
    assert os.environ['IACPAAS_BACKEND']=='http' and json.loads(os.environ['IACPAAS_KNOWN_IRS'])==['Моя база']


@pytest.mark.skipif(os.name=='nt',reason='POSIX permissions are verified in Linux/macOS CI')
def test_posix_key_permissions_and_roundtrip(tmp_path,monkeypatch):
    monkeypatch.setattr(credentials,'KEY_FILE',tmp_path/'state'/'key')
    encrypted=credentials.protect(b'not-a-real-password')
    assert b'not-a-real-password' not in encrypted
    assert credentials.protect(encrypted,decrypt=True)==b'not-a-real-password'
    assert credentials.KEY_FILE.stat().st_mode&0o777==0o600
    credentials.KEY_FILE.chmod(0o644)
    with pytest.raises(ValueError):credentials.protect(encrypted,decrypt=True)
