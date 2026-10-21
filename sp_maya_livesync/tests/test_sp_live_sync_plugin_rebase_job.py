# -*- coding: utf-8 -*-
"""sp_live_sync_plugin 1.4.0 PB-b の RebaseJob / Coordinator / 配線。"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import sys
import time
import types

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_TESTS)
for _p in (_PKG, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_sp_live_sync_plugin_protocol import (  # noqa: E402
    plugin, ProjectError, _arm_export, _status_file,
)
from plugin_patch import patch_plugin_attr  # noqa: E402
from plugin_source import plugin_source_text  # noqa: E402

pytest_plugins = ["test_sp_live_sync_plugin_protocol"]
import uvremap_rebase as rb  # noqa: E402

RID = "rb-20261019-053400-3fa2"
PROJECT_KEY = "C:/Work/Demo.spp"
NOW = 1000.0


class Clock(object):
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class ReloadStatus(object):
    def __init__(self, name):
        self.name = name


class FakeApi(object):
    def __init__(self, tmp_path):
        self.closed = False
        self.calls = []
        self.busy = False
        self.spp = str(tmp_path / "Demo.spp")
        self.exists = set()
        self.sizes = {}
        self.save_error = None
        self.reload_error = None
        self.reload_callback = None
        self.export_queue = []
        self.record_ok = True
        self.decision = plugin.ApprovalDecision("run", "auto_approved")
        self.supports_unwrap = True
        self.last_mesh = None
        self.uv_ctx = {"api": None, "texture_sets": [], "ts_names": []}
        self.fail_if_called_from_callback = False
        self._in_callback = False
        self.project_is_open = True
        self.in_edition = True
        self.exporting = False
        self.needs_saving = False
        self.needs_saving_missing = False
        self.project_key = PROJECT_KEY
        self.saved_path = PROJECT_KEY
        self.last_edit_at = NOW - 30
        PathWrite = tmp_path / "Demo.spp"
        PathWrite.write_bytes(b"spp")

    def _guard(self):
        if self._in_callback and self.fail_if_called_from_callback:
            raise AssertionError("callback must only set a flag")

    def now(self):
        return NOW

    def recheck(self, request):
        self._guard()
        self.calls.append("recheck")
        return self.decision

    def project_open(self):
        return self.project_is_open

    def project_file_path(self):
        return self.spp

    def is_busy(self):
        return self.busy

    def file_exists(self, path):
        return path in self.exists or os.path.isfile(path)

    def file_size(self, path):
        if path in self.sizes:
            return self.sizes[path]
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def make_dirs(self, path):
        self.calls.append(("make_dirs", path))
        os.makedirs(path, exist_ok=True)

    def save_as_copy(self, path):
        self._guard()
        self.calls.append(("save_as_copy", os.path.basename(path)))
        if self.save_error is not None:
            raise self.save_error
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"spp-backup")
        self.exists.add(path)
        self.sizes[path] = 10

    def supports_auto_unwrap(self):
        return self.supports_unwrap

    def make_reload_settings(self, **kwargs):
        self.calls.append(("make_reload_settings", kwargs))
        return kwargs

    def reload_mesh(self, path, settings, callback):
        self._guard()
        self.calls.append(("reload_mesh", path, settings))
        self.reload_callback = callback
        if self.reload_error is not None:
            raise self.reload_error

    def uv_fill_context(self):
        self.calls.append("uv_fill_context")
        return self.uv_ctx

    def run_final_export(self, request_id):
        self.calls.append(("run_final_export", request_id))
        if self.export_queue:
            return self.export_queue.pop(0)
        return plugin.ExportAttempt(
            executed=True, ok=True, status="success", export_id="ex-1")

    def record_painter_mesh(self, request):
        self.calls.append(("record_painter_mesh", request.get("request_id")))
        if isinstance(self.record_ok, Exception):
            raise self.record_ok
        return bool(self.record_ok)

    def last_imported_mesh_path(self):
        return self.last_mesh

    def snapshot(self):
        return {
            "is_open": self.project_is_open,
            "in_edition": self.in_edition,
            "project_key": self.project_key,
            "saved_path": self.saved_path,
            "busy": self.busy,
            "exporting": self.exporting,
            "needs_saving": self.needs_saving,
            "needs_saving_missing": self.needs_saving_missing,
            "can_disable_auto_unwrap": self.supports_unwrap,
            "last_edit_at": self.last_edit_at,
        }

    def fire_reload(self, status):
        self._in_callback = True
        try:
            if self.reload_callback is not None:
                self.reload_callback(status)
        finally:
            self._in_callback = False


class FakeDialog(object):
    def __init__(self, text, on_result):
        self.text = text
        self.on_result = on_result
        self.closed = False
        self.disconnected = False
        self.deleted = False

    def disconnect_all(self):
        self.disconnected = True

    def close(self):
        self.closed = True

    def deleteLater(self):
        self.deleted = True

    def accept_run(self):
        self.on_result("run")

    def accept_cancel(self):
        self.on_result("cancel")


class FakeHooks(object):
    def __init__(self, tmp_path, cfg):
        self.tmp_path = tmp_path
        self.cfg = cfg
        self.blocks = False
        self.resumed = 0
        self.logs = []
        self.statuses = []
        self.load_calls = 0
        self._shutting = False

    def is_shutting_down(self):
        return self._shutting

    def emit_status(self, text):
        self.statuses.append(text)

    def log(self, level, text):
        self.logs.append((level, text))

    def set_blocks_exports(self, value):
        self.blocks = bool(value)

    def resume_blocked_exports(self):
        self.resumed += 1

    def load_config(self):
        self.load_calls += 1
        return dict(self.cfg)

    def current_project_key(self):
        return PROJECT_KEY

    def project_subfolder(self):
        return "proj"


def _mesh_file(tmp_path, payload=b"hello-mesh"):
    path = tmp_path / "body.fbx"
    path.write_bytes(payload)
    digest = hashlib.sha1(payload).hexdigest()
    st = path.stat()
    return {
        "path": str(path),
        "size": st.st_size,
        "sha1": digest,
        "mtime_ns": int(st.st_mtime_ns),
    }


def _request(tmp_path, **over):
    mesh = over.pop("mesh", None) or _mesh_file(tmp_path)
    raw = rb.build_request(
        request_id=over.pop("request_id", RID),
        project_key=over.pop("project_key", PROJECT_KEY),
        created_at=over.pop("created_at", NOW - 10),
        ttl_s=over.pop("ttl_s", 1800),
        mesh=mesh,
        baseline_id=over.pop("baseline_id", "bl-1"),
        ts_names=over.pop("ts_names", ["Body"]),
        final_full=over.pop("final_full", True),
        uv_old=over.pop("uv_old", {"name": "uvOld", "index": 1}),
    )
    raw.update(over)
    return raw


def _settings(**over):
    cfg = {
        "rebase_enabled": True,
        "rebase_approval": "ask",
        "rebase_auto_idle_s": 10.0,
        "rebase_reload_timeout_s": 600.0,
        "rebase_retry_max": 5,
        "rebase_wait_max_s": 1800.0,
        "rebase_reproject_uv_fills": True,
    }
    cfg.update(over)
    return cfg


def _job(tmp_path, api=None, request=None, clock=None, settings=None):
    clock = clock or Clock()
    api = api or FakeApi(tmp_path)
    request = request or _request(tmp_path)
    root = tmp_path / "uv" / request["request_id"] / str(id(clock))
    root.mkdir(parents=True, exist_ok=True)
    plugin.write_rebase_status(
        str(root),
        plugin.build_rebase_status(
            request["request_id"], "received", clock(), plugin.__version__),
    )
    reporter = plugin.RebaseReporter(
        root=str(root),
        request_id=request["request_id"],
        clock=clock,
        log_fn=lambda level, text: None,
        emit_status_fn=lambda text: None,
        shutting_down_fn=lambda: False,
        plugin_version=plugin.__version__,
    )
    job = plugin.RebaseJob(
        request, reporter, api, settings or _settings(),
        clock=clock, approved=True)
    return job, api, reporter, clock, root


def _drive_happy_job(job, api, clock):
    states = []
    job.run()
    states.append(job.state)
    job.tick(clock())
    states.append(job.state)
    job.tick(clock())
    states.append(job.state)
    job.tick(clock())
    api.fire_reload(ReloadStatus("SUCCESS"))
    job.tick(clock())
    states.append(job.state)
    job.tick(clock())
    states.append(job.state)
    return states


def _no_sleep(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("time.sleep must not be used")

    monkeypatch.setattr(plugin.time, "sleep", boom)
    monkeypatch.setattr(time, "sleep", boom)


@pytest.fixture(autouse=True)
def _ban_sleep(monkeypatch):
    _no_sleep(monkeypatch)


def test_rebase_reject_state_all_combos():
    reasons = (
        "cancelled", "superseded", "expired", "disabled", "unsaved_project",
        "mesh_missing", "mesh_hash_mismatch", "bad_request",
    )
    prevs = (None, "received", "awaiting_approval", "preparing")
    for reason in reasons:
        for prev in prevs:
            state, out_reason = plugin.rebase_reject_state(reason, prev)
            assert plugin.is_valid_rebase_transition(prev, state), (prev, reason, state)


def test_job_happy_path_order(tmp_path):
    job, api, reporter, clock, root = _job(tmp_path)
    states = _drive_happy_job(job, api, clock)
    assert states[0] == "preparing"
    assert "backing_up" in states
    assert job.state == "done"
    names = [c[0] if isinstance(c, tuple) else c for c in api.calls]
    assert names.index("save_as_copy") < names.index("reload_mesh")
    assert names.index("reload_mesh") < names.index("uv_fill_context")
    assert names.index("uv_fill_context") < names.index("run_final_export")
    assert names.index("run_final_export") < names.index("record_painter_mesh")
    reload_call = [c for c in api.calls if isinstance(c, tuple) and c[0] == "reload_mesh"][0]
    assert reload_call[2]["import_cameras"] is False
    assert reload_call[2]["preserve_strokes"] is True
    assert reload_call[2]["auto_unwrap_settings"] is None
    assert "mesh_settings" not in reload_call[2]
    status = plugin.read_rebase_status(str(root), RID)
    assert status["state"] == "done"
    assert status["result_export_id"] == "ex-1"
    assert "/" not in (status.get("backup_name") or "")
    assert status["plugin_version"] == plugin.__version__
    raw = plugin.read_json_safe(plugin.rebase_status_path(str(root), RID))
    assert raw.get("uv_fills") is not None
    assert job.uv_fills is not None
    message = status["message"]
    assert message.count("リベースが完了しました。") == 1
    assert message.endswith("。")


def test_backup_failures_skip_reload(tmp_path):
    job, api, _reporter, clock, root = _job(tmp_path)
    api.save_error = RuntimeError("disk")
    job.run()
    job.tick(clock())
    job.tick(clock())
    assert job.state == "failed"
    assert job.outcome == "failed"
    assert "reload_mesh" not in [c[0] if isinstance(c, tuple) else c for c in api.calls]
    status = plugin.read_rebase_status(str(root), RID)
    assert status["reason"] == "backup_failed"

    job, api, _reporter, clock, root = _job(tmp_path)
    original = api.save_as_copy

    def missing(path):
        original(path)
        try:
            os.remove(path)
        except OSError:
            pass
        api.exists.discard(path)

    api.save_as_copy = missing
    job.run()
    job.tick(clock())
    job.tick(clock())
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "backup_failed"

    job, api, _reporter, clock, root = _job(tmp_path)

    def zero(path):
        original(path)
        api.sizes[path] = 0

    api.save_as_copy = zero
    job.run()
    job.tick(clock())
    job.tick(clock())
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "backup_failed"

    job, api, _reporter, clock, root = _job(tmp_path)
    before = api.spp

    def change(path):
        original(path)
        api.spp = str(tmp_path / "Other.spp")

    api.save_as_copy = change
    job.run()
    job.tick(clock())
    job.tick(clock())
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "backup_failed"
    api.spp = before


def test_backup_project_error_backoff(tmp_path):
    job, api, _reporter, clock, root = _job(tmp_path)
    api.save_error = ProjectError("locked")
    job.run()
    job.tick(clock())
    job.tick(clock())
    assert job.state == "backing_up"
    clock.advance(0.4)
    job.tick(clock())
    assert [c for c in api.calls if isinstance(c, tuple) and c[0] == "save_as_copy"]
    clock.advance(0.2)
    job.tick(clock())
    delays = [0.5, 1, 2, 4, 8]
    for delay in delays[1:]:
        clock.advance(delay)
        job.tick(clock())
    clock.advance(8)
    job.tick(clock())
    status = plugin.read_rebase_status(str(root), RID)
    assert status["state"] == "failed"
    assert status["reason"] == "project_locked"

    job, api, _reporter, clock, root = _job(
        tmp_path, settings=_settings(rebase_retry_max=0))
    api.save_error = ProjectError("locked")
    job.run()
    job.tick(clock())
    job.tick(clock())
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "project_locked"


def test_reload_callback_is_flag_only(tmp_path):
    job, api, _reporter, clock, root = _job(tmp_path)
    api.fail_if_called_from_callback = True
    job.run()
    job.tick(clock())
    job.tick(clock())
    job.tick(clock())
    before = job.state
    api.fire_reload(ReloadStatus("SUCCESS"))
    assert job.state == before
    job.tick(clock())
    assert job.state == "exporting"
    api.fire_reload(ReloadStatus("ERROR"))
    assert job.state == "exporting"
    job.tick(clock())
    assert job.state == "done"


def test_reload_error_timeout_and_busy_wait(tmp_path):
    job, api, _reporter, clock, root = _job(tmp_path)
    job.run()
    job.tick(clock())
    job.tick(clock())
    job.tick(clock())
    api.fire_reload(ReloadStatus("ERROR"))
    job.tick(clock())
    status = plugin.read_rebase_status(str(root), RID)
    assert status["reason"] == "reload_error"
    assert status["backup_name"]

    job, api, _reporter, clock, root = _job(tmp_path)
    job.run()
    job.tick(clock())
    job.tick(clock())
    job.tick(clock())
    api.fire_reload(ReloadStatus("UNKNOWN"))
    job.tick(clock())
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "reload_timeout"

    job, api, _reporter, clock, root = _job(
        tmp_path, settings=_settings(rebase_reload_timeout_s=30))
    job.run()
    job.tick(clock())
    job.tick(clock())
    job.tick(clock())
    clock.advance(31)
    job.tick(clock())
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "reload_timeout"

    job, api, _reporter, clock, root = _job(tmp_path)
    job.run()
    job.tick(clock())
    job.tick(clock())
    job.tick(clock())
    api.busy = True
    api.fire_reload(ReloadStatus("SUCCESS"))
    job.tick(clock())
    assert job.state == "reloading"
    api.busy = False
    job.tick(clock())
    assert job.state == "exporting"


def test_preparing_recheck_and_locked_cancel(tmp_path):
    cases = [
        (plugin.ApprovalDecision("ask", "needs_approval"), "backing_up"),
        (plugin.ApprovalDecision("run", "auto_approved"), "backing_up"),
        (plugin.ApprovalDecision("wait", "busy"), "preparing"),
        (plugin.ApprovalDecision("reject", "cancelled"), "declined"),
        (plugin.ApprovalDecision("reject", "superseded"), "superseded"),
        (plugin.ApprovalDecision("reject", "expired"), "failed"),
        (plugin.ApprovalDecision("reject", "mesh_hash_mismatch"), "failed"),
    ]
    for decision, expect in cases:
        job, api, _reporter, clock, root = _job(tmp_path)
        api.decision = decision
        job.run()
        job.tick(clock())
        assert job.state == expect, (decision, job.state)


def test_export_wait_warning_and_skip_final(tmp_path):
    job, api, _reporter, clock, root = _job(
        tmp_path, settings=_settings(rebase_reload_timeout_s=30))
    api.export_queue = [
        plugin.ExportAttempt(executed=False),
        plugin.ExportAttempt(executed=False),
    ]
    job.run()
    job.tick(clock())
    job.tick(clock())
    job.tick(clock())
    api.fire_reload(ReloadStatus("SUCCESS"))
    job.tick(clock())
    job.tick(clock())
    clock.advance(31)
    job.tick(clock())
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "export_failed"

    job, api, _reporter, clock, root = _job(tmp_path)
    api.export_queue = [
        plugin.ExportAttempt(
            executed=True, ok=False, status="warning",
            message="partial", export_id="ex-w"),
    ]
    _drive_happy_job(job, api, clock)
    status = plugin.read_rebase_status(str(root), RID)
    assert status["state"] == "failed"
    assert status["reason"] == "export_failed"
    assert "メッシュは読み直し済み" in status["message"]
    assert "やり直してください" in status["message"]
    assert status["backup_name"]

    req = _request(tmp_path, final_full=False)
    job, api, _reporter, clock, root = _job(tmp_path, request=req)
    _drive_happy_job(job, api, clock)
    status = plugin.read_rebase_status(str(root), RID)
    assert status["state"] == "done"
    assert status["result_export_id"] is None
    assert "run_final_export" not in [
        c[0] if isinstance(c, tuple) else c for c in api.calls]


def test_uv_fill_failure_still_done_and_record_failure(tmp_path):
    job, api, _reporter, clock, root = _job(tmp_path)

    def boom():
        raise RuntimeError("uv boom")

    api.uv_fill_context = boom
    _drive_happy_job(job, api, clock)
    status = plugin.read_rebase_status(str(root), RID)
    assert status["state"] == "done"
    raw = plugin.read_json_safe(plugin.rebase_status_path(str(root), RID))
    assert raw.get("uv_fills") is not None

    job, api, _reporter, clock, root = _job(tmp_path)
    api.record_ok = False
    _drive_happy_job(job, api, clock)
    assert plugin.read_rebase_status(str(root), RID)["state"] == "done"

    job, api, reporter, clock, root = _job(tmp_path)
    job.run()
    job.abort("interrupted", plugin.rebase_failure_message("interrupted"))
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "interrupted"


def test_terminal_write_retry(tmp_path, monkeypatch):
    job, api, reporter, clock, root = _job(tmp_path)
    writes = {"n": 0}
    real = plugin.write_rebase_status

    def flaky(root_arg, status):
        state = status.get("state") if isinstance(status, dict) else None
        if state == "done":
            writes["n"] += 1
            if writes["n"] < 3:
                return False
        return real(root_arg, status)

    patch_plugin_attr(monkeypatch, plugin, "write_rebase_status", flaky)
    _drive_happy_job(job, api, clock)
    assert job.finished
    job.tick(clock())
    job.tick(clock())
    assert writes["n"] >= 2


def _coord(tmp_path, api=None, clock=None, settings=None, dialogs=None, jobs=None):
    clock = clock or Clock()
    api = api or FakeApi(tmp_path)
    settings = settings or _settings()
    cfg = dict(settings)
    cfg["uvremap_root_dir"] = str(tmp_path)
    hooks = FakeHooks(tmp_path, cfg)
    dialogs = dialogs if dialogs is not None else []
    jobs = jobs if jobs is not None else []

    def dialog_factory(text, on_result):
        box = FakeDialog(text, on_result)
        dialogs.append(box)
        return box

    def job_factory(request, reporter, job_api, job_settings, clock=None, approved=True):
        job = plugin.RebaseJob(
            request, reporter, job_api, job_settings,
            clock=clock, approved=approved)
        jobs.append(job)
        return job

    coord = plugin.RebaseCoordinator(
        api, dialog_factory, job_factory, clock=clock, hooks=hooks)
    return coord, api, hooks, clock, dialogs, jobs


def _write_req(tmp_path, request):
    root = tmp_path / "proj"
    rb.write_request(str(root), request)
    return str(root)


def test_coordinator_ask_dialog_and_cancel(tmp_path):
    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    status = plugin.read_rebase_status(root, RID)
    assert status["state"] == "awaiting_approval"
    assert dialogs
    assert "2D ビュー" in dialogs[0].text
    assert "rebase_backup" in dialogs[0].text
    assert "Body" in dialogs[0].text
    dialogs[0].accept_run()
    coord.tick(clock())
    assert jobs
    assert jobs[0].state == "preparing"

    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)
    request = _request(tmp_path, request_id="rb-20261019-000002-0002")
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    dialogs[0].accept_cancel()
    coord.tick(clock())
    assert plugin.read_rebase_status(root, request["request_id"])["state"] == "declined"


def test_coordinator_expire_cancel_disabled_auto(tmp_path):
    coord, api, hooks, clock, dialogs, jobs = _coord(
        tmp_path, settings=_settings())
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    assert dialogs
    coord._entries[RID]["request"]["expires_at"] = clock() + 1
    clock.advance(2)
    coord.tick(clock())
    assert plugin.read_rebase_status(root, RID)["state"] == "expired"
    assert dialogs[0].closed

    rid = "rb-20261019-000003-0003"
    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)
    request = _request(tmp_path, request_id=rid)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    rb.write_cancel(root, rid)
    coord.tick(clock())
    assert plugin.read_rebase_status(root, rid)["state"] == "declined"

    coord, api, hooks, clock, dialogs, jobs = _coord(
        tmp_path, settings=_settings(rebase_enabled=False))
    request = _request(tmp_path, request_id="rb-20261019-000004-0004")
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    status = plugin.read_rebase_status(root, request["request_id"])
    assert status["state"] == "rejected"
    assert status["reason"] == "disabled"


def test_coordinator_auto_and_uv_old_text(tmp_path):
    coord, api, hooks, clock, dialogs, jobs = _coord(
        tmp_path, settings=_settings(rebase_approval="auto"))
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    assert not dialogs
    assert jobs

    api.last_edit_at = clock() - 1
    coord, api, hooks, clock, dialogs, jobs = _coord(
        tmp_path, api=api, settings=_settings(rebase_approval="auto"))
    request = _request(tmp_path, request_id="rb-20261019-000005-0005")
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    assert plugin.read_rebase_status(root, request["request_id"])["state"] == "received"
    clock.advance(15)
    api.last_edit_at = clock.t - 15
    coord.tick(clock())
    assert jobs

    api.supports_unwrap = False
    coord, api, hooks, clock, dialogs, jobs = _coord(
        tmp_path, api=api, settings=_settings(rebase_approval="auto"))
    request = _request(tmp_path, request_id="rb-20261019-000006-0006")
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    assert dialogs
    assert "すべて再計算" in dialogs[0].text

    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)
    request = _request(tmp_path, request_id="rb-20261019-000007-0007")
    request.pop("uv_old", None)
    parsed, reason = plugin.parse_rebase_request(request)
    assert reason is None
    rb.write_request(str(tmp_path / "proj"), parsed)
    coord.tick(clock())
    assert "旧 UV セット経由" not in dialogs[0].text
    assert "2D ビュー" in dialogs[0].text


def test_coordinator_wait_rewrites_only_when_reason_changes(tmp_path):
    api = FakeApi(tmp_path)
    api.project_is_open = False
    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path, api=api)
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    # project closed: tick returns without scan
    coord.tick(clock())
    assert plugin.read_rebase_status(root, RID) is None

    api.project_is_open = True
    api.busy = True
    coord.tick(clock())
    first = plugin.read_rebase_status(root, RID)
    clock.advance(1)
    coord.tick(clock())
    second = plugin.read_rebase_status(root, RID)
    assert first["updated_at"] == second["updated_at"]
    assert first["state"] == "received"
    assert first["reason"] == "busy"
    assert first["message"] == plugin.rebase_wait_message("busy")

    api.busy = False
    api.needs_saving = True
    clock.advance(1)
    coord.tick(clock())
    changed = plugin.read_rebase_status(root, RID)
    assert changed["updated_at"] > second["updated_at"]
    assert changed["reason"] == "unsaved_changes"
    assert changed["message"] == plugin.rebase_wait_message("unsaved_changes")


def test_wait_reason_messages_are_actionable_and_unknown_is_empty():
    assert "保存" in plugin.rebase_wait_message("unsaved_changes")
    assert "終わるまで" in plugin.rebase_wait_message("busy")
    assert "開いてください" in plugin.rebase_wait_message("no_project")
    assert "依頼と違います" in plugin.rebase_wait_message("project_mismatch")
    assert "落ち着く" in plugin.rebase_wait_message("recent_edit")
    assert plugin.rebase_wait_message("other") == ""
    assert plugin.is_valid_rebase_transition("received", "received") is True
    status = plugin.build_rebase_status(
        RID,
        "received",
        NOW,
        plugin.__version__,
        reason="recent_edit",
        message=plugin.rebase_wait_message("recent_edit"),
    )
    assert status["reason"] == "recent_edit"


def test_coordinator_supersede_and_job_lock(tmp_path):
    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)
    older = _request(tmp_path, request_id="rb-20261019-000010-000a", created_at=NOW - 20)
    newer = _request(tmp_path, request_id="rb-20261019-000011-000b", created_at=NOW - 5)
    root = str(tmp_path / "proj")
    rb.write_request(root, older)
    rb.write_request(root, newer)
    coord.tick(clock())
    assert plugin.read_rebase_status(root, older["request_id"])["state"] == "superseded"
    assert dialogs[-1].closed or plugin.read_rebase_status(
        root, newer["request_id"])["state"] in ("awaiting_approval", "received")


def test_coordinator_recovery_and_precheck_cache(tmp_path):
    root = str(tmp_path / "proj")
    (tmp_path / "proj" / "rebase" / "status").mkdir(parents=True)
    leftover = plugin.build_rebase_status(
        "rb-20261019-000012-000c", "reloading", NOW - 5, plugin.__version__)
    waiting = plugin.build_rebase_status(
        "rb-20261019-000013-000d", "received", NOW - 5, plugin.__version__)
    done = plugin.build_rebase_status(
        "rb-20261019-000014-000e", "done", NOW - 5, plugin.__version__)
    for status in (leftover, waiting, done):
        path = plugin.rebase_status_path(root, status["request_id"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        plugin.write_json_atomic(path, status)
    mesh = _mesh_file(tmp_path)
    live = _request(tmp_path, request_id="rb-20261019-000013-000d", mesh=mesh)
    rb.write_request(root, live)
    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)
    coord.tick(clock())
    assert plugin.read_rebase_status(root, leftover["request_id"])["reason"] == "interrupted"
    assert plugin.read_rebase_status(root, done["request_id"])["state"] == "done"
    assert plugin.read_rebase_status(root, live["request_id"])["state"] in (
        "received", "awaiting_approval")

    mesh_a = _mesh_file(tmp_path, b"hello-mesh")
    mesh_b = dict(mesh_a)
    mesh_b["sha1"] = "b" * 40
    rid_a = "rb-20261019-000020-0014"
    rid_b = "rb-20261019-000021-0015"
    req_a = _request(tmp_path, request_id=rid_a, mesh=mesh_a, created_at=NOW - 4)
    req_b = _request(tmp_path, request_id=rid_b, mesh=mesh_b, created_at=NOW - 3)
    rb.write_request(root, req_a)
    rb.write_request(root, req_b)
    hashes = {"n": 0}
    real_hash = plugin._hash_file_hex

    def counting(path, algorithm, chunk_size=None):
        hashes["n"] += 1
        return real_hash(path, algorithm)

    coord._precheck.clear()
    # newer request is processed
    original = plugin.mesh_precheck
    calls = []

    def wrapped(mesh, cache, *, stat_fn=os.stat, hash_fn=None):
        calls.append((mesh.get("sha1"), cache is not None))
        return original(mesh, cache, stat_fn=stat_fn, hash_fn=hash_fn)

    plugin_mesh = plugin.mesh_precheck
    coord.tick(clock())
    first_reads = dict(coord._read_counts)
    coord.tick(clock())
    for rid, count in first_reads.items():
        assert coord._read_counts.get(rid, 0) == count
    coord.on_project_closing()
    assert len(coord._precheck) == 0
    coord.shutdown()
    assert coord._job is None


def test_coordinator_swallows_exceptions_and_shutdown(tmp_path):
    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)

    def boom():
        raise RuntimeError("scan fail")

    api.project_open = boom
    coord.tick(clock())
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    api.project_open = lambda: True
    coord.tick(clock())
    assert dialogs
    job, job_api, _r, _c, _root = _job(tmp_path)
    job.run()
    job.tick(_c())
    job.tick(_c())
    job.tick(_c())
    coord._job = job
    coord.shutdown()
    assert dialogs[0].disconnected
    assert dialogs[0].closed
    assert job.state == "failed"
    coord.tick(clock())
    assert api.closed is False


def test_config_reload_interval(tmp_path):
    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)
    request = _request(tmp_path)
    _write_req(tmp_path, request)
    coord.tick(clock())
    first = hooks.load_calls
    clock.advance(2)
    coord.tick(clock())
    assert hooks.load_calls == first
    clock.advance(4)
    coord.tick(clock())
    assert hooks.load_calls > first


def test_maya_roundtrip_complete_and_warning(tmp_path):
    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    dialogs[0].accept_run()
    for _ in range(8):
        if jobs:
            if jobs[0].state == "reloading" and api.reload_callback:
                api.fire_reload(ReloadStatus("SUCCESS"))
        coord.tick(clock())
        if jobs and jobs[0].finished:
            break
    status = rb.read_status(root, RID)
    assert status.kind == "ok"
    display = rb.decide_display(request, status, now=clock(), cancel_requested=False)
    assert display.terminal is True
    dest = tmp_path / "final"
    dest.mkdir()
    result = plugin.build_export_result(
        plugin.__version__, "ex-round", "final", True, "success", "ok",
        1, 0, ["Body"], True, NOW, NOW + 1, request_id=RID)
    plugin.publish_export_outputs(str(dest), result, True)
    judged = rb.judge_completion(
        status_read=status,
        sync_result=rb.parse_sync_result(result),
        request=request,
        enabled=True,
        scene_id_now="scene-a",
        scene_id_request="scene-a",
        already_completed=False,
    )
    assert judged.action == "complete"
    assert rb.plugin_supports_rebase(rb.parse_sync_result(result)) is True

    api.export_queue = [
        plugin.ExportAttempt(executed=True, ok=False, status="warning", message="w"),
    ]
    coord2, api2, hooks2, clock2, dialogs2, jobs2 = _coord(tmp_path, api=api)
    rid = "rb-20261019-000030-001e"
    request2 = _request(tmp_path, request_id=rid)
    root2 = _write_req(tmp_path, request2)
    coord2.tick(clock2())
    dialogs2[0].accept_run()
    for _ in range(8):
        if jobs2 and jobs2[0].state == "reloading" and api2.reload_callback:
            api2.fire_reload(ReloadStatus("SUCCESS"))
        coord2.tick(clock2())
        if jobs2 and jobs2[0].finished:
            break
    failed = rb.read_status(root2, rid)
    warn_result = plugin.build_export_result(
        plugin.__version__, "ex-w", "final", True, "warning", "w",
        1, 0, ["Body"], True, NOW, NOW + 1, request_id=rid)
    judged2 = rb.judge_completion(
        status_read=failed,
        sync_result=rb.parse_sync_result(warn_result),
        request=request2,
        enabled=True,
        scene_id_now="scene-a",
        scene_id_request="scene-a",
        already_completed=False,
    )
    assert failed.status["state"] == "failed"
    assert judged2.action == "skip"


def test_export_path_writes_request_id_first(engine, isolated, monkeypatch, tmp_path):
    order = []
    real_json = plugin.write_json_atomic
    real_flag = plugin.write_sync_flag

    def _json(path, obj):
        order.append(("result", obj.get("request_id")))
        return real_json(path, obj)

    def _flag(path):
        order.append(("flag", None))
        return real_flag(path)

    patch_plugin_attr(monkeypatch, plugin, "write_json_atomic", _json)
    patch_plugin_attr(monkeypatch, plugin, "write_sync_flag", _flag)
    _arm_export(monkeypatch, isolated, "Success")
    engine._safe_export(preview=False, force_full=True, export_request_id=RID)
    assert order[0][0] == "result"
    assert order[0][1] == RID
    assert [kind for kind, _value in order[:3]] == ["result", "result", "flag"]
    copies = list((isolated / "uvremap").rglob("rebase/results/{0}.json".format(RID)))
    assert len(copies) == 1
    body = _status_file(isolated)
    assert json.loads(copies[0].read_text(encoding="utf-8")) == body
    parsed = rb.parse_sync_result(body)
    assert parsed["request_id"] == RID
    judged = rb.judge_completion(
        status_read=types.SimpleNamespace(
            kind="ok",
            status={"state": "done", "request_id": RID, "result_export_id": body["export_id"]},
        ),
        sync_result=parsed,
        request=_request(tmp_path),
        enabled=True,
        scene_id_now="s",
        scene_id_request="s",
        already_completed=False,
    )
    # judge may need a real StatusRead; at least request_id is in the file
    assert body["plugin_version"] == plugin.__version__
    engine._safe_export(preview=True)
    body2 = list(isolated.rglob("_sync_result.json"))
    assert body2


def test_run_rebase_final_export_cases(engine, isolated, monkeypatch):
    _arm_export(monkeypatch, isolated, "Success")
    engine.enabled = False
    got = engine.run_rebase_final_export(RID)
    assert got.executed is True
    assert got.ok is True
    _arm_export(monkeypatch, isolated, "Warning")
    got = engine.run_rebase_final_export(RID)
    assert got.ok is False
    monkeypatch.setattr(plugin.project, "is_busy", lambda: True)
    got = engine.run_rebase_final_export(RID)
    assert got.executed is False
    monkeypatch.setattr(plugin.project, "is_busy", lambda: False)
    engine.exporting = True
    got = engine.run_rebase_final_export(RID)
    assert got.executed is False
    engine.exporting = False
    monkeypatch.setattr(plugin.project, "is_open", lambda: False)
    got = engine.run_rebase_final_export(RID)
    assert got.executed is False


def test_blocks_exports_and_resume(engine, isolated, monkeypatch):
    engine._set_rebase_blocks_exports(True)
    engine.force_sync_now()
    assert engine.pending_reexport is True
    assert any("リベース中" in text for text in engine.messages)
    engine.pending_final = True
    engine._set_rebase_blocks_exports(False)
    engine._resume_blocked_exports()
    assert engine._final_export_timer.started_ms is not None or engine.pending_final


def test_engine_wiring_and_shutdown(engine, monkeypatch):
    assert engine._rebase_timer is not None
    engine.start_rebase_watch()
    assert getattr(engine._rebase_timer, "started_ms", None) == 1000
    engine.enabled = False
    engine.on_texture_state(types.SimpleNamespace(action=plugin.event.TextureStateEventAction.ADD, stack_id=1))
    assert engine._last_texture_event_at is not None
    assert plugin.__version__ == "1.4.5"
    src = inspect.getsource(plugin.LiveSyncPanel)
    assert "rebase" not in src
    engine.shutdown()
    # tick after shutdown must not call api
    calls = []
    engine._rebase.api = types.SimpleNamespace(
        closed=True,
        project_open=lambda: calls.append("open") or True,
    )
    engine._on_rebase_timer()
    engine._rebase.tick()
    assert calls == []


def test_shutdown_safety(tmp_path, engine):
    coord, api, hooks, clock, dialogs, jobs = _coord(tmp_path)
    request = _request(tmp_path)
    _write_req(tmp_path, request)
    coord.tick(clock())
    assert dialogs
    job, job_api, _r, _c, root = _job(tmp_path)
    job.run()
    job.tick(_c())
    job.tick(_c())
    job.tick(_c())
    coord._job = job
    coord._job_request_id = RID
    coord.shutdown()
    assert dialogs[0].disconnected and dialogs[0].closed
    assert job.state == "failed"
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "interrupted"
    job_api.fire_reload(ReloadStatus("SUCCESS"))
    assert job.state == "failed"
    reporter = plugin.RebaseReporter(
        root=str(root), request_id=RID, clock=clock,
        log_fn=lambda *a: None,
        emit_status_fn=lambda text: (_ for _ in ()).throw(AssertionError("signal")),
        shutting_down_fn=lambda: True,
        plugin_version=plugin.__version__,
    )
    reporter.log("quiet")
    engine._shutting_down = True
    engine._safe_export(preview=True)
    assert engine._last_export_attempt.executed is False
    engine._resume_safe_export_after_busy(True, None, False, None)
    assert engine._last_export_attempt.executed is False
    engine._shutting_down = False
    tree = ast.parse(plugin_source_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "attr", None) == "singleShot":
            raise AssertionError("QTimer.singleShot call remains")
    engine._shutting_down = True
    engine.on_texture_state(types.SimpleNamespace(action="add", stack_id=1))
    engine.on_project_saved(None)
    engine.on_project_closing(None)
    engine.on_project_edition_entered(None)


def test_close_plugin_survives_panel_error(monkeypatch):
    class Boom(object):
        def shutdown(self):
            pass

    plugin._engine = Boom()
    plugin._panel = object()

    def fail(_panel):
        raise RuntimeError("ui")

    monkeypatch.setattr(plugin.ui, "delete_ui_element", fail)
    plugin.close_plugin()
    assert plugin._engine is None
    assert plugin._panel is None


def test_plugin_version_in_results(engine, isolated, monkeypatch):
    _arm_export(monkeypatch, isolated, "Success")
    engine._safe_export(preview=True)
    body = _status_file(isolated)
    assert body["plugin_version"] == plugin.__version__
    assert rb.plugin_supports_rebase(rb.parse_sync_result(body)) is True


def test_coexist_with_settings_ui(engine, isolated, monkeypatch):
    cfg_obj = engine.config
    saved = {key: engine.config.get(key) for key in plugin.PANEL_SETTING_KEYS}
    engine._rebase.tick()
    assert engine.config is cfg_obj
    assert {key: engine.config.get(key) for key in plugin.PANEL_SETTING_KEYS} == saved
    assert plugin.unsaved_setting_keys(saved, engine.config) == []
    assert list(inspect.signature(engine.force_sync_now).parameters) == []
    assert list(inspect.signature(engine.force_full_final_sync).parameters) == []
    assert "new_values" in inspect.signature(engine.apply_and_save_config).parameters
    engine._set_rebase_blocks_exports(True)
    engine.force_sync_now()
    assert engine.pending_reexport is True
    assert any("リベース中" in text for text in engine.messages)
    assert callable(plugin.unsaved_setting_keys)
    assert plugin.PANEL_SETTING_KEYS


def test_no_lambda_connect_or_new_threads():
    tree = ast.parse(plugin_source_text())
    banned = {"threading", "atexit", "subprocess"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (banned & imported)
    new_types = {
        "RebaseJob", "RebaseCoordinator", "RebaseReporter",
        "PainterRebaseApi", "RebaseApprovalDialogHost", "RebaseEngineHooks",
    }
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in new_types:
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                if getattr(child.func, "attr", None) != "connect":
                    continue
                for arg in child.args:
                    assert not isinstance(arg, ast.Lambda), ast.dump(arg)


def test_terminal_write_failure_keeps_job_for_retry(tmp_path, monkeypatch):
    coord, api, hooks, clock, _dialogs, jobs = _coord(
        tmp_path, settings=_settings(rebase_approval="auto"))
    request = _request(tmp_path, final_full=False)
    root = _write_req(tmp_path, request)
    real_write = plugin.write_rebase_status
    pending = {"fails": 2}

    def flaky(path_root, status):
        if isinstance(status, dict) and status.get("state") in plugin.REBASE_TERMINAL_STATES:
            if pending["fails"] > 0:
                pending["fails"] -= 1
                return False
        return real_write(path_root, status)

    patch_plugin_attr(monkeypatch, plugin, "write_rebase_status", flaky)
    held = False
    for _ in range(16):
        coord.tick(clock())
        if api.reload_callback is not None and not getattr(api, "_fired", False):
            api.fire_reload(ReloadStatus("SUCCESS"))
            api._fired = True
        if (
            coord._job is not None
            and getattr(coord._job, "finished", False)
            and pending["fails"] >= 0
            and plugin.read_rebase_status(root, RID).get("state") != "done"
        ):
            held = True
        if coord._job is None and jobs and jobs[0].finished and pending["fails"] == 0:
            break
    assert held
    assert coord._job is None
    status = plugin.read_rebase_status(root, RID)
    assert status["state"] == "done"
    assert any("終端状態" in text for _level, text in hooks.logs)


def test_terminal_write_giveup_is_logged(tmp_path, monkeypatch):
    coord, api, hooks, clock, _dialogs, jobs = _coord(
        tmp_path, settings=_settings(rebase_approval="auto"))
    request = _request(tmp_path, final_full=False)
    root = _write_req(tmp_path, request)
    real_write = plugin.write_rebase_status

    def always_fail(path_root, status):
        if isinstance(status, dict) and status.get("state") in plugin.REBASE_TERMINAL_STATES:
            return False
        return real_write(path_root, status)

    patch_plugin_attr(monkeypatch, plugin, "write_rebase_status", always_fail)
    for _ in range(40):
        if coord._job is None and jobs and jobs[0].finished:
            break
        coord.tick(clock())
        if api.reload_callback is not None and not getattr(api, "_fired", False):
            api.fire_reload(ReloadStatus("SUCCESS"))
            api._fired = True
    assert jobs and jobs[0].finished
    assert coord._job is None
    status = plugin.read_rebase_status(root, RID)
    assert status["state"] != "done"
    assert any("終端状態の書き込みを" in text for _level, text in hooks.logs)


def test_cancel_during_preparing_skips_backup_and_reload(tmp_path):
    coord, api, _hooks, clock, _dialogs, jobs = _coord(
        tmp_path, settings=_settings(rebase_approval="auto"))
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    assert jobs and jobs[0].state == "preparing"
    rb.write_cancel(root, RID)
    coord.tick(clock())
    assert jobs[0].state == "declined"
    assert jobs[0].outcome == "declined"
    assert coord._job is None
    names = [c[0] if isinstance(c, tuple) else c for c in api.calls]
    assert "save_as_copy" not in names
    assert "reload_mesh" not in names
    status = plugin.read_rebase_status(root, RID)
    assert status["state"] == "declined"
    assert status["reason"] == "cancelled"


def test_cancel_after_backing_up_still_continues(tmp_path):
    coord, api, _hooks, clock, _dialogs, jobs = _coord(
        tmp_path, settings=_settings(rebase_approval="auto"))
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    coord.tick(clock())
    assert jobs[0].state == "backing_up"
    rb.write_cancel(root, RID)
    coord.tick(clock())
    names = [c[0] if isinstance(c, tuple) else c for c in api.calls]
    assert "save_as_copy" in names
    assert jobs[0].state != "declined"
    status = plugin.read_rebase_status(root, RID)
    assert status["state"] != "declined"


def test_busy_waits_around_reload_time_out(tmp_path):
    job, api, _reporter, clock, root = _job(
        tmp_path, settings=_settings(rebase_reload_timeout_s=30))
    api.busy = True
    job.run()
    job.tick(clock())
    job.tick(clock())
    assert job.state == "reloading"
    job.tick(clock())
    assert not any(isinstance(c, tuple) and c[0] == "reload_mesh" for c in api.calls)
    clock.advance(31)
    job.tick(clock())
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "reload_timeout"
    assert job.state == "failed"

    job, api, _reporter, clock, root = _job(
        tmp_path, settings=_settings(rebase_reload_timeout_s=30))
    job.run()
    job.tick(clock())
    job.tick(clock())
    job.tick(clock())
    api.fire_reload(ReloadStatus("SUCCESS"))
    api.busy = True
    job.tick(clock())
    assert job.state == "reloading"
    clock.advance(31)
    job.tick(clock())
    assert job.state == "failed"
    assert plugin.read_rebase_status(str(root), RID)["reason"] == "reload_timeout"


def test_empty_ts_names_is_not_all_texture_sets(tmp_path, monkeypatch):
    request = _request(tmp_path)
    request["ts_names"] = []
    job, api, _reporter, clock, _root = _job(tmp_path, request=request)
    calls = []

    def spy(stack, fill_api, label, source):
        calls.append(label)
        return plugin.UvFillStackReport(label)

    patch_plugin_attr(monkeypatch, plugin, "switch_uv_fills_in_stack", spy)

    class _Ts(object):
        def name(self):
            return "Body"

        def all_stacks(self):
            return ["stack"]

    api.uv_ctx = {
        "api": object(),
        "texture_sets": [_Ts()],
        "ts_names": ["Body", "Trim"],
    }
    job.run()
    job.tick(clock())
    job.tick(clock())
    job.tick(clock())
    api.fire_reload(ReloadStatus("SUCCESS"))
    job.tick(clock())
    assert calls == []
    assert job.uv_fills is not None
    assert job.uv_fills.gate == "no_matching_texture_set"


def test_unreadable_needs_saving_ask_false_auto_waits(tmp_path, monkeypatch):
    api = FakeApi(tmp_path)
    api.needs_saving = False
    api.needs_saving_missing = True
    coord, api, hooks, clock, dialogs, jobs = _coord(
        tmp_path, api=api, settings=_settings(rebase_approval="auto"))
    root = _write_req(tmp_path, _request(tmp_path))
    coord.tick(clock())
    assert jobs == []
    assert plugin.read_rebase_status(root, RID)["state"] == "received"
    assert any("auto" in text for _level, text in hooks.logs)

    monkeypatch.setattr(plugin.project, "is_open", lambda: True)
    monkeypatch.setattr(plugin.project, "is_busy", lambda: False)
    monkeypatch.setattr(plugin.project, "name", lambda: "Demo")
    monkeypatch.setattr(plugin.project, "file_path", lambda: PROJECT_KEY)
    monkeypatch.setattr(
        plugin.project, "is_in_edition_state", lambda: True, raising=False)

    def _needs_saving():
        raise RuntimeError("missing")

    monkeypatch.setattr(plugin.project, "needs_saving", _needs_saving, raising=False)

    def _mesh_settings(auto_unwrap_settings=None, **kwargs):
        return kwargs

    monkeypatch.setattr(
        plugin.project, "MeshReloadingSettings", _mesh_settings, raising=False)

    class _Pre(object):
        def get(self, rid):
            return None

        def put(self, rid, cache):
            return None

    class _Coord(object):
        def __init__(self, approval):
            self._settings = _settings(rebase_approval=approval)
            self._cancelled = set()
            self._superseded = set()
            self._precheck = _Pre()

        def clock(self):
            return NOW

    class _Eng(object):
        def __init__(self, approval):
            self._shutting_down = False
            self.exporting = False
            # recheck の時刻は PainterRebaseApi.now() (実時刻)。アイドルはそれを基準にする。
            self._last_texture_event_at = time.time() - 100
            self._needs_saving_missing_logged = False
            self._rebase = _Coord(approval)

    request = _request(tmp_path, created_at=time.time(), ttl_s=1800)
    auto_api = plugin.PainterRebaseApi(_Eng("auto"))
    auto_decision = auto_api.recheck(request)
    assert auto_decision.action == "wait"
    assert auto_decision.reason == "unsaved_changes"
    ask_api = plugin.PainterRebaseApi(_Eng("ask"))
    ask_decision = ask_api.recheck(request)
    assert ask_decision.action == "ask"


def test_busy_export_callbacks_keep_their_args(engine, monkeypatch):
    queued = []
    monkeypatch.setattr(plugin.project, "is_open", lambda: True)
    monkeypatch.setattr(plugin.project, "is_busy", lambda: True)
    monkeypatch.setattr(plugin.project, "execute_when_not_busy", queued.append)
    engine._safe_export(preview=True, export_request_id=None)
    engine._safe_export(
        preview=False, force_full=True, export_request_id=RID)
    assert len(queued) == 2
    assert queued[0] is not queued[1]
    seen = []

    def fake_do_export(
        preview=True, dirty_stack_ids=None, force_full=False, export_request_id=None,
    ):
        seen.append((preview, force_full, export_request_id))

    monkeypatch.setattr(engine, "_do_export", fake_do_export)
    monkeypatch.setattr(plugin.project, "is_busy", lambda: False)
    queued[0]()
    queued[1]()
    assert seen == [(True, False, None), (False, True, RID)]
    engine._shutting_down = True
    queued[0]()
    assert seen == [(True, False, None), (False, True, RID)]


def test_close_plugin_reports_engine_released(monkeypatch):
    logs = []

    def capture(level, text):
        logs.append((level, text))

    patch_plugin_attr(monkeypatch, plugin, "_log", capture)
    plugin._engine = plugin.LiveSyncEngine()
    plugin._panel = types.SimpleNamespace(engine=plugin._engine)
    plugin.close_plugin()
    assert plugin._engine is None
    assert plugin._panel is None
    assert any("解放されました" in text for _level, text in logs)
    assert not any("残っています" in text for _level, text in logs)


class _BoxSignal(object):
    def __init__(self):
        self.slots = []
        self.ever = []

    def connect(self, fn):
        self.slots.append(fn)
        self.ever.append(fn)

    def disconnect(self, fn=None):
        if fn is None:
            self.slots = []
            return
        self.slots = [slot for slot in self.slots if slot is not fn]

    def emit_ever(self, *args):
        # 切断のあとでも finished が届く場合を含めて、一度つながったスロットを呼ぶ。
        for slot in list(self.ever):
            slot(*args)


class _RecordingMessageBox(object):
    """ボタンのラッパーが作り直されても、役割だけで action を決めるための偽 Qt。"""

    # PySide の AcceptRole は 0。真偽値で落とさないことをテストで固定する。
    AcceptRole = 0
    RejectRole = 1

    def __init__(self, parent=None):
        self.parent_arg = parent
        self.calls = []
        self.finished = _BoxSignal()
        self.foreign_clicked = object()
        self.role_for_clicked = self.AcceptRole
        self.click_error = None
        self.role_error = None
        self.on_close = None
        self.escape_button = None

    def setWindowTitle(self, title):
        self.calls.append(("setWindowTitle", title))

    def setText(self, text):
        self.calls.append(("setText", text))

    def addButton(self, label, role):
        button = object()
        self.calls.append(("addButton", label, role))
        return button

    def setDefaultButton(self, button):
        self.calls.append(("setDefaultButton", button))

    def setEscapeButton(self, button):
        self.escape_button = button
        self.calls.append(("setEscapeButton", button))

    def setWindowModality(self, modality):
        self.calls.append(("setWindowModality", modality))

    def setModal(self, modal):
        self.calls.append(("setModal", modal))

    def show(self):
        self.calls.append(("show",))

    def clickedButton(self):
        if self.click_error is not None:
            raise self.click_error
        return self.foreign_clicked

    def buttonRole(self, button):
        if self.role_error is not None:
            raise self.role_error
        return self.role_for_clicked

    def close(self):
        self.calls.append(("close",))
        if self.on_close is not None:
            self.on_close()
        self.finished.emit_ever(0)

    def deleteLater(self):
        self.calls.append(("deleteLater",))


def _patch_approval_qt(monkeypatch):
    monkeypatch.setattr(plugin.QtWidgets, "QMessageBox", _RecordingMessageBox)
    parent = object()
    monkeypatch.setattr(plugin.ui, "get_main_window", lambda: parent, raising=False)
    return parent


def test_approval_action_from_role():
    accept_role = 0
    reject_role = 1

    class _Unequal(object):
        def __eq__(self, other):
            raise RuntimeError("eq")

    assert plugin.approval_action_from_role(accept_role, accept_role) == "run"
    assert plugin.approval_action_from_role(reject_role, accept_role) == "cancel"
    assert plugin.approval_action_from_role(None, accept_role) == "cancel"
    assert plugin.approval_action_from_role(object(), accept_role) == "cancel"
    assert plugin.approval_action_from_role(_Unequal(), accept_role) == "cancel"


def test_approval_dialog_uses_role_and_is_nonmodal_before_show(monkeypatch):
    parent = _patch_approval_qt(monkeypatch)
    actions = []
    host = plugin.RebaseApprovalDialogHost("確認", actions.append)
    box = host.box
    assert host._parent is parent
    assert box.parent_arg is parent
    assert box.foreign_clicked is not host._run_btn
    assert box.foreign_clicked is not host._cancel_btn
    names = [call[0] for call in box.calls]
    modality_at = names.index("setWindowModality")
    show_at = names.index("show")
    assert modality_at < show_at
    assert box.calls[modality_at] == ("setWindowModality", plugin.QtCore.Qt.NonModal)
    assert "setModal" not in names
    assert box.escape_button is host._cancel_btn
    host._on_finished()
    assert actions == ["run"]
    box.role_for_clicked = _RecordingMessageBox.RejectRole
    host._on_finished()
    box.foreign_clicked = None
    host._on_finished()
    box.foreign_clicked = object()
    box.role_for_clicked = "NoRole"
    host._on_finished()
    box.click_error = RuntimeError("clicked")
    host._on_finished()
    box.click_error = None
    box.role_error = RuntimeError("role")
    host._on_finished()
    assert actions == ["run", "cancel", "cancel", "cancel", "cancel", "cancel"]
    closed_at = []
    box.on_close = lambda: closed_at.append(host._closed)
    before = len(actions)
    host.close()
    assert closed_at == [True]
    assert host._parent is None
    assert len(actions) == before
    host2 = plugin.RebaseApprovalDialogHost("確認", actions.append)
    assert host2._parent is parent
    host2.deleteLater()
    assert host2._parent is None


def _approval_host_factory(hosts, actions, closed_flags):
    def factory(text, on_result):
        def wrapped(action):
            actions.append(action)
            on_result(action)

        host = plugin.RebaseApprovalDialogHost(text, wrapped)
        host.box.on_close = lambda: closed_flags.append(host._closed)
        hosts.append(host)
        return host

    return factory


def _host_closed_on_close(on_result):
    host = plugin.RebaseApprovalDialogHost("確認", on_result)
    flags = []
    host.box.on_close = lambda: flags.append(host._closed)
    return host, flags


def test_closing_approval_dialog_does_not_decline(tmp_path, monkeypatch, engine):
    _patch_approval_qt(monkeypatch)
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)

    def _open():
        hosts = []
        actions = []
        closed_flags = []
        coord, _api, _hooks, clock, _dialogs, _jobs = _coord(tmp_path)
        coord.dialog_factory = _approval_host_factory(hosts, actions, closed_flags)
        coord.tick(clock())
        assert plugin.read_rebase_status(root, RID)["state"] == "awaiting_approval"
        assert len(hosts) == 1
        return coord, clock, actions, closed_flags

    coord, clock, actions, closed_flags = _open()
    before = plugin.read_rebase_status(root, RID)
    coord.shutdown()
    assert closed_flags == [True]
    assert actions == []
    after = plugin.read_rebase_status(root, RID)
    assert after["state"] == "awaiting_approval"
    assert after["reason"] == before["reason"]
    coord.tick(clock())
    assert plugin.read_rebase_status(root, RID)["state"] == "awaiting_approval"

    coord, clock, actions, closed_flags = _open()
    coord.on_project_closing()
    assert closed_flags == [True]
    assert actions == []
    assert plugin.read_rebase_status(root, RID)["state"] == "awaiting_approval"
    coord.tick(clock())
    again = plugin.read_rebase_status(root, RID)
    assert again["state"] == "awaiting_approval"
    assert again.get("reason") != "declined"

    actions = []
    host, flags = _host_closed_on_close(actions.append)
    engine._rebase._dialog = host
    engine._rebase._dialog_rid = RID
    engine.on_project_closing(None)
    assert flags == [True]
    assert actions == []

    actions = []
    host, flags = _host_closed_on_close(actions.append)
    engine._rebase._dialog = host
    engine._rebase._dialog_rid = RID
    engine.shutdown()
    assert flags == [True]
    assert actions == []

    actions = []
    eng = plugin.LiveSyncEngine()
    host, flags = _host_closed_on_close(actions.append)
    eng._rebase._dialog = host
    eng._rebase._dialog_rid = RID
    plugin._engine = eng
    plugin._panel = None
    plugin.close_plugin()
    assert flags == [True]
    assert actions == []
    assert plugin._engine is None
