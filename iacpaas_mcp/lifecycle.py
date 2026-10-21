"""Local start/stop control. Stop only this app; never kill processes by name."""
import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import uuid
import webbrowser

import httpx
from dotenv import load_dotenv

from .context import context_settings

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/".local/runtime.json"
WEB="http://127.0.0.1:8770"
HIDDEN=0x08000000 if os.name=="nt" else 0


def healthy():
    try:
        response=httpx.get(WEB+"/api/health",timeout=2)
        return response.status_code==200 and response.json().get("application")=="iacpaas-atlas"
    except (httpx.HTTPError,ValueError):
        return False


def owned_process(record):
    """A stale/reused PID cannot authorize terminating an unrelated Python process."""
    if os.name!="nt" or not isinstance(record,dict) or type(record.get("pid")) is not int or record["pid"]<=0 or not re.fullmatch(r"[a-f0-9]{32}",str(record.get("instance_id",""))):
        return False
    try:
        pid=record["pid"]
        command=f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object -ExpandProperty CommandLine"
        reply=subprocess.run(["powershell.exe","-NoProfile","-Command",command],capture_output=True,text=True,timeout=5,creationflags=HIDDEN)
        return "iacpaas_mcp.web" in reply.stdout and ("--instance-id "+record.get("instance_id","INVALID")) in reply.stdout
    except (OSError,subprocess.TimeoutExpired):
        return False


def start_app(open_browser=True, progress=lambda message:None):
    load_dotenv(ROOT/".env",override=True)
    (ROOT/"logs").mkdir(exist_ok=True)
    if not healthy():
        progress("Запускаю службу Ollama…")
        startup=subprocess.run(["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(ROOT/"Start-Ollama.ps1")],capture_output=True,timeout=90,creationflags=HIDDEN)
        if startup.returncode:
            raise RuntimeError("Ollama не запустилась. Проверьте logs/ollama.stderr.log.")
        progress("Запускаю веб-интерфейс…")
        instance=uuid.uuid4().hex
        with (ROOT/"logs/web.stdout.log").open("ab") as stdout,(ROOT/"logs/web.stderr.log").open("ab") as stderr:
            process=subprocess.Popen([sys.executable,"-X","utf8","-m","iacpaas_mcp.web","--port","8770","--instance-id",instance],cwd=ROOT,stdout=stdout,stderr=stderr,creationflags=HIDDEN)
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps({"pid":process.pid,"instance_id":instance}),encoding="utf-8")
        for _ in range(60):
            if healthy():break
            if process.poll() is not None:raise RuntimeError("Веб-интерфейс не запустился. Проверьте logs/web.stderr.log.")
            time.sleep(.25)
        else:raise RuntimeError("Веб-интерфейс не ответил вовремя.")
        # A Windows venv launcher can spawn a base-Python child. Record the actual
        # serving PID, identified by the marker returned by our health endpoint.
        serving=httpx.get(WEB+"/api/health",timeout=3).json()
        if serving.get("instance_id")==instance:
            STATE.write_text(json.dumps({"pid":serving["pid"],"instance_id":instance}),encoding="utf-8")
    if open_browser:webbrowser.open(WEB)
    progress("Сайт работает. Загружаю Qwen в память…")
    context,_,output=context_settings()
    options={"num_ctx":context,"num_predict":output}
    if os.getenv("AGENT_GPU_LAYERS"):options["num_gpu"]=int(os.environ["AGENT_GPU_LAYERS"])
    url=os.getenv("OLLAMA_URL","http://127.0.0.1:11435")
    response=httpx.post(url+"/api/generate",json={"model":os.getenv("OLLAMA_MODEL","qwen-agent"),"keep_alive":-1,"options":options},timeout=180)
    response.raise_for_status()
    return "Web и Qwen запущены. Модель остаётся в памяти до остановки."


def stop_app(progress=lambda message:None):
    load_dotenv(ROOT/".env",override=True)
    progress("Останавливаю веб-интерфейс и текущие запросы…")
    try:
        status=httpx.get(WEB+"/api/status",timeout=5)
        status.raise_for_status()
        response=httpx.post(WEB+"/api/app/stop",headers={"X-CSRF-Token":status.json()["csrf"]},json={},timeout=10)
        response.raise_for_status()
    except httpx.HTTPError:
        # Fall back only to a recorded PID carrying our unique instance marker.
        try:record=json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError,ValueError):record={}
        if owned_process(record):
            subprocess.run(["powershell.exe","-NoProfile","-Command",f"Stop-Process -Id {record['pid']}"],timeout=10,creationflags=HIDDEN,check=True)
    for _ in range(60):
        if not healthy():break
        time.sleep(.3)
    progress("Освобождаю память Qwen…")
    url=os.getenv("OLLAMA_URL","http://127.0.0.1:11435")
    model=os.getenv("OLLAMA_MODEL","qwen-agent")
    try:
        httpx.post(url+"/api/generate",json={"model":model,"keep_alive":0},timeout=30).raise_for_status()
        for _ in range(20):
            data=httpx.get(url+"/api/ps",timeout=3).json()
            if not any(m["name"].removesuffix(":latest")==model.removesuffix(":latest") for m in data.get("models",[])):break
            time.sleep(.25)
        else:raise RuntimeError("Qwen ещё загружена. Повторите остановку.")
    except httpx.ConnectError:
        pass
    if healthy():raise RuntimeError("Модель остановлена, но веб-сервер ещё отвечает. Проверьте окно запуска.")
    return "Web остановлен. Qwen выгружена из памяти."


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["start","stop","status"])
    parser.add_argument("--no-browser",action="store_true")
    args=parser.parse_args()
    if args.action=="start":print(start_app(not args.no_browser,print))
    elif args.action=="stop":print(stop_app(print))
    else:print("Web запущен" if healthy() else "Web остановлен")


if __name__=="__main__":main()
