import asyncio
import copy
import json
from decimal import Decimal

import httpx
import pytest
from mcp import Client

from iacpaas_mcp import provenance
from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.knowledge import query_graphs, restriction, verify_result, execute_plan
from iacpaas_mcp.planner_agent import dialogue_context, run_planned_agent
from iacpaas_mcp.search import ResourceSearch
from iacpaas_mcp.server import build_server

MATERIALS="Загрузки/Справочник по материалам"
CHEMISTRY="Загрузки/База химических элементов и веществ"


def value(number):
    return {"meta":"Числовое значение","value":number}


def component(symbol, low, high, kind="Числовой интервал"):
    bounds=[{"name":"Нижняя граница","successors":[value(low)]},{"name":"Верхняя граница","successors":[value(high)]}]
    restriction_node={"name":kind,"successors":bounds if kind=="Числовой интервал" else [value(high)]}
    return {"meta":"Компонент","name":"1","successors":[{"name":symbol,"meta":"Химический элемент","successors":[{"meta":"%","value":"%"},restriction_node]}]}


def material(name, low, high):
    return {"meta":"Материал","name":name,"successors":[{"meta":"Элементный состав","name":"Элементный состав","successors":[component("Cr",low,high)]}]}


def graphs():
    return {MATERIALS:{"name":"Справочник по материалам","successors":[{"name":"Бронзы","meta":"Класс материалов","successors":[material("A",1,3),material("B",5,8),material("C",2,4)]}]},
            CHEMISTRY:{"name":"База химических элементов и веществ","successors":[{"name":"Железо","meta":"Химический элемент","successors":[{"meta":"обозначение","value":"Fe"},{"meta":"порядковый номер","value":26}]}]}}


def test_composition_preserves_ranges_and_source_identity():
    result=query_graphs({"operation":"composition","subject":"B"},graphs(),list(graphs()))
    assert result["rows"]==[["B","Cr","5 — 8","%"]]
    assert result["references"][0]["resource"]==MATERIALS
    assert "Химический элемент" not in result["answer"]


def test_full_ranking_before_pagination_and_exact_counts():
    result=query_graphs({"operation":"rank","element":"Cr","limit":1},graphs(),list(graphs()))
    assert result["rows"][0][0]=="B" and result["total"]==3 and result["examined"]==3


@pytest.mark.parametrize("comparator,threshold,expected",[("le",4,["C","A"]),("ge",3,["B"]),("le",0,[])])
def test_filter_proves_entire_interval(comparator,threshold,expected):
    result=query_graphs({"operation":"filter","element":"Cr","comparator":comparator,"threshold":threshold},graphs(),list(graphs()))
    assert [row[0] for row in result["rows"]]==expected


def test_missing_bound_is_not_zero_or_exact_value():
    node={"name":"Не более","successors":[value(0.3)]}
    parsed=restriction(node)
    assert parsed["lower"] is None and parsed["upper"]==Decimal("0.3")
    assert parsed["text"]=="≤ 0.3"


def test_symbolic_formula_is_not_misinterpreted_as_numeric_interval():
    parsed=restriction({"name":"Расчетное","successors":[{"name":"Интервал","successors":[{"name":"Нижняя граница","successors":[{"meta":"коэффициент","value":5},{"meta":"Химический элемент","value":"C"}]},{"name":"Верхняя граница","successors":[value(0.8)]}]}]})
    assert "5·C" in parsed["text"] and "0.8" in parsed["text"]
    assert parsed["upper"] is None and parsed["lower"] is None


def test_element_designation_resolves_card_not_thousands_of_mentions():
    result=query_graphs({"operation":"element","subject":"Fe"},graphs(),list(graphs()))
    assert result["rows"]==[["обозначение","Fe"],["порядковый номер",26]]
    assert "Железо" in result["answer"]


def test_ambiguous_material_requires_clarification():
    data=graphs()
    data[MATERIALS]["successors"].append({"name":"Другой класс","successors":[material("B",0,1)]})
    result=query_graphs({"operation":"composition","subject":"B"},data,list(data))
    assert result["status"]=="ambiguous" and not result["rows"]


def test_no_invention_of_missing_property():
    result=query_graphs({"operation":"property","subject":"Плотность алюминия"},graphs(),list(graphs()))
    assert result["status"]=="not_found" and "нет" in result["answer"]


def test_count_not_limited_to_displayed_rows():
    result=query_graphs({"operation":"materials","class_name":"Бронзы","limit":1},graphs(),list(graphs()))
    assert len(result["rows"])==1 and result["total"]==3


@pytest.fixture
def backend(tmp_path,monkeypatch):
    monkeypatch.setattr(provenance,"SNAPSHOTS",tmp_path/"exports")
    from iacpaas_mcp import search
    monkeypatch.setattr(search,"INDEX",tmp_path/"index.sqlite3")
    instance=MockBackend()
    instance.resources=graphs()
    return instance


def test_replay_rejects_tampered_final_text_and_wrong_ranking(backend,tmp_path):
    async def run():
        return await execute_plan(ResourceSearch(backend,tmp_path/"index.sqlite3"),list(graphs()),{"operation":"rank","element":"Cr"})
    data=asyncio.run(run())
    assert verify_result(data,list(graphs()))
    changed=copy.deepcopy(data)
    changed["answer"]=changed["answer"].replace("B","A")
    assert not verify_result(changed,list(graphs()))
    changed=copy.deepcopy(data)
    changed["rows"].reverse()
    assert not verify_result(changed,list(graphs()))


def test_planning_model_cannot_replace_verified_answer(backend,tmp_path,monkeypatch):
    requests=[]
    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={"message":{"role":"assistant","content":"Выдуманный состав 100% золота", "tool_calls":[{"function":{"name":"query_knowledge","arguments":{"plan":{"operation":"composition","subject":"B"}}}}]}})
    real_http=httpx.AsyncClient
    monkeypatch.setattr("iacpaas_mcp.planner_agent.httpx.AsyncClient",lambda **kw:real_http(transport=httpx.MockTransport(reply),**kw))
    events=[]
    async def run():
        async with Client(build_server(backend=backend,allow_write=False)) as client:
            return await run_planned_agent(client,"Покажи состав B","test","http://mock.test",tmp_path/"trace.jsonl",on_event=events.append)
    answer=asyncio.run(run())
    assert "5 — 8" in answer and "золота" not in answer and len(requests)==1
    assert next(e for e in events if e["event"]=="verification")["status"]=="verified_result"


def test_structured_memory_retains_early_material_and_conditions():
    history=[{"role":"user","content":"Состав B"},{"role":"assistant","content":"long "*500,"query_plan":{"operation":"composition","subject":"B","element":"Cr","threshold":8,"comparator":"le"}}]
    history.extend({"role":"user" if i%2==0 else "assistant","content":"x"*1000} for i in range(16))
    packed,metrics=dialogue_context(history,1600)
    assert '"subject": "B"' in packed[0]["content"] and '"threshold": 8' in packed[0]["content"]
    assert metrics["compressed"] and len(history)==18


def test_guard_requires_explicit_bound_and_rejects_silently_dropped_condition():
    from iacpaas_mcp.planner_agent import guard_plan
    from iacpaas_mcp.knowledge import KnowledgePlan
    rank=KnowledgePlan(operation="rank",element="Cr")
    assert guard_plan(rank,"Где больше Cr?",[]).bound_inferred is True
    assert guard_plan(rank,"По верхней границе Cr",[]).operation=="rank"
    plan=KnowledgePlan(operation="filter",element="Cr",threshold=19)
    assert guard_plan(plan,"Cr не более 19% и Ni не менее 9%",[]).clarification=="compound"


def test_exact_term_path_disambiguates_and_reads_nested_synonyms():
    path="Загрузки/Терминологический справочник"
    data={path:{"name":"Терминологический справочник","successors":[{"name":group,"successors":[{"name":"сферическая","meta":"Термин","successors":[{"name":"Синонимы","successors":[{"meta":"синоним","value":"шаровая"}]}]}]} for group in ["Порошки","Фазы"]]}}
    result=query_graphs({"operation":"term","subject":"сферическая"},data,[path])
    assert result["status"]=="ambiguous" and "Порошки/сферическая" in result["answer"]
    result=query_graphs({"operation":"term","subject":"Порошки/сферическая"},data,[path])
    assert result["status"]=="ok" and result["rows"]==[["синоним","шаровая"]]


def test_verification_rejects_falsified_resource_coverage(backend,tmp_path):
    data=asyncio.run(execute_plan(ResourceSearch(backend,tmp_path/"index.sqlite3"),list(graphs()),{"operation":"composition","subject":"A"}))
    data["coverage"]=[]
    assert not verify_result(data,list(graphs()))


@pytest.mark.parametrize("subject",["онтологии материалов и свойств материалов","онтология материалов; онтология свойств материалов","Онтология справочника по материалам; Онтология базы свойств материалов"])
def test_structure_resolves_multiple_resource_names(subject):
    paths=["Загрузки/Онтология справочника по материалам","Загрузки/Онтология базы свойств материалов","Загрузки/Справочник по материалам"]
    data={paths[0]:{"name":"Онтология справочника по материалам","successors":[{"name":"Класс материалов","meta":"описать элемент списка"}]},
          paths[1]:{"name":"Онтология базы свойств материалов","successors":[{"name":"Класс свойств","meta":"описать элемент списка"}]},
          paths[2]:{"name":"Справочник по материалам","successors":[{"name":"Бронзы"}]}}
    result=query_graphs({"operation":"structure","subject":subject},data,paths)
    assert result["status"]=="ok" and result["total"]==2
    assert {row[1] for row in result["rows"]}=={"Класс материалов","Класс свойств"}
    assert result["resources"]==paths[:2]
    assert "Подключённые онтологии" in result["answer"]


def test_unknown_structure_does_not_silently_return_empty_verified_table():
    from iacpaas_mcp.knowledge import verification_status
    result=query_graphs({"operation":"structure","subject":"несуществующий справочник"},graphs(),list(graphs()))
    result["complete"]=True
    assert result["status"]=="clarify" and verification_status(result)=="needs_clarification"
    assert "|---" not in result["answer"]


@pytest.mark.parametrize("status,complete,rows,expected",[("empty",True,[],"needs_clarification"),("ok",True,[],"empty_result"),("ok",False,[[1]],"incomplete_result"),("ok",True,[[1]],"verified_result")])
def test_verification_badge_requires_useful_complete_result(status,complete,rows,expected):
    from iacpaas_mcp.knowledge import verification_status
    assert verification_status({"status":status,"complete":complete,"rows":rows})==expected


def test_structure_execution_reads_only_resolved_resources_and_replays(backend,tmp_path):
    first="Загрузки/Онтология справочника по материалам"
    second="Загрузки/Онтология базы свойств материалов"
    backend.resources={first:{"name":first.split('/')[-1],"successors":[{"name":"Класс материалов"}]},second:{"name":second.split('/')[-1],"successors":[{"name":"Класс свойств"}]},**graphs()}
    data=asyncio.run(execute_plan(ResourceSearch(backend,tmp_path/"index.sqlite3"),list(backend.resources),{"operation":"structure","subject":"онтологии материалов и свойств материалов"}))
    assert len(data["coverage"])==2 and data["total"]==2
    assert verify_result(data,list(backend.resources))


def test_structure_guard_preserves_specific_targets_after_general_overview():
    from iacpaas_mcp.knowledge import KnowledgePlan
    from iacpaas_mcp.planner_agent import guard_plan
    question="Какие онтологии подключены? Покажи, какие понятия первого уровня описывают онтология материалов и онтология свойств материалов."
    plan=guard_plan(KnowledgePlan(operation="structure",subject="онтологии"),question,[])
    assert plan.subject=="онтология материалов; онтология свойств материалов"


def test_long_context_retains_more_than_six_messages_and_never_mutates_history():
    history=[{"role":"user" if i%2==0 else "assistant","content":"История материала "*50} for i in range(40)]
    original=copy.deepcopy(history)
    small,small_metrics=dialogue_context(history,4500)
    large,large_metrics=dialogue_context(history,21000)
    assert large_metrics["retained_messages"]>6
    assert large_metrics["retained_messages"]>small_metrics["retained_messages"]
    assert large_metrics["after_estimate"]<=21000 and small_metrics["after_estimate"]<=4500
    assert history==original


def test_question_bank_has_valid_plans_and_diverse_real_database_questions():
    from pathlib import Path
    from iacpaas_mcp.knowledge import KnowledgePlan
    bank=json.loads((Path(__file__).parents[1]/"iacpaas_mcp/static/questions.json").read_text(encoding="utf-8"))
    assert len(bank)>=28 and len({q["id"] for q in bank})==len(bank)
    assert len({q["category"] for q in bank})>=8
    for question in bank:
        KnowledgePlan.model_validate(question["plan"])
        assert len(question["question"])>10


def test_runner_recovery_retries_identical_question_and_context_once(backend,tmp_path,monkeypatch):
    requests=[]
    def reply(request):
        if request.url.path=="/api/generate":return httpx.Response(200,json={"done":True})
        if request.url.path=="/api/ps":return httpx.Response(200,json={"models":[]})
        requests.append(json.loads(request.content))
        if len(requests)==1:return httpx.Response(500,json={"error":"runner initialization failed"})
        return httpx.Response(200,json={"message":{"role":"assistant","content":json.dumps({"operation":"composition","subject":"B"})}})
    real_http=httpx.AsyncClient
    monkeypatch.setattr("iacpaas_mcp.planner_agent.httpx.AsyncClient",lambda **kw:real_http(transport=httpx.MockTransport(reply),**kw))
    events=[]
    async def run():
        async with Client(build_server(backend=backend,allow_write=False)) as client:
            return await run_planned_agent(client,"Состав B","test","http://mock.test",tmp_path/"trace.jsonl",on_event=events.append)
    assert "5 — 8" in asyncio.run(run())
    assert len(requests)==2 and requests[0]==requests[1]
    assert sum(e["event"]=="model_retry" for e in events)==1


def test_count_of_component_is_not_changed_into_ranking():
    from iacpaas_mcp.planner_agent import guard_plan
    from iacpaas_mcp.knowledge import KnowledgePlan
    plan=guard_plan(KnowledgePlan(operation="rank",element="Cr"),"Сколько материалов содержат Cr?",[])
    assert plan.operation=="materials" and plan.element=="Cr" and not plan.class_name
    result=query_graphs(plan,graphs(),list(graphs()))
    assert result["total"]==3


def test_aluminium_symbol_and_name_resolve_to_existing_class():
    from iacpaas_mcp.knowledge import resolve_material_class
    classes=["Бронзы","Сплавы на основе алюминия","Сплавы на основе железа"]
    for text in ["Al","Алюминий","алюминиевые сплавы"]:
        assert resolve_material_class(text,classes)=="Сплавы на основе алюминия"


def test_count_with_two_elements_does_not_drop_second_condition():
    from iacpaas_mcp.planner_agent import guard_plan
    from iacpaas_mcp.knowledge import KnowledgePlan
    data=graphs()
    data[MATERIALS]["successors"][0]["successors"][0]["successors"][0]["successors"].append(component("Ni",9,11))
    plan=guard_plan(KnowledgePlan(operation="clarify"),"Сколько материалов содержат Cr и Ni?",[])
    assert set(plan.elements)=={"Cr","Ni"}
    assert query_graphs(plan,data,list(data))["total"]==1


def test_explicit_not_less_than_overrides_wrong_model_comparator():
    from iacpaas_mcp.planner_agent import guard_plan
    from iacpaas_mcp.knowledge import KnowledgePlan
    plan=guard_plan(KnowledgePlan(operation="filter",element="Ni",comparator="le",threshold=90),"Материалы с Ni не менее 9%",[])
    assert plan.comparator=="ge" and plan.threshold==9


def test_class_repeated_in_subject_is_not_used_as_grade_name_filter():
    data=query_graphs({"operation":"materials","class_name":"Бронзы","subject":"Бронзы"},graphs(),list(graphs()))
    assert data["total"]==3


@pytest.mark.parametrize("question",["Сколько материалов содержат Cr?","В скольких сплавах в составе есть хром?","Посчитай материалы с Cr."])
def test_count_paraphrases_preserve_intent(question):
    from iacpaas_mcp.planner_agent import guard_plan
    from iacpaas_mcp.knowledge import KnowledgePlan
    plan=guard_plan(KnowledgePlan(operation="rank",element="Cr"),question,[])
    assert plan.operation=="materials" and plan.element=="Cr"


def test_quantity_of_element_in_one_material_is_not_count_of_materials():
    from iacpaas_mcp.planner_agent import guard_plan
    from iacpaas_mcp.knowledge import KnowledgePlan
    plan=guard_plan(KnowledgePlan(operation="composition",subject="12Х18Н10Т"),"Какое количество Cr в материале 12Х18Н10Т?",[])
    assert plan.operation=="composition"


def test_presence_any_mode_uses_union_not_intersection():
    data=graphs()
    extra=material("D",1,2)
    extra["successors"][0]["successors"]=[component("Ni",9,11)]
    data[MATERIALS]["successors"][0]["successors"].append(extra)
    result=query_graphs({"operation":"materials","elements":["Cr","Ni"],"element_mode":"any"},data,list(data))
    assert result["total"]==4
