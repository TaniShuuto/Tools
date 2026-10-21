# -*- coding: utf-8 -*-
"""uvremap_integration.py のテスト(Maya 無し・Pillow 無し)。

Qt は PySide6 があるときだけ QT_QPA_PLATFORM=offscreen で実行する。
時刻には依存しない。
"""
from __future__ import annotations

import ast
import json
import os
import py_compile
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
_REPO = _PKG.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fresh_import_probe import new_forbidden_modules  # noqa: E402
import uvremap_follow as follow  # noqa: E402
import uvremap_integration as integ  # noqa: E402


FIXED_NOW = "2026-03-04T05:06:07"
PROJECT = "proj_a"
SUBFOLDER = "Table_445ba2"
ROOT_DIR = "C:/SPMayaLiveSync/uvremap"
FINAL_ROOT = "C:/SPMayaLiveSync/final"
WATCH_ROOT = "C:/SPMayaLiveSync/live"


def test_import_does_not_load_maya_qt_or_pillow():
    # 先に走った別のテストが本物の PySide6 / PIL を読み込んでいても影響を受けないよう、
    # 新しいインタープリタで import して、禁止モジュールが増えないことを確かめる。
    assert new_forbidden_modules(_PKG, ["uvremap_integration", "uvremap_follow"]) == []


def test_source_imports_maya_qt_only_inside_functions():
    source = Path(integ.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("maya")
                assert alias.name not in ("PySide6", "PySide2", "PIL")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("maya")
            assert not mod.startswith("PySide6")
            assert not mod.startswith("PySide2")
            assert mod != "PIL" and not mod.startswith("PIL.")


# ---------------------------------------------------------------------------
# 純粋部分
# ---------------------------------------------------------------------------
def test_resolve_targets_keeps_present_ts_and_union_of_shapes():
    known = ["Body", "Trim", "Missing"]
    texture_sets = {
        "Body": [
            {"shape": "|grp|meshB"},
            {"shape": "|grp|meshA"},
        ],
        "Trim": [{"shape": "|grp|meshB"}],
        "Other": [{"shape": "|skip"}],
    }
    ts_names, shapes, missing = integ.resolve_targets(known, texture_sets)
    assert ts_names == ["Body", "Trim"]
    assert shapes == ["|grp|meshA", "|grp|meshB"]
    assert missing == ["Missing"]


def test_resolve_targets_zero_known_or_none_present():
    ts_names, shapes, missing = integ.resolve_targets(["Body"], {})
    assert ts_names == []
    assert shapes == []
    assert missing == ["Body"]
    empty = integ.resolve_targets([], {"Body": [{"shape": "|m"}]})
    assert empty == ([], [], [])


def test_resolve_targets_accepts_dict_known_ts():
    ts_names, shapes, missing = integ.resolve_targets(
        {"Body": True, "Gone": 1},
        {"Body": [{"shape": "|m"}]},
    )
    assert ts_names == ["Body"]
    assert shapes == ["|m"]
    assert missing == ["Gone"]


def test_uvremap_root_is_outside_final_and_watch():
    root = integ.uvremap_root_for_project(ROOT_DIR, SUBFOLDER)
    out = integ.output_dir_for_root(root)
    final_dir = os.path.normpath(os.path.join(FINAL_ROOT, SUBFOLDER))
    watch_dir = os.path.normpath(os.path.join(WATCH_ROOT, SUBFOLDER))
    assert root == os.path.normpath(os.path.join(ROOT_DIR, SUBFOLDER))
    assert out == os.path.normpath(os.path.join(root, "output", "final"))
    assert integ.work_folder_is_safe(
        root, final_dir=final_dir, watch_dir=watch_dir, final_export_dir=FINAL_ROOT,
    )
    assert not integ.path_is_under(root, FINAL_ROOT)
    assert not integ.path_is_under(root, WATCH_ROOT)
    assert not integ.path_is_under(out, FINAL_ROOT)
    assert not integ.path_is_under(out, WATCH_ROOT)
    assert integ.uvremap_root_for_project(ROOT_DIR, None) is None
    assert integ.uvremap_root_for_project(ROOT_DIR, "") is None


def test_work_folder_inside_final_is_rejected():
    inside = os.path.normpath(os.path.join(FINAL_ROOT, "uvremap_work"))
    assert not integ.work_folder_is_safe(
        inside, final_dir=os.path.join(FINAL_ROOT, SUBFOLDER),
        watch_dir=WATCH_ROOT, final_export_dir=FINAL_ROOT,
    )


def test_lookup_project_entry_is_per_project():
    by_project = {
        "proj_a": ["Body"],
        "proj_b": ["Trim", "Body"],
    }
    assert list(integ.lookup_project_entry(by_project, "proj_a")) == ["Body"]
    assert list(integ.lookup_project_entry(by_project, "PROJ_A")) == ["Body"]
    assert integ.lookup_project_entry(by_project, None) == []


def test_decide_baseline_prompt():
    kind, text = integ.decide_baseline_prompt(True)
    assert kind == "already"
    assert text == integ.MSG_BASELINE_EXISTS
    kind, text = integ.decide_baseline_prompt(False)
    assert kind == "confirm"
    assert text == integ.MSG_CONFIRM_BASELINE


def test_warning_message_prefers_status_text_except_missing():
    assert integ.warning_message({"warning": "scene_changed", "message": integ.MSG_SCENE_CHANGED}) == integ.MSG_SCENE_CHANGED
    assert integ.warning_message({"warning": "no_file_nodes_switched", "message": integ.MSG_NO_FILE_NODES}) == integ.MSG_NO_FILE_NODES
    assert integ.warning_message({"warning": "converted_file_missing", "message": "ok"}) == integ.MSG_FILE_MISSING
    assert integ.warning_message({"warning": integ.WARN_DISABLED, "message": ""}) == integ.MSG_DISABLED_SKIP
    assert integ.warning_message({"state": "ORIGINAL"}) == ""


# ---------------------------------------------------------------------------
# コントローラ
# ---------------------------------------------------------------------------
class FakeRuntime:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None
        self.convert_calls = []
        self.revert_calls = []
        self.record_calls = []
        self.recover_calls = []
        self.recover_result = []
        self.poll_events = []
        self.fail_convert = False
        self.fail_record = False
        self.empty_job = False
        self.already_running = set()
        self.queue_ts = set()
        self.stale_calls = []
        self.scene_id = "scene.ma"
        self.revert_result = ["file1"]
        self.revert_raises = False

    def record_baseline(self, project_key, ts_names, shape_paths, **kwargs):
        if self.fail_record:
            raise RuntimeError("record boom")
        self.record_calls.append((project_key, list(ts_names), list(shape_paths)))
        return "bl-fixed"

    def convert_now(self, project_key, ts_name, shape_paths, source_dir, output_dir, **kwargs):
        if self.fail_convert:
            raise RuntimeError("convert boom")
        self.convert_calls.append({
            "project_key": project_key,
            "ts_name": ts_name,
            "shape_paths": list(shape_paths),
            "source_dir": os.path.normpath(str(source_dir)),
            "output_dir": os.path.normpath(str(output_dir)),
            "timeout_s": kwargs.get("timeout_s"),
            "quality": kwargs.get("quality"),
            "origin": kwargs.get("origin", "manual"),
        })
        if self.empty_job:
            return ""
        if ts_name in self.already_running or ts_name in self.queue_ts:
            return "queued:{0}".format(ts_name)
        return "job-{0}".format(ts_name)

    def has_running_job_for_ts(self, ts_name):
        return ts_name in self.already_running

    def mark_stale(self, project_key, ts_name, message):
        self.stale_calls.append((project_key, ts_name, message))
        return True

    def _scene_id(self):
        return self.scene_id

    def recover_orphaned(self, project_key):
        self.recover_calls.append(project_key)
        return list(self.recover_result)

    def revert_to_original(self, ts_name=None):
        self.revert_calls.append(ts_name)
        if self.revert_raises:
            raise RuntimeError("revert boom")
        return self.revert_result

    def poll(self):
        return list(self.poll_events)

    def status_snapshot(self, project_key):
        if self.root is None:
            raise RuntimeError("no root")
        return self.root / "status.json"


def _make_controller(tmp_path, *, enabled=True, project=PROJECT, known=None,
                     texture_sets=None, final_dir=None, subfolder=SUBFOLDER,
                     runtime=None, history=None, cfg=None, raise_project=False,
                     uv_fingerprints_fn=None):
    root_dir = tmp_path / "uvremap"
    work = root_dir / SUBFOLDER
    work.mkdir(parents=True, exist_ok=True)
    if runtime is None:
        runtime = FakeRuntime(work)
    else:
        runtime.root = work
    final = final_dir
    if final is None and project:
        final = os.path.normpath(str(tmp_path / "final" / SUBFOLDER))
    config = {
        "uvremap_enabled": enabled,
        "uvremap_root_dir": str(root_dir),
        "uvremap_mayapy_path": "",
        "watch_dir": str(tmp_path / "live"),
        "final_export_dir": str(tmp_path / "final"),
        "known_texture_sets_by_project": {PROJECT: ["Body", "Trim"]},
    }
    if cfg:
        config.update(cfg)
    saved = []
    history_rows = history if history is not None else []

    if texture_sets is None:
        texture_sets = {
            "Body": [{"shape": "|meshA"}],
            "Trim": [{"shape": "|meshB"}],
        }
    if known is None:
        known = ["Body", "Trim"]

    def project_key_fn():
        if raise_project:
            raise RuntimeError("no project")
        return project

    ctrl = integ.UVRemapController(
        get_config=lambda: config,
        save_config_fn=lambda partial: saved.append(dict(partial)),
        project_key_fn=project_key_fn,
        known_ts_fn=lambda _key: list(known),
        final_dir_fn=lambda _key: final,
        final_subfolder_fn=lambda _key: subfolder,
        list_texture_sets_fn=lambda: texture_sets,
        runtime=runtime,
        history_fn=lambda source, text: history_rows.append((source, text)),
        uv_fingerprints_fn=uv_fingerprints_fn,
    )
    ctrl._config = config
    ctrl._saved = saved
    ctrl._history = history_rows
    ctrl._work = work
    ctrl._runtime = runtime
    return ctrl


def test_controller_no_project_link(tmp_path):
    ctrl = _make_controller(tmp_path, project=None)
    result = ctrl.convert_now()
    assert result["ok"] is False
    assert result["message"] == integ.MSG_NO_PROJECT
    assert ctrl._runtime.convert_calls == []
    assert ctrl.status_path()["path"] is None
    assert ctrl.status_path()["message"] == integ.MSG_NO_PROJECT


def test_controller_no_final_dir(tmp_path):
    ctrl = _make_controller(tmp_path, final_dir="")
    result = ctrl.record_baseline()
    assert result["ok"] is False
    assert result["message"] == integ.MSG_NO_FINAL


def test_controller_zero_targets(tmp_path):
    ctrl = _make_controller(
        tmp_path, known=["Gone"], texture_sets={"Body": [{"shape": "|m"}]},
    )
    result = ctrl.convert_now()
    assert result["ok"] is False
    assert integ.MSG_NO_TARGETS in result["message"]
    assert "Gone" in result["message"]
    assert ctrl._runtime.convert_calls == []


def test_controller_kill_switch_off_skips_convert(tmp_path):
    ctrl = _make_controller(tmp_path, enabled=False)
    result = ctrl.convert_now()
    assert result["ok"] is False
    assert result["message"] == integ.MSG_DISABLED
    assert ctrl._runtime.convert_calls == []


def test_controller_convert_blocked_without_baseline(tmp_path):
    ctrl = _make_controller(tmp_path, enabled=True)
    result = ctrl.convert_now()
    assert result["ok"] is False
    assert result["message"] == integ.MSG_NO_BASELINE
    assert ctrl._runtime.convert_calls == []


def test_record_baseline_allowed_when_disabled(tmp_path):
    ctrl = _make_controller(tmp_path, enabled=False)
    result = ctrl.record_baseline()
    assert result["ok"] is True
    assert result["baseline_id"] == "bl-fixed"
    assert ctrl._runtime.record_calls


def test_record_baseline_refuses_overwrite(tmp_path):
    ctrl = _make_controller(tmp_path)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True)
    (dest / "mesh.npz").write_bytes(b"x")
    result = ctrl.record_baseline()
    assert result["ok"] is False
    assert result["already"] is True
    assert result["message"] == integ.MSG_BASELINE_EXISTS
    assert ctrl._runtime.record_calls == []


def test_handle_record_request_confirm_then_record(tmp_path):
    ctrl = _make_controller(tmp_path)
    prompts = []

    def confirm(text):
        prompts.append(("confirm", text))
        return True

    result = integ.handle_record_request(ctrl, confirm)
    assert result["ok"] is True
    assert prompts == [("confirm", integ.MSG_CONFIRM_BASELINE)]
    assert ctrl._runtime.record_calls


def test_handle_record_request_already_does_not_record(tmp_path):
    ctrl = _make_controller(tmp_path)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True)
    (dest / "mesh.npz").write_bytes(b"x")
    informed = []
    result = integ.handle_record_request(
        ctrl, confirm_fn=lambda _t: True, inform_fn=lambda t: informed.append(t),
    )
    assert result["ok"] is False
    assert result["already"] is True
    assert informed == [integ.MSG_BASELINE_EXISTS]
    assert ctrl._runtime.record_calls == []


def test_convert_now_calls_runtime_per_ts(tmp_path):
    ctrl = _make_controller(tmp_path)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True)
    (dest / "mesh.npz").write_bytes(b"x")
    result = ctrl.convert_now()
    assert result["ok"] is True
    names = [call["ts_name"] for call in ctrl._runtime.convert_calls]
    assert names == ["Body", "Trim"]
    for call in ctrl._runtime.convert_calls:
        assert call["timeout_s"] == integ.CONVERT_TIMEOUT_S
        assert call["quality"] == "final"
        assert call["output_dir"] == integ.output_dir_for_root(str(ctrl._work))
        assert not integ.path_is_under(call["output_dir"], str(tmp_path / "final"))
        assert not integ.path_is_under(call["output_dir"], str(tmp_path / "live"))
        assert call["shape_paths"] == ["|meshA", "|meshB"]


def test_set_enabled_false_calls_revert(tmp_path):
    ctrl = _make_controller(tmp_path, enabled=True)
    result = ctrl.set_enabled(False)
    assert result["ok"] is True
    assert result["enabled"] is False
    assert ctrl._runtime.revert_calls == [None]
    assert ctrl._saved[-1] == {"uvremap_enabled": False}
    assert ctrl._config["uvremap_enabled"] is False


def test_methods_do_not_raise(tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    ctrl = integ.UVRemapController(
        get_config=boom,
        save_config_fn=boom,
        project_key_fn=boom,
        known_ts_fn=boom,
        final_dir_fn=boom,
        final_subfolder_fn=boom,
        list_texture_sets_fn=boom,
        runtime=FakeRuntime(),
    )
    results = [
        ctrl.set_enabled(True),
        ctrl.set_auto_follow(True),
        ctrl.record_baseline(),
        ctrl.convert_now(),
        ctrl.revert(),
        ctrl.poll(),
        ctrl.follow_tick(0),
        ctrl.status_path(),
        ctrl.recover_orphaned(),
    ]
    for result in results[:-1]:
        assert isinstance(result, dict)
        assert "ok" in result
        assert "message" in result
    assert results[-1] == []
    assert ctrl.has_baseline() is False


def test_convert_now_already_running_returns_pending_message(tmp_path):
    ctrl = _make_controller(tmp_path)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True)
    (dest / "mesh.npz").write_bytes(b"x")
    ctrl._runtime.already_running = {"Body"}
    result = ctrl.convert_now()
    assert result["ok"] is True
    assert integ.MSG_PEND_FMT.format(ts="Body") in result["message"]
    assert "変換を開始しました" in result["message"]
    names = [call["ts_name"] for call in ctrl._runtime.convert_calls]
    assert names == ["Body", "Trim"]
    assert result["job_ids"] == ["queued:Body", "job-Trim"]
    assert result["pending"] == ["Body"]


def test_convert_now_all_reserved_does_not_say_started(tmp_path):
    ctrl = _make_controller(tmp_path)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True)
    (dest / "mesh.npz").write_bytes(b"x")
    ctrl._runtime.already_running = {"Body", "Trim"}
    result = ctrl.convert_now()
    assert result["ok"] is True
    assert "変換を開始しました" not in result["message"]
    assert integ.MSG_PEND_FMT.format(ts="Body") in result["message"]
    assert integ.MSG_PEND_FMT.format(ts="Trim") in result["message"]
    assert result["message"] != integ.MSG_NOT_STARTED
    assert result["job_ids"] == ["queued:Body", "queued:Trim"]


def test_convert_now_empty_jobs_returns_not_started(tmp_path):
    ctrl = _make_controller(tmp_path)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True)
    (dest / "mesh.npz").write_bytes(b"x")
    ctrl._runtime.empty_job = True
    result = ctrl.convert_now()
    assert result["ok"] is False
    assert result["message"] == integ.MSG_NOT_STARTED
    assert result["message"] != integ.MSG_NO_BASELINE
    assert result["job_ids"] == []
    assert ctrl._runtime.convert_calls


def test_controller_recover_orphaned_writes_history(tmp_path):
    ctrl = _make_controller(tmp_path)
    ctrl._runtime.recover_result = ["Trim"]
    names = ctrl.recover_orphaned()
    assert names == ["Trim"]
    assert ctrl._runtime.recover_calls == [PROJECT]
    assert ctrl._history == [("uvremap", integ.MSG_INTERRUPTED)]


def test_controller_recover_orphaned_no_project(tmp_path):
    ctrl = _make_controller(tmp_path, project=None)
    assert ctrl.recover_orphaned() == []
    assert ctrl._runtime.recover_calls == []
    assert ctrl._history == []


def test_controller_recover_orphaned_swallows(tmp_path):
    ctrl = _make_controller(tmp_path, raise_project=True)
    assert ctrl.recover_orphaned() == []


def test_start_poll_after_recover_runs_before_timer():
    order = []
    messages = []

    class Ctrl:
        def recover_orphaned(self):
            order.append("recover")
            return ["Body"]

    class Timer:
        def start(self):
            order.append("start")

    names = integ.start_poll_after_recover(Ctrl(), Timer(), messages.append)
    assert order == ["recover", "start"]
    assert names == ["Body"]
    assert messages == [integ.MSG_INTERRUPTED]


def test_start_poll_after_recover_swallows_and_starts():
    class Ctrl:
        def recover_orphaned(self):
            raise RuntimeError("boom")

    class Timer:
        def __init__(self):
            self.started = False

        def start(self):
            self.started = True

    timer = Timer()
    assert integ.start_poll_after_recover(Ctrl(), timer) == []
    assert timer.started is True


def test_on_scene_changed_recovers_orphaned(tmp_path):
    ctrl = _make_controller(tmp_path)
    ctrl._runtime.recover_result = ["Trim"]

    class Label:
        def __init__(self):
            self.text = ""

        def setText(self, text):
            self.text = text

    label = Label()
    handle = integ.UVRemapAttachment(None, ctrl, message_label=label)
    handle.on_scene_changed()
    assert label.text == integ.MSG_INTERRUPTED
    assert ctrl._runtime.recover_calls == [PROJECT]
    assert ctrl._history == [("uvremap", integ.MSG_INTERRUPTED)]


def test_poll_writes_warning_to_history(tmp_path):
    ctrl = _make_controller(tmp_path)
    ctrl._runtime.poll_events = [
        {"warning": "scene_changed", "message": integ.MSG_SCENE_CHANGED, "state": "ORIGINAL"},
        {
            "warning": "converted_file_missing",
            "message": "ok",
            "state": "CONVERTED_OK",
            "ts_name": "Body",
        },
    ]
    result = ctrl.poll()
    assert result["ok"] is True
    assert result["message"] == integ.MSG_FILE_MISSING
    assert ctrl._history == [
        ("uvremap", integ.MSG_SCENE_CHANGED),
        ("uvremap", integ.MSG_CONVERT_DONE_FMT.format(ts_name="Body")),
        ("uvremap", integ.MSG_FILE_MISSING),
    ]


def _done(ts_name):
    return integ.MSG_CONVERT_DONE_FMT.format(ts_name=ts_name)


def test_completion_message_states():
    assert integ.completion_message(
        {"state": "CONVERTED_OK", "ts_name": "Body", "exit_code": 0},
    ) == _done("Body")
    assert integ.completion_message(
        {"state": "FAILED", "ts_name": "Trim", "message": "プローブ不合格"},
    ) == integ.MSG_CONVERT_FAILED_FMT.format(ts_name="Trim", message="プローブ不合格")
    assert integ.completion_message(
        {"state": "FAILED", "ts_name": "Trim", "message": ""},
    ) == integ.MSG_CONVERT_FAILED_BARE_FMT.format(ts_name="Trim")
    assert integ.completion_message(
        {"state": "FAILED", "ts_name": "Trim"},
    ) == integ.MSG_CONVERT_FAILED_BARE_FMT.format(ts_name="Trim")
    assert integ.completion_message(
        {"state": "BLOCKED", "ts_name": "Face", "message": "基準が違います"},
    ) == integ.MSG_CONVERT_BLOCKED_FMT.format(ts_name="Face", message="基準が違います")
    assert integ.completion_message(
        {"state": "ORIGINAL", "ts_name": "Body", "exit_code": 0},
    ) == ""
    assert integ.completion_message(
        {"state": "CONVERTED_OK", "ts_name": "Body", "exit_code": 10},
    ) == ""
    assert integ.completion_message({"state": "WEIRD", "ts_name": "Body"}) == ""
    assert integ.completion_message("not-a-dict") == ""


def test_poll_completion_and_failure_messages(tmp_path):
    ctrl = _make_controller(tmp_path)
    ctrl._runtime.poll_events = [
        {"state": "CONVERTED_OK", "ts_name": "Body", "exit_code": 0, "message": ""},
    ]
    result = ctrl.poll()
    assert result["message"] == _done("Body")
    assert ctrl._history == [("uvremap", _done("Body"))]

    ctrl._history.clear()
    ctrl._runtime.poll_events = [
        {
            "state": "CONVERTED_OK",
            "ts_name": "Body",
            "exit_code": 0,
            "warning": "converted_file_missing",
            "message": "ok",
        },
    ]
    result = ctrl.poll()
    assert result["message"] == integ.MSG_FILE_MISSING
    assert ctrl._history == [
        ("uvremap", _done("Body")),
        ("uvremap", integ.MSG_FILE_MISSING),
    ]

    ctrl._history.clear()
    ctrl._runtime.poll_events = [
        {"state": "FAILED", "ts_name": "Trim", "exit_code": 12, "message": "ディスク不足"},
    ]
    result = ctrl.poll()
    failed = integ.MSG_CONVERT_FAILED_FMT.format(ts_name="Trim", message="ディスク不足")
    assert result["message"] == failed
    assert ctrl._history == [("uvremap", failed)]

    ctrl._history.clear()
    ctrl._runtime.poll_events = [
        {"state": "FAILED", "ts_name": "Trim", "exit_code": 12, "message": "  "},
    ]
    bare = integ.MSG_CONVERT_FAILED_BARE_FMT.format(ts_name="Trim")
    assert ctrl.poll()["message"] == bare

    ctrl._history.clear()
    ctrl._runtime.poll_events = [
        {"state": "BLOCKED", "ts_name": "Face", "exit_code": 11, "message": "UVがありません"},
    ]
    blocked = integ.MSG_CONVERT_BLOCKED_FMT.format(ts_name="Face", message="UVがありません")
    assert ctrl.poll()["message"] == blocked

    ctrl._history.clear()
    ctrl._runtime.poll_events = [
        {"state": "ORIGINAL", "ts_name": "Body", "exit_code": 0},
        {"state": "CONVERTED_OK", "ts_name": "Body", "exit_code": 10},
    ]
    result = ctrl.poll()
    assert result["message"] == ""
    assert ctrl._history == []

    ctrl._history.clear()
    ctrl._runtime.poll_events = [
        {"state": "CONVERTED_OK", "ts_name": "Body", "exit_code": 0},
        {"state": "FAILED", "ts_name": "Trim", "exit_code": 12, "message": "中断"},
    ]
    result = ctrl.poll()
    last = integ.MSG_CONVERT_FAILED_FMT.format(ts_name="Trim", message="中断")
    assert result["message"] == last
    assert ctrl._history == [
        ("uvremap", _done("Body")),
        ("uvremap", last),
    ]


def test_poll_tick_sets_message_on_completion(tmp_path):
    ctrl = _make_controller(tmp_path)
    ctrl._runtime.poll_events = [
        {"state": "CONVERTED_OK", "ts_name": "Body", "exit_code": 0},
    ]
    seen = []

    class Handle:
        def set_message(self, text):
            seen.append(text)

        def _refresh_panel(self):
            seen.append("refresh")

    result = integ.run_poll_tick(ctrl, Handle())
    assert result["message"] == _done("Body")
    assert seen == [_done("Body"), "refresh"]


def test_handle_record_request_skips_confirm_when_not_ready(tmp_path):
    ctrl = _make_controller(tmp_path, project=None)
    confirms = []
    informed = []

    def confirm(text):
        confirms.append(text)
        return True

    result = integ.handle_record_request(
        ctrl, confirm, inform_fn=lambda text: informed.append(text),
    )
    assert result["ok"] is False
    assert result["message"] == integ.MSG_NO_PROJECT
    assert confirms == []
    assert informed == [integ.MSG_NO_PROJECT]
    assert ctrl._runtime.record_calls == []


def test_handle_record_request_ready_still_confirms(tmp_path):
    ctrl = _make_controller(tmp_path)
    ready = ctrl.check_record_ready()
    assert ready == {"ok": True, "message": ""}
    prompts = []

    def confirm(text):
        prompts.append(text)
        return True

    result = integ.handle_record_request(ctrl, confirm)
    assert result["ok"] is True
    assert prompts == [integ.MSG_CONFIRM_BASELINE]
    assert ctrl._runtime.record_calls


def test_handle_record_request_without_check_record_ready():
    class OldController:
        def has_baseline(self):
            return False

        def record_baseline(self):
            self.recorded = True
            return {"ok": True, "message": "基準UVを記録しました"}

    ctrl = OldController()
    prompts = []
    result = integ.handle_record_request(ctrl, lambda text: prompts.append(text) or True)
    assert prompts == [integ.MSG_CONFIRM_BASELINE]
    assert result["ok"] is True
    assert ctrl.recorded is True


def test_set_enabled_false_message_follows_revert_count(tmp_path):
    ctrl = _make_controller(tmp_path, enabled=True)

    ctrl._runtime.revert_result = []
    empty = ctrl.set_enabled(False)
    assert empty["ok"] is True
    assert empty["message"] == integ.MSG_DISABLED_NO_REVERT

    ctrl._runtime.revert_result = ["a", "b"]
    two = ctrl.set_enabled(False)
    assert two["message"] == integ.MSG_DISABLED_REVERTED_FMT.format(n=2)

    ctrl._runtime.revert_result = "file1"
    other = ctrl.set_enabled(False)
    assert other["message"] == integ.MSG_DISABLED_REVERTED

    ctrl._runtime.revert_raises = True
    blown = ctrl.set_enabled(False)
    assert blown["ok"] is True
    assert blown["enabled"] is False
    assert blown["message"] == integ.MSG_DISABLED_REVERTED


# ---------------------------------------------------------------------------
# maya_live_sync.py の静的確認
# ---------------------------------------------------------------------------
ALLOWED_UVREMAP_FUNCS = {
    "LiveSyncWindow.__init__",
    "LiveSyncWindow.closeEvent",
    "LiveSyncWindow._on_scene_changed",
    "LiveSyncWatcher.reload_textures",
    "LiveSyncWatcher.reload_final_textures",
    "LiveSyncWatcher.switch_texture_quality",
    # D2h: 飛ばした件数の注記。Maya 非依存の純関数。
    "_uvremap_skip_note",
}


def _qualified_func_spans(tree):
    spans = []

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                spans.append((name, child.lineno, getattr(child, "end_lineno", child.lineno)))
                walk(child, name + ".")

    walk(tree)
    return spans


def test_maya_live_sync_py_compile():
    py_compile.compile(str(_PKG / "maya_live_sync.py"), doraise=True)


def test_maya_live_sync_uvremap_mentions_only_allowed():
    path = _PKG / "maya_live_sync.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    spans = _qualified_func_spans(tree)
    offenders = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if "uvremap" not in line.lower():
            continue
        owner = None
        for name, start, end in spans:
            if start <= lineno <= end:
                owner = name
        if owner is None:
            continue
        if owner not in ALLOWED_UVREMAP_FUNCS:
            offenders.append((owner, lineno, line.strip()))
    assert offenders == []


def test_maya_live_sync_has_new_config_keys_and_version():
    source = (_PKG / "maya_live_sync.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    version = None
    keys = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    if isinstance(node.value, ast.Constant):
                        version = node.value.value
                if isinstance(target, ast.Name) and target.id == "DEFAULT_CONFIG":
                    if isinstance(node.value, ast.Dict):
                        keys = []
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant):
                                keys.append(key.value)
    assert version == "1.7.1"
    assert "uvremap_enabled" in keys
    assert "uvremap_root_dir" in keys
    assert "uvremap_mayapy_path" in keys


def test_install_destroy_stale_window_calls_uvremap_shutdown():
    src = (_PKG / "install.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_destroy_stale_livesync_window":
            fn = node
            break
    assert fn is not None
    dump = ast.dump(fn)
    assert "_uvremap" in dump
    assert "shutdown" in dump


def test_qt_load_tries_pyside2_before_pyside6():
    for path in (_PKG / "uvremap_integration.py", _PKG / "uvremap_panel.py"):
        text = path.read_text(encoding="utf-8")
        i2 = text.find("from PySide2 import")
        i6 = text.find("from PySide6 import")
        assert i2 != -1 and i6 != -1
        assert i2 < i6


def test_install_and_uninstall_list_optional_uvremap_files():
    install_src = (_PKG / "install.py").read_text(encoding="utf-8")
    uninstall_src = (_PKG / "uninstall.py").read_text(encoding="utf-8")
    expected = [
        "uvremap_core.py",
        "uvremap_io.py",
        "uvremap_worker.py",
        "uvremap_bridge.py",
        "uvremap_maya.py",
        "uvremap_runtime.py",
        "uvremap_panel.py",
        "uvremap_integration.py",
        "uvremap_follow.py",
        "uvremap_rebase.py",
    ]
    for name in expected:
        assert name in install_src
        assert name in uninstall_src


# ---------------------------------------------------------------------------
# R2-01〜R2-05
# ---------------------------------------------------------------------------
def test_revert_message_follows_listed_count(tmp_path):
    ctrl = _make_controller(tmp_path)

    ctrl._runtime.revert_result = []
    empty = ctrl.revert()
    assert empty["ok"] is True
    assert empty["message"] == integ.MSG_REVERT_NONE
    assert empty["message"] == "戻す対象はありませんでした"
    assert empty["reverted"] == []

    ctrl._runtime.revert_result = ["a", "b"]
    two = ctrl.revert()
    assert two["ok"] is True
    assert two["message"] == integ.MSG_REVERT_OK_FMT.format(n=2)
    assert two["message"] == "原本に戻しました(2 件)"
    assert two["reverted"] == ["a", "b"]

    ctrl._runtime.revert_result = "file1"
    other = ctrl.revert()
    assert other["ok"] is True
    assert other["message"] == integ.MSG_REVERT_OK
    assert other["message"] == "原本に戻しました"
    assert other["reverted"] == []

    ctrl._runtime.revert_raises = True
    blown = ctrl.revert()
    assert blown["ok"] is False
    assert blown["message"] == integ.MSG_REVERT_FAIL
    assert blown["message"] == "原本への復帰に失敗しました"


def test_revert_message_one_item_uses_count():
    assert integ.revert_message(["only"]) == "原本に戻しました(1 件)"
    assert integ.revert_message(()) == integ.MSG_REVERT_NONE
    assert integ.revert_message(None) == integ.MSG_REVERT_OK


def test_warning_message_partial_when_some_channels_switched():
    base = {"warning": "converted_file_missing", "message": "ok", "state": "CONVERTED_OK"}
    partial = dict(base, switched=["file_base"])
    assert integ.warning_message(partial) == integ.MSG_FILE_PARTIAL
    assert integ.warning_message(partial) == (
        "一部のチャンネルだけ切り替えました。変換後のファイルが無いチャンネルは見送りました"
    )
    assert integ.warning_message(dict(base, switched=[])) == integ.MSG_FILE_MISSING
    missing_key = dict(base)
    assert "switched" not in missing_key
    assert integ.warning_message(missing_key) == integ.MSG_FILE_MISSING


def _assert_single_periods(text):
    assert "。。" not in text


def test_completion_message_follows_reverted_list():
    failed_detail = "プローブ不合格"
    blocked_detail = "基準が違います"
    cases = [
        (
            {"state": "FAILED", "ts_name": "Trim", "message": failed_detail, "reverted": ["file1"]},
            "変換に失敗し、原本に戻しました(Trim)。プローブ不合格",
        ),
        (
            {"state": "FAILED", "ts_name": "Trim", "message": "", "reverted": ["file1"]},
            "変換に失敗し、原本に戻しました(Trim)。",
        ),
        (
            {"state": "FAILED", "ts_name": "Trim", "reverted": ["file1", "file2"]},
            "変換に失敗し、原本に戻しました(Trim)。",
        ),
        (
            {"state": "FAILED", "ts_name": "Trim", "message": failed_detail, "reverted": []},
            "変換に失敗しました(Trim)。プローブ不合格",
        ),
        (
            {"state": "FAILED", "ts_name": "Trim", "message": "  ", "reverted": []},
            "変換に失敗しました(Trim)。",
        ),
        (
            {"state": "FAILED", "ts_name": "Trim", "message": failed_detail},
            "変換に失敗しました(Trim)。プローブ不合格",
        ),
        (
            {"state": "FAILED", "ts_name": "Trim"},
            "変換に失敗しました(Trim)。",
        ),
        (
            {"state": "BLOCKED", "ts_name": "Face", "message": blocked_detail, "reverted": ["n"]},
            "変換できませんでした。原本に戻しました(Face)。基準が違います",
        ),
        (
            {"state": "BLOCKED", "ts_name": "Face", "message": "", "reverted": ["n"]},
            "変換できませんでした。原本に戻しました(Face)。",
        ),
        (
            {"state": "BLOCKED", "ts_name": "Face", "reverted": ["n"]},
            "変換できませんでした。原本に戻しました(Face)。",
        ),
        (
            {"state": "BLOCKED", "ts_name": "Face", "message": blocked_detail, "reverted": []},
            "変換できませんでした(Face)。基準が違います",
        ),
        (
            {"state": "BLOCKED", "ts_name": "Face", "message": "", "reverted": []},
            "変換できませんでした(Face)。",
        ),
        (
            {"state": "BLOCKED", "ts_name": "Face", "message": blocked_detail},
            "変換できませんでした(Face)。基準が違います",
        ),
        (
            {"state": "BLOCKED", "ts_name": "Face"},
            "変換できませんでした(Face)。",
        ),
    ]
    for event, expected in cases:
        text = integ.completion_message(event)
        assert text == expected
        _assert_single_periods(text)


def test_fold_status_label_disabled_enabled_converting():
    assert integ.fold_status_label(False, False) == integ.LABEL_FOLD_OFF
    assert integ.fold_status_label(False, False) == "無効"
    assert integ.fold_status_label(False, True) == "無効"
    assert integ.fold_status_label(True, False) == integ.LABEL_FOLD_ON
    assert integ.fold_status_label(True, False) == "有効"
    assert integ.fold_status_label(True, True) == integ.LABEL_FOLD_BUSY
    assert integ.fold_status_label(True, True) == "変換中"


class _RecordingLabel:
    def __init__(self, text=""):
        self.text = text
        self.history = []

    def setText(self, text):
        self.text = text
        self.history.append(text)


def test_on_scene_changed_replaces_previous_message(tmp_path):
    ctrl = _make_controller(tmp_path)
    label = _RecordingLabel("変換を開始しました")
    handle = integ.UVRemapAttachment(None, ctrl, message_label=label)
    handle.on_scene_changed()
    assert label.history == [integ.MSG_SCENE_SWITCHED]
    assert label.text == "シーンが切り替わりました"


def test_on_scene_changed_orphan_overwrites_scene_message(tmp_path):
    ctrl = _make_controller(tmp_path)
    ctrl._runtime.recover_result = ["Trim"]
    label = _RecordingLabel("変換が完了しました")
    handle = integ.UVRemapAttachment(None, ctrl, message_label=label)
    handle.on_scene_changed()
    assert label.history[0] == integ.MSG_SCENE_SWITCHED
    assert label.text == integ.MSG_INTERRUPTED
    assert ctrl._history == [("uvremap", integ.MSG_INTERRUPTED)]


def test_on_scene_changed_status_failure_overwrites_scene_message(tmp_path):
    ctrl = _make_controller(tmp_path, project=None)
    label = _RecordingLabel("変換が完了しました")
    handle = integ.UVRemapAttachment(None, ctrl, message_label=label)
    handle.on_scene_changed()
    assert label.history[0] == "シーンが切り替わりました"
    assert label.text == integ.MSG_NO_PROJECT


# ---------------------------------------------------------------------------
# Qt (任意)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip("PySide6 を import できない: {0}".format(exc))
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_attach_timer_starts_and_shutdown_stops(qapp, tmp_path):
    from PySide6 import QtWidgets

    ctrl = _make_controller(tmp_path)
    win = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(win)
    handle = integ.attach(
        win, layout, controller=ctrl,
        confirm_fn=lambda _t: False, inform_fn=lambda _t: None,
    )
    try:
        qapp.processEvents()
        assert handle.timer_is_active() is True
        assert ctrl._runtime.recover_calls == [PROJECT]
        group = win.findChild(QtWidgets.QGroupBox, "uvremap_group")
        assert group is not None
        assert group.isChecked() is False
        handle.shutdown()
        qapp.processEvents()
        assert handle.timer_is_active() is False
    finally:
        handle.shutdown()
        win.close()


def test_attach_fold_button_toggles_detail_and_is_not_checkable(qapp, tmp_path):
    import json

    from PySide6 import QtWidgets

    ctrl = _make_controller(tmp_path, enabled=False)
    win = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(win)
    handle = integ.attach(
        win, layout, controller=ctrl,
        confirm_fn=lambda _t: False, inform_fn=lambda _t: None,
    )
    try:
        win.show()
        qapp.processEvents()
        group = win.findChild(QtWidgets.QGroupBox, "uvremap_group")
        detail = win.findChild(QtWidgets.QWidget, "uvremap_detail")
        button = win.findChild(QtWidgets.QPushButton, "btn_uvremap_fold")
        status = win.findChild(QtWidgets.QLabel, "uvremap_fold_status")
        kill = win.findChild(QtWidgets.QCheckBox, "kill_switch")
        assert group is not None and group.isCheckable() is False
        assert button is not None and button.isFlat() is True
        assert button.text() == integ.LABEL_FOLD_SHOW
        assert detail is not None and detail.isVisible() is False
        assert status is not None and status.text() == "無効"
        assert kill is not None

        button.click()
        qapp.processEvents()
        assert detail.isVisible() is True
        assert button.text() == integ.LABEL_FOLD_HIDE
        assert group.isCheckable() is False

        button.click()
        qapp.processEvents()
        assert detail.isVisible() is False
        assert button.text() == integ.LABEL_FOLD_SHOW
    finally:
        handle.shutdown()
        win.close()

    busy = _make_controller(tmp_path, enabled=True)
    payload = {
        "schema": 1,
        "ts": {
            "Body": {
                "state": "CONVERTING",
                "generation": 1,
                "updated": "2026-10-16T10:15:00",
                "message": "",
            },
        },
    }
    (busy._work / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )
    win2 = QtWidgets.QWidget()
    layout2 = QtWidgets.QVBoxLayout(win2)
    handle2 = integ.attach(
        win2, layout2, controller=busy,
        confirm_fn=lambda _t: False, inform_fn=lambda _t: None,
    )
    try:
        win2.show()
        qapp.processEvents()
        status2 = win2.findChild(QtWidgets.QLabel, "uvremap_fold_status")
        assert status2 is not None
        assert status2.text() == "変換中"
        (busy._work / "status.json").unlink()
        handle2.refresh_fold_status()
        qapp.processEvents()
        assert status2.text() == "有効"
    finally:
        handle2.shutdown()
        win2.close()


# ---------------------------------------------------------------------------
# P2-c1: 設定・follow_tick・折りたたみ・予約 ID
# ---------------------------------------------------------------------------
FOLLOW_NS = 1_000_000_000


def test_follow_settings_defaults_types_and_partial_save(tmp_path):
    ctrl = _make_controller(tmp_path, cfg={
        "uvremap_auto_follow": "yes",
        "uvremap_max_parallel": 99,
        "uvremap_follow_settle_s": 0.1,
    })
    settings = ctrl._follow_settings_from_config()
    assert settings["uvremap_auto_follow"] is False
    assert settings["uvremap_auto_follow_uv"] is False
    assert settings["uvremap_max_parallel"] == 4
    assert settings["uvremap_follow_settle_s"] == 0.5
    assert settings["uvremap_uv_settle_s"] == 3.0
    assert settings["uvremap_uv_poll_s"] == 2.0
    assert ctrl._auto_follow_from_config() is False

    first = ctrl.set_auto_follow(True)
    assert first["ok"] is True
    assert first["auto_follow"] is True
    assert first["message"] == follow.MSG_AUTO_INPUT_AS_IS
    assert ctrl._saved[-1] == {"uvremap_auto_follow": True}
    assert ctrl._config["uvremap_auto_follow"] is True
    assert ctrl._history == [("uvremap", follow.MSG_AUTO_INPUT_AS_IS)]

    second = ctrl.set_auto_follow(True)
    assert second["ok"] is True
    assert second["message"] == ""
    assert ctrl._history == [("uvremap", follow.MSG_AUTO_INPUT_AS_IS)]

    off = ctrl.set_auto_follow(False)
    assert off["ok"] is True
    assert off["auto_follow"] is False
    assert ctrl._saved[-1] == {"uvremap_auto_follow": False}


def test_convert_launch_messages_reservation_only():
    assert integ.convert_launch_messages(["job-1"], [], []) == "変換を開始しました"
    assert integ.convert_launch_messages([], ["Body"], []) == follow.MSG_PEND_FMT.format(ts="Body")
    assert integ.convert_launch_messages([], [], ["Trim"]) == follow.MSG_QUEUE_FMT.format(ts="Trim")
    mixed = integ.convert_launch_messages(["job-1"], ["Body"], ["Trim"])
    assert mixed.startswith("変換を開始しました")
    assert follow.MSG_PEND_FMT.format(ts="Body") in mixed
    assert follow.MSG_QUEUE_FMT.format(ts="Trim") in mixed
    assert integ.is_reservation_id("queued:1") is True
    assert integ.is_reservation_id("20261018-0001") is False


def test_convert_now_queued_id_uses_queue_message(tmp_path):
    ctrl = _make_controller(tmp_path)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True)
    (dest / "mesh.npz").write_bytes(b"x")
    ctrl._runtime.queue_ts = {"Body", "Trim"}
    result = ctrl.convert_now()
    assert result["ok"] is True
    assert "変換を開始しました" not in result["message"]
    assert follow.MSG_QUEUE_FMT.format(ts="Body") in result["message"]
    assert follow.MSG_QUEUE_FMT.format(ts="Trim") in result["message"]
    assert result["queued"] == ["Body", "Trim"]


def _touch_ns(path: Path, data: bytes, mtime_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _write_status_rows(root: Path, states: dict) -> None:
    table = {}
    for name, state in states.items():
        table[name] = {
            "state": state,
            "generation": 1,
            "updated": "2026-03-04T05:06:07",
            "message": "",
        }
    (root / "status.json").write_text(
        json.dumps({"schema": 1, "ts": table}, ensure_ascii=False), encoding="utf-8",
    )


def _prepare_follow_ctrl(
    tmp_path, *, enabled=True, auto_follow=True, states=None,
    recorded_sig=None, scene_id="scene.ma", runtime_scene="scene.ma",
    flag_mtime_ns=100, source_mtime_ns=200,
):
    ctrl = _make_controller(
        tmp_path, enabled=enabled, cfg={"uvremap_auto_follow": auto_follow},
    )
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "mesh.npz").write_bytes(b"x")
    resolved = ctrl._resolve_context()
    final = Path(resolved["final_dir"])
    final.mkdir(parents=True, exist_ok=True)
    if states is None:
        states = {"Body": "CONVERTED_OK"}
    _write_status_rows(ctrl._work, states)
    _touch_ns(final / "Body_BaseColor.png", b"abc", source_mtime_ns)
    recorded = recorded_sig
    if recorded is None:
        recorded = {"Body_BaseColor.png": [3, source_mtime_ns - 1]}
    follow.save_follow(ctrl._work, {
        "schema": 1,
        "flag_mtime_ns": 10,
        "ts": {
            "Body": {
                "source_sig": recorded,
                "last_job_id": "old-job",
                "scene_id": scene_id,
            },
        },
    })
    _touch_ns(final / "_sync_complete.flag", b"done", flag_mtime_ns)
    ctrl._runtime.scene_id = runtime_scene
    return ctrl, final


def _settled_tick(ctrl, now_ns, flag_mtime_ns):
    ctrl._follow_last_flag_mtime_ns = flag_mtime_ns
    ctrl._follow_last_change_seen_ns = now_ns - 3 * FOLLOW_NS
    return ctrl.follow_tick(now_ns)


def test_follow_tick_no_flag_does_nothing(tmp_path):
    ctrl, _final = _prepare_follow_ctrl(tmp_path)
    (_final / "_sync_complete.flag").unlink()
    result = ctrl.follow_tick(10 * FOLLOW_NS)
    assert result["ok"] is True
    assert result["message"] == ""
    assert ctrl._runtime.convert_calls == []
    assert ctrl._runtime.stale_calls == []
    loaded = follow.load_follow(ctrl._work)
    assert loaded["flag_mtime_ns"] == 10


def test_follow_tick_before_settle_does_nothing(tmp_path):
    ctrl, _final = _prepare_follow_ctrl(tmp_path, flag_mtime_ns=50)
    result = ctrl.follow_tick(10 * FOLLOW_NS)
    assert result["ok"] is True
    assert ctrl._runtime.convert_calls == []
    assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 10


def test_follow_tick_settled_converted_ok_starts_auto(tmp_path):
    ctrl, _final = _prepare_follow_ctrl(tmp_path, flag_mtime_ns=50)
    result = _settled_tick(ctrl, 10 * FOLLOW_NS, 50)
    assert result["ok"] is True
    assert result["message"] == follow.MSG_PAINTER_START_FMT.format(ts="Body")
    assert [c["origin"] for c in ctrl._runtime.convert_calls] == ["auto"]
    assert [c["ts_name"] for c in ctrl._runtime.convert_calls] == ["Body"]
    assert ctrl._history == [("uvremap", follow.MSG_PAINTER_START_FMT.format(ts="Body"))]
    assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 50


def test_follow_tick_original_and_failed_do_not_convert(tmp_path):
    for state in ("ORIGINAL", "FAILED"):
        ctrl, _final = _prepare_follow_ctrl(tmp_path, states={"Body": state}, flag_mtime_ns=50)
        result = _settled_tick(ctrl, 10 * FOLLOW_NS, 50)
        assert result["ok"] is True
        assert ctrl._runtime.convert_calls == []
        assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 50


def test_follow_tick_converting_is_pending(tmp_path):
    ctrl, _final = _prepare_follow_ctrl(tmp_path, states={"Body": "CONVERTING"}, flag_mtime_ns=50)
    ctrl._runtime.already_running = {"Body"}
    result = _settled_tick(ctrl, 10 * FOLLOW_NS, 50)
    assert result["ok"] is True
    assert result["message"] == follow.MSG_PEND_FMT.format(ts="Body")
    assert [c["origin"] for c in ctrl._runtime.convert_calls] == ["auto"]
    assert ctrl._history == [("uvremap", follow.MSG_PEND_FMT.format(ts="Body"))]


def test_follow_tick_other_scene_does_not_convert(tmp_path):
    ctrl, _final = _prepare_follow_ctrl(
        tmp_path, flag_mtime_ns=50, scene_id="scene.ma", runtime_scene="other.ma",
    )
    result = _settled_tick(ctrl, 10 * FOLLOW_NS, 50)
    assert result["ok"] is True
    assert ctrl._runtime.convert_calls == []
    assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 50


def test_follow_tick_auto_off_marks_stale(tmp_path):
    ctrl, _final = _prepare_follow_ctrl(tmp_path, auto_follow=False, flag_mtime_ns=50)
    result = ctrl.follow_tick(10 * FOLLOW_NS)
    assert result["ok"] is True
    assert ctrl._runtime.convert_calls == []
    assert ctrl._runtime.stale_calls == [(PROJECT, "Body", follow.MSG_INPUT_STALE)]
    assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 10


def test_follow_tick_killswitch_off_does_nothing(tmp_path):
    ctrl, _final = _prepare_follow_ctrl(tmp_path, enabled=False, flag_mtime_ns=50)
    result = ctrl.follow_tick(10 * FOLLOW_NS)
    assert result["ok"] is True
    assert ctrl._runtime.convert_calls == []
    assert ctrl._runtime.stale_calls == []
    assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 10


def test_follow_tick_q4_flag_mtime_rules(tmp_path):
    ready = _prepare_follow_ctrl(tmp_path, flag_mtime_ns=50)[0]
    _settled_tick(ready, 10 * FOLLOW_NS, 50)
    assert follow.load_follow(ready._work)["flag_mtime_ns"] == 50

    settling = _prepare_follow_ctrl(tmp_path, flag_mtime_ns=77)[0]
    settling.follow_tick(10 * FOLLOW_NS)
    assert follow.load_follow(settling._work)["flag_mtime_ns"] == 10

    no_base = _prepare_follow_ctrl(tmp_path, flag_mtime_ns=88)[0]
    import shutil
    shutil.rmtree(no_base._work / "baseline")
    _settled_tick(no_base, 10 * FOLLOW_NS, 88)
    assert follow.load_follow(no_base._work)["flag_mtime_ns"] == 10
    assert no_base._runtime.convert_calls == []


def test_current_fold_status_p2_needs_apply_and_auto(tmp_path):
    stale = _make_controller(tmp_path, enabled=True)
    _write_status_rows(stale._work, {"Body": "STALE"})
    assert integ._current_fold_status(stale) == "要反映"

    auto = _make_controller(tmp_path, enabled=True, cfg={"uvremap_auto_follow": True})
    _write_status_rows(auto._work, {"Body": "CONVERTED_OK"})
    assert integ._current_fold_status(auto) == "自動追従"

    off = _make_controller(tmp_path, enabled=False, cfg={"uvremap_auto_follow": True})
    _write_status_rows(off._work, {"Body": "STALE"})
    assert integ._current_fold_status(off) == "無効"


def test_poll_tick_calls_follow_tick_after_poll(tmp_path):
    ctrl = _make_controller(tmp_path)
    seen = []

    class Handle:
        def set_message(self, text):
            seen.append(text)

        def _refresh_panel(self):
            seen.append("refresh")

    ctrl._runtime.poll_events = [
        {"state": "CONVERTED_OK", "ts_name": "Body", "exit_code": 0},
    ]
    result = integ.run_poll_tick(ctrl, Handle(), now_ns=1)
    assert result["message"] == _done("Body")
    assert "refresh" in seen
    assert ctrl._runtime.convert_calls == []


def _arm_follow_counts(ctrl, monkeypatch):
    """``list_texture_sets_fn`` と ``source_signature`` の呼び出し回数を数える。"""
    counts = {"list": 0, "sig": 0}
    orig_list = ctrl.list_texture_sets_fn

    def list_fn():
        counts["list"] += 1
        return orig_list()

    ctrl.list_texture_sets_fn = list_fn
    real_sig = integ.source_signature

    def sig_fn(*args, **kwargs):
        counts["sig"] += 1
        return real_sig(*args, **kwargs)

    monkeypatch.setattr(integ, "source_signature", sig_fn)
    return counts


def _match_flag_to_record(ctrl, final):
    """印の mtime を follow.json の記録に揃え、未処理の書き出しが無い状態にする。"""
    recorded = follow.load_follow(ctrl._work)["flag_mtime_ns"]
    _touch_ns(final / "_sync_complete.flag", b"done", recorded)
    return recorded


def test_follow_tick_unchanged_flag_scans_once(tmp_path, monkeypatch):
    ctrl, final = _prepare_follow_ctrl(tmp_path, flag_mtime_ns=50)
    _match_flag_to_record(ctrl, final)
    counts = _arm_follow_counts(ctrl, monkeypatch)
    start = 30 * FOLLOW_NS
    for step in range(10):
        result = ctrl.follow_tick(start + step * FOLLOW_NS)
        assert result["ok"] is True
        assert result["message"] == ""
    assert counts == {"list": 1, "sig": 1}
    assert ctrl._runtime.convert_calls == []
    assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 10


def test_follow_tick_flag_update_runs_heavy_then_once_after_settle(tmp_path, monkeypatch):
    ctrl, final = _prepare_follow_ctrl(tmp_path, flag_mtime_ns=50)
    _match_flag_to_record(ctrl, final)
    counts = _arm_follow_counts(ctrl, monkeypatch)
    start = 30 * FOLLOW_NS
    ctrl.follow_tick(start)
    for step in range(1, 4):
        ctrl.follow_tick(start + step * FOLLOW_NS)
    assert counts == {"list": 1, "sig": 1}
    assert ctrl._runtime.convert_calls == []

    _touch_ns(final / "_sync_complete.flag", b"new", 80)
    changed_at = start + 4 * FOLLOW_NS
    before = ctrl.follow_tick(changed_at)
    assert before["ok"] is True
    assert before["message"] == ""
    assert counts == {"list": 2, "sig": 2}
    assert ctrl._runtime.convert_calls == []
    assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 10

    still = ctrl.follow_tick(changed_at + FOLLOW_NS)
    assert still["message"] == ""
    assert counts == {"list": 2, "sig": 2}
    assert ctrl._runtime.convert_calls == []

    settled = ctrl.follow_tick(changed_at + 2 * FOLLOW_NS)
    assert settled["ok"] is True
    assert settled["message"] == follow.MSG_PAINTER_START_FMT.format(ts="Body")
    assert counts == {"list": 3, "sig": 3}
    assert [c["origin"] for c in ctrl._runtime.convert_calls] == ["auto"]
    assert [c["ts_name"] for c in ctrl._runtime.convert_calls] == ["Body"]
    assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 80

    ctrl.follow_tick(changed_at + 3 * FOLLOW_NS)
    ctrl.follow_tick(changed_at + 4 * FOLLOW_NS)
    assert counts == {"list": 3, "sig": 3}
    assert len(ctrl._runtime.convert_calls) == 1


def test_follow_tick_toggle_and_killswitch_run_heavy_once(tmp_path, monkeypatch):
    ctrl, final = _prepare_follow_ctrl(tmp_path, flag_mtime_ns=50)
    _match_flag_to_record(ctrl, final)
    counts = _arm_follow_counts(ctrl, monkeypatch)
    start = 40 * FOLLOW_NS
    for step in range(3):
        ctrl.follow_tick(start + step * FOLLOW_NS)
    assert counts == {"list": 1, "sig": 1}

    same = ctrl.set_auto_follow(True)
    assert same["ok"] is True
    ctrl.follow_tick(start + 3 * FOLLOW_NS)
    assert counts == {"list": 1, "sig": 1}

    off = ctrl.set_auto_follow(False)
    assert off["ok"] is True
    assert off["auto_follow"] is False
    toggled = ctrl.follow_tick(start + 4 * FOLLOW_NS)
    assert toggled["ok"] is True
    assert counts == {"list": 2, "sig": 2}
    assert ctrl._runtime.stale_calls == [(PROJECT, "Body", follow.MSG_INPUT_STALE)]
    ctrl.follow_tick(start + 5 * FOLLOW_NS)
    assert counts == {"list": 2, "sig": 2}
    assert ctrl._runtime.stale_calls == [(PROJECT, "Body", follow.MSG_INPUT_STALE)]

    disabled = ctrl.set_enabled(False)
    assert disabled["ok"] is True
    ctrl.follow_tick(start + 6 * FOLLOW_NS)
    assert counts == {"list": 2, "sig": 2}

    enabled = ctrl.set_enabled(True)
    assert enabled["ok"] is True
    ctrl.follow_tick(start + 7 * FOLLOW_NS)
    assert counts == {"list": 3, "sig": 3}
    ctrl.follow_tick(start + 8 * FOLLOW_NS)
    assert counts == {"list": 3, "sig": 3}
    assert len(ctrl._runtime.stale_calls) == 2


def test_follow_tick_auto_off_stale_only_on_heavy_pass(tmp_path, monkeypatch):
    ctrl, final = _prepare_follow_ctrl(tmp_path, auto_follow=False, flag_mtime_ns=50)
    counts = _arm_follow_counts(ctrl, monkeypatch)
    start = 10 * FOLLOW_NS
    for step in range(10):
        result = ctrl.follow_tick(start + step * FOLLOW_NS)
        assert result["ok"] is True
        assert result["message"] == ""
    assert counts == {"list": 1, "sig": 1}
    assert ctrl._runtime.convert_calls == []
    assert ctrl._runtime.stale_calls == [(PROJECT, "Body", follow.MSG_INPUT_STALE)]
    assert follow.load_follow(ctrl._work)["flag_mtime_ns"] == 10


# ---------------------------------------------------------------------------
# P2-c2: UV 追従
# ---------------------------------------------------------------------------
def _fp_box(initial, elapsed=0.01):
    box = {"fp": dict(initial), "elapsed": elapsed, "n": 0, "paths": None}

    def fake(shape_paths, uv_set=None):
        box["n"] += 1
        box["paths"] = list(shape_paths)
        return (dict(box["fp"]), box["elapsed"])

    return box, fake


def _prepare_uv_ctrl(
    tmp_path, *, enabled=True, auto_uv=False, states=None, recorded=None,
    fp=None, elapsed=0.01, settle=3.0, poll=2.0,
):
    box, fake = _fp_box(fp or {"|meshA": (2, 4)}, elapsed)
    ctrl = _make_controller(
        tmp_path,
        enabled=enabled,
        cfg={
            "uvremap_auto_follow_uv": auto_uv,
            "uvremap_uv_settle_s": settle,
            "uvremap_uv_poll_s": poll,
        },
        uv_fingerprints_fn=fake,
    )
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "mesh.npz").write_bytes(b"x")
    if states is None:
        states = {"Body": "CONVERTED_OK"}
    _write_status_rows(ctrl._work, states)
    ctrl.refresh_shape_map()
    if recorded is None:
        recorded = {"Body": {"uv_fp": {"|meshA": [1, 4]}}}
    follow.save_follow(ctrl._work, {
        "schema": 1,
        "flag_mtime_ns": None,
        "ts": recorded,
    })
    ctrl._fp_box = box
    return ctrl


def test_set_auto_follow_uv_partial_merge(tmp_path):
    ctrl = _make_controller(tmp_path)
    result = ctrl.set_auto_follow_uv(True)
    assert result["ok"] is True
    assert result["auto_follow_uv"] is True
    assert result["message"] == ""
    assert ctrl._saved[-1] == {"uvremap_auto_follow_uv": True}
    assert ctrl._config["uvremap_auto_follow_uv"] is True
    assert ctrl._auto_follow_uv_from_config() is True
    off = ctrl.set_auto_follow_uv(False)
    assert off["ok"] is True
    assert off["auto_follow_uv"] is False
    assert ctrl._saved[-1] == {"uvremap_auto_follow_uv": False}


def test_current_fold_status_uv_follow_is_auto(tmp_path):
    ctrl = _make_controller(tmp_path, enabled=True, cfg={"uvremap_auto_follow_uv": True})
    _write_status_rows(ctrl._work, {"Body": "CONVERTED_OK"})
    assert integ._current_fold_status(ctrl) == "自動追従"


def test_uv_tick_before_interval_does_nothing(tmp_path):
    ctrl = _prepare_uv_ctrl(tmp_path)
    assert ctrl.uv_tick(0)["message"] == ""
    assert ctrl.uv_tick(1)["message"] == ""
    assert ctrl._fp_box["n"] == 0
    assert ctrl._runtime.stale_calls == []
    ctrl.uv_tick(2)
    assert ctrl._fp_box["n"] == 1


def test_uv_tick_skips_fingerprint_without_watch_states(tmp_path):
    ctrl = _prepare_uv_ctrl(
        tmp_path, states={"Body": "ORIGINAL", "Trim": "FAILED"},
    )
    ctrl.uv_tick(0)
    result = ctrl.uv_tick(2)
    assert result["ok"] is True
    assert ctrl._fp_box["n"] == 0
    assert ctrl._runtime.stale_calls == []


def test_uv_tick_change_marks_stale(tmp_path):
    ctrl = _prepare_uv_ctrl(tmp_path, auto_uv=False)
    ctrl.uv_tick(0)
    result = ctrl.uv_tick(2)
    assert result["ok"] is True
    assert result["message"] == ""
    assert ctrl._runtime.convert_calls == []
    assert ctrl._runtime.stale_calls == [(PROJECT, "Body", follow.MSG_UV_STALE)]
    assert ctrl._fp_box["paths"] == ["|meshA"]


def test_uv_tick_before_settle_does_not_convert(tmp_path):
    ctrl = _prepare_uv_ctrl(tmp_path, auto_uv=True, settle=2.0, poll=2.0)
    ctrl.uv_tick(0)
    result = ctrl.uv_tick(2)
    assert result["ok"] is True
    assert ctrl._runtime.convert_calls == []
    assert ctrl._runtime.stale_calls == [(PROJECT, "Body", follow.MSG_UV_STALE)]
    assert follow.MSG_UV_START_FMT.format(ts="Body") not in result["message"]


def test_uv_tick_after_settle_starts_auto(tmp_path):
    ctrl = _prepare_uv_ctrl(tmp_path, auto_uv=True, settle=2.0, poll=2.0)
    ctrl.uv_tick(0)
    ctrl.uv_tick(2)
    result = ctrl.uv_tick(4)
    text = follow.MSG_UV_START_FMT.format(ts="Body")
    assert result["ok"] is True
    assert result["message"] == text
    assert [c["origin"] for c in ctrl._runtime.convert_calls] == ["auto"]
    assert [c["ts_name"] for c in ctrl._runtime.convert_calls] == ["Body"]
    assert ("uvremap", text) in ctrl._history


def test_uv_tick_killswitch_off_does_nothing(tmp_path):
    ctrl = _prepare_uv_ctrl(tmp_path, enabled=False, auto_uv=True)
    ctrl.uv_tick(0)
    result = ctrl.uv_tick(100)
    assert result["ok"] is True
    assert result["message"] == ""
    assert ctrl._fp_box["n"] == 0
    assert ctrl._runtime.stale_calls == []
    assert ctrl._runtime.convert_calls == []


def test_uv_tick_slow_fingerprint_widens_interval_until_scene_change(tmp_path):
    ctrl = _prepare_uv_ctrl(tmp_path, elapsed=0.25, poll=2.0)
    assert ctrl.uv_tick(0)["message"] == ""
    assert ctrl._fp_box["n"] == 0
    first = ctrl.uv_tick(2)
    assert ctrl._fp_box["n"] == 1
    assert first["message"] == follow.MSG_UV_SLOW
    assert ctrl._history == [("uvremap", follow.MSG_UV_SLOW)]
    assert ctrl.uv_tick(11)["message"] == ""
    assert ctrl._fp_box["n"] == 1
    assert ctrl.uv_tick(12)["ok"] is True
    assert ctrl._fp_box["n"] == 2
    assert [row[1] for row in ctrl._history].count(follow.MSG_UV_SLOW) == 1
    ctrl._fp_box["elapsed"] = 0.01
    ctrl.note_scene_changed()
    assert ctrl._uv_slow is False
    ctrl.uv_tick(13)
    assert ctrl._fp_box["n"] == 2
    ctrl.uv_tick(14)
    assert ctrl._fp_box["n"] == 3
    assert [row[1] for row in ctrl._history].count(follow.MSG_UV_SLOW) == 1


def test_uv_tick_does_not_scan_scene_each_tick(tmp_path, monkeypatch):
    ctrl = _prepare_uv_ctrl(tmp_path)
    counts = _arm_follow_counts(ctrl, monkeypatch)
    ctrl.uv_tick(0)
    for step in range(1, 6):
        ctrl.uv_tick(step * 2)
    assert counts["list"] == 0
    assert ctrl._fp_box["n"] == 5


def test_apply_changes_converts_only_stale(tmp_path):
    ctrl = _make_controller(tmp_path, enabled=True)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "mesh.npz").write_bytes(b"x")
    _write_status_rows(ctrl._work, {"Body": "STALE", "Trim": "CONVERTED_OK"})
    result = ctrl.apply_changes()
    assert result["ok"] is True
    assert [c["ts_name"] for c in ctrl._runtime.convert_calls] == ["Body"]
    assert [c["origin"] for c in ctrl._runtime.convert_calls] == ["manual"]
    assert "変換を開始しました" in result["message"]
    assert follow.apply_button_visible({"Body": "STALE", "Trim": "CONVERTED_OK"}) is True

    ctrl._runtime.already_running = {"Body"}
    ctrl._runtime.convert_calls.clear()
    pending = ctrl.apply_changes()
    assert pending["ok"] is True
    assert follow.MSG_PEND_FMT.format(ts="Body") in pending["message"]
    assert [c["ts_name"] for c in ctrl._runtime.convert_calls] == ["Body"]

    quiet = _make_controller(tmp_path / "other", enabled=True)
    qdest = quiet._work / "baseline" / "bl-old"
    qdest.mkdir(parents=True, exist_ok=True)
    (qdest / "mesh.npz").write_bytes(b"x")
    _write_status_rows(quiet._work, {"Body": "CONVERTED_OK", "Trim": "ORIGINAL"})
    none = quiet.apply_changes()
    assert none["ok"] is False
    assert quiet._runtime.convert_calls == []
    assert follow.apply_button_visible({"Body": "CONVERTED_OK", "Trim": "ORIGINAL"}) is False


def test_follow_tick_no_targets_heavy_once(tmp_path, monkeypatch):
    ctrl = _make_controller(
        tmp_path, enabled=True, known=[], cfg={"uvremap_auto_follow": True},
    )
    final = Path(tmp_path) / "final" / SUBFOLDER
    _touch_ns(final / "_sync_complete.flag", b"done", 50)
    counts = _arm_follow_counts(ctrl, monkeypatch)
    start = 10 * FOLLOW_NS
    for step in range(6):
        result = ctrl.follow_tick(start + step * FOLLOW_NS)
        assert result["ok"] is True
        assert result["message"] == ""
    assert counts["list"] == 1
    ctrl.note_scene_changed()
    ctrl.follow_tick(start + 6 * FOLLOW_NS)
    assert counts["list"] > 1


def test_poll_tick_calls_uv_tick_and_swallows_errors(tmp_path):
    ctrl = _make_controller(tmp_path)
    seen = []

    def uv(now_s):
        seen.append(now_s)
        return {"ok": True, "message": "uv-msg"}

    ctrl.uv_tick = uv

    class Handle:
        def __init__(self):
            self.texts = []

        def set_message(self, text):
            self.texts.append(text)

        def _refresh_panel(self):
            self.texts.append("refresh")

    handle = Handle()
    integ.run_poll_tick(ctrl, handle, now_ns=4 * FOLLOW_NS)
    assert seen == [4.0]
    assert "uv-msg" in handle.texts

    def boom(_now_s):
        raise RuntimeError("uv")

    ctrl.uv_tick = boom
    integ.run_poll_tick(ctrl, handle, now_ns=5 * FOLLOW_NS)


def test_apply_button_widget_follows_stale(qapp, tmp_path):
    from PySide6 import QtWidgets

    from uvremap_panel import COLOR_ATTENTION

    ctrl = _make_controller(tmp_path, enabled=True)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "mesh.npz").write_bytes(b"x")
    _write_status_rows(ctrl._work, {"Body": "STALE", "Trim": "CONVERTED_OK"})
    win = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(win)
    handle = integ.attach(
        win, layout, controller=ctrl,
        confirm_fn=lambda _t: False, inform_fn=lambda _t: None,
    )
    try:
        win.show()
        qapp.processEvents()
        detail = win.findChild(QtWidgets.QWidget, "uvremap_detail")
        button = win.findChild(QtWidgets.QPushButton, "btn_apply_changes")
        convert = win.findChild(QtWidgets.QPushButton, "btn_convert")
        assert detail is not None and detail.isVisible() is False
        assert button is not None
        assert button.text() == follow.LABEL_APPLY
        assert button.isVisible() is True
        assert COLOR_ATTENTION in button.styleSheet()
        assert convert is not None
        button.click()
        qapp.processEvents()
        assert [c["ts_name"] for c in ctrl._runtime.convert_calls] == ["Body"]
        assert [c["origin"] for c in ctrl._runtime.convert_calls] == ["manual"]
        _write_status_rows(ctrl._work, {"Body": "CONVERTED_OK", "Trim": "ORIGINAL"})
        handle.refresh_fold_status()
        qapp.processEvents()
        assert button.isVisible() is False
    finally:
        handle.shutdown()
        win.close()
