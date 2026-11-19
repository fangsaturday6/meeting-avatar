"""给混元 3D 网页版做的屠夫绑骨、补眼睛和眼皮、做每种状态的动作，导出页面用的 GLB。

    /Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup --python tools/figure_hunyuan.py -- [--preview] [--out 路径]
默认导出到 avatar/web/models/figure.glb（仓库里自带的默认形象），页面刷新就换上。
先跑 tools/hunyuan_prepare.py 出 prepared.blend（在 --dir，默认 ~/Documents/3D-Characters/butcher-hunyuan）。

和 tools/figure.py（Codex 那版）同一套和页面的约定（见 avatar/web/model.js 开头），页面不用改：
- 动作片段 listening / thinking / waiting / speaking / off / working，idle-* 是站着时的小动作。
- 骨骼 laptop 带着笔记本电脑（干活时页面放出来）；jaw（下巴，带着整把胡子）、lidL / lidR（眼皮，沿骨骼方向压扁 = 睁眼）、
  browL / browR（眉毛）、eyeL / eyeR（眼珠）由页面驱动。名字以 glow 开头的材质叠状态色（电脑屏幕和标志）。
和 Codex 那版不同的地方：
- 模型是一整块网格（钩子、背上的刀、铁链都长在身上），权重按位置分，不自动算；钩子不能单独甩，没有 idle-hook。
- 生成的眼睛是一团银灰、看不出眼珠，这里在上面贴一层：眼白、眼珠（跟 eyeL / eyeR 转）、眼皮（皮肤色，下沿一道睫毛线）。
- 胡子、眉毛是脸上的一片区域，按离区域中心的远近给 jaw / brow 权重，再沿网格抹匀，免得边上撕开。
坐标：Z 朝上，人物面朝 -Y，人物的左手在 +X，身高 1、脚底 z=0。
"""
import json
import math
import sys
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Euler, Matrix, Quaternion, Vector
from mathutils.bvhtree import BVHTree

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
DIR = Path(ARGS[ARGS.index("--dir") + 1]) if "--dir" in ARGS else Path.home() / "Documents/3D-Characters/butcher-hunyuan"
OUT = Path(ARGS[ARGS.index("--out") + 1]) if "--out" in ARGS else Path(__file__).resolve().parent.parent / "avatar/web/models/figure.glb"
PREVIEW = "--preview" in ARGS
SHOTS = DIR / "preview"
SHOTS.mkdir(exist_ok=True)

_map = json.loads((DIR / "prepared.json").read_text())
S, OX, OY = _map["S"], _map["OX"], _map["OY"]


def px(x, y):
    """正面正交图（ortho-front.png）上的像素 → 模型坐标 (x, z)"""
    return (x - OX) / S, (OY - y) / S


# ---------- 量好的位置（2026-11-19 从 diag/*-grid.png 量的，像素是 ortho-front.png 上的） ----------
EYES = {"R": (953, 280), "L": (1045, 280)}      # 人物右眼在画面左边；眼睛约 38×16 像素，眯着
EYE_W, EYE_H = 40 / S, 18 / S                    # 眼白
LID_W, LID_H = 48 / S, 26 / S                    # 眼皮比眼睛大一圈
BROWS = {"R": (955, 257), "L": (1043, 257)}      # 眉毛中心；眉毛约 55×16 像素
MOUTH_Z = px(0, 372)[1]                          # 下牙床：往下的胡子跟 jaw 走
# 胳膊和身子的分界：离中线比 ARM_EDGE(z) 远的算胳膊。z 0.66 以下胳膊和肚子之间有真缝；往上到肩膀是连着的肉，过渡放宽。
# 左右不对称（身子偏人物右边约 0.03，左胳膊垂得更靠里），两边分开量（diag/armR-grid.png、armL-grid.png）
# 右边小臂内侧有一层贴着肚子（正面图上像是肚子的边），按网格的点量：身子只到 0.23，再往外就是胳膊
ARM_EDGE = {"R": [(0.40, 0.29), (0.45, 0.27), (0.48, 0.24), (0.55, 0.235), (0.58, 0.24), (0.62, 0.248), (0.66, 0.245), (0.71, 0.232),
                  (0.77, 0.21), (0.90, 0.20)],
            "L": [(0.40, 0.29), (0.45, 0.26), (0.50, 0.235), (0.55, 0.228), (0.605, 0.217), (0.63, 0.208), (0.66, 0.205), (0.77, 0.19), (0.90, 0.19)]}
ARM_SOFT = [(0.58, 0.004), (0.66, 0.02), (0.80, 0.03)]
# 关节（正面像素，那一段胳膊横截面的中心）；前后位置脚本自己从网格量（那一段胳膊的前后中点）
JOINTS = {"R": {"shoulder": (585, 585), "elbow": (385, 770), "wrist": (180, 1080), "tip": (100, 1390)},
          "L": {"shoulder": (1410, 590), "elbow": (1560, 770), "wrist": (1660, 1085), "tip": (1760, 1390)}}


def lerp_table(table, z):
    zs, vs = zip(*table)
    return np.interp(z, zs, vs)


def smooth(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def activate(obj, *others):
    bpy.ops.object.select_all(action="DESELECT")
    for o in (obj, *others):
        o.select_set(True)
    bpy.context.view_layer.objects.active = obj


# ---------- 读模型 ----------
bpy.ops.wm.open_mainfile(filepath=str(DIR / "prepared.blend"))
body = bpy.data.objects["butcher"]
me = body.data
co = np.empty(len(me.vertices) * 3)
me.vertices.foreach_get("co", co)
co = co.reshape(-1, 3)
x, y, z = co[:, 0], co[:, 1], co[:, 2]

_bm = bmesh.new()
_bm.from_mesh(me)
_bm.faces.ensure_lookup_table()
_bm.verts.ensure_lookup_table()
TREE = BVHTree.FromBMesh(_bm)
EDGES = np.array([(e.verts[0].index, e.verts[1].index) for e in _bm.edges])
base_img = bpy.data.images["butcher_base"]
TW, TH = base_img.size
tex = np.array(base_img.pixels[:], dtype=np.float32).reshape(TH, TW, 4)[:, :, :3]     # sRGB 值，第 0 行在下面


def to_linear(c):
    return np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)


def blur(w, times):
    """沿网格的边把权重抹匀：每次每个点取自己和邻居的平均"""
    for _ in range(times):
        acc, cnt = w.copy(), np.ones_like(w)
        np.add.at(acc, EDGES[:, 0], w[EDGES[:, 1]])
        np.add.at(acc, EDGES[:, 1], w[EDGES[:, 0]])
        np.add.at(cnt, EDGES[:, 0], 1)
        np.add.at(cnt, EDGES[:, 1], 1)
        w = acc / cnt
    return w


def face_hit(fx, fz):
    hit, nrm, idx, _ = TREE.ray_cast(Vector((fx, -1, fz)), Vector((0, 1, 0)))
    if hit is None:
        raise RuntimeError(f"ray missed the face at x={fx:.4f} z={fz:.4f}")
    if nrm.y > 0:
        nrm = -nrm
    return hit, nrm, idx


def on_face(fx, fz, off):
    hit, nrm, _ = face_hit(fx, fz)
    return hit + nrm * off


def albedo_at(fx, fz):
    from mathutils.geometry import barycentric_transform  # noqa: PLC0415
    hit, _, idx = face_hit(fx, fz)
    f = _bm.faces[idx]
    a, b_, c = (lp.vert.co for lp in f.loops[:3])
    uvl = _bm.loops.layers.uv.active
    ua, ub, uc = (Vector((*lp[uvl].uv, 0)) for lp in f.loops[:3])
    uv = barycentric_transform(hit, a, b_, c, ua, ub, uc)
    u = int(np.clip((uv.x % 1) * TW, 0, TW - 1))
    v = int(np.clip((uv.y % 1) * TH, 0, TH - 1))
    return tex[v, u]


def skin_around(cx_px, cy_px, w_px, h_px):
    pts = []
    for k in range(32):
        a = 2 * math.pi * k / 32
        c = albedo_at(*px(cx_px + math.cos(a) * w_px * 0.75, cy_px + math.sin(a) * h_px * 1.1))
        if c.max() > 0.35:
            pts.append(c)
    return tuple(float(v) for v in to_linear(np.median(pts, axis=0)))


def flat_material(name, rgb, rough=0.6):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    p = m.node_tree.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = (*rgb, 1)
    p.inputs["Roughness"].default_value = rough
    return m


def new_object(name, bm, mat, smooth_faces=True):
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    for p in mesh.polygons:
        p.use_smooth = smooth_faces
    mesh.materials.append(mat)
    o = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(o)
    return o


def disc(name, center_px, w, h, off, mat, top=1.0, bottom=1.0, segs=40, rings=8):
    """贴在脸上的一片椭圆（上半边高 top×h/2、下半边 bottom×h/2），一圈圈往前打到脸上"""
    cx, cz = px(*center_px)
    bm = bmesh.new()
    center = bm.verts.new(on_face(cx, cz, off))
    prev = None
    for ring_i in range(1, rings + 1):
        k = ring_i / rings
        ring = []
        for j in range(segs):
            a = 2 * math.pi * j / segs
            sz = math.sin(a) * (top if math.sin(a) > 0 else bottom)
            ring.append(bm.verts.new(on_face(cx + math.cos(a) * w / 2 * k, cz + sz * h / 2 * k, off)))
        for j in range(segs):
            if prev is None:
                bm.faces.new((center, ring[j], ring[(j + 1) % segs]))
            else:
                bm.faces.new((prev[j], ring[j], ring[(j + 1) % segs], prev[(j + 1) % segs]))
        prev = ring
    return new_object(name, bm, mat)


def ribbon(name, points_px, width_px, off, mat):
    bm = bmesh.new()
    pts = [Vector((*px(a, b), 0)) for a, b in points_px]
    rows = []
    for i, p in enumerate(pts):
        a, b2 = pts[max(i - 1, 0)], pts[min(i + 1, len(pts) - 1)]
        d = (b2 - a).normalized()
        n2 = Vector((-d.y, d.x, 0))
        half = width_px / S / 2 * math.sin(math.pi * (i + 0.5) / len(pts)) ** 0.6
        rows.append([bm.verts.new(on_face(p.x + n2.x * sgn * half, p.y + n2.y * sgn * half, off)) for sgn in (-1, 1)])
    for i in range(len(rows) - 1):
        bm.faces.new((rows[i][0], rows[i + 1][0], rows[i + 1][1], rows[i][1]))
    return new_object(name, bm, mat, smooth_faces=False)


# ---------- 眼睛：眼白、眼珠、眼皮 ----------
sclera = flat_material("Eye white", (0.78, 0.72, 0.64), 0.35)
iris = flat_material("Eye iris", (0.10, 0.045, 0.02), 0.3)
pupil = flat_material("Eye pupil", (0.008, 0.006, 0.005), 0.3)
spark = flat_material("Eye sparkle", (1, 1, 1), 0.2)
lash = flat_material("Lid lash", (0.03, 0.016, 0.012), 0.5)
decals = []          # [(物体, 骨骼)]
eye_front = {}
for side, (ex_px, ey_px) in EYES.items():
    sx = -1 if side == "R" else 1                       # 外眼角朝哪边（画面上）
    decals.append((disc(f"Eye white {side}", (ex_px, ey_px), EYE_W, EYE_H, 0.0010, sclera, top=0.9, bottom=1.0), "head"))
    look = (ex_px - sx * 2, ey_px)                       # 眼珠稍微往里收一点：盯着前面的人
    decals.append((disc(f"Eye iris {side}", look, 13 / S, 13 / S, 0.0016, iris, segs=28, rings=4), f"eye{side}"))
    decals.append((disc(f"Eye pupil {side}", look, 6 / S, 6 / S, 0.0020, pupil, segs=20, rings=3), f"eye{side}"))
    decals.append((disc(f"Eye sparkle {side}", (look[0] + 3, look[1] - 3), 3 / S, 3 / S, 0.0024, spark, segs=12, rings=2), f"eye{side}"))
    lid_rgb = skin_around(ex_px, ey_px, LID_W * S, LID_H * S)
    print("LID", side, tuple(round(v, 3) for v in lid_rgb))
    decals.append((disc(f"Eyelid {side}", (ex_px, ey_px - 1), LID_W, LID_H, 0.0030, flat_material(f"Lid skin {side}", lid_rgb, 0.8),
                        top=1.0, bottom=0.8), f"lid{side}"))
    half = 21
    line = [(ex_px - sx * half + sx * 2 * half * t, ey_px + 4 + 4 * math.sin(math.pi * t) - 3 * t) for t in np.linspace(0, 1, 15)]
    decals.append((ribbon(f"Eyelid lash {side}", line, 3.0, 0.0034, lash), f"lid{side}"))
    eye_front[side] = face_hit(*px(ex_px, ey_px))[0]

# ---------- 沿胳膊和身子的交界切开 ----------
# 生成的网格是一整块：小臂内侧、大臂下沿和肚子侧面在好几处粘在一起，胳膊一抬就扯出一大片膜。
# CUT_TOP 以下按面中心落在哪边分给胳膊或身子，交界上的边切开（顶点各留一份），胳膊动时直接分开走；
# 再往上到肩膀是真连着的肉，不切，权重慢慢过渡
CUT_TOP = 0.665


def arm_edge(px_, pz):
    return np.where(px_ > 0, lerp_table(ARM_EDGE["L"], pz), lerp_table(ARM_EDGE["R"], pz))


def arm_side(px_, pz):
    return (np.abs(px_) > arm_edge(px_, pz)) & (pz > HAND_LOW)      # 指尖以下是靴子：外沿也伸到这么宽，别切下来


HAND_LOW = 0.26


fc = np.array([f.calc_center_median()[:] for f in _bm.faces])
face_arm = arm_side(fc[:, 0], fc[:, 2]) & (fc[:, 2] < CUT_TOP)
face_low = fc[:, 2] < CUT_TOP


def cut_edges():
    return [e for e in _bm.edges if len(e.link_faces) == 2
            and face_low[e.link_faces[0].index] and face_low[e.link_faces[1].index]
            and face_arm[e.link_faces[0].index] != face_arm[e.link_faces[1].index]]


# 切完会剩下孤零零的小块：铁链内侧、手背上的几片按位置分到了身子那边，切开后不和身子连着，胳膊一抬就留在腰边飘着；
# 反过来也有。这种块（不是最大的那几块）整块归到它隔着切缝挨得最多的那一边
for _ in range(3):
    cut = cut_edges()
    cut_set = {e.index for e in cut}
    comp = np.full(len(_bm.faces), -1)
    sizes = []
    for f in _bm.faces:
        if comp[f.index] >= 0:
            continue
        cid, todo, n = len(sizes), [f], 0
        comp[f.index] = cid
        while todo:
            g = todo.pop()
            n += 1
            for e in g.edges:
                if e.index in cut_set:
                    continue
                for h in e.link_faces:
                    if comp[h.index] < 0:
                        comp[h.index] = cid
                        todo.append(h)
        sizes.append(n)
    big = set(np.argsort(sizes)[-3:])                 # 身子、两条胳膊
    votes = {}
    for e in cut:
        a, b2 = (lf.index for lf in e.link_faces)
        for me_, other in ((a, b2), (b2, a)):
            if comp[me_] not in big:
                votes.setdefault(comp[me_], []).append(face_arm[other])
    flipped = 0
    for cid, vs in votes.items():
        side = np.mean(vs) > 0.5
        members = comp == cid
        if face_arm[members][0] != side:
            face_arm[members] = side
            flipped += int(members.sum())
    print("ISLANDS", len(sizes), "flipped faces", flipped)
    if not flipped:
        break
cut = cut_edges()
bm_cut = _bm.copy()
bm_cut.edges.ensure_lookup_table()
bmesh.ops.split_edges(bm_cut, edges=[bm_cut.edges[e.index] for e in cut])
bm_cut.verts.ensure_lookup_table()
bm_cut.faces.ensure_lookup_table()
vert_cut_arm = np.array([any(face_arm[f.index] for f in v.link_faces) for v in bm_cut.verts])      # 切开后每个点只挨着一边
vert_low = np.array([all(face_low[f.index] for f in v.link_faces) for v in bm_cut.verts])
bm_cut.to_mesh(me)
EDGES = np.array([(e.verts[0].index, e.verts[1].index) for e in bm_cut.edges])
bm_cut.free()
print("SPLIT edges", len(cut))
co = np.empty(len(me.vertices) * 3)
me.vertices.foreach_get("co", co)
co = co.reshape(-1, 3)
x, y, z = co[:, 0], co[:, 1], co[:, 2]

# ---------- 关节：前后位置从网格量 ----------
edge = arm_edge(x, z)
soft = lerp_table(ARM_SOFT, z)
behind = smooth(0.11, 0.14, y) * smooth(0.58, 0.62, z)          # 背上的刀：在肩膀后面，不算胳膊
armness = smooth(edge - soft, edge + soft, np.abs(x)) * (1 - behind)
armness = blur(armness, 3)                                       # 切缝两边已经不相连，抹匀不会抹过缝
armness = np.where(vert_low, vert_cut_arm.astype(float), armness)


def joint(side, name):
    jx, jz = px(*JOINTS[side][name])
    near = (np.abs(x - jx) < 0.02) & (np.abs(z - jz) < 0.02) & (armness > 0.5)
    if not near.any():
        raise RuntimeError(f"no arm vertices near {side} {name}")
    return Vector((jx, float((y[near].min() + y[near].max()) / 2), jz))


J = {side: {name: joint(side, name) for name in JOINTS[side]} for side in ("R", "L")}
_sy = (J["R"]["shoulder"].y + J["L"]["shoulder"].y) / 2       # 左肩盖着护肩，量出来偏前；两肩取同一个前后位置
J["R"]["shoulder"].y = J["L"]["shoulder"].y = _sy
for side in J:
    print("JOINTS", side, {k: tuple(round(c, 3) for c in v) for k, v in J[side].items()})

# ---------- 骨架 ----------
LAPTOP = Vector((0.0, -0.33, 0.55))     # 笔记本电脑底座的中心：大肚子前面，屏幕顶到胡子底下
BONES = [("hips", (0, 0, 0.30), (0, 0, 0.42), None),
         ("spine", (0, 0, 0.42), (0, 0, 0.58), "hips"),
         ("belly", (0, -0.02, 0.55), (0, -0.25, 0.55), "spine"),
         ("chest", (0, 0, 0.58), (0, 0, 0.74), "spine"),
         ("neck", (0, 0, 0.74), (0, 0, 0.80), "chest"),
         ("head", (0, 0, 0.80), (0, 0, 0.95), "neck"),
         ("jaw", (0, 0.07, 0.85), (0, -0.12, 0.80), "head")]        # 轴放在脑后：张嘴时胡子往下走，不往胸口里扎
for side in ("R", "L"):
    j = J[side]
    ef = eye_front[side]
    bx, bz = px(*BROWS[side])
    brow_y = face_hit(bx, bz)[0].y
    BONES += [(f"arm{side}", tuple(j["shoulder"]), tuple(j["elbow"]), "chest"),
              (f"forearm{side}", tuple(j["elbow"]), tuple(j["wrist"]), f"arm{side}"),
              (f"hand{side}", tuple(j["wrist"]), tuple(j["tip"]), f"forearm{side}"),
              (f"eye{side}", (ef.x, ef.y + 0.02, ef.z), (ef.x, ef.y - 0.01, ef.z), "head"),
              (f"lid{side}", (ef.x, ef.y - 0.02, ef.z + LID_H / 2), (ef.x, ef.y - 0.02, ef.z - LID_H / 2 * 0.8 - 0.002), "head"),
              (f"brow{side}", (bx, brow_y + 0.15, bz), (bx, brow_y - 0.02, bz), "head")]     # 轴放远：页面只转 0.06 弧度
BONES += [("laptop", tuple(LAPTOP), tuple(LAPTOP + Vector((0, -0.08, 0))), "spine")]

arm = bpy.data.objects.new("butcher_rig", bpy.data.armatures.new("butcher_rig"))
bpy.context.scene.collection.objects.link(arm)
activate(arm)
bpy.ops.object.mode_set(mode="EDIT")
for name, head, tail, parent in BONES:
    eb = arm.data.edit_bones.new(name)
    eb.head, eb.tail, eb.roll = head, tail, 0
    if parent:
        eb.parent = arm.data.edit_bones[parent]
bpy.ops.object.mode_set(mode="OBJECT")

# ---------- 权重 ----------
w = {}
trunk = 1 - armness
w["hips"] = trunk * (1 - smooth(0.40, 0.46, z))
w["spine"] = trunk * smooth(0.40, 0.46, z) * (1 - smooth(0.56, 0.62, z))
w["chest"] = trunk * smooth(0.56, 0.62, z) * (1 - smooth(0.73, 0.78, z))
upper = trunk * smooth(0.73, 0.78, z)
# 头：脖子往上、离中线 0.14 以内、不在背后（背上的刀把、肩膀后面的肉跟胸口）
in_head = (1 - smooth(0.12, 0.15, np.abs(x))) * (1 - smooth(0.07, 0.10, y))
headness = blur(smooth(0.76, 0.80, z) * in_head, 4)
w["head"] = upper * headness
in_neck = (1 - smooth(0.13, 0.17, np.abs(x))) * (1 - smooth(0.08, 0.11, y))     # 脖子只管中间；两边的肩膀、背上的刀跟胸口
w["neck"] = upper * (1 - headness) * in_neck
w["chest"] = w["chest"] + upper * (1 - headness) * (1 - in_neck)
# 胡子：下牙床往下、前面那一大把（盖在胸口上），按网格抹匀，边上慢慢过渡到头 / 胸口
beard = ((z < MOUTH_Z) & (z > 0.70) & (np.abs(x) < 0.095 + 0.15 * (z - 0.70)) & (y < -0.035)).astype(float)
beard = blur(beard, 6) * trunk
w["jaw"] = beard
for name in ("chest", "neck", "head"):
    w[name] = w[name] * (1 - beard)
# 眉毛：眉心附近、脸表面上那一层
for side, (bx_px, by_px) in BROWS.items():
    bx, bz = px(bx_px, by_px)
    surf = face_hit(bx, bz)[0].y
    d = np.sqrt(((x - bx) / (40 / S)) ** 2 + ((z - bz) / (17 / S)) ** 2)
    browness = blur((1 - smooth(0.6, 1.0, d)) * (1 - smooth(surf + 0.006, surf + 0.016, y)), 2)
    w[f"brow{side}"] = browness * w["head"]
    w["head"] = w["head"] - w[f"brow{side}"]
# 肚子：前面鼓出来的那一团（呼吸、拍肚子时缩放）
bel = (1 - smooth(0.12, 0.2, np.sqrt((x / 1.25) ** 2 + ((z - 0.55) / 1.0) ** 2))) * smooth(-0.10, -0.16, y) * trunk
bel = blur(bel, 3) * (1 - beard)
for name in ("spine", "chest"):
    w[name] = w[name] * (1 - bel)
w["belly"] = bel
# 胳膊沿肩→肘→腕→指尖分
for side in ("R", "L"):
    on = armness * ((x > 0) if side == "L" else (x < 0))
    j = {k: np.array(v[:]) for k, v in J[side].items()}

    def along(p0, p1):
        seg = p1 - p0
        return ((co - p0) @ seg) / (seg @ seg)
    fore = smooth(0.85, 1.15, along(j["shoulder"], j["elbow"]))
    hand = smooth(0.9, 1.1, along(j["elbow"], j["wrist"]))
    w[f"arm{side}"] = on * (1 - fore)
    w[f"forearm{side}"] = on * fore * (1 - hand)
    w[f"hand{side}"] = on * fore * hand
total = sum(w.values())
for name in w:
    vg = body.vertex_groups.new(name=name)
    vals = w[name] / np.maximum(total, 1e-6)
    idx = np.nonzero(vals > 1e-3)[0]
    for i in idx:
        vg.add([int(i)], float(vals[i]), "REPLACE")
    print("WEIGHT", name, len(idx))
for o, bone in decals:
    o.vertex_groups.new(name=bone).add(range(len(o.data.vertices)), 1.0, "REPLACE")


# ---------- 笔记本电脑（干活时用；屏幕和盖子上的标志是状态色） ----------
def add_laptop():
    glow = flat_material("glow laptop", (0.06, 0.07, 0.08), 0.3)
    shell = flat_material("Laptop shell", (0.10, 0.11, 0.13), 0.38)
    shell.node_tree.nodes["Principled BSDF"].inputs["Metallic"].default_value = 0.6
    out = []
    k = 0.16                                                   # 和 Codex 那版同样比例（那边站高 6.4，这边 1）
    lean = Euler((math.radians(-16), 0, 0)).to_matrix().to_4x4()
    hinge = Matrix.Translation(LAPTOP + Vector((0, -0.5 * k, 0.005)))
    for name, mat, matrix in [
            ("Laptop base", shell, Matrix.Translation(LAPTOP) @ Matrix.Diagonal((1.5 * k, 1.0 * k, 0.07 * k, 1))),
            ("Laptop lid", shell, hinge @ lean @ Matrix.Translation((0, 0, 0.5 * k)) @ Matrix.Diagonal((1.5 * k, 0.05 * k, 1.0 * k, 1))),
            ("Laptop screen", glow, hinge @ lean @ Matrix.Translation((0, 0.03 * k, 0.5 * k)) @ Matrix.Diagonal((1.36 * k, 0.01 * k, 0.86 * k, 1))),
            ("Laptop logo", glow, hinge @ lean @ Matrix.Translation((0, -0.03 * k, 0.52 * k)) @ Matrix.Diagonal((0.26 * k, 0.012 * k, 0.26 * k, 1)))]:
        bm = bmesh.new()
        if "logo" in name:
            bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=10, radius=0.5, matrix=matrix)
        else:
            made = bmesh.ops.create_cube(bm, size=1, matrix=matrix)
            bmesh.ops.bevel(bm, geom=list({e for v in made["verts"] for e in v.link_edges}), offset=0.003, segments=2, affect="EDGES")
        o = new_object(name, bm, mat, smooth_faces="logo" in name)
        o.vertex_groups.new(name="laptop").add(range(len(o.data.vertices)), 1.0, "REPLACE")
        out.append(o)
    return out


laptop_parts = add_laptop()
activate(body, *[o for o, _ in decals], *laptop_parts)
bpy.ops.object.join()
body.name = me.name = "figure"
mod = body.modifiers.new("rig", "ARMATURE")
mod.object = arm
body.parent = arm


# ---------- 动作（摆姿势的写法同 tools/figure.py） ----------
def aim(*d):
    """这根骨骼指向哪（人物坐标：+X 他的左边，-Y 前面，+Z 上面）"""
    return {"aim": Vector(d)}


def rot(x=0, y=0, z=0):
    """在原来的姿势上转多少度：X 点头为正，Y 往他的右边歪为正，Z 转向他的左边为正"""
    return {"rot": (x, y, z)}


def pose(spec, last):
    world, head, tip = {}, {}, {}
    for b in arm.data.bones:
        rest, s = b.matrix_local.to_quaternion(), spec.get(b.name, {})
        if b.parent:
            parent = b.parent.matrix_local.to_quaternion().inverted()
            up = world[b.parent.name] @ (parent @ rest)
            origin = head[b.parent.name] + world[b.parent.name] @ (parent @ (b.head_local - b.parent.head_local))
        else:
            up, origin = rest, b.head_local.copy()
        turn = Quaternion()
        if "aim" in s:
            turn = (up @ Vector((0, 1, 0))).rotation_difference(s["aim"].normalized())
        if "rot" in s:
            turn = Euler([math.radians(v) for v in s["rot"]], "XYZ").to_quaternion() @ turn
        q = up.inverted() @ turn @ up
        if b.name in last and q.dot(last[b.name]) < 0:
            q.negate()
        last[b.name] = q
        move, squash = Vector(s.get("move", (0, 0, 0))), s.get("squash", 1)
        move.z -= b.head_local.z * (1 - squash)
        world[b.name] = up @ q
        head[b.name] = origin + move
        tip[b.name] = head[b.name] + world[b.name] @ (rest.inverted() @ (b.tail_local - b.head_local))
        pb = arm.pose.bones[b.name]
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = q
        pb.location = up.inverted() @ move
        size = s.get("scale", 1)
        pb.scale = (size / math.sqrt(squash), size * squash, size / math.sqrt(squash))


def loop(*keys, every):
    return [(1 + i * every, k) for i, k in enumerate((*keys, keys[0]))]


def mix(*specs):
    out = {}
    for s in specs:
        for bone, v in s.items():
            out[bone] = {**out.get(bone, {}), **v}
    return out


def mirror(spec):
    """右边的姿势照到左边：aim 的 X 取反、rot 的 Y 和 Z 取反，骨骼名 R→L"""
    out = {}
    for bone, v in spec.items():
        name = bone[:-1] + {"L": "R", "R": "L"}[bone[-1]] if bone[:-1] in ("arm", "forearm", "hand") else bone
        v2 = dict(v)
        if "aim" in v:
            v2["aim"] = Vector((-v["aim"].x, v["aim"].y, v["aim"].z))
        if "rot" in v:
            v2["rot"] = (v["rot"][0], -v["rot"][1], -v["rot"][2])
        out[name] = v2
    return out


IN, OUT_ = {"scale": 1.0}, {"scale": 1.035}
# 放松站着：生成时就是 A 字站姿，胳膊和大肚子之间只剩一指宽，不能再往里收；小臂微微往前
RELAX = {"forearmR": rot(x=-8), "forearmL": rot(x=-8), "handR": rot(x=-6), "handL": rot(x=-6)}
# 胳膊往前收到身前（说话比划、捋胡子、敲电脑都从这儿出发）。aim 都是左手的，右手用 mirror
L_FRONT = {"armL": aim(0.55, -0.55, -0.62)}
TALK_A = {**L_FRONT, "forearmL": aim(0.05, -0.95, 0.30), "handL": aim(0.1, -0.9, 0.45)}
TALK_B = {"armL": aim(0.65, -0.42, -0.62), "forearmL": aim(0.40, -0.90, 0.10), "handL": aim(0.5, -0.85, 0.15)}
TALK_C = {**L_FRONT, "forearmL": aim(-0.15, -0.95, 0.40), "handL": aim(-0.05, -0.9, 0.6)}
BEARD = {"armL": aim(0.40, -0.75, -0.52), "forearmL": aim(-0.62, -0.45, 0.65), "handL": aim(-0.55, -0.35, 0.75)}
RAISE = {"armL": aim(0.86, -0.18, 0.30), "forearmL": aim(0.05, -0.2, 1), "handL": aim(0.15, -0.2, 1)}     # 大臂只抬到比肩略高：再高腋下的肉扯得太厉害
TYPE_L = {"armL": aim(0.45, -0.62, -0.65), "forearmL": aim(-0.45, -0.88, -0.10), "handL": aim(-0.55, -0.8, -0.2)}
_UP_R = {"armR": aim(-0.88, 0.05, 0.25), "forearmR": aim(-0.15, 0.1, 1), "handR": aim(-0.1, 0.15, 1)}     # 伸懒腰：两只胳膊端平、小臂竖起来
UP = {**_UP_R, **mirror(_UP_R)}
PAT = {"armL": aim(0.50, -0.62, -0.60), "forearmL": aim(-0.65, -0.62, -0.30), "handL": aim(-0.75, -0.5, -0.35)}
SLUMP = {"spine": rot(x=5), "chest": rot(x=6), "neck": rot(x=6), "head": rot(x=14, z=3),
         "armR": rot(x=-6, y=-4), "armL": rot(x=-6, y=4), "forearmR": rot(x=-4), "forearmL": rot(x=-4)}


def breathe(spec, k):
    return mix(spec, {"belly": OUT_ if k % 2 else IN, "chest": rot(x=-1.0 if k % 2 else 0.4)})


CLIPS = {
    # 在听：站着呼吸，重心左右换，头微微转
    "listening": loop(breathe(mix(RELAX, {"spine": rot(y=1.0), "head": rot(x=1, z=-3), "armR": rot(y=2), "armL": rot(y=-1)}), 0),
                      breathe(mix(RELAX, {"spine": rot(y=-0.5), "head": rot(x=2, z=2)}), 1),
                      breathe(mix(RELAX, {"spine": rot(y=-1.0), "head": rot(x=1, z=3), "armR": rot(y=1), "armL": rot(y=-2)}), 0),
                      breathe(mix(RELAX, {"spine": rot(y=0.5), "head": rot(x=2, z=-2)}), 1), every=30),
    # 在想：左手捋胡子，抬头看斜上方
    "thinking": loop(breathe(mix(RELAX, BEARD, {"head": rot(x=-7, y=3, z=9), "chest": rot(x=-2)}), 0),
                     breathe(mix(RELAX, BEARD, {"forearmL": aim(-0.58, -0.47, 0.72), "head": rot(x=-8, y=4, z=11), "chest": rot(x=-2.5)}), 1), every=26),
    # 想发言：举起左手挥，身子一颠一颠
    "waiting": loop(mix(RELAX, RAISE, {"forearmL": aim(-0.22, -0.2, 1), "handL": aim(-0.35, -0.2, 1), "head": rot(x=-4, y=-2, z=5),
                                       "chest": rot(y=2), "hips": {"squash": 0.97}}),
                    mix(RELAX, RAISE, {"forearmL": aim(0.25, -0.2, 1), "handL": aim(0.4, -0.2, 1), "head": rot(x=-5, y=-1, z=6),
                                       "chest": rot(y=2), "hips": {"squash": 1.02, "move": (0, 0, 0.012)}}), every=10),
    # 在说：两只手轮着在身前比划
    "speaking": loop(breathe(mix(RELAX, TALK_A, {"chest": rot(z=-2), "head": rot(x=1, z=2)}), 0),
                     breathe(mix(RELAX, TALK_B, mirror(TALK_C), {"chest": rot(z=2), "head": rot(x=-1, z=-2)}), 1),
                     breathe(mix(RELAX, TALK_C, {"chest": rot(z=-1), "head": rot(x=2, z=1)}), 0),
                     breathe(mix(RELAX, mirror(TALK_A), {"chest": rot(z=2.5), "head": rot(x=0, z=-3)}), 1), every=16),
    # 未开会：垂头弓背，慢慢呼吸
    "off": loop(mix(SLUMP, {"belly": IN}), mix(SLUMP, {"chest": rot(x=7.5), "belly": OUT_}), every=72),
    # 后台在干活：两手在身前敲电脑，偶尔抬头看一眼
    "working": loop(mix(TYPE_L, mirror(TYPE_L), {"head": rot(x=12), "chest": rot(x=2), "belly": IN}),
                    mix(TYPE_L, mirror(TYPE_L), {"forearmL": aim(-0.40, -0.9, -0.18), "head": rot(x=13, z=-2), "chest": rot(x=2), "belly": OUT_}),
                    mix(TYPE_L, mirror(TYPE_L), {"forearmR": aim(0.40, -0.9, -0.18), "head": rot(x=12, z=2), "chest": rot(x=2), "belly": IN}),
                    mix(TYPE_L, mirror(TYPE_L), {"head": rot(x=2, z=-4), "chest": rot(x=1), "belly": OUT_}), every=10),

    # ---- 站着时自己做的小动作：从放松站姿开始，回到放松站姿 ----
    "idle-look": [(1, RELAX), (16, mix(RELAX, {"chest": rot(z=8), "head": rot(x=-3, z=16), "spine": rot(z=4)})),
                  (32, mix(RELAX, {"chest": rot(z=9), "head": rot(x=-2, z=18), "spine": rot(z=4)})),
                  (50, mix(RELAX, {"chest": rot(z=-8), "head": rot(x=-3, z=-16), "spine": rot(z=-4)})),
                  (66, mix(RELAX, {"chest": rot(z=-9), "head": rot(x=-2, z=-18), "spine": rot(z=-4)})), (82, RELAX)],
    "idle-stretch": [(1, RELAX), (22, mix(UP, {"chest": rot(x=-6), "head": rot(x=-10), "belly": {"scale": 1.06}, "hips": {"squash": 1.03}})),
                     (40, mix(UP, {"forearmL": aim(0.1, 0.2, 1), "forearmR": aim(-0.1, 0.2, 1), "chest": rot(x=-7, z=3), "head": rot(x=-11, z=4),
                                   "belly": {"scale": 1.07}, "hips": {"squash": 1.04}})), (60, RELAX)],
    "idle-belly": [(1, RELAX), (14, mix(RELAX, PAT, {"head": rot(x=9, z=4)})),
                   (20, mix(RELAX, PAT, {"forearmL": aim(-0.72, -0.52, -0.36), "head": rot(x=10, z=4), "belly": {"scale": 1.06}})),
                   (26, mix(RELAX, PAT, {"head": rot(x=9, z=4), "belly": {"scale": 0.98}})),
                   (32, mix(RELAX, PAT, {"forearmL": aim(-0.72, -0.52, -0.36), "head": rot(x=10, z=4), "belly": {"scale": 1.06}})),
                   (38, mix(RELAX, PAT, {"head": rot(x=6, z=3), "belly": {"scale": 0.98}})), (56, RELAX)],
    "idle-jump": [(1, RELAX), (8, mix(RELAX, {"hips": {"squash": 0.88}, "head": rot(x=5)})),
                  (13, mix(RELAX, {"hips": {"squash": 1.06, "move": (0, 0, 0.04)}, "armL": rot(y=-12), "armR": rot(y=12), "head": rot(x=-4)})),
                  (19, mix(RELAX, {"hips": {"move": (0, 0, 0.08)}, "armL": rot(y=-18), "armR": rot(y=18), "head": rot(x=-6), "belly": {"scale": 0.98}})),
                  (25, mix(RELAX, {"hips": {"squash": 1.04, "move": (0, 0, 0.03)}, "armL": rot(y=-6), "armR": rot(y=6)})),
                  (29, mix(RELAX, {"hips": {"squash": 0.86}, "belly": {"scale": 1.07}, "head": rot(x=6)})),
                  (35, mix(RELAX, {"hips": {"squash": 1.03}, "belly": {"scale": 0.99}})), (44, RELAX)],
    "idle-beard": [(1, RELAX), (16, mix(RELAX, BEARD, {"head": rot(x=-3, z=4)})),
                   (26, mix(RELAX, BEARD, {"forearmL": aim(-0.58, -0.40, 0.70), "handL": aim(-0.5, -0.3, 0.8), "head": rot(x=-4, z=5)})),
                   (36, mix(RELAX, BEARD, {"head": rot(x=-3, z=4)})),
                   (46, mix(RELAX, BEARD, {"forearmL": aim(-0.58, -0.40, 0.70), "handL": aim(-0.5, -0.3, 0.8), "head": rot(x=-4, z=5)})), (62, RELAX)],
}
ANIMATED = ["hips", "spine", "belly", "chest", "neck", "head", "armR", "forearmR", "handR", "armL", "forearmL", "handL"]
MOVED, SCALED = ["hips"], ["belly", "hips"]

arm.animation_data_create()
for name, keys in CLIPS.items():
    act = bpy.data.actions.new(name)
    arm.animation_data.action = act
    if hasattr(act, "slots") and not act.slots:
        arm.animation_data.action_slot = act.slots.new(id_type="OBJECT", name=arm.name)
    last = {}
    for frame, spec in keys:
        pose(spec, last)
        for b in ANIMATED:
            arm.pose.bones[b].keyframe_insert("rotation_quaternion", frame=frame)
        for b in MOVED:
            arm.pose.bones[b].keyframe_insert("location", frame=frame)
        for b in SCALED:
            arm.pose.bones[b].keyframe_insert("scale", frame=frame)
    track = arm.animation_data.nla_tracks.new()
    track.name = name
    track.strips.new(name, 1, act)
    arm.animation_data.action = None
pose({}, {})
bpy.ops.wm.save_as_mainfile(filepath=str(DIR / "rigged.blend"))


# ---------- 预览 ----------
def preview():
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    world = bpy.data.worlds.new("studio")
    scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.55, 0.57, 0.6, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 0.8
    bpy.ops.object.light_add(type="SUN", location=(-1.5, -3, 3))
    sun = bpy.context.object
    sun.data.energy = 1.2
    sun.rotation_euler = (Vector((0, 0, 0.5)) - sun.location).to_track_quat("-Z", "Y").to_euler()
    bpy.ops.object.camera_add(location=(0.8, -4, 0.55))
    cam = bpy.context.object
    cam.data.type, cam.data.ortho_scale = "ORTHO", 1.15
    scene.camera = cam
    scene.view_settings.view_transform = "Standard"

    def look(loc, at, ortho, rx, ry):
        cam.location = loc
        cam.rotation_euler = (Vector(at) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
        cam.data.ortho_scale = ortho
        scene.render.resolution_x, scene.render.resolution_y = rx, ry

    def face(lid=0.05, mouth=0.0, brow=0.0):
        arm.pose.bones["lidL"].scale = arm.pose.bones["lidR"].scale = (1, lid, 1)
        arm.pose.bones["jaw"].rotation_mode = "XYZ"
        arm.pose.bones["jaw"].rotation_euler = (mouth * 0.07, 0, 0)
        for s in "LR":
            arm.pose.bones[f"brow{s}"].rotation_mode = "XYZ"
            arm.pose.bones[f"brow{s}"].rotation_euler = (-brow * 0.06, 0, 0)

    def solo(clip):
        for t in arm.animation_data.nla_tracks:
            t.mute = t.name != clip
        arm.pose.bones["laptop"].scale = (1, 1, 1) if clip == "working" else (0.001,) * 3

    look((0.8, -4, 0.55), (0, 0, 0.5), 1.15, 640, 760)
    shots = [("listening", 1), ("thinking", 1), ("waiting", 1), ("waiting", 11), ("speaking", 1), ("speaking", 17),
             ("speaking", 33), ("off", 1), ("working", 1), ("idle-look", 16), ("idle-stretch", 40), ("idle-belly", 20),
             ("idle-jump", 19), ("idle-beard", 26)]
    for clip, frame in shots:
        solo(clip)
        face(lid=1.0 if clip == "off" else 0.05, mouth=0.8 if clip == "speaking" else 0)
        scene.frame_set(frame)
        scene.render.filepath = str(SHOTS / f"pose-{clip}-{frame}.png")
        bpy.ops.render.render(write_still=True)
    solo("speaking")
    look((3.0, -2.6, 0.55), (0, 0, 0.5), 1.15, 640, 760)
    scene.frame_set(17)
    scene.render.filepath = str(SHOTS / "pose-speaking-17-side.png")
    bpy.ops.render.render(write_still=True)
    solo("waiting")
    look((-3.2, -2.4, 0.6), (0, 0, 0.55), 1.15, 640, 760)
    scene.frame_set(1)
    scene.render.filepath = str(SHOTS / "pose-waiting-back.png")
    bpy.ops.render.render(write_still=True)
    # 脸部特写：睁眼、闭眼、张嘴、抬眉、眼珠往上看
    for t in arm.animation_data.nla_tracks:
        t.mute = True
    pose({}, {})
    arm.pose.bones["laptop"].scale = (0.001,) * 3
    look((0, -4, 0.85), (0, 0, 0.85), 0.22, 600, 600)
    for name, lid, mouth, brow in (("open", 0.05, 0, 0), ("blink", 1.0, 0, 0), ("talk", 0.05, 1.0, 0), ("brow", 0.05, 0, 1.0)):
        face(lid, mouth, brow)
        scene.frame_set(1)
        scene.render.filepath = str(SHOTS / f"face-{name}.png")
        bpy.ops.render.render(write_still=True)
    face()
    for s in "LR":
        arm.pose.bones[f"eye{s}"].rotation_mode = "XYZ"
        arm.pose.bones[f"eye{s}"].rotation_euler = (0.08, 0, 0.07)
    scene.render.filepath = str(SHOTS / "face-look.png")
    bpy.ops.render.render(write_still=True)


if PREVIEW:
    preview()
    bpy.ops.wm.open_mainfile(filepath=str(DIR / "rigged.blend"))
arm = bpy.data.objects["butcher_rig"]
body = bpy.data.objects["figure"]
OUT.parent.mkdir(parents=True, exist_ok=True)
activate(body, arm)
bpy.ops.export_scene.gltf(filepath=str(OUT), export_format="GLB", use_selection=True, export_yup=True, export_skins=True,
                          export_animations=True, export_animation_mode="ACTIONS", export_force_sampling=True,
                          export_def_bones=False, export_tangents=False,
                          export_image_format="JPEG", export_jpeg_quality=88)
print(f"BUTCHER_RIG {OUT} {OUT.stat().st_size / 1e6:.2f}MB verts={len(body.data.vertices)} clips={list(CLIPS)}")
