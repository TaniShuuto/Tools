# -*- coding: utf-8 -*-
"""V4: MFnMesh.getConnectedShaders(instance) を cmds の正解と照合する。

テスト用の新規シーンだけを使う。利用者の既存シーンは触らない。
2マテリアルを面ごとに割り当てたキューブ/球と、インスタンス1つを作る。
割り当てのないフェースの値も記録する。
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


def _make_shader(cmds, name):
    shader = cmds.shadingNode("lambert", asShader=True, name=name)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name + "SG")
    cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader", force=True)
    return shader, sg


def _shape_of(cmds, transform):
    shapes = cmds.listRelatives(transform, shapes=True, fullPath=True) or []
    if not shapes:
        raise RuntimeError("no shape under {0}".format(transform))
    return shapes[0]


def cmds_face_to_sg(cmds, shape):
    face_count = int(cmds.polyEvaluate(shape, face=True))
    result = [None] * face_count
    sgs = cmds.listConnections(shape, type="shadingEngine") or []
    # セットメンバーからも拾う(listConnections 漏れ対策)
    extra = cmds.listSets(object=shape) or []
    for name in extra:
        if name not in sgs:
            try:
                if cmds.nodeType(name) == "shadingEngine":
                    sgs.append(name)
            except Exception:
                pass
    seen = []
    for sg in sgs:
        if sg in seen:
            continue
        seen.append(sg)
        members = cmds.sets(sg, query=True) or []
        for item in members:
            flat = cmds.ls(item, flatten=True) or []
            whole = False
            parent = (cmds.listRelatives(shape, parent=True, fullPath=True) or [None])[0]
            for comp in flat:
                if ".f[" in comp:
                    try:
                        idx = int(comp.split(".f[")[-1].rstrip("]"))
                    except ValueError:
                        continue
                    if 0 <= idx < face_count:
                        result[idx] = sg
                else:
                    node = comp.split(".")[0]
                    short = os.path.basename(shape)
                    if node in (shape, parent, short) or comp == shape or comp.endswith("|" + short):
                        whole = True
            if whole:
                for i in range(face_count):
                    if result[i] is None:
                        result[i] = sg
    return result, face_count


def api_face_to_sg(shape, instance_number):
    import maya.api.OpenMaya as om  # type: ignore

    sel = om.MSelectionList()
    sel.add(shape)
    dag = sel.getDagPath(0)
    # 指定インスタンスの DAG パスへ
    try:
        fn_dag = om.MFnDagNode(sel.getDependNode(0))
        paths = fn_dag.getAllPaths()
        for path in paths:
            if int(path.instanceNumber()) == int(instance_number):
                dag = path
                break
    except Exception:
        pass
    fn = om.MFnMesh(dag)
    shaders, indices = fn.getConnectedShaders(int(instance_number))
    names = []
    for obj in shaders:
        names.append(om.MFnDependencyNode(obj).name())
    mapped = []
    raw_indices = [int(i) for i in indices]
    for idx in raw_indices:
        if idx < 0:
            mapped.append(None)
        elif idx < len(names):
            mapped.append(names[idx])
        else:
            mapped.append({"bad_index": idx, "n_shaders": len(names)})
    return {
        "shader_names": names,
        "face_indices": raw_indices,
        "face_sg": mapped,
        "instance_number": int(instance_number),
        "dag": str(dag),
    }


def _assign_two_materials(cmds, transform, unassign_last=True):
    shape = _shape_of(cmds, transform)
    face_count = int(cmds.polyEvaluate(shape, face=True))
    shader_a, sg_a = _make_shader(cmds, transform + "_matA")
    shader_b, sg_b = _make_shader(cmds, transform + "_matB")
    mid = max(1, face_count // 2)
    faces_a = ["{0}.f[{1}]".format(transform, i) for i in range(0, mid)]
    faces_b = ["{0}.f[{1}]".format(transform, i) for i in range(mid, face_count)]
    if unassign_last and faces_b:
        last = faces_b[-1]
        faces_b = faces_b[:-1]
    else:
        last = None
    if faces_a:
        cmds.sets(faces_a, edit=True, forceElement=sg_a)
    if faces_b:
        cmds.sets(faces_b, edit=True, forceElement=sg_b)
    unassigned = None
    if last:
        # どの SG からも外す
        for sg in (sg_a, sg_b, "initialShadingGroup"):
            try:
                cmds.sets(last, edit=True, remove=sg)
            except Exception:
                pass
        unassigned = last
    return {
        "transform": transform,
        "shape": shape,
        "face_count": face_count,
        "sg_a": sg_a,
        "sg_b": sg_b,
        "shader_a": shader_a,
        "shader_b": shader_b,
        "unassigned_comp": unassigned,
    }


def _compare(api_row, cmds_list):
    api_list = api_row.get("face_sg") or []
    n = max(len(api_list), len(cmds_list))
    diffs = []
    for i in range(n):
        a = api_list[i] if i < len(api_list) else "<missing>"
        b = cmds_list[i] if i < len(cmds_list) else "<missing>"
        if a != b:
            diffs.append({"face": i, "api": a, "cmds": b})
    return {
        "n_faces": n,
        "n_diff": len(diffs),
        "diffs_head": diffs[:12],
        "match": len(diffs) == 0 and len(api_list) == len(cmds_list),
    }


def run_maya_cases(cmds):
    new_scratch_scene(cmds)
    cube = cmds.polyCube(name="phase0_v4_cube")[0]
    sphere = cmds.polySphere(name="phase0_v4_sphere", sx=6, sy=4)[0]
    cube_info = _assign_two_materials(cmds, cube, unassign_last=True)
    sphere_info = _assign_two_materials(cmds, sphere, unassign_last=True)
    inst = cmds.instance(cube, name="phase0_v4_cube_inst")[0]

    results = []
    for label, info, inst_no, dag_for_cmds in (
        ("cube_instance0", cube_info, 0, cube_info["shape"]),
        ("cube_instance1", cube_info, 1, cube_info["shape"]),
        ("sphere_instance0", sphere_info, 0, sphere_info["shape"]),
    ):
        cmds_map, _n = cmds_face_to_sg(cmds, dag_for_cmds)
        try:
            api_row = api_face_to_sg(info["shape"], inst_no)
            api_error = None
        except Exception as exc:
            api_row = {}
            api_error = exception_observation(exc)
        cmp_row = _compare(api_row, cmds_map) if api_row else {"match": False, "n_diff": None}
        unassigned_idx = None
        if info.get("unassigned_comp") and ".f[" in info["unassigned_comp"]:
            try:
                unassigned_idx = int(info["unassigned_comp"].split(".f[")[-1].rstrip("]"))
            except ValueError:
                unassigned_idx = None
        results.append({
            "label": label,
            "instance": inst_no,
            "setup": info,
            "instance_transform": inst if inst_no == 1 else info["transform"],
            "cmds_face_sg": cmds_map,
            "api": api_row,
            "api_error": api_error,
            "compare": cmp_row,
            "unassigned_face_index": unassigned_idx,
            "unassigned_cmds": cmds_map[unassigned_idx] if unassigned_idx is not None and unassigned_idx < len(cmds_map) else None,
            "unassigned_api": (api_row.get("face_sg") or [None])[unassigned_idx]
            if api_row and unassigned_idx is not None and unassigned_idx < len(api_row.get("face_sg") or [])
            else None,
        })
    return {"meshes": results}


def run_check():
    cmds, meta = try_import_maya()
    if cmds is None:
        return make_check(
            STATUS_INCONCLUSIVE,
            "Maya が無い(この環境では未実行)",
            {"maya": meta},
        )
    scratch = scratch_scene_ok(cmds)
    if not scratch.get("ok"):
        return make_check(
            STATUS_INCONCLUSIVE,
            "既存シーンを触らないため中止。mayapy か空の新規シーンで実行する",
            {"maya": meta, "scratch": scratch},
        )
    try:
        obs = run_maya_cases(cmds)
    except Exception as exc:
        return make_check(
            STATUS_INCONCLUSIVE,
            "Maya 操作中に例外",
            {"maya": meta, "error": exception_observation(exc)},
        )
    obs["maya"] = meta
    obs["scratch"] = scratch
    compared = [m for m in obs.get("meshes") or [] if m.get("api_error") is None]
    if not compared:
        return make_check(STATUS_INCONCLUSIVE, "getConnectedShaders を呼べなかった", obs)
    mismatches = [m for m in compared if not (m.get("compare") or {}).get("match")]
    if mismatches:
        return make_check(
            STATUS_FAIL,
            "cmds の割り当てと getConnectedShaders が一致しないメッシュがある",
            obs,
        )
    return make_check(
        STATUS_PASS,
        "キューブ/球/インスタンスで getConnectedShaders が cmds と一致",
        obs,
    )


def _output_path():
    phase0 = resolve_phase0_dir(PHASE0_DIR_OVERRIDE) or _HERE
    return default_results_path("maya", phase0, OUTPUT_JSON_OVERRIDE)


def main():
    check = run_check()
    payload = write_results(_output_path(), {"V4": check}, env=collect_env())
    print_results(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
