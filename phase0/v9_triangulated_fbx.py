# -*- coding: utf-8 -*-
"""V9: 四角形を含むテストメッシュを FBXExportTriangulate で出力する。

Painter での読み込み・ペイント・書き出しは v9_protocol.md の手動確認。
利用者の既存シーンは触らない。出力は phase0/scratch 配下。
"""
from __future__ import annotations

import os
import sys

PHASE0_DIR_OVERRIDE = ""
OUTPUT_JSON_OVERRIDE = ""

_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from common import (  # noqa: E402
    STATUS_FAIL,
    STATUS_INCONCLUSIVE,
    STATUS_PASS,
    anonymize_path,
    collect_env,
    default_results_path,
    exception_observation,
    make_check,
    new_scratch_scene,
    print_results,
    resolve_phase0_dir,
    scratch_scene_ok,
    try_import_maya,
    write_results,
)


def _shape_of(cmds, transform):
    shapes = cmds.listRelatives(transform, shapes=True, fullPath=True) or []
    if not shapes:
        raise RuntimeError("no shape")
    return shapes[0]


def face_vertex_counts(cmds, transform):
    shape = _shape_of(cmds, transform)
    try:
        import maya.api.OpenMaya as om  # type: ignore

        sel = om.MSelectionList()
        sel.add(shape)
        fn = om.MFnMesh(sel.getDagPath(0))
        counts, _indices = fn.getVertices()
        return [int(c) for c in counts], "OpenMaya2"
    except Exception as exc:
        n = int(cmds.polyEvaluate(shape, face=True))
        counts = []
        for i in range(n):
            info = cmds.polyInfo("{0}.f[{1}]".format(shape, i), faceToVertex=True)
            parts = str(info[0]).replace(":", " ").split()
            nums = [int(t) for t in parts if t.isdigit()]
            counts.append(max(0, len(nums) - 1))
        return counts, "polyInfo:{0}".format(exception_observation(exc)["type"])


# --- v9b: テストメッシュの配置(UV を重ねない・マテリアル 2 つ) -------------
FBX_NAME = "phase0_v9b_triangulated.fbx"
PLANE_SHADER = "phase0_matA"
PLANE_SG = "phase0_sgA"  # シェーダー名と、あえて別の名前にする
CUBE_SHADER = "phase0_matB"
CUBE_SG = "phase0_sgB"
# 既定の UV はどちらも 0〜1 いっぱいに広がる。0.4 倍に縮め、中心を左下と右上へ置く。
UV_SCALE = 0.4
PLANE_CENTER = (0.25, 0.25)
CUBE_CENTER = (0.75, 0.75)


def uv_bbox(us, vs):
    """UV 値の外接矩形 (umin, vmin, umax, vmax)。空なら None。"""
    if not us or not vs:
        return None
    return (min(us), min(vs), max(us), max(vs))


def bboxes_overlap(a, b, eps=1e-6):
    """2 つの外接矩形が重なる(辺が接するだけは重ならない)なら True。"""
    if a is None or b is None:
        return None
    return not (a[2] <= b[0] + eps or b[2] <= a[0] + eps or a[3] <= b[1] + eps or b[3] <= a[1] + eps)


def bbox_inside_unit(bbox, eps=1e-6):
    if bbox is None:
        return False
    return bbox[0] >= -eps and bbox[1] >= -eps and bbox[2] <= 1 + eps and bbox[3] <= 1 + eps


def assign_material(cmds, transform, shader_name, sg_name):
    """lambert とシェーディンググループを作って割り当てる(C2 の実機検証と同じ作り)。"""
    shader = cmds.shadingNode("lambert", asShader=True, name=shader_name)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=sg_name)
    cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader", force=True)
    cmds.sets(transform, edit=True, forceElement=sg)
    return {"shader": shader, "shading_group": sg}


def place_uvs(cmds, transform, center_u, center_v, scale):
    """全 UV を (0.5, 0.5) 基準で縮め、中心を (center_u, center_v) へ動かす。"""
    uvs = transform + ".map[*]"
    cmds.polyEditUV(uvs, pivotU=0.5, pivotV=0.5, scaleU=scale, scaleV=scale)
    cmds.polyEditUV(uvs, u=center_u - 0.5, v=center_v - 0.5)
    return {"scale": scale, "center": [center_u, center_v]}


def uv_values(cmds, transform):
    """現在の UV セットの U・V の値を OpenMaya 2.0 で一括取得する。"""
    import maya.api.OpenMaya as om  # type: ignore

    sel = om.MSelectionList()
    sel.add(_shape_of(cmds, transform))
    us, vs = om.MFnMesh(sel.getDagPath(0)).getUVs()
    return [float(u) for u in us], [float(v) for v in vs]


def export_triangulated_fbx(cmds, dest_path, nodes):
    import maya.mel as mel  # type: ignore

    loaded = False
    try:
        if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
            cmds.loadPlugin("fbxmaya")
        loaded = True
    except Exception as exc:
        return {"ok": False, "error": exception_observation(exc), "plugin_loaded": False}

    cmds.select(nodes, replace=True)
    # 公式 MEL: FBXExportTriangulate / FBXExport
    steps = []
    for cmd in (
        'FBXResetExport',
        'FBXExportTriangulate -v true',
        'FBXExportSmoothingGroups -v true',
        'FBXExportInputConnections -v false',
        'FBXExportEmbeddedTextures -v false',
    ):
        try:
            mel.eval(cmd)
            steps.append({"cmd": cmd, "ok": True})
        except Exception as exc:
            steps.append({"cmd": cmd, "ok": False, "error": exception_observation(exc)})

    # Windows でも MEL は / 区切りを受け付けることが多い
    dest_mel = dest_path.replace("\\", "/")
    export_cmd = 'FBXExport -f "{0}" -s'.format(dest_mel)
    try:
        mel.eval(export_cmd)
        steps.append({"cmd": "FBXExport -f <anon> -s", "ok": True})
        existed = os.path.isfile(dest_path)
        return {
            "ok": existed,
            "plugin_loaded": loaded,
            "steps": steps,
            "path": anonymize_path(dest_path),
            "size_bytes": os.path.getsize(dest_path) if existed else None,
        }
    except Exception as exc:
        steps.append({"cmd": "FBXExport -f <anon> -s", "ok": False, "error": exception_observation(exc)})
        return {"ok": False, "plugin_loaded": loaded, "steps": steps, "error": exception_observation(exc)}


def run_maya_cases(cmds, scratch_dir):
    new_scratch_scene(cmds)
    os.makedirs(scratch_dir, exist_ok=True)
    # 平面は四角形。キューブも四角形。
    plane = cmds.polyPlane(name="phase0_v9_plane", sx=2, sy=2, w=2, h=2)[0]
    cube = cmds.polyCube(name="phase0_v9_cube", w=1, h=1, d=1)[0]

    # v9b: UV を重ならない配置にし、2 つのマテリアルを割り当てる。
    materials = {
        "plane": assign_material(cmds, plane, PLANE_SHADER, PLANE_SG),
        "cube": assign_material(cmds, cube, CUBE_SHADER, CUBE_SG),
    }
    layout = {}
    for key, node, (cu, cv) in (
        ("plane", plane, PLANE_CENTER),
        ("cube", cube, CUBE_CENTER),
    ):
        layout[key] = {"edit": place_uvs(cmds, node, cu, cv, UV_SCALE)}
        us, vs = uv_values(cmds, node)
        layout[key]["bbox"] = uv_bbox(us, vs)
    layout["overlap"] = bboxes_overlap(layout["plane"]["bbox"], layout["cube"]["bbox"])
    layout["inside_0_1"] = all(
        bbox_inside_unit(layout[k]["bbox"]) for k in ("plane", "cube")
    )

    counts_plane, api_p = face_vertex_counts(cmds, plane)
    counts_cube, api_c = face_vertex_counts(cmds, cube)
    dest = os.path.join(scratch_dir, FBX_NAME)
    exported = export_triangulated_fbx(cmds, dest, [plane, cube])
    return {
        "plane_face_vertex_counts": counts_plane,
        "cube_face_vertex_counts": counts_cube,
        "plane_has_quads": any(c == 4 for c in counts_plane),
        "cube_has_quads": any(c == 4 for c in counts_cube),
        "count_api_plane": api_p,
        "count_api_cube": api_c,
        "materials": materials,
        "uv_layout": layout,
        "export": exported,
        "fbx_path": anonymize_path(dest),
        "painter_import": "MANUAL see v9_protocol.md",
    }


def run_check():
    cmds, meta = try_import_maya()
    if cmds is None:
        return make_check(
            STATUS_INCONCLUSIVE,
            "Maya が無い(FBX出力は未実行)。Painter確認は v9_protocol.md",
            {"maya": meta},
        )
    scratch = scratch_scene_ok(cmds)
    if not scratch.get("ok"):
        return make_check(
            STATUS_INCONCLUSIVE,
            "既存シーンを触らないため中止。mayapy か空の新規シーンで実行する",
            {"maya": meta, "scratch": scratch},
        )
    phase0 = resolve_phase0_dir(PHASE0_DIR_OVERRIDE) or _HERE
    scratch_dir = os.path.join(phase0, "scratch")
    try:
        obs = run_maya_cases(cmds, scratch_dir)
    except Exception as exc:
        return make_check(
            STATUS_INCONCLUSIVE,
            "Maya 操作中に例外",
            {"maya": meta, "error": exception_observation(exc)},
        )
    obs["maya"] = meta
    obs["scratch"] = scratch
    exported = (obs.get("export") or {}).get("ok")
    has_quads = obs.get("plane_has_quads") or obs.get("cube_has_quads")
    layout = obs.get("uv_layout") or {}
    layout_ok = layout.get("overlap") is False and layout.get("inside_0_1") is True
    if exported and has_quads and not layout_ok:
        return make_check(
            STATUS_FAIL,
            "FBX は出力したが、UV が重なっているか 0〜1 の外にある(配置の確認が必要)",
            obs,
        )
    if exported and has_quads:
        return make_check(
            STATUS_PASS,
            "四角形メッシュ(UV 重なりなし・マテリアル 2 つ)を FBXExportTriangulate で出力した。Painter確認は手動",
            obs,
        )
    if not has_quads:
        return make_check(STATUS_INCONCLUSIVE, "四角形フェースを確認できなかった", obs)
    return make_check(STATUS_FAIL, "FBX を書き出せなかった", obs)


def _output_path():
    phase0 = resolve_phase0_dir(PHASE0_DIR_OVERRIDE) or _HERE
    return default_results_path("maya", phase0, OUTPUT_JSON_OVERRIDE)


def main():
    check = run_check()
    payload = write_results(_output_path(), {"V9": check}, env=collect_env())
    print_results(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
