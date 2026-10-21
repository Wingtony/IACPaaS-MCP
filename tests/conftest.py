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
