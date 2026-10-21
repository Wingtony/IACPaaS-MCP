"""Bounded search over all configured resources, with an account-scoped SQLite cache."""
import asyncio
import hashlib
import json
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path

from .graph import MAX_NODES
from . import provenance

INDEX = Path(__file__).resolve().parent.parent / "logs" / "search.sqlite3"
LOOKALIKES = str.maketrans("авекмнорстух", "abekmhopctyx")


def normalize(text):
    return unicodedata.normalize("NFKC", str(text)).casefold().replace("ё", "е").translate(LOOKALIKES)


def located_nodes(graph):
    stack = [(graph, "", "")]
    seen = set()
    visits = 0
    while stack:
        node, path, pointer = stack.pop()
        visits += 1
        if visits > MAX_NODES:
            raise ValueError("В ресурсе больше 250000 элементов; поиск не завершён")
        if not isinstance(node,dict):
            raise ValueError("Некорректная вершина")
        if "link" in node:
            continue
        identity = node.get("id")
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        yield node,path,pointer
        children = node.get("successors",[])
        if not isinstance(children,list):
            raise ValueError("successors должен быть массивом")
        for index in range(len(children)-1,-1,-1):
            child = children[index]
            if not isinstance(child,dict):
                raise ValueError("Некорректный потомок")
            label = str(child.get("name", child.get("value", "")))
            if "/" in label:
                label = "‴" + label + "‴"
            stack.append((child, "/".join(filter(None,[path,label])), pointer+f"/successors/{index}"))


class ResourceSearch:
    def __init__(self,backend,index_path=None,ttl=300):
        self.backend=backend
        self.path=index_path or INDEX
        self.ttl=ttl
        self.lock=asyncio.Lock()
        identity = getattr(backend,"base","mock") + json.dumps(getattr(backend,"headers",{}),sort_keys=True)
        # Separate users/tokens and test/mock server instances. No credentials in SQLite.
        if hasattr(backend,"resources"):
            identity += uuid.uuid4().hex
        self.namespace=hashlib.sha256(identity.encode()).hexdigest()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS resource_cache (namespace TEXT,resource TEXT,source TEXT,updated REAL,nodes INTEGER,PRIMARY KEY(namespace,resource))")
            db.execute("CREATE TABLE IF NOT EXISTS search_nodes (namespace TEXT,resource TEXT,path TEXT,pointer TEXT,payload TEXT,search_text TEXT,name_key TEXT,meta_key TEXT,depth INTEGER)")
            db.execute("CREATE INDEX IF NOT EXISTS resource_nodes_idx ON search_nodes(namespace,resource)")

    def connect(self):
        db=sqlite3.connect(self.path,timeout=30)
        db.row_factory=sqlite3.Row
        return db

    def cached(self,resource):
        with self.connect() as db:
            row=db.execute("SELECT source,updated,nodes FROM resource_cache WHERE namespace=? AND resource=?",(self.namespace,resource)).fetchone()
        return dict(row) if row else None

    def invalidate(self):
        with self.connect() as db:
            db.execute("DELETE FROM resource_cache WHERE namespace=?",(self.namespace,))

    async def full_export(self,resource):
        cached=self.cached(resource)
        if cached and time.time()-cached["updated"]<self.ttl:
            source=json.loads(cached["source"])
            if await asyncio.to_thread(provenance.verify_snapshot,source):
                def load():
                    with (provenance.SNAPSHOTS / (source["export_sha256"]+".json")).open(encoding="utf-8") as handle:
                        graph=json.load(handle)
                    graph["_source"]=source
                    return graph
                return await asyncio.to_thread(load)
        graph = await self.backend.export(resource)
        await asyncio.to_thread(self.index_graph, resource, graph)
        return graph

    def index_graph(self,resource,graph):
        source=graph.get("_source")
        if not source:
            raise ValueError("Экспорт не содержит доказательства происхождения")
        count=0
        def rows():
            nonlocal count
            for node,path,pointer in located_nodes(graph):
                count+=1
                payload={key:node[key] for key in ("name","value","type","meta","valtype") if key in node}
                text="\n".join(str(node.get(key,"")) for key in ("name","value","meta"))
                yield (self.namespace,resource,path,pointer,json.dumps(payload,ensure_ascii=False),normalize(text),
                       normalize(node.get("name",node.get("value",""))),normalize(node.get("meta","")),pointer.count("/successors/"))
        with self.connect() as db:
            db.execute("DELETE FROM search_nodes WHERE namespace=? AND resource=?",(self.namespace,resource))
            db.executemany("INSERT INTO search_nodes VALUES (?,?,?,?,?,?,?,?,?)",rows())
            db.execute("INSERT OR REPLACE INTO resource_cache VALUES (?,?,?,?,?)",(self.namespace,resource,json.dumps(source,ensure_ascii=False),time.time(),count))
        return source,count

    def lookup(self,resources,text,meta,limit):
        if not resources:
            return [],0
        marks=",".join("?" for _ in resources)
        condition=f"namespace=? AND resource IN ({marks}) AND instr(search_text,?)>0"
        params=[self.namespace,*resources,normalize(text)]
        if meta:
            condition+=" AND meta_key=?"
            params.append(normalize(meta))
        with self.connect() as db:
            total=db.execute("SELECT count(*) FROM search_nodes WHERE "+condition,params).fetchone()[0]
            rows=db.execute("SELECT resource,path,pointer,payload FROM search_nodes WHERE "+condition+
                            " ORDER BY CASE WHEN name_key=? THEN 0 ELSE 1 END,depth,resource,path,pointer LIMIT ?",[*params,normalize(text),limit]).fetchall()
        return [{"resource":row["resource"],"path":row["path"],"snapshot_pointer":row["pointer"],**json.loads(row["payload"])} for row in rows],total

    async def search(self,resources,text,meta="",limit=12,refresh=False):
        if not text.strip() and not meta.strip():
            raise ValueError("Укажите короткое слово/марку/символ для поиска или метапонятие")
        coverage=[]
        sources=[]
        async with self.lock:
            for path in resources:
                cached=self.cached(path)
                cache_hit=bool(cached and not refresh and time.time()-cached["updated"]<self.ttl)
                if cache_hit:
                    cache_hit=await asyncio.to_thread(provenance.verify_snapshot,json.loads(cached["source"]))
                try:
                    if cache_hit:
                        source,count=json.loads(cached["source"]),cached["nodes"]
                    else:
                        graph=await self.backend.export(path)
                        source,count=await asyncio.to_thread(self.index_graph,path,graph)
                        del graph
                    sources.append(source)
                    coverage.append({"path":path,"status":"searched","nodes":count,"cached":cache_hit})
                except (ValueError,TypeError,KeyError,OSError,RecursionError) as exc:
                    coverage.append({"path":path,"status":"failed","error":str(exc)[:220]})
            complete_paths=[row["path"] for row in coverage if row["status"]=="searched"]
            matches,total=await asyncio.to_thread(self.lookup,complete_paths,text,meta,limit)
        return {"matches":matches,"total":total,"truncated":total>len(matches),
                "coverage":coverage,"complete":len(complete_paths)==len(resources),
                "searched_resources":len(complete_paths),"configured_resources":len(resources),
                "cache_ttl_seconds":self.ttl,"_sources":sources}
