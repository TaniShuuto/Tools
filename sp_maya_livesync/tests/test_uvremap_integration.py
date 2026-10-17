# -*- coding: utf-8 -*-
"""uvremap_integration.py のテスト(Maya 無し・Pillow 無し)。

Qt は PySide6 があるときだけ QT_QPA_PLATFORM=offscreen で実行する。
時刻には依存しない。
"""
from __future__ import annotations

import ast
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

import uvremap_integration as integ  # noqa: E402


FIXED_NOW = "2026-03-04T05:06:07"
PROJECT = "proj_a"
SUBFOLDER = "Table_445ba2"
ROOT_DIR = "C:/SPMayaLiveSync/uvremap"
FINAL_ROOT = "C:/SPMayaLiveSync/final"
WATCH_ROOT = "C:/SPMayaLiveSync/live"


def test_import_does_not_load_maya_qt_or_pillow():
    banned = []
    for name in sys.modules:
        if name == "maya" or name.startswith("maya."):
            banned.append(name)
        if name in ("PySide6", "PySide2", "PIL") or name.startswith(
            ("PySide6.", "PySide2.", "PIL.")
        ):
            banned.append(name)
    assert banned == []


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
        })
        if self.empty_job:
            return ""
        if ts_name in self.already_running:
            return ""
        return "job-{0}".format(ts_name)

    def has_running_job_for_ts(self, ts_name):
        return ts_name in self.already_running

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
                     runtime=None, history=None, cfg=None, raise_project=False):
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
        ctrl.record_baseline(),
        ctrl.convert_now(),
        ctrl.revert(),
        ctrl.poll(),
        ctrl.status_path(),
        ctrl.recover_orphaned(),
    ]
    for result in results[:-1]:
        assert isinstance(result, dict)
        assert "ok" in result
        assert "message" in result
    assert results[-1] == []
    assert ctrl.has_baseline() is False


def test_convert_now_already_running_returns_japanese_message(tmp_path):
    ctrl = _make_controller(tmp_path)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True)
    (dest / "mesh.npz").write_bytes(b"x")
    ctrl._runtime.already_running = {"Body"}
    result = ctrl.convert_now()
    assert result["ok"] is True
    assert integ.MSG_ALREADY_RUNNING in result["message"]
    names = [call["ts_name"] for call in ctrl._runtime.convert_calls]
    assert names == ["Body", "Trim"]
    assert result["job_ids"] == ["job-Trim"]
    assert result["already_running"] == ["Body"]


def test_convert_now_all_already_running_is_not_started_message(tmp_path):
    ctrl = _make_controller(tmp_path)
    dest = ctrl._work / "baseline" / "bl-old"
    dest.mkdir(parents=True)
    (dest / "mesh.npz").write_bytes(b"x")
    ctrl._runtime.already_running = {"Body", "Trim"}
    result = ctrl.convert_now()
    assert result["ok"] is False
    assert result["message"] == integ.MSG_ALREADY_RUNNING
    assert result["message"] != integ.MSG_NOT_STARTED
    assert result["job_ids"] == []


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
    assert version == "1.7.0"
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
    ]
    for name in expected:
        assert name in install_src
        assert name in uninstall_src


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
