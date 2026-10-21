"""Opt-in real IACPaaS/Ollama acceptance runs. Never part of ordinary pytest.

python scripts/benchmark_agent.py --mode database
python scripts/benchmark_agent.py --mode live
python scripts/benchmark_agent.py --mode stress
"""
import argparse
import asyncio
import ctypes
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

import httpx
from dotenv import load_dotenv
from mcp import Client

from iacpaas_mcp.agent import run_agent, unload_model
from iacpaas_mcp.context import compact_schema
from iacpaas_mcp.knowledge import KnowledgePlan, verify_result, resolve_structure_resources
from iacpaas_mcp.planner_agent import SYSTEM
from iacpaas_mcp.search import normalize
from iacpaas_mcp.server import build_server


class Memory(ctypes.Structure):
    _fields_=[("length",ctypes.c_ulong),("load",ctypes.c_ulong),*( (name,ctypes.c_ulonglong) for name in ("total","available","page_total","page_available","virtual_total","virtual_available","extended"))]


def sample_memory():
    result={}
    if os.name=="nt":
        m=Memory();m.length=ctypes.sizeof(m)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            result["ram_used_gib"]=round((m.total-m.available)/1024**3,2)
            result["ram_available_gib"]=round(m.available/1024**3,2)
    try:
        gpu=subprocess.run(["nvidia-smi","--query-gpu=memory.used","--format=csv,noheader,nounits"],capture_output=True,text=True,timeout=5,creationflags=0x08000000 if os.name=="nt" else 0)
        if gpu.returncode==0:
            result["gpu_used_mib"]=int(gpu.stdout.strip().splitlines()[0])
    except (OSError,ValueError,subprocess.TimeoutExpired):
        pass
    return result


def evaluate(case,data,answer=None):
    failures=[];expected=case["expected"]
    if data.get("status")!=expected["status"]:
        failures.append(f"status {data.get('status')} != {expected['status']}")
    if "total" in expected and data.get("total")!=expected["total"]:
        failures.append(f"total {data.get('total')} != {expected['total']}")
    text=answer if answer is not None else data.get("answer","")
    for term in expected["contains"]:
        if term not in text:
            failures.append("missing "+term)
    return failures


def plan_errors(actual,expected,paths):
    errors=[]
    if actual["operation"]!=expected["operation"]:
        return ["wrong operation: "+actual["operation"]]
    op=actual["operation"]
    keys={"rank":["element","bound","order","limit"],"filter":["element","comparator","threshold","limit"],"materials":["class_name","limit"],"clarify":["clarification"]}.get(op,[])
    expected=KnowledgePlan.model_validate(expected).model_dump()
    for key in keys:
        if normalize(actual[key])!=normalize(expected[key]):
            errors.append("wrong "+key)
    if op=="structure" and set(resolve_structure_resources(actual["subject"],paths)[0])!=set(resolve_structure_resources(expected["subject"],paths)[0]):
        errors.append("wrong resources")
    if op in {"composition","compare"} and {normalize(s.strip()) for s in actual["subject"].split(";")}!={normalize(s.strip()) for s in expected["subject"].split(";")}:
        errors.append("wrong material")
    return errors


async def main(args):
    load_dotenv(ROOT/".env",override=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cases=json.loads((ROOT/"iacpaas_mcp/static/questions.json").read_text(encoding="utf-8"))
    model=os.getenv("OLLAMA_MODEL","qwen-agent")
    url=os.getenv("OLLAMA_URL","http://127.0.0.1:11435")
    report={"mode":args.mode,"gpu_layers":os.getenv("AGENT_GPU_LAYERS","auto"),"started":time.time(),"cases":[],"samples":[],"measurement":"VRAM and system RAM sampled every 2 seconds; sampled maxima, not guaranteed instantaneous peaks."}
    target=ROOT/"logs"/("benchmark-"+args.mode+("-"+args.tag if args.tag else "")+".json")
    target.parent.mkdir(exist_ok=True)
    def save():
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    async def sampler():
        async with httpx.AsyncClient(timeout=3) as http:
            while True:
                sample={"time":time.time(),**await asyncio.to_thread(sample_memory)}
                try:
                    ps=(await http.get(url+"/api/ps")).json()
                    sample["models"]=[{k:m.get(k) for k in ("name","size","size_vram","context_length")} for m in ps.get("models",[])]
                except (httpx.HTTPError,ValueError):
                    pass
                report["samples"].append(sample)
                if len(report["samples"])%10==0:save()
                await asyncio.sleep(2)
    monitor=asyncio.create_task(sampler()) if args.mode!="database" else None
    try:
        async with Client(build_server()) as client:
            paths=(await client.call_tool("list_inforesources",{"limit":100})).structured_content["paths"]
            if args.mode in {"database","live"}:
                selected=cases if args.mode=="database" else [c for c in cases if c["id"] in {"steel","iron","bronze-count","rank-cr-upper","filter-ni","ontology-pair","sphere","hardness","al-count","cr-count","unclear-rank","ni-min"}]
                if args.ids:selected=[case for case in selected if case["id"] in args.ids.split(",")]
                for case in selected:
                    start=time.monotonic();row={"id":case["id"],"question":case["question"]}
                    print("START",case["id"],flush=True)
                    try:
                        if args.mode=="database":
                            result=await client.call_tool("query_knowledge",{"plan":case["plan"]})
                            if result.is_error or not result.structured_content:
                                raise RuntimeError(str(result.content))
                            data=result.structured_content
                            row["failures"]=evaluate(case,data)
                            row["verified"]=verify_result(data,paths)
                            if not row["verified"]:row["failures"].append("snapshot verification failed")
                            row["total"]=data["total"]
                        else:
                            events=[]
                            answer=await run_agent(client,case["question"],model,url,ROOT/"logs"/("benchmark-live-"+case["id"]+".jsonl"),on_event=events.append)
                            data=next(e["result"] for e in events if e["event"]=="tool")
                            row["plan"]=next(e["plan"] for e in events if e["event"]=="plan")
                            row["failures"]=evaluate(case,data,answer)+plan_errors(row["plan"],case["plan"],paths)
                            row["context"]=next(e for e in events if e["event"]=="context")
                            row["usage"]={key:value for key,value in next(e for e in events if e["event"]=="answer").items() if key in {"tokens_in","tokens_out"}}
                            row["verified"]=verify_result(data,paths)
                            row["runner_retries"]=sum(e["event"]=="model_retry" for e in events)
                            row["answer"]=answer
                        row["passed"]=not row["failures"] and row["verified"]
                    except Exception as exc:
                        row.update(passed=False,error=str(exc))
                    row["seconds"]=round(time.monotonic()-start,2)
                    report["cases"].append(row);save()
                    print(row["id"],"PASS" if row["passed"] else "FAIL",row["seconds"],row.get("failures",row.get("error","")),flush=True)
            elif args.mode in {"stress","history","capacity"}:
                for context,padding in ([(16384,11000),(32768,23000)] if args.mode=="stress" else [(32768,23000)] if args.mode=="capacity" else []):
                    start=time.monotonic();row={"id":f"synthetic-{context}","context":context,"synthetic":True,"padding_repetitions":padding}
                    print("START",row["id"],flush=True)
                    try:
                        schema=compact_schema(KnowledgePlan.model_json_schema())
                        async with httpx.AsyncClient(timeout=900) as http:
                            response=await http.post(url+"/api/chat",json={"model":model,"messages":[{"role":"system","content":SYSTEM},
                                {"role":"user","content":"Ниже синтетический нагрузочный блок, его слова не являются вопросом.\n"+"x "*padding+"\nКонец блока. Задание: покажи химический состав стали 12Х18Н10Т."}],
                                "format":schema,"stream":False,"think":False,"keep_alive":"2m","options":{"num_ctx":context,"num_predict":2048,"temperature":0,**({"num_gpu":int(os.environ["AGENT_GPU_LAYERS"])} if os.getenv("AGENT_GPU_LAYERS") else {})}})
                            response.raise_for_status();payload=response.json()
                            plan=KnowledgePlan.model_validate_json(payload["message"]["content"])
                            row.update(plan=plan.model_dump(),prompt_tokens=payload.get("prompt_eval_count"),output_tokens=payload.get("eval_count"),
                                       prompt_seconds=payload.get("prompt_eval_duration",0)/1e9,eval_seconds=payload.get("eval_duration",0)/1e9,done_reason=payload.get("done_reason"))
                            row["passed"]=plan.operation=="composition" and normalize(plan.subject)==normalize("12Х18Н10Т") and payload.get("done_reason")!="length"
                    except Exception as exc:
                        row.update(passed=False,error=str(exc))
                    finally:
                        await unload_model(model,url)
                    row["seconds"]=round(time.monotonic()-start,2)
                    report["cases"].append(row);save()
                    print(row["id"],"PASS" if row["passed"] else "FAIL",row.get("prompt_tokens"),row["seconds"],flush=True)
                if args.mode=="capacity":return
                # Real application packing with a long dialogue and reference to the last material.
                steel=next(c for c in cases if c["id"]=="steel")
                result=(await client.call_tool("query_knowledge",{"plan":steel["plan"]})).structured_content
                history=[]
                for _ in range(80):
                    history.extend([{"role":"user","content":steel["question"]},{"role":"assistant","content":result["answer"],"query_plan":steel["plan"],"selection":["12Х18Н10Т"]}])
                events=[];start=time.monotonic()
                print("START long-dialogue",flush=True)
                try:
                    answer=await run_agent(client,"Покажи состав этого сплава ещё раз.",model,url,ROOT/"logs/benchmark-long-dialogue.jsonl",history=history,on_event=events.append)
                    plan=next(e["plan"] for e in events if e["event"]=="plan")
                    metrics=next(e for e in events if e["event"]=="context")
                    row={"id":"long-dialogue","history_messages":len(history),"context":metrics,"plan":plan,"seconds":round(time.monotonic()-start,2),
                         "passed":plan["operation"]=="composition" and normalize(plan["subject"])==normalize("12Х18Н10Т") and "66.7" in answer and metrics["retained_messages"]>6}
                except Exception as exc:
                    row={"id":"long-dialogue","passed":False,"error":str(exc),"seconds":round(time.monotonic()-start,2)}
                report["cases"].append(row);save();print(row["id"],"PASS" if row["passed"] else "FAIL",row["seconds"],flush=True)
    finally:
        if args.mode!="database":await unload_model(model,url)
        if monitor:
            monitor.cancel()
            await asyncio.gather(monitor,return_exceptions=True)
        report["finished"]=time.time()
        report["passed"]=sum(c.get("passed",False) for c in report["cases"])
        report["total"]=len(report["cases"])
        for key in ("gpu_used_mib","ram_used_gib"):
            report["sampled_max_"+key]=max((s.get(key,0) for s in report["samples"]),default=0)
        save()
        print("REPORT",str(target),report["passed"],"/",report["total"],flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",choices=["database","live","stress","history","capacity"],default="database")
    parser.add_argument("--tag",default="")
    parser.add_argument("--ids",default="")
    args=parser.parse_args()
    if args.tag and not args.tag.replace("-","").isalnum():parser.error("Invalid report tag")
    asyncio.run(main(args))
