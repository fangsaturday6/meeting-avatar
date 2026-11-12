// 从文件加载的 3D 形象：avatar/web/models/<名字>.glb，另可带每种状态一个动作文件 <名字>.<状态>.glb。
// 带骨骼和动作最好；只有一个静态模型也能用（靠整体起伏和点头表现神态）。
// 和模型的约定（tools/figure.py 按这个导出，别的模型有多少算多少）：
//   动作片段叫状态名（listening / thinking / waiting / speaking / off）就在那种状态播；
//   骨骼 jaw、browL / browR、eyeL / eyeR 由这里按实时数据转（张嘴、抬眉、眼神），
//   lidL / lidR 是眼皮：沿骨骼方向压扁 = 睁眼，放开 = 闭眼；
//   名字以 glow 开头的材质叠上状态色。
// 对外只有 loadModel(figure) → { root, materials, update }。光、状态色、镜头都在 character.js。
import * as THREE from 'three';
import { GLTFLoader } from './vendor/three/GLTFLoader.js';

const HEIGHT = 3.6;      // 和代码搭的形象一样高，镜头不用跟着改
const FADE = 0.35;       // 动作之间过渡的秒数
// 模型文件自带多个动作时，每种状态按顺序找名字里带这些词的；都没有就用待机动作
const WORDS = {
  off: ['sleep', 'sit'],
  thinking: ['think', 'ponder'],
  waiting: ['wave', 'raise', 'greet', 'hello'],
  speaking: ['talk', 'speak', 'explain'],
};
const IDLE = /idle|stand|breath/i;

// 这种状态该播哪个动作。own = 是这种状态专属的动作（不是拿待机顶替的）
function pick(clips, status) {
  const exact = clips.find((c) => c.name === status);
  if (exact) return { clip: exact, own: true };
  for (const word of WORDS[status] || []) {
    const clip = clips.find((c) => c.name.toLowerCase().includes(word));
    if (clip) return { clip, own: true };
  }
  return { clip: clips.find((c) => c.name === 'listening') || clips.find((c) => IDLE.test(c.name)) || null, own: false };
}

// 脸：眼皮（0 睁、1 闭）、眉毛（正 = 抬）、眼睛往上看和往旁边看多少
const CALM = { lid: 0, brow: 0, eyeUp: 0, eyeSide: 0 };
const FACE = {
  off: { lid: 1 },
  starting: { lid: 0.5 },
  thinking: { lid: 0.22, brow: 0.5, eyeUp: 0.8, eyeSide: 0.7 },
  waiting: { brow: 1 },
  speaking: { brow: 0.3 },
};
const HEARING = { brow: 0.8 };
const LID_OPEN = 0.05;        // 睁眼时眼皮压到多扁
const JAW = 0.07, BROW = 0.06, EYE = 0.1;     // 张嘴、抬眉、眼神各转多少弧度（轴都放得远，小角度就够）
const FACE_BONES = ['jaw', 'lidL', 'lidR', 'browL', 'browR', 'eyeL', 'eyeR'];

// 按人物的朝向给一根骨骼加一点转动。不用骨骼自己的坐标轴：各家工具导出的骨骼朝向不一样
const up = new THREE.Quaternion(), local = new THREE.Quaternion(), add = new THREE.Quaternion(), euler = new THREE.Euler();
function turn(bone, x, y, z) {
  bone.parent.getWorldQuaternion(up);
  local.copy(up).invert().multiply(add.setFromEuler(euler.set(x, y, z))).multiply(up);
  bone.quaternion.premultiply(local);
}

// 没有专属动作时，用整体姿态补出这种状态的样子
const REST = { bow: 0, tilt: 0, turn: 0, lean: 0, hop: 0 };
const HINTS = {
  off: { bow: 0.34 },
  starting: { bow: 0.14 },
  thinking: { tilt: 0.13, turn: -0.3, bow: -0.1 },
  waiting: { hop: 1 },
};

export async function loadModel(figure) {
  const loader = new GLTFLoader();
  const gltf = await loader.loadAsync(figure.url);
  const body = gltf.scene, materials = [];
  let head = body.getObjectByName('head') || null;
  const face = {};                         // 脸上的骨骼：{ 骨骼, 原始朝向 }，模型里没有的就空着
  for (const name of FACE_BONES) {
    const bone = body.getObjectByName(name);
    if (bone) face[name] = { bone, rest: bone.quaternion.clone() };
  }
  body.traverse((o) => {
    if (o.isBone && !head && /head/i.test(o.name) && !/end|top|nub/i.test(o.name)) head = o;
    if (!o.isMesh) return;
    o.castShadow = true; o.receiveShadow = true; o.frustumCulled = false;
    for (const m of [].concat(o.material)) if (!materials.includes(m)) materials.push(m);
  });

  // 摆正：脚踩地面、站在正中、缩放到统一身高
  body.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(body, true);
  const size = box.getSize(new THREE.Vector3()), mid = box.getCenter(new THREE.Vector3());
  body.position.sub(new THREE.Vector3(mid.x, box.min.y, mid.z));
  const scale = HEIGHT / (size.y || 1);
  const holder = new THREE.Group();
  holder.add(body); holder.scale.setScalar(scale);

  // 脚下一圈光：颜色 = 状态色，跟着听到的声音起伏
  const glows = materials.filter((m) => /^glow/i.test(m.name));
  const ringR = Math.min(Math.max(size.x, size.z) * scale * 0.5 + 0.2, HEIGHT * 0.42);
  const ringMat = new THREE.MeshBasicMaterial({ color: 0x888888, transparent: true, opacity: 0, toneMapped: false, depthWrite: false });
  const ring = new THREE.Mesh(new THREE.RingGeometry(ringR, ringR + 0.09, 96), ringMat);
  ring.rotation.x = -Math.PI / 2; ring.position.y = 0.012;

  const root = new THREE.Group();
  root.add(holder, ring);

  // ---------- 动作 ----------
  const mixer = new THREE.AnimationMixer(body);
  const extra = {};                        // 状态 → 单独文件里的动作
  const headRest = head ? head.quaternion.clone() : null;
  let status = '', action = null, own = false;

  function play(next) {
    status = next;
    const found = extra[next] ? { clip: extra[next], own: true } : pick(gltf.animations, next);
    own = found.own;
    const to = found.clip ? mixer.clipAction(found.clip) : null;
    if (to === action) return;
    if (to) to.reset().fadeIn(FADE).play();
    if (action) action.fadeOut(FADE);
    action = to;
  }

  for (const [name, url] of Object.entries(figure.motions || {})) {
    loader.loadAsync(url).then((g) => {
      if (!g.animations.length) return;
      extra[name] = g.animations[0];
      if (name === status) play(name);
    }, () => {});
  }

  const cur = { ...REST, ...CALM };
  let lookX = 0, lookY = 0, lookAt = 0, blink = 0, blinkAt = 2;

  return {
    root, materials,
    // lv：{ mouth, ear, glow, tone, calm }，由 character.js 每帧算好传进来
    update(dt, t, st, lv) {
      if (st.status !== status) play(st.status);
      const live = st.status !== 'off', ease = 1 - Math.exp(-dt * 5.5);
      const tgt = { ...REST, ...CALM, ...(own ? {} : HINTS[st.status]), ...FACE[st.status] };
      if (st.hearing && st.status === 'listening') Object.assign(tgt, HEARING, { lean: 0.06 });
      for (const k in cur) cur[k] += (tgt[k] - cur[k]) * (k === 'lid' ? 1 - Math.exp(-dt * 12) : ease);
      if (t > lookAt) { lookX = (Math.random() - 0.5) * 0.4; lookY = (Math.random() - 0.5) * 0.25; lookAt = t + 1.8 + Math.random() * 2.6; }
      if (live && t > blinkAt) { blink = 1; blinkAt = t + 2.4 + Math.random() * 3.6; }
      blink = Math.max(0, blink - dt * 7.5);

      if (head) head.quaternion.copy(headRest);          // 动作里没管头的时候，别让下面的偏转越加越多
      mixer.timeScale = live ? 1 : 0.35;
      mixer.update(dt);

      // 整个人：没有动作文件时自己呼吸；想发言时踮一踮；说话时跟着声音微微起伏
      const breath = action ? 0 : Math.sin(t * (live ? 1.5 : 1)) * 0.008 * lv.calm;
      holder.scale.set(scale, scale * (1 + breath), scale);
      holder.position.y = Math.abs(Math.sin(t * 5.2)) * 0.09 * cur.hop * lv.calm + lv.mouth * 0.025;
      holder.rotation.x = cur.lean + (head ? 0 : cur.bow * 0.3);
      holder.rotation.z = (head ? 0 : cur.tilt * 0.4) + Math.sin(t * 0.55) * 0.01 * lv.calm * (live ? 1 : 0);

      // 头：低头、歪头、眼神游移，说话时跟着音量点头
      const wander = live && st.status !== 'thinking' ? lv.calm : 0;
      if (head) turn(head, cur.bow + lv.mouth * 0.1 * (st.status === 'speaking' ? 1 : 0), cur.turn + lookX * 0.3 * wander, cur.tilt);

      // 脸：下巴跟音量，眼皮眨眼，眉毛和眼球跟状态
      for (const name in face) face[name].bone.quaternion.copy(face[name].rest);
      if (face.jaw) turn(face.jaw.bone, lv.mouth * JAW, 0, 0);
      const lid = Math.min(1, Math.max(cur.lid, blink * lv.calm));
      for (const side of ['L', 'R']) {
        if (face['lid' + side]) face['lid' + side].bone.scale.y = LID_OPEN + lid * (1 - LID_OPEN);
        if (face['brow' + side]) turn(face['brow' + side].bone, -cur.brow * BROW, 0, 0);
        if (face['eye' + side]) turn(face['eye' + side].bone, (-cur.eyeUp + lookY * 1.6 * wander) * EYE, (cur.eyeSide + lookX * 1.2 * wander) * EYE, 0);
      }

      for (const m of glows) m.emissive.copy(lv.tone).multiplyScalar(lv.glow * (1.5 + lv.ear * 1.3 + lv.mouth * 0.8));

      ringMat.color.copy(lv.tone);
      ringMat.opacity = lv.glow * (st.muted ? 0.18 : 0.5 + lv.mouth * 0.4);
      ring.scale.setScalar(1 + lv.ear * 0.1 + lv.mouth * 0.05);
    },
  };
}
