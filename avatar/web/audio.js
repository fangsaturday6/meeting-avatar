// 主窗口的耳朵和嘴：采集麦克风送给后台，接收语音片段按顺序播放。
// 收音和播放都在浏览器里，是为了用浏览器自带的回声消除（当面开会时数字人不会听到自己）。

const MIC_STALL = 1500;      // 麦克风正常每 40 毫秒来一包声音；这么久没有新的，就当它断了
const GONE = ['NotFoundError', 'OverconstrainedError'];

// onMic(error)：麦克风自己断过之后重新接的结果，接上了是 null
export function createAudioHost({ onMicLevel, onOutLevel, onCaption, onLink, onMic = () => {} }) {
  let ws = null, wantOpen = false, replaced = false, retry = null;
  let want = { scene: 'room', micId: '', speakerId: '' };     // 上一次要接的场合和设备
  let micStream = null, micCtx = null, micJob = Promise.resolve();
  let micAt = 0, micBusy = 0, micFallback = false, micWatch = 0, lastCheck = 0;
  let outCtx = null, analyser = null, outEl = null;
  let queue = [], current = null, generation = 0, decodeChain = Promise.resolve();
  let raf = 0;

  // ---------- 播放 ----------
  async function setupOutput(speakerId) {
    if (!outCtx) {
      outCtx = new AudioContext();
      analyser = outCtx.createAnalyser();
      analyser.fftSize = 512;
      const dest = outCtx.createMediaStreamDestination();
      analyser.connect(dest);
      outEl = new Audio();
      outEl.srcObject = dest.stream;
    }
    await setSink(speakerId);
    await outCtx.resume();
    await outEl.play();
    cancelAnimationFrame(raf);
    const data = new Uint8Array(analyser.fftSize);
    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
      onOutLevel(current ? Math.min(1, Math.sqrt(sum / data.length) * 4.2) : 0);
      raf = requestAnimationFrame(tick);
    };
    tick();
  }

  // 选的扬声器不在了（耳机断开）就用系统默认的，不然它说话没人听得到
  async function setSink(speakerId) {
    if (!outEl || !outEl.setSinkId) return;
    await outEl.setSinkId(speakerId || '').catch(() => outEl.setSinkId('').catch(() => {}));
  }

  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

  function enqueue(head, bytes) {
    const gen = generation;
    decodeChain = decodeChain.then(async () => {
      if (gen !== generation) return;
      let buffer = null;
      try { buffer = await outCtx.decodeAudioData(bytes); } catch { /* 解不开的片段直接跳过 */ }
      if (gen !== generation) return;
      if (!buffer) { send({ type: 'clip_started', id: head.id }); send({ type: 'clip_ended', id: head.id }); return; }
      queue.push({ head, buffer });
      playNext();
    });
  }

  function playNext() {
    if (current || !queue.length) return;
    const item = queue.shift();
    const src = outCtx.createBufferSource();
    src.buffer = item.buffer;
    src.connect(analyser);
    item.src = src;
    current = item;
    src.onended = () => {
      if (current !== item) return;
      current = null;
      send({ type: 'clip_ended', id: item.head.id });
      if (!queue.length) onCaption('', true);
      playNext();
    };
    src.start();
    send({ type: 'clip_started', id: item.head.id });
    onCaption(item.head.text, false);
  }

  function stopPlayback() {
    generation++;
    queue = [];
    if (current) {
      const c = current;
      current = null;
      c.src.onended = null;
      try { c.src.stop(); } catch { /* 已经停了 */ }
    }
    onCaption('', true);
  }

  // ---------- 采集 ----------
  const live = () => wantOpen && !replaced;

  function getMic(micId, room) {
    return navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: micId ? { exact: micId } : undefined,
        echoCancellation: room, noiseSuppression: room, autoGainControl: room, channelCount: 1,
      },
    });
  }

  async function openMic({ scene, micId }) {
    stopMic();
    const room = scene !== 'online';   // 线上会议的声音来自虚拟声卡，不能做降噪和回声消除
    let stream;
    try {
      stream = await getMic(micId, room);
      micFallback = false;
    } catch (e) {
      // 选定的麦克风不在了（多半是耳机断开）：当面开会先用系统默认的顶上，它回来了再换回去。
      // 线上会议不顶：换成真麦克风，它听到的就成了你的房间和它自己的声音
      if (!micId || !room || !GONE.includes(e.name)) throw e;
      stream = await getMic('', room);
      micFallback = true;
    }
    let ctx;
    try { ctx = new AudioContext({ sampleRate: 16000 }); } catch { ctx = new AudioContext(); }
    await ctx.audioWorklet.addModule('/static/pcm-worklet.js').catch(() => {});
    if (!live()) {                     // 等的这一会儿会议已经结束，或者被别的窗口顶替了
      stream.getTracks().forEach((t) => t.stop());
      ctx.close().catch(() => {});
      return;
    }
    micStream = stream; micCtx = ctx;
    const node = new AudioWorkletNode(ctx, 'pcm');
    node.port.onmessage = (e) => {
      micAt = performance.now();
      if (ws && ws.readyState === 1) ws.send(e.data.pcm);
      onMicLevel(Math.min(1, e.data.level * 3));
    };
    const mute = ctx.createGain();
    mute.gain.value = 0;
    ctx.createMediaStreamSource(stream).connect(node).connect(mute).connect(ctx.destination);
    stream.getAudioTracks()[0].addEventListener('ended', healMic);     // 设备被拔掉
    await ctx.resume();
  }

  function stopMic() {
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    if (micCtx) micCtx.close().catch(() => {});
    micStream = micCtx = null;
  }

  // 按 want 接麦克风。开和关排着队做：自动重接、换设备、点「重试」可能同时来
  async function setupMic() {
    micBusy++;
    clearInterval(micWatch);
    lastCheck = performance.now();
    micWatch = setInterval(checkMic, 500);
    const job = micJob.catch(() => {}).then(() => openMic(want));
    micJob = job;
    try {
      await job;
    } catch (e) {
      if (e.name === 'NotAllowedError') clearInterval(micWatch);     // 没给权限：再试也一样，等人点「重试」
      throw e;
    } finally {
      micBusy--;
      micAt = performance.now();       // 刚接上（或刚试过），给它一点时间
    }
  }

  // 麦克风断了（耳机没电、被拔掉、被系统收走），页面收不到任何报错，只是不再有声音数据，所以盯的是数据本身。
  // 断了就重新接；接不上过一会儿再试，设备一有变化也马上试
  async function healMic() {
    if (micBusy || !live()) return;
    onMicLevel(0);
    try { await setupMic(); onMic(null); } catch (e) { onMic(e); }
  }

  function checkMic() {
    const now = performance.now(), late = now - lastCheck > 1500;
    lastCheck = now;
    if (late) micAt = now;             // 页面自己卡了一下（或者电脑刚睡醒），不算麦克风的事
    else if (now - micAt > MIC_STALL) healMic();
  }

  navigator.mediaDevices?.addEventListener('devicechange', () => {
    if (!live()) return;
    setSink(want.speakerId);           // 选的扬声器回来了就换回去
    const track = micStream && micStream.getAudioTracks()[0];
    if (micFallback || !track || track.readyState === 'ended') healMic();
  });

  // ---------- 与后台的音频通道 ----------
  function openSocket() {
    if (!wantOpen || replaced) return;
    ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/audio`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => onLink('on');
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {
        const m = JSON.parse(e.data);
        if (m.type === 'stop') stopPlayback();
        if (m.type === 'replaced') { replaced = true; stopPlayback(); stopMic(); onLink('replaced'); }
        return;
      }
      const n = new DataView(e.data).getUint32(0);
      const head = JSON.parse(new TextDecoder().decode(new Uint8Array(e.data, 4, n)));
      enqueue(head, e.data.slice(4 + n));
    };
    ws.onclose = () => {
      stopPlayback();
      if (replaced) return;
      onLink('off');
      clearTimeout(retry);
      retry = setTimeout(openSocket, 1500);
    };
  }

  return {
    // 先接通扬声器和后台，再接麦克风：麦克风拿不到时数字人仍然能说话（抛出的错误由页面提示）
    async start({ scene, micId, speakerId }) {
      want = { scene, micId, speakerId };
      // 没有点击就启动时（?autostart=1），浏览器可能不让先出声；麦克风开始采集后就允许了，所以失败时放到后面重试
      let outputError = null;
      try { await setupOutput(speakerId); } catch (e) { outputError = e; }
      wantOpen = true; replaced = false;
      if (!ws || ws.readyState > 1) openSocket();
      await setupMic();
      if (outputError) await setupOutput(speakerId);
    },
    async reconfigure({ scene, micId, speakerId }) {
      if (!wantOpen) return;
      want = { scene, micId, speakerId };
      await setupOutput(speakerId);
      await setupMic();
    },
    // 会议结束后松开麦克风和音频通道，系统的麦克风指示灯随之熄灭
    stop() {
      wantOpen = false;
      clearTimeout(retry);
      clearInterval(micWatch);
      stopPlayback();
      stopMic();
      onMicLevel(0);
      if (ws) { ws.onclose = null; ws.close(); ws = null; }
      onLink('idle');
    },
    get running() { return wantOpen && !replaced; },
  };
}

export async function listDevices() {
  const all = await navigator.mediaDevices.enumerateDevices();
  const pick = (kind) => all.filter((d) => d.kind === kind && d.deviceId && d.deviceId !== 'default' && d.deviceId !== 'communications');
  return { mics: pick('audioinput'), speakers: pick('audiooutput') };
}
