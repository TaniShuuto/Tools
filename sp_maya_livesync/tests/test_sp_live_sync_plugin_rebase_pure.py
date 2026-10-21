# -*- coding: utf-8 -*-
"""sp_live_sync_plugin 1.4.0 PB-a のリベース純関数。

Painter と Qt は既存テストが sys.modules に入れた偽物を再利用する。
Maya・Pillow は使わない。時刻は引数、ファイルは tmp_path だけ。
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import sys
import types
from pathlib import Path

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_TESTS)
for _p in (_PKG, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_sp_live_sync_plugin_protocol import plugin  # noqa: E402
from plugin_patch import patch_plugin_attr  # noqa: E402
from plugin_source import plugin_source_text  # noqa: E402
import uvremap_rebase as rb  # noqa: E402

RID = "rb-20261019-053400-3fa2"
SHA1 = "a" * 40
MESH_PATH = "C:/SPMayaLiveSync/uvremap/meshes/body.fbx"
CREATED = 1792330535.4
TTL = 1800
NOW = 1000.0
EXPIRES = 2000.0
PROJECT_KEY = "C:/Work/Demo.spp"
PLUGIN_VERSION = "1.4.0"

_EXISTING_DEFAULT_KEYS = (
    "staging_dir",
    "watch_dir",
    "final_export_dir",
    "preview_resolution_log2",
    "final_resolution_log2",
    "debounce_seconds",
    "file_format",
    "bit_depth",
    "texture_sets",
    "renderer",
    "raw_colorspace_suffixes",
    "known_texture_sets_by_project",
    "active_project_key",
    "active_final_subfolder",
    "active_watch_subfolder",
    "watch_subfolder_by_project",
    "final_subfolder_by_project",
    "texture_set_export_prefix_by_project",
    "painter_mesh_by_project",
    "setup_wizard_completed",
    "final_cooldown_seconds",
)
_REBASE_SETTING_KEYS = (
    "rebase_enabled",
    "rebase_approval",
    "rebase_auto_idle_s",
    "rebase_reload_timeout_s",
    "rebase_retry_max",
    "rebase_wait_max_s",
    "rebase_reproject_uv_fills",
)
_PAINTER_STATE_KEYS = (
    "now",
    "project_open",
    "project_key",
    "saved_path",
    "busy",
    "exporting",
    "needs_saving",
    "backup_includes_unsaved",
    "mesh_exists",
    "mesh_sha1_ok",
    "superseded",
    "cancelled",
    "can_disable_auto_unwrap",
    "last_edit_age_s",
)
_ALLOWED_TRANSITIONS = {
    None: frozenset(("received", "rejected")),
    "received": frozenset((
        "received", "rejected", "awaiting_approval", "preparing",
        "declined", "expired", "superseded", "failed",
    )),
    "awaiting_approval": frozenset((
        "awaiting_approval", "preparing", "declined", "expired",
        "superseded", "failed",
    )),
    "preparing": frozenset((
        "preparing", "backing_up", "declined", "superseded", "failed",
    )),
    "backing_up": frozenset(("backing_up", "reloading", "failed")),
    "reloading": frozenset(("reloading", "exporting", "failed")),
    "exporting": frozenset(("exporting", "done", "failed")),
}


def _rid(index):
    return "rb-20261019-{0:06d}-{1:04x}".format(index, index)


def _mesh(path=MESH_PATH, size=123456, sha1=SHA1):
    return {"path": path, "size": size, "sha1": sha1}


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


def _ok_request(**over):
    raw = _valid_raw(
        project_key=PROJECT_KEY,
        created_at=NOW - 10,
        expires_at=EXPIRES,
    )
    raw.update(over)
    parsed, reason = plugin.parse_rebase_request(raw)
    assert reason is None
    return parsed


def _ok_state(**over):
    state = {
        "now": NOW,
        "project_open": True,
        "project_key": PROJECT_KEY,
        "saved_path": PROJECT_KEY,
        "busy": False,
        "exporting": False,
        "needs_saving": False,
        "backup_includes_unsaved": False,
        "mesh_exists": True,
        "mesh_sha1_ok": True,
        "superseded": False,
        "cancelled": False,
        "can_disable_auto_unwrap": True,
        "last_edit_age_s": 100.0,
    }
    state.update(over)
    return state


def _settings(approval="ask", **over):
    cfg = {"rebase_enabled": True, "rebase_approval": approval}
    cfg.update(over)
    return plugin.normalize_rebase_settings(cfg)


# ---------------------------------------------------------------------------
# 1. import
# ---------------------------------------------------------------------------
def test_plugin_source_has_no_uvremap_import():
    source = plugin_source_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "uvremap_" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "uvremap_" not in module
            # uvremap_core のようなモジュールの取り込みを禁じる。パッケージ内の部品から
            # resolve_uvremap_root のような関数を取り込むのは構わない(module が部品名のとき)。
            if node.level == 0 or not node.module:
                for alias in node.names:
                    assert "uvremap_" not in alias.name


def test_plugin_imports_without_painter_or_qt_objects():
    assert plugin.__version__ == "1.4.4"
    assert callable(plugin.parse_rebase_request)
    assert callable(plugin.decide_approval)


# ---------------------------------------------------------------------------
# 2. キー正規化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,stored,compare", [
    ("C:\\Work\\Demo.spp", "C:/Work/Demo.spp", "c:/work/demo.spp"),
    ("C:/Work/Demo.spp", "C:/Work/Demo.spp", "c:/work/demo.spp"),
    ("c:\\work\\DEMO.spp", "c:/work/DEMO.spp", "c:/work/demo.spp"),
    ("", "", ""),
])
def test_project_key_normalization_table(raw, stored, compare):
    assert plugin.normalize_project_key_for_storage(raw) == stored
    assert plugin.normalize_project_key_for_compare(raw) == compare


def test_project_key_nfc_and_none():
    nfd = "C:/Work/\u30ab\u3099.spp"
    nfc = unicodedata_nfc(nfd)
    assert plugin.normalize_project_key_for_storage(nfd) == nfc
    assert plugin.normalize_project_key_for_storage(None) is None
    assert plugin.normalize_project_key_for_compare(None) is None
    assert plugin.project_keys_match(None, None) is False
    assert plugin.project_keys_match("C:\\Work\\Demo.spp", "c:/work/demo.spp") is True
    assert plugin.project_keys_match("", "") is False
    assert plugin.project_keys_match("C:/A.spp", "C:/B.spp") is False


def unicodedata_nfc(text):
    import unicodedata
    return unicodedata.normalize("NFC", text)


# ---------------------------------------------------------------------------
# 3. パス
# ---------------------------------------------------------------------------
def test_rebase_paths_match_maya(tmp_path):
    root = str(tmp_path)
    assert plugin.rebase_request_path(root, RID) == str(rb.request_path(root, RID))
    assert plugin.rebase_cancel_path(root, RID) == str(rb.cancel_path(root, RID))
    assert plugin.rebase_status_path(root, RID) == str(rb.status_path(root, RID))


@pytest.mark.parametrize("bad_id", [
    "",
    "..",
    "../rb-20261019-000000-abcd",
    "rb-20261019-000000-abcd/../x",
    "rb-20261019-000000-ABCD",
    "rb-20261019-000000-abcdX",
    "rb-20261019-000000-abcd.json",
    "rb-20261019-000000-abcd/",
    "x" * 20,
])
def test_rebase_paths_reject_bad_ids(tmp_path, bad_id):
    with pytest.raises(ValueError):
        plugin.rebase_request_path(tmp_path, bad_id)
    with pytest.raises(ValueError):
        plugin.rebase_cancel_path(tmp_path, bad_id)
    with pytest.raises(ValueError):
        plugin.rebase_status_path(tmp_path, bad_id)


def test_uvremap_root_and_resolve():
    assert plugin.uvremap_root_for_project("", "sub") is None
    assert plugin.uvremap_root_for_project("root", "") is None
    assert plugin.uvremap_root_for_project(None, "sub") is None
    assert plugin.uvremap_root_for_project("root", None) is None
    expected = os.path.normpath(os.path.join("D:/uv", "proj"))
    assert plugin.uvremap_root_for_project("D:/uv", "proj") == expected
    default_sub = os.path.normpath(os.path.join(plugin.DEFAULT_UVREMAP_ROOT_DIR, "sub"))
    assert plugin.resolve_uvremap_root({}, "sub") == default_sub
    assert plugin.resolve_uvremap_root({"uvremap_root_dir": ""}, "sub") == default_sub
    assert plugin.resolve_uvremap_root({"uvremap_root_dir": 1}, "sub") == default_sub
    assert plugin.resolve_uvremap_root(None, "sub") == default_sub
    assert plugin.resolve_uvremap_root([], "sub") == default_sub
    assert plugin.resolve_uvremap_root("nope", "sub") == default_sub
    assert plugin.resolve_uvremap_root({"uvremap_root_dir": "D:/uv"}, "proj") == expected
    assert "uvremap_root_dir" not in plugin.DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# 4. 依頼の突き合わせ
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("uv_old", [None, {"name": "uvOld", "index": 1}])
def test_read_rebase_request_matches_maya_roundtrip(tmp_path, uv_old):
    kwargs = dict(
        request_id=RID,
        project_key="project-key",
        created_at=CREATED,
        ttl_s=TTL,
        mesh=_mesh(),
        baseline_id="bl-1",
        ts_names=["Body"],
    )
    if uv_old is not None:
        kwargs["uv_old"] = uv_old
    built = rb.build_request(**kwargs)
    rb.write_request(tmp_path, built)
    ours = plugin.read_rebase_request(tmp_path, RID)
    theirs = rb.read_request(tmp_path, RID)
    assert ours == theirs
    if uv_old is None:
        assert "uv_old" not in ours
    else:
        assert ours["uv_old"] == uv_old


@pytest.mark.parametrize("raw", [
    _valid_raw(),
    _valid_raw(maya_version="2024", extra=1),
    _valid_raw(mesh=_mesh(sha1="AB" * 20)),
    _valid_raw(uv_old=None),
    _valid_raw(uv_old={"name": "uvOld", "index": 1}),
    _valid_raw(uv_old={"name": "uvOld_1", "index": 7}),
    None,
    [],
    "nope",
])
def test_parse_rebase_request_matches_maya(raw):
    assert plugin.parse_rebase_request(raw) == rb.parse_request(raw)


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
def test_parse_missing_required_key_matches_maya(key):
    raw = _valid_raw()
    del raw[key]
    assert plugin.parse_rebase_request(raw) == rb.parse_request(raw)
    assert plugin.parse_rebase_request(raw) == (None, "bad_request")


@pytest.mark.parametrize("key,value", [
    ("schema", 2),
    ("schema", True),
    ("schema", "1"),
    ("schema", 1.0),
    ("request_id", "nope"),
    ("project_key", ""),
    ("project_key", 1),
    ("created_at", True),
    ("created_at", float("nan")),
    ("expires_at", float("inf")),
    ("mesh", []),
    ("ts_names", "Body"),
    ("ts_names", []),
    ("ts_names", [""]),
    ("final_full", 1),
    ("baseline_id", ".."),
    ("baseline_id", ""),
    ("reload", []),
])
def test_parse_bad_types_match_maya(key, value):
    raw = _valid_raw()
    raw[key] = value
    assert plugin.parse_rebase_request(raw) == rb.parse_request(raw)


@pytest.mark.parametrize("reload", [
    {"preserve_strokes": False, "import_cameras": False, "auto_unwrap": False},
    {"preserve_strokes": True, "import_cameras": 1, "auto_unwrap": False},
    {"preserve_strokes": True, "import_cameras": False, "auto_unwrap": "false"},
    {"preserve_strokes": True, "import_cameras": False},
])
def test_parse_reload_mismatch_is_bad_settings(reload):
    raw = _valid_raw(reload=reload)
    assert plugin.parse_rebase_request(raw) == rb.parse_request(raw)
    assert plugin.parse_rebase_request(raw) == (None, "bad_settings")


@pytest.mark.parametrize("mesh", [
    {"path": MESH_PATH, "size": -1, "sha1": SHA1},
    {"path": MESH_PATH, "size": True, "sha1": SHA1},
    {"path": MESH_PATH, "size": 10, "sha1": "abc"},
    {"path": MESH_PATH, "size": 10, "sha1": "g" * 40},
    {"path": MESH_PATH, "size": 10, "sha1": "AB" * 19 + "zz"},
])
def test_parse_bad_mesh_matches_maya(mesh):
    raw = _valid_raw(mesh=mesh)
    assert plugin.parse_rebase_request(raw) == rb.parse_request(raw)


def test_parse_expires_and_unknown_keys_match_maya():
    assert plugin.parse_rebase_request(_valid_raw(created_at=100, expires_at=100)) == (
        None, "bad_request")
    raw = _valid_raw(painter_extra={"a": 1}, maya_version="2024", tool_version=3)
    assert plugin.parse_rebase_request(raw) == rb.parse_request(raw)
    parsed, reason = plugin.parse_rebase_request(raw)
    assert reason is None
    assert "painter_extra" not in parsed
    assert parsed["maya_version"] == "2024"
    assert "tool_version" not in parsed


@pytest.mark.parametrize("uv_old", [
    {"name": "uvOld", "index": 1},
    {"name": "uvOld_1", "index": 7},
    {"name": "A" + ("a" * 63), "index": 1},
    {"name": "uvOld", "index": 1, "extra": True},
])
def test_parse_uv_old_valid_matches_maya(uv_old):
    raw = _valid_raw(uv_old=uv_old)
    assert plugin.parse_rebase_request(raw) == rb.parse_request(raw)


@pytest.mark.parametrize("uv_old", [
    "",
    [],
    "uvOld",
    1,
    True,
    {"name": "", "index": 1},
    {"name": "A" + ("a" * 64), "index": 1},
    {"name": "1abc", "index": 1},
    {"name": "a-b", "index": 1},
    {"name": "日本語", "index": 1},
    {"name": "uvOld", "index": 0},
    {"name": "uvOld", "index": 8},
    {"name": "uvOld", "index": True},
    {"name": "uvOld", "index": "1"},
    {"name": "uvOld", "index": 1.0},
    {"name": "uvOld"},
    {"index": 1},
])
def test_parse_uv_old_invalid_matches_maya(uv_old):
    raw = _valid_raw(uv_old=uv_old)
    assert plugin.parse_rebase_request(raw) == rb.parse_request(raw)
    assert plugin.parse_rebase_request(raw) == (None, "bad_request")


def test_uv_old_invalid_wins_over_bad_settings():
    raw = _valid_raw(uv_old="uvOld")
    raw["reload"] = {
        "preserve_strokes": False,
        "import_cameras": False,
        "auto_unwrap": False,
    }
    assert plugin.parse_rebase_request(raw) == rb.parse_request(raw)
    assert plugin.parse_rebase_request(raw) == (None, "bad_request")
    missing = _valid_raw(uv_old="uvOld")
    del missing["schema"]
    assert plugin.parse_rebase_request(missing) == (None, "bad_request")


def test_uv_old_absent_and_null():
    absent, reason = plugin.parse_rebase_request(_valid_raw())
    assert reason is None
    assert "uv_old" not in absent
    kept, reason = plugin.parse_rebase_request(_valid_raw(uv_old=None))
    assert reason is None
    assert kept["uv_old"] is None
    assert plugin.parse_rebase_request(_valid_raw()) == rb.parse_request(_valid_raw())
    assert plugin.parse_rebase_request(_valid_raw(uv_old=None)) == rb.parse_request(
        _valid_raw(uv_old=None))


# ---------------------------------------------------------------------------
# 5. 依頼のファイル
# ---------------------------------------------------------------------------
def test_read_rebase_request_broken_empty_bom_and_id_mismatch(tmp_path):
    path = Path(plugin.rebase_request_path(tmp_path, RID))
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    assert plugin.read_rebase_request(tmp_path, RID) is None
    path.write_text("", encoding="utf-8")
    assert plugin.read_rebase_request(tmp_path, RID) is None
    payload = json.dumps(_valid_raw()).encode("utf-8")
    path.write_bytes(b"\xef\xbb\xbf" + payload)
    assert plugin.read_rebase_request(tmp_path, RID) is None
    path.write_bytes("あ".encode("cp932"))
    assert plugin.read_rebase_request(tmp_path, RID) is None
    raw = _valid_raw()
    path.write_text(json.dumps(raw), encoding="utf-8")
    other = _rid(9)
    other_path = Path(plugin.rebase_request_path(tmp_path, other))
    other_path.write_text(json.dumps(raw), encoding="utf-8")
    assert plugin.read_rebase_request(tmp_path, other) is None
    parsed, reason = plugin.read_rebase_request_with_reason(tmp_path, other)
    assert parsed is None
    assert reason == "bad_request"
    missing, reason = plugin.read_rebase_request_with_reason(tmp_path, _rid(3))
    assert missing is None
    assert reason == "bad_request"
    ok, reason = plugin.read_rebase_request_with_reason(tmp_path, RID)
    assert reason is None
    assert ok["request_id"] == RID
    bad_reload = _valid_raw(reload={
        "preserve_strokes": False,
        "import_cameras": False,
        "auto_unwrap": False,
    })
    path.write_text(json.dumps(bad_reload), encoding="utf-8")
    parsed, reason = plugin.read_rebase_request_with_reason(tmp_path, RID)
    assert parsed is None
    assert reason == "bad_settings"


# ---------------------------------------------------------------------------
# 6. 走査と差分
# ---------------------------------------------------------------------------
def test_scan_and_diff_rebase_dir(tmp_path):
    empty = plugin.scan_rebase_dir(tmp_path)
    assert empty.requests == {}
    assert empty.cancels == {}
    req_dir = Path(plugin.rebase_requests_dir(tmp_path))
    req_dir.mkdir(parents=True)
    (req_dir / "note.tmp").write_text("x", encoding="utf-8")
    (req_dir / "note.txt").write_text("x", encoding="utf-8")
    (req_dir / "not-an-id.json").write_text("{}", encoding="utf-8")
    (req_dir / RID).mkdir()
    first = rb.build_request(
        request_id=RID,
        project_key="p",
        created_at=CREATED,
        ttl_s=TTL,
        mesh=_mesh(),
        baseline_id="bl-1",
        ts_names=["Body"],
    )
    rb.write_request(tmp_path, first)
    second_id = _rid(2)
    second = rb.build_request(
        request_id=second_id,
        project_key="p",
        created_at=CREATED + 1,
        ttl_s=TTL,
        mesh=_mesh(),
        baseline_id="bl-1",
        ts_names=["Body"],
    )
    rb.write_request(tmp_path, second)
    rb.write_cancel(tmp_path, RID)
    scan = plugin.scan_rebase_dir(tmp_path)
    assert set(scan.requests) == {RID, second_id}
    assert RID in scan.cancels
    assert second_id not in scan.cancels
    for rid, meta in scan.requests.items():
        mtime_ns, size = meta
        assert isinstance(mtime_ns, int)
        assert size > 0
    diff_new = plugin.diff_rebase_scan(None, scan)
    assert diff_new.changed_ids == sorted([RID, second_id])
    assert diff_new.new_cancel_ids == [RID]
    same = plugin.diff_rebase_scan(scan, scan)
    assert same.changed_ids == []
    assert same.new_cancel_ids == []
    path = Path(plugin.rebase_request_path(tmp_path, RID))
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = plugin.scan_rebase_dir(tmp_path)
    diff = plugin.diff_rebase_scan(scan, changed)
    assert diff.changed_ids == [RID]
    rb.write_cancel(tmp_path, second_id)
    with_cancel = plugin.scan_rebase_dir(tmp_path)
    diff_cancel = plugin.diff_rebase_scan(changed, with_cancel)
    assert diff_cancel.new_cancel_ids == [second_id]


# ---------------------------------------------------------------------------
# 7. 置き換え
# ---------------------------------------------------------------------------
def test_find_superseded_ids():
    older = _ok_request(request_id=_rid(1), created_at=10, project_key="C:\\Work\\Demo.spp")
    newer = _ok_request(request_id=_rid(2), created_at=20, project_key="c:/work/demo.spp")
    other = _ok_request(request_id=_rid(3), created_at=5, project_key="C:/Other.spp")
    assert plugin.find_superseded_ids([older, newer, other]) == {_rid(1)}
    a = _ok_request(request_id=_rid(4), created_at=50, project_key="P")
    b = _ok_request(request_id=_rid(5), created_at=50, project_key="P")
    assert plugin.find_superseded_ids([a, b]) == {_rid(4)}
    assert plugin.find_superseded_ids([newer]) == set()
    assert plugin.find_superseded_ids([]) == set()


# ---------------------------------------------------------------------------
# 8. 状態の突き合わせ
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", list(plugin.REBASE_STATES))
def test_build_status_survives_maya_parse(state):
    status = plugin.build_rebase_status(
        RID,
        state,
        1792330535.4,
        PLUGIN_VERSION,
        reason="busy" if state == "failed" else None,
        message="あ" * 501,
        backup_name="C:\\SPMayaLiveSync\\uvremap\\backup\\scene.spp",
        result_export_id="ex-1",
    )
    parsed = rb.parse_status(status)
    assert parsed is not None
    assert parsed["state"] == state
    assert parsed["message"] == "あ" * 500
    assert parsed["backup_name"] == "scene.spp"
    assert parsed["result_export_id"] == "ex-1"


def test_write_status_readable_by_maya(tmp_path):
    status = plugin.build_rebase_status(RID, "received", 10, PLUGIN_VERSION)
    assert plugin.write_rebase_status(tmp_path, status) is True
    got = rb.read_status(tmp_path, RID)
    assert got.kind == "ok"
    assert got.status["state"] == "received"


def _without_direction_fills(parsed):
    """Maya の parse_status は RB-d2 で direction_fills を足す。プラグイン側は足さない。"""
    if not isinstance(parsed, dict) or "direction_fills" not in parsed:
        return parsed
    copied = dict(parsed)
    del copied["direction_fills"]
    return copied


@pytest.mark.parametrize("raw", [
    None,
    [],
    {"schema": 2, "request_id": RID, "state": "done", "updated_at": 1},
    {"schema": True, "request_id": RID, "state": "done", "updated_at": 1},
    {"schema": 1, "request_id": RID, "state": "nope", "updated_at": 1},
    {"schema": 1, "request_id": RID, "state": "done", "updated_at": True},
    {"schema": 1, "request_id": "", "state": "done", "updated_at": 1},
    {"request_id": RID, "state": "done", "updated_at": 1},
    {
        "schema": 1,
        "request_id": RID,
        "state": "failed",
        "updated_at": 1,
        "unknown": 1,
        "plugin_version": 1,
        "message": "あ" * 501,
        "backup_name": "a/b/c.spp",
    },
])
def test_parse_status_matches_maya(raw):
    assert plugin.parse_rebase_status(raw) == _without_direction_fills(rb.parse_status(raw))


def test_parse_status_drops_uv_fills_and_maya_still_reads():
    status = plugin.build_rebase_status(
        RID,
        "done",
        11,
        PLUGIN_VERSION,
        uv_fills={"enabled": True, "switched": 2, "extra": 1},
    )
    assert status["uv_fills"]["enabled"] is True
    assert status["uv_fills"]["switched"] == 2
    assert "extra" not in status["uv_fills"]
    ours = plugin.parse_rebase_status(status)
    theirs = rb.parse_status(status)
    assert ours == _without_direction_fills(theirs)
    assert "uv_fills" not in ours
    assert theirs is not None
    assert theirs["direction_fills"] == 0


def test_build_status_value_error():
    with pytest.raises(ValueError):
        plugin.build_rebase_status("bad", "received", 1, PLUGIN_VERSION)
    with pytest.raises(ValueError):
        plugin.build_rebase_status(RID, "nope", 1, PLUGIN_VERSION)
    with pytest.raises(ValueError):
        plugin.build_rebase_status(RID, "failed", 1, PLUGIN_VERSION, reason="nope")
    with pytest.raises(ValueError):
        plugin.build_rebase_status(RID, "received", True, PLUGIN_VERSION)
    with pytest.raises(ValueError):
        plugin.build_rebase_status(RID, "received", float("nan"), PLUGIN_VERSION)


# ---------------------------------------------------------------------------
# 9. 遷移
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("prev", [None] + list(plugin.REBASE_STATES))
@pytest.mark.parametrize("new", list(plugin.REBASE_STATES))
def test_transition_table(prev, new):
    expected = new in _ALLOWED_TRANSITIONS.get(prev, frozenset())
    assert plugin.is_valid_rebase_transition(prev, new) is expected


def test_terminal_cannot_transition():
    for prev in plugin.REBASE_TERMINAL_STATES:
        for new in plugin.REBASE_STATES:
            assert plugin.is_valid_rebase_transition(prev, new) is False


def test_write_status_transition_and_corrupt_and_error(tmp_path, monkeypatch):
    sequence = (
        "received",
        "awaiting_approval",
        "preparing",
        "backing_up",
        "reloading",
        "exporting",
        "done",
    )
    updated = 1
    for state in sequence:
        status = plugin.build_rebase_status(RID, state, updated, PLUGIN_VERSION)
        assert plugin.write_rebase_status(tmp_path, status) is True
        updated += 1
    failed = plugin.build_rebase_status(RID, "failed", updated, PLUGIN_VERSION, reason="busy")
    assert plugin.write_rebase_status(tmp_path, failed) is False
    assert rb.read_status(tmp_path, RID).status["state"] == "done"

    other = _rid(8)
    path = Path(plugin.rebase_status_path(tmp_path, other))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    recovered = plugin.build_rebase_status(other, "received", 3, PLUGIN_VERSION)
    assert plugin.write_rebase_status(tmp_path, recovered) is True
    assert rb.read_status(tmp_path, other).kind == "ok"

    def boom(*args, **kwargs):
        raise OSError("readonly")

    patch_plugin_attr(monkeypatch, plugin, "write_json_atomic", boom)
    third = _rid(7)
    status = plugin.build_rebase_status(third, "received", 4, PLUGIN_VERSION)
    assert plugin.write_rebase_status(tmp_path, status) is False


def test_same_nonterminal_rewrite_is_allowed(tmp_path):
    seed = plugin.build_rebase_status(RID, "received", 1, PLUGIN_VERSION)
    assert plugin.write_rebase_status(tmp_path, seed) is True
    first = plugin.build_rebase_status(RID, "preparing", 2, PLUGIN_VERSION, message="a")
    assert plugin.write_rebase_status(tmp_path, first) is True
    second = plugin.build_rebase_status(RID, "preparing", 3, PLUGIN_VERSION, message="b")
    assert plugin.write_rebase_status(tmp_path, second) is True
    assert rb.read_status(tmp_path, RID).status["message"] == "b"


# ---------------------------------------------------------------------------
# 10. 中断と失敗文
# ---------------------------------------------------------------------------
def test_interrupted_status_and_failure_messages():
    prev = plugin.build_rebase_status(
        RID, "reloading", 1, PLUGIN_VERSION, backup_name="keep.spp")
    interrupted = plugin.build_interrupted_status(prev, 2, PLUGIN_VERSION)
    assert interrupted["state"] == "failed"
    assert interrupted["reason"] == "interrupted"
    assert interrupted["backup_name"] == "keep.spp"
    assert "完了しませんでした" in interrupted["message"]
    done = plugin.build_rebase_status(RID, "done", 1, PLUGIN_VERSION)
    assert plugin.build_interrupted_status(done, 2, PLUGIN_VERSION) is None
    for reason in plugin.REBASE_REASONS:
        text = plugin.rebase_failure_message(reason)
        assert text
        assert len(text) <= 500
        assert "C:/" not in text
        assert "\\" not in text
    export_text = plugin.rebase_failure_message("export_failed")
    assert "やり直してください" in export_text
    assert "メッシュは読み直し済み" in export_text
    reload_text = plugin.rebase_failure_message(
        "reload_error",
        backup_name="C:/work/rebase_backup/keep.spp",
        detail="x" * 600,
    )
    assert "バックアップ: keep.spp" in reload_text
    assert "C:/work" not in reload_text
    assert len(reload_text) <= 500


# ---------------------------------------------------------------------------
# 11. decide_approval
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("approval", ["ask", "auto"])
@pytest.mark.parametrize("case", [
    ("disabled", {"rebase_enabled": False}, {}, "reject", "disabled"),
    ("cancelled", {}, {"cancelled": True}, "reject", "cancelled"),
    ("superseded", {}, {"superseded": True}, "reject", "superseded"),
    ("expired", {}, {"now": EXPIRES + 1}, "reject", "expired"),
    ("no_project", {}, {"project_open": False}, "wait", "no_project"),
    ("project_mismatch", {}, {"project_key": "C:/Other.spp"}, "wait", "project_mismatch"),
    ("unsaved_project", {}, {"saved_path": None}, "reject", "unsaved_project"),
    ("unsaved_empty", {}, {"saved_path": ""}, "reject", "unsaved_project"),
    ("mesh_missing", {}, {"mesh_exists": False}, "reject", "mesh_missing"),
    ("mesh_hash", {}, {"mesh_sha1_ok": False}, "reject", "mesh_hash_mismatch"),
    ("busy", {}, {"busy": True}, "wait", "busy"),
    ("exporting", {}, {"exporting": True}, "wait", "busy"),
])
def test_decide_approval_each_early_row(approval, case):
    _name, setting_over, state_over, action, reason = case
    request = _ok_request()
    decision = plugin.decide_approval(
        request, _ok_state(**state_over), _settings(approval, **setting_over))
    assert decision.action == action
    assert decision.reason == reason


def test_decide_approval_auto_and_ask_rows():
    request = _ok_request()
    auto = plugin.decide_approval(
        request, _ok_state(can_disable_auto_unwrap=False), _settings("auto"))
    assert auto == plugin.ApprovalDecision("ask", "auto_unwrap_unverified")
    ask = plugin.decide_approval(
        request, _ok_state(can_disable_auto_unwrap=False), _settings("ask"))
    assert ask == plugin.ApprovalDecision("ask", "needs_approval")
    recent = plugin.decide_approval(
        request, _ok_state(last_edit_age_s=9.9), _settings("auto", rebase_auto_idle_s=10))
    assert recent == plugin.ApprovalDecision("wait", "recent_edit")
    none_age = plugin.decide_approval(
        request, _ok_state(last_edit_age_s=None), _settings("auto"))
    assert none_age == plugin.ApprovalDecision("wait", "recent_edit")
    idle_ok = plugin.decide_approval(
        request, _ok_state(last_edit_age_s=10.0), _settings("auto", rebase_auto_idle_s=10))
    assert idle_ok == plugin.ApprovalDecision("run", "auto_approved")
    unsaved_auto = plugin.decide_approval(
        request,
        _ok_state(needs_saving=True, backup_includes_unsaved=True, last_edit_age_s=100),
        _settings("auto"),
    )
    assert unsaved_auto == plugin.ApprovalDecision("wait", "unsaved_changes")
    unsaved_ask_wait = plugin.decide_approval(
        request,
        _ok_state(needs_saving=True, backup_includes_unsaved=False),
        _settings("ask"),
    )
    assert unsaved_ask_wait == plugin.ApprovalDecision("wait", "unsaved_changes")
    unsaved_ask_ok = plugin.decide_approval(
        request,
        _ok_state(needs_saving=True, backup_includes_unsaved=True),
        _settings("ask"),
    )
    assert unsaved_ask_ok == plugin.ApprovalDecision("ask", "needs_approval")
    run = plugin.decide_approval(request, _ok_state(), _settings("auto"))
    assert run == plugin.ApprovalDecision("run", "auto_approved")
    need = plugin.decide_approval(request, _ok_state(), _settings("ask"))
    assert need == plugin.ApprovalDecision("ask", "needs_approval")


def test_decide_approval_priority_and_edges():
    request = _ok_request()
    assert plugin.decide_approval(
        request, _ok_state(now=EXPIRES + 1, saved_path=None), _settings("ask"),
    ) == plugin.ApprovalDecision("reject", "expired")
    assert plugin.decide_approval(
        request, _ok_state(cancelled=True, superseded=True), _settings("ask"),
    ) == plugin.ApprovalDecision("reject", "cancelled")
    assert plugin.decide_approval(
        request, _ok_state(project_open=False, project_key="C:/Other.spp"), _settings("ask"),
    ) == plugin.ApprovalDecision("wait", "no_project")
    assert plugin.decide_approval(
        request, _ok_state(saved_path=None, mesh_exists=False), _settings("ask"),
    ) == plugin.ApprovalDecision("reject", "unsaved_project")
    assert plugin.decide_approval(
        request, _ok_state(mesh_sha1_ok=False, busy=True), _settings("ask"),
    ) == plugin.ApprovalDecision("reject", "mesh_hash_mismatch")
    assert plugin.decide_approval(
        request, _ok_state(busy=True, last_edit_age_s=0), _settings("auto"),
    ) == plugin.ApprovalDecision("wait", "busy")
    assert plugin.decide_approval(
        request,
        _ok_state(can_disable_auto_unwrap=False, last_edit_age_s=0),
        _settings("auto"),
    ) == plugin.ApprovalDecision("ask", "auto_unwrap_unverified")
    assert plugin.decide_approval(
        request,
        _ok_state(last_edit_age_s=0, needs_saving=True, backup_includes_unsaved=False),
        _settings("auto"),
    ) == plugin.ApprovalDecision("wait", "recent_edit")
    assert plugin.decide_approval(
        request, _ok_state(), _settings("ask", rebase_enabled=False),
    ) == plugin.ApprovalDecision("reject", "disabled")
    assert plugin.decide_approval(
        request, _ok_state(), _settings("auto", rebase_enabled=False),
    ) == plugin.ApprovalDecision("reject", "disabled")
    for value in ("AUTO", 1, None):
        assert plugin.decide_approval(
            request, _ok_state(), {"rebase_approval": value},
        ) == plugin.ApprovalDecision("ask", "needs_approval")
    assert plugin.decide_approval(
        request, _ok_state(), {},
    ) == plugin.ApprovalDecision("ask", "needs_approval")
    assert plugin.decide_approval(
        request, _ok_state(now=EXPIRES), _settings("ask"),
    ) == plugin.ApprovalDecision("ask", "needs_approval")
    assert plugin.decide_approval(
        request, _ok_state(mesh_sha1_ok=None), _settings("ask"),
    ) == plugin.ApprovalDecision("ask", "needs_approval")
    assert plugin.decide_approval(
        request,
        {"now": NOW},
        _settings("ask"),
    ).action in ("wait", "reject")
    assert plugin.decide_approval("nope", _ok_state(), _settings("ask")) == (
        plugin.ApprovalDecision("reject", "bad_request"))
    bad_req = dict(_ok_request())
    bad_req["expires_at"] = "later"
    assert plugin.decide_approval(bad_req, _ok_state(), _settings("ask")) == (
        plugin.ApprovalDecision("reject", "bad_request"))
    assert plugin.decide_approval(
        request,
        _ok_state(project_key="c:\\work\\demo.spp"),
        _settings("ask"),
    ) == plugin.ApprovalDecision("ask", "needs_approval")


# ---------------------------------------------------------------------------
# 12. wait_timed_out
# ---------------------------------------------------------------------------
def test_wait_timed_out():
    assert plugin.wait_timed_out(None, 100, {}) is False
    assert plugin.wait_timed_out(0, 60, {"rebase_wait_max_s": 60}) is False
    assert plugin.wait_timed_out(0, 60.1, {"rebase_wait_max_s": 60}) is True
    assert plugin.wait_timed_out(0, 59, {"rebase_wait_max_s": 10}) is False
    assert plugin.wait_timed_out(0, 61, {"rebase_wait_max_s": 10}) is True
    assert plugin.wait_timed_out(0, 86400, {"rebase_wait_max_s": 999999}) is False
    assert plugin.wait_timed_out(0, 86400.1, {"rebase_wait_max_s": 999999}) is True


# ---------------------------------------------------------------------------
# 13. 事前チェック
# ---------------------------------------------------------------------------
def test_mesh_precheck(tmp_path):
    path = tmp_path / "mesh.fbx"
    path.write_bytes(b"hello")
    digest = hashlib.sha1(b"hello").hexdigest()
    mesh = {"path": str(path), "size": 5, "sha1": digest}
    missing = plugin.mesh_precheck({"path": str(tmp_path / "no.fbx"), "size": 1, "sha1": digest}, None)
    assert missing == (False, None, None)
    directory = plugin.mesh_precheck({"path": str(tmp_path), "size": 1, "sha1": digest}, None)
    assert directory == (False, None, None)
    calls = []

    def counting_hash(hash_path):
        calls.append(hash_path)
        return hashlib.sha1(Path(hash_path).read_bytes()).hexdigest()

    size_mismatch = plugin.mesh_precheck(
        {"path": str(path), "size": 99, "sha1": digest}, None, hash_fn=counting_hash)
    assert size_mismatch[0] is True
    assert size_mismatch[1] is False
    assert calls == []
    ok, sha1_ok, cache = plugin.mesh_precheck(mesh, None, hash_fn=counting_hash)
    assert ok is True and sha1_ok is True
    assert calls == [str(path)]
    ok2, sha1_ok2, cache2 = plugin.mesh_precheck(mesh, cache, hash_fn=counting_hash)
    assert ok2 is True and sha1_ok2 is True
    assert cache2 is cache
    assert calls == [str(path)]
    st = path.stat()
    path.write_bytes(b"hello")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
    ok3, sha1_ok3, _cache3 = plugin.mesh_precheck(mesh, cache, hash_fn=counting_hash)
    assert ok3 is True and sha1_ok3 is True
    assert len(calls) == 2
    upper = plugin.mesh_precheck(
        {"path": str(path), "size": 5, "sha1": digest.upper()}, None)
    assert upper[1] is True

    def boom(_path):
        raise OSError("unreadable")

    assert plugin.mesh_precheck(mesh, None, hash_fn=boom) == (False, None, None)
    wrong = plugin.mesh_precheck(
        {"path": str(path), "size": 5, "sha1": "b" * 40}, None)
    assert wrong[0] is True
    assert wrong[1] is False


def test_supports_auto_unwrap_and_painter_state():
    def with_arg(auto_unwrap_settings=None):
        return auto_unwrap_settings

    class HasAttr(object):
        auto_unwrap_settings = None

    class MeshReloadingSettings1200(object):
        def __init__(self, import_cameras=False, preserve_strokes=True, mesh_settings=None):
            self.import_cameras = import_cameras
            self.preserve_strokes = preserve_strokes
            self.mesh_settings = mesh_settings

    class MeshReloadingSettings1215(object):
        def __init__(
            self,
            import_cameras=False,
            preserve_strokes=True,
            mesh_settings=None,
            auto_unwrap_settings=None,
        ):
            self.auto_unwrap_settings = auto_unwrap_settings

    assert plugin.supports_auto_unwrap_settings(with_arg) is True
    assert plugin.supports_auto_unwrap_settings(HasAttr) is True
    assert plugin.supports_auto_unwrap_settings(MeshReloadingSettings1200) is False
    assert plugin.supports_auto_unwrap_settings(MeshReloadingSettings1215) is True
    assert plugin.supports_auto_unwrap_settings(None) is False
    assert plugin.supports_auto_unwrap_settings(1) is False
    assert plugin.compute_last_edit_age(10, None) is None
    assert plugin.compute_last_edit_age(10, "x") is None
    assert plugin.compute_last_edit_age(10, 12) == 0.0
    assert plugin.compute_last_edit_age(12, 10) == 2.0
    state = plugin.build_painter_state(
        now=NOW,
        is_open=True,
        in_edition=True,
        project_key=PROJECT_KEY,
        saved_path=PROJECT_KEY,
        busy=0,
        exporting=1,
        needs_saving="yes",
        backup_includes_unsaved=None,
        mesh_exists=1,
        mesh_sha1_ok=True,
        superseded=0,
        cancelled=0,
        can_disable_auto_unwrap=1,
        last_edit_at=NOW - 3,
    )
    assert set(state) == set(_PAINTER_STATE_KEYS)
    assert state["project_open"] is True
    assert state["busy"] is False
    assert state["exporting"] is True
    assert state["needs_saving"] is True
    assert state["backup_includes_unsaved"] is False
    assert state["mesh_sha1_ok"] is True
    assert state["last_edit_age_s"] == 3.0
    closed = plugin.build_painter_state(
        now=NOW,
        is_open=True,
        in_edition=False,
        project_key=PROJECT_KEY,
        saved_path=PROJECT_KEY,
        busy=False,
        exporting=False,
        needs_saving=False,
        backup_includes_unsaved=False,
        mesh_exists=True,
        mesh_sha1_ok=None,
        superseded=False,
        cancelled=False,
        can_disable_auto_unwrap=True,
        last_edit_at=None,
    )
    assert closed["project_open"] is False
    assert closed["mesh_sha1_ok"] is None


# ---------------------------------------------------------------------------
# 14. 設定の丸め
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cfg", [None, [], {}, "nope", 1])
def test_normalize_settings_non_dict(cfg):
    got = plugin.normalize_rebase_settings(cfg)
    assert got["rebase_enabled"] is True
    assert got["rebase_approval"] == "ask"
    assert got["rebase_auto_idle_s"] == 10.0
    assert got["rebase_reload_timeout_s"] == 600.0
    assert got["rebase_retry_max"] == 5
    assert got["rebase_wait_max_s"] == 1800.0
    assert got["rebase_reproject_uv_fills"] is True


def test_normalize_settings_rounding_and_defaults():
    got = plugin.normalize_rebase_settings({
        "rebase_enabled": "yes",
        "rebase_approval": "AUTO",
        "rebase_auto_idle_s": 0,
        "rebase_reload_timeout_s": 10000,
        "rebase_retry_max": 3.0,
        "rebase_wait_max_s": 10,
        "rebase_reproject_uv_fills": 1,
    })
    assert got["rebase_enabled"] is True
    assert got["rebase_approval"] == "ask"
    assert got["rebase_auto_idle_s"] == 1.0
    assert got["rebase_reload_timeout_s"] == 3600.0
    assert got["rebase_retry_max"] == 3
    assert got["rebase_wait_max_s"] == 60.0
    assert got["rebase_reproject_uv_fills"] is True
    nan = plugin.normalize_rebase_settings({
        "rebase_auto_idle_s": float("nan"),
        "rebase_reload_timeout_s": float("inf"),
        "rebase_retry_max": 1.5,
        "rebase_wait_max_s": True,
        "rebase_enabled": 0,
        "rebase_approval": "auto",
        "rebase_reproject_uv_fills": False,
    })
    assert nan["rebase_auto_idle_s"] == 10.0
    assert nan["rebase_reload_timeout_s"] == 600.0
    assert nan["rebase_retry_max"] == 5
    assert nan["rebase_wait_max_s"] == 1800.0
    assert nan["rebase_enabled"] is True
    assert nan["rebase_approval"] == "auto"
    assert nan["rebase_reproject_uv_fills"] is False
    clamped = plugin.normalize_rebase_settings({
        "rebase_retry_max": -1,
        "rebase_auto_idle_s": 900,
        "rebase_reload_timeout_s": 1,
        "rebase_wait_max_s": 999999,
    })
    assert clamped["rebase_retry_max"] == 0
    assert clamped["rebase_auto_idle_s"] == 600.0
    assert clamped["rebase_reload_timeout_s"] == 30.0
    assert clamped["rebase_wait_max_s"] == 86400.0
    for key in _REBASE_SETTING_KEYS:
        assert key in plugin.DEFAULT_CONFIG
    for key in _EXISTING_DEFAULT_KEYS:
        assert key in plugin.DEFAULT_CONFIG
    assert plugin.effective_rebase_approval({"rebase_approval": "auto"}) == "auto"
    assert plugin.effective_rebase_approval({"rebase_approval": "AUTO"}) == "ask"
    assert plugin.effective_rebase_approval({}) == "ask"


# ---------------------------------------------------------------------------
# 15. バックアップ・再試行・読み直し
# ---------------------------------------------------------------------------
def test_rebase_backup_path():
    rid = RID
    full, name, folder = plugin.rebase_backup_path(r"C:\work\Demo.spp", rid)
    assert full == "C:/work/rebase_backup/Demo_before_rebase_{0}.spp".format(rid)
    assert name == "Demo_before_rebase_{0}.spp".format(rid)
    assert folder == "C:/work/rebase_backup"
    slash = plugin.rebase_backup_path("C:/work/Demo.spp", rid)
    assert slash == (full, name, folder)
    jp_full, jp_name, jp_folder = plugin.rebase_backup_path("C:/work/デモ.spp", rid)
    assert jp_full == "C:/work/rebase_backup/デモ_before_rebase_{0}.spp".format(rid)
    assert "デモ" in jp_name
    assert jp_folder == "C:/work/rebase_backup"
    bad_full, bad_name, _folder = plugin.rebase_backup_path(r"C:\work\De<mo>.spp", rid)
    assert "<" not in bad_name
    assert bad_full.startswith("C:/work/rebase_backup/")
    existing = set()
    first = plugin.rebase_backup_path("C:/work/Demo.spp", rid, exists_fn=existing.__contains__)
    existing.add(first[0])
    second = plugin.rebase_backup_path("C:/work/Demo.spp", rid, exists_fn=existing.__contains__)
    assert second[1].endswith("_2.spp")
    existing.add(second[0])
    third = plugin.rebase_backup_path("C:/work/Demo.spp", rid, exists_fn=existing.__contains__)
    assert third[1].endswith("_3.spp")
    with pytest.raises(ValueError):
        plugin.rebase_backup_path("", rid)
    with pytest.raises(ValueError):
        plugin.rebase_backup_path("C:/work/Demo.spp", "bad")


def test_retry_delay_and_reload_helpers():
    expected = [0.5, 1.0, 2.0, 4.0, 8.0]
    for attempt, delay in enumerate(expected, start=1):
        assert plugin.rebase_retry_delay(attempt, 5) == delay
    assert plugin.rebase_retry_delay(6, 10) == 8.0
    assert plugin.rebase_retry_delay(6, 5) is None
    assert plugin.rebase_retry_delay(1, 0) is None
    named = types.SimpleNamespace(name="SUCCESS")
    assert plugin.classify_reload_status(named) == "success"
    assert plugin.classify_reload_status(types.SimpleNamespace(name="ERROR")) == "error"
    assert plugin.classify_reload_status("ReloadMeshStatus.SUCCESS") == "success"
    assert plugin.classify_reload_status("other") == "other"
    assert plugin.classify_reload_status(3) == "other"
    assert plugin.classify_reload_status(None) == "other"
    request = _ok_request()
    with_auto = plugin.build_reload_settings_kwargs(request, True)
    assert with_auto == {
        "import_cameras": False,
        "preserve_strokes": True,
        "auto_unwrap_settings": None,
    }
    without = plugin.build_reload_settings_kwargs(request, False)
    assert without == {"import_cameras": False, "preserve_strokes": True}
    assert "mesh_settings" not in with_auto
    bad = dict(request)
    bad["reload"] = {
        "preserve_strokes": False,
        "import_cameras": False,
        "auto_unwrap": False,
    }
    with pytest.raises(ValueError):
        plugin.build_reload_settings_kwargs(bad, True)


# ---------------------------------------------------------------------------
# 16. 文言
# ---------------------------------------------------------------------------
def test_approval_text_and_constants():
    request = _ok_request()
    request["ts_names"] = ["Body", "Trim"]
    text = plugin.build_rebase_approval_text(
        request, backup_dir="C:/SPMayaLiveSync/uvremap/demo/rebase_backup")
    assert "2D ビュー" in text
    assert "C:/SPMayaLiveSync/uvremap/demo/rebase_backup" in text
    assert "Body, Trim" in text
    assert plugin.REBASE_WORKFLOW_GUIDANCE in text
    assert "見つからないデータのみを生成" not in text
    reproj = plugin.build_rebase_approval_text(
        request,
        backup_dir="C:/SPMayaLiveSync/uvremap/demo/rebase_backup",
        uv_fill_reproject=True,
    )
    assert "2D ビュー" in reproj
    assert "C:/SPMayaLiveSync/uvremap/demo/rebase_backup" in reproj
    assert plugin.REBASE_WORKFLOW_GUIDANCE in reproj
    assert "旧 UV セット" in reproj
    unsaved = plugin.build_rebase_approval_text(
        request,
        backup_dir="backup",
        needs_saving=True,
        backup_includes_unsaved=True,
    )
    assert plugin.REBASE_UNSAVED_NOTE in unsaved
    no_unsaved = plugin.build_rebase_approval_text(
        request,
        backup_dir="backup",
        needs_saving=True,
        backup_includes_unsaved=False,
    )
    assert plugin.REBASE_UNSAVED_NOTE not in no_unsaved
    g07 = plugin.build_rebase_approval_text(
        request, backup_dir="backup", can_disable_auto_unwrap=False)
    assert plugin.REBASE_AUTO_UNWRAP_NOTE in g07
    assert "見つからないデータのみを生成" in g07
    assert "すべて再計算" in g07
    assert "止められない可能性" not in plugin.REBASE_AUTO_UNWRAP_NOTE
    assert plugin.REBASE_AUTO_UNWRAP_NOTE not in text
    assert plugin.REBASE_BUTTON_RUN == "実行する"
    assert plugin.REBASE_BUTTON_CANCEL == "やめる"
    assert plugin.REBASE_DIALOG_TITLE
    assert plugin.REBASE_CONFIRM_LEAD
    assert plugin.REBASE_CAUTION_TEXT
    assert plugin.REBASE_CAUTION_TEXT_REPROJECT
    assert plugin.REBASE_DONE_GUIDANCE
    assert plugin.REBASE_WORKFLOW_GUIDANCE


# ---------------------------------------------------------------------------
# 17. 終端までの結合
# ---------------------------------------------------------------------------
def test_roundtrip_to_done_with_maya(tmp_path):
    request = rb.build_request(
        request_id=RID,
        project_key=PROJECT_KEY,
        created_at=NOW,
        ttl_s=TTL,
        mesh=_mesh(),
        baseline_id="bl-1",
        ts_names=["Body"],
        uv_old={"name": "uvOld", "index": 1},
    )
    rb.write_request(tmp_path, request)
    scan = plugin.scan_rebase_dir(tmp_path)
    assert RID in scan.requests
    parsed = plugin.read_rebase_request(tmp_path, RID)
    assert parsed == rb.read_request(tmp_path, RID)
    decision = plugin.decide_approval(parsed, _ok_state(), _settings("ask"))
    assert decision == plugin.ApprovalDecision("ask", "needs_approval")
    sequence = (
        "received",
        "awaiting_approval",
        "preparing",
        "backing_up",
        "reloading",
        "exporting",
        "done",
    )
    updated = NOW
    for state in sequence:
        status = plugin.build_rebase_status(RID, state, updated, PLUGIN_VERSION)
        assert plugin.write_rebase_status(tmp_path, status) is True
        read = rb.read_status(tmp_path, RID)
        assert read.kind == "ok"
        assert read.status["state"] == state
        display = rb.decide_display(parsed, read, now=updated, cancel_requested=False)
        assert display.text
        if state == "done":
            assert display.terminal is True
            assert display.show_done_guidance is True
        updated += 1
    result = plugin.build_export_result(
        "1.3.0",
        "ex-roundtrip-1",
        "final",
        True,
        "success",
        "ok",
        1,
        0,
        ["Body"],
        True,
        NOW,
        NOW + 1,
        request_id=RID,
    )
    parsed_result = rb.parse_sync_result(result)
    assert parsed_result is not None
    judged = rb.judge_completion(
        status_read=rb.read_status(tmp_path, RID),
        sync_result=parsed_result,
        request=parsed,
        enabled=True,
        scene_id_now="scene-a",
        scene_id_request="scene-a",
        already_completed=False,
    )
    assert judged.action == "complete"


# ---------------------------------------------------------------------------
# 18. 0.5: unhashable な state / reason、keyword-only、依頼ごとのキャッシュ
# ---------------------------------------------------------------------------
_UNHASHABLE_VALUES = (
    [],
    {},
    ["failed"],
    {"a": 1},
    1,
    1.5,
    True,
    None,
    b"failed",
    set(),
)


@pytest.mark.parametrize("bad", _UNHASHABLE_VALUES)
def test_unhashable_state_reason_do_not_raise_typeerror(bad):
    assert plugin.parse_rebase_status({
        "schema": 1,
        "request_id": RID,
        "state": bad,
        "updated_at": 1,
    }) is None
    assert plugin.build_interrupted_status({"state": bad, "request_id": RID}, 1, PLUGIN_VERSION) is None
    # None は遷移表の始点として合法。それ以外の非 str は False。
    if bad is None:
        assert plugin.is_valid_rebase_transition(bad, "received") is True
    else:
        assert plugin.is_valid_rebase_transition(bad, "received") is False
    assert plugin.is_valid_rebase_transition("received", bad) is False
    assert plugin.rebase_failure_message(bad) == "リベースに失敗しました"
    with pytest.raises(ValueError):
        plugin.build_rebase_status(RID, bad, 1, PLUGIN_VERSION)
    if bad is None:
        # reason=None は省略と同じで合法。
        plugin.build_rebase_status(RID, "failed", 1, PLUGIN_VERSION, reason=None)
    else:
        with pytest.raises(ValueError):
            plugin.build_rebase_status(RID, "failed", 1, PLUGIN_VERSION, reason=bad)


def test_read_write_status_recovers_unhashable_existing_file(tmp_path):
    path = Path(plugin.rebase_status_path(tmp_path, RID))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema": 1,
            "request_id": RID,
            "state": ["failed"],
            "updated_at": 1,
        }),
        encoding="utf-8",
    )
    assert plugin.read_rebase_status(tmp_path, RID) is None
    recovered = plugin.build_rebase_status(RID, "received", 2, PLUGIN_VERSION)
    assert plugin.write_rebase_status(tmp_path, recovered) is True
    got = plugin.read_rebase_status(tmp_path, RID)
    assert got is not None
    assert got["state"] == "received"

    path.write_text(
        json.dumps({
            "schema": 1,
            "request_id": RID,
            "state": {"a": 1},
            "updated_at": 1,
        }),
        encoding="utf-8",
    )
    assert plugin.read_rebase_status(tmp_path, RID) is None
    again = plugin.build_rebase_status(RID, "rejected", 3, PLUGIN_VERSION, reason="bad_request")
    assert plugin.write_rebase_status(tmp_path, again) is True
    got2 = plugin.read_rebase_status(tmp_path, RID)
    assert got2["state"] == "rejected"


def _assert_keyword_only(fn, *names):
    import inspect
    params = inspect.signature(fn).parameters
    for name in names:
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name


def test_pb_a_functions_are_keyword_only():
    _assert_keyword_only(
        plugin.build_rebase_status,
        "reason", "message", "backup_name", "result_export_id", "uv_fills",
    )
    _assert_keyword_only(plugin.rebase_failure_message, "backup_name", "detail")
    _assert_keyword_only(plugin.mesh_precheck, "stat_fn", "hash_fn")
    _assert_keyword_only(
        plugin.build_painter_state,
        "now", "is_open", "in_edition", "project_key", "saved_path",
        "busy", "exporting", "needs_saving", "backup_includes_unsaved",
        "mesh_exists", "mesh_sha1_ok", "superseded", "cancelled",
        "can_disable_auto_unwrap", "last_edit_at",
    )
    _assert_keyword_only(
        plugin.build_rebase_approval_text,
        "backup_dir", "needs_saving", "can_disable_auto_unwrap",
        "backup_includes_unsaved", "uv_fill_reproject", "has_direction_fills",
    )
    with pytest.raises(TypeError):
        plugin.build_rebase_status(RID, "failed", 1, PLUGIN_VERSION, "busy")
    with pytest.raises(TypeError):
        plugin.rebase_failure_message("export_failed", "keep.spp")
    with pytest.raises(TypeError):
        plugin.mesh_precheck({"path": "x"}, None, os.stat)
    with pytest.raises(TypeError):
        plugin.build_painter_state(
            NOW, True, True, PROJECT_KEY, PROJECT_KEY,
            False, False, False, False, True, True, False, False, True, None,
        )
    with pytest.raises(TypeError):
        plugin.build_rebase_approval_text(_ok_request(), "backup")


def test_precheck_cache_is_per_request(tmp_path):
    path = tmp_path / "shared.fbx"
    path.write_bytes(b"hello")
    digest_a = hashlib.sha1(b"hello").hexdigest()
    digest_b = "b" * 40
    mesh_a = {"path": str(path), "size": 5, "sha1": digest_a}
    mesh_b = {"path": str(path), "size": 5, "sha1": digest_b}
    rid_a = _rid(21)
    rid_b = _rid(22)
    stores = plugin.RebasePrecheckCaches()
    calls = []

    def counting_hash(hash_path):
        calls.append(hash_path)
        return hashlib.sha1(Path(hash_path).read_bytes()).hexdigest()

    exists_a, ok_a, cache_a = plugin.mesh_precheck(
        mesh_a, cache=stores.get(rid_a), hash_fn=counting_hash)
    stores.put(rid_a, cache_a)
    assert exists_a is True and ok_a is True
    assert len(calls) == 1

    exists_b, ok_b, cache_b = plugin.mesh_precheck(
        mesh_b, cache=stores.get(rid_b), hash_fn=counting_hash)
    stores.put(rid_b, cache_b)
    assert exists_b is True and ok_b is False
    assert rid_a in stores and rid_b in stores
    assert len(calls) == 2
    leaked = plugin.mesh_precheck(mesh_b, cache=stores.get(rid_a), hash_fn=counting_hash)
    assert leaked[1] is True
    assert len(calls) == 2

    exists_a2, ok_a2, cache_a2 = plugin.mesh_precheck(
        mesh_a, cache=stores.get(rid_a), hash_fn=counting_hash)
    exists_b2, ok_b2, cache_b2 = plugin.mesh_precheck(
        mesh_b, cache=stores.get(rid_b), hash_fn=counting_hash)
    assert exists_a2 is True and ok_a2 is True
    assert exists_b2 is True and ok_b2 is False
    assert cache_a2 is cache_a
    assert cache_b2 is cache_b
    assert len(calls) == 2

    stores.drop_if_terminal(rid_a, "done")
    stores.drop_if_terminal(rid_b, "preparing")
    assert rid_a not in stores
    assert rid_b in stores
    stores.clear()
    assert rid_b not in stores
    assert len(stores) == 0
