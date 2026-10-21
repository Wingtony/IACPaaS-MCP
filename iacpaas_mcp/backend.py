import copy
import json
import os
from pathlib import Path

import httpx

from .graph import clip, select
from .provenance import capture
from .credentials import load_account

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "observations.json"


class MockBackend:
    """Changes exist only during this server process."""
    def __init__(self):
        self.resources = {"Учебные/База наблюдений": json.loads(FIXTURE.read_text(encoding="utf-8"))}

    async def export(self, path, depth=None):
        if path not in self.resources:
            raise ValueError("ИР не найден. Получите пути через list_inforesources")
        graph = self.resources[path]
        return capture(copy.deepcopy(graph) if depth is None else clip(graph, depth), path, "mock", depth)

    async def update(self, args):
        graph = await self.export(args["path"])
        node = select(graph, args["concept_path"])
        node["value" if node.get("type") == "ТЕРМИНАЛ-ЗНАЧЕНИЕ" else "name"] = args["mark"]
        if args.get("comment"):
            node["comment"] = args["comment"]
        self.resources[args["path"]] = graph
        return {"success": True}

    async def import_resource(self, args):
        resource = json.loads(args["ir_json"])
        path = "/".join(filter(None, [args["folder_path"].strip("/"), resource["title"]]))
        if path in self.resources:
            raise ValueError("Учебный режим импортирует только новые ИР; выберите другое title")
        self.resources[path] = resource
        return {"success": True, "path": path}


class HttpBackend:
    def __init__(self, transport=None):
        self.base = os.getenv("IACPAAS_BASE_URL", "https://iacpaas.dvo.ru").rstrip("/")
        self.headers = {}
        if key := os.getenv("IACPAAS_API_KEY"):
            self.headers["X-API-KEY"] = key
        elif jwt := os.getenv("IACPAAS_JWT"):
            self.headers["Authorization"] = "Bearer " + jwt
        elif saved_token := load_account().get("token"):
            self.headers["Authorization"] = "Bearer " + saved_token
        else:
            raise ValueError("Для HTTP backend задайте IACPAAS_API_KEY или IACPAAS_JWT в .env")
        self.transport = transport

    async def request(self, method, endpoint, max_bytes=8_000_000, **kwargs):
        try:
            async with httpx.AsyncClient(base_url=self.base, headers=self.headers, timeout=60,
                                         transport=self.transport, follow_redirects=False) as client:
                async with client.stream(method, endpoint, **kwargs) as response:
                    if response.status_code >= 400:
                        raise ValueError(f"IACPaaS: HTTP {response.status_code}; проверьте доступ и параметры")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise ValueError(f"Экспорт превышает лимит {max_bytes // 1_000_000} МБ")
            result = json.loads(data)
        except httpx.HTTPError:
            raise ValueError("IACPaaS недоступна или истёк таймаут 60 с") from None
        if isinstance(result, dict) and (result.get("success") is False or result.get("platformUnavailable") or result.get("error")):
            raise ValueError("IACPaaS отклонила запрос; проверьте путь, права и состояние платформы")
        return result

    async def export(self, path, depth=None):
        params = {"path": path, "json-type": "universal", "no-blob-data": "true"}
        if depth is not None:
            params["export-depth"] = depth
        result = await self.request("GET", "/api/data/export/user-item", params=params,
                                    max_bytes=128_000_000 if depth is None else 8_000_000)
        if isinstance(result, dict) and "result" in result:
            result = result["result"]
        elif isinstance(result, dict) and isinstance(result.get("data"), (str, dict)):
            # The live export endpoint returns {filename, code, data: "{...}"}.
            result = result["data"]
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict) or "type" not in result:
            raise ValueError("Неизвестный формат экспорта; нужна проверка ответа живой платформы")
        return capture(result, path, "http", depth)

    async def update(self, args):
        return await self.request("PUT", "/api/data/update", json=args)

    async def import_resource(self, args):
        return await self.request("POST", "/api/data/import", json={
            "path": args["folder_path"], "json": args["ir_json"], "clearIfExists": args["clear_if_exists"]})
