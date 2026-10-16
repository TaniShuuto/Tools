# -*- coding: utf-8 -*-
"""テスト専用のスタブ core(本物の ``uvremap_core`` ではない)。

契約 CONTRACT.md §3 のシグネチャだけを再現する:
    load_snapshot / classify / convert_channel / probe_verify と
    ChannelSpec / Decision / ConvertResult / ProbeReport / Snapshot。
変換は「U方向の反転(タイルごとに行も巻き戻す)」という恒等ではない決定的な処理で、
出力が入力と区別できる。振る舞いは ``CONFIG``(同一プロセス)または環境変数
``STUB_CORE_CONFIG`` が指す JSON(サブプロセス)で制御する。

CONFIG のキー:
    method        "F"(既定) / "F_FLIPPED" / "STOP"
    new_tiles     出力するタイル番号のリスト(既定 [1001])
    probe         {suffix: "pass"|"fail"|"inconclusive"}、probe_default(既定 "pass")
    raise_in_convert  文字列: convert_channel でこの内容の RuntimeError を送出
    sleep_s       convert_channel で眠る秒数
    marker        convert_channel に入ったら作るファイルのパス(別プロセスの同期用)
    on_convert    callable(spec)  ※同一プロセス専用
    salt          出力に加える微小な差(世代ごとに出力を変えたいとき)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

CONFIG: dict = {}
CALLS: list = []


def _cfg() -> dict:
    cfg = dict(CONFIG)
    env = os.environ.get("STUB_CORE_CONFIG")
    if env:
        cfg.update(json.loads(Path(env).read_text(encoding="utf-8")))
    return cfg


@dataclass
class Snapshot:
    meta: dict
    ts_names: list
    warnings: list = field(default_factory=list)


def load_snapshot(dirpath):
    meta = json.loads((Path(dirpath) / "meta.json").read_text(encoding="utf-8"))
    CALLS.append(("load_snapshot", Path(dirpath).name))
    return Snapshot(meta=meta, ts_names=list(meta.get("ts_names", [])))


@dataclass
class Decision:
    method: str
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    details: dict = field(default_factory=dict)


def classify(baseline, current, ts_name, *, bbox_tol: float = 1e-3):
    method = _cfg().get("method", "F")
    if method == "STOP":
        return Decision("STOP", reasons=["topology_changed"])
    warnings = list(_cfg().get("decision_warnings", []))
    return Decision(method, warnings=warnings)


@dataclass
class ChannelSpec:
    suffix: str
    colorspace: str
    kind: str
    normal_green_down: bool = False


@dataclass
class ConvertResult:
    tiles: dict
    coverage: dict
    stats: dict


def convert_channel(old_tiles, baseline, current, ts_name, decision, spec, out_res, *,
                    dilate_px: int = 16, normal_mode: str = "uv"):
    cfg = _cfg()
    CALLS.append(("convert", spec.suffix, out_res, sorted(old_tiles), dilate_px, normal_mode))
    if cfg.get("marker"):
        Path(cfg["marker"]).write_text("in convert", encoding="utf-8")
    hook = cfg.get("on_convert")
    if callable(hook):
        hook(spec)
    if cfg.get("sleep_s"):
        time.sleep(float(cfg["sleep_s"]))
    if cfg.get("raise_in_convert"):
        raise RuntimeError(cfg["raise_in_convert"])
    src = old_tiles[min(old_tiles)]
    salt = float(cfg.get("salt", 0.0))
    tiles, coverage = {}, {}
    for i, tile in enumerate(cfg.get("new_tiles", [1001])):
        arr = np.roll(src[:, ::-1, :], i, axis=0).astype(np.float32)
        if salt:
            arr = np.clip(arr + salt, 0.0, 1.0).astype(np.float32)
        tiles[int(tile)] = arr
        coverage[int(tile)] = np.ones(arr.shape[:2], dtype=bool)
    return ConvertResult(tiles=tiles, coverage=coverage, stats={"overlap": 0, "suffix": spec.suffix})


@dataclass
class ProbeReport:
    status: str
    n_total: int
    n_valid: int
    n_bad: int
    max_err: float
    mean_err: float
    bad_points: list = field(default_factory=list)


def probe_verify(old_tiles, new_tiles, baseline, current, ts_name, spec, *, n: int = 512, seed: int = 0):
    cfg = _cfg()
    status = cfg.get("probe", {}).get(spec.suffix, cfg.get("probe_default", "pass"))
    if status == "fail":
        return ProbeReport("fail", n, 400, 80, 0.9, 0.3, bad_points=[{"u": 0.5, "v": 0.5}])
    if status == "inconclusive":
        return ProbeReport("inconclusive", n, 10, 0, 0.0, 0.0)
    return ProbeReport("pass", n, 480, 0, 0.03, 0.01)
