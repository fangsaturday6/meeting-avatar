// 主窗口：状态 → 五块视图（形象、资料板、字幕、提示、控制条）。收音和播放交给 audio.js。
// 网址参数：?autostart=1 开会时免点击接通声音（桌面应用用）；
//          ?pose=thinking&hearing&working&muted 强行指定神态（看效果、截图用）；
//          ?bust 只留头和肩、不显示控制条（生成应用图标用）；
//          ?model=名字 指定用 models/ 里的哪个形象，?model=0 用代码搭的那个（挑形象时对比用）；
//          ?mouth=0.8 嘴张到多大（0~1，看说话时的口型用）。
import { api, connectEvents, emptyState, esc, STATUS_TEXT } from './api.js';
import { createAudioHost } from './audio.js';
import { mountAvatar } from './avatar.js';
import { fillIcons } from './icons.js';
import { renderMarkdown } from './md.js';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const AUTO = params.get('autostart') === '1';
const state = emptyState();
const ui = { link: 'idle', connecting: false, busy: false, error: '', micError: '', caption: '', heard: null, heardTimer: 0, lastLineId: -1, boardKey: '', scene: '', dockTimer: 0, pinDock: true };
const devices = () => { try { return JSON.parse(localStorage.getItem('stage-devices') || '{}'); } catch { return {}; } };

let figure = { setState() {}, setMouth() {}, setEar() {} };
const host = createAudioHost({
  onMicLevel: (l) => figure.setEar(state.status === 'off' ? 0 : l),
  onOutLevel: (l) => figure.setMouth(l),
  onCaption: (text, done) => { ui.caption = done ? '' : text; renderCaption(); },
  onLink: (link) => { ui.link = link; renderDock(); },
});

const isActive = () => state.status !== 'off';
const running = () => state.tasks.filter((t) => t.status === 'running');

// 画面要表现的神态
function look() {
  return {
    status: params.get('pose') || state.status,
    hearing: params.has('hearing') || state.humans_speaking,
    working: params.has('working') || running().length > 0,
    muted: params.has('muted') || state.muted,
  };
}

// ---------- 视图 ----------
function renderScene() {
  const v = look(), stage = $('stage');
  stage.dataset.status = v.status;
  stage.dataset.hearing = v.hearing ? '1' : '0';
  stage.dataset.working = v.working ? '1' : '0';
  stage.dataset.muted = v.muted ? '1' : '0';
  figure.setState(v, params.has('pose'));
  const name = state.settings.name || '数字人';
  document.title = name;
  // 替主人开会时，让会上的人一眼看出它是谁的 AI
  const owner = (state.settings.owner || '').trim();
  $('ai-tag').hidden = !(owner && state.settings.role === 'delegate');
  $('ai-tag').textContent = `${owner}的 AI`;
  $('status-text').textContent = `${name}：${state.muted && isActive() ? '已静音' : STATUS_TEXT[state.status] || ''}`;
}

function renderBoard() {
  const task = state.tasks.find((t) => t.id === state.board_task_id);
  const board = $('board');
  if ((board.hidden ? 'off' : 'on') !== (task ? 'on' : 'off')) window.shell?.setBoard(!!task);   // 桌面应用：窗口跟着变宽
  $('stage').dataset.board = task ? 'on' : 'off';
  board.hidden = !task;
  if (!task) { ui.boardKey = ''; return; }
  board.dataset.state = task.status;
  $('board-kind').textContent = task.kind;
  $('board-brief').textContent = task.brief;
  $('board-open').hidden = !(task.status === 'done' && task.artifact);
  if (task.artifact) $('board-open').href = task.artifact;
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

// 字幕三选一：它在说的话 > 刚听到的话 > 后台任务进度
function renderCaption() {
  const el = $('caption'), work = running();
  if (ui.caption) {
    el.dataset.kind = 'bot';
    el.innerHTML = `<span>${esc(ui.caption)}</span>`;
  } else if (ui.heard) {
    el.dataset.kind = 'heard';
    el.innerHTML = `<span><b>${esc(ui.heard.speaker)}</b>${esc(ui.heard.text)}</span>`;
  } else if (work.length && state.board_task_id === null) {
    el.dataset.kind = 'work';
    el.innerHTML = `<span>${esc(work[work.length - 1].progress || '正在处理')}</span>`;
  } else {
    el.textContent = '';
  }
}

function renderAlerts() {
  const items = [];
  if (ui.error) items.push(['danger', esc(ui.error)]);
  if (!state.connected) items.push(['danger', '和后台断开了，正在重连']);
  else {
    if (!state.brain_ok && state.status !== 'off') items.push(['danger', '网络不稳，它的大脑暂时连不上，正在自动重试。这段时间它不会回应']);
    if (ui.micError && host.running) items.push(['danger', `${esc(ui.micError)}<button id="btn-mic-retry" type="button">重试</button>`]);
    if (!state.ready.ears) items.push(['info', '语音模型还没装好，现在只能打字交流']);
    else if (!state.ready.speakers) items.push(['info', '声纹模型还没装好，暂时分不清是谁在说话']);
  }
  const el = $('alerts');
  el.hidden = !items.length;
  el.innerHTML = items.map(([level, html]) => `<li data-level="${level}">${html}</li>`).join('');
}

function renderDock() {
  const active = isActive();
  const linkedHere = host.running && (ui.link === 'on' || ui.link === 'off');   // off = 接通过，正在自动重连
  const needLink = active && !linkedHere && !state.stage_online;                // 开着会，但没有任何窗口接通声音
  const start = $('btn-start');
  start.hidden = active && !needLink && !ui.connecting;
  start.disabled = ui.busy || ui.connecting || state.status === 'starting';
  start.textContent = !active ? (ui.busy ? '正在入会…' : '开始') : ui.connecting ? '正在接通…' : '接通声音';
  for (const id of ['btn-mute', 'btn-hush', 'btn-stop', 'btn-notes', 'dock-gap']) $(id).hidden = !active;
  $('btn-mute').setAttribute('aria-pressed', String(state.muted));
  $('btn-mute').title = state.muted ? '取消静音' : '静音';
  $('btn-mute').setAttribute('aria-label', $('btn-mute').title);
  $('btn-hush').disabled = !['speaking', 'thinking', 'waiting'].includes(state.status);
  $('btn-stop').disabled = ui.busy;
  ui.pinDock = !active || needLink;      // 没开会、或者开着会却没接通声音：控制条一直留着
  if (ui.pinDock) showDock();
}

// 开会后控制条自动收起，鼠标动一下再出来
function showDock() {
  $('stage').dataset.dock = 'show';
  clearTimeout(ui.dockTimer);
  ui.dockTimer = setTimeout(() => { if (!ui.pinDock && !$('dock').matches(':hover')) $('stage').dataset.dock = 'hide'; }, 2600);
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
  if (part === 'lines') return noteHeard();
  renderScene(); renderAlerts(); renderDock();
  if (part === 'all' || part === 'tasks') { renderBoard(); renderCaption(); }
  if (part === 'all' || part === 'status') syncAudio();
  if (part === 'settings' && host.running && state.settings.scene !== ui.scene) reconfigure();
}

// ---------- 声音 ----------
function audioOptions() {
  const d = devices();
  ui.scene = state.settings.scene || 'room';
  return { scene: ui.scene, micId: d.micId || '', speakerId: d.speakerId || '' };
}

function micProblem(e) {
  if (e && e.name === 'NotAllowedError') return '没有拿到麦克风权限，它现在只能说、听不到';
  if (e && (e.name === 'NotFoundError' || e.name === 'OverconstrainedError')) return '没有找到这个麦克风，它现在只能说、听不到';
  return '麦克风没接上，它现在只能说、听不到';
}

async function connect() {
  if (ui.connecting) return;
  ui.connecting = true; ui.error = ''; ui.micError = ''; renderDock();
  try {
    await host.start(audioOptions());
  } catch (e) {
    if (host.running) ui.micError = micProblem(e);      // 扬声器已接通，只是麦克风不行
    else ui.error = '没能接通声音，请再试一次';
  }
  ui.connecting = false; renderAlerts(); renderDock();
}

async function reconfigure() {
  try { await host.reconfigure(audioOptions()); ui.micError = ''; }
  catch (e) { ui.micError = micProblem(e); }
  renderAlerts();
}

// 会议开着就该有声音，会议结束就松开麦克风
function syncAudio() {
  if (!isActive()) {
    if (host.running && !ui.busy) { host.stop(); ui.micError = ''; renderAlerts(); }
  } else if (AUTO && !host.running && !ui.connecting && !state.stage_online && ui.link !== 'replaced') {
    connect();
  }
}

// 统一执行一个操作：失败时把原因用人话显示在顶部
async function run(fn) {
  const r = await fn();
  ui.error = r.ok ? '' : r.message;
  renderAlerts();
  return r;
}

// ---------- 操作（按钮和桌面应用的菜单共用） ----------
const actions = {
  async start() {
    if (isActive()) return connect();
    ui.busy = true; renderDock();
    const audio = connect();          // 必须在点击的当下开始，浏览器才允许出声
    await run(api.start);
    await audio;
    ui.busy = false; renderDock(); showDock();
  },
  // 「结束」的回报在等接口的那一会儿就到了，当时还在忙、没松开麦克风，所以忙完再同步一次
  async stop() { ui.busy = true; renderDock(); await run(api.stop); ui.busy = false; renderDock(); syncAudio(); },
  mute: () => run(() => api.mute(!state.muted)),
  hush: () => run(api.hush),
  notes: () => openPanel('notes'),
  settings: () => openPanel('settings'),
  closeBoard: () => run(() => api.showOnBoard(null)),
};

function openPanel(tab) {
  window.open(`/panel#${tab}`, 'avatar-panel', 'width=420,height=700');
}

// ---------- 事件绑定 ----------
fillIcons();
$('dock').hidden = params.has('bust');
for (const [id, name] of [['btn-start', 'start'], ['btn-stop', 'stop'], ['btn-mute', 'mute'], ['btn-hush', 'hush'],
  ['btn-notes', 'notes'], ['btn-settings', 'settings'], ['btn-board-close', 'closeBoard']]) {
  $(id).addEventListener('click', () => actions[name]());
}
$('alerts').addEventListener('click', (e) => { if (e.target.id === 'btn-mic-retry') reconfigure(); });
$('stage').addEventListener('pointermove', () => showDock());
window.addEventListener('storage', (e) => { if (e.key === 'stage-devices' && host.running) reconfigure(); });
window.shell?.onAction((name) => actions[name]?.());

// 用哪个形象文件：网址里点名的 → 叫 figure 的 → 目录里的第一个；一个都没有就用代码搭的
async function chooseFigure() {
  const r = await api.figures(), all = r.ok ? r.data.figures : [];
  if (params.has('model')) return all.find((f) => f.name === params.get('model')) || null;
  return all.find((f) => f.name === 'figure') || all[0] || null;
}

// 3D 形象起不来（没有 WebGL）就退回 2D 形象
Promise.all([import('./character.js'), chooseFigure()])
  .then(([{ mountCharacter }, model]) => {
    figure = mountCharacter($('avatar'), { bust: params.has('bust'), model });
    if (params.has('mouth')) figure.setMouth(+params.get('mouth') || 0);
  })
  .catch(() => { figure = { setState() {}, ...mountAvatar($('avatar')) }; })
  .finally(renderScene);

connectEvents(state, render);
render('all');
