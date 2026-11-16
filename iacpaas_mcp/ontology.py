"""Build a bounded universal resource from supported ontology concepts, never arbitrary JSON."""
import math
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class TreeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concept: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=200)
    value: Any = None
    comment: str = Field(default="", max_length=1000)
    children: list["TreeItem"] = Field(default_factory=list, max_length=100)


def data_tree(node):
    """Compare generated data with readback, ignoring platform IDs/dates and sibling order."""
    value={k:node[k] for k in ("name","type","meta","valtype","value") if k in node}
    if node.get("successors"):
        import json
        value["successors"]=sorted((data_tree(c) for c in node["successors"]),key=lambda c:json.dumps(c,ensure_ascii=False,sort_keys=True))
    return value


def typed_value(kind, value):
    if kind == "STRING" and isinstance(value, str) and value.strip(): return value
    if kind == "BOOLEAN" and type(value) is bool: return value
    if kind == "INTEGER" and type(value) is int: return value
    if kind == "REAL" and type(value) in (int, float) and math.isfinite(value): return value
    raise ValueError("Значение не соответствует типу онтологии " + str(kind))


def build_resource(ontology, ontology_path, title, items):
    if not title.strip() or any(c in title for c in "/\\\x00"):
        raise ValueError("Нужно непустое имя нового ресурса без разделителей пути")
    counter = 0

    def branch(definition, entries, depth=0):
        nonlocal counter
        if depth > 8: raise ValueError("Дерево глубже 8 уровней")
        allowed = {}
        for child in definition.get("successors", []):
            if child.get("name"): allowed.setdefault(child["name"], []).append(child)
        result, used, names = [], {}, set()
        for raw in entries:
            item = TreeItem.model_validate(raw) if isinstance(raw, dict) else raw
            candidates = allowed.get(item.concept, [])
            if len(candidates) != 1:
                raise ValueError("Недопустимое или неоднозначное понятие: " + item.concept + ". Допустимы: " + ", ".join(allowed))
            concept = candidates[0]
            if concept.get("link") or concept.get("original") or concept.get("restrictor", "new") not in {"new", ""}:
                raise ValueError("Понятие требует ссылки/клона; создание этой конструкции не поддерживается")
            if "альтернатив" in concept.get("specifier", "").casefold():
                raise ValueError("Альтернативы онтологии пока не поддерживаются конструктором")
            counter += 1
            if counter > 250: raise ValueError("Не больше 250 вершин за одну операцию")
            used[item.concept] = used.get(item.concept, 0) + 1
            kind = concept.get("type")
            if kind == "ТЕРМИНАЛ-СОРТ":
                if item.children or item.name: raise ValueError("Для значения нужны value и пустые children/name")
                node = {"type": "ТЕРМИНАЛ-ЗНАЧЕНИЕ", "meta": item.concept,
                        "valtype": concept.get("valtype"), "value": typed_value(concept.get("valtype"), item.value)}
            elif kind == "НЕТЕРМИНАЛ":
                if item.value is not None: raise ValueError("У нетерминальной вершины нет value")
                if any(c in item.name for c in "/\\\x00"): raise ValueError("Имя вершины не должно содержать разделителей")
                node = {"type": kind, "meta": item.concept, "name": item.name or item.concept}
                if node["name"] in names:
                    raise ValueError("Имена соседних вершин должны быть уникальны: " + node["name"])
                names.add(node["name"])
                children = branch(concept, item.children, depth + 1)
                if children: node["successors"] = children
            else:
                raise ValueError("Конструктор не поддерживает тип понятия " + str(kind))
            if item.comment: node["comment"] = item.comment
            result.append(node)
        for name, definitions in allowed.items():
            if len(definitions) != 1: continue
            spec = definitions[0].get("specifier", "").casefold()
            if "в точности один" in spec and used.get(name, 0) != 1:
                raise ValueError("Понятие «" + name + "» требуется ровно один раз")
            if "непустое" in spec and not used.get(name):
                raise ValueError("Нужно хотя бы одно значение понятия «" + name + "»")
        return result

    return {"title": title, "name": title, "type": "КОРЕНЬ", "json_type": "universal",
            "meta": ontology.get("title", ontology.get("name", "")),
            "ontology": ontology.get("path", ontology_path), "successors": branch(ontology, items)}
