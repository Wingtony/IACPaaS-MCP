"""Loopback web UI for the existing MCP/Qwen agent."""
import argparse
import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp import Client
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from .agent import run_agent, unload_model
from .context import context_settings
from .server import PROJECT, build_server
from .provenance import SNAPSHOTS
from .auth import connect_account
from .credentials import account_status

STATIC = Path(__file__).resolve().parent / "static"


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL, messages TEXT)")

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,title,created,updated FROM chats ORDER BY updated DESC")]

    def get(self, chat_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row:
            return None
        chat = dict(row)
        chat["messages"] = json.loads(chat["messages"])
        return chat

    def save(self, chat):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO chats VALUES (?,?,?,?,?)", (
                chat["id"], chat["title"], chat["created"], time.time(), json.dumps(chat["messages"], ensure_ascii=False)))

    def delete(self, chat_id):
        with self.connect() as db:
            db.execute("DELETE FROM chats WHERE id=?", (chat_id,))


def create_app(db_path=None, server=None, open_url=None, shutdown_callback=None,instance_id=""):
    load_dotenv(PROJECT / ".env")
    store = Store(db_path or PROJECT / "logs" / "web.sqlite3")
    mcp = server or build_server()
    csrf = secrets.token_urlsafe(32)
    runs = {}
    lock = asyncio.Lock()
    model = os.getenv("OLLAMA_MODEL", "qwen-agent")
    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11435")

    async def publish(run, event):
        event = {**event, "at": time.time()}
        # No model capabilities / confirmation tokens in browser events.
        run["events"].append(event)
        run["changed"].set()

    async def execute(run):
        chat = store.get(run["chat_id"])
        started = time.monotonic()
        steps = []
        metrics = {}
        verification = {}
        context = {}

        async def event(data):
            if data["event"] == "tool":
                steps.append(data)
            if data["event"] == "answer":
                metrics.update({k: data[k] for k in ("tokens_in", "tokens_out")})
            if data["event"] == "verification":
                verification.update(data)
            if data["event"] == "context":
                context.update(data)
            await publish(run, data)

        async def approve(data):
            approval_id = uuid.uuid4().hex
            run["approval_id"] = approval_id
            run["approval"] = asyncio.get_running_loop().create_future()
            await publish(run, {"event": "approval", "approval_id": approval_id,
                                "preview": data["preview"], "operation": data["operation"]})
            try:
                return await asyncio.wait_for(run["approval"], timeout=240)
            except asyncio.TimeoutError:
                await publish(run, {"event": "approval_expired"})
                return False
            finally:
                run["approval_id"] = None

        try:
            await publish(run, {"event": "queued"})
            async with lock:
                await publish(run, {"event": "started"})
                history = [{"role": message["role"], "content": message["content"], "query_plan":message.get("verification",{}).get("query_plan"),"selection":message.get("verification",{}).get("selection",[])}
                           for message in chat["messages"][:-1] if message.get("status", "done") == "done"]
                async with Client(mcp) as client:
                    answer = await run_agent(client, chat["messages"][-1]["content"], model, ollama_url,
                                             PROJECT / "logs" / ("web-" + run["id"] + ".jsonl"),
                                             history=history, on_event=event, approve=approve,
                                             resource_path=run.get("resource_path", ""))
            message = {"role": "assistant", "content": answer, "status": "done"}
        except asyncio.CancelledError:
            message = {"role": "assistant", "content": "Генерация остановлена.", "status": "cancelled"}
            await publish(run, {"event": "cancelled"})
        except Exception as exc:
            logging.getLogger(__name__).exception("Web agent failed")
            while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                exc = exc.exceptions[0]
            if isinstance(exc, httpx.HTTPError):
                text = "Не удалось получить ответ Qwen. Проверьте, что Ollama запущен и модель доступна."
            elif isinstance(exc, (RuntimeError, ValueError)):
                text = str(exc)
            else:
                text = "Агент завершился с ошибкой. Проверьте подключение MCP и журнал сервера."
            message = {"role": "assistant", "content": text, "status": "error"}
            await publish(run, {"event": "error", "message": text})
        finally:
            message = locals().get("message", {"role": "assistant", "content": "Запрос прерван.", "status": "error"})
            message.update({"steps": steps, "metrics": metrics, "verification": verification, "context":context,
                            "seconds": round(time.monotonic() - started, 1), "model": model})
            chat["messages"].append(message)
            store.save(chat)
            run["done"] = True
            await publish(run, {"event": "done", "message": message})

    async def index(request):
        return FileResponse(STATIC / "index.html")

    async def health(request):
        return JSONResponse({"running":True,"application":"iacpaas-atlas","pid":os.getpid(),"instance_id":instance_id})

    async def stop_application(request):
        if shutdown_callback is None:
            return JSONResponse({"error":"Остановка доступна при штатном запуске приложения"},status_code=409)
        for run in runs.values():
            if not run["done"]:run["task"].cancel()
        asyncio.get_running_loop().call_later(.3,shutdown_callback)
        return JSONResponse({"stopping":True})

    async def status(request):
        online = False
        model_available = False
        try:
            async with httpx.AsyncClient(timeout=2) as http:
                response = await http.get(ollama_url.rstrip("/") + "/api/tags")
                response.raise_for_status()
                names = [item["name"] for item in response.json().get("models", [])]
                online = True
                model_available = any(name == model or name.removesuffix(":latest") == model for name in names)
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        async with Client(mcp) as client:
            tools = (await client.list_tools()).tools
        window,input_budget,output_limit=context_settings()
        return JSONResponse({"context_length":window,"context_budget":input_budget,"output_limit":output_limit,"csrf": csrf, "model": model, "online": online, "model_available": model_available,
                             "backend": os.getenv("IACPAAS_BACKEND", "mock"), "tools": len(tools),
                             "write_enabled": os.getenv("IACPAAS_ALLOW_WRITE", "false").lower() == "true"})

    async def chats(request):
        if request.method == "GET":
            return JSONResponse(store.list())
        chat = {"id": uuid.uuid4().hex, "title": "Новый диалог", "created": time.time(), "messages": []}
        store.save(chat)
        return JSONResponse(chat, status_code=201)

    def active(chat_id):
        return next((run for run in runs.values() if run["chat_id"] == chat_id and not run["done"]), None)

    async def chat_item(request):
        chat_id = request.path_params["chat_id"]
        chat = store.get(chat_id)
        if not chat:
            return JSONResponse({"error": "Диалог не найден"}, status_code=404)
        if request.method == "DELETE":
            if active(chat_id):
                return JSONResponse({"error": "Сначала остановите ответ агента"}, status_code=409)
            store.delete(chat_id)
            return JSONResponse({"ok": True})
        current = active(chat_id)
        return JSONResponse({**chat, "run_id": current["id"] if current else None})

    async def send(request):
        try:
            data = await request.json()
        except ValueError:
            return JSONResponse({"error": "Некорректный JSON"}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse({"error": "Ожидается объект запроса"}, status_code=400)
        question = data.get("message", "")
        if not isinstance(data.get("resource_path", ""), str):
            return JSONResponse({"error":"Путь базы должен быть строкой"}, status_code=400)
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 4000:
            return JSONResponse({"error": "Введите вопрос длиной до 4000 символов"}, status_code=400)
        chat = store.get(data.get("chat_id", ""))
        if not chat:
            return JSONResponse({"error": "Диалог не найден"}, status_code=404)
        if active(chat["id"]):
            return JSONResponse({"error": "В этом диалоге уже выполняется запрос"}, status_code=409)
        if sum(not run["done"] for run in runs.values()) >= 4:
            return JSONResponse({"error": "Очередь заполнена. Дождитесь завершения запросов"}, status_code=429)
        # Bound transient event memory while preserving durable conversations.
        for old_id in list(runs):
            if runs[old_id]["done"] and len(runs) >= 30:
                del runs[old_id]
        chat["messages"].append({"role": "user", "content": question.strip(), "status": "done"})
        if len(chat["messages"]) == 1:
            chat["title"] = question.strip()[:65]
        store.save(chat)
        run_id = uuid.uuid4().hex
        run = {"id": run_id, "chat_id": chat["id"], "events": [], "changed": asyncio.Event(), "done": False,
               "approval_id": None, "resource_path":data.get("resource_path", "")}
        runs[run_id] = run
        run["task"] = asyncio.create_task(execute(run))
        return JSONResponse({"run_id": run_id}, status_code=202)

    async def branch_chat(request):
        chat=store.get(request.path_params["chat_id"])
        if not chat:
            return JSONResponse({"error":"Диалог не найден"},status_code=404)
        if active(chat["id"]):
            return JSONResponse({"error":"Сначала остановите ответ"},status_code=409)
        try:
            index=(await request.json())["index"]
            if type(index) is not int or index<0 or index>=len(chat["messages"]) or chat["messages"][index]["role"]!="user":
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            return JSONResponse({"error":"Выберите вопрос пользователя"},status_code=400)
        branch={"id":uuid.uuid4().hex,"title":chat["title"]+" · правка","created":time.time(),
                "messages":json.loads(json.dumps(chat["messages"][:index])),"parent_id":chat["id"]}
        store.save(branch)
        return JSONResponse(branch,status_code=201)

    async def events(request):
        run = runs.get(request.path_params["run_id"])
        if not run:
            return JSONResponse({"error": "Запрос не найден"}, status_code=404)
        try:
            start = max(0, int(request.headers.get("last-event-id", "-1")) + 1)
        except ValueError:
            start = 0
        async def stream():
            position = start
            while True:
                run["changed"].clear()
                while position < len(run["events"]):
                    event = run["events"][position]
                    yield f"id: {position}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    position += 1
                if run["done"]:
                    return
                try:
                    await asyncio.wait_for(run["changed"].wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def control(request):
        if request.path_params["action"] not in {"approve", "cancel"}:
            return JSONResponse({"error": "Действие не найдено"}, status_code=404)
        run = runs.get(request.path_params["run_id"])
        if not run or run["done"]:
            return JSONResponse({"error": "Запрос уже завершён"}, status_code=409)
        if request.path_params["action"] == "cancel":
            run["task"].cancel()
            with suppress(asyncio.CancelledError):
                await run["task"]
            return JSONResponse({"ok": True})
        data = await request.json()
        if not run.get("approval_id") or data.get("approval_id") != run["approval_id"] or not isinstance(data.get("approved"), bool):
            return JSONResponse({"error": "Подтверждение истекло или не найдено"}, status_code=409)
        future = run["approval"]
        if future.done():
            return JSONResponse({"error": "Подтверждение уже принято"}, status_code=409)
        future.set_result(data["approved"])
        return JSONResponse({"ok": True})

    async def run_status(request):
        run = runs.get(request.path_params["run_id"])
        if not run:
            return JSONResponse({"error": "Запрос не найден"}, status_code=404)
        approval = next((item for item in reversed(run["events"]) if item["event"] == "approval"), None) if run.get("approval_id") else None
        return JSONResponse({"done": run["done"], "pending_approval": approval})

    async def resources(request):
        async with Client(mcp) as client:
            paths = await client.call_tool("list_inforesources", {"limit": 100})
            if paths.is_error:
                return JSONResponse({"error": "Не удалось получить список ИР"}, status_code=502)
            result = []
            for path in paths.structured_content["paths"]:
                result.append({"path": path})
        return JSONResponse({"resources": result})

    async def resource_detail(request):
        path = request.query_params.get("path", "")
        async with Client(mcp) as client:
            result = await client.call_tool("get_subgraph", {"path": path, "depth": 2})
            if result.is_error:
                return JSONResponse({"error": "Не удалось прочитать ИР. Проверьте путь и доступ"}, status_code=502)
        return JSONResponse(result.structured_content)

    async def export_snapshot(request):
        checksum = request.path_params["checksum"]
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            return JSONResponse({"error":"Неверный идентификатор снимка"}, status_code=400)
        path = SNAPSHOTS / (checksum + ".json")
        if not path.exists():
            return JSONResponse({"error":"Снимок не найден"}, status_code=404)
        return FileResponse(path, media_type="application/json", filename="iacpaas-" + checksum[:12] + ".json")

    async def account_settings(request):
        nonlocal mcp
        if request.method == "GET":
            try:
                saved = account_status()
            except ValueError:
                saved = {"username":"", "connected":False, "password_saved":False}
            return JSONResponse({**saved, "paths":json.loads(os.getenv("IACPAAS_KNOWN_IRS", "[]"))})
        if any(not run["done"] for run in runs.values()):
            return JSONResponse({"error":"Дождитесь завершения текущих запросов перед сменой аккаунта"},status_code=409)
        try:
            data = await request.json()
            username, password, paths = data.get("username", ""), data.get("password", ""), data.get("paths", [])
            if not isinstance(username,str) or not 1 <= len(username.strip()) <= 254 or not isinstance(password,str) or len(password)>1000:
                raise ValueError("Проверьте логин и пароль")
            if not isinstance(paths,list) or len(paths)>100 or not all(isinstance(path,str) and 1<=len(path)<=500 for path in paths):
                raise ValueError("Пути баз должны быть списком непустых строк длиной до 500 символов")
            await asyncio.to_thread(connect_account, username, password, paths)
            load_dotenv(PROJECT / ".env",override=True)
            mcp = build_server()
            return JSONResponse({**account_status(), "paths":paths})
        except (ValueError, httpx.HTTPError, OSError):
            return JSONResponse({"error":"Не удалось подключить аккаунт. Проверьте данные входа и доступность IACPaaS. Старый пароль используйте только с прежним логином."},status_code=400)

    @asynccontextmanager
    async def lifespan(app):
        if open_url:
            import webbrowser
            asyncio.get_running_loop().call_later(.7, webbrowser.open, open_url)
        yield
        tasks = [run["task"] for run in runs.values() if not run["done"]]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if shutdown_callback is not None:
            await unload_model(model,ollama_url)

    app = Starlette(lifespan=lifespan, routes=[Route("/", index), Route("/api/health",health),Route("/api/app/stop",stop_application,methods=["POST"]),Route("/api/status", status),
        Route("/api/chats", chats, methods=["GET", "POST"]), Route("/api/chats/{chat_id}", chat_item, methods=["GET", "DELETE"]),
        Route("/api/chats/{chat_id}/branch", branch_chat, methods=["POST"]),
        Route("/api/chat", send, methods=["POST"]), Route("/api/resources", resources), Route("/api/resource", resource_detail),
        Route("/api/exports/{checksum}", export_snapshot),
        Route("/api/account", account_settings, methods=["GET","POST"]),
        Route("/api/runs/{run_id}", run_status), Route("/api/runs/{run_id}/events", events), Route("/api/runs/{run_id}/{action}", control, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static")])

    async def local_only(request: Request, call_next):
        if request.url.hostname not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"error": "Только локальный доступ"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"error": "Недопустимый источник запроса"}, status_code=403)
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
                return JSONResponse({"error": "Обновите страницу и повторите запрос"}, status_code=403)
            try:
                content_length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"error": "Некорректная длина запроса"}, status_code=400)
            if content_length > 64000:
                return JSONResponse({"error": "Запрос слишком велик"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=local_only)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--instance-id",default="")
    args = parser.parse_args()
    import uvicorn
    print(f"IACPaaS: http://127.0.0.1:{args.port}")
    instance=None
    def shutdown():
        if instance:instance.should_exit=True
    app=create_app(open_url=f"http://127.0.0.1:{args.port}" if args.open_browser else None,shutdown_callback=shutdown,instance_id=args.instance_id)
    instance=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=args.port))
    instance.run()


if __name__ == "__main__":
    main()
