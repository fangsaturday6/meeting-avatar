// 脚下的刻度环：一圈一小时。亮的一段 = 这场会开到这一小时的第几分钟，走满一圈后底圈变亮、再从头走。
// 从上往下看是一面钟：从离镜头最远的那头（12 点）起顺时针走，所以正右边是一刻钟、最靠前是半小时。
// 亮段的头上有一颗小圆点，标出「现在」走到哪了。不知道会议什么时候开始的（只是摆个神态看效果）整圈都亮、没有圆点。
// 对外只有 createDial(半径) → { mesh, update }。颜色、亮度由舞台（character.js）每帧传进来。
import * as THREE from 'three';

const LAP = 3600;        // 一圈多少秒
const WIDTH = 0.09;      // 环有多宽
const STEPS = 720;       // 一圈分多少小段画：一段 5 秒，看不出一格一格地跳
const ARC = 1.5;         // 亮段比舞台给的亮度再亮多少：它是用来读的，不只是一圈光
const TRACK = 0.16;      // 没走到的那段底圈，亮度是亮段的多少
const LAPPED = 0.42;     // 走满过一圈之后的底圈
const START = -Math.PI / 2;     // 12 点在哪个角度

const flat = () => new THREE.MeshBasicMaterial({ color: 0x888888, transparent: true, opacity: 0, toneMapped: false, depthWrite: false, side: THREE.DoubleSide });

function ring(radius) {
  const mesh = new THREE.Mesh(new THREE.RingGeometry(radius, radius + WIDTH, STEPS, 1, START, Math.PI * 2), flat());
  mesh.rotation.x = Math.PI / 2;      // 平铺在地上；这样转过去，角度增加的方向就是从上往下看的顺时针
  return mesh;
}

// 开了 seconds 秒：{ laps: 走满了几圈, part: 这一圈走了多少（0~1） }
export function dialReading(seconds) {
  const s = Math.max(0, seconds);
  return { laps: Math.floor(s / LAP), part: (s % LAP) / LAP };
}

export function createDial(radius) {
  const track = ring(radius), arc = ring(radius);
  const tip = new THREE.Mesh(new THREE.CircleGeometry(WIDTH * 1.15, 24), flat());
  tip.rotation.x = Math.PI / 2;
  arc.position.y = 0.001; tip.position.y = 0.002;     // 一层叠一层，免得抢着画
  const mesh = new THREE.Group();
  mesh.add(track, arc, tip);
  mesh.position.y = 0.012;
  return {
    mesh,
    // seconds：会议开了多少秒，null = 不知道；tone：状态色；level：亮度（0 = 不显示）；swell：跟着声音胀大多少
    update({ seconds, tone, level, swell }) {
      const { laps, part } = seconds === null ? { laps: 0, part: 1 } : dialReading(seconds);
      arc.geometry.setDrawRange(0, Math.round(part * STEPS) * 6);     // 每小段两个三角形、六个顶点序号
      const angle = START + part * Math.PI * 2, bright = Math.min(1, level * ARC);
      tip.position.x = Math.cos(angle) * (radius + WIDTH / 2); tip.position.z = Math.sin(angle) * (radius + WIDTH / 2);
      for (const m of [track, arc, tip]) m.material.color.copy(tone);
      arc.material.opacity = bright;
      track.material.opacity = bright * (laps ? LAPPED : TRACK);
      tip.material.opacity = seconds === null ? 0 : Math.min(1, bright * 1.4);
      mesh.scale.setScalar(swell);
    },
  };
}
