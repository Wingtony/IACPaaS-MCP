"""Explicit request facets; detecting a gap is not a general proof of understanding."""
import re


def capabilities_request(question):
    text=question.casefold().replace('ё','е').strip(' ?!.')
    return bool(re.fullmatch(r'(?:привет|здравствуй(?:те)?|что\s+(?:ты|вы)\s+(?:вообще\s+)?(?:можешь|можете|умеешь|умеете)(?:\s+(?:сделать|делать))?|(?:какие|покажи)\s+(?:у\s+тебя\s+)?возможности|чем\s+(?:ты\s+)?(?:можешь\s+)?(?:мне\s+)?помочь|как\s+(?:тобой|вами)\s+пользоваться|помощь|help)',text))


def ranking_constraints(question):
    """Describe capabilities, not engineering recommendations or inferred property values."""
    text=question.casefold().replace('ё','е')
    ranking=bool(re.search(r'\bтоп\b|рейтинг|лучш|оптимальн|наиболее|наименее|\bсам(?:ый|ые|ая|ое|ых)\b',text))
    if not ranking:return None,[]
    mechanical=bool(re.search(r'прочн|предел\w*\s+текуч|сопротивлен\w*\s+разрыв',text))
    purpose=re.search(r'для\s+(\w+)',text)
    application=bool(re.search(r'ракет|авиац|космич|назначени|пригодн',text) or (purpose and purpose[1] not in {'меня','нас','вас','сравнения','анализа','примера','просмотра'}))
    from .knowledge import mentioned_elements
    composition=bool(mentioned_elements(question) and re.search(r'содержан|концентрац|массов\w*\s+дол|больш\w*\s+(?:углерод|хром|никел)',text))
    if (mechanical or application) and not composition:
        return ('Для обоснованного рейтинга нужен измеряемый критерий и условия сравнения. '
                'Текущий модуль рассчитывает рейтинги по элементному составу, а не прочности или пригодности для отрасли. '
                'Содержание C нельзя выдавать за измеренную прочность. Укажите показатель, его значения/источник и условия '
                'либо попросите явно отсортировать материалы по содержанию элемента.'),[]
    limitations=[]
    if composition and mechanical:limitations.append('Это рейтинг по записанному содержанию элемента, а не по измеренной прочности. Прочность этим расчётом не определялась.')
    if composition and application:limitations.append('Пригодность для указанного назначения этим отбором по составу не подтверждена; инженерные критерии применения не оценивались.')
    return None,limitations


def wants_overview(question):
    text=question.casefold().replace('ё','е')
    explicit_relations=bool(re.search(r'связан|связ[ьи]|связей|связях',text))
    if not explicit_relations and re.search(r'[<>≤≥]|не\s+(?:более|менее)',text):return False
    if not explicit_relations and re.search(r'структур|онтологи|вершин|дуг|подграф|ресурс',text):return False
    if not explicit_relations and 'состав' in text and not re.search(r'все\s+(?:сведени|данн|информац)|всю\s+информац',text):return False
    return explicit_relations or bool(re.search(r'(?:все|всю)\s+(?:сведени|данн|информац)|все\s+(?:о|об|про)\b|расскажи|расскажите|(?:узнать|узнанть)\s+(?:о|об)\b',text))


def required_parts(question):
    text=question.casefold().replace('ё','е')
    parts=[]
    if wants_overview(question):
        parts.extend([('entity','Сведения об объекте'),('relations','Связанные объекты в выбранных базах')])
    else:
        if re.search(r'состав',text):parts.append(('composition','Состав'))
        if re.search(r'свойств|характеристик',text):parts.append(('properties','Свойства и характеристики'))
        if re.search(r'структур|онтологи|подграф',text):parts.append(('structure','Структура'))
        if re.search(r'(?:сколько|посчитай|подсчитай).*(?:вершин|дуг|ссылок)',text):parts.append(('graph_count','Размер графа'))
    return parts if len(parts)>1 or (parts and parts[0][0]=='graph_count') else []


def overview_sections(question):
    text=question.casefold().replace('ё','е')
    all_sections=['attributes','substances','materials','other']
    if re.search(r'все\s+(?:сведени|данн|информац|о\b|об\b|про\b)|всю\s+информац',text):return all_sections
    selected=[]
    if 'веществ' in text:selected.append('substances')
    if 'материал' in text and re.search(r'связан|связ[ьи]',text):selected.append('materials')
    if not selected:return all_sections
    if re.search(r'сведени|информац|карточ|описан|характерист',text):selected.insert(0,'attributes')
    return selected


def request_coverage(question, records):
    required=required_parts(question)
    if not required and any(r['data'].get('plan',{}).get('operation')=='overview' for r in records):
        required=[('entity','Сведения об объекте')]
        if any(set(r['data'].get('plan',{}).get('overview_sections',['attributes','substances','materials','other']))-{'attributes'} for r in records if r['data'].get('plan',{}).get('operation')=='overview'):
            required.append(('relations','Связанные объекты в выбранных базах'))
    answered=set(); details={}
    for record in records:
        tool,data=record['tool'],record['data']
        plan=data.get('plan')
        operation=plan.get('operation') if isinstance(plan,dict) else None
        if tool=='query_knowledge':
            if data.get('status')!='ok':continue
            if operation=='overview':
                for part in data.get('sections',[]):
                    status='unavailable' if part['id']=='relations' and not data.get('complete',True) else part['status']
                    if status in {'answered','no_matches'}:answered.add(part['id'])
                    details[part['id']]=status
            if operation in {'element','property','term','composition','compare'}:answered.add('entity')
            if operation in {'composition','compare'}:answered.add('composition')
            if operation=='property':answered.add('properties')
            if operation=='structure':answered.add('structure')
        if tool in {'get_subgraph','get_children'}:answered.add('structure')
        if tool=='count_graph':answered.add('graph_count')
    parts=[{'id':key,'label':label,'status':details.get(key,'answered' if key in answered else 'missing')} for key,label in required]
    return {'parts':parts,'missing':[p['label'] for p in parts if p['status'] not in {'answered','no_matches'}],
            'evaluated':bool(required)}


def capabilities_answer(tools,paths):
    names={t.name for t in tools}
    lines=['Могу исследовать подключённые графовые базы IACPaaS обычными вопросами.']
    if 'query_knowledge' in names:
        lines+=['- Искать сведения об объекте, его состав, свойства и связанные объекты.',
                '- Сравнивать материалы, считать результаты, применять несколько условий состава и показывать следующие страницы.']
    if 'get_subgraph' in names:lines.append('- Показывать структуру базы и онтологий, считать вершины, дуги и ссылки.')
    if 'create_resource' in names:lines.append('- Создавать свои ресурсы по онтологии и работать со своими папками, если запись разрешена; каждое изменение требует вашего подтверждения.')
    lines.append(f'Сейчас подключено ресурсов: **{len(paths)}**. Опираюсь на их данные; недостающие сведения и неполный поиск отмечаю отдельно.')
    return '\n\n'.join([lines[0],'\n'.join(lines[1:-1]),lines[-1]])
