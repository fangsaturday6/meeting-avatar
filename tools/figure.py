"""给 Codex 设计的「可爱屠夫打工人」绑骨、做每种状态的动作，导出成页面用的 GLB。

    /Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup --python tools/figure.py
    可选：-- --src 原模型.blend  --out 导出路径  --blend 另存一份绑好骨的工程文件

原模型是静态的（120 个独立部件，没有骨骼），在 ~/Documents/3D-Characters/cute-butcher/cute-butcher.blend，
由那个目录里的 build_character.py 生成。这个脚本只读它，不改它；造型要改去改那边，改完重跑这里。
导出到 avatar/web/models/figure.glb，页面刷新就换上（见 avatar/web/model.js）。和页面的约定：
- 动作片段的名字就是状态名：listening / thinking / waiting / speaking / off。
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


# ---------- 骨架 ----------
EYE = (0.31, -0.595, 5.37)       # 左眼眼白的中心；右眼 x 取负
#          名字      头                  尾                  父        参与衣服的自动权重
BONES = [("hips", (0, 0, 1.6), (0, 0, 2.2), None, True),
         ("spine", (0, 0, 2.2), (0, 0, 3.2), "hips", True),
         ("belly", (0, -0.1, 2.75), (0, -0.8, 2.75), "spine", True),
         ("chest", (0, 0, 3.2), (0, 0, 4.0), "spine", True),
         ("neck", (0, 0, 4.0), (0, 0, 4.5), "chest", False),
         ("head", (0, 0, 4.5), (0, 0, 6.1), "neck", False),
         ("jaw", (0, 1.5, 4.9), (0, -0.8, 4.6), "head", False),          # 轴放在脑后很远：张嘴时胡子基本是往下走，不往胸口里扎
         ("hook", (1.16, -0.8, 2.6), (1.5, -0.9, 1.5), "spine", False)]  # 腰间的钩子：可以晃
for _sx, _s in ((-1, "R"), (1, "L")):
    BONES += [(f"arm{_s}", (_sx * 1.35, 0, 3.95), (_sx * 1.89, -0.015, 3.31), "chest", True),
              (f"forearm{_s}", (_sx * 1.89, -0.015, 3.31), (_sx * 2.45, -0.06, 2.72), f"arm{_s}", False),
              (f"hand{_s}", (_sx * 2.45, -0.06, 2.72), (_sx * 2.6, -0.1, 2.2), f"forearm{_s}", False),
              (f"eye{_s}", (_sx * EYE[0], 0.1, EYE[2]), (_sx * EYE[0], -0.7, EYE[2]), "head", False),
              (f"lid{_s}", (_sx * EYE[0], EYE[1], EYE[2] + 0.255), (_sx * EYE[0], EYE[1], EYE[2] - 0.255), "head", False),
              (f"brow{_s}", (_sx * 0.32, 0.6, 5.70), (_sx * 0.32, -0.74, 5.72), "head", False)]
ANIMATED = ["spine", "belly", "chest", "neck", "head", "hook", "armR", "forearmR", "handR", "armL", "forearmL", "handL"]


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
                                  matrix=Matrix.Translation((sx * EYE[0], EYE[1] - 0.005, EYE[2])) @ Matrix.Diagonal((0.222, 0.14, 0.255, 1)))
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


# ---------- 动作：每种状态一个片段，名字就是状态名 ----------
def aim(*d):
    """这根骨骼指向哪（按人物的坐标说）"""
    return {"aim": Vector(d)}


def rot(x=0, y=0, z=0):
    """在原来的姿势上，绕人物的 X（点头为正）、Y（往人物右边歪为正）、Z（转向人物左边为正）转多少度"""
    return {"rot": (x, y, z)}


def pose(arm, spec, last):
    """把一组姿势（朝向和角度都按人物的坐标说）换算成每根骨骼自己的旋转"""
    world = {}
    for b in arm.data.bones:            # 骨骼按建立顺序排，父在前
        rest = b.matrix_local.to_quaternion()
        up = world[b.parent.name] @ (b.parent.matrix_local.to_quaternion().inverted() @ rest) if b.parent else rest
        s, turn = spec.get(b.name, {}), Quaternion()
        if "aim" in s:
            turn = (up @ Vector((0, 1, 0))).rotation_difference(s["aim"].normalized())
        if "rot" in s:
            turn = Euler([math.radians(a) for a in s["rot"]], "XYZ").to_quaternion() @ turn
        q = up.inverted() @ turn @ up
        if b.name in last and q.dot(last[b.name]) < 0:
            q.negate()                   # 相邻两帧别绕远路
        last[b.name] = q
        world[b.name] = up @ q
        pb = arm.pose.bones[b.name]
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = q
        pb.scale = (s.get("scale", 1),) * 3


def loop(*keys, every):
    """几个关键姿势排成一圈：最后回到第一个，首尾能接上"""
    return [(1 + i * every, k) for i, k in enumerate((*keys, keys[0]))]


IN = {"scale": 1.0}
OUT_ = {"scale": 1.035}
BEARD = {"armL": aim(0.05, -0.85, -0.5), "handL": aim(-0.7, -0.1, 0.7), "spine": rot(z=-3), "armR": rot(y=2)}
RAISE = {"armL": aim(0.8, -0.1, 0.6), "spine": rot(y=2), "chest": rot(x=-3, y=3), "armR": rot(y=3), "forearmR": rot(x=-8)}
TALK = {"armL": aim(0.55, -0.5, -0.65)}
SLUMP = {"spine": rot(x=6), "chest": rot(x=8), "neck": rot(x=8), "head": rot(x=20, z=3),
         "armR": rot(x=-10, y=4), "armL": rot(x=-10, y=-4), "forearmR": rot(x=-6), "forearmL": rot(x=-6)}
CLIPS = {
    # 在听：站着呼吸，重心轻晃，钩子跟着微微摆
    "listening": loop({"belly": IN},
                      {"belly": OUT_, "chest": rot(x=-1.5), "head": rot(x=1.2), "spine": rot(y=0.8), "hook": rot(y=2.5),
                       "armR": rot(y=1.5), "armL": rot(y=-1.5)}, every=48),
    # 在想：左手捋胡子，抬头看斜上方
    "thinking": loop({**BEARD, "forearmL": aim(-0.68, -0.30, 0.67), "head": rot(x=-9, y=5, z=12), "chest": rot(x=-3), "belly": IN},
                     {**BEARD, "forearmL": aim(-0.62, -0.32, 0.76), "head": rot(x=-11, y=6, z=14), "chest": rot(x=-3.5), "belly": OUT_},
                     {**BEARD, "forearmL": aim(-0.68, -0.30, 0.67), "head": rot(x=-8, y=4, z=10), "chest": rot(x=-3), "belly": IN},
                     {**BEARD, "forearmL": aim(-0.62, -0.32, 0.76), "head": rot(x=-10, y=6, z=13), "chest": rot(x=-3.5), "belly": OUT_}, every=24),
    # 想发言：举起左手挥，钩子晃得欢
    "waiting": loop({**RAISE, "forearmL": aim(-0.28, -0.2, 1), "handL": aim(-0.45, -0.2, 1), "head": rot(x=-5, y=-3, z=5), "hook": rot(y=-6)},
                    {**RAISE, "forearmL": aim(0.22, -0.2, 1), "handL": aim(0.45, -0.2, 1), "head": rot(x=-6, y=-2, z=7), "hook": rot(y=7)}, every=12),
    # 在说：左手在身前比划，身体跟着话起伏
    "speaking": loop({**TALK, "forearmL": aim(-0.10, -1, 0.30), "handL": aim(0, -1, 0.5), "chest": rot(x=-1.5, z=-2), "armR": rot(x=-4), "belly": IN},
                     {**TALK, "armL": aim(0.65, -0.4, -0.65), "forearmL": aim(0.30, -1, 0.10), "handL": aim(0.3, -1, 0.2), "chest": rot(x=1, z=2),
                      "head": rot(x=2, z=-3), "armR": rot(x=-8), "forearmR": rot(x=-12), "belly": OUT_, "hook": rot(y=3)},
                     {**TALK, "forearmL": aim(-0.20, -1, 0.45), "handL": aim(-0.1, -1, 0.7), "chest": rot(x=-2, z=-1), "head": rot(x=-2, z=2),
                      "armR": rot(x=-4), "belly": IN},
                     {**TALK, "armL": aim(0.65, -0.4, -0.65), "forearmL": aim(0.25, -1, 0.05), "handL": aim(0.3, -1, 0.1), "chest": rot(x=0.5, z=2),
                      "head": rot(x=1, z=-2), "armR": rot(x=-7), "forearmR": rot(x=-10), "belly": OUT_, "hook": rot(y=-3)}, every=18),
    # 未开会：垂头弓背，慢慢呼吸
    "off": loop({**SLUMP, "belly": IN}, {**SLUMP, "chest": rot(x=9.5), "belly": OUT_}, every=72),
}


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
            arm.pose.bones["belly"].keyframe_insert("scale", frame=frame)
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
    parts += add_lids(character, bpy.data.materials["Warm peach skin"])
    arm = build_rig()
    figure = bind(arm, parts)
    activate(figure, *[o for o, _ in parts])
    bpy.ops.object.join()
    figure.name = figure.data.name = "figure"
    animate(arm)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if BLEND:
        bpy.ops.wm.save_as_mainfile(filepath=str(BLEND))
    activate(figure, arm)
    bpy.ops.export_scene.gltf(filepath=str(OUT), export_format="GLB", use_selection=True, export_yup=True, export_skins=True,
                              export_animations=True, export_animation_mode="ACTIONS", export_force_sampling=True,
                              export_def_bones=False, export_tangents=False)
    used = sorted({how for _, how in parts})
    print(f"FIGURE {OUT} {OUT.stat().st_size / 1e6:.1f}MB verts={len(figure.data.vertices)} clips={list(CLIPS)} binds={used}")


main()
