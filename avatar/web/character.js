// 3D 形象：一个原创的大块头（光头、大胡子、皮围裙、耳麦）。全部用代码建模，不依赖外部模型文件。
// 对外只有 mountCharacter(root)，返回 { setState, setMouth, setEar }，和 2D 形象（avatar.js）接口一致。
// 状态色不写在这里：从 root 上的 CSS 变量 --tone 读，颜色只在 common.css 里定义一次。
import * as THREE from 'three';
import { RoomEnvironment } from './vendor/three/RoomEnvironment.js';

const SKIN = 0xc8834f, HAIR = 0x2b1a12, APRON = 0x3b4654, LEATHER = 0x6a442c, LEATHER_DARK = 0x2a1e18;
const CLOTH = 0x54463a, STEEL = 0x9aa3ad, BRASS = 0xd0a548, RUBBER = 0x1d2026;

// ---------- 建模小工具 ----------
const std = (color, o = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.62, metalness: 0, vertexColors: true, ...o });

// 手绘感：朝下的面压暗、朝上的面提亮，直接写进顶点色
function paint(g, lo = 0.52, hi = 1.08) {
  const n = g.attributes.normal, c = new Float32Array(n.count * 3);
  for (let i = 0; i < n.count; i++) {
    const t = THREE.MathUtils.smoothstep(n.getY(i), -1, 0.7);
    c[i * 3] = c[i * 3 + 1] = c[i * 3 + 2] = lo + (hi - lo) * t;
  }
  g.setAttribute('color', new THREE.BufferAttribute(c, 3));
  return g;
}

// 球面接缝处的顶点位置相同、法线不同，会露出一条线：把同位置的法线取平均
function weld(g) {
  const p = g.attributes.position, n = g.attributes.normal, acc = new Map();
  const key = (i) => `${p.getX(i).toFixed(4)},${p.getY(i).toFixed(4)},${p.getZ(i).toFixed(4)}`;
  for (let i = 0; i < p.count; i++) {
    const k = key(i), a = acc.get(k) || [0, 0, 0];
    a[0] += n.getX(i); a[1] += n.getY(i); a[2] += n.getZ(i); acc.set(k, a);
  }
  for (let i = 0; i < p.count; i++) {
    const a = acc.get(key(i)), l = Math.hypot(a[0], a[1], a[2]) || 1;
    n.setXYZ(i, a[0] / l, a[1] / l, a[2] / l);
  }
  return g;
}

// 椭球，可选变形函数 deform(顶点, 单位球上的原始位置)
function blob(r, material, { seg = 44, deform, range } = {}) {
  const g = range
    ? new THREE.SphereGeometry(1, seg, Math.round(seg * 0.7), range[0], range[1], range[2], range[3])
    : new THREE.SphereGeometry(1, seg, Math.round(seg * 0.7));
  const p = g.attributes.position, v = new THREE.Vector3(), u = new THREE.Vector3();
  for (let i = 0; i < p.count; i++) {
    u.fromBufferAttribute(p, i);
    v.set(u.x * r[0], u.y * r[1], u.z * r[2]);
    if (deform) deform(v, u);
    p.setXYZ(i, v.x, v.y, v.z);
  }
  g.computeVertexNormals();
  if (!range) weld(g);
  const m = new THREE.Mesh(paint(g), material);
  m.castShadow = true; m.receiveShadow = true;
  return m;
}

// 旋转体：profile 是 [半径, 高度] 的折线，从上到下
function lathe(profile, material, seg = 32) {
  const pts = new THREE.SplineCurve(profile.map(([r, y]) => new THREE.Vector2(r, y))).getPoints(28);
  const g = new THREE.LatheGeometry(pts, seg);
  g.computeVertexNormals();
  const m = new THREE.Mesh(paint(weld(g)), material);
  m.castShadow = true; m.receiveShadow = true;
  return m;
}

// 现成几何体 → 上了手绘明暗的网格
function mesh(geometry, material, shadow = true) {
  const m = new THREE.Mesh(paint(geometry), material);
  m.castShadow = shadow;
  return m;
}

function capsule(r, len, material) {
  const m = new THREE.Mesh(paint(new THREE.CapsuleGeometry(r, len, 8, 20)), material);
  m.castShadow = true;
  return m;
}

function at(obj, x, y, z, rx = 0, ry = 0, rz = 0) {
  obj.position.set(x, y, z); obj.rotation.set(rx, ry, rz);
  return obj;
}

function group(parent, x = 0, y = 0, z = 0) {
  const g = new THREE.Group();
  g.position.set(x, y, z); parent.add(g);
  return g;
}

// ---------- 搭人 ----------
function build(scene, glow) {
  const skin = std(SKIN, { roughness: 0.52 });
  const blush = std(0xc9704e, { roughness: 0.6 });
  const hair = std(HAIR, { roughness: 0.85 });
  const apron = std(APRON, { roughness: 0.66, side: THREE.DoubleSide });
  const leather = std(LEATHER, { roughness: 0.7 });
  const dark = std(LEATHER_DARK, { roughness: 0.6 });
  const cloth = std(CLOTH, { roughness: 0.9 });
  const steel = std(STEEL, { roughness: 0.3, metalness: 0.85, side: THREE.DoubleSide });
  const brass = std(BRASS, { roughness: 0.3, metalness: 0.9 });
  const rubber = std(RUBBER, { roughness: 0.42 });
  const white = std(0xf7f2ea, { roughness: 0.22 });
  const iris = std(0x6a3d1f, { roughness: 0.25 });
  const black = std(0x0c0a09, { roughness: 0.2 });
  const cavity = std(0x3a0f12, { roughness: 0.8 });
  const rim = [skin, blush, hair, apron, leather, dark, cloth, rubber];   // 这些材质吃轮廓光

  const J = {};                                   // 关节和会动的零件
  const body = group(scene);                      // 整个人：呼吸、前倾
  J.body = body;

  // 腿和靴子
  for (const sx of [-1, 1]) {
    body.add(at(lathe([[0.37, 0.98], [0.35, 0.62], [0.31, 0.3]], cloth), sx * 0.43, 0, 0));
    body.add(at(blob([0.37, 0.25, 0.54], dark), sx * 0.45, 0.23, 0.12));
    body.add(at(blob([0.3, 0.16, 0.2], steel, { seg: 24 }), sx * 0.45, 0.2, 0.5));
    body.add(at(lathe([[0.335, 0.46], [0.35, 0.38], [0.335, 0.3]], leather, 28), sx * 0.43, 0, 0));
  }

  // 躯干：下宽上窄的大肚子，上面压一块厚肩
  const torso = group(body, 0, 1.52, 0);
  J.torso = torso;
  const BELLY = [1.06, 0.98, 0.92], CHEST = [1.08, 0.6, 0.76];
  const pear = (k) => (v, u) => { const w = 1 + 0.2 * Math.max(0, -u.y) - 0.1 * Math.max(0, u.y); v.x *= w * k; v.z *= w * k; };
  const front = (half) => [Math.PI / 2 - half, half * 2];
  torso.add(blob(BELLY, skin, { deform: pear(1) }));
  torso.add(at(blob(CHEST, skin), 0, 0.56, -0.04));
  torso.add(at(blob([0.44, 0.34, 0.44], skin, { seg: 28 }), 0, 0.95, 0));                 // 脖子
  torso.add(blob(BELLY, cloth, { deform: pear(1.012), range: [0, Math.PI * 2, Math.PI * 0.66, Math.PI * 0.34] }));   // 裤腰
  // 皮围裙：肚子前面一片、胸口一块护胸、一个口袋、一圈挂脖带
  torso.add(blob(BELLY, apron, { deform: pear(1.04), range: [...front(1.0), Math.PI * 0.3, Math.PI * 0.58] }));
  torso.add(at(blob(CHEST, apron, { range: [...front(0.52), Math.PI * 0.2, Math.PI * 0.62] }), 0, 0.565, -0.01)).scale.setScalar(1.03);
  torso.add(blob(BELLY, leather, { deform: pear(1.07), range: [...front(0.3), Math.PI * 0.58, Math.PI * 0.13], seg: 60 }));
  for (const sx of [-1, 1]) torso.add(at(mesh(new THREE.SphereGeometry(0.03, 10, 8), brass), sx * 0.3, -0.25, 1.065));
  const loop = mesh(new THREE.TorusGeometry(0.47, 0.035, 8, 40), apron);
  torso.add(at(loop, 0, 0.98, 0.1, 1.05));
  // 腰带和带扣（扣上的宝石跟着状态变色）
  const belt = mesh(new THREE.TorusGeometry(1, 0.09, 14, 64), dark);
  torso.add(at(belt, 0, -0.1, 0, Math.PI / 2)); belt.scale.set(1.135, 0.99, 1);
  const buckle = group(torso, 0, -0.1, 1.06);
  buckle.add(at(mesh(new THREE.CylinderGeometry(0.19, 0.19, 0.07, 28), brass), 0, 0, 0, Math.PI / 2));
  const gem = new THREE.Mesh(new THREE.SphereGeometry(0.11, 24, 16), glow);
  buckle.add(at(gem, 0, 0, 0.03)); gem.scale.z = 0.6;

  // 头：绕脖子转
  const head = group(body, 0, 2.54, 0.04);
  J.head = head;
  const jawly = (v, u) => { const w = 1 + 0.18 * Math.max(0, -u.y); v.x *= w; if (u.z > 0) v.z *= 1 + 0.06 * Math.max(0, -u.y); };
  head.add(at(blob([0.63, 0.65, 0.62], skin, { seg: 52, deform: jawly }), 0, 0.42, 0));
  for (const sx of [-1, 1]) {
    head.add(at(blob([0.1, 0.17, 0.13], skin, { seg: 20 }), sx * 0.67, 0.4, -0.02, 0, 0, sx * -0.18));      // 耳朵
    head.add(at(blob([0.17, 0.12, 0.06], blush, { seg: 20 }), sx * 0.4, 0.3, 0.47, 0, sx * 0.6, 0));        // 脸颊
  }
  head.add(at(blob([0.18, 0.16, 0.18], skin, { seg: 28 }), 0, 0.36, 0.62));                                 // 鼻子
  for (const sx of [-1, 1]) head.add(at(blob([0.1, 0.075, 0.1], skin, { seg: 16 }), sx * 0.12, 0.3, 0.6));
  head.add(at(blob([0.48, 0.1, 0.16], skin, { seg: 28 }), 0, 0.76, 0.43, -0.25));                           // 眉骨

  // 眼睛：眼白、虹膜、瞳孔、高光；上眼皮会动，下眼皮固定
  J.eyes = []; J.lids = []; J.brows = [];
  const lidGeo = () => new THREE.SphereGeometry(0.15, 28, 14, 0, Math.PI * 2, 0, Math.PI / 2);
  for (const sx of [-1, 1]) {
    const eye = group(head, sx * 0.25, 0.57, 0.45);
    eye.add(mesh(new THREE.SphereGeometry(0.13, 32, 22), white, false));
    const pupil = group(eye, 0, 0, 0.115);
    pupil.add(mesh(new THREE.SphereGeometry(0.072, 24, 16), iris, false)).scale.z = 0.3;
    pupil.add(at(mesh(new THREE.SphereGeometry(0.04, 18, 12), black, false), 0, 0, 0.012)).scale.z = 0.3;
    const glint = new THREE.Mesh(new THREE.SphereGeometry(0.016, 10, 8), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    pupil.add(at(glint, 0.026, 0.03, 0.016));
    const lid = mesh(lidGeo(), skin, false);
    eye.add(lid);
    eye.add(at(mesh(lidGeo(), skin, false), 0, 0, 0, -2.42));
    const brow = capsule(0.055, 0.22, hair);
    head.add(at(brow, sx * 0.26, 0.8, 0.55, 0, 0, Math.PI / 2 - sx * 0.1));
    brow.scale.z = 0.7;
    J.eyes.push({ pupil, glint }); J.lids.push(lid); J.brows.push({ mesh: brow, sx });
  }

  // 嘴：胡子后面一个暗色的口腔，下巴（大胡子）跟着声音往下掉
  J.mouth = at(blob([0.17, 0.05, 0.06], cavity, { seg: 20 }), 0, 0.12, 0.57);
  head.add(J.mouth);
  for (const sx of [-1, 1]) {
    const m = blob([0.28, 0.105, 0.13], hair, { seg: 24, deform: (v, u) => { v.y -= 0.22 * u.x * u.x; } });
    head.add(at(m, sx * 0.2, 0.2, 0.6, 0, sx * 0.3, sx * -0.4));
  }
  J.jaw = group(head, 0, 0.1, 0.1);
  J.jaw.add(at(blob([0.5, 0.38, 0.4], hair, { seg: 36, deform: (v, u) => { if (u.y < 0) v.x *= 1 - 0.4 * u.y * u.y; } }), 0, -0.22, 0.2));
  J.jaw.add(at(blob([0.15, 0.045, 0.06], std(0xb0624c), { seg: 16 }), 0, -0.02, 0.5));                  // 下嘴唇

  // 耳麦：头带、两个耳罩（灯圈跟着状态变色）、话筒杆（静音时翻上去）
  const band = mesh(new THREE.TorusGeometry(0.71, 0.05, 12, 48, Math.PI), rubber);
  head.add(at(band, 0, 0.44, -0.02)); band.scale.set(1, 1.03, 1.6);
  J.cups = [];
  for (const sx of [-1, 1]) {
    const cup = group(head, sx * 0.74, 0.42, -0.02);
    cup.add(at(mesh(new THREE.CylinderGeometry(0.2, 0.23, 0.14, 28), rubber), 0, 0, 0, 0, 0, sx * -Math.PI / 2));
    cup.add(at(new THREE.Mesh(new THREE.TorusGeometry(0.135, 0.028, 10, 32), glow), sx * 0.07, 0, 0, 0, Math.PI / 2));
    cup.add(at(mesh(new THREE.CylinderGeometry(0.09, 0.09, 0.02, 20), steel), sx * 0.075, 0, 0, 0, 0, Math.PI / 2));
    J.cups.push(cup);
  }
  J.boom = group(head, -0.75, 0.36, 0.02);
  const arc = new THREE.QuadraticBezierCurve3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(-0.1, -0.3, 0.52), new THREE.Vector3(0.36, -0.3, 0.68));
  J.boom.add(mesh(new THREE.TubeGeometry(arc, 20, 0.022, 8), rubber));
  J.boom.add(at(blob([0.07, 0.06, 0.07], rubber, { seg: 14 }), 0.38, -0.3, 0.69));
  // 胳膊：肩 → 上臂 → 肘 → 粗小臂（带护腕）→ 手
  J.arms = [];
  for (const sx of [-1, 1]) {
    const shoulder = group(body, sx * 1.04, 2.22, -0.02);
    shoulder.add(blob([0.37, 0.37, 0.37], skin, { seg: 28 }));
    shoulder.add(lathe([[0.31, 0], [0.32, -0.3], [0.25, -0.66]], skin));
    const elbow = group(shoulder, 0, -0.68, 0);
    elbow.add(blob([0.26, 0.26, 0.26], skin, { seg: 24 }));
    elbow.add(lathe([[0.24, 0], [0.33, -0.3], [0.23, -0.64]], skin));
    elbow.add(lathe([[0.315, -0.3], [0.31, -0.44], [0.245, -0.64]], leather, 28));
    for (const [y, r] of [[-0.31, 0.318], [-0.63, 0.25]]) elbow.add(at(mesh(new THREE.TorusGeometry(r, 0.018, 8, 32), brass), 0, y, 0, Math.PI / 2));
    const hand = group(elbow, 0, -0.7, 0);
    hand.add(at(blob([0.23, 0.22, 0.16], skin, { seg: 24 }), 0, -0.14, 0));
    const fingers = [];
    for (let i = 0; i < 4; i++) {
      const f = group(hand, (i - 1.5) * 0.105, -0.3, 0.01);
      f.add(at(capsule(0.058, 0.15, skin), 0, -0.1, 0));
      fingers.push(f);
    }
    const thumb = group(hand, sx * -0.2, -0.12, 0.06);
    thumb.add(at(capsule(0.062, 0.12, skin), 0, -0.08, 0));
    thumb.rotation.set(-0.3, 0, sx * -0.7);
    J.arms.push({ sx, shoulder, elbow, hand, fingers });
  }
  // 画面右侧的肩上扣一块钢肩甲
  const pauldron = group(J.arms[1].shoulder, 0.03, 0.03, 0);
  pauldron.rotation.z = -0.72;
  pauldron.add(mesh(new THREE.SphereGeometry(0.45, 36, 18, 0, Math.PI * 2, 0, Math.PI * 0.5), steel));
  pauldron.add(at(mesh(new THREE.TorusGeometry(0.45, 0.032, 10, 40), brass), 0, 0, 0, Math.PI / 2));
  for (let i = 0; i < 6; i++) {
    const a = (i / 6) * Math.PI * 2 + 0.3;
    pauldron.add(at(mesh(new THREE.SphereGeometry(0.034, 10, 8), brass), Math.cos(a) * 0.36, 0.28, Math.sin(a) * 0.36));
  }

  // 后台在干活：两颗小光球绕着头转
  J.orbit = group(body, 0, 3.1, 0);
  for (const a of [0, Math.PI]) {
    J.orbit.add(at(new THREE.Mesh(new THREE.SphereGeometry(0.075, 14, 10), glow), Math.cos(a) * 0.98, 0, Math.sin(a) * 0.98));
  }
  return { J, rim };
}

// ---------- 神态：每种状态一组目标值，画面每帧向目标靠拢 ----------
const REST = { headX: 0, headY: 0, headZ: 0, lid: 0, brow: 0, eyeX: 0, eyeY: 0, lean: 0, rX: 0.06, rZ: 0.2, rE: -0.3, lX: 0.06, lZ: 0.2, lE: -0.3, open: 0, glow: 1 };
const POSES = {
  off: { headX: 0.44, lid: 1, glow: 0, rE: -0.16, lE: -0.16 },
  starting: { headX: 0.18, lid: 0.55, glow: 0.5 },
  listening: {},
  thinking: { headX: -0.14, headY: -0.26, headZ: 0.07, eyeX: -0.7, eyeY: 0.7, brow: 0.5, rX: -0.85, rZ: -0.42, rE: -2.4, open: 0.25 },
  waiting: { headZ: 0.06, brow: 0.7, rX: -0.1, rZ: 2.84, rE: -0.3, open: 1 },
  speaking: { brow: 0.25, lX: -0.42, lZ: 0.26, lE: -1.05, open: 0.7 },
};
const HEARING = { lid: -0.3, brow: 0.6, lean: 0.07, headZ: -0.06 };

export function mountCharacter(root, { bust = false } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });   // 不支持 WebGL 时这里会抛错，由调用方退回 2D 形象
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.domElement.className = 'character';
  renderer.domElement.setAttribute('role', 'img');
  renderer.domElement.setAttribute('aria-label', '数字人形象');
  root.replaceChildren(renderer.domElement);

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.5;
  const camera = new THREE.PerspectiveCamera(27, 1, 0.1, 50);

  const key = new THREE.DirectionalLight(0xfff1e0, 2.6);
  key.position.set(-2.8, 5.5, 4.6);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.radius = 5; key.shadow.bias = -0.0004; key.shadow.normalBias = 0.03;
  Object.assign(key.shadow.camera, { left: -3, right: 3, top: 4.5, bottom: -1, near: 1, far: 16 });
  const fill = new THREE.HemisphereLight(0xdde6ff, 0x4a3a2e, 0.55);
  const back = new THREE.DirectionalLight(0xffffff, 0);       // 轮廓光：颜色 = 状态色
  back.position.set(3.2, 3.4, -4.2);
  scene.add(key, fill, back);

  const tone = new THREE.Color(0x888888), toneNow = new THREE.Color(0x888888);
  const glow = new THREE.MeshStandardMaterial({ color: 0x15171a, emissive: 0x000000, roughness: 0.3 });
  const uRim = { value: new THREE.Color(0, 0, 0) };
  const { J, rim } = build(scene, glow);
  for (const m of rim) {
    m.onBeforeCompile = (sh) => {
      sh.uniforms.uRim = uRim;
      sh.fragmentShader = sh.fragmentShader
        .replace('#include <common>', '#include <common>\nuniform vec3 uRim;')
        .replace('#include <emissivemap_fragment>', '#include <emissivemap_fragment>\n  totalEmissiveRadiance += uRim * pow(1.0 - clamp(dot(normal, normalize(vViewPosition)), 0.0, 1.0), 3.2);');
    };
  }

  const floor = new THREE.Mesh(new THREE.CircleGeometry(4, 48), new THREE.ShadowMaterial({ opacity: 0.26 }));
  floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true;
  scene.add(floor);

  // ---------- 状态 ----------
  const st = { status: 'off', hearing: false, working: false, muted: false };
  const cur = { ...REST, ...POSES.off }, tgt = { ...cur };
  let mouth = 0, mouthIn = 0, ear = 0, earIn = 0, boom = 0, orbit = 0;
  let blinkAt = 2, blink = 0, lookAt = 3, lookX = 0, lookY = 0;

  function retarget() {
    Object.assign(tgt, REST, POSES[st.status] || {});
    if (st.hearing && st.status === 'listening') Object.assign(tgt, HEARING);
    const css = getComputedStyle(root).getPropertyValue('--tone').trim();
    if (css) tone.set(css);
  }

  function resize() {
    const w = root.clientWidth || 1, h = root.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    // 窄窗口时把镜头拉远，保证整个人都在画面里
    const dist = 10.4 * Math.max(1, 0.92 / camera.aspect);
    if (bust) { camera.position.set(0, 3.05, 5.6); camera.lookAt(0, 2.92, 0); }     // 只取头和肩：做应用图标用
    else { camera.position.set(0, 2.3, dist); camera.lookAt(0, 1.78, 0); }
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(root);
  resize();

  const clock = new THREE.Clock();
  const still = window.matchMedia('(prefers-reduced-motion: reduce)');
  function frame() {
    const dt = Math.min(clock.getDelta(), 0.25), t = clock.elapsedTime;
    const live = st.status !== 'off', calm = still.matches ? 0 : 1;
    const ease = (k) => 1 - Math.exp(-dt * k);
    for (const k in tgt) cur[k] += (tgt[k] - cur[k]) * ease(k === 'lid' ? 12 : 5.5);
    mouth += (mouthIn - mouth) * ease(mouthIn > mouth ? 30 : 12);
    ear += (earIn - ear) * ease(14);
    boom += ((st.muted ? 1 : 0) - boom) * ease(7);
    orbit += ((st.working ? 1 : 0) - orbit) * ease(5);

    // 眨眼和眼神游移
    if (live && t > blinkAt) { blink = 1; blinkAt = t + 2.4 + Math.random() * 3.6; }
    blink = Math.max(0, blink - dt * 7.5);
    if (t > lookAt) { lookX = (Math.random() - 0.5) * 0.5; lookY = (Math.random() - 0.5) * 0.3; lookAt = t + 1.6 + Math.random() * 2.6; }
    const wander = live && st.status !== 'thinking' ? calm : 0;

    // 呼吸、重心
    const breath = Math.sin(t * (live ? 1.5 : 1.0)) * calm;
    J.torso.scale.set(1 + breath * 0.008, 1 + breath * 0.014, 1 + breath * 0.02);
    J.body.rotation.x = cur.lean + mouth * 0.015;
    J.body.rotation.z = Math.sin(t * 0.55) * 0.012 * calm * (live ? 1 : 0);
    J.body.rotation.y = -0.1;
    J.head.position.y = 2.54 + breath * 0.014;

    // 头
    const speaking = st.status === 'speaking' ? 1 : 0;
    J.head.rotation.x = cur.headX + Math.sin(t * 0.9) * 0.02 * calm * (live ? 1 : 0.3) - mouth * 0.06 * speaking;
    J.head.rotation.y = cur.headY + Math.sin(t * 0.4) * 0.05 * wander + lookX * 0.12 * wander;
    J.head.rotation.z = cur.headZ + Math.sin(t * 0.63 + 1) * 0.02 * wander;

    // 眼睛、眼皮、眉毛
    const lid = Math.min(1, Math.max(cur.lid, blink));
    for (let i = 0; i < 2; i++) {
      J.lids[i].rotation.x = -0.55 + lid * 2.05 + Math.min(0, cur.lid) * 1.0;
      J.eyes[i].pupil.position.x = (cur.eyeX + lookX * wander) * 0.035;
      J.eyes[i].pupil.position.y = (cur.eyeY + lookY * wander) * 0.03;
      J.eyes[i].glint.visible = lid < 0.5;
      const b = J.brows[i];
      b.mesh.position.y = 0.8 + cur.brow * 0.06;
      b.mesh.rotation.z = Math.PI / 2 - b.sx * (0.1 + cur.brow * 0.14);
    }

    // 嘴：声音越大张得越开
    J.jaw.position.y = 0.1 - mouth * 0.13;
    J.jaw.rotation.x = mouth * 0.16;
    J.mouth.scale.set(1 + mouth * 0.15, 1 + mouth * 3.4, 1);
    J.mouth.position.y = 0.13 - mouth * 0.05;

    // 胳膊。画面左侧那只 = 举手、托下巴；右侧那只 = 说话时比划
    for (const a of J.arms) {
      const R = a.sx < 0;
      let x = R ? cur.rX : cur.lX, z = R ? cur.rZ : cur.lZ, e = R ? cur.rE : cur.lE;
      if (R && st.status === 'waiting') { z += Math.sin(t * 6.5) * 0.11 * calm; e += Math.sin(t * 6.5 + 1) * 0.1 * calm; }
      if (!R && speaking) { x += Math.sin(t * 2.3) * 0.1 * calm - mouth * 0.14; e += Math.sin(t * 3.1) * 0.16 * calm - mouth * 0.2; }
      x += breath * 0.012;
      a.shoulder.rotation.set(x, 0, a.sx * z);
      a.elbow.rotation.set(e, 0, 0);
      const curl = 1.15 - cur.open * (R || speaking ? 1 : 0) * 1.0;
      a.fingers.forEach((f, i) => { f.rotation.x = -curl; f.rotation.z = (i - 1.5) * 0.12 * cur.open; });
      a.hand.rotation.x = -0.15;
    }

    // 耳麦：话筒杆、灯圈；带扣宝石；绕头的小光球
    J.boom.rotation.x = -boom * 1.5;
    const pulse = 1 + ear * 0.22;
    for (const c of J.cups) c.scale.set(1, pulse, pulse);
    J.orbit.visible = orbit > 0.02;
    J.orbit.rotation.y = t * 2.6;
    J.orbit.scale.setScalar(orbit);

    // 状态色：灯、轮廓光
    toneNow.lerp(tone, ease(6));
    const g = cur.glow;
    glow.emissive.copy(toneNow).multiplyScalar(g * (1.5 + ear * 1.3 + mouth * 0.8));
    uRim.value.copy(toneNow).multiplyScalar(g * 0.2);
    back.color.copy(toneNow); back.intensity = g * 2.1;
    key.intensity = 1.5 + g * 1.1;
    fill.intensity = 0.36 + g * 0.2;

    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  retarget();
  requestAnimationFrame(frame);

  return {
    // snap：不做过渡，直接定格到目标神态（截图、看效果用）
    setState(next, snap = false) {
      Object.assign(st, next); retarget();
      if (snap) { Object.assign(cur, tgt); toneNow.copy(tone); boom = st.muted ? 1 : 0; orbit = st.working ? 1 : 0; }
    },
    setMouth(level) { mouthIn = level; },
    setEar(level) { earIn = level; },
  };
}
