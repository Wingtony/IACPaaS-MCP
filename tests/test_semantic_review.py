import asyncio
import copy
import json

import httpx
import pytest

from iacpaas_mcp import provenance, verifier
from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.graph import counts
from iacpaas_mcp.llm import ModelConfig


@pytest.fixture
def step(tmp_path,monkeypatch):
    monkeypatch.setattr(provenance,'SNAPSHOTS',tmp_path/'exports')
    path='Учебные/База наблюдений'
    graph=asyncio.run(MockBackend().export(path))
    return {'name':'count_graph','arguments':{'path':path},'result':{**counts(graph),'_source':graph['_source']}}


def test_tampered_result_is_rejected_before_calling_llm(step,monkeypatch):
    step['result']['nodes']=999
    async def forbidden(*args):pytest.fail('Invalid evidence must not be sent to an LLM')
    monkeypatch.setattr(verifier,'complete',forbidden)
    with pytest.raises(ValueError,match='повторную сверку'):
        asyncio.run(verifier.verify_answer('Сколько?','999 вершин',[step],ModelConfig('deepseek','test','fake')))


@pytest.mark.parametrize('status',['confirmed','contradicted'])
def test_answer_cannot_be_evidence_even_for_contradiction(status):
    review=verifier.Review(claims=[{'segment_id':'A1','status':status,'fact_id':'T1/answer'}])
    result=verifier.resolve_claims(review,'15 материалов',{'T1':{'data':{'total':15}}})
    assert result[0]['status']=='reference_error' and not result[0]['binding_verified']


def test_scalar_and_table_row_are_extracted_without_llm_copying_json():
    review=verifier.Review(claims=[{'segment_id':'A1','status':'confirmed','fact_id':'T1/total'},
        {'segment_id':'A2','status':'confirmed','fact_id':'T1/rows/0'}])
    source={'T1':{'data':{'total':15,'rows':[['Н70МФ','64.21 — 73.6','%',73.6]]}}}
    claims=verifier.resolve_claims(review,'15 материалов\nН70МФ | 64.21 — 73.6 | %',source)
    assert all(c['status']=='confirmed' and c['binding_verified'] for c in claims)
    assert claims[0]['evidence']=='15'
    assert json.loads(claims[1]['evidence'])==source['T1']['data']['rows'][0]


def test_model_receives_bounded_schema_without_evidence_copy_field(step,monkeypatch):
    original=httpx.AsyncClient;requests=[]
    def reply(request):
        body=json.loads(request.content);requests.append(body)
        assert body['options']['num_predict']<=1536 and body['keep_alive']==-1
        document=json.loads(body['messages'][1]['content'])
        assert not any(f['pointer']=='/answer' for f in document['fact_catalog'].values())
        assert document['answer_segments']=={'A1':'8 вершин'}
        assert 'evidence' not in json.dumps(body['format'])
        assert 'quote' not in json.dumps(body['format'])
        assert body['format']['$defs']['Claim']['properties']['segment_id']['enum']==['A1']
        assert 'T0/nodes' in body['format']['$defs']['Claim']['properties']['fact_id']['enum']
        return httpx.Response(200,json={'message':{'content':json.dumps({'claims':[{'segment_id':'A1','status':'confirmed','fact_id':'T0/nodes'}]})},'done_reason':'stop'})
    monkeypatch.setattr(verifier.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(reply),**kw))
    report=asyncio.run(verifier.verify_answer('Сколько?','8 вершин',[step],ModelConfig('ollama','test',endpoint='http://mock.test')))
    assert len(requests)==1 and report['version']==3 and report['confirmed']==1
    assert report['claims'][0]['quote']=='8 вершин'
    assert report['replay'][0]['verified']


def test_failed_tools_are_never_sources(step):
    failed=copy.deepcopy(step);failed['is_error']=True
    sources,_,_=verifier.prepare_sources([failed,step])
    assert set(sources)=={'T1'}


def test_empty_review_is_not_successful():
    with pytest.raises(ValueError):verifier.Review(claims=[])


def test_client_evidence_id_does_not_break_root_snapshot_replay(step):
    path=step['arguments']['path']
    graph=asyncio.run(MockBackend().export(path,depth=0))
    graph['evidence_id']='S1'
    sources,_,_=verifier.prepare_sources([{'name':'get_inforesource','arguments':{'path':path},'result':graph}])
    assert 'evidence_id' not in sources['T0']['data']


@pytest.mark.parametrize('text',[
    'Подходит материалов: **15**. Проверено: 1334.',
    '| Н70МФ | 64.21 — 73.6 | % | 73.6 |',
    'Ni > 25%, Ni ≥ 9%; Cr ≤ 19%. Элемент не отсутствует.',
    'Прочность\u00a0—\u00a0100 МПа\n\nДругая строка',
    'x'*1200,
])
def test_segments_preserve_exact_spelling_numbers_negation_and_unicode(text):
    segments=verifier.answer_segments(text)
    assert segments and all(part in text and len(part)<=500 for part in segments.values())
    assert verifier.answer_segments(text)==segments


def test_unknown_fragment_does_not_cancel_valid_claims_or_become_confirmed():
    review=verifier.Review(claims=[{'segment_id':'A999','status':'confirmed','fact_id':'T0/nodes'},
                                 {'segment_id':'A1','status':'confirmed','fact_id':'T0/nodes'}])
    claims=verifier.resolve_claims(review,'8 вершин',{'T0':{'data':{'nodes':8}}})
    assert claims[0]['status']=='reference_error' and not claims[0]['quote']
    assert claims[0]['error_code']=='unknown_answer_segment'
    assert claims[1]['status']=='confirmed' and claims[1]['quote']=='8 вершин'


def test_duplicate_ids_are_not_counted_twice():
    claim={'segment_id':'A1','status':'confirmed','fact_id':'T0/nodes'}
    assert len(verifier.resolve_claims(verifier.Review(claims=[claim,claim]),'8 вершин',{'T0':{'data':{'nodes':8}}}))==1


def test_catalog_exposes_whole_rows_and_rejects_invented_data_prefix():
    sources={'T1':{'data':{'total':15,'rows':[['Н70МФ','64.21 — 73.6','%',73.6]]}}}
    facts=verifier.fact_catalog(sources)
    assert facts['T1/rows/0']['value']==sources['T1']['data']['rows'][0]
    assert 'T1/rows/0/0' not in facts and 'T1/data/total' not in facts
    review=verifier.Review(claims=[{'segment_id':'A1','status':'confirmed','fact_id':'T1/data/total'}])
    assert verifier.resolve_claims(review,'15 материалов',sources)[0]['status']=='reference_error'
