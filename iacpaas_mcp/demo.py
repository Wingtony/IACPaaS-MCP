"""Explicit MCP demonstration on newly created resources; never modifies supplied databases."""
import argparse
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import uuid

from dotenv import load_dotenv
from mcp import Client

from .backend import HttpBackend, MockBackend
from .credentials import load_account
from .graph import counts, validate, walk
from .server import PROJECT, build_server


def bundle(suffix, fund='demo@example.invalid/Мой Фонд', folder='Загрузки'):
    """Only synthetic project data; no copies of the supplied material databases."""
    ontology_title='АТЛАС - онтология проектов '+suffix
    data_title='АТЛАС - учебные проекты '+suffix
    def ir_path(title):return folder+'/'+title
    def full_path(title):return fund+'/'+ir_path(title)+'$;'
    ontology={
        'title':ontology_title,'name':ontology_title,'path':full_path(ontology_title),
        'type':'КОРЕНЬ','meta':'Язык ИРУО','ontology':'Язык ИРУО$;',
        'json_type':'universal',
        'comment':'Собственная учебная онтология АТЛАС для демонстрации MCP.',
        'successors':[{'name':'Проект','type':'НЕТЕРМИНАЛ','meta':'описать элемент списка',
            'specifier':'возможно пустое упорядоченное множество','restrictor':'new',
            'successors':[
                {'name':'Статус','type':'ТЕРМИНАЛ-СОРТ','valtype':'STRING','meta':'описать элемент списка',
                 'specifier':'в точности один','restrictor':'new'},
                {'name':'Число сценариев','type':'ТЕРМИНАЛ-СОРТ','valtype':'INTEGER','meta':'описать элемент списка',
                 'specifier':'в точности один','restrictor':'new'}]}]}
    data={'title':data_title,'name':data_title,'path':full_path(data_title),
        'type':'КОРЕНЬ','meta':ontology_title,'ontology':full_path(ontology_title),
        'json_type':'universal',
        'comment':'Синтетические учебные примеры, не сведения о материалах и не отчёт о состоянии проекта.',
        'successors':[
            {'name':name,'type':'НЕТЕРМИНАЛ','meta':'Проект','successors':[
                {'value':'учебный пример','type':'ТЕРМИНАЛ-ЗНАЧЕНИЕ','valtype':'STRING','meta':'Статус'},
                {'value':number,'type':'ТЕРМИНАЛ-ЗНАЧЕНИЕ','valtype':'INTEGER','meta':'Число сценариев'}]}
            for name,number in [('MCP-сервер',2),('Веб-агент',3)]]}
    imports=[{'folder_path':folder,'ir_json':json.dumps(graph,ensure_ascii=True),'clear_if_exists':False}
             for graph in [ontology,data]]
    update={'path':ir_path(data_title),'concept_path':'Веб-агент/учебный пример',
            'metaconcept_path':'Проект/Статус','mark':'демонстрация пройдена','comment':''}
    return {'paths':[ir_path(ontology_title),ir_path(data_title)],'imports':imports,'update':update}


class RestrictedDemoBackend:
    """Exact allowlist: only the two generated payloads and one generated update."""
    def __init__(self, backend, plan):
        self.backend=backend
        self.plan=copy.deepcopy(plan)
        self.mutations=[]

    async def export(self,path,depth=None):
        if path not in self.plan['paths']:raise ValueError('ИР вне учебной демонстрации')
        return await self.backend.export(path,depth)

    async def import_resource(self,args):
        if args not in self.plan['imports']:raise ValueError('Разрешён только точный импорт новых учебных ресурсов без очистки')
        self.mutations.append('import_inforesource')
        return await self.backend.import_resource(args)

    async def update(self,args):
        if args!=self.plan['update']:raise ValueError('Разрешено только утверждённое изменение собственного учебного ресурса')
        self.mutations.append('update_concept')
        return await self.backend.update(args)


async def demonstrate(live=False, accept_mock=False):
    load_dotenv(PROJECT/'.env')
    if live and accept_mock:raise ValueError('--accept-mock нельзя использовать с --live')
    suffix=datetime.now(timezone(timedelta(hours=10))).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    fund='demo@example.invalid/Мой Фонд'
    if live:
        username=load_account().get('username')
        if not username:raise ValueError('Сначала подключите свой аккаунт IACPaaS в приложении')
        fund=username+'/Мой Фонд'
    plan=bundle(suffix,fund)
    for args in plan['imports']:validate(args['ir_json'])
    backend=RestrictedDemoBackend(HttpBackend() if live else MockBackend(),plan)
    report={'mode':'live' if live else 'mock','paths':plan['paths'],'events':[],'status':'prepared'}
    directory=PROJECT/'logs'/('demo-'+suffix)
    directory.mkdir(parents=True)
    manifest=directory/'manifest.json'
    manifest.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    checksum=hashlib.sha256(manifest.read_bytes()).hexdigest()
    report['manifest_sha256']=checksum
    def record(event,**data):
        report['events'].append({'event':event,**data})
        (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'event':event,**data},ensure_ascii=False),flush=True)
    print('LIVE IACPaaS: создаются два НОВЫХ собственных ИР.' if live else 'MOCK: учебный MCP, без сети и без модели.',flush=True)
    print('Манифест: '+str(manifest),flush=True)
    print('SHA-256: '+checksum,flush=True)
    print('Новые пути: '+json.dumps(plan['paths'],ensure_ascii=False),flush=True)
    print('Данные: MCP-сервер (2), Веб-агент (3); статус «учебный пример».\nИзменение: статус Веб-агента → «демонстрация пройдена».',flush=True)
    async with Client(build_server(backend=backend,known_paths=plan['paths'],allow_write=True)) as client:
        async def invoke(name,args):
            result=await client.call_tool(name,args)
            if result.is_error:
                error=(result.structured_content or {}).get('error',{})
                report['status']='failed';record('tool_error',tool=name,error=error)
                raise RuntimeError(error.get('message') or 'MCP tool вернул ошибку')
            return result.structured_content
        bad=await client.call_tool('get_subgraph',{'path':plan['paths'][0],'root':'','depth':6})
        if not bad.is_error:raise RuntimeError('Ограничение глубины не сработало')
        record('invalid_depth_rejected')
        preview=await invoke('import_inforesource',plan['imports'][0])
        if preview.get('status')!='pending_confirmation':raise RuntimeError('Нет предпросмотра записи')
        record('preview',operation='import_inforesource',preview=preview['preview'])
        # Reject this preview: do not consume its capability. No mutation has occurred.
        if backend.mutations:raise RuntimeError('Предпросмотр неожиданно вызвал изменение')
        record('declined',changed=False,backend_mutations=0)
        if live:
            answer=await asyncio.to_thread(input,'Для создания двух ИР и проверки изменения только в них введите ДА: ')
            approved=answer.strip()=='ДА'
        else:
            approved=accept_mock or (await asyncio.to_thread(input,'Выполнить учебную запись в памяти? Введите ДА: ')).strip()=='ДА'
        if not approved:
            report['status']='cancelled';record('cancelled');return directory
        record('mock_approved' if accept_mock else 'human_approved',manifest_sha256=checksum)
        for index,args in enumerate(plan['imports']):
            # Fresh preview binds exactly the approved manifest; no token in the report.
            prepared=await invoke('import_inforesource',args)
            await invoke('import_inforesource',{**args,'confirm_token':prepared['confirm_token']})
            actual=await invoke('count_graph',{'path':plan['paths'][index]})
            expected=counts(validate(args['ir_json']))
            if any(actual[key]!=expected[key] for key in expected):raise RuntimeError('Количество вершин/дуг после импорта не совпало')
            record('created',path=plan['paths'][index],counts=expected)
        change=await invoke('update_concept',plan['update'])
        if change.get('status')!='pending_confirmation':raise RuntimeError('Нет предпросмотра изменения')
        updated=await client.call_tool('update_concept',{**plan['update'],'confirm_token':change['confirm_token']})
        if updated.is_error:
            error=(updated.structured_content or {}).get('error',{})
            record('update_rejected',error=error)
            current=await invoke('get_subgraph',{'path':plan['paths'][1],'root':'','depth':2})
            statuses=[node.get('value') for node,path in walk(current['graph'])
                      if path.startswith('Веб-агент/') and node.get('meta')=='Статус']
            report['status']='created_update_unavailable'
            record('completed_with_limitation',current_status=statuses,
                   notice='Создание и чтение подтверждены; обновление не подтверждено. Автоповтора записи нет.')
            print('Отчёт: '+str(directory/'report.json'),flush=True)
            return directory
        graph=(await invoke('get_subgraph',{'path':plan['paths'][1],'root':'','depth':2}))['graph']
        statuses=[node.get('value') for node,path in walk(graph) if path.startswith('Веб-агент/') and node.get('meta')=='Статус']
        if statuses!=['демонстрация пройдена']:raise RuntimeError('Повторное чтение не подтвердило изменение статуса')
        record('updated_and_read_back',path=plan['paths'][1],status=statuses[0])
        report['status']='passed';record('completed')
    print('Отчёт: '+str(directory/'report.json'),flush=True)
    return directory


async def inspect_resources(manifest):
    """Read an existing demonstration without imports, updates, LLM or new resources."""
    load_dotenv(PROJECT/'.env')
    plan=json.loads(Path(manifest).read_text(encoding='utf-8'))
    if len(plan['paths'])!=2 or not all(path.startswith('Загрузки/АТЛАС - ') for path in plan['paths']):
        raise ValueError('Нужен манифест двух собственных учебных ресурсов АТЛАС')
    backend=RestrictedDemoBackend(HttpBackend(),plan)
    results=[]
    async with Client(build_server(backend=backend,known_paths=plan['paths'],allow_write=False)) as client:
        for path in plan['paths']:
            amounts=await client.call_tool('count_graph',{'path':path})
            result=await client.call_tool('get_subgraph',{'path':path,'root':'','depth':2})
            if result.is_error or amounts.is_error:raise RuntimeError('Не удалось прочитать собственный учебный ресурс')
            graph=result.structured_content['graph']
            item={'path':path,'counts':{key:amounts.structured_content[key] for key in ('nodes','edges','links')},'display_depth':2,'fields':[
                {'path':relative,**{key:node[key] for key in ('name','value','meta') if key in node}}
                for node,relative in walk(graph)]}
            results.append(item)
            print(json.dumps(item,ensure_ascii=False,indent=2),flush=True)
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--live',action='store_true',help='Создать собственную онтологию и базу в IACPaaS, только после ДА')
    mode.add_argument('--inspect',type=Path,help='Только прочитать два уже созданных ИР из локального манифеста')
    parser.add_argument('--accept-mock',action='store_true',help='Неинтерактивное согласие только для mock, не для платформы')
    args=parser.parse_args()
    try:
        if args.inspect:
            if args.accept_mock:raise ValueError('--inspect не использует согласие на запись')
            asyncio.run(inspect_resources(args.inspect));return
        directory=asyncio.run(demonstrate(args.live,args.accept_mock))
        result=json.loads((directory/'report.json').read_text(encoding='utf-8'))
        if result['status']=='created_update_unavailable':raise SystemExit(2)
    except (ValueError,RuntimeError,ExceptionGroup) as exc:
        while isinstance(exc,ExceptionGroup) and exc.exceptions:exc=exc.exceptions[0]
        raise SystemExit(str(exc))


if __name__=='__main__':main()
