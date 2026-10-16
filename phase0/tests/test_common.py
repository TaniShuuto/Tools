# -*- coding: utf-8 -*-
import json
import os

import common


def test_anonymize_path_replaces_home(tmp_path, monkeypatch):
    home = str(tmp_path / "Someone")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("USER", "Someone")
    monkeypatch.setenv("USERNAME", "Someone")
    target = os.path.join(home, "Documents", "file.png")
    out = common.anonymize_path(target)
    assert "Someone" not in out
    assert out.startswith("~")
    assert "file.png" in out.replace("\\", "/")


def test_anonymize_text_strips_email_and_user(tmp_path, monkeypatch):
    home = str(tmp_path / "Pat")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("USER", "Pat")
    monkeypatch.setenv("USERNAME", "Pat")
    text = "boom at {0} contact a.b@example.com".format(home)
    out = common.anonymize_text(text)
    assert "example.com" not in out
    assert "<email>" in out
    assert home not in out
    assert "Pat" not in out


def test_safe_getattr_and_dir():
    class Box:
        x = 3

        def __dir__(self):
            return ["x", "y"]

    box = Box()
    assert common.safe_getattr(box, "x") == 3
    err = common.safe_getattr(box, "missing")
    assert err["_getattr_error"]["type"] == "AttributeError"
    names = common.safe_dir(box)
    assert "x" in names


def test_exception_observation_has_type_and_message():
    try:
        raise ValueError("nope")
    except ValueError as exc:
        obs = common.exception_observation(exc)
    assert obs["type"] == "ValueError"
    assert "nope" in obs["message"]


def test_make_check_and_write_results_merge(tmp_path):
    path = str(tmp_path / "out.json")
    first = common.make_check(common.STATUS_PASS, "one", {"n": 1})
    common.write_results(path, {"V1": first}, env={"python_version": "3.x"})
    second = common.make_check(common.STATUS_SKIPPED, "two", {"n": 2})
    payload = common.write_results(path, {"V8": second}, env={"python_version": "3.x"})
    assert payload["schema"] == 1
    assert "V1" in payload["checks"]
    assert "V8" in payload["checks"]
    assert payload["checks"]["V1"]["status"] == "PASS"
    assert payload["path"]
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["checks"]["V8"]["status"] == "SKIPPED"


def test_collect_env_has_no_raw_home():
    env = common.collect_env()
    home = os.path.expanduser("~")
    dumped = json.dumps(env)
    # ホーム絶対パスが残っていたら失敗
    if len(home) > 3:
        assert home.replace("\\", "/") not in dumped.replace("\\", "/")


def test_probe_module_missing():
    info = common.probe_module("definitely_not_a_real_module_phase0")
    assert info["installed"] is False
    assert info["error"]["type"]


def test_json_safe_enum_like():
    class E:
        name = "SUCCESS"
        value = 1

    out = common.json_safe(E())
    assert out["name"] == "SUCCESS"
    assert out["value"] == 1


def test_json_safe_scalars_ignore_depth():
    assert common.json_safe("keep", depth=99) == "keep"
    assert common.json_safe(23, depth=99) == 23
    assert common.json_safe(False, depth=99) is False
    assert common.json_safe(None, depth=99) is None
    nested = {"_truncated": True, "type": "dict"}
    assert common.json_safe({"a": {"b": 1}}, depth=common.JSON_SAFE_MAX_DEPTH + 1) == nested


def test_json_safe_list_over_200_records_total():
    items = list(range(250))
    out = common.json_safe(items)
    assert out["_total"] == 250
    assert out["_items"] == list(range(200))
    assert common.json_safe(list(range(200))) == list(range(200))


def test_json_safe_preserves_v4_like_nested_scalars(tmp_path):
    """実機と同じ入れ子(checks.V4.observations.meshes[].api.*)で欠落しない。"""
    shader_names = ["phase0_v4_cube_matASG", "phase0_v4_cube_matBSG"]
    face_indices = [0, 0, 0, 1, 1, -1]
    face_sg = [
        "phase0_v4_cube_matASG",
        "phase0_v4_cube_matASG",
        "phase0_v4_cube_matASG",
        "phase0_v4_cube_matBSG",
        "phase0_v4_cube_matBSG",
        None,
    ]
    cmds_face_sg = [
        "phase0_v4_cube_matASG",
        "phase0_v4_cube_matASG",
        "phase0_v4_cube_matASG",
        "phase0_v4_cube_matBSG",
        "phase0_v4_cube_matBSG",
        "initialShadingGroup",
    ]
    sphere_cmds = ["matA"] * 24
    sphere_cmds[5] = "initialShadingGroup"
    sphere_cmds[23] = "initialShadingGroup"
    diffs = [
        {
            "face": 5,
            "api": None,
            "cmds": "initialShadingGroup",
            "kind": "unassigned_none_vs_initial",
        },
    ]
    obs = {
        "meshes": [
            {
                "label": "cube_instance1",
                "instance": 1,
                "api": {
                    "shader_names": shader_names,
                    "face_indices": face_indices,
                    "face_sg": face_sg,
                },
                "cmds_face_sg": cmds_face_sg,
                "compare": {
                    "n_faces": 6,
                    "n_diff": 1,
                    "diffs": diffs,
                    "diffs_head": diffs,
                },
            },
            {
                "label": "sphere_instance0",
                "cmds_face_sg": sphere_cmds,
                "compare": {
                    "n_diff": 1,
                    "diffs": [{"face": 23, "api": None, "cmds": "initialShadingGroup"}],
                },
            },
        ],
    }
    check = common.make_check(common.STATUS_PASS, "nested", obs)
    path = str(tmp_path / "phase0_results_maya.json")
    payload = common.write_results(path, {"V4": check}, env={"python_version": "3.x"})
    api = payload["checks"]["V4"]["observations"]["meshes"][0]["api"]
    assert api["shader_names"] == shader_names
    assert api["face_indices"] == face_indices
    assert api["face_sg"] == face_sg
    cmp_row = payload["checks"]["V4"]["observations"]["meshes"][0]["compare"]
    assert cmp_row["diffs"][0]["face"] == 5
    assert cmp_row["diffs"][0]["cmds"] == "initialShadingGroup"
    assert cmp_row["diffs_head"][0]["kind"] == "unassigned_none_vs_initial"
    sphere = payload["checks"]["V4"]["observations"]["meshes"][1]
    assert sphere["cmds_face_sg"][5] == "initialShadingGroup"
    assert sphere["cmds_face_sg"][23] == "initialShadingGroup"
    assert not isinstance(api["shader_names"][0], dict)


def test_collect_env_includes_maya_about():
    env = common.collect_env()
    assert "maya" in env
    assert set(env["maya"]["about"].keys()) >= {"version", "apiVersion", "product"}
    assert "available" in env["maya"]
