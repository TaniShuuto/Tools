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
import subprocess
import sys
from pathlib import Path

import uvremap_bridge as br
import uvremap_core as core
from uvremap_worker import (
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
STATE_FAILED = "FAILED"
STATE_BLOCKED = "BLOCKED"
MSG_NO_BASELINE = "基準UVが未記録です。Painterへ送った時点で記録してください"
MSG_KILLSWITCH = "キルスイッチがオフのため原本を表示します"
MSG_NO_MAYAPY = (
    "mayapy が見つかりません。Maya の mayapy のパスを mayapy_path で指定してください"
)
MSG_NO_FILE_NODES = "変換は完了しましたが、切り替え対象のファイルノードが見つかりません"
MSG_SCENE_CHANGED = (
    "シーンが変わったため切り替えを見送りました。もう一度『今すぐ変換』を押してください"
)
MSG_INTERRUPTED = (
    "前回の変換は途中で中断されました。もう一度『今すぐ変換』を押してください"
)
MSG_ALREADY_RUNNING = "このテクスチャセットは変換中です"
MSG_WORKER_CRASH = "ワーカーが異常終了しました(終了コード {0})"
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
        self._seq = 0
        self._jobs = {}
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

    def _set_status(self, root: Path, ts_name: str, state: str, generation: int, message: str, iso: str):
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
        """同じ TS のジョブが ``_jobs`` にあれば True。"""
        want = str(ts_name) if ts_name is not None else ""
        if not want:
            return False
        for rec in self._jobs.values():
            if rec.get("ts_name") == want:
                return True
        return False

    def recover_orphaned(self, project_key) -> list:
        """status.json の CONVERTING のうち、この runtime の _jobs に無い TS を ORIGINAL へ戻す。

        窓を閉じてタイマーが止まったあとに残った行を、次の窓で回収する。
        実行中(_jobs にある)の TS は触らない。例外は外へ出さない。
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
            iso = self._now()
            for ts_name, entry in list(table.items()):
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("state") or "") != STATE_CONVERTING:
                    continue
                name = str(ts_name)
                if name in inflight:
                    continue
                gen = entry.get("generation", 0)
                if not isinstance(gen, int) or isinstance(gen, bool):
                    gen = 0
                self._set_status(root, name, STATE_ORIGINAL, gen, MSG_INTERRUPTED, iso)
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
        **opts,
    ) -> str:
        """ジョブを作り mayapy ワーカーを非同期起動する。条件を満たさなければジョブは作らない。"""
        iso = self._now()
        root = self._root(project_key)
        if not self.get_enabled():
            revert_ts_file_nodes(self._cmds(), ts_name=None)
            self._set_status_all(root, STATE_ORIGINAL, MSG_KILLSWITCH, iso, extra_ts=(ts_name,))
            return ""

        if not ts_name:
            raise RuntimeControlError("ts_name が空です")

        if self.has_running_job_for_ts(ts_name):
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
        ledger = _load_ledger(root)
        generation = max(_ledger_generation(ledger, ts_name), self._max_inflight_generation(ts_name)) + 1
        latest_path = latest_generation_file(root, ts_name)
        job_id = self._next_id("", iso)
        current_dir = br.save_current(current, root, job_id)

        src = Path(source_dir)
        out = Path(output_dir)
        flag = opts.get("source_flag")
        if flag is None:
            auto_flag = src / "_sync_complete.flag"
            if auto_flag.is_file():
                flag = auto_flag
        ch = channels
        if ch is None:
            ch = [dict(item) for item in br.DEFAULT_CHANNELS]
        out_res = opts.get("out_res", None)
        n_tiles = int(opts.get("n_tiles", 1) or 1)
        if "timeout_s" in opts:
            timeout_s = opts["timeout_s"]
        elif out_res is None:
            timeout_s = 300
        else:
            timeout_s = br.choose_timeout_s(out_res, len(ch), n_tiles)

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
        self._set_status(root, ts_name, STATE_CONVERTING, generation, "", iso)
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
            "scene_id": self._scene_id(),
        }
        return job_id

    def poll(self) -> list:
        """実行中ジョブを調べ、終わったものがあれば切替/D-3 を行いイベントを返す。"""
        events = []
        finished = []
        for job_id, rec in list(self._jobs.items()):
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
        return event
