// 麦克风采集：把任意采样率的输入变成 16k 单声道 Int16，每 40 毫秒发一包。
class Pcm extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;
    this.out = new Int16Array(640);
    this.n = 0;
    this.peak = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const span = Math.max(1, Math.round(this.ratio));
    while (this.pos < ch.length) {
      const i = Math.floor(this.pos);
      let sum = 0, cnt = 0;
      for (let k = i; k < i + span && k < ch.length; k++) { sum += ch[k]; cnt++; }
      const v = Math.max(-1, Math.min(1, cnt ? sum / cnt : 0));
      this.out[this.n++] = v * 32767;
      const a = Math.abs(v);
      if (a > this.peak) this.peak = a;
      if (this.n === this.out.length) {
        const buf = this.out.slice().buffer;
        this.port.postMessage({ pcm: buf, level: this.peak }, [buf]);
        this.n = 0; this.peak = 0;
      }
      this.pos += this.ratio;
    }
    this.pos -= ch.length;
    return true;
  }
}
registerProcessor('pcm', Pcm);
