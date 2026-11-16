"""Domain queries and deterministic presentation over universal IACPaaS graphs.

The LLM selects a bounded operation. It never calculates percentages or renders facts.
"""
import json
import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .search import located_nodes, normalize
from .conditions import CompositionCondition, satisfies


class KnowledgePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["catalog", "resources", "structure", "materials", "composition", "element", "overview", "property", "term", "rank", "filter", "compare", "clarify"]
    subject: str = Field(default="", max_length=150, description="Точная марка, элемент, свойство или термин. Для compare марки через ;")
    structure_root: str = Field(default='',max_length=500,description='Для structure: точный путь ветки из query_graph/get_children; subject задаёт ресурс, а не ветку')
    entity_meta: Literal['','Химический элемент','Химическое вещество','Материал','Класс материалов','Класс свойств','Свойство','Термин'] = Field(default='',description='Для overview: тип объекта, если название/обозначение неоднозначно')
    overview_sections: list[Literal['attributes','substances','materials','other']] = Field(default_factory=lambda:['attributes','substances','materials','other'],min_length=1,max_length=4,description='Для overview: какие разделы нужны пользователю; отдельный вопрос о веществах не требует повторять весь список материалов')
    class_name: str = Field(default="", max_length=150, description="Класс материалов, например Бронзы; пусто — все классы")
    element: str = Field(default="", max_length=40, description="Символ элемента для rank/filter, например Cr")
    elements: list[str] = Field(default_factory=list,max_length=10,description="Для materials: все элементы, которые должны одновременно быть указаны в составе")
    element_mode: Literal["all", "any"] = "all"
    comparator: Literal["le", "ge"] = "le"
    threshold: float | None = None
    bound: Literal["upper", "lower"] = "upper"
    bound_inferred: bool = False
    order: Literal["asc", "desc"] = "desc"
    clarification: Literal["entity", "bound", "compound", "property", "scope"] = "entity"
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=100000)
    dataset: str = Field(default="", max_length=64, pattern="^([a-f0-9]{64})?$", description="Идентификатор набора из предыдущей страницы; при изменении базы продолжение отклоняется")
    conditions: list[CompositionCondition] = Field(default_factory=list, max_length=16, description="Все условия состава применяются сервером к полному набору; absent означает не указан, не нулевое содержание")
    condition_mode: Literal["all", "any"] = "all"


# Language aliases, not stored answers. Counts and memberships always come from graphs.
ELEMENT_ALIASES={"Al":("алюмини","алюминий"),"Cr":("хром",),"Ni":("никел",),"Fe":("желез","железо"),"Cu":("мед","медь"),"Mg":("магни",),"Ti":("титан",),"Sn":("олов",),"C":("углерод",),"Mn":("марган",)}


def mentioned_element(text):
    return next(iter(mentioned_elements(text)),"")


def mentioned_elements(text):
    found=[]
    for symbol,aliases in ELEMENT_ALIASES.items():
        if re.search(r"(?<![A-Za-zА-Яа-я])"+symbol+r"(?![A-Za-zА-Яа-я])",text,re.I):
            found.append(symbol)
        elif any(alias in text.casefold().replace("ё","е") for alias in aliases):
            found.append(symbol)
    return found


def resolve_material_class(wanted, classes):
    if not wanted:
        return ""
    exact=[name for name in classes if normalize(name)==normalize(wanted)]
    if exact:return exact[0]
    if "бронз" in wanted.casefold():
        return next((name for name in classes if "бронз" in name.casefold()),None)
    symbol=mentioned_element(wanted)
    if symbol:
        stems=ELEMENT_ALIASES[symbol]
        choices=[name for name in classes if any(stem in name.casefold() for stem in stems)]
        if len(choices)==1:return choices[0]
    return None


def children(node):
    return node.get("successors", [])


def leaves(node):
    for n, path, pointer in located_nodes(node):
        if "value" in n:
            yield n, path, pointer


def label(node):
    return str(node.get("name", node.get("value", "")))


def decimal(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def number(value):
    return format(value, "f").rstrip("0").rstrip(".") if value and "." in format(value, "f") else str(value)


def expression(node):
    numeric = [str(n["value"]) for n, _, _ in leaves(node) if n.get("meta") == "Числовое значение"]
    coefficients = [str(n["value"]) for n, _, _ in leaves(node) if n.get("meta") == "коэффициент"]
    elements = [str(n["value"]) for n, _, _ in leaves(node) if n.get("meta") == "Химический элемент"]
    if coefficients and elements:
        return "·".join(coefficients + elements)
    return "; ".join(numeric) or "не задана"


def restriction(node):
    """Preserve ≤/≥, intervals and symbolic formulas; never turn a bound into a value."""
    kind = node.get("meta") or label(node)
    vals = [decimal(n["value"]) for n, _, _ in leaves(node) if n.get("meta") == "Числовое значение"]
    vals = [v for v in vals if v is not None]
    if kind == "Расчетное":
        interval = next((n for n, _, _ in located_nodes(node) if label(n) == "Интервал"), node)
        sides = children(interval)
        return {"text": "расчётное: " + " — ".join(expression(n) for n in sides), "lower": None, "upper": None, "kind": kind}
    if kind in {"Числовой интервал", "Интервал"}:
        low = next((n for n in children(node) if label(n) == "Нижняя граница"), {})
        high = next((n for n in children(node) if label(n) == "Верхняя граница"), {})
        lo, hi = expression(low), expression(high)
        return {"text": lo + " — " + hi, "lower": decimal(lo), "upper": decimal(hi), "kind": kind}
    if kind in {"Не более", "Не менее"} and len(vals) == 1:
        return {"text": ("≤ " if kind == "Не более" else "≥ ") + number(vals[0]),
                "lower": vals[0] if kind == "Не менее" else None,
                "upper": vals[0] if kind == "Не более" else None, "kind": kind}
    if len(vals) == 1 and kind in {"Точное значение", "Числовое значение", "Значение"}:
        return {"text": number(vals[0]), "lower": vals[0], "upper": vals[0], "kind": kind}
    return {"text": "ограничение не распознано", "lower": None, "upper": None, "kind": kind}


def composition(node):
    section = next((n for n in children(node) if n.get("meta") == "Элементный состав"), None)
    if section is None:
        return [], ""
    base = next((label(v) for n in children(section) if n.get("meta") == "Основа" for v in children(n)), "")
    result = []
    for i, component in enumerate(children(section)):
        if component.get("meta") != "Компонент":
            continue
        for e in children(component):
            if e.get("meta") != "Химический элемент":
                continue
            constraints = [n for n in children(e) if n.get("meta") != "%"]
            units = [label(n) for n in children(e) if n.get("meta") == "%"]
            value = restriction(constraints[0]) if len(constraints) == 1 else {"text":"ограничение отсутствует или неоднозначно", "lower":None,"upper":None,"kind":"unknown"}
            result.append({"element": label(e), "base": label(e) == base, "unit": "; ".join(units), **value})
    return result, base


def safe(value):
    return str(value).replace("|", " / ").replace("\n", " ").replace("\r", " ")


def table(columns, rows):
    return "\n".join(["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns),
                       *("| " + " | ".join(safe(v) for v in row) + " |" for row in rows)])


def resolve_structure_resources(subject, paths):
    """Resolve resource names/aliases individually, never match a whole phrase as a path.

    Generic ontology selection is explicit; unknown clauses block the query rather
    than silently broadening it. The same resolver is used by execution and replay.
    """
    text=subject.casefold().replace("ё","е").strip()
    if not text:
        return list(paths), []
    exact=[p for p in paths if normalize(text) in {normalize(p),normalize(p.rsplit("/",1)[-1])}]
    if exact:
        return exact, []
    ontology="онтолог" in text
    scope=[p for p in paths if not ontology or p.rsplit("/",1)[-1].startswith("Онтология")]
    selected=[]
    unresolved=[]
    # Do not split "характеристик и их значений": those words name one resource.
    text=text.replace("характеристик и их значений","характеристик значений")
    clauses=re.split(r"\s+и\s+|[;,]",text)
    for clause in clauses:
        clause=clause.strip()
        if not clause:
            continue
        if "свойств" in clause:
            candidates=[p for p in scope if "свойств" in p.casefold()]
        elif "характерист" in clause:
            candidates=[p for p in scope if "характерист" in p.casefold()]
        elif "материал" in clause:
            candidates=[p for p in scope if "материал" in p.casefold() and "свойств" not in p.casefold()]
        elif "химическ" in clause:
            candidates=[p for p in scope if "химическ" in p.casefold()]
        elif "терминолог" in clause:
            candidates=[p for p in scope if "терминолог" in p.casefold()]
        elif "единиц" in clause:
            candidates=[p for p in scope if "единиц" in p.casefold()]
        elif re.fullmatch(r"(?:(?:все|всех|подключенные|подключенных)\s+)*онтологи[яийю]*",clause):
            candidates=scope
        else:
            candidates=[p for p in scope if normalize(clause) in normalize(p)]
        if not candidates:
            unresolved.append(clause)
        selected.extend(p for p in candidates if p not in selected)
    return ([] if unresolved else selected), unresolved


def structure_target(subject,paths,root=''):
    matches=[]
    for path in paths:
        for prefix in (path,path.rsplit('/',1)[-1]):
            if subject.casefold().startswith(prefix.casefold()+'/'):
                matches.append((len(prefix),path,subject[len(prefix)+1:]))
    if matches:
        longest=max(m[0] for m in matches);matches=[m for m in matches if m[0]==longest]
        if len({m[1] for m in matches})!=1:return [],['Уточните ресурс для ветки'],root
        _,path,embedded=matches[0]
        if root and normalize(root)!=normalize(embedded):return [],['Заданы разные пути ветки'],root
        return [path],[],root or embedded
    selected,unresolved=resolve_structure_resources(subject,paths)
    if root and len(selected)!=1:return [],['Для ветки нужен один ресурс'],root
    return selected,unresolved,root


def verification_status(data):
    if not data.get("complete"):
        return "incomplete_result"
    if data.get("status")!="ok":
        return "needs_clarification"
    if data.get('plan',{}).get('operation')=='overview' and data.get('entity'):return 'verified_result'
    return "verified_result" if data.get("rows") else "empty_result"


def query_graphs(plan, graphs, configured_paths):
    """Pure, replayable operation. Graphs map resource paths to archived exports."""
    plan = KnowledgePlan.model_validate(plan)
    op, subject = plan.operation, plan.subject.strip()
    result = {"status":"ok", "plan":plan.model_dump(), "rows":[], "total":0, "warnings":[], "references":[]}
    refs = result["references"]
    dataset = hashlib.sha256(json.dumps({path:{k:v for k,v in graph.items() if k!="_source"} for path,graph in graphs.items()}, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode()).hexdigest()
    result["dataset"] = dataset
    def reference(path, pointer, title):
        item = {"resource":path,"pointer":pointer,"title":title}
        if item not in refs:
            refs.append(item)
    def finish(text):
        if op in {"materials", "rank", "filter", "structure"} or plan.conditions:
            result["offset"] = plan.offset
            result["limit"] = plan.limit
            result["has_more"] = plan.offset + len(result["rows"]) < result["total"]
            result["next_offset"] = plan.offset + len(result["rows"]) if result["has_more"] else None
            if plan.offset:
                text = f"Страница с позиции {plan.offset + 1}. Всего: {result['total']}.\n\n" + text
            if result["has_more"]:
                text += f"\n\nЕсть продолжение: offset={result['next_offset']}, limit={plan.limit}."
        result["answer"] = text
        return result
    if plan.dataset and plan.dataset != dataset:
        result["status"] = "clarify"
        return finish("Содержимое базы изменилось между страницами. Начните поиск заново, чтобы не пропустить и не повторить материалы.")
    if op == 'overview':
        from .overview import evaluate
        result.update(evaluate(plan,graphs,configured_paths))
        return finish(result['answer'])
    if op == "clarify":
        result["status"] = "clarify"
        questions={"bound":"В базе содержание задано диапазонами и ограничениями. Рейтинг нужен по верхней или по нижней границе содержания? Это разные результаты.",
                   "compound":"Не удалось однозначно разобрать все условия. Уточните элементы, пороги и связь И/ИЛИ; ни одно условие не будет отброшено молча.",
                   "property":"Вы спрашиваете числовое свойство конкретного материала. Доступный справочник свойств описывает понятия и шкалы; из него нельзя получить измеренную величину для сплава. Уточните источник измерений или запросите описание свойства.",
                   "scope":"Я работаю с подключёнными базами материалов IACPaaS. Можно спросить состав марки, химический элемент, описание свойства, список материалов или рейтинг по границе состава.",
                   "entity":"Уточните марку материала, свойство или условие поиска. Можно спросить: «Состав 12Х18Н10Т», «Что есть о Fe?», «Сколько бронз?»"}
        return finish(questions[plan.clarification])
    if op == "resources":
        result["rows"] = [[path.rsplit("/",1)[-1], path] for path in configured_paths]
        result["total"] = len(configured_paths)
        return finish(f"Подключено ресурсов: **{len(configured_paths)}**.\n\n" + table(["Ресурс","Путь"], result["rows"]))
    if op=="catalog":
        result["catalog"]={"classes":[],"elements":[],"properties":[],"resources":configured_paths}
        for path,g in graphs.items():
            if path.endswith("/Справочник по материалам"):
                result["catalog"]["classes"]=[label(n) for n in children(g)]
            elif path.endswith("/База химических элементов и веществ"):
                result["catalog"]["elements"]=[{"name":label(n),"symbol":next((label(c) for c in children(n) if c.get("meta")=="обозначение"),"")} for n,_,_ in located_nodes(g) if n.get("meta")=="Химический элемент" and "name" in n]
            elif path.endswith("/База свойств материалов"):
                result["catalog"]["properties"]=[label(n) for n,_,_ in located_nodes(g) if n.get("meta")=="Свойство"]
        result["answer"]="Справочный каталог для разбора запроса."
        return result
    nodes = [(path,n,p,ptr) for path,g in graphs.items() for n,p,ptr in located_nodes(g)]
    materials = [(r,n,p,ptr) for r,n,p,ptr in nodes if n.get("meta") == "Материал" and "name" in n]
    materials.sort(key=lambda item:(item[0],item[2]))
    if plan.class_name:
        classes=list(dict.fromkeys(part for _,_,p,_ in materials for part in p.split("/")[:-1]))
        resolved=resolve_material_class(plan.class_name,classes)
        if resolved is None:
            result["status"]="clarify"
            return finish(f"Класс **{plan.class_name}** не распознан. Выберите класс из базы: "+"; ".join(classes)+".")
        result["resolved_class"]=resolved
        if subject and resolve_material_class(subject,classes)==resolved:
            # A class name repeated in subject is not a material-name filter.
            subject=""
        materials = [item for item in materials if resolved in item[2].split("/")[:-1]]
    if op == "structure":
        selected,unresolved,branch_root=structure_target(subject,configured_paths,plan.structure_root)
        if unresolved:
            result["status"]="clarify"
            return finish("Не удалось однозначно выбрать ресурсы: **"+"; ".join(unresolved)+"**. Укажите названия из подключённого списка:\n\n"+"\n".join("- "+path.rsplit("/",1)[-1] for path in configured_paths))
        result["resources"]=selected
        result["available_ontologies"]=[p for p in configured_paths if p.rsplit("/",1)[-1].startswith("Онтология")]
        for path,g in graphs.items():
            if path not in selected:
                continue
            from .graph import select
            try:branch=select(g,branch_root)
            except ValueError:
                result['status']='not_found'
                return finish('Ветка **'+branch_root+'** не найдена. Найдите точный путь через query_graph или раскройте get_children; родительские разделы не заменяют запрошенную ветку.')
            base_pointer=next((ptr for n,p,ptr in located_nodes(g) if p==branch_root),'') if branch_root else ''
            for i,n in enumerate(children(branch)):
                result["rows"].append([g.get("name",path),label(n),n.get("meta","")])
                reference(path,base_pointer+f"/successors/{i}",label(n))
        result["total"] = len(result["rows"])
        result["rows"] = result["rows"][plan.offset:plan.offset+plan.limit]
        result["references"] = refs[plan.offset:plan.offset+plan.limit]
        if not result["rows"]:
            result["status"]="empty"
            return finish("В выбранных ресурсах не получены понятия первого уровня. Проверьте доступность и содержимое ресурсов; пустой результат не подтверждает успешное выполнение вопроса.")
        prefix=""
        if "онтолог" in subject.casefold():
            prefix="Подключённые онтологии:\n\n"+"\n".join("- "+p.rsplit("/",1)[-1] for p in result["available_ontologies"])+"\n\n"
        return finish(prefix+(f'Непосредственные дочерние вершины ветки **{branch_root}**:' if branch_root else "Понятия первого уровня выбранных ресурсов:")+"\n\n" + table(["Ресурс","Понятие","Тип"], result["rows"])+ (f"\n\nПоказано {len(result['rows'])} из {result['total']} понятий." if result["total"]>len(result["rows"]) else ""))
    if op == "materials" and not plan.conditions:
        chosen = [item for item in materials if not subject or normalize(subject) in normalize(label(item[1]))]
        required=list(dict.fromkeys([*plan.elements,*([plan.element] if plan.element else [])]))
        if required:
            wanted={normalize(s) for s in required}
            chosen=[item for item in chosen if (wanted<={normalize(c["element"]) for c in composition(item[1])[0]} if plan.element_mode=="all" else bool(wanted & {normalize(c["element"]) for c in composition(item[1])[0]}))]
        result["total"] = len(chosen)
        for r,n,p,ptr in chosen[plan.offset:plan.offset+plan.limit]:
            result["rows"].append([label(n),p.rsplit("/",1)[0]])
            reference(r,ptr,label(n))
        heading=(("Все компоненты" if plan.element_mode=="all" else "Хотя бы один из компонентов")+f" **{', '.join(required)}** указаны в составах **{len(chosen)}** материалов" if required else f"Материалов по условию: **{len(chosen)}**")
        if result.get("resolved_class"):heading+=". Класс: **"+result["resolved_class"]+"**"
        return finish(heading + (f". Показаны позиции {plan.offset+1}–{min(plan.offset+plan.limit,len(chosen))}." if result["rows"] and len(chosen)>plan.limit else ".") + "\n\n" + table(["Марка","Класс"],result["rows"]) + ("\n\nПодсчёт выполнен по указанию компонентов в базе. Ограничение вида «не более» не доказывает ненулевое содержание в конкретной партии." if required else ""))
    if op in {"rank", "filter"} or plan.conditions:
        if not plan.conditions and (not plan.element or (op == "filter" and plan.threshold is None)):
            result["status"] = "clarify"
            return finish("Укажите элемент и числовое условие. Например: материалы с верхней границей Cr не более 19%. Для рейтинга уточните верхнюю или нижнюю границу содержания.")
        qualified, unknown, absent = [], 0, 0
        threshold = decimal(plan.threshold)
        conditions = plan.conditions or ([CompositionCondition(element=plan.element, op=plan.comparator, value=plan.threshold)] if op == "filter" else [])
        for r,n,p,ptr in materials:
            comps,_ = composition(n)
            by_element = {normalize(v["element"]): v for v in comps}
            if conditions:
                passed = [satisfies(by_element.get(normalize(c.element)), c) for c in conditions]
                if not (all(passed) if plan.condition_mode == "all" else any(passed)):
                    continue
                if not plan.element or op != "rank":
                    qualified.append((None, r, n, p, ptr, {"text": "; ".join(c.element + ": " + (by_element.get(normalize(c.element), {}).get("text") or "не указан") for c in conditions), "unit": "%"}))
                    continue
            component = by_element.get(normalize(plan.element))
            if component is None:
                absent += 1
                continue
            # Filters prove the condition across the entire interval, not just one endpoint.
            key = plan.bound if op == "rank" else ("upper" if plan.comparator == "le" else "lower")
            value = component[key]
            if value is None or component["unit"] != "%":
                unknown += 1
                continue
            if not conditions and op == "filter" and not (value<=threshold if plan.comparator=="le" else value>=threshold):
                continue
            qualified.append((value,r,n,p,ptr,component))
        qualified.sort(key=lambda item:((item[0] if item[0] is not None else Decimal(0)),normalize(label(item[2]))), reverse=plan.order=="desc")
        result["total"] = len(qualified)
        result["examined"] = len(materials)
        result["unknown"] = unknown
        result["without_element"] = absent
        for value,r,n,p,ptr,c in qualified[plan.offset:plan.offset+plan.limit]:
            result["rows"].append([label(n),c["text"],c["unit"]] + ([number(value)] if value is not None else []))
            reference(r,ptr,label(n))
        if plan.conditions:
            criterion = (" И " if plan.condition_mode=="all" else " ИЛИ ").join(c.element + " " + c.op + (" " + str(c.value) + "%" if c.value is not None else "") for c in conditions)
            if not plan.element or op != "rank":
                return finish(f"Условия: **{criterion}**. Подходит материалов: **{len(qualified)}**, проверено: {len(materials)}.\n\n" + table(["Материал","Указанные ограничения состава","Единица"], result["rows"]) + "\n\nЧисловое условие должно выполняться на всём допустимом диапазоне. «Не указан» означает отсутствие записи, а не нулевое содержание.")
        else:
            criterion = ("верхней" if plan.bound=="upper" else "нижней") + " границе" if op=="rank" else ("≤ " if plan.comparator=="le" else "≥ ")+str(plan.threshold)+"%"
        return finish(("Сравниваю по верхней допустимой границе содержания: фактическое содержание партий в базе не задано. Для другого сравнения можно указать нижнюю границу.\n\n" if plan.bound_inferred else "")+f"Элемент **{plan.element}**. " + (f"Рейтинг по {criterion}" if op=="rank" else f"Условие: весь допустимый диапазон {criterion}") + f". Подходит материалов: **{len(qualified)}**, проверено: {len(materials)}.\n\n" + table(["Материал","Содержание","Единица","Граница для отбора"],result["rows"]) + f"\n\nНе включены: нет числовой границы/единицы — {unknown}; элемент не указан — {absent}. Отсутствие элемента не означает нулевое содержание. Расчётные формулы не подменяются числами.")
    if op in {"composition", "compare"}:
        subjects = [x.strip() for x in subject.split(";") if x.strip()]
        if not subjects:
            result["status"]="clarify"
            return finish("Укажите точную марку материала, состав которого нужно показать.")
        parts=[]
        for wanted in subjects[:4]:
            exact=[item for item in materials if normalize(label(item[1]))==normalize(wanted)]
            if len(exact)!=1:
                result["status"]="ambiguous" if exact else "not_found"
                candidates=[label(n) for _,n,_,_ in materials if normalize(wanted) in normalize(label(n))][:8]
                return finish(f"Марка **{wanted}** " + ("неоднозначна." if exact else "не найдена точным совпадением.") + (" Уточните: "+", ".join(candidates) if candidates else " Укажите точную марку или другой класс материалов."))
            r,n,p,ptr=exact[0]
            comps,base=composition(n)
            rows=[[c["element"]+(" (основа)" if c["base"] else ""),c["text"],c["unit"] or "не указана"] for c in comps]
            result["rows"].extend([[label(n),*row] for row in rows])
            reference(r,ptr,label(n))
            parts.append(f"**{label(n)}** — {p.rsplit('/',1)[0]}." + (f" Основа: **{base}**." if base else "") + "\n\n" + (table(["Элемент","Содержание / ограничение","Единица"],rows) if rows else "Элементный состав в базе не указан."))
        result["total"]=len(result["rows"])
        return finish("\n\n".join(parts))
    meta = {"element":"Химический элемент","property":"Свойство","term":"Термин"}[op]
    # Element lookup matches its designation child as well as its exact Russian name.
    candidates=[item for item in nodes if item[1].get("meta")==meta and "name" in item[1] and
                (normalize(label(item[1]))==normalize(subject) or normalize(item[2])==normalize(subject) or (op=="element" and any(normalize(label(c))==normalize(subject) for c in children(item[1]))))]
    if not candidates and subject:
        candidates=[item for item in nodes if item[1].get("meta")==meta and "name" in item[1] and normalize(subject) in normalize(label(item[1]))]
    if len(candidates)!=1:
        result["status"]="ambiguous" if candidates else "not_found"
        return finish(("Найдено несколько вариантов: "+"; ".join(p for _,n,p,_ in candidates[:12])+". Уточните раздел и название.") if candidates else f"**{subject}** в выбранных справочниках не найдено. Это не подтверждает отсутствие свойства у материала; данных для такого ответа нет.")
    r,n,p,ptr=candidates[0]
    reference(r,ptr,label(n))
    rows=[]
    if n.get("comment"):
        rows.append(["Описание",n["comment"]])
    if op=="term":
        rows.extend([leaf.get("meta",path),leaf["value"]] for leaf,path,_ in leaves(n))
    for child in children(n):
        if op=="term":
            continue
        if "value" in child:
            rows.append([child.get("meta","Значение"),child["value"]])
        elif child.get("comment"):
            rows.append([label(child),child["comment"]])
        elif op=="property" and child.get("meta")=="Характеристика":
            rows.append([label(child),"характеристика; конкретное значение не задано"])
    if op=="element":
        substances=[]
        names={normalize(label(n)),normalize(subject),*(normalize(label(c)) for c in children(n) if c.get("meta")=="обозначение")}
        for rr,nn,pp,pt in nodes:
            if nn.get("meta")=="Химическое вещество" and any(normalize(label(v)) in names for v,_,_ in located_nodes(nn) if v is not nn):
                substances.append(label(nn))
        if substances:
            rows.append(["Связанные вещества", ", ".join(sorted(set(substances)))])
    result["rows"]=rows
    result["total"]=len(rows)
    return finish(f"**{label(n)}**\n\n" + (table(["Поле","Данные базы"],rows) if rows else "В базе есть карточка, но описание и значения не заполнены.") + ("\n\nЭто описание свойства и шкал, а не измеренное значение для конкретного сплава." if op=="property" else ""))


def resource_needed(path, operation):
    title=path.rsplit("/",1)[-1]
    if operation in {"resources","clarify"}:
        return False
    if operation=="structure":
        return True
    if operation=="catalog":
        return any(path.endswith("/"+name) for name in ("Справочник по материалам","База химических элементов и веществ","База свойств материалов"))
    if title.startswith("Онтология"):
        return False
    if operation=='overview':
        return True
    return {"composition":"Справочник по материалам", "compare":"Справочник по материалам", "materials":"Справочник по материалам", "rank":"Справочник по материалам", "filter":"Справочник по материалам", "element":"База химических элементов и веществ", "property":"База свойств материалов", "term":"Терминологический справочник"}.get(operation)==title


async def execute_plan(search, paths, plan):
    plan=KnowledgePlan.model_validate(plan)
    graphs,coverage,sources={ },[],[]
    structure_paths=structure_target(plan.subject,paths,plan.structure_root)[0] if plan.operation=="structure" else None
    for path in paths:
        if structure_paths is not None and path not in structure_paths:
            continue
        if not resource_needed(path,plan.operation):
            continue
        try:
            graph=await search.full_export(path)
            graphs[path]=graph
            sources.append(graph["_source"])
            coverage.append({"path":path,"status":"read"})
        except Exception as exc:
            coverage.append({"path":path,"status":"failed","error":type(exc).__name__})
    data=query_graphs(plan,graphs,paths)
    data["coverage"]=coverage
    data["complete"]=not any(item["status"]=="failed" for item in coverage)
    data["_sources"]=sources
    if not data["complete"]:
        data["answer"]="**Часть ресурсов недоступна. Результат неполный.**\n\n"+data["answer"]
    if not graphs and plan.operation not in {"resources","clarify"} and data["status"]!="clarify":
        data["status"]="unavailable"
        data["answer"]="Нужная информационная база недоступна в выбранной области. Выберите «Все подключённые базы» или проверьте подключение."
    apply_source_notices(data, sources)
    return data


def apply_source_notices(data, sources):
    stale=[s for s in sources if s.get("stale")]
    if stale:
        notice="**Платформа недоступна. Использован резервный снимок: **" + "; ".join(s["path"] + " от " + s["retrieved_at"] for s in stale)
        data["warnings"].append(notice)
        data["answer"]=notice+"\n\n"+data["answer"]


def verify_result(data, paths):
    """Replay both calculations AND final display against SHA-checked raw snapshots."""
    from . import provenance
    graphs={}
    needed=set(structure_target(data["plan"].get("subject",""),paths,data['plan'].get('structure_root',''))[0]) if data["plan"]["operation"]=="structure" else {p for p in paths if resource_needed(p,data["plan"]["operation"])}
    coverage=data.get("coverage",[])
    if len(coverage)!=len(needed) or {c.get("path") for c in coverage}!=needed:
        return False
    if any(c.get("status") not in {"read","failed"} for c in coverage):
        return False
    read={c["path"] for c in coverage if c["status"]=="read"}
    if data.get("complete") is not (len(read)==len(needed)):
        return False
    sources=data.get("_sources",[])
    if len(sources)!=len(read) or {s.get("path") for s in sources}!=read:
        return False
    for source in data.get("_sources",[]):
        if source.get("path") not in paths or not provenance.verify_snapshot(source):
            return False
        with (provenance.SNAPSHOTS/(source["export_sha256"]+".json")).open(encoding="utf-8") as handle:
            graphs[source["path"]]=json.load(handle)
    expected=query_graphs(data["plan"],graphs,paths)
    if not data.get("complete"):
        expected["answer"]="**Часть ресурсов недоступна. Результат неполный.**\n\n"+expected["answer"]
    if not graphs and data["plan"]["operation"] not in {"resources","clarify"} and expected["status"]!="clarify":
        expected["status"]="unavailable"
        expected["answer"]="Нужная информационная база недоступна в выбранной области. Выберите «Все подключённые базы» или проверьте подключение."
    apply_source_notices(expected, sources)
    # New optional plan fields must not invalidate historical proofs with identical defaults.
    actual_plan=KnowledgePlan.model_validate(data['plan']).model_dump()
    return all((actual_plan if key=='plan' else data.get(key))==value for key,value in expected.items())
