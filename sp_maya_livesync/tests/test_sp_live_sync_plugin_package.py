# -*- coding: utf-8 -*-
"""フォルダー形式のソース一覧(1.4.3 で役割ごとの部品に分割)と、古い1ファイル版の検出。"""
from __future__ import annotations

import ast
import os
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_TESTS)
for _p in (_PKG, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_sp_live_sync_plugin_protocol import plugin  # noqa: E402
from plugin_source import (  # noqa: E402
    DEFAULT_PACKAGE_DIR,
    plugin_source_files,
    plugin_source_text,
)


def _relpaths(package_dir=None):
    root = DEFAULT_PACKAGE_DIR if package_dir is None else os.fspath(package_dir)
    return {
        os.path.relpath(path, root).replace("\\", "/")
        for path in plugin_source_files(package_dir)
    }


def _imported_and_single_shot(source):
    tree = ast.parse(source)
    imported = set()
    single_shot = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "singleShot":
            single_shot = True
    return imported, single_shot


EXPECTED_PACKAGE_FILES = {
    "__init__.py",
    "_base.py",
    "_live_engine.py",
    "_live_panel.py",
    "_painter_rebase_api.py",
    "_project_key.py",
    "_qt.py",
    "_rebase_coordinator.py",
    "_rebase_job.py",
    "_rebase_policy.py",
    "_rebase_protocol.py",
    "_rebase_uvfill.py",
    "_settings_model.py",
    "_version.py",
    "_wizard.py",
}


def test_plugin_package_py_files_are_the_expected_parts():
    assert _relpaths() == EXPECTED_PACKAGE_FILES


def test_source_helper_finds_violation_only_in_sibling_module(tmp_path):
    package = tmp_path / "fake_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    (package / "sub.py").write_text(
        "import threading\n"
        "QTimer.singleShot(0, lambda: None)\n",
        encoding="utf-8",
    )
    imported, single_shot = _imported_and_single_shot(plugin_source_text(str(package)))
    assert "threading" in imported
    assert single_shot is True
    init_imported, init_shot = _imported_and_single_shot(
        (package / "__init__.py").read_text(encoding="utf-8"))
    assert "threading" not in init_imported
    assert init_shot is False
    assert _relpaths(str(package)) == {"__init__.py", "sub.py"}


def test_stale_warning_text():
    assert plugin.STALE_SINGLE_FILE_PLUGIN_WARNING == (
        "古い sp_live_sync_plugin.py が plugins フォルダーに残っています({path})。"
        "このフォルダー版が使われていますが、混乱を防ぐため古いファイルを削除してください。"
    )


def _package_init(tmp_path):
    package = tmp_path / "sp_live_sync_plugin"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text("# package\n", encoding="utf-8")
    return init


def test_stale_single_file_returns_path_when_file_exists(tmp_path):
    init = _package_init(tmp_path)
    stale = tmp_path / "sp_live_sync_plugin.py"
    stale.write_text("# old\n", encoding="utf-8")
    (tmp_path / "sp_live_sync_plugin_old.py").write_text("# decoy\n", encoding="utf-8")
    (tmp_path / "sp_live_sync_plugin.py.bak").write_text("# decoy\n", encoding="utf-8")
    got = plugin.stale_single_file_plugin_path(os.path.abspath(str(init)))
    assert got == os.path.join(str(tmp_path), "sp_live_sync_plugin.py")
    assert os.path.isfile(got)


def test_stale_single_file_absent_is_none(tmp_path):
    init = _package_init(tmp_path)
    assert plugin.stale_single_file_plugin_path(os.path.abspath(str(init))) is None


def test_stale_single_file_directory_is_none(tmp_path):
    init = _package_init(tmp_path)
    same_name = tmp_path / "sp_live_sync_plugin.py"
    same_name.mkdir()
    (same_name / "nested.py").write_text("import threading\n", encoding="utf-8")
    assert plugin.stale_single_file_plugin_path(os.path.abspath(str(init))) is None


def test_stale_single_file_broken_path_is_none():
    class Boom(object):
        def __fspath__(self):
            raise RuntimeError("broken")

    for bad in (None, "", 0, object(), "\x00", Boom()):
        assert plugin.stale_single_file_plugin_path(bad) is None


def test_start_plugin_checks_stale_file_after_version_log():
    source = plugin_source_text()
    start = source.index("def start_plugin():")
    body = source[start:source.index("\ndef close_plugin(", start)]
    version_at = body.index("loaded version:")
    check_at = body.index("stale_single_file_plugin_path(")
    assert version_at < check_at
    tail = body[check_at:]
    assert '_log("warning"' in tail
    assert '_append_history("SP"' in tail
