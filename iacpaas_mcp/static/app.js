const $ = (id) => document.getElementById(id);
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHTML = (value) => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let config = {}, chats = [], current = null, runId = null, stream = null, pending = null, csrf = '', page = 'chat';
let toastTimer;
let submitting = false;
const toolNames = {query_knowledge:'Запрос к базе материалов',list_inforesources:'Список ресурсов',get_inforesource:'Карточка ресурса',get_subgraph:'Чтение подграфа',search_resources:'Поиск по подключённым базам',count_graph:'Подсчёт графа',query_graph:'Поиск в графе',validate_resource:'Проверка JSON',update_concept:'Изменение понятия',import_inforesource:'Импорт ресурса'};

function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4500); }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf, ...(options.headers || {})}});
  const data = await response.json();
  if (!response.ok) {const error=new Error([typeof data.error==='string'?data.error:`Ошибка сервера: ${response.status}`,data.suggestion].filter(Boolean).join(' · '));error.code=data.code;throw error;}
  return data;
}
function inline(text) {
  return escapeHTML(text).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}
function markdown(text) {
  const lines = String(text).split('\n'); let html = '', list = '', code = false, buffer = [];
  function closeList() { if (list) { html += `</${list}>`; list = ''; } }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith('```')) { closeList(); if (code) { html += `<pre><code>${escapeHTML(buffer.join('\n'))}</code></pre>`; buffer = []; } code = !code; continue; }
    if (code) { buffer.push(line); continue; }
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i+1])) {
      closeList(); const cells = row => row.trim().replace(/^\||\|$/g,'').split('|');
      html += `<div class="table-wrap"><table><thead><tr>${cells(line).map(cell => `<th>${inline(cell.trim())}</th>`).join('')}</tr></thead><tbody>`; i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { html += `<tr>${cells(lines[i]).map(cell => `<td>${inline(cell.trim())}</td>`).join('')}</tr>`; i++; }
      i--; html += '</tbody></table></div>'; continue;
    }
    const item = line.match(/^\s*(?:([-*•])|\d+[.)])\s+(.+)$/);
    if (item) { const type = item[1] ? 'ul' : 'ol'; if (list !== type) { closeList(); html += `<${type}>`; list = type; } html += `<li>${inline(item[2])}</li>`; continue; }
    closeList();
    const heading = line.match(/^#{1,4}\s+(.+)/);
    if (heading) html += `<h3>${inline(heading[1])}</h3>`;
    else if (/^\s*---+\s*$/.test(line)) html += '<hr>';
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  closeList(); if (buffer.length) html += `<pre><code>${escapeHTML(buffer.join('\n'))}</code></pre>`;
  return html;
}

function setState(label, busy = false) {
  $('resource-selector').disabled = busy;
  if (!busy && !config.model_available && label === 'Агент готов к работе') label='Модель выключена';
  $('agent-state').classList.toggle('busy', busy); $('agent-state').lastElementChild.textContent = label;
  $('send').hidden = busy; $('stop').hidden = !busy;
  $('send').disabled = !$('question').value.trim() || !config.model_available;
}
function follow() { const box = $('scroll-area'); box.scrollTop = box.scrollHeight; }
function showPage(name) {
  page = name; $('chat-view').hidden = name !== 'chat'; $('data-view').hidden = name !== 'data';
  for (const tab of ['chat','data']) { $(tab+'-tab').classList.toggle('active', name === tab); $(tab+'-tab').setAttribute('aria-selected', String(name === tab)); }
  if (name === 'data') loadResources();
}
function history() {
  const query = $('search').value.toLocaleLowerCase(); const filtered = chats.filter(chat => chat.title.toLocaleLowerCase().includes(query));
  $('history').replaceChildren();
  if (!filtered.length) { $('history').innerHTML = `<div class="empty-history">${icon('message')}${query ? 'Ничего не найдено' : 'Здесь появятся ваши диалоги.<br>Начните с первого вопроса.'}</div>`; return; }
  let previous = '';
  for (const chat of filtered) {
    const date = new Date(chat.updated * 1000), today = new Date();
    const group = date.toDateString() === today.toDateString() ? 'Сегодня' : 'Ранее';
    if (group !== previous) { const heading = document.createElement('div'); heading.className = 'history-group'; heading.textContent = group; $('history').append(heading); previous = group; }
    const row = document.createElement('div'); row.className = 'history-row' + (current?.id === chat.id ? ' active' : '');
    const open = document.createElement('button'); open.className = 'history-open'; open.textContent = chat.title; open.title = chat.title; open.onclick = () => selectChat(chat.id).catch(error => toast(error.message));
    const remove = document.createElement('button'); remove.className = 'history-delete'; remove.innerHTML = icon('trash'); remove.title = 'Удалить диалог'; remove.setAttribute('aria-label', `Удалить диалог «${chat.title}»`);
    remove.onclick = async () => { if (!confirm('Удалить этот диалог?')) return; try { await api('/api/chats/'+chat.id, {method:'DELETE'}); if (current?.id === chat.id) resetChat(); await refreshHistory(); } catch(error) { toast(error.message); } };
    row.append(open,remove); $('history').append(row);
  }
}
async function refreshHistory() { chats = await api('/api/chats'); history(); }
function resetChat() {
  if (stream) stream.close(); stream = null; runId = null; pending = null; current = null;
  $('messages').replaceChildren(); $('welcome').hidden = false; $('question').value = ''; $('question').style.height = 'auto';
  localStorage.removeItem('iacpaas-chat'); setState('Агент готов к работе'); history(); showPage('chat'); closeSidebar(); $('question').focus();
}
async function selectChat(id) {
  if (stream) stream.close(); stream = null; pending = null; runId = null;
  current = await api('/api/chats/'+id); localStorage.setItem('iacpaas-chat',id);
  renderMessages(); history(); showPage('chat'); closeSidebar();
  if (current.run_id) connectRun(current.run_id); else setState('Агент готов к работе');
  follow();
}
function stepsHTML(steps, busy = false) {
  if (!steps.length) return '';
  return `<details class="steps" ${busy ? 'open' : ''}><summary>${icon('check')}<span>${busy ? 'Агент работает с базой' : 'Шаги работы с базой'}</span><span class="steps-count">${steps.length} MCP</span></summary>${steps.map(step => {
    const result = step.result; const error = step.is_error || result?.is_error || !!result?.error;
    return `<details class="tool-step"><summary><span class="tool-name">${escapeHTML(step.name)}</span><span class="tool-time">${result ? `${step.seconds ?? 0} с` : 'выполняется'}</span><span class="tool-badge ${error ? 'error' : ''}">${error ? 'ошибка' : result ? 'MCP' : '…'}</span></summary><pre>${escapeHTML(JSON.stringify({arguments:step.arguments, ...(result ? {result} : {})},null,2))}</pre></details>`;
  }).join('')}</details>`;
}
function planHTML(report){if(!report?.query_plan)return '';const p=report.query_plan;const names={resources:'Список ресурсов',structure:'Структура баз',materials:'Марки и количество',composition:'Химический состав',element:'Химический элемент',property:'Описание свойства',term:'Карточка термина',rank:'Рейтинг материалов',filter:'Отбор материалов',compare:'Сравнение состава',clarify:'Нужно уточнение'};const detail=[p.subject,p.class_name,p.element].filter(Boolean).join(' · ');return `<div class="interpreted-query">${escapeHTML(names[p.operation]||p.operation)}${detail ? ' · '+escapeHTML(detail):''}</div>`;}
function assistantHeading(model = config.model) { return '<div class="assistant-heading"><img src="/static/logo.svg" alt="">IACPaaS<span><small>'+escapeHTML(model || 'QWEN')+'</small></span></div>'; }
function verificationHTML(report) {
  if(report?.status==='verified_result' && report.rows===0 && report.query_plan?.operation!=='overview') report={...report,status:'empty_result'};
  if (!report?.status) report={status:'not_verified',scope:'',notice:'Для этого исторического ответа проверка не выполнялась. Повторите вопрос, чтобы получить ссылки на снимок базы.'};
  const checked = ['verified_fields','verified_result'].includes(report.status) && report.data_verified!==false;
  const statusLabel={capabilities:'Справка о возможностях',verified_result:'Результат получен; сверка со снимками не применима',blocked:'Ответ не прошёл проверку',needs_clarification:'Запрос требует уточнения',empty_result:'Совпадений нет — проверьте условия',incomplete_result:'Результат неполный: часть баз недоступна',incomplete:report.data_verified?'Данные сверены; вопрос выполнен частично':'Полнота ответа не подтверждена'}[report.status] || 'Нет проверенных фактов базы';
  const sources = report.sources || [];
  const coverageSummary=(sources.length||report.request_coverage)?`<div class="coverage-summary"><span><strong>Данные:</strong> ${checked||report.data_verified?'сверены с источниками':'сверка со снимками не подтверждена'}</span><span><strong>Части вопроса:</strong> ${report.request_coverage?.missing?.length?'остались невыполненные':report.request_coverage?.evaluated?'проверены приложением':'отдельно не проверялись'}</span></div>`:'';
  const snapshots = sources.flatMap(source=>source.exports||[]);
  const partLabels={answered:'проверено',no_matches:'поиск выполнен, совпадений нет',missing:'не проверено',unavailable:'недоступно'};
  const taskProof=report.request_coverage?`<div class="source-proof"><strong>Выполнение частей вопроса</strong>${report.request_coverage.parts.length?report.request_coverage.parts.map(p=>`<p>${escapeHTML(p.label)}: ${escapeHTML(partLabels[p.status]||p.status)}</p>`).join(''):'<p>Отдельный контроль частей для этого вопроса не задан; достаточность оценила модель.</p>'}<p>Сверка значений с базой и полнота ответа — разные проверки.</p></div>`:'';
  const extraProof = taskProof + (snapshots.length ? `<div class="search-coverage"><strong>Экспорты проверенных баз</strong>${snapshots.map(source=>/^[a-f0-9]{64}$/.test(source.export_sha256||'') ? `<a href="/api/exports/${source.export_sha256}" download>${escapeHTML(source.path)} · ${escapeHTML(new Date(source.retrieved_at).toLocaleString('ru'))}</a>`:'').join('')}</div>` : '');
  return coverageSummary + `<details class="verification ${checked ? 'checked' : 'unverified'}"><summary>${icon(checked ? 'check' : 'info')}<span>${checked ? (report.status === 'verified_result' ? 'Данные и расчёты сверены со снимками' : 'Поля сверены со снимком') : statusLabel}</span><span>${checked ? (report.status === 'verified_result' ? (Number.isInteger(report.rows)?'Строк: '+report.rows:'Источников: '+sources.length) : 'Поля: '+report.claims.length) : ''}</span></summary><div class="verification-body">${report.query_plan ? `<div class="query-plan"><strong>Как понят вопрос</strong><pre>${escapeHTML(JSON.stringify(report.query_plan,null,2))}</pre><small>Если выбран неверный объект или условие, отредактируйте вопрос.</small></div>` : ''}<p><strong>Область запроса:</strong> ${escapeHTML(report.scope || 'Все подключённые базы')}</p><p>${escapeHTML(report.notice || '')}</p>${report.assessment ? `<p><strong>Оценка достаточности агентом:</strong> ${escapeHTML(report.assessment)}</p>` : ''}${sources.map(source => `<div class="source-proof"><strong>${escapeHTML(source.id)} · ${escapeHTML(source.tool)}</strong><pre>${escapeHTML(JSON.stringify(source.arguments,null,2))}</pre><small>Получено: ${escapeHTML(new Date(source.retrieved_at).toLocaleString('ru'))}<br>SHA-256 результата: ${escapeHTML(source.sha256)}</small>${source.export?.export_sha256 && /^[a-f0-9]{64}$/.test(source.export.export_sha256) ? `<a href="/api/exports/${source.export.export_sha256}" download>Скачать исходный снимок экспорта</a>` : ''}</div>`).join('')}${extraProof}</div></details>`;
}
function renderMessages() {
  $('welcome').hidden = !!current?.messages.length; $('messages').replaceChildren();
  for (const [messageIndex,message] of (current?.messages || []).entries()) {
    const article = document.createElement('article'); article.className = 'turn';
    if (message.role === 'user') { article.className = 'user-message'; article.textContent = message.content;
      const edit=document.createElement('button'); edit.className='edit-question'; edit.textContent='Изменить вопрос'; edit.title='Создать ветку и исправить вопрос';
      edit.onclick=async()=>{try{const text=message.content;const branch=await api('/api/chats/'+current.id+'/branch',{method:'POST',body:JSON.stringify({index:messageIndex})});await selectChat(branch.id);$('question').value=text;$('question').style.height='auto';$('question').focus();setState('Агент готов к работе');await refreshHistory();toast('Создана ветка. Исправьте вопрос и отправьте.');}catch(error){toast(error.message)}};
      article.append(edit); }
    else {
      article.innerHTML = assistantHeading(message.model) + planHTML(message.verification) + stepsHTML(message.steps || []) + `<div class="${message.status === 'error' ? 'error-message' : 'answer'}">${markdown(message.content)}</div>` + verificationHTML(message.verification);
      const footer = document.createElement('div'); footer.className = 'message-footer';
      const tokens = message.metrics?.tokens_out; footer.innerHTML = `<span>${message.seconds ?? 0} с</span>${tokens ? `<span>${tokens.toLocaleString('ru')} ${message.verification?.query_plan ? 'токенов плана' : 'токенов ответа'}</span>` : ''}<span>Вызовы MCP: ${(message.steps || []).length}</span>${message.context?.compressed ? `<span>Контекст сжат: ≈${message.context.before_estimate} → ≈${message.context.after_estimate}</span>` : ''}`;
      const copy = document.createElement('button'); copy.className = 'copy-button'; copy.innerHTML = icon('copy') + 'Копировать'; copy.onclick = () => navigator.clipboard.writeText(message.content).then(() => toast('Ответ скопирован')).catch(() => toast('Не удалось скопировать ответ'));
      footer.append(copy);
      const pageResult=[...(message.steps || [])].reverse().find(s=>s.name==='query_knowledge' && !s.is_error)?.result;
      if(messageIndex===current.messages.length-1 && pageResult?.has_more && message.status==='done'){
        const next=document.createElement('button');next.className='copy-button';next.textContent='Следующие '+pageResult.limit;
        next.onclick=()=>{if(runId)return;$('question').value='Покажи следующие '+pageResult.limit;sendMessage();};footer.append(next);
      }
      if (message.status === 'done' && message.steps?.some(s=>!s.is_error)) {
        const review=document.createElement('button'); review.className='copy-button'; review.textContent='Проверить смысл · LLM';
        review.title='Повторная сверка источников и выборочная проверка до 8 утверждений моделью, до 120 секунд. Облачный запрос оплачивается провайдеру.';
        review.onclick=async()=>{
          if(config.review_version!==3){toast('Перезапустите Web-приложение и обновите страницу: сервер использует старую проверку.');return;}
          const chatId=current.id,started=Date.now();review.disabled=true;
          const tick=()=>{review.textContent=`Проверка: ${Math.floor((Date.now()-started)/1000)} с / 120 с`;};tick();
          const timer=setInterval(tick,1000),cancel=document.createElement('button');cancel.className='copy-button';cancel.textContent='Отменить проверку';footer.append(cancel);
          cancel.onclick=async()=>{cancel.disabled=true;try{await api('/api/chats/'+chatId+'/verify/cancel',{method:'POST',body:'{}'});}catch(error){toast(error.message);cancel.disabled=false;}};
          try{await api('/api/chats/'+chatId+'/verify',{method:'POST',body:JSON.stringify({index:messageIndex})});if(current?.id===chatId)await selectChat(chatId);else toast('Проверка завершена и сохранена');}
          catch(error){toast(error.message);}
          finally{clearInterval(timer);cancel.remove();review.disabled=false;review.textContent='Проверить смысл · LLM';}
        };
        footer.append(review);
      }
      article.append(footer);
      if (message.claim_review) {
        const panel=document.createElement('details'); panel.className='verification';
        const report=message.claim_review;const summary=document.createElement('summary');
        summary.textContent=report.version>=2?`Смысловая проверка: подтверждено ${report.confirmed} из ${report.total}${report.reference_errors?' · ошибок ссылок: '+report.reference_errors:''}`:'Устаревший результат проверки — запустите новую';panel.append(summary);
        const notice=document.createElement('p');notice.textContent=report.notice;panel.append(notice);
        if(report.version>=2){
          const labels={confirmed:'Поддерживается источником',not_in_data:'Недостаточно данных',contradicted:'Противоречит источнику',reference_error:'Ошибка ссылки проверяющего'};
          const proof=document.createElement('p');proof.className='review-replay';proof.textContent=`Повторная сверка MCP: ${report.replay?.length||0} результатов · ${report.seconds} с`;panel.append(proof);
          for(const claim of report.claims){const row=document.createElement('div');row.className='review-claim';const label=document.createElement('strong');label.textContent=labels[claim.status]||claim.status;
            const quote=document.createElement('p');quote.textContent=claim.quote;row.append(label,quote);
            if(claim.evidence){const evidence=document.createElement('p');evidence.className='review-evidence';evidence.textContent='Источник: '+claim.evidence;row.append(evidence);}
            if(claim.pointer){const ref=document.createElement('small');ref.textContent=claim.source_id+claim.pointer;row.append(ref);}panel.append(row);}
        }else{const legacy=document.createElement('p');legacy.textContent='Старая проверка смешивала ошибки формата ссылки с отсутствием данных и допускала ссылку на сам ответ. Этот результат нельзя считать достоверным.';panel.append(legacy);}
        article.append(panel);
      }
    }
    $('messages').append(article);
  }
}
function renderPending(label = 'Модель готовит ответ') {
  if (!pending) return;
  pending.element.innerHTML = assistantHeading() + stepsHTML(pending.steps, true) + `<div class="thinking-line"><span class="thinking-dots"><span></span><span></span><span></span></span><span>${escapeHTML(label)}</span></div>`;
  follow();
}
function connectRun(id) {
  runId = id; const element = document.createElement('article'); element.className = 'turn'; $('messages').append(element);
  pending = {element, steps:[]}; renderPending(); setState('Агент работает',true);
  stream = new EventSource(`/api/runs/${id}/events`);
  stream.onmessage = async ({data}) => {
    if (runId !== id || !pending) return;
    const event = JSON.parse(data);
    if (event.event === 'queued') { renderPending('Запрос в очереди к модели'); setState('Ожидание модели',true); }
    if (event.event === 'started' || event.event === 'thinking') { renderPending('Агент выбирает следующий шаг'); setState('Агент работает',true); }
    if (event.event === 'tool_start') { pending.steps.push(event); renderPending(toolNames[event.name] || 'Вызов инструмента'); }
    if (event.event === 'tool') {
      const index = pending.steps.findLastIndex(step => step.name === event.name && !step.result);
      if (index >= 0) pending.steps[index] = event; else pending.steps.push(event);
      renderPending('Модель готовит ответ по данным базы');
    }
    if (event.event === 'replanning') renderPending('Агент корректирует план: '+event.message);
    if (event.event === 'approval') showApproval(event);
    if (event.event === 'context') { $('context-indicator').textContent=event.compressed ? `Сжато: ≈${event.before_estimate} → ≈${event.after_estimate}` : `Контекст: ≈${event.after_estimate} / ${event.budget}${event.context_length ? ' · окно '+Math.round(event.context_length/1024)+'K' : ''}`; }
    if (event.event === 'verification') { pending.verification=event; }
    if (event.event === 'approval_expired') { toast('Время подтверждения истекло. Изменение отменено.'); renderPending('Изменение отменено'); }
    if (event.event === 'answer') { pending.element.innerHTML = assistantHeading() + planHTML(pending.verification) + stepsHTML(pending.steps) + `<div class="answer">${markdown(event.answer)}</div>` + verificationHTML(pending.verification); follow(); }
    if (event.event === 'done') {
      stream.close(); stream = null; runId = null; pending = null;
      current = await api('/api/chats/'+current.id); renderMessages(); setState('Агент готов к работе'); await refreshHistory(); follow();
    }
  };
  stream.onerror = () => {
    if (stream?.readyState === EventSource.CLOSED) { setState('Соединение прервано'); toast('Соединение прервано. Откройте диалог заново.'); }
  };
}
function showApproval(event) {
  setState('Нужно ваше подтверждение',true); renderPending('Ожидается подтверждение изменения');
  const panel = document.createElement('section'); panel.className = 'approval';
  panel.innerHTML = `<strong>Разрешить изменение данных?</strong><p>Проверьте параметры. Без вашего согласия изменение выполнено не будет.</p><pre>${escapeHTML(JSON.stringify(event.preview,null,2))}</pre>`;
  const actions = document.createElement('div'); actions.className = 'approval-actions';
  for (const approved of [false,true]) {
    const button = document.createElement('button'); button.className = approved ? 'primary' : 'secondary'; button.textContent = approved ? 'Подтвердить изменение' : 'Отменить';
    button.onclick = async () => { try { await api(`/api/runs/${runId}/approve`, {method:'POST',body:JSON.stringify({approval_id:event.approval_id,approved})}); panel.remove(); setState('Агент работает',true); } catch(error) { toast(error.message); } };
    actions.append(button);
  }
  panel.append(actions); pending.element.append(panel); follow();
}
async function sendMessage(event) {
  event?.preventDefault(); if (runId || submitting) return;
  const message = $('question').value.trim(); if (!message) return;
  if(config.dialogue_version!==1){toast('Перезапустите Web-приложение и обновите страницу: сервер использует старую логику диалога.');return;}
  if (!config.model_available) { toast('Модель недоступна. Проверьте настройки моделей и API или запуск Ollama.'); return; }
  submitting = true; $('send').disabled = true;
  try {
    if (!current) { current = await api('/api/chats', {method:'POST',body:'{}'}); localStorage.setItem('iacpaas-chat',current.id); }
    const result = await api('/api/chat',{method:'POST',body:JSON.stringify({chat_id:current.id,message,resource_path:$('resource-selector').value})});
    current.messages.push({role:'user',content:message}); $('question').value=''; $('question').style.height='auto';
    renderMessages(); showPage('chat'); connectRun(result.run_id); await refreshHistory(); follow();
  } catch(error) { toast(error.message); setState('Агент готов к работе'); }
  finally { submitting = false; }
}
async function loadResources() {
  $('resources').textContent = 'Получаем ресурсы через MCP…';
  try {
    const data = await api('/api/resources'); $('resources').replaceChildren();
    if (!data.resources.length) { $('resources').innerHTML='<p class="data-empty">Ресурсы пока не настроены. Добавьте пути ИР в IACPAAS_KNOWN_IRS.</p>'; return; }
    for (const resource of data.resources) {
      const card=document.createElement('article');card.className='resource';
      card.innerHTML=`<div class="resource-header">${icon('database')}<div><strong>${escapeHTML(resource.path.split('/').at(-1))}</strong><small>${escapeHTML(resource.path)}</small></div><button>Посмотреть структуру</button></div>`;
      const button=card.querySelector('button');
      button.onclick=async()=>{
        const existing=card.querySelector('.resource-tree');if(existing){existing.remove();button.textContent='Посмотреть структуру';return;}
        button.disabled=true;button.textContent='Загрузка…';
        try { const result=await api('/api/resource/children?path='+encodeURIComponent(resource.path));const tree=document.createElement('div');tree.className='resource-tree';tree.append(navigationNode({graph:result.parent,path:'',has_children:result.total>0},resource.path,result._source.export_sha256,result));card.append(tree);button.textContent='Свернуть'; }
        catch(error){toast(error.message);button.textContent='Повторить';}finally{button.disabled=false;}
      };
      $('resources').append(card);
    }
  }catch(error){$('resources').textContent=error.message;}
}
function navigationNode(info,resourcePath,snapshot,initialPage=null){
 const branch=document.createElement(info.has_children?'details':'div');branch.className='tree-node';
 const label=document.createElement(info.has_children?'summary':'span'),node=info.graph;
 const value=node.value??(node.value_preview?node.value_preview+'…':null);
 label.textContent=(info.is_link?'↗ ':'')+(node.name?(node.name+(value!==null?': '+value:'')):(value??'Вершина'));
 const meta=document.createElement('small');meta.textContent=node.meta||node.type||'';label.append(meta);branch.append(label);
 if(!info.has_children)return branch;
 const content=document.createElement('div');branch.append(content);let loaded=false,busy=false,next=0,more=null;
 const error=document.createElement('p');error.className='tree-error';error.setAttribute('role','alert');
 const apply=page=>{more?.remove();for(const child of page.children)content.append(navigationNode(child,resourcePath,page._source.export_sha256));loaded=true;next=page.next_offset;snapshot=page._source.export_sha256;
  if(page.has_more){more=document.createElement('button');more.className='tree-more';more.textContent=`Показать ещё · ${page.offset+page.children.length} из ${page.total}`;more.onclick=()=>load();content.append(more);}
 };
 const load=async()=>{if(busy)return;busy=true;error.remove();if(more)more.disabled=true;
  try{const query=new URLSearchParams({path:resourcePath,root:info.path,offset:String(next??0),limit:'20',snapshot});apply(await api('/api/resource/children?'+query));}
  catch(e){error.textContent=e.message;content.append(error);if(!loaded&&!more){more=document.createElement('button');more.className='tree-more';more.textContent='Повторить загрузку';more.onclick=()=>load();content.append(more);}}
  finally{busy=false;if(more)more.disabled=false;}
 };
 branch.addEventListener('toggle',()=>{if(branch.open&&!loaded)load();});
 if(initialPage){apply(initialPage);branch.open=true;}
 return branch;
}
function closeSidebar(){document.body.classList.remove('mobile-open');}
function theme(value){document.documentElement.dataset.theme=value;localStorage.setItem('iacpaas-theme',value);$('theme-toggle').innerHTML=icon(value==='dark'?'sun':'moon');}
async function loadResourceChoices(){const resources=await api('/api/resources');const selector=$('resource-selector');selector.replaceChildren(new Option('Все подключённые базы',''));for(const resource of resources.resources)selector.append(new Option(resource.path,resource.path));const selected=localStorage.getItem('iacpaas-search-scope-v2');if(resources.resources.some(resource=>resource.path===selected))selector.value=selected;}
async function openAccountSettings(){
  $('account-error').hidden=true;$('account-password').value='';
  try{const account=await api('/api/account');$('account-username').value=account.username||'';$('account-paths').value=(account.paths||[]).join('\n');$('account-status').textContent=account.password_saved?'Пароль сохранён в защищённом хранилище Windows.':'Сохранённого пароля пока нет.';$('account-dialog').showModal();}
  catch(error){toast(error.message);}
}
async function refreshStatus(){
  config=await api('/api/status');csrf=config.csrf;
  if(!current && !runId && config.context_length)$('context-indicator').textContent=`Контекст: ${config.context_length/1024}K · бюджет ≈${config.context_budget}`;
  $('model-name').textContent=config.model;document.getElementById('model-location').textContent=config.cloud?'Облачный API':'Ваш сервер Ollama';$('composer-model').textContent=config.model+(config.cloud?' · облако':' · Ollama');
  $('stop-app').disabled=!!config.docker;$('stop-app').title=config.docker?'Остановка контейнера: docker compose stop atlas. Ollama управляется отдельно.':'Остановить приложение';
  $('model-dot').classList.toggle('offline',!config.model_available);
  const mock=config.backend==='mock';
  $('workspace-name').textContent=mock?'Учебное пространство':'Фонд IACPaaS';$('workspace-description').textContent=mock?'Демонстрационные данные':'Ресурсы из конфигурации';
  $('welcome-note').textContent=mock?'Учебная база подключена. Можно исследовать.':'Настроено подключение к IACPaaS. Данные проверяются при запросе.';
  $('backend-hint').textContent=(mock?'Учебные данные':'IACPaaS')+` · MCP ${config.tools}`;
  $('connection-notice').hidden=config.model_available;
  $('connection-notice').textContent=config.cloud?'Для облачной модели сохраните API-ключ в разделе «Модели и API».':'Ollama недоступна или модель не найдена. Укажите адрес Ollama и точное имя модели в разделе «Модели и API».';
  setState(runId?'Агент работает':config.model_available?(config.cloud?'Облако настроено · доступ не проверен':'Агент готов к работе'):'Модель выключена',!!runId);
}
$('new-chat').onclick=resetChat;$('search').oninput=history;$('composer').onsubmit=sendMessage;
$('question').oninput=()=>{const el=$('question');el.style.height='auto';el.style.height=Math.min(el.scrollHeight,140)+'px';$('send').disabled=!el.value.trim()||!config.model_available;};
$('question').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMessage();}};
$('stop').onclick=async()=>{if(!runId)return;try{await api(`/api/runs/${runId}/cancel`,{method:'POST',body:'{}'});}catch(error){toast(error.message);}};
$('suggestions').onclick=event=>{const card=event.target.closest('[data-prompt]');if(card){$('question').value=card.dataset.prompt;$('question').dispatchEvent(new Event('input'));$('question').focus();}};
$('chat-tab').onclick=()=>showPage('chat');$('data-tab').onclick=()=>showPage('data');
$('sidebar-toggle').onclick=()=>{if(innerWidth<=760)document.body.classList.toggle('mobile-open');else document.body.classList.toggle('sidebar-collapsed');};
$('sidebar-overlay').onclick=closeSidebar;
$('theme-toggle').onclick=()=>theme(document.documentElement.dataset.theme==='dark'?'light':'dark');
$('about').onclick=()=>{const values=[['Модель',config.model||'—'],['Среда',config.cloud?config.provider+' · облачный API':'Локальная Ollama'],['Окно приложения',config.context_length||'—'],['Бюджет входа',config.context_budget||'—'],['Лимит генерации',config.output_limit||'—'],['Данные',config.backend==='mock'?'Учебный граф':'IACPaaS REST'],['MCP-инструменты',config.tools||'—'],['Запись',config.write_enabled?'С подтверждением':'Отключена'],['История','На этом компьютере']];$('about-details').innerHTML=values.map(([key,value])=>`<dt>${key}</dt><dd>${escapeHTML(value)}</dd>`).join('');$('about-dialog').showModal();};
document.querySelectorAll('.dialog-close').forEach(button=>button.onclick=()=>$('about-dialog').close());
$('export').onclick=()=>{if(!current?.messages.length){toast('Сначала начните диалог');return;}const text='# '+current.title+'\n\n'+current.messages.map(message=>`## ${message.role==='user'?'Вы':'IACPaaS'}\n\n${message.content}`).join('\n\n');const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download='IACPaaS-'+current.id.slice(0,8)+'.md';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.shiftKey&&event.key.toLowerCase()==='o'){event.preventDefault();resetChat();}if(event.key==='/'&&!['INPUT','TEXTAREA'].includes(document.activeElement.tagName)){event.preventDefault();$('search').focus();}});
theme(localStorage.getItem('iacpaas-theme')||'light');
$('resource-selector').onchange=()=>localStorage.setItem('iacpaas-search-scope-v2',$('resource-selector').value);
$('account-settings').onclick=openAccountSettings;
$('stop-app').onclick=async()=>{const button=$('stop-app');button.disabled=true;try{await api('/api/app/stop',{method:'POST',body:'{}'});if(stream)stream.close();stream=null;runId=null;config.model_available=false;setState('Приложение остановлено');$('connection-notice').hidden=false;$('connection-notice').textContent='Web и Qwen останавливаются. Для запуска используйте кнопку «Запустить всё» в окне управления АТЛАС или Start-Web.cmd.';toast('Останавливаю Web и освобождаю память Qwen.');}catch(error){button.disabled=false;toast(error.message)}};
document.querySelectorAll('.account-close').forEach(button=>button.onclick=()=>{$('account-password').value='';$('account-dialog').close();});
$('account-dialog').addEventListener('close',()=>{$('account-password').value='';});
$('account-form').onsubmit=async(event)=>{event.preventDefault();$('account-save').disabled=true;$('account-error').hidden=true;try{await api('/api/account',{method:'POST',body:JSON.stringify({username:$('account-username').value,password:$('account-password').value,paths:$('account-paths').value.split('\n').map(path=>path.trim()).filter(Boolean)})});$('account-password').value='';$('account-dialog').close();await refreshStatus();await loadResourceChoices();if(page==='data')await loadResources();toast('Аккаунт сохранён и подключён');}catch(error){$('account-error').hidden=false;$('account-error').textContent=error.message;}finally{$('account-save').disabled=false;}};
(async()=>{try{await refreshStatus();await refreshHistory();await loadResourceChoices();const saved=localStorage.getItem('iacpaas-chat');if(saved&&chats.some(chat=>chat.id===saved))await selectChat(saved);}catch(error){$('connection-notice').hidden=false;$('connection-notice').textContent='Не удалось подключиться к приложению. '+error.message;}setInterval(()=>refreshStatus().catch(()=>{}),30000);})();

async function loadQuestionBank(){
  const response=await fetch('/static/questions.json');if(!response.ok)return;
  const bank=await response.json();$('question-count').textContent=bank.length;
  const categories=[...new Set(bank.map(item=>item.category))];
  $('question-groups').innerHTML=categories.map(category=>`<section class="question-category"><h3>${escapeHTML(category)}</h3><div>${bank.filter(item=>item.category===category).map(item=>`<button type="button" data-prompt="${escapeHTML(item.question)}">${escapeHTML(item.question)}</button>`).join('')}</div></section>`).join('');
  $('question-groups').onclick=event=>{const button=event.target.closest('[data-prompt]');if(button){$('question').value=button.dataset.prompt;$('question').dispatchEvent(new Event('input'));$('question').focus();toast('Вопрос подготовлен. Нажмите Enter для отправки.');}};
}
loadQuestionBank().catch(()=>{});

let modelSettings = null;
function modelFields() {
  const item=modelSettings.providers.find(p=>p.id===$('llm-provider').value);
  const local=item.id==='ollama';
  $('llm-model-list').innerHTML=item.models.map(model=>`<option value="${escapeHTML(model)}"></option>`).join('');
  $('llm-model').value=item.id===modelSettings.provider?modelSettings.model:(item.saved_model||item.models[0]);
  $('llm-key').value='';$('llm-delete-key').checked=false;$('llm-key').disabled=local;$('llm-delete-key').disabled=local;
  $('llm-key-status').textContent=local?'Ollama работает без API-ключа.':item.key_source==='environment'?'Ключ задан переменной окружения. Она имеет приоритет; удаление здесь затронет только сохранённую копию.':item.key_saved?'Ключ сохранён на сервере. Пустое поле оставит его прежним.':'Ключ ещё не сохранён.';
  $('llm-disclosure').hidden=local;$('llm-error').hidden=true;$('llm-test-result').textContent='';
  $('llm-endpoint-label').hidden=!local;$('llm-endpoint').value=modelSettings.ollama_url||'';$('llm-endpoint').required=local;
  $('llm-check').disabled=item.id!==modelSettings.provider;
}
async function openModelSettings() {
  try {
    modelSettings=await api('/api/llm');
    $('llm-provider').innerHTML=modelSettings.providers.map(p=>`<option value="${p.id}">${escapeHTML(p.name)}</option>`).join('');
    $('llm-provider').value=modelSettings.provider;modelFields();$('model-dialog').showModal();
  } catch(error){toast(error.message);}
}
$('model-settings').onclick=openModelSettings;
$('model-name').parentElement.style.cursor='pointer';
$('model-name').parentElement.onclick=openModelSettings;
$('llm-provider').onchange=modelFields;
document.querySelectorAll('.model-close').forEach(button=>button.onclick=()=>$('model-dialog').close());
$('model-dialog').addEventListener('close',()=>{$('llm-key').value='';});
$('model-form').onsubmit=async event=>{
  event.preventDefault();$('llm-save').disabled=true;$('llm-error').hidden=true;
  try {
    modelSettings=await api('/api/llm',{method:'POST',body:JSON.stringify({provider:$('llm-provider').value,model:$('llm-model').value.trim(),api_key:$('llm-key').value.trim(),delete_key:$('llm-delete-key').checked,...($('llm-provider').value==='ollama'?{ollama_url:$('llm-endpoint').value.trim()}:{})})});
    $('llm-key').value='';modelFields();await refreshStatus();$('question').dispatchEvent(new Event('input'));toast('Модель сохранена');
  } catch(error){$('llm-error').hidden=false;$('llm-error').textContent=error.message;}
  finally{$('llm-save').disabled=false;}
};
$('llm-check').onclick=async()=>{
  $('llm-check').disabled=true;$('llm-test-result').textContent='Проверяю сохранённую модель…';
  try {const result=await api('/api/llm/check',{method:'POST',body:'{}'});
    if(result.local){$('llm-test-result').textContent=`Ollama доступна: ${result.endpoint}. ${result.available?'Модель найдена.':'Выбранная модель не найдена.'} Доступны: ${result.models.join(', ')||'нет установленных моделей'}`;$('llm-model-list').innerHTML=result.models.map(m=>`<option value="${escapeHTML(m)}"></option>`).join('');}
    else $('llm-test-result').textContent=`API отвечает: ${result.model}. Токены: ${result.tokens_in} вход / ${result.tokens_out} выход.`;}
  catch(error){$('llm-test-result').textContent=error.message;}
  finally{$('llm-check').disabled=false;}
};

const atlasIndexButtons=document.createElement('div');atlasIndexButtons.className='index-controls';
const atlasIndexStatus=document.createElement('pre');atlasIndexStatus.className='index-status';
for(const [label,force] of [['Сверить изменения IACPaaS',false],['Загрузить все ресурсы заново',true]]){
 const button=document.createElement('button');button.className='copy-button';button.textContent=label;
 button.onclick=async()=>{button.disabled=true;atlasIndexStatus.textContent='Сверяю ресурсы…';try{
 const result=await api('/api/index',{method:'POST',body:JSON.stringify({force})});
 atlasIndexStatus.textContent=result.resources.map(r=>`${r.path}: ${r.status}${r.source?.retrieved_at?' · снимок '+r.source.retrieved_at:''}${r.error?' · '+r.error:''}`).join('\n');
 }catch(error){atlasIndexStatus.textContent=error.message;}finally{button.disabled=false;}};
 atlasIndexButtons.append(button);
}
$('resources').before(atlasIndexButtons,atlasIndexStatus);
