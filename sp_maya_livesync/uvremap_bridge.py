# -*- coding: utf-8 -*-
"""Maya 配列 → core Snapshot → ワーカー job.json の橋渡し(Maya 非依存)。

``uvremap_maya.collect_arrays`` と同じ形の ``arrays`` / ``info`` を受け取り、
core の ``Snapshot`` と契約 §4.1 の job.json を作る。Maya は import しない。
指紋・UVハッシュ・bbox の計算は core の関数に任せ、ここで独自に計算しない。

D-3 で ledger と比べる UV ハッシュは、必ず ``current_uv_hash``
(= ``core.compute_uv_hash(snapshot, ts_name)``) のテクスチャセット単位の値である
(QUESTIONS_e2e.md E4)。
"""
from __future__ import annotations

import datetime
import json
import math
import os
from pathlib import Path

import numpy as np

import uvremap_core as core
from uvremap_worker import (
    FileLock,
    LockTimeout,
    STATUS_LOCK_TIMEOUT_S,
    read_json,
    write_json_atomic,
)

SCHEMA = 1
QUALITY_VALUES = ("final", "live")
DEFAULT_CHANNELS = (
    {"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"},
    {"suffix": "Roughness", "colorspace": "raw", "kind": "scalar"},
    {"suffix": "Metallic", "colorspace": "raw", "kind": "scalar"},
    {"suffix": "Normal", "colorspace": "raw", "kind": "normal"},
    {"suffix": "Height", "colorspace": "raw", "kind": "scalar"},
    {"suffix": "Emissive", "colorspace": "srgb", "kind": "color"},
)
# A1 実測: 2048²・4 タイル・5 チャンネルで変換 27.0 秒。
# メガピクセル×チャンネルあたり 27.0 / (4 * 2048² / 1e6 * 5) ≈ 0.3217 秒。
_SEC_PER_MP_CHANNEL = 0.32
_TIMEOUT_MIN_S = 120
_TIMEOUT_MAX_S = 1800
_TIMEOUT_SLACK_MUL = 3.0
_TIMEOUT_SLACK_ADD = 30.0

_ARRAY_KEYS = (
    "face_vertex_counts",
    "corner_vid",
    "corner_pos",
    "corner_uv",
    "face_mesh",
    "tri_face",
    "tri_corner",
)
_EXCLUDED_FACE_KEYS = (
    "nonmanifold_faces",
    "untriangulated_faces",
    "no_uv_faces",
)
_JOB_REQUIRED = (
    "job_id",
    "generation",
    "ts_name",
    "quality",
    "baseline_dir",
    "current_snapshot_dir",
    "source_dir",
    "output_dir",
    "uvremap_root",
)
_TS_FACES_PREFIX = "ts_faces__"

_STOP_MESSAGES = {
    "topology_changed": "トポロジーが変わったため変換を停止しました",
    "ts_missing": "テクスチャセットがスナップショットにありません",
}


class BridgeError(Exception):
    """橋渡しの入力が契約と合わない(キー欠落、TS対応崩れ、job の必須欠落など)。"""


def _naive_now_iso() -> str:
    """ナイーブな現地時刻の ISO8601(秒まで)。ワーカーの ``_now_iso`` と同じ形。"""
    return datetime.datetime.now().isoformat(timespec="seconds")


def _as_path(value, what: str) -> Path:
    if value is None or value == "":
        raise BridgeError("{0} が空です".format(what))
    return Path(value)


def _ts_faces_index(key: str) -> int:
    if not key.startswith(_TS_FACES_PREFIX):
        raise BridgeError("不正な ts_faces キーです: {0!r}".format(key))
    suffix = key[len(_TS_FACES_PREFIX):]
    if suffix == "" or (suffix != "0" and suffix.startswith("0")):
        raise BridgeError("ts_faces キーの番号が不正です: {0!r}".format(key))
    try:
        index = int(suffix)
    except ValueError:
        raise BridgeError("ts_faces キーの番号が不正です: {0!r}".format(key)) from None
    if index < 0 or str(index) != suffix:
        raise BridgeError("ts_faces キーの番号が不正です: {0!r}".format(key))
    return index


def _collect_ts_faces(arrays: dict, ts_names: list) -> dict:
    found = {}
    for key in arrays:
        if not key.startswith(_TS_FACES_PREFIX):
            continue
        found[_ts_faces_index(key)] = key
    expected = set(range(len(ts_names)))
    actual = set(found)
    if actual != expected:
        raise BridgeError(
            "ts_faces__<n> と ts_names[n] の対応が崩れています: "
            "ts_names={0} 個, キー={1}".format(
                len(ts_names),
                "[" + ", ".join(sorted(found.values(), key=_ts_faces_index)) + "]",
            )
        )
    out = {}
    for index, name in enumerate(ts_names):
        out[name] = np.asarray(arrays[found[index]], dtype=np.int32).reshape(-1)
    return out


def _inherit_warnings(info: dict) -> list:
    """info['warnings'] のコードと、除外フェースIDを Snapshot.warnings へ写す。"""
    warnings = [str(code) for code in (info.get("warnings") or [])]
    for key in _EXCLUDED_FACE_KEYS:
        if key not in info:
            continue
        ids = info.get(key) or []
        if not ids:
            continue
        joined = ",".join(str(int(i)) for i in ids)
        warnings.append("{0}:{1}".format(key, joined))
    return warnings


def _decision_method(decision) -> str:
    if decision is None:
        raise BridgeError("decision がありません")
    if isinstance(decision, dict):
        method = decision.get("method")
    else:
        method = getattr(decision, "method", None)
    if not method:
        raise BridgeError("decision.method がありません")
    return str(method)


def _decision_reasons(decision) -> list:
    if isinstance(decision, dict):
        reasons = decision.get("reasons") or []
    else:
        reasons = getattr(decision, "reasons", None) or []
    return [str(r) for r in reasons]


def arrays_to_snapshot(arrays, info, *, baseline_id=None, created=None) -> core.Snapshot:
    """collect_arrays の出力から core の Snapshot を組み立てる。

    meta の topology_fingerprint / uv_hash は core の関数、
    ts_names / mesh_uuids / uv_set / bbox は info から埋める。
    ``created`` を渡せば時計に依存しない(省略時だけ core の既定=UTC now)。
    """
    if not isinstance(arrays, dict):
        raise BridgeError("arrays が dict ではありません")
    if not isinstance(info, dict):
        raise BridgeError("info が dict ではありません")
    missing = [key for key in _ARRAY_KEYS if key not in arrays]
    if missing:
        raise BridgeError("arrays に必須キーがありません: {0}".format(", ".join(missing)))
    if "ts_names" not in info:
        raise BridgeError("info に ts_names がありません")
    ts_names = [str(name) for name in info["ts_names"]]
    ts_faces = _collect_ts_faces(arrays, ts_names)
    try:
        snap = core.make_snapshot(
            arrays["face_vertex_counts"],
            arrays["corner_vid"],
            arrays["corner_pos"],
            arrays["corner_uv"],
            ts_faces=ts_faces,
            face_mesh=arrays["face_mesh"],
            tri_face=arrays["tri_face"],
            tri_corner=arrays["tri_corner"],
            baseline_id=baseline_id,
            uv_set=str(info["uv_set"]) if info.get("uv_set") is not None else "map1",
            mesh_uuids=info.get("mesh_uuids"),
            created=created,
        )
    except core.SnapshotError as exc:
        raise BridgeError("スナップショットを組み立てられません: {0}".format(exc)) from exc

    snap.meta["topology_fingerprint"] = core.compute_topology_fingerprint(snap)
    snap.meta["uv_hash"] = core.compute_uv_hash(snap)
    snap.meta["ts_names"] = list(ts_names)
    if info.get("mesh_uuids") is not None:
        snap.meta["mesh_uuids"] = [str(u) for u in info["mesh_uuids"]]
    if info.get("uv_set") is not None:
        snap.meta["uv_set"] = str(info["uv_set"])
    if info.get("bbox_min") is not None:
        snap.meta["bbox_min"] = [float(x) for x in info["bbox_min"]]
    if info.get("bbox_max") is not None:
        snap.meta["bbox_max"] = [float(x) for x in info["bbox_max"]]
    if baseline_id:
        snap.meta["baseline_id"] = str(baseline_id)
    if created is not None:
        snap.meta["created"] = created
    snap.ts_names = list(ts_names)
    snap.warnings = _inherit_warnings(info)
    return snap


def save_baseline(snapshot: core.Snapshot, uvremap_root) -> Path:
    """``<root>/baseline/<baseline_id>/`` に書く。既にあれば上書きしない。"""
    bid = snapshot.meta.get("baseline_id")
    if not bid:
        raise BridgeError("snapshot.meta に baseline_id がありません")
    dest = Path(uvremap_root) / "baseline" / str(bid)
    if (dest / "mesh.npz").exists() or (dest / "meta.json").exists():
        return dest
    core.save_snapshot(dest, snapshot)
    return dest


def save_current(snapshot: core.Snapshot, uvremap_root, job_id) -> Path:
    """``<root>/jobs/<job_id>/current/`` に現行スナップショットを書く。"""
    if not job_id:
        raise BridgeError("job_id が空です")
    dest = Path(uvremap_root) / "jobs" / str(job_id) / "current"
    core.save_snapshot(dest, snapshot)
    return dest


def current_uv_hash(snapshot: core.Snapshot, ts_name: str) -> str:
    """テクスチャセット単位の現行 UV ハッシュ。

    ``core.compute_uv_hash(snapshot, ts_name)`` を呼ぶだけの薄い関数。
    D-3(直前の正常世代が現行 UV ハッシュと一致しないときは原本へ戻す)で
    ledger の値と比べるのは、**必ずこのテクスチャセット単位の値**である。
    メッシュ全体の ``meta['uv_hash']`` を使ってはならない
    (QUESTIONS_e2e.md E4、複数 TS のメッシュで誤判定になる)。
    """
    return core.compute_uv_hash(snapshot, ts_name)


def next_generation(ledger, ts_name: str) -> int:
    """``ledger['ts'][ts_name]['generation'] + 1``。無ければ 1。"""
    if not isinstance(ledger, dict):
        return 1
    table = ledger.get("ts")
    if not isinstance(table, dict):
        return 1
    entry = table.get(ts_name)
    if not isinstance(entry, dict):
        return 1
    gen = entry.get("generation")
    if isinstance(gen, bool) or not isinstance(gen, int):
        return 1
    return gen + 1


def _pixels_of(out_res, n_tiles: int) -> float:
    if isinstance(out_res, bool) or not isinstance(out_res, (int, float, dict)):
        raise BridgeError("out_res は int か {{tile: (W, H)}} です")
    if isinstance(out_res, dict):
        total = 0.0
        for value in out_res.values():
            try:
                width, height = int(value[0]), int(value[1])
            except Exception:
                raise BridgeError("out_res のタイルサイズが不正です") from None
            if width <= 0 or height <= 0:
                raise BridgeError("out_res のタイルサイズが正ではありません")
            total += float(width) * float(height)
        return total
    if int(out_res) != out_res or int(out_res) <= 0:
        raise BridgeError("out_res は正の整数です")
    if isinstance(n_tiles, bool) or not isinstance(n_tiles, int) or n_tiles < 0:
        raise BridgeError("n_tiles が不正です")
    side = int(out_res)
    return float(n_tiles) * float(side) * float(side)


def choose_timeout_s(out_res, n_channels, n_tiles) -> int:
    """job.json の timeout_s を解像度とチャンネル数から決める。

    根拠(QUESTIONS_e2e.md E7 / 契約 D-5(3) A1 実測):
        2048²・4 タイル・5 チャンネルの変換が 27.0 秒。
        メガピクセル×チャンネルあたり 27.0 / (4 * 2048²/1e6 * 5) ≈ 0.32 秒。
    式:
        estimate = (総ピクセル / 1e6) * n_channels * 0.32
        timeout  = min(1800, max(120, ceil(3 * estimate + 30)))
    ``out_res`` は int(全タイルが正方形)または ``{tile: (W, H)}``。
    契約の既定 300 秒は 4096²・6 チャンネルでは足りない可能性が高い(E7)。
    """
    if isinstance(n_channels, bool) or not isinstance(n_channels, int) or n_channels < 0:
        raise BridgeError("n_channels が不正です")
    if isinstance(n_tiles, bool) or not isinstance(n_tiles, int) or n_tiles < 0:
        raise BridgeError("n_tiles が不正です")
    pixels = _pixels_of(out_res, n_tiles)
    estimate = (pixels / 1_000_000.0) * float(n_channels) * _SEC_PER_MP_CHANNEL
    raw = _TIMEOUT_SLACK_MUL * estimate + _TIMEOUT_SLACK_ADD
    return int(min(_TIMEOUT_MAX_S, max(_TIMEOUT_MIN_S, math.ceil(raw))))


def build_job(
    *,
    job_id,
    generation,
    ts_name,
    quality,
    baseline_dir,
    current_snapshot_dir,
    source_dir,
    output_dir,
    uvremap_root,
    source_flag=None,
    udim=True,
    out_res=None,
    dilate_px=16,
    normal_mode="uv",
    channels=None,
    timeout_s=300,
    copy_retries=3,
    **extra,
) -> dict:
    """契約 §4.1 の job.json を組み立てる。必須キーが欠けたら BridgeError。"""
    values = {
        "job_id": job_id,
        "generation": generation,
        "ts_name": ts_name,
        "quality": quality,
        "baseline_dir": baseline_dir,
        "current_snapshot_dir": current_snapshot_dir,
        "source_dir": source_dir,
        "output_dir": output_dir,
        "uvremap_root": uvremap_root,
    }
    for key in _JOB_REQUIRED:
        if values[key] is None or values[key] == "":
            raise BridgeError("job.json に必須項目がありません: {0}".format(key))
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise BridgeError("job.json の generation が整数ではありません")
    if quality not in QUALITY_VALUES:
        raise BridgeError("job.json の quality は 'final' または 'live' です")
    if channels is None:
        channels = [dict(ch) for ch in DEFAULT_CHANNELS]
    if not isinstance(channels, list) or not channels:
        raise BridgeError("job.json の channels が空です")
    job = {
        "schema": SCHEMA,
        "job_id": str(job_id),
        "generation": int(generation),
        "ts_name": str(ts_name),
        "quality": str(quality),
        "baseline_dir": str(baseline_dir),
        "current_snapshot_dir": str(current_snapshot_dir),
        "source_dir": str(source_dir),
        "output_dir": str(output_dir),
        "uvremap_root": str(uvremap_root),
        "udim": bool(udim),
        "out_res": out_res,
        "dilate_px": int(dilate_px),
        "normal_mode": str(normal_mode),
        "channels": [dict(ch) for ch in channels],
        "timeout_s": timeout_s,
        "copy_retries": int(copy_retries),
    }
    if source_flag is not None:
        job["source_flag"] = str(source_flag)
    for key, value in extra.items():
        if key not in job:
            job[key] = value
    return job


def write_job(job, path) -> Path:
    """job.json を一時ファイルへ書いてから ``os.replace`` する(原子的)。"""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name("{0}.{1}.tmp".format(dest.name, os.getpid()))
    try:
        data = json.dumps(job, ensure_ascii=False, indent=2).encode("utf-8")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    return dest


def decide_display_state(ledger_entry, current_hash, decision, *, enabled) -> tuple[str, str]:
    """パネルの表示状態を決める(契約 §6 D-1 / D-3 / D-4)。

    優先順:
        1. enabled が False → ORIGINAL(D-4 キルスイッチ。ジョブを作らず原本)
        2. decision.method == "STOP" → BLOCKED(理由を日本語で)
        3. ledger に当該 TS が無い → ORIGINAL
        4. ledger の uv_hash が current_hash と一致 → CONVERTED_OK
        5. 不一致 → STALE
    current_hash は ``current_uv_hash``(TS 単位)であること(E4)。
    STALE は「直前の正常世代の UV ハッシュが現行と一致しない」。
    ファイルノードを原本へ戻す判断(D-3)は B2 がこの状態を見て行う。
    """
    if not enabled:
        return "ORIGINAL", "キルスイッチがオフのため原本を表示します"
    method = _decision_method(decision)
    if method == "STOP":
        reasons = _decision_reasons(decision)
        if not reasons:
            return "BLOCKED", "変換を停止しました"
        if len(reasons) == 1 and reasons[0] in _STOP_MESSAGES:
            return "BLOCKED", _STOP_MESSAGES[reasons[0]]
        mapped = [_STOP_MESSAGES.get(r, r) for r in reasons]
        return "BLOCKED", "変換を停止しました(理由: {0})".format("、".join(mapped))
    if not isinstance(ledger_entry, dict) or "uv_hash" not in ledger_entry:
        return "ORIGINAL", "未変換のため原本を表示します"
    if ledger_entry.get("uv_hash") == current_hash:
        return "CONVERTED_OK", ""
    return "STALE", "現行UVが直前の正常世代と一致しないため再変換が必要です"


def update_status_entry(uvremap_root, ts_name, state, generation, message, *, now_fn=None):
    """status.json の当該 TS 行だけを更新する(他 TS の行は保持する)。

    ワーカーの ``_Run.set_status`` と同じ ``FileLock(status.lock)`` で
    読み→更新→原子置換を排他する。``updated`` はナイーブな現地時刻の
    ISO8601。``now_fn`` で注入できる(時計に依存するテストを避ける)。
    """
    root = _as_path(uvremap_root, "uvremap_root")
    if not ts_name:
        raise BridgeError("ts_name が空です")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise BridgeError("generation が整数ではありません")
    stamp = (now_fn or _naive_now_iso)()
    root.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(root / "status.lock", STATUS_LOCK_TIMEOUT_S, label="status"):
            data = read_json(root / "status.json", None)
            if not isinstance(data, dict):
                data = {"schema": SCHEMA, "ts": {}}
            data.setdefault("schema", SCHEMA)
            table = data.setdefault("ts", {})
            if not isinstance(table, dict):
                table = data["ts"] = {}
            prev = table.get(ts_name) if isinstance(table.get(ts_name), dict) else {}
            row = dict(prev)
            row.update({
                "state": str(state),
                "generation": int(generation),
                "updated": stamp,
                "message": "" if message is None else str(message),
            })
            table[ts_name] = row
            write_json_atomic(root / "status.json", data)
    except LockTimeout as exc:
        raise BridgeError(str(exc)) from exc
    return root / "status.json"
