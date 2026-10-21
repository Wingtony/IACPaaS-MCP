"""Cloud planner adapters. Keys stay on the server; endpoints are fixed."""
import copy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re

import httpx

from .credentials import protect

SETTINGS_FILE = Path(__file__).resolve().parents[1] / ".local" / "llm.dpapi"
PROVIDERS = {
    "ollama": {"name": "Локальная Qwen · Ollama", "models": ["qwen-agent"], "env": "", "url": ""},
    "openai": {"name": "OpenAI", "models": ["gpt-4.1-mini", "gpt-4.1"], "env": "OPENAI_API_KEY", "url": "https://api.openai.com/v1"},
    "anthropic": {"name": "Anthropic · Claude", "models": ["claude-haiku-4-5", "claude-sonnet-4-5"], "env": "ANTHROPIC_API_KEY", "url": "https://api.anthropic.com/v1"},
    "gemini": {"name": "Google · Gemini", "models": ["gemini-3.8-flash", "gemini-2.5-flash"], "env": "GEMINI_API_KEY", "url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "deepseek": {"name": "DeepSeek", "models": ["deepseek-flash"], "env": "DEEPSEEK_API_KEY", "url": "https://api.deepseek.com"},
    "openrouter": {"name": "OpenRouter", "models": ["openai/gpt-4.1-mini", "anthropic/claude-haiku-4.5"], "env": "OPENROUTER_API_KEY", "url": "https://openrouter.ai/api/v1"},
}


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    api_key: str = field(default="", repr=False)

    @property
    def cloud(self):
        return self.provider != "ollama"


def load_settings():
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(protect(SETTINGS_FILE.read_bytes(), decrypt=True).decode("utf-8"))
    except (OSError, ValueError):
        raise ValueError("Не удалось прочитать настройки моделей в хранилище Windows") from None


def validate_choice(provider, model):
    if provider not in PROVIDERS:
        raise ValueError("Неизвестный провайдер модели")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,149}", model):
        raise ValueError("Укажите корректный API ID модели, до 150 символов")


def get_config(default_model=None, provider=None, model=None):
    settings = load_settings()
    selected = provider or settings.get("provider") or os.getenv("LLM_PROVIDER", "ollama")
    if selected not in PROVIDERS:
        raise ValueError("Неизвестный провайдер модели")
    chosen = model or settings.get("models", {}).get(selected) or (
        default_model or os.getenv("OLLAMA_MODEL", "qwen-agent") if selected == "ollama" else PROVIDERS[selected]["models"][0])
    validate_choice(selected, chosen)
    key = os.getenv(PROVIDERS[selected]["env"], "") or settings.get("keys", {}).get(selected, "")
    return ModelConfig(selected, chosen, key)


def public_settings(default_model=None):
    settings = load_settings()
    selected = get_config(default_model)
    return {"provider": selected.provider, "model": selected.model, "cloud": selected.cloud,
            "providers": [{"id": key, "name": value["name"], "models": value["models"],
                           "saved_model": settings.get("models", {}).get(key, ""),
                           "key_source": "environment" if value["env"] and os.getenv(value["env"]) else "saved" if settings.get("keys", {}).get(key) else "none",
                           "key_saved": bool(os.getenv(value["env"], "") or settings.get("keys", {}).get(key))}
                          for key, value in PROVIDERS.items()]}


def save_settings(data):
    if not isinstance(data, dict) or set(data) - {"provider", "model", "api_key", "delete_key"}:
        raise ValueError("Некорректные настройки модели")
    provider, model = data.get("provider"), data.get("model")
    validate_choice(provider, model)
    key = data.get("api_key", "")
    if not isinstance(key, str) or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValueError("API-ключ не должен содержать пробелы; максимум 4096 символов")
    if type(data.get("delete_key", False)) is not bool:
        raise ValueError("Некорректный флаг удаления ключа")
    if key and data.get("delete_key"):
        raise ValueError("Нельзя одновременно заменить и удалить ключ")
    settings = load_settings()
    settings["provider"] = provider
    settings.setdefault("models", {})[provider] = model
    if data.get("delete_key"):
        settings.setdefault("keys", {}).pop(provider, None)
    elif key and provider != "ollama":
        settings.setdefault("keys", {})[provider] = key
    encrypted = protect(json.dumps(settings, ensure_ascii=False).encode("utf-8"))
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_FILE.with_suffix(".tmp")
    temp.write_bytes(encrypted)
    temp.replace(SETTINGS_FILE)


def wire_schema(schema):
    """Portable strict subset; original Pydantic constraints still validate locally."""
    result = copy.deepcopy(schema)
    def visit(node):
        if isinstance(node, list):
            for item in node: visit(item)
        elif isinstance(node, dict):
            for key in ("default", "title", "maximum", "minimum", "maxLength", "minLength", "maxItems", "minItems"):
                node.pop(key, None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for child in node.values(): visit(child)
    visit(result)
    return result


async def complete(config, messages, schema, output_limit):
    """Normalize cloud responses into the planner's existing Ollama-shaped envelope."""
    validate_choice(config.provider, config.model)
    if not config.cloud or not config.api_key:
        raise ValueError("Для облачной модели сохраните API-ключ в настройках моделей")
    base = PROVIDERS[config.provider]["url"]
    schema = wire_schema(schema)
    headers = {"Authorization": "Bearer " + config.api_key}
    if config.provider == "openai":
        endpoint = "/responses"
        body = {"model": config.model, "input": messages, "store": False, "max_output_tokens": output_limit,
                "text": {"format": {"type": "json_schema", "name": "knowledge_plan", "strict": True, "schema": schema}}}
    elif config.provider == "anthropic":
        endpoint = "/messages"
        headers = {"x-api-key": config.api_key, "anthropic-version": "2023-06-01"}
        # Messages API has a single top-level system prompt. Compaction memory is
        # retained there; JSON repair guidance is retained as well.
        body = {"model": config.model, "max_tokens": output_limit,
                "system": "\n\n".join(m["content"] for m in messages if m["role"] == "system"),
                "messages": [m for m in messages if m["role"] != "system"],
                "output_config": {"format": {"type": "json_schema", "schema": schema}}}
    else:
        endpoint = "/chat/completions"
        # JSON mode is common to DeepSeek/OpenRouter. Include schema in the
        # prompt too; never silently retry without validation or change provider.
        prompt = {"role": "system", "content": "Return JSON matching this schema: " + json.dumps(schema)}
        body = {"model": config.model, "messages": [prompt, *messages], "max_tokens": output_limit,
                "response_format": {"type": "json_object"}}
        if config.provider == "gemini":
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "knowledge_plan", "schema": schema}}
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            response = await client.post(base + endpoint, headers=headers, json=body)
    except httpx.HTTPError:
        raise ValueError("Облачный API недоступен или истёк таймаут. Повторите запрос позже.") from None
    if response.status_code != 200:
        reason = {401: "Неверный API-ключ", 403: "Нет доступа к модели или региону", 404: "Модель или API не найдены",
                  429: "Исчерпан лимит запросов или баланс"}.get(response.status_code, "Запрос отклонён провайдером")
        # Do not expose error bodies, URLs, headers or SDK exceptions with keys.
        raise ValueError(f"{PROVIDERS[config.provider]['name']}: {reason} (HTTP {response.status_code})")
    try:
        data = response.json()
        usage = data.get("usage", {})
        if config.provider == "openai":
            if data.get("status") not in (None, "completed"):
                raise ValueError()
            blocks = [c for item in data.get("output", []) if item.get("type") == "message" for c in item.get("content", [])]
            if any(c.get("type") == "refusal" for c in blocks): raise ValueError()
            content = "".join(c["text"] for c in blocks if c.get("type") == "output_text")
        elif config.provider == "anthropic":
            if data.get("stop_reason") in {"max_tokens", "refusal"}: raise ValueError()
            content = "".join(c["text"] for c in data["content"] if c.get("type") == "text")
        else:
            choice = data["choices"][0]
            if choice.get("finish_reason") not in (None, "stop"): raise ValueError()
            content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip() or len(content) > 64000: raise ValueError()
        return {"message": {"role": "assistant", "content": content},
                "prompt_eval_count": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                "eval_count": usage.get("output_tokens", usage.get("completion_tokens", 0))}
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        raise ValueError("Облачная модель вернула отказ, неполный или некорректный ответ") from None
