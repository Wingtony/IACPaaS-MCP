import copy
import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx

from .graph import clip, select
from .provenance import capture
from .credentials import load_account

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "observations.json"


class PlatformError(ValueError):
    def __init__(self,message,code='platform_error',retryable=True,suggestion='Проверьте параметры запроса'):
        super().__init__(message)
        self.code,self.retryable,self.suggestion=code,retryable,suggestion


def token_expired(token):
    # This is only a refresh hint, never authentication or signature validation.
    try:
        raw=token.split('.')[1];payload=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        exp=payload.get('exp')
        return isinstance(exp,(float,int)) and exp<time.time()+30
    except (ValueError,IndexError,TypeError):
        return False


class MockBackend:
    """Changes exist only during this server process."""
    def __init__(self):
        self.folders = set()
        self.resources = {"Учебные/База наблюдений": json.loads(FIXTURE.read_text(encoding="utf-8"))}

    async def export(self, path, depth=None):
        if path not in self.resources:
            raise PlatformError("ИР не найден. Получите пути через list_inforesources", "resource_not_found", False)
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

    async def create_folder(self, args):
        path = "/".join(filter(None, [args["parent_path"], args["name"]]))
        if path in self.folders: raise ValueError("Папка уже существует")
        self.folders.add(path)
        return {"success": True, "path": path}

    async def delete_resource(self, args):
        if args["path"] not in self.resources: raise ValueError("Ресурс не найден")
        del self.resources[args["path"]]
        return {"success": True, "path": args["path"], "mode": args["mode"]}


class HttpBackend:
    def __init__(self, transport=None):
        self.base = os.getenv("IACPAAS_BASE_URL", "https://iacpaas.dvo.ru").rstrip("/")
        self.headers = {}
        self.saved_login=False
        self.saved_identity=None
        self.refresh_lock=asyncio.Lock()
        if key := os.getenv("IACPAAS_API_KEY"):
            self.headers["X-API-KEY"] = key
        elif jwt := os.getenv("IACPAAS_JWT"):
            self.headers["Authorization"] = "Bearer " + jwt
        elif saved_token := load_account().get("token"):
            if self.base!='https://iacpaas.dvo.ru':
                raise ValueError('Сохранённый аккаунт можно использовать только с официальным HTTPS API IACPaaS')
            self.headers["Authorization"] = "Bearer " + saved_token
            self.saved_login=True
            self.saved_identity=load_account().get('username')
        else:
            raise ValueError("Для HTTP backend задайте IACPAAS_API_KEY или IACPAAS_JWT в .env")
        self.transport = transport

    async def request(self, method, endpoint, max_bytes=8_000_000, **kwargs):
        used=self.headers.get('Authorization','')
        if token_expired(used.removeprefix('Bearer ')):
            if self.saved_login:
                await self.refresh_saved(used)
            else:
                raise PlatformError('JWT IACPaaS истёк','authentication_required',False,'Переподключите IACPaaS или обновите IACPAAS_JWT')
        used=self.headers.get('Authorization','')
        try:
            return await self._request_once(method,endpoint,max_bytes,**kwargs)
        except PlatformError as exc:
            # Never automatically replay a mutation: its outcome may be unknown.
            if method=='GET' and self.saved_login and exc.code=='authentication_required':
                await self.refresh_saved(used)
                return await self._request_once(method,endpoint,max_bytes,**kwargs)
            raise

    async def refresh_saved(self, previous):
        async with self.refresh_lock:
            if self.headers.get('Authorization')!=previous:return
            from .auth import signin
            from .credentials import save_account
            saved=load_account()
            try:
                if not saved.get('username') or not saved.get('password') or saved.get('username')!=self.saved_identity:raise ValueError()
                token=await asyncio.to_thread(signin,saved['username'],saved['password'])
                await asyncio.to_thread(save_account,saved['username'],saved['password'],token)
            except (ValueError,OSError,httpx.HTTPError):
                raise PlatformError('Не удалось обновить вход IACPaaS','authentication_required',False,'Переподключите аккаунт в настройках IACPaaS') from None
            self.headers['Authorization']='Bearer '+token

    async def _request_once(self, method, endpoint, max_bytes=8_000_000, **kwargs):
        try:
            async with httpx.AsyncClient(base_url=self.base, headers=self.headers, timeout=60,
                                         transport=self.transport, follow_redirects=False) as client:
                async with client.stream(method, endpoint, **kwargs) as response:
                    if response.status_code >= 400:
                        if response.status_code == 404:
                            raise PlatformError("IACPaaS: ресурс не найден", "resource_not_found", False)
                        if response.status_code >= 500:
                            raise PlatformError("IACPaaS временно недоступна", "platform_unavailable", True)
                        if response.status_code in (401,403):
                            auth=response.status_code==401
                            raise PlatformError(f'IACPaaS: HTTP {response.status_code}; требуется вход или доступ',
                                'authentication_required' if auth else 'permission_denied',False,'Переподключите IACPaaS' if auth else 'Проверьте права аккаунта на ресурс')
                        raise ValueError(f"IACPaaS: HTTP {response.status_code}; проверьте доступ и параметры")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise ValueError(f"Экспорт превышает лимит {max_bytes // 1_000_000} МБ")
            result = json.loads(data)
        except httpx.HTTPError:
            raise PlatformError("IACPaaS недоступна или истёк таймаут 60 с", "platform_unavailable", True) from None
        if isinstance(result, dict) and (result.get("success") is False or result.get("platformUnavailable") or result.get("error")):
            if result.get('platformUnavailable'):
                raise PlatformError('IACPaaS временно недоступна','platform_unavailable',True)
            import re
            details=' '.join(str(result.get(key,'')) for key in ('error','explanation','message'))
            if endpoint == '/api/data/update' and 'updateVertice' in details and 'action' in details:
                raise PlatformError('IACPaaS отклонила внутреннюю операцию обновления вершины',
                    'platform_operation_unavailable',False,
                    'Не повторяйте изменение и не подбирайте другие пути: проверьте состояние через чтение и обратитесь к администратору платформы')
            if re.search(r'jwt|token|expired|авториз|аутенти|ист[её]к',details,re.I):
                raise PlatformError('IACPaaS отклонила авторизацию','authentication_required',False,'Переподключите аккаунт IACPaaS')
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


    async def create_folder(self, args):
        return await self.request("POST", "/api/storage-unit/folder/create", json={
            "path": args["parent_path"], "name": args["name"], "description": args.get("description", "")})

    async def delete_resource(self, args):
        return await self.request("DELETE", "/api/data/delete", json={"path": args["path"], "mode": args["mode"]})
