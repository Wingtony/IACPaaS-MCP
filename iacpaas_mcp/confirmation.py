"""Client-side human UI bridge for server-initiated MCP confirmations."""
import asyncio
import inspect
import json
from mcp.types import ElicitResult


def elicitation_handler(approve=None):
    async def handle(context, params):
        try:
            preview = json.loads(params.message)
            if not isinstance(preview, dict) or set(preview) != {"operation", "preview"}:
                return ElicitResult(action="decline")
            if approve is None:
                answer = await asyncio.to_thread(input, json.dumps(preview, ensure_ascii=False) + "\nВыполнить? Введите ДА: ")
                accepted = answer == "ДА"
            else:
                accepted = approve(preview)
                if inspect.isawaitable(accepted): accepted = await accepted
            return ElicitResult(action="accept", content={"confirm": True}) if accepted is True else ElicitResult(action="decline")
        except (ValueError, TypeError):
            return ElicitResult(action="decline")
    return handle
