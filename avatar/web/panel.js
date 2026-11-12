// 设置窗口：状态 → 视图。每块界面一个函数；所有操作都走 api.js 里的接口。
import { api, connectEvents, emptyState, esc } from './api.js';
import { listDevices } from './audio.js';
import { fillIcons } from './icons.js';

const $ = (id) => document.getElementById(id);
const state = emptyState();
const ui = { renaming: null, settingsKey: '', error: '' };
const store = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 存不了就算了 */ } },
};
const isActive = () => state.status !== 'off' && state.status !== 'starting';

// ---------- 标签页 ----------
function renderTabs() {
  const tab = location.hash === '#notes' ? 'notes' : 'settings';
  $('panel').dataset.tab = tab;
  $('panel').dataset.status = state.status;
  for (const t of ['notes', 'settings']) $(`tab-${t}`).setAttribute('aria-selected', String(t === tab));
  document.title = `${state.settings.name || '数字人'} · ${tab === 'notes' ? '记录' : '设置'}`;
}

function renderAlerts() {
  const items = [];
  if (ui.error) items.push(esc(ui.error));
  if (!state.connected) items.push('和后台断开了，正在重连');
  $('alerts').hidden = !items.length;
  $('alerts').innerHTML = items.map((html) => `<li data-level="danger">${html}</li>`).join('');
}

// ---------- 记录：任务 ----------
function renderTasks() {
  $('tasks').innerHTML = [...state.tasks].reverse().map((t) => `
    <li><button class="task" type="button" data-task-id="${t.id}" data-state="${t.status}" title="${esc(t.kind)}"
      aria-pressed="${state.board_task_id === t.id}"><span class="dot"></span><span class="brief">${esc(t.brief)}</span></button></li>`).join('');
}

// ---------- 记录：对话 ----------
function renderTranscript() {
  if (ui.renaming !== null) return;   // 正在改名时不要重画，免得输入框被冲掉
  const el = $('transcript');
  const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  let prev = null;
  el.innerHTML = state.lines.filter((l) => l.who !== 'note').map((l) => {
    const key = l.who === 'human' ? `h${l.speaker_id}` : l.who;
    const first = key !== prev;
    prev = key;
    const name = !first ? '<span class="who"></span>'
      : l.who === 'human' ? `<button class="who" type="button" data-speaker-id="${l.speaker_id}">${esc(l.speaker)}</button>`
      : `<span class="who">${esc(l.speaker)}</span>`;
    return `<li class="line ${l.who}${first ? ' first' : ''}" data-line-id="${l.id}">${name}
      <span class="text">${esc(l.text)}${l.cut ? '<span class="cut">被打断</span>' : ''}</span></li>`;
  }).join('');
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

// ---------- 记录：打字 ----------
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
  const r = await (kind ? api.createTask(kind, text) : api.utterance($('say-as').value.trim() || '我', text));
  fail(r.ok ? '' : r.message);
  if (!r.ok) $('say-text').value = text;
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
  if (r.ok) { state.settings = r.data.settings; ui.settingsKey = ''; renderSettings(); renderTabs(); }
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
function render(part) {
  renderTabs(); renderAlerts(); renderComposer();
  if (part === 'all' || part === 'tasks') renderTasks();
  if (part === 'all' || part === 'lines' || part === 'speakers') renderTranscript();
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
fillIcons();
$('say-as').value = store.get('say-as', '我');
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

$('settings').addEventListener('change', (e) => {
  const t = e.target;
  if (t.id === 'say-as') return store.set('say-as', t.value.trim() || '我');
  if (t.id === 'mic-select' || t.id === 'speaker-select') {
    return store.set('stage-devices', { micId: $('mic-select').value, speakerId: $('speaker-select').value });
  }
  if (!t.name) return;
  saveSetting(t.name, t.type === 'checkbox' ? t.checked : t.type === 'range' ? Number(t.value) : t.value.trim());
});
navigator.mediaDevices?.addEventListener?.('devicechange', renderDevices);

connectEvents(state, render);
render('all');
renderDevices();
