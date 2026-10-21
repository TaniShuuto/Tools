"""リベースの司令塔(RebaseCoordinator)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os
import time

from ._version import __version__
from ._base import (
    _append_history,
    _is_unsaved_project_key,
    _log,
    is_valid_rebase_request_id,
    load_config,
)
from ._rebase_policy import (
    RebasePrecheckCaches,
    build_painter_state,
    build_rebase_approval_text,
    decide_approval,
    mesh_precheck,
    needs_saving_for_rebase,
    rebase_backup_path,
    rebase_wait_message,
    wait_timed_out,
)
from ._rebase_protocol import (
    BACKUP_INCLUDES_UNSAVED,
    REBASE_BACKUP_DIRNAME,
    REBASE_CANCELABLE_STATES,
    REBASE_LOCKED_STATES,
    REBASE_TERMINAL_WRITE_MAX,
    _REBASE_TERMINAL,
    _rebase_is_finite_number,
    build_interrupted_status,
    build_rebase_status,
    diff_rebase_scan,
    effective_rebase_approval,
    find_superseded_ids,
    get_uv_old_index,
    normalize_rebase_settings,
    read_rebase_request_with_reason,
    read_rebase_status,
    rebase_cancel_path,
    rebase_failure_message,
    rebase_reject_state,
    rebase_requests_dir,
    rebase_status_dir,
    resolve_uvremap_root,
    scan_rebase_dir,
    write_rebase_status,
)
from ._rebase_uvfill import inspect_direction_uv_fills
from ._rebase_job import RebaseReporter

__all__ = [
    "_REBASE_CFG_RELOAD_S",
    "RebaseCoordinator",
    "_iter_status_ids",
]


_REBASE_CFG_RELOAD_S = 5.0


class RebaseCoordinator(object):
    def __init__(self, api, dialog_factory, job_factory, clock=time.time, hooks=None):
        self.api = api
        self.dialog_factory = dialog_factory
        self.job_factory = job_factory
        self.clock = clock if callable(clock) else time.time
        self.hooks = hooks
        self._precheck = RebasePrecheckCaches()
        self._job = None
        self._job_request_id = None
        self._dialog = None
        self._dialog_rid = None
        self._dialog_action = None
        self._entries = {}
        self._cancelled = set()
        self._superseded = set()
        self._prev_scan = None
        self._recover_key = None
        self._cfg = None
        self._settings = None
        self._last_cfg_at = None
        self._shutting_down = False
        self._root = None
        self._last_wait_reason = {}
        self._needs_saving_missing_logged = False
        self._can_disable_cache = None
        self._read_counts = {}

    def _now(self, now=None):
        if now is not None:
            return now
        return self.clock()

    def _log(self, level, text):
        if self.hooks is not None and callable(getattr(self.hooks, "log", None)):
            try:
                self.hooks.log(level, text)
                return
            except Exception:
                pass
        _log(level, text)

    def _emit(self, text):
        if self._shutting_down:
            self._log("info", text)
            try:
                _append_history("SP", text)
            except Exception:
                pass
            return
        if self.hooks is not None and callable(getattr(self.hooks, "emit_status", None)):
            try:
                self.hooks.emit_status(text)
                return
            except Exception:
                pass
        _log("info", text)

    def _is_closed(self):
        if self._shutting_down:
            return True
        if self.api is not None and getattr(self.api, "closed", False):
            return True
        if self.hooks is not None and callable(getattr(self.hooks, "is_shutting_down", None)):
            try:
                return bool(self.hooks.is_shutting_down())
            except Exception:
                return False
        return False

    def _set_blocks(self, value):
        if self.hooks is not None and callable(getattr(self.hooks, "set_blocks_exports", None)):
            try:
                self.hooks.set_blocks_exports(bool(value))
            except Exception:
                pass

    def _resume_blocked(self):
        if self.hooks is not None and callable(getattr(self.hooks, "resume_blocked_exports", None)):
            try:
                self.hooks.resume_blocked_exports()
            except Exception:
                pass

    def _load_settings(self, now):
        if self._last_cfg_at is not None and (now - self._last_cfg_at) < _REBASE_CFG_RELOAD_S:
            return
        loader = load_config
        if self.hooks is not None and callable(getattr(self.hooks, "load_config", None)):
            loader = self.hooks.load_config
        try:
            self._cfg = loader()
        except Exception as exc:
            self._log("error", "rebase: 設定の再読込に失敗しました: {0}".format(exc))
            if self._cfg is None:
                self._cfg = {}
        self._settings = normalize_rebase_settings(self._cfg)
        self._last_cfg_at = now

    def _project_open(self):
        if self.api is not None and callable(getattr(self.api, "project_open", None)):
            try:
                return bool(self.api.project_open())
            except Exception:
                return False
        return False

    def _current_key(self):
        if self.hooks is not None and callable(getattr(self.hooks, "current_project_key", None)):
            try:
                return self.hooks.current_project_key()
            except Exception:
                return None
        if self.api is not None and callable(getattr(self.api, "current_project_key", None)):
            try:
                return self.api.current_project_key()
            except Exception:
                return None
        return None

    def _subfolder(self):
        if self.hooks is not None and callable(getattr(self.hooks, "project_subfolder", None)):
            try:
                return self.hooks.project_subfolder()
            except Exception:
                return None
        return None

    def tick(self, now=None):
        try:
            self._tick_impl(now)
        except Exception as exc:
            self._log("error", "RebaseCoordinator.tick: {0}".format(exc))

    def _tick_impl(self, now):
        now = self._now(now)
        if self._is_closed():
            return
        if not self._project_open():
            return
        self._load_settings(now)
        if self._job is not None:
            self._decline_preparing_if_cancelled()
            if self._job is not None:
                self._job.tick(now)
            if self._job is not None and self._job.finished:
                if self._terminal_write_pending():
                    return
                self._on_job_finished()
            if self._job is not None:
                return
        key = self._current_key()
        if _is_unsaved_project_key(key):
            return
        subfolder = self._subfolder()
        root = resolve_uvremap_root(self._cfg, subfolder)
        self._root = root
        if not root:
            return
        requests_dir = rebase_requests_dir(root)
        if not os.path.isdir(requests_dir):
            return
        self._recover_if_needed(root, key, now)
        self._scan_and_ingest(root, now)
        self._apply_cancels(root)
        self._apply_superseded()
        if self._dialog_action:
            self._handle_dialog_action(now)
        self._process_active(root, now)

    def _terminal_write_pending(self):
        """終端の書き込みが失敗したまま、再試行の上限に達していなければ真。"""
        job = self._job
        if job is None:
            return False
        reporter = getattr(job, "reporter", None)
        if reporter is None:
            return False
        if getattr(reporter, "_pending_terminal", None) is None:
            return False
        tries = getattr(reporter, "_terminal_tries", 0)
        return tries < REBASE_TERMINAL_WRITE_MAX

    def _decline_preparing_if_cancelled(self):
        """preparing までのジョブは .cancel を見て declined で止める。"""
        job = self._job
        if job is None or job.finished:
            return
        state = getattr(job, "state", None)
        if state not in REBASE_CANCELABLE_STATES:
            return
        rid = self._job_request_id
        root = self._root
        if not isinstance(rid, str) or not root:
            return
        cancelled = rid in self._cancelled
        if not cancelled:
            try:
                cancelled = os.path.isfile(rebase_cancel_path(root, rid))
            except OSError:
                cancelled = False
        if not cancelled:
            return
        self._cancelled.add(rid)
        try:
            job._end_reject("cancelled", state)
        except Exception as exc:
            self._log("error", "rebase: preparing 中の取り消しに失敗しました: {0}".format(exc))

    def _on_job_finished(self):
        rid = self._job_request_id
        if rid:
            state = getattr(self._job, "state", None)
            self._precheck.drop_if_terminal(rid, state)
            entry = self._entries.get(rid)
            if entry is not None:
                entry["state"] = state
        self._job = None
        self._job_request_id = None
        self._set_blocks(False)
        self._resume_blocked()

    def _recover_if_needed(self, root, key, now):
        if self._recover_key == key:
            return
        self._recover_key = key
        for rid in _iter_status_ids(root):
            if self._job is not None and rid == self._job_request_id:
                continue
            prev = read_rebase_status(root, rid)
            if prev is None:
                continue
            state = prev.get("state")
            if state in ("preparing", "backing_up", "reloading", "exporting"):
                interrupted = build_interrupted_status(prev, now, __version__)
                if interrupted is not None:
                    write_rebase_status(root, interrupted)
                    self._precheck.drop(rid)
            elif state in _REBASE_TERMINAL:
                continue

    def _scan_and_ingest(self, root, now):
        scan = scan_rebase_dir(root)
        diff = diff_rebase_scan(self._prev_scan, scan)
        self._prev_scan = scan
        for rid in diff.new_cancel_ids:
            entry = self._entries.get(rid)
            state = entry.get("state") if entry else None
            if self._job is not None and rid == self._job_request_id:
                job_state = getattr(self._job, "state", None)
                if job_state in REBASE_LOCKED_STATES:
                    continue
            if state in REBASE_CANCELABLE_STATES or state is None:
                self._cancelled.add(rid)
        for rid in diff.changed_ids:
            existing = read_rebase_status(root, rid)
            if existing is not None and existing.get("state") in _REBASE_TERMINAL:
                self._precheck.drop(rid)
                continue
            request, reason = read_rebase_request_with_reason(root, rid)
            self._read_counts[rid] = self._read_counts.get(rid, 0) + 1
            if request is None:
                if existing is None:
                    reject_reason = reason or "bad_request"
                    self._write_once(
                        root, rid, "rejected", now, reason=reject_reason,
                        message=rebase_failure_message(reject_reason),
                    )
                continue
            entry = self._entries.get(rid) or {}
            entry["request"] = request
            if existing is None:
                if entry.get("state") is None:
                    self._write_once(root, rid, "received", now)
                    entry["state"] = "received"
            else:
                entry["state"] = existing.get("state")
            self._entries[rid] = entry

    def _apply_cancels(self, root):
        for rid in list(self._cancelled):
            entry = self._entries.get(rid)
            if entry is None:
                continue
            state = entry.get("state")
            if state not in REBASE_CANCELABLE_STATES:
                continue
            if self._job is not None and rid == self._job_request_id:
                if getattr(self._job, "state", None) in REBASE_LOCKED_STATES:
                    continue
            self._finish_request(
                root, rid, "declined", reason="cancelled",
                message=rebase_failure_message("cancelled"),
            )
            if self._dialog_rid == rid:
                self._close_dialog()

    def _apply_superseded(self):
        live = []
        for rid, entry in self._entries.items():
            state = entry.get("state")
            if state in _REBASE_TERMINAL:
                continue
            request = entry.get("request")
            if request:
                live.append(request)
        if not live:
            return
        for rid in find_superseded_ids(live):
            entry = self._entries.get(rid)
            if entry is None:
                continue
            state = entry.get("state")
            if self._job is not None and rid == self._job_request_id:
                if getattr(self._job, "state", None) in REBASE_LOCKED_STATES:
                    continue
            if state not in REBASE_CANCELABLE_STATES:
                continue
            self._superseded.add(rid)
            self._finish_request(
                self._root, rid, "superseded", reason="superseded",
                message=rebase_failure_message("superseded"),
            )
            if self._dialog_rid == rid:
                self._close_dialog()

    def _handle_dialog_action(self, now):
        action = self._dialog_action
        rid = self._dialog_rid
        self._dialog_action = None
        if not rid:
            return
        entry = self._entries.get(rid)
        if action == "run":
            self._close_dialog()
            if entry and entry.get("request"):
                self._start_job(entry["request"], approved=True)
            return
        self._close_dialog()
        if entry and entry.get("state") not in _REBASE_TERMINAL:
            self._finish_request(
                self._root, rid, "declined", reason="declined",
                message=rebase_failure_message("declined"),
            )

    def _process_active(self, root, now):
        if self._job is not None:
            return
        active = self._newest_nonterminal()
        if active is None:
            return
        rid, entry = active
        request = entry.get("request")
        if request is None:
            return
        mesh = request.get("mesh")
        exists, sha1_ok, new_cache = mesh_precheck(
            mesh, cache=self._precheck.get(rid))
        self._precheck.put(rid, new_cache)
        painter_state = self._build_state(
            request, now, exists, sha1_ok, rid)
        decision = decide_approval(request, painter_state, self._settings)
        action = decision.action
        reason = decision.reason
        current = entry.get("state")
        if action == "reject":
            new_state, new_reason = rebase_reject_state(reason, current)
            self._finish_request(
                root, rid, new_state, reason=new_reason,
                message=rebase_failure_message(new_reason),
            )
            if self._dialog_rid == rid:
                self._close_dialog()
            return
        if action == "wait":
            self._note_wait(rid, reason, now, root, current)
            return
        self._last_wait_reason.pop(rid, None)
        entry.pop("first_wait_at", None)
        if action == "ask":
            if self._dialog is not None and self._dialog_rid == rid:
                self._refresh_open_dialog(root, rid, request, painter_state, now)
                return
            if current != "awaiting_approval":
                self._write_once(root, rid, "awaiting_approval", now)
                entry["state"] = "awaiting_approval"
            self._open_dialog(request, painter_state)
            return
        if action == "run":
            self._start_job(request, approved=True)

    def _refresh_open_dialog(self, root, rid, request, painter_state, now):
        if bool(painter_state.get("cancelled")):
            self._finish_request(
                root, rid, "declined", reason="cancelled",
                message=rebase_failure_message("cancelled"),
            )
            self._close_dialog()
            return
        if bool(painter_state.get("superseded")):
            self._finish_request(
                root, rid, "superseded", reason="superseded",
                message=rebase_failure_message("superseded"),
            )
            self._close_dialog()
            return
        expires_at = request.get("expires_at")
        if _rebase_is_finite_number(expires_at) and now > expires_at:
            self._finish_request(
                root, rid, "expired", reason="expired",
                message=rebase_failure_message("expired"),
            )
            self._close_dialog()
            return
        rounded = self._settings or {}
        if rounded.get("rebase_enabled") is False:
            self._finish_request(
                root, rid, "failed", reason="disabled",
                message=rebase_failure_message("disabled"),
            )
            self._close_dialog()

    def _note_wait(self, rid, reason, now, root, current):
        entry = self._entries.get(rid) or {}
        if entry.get("first_wait_at") is None:
            entry["first_wait_at"] = now
            self._entries[rid] = entry
        prev_reason = self._last_wait_reason.get(rid)
        if prev_reason != reason:
            self._last_wait_reason[rid] = reason
            self._emit("rebase 待機: {0}".format(reason))
            if current == "received":
                self._write_once(
                    root,
                    rid,
                    "received",
                    now,
                    reason=reason,
                    message=rebase_wait_message(reason),
                )
        if wait_timed_out(entry.get("first_wait_at"), now, self._settings):
            if current in ("received", "awaiting_approval"):
                self._finish_request(
                    root, rid, "expired", reason="expired",
                    message=rebase_failure_message("expired"),
                )
                if self._dialog_rid == rid:
                    self._close_dialog()

    def _newest_nonterminal(self):
        best = None
        best_created = None
        for rid, entry in self._entries.items():
            if entry.get("state") in _REBASE_TERMINAL:
                continue
            request = entry.get("request")
            if not request:
                continue
            created = request.get("created_at")
            if best is None or (
                _rebase_is_finite_number(created)
                and (best_created is None or created > best_created)
            ):
                best = (rid, entry)
                best_created = created
        return best

    def _build_state(self, request, now, mesh_exists, mesh_sha1_ok, rid):
        snap = {}
        if self.api is not None and callable(getattr(self.api, "snapshot", None)):
            try:
                snap = self.api.snapshot() or {}
            except Exception:
                snap = {}
        is_open = snap.get("is_open")
        if is_open is None:
            is_open = False
        in_edition = snap.get("in_edition")
        if in_edition is None:
            in_edition = False
        project_key = snap.get("project_key")
        saved_path = snap.get("saved_path")
        busy = snap.get("busy")
        if busy is None:
            busy = True
        exporting = snap.get("exporting")
        if exporting is None:
            exporting = True
        if snap.get("needs_saving_missing") and not self._needs_saving_missing_logged:
            self._needs_saving_missing_logged = True
            if effective_rebase_approval(self._settings) == "auto":
                self._log(
                    "warning",
                    "project.needs_saving が読めないため、auto では未保存として待ちます。要実機確認。",
                )
            else:
                self._log(
                    "warning",
                    "project.needs_saving が読めないため、未保存判定は偽です。要実機確認。",
                )
        needs_saving = needs_saving_for_rebase(snap, self._settings)
        can_disable = snap.get("can_disable_auto_unwrap")
        if can_disable is None:
            can_disable = False
        last_edit_at = snap.get("last_edit_at")
        if not mesh_exists:
            mesh_sha1_ok = None
        return build_painter_state(
            now=now,
            is_open=is_open,
            in_edition=in_edition,
            project_key=project_key,
            saved_path=saved_path,
            busy=busy,
            exporting=exporting,
            needs_saving=needs_saving,
            backup_includes_unsaved=BACKUP_INCLUDES_UNSAVED,
            mesh_exists=mesh_exists,
            mesh_sha1_ok=mesh_sha1_ok,
            superseded=rid in self._superseded,
            cancelled=rid in self._cancelled,
            can_disable_auto_unwrap=can_disable,
            last_edit_at=last_edit_at,
        )

    def _write_once(self, root, rid, state, now, reason=None, message=""):
        entry = self._entries.get(rid) or {}
        if entry.get("state") == state and entry.get("reason") == reason:
            return False
        status = build_rebase_status(
            rid, state, now, __version__, reason=reason, message=message)
        ok = write_rebase_status(root, status)
        if not ok:
            self._log("warning", "rebase: 状態 {0} の書き込みに失敗しました".format(state))
        entry["state"] = state
        entry["reason"] = reason
        self._entries[rid] = entry
        if state in _REBASE_TERMINAL:
            self._precheck.drop(rid)
        self._emit("rebase {0}: {1}".format(rid, state))
        return ok

    def _finish_request(self, root, rid, state, reason=None, message=""):
        now = self._now()
        self._write_once(root, rid, state, now, reason=reason, message=message)
        self._precheck.drop_if_terminal(rid, state)

    def _open_dialog(self, request, painter_state):
        if not callable(self.dialog_factory):
            return
        rid = request.get("request_id")
        backup_dir = REBASE_BACKUP_DIRNAME
        try:
            spp = self.api.project_file_path() if self.api is not None else None
            _full, _name, backup_dir = rebase_backup_path(
                spp, rid, self.api.file_exists if self.api is not None else os.path.exists)
        except Exception:
            backup_dir = REBASE_BACKUP_DIRNAME
        uv_fill_reproject = bool(
            (self._settings or {}).get("rebase_reproject_uv_fills")
        ) and get_uv_old_index(request) is not None
        has_direction_fills = False
        # 要実機確認: レイヤー数百のプロジェクトで、この直前 1 回だけ行う
        # 全スタック列挙にかかる時間を確認する。失敗してもダイアログは出す。
        try:
            ctx = self.api.uv_fill_context()
            fill_api = ctx.get("api") if isinstance(ctx, dict) else ctx[0]
            texture_sets = (
                ctx.get("texture_sets") or []
                if isinstance(ctx, dict)
                else (ctx[1] or [] if len(ctx) > 1 else [])
            )
            direction_count, _direction_names = inspect_direction_uv_fills(
                fill_api, texture_sets)
            has_direction_fills = direction_count >= 1
        except Exception:
            has_direction_fills = False
        text = build_rebase_approval_text(
            request,
            backup_dir=backup_dir,
            needs_saving=bool(painter_state.get("needs_saving")),
            can_disable_auto_unwrap=bool(
                painter_state.get("can_disable_auto_unwrap")),
            backup_includes_unsaved=BACKUP_INCLUDES_UNSAVED,
            uv_fill_reproject=uv_fill_reproject,
            has_direction_fills=has_direction_fills,
        )
        self._dialog_action = None
        self._dialog_rid = rid
        self._dialog = self.dialog_factory(text, self._on_dialog_result)

    def _on_dialog_result(self, action):
        if self._is_closed():
            return
        self._dialog_action = action

    def _close_dialog(self):
        dialog = self._dialog
        self._dialog = None
        self._dialog_rid = None
        if dialog is None:
            return
        for name in ("disconnect_all", "disconnect"):
            fn = getattr(dialog, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        for name in ("close", "deleteLater"):
            fn = getattr(dialog, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    def _start_job(self, request, approved=True):
        if self._job is not None:
            return
        if not callable(self.job_factory):
            return
        rid = request.get("request_id")
        reporter = RebaseReporter(
            root=self._root,
            request_id=rid,
            clock=self.clock,
            log_fn=self._log,
            emit_status_fn=self._emit,
            shutting_down_fn=self._is_closed,
            plugin_version=__version__,
        )
        cache = self._precheck.get(rid)
        job = self.job_factory(
            request, reporter, self.api, self._settings,
            clock=self.clock, approved=approved,
        )
        if cache is not None and hasattr(job, "_precheck_cache"):
            job._precheck_cache = cache
        self._job = job
        self._job_request_id = rid
        self._set_blocks(True)
        entry = self._entries.get(rid) or {}
        entry["state"] = "preparing"
        self._entries[rid] = entry
        self._emit("rebase ジョブ開始: {0}".format(rid))
        job.run()

    def shutdown(self):
        self._shutting_down = True
        self._close_dialog()
        if self._job is not None and not self._job.finished:
            try:
                self._job.abort(
                    "interrupted",
                    rebase_failure_message(
                        "interrupted", backup_name=getattr(self._job, "backup_name", None)),
                )
            except Exception:
                pass
        self._job = None
        self._job_request_id = None
        self.api = None
        self._precheck.clear()
        self._entries.clear()
        self._set_blocks(False)

    def on_project_closing(self):
        self._close_dialog()
        if self._job is not None and not self._job.finished:
            try:
                self._job.abort(
                    "interrupted",
                    rebase_failure_message(
                        "interrupted", backup_name=getattr(self._job, "backup_name", None)),
                )
            except Exception:
                pass
            self._on_job_finished()
        self._precheck.clear()
        self._prev_scan = None
        self._entries.clear()
        self._cancelled.clear()
        self._superseded.clear()
        self._recover_key = None
        self._last_wait_reason.clear()


def _iter_status_ids(root):
    directory = rebase_status_dir(root)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue
        rid = name[:-5]
        if is_valid_rebase_request_id(rid):
            yield rid
