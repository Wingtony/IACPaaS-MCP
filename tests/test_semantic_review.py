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
    review=verifier.Review(claims=[{'quote':'15 материалов','status':status,'source_id':'T1','pointer':'/answer'}])
    result=verifier.resolve_claims(review,'15 материалов',{'T1':{'data':{'total':15}}})
    assert result[0]['status']=='reference_error' and not result[0]['binding_verified']


def test_scalar_and_table_row_are_extracted_without_llm_copying_json():
    review=verifier.Review(claims=[{'quote':'15 материалов','status':'confirmed','source_id':'T1','pointer':'/total'},
        {'quote':'Н70МФ | 64.21 — 73.6 | %','status':'confirmed','source_id':'T1','pointer':'/rows/0'}])
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
        assert 'answer' not in document['sources']['T0']['data']
        assert 'evidence' not in json.dumps(body['format'])
        return httpx.Response(200,json={'message':{'content':json.dumps({'claims':[{'quote':'8 вершин','status':'confirmed','source_id':'T0','pointer':'/nodes'}]})},'done_reason':'stop'})
    monkeypatch.setattr(verifier.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(reply),**kw))
    report=asyncio.run(verifier.verify_answer('Сколько?','8 вершин',[step],ModelConfig('ollama','test',endpoint='http://mock.test')))
    assert len(requests)==1 and report['version']==2 and report['confirmed']==1
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
