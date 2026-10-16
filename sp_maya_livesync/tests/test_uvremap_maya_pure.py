# -*- coding: utf-8 -*-
"""uvremap_maya.py の Maya 非依存ロジックのテスト。

maya.api / maya.cmds は関数の中に隠してあるため、このファイルは
通常の CPython + numpy + pytest で動く。合成データのみを使う。
"""
from __future__ import annotations

import ast
import os
import sys

import numpy as np
import pytest

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

import uvremap_maya as um  # noqa: E402


def _record(
    uuid,
    counts,
    vids,
    pos=None,
    uvs=None,
    tri_face=None,
    tri_corner=None,
    ts_local=None,
    **extra
):
    counts = np.asarray(counts, dtype=np.int32)
    vids = np.asarray(vids, dtype=np.int32)
    n_c = int(vids.size)
    n_f = int(counts.size)
    if pos is None:
        pos = np.zeros((n_c, 3), dtype=np.float32)
        if n_c:
            pos[:, 0] = np.arange(n_c, dtype=np.float32)
    if uvs is None:
        uvs = np.zeros((n_c, 2), dtype=np.float32)
    if tri_face is None:
        tri_face = np.zeros((0,), dtype=np.int32)
    if tri_corner is None:
        tri_corner = np.zeros((0, 3), dtype=np.int8)
    rec = {
        "uuid": uuid,
        "face_vertex_counts": counts,
        "corner_vid": vids,
        "corner_pos": np.asarray(pos, dtype=np.float32),
        "corner_uv": np.asarray(uvs, dtype=np.float32),
        "tri_face": np.asarray(tri_face, dtype=np.int32),
        "tri_corner": np.asarray(tri_corner, dtype=np.int8),
        "ts_local_faces": ts_local or {},
        "nonmanifold_faces": extra.get("nonmanifold_faces", []),
        "untriangulated_faces": extra.get("untriangulated_faces", []),
        "no_uv_faces": extra.get("no_uv_faces", []),
        "instance_mismatch": extra.get("instance_mismatch", False),
        "deformer_positions_used": extra.get("deformer_positions_used", False),
    }
    rec["_n_faces"] = n_f
    return rec


def test_import_does_not_load_maya():
    before = {name for name in sys.modules if name == "maya" or name.startswith("maya.")}
    import importlib
    importlib.reload(um)
    after = {name for name in sys.modules if name == "maya" or name.startswith("maya.")}
    assert after == before


def test_source_has_no_top_level_maya_or_core_import():
    source = open(um.__file__, "r", encoding="utf-8").read()
    tree = ast.parse(source)
    top = [node for node in tree.body]
    for node in top:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("maya")
                assert alias.name != "uvremap_core"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("maya")
            assert mod != "uvremap_core"


def test_map_triangles_quad_fan():
    counts = np.array([4], dtype=np.int32)
    vids = np.array([10, 20, 30, 40], dtype=np.int32)
    tri_counts = np.array([2], dtype=np.int32)
    tri_verts = np.array([10, 20, 30, 10, 30, 40], dtype=np.int32)
    tri_face, tri_corner, extras = um.map_triangles_to_local_corners(
        counts, vids, tri_counts, tri_verts,
    )
    assert extras["nonmanifold_faces"] == []
    assert extras["untriangulated_faces"] == []
    np.testing.assert_array_equal(tri_face, [0, 0])
    np.testing.assert_array_equal(tri_corner, [[0, 1, 2], [0, 2, 3]])
    assert tri_corner.dtype == np.int8


def test_map_triangles_keeps_reversed_winding():
    counts = np.array([3], dtype=np.int32)
    vids = np.array([2, 1, 0], dtype=np.int32)
    tri_counts = np.array([1], dtype=np.int32)
    tri_verts = np.array([2, 1, 0], dtype=np.int32)
    tri_face, tri_corner, extras = um.map_triangles_to_local_corners(
        counts, vids, tri_counts, tri_verts,
    )
    assert extras["nonmanifold_faces"] == []
    np.testing.assert_array_equal(tri_face, [0])
    np.testing.assert_array_equal(tri_corner, [[0, 1, 2]])


def test_map_triangles_rotated_ids():
    counts = np.array([3], dtype=np.int32)
    vids = np.array([10, 20, 30], dtype=np.int32)
    tri_counts = np.array([1], dtype=np.int32)
    tri_verts = np.array([30, 10, 20], dtype=np.int32)
    _tri_face, tri_corner, extras = um.map_triangles_to_local_corners(
        counts, vids, tri_counts, tri_verts,
    )
    np.testing.assert_array_equal(tri_corner, [[2, 0, 1]])
    assert extras["untriangulated_faces"] == []


def test_map_triangles_excludes_nonmanifold():
    counts = np.array([4, 3], dtype=np.int32)
    vids = np.array([0, 1, 0, 2, 3, 4, 5], dtype=np.int32)
    tri_counts = np.array([2, 1], dtype=np.int32)
    tri_verts = np.array([0, 1, 0, 0, 0, 2, 3, 4, 5], dtype=np.int32)
    tri_face, tri_corner, extras = um.map_triangles_to_local_corners(
        counts, vids, tri_counts, tri_verts,
    )
    assert extras["nonmanifold_faces"] == [0]
    assert extras["untriangulated_faces"] == []
    np.testing.assert_array_equal(tri_face, [1])
    np.testing.assert_array_equal(tri_corner, [[0, 1, 2]])


def test_map_triangles_excludes_empty_and_unknown_vertex():
    counts = np.array([3, 3], dtype=np.int32)
    vids = np.array([0, 1, 2, 3, 4, 5], dtype=np.int32)
    tri_counts = np.array([0, 1], dtype=np.int32)
    tri_verts = np.array([3, 4, 99], dtype=np.int32)
    tri_face, tri_corner, extras = um.map_triangles_to_local_corners(
        counts, vids, tri_counts, tri_verts,
    )
    assert extras["untriangulated_faces"] == [0, 1]
    assert tri_face.size == 0
    assert tri_corner.shape == (0, 3)


def test_assigned_uvs_nan_for_minus_one():
    counts = np.array([3, 3], dtype=np.int32)
    uv_counts = np.array([3, 3], dtype=np.int32)
    uv_ids = np.array([0, 1, 2, -1, -1, -1], dtype=np.int32)
    u_values = np.array([0.0, 0.5, 1.0], dtype=np.float32)
    v_values = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    corner_uv, no_uv = um.assigned_uvs_to_corner_uv(
        counts, uv_counts, uv_ids, u_values, v_values,
    )
    assert corner_uv.dtype == np.float32
    assert corner_uv.shape == (6, 2)
    np.testing.assert_allclose(corner_uv[:3, 0], [0.0, 0.5, 1.0])
    np.testing.assert_allclose(corner_uv[:3, 1], [0.1, 0.2, 0.3])
    assert np.all(np.isnan(corner_uv[3:]))
    assert no_uv == [1]


def test_assigned_uvs_zero_count_slow_path():
    counts = np.array([3, 3], dtype=np.int32)
    uv_counts = np.array([0, 3], dtype=np.int32)
    uv_ids = np.array([0, 1, 2], dtype=np.int32)
    u_values = np.array([0.0, 0.25, 0.5], dtype=np.float32)
    v_values = np.array([0.0, 0.25, 0.5], dtype=np.float32)
    corner_uv, no_uv = um.assigned_uvs_to_corner_uv(
        counts, uv_counts, uv_ids, u_values, v_values,
    )
    assert 0 in no_uv
    assert np.all(np.isnan(corner_uv[:3]))
    np.testing.assert_allclose(corner_uv[3:, 0], [0.0, 0.25, 0.5])


def test_concat_sorts_by_uuid_and_offsets_faces():
    rec_b = _record(
        "b-uuid",
        counts=[3, 3],
        vids=[0, 1, 2, 0, 2, 3],
        tri_face=[0, 1],
        tri_corner=[[0, 1, 2], [0, 1, 2]],
        ts_local={"Body": [0], "Trim": [1]},
        pos=np.array([[1, 0, 0], [2, 0, 0], [3, 0, 0], [1, 0, 0], [3, 0, 0], [4, 0, 0]], dtype=np.float32),
    )
    rec_a = _record(
        "a-uuid",
        counts=[4],
        vids=[0, 1, 2, 3],
        tri_face=[0, 0],
        tri_corner=[[0, 1, 2], [0, 2, 3]],
        ts_local={"Body": [0]},
        pos=np.array([[0, 1, 0], [1, 1, 0], [1, 2, 0], [0, 2, 0]], dtype=np.float32),
    )
    arrays, info = um.concat_records([rec_b, rec_a], ["Body", "Trim"], uv_set="map1")
    assert info["mesh_uuids"] == ["a-uuid", "b-uuid"]
    assert info["uv_set"] == "map1"
    np.testing.assert_array_equal(arrays["face_mesh"], [0, 1, 1])
    np.testing.assert_array_equal(arrays["face_vertex_counts"], [4, 3, 3])
    np.testing.assert_array_equal(arrays["corner_vid"], [0, 1, 2, 3, 0, 1, 2, 0, 2, 3])
    np.testing.assert_array_equal(arrays["tri_face"], [0, 0, 1, 2])
    np.testing.assert_array_equal(arrays["ts_faces__0"], [0, 1])
    np.testing.assert_array_equal(arrays["ts_faces__1"], [2])
    assert arrays["corner_pos"].shape == (10, 3)
    assert arrays["corner_uv"].shape == (10, 2)
    assert arrays["tri_corner"].dtype == np.int8
    assert info["bbox_min"][1] == 0.0
    assert info["bbox_max"][1] == 2.0


def test_concat_ts_faces_are_global_ids():
    rec0 = _record(
        "m0",
        counts=[3, 3, 3],
        vids=[0, 1, 2, 0, 2, 3, 0, 3, 4],
        ts_local={"A": [1, 2]},
    )
    rec1 = _record(
        "m1",
        counts=[3],
        vids=[0, 1, 2],
        ts_local={"A": [0], "B": [0]},
    )
    arrays, _info = um.concat_records([rec0, rec1], ["A", "B"])
    np.testing.assert_array_equal(arrays["ts_faces__0"], [1, 2, 3])
    np.testing.assert_array_equal(arrays["ts_faces__1"], [3])


def test_concat_nonmanifold_and_no_uv_face_ids_are_offset():
    rec0 = _record("u0", counts=[3], vids=[0, 1, 2])
    rec1 = _record(
        "u1",
        counts=[4, 3],
        vids=[0, 1, 0, 2, 3, 4, 5],
        nonmanifold_faces=[0],
        no_uv_faces=[1],
        untriangulated_faces=[],
        tri_face=[1],
        tri_corner=[[0, 1, 2]],
    )
    arrays, info = um.concat_records([rec0, rec1], ["Body"])
    assert info["nonmanifold_faces"] == [1]
    assert info["no_uv_faces"] == [2]
    assert um.WARN_NONMANIFOLD in info["warnings"]
    assert um.WARN_NO_UV in info["warnings"]
    np.testing.assert_array_equal(arrays["tri_face"], [2])


def test_concat_default_sg_warning_keeps_faces():
    rec = _record(
        "u0",
        counts=[3, 3],
        vids=[0, 1, 2, 3, 4, 5],
        ts_local={"initialShadingGroup": [1], "BodySG": [0]},
    )
    arrays, info = um.concat_records(
        [rec], ["BodySG", "initialShadingGroup"],
    )
    assert um.WARN_DEFAULT_SG in info["warnings"]
    np.testing.assert_array_equal(arrays["ts_faces__0"], [0])
    np.testing.assert_array_equal(arrays["ts_faces__1"], [1])


def test_concat_instance_and_deformer_warnings():
    rec = _record(
        "u0",
        counts=[3],
        vids=[0, 1, 2],
        instance_mismatch=True,
        deformer_positions_used=True,
    )
    _arrays, info = um.concat_records([rec], [])
    assert um.WARN_INSTANCE in info["warnings"]
    assert um.WARN_DEFORMER in info["warnings"]


def test_concat_empty_records():
    arrays, info = um.concat_records([], ["A", "B"], uv_set="map1")
    assert arrays["face_vertex_counts"].shape == (0,)
    assert arrays["corner_pos"].shape == (0, 3)
    assert arrays["tri_corner"].shape == (0, 3)
    assert arrays["ts_faces__0"].shape == (0,)
    assert arrays["ts_faces__1"].shape == (0,)
    assert info["mesh_uuids"] == []
    assert info["uv_set"] == "map1"


def test_is_default_sg():
    assert um.is_default_sg("initialShadingGroup") is True
    assert um.is_default_sg("|initialShadingGroup") is True
    assert um.is_default_sg("BodySG") is False
    assert um.is_default_sg(None) is False


def test_public_functions_exist():
    assert callable(um.list_texture_sets)
    assert callable(um.collect_arrays)


def test_concat_does_not_flip_corners():
    rec = _record(
        "u0",
        counts=[3],
        vids=[0, 1, 2],
        tri_face=[0],
        tri_corner=[[2, 1, 0]],
    )
    arrays, _info = um.concat_records([rec], [])
    np.testing.assert_array_equal(arrays["tri_corner"], [[2, 1, 0]])


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-v"]))
