# -*- coding: utf-8 -*-
"""uvremap_runtime.py のテスト(Maya 無し・Pillow 無し)。

時刻は now_fn だけを見る。cmds / Popen / 配列収集は差し替える。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uvremap_runtime as rt  # noqa: E402
from png_testkit import fast_encode_png  # noqa: E402
from test_uvremap_bridge import plane_arrays  # noqa: E402

TS = "Body"
FIXED_NOW = "2026-03-04T05:06:07"
WORKER_PY = _PKG / "uvremap_worker.py"
RES = 256


# ---------------------------------------------------------------------------
# 偽 cmds / 偽プロセス / ヘルパ
# ---------------------------------------------------------------------------
class FakeCmds:
    """ノード辞書で ls / getAttr / setAttr / addAttr / deleteAttr / attributeQuery を模す。"""

    def __init__(self, nodes=None):
        self.nodes = {}
        for name, data in (nodes or {}).items():
            self.add_node(name, **data)

    def add_node(self, name, fileTextureName="", uvTilingMode=0, extra=None):
        self.nodes[name] = {
            "fileTextureName": fileTextureName,
            "uvTilingMode": int(uvTilingMode),
            "extra": dict(extra or {}),
        }

    def copy_state(self):
        out = {}
        for name, data in self.nodes.items():
            out[name] = {
                "fileTextureName": data["fileTextureName"],
                "uvTilingMode": data["uvTilingMode"],
                "extra": dict(data["extra"]),
            }
        return out

    def ls(self, type=None):
        if type not in (None, "file"):
            return []
        return list(self.nodes)

    def _split(self, plug):
        node, sep, attr = str(plug).rpartition(".")
        if not sep:
            raise RuntimeError("bad plug: {0}".format(plug))
        return node, attr

    def getAttr(self, plug):
        node, attr = self._split(plug)
        rec = self.nodes[node]
        if attr in ("fileTextureName", "uvTilingMode"):
            return rec[attr]
        if attr not in rec["extra"]:
            raise RuntimeError("{0} not found".format(plug))
        return rec["extra"][attr]

    def setAttr(self, plug, value, type=None):
        node, attr = self._split(plug)
        rec = self.nodes[node]
        if attr in ("fileTextureName", "uvTilingMode"):
            rec[attr] = value
            return
        if attr not in rec["extra"]:
            raise RuntimeError("{0} not found".format(plug))
        rec["extra"][attr] = value

    def addAttr(self, node, ln=None, dt=None, at=None, longName=None, dataType=None, attributeType=None):
        name = ln or longName
        kind = dt or dataType or at or attributeType
        if kind in ("string",):
            self.nodes[node]["extra"][name] = ""
        else:
            self.nodes[node]["extra"][name] = 0

    def deleteAttr(self, node_or_plug, at=None):
        if at is None:
            node, attr = self._split(node_or_plug)
        else:
            node, attr = node_or_plug, at
        self.nodes[node]["extra"].pop(attr, None)

    def attributeQuery(self, name, node=None, exists=False):
        rec = self.nodes[node]
        found = name in rec["extra"]
        return found if exists else found


class FakeProc:
    def __init__(self, code=None):
        self._code = code

    def poll(self):
        return self._code


class CapturedPopen:
    def __init__(self, code=None):
        self.calls = []
        self.code = code

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        return FakeProc(self.code)


def _smooth_png(path: Path, res: int = RES) -> None:
    yy, xx = np.meshgrid(
        np.linspace(0.0, 1.0, res, dtype=np.float32),
        np.linspace(0.0, 1.0, res, dtype=np.float32),
        indexing="ij",
    )
    img = np.stack([xx, yy, 0.5 * (xx + yy)], -1)
    u8 = np.clip(np.rint(img * 255.0), 0, 255).astype(np.uint8)
    path.write_bytes(fast_encode_png(u8, [0] * res))


def _collect_holder(arrays, info):
    state = {"arrays": arrays, "info": info}

    def collect(shape_paths, ts_names, uv_set=None):
        return state["arrays"], state["info"]

    collect.state = state
    return collect


def _yes_exists(_path):
    return True


def _make_runtime(
    tmp_path, *, enabled=True, cmds=None, popen=None, collect=None, mayapy=None,
    exists_fn=None,
):
    root = tmp_path / "uvremap" / "proj"
    root.mkdir(parents=True, exist_ok=True)
    if cmds is None:
        cmds = FakeCmds()
    if popen is None:
        popen = CapturedPopen()
    if collect is None:
        arrays, info = plane_arrays(nx=4, ny=4)
        collect = _collect_holder(arrays, info)
    if mayapy is None:
        fake_bin = tmp_path / "mayapy_bin"
        fake_bin.write_text("", encoding="utf-8")
        mayapy = str(fake_bin)
    now_box = {"value": FIXED_NOW}

    def now_fn():
        return now_box["value"]

    runtime = rt.UVRemapRuntime(
        get_enabled=lambda: enabled if not callable(enabled) else enabled(),
        get_root=lambda _key: root,
        now_fn=now_fn,
        popen_fn=popen,
        cmds_fn=lambda: cmds,
        mayapy_path=mayapy,
        collect_arrays_fn=collect,
        exists_fn=exists_fn,
    )
    runtime._now_box = now_box
    runtime._root_path = root
    runtime._cmds_obj = cmds
    return runtime


# ---------------------------------------------------------------------------
# import / 時計
# ---------------------------------------------------------------------------
def test_import_does_not_load_maya_or_pillow():
    before = {
        n for n in sys.modules
        if n == "maya" or n.startswith("maya.") or n == "PIL" or n.startswith("PIL.")
    }
    import importlib
    importlib.reload(rt)
    after = {
        n for n in sys.modules
        if n == "maya" or n.startswith("maya.") or n == "PIL" or n.startswith("PIL.")
    }
    assert after == before


def test_source_imports_maya_only_inside_functions():
    source = Path(rt.__file__).read_text(encoding="utf-8")
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


def test_datetime_now_only_in_default_clock():
    source = Path(rt.__file__).read_text(encoding="utf-8")
    assert source.count("datetime.now") == 1
    assert "def _naive_now_iso" in source


# ---------------------------------------------------------------------------
# 1. 基準が無いと BLOCKED。record_baseline は上書きしない
# ---------------------------------------------------------------------------
def test_convert_now_blocked_without_baseline(tmp_path):
    popen = CapturedPopen()
    runtime = _make_runtime(tmp_path, popen=popen)
    job_id = runtime.convert_now(
        "proj", TS, ["mesh"], tmp_path / "src", tmp_path / "out",
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
    )
    assert job_id == ""
    assert runtime.running_job_ids() == []
    assert popen.calls == []
    status = json.loads(runtime.status_snapshot("proj").read_text(encoding="utf-8"))
    assert status["ts"][TS]["state"] == rt.STATE_BLOCKED
    assert status["ts"][TS]["message"] == rt.MSG_NO_BASELINE
    assert status["ts"][TS]["updated"] == FIXED_NOW
    jobs = runtime._root_path / "jobs"
    assert not jobs.exists() or list(jobs.iterdir()) == []


def test_record_baseline_does_not_overwrite(tmp_path):
    arrays, info = plane_arrays(nx=4, ny=4)
    collect = _collect_holder(arrays, info)
    runtime = _make_runtime(tmp_path, collect=collect)
    first = runtime.record_baseline("proj", [TS], ["mesh"])
    mesh = runtime._root_path / "baseline" / first / "mesh.npz"
    meta = runtime._root_path / "baseline" / first / "meta.json"
    assert mesh.is_file() and meta.is_file()
    stamp = mesh.stat().st_mtime_ns
    text = meta.read_text(encoding="utf-8")
    shifted, shifted_info = plane_arrays(nx=4, ny=4, uv_shift=(0.3, 0.1))
    collect.state["arrays"] = shifted
    collect.state["info"] = shifted_info
    second = runtime.record_baseline("proj", [TS], ["mesh"])
    assert second == first
    assert mesh.stat().st_mtime_ns == stamp
    assert meta.read_text(encoding="utf-8") == text
    assert len(rt._list_baseline_ids(runtime._root_path)) == 1

    third = runtime.record_baseline("proj", [TS], ["mesh"], force=True)
    assert third != first
    ids = rt._list_baseline_ids(runtime._root_path)
    assert first in ids and third in ids
    assert (runtime._root_path / "baseline" / first / "mesh.npz").is_file()
    assert (runtime._root_path / "baseline" / third / "mesh.npz").is_file()


# ---------------------------------------------------------------------------
# 2. ファイルノード切替の往復・他TS・属性なし・UDIM
# ---------------------------------------------------------------------------
def _body_nodes():
    return {
        "file_body": {
            "fileTextureName": "/src/Final/Body_BaseColor.<UDIM>.png",
            "uvTilingMode": rt.UV_TILING_UDIM,
        },
        "file_body_single": {
            "fileTextureName": "/src/Final/Body_Roughness.png",
            "uvTilingMode": rt.UV_TILING_OFF,
        },
        "file_trim": {
            "fileTextureName": "/src/Final/Trim_BaseColor.png",
            "uvTilingMode": rt.UV_TILING_OFF,
        },
        "file_plain": {
            "fileTextureName": "/other/unrelated.png",
            "uvTilingMode": rt.UV_TILING_OFF,
        },
    }


def test_switch_and_revert_roundtrip_leaves_other_nodes(tmp_path):
    cmds = FakeCmds(_body_nodes())
    before = cmds.copy_state()
    files = [
        {"name": "Body_BaseColor.png", "tile": 1001, "sha1": "a"},
        {"name": "Body_Roughness.1001.png", "tile": 1001, "sha1": "b"},
        {"name": "Body_Roughness.1002.png", "tile": 1002, "sha1": "c"},
    ]
    out = tmp_path / "output" / "final"
    switched = rt.switch_ts_file_nodes(cmds, TS, files, out, exists_fn=_yes_exists)
    assert set(switched) == {"file_body", "file_body_single"}

    body = cmds.nodes["file_body"]
    assert body["fileTextureName"] == str(out / "Body_BaseColor.png")
    assert body["uvTilingMode"] == rt.UV_TILING_OFF
    assert body["extra"][rt.ATTR_ORIGINAL_PATH] == before["file_body"]["fileTextureName"]
    assert body["extra"][rt.ATTR_ORIGINAL_TILING] == rt.UV_TILING_UDIM
    assert body["extra"][rt.ATTR_TS_NAME] == TS

    single = cmds.nodes["file_body_single"]
    assert single["fileTextureName"] == str(out / "Body_Roughness.<UDIM>.png")
    assert single["uvTilingMode"] == rt.UV_TILING_UDIM
    assert single["extra"][rt.ATTR_ORIGINAL_PATH] == before["file_body_single"]["fileTextureName"]
    assert single["extra"][rt.ATTR_ORIGINAL_TILING] == rt.UV_TILING_OFF

    assert cmds.nodes["file_trim"] == before["file_trim"]
    assert cmds.nodes["file_plain"] == before["file_plain"]
    assert rt.ATTR_ORIGINAL_PATH not in cmds.nodes["file_trim"]["extra"]
    assert rt.ATTR_ORIGINAL_PATH not in cmds.nodes["file_plain"]["extra"]

    reverted = rt.revert_ts_file_nodes(cmds, ts_name=TS)
    assert set(reverted) == {"file_body", "file_body_single"}
    assert cmds.copy_state() == before


def test_udim_multi_to_single_and_single_to_multi(tmp_path):
    out = tmp_path / "out"
    cmds = FakeCmds({
        "file_udim": {
            "fileTextureName": "/src/Body_BaseColor.<UDIM>.png",
            "uvTilingMode": rt.UV_TILING_UDIM,
        },
    })
    original = cmds.copy_state()
    rt.switch_ts_file_nodes(
        cmds, TS,
        [{"name": "Body_BaseColor.png", "tile": 1001, "sha1": "x"}],
        out, exists_fn=_yes_exists,
    )
    assert cmds.nodes["file_udim"]["fileTextureName"] == str(out / "Body_BaseColor.png")
    assert cmds.nodes["file_udim"]["uvTilingMode"] == rt.UV_TILING_OFF
    rt.revert_ts_file_nodes(cmds, ts_name=TS)
    assert cmds.copy_state() == original

    cmds = FakeCmds({
        "file_one": {
            "fileTextureName": "/src/Body_BaseColor.png",
            "uvTilingMode": rt.UV_TILING_OFF,
        },
    })
    original = cmds.copy_state()
    rt.switch_ts_file_nodes(
        cmds, TS,
        [
            {"name": "Body_BaseColor.1001.png", "tile": 1001, "sha1": "x"},
            {"name": "Body_BaseColor.1002.png", "tile": 1002, "sha1": "y"},
        ],
        out, exists_fn=_yes_exists,
    )
    assert cmds.nodes["file_one"]["fileTextureName"] == str(out / "Body_BaseColor.<UDIM>.png")
    assert cmds.nodes["file_one"]["uvTilingMode"] == rt.UV_TILING_UDIM
    rt.revert_ts_file_nodes(cmds, ts_name=TS)
    assert cmds.copy_state() == original


def test_revert_skips_nodes_without_attributes(tmp_path):
    cmds = FakeCmds({
        "file_body": {
            "fileTextureName": "/src/Body_BaseColor.png",
            "uvTilingMode": 0,
        },
    })
    before = cmds.copy_state()
    assert rt.revert_ts_file_nodes(cmds, ts_name=TS) == []
    assert cmds.copy_state() == before


# ---------------------------------------------------------------------------
# 3. 偽 Popen: 引数・環境変数・worker.log
# ---------------------------------------------------------------------------
def test_popen_args_env_and_worker_log(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/should/not/inherit/home")
    monkeypatch.setenv("PYTHONPATH", "/should/not/inherit/path")
    monkeypatch.setenv("PYTHONSTARTUP", "/should/not/inherit/startup")
    popen = CapturedPopen()
    arrays, info = plane_arrays(nx=4, ny=4)
    collect = _collect_holder(arrays, info)
    mayapy = tmp_path / "mayapy_stub"
    mayapy.write_text("", encoding="utf-8")
    runtime = _make_runtime(tmp_path, popen=popen, collect=collect, mayapy=str(mayapy))
    runtime.record_baseline("proj", [TS], ["mesh"])
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    job_id = runtime.convert_now(
        "proj", TS, ["mesh"], src, out, quality="final",
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
        udim=False, out_res=RES, timeout_s=120, copy_retries=1,
    )
    assert job_id
    assert len(popen.calls) == 1
    args, kwargs = popen.calls[0]
    assert args[0] == str(mayapy)
    assert args[1] == str(WORKER_PY)
    assert args[2] == "run"
    assert args[3].endswith("job.json")
    job_path = Path(args[3])
    assert job_path.is_file()
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["job_id"] == job_id
    assert job["ts_name"] == TS
    env = kwargs["env"]
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert "PYTHONSTARTUP" not in env
    assert env.get("PYTHONNOUSERSITE") == "1"
    log_path = runtime._root_path / "jobs" / job_id / "worker.log"
    assert log_path.is_file()
    stdout = kwargs["stdout"]
    assert Path(stdout.name) == log_path
    assert kwargs["stderr"] == subprocess.STDOUT
    if os.name == "nt":
        assert kwargs.get("creationflags") == rt.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in kwargs


# ---------------------------------------------------------------------------
# D-3(偽プロセス)とキルスイッチ
# ---------------------------------------------------------------------------
def test_d3_reverts_when_last_good_hash_differs(tmp_path):
    cmds = FakeCmds(_body_nodes())
    arrays, info = plane_arrays(nx=4, ny=4)
    collect = _collect_holder(arrays, info)
    runtime = _make_runtime(tmp_path, cmds=cmds, collect=collect)
    runtime.record_baseline("proj", [TS], ["mesh"])
    out = tmp_path / "out"
    rt.switch_ts_file_nodes(
        cmds, TS, [{"name": "Body_BaseColor.png", "tile": 1001, "sha1": "a"}], out,
        exists_fn=_yes_exists,
    )
    assert cmds.nodes["file_body"]["extra"][rt.ATTR_ORIGINAL_PATH]
    ledger = {
        "schema": 1,
        "ts": {TS: {"generation": 1, "last_good_generation": 1, "uv_hash": "not-the-current-hash"}},
    }
    (runtime._root_path / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

    def popen_fail(args, **kwargs):
        job_path = Path(args[3])
        result = {
            "schema": 1, "job_id": job_path.parent.name, "generation": 2,
            "status": "blocked", "exit_code": 11, "ts_name": TS, "files": [],
            "messages": [{"level": "error", "text": "topology_changed"}],
        }
        (job_path.parent / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return FakeProc(11)

    runtime.popen_fn = popen_fail
    collect.state["arrays"], collect.state["info"] = plane_arrays(nx=4, ny=4, uv_shift=(0.2, 0.1))
    job_id = runtime.convert_now(
        "proj", TS, ["mesh"], tmp_path / "src", out,
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
        udim=False, out_res=RES, timeout_s=30,
    )
    assert job_id
    events = runtime.poll()
    assert len(events) == 1
    assert events[0]["exit_code"] == 11
    assert events[0]["reverted"]
    assert rt.ATTR_ORIGINAL_PATH not in cmds.nodes["file_body"]["extra"]
    assert cmds.nodes["file_body"]["fileTextureName"] == "/src/Final/Body_BaseColor.<UDIM>.png"
    assert cmds.nodes["file_trim"]["fileTextureName"] == "/src/Final/Trim_BaseColor.png"


def test_d3_keeps_converted_when_hash_matches(tmp_path):
    cmds = FakeCmds(_body_nodes())
    arrays, info = plane_arrays(nx=4, ny=4, uv_shift=(0.12, -0.08))
    collect = _collect_holder(arrays, info)
    runtime = _make_runtime(tmp_path, cmds=cmds, collect=collect)
    bid = runtime.record_baseline("proj", [TS], ["mesh"])
    import uvremap_bridge as br
    snap = br.arrays_to_snapshot(arrays, info, baseline_id=bid, created=FIXED_NOW)
    uv_hash = br.current_uv_hash(snap, TS)
    out = tmp_path / "out"
    rt.switch_ts_file_nodes(
        cmds, TS, [{"name": "Body_BaseColor.png", "tile": 1001, "sha1": "a"}], out,
        exists_fn=_yes_exists,
    )
    converted = cmds.nodes["file_body"]["fileTextureName"]
    ledger = {
        "schema": 1,
        "ts": {TS: {"generation": 1, "last_good_generation": 1, "uv_hash": uv_hash}},
    }
    (runtime._root_path / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

    def popen_fail(args, **kwargs):
        job_path = Path(args[3])
        result = {
            "schema": 1, "job_id": job_path.parent.name, "generation": 2,
            "status": "blocked", "exit_code": 11, "ts_name": TS, "files": [],
            "messages": [{"level": "error", "text": "topology_changed"}],
        }
        (job_path.parent / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return FakeProc(11)

    runtime.popen_fn = popen_fail
    job_id = runtime.convert_now(
        "proj", TS, ["mesh"], tmp_path / "src", out,
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
        udim=False, out_res=RES, timeout_s=30,
    )
    assert job_id
    events = runtime.poll()
    assert events[0]["exit_code"] == 11
    assert events[0]["reverted"] == []
    assert cmds.nodes["file_body"]["fileTextureName"] == converted
    assert rt.ATTR_ORIGINAL_PATH in cmds.nodes["file_body"]["extra"]


def test_kill_switch_reverts_and_does_not_create_job(tmp_path):
    cmds = FakeCmds(_body_nodes())
    arrays, info = plane_arrays(nx=4, ny=4)
    collect = _collect_holder(arrays, info)
    flag = {"on": True}

    def get_enabled():
        return flag["on"]

    popen = CapturedPopen()
    runtime = _make_runtime(
        tmp_path, enabled=get_enabled, cmds=cmds, collect=collect, popen=popen,
    )
    runtime.record_baseline("proj", [TS], ["mesh"])
    out = tmp_path / "out"
    rt.switch_ts_file_nodes(
        cmds, TS, [{"name": "Body_BaseColor.png", "tile": 1001, "sha1": "a"}], out,
        exists_fn=_yes_exists,
    )
    assert rt.ATTR_ORIGINAL_PATH in cmds.nodes["file_body"]["extra"]
    flag["on"] = False
    job_id = runtime.convert_now(
        "proj", TS, ["mesh"], tmp_path / "src", out,
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
    )
    assert job_id == ""
    assert popen.calls == []
    jobs = runtime._root_path / "jobs"
    assert not jobs.exists() or list(jobs.iterdir()) == []
    status = json.loads(runtime.status_snapshot("proj").read_text(encoding="utf-8"))
    assert status["ts"][TS]["state"] == rt.STATE_ORIGINAL
    assert status["ts"][TS]["updated"] == FIXED_NOW
    assert cmds.nodes["file_body"]["fileTextureName"] == "/src/Final/Body_BaseColor.<UDIM>.png"
    assert rt.ATTR_ORIGINAL_PATH not in cmds.nodes["file_body"]["extra"]
    assert cmds.nodes["file_trim"]["fileTextureName"] == "/src/Final/Trim_BaseColor.png"


def test_status_snapshot_path(tmp_path):
    runtime = _make_runtime(tmp_path)
    path = runtime.status_snapshot("proj")
    assert path == runtime._root_path / "status.json"


# ---------------------------------------------------------------------------
# 4. 実ワーカー一周
# ---------------------------------------------------------------------------
def _wait_events(runtime, timeout=45.0):
    deadline = time.monotonic() + timeout
    events = []
    while time.monotonic() < deadline:
        events.extend(runtime.poll())
        if not runtime.running_job_ids():
            return events
        time.sleep(0.05)
    raise AssertionError("worker did not finish in time")


def _real_popen(args, **kwargs):
    return subprocess.Popen(args, **kwargs)


def test_real_worker_switch_then_topology_fail_reverts(tmp_path):
    src = tmp_path / "painter" / "Final"
    src.mkdir(parents=True)
    _smooth_png(src / "{0}_BaseColor.png".format(TS), RES)
    (src / "_sync_complete.flag").write_text("{}", encoding="utf-8")
    out = tmp_path / "uvremap" / "proj" / "output" / "final"
    cmds = FakeCmds({
        "file_body": {
            "fileTextureName": str(src / "Body_BaseColor.png"),
            "uvTilingMode": rt.UV_TILING_OFF,
        },
        "file_trim": {
            "fileTextureName": str(src / "Trim_BaseColor.png"),
            "uvTilingMode": rt.UV_TILING_OFF,
        },
    })
    original_body = cmds.nodes["file_body"]["fileTextureName"]
    base_arr, base_info = plane_arrays(nx=4, ny=4)
    collect = _collect_holder(base_arr, base_info)
    runtime = _make_runtime(
        tmp_path, cmds=cmds, collect=collect,
        popen=_real_popen, mayapy=sys.executable,
    )
    runtime.record_baseline("proj", [TS], ["mesh"])

    shifted, shifted_info = plane_arrays(nx=4, ny=4, uv_shift=(0.12, -0.08))
    collect.state["arrays"] = shifted
    collect.state["info"] = shifted_info
    job_ok = runtime.convert_now(
        "proj", TS, ["mesh"], src, out, quality="final",
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
        udim=False, out_res=RES, timeout_s=120, copy_retries=1, lock_timeout_s=15,
    )
    assert job_ok
    events = _wait_events(runtime)
    assert events and events[0]["exit_code"] == 0, events
    assert (out / "Body_BaseColor.png").is_file()
    assert cmds.nodes["file_body"]["fileTextureName"] == str(out / "Body_BaseColor.png")
    assert cmds.nodes["file_body"]["uvTilingMode"] == rt.UV_TILING_OFF
    assert cmds.nodes["file_body"]["extra"][rt.ATTR_ORIGINAL_PATH] == original_body
    assert cmds.nodes["file_trim"]["fileTextureName"].endswith("Trim_BaseColor.png")

    broken, broken_info = plane_arrays(nx=4, ny=4, uv_shift=(0.40, 0.10))
    broken["corner_vid"] = broken["corner_vid"].copy()
    broken["corner_vid"][0] = int(broken["corner_vid"].max()) + 9
    collect.state["arrays"] = broken
    collect.state["info"] = broken_info
    job_fail = runtime.convert_now(
        "proj", TS, ["mesh"], src, out, quality="final",
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
        udim=False, out_res=RES, timeout_s=120, copy_retries=1, lock_timeout_s=15,
    )
    assert job_fail
    events = _wait_events(runtime)
    assert events and events[0]["exit_code"] == 11, events
    assert events[0]["reverted"]
    assert cmds.nodes["file_body"]["fileTextureName"] == original_body
    assert rt.ATTR_ORIGINAL_PATH not in cmds.nodes["file_body"]["extra"]
    assert rt.ATTR_TS_NAME not in cmds.nodes["file_body"]["extra"]
    assert cmds.nodes["file_trim"]["fileTextureName"].endswith("Trim_BaseColor.png")


# ---------------------------------------------------------------------------
# D2c: 世代・空切替・欠損ファイル・TS名一致・CONVERTING の順序
# ---------------------------------------------------------------------------
def _popen_ok_with_files(files):
    def popen_ok(args, **kwargs):
        job_path = Path(args[3])
        job = json.loads(job_path.read_text(encoding="utf-8"))
        result = {
            "schema": 1,
            "job_id": job_path.parent.name,
            "generation": job.get("generation", 1),
            "status": "ok",
            "exit_code": 0,
            "ts_name": job.get("ts_name", TS),
            "files": files,
            "messages": [],
        }
        (job_path.parent / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return FakeProc(0)

    return popen_ok


def test_second_job_generation_advances_and_writes_latest(tmp_path):
    popen = CapturedPopen(code=None)
    runtime = _make_runtime(tmp_path, popen=popen)
    runtime.record_baseline("proj", [TS], ["mesh"])
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    channels = [{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}]
    job1 = runtime.convert_now(
        "proj", TS, ["mesh"], src, out, channels=channels, udim=False, timeout_s=30,
    )
    job2 = runtime.convert_now(
        "proj", TS, ["mesh"], src, out, channels=channels, udim=False, timeout_s=30,
    )
    assert job1 and job2 and job1 != job2
    rec1 = runtime._jobs[job1]
    rec2 = runtime._jobs[job2]
    assert rec2["generation"] > rec1["generation"]
    latest = rt.latest_generation_file(runtime._root_path, TS)
    assert latest.is_file()
    assert latest.parent.name == rt.LATEST_GENERATION_DIR
    assert latest.name == rt.latest_generation_filename(TS)
    data = json.loads(latest.read_text(encoding="utf-8"))
    assert data["generation"] == rec2["generation"]
    assert data["ts_name"] == TS
    j1 = json.loads((runtime._root_path / "jobs" / job1 / "job.json").read_text(encoding="utf-8"))
    j2 = json.loads((runtime._root_path / "jobs" / job2 / "job.json").read_text(encoding="utf-8"))
    assert j1["latest_generation_path"] == str(latest)
    assert j2["latest_generation_path"] == str(latest)
    assert j1["generation"] == rec1["generation"]
    assert j2["generation"] == rec2["generation"]


def test_latest_generation_filename_sanitizes_without_collision():
    assert rt.latest_generation_filename("Body") == "Body.json"
    assert rt.latest_generation_filename("mat_2") == "mat_2.json"
    slash = rt.latest_generation_filename("mat/2")
    assert slash != "mat_2.json"
    assert slash.endswith(".json")
    assert "/" not in slash


def test_poll_original_when_no_file_nodes_switched(tmp_path):
    cmds = FakeCmds({
        "file_other": {"fileTextureName": "/src/Other_BaseColor.png", "uvTilingMode": 0},
    })
    files = [{"name": "Body_BaseColor.png", "tile": 1001, "sha1": "a"}]
    runtime = _make_runtime(
        tmp_path, cmds=cmds, popen=_popen_ok_with_files(files), exists_fn=_yes_exists,
    )
    runtime.record_baseline("proj", [TS], ["mesh"])
    job_id = runtime.convert_now(
        "proj", TS, ["mesh"], tmp_path / "src", tmp_path / "out",
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
        udim=False, timeout_s=30,
    )
    assert job_id
    events = runtime.poll()
    assert len(events) == 1
    assert events[0]["switched"] == []
    assert events[0]["warning"] == rt.WARN_NO_FILE_NODES
    assert events[0]["state"] == rt.STATE_ORIGINAL
    assert events[0]["message"] == rt.MSG_NO_FILE_NODES
    assert events[0]["updated"] == FIXED_NOW
    status = json.loads(runtime.status_snapshot("proj").read_text(encoding="utf-8"))
    assert status["ts"][TS]["state"] == rt.STATE_ORIGINAL
    assert status["ts"][TS]["message"] == rt.MSG_NO_FILE_NODES
    assert status["ts"][TS]["updated"] == FIXED_NOW
    assert cmds.nodes["file_other"]["fileTextureName"] == "/src/Other_BaseColor.png"


def test_missing_converted_file_skips_that_channel_only(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "Body_BaseColor.png").write_bytes(b"x")
    files = [
        {"name": "Body_BaseColor.png", "tile": 1001, "sha1": "a"},
        {"name": "Body_Roughness.png", "tile": 1001, "sha1": "b"},
    ]
    cmds = FakeCmds({
        "file_bc": {"fileTextureName": "/src/Body_BaseColor.png", "uvTilingMode": 0},
        "file_rg": {"fileTextureName": "/src/Body_Roughness.png", "uvTilingMode": 0},
    })
    missing = []
    switched = rt.switch_ts_file_nodes(cmds, TS, files, out, missing_out=missing)
    assert switched == ["file_bc"]
    assert cmds.nodes["file_bc"]["fileTextureName"] == str(out / "Body_BaseColor.png")
    assert cmds.nodes["file_rg"]["fileTextureName"] == "/src/Body_Roughness.png"
    assert {"suffix": "Roughness", "name": "Body_Roughness.png"} in missing

    poll_cmds = FakeCmds({
        "file_bc": {"fileTextureName": "/src/Body_BaseColor.png", "uvTilingMode": 0},
        "file_rg": {"fileTextureName": "/src/Body_Roughness.png", "uvTilingMode": 0},
    })
    runtime = _make_runtime(
        tmp_path, cmds=poll_cmds, popen=_popen_ok_with_files(files),
    )
    runtime.record_baseline("proj", [TS], ["mesh"])
    job_id = runtime.convert_now(
        "proj", TS, ["mesh"], tmp_path / "src", out,
        channels=[
            {"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"},
            {"suffix": "Roughness", "colorspace": "raw", "kind": "scalar"},
        ],
        udim=False, timeout_s=30,
    )
    assert job_id
    events = runtime.poll()
    assert events[0]["warning"] == rt.WARN_FILE_MISSING
    assert events[0]["switched"] == ["file_bc"]
    assert {"suffix": "Roughness", "name": "Body_Roughness.png"} in events[0]["missing"]
    assert poll_cmds.nodes["file_bc"]["fileTextureName"] == str(out / "Body_BaseColor.png")
    assert poll_cmds.nodes["file_rg"]["fileTextureName"] == "/src/Body_Roughness.png"


def test_udim_missing_tile_skips_channel(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "Body_BaseColor.1001.png").write_bytes(b"x")
    cmds = FakeCmds({
        "file_udim": {
            "fileTextureName": "/src/Body_BaseColor.<UDIM>.png",
            "uvTilingMode": rt.UV_TILING_UDIM,
        },
    })
    files = [
        {"name": "Body_BaseColor.1001.png", "tile": 1001, "sha1": "a"},
        {"name": "Body_BaseColor.1002.png", "tile": 1002, "sha1": "b"},
    ]
    missing = []
    switched = rt.switch_ts_file_nodes(cmds, TS, files, out, missing_out=missing)
    assert switched == []
    assert cmds.nodes["file_udim"]["fileTextureName"] == "/src/Body_BaseColor.<UDIM>.png"
    assert any(item["name"] == "Body_BaseColor.1002.png" for item in missing)


def test_revert_mat_does_not_revert_mat_2(tmp_path):
    out = tmp_path / "out"
    cmds = FakeCmds({
        "file_mat": {"fileTextureName": "/src/mat_BaseColor.png", "uvTilingMode": 0},
        "file_mat2": {"fileTextureName": "/src/mat_2_BaseColor.png", "uvTilingMode": 0},
    })
    rt.switch_ts_file_nodes(
        cmds, "mat", [{"name": "mat_BaseColor.png", "tile": 1001, "sha1": "a"}],
        out, exists_fn=_yes_exists,
    )
    rt.switch_ts_file_nodes(
        cmds, "mat_2", [{"name": "mat_2_BaseColor.png", "tile": 1001, "sha1": "b"}],
        out, exists_fn=_yes_exists,
    )
    assert cmds.nodes["file_mat"]["extra"][rt.ATTR_TS_NAME] == "mat"
    assert cmds.nodes["file_mat2"]["extra"][rt.ATTR_TS_NAME] == "mat_2"
    mat2_path = cmds.nodes["file_mat2"]["fileTextureName"]
    reverted = rt.revert_ts_file_nodes(cmds, ts_name="mat")
    assert reverted == ["file_mat"]
    assert cmds.nodes["file_mat"]["fileTextureName"] == "/src/mat_BaseColor.png"
    assert rt.ATTR_TS_NAME not in cmds.nodes["file_mat"]["extra"]
    assert rt.ATTR_ORIGINAL_PATH not in cmds.nodes["file_mat"]["extra"]
    assert cmds.nodes["file_mat2"]["fileTextureName"] == mat2_path
    assert cmds.nodes["file_mat2"]["extra"][rt.ATTR_TS_NAME] == "mat_2"


def test_revert_legacy_prefix_without_ts_attr():
    cmds = FakeCmds({
        "file_old": {
            "fileTextureName": "/out/mat_BaseColor.png",
            "uvTilingMode": 0,
            "extra": {
                rt.ATTR_ORIGINAL_PATH: "/src/mat_BaseColor.png",
                rt.ATTR_ORIGINAL_TILING: 0,
            },
        },
        "file_old2": {
            "fileTextureName": "/out/mat_2_BaseColor.png",
            "uvTilingMode": 0,
            "extra": {
                rt.ATTR_ORIGINAL_PATH: "/src/mat_2_BaseColor.png",
                rt.ATTR_ORIGINAL_TILING: 0,
            },
        },
        "file_plain": {
            "fileTextureName": "/out/Trim_BaseColor.png",
            "uvTilingMode": 0,
            "extra": {
                rt.ATTR_ORIGINAL_PATH: "/src/Trim_BaseColor.png",
                rt.ATTR_ORIGINAL_TILING: 0,
            },
        },
    })
    reverted = rt.revert_ts_file_nodes(cmds, ts_name="mat")
    assert "file_old" in reverted
    assert "file_old2" in reverted
    assert "file_plain" not in reverted
    assert cmds.nodes["file_old"]["fileTextureName"] == "/src/mat_BaseColor.png"
    assert cmds.nodes["file_old2"]["fileTextureName"] == "/src/mat_2_BaseColor.png"
    assert cmds.nodes["file_plain"]["fileTextureName"] == "/out/Trim_BaseColor.png"


def test_converting_written_before_popen(tmp_path):
    seen = {}
    box = {}

    def popen_fn(args, **kwargs):
        path = box["rt"]._root_path / "status.json"
        seen["status"] = json.loads(path.read_text(encoding="utf-8"))
        return FakeProc(None)

    runtime = _make_runtime(tmp_path, popen=popen_fn)
    box["rt"] = runtime
    runtime.record_baseline("proj", [TS], ["mesh"])
    job_id = runtime.convert_now(
        "proj", TS, ["mesh"], tmp_path / "src", tmp_path / "out",
        channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
        udim=False, timeout_s=30,
    )
    assert job_id
    assert seen["status"]["ts"][TS]["state"] == rt.STATE_CONVERTING
    assert seen["status"]["ts"][TS]["updated"] == FIXED_NOW
    assert seen["status"]["ts"][TS]["generation"] == 1
