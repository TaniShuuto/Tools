# -*- coding: utf-8 -*-
"""Maya 側の UV 再マップ実行制御(基準記録・ワーカー起動・ファイルノード切替)。

契約: docs/uvremap/CONTRACT.md §2〜§6、D-3 / D-4。
Maya の import は関数の中だけ。cmds / Popen / 配列収集は差し替え可能。
Live Sync 本体(maya_live_sync.py)への変更は PR #18 で必要最小限に抑えた。
本モジュールは実行制御を担当する。
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import uvremap_bridge as br
import uvremap_core as core
import uvremap_follow as follow
from uvremap_worker import (
    FileLock,
    STATUS_LOCK_TIMEOUT_S,
    is_windows_reserved_name,
    read_json,
    write_json_atomic,
)

SCHEMA = 1
ATTR_ORIGINAL_PATH = "uvremapOriginalPath"
ATTR_ORIGINAL_TILING = "uvremapOriginalTiling"
ATTR_TS_NAME = "uvremapTsName"
UV_TILING_OFF = 0
UV_TILING_UDIM = 3
STATE_ORIGINAL = "ORIGINAL"
STATE_CONVERTING = "CONVERTING"
STATE_CONVERTED_OK = "CONVERTED_OK"
STATE_STALE = "STALE"
STATE_FAILED = "FAILED"
STATE_BLOCKED = "BLOCKED"
MSG_NO_BASELINE = "基準UVが未記録です。Painterへ送った時点で記録してください"
MSG_KILLSWITCH = "UV変換が無効のため原本を表示します"
MSG_NO_MAYAPY = (
    "変換用の mayapy が見つかりません。Maya のインストール先の bin フォルダに mayapy.exe があるか確認してください"
)
MSG_NO_FILE_NODES = "変換は完了しましたが、切り替え対象のファイルノードが見つかりません"
MSG_SCENE_CHANGED = (
    "シーンが変わったため切り替えを見送りました。もう一度『今すぐ変換』を押してください"
)
MSG_INTERRUPTED = (
    "前回の変換は途中で中断されました。もう一度『今すぐ変換』を押してください"
)
MSG_ALREADY_RUNNING = "このテクスチャセットは変換中です"
MSG_QUEUED = "順番待ち"
MSG_WORKER_CRASH = "ワーカーが異常終了しました(終了コード {0})"
# 2026.10.19(RB-b): リベース完了処理の履歴文言。
MSG_REBASE_HISTORY_DONE = "リベースが完了しました"
MSG_REBASE_HISTORY_PARTIAL_FMT = "リベースの完了処理が途中で止まりました(未完了の段階: {step})"
# 契約に無い。仮。完了の印は rebase/ の外(Painter は読まない・書かない)。
REBASE_LOCAL_NAME = "rebase_local.json"
_CANDIDATE_ID_ATTEMPTS = 8
_REBASE_STEP_NAMES = ("active", "reverted", "follow_reset", "history")
_SAFE_BASELINE_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,79}$")
_ABS_WIN_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"']+")
_ABS_POSIX_RE = re.compile(r"(?<![0-9A-Za-z_])/(?:[^/\s\"']+/)*[^/\s\"']+")
ORIGIN_MANUAL = "manual"
ORIGIN_AUTO = "auto"
MAX_PARALLEL_DEFAULT = 2
MAX_PARALLEL_MIN = 1
MAX_PARALLEL_MAX = 4
_STATUS_TO_EXIT = {
    "ok": 0,
    "superseded": 10,
    "blocked": 11,
    "failed": 12,
    "retry_exhausted": 13,
}
WARN_NO_FILE_NODES = "no_file_nodes_switched"
WARN_FILE_MISSING = "converted_file_missing"
WARN_SCENE_CHANGED = "scene_changed"
WARN_DISABLED = "uvremap_disabled"
WORKER_SCRIPT = Path(__file__).resolve().parent / "uvremap_worker.py"
ACTIVE_BASELINE_NAME = "active.json"
LATEST_GENERATION_DIR = "latest_generation"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
FAIL_EXIT_CODES = (11, 12, 13)
_TILE_STEM_RE = re.compile(r"^(.+)\.(\d{4})$")
_UNSAFE_TS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
KNOWN_WORKER_EXITS = (0, 10, 11, 12, 13)
TERMINAL_STATES = (
    STATE_CONVERTED_OK, STATE_FAILED, STATE_BLOCKED, STATE_ORIGINAL,
)
_STRIP_ENV = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
)


class RuntimeError_(Exception):
    """実行制御の入力や前提が満たされない。"""


# Maya の RuntimeError と名前がぶつからないように公開名を分ける
RuntimeControlError = RuntimeError_


def _naive_now_iso() -> str:
    """ナイーブな現地時刻の ISO8601(秒まで)。now_fn 未注入時の既定。"""
    return datetime.datetime.now().isoformat(timespec="seconds")


def _as_path(value, what: str) -> Path:
    if value is None or value == "":
        raise RuntimeControlError("{0} が空です".format(what))
    return Path(value)


def _as_iso(value) -> str:
    if value is None:
        return _naive_now_iso()
    if hasattr(value, "isoformat"):
        try:
            return value.replace(tzinfo=None).isoformat(timespec="seconds")
        except TypeError:
            return value.isoformat(timespec="seconds")
    return str(value)


def _stamp_from_iso(iso: str) -> str:
    digits = re.sub(r"[^0-9]", "", iso)
    if len(digits) >= 14:
        return digits[:8] + "-" + digits[8:14]
    return digits or "0"


def worker_env(base_env=None) -> dict:
    """Maya GUI 由来の Python 変数を外し、mayapy が壊れない環境を返す。"""
    env = dict(os.environ if base_env is None else base_env)
    for key in _STRIP_ENV:
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def resolve_mayapy(mayapy_path=None, executable=None):
    """mayapy のパス。明示指定を優先し、無ければ sys.executable の隣を探す。"""
    if mayapy_path:
        path = Path(mayapy_path)
        if path.is_file():
            return str(path)
        return None
    exe = Path(executable or sys.executable)
    names = []
    if os.name == "nt":
        names.append("mayapy.exe")
    else:
        names.append("mayapy")
    if exe.name.lower() in ("mayapy", "mayapy.exe"):
        return str(exe)
    for name in names:
        cand = exe.with_name(name)
        if cand.is_file():
            return str(cand)
    return None


def _basename(path) -> str:
    return os.path.basename(os.fspath(path) if path is not None else "")


def parse_output_name(ts_name: str, name: str):
    """出力ファイル名から (suffix, tile or None, ext) を取る。合わなければ None。"""
    if not ts_name or not name:
        return None
    prefix = "{0}_".format(ts_name)
    base = _basename(name)
    if not base.startswith(prefix):
        return None
    rest = base[len(prefix):]
    stem, ext = os.path.splitext(rest)
    if not stem or not ext:
        return None
    matched = _TILE_STEM_RE.match(stem)
    if matched:
        return matched.group(1), int(matched.group(2)), ext
    return stem, None, ext


def suffixes_from_files(ts_name: str, files) -> dict:
    """result.json の files を suffix → {tiles, ext} にまとめる。"""
    grouped = {}
    for entry in files or []:
        if not isinstance(entry, dict):
            continue
        parsed = parse_output_name(ts_name, entry.get("name") or "")
        if parsed is None:
            continue
        suffix, tile, ext = parsed
        slot = grouped.setdefault(suffix, {"tiles": set(), "ext": ext})
        slot["ext"] = ext
        if tile is None:
            tile = entry.get("tile")
        if tile is None:
            tile = 1001
        slot["tiles"].add(int(tile))
    return grouped


def converted_filename(ts_name: str, suffix: str, ext: str, n_tiles: int) -> str:
    """契約 §4.1: 1タイルはタイル番号なし、複数は <UDIM> トークン。"""
    if n_tiles <= 1:
        return "{0}_{1}{2}".format(ts_name, suffix, ext)
    return "{0}_{1}.<UDIM>{2}".format(ts_name, suffix, ext)


def _filename_matches_suffix(filename: str, ts_name: str, suffix: str) -> bool:
    prefix = "{0}_{1}".format(ts_name, suffix)
    base = _basename(filename)
    if not base.startswith(prefix):
        return False
    if len(base) == len(prefix):
        return True
    return base[len(prefix)] in ".<"


def _filename_matches_ts(filename: str, ts_name: str) -> bool:
    return _basename(filename).startswith("{0}_".format(ts_name))


def latest_generation_filename(ts_name: str) -> str:
    """TS名を ``latest_generation/<name>.json`` 用のファイル名にする。

    使えない文字は ``_`` に置き、置き換えや予約名で元と違うときは
    元の UTF-8 の sha1 先頭8桁を付けて衝突を避ける。
    """
    raw = "" if ts_name is None else str(ts_name)
    safe = _UNSAFE_TS_RE.sub("_", raw).rstrip(" .")
    changed = safe != raw
    if not safe or safe in (".", "..") or is_windows_reserved_name(safe):
        safe = "ts_{0}".format(safe) if safe and safe not in (".", "..") else "ts"
        changed = True
    if len(safe) > 80:
        safe = safe[:80].rstrip(" .") or "ts"
        changed = True
    if changed:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        safe = "{0}__{1}".format(safe, digest)
    return safe + ".json"


def latest_generation_file(root, ts_name: str) -> Path:
    """``<root>/latest_generation/<safe_ts>.json``。"""
    return Path(root) / LATEST_GENERATION_DIR / latest_generation_filename(ts_name)


def _ledger_generation(ledger, ts_name: str) -> int:
    if not isinstance(ledger, dict):
        return 0
    table = ledger.get("ts")
    if not isinstance(table, dict):
        return 0
    entry = table.get(ts_name)
    if not isinstance(entry, dict):
        return 0
    gen = entry.get("generation")
    if isinstance(gen, bool) or not isinstance(gen, int):
        return 0
    return gen


def missing_converted_targets(ts_name, files, output_dir, exists_fn) -> list:
    """result.json の files のうち、出力先に無いものを {suffix, name} で返す。"""
    dest = Path(output_dir)
    missing = []
    for entry in files or []:
        if not isinstance(entry, dict):
            continue
        parsed = parse_output_name(ts_name, entry.get("name") or "")
        if parsed is None:
            continue
        suffix, _tile, _ext = parsed
        name = _basename(entry.get("name") or "")
        if not name:
            continue
        path = dest / name
        try:
            present = bool(exists_fn(os.fspath(path)))
        except Exception:
            present = False
        if not present:
            missing.append({"suffix": suffix, "name": name})
    return missing


def _plug(node: str, attr: str) -> str:
    return "{0}.{1}".format(node, attr)


def _cmds_ls_files(cmds) -> list:
    try:
        found = cmds.ls(type="file")
    except Exception:
        return []
    return list(found or [])


def _get_attr(cmds, node: str, attr: str, default=None):
    try:
        return cmds.getAttr(_plug(node, attr))
    except Exception:
        return default


def _has_attr(cmds, node: str, attr: str) -> bool:
    try:
        return bool(cmds.attributeQuery(attr, node=node, exists=True))
    except Exception:
        return False


def _ensure_string_attr(cmds, node: str, attr: str) -> None:
    if _has_attr(cmds, node, attr):
        return
    try:
        cmds.addAttr(node, ln=attr, dt="string")
    except TypeError:
        cmds.addAttr(node, longName=attr, dataType="string")


def _ensure_long_attr(cmds, node: str, attr: str) -> None:
    if _has_attr(cmds, node, attr):
        return
    try:
        cmds.addAttr(node, ln=attr, at="long")
    except TypeError:
        cmds.addAttr(node, longName=attr, attributeType="long")


def _delete_attr(cmds, node: str, attr: str) -> None:
    if not _has_attr(cmds, node, attr):
        return
    try:
        cmds.deleteAttr(node, at=attr)
        return
    except Exception:
        pass
    try:
        cmds.deleteAttr(_plug(node, attr))
    except Exception:
        pass


def _set_string(cmds, node: str, attr: str, value: str) -> None:
    cmds.setAttr(_plug(node, attr), value, type="string")


def _set_int(cmds, node: str, attr: str, value: int) -> None:
    cmds.setAttr(_plug(node, attr), int(value))


def _set_file_texture(cmds, node: str, path: str) -> None:
    """既存 Live Sync と同じく、一度空にしてからパスを書く。"""
    _set_string(cmds, node, "fileTextureName", "")
    _set_string(cmds, node, "fileTextureName", path)


def _node_path_candidates(cmds, node: str) -> list:
    paths = []
    current = _get_attr(cmds, node, "fileTextureName", "")
    if current:
        paths.append(str(current))
    if _has_attr(cmds, node, ATTR_ORIGINAL_PATH):
        original = _get_attr(cmds, node, ATTR_ORIGINAL_PATH, "")
        if original:
            paths.append(str(original))
    return paths


def _match_suffix(cmds, node: str, ts_name: str, suffixes: dict):
    ordered = sorted(suffixes.keys(), key=len, reverse=True)
    for path in _node_path_candidates(cmds, node):
        for suffix in ordered:
            if _filename_matches_suffix(path, ts_name, suffix):
                return suffix
    return None


def _record_original(cmds, node: str) -> None:
    if not _has_attr(cmds, node, ATTR_ORIGINAL_PATH):
        current = _get_attr(cmds, node, "fileTextureName", "")
        _ensure_string_attr(cmds, node, ATTR_ORIGINAL_PATH)
        _set_string(cmds, node, ATTR_ORIGINAL_PATH, "" if current is None else str(current))
    if not _has_attr(cmds, node, ATTR_ORIGINAL_TILING):
        tiling = _get_attr(cmds, node, "uvTilingMode", UV_TILING_OFF)
        try:
            tiling_i = int(tiling)
        except (TypeError, ValueError):
            tiling_i = UV_TILING_OFF
        _ensure_long_attr(cmds, node, ATTR_ORIGINAL_TILING)
        _set_int(cmds, node, ATTR_ORIGINAL_TILING, tiling_i)


def _without_undo(cmds, fn):
    """``setAttr`` 等を Undo に載せない。cmds が偽物/undoInfo が無いときはそのまま実行。"""
    undo_info = getattr(cmds, "undoInfo", None) if cmds is not None else None
    if not callable(undo_info):
        return fn()
    try:
        prev = undo_info(query=True, stateWithoutFlush=True)
    except Exception:
        return fn()
    try:
        undo_info(stateWithoutFlush=False)
    except Exception:
        return fn()
    try:
        return fn()
    finally:
        try:
            undo_info(stateWithoutFlush=prev)
        except Exception:
            pass


def switch_ts_file_nodes(
    cmds, ts_name: str, files, output_dir, exists_fn=None, missing_out=None,
) -> list:
    """当該 TS のファイルノードだけを変換後パスへ切り替える。

    属性名 ``uvremapOriginalPath`` / ``uvremapOriginalTiling`` は後続作業が
    依存するため変更しない。既に属性があるノードは元パスを上書きしない。
    切替時に ``uvremapTsName`` へ TS 名を書く。変換後ファイルが1つでも欠けている
    チャンネルのノードは切り替えない。
    """
    grouped = suffixes_from_files(ts_name, files)
    if not grouped:
        return []
    checker = exists_fn if exists_fn is not None else os.path.isfile
    missing = missing_converted_targets(ts_name, files, output_dir, checker)
    if missing_out is not None:
        missing_out.extend(missing)
    skip = {item["suffix"] for item in missing}
    dest_dir = Path(output_dir)

    def _apply():
        switched = []
        for node in _cmds_ls_files(cmds):
            suffix = _match_suffix(cmds, node, ts_name, grouped)
            if suffix is None or suffix in skip:
                continue
            info = grouped[suffix]
            n_tiles = len(info["tiles"])
            name = converted_filename(ts_name, suffix, info["ext"], n_tiles)
            new_path = str(dest_dir / name)
            tiling = UV_TILING_OFF if n_tiles <= 1 else UV_TILING_UDIM
            _record_original(cmds, node)
            _ensure_string_attr(cmds, node, ATTR_TS_NAME)
            _set_string(cmds, node, ATTR_TS_NAME, str(ts_name))
            _set_file_texture(cmds, node, new_path)
            _set_int(cmds, node, "uvTilingMode", tiling)
            switched.append(node)
        return switched

    return _without_undo(cmds, _apply)


def revert_ts_file_nodes(cmds, ts_name=None) -> list:
    """属性があるノードだけを原本へ戻し、属性を削除する。属性が無いノードは触らない。

    ``uvremapTsName`` があるノードは TS 名の完全一致。無い(D2a 当時の)ノードだけ
    ファイル名の ``<ts>_`` 前方一致を使う。
    """

    def _apply():
        reverted = []
        for node in _cmds_ls_files(cmds):
            if not _has_attr(cmds, node, ATTR_ORIGINAL_PATH) and not _has_attr(
                cmds, node, ATTR_ORIGINAL_TILING
            ):
                continue
            if ts_name:
                if _has_attr(cmds, node, ATTR_TS_NAME):
                    tagged = _get_attr(cmds, node, ATTR_TS_NAME, "")
                    if str(tagged) != str(ts_name):
                        continue
                elif not any(
                    _filename_matches_ts(path, ts_name) for path in _node_path_candidates(cmds, node)
                ):
                    continue
            original = _get_attr(cmds, node, ATTR_ORIGINAL_PATH, None)
            tiling = _get_attr(cmds, node, ATTR_ORIGINAL_TILING, UV_TILING_OFF)
            if original is not None:
                _set_file_texture(cmds, node, str(original))
            try:
                tiling_i = int(tiling)
            except (TypeError, ValueError):
                tiling_i = UV_TILING_OFF
            _set_int(cmds, node, "uvTilingMode", tiling_i)
            _delete_attr(cmds, node, ATTR_ORIGINAL_PATH)
            _delete_attr(cmds, node, ATTR_ORIGINAL_TILING)
            _delete_attr(cmds, node, ATTR_TS_NAME)
            reverted.append(node)
        return reverted

    return _without_undo(cmds, _apply)


def _default_cmds():
    import maya.cmds as cmds  # type: ignore
    return cmds


def _default_collect(shape_paths, ts_names, uv_set=None):
    import uvremap_maya as um
    return um.collect_arrays(shape_paths, ts_names, uv_set=uv_set)


def _default_scene_id():
    """現在のシーン識別子。未保存は空文字(未保存同士は区別できない)。"""
    try:
        import maya.cmds as cmds  # type: ignore
        name = cmds.file(query=True, sceneName=True)
    except Exception:
        return ""
    if not name:
        return ""
    return str(name)


def _list_baseline_ids(root: Path) -> list:
    base = root / "baseline"
    if not base.is_dir():
        return []
    ids = []
    for child in sorted(base.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if (child / "mesh.npz").is_file() and (child / "meta.json").is_file():
            ids.append(child.name)
    return ids


def _read_active_baseline(root: Path):
    data = read_json(root / "baseline" / ACTIVE_BASELINE_NAME, None)
    if not isinstance(data, dict):
        return None
    bid = data.get("baseline_id")
    if not bid:
        return None
    dest = root / "baseline" / str(bid)
    if (dest / "mesh.npz").is_file():
        return str(bid)
    return None


def _write_active_baseline(root: Path, baseline_id: str) -> None:
    dest = root / "baseline"
    dest.mkdir(parents=True, exist_ok=True)
    write_json_atomic(dest / ACTIVE_BASELINE_NAME, {
        "schema": SCHEMA,
        "baseline_id": str(baseline_id),
    })


def _load_ledger(root: Path) -> dict:
    data = read_json(root / "ledger.json", None)
    return data if isinstance(data, dict) else {}


def _last_good_uv_hash(root: Path, ts_name: str):
    entry = (_load_ledger(root).get("ts") or {}).get(ts_name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("uv_hash")
    if value is None or value == "":
        return None
    return str(value)


def _should_revert_d3(root: Path, ts_name: str, current_hash) -> bool:
    """直前の正常世代の uv_hash が現行と一致しなければ原本へ戻す(D-3)。"""
    last = _last_good_uv_hash(root, ts_name)
    if last is None:
        return True
    if current_hash is None or current_hash == "":
        return True
    return last != str(current_hash)


def _clamp_max_parallel(value) -> int:
    """同時実行の上限を 1〜4 に丸める。型が違う値は既定 2。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return MAX_PARALLEL_DEFAULT
    if value < MAX_PARALLEL_MIN:
        return MAX_PARALLEL_MIN
    if value > MAX_PARALLEL_MAX:
        return MAX_PARALLEL_MAX
    return value


def _exit_code_from_result(result):
    """result.json から終了コードを取る。読めなければ None(異常終了扱い)。"""
    if not isinstance(result, dict):
        return None
    code = result.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool):
        return int(code)
    status = str(result.get("status") or "")
    if status in _STATUS_TO_EXIT:
        return _STATUS_TO_EXIT[status]
    return None


def _normalize_origin(origin) -> str:
    if origin == ORIGIN_AUTO:
        return ORIGIN_AUTO
    return ORIGIN_MANUAL


def _is_safe_baseline_id(baseline_id) -> bool:
    """基準 ID としてパスに使ってよいか。英数字で始まり、英数字と - _ だけ、80 文字以内。"""
    if not isinstance(baseline_id, str) or not baseline_id or len(baseline_id) > 80:
        return False
    if "/" in baseline_id or "\\" in baseline_id or ".." in baseline_id:
        return False
    if any(ch.isspace() for ch in baseline_id):
        return False
    return _SAFE_BASELINE_ID_RE.fullmatch(baseline_id) is not None


def _remove_candidate_dir(dest: Path) -> None:
    """この呼び出しが作った候補フォルダを消す。失敗しても例外は出さない。"""
    try:
        if dest.is_symlink():
            dest.unlink()
            return
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
    except Exception:
        return


def _read_raw_active_baseline_id(root: Path):
    """active.json の生の baseline_id。指す先が無くても返す。読めなければ None。"""
    try:
        data = read_json(root / "baseline" / ACTIVE_BASELINE_NAME, None)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    bid = data.get("baseline_id")
    if isinstance(bid, str) and bid:
        return bid
    return None


def _resolved_baseline_child(root: Path, baseline_id: str):
    """baseline/<id> が baseline 直下の実ディレクトリなら、解決後のパスを返す。"""
    try:
        base = (root / "baseline").resolve()
        target = (base / baseline_id).resolve()
    except OSError:
        return None
    if target.name != baseline_id or target.parent != base:
        return None
    if not target.is_dir():
        return None
    return target


def _empty_rebase_steps() -> dict:
    return {name: False for name in _REBASE_STEP_NAMES}


def _require_nonempty_str(value, what: str) -> str:
    if not isinstance(value, str) or value == "":
        raise RuntimeControlError("{0} が空です".format(what))
    return value


def _require_ts_names(ts_names) -> list:
    if isinstance(ts_names, (str, bytes)) or not isinstance(ts_names, (list, tuple)):
        raise RuntimeControlError("ts_names が空です")
    names = []
    for name in ts_names:
        if not isinstance(name, str) or name == "":
            raise RuntimeControlError("ts_names が空です")
        names.append(name)
    if not names:
        raise RuntimeControlError("ts_names が空です")
    return names


def _scene_token(value) -> str:
    """None は空文字。シーン一致の比較用。"""
    if value is None:
        return ""
    return str(value)


def _rebase_error_text(exc: Exception) -> str:
    """例外の型と 1 行の要約。絶対パスは含めない。契約に無い。仮。"""
    name = type(exc).__name__
    try:
        raw = str(exc)
    except Exception:
        raw = ""
    line = raw.replace("\r", " ").replace("\n", " ").strip()
    line = _ABS_WIN_RE.sub("", line)
    line = _ABS_POSIX_RE.sub("", line)
    line = " ".join(line.split())
    if line:
        return "{0}: {1}".format(name, line)
    return name


def _rebase_lock(root: Path):
    """rebase_local.json 用。status と同じ FileLock。2026.10.19(RB-b)。"""
    return FileLock(root / "rebase_local.lock", STATUS_LOCK_TIMEOUT_S, label="rebase_local")


def _utc_file_stamp(epoch) -> str:
    """ファイル名に使う UTC 時刻。時計は引数と time.gmtime。"""
    try:
        when = float(epoch)
    except (TypeError, ValueError):
        when = 0.0
    if when < 0:
        when = 0.0
    clock = time.gmtime(when)
    return "{0:04d}{1:02d}{2:02d}T{3:02d}{4:02d}{5:02d}Z".format(
        clock.tm_year, clock.tm_mon, clock.tm_mday,
        clock.tm_hour, clock.tm_min, clock.tm_sec,
    )


def _quarantine_corrupt_rebase(root: Path, time_fn) -> None:
    """壊れた rebase_local.json を消さず退避する。契約に無い。仮。

    名前は ``rebase_local.json.corrupt-<UTC時刻>``。同じ秒に既にあれば
    連番を足す(既存の退避は上書きしない)。
    """
    src = root / REBASE_LOCAL_NAME
    if not src.is_file():
        return
    try:
        stamp = _utc_file_stamp(time_fn())
    except Exception:
        stamp = _utc_file_stamp(0)
    dest = root / "{0}.corrupt-{1}".format(REBASE_LOCAL_NAME, stamp)
    n = 2
    while dest.exists():
        dest = root / "{0}.corrupt-{1}-{2}".format(REBASE_LOCAL_NAME, stamp, n)
        n += 1
        if n > 1000:
            raise RuntimeControlError("壊れた記録の退避先を作れませんでした")
    src.replace(dest)


def _read_rebase_document(root: Path):
    """rebase_local.json。無いときは空の記録。壊れていたら None(記録なし)。

    ロックは呼び出し側が取る。
    """
    try:
        data = read_json(root / REBASE_LOCAL_NAME, None)
    except Exception:
        return None
    if data is None:
        return {"schema": SCHEMA, "requests": {}}
    if not isinstance(data, dict):
        return None
    requests = data.get("requests")
    if requests is None:
        return {"schema": SCHEMA, "requests": {}}
    if not isinstance(requests, dict):
        return None
    return data


def _coerce_rebase_row(row):
    """型が合う行だけ辞書にして返す。違えば None(その行は無視)。"""
    if not isinstance(row, dict):
        return None
    baseline_id = row.get("baseline_id")
    ts_names = row.get("ts_names")
    if not isinstance(baseline_id, str) or baseline_id == "":
        return None
    if not isinstance(ts_names, list) or not ts_names:
        return None
    if not all(isinstance(name, str) and name for name in ts_names):
        return None
    scene_id = row.get("scene_id")
    if scene_id is None:
        scene_id = ""
    elif not isinstance(scene_id, str):
        return None
    registered_at = row.get("registered_at")
    if registered_at is None:
        registered_at = ""
    elif not isinstance(registered_at, str):
        return None
    completed = row.get("completed", False)
    if not isinstance(completed, bool):
        return None
    completed_at = row.get("completed_at", None)
    if completed_at is not None and not isinstance(completed_at, str):
        return None
    steps_in = row.get("steps", {})
    if not isinstance(steps_in, dict):
        return None
    steps = _empty_rebase_steps()
    for name in _REBASE_STEP_NAMES:
        value = steps_in.get(name, False)
        if not isinstance(value, bool):
            return None
        steps[name] = value
    return {
        "baseline_id": baseline_id,
        "ts_names": list(ts_names),
        "scene_id": scene_id,
        "registered_at": registered_at,
        "completed": completed,
        "completed_at": completed_at,
        "steps": steps,
    }


def _copy_rebase_row(row: dict) -> dict:
    return {
        "baseline_id": row["baseline_id"],
        "ts_names": list(row["ts_names"]),
        "scene_id": row["scene_id"],
        "registered_at": row["registered_at"],
        "completed": row["completed"],
        "completed_at": row["completed_at"],
        "steps": dict(row["steps"]),
    }


def _lookup_rebase_row(root: Path, request_id):
    if not isinstance(request_id, str):
        return None
    with _rebase_lock(root):
        doc = _read_rebase_document(root)
    if doc is None:
        return None
    requests = doc.get("requests")
    if not isinstance(requests, dict):
        return None
    return _coerce_rebase_row(requests.get(request_id))


def _write_rebase_row(root: Path, request_id: str, row: dict, time_fn, *, only_if_absent=False) -> bool:
    """1 行を書く。``only_if_absent`` で有効な行が既にあれば何も書かず False。"""
    with _rebase_lock(root):
        doc = _read_rebase_document(root)
        if doc is None:
            _quarantine_corrupt_rebase(root, time_fn)
            doc = {"schema": SCHEMA, "requests": {}}
        requests = doc.get("requests")
        if not isinstance(requests, dict):
            requests = {}
        if only_if_absent and _coerce_rebase_row(requests.get(request_id)) is not None:
            return False
        requests[request_id] = _copy_rebase_row(row)
        write_json_atomic(root / REBASE_LOCAL_NAME, {
            "schema": SCHEMA,
            "requests": requests,
        })
        return True


def _notify_rebase_partial(history_fn, step: str) -> None:
    if history_fn is None:
        return
    try:
        history_fn("uvremap", MSG_REBASE_HISTORY_PARTIAL_FMT.format(step=step))
    except Exception:
        return


def _rebase_call_result(
    *, status, reason, request_id, baseline_id, ts_names, steps, reverted,
    failed_step, error,
) -> dict:
    steps_out = _empty_rebase_steps()
    if isinstance(steps, dict):
        for name in _REBASE_STEP_NAMES:
            steps_out[name] = bool(steps.get(name, False))
    return {
        "status": status,
        "reason": "" if reason is None else str(reason),
        "request_id": request_id,
        "baseline_id": "" if baseline_id is None else str(baseline_id),
        "ts_names": [str(name) for name in (ts_names or [])],
        "steps": steps_out,
        "reverted": [str(name) for name in (reverted or [])],
        "failed_step": failed_step,
        "error": error,
    }


class UVRemapRuntime:
    """基準UVの記録・ワーカー起動・ファイルノード切替・原本復帰。"""

    def __init__(
        self,
        get_enabled,
        get_root,
        now_fn=None,
        popen_fn=None,
        cmds_fn=None,
        mayapy_path=None,
        collect_arrays_fn=None,
        worker_script=None,
        exists_fn=None,
        scene_id_fn=None,
        max_parallel_fn=None,
        time_fn=None,
        mtime_fn=None,
        uv_fingerprints_fn=None,
    ):
        if get_enabled is None or not callable(get_enabled):
            raise RuntimeControlError("get_enabled がありません")
        if get_root is None or not callable(get_root):
            raise RuntimeControlError("get_root がありません")
        self.get_enabled = get_enabled
        self.get_root = get_root
        self.now_fn = now_fn or _naive_now_iso
        self.popen_fn = popen_fn or subprocess.Popen
        self.cmds_fn = cmds_fn or _default_cmds
        self.mayapy_path = mayapy_path
        self.collect_arrays_fn = collect_arrays_fn or _default_collect
        self.worker_script = Path(worker_script) if worker_script else WORKER_SCRIPT
        self.exists_fn = exists_fn if exists_fn is not None else os.path.isfile
        self.scene_id_fn = scene_id_fn if scene_id_fn is not None else _default_scene_id
        self.max_parallel_fn = max_parallel_fn
        self.time_fn = time_fn if time_fn is not None else time.time
        self.mtime_fn = mtime_fn if mtime_fn is not None else os.path.getmtime
        self.uv_fingerprints_fn = uv_fingerprints_fn
        self._seq = 0
        self._queue_seq = 0
        self._jobs = {}
        self._queue = []
        self._pending = {}
        self._recovered_events = []
        self._seen_roots = {}

    def _now(self) -> str:
        return _as_iso(self.now_fn())

    def _enabled_now(self) -> bool:
        """キルスイッチ。例外は OFF(安全側)。"""
        try:
            return bool(self.get_enabled())
        except Exception:
            return False

    def _scene_id(self) -> str:
        try:
            value = self.scene_id_fn()
        except Exception:
            return ""
        if value is None:
            return ""
        return str(value)

    def _next_id(self, prefix: str, iso: str) -> str:
        self._seq += 1
        return "{0}{1}-{2:04x}".format(prefix, _stamp_from_iso(iso), self._seq)

    def _cmds(self):
        cmds = self.cmds_fn()
        if cmds is None:
            raise RuntimeControlError("cmds_fn が cmds を返しませんでした")
        return cmds

    def _root(self, project_key) -> Path:
        root = _as_path(self.get_root(project_key), "uvremap_root")
        self._seen_roots[str(project_key)] = root
        return root

    def _collect(self, shape_paths, ts_names, uv_set=None):
        arrays, info = self.collect_arrays_fn(shape_paths, ts_names, uv_set)
        return arrays, info

    def _max_inflight_generation(self, ts_name: str) -> int:
        found = 0
        for rec in self._jobs.values():
            if rec.get("ts_name") != ts_name:
                continue
            gen = rec.get("generation")
            if isinstance(gen, int) and not isinstance(gen, bool) and gen > found:
                found = gen
        return found

    def _write_latest_generation(self, path: Path, ts_name: str, generation: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {
            "schema": SCHEMA,
            "generation": int(generation),
            "ts_name": str(ts_name),
        })

    def _point_inflight_jobs_at_latest(self, ts_name: str, latest_path: Path) -> None:
        dest = str(latest_path)
        for rec in self._jobs.values():
            if rec.get("ts_name") != ts_name:
                continue
            job_file = Path(rec["root"]) / "jobs" / rec["job_id"] / "job.json"
            data = read_json(job_file, None)
            if not isinstance(data, dict):
                continue
            if data.get("latest_generation_path") == dest:
                continue
            data["latest_generation_path"] = dest
            br.write_job(data, job_file)

    def _max_parallel(self) -> int:
        """起動判定のたびに呼び、1〜4 に丸める。例外・型違いは既定 2。"""
        fn = self.max_parallel_fn
        if fn is None or not callable(fn):
            return MAX_PARALLEL_DEFAULT
        try:
            raw = fn()
        except Exception:
            return MAX_PARALLEL_DEFAULT
        return _clamp_max_parallel(raw)

    def _running_worker_count(self) -> int:
        return len(self._jobs)

    def _next_queue_id(self) -> str:
        self._queue_seq += 1
        return "queued:{0}".format(self._queue_seq)

    def _status_generation(self, root: Path, ts_name: str) -> int:
        entry = self._status_row(root, ts_name)
        if entry is None:
            return 0
        gen = entry.get("generation", 0)
        if not isinstance(gen, int) or isinstance(gen, bool):
            return 0
        return gen

    def _set_status(self, root: Path, ts_name: str, state: str, generation: int, message: str, iso: str, queued=None):
        dest = Path(root)
        try:
            dest.mkdir(parents=True, exist_ok=True)
            with FileLock(dest / "status.lock", STATUS_LOCK_TIMEOUT_S, label="status"):
                data = read_json(dest / "status.json", None)
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
                    "updated": iso,
                    "message": "" if message is None else str(message),
                })
                if state != STATE_CONVERTING:
                    row.pop("queued", None)
                elif queued is True:
                    row["queued"] = True
                    row.pop("job_id", None)
                    row.pop("pid", None)
                elif queued is False:
                    row.pop("queued", None)
                table[ts_name] = row
                write_json_atomic(dest / "status.json", data)
                return
        except Exception:
            pass
        br.update_status_entry(
            root, ts_name, state, generation, message, now_fn=lambda: iso,
        )

    def _set_status_all(self, root: Path, state: str, message: str, iso: str, extra_ts=()):
        data = read_json(root / "status.json", None)
        names = set(extra_ts)
        if isinstance(data, dict) and isinstance(data.get("ts"), dict):
            names.update(str(name) for name in data["ts"])
        if not names:
            for name in extra_ts:
                self._set_status(root, name, state, 0, message, iso)
            return
        for name in names:
            entry = {}
            if isinstance(data, dict) and isinstance(data.get("ts"), dict):
                prev = data["ts"].get(name)
                if isinstance(prev, dict):
                    entry = prev
            gen = entry.get("generation", 0)
            if not isinstance(gen, int) or isinstance(gen, bool):
                gen = 0
            self._set_status(root, name, state, gen, message, iso)

    def _status_row(self, root: Path, ts_name: str):
        try:
            data = read_json(root / "status.json", None)
        except Exception:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("ts"), dict):
            return None
        entry = data["ts"].get(ts_name)
        return entry if isinstance(entry, dict) else None

    def _should_write_crash_status(self, root: Path, ts_name: str, gen: int) -> bool:
        """終端状態を同じ世代以上で書いていれば上書きしない。より新しい世代も残す。"""
        entry = self._status_row(root, ts_name)
        if entry is None:
            return True
        cur_gen = entry.get("generation", 0)
        if not isinstance(cur_gen, int) or isinstance(cur_gen, bool):
            cur_gen = 0
        if cur_gen > gen:
            return False
        state = str(entry.get("state") or "")
        if state in TERMINAL_STATES and cur_gen >= gen:
            return False
        return True

    def status_snapshot(self, project_key):
        """パネル(StatusModel)が読む status.json のパス。"""
        return self._root(project_key) / "status.json"

    def running_job_ids(self) -> list:
        return list(self._jobs)

    def has_running_job_for_ts(self, ts_name: str) -> bool:
        """同じ TS のジョブが ``_jobs`` にあれば True。待ち行列は含めない。"""
        want = str(ts_name) if ts_name is not None else ""
        if not want:
            return False
        for rec in self._jobs.values():
            if rec.get("ts_name") == want:
                return True
        return False

    def _job_id_for_ts(self, ts_name: str):
        want = str(ts_name) if ts_name is not None else ""
        if not want:
            return None
        for job_id, rec in self._jobs.items():
            if rec.get("ts_name") == want:
                return job_id
        return None

    def adopted_ts(self) -> list:
        """引き継ぎ中(プロセスを持たず result.json を待っている)TS 名の一覧。"""
        names = []
        for rec in self._jobs.values():
            if rec.get("adopted") and rec.get("ts_name"):
                names.append(str(rec["ts_name"]))
        return names

    def _queue_item_for_ts(self, ts_name: str):
        want = str(ts_name)
        for item in self._queue:
            if item.get("ts_name") == want:
                return item
        return None

    def _origin_rank(self, origin) -> int:
        return 1 if origin == ORIGIN_MANUAL else 0

    def _insert_queue(self, item: dict) -> None:
        rank = self._origin_rank(item.get("origin"))
        idx = len(self._queue)
        for i, other in enumerate(self._queue):
            if rank > self._origin_rank(other.get("origin")):
                idx = i
                break
        self._queue.insert(idx, item)

    def _make_request(
        self, project_key, ts_name, shape_paths, source_dir, output_dir,
        quality, channels, origin, opts,
    ) -> dict:
        paths = list(shape_paths) if shape_paths is not None else []
        return {
            "project_key": project_key,
            "ts_name": str(ts_name),
            "shape_paths": paths,
            "source_dir": source_dir,
            "output_dir": output_dir,
            "quality": quality,
            "channels": channels,
            "origin": origin,
            "opts": dict(opts or {}),
            "scene_id": self._scene_id(),
        }

    def _merge_request(self, dest: dict, src: dict) -> None:
        upgraded = (
            src.get("origin") == ORIGIN_MANUAL and dest.get("origin") != ORIGIN_MANUAL
        )
        if src.get("origin") == ORIGIN_MANUAL:
            dest["origin"] = ORIGIN_MANUAL
        for key in (
            "project_key", "shape_paths", "source_dir", "output_dir",
            "quality", "channels", "opts", "scene_id",
        ):
            if key in src:
                dest[key] = src[key]
        if upgraded and dest in self._queue:
            self._queue.remove(dest)
            self._insert_queue(dest)

    def _enqueue(self, request: dict) -> str:
        existing = self._queue_item_for_ts(request["ts_name"])
        if existing is not None:
            self._merge_request(existing, request)
            return existing["queue_id"]
        if not request.get("queue_id"):
            request["queue_id"] = self._next_queue_id()
        iso = self._now()
        root = self._root(request["project_key"])
        ts_name = request["ts_name"]
        entry = self._status_row(root, ts_name)
        if isinstance(entry, dict):
            request["prev_state"] = entry.get("state")
            prev_gen = entry.get("generation", 0)
            if not isinstance(prev_gen, int) or isinstance(prev_gen, bool):
                prev_gen = 0
            request["prev_generation"] = prev_gen
            prev_msg = entry.get("message")
            request["prev_message"] = "" if prev_msg is None else str(prev_msg)
        else:
            request["prev_state"] = None
            request["prev_generation"] = 0
            request["prev_message"] = ""
        self._insert_queue(request)
        gen = self._status_generation(root, ts_name)
        self._set_status(
            root, ts_name, STATE_CONVERTING, gen, MSG_QUEUED, iso,
            queued=True,
        )
        return request["queue_id"]

    def _set_pending(self, request: dict) -> str:
        ts_name = request["ts_name"]
        current = self._pending.get(ts_name)
        if current is not None:
            self._merge_request(current, request)
            return current["queue_id"]
        request = dict(request)
        request["queue_id"] = self._next_queue_id()
        self._pending[ts_name] = request
        return request["queue_id"]

    def _discard_queue_and_pending(self, iso=None) -> None:
        iso = iso or self._now()
        items = list(self._queue)
        self._queue = []
        self._pending = {}
        for item in items:
            try:
                root = self._root(item["project_key"])
                name = item["ts_name"]
                gen = self._status_generation(root, name)
                self._set_status(root, name, STATE_ORIGINAL, gen, MSG_KILLSWITCH, iso)
            except Exception:
                pass

    def _job_age_s(self, job_path: Path):
        mtime = self.mtime_fn(os.fspath(job_path))
        now = self.time_fn()
        return float(now) - float(mtime)

    def _timeout_s_of(self, job) -> float:
        if not isinstance(job, dict):
            return 300.0
        raw = job.get("timeout_s", 300)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 300.0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 300.0
        if value <= 0:
            return 300.0
        return value

    def _adopt_rec(self, project_key, root: Path, ts_name: str, job_id: str, job: dict, entry: dict) -> dict:
        gen = entry.get("generation", 0) if isinstance(entry, dict) else 0
        if not isinstance(gen, int) or isinstance(gen, bool):
            gen = 0
        if isinstance(job, dict):
            job_gen = job.get("generation")
            if isinstance(job_gen, int) and not isinstance(job_gen, bool):
                gen = job_gen
            output_dir = job.get("output_dir")
            current_dir = job.get("current_snapshot_dir")
            uv_hash = job.get("uv_hash")
            scene_id = job.get("scene_id")
            timeout_s = self._timeout_s_of(job)
        else:
            output_dir = None
            current_dir = None
            uv_hash = None
            scene_id = None
            timeout_s = 300.0
        if scene_id is None:
            scene_id = self._scene_id()
        if not output_dir:
            output_dir = root / "output" / "final"
        if not current_dir:
            current_dir = root / "jobs" / job_id / "current"
        return {
            "job_id": job_id,
            "project_key": project_key,
            "ts_name": ts_name,
            "root": root,
            "output_dir": Path(output_dir),
            "current_dir": Path(current_dir),
            "uv_hash": uv_hash,
            "generation": gen,
            "proc": None,
            "log_fh": None,
            "log_path": None,
            "scene_id": "" if scene_id is None else str(scene_id),
            "adopted": True,
            "timeout_s": timeout_s,
        }

    def _interrupt_ts(self, root: Path, ts_name: str, entry: dict, iso: str) -> None:
        gen = entry.get("generation", 0) if isinstance(entry, dict) else 0
        if not isinstance(gen, int) or isinstance(gen, bool):
            gen = 0
        self._set_status(root, ts_name, STATE_ORIGINAL, gen, MSG_INTERRUPTED, iso)

    def _recover_job(self, project_key, root: Path, ts_name: str, job_id: str, entry: dict, iso: str) -> str:
        """'finished' / 'adopted' / 'interrupted'。"""
        job_dir = root / "jobs" / job_id
        job_path = job_dir / "job.json"
        result_path = job_dir / "result.json"
        try:
            result = read_json(result_path, None)
        except Exception:
            result = None
        try:
            job = read_json(job_path, None)
        except Exception:
            job = None
        job_ok = isinstance(job, dict)
        if isinstance(result, dict):
            rec = self._adopt_rec(project_key, root, ts_name, job_id, job if job_ok else {}, entry)
            code = _exit_code_from_result(result)
            if code is None:
                code = 1
            event = self._finish_job(rec, int(code))
            self._recovered_events.append(event)
            return "finished"
        if not job_ok:
            self._interrupt_ts(root, ts_name, entry, iso)
            return "interrupted"
        try:
            age = self._job_age_s(job_path)
        except Exception:
            self._interrupt_ts(root, ts_name, entry, iso)
            return "interrupted"
        timeout_s = self._timeout_s_of(job)
        if age > timeout_s:
            self._interrupt_ts(root, ts_name, entry, iso)
            return "interrupted"
        rec = self._adopt_rec(project_key, root, ts_name, job_id, job, entry)
        self._jobs[job_id] = rec
        return "adopted"

    def recover_orphaned(self, project_key) -> list:
        """status.json の CONVERTING のうち、この runtime の _jobs に無い TS を回収する。

        job_id があり result.json があれば完了処理、timeout 以内なら引き継ぎ、
        それ以外は ORIGINAL + MSG_INTERRUPTED。戻り値は中断扱いにした TS 名。
        実行中・待ち行列の TS は触らない。例外は外へ出さない。
        """
        recovered = []
        try:
            if project_key is None or project_key == "":
                return []
            root = self._root(project_key)
            data = read_json(root / "status.json", None)
            if not isinstance(data, dict):
                return []
            table = data.get("ts")
            if not isinstance(table, dict):
                return []
            inflight = set()
            for rec in self._jobs.values():
                name = rec.get("ts_name")
                if name:
                    inflight.add(str(name))
            for item in self._queue:
                name = item.get("ts_name")
                if name:
                    inflight.add(str(name))
            iso = self._now()
            for ts_name, entry in list(table.items()):
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("state") or "") != STATE_CONVERTING:
                    continue
                name = str(ts_name)
                if name in inflight:
                    continue
                if entry.get("queued") is True:
                    self._interrupt_ts(root, name, entry, iso)
                    recovered.append(name)
                    continue
                job_id = entry.get("job_id")
                if job_id:
                    action = self._recover_job(
                        project_key, root, name, str(job_id), entry, iso,
                    )
                    if action == "interrupted":
                        recovered.append(name)
                    continue
                self._interrupt_ts(root, name, entry, iso)
                recovered.append(name)
        except Exception:
            return recovered
        return recovered

    def record_baseline(self, project_key, ts_names, shape_paths, *, force=False, uv_set=None) -> str:
        """基準UVを記録する。既にあれば上書きしない。force=True のときだけ別 ID で追加する。"""
        root = self._root(project_key)
        existing = _read_active_baseline(root)
        if existing is None:
            ids = _list_baseline_ids(root)
            existing = ids[0] if ids else None
        if existing and not force:
            return existing
        iso = self._now()
        baseline_id = self._next_id("bl-", iso)
        arrays, info = self._collect(shape_paths, ts_names, uv_set)
        snap = br.arrays_to_snapshot(
            arrays, info, baseline_id=baseline_id, created=iso,
        )
        br.save_baseline(snap, root)
        _write_active_baseline(root, baseline_id)
        return baseline_id

    def revert_to_original(self, ts_name=None) -> list:
        """指定 TS(省略時は全部)の、属性付きファイルノードを原本へ戻す。"""
        if not self._enabled_now():
            self._discard_queue_and_pending()
        cmds = self._cmds()
        reverted = revert_ts_file_nodes(cmds, ts_name=ts_name)
        iso = self._now()
        for root in self._seen_roots.values():
            if ts_name is None:
                self._set_status_all(root, STATE_ORIGINAL, "", iso)
            else:
                self._set_status(root, ts_name, STATE_ORIGINAL, 0, "", iso)
        return reverted

    def convert_now(
        self,
        project_key,
        ts_name,
        shape_paths,
        source_dir,
        output_dir,
        quality="final",
        channels=None,
        origin="manual",
        **opts,
    ) -> str:
        """ジョブを作り mayapy ワーカーを非同期起動する。条件を満たさなければジョブは作らない。

        同時実行の上限に達していれば予約 ID(``queued:<連番>``)を返す。
        ``origin`` は ``"manual"`` / ``"auto"``。手動は自動より先に並べる。
        """
        iso = self._now()
        root = self._root(project_key)
        origin = _normalize_origin(origin)
        if not self.get_enabled():
            self._discard_queue_and_pending(iso=iso)
            revert_ts_file_nodes(self._cmds(), ts_name=None)
            self._set_status_all(root, STATE_ORIGINAL, MSG_KILLSWITCH, iso, extra_ts=(ts_name,))
            return ""

        if not ts_name:
            raise RuntimeControlError("ts_name が空です")

        request = self._make_request(
            project_key, ts_name, shape_paths, source_dir, output_dir,
            quality, channels, origin, opts,
        )

        if self.has_running_job_for_ts(ts_name):
            return self._set_pending(request)

        queued = self._queue_item_for_ts(str(ts_name))
        if queued is not None:
            self._merge_request(queued, request)
            self._drain_queue()
            started = self._job_id_for_ts(ts_name)
            return started or queued["queue_id"]

        if self._running_worker_count() < self._max_parallel() and not self._queue:
            return self._launch_job(request)

        qid = self._enqueue(request)
        self._drain_queue()
        started = self._job_id_for_ts(ts_name)
        return started or qid

    def mark_stale(self, project_key, ts_name, message) -> bool:
        """CONVERTED_OK のときだけ STALE にする。generation は変えない。"""
        try:
            root = self._root(project_key)
            entry = self._status_row(root, ts_name)
            if entry is None:
                return False
            if str(entry.get("state") or "") != STATE_CONVERTED_OK:
                return False
            gen = entry.get("generation", 0)
            if not isinstance(gen, int) or isinstance(gen, bool):
                gen = 0
            self._set_status(
                root, ts_name, STATE_STALE, gen,
                "" if message is None else str(message),
                self._now(),
            )
            return True
        except Exception:
            return False

    def _source_sig_now(self, source_dir, ts_name, channels):
        try:
            return follow.source_signature(source_dir, ts_name, channels)
        except Exception:
            return {}

    def _recorded_source_sig(self, root: Path, ts_name: str):
        try:
            data = follow.load_follow(root)
        except Exception:
            return None
        table = data.get("ts") if isinstance(data, dict) else None
        if not isinstance(table, dict):
            return None
        row = table.get(ts_name)
        if not isinstance(row, dict):
            return None
        return row.get("source_sig")

    def _should_skip_auto_launch(self, root: Path, ts_name, uv_hash, source_dir, channels) -> bool:
        """P2-5b。自動要求で UV ハッシュと source_sig が両方一致するときだけ True。"""
        try:
            last = _last_good_uv_hash(root, ts_name)
            sig_now = self._source_sig_now(source_dir, ts_name, channels)
            sig_rec = self._recorded_source_sig(root, ts_name)
            return follow.should_skip_auto(uv_hash, last, sig_now, sig_rec)
        except Exception:
            return False

    def _uv_fp_shapes(self, request_shapes, opts):
        """指紋を取るシェイプ。``uv_fp_shapes`` が無ければジョブのシェイプを使う。"""
        named = opts.get("uv_fp_shapes") if isinstance(opts, dict) else None
        if isinstance(named, (list, tuple)) and not isinstance(named, str) and named:
            return [path for path in named if isinstance(path, str) and path]
        if request_shapes is None:
            return []
        return [path for path in list(request_shapes) if isinstance(path, str)]

    def _call_uv_fingerprints(self, shape_paths, uv_set):
        """指紋関数の戻り値を ``{シェイプ名: [crc, UV数]}`` にする。失敗は None。"""
        fn = self.uv_fingerprints_fn
        if fn is None:
            import uvremap_maya
            fn = uvremap_maya.uv_fingerprints
        result = fn(list(shape_paths), uv_set)
        mapping = None
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], dict):
            mapping = result[0]
        elif isinstance(result, dict):
            mapping = result
        if not isinstance(mapping, dict):
            return None
        stored = {}
        for name, value in mapping.items():
            if not isinstance(name, str) or not name:
                continue
            pair = follow._fingerprint_pair(value)
            if pair is None:
                continue
            stored[name] = [pair[0], pair[1]]
        return stored

    def _write_follow_launch(
        self, root: Path, ts_name, source_dir, channels, job_id, scene_id,
        shape_paths=None, uv_set=None,
    ) -> None:
        """起動直後に follow.json へ source_sig / last_job_id / scene_id / uv_fp を書く。

        指紋の取得に失敗しても変換は止めない。失敗時は既存の ``uv_fp`` を残す。
        """
        try:
            data = follow.load_follow(root)
            table = data.get("ts")
            if not isinstance(table, dict):
                table = {}
                data["ts"] = table
            prev = table.get(ts_name) if isinstance(table.get(ts_name), dict) else {}
            row = dict(prev)
            row["source_sig"] = self._source_sig_now(source_dir, ts_name, channels)
            row["last_job_id"] = str(job_id)
            row["scene_id"] = "" if scene_id is None else str(scene_id)
            try:
                uv_fp = self._call_uv_fingerprints(shape_paths, uv_set)
            except Exception:
                uv_fp = None
            if isinstance(uv_fp, dict):
                row["uv_fp"] = uv_fp
            table[ts_name] = row
            follow.save_follow(root, data)
        except Exception:
            pass

    def _no_change_message(self, ts_name) -> str:
        try:
            return follow.MSG_NO_CHANGE.format(ts=ts_name)
        except (KeyError, IndexError, ValueError):
            return follow.MSG_NO_CHANGE

    def _launch_job(self, request: dict) -> str:
        """配列取得・スナップショット・ワーカー起動。待ち行列からの起動でも使う。"""
        project_key = request["project_key"]
        ts_name = request["ts_name"]
        shape_paths = request.get("shape_paths")
        source_dir = request["source_dir"]
        output_dir = request["output_dir"]
        quality = request.get("quality") or "final"
        channels = request.get("channels")
        origin = _normalize_origin(request.get("origin"))
        opts = dict(request.get("opts") or {})
        iso = self._now()
        root = self._root(project_key)

        if not self._enabled_now():
            self._discard_queue_and_pending(iso=iso)
            revert_ts_file_nodes(self._cmds(), ts_name=None)
            self._set_status_all(root, STATE_ORIGINAL, MSG_KILLSWITCH, iso, extra_ts=(ts_name,))
            return ""

        baseline_id = _read_active_baseline(root)
        if baseline_id is None:
            ids = _list_baseline_ids(root)
            baseline_id = ids[0] if ids else None
        if not baseline_id:
            self._set_status(root, ts_name, STATE_BLOCKED, 0, MSG_NO_BASELINE, iso)
            return ""

        mayapy = resolve_mayapy(self.mayapy_path)
        if not mayapy:
            self._set_status(root, ts_name, STATE_FAILED, 0, MSG_NO_MAYAPY, iso)
            return ""
        if not self.worker_script.is_file():
            self._set_status(
                root, ts_name, STATE_FAILED, 0,
                "ワーカー脚本が見つかりません: {0}".format(self.worker_script.name),
                iso,
            )
            return ""

        baseline_dir = root / "baseline" / baseline_id
        ts_names = [str(ts_name)]
        uv_set = opts.get("uv_set")
        try:
            meta = read_json(baseline_dir / "meta.json", None)
        except Exception:
            meta = None
        if isinstance(meta, dict) and meta.get("ts_names"):
            ts_names = [str(n) for n in meta["ts_names"]]
            if ts_name not in ts_names:
                ts_names.append(str(ts_name))
            if uv_set is None and meta.get("uv_set") is not None:
                uv_set = meta.get("uv_set")

        arrays, info = self._collect(shape_paths, ts_names, uv_set)
        current = br.arrays_to_snapshot(
            arrays, info, baseline_id=baseline_id, created=iso,
        )
        uv_hash = br.current_uv_hash(current, ts_name)
        src = Path(source_dir)
        ch = channels
        if ch is None:
            ch = [dict(item) for item in br.DEFAULT_CHANNELS]
        if origin == ORIGIN_AUTO and self._should_skip_auto_launch(
            root, ts_name, uv_hash, src, ch,
        ):
            gen = self._status_generation(root, ts_name)
            self._set_status(
                root, ts_name, STATE_CONVERTED_OK, gen,
                self._no_change_message(ts_name), iso,
            )
            return ""
        ledger = _load_ledger(root)
        generation = max(_ledger_generation(ledger, ts_name), self._max_inflight_generation(ts_name)) + 1
        latest_path = latest_generation_file(root, ts_name)
        job_id = self._next_id("", iso)
        current_dir = br.save_current(current, root, job_id)

        out = Path(output_dir)
        flag = opts.get("source_flag")
        if flag is None:
            auto_flag = src / "_sync_complete.flag"
            if auto_flag.is_file():
                flag = auto_flag
        out_res = opts.get("out_res", None)
        n_tiles = int(opts.get("n_tiles", 1) or 1)
        if "timeout_s" in opts:
            timeout_s = opts["timeout_s"]
        elif out_res is None:
            timeout_s = 300
        else:
            timeout_s = br.choose_timeout_s(out_res, len(ch), n_tiles)

        scene_id = request.get("scene_id")
        if scene_id is None:
            scene_id = self._scene_id()
        job = br.build_job(
            job_id=job_id,
            generation=generation,
            ts_name=ts_name,
            quality=quality,
            baseline_dir=str(baseline_dir),
            current_snapshot_dir=str(current_dir),
            source_dir=str(src),
            output_dir=str(out),
            uvremap_root=str(root),
            source_flag=None if flag is None else str(flag),
            udim=bool(opts.get("udim", True)),
            out_res=out_res,
            dilate_px=int(opts.get("dilate_px", 16)),
            normal_mode=str(opts.get("normal_mode", "uv")),
            channels=ch,
            timeout_s=timeout_s,
            copy_retries=int(opts.get("copy_retries", 3)),
            uv_hash=uv_hash,
            baseline_id=baseline_id,
            lock_timeout_s=opts["lock_timeout_s"] if "lock_timeout_s" in opts else timeout_s,
            latest_generation_path=str(latest_path),
            scene_id=str(scene_id),
        )
        self._write_latest_generation(latest_path, ts_name, generation)
        self._point_inflight_jobs_at_latest(ts_name, latest_path)
        job_dir = root / "jobs" / job_id
        job_path = br.write_job(job, job_dir / "job.json")
        log_path = job_dir / "worker.log"
        log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
        cmd = [mayapy, str(self.worker_script), "run", str(job_path)]
        popen_kwargs = {
            "stdout": log_fh,
            "stderr": subprocess.STDOUT,
            "env": worker_env(),
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = CREATE_NO_WINDOW
        self._set_status(root, ts_name, STATE_CONVERTING, generation, "", iso, queued=False)
        try:
            proc = self.popen_fn(cmd, **popen_kwargs)
        except Exception as exc:
            log_fh.close()
            self._set_status(
                root, ts_name, STATE_FAILED, generation,
                "ワーカーを起動できません: {0}".format(exc),
                iso,
            )
            return ""

        uv_shapes = self._uv_fp_shapes(shape_paths, opts)
        self._jobs[job_id] = {
            "job_id": job_id,
            "project_key": project_key,
            "ts_name": ts_name,
            "root": root,
            "output_dir": out,
            "current_dir": current_dir,
            "uv_hash": uv_hash,
            "generation": generation,
            "proc": proc,
            "log_fh": log_fh,
            "log_path": log_path,
            "scene_id": str(scene_id),
        }
        self._write_follow_launch(
            root, ts_name, src, ch, job_id, scene_id, uv_shapes, uv_set,
        )
        return job_id

    def _restore_queued_after_scene_change(self, item: dict) -> None:
        """待ち行列から起動できなかったとき、順番待ち直前の行へ戻す。"""
        iso = self._now()
        root = self._root(item["project_key"])
        prev_state = item.get("prev_state")
        gen = item.get("prev_generation", 0)
        if not isinstance(gen, int) or isinstance(gen, bool):
            gen = 0
        if not prev_state:
            self._set_status(
                root, item["ts_name"], STATE_ORIGINAL, gen, MSG_SCENE_CHANGED, iso,
            )
            return
        self._set_status(
            root, item["ts_name"], str(prev_state), gen, MSG_SCENE_CHANGED, iso,
        )

    def _drain_queue(self) -> None:
        """空きがあれば待ち行列の先頭から起動する。起動時にキルスイッチ・シーンを再確認する。"""
        while self._queue and self._running_worker_count() < self._max_parallel():
            if not self._enabled_now():
                self._discard_queue_and_pending()
                return
            item = self._queue.pop(0)
            current_scene = self._scene_id()
            if current_scene != str(item.get("scene_id") or ""):
                try:
                    self._restore_queued_after_scene_change(item)
                except Exception:
                    pass
                continue
            self._launch_job(item)

    def _poll_adopted(self, rec: dict):
        """引き継ぎジョブ。result.json があれば完了、timeout 超過なら中断。待ちなら None。"""
        root = rec["root"]
        job_id = rec["job_id"]
        result_path = root / "jobs" / job_id / "result.json"
        try:
            result = read_json(result_path, None)
        except Exception:
            result = None
        if isinstance(result, dict):
            code = _exit_code_from_result(result)
            if code is None:
                code = 1
            return self._finish_job(rec, int(code))
        job_path = root / "jobs" / job_id / "job.json"
        timeout_s = rec.get("timeout_s", 300)
        try:
            timeout_s = float(timeout_s)
        except (TypeError, ValueError):
            timeout_s = 300.0
        try:
            age = self._job_age_s(job_path)
        except Exception:
            age = timeout_s + 1.0
        if age > timeout_s:
            iso = self._now()
            ts_name = rec["ts_name"]
            gen = rec.get("generation", 0)
            if not isinstance(gen, int) or isinstance(gen, bool):
                gen = 0
            self._set_status(root, ts_name, STATE_ORIGINAL, gen, MSG_INTERRUPTED, iso)
            return {
                "job_id": job_id,
                "project_key": rec.get("project_key"),
                "ts_name": ts_name,
                "exit_code": None,
                "state": STATE_ORIGINAL,
                "message": MSG_INTERRUPTED,
                "switched": [],
                "reverted": [],
                "updated": iso,
            }
        return None

    def poll(self) -> list:
        """実行中ジョブを調べ、終わったものがあれば切替/D-3 を行いイベントを返す。

        最後に空きがあれば待ち行列の先頭から起動する。
        recover_orphaned が直前に完了させたジョブのイベントを先頭に含める。
        """
        events = list(self._recovered_events)
        self._recovered_events = []
        finished = []
        for job_id, rec in list(self._jobs.items()):
            if rec.get("adopted"):
                outcome = self._poll_adopted(rec)
                if outcome is None:
                    continue
                events.append(outcome)
                finished.append(job_id)
                continue
            proc = rec.get("proc")
            code = None
            try:
                code = proc.poll() if proc is not None else 0
            except Exception:
                code = None
            if code is None:
                continue
            events.append(self._finish_job(rec, int(code)))
            finished.append(job_id)
        pending_to_enqueue = []
        for job_id in finished:
            rec = self._jobs.pop(job_id, None)
            if rec is None:
                continue
            fh = rec.get("log_fh")
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
            ts_name = rec.get("ts_name")
            if ts_name and ts_name in self._pending:
                pending_to_enqueue.append(self._pending.pop(ts_name))
        if not self._enabled_now():
            self._discard_queue_and_pending()
        else:
            for request in pending_to_enqueue:
                self._enqueue(request)
            self._drain_queue()
        for key in list(self._seen_roots):
            self.recover_orphaned(key)
        return events

    def _finish_job(self, rec: dict, exit_code: int) -> dict:
        ts_name = rec["ts_name"]
        root = rec["root"]
        gen = rec.get("generation", 0)
        if not isinstance(gen, int) or isinstance(gen, bool):
            gen = 0
        # キルスイッチをシーン確認より先に見る(OFF なら切替も D-3 も行わない)。
        if not self._enabled_now():
            iso = self._now()
            self._set_status(root, ts_name, STATE_ORIGINAL, gen, MSG_KILLSWITCH, iso)
            return {
                "job_id": rec["job_id"],
                "project_key": rec.get("project_key"),
                "ts_name": ts_name,
                "exit_code": exit_code,
                "state": STATE_ORIGINAL,
                "message": MSG_KILLSWITCH,
                "switched": [],
                "reverted": [],
                "updated": iso,
                "warning": WARN_DISABLED,
            }
        if self._scene_id() != str(rec.get("scene_id") or ""):
            iso = self._now()
            self._set_status(root, ts_name, STATE_ORIGINAL, gen, MSG_SCENE_CHANGED, iso)
            return {
                "job_id": rec["job_id"],
                "project_key": rec.get("project_key"),
                "ts_name": ts_name,
                "exit_code": exit_code,
                "state": STATE_ORIGINAL,
                "message": MSG_SCENE_CHANGED,
                "switched": [],
                "reverted": [],
                "updated": iso,
                "warning": WARN_SCENE_CHANGED,
            }
        iso = self._now()
        ts_name = rec["ts_name"]
        root = rec["root"]
        result_path = root / "jobs" / rec["job_id"] / "result.json"
        result_missing = False
        try:
            result = read_json(result_path, None)
        except Exception:
            result = None
            result_missing = True
        if not isinstance(result, dict):
            result = {}
            result_missing = True
        status = str(result.get("status") or "")
        files = result.get("files") if isinstance(result.get("files"), list) else []
        message = ""
        for item in result.get("messages") or []:
            if isinstance(item, dict) and item.get("text"):
                message = str(item["text"])
                if item.get("level") == "error":
                    break
        switched = []
        reverted = []
        missing = []
        state = STATE_FAILED
        cmds = self._cmds()
        attempted_switch = False
        if exit_code == 0:
            attempted_switch = True
            switched = switch_ts_file_nodes(
                cmds, ts_name, files, rec["output_dir"],
                exists_fn=self.exists_fn, missing_out=missing,
            )
            state = STATE_CONVERTED_OK
        elif exit_code == 10:
            state = str((read_json(root / "status.json", {}) or {}).get("ts", {}).get(ts_name, {}).get("state") or STATE_CONVERTING)
        elif exit_code in FAIL_EXIT_CODES:
            if exit_code == 11:
                state = STATE_BLOCKED
            else:
                state = STATE_FAILED
            if _should_revert_d3(root, ts_name, rec.get("uv_hash")):
                reverted = revert_ts_file_nodes(cmds, ts_name=ts_name)
        else:
            if _should_revert_d3(root, ts_name, rec.get("uv_hash")):
                reverted = revert_ts_file_nodes(cmds, ts_name=ts_name)
        if status == "blocked":
            state = STATE_BLOCKED
        elif status == "ok":
            state = STATE_CONVERTED_OK
        elif status in ("failed", "retry_exhausted"):
            state = STATE_FAILED
        crash = exit_code != 10 and (
            exit_code not in KNOWN_WORKER_EXITS or result_missing
        )
        if crash:
            crash_msg = MSG_WORKER_CRASH.format(exit_code)
            if self._should_write_crash_status(root, ts_name, gen):
                self._set_status(root, ts_name, STATE_FAILED, gen, crash_msg, iso)
                state = STATE_FAILED
                if not message:
                    message = crash_msg
            else:
                existing = self._status_row(root, ts_name)
                if existing is not None:
                    state = str(existing.get("state") or state)
                    if not message and existing.get("message"):
                        message = str(existing.get("message"))
        event = {
            "job_id": rec["job_id"],
            "project_key": rec.get("project_key"),
            "ts_name": ts_name,
            "exit_code": exit_code,
            "state": state,
            "message": message,
            "switched": switched,
            "reverted": reverted,
            "updated": iso,
        }
        if attempted_switch and missing:
            event["warning"] = WARN_FILE_MISSING
            event["missing"] = missing
        if attempted_switch and not switched:
            if "warning" not in event:
                event["warning"] = WARN_NO_FILE_NODES
            event["message"] = MSG_NO_FILE_NODES
            event["state"] = STATE_ORIGINAL
            gen = rec.get("generation", 0)
            if not isinstance(gen, int) or isinstance(gen, bool):
                gen = 0
            self._set_status(root, ts_name, STATE_ORIGINAL, gen, MSG_NO_FILE_NODES, iso)
        elif state in TERMINAL_STATES:
            existing = self._status_row(root, ts_name)
            cur_state = str((existing or {}).get("state") or "")
            if existing is None or cur_state == STATE_CONVERTING:
                self._set_status(root, ts_name, state, gen, message, iso)
        return event

    def record_baseline_candidate(self, project_key, ts_names, shape_paths, *, uv_set=None) -> str:
        """基準UVの候補を記録する。``active.json`` は作らず、書き換えない。

        2026.10.19(RB-b): 同じ ID のフォルダがあれば ID を取り直す(最大 8 回)。
        収集か保存に失敗したら、この呼び出しが作ったフォルダだけ消してから再送出する。
        """
        root = self._root(project_key)
        iso = self._now()
        for _attempt in range(_CANDIDATE_ID_ATTEMPTS):
            baseline_id = self._next_id("bl-", iso)
            dest = root / "baseline" / baseline_id
            if dest.exists():
                continue
            try:
                arrays, info = self._collect(shape_paths, ts_names, uv_set)
                if dest.exists():
                    continue
                snap = br.arrays_to_snapshot(
                    arrays, info, baseline_id=baseline_id, created=iso,
                )
                if dest.exists():
                    continue
                br.save_baseline(snap, root)
            except Exception:
                _remove_candidate_dir(dest)
                raise
            return baseline_id
        raise RuntimeControlError("基準候補の ID が既存フォルダと衝突したため作れませんでした")

    def discard_baseline_candidate(self, project_key, baseline_id) -> bool:
        """候補フォルダを消す。現行の基準は消さない。失敗しても False で、例外は出さない。

        2026.10.19(RB-b): ``active.json`` は書き換えない。他の候補フォルダも触らない。
        """
        try:
            if not _is_safe_baseline_id(baseline_id):
                return False
            root = self._root(project_key)
            folder = root / "baseline" / baseline_id
            if not folder.is_dir():
                return False
            try:
                active = _read_active_baseline(root)
            except Exception:
                active = None
            if active is not None and active == baseline_id:
                return False
            raw = _read_raw_active_baseline_id(root)
            if raw is not None and raw == baseline_id:
                return False
            # 解決できた基準も active.json の生 ID も無いときだけ、名前順の先頭を守る。
            # record_baseline / convert_now が事実上の基準に使うため。
            # 括弧書きの「1」は条件1(安全でない ID)で、ここに来る時点では無い。
            # 生 ID があるときは条件4だけを守り、先頭は守らない。契約に無い。仮。
            if active is None and raw is None:
                ids = _list_baseline_ids(root)
                if ids and ids[0] == baseline_id:
                    return False
            resolved = _resolved_baseline_child(root, baseline_id)
            if resolved is None:
                return False
            shutil.rmtree(resolved)
            return True
        except Exception:
            return False

    def register_rebase(self, project_key, request_id, baseline_id, ts_names, *, scene_id=None) -> None:
        """完了処理のための Maya ローカル記録を作る。同じ request_id は上書きしない。

        2026.10.19(RB-b): 依頼ファイルを書く前に呼ぶ(RB-d への申し送り)。
        ``scene_id`` を省略したときは現在のシーン。``rebase/`` には書かない。
        """
        request_id = _require_nonempty_str(request_id, "request_id")
        baseline_id = _require_nonempty_str(baseline_id, "baseline_id")
        names = _require_ts_names(ts_names)
        root = self._root(project_key)
        if scene_id is None:
            scene = self._scene_id()
        else:
            scene = str(scene_id)
        _write_rebase_row(root, request_id, {
            "baseline_id": baseline_id,
            "ts_names": names,
            "scene_id": scene,
            "registered_at": self._now(),
            "completed": False,
            "completed_at": None,
            "steps": _empty_rebase_steps(),
        }, self.time_fn, only_if_absent=True)

    def rebase_record(self, project_key, request_id):
        """ローカル記録のコピー。無い、壊れている、request_id が文字列でないときは None。"""
        if not isinstance(request_id, str):
            return None
        root = self._root(project_key)
        row = _lookup_rebase_row(root, request_id)
        if row is None:
            return None
        return _copy_rebase_row(row)

    def _rebase_activate(self, root: Path, baseline_id: str) -> None:
        folder = root / "baseline" / str(baseline_id)
        if not (folder / "mesh.npz").is_file():
            raise RuntimeControlError("基準候補が見つかりません")
        _write_active_baseline(root, baseline_id)

    def _rebase_reset_follow(self, root: Path, ts_names) -> None:
        """切り替えた TS の follow 行だけ消す。1 行も無ければ follow.json は書かない。

        契約の「初期化」の形は無い。契約に無い。仮。行の削除にした。
        """
        data = follow.load_follow(root)
        table = data.get("ts")
        if not isinstance(table, dict):
            return
        removed = False
        for name in ts_names:
            if name in table:
                del table[name]
                removed = True
        if not removed:
            return
        data["ts"] = table
        follow.save_follow(root, data)

    def _save_rebase_step(self, root: Path, request_id: str, row: dict, step_name: str) -> None:
        row["steps"][step_name] = True
        try:
            _write_rebase_row(root, request_id, row, self.time_fn)
        except Exception:
            row["steps"][step_name] = False
            raise

    def _rebase_blocked_by_job(self, ts_names) -> bool:
        """対象 TS が実行中・待ち行列・保留のどれかなら True。

        2026.10.19(RB-b): 旧基準のジョブが後から旧 UV へ切り戻すのを防ぐ。
        """
        for name in ts_names:
            if self.has_running_job_for_ts(name):
                return True
            if self._queue_item_for_ts(name) is not None:
                return True
            key = str(name) if name is not None else ""
            if key and key in self._pending:
                return True
        return False

    def complete_rebase(self, project_key, request_id, *, history_fn=None) -> dict:
        """リベースの完了処理。順序は active → reverted → follow_reset → history。

        2026.10.19(RB-b): 済んだ段階は記録して飛ばす。失敗しても先に進んだ分は戻さない。
        キルスイッチはシーン確認より先に見る。reverted の前に、対象 TS の
        実行中・待ち・保留ジョブがあれば何も変えず ``skipped`` / ``job_running``。
        """
        if not isinstance(request_id, str):
            return _rebase_call_result(
                status="skipped",
                reason="no_local_record",
                request_id=request_id,
                baseline_id="",
                ts_names=[],
                steps=_empty_rebase_steps(),
                reverted=[],
                failed_step=None,
                error=None,
            )
        root = self._root(project_key)
        row = _lookup_rebase_row(root, request_id)
        if row is None:
            return _rebase_call_result(
                status="skipped",
                reason="no_local_record",
                request_id=request_id,
                baseline_id="",
                ts_names=[],
                steps=_empty_rebase_steps(),
                reverted=[],
                failed_step=None,
                error=None,
            )
        if row["completed"]:
            return _rebase_call_result(
                status="already_completed",
                reason="",
                request_id=request_id,
                baseline_id=row["baseline_id"],
                ts_names=row["ts_names"],
                steps=row["steps"],
                reverted=[],
                failed_step=None,
                error=None,
            )
        if not self._enabled_now():
            return _rebase_call_result(
                status="skipped",
                reason="killswitch_off",
                request_id=request_id,
                baseline_id=row["baseline_id"],
                ts_names=row["ts_names"],
                steps=row["steps"],
                reverted=[],
                failed_step=None,
                error=None,
            )
        if _scene_token(self._scene_id()) != _scene_token(row.get("scene_id")):
            return _rebase_call_result(
                status="skipped",
                reason="scene_changed",
                request_id=request_id,
                baseline_id=row["baseline_id"],
                ts_names=row["ts_names"],
                steps=row["steps"],
                reverted=[],
                failed_step=None,
                error=None,
            )
        if self._rebase_blocked_by_job(row["ts_names"]):
            return _rebase_call_result(
                status="skipped",
                reason="job_running",
                request_id=request_id,
                baseline_id=row["baseline_id"],
                ts_names=row["ts_names"],
                steps=row["steps"],
                reverted=[],
                failed_step=None,
                error=None,
            )

        reverted = []
        step = "active"
        try:
            if not row["steps"]["active"]:
                step = "active"
                self._rebase_activate(root, row["baseline_id"])
                self._save_rebase_step(root, request_id, row, "active")
            if not row["steps"]["reverted"]:
                step = "reverted"
                self._root(project_key)
                for ts_name in list(row["ts_names"]):
                    reverted.extend(self.revert_to_original(ts_name))
                self._save_rebase_step(root, request_id, row, "reverted")
            if not row["steps"]["follow_reset"]:
                step = "follow_reset"
                self._rebase_reset_follow(root, row["ts_names"])
                self._save_rebase_step(root, request_id, row, "follow_reset")
            if not row["steps"]["history"]:
                step = "history"
                if history_fn is not None:
                    history_fn("uvremap", MSG_REBASE_HISTORY_DONE)
                self._save_rebase_step(root, request_id, row, "history")
            row["completed"] = True
            row["completed_at"] = self._now()
            try:
                _write_rebase_row(root, request_id, row, self.time_fn)
            except Exception:
                row["completed"] = False
                row["completed_at"] = None
                raise
        except Exception as exc:
            _notify_rebase_partial(history_fn, step)
            saved = _lookup_rebase_row(root, request_id)
            if saved is None:
                saved_steps = _empty_rebase_steps()
                saved_baseline = row["baseline_id"]
                saved_names = row["ts_names"]
            else:
                saved_steps = saved["steps"]
                saved_baseline = saved["baseline_id"]
                saved_names = saved["ts_names"]
            return _rebase_call_result(
                status="partial",
                reason="",
                request_id=request_id,
                baseline_id=saved_baseline,
                ts_names=saved_names,
                steps=saved_steps,
                reverted=reverted,
                failed_step=step,
                error=_rebase_error_text(exc),
            )
        saved = _lookup_rebase_row(root, request_id) or row
        return _rebase_call_result(
            status="completed",
            reason="",
            request_id=request_id,
            baseline_id=saved["baseline_id"],
            ts_names=saved["ts_names"],
            steps=saved["steps"],
            reverted=reverted,
            failed_step=None,
            error=None,
        )
