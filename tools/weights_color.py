"""按骨头给顶点上色渲一张图，查哪块网格跟错了骨头（tools/figure_hunyuan.py 出的 rigged.blend 用）。

    Blender -b ~/Documents/3D-Characters/butcher-hunyuan/rigged.blend --python tools/weights_color.py -- <动作名> <帧> <输出.png> [正面|侧面|背面]
颜色：胯黄、腰橙、肚子棕、胸红、脖子紫、头粉、下巴白、大臂蓝、小臂青、手绿（按权重混合）。
"""
import sys

import bpy
import numpy as np
from mathutils import Vector

args = sys.argv[sys.argv.index("--") + 1:]
clip, frame, out = args[:3]
view = args[3] if len(args) > 3 else "正面"
arm = bpy.data.objects["butcher_rig"]
o = bpy.data.objects["figure"]
me = o.data
names = {g.index: g.name for g in o.vertex_groups}
pal = {"hips": (1, 1, 0), "spine": (1, .5, 0), "belly": (.5, .3, .1), "chest": (1, 0, 0), "neck": (.5, 0, .5), "head": (1, 0, 1),
       "jaw": (1, 1, 1), "armR": (0, 0, 1), "forearmR": (0, .8, .8), "handR": (0, 1, 0), "armL": (0, 0, 1), "forearmL": (0, .8, .8), "handL": (0, 1, 0)}
col = me.color_attributes.new("dbg", "FLOAT_COLOR", "POINT")
for v in me.vertices:
    c = np.zeros(3)
    for g in v.groups:
        c += np.array(pal.get(names[g.group], (.3, .3, .3))) * g.weight
    col.data[v.index].color = (*c, 1)
m = bpy.data.materials.new("dbg")
m.use_nodes = True
nt = m.node_tree
a = nt.nodes.new("ShaderNodeAttribute")
a.attribute_name = "dbg"
e = nt.nodes.new("ShaderNodeEmission")
nt.links.new(a.outputs["Color"], e.inputs["Color"])
nt.links.new(e.outputs["Emission"], nt.nodes["Material Output"].inputs["Surface"])
for i in range(len(me.materials)):
    me.materials[i] = m
for t in arm.animation_data.nla_tracks:
    t.mute = t.name != clip
sc = bpy.context.scene
sc.frame_set(int(frame))
sc.render.engine = "BLENDER_EEVEE_NEXT"
cam = bpy.data.objects.new("c", bpy.data.cameras.new("c"))
sc.collection.objects.link(cam)
sc.camera = cam
cam.data.type = "ORTHO"
cam.data.ortho_scale = 1.15
loc = {"正面": (0, -4, 0.55), "侧面": (4, 0, 0.55), "背面": (0, 4, 0.55)}[view]
cam.location = loc
cam.rotation_euler = (Vector((0, 0, 0.55)) - cam.location).to_track_quat("-Z", "Y").to_euler()
sc.render.resolution_x = sc.render.resolution_y = 800
sc.render.filepath = out
bpy.ops.render.render(write_still=True)
print("WCOLOR done")
