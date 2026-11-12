// 接口与状态：两个页面共用。视图只读 state，不直接碰网络。

export async function call(method, path, body) {
  try {
    const r = await fetch(path, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) return { ok: true, data };
    const d = data.detail;
    return { ok: false, message: typeof d === 'string' ? d : '填写的内容不对，请检查后再试' };
  } catch {
    return { ok: false, message: '连不上数字人的后台，请确认它还在运行' };
  }
}

// 取一段纯文本（会议记录是 Markdown 文本，不是 JSON）
export async function callText(path) {
  try {
    const r = await fetch(path);
    if (r.ok) return { ok: true, data: await r.text() };
    const data = await r.json().catch(() => ({}));
    return { ok: false, message: typeof data.detail === 'string' ? data.detail : '没读到' };
  } catch {
    return { ok: false, message: '连不上数字人的后台，请确认它还在运行' };
  }
}

export const api = {
  state: () => call('GET', '/api/state'),
  start: () => call('POST', '/api/meeting/start'),
  stop: () => call('POST', '/api/meeting/stop'),
  utterance: (speaker, text) => call('POST', '/api/utterance', { speaker, text }),
  say: (text) => call('POST', '/api/say', { text }),
  hush: () => call('POST', '/api/hush'),
  mute: (muted) => call('POST', '/api/mute', { muted }),
  renameSpeaker: (id, name) => call('PATCH', `/api/speakers/${id}`, { name }),
  createTask: (kind, brief) => call('POST', '/api/tasks', { kind, brief }),
  showOnBoard: (task_id) => call('POST', '/api/board', { task_id }),
  settings: (patch) => call('PATCH', '/api/settings', patch),
  meetings: () => call('GET', '/api/meetings'),
  meeting: (id) => callText(`/api/meetings/${encodeURIComponent(id)}`),
  makeMinutes: (id) => call('POST', `/api/meetings/${encodeURIComponent(id)}/minutes`),
  figures: () => call('GET', '/api/figures'),
  agents: () => call('GET', '/api/agents'),
  connectAgent: (id, connected) => call('PUT', `/api/agents/${encodeURIComponent(id)}`, { connected }),
};

function upsert(list, item) {
  const i = list.findIndex((x) => x.id === item.id);
  if (i >= 0) list[i] = item; else list.push(item);
}

// 把一条事件应用到状态上（原地修改），返回受影响的部分，供视图决定重画哪一块
export function reduce(state, ev) {
  switch (ev.type) {
    case 'snapshot': Object.assign(state, ev.state); return 'all';
    case 'status': state.status = ev.status; return 'status';
    case 'flag': state[ev.name] = ev.value; return 'status';
    case 'ready': state.ready = ev.ready; return 'status';
    case 'line': upsert(state.lines, ev.line); return 'lines';
    case 'speaker':
      upsert(state.speakers, ev.speaker);
      for (const l of state.lines) if (l.who === 'human' && l.speaker_id === ev.speaker.id) l.speaker = ev.speaker.name;
      return 'speakers';
    case 'task': upsert(state.tasks, ev.task); return 'tasks';
    case 'board': state.board_task_id = ev.task_id; return 'tasks';
    case 'settings': state.settings = ev.settings; return 'settings';
    case 'meetings': state.meetings = ev.meetings; return 'meetings';
    case 'projects': state.projects = ev.projects; return 'settings';
    case 'agents': if (state.agents) state.agents.clients = ev.clients; return 'agents';
    case 'minutes':     // 一场会的纪要开始整理 / 整理完了
      state.minuting = state.minuting.filter((id) => id !== ev.id);
      if (ev.state === 'running') state.minuting.push(ev.id);
      state.minutes_result = { id: ev.id, state: ev.state };
      return 'meetings';
    default: return null;
  }
}

export function emptyState() {
  return {
    status: 'off', muted: false, brain_ok: true, stage_online: false, humans_speaking: false,
    mic_online: true,   // 接通声音的窗口有没有把麦克风的声音送到后台；后台没报之前先当它有
    settings: {}, speakers: [], lines: [], tasks: [], board_task_id: null,
    meetings: [], record_id: '', minuting: [], minutes_result: null, projects: { root: '', names: [] },
    agents: null,      // 会议记录接给了哪些智能体。要去问本机的 Claude Code / Codex，慢，所以不在全量状态里，由设置窗口单独取
    ready: { ears: false, speakers: false, local_voice: false },
    voices: {}, scenes: {}, roles: {}, engagements: {}, task_kinds: [],
    connected: false,
  };
}

// 事件流：断了自动重连，重连后服务端会先发全量快照
export function connectEvents(state, onChange) {
  let ws, timer;
  const open = () => {
    ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/events`);
    ws.onopen = () => { state.connected = true; onChange('status'); };
    ws.onmessage = (e) => { const part = reduce(state, JSON.parse(e.data)); if (part) onChange(part); };
    ws.onclose = () => {
      state.connected = false; onChange('status');
      clearTimeout(timer); timer = setTimeout(open, 1500);
    };
  };
  open();
}

export const STATUS_TEXT = {
  off: '未开会', starting: '正在入会', listening: '在听', thinking: '在想', waiting: '想发言', speaking: '在说',
};

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
