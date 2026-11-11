// 形象页的耳朵和嘴：采集麦克风送给后台，接收语音片段按顺序播放。
// 收音和播放都在浏览器里，是为了用浏览器自带的回声消除（当面开会时数字人不会听到自己）。

export function createAudioHost({ onMicLevel, onOutLevel, onCaption, onLink }) {
  let ws = null, wantOpen = false, replaced = false, retry = null;
  let micStream = null, micCtx = null;
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
    if (outEl.setSinkId) await outEl.setSinkId(speakerId || '').catch(() => {});
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
  async function setupMic(scene, micId) {
    stopMic();
    const room = scene !== 'online';   // 线上会议的声音来自虚拟声卡，不能做降噪和回声消除
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: micId ? { exact: micId } : undefined,
        echoCancellation: room, noiseSuppression: room, autoGainControl: room, channelCount: 1,
      },
    });
    try { micCtx = new AudioContext({ sampleRate: 16000 }); } catch { micCtx = new AudioContext(); }
    await micCtx.audioWorklet.addModule('/static/pcm-worklet.js');
    const node = new AudioWorkletNode(micCtx, 'pcm');
    node.port.onmessage = (e) => {
      if (ws && ws.readyState === 1) ws.send(e.data.pcm);
      onMicLevel(Math.min(1, e.data.level * 3));
    };
    const mute = micCtx.createGain();
    mute.gain.value = 0;
    micCtx.createMediaStreamSource(micStream).connect(node).connect(mute).connect(micCtx.destination);
    await micCtx.resume();
  }

  function stopMic() {
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    if (micCtx) micCtx.close().catch(() => {});
    micStream = micCtx = null;
  }

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
      await setupOutput(speakerId);
      wantOpen = true; replaced = false;
      if (!ws || ws.readyState > 1) openSocket();
      await setupMic(scene, micId);
    },
    async reconfigure({ scene, micId, speakerId }) {
      if (!wantOpen) return;
      await setupOutput(speakerId);
      await setupMic(scene, micId);
    },
    get running() { return wantOpen && !replaced; },
  };
}

export async function listDevices() {
  const all = await navigator.mediaDevices.enumerateDevices();
  const pick = (kind) => all.filter((d) => d.kind === kind && d.deviceId && d.deviceId !== 'default' && d.deviceId !== 'communications');
  return { mics: pick('audioinput'), speakers: pick('audiooutput') };
}
