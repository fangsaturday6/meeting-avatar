// 控制台：状态 → 视图。每块界面一个函数；所有操作都走 api.js 里的接口。
import { api, connectEvents, emptyState, esc, STATUS_TEXT } from './api.js';
import { mountAvatar } from './avatar.js';
import { renderMarkdown } from './md.js';

const $ = (id) => document.getElementById(id);
const state = emptyState();
const ui = { busy: false, renaming: null, openTask: null, settingsKey: '', error: '' };

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
}

// ---------- 异常提示 ----------
function renderAlerts() {
  const active = state.status !== 'off' && state.status !== 'starting';
  const items = [];
  if (ui.error) items.push(['danger', esc(ui.error)]);
  if (!state.connected) items.push(['danger', '和后台断开了，正在重连']);
  else {
    if (!state.brain_ok) items.push(['danger', '网络不稳，它的大脑暂时连不上，正在自动重试。这段时间它不会回应']);
    if (active && !state.stage_online) items.push(['danger', '形象页还没接通，它现在听不到也说不出。<a href="/stage" target="avatar-stage">去接通</a>']);
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
  $('idle').hidden = active;
  for (const id of ['speakers', 'transcript', 'composer']) $(id).hidden = !active;
  if (active) { renderSpeakers(); renderTranscript(); }
}

function renderSpeakers() {
  if (ui.renaming !== null) return;   // 正在改名时不要重画，免得输入框被冲掉
  $('speakers').innerHTML = state.speakers.map((s) =>
    `<button class="speaker-chip" type="button" data-speaker-id="${s.id}" title="点一下改名">${esc(s.name)}</button>`).join('');
}

function renderTranscript() {
  const el = $('transcript');
  const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  el.innerHTML = state.lines.map((l) => `
    <li class="line ${l.who}" data-line-id="${l.id}">
      <span class="who">${esc(l.speaker)}</span>
      <span class="text">${esc(l.text)}${l.cut ? '<span class="cut">被打断</span>' : ''}</span>
    </li>`).join('');
  if (stick) el.scrollTop = el.scrollHeight;
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

// ---------- 设置 ----------
function renderSettings() {
  const s = state.settings;
  const key = JSON.stringify([s, state.voices, state.scenes]);
  if (key === ui.settingsKey) return;      // 没变就不动表单，避免打字时被改写
  ui.settingsKey = key;
  const focused = document.activeElement;
  const set = (id, v) => { const el = $(id); if (el !== focused) el.value = v ?? ''; };
  $('set-voice').innerHTML = Object.entries(state.voices).map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join('');
  $('set-scene').innerHTML = Object.entries(state.scenes).map(([id, label]) =>
    `<label><input type="radio" name="scene" id="set-scene-${esc(id)}" value="${esc(id)}" ${s.scene === id ? 'checked' : ''}>${esc(label)}</label>`).join('');
  set('set-name', s.name); set('set-voice', s.voice); set('set-speed', s.speed);
  set('set-project', s.project_dir); set('set-topic', s.topic);
  $('set-skills').checked = !!s.use_my_skills;
}

async function saveSetting(name, value) {
  const r = await api.settings({ [name]: value });
  const err = $('settings-error');
  err.hidden = r.ok;
  err.textContent = r.ok ? '' : r.message;
  if (r.ok) { state.settings = r.data.settings; ui.settingsKey = ''; renderSettings(); renderTopbar(); }
}

// ---------- 调度 ----------
function render(part) {
  renderTopbar(); renderAlerts();
  if (part === 'all' || part === 'status') { renderTalk(); renderTasks(); }
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

$('settings').addEventListener('change', (e) => {
  const t = e.target;
  if (!t.name) return;
  const value = t.type === 'checkbox' ? t.checked : t.type === 'range' ? Number(t.value) : t.value.trim();
  saveSetting(t.name, value);
});

connectEvents(state, render);
render('all');
