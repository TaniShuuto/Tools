"""リベースが使う Painter API、承認ダイアログ、エンジン用フック、
UV フィル API の組み立て。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os
import time

import substance_painter.project as project
import substance_painter.textureset as textureset
import substance_painter.ui as ui

from ._qt import QtCore, QtWidgets
from ._base import (
    _append_history,
    _is_unsaved_project_key,
    _log,
    _update_nested_config_entry,
    build_mesh_record,
    load_config,
)
from ._project_key import _current_project_key, _project_subfolder_name
from ._rebase_policy import (
    build_painter_state,
    decide_approval,
    mesh_precheck,
    needs_saving_for_rebase,
    supports_auto_unwrap_settings,
)
from ._rebase_protocol import (
    ApprovalDecision,
    BACKUP_INCLUDES_UNSAVED,
    REBASE_BUTTON_CANCEL,
    REBASE_BUTTON_RUN,
    REBASE_DIALOG_TITLE,
    _PAINTER_STATE_DEFAULTS,
    effective_rebase_approval,
)
from ._rebase_uvfill import UvFillApi, _texture_set_name
from ._rebase_job import ExportAttempt, RebaseJob

__all__ = [
    "make_uv_fill_api",
    "layerstack",
    "RebaseEngineHooks",
    "PainterRebaseApi",
    "approval_action_from_role",
    "RebaseApprovalDialogHost",
    "make_rebase_approval_dialog",
    "default_rebase_job_factory",
]


def make_uv_fill_api(layerstack_module):
    """必須 3 点が揃わなければ None。未確認の名前は getattr で守る(要実機確認)。"""
    if layerstack_module is None:
        return None
    get_roots = getattr(layerstack_module, "get_root_layer_nodes", None)
    mode = getattr(layerstack_module, "ProjectionMode", None)
    params_cls = getattr(layerstack_module, "UVSetToUVSetProjectionParams", None)
    if not callable(get_roots) or mode is None or params_cls is None:
        return None
    if not hasattr(mode, "UV") or not hasattr(mode, "UVSetToUVSet"):
        return None
    # 要実機確認: Painter 12.1.5 の列挙名は ChannelType.Normal /
    # ChannelType.AnisotropyAngle を想定する。Normal を取得できない場合は
    # 安全側で「方向チャンネル不明」として、チャンネルを持つフィルを数える。
    direction_types = None
    try:
        channel_type = getattr(layerstack_module, "ChannelType", None)
        if channel_type is None:
            channel_type = getattr(textureset, "ChannelType")
        normal = getattr(channel_type, "Normal")
        direction_types = [normal]
        anisotropy = getattr(channel_type, "AnisotropyAngle", None)
        if anisotropy is not None:
            direction_types.append(anisotropy)
        direction_types = tuple(direction_types)
    except Exception:
        direction_types = None
    return UvFillApi(get_roots, mode, params_cls, direction_types)


# ---------------------------------------------------------------------------
# 2026.10.19(1.4.0 PB-b): Painter API と確認ダイアログ(engine 依存)
# ---------------------------------------------------------------------------
try:
    import substance_painter.layerstack as layerstack
except Exception:
    layerstack = None


class RebaseEngineHooks(object):
    def __init__(self, engine):
        self.engine = engine

    def is_shutting_down(self):
        return bool(getattr(self.engine, "_shutting_down", False))

    def emit_status(self, text):
        if self.is_shutting_down():
            _log("info", text)
            try:
                _append_history("SP", text)
            except Exception:
                pass
            return
        self.engine._emit_status(text)

    def log(self, level, text):
        _log(level, text)

    def set_blocks_exports(self, value):
        self.engine._set_rebase_blocks_exports(value)

    def resume_blocked_exports(self):
        self.engine._resume_blocked_exports()

    def load_config(self):
        return load_config()

    def current_project_key(self):
        return _current_project_key()

    def project_subfolder(self):
        return _project_subfolder_name()


class PainterRebaseApi(object):
    def __init__(self, engine):
        self.engine = engine
        self.closed = False
        self._can_disable = None

    def _alive(self):
        return not self.closed and self.engine is not None and not getattr(
            self.engine, "_shutting_down", False)

    def now(self):
        return time.time()

    def project_open(self):
        if not self._alive():
            return False
        try:
            return bool(project.is_open())
        except Exception:
            return False

    def current_project_key(self):
        if not self._alive():
            return None
        try:
            return _current_project_key()
        except Exception:
            return None

    def snapshot(self):
        defaults = dict(_PAINTER_STATE_DEFAULTS)
        out = {
            "is_open": False,
            "in_edition": False,
            "project_key": defaults.get("project_key"),
            "saved_path": defaults.get("saved_path"),
            "busy": defaults.get("busy"),
            "exporting": defaults.get("exporting"),
            "needs_saving": defaults.get("needs_saving"),
            "needs_saving_missing": False,
            "can_disable_auto_unwrap": defaults.get("can_disable_auto_unwrap"),
            "last_edit_at": None,
        }
        if not self._alive():
            return out
        try:
            out["is_open"] = bool(project.is_open())
        except Exception:
            out["is_open"] = False
        try:
            fn = getattr(project, "is_in_edition_state", None)
            out["in_edition"] = bool(fn()) if callable(fn) else False
        except Exception:
            out["in_edition"] = False
        try:
            out["project_key"] = _current_project_key()
        except Exception:
            out["project_key"] = None
        if _is_unsaved_project_key(out["project_key"]):
            out["saved_path"] = None
        else:
            try:
                out["saved_path"] = project.file_path()
            except Exception:
                out["saved_path"] = None
        try:
            out["busy"] = bool(project.is_busy())
        except Exception:
            out["busy"] = True
        out["exporting"] = bool(getattr(self.engine, "exporting", True))
        needs_fn = getattr(project, "needs_saving", None)
        if not callable(needs_fn):
            out["needs_saving"] = False
            out["needs_saving_missing"] = True
        else:
            try:
                out["needs_saving"] = bool(needs_fn())
            except Exception:
                out["needs_saving"] = False
                out["needs_saving_missing"] = True
        if self._can_disable is None:
            self._can_disable = supports_auto_unwrap_settings(
                getattr(project, "MeshReloadingSettings", None))
        out["can_disable_auto_unwrap"] = bool(self._can_disable)
        out["last_edit_at"] = getattr(self.engine, "_last_texture_event_at", None)
        return out

    def recheck(self, request):
        if not self._alive():
            return ApprovalDecision("wait", "busy")
        snap = self.snapshot()
        mesh = request.get("mesh") if isinstance(request, dict) else None
        cache = None
        coord = getattr(self.engine, "_rebase", None)
        rid = request.get("request_id") if isinstance(request, dict) else None
        if coord is not None and rid:
            cache = coord._precheck.get(rid)
        exists, sha1_ok, new_cache = mesh_precheck(mesh, cache=cache)
        if coord is not None and rid:
            coord._precheck.put(rid, new_cache)
        cancelled = False
        superseded = False
        if coord is not None and rid:
            cancelled = rid in coord._cancelled
            superseded = rid in coord._superseded
        if not exists:
            sha1_ok = None
        settings = None
        if coord is not None:
            settings = coord._settings
        if snap.get("needs_saving_missing") and self.engine is not None:
            if not getattr(self.engine, "_needs_saving_missing_logged", False):
                self.engine._needs_saving_missing_logged = True
                if effective_rebase_approval(settings) == "auto":
                    _log(
                        "warning",
                        "project.needs_saving が読めないため、auto では未保存として待ちます。要実機確認。",
                    )
                else:
                    _log(
                        "warning",
                        "project.needs_saving が読めないため、未保存判定は偽です。要実機確認。",
                    )
        painter_state = build_painter_state(
            now=self.now() if not callable(getattr(self.engine, "_rebase", None)) else (
                self.engine._rebase.clock() if self.engine._rebase is not None else self.now()
            ),
            is_open=snap.get("is_open"),
            in_edition=snap.get("in_edition"),
            project_key=snap.get("project_key"),
            saved_path=snap.get("saved_path"),
            busy=snap.get("busy"),
            exporting=snap.get("exporting"),
            needs_saving=needs_saving_for_rebase(snap, settings),
            backup_includes_unsaved=BACKUP_INCLUDES_UNSAVED,
            mesh_exists=exists,
            mesh_sha1_ok=sha1_ok,
            superseded=superseded,
            cancelled=cancelled,
            can_disable_auto_unwrap=snap.get("can_disable_auto_unwrap"),
            last_edit_at=snap.get("last_edit_at"),
        )
        return decide_approval(request, painter_state, settings)

    def project_file_path(self):
        if not self._alive():
            return None
        try:
            return project.file_path()
        except Exception:
            return None

    def is_busy(self):
        if not self._alive():
            return True
        try:
            return bool(project.is_busy())
        except Exception:
            return True

    def file_exists(self, path):
        try:
            return os.path.isfile(path)
        except OSError:
            return False

    def file_size(self, path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def make_dirs(self, path):
        if not self._alive():
            return
        os.makedirs(path, exist_ok=True)

    def save_as_copy(self, path):
        if not self._alive():
            raise RuntimeError("closed")
        mode = getattr(project, "ProjectSaveMode", None)
        full = getattr(mode, "Full", None) if mode is not None else None
        if full is None:
            raise RuntimeError("ProjectSaveMode.Full がありません")
        project.save_as_copy(path, full)

    def supports_auto_unwrap(self):
        if self._can_disable is None:
            self._can_disable = supports_auto_unwrap_settings(
                getattr(project, "MeshReloadingSettings", None))
        return bool(self._can_disable)

    def make_reload_settings(self, **kwargs):
        if not self._alive():
            raise RuntimeError("closed")
        cls = getattr(project, "MeshReloadingSettings", None)
        if cls is None:
            raise RuntimeError("MeshReloadingSettings がありません")
        return cls(**kwargs)

    def reload_mesh(self, path, settings, callback):
        if not self._alive():
            return
        project.reload_mesh(path, settings, callback)

    def uv_fill_context(self):
        if not self._alive():
            return {"api": None, "texture_sets": [], "ts_names": []}
        fill_api = make_uv_fill_api(layerstack)
        sets = []
        names = []
        try:
            sets = list(textureset.all_texture_sets())
        except Exception:
            sets = []
        for ts in sets:
            name = _texture_set_name(ts)
            if name:
                names.append(name)
        return {"api": fill_api, "texture_sets": sets, "ts_names": names}

    def run_final_export(self, request_id):
        if not self._alive():
            return ExportAttempt(executed=False, message="closed")
        return self.engine.run_rebase_final_export(request_id)

    def last_imported_mesh_path(self):
        if not self._alive():
            return None
        getter = getattr(project, "last_imported_mesh_path", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def record_painter_mesh(self, request):
        if not self._alive():
            return False
        key = _current_project_key()
        if _is_unsaved_project_key(key):
            return False
        path = self.last_imported_mesh_path()
        mesh = request.get("mesh") if isinstance(request, dict) else None
        if not isinstance(path, str) or path == "":
            if isinstance(mesh, dict):
                path = mesh.get("path")
        if not isinstance(path, str) or path == "":
            return False
        prev = None
        if isinstance(mesh, dict) and mesh.get("path") == path:
            prev = {
                "path": path,
                "size": mesh.get("size"),
                "mtime_ns": mesh.get("mtime_ns"),
                "sha1": mesh.get("sha1"),
            }
        else:
            by_project = self.engine.config.get("painter_mesh_by_project")
            if isinstance(by_project, dict):
                prev = by_project.get(key)
        try:
            record = build_mesh_record(path, prev, time.time())
        except Exception:
            return False
        if record is None:
            return False
        written = _update_nested_config_entry("painter_mesh_by_project", key, record)
        if written is None:
            return False
        self.engine.config["painter_mesh_by_project"] = written
        return True


def approval_action_from_role(role, accept_role):
    """ボタンの役割から action を決める。AcceptRole のときだけ "run"。

    None、RejectRole、未知の値、比較の例外は "cancel"。
    ボタンのラッパー同士の同一性や、ラベルの文字列では比べない。
    AcceptRole は 0 なので、値の真偽では判定しない。
    """
    if role is None:
        return "cancel"
    try:
        matched = role == accept_role
    except Exception:
        return "cancel"
    if matched:
        return "run"
    return "cancel"


class RebaseApprovalDialogHost(object):
    """非モーダルの確認ダイアログ。シグナルはメソッドへつなぎ、shutdown で切る。"""

    def __init__(self, text, on_result, default_cancel=False):
        self._on_result = on_result
        self._closed = False
        parent = None
        getter = getattr(ui, "get_main_window", None)
        if callable(getter):
            try:
                parent = getter()
            except Exception:
                parent = None
        # 親ラッパーを保持しないと、PySide が子ボタンを無効にする(12.1.5)。
        self._parent = parent
        if parent is not None:
            self.box = QtWidgets.QMessageBox(parent)
        else:
            self.box = QtWidgets.QMessageBox()
        self.box.setWindowTitle(REBASE_DIALOG_TITLE)
        self.box.setText(text)
        self._run_btn = self.box.addButton(
            REBASE_BUTTON_RUN, QtWidgets.QMessageBox.AcceptRole)
        self._cancel_btn = self.box.addButton(
            REBASE_BUTTON_CANCEL, QtWidgets.QMessageBox.RejectRole)
        self.box.setDefaultButton(
            self._cancel_btn if default_cancel else self._run_btn)
        self.box.setEscapeButton(self._cancel_btn)
        self.box.finished.connect(self._on_finished)
        # 既定は ApplicationModal。仕様は出したまま操作できる非モーダル。
        self.box.setWindowModality(QtCore.Qt.NonModal)
        self.box.show()

    def _on_finished(self, _result=None):
        if self._closed:
            return
        action = "cancel"
        try:
            clicked = self.box.clickedButton()
            if clicked is not None:
                action = approval_action_from_role(
                    self.box.buttonRole(clicked),
                    QtWidgets.QMessageBox.AcceptRole,
                )
        except Exception:
            action = "cancel"
        if callable(self._on_result):
            self._on_result(action)

    def disconnect_all(self):
        try:
            self.box.finished.disconnect(self._on_finished)
        except Exception:
            pass

    def close(self):
        # finished より先。終了処理の close を declined として扱わない。
        self._closed = True
        try:
            self.box.close()
        except Exception:
            pass
        self._parent = None

    def deleteLater(self):
        fn = getattr(self.box, "deleteLater", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
        self._parent = None


def make_rebase_approval_dialog(text, on_result, default_cancel=False):
    return RebaseApprovalDialogHost(
        text, on_result, default_cancel=default_cancel)


def default_rebase_job_factory(request, reporter, api, settings, clock=None, approved=True):
    return RebaseJob(
        request, reporter, api, settings,
        clock=clock or time.time, approved=approved,
    )
