# -*- coding: utf-8 -*-
"""リベースの送信・poll・完了・取り消し・自動追従の停止。Maya は使わない。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fresh_import_probe import new_forbidden_modules  # noqa: E402
import uvremap_integration as integ  # noqa: E402
import uvremap_rebase as rb  # noqa: E402


SHA = "ab" * 20
UTC = datetime(2026, 10, 19, 1, 2, 3, tzinfo=timezone.utc)
PROJECT = "proj"


def test_import_does_not_load_maya_qt_or_pillow():
    # 先に走った別のテストが本物の PySide6 / PIL を読み込んでいても影響を受けないよう、
    # 新しいインタープリタで import して、禁止モジュールが増えないことを確かめる。
    assert new_forbidden_modules(_PKG, ["uvremap_integration", "uvremap_rebase"]) == []


class FakeRuntime:
    def __init__(self, order):
        self.order = order
        self.scene_id = "scene-1"
        self.records = {}
        self.fail = set()
        self.complete_results = []
        self.candidate_n = 0

    def _scene_id(self):
        return self.scene_id

    def record_baseline_candidate(self, project_key, ts_names, shape_paths, uv_set=None):
        self.order.append(("candidate", project_key, list(ts_names), list(shape_paths)))
        if "candidate" in self.fail:
            raise RuntimeError("candidate")
        self.candidate_n += 1
        return "bl-cand" if self.candidate_n == 1 else "bl-cand-{0}".format(self.candidate_n)

    def discard_baseline_candidate(self, project_key, baseline_id):
        self.order.append(("discard", project_key, baseline_id))
        if "discard" in self.fail:
            raise RuntimeError("discard")
        return True

    def register_rebase(self, project_key, request_id, baseline_id, ts_names, *, scene_id=None):
        self.order.append((
            "register", project_key, request_id, baseline_id, list(ts_names), scene_id,
        ))
        if "register" in self.fail:
            raise RuntimeError("register")
        self.records[request_id] = {
            "completed": False,
            "scene_id": scene_id,
            "baseline_id": baseline_id,
            "ts_names": list(ts_names),
        }

    def rebase_record(self, project_key, request_id):
        if "record_raise" in self.fail:
            raise RuntimeError("record")
        row = self.records.get(request_id)
        if row is None:
            return None
        return dict(row)

    def complete_rebase(self, project_key, request_id, history_fn=None):
        self.order.append(("complete", project_key, request_id))
        if self.complete_results:
            result = dict(self.complete_results.pop(0))
        else:
            result = {"status": "completed", "reason": "", "failed_step": None}
        if result.get("status") == "completed" and request_id in self.records:
            self.records[request_id]["completed"] = True
        return result

    def revert_to_original(self, *args, **kwargs):
        self.order.append(("revert", args))
        return []

    def convert_now(self, *args, **kwargs):
        self.order.append(("convert_now",))
        return "job-1"

    def poll(self):
        self.order.append(("poll",))
        return []


def _sync_body(version="1.4.0", **over):
    body = {
        "schema": 1,
        "plugin_version": version,
        "export_id": "ex-1",
        "status": "exported",
        "ok": True,
        "force_full": True,
        "request_id": None,
    }
    body.update(over)
    return body


def _make(tmp_path, *, enabled=True, scene_id="scene-1", known=None,
          texture_sets=None, project=PROJECT, sync=True, sync_body=None,
          active=True, candidate_only=False, cfg=None, export=None,
          time_value=1_700_000_000.0):
    slot = getattr(_make, "n", 0) + 1
    _make.n = slot
    sub = "sub{0}".format(slot)
    root_dir = tmp_path / "uvremap"
    work = root_dir / sub
    work.mkdir(parents=True)
    final = tmp_path / "final" / sub
    final.mkdir(parents=True)
    order = []
    runtime = FakeRuntime(order)
    runtime.scene_id = scene_id
    clock = {"t": time_value}
    history = []
    if active:
        base = work / "baseline" / "bl-active"
        base.mkdir(parents=True)
        (base / "mesh.npz").write_bytes(b"x")
        (work / "baseline" / "active.json").write_text(
            json.dumps({"schema": 1, "baseline_id": "bl-active"}),
            encoding="utf-8",
        )
    elif candidate_only:
        only = work / "baseline" / "bl-only"
        only.mkdir(parents=True)
        (only / "mesh.npz").write_bytes(b"x")
        (only / "meta.json").write_text("{}", encoding="utf-8")
    if sync:
        body = sync_body if sync_body is not None else _sync_body()
        (final / "_sync_result.json").write_text(
            json.dumps(body), encoding="utf-8",
        )
    if known is None:
        known = ["Body"]
    if texture_sets is None:
        texture_sets = {"Body": [{"shape": "|mesh|shapeA"}]}
    config = {
        "uvremap_enabled": enabled,
        "uvremap_root_dir": str(root_dir),
        "watch_dir": str(tmp_path / "live"),
        "final_export_dir": str(tmp_path / "final"),
    }
    if cfg:
        config.update(cfg)
    exports = []

    def export_fn(shape_paths, meshes_dir, baseline_id, triangulate=False, **kwargs):
        path = Path(meshes_dir) / "{0}.fbx".format(baseline_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FBX!")
        info = {"applied": True, "name": "uvOld", "warnings": [], "shapes": []}
        exports.append({
            "shape_paths": list(shape_paths),
            "meshes_dir": Path(meshes_dir),
            "baseline_id": baseline_id,
            "triangulate": triangulate,
            "baseline_snapshot": kwargs.get("baseline_snapshot"),
            "candidate_snapshot": kwargs.get("candidate_snapshot"),
            "info": info,
            "path": path,
        })
        order.append(("export", str(meshes_dir), baseline_id))
        return {"path": str(path), "size": 4, "sha1": SHA}, info

    if export is not None:
        def wrapped(shape_paths, meshes_dir, baseline_id, triangulate=False, **kwargs):
            order.append(("export", str(meshes_dir), baseline_id))
            return export(shape_paths, meshes_dir, baseline_id, triangulate=triangulate, **kwargs)
        export_fn = wrapped

    utc_calls = {"n": 0}

    def utcnow():
        utc_calls["n"] += 1
        return UTC

    ctrl = integ.UVRemapController(
        get_config=lambda: config,
        save_config_fn=lambda partial: config.update(partial),
        project_key_fn=lambda: project,
        known_ts_fn=lambda _key: list(known),
        final_dir_fn=lambda _key: str(final),
        final_subfolder_fn=lambda _key: sub,
        list_texture_sets_fn=lambda: texture_sets,
        runtime=runtime,
        history_fn=lambda source, text: history.append((source, text)),
        time_fn=lambda: clock["t"],
        utcnow_fn=utcnow,
        rebase_export_fn=export_fn,
        maya_version_fn=lambda: None,
    )
    ctrl._work = work
    ctrl._final = final
    ctrl._order = order
    ctrl._history = history
    ctrl._clock = clock
    ctrl._config = config
    ctrl._exports = exports
    ctrl._utc_calls = utc_calls
    ctrl._runtime = runtime
    return ctrl


def _kinds(ctrl):
    return [item[0] for item in ctrl._order]


def _put_status(ctrl, request_id, state, **extra):
    body = {
        "schema": 1,
        "request_id": request_id,
        "state": state,
        "updated_at": 10,
    }
    body.update(extra)
    path = rb.status_path(ctrl._work, request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _put_result(ctrl, request_id, **over):
    body = _sync_body()
    body["request_id"] = request_id
    body.update(over)
    (ctrl._final / "_sync_result.json").write_text(json.dumps(body), encoding="utf-8")


def _put_rebase_result(ctrl, request_id, **over):
    body = _sync_body()
    body["request_id"] = request_id
    body.update(over)
    path = rb.result_path(ctrl._work, request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def test_send_order_uses_utc_and_candidate_id(tmp_path, monkeypatch):
    ctrl = _make(tmp_path)
    real_write = rb.write_request
    real_cancel = rb.write_cancel

    def write_request(root, request):
        ctrl._order.append(("write_request", request["request_id"]))
        return real_write(root, request)

    def write_cancel(root, request_id):
        ctrl._order.append(("write_cancel", request_id))
        return real_cancel(root, request_id)

    monkeypatch.setattr(rb, "write_request", write_request)
    monkeypatch.setattr(rb, "write_cancel", write_cancel)
    result = ctrl.rebase_send()
    assert result["ok"] is True
    assert result["request_id"].startswith("rb-20261019-010203-")
    assert ctrl._utc_calls["n"] >= 1
    assert _kinds(ctrl) == ["candidate", "export", "register", "write_request"]
    saved = rb.read_request(ctrl._work, result["request_id"])
    assert saved["baseline_id"] == result["baseline_id"] == "bl-cand"
    assert saved["mesh"]["sha1"] == SHA
    assert saved["mesh"]["size"] == 4
    assert saved["final_full"] is True
    assert saved["project_key"] == PROJECT
    assert saved["ts_names"] == ["Body"]
    register = next(item for item in ctrl._order if item[0] == "register")
    assert register[5] == "scene-1"
    assert register[2] == result["request_id"]
    export = ctrl._exports[0]
    assert export["meshes_dir"] == ctrl._work / "meshes"
    assert export["shape_paths"] == ["|mesh|shapeA"]
    assert any(MSG in text for _source, text in ctrl._history for MSG in (integ.MSG_REBASE_SENT,))
    assert "uvOld" in ctrl._history[0][1]


@pytest.mark.parametrize("setup,message", [
    ({"enabled": False}, integ.MSG_REBASE_NEED_KILLSWITCH),
    ({"project": None}, integ.MSG_NO_PROJECT),
    ({"scene_id": ""}, integ.MSG_REBASE_NEED_SAVED_SCENE),
    ({"active": False}, integ.MSG_REBASE_NEED_BASELINE),
    ({"active": False, "candidate_only": True}, integ.MSG_REBASE_NEED_BASELINE),
    ({"sync": False}, integ.MSG_REBASE_PLUGIN_UNKNOWN),
    ({"sync_body": _sync_body("1.2.9")}, rb.MSG_PLUGIN_UPDATE_REQUIRED),
    ({"known": ["Gone"], "texture_sets": {"Body": [{"shape": "|m"}]}}, integ.MSG_NO_TARGETS),
])
def test_send_precheck_creates_nothing(tmp_path, setup, message):
    ctrl = _make(tmp_path, **setup)
    result = ctrl.rebase_send(confirm_fn=lambda _text: True)
    assert result["ok"] is False
    assert message in result["message"]
    assert ctrl._order == []
    assert rb.list_requests(ctrl._work) == []


@pytest.mark.parametrize("version", ["1.3.0", "1.3.9"])
def test_rebase_send_refuses_plugin_before_140(tmp_path, version):
    ctrl = _make(tmp_path, sync_body=_sync_body(version))
    result = ctrl.rebase_send()
    assert result["ok"] is False
    assert result["message"] == rb.MSG_PLUGIN_UPDATE_REQUIRED
    assert ctrl._order == []
    assert rb.list_requests(ctrl._work) == []


@pytest.mark.parametrize("version", ["1.4.0", "1.4.3"])
def test_rebase_send_accepts_plugin_140_or_newer(tmp_path, version):
    ctrl = _make(tmp_path, sync_body=_sync_body(version))
    result = ctrl.rebase_send()
    assert result["ok"] is True
    assert rb.list_requests(ctrl._work)


def test_replace_confirms_once_and_locked_does_not(tmp_path, monkeypatch):
    ctrl = _make(tmp_path)
    real_cancel = rb.write_cancel
    monkeypatch.setattr(
        rb, "write_cancel",
        lambda root, request_id: (
            ctrl._order.append(("write_cancel", request_id)) or real_cancel(root, request_id)
        ),
    )
    monkeypatch.setattr(
        rb, "write_request",
        (lambda real: lambda root, request: (
            ctrl._order.append(("write_request", request["request_id"])) or real(root, request)
        ))(rb.write_request),
    )
    first = ctrl.rebase_send()
    assert first["ok"] is True
    ctrl._clock["t"] += 1
    confirms = []

    def confirm(text):
        confirms.append(text)
        return False

    refused = ctrl.rebase_send(confirm_fn=confirm)
    assert refused["ok"] is False
    assert refused["cancelled"] is True
    assert confirms == [integ.MSG_REBASE_CONFIRM_REPLACE]
    assert rb.list_requests(ctrl._work)[0]["request_id"] == first["request_id"]
    assert not rb.cancel_exists(ctrl._work, first["request_id"])

    confirms.clear()

    def accept(text):
        confirms.append(text)
        return True

    before = len(ctrl._order)
    second = ctrl.rebase_send(confirm_fn=accept)
    assert second["ok"] is True
    assert confirms == [integ.MSG_REBASE_CONFIRM_REPLACE]
    tail = [item[0] for item in ctrl._order[before:]]
    assert tail == ["candidate", "export", "register", "write_request", "write_cancel"]
    assert ctrl._order[-1][1] == first["request_id"]
    assert rb.cancel_exists(ctrl._work, first["request_id"])

    _put_status(ctrl, second["request_id"], "backing_up")
    ctrl.rebase_tick(ctrl._clock["t"] + 5)
    locked_view = ctrl.rebase_view()
    assert locked_view["phase"] == "locked"
    assert locked_view["can_send"] is False
    confirms.clear()
    locked = ctrl.rebase_send(confirm_fn=accept)
    assert locked["ok"] is False
    assert locked["message"] == integ.MSG_REBASE_LOCKED_NO_REPLACE
    assert confirms == []


def test_failures_discard_candidate_and_hide_paths(tmp_path, monkeypatch):
    ctrl = _make(tmp_path)

    def boom(*_args, **_kwargs):
        raise integ_export_error()

    def integ_export_error():
        import uvremap_maya as um
        return um.RebaseExportError("メッシュの書き出しに失敗しました")

    ctrl._rebase_export_fn = boom
    failed = ctrl.rebase_send()
    assert failed["ok"] is False
    assert "/" not in failed["message"] and "\\" not in failed["message"]
    assert ("discard", PROJECT, "bl-cand") in [
        item for item in ctrl._order if item[0] == "discard"
    ]
    assert rb.list_requests(ctrl._work) == []

    ctrl = _make(tmp_path)
    ctrl._runtime.fail.add("register")
    failed = ctrl.rebase_send()
    assert failed["ok"] is False
    assert "/" not in failed["message"]
    assert any(item[0] == "discard" for item in ctrl._order)
    assert not (ctrl._work / "meshes" / "bl-cand.fbx").exists()
    assert rb.list_requests(ctrl._work) == []

    ctrl = _make(tmp_path)

    def bad_write(_root, _request):
        raise OSError("disk")

    monkeypatch.setattr(rb, "write_request", bad_write)
    failed = ctrl.rebase_send()
    assert failed["ok"] is False
    assert "/" not in failed["message"] and "\\" not in failed["message"]
    assert any(item[0] == "discard" for item in ctrl._order)
    assert not (ctrl._work / "meshes" / "bl-cand.fbx").exists()
    assert rb.list_requests(ctrl._work) == []


def test_uvold_result_is_history_and_cleanup_is_prominent(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    assert sent["uvold"]["applied"] is True
    assert "uvOld" in ctrl._history[0][1]
    assert integ.MSG_REBASE_SENT in ctrl._history[0][1]

    def skipped(*_a, **_k):
        ctrl._order.append(("export", "", "bl"))
        return (
            {"path": str(ctrl._work / "meshes" / "x.fbx"), "size": 1, "sha1": SHA},
            {
                "applied": False,
                "name": None,
                "warnings": ["uvold_topology_mismatch"],
                "shapes": [],
            },
        )

    ctrl = _make(tmp_path, export=skipped)
    (ctrl._work / "meshes").mkdir(parents=True, exist_ok=True)
    (ctrl._work / "meshes" / "x.fbx").write_bytes(b"x")
    result = ctrl.rebase_send()
    assert result["ok"] is True
    assert "形が基準と違います" in ctrl._history[0][1]

    def dirty(*_a, **_k):
        path = ctrl._work / "meshes" / "bl-cand.fbx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FBX!")
        ctrl._order.append(("export", "", "bl-cand"))
        return (
            {"path": str(path), "size": 4, "sha1": SHA},
            {
                "applied": True,
                "name": "uvOld",
                "warnings": ["uvold_cleanup_failed"],
                "shapes": ["pSphereShape1"],
            },
        )

    ctrl = _make(tmp_path, export=dirty)
    result = ctrl.rebase_send()
    assert result["ok"] is True
    assert "pSphereShape1" in result["message"]
    assert integ.MSG_UVOLD_CLEANUP_FAILED_FMT.split("{")[0] in result["message"]
    assert any("pSphereShape1" in text for _source, text in ctrl._history)
    assert "/" not in result["message"]


def _export_uvold(*, applied, name, warnings=None, shapes=None):
    def export(shape_paths, meshes_dir, baseline_id, triangulate=False, **kwargs):
        path = Path(meshes_dir) / "{0}.fbx".format(baseline_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FBX!")
        return (
            {"path": str(path), "size": 4, "sha1": SHA},
            {
                "applied": applied,
                "name": name,
                "warnings": list(warnings or []),
                "shapes": list(shapes or []),
            },
        )
    return export


def test_rebase_send_writes_uv_old_when_applied(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    assert sent["ok"] is True
    recs = rb.list_requests(ctrl._work)
    assert recs[0]["uv_old"] == {"name": "uvOld", "index": 1}

    ctrl2 = _make(tmp_path, export=_export_uvold(applied=True, name="uvOld_1"))
    sent2 = ctrl2.rebase_send()
    assert sent2["ok"] is True
    recs2 = rb.list_requests(ctrl2._work)
    assert recs2[0]["uv_old"] == {"name": "uvOld_1", "index": 1}


def test_rebase_send_omits_uv_old_when_skipped(tmp_path):
    ctrl = _make(
        tmp_path,
        export=_export_uvold(
            applied=False,
            name=None,
            warnings=["uvold_topology_mismatch"],
        ),
    )
    sent = ctrl.rebase_send()
    assert sent["ok"] is True
    recs = rb.list_requests(ctrl._work)
    assert "uv_old" not in recs[0]


def test_rebase_send_omits_uv_old_when_name_is_invalid(tmp_path):
    ctrl = _make(tmp_path, export=_export_uvold(applied=True, name="bad name"))
    sent = ctrl.rebase_send()
    assert sent["ok"] is True
    recs = rb.list_requests(ctrl._work)
    assert "uv_old" not in recs[0]


def test_rebase_send_omits_uv_old_when_applied_name_is_none(tmp_path):
    ctrl = _make(tmp_path, export=_export_uvold(applied=True, name=None))
    sent = ctrl.rebase_send()
    assert sent["ok"] is True
    recs = rb.list_requests(ctrl._work)
    assert "uv_old" not in recs[0]


def _arm_done(ctrl, request_id):
    _put_status(ctrl, request_id, "done")
    _put_result(ctrl, request_id)


def test_done_completes_once_and_guides_once(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    first = ctrl.rebase_tick(100.0)
    assert first["message"] == rb.MSG_DONE_GUIDANCE
    assert _kinds(ctrl).count("complete") == 1
    assert sum(1 for _s, text in ctrl._history if text == rb.MSG_DONE_GUIDANCE) == 1
    second = ctrl.rebase_tick(102.0)
    assert _kinds(ctrl).count("complete") == 1
    assert sum(1 for _s, text in ctrl._history if text == rb.MSG_DONE_GUIDANCE) == 1
    assert second["message"] == ""
    assert ctrl.rebase_view()["text"] == rb.MSG_DONE_GUIDANCE


def test_done_with_direction_fills_notes_history_and_screen_once(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _put_status(
        ctrl, sent["request_id"], "done",
        uv_fills={"direction": 2, "enabled": True},
    )
    _put_result(ctrl, sent["request_id"])
    note = rb.MSG_DONE_DIRECTION_FMT.format(n=2)
    shown = ctrl.rebase_tick(100.0)
    assert shown["message"] == rb.MSG_DONE_GUIDANCE + "\n" + note
    assert ctrl.rebase_view()["text"] == shown["message"]
    texts = [text for _source, text in ctrl._history]
    assert texts.count(rb.MSG_DONE_GUIDANCE) == 1
    assert texts.count(note) == 1
    assert texts.index(rb.MSG_DONE_GUIDANCE) < texts.index(note)
    again = ctrl.rebase_tick(102.0)
    assert again["message"] == ""
    assert [text for _source, text in ctrl._history].count(note) == 1


def test_done_with_zero_direction_fills_skips_the_note(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _put_status(ctrl, sent["request_id"], "done", uv_fills={"direction": 0})
    _put_result(ctrl, sent["request_id"])
    shown = ctrl.rebase_tick(100.0)
    assert shown["message"] == rb.MSG_DONE_GUIDANCE
    assert ctrl.rebase_view()["text"] == rb.MSG_DONE_GUIDANCE
    assert not any("法線" in text for _source, text in ctrl._history)


def test_done_prefers_rebase_result_when_shared_result_was_overwritten(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    rid = sent["request_id"]
    _put_status(ctrl, rid, "done")
    _put_rebase_result(ctrl, rid)
    _put_result(ctrl, None)
    shown = ctrl.rebase_tick(100.0)
    assert _kinds(ctrl).count("complete") == 1
    assert shown["message"] == rb.MSG_DONE_GUIDANCE


def test_done_without_rebase_result_uses_legacy_shared_result(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    rid = sent["request_id"]
    _arm_done(ctrl, rid)
    assert not rb.result_path(ctrl._work, rid).exists()
    ctrl.rebase_tick(100.0)
    assert _kinds(ctrl).count("complete") == 1


@pytest.mark.parametrize("over,reason_file", [
    ({"missing": True}, "result_missing"),
    ({"ok": False}, "result_not_ok"),
    ({"request_id": "rb-20261019-010203-ffff"}, "result_request_id_mismatch"),
    ({"force_full": False}, "not_full_final"),
])
def test_unconfirmed_done_does_not_complete_or_discard(tmp_path, over, reason_file):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _put_status(ctrl, sent["request_id"], "done")
    if over.get("missing"):
        (ctrl._final / "_sync_result.json").unlink()
    else:
        payload = dict(over)
        payload.pop("missing", None)
        rid = payload.pop("request_id", sent["request_id"])
        _put_result(ctrl, rid, **payload)
    shown = ctrl.rebase_tick(100.0)
    assert "complete" not in _kinds(ctrl)
    assert "discard" not in _kinds(ctrl)
    assert shown["message"] == rb.MSG_RESULT_UNCONFIRMED
    assert ctrl.rebase_view()["phase"] == "failed"
    assert ctrl.rebase_view()["can_cancel"] is False
    again = ctrl.rebase_tick(102.0)
    assert again["message"] == ""
    assert ctrl.rebase_view()["text"] == rb.MSG_RESULT_UNCONFIRMED
    assert sum(1 for _s, text in ctrl._history if text == rb.MSG_RESULT_UNCONFIRMED) == 1
    assert reason_file


def test_killswitch_and_scene_wait_without_complete(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    ctrl._config["uvremap_enabled"] = False
    shown = ctrl.rebase_tick(100.0)
    assert "complete" not in _kinds(ctrl)
    assert shown["message"] == integ.MSG_REBASE_DONE_SKIPPED_FMT["killswitch_off"]

    ctrl._config["uvremap_enabled"] = True
    ctrl._runtime.scene_id = "scene-other"
    ctrl._rebase_last_tick_s = None
    shown = ctrl.rebase_tick(100.0)
    assert "complete" not in _kinds(ctrl)
    assert shown["message"] == integ.MSG_REBASE_DONE_SKIPPED_FMT["scene_changed"]


def test_job_running_does_not_consume_retries_partial_stops_at_five(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    ctrl._runtime.complete_results = [
        {"status": "skipped", "reason": "job_running", "failed_step": None}
        for _ in range(8)
    ]
    for step in range(8):
        ctrl.rebase_tick(100.0 + step)
    assert _kinds(ctrl).count("complete") == 8
    assert ctrl._rebase_complete_tries.get(sent["request_id"], 0) == 0
    assert ctrl.rebase_view()["text"] == integ.MSG_REBASE_DONE_SKIPPED_FMT["job_running"]
    assert integ.MSG_REBASE_RETRY_EXHAUSTED not in [text for _s, text in ctrl._history]

    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    ctrl._runtime.complete_results = [
        {"status": "partial", "reason": "", "failed_step": "active"}
        for _ in range(6)
    ]
    shown = ctrl.rebase_tick(200.0)
    assert "active" in shown["message"]
    assert "もう一度試します" in shown["message"]
    messages = [shown["message"]]
    for step in range(1, 5):
        messages.append(ctrl.rebase_tick(200.0 + step)["message"])
    assert _kinds(ctrl).count("complete") == 5
    assert integ.MSG_REBASE_RETRY_EXHAUSTED in messages
    assert ctrl.rebase_view()["text"] == integ.MSG_REBASE_RETRY_EXHAUSTED
    assert sum(1 for _s, text in ctrl._history if text == integ.MSG_REBASE_RETRY_EXHAUSTED) == 1
    stopped = ctrl.rebase_tick(210.0)
    assert _kinds(ctrl).count("complete") == 5
    assert stopped["message"] == ""


def test_foreign_and_completed_records_are_left_alone(tmp_path):
    ctrl = _make(tmp_path)
    foreign = rb.build_request(
        request_id="rb-20261019-010203-abcd",
        project_key=PROJECT,
        created_at=10,
        ttl_s=1800,
        mesh={"path": "mesh.fbx", "size": 4, "sha1": SHA},
        baseline_id="bl-foreign",
        ts_names=["Body"],
        final_full=True,
    )
    rb.write_request(ctrl._work, foreign)
    _put_status(ctrl, foreign["request_id"], "failed", message="x")
    ctrl.rebase_tick(100.0)
    assert "discard" not in _kinds(ctrl)
    assert "complete" not in _kinds(ctrl)

    sent = ctrl.rebase_send()
    ctrl._runtime.records[sent["request_id"]]["completed"] = True
    _put_status(ctrl, sent["request_id"], "failed", message="x")
    ctrl._rebase_last_tick_s = None
    ctrl.rebase_tick(100.0)
    assert not any(
        item[0] == "discard" and item[2] == "bl-cand" for item in ctrl._order
    )
    assert "complete" not in _kinds(ctrl)


@pytest.mark.parametrize("state", ["failed", "declined", "rejected", "expired", "superseded"])
def test_terminal_discards_candidate_once(tmp_path, state):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    extra = {}
    if state == "failed":
        extra = {"message": "書き出せません", "backup_name": "C:/hidden/backup.spp"}
    _put_status(ctrl, sent["request_id"], state, **extra)
    shown = ctrl.rebase_tick(100.0)
    assert _kinds(ctrl).count("discard") == 1
    assert "complete" not in _kinds(ctrl)
    assert "revert" not in _kinds(ctrl)
    if state == "failed":
        assert "backup.spp" in shown["message"]
        assert "C:" not in shown["message"]
        assert any("backup.spp" in text for _s, text in ctrl._history)
    ctrl.rebase_tick(102.0)
    assert _kinds(ctrl).count("discard") == 1
    assert sum(1 for _s, text in ctrl._history if text == shown["message"]) == 1


def test_missing_status_past_expiry_discards_once(tmp_path):
    ctrl = _make(tmp_path, time_value=5000.0)
    sent = ctrl.rebase_send()
    ctrl._clock["t"] = 5000.0 + 1801
    shown = ctrl.rebase_tick(ctrl._clock["t"])
    assert shown["message"] == rb.MSG_EXPIRED
    assert _kinds(ctrl).count("discard") == 1
    assert "complete" not in _kinds(ctrl)
    assert "revert" not in _kinds(ctrl)
    ctrl.rebase_tick(ctrl._clock["t"] + 5)
    assert _kinds(ctrl).count("discard") == 1


def test_cancel_only_before_lock(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    result = ctrl.rebase_cancel()
    assert result["ok"] is True
    assert result["message"] == integ.MSG_REBASE_CANCEL_SENT
    assert rb.cancel_exists(ctrl._work, sent["request_id"])
    assert ctrl.rebase_view()["text"] == rb.MSG_CANCELING

    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _put_status(ctrl, sent["request_id"], "backing_up")
    result = ctrl.rebase_cancel()
    assert result["ok"] is False
    assert result["message"] == rb.MSG_CANNOT_CANCEL
    assert not rb.cancel_exists(ctrl._work, sent["request_id"])

    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    assert rb.read_status(ctrl._work, sent["request_id"]).kind == "missing"
    assert ctrl.rebase_view()["can_cancel"] is True


def test_follow_and_uv_stop_while_rebase_is_open(tmp_path):
    ctrl = _make(tmp_path)
    follow_calls = []
    uv_calls = []
    ctrl._follow_tick_impl = lambda now_ns: follow_calls.append(now_ns) or {"ok": True, "message": ""}
    ctrl._uv_tick_impl = lambda now_s: uv_calls.append(now_s) or {"ok": True, "message": ""}
    assert ctrl.follow_tick(10)["ok"] is True
    assert ctrl.uv_tick(10)["ok"] is True
    assert follow_calls == [10]
    assert uv_calls == [10]

    sent = ctrl.rebase_send()
    ctrl.follow_tick(11)
    ctrl.uv_tick(11)
    assert follow_calls == [10]
    assert uv_calls == [10]
    ctrl.convert_now()
    assert "convert_now" in _kinds(ctrl)

    _put_status(ctrl, sent["request_id"], "done")
    (ctrl._final / "_sync_result.json").unlink()
    ctrl.rebase_tick(100.0)
    ctrl.follow_tick(12)
    assert follow_calls == [10]

    _put_status(ctrl, sent["request_id"], "declined")
    ctrl.rebase_tick(102.0)
    ctrl.follow_tick(13)
    ctrl.uv_tick(13)
    assert follow_calls[-1] == 13
    assert uv_calls[-1] == 13


def test_poll_interval_and_clamp(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    ctrl._runtime.complete_results = [
        {"status": "skipped", "reason": "job_running", "failed_step": None}
        for _ in range(4)
    ]
    ctrl.rebase_tick(10.0)
    ctrl.rebase_tick(10.4)
    assert _kinds(ctrl).count("complete") == 1
    ctrl.rebase_tick(11.0)
    assert _kinds(ctrl).count("complete") == 2

    ctrl = _make(tmp_path, cfg={"uvremap_rebase_poll_s": 0.01})
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    ctrl._runtime.complete_results = [
        {"status": "skipped", "reason": "job_running", "failed_step": None}
        for _ in range(3)
    ]
    ctrl.rebase_tick(10.0)
    ctrl.rebase_tick(10.4)
    assert _kinds(ctrl).count("complete") == 1
    ctrl.rebase_tick(10.5)
    assert _kinds(ctrl).count("complete") == 2

    ctrl = _make(tmp_path, cfg={"uvremap_rebase_poll_s": 100})
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    ctrl._runtime.complete_results = [
        {"status": "skipped", "reason": "job_running", "failed_step": None}
        for _ in range(3)
    ]
    ctrl.rebase_tick(10.0)
    ctrl.rebase_tick(20.0)
    assert _kinds(ctrl).count("complete") == 1
    ctrl.rebase_tick(40.0)
    assert _kinds(ctrl).count("complete") == 2


def test_run_poll_tick_order_and_missing_or_raising_rebase():
    class Dummy:
        def __init__(self):
            self.log = []

        def poll(self):
            self.log.append("poll")
            return {"ok": True, "message": "p"}

        def rebase_tick(self, now_s):
            self.log.append(("rebase", now_s))
            return {"ok": True, "message": "r"}

        def follow_tick(self, now_ns):
            self.log.append(("follow", now_ns))
            return {"ok": True, "message": ""}

        def uv_tick(self, now_s):
            self.log.append(("uv", now_s))
            return {"ok": True, "message": ""}

    class Handle:
        def __init__(self):
            self.texts = []

        def set_message(self, text):
            self.texts.append(text)

        def _refresh_panel(self):
            self.texts.append("refresh")

    dummy = Dummy()
    handle = Handle()
    integ.run_poll_tick(dummy, handle, now_ns=2_000_000_000)
    assert [item if isinstance(item, str) else item[0] for item in dummy.log] == [
        "poll", "rebase", "follow", "uv",
    ]
    assert dummy.log[1][1] == 2.0
    assert "r" in handle.texts

    class NoRebase:
        def __init__(self):
            self.log = []

        def poll(self):
            self.log.append("poll")
            return {"message": ""}

        def follow_tick(self, now_ns):
            self.log.append("follow")
            return {"message": ""}

        def uv_tick(self, now_s):
            self.log.append("uv")
            return {"message": ""}

    plain = NoRebase()
    integ.run_poll_tick(plain, Handle(), now_ns=1)
    assert plain.log == ["poll", "follow", "uv"]

    def boom(_now):
        dummy.log.append("rebase-boom")
        raise RuntimeError("rebase")

    dummy.rebase_tick = boom
    dummy.log.clear()
    integ.run_poll_tick(dummy, Handle(), now_ns=3_000_000_000)
    assert "follow" in [item if isinstance(item, str) else item[0] for item in dummy.log]


def test_handle_rebase_request_skips_confirm_until_ready(tmp_path):
    ctrl = _make(tmp_path, enabled=False)
    confirms = []
    informed = []
    result = integ.handle_rebase_request(
        ctrl,
        lambda text: confirms.append(text) or True,
        inform_fn=lambda text: informed.append(text),
    )
    assert result["ok"] is False
    assert confirms == []
    assert informed == [integ.MSG_REBASE_NEED_KILLSWITCH]

    ctrl = _make(tmp_path)
    first = ctrl.rebase_send()
    assert first["ok"] is True

    def explode(_text):
        raise RuntimeError("dialog")

    result = integ.handle_rebase_request(ctrl, explode)
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert len(rb.list_requests(ctrl._work)) == 1


def test_public_methods_swallow_runtime_errors(tmp_path):
    ctrl = _make(tmp_path)
    ctrl._runtime.fail.add("candidate")
    assert ctrl.rebase_send()["ok"] is False
    ctrl._runtime.fail.add("record_raise")
    assert ctrl.rebase_tick(1.0)["ok"] is True
    assert ctrl.rebase_view()["text"] == ""
    ctrl._runtime.fail.discard("record_raise")
    ctrl._runtime.fail.discard("candidate")
    sent = ctrl.rebase_send()
    assert sent["ok"] is True
    ctrl._runtime.fail.add("record_raise")

    def raise_cancel(_root, _request_id):
        raise RuntimeError("cancel")

    original = rb.write_cancel
    rb.write_cancel = raise_cancel
    try:
        assert isinstance(ctrl.rebase_cancel(), dict)
    finally:
        rb.write_cancel = original
    assert isinstance(ctrl.rebase_view(), dict)


class _Bar:
    def __init__(self):
        self.texts = []

    def set_message(self, text):
        self.texts.append(text)


def test_poll_keeps_send_text_and_other_button_text(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    assert sent["message"] == integ.MSG_REBASE_SENT
    handle = _Bar()
    integ.run_poll_tick(ctrl, handle, now_ns=2_000_000_000)
    assert handle.texts == []
    handle.texts.append("変換を開始しました")
    integ.run_poll_tick(ctrl, handle, now_ns=5_000_000_000)
    assert handle.texts == ["変換を開始しました"]

    def dirty(shape_paths, meshes_dir, baseline_id, triangulate=False, **kwargs):
        path = Path(meshes_dir) / "{0}.fbx".format(baseline_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FBX!")
        return (
            {"path": str(path), "size": 4, "sha1": SHA},
            {
                "applied": True,
                "name": "uvOld",
                "warnings": ["uvold_cleanup_failed"],
                "shapes": ["pSphereShape1"],
            },
        )

    ctrl = _make(tmp_path, export=dirty)
    result = ctrl.rebase_send()
    assert "削除できませんでした" in result["message"]
    handle = _Bar()
    integ.run_poll_tick(ctrl, handle, now_ns=2_000_000_000)
    assert handle.texts == []
    _put_status(ctrl, result["request_id"], "received")
    integ.run_poll_tick(ctrl, handle, now_ns=8_000_000_000)
    assert handle.texts == [rb.MSG_PREPARING]


def test_export_and_cleanup_failure_reaches_screen_and_history(tmp_path):
    def failing(*_args, **_kwargs):
        err = RuntimeError("export")
        err.uvold_info = {
            "applied": True,
            "name": "uvOld",
            "warnings": ["uvold_cleanup_failed"],
            "shapes": ["pSphereShape1"],
        }
        raise err

    ctrl = _make(tmp_path, export=failing)
    result = ctrl.rebase_send()
    assert result["ok"] is False
    assert "削除できませんでした" in result["message"]
    assert "pSphereShape1" in result["message"]
    assert integ.MSG_REBASE_SEND_FAILED not in result["message"]
    assert any("pSphereShape1" in text for _source, text in ctrl._history)
    assert "/" not in result["message"]


def test_exception_after_write_request_is_still_sent(tmp_path, monkeypatch):
    ctrl = _make(tmp_path)

    def boom(self, _uvold):
        raise RuntimeError("after write")

    monkeypatch.setattr(integ.UVRemapController, "_uvold_history_line", boom)
    result = ctrl.rebase_send()
    assert result["ok"] is True
    assert result["message"] != integ.MSG_REBASE_SEND_FAILED
    assert "送れませんでした" not in result["message"]
    assert ctrl._rebase_blocks_follow is True
    assert len(rb.list_requests(ctrl._work)) == 1


def test_follow_block_releases_except_open_and_recent_done(tmp_path):
    ctrl = _make(tmp_path)
    calls = []
    ctrl._follow_tick_impl = lambda now_ns: calls.append(now_ns) or {"ok": True, "message": ""}
    sent = ctrl.rebase_send()
    ctrl.follow_tick(1)
    assert calls == []

    ctrl = _make(tmp_path)
    calls = []
    ctrl._follow_tick_impl = lambda now_ns: calls.append(now_ns) or {"ok": True, "message": ""}
    sent = ctrl.rebase_send()
    _put_status(ctrl, sent["request_id"], "done")
    (ctrl._final / "_sync_result.json").unlink()
    ctrl.rebase_tick(100.0)
    ctrl.follow_tick(1)
    assert calls == []
    assert ctrl.rebase_view()["text"] == rb.MSG_RESULT_UNCONFIRMED
    assert "discard" not in _kinds(ctrl)
    ctrl.rebase_tick(160.0)
    ctrl.follow_tick(2)
    assert calls == []
    ctrl.rebase_tick(161.0)
    ctrl.follow_tick(3)
    assert calls == [3]
    assert ctrl.rebase_view()["text"] == rb.MSG_RESULT_UNCONFIRMED
    assert "discard" not in _kinds(ctrl)

    ctrl = _make(tmp_path)
    calls = []
    ctrl._follow_tick_impl = lambda now_ns: calls.append(now_ns) or {"ok": True, "message": ""}
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    ctrl._runtime.complete_results = [
        {"status": "partial", "reason": "", "failed_step": "active"}
        for _ in range(6)
    ]
    for step in range(5):
        ctrl.rebase_tick(300.0 + step)
    assert ctrl.rebase_view()["text"] == integ.MSG_REBASE_RETRY_EXHAUSTED
    # RB-d2: 上限到達は settled。追従の 60 秒止めは外れる。表示は上限の文言のまま。
    ctrl.follow_tick(1)
    assert calls == [1]
    ctrl.rebase_tick(361.0)
    ctrl.follow_tick(2)
    assert calls == [1, 2]
    assert ctrl.rebase_view()["text"] == integ.MSG_REBASE_RETRY_EXHAUSTED
    assert "discard" not in _kinds(ctrl)

    ctrl = _make(tmp_path)
    calls = []
    ctrl._follow_tick_impl = lambda now_ns: calls.append(now_ns) or {"ok": True, "message": ""}
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    ctrl._runtime.scene_id = "scene-other"
    ctrl.rebase_tick(400.0)
    assert ctrl._rebase_blocks_follow is False
    ctrl.follow_tick(9)
    assert calls == [9]
    assert "complete" not in _kinds(ctrl)
    assert ctrl._runtime.records[sent["request_id"]]["scene_id"] != ctrl._runtime.scene_id

    ctrl._clock["t"] += 1
    second = ctrl.rebase_send()
    assert second["ok"] is True
    ctrl.rebase_tick(402.0)
    ctrl.follow_tick(10)
    assert calls == [9]
    assert ctrl._rebase_blocks_follow is True


def test_restart_does_not_repeat_terminal_history(tmp_path):
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _put_status(ctrl, sent["request_id"], "failed", message="書き出せません")
    ctrl.rebase_tick(100.0)
    failed = [text for _source, text in ctrl._history if "書き出せません" in text]
    assert failed
    ctrl._rebase_live_ids.clear()
    ctrl._rebase_noted.clear()
    ctrl._rebase_discarded.clear()
    ctrl._rebase_frozen_view.clear()
    ctrl._rebase_emitted_text = None
    ctrl._rebase_last_tick_s = None
    ctrl.rebase_tick(200.0)
    assert [text for _source, text in ctrl._history if "書き出せません" in text] == failed

    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    ctrl._rebase_live_ids.clear()
    ctrl.rebase_tick(100.0)
    _put_status(ctrl, sent["request_id"], "failed", message="書き出せません")
    ctrl._rebase_last_tick_s = None
    ctrl.rebase_tick(102.0)
    assert any("書き出せません" in text for _source, text in ctrl._history)


def test_settled_requests_skip_later_status_reads(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = rb.read_status

    def wrapped(root, request_id):
        calls["n"] += 1
        return real(root, request_id)

    monkeypatch.setattr(rb, "read_status", wrapped)
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _put_status(ctrl, sent["request_id"], "declined")
    calls["n"] = 0
    ctrl.rebase_tick(100.0)
    assert calls["n"] >= 1
    calls["n"] = 0
    ctrl.rebase_tick(102.0)
    assert calls["n"] == 0
    ctrl.rebase_view()
    assert calls["n"] == 0

    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    calls["n"] = 0
    ctrl.rebase_tick(200.0)
    assert calls["n"] >= 1
    assert _kinds(ctrl).count("complete") == 1
    calls["n"] = 0
    ctrl.rebase_tick(202.0)
    assert calls["n"] == 0
    ctrl.rebase_view()
    assert calls["n"] == 0
    assert ctrl.rebase_view()["text"] == rb.MSG_DONE_GUIDANCE


def test_exhausted_done_skips_later_status_reads(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = rb.read_status

    def wrapped(root, request_id):
        calls["n"] += 1
        return real(root, request_id)

    monkeypatch.setattr(rb, "read_status", wrapped)
    ctrl = _make(tmp_path)
    sent = ctrl.rebase_send()
    _arm_done(ctrl, sent["request_id"])
    ctrl._runtime.complete_results = [
        {"status": "partial", "reason": "", "failed_step": "active"}
        for _ in range(6)
    ]
    for step in range(5):
        ctrl.rebase_tick(300.0 + step)
    assert sent["request_id"] in ctrl._rebase_exhausted
    assert ctrl.rebase_view()["text"] == integ.MSG_REBASE_RETRY_EXHAUSTED
    calls["n"] = 0
    again = ctrl.rebase_tick(310.0)
    assert calls["n"] == 0
    assert again["message"] == ""
    assert ctrl.rebase_view()["text"] == integ.MSG_REBASE_RETRY_EXHAUSTED
    ctrl.rebase_view()
    assert calls["n"] == 0
