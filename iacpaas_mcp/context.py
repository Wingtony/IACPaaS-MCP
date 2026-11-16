"""Deterministic application-side context packing; original data stays on disk."""
import copy
import json
import os


def execution_limits(max_steps=None):
    steps=int(os.getenv('AGENT_MAX_STEPS','12')) if max_steps is None else max_steps
    stagnant=int(os.getenv('AGENT_MAX_STAGNANT','3'))
    if type(steps) is not int or not 1<=steps<=60:raise ValueError('AGENT_MAX_STEPS должен быть от 1 до 60')
    if not 1<=stagnant<=5:raise ValueError('AGENT_MAX_STAGNANT должен быть от 1 до 5')
    return steps,stagnant


def context_settings():
    """Per-request configuration shared by Ollama and application packing."""
    context = int(os.getenv("AGENT_CONTEXT_LENGTH", "32768"))
    output = int(os.getenv("OLLAMA_NUM_PREDICT", "2048"))
    budget = int(os.getenv("AGENT_CONTEXT_BUDGET", str(context - max(2048, context // 4))))
    if not 4096 <= context <= 262144:
        raise ValueError("AGENT_CONTEXT_LENGTH должен быть от 4096 до 262144")
    if not 128 <= output <= context // 2:
        raise ValueError("OLLAMA_NUM_PREDICT должен быть от 128 до половины контекста")
    if not 1024 <= budget <= context - output - 512:
        raise ValueError("AGENT_CONTEXT_BUDGET должен оставлять место для ответа и запас 512 токенов")
    return context, budget, output


def estimate_tokens(value):
    # Conservative operational estimate, NOT the Qwen tokenizer.
    return (len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1) // 2 + 64


def compact_schema(value):
    if isinstance(value, list):
        return [compact_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: compact_schema(item) for key, item in value.items() if key not in {"title", "default"}}


def pack_context(messages, tools, source_index, budget=5600):
    """Keep system, current question, whole tool-call groups and explicit source IDs.

    Old questions are extractively shortened, never converted into alleged facts.
    Tool payloads may be replaced by exact retrieval handles from the evidence store.
    """
    original = estimate_tokens({"messages": messages, "tools": tools})
    if original <= budget:
        return messages, {"compressed": False, "before_estimate": original, "after_estimate": original,
                          "budget": budget, "estimation": "utf8_bytes/2", "dropped_messages": 0}
    system = copy.deepcopy(messages[0])
    rest = copy.deepcopy(messages[1:])
    for message in rest:
        if message.get("role") == "tool" and len(message.get("content", "")) > 900:
            try:
                data = json.loads(message["content"])
            except ValueError:
                continue
            if data.get("evidence_id"):
                message["content"] = json.dumps({"evidence_id": data["evidence_id"],
                    "notice": "Полные данные сохранены приложением. Используй read_evidence для точного поля.",
                    "fields": data.get("fields", [])[:8]}, ensure_ascii=False)
    # A group starts with a user/assistant message and contains all responses to its tool calls.
    groups = []
    for message in rest:
        if message["role"] == "tool" and groups and groups[-1][0].get("tool_calls"):
            groups[-1].append(message)
        else:
            groups.append([message])
    # Preserve the current user request and all its tool messages until older turns are removed.
    latest_user = max((i for i, group in enumerate(groups) if group[0]["role"] == "user"), default=0)
    dropped = []

    def assemble():
        memory = []
        if dropped:
            questions = [item["content"][:160] for item in dropped if item["role"] == "user"][-6:]
            memory.append("Сжатая история: предыдущие вопросы пользователя (цитаты, НЕ доказательства): " + json.dumps(questions, ensure_ascii=False))
        if source_index:
            memory.append("Сохранённые источники текущего запроса, доступны через read_evidence: " +
                          json.dumps([{k: v for k, v in item.items() if k in {"id", "tool"}} for item in source_index[-3:]], ensure_ascii=False))
        return [system] + ([{"role": "system", "content": "\n".join(memory)}] if memory else []) + [item for group in groups for item in group]

    packed = assemble()
    while estimate_tokens({"messages": packed, "tools": tools}) > budget and groups:
        # Drop old turns first; then completed reasoning/tool groups within this request.
        if latest_user > 0:
            dropped.extend(groups.pop(0))
            latest_user -= 1
        else:
            removable = next((i for i, group in enumerate(groups[:-1]) if group[0]["role"] == "assistant"), None)
            if removable is None:
                break
            dropped.extend(groups.pop(removable))
        packed = assemble()
    # A single model turn can contain several tool results. Keep that protocol group,
    # reducing its projections further before declaring the current question too big.
    for field_limit in (4, 2, 1):
        if estimate_tokens({"messages": packed, "tools": tools}) <= budget:
            break
        for group in groups:
            for message in group:
                if message.get("role") == "tool":
                    try:
                        data = json.loads(message["content"])
                    except ValueError:
                        continue
                    if data.get("evidence_id"):
                        message["content"] = json.dumps({"evidence_id":data["evidence_id"], "fields":data.get("fields",[])[:field_limit]}, ensure_ascii=False)
        packed = assemble()
    after = estimate_tokens({"messages": packed, "tools": tools})
    if after > budget:
        raise ValueError("Даже после сжатия вопрос и схемы превышают бюджет контекста. Сократите текущий вопрос.")
    return packed, {"compressed": True, "before_estimate": original, "after_estimate": after,
                    "budget": budget, "estimation": "utf8_bytes/2", "dropped_messages": len(dropped),
                    "source_count": len(source_index)}
