// 控制台：状态 → 视图。每块界面一个函数；所有操作都走 api.js 里的接口。
import { api, connectEvents, emptyState, esc, STATUS_TEXT } from './api.js';
import { mountAvatar } from './avatar.js';
import { renderMarkdown } from './md.js';

const $ = (id) => document.getElementById(id);
const state = emptyState();
const ui = { busy: false, renaming: null, openTask: null, settingsKey: '', engageKey: '', error: '',
  reader: { id: null, text: '', error: '', copied: false } };

mountAvatar($('mini-avatar'));
mountAvatar($('idle-avatar'));
try { $('say-as').value = localStorage.getItem('say-as') || '我'; } catch { /* 用默认名字 */ }

// ---------- 顶栏 ----------
function renderTopbar() {
  const app = $('app');
  const active = state.status !== 'off';
  app.dataset.status = state.status;
  app.dataset.hearing = state.humans_speaking ? '1' : '0';
  app.dataset.working = state.tasks.some((t) => t.status === 'running') ? '1' : '0';
  $('bot-name').textContent = state.settings.name || '';
  $('status-chip').textContent = state.muted && active ? '已静音' : STATUS_TEXT[state.status] || '';
  for (const id of ['btn-mute', 'btn-hush', 'btn-stop']) $(id).hidden = !active;
  $('btn-mute').setAttribute('aria-pressed', String(state.muted));
  $('btn-mute').textContent = state.muted ? '取消静音' : '静音';
  $('btn-hush').disabled = !['speaking', 'thinking', 'waiting'].includes(state.status);
  $('btn-stop').disabled = ui.busy;
  $('btn-start').disabled = ui.busy || state.status === 'starting';
  $('btn-start').textContent = state.status === 'starting' || ui.busy ? '正在入会…' : '开始会议';
  renderEngagement();
}

// 它说多少：少说 / 适中 / 多说。会上有人嫌它话多，它自己也会把这个调低
function renderEngagement() {
  const cur = state.settings.engagement;
  const key = JSON.stringify([state.engagements, cur]);
  if (key === ui.engageKey) return;
  ui.engageKey = key;
  $('engagement').innerHTML = choices('engagement', state.engagements, cur);
}

// 几选一：一排按钮，选中的那个 aria-checked="true"。每个选项的 id 是「前缀-值」，如 engagement-quiet
function choices(prefix, options, current) {
  return Object.entries(options || {}).map(([id, label]) =>
    `<button type="button" role="radio" id="${esc(prefix)}-${esc(id)}" data-value="${esc(id)}" aria-checked="${current === id}">${esc(label)}</button>`).join('');
}

// ---------- 异常提示 ----------
function renderAlerts() {
  const active = state.status !== 'off' && state.status !== 'starting';
  const items = [];
  if (ui.error) items.push(['danger', esc(ui.error)]);
  if (!state.connected) items.push(['danger', '和后台断开了，正在重连']);
  else {
    if (!state.brain_ok && state.status !== 'off') items.push(['danger', '网络不稳，它的大脑暂时连不上，正在自动重试。这段时间它不会回应']);
    if (active && !state.stage_online) items.push(['danger', '形象页还没接通，它现在听不到也说不出。<a href="/stage?autostart=1" target="avatar-stage">去接通</a>']);
    if (!state.ready.ears) items.push(['info', '语音模型还没装好，现在只能打字交流']);
    else if (!state.ready.speakers) items.push(['info', '声纹模型还没装好，暂时分不清是谁在说话']);
  }
  const el = $('alerts');
  el.hidden = !items.length;
  el.innerHTML = items.map(([level, html]) => `<li data-level="${level}">${html}</li>`).join('');
}

// ---------- 对话区 ----------
function renderTalk() {
  const active = state.status !== 'off' && state.status !== 'starting';
  const kept = state.status === 'off' && state.lines.length > 0;   // 会议结束后，刚才的对话留在原处
  $('idle').hidden = active;
  $('idle').dataset.compact = kept ? '1' : '0';
  for (const id of ['speakers', 'transcript']) $(id).hidden = !active && !kept;
  $('composer').hidden = !active;
  if (active || kept) { renderSpeakers(); renderTranscript(); }
}

function renderSpeakers() {
  if (ui.renaming !== null) return;   // 正在改名时不要重画，免得输入框被冲掉
  $('speakers').innerHTML = state.speakers.map((s) =>
    `<button class="speaker-chip" type="button" data-speaker-id="${s.id}" aria-label="给${esc(s.name)}改名" title="点一下改名">${esc(s.name)}</button>`).join('');
}

function renderTranscript() {
  const el = $('transcript');
  const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  el.innerHTML = lineRows(state.lines);
  if (stick) el.scrollTop = el.scrollHeight;
}

// 同一个人连着说的几句，名字只在第一句出现（一句话常被停顿切成几段）
function lineRows(lines) {
  return lines.map((l, i) => {
    const prev = lines[i - 1];
    const same = prev && prev.who === l.who && prev.speaker === l.speaker;
    return `
    <li class="line ${esc(l.who)}${same ? ' same' : ''}"${l.id === undefined ? '' : ` data-line-id="${l.id}"`}>
      <span class="who">${same ? '' : esc(l.speaker)}</span>
      <span class="text">${esc(l.text)}${l.cut ? '<span class="cut">被打断</span>' : ''}</span>
    </li>`;
  }).join('');
}

function startRename(chip) {
  const id = Number(chip.dataset.speakerId);
  const sp = state.speakers.find((s) => s.id === id);
  if (!sp) return;
  ui.renaming = id;
  chip.innerHTML = `<input id="rename-input" maxlength="30" aria-label="说话人名字" value="${esc(sp.name)}">`;
  const input = chip.firstChild;
  input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    const name = input.value.trim();
    ui.renaming = null;
    if (save && name && name !== sp.name) await run(() => api.renameSpeaker(id, name));
    renderSpeakers();
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); finish(true); } if (e.key === 'Escape') finish(false); });
  input.addEventListener('blur', () => finish(true));
}

// ---------- 后台任务 ----------
function renderTasks() {
  const kinds = state.task_kinds || [];
  const sel = $('task-kind');
  if (sel.options.length !== kinds.length) sel.innerHTML = kinds.map((k) => `<option>${esc(k)}</option>`).join('');
  const active = state.status !== 'off' && state.status !== 'starting';
  for (const id of ['task-kind', 'task-brief', 'btn-task']) $(id).disabled = !active;
  $('tasks').innerHTML = [...state.tasks].reverse().map((t) => {
    const sub = t.status === 'running' ? (t.progress || '正在处理') : firstLine(t.result);
    const open = ui.openTask === t.id && t.status !== 'running';
    const body = !open ? '' : `<div class="task-result md">${t.artifact ? `<a class="open-design" href="${esc(t.artifact)}" target="_blank" rel="noopener">打开设计稿</a>` : ''}${renderMarkdown(t.result)}</div>`;
    return `<li class="task" data-task-id="${t.id}" data-state="${t.status}" data-on-board="${state.board_task_id === t.id ? 1 : 0}">
      <button class="task-head" type="button" aria-expanded="${open}">
        <span class="dot"></span><span class="brief"><small>${esc(t.kind)}</small>${esc(t.brief)}</span>
        <span class="sub">${esc(sub)}</span>
      </button>${body}</li>`;
  }).join('');
}

function firstLine(s) {
  return (s || '').split('\n').map((x) => x.replace(/^[#>*\-\s]+/, '').trim()).find(Boolean) || '';
}

// ---------- 会议记录 ----------
function meetingRow(m, liveId) {
  const start = new Date(m.started_at * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const minutes = Math.max(1, Math.round((m.ended_at - m.started_at) / 60));
  const when = m.started_at ? `${start.getMonth() + 1}月${start.getDate()}日 ${pad(start.getHours())}:${pad(start.getMinutes())}` : m.id;
  const length = m.started_at ? `${minutes} 分钟 · ${m.lines} 句` : '';
  const titled = m.minutes === 'yes' && m.title;
  const state_ = m.id === liveId ? 'live' : state.minuting.includes(m.id) ? 'minuting' : '';
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
  const liveId = state.status === 'off' ? '' : state.record_id;
  const rows = state.meetings.map((m) => meetingRow(m, liveId));
  $('meetings-panel').hidden = !rows.length;
  $('meetings').innerHTML = rows.map((r) => `
    <li><a class="meeting" data-meeting-id="${esc(r.id)}" data-state="${r.state}" href="${esc(r.href)}" target="_blank" rel="noopener" aria-label="会议记录 ${esc(r.name)} ${esc(r.sub)}">
      <span class="when">${esc(r.name)}</span><span class="sub">${esc(r.sub)}</span></a></li>`).join('');
  if (ui.reader.id) renderReader();
}

// ---------- 会议记录阅读器 ----------
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
  const lines = between('对话', '后台任务').split('\n').map((l) => l.match(/^\[(\d\d:\d\d):\d\d\] ([^：]+)：(.*?)(（被打断）)?$/)).filter(Boolean)
    .map((m) => ({ who: m[2] === meta.bot ? 'bot' : 'human', speaker: m[2], text: m[3], cut: !!m[4] }));
  return { meta, minutes: between('纪要', '对话'), lines, tasks: between('后台任务', null) };
}

function renderReader() {
  const r = ui.reader;
  const row = state.meetings.find((m) => m.id === r.id);
  // 会议进行中每秒有好几个状态事件：内容没变就不重画，否则阅读器里选不中字、长记录会卡
  const key = JSON.stringify([r, row, state.minuting.includes(r.id), state.minutes_result, state.status === 'off' || state.record_id]);
  if (key === ui.readerKey) return;
  ui.readerKey = key;
  const info = row ? meetingRow(row, state.status === 'off' ? '' : state.record_id) : null;
  const rec = parseRecord(r.text);
  const busy = info && info.state === 'minuting';
  const live = info && info.state === 'live';
  $('reader-title').textContent = info ? info.name : '';
  $('reader-sub').textContent = info ? [info.name === info.when ? '' : info.when, info.length, (row.speakers || []).join('、')].filter(Boolean).join(' · ') : '';
  $('btn-minutes').hidden = !!live;
  $('btn-minutes').disabled = !!busy;
  $('btn-minutes').textContent = busy ? '正在整理…' : rec.minutes ? '重新整理纪要' : '整理纪要';
  $('btn-copy-path').textContent = r.copied ? '已复制' : '复制路径';
  $('btn-copy-path').disabled = !row;
  const failed = state.minutes_result && state.minutes_result.id === r.id ? state.minutes_result.state : '';
  const note = r.error ? esc(r.error)
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
  if (!$('reader').open) $('reader').showModal();
  loadReader();
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

async function saveSetting(name, value) {
  const r = await api.settings({ [name]: value });
  const err = $('settings-error');
  err.hidden = r.ok;
  err.textContent = r.ok ? '' : r.message;
  if (r.ok) { state.settings = r.data.settings; ui.settingsKey = ''; renderSettings(); renderTopbar(); }
  else { ui.settingsKey = ''; ui.engageKey = ''; renderSettings(); renderTopbar(); }   // 没改成：把选项拨回原来的位置
}

// ---------- 调度 ----------
function render(part) {
  if (part === 'meetings' && ui.reader.id && state.minutes_result && state.minutes_result.id === ui.reader.id
      && state.minutes_result.state !== 'running') loadReader();     // 纪要整理完了：重新读这场会
  renderTopbar(); renderAlerts();
  if (part === 'all' || part === 'status') { renderTalk(); renderTasks(); }
  if (part === 'all' || part === 'status' || part === 'meetings') renderMeetings();
  if (part === 'all' || part === 'lines') renderTranscript();
  if (part === 'all' || part === 'speakers') { renderSpeakers(); renderTranscript(); }
  if (part === 'all' || part === 'tasks') renderTasks();
  if (part === 'all' || part === 'settings') renderSettings();
}

// 统一执行一个操作：失败时把原因用人话显示在顶部
async function run(fn) {
  const r = await fn();
  ui.error = r.ok ? '' : r.message;
  renderAlerts();
  return r;
}

// ---------- 事件绑定 ----------
$('btn-start').addEventListener('click', async () => {
  ui.busy = true; renderTopbar();
  $('settings-panel').open = false;
  await run(api.start);
  ui.busy = false; renderTopbar();
});
$('btn-stop').addEventListener('click', async () => { ui.busy = true; renderTopbar(); await run(api.stop); ui.busy = false; renderTopbar(); });
$('btn-mute').addEventListener('click', () => run(() => api.mute(!state.muted)));
$('btn-hush').addEventListener('click', () => run(api.hush));

$('composer').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = $('say-text').value.trim();
  const who = $('say-as').value.trim() || '我';
  if (!text) return;
  try { localStorage.setItem('say-as', who); } catch { /* 记不住名字也没关系 */ }
  $('say-text').value = '';
  const r = await run(() => api.utterance(who, text));
  if (!r.ok) $('say-text').value = text;
});

$('speakers').addEventListener('click', (e) => {
  const chip = e.target.closest('.speaker-chip');
  if (chip && ui.renaming === null) startRename(chip);
});

$('task-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const brief = $('task-brief').value.trim();
  const err = $('task-error');
  if (brief.length < 2) { err.hidden = false; err.textContent = '先写清楚要它做什么'; return; }
  const r = await api.createTask($('task-kind').value, brief);
  err.hidden = r.ok; err.textContent = r.ok ? '' : r.message;
  if (r.ok) $('task-brief').value = '';
});

$('tasks').addEventListener('click', async (e) => {
  const li = e.target.closest('.task');
  if (!li || !e.target.closest('.task-head')) return;
  const id = Number(li.dataset.taskId);
  const same = ui.openTask === id && state.board_task_id === id;
  ui.openTask = same ? null : id;
  renderTasks();
  await run(() => api.showOnBoard(same ? null : id));   // 点任务 = 放到它的资料板上；再点一次收起
});

// 所有「几选一」共用：点哪个就把它所在那一组对应的设置改成哪个
document.addEventListener('click', (e) => {
  const b = e.target.closest('.segmented [role="radio"]');
  if (!b || b.getAttribute('aria-checked') === 'true') return;
  for (const x of b.parentElement.children) x.setAttribute('aria-checked', String(x === b));   // 先动，不等后台
  saveSetting(b.parentElement.dataset.setting, b.dataset.value);
});

// 点一条会议记录：在页面里读。按住 ⌘ / Ctrl 点仍然是在新标签页打开原文
$('meetings').addEventListener('click', (e) => {
  const a = e.target.closest('.meeting');
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  openReader(a.dataset.meetingId);
});
$('btn-reader-close').addEventListener('click', () => $('reader').close());
$('reader').addEventListener('click', (e) => { if (e.target === $('reader')) $('reader').close(); });   // 点遮罩关闭
$('reader').addEventListener('close', () => { ui.reader = { id: null, text: '', error: '', copied: false }; });
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

$('settings').addEventListener('change', (e) => {
  const t = e.target;
  if (!t.name) return;
  const value = t.type === 'checkbox' ? t.checked : t.type === 'range' ? Number(t.value) : t.value.trim();
  saveSetting(t.name, value);
});

connectEvents(state, render);
render('all');
