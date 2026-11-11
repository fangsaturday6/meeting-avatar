// 数字人形象：一张 SVG。神态由外层元素的 data-* 属性决定（见 avatar.css）。

const SVG = `
<svg class="av" viewBox="0 0 400 400" role="img" aria-label="数字人形象">
  <circle class="av-ring" cx="200" cy="210" r="190"/>
  <circle class="av-halo" cx="200" cy="210" r="164"/>
  <g class="av-body">
    <line class="av-antenna" x1="200" y1="100" x2="200" y2="66"/>
    <circle class="av-bulb" cx="200" cy="52" r="13"/>
    <g class="av-orbit"><circle class="av-dot" cx="200" cy="24" r="6"/></g>
    <g class="av-hand">
      <rect class="av-arm" x="316" y="176" width="26" height="92" rx="13" transform="rotate(14 329 268)"/>
      <circle class="av-palm" cx="350" cy="166" r="25"/>
    </g>
    <rect class="av-head" x="88" y="98" width="224" height="218" rx="94"/>
    <ellipse class="av-eye l" cx="160" cy="194" rx="15" ry="21"/>
    <ellipse class="av-eye r" cx="240" cy="194" rx="15" ry="21"/>
    <path class="av-smile" d="M172 250 Q200 272 228 250"/>
    <ellipse class="av-mouth" cx="200" cy="256" rx="30" ry="4"/>
    <g class="av-dots">
      <circle class="av-dot" cx="306" cy="92" r="9"/>
      <circle class="av-dot" cx="334" cy="92" r="9"/>
      <circle class="av-dot" cx="362" cy="92" r="9"/>
    </g>
  </g>
</svg>`;

export function mountAvatar(root) {
  root.innerHTML = SVG;
  const mouth = root.querySelector('.av-mouth');
  const halo = root.querySelector('.av-halo');
  let m = 0, e = 0;
  return {
    // 嘴型跟随输出音量（0~1）。上升快、回落慢，看起来更像在说话
    setMouth(level) {
      m = level > m ? m + (level - m) * 0.6 : m + (level - m) * 0.25;
      mouth.setAttribute('ry', (4 + m * 30).toFixed(1));
      mouth.setAttribute('rx', (30 - m * 9).toFixed(1));
    },
    // 光圈跟随麦克风音量：让人看得出它在听
    setEar(level) {
      e += (level - e) * 0.35;
      halo.style.transform = `scale(${(1 + e * 0.13).toFixed(3)})`;
    },
  };
}
