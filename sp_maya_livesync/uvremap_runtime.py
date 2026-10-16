# -*- coding: utf-8 -*-
"""Maya 側の UV 再マップ実行制御(基準記録・ワーカー起動・ファイルノード切替)。

契約: docs/uvremap/CONTRACT.md §2〜§6、D-3 / D-4。
Maya の import は関数の中だけ。cmds / Popen / 配列収集は差し替え可能。
既存の maya_live_sync.py は変更しない。
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

import uvremap_bridge as br
import uvremap_core as core
from uvremap_worker import read_json, write_json_atomic

SCHEMA = 1
ATTR_ORIGINAL_PATH = "uvremapOriginalPath"
ATTR_ORIGINAL_TILING = "uvremapOriginalTiling"
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
WORKER_SCRIPT = Path(__file__).resolve().parent / "uvremap_worker.py"
ACTIVE_BASELINE_NAME = "active.json"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
FAIL_EXIT_CODES = (11, 12, 13)
_TILE_STEM_RE = re.compile(r"^(.+)\.(\d{4})$")
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


def switch_ts_file_nodes(cmds, ts_name: str, files, output_dir) -> list:
    """当該 TS のファイルノードだけを変換後パスへ切り替える。

    属性名 ``uvremapOriginalPath`` / ``uvremapOriginalTiling`` は後続作業が
    依存するため変更しない。既に属性があるノードは元パスを上書きしない。
    """
    grouped = suffixes_from_files(ts_name, files)
    if not grouped:
        return []
    dest_dir = Path(output_dir)
    switched = []
    for node in _cmds_ls_files(cmds):
        suffix = _match_suffix(cmds, node, ts_name, grouped)
        if suffix is None:
            continue
        info = grouped[suffix]
        n_tiles = len(info["tiles"])
        name = converted_filename(ts_name, suffix, info["ext"], n_tiles)
        new_path = str(dest_dir / name)
        tiling = UV_TILING_OFF if n_tiles <= 1 else UV_TILING_UDIM
        _record_original(cmds, node)
        _set_file_texture(cmds, node, new_path)
        _set_int(cmds, node, "uvTilingMode", tiling)
        switched.append(node)
    return switched


def revert_ts_file_nodes(cmds, ts_name=None) -> list:
    """属性があるノードだけを原本へ戻し、属性を削除する。属性が無いノードは触らない。"""
    reverted = []
    for node in _cmds_ls_files(cmds):
        if not _has_attr(cmds, node, ATTR_ORIGINAL_PATH) and not _has_attr(
            cmds, node, ATTR_ORIGINAL_TILING
        ):
            continue
        if ts_name:
            if not any(_filename_matches_ts(path, ts_name) for path in _node_path_candidates(cmds, node)):
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
        reverted.append(node)
    return reverted


def _default_cmds():
    import maya.cmds as cmds  # type: ignore
    return cmds


def _default_collect(shape_paths, ts_names, uv_set=None):
    import uvremap_maya as um
    return um.collect_arrays(shape_paths, ts_names, uv_set=uv_set)


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
        self._seq = 0
        self._jobs = {}
        self._seen_roots = {}

    def _now(self) -> str:
        return _as_iso(self.now_fn())

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

    def status_snapshot(self, project_key):
        """パネル(StatusModel)が読む status.json のパス。"""
        return self._root(project_key) / "status.json"

    def running_job_ids(self) -> list:
        return list(self._jobs)

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
        generation = br.next_generation(ledger, ts_name)
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
            lock_timeout_s=opts.get("lock_timeout_s", 60),
        )
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

        self._set_status(root, ts_name, STATE_CONVERTING, generation, "", iso)
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
        return events

    def _finish_job(self, rec: dict, exit_code: int) -> dict:
        iso = self._now()
        ts_name = rec["ts_name"]
        root = rec["root"]
        result_path = root / "jobs" / rec["job_id"] / "result.json"
        result = read_json(result_path, None)
        if not isinstance(result, dict):
            result = {}
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
        state = STATE_FAILED
        cmds = self._cmds()
        if exit_code == 0:
            switched = switch_ts_file_nodes(cmds, ts_name, files, rec["output_dir"])
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
        return {
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
