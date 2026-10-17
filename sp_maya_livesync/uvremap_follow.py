# -*- coding: utf-8 -*-
"""UV再マップ Phase 2 の判定(純関数)。契約 ``docs/uvremap/CONTRACT_PHASE2.md`` v0.2。

Maya・Qt には依存しない。標準ライブラリと numpy、既存の ``uvremap_io.replace_file``、
ワーカーから切り出した ``match_source_name`` だけを使う。
現在時刻は読まない。時刻は引数で受け取る。

契約が決めていない点は ``docs/uvremap/QUESTIONS_phase2.md`` に書いてある。
このモジュールは follow.json の ``flag_mtime_ns`` を自分では更新しない。
"""
from __future__ import annotations

import json
import math
import os
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import uvremap_io
from uvremap_worker import match_source_name

__all__ = (
    "match_source_name",
    "FOLLOW_SCHEMA",
    "FOLLOW_FILENAME",
    "STATE_CONVERTED_OK",
    "STATE_STALE",
    "STATE_CONVERTING",
    "STATE_ORIGINAL",
    "STATE_FAILED",
    "STATE_BLOCKED",
    "MSG_PAINTER_START_FMT",
    "MSG_PEND_FMT",
    "MSG_QUEUE_FMT",
    "MSG_NO_CHANGE",
    "MSG_INPUT_STALE",
    "MSG_UV_STALE",
    "MSG_AUTO_INPUT_AS_IS",
    "LABEL_FOLD_OFF",
    "LABEL_FOLD_BUSY",
    "LABEL_NEEDS_APPLY",
    "LABEL_AUTO_FOLLOW",
    "LABEL_FOLD_ON",
    "LABEL_APPLY",
    "LABEL_CHECK_PAINTER",
    "LABEL_CHECK_UV",
    "REASON_FLAG_SAME",
    "REASON_SETTLING",
    "REASON_DISABLED",
    "REASON_AUTO_OFF",
    "REASON_NO_BASELINE",
    "REASON_NO_TARGET",
    "REASON_UV_UNCHANGED",
    "REASON_UV_SETTLING",
    "FollowDecision",
    "UVFollowDecision",
    "empty_follow",
    "source_signature",
    "load_follow",
    "save_follow",
    "normalize_follow_settings",
    "decide_follow",
    "uv_fingerprint",
    "decide_uv_follow",
    "should_skip_auto",
    "input_stale",
    "fold_status_label_p2",
)

FOLLOW_SCHEMA = 1
FOLLOW_FILENAME = "follow.json"
_NS = 1_000_000_000

STATE_CONVERTED_OK = "CONVERTED_OK"
STATE_STALE = "STALE"
STATE_CONVERTING = "CONVERTING"
STATE_ORIGINAL = "ORIGINAL"
STATE_FAILED = "FAILED"
STATE_BLOCKED = "BLOCKED"
_AUTO_OK_STATES = (STATE_CONVERTED_OK, STATE_STALE)

# --- 契約 P2-5b / P2-6 / P2-6b / P2-8 / P2-9 の文言(原文のまま) ---
MSG_PAINTER_START_FMT = "Painter の書き出しを検知しました。変換を開始します({ts})"
MSG_PEND_FMT = "変換中のため、完了後にもう一度変換します({ts})"
MSG_QUEUE_FMT = "順番待ちです({ts})"
MSG_NO_CHANGE = "変更はありません"
MSG_INPUT_STALE = "Painter の書き出しが新しくなりました。『反映する』を押してください"
MSG_UV_STALE = "UV が変更されました。『反映する』を押してください"
MSG_AUTO_INPUT_AS_IS = "自動変換の入力は Painter の書き出し結果をそのまま使います"

LABEL_FOLD_OFF = "無効"
LABEL_FOLD_BUSY = "変換中"
LABEL_NEEDS_APPLY = "要反映"
LABEL_AUTO_FOLLOW = "自動追従"
LABEL_FOLD_ON = "有効"
LABEL_APPLY = "変更を反映する"
LABEL_CHECK_PAINTER = "Painter の書き出しに自動で追従する"
LABEL_CHECK_UV = "UV の編集が落ち着いたら自動で反映する"

# decide_* の reason。契約に文面は無い(QUESTIONS_phase2.md)。
REASON_FLAG_SAME = "印は更新されていません"
REASON_SETTLING = "印の落ち着き待ちです"
REASON_DISABLED = "UV変換は無効です"
REASON_AUTO_OFF = "自動追従はオフです"
REASON_NO_BASELINE = "基準UVがありません"
REASON_NO_TARGET = "再変換するテクスチャセットがありません"
REASON_UV_UNCHANGED = "UV の変更はありません"
REASON_UV_SETTLING = "UV の落ち着き待ちです"

_DEFAULT_AUTO_FOLLOW = False
_DEFAULT_AUTO_FOLLOW_UV = False
_DEFAULT_MAX_PARALLEL = 2
_DEFAULT_FOLLOW_SETTLE_S = 2.0
_DEFAULT_UV_SETTLE_S = 3.0
_DEFAULT_UV_POLL_S = 2.0


@dataclass(frozen=True)
class FollowDecision:
    """Painter 追従の判定。``ready`` は印が落ち着いたか。"""

    ready: bool
    ts_to_convert: tuple[str, ...]
    ts_to_pend: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class UVFollowDecision:
    """UV 追従の判定。``ts_to_stale`` は STALE にする TS(名前の昇順)。"""

    ts_to_stale: tuple[str, ...]
    ts_to_convert: tuple[str, ...]
    reason: str


def empty_follow() -> dict:
    """follow.json が無い・壊れている・schema が違うときの既定。"""
    return {"schema": FOLLOW_SCHEMA, "flag_mtime_ns": None, "ts": {}}


def source_signature(source_dir, ts_name, channels, os_name=None) -> dict:
    """入力フォルダを読むだけで、TS に一致するファイルごとの ``[size, mtime_ns]`` を返す。

    照合は ``match_source_name``。チャンネルは job の ``channels``(各要素の ``suffix``)。
    ワーカーが変換時に行う「拡張子やタイル有無の選別」はしない。一致した通常ファイルを
    すべて載せる。フォルダが無い、または読めないときは ``{}``(例外は出さない)。
    個々のファイルの stat に失敗した場合とその場がファイルでない場合は、その名前だけ飛ばす。
    """
    try:
        names = os.listdir(source_dir)
    except OSError:
        return {}
    suffixes = [ch["suffix"] for ch in channels]
    found = {}
    root = Path(source_dir)
    for name in sorted(names):
        if not any(match_source_name(ts_name, suffix, name, os_name) is not None for suffix in suffixes):
            continue
        path = root / name
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        found[name] = [int(st.st_size), int(st.st_mtime_ns)]
    return found


def _follow_path(uvremap_root) -> Path:
    return Path(uvremap_root) / FOLLOW_FILENAME


def load_follow(uvremap_root) -> dict:
    """``uvremap_root/follow.json`` を読む。失敗と schema 違いでは ``empty_follow()``。"""
    path = _follow_path(uvremap_root)
    try:
        raw = path.read_bytes()
    except OSError:
        return empty_follow()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return empty_follow()
    schema = data.get("schema") if isinstance(data, dict) else None
    if not isinstance(data, dict) or isinstance(schema, bool) or schema != FOLLOW_SCHEMA:
        return empty_follow()
    if "ts" in data and not isinstance(data["ts"], dict):
        return empty_follow()
    return data


def save_follow(uvremap_root, data) -> None:
    """follow.json を一時ファイルへ書き、``uvremap_io.replace_file`` で置き換える。

    ロックファイルは作らない(名前と待ち時間は契約に無い)。
    """
    path = _follow_path(uvremap_root)
    tmp = path.with_name("{0}.{1}.tmp".format(path.name, os.getpid()))
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        uvremap_io.replace_file(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _clamp_number(value, default: float, lo: float, hi: float) -> float:
    """float 項目。JSON の整数(小数点なし)も数値として受ける。bool は既定値。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float):
        number = value
    else:
        number = float(value)
    if not math.isfinite(number):
        return default
    if number < lo:
        return float(lo)
    if number > hi:
        return float(hi)
    if isinstance(value, float):
        return value
    return number


def normalize_follow_settings(cfg: dict) -> dict:
    """P2-2 と P2-6 の 6 キーを返す。型の違いは既定値、範囲外は丸める。

    float 項目は int も受ける(QUESTIONS_phase2.md)。``cfg`` が dict でなければ全既定。
    """
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "uvremap_auto_follow": _as_bool(cfg.get("uvremap_auto_follow"), _DEFAULT_AUTO_FOLLOW),
        "uvremap_auto_follow_uv": _as_bool(cfg.get("uvremap_auto_follow_uv"), _DEFAULT_AUTO_FOLLOW_UV),
        "uvremap_max_parallel": _clamp_int(
            cfg.get("uvremap_max_parallel"), _DEFAULT_MAX_PARALLEL, 1, 4),
        "uvremap_follow_settle_s": _clamp_number(
            cfg.get("uvremap_follow_settle_s"), _DEFAULT_FOLLOW_SETTLE_S, 0.5, 30.0),
        "uvremap_uv_settle_s": _clamp_number(
            cfg.get("uvremap_uv_settle_s"), _DEFAULT_UV_SETTLE_S, 1.0, 60.0),
        "uvremap_uv_poll_s": _clamp_number(
            cfg.get("uvremap_uv_poll_s"), _DEFAULT_UV_POLL_S, 1.0, 30.0),
    }


def _signature_rows(sig):
    """``source_sig`` の比較キー。``None`` は未記録。list と tuple は同じ値として扱う。"""
    if sig is None:
        return None
    if not isinstance(sig, dict):
        return ("bad", sig)
    rows = []
    for name in sorted(sig, key=str):
        val = sig[name]
        if isinstance(val, (list, tuple)) and not isinstance(val, str) and len(val) == 2:
            rows.append((str(name), (val[0], val[1])))
        elif isinstance(val, (str, int, float)) or val is None:
            rows.append((str(name), ("raw", val)))
        else:
            rows.append((str(name), ("raw", repr(val))))
    return tuple(rows)


def _sigs_equal(left, right) -> bool:
    return _signature_rows(left) == _signature_rows(right)


def _flag_is_newer(flag_mtime_ns, last_flag_ns) -> bool:
    if isinstance(flag_mtime_ns, bool) or not isinstance(flag_mtime_ns, int):
        return False
    if last_flag_ns is None:
        return True
    if isinstance(last_flag_ns, bool) or not isinstance(last_flag_ns, int):
        return False
    return flag_mtime_ns > last_flag_ns


def _settled_ns(now_ns, last_change_seen_ns, settle_s) -> bool:
    if isinstance(now_ns, bool) or not isinstance(now_ns, int):
        return False
    if isinstance(last_change_seen_ns, bool) or not isinstance(last_change_seen_ns, int):
        return False
    if isinstance(settle_s, bool) or not isinstance(settle_s, (int, float)):
        return False
    return (now_ns - last_change_seen_ns) >= (float(settle_s) * _NS)


def _scene_matches(same_scene, ts_name: str) -> bool:
    """``same_scene`` は TS 名 → bool、または全 TS 共通の bool。"""
    if isinstance(same_scene, bool):
        return same_scene
    if isinstance(same_scene, dict):
        return same_scene.get(ts_name) is True
    return False


def decide_follow(
    now_ns, flag_mtime_ns, last_flag_ns, last_change_seen_ns, settle_s,
    enabled, auto_follow, has_baseline, ts_states, current_sigs, recorded_sigs,
    same_scene,
) -> FollowDecision:
    """P2-4。時刻はナノ秒。``settle_s`` は秒。

    ``ready`` は、印の mtime が記録より新しく、かつ最後の変化から ``settle_s`` 秒
    経っているときだけ True。条件 1〜2 で対象が無くても、落ち着いていれば True。
    条件 3 の ``CONVERTING`` は、条件 1・2・4・5 を満たすとき ``ts_to_pend``。
    名前は昇順。この関数は follow.json を更新しない。
    """
    if not _flag_is_newer(flag_mtime_ns, last_flag_ns):
        return FollowDecision(False, (), (), REASON_FLAG_SAME)
    if not _settled_ns(now_ns, last_change_seen_ns, settle_s):
        return FollowDecision(False, (), (), REASON_SETTLING)
    if enabled is not True:
        return FollowDecision(True, (), (), REASON_DISABLED)
    if auto_follow is not True:
        return FollowDecision(True, (), (), REASON_AUTO_OFF)
    if has_baseline is not True:
        return FollowDecision(True, (), (), REASON_NO_BASELINE)

    states = ts_states if isinstance(ts_states, dict) else {}
    current = current_sigs if isinstance(current_sigs, dict) else {}
    recorded = recorded_sigs if isinstance(recorded_sigs, dict) else {}
    convert = []
    pend = []
    for ts_name in sorted((name for name in states if isinstance(name, str))):
        if not _scene_matches(same_scene, ts_name):
            continue
        if _sigs_equal(current.get(ts_name), recorded.get(ts_name)):
            continue
        state = states.get(ts_name)
        if state in _AUTO_OK_STATES:
            convert.append(ts_name)
        elif state == STATE_CONVERTING:
            pend.append(ts_name)
    if not convert and not pend:
        return FollowDecision(True, (), (), REASON_NO_TARGET)
    return FollowDecision(True, tuple(convert), tuple(pend), "")


def uv_fingerprint(u_values, v_values) -> tuple[int, int]:
    """U・V を float32 で詰めたバイト列の ``zlib.crc32`` と UV 数。

    バイト列は U の後に V(numpy の native endian、C 連続)。長さが違うときは ``ValueError``。
    """
    u_arr = np.ascontiguousarray(np.asarray(u_values, dtype=np.float32).reshape(-1))
    v_arr = np.ascontiguousarray(np.asarray(v_values, dtype=np.float32).reshape(-1))
    if int(u_arr.shape[0]) != int(v_arr.shape[0]):
        raise ValueError("U と V の長さが違います")
    packed = np.concatenate((u_arr, v_arr))
    crc = zlib.crc32(packed.tobytes()) & 0xFFFFFFFF
    return (int(crc), int(u_arr.shape[0]))


def _settled_s(now, last_change_at, settle_s) -> bool:
    """``now`` と ``last_change_at`` は秒。``last_change_at`` が無いときは未了。"""
    if last_change_at is None or now is None:
        return False
    if isinstance(now, bool) or isinstance(last_change_at, bool) or isinstance(settle_s, bool):
        return False
    if not isinstance(now, (int, float)) or not isinstance(last_change_at, (int, float)):
        return False
    if not isinstance(settle_s, (int, float)):
        return False
    if not math.isfinite(float(now)) or not math.isfinite(float(last_change_at)):
        return False
    if not math.isfinite(float(settle_s)):
        return False
    return (float(now) - float(last_change_at)) >= float(settle_s)


def _shape_members(value) -> list[str]:
    if isinstance(value, (list, tuple)) and not isinstance(value, str):
        return [ts for ts in value if isinstance(ts, str)]
    return []


def decide_uv_follow(
    now, fingerprints_now, fingerprints_recorded, last_change_at, settle_s,
    ts_by_shape, ts_states, enabled, auto_follow_uv,
) -> UVFollowDecision:
    """P2-6 / P2-6b。指紋のマップはシェイプ名 → 指紋。時刻は秒。

    指紋が記録と違うシェイプに属する TS をすべて ``ts_to_stale`` に入れる。
    自動変換は ``auto_follow_uv`` が真で、指紋の最終変化から ``settle_s`` 秒経ったときだけ。
    対象は、状態が ``CONVERTED_OK`` または ``STALE`` の TS(今回 STALE にするものを含む)。
    ``enabled`` が真でないときは何もしない。
    """
    if enabled is not True:
        return UVFollowDecision((), (), REASON_DISABLED)

    shapes = ts_by_shape if isinstance(ts_by_shape, dict) else {}
    states = ts_states if isinstance(ts_states, dict) else {}
    now_map = fingerprints_now if isinstance(fingerprints_now, dict) else {}
    recorded_map = fingerprints_recorded if isinstance(fingerprints_recorded, dict) else {}
    stale = []
    for shape in sorted(shapes, key=str):
        current = now_map.get(shape, None)
        recorded = recorded_map.get(shape, None)
        if current is None or recorded is None or current == recorded:
            continue
        for ts_name in _shape_members(shapes.get(shape)):
            stale.append(ts_name)
    stale_names = tuple(sorted(set(stale)))

    settled = _settled_s(now, last_change_at, settle_s)
    convert: list[str] = []
    if auto_follow_uv is True and settled:
        chosen = []
        for ts_name, state in states.items():
            if isinstance(ts_name, str) and state == STATE_STALE:
                chosen.append(ts_name)
        for ts_name in stale_names:
            if states.get(ts_name) in _AUTO_OK_STATES:
                chosen.append(ts_name)
        convert = sorted(set(chosen))

    if convert:
        reason = ""
    elif stale_names and auto_follow_uv is True and not settled:
        reason = REASON_UV_SETTLING
    elif stale_names:
        reason = ""
    else:
        reason = REASON_UV_UNCHANGED
    return UVFollowDecision(stale_names, tuple(convert), reason)


def should_skip_auto(uv_hash_now, uv_hash_last_good, sig_now, sig_recorded) -> bool:
    """P2-5b。UV ハッシュと source_sig が両方、記録と一致するときだけ True。"""
    return uv_hash_now == uv_hash_last_good and _sigs_equal(sig_now, sig_recorded)


def input_stale(state, sig_now, sig_recorded, auto_follow) -> bool:
    """P2-6 入力側。``CONVERTED_OK`` かつ sig が違い、Painter 追従がオフのとき True。"""
    if auto_follow is True:
        return False
    if state != STATE_CONVERTED_OK:
        return False
    return not _sigs_equal(sig_now, sig_recorded)


def fold_status_label_p2(enabled, converting, needs_apply, auto_any) -> str:
    """閉じた欄の短い状態。無効 > 変換中 > 要反映 > 自動追従 > 有効。"""
    if not enabled:
        return LABEL_FOLD_OFF
    if converting:
        return LABEL_FOLD_BUSY
    if needs_apply:
        return LABEL_NEEDS_APPLY
    if auto_any:
        return LABEL_AUTO_FOLLOW
    return LABEL_FOLD_ON
