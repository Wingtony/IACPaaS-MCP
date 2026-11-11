"""Tests must never consume the user's cloud credentials or saved selection."""
import pytest


@pytest.fixture(autouse=True)
def isolated_llm_settings(tmp_path, monkeypatch):
    from iacpaas_mcp import llm
    monkeypatch.setattr(llm, "SETTINGS_FILE", tmp_path / "llm.dpapi")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    for provider in llm.PROVIDERS.values():
        if provider["env"]:
            monkeypatch.delenv(provider["env"], raising=False)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    import importlib
    for name in ("paths","auth","server","agent","web","demo","lifecycle"):
        module=importlib.import_module("iacpaas_mcp."+name)
        if hasattr(module,"ENV_FILE"): monkeypatch.setattr(module,"ENV_FILE",tmp_path/".env")
    from iacpaas_mcp import credentials
    monkeypatch.setattr(credentials,"KEY_FILE",tmp_path/"secrets.key")
    monkeypatch.setattr(credentials,"ACCOUNT_FILE",tmp_path/"account.dpapi")
    monkeypatch.setenv("IACPAAS_WRITE_ROOTS", '["Учебные", "Новые", "АТЛАС демо"]')
    for name in ('ATLAS_DOCKER','ATLAS_USERNAME','ATLAS_PASSWORD','ATLAS_ALLOWED_HOSTS','ATLAS_MANAGE_OLLAMA'):
        monkeypatch.delenv(name,raising=False)
