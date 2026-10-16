# -*- coding: utf-8 -*-
"""uvremap_bridge.py のテスト(Maya 無し・Pillow 無し。合成配列のみ)。

単体は各公開関数と異常系。一周は立方体/平面の配列 → Snapshot → job.json →
実ワーカー CLI → 出力確認、のあと STALE / BLOCKED。
時刻はすべて注入する(datetime.now に依存しない)。
"""
from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
import warnings
from pathlib import Path

import numpy as np
import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uvremap_bridge as br  # noqa: E402
import uvremap_core as core  # noqa: E402
from png_testkit import fast_encode_png  # noqa: E402

WORKER_PY = _PKG / "uvremap_worker.py"
FIXED_CREATED = "2026-01-02T03:04:05"
FIXED_STATUS = "2026-03-04T05:06:07"
TS = "Body"

_RUN_SLOW = bool(os.environ.get("UVREMAP_SLOW"))
warnings.filterwarnings("ignore", category=pytest.PytestUnknownMarkWarning)


def slow(fn):
    fn = pytest.mark.slow(fn)
    return fn if _RUN_SLOW else pytest.mark.skip(reason="slow test: set UVREMAP_SLOW=1")(fn)


# ============================================================================
# 合成配列(collect_arrays と同じキー。Maya は使わない)
# ============================================================================
def _info(ts_names, mesh_uuids, bbox_min, bbox_max, **extra):
    info = {
        "ts_names": list(ts_names),
        "mesh_uuids": list(mesh_uuids),
        "uv_set": extra.pop("uv_set", "map1"),
        "bbox_min": [float(x) for x in bbox_min],
        "bbox_max": [float(x) for x in bbox_max],
        "warnings": list(extra.pop("warnings", [])),
    }
    info.update(extra)
    return info


def plane_arrays(*, uv_shift=(0.0, 0.0), nx=2, ny=2):
    """分割した平面(四角形グリッド)。UV は [0,1]。"""
    pos, vid, uv, counts = [], [], [], []
    gx, gy = np.meshgrid(np.linspace(0.0, 1.0, nx + 1), np.linspace(0.0, 1.0, ny + 1),
                         indexing="xy")
    P = np.stack([gx, gy, np.zeros_like(gx)], -1).reshape(-1, 3)
    Q = np.stack([gx, gy], -1).reshape(-1, 2)
    idx = np.arange((nx + 1) * (ny + 1)).reshape(ny + 1, nx + 1)
    v00, v10 = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel()
    v01, v11 = idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    faces = np.stack([v00, v10, v11, v01], 1)
    pos.append(P[faces].reshape(-1, 3))
    uv.append(Q[faces].reshape(-1, 2))
    vid.append(faces.ravel())
    counts.append(np.full(len(faces), 4, dtype=np.int32))
    counts = np.concatenate(counts)
    corner_pos = np.concatenate(pos).astype(np.float32)
    corner_uv = (np.concatenate(uv) + np.asarray(uv_shift, dtype=np.float64)).astype(np.float32)
    corner_vid = np.concatenate(vid).astype(np.int32)
    n_face = int(counts.size)
    tri_face = np.repeat(np.arange(n_face, dtype=np.int32), 2)
    tri_corner = np.tile(np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int8), (n_face, 1))
    arrays = {
        "face_vertex_counts": counts,
        "corner_vid": corner_vid,
        "corner_pos": corner_pos,
        "corner_uv": corner_uv,
        "face_mesh": np.zeros(n_face, dtype=np.int32),
        "tri_face": tri_face,
        "tri_corner": tri_corner,
        "ts_faces__0": np.arange(n_face, dtype=np.int32),
    }
    info = _info(
        [TS], ["mesh-plane"],
        corner_pos.min(axis=0), corner_pos.max(axis=0),
    )
    return arrays, info


def cube_arrays(*, uv_shift=(0.0, 0.0)):
    """立方体(6四角形)。各面を [0,1] に展開。"""
    faces_xyz = [
        np.array([[1, -1, -1], [1, 1, -1], [1, 1, 1], [1, -1, 1]], np.float32),
        np.array([[-1, 1, -1], [-1, -1, -1], [-1, -1, 1], [-1, 1, 1]], np.float32),
        np.array([[-1, 1, -1], [1, 1, -1], [1, 1, 1], [-1, 1, 1]], np.float32),
        np.array([[1, -1, -1], [-1, -1, -1], [-1, -1, 1], [1, -1, 1]], np.float32),
        np.array([[-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], np.float32),
        np.array([[-1, 1, -1], [1, 1, -1], [1, -1, -1], [-1, -1, -1]], np.float32),
    ]
    uv0 = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)
    pos, vid, uv, counts = [], [], [], []
    off = 0
    for k, xyz in enumerate(faces_xyz):
        pos.append(xyz)
        uv.append(uv0 + np.asarray(uv_shift, dtype=np.float32))
        vid.append(np.arange(4, dtype=np.int32) + off)
        counts.append(4)
        off += 4
    n_face = 6
    arrays = {
        "face_vertex_counts": np.asarray(counts, dtype=np.int32),
        "corner_vid": np.concatenate(vid).astype(np.int32),
        "corner_pos": np.concatenate(pos).astype(np.float32),
        "corner_uv": np.concatenate(uv).astype(np.float32),
        "face_mesh": np.zeros(n_face, dtype=np.int32),
        "tri_face": np.repeat(np.arange(n_face, dtype=np.int32), 2),
        "tri_corner": np.tile(np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int8), (n_face, 1)),
        "ts_faces__0": np.arange(n_face, dtype=np.int32),
    }
    pos_all = arrays["corner_pos"]
    info = _info([TS], ["mesh-cube"], pos_all.min(axis=0), pos_all.max(axis=0))
    return arrays, info


def split_sphere_arrays(*, n=2, uv_shift=(0.0, 0.0)):
    """立方体を細分して球に正規化したメッシュ(6シェル)。"""
    faces = [
        ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
        ((0, 1, 0), (0, 0, 1), (1, 0, 0)),
        ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
        ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
        ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
    ]
    pos, vid, uv, shell = [], [], [], []
    off = 0
    g = np.linspace(-1.0, 1.0, n + 1)
    S, T = np.meshgrid(g, g, indexing="xy")
    for k, (nn, a, b) in enumerate(faces):
        nn, a, b = map(np.asarray, (nn, a, b))
        P = nn + S[..., None] * a + T[..., None] * b
        P = P / np.linalg.norm(P, axis=-1, keepdims=True)
        P = P.reshape(-1, 3)
        q = np.stack([(S + 1) / 2, (T + 1) / 2], -1).reshape(-1, 2)
        idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
        v00, v10 = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel()
        v01, v11 = idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
        f = np.concatenate([np.stack([v00, v10, v11], 1), np.stack([v00, v11, v01], 1)])
        pos.append(P[f].reshape(-1, 3))
        uv.append(q[f].reshape(-1, 2) + np.asarray(uv_shift, dtype=np.float64))
        vid.append((f + off).ravel())
        shell.append(np.full(len(f), k, dtype=np.int32))
        off += len(P)
    n_face = int(sum(len(s) for s in shell))
    arrays = {
        "face_vertex_counts": np.full(n_face, 3, dtype=np.int32),
        "corner_vid": np.concatenate(vid).astype(np.int32),
        "corner_pos": np.concatenate(pos).astype(np.float32),
        "corner_uv": np.concatenate(uv).astype(np.float32),
        "face_mesh": np.zeros(n_face, dtype=np.int32),
        "tri_face": np.arange(n_face, dtype=np.int32),
        "tri_corner": np.tile(np.array([[0, 1, 2]], dtype=np.int8), (n_face, 1)),
        "ts_faces__0": np.arange(n_face, dtype=np.int32),
    }
    pos_all = arrays["corner_pos"]
    info = _info([TS], ["mesh-sphere"], pos_all.min(axis=0), pos_all.max(axis=0))
    return arrays, info


def with_uv(arrays, info, corner_uv, **info_over):
    out = dict(arrays)
    out["corner_uv"] = np.asarray(corner_uv, dtype=np.float32)
    new_info = dict(info)
    new_info.update(info_over)
    return out, new_info


def snap_from(arrays, info, **kw):
    return br.arrays_to_snapshot(arrays, info, created=FIXED_CREATED, **kw)


# ============================================================================
# import / 時計
# ============================================================================
def test_import_does_not_load_maya_or_pillow():
    before = {n for n in sys.modules if n == "maya" or n.startswith("maya.")
              or n == "uvremap_maya" or n == "PIL" or n.startswith("PIL.")}
    import importlib
    importlib.reload(br)
    after = {n for n in sys.modules if n == "maya" or n.startswith("maya.")
             or n == "uvremap_maya" or n == "PIL" or n.startswith("PIL.")}
    assert after == before


def test_source_has_no_maya_import():
    source = Path(br.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("maya")
                assert alias.name != "uvremap_maya"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("maya")
            assert mod != "uvremap_maya"


def test_naive_now_iso_is_the_only_clock():
    """datetime.now は _naive_now_iso の1箇所だけ。呼び出し側は now_fn / created で注入する。"""
    source = Path(br.__file__).read_text(encoding="utf-8")
    assert source.count("datetime.now") == 1
    assert "def _naive_now_iso" in source
    assert "now_fn or _naive_now_iso" in source


# ============================================================================
# arrays_to_snapshot
# ============================================================================
@pytest.mark.parametrize("factory", [plane_arrays, cube_arrays, split_sphere_arrays])
def test_arrays_to_snapshot_fills_meta_from_core_and_info(factory):
    arrays, info = factory()
    snap = snap_from(arrays, info, baseline_id="bl_test")
    assert snap.meta["schema"] == 1
    assert snap.meta["baseline_id"] == "bl_test"
    assert snap.meta["created"] == FIXED_CREATED
    assert snap.meta["ts_names"] == info["ts_names"]
    assert snap.meta["mesh_uuids"] == info["mesh_uuids"]
    assert snap.meta["uv_set"] == info["uv_set"]
    assert snap.meta["bbox_min"] == [float(x) for x in info["bbox_min"]]
    assert snap.meta["bbox_max"] == [float(x) for x in info["bbox_max"]]
    assert snap.meta["topology_fingerprint"] == core.compute_topology_fingerprint(snap)
    assert snap.meta["uv_hash"] == core.compute_uv_hash(snap)
    assert snap.ts_names == info["ts_names"]
    np.testing.assert_array_equal(snap.faces_of(TS), arrays["ts_faces__0"])


def test_arrays_to_snapshot_inherits_warnings_and_excluded_faces():
    arrays, info = plane_arrays()
    info["warnings"] = ["nonmanifold_faces", "no_uv_faces"]
    info["nonmanifold_faces"] = [1]
    info["untriangulated_faces"] = []
    info["no_uv_faces"] = [2, 3]
    snap = snap_from(arrays, info)
    assert "nonmanifold_faces" in snap.warnings
    assert "no_uv_faces" in snap.warnings
    assert "nonmanifold_faces:1" in snap.warnings
    assert "no_uv_faces:2,3" in snap.warnings
    assert not any(w.startswith("untriangulated_faces:") for w in snap.warnings)


def test_arrays_to_snapshot_ts_faces_mismatch_raises():
    arrays, info = plane_arrays()
    arrays["ts_faces__1"] = np.array([0], dtype=np.int32)
    with pytest.raises(br.BridgeError, match="ts_faces"):
        snap_from(arrays, info)
    del arrays["ts_faces__1"]
    del arrays["ts_faces__0"]
    with pytest.raises(br.BridgeError, match="ts_faces"):
        snap_from(arrays, info)
    arrays["ts_faces__01"] = np.array([0], dtype=np.int32)
    with pytest.raises(br.BridgeError, match="ts_faces"):
        snap_from(arrays, info)


def test_arrays_to_snapshot_two_texture_sets_keep_order():
    arrays, info = plane_arrays()
    n = int(arrays["face_vertex_counts"].size)
    mid = n // 2
    arrays["ts_faces__0"] = np.arange(mid, dtype=np.int32)
    arrays["ts_faces__1"] = np.arange(mid, n, dtype=np.int32)
    info["ts_names"] = ["Body", "Trim"]
    snap = snap_from(arrays, info)
    assert snap.ts_names == ["Body", "Trim"]
    np.testing.assert_array_equal(snap.faces_of("Body"), arrays["ts_faces__0"])
    np.testing.assert_array_equal(snap.faces_of("Trim"), arrays["ts_faces__1"])


def test_arrays_to_snapshot_missing_array_key():
    arrays, info = plane_arrays()
    del arrays["corner_uv"]
    with pytest.raises(br.BridgeError, match="corner_uv"):
        snap_from(arrays, info)


def test_arrays_to_snapshot_missing_ts_names():
    arrays, info = plane_arrays()
    del info["ts_names"]
    with pytest.raises(br.BridgeError, match="ts_names"):
        snap_from(arrays, info)


def test_arrays_to_snapshot_rejects_non_dict():
    with pytest.raises(br.BridgeError, match="arrays"):
        br.arrays_to_snapshot([], {}, created=FIXED_CREATED)
    with pytest.raises(br.BridgeError, match="info"):
        br.arrays_to_snapshot({}, [], created=FIXED_CREATED)


# ============================================================================
# save_baseline / save_current
# ============================================================================
def test_save_baseline_writes_then_refuses_overwrite(tmp_path):
    arrays, info = cube_arrays()
    snap = snap_from(arrays, info, baseline_id="bl_keep")
    dest = br.save_baseline(snap, tmp_path)
    assert dest == tmp_path / "baseline" / "bl_keep"
    assert (dest / "mesh.npz").is_file() and (dest / "meta.json").is_file()
    first = (dest / "mesh.npz").read_bytes()
    meta1 = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
    moved, info2 = cube_arrays(uv_shift=(0.25, 0.0))
    snap2 = snap_from(moved, info2, baseline_id="bl_keep")
    dest2 = br.save_baseline(snap2, tmp_path)
    assert dest2 == dest
    assert (dest / "mesh.npz").read_bytes() == first
    meta2 = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
    assert meta2["uv_hash"] == meta1["uv_hash"]
    assert meta2["uv_hash"] != snap2.meta["uv_hash"]


def test_save_current_path(tmp_path):
    arrays, info = plane_arrays()
    snap = snap_from(arrays, info)
    dest = br.save_current(snap, tmp_path, "job-a")
    assert dest == tmp_path / "jobs" / "job-a" / "current"
    back = core.load_snapshot(dest)
    assert back.meta["uv_hash"] == snap.meta["uv_hash"]


def test_save_baseline_requires_baseline_id(tmp_path):
    arrays, info = plane_arrays()
    snap = snap_from(arrays, info, baseline_id="bl_x")
    snap.meta.pop("baseline_id")
    with pytest.raises(br.BridgeError, match="baseline_id"):
        br.save_baseline(snap, tmp_path)


# ============================================================================
# current_uv_hash / next_generation
# ============================================================================
def test_current_uv_hash_is_per_texture_set():
    arrays, info = plane_arrays()
    n = int(arrays["face_vertex_counts"].size)
    mid = max(1, n // 2)
    arrays["ts_faces__0"] = np.arange(mid, dtype=np.int32)
    arrays["ts_faces__1"] = np.arange(mid, n, dtype=np.int32)
    info["ts_names"] = ["Body", "Trim"]
    snap = snap_from(arrays, info)
    body = br.current_uv_hash(snap, "Body")
    trim = br.current_uv_hash(snap, "Trim")
    whole = core.compute_uv_hash(snap)
    assert body == core.compute_uv_hash(snap, "Body")
    assert trim == core.compute_uv_hash(snap, "Trim")
    assert body != trim
    assert body != whole
    assert "テクスチャセット単位" in br.current_uv_hash.__doc__
    assert "E4" in br.current_uv_hash.__doc__


def test_next_generation_missing_is_one():
    assert br.next_generation({}, TS) == 1
    assert br.next_generation({"schema": 1, "ts": {}}, TS) == 1
    assert br.next_generation({"ts": {TS: {}}}, TS) == 1
    assert br.next_generation({"ts": {TS: {"generation": 7}}}, TS) == 8
    assert br.next_generation({"ts": {"Other": {"generation": 4}}}, TS) == 1
    assert br.next_generation(None, TS) == 1


# ============================================================================
# choose_timeout_s
# ============================================================================
def test_choose_timeout_s_a1_floor_and_cap():
    # A1: 2048²・4 タイル・5 ch → 見積もり ≈ 26.8 秒 → 3x+30 ≈ 111 → 下限 120
    assert br.choose_timeout_s(2048, 5, 4) == 120
    # 小さな解像度も下限
    assert br.choose_timeout_s(64, 1, 1) == 120
    # 4096²・4 タイル・6 ch: 3*128.85+30 ≈ 416.5 → 417(E7: 300 では足りない)
    assert br.choose_timeout_s(4096, 6, 4) == 417
    assert br.choose_timeout_s(4096, 6, 4) > 300
    # 上限 1800
    assert br.choose_timeout_s(8192, 8, 16) == 1800


def test_choose_timeout_s_accepts_tile_dict():
    tiles = {1001: (2048, 2048), 1002: (2048, 2048),
             1011: (2048, 2048), 1012: (2048, 2048)}
    assert br.choose_timeout_s(tiles, 5, 4) == br.choose_timeout_s(2048, 5, 4)
    mixed = {1001: (1024, 512), 1002: (256, 256)}
    # ピクセル = 1024*512 + 256*256、ch=2
    got = br.choose_timeout_s(mixed, 2, 99)
    pixels = 1024 * 512 + 256 * 256
    estimate = (pixels / 1e6) * 2 * 0.32
    expect = int(min(1800, max(120, math.ceil(3 * estimate + 30))))
    assert got == expect


def test_choose_timeout_s_rejects_bad_input():
    with pytest.raises(br.BridgeError):
        br.choose_timeout_s(-4, 1, 1)
    with pytest.raises(br.BridgeError):
        br.choose_timeout_s(0, 1, 1)
    with pytest.raises(br.BridgeError):
        br.choose_timeout_s(True, 1, 1)
    with pytest.raises(br.BridgeError):
        br.choose_timeout_s(256, -1, 1)
    with pytest.raises(br.BridgeError):
        br.choose_timeout_s({1001: (-1, 8)}, 1, 1)
    with pytest.raises(br.BridgeError):
        br.choose_timeout_s("256", 1, 1)


# ============================================================================
# build_job / write_job
# ============================================================================
def _job_kwargs(root: Path, **over):
    kw = dict(
        job_id="20260102-030405-ab12",
        generation=1,
        ts_name=TS,
        quality="final",
        baseline_dir=str(root / "baseline" / "bl1"),
        current_snapshot_dir=str(root / "jobs" / "j1" / "current"),
        source_dir=str(root / "src"),
        source_flag=str(root / "src" / "_sync_complete.flag"),
        output_dir=str(root / "output" / "final"),
        uvremap_root=str(root),
        udim=False,
        out_res=256,
        timeout_s=120,
    )
    kw.update(over)
    return kw


def test_build_job_defaults_and_required_keys(tmp_path):
    job = br.build_job(**_job_kwargs(tmp_path))
    assert job["schema"] == 1
    assert job["quality"] == "final"
    assert job["channels"] == [dict(ch) for ch in br.DEFAULT_CHANNELS]
    assert job["dilate_px"] == 16 and job["normal_mode"] == "uv"
    assert job["copy_retries"] == 3
    assert job["source_flag"].endswith("_sync_complete.flag")
    with pytest.raises(br.BridgeError, match="job_id"):
        br.build_job(**_job_kwargs(tmp_path, job_id=""))
    with pytest.raises(br.BridgeError, match="quality"):
        br.build_job(**_job_kwargs(tmp_path, quality="draft"))
    with pytest.raises(br.BridgeError, match="generation"):
        br.build_job(**_job_kwargs(tmp_path, generation="1"))
    with pytest.raises(br.BridgeError, match="ts_name"):
        br.build_job(**_job_kwargs(tmp_path, ts_name=None))
    with pytest.raises(br.BridgeError, match="channels"):
        br.build_job(**_job_kwargs(tmp_path), channels=[])


def test_write_job_is_atomic_and_readable(tmp_path):
    job = br.build_job(**_job_kwargs(tmp_path), quality="live")
    path = tmp_path / "jobs" / "j1" / "job.json"
    written = br.write_job(job, path)
    assert written == path
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["job_id"] == job["job_id"]
    assert loaded["quality"] == "live"
    assert not list(path.parent.glob("*.tmp"))


# ============================================================================
# decide_display_state
# ============================================================================
def _dec(method, reasons=None):
    return core.Decision(method, list(reasons or []), [], {})


def test_decide_display_state_kill_switch_and_stop():
    entry = {"uv_hash": "aaa", "generation": 3}
    state, msg = br.decide_display_state(entry, "aaa", _dec("F"), enabled=False)
    assert state == "ORIGINAL" and "キルスイッチ" in msg
    state, msg = br.decide_display_state(entry, "aaa", _dec("STOP", ["topology_changed"]),
                                         enabled=True)
    assert state == "BLOCKED" and "トポロジー" in msg
    state, msg = br.decide_display_state(entry, "aaa", {"method": "STOP", "reasons": ["ts_missing"]},
                                         enabled=True)
    assert state == "BLOCKED" and "テクスチャセット" in msg


def test_decide_display_state_ledger_hash():
    dec = _dec("F")
    state, msg = br.decide_display_state(None, "hhh", dec, enabled=True)
    assert state == "ORIGINAL" and "未変換" in msg
    state, _msg = br.decide_display_state({"generation": 1}, "hhh", dec, enabled=True)
    assert state == "ORIGINAL"
    state, msg = br.decide_display_state({"uv_hash": "hhh"}, "hhh", dec, enabled=True)
    assert state == "CONVERTED_OK" and msg == ""
    state, msg = br.decide_display_state({"uv_hash": "old"}, "hhh", dec, enabled=True)
    assert state == "STALE" and "一致しない" in msg


# ============================================================================
# update_status_entry
# ============================================================================
def test_update_status_entry_keeps_other_ts_and_uses_now_fn(tmp_path):
    root = tmp_path / "uvremap" / "proj"
    root.mkdir(parents=True)
    existing = {
        "schema": 1,
        "ts": {
            "Other": {
                "state": "CONVERTED_OK",
                "generation": 4,
                "updated": "2020-01-01T00:00:00",
                "message": "keep-me",
                "job_id": "worker-other",
                "pid": 4242,
            },
            TS: {
                "state": "CONVERTING",
                "generation": 1,
                "updated": "2020-01-01T00:00:00",
                "message": "",
                "job_id": "worker-body",
                "pid": 111,
            },
        },
    }
    (root / "status.json").write_text(json.dumps(existing), encoding="utf-8")
    stamps = []

    def now_fn():
        stamps.append(FIXED_STATUS)
        return FIXED_STATUS

    br.update_status_entry(root, TS, "STALE", 2, "古い", now_fn=now_fn)
    data = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert data["ts"]["Other"] == existing["ts"]["Other"]
    body = data["ts"][TS]
    assert body["state"] == "STALE"
    assert body["generation"] == 2
    assert body["updated"] == FIXED_STATUS
    assert body["message"] == "古い"
    assert body["job_id"] == "worker-body"
    assert stamps == [FIXED_STATUS]


def test_update_status_entry_creates_file(tmp_path):
    root = tmp_path / "root"
    br.update_status_entry(root, TS, "ORIGINAL", 1, "", now_fn=lambda: FIXED_STATUS)
    data = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert data["ts"][TS]["updated"] == FIXED_STATUS
    assert data["ts"][TS]["state"] == "ORIGINAL"


# ============================================================================
# 一周: 橋渡し → 実ワーカー CLI → 表示状態
# ============================================================================
def _smooth_png(path: Path, res: int) -> None:
    yy, xx = np.meshgrid(
        np.linspace(0.0, 1.0, res, dtype=np.float32),
        np.linspace(0.0, 1.0, res, dtype=np.float32),
        indexing="ij",
    )
    img = np.stack([xx, yy, 0.5 * (xx + yy)], -1)
    u8 = np.clip(np.rint(img * 255.0), 0, 255).astype(np.uint8)
    path.write_bytes(fast_encode_png(u8, [0] * res))


def _run_worker(job_path: Path, timeout=90):
    proc = subprocess.run(
        [sys.executable, str(WORKER_PY), "run", str(job_path)],
        capture_output=True, text=True, timeout=timeout,
    )
    result_path = job_path.parent / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    return proc.returncode, result, proc.stdout + proc.stderr


def test_roundtrip_bridge_to_worker_cli(tmp_path):
    """合成平面: 基準 → UV移動した現行 → job → 実 CLI → OK / STALE / BLOCKED。"""
    root = tmp_path / "uvremap" / "proj"
    src = tmp_path / "painter" / "Final"
    out = root / "output" / "final"
    src.mkdir(parents=True)
    res = 256
    _smooth_png(src / "{0}_BaseColor.png".format(TS), res)
    (src / "_sync_complete.flag").write_text("{}", encoding="utf-8")

    base_arr, base_info = plane_arrays(nx=4, ny=4)
    baseline = snap_from(base_arr, base_info, baseline_id="bl_plane")
    baseline_dir = br.save_baseline(baseline, root)

    cur_arr, cur_info = plane_arrays(nx=4, ny=4, uv_shift=(0.12, -0.08))
    current = snap_from(cur_arr, cur_info, baseline_id="bl_plane")
    assert current.meta["topology_fingerprint"] == baseline.meta["topology_fingerprint"]
    assert current.meta["uv_hash"] != baseline.meta["uv_hash"]

    job_id = "j-round"
    current_dir = br.save_current(current, root, job_id)
    uvh = br.current_uv_hash(current, TS)
    gen = br.next_generation({}, TS)
    assert gen == 1
    timeout_s = br.choose_timeout_s(res, 1, 1)
    job = br.build_job(
        job_id=job_id,
        generation=gen,
        ts_name=TS,
        quality="final",
        baseline_dir=str(baseline_dir),
        current_snapshot_dir=str(current_dir),
        source_dir=str(src),
        source_flag=str(src / "_sync_complete.flag"),
        output_dir=str(out),
        uvremap_root=str(root),
        udim=False,
        out_res=res,
        timeout_s=timeout_s,
        copy_retries=1,
        lock_timeout_s=10,
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
        uv_hash=uvh,
    )
    job_path = br.write_job(job, root / "jobs" / job_id / "job.json")
    code, result, log = _run_worker(job_path)
    assert code == 0, (result, log)
    assert result.get("status") == "ok"
    assert result.get("exit_code") == 0
    assert (out / "{0}_BaseColor.png".format(TS)).is_file()
    probe = result["probe"]["BaseColor"]
    assert probe["status"] in ("pass", "inconclusive"), probe
    if probe["status"] == "pass":
        assert probe["n_bad"] == 0
    ledger = json.loads((root / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["ts"][TS]["uv_hash"] == uvh
    decision = core.classify(baseline, current, TS)
    assert decision.method == "F"
    state, _msg = br.decide_display_state(ledger["ts"][TS], uvh, decision, enabled=True)
    assert state == "CONVERTED_OK"

    # UV をさらに動かす → STALE(D-3: 直前の正常世代のハッシュと一致しない)
    stale_arr, stale_info = plane_arrays(nx=4, ny=4, uv_shift=(0.40, 0.10))
    stale = snap_from(stale_arr, stale_info, baseline_id="bl_plane")
    stale_hash = br.current_uv_hash(stale, TS)
    assert stale_hash != uvh
    state, msg = br.decide_display_state(
        ledger["ts"][TS], stale_hash, core.classify(baseline, stale, TS), enabled=True)
    assert state == "STALE" and "一致しない" in msg

    # トポロジー変更 → BLOCKED
    broken_arr, broken_info = plane_arrays(nx=4, ny=4, uv_shift=(0.12, -0.08))
    broken_arr["corner_vid"] = broken_arr["corner_vid"].copy()
    broken_arr["corner_vid"][0] = int(broken_arr["corner_vid"].max()) + 9
    broken = snap_from(broken_arr, broken_info, baseline_id="bl_plane")
    stop = core.classify(baseline, broken, TS)
    assert stop.method == "STOP" and "topology_changed" in stop.reasons
    state, msg = br.decide_display_state(ledger["ts"][TS], uvh, stop, enabled=True)
    assert state == "BLOCKED" and "トポロジー" in msg

    # ワーカーが書いた他 TS 行を、橋渡しの status 更新が壊さない
    status_path = root / "status.json"
    data = json.loads(status_path.read_text(encoding="utf-8"))
    data["ts"]["Trim"] = {
        "state": "CONVERTED_OK",
        "generation": 9,
        "updated": "2019-01-01T00:00:00",
        "message": "from-worker",
        "job_id": "other-job",
        "pid": 7,
    }
    status_path.write_text(json.dumps(data), encoding="utf-8")
    br.update_status_entry(root, TS, "STALE", 2, "再変換", now_fn=lambda: FIXED_STATUS)
    after = json.loads(status_path.read_text(encoding="utf-8"))
    assert after["ts"]["Trim"] == data["ts"]["Trim"]
    assert after["ts"][TS]["state"] == "STALE"
    assert after["ts"][TS]["updated"] == FIXED_STATUS
