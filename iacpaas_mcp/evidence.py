"""Exact-value verification, independent of the language model's wording."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from .provenance import verify_snapshot
from . import provenance
from .graph import counts, select, clip, walk

READ_TOOLS = {"list_inforesources", "get_inforesource", "get_subgraph", "count_graph", "query_graph", "search_resources", "validate_resource"}
LABELS = {"nodes":"Вершины", "edges":"Дуги", "links":"Ссылки", "total":"Всего совпадений", "path":"Путь",
          "name":"Имя", "value":"Значение", "meta":"Метапонятие", "type":"Тип", "title":"Название",
          "paths":"Доступные ресурсы", "truncated":"Список сокращён", "depth":"Глубина", "resource":"База",
          "complete":"Поиск выполнен по всей области", "searched_resources":"Проверено баз", "configured_resources":"Баз в области поиска"}


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def pointer_get(data, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("Нужен точный JSON Pointer, например /nodes")
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(data, list):
            if not key.isdigit() or (len(key) > 1 and key.startswith("0")):
                raise ValueError("Некорректный индекс")
            data = data[int(key)]
        elif isinstance(data, dict):
            data = data[key]
        else:
            raise ValueError("Путь не указывает на поле")
    return data


def fields(data, prefix=""):
    if isinstance(data, dict):
        for key, value in data.items():
            if key.startswith("_") or key == "is_error":
                continue
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from fields(value, prefix + "/" + escaped)
    elif isinstance(data, list):
        for i, value in enumerate(data):
            yield from fields(value, prefix + "/" + str(i))
    else:
        yield {"pointer": prefix, "value": data}


def verify_derivation(record):
    """Recompute graph counts / re-read returned vertices from the archived export."""
    data, args = record["data"], record["arguments"]
    if record['tool']=='validate_resource':
        from .graph import validate
        return data.get('valid') is True and counts(validate(args['ir_json']))=={key:data.get(key) for key in ('nodes','edges','links')}
    if record["tool"] == "search_resources":
        from .search import located_nodes, normalize
        sources=data.get("_sources",[])
        coverage=data.get("coverage",[])
        completed=[item["path"] for item in coverage if item.get("status")=="searched"]
        if len(set(completed))!=len(completed) or sorted(source["path"] for source in sources)!=sorted(completed):
            return False
        if data.get("searched_resources")!=len(completed) or data.get("configured_resources")!=len(coverage):
            return False
        if data.get("complete") is not (len(completed)==len(coverage)):
            return False
        matches=data.get("matches",[])
        found=set()
        total=0
        for source in sources:
            if not verify_snapshot(source):
                return False
            with (provenance.SNAPSHOTS / (source["export_sha256"]+".json")).open(encoding="utf-8") as handle:
                graph=json.load(handle)
            wanted={(item["snapshot_pointer"],item["path"]):item for item in matches if item["resource"]==source["path"]}
            for node,path,pointer in located_nodes(graph):
                text="\n".join(str(node.get(key,"")) for key in ("name","value","meta"))
                if normalize(args.get("text","")) not in normalize(text) or (args.get("meta") and normalize(node.get("meta",""))!=normalize(args["meta"])):
                    continue
                total+=1
                if (pointer,path) in wanted:
                    match=wanted[(pointer,path)]
                    expected={key:node[key] for key in ("name","value","type","meta","valtype") if key in node}
                    actual={key:value for key,value in match.items() if key not in {"resource","path","snapshot_pointer"}}
                    if digest(expected)!=digest(actual):
                        return False
                    found.add((source["path"],pointer,path))
        if len(found)!=len(matches) or type(data.get("total")) is not int or data["total"]!=total:
            return False
        return data.get("truncated") is (total>len(matches))
    source = data.get("_source")
    if not source:
        # Resource lists come from configured paths, not an export endpoint.
        return record["tool"] == "list_inforesources"
    if not verify_snapshot(source) or source.get("path") != args.get("path"):
        return False
    raw = json.loads((provenance.SNAPSHOTS / (source["export_sha256"] + ".json")).read_text(encoding="utf-8"))
    if record["tool"] == "count_graph":
        calculated = counts(select(raw, args.get("root", "")))
        return all(type(data.get(key)) is int and data[key] == value for key, value in calculated.items())
    if record["tool"] == "query_graph":
        expected_total = sum(1 for node, _ in walk(select(raw, args.get("root", "")))
            if "link" not in node
            and args.get("text", "").casefold() in str(node.get("name", node.get("value", ""))).casefold()
            and (not args.get("meta") or node.get("meta", "").casefold() == args["meta"].casefold()))
        if type(data.get("total")) is not int or data["total"] != expected_total:
            return False
        if len(data.get("matches", [])) != min(expected_total, args.get("limit", 20)):
            return False
        for match in data.get("matches", []):
            actual = select(raw, match["path"])
            if "link" in actual or any(digest(actual.get(key)) != digest(value) for key, value in match.items() if key != "path"):
                return False
        return True
    if record["tool"] == "get_subgraph":
        expected = clip(select(raw, args.get("root", "")), args.get("depth", 2))
        actual = dict(data["graph"])
        actual.pop("_source", None)
        return actual == expected
    if record["tool"] == "get_inforesource":
        actual = {key:value for key,value in data.items() if key not in {"_source", "is_error"}}
        return actual == raw
    return False


class EvidenceStore:
    def __init__(self, directory, backend="mock", scope=""):
        self.directory = directory
        self.backend = backend
        self.scope = scope
        self.records = {}

    def add(self, tool, arguments, result):
        actual_backend = result.get("_source", {}).get("backend") or next((source.get("backend") for source in result.get("_sources",[]) if source.get("backend")),None) or result.get("source")
        if actual_backend in {"mock", "http"}:
            self.backend = actual_backend
        evidence_id = "S" + str(len(self.records) + 1)
        record = {"id": evidence_id, "tool": tool, "arguments": copy.deepcopy(arguments),
                  "data": copy.deepcopy(result), "backend": self.backend,
                  "retrieved_at": datetime.now(timezone.utc).isoformat(), "sha256": digest(result)}
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / (evidence_id + ".json")).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        self.records[evidence_id] = record
        return evidence_id

    def index(self):
        return [{"id": r["id"], "tool": r["tool"], "path": r["arguments"].get("path", ""),
                 "root": r["arguments"].get("root", "")} for r in self.records.values()]

    def read(self, evidence_id, pointer):
        if evidence_id not in self.records:
            raise ValueError("Источник не найден в текущем запросе")
        value = pointer_get(self.records[evidence_id]["data"], pointer)
        if len(json.dumps(value, ensure_ascii=False)) > 3000:
            raise ValueError("Поле слишком большое. Укажите путь к конкретному значению")
        return {"evidence_id": evidence_id, "pointer": pointer, "value": value}

    def for_model(self, evidence_id, max_fields=35):
        record = self.records[evidence_id]
        flat = list(fields(record["data"]))
        if record["tool"]=="search_resources":
            summary=[item for item in flat if item["pointer"] in {"/complete","/total","/truncated","/searched_resources","/configured_resources"}]
            matches=[item for item in flat if item["pointer"].startswith("/matches/") and not item["pointer"].endswith("/snapshot_pointer")]
            flat=summary+matches+[item for item in flat if item not in summary and item not in matches]
            max_fields=65
        candidates = [{"pointer": item["pointer"], "value": item["value"] if len(str(item["value"])) < 300 else "[длинное поле: read_evidence]"}
                      for item in flat[:max_fields]]
        return {"evidence_id": evidence_id, "tool": record["tool"], "scope": record["arguments"],
                "fields": candidates, "field_count": len(flat), "omitted_fields": max(0, len(flat)-max_fields)}

    def verify(self, answer):
        try:
            raw = answer.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.splitlines()[1:-1])
            document = json.loads(raw)
            claims = document["claims"]
            if not isinstance(claims, list) or not 1 <= len(claims) <= 40:
                raise ValueError("Нужны 1–40 проверяемых утверждений")
            checked = []
            seen = set()
            verified_records=set()
            for claim in claims:
                source, pointer = claim["evidence_id"], claim["pointer"]
                record = self.records[source]
                if digest(record["data"]) != record["sha256"]:
                    raise ValueError("Источник изменён")
                if source not in verified_records and not verify_derivation(record):
                    raise ValueError("Поле или вычисление не прошло проверку по снимку экспорта")
                verified_records.add(source)
                if not isinstance(pointer, str) or any(part.startswith("_") or part == "is_error" for part in pointer.split("/")):
                    raise ValueError("Служебное поле не является фактом базы")
                value = self.read(source, pointer)["value"]
                if isinstance(value, (list, dict)):
                    raise ValueError("Укажите конкретное поле, а не целый граф")
                # A supplied value is optional; if supplied it must match with identical JSON type.
                if "value" in claim and (type(claim["value"]) is not type(value) or claim["value"] != value):
                    raise ValueError("Значение модели не совпало с данными источника")
                if (source, pointer) in seen:
                    continue
                seen.add((source, pointer))
                checked.append({"evidence_id": source, "pointer": pointer, "value": value})
            return {"status": "verified_fields", "claims": checked, "scope": self.scope,
                    "sources": [{**{k: v for k, v in record.items() if k != "data"},
                                 "export":record["data"].get("_source"), "exports":record["data"].get("_sources",[])} for record in self.records.values()],
                    "meaning_verified": False,
                    "notice": "Поля точно соответствуют сохранённому ответу MCP. Хеш фиксирует снимок, но не доказывает истинность самой базы или смысл выбора."}
        except (ValueError, KeyError, IndexError, TypeError, OSError):
            return {"status":"blocked", "claims":[], "scope":self.scope, "meaning_verified":False,
                    "notice":"Ответ модели не прошёл проверку точных ссылок/значений. Неподтверждённые факты скрыты."}

    def render(self, report):
        if report["status"] != "verified_fields":
            return report["notice"] + " Уточните вопрос или выберите другой ресурс."
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        lines = ["Проверенные поля " + ("учебной базы" if self.backend == "mock" else "ответа MCP для IACPaaS") + ":",
                 "", "| Источник / поле | Значение |", "|---|---|"]
        for claim in report["claims"]:
            label = LABELS.get(claim["pointer"].rsplit("/", 1)[-1], claim["pointer"])
            lines.append(f"| {cell(claim['evidence_id'])} · {cell(label)} (`{cell(claim['pointer'])}`) | {cell(claim['value'])} |")
        searches=[record["data"] for record in self.records.values() if record["tool"]=="search_resources"]
        for search in searches:
            lines.extend(["",f"Поиск: {search['searched_resources']} из {search['configured_resources']} баз. " + ("Область поиска проверена полностью." if search["complete"] else "Поиск неполный: некоторые базы не удалось прочитать.")])
            if search.get("truncated"):
                lines.append("Показана только часть совпадений; полный счётчик находится в источнике.")
        lines.extend(["", "Область: " + self.scope if self.scope else "Область: все подключённые базы.",
                      "", "Проверено совпадение полей с ответом MCP; смысл выбора показан в источниках ниже."])
        return "\n".join(lines)
