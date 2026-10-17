# -*- coding: utf-8 -*-
"""uvremap_worker / uvremap_io のテスト(契約 CONTRACT.md §4・§5、不変条件 I1〜I8)。

実行(Maya 不要。標準ライブラリ + numpy。Pillow は不要で、入っていれば比較テストが追加で走る):

    python -m pytest sp_maya_livesync/tests/test_uvremap_worker.py

- core(``uvremap_core``)は並行実装中のため、``stub_uvremap_core.py``(契約 §3 のシグネチャだけを
  持つテスト専用スタブ)を ``sys.modules["uvremap_core"]`` に差し込んで検証する。
- すべて一時フォルダ上で動き、実在のユーザーフォルダや Painter/Maya には触れない。
- 外部通信は行わない(サブプロセスは自分自身のテスト用ランナー / ロック保持役のみ)。

不変条件とテストの対応は各セクション見出し(``# I1`` など)を参照。
"""
from __future__ import annotations

import ast
import builtins
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stub_uvremap_core as stub  # noqa: E402
from png_testkit import make_png_bytes  # noqa: E402
import uvremap_io  # noqa: E402
import uvremap_worker as W  # noqa: E402

T0 = 1_600_000_000_000_000_000  # 原本ファイルの固定 mtime(ns)。書き換えがあれば変わる
IS_NT = os.name == "nt"


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------
def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_path(path) -> str:
    return sha1_bytes(Path(path).read_bytes())


def tree_state(root, *, dirs: bool = True, mtimes: bool = True):
    """フォルダ配下の {相対パス: (sha1, mtime_ns)} 。存在しなければ None。"""
    root = Path(root)
    if not root.exists():
        return None
    state = {}
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rel_d = d.relative_to(root).as_posix() + "/"
        if dirs:
            state[rel_d] = ("dir", d.stat().st_mtime_ns if mtimes else 0)
        for name in filenames:
            p = d / name
            state[(d / name).relative_to(root).as_posix()] = (
                sha1_path(p), p.stat().st_mtime_ns if mtimes else 0)
    return state


def make_array(h: int, w: int, c: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    q = rng.integers(0, 256, size=(h, w, c), dtype=np.uint8)
    return (q.astype(np.float32) / 255.0)


MODE_FOR_C = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}


class Env:
    """1 件の uvremap プロジェクト(原本フォルダ + uvremap_root)を一時フォルダ上に作る。"""

    TS = "Body"

    def __init__(self, base: Path):
        self.base = base
        self.src = base / "painter" / "Final"
        self.root = base / "uvremap" / "proj"
        self.out = self.root / "output" / "final"
        self.flag = self.src / "_sync_complete.flag"
        self.baseline = self.root / "baseline" / "bl1"
        self.src.mkdir(parents=True)
        self.baseline.mkdir(parents=True)
        (self.baseline / "meta.json").write_text(json.dumps({
            "schema": 1, "baseline_id": "bl1", "ts_names": [self.TS], "uv_hash": "base"}),
            encoding="utf-8")
        self.flag.write_text("1.0", encoding="utf-8")
        self._files: list[Path] = [self.flag]

    # ---- 入力
    def add_png(self, name: str, *, size: int = 32, c: int = 3, seed: int = 1, bit_depth: int = 8):
        p = self.src / name
        arr = make_array(size, size, c, seed)
        meta = {"bit_depth": bit_depth, "mode": uvremap_io.mode_for_channels(bit_depth, c), "ext": ".png"}
        uvremap_io.write_image(p, arr, meta)
        self._files.append(p)
        return p

    def add_raw(self, name: str, data: bytes = b"not an image"):
        p = self.src / name
        p.write_bytes(data)
        self._files.append(p)
        return p

    def standard_inputs(self):
        """BaseColor(1001,1002)・Roughness(1001)・Normal(タイル無し)。他チャンネルは無し。"""
        self.add_png("Body_BaseColor.1001.png", c=3, seed=1)
        self.add_png("Body_BaseColor.1002.png", c=3, seed=2)
        self.add_png("Body_Roughness.1001.png", c=1, seed=3)
        self.add_png("Body_Normal.png", c=3, seed=4)

    def freeze_times(self):
        """原本側の mtime を固定する(以降に変化すれば書き込みがあったと分かる)。"""
        for p in self._files:
            if p.exists():
                os.utime(p, ns=(T0, T0))
        os.utime(self.src, ns=(T0, T0))
        return self

    # ---- ジョブ
    def job_dict(self, job_id: str, generation: int, **over) -> dict:
        jd = self.root / "jobs" / job_id
        cur = jd / "current"
        cur.mkdir(parents=True, exist_ok=True)
        (cur / "meta.json").write_text(json.dumps({
            "schema": 1, "uv_hash": "uv-{0}".format(generation), "ts_names": [self.TS]}), encoding="utf-8")
        job = {
            "schema": 1, "job_id": job_id, "generation": generation,
            "ts_name": self.TS, "quality": "final",
            "baseline_dir": str(self.baseline), "current_snapshot_dir": str(cur),
            "source_dir": str(self.src), "source_flag": str(self.flag),
            "output_dir": str(self.out), "uvremap_root": str(self.root),
            "udim": True, "out_res": None, "dilate_px": 16, "normal_mode": "uv",
            "channels": [
                {"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"},
                {"suffix": "Roughness", "colorspace": "raw", "kind": "scalar"},
                {"suffix": "Metallic", "colorspace": "raw", "kind": "scalar"},
                {"suffix": "Normal", "colorspace": "raw", "kind": "normal"},
                {"suffix": "Height", "colorspace": "raw", "kind": "scalar"},
                {"suffix": "Emissive", "colorspace": "srgb", "kind": "color"},
            ],
            "timeout_s": 60, "copy_retries": 3, "lock_timeout_s": 5,
        }
        job.update(over)
        return job

    def write_job(self, job_id: str = "j1", generation: int = 1, *, where=None, **over) -> Path:
        job = self.job_dict(job_id, generation, **over)
        dirp = Path(where) if where else self.root / "jobs" / job_id
        dirp.mkdir(parents=True, exist_ok=True)
        path = dirp / "job.json"
        path.write_text(json.dumps(job), encoding="utf-8")
        return path

    def run(self, job_id: str = "j1", generation: int = 1, **over):
        p = self.write_job(job_id, generation, **over)
        code = W.run_job(p)
        return code, self.result(p)

    @staticmethod
    def result(job_path: Path) -> dict:
        return json.loads((Path(job_path).parent / "result.json").read_text(encoding="utf-8"))

    # ---- 状態の読み取り
    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.json"

    def ledger(self) -> dict:
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))

    def ledger_bytes(self):
        return self.ledger_path.read_bytes() if self.ledger_path.exists() else None

    def status(self) -> dict:
        return json.loads((self.root / "status.json").read_text(encoding="utf-8"))["ts"]

    def out_files(self):
        return sorted(p.name for p in self.out.iterdir()) if self.out.exists() else []

    def out_state(self):
        return tree_state(self.out, dirs=False, mtimes=False)


@pytest.fixture
def core(monkeypatch):
    """契約どおりのシグネチャを持つスタブ core を ``uvremap_core`` として差し込む。"""
    monkeypatch.setitem(sys.modules, "uvremap_core", stub)
    monkeypatch.delenv("STUB_CORE_CONFIG", raising=False)
    stub.CONFIG.clear()
    stub.CONFIG.update({"new_tiles": [1001, 1002]})
    stub.CALLS.clear()
    yield stub
    stub.CONFIG.clear()
    stub.CALLS.clear()


@pytest.fixture
def env(tmp_path, core):
    e = Env(tmp_path)
    e.standard_inputs()
    e.freeze_times()
    return e


def messages_text(result: dict, level: str | None = None) -> str:
    return "\n".join(m["text"] for m in result["messages"] if level is None or m["level"] == level)


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------
def test_discovery_naming_and_missing_channels_skipped(env, core):
    # 無関係なファイル(別TS・別サフィックス・バックアップ)は拾わない
    for name in ("Head_BaseColor.1001.png", "Body_BaseColorX.1001.png", "Body_BaseColor.1001.png.bak"):
        env.add_raw(name)
    env.freeze_times()
    code, res = env.run()
    assert code == 0 and res["status"] == "ok" and res["exit_code"] == 0
    converts = [c for c in core.CALLS if c[0] == "convert"]
    assert [(c[1], c[3]) for c in converts] == [
        ("BaseColor", [1001, 1002]), ("Roughness", [1001]), ("Normal", [1001])]
    text = messages_text(res, "info")
    for missing in ("Metallic", "Height", "Emissive"):
        assert "チャンネル {0}".format(missing) in text and "スキップ" in text
    led = env.ledger()["ts"]["Body"]
    assert sorted(led["sources"]) == [
        "Body_BaseColor.1001.png", "Body_BaseColor.1002.png", "Body_Normal.png", "Body_Roughness.1001.png"]
    for name, sha in led["sources"].items():
        assert sha == sha1_path(env.src / name)  # 台帳の sources は原本の sha1


def test_single_tile_output_has_no_tile_suffix(env, core):
    core.CONFIG["new_tiles"] = [1001]
    code, res = env.run()
    assert code == 0
    assert env.out_files() == ["Body_BaseColor.png", "Body_Normal.png", "Body_Roughness.png",
                               "_sync_complete.flag"]
    assert sorted(f["name"] for f in res["files"]) == [
        "Body_BaseColor.png", "Body_Normal.png", "Body_Roughness.png"]


def test_multi_tile_output_has_tile_suffix(env, core):
    core.CONFIG["new_tiles"] = [1001, 1002]
    code, res = env.run()
    assert code == 0
    assert env.out_files() == [
        "Body_BaseColor.1001.png", "Body_BaseColor.1002.png", "Body_Normal.1001.png",
        "Body_Normal.1002.png", "Body_Roughness.1001.png", "Body_Roughness.1002.png",
        "_sync_complete.flag"]
    assert {f["tile"] for f in res["files"]} == {1001, 1002}


def test_output_name_follows_new_uv_tiles_not_input_tiles(env, core):
    """入力は2タイルでも、新UVが1タイルだけなら無印名。逆に入力1タイル→新UV2タイルなら付く。"""
    core.CONFIG["new_tiles"] = [1002]
    code, res = env.run()
    assert code == 0 and "Body_BaseColor.png" in env.out_files()
    only = next(f for f in res["files"] if f["name"] == "Body_BaseColor.png")
    assert only["tile"] == 1002  # 無印名でも tile 番号は result に残る


def test_result_json_schema_and_hashes(env, core):
    code, res = env.run()
    assert code == 0
    assert res["schema"] == 1 and res["job_id"] == "j1" and res["generation"] == 1
    assert res["ts_name"] == "Body" and res["method"] == "F"
    for key in ("files", "probe", "stats", "messages", "timings"):
        assert key in res
    assert set(res["timings"]) >= {"copy", "convert", "probe", "publish"}
    assert set(res["probe"]) == {"BaseColor", "Roughness", "Normal"}
    assert res["probe"]["BaseColor"]["status"] == "pass" and res["probe"]["BaseColor"]["n_valid"] == 480
    for f in res["files"]:
        assert f["sha1"] == sha1_path(env.out / f["name"])
    assert res["stats"]["BaseColor"]["suffix"] == "BaseColor"
    assert (env.root / "jobs" / "j1" / "result.json").exists()


def test_ledger_updated_after_success(env, core):
    code, res = env.run(generation=7)
    assert code == 0
    led = env.ledger()
    assert led["schema"] == 1
    e = led["ts"]["Body"]
    assert e["generation"] == 7 and e["last_good_generation"] == 7
    assert e["uv_hash"] == "uv-7" and e["baseline_id"] == "bl1" and e["method"] == "F"
    assert e["frozen"] is False
    assert e["outputs"] == {f["name"]: f["sha1"] for f in res["files"]}
    for name, sha in e["outputs"].items():
        assert sha == sha1_path(env.out / name)


def test_second_generation_replaces_outputs_and_ledger(env, core):
    assert env.run("j1", 1)[0] == 0
    first = env.out_state()
    core.CONFIG["salt"] = 0.25
    assert env.run("j2", 2)[0] == 0
    second = env.out_state()
    changed = [k for k in first if k != "_sync_complete.flag" and first[k] != second[k]]
    assert changed  # 出力が新世代に置き換わった
    e = env.ledger()["ts"]["Body"]
    assert e["generation"] == 2 and e["last_good_generation"] == 2 and e["uv_hash"] == "uv-2"
    flag = json.loads((env.out / "_sync_complete.flag").read_text(encoding="utf-8"))
    assert flag["generation"] == 2 and flag["uv_hash"] == "uv-2"
    assert flag["files"] == sorted(e["outputs"])
    assert not [n for n in env.out_files() if ".edited_" in n or n.endswith(W.TMP_SUFFIX)]


def test_rerun_same_generation_is_idempotent(env, core):
    assert env.run("j1", 3)[0] == 0
    assert env.run("j2", 3)[0] == 0  # 同世代の再実行は許可(古い世代ではない)
    assert not [n for n in env.out_files() if ".edited_" in n]


def test_tile_count_change_removes_obsolete_outputs(env, core):
    assert env.run("j1", 1)[0] == 0
    assert "Body_BaseColor.1002.png" in env.out_files()
    core.CONFIG["new_tiles"] = [1001]
    assert env.run("j2", 2)[0] == 0
    files = env.out_files()
    assert "Body_BaseColor.png" in files
    assert not [n for n in files if re.search(r"\.\d{4}\.png$", n)]  # 旧タイル付き出力は消える


def test_out_res_square_default_and_explicit(env, core):
    assert env.run("j1", 1)[0] == 0
    assert {c[2] for c in core.CALLS if c[0] == "convert"} == {32}  # 元タイルが正方形なら整数
    core.CALLS.clear()
    assert env.run("j2", 2, out_res={"1001": [64, 64], "1002": [32, 16]})[0] == 0
    assert [c for c in core.CALLS if c[0] == "convert"][-1][2] == {1001: (64, 64), 1002: (32, 16)}
    core.CALLS.clear()
    assert env.run("j3", 3, out_res=128, dilate_px=4, normal_mode="off")[0] == 0
    last = [c for c in core.CALLS if c[0] == "convert"][0]
    assert last[2] == 128 and last[4] == 4 and last[5] == "off"


def test_decision_f_flipped_accepted_and_warnings_reported(env, core):
    core.CONFIG.update(method="F_FLIPPED", decision_warnings=["bbox_mismatch"])
    code, res = env.run()
    assert code == 0 and res["method"] == "F_FLIPPED"
    assert env.ledger()["ts"]["Body"]["method"] == "F_FLIPPED"
    assert "bbox_mismatch" in messages_text(res, "warn")


def test_probe_inconclusive_is_published_with_warning(env, core):
    core.CONFIG["probe"] = {"Roughness": "inconclusive"}
    code, res = env.run()
    assert code == 0
    assert res["probe"]["Roughness"]["status"] == "inconclusive"
    assert "inconclusive" in messages_text(res, "warn")


def test_decision_stop_blocks_with_reasons(env, core):
    core.CONFIG["method"] = "STOP"
    code, res = env.run()
    assert code == 11 and res["status"] == "blocked" and res["method"] == "STOP"
    assert "topology_changed" in messages_text(res, "error")
    assert env.status()["Body"]["state"] == "BLOCKED"
    assert not env.out.exists() and env.ledger_bytes() is None


def test_no_input_textures_blocks(env, core):
    for p in env.src.glob("Body_*"):
        p.unlink()
    code, res = env.run()
    assert code == 11 and "入力テクスチャが見つかりません" in messages_text(res)
    assert not env.out.exists()


def test_frozen_texture_set_is_not_converted(env, core):
    env.root.mkdir(parents=True, exist_ok=True)
    env.ledger_path.write_text(json.dumps({"schema": 1, "ts": {"Body": {
        "generation": 1, "frozen": True, "outputs": {}}}}), encoding="utf-8")
    before = env.ledger_bytes()
    code, res = env.run("j2", 2)
    assert code == 11 and "frozen" in messages_text(res)
    assert not env.out.exists() and env.ledger_bytes() == before


def test_16bit_gray_channel_round_trips_through_worker(env, core):
    env.add_png("Body_Height.1001.png", c=1, seed=9, bit_depth=16)
    env.freeze_times()
    core.CONFIG["new_tiles"] = [1001]
    code, res = env.run()
    assert code == 0, res["messages"]
    arr, meta = uvremap_io.read_image(env.out / "Body_Height.png")
    assert meta["bit_depth"] == 16 and meta["mode"] == "I;16" and arr.shape == (32, 32, 1)
    src_arr, _ = uvremap_io.read_image(env.src / "Body_Height.1001.png")
    assert np.allclose(arr, src_arr[:, ::-1, :], atol=1e-6)  # スタブ変換(U反転)が16bit精度で出る
    _arr8, meta8 = uvremap_io.read_image(env.out / "Body_Roughness.png")
    assert meta8["bit_depth"] == 8 and meta8["mode"] == "L"


def test_udim_and_normal_mode_are_passed_to_both_convert_and_probe(env, core):
    """QUESTIONS_core Q4: convert_channel と probe_verify に、job の udim / normal_mode を同じ値で渡す。"""
    for udim, mode in ((True, "uv"), (False, "off")):
        core.CALLS.clear()
        code, res = env.run("j_{0}".format(mode), 1, udim=udim, normal_mode=mode)
        assert code == 0, res["messages"]
        converts = [c for c in core.CALLS if c[0] == "convert"]
        probes = [c for c in core.CALLS if c[0] == "probe"]
        assert converts and len(probes) == len(converts)
        assert {(c[5], c[6]) for c in converts} == {(mode, udim)}
        assert {(c[3], c[2]) for c in probes} == {(mode, udim)}


def test_udim_defaults_to_true_when_job_omits_it(env, core):
    p = env.write_job("j1", 1)
    job = json.loads(p.read_text(encoding="utf-8"))
    job.pop("udim", None)
    p.write_text(json.dumps(job), encoding="utf-8")
    assert W.run_job(p) == 0
    assert {c[6] for c in core.CALLS if c[0] == "convert"} == {True}
    assert {c[2] for c in core.CALLS if c[0] == "probe"} == {True}


# ---------------------------------------------------------------------------
# core の例外 -> 終了コード(11 停止 / 12 失敗)。本物の core との接続は test_uvremap_e2e.py
# ---------------------------------------------------------------------------
def _core_error(name: str, *args, bases=(Exception,)):
    """``uvremap_core`` モジュールで定義された例外クラスを模す(ワーカーはモジュール名とクラス名で見分ける)。"""
    cls = type(name, bases, {"__module__": "uvremap_core"})
    exc = cls(*args)
    if name == "ConversionStopped":
        exc.reasons = list(args[0]) if args else []
    return exc


@pytest.mark.parametrize("where", ["convert", "probe"])
@pytest.mark.parametrize("name,code,state,needle", [
    ("ConversionStopped", 11, "BLOCKED", "no_convertible_faces"),
    ("TextureSetNotFound", 11, "BLOCKED", "ts_missing"),
    ("ChannelFormatError", 11, "BLOCKED", "形式"),
])
def test_core_exceptions_map_to_blocked_without_touching_outputs(env, core, monkeypatch, where, name, code, state, needle):
    exc = _core_error(name, ["no_convertible_faces"] if name == "ConversionStopped" else "details here")

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(core, "convert_channel" if where == "convert" else "probe_verify", boom)
    got, res = env.run()
    assert got == code and res["status"] == "blocked", res["messages"]
    assert needle in messages_text(res, "error") and "予期しない例外" not in messages_text(res)
    assert env.status()["Body"]["state"] == state
    assert not env.out.exists() and not env.ledger_path.exists()


@pytest.mark.parametrize("which", ["baseline", "current"])
def test_snapshot_error_is_failed_with_a_clear_message(env, core, monkeypatch, which):
    real = core.load_snapshot

    def load(dirpath):
        if (which == "baseline") == (Path(dirpath).name == "bl1"):
            raise _core_error("SnapshotError", "mesh.npz is missing keys")
        return real(dirpath)

    monkeypatch.setattr(core, "load_snapshot", load)
    got, res = env.run()
    label = "基準" if which == "baseline" else "現行"
    assert got == 12 and res["status"] == "failed"
    assert label + "スナップショット" in messages_text(res, "error") and "予期しない例外" not in messages_text(res)
    assert env.status()["Body"]["state"] == "FAILED" and not env.out.exists()


def test_unrelated_exceptions_with_core_like_names_are_still_unexpected_failures(env, core, monkeypatch):
    class ConversionStopped(Exception):      # uvremap_core 以外のモジュールで定義された同名の例外
        pass

    def boom(*a, **k):
        raise ConversionStopped("not from core")

    monkeypatch.setattr(core, "convert_channel", boom)
    got, res = env.run()
    assert got == 12 and "予期しない例外" in messages_text(res, "error")


def test_ts_uv_hash_prefers_job_then_core_compute_then_meta(env, core, monkeypatch):
    """ledger / flag の uv_hash: job の値 > core.compute_uv_hash(現行, ts) > スナップショット meta.json の値。"""
    code, res = env.run("j1", 1)
    assert code == 0 and env.ledger()["ts"]["Body"]["uv_hash"] == "uv-1"   # スタブ core には compute_uv_hash が無い -> meta.json
    monkeypatch.setattr(core, "compute_uv_hash", lambda snap, ts=None: "ts-level:{0}".format(ts), raising=False)
    code, res = env.run("j2", 2)
    assert code == 0 and env.ledger()["ts"]["Body"]["uv_hash"] == "ts-level:Body"
    code, res = env.run("j3", 3, uv_hash="from-job")
    assert code == 0 and env.ledger()["ts"]["Body"]["uv_hash"] == "from-job"


# ---------------------------------------------------------------------------
# I3: flag は最後 / 失敗時は flag も出力も ledger も変化しない
# ---------------------------------------------------------------------------
def test_flag_is_written_last(env, core, monkeypatch):
    order = []
    real = uvremap_io.replace_file

    def spy(src, dst, **kw):
        order.append(Path(dst).name)
        return real(src, dst, **kw)

    monkeypatch.setattr(uvremap_io, "replace_file", spy)
    code, res = env.run()
    assert code == 0
    out_names = {f["name"] for f in res["files"]}
    flag_i = order.index("_sync_complete.flag")
    ledger_i = order.index("ledger.json", order.index(sorted(out_names)[0]))  # 公開中の ledger 更新
    assert all(order.index(n) < ledger_i < flag_i for n in out_names)
    # flag より後に出力フォルダへ書かれたものは無い(status/result は uvremap_root 側)
    assert not [n for n in order[flag_i + 1:] if n in out_names or n.endswith(W.TMP_SUFFIX)]
    outs = [p.stat().st_mtime_ns for p in env.out.iterdir() if p.name != "_sync_complete.flag"]
    assert (env.out / "_sync_complete.flag").stat().st_mtime_ns >= max(outs)


def _publish_good_generation(env):
    assert env.run("good", 1)[0] == 0
    return {"out": tree_state(env.out), "ledger": env.ledger_bytes(),
            "flag": (env.out / "_sync_complete.flag").read_bytes()}


@pytest.mark.parametrize("scenario", ["exception", "probe_fail_first", "probe_fail_last", "unsupported",
                                      "timeout", "decision_stop"])
def test_failure_leaves_outputs_ledger_and_flag_untouched(env, core, scenario):
    """I3: 途中失敗では出力フォルダ(flag の mtime まで)・ledger が一切変化しない。"""
    good = _publish_good_generation(env)
    over = {}
    if scenario == "exception":
        core.CONFIG["raise_in_convert"] = "boom"
    elif scenario == "probe_fail_first":
        core.CONFIG["probe"] = {"BaseColor": "fail"}
    elif scenario == "probe_fail_last":
        core.CONFIG["probe"] = {"Normal": "fail"}  # 先のチャンネルが pass でも公開しない
    elif scenario == "unsupported":
        env.add_raw("Body_Normal.1001.exr")  # Normal を EXR にすり替える(タイル無しPNGは消す)
        (env.src / "Body_Normal.png").unlink()
        env.freeze_times()
    elif scenario == "timeout":
        core.CONFIG["sleep_s"] = 0.5
        over["timeout_s"] = 0.2
    elif scenario == "decision_stop":
        core.CONFIG["method"] = "STOP"
    core.CONFIG["salt"] = 0.3  # 成功していれば出力が変わるはずの設定
    code, res = env.run("bad", 2, **over)
    assert code in (11, 12), res
    assert tree_state(env.out) == good["out"]  # sha1・mtime・ディレクトリ mtime まで同じ
    assert env.ledger_bytes() == good["ledger"]
    assert (env.out / "_sync_complete.flag").read_bytes() == good["flag"]
    assert res["status"] in ("failed", "blocked") and res["files"] == []
    assert env.status()["Body"]["state"] in ("FAILED", "BLOCKED")
    assert not list(env.out.glob("*" + W.TMP_SUFFIX))


def test_failure_on_first_ever_run_creates_no_output_or_flag(env, core):
    core.CONFIG["raise_in_convert"] = "boom"
    code, res = env.run()
    assert code == 12 and not env.out.exists() and not env.ledger_path.exists()


def test_publish_failure_rolls_back_to_previous_generation(env, core, monkeypatch):
    """公開中の失敗(Windows で他プロセスが開いている等)は、出力・flag・ledger・手編集退避まで戻す。"""
    good = _publish_good_generation(env)
    edited = env.out / "Body_Normal.1001.png"
    edited.write_bytes(b"hand edited")  # I6 の退避 → ロールバックで元に戻ること
    edited_state = {"Body_Normal.1001.png": sha1_bytes(b"hand edited")}
    monkeypatch.setattr(uvremap_io, "REPLACE_DELAY_S", 0)
    monkeypatch.setattr(uvremap_io, "REPLACE_RETRIES", 1)
    real = os.replace
    hits = {"n": 0}

    def flaky(src, dst, *a, **k):
        if Path(dst).name == "Body_BaseColor.1002.png":  # 公開の 1 回目だけ失敗。復元は通す
            hits["n"] += 1
            if hits["n"] == 1:
                raise PermissionError(13, "in use by another process")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", flaky)
    core.CONFIG["salt"] = 0.3
    code, res = env.run("bad", 2)
    assert code == 12 and res["status"] == "failed"
    assert "ロールバック" in messages_text(res, "error")
    now = {k: v[0] for k, v in env.out_state().items()}
    expect = {k: v[0] for k, v in good["out"].items() if not k.endswith("/")}
    expect["Body_Normal.1001.png"] = edited_state["Body_Normal.1001.png"]
    assert now == expect  # 手編集ファイルも元の名前へ戻り、.edited_ や一時ファイルは残らない
    assert env.ledger_bytes() == good["ledger"]
    assert (env.out / "_sync_complete.flag").read_bytes() == good["flag"]
    assert not (env.root / "publish_journal.Body.json").exists()
    assert env.status()["Body"]["state"] == "FAILED"


def test_crash_during_publish_recovers_on_next_run(env, core, monkeypatch):
    """公開途中でプロセスごと死んでも(例外が握りつぶされない BaseException で模擬)、
    flag は無く(=完了と見えない)、次の実行が復帰して完全な世代を公開する。"""
    assert env.run("good", 1)[0] == 0

    class Crash(BaseException):
        pass

    real = uvremap_io.replace_file
    state = {"armed": True}

    def crashing(src, dst, **kw):
        if state["armed"] and Path(dst).name == "Body_BaseColor.1002.png":
            raise Crash()
        return real(src, dst, **kw)

    monkeypatch.setattr(uvremap_io, "replace_file", crashing)
    core.CONFIG["salt"] = 0.2
    p = env.write_job("crash", 2)
    with pytest.raises(Crash):
        W.run_job(p)
    assert not (env.out / "_sync_complete.flag").exists()  # 中途半端な状態は「完了」に見えない
    assert env.ledger()["ts"]["Body"]["generation"] == 1
    assert env.status()["Body"]["state"] == "CONVERTING"

    state["armed"] = False
    core.CONFIG["salt"] = 0.4
    code, res = env.run("recover", 3)
    assert code == 0, res["messages"]
    files = env.out_files()
    assert not [n for n in files if ".edited_" in n or n.endswith(W.TMP_SUFFIX)]  # 退避の誤検出なし
    flag = json.loads((env.out / "_sync_complete.flag").read_text(encoding="utf-8"))
    assert flag["generation"] == 3
    led = env.ledger()["ts"]["Body"]
    assert led["generation"] == 3
    for name, sha in led["outputs"].items():
        assert sha1_path(env.out / name) == sha
    assert env.status()["Body"]["state"] == "CONVERTED_OK"
    assert not (env.root / "publish_journal.Body.json").exists()


# ---------------------------------------------------------------------------
# I1: 原本は読み取りのみ
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scenario", ["ok", "probe_fail", "exception", "decision_stop"])
def test_i1_source_dir_never_changes(env, core, scenario):
    before = tree_state(env.src)  # sha1 + mtime + ディレクトリ mtime
    if scenario == "probe_fail":
        core.CONFIG["probe_default"] = "fail"
    elif scenario == "exception":
        core.CONFIG["raise_in_convert"] = "boom"
    elif scenario == "decision_stop":
        core.CONFIG["method"] = "STOP"
    code, _res = env.run()
    assert code == (0 if scenario == "ok" else (11 if scenario == "decision_stop" else 12))
    assert tree_state(env.src) == before


def test_i1_source_only_opened_read_only(env, core, monkeypatch):
    real_open = builtins.open
    opened = []

    def spy(file, mode="r", *a, **k):
        try:
            if Path(os.fspath(file)).resolve().is_relative_to(env.src.resolve()):
                opened.append((Path(os.fspath(file)).name, mode))
        except (TypeError, ValueError, OSError):
            pass
        return real_open(file, mode, *a, **k)

    removed = []
    for fn in ("remove", "unlink", "rename", "rmdir"):
        real_fn = getattr(os, fn)

        def guard(path, *a, _real=real_fn, _fn=fn, **k):
            if Path(os.fspath(path)).resolve().is_relative_to(env.src.resolve()):
                removed.append((_fn, str(path)))
            return _real(path, *a, **k)

        monkeypatch.setattr(os, fn, guard)
    monkeypatch.setattr(builtins, "open", spy)
    code, _res = env.run()
    assert code == 0 and opened and removed == []
    assert {mode for _n, mode in opened} == {"rb"}  # 書き込み・追記・更新モードで開いていない


def test_i1_output_inside_source_dir_is_blocked(env, core):
    before = tree_state(env.src)
    code, res = env.run(output_dir=str(env.src / "converted"))
    assert code == 11 and res["status"] == "blocked"
    assert "出力先" in messages_text(res, "error") and "I1" in messages_text(res, "error")
    assert tree_state(env.src) == before and not (env.src / "converted").exists()
    assert not env.ledger_path.exists()
    assert env.status()["Body"]["state"] == "BLOCKED"


def test_i1_source_inside_output_dir_is_blocked(env, core):
    code, res = env.run(output_dir=str(env.src.parent))
    assert code == 11 and "原本フォルダが出力先配下" in messages_text(res)


def test_i1_uvremap_root_inside_source_dir_is_blocked_and_writes_nothing(env, core):
    before = tree_state(env.src)
    root_in_src = env.src / "uvremap_proj"
    p = env.write_job("jroot", 1, where=env.base / "jobfiles" / "jroot", uvremap_root=str(root_in_src))
    code = W.run_job(p)
    res = env.result(p)  # result.json は job.json の隣へ(原本配下には書かない)
    assert code == 11 and "uvremap_root" in messages_text(res)
    assert tree_state(env.src) == before and not root_in_src.exists()


def test_i1_input_under_uvremap_output_is_blocked(env, core):
    """変換結果(uvremap_root/output 配下)を入力にして二重変換しない。"""
    converted = env.root / "output" / "final"
    converted.mkdir(parents=True)
    arr = make_array(32, 32, 3, 5)
    uvremap_io.write_image(converted / "Body_BaseColor.1001.png", arr,
                           {"bit_depth": 8, "mode": "RGB", "ext": ".png"})
    other_out = env.root / "output" / "final2"
    code, res = env.run(source_dir=str(converted), source_flag=None, output_dir=str(other_out))
    assert code == 11 and "多重変換" in messages_text(res, "error")
    assert not other_out.exists() and not core.CALLS


# ---------------------------------------------------------------------------
# I2: コピー経由 / コピー前後の source_flag
# ---------------------------------------------------------------------------
def test_i2_original_is_never_decoded_in_place(env, core, monkeypatch):
    paths = []
    real = uvremap_io.read_image

    def spy(path):
        paths.append(Path(path))
        return real(path)

    monkeypatch.setattr(uvremap_io, "read_image", spy)
    code, _res = env.run()
    assert code == 0 and len(paths) == 4
    input_dir = (env.root / "jobs" / "j1" / "input").resolve()
    for p in paths:
        assert p.resolve().is_relative_to(input_dir)
        assert not p.resolve().is_relative_to(env.src.resolve())
    assert not (env.root / "jobs" / "j1" / "input").exists()  # 成功後は作業コピーを消す


def _bump_flag_on_copy(monkeypatch, env, times: int | None):
    """_copy_input のたびに source_flag の mtime を進める(times 回まで。None なら常に)。"""
    real = W._Run._copy_input
    count = {"n": 0, "flag": 0}

    def bumping(self, src, dst):
        sha = real(self, src, dst)
        if not Path(src).resolve().is_relative_to(env.src.resolve()):
            return sha  # 公開時のステージング -> 一時ファイルのコピーは数えない
        count["n"] += 1
        if times is None or count["flag"] < times:
            count["flag"] += 1
            t = T0 + (count["flag"] + 1) * 10**9
            os.utime(env.flag, ns=(t, t))
        return sha

    monkeypatch.setattr(W._Run, "_copy_input", bumping)
    return count


def test_i2_flag_changed_during_copy_retries_then_succeeds(env, core, monkeypatch):
    count = _bump_flag_on_copy(monkeypatch, env, times=1)
    code, res = env.run()
    assert code == 0, res["messages"]
    assert "source_flag が変化" in messages_text(res, "warn")
    assert "2 回目" in messages_text(res, "info")
    # 採用された入力は2回目のコピー(= 台帳の sources は原本の最終内容)
    for name, sha in env.ledger()["ts"]["Body"]["sources"].items():
        assert sha == sha1_path(env.src / name)
    assert count["n"] == 4 + 4  # 1回目(破棄)+ 2回目


def test_i2_flag_keeps_changing_exhausts_retries_with_13(env, core, monkeypatch):
    _bump_flag_on_copy(monkeypatch, env, times=None)
    before_src = {p.name: sha1_path(p) for p in env.src.iterdir() if p.name != "_sync_complete.flag"}
    code, res = env.run(copy_retries=2)
    assert code == 13 and res["status"] == "retry_exhausted" and res["exit_code"] == 13
    assert "試行 3 回" in messages_text(res, "error")  # 最初の1回 + やり直し2回
    assert not env.out.exists() and not env.ledger_path.exists() and not core.CALLS
    assert env.status()["Body"]["state"] == "FAILED"
    assert before_src == {p.name: sha1_path(p) for p in env.src.iterdir() if p.name != "_sync_complete.flag"}


def test_i2_zero_retries_means_single_attempt(env, core, monkeypatch):
    count = _bump_flag_on_copy(monkeypatch, env, times=None)
    code, res = env.run(copy_retries=0)
    assert code == 13 and count["n"] == 4  # 1 回だけコピーして諦める


def test_i2_missing_flag_still_works_with_warning(env, core):
    env.flag.unlink()
    code, res = env.run()
    assert code == 0 and "source_flag が存在しません" in messages_text(res, "warn")


# ---------------------------------------------------------------------------
# I3: 強制終了 / タイムアウト(実サブプロセス)
# ---------------------------------------------------------------------------
RUNNER = _TESTS / "_run_worker_stub.py"


def spawn_worker(job_path, cfg_path=None):
    """スタブ core を差し込んだ CLI(`run <job.json>`)をサブプロセスで起動する。"""
    environ = dict(os.environ)
    environ["PYTHONIOENCODING"] = "utf-8"
    if cfg_path is not None:
        environ["STUB_CORE_CONFIG"] = str(cfg_path)
    else:
        environ.pop("STUB_CORE_CONFIG", None)
    return subprocess.Popen([sys.executable, str(RUNNER), "run", str(job_path)], env=environ,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")


def wait_for(path, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return
        time.sleep(0.02)
    raise AssertionError("待機タイムアウト: {0}".format(Path(path).name))


def run_sub(job_path, cfg_path=None, timeout: float = 60.0):
    proc = spawn_worker(job_path, cfg_path)
    out, _ = proc.communicate(timeout=timeout)
    return proc.returncode, out


@pytest.mark.parametrize("how", ["terminate", "kill"])
def test_i3_killed_worker_leaves_outputs_intact_and_next_run_recovers(env, tmp_path, how):
    cfg = tmp_path / "stub_cfg.json"
    marker = tmp_path / "in_convert.marker"

    def write_cfg(**kw):
        cfg.write_text(json.dumps({"new_tiles": [1001, 1002], **kw}), encoding="utf-8")

    write_cfg()
    rc, log = run_sub(env.write_job("good", 1), cfg)
    assert rc == 0, log
    good = {"out": tree_state(env.out), "ledger": env.ledger_bytes()}

    write_cfg(sleep_s=120, marker=str(marker), salt=0.3)
    job2 = env.write_job("victim", 2)
    proc = spawn_worker(job2, cfg)
    try:
        wait_for(marker)  # 変換の最中
        (proc.terminate if how == "terminate" else proc.kill)()
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    # 出力・台帳は旧世代のまま(flag の mtime まで)
    assert tree_state(env.out) == good["out"]
    assert env.ledger_bytes() == good["ledger"]
    assert not list(env.out.glob("*" + W.TMP_SUFFIX))
    if how == "terminate" and not IS_NT:
        # SIGTERM はハンドラで中断扱い(12)になり、理由が残る
        assert proc.returncode == 12
        res = env.result(job2)
        assert res["status"] == "failed" and "中断" in messages_text(res, "error")
        assert env.status()["Body"]["state"] == "FAILED"
    else:
        # SIGKILL 相当: 何も書けない。status は CONVERTING のまま(pid/job_id 付きで古さを判定できる)
        assert not (job2.parent / "result.json").exists()
        row = env.status()["Body"]
        assert row["state"] == "CONVERTING" and row["job_id"] == "victim" and isinstance(row["pid"], int)

    # 次の実行で復帰する(ロックは OS が解放済み・半端なファイルに引きずられない)
    marker.unlink(missing_ok=True)
    write_cfg(salt=0.3)
    rc, log = run_sub(env.write_job("victim", 2), cfg)
    assert rc == 0, log
    assert env.status()["Body"]["state"] == "CONVERTED_OK"
    flag = json.loads((env.out / "_sync_complete.flag").read_text(encoding="utf-8"))
    assert flag["generation"] == 2
    led = env.ledger()["ts"]["Body"]
    assert led["generation"] == 2
    for name, sha in led["outputs"].items():
        assert sha1_path(env.out / name) == sha
    assert not [n for n in env.out_files() if ".edited_" in n]


def test_i3_stuck_convert_is_hard_killed_after_grace(env, tmp_path):
    """協調的に止まらない処理(長い numpy 呼び出し相当)は、猶予後に結果を書いて強制終了する。"""
    cfg = tmp_path / "stub_cfg.json"
    marker = tmp_path / "m"
    cfg.write_text(json.dumps({"new_tiles": [1001], "sleep_s": 120, "marker": str(marker)}), encoding="utf-8")
    before = tree_state(env.out)
    job = env.write_job("stuck", 1, timeout_s=0.5, hard_kill_grace_s=0.5)
    t0 = time.monotonic()
    rc, log = run_sub(job, cfg, timeout=60)
    assert rc == 12 and time.monotonic() - t0 < 30, log
    res = env.result(job)
    assert res["status"] == "failed" and "強制終了" in messages_text(res, "error")
    assert env.status()["Body"]["state"] == "FAILED"
    assert tree_state(env.out) == before and not env.ledger_path.exists()


def test_i3_cooperative_timeout_and_watchdog_in_process(env, core, monkeypatch):
    hard = []
    monkeypatch.setattr(W._Run, "_hard_exit", lambda self, code: hard.append(code))
    core.CONFIG["sleep_s"] = 1.0
    t0 = time.monotonic()
    code, res = env.run(timeout_s=0.2, hard_kill_grace_s=0.2)
    assert code == 12 and res["status"] == "failed"
    assert hard == [12]  # ウォッチドッグが猶予後に強制終了を要求した(テストでは差し替え)
    assert "タイムアウト" in messages_text(res, "error") and time.monotonic() - t0 < 10
    assert not env.out.exists() and not env.ledger_path.exists()


# ---------------------------------------------------------------------------
# I4: 世代
# ---------------------------------------------------------------------------
def _seed_ledger(env, generation: int, **extra):
    env.root.mkdir(parents=True, exist_ok=True)
    entry = {"generation": generation, "last_good_generation": generation, "uv_hash": "old",
             "outputs": {}, "sources": {}, "frozen": False}
    entry.update(extra)
    env.ledger_path.write_text(json.dumps({"schema": 1, "ts": {"Body": entry}}), encoding="utf-8")


def test_i4_older_generation_writes_nothing_and_exits_10(env, core):
    _seed_ledger(env, 5)
    before = env.ledger_bytes()
    code, res = env.run("old", 3)
    assert code == 10 and res["status"] == "superseded" and res["exit_code"] == 10
    assert not env.out.exists() and env.ledger_bytes() == before
    assert not (env.root / "status.json").exists()  # status.json にも触れない
    assert not core.CALLS  # 変換にも進まない
    assert not (env.root / "jobs" / "old" / "input").exists()


def test_i4_older_generation_does_not_clobber_existing_status_row(env, core):
    _seed_ledger(env, 5)
    row = {"state": "CONVERTED_OK", "generation": 5, "updated": "2026-10-16T10:00:00", "message": ""}
    (env.root / "status.json").write_text(json.dumps({"schema": 1, "ts": {"Body": row}}), encoding="utf-8")
    code, _res = env.run("old", 4)
    assert code == 10 and env.status()["Body"] == row


def test_i4_equal_generation_is_allowed(env, core):
    _seed_ledger(env, 5)
    code, _res = env.run("same", 5)
    assert code == 0


def test_i4_generation_advances_during_convert_discards_result(env, core):
    """変換中に世代が進んだ(別ジョブが公開済み / 台帳の世代が上がった)ら、公開せず 10。"""
    good_gen = 4
    assert env.run("good", good_gen)[0] == 0
    out_before = tree_state(env.out)

    def advance(_spec):
        led = env.ledger()
        led["ts"]["Body"]["generation"] = 6
        env.ledger_path.write_text(json.dumps(led), encoding="utf-8")

    core.CONFIG.update(on_convert=advance, salt=0.3)
    ledger_after_advance = None
    code, res = env.run("slow", 5)
    ledger_after_advance = env.ledger()
    assert code == 10 and res["status"] == "superseded" and res["files"] == []
    assert tree_state(env.out) == out_before  # 出力は旧世代のまま
    assert ledger_after_advance["ts"]["Body"]["generation"] == 6  # 台帳は他者の更新のまま
    assert "古い" in messages_text(res)
    # 破棄したジョブが残した CONVERTING 表示は、元の行へ戻る
    assert env.status()["Body"]["state"] == "CONVERTED_OK"
    assert env.status()["Body"]["generation"] == good_gen


@pytest.mark.parametrize("content", ["9", '{"generation": 9}'])
def test_i4_latest_generation_path_supersedes_before_start(env, core, content):
    latest = env.root / "jobs" / "latest_generation"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(content, encoding="utf-8")
    code, res = env.run("old", 8, latest_generation_path=str(latest))
    assert code == 10 and not env.out.exists() and not core.CALLS


def test_i4_latest_generation_path_advances_during_convert(env, core):
    latest = env.root / "jobs" / "latest_generation"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text("5", encoding="utf-8")
    core.CONFIG["on_convert"] = lambda _spec: latest.write_text("6", encoding="utf-8")
    code, res = env.run("slow", 5, latest_generation_path=str(latest))
    assert code == 10 and not env.out.exists() and not env.ledger_path.exists()


def test_i4_latest_generation_path_garbage_is_warned_not_fatal(env, core):
    latest = env.root / "jobs" / "latest_generation"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text("not a number", encoding="utf-8")
    code, res = env.run("j", 1, latest_generation_path=str(latest))
    assert code == 0 and "latest_generation_path" in messages_text(res, "warn")


# ---------------------------------------------------------------------------
# I5: 排他ロック
# ---------------------------------------------------------------------------
_HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
import uvremap_worker as W
lock = W.FileLock(sys.argv[2], 5)
lock.acquire()
open(sys.argv[3], "w").write("held")
time.sleep(float(sys.argv[4]))
"""


@contextmanager
def hold_lock_in_other_process(lock_path: Path, marker: Path, seconds: float):
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(_PKG), str(lock_path), str(marker), str(seconds)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        try:
            wait_for(marker)
        except AssertionError:
            proc.kill()
            raise AssertionError("ロック保持役が起動しません: " + proc.communicate()[1].decode("utf-8", "replace"))
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


def test_i5_lock_held_by_other_process_times_out_with_12(env, core, tmp_path):
    env.root.mkdir(parents=True, exist_ok=True)
    with hold_lock_in_other_process(env.root / "worker.lock", tmp_path / "held", 60):
        t0 = time.monotonic()
        code, res = env.run(lock_timeout_s=0.5)
        waited = time.monotonic() - t0
    assert code == 12 and res["status"] == "failed"
    assert 0.4 <= waited < 15  # 待機したうえで諦める
    assert "worker.lock" in messages_text(res, "error") and "タイムアウト" in messages_text(res, "error")
    assert not env.out.exists() and not env.ledger_path.exists() and not core.CALLS
    assert env.status()["Body"]["state"] == "FAILED"


def test_i5_waits_for_lock_then_runs(env, core, tmp_path):
    env.root.mkdir(parents=True, exist_ok=True)
    with hold_lock_in_other_process(env.root / "worker.lock", tmp_path / "held", 1.5):
        t0 = time.monotonic()
        code, res = env.run(lock_timeout_s=30)
        waited = time.monotonic() - t0
    assert code == 0 and waited >= 0.5, res["messages"]  # 保持者が終わるまで待ってから実行
    assert env.out_files()


def test_i5_two_workers_never_overlap(env, core, tmp_path):
    """同時に起動した2つのワーカーの変換区間(ロック内)が時間的に重ならない。"""
    cfg = tmp_path / "cfg.json"
    trace = tmp_path / "trace.txt"
    cfg.write_text(json.dumps({"new_tiles": [1001], "sleep_s": 0.3, "trace": str(trace)}), encoding="utf-8")
    p1, p2 = env.write_job("a", 1), env.write_job("b", 1)
    procs = [spawn_worker(p1, cfg), spawn_worker(p2, cfg)]
    outs = [p.communicate(timeout=90)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    events = []
    for line in trace.read_text(encoding="utf-8").splitlines():
        kind, pid, ts = line.split()
        events.append((float(ts), kind, pid))
    events.sort()
    assert len(events) == 2 * 2 * 3  # 2 プロセス x 3 チャンネル x (start, end)
    # 各プロセスの最初の start から最後の end までが、もう一方と交差しない
    spans = {}
    for ts, kind, pid in events:
        lo, hi = spans.get(pid, (ts, ts))
        spans[pid] = (min(lo, ts), max(hi, ts))
    (a1, a2), (b1, b2) = sorted(spans.values())
    assert a2 <= b1, spans
    assert env.ledger()["ts"]["Body"]["generation"] == 1


class FakeMsvcrt:
    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = []

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes))
        if mode == self.LK_NBLCK and self.fail_times > 0:
            self.fail_times -= 1
            raise OSError(36, "locked by another process")


def test_i5_windows_path_uses_msvcrt_locking(tmp_path, monkeypatch):
    fake = FakeMsvcrt(fail_times=3)
    monkeypatch.setattr(W, "msvcrt", fake)
    lock = W.FileLock(tmp_path / "w.lock", timeout=5, poll=0.001)
    lock.acquire()
    assert fake.calls == [(2, 1)] * 4  # 3 回失敗(ポーリング)+ 1 回成功
    lock.release()
    assert fake.calls[-1] == (0, 1)  # LK_UNLCK


def test_i5_windows_path_times_out(tmp_path, monkeypatch):
    fake = FakeMsvcrt(fail_times=10**9)
    monkeypatch.setattr(W, "msvcrt", fake)
    with pytest.raises(W.LockTimeout):
        W.FileLock(tmp_path / "w.lock", timeout=0.1, poll=0.01).acquire()


@pytest.mark.skipif(IS_NT, reason="POSIX のみ")
def test_i5_posix_uses_fcntl_and_msvcrt_is_nt_only():
    assert W.msvcrt is None and W.fcntl is not None
    tree = ast.parse((_PKG / "uvremap_worker.py").read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import) and any(a.name == "msvcrt" for a in n.names)]
    assert imports
    for n in imports:  # msvcrt の import は `if os.name == "nt":` の中だけ
        parent = parents[n]
        assert isinstance(parent, ast.If) and ast.unparse(parent.test) == "os.name == 'nt'"


@pytest.mark.skipif(IS_NT, reason="POSIX のみ")
def test_i5_fcntl_lock_excludes_second_holder_in_same_process(tmp_path):
    a = W.FileLock(tmp_path / "x.lock", timeout=1)
    b = W.FileLock(tmp_path / "x.lock", timeout=0.1, poll=0.01)
    a.acquire()
    try:
        with pytest.raises(W.LockTimeout):
            b.acquire()
    finally:
        a.release()
    b.acquire()
    b.release()


# ---------------------------------------------------------------------------
# I6: 手編集の保護
# ---------------------------------------------------------------------------
def _asides(env, base_name):
    return sorted(n for n in env.out_files() if n.startswith(base_name + ".edited_"))


def test_i6_hand_edited_output_is_set_aside_with_warning(env, core):
    assert env.run("j1", 1)[0] == 0
    target = env.out / "Body_BaseColor.1001.png"
    target.write_bytes(b"artist retouch")
    (env.out / "notes.txt").write_text("keep me", encoding="utf-8")  # 台帳に無い無関係ファイル
    core.CONFIG["salt"] = 0.2
    code, res = env.run("j2", 2)
    assert code == 0
    asides = _asides(env, "Body_BaseColor.1001.png")
    assert len(asides) == 1 and re.fullmatch(
        r"Body_BaseColor\.1001\.png\.edited_\d{8}-\d{6}(_\d+)?", asides[0])
    assert (env.out / asides[0]).read_bytes() == b"artist retouch"
    assert "手編集" in messages_text(res, "warn") and asides[0] in messages_text(res, "warn")
    assert sha1_path(target) == env.ledger()["ts"]["Body"]["outputs"]["Body_BaseColor.1001.png"]
    assert (env.out / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert len([n for n in env.out_files() if ".edited_" in n]) == 1  # 未編集のファイルは退避しない


def test_i6_unedited_outputs_are_never_set_aside(env, core):
    assert env.run("j1", 1)[0] == 0
    core.CONFIG["salt"] = 0.2
    code, res = env.run("j2", 2)
    assert code == 0 and not [n for n in env.out_files() if ".edited_" in n]
    assert "手編集" not in messages_text(res)


def test_i6_repeated_edits_in_same_second_keep_all_versions(env, core):
    assert env.run("j1", 1)[0] == 0
    target = env.out / "Body_Roughness.1001.png"
    target.write_bytes(b"edit one")
    core.CONFIG["salt"] = 0.1
    assert env.run("j2", 2)[0] == 0
    target.write_bytes(b"edit two")
    core.CONFIG["salt"] = 0.2
    assert env.run("j3", 3)[0] == 0
    asides = _asides(env, "Body_Roughness.1001.png")
    assert len(asides) == 2
    assert {(env.out / a).read_bytes() for a in asides} == {b"edit one", b"edit two"}


def test_i6_edited_obsolete_tile_is_set_aside_not_deleted(env, core):
    assert env.run("j1", 1)[0] == 0
    (env.out / "Body_BaseColor.1002.png").write_bytes(b"precious")
    core.CONFIG["new_tiles"] = [1001]
    assert env.run("j2", 2)[0] == 0
    asides = _asides(env, "Body_BaseColor.1002.png")
    assert len(asides) == 1 and (env.out / asides[0]).read_bytes() == b"precious"
    assert not (env.out / "Body_BaseColor.1002.png").exists()


def test_i6_output_without_ledger_record_is_treated_as_unknown_and_set_aside(env, core):
    assert env.run("j1", 1)[0] == 0
    env.ledger_path.unlink()
    (env.root / "publish_journal.Body.json").unlink(missing_ok=True)
    core.CONFIG["salt"] = 0.2
    code, res = env.run("j2", 2)
    assert code == 0 and "手編集" in messages_text(res, "warn")
    assert _asides(env, "Body_BaseColor.1001.png")


# ---------------------------------------------------------------------------
# I7: 例外は必ず理由を残す / status.json は原子的で正しく遷移する
# ---------------------------------------------------------------------------
@pytest.fixture
def status_trace(monkeypatch):
    seen = []
    real = W._Run.set_status

    def spy(self, state, message=""):
        seen.append(state)
        return real(self, state, message)

    monkeypatch.setattr(W._Run, "set_status", spy)
    return seen


@pytest.mark.parametrize("scenario,expected_code,expected_states", [
    ("ok", 0, ["CONVERTING", "CONVERTED_OK"]),
    ("exception", 12, ["CONVERTING", "FAILED"]),
    ("probe_fail", 12, ["CONVERTING", "FAILED"]),
    ("stop", 11, ["CONVERTING", "BLOCKED"]),
    ("guard", 11, ["BLOCKED"]),
    ("superseded", 10, ["CONVERTING"]),  # 別ジョブが進めた場合のみ。下で CONVERTING 後に破棄される形
])
def test_i7_status_state_transitions(env, core, status_trace, scenario, expected_code, expected_states):
    over = {}
    if scenario == "exception":
        core.CONFIG["raise_in_convert"] = "boom"
    elif scenario == "probe_fail":
        core.CONFIG["probe_default"] = "fail"
    elif scenario == "stop":
        core.CONFIG["method"] = "STOP"
    elif scenario == "guard":
        over["output_dir"] = str(env.src / "x")
    elif scenario == "superseded":
        def advance(_spec):
            env.ledger_path.write_text(json.dumps({"schema": 1, "ts": {"Body": {"generation": 99}}}),
                                       encoding="utf-8")
        core.CONFIG["on_convert"] = advance
    code, res = env.run(**over)
    assert code == expected_code
    assert status_trace == expected_states
    row = env.status().get("Body")
    if scenario == "superseded":
        assert row is None  # 破棄時は CONVERTING を元(無し)へ戻す
        return
    assert row["state"] == expected_states[-1] and row["generation"] == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", row["updated"])
    if scenario != "ok":
        assert row["message"]  # 理由が表示用にも残る


def test_i7_status_preserves_other_rows_and_never_downgrades_a_newer_generation(env, core):
    env.root.mkdir(parents=True, exist_ok=True)
    head = {"state": "STALE", "generation": 4, "updated": "2026-10-16T09:00:00", "message": ""}
    newer = {"state": "CONVERTING", "generation": 9, "updated": "2026-10-16T09:00:00", "message": ""}
    (env.root / "status.json").write_text(json.dumps({"schema": 1, "ts": {"Head": head, "Body": newer}}),
                                          encoding="utf-8")
    code, _res = env.run("j", 8)
    assert code == 0
    st = env.status()
    assert st["Head"] == head and st["Body"] == newer  # 世代9の行を世代8の結果で戻さない


def test_i7_status_and_ledger_are_atomic_for_readers(env, core):
    """書き込み中に読んでも、壊れた/途中のJSONや空ファイルを見ない。"""
    stop = threading.Event()
    bad = []
    reads = {"n": 0}
    targets = [env.root / "status.json", env.ledger_path, env.out / "_sync_complete.flag"]

    def reader():
        while not stop.is_set():
            for p in targets:
                try:
                    raw = p.read_bytes()
                except OSError:
                    continue  # まだ無い(Windows で置換と衝突する場合も含む)
                reads["n"] += 1
                try:
                    json.loads(raw.decode("utf-8"))
                except ValueError:
                    bad.append((p.name, raw[:40]))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for g in range(1, 16):
            core.CONFIG["salt"] = 0.01 * g
            assert env.run("j{0}".format(g), g)[0] == 0
    finally:
        stop.set()
        t.join(5)
    assert reads["n"] > 20 and bad == []


def test_write_json_atomic_uses_temp_then_replace_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    W.write_json_atomic(target, {"a": 1})
    seen = []
    real = uvremap_io.replace_file

    def spy(src, dst, **kw):
        seen.append((Path(src), Path(dst)))
        assert Path(src) != Path(dst) and Path(src).parent == Path(dst).parent
        assert Path(src).exists() and json.loads(Path(src).read_text(encoding="utf-8")) == {"a": 2}
        return real(src, dst, **kw)

    monkeypatch.setattr(uvremap_io, "replace_file", spy)
    W.write_json_atomic(target, {"a": 2})
    assert len(seen) == 1 and json.loads(target.read_text(encoding="utf-8")) == {"a": 2}

    def boom(src, dst, **kw):
        raise OSError("disk gone")

    monkeypatch.setattr(uvremap_io, "replace_file", boom)
    with pytest.raises(OSError):
        W.write_json_atomic(target, {"a": 3})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}  # 元の内容が残る
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # 一時ファイルも残らない


def _scenario_bad_json(env, core, monkeypatch):
    p = env.root / "jobs" / "bad" / "job.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    return p, "job.json"


def _scenario_missing_field(env, core, monkeypatch):
    p = env.write_job("bad", 1)
    data = json.loads(p.read_text(encoding="utf-8"))
    del data["channels"]
    p.write_text(json.dumps(data), encoding="utf-8")
    return p, "channels"


def _scenario_job_missing(env, core, monkeypatch):
    p = env.root / "jobs" / "none" / "job.json"
    p.parent.mkdir(parents=True)
    return p, "見つかりません"


def _scenario_core_missing(env, core, monkeypatch):
    monkeypatch.setitem(sys.modules, "uvremap_core", None)
    return env.write_job("bad", 1), "uvremap_core"


def _scenario_core_api_missing(env, core, monkeypatch):
    import types
    mod = types.ModuleType("uvremap_core")
    mod.classify = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "uvremap_core", mod)
    return env.write_job("bad", 1), "必要な API"


def _scenario_convert_raises(env, core, monkeypatch):
    core.CONFIG["raise_in_convert"] = "boom-in-convert"
    return env.write_job("bad", 1), "RuntimeError: boom-in-convert"


def _scenario_probe_fail(env, core, monkeypatch):
    core.CONFIG["probe_default"] = "fail"
    return env.write_job("bad", 1), "プローブ失敗"


def _scenario_ledger_corrupt(env, core, monkeypatch):
    env.root.mkdir(parents=True, exist_ok=True)
    env.ledger_path.write_text("{broken", encoding="utf-8")
    return env.write_job("bad", 1), "ledger.json"


def _scenario_corrupt_png(env, core, monkeypatch):
    env.add_raw("Body_Metallic.1001.png", make_png_bytes(8, 8, bit_depth=8, color_type=2)[:-30] + b"\x00" * 30)
    env.freeze_times()
    return env.write_job("bad", 1), "Body_Metallic"


def _scenario_core_load_fails(env, core, monkeypatch):
    def load(_p):
        raise FileNotFoundError("mesh.npz missing")
    monkeypatch.setattr(core, "load_snapshot", load)
    return env.write_job("bad", 1), "mesh.npz missing"


_FAILURE_SCENARIOS = [
    (_scenario_bad_json, 12, False), (_scenario_missing_field, 12, False), (_scenario_job_missing, 12, False),
    (_scenario_core_missing, 12, True), (_scenario_core_api_missing, 12, True),
    (_scenario_convert_raises, 12, True), (_scenario_probe_fail, 12, True),
    (_scenario_ledger_corrupt, 12, True), (_scenario_corrupt_png, 12, True),
    (_scenario_core_load_fails, 12, True),
]


@pytest.mark.parametrize("scenario,code_expected,has_root",
                         _FAILURE_SCENARIOS, ids=[s[0].__name__[10:] for s in _FAILURE_SCENARIOS])
def test_i7_every_failure_leaves_a_reason(env, core, monkeypatch, scenario, code_expected, has_root):
    job_path, needle = scenario(env, core, monkeypatch)
    ledger_before = env.ledger_bytes()
    code = W.run_job(job_path)
    assert code == code_expected
    result = Env.result(job_path)  # result.json は必ず job.json の隣に出る
    assert result["status"] == "failed" and result["exit_code"] == code_expected
    errors = [m for m in result["messages"] if m["level"] == "error"]
    assert errors and needle in messages_text(result, "error"), result["messages"]
    assert not env.out.exists() and env.ledger_bytes() == ledger_before
    if has_root:
        row = env.status()["Body"]
        assert row["state"] == "FAILED" and row["message"]


# ホームパスらしい偽データ。ユーザー名・パスをリテラルで書かない(組み立てて使う)
_U1, _U2, _U3 = "dummy" + "one", "dummy" + "two", "dummy" + "three"
_POSIX_HOME_ROOT = "/" + "ho" + "me" + "/"
_WIN_HOME_ROOT = "C:" + "\\" + "Us" + "ers" + "\\"


def test_i7_exception_trace_has_no_absolute_paths_and_home_is_scrubbed(env, core, tmp_path, monkeypatch):
    fake_home = tmp_path / "homes" / _U1
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    core.CONFIG["raise_in_convert"] = "cannot open {0}/tex/a.png and {1}{2}{3}x.png".format(
        fake_home, _WIN_HOME_ROOT, _U2, "\\")
    code, res = env.run()
    assert code == 12
    blob = json.dumps(res, ensure_ascii=False) + json.dumps(env.status(), ensure_ascii=False)
    for secret in (_U1, _U2, str(fake_home), str(tmp_path)):
        assert secret not in blob
    assert "~" in messages_text(res, "error")  # ホーム部分は ~ に置き換わる
    assert "トレース" in messages_text(res, "error")  # 場所の手掛かりはファイル名と行番号で残る


def test_scrub_unit():
    s = W._scrub("open {0}{1}/a/b.png then {2}{3}\\c.png and /Us{4}/{5}/d".format(
        _POSIX_HOME_ROOT, _U1, _WIN_HOME_ROOT, _U2, "ers", _U3))
    assert not any(u in s for u in (_U1, _U2, _U3))
    assert W._scrub("/mnt/data/tex.png") == "/mnt/data/tex.png"


# ---------------------------------------------------------------------------
# 未対応形式(EXR 等) -> 11
# ---------------------------------------------------------------------------
def test_unsupported_exr_exits_11_with_message_and_publishes_nothing(env, core):
    (env.src / "Body_Roughness.1001.png").unlink()  # PNG の Roughness を EXR にすり替える
    env.add_raw("Body_Roughness.1001.exr", b"v/1\x01 fake exr")
    env.freeze_times()
    before = tree_state(env.src)
    code, res = env.run()
    assert code == 11 and res["status"] == "blocked"
    assert "未対応形式" in messages_text(res, "error") and ".exr" in messages_text(res, "error")
    assert not env.out.exists() and not env.ledger_path.exists()  # 先に処理した BaseColor も公開しない
    assert env.status()["Body"]["state"] == "BLOCKED"
    assert tree_state(env.src) == before


def test_worker_runs_without_pillow(env, core, monkeypatch):
    """画像 I/O は自前の PNG 実装だけで動く。Pillow が import できない環境でも変換できる。"""
    monkeypatch.setitem(sys.modules, "PIL", None)          # import PIL -> ImportError
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    code, res = env.run()
    assert code == 0, res["messages"]
    assert (env.out / "Body_Roughness.png").exists() or any(env.out.glob("Body_Roughness*"))


def test_unsupported_interlaced_png_exits_11(env, core):
    env.add_raw("Body_Emissive.1001.png", make_png_bytes(8, 8, bit_depth=8, color_type=2, interlace=1))
    env.freeze_times()
    code, res = env.run()
    assert code == 11 and "インターレース" in messages_text(res, "error")
    assert not env.out.exists()


def test_16bit_rgb_channel_round_trips_through_worker(env, core):
    """16bit のカラー PNG(Painter が出す可能性がある)も、ビット深度とモードを保ったまま変換される。"""
    env.add_png("Body_Emissive.1001.png", c=3, seed=11, bit_depth=16)
    env.freeze_times()
    core.CONFIG["new_tiles"] = [1001]
    code, res = env.run()
    assert code == 0, res["messages"]
    arr, meta = uvremap_io.read_image(env.out / "Body_Emissive.png")
    assert meta["bit_depth"] == 16 and meta["mode"] == "RGB" and arr.shape == (32, 32, 3)
    src_arr, _ = uvremap_io.read_image(env.src / "Body_Emissive.1001.png")
    assert np.allclose(arr, src_arr[:, ::-1, :], atol=1e-6)


# ---------------------------------------------------------------------------
# I8: 外部通信・サブプロセスを使わない
# ---------------------------------------------------------------------------
_FORBIDDEN_MODULES = {"subprocess", "socket", "ssl", "http", "urllib", "ftplib", "smtplib", "telnetlib",
                      "requests", "multiprocessing", "ctypes", "asyncio", "webbrowser", "xmlrpc",
                      "socketserver", "selectors", "pty", "shlex"}
_ALLOWED_MODULES = {"__future__", "argparse", "datetime", "hashlib", "importlib", "inspect", "json", "os",
                    "re", "shutil", "signal", "sys", "threading", "time", "pathlib", "traceback", "struct",
                    "msvcrt", "fcntl", "numpy", "uvremap_io", "zlib"}


@pytest.mark.parametrize("fname", ["uvremap_worker.py", "uvremap_io.py"])
def test_i8_static_no_network_or_subprocess(fname):
    tree = ast.parse((_PKG / fname).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            assert not re.match(r"(system|popen|spawn|exec|startfile|fork|posix_spawn)", node.attr), node.attr
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("eval", "exec", "__import__", "compile"), node.func.id
    assert not (imported & _FORBIDDEN_MODULES), imported & _FORBIDDEN_MODULES
    assert imported <= _ALLOWED_MODULES, imported - _ALLOWED_MODULES


def test_i8_dynamic_job_runs_with_subprocess_and_sockets_disabled(env, core, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("外部通信/サブプロセスは禁止")

    class NoSocket:
        def __init__(self, *a, **k):
            boom()

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket, "socket", NoSocket)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(os, "system", boom)
    monkeypatch.setattr(os, "popen", boom)
    code, _res = env.run()
    assert code == 0
    assert not hasattr(W, "subprocess") and not hasattr(W, "socket")


def test_import_core_only_imports_the_contract_module():
    src = (_PKG / "uvremap_worker.py").read_text(encoding="utf-8")
    assert re.findall(r"import_module\(([^)]*)\)", src) == ['"uvremap_core"']


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_run_end_to_end_via_subprocess(env, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"new_tiles": [1001, 1002]}), encoding="utf-8")
    job = env.write_job("cli", 1)
    rc, log = run_sub(job, cfg)
    assert rc == 0, log
    assert "[uvremap_worker]" in log and "完了" in log  # 標準出力は人間向けログのみ
    assert env.result(job)["status"] == "ok" and env.status()["Body"]["state"] == "CONVERTED_OK"


def test_cli_exit_codes_for_usage_and_missing_job(tmp_path):
    worker = _PKG / "uvremap_worker.py"
    p = subprocess.run([sys.executable, str(worker)], capture_output=True, text=True)
    assert p.returncode == 2
    missing = tmp_path / "nothing" / "job.json"
    p = subprocess.run([sys.executable, str(worker), "run", str(missing)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert p.returncode == 12
    assert "job.json" in json.loads((missing.parent / "result.json").read_text(encoding="utf-8"))["messages"][0]["text"]


# ---------------------------------------------------------------------------
# replace_file の再試行(契約 §5)。単体テストは test_uvremap_io.py。ここはワーカーの公開との接続
# ---------------------------------------------------------------------------
def test_worker_publish_survives_transient_replace_failures(env, core, monkeypatch):
    """他プロセスが一瞬開いている状況(2回失敗してから成功)でも、再試行で公開できる。"""
    monkeypatch.setattr(uvremap_io, "REPLACE_DELAY_S", 0)
    monkeypatch.setattr(uvremap_io, "REPLACE_RETRIES", 3)
    real = os.replace
    left = {"n": 2}

    def flaky(s, d, *a, **k):
        if Path(d).name == "Body_Normal.1001.png" and left["n"] > 0:
            left["n"] -= 1
            raise PermissionError(13, "used by another process")
        return real(s, d, *a, **k)

    monkeypatch.setattr(os, "replace", flaky)
    code, res = env.run()
    assert code == 0 and left["n"] == 0
    assert "Body_Normal.1001.png" in env.out_files()


def test_parse_job_lock_timeout_defaults_to_timeout_s(env):
    job = env.job_dict("j", 1, timeout_s=1200)
    job.pop("lock_timeout_s")
    parsed = W.parse_job(job)
    assert parsed["lock_timeout_s"] == 1200
    parsed2 = W.parse_job(env.job_dict("j", 1, timeout_s=1200, lock_timeout_s=3))
    assert parsed2["lock_timeout_s"] == 3


def test_lock_timeout_does_not_overwrite_newer_generation(env, core, monkeypatch):
    env.root.mkdir(parents=True, exist_ok=True)
    latest = env.root / "latest_generation" / "Body.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps({"generation": 2}), encoding="utf-8")
    (env.root / "status.json").write_text(json.dumps({
        "schema": 1,
        "ts": {
            "Body": {
                "state": "CONVERTING", "generation": 2,
                "updated": "2026-01-01T00:00:00", "message": "",
            },
        },
    }), encoding="utf-8")

    class BoomLock(W.FileLock):
        def acquire(self):
            if Path(self.path).name == "worker.lock":
                raise W.LockTimeout("worker.lock の取得がタイムアウトしました(0.4秒)")
            return super().acquire()

    monkeypatch.setattr(W, "FileLock", BoomLock)
    code, res = env.run("old", 1, latest_generation_path=str(latest), lock_timeout_s=0.4)
    assert code == 10 and res["status"] == "superseded"
    row = env.status()["Body"]
    assert row["state"] == "CONVERTING" and row["generation"] == 2


def test_publish_rollback_failure_deletes_flag_and_next_run_recovers(env, core, monkeypatch):
    good = _publish_good_generation(env)
    monkeypatch.setattr(uvremap_io, "REPLACE_DELAY_S", 0)
    monkeypatch.setattr(uvremap_io, "REPLACE_RETRIES", 1)
    real = os.replace
    counts = {"1001": 0}

    def flaky(src, dst, *a, **k):
        name = Path(dst).name
        if name == "Body_BaseColor.1002.png":
            raise PermissionError(13, "in use by another process")
        if name == "Body_BaseColor.1001.png":
            counts["1001"] += 1
            if counts["1001"] >= 2:
                raise PermissionError(13, "in use by another process")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", flaky)
    core.CONFIG["salt"] = 0.3
    code, res = env.run("bad", 2)
    assert code == 12 and res["status"] == "failed"
    assert "ロールバックに失敗しました。出力が不整合の可能性があります" in messages_text(res, "error")
    assert not (env.out / "_sync_complete.flag").exists()
    assert sha1_path(env.out / "Body_BaseColor.1001.png") != good["out"]["Body_BaseColor.1001.png"][0]

    monkeypatch.setattr(os, "replace", real)
    monkeypatch.setattr(uvremap_io, "REPLACE_RETRIES", None)
    core.CONFIG["salt"] = 0.4
    code2, res2 = env.run("recover", 3)
    assert code2 == 0, res2["messages"]
    flag = json.loads((env.out / "_sync_complete.flag").read_text(encoding="utf-8"))
    assert flag["generation"] == 3
    assert env.status()["Body"]["state"] == "CONVERTED_OK"


def test_ledger_path_escape_is_ignored_and_outside_file_stays(env, core):
    assert env.run("g1", 1)[0] == 0
    secret = env.root / "output" / "secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("keep-me", encoding="utf-8")
    led = env.ledger()
    led["ts"]["Body"]["outputs"]["../secret.txt"] = "deadbeef"
    env.ledger_path.write_text(json.dumps(led), encoding="utf-8")
    core.CONFIG["salt"] = 0.2
    code, res = env.run("g2", 2)
    assert code == 0, res["messages"]
    assert secret.is_file() and secret.read_text(encoding="utf-8") == "keep-me"
    assert not list(env.root.glob("output/secret.txt.edited_*"))
    assert not list(env.out.glob("secret.txt.edited_*"))
    assert "../secret.txt" not in env.ledger()["ts"]["Body"]["outputs"]
    assert "安全でない" in messages_text(res, "warn")


def test_windows_input_match_uses_actual_filename(env, core, monkeypatch):
    monkeypatch.setattr(W, "_OS_NAME", "nt")
    for p in list(env.src.glob("Body_*")):
        p.unlink()
    env.add_png("body_BaseColor.1001.png", c=3, seed=1)
    env.add_png("body_BaseColor.1002.png", c=3, seed=2)
    env.add_png("body_Roughness.1001.png", c=1, seed=3)
    env.add_png("body_Normal.png", c=3, seed=4)
    env.freeze_times()
    code, res = env.run()
    assert code == 0, res["messages"]
    sources = env.ledger()["ts"]["Body"]["sources"]
    assert "body_BaseColor.1001.png" in sources
    assert "Body_BaseColor.1001.png" not in sources


def test_posix_input_match_is_case_sensitive(env, core, monkeypatch):
    monkeypatch.setattr(W, "_OS_NAME", "posix")
    for p in list(env.src.glob("Body_*")):
        p.unlink()
    env.add_png("body_BaseColor.1001.png", c=3, seed=1)
    env.add_png("body_BaseColor.1002.png", c=3, seed=2)
    env.add_png("body_Roughness.1001.png", c=1, seed=3)
    env.add_png("body_Normal.png", c=3, seed=4)
    env.freeze_times()
    code, res = env.run()
    assert code == 11
    assert "入力テクスチャが見つかりません" in messages_text(res)


@pytest.mark.parametrize("name", ["CON", "NUL", "COM1", "Body.", "Body ", "x" * 121, 'Body<>x'])
def test_unsafe_ts_name_is_blocked(env, core, name):
    code, res = env.run("j", 1, ts_name=name)
    assert code == 11 and res["status"] == "blocked"
    assert W.MSG_UNSAFE_TS_NAME in messages_text(res)


def test_namespaced_ts_name_still_rejected_as_job_failed(env, core):
    code, res = env.run("j", 1, ts_name="ns:Body")
    assert code == 12
    assert "パス区切り" in messages_text(res)


def test_discover_delegates_matching_to_match_source_name(tmp_path, monkeypatch):
    """照合はモジュール関数へ切り出してあり、_discover はそれを呼ぶ。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "Body_BaseColor.1001.png").write_bytes(b"x")
    (src / "BodyB_BaseColor.png").write_bytes(b"y")
    run = W._Run(tmp_path / "job.json")
    run.ts_name = "Body"
    run.job = {
        "source_dir": str(src),
        "channels": [{"suffix": "BaseColor"}, {"suffix": "Normal"}],
    }
    calls = []
    real = W.match_source_name

    def spy(ts_name, suffix, name, os_name=None):
        calls.append((ts_name, suffix, name, os_name))
        return real(ts_name, suffix, name, os_name)

    monkeypatch.setattr(W, "match_source_name", spy)
    found = run._discover()
    assert calls
    assert {c[3] for c in calls} == {W._OS_NAME}
    assert found["BaseColor"]["files"] == {1001: "Body_BaseColor.1001.png"}
    assert found["BaseColor"]["ext"] == ".png"
    assert "Normal" not in found
