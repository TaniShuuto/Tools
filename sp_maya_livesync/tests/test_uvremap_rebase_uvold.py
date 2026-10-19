# -*- coding: utf-8 -*-
"""旧 UV セット(uvOld)の純関数と、cmds 注入での一時作成・後始末。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_MODULE_DIR = Path(__file__).resolve().parents[1]
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

import uvremap_core as core  # noqa: E402
import uvremap_maya as um  # noqa: E402


def test_corner_uv_merges_duplicates_and_drops_nan_faces():
    counts = np.array([4, 3, 4], dtype=np.int32)
    corner_uv = np.array(
        [
            [0.0, 0.0],
            [1.0, 0.0],
            [1.0, 1.0],
            [0.0, 1.0],
            [0.0, 0.0],
            [0.5, 0.0],
            [0.5, 0.5],
            [np.nan, 0.0],
            [0.0, 1.0],
            [1.0, 1.0],
            [0.2, 0.2],
        ],
        dtype=np.float32,
    )
    u_values, v_values, uv_counts, uv_ids = um.corner_uv_to_uv_arrays(counts, corner_uv)
    assert uv_counts.tolist() == [4, 3, 0]
    assert int(uv_ids.size) == int(uv_counts.sum())
    assert u_values.shape == v_values.shape
    assert int(u_values.size) == 6
    back, _no_uv = um.assigned_uvs_to_corner_uv(
        counts, uv_counts, uv_ids, u_values, v_values,
    )
    assert np.allclose(back[:7], corner_uv[:7])
    assert np.isnan(back[7:]).all()


def test_choose_uvold_name_skips_taken_names_case_sensitively():
    assert um.choose_uvold_name({}) == "uvOld"
    assert um.choose_uvold_name({"|a": ["map1"]}) == "uvOld"
    assert um.choose_uvold_name({"|a": ["uvOld"]}) == "uvOld_1"
    assert um.choose_uvold_name({"|a": ["uvOld"], "|b": ["uvOld_1"]}) == "uvOld_2"
    assert um.choose_uvold_name({"|a": ["map1"], "|b": ["uvOld"]}) == "uvOld_1"
    assert um.choose_uvold_name({"|a": ["uvold"]}) == "uvOld"


def _snap(counts, vids, uv, face_mesh, uuids):
    pos = np.zeros((int(np.asarray(vids).size), 3), dtype=np.float32)
    return core.make_snapshot(
        np.asarray(counts, dtype=np.int32),
        np.asarray(vids, dtype=np.int32),
        pos,
        np.asarray(uv, dtype=np.float32),
        face_mesh=np.asarray(face_mesh, dtype=np.int32),
        mesh_uuids=list(uuids),
        ts_faces={"Body": list(range(len(counts)))},
    )


def _quad_tri(rotate=False, uuids=("MESH-A",), face_mesh=(0, 0), uv_shift=0.0):
    if rotate:
        vids = [1, 2, 3, 0, 4, 5, 6]
        uv = [
            [1.0, 0.0],
            [1.0, 1.0],
            [0.0, 1.0],
            [0.0, 0.0],
            [0.2, 0.2],
            [0.8, 0.2],
            [0.5, 0.9],
        ]
    else:
        vids = [0, 1, 2, 3, 4, 5, 6]
        uv = [
            [0.0, 0.0],
            [1.0, 0.0],
            [1.0, 1.0],
            [0.0, 1.0],
            [0.2, 0.2],
            [0.8, 0.2],
            [0.5, 0.9],
        ]
    uv = [[u + uv_shift, v] for u, v in uv]
    return _snap([4, 3], vids, uv, face_mesh, uuids)


def test_plan_uvold_matches_saved_snapshots_and_rejects_rotation(tmp_path):
    baseline = _quad_tri()
    candidate = _quad_tri(uv_shift=0.25)
    core.save_snapshot(tmp_path / "base", baseline)
    core.save_snapshot(tmp_path / "cand", candidate)
    loaded_base = core.load_snapshot(tmp_path / "base")
    loaded_cand = core.load_snapshot(tmp_path / "cand")
    mapping = {"|mesh|shapeA": "MESH-A"}
    plan, warning = um.plan_uvold(loaded_base, loaded_cand, mapping)
    assert warning is None
    assert set(plan) == {"|mesh|shapeA"}
    assert plan["|mesh|shapeA"]["uv_counts"].tolist() == [4, 3]
    assert int(plan["|mesh|shapeA"]["uv_ids"].size) == 7

    rotated = _quad_tri(rotate=True)
    assert core.compute_topology_fingerprint(baseline) == core.compute_topology_fingerprint(rotated)
    _plan, code = um.plan_uvold(baseline, rotated, mapping)
    assert code == um.UVOLD_TOPOLOGY_MISMATCH


def test_plan_uvold_rejects_topology_and_uuid_differences():
    base = _quad_tri()
    mapping = {"|mesh|shapeA": "MESH-A"}
    extra_face = _snap(
        [4, 3, 3],
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        [[0, 0], [1, 0], [1, 1], [0, 1], [0.2, 0.2], [0.8, 0.2], [0.5, 0.9], [0, 0], [1, 0], [0, 1]],
        [0, 0, 0],
        ["MESH-A"],
    )
    assert um.plan_uvold(base, extra_face, mapping)[1] == um.UVOLD_TOPOLOGY_MISMATCH
    pentagon = _snap(
        [5, 3],
        [0, 1, 2, 3, 8, 4, 5, 6],
        [[0, 0], [1, 0], [1, 1], [0.5, 1], [0, 1], [0.2, 0.2], [0.8, 0.2], [0.5, 0.9]],
        [0, 0],
        ["MESH-A"],
    )
    assert um.plan_uvold(base, pentagon, mapping)[1] == um.UVOLD_TOPOLOGY_MISMATCH
    reordered = _quad_tri(uuids=("MESH-B", "MESH-A"), face_mesh=(0, 1))
    two = _quad_tri(uuids=("MESH-A", "MESH-B"), face_mesh=(0, 1))
    assert um.plan_uvold(two, reordered, {
        "|a": "MESH-A", "|b": "MESH-B",
    })[1] == um.UVOLD_TOPOLOGY_MISMATCH
    split = _quad_tri(face_mesh=(0, 1), uuids=("MESH-A", "MESH-B"))
    assert um.plan_uvold(base, split, mapping)[1] == um.UVOLD_TOPOLOGY_MISMATCH
    assert um.plan_uvold(base, base, {"|a": "OTHER"})[1] == um.UVOLD_UUID_UNRESOLVED
    assert um.plan_uvold(None, base, mapping)[1] == um.UVOLD_BASELINE_UNREADABLE
    assert um.plan_uvold(object(), base, mapping)[1] == um.UVOLD_BASELINE_UNREADABLE


def test_plan_uvold_slices_each_mesh_and_ignores_uuid_case():
    baseline = _quad_tri(uuids=("AbC", "DeF"), face_mesh=(0, 1))
    candidate = _quad_tri(uuids=("AbC", "DeF"), face_mesh=(0, 1), uv_shift=0.5)
    plan, warning = um.plan_uvold(baseline, candidate, {
        "|grp|bodyShape": "abc",
        "|grp|trimShape": "def",
    })
    assert warning is None
    assert plan["|grp|bodyShape"]["uv_counts"].tolist() == [4]
    assert plan["|grp|trimShape"]["uv_counts"].tolist() == [3]
    body_u = plan["|grp|bodyShape"]["u"]
    assert float(np.min(body_u)) == pytest.approx(0.0)
    assert float(np.max(body_u)) == pytest.approx(1.0)


class FakeCmds:
    def __init__(self, shapes, undo=True, switch_on_create=False):
        self.uv_sets = {shape: ["map1"] for shape in shapes}
        self.current = {shape: "map1" for shape in shapes}
        self.undo = undo
        self.switch_on_create = switch_on_create
        self.log = []
        self.create_fail = set()
        self.delete_fail = set()
        self.undo_fail = None

    def undoInfo(self, query=False, stateWithoutFlush=None, **_kwargs):
        if self.undo_fail == "query" and query:
            raise RuntimeError("undo query")
        if self.undo_fail == "set" and not query:
            raise RuntimeError("undo set")
        if query:
            self.log.append(("undo_query",))
            return self.undo
        self.log.append(("undo_set", stateWithoutFlush))
        self.undo = stateWithoutFlush

    def polyUVSet(
        self, shape, query=False, allUVSets=False, currentUVSet=False,
        create=False, delete=False, uvSet=None, **_kwargs,
    ):
        if query and allUVSets:
            self.log.append(("query_all", shape))
            return list(self.uv_sets[shape])
        if query and currentUVSet:
            self.log.append(("query_current", shape))
            return self.current[shape]
        if create:
            self.log.append(("create", shape, uvSet))
            if shape in self.create_fail:
                raise RuntimeError("create")
            self.uv_sets[shape].append(uvSet)
            if self.switch_on_create:
                self.current[shape] = uvSet
            return uvSet
        if delete:
            self.log.append(("delete", shape, uvSet))
            if shape in self.delete_fail:
                raise RuntimeError("delete")
            names = self.uv_sets[shape]
            if uvSet in names:
                names.remove(uvSet)
            return None
        if currentUVSet and not query:
            self.log.append(("set_current", shape, uvSet))
            self.current[shape] = uvSet
            return uvSet
        raise AssertionError("unexpected polyUVSet")


def _significant(log):
    names = {"undo_set", "create", "fill", "set_current", "export", "delete"}
    return [item[0] for item in log if item[0] in names]


def _export_recorder(log, fail=None):
    def export_fn(shape_paths, meshes_dir, baseline_id, triangulate=False, **kwargs):
        log.append(("export", list(shape_paths), meshes_dir, baseline_id, triangulate, kwargs))
        if fail is not None:
            raise fail
        return {"path": "mesh.fbx", "size": 1, "sha1": "ab" * 20}
    return export_fn


def _fill_recorder(log, fail_on=None):
    seen = {"n": 0}

    def fill_fn(cmds, shape, name, u_values, v_values, uv_counts, uv_ids):
        seen["n"] += 1
        log.append(("fill", shape, name))
        if fail_on is not None and seen["n"] == fail_on:
            raise RuntimeError("fill")
        assert int(np.asarray(uv_ids).size) == int(np.asarray(uv_counts).sum())

    return fill_fn


def test_export_with_uvold_creates_and_restores(tmp_path):
    shape = "|grp|pSphereShape1"
    cmds = FakeCmds([shape])
    log = cmds.log
    snap = _snap(
        [4], [0, 1, 2, 3],
        [[0, 0], [1, 0], [1, 1], [0, 1]],
        [0], ["UUID-A"],
    )
    mesh, info = um.export_rebase_mesh_with_uvold(
        [shape], tmp_path, "bl-1",
        triangulate=False,
        baseline_snapshot=snap,
        candidate_snapshot=snap,
        cmds=cmds,
        uuid_fn=lambda _shape: "UUID-A",
        fill_uvs_fn=_fill_recorder(log),
        export_fn=_export_recorder(log),
    )
    assert mesh["path"] == "mesh.fbx"
    assert info["applied"] is True
    assert info["name"] == "uvOld"
    assert info["warnings"] == []
    assert info["shapes"] == []
    assert _significant(log) == [
        "undo_set",
        "create",
        "fill",
        "set_current",
        "export",
        "set_current",
        "delete",
        "undo_set",
    ]
    assert cmds.uv_sets[shape] == ["map1"]
    assert cmds.current[shape] == "map1"
    assert cmds.undo is True
    delete_at = [index for index, item in enumerate(log) if item[0] == "delete"][-1]
    undo_restore = [index for index, item in enumerate(log) if item[0] == "undo_set"][-1]
    assert any(item[0] == "query_all" and index > delete_at for index, item in enumerate(log))
    assert undo_restore > delete_at


@pytest.mark.parametrize("fail", [
    um.RebaseExportError("メッシュの書き出しに失敗しました"),
    KeyboardInterrupt(),
])
def test_export_failure_still_cleans_uvold(tmp_path, fail):
    shape = "|grp|pSphereShape1"
    cmds = FakeCmds([shape])
    snap = _snap(
        [4], [0, 1, 2, 3],
        [[0, 0], [1, 0], [1, 1], [0, 1]],
        [0], ["UUID-A"],
    )
    with pytest.raises(type(fail)):
        um.export_rebase_mesh_with_uvold(
            [shape], tmp_path, "bl-1",
            baseline_snapshot=snap,
            candidate_snapshot=snap,
            cmds=cmds,
            uuid_fn=lambda _shape: "UUID-A",
            fill_uvs_fn=_fill_recorder(cmds.log),
            export_fn=_export_recorder(cmds.log, fail=fail),
        )
    assert cmds.uv_sets[shape] == ["map1"]
    assert cmds.current[shape] == "map1"
    assert cmds.undo is True


def test_export_and_cleanup_failure_marks_the_exception(tmp_path):
    shape = "|grp|pSphereShape1"
    cmds = FakeCmds([shape])
    cmds.delete_fail.add(shape)
    snap = _snap(
        [4], [0, 1, 2, 3],
        [[0, 0], [1, 0], [1, 1], [0, 1]],
        [0], ["UUID-A"],
    )
    with pytest.raises(um.RebaseExportError) as caught:
        um.export_rebase_mesh_with_uvold(
            [shape], tmp_path, "bl-1",
            baseline_snapshot=snap,
            candidate_snapshot=snap,
            cmds=cmds,
            uuid_fn=lambda _shape: "UUID-A",
            fill_uvs_fn=_fill_recorder(cmds.log),
            export_fn=_export_recorder(
                cmds.log, fail=um.RebaseExportError("メッシュの書き出しに失敗しました"),
            ),
        )
    info = getattr(caught.value, "uvold_info", None)
    assert isinstance(info, dict)
    assert um.UVOLD_CLEANUP_FAILED in info["warnings"]
    assert info["name"] == "uvOld"
    assert info["shapes"] == ["pSphereShape1"]
    assert "/" not in "".join(info["shapes"])
    assert cmds.undo is True


def test_create_or_fill_failure_exports_without_uvold():
    shapes = ["|a|shapeA", "|b|shapeB"]
    snap = _quad_tri(uuids=("UUID-A", "UUID-B"), face_mesh=(0, 1))
    uuids = {"|a|shapeA": "UUID-A", "|b|shapeB": "UUID-B"}

    cmds = FakeCmds(shapes)
    cmds.create_fail.add("|b|shapeB")
    _mesh, info = um.export_rebase_mesh_with_uvold(
        shapes, Path("."), "bl-1",
        baseline_snapshot=snap, candidate_snapshot=snap,
        cmds=cmds, uuid_fn=lambda shape: uuids[shape],
        fill_uvs_fn=_fill_recorder(cmds.log),
        export_fn=_export_recorder(cmds.log),
    )
    assert info["applied"] is False
    assert um.UVOLD_CREATE_FAILED in info["warnings"]
    assert any(item[0] == "export" for item in cmds.log)
    assert cmds.uv_sets["|a|shapeA"] == ["map1"]
    assert cmds.uv_sets["|b|shapeB"] == ["map1"]

    cmds = FakeCmds(shapes)
    _mesh, info = um.export_rebase_mesh_with_uvold(
        shapes, Path("."), "bl-1",
        baseline_snapshot=snap, candidate_snapshot=snap,
        cmds=cmds, uuid_fn=lambda shape: uuids[shape],
        fill_uvs_fn=_fill_recorder(cmds.log, fail_on=2),
        export_fn=_export_recorder(cmds.log),
    )
    assert info["applied"] is False
    assert um.UVOLD_FILL_FAILED in info["warnings"]
    assert cmds.uv_sets["|a|shapeA"] == ["map1"]
    assert cmds.uv_sets["|b|shapeB"] == ["map1"]
    assert any(item[0] == "export" for item in cmds.log)


def test_delete_failure_keeps_export_and_names_the_shape():
    shapes = ["|grp|shapeA", "|grp|shapeB"]
    cmds = FakeCmds(shapes)
    cmds.delete_fail.add("|grp|shapeA")
    snap = _quad_tri(uuids=("UUID-A", "UUID-B"), face_mesh=(0, 1))
    uuids = {shapes[0]: "UUID-A", shapes[1]: "UUID-B"}
    mesh, info = um.export_rebase_mesh_with_uvold(
        shapes, Path("."), "bl-1",
        baseline_snapshot=snap, candidate_snapshot=snap,
        cmds=cmds, uuid_fn=lambda shape: uuids[shape],
        fill_uvs_fn=_fill_recorder(cmds.log),
        export_fn=_export_recorder(cmds.log),
    )
    assert mesh["sha1"]
    assert info["applied"] is True
    assert um.UVOLD_CLEANUP_FAILED in info["warnings"]
    assert info["shapes"] == ["shapeA"]
    assert "uvOld" not in cmds.uv_sets["|grp|shapeB"]
    assert "/" not in "".join(info["shapes"])


def test_existing_uvold_name_is_not_deleted():
    shape = "|grp|shapeA"
    cmds = FakeCmds([shape])
    cmds.uv_sets[shape] = ["uvOld"]
    cmds.current[shape] = "uvOld"
    snap = _snap(
        [4], [0, 1, 2, 3],
        [[0, 0], [1, 0], [1, 1], [0, 1]],
        [0], ["UUID-A"],
    )
    _mesh, info = um.export_rebase_mesh_with_uvold(
        [shape], Path("."), "bl-1",
        baseline_snapshot=snap, candidate_snapshot=snap,
        cmds=cmds, uuid_fn=lambda _shape: "UUID-A",
        fill_uvs_fn=_fill_recorder(cmds.log),
        export_fn=_export_recorder(cmds.log),
    )
    assert info["applied"] is True
    assert info["name"] == "uvOld_1"
    assert cmds.uv_sets[shape] == ["uvOld"]
    assert cmds.current[shape] == "uvOld"
    assert not any(item[:3] == ("delete", shape, "uvOld") for item in cmds.log)


def test_multi_uv_set_skips_every_shape():
    shapes = ["|a|shapeA", "|b|shapeB"]
    cmds = FakeCmds(shapes)
    cmds.uv_sets["|b|shapeB"] = ["map1", "map2"]
    snap = _quad_tri(uuids=("UUID-A", "UUID-B"), face_mesh=(0, 1))
    uuids = {shapes[0]: "UUID-A", shapes[1]: "UUID-B"}
    _mesh, info = um.export_rebase_mesh_with_uvold(
        shapes, Path("."), "bl-1",
        baseline_snapshot=snap, candidate_snapshot=snap,
        cmds=cmds, uuid_fn=lambda shape: uuids[shape],
        fill_uvs_fn=_fill_recorder(cmds.log),
        export_fn=_export_recorder(cmds.log),
    )
    assert info["applied"] is False
    assert um.UVOLD_MULTI_UV_SET in info["warnings"]
    assert not any(item[0] == "create" for item in cmds.log)
    assert not any(item[0] == "delete" for item in cmds.log)
    assert any(item[0] == "export" for item in cmds.log)
    assert cmds.uv_sets["|a|shapeA"] == ["map1"]
    assert cmds.uv_sets["|b|shapeB"] == ["map1", "map2"]


def test_plan_failure_does_not_touch_uv_sets():
    shape = "|grp|shapeA"
    cmds = FakeCmds([shape])
    base = _snap(
        [4], [0, 1, 2, 3],
        [[0, 0], [1, 0], [1, 1], [0, 1]],
        [0], ["UUID-A"],
    )
    other = _snap(
        [3], [0, 1, 2],
        [[0, 0], [1, 0], [0, 1]],
        [0], ["UUID-A"],
    )
    _mesh, info = um.export_rebase_mesh_with_uvold(
        [shape], Path("."), "bl-1",
        baseline_snapshot=base, candidate_snapshot=other,
        cmds=cmds, uuid_fn=lambda _shape: "UUID-A",
        fill_uvs_fn=_fill_recorder(cmds.log),
        export_fn=_export_recorder(cmds.log),
    )
    assert info["applied"] is False
    assert um.UVOLD_TOPOLOGY_MISMATCH in info["warnings"]
    assert not any(item[0] in ("create", "delete", "query_all") for item in cmds.log)

    cmds = FakeCmds([shape])
    _mesh, info = um.export_rebase_mesh_with_uvold(
        [shape], Path("."), "bl-1",
        baseline_snapshot=None, candidate_snapshot=base,
        cmds=cmds, uuid_fn=lambda _shape: "UUID-A",
        fill_uvs_fn=_fill_recorder(cmds.log),
        export_fn=_export_recorder(cmds.log),
    )
    assert um.UVOLD_NO_BASELINE in info["warnings"]
    assert not any(item[0] in ("create", "delete") for item in cmds.log)
    assert any(item[0] == "export" for item in cmds.log)


def test_create_switches_current_uv_set_back_immediately():
    shape = "|grp|shapeA"
    cmds = FakeCmds([shape], switch_on_create=True)
    snap = _snap(
        [4], [0, 1, 2, 3],
        [[0, 0], [1, 0], [1, 1], [0, 1]],
        [0], ["UUID-A"],
    )
    um.export_rebase_mesh_with_uvold(
        [shape], Path("."), "bl-1",
        baseline_snapshot=snap, candidate_snapshot=snap,
        cmds=cmds, uuid_fn=lambda _shape: "UUID-A",
        fill_uvs_fn=_fill_recorder(cmds.log),
        export_fn=_export_recorder(cmds.log),
    )
    fill_at = next(index for index, item in enumerate(cmds.log) if item[0] == "fill")
    export_at = next(index for index, item in enumerate(cmds.log) if item[0] == "export")
    restored = [
        item for item in cmds.log[fill_at:export_at] if item[0] == "set_current"
    ]
    assert restored == [("set_current", shape, "map1")]
    assert cmds.current[shape] == "map1"


def test_missing_or_failing_undo_does_not_crash():
    shape = "|grp|shapeA"
    snap = _snap(
        [4], [0, 1, 2, 3],
        [[0, 0], [1, 0], [1, 1], [0, 1]],
        [0], ["UUID-A"],
    )

    class NoUndo:
        def __init__(self):
            self.inner = FakeCmds([shape])

        def polyUVSet(self, *args, **kwargs):
            return self.inner.polyUVSet(*args, **kwargs)

    bare = NoUndo()
    _mesh, info = um.export_rebase_mesh_with_uvold(
        [shape], Path("."), "bl-1",
        baseline_snapshot=snap, candidate_snapshot=snap,
        cmds=bare, uuid_fn=lambda _shape: "UUID-A",
        fill_uvs_fn=_fill_recorder(bare.inner.log),
        export_fn=_export_recorder(bare.inner.log),
    )
    assert info["applied"] is True

    cmds = FakeCmds([shape])
    cmds.undo_fail = "query"
    _mesh, info = um.export_rebase_mesh_with_uvold(
        [shape], Path("."), "bl-1",
        baseline_snapshot=snap, candidate_snapshot=snap,
        cmds=cmds, uuid_fn=lambda _shape: "UUID-A",
        fill_uvs_fn=_fill_recorder(cmds.log),
        export_fn=_export_recorder(cmds.log),
    )
    assert info["applied"] is True

    cmds = FakeCmds([shape], undo=False)
    um.export_rebase_mesh_with_uvold(
        [shape], Path("."), "bl-1",
        baseline_snapshot=snap, candidate_snapshot=snap,
        cmds=cmds, uuid_fn=lambda _shape: "UUID-A",
        fill_uvs_fn=_fill_recorder(cmds.log),
        export_fn=_export_recorder(cmds.log),
    )
    assert cmds.undo is False
    assert [item for item in cmds.log if item[0] == "undo_set"] == [
        ("undo_set", False),
        ("undo_set", False),
    ]


def test_export_fn_arguments_pass_through(tmp_path):
    shape = "|grp|shapeA"
    cmds = FakeCmds([shape])
    seen = {}

    def export_fn(shape_paths, meshes_dir, baseline_id, triangulate=False, **kwargs):
        seen["args"] = (list(shape_paths), meshes_dir, baseline_id, triangulate)
        return {"path": "mesh.fbx", "size": 1, "sha1": "cd" * 20}

    um.export_rebase_mesh_with_uvold(
        [shape], tmp_path, "bl-9",
        triangulate=True,
        baseline_snapshot=None,
        candidate_snapshot=None,
        cmds=cmds,
        export_fn=export_fn,
        mel_eval="mel",
        replace_fn="replace",
        remove_fn="remove",
        tmp_style="infix",
    )
    assert seen["args"] == ([shape], tmp_path, "bl-9", True)


def test_reason_text_skips_cleanup_code():
    assert "複数" in um.uvold_reason_text([um.UVOLD_MULTI_UV_SET])
    assert um.uvold_reason_text([um.UVOLD_CLEANUP_FAILED, um.UVOLD_FILL_FAILED])
    text = um.uvold_reason_text([um.UVOLD_CLEANUP_FAILED, um.UVOLD_FILL_FAILED])
    assert "埋め" in text
