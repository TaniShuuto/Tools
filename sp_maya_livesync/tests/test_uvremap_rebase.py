# -*- coding: utf-8 -*-
"""uvremap_rebase.py のテスト(Maya 無し・Pillow 無し)。

時刻と乱数は引数で渡す。ファイルは tmp_path だけを使う。
"""
from __future__ import annotations

import ast
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uvremap_rebase as rb  # noqa: E402

RID = "rb-20261019-053400-3fa2"
SHA1 = "a" * 40
MESH_PATH = "C:/SPMayaLiveSync/uvremap/meshes/body.fbx"
CREATED = 1792330535.4
TTL = 1800


def _rid(index: int) -> str:
    return "rb-20261019-{0:06d}-{1:04x}".format(index, index)


def _mesh(path=MESH_PATH, size=123456, sha1=SHA1):
    return {"path": path, "size": size, "sha1": sha1}


def _build(**over):
    kwargs = dict(
        request_id=RID,
        project_key="project-key",
        created_at=CREATED,
        ttl_s=TTL,
        mesh=_mesh(),
        baseline_id="bl-1",
        ts_names=["Body"],
    )
    kwargs.update(over)
    return rb.build_request(**kwargs)


def _valid_raw(**over):
    raw = {
        "schema": 1,
        "request_id": RID,
        "project_key": "project-key",
        "created_at": CREATED,
        "expires_at": CREATED + TTL,
        "mesh": _mesh(),
        "baseline_id": "bl-1",
        "reload": {
            "preserve_strokes": True,
            "import_cameras": False,
            "auto_unwrap": False,
        },
        "final_full": True,
        "ts_names": ["Body"],
    }
    raw.update(over)
    return raw


def _put_request(root, rid, created_at, ttl_s, project="proj", final_full=True):
    request = rb.build_request(
        request_id=rid,
        project_key=project,
        created_at=created_at,
        ttl_s=ttl_s,
        mesh=_mesh(),
        baseline_id="bl-1",
        ts_names=["Body"],
        final_full=final_full,
    )
    rb.write_request(root, request)
    return request


def _put_status(root, rid, state, updated_at, **extra):
    body = {
        "schema": 1,
        "request_id": rid,
        "state": state,
        "updated_at": updated_at,
    }
    body.update(extra)
    path = rb.status_path(root, rid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _status(state, rid=RID, **extra):
    raw = {"schema": 1, "request_id": rid, "state": state, "updated_at": 50}
    raw.update(extra)
    parsed = rb.parse_status(raw)
    assert parsed is not None
    return rb.StatusRead("ok", parsed)


def _sync(rid, **over):
    raw = {
        "schema": 1,
        "plugin_version": "1.3.0",
        "export_id": "ex-1",
        "status": "exported",
        "ok": True,
        "force_full": True,
        "request_id": rid,
    }
    raw.update(over)
    parsed = rb.parse_sync_result(raw)
    assert parsed is not None
    return parsed


def _forbidden_modules():
    found = set()
    for name in sys.modules:
        if name == "maya" or name.startswith("maya."):
            found.add(name)
        elif name == "PIL" or name.startswith("PIL."):
            found.add(name)
        elif name in ("PySide2", "PySide6") or name.startswith(("PySide2.", "PySide6.")):
            found.add(name)
    return found


# ---------------------------------------------------------------------------
# 1. import
# ---------------------------------------------------------------------------
def test_import_does_not_load_maya_qt_or_pillow():
    before = _forbidden_modules()
    import importlib
    importlib.reload(rb)
    after = _forbidden_modules()
    assert after == before


def test_source_imports_maya_only_inside_functions():
    source = Path(rb.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("maya")
                assert alias.name != "uvremap_maya"
                assert alias.name != "uvremap_runtime"
                assert not alias.name.startswith("PySide")
                assert alias.name != "PIL"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("maya")
            assert mod != "uvremap_maya"
            assert mod != "uvremap_runtime"
            assert not mod.startswith("PySide")
            assert mod != "PIL"


def test_source_has_no_clock_calls():
    source = Path(rb.__file__).read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "time.time" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("maya")
                assert alias.name not in ("uvremap_maya", "uvremap_runtime", "PIL")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("maya")
            assert mod not in ("uvremap_maya", "uvremap_runtime", "PIL")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("now", "utcnow")


# ---------------------------------------------------------------------------
# 2. request_id とパス
# ---------------------------------------------------------------------------
def test_generate_request_id_format_and_rand_fn():
    when = datetime(2026, 10, 19, 5, 34, 0)
    assert rb.generate_request_id(when, rand_fn=lambda: 0x3FA2) == RID
    assert rb.generate_request_id(when, rand_fn=lambda: 0) == "rb-20261019-053400-0000"
    assert rb.generate_request_id(when, rand_fn=lambda: 65535) == "rb-20261019-053400-ffff"
    assert rb.is_valid_request_id(rb.generate_request_id(when, rand_fn=lambda: 1))


def test_generate_request_id_converts_aware_datetime_to_utc():
    jst = timezone(timedelta(hours=9))
    aware = datetime(2026, 10, 19, 14, 34, 0, tzinfo=jst)
    assert rb.generate_request_id(aware, rand_fn=lambda: 0x3FA2) == RID
    utc = datetime(2026, 10, 19, 5, 34, 0, tzinfo=timezone.utc)
    assert rb.generate_request_id(utc, rand_fn=lambda: 0x3FA2) == RID


@pytest.mark.parametrize("value,ok", [
    (RID, True),
    ("rb-20261019-053400-0000", True),
    ("rb-20261019-053400-ffff", True),
    ("rb-20261019-053400-3FA2", False),
    ("RB-20261019-053400-3fa2", False),
    ("", False),
    ("..", False),
    ("../x", False),
    ("rb-20261019-053400-3fa2/../x", False),
    ("rb-20261019-053400-3fa", False),
    (None, False),
    (1, False),
])
def test_is_valid_request_id(value, ok):
    assert rb.is_valid_request_id(value) is ok


@pytest.mark.parametrize("bad", [
    "",
    "..",
    "../x",
    "rb-20261019-053400-3fa2/../etc",
    "rb-20261019-053400-3fa2\\..\\x",
    "rb-20261019-053400-3FA2",
    "foo",
    None,
])
def test_path_rejects_unsafe_request_id(tmp_path, bad):
    before = list(tmp_path.rglob("*"))
    for fn in (rb.request_path, rb.cancel_path, rb.status_path, rb.result_path):
        with pytest.raises(ValueError):
            fn(tmp_path, bad)
    assert list(tmp_path.rglob("*")) == before


def test_paths_stay_inside_rebase_dirs(tmp_path):
    request = rb.request_path(tmp_path, RID)
    cancel = rb.cancel_path(tmp_path, RID)
    status = rb.status_path(tmp_path, RID)
    result = rb.result_path(tmp_path, RID)
    root = tmp_path.resolve()
    assert isinstance(request, Path)
    assert rb.rebase_dir(tmp_path) == tmp_path / "rebase"
    assert rb.requests_dir(tmp_path) == tmp_path / "rebase" / "requests"
    assert rb.status_dir(tmp_path) == tmp_path / "rebase" / "status"
    assert rb.results_dir(tmp_path) == tmp_path / "rebase" / "results"
    assert request == tmp_path / "rebase" / "requests" / (RID + ".json")
    assert cancel == tmp_path / "rebase" / "requests" / (RID + ".cancel")
    assert status == tmp_path / "rebase" / "status" / (RID + ".json")
    assert result == tmp_path / "rebase" / "results" / (RID + ".json")
    sub = tmp_path / "final" / "body"
    assert rb.sync_result_path(sub) == sub / "_sync_result.json"
    for path in (request, cancel, status, result):
        assert path.resolve().is_relative_to(root)
        assert ".." not in path.parts


# ---------------------------------------------------------------------------
# 3. 依頼の往復
# ---------------------------------------------------------------------------
def test_build_write_read_roundtrip(tmp_path):
    mesh = _mesh()
    names = ["Body", "Head"]
    request = rb.build_request(
        request_id=RID,
        project_key="project-key",
        created_at=CREATED,
        ttl_s=TTL,
        mesh=mesh,
        baseline_id="bl-1",
        ts_names=names,
        maya_version="2024",
        tool_version="1.3.0",
    )
    assert request["expires_at"] == CREATED + TTL
    assert request["reload"] == {
        "preserve_strokes": True,
        "import_cameras": False,
        "auto_unwrap": False,
    }
    assert request["reload"] is not rb.REQUEST_RELOAD
    assert request["mesh"]["size"] == 123456
    assert request["ts_names"] == ["Body", "Head"]
    assert request["maya_version"] == "2024"
    mesh["size"] = -1
    names.append("Extra")
    assert request["mesh"]["size"] == 123456
    assert request["ts_names"] == ["Body", "Head"]
    path = rb.write_request(tmp_path, request)
    assert path.is_file()
    assert list(path.parent.glob("*.tmp")) == []
    assert rb.read_request(tmp_path, RID) == request
    assert rb.list_requests(tmp_path) == [request]
    request["reload"]["preserve_strokes"] = False
    assert rb.REQUEST_RELOAD["preserve_strokes"] is True
    bare = _build()
    assert "maya_version" not in bare
    assert "tool_version" not in bare


@pytest.mark.parametrize("ttl,expected", [
    (59, 60),
    (86401, 86400),
    (True, 1800),
    ("1800", 1800),
    (600.0, 600),
    (60, 60),
    (86400, 86400),
])
def test_build_request_rounds_ttl(ttl, expected):
    request = _build(created_at=1000, ttl_s=ttl)
    assert request["expires_at"] == 1000 + expected


def test_build_request_rejects_bad_input():
    with pytest.raises(ValueError):
        _build(request_id="not-an-id")
    with pytest.raises(ValueError):
        _build(ts_names=[])
    with pytest.raises(ValueError):
        _build(mesh=_mesh(sha1="zz"))
    with pytest.raises(ValueError):
        _build(mesh=_mesh(size=-1))
    with pytest.raises(ValueError):
        _build(mesh=_mesh(size=True))
    with pytest.raises(ValueError):
        _build(final_full=1)
    with pytest.raises(ValueError):
        _build(maya_version=2024)


def test_list_requests_ignores_tmp_and_sorts(tmp_path):
    rb.write_request(tmp_path, _build(request_id=_rid(2), created_at=300))
    rb.write_request(tmp_path, _build(request_id=_rid(1), created_at=100))
    rb.write_request(tmp_path, _build(request_id=_rid(3), created_at=300))
    folder = rb.requests_dir(tmp_path)
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    (folder / "partial.tmp").write_text("x", encoding="utf-8")
    (folder / (_rid(9) + ".json.4321.tmp")).write_text("{}", encoding="utf-8")
    (folder / "broken.json").write_text("{", encoding="utf-8")
    (folder / (_rid(8) + ".JSON")).write_text("{}", encoding="utf-8")
    ids = [rec["request_id"] for rec in rb.list_requests(tmp_path)]
    assert ids == [_rid(1), _rid(2), _rid(3)]
    assert rb.list_requests(tmp_path / "missing") == []


def test_write_request_rejects_without_creating_a_file(tmp_path):
    with pytest.raises(ValueError):
        rb.write_request(tmp_path, {"schema": 2})
    assert not rb.requests_dir(tmp_path).exists()


# ---------------------------------------------------------------------------
# 4. 依頼の検査
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", [
    "schema",
    "request_id",
    "project_key",
    "created_at",
    "expires_at",
    "mesh",
    "baseline_id",
    "reload",
    "final_full",
    "ts_names",
])
def test_parse_request_missing_key_is_bad_request(key):
    raw = _valid_raw()
    del raw[key]
    parsed, reason = rb.parse_request(raw)
    assert parsed is None
    assert reason == "bad_request"


@pytest.mark.parametrize("key,value", [
    ("schema", 2),
    ("schema", True),
    ("schema", 1.0),
    ("schema", "1"),
    ("request_id", "nope"),
    ("request_id", 1),
    ("request_id", True),
    ("project_key", 1),
    ("project_key", None),
    ("project_key", ""),
    ("created_at", True),
    ("created_at", "100"),
    ("created_at", None),
    ("created_at", float("nan")),
    ("expires_at", False),
    ("expires_at", "100"),
    ("mesh", []),
    ("mesh", "mesh"),
    ("mesh", None),
    ("baseline_id", 1),
    ("baseline_id", None),
    ("baseline_id", ""),
    ("reload", []),
    ("reload", "reload"),
    ("reload", None),
    ("final_full", 1),
    ("final_full", "true"),
    ("final_full", None),
    ("ts_names", "Body"),
    ("ts_names", []),
    ("ts_names", None),
    ("ts_names", ["Body", 1]),
    ("ts_names", [""]),
])
def test_parse_request_bad_types_do_not_raise(key, value):
    raw = _valid_raw()
    raw[key] = value
    parsed, reason = rb.parse_request(raw)
    assert parsed is None
    assert reason == "bad_request"


@pytest.mark.parametrize("mesh", [
    {"path": 1, "size": 10, "sha1": SHA1},
    {"path": "", "size": 10, "sha1": SHA1},
    {"path": MESH_PATH, "size": -1, "sha1": SHA1},
    {"path": MESH_PATH, "size": True, "sha1": SHA1},
    {"path": MESH_PATH, "size": 10.0, "sha1": SHA1},
    {"path": MESH_PATH, "size": "10", "sha1": SHA1},
    {"path": MESH_PATH, "size": 10, "sha1": "abc"},
    {"path": MESH_PATH, "size": 10, "sha1": "g" * 40},
    {"path": MESH_PATH, "size": 10},
    {},
])
def test_parse_request_bad_mesh_is_bad_request(mesh):
    parsed, reason = rb.parse_request(_valid_raw(mesh=mesh))
    assert parsed is None
    assert reason == "bad_request"


@pytest.mark.parametrize("reload", [
    {"preserve_strokes": False, "import_cameras": False, "auto_unwrap": False},
    {"preserve_strokes": True, "import_cameras": 1, "auto_unwrap": False},
    {"preserve_strokes": True, "import_cameras": False, "auto_unwrap": "false"},
    {"preserve_strokes": True, "import_cameras": True, "auto_unwrap": False},
    {"preserve_strokes": True, "import_cameras": False, "auto_unwrap": True},
    {"preserve_strokes": True, "import_cameras": False},
    {},
])
def test_parse_request_reload_mismatch_is_bad_settings(reload):
    parsed, reason = rb.parse_request(_valid_raw(reload=reload))
    assert parsed is None
    assert reason == "bad_settings"


@pytest.mark.parametrize("baseline_id", [
    "b",
    "bl-1",
    "A_b-9",
    "0",
    "a" * 80,
    "Z" + ("_" * 79),
])
def test_baseline_id_matches_shared_pattern(baseline_id):
    parsed, reason = rb.parse_request(_valid_raw(baseline_id=baseline_id))
    assert reason is None
    assert parsed["baseline_id"] == baseline_id
    assert _build(baseline_id=baseline_id)["baseline_id"] == baseline_id


@pytest.mark.parametrize("baseline_id", [
    "",
    "-abc",
    "_abc",
    "a" * 81,
    "has space",
    "foo/bar",
    "foo\\bar",
    "..",
    "bl.1",
    "日本語",
])
def test_baseline_id_rejects_shared_pattern(baseline_id):
    parsed, reason = rb.parse_request(_valid_raw(baseline_id=baseline_id))
    assert parsed is None
    assert reason == "bad_request"
    with pytest.raises(ValueError):
        _build(baseline_id=baseline_id)


@pytest.mark.parametrize("created_at,expires_at", [
    (100, 100),
    (100, 99),
    (100.0, 100.0),
    (100.5, 100.5),
])
def test_parse_request_expires_must_be_after_created(created_at, expires_at):
    parsed, reason = rb.parse_request(_valid_raw(created_at=created_at, expires_at=expires_at))
    assert parsed is None
    assert reason == "bad_request"


def test_parse_request_accepts_expires_just_after_created(tmp_path):
    parsed, reason = rb.parse_request(_valid_raw(created_at=100, expires_at=100.1))
    assert reason is None
    assert parsed["expires_at"] == 100.1
    with pytest.raises(ValueError):
        rb.write_request(tmp_path, _valid_raw(created_at=100, expires_at=100))
    assert not rb.requests_dir(tmp_path).exists()


def test_parse_request_bad_request_wins_over_bad_settings():
    raw = _valid_raw(schema=2)
    raw["reload"] = {"preserve_strokes": False, "import_cameras": False, "auto_unwrap": False}
    assert rb.parse_request(raw) == (None, "bad_request")


def test_parse_request_ignores_unknown_keys_and_keeps_string_versions():
    raw = _valid_raw(painter_extra={"a": 1}, maya_version="2024", tool_version=3)
    raw["mesh"] = _mesh(sha1="AB" * 20)
    parsed, reason = rb.parse_request(raw)
    assert reason is None
    assert parsed is not None
    assert "painter_extra" not in parsed
    assert parsed["maya_version"] == "2024"
    assert "tool_version" not in parsed
    assert parsed["mesh"]["sha1"] == "ab" * 20
    assert parsed["reload"] == dict(rb.REQUEST_RELOAD)
    assert parsed["reload"] is not rb.REQUEST_RELOAD
    assert rb.parse_request(None) == (None, "bad_request")
    assert rb.parse_request([]) == (None, "bad_request")


# ---------------------------------------------------------------------------
# 4b. uv_old (RB-a2)
# ---------------------------------------------------------------------------
def test_uv_old_absent_keeps_previous_shape():
    parsed, reason = rb.parse_request(_valid_raw())
    assert reason is None
    assert "uv_old" not in parsed
    assert "uv_old" not in _build()
    assert "uv_old" not in _build(uv_old=None)


def test_parse_request_keeps_explicit_uv_old_null():
    parsed, reason = rb.parse_request(_valid_raw(uv_old=None))
    assert reason is None
    assert parsed is not None
    assert "uv_old" in parsed
    assert parsed["uv_old"] is None


def test_uv_old_roundtrip_write_read_list(tmp_path):
    first = _build(uv_old={"name": "uvOld", "index": 1})
    assert first["uv_old"] == {"name": "uvOld", "index": 1}
    rb.write_request(tmp_path, first)
    assert rb.read_request(tmp_path, RID)["uv_old"] == {"name": "uvOld", "index": 1}
    assert rb.list_requests(tmp_path)[0]["uv_old"] == {"name": "uvOld", "index": 1}

    second = _build(request_id=_rid(1), uv_old={"name": "uvOld_1", "index": 7})
    assert second["uv_old"] == {"name": "uvOld_1", "index": 7}
    rb.write_request(tmp_path, second)
    assert rb.read_request(tmp_path, _rid(1))["uv_old"] == {"name": "uvOld_1", "index": 7}


def test_parse_request_drops_unknown_keys_inside_uv_old():
    raw = _valid_raw(uv_old={"name": "uvOld", "index": 1, "extra": True, "path": "x"})
    parsed, reason = rb.parse_request(raw)
    assert reason is None
    assert parsed["uv_old"] == {"name": "uvOld", "index": 1}
    built = _build(uv_old={"name": "uvOld", "index": 2, "unused": 0})
    assert built["uv_old"] == {"name": "uvOld", "index": 2}


def test_uv_old_name_of_64_chars_is_valid():
    name64 = "A" + ("a" * 63)
    assert len(name64) == 64
    parsed, reason = rb.parse_request(_valid_raw(uv_old={"name": name64, "index": 1}))
    assert reason is None
    assert parsed["uv_old"] == {"name": name64, "index": 1}
    assert _build(uv_old={"name": name64, "index": 1})["uv_old"]["name"] == name64


@pytest.mark.parametrize("value", [
    "uvOld",
    ["uvOld"],
    1,
    True,
    False,
    {"index": 1},
    {"name": "uvOld"},
    {"name": 1, "index": 1},
    {"name": "", "index": 1},
    {"name": "A" + ("a" * 64), "index": 1},
    {"name": "1abc", "index": 1},
    {"name": "a-b", "index": 1},
    {"name": "a b", "index": 1},
    {"name": "日本語", "index": 1},
    {"name": "uvOld\n", "index": 1},
    {"name": None, "index": 1},
    {"name": "uvOld", "index": 0},
    {"name": "uvOld", "index": 8},
    {"name": "uvOld", "index": -1},
    {"name": "uvOld", "index": 1.0},
    {"name": "uvOld", "index": "1"},
    {"name": "uvOld", "index": True},
    {"name": "uvOld", "index": None},
])
def test_invalid_uv_old_is_bad_request_and_write_creates_nothing(tmp_path, value):
    parsed, reason = rb.parse_request(_valid_raw(uv_old=value))
    assert parsed is None
    assert reason == "bad_request"
    with pytest.raises(ValueError):
        _build(uv_old=value)
    with pytest.raises(ValueError):
        rb.write_request(tmp_path, _valid_raw(uv_old=value))
    assert not rb.requests_dir(tmp_path).exists()


def test_bad_uv_old_wins_over_bad_settings():
    raw = _valid_raw(uv_old="uvOld")
    raw["reload"] = {
        "preserve_strokes": False,
        "import_cameras": False,
        "auto_unwrap": False,
    }
    assert rb.parse_request(raw) == (None, "bad_request")


def test_parse_request_without_uv_old_matches_previous_result():
    raw = {
        "schema": 1,
        "request_id": RID,
        "project_key": "project-key",
        "created_at": CREATED,
        "expires_at": CREATED + TTL,
        "mesh": {"path": MESH_PATH, "size": 123456, "sha1": SHA1},
        "baseline_id": "bl-1",
        "reload": {
            "preserve_strokes": True,
            "import_cameras": False,
            "auto_unwrap": False,
        },
        "final_full": True,
        "ts_names": ["Body"],
    }
    parsed, reason = rb.parse_request(raw)
    assert reason is None
    assert parsed == {
        "schema": 1,
        "request_id": RID,
        "project_key": "project-key",
        "created_at": CREATED,
        "expires_at": CREATED + TTL,
        "mesh": {"path": MESH_PATH, "size": 123456, "sha1": SHA1},
        "baseline_id": "bl-1",
        "reload": {
            "preserve_strokes": True,
            "import_cameras": False,
            "auto_unwrap": False,
        },
        "final_full": True,
        "ts_names": ["Body"],
    }
    assert rb.SCHEMA == 1
    assert "UV_OLD_NAME_RE" in rb.__all__
    assert "UV_OLD_NAME_MAX" in rb.__all__
    assert "UV_OLD_INDEX_MIN" in rb.__all__
    assert "UV_OLD_INDEX_MAX" in rb.__all__
    assert rb.UV_OLD_NAME_MAX == 64
    assert rb.UV_OLD_INDEX_MIN == 1
    assert rb.UV_OLD_INDEX_MAX == 7


@pytest.mark.parametrize("payload", [
    b"{",
    b"",
    b"\xef\xbb\xbf" + b"{}",
    b"\xff\xfe\x00",
])
def test_read_request_unreadable_bytes_are_none(tmp_path, payload):
    path = rb.request_path(tmp_path, RID)
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    assert rb.read_request(tmp_path, RID) is None


def test_read_request_id_mismatch_is_none(tmp_path):
    path = rb.request_path(tmp_path, _rid(1))
    path.parent.mkdir(parents=True)
    body = _valid_raw(request_id=_rid(2))
    path.write_text(json.dumps(body), encoding="utf-8")
    assert rb.read_request(tmp_path, _rid(1)) is None
    assert rb.read_request(tmp_path, "../x") is None
    assert rb.read_request(tmp_path, _rid(3)) is None


def test_read_request_bom_prefixed_json_is_none(tmp_path):
    path = rb.request_path(tmp_path, RID)
    path.parent.mkdir(parents=True)
    payload = json.dumps(_valid_raw()).encode("utf-8")
    path.write_bytes(b"\xef\xbb\xbf" + payload)
    assert rb.read_request(tmp_path, RID) is None


# ---------------------------------------------------------------------------
# 5. 取り消し
# ---------------------------------------------------------------------------
def test_write_cancel_is_empty_and_idempotent(tmp_path):
    assert rb.cancel_exists(tmp_path, RID) is False
    path = rb.write_cancel(tmp_path, RID)
    assert path.is_file()
    assert path.read_bytes() == b""
    assert list(path.parent.glob("*.tmp")) == []
    again = rb.write_cancel(tmp_path, RID)
    assert again == path
    assert path.read_bytes() == b""
    assert rb.cancel_exists(tmp_path, RID) is True
    assert rb.cancel_exists(tmp_path, "..") is False
    with pytest.raises(ValueError):
        rb.write_cancel(tmp_path, "..")


def test_write_cancel_leaves_existing_file_and_uses_replace_file(tmp_path, monkeypatch):
    seen = []
    real = rb.uvremap_io.replace_file

    def spy(src, dst, **kwargs):
        seen.append(Path(dst))
        return real(src, dst, **kwargs)

    monkeypatch.setattr(rb.uvremap_io, "replace_file", spy)
    path = rb.write_cancel(tmp_path, RID)
    assert seen == [path]
    assert path.read_bytes() == b""
    assert list(path.parent.glob("*.tmp")) == []
    path.write_bytes(b"keep")
    seen.clear()
    again = rb.write_cancel(tmp_path, RID)
    assert again == path
    assert path.read_bytes() == b"keep"
    assert seen == []


def test_write_cancel_removes_tmp_when_replace_fails(tmp_path, monkeypatch):
    def boom(src, dst, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(rb.uvremap_io, "replace_file", boom)
    with pytest.raises(OSError):
        rb.write_cancel(tmp_path, RID)
    folder = rb.requests_dir(tmp_path)
    assert list(folder.glob("*.tmp")) == []
    assert not rb.cancel_path(tmp_path, RID).exists()


# ---------------------------------------------------------------------------
# 6. 状態の読み取り
# ---------------------------------------------------------------------------
def test_read_status_all_states_are_ok(tmp_path):
    rid = _rid(4)
    for state in rb.STATES:
        _put_status(tmp_path, rid, state, 10)
        got = rb.read_status(tmp_path, rid)
        assert got.kind == "ok"
        assert got.status["state"] == state
        assert got.status["schema"] == 1


def test_read_status_missing_broken_mismatch_and_bad_fields(tmp_path):
    assert rb.read_status(tmp_path, _rid(5)).kind == "missing"
    assert rb.read_status(tmp_path, _rid(5)).status is None
    assert rb.read_status(tmp_path, "../x").kind == "unreadable"

    rid = _rid(6)
    path = rb.status_path(tmp_path, rid)
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    assert rb.read_status(tmp_path, rid).kind == "unreadable"

    path.write_text(json.dumps({
        "schema": 1,
        "request_id": _rid(7),
        "state": "done",
        "updated_at": 1,
    }), encoding="utf-8")
    assert rb.read_status(tmp_path, rid).kind == "unreadable"

    path.write_text(json.dumps({
        "schema": 1,
        "request_id": rid,
        "state": "nope",
        "updated_at": 1,
    }), encoding="utf-8")
    assert rb.read_status(tmp_path, rid).kind == "unreadable"

    path.write_text(json.dumps({
        "schema": 1,
        "request_id": rid,
        "state": "done",
        "updated_at": True,
    }), encoding="utf-8")
    assert rb.read_status(tmp_path, rid).kind == "unreadable"


def test_parse_status_backup_name_and_message_limit():
    backup = rb.parse_status({
        "schema": 1,
        "request_id": RID,
        "state": "failed",
        "updated_at": 1,
        "backup_name": "C:\\SPMayaLiveSync\\uvremap\\backup\\scene.spp",
        "message": "あ" * 501,
        "plugin_version": 1,
        "reason": None,
        "result_export_id": "ex-9",
        "unknown": 1,
    })
    assert backup is not None
    assert backup["backup_name"] == "scene.spp"
    assert backup["message"] == "あ" * 500
    assert len(backup["message"]) == 500
    assert backup["plugin_version"] is None
    assert backup["reason"] is None
    assert backup["result_export_id"] == "ex-9"
    assert "unknown" not in backup
    slash = rb.parse_status({
        "schema": 1,
        "request_id": RID,
        "state": "failed",
        "updated_at": 1.5,
        "backup_name": "a/b/c.spp",
    })
    assert slash["backup_name"] == "c.spp"
    assert slash["message"] == ""
    assert rb.parse_status({
        "schema": True,
        "request_id": RID,
        "state": "done",
        "updated_at": 1,
    }) is None
    assert rb.parse_status({
        "schema": 1,
        "request_id": RID,
        "state": "done",
        "updated_at": False,
    }) is None


@pytest.mark.parametrize("uv_fills,expected", [
    ({"direction": 2}, 2),
    ({"direction": 0}, 0),
    (None, 0),
    ({"direction": "2"}, 0),
    ({"direction": True}, 0),
    ({"direction": -1}, 0),
])
def test_parse_status_direction_fills(uv_fills, expected):
    raw = {
        "schema": 1,
        "request_id": RID,
        "state": "done",
        "updated_at": 1,
        "message": "残す",
        "unknown": 1,
    }
    if uv_fills is not None:
        raw["uv_fills"] = uv_fills
    parsed = rb.parse_status(raw)
    assert parsed is not None
    assert parsed["direction_fills"] == expected
    assert parsed["message"] == "残す"
    assert parsed["state"] == "done"
    assert "uv_fills" not in parsed
    assert "unknown" not in parsed


# ---------------------------------------------------------------------------
# 7. decide_display
# ---------------------------------------------------------------------------
_DISPLAY = {
    "received": ("working", rb.MSG_PREPARING, True, False),
    "awaiting_approval": ("waiting", rb.MSG_AWAITING_APPROVAL, True, False),
    "preparing": ("working", rb.MSG_PREPARING, True, False),
    "backing_up": ("locked", rb.MSG_WORKING_LOCKED, False, False),
    "reloading": ("locked", rb.MSG_WORKING_LOCKED, False, False),
    "exporting": ("locked", rb.MSG_WORKING_LOCKED, False, False),
    "done": ("done", rb.MSG_DONE, False, True),
    "failed": ("failed", "リベースに失敗しました。", False, True),
    "declined": ("ended", rb.MSG_DECLINED, False, True),
    "rejected": ("ended", rb.MSG_REJECTED, False, True),
    "expired": ("ended", rb.MSG_EXPIRED, False, True),
    "superseded": ("ended", rb.MSG_SUPERSEDED, False, True),
}


@pytest.mark.parametrize("state", list(rb.STATES))
def test_decide_display_for_each_state(state):
    request = _build(created_at=1000, ttl_s=1800)
    shown = rb.decide_display(request, _status(state), now=1000, cancel_requested=False)
    phase, text, can_cancel, terminal = _DISPLAY[state]
    assert shown.phase == phase
    assert shown.text == text
    assert shown.can_cancel is can_cancel
    assert shown.terminal is terminal
    assert shown.show_done_guidance is (state == "done")


@pytest.mark.parametrize("state", ["received", "awaiting_approval"])
def test_decide_display_prefers_wait_message(state):
    request = _build(created_at=1000, ttl_s=1800)
    shown = rb.decide_display(
        request,
        _status(state, message="Painter で保存してください"),
        now=1000,
        cancel_requested=False,
    )
    assert shown.text == "Painter で保存してください"
    assert shown.can_cancel is True


@pytest.mark.parametrize("state", ["received", "awaiting_approval", "preparing"])
def test_decide_display_canceling_while_cancelable(state):
    request = _build(created_at=1000, ttl_s=1800)
    shown = rb.decide_display(request, _status(state), now=1000, cancel_requested=True)
    assert shown.text == rb.MSG_CANCELING
    assert shown.phase == "canceling"
    assert shown.can_cancel is False
    assert shown.terminal is False
    assert shown.show_done_guidance is False


def test_decide_display_locked_ignores_cancel_request():
    request = _build(created_at=1000, ttl_s=1800)
    shown = rb.decide_display(
        request, _status("reloading"), now=1000, cancel_requested=True,
    )
    assert shown.text == rb.MSG_WORKING_LOCKED
    assert shown.phase == "locked"
    assert shown.can_cancel is False
    assert shown.terminal is False


def test_decide_display_missing_and_unreadable(tmp_path):
    request = _build(created_at=1000, ttl_s=1800)
    assert request["expires_at"] == 2800
    missing = rb.StatusRead("missing", None)
    waiting = rb.decide_display(request, missing, now=2800, cancel_requested=False)
    assert waiting.text == rb.MSG_WAITING
    assert waiting.phase == "waiting"
    assert waiting.can_cancel is True
    assert waiting.terminal is False
    expired = rb.decide_display(request, missing, now=2800.1, cancel_requested=False)
    assert expired.text == rb.MSG_EXPIRED
    assert expired.phase == "ended"
    assert expired.terminal is True
    assert expired.can_cancel is False
    expired_cancel = rb.decide_display(request, missing, now=3000, cancel_requested=True)
    assert expired_cancel.text == rb.MSG_EXPIRED
    assert expired_cancel.can_cancel is False
    canceling = rb.decide_display(request, missing, now=1000, cancel_requested=True)
    assert canceling.text == rb.MSG_CANCELING
    assert canceling.phase == "canceling"
    assert canceling.can_cancel is False

    bad = rb.StatusRead("unreadable", None)
    unreadable = rb.decide_display(request, bad, now=1000, cancel_requested=False)
    assert unreadable.text == rb.MSG_STATUS_UNREADABLE
    assert unreadable.phase == "unreadable"
    assert unreadable.can_cancel is False
    later = rb.decide_display(request, bad, now=3000, cancel_requested=True)
    assert later.text == rb.MSG_EXPIRED
    assert later.terminal is True
    assert later.can_cancel is False


def test_decide_display_failed_backup_is_filename_only_and_done_guides():
    request = _build(created_at=1000, ttl_s=1800)
    failed = rb.decide_display(
        request,
        _status(
            "failed",
            message="ディスクが一杯です",
            backup_name="C:\\SPMayaLiveSync\\uvremap\\backup\\scene.spp",
        ),
        now=1000,
        cancel_requested=False,
    )
    assert failed.text == "リベースに失敗しました。ディスクが一杯です バックアップ: scene.spp"
    assert "SPMayaLiveSync" not in failed.text
    assert failed.phase == "failed"
    assert failed.show_done_guidance is False
    rejected = rb.decide_display(
        request,
        _status("rejected", message="メッシュが違います"),
        now=1000,
        cancel_requested=False,
    )
    assert rejected.text == rb.MSG_REJECTED + " メッシュが違います"
    done = rb.decide_display(request, _status("done"), now=1000, cancel_requested=False)
    assert done.show_done_guidance is True
    assert done.text == rb.MSG_DONE


# ---------------------------------------------------------------------------
# 8. is_request_open / find_open_requests
# ---------------------------------------------------------------------------
def test_is_request_open_terminal_missing_and_expiry():
    request = _build(created_at=1000, ttl_s=1800)
    assert rb.is_request_open(request, _status("done"), 1000) is False
    assert rb.is_request_open(request, _status("failed"), 1000) is False
    assert rb.is_request_open(request, _status("superseded"), 1000) is False
    assert rb.is_request_open(request, _status("preparing"), 99999) is True
    missing = rb.StatusRead("missing", None)
    assert rb.is_request_open(request, missing, 2800) is True
    assert rb.is_request_open(request, missing, 2800.1) is False
    bad = rb.StatusRead("unreadable", None)
    assert rb.is_request_open(request, bad, 2800) is True
    assert rb.is_request_open(request, bad, 2801) is False


def test_find_open_requests_filters_project_and_sorts(tmp_path):
    now = 4000
    early = _put_request(tmp_path, _rid(1), created_at=1000, ttl_s=1800, project="proj")
    _put_status(tmp_path, _rid(1), "preparing", 1000)
    _put_request(tmp_path, _rid(2), created_at=2000, ttl_s=1800, project="other")
    closed = _rid(3)
    _put_request(tmp_path, closed, created_at=1500, ttl_s=1800, project="proj")
    _put_status(tmp_path, closed, "done", 1600)
    waiting = _put_request(tmp_path, _rid(4), created_at=3000, ttl_s=1800, project="proj")
    expired = _rid(5)
    _put_request(tmp_path, expired, created_at=100, ttl_s=60, project="proj")
    unreadable = _put_request(tmp_path, _rid(6), created_at=2500, ttl_s=1800, project="proj")
    status = rb.status_path(tmp_path, _rid(6))
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text("{", encoding="utf-8")
    found = rb.find_open_requests(tmp_path, "proj", now)
    assert [rec["request_id"] for rec in found] == [_rid(1), _rid(6), _rid(4)]
    assert found[0] == early
    assert found[1]["request_id"] == unreadable["request_id"]
    assert found[2] == waiting
    assert rb.find_open_requests(tmp_path / "missing", "proj", now) == []


# ---------------------------------------------------------------------------
# 9. judge_completion
# ---------------------------------------------------------------------------
def _judge(**over):
    request = over.pop("request", None)
    if request is None:
        request = _build()
    kwargs = dict(
        status_read=_status("done", rid=request["request_id"]),
        sync_result=_sync(request["request_id"]),
        request=request,
        enabled=True,
        scene_id_now="scene-a",
        scene_id_request="scene-a",
        already_completed=False,
    )
    kwargs.update(over)
    return rb.judge_completion(**kwargs)


def test_judge_completion_each_branch():
    assert ( _judge(already_completed=True).action, _judge(already_completed=True).reason) == (
        "skip", "already_completed",
    )
    missing = _judge(status_read=rb.StatusRead("missing", None))
    assert (missing.action, missing.reason) == ("wait", "not_done")
    preparing = _judge(status_read=_status("preparing"))
    assert (preparing.action, preparing.reason) == ("wait", "not_done")
    failed = _judge(status_read=_status("failed"))
    assert (failed.action, failed.reason) == ("skip", "terminal_not_done")
    no_result = _judge(sync_result=None)
    assert (no_result.action, no_result.reason) == ("skip", "result_missing")
    not_ok = _judge(sync_result=_sync(RID, ok=False))
    assert (not_ok.action, not_ok.reason) == ("skip", "result_not_ok")
    mismatch = _judge(sync_result=_sync(_rid(9)))
    assert (mismatch.action, mismatch.reason) == ("skip", "result_request_id_mismatch")
    off = _judge(enabled=False)
    assert (off.action, off.reason) == ("skip", "killswitch_off")
    moved = _judge(scene_id_now="scene-b")
    assert (moved.action, moved.reason) == ("skip", "scene_changed")
    not_full = _judge(sync_result=_sync(RID, force_full=False))
    assert (not_full.action, not_full.reason) == ("skip", "not_full_final")
    done = _judge()
    assert (done.action, done.reason) == ("complete", "ok")


def test_judge_completion_priority_and_scene_and_final_full():
    not_done = _judge(status_read=_status("preparing"), enabled=False, sync_result=None)
    assert (not_done.action, not_done.reason) == ("wait", "not_done")
    terminal = _judge(status_read=_status("failed"), enabled=False, sync_result=None)
    assert (terminal.action, terminal.reason) == ("skip", "terminal_not_done")
    first = _judge(already_completed=True, status_read=_status("failed"))
    assert first.reason == "already_completed"
    both = _judge(sync_result=_sync(_rid(8), ok=False))
    assert both.reason == "result_not_ok"
    missing_off = _judge(sync_result=None, enabled=False)
    assert missing_off.reason == "result_missing"
    same = _judge(scene_id_now=None, scene_id_request="")
    assert (same.action, same.reason) == ("complete", "ok")
    flipped = _judge(scene_id_now="", scene_id_request=None)
    assert flipped.reason == "ok"
    request = _build(final_full=False)
    partial = _judge(request=request, sync_result=_sync(request["request_id"], force_full=False))
    assert (partial.action, partial.reason) == ("complete", "ok")


# ---------------------------------------------------------------------------
# 10. plugin_supports_rebase
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("version,ok", [
    ("1.2.0", False),
    ("1.3.0", False),
    ("1.3.9", False),
    ("1.4", True),
    ("1.4.0", True),
    ("1.4.3", True),
    ("1.10.0", True),
    ("", False),
    ("abc", False),
    ("nope", False),
    ("1.4.0-beta", True),
    ("1.2.0a", False),
])
def test_plugin_supports_rebase(version, ok):
    assert rb.plugin_supports_rebase({"plugin_version": version}) is ok


@pytest.mark.parametrize("sync_result", [None, [], "1.4.0", 1, {"plugin_version": None}])
def test_plugin_supports_rebase_rejects_non_dict_and_unreadable(sync_result):
    assert rb.plugin_supports_rebase(sync_result) is False


def test_rebase_version_floor_is_separate_from_result_floor():
    assert rb.MIN_PLUGIN_VERSION == (1, 3, 0)
    assert rb.MIN_REBASE_PLUGIN_VERSION == (1, 4, 0)
    assert "MIN_REBASE_PLUGIN_VERSION" in rb.__all__
    assert "MSG_DONE_DIRECTION_FMT" in rb.__all__
    assert rb.MSG_PLUGIN_UPDATE_REQUIRED == (
        "Painter プラグインの更新が必要です(1.4.0 以上)。"
        "更新済みの場合は、Painter のパネルで「今すぐ全量同期(Final)」を 1 回実行してから送ってください"
    )
    assert rb.MSG_DONE_DIRECTION_FMT.format(n=2) == (
        "法線(または異方性の角度)を含む UV 投影のフィルが 2 件あります。"
        "UV を回転・反転した島では陰影が狂うことがあります。"
        "Painter の履歴で対象を確認し、必要なら法線を描き直すか再ベイクしてください"
    )


def test_parse_version_is_numeric_not_lexical():
    assert rb.parse_version("1.3.0") == (1, 3, 0)
    assert rb.parse_version("1.10.0") == (1, 10, 0)
    assert rb.parse_version("1.10.0") > rb.parse_version("1.3.0")
    assert "1.10.0" < "1.3.0"
    assert rb.parse_version("1.4.0-beta") == (1, 4, 0)
    assert rb.parse_version("v1.3.0") is None
    assert rb.parse_version(None) is None


# ---------------------------------------------------------------------------
# 11. sync_result
# ---------------------------------------------------------------------------
def test_parse_sync_result_rejects_non_bool_ok_and_keeps_null_request_id():
    base = {
        "schema": 1,
        "plugin_version": "1.3.0",
        "export_id": "ex-1",
        "status": "exported",
        "ok": True,
        "force_full": True,
        "request_id": RID,
        "extra": 5,
    }
    as_text = dict(base)
    as_text["ok"] = "true"
    assert rb.parse_sync_result(as_text) is None
    as_one = dict(base)
    as_one["ok"] = 1
    assert rb.parse_sync_result(as_one) is None
    as_null = dict(base)
    as_null["request_id"] = None
    parsed = rb.parse_sync_result(as_null)
    assert parsed is not None
    assert parsed["request_id"] is None
    assert parsed["extra"] == 5
    assert parsed["ok"] is True
    missing = dict(base)
    del missing["request_id"]
    assert rb.parse_sync_result(missing) is None
    assert rb.parse_sync_result([]) is None


def test_read_sync_result_broken_file_is_none(tmp_path):
    folder = tmp_path / "final" / "sub"
    folder.mkdir(parents=True)
    assert rb.read_sync_result(folder) is None
    path = rb.sync_result_path(folder)
    path.write_bytes(b"{")
    assert rb.read_sync_result(folder) is None
    path.write_text(json.dumps({
        "schema": 1,
        "plugin_version": "1.3.0",
        "export_id": "ex-1",
        "status": "exported",
        "ok": True,
        "force_full": False,
        "request_id": None,
    }), encoding="utf-8")
    parsed = rb.read_sync_result(folder)
    assert parsed["force_full"] is False
    assert parsed["request_id"] is None


def test_read_rebase_result_parses_and_requires_matching_request_id(tmp_path):
    path = rb.result_path(tmp_path, RID)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_sync(RID)), encoding="utf-8")
    parsed = rb.read_rebase_result(tmp_path, RID)
    assert parsed is not None
    assert parsed["request_id"] == RID
    path.write_text(json.dumps(_sync(_rid(9))), encoding="utf-8")
    assert rb.read_rebase_result(tmp_path, RID) is None
    path.write_text("{", encoding="utf-8")
    assert rb.read_rebase_result(tmp_path, RID) is None


# ---------------------------------------------------------------------------
# 12. 設定の丸め
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cfg", [None, [], {}, "nope", 1])
def test_normalize_rebase_settings_does_not_raise(cfg):
    out = rb.normalize_rebase_settings(cfg)
    assert out == {
        "uvremap_rebase_ttl_s": 1800,
        "uvremap_rebase_triangulate": False,
        "uvremap_rebase_poll_s": 1.0,
    }


def test_normalize_rebase_settings_clamps_and_ignores_bool_numbers():
    original = {
        "uvremap_rebase_ttl_s": 59,
        "uvremap_rebase_triangulate": True,
        "uvremap_rebase_poll_s": 2,
        "other": 1,
    }
    out = rb.normalize_rebase_settings(original)
    assert original["other"] == 1
    assert out["uvremap_rebase_ttl_s"] == 60
    assert out["uvremap_rebase_triangulate"] is True
    assert out["uvremap_rebase_poll_s"] == 2.0
    assert set(out) == {
        "uvremap_rebase_ttl_s",
        "uvremap_rebase_triangulate",
        "uvremap_rebase_poll_s",
    }
    weird = rb.normalize_rebase_settings({
        "uvremap_rebase_ttl_s": True,
        "uvremap_rebase_triangulate": 1,
        "uvremap_rebase_poll_s": True,
    })
    assert weird["uvremap_rebase_ttl_s"] == 1800
    assert weird["uvremap_rebase_triangulate"] is False
    assert weird["uvremap_rebase_poll_s"] == 1.0
    numbers = rb.normalize_rebase_settings({
        "uvremap_rebase_ttl_s": 600.0,
        "uvremap_rebase_poll_s": 0.1,
    })
    assert numbers["uvremap_rebase_ttl_s"] == 600
    assert numbers["uvremap_rebase_poll_s"] == rb.POLL_MIN_S
    high = rb.normalize_rebase_settings({
        "uvremap_rebase_ttl_s": 600.5,
        "uvremap_rebase_poll_s": float("nan"),
    })
    assert high["uvremap_rebase_ttl_s"] == 1800
    assert high["uvremap_rebase_poll_s"] == 1.0
    capped = rb.normalize_rebase_settings({"uvremap_rebase_poll_s": float("inf")})
    assert capped["uvremap_rebase_poll_s"] == 1.0
    edge = rb.normalize_rebase_settings({"uvremap_rebase_poll_s": 30})
    assert edge["uvremap_rebase_poll_s"] == 30.0
    assert math.isclose(
        rb.normalize_rebase_settings({"uvremap_rebase_poll_s": 0.5})["uvremap_rebase_poll_s"],
        0.5,
    )


# ---------------------------------------------------------------------------
# 13. 掃除
# ---------------------------------------------------------------------------
def test_select_cleanup_targets_age_and_order():
    now = 1_000_000
    age = rb.CLEANUP_AGE_S
    rows = [
        {"request_id": _rid(2), "terminal_at": now - age},
        {"request_id": _rid(1), "terminal_at": now - age + 1},
        {"request_id": _rid(3), "terminal_at": None},
        {"request_id": _rid(4), "terminal_at": now + 1},
        {"request_id": "not-an-id", "terminal_at": 0},
    ]
    assert rb.select_cleanup_targets(rows, now) == [_rid(2)]
    assert rb.select_cleanup_targets(rows, now, max_age_s=0) == [_rid(1), _rid(2)]


def test_cleanup_keeps_fresh_open_and_unreadable_files(tmp_path):
    now = 10_000_000.0
    age = rb.CLEANUP_AGE_S
    recent = _rid(1)
    _put_request(tmp_path, recent, created_at=now - 100, ttl_s=1800)
    _put_status(tmp_path, recent, "done", now - age + 10)
    rb.write_cancel(tmp_path, recent)

    old = _rid(2)
    _put_request(tmp_path, old, created_at=now - age - 100, ttl_s=60)
    _put_status(tmp_path, old, "failed", now - age)
    rb.write_cancel(tmp_path, old)
    old_result = rb.result_path(tmp_path, old)
    old_result.parent.mkdir(parents=True, exist_ok=True)
    old_result.write_text(json.dumps(_sync(old)), encoding="utf-8")

    busy = _rid(3)
    _put_request(tmp_path, busy, created_at=1, ttl_s=60)
    _put_status(tmp_path, busy, "reloading", 1)
    rb.write_cancel(tmp_path, busy)

    junk = _rid(4)
    _put_request(tmp_path, junk, created_at=1, ttl_s=60)
    junk_status = rb.status_path(tmp_path, junk)
    junk_status.parent.mkdir(parents=True, exist_ok=True)
    junk_status.write_text("{", encoding="utf-8")

    expired = _rid(5)
    _put_request(tmp_path, expired, created_at=now - age - 60, ttl_s=60)
    rb.write_cancel(tmp_path, expired)

    fresh_expired = _rid(6)
    _put_request(tmp_path, fresh_expired, created_at=now - 120, ttl_s=60)
    waiting = _rid(7)
    _put_request(tmp_path, waiting, created_at=now, ttl_s=1800)

    mesh = tmp_path / "rebase" / "meshes" / "old.fbx"
    mesh.parent.mkdir(parents=True, exist_ok=True)
    mesh.write_bytes(b"MESH")
    baseline = tmp_path / "rebase" / "baseline" / "keep.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text("keep", encoding="utf-8")
    scratch = rb.requests_dir(tmp_path) / "scratch.tmp"
    scratch.write_text("tmp", encoding="utf-8")

    removed = rb.cleanup_rebase_files(tmp_path, now)
    assert removed == sorted([old, expired])
    for rid in (recent, busy, junk, fresh_expired, waiting):
        assert rb.request_path(tmp_path, rid).is_file()
    assert rb.cancel_path(tmp_path, recent).is_file()
    assert rb.status_path(tmp_path, recent).is_file()
    assert rb.status_path(tmp_path, busy).is_file()
    assert rb.cancel_path(tmp_path, busy).is_file()
    assert junk_status.read_text(encoding="utf-8") == "{"
    assert not rb.request_path(tmp_path, old).exists()
    assert not rb.cancel_path(tmp_path, old).exists()
    assert not rb.status_path(tmp_path, old).exists()
    assert not old_result.exists()
    assert not rb.request_path(tmp_path, expired).exists()
    assert not rb.cancel_path(tmp_path, expired).exists()
    assert mesh.read_bytes() == b"MESH"
    assert baseline.read_text(encoding="utf-8") == "keep"
    assert scratch.read_text(encoding="utf-8") == "tmp"
    assert rb.cleanup_rebase_files(tmp_path / "missing", now) == []


def test_cleanup_continues_when_remove_raises(tmp_path):
    now = 10_000_000.0
    age = rb.CLEANUP_AGE_S
    first = _rid(10)
    second = _rid(11)
    for rid in (first, second):
        _put_request(tmp_path, rid, created_at=1, ttl_s=60)
        _put_status(tmp_path, rid, "done", now - age)
        rb.write_cancel(tmp_path, rid)
    blocked = "/requests/{0}.json".format(first)

    def remove(path):
        text = str(path).replace("\\", "/")
        if text.endswith(blocked):
            raise OSError("boom")
        os.remove(path)

    removed = rb.cleanup_rebase_files(tmp_path, now, remove_fn=remove)
    assert removed == [first, second]
    assert rb.request_path(tmp_path, first).is_file()
    assert not rb.cancel_path(tmp_path, first).exists()
    assert not rb.status_path(tmp_path, first).exists()
    assert not rb.request_path(tmp_path, second).exists()
    assert not rb.status_path(tmp_path, second).exists()
    assert not rb.cancel_path(tmp_path, second).exists()


# ---------------------------------------------------------------------------
# 14. 文言
# ---------------------------------------------------------------------------
def test_message_constants_are_non_empty_and_failed_formats():
    names = (
        "MSG_WAITING",
        "MSG_PREPARING",
        "MSG_AWAITING_APPROVAL",
        "MSG_WORKING_LOCKED",
        "MSG_DONE",
        "MSG_FAILED_FMT",
        "MSG_DECLINED",
        "MSG_REJECTED",
        "MSG_EXPIRED",
        "MSG_SUPERSEDED",
        "MSG_CANCELING",
        "MSG_CANNOT_CANCEL",
        "MSG_STATUS_UNREADABLE",
        "MSG_PLUGIN_UPDATE_REQUIRED",
        "MSG_DONE_GUIDANCE",
        "MSG_DONE_DIRECTION_FMT",
        "MSG_RESULT_UNCONFIRMED",
    )
    for name in names:
        value = getattr(rb, name)
        assert isinstance(value, str)
        assert value
    assert rb.MSG_FAILED_FMT.format(message="理由") == "リベースに失敗しました。理由"
    assert rb.MSG_RESULT_UNCONFIRMED == "結果を確認できません"
    assert set(rb.CANCELABLE_STATES).isdisjoint(rb.LOCKED_STATES)
    assert set(rb.CANCELABLE_STATES).isdisjoint(rb.TERMINAL_STATES)
    assert set(rb.LOCKED_STATES).isdisjoint(rb.TERMINAL_STATES)
    assert set(rb.STATES) == set(rb.CANCELABLE_STATES) | set(rb.LOCKED_STATES) | set(rb.TERMINAL_STATES)
    assert "backing_up" in rb.LOCKED_STATES
    assert "backing_up" not in rb.CANCELABLE_STATES
