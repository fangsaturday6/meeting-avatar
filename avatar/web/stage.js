// 形象页：状态 → 四块视图（形象、资料板、字幕、接通按钮）。收音和播放交给 audio.js。
import { api, connectEvents, emptyState, esc } from './api.js';
import { createAudioHost, listDevices } from './audio.js';
import { mountAvatar } from './avatar.js';
import { renderMarkdown } from './md.js';

const $ = (id) => document.getElementById(id);
const state = emptyState();
const ui = { link: 'idle', starting: false, error: '', micError: '', caption: '', heard: null, heardTimer: 0, lastLineId: -1, boardKey: '' };
const prefs = (() => { try { return JSON.parse(localStorage.getItem('stage-devices') || '{}'); } catch { return {}; } })();
const savePrefs = () => { try { localStorage.setItem('stage-devices', JSON.stringify(prefs)); } catch { /* 存不了就算了 */ } };

const avatar = mountAvatar($('avatar'));
const host = createAudioHost({
  onMicLevel: (l) => avatar.setEar(state.status === 'off' ? 0 : l),
  onOutLevel: (l) => avatar.setMouth(l),
  onCaption: (text, done) => { ui.caption = done ? '' : text; renderCaption(); },
  onLink: (link) => { ui.link = link; renderGate(); },
});

// ---------- 视图 ----------
function renderPresence() {
  const stage = $('stage');
  stage.dataset.status = state.status;
  stage.dataset.hearing = state.humans_speaking ? '1' : '0';
  const running = state.tasks.filter((t) => t.status === 'running');
  stage.dataset.working = running.length ? '1' : '0';
  $('bot-name').textContent = state.settings.name || '';
  document.title = state.settings.name ? `${state.settings.name} · AI` : '数字人';
  const p = $('progress');
  p.hidden = !running.length;
  if (running.length) p.innerHTML = `<span>${esc(running[running.length - 1].progress || '正在处理')}</span>`;
}

function renderBoard() {
  const task = state.tasks.find((t) => t.id === state.board_task_id);
  const board = $('board');
  $('stage').dataset.board = task ? 'on' : 'off';
  board.hidden = !task;
  if (!task) { ui.boardKey = ''; return; }
  board.dataset.state = task.status;
  $('board-kind').textContent = task.kind;
  $('board-brief').textContent = task.brief;
  const body = $('board-body');
  if (task.status === 'running') {
    ui.boardKey = `${task.id}:running`;
    body.className = '';
    body.innerHTML = `<div class="running"><span>${esc(task.progress || '正在处理')}</span></div>`;
    return;
  }
  // 结果只画一次：避免每来一条事件就重载设计稿
  const key = `${task.id}:${task.status}:${task.artifact}:${task.result.length}`;
  if (key === ui.boardKey) return;
  ui.boardKey = key;
  if (task.status === 'failed') {
    body.className = '';
    body.innerHTML = `<p class="failed">${esc(task.result || '没做成')}</p>`;
  } else if (task.artifact) {
    body.className = 'is-frame';
    body.innerHTML = `<iframe id="board-frame" title="设计稿" sandbox="allow-scripts" src="${esc(task.artifact)}"></iframe>`;
  } else {
    body.className = 'md';
    body.innerHTML = renderMarkdown(task.result);
  }
}

function renderCaption() {
  const el = $('caption');
  if (ui.caption) {
    el.dataset.kind = 'bot';
    el.textContent = ui.caption;
  } else if (ui.heard) {
    el.dataset.kind = 'heard';
    el.innerHTML = `<span><b>${esc(ui.heard.speaker)}</b>${esc(ui.heard.text)}</span>`;
  } else {
    el.textContent = '';
  }
}

function renderGate() {
  const on = ui.link === 'on' || ui.link === 'off';   // off = 已接通过，正在自动重连
  $('gate').hidden = on;
  $('stage').dataset.gated = on ? '0' : '1';
  $('mic-alert').hidden = !on || !ui.micError;
  $('mic-alert-text').textContent = ui.micError;
  const btn = $('stage-start');
  btn.disabled = ui.starting;
  btn.textContent = ui.starting ? '正在接通…' : ui.link === 'replaced' ? '已在别的窗口接通，改到这里' : '接通麦克风和扬声器';
  const err = $('gate-error');
  err.hidden = !ui.error;
  err.textContent = ui.error;
  $('btn-devices').hidden = !on;
  $('btn-devices').dataset.link = ui.link;
  $('btn-devices').title = ui.link === 'off' ? '和后台断开了，正在重连' : '选择麦克风和扬声器';
}

function noteHeard() {
  const last = state.lines[state.lines.length - 1];
  if (!last || last.id === ui.lastLineId) return;
  ui.lastLineId = last.id;
  if (last.who !== 'human') return;
  ui.heard = last;
  clearTimeout(ui.heardTimer);
  ui.heardTimer = setTimeout(() => { ui.heard = null; renderCaption(); }, 6000);
  renderCaption();
}

function render(part) {
  if (part === 'all') ui.lastLineId = state.lines.length ? state.lines[state.lines.length - 1].id : -1;
  if (part === 'all' || part === 'status' || part === 'tasks' || part === 'settings') renderPresence();
  if (part === 'all' || part === 'tasks') renderBoard();
  if (part === 'lines') noteHeard();
  if (part === 'all') { renderCaption(); renderGate(); }
  if (part === 'settings' && host.running && state.settings.scene !== ui.scene) reconfigure();
}

// ---------- 设备 ----------
async function fillDevices() {
  const { mics, speakers } = await listDevices();
  const fill = (sel, list, chosen) => {
    sel.innerHTML = `<option value="">系统默认</option>` + list.map((d) => `<option value="${esc(d.deviceId)}">${esc(d.label || '未命名设备')}</option>`).join('');
    sel.value = list.some((d) => d.deviceId === chosen) ? chosen : '';
  };
  fill($('mic-select'), mics, prefs.micId);
  fill($('speaker-select'), speakers, prefs.speakerId);
}

function audioOptions() {
  ui.scene = state.settings.scene || 'room';
  return { scene: ui.scene, micId: prefs.micId || '', speakerId: prefs.speakerId || '' };
}

function micProblem(e) {
  if (e && e.name === 'NotAllowedError') return '没有拿到麦克风权限，它现在只能说、听不到。请点地址栏左侧的图标允许麦克风';
  if (e && (e.name === 'NotFoundError' || e.name === 'OverconstrainedError')) return '没有找到这个麦克风，它现在只能说、听不到';
  return '麦克风没接上，它现在只能说、听不到';
}

async function reconfigure() {
  try { await host.reconfigure(audioOptions()); ui.micError = ''; }
  catch (e) { ui.micError = micProblem(e); }
  renderGate();
}

async function start() {
  ui.starting = true; ui.error = ''; ui.micError = ''; renderGate();
  try {
    await host.start(audioOptions());
  } catch (e) {
    if (host.running) ui.micError = micProblem(e);      // 扬声器已接通，只是麦克风不行
    else { ui.link = 'idle'; ui.error = '没能接通声音，请再试一次'; }
  }
  fillDevices().catch(() => {});
  ui.starting = false; renderGate();
}

// ---------- 事件绑定 ----------
$('stage-start').addEventListener('click', start);
$('btn-mic-retry').addEventListener('click', reconfigure);
$('btn-devices').addEventListener('click', async () => {
  const panel = $('devices');
  panel.hidden = !panel.hidden;
  $('btn-devices').setAttribute('aria-expanded', String(!panel.hidden));
  if (!panel.hidden) await fillDevices();
});
$('mic-select').addEventListener('change', (e) => { prefs.micId = e.target.value; savePrefs(); reconfigure(); });
$('speaker-select').addEventListener('change', (e) => { prefs.speakerId = e.target.value; savePrefs(); reconfigure(); });
document.addEventListener('click', (e) => {
  if (!$('devices').hidden && !e.target.closest('#devices, #btn-devices')) {
    $('devices').hidden = true; $('btn-devices').setAttribute('aria-expanded', 'false');
  }
});
navigator.mediaDevices?.addEventListener?.('devicechange', () => { if (!$('devices').hidden) fillDevices(); });

connectEvents(state, render);
render('all');
// ?autostart=1：已经授权过麦克风时免点击接通（给自动化和固定机位用）
if (new URLSearchParams(location.search).get('autostart') === '1') start();
