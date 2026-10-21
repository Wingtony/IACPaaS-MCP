import asyncio
import json

import httpx
import pytest

from iacpaas_mcp import credentials
from iacpaas_mcp.auth import connect_account
from iacpaas_mcp.backend import HttpBackend


def test_windows_account_encryption_and_replacement(tmp_path,monkeypatch):
    monkeypatch.setattr(credentials,"ACCOUNT_FILE",tmp_path / "account.dpapi")
    credentials.save_account("test-account","secret-test-password","secret-test-token")
    assert credentials.load_account()["password"] == "secret-test-password"
    encrypted=credentials.ACCOUNT_FILE.read_bytes()
    assert b"secret-test-password" not in encrypted and b"secret-test-token" not in encrypted
    public=credentials.account_status()
    assert "password" not in public and "token" not in public
    assert public["password_saved"] is True
    credentials.save_account("another-account","new-password","new-token")
    assert credentials.load_account()["username"] == "another-account"


def test_connect_does_not_save_plaintext_secrets(tmp_path,monkeypatch):
    monkeypatch.setattr(credentials,"ACCOUNT_FILE",tmp_path / "account.dpapi")
    monkeypatch.setattr("iacpaas_mcp.auth.PROJECT",tmp_path)
    (tmp_path / ".env.example").write_text('IACPAAS_ALLOW_WRITE=true\n',encoding="utf-8")
    monkeypatch.setattr("iacpaas_mcp.auth.signin",lambda username,password,transport=None:"test-secret-jwt")
    connect_account("test-user","test-secret-password",paths=["Folder/Resource"])
    env=(tmp_path / ".env").read_text(encoding="utf-8")
    assert "test-secret-jwt" not in env and "test-secret-password" not in env
    assert "Folder/Resource" in env
    assert credentials.account_status()["connected"] is True
    connect_account("test-user","",paths=["Folder/Other"])
    assert credentials.load_account()["password"] == "test-secret-password"
    with pytest.raises(ValueError):
        connect_account("changed-user","",paths=[])


def test_live_export_data_wrapper(monkeypatch):
    monkeypatch.setenv("IACPAAS_API_KEY","test-key")
    graph={"title":"Real format fixture","name":"Fixture","type":"КОРЕНЬ"}
    backend=HttpBackend(httpx.MockTransport(lambda request:httpx.Response(200,json={
        "filename":"fixture.universal.json","code":123,"data":json.dumps(graph)})))
    result=asyncio.run(backend.export("Folder/Fixture",depth=0))
    assert result["name"] == "Fixture" and result["_source"]["backend"] == "http"


def test_account_endpoint_never_returns_saved_secrets(tmp_path,monkeypatch):
    from starlette.testclient import TestClient
    from iacpaas_mcp.backend import MockBackend
    from iacpaas_mcp.server import build_server
    from iacpaas_mcp.web import create_app
    monkeypatch.setattr(credentials,"ACCOUNT_FILE",tmp_path / "account.dpapi")
    credentials.save_account("test-user","private-password","private-jwt")
    monkeypatch.setenv("IACPAAS_BACKEND","mock")
    app=create_app(tmp_path / "web.sqlite3",server=build_server(backend=MockBackend(),allow_write=False))
    with TestClient(app,base_url="http://localhost") as client:
        response=client.get("/api/account")
        assert response.status_code==200 and response.json()["username"]=="test-user"
        assert "private-password" not in response.text and "private-jwt" not in response.text
        assert "password" not in response.json() and "token" not in response.json()
        assert client.post("/api/account",json={}).status_code==403
