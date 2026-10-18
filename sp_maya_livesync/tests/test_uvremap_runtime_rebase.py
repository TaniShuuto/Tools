# -*- coding: utf-8 -*-
"""uvremap_runtime.py のリベース完了処理(RB-b)。Maya 無し・Pillow 無し。

時刻は now_fn。cmds は FakeCmds。配列収集は合成配列。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uvremap_bridge as br  # noqa: E402
import uvremap_follow as follow  # noqa: E402
import uvremap_runtime as rt  # noqa: E402
from test_uvremap_runtime import (  # noqa: E402
    FIXED_NOW,
    FakeCmds,
    _make_runtime,
    _yes_exists,
)

REQ = "rb-20261019-053400-3fa2"
_RESULT_KEYS = {
    "status", "reason", "request_id", "baseline_id", "ts_names",
    "steps", "reverted", "failed_step", "error",
}
_STEP_NAMES = ("active", "reverted", "follow_reset", "history")


def _assert_result_keys(result):
    assert set(result) == _RESULT_KEYS
    assert set(result["steps"]) == set(_STEP_NAMES)


def _rebase_nodes():
    return {
        "file_body": {
            "fileTextureName": "/src/Final/Body_BaseColor.png",
            "uvTilingMode": rt.UV_TILING_UDIM,
        },
        "file_head": {
            "fileTextureName": "/src/Final/Head_BaseColor.png",
            "uvTilingMode": rt.UV_TILING_OFF,
        },
        "file_trim": {
            "fileTextureName": "/src/Final/Trim_BaseColor.png",
            "uvTilingMode": rt.UV_TILING_OFF,
        },
    }


def _switch(cmds, ts_name, out):
    rt.switch_ts_file_nodes(
        cmds, ts_name,
        [{"name": "{0}_BaseColor.png".format(ts_name), "tile": 1001, "sha1": "a"}],
        out, exists_fn=_yes_exists,
    )


def _prepare_rebase(tmp_path, cmds=None):
    """現行基準・候補・追従・切り替え済みノード・ローカル記録まで作る。"""
    if cmds is None:
        cmds = FakeCmds(_rebase_nodes())
    enabled = {"on": True}
    scene = {"id": "sceneA.ma"}
    runtime = _make_runtime(
        tmp_path,
        enabled=lambda: enabled["on"],
        cmds=cmds,
        scene_id_fn=lambda: scene["id"],
    )
    root = runtime._root_path
    original = runtime.record_baseline("proj", ["Body", "Head", "Cape"], ["mesh"])
    candidate = runtime.record_baseline_candidate(
        "proj", ["Body", "Head", "Cape"], ["mesh"],
    )
    follow.save_follow(root, {
        "schema": 1,
        "flag_mtime_ns": 424242,
        "ts": {
            "Body": {
                "source_sig": {"Body_BaseColor.png": [1, 2]},
                "last_job_id": "j-body",
                "scene_id": "sceneA.ma",
                "uv_fp": {"mesh": [1, 2]},
            },
            "Head": {
                "source_sig": {},
                "last_job_id": "j-head",
                "scene_id": "sceneA.ma",
                "uv_fp": {},
            },
            "Trim": {
                "source_sig": {"Trim_BaseColor.png": [3, 4]},
                "last_job_id": "j-trim",
                "scene_id": "sceneA.ma",
                "uv_fp": {"mesh": [9, 9]},
            },
        },
    })
    out = tmp_path / "output" / "final"
    _switch(cmds, "Body", out)
    _switch(cmds, "Head", out)
    for name in ("Body", "Head", "Cape"):
        runtime._set_status(root, name, rt.STATE_CONVERTED_OK, 4, "busy", runtime._now())
    runtime.register_rebase(
        "proj", REQ, candidate, ["Body", "Head", "Cape"],
    )
    return {
        "runtime": runtime,
        "cmds": cmds,
        "root": root,
        "original": original,
        "candidate": candidate,
        "enabled": enabled,
        "scene": scene,
        "out": out,
    }


def _install_order_wrappers(monkeypatch):
    """完了処理が呼ぶ 4 つを、順番を残してから本物へ渡す。"""
    order = []
    orig_write = rt._write_active_baseline
    orig_revert = rt.revert_ts_file_nodes
    orig_save = follow.save_follow

    def wrapped_write(root, baseline_id):
        order.append("active")
        return orig_write(root, baseline_id)

    def wrapped_revert(cmds, ts_name=None):
        order.append("reverted")
        return orig_revert(cmds, ts_name=ts_name)

    def wrapped_save(root, data):
        order.append("follow_reset")
        return orig_save(root, data)

    monkeypatch.setattr(rt, "_write_active_baseline", wrapped_write)
    monkeypatch.setattr(rt, "revert_ts_file_nodes", wrapped_revert)
    monkeypatch.setattr(follow, "save_follow", wrapped_save)
    return order, orig_write, orig_revert, orig_save


# ---------------------------------------------------------------------------
# 1. 候補の作成。active.json が無いときは作らない
# ---------------------------------------------------------------------------
def test_candidate_does_not_create_active_json(tmp_path):
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    baseline_id = runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    assert baseline_id.startswith("bl-")
    folder = root / "baseline" / baseline_id
    assert (folder / "mesh.npz").is_file()
    assert (folder / "meta.json").is_file()
    assert not (root / "baseline" / "active.json").exists()


# ---------------------------------------------------------------------------
# 2. active.json があるとき、候補を作ってもポインタは不変
# ---------------------------------------------------------------------------
def test_candidate_leaves_existing_active_untouched(tmp_path):
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    original = runtime.record_baseline("proj", ["Body"], ["mesh"])
    active = root / "baseline" / "active.json"
    blob = active.read_bytes()
    mtime = active.stat().st_mtime_ns
    candidate = runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    assert candidate != original
    assert candidate.startswith("bl-")
    assert active.read_bytes() == blob
    assert active.stat().st_mtime_ns == mtime
    assert rt._read_active_baseline(root) == original
    assert runtime.record_baseline("proj", ["Body"], ["mesh"]) == original
    ids = rt._list_baseline_ids(root)
    assert original in ids and candidate in ids
    assert rt._read_active_baseline(root) == original


# ---------------------------------------------------------------------------
# 3. ハザード: 基準未記録の候補は、事実上の基準になる(挙動は変えない)
# ---------------------------------------------------------------------------
def test_candidate_without_active_json_becomes_de_facto_baseline(tmp_path):
    """基準が未記録のときにリベースの候補を作ると、候補が事実上の基準になる。

    active.json が無く baseline/ に候補だけがあるとき、record_baseline(force なし)は
    _list_baseline_ids の先頭(その候補)を返す。この挙動は変えない。
    """
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    candidate = runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    active = root / "baseline" / "active.json"
    assert not active.exists()
    assert rt._list_baseline_ids(root)[0] == candidate
    assert runtime.record_baseline("proj", ["Body"], ["mesh"]) == candidate
    assert not active.exists()


# ---------------------------------------------------------------------------
# 4. ID の衝突
# ---------------------------------------------------------------------------
def test_candidate_id_collision_retries_with_a_new_id(tmp_path):
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    existing = "bl-collide"
    old = root / "baseline" / existing
    old.mkdir(parents=True)
    (old / "keep.txt").write_text("stay", encoding="utf-8")
    real = runtime._next_id
    calls = {"n": 0}

    def next_id(prefix, iso):
        calls["n"] += 1
        if calls["n"] == 1:
            return existing
        return real(prefix, iso)

    runtime._next_id = next_id
    new_id = runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    assert calls["n"] == 2
    assert new_id != existing
    assert new_id.startswith("bl-")
    assert (old / "keep.txt").read_text(encoding="utf-8") == "stay"
    assert not (old / "mesh.npz").exists()
    assert (root / "baseline" / new_id / "mesh.npz").is_file()
    assert (root / "baseline" / new_id / "meta.json").is_file()
    assert not (root / "baseline" / "active.json").exists()


def test_candidate_id_collision_eight_times_raises(tmp_path):
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    prepared = ["bl-c{0:04d}".format(i) for i in range(8)]
    for bid in prepared:
        folder = root / "baseline" / bid
        folder.mkdir(parents=True)
        (folder / "keep.txt").write_text(bid, encoding="utf-8")
    seq = {"i": 0}

    def next_id(prefix, iso):
        bid = prepared[seq["i"]]
        seq["i"] += 1
        return bid

    def collect(*_args, **_kwargs):
        raise AssertionError("衝突中に収集してはいけない")

    runtime._next_id = next_id
    runtime.collect_arrays_fn = collect
    with pytest.raises(rt.RuntimeControlError):
        runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    assert seq["i"] == 8
    for bid in prepared:
        folder = root / "baseline" / bid
        assert (folder / "keep.txt").is_file()
        assert not (folder / "mesh.npz").exists()
    assert not (root / "baseline" / "active.json").exists()


# ---------------------------------------------------------------------------
# 5. 失敗時の後始末
# ---------------------------------------------------------------------------
def test_candidate_collect_failure_removes_only_the_new_folder(tmp_path):
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    original = runtime.record_baseline("proj", ["Body"], ["mesh"])
    (root / "baseline" / original / "keep.txt").write_text("stay", encoding="utf-8")
    active = (root / "baseline" / "active.json").read_bytes()
    seen = {}
    real = runtime._next_id

    def next_id(prefix, iso):
        bid = real(prefix, iso)
        seen["id"] = bid
        return bid

    def collect(_shape_paths, _ts_names, uv_set=None):
        dest = root / "baseline" / seen["id"]
        dest.mkdir(parents=True)
        (dest / "partial.txt").write_text("x", encoding="utf-8")
        raise RuntimeError("collect failed")

    runtime._next_id = next_id
    runtime.collect_arrays_fn = collect
    with pytest.raises(RuntimeError, match="collect failed"):
        runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    assert not (root / "baseline" / seen["id"]).exists()
    assert (root / "baseline" / original / "keep.txt").is_file()
    assert (root / "baseline" / original / "mesh.npz").is_file()
    assert (root / "baseline" / "active.json").read_bytes() == active


def test_candidate_save_failure_removes_only_the_new_folder(tmp_path, monkeypatch):
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    original = runtime.record_baseline("proj", ["Body"], ["mesh"])
    (root / "baseline" / original / "keep.txt").write_text("stay", encoding="utf-8")
    active = (root / "baseline" / "active.json").read_bytes()
    real = runtime._next_id
    calls = {"n": 0}

    def next_id(prefix, iso):
        calls["n"] += 1
        if calls["n"] == 1:
            return original
        return real(prefix, iso)

    def boom(snapshot, uvremap_root):
        dest = Path(uvremap_root) / "baseline" / str(snapshot.meta["baseline_id"])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "mesh.npz").write_bytes(b"partial")
        raise RuntimeError("save failed")

    runtime._next_id = next_id
    monkeypatch.setattr(br, "save_baseline", boom)
    with pytest.raises(RuntimeError, match="save failed"):
        runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    created = "bl-20260304-050607-{0:04x}".format(runtime._seq)
    assert not (root / "baseline" / created).exists()
    assert (root / "baseline" / original / "keep.txt").is_file()
    assert (root / "baseline" / original / "mesh.npz").is_file()
    assert (root / "baseline" / original / "mesh.npz").read_bytes() != b"partial"
    assert (root / "baseline" / "active.json").read_bytes() == active


# ---------------------------------------------------------------------------
# 6. 破棄
# ---------------------------------------------------------------------------
def test_discard_removes_candidate_and_keeps_active(tmp_path):
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    original = runtime.record_baseline("proj", ["Body"], ["mesh"])
    candidate = runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    other = runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    active = root / "baseline" / "active.json"
    blob = active.read_bytes()
    mtime = active.stat().st_mtime_ns
    assert runtime.discard_baseline_candidate("proj", original) is False
    assert (root / "baseline" / original / "mesh.npz").is_file()
    assert runtime.discard_baseline_candidate("proj", candidate) is True
    assert not (root / "baseline" / candidate).exists()
    assert (root / "baseline" / other / "mesh.npz").is_file()
    assert (root / "baseline" / original / "mesh.npz").is_file()
    assert active.read_bytes() == blob
    assert active.stat().st_mtime_ns == mtime


def test_discard_keeps_raw_active_id_when_pointer_is_broken(tmp_path):
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    raw_id = "bl-rawpointer"
    folder = root / "baseline" / raw_id
    folder.mkdir(parents=True)
    (folder / "note.txt").write_text("keep", encoding="utf-8")
    active = root / "baseline" / "active.json"
    active.write_text(
        json.dumps({"schema": 1, "baseline_id": raw_id}),
        encoding="utf-8",
    )
    blob = active.read_bytes()
    assert rt._read_active_baseline(root) is None
    assert runtime.discard_baseline_candidate("proj", raw_id) is False
    assert (folder / "note.txt").is_file()
    assert active.read_bytes() == blob


def test_discard_keeps_list_head_when_active_is_undecided(tmp_path):
    """active.json が無いとき、_list_baseline_ids の先頭は消さない。"""
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    first = runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    second = runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    assert rt._list_baseline_ids(root)[0] == first
    assert not (root / "baseline" / "active.json").exists()
    assert runtime.discard_baseline_candidate("proj", first) is False
    assert (root / "baseline" / first / "mesh.npz").is_file()
    assert runtime.discard_baseline_candidate("proj", second) is True
    assert not (root / "baseline" / second).exists()
    assert (root / "baseline" / first / "mesh.npz").is_file()
    assert not (root / "baseline" / "active.json").exists()


def test_discard_rejects_unsafe_ids_without_leaving_baseline(tmp_path):
    runtime = _make_runtime(tmp_path)
    root = runtime._root_path
    kept = runtime.record_baseline("proj", ["Body"], ["mesh"])
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    nested = root / "baseline" / "a"
    nested.mkdir()
    (nested / "keep.txt").write_text("keep", encoding="utf-8")
    link_target = tmp_path / "linked"
    link_target.mkdir()
    (link_target / "secret.txt").write_text("keep", encoding="utf-8")
    link = root / "baseline" / "bl-link"
    link.symlink_to(link_target, target_is_directory=True)
    before = sorted(p.name for p in (root / "baseline").iterdir())
    unsafe = ["", None, "../x", "a/b", "a\\b", "..", "missing-id", 12, " bl-1", "-bl"]
    for bad in unsafe:
        assert runtime.discard_baseline_candidate("proj", bad) is False
    long_id = "b" + ("a" * 80)
    assert len(long_id) == 81
    assert runtime.discard_baseline_candidate("proj", long_id) is False
    assert outside.read_text(encoding="utf-8") == "keep"
    assert (root / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (nested / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (link_target / "secret.txt").read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()
    assert (root / "baseline" / kept / "mesh.npz").is_file()
    assert sorted(p.name for p in (root / "baseline").iterdir()) == before
    ok_id = "c" + ("d" * 79)
    assert len(ok_id) == 80
    ok = root / "baseline" / ok_id
    ok.mkdir()
    (ok / "note.txt").write_text("x", encoding="utf-8")
    assert runtime.discard_baseline_candidate("proj", ok_id) is True
    assert not ok.exists()


# ---------------------------------------------------------------------------
# 7. ローカルの記録
# ---------------------------------------------------------------------------
def test_register_rebase_roundtrip_and_no_overwrite(tmp_path):
    runtime = _make_runtime(tmp_path, scene_id_fn=lambda: "sceneA.ma")
    root = runtime._root_path
    runtime.register_rebase("proj", REQ, "bl-1", ["Body"])
    rec = runtime.rebase_record("proj", REQ)
    assert rec["baseline_id"] == "bl-1"
    assert rec["ts_names"] == ["Body"]
    assert rec["scene_id"] == "sceneA.ma"
    assert rec["completed"] is False
    assert rec["completed_at"] is None
    assert rec["steps"] == {name: False for name in _STEP_NAMES}
    assert rec["registered_at"] == FIXED_NOW
    raw = json.loads((root / rt.REBASE_LOCAL_NAME).read_text(encoding="utf-8"))
    assert raw["schema"] == 1
    assert set(raw["requests"]) == {REQ}
    rec["ts_names"].append("Head")
    rec["steps"]["active"] = True
    again = runtime.rebase_record("proj", REQ)
    assert again["ts_names"] == ["Body"]
    assert again["steps"]["active"] is False

    blob = (root / rt.REBASE_LOCAL_NAME).read_bytes()
    runtime.register_rebase("proj", REQ, "bl-other", ["Head"], scene_id="nope.ma")
    assert (root / rt.REBASE_LOCAL_NAME).read_bytes() == blob
    assert runtime.rebase_record("proj", REQ)["baseline_id"] == "bl-1"

    doc = json.loads(blob.decode("utf-8"))
    doc["requests"][REQ]["completed"] = True
    doc["requests"][REQ]["completed_at"] = "2026-03-04T08:00:00"
    (root / rt.REBASE_LOCAL_NAME).write_text(
        json.dumps(doc), encoding="utf-8",
    )
    runtime.register_rebase("proj", REQ, "bl-other", ["Head"])
    kept = runtime.rebase_record("proj", REQ)
    assert kept["completed"] is True
    assert kept["baseline_id"] == "bl-1"
    assert kept["completed_at"] == "2026-03-04T08:00:00"

    other = _make_runtime(tmp_path, scene_id_fn=lambda: "sceneA.ma")
    assert other.rebase_record("proj", REQ)["baseline_id"] == "bl-1"
    assert other.rebase_record("proj", REQ)["completed"] is True


def test_register_rebase_rejects_empty_args(tmp_path):
    runtime = _make_runtime(tmp_path, scene_id_fn=lambda: "sceneA.ma")
    with pytest.raises(rt.RuntimeControlError):
        runtime.register_rebase("proj", "", "bl-1", ["Body"])
    with pytest.raises(rt.RuntimeControlError):
        runtime.register_rebase("proj", None, "bl-1", ["Body"])
    with pytest.raises(rt.RuntimeControlError):
        runtime.register_rebase("proj", REQ, "", ["Body"])
    with pytest.raises(rt.RuntimeControlError):
        runtime.register_rebase("proj", REQ, None, ["Body"])
    with pytest.raises(rt.RuntimeControlError):
        runtime.register_rebase("proj", REQ, "bl-1", [])
    with pytest.raises(rt.RuntimeControlError):
        runtime.register_rebase("proj", REQ, "bl-1", None)
    assert not (runtime._root_path / rt.REBASE_LOCAL_NAME).exists()


def test_corrupt_rebase_local_is_no_record(tmp_path):
    runtime = _make_runtime(tmp_path, scene_id_fn=lambda: "sceneA.ma")
    path = runtime._root_path / rt.REBASE_LOCAL_NAME
    path.write_text("{", encoding="utf-8")
    assert runtime.rebase_record("proj", REQ) is None
    path.write_text(json.dumps({
        "schema": 1,
        "requests": {"rb-bad": ["nope"], "rb-num": {"baseline_id": 3, "ts_names": ["Body"]}},
    }), encoding="utf-8")
    assert runtime.rebase_record("proj", "rb-bad") is None
    assert runtime.rebase_record("proj", "rb-num") is None
    other = _make_runtime(tmp_path, scene_id_fn=lambda: "sceneA.ma")
    assert other.rebase_record("proj", "rb-bad") is None


# ---------------------------------------------------------------------------
# 8-10. 完了処理の順序・結果・冪等性
# ---------------------------------------------------------------------------
def test_complete_rebase_order_result_and_idempotent(tmp_path, monkeypatch):
    env = _prepare_rebase(tmp_path)
    runtime = env["runtime"]
    root = env["root"]
    cmds = env["cmds"]
    ledger = root / "ledger.json"
    ledger.write_text('{"schema": 1, "ts": {"Body": {"uv_hash": "old"}}}', encoding="utf-8")
    final = env["out"] / "Body_BaseColor.png"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"old-uv")
    ledger_blob = ledger.read_bytes()
    final_blob = final.read_bytes()
    order, _write, _revert, _save = _install_order_wrappers(monkeypatch)
    hist = []

    def history(source, text):
        order.append("history")
        hist.append((source, text))

    runtime._now_box["value"] = "2026-03-04T06:00:00"
    result = runtime.complete_rebase("proj", REQ, history_fn=history)
    _assert_result_keys(result)
    assert order == [
        "active", "reverted", "reverted", "reverted", "follow_reset", "history",
    ]
    assert result["status"] == "completed"
    assert result["reason"] == ""
    assert result["request_id"] == REQ
    assert result["baseline_id"] == env["candidate"]
    assert result["ts_names"] == ["Body", "Head", "Cape"]
    assert result["steps"] == {name: True for name in _STEP_NAMES}
    assert result["reverted"] == ["file_body", "file_head"]
    assert result["failed_step"] is None
    assert result["error"] is None
    assert hist == [("uvremap", rt.MSG_REBASE_HISTORY_DONE)]
    assert rt._read_active_baseline(root) == env["candidate"]
    assert (root / "baseline" / env["original"] / "mesh.npz").is_file()
    assert cmds.nodes["file_body"]["fileTextureName"] == "/src/Final/Body_BaseColor.png"
    assert cmds.nodes["file_body"]["uvTilingMode"] == rt.UV_TILING_UDIM
    assert rt.ATTR_ORIGINAL_PATH not in cmds.nodes["file_body"]["extra"]
    assert cmds.nodes["file_head"]["fileTextureName"] == "/src/Final/Head_BaseColor.png"
    assert rt.ATTR_ORIGINAL_PATH not in cmds.nodes["file_head"]["extra"]
    assert cmds.nodes["file_trim"]["fileTextureName"] == "/src/Final/Trim_BaseColor.png"
    assert rt.ATTR_ORIGINAL_PATH not in cmds.nodes["file_trim"]["extra"]
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    for name in ("Body", "Head", "Cape"):
        assert status["ts"][name]["state"] == rt.STATE_ORIGINAL
        assert status["ts"][name]["generation"] == 0
    data = json.loads((root / "follow.json").read_text(encoding="utf-8"))
    assert "Body" not in data["ts"]
    assert "Head" not in data["ts"]
    assert data["ts"]["Trim"]["last_job_id"] == "j-trim"
    assert data["ts"]["Trim"]["source_sig"] == {"Trim_BaseColor.png": [3, 4]}
    assert data["flag_mtime_ns"] == 424242
    saved = runtime.rebase_record("proj", REQ)
    assert saved["completed"] is True
    assert saved["completed_at"] == "2026-03-04T06:00:00"
    assert ledger.read_bytes() == ledger_blob
    assert final.read_bytes() == final_blob

    _switch(cmds, "Body", env["out"])
    switched = cmds.nodes["file_body"]["fileTextureName"]
    assert switched != "/src/Final/Body_BaseColor.png"
    mark = len(order)
    env["enabled"]["on"] = False
    env["scene"]["id"] = "other.ma"
    again = runtime.complete_rebase("proj", REQ, history_fn=history)
    assert again["status"] == "already_completed"
    assert again["reason"] == ""
    assert len(order) == mark
    assert cmds.nodes["file_body"]["fileTextureName"] == switched

    runtime2 = _make_runtime(
        tmp_path, cmds=cmds, enabled=lambda: False, scene_id_fn=lambda: "elsewhere.ma",
    )
    third = runtime2.complete_rebase("proj", REQ, history_fn=history)
    assert third["status"] == "already_completed"
    assert len(order) == mark
    assert cmds.nodes["file_body"]["fileTextureName"] == switched


def test_complete_rebase_without_history_fn(tmp_path):
    env = _prepare_rebase(tmp_path)
    result = env["runtime"].complete_rebase("proj", REQ)
    _assert_result_keys(result)
    assert result["status"] == "completed"
    assert result["steps"]["history"] is True
    assert result["error"] is None
    data = json.loads((env["root"] / "follow.json").read_text(encoding="utf-8"))
    assert "Body" not in data["ts"]
    assert data["flag_mtime_ns"] == 424242


def test_null_scene_id_matches_empty_and_does_not_create_follow(tmp_path):
    runtime = _make_runtime(tmp_path, scene_id_fn=lambda: None, cmds=FakeCmds())
    root = runtime._root_path
    candidate = runtime.record_baseline_candidate("proj", ["Body"], ["mesh"])
    runtime.register_rebase("proj", REQ, candidate, ["Body"], scene_id="")
    doc = json.loads((root / rt.REBASE_LOCAL_NAME).read_text(encoding="utf-8"))
    doc["requests"][REQ]["scene_id"] = None
    (root / rt.REBASE_LOCAL_NAME).write_text(json.dumps(doc), encoding="utf-8")
    result = runtime.complete_rebase("proj", REQ)
    assert result["status"] == "completed"
    assert not (root / "follow.json").exists()
    assert rt._read_active_baseline(root) == candidate


# ---------------------------------------------------------------------------
# 11. 途中失敗と再開
# ---------------------------------------------------------------------------
def test_complete_rebase_resumes_after_revert_failure(tmp_path, monkeypatch):
    env = _prepare_rebase(tmp_path)
    runtime = env["runtime"]
    root = env["root"]
    follow_blob = (root / "follow.json").read_bytes()
    counts = {"active": 0}
    orig_write = rt._write_active_baseline
    boom = {"on": True}
    orig_revert = rt.revert_ts_file_nodes
    hist = []

    def wrapped_write(root_path, baseline_id):
        counts["active"] += 1
        return orig_write(root_path, baseline_id)

    def wrapped_revert(cmds, ts_name=None):
        if boom["on"]:
            raise RuntimeError("revert boom")
        return orig_revert(cmds, ts_name=ts_name)

    def history(source, text):
        hist.append((source, text))

    monkeypatch.setattr(rt, "_write_active_baseline", wrapped_write)
    monkeypatch.setattr(rt, "revert_ts_file_nodes", wrapped_revert)
    result = runtime.complete_rebase("proj", REQ, history_fn=history)
    _assert_result_keys(result)
    assert result["status"] == "partial"
    assert result["failed_step"] == "reverted"
    assert result["error"].startswith("RuntimeError:")
    assert "revert boom" in result["error"]
    assert str(tmp_path) not in (result["error"] or "")
    assert counts["active"] == 1
    assert rt._read_active_baseline(root) == env["candidate"]
    assert (root / "follow.json").read_bytes() == follow_blob
    assert hist == [("uvremap", rt.MSG_REBASE_HISTORY_PARTIAL_FMT.format(step="reverted"))]
    assert env["cmds"].nodes["file_body"]["fileTextureName"] != "/src/Final/Body_BaseColor.png"
    saved = runtime.rebase_record("proj", REQ)
    assert saved["completed"] is False
    assert saved["steps"]["active"] is True
    assert saved["steps"]["reverted"] is False

    boom["on"] = False
    again = runtime.complete_rebase("proj", REQ, history_fn=history)
    assert counts["active"] == 1
    assert again["status"] == "completed"
    assert again["failed_step"] is None
    data = json.loads((root / "follow.json").read_text(encoding="utf-8"))
    assert "Body" not in data["ts"]
    assert data["ts"]["Trim"]["last_job_id"] == "j-trim"
    assert env["cmds"].nodes["file_body"]["fileTextureName"] == "/src/Final/Body_BaseColor.png"
    assert hist[-1] == ("uvremap", rt.MSG_REBASE_HISTORY_DONE)
    assert runtime.rebase_record("proj", REQ)["completed"] is True


def test_complete_rebase_resumes_after_follow_failure(tmp_path, monkeypatch):
    env = _prepare_rebase(tmp_path)
    runtime = env["runtime"]
    root = env["root"]
    follow_blob = (root / "follow.json").read_bytes()
    counts = {"active": 0, "reverted": 0}
    orig_write = rt._write_active_baseline
    orig_revert = rt.revert_ts_file_nodes
    real_save = follow.save_follow
    boom = {"on": True}
    hist = []

    def wrapped_write(root_path, baseline_id):
        counts["active"] += 1
        return orig_write(root_path, baseline_id)

    def wrapped_revert(cmds, ts_name=None):
        counts["reverted"] += 1
        return orig_revert(cmds, ts_name=ts_name)

    def wrapped_save(root_path, data):
        if boom["on"]:
            raise OSError("cannot write {0}".format(root_path / "follow.json"))
        return real_save(root_path, data)

    def history(source, text):
        hist.append(text)

    monkeypatch.setattr(rt, "_write_active_baseline", wrapped_write)
    monkeypatch.setattr(rt, "revert_ts_file_nodes", wrapped_revert)
    monkeypatch.setattr(follow, "save_follow", wrapped_save)
    result = runtime.complete_rebase("proj", REQ, history_fn=history)
    assert result["status"] == "partial"
    assert result["failed_step"] == "follow_reset"
    assert result["error"].startswith("OSError:")
    assert str(tmp_path) not in result["error"]
    assert "/" not in result["error"]
    assert (root / "follow.json").read_bytes() == follow_blob
    assert env["cmds"].nodes["file_body"]["fileTextureName"] == "/src/Final/Body_BaseColor.png"
    assert hist == [rt.MSG_REBASE_HISTORY_PARTIAL_FMT.format(step="follow_reset")]
    assert counts["active"] == 1
    reverted_calls = counts["reverted"]
    assert reverted_calls == 3

    boom["on"] = False
    again = runtime.complete_rebase("proj", REQ, history_fn=history)
    assert again["status"] == "completed"
    assert counts["active"] == 1
    assert counts["reverted"] == reverted_calls
    data = json.loads((root / "follow.json").read_text(encoding="utf-8"))
    assert "Body" not in data["ts"] and "Head" not in data["ts"]
    assert data["flag_mtime_ns"] == 424242
    assert hist[-1] == rt.MSG_REBASE_HISTORY_DONE


def test_complete_rebase_history_exception_is_partial_and_resumes(tmp_path, monkeypatch):
    env = _prepare_rebase(tmp_path)
    runtime = env["runtime"]
    counts = {"active": 0, "reverted": 0, "follow": 0}
    orig_write = rt._write_active_baseline
    orig_revert = rt.revert_ts_file_nodes
    real_save = follow.save_follow
    fail = {"on": True}
    hist = []

    def wrapped_write(root_path, baseline_id):
        counts["active"] += 1
        return orig_write(root_path, baseline_id)

    def wrapped_revert(cmds, ts_name=None):
        counts["reverted"] += 1
        return orig_revert(cmds, ts_name=ts_name)

    def wrapped_save(root_path, data):
        counts["follow"] += 1
        return real_save(root_path, data)

    def history(source, text):
        hist.append(text)
        if fail["on"] and text == rt.MSG_REBASE_HISTORY_DONE:
            raise RuntimeError("hist boom")

    monkeypatch.setattr(rt, "_write_active_baseline", wrapped_write)
    monkeypatch.setattr(rt, "revert_ts_file_nodes", wrapped_revert)
    monkeypatch.setattr(follow, "save_follow", wrapped_save)
    result = runtime.complete_rebase("proj", REQ, history_fn=history)
    assert result["status"] == "partial"
    assert result["failed_step"] == "history"
    assert result["error"].startswith("RuntimeError:")
    assert "hist boom" in result["error"]
    assert runtime.rebase_record("proj", REQ)["completed"] is False
    assert runtime.rebase_record("proj", REQ)["steps"]["history"] is False
    assert rt.MSG_REBASE_HISTORY_DONE in hist
    assert rt.MSG_REBASE_HISTORY_PARTIAL_FMT.format(step="history") in hist
    active_n = counts["active"]
    reverted_n = counts["reverted"]
    follow_n = counts["follow"]

    fail["on"] = False
    again = runtime.complete_rebase("proj", REQ, history_fn=history)
    assert again["status"] == "completed"
    assert counts["active"] == active_n
    assert counts["reverted"] == reverted_n
    assert counts["follow"] == follow_n
    assert hist[-1] == rt.MSG_REBASE_HISTORY_DONE


# ---------------------------------------------------------------------------
# 12. 事前確認
# ---------------------------------------------------------------------------
def test_complete_rebase_skips_without_record(tmp_path):
    env = _prepare_rebase(tmp_path)
    runtime = env["runtime"]
    root = env["root"]
    doc = json.loads((root / rt.REBASE_LOCAL_NAME).read_text(encoding="utf-8"))
    blob = (root / rt.REBASE_LOCAL_NAME).read_bytes()
    result = runtime.complete_rebase("proj", "rb-missing", history_fn=lambda *_a: None)
    _assert_result_keys(result)
    assert result["status"] == "skipped"
    assert result["reason"] == "no_local_record"
    assert result["baseline_id"] == ""
    assert result["ts_names"] == []
    assert result["steps"] == {name: False for name in _STEP_NAMES}
    assert result["failed_step"] is None
    assert (root / rt.REBASE_LOCAL_NAME).read_bytes() == blob
    assert set(json.loads((root / rt.REBASE_LOCAL_NAME).read_text(encoding="utf-8"))["requests"]) == set(doc["requests"])


def test_complete_rebase_killswitch_before_scene_changes_nothing(tmp_path):
    env = _prepare_rebase(tmp_path)
    runtime = env["runtime"]
    root = env["root"]
    blobs = {
        "active": (root / "baseline" / "active.json").read_bytes(),
        "follow": (root / "follow.json").read_bytes(),
        "status": (root / "status.json").read_bytes(),
        "local": (root / rt.REBASE_LOCAL_NAME).read_bytes(),
    }
    nodes = env["cmds"].copy_state()
    calls = []
    hist = []
    runtime.get_enabled = lambda: calls.append("enabled") or False
    runtime.scene_id_fn = lambda: calls.append("scene") or "other.ma"
    result = runtime.complete_rebase(
        "proj", REQ, history_fn=lambda source, text: hist.append((source, text)),
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "killswitch_off"
    assert result["baseline_id"] == env["candidate"]
    assert calls == ["enabled"]
    assert hist == []
    assert (root / "baseline" / "active.json").read_bytes() == blobs["active"]
    assert (root / "follow.json").read_bytes() == blobs["follow"]
    assert (root / "status.json").read_bytes() == blobs["status"]
    assert (root / rt.REBASE_LOCAL_NAME).read_bytes() == blobs["local"]
    assert env["cmds"].copy_state() == nodes


def test_complete_rebase_scene_changed_changes_nothing(tmp_path):
    env = _prepare_rebase(tmp_path)
    runtime = env["runtime"]
    root = env["root"]
    blobs = {
        "active": (root / "baseline" / "active.json").read_bytes(),
        "follow": (root / "follow.json").read_bytes(),
        "status": (root / "status.json").read_bytes(),
        "local": (root / rt.REBASE_LOCAL_NAME).read_bytes(),
    }
    nodes = env["cmds"].copy_state()
    calls = []
    hist = []
    runtime.get_enabled = lambda: calls.append("enabled") or True
    runtime.scene_id_fn = lambda: calls.append("scene") or "other.ma"
    result = runtime.complete_rebase(
        "proj", REQ, history_fn=lambda source, text: hist.append(text),
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "scene_changed"
    assert calls[:2] == ["enabled", "scene"]
    assert hist == []
    assert (root / "baseline" / "active.json").read_bytes() == blobs["active"]
    assert (root / "follow.json").read_bytes() == blobs["follow"]
    assert (root / "status.json").read_bytes() == blobs["status"]
    assert (root / rt.REBASE_LOCAL_NAME).read_bytes() == blobs["local"]
    assert env["cmds"].copy_state() == nodes
    assert rt._read_active_baseline(root) == env["original"]


def test_complete_rebase_missing_candidate_stops_at_active(tmp_path, monkeypatch):
    env = _prepare_rebase(tmp_path)
    runtime = env["runtime"]
    root = env["root"]
    blobs = {
        "active": (root / "baseline" / "active.json").read_bytes(),
        "follow": (root / "follow.json").read_bytes(),
        "status": (root / "status.json").read_bytes(),
    }
    nodes = env["cmds"].copy_state()
    order, _w, _r, _s = _install_order_wrappers(monkeypatch)
    hist = []
    import shutil
    shutil.rmtree(root / "baseline" / env["candidate"])
    result = runtime.complete_rebase(
        "proj", REQ, history_fn=lambda source, text: hist.append((source, text)) or order.append("history"),
    )
    assert result["status"] == "partial"
    assert result["failed_step"] == "active"
    assert result["error"] == "RuntimeError_: 基準候補が見つかりません"
    assert str(tmp_path) not in result["error"]
    assert order == ["history"]
    assert hist == [("uvremap", rt.MSG_REBASE_HISTORY_PARTIAL_FMT.format(step="active"))]
    assert (root / "baseline" / "active.json").read_bytes() == blobs["active"]
    assert (root / "follow.json").read_bytes() == blobs["follow"]
    assert (root / "status.json").read_bytes() == blobs["status"]
    assert env["cmds"].copy_state() == nodes
    saved = runtime.rebase_record("proj", REQ)
    assert saved["completed"] is False
    assert saved["steps"] == {name: False for name in _STEP_NAMES}


# ---------------------------------------------------------------------------
# 13. 既存の保証(maya を読まない、datetime.now は 1 回)
# ---------------------------------------------------------------------------
def test_rebase_additions_do_not_import_maya_or_add_datetime_now():
    source = Path(rt.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("maya")
                assert alias.name != "uvremap_maya"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("maya")
            assert mod != "uvremap_maya"
    assert source.count("datetime.now") == 1
    assert "maya" not in sys.modules or "maya.cmds" not in sys.modules
