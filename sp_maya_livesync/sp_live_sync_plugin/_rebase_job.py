"""リベース 1 件ぶんの実行(ExportAttempt / RebaseReporter / RebaseJob)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os
import time

import substance_painter.exception as sp_exception

from ._base import _append_history
from ._rebase_policy import (
    build_reload_settings_kwargs,
    classify_reload_status,
    rebase_backup_path,
    rebase_retry_delay,
)
from ._rebase_protocol import (
    REBASE_AUTO_UNWRAP_UNSUPPORTED_LOG,
    REBASE_DIRECTION_WARNING_FMT,
    REBASE_DONE_GUIDANCE,
    REBASE_TERMINAL_WRITE_MAX,
    REBASE_UV_FILL_PROGRESS,
    _REBASE_TERMINAL,
    _rebase_clip_message,
    build_rebase_status,
    get_uv_old_index,
    normalize_rebase_settings,
    rebase_failure_message,
    rebase_reject_state,
    write_rebase_status,
)
from ._rebase_uvfill import UvFillSummary, switch_uv_fills_for_project

__all__ = [
    "ExportAttempt",
    "RebaseReporter",
    "RebaseJob",
    "_is_project_error",
]


class ExportAttempt(object):
    def __init__(
        self,
        executed=False,
        ok=False,
        status=None,
        export_id=None,
        message="",
        locked_error=None,
        error=None,
    ):
        self.executed = bool(executed)
        self.ok = bool(ok)
        self.status = status
        self.export_id = export_id
        self.message = message if isinstance(message, str) else "{0}".format(message)
        self.locked_error = locked_error
        self.error = error


class RebaseReporter(object):
    def __init__(
        self,
        root,
        request_id,
        clock,
        log_fn,
        emit_status_fn,
        shutting_down_fn,
        plugin_version,
    ):
        self.root = root
        self.request_id = request_id
        self.clock = clock
        self._log_fn = log_fn
        self._emit_status_fn = emit_status_fn
        self._shutting_down_fn = shutting_down_fn
        self.plugin_version = plugin_version
        self.last_state = None
        self._pending_terminal = None
        self._terminal_tries = 0
        self.outcome = None

    def _shutting_down(self):
        if not callable(self._shutting_down_fn):
            return False
        try:
            return bool(self._shutting_down_fn())
        except Exception:
            return False

    def log(self, text):
        line = "{0}".format(text)
        if callable(self._log_fn):
            try:
                self._log_fn("info", line)
            except Exception:
                pass
        if self._shutting_down():
            try:
                _append_history("SP", line)
            except Exception:
                pass
            return
        if callable(self._emit_status_fn):
            try:
                self._emit_status_fn(line)
            except Exception:
                pass

    def state(self, name, **fields):
        try:
            status = build_rebase_status(
                self.request_id,
                name,
                self.clock(),
                self.plugin_version,
                reason=fields.get("reason"),
                message=fields.get("message", ""),
                backup_name=fields.get("backup_name"),
                result_export_id=fields.get("result_export_id"),
                uv_fills=fields.get("uv_fills"),
            )
        except ValueError as exc:
            self.log("warning: rebase 状態を組み立てられませんでした: {0}".format(exc))
            return False
        ok = write_rebase_status(self.root, status)
        if name in _REBASE_TERMINAL:
            if ok:
                self._pending_terminal = None
                self.last_state = name
                return True
            self._pending_terminal = status
            self._terminal_tries += 1
            if self._terminal_tries >= REBASE_TERMINAL_WRITE_MAX:
                self.log("error: 終端状態の書き込みを {0} 回失敗しました".format(
                    REBASE_TERMINAL_WRITE_MAX))
            else:
                self.log("warning: 終端状態の書き込みに失敗しました。再試行します")
            return False
        if not ok:
            self.log("warning: 状態ファイルの書き込みに失敗しました({0})".format(name))
            return False
        self.last_state = name
        return True

    def retry_terminal(self):
        if self._pending_terminal is None:
            return True
        if self._terminal_tries >= REBASE_TERMINAL_WRITE_MAX:
            return False
        ok = write_rebase_status(self.root, self._pending_terminal)
        self._terminal_tries += 1
        if ok:
            self.last_state = self._pending_terminal.get("state")
            self._pending_terminal = None
            return True
        if self._terminal_tries >= REBASE_TERMINAL_WRITE_MAX:
            self.log("error: 終端状態の書き込みを {0} 回失敗しました".format(
                REBASE_TERMINAL_WRITE_MAX))
        return False

    def finished(self, outcome):
        self.outcome = outcome


class RebaseJob(object):
    def __init__(self, request, reporter, api, settings, *, clock=time.time, approved=True):
        self.request = request if isinstance(request, dict) else {}
        self.reporter = reporter
        self.api = api
        self.settings = normalize_rebase_settings(settings)
        self.clock = clock
        self.approved = bool(approved)
        self.state = None
        self.finished = False
        self.outcome = None
        self.uv_fills = None
        self.backup_name = None
        self._reload_status = None
        self._reload_started_at = None
        self._reload_busy_since = None
        self._reload_called = False
        self._export_started_at = None
        self._next_action_at = None
        self._retry_attempt = 0
        self._retry_stage = None
        self._stage_entered_at = None
        self._auto_unwrap_logged = False
        self._cancel_ignored = False

    def _now(self, now=None):
        if now is not None:
            return now
        if callable(self.clock):
            return self.clock()
        return time.time()

    def _request_id(self):
        return self.request.get("request_id")

    def _write(self, name, **fields):
        if self.backup_name and "backup_name" not in fields:
            fields["backup_name"] = self.backup_name
        if self.uv_fills is not None and "uv_fills" not in fields:
            try:
                fields["uv_fills"] = self.uv_fills.to_status_dict()
            except Exception:
                pass
        self.reporter.state(name, **fields)
        self.state = name

    def _fail(self, reason, message=None, detail=None):
        if self.finished:
            return
        text = message
        if text is None:
            text = rebase_failure_message(
                reason, backup_name=self.backup_name, detail=detail)
        self._write("failed", reason=reason, message=text)
        self.finished = True
        self.outcome = "failed"
        self.reporter.finished("failed")

    def _done(self, result_export_id):
        uv_dict = None
        extra = ""
        if self.uv_fills is not None:
            try:
                uv_dict = self.uv_fills.to_status_dict()
                extra = self.uv_fills.to_message()
            except Exception:
                uv_dict = None
        # 案内文がすでに「リベースが完了しました。」で始まる。先頭には重ねない。
        parts = [REBASE_DONE_GUIDANCE]
        if extra:
            parts.append(extra)
        text = _rebase_clip_message(" ".join(part for part in parts if part))
        self._write(
            "done",
            message=text,
            result_export_id=result_export_id,
            uv_fills=uv_dict,
            backup_name=self.backup_name,
        )
        self.reporter.log(REBASE_DONE_GUIDANCE)
        if self.uv_fills is not None and self.uv_fills.direction_fills >= 1:
            names = ", ".join(self.uv_fills.direction_fill_names) or "名前不明"
            self.reporter.log(REBASE_DIRECTION_WARNING_FMT.format(
                n=self.uv_fills.direction_fills, names=names))
        self.finished = True
        self.outcome = "done"
        self.reporter.finished("done")

    def _end_reject(self, reason, prev_state):
        new_state, new_reason = rebase_reject_state(reason, prev_state)
        text = rebase_failure_message(new_reason, backup_name=self.backup_name)
        self._write(new_state, reason=new_reason, message=text)
        self.finished = True
        self.outcome = new_state
        self.reporter.finished(new_state)

    def run(self):
        self.state = "preparing"
        self._stage_entered_at = self._now()
        self._write("preparing")

    def on_reload_status(self, status):
        if self.finished:
            return
        if self.api is not None and getattr(self.api, "closed", False):
            return
        if self._reload_status is not None:
            self.reporter.log("reload_mesh の追加のコールバックを無視します")
            return
        self._reload_status = status

    def abort(self, reason, message):
        if self.finished:
            return
        text = message
        if not text:
            text = rebase_failure_message(reason, backup_name=self.backup_name)
        self._write("failed", reason=reason, message=text)
        self.finished = True
        self.outcome = "failed"
        self.reporter.finished("failed")

    def ignore_cancel(self):
        self._cancel_ignored = True

    def tick(self, now=None):
        try:
            self._tick_impl(now)
        except Exception as exc:
            try:
                self.reporter.log("error: RebaseJob.tick: {0}".format(exc))
            except Exception:
                pass

    def _tick_impl(self, now):
        now = self._now(now)
        if self.finished:
            self.reporter.retry_terminal()
            return
        if self.api is not None and getattr(self.api, "closed", False):
            return
        if self._next_action_at is not None and now < self._next_action_at:
            return
        if self.state == "preparing":
            self._tick_preparing(now)
        elif self.state == "backing_up":
            self._tick_backing_up(now)
        elif self.state == "reloading":
            self._tick_reloading(now)
        elif self.state == "exporting":
            self._tick_exporting(now)

    def _tick_preparing(self, now):
        decision = self.api.recheck(self.request)
        action = getattr(decision, "action", None)
        reason = getattr(decision, "reason", None)
        if action in ("ask", "run"):
            self._retry_attempt = 0
            self._write("backing_up")
            return
        if action == "wait":
            return
        if action == "reject":
            self._end_reject(reason, "preparing")
            return

    def _schedule_retry(self, now, stage):
        self._retry_attempt += 1
        delay = rebase_retry_delay(
            self._retry_attempt, self.settings.get("rebase_retry_max"))
        if delay is None:
            return False
        self._retry_stage = stage
        self._next_action_at = now + delay
        self.reporter.log(
            "プロジェクトがロック中のため {0} 秒後に再試行します({1})".format(
                delay, self._retry_attempt)
        )
        return True

    def _tick_backing_up(self, now):
        spp_before = self.api.project_file_path()
        try:
            full, name, backup_dir = rebase_backup_path(
                spp_before, self._request_id(), self.api.file_exists)
        except ValueError:
            self._fail("backup_failed")
            return
        self.backup_name = name
        self._write("backing_up", backup_name=name)
        try:
            self.api.make_dirs(backup_dir)
            self.api.save_as_copy(full)
        except Exception as exc:
            if _is_project_error(exc):
                if not self._schedule_retry(now, "backing_up"):
                    self._fail("project_locked")
                return
            self._fail("backup_failed", detail="{0}".format(exc))
            return
        if not self.api.file_exists(full):
            self._fail("backup_failed")
            return
        size_fn = getattr(self.api, "file_size", None)
        try:
            if callable(size_fn):
                size = size_fn(full)
            else:
                size = os.path.getsize(full)
        except Exception:
            size = None
        if size is not None and size <= 0:
            self._fail("backup_failed")
            return
        spp_after = self.api.project_file_path()
        if spp_after != spp_before:
            self._fail("backup_failed")
            return
        self.reporter.log("バックアップ: {0}".format(name))
        self._retry_attempt = 0
        self._write("reloading")

    def _reload_busy_wait_exceeded(self, now, timeout_s):
        if self._reload_busy_since is None:
            self._reload_busy_since = now
            return False
        return (now - self._reload_busy_since) > timeout_s

    def _tick_reloading(self, now):
        timeout_s = self.settings.get("rebase_reload_timeout_s", 600.0)
        if not self._reload_called:
            if self.api.is_busy():
                if self._reload_busy_wait_exceeded(now, timeout_s):
                    self._fail("reload_timeout")
                return
            self._reload_busy_since = None
            try:
                supports = self.api.supports_auto_unwrap()
            except Exception:
                supports = False
            if not supports and not self._auto_unwrap_logged:
                self.reporter.log(REBASE_AUTO_UNWRAP_UNSUPPORTED_LOG)
                self._auto_unwrap_logged = True
            try:
                kwargs = build_reload_settings_kwargs(self.request, supports)
            except ValueError:
                self._fail("bad_settings")
                return
            try:
                settings_obj = self.api.make_reload_settings(**kwargs)
                mesh = self.request.get("mesh") or {}
                path = mesh.get("path")
                self.api.reload_mesh(path, settings_obj, self.on_reload_status)
            except Exception as exc:
                if _is_project_error(exc):
                    if not self._schedule_retry(now, "reloading"):
                        self._fail("project_locked")
                    return
                self._fail("reload_error", detail="{0}".format(exc))
                return
            self._reload_called = True
            self._reload_started_at = now
            self._reload_busy_since = None
            self._write("reloading")
            return
        if self._reload_status is None:
            if self._reload_started_at is not None and (now - self._reload_started_at) > timeout_s:
                self._fail("reload_timeout")
            return
        kind = classify_reload_status(self._reload_status)
        if kind == "success":
            if self.api.is_busy():
                if self._reload_busy_wait_exceeded(now, timeout_s):
                    self._fail("reload_timeout")
                return
            self._reload_busy_since = None
            self._switch_uv_fills()
            self._retry_attempt = 0
            self._write("exporting")
            return
        if kind == "error":
            self._fail("reload_error")
            return
        self._fail("reload_timeout")

    def _switch_uv_fills(self):
        self._write("reloading", message=REBASE_UV_FILL_PROGRESS)
        try:
            ctx = self.api.uv_fill_context()
        except Exception as exc:
            self.reporter.log("UV フィル切り替えをスキップしました: {0}".format(exc))
            self.uv_fills = UvFillSummary()
            self.uv_fills.gate = "no_api"
            return
        fill_api = None
        texture_sets = []
        if isinstance(ctx, dict):
            fill_api = ctx.get("api")
            texture_sets = ctx.get("texture_sets") or []
        elif isinstance(ctx, (tuple, list)) and ctx:
            fill_api = ctx[0]
            if len(ctx) > 1:
                texture_sets = ctx[1] or []
        # 空の ts_names をプロジェクト全件で埋めない。
        request_names = self.request.get("ts_names")
        try:
            self.uv_fills = switch_uv_fills_for_project(
                fill_api,
                texture_sets,
                request_names,
                enabled=bool(self.settings.get("rebase_reproject_uv_fills")),
                uv_old_index=get_uv_old_index(self.request),
                log=self.reporter.log,
            )
        except Exception as exc:
            self.reporter.log("UV フィル切り替えで例外: {0}".format(exc))
            self.uv_fills = UvFillSummary()
            self.uv_fills.gate = "no_api"

    def _tick_exporting(self, now):
        if self._export_started_at is None:
            self._export_started_at = now
            self._write("exporting")
        if not self.request.get("final_full"):
            self._finish_success(None)
            return
        timeout_s = self.settings.get("rebase_reload_timeout_s", 600.0)
        try:
            attempt = self.api.run_final_export(self._request_id())
        except Exception as exc:
            if _is_project_error(exc):
                if not self._schedule_retry(now, "exporting"):
                    self._fail(
                        "project_locked",
                        detail="メッシュは読み直し済み。Final の書き出しをやり直してください",
                    )
                return
            self._fail("export_failed", detail="{0}".format(exc))
            return
        if attempt is None:
            attempt = ExportAttempt(executed=False)
        if not attempt.executed:
            if (now - self._export_started_at) > timeout_s:
                self._fail("export_failed")
            return
        if attempt.locked_error is not None:
            if not self._schedule_retry(now, "exporting"):
                self._fail(
                    "project_locked",
                    detail="メッシュは読み直し済み。Final の書き出しをやり直してください",
                )
            return
        if (not attempt.ok) or attempt.error is not None:
            detail = attempt.message
            if detail and (
                "メッシュは読み直し済み" in detail
                or "やり直してください" in detail
            ):
                detail = None
            self._fail("export_failed", detail=detail)
            return
        self._finish_success(attempt.export_id)

    def _finish_success(self, export_id):
        try:
            ok = self.api.record_painter_mesh(self.request)
            if ok is False:
                self.reporter.log("warning: メッシュ記録に失敗しました。リベースは完了とします")
        except Exception as exc:
            self.reporter.log(
                "warning: メッシュ記録に失敗しました。リベースは完了とします: {0}".format(exc)
            )
        self._done(export_id)


def _is_project_error(exc):
    try:
        return isinstance(exc, sp_exception.ProjectError)
    except Exception:
        return type(exc).__name__ == "ProjectError"
