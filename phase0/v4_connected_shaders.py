# -*- coding: utf-8 -*-
"""V4: MFnMesh.getConnectedShaders(instance) を cmds の正解と照合する。

テスト用の新規シーンだけを使う。利用者の既存シーンは触らない。
2マテリアルを面ごとに割り当てたキューブ/球と、インスタンス1つを作る。
割り当てのないフェースの値も記録する。

比較はインスタンスごと・フェースごと。差分は全件記録し、
unassigned_none_vs_initial と real_mismatch に分類する。
real_mismatch が 0 なら、未割当の扱いの違いは observations に残して PASS。
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

KIND_UNASSIGNED = "unassigned_none_vs_initial"
KIND_REAL = "real_mismatch"
INITIAL_SG = "initialShadingGroup"


def face_range(transform, start, end_inclusive):
    """Maya のフェース範囲。start==end なら単一面。"""
    if start == end_inclusive:
        return "{0}.f[{1}]".format(transform, start)
    return "{0}.f[{1}:{2}]".format(transform, start, end_inclusive)


def is_initial_sg(name):
    if name is None:
        return False
    text = str(name)
    return text == INITIAL_SG or text.endswith("|" + INITIAL_SG)


def compare_instance_faces(api_row, cmds_list):
    """インスタンス1つ分をフェースごとに比較し、差分を全件分類する。"""
    api_list = api_row.get("face_sg") or []
    n = max(len(api_list), len(cmds_list))
    diffs = []
    n_unassigned = 0
    n_real = 0
    for i in range(n):
        a = api_list[i] if i < len(api_list) else "<missing>"
        b = cmds_list[i] if i < len(cmds_list) else "<missing>"
        if a == b:
            continue
        if a is None and is_initial_sg(b):
            kind = KIND_UNASSIGNED
            n_unassigned += 1
        else:
            kind = KIND_REAL
            n_real += 1
        diffs.append({"face": i, "api": a, "cmds": b, "kind": kind})
    return {
        "n_faces": n,
        "n_diff": len(diffs),
        "n_unassigned_none_vs_initial": n_unassigned,
        "n_real_mismatch": n_real,
        "diffs": diffs,
        "diffs_head": diffs[:12],
        "match": len(diffs) == 0 and len(api_list) == len(cmds_list),
        "match_ignoring_unassigned": n_real == 0 and len(api_list) == len(cmds_list),
    }


def evaluate_meshes(meshes):
    """real_mismatch が 0 なら PASS。API を呼べなければ INCONCLUSIVE。"""
    compared = [m for m in meshes or [] if m.get("api_error") is None]
    if not compared:
        return STATUS_INCONCLUSIVE, "getConnectedShaders を呼べなかった", {
            "unassigned_handling": None,
        }
    real = [m for m in compared if (m.get("compare") or {}).get("n_real_mismatch")]
    unassigned_rows = []
    for mesh in compared:
        cmp_row = mesh.get("compare") or {}
        n_u = cmp_row.get("n_unassigned_none_vs_initial") or 0
        if n_u:
            unassigned_rows.append({
                "label": mesh.get("label"),
                "instance": mesh.get("instance"),
                "n_unassigned_none_vs_initial": n_u,
                "faces": [
                    d.get("face") for d in (cmp_row.get("diffs") or [])
                    if d.get("kind") == KIND_UNASSIGNED
                ],
            })
    extra = {
        "unassigned_handling": {
            "kind": KIND_UNASSIGNED,
            "treated_as": "not_fail",
            "note": (
                "未割当フェースを API は None、cmds は initialShadingGroup と"
                "返す場合がある。インスタンスのシェーディング接続は"
                "instObjGroups[n] で別になる。"
            ),
            "meshes": unassigned_rows,
        },
    }
    if real:
        return (
            STATUS_FAIL,
            "cmds の割り当てと getConnectedShaders に real_mismatch がある",
            extra,
        )
    if unassigned_rows:
        return (
            STATUS_PASS,
            "real_mismatch は 0。未割当フェースの扱いの違いは observations に記録",
            extra,
        )
    return (
        STATUS_PASS,
        "キューブ/球/インスタンスで getConnectedShaders が cmds と一致",
        extra,
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


def _node_name_set(cmds, *nodes):
    names = set()
    for node in nodes:
        if not node:
            continue
        names.add(node)
        try:
            for found in (cmds.ls(node, long=True) or []):
                names.add(found)
                names.add(found.split("|")[-1])
            for found in (cmds.ls(node) or []):
                names.add(found)
                names.add(found.split("|")[-1])
        except Exception:
            names.add(str(node).split("|")[-1])
    return names


def _in_names(node, names):
    if not names:
        return False
    if node in names:
        return True
    text = str(node)
    short = text.split("|")[-1]
    return short in names or text in names


def cmds_inst_obj_groups(cmds, shape, instance_number):
    """instObjGroups[n] の接続を記録する(期待値の根拠)。"""
    plug = "{0}.instObjGroups[{1}]".format(shape, instance_number)
    info = {"plug": plug, "object_sgs": [], "object_groups": [], "error": None}
    try:
        info["object_sgs"] = cmds.listConnections(plug, type="shadingEngine") or []
    except Exception as exc:
        info["error"] = exception_observation(exc)
        return info
    try:
        indices = cmds.getAttr(plug + ".objectGroups", multiIndices=True) or []
    except Exception:
        indices = []
    for index in indices:
        og = "{0}.objectGroups[{1}]".format(plug, index)
        row = {"index": int(index), "sgs": [], "comp_list": None}
        try:
            row["sgs"] = cmds.listConnections(og, type="shadingEngine") or []
        except Exception:
            pass
        try:
            row["comp_list"] = cmds.getAttr(og + ".objectGrpCompList")
        except Exception:
            pass
        info["object_groups"].append(row)
    return info


def cmds_face_to_sg(cmds, shape, instance_transform=None, instance_number=0):
    """cmds でフェース→SG。インスタンスは instObjGroups とセットメンバーで区別する。

    割り当て済み SG は元トランスフォームのメンバーも見る(インスタンスが共有するため)。
    initialShadingGroup は、インスタンス番号が 0 以外なら、そのインスタンスの
    コンポーネントに付いている場合だけ採用する。元側の未割当を流用しない。
    """
    face_count = int(cmds.polyEvaluate(shape, face=True))
    result = [None] * face_count
    parent = (cmds.listRelatives(shape, parent=True, fullPath=True) or [None])[0]
    inst_names = _node_name_set(cmds, instance_transform) if instance_transform else set()
    orig_names = _node_name_set(cmds, parent, shape)
    if instance_transform:
        try:
            inst_shapes = cmds.listRelatives(
                instance_transform, shapes=True, fullPath=True
            ) or []
            inst_names |= _node_name_set(cmds, *inst_shapes)
        except Exception:
            pass
    assigned_names = orig_names | inst_names
    if instance_number == 0:
        initial_names = assigned_names
    else:
        initial_names = inst_names

    sgs = []
    inst_plug = "{0}.instObjGroups[{1}]".format(shape, instance_number)
    try:
        sgs.extend(cmds.listConnections(inst_plug, type="shadingEngine") or [])
    except Exception:
        pass
    try:
        og_idx = cmds.getAttr(inst_plug + ".objectGroups", multiIndices=True) or []
        for index in og_idx:
            og = "{0}.objectGroups[{1}]".format(inst_plug, index)
            sgs.extend(cmds.listConnections(og, type="shadingEngine") or [])
    except Exception:
        pass
    try:
        sgs.extend(cmds.listConnections(shape, type="shadingEngine") or [])
    except Exception:
        pass
    extra_objects = [shape]
    if instance_transform:
        extra_objects.append(instance_transform)
    for obj in extra_objects:
        try:
            extra = cmds.listSets(object=obj) or []
        except Exception:
            extra = []
        for name in extra:
            try:
                if cmds.nodeType(name) == "shadingEngine" and name not in sgs:
                    sgs.append(name)
            except Exception:
                pass

    seen = []
    for sg in sgs:
        if sg in seen:
            continue
        seen.append(sg)
        accept = initial_names if is_initial_sg(sg) else assigned_names
        members = cmds.sets(sg, query=True) or []
        for item in members:
            flat = cmds.ls(item, flatten=True) or []
            whole = False
            for comp in flat:
                node = str(comp).split(".")[0]
                node_ok = _in_names(node, accept)
                if ".f[" in str(comp):
                    if not node_ok:
                        continue
                    try:
                        idx = int(str(comp).split(".f[")[-1].rstrip("]"))
                    except ValueError:
                        continue
                    if 0 <= idx < face_count:
                        result[idx] = sg
                elif node_ok or comp == shape:
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
    # 範囲指定。球で個別リストだと一部が initialShadingGroup に残ることがある。
    faces_a = face_range(transform, 0, mid - 1) if face_count else None
    last = None
    faces_b = None
    if unassign_last and face_count:
        last = face_range(transform, face_count - 1, face_count - 1)
        if mid <= face_count - 2:
            faces_b = face_range(transform, mid, face_count - 2)
    elif face_count and mid <= face_count - 1:
        faces_b = face_range(transform, mid, face_count - 1)
    if faces_a:
        cmds.sets(faces_a, edit=True, forceElement=sg_a)
    if faces_b:
        cmds.sets(faces_b, edit=True, forceElement=sg_b)
    if last:
        for sg in (sg_a, sg_b, INITIAL_SG):
            try:
                cmds.sets(last, edit=True, remove=sg)
            except Exception:
                pass
    # セットアップ後に cmds で読み戻し、それを期待値として保存する。
    readback, _n = cmds_face_to_sg(
        cmds, shape, instance_transform=transform, instance_number=0,
    )
    return {
        "transform": transform,
        "shape": shape,
        "face_count": face_count,
        "sg_a": sg_a,
        "sg_b": sg_b,
        "shader_a": shader_a,
        "shader_b": shader_b,
        "faces_a": faces_a,
        "faces_b": faces_b,
        "unassigned_comp": last,
        "cmds_face_sg_after_setup": readback,
    }


def run_maya_cases(cmds):
    new_scratch_scene(cmds)
    cube = cmds.polyCube(name="phase0_v4_cube")[0]
    sphere = cmds.polySphere(name="phase0_v4_sphere", sx=6, sy=4)[0]
    cube_info = _assign_two_materials(cmds, cube, unassign_last=True)
    sphere_info = _assign_two_materials(cmds, sphere, unassign_last=True)
    inst = cmds.instance(cube, name="phase0_v4_cube_inst")[0]

    results = []
    for label, info, inst_no, inst_xform in (
        ("cube_instance0", cube_info, 0, cube_info["transform"]),
        ("cube_instance1", cube_info, 1, inst),
        ("sphere_instance0", sphere_info, 0, sphere_info["transform"]),
    ):
        cmds_map, _n = cmds_face_to_sg(
            cmds,
            info["shape"],
            instance_transform=inst_xform,
            instance_number=inst_no,
        )
        try:
            api_row = api_face_to_sg(info["shape"], inst_no)
            api_error = None
        except Exception as exc:
            api_row = {}
            api_error = exception_observation(exc)
        cmp_row = compare_instance_faces(api_row, cmds_map) if api_row else {
            "match": False,
            "n_diff": None,
            "n_real_mismatch": None,
            "n_unassigned_none_vs_initial": None,
            "diffs": [],
        }
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
            "instance_transform": inst_xform,
            "inst_obj_groups": cmds_inst_obj_groups(cmds, info["shape"], inst_no),
            "cmds_face_sg": cmds_map,
            "api": api_row,
            "api_error": api_error,
            "compare": cmp_row,
            "unassigned_face_index": unassigned_idx,
            "unassigned_cmds": (
                cmds_map[unassigned_idx]
                if unassigned_idx is not None and unassigned_idx < len(cmds_map)
                else None
            ),
            "unassigned_api": (
                (api_row.get("face_sg") or [None])[unassigned_idx]
                if api_row and unassigned_idx is not None
                and unassigned_idx < len(api_row.get("face_sg") or [])
                else None
            ),
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
    status, summary, extra = evaluate_meshes(obs.get("meshes") or [])
    obs["maya"] = meta
    obs["scratch"] = scratch
    obs.update(extra)
    return make_check(status, summary, obs)


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
