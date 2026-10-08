const question = document.querySelector('#question');
const askButton = document.querySelector('#ask');
const result = document.querySelector('#result');
const loading = document.querySelector('#loading');
const errorBox = document.querySelector('#error');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const llmForm = document.querySelector('#llm-form');
const llmMessage = document.querySelector('#llm-config-message');
const ragForm = document.querySelector('#rag-form');
const ragMessage = document.querySelector('#rag-config-message');
const memoryTurns = document.querySelector('#memory-turns');
const sessionId = getSessionId();
loadLLMSettings();
loadRAGSettings();
loadMemory();
llmForm.addEventListener('submit', saveLLMSettings);
ragForm.addEventListener('submit', saveRAGSettings);
document.querySelector('#clear-memory').addEventListener('click', clearMemory);

document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => { question.value = button.dataset.question; submit(); }));
askButton.addEventListener('click', submit);
question.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit(); } });

async function submit() {
  const text = question.value.trim();
  if (text.length < 2) { question.focus(); return; }
  result.classList.add('hidden'); errorBox.classList.add('hidden'); loading.classList.remove('hidden'); askButton.disabled = true;
  try {
    const response = await fetch('/query', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question:text, session_id:sessionId})});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || '查询失败');
    render(payload);
    await loadMemory();
  } catch (error) {
    errorBox.textContent = error.message || '服务连接失败，请确认后端已启动。'; errorBox.classList.remove('hidden');
  } finally { loading.classList.add('hidden'); askButton.disabled = false; }
}

function render(data) {
  document.querySelector('#answer').textContent = data.answer;
  document.querySelector('#range').textContent = `${data.time_range.start} — ${data.time_range.end}`;
  document.querySelector('#trace').textContent = data.trace_id;
  document.querySelector('#timing').textContent = `${data.meta.timings_ms.total} ms${data.meta.rag_status === 'ok' ? ' · RAG' : ''}${data.meta.llm_used ? ' · LLM' : ' · RULES'}`;
  const sources = data.sources || [];
  document.querySelector('#source-count').textContent = `${sources.length} 条资料`;
  document.querySelector('#empty-sources').classList.toggle('hidden', sources.length > 0);
  document.querySelector('#sources').innerHTML = sources.map(s => `<article class="source-card"><div class="source-top"><span class="tag ${esc(s.type)}">${esc(typeLabel(s.type))}</span><span class="source-date">${esc(s.date)}</span></div>${s.url ? `<a class="source-title" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)} ↗</a>` : `<span class="source-title">${esc(s.title)}</span>`}<p class="source-snippet">${esc(s.snippet)}</p><div class="source-foot">${esc(s.source)}${s.data_quality === 'illustrative' ? ' · <span class="demo-label">演示样例</span>' : ''}</div></article>`).join('');
  const trend = data.price_change;
  const section = document.querySelector('#price-section');
  section.classList.toggle('hidden', !trend);
  if (trend) {
    const card = document.querySelector('#price-card');
    document.querySelector('#price-quality').textContent = trend.data_quality === 'illustrative' ? '演示样例数据' : '来源行情';
    if (!trend.available) card.innerHTML = `<div class="empty-note">${esc(trend.message)}</div>`;
    else {
      const cls = trend.absolute_change > 0 ? 'up' : trend.absolute_change < 0 ? 'down' : 'flat';
      const sign = trend.absolute_change > 0 ? '+' : '';
      const vals = trend.points.map(p => Number(p.value));
      const lo = Math.min(...vals), hi = Math.max(...vals), span = hi-lo || 1;
      const chart = vals.map((v,i) => `${(i/(Math.max(1,vals.length-1))*200).toFixed(1)},${(38-(v-lo)/span*32).toFixed(1)}`).join(' ');
      card.innerHTML = `<div class="price-main"><div class="price-title">${esc(trend.commodity)} · ${esc(trend.unit)}</div><div class="price-value">${Number(trend.last_value).toLocaleString()} <small>${esc(trend.unit)}</small></div><div class="price-sub">${esc(trend.first_date)} → ${esc(trend.last_date)} · ${esc(trend.points.length)} 个交易日</div></div><svg class="sparkline" viewBox="0 0 200 44" role="img" aria-label="价格变化折线图"><polyline points="${chart}" fill="none" stroke="#4f8a62" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg><div class="change ${cls}">${sign}${esc(trend.percent_change)}%</div>`;
    }
  }
  result.classList.remove('hidden'); result.scrollIntoView({behavior:'smooth', block:'start'});
}
function typeLabel(type) { return ({news:'新闻',policy:'政策',price:'价格',knowledge:'知识库'})[type] || type; }

fetch('/api/stats').then(r=>r.json()).then(s=>document.querySelector('#record-count').textContent=`${s.total} 条记录`).catch(()=>{});

async function loadLLMSettings() {
  try {
    const response = await fetch('/api/settings/llm');
    if (!response.ok) throw new Error('读取设置失败');
    const settings = await response.json();
    document.querySelector('#llm-api-key').placeholder = settings.key_configured ? '已保存 Key；留空则保持不变' : '粘贴 API Key';
    document.querySelector('#llm-status').textContent = settings.enabled && settings.key_configured ? 'LLM 已启用' : settings.key_configured ? 'Key 已保存' : '规则问答模式';
  } catch (error) { llmMessage.textContent = error.message || '无法读取 LLM 设置'; }
}

async function saveLLMSettings(event) {
  event.preventDefault();
  const button = document.querySelector('#save-llm');
  button.disabled = true; llmMessage.textContent = '正在保存…';
  try {
    const response = await fetch('/api/settings/llm', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({api_key:document.querySelector('#llm-api-key').value})});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || '保存失败');
    document.querySelector('#llm-api-key').value = '';
    await loadLLMSettings();
    llmMessage.textContent = payload.message;
  } catch (error) { llmMessage.textContent = error.message || '保存失败'; }
  finally { button.disabled = false; }
}

async function loadRAGSettings() {
  try {
    const response = await fetch('/api/settings/rag');
    if (!response.ok) throw new Error('读取向量化配置失败');
    const settings = await response.json();
    document.querySelector('#rag-api-key').placeholder = settings.key_configured ? '已保存独立 Key；留空保持不变' : '粘贴向量化服务 API Key';
    document.querySelector('#rag-status').textContent = settings.key_configured ? '已配置独立 Embedding Key' : settings.llm_fallback_available ? '使用模型 Key 兜底' : '尚未配置向量化 Key';
  } catch (error) { ragMessage.textContent = error.message || '无法读取向量化配置'; }
}

async function saveRAGSettings(event) {
  event.preventDefault();
  const button = document.querySelector('#save-rag');
  button.disabled = true; ragMessage.textContent = '正在保存…';
  try {
    const response = await fetch('/api/settings/rag', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({api_key:document.querySelector('#rag-api-key').value})});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || '保存失败');
    document.querySelector('#rag-api-key').value = '';
    await loadRAGSettings();
    ragMessage.textContent = payload.message;
  } catch (error) { ragMessage.textContent = error.message || '保存失败'; }
  finally { button.disabled = false; }
}

function getSessionId() {
  let id = localStorage.getItem('miningIntelSessionId');
  if (!id) {
    id = globalThis.crypto?.randomUUID?.() || `session_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    localStorage.setItem('miningIntelSessionId', id);
  }
  return id;
}

async function loadMemory() {
  try {
    const response = await fetch(`/api/memory?session_id=${encodeURIComponent(sessionId)}`);
    if (!response.ok) throw new Error('无法读取记忆');
    const data = await response.json();
    const turns = data.turns || [];
    document.querySelector('#memory-status').textContent = turns.length ? `最近 ${turns.length} 轮 · 可用于理解追问` : '本浏览器最近对话';
    memoryTurns.innerHTML = turns.length ? turns.map((turn, index) => `<article class="memory-turn"><span class="memory-index">${index + 1}</span><div><strong>${esc(turn.question)}</strong><p>${esc(turn.answer)}</p></div></article>`).join('') : '<p class="memory-empty">还没有保存的对话。</p>';
  } catch (error) {
    document.querySelector('#memory-status').textContent = error.message || '记忆读取失败';
  }
}

async function clearMemory() {
  const button = document.querySelector('#clear-memory');
  button.disabled = true;
  try {
    const response = await fetch(`/api/memory?session_id=${encodeURIComponent(sessionId)}`, {method:'DELETE'});
    if (!response.ok) throw new Error('清除失败');
    await loadMemory();
  } catch (error) {
    document.querySelector('#memory-status').textContent = error.message || '清除失败';
  } finally { button.disabled = false; }
}
