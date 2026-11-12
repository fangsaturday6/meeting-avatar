"""给 Codex 设计的「可爱屠夫打工人」绑骨、做每种状态的动作，导出成页面用的 GLB。

    /Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup --python tools/figure.py
    可选：-- --src 原模型.blend  --out 导出路径  --blend 另存一份绑好骨的工程文件

原模型是静态的（120 个独立部件，没有骨骼），在 ~/Documents/3D-Characters/cute-butcher/cute-butcher.blend，
由那个目录里的 build_character.py 生成。这个脚本只读它，不改它；造型要改去改那边，改完重跑这里。
原模型是照着正面做的，前后很薄，转到侧面像块板。所以这里先把身体和头前后撑厚（见「加厚」一节），
再把缝隙和被挡住的地方的阴影烘进顶点色，转着看才有体积。
导出到 avatar/web/models/figure.glb，页面刷新就换上（见 avatar/web/model.js）。和页面的约定：
- 动作片段：名字是状态名的（listening / thinking / waiting / speaking / off）在那种状态循环播；working 在后台干活时播（敲电脑）；
  idle-* 是站着时自己做的小动作（扶耳机、跳、甩钩子、伸懒腰、拍肚子、张望），页面隔几秒挑一个播一遍。
- 骨骼 laptop 带着笔记本电脑，平时由页面缩到看不见，干活时放出来。
- 脸上的骨骼由页面按实时数据驱动，这里不做动作：jaw（下巴，带着整把胡子）、lidL / lidR（眼皮，沿骨骼方向压扁 = 睁眼）、
  browL / browR（眉毛）、eyeL / eyeR（眼珠）。
- 名字以 glow 开头的材质由页面叠上状态色（耳机两侧的标志）。
坐标：Z 朝上，人物面朝 -Y，人物的左手在 +X，站高约 6.4。
"""
import math
import sys
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Euler, Matrix, Quaternion, Vector
from mathutils.bvhtree import BVHTree

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def arg(name, default):
    return Path(ARGS[ARGS.index(name) + 1]) if name in ARGS else default


SRC = arg("--src", Path.home() / "Documents/3D-Characters/cute-butcher/cute-butcher.blend")
OUT = arg("--out", Path(__file__).resolve().parent.parent / "avatar/web/models/figure.glb")
BLEND = arg("--blend", None)


def activate(obj, *others):
    bpy.ops.object.select_all(action="DESELECT")
    for o in (obj, *others):
        o.select_set(True)
    bpy.context.view_layer.objects.active = obj


# ---------- 加厚：前后方向（Y）撑开 ----------
HEAD_DEPTH = 1.25                # 头和脸上的东西整体前后放大多少
LEG_DEPTH = 1.18                 # 腿和鞋：身体厚了，腿也跟着粗一点
HEAD_PARTS = ("head", "jaw", "neck", "eyeL", "eyeR", "browL", "browR")
ANCHORS = {"hook": Vector((1.16, -0.8, 2.6)), "chest": Vector((-0.8, 0.55, 4.3))}    # 钩子、背上的刀：不变形，跟着挂它的地方平移


def smooth(a, b, x):
    t = min(1.0, max(0.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def body_depth(p):
    """身体上这个位置前后撑开多少倍：肚子最多（前面比后背鼓），往肩膀和袖子收回 1，胳膊不动"""
    side = 1 - smooth(1.0, 1.75, abs(p.x))
    height = 1 - 0.55 * smooth(3.2, 4.4, p.z)
    return 1 + (0.55 if p.y < 0 else 0.42) * side * height


def fatten(parts):
    for obj, how in parts:
        m, inv = obj.matrix_world, obj.matrix_world.inverted()
        shift = ANCHORS[how].y * (body_depth(ANCHORS[how]) - 1) if how in ANCHORS else 0
        if not (how in ("auto", "skin", "hips") or how in HEAD_PARTS or shift):
            continue
        for v in obj.data.vertices:
            w = m @ v.co
            if how in HEAD_PARTS:
                w.y *= HEAD_DEPTH
            elif how == "hips":
                w.y *= LEG_DEPTH
            elif shift:
                w.y += shift
            else:
                w.y *= body_depth(w)
            v.co = inv @ w
        obj.data.update()


# ---------- 骨架 ----------
EYE = (0.31, -0.595 * HEAD_DEPTH, 5.37)       # 左眼眼白的中心（加厚之后）；右眼 x 取负
HOOK_Y = ANCHORS["hook"].y * body_depth(ANCHORS["hook"])
#          名字      头                  尾                  父        参与衣服的自动权重
BONES = [("hips", (0, 0, 1.6), (0, 0, 2.2), None, True),
         ("spine", (0, 0, 2.2), (0, 0, 3.2), "hips", True),
         ("belly", (0, -0.1, 2.75), (0, -1.2, 2.75), "spine", True),
         ("chest", (0, 0, 3.2), (0, 0, 4.0), "spine", True),
         ("neck", (0, 0, 4.0), (0, 0, 4.5), "chest", False),
         ("head", (0, 0, 4.5), (0, 0, 6.1), "neck", False),
         ("jaw", (0, 1.5, 4.9), (0, -1.0, 4.6), "head", False)]          # 轴放在脑后很远：张嘴时胡子基本是往下走，不往胸口里扎
for _sx, _s in ((-1, "R"), (1, "L")):
    BONES += [(f"arm{_s}", (_sx * 1.35, 0, 3.95), (_sx * 1.89, -0.015, 3.31), "chest", True),
              (f"forearm{_s}", (_sx * 1.89, -0.015, 3.31), (_sx * 2.45, -0.06, 2.72), f"arm{_s}", False),
              (f"hand{_s}", (_sx * 2.45, -0.06, 2.72), (_sx * 2.6, -0.1, 2.2), f"forearm{_s}", False),
              (f"eye{_s}", (_sx * EYE[0], 0.1, EYE[2]), (_sx * EYE[0], -0.9, EYE[2]), "head", False),
              (f"lid{_s}", (_sx * EYE[0], EYE[1], EYE[2] + 0.255), (_sx * EYE[0], EYE[1], EYE[2] - 0.255), "head", False),
              (f"brow{_s}", (_sx * 0.32, 0.6, 5.70), (_sx * 0.32, -0.92, 5.72), "head", False)]
# 道具排在最后：摆姿势时要先算出手在哪，才能把道具放到手里
LAPTOP = Vector((0.1, -2.0, 3.0))        # 笔记本电脑底座的中心：肚子前面，屏幕顶到胡子底下，不挡脸
BONES += [("hook", (1.16, HOOK_Y, 2.6), (1.5, HOOK_Y - 0.1, 1.5), "spine", False),          # 腰间的钩子：会晃，能摘下来甩出去
          ("laptop", tuple(LAPTOP), tuple(LAPTOP + Vector((0, -0.5, 0))), "spine", False)]  # 电脑：平时收着，页面在干活时把它放大出来
ANIMATED = ["hips", "spine", "belly", "chest", "neck", "head", "armR", "forearmR", "handR", "armL", "forearmL", "handL", "hook"]


def bone_for(name, x):
    """这个部件跟哪根骨骼走。'auto' = 按自动权重变形（衬衫）；'skin' = 贴在衬衫上，跟着衬衫表面走"""
    side, words = ("L" if x > 0 else "R"), name.lower().replace(".", " ").split()
    has = lambda *ws: any(w in words for w in ws)      # noqa: E731
    if has("iris", "sparkle"):
        return f"eye{side}"
    if has("eyebrow"):
        return f"brow{side}"
    if has("beard"):
        return "jaw"
    if has("head", "headband", "ear", "nose", "cheek", "eye", "moustache", "smile", "beats"):
        return "head"
    if has("neck"):
        return "neck"
    if has("forearm", "elbow"):
        return f"forearm{side}"
    if has("palm", "finger", "fingertip", "thumb"):
        return f"hand{side}"
    if has("sleeve"):
        return f"arm{side}"
    if has("hook"):
        return "hook"
    if has("cleaver"):
        return "chest"
    if has("trouser", "boot", "shoe"):
        return "hips"
    return "auto" if has("torso") else "skin"


def build_rig():
    arm = bpy.data.objects.new("butcher", bpy.data.armatures.new("butcher"))
    bpy.context.scene.collection.objects.link(arm)
    activate(arm)
    bpy.ops.object.mode_set(mode="EDIT")
    for name, head, tail, parent, auto in BONES:
        b = arm.data.edit_bones.new(name)
        b.head, b.tail, b.roll, b.use_deform = head, tail, 0, auto
        if parent:
            b.parent = arm.data.edit_bones[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    return arm


def add_lids(character, skin):
    """原模型没有眼皮：给每只眼加一片皮肤色的罩子，页面把它压扁就是睁眼，放开就是闭眼"""
    lids = []
    for sx, side in ((-1, "R"), (1, "L")):
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=28, v_segments=16, radius=1,
                                  matrix=Matrix.Translation((sx * EYE[0], EYE[1] - 0.005, EYE[2])) @ Matrix.Diagonal((0.222, 0.14 * HEAD_DEPTH, 0.255, 1)))
        me = bpy.data.meshes.new(f"Eyelid {side}")
        bm.to_mesh(me)
        bm.free()
        for p in me.polygons:
            p.use_smooth = True
        me.materials.append(skin)
        lid = bpy.data.objects.new(f"Eyelid {side}", me)
        character.objects.link(lid)
        lids.append((lid, f"lid{side}"))
    return lids


def add_bow(character, leather):
    """后腰上围裙带子打的结：背面不至于光秃秃一片"""
    back = Vector((0, 0.94, 2.53))
    back.y *= body_depth(back)                               # 腰带在后腰的位置（加厚之后）
    bm = bmesh.new()
    for loc, rot, size in [((0, 0.06, 0), (0, 0, 0), (0.10, 0.09, 0.10)),                  # 结
                           ((-0.27, 0.03, 0.03), (0, 18, 0), (0.25, 0.075, 0.14)),          # 两个圈
                           ((0.27, 0.03, 0.03), (0, -18, 0), (0.25, 0.075, 0.14)),
                           ((-0.13, 0.02, -0.33), (0, -14, 0), (0.075, 0.05, 0.30)),        # 垂下来的两条带子
                           ((0.13, 0.02, -0.33), (0, 14, 0), (0.075, 0.05, 0.30))]:
        turn = Euler([math.radians(a) for a in rot]).to_matrix().to_4x4()
        bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=12, radius=1,
                                  matrix=Matrix.Translation(back + Vector(loc)) @ turn @ Matrix.Diagonal((*size, 1)))
    me = bpy.data.meshes.new("Apron bow")
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    me.materials.append(leather)
    bow = bpy.data.objects.new("Apron bow", me)
    character.objects.link(bow)
    return [(bow, "skin")]


def add_laptop(character):
    """后台在干活时用的笔记本电脑。盖子上的标志和屏幕是状态色（材质名以 glow 开头）"""
    glow = bpy.data.materials.new("glow laptop")
    glow.use_nodes = True
    glow.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.06, 0.07, 0.08, 1)
    body = bpy.data.materials.new("Laptop shell")
    body.use_nodes = True
    shell = body.node_tree.nodes["Principled BSDF"]
    shell.inputs["Base Color"].default_value = (0.10, 0.11, 0.13, 1)
    shell.inputs["Metallic"].default_value, shell.inputs["Roughness"].default_value = 0.6, 0.38
    out = []
    lean = Euler((math.radians(-16), 0, 0)).to_matrix().to_4x4()                 # 屏幕从后沿立起来，往外仰一点
    hinge = Matrix.Translation(LAPTOP + Vector((0, -0.5, 0.03)))
    for name, material, matrix in [
            ("Laptop base", body, Matrix.Translation(LAPTOP) @ Matrix.Diagonal((1.5, 1.0, 0.07, 1))),
            ("Laptop lid", body, hinge @ lean @ Matrix.Translation((0, 0, 0.5)) @ Matrix.Diagonal((1.5, 0.05, 1.0, 1))),
            ("Laptop screen", glow, hinge @ lean @ Matrix.Translation((0, 0.03, 0.5)) @ Matrix.Diagonal((1.36, 0.01, 0.86, 1))),
            ("Laptop logo", glow, hinge @ lean @ Matrix.Translation((0, -0.03, 0.52)) @ Matrix.Diagonal((0.26, 0.012, 0.26, 1)))]:
        bm = bmesh.new()
        if "logo" in name:
            bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=10, radius=0.5, matrix=matrix)
        else:
            made = bmesh.ops.create_cube(bm, size=1, matrix=matrix)
            bmesh.ops.bevel(bm, geom=list({e for v in made["verts"] for e in v.link_edges}), offset=0.02, segments=2, affect="EDGES")
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        for p in me.polygons:
            p.use_smooth = "logo" in name
        me.materials.append(material)
        obj = bpy.data.objects.new(name, me)
        character.objects.link(obj)
        out.append((obj, "laptop"))
    return out


def bind(arm, parts):
    """parts：[(物体, 绑法)]。衬衫用自动权重，贴在衬衫上的抄最近衬衫顶点的权重，其余整块跟一根骨骼"""
    torso = next(o for o, how in parts if how == "auto")
    activate(arm, torso)
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    for b in arm.data.bones:
        b.use_deform = True
    groups = [g.name for g in torso.vertex_groups]
    co = np.array([torso.matrix_world @ v.co for v in torso.data.vertices])
    weights = [[(groups[g.group], g.weight) for g in v.groups] for v in torso.data.vertices]
    for obj, how in parts:
        if how == "auto":
            continue
        if how == "skin":
            for v in obj.data.vertices:
                near = int(np.argmin(((co - np.array(obj.matrix_world @ v.co)) ** 2).sum(axis=1)))
                for name, w in weights[near]:
                    (obj.vertex_groups.get(name) or obj.vertex_groups.new(name=name)).add([v.index], w, "REPLACE")
        else:
            obj.vertex_groups.new(name=how).add(range(len(obj.data.vertices)), 1.0, "REPLACE")
    return torso


# ---------- 阴影：烘进顶点色 ----------
def shade(obj, rays=16, reach=1.5):
    """每个顶点往外打一把射线，被挡得越多越暗（缝隙、腋下、胡子底下、腰带上沿、脚底）。
    材质是纯色的，页面上颜色 = 材质色 × 这个顶点色"""
    me = obj.data
    n = len(me.vertices)
    co, nrm = np.empty(n * 3), np.empty(n * 3)
    me.vertices.foreach_get("co", co)
    me.vertices.foreach_get("normal", nrm)
    co, nrm = co.reshape(-1, 3), nrm.reshape(-1, 3)
    away = {i for i, slot in enumerate(obj.vertex_groups) if slot.name.startswith("lid") or slot.name == "laptop"}
    hidden = {v.index for v in me.vertices if any(g.group in away for g in v.groups)}      # 眼皮此刻合着、电脑平时不在：别让它们把身上挡黑
    faces = [tuple(p.vertices) for p in me.polygons if p.vertices[0] not in hidden]
    floor = len(co)
    points = [tuple(c) for c in co] + [(-9, -9, 0), (9, -9, 0), (9, 9, 0), (-9, 9, 0)]     # 加一块地面：脚底和裤脚会暗下去
    tree = BVHTree.FromPolygons(points, faces + [(floor, floor + 1, floor + 2, floor + 3)])
    dirs = np.random.default_rng(7).normal(size=(rays, 3))
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    light = np.ones(n)
    for i in range(n):
        normal, origin, blocked = nrm[i], Vector(co[i] + nrm[i] * 0.02), 0.0
        for d in dirs:
            if d @ normal < 0:
                d = -d
            hit = tree.ray_cast(origin, Vector(d), reach)
            if hit[0] is not None:
                blocked += (1 - hit[3] / reach) * float(d @ normal)
        light[i] = max(0.0, 1 - blocked / (rays * 0.3))
    light = light ** 1.2
    dark = np.array([0.36, 0.31, 0.35])                      # 阴影不是纯黑，带一点暖
    rgb = dark + (1 - dark) * light[:, None]
    layer = me.color_attributes.new("Shade", "FLOAT_COLOR", "POINT")
    layer.data.foreach_set("color", np.concatenate([rgb, np.ones((n, 1))], axis=1).reshape(-1))
    me.color_attributes.active_color = layer
    me.color_attributes.render_color_index = me.color_attributes.find("Shade")


# ---------- 动作 ----------
# 三类片段：状态（名字就是状态名，循环）、working（后台在干活：敲电脑，循环）、idle-*（站着时自己做的小动作，播一遍）
def aim(*d):
    """这根骨骼指向哪（按人物的坐标说）"""
    return {"aim": Vector(d)}


def rot(x=0, y=0, z=0):
    """在原来的姿势上，绕人物的 X（点头为正）、Y（往人物右边歪为正）、Z（转向人物左边为正）转多少度"""
    return {"rot": (x, y, z)}


def pose(arm, spec, last):
    """把一组姿势换算成每根骨骼自己的旋转、位移、缩放。姿势都按人物的坐标说：
    aim 指向哪、rot 转多少度、move 挪多少、grab 抓在哪根骨骼的末端（道具用）、
    squash 上下压扁或拉长（脚不离地）、scale 整体放大"""
    world, head, tip = {}, {}, {}
    for b in arm.data.bones:            # 骨骼按建立顺序排，父在前
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
            turn = Euler([math.radians(a) for a in s["rot"]], "XYZ").to_quaternion() @ turn
        q = up.inverted() @ turn @ up
        if b.name in last and q.dot(last[b.name]) < 0:
            q.negate()                   # 相邻两帧别绕远路
        last[b.name] = q
        move, squash = Vector(s.get("move", (0, 0, 0))), s.get("squash", 1)
        if "grab" in s:
            move = tip[s["grab"]] + Vector(s.get("offset", (0, 0, 0))) - origin
        move.z -= b.head_local.z * (1 - squash)
        world[b.name] = up @ q
        head[b.name] = origin + move
        tip[b.name] = head[b.name] + world[b.name] @ (rest.inverted() @ (b.tail_local - b.head_local))
        pb = arm.pose.bones[b.name]
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = q
        pb.location = up.inverted() @ move
        wide = s.get("scale", 1) / math.sqrt(squash)
        pb.scale = (wide, s.get("scale", 1) * squash, wide)


def loop(*keys, every):
    """几个关键姿势排成一圈：最后回到第一个，首尾能接上"""
    return [(1 + i * every, k) for i, k in enumerate((*keys, keys[0]))]


def mix(*specs):
    out = {}
    for s in specs:
        for bone, v in s.items():
            out[bone] = {**out.get(bone, {}), **v}
    return out


IN, OUT_ = {"scale": 1.0}, {"scale": 1.04}
# 放松站着：胳膊比原模型的 A 字站姿垂下来一些，小臂微微往前
RELAX = {"armR": rot(y=-12), "armL": rot(y=12), "forearmR": rot(x=-14), "forearmL": rot(x=-14)}
BEARD = {**RELAX, "armL": aim(0.05, -0.85, -0.5), "handL": aim(-0.7, -0.1, 0.7), "spine": rot(z=-3)}
RAISE = {**RELAX, "armL": aim(0.8, -0.1, 0.6), "spine": rot(y=2), "chest": rot(x=-3, y=3), "forearmR": rot(x=-22)}
TALK = {**RELAX, "armL": aim(0.55, -0.5, -0.65)}
SLUMP = {"spine": rot(x=6), "chest": rot(x=8), "neck": rot(x=8), "head": rot(x=20, z=3),
         "armR": rot(x=-10, y=-10), "armL": rot(x=-10, y=10), "forearmR": rot(x=-6), "forearmL": rot(x=-6)}
TYPE = {"armL": aim(0.35, -0.65, -0.8), "forearmL": aim(-0.5, -0.85, -0.25), "handL": aim(-0.7, -0.7, -0.1),
        "armR": aim(-0.38, -0.62, -0.75), "chest": rot(x=4), "spine": rot(x=2)}
EAR = {**RELAX, "armR": aim(-0.75, -0.25, 0.2), "forearmR": aim(0.62, 0.10, 0.78), "handR": aim(0.6, 0.2, 0.75)}
REACH = {**RELAX, "armL": aim(0.45, -0.55, -0.7), "forearmL": aim(-0.475, -0.617, -0.633), "handL": aim(-0.4, -0.6, -0.6)}
PAT = {**RELAX, "armL": aim(0.4, -0.7, -0.6), "handL": aim(-0.8, -0.5, -0.3)}
UP = {"armL": aim(0.6, 0.1, 0.8), "forearmL": aim(0.25, 0.1, 1), "handL": aim(0.3, 0, 1),
      "armR": aim(-0.6, 0.1, 0.8), "forearmR": aim(-0.25, 0.1, 1), "handR": aim(-0.3, 0, 1)}
CLIPS = {
    # 在听：站着呼吸，重心左右换，钩子跟着微微摆
    "listening": loop(mix(RELAX, {"belly": IN}),
                      mix(RELAX, {"belly": OUT_, "chest": rot(x=-1.5, z=2), "head": rot(x=1, z=-3), "spine": rot(y=1.4), "hook": rot(y=3),
                                  "armR": rot(y=-9), "armL": rot(y=15)}),
                      mix(RELAX, {"belly": IN, "chest": rot(z=-1), "head": rot(z=2), "forearmL": rot(x=-20)}),
                      mix(RELAX, {"belly": OUT_, "chest": rot(x=-1.5, z=-2), "head": rot(x=1, z=3), "spine": rot(y=-1.4), "hook": rot(y=-3),
                                  "armR": rot(y=-15), "armL": rot(y=9)}), every=30),
    # 在想：左手捋胡子，抬头看斜上方
    "thinking": loop({**BEARD, "forearmL": aim(-0.68, -0.30, 0.67), "head": rot(x=-9, y=5, z=12), "chest": rot(x=-3), "belly": IN},
                     {**BEARD, "forearmL": aim(-0.62, -0.32, 0.76), "head": rot(x=-11, y=6, z=14), "chest": rot(x=-3.5), "belly": OUT_},
                     {**BEARD, "forearmL": aim(-0.68, -0.30, 0.67), "head": rot(x=-8, y=4, z=10), "chest": rot(x=-3), "belly": IN},
                     {**BEARD, "forearmL": aim(-0.62, -0.32, 0.76), "head": rot(x=-10, y=6, z=13), "chest": rot(x=-3.5), "belly": OUT_}, every=24),
    # 想发言：举起左手挥，身子一颠一颠，钩子晃得欢
    "waiting": loop({**RAISE, "forearmL": aim(-0.28, -0.2, 1), "handL": aim(-0.45, -0.2, 1), "head": rot(x=-5, y=-3, z=5), "hook": rot(y=-8),
                     "hips": {"squash": 0.95}},
                    {**RAISE, "forearmL": aim(0.22, -0.2, 1), "handL": aim(0.45, -0.2, 1), "head": rot(x=-6, y=-2, z=7), "hook": rot(y=9),
                     "hips": {"squash": 1.04, "move": (0, 0, 0.22)}}, every=10),
    # 在说：左手在身前比划，身体跟着话起伏
    "speaking": loop({**TALK, "forearmL": aim(-0.10, -1, 0.30), "handL": aim(0, -1, 0.5), "chest": rot(x=-1.5, z=-2), "armR": rot(x=-4, y=-12), "belly": IN},
                     {**TALK, "armL": aim(0.65, -0.4, -0.65), "forearmL": aim(0.30, -1, 0.10), "handL": aim(0.3, -1, 0.2), "chest": rot(x=1, z=2),
                      "head": rot(x=2, z=-3), "armR": rot(x=-10, y=-16), "forearmR": rot(x=-30), "belly": OUT_, "hook": rot(y=3)},
                     {**TALK, "forearmL": aim(-0.20, -1, 0.45), "handL": aim(-0.1, -1, 0.7), "chest": rot(x=-2, z=-1), "head": rot(x=-2, z=2),
                      "armR": rot(x=-4, y=-12), "belly": IN},
                     {**TALK, "armL": aim(0.65, -0.4, -0.65), "forearmL": aim(0.25, -1, 0.05), "handL": aim(0.3, -1, 0.1), "chest": rot(x=0.5, z=2),
                      "head": rot(x=1, z=-2), "armR": rot(x=-8, y=-18), "forearmR": rot(x=-26), "belly": OUT_, "hook": rot(y=-3)}, every=18),
    # 未开会：垂头弓背，慢慢呼吸
    "off": loop({**SLUMP, "belly": IN}, {**SLUMP, "chest": rot(x=9.5), "belly": OUT_}, every=72),
    # 后台在干活：左臂托着电脑，右手敲键盘，偶尔抬头看一眼
    "working": loop({**TYPE, "forearmR": aim(0.50, -0.85, -0.15), "handR": aim(0.6, -0.8, -0.35), "head": rot(x=15, z=2), "belly": IN},
                    {**TYPE, "forearmR": aim(0.42, -0.90, -0.24), "handR": aim(0.5, -0.8, -0.5), "head": rot(x=16, z=-2), "belly": OUT_},
                    {**TYPE, "forearmR": aim(0.56, -0.82, -0.12), "handR": aim(0.7, -0.8, -0.3), "head": rot(x=14, z=3), "belly": IN},
                    {**TYPE, "forearmR": aim(0.44, -0.88, -0.22), "handR": aim(0.55, -0.8, -0.5), "head": rot(x=3, z=-4), "chest": rot(x=1), "belly": OUT_},
                    {**TYPE, "forearmR": aim(0.52, -0.84, -0.14), "handR": aim(0.6, -0.8, -0.3), "head": rot(x=15, z=1), "belly": IN},
                    {**TYPE, "forearmR": aim(0.42, -0.90, -0.24), "handR": aim(0.5, -0.8, -0.5), "head": rot(x=16, z=-1), "belly": OUT_}, every=8),

    # ---- 站着时自己做的小动作：从放松站姿开始，回到放松站姿 ----
    # 扶一下耳机，跟着节奏点头
    "idle-headphones": [(1, RELAX), (14, {**EAR, "head": rot(x=4, y=-8, z=-6)}), (24, {**EAR, "head": rot(x=10, y=-8, z=-6), "hips": {"squash": 0.97}}),
                        (32, {**EAR, "head": rot(x=0, y=-7, z=-5)}), (40, {**EAR, "head": rot(x=10, y=-8, z=-6), "hips": {"squash": 0.97}}),
                        (48, {**EAR, "head": rot(x=0, y=-7, z=-5)}), (56, {**EAR, "head": rot(x=10, y=-8, z=-6), "hips": {"squash": 0.97}}),
                        (64, {**EAR, "head": rot(x=2, y=-6, z=-4)}), (80, RELAX)],
    # 原地跳一下：下蹲、腾空举手、落地回弹
    "idle-jump": [(1, RELAX), (8, mix(RELAX, {"hips": {"squash": 0.84}, "armL": rot(x=22, y=6), "armR": rot(x=22, y=-6), "head": rot(x=6)})),
                  (13, {**UP, "hips": {"squash": 1.10, "move": (0, 0, 0.55)}, "head": rot(x=-6)}),
                  (19, {**UP, "hips": {"squash": 1.0, "move": (0, 0, 1.15)}, "head": rot(x=-8), "hook": rot(y=14), "belly": {"scale": 0.97}}),
                  (25, mix(RELAX, {"hips": {"squash": 1.06, "move": (0, 0, 0.45)}, "armL": rot(y=-14), "armR": rot(y=14), "hook": rot(y=-10)})),
                  (29, mix(RELAX, {"hips": {"squash": 0.82}, "belly": {"scale": 1.08}, "head": rot(x=8), "hook": rot(y=8)})),
                  (35, mix(RELAX, {"hips": {"squash": 1.04}, "belly": {"scale": 0.98}})), (44, RELAX)],
    # 把腰间的钩子摘下来甩出去，飞一圈再接住挂回去
    "idle-hook": [(1, RELAX), (12, REACH),
                  (22, {**RELAX, "armL": aim(0.75, 0.15, 0.45), "forearmL": aim(0.2, 0.5, 0.85), "handL": aim(0.1, 0.6, 0.8), "chest": rot(z=12), "spine": rot(z=6),
                        "head": rot(z=-6), "hook": {"grab": "handL", **rot(x=150)}, "hips": {"squash": 0.95}}),
                  (29, {**RELAX, "armL": aim(0.2, -0.95, 0.15), "forearmL": aim(0.0, -1, 0.1), "handL": aim(0, -1, 0), "chest": rot(x=6, z=-12), "spine": rot(z=-5),
                        "hook": {"grab": "handL", **rot(x=60)}, "hips": {"squash": 1.03}}),
                  (36, {**RELAX, "armL": aim(0.25, -0.8, -0.3), "forearmL": aim(-0.1, -1, -0.1), "handL": aim(0, -1, -0.2), "chest": rot(x=5, z=-10),
                        "hook": {"move": (-0.2, -3.6, 1.9), **rot(x=220)}}),
                  (46, {**RELAX, "armL": aim(0.25, -0.8, -0.3), "forearmL": aim(-0.1, -1, -0.1), "handL": aim(0, -1, -0.2), "chest": rot(x=3, z=-6), "head": rot(x=-4),
                        "hook": {"move": (-0.9, -6.6, 1.5), **rot(x=380)}}),
                  (56, {**RELAX, "armL": aim(0.3, -0.9, 0.0), "forearmL": aim(0.0, -0.95, 0.3), "handL": aim(0, -1, 0.3), "chest": rot(x=1, z=-3),
                        "hook": {"move": (-0.5, -3.4, 1.4), **rot(x=540)}}),
                  (64, {**RELAX, "armL": aim(0.3, -0.9, 0.05), "forearmL": aim(0.0, -0.95, 0.35), "handL": aim(0, -1, 0.3), "chest": rot(x=-4), "head": rot(x=-3),
                        "hook": {"grab": "handL", **rot(x=700)}, "belly": {"scale": 1.07}, "hips": {"squash": 0.96}}),
                  (78, {**REACH, "hook": rot(x=720)}), (92, {**RELAX, "hook": rot(x=720)})],
    # 伸个懒腰：两手举过头，往后仰
    "idle-stretch": [(1, RELAX), (22, {**UP, "chest": rot(x=-9), "head": rot(x=-14), "belly": {"scale": 1.08}, "hips": {"squash": 1.04}}),
                     (40, {**UP, "armL": aim(0.75, 0.15, 0.7), "armR": aim(-0.75, 0.15, 0.7), "chest": rot(x=-11, z=3), "head": rot(x=-16, z=4),
                           "belly": {"scale": 1.10}, "hips": {"squash": 1.05}}),
                     (58, mix(RELAX, {"armL": rot(y=-25), "armR": rot(y=25), "chest": rot(x=3), "belly": {"scale": 0.98}})), (76, RELAX)],
    # 低头拍拍肚子
    "idle-belly": [(1, RELAX), (14, {**PAT, "forearmL": aim(-0.70, -0.62, -0.30), "head": rot(x=12, z=5)}),
                   (20, {**PAT, "forearmL": aim(-0.78, -0.50, -0.36), "head": rot(x=13, z=5), "belly": {"scale": 1.08}}),
                   (26, {**PAT, "forearmL": aim(-0.70, -0.62, -0.30), "head": rot(x=12, z=5), "belly": {"scale": 0.98}}),
                   (32, {**PAT, "forearmL": aim(-0.78, -0.50, -0.36), "head": rot(x=13, z=5), "belly": {"scale": 1.08}}),
                   (38, {**PAT, "forearmL": aim(-0.70, -0.62, -0.30), "head": rot(x=10, z=4), "belly": {"scale": 0.98}}),
                   (44, {**PAT, "forearmL": aim(-0.78, -0.50, -0.36), "head": rot(x=2, z=0), "belly": {"scale": 1.07}}), (62, RELAX)],
    # 左看看、右看看
    "idle-look": [(1, RELAX), (16, mix(RELAX, {"chest": rot(z=14), "head": rot(x=-4, z=24), "spine": rot(z=6, y=1.5), "armR": rot(x=-10)})),
                  (32, mix(RELAX, {"chest": rot(z=15), "head": rot(x=-2, z=26), "spine": rot(z=6, y=1.5), "armR": rot(x=-10)})),
                  (50, mix(RELAX, {"chest": rot(z=-14), "head": rot(x=-4, z=-24), "spine": rot(z=-6, y=-1.5), "armL": rot(x=-10)})),
                  (66, mix(RELAX, {"chest": rot(z=-15), "head": rot(x=-2, z=-26), "spine": rot(z=-6, y=-1.5), "armL": rot(x=-10)})), (82, RELAX)],
}
MOVED, SCALED = ["hips", "hook"], ["belly", "hips"]          # 除了转动，还会挪位置、会缩放的骨骼


def animate(arm):
    arm.animation_data_create()
    for name, keys in CLIPS.items():
        act = bpy.data.actions.new(name)
        arm.animation_data.action = act
        if hasattr(act, "slots") and not act.slots:          # 4.4 起动作要有“槽”才能挂到物体上
            arm.animation_data.action_slot = act.slots.new(id_type="OBJECT", name=arm.name)
        last = {}
        for frame, spec in keys:
            pose(arm, spec, last)
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
    pose(arm, {}, {})


# ---------- 总装 ----------
def main():
    bpy.ops.wm.open_mainfile(filepath=str(SRC))
    character = next(c for c in bpy.data.collections if c.name.startswith("CHARACTER"))
    meshes = [o for o in character.objects if o.type == "MESH"]
    activate(meshes[0], *meshes)
    bpy.ops.object.convert(target="MESH")                    # 把倒角、加厚这些修改器定下来
    for o in meshes:
        if o.data.users > 1:
            o.data = o.data.copy()

    for o in meshes:                                         # 耳机两侧的标志：页面给它叠上状态色
        if "emblem" in o.name.lower():
            glow = bpy.data.materials.get("glow emblem") or o.data.materials[0].copy()
            glow.name = "glow emblem"
            o.data.materials[0] = glow

    parts = [(o, bone_for(o.name, sum((o.matrix_world @ Vector(c)).x for c in o.bound_box) / 8)) for o in meshes]
    fatten(parts)
    parts += add_lids(character, bpy.data.materials["Warm peach skin"])
    parts += add_bow(character, bpy.data.materials["Cognac apron"])
    parts += add_laptop(character)
    arm = build_rig()
    figure = bind(arm, parts)
    activate(figure, *[o for o, _ in parts])
    bpy.ops.object.join()
    figure.name = figure.data.name = "figure"
    shade(figure)
    animate(arm)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if BLEND:
        bpy.ops.wm.save_as_mainfile(filepath=str(BLEND))
    activate(figure, arm)
    bpy.ops.export_scene.gltf(filepath=str(OUT), export_format="GLB", use_selection=True, export_yup=True, export_skins=True,
                              export_animations=True, export_animation_mode="ACTIONS", export_force_sampling=True,
                              export_def_bones=False, export_tangents=False, export_vertex_color="ACTIVE")
    used = sorted({how for _, how in parts})
    print(f"FIGURE {OUT} {OUT.stat().st_size / 1e6:.1f}MB verts={len(figure.data.vertices)} clips={list(CLIPS)} binds={used}")


main()
