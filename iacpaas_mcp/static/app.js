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
  if (!response.ok) throw new Error(data.error || `Ошибка сервера: ${response.status}`);
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
    const result = step.result; const error = result?.is_error;
    return `<details class="tool-step"><summary><span class="tool-name">${escapeHTML(step.name)}</span><span class="tool-time">${result ? `${step.seconds ?? 0} с` : 'выполняется'}</span><span class="tool-badge ${error ? 'error' : ''}">${error ? 'ошибка' : result ? 'MCP' : '…'}</span></summary><pre>${escapeHTML(JSON.stringify({arguments:step.arguments, ...(result ? {result} : {})},null,2))}</pre></details>`;
  }).join('')}</details>`;
}
function planHTML(report){if(!report?.query_plan)return '';const p=report.query_plan;const names={resources:'Список ресурсов',structure:'Структура баз',materials:'Марки и количество',composition:'Химический состав',element:'Химический элемент',property:'Описание свойства',term:'Карточка термина',rank:'Рейтинг материалов',filter:'Отбор материалов',compare:'Сравнение состава',clarify:'Нужно уточнение'};const detail=[p.subject,p.class_name,p.element].filter(Boolean).join(' · ');return `<div class="interpreted-query">${escapeHTML(names[p.operation]||p.operation)}${detail ? ' · '+escapeHTML(detail):''}</div>`;}
function assistantHeading() { return '<div class="assistant-heading"><img src="/static/logo.svg" alt="">IACPaaS<span><small>QWEN</small></span></div>'; }
function verificationHTML(report) {
  if(report?.status==='verified_result' && report.rows===0) report={...report,status:'empty_result'};
  if (!report?.status) report={status:'not_verified',scope:'',notice:'Для этого исторического ответа проверка не выполнялась. Повторите вопрос, чтобы получить ссылки на снимок базы.'};
  const checked = ['verified_fields','verified_result'].includes(report.status);
  const statusLabel={blocked:'Ответ не прошёл проверку',needs_clarification:'Запрос требует уточнения',empty_result:'Совпадений нет — проверьте условия',incomplete_result:'Результат неполный: часть баз недоступна'}[report.status] || 'Нет проверенных фактов базы';
  const sources = report.sources || [];
  const snapshots = sources.flatMap(source=>source.exports||[]);
  const extraProof = snapshots.length ? `<div class="search-coverage"><strong>Экспорты проверенных баз</strong>${snapshots.map(source=>/^[a-f0-9]{64}$/.test(source.export_sha256||'') ? `<a href="/api/exports/${source.export_sha256}" download>${escapeHTML(source.path)} · ${escapeHTML(new Date(source.retrieved_at).toLocaleString('ru'))}</a>`:'').join('')}</div>` : '';
  return `<details class="verification ${checked ? 'checked' : 'unverified'}"><summary>${icon(checked ? 'check' : 'info')}<span>${checked ? (report.status === 'verified_result' ? 'Запрос, расчёты и ответ сверены' : 'Поля сверены со снимком') : statusLabel}</span><span>${checked ? (report.status === 'verified_result' ? 'Строк: '+report.rows : 'Поля: '+report.claims.length) : ''}</span></summary><div class="verification-body">${report.query_plan ? `<div class="query-plan"><strong>Как понят вопрос</strong><pre>${escapeHTML(JSON.stringify(report.query_plan,null,2))}</pre><small>Если выбран неверный объект или условие, отредактируйте вопрос.</small></div>` : ''}<p><strong>Область запроса:</strong> ${escapeHTML(report.scope || 'Все подключённые базы')}</p><p>${escapeHTML(report.notice || '')}</p>${sources.map(source => `<div class="source-proof"><strong>${escapeHTML(source.id)} · ${escapeHTML(source.tool)}</strong><pre>${escapeHTML(JSON.stringify(source.arguments,null,2))}</pre><small>Получено: ${escapeHTML(new Date(source.retrieved_at).toLocaleString('ru'))}<br>SHA-256 результата: ${escapeHTML(source.sha256)}</small>${source.export?.export_sha256 && /^[a-f0-9]{64}$/.test(source.export.export_sha256) ? `<a href="/api/exports/${source.export.export_sha256}" download>Скачать исходный снимок экспорта</a>` : ''}</div>`).join('')}${extraProof}</div></details>`;
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
      article.innerHTML = assistantHeading() + planHTML(message.verification) + stepsHTML(message.steps || []) + `<div class="${message.status === 'error' ? 'error-message' : 'answer'}">${markdown(message.content)}</div>` + verificationHTML(message.verification);
      const footer = document.createElement('div'); footer.className = 'message-footer';
      const tokens = message.metrics?.tokens_out; footer.innerHTML = `<span>${message.seconds ?? 0} с</span>${tokens ? `<span>${tokens.toLocaleString('ru')} ${message.verification?.query_plan ? 'токенов плана' : 'токенов ответа'}</span>` : ''}<span>Вызовы MCP: ${(message.steps || []).length}</span>${message.context?.compressed ? `<span>Контекст сжат: ≈${message.context.before_estimate} → ≈${message.context.after_estimate}</span>` : ''}`;
      const copy = document.createElement('button'); copy.className = 'copy-button'; copy.innerHTML = icon('copy') + 'Копировать'; copy.onclick = () => navigator.clipboard.writeText(message.content).then(() => toast('Ответ скопирован')).catch(() => toast('Не удалось скопировать ответ'));
      footer.append(copy); article.append(footer);
    }
    $('messages').append(article);
  }
}
function renderPending(label = 'Qwen готовит ответ') {
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
    if (event.event === 'queued') { renderPending('Запрос в очереди к локальной модели'); setState('Ожидание модели',true); }
    if (event.event === 'started' || event.event === 'thinking') { renderPending('Qwen изучает вопрос'); setState('Агент работает',true); }
    if (event.event === 'tool_start') { pending.steps.push(event); renderPending(toolNames[event.name] || 'Вызов инструмента'); }
    if (event.event === 'tool') {
      const index = pending.steps.findLastIndex(step => step.name === event.name && !step.result);
      if (index >= 0) pending.steps[index] = event; else pending.steps.push(event);
      renderPending('Qwen готовит ответ по данным базы');
    }
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
  if (!config.model_available) { toast('Qwen недоступен. Запустите Start-Web.cmd и обновите страницу.'); return; }
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
        try { const result=await api('/api/resource?path='+encodeURIComponent(resource.path));const tree=document.createElement('div');tree.className='resource-tree';tree.append(treeNode(result.graph));card.append(tree);button.textContent='Свернуть'; }
        catch(error){toast(error.message);button.textContent='Повторить';}finally{button.disabled=false;}
      };
      $('resources').append(card);
    }
  }catch(error){$('resources').textContent=error.message;}
}
function treeNode(node){const branch=document.createElement(node.successors?.length?'details':'div');branch.className='tree-node';if(branch.tagName==='DETAILS')branch.open=true;const label=document.createElement(branch.tagName==='DETAILS'?'summary':'span');label.textContent=node.name ?? node.value ?? (node.link?'↗ Ссылка':'Вершина');const meta=document.createElement('small');meta.textContent=node.meta||node.type||'';label.append(meta);branch.append(label);for(const child of node.successors||[])branch.append(treeNode(child));return branch;}
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
  $('model-name').textContent=config.model;$('composer-model').textContent=config.model+' · локально';
  $('model-dot').classList.toggle('offline',!config.model_available);
  const mock=config.backend==='mock';
  $('workspace-name').textContent=mock?'Учебное пространство':'Фонд IACPaaS';$('workspace-description').textContent=mock?'Демонстрационные данные':'Ресурсы из конфигурации';
  $('welcome-note').textContent=mock?'Учебная база подключена. Можно исследовать.':'Настроено подключение к IACPaaS. Данные проверяются при запросе.';
  $('backend-hint').textContent=(mock?'Учебные данные':'IACPaaS')+` · MCP ${config.tools}`;
  $('connection-notice').hidden=config.model_available;
  $('connection-notice').textContent='Qwen выключена. Чтобы начать диалог, запустите Start-Web.cmd.';
  setState(runId?'Агент работает':config.model_available?'Агент готов к работе':'Модель выключена',!!runId);
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
$('about').onclick=()=>{const values=[['Модель',config.model||'—'],['Среда','Локальная Ollama'],['Контекст модели',config.context_length||'—'],['Бюджет входа',config.context_budget||'—'],['Лимит генерации',config.output_limit||'—'],['Данные',config.backend==='mock'?'Учебный граф':'IACPaaS REST'],['MCP-инструменты',config.tools||'—'],['Запись',config.write_enabled?'С подтверждением':'Отключена'],['История','На этом компьютере']];$('about-details').innerHTML=values.map(([key,value])=>`<dt>${key}</dt><dd>${escapeHTML(value)}</dd>`).join('');$('about-dialog').showModal();};
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
