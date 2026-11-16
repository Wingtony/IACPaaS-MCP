import pytest

from iacpaas_mcp.context import execution_limits
from iacpaas_mcp.progress import signature,fallback_ids
from iacpaas_mcp.request_scope import ranking_constraints


def test_progress_ignores_caption_and_timestamp_but_counts_new_rows():
    a={'plan':{'limit':1},'answer':'old caption','rows':[['A',1]],'total':1,'_source':{'path':'R','export_sha256':'a','retrieved_at':'old'}}
    b={**a,'plan':{'limit':2},'answer':'different caption','_source':{**a['_source'],'retrieved_at':'new'}}
    assert signature('query_knowledge',{},a)==signature('query_knowledge',{},b)
    assert signature('query_knowledge',{},a)!=signature('query_knowledge',{},dict(b,rows=[['B',2]]))


def test_fallback_keeps_complementary_facts_and_deduplicates_reads():
    def record(tool,data):return {'tool':tool,'arguments':{'path':'R'},'data':data}
    records={'S1':record('list_inforesources',{'paths':['R']}),'S2':record('count_graph',{'nodes':8}),
             'S3':record('get_children',{'children':[{'path':'A'}]}),'S4':record('count_graph',{'nodes':8})}
    assert set(fallback_ids(records))=={'S3','S4'}


def test_configurable_budget_has_independent_progress_guard(monkeypatch):
    monkeypatch.setenv('AGENT_MAX_STEPS','30');monkeypatch.setenv('AGENT_MAX_STAGNANT','3')
    assert execution_limits()==(30,3) and execution_limits(5)==(5,3)
    monkeypatch.setenv('AGENT_MAX_STAGNANT','0')
    with pytest.raises(ValueError):execution_limits()


@pytest.mark.parametrize('question',['Топ 5 по содержанию Cr','Топ материалов по содержанию C для сравнения'])
def test_composition_ranking_is_supported(question):
    assert ranking_constraints(question)==(None,[])


def test_explicit_carbon_proxy_warns_that_strength_is_not_measured():
    clarification,limitations=ranking_constraints('Топ самых прочных сплавов по содержанию углерода C')
    assert clarification is None and 'не по измеренной прочности' in limitations[0]
