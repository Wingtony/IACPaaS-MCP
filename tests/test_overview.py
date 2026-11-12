import asyncio
import copy
import json

import pytest

from iacpaas_mcp import provenance,search
from iacpaas_mcp.backend import MockBackend,PlatformError
from iacpaas_mcp.knowledge import KnowledgePlan,query_graphs,execute_plan,verify_result
from iacpaas_mcp.planner_agent import guard_plan,contextual_plan
from iacpaas_mcp.request_scope import capabilities_request,request_coverage

CHEM='Загрузки/База химических элементов и веществ'
MATERIALS='Загрузки/Справочник по материалам'


def graphs():
    iron={'id':10,'type':'НЕТЕРМИНАЛ','meta':'Химический элемент','name':'Железо','successors':[
        {'type':'ТЕРМИНАЛ-ЗНАЧЕНИЕ','meta':'обозначение','value':'Fe','valtype':'STRING'},
        {'type':'ТЕРМИНАЛ-ЗНАЧЕНИЕ','meta':'порядковый номер','value':26,'valtype':'INTEGER'}]}
    def substance(name,formula):return {'type':'НЕТЕРМИНАЛ','meta':'Химическое вещество','name':name,'successors':[{'meta':'обозначение','value':formula}]}
    linked={'type':'НЕТЕРМИНАЛ','meta':'Химическое вещество','name':'Ссылка на элемент','successors':[{'link':'target','id':10}]}
    def material(name,symbol):return {'type':'НЕТЕРМИНАЛ','meta':'Материал','name':name,'successors':[
        {'meta':'Элементный состав','name':'Элементный состав','successors':[{'meta':'Компонент','name':'1','successors':[
            {'meta':'Химический элемент','name':symbol,'successors':[{'meta':'%','value':'%'},{'name':'Числовое значение','meta':'Числовое значение','value':99}]}]}]}]}
    return {CHEM:{'type':'КОРЕНЬ','name':'Химия','successors':[iron,substance('Оксид железа','Fe₂O₃'),substance('Вода','H₂O'),substance('Не формула','Feather'),linked]},
            MATERIALS:{'type':'КОРЕНЬ','name':'Материалы','successors':[material('A','Fe'),material('B','Fe'),material('C','Ni')]}}


def overview(**options):
    data=graphs()
    return query_graphs({'operation':'overview','subject':'Fe',**options},data,list(data))


def test_overview_reads_attributes_formulas_id_links_and_material_composition():
    result=overview()
    assert result['entity']['name']=='Железо'
    assert result['attribute_total']==2 and '26' in result['answer']
    assert result['related']['substances']['total']==2
    assert {r['name'] for r in result['related']['substances']['items']}=={'Оксид железа','Ссылка на элемент'}
    assert result['related']['materials']['total']==2
    assert {r['name'] for r in result['related']['materials']['items']}=={'A','B'}
    assert len({r['resource'] for r in result['references']})==2


def test_related_totals_are_full_even_when_display_is_limited():
    result=overview(limit=1)
    assert result['related']['materials']['total']==2
    assert len(result['related']['materials']['items'])==1 and result['related']['materials']['has_more']


def test_empty_relations_are_explicit_not_silently_omitted():
    data=graphs();data[CHEM]['successors']=data[CHEM]['successors'][:1];data[MATERIALS]['successors']=[]
    result=query_graphs({'operation':'overview','subject':'Железо'},data,list(data))
    assert result['sections'][1]['status']=='no_matches'
    assert 'В проверенных данных такие связи не найдены' in result['answer']
    assert 'не доказывает' in result['answer']


def test_ambiguous_entity_is_not_guessed():
    data=graphs();other=copy.deepcopy(data[CHEM]['successors'][0]);other['id']=99
    data[CHEM]['successors'].append(other)
    assert query_graphs({'operation':'overview','subject':'Fe'},data,list(data))['status']=='ambiguous'


@pytest.mark.parametrize('question',['Расскажи вообще всё о железке','Мне нужно узнанть о железе','О Fe выведи все сведения и связанных веществах','Выведи сведения об элементе и связанных веществах?'])
def test_latest_dialogue_does_not_fall_back_to_short_card(question):
    plan=guard_plan(KnowledgePlan(operation='element',subject='Железо'),question,[])
    assert plan.operation=='overview'


def test_overview_subject_can_come_from_previous_turn():
    plan=guard_plan(KnowledgePlan(operation='element'),'А все сведения и связанные вещества?', [{'query_plan':{'operation':'element','subject':'Железо'}}])
    assert plan.operation=='overview' and plan.subject=='Железо'


@pytest.mark.parametrize('question',['Что ты можешь сделать?','Что ты умеешь?','Какие у тебя возможности?','Привет'])
def test_capability_questions_do_not_need_entity_clarification(question):assert capabilities_request(question)


def test_compound_question_requires_separate_evidence_for_its_parts():
    card={'tool':'query_knowledge','data':{'status':'ok','plan':{'operation':'element'},'rows':[['обозначение','Fe']]}}
    coverage=request_coverage('Все сведения об Fe и связанные вещества',[card])
    assert coverage['missing']==['Связанные объекты в выбранных базах']
    both={'tool':'query_knowledge','data':{**overview(),'complete':True}}
    assert not request_coverage('Все сведения об Fe и связанные вещества',[both])['missing']


def test_partial_resource_failure_does_not_claim_relationship_search_complete(tmp_path,monkeypatch):
    monkeypatch.setattr(provenance,'SNAPSHOTS',tmp_path/'exports')
    backend=MockBackend();backend.resources=graphs();original=backend.export
    async def export(path,depth=None):
        if path==MATERIALS:raise PlatformError('forbidden','permission_denied',False)
        return await original(path,depth)
    backend.export=export
    data=asyncio.run(execute_plan(search.ResourceSearch(backend,tmp_path/'index'),list(backend.resources),{'operation':'overview','subject':'Fe'}))
    assert not data['complete'] and verify_result(data,list(backend.resources))
    coverage=request_coverage('Все сведения об Fe',[{'tool':'query_knowledge','data':data}])
    assert coverage['parts'][1]['status']=='unavailable'


def test_overview_can_be_replayed_and_tampering_is_detected(tmp_path,monkeypatch):
    monkeypatch.setattr(provenance,'SNAPSHOTS',tmp_path/'exports')
    backend=MockBackend();backend.resources=graphs()
    data=asyncio.run(execute_plan(search.ResourceSearch(backend,tmp_path/'index'),list(backend.resources),{'operation':'overview','subject':'Fe'}))
    assert verify_result(data,list(backend.resources))
    data['plan'].pop('entity_meta')
    assert verify_result(data,list(backend.resources))
    data['related']['materials']['total']=999
    assert not verify_result(data,list(backend.resources))


def test_formula_matching_is_generic_and_does_not_confuse_c_with_co():
    data=graphs();data[CHEM]['successors'] += [
        {'id':11,'name':'Углерод','meta':'Химический элемент','successors':[{'meta':'обозначение','value':'C'}]},
        {'name':'Углекислый газ','meta':'Химическое вещество','successors':[{'meta':'обозначение','value':'CO₂'}]},
        {'name':'Кобальт','meta':'Химическое вещество','successors':[{'meta':'обозначение','value':'Co'}]}]
    result=query_graphs({'operation':'overview','subject':'C','entity_meta':'Химический элемент'},data,list(data))
    assert [r['name'] for r in result['related']['substances']['items']]==['Углекислый газ']


def test_overview_of_material_reports_elements_from_its_actual_composition():
    data=graphs();result=query_graphs({'operation':'overview','subject':'A'},data,list(data))
    assert result['entity']['meta']=='Материал'
    assert any(r['name']=='Железо' for r in result['related']['other']['items'])


def test_overview_continuation_keeps_subject_and_does_not_skip_rows():
    first=overview(limit=1)
    assert first['next_offset']==1
    previous={**first['plan'],'dataset':first['dataset']}
    plan=guard_plan(KnowledgePlan(operation='element'),'Покажи следующие',[{'query_plan':previous}])
    assert plan.operation=='overview' and plan.subject=='Fe' and plan.offset==1
    data=graphs();second=query_graphs(plan,data,list(data))
    assert first['related']['materials']['items'][0]['name']!=second['related']['materials']['items'][0]['name']


def test_overview_does_not_replace_explicit_filter_or_composition_request():
    assert guard_plan(KnowledgePlan(operation='filter',element='Ni',threshold=9),'Расскажи о материалах с Ni не менее 9%',[]).operation=='filter'
    assert guard_plan(KnowledgePlan(operation='composition',subject='A'),'Расскажи состав A',[]).operation=='composition'
    assert guard_plan(KnowledgePlan(operation='structure'),'Расскажи о структуре графа',[]).operation=='structure'
    assert not request_coverage('Расскажи о ресурсах',[{'tool':'list_inforesources','data':{}}])['missing']


def test_followup_about_substances_does_not_repeat_card_and_materials():
    plan=guard_plan(KnowledgePlan(operation='element',subject='Fe'),'А какие связанные вещества?',[])
    assert plan.overview_sections==['substances']
    data=graphs();result=query_graphs(plan,data,list(data))
    assert 'Связанные вещества' in result['answer'] and 'Карточка объекта' not in result['answer']
    assert 'Связанные материалы' not in result['answer'] and set(result['related'])=={'substances'}


def test_contextual_followup_uses_known_object_but_never_overrides_a_new_one():
    history=[{'query_plan':{'operation':'element','subject':'Fe'}}]
    plan=contextual_plan('А какие связанные вещества нашлись?',history)
    assert plan.subject=='Fe' and plan.operation=='overview' and plan.overview_sections==['substances']
    assert contextual_plan('А какие вещества связаны с медью?',history) is None
    assert contextual_plan('А какие связанные вещества?',[]) is None
    assert contextual_plan('А какие связанные вещества?',history+[{'query_plan':{'operation':'filter','element':'Ni','threshold':9}}]) is None


def test_overview_semantic_review_can_replay_after_ontologies_were_excluded(tmp_path,monkeypatch):
    from iacpaas_mcp.verifier import prepare_sources
    monkeypatch.setattr(provenance,'SNAPSHOTS',tmp_path/'exports')
    backend=MockBackend();backend.resources=graphs();backend.resources['Загрузки/Онтология материалов']={'type':'КОРЕНЬ','name':'Онтология'}
    data=asyncio.run(execute_plan(search.ResourceSearch(backend,tmp_path/'index'),list(backend.resources),{'operation':'overview','subject':'Fe'}))
    sources,_,replay=prepare_sources([{'name':'query_knowledge','arguments':{'plan':data['plan']},'result':data}])
    assert replay[0]['verified'] and sources['T0']['data']['related']['materials']['total']==2


def test_class_overview_keeps_class_subject_instead_of_previous_element():
    data=graphs();data[MATERIALS]['successors']=[{'name':'Бронзы','meta':'Класс материалов','successors':data[MATERIALS]['successors']}]
    plan=guard_plan(KnowledgePlan(operation='materials',class_name='Бронзы'),'Расскажи о бронзах',[{'query_plan':{'operation':'element','subject':'Fe'}}])
    assert plan.subject=='Бронзы' and plan.entity_meta=='Класс материалов'
    result=query_graphs(plan,data,list(data))
    assert result['related']['materials']['total']==3 and result['attribute_total']==0
