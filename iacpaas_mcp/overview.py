"""Replayable entity overview: attributes plus explicit/formula/composition relations."""
import re
import unicodedata

from .search import located_nodes, normalize
from .graph import walk

PRIMARY={'Химический элемент','Химическое вещество','Материал','Класс материалов','Класс свойств','Свойство','Термин'}


def evaluate(plan,graphs,paths):
    from .knowledge import children,label,composition,table,resolve_material_class
    nodes=[(resource,n,path,pointer) for resource,g in graphs.items() for n,path,pointer in located_nodes(g)]
    subject=normalize(plan.subject.strip())
    if plan.entity_meta=='Класс материалов':
        resolved=resolve_material_class(plan.subject,[label(n) for r,n,p,ptr in nodes if n.get('meta')=='Класс материалов'])
        if resolved:subject=normalize(resolved)
    candidates=[]
    for resource,n,path,pointer in nodes:
        if n.get('meta') not in PRIMARY or not n.get('name'):continue
        if plan.entity_meta and n.get('meta')!=plan.entity_meta:continue
        # Component vertices are mentions, not canonical element cards.
        if n.get('meta')=='Химический элемент' and not any(c.get('meta')=='обозначение' for c in children(n)):continue
        aliases={normalize(label(n)),*(normalize(label(c)) for c in children(n) if c.get('meta')=='обозначение')}
        if subject and subject in aliases:candidates.append((resource,n,path,pointer))
    if len(candidates)!=1:
        return {'status':'ambiguous' if candidates else 'not_found','rows':[],'total':0,'references':[],
                'sections':[{'id':'entity','status':'missing'},{'id':'relations','status':'missing'}],
                'answer':('Найдено несколько карточек: '+ '; '.join(r+' / '+p for r,n,p,ptr in candidates[:10])+'. Уточните объект.' if candidates else 'Карточка объекта **'+plan.subject+'** не найдена в выбранных базах. Укажите точное имя или обозначение; это не доказывает отсутствие сведений в других базах.')}
    resource,node,path,pointer=candidates[0]
    refs=[]
    def reference(r,ptr,title):
        record={'resource':r,'pointer':ptr,'title':title}
        if record not in refs:refs.append(record)
    reference(resource,pointer,label(node))
    attributes=[]
    for leaf,lp,pt in located_nodes(node):
        if node.get('meta','').startswith('Класс ') and (pt.count('/successors/')>1 or (pt and leaf.get('meta') in PRIMARY)):continue
        if leaf.get('comment'):
            attributes.append([lp or 'Описание',leaf['comment']])
            if len(attributes)<=plan.limit:reference(resource,pointer+pt,label(leaf))
        if 'value' in leaf:
            attributes.append([lp.rsplit('/',1)[0] or leaf.get('meta','Значение'),leaf['value']])
            if len(attributes)<=plan.limit:reference(resource,pointer+pt,label(leaf))
    aliases={normalize(label(node)),*(normalize(label(c)) for c in children(node) if c.get('meta')=='обозначение')}
    symbols={str(c.get('value','')) for c in children(node) if c.get('meta')=='обозначение'}
    target_ids={str(node[k]) for k in ('id','original_id') if node.get(k) is not None}
    groups={'substances':[],'materials':[],'other':[]}
    outgoing={normalize(c['element']) for c in composition(node)[0]} if node.get('meta')=='Материал' else set()
    for rr,other,pp,pt in nodes:
        if other.get('meta') not in PRIMARY or not other.get('name') or (rr==resource and pt==pointer):continue
        # Ignore component mentions as independent entity results.
        if other.get('meta')=='Химический элемент' and not any(c.get('meta')=='обозначение' for c in children(other)):continue
        basis=''
        if node.get('meta','').startswith('Класс ') and rr==resource and pt.startswith(pointer+'/'):
            basis='Объект входит в выбранный класс'
        if other.get('meta')=='Материал' and node.get('meta')=='Химический элемент':
            matching=[c for c in composition(other)[0] if normalize(c['element']) in aliases]
            if matching:basis='Компонент указан в составе: '+ '; '.join(c['element']+' '+c['text']+' '+(c.get('unit') or '') for c in matching)
        if not basis and outgoing and other.get('meta')=='Химический элемент':
            if any(normalize(label(c)) in outgoing for c in children(other) if c.get('meta')=='обозначение'):
                basis='Элемент указан в составе выбранного материала'
        if not basis:
            for desc,_ in walk(other):
                if any(str(desc.get(k)) in target_ids for k in ('id','original_id','link_id') if desc.get(k) is not None):
                    basis='Связь по идентификатору вершины';break
                if desc is not other and (normalize(desc.get('name','')) in aliases or normalize(desc.get('value','')) in aliases):
                    basis='Точное обозначение/название внутри карточки';break
                if other.get('meta')=='Химическое вещество' and desc.get('meta')=='обозначение' and node.get('meta')=='Химический элемент':
                    formula=unicodedata.normalize('NFKC',str(desc.get('value','')))
                    if len(formula)<=150 and re.fullmatch(r'(?:[A-Z][a-z]?\d*|[0-9]+|[()[\]{}.+\-·])+',formula):
                        if symbols & set(re.findall(r'[A-Z][a-z]?',formula)):
                            basis='Обозначение элемента в формуле '+formula;break
        if basis:
            group='substances' if other.get('meta')=='Химическое вещество' else 'materials' if other.get('meta')=='Материал' else 'other'
            groups[group].append({'name':label(other),'resource':rr,'path':pp,'pointer':pt,'basis':basis})
    selected=set(plan.overview_sections)
    texts=['**'+label(node)+'** — сведения из подключённых баз.']
    rows=[]
    if 'attributes' in selected:
        texts.append('### Карточка объекта')
        texts.append(table(['Поле / раздел','Данные базы'],attributes[:plan.limit]) if attributes else 'Карточка найдена, но текстовые описания и значения не заполнены.')
        texts.append(f'В карточке найдено полей/описаний: **{len(attributes)}**, показано: {min(len(attributes),plan.limit)}. Неизвестные свойства из памяти модели не добавлены.')
        rows=[['Карточка',*row] for row in attributes[:plan.limit]]
    related={}
    for key,title in [('substances','Связанные вещества'),('materials','Связанные материалы'),('other','Другие связанные объекты')]:
        if key not in selected:continue
        records=sorted(groups[key],key=lambda x:(x['resource'],x['path']))
        related[key]={'total':len(records),'items':records[plan.offset:plan.offset+plan.limit],
                      'has_more':plan.offset+plan.limit<len(records)}
        texts.append('### '+title)
        texts.append(f'Найдено: **{len(records)}**. Связи определены по записанным компонентам, обозначениям, формулам и идентификаторам, а не по памяти модели.')
        if records:
            texts.append(table(['Объект','Основание связи','Ресурс'],[[r['name'],r['basis'],r['resource']] for r in related[key]['items']]))
            if related[key]['has_more']:texts.append(f'Показано {len(related[key]["items"])} из {len(records)}; продолжение: offset={plan.offset+plan.limit}.')
            for r in related[key]['items']:
                reference(r['resource'],r['pointer'],r['name']);rows.append([title,r['name'],r['basis']])
        else:texts.append('В проверенных данных такие связи не найдены. Это не доказывает их отсутствия вне выбранных ресурсов или в неописанных связях.')
    texts.append(f'Проверено ИР: **{len(graphs)}**. Ресурсы с названием «Онтология…» не используются как сведения об объектах. «Указан в составе» не означает ненулевое содержание каждой партии.')
    return {'status':'ok','rows':rows,'total':(len(attributes) if 'attributes' in selected else 0)+sum(v['total'] for v in related.values()),'references':refs,
            'entity':{'name':label(node),'meta':node.get('meta',''),'resource':resource,'path':path},
            'attributes':attributes[:plan.limit] if 'attributes' in selected else [],'attribute_total':len(attributes),'related':related,
            'offset':plan.offset,'limit':plan.limit,'has_more':any(v['has_more'] for v in related.values()),
            'next_offset':plan.offset+plan.limit if any(v['has_more'] for v in related.values()) else None,
            'sections':[{'id':'entity','status':'answered'}, {'id':'relations','status':('answered' if any(v['total'] for v in related.values()) else 'no_matches') if related else 'not_requested'}],
            'answer':'\n\n'.join(texts)}
