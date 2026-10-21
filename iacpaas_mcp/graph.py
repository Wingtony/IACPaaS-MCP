"""Bounded universal-format traversal. Links count as edges, never new nodes."""
import copy
import json

MAX_NODES = 250000
TYPES = {"КОРЕНЬ", "НЕТЕРМИНАЛ", "ТЕРМИНАЛ-СОРТ", "ТЕРМИНАЛ-ЗНАЧЕНИЕ"}


def walk(root):
    stack = [(root, "")]
    seen = set()
    total = 0
    while stack:
        node, path = stack.pop()
        total += 1
        if total > MAX_NODES:
            raise ValueError("Граф слишком велик: предел 250000 элементов")
        if not isinstance(node, dict):
            raise ValueError("Вершина должна быть объектом")
        if "link" in node:
            yield node, path
            continue
        identity = node.get("id")
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        yield node, path
        children = node.get("successors", [])
        if not isinstance(children, list):
            raise ValueError("successors должен быть массивом")
        for child in reversed(children):
            label = str(child.get("name", child.get("value", ""))) if isinstance(child, dict) else ""
            if "/" in label:
                label = "‴" + label + "‴"
            stack.append((child, path + "/" + label if path else label))


def select(graph, root=""):
    if not root:
        return graph
    for node, path in walk(graph):
        if path == root and "link" not in node:
            return node
    raise ValueError("Вершина не найдена; используйте путь из query_graph")


def counts(graph):
    nodes = links = edges = 0
    for node, _ in walk(graph):
        if "link" in node:
            links += 1
        else:
            nodes += 1
            edges += len(node.get("successors", []))
    return {"nodes": nodes, "edges": edges, "links": links}


def clip(node, depth):
    result = {k: copy.deepcopy(v) for k, v in node.items() if k != "successors"}
    if depth and node.get("successors"):
        result["successors"] = [clip(child, depth - 1) for child in node["successors"]]
    return result


def validate(ir_json):
    if len(ir_json.encode("utf-8")) > 2_000_000:
        raise ValueError("JSON больше 2 МБ")
    resource = json.loads(ir_json)
    if not isinstance(resource, dict) or not resource.get("title"):
        raise ValueError("Обязателен объект с непустым title")
    if resource.get("type") != "КОРЕНЬ":
        raise ValueError("Корень должен иметь type=КОРЕНЬ")
    for node, path in walk(resource):
        if node.get("type") not in TYPES:
            raise ValueError(f"Неизвестный тип вершины: {path}")
        if node.get("type") == "ТЕРМИНАЛ-ЗНАЧЕНИЕ" and "link" not in node:
            if node.get("valtype") not in {"STRING", "INTEGER", "REAL", "BOOLEAN", "DATE", "BLOB"} or "value" not in node:
                raise ValueError(f"Нужны value и корректный valtype: {path}")
    return resource
