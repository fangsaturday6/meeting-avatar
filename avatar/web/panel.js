// 设置窗口：状态 → 视图。每块界面一个函数；所有操作都走 api.js 里的接口。
import { api, connectEvents, emptyState, esc } from './api.js';
import { listDevices } from './audio.js';
import { fillIcons } from './icons.js';
import { renderMarkdown } from './md.js';

const $ = (id) => document.getElementById(id);
const state = emptyState();
// view：记录页现在显示哪个视图。null = 跟着会议走（开着会看这场会，没开会看以前的会议）
const ui = { view: null, renaming: null, settingsKey: '', engageKey: '', error: '', readerKey: '',
  reader: { id: null, text: '', error: '', copied: false },
  agents: { busy: '', error: '', copied: false } };     // busy：正在接上或取消的那个智能体
const store = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 存不了就算了 */ } },
};
const isActive = () => state.status !== 'off' && state.status !== 'starting';
const liveId = () => (state.status === 'off' ? '' : state.record_id);

// ---------- 标签页和记录页的三个视图 ----------
function currentView() {
  if (ui.view === 'read' && ui.reader.id) return 'read';
  if (ui.view === 'list' && state.meetings.length) return 'list';
  if (ui.view === 'live') return 'live';
  return state.status !== 'off' || state.lines.length || !state.meetings.length ? 'live' : 'list';
}

function renderTabs() {
  const tab = location.hash === '#notes' ? 'notes' : 'settings';
  $('panel').dataset.tab = tab;
  $('panel').dataset.status = state.status;
  $('panel').dataset.view = currentView();
  for (const t of ['notes', 'settings']) $(`tab-${t}`).setAttribute('aria-selected', String(t === tab));
  $('btn-meetings').hidden = !state.meetings.length;
  document.title = `${state.settings.name || '数字人'} · ${tab === 'notes' ? '记录' : '设置'}`;
}

function show(view) { ui.view = view; renderTabs(); }

function renderAlerts() {
  const items = [];
  if (ui.error) items.push(esc(ui.error));
  if (!state.connected) items.push('和后台断开了，正在重连');
  $('alerts').hidden = !items.length;
  $('alerts').innerHTML = items.map((html) => `<li data-level="danger">${html}</li>`).join('');
}

// 几选一：一排按钮，选中的那个 aria-checked="true"。每个选项的 id 是「前缀-值」，如 engagement-quiet
function choices(prefix, options, current) {
  return Object.entries(options || {}).map(([id, label]) =>
    `<button type="button" role="radio" id="${esc(prefix)}-${esc(id)}" data-value="${esc(id)}" aria-checked="${current === id}">${esc(label)}</button>`).join('');
}

// ---------- 这场会：任务 ----------
function renderTasks() {
  $('tasks').innerHTML = [...state.tasks].reverse().map((t) => `
    <li><button class="task" type="button" data-task-id="${t.id}" data-state="${t.status}" title="${esc(t.kind)}"
      aria-pressed="${state.board_task_id === t.id}"><span class="dot"></span><span class="brief">${esc(t.brief)}</span></button></li>`).join('');
}

// ---------- 这场会：对话 ----------
// 同一个人连着说的几句，名字只在第一句出现（一句话常被停顿切成几段）。rename：名字能不能点开改
function lineRows(lines, rename = false) {
  return lines.filter((l) => l.who !== 'note').map((l, i, all) => {
    const prev = all[i - 1];
    const same = prev && prev.who === l.who && prev.speaker === l.speaker;
    const name = same ? '<span class="who"></span>'
      : rename && l.who === 'human' ? `<button class="who" type="button" data-speaker-id="${l.speaker_id}" aria-label="给${esc(l.speaker)}改名">${esc(l.speaker)}</button>`
      : `<span class="who">${esc(l.speaker)}</span>`;
    return `<li class="line ${esc(l.who)}${same ? ' same' : ''}"${l.id === undefined ? '' : ` data-line-id="${l.id}"`}>${name}
      <span class="text">${esc(l.text)}${l.cut ? '<span class="cut">被打断</span>' : ''}</span></li>`;
  }).join('');
}

function renderTranscript() {
  if (ui.renaming !== null) return;   // 正在改名时不要重画，免得输入框被冲掉
  const el = $('transcript');
  const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  el.innerHTML = lineRows(state.lines, true);
  if (stick) el.scrollTop = el.scrollHeight;
}

function startRename(btn) {
  const id = Number(btn.dataset.speakerId);
  const sp = state.speakers.find((s) => s.id === id);
  if (!sp) return;
  ui.renaming = id;
  btn.innerHTML = `<input id="rename-input" maxlength="30" aria-label="说话人名字" value="${esc(sp.name)}">`;
  const input = btn.firstChild;
  input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    const name = input.value.trim();
    ui.renaming = null;
    if (save && name && name !== sp.name) await run(() => api.renameSpeaker(id, name));
    renderTranscript();
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); finish(true); } if (e.key === 'Escape') finish(false); });
  input.addEventListener('click', (e) => e.stopPropagation());
  input.addEventListener('blur', () => finish(true));
}

// ---------- 这场会：打字 ----------
function renderComposer() {
  const sel = $('say-kind'), kinds = state.task_kinds || [];
  if (sel.options.length !== kinds.length + 1) {
    sel.innerHTML = '<option value="">说话</option>' + kinds.map((k) => `<option>${esc(k)}</option>`).join('');
  }
  for (const id of ['say-kind', 'say-text', 'btn-send']) $(id).disabled = !isActive();
  $('say-text').placeholder = isActive() ? (sel.value ? '让它去查什么、做什么' : '打字') : '还没开会';
}

async function submitComposer() {
  const text = $('say-text').value.trim(), kind = $('say-kind').value, err = $('composer-error');
  const fail = (msg) => { err.hidden = !msg; err.textContent = msg; };
  if (!text) return;
  if (kind && text.length < 2) return fail('先写清楚要它做什么');
  $('say-text').value = '';
  const me = (state.settings.owner || '').trim() || '我';        // 打字发言算在「我的名字」头上
  const r = await (kind ? api.createTask(kind, text) : api.utterance(me, text));
  fail(r.ok ? '' : r.message);
  if (!r.ok) $('say-text').value = text;
}

// ---------- 以前的会议 ----------
function meetingRow(m) {
  const start = new Date(m.started_at * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const minutes = Math.max(1, Math.round((m.ended_at - m.started_at) / 60));
  const when = m.started_at ? `${start.getMonth() + 1}月${start.getDate()}日 ${pad(start.getHours())}:${pad(start.getMinutes())}` : m.id;
  const length = m.started_at ? `${minutes} 分钟 · ${m.lines} 句` : '';
  const titled = m.minutes === 'yes' && m.title;
  const state_ = m.id === liveId() ? 'live' : state.minuting.includes(m.id) ? 'minuting' : '';
  return {
    id: m.id,
    href: `/api/meetings/${encodeURIComponent(m.id)}`,
    state: state_,
    name: titled ? m.title : when,          // 有纪要的会用标题认，没有的用时间认
    when, length,
    sub: state_ === 'live' ? '进行中' : state_ === 'minuting' ? '正在整理纪要' : titled ? when : length,
  };
}

function renderMeetings() {
  $('meetings').innerHTML = state.meetings.map(meetingRow).map((r) => `
    <li><a class="meeting" data-meeting-id="${esc(r.id)}" data-state="${r.state}" href="${esc(r.href)}" target="_blank" rel="noopener" aria-label="会议记录 ${esc(r.name)} ${esc(r.sub)}">
      <span class="when">${esc(r.name)}</span><span class="sub">${esc(r.sub)}</span></a></li>`).join('');
  if (ui.reader.id) renderReader();
}

// ---------- 读一场会 ----------
// 把一份会议记录（Markdown 文本）拆成元数据和几节，视图只管画
function parseRecord(text) {
  const [, head = '', body = text] = text.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/) || [];
  const meta = Object.fromEntries(head.split('\n').map((l) => { const i = l.indexOf(':'); return [l.slice(0, i).trim(), l.slice(i + 1).trim()]; }));
  // 各节按固定的小标题切。任务结果和纪要里自己也会有「## 」标题，所以不能见到「## 」就切
  const between = (from, to) => {
    const i = body.indexOf(`\n## ${from}\n`);
    if (i < 0) return '';
    const rest = body.slice(i + from.length + 5);
    const j = to ? rest.indexOf(`\n## ${to}\n`) : -1;
    return (j < 0 ? rest : rest.slice(0, j)).trim();
  };
  const rows = (part) => part.split('\n').map((l) => l.match(/^\[(\d\d:\d\d):\d\d\] ([^：]+)：(.*?)(（被打断）)?$/)).filter(Boolean)
    .map((m) => ({ who: m[2] === meta.bot ? 'bot' : 'human', speaker: m[2], text: m[3], cut: !!m[4] }));
  // 有整理稿（会后书面化的对话）就看整理稿，逐句原话留在文件里对照
  const polished = rows(between('整理稿', '对话'));
  return {
    meta, minutes: between('纪要', polished.length ? '整理稿' : '对话'),
    lines: polished.length ? polished : rows(between('对话', '后台任务')), tasks: between('后台任务', null),
  };
}

function renderReader() {
  const r = ui.reader;
  const row = state.meetings.find((m) => m.id === r.id);
  // 会议进行中每秒有好几个状态事件：内容没变就不重画，否则选不中字、长记录会卡
  const key = JSON.stringify([r, row, state.minuting.includes(r.id), state.minutes_result, liveId()]);
  if (key === ui.readerKey) return;
  ui.readerKey = key;
  const info = row ? meetingRow(row) : null;
  const rec = parseRecord(r.text);
  const busy = info && info.state === 'minuting';
  $('reader-title').textContent = info ? info.name : '';
  $('reader-sub').textContent = info ? [info.name === info.when ? '' : info.when, info.length, (row.speakers || []).join('、')].filter(Boolean).join(' · ') : '';
  $('btn-minutes').hidden = !!(info && info.state === 'live');
  $('btn-minutes').disabled = !!busy;
  $('btn-minutes').textContent = busy ? '正在整理…' : rec.minutes ? '重新整理纪要' : '整理纪要';
  $('btn-copy-path').textContent = r.copied ? '已复制' : '复制路径';
  $('btn-copy-path').disabled = !row;
  const failed = state.minutes_result && state.minutes_result.id === r.id ? state.minutes_result.state : '';
  const note = r.error ? esc(r.error)
    : rec.meta.recovered === 'yes' && !failed ? '这场会是事后从运行日志里恢复的：别人说的话基本完整，它的长回答只留下了开头'
    : failed === 'failed' ? '纪要没整理成，多半是网络不稳，过一会儿再点一次'
    : failed === 'empty' || (row && row.minutes === 'none') ? '这场会没有可以整理的实质内容' : '';
  $('reader-body').innerHTML = `
    ${note ? `<p id="reader-note">${note}</p>` : ''}
    ${rec.minutes ? `<section id="reader-minutes" class="md">${renderMarkdown(rec.minutes)}</section>` : ''}
    <ol id="reader-lines">${lineRows(rec.lines)}</ol>
    ${rec.tasks ? `<section id="reader-tasks" class="md">${renderMarkdown(rec.tasks)}</section>` : ''}`;
}

async function loadReader() {
  const id = ui.reader.id;
  const r = await api.meeting(id);
  if (ui.reader.id !== id) return;       // 等的时候已经换了一场或关掉了
  ui.reader.text = r.ok ? r.data : '';
  ui.reader.error = r.ok ? '' : r.message;
  renderReader();
}

function openReader(id) {
  ui.reader = { id, text: '', error: '', copied: false };
  ui.readerKey = '';
  renderReader();
  show('read');
  loadReader();
}

function closeReader() {
  ui.reader = { id: null, text: '', error: '', copied: false };
  show('list');
}

// ---------- 设置 ----------
function renderSettings() {
  const s = state.settings;
  const key = JSON.stringify([s, state.voices, state.scenes, state.roles, state.projects]);
  if (key === ui.settingsKey) return;      // 没变就不动表单，避免打字时被改写
  ui.settingsKey = key;
  const focused = document.activeElement;
  const set = (id, v) => { const el = $(id); if (el !== focused) el.value = v ?? ''; };
  $('set-voice').innerHTML = Object.entries(state.voices).map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join('');
  $('set-scene').innerHTML = choices('set-scene', state.scenes, s.scene);
  $('set-role').innerHTML = choices('set-role', state.roles, s.role);
  set('set-name', s.name); set('set-owner', s.owner); set('set-voice', s.voice); set('set-speed', s.speed);
  set('set-project', s.project_dir); set('set-projects-root', s.projects_root); set('set-topic', s.topic);
  $('set-skills').checked = !!s.use_my_skills;
  // 替我开会时默认只谈这场会的项目；要不要放开由主人自己定
  $('open-book-row').hidden = s.role !== 'delegate';
  $('set-open-book').checked = !!s.open_book;
  // 没单独填时，它默认去「这场会聊的项目」的上一级找你的其他项目
  const p = state.projects || { root: '', names: [] };
  $('set-projects-root').placeholder = p.root || '/Users/你/Documents';
  $('projects-count').textContent = p.names.length ? `认识 ${p.names.length} 个项目` : p.root ? '这里没找到项目' : '';
  $('projects-count').title = p.names.join('\n');
}

// 它说多少：少说 / 适中 / 多说。会上有人嫌它话多，它自己也会把这个调低
function renderEngagement() {
  const cur = state.settings.engagement;
  const key = JSON.stringify([state.engagements, cur]);
  if (key === ui.engageKey) return;
  ui.engageKey = key;
  $('engagement').innerHTML = choices('engagement', state.engagements, cur);
}

async function saveSetting(name, value) {
  const r = await api.settings({ [name]: value });
  const err = $('settings-error');
  err.hidden = r.ok;
  err.textContent = r.ok ? '' : r.message;
  if (r.ok) state.settings = r.data.settings;
  ui.settingsKey = ''; ui.engageKey = '';      // 没改成时：把选项拨回原来的位置
  renderSettings(); renderEngagement(); renderTabs();
}

// ---------- 接给我的智能体 ----------
// 每个智能体一行。接没接上看开关；没装的不给开关，直接写出来
function renderAgents() {
  const clients = state.agents ? state.agents.clients : [];
  for (const li of document.querySelectorAll('#agents li[data-agent]')) {
    const id = li.dataset.agent, c = clients.find((x) => x.id === id), sw = $(`agent-${id}`);
    li.dataset.state = c ? c.state : '';
    li.querySelector('.agent-state').textContent = c && c.state === 'missing' ? '这台电脑上没装' : '';
    sw.setAttribute('aria-checked', String(!!c && c.state === 'on'));
    sw.disabled = !c || c.state === 'missing' || !!ui.agents.busy;
    sw.setAttribute('aria-busy', String(ui.agents.busy === id));
  }
  const copy = $('btn-copy-agent-guide');
  copy.textContent = ui.agents.copied ? '已复制，粘贴给它' : '复制一段话发给它';
  copy.classList.toggle('done', ui.agents.copied);
  $('agents-error').hidden = !ui.agents.error;
  $('agents-error').textContent = ui.agents.error;
}

async function loadAgents() {
  const r = await api.agents();
  if (r.ok) state.agents = r.data;
  renderAgents();
  return r;
}

async function toggleAgent(id) {
  const sw = $(`agent-${id}`), on = sw.getAttribute('aria-checked') !== 'true';
  ui.agents.busy = id; ui.agents.error = '';
  renderAgents();
  sw.setAttribute('aria-checked', String(on));      // 先动，不等后台（登记要一两秒）
  const r = await api.connectAgent(id, on);
  ui.agents.busy = '';
  ui.agents.error = r.ok ? '' : r.message;
  if (r.ok && state.agents) state.agents.clients = r.data.clients;
  renderAgents();                                   // 没成时：开关拨回原来的位置
}

// 复制到剪贴板。不给剪贴板权限的环境（内嵌的预览面板）里退回老办法
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* 往下试老办法 */ }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

async function copyAgentGuide() {
  const r = state.agents ? { ok: true } : await loadAgents();
  ui.agents.error = r.ok ? '' : r.message;
  if (r.ok) {
    ui.agents.copied = await copyText(state.agents.guide);
    if (!ui.agents.copied) ui.agents.error = '没能复制，点一下这个窗口再试';
  }
  renderAgents();
  if (ui.agents.copied) setTimeout(() => { ui.agents.copied = false; renderAgents(); }, 2400);
}

// 麦克风和扬声器存在本机；主窗口监听到变化后立刻换设备
async function renderDevices() {
  const { mics, speakers } = await listDevices().catch(() => ({ mics: [], speakers: [] }));
  const chosen = store.get('stage-devices', {});
  const fill = (sel, list, id) => {
    sel.innerHTML = '<option value="">系统默认</option>' + list.map((d) => `<option value="${esc(d.deviceId)}">${esc(d.label || '未命名设备')}</option>`).join('');
    sel.value = list.some((d) => d.deviceId === id) ? id : '';
  };
  fill($('mic-select'), mics, chosen.micId);
  fill($('speaker-select'), speakers, chosen.speakerId);
}

// ---------- 调度 ----------
let wasOff = true;
function render(part) {
  if (part === 'meetings' && ui.reader.id && state.minutes_result && state.minutes_result.id === ui.reader.id
      && state.minutes_result.state !== 'running') loadReader();     // 纪要整理完了：重新读这场会
  if (wasOff && state.status !== 'off' && ui.view === 'list') ui.view = null;   // 会议开始：从列表切回这场会
  wasOff = state.status === 'off';
  renderTabs(); renderAlerts(); renderComposer(); renderEngagement();
  if (part === 'all' || part === 'tasks') renderTasks();
  if (part === 'all' || part === 'lines' || part === 'speakers') renderTranscript();
  if (part === 'all' || part === 'status' || part === 'meetings') renderMeetings();
  if (part === 'all' || part === 'settings') renderSettings();
  if (part === 'all' || part === 'agents') renderAgents();
}

// 统一执行一个操作：失败时把原因用人话显示在顶部
async function run(fn) {
  const r = await fn();
  ui.error = r.ok ? '' : r.message;
  renderAlerts();
  return r;
}

// ---------- 事件绑定 ----------
fillIcons();
$('tabs').addEventListener('click', (e) => { const t = e.target.dataset.tab; if (t) location.hash = t; });
window.addEventListener('hashchange', () => { renderTabs(); window.focus(); });

$('tasks').addEventListener('click', (e) => {
  const btn = e.target.closest('.task');
  if (!btn) return;
  const id = Number(btn.dataset.taskId);
  run(() => api.showOnBoard(state.board_task_id === id ? null : id));   // 点任务 = 放到主窗口的资料板上；再点一次收起
});
$('transcript').addEventListener('click', (e) => {
  const btn = e.target.closest('button.who');
  if (btn && ui.renaming === null) startRename(btn);
});
$('composer').addEventListener('submit', (e) => { e.preventDefault(); submitComposer(); });
$('say-kind').addEventListener('change', renderComposer);

// 点一条会议记录：在窗口里读（正在开的那场直接回到这场会）。按住 ⌘ / Ctrl 点是单独打开原文
$('btn-meetings').addEventListener('click', () => show('list'));
$('meetings').addEventListener('click', (e) => {
  const a = e.target.closest('.meeting');
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  if (a.dataset.state === 'live') show('live'); else openReader(a.dataset.meetingId);
});
$('btn-reader-close').addEventListener('click', closeReader);
$('btn-minutes').addEventListener('click', async () => {
  const id = ui.reader.id;
  const r = await api.makeMinutes(id);      // 进行到哪了由事件流告诉我们，这里只管报错
  if (ui.reader.id !== id) return;
  ui.reader.error = r.ok ? '' : r.message;
  renderReader();
});
$('btn-copy-path').addEventListener('click', async () => {
  const row = state.meetings.find((m) => m.id === ui.reader.id);
  if (!row) return;
  try { await navigator.clipboard.writeText(row.file); ui.reader.copied = true; } catch { ui.reader.error = '没能复制，路径是：' + row.file; }
  renderReader();
  setTimeout(() => { ui.reader.copied = false; if (ui.reader.id) renderReader(); }, 1600);
});

// 所有「几选一」共用：点哪个就把它所在那一组对应的设置改成哪个
$('settings').addEventListener('click', (e) => {
  const b = e.target.closest('[role="radio"]');
  if (!b || b.getAttribute('aria-checked') === 'true') return;
  for (const x of b.parentElement.children) x.setAttribute('aria-checked', String(x === b));   // 先动，不等后台
  saveSetting(b.parentElement.dataset.setting, b.dataset.value);
});
$('settings').addEventListener('change', (e) => {
  const t = e.target;
  if (t.id === 'mic-select' || t.id === 'speaker-select') {
    return store.set('stage-devices', { micId: $('mic-select').value, speakerId: $('speaker-select').value });
  }
  if (!t.name) return;
  saveSetting(t.name, t.type === 'checkbox' ? t.checked : t.type === 'range' ? Number(t.value) : t.value.trim());
});
$('agents').addEventListener('click', (e) => {
  const li = e.target.closest('.switch:not(:disabled)')?.closest('li[data-agent]');
  if (li) toggleAgent(li.dataset.agent);
});
$('btn-copy-agent-guide').addEventListener('click', copyAgentGuide);
navigator.mediaDevices?.addEventListener?.('devicechange', renderDevices);

connectEvents(state, render);
render('all');
renderDevices();
loadAgents();
