# -*- coding: utf-8 -*-
"""UV再マップ中に飛ばしたノードの注記。

maya_live_sync.py は Maya が無いと import できない。定義は ast で取り出し、
その部分だけを実行する。Pillow は使わない。
"""
from __future__ import annotations

import ast
from pathlib import Path

_LIVE_SYNC = Path(__file__).resolve().parent.parent / "maya_live_sync.py"

_NOTE = (
    "UV再マップで切り替え中のノードが {n} 件あるため、それらは更新していません。"
    "Painter の変更を反映するには、UV再マップ欄で『今すぐ変換』を押すか、"
    "『原本に戻す』を押してください。"
)


def _source_tree():
    source = _LIVE_SYNC.read_text(encoding="utf-8")
    return source, ast.parse(source)


def _note_namespace(tree):
    """MSG_UVREMAP_SKIPPED_FMT と _uvremap_skip_note の定義だけを実行する。"""
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MSG_UVREMAP_SKIPPED_FMT":
                    body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_uvremap_skip_note":
            body.append(node)
    assert len(body) == 2
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, "<uvremap_skip_note>", "exec"), namespace)
    return namespace


def _method(tree, class_name, method_name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method_name:
                    return child
    raise AssertionError("{0}.{1} が無い".format(class_name, method_name))


def _calls(fn, name):
    found = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
            found += 1
    return found


def _skip_adds(fn):
    found = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add":
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "uvremap_skipped":
            found += 1
    return found


def test_skip_note_empty_for_zero_negative_and_non_int():
    _source, tree = _source_tree()
    ns = _note_namespace(tree)
    note = ns["_uvremap_skip_note"]
    assert ns["MSG_UVREMAP_SKIPPED_FMT"] == _NOTE
    for value in (0, -1, -6, None, "", "1", 1.0, 6.5, True, False, []):
        assert note(value) == ""


def test_skip_note_includes_count():
    _source, tree = _source_tree()
    note = _note_namespace(tree)["_uvremap_skip_note"]
    assert note(1) == _NOTE.format(n=1)
    assert note(6) == _NOTE.format(n=6)
    assert "1 件" in note(1)
    assert "6 件" in note(6)


def test_reload_and_switch_call_skip_note():
    _source, tree = _source_tree()
    watcher = "LiveSyncWatcher"
    reload_textures = _method(tree, watcher, "reload_textures")
    reload_final = _method(tree, watcher, "reload_final_textures")
    switch = _method(tree, watcher, "switch_texture_quality")
    assert _calls(reload_textures, "_uvremap_skip_note") >= 1
    assert _calls(reload_final, "_uvremap_skip_note") >= 1
    assert _calls(switch, "_uvremap_skip_note") >= 1
    assert _skip_adds(reload_textures) == 1
    assert _skip_adds(reload_final) == 1
    assert _skip_adds(switch) == 3
    reload_src = ast.get_source_segment(_LIVE_SYNC.read_text(encoding="utf-8"), reload_textures)
    assert "_uvremap_skip_noted_count" in reload_src


def test_version_is_1_7_1():
    _source, tree = _source_tree()
    version = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__version__":
                if isinstance(node.value, ast.Constant):
                    version = node.value.value
    assert version == "1.7.1"
