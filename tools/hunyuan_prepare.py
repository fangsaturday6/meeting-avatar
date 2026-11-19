"""把混元 3D 网页版生成的屠夫（一整块网格，150 万三角面）整理成 tools/figure_hunyuan.py 能绑骨的样子。

    /Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup --python tools/hunyuan_prepare.py
    可选：-- --dir 目录（默认 ~/Documents/3D-Characters/butcher-hunyuan，里面要有 source.glb）  --tris 三角面数（默认 9 万）

做法照小刘那次（~/Documents/3D-Characters/xiaoliu-hunyuan/prepare_hy.py，坑都记在那边的 README）：
摆正 → 合并重合的点后减面（保留原 UV 和原贴图）→ 只留底色贴图，缩到 2048。
- 导入时一定要 merge_vertices：glTF 里每块 UV 碎片是单独的一块，不合并就减面，碎片之间裂缝，透出后面的东西。
- 别重新展 UV 再烘焙：减面后的法线乱，烘出来满身黑三角。
- 法线、金属粗糙度贴图去掉：页面里小尺寸下法线贴图闪碎亮斑；金属度贴图把眼睛和牙认成了金属（渲出来是银色）。

出（都在 --dir 里）：
- prepared.blend：物体名 butcher，面朝 -Y、Z 朝上，身高 1、脚底 z=0、左右前后按包围盒居中；贴图打包在里面。
- prepared.json：正面正交图的像素 → 模型坐标，绑骨脚本按它把五官的像素位置换成坐标。
- ortho-front.png / ortho-side.png（2000×2000）：量五官和关节用。
- 终端打出每个高度上网格的横截面（x 方向哪些区间有东西），找胳膊和身子的分界。
"""
import json
import sys
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
DIR = Path(ARGS[ARGS.index("--dir") + 1]) if "--dir" in ARGS else Path.home() / "Documents/3D-Characters/butcher-hunyuan"
TARGET_TRIS = int(ARGS[ARGS.index("--tris") + 1]) if "--tris" in ARGS else 90_000
TEX = 2048

t0 = time.time()
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(DIR / "source.glb"), merge_vertices=True)
obj = next(o for o in bpy.context.scene.objects if o.type == "MESH")
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
obj.name = obj.data.name = "butcher"
me = obj.data

# 摆正：身高 1、脚踩 z=0、左右前后居中
co = np.empty(len(me.vertices) * 3)
me.vertices.foreach_get("co", co)
co = co.reshape(-1, 3)
lo, hi = co.min(axis=0), co.max(axis=0)
co = (co - np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])) / (hi[2] - lo[2])
me.vertices.foreach_set("co", co.reshape(-1))
me.update()

before = len(me.polygons)
dec = obj.modifiers.new("decimate", "DECIMATE")
dec.ratio = TARGET_TRIS / before
dec.use_collapse_triangulate = True
bpy.ops.object.modifier_apply(modifier=dec.name)
for p in me.polygons:
    p.use_smooth = True
print("DECIMATE", before, "->", len(me.polygons), "verts", len(me.vertices), f"{time.time() - t0:.0f}s")

mat = me.materials[0]
mat.name = "butcher"
nt = mat.node_tree
bsdf = next(nd for nd in nt.nodes if nd.type == "BSDF_PRINCIPLED")
bsdf.inputs["Specular Tint"].default_value = (1, 1, 1, 1)          # 源文件高光 ×2，人会发白
for socket in ("Normal", "Metallic", "Roughness"):
    for link in list(bsdf.inputs[socket].links):
        nt.links.remove(link)
bsdf.inputs["Metallic"].default_value = 0.0
bsdf.inputs["Roughness"].default_value = 0.8
for nd in list(nt.nodes):
    if nd.type in {"NORMAL_MAP", "SEPRGB", "SEPARATE_COLOR"}:
        nt.nodes.remove(nd)
for nd in list(nt.nodes):          # 上面删完以后，没连到别处的贴图节点就是法线、金属粗糙度那两张
    if nd.type == "TEX_IMAGE" and not nd.outputs["Color"].links:
        nt.nodes.remove(nd)
for img in list(bpy.data.images):
    if "normal" in img.name or "metallic" in img.name:
        bpy.data.images.remove(img)
        continue
    img.scale(TEX, TEX)
    img.name = "butcher_base"
    img.pack()
    print("TEX", img.name, tuple(img.size))
bpy.ops.wm.save_as_mainfile(filepath=str(DIR / "prepared.blend"))
print("SAVED", f"{time.time() - t0:.0f}s")

co = np.empty(len(me.vertices) * 3)
me.vertices.foreach_get("co", co)
co = co.reshape(-1, 3)
for z0 in np.arange(0.30, 0.80, 0.02):
    sl = co[np.abs(co[:, 2] - z0) < 0.004]
    if not len(sl):
        continue
    xs = np.sort(sl[:, 0])
    gaps = np.nonzero(np.diff(xs) > 0.008)[0]
    spans = " ".join(f"[{a:+.3f},{b:+.3f}]" for a, b in zip(np.r_[xs[0], xs[gaps + 1]], np.r_[xs[gaps], xs[-1]]))
    print(f"SLICE z={z0:+.2f} {spans}")

scene = bpy.context.scene
scene.render.engine = "BLENDER_EEVEE_NEXT"
scene.view_settings.view_transform = "Standard"
world = bpy.data.worlds.new("w")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.6, 0.6, 0.62, 1)
world.node_tree.nodes["Background"].inputs[1].default_value = 0.9
scene.world = world
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
scene.collection.objects.link(cam)
scene.camera = cam
cam.data.type = "ORTHO"
RES, ORTHO, CZ = 2000, 1.05, 0.5
cam.data.ortho_scale = ORTHO
scene.render.resolution_x = scene.render.resolution_y = RES
for name, loc in (("front", (0, -4, CZ)), ("side", (-4, 0, CZ))):
    cam.location = loc
    cam.rotation_euler = (Vector((0, 0, CZ)) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = str(DIR / f"ortho-{name}.png")
    bpy.ops.render.render(write_still=True)

S = RES / ORTHO
mapping = {"S": S, "OX": RES / 2, "OY": RES / 2 + CZ * S, "note": "front ortho px -> model (x, z): ((px-OX)/S, (OY-py)/S)"}
(DIR / "prepared.json").write_text(json.dumps(mapping, indent=2))
print("PREPARED", mapping, f"{time.time() - t0:.0f}s")
