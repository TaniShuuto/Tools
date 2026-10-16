# -*- coding: utf-8 -*-
from v4_connected_shaders import (
    KIND_REAL,
    KIND_UNASSIGNED,
    compare_instance_faces,
    evaluate_meshes,
    face_range,
)


def test_face_range_inclusive():
    assert face_range("mesh", 0, 11) == "mesh.f[0:11]"
    assert face_range("mesh", 23, 23) == "mesh.f[23]"
    assert face_range("sphere", 12, 22) == "sphere.f[12:22]"


def test_compare_classifies_unassigned_and_real():
    api = {
        "face_sg": ["A", "A", "B", None, "B", None],
    }
    cmds_list = ["A", "A", "B", "initialShadingGroup", "B", "C"]
    row = compare_instance_faces(api, cmds_list)
    assert row["n_diff"] == 2
    assert row["n_unassigned_none_vs_initial"] == 1
    assert row["n_real_mismatch"] == 1
    assert row["diffs"][0] == {
        "face": 3,
        "api": None,
        "cmds": "initialShadingGroup",
        "kind": KIND_UNASSIGNED,
    }
    assert row["diffs"][1]["face"] == 5
    assert row["diffs"][1]["kind"] == KIND_REAL
    assert row["match"] is False
    assert row["match_ignoring_unassigned"] is False


def test_evaluate_pass_when_only_unassigned_diff():
    meshes = [
        {
            "label": "cube_instance1",
            "instance": 1,
            "api_error": None,
            "compare": compare_instance_faces(
                {"face_sg": ["A", "A", "A", "B", "B", None]},
                ["A", "A", "A", "B", "B", "initialShadingGroup"],
            ),
        },
        {
            "label": "sphere_instance0",
            "instance": 0,
            "api_error": None,
            "compare": compare_instance_faces(
                {"face_sg": ["A"] * 23 + [None]},
                ["A"] * 23 + ["initialShadingGroup"],
            ),
        },
    ]
    status, summary, extra = evaluate_meshes(meshes)
    assert status == "PASS"
    assert extra["unassigned_handling"]["treated_as"] == "not_fail"
    assert extra["unassigned_handling"]["meshes"][0]["faces"] == [5]
    assert extra["unassigned_handling"]["meshes"][1]["faces"] == [23]
    assert "real_mismatch" in summary or "未割当" in summary


def test_evaluate_fail_on_real_mismatch():
    meshes = [
        {
            "label": "cube_instance0",
            "instance": 0,
            "api_error": None,
            "compare": compare_instance_faces(
                {"face_sg": ["A", "B"]},
                ["A", "C"],
            ),
        },
    ]
    status, _summary, extra = evaluate_meshes(meshes)
    assert status == "FAIL"
    assert extra["unassigned_handling"]["meshes"] == []


def test_evaluate_inconclusive_without_api():
    status, _summary, extra = evaluate_meshes([
        {"label": "x", "api_error": {"type": "RuntimeError"}, "compare": {}},
    ])
    assert status == "INCONCLUSIVE"
    assert extra["unassigned_handling"] is None
