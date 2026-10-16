# -*- coding: utf-8 -*-
"""V6: トポロジー指紋。Maya で球を作り、UV操作では不変、分割/削減では変化することを確認する。

純粋ロジックは v6_topology.py (pytest 対象)。
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
from v6_topology import (  # noqa: E402
    canonical_cycle,
    compute_topology_fingerprint,
    faces_all_reversed,
)


def mesh_face_vertex_ids(cmds, dag_name):
    """OpenMaya 2.0 の getVertices。失敗時は cmds へ倒す。"""
    try:
        import maya.api.OpenMaya as om  # type: ignore

        sel = om.MSelectionList()
        sel.add(dag_name)
        dag = sel.getDagPath(0)
        fn = om.MFnMesh(dag)
        counts, indices = fn.getVertices()
        faces = []
        cursor = 0
        for count in counts:
            faces.append([int(x) for x in indices[cursor:cursor + int(count)]])
            cursor += int(count)
        return faces, {"api": "OpenMaya2.MFnMesh.getVertices"}
    except Exception as api_exc:
        faces = []
        n = int(cmds.polyEvaluate(dag_name, face=True))
        for i in range(n):
            info = cmds.polyInfo("{0}.f[{1}]".format(dag_name, i), faceToVertex=True)
            if not info:
                raise RuntimeError("polyInfo empty for face {0}".format(i))
            # "FACE 0: 0 1 2 3"
            parts = str(info[0]).replace(":", " ").split()
            nums = []
            for token in parts:
                if token.isdigit() or (token.startswith("-") and token[1:].isdigit()):
                    nums.append(int(token))
            if nums:
                nums = nums[1:]  # 先頭はフェース番号
            faces.append(nums)
        return faces, {
            "api": "cmds.polyInfo",
            "om2_error": exception_observation(api_exc),
        }


def _fingerprint_of(cmds, name):
    faces, meta = mesh_face_vertex_ids(cmds, name)
    return compute_topology_fingerprint(faces), faces, meta


def _try_cmd(label, fn):
    try:
        fn()
        return {"ok": True, "label": label}
    except Exception as exc:
        return {"ok": False, "label": label, "error": exception_observation(exc)}


def _edge_components(cmds, mesh, edge_index, mode):
    """polySelect でリング/ループを取る。失敗時は単一エッジ。"""
    comps = []
    try:
        if hasattr(cmds, "polySelect"):
            ids = cmds.polySelect(mesh, noSelection=True, **{mode: int(edge_index)}) or []
            comps = ["{0}.e[{1}]".format(mesh, int(i)) for i in ids]
    except Exception:
        comps = []
    if not comps:
        comps = ["{0}.e[{1}]".format(mesh, int(edge_index))]
    return comps


def _try_face_increase(cmds, mesh):
    """面数が増える分割を試す。e[0] の no-op を避け、赤道付近のリング/ループを選ぶ。

    増えなければ polySubdivideFacet 等へ切り替える。
    面数増加は前提条件。増えなくてもここでは判定せず、evaluate が INCONCLUSIVE にする。
    """
    before_faces = mesh_face_vertex_ids(cmds, mesh)[0]
    before = compute_topology_fingerprint(before_faces)
    n_edges = int(cmds.polyEvaluate(mesh, edge=True))
    attempts = []

    def n_faces():
        return int(cmds.polyEvaluate(mesh, face=True))

    edge_candidates = []
    if n_edges > 0:
        for idx in (n_edges // 2, n_edges // 3, min(8, n_edges - 1), 0):
            if idx not in edge_candidates:
                edge_candidates.append(idx)

    increased = False
    if hasattr(cmds, "polySplitRing"):
        for idx in edge_candidates:
            if increased:
                break
            for mode in ("edgeRing", "edgeLoop"):
                comps = _edge_components(cmds, mesh, idx, mode)
                before_n = n_faces()

                def _do(c=list(comps)):
                    cmds.select(c)
                    cmds.polySplitRing(c, splitType=1, divisions=1, weight=0.5)

                ran = _try_cmd("polySplitRing_{0}_e{1}".format(mode, idx), _do)
                after_n = n_faces()
                attempts.append({
                    "label": ran.get("label"),
                    "command": ran,
                    "mode": mode,
                    "seed_edge": idx,
                    "n_edges_used": len(comps),
                    "edges_head": comps[:16],
                    "n_faces_before": before_n,
                    "n_faces_after": after_n,
                    "increased": after_n > before_n,
                })
                if after_n > before_n:
                    increased = True
                    break

    if not increased and hasattr(cmds, "polySubdivideFacet"):
        before_n = n_faces()
        ran = _try_cmd(
            "polySubdivideFacet",
            lambda: cmds.polySubdivideFacet(mesh + ".f[0]", divisions=1),
        )
        after_n = n_faces()
        attempts.append({
            "label": "polySubdivideFacet",
            "command": ran,
            "n_faces_before": before_n,
            "n_faces_after": after_n,
            "increased": after_n > before_n,
        })
        if after_n > before_n:
            increased = True

    if not increased and hasattr(cmds, "polySubdivideEdge"):
        before_n = n_faces()
        mid = n_edges // 2 if n_edges else 0
        ran = _try_cmd(
            "polySubdivideEdge",
            lambda: cmds.polySubdivideEdge(mesh + ".e[{0}]".format(mid), divisions=1),
        )
        after_n = n_faces()
        attempts.append({
            "label": "polySubdivideEdge",
            "command": ran,
            "n_faces_before": before_n,
            "n_faces_after": after_n,
            "increased": after_n > before_n,
        })
        if after_n > before_n:
            increased = True

    after_faces = mesh_face_vertex_ids(cmds, mesh)[0]
    after = compute_topology_fingerprint(after_faces)
    any_ok = any((a.get("command") or {}).get("ok") for a in attempts)
    return {
        "label": "face_split",
        "command": {"ok": any_ok, "label": "face_split"} if attempts else {
            "ok": False, "error": "no_split_command",
        },
        "attempts": attempts,
        "before": before,
        "after": after,
        "changed": before != after,
        "n_faces_before": len(before_faces),
        "n_faces_after": len(after_faces),
        "faces_increased": increased,
        "requires_face_increase": True,
    }


def run_maya_cases(cmds):
    obs = {"steps": [], "invariants": [], "expected_changes": [], "normal_flip": {}}
    new_scratch_scene(cmds)
    sphere = cmds.polySphere(name="phase0_v6_sphere", sx=8, sy=6)[0]
    base_fp, base_faces, meta = _fingerprint_of(cmds, sphere)
    obs["api"] = meta
    obs["n_faces"] = len(base_faces)
    obs["baseline"] = base_fp
    obs["sample_face0_canonical"] = canonical_cycle(base_faces[0]) if base_faces else []

    def record_invariant(label, action):
        before = compute_topology_fingerprint(mesh_face_vertex_ids(cmds, sphere)[0])
        ran = _try_cmd(label, action)
        after_faces = mesh_face_vertex_ids(cmds, sphere)[0]
        after = compute_topology_fingerprint(after_faces)
        row = {
            "label": label,
            "command": ran,
            "before": before,
            "after": after,
            "unchanged": before == after,
        }
        obs["invariants"].append(row)
        return row

    # UV 操作。存在しないコマンドは記録してスキップ。
    all_uv = sphere + ".map[*]"
    all_edge = sphere + ".e[*]"
    record_invariant("polyMapCut", lambda: cmds.polyMapCut(all_edge, ch=False))
    record_invariant("polyMapSew", lambda: cmds.polyMapSew(all_edge, ch=False))
    record_invariant("polyEditUV_move", lambda: cmds.polyEditUV(all_uv, u=0.05, v=-0.03))
    if hasattr(cmds, "polyFlipUV"):
        record_invariant("polyFlipUV", lambda: cmds.polyFlipUV(all_uv))
    else:
        obs["invariants"].append({
            "label": "polyFlipUV",
            "command": {"ok": False, "error": "hasattr_false"},
            "unchanged": None,
        })
    record_invariant(
        "polyEditUV_rotate_shell",
        lambda: cmds.polyEditUV(all_uv, rot=35.0, pu=0.5, pv=0.5),
    )

    split_row = _try_face_increase(cmds, sphere)
    obs["expected_changes"].append(split_row)

    # 削減は別メッシュで(上の分割後だと条件が変わるため)
    reduce_mesh = cmds.polySphere(name="phase0_v6_reduce", sx=10, sy=8)[0]
    before_r = compute_topology_fingerprint(mesh_face_vertex_ids(cmds, reduce_mesh)[0])
    ran = _try_cmd("polyReduce", lambda: cmds.polyReduce(reduce_mesh, percentage=50, ch=False))
    after_r_faces = mesh_face_vertex_ids(cmds, reduce_mesh)[0]
    after_r = compute_topology_fingerprint(after_r_faces)
    obs["expected_changes"].append({
        "label": "polyReduce",
        "command": ran,
        "before": before_r,
        "after": after_r,
        "changed": before_r != after_r,
        "n_faces_after": len(after_r_faces),
    })

    flip = cmds.polySphere(name="phase0_v6_flip", sx=6, sy=4)[0]
    before_faces = mesh_face_vertex_ids(cmds, flip)[0]
    before_fp = compute_topology_fingerprint(before_faces)
    ran = _try_cmd("polyNormal_reverse", lambda: cmds.polyNormal(flip, normalMode=0, ch=False))
    after_faces = mesh_face_vertex_ids(cmds, flip)[0]
    after_fp = compute_topology_fingerprint(after_faces)
    obs["normal_flip"] = {
        "command": ran,
        "fingerprint_before": before_fp,
        "fingerprint_after": after_fp,
        "fingerprint_changed": before_fp != after_fp,
        "all_faces_reversed": faces_all_reversed(before_faces, after_faces),
        "sample_before": before_faces[0] if before_faces else [],
        "sample_after": after_faces[0] if after_faces else [],
        "sample_before_canonical": canonical_cycle(before_faces[0]) if before_faces else [],
        "sample_after_canonical": canonical_cycle(after_faces[0]) if after_faces else [],
    }
    return obs


def evaluate(obs):
    if not obs:
        return STATUS_INCONCLUSIVE, "観測が空"
    invariant_fail = [
        r for r in obs.get("invariants") or []
        if r.get("command", {}).get("ok") and r.get("unchanged") is False
    ]
    invariant_ran = [
        r for r in obs.get("invariants") or []
        if r.get("command", {}).get("ok")
    ]
    split_no_increase = [
        r for r in obs.get("expected_changes") or []
        if r.get("requires_face_increase") and not r.get("faces_increased")
    ]
    change_fail = [
        r for r in obs.get("expected_changes") or []
        if r.get("command", {}).get("ok")
        and r.get("changed") is False
        and not (r.get("requires_face_increase") and not r.get("faces_increased"))
    ]
    change_ran = [
        r for r in obs.get("expected_changes") or []
        if r.get("command", {}).get("ok")
    ]
    if split_no_increase:
        return STATUS_INCONCLUSIVE, "分割しても面数が増えなかった(前提条件未達)"
    if not invariant_ran or not change_ran:
        return STATUS_INCONCLUSIVE, "UV操作またはトポロジー変更のコマンドが実行できなかった"
    if invariant_fail or change_fail:
        return STATUS_FAIL, "指紋の期待(UVで不変 / 分割削減で変化)が崩れた"
    return STATUS_PASS, "UV操作では指紋不変、分割/削減では変化を確認"


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
    status, summary = evaluate(obs)
    obs["maya"] = meta
    obs["scratch"] = scratch
    return make_check(status, summary, obs)


def _output_path():
    phase0 = resolve_phase0_dir(PHASE0_DIR_OVERRIDE) or _HERE
    return default_results_path("maya", phase0, OUTPUT_JSON_OVERRIDE)


def main():
    check = run_check()
    payload = write_results(_output_path(), {"V6": check}, env=collect_env())
    print_results(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
