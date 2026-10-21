# -*- coding: utf-8 -*-
"""1.4.3: プラグインを役割ごとの部品へ分割したことの構造テスト。

- 部品の一覧、層、依存の向き、循環が無いこと
- 純関数の部品が Painter / Qt を import しないこと
- 分割前のトップレベル名がすべて ``sp_live_sync_plugin`` から参照できること
- テストの差し替え補助関数(plugin_patch.py)と、それを使う約束
- reload_plugin(再読み込みのフック)
"""
from __future__ import annotations

import ast
import glob
import importlib
import importlib.util
import os
import shutil
import sys
import uuid

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_TESTS)
_REPO = os.path.dirname(_PKG)
for _p in (_PKG, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_sp_live_sync_plugin_protocol import (  # noqa: E402
    plugin,
    _install_stubs,
    _restore_modules,
)
from plugin_patch import patch_plugin_attr, plugin_modules  # noqa: E402
from plugin_source import DEFAULT_PACKAGE_DIR, plugin_source_files  # noqa: E402

# 部品 -> 層。下の層は上の層を import しない。同じ層どうしも循環させない。
EXPECTED_LAYERS = {
    "_version": 0,
    "_qt": 0,
    "_base": 1,
    "_project_key": 2,
    "_settings_model": 2,
    "_rebase_protocol": 3,
    "_rebase_policy": 3,
    "_rebase_uvfill": 3,
    "_rebase_job": 4,
    "_rebase_coordinator": 4,
    "_painter_rebase_api": 5,
    "_live_engine": 6,
    "_wizard": 7,
    "_live_panel": 7,
    "__init__": 8,
}
PURE_PARTS = ("_settings_model", "_rebase_protocol", "_rebase_policy", "_rebase_uvfill")
PUBLIC_NAMES_PATH = os.path.join(_TESTS, "data", "plugin_public_names.txt")
IMPURE_PREFIXES = ("substance_painter", "PySide2", "PySide6")


def _parts():
    """{部品名: (ast, 文字列)}。"""
    found = {}
    for path in plugin_source_files():
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        found[name] = (ast.parse(text), text)
    return found


def _part_module(name):
    return importlib.import_module(plugin.__name__ + "." + name)


def _top_level_bound_names(tree):
    names = []

    def collect(statements):
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            names.append(sub.id)
            elif isinstance(node, ast.Try):
                collect(node.body)
                for handler in node.handlers:
                    collect(handler.body)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                pass
            if isinstance(node, ast.Try):
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        for alias in sub.names:
                            if alias.name != "*":
                                name = (alias.asname or alias.name).split(".")[0]
                                if name not in names:
                                    names.append(name)

    collect(tree.body)
    return [n for n in names if n != "__all__"]


# ---------------------------------------------------------------------------
# 部品の一覧と層、依存の向き
# ---------------------------------------------------------------------------
def test_parts_are_exactly_the_expected_files():
    assert set(_parts()) == set(EXPECTED_LAYERS)


def _relative_imports(tree):
    """[(level, module, [名前], トップレベルか)]"""
    top = {id(node) for node in tree.body}
    return [
        (node.level, node.module, [a.name for a in node.names], id(node) in top)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
    ]


def _dependency_graph():
    graph = {}
    for name, (tree, _text) in _parts().items():
        deps = set()
        for level, module, names, _at_top in _relative_imports(tree):
            assert level == 1, "{0}: 想定外の相対 import".format(name)
            assert module is not None, "{0}: from . import は使わない".format(name)
            deps.add(module)
        graph[name] = deps
    return graph


def test_dependencies_follow_layers():
    graph = _dependency_graph()
    for name, deps in graph.items():
        if name == "__init__":
            continue
        assert "__init__" not in deps
        for dep in deps:
            assert dep in EXPECTED_LAYERS, "{0} -> {1}".format(name, dep)
            assert EXPECTED_LAYERS[dep] <= EXPECTED_LAYERS[name], (
                "{0}(層{1}) が上の層 {2}(層{3}) を import".format(
                    name, EXPECTED_LAYERS[name], dep, EXPECTED_LAYERS[dep]))


def test_dependencies_have_no_cycles():
    graph = {k: v for k, v in _dependency_graph().items() if k != "__init__"}
    state = {}

    def visit(node, path):
        state[node] = 1
        for nxt in sorted(graph.get(node, ())):
            assert state.get(nxt) != 1, "循環: " + " -> ".join(path + [node, nxt])
            if state.get(nxt) is None:
                visit(nxt, path + [node])
        state[node] = 2

    for node in sorted(graph):
        if state.get(node) is None:
            visit(node, [])


def test_layer_table_matches_split_tool():
    path = os.path.join(_REPO, "tools", "split_plugin", "assignment.py")
    spec = importlib.util.spec_from_file_location("_split_assignment_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.LAYERS == EXPECTED_LAYERS
    assert tuple(module.PURE_PARTS) == PURE_PARTS


def test_init_imports_every_part_first_and_explicitly():
    tree, _text = _parts()["__init__"]
    imported = {}
    last_part_import = -1
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            names = [a.name for a in node.names]
            assert "*" not in names
            imported[node.module] = names
            last_part_import = index
    assert set(imported) == set(EXPECTED_LAYERS) - {"__init__"}
    for node in tree.body[:last_part_import]:
        assert isinstance(node, (ast.Import, ast.ImportFrom, ast.Expr)), (
            "部品の import より前に import 以外の文があります")


def test_no_lazy_package_imports_and_no_star_imports():
    for name, (tree, _text) in _parts().items():
        for level, module, names, at_top in _relative_imports(tree):
            assert at_top, "{0}: 関数の中などで部品を import しています".format(name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert all(a.name != "*" for a in node.names), name


# ---------------------------------------------------------------------------
# 純関数の部品
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", PURE_PARTS)
def test_pure_parts_do_not_import_painter_or_qt(name):
    tree, _text = _parts()[name]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(IMPURE_PREFIXES), alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith(IMPURE_PREFIXES), module
            assert not (node.level and module == "_qt"), "_qt を import しています"


# ---------------------------------------------------------------------------
# 公開名・__all__・版・_engine/_panel
# ---------------------------------------------------------------------------
def _public_names():
    with open(PUBLIC_NAMES_PATH, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def test_every_pre_split_top_level_name_is_reachable():
    names = _public_names()
    assert len(names) > 200
    assert len(names) == len(set(names))
    missing = [name for name in names if not hasattr(plugin, name)]
    assert missing == []


def test_all_lists_match_definitions_and_reexports_are_identical():
    for name, (tree, _text) in _parts().items():
        if name == "__init__":
            continue
        listed = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and (
                    node.targets[0].id == "__all__"):
                listed = [e.value for e in node.value.elts]
        assert listed is not None, "{0}: __all__ がありません".format(name)
        assert sorted(listed) == sorted(_top_level_bound_names(tree)), name
        module = _part_module(name)
        assert sorted(module.__all__) == sorted(listed)
        for symbol in listed:
            assert getattr(plugin, symbol) is getattr(module, symbol), (name, symbol)


def test_version_has_a_single_home():
    homes = []
    for name, (tree, _text) in _parts().items():
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
                homes.append(name)
    assert homes == ["_version"]
    assert plugin.__version__ == _part_module("_version").__version__ == "1.4.5"


def test_engine_and_panel_are_only_touched_in_init():
    for name, (tree, _text) in _parts().items():
        if name == "__init__":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in ("_engine", "_panel"), (name, node.lineno)
            assert not isinstance(node, ast.Global), (name, node.lineno)
    init_tree = _parts()["__init__"][0]
    declared = {
        n for node in ast.walk(init_tree) if isinstance(node, ast.Global) for n in node.names}
    assert declared == {"_engine", "_panel"}


def test_package_files_use_crlf():
    for path in plugin_source_files():
        with open(path, "rb") as handle:
            raw = handle.read()
        assert raw.endswith(b"\r\n"), path
        assert raw.count(b"\n") == raw.count(b"\r\n"), path
        assert raw.count(b"\r") == raw.count(b"\r\n"), path


# ---------------------------------------------------------------------------
# テストの差し替え補助関数
# ---------------------------------------------------------------------------
def test_patch_helper_reaches_parts_that_imported_the_name(tmp_path):
    original = plugin._log
    calls = []
    with pytest.MonkeyPatch.context() as mp:
        targets = patch_plugin_attr(
            mp, plugin, "_log", lambda level, message: calls.append((level, message)))
        names = {module.__name__ for module in targets}
        prefix = plugin.__name__ + "."
        # 本体、_log を定義する _base、_log を取り込む部品が、すべて差し替わる
        assert plugin.__name__ in names
        assert prefix + "_base" in names
        assert prefix + "_live_engine" in names
        assert prefix + "_painter_rebase_api" in names
        for module in targets:
            assert module._log is not original
        # _base の中の関数が、差し替え後の _log を呼ぶ
        assert plugin.write_rebase_result_copy(str(tmp_path), "bad id", {}) is False
        assert [level for level, _ in calls] == ["warning"]
    for module in targets:
        assert module._log is original


def test_patch_helper_leaves_other_names_alone():
    marker = object()
    with pytest.MonkeyPatch.context() as mp:
        targets = patch_plugin_attr(mp, plugin, "CONFIG_DIR", marker)
        assert all(module.CONFIG_DIR is marker for module in targets)
        base = _part_module("_base")
        assert base.CONFIG_PATH is not marker
        assert base.HISTORY_LOG_PATH is not marker


def test_patch_helper_rejects_unknown_names():
    with pytest.MonkeyPatch.context() as mp:
        with pytest.raises(AttributeError):
            patch_plugin_attr(mp, plugin, "no_such_name_in_plugin", None)


def test_plugin_modules_lists_package_and_parts():
    names = {module.__name__ for module in plugin_modules(plugin)}
    expected = {plugin.__name__} | {
        plugin.__name__ + "." + part for part in EXPECTED_LAYERS if part != "__init__"}
    assert names == expected


def test_tests_never_patch_plugin_attributes_directly():
    """monkeypatch.setattr(plugin, "名前", ...) は部品の中の呼び出しに効かない。

    パッケージ本体の属性しか差し替わらないので、patch_plugin_attr() を使う。
    ``plugin.project`` のような属性(Painter の偽物のモジュール)の差し替えは対象外。
    """
    offenders = []
    for path in sorted(glob.glob(os.path.join(_TESTS, "test_*.py"))):
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "setattr"):
                continue
            if len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and (
                    node.args[0].id == "plugin") and isinstance(
                    node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                offenders.append("{0}:{1}".format(os.path.basename(path), node.lineno))
    assert offenders == []


def test_direct_setattr_on_package_would_miss_parts():
    """なぜ補助関数が要るのか。パッケージ本体だけを差し替えても、部品は元のまま。"""
    package = sys.modules[plugin.__name__]
    base = _part_module("_base")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(package, "_log", lambda level, message: None)
        assert base._log is not package._log
    assert base._log is package._log


# ---------------------------------------------------------------------------
# reload_plugin
# ---------------------------------------------------------------------------
@pytest.fixture
def copied_plugin(tmp_path, monkeypatch):
    """パッケージを別名でコピーして読み込む(本物の plugin を汚さない)。"""
    name = "splug_reload_{0}".format(uuid.uuid4().hex[:8])
    shutil.copytree(
        DEFAULT_PACKAGE_DIR, str(tmp_path / name),
        ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("SPMAYA_CONFIG_DIR", str(tmp_path / "cfg"))
    _install_stubs()
    try:
        module = importlib.import_module(name)
        yield module
    finally:
        for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
            del sys.modules[key]
        _restore_modules()


def _loaded_parts(module):
    prefix = module.__name__ + "."
    return {key: sys.modules[key] for key in sorted(sys.modules) if key.startswith(prefix)}


def _assert_bound_to_current_parts(module, old_parts):
    current = _loaded_parts(module)
    assert set(current) == set(old_parts)
    for key, part in current.items():
        assert part is not old_parts[key], "{0} が読み直されていません".format(key)
        for symbol in part.__all__:
            assert getattr(module, symbol) is getattr(part, symbol), (key, symbol)
    assert sys.modules[module.__name__] is module


def test_hook_exists_and_package_has_all_parts_loaded(copied_plugin):
    assert callable(copied_plugin.reload_plugin)
    names = set(_loaded_parts(copied_plugin))
    assert names == {
        copied_plugin.__name__ + "." + part for part in EXPECTED_LAYERS if part != "__init__"}


def test_reload_hook_makes_parts_fresh(copied_plugin):
    old = _loaded_parts(copied_plugin)
    copied_plugin.reload_plugin()
    _assert_bound_to_current_parts(copied_plugin, old)


def test_reload_hook_accepts_a_module_argument(copied_plugin):
    old = _loaded_parts(copied_plugin)
    copied_plugin.reload_plugin(copied_plugin)
    _assert_bound_to_current_parts(copied_plugin, old)


def test_package_reload_alone_keeps_old_parts(copied_plugin):
    """フックが無いと、本体だけの importlib.reload は古い部品を指したまま。"""
    old = _loaded_parts(copied_plugin)
    importlib.reload(copied_plugin)
    assert _loaded_parts(copied_plugin) == old


@pytest.mark.parametrize("order", ["hook_then_reload", "reload_then_hook"])
def test_parts_are_fresh_in_either_order(copied_plugin, order):
    """Adobe の資料だけでは、本体の reload がフックの前か後か確定できない。"""
    old = _loaded_parts(copied_plugin)
    if order == "hook_then_reload":
        copied_plugin.reload_plugin()
        importlib.reload(copied_plugin)
    else:
        importlib.reload(copied_plugin)
        copied_plugin.reload_plugin()
    _assert_bound_to_current_parts(copied_plugin, old)
    # 読み直したあとも、起動と停止の入口が動く
    copied_plugin.close_plugin()
    assert copied_plugin._engine is None and copied_plugin._panel is None
    assert copied_plugin.LiveSyncEngine is _loaded_parts(copied_plugin)[
        copied_plugin.__name__ + "._live_engine"].LiveSyncEngine
