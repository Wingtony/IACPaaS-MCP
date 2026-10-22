import asyncio
import json

import httpx
import pytest
from mcp import Client

from iacpaas_mcp.agent import run_agent
from iacpaas_mcp.adaptive_agent import decision_schema, messages_for
from iacpaas_mcp.backend import MockBackend
from iacpaas_mcp.context import estimate_tokens
from iacpaas_mcp.llm import ModelConfig
from iacpaas_mcp.server import build_server

PATH='Учебные/База наблюдений'


def call(tool, **args):
    return {'action':'call','call':{'tool':tool,'arguments':args},'evidence_ids':[],
            'question':'','sufficient':False,'assessment':'Нужно получить данные'}


def finish(*ids, sufficient=True):
    return {'action':'finish','call':None,'evidence_ids':list(ids),'question':'',
            'sufficient':sufficient,'assessment':'Проверены запрошенные части вопроса'}


@pytest.fixture
def harness(tmp_path,monkeypatch):
    def execute(decisions, backend=None, approve=None, max_steps=12, provider='ollama', question='Посчитай граф', **options):
        backend=backend or MockBackend();events=[];requests=[]
        original=httpx.AsyncClient
        def reply(request):
            body=json.loads(request.content);requests.append(body)
            assert len(requests)<=len(decisions),'Unexpected model request'
            decision=decisions[len(requests)-1]
            text=json.dumps(decision,ensure_ascii=False)
            if provider=='openai':
                return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':text}]}]})
            if provider=='anthropic':
                return httpx.Response(200,json={'stop_reason':'end_turn','content':[{'type':'text','text':text}]})
            if provider!='ollama':
                return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':text}}]})
            return httpx.Response(200,json={'message':{'content':text},'prompt_eval_count':10,'eval_count':5})
        monkeypatch.setattr('iacpaas_mcp.adaptive_agent.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(reply),**kw))
        async def run():
            async with Client(build_server(backend=backend,allow_write=approve is not None)) as client:
                return await run_agent(client,question,'test-model','http://ollama.test',tmp_path/'adaptive.jsonl',
                    approve=approve,on_event=events.append,max_steps=max_steps,llm_config=ModelConfig(provider,'test-model','test-key'),**options)
        return asyncio.run(run()),events,requests,backend
    return execute


@pytest.mark.parametrize('provider',['ollama','openai','anthropic','gemini','deepseek','openrouter'])
def test_dependent_calls_are_selected_after_actual_results(harness,provider):
    answer,events,requests,_=harness([call('list_inforesources'),call('count_graph',path=PATH),finish('S2')],provider=provider)
    assert '8' in answer and 'Ссылки: **1**' in answer
    assert [e['name'] for e in events if e['event']=='tool']==['list_inforesources','count_graph']
    messages=requests[1].get('messages',requests[1].get('input'))
    assert PATH in json.dumps(messages,ensure_ascii=False)
    report=next(e for e in events if e['event']=='verification')
    assert report['status']=='verified_result' and not report['meaning_verified']


def test_api_error_is_given_to_model_before_replanning(harness):
    class Flaky(MockBackend):
        attempts=0
        async def export(self,path,depth=None):
            self.attempts+=1
            if self.attempts==1:raise ValueError('IACPaaS: HTTP 503; повторите позже')
            return await super().export(path,depth)
    answer,events,requests,backend=harness([call('count_graph',path=PATH),call('get_inforesource',path=PATH),finish('S1')],backend=Flaky())
    assert backend.attempts==2
    assert '503' in json.dumps(requests[1],ensure_ascii=False)
    assert next(e for e in events if e['event']=='tool')['is_error'] is True
    assert 'КОРЕНЬ' in answer


def test_unknown_resource_cannot_be_guessed_and_model_recovers(harness):
    answer,events,requests,_=harness([call('count_graph',path='private/other'),call('list_inforesources'),call('count_graph',path=PATH),finish('S2')])
    assert '8' in answer
    assert any(e['event']=='replanning' for e in events)
    assert 'вне подключённой области' in json.dumps(requests[1],ensure_ascii=False)
    assert all(e['arguments'].get('path')!='private/other' for e in events if e['event']=='tool')


def test_result_path_is_used_for_next_subgraph_call(harness):
    answer,events,_,_=harness([call('query_graph',path=PATH,text='Основные жалобы'),
        call('get_subgraph',path=PATH,root='Основные жалобы',depth=1),finish('S2')])
    assert 'Основные жалобы' in answer
    assert next(e for e in events if e['event']=='verification')['status']=='verified_result'


def test_unseen_vertex_path_is_rejected_before_tool_call(harness):
    _,events,_,_=harness([call('get_subgraph',path=PATH,root='invented'),call('count_graph',path=PATH),finish('S1')])
    assert any(e['event']=='replanning' for e in events)
    assert [e['name'] for e in events if e['event']=='tool']==['count_graph']


def test_invalid_finish_must_be_repaired_not_displayed(harness):
    answer,events,requests,_=harness([finish('S99'),call('count_graph',path=PATH),finish('S1')])
    assert len(requests)==3 and '8' in answer
    assert any(e['event']=='replanning' for e in events)


def test_insufficient_result_continues_instead_of_early_answer(harness):
    answer,events,_,_=harness([call('list_inforesources'),call('query_graph',path=PATH,text='missing'),
        call('count_graph',path=PATH),finish('S3')])
    assert '8' in answer and len([e for e in events if e['event']=='tool'])==3


def test_step_limit_returns_explicitly_incomplete_facts(harness):
    answer,events,_,_=harness([call('list_inforesources'),call('count_graph',path=PATH)],max_steps=2)
    assert 'Поиск неполный' in answer
    assert next(e for e in events if e['event']=='verification')['status']=='incomplete'


def test_clarification_is_not_labelled_as_verified(harness):
    decision={'action':'clarify','call':None,'evidence_ids':[],'question':'Какую марку вы хотите сравнить?', 'sufficient':False,'assessment':'Не указана марка'}
    answer,events,_,_=harness([decision])
    assert answer==decision['question']
    assert next(e for e in events if e['event']=='verification')['status']=='needs_clarification'


@pytest.mark.parametrize('approved',[False,True])
def test_write_uses_human_approval_and_never_exposes_confirmation_capability(harness,tmp_path,approved,monkeypatch):
    from iacpaas_mcp.policy import WritePolicy
    minted=[];original=WritePolicy.issue
    def issue(self,*args):
        token=original(self,*args);minted.append(token);return token
    monkeypatch.setattr(WritePolicy,'issue',issue)
    previews=[]
    def approve(preview):previews.append(preview);return approved
    answer,events,requests,backend=harness([call('query_graph',path=PATH,text='Основные жалобы'),
        call('update_concept',path=PATH,concept_path='Основные жалобы',metaconcept_path='Группа наблюдений',mark='Новая группа'),finish('W1')],approve=approve)
    assert len(previews)==1 and 'confirm_token' not in json.dumps(previews)
    assert 'confirm_token' not in json.dumps([e for e in events if e['event']=='tool'])
    assert 'confirm_token' not in (tmp_path/'adaptive.jsonl').read_text(encoding='utf-8')
    assert minted and all(token not in json.dumps(requests) for token in minted)
    assert ('выполнено' in answer) is approved
    assert (backend.resources[PATH]['successors'][0]['name']=='Новая группа') is approved


def test_all_mcp_tools_have_typed_output_and_runtime_diagnostics():
    async def run():
        async with Client(build_server(backend=MockBackend())) as client:
            tools=(await client.list_tools()).tools
            for tool in tools:
                assert tool.output_schema and tool.output_schema.get('properties'),tool.name
            counts=next(t for t in tools if t.name=='count_graph').output_schema
            assert counts['properties']['nodes']['type']=='integer'
            bad=await client.call_tool('count_graph',{'path':'bad'})
            error=json.loads(bad.content[0].text)['error']
            assert bad.is_error and error['tool']=='count_graph' and error['retryable'] and error['suggestion']
    asyncio.run(run())


def test_decision_schema_includes_real_argument_types_and_hides_confirmation_token():
    async def run():
        async with Client(build_server(backend=MockBackend())) as client:return (await client.list_tools()).tools
    schema=decision_schema(asyncio.run(run()))
    raw=json.dumps(schema)
    assert 'confirm_token' not in raw and 'KnowledgePlan' in raw
    assert any(c.get('properties',{}).get('tool',{}).get('enum')==['count_graph'] for c in schema['properties']['call']['anyOf'])
    decision=call('count_graph',path=PATH,root='')
    from jsonschema import Draft202012Validator
    Draft202012Validator(schema).validate(decision)
    messages,metrics=messages_for('Вопрос',[],[{'tool':'count_graph','data':{'evidence_id':'S1','value':'large '*2000}}]*12,schema,24000)
    assert estimate_tokens({'messages':messages,'format':schema})<=24000
    assert metrics['omitted_observations']>0 and 'Вопрос' in messages[1]['content']


def test_invalid_platform_output_is_a_diagnostic_not_a_success():
    class Invalid(MockBackend):
        async def export(self, path, depth=None):
            return {'type':'КОРЕНЬ','name':123}
    async def run():
        async with Client(build_server(backend=Invalid())) as client:
            result=await client.call_tool('get_inforesource',{'path':PATH})
            assert result.is_error
            assert result.structured_content['error']['code']=='invalid_result'
    asyncio.run(run())
