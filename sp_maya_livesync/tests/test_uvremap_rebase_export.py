# -*- coding: utf-8 -*-
"""リベース用 FBX 書き出し(uvremap_maya)の Maya 非依存テスト。

MEL と cmds は注入する。ファイルは tmp_path の中だけに作る。
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys

import pytest

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

import uvremap_maya as um  # noqa: E402

_SHAPES = ["|group|mesh|meshShape", "|group|other|otherShape"]
_PARENTS = ["|group|mesh", "|group|other"]
_ABS_PATH_RE = re.compile(
    r"(?:^|[\s'\"`(])(?:/[^\s'\"`]+|[A-Za-z]:[\\/][^\s'\"`]*)"
)


class FakeCmds(object):
    """pluginInfo / loadPlugin / objExists / ls / select / listRelatives を持つ。"""

    def __init__(
        self,
        exists=None,
        plugin_loaded=False,
        load_error=False,
        fail_restore=False,
        parents=None,
        relatives_error=False,
    ):
        self.exists = set(exists or [])
        self.plugin_loaded = plugin_loaded
        self.load_error = load_error
        self.fail_restore = fail_restore
        self.parents = {} if parents is None else parents
        self.relatives_error = relatives_error
        self.selection = []
        self.calls = []

    def pluginInfo(self, name, query=False, loaded=False):
        self.calls.append(("pluginInfo", name, query, loaded))
        return self.plugin_loaded

    def loadPlugin(self, name, quiet=False):
        self.calls.append(("loadPlugin", name, quiet))
        if self.load_error:
            raise RuntimeError("load failed")
        self.plugin_loaded = True
        return [name]

    def objExists(self, path):
        self.calls.append(("objExists", path))
        return path in self.exists

    def listRelatives(self, node, parent=False, fullPath=False):
        self.calls.append(("listRelatives", node, parent, fullPath))
        if self.relatives_error:
            raise RuntimeError("relatives failed")
        found = self.parents.get(node, [])
        if isinstance(found, str):
            return [found]
        return list(found)

    def ls(self, selection=False, long=False):
        self.calls.append(("ls", selection, long))
        return list(self.selection)

    def select(self, *args, **kwargs):
        self.calls.append(("select", args, kwargs))
        if self.fail_restore and sum(1 for call in self.calls if call[0] == "select") >= 2:
            raise RuntimeError("restore failed")
        if kwargs.get("clear"):
            self.selection = []
            return
        items = args[0] if args else []
        if kwargs.get("replace", False):
            self.selection = list(items)


class FakeMel(object):
    """FBXExport を受けたらパスへバイト列を書く。mode で失敗を切り替える。"""

    def __init__(self, cmds, payload=None, mode="write"):
        self.cmds = cmds
        self.payload = b"fbx-payload" if payload is None else payload
        self.mode = mode
        self.commands = []
        self.selection_at_calls = []
        self.written = []

    def __call__(self, command):
        self.commands.append(command)
        self.cmds.calls.append(("mel", command))
        self.selection_at_calls.append(list(self.cmds.selection))
        if not command.startswith("FBXExport -f "):
            return
        if self.mode == "interrupt":
            path = mel_export_path(command)
            _write_bytes(path, b"partial")
            self.written.append(path)
            raise KeyboardInterrupt()
        if self.mode == "error":
            path = mel_export_path(command)
            _write_bytes(path, self.payload or b"x")
            self.written.append(path)
            raise RuntimeError("mel failed")
        if self.mode == "empty":
            return
        path = mel_export_path(command)
        data = b"" if self.mode == "zero" else self.payload
        _write_bytes(path, data)
        self.written.append(path)


def _write_bytes(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def mel_export_path(command):
    prefix = 'FBXExport -f "'
    suffix = '" -s;'
    if not (command.startswith(prefix) and command.endswith(suffix)):
        raise AssertionError(command)
    body = command[len(prefix):-len(suffix)]
    out = []
    index = 0
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body):
            out.append(body[index + 1])
            index += 2
            continue
        out.append(body[index])
        index += 1
    return "".join(out)


def assert_safe_error(exc, tmp_path):
    assert isinstance(exc, um.RebaseExportError)
    assert exc.code == "export_failed"
    text = str(exc)
    assert os.fspath(tmp_path) not in text
    assert str(tmp_path) not in text
    for arg in exc.args:
        assert os.fspath(tmp_path) not in str(arg)
    assert _ABS_PATH_RE.search(text) is None


def _export(meshes, cmds, mel, **kwargs):
    kwargs.setdefault("shape_paths", list(_SHAPES))
    kwargs.setdefault("baseline_id", "bl-1")
    return um.export_rebase_mesh(
        kwargs.pop("shape_paths"),
        meshes,
        kwargs.pop("baseline_id"),
        mel_eval=mel,
        cmds=cmds,
        **kwargs,
    )


def _paths(meshes, baseline_id="bl-1", tmp_style=None):
    return um.rebase_fbx_paths(meshes, baseline_id, tmp_style=tmp_style)


def test_import_does_not_load_maya():
    script = (
        "import sys\n"
        "before = {n for n in sys.modules if n == 'maya' or n.startswith('maya.')}\n"
        "import uvremap_maya\n"
        "after = {n for n in sys.modules if n == 'maya' or n.startswith('maya.')}\n"
        "assert after == before, (sorted(before), sorted(after))\n"
    )
    env = os.environ.copy()
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PKG_DIR + (os.pathsep + previous if previous else "")
    subprocess.check_call([sys.executable, "-c", script], env=env)


def test_source_has_no_top_level_maya_import_and_no_clock():
    source = open(um.__file__, "r", encoding="utf-8").read()
    assert "datetime.now" not in source
    assert "time.time" not in source
    marker = "# 2026.10.19(RB-c):"
    assert marker in source
    section = source.split(marker, 1)[1]
    for line in section.splitlines():
        assert not line.startswith("import maya")
        assert not line.startswith("from maya")

    forbidden = {
        "maya",
        "uvremap_core",
        "uvremap_rebase",
        "uvremap_runtime",
        "uvremap_worker",
        "uvremap_io",
    }

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.depth = 0
            self.bad = []

        def visit_FunctionDef(self, node):
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Import(self, node):
            if self.depth == 0:
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden:
                        self.bad.append(alias.name)
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            if self.depth == 0 and (node.module or "").split(".")[0] in forbidden:
                self.bad.append(node.module)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(ast.parse(source))
    assert visitor.bad == []


def test_export_success_matches_file_and_leaves_no_tmp(tmp_path):
    meshes = tmp_path / "meshes"
    payload = b"fbx-payload-" + bytes(range(256)) * 8
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=False)
    cmds.selection = ["|persp", "|cam"]
    mel = FakeMel(cmds, payload=payload)
    final, tmp = _paths(meshes)
    result = _export(meshes, cmds, mel)
    assert type(result) is dict
    assert set(result) == {"path", "size", "sha1"}
    assert isinstance(result["path"], str)
    assert isinstance(result["size"], int) and not isinstance(result["size"], bool)
    data = final.read_bytes()
    assert data == payload
    assert result["path"] == os.path.abspath(os.fspath(final))
    assert result["size"] == len(data) == final.stat().st_size
    assert result["sha1"] == hashlib.sha1(data).hexdigest()
    assert len(result["sha1"]) == 40
    assert result["sha1"] == result["sha1"].lower()
    assert not tmp.exists()
    assert cmds.selection == ["|persp", "|cam"]
    assert mel.written == [os.fspath(tmp)]


def test_mel_command_order_triangulate_and_temp_path(tmp_path):
    expected = [
        "FBXResetExport;",
        "FBXExportInAscii -v false;",
        "FBXExportEmbeddedTextures -v false;",
        "FBXExportAnimationOnly -v false;",
        "FBXExportInputConnections -v false;",
        "FBXExportCameras -v false;",
        "FBXExportLights -v false;",
        "FBXExportSmoothingGroups -v true;",
        "FBXExportTriangulate -v false;",
        'FBXExport -f "C:/tmp/bl-1.fbx.tmp" -s;',
    ]
    assert um.build_fbx_export_commands(r"C:\tmp\bl-1.fbx.tmp") == expected
    assert len(expected) == 10
    assert all(command.endswith(";") for command in expected)
    flipped = um.build_fbx_export_commands("C:/tmp/bl-1.fbx.tmp", triangulate=True)
    assert flipped[8] == "FBXExportTriangulate -v true;"
    assert flipped[0] == "FBXResetExport;"
    assert flipped[1:4] == expected[1:4]
    assert flipped[-1].startswith("FBXExport -f ")
    with pytest.raises(ValueError):
        um.build_fbx_export_commands("C:/tmp/bl-1.fbx.tmp", triangulate=1)
    with pytest.raises(ValueError):
        um.build_fbx_export_commands("C:/tmp/bl-1.fbx.tmp", triangulate="false")

    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    mel = FakeMel(cmds, payload=b"abc")
    final, tmp = _paths(meshes)
    _export(meshes, cmds, mel)
    assert mel.commands[0] == "FBXResetExport;"
    assert mel.commands[-1].startswith("FBXExport -f ")
    assert "FBXExportTriangulate -v false;" in mel.commands
    assert mel_export_path(mel.commands[-1]) == os.fspath(tmp)
    assert mel_export_path(mel.commands[-1]) != os.fspath(final)
    assert mel.commands[1:4] == [
        "FBXExportInAscii -v false;",
        "FBXExportEmbeddedTextures -v false;",
        "FBXExportAnimationOnly -v false;",
    ]
    assert len(mel.commands) == 10
    assert all(seen == list(_PARENTS) for seen in mel.selection_at_calls)
    assert not any(call[0] == "listRelatives" for call in cmds.calls)


def test_tmp_style_suffix_infix_and_unknown(tmp_path):
    meshes = tmp_path / "meshes"
    final_s, tmp_s = _paths(meshes, "bl-1", "suffix")
    final_i, tmp_i = _paths(meshes, "bl-1", "infix")
    assert final_s.name == "bl-1.fbx"
    assert tmp_s.name == "bl-1.fbx.tmp"
    assert final_i.name == "bl-1.fbx"
    assert tmp_i.name == "bl-1.tmp.fbx"
    assert final_s == final_i
    with pytest.raises(ValueError):
        _paths(meshes, "bl-1", "nope")

    for style, name in (("suffix", "bl-suffix.fbx.tmp"), ("infix", "bl-infix.tmp.fbx")):
        baseline = "bl-" + style
        cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
        mel = FakeMel(cmds, payload=b"style-" + style.encode("ascii"))
        final, tmp = _paths(meshes, baseline, tmp_style=style)
        result = _export(meshes, cmds, mel, baseline_id=baseline, tmp_style=style)
        assert tmp.name == name
        assert final.name == baseline + ".fbx"
        assert mel.written == [os.fspath(tmp)]
        assert result["path"] == os.path.abspath(os.fspath(final))
        assert not tmp.exists()
        assert final.read_bytes().startswith(b"style-")

    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    mel = FakeMel(cmds, payload=b"default-style")
    _export(meshes, cmds, mel, baseline_id="bl-default")
    assert mel.written[0].endswith(os.sep + "bl-default.fbx.tmp")


def test_tmp_style_follows_constant(tmp_path, monkeypatch):
    monkeypatch.setattr(um, "REBASE_FBX_TMP_STYLE", "infix")
    meshes = tmp_path / "meshes"
    final, tmp = um.rebase_fbx_paths(meshes, "bl-const")
    assert tmp.name == "bl-const.tmp.fbx"
    assert final.name == "bl-const.fbx"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    mel = FakeMel(cmds, payload=b"const")
    _export(meshes, cmds, mel, baseline_id="bl-const")
    assert mel.written == [os.fspath(tmp)]


def test_escape_mel_string_windows_path():
    raw = "C:\\SPMayaLiveSync\\uvremap\\meshes\\bl-1.fbx.tmp"
    assert um.escape_mel_string(raw) == (
        "C:/SPMayaLiveSync/uvremap/meshes/bl-1.fbx.tmp"
    )
    quoted = 'C:\\mesh"a"\\b.fbx.tmp'
    assert um.escape_mel_string(quoted) == 'C:/mesh\\"a\\"/b.fbx.tmp'
    command = um.build_fbx_export_commands(quoted)[-1]
    assert command == 'FBXExport -f "C:/mesh\\"a\\"/b.fbx.tmp" -s;'
    assert "\\" not in command.replace('\\"', "")
    assert mel_export_path(command) == 'C:/mesh"a"/b.fbx.tmp'


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a\\b", "..", "a b", ".a", "-a", "a" * 81])
def test_baseline_id_rejected(tmp_path, bad):
    meshes = tmp_path / "meshes"
    with pytest.raises(ValueError):
        _paths(meshes, bad)
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    cmds.selection = ["|keep"]
    mel = FakeMel(cmds)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel, baseline_id=bad)
    assert_safe_error(caught.value, tmp_path)
    if bad:
        assert bad not in str(caught.value)
    assert cmds.selection == ["|keep"]
    assert cmds.calls == []
    assert mel.commands == []
    assert not meshes.exists()


def test_baseline_id_accepts_boundary(tmp_path):
    um.rebase_fbx_paths(tmp_path, "a")
    um.rebase_fbx_paths(tmp_path, "A" * 80)
    um.rebase_fbx_paths(tmp_path, "b_1-Z")


def test_selection_restored_when_mel_fails(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    cmds.selection = ["|keepA", "|keepB"]
    mel = FakeMel(cmds, mode="error", payload=b"partial-export")
    final, tmp = _paths(meshes)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel)
    assert_safe_error(caught.value, tmp_path)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert cmds.selection == ["|keepA", "|keepB"]
    assert mel.selection_at_calls[-1] == list(_PARENTS)
    assert not tmp.exists()
    assert not final.exists()


def test_selection_cleared_when_previous_was_empty(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    cmds.selection = []
    mel = FakeMel(cmds, payload=b"empty-sel")
    _export(meshes, cmds, mel)
    assert cmds.selection == []
    select_calls = [call for call in cmds.calls if call[0] == "select"]
    assert select_calls[0][1][0] == list(_PARENTS)
    assert select_calls[0][2].get("replace") is True
    assert select_calls[-1][2].get("clear") is True
    assert all(seen == list(_PARENTS) for seen in mel.selection_at_calls)


def test_keyboard_interrupt_cleans_tmp_and_restores_selection(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    cmds.selection = ["|keep"]
    mel = FakeMel(cmds, mode="interrupt")
    final, tmp = _paths(meshes)
    with pytest.raises(KeyboardInterrupt):
        _export(meshes, cmds, mel)
    assert cmds.selection == ["|keep"]
    assert not tmp.exists()
    assert not final.exists()


def test_restore_failure_does_not_hide_success(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True, fail_restore=True)
    mel = FakeMel(cmds, payload=b"kept")
    result = _export(meshes, cmds, mel)
    assert result["size"] == 4
    assert (meshes / "bl-1.fbx").read_bytes() == b"kept"


@pytest.mark.parametrize(
    "kwargs,mode",
    [
        ({}, "error"),
        ({}, "empty"),
        ({}, "zero"),
    ],
)
def test_mel_failure_leaves_no_files(tmp_path, kwargs, mode):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    mel = FakeMel(cmds, mode=mode, payload=b"nope")
    final, tmp = _paths(meshes)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel, **kwargs)
    assert_safe_error(caught.value, tmp_path)
    assert not tmp.exists()
    assert not final.exists()


def test_replace_exception_leaves_no_tmp_when_final_was_not_written(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    mel = FakeMel(cmds, payload=b"body")

    def boom(src, dst):
        raise RuntimeError("replace failed")

    final, tmp = _paths(meshes)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel, replace_fn=boom)
    assert_safe_error(caught.value, tmp_path)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert not tmp.exists()
    assert not final.exists()


def test_replace_failure_keeps_final_file(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    mel = FakeMel(cmds, payload=b"body")
    removed = []

    def remove_fn(path):
        removed.append(os.path.basename(path))
        os.remove(path)

    def boom(src, dst):
        with open(dst, "wb") as handle:
            handle.write(b"leave-this-final")
        raise RuntimeError("replace failed")

    final, tmp = _paths(meshes)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel, replace_fn=boom, remove_fn=remove_fn)
    assert_safe_error(caught.value, tmp_path)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert not tmp.exists()
    assert final.read_bytes() == b"leave-this-final"
    assert tmp.name in removed
    assert final.name not in removed


@pytest.mark.parametrize(
    "shapes",
    [[], (), None, "|one|shape", b"|one", ["|ok", None], ["|ok", 1], ["|ok", b"x"]],
)
def test_bad_shapes_do_not_touch_selection(tmp_path, shapes):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    cmds.selection = ["|keep"]
    mel = FakeMel(cmds)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel, shape_paths=shapes)
    assert_safe_error(caught.value, tmp_path)
    assert cmds.selection == ["|keep"]
    assert cmds.calls == []
    assert not meshes.exists()


def test_missing_shape_does_not_change_selection(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=["|group|mesh|meshShape"], plugin_loaded=True)
    cmds.selection = ["|keep"]
    mel = FakeMel(cmds)
    final, tmp = _paths(meshes)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel)
    assert_safe_error(caught.value, tmp_path)
    assert cmds.selection == ["|keep"]
    assert not any(call[0] == "select" for call in cmds.calls)
    assert not any(call[0] == "mel" for call in cmds.calls)
    assert ("objExists", "|group|other|otherShape") in cmds.calls
    assert not tmp.exists()
    assert not final.exists()


def test_selects_parent_from_full_path_once(tmp_path):
    meshes = tmp_path / "meshes"
    shapes = ["|g|mesh|aShape", "|g|mesh|bShape"]
    cmds = FakeCmds(exists=shapes, plugin_loaded=True)
    mel = FakeMel(cmds, payload=b"same-parent")
    _export(meshes, cmds, mel, shape_paths=shapes, baseline_id="bl-parent")
    assert mel.selection_at_calls[0] == ["|g|mesh"]
    assert not any(call[0] == "listRelatives" for call in cmds.calls)


def test_walks_parent_when_only_shape_name_is_known(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(
        exists=["pCubeShape", "|onlyShape"],
        plugin_loaded=True,
        parents={
            "pCubeShape": ["|char|pCube"],
            "|onlyShape": ["|char|only"],
        },
    )
    mel = FakeMel(cmds, payload=b"walked")
    _export(
        meshes,
        cmds,
        mel,
        shape_paths=["pCubeShape", "|onlyShape"],
        baseline_id="bl-walk",
    )
    assert mel.selection_at_calls[0] == ["|char|pCube", "|char|only"]
    relatives = [call for call in cmds.calls if call[0] == "listRelatives"]
    assert relatives == [
        ("listRelatives", "pCubeShape", True, True),
        ("listRelatives", "|onlyShape", True, True),
    ]


@pytest.mark.parametrize("found", [[], ["|a|mesh", "|b|mesh"], [""], [None]])
def test_parent_walk_failure_does_not_change_selection(tmp_path, found):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(
        exists=["pCubeShape"],
        plugin_loaded=True,
        parents={"pCubeShape": found},
    )
    cmds.selection = ["|keep"]
    mel = FakeMel(cmds)
    final, tmp = _paths(meshes, "bl-noparent")
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel, shape_paths=["pCubeShape"], baseline_id="bl-noparent")
    assert_safe_error(caught.value, tmp_path)
    assert cmds.selection == ["|keep"]
    assert not any(call[0] == "select" for call in cmds.calls)
    assert mel.commands == []
    assert not tmp.exists()
    assert not final.exists()


def test_parent_walk_error_does_not_change_selection(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=["pCubeShape"], plugin_loaded=True, relatives_error=True)
    cmds.selection = ["|keep"]
    mel = FakeMel(cmds)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel, shape_paths=["pCubeShape"], baseline_id="bl-rel")
    assert_safe_error(caught.value, tmp_path)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert cmds.selection == ["|keep"]
    assert not any(call[0] == "select" for call in cmds.calls)
    assert mel.commands == []


def test_load_plugin_error(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=False, load_error=True)
    cmds.selection = ["|keep"]
    mel = FakeMel(cmds)
    final, tmp = _paths(meshes)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel)
    assert_safe_error(caught.value, tmp_path)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert cmds.selection == ["|keep"]
    assert not any(call[0] == "select" for call in cmds.calls)
    assert mel.commands == []
    assert not tmp.exists()
    assert not final.exists()
    assert [call for call in cmds.calls if call[0] == "loadPlugin"] == [
        ("loadPlugin", "fbxmaya", True)
    ]


def test_existing_final_is_immutable(tmp_path):
    meshes = tmp_path / "meshes"
    meshes.mkdir()
    final, tmp = _paths(meshes)
    final.write_bytes(b"original-bytes")
    os.utime(final, ns=(10**18, 10**18))
    stamp = final.stat().st_mtime_ns
    tmp.write_bytes(b"stale-tmp")
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    cmds.selection = ["|keep"]
    mel = FakeMel(cmds, payload=b"should-not-write")
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel)
    assert_safe_error(caught.value, tmp_path)
    assert final.read_bytes() == b"original-bytes"
    assert final.stat().st_mtime_ns == stamp
    assert not tmp.exists()
    assert mel.commands == []
    assert cmds.calls == []
    assert cmds.selection == ["|keep"]


def test_leftover_tmp_is_replaced(tmp_path):
    meshes = tmp_path / "meshes"
    meshes.mkdir()
    final, tmp = _paths(meshes)
    tmp.write_bytes(b"stale-from-failed-export")
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    payload = b"fresh-export"
    mel = FakeMel(cmds, payload=payload)
    result = _export(meshes, cmds, mel)
    assert not tmp.exists()
    assert final.read_bytes() == payload
    assert result["sha1"] == hashlib.sha1(payload).hexdigest()
    assert result["size"] == len(payload)


@pytest.mark.parametrize("payload_size", [100, 1023, 1024, 1025, 2048, 4096])
def test_sha1_streams_across_chunks(tmp_path, monkeypatch, payload_size):
    monkeypatch.setattr(um, "REBASE_FBX_CHUNK", 1024)
    reads = []
    real_open = open

    def spy(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if "b" in mode and "r" in mode and str(file).endswith(".fbx.tmp"):
            original = handle.read

            def read(n=-1):
                reads.append(n)
                return original(n)

            handle.read = read
        return handle

    monkeypatch.setattr("builtins.open", spy)
    payload = bytes((index * 17) % 256 for index in range(payload_size))
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    mel = FakeMel(cmds, payload=payload)
    final, tmp = _paths(meshes, "bl-chunk")
    result = _export(meshes, cmds, mel, baseline_id="bl-chunk")
    assert result["sha1"] == hashlib.sha1(payload).hexdigest()
    assert result["size"] == payload_size
    assert final.read_bytes() == payload
    assert not tmp.exists()
    assert reads
    assert all(size == 1024 for size in reads)
    assert len(reads) == ((payload_size + 1023) // 1024) + 1


def test_plugin_load_once_before_mel_and_skipped_when_loaded(tmp_path):
    meshes = tmp_path / "meshes"
    missing = FakeCmds(exists=_SHAPES, plugin_loaded=False)
    mel = FakeMel(missing, payload=b"plugin")
    _export(meshes, missing, mel, baseline_id="bl-load")
    names = [call[0] for call in missing.calls]
    assert names.count("loadPlugin") == 1
    assert names.index("loadPlugin") < names.index("mel")
    load = [call for call in missing.calls if call[0] == "loadPlugin"][0]
    assert load == ("loadPlugin", "fbxmaya", True)
    info = [call for call in missing.calls if call[0] == "pluginInfo"][0]
    assert info == ("pluginInfo", "fbxmaya", True, True)
    assert names.index("pluginInfo") < names.index("loadPlugin")
    assert names.index("objExists") < names.index("ls")
    assert names.index("ls") < names.index("select")
    assert names.index("select") < names.index("mel")

    loaded = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    mel_loaded = FakeMel(loaded, payload=b"already")
    _export(meshes, loaded, mel_loaded, baseline_id="bl-loaded")
    assert not any(call[0] == "loadPlugin" for call in loaded.calls)
    assert any(call[0] == "pluginInfo" for call in loaded.calls)


def test_size_mismatch_removes_final(tmp_path):
    meshes = tmp_path / "meshes"
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    payload = b"0123456789abcdef"
    mel = FakeMel(cmds, payload=payload)

    def shrink(src, dst):
        data = open(src, "rb").read()
        with open(dst, "wb") as handle:
            handle.write(data + b"EXTRA")
        os.remove(src)

    final, tmp = _paths(meshes)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(meshes, cmds, mel, replace_fn=shrink)
    assert_safe_error(caught.value, tmp_path)
    assert not final.exists()
    assert not tmp.exists()


def test_default_cmds_without_maya_is_export_error(tmp_path):
    meshes = tmp_path / "meshes"
    with pytest.raises(um.RebaseExportError) as caught:
        um.export_rebase_mesh(["|group|meshShape"], meshes, "bl-1")
    assert_safe_error(caught.value, tmp_path)
    assert not (meshes / "bl-1.fbx").exists()
    assert not (meshes / "bl-1.fbx.tmp").exists()


def test_meshes_dir_not_a_directory(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_bytes(b"x")
    cmds = FakeCmds(exists=_SHAPES, plugin_loaded=True)
    mel = FakeMel(cmds)
    with pytest.raises(um.RebaseExportError) as caught:
        _export(blocker, cmds, mel)
    assert_safe_error(caught.value, tmp_path)
    assert blocker.read_bytes() == b"x"
    assert mel.commands == []
