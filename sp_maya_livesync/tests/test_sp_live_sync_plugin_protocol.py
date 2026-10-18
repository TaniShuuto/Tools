# -*- coding: utf-8 -*-
"""sp_live_sync_plugin 1.3.0 の結果ファイルとメッシュ記録。

Painter と Qt は sys.modules に偽物を入れてから読み込む。
実行例:

    pytest sp_maya_livesync/tests/test_sp_live_sync_plugin_protocol.py
"""
import hashlib
import json
import os
import sys
import tempfile
import types

import pytest

_MISSING = object()
_SAVED_MODULES = {}


def _install_module(name, module):
    _SAVED_MODULES[name] = sys.modules.get(name, _MISSING)
    sys.modules[name] = module
    return module


def _restore_modules():
    for name, prev in _SAVED_MODULES.items():
        if prev is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev
    _SAVED_MODULES.clear()


class _Signal:
    def __init__(self, *args, **kwargs):
        pass

    def connect(self, *args, **kwargs):
        pass

    def emit(self, *args, **kwargs):
        pass

    def disconnect(self, *args, **kwargs):
        pass


class _QObject:
    def __init__(self, *args, **kwargs):
        pass


class _QTimer:
    def __init__(self, *args, **kwargs):
        self.timeout = _Signal()

    def setSingleShot(self, value):
        pass

    def start(self, ms=None):
        self.started_ms = ms

    def stop(self):
        pass

    @staticmethod
    def singleShot(*args, **kwargs):
        pass


class _Dispatcher:
    def connect_strong(self, *args, **kwargs):
        pass

    def disconnect(self, *args, **kwargs):
        pass


class _ExportStatus:
    Success = "Success"
    Warning = "Warning"
    Error = "Error"
    Cancelled = "Cancelled"


class ProjectError(Exception):
    pass


def _noop(*args, **kwargs):
    pass


def _install_stubs():
    sp = _install_module("substance_painter", types.ModuleType("substance_painter"))

    event = _install_module("substance_painter.event", types.ModuleType("substance_painter.event"))
    event.DISPATCHER = _Dispatcher()
    event.TextureStateEvent = type("TextureStateEvent", (), {})
    event.ProjectSaved = type("ProjectSaved", (), {})
    event.ProjectAboutToClose = type("ProjectAboutToClose", (), {})
    event.ProjectEditionEntered = type("ProjectEditionEntered", (), {})
    event.TextureStateEventAction = types.SimpleNamespace(ADD="add", UPDATE="update")
    sp.event = event

    export = _install_module("substance_painter.export", types.ModuleType("substance_painter.export"))
    export.ExportStatus = _ExportStatus
    export.export_project_textures = _noop
    sp.export = export

    project = _install_module("substance_painter.project", types.ModuleType("substance_painter.project"))
    project.is_open = lambda: False
    project.is_busy = lambda: False
    project.name = lambda: None
    project.file_path = lambda: None
    project.execute_when_not_busy = lambda callback: None
    sp.project = project

    textureset = _install_module(
        "substance_painter.textureset", types.ModuleType("substance_painter.textureset"))
    textureset.all_texture_sets = lambda: []

    class _Stack:
        def __init__(self, stack_id):
            self.stack_id = stack_id

    textureset.Stack = _Stack
    sp.textureset = textureset

    logging = _install_module("substance_painter.logging", types.ModuleType("substance_painter.logging"))
    logging.info = _noop
    logging.warning = _noop
    logging.error = _noop
    sp.logging = logging

    ui = _install_module("substance_painter.ui", types.ModuleType("substance_painter.ui"))
    ui.add_dock_widget = _noop
    ui.delete_ui_element = _noop
    sp.ui = ui

    exception = _install_module(
        "substance_painter.exception", types.ModuleType("substance_painter.exception"))
    exception.ProjectError = ProjectError
    sp.exception = exception

    qtcore = _install_module("PySide2.QtCore", types.ModuleType("PySide2.QtCore"))
    qtcore.QObject = _QObject
    qtcore.Signal = _Signal
    qtcore.QTimer = _QTimer
    qtcore.Qt = types.SimpleNamespace(TextSelectableByMouse=0, ToolButtonTextOnly=0)

    qtwidgets = _install_module("PySide2.QtWidgets", types.ModuleType("PySide2.QtWidgets"))
    for name in (
        "QWizard", "QWidget", "QWizardPage", "QVBoxLayout", "QLabel", "QFormLayout",
        "QLineEdit", "QHBoxLayout", "QPushButton", "QFileDialog", "QComboBox",
        "QDoubleSpinBox", "QMessageBox", "QCheckBox", "QGroupBox", "QToolButton",
        "QMenu", "QListWidget", "QPlainTextEdit", "QDialog",
    ):
        setattr(qtwidgets, name, type(name, (), {}))
    qtwidgets.QMessageBox.Yes = 1
    qtwidgets.QMessageBox.No = 2
    qtwidgets.QDialog.Accepted = 1
    qtwidgets.QFormLayout.LabelRole = 0
    qtwidgets.QFormLayout.FieldRole = 1
    qtwidgets.QToolButton.InstantPopup = 0

    qtgui = _install_module("PySide2.QtGui", types.ModuleType("PySide2.QtGui"))

    pyside2 = _install_module("PySide2", types.ModuleType("PySide2"))
    pyside2.QtCore = qtcore
    pyside2.QtWidgets = qtwidgets
    pyside2.QtGui = qtgui


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 読み込み時に本番の共有フォルダを作らない。読み込んだあと環境変数は戻す
# (他のテストが後から maya_live_sync を読むときに影響しないように)。
_IMPORT_CONFIG = tempfile.mkdtemp(prefix="sp130_import_")
_PREV_CONFIG_ENV = os.environ.get("SPMAYA_CONFIG_DIR", _MISSING)
os.environ["SPMAYA_CONFIG_DIR"] = _IMPORT_CONFIG

_install_stubs()
try:
    import sp_live_sync_plugin as plugin
finally:
    if _PREV_CONFIG_ENV is _MISSING:
        os.environ.pop("SPMAYA_CONFIG_DIR", None)
    else:
        os.environ["SPMAYA_CONFIG_DIR"] = _PREV_CONFIG_ENV
    _restore_modules()


class _NamedStatus:
    def __init__(self, name):
        self.name = name


def _result(**overrides):
    values = {
        "plugin_version": "1.3.0",
        "export_id": "a" * 32,
        "kind": "final",
        "force_full": False,
        "status": "success",
        "message": "ok",
        "moved_count": 1,
        "skipped_unchanged_count": 0,
        "texture_sets": ["Body"],
        "flag_written": True,
        "started_at": 10.0,
        "finished_at": 12.5,
    }
    values.update(overrides)
    return plugin.build_export_result(**values)


def test_ok_is_true_only_for_success():
    success = _result(status=_NamedStatus("Success"))
    assert success["ok"] is True
    assert success["status"] == "success"
    cases = [
        (_NamedStatus("Warning"), "warning"),
        (_NamedStatus("Error"), "error"),
        (_NamedStatus("Cancelled"), "cancelled"),
        ("exception", "exception"),
    ]
    for status, expected in cases:
        body = _result(status=status)
        assert body["ok"] is False
        assert body["status"] == expected


def test_unknown_status_is_error():
    assert plugin.export_status_name(_NamedStatus("InProgress")) == "error"
    assert plugin.export_status_name(None) == "error"
    body = _result(status=_NamedStatus("InProgress"))
    assert body["status"] == "error"
    assert body["ok"] is False


def test_message_is_truncated_at_500_characters():
    body = _result(message="あ" * 600)
    assert body["message"] == "あ" * 500
    assert len(body["message"]) == 500
    assert _result(message=None)["message"] == ""
    assert _result(message="")["message"] == ""


def test_result_schema_fields():
    body = _result(texture_sets=["Zed", "Alpha", "Alpha"], request_id=None)
    assert body["schema"] == 1
    assert body["plugin_version"] == "1.3.0"
    assert body["request_id"] is None
    assert body["texture_sets"] == ["Alpha", "Zed"]
    assert body["kind"] == "final"
    assert body["force_full"] is False


def test_write_json_atomic_replaces_and_leaves_no_temp(tmp_path):
    path = tmp_path / "out.json"
    path.write_text('{"old": true}', encoding="utf-8")
    plugin.write_json_atomic(str(path), {"schema": 1, "ok": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema": 1, "ok": True}
    assert not (tmp_path / "out.json.tmp").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


def test_write_json_atomic_removes_temp_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "out.json"
    path.write_text("keep", encoding="utf-8")

    def _fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(plugin.os, "replace", _fail_replace)
    with pytest.raises(OSError):
        plugin.write_json_atomic(str(path), {"schema": 1})
    assert path.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "out.json.tmp").exists()


def test_mesh_record_hashes_then_reuses_when_size_and_mtime_match(tmp_path):
    mesh = tmp_path / "hero.fbx"
    mesh.write_bytes(b"hello-mesh")
    first = plugin.build_mesh_record(str(mesh), None, now=1000.0)
    assert first["sha1"] == hashlib.sha1(b"hello-mesh").hexdigest()
    assert len(first["sha1"]) == 40
    assert first["size"] == len(b"hello-mesh")
    assert first["plugin_version"] == "1.3.0"
    assert first["recorded_at"] == 1000.0

    st = mesh.stat()
    mesh.write_bytes(b"HELLO-MESH")
    os.utime(mesh, ns=(st.st_atime_ns, st.st_mtime_ns))
    second = plugin.build_mesh_record(str(mesh), first, now=2000.0)
    assert second["sha1"] == first["sha1"]
    assert second["recorded_at"] == 2000.0
    assert second["mtime_ns"] == first["mtime_ns"]


def test_mesh_record_over_limit_is_null_and_equal_limit_is_hashed(tmp_path):
    mesh = tmp_path / "big.fbx"
    payload = b"0123456789"
    mesh.write_bytes(payload)
    over = plugin.build_mesh_record(str(mesh), None, now=1.0, max_hash_bytes=9)
    assert over["sha1"] is None
    assert over["size"] == 10
    exact = plugin.build_mesh_record(str(mesh), None, now=1.0, max_hash_bytes=10)
    assert exact["sha1"] == hashlib.sha1(payload).hexdigest()


def test_mesh_record_empty_path_is_none():
    assert plugin.build_mesh_record("", None, now=1.0) is None
    assert plugin.build_mesh_record(None, None, now=1.0) is None


def test_publish_writes_result_before_flag(monkeypatch):
    order = []

    def _json(path, obj):
        order.append("result")

    def _flag(path):
        order.append("flag")

    monkeypatch.setattr(plugin, "write_json_atomic", _json)
    monkeypatch.setattr(plugin, "write_sync_flag", _flag)
    plugin.publish_export_outputs("C:/SPMayaLiveSync/live/demo", {"schema": 1}, True)
    assert order == ["result", "flag"]

    order.clear()
    plugin.publish_export_outputs("C:/SPMayaLiveSync/live/demo", {"schema": 1}, False)
    assert order == ["result"]


def test_publish_still_writes_flag_when_result_write_fails(monkeypatch):
    order = []

    def _json(path, obj):
        order.append("result")
        raise OSError("disk full")

    def _flag(path):
        order.append("flag")

    monkeypatch.setattr(plugin, "write_json_atomic", _json)
    monkeypatch.setattr(plugin, "write_sync_flag", _flag)
    plugin.publish_export_outputs("C:/SPMayaLiveSync/live/demo", {"schema": 1}, True)
    assert order == ["result", "flag"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr(plugin, "CONFIG_DIR", str(cfg))
    monkeypatch.setattr(plugin, "CONFIG_PATH", str(cfg / "live_sync_config.json"))
    monkeypatch.setattr(plugin, "CONFIG_LOCK_PATH", str(cfg / "live_sync_config.json.lock"))
    monkeypatch.setattr(plugin, "HISTORY_LOG_PATH", str(cfg / "history.log"))
    return tmp_path


@pytest.fixture
def engine(isolated, monkeypatch):
    monkeypatch.setattr(plugin.project, "is_open", lambda: True)
    monkeypatch.setattr(plugin.project, "is_busy", lambda: False)
    monkeypatch.setattr(plugin.project, "name", lambda: "Demo")
    monkeypatch.setattr(plugin.project, "file_path", lambda: "C:/work/Demo.spp")
    monkeypatch.setattr(plugin.textureset, "all_texture_sets", lambda: [])
    eng = plugin.LiveSyncEngine()
    eng.config["staging_dir"] = str(isolated / "staging")
    eng.config["watch_dir"] = str(isolated / "watch")
    eng.config["final_export_dir"] = str(isolated / "final")
    (isolated / "staging").mkdir()
    (isolated / "watch").mkdir()
    (isolated / "final").mkdir()
    messages = []
    monkeypatch.setattr(eng, "_emit_status", lambda text: messages.append(text))
    eng.messages = messages
    return eng


def _status_file(root):
    found = list(root.rglob("_sync_result.json"))
    assert len(found) == 1
    return json.loads(found[0].read_text(encoding="utf-8"))


def _arm_export(monkeypatch, isolated, status_name, payload=b"png-bytes", message="exported"):
    src = isolated / "Body_BaseColor.png"
    src.write_bytes(payload)

    class _Result:
        pass

    result = _Result()
    result.status = _NamedStatus(status_name)
    result.message = message
    result.textures = {("Body", "default"): [str(src)]}

    def _export(config):
        return result

    monkeypatch.setattr(plugin.export, "export_project_textures", _export)
    return src


def test_do_export_writes_result_before_flag(engine, isolated, monkeypatch):
    order = []
    real_json = plugin.write_json_atomic
    real_flag = plugin.write_sync_flag

    def _json(path, obj):
        order.append(("result", os.path.basename(path)))
        return real_json(path, obj)

    def _flag(path):
        order.append(("flag", os.path.basename(path)))
        return real_flag(path)

    monkeypatch.setattr(plugin, "write_json_atomic", _json)
    monkeypatch.setattr(plugin, "write_sync_flag", _flag)
    _arm_export(monkeypatch, isolated, "Success")
    engine._do_export(preview=True)
    assert [name for name, _base in order] == ["result", "flag"]
    body = _status_file(isolated)
    assert body["ok"] is True
    assert body["status"] == "success"
    assert body["kind"] == "preview"
    assert body["moved_count"] == 1
    assert body["texture_sets"] == ["Body"]
    assert body["flag_written"] is True
    assert body["request_id"] is None
    assert body["plugin_version"] == "1.3.0"
    assert len(body["export_id"]) == 32
    assert int(body["export_id"], 16) >= 0
    result_path = next(isolated.rglob("_sync_result.json"))
    flag_path = next(isolated.rglob("_sync_complete.flag"))
    assert flag_path.stat().st_mtime_ns >= result_path.stat().st_mtime_ns
    assert any(text == "プレビュー同期完了(1件更新)" for text in engine.messages)
    assert flag_path.read_text(encoding="utf-8")


def test_warning_does_not_establish_baseline_and_restores_dirty(engine, isolated, monkeypatch):
    _arm_export(monkeypatch, isolated, "Warning", message="partial")
    engine.final_baseline_established = False
    engine.dirty_stack_ids_final.add("stack-1")
    engine._do_export(preview=False, force_full=True)
    assert engine.final_baseline_established is False
    assert "stack-1" in engine.dirty_stack_ids_final
    body = _status_file(isolated)
    assert body["ok"] is False
    assert body["status"] == "warning"
    assert body["kind"] == "final"
    assert body["moved_count"] == 1
    assert any("最終書き出しは完了しませんでした(状態: 警告)" in text for text in engine.messages)
    assert not any("完了しました" in text for text in engine.messages)


def test_success_final_establishes_baseline(engine, isolated, monkeypatch):
    _arm_export(monkeypatch, isolated, "Success")
    engine.final_baseline_established = False
    engine.dirty_stack_ids_final.add("stack-1")
    engine._do_export(preview=False, force_full=True)
    assert engine.final_baseline_established is True
    assert "stack-1" not in engine.dirty_stack_ids_final
    body = _status_file(isolated)
    assert body["ok"] is True
    assert body["force_full"] is True
    assert any(text.startswith("フル解像度の最終書き出しが完了しました(1件") for text in engine.messages)


def test_cancelled_and_exception_are_not_ok(engine, isolated, monkeypatch):
    _arm_export(monkeypatch, isolated, "Cancelled", message="stop")
    engine.dirty_stack_ids_final.add("stack-9")
    engine._do_export(preview=False, force_full=True)
    body = _status_file(isolated)
    assert body["status"] == "cancelled"
    assert body["ok"] is False
    assert body["moved_count"] == 0
    assert body["flag_written"] is False
    assert "stack-9" in engine.dirty_stack_ids_final
    assert not list(isolated.rglob("_sync_complete.flag"))

    def _boom(config):
        raise RuntimeError("export failed")

    monkeypatch.setattr(plugin.export, "export_project_textures", _boom)
    with pytest.raises(RuntimeError):
        engine._do_export(preview=True)
    bodies = [json.loads(p.read_text(encoding="utf-8")) for p in isolated.rglob("_sync_result.json")]
    assert any(item["status"] == "exception" and item["ok"] is False for item in bodies)


def test_no_result_file_when_dest_root_is_unknown(engine, isolated):
    del engine.config["watch_dir"]
    with pytest.raises(KeyError):
        engine._do_export(preview=True)
    assert list(isolated.rglob("_sync_result.json")) == []


def test_result_write_failure_does_not_stop_export(engine, isolated, monkeypatch):
    _arm_export(monkeypatch, isolated, "Success")

    def _bad_json(path, obj):
        raise OSError("readonly")

    monkeypatch.setattr(plugin, "write_json_atomic", _bad_json)
    engine._do_export(preview=True)
    assert list(isolated.rglob("_sync_complete.flag"))
    assert list(isolated.rglob("_sync_result.json")) == []


def test_sync_count_increases_only_when_a_file_is_replaced(engine, isolated, monkeypatch):
    _arm_export(monkeypatch, isolated, "Success", payload=b"same-bytes")
    engine.stats["sync_count"] = 0
    engine._safe_export(preview=True)
    assert engine.stats["sync_count"] == 1

    # 同じファイル名へ、同じ内容をもう一度出す。置き換えは起きない。
    src2 = isolated / "Body_BaseColor.png"
    src2.write_bytes(b"same-bytes")

    class _Result:
        status = _NamedStatus("Success")
        message = "again"
        textures = {("Body", "default"): [str(src2)]}

    monkeypatch.setattr(plugin.export, "export_project_textures", lambda config: _Result())
    engine._safe_export(preview=True)
    assert engine.stats["sync_count"] == 1

    engine.final_baseline_established = True
    engine.dirty_stack_ids_final.clear()
    engine._safe_export(preview=False, force_full=False)
    assert engine.stats["sync_count"] == 1


def test_nested_config_updates_one_project_only(isolated):
    plugin.write_json_atomic(plugin.CONFIG_PATH, {
        "active_project_key": "keep-me",
        "painter_mesh_by_project": {
            "C:/work/Other.spp": {"path": "C:/SPMayaLiveSync/other.fbx"},
        },
    })
    written = plugin._update_nested_config_entry(
        "painter_mesh_by_project",
        "C:/work/Demo.spp",
        {"path": "C:/SPMayaLiveSync/demo.fbx", "sha1": "ab" * 20},
    )
    assert written["C:/work/Other.spp"]["path"] == "C:/SPMayaLiveSync/other.fbx"
    disk = json.loads(open(plugin.CONFIG_PATH, encoding="utf-8").read())
    assert disk["active_project_key"] == "keep-me"
    assert disk["painter_mesh_by_project"]["C:/work/Demo.spp"]["sha1"] == "ab" * 20
    assert disk["painter_mesh_by_project"]["C:/work/Other.spp"]["path"] == "C:/SPMayaLiveSync/other.fbx"


def test_nested_config_aborts_when_file_is_unreadable(isolated):
    with open(plugin.CONFIG_PATH, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert plugin._update_nested_config_entry("painter_mesh_by_project", "k", {"a": 1}) is None
    with open(plugin.CONFIG_PATH, encoding="utf-8") as handle:
        assert handle.read() == "{not json"


def test_migrate_moves_painter_mesh_entry(engine):
    engine.config["painter_mesh_by_project"] = {
        "__unsaved__": {"path": "C:/SPMayaLiveSync/mesh.fbx", "sha1": "cd" * 20},
    }
    assert engine._migrate_project_key_entries("__unsaved__", "C:/work/Demo.spp") is True
    moved = engine.config["painter_mesh_by_project"]
    assert "__unsaved__" not in moved
    assert moved["C:/work/Demo.spp"]["path"] == "C:/SPMayaLiveSync/mesh.fbx"


def test_record_mesh_skips_unsaved_empty_and_missing_api(engine, isolated, monkeypatch):
    mesh = isolated / "hero.fbx"
    mesh.write_bytes(b"mesh-body")
    monkeypatch.setattr(
        plugin.project, "last_imported_mesh_path", lambda: str(mesh), raising=False)
    monkeypatch.setattr(plugin.project, "name", lambda: None)
    engine._record_painter_mesh()
    assert not os.path.exists(plugin.CONFIG_PATH) or "painter_mesh_by_project" not in json.loads(
        open(plugin.CONFIG_PATH, encoding="utf-8").read()
    )

    monkeypatch.setattr(plugin.project, "name", lambda: "Demo")
    monkeypatch.setattr(plugin.project, "file_path", lambda: "C:/work/Demo.spp")
    monkeypatch.setattr(plugin.project, "last_imported_mesh_path", None, raising=False)
    engine._mesh_api_missing_logged = False
    engine._record_painter_mesh()
    assert not os.path.exists(plugin.CONFIG_PATH)

    monkeypatch.setattr(
        plugin.project, "last_imported_mesh_path", lambda: str(mesh), raising=False)
    engine._record_painter_mesh()
    disk = json.loads(open(plugin.CONFIG_PATH, encoding="utf-8").read())
    record = disk["painter_mesh_by_project"]["C:/work/Demo.spp"]
    assert record["sha1"] == hashlib.sha1(b"mesh-body").hexdigest()
    assert record["path"] == str(mesh)

    monkeypatch.setattr(plugin.project, "last_imported_mesh_path", lambda: "")
    engine._record_painter_mesh()
    disk = json.loads(open(plugin.CONFIG_PATH, encoding="utf-8").read())
    assert disk["painter_mesh_by_project"]["C:/work/Demo.spp"]["sha1"] == record["sha1"]


def test_edition_entered_schedules_named_timer(engine):
    started = []
    engine._mesh_record_timer.start = lambda ms=0: started.append(ms)
    engine.on_project_edition_entered(None)
    assert started == [plugin._MESH_RECORD_DELAY_MS]
