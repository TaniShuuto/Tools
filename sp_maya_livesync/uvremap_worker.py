# -*- coding: utf-8 -*-
"""UV再マップ ワーカー(別プロセスのジョブ実行器)。契約 CONTRACT.md §4。

    python uvremap_worker.py run <job.json>

標準出力は人間向けログのみ。結果は ``result.json`` を読むこと。
終了コード: 0 成功 / 10 世代古い(破棄) / 11 停止(ガード・Decision STOP・未対応形式) /
12 失敗(例外・プローブ失敗・タイムアウト・ロック取得失敗) / 13 原本が変化し続けた。

設計の要点(不変条件 I1〜I8):
    I1 原本(source_dir)は読み取りのみ。出力先/uvremap_root が原本配下、入力が
       uvremap_root/output 配下なら終了コード 11。
    I2 原本を直接開いて変換しない。jobs/<job_id>/input/ へコピーし、コピー前後で
       source_flag の mtime を比べる。変化していたらやり直し、上限で 13。
    I3 全チャンネル変換 -> プローブ全チャンネル pass/inconclusive -> ステージング ->
       os.replace -> ledger 更新 -> _sync_complete.flag を最後に書く。公開前の失敗では
       出力フォルダと ledger に一切触れない。公開中の失敗はロールバックする。
    I4 ledger の generation より小さい job は何も書かず 10。公開直前にも再確認する。
    I5 uvremap_root/worker.lock で排他(Windows は msvcrt.locking、他は fcntl)。
    I6 公開前に既存出力の sha1 を ledger と照合し、不一致は <name>.edited_<日時> へ退避。
    I7 例外はすべて result.json に残す。status.json は原子的に更新する。
    I8 外部通信・サブプロセス起動をしない(本ファイルは subprocess/socket 等を import しない)。

core(uvremap_core)は遅延 import し、契約 §3 のシグネチャだけに依存する。
ワーカーは世代番号を採番しない(Maya側が採番する)。

標準ライブラリ + numpy のみ(画像 I/O は uvremap_io 経由。Pillow は使わない)。Python 3.10+。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib
import inspect
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

if os.name == "nt":  # Windows: Maya 側の設定ロックと同じ msvcrt.locking 方式
    import msvcrt
    fcntl = None
else:
    import fcntl
    msvcrt = None

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:  # `python -I` 等でもスクリプト隣のモジュールを見つけられるように
    sys.path.insert(0, _HERE)

import numpy as np  # noqa: E402

import uvremap_io  # noqa: E402

__version__ = "0.1.0"

EXIT_OK = 0
EXIT_SUPERSEDED = 10
EXIT_BLOCKED = 11
EXIT_FAILED = 12
EXIT_RETRY_EXHAUSTED = 13

SCHEMA = 1
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_COPY_RETRIES = 3
# ジョブに lock_timeout_s が無いときは timeout_s を使う(この定数は明示指定の参考値)。
DEFAULT_LOCK_TIMEOUT_S = 60.0
DEFAULT_HARD_KILL_GRACE_S = 15.0
LEDGER_LOCK_TIMEOUT_S = 15.0
STATUS_LOCK_TIMEOUT_S = 5.0
TMP_SUFFIX = ".uvremap-tmp"
FLAG_NAME = "_sync_complete.flag"
LOCK_POLL_S = 0.05
_CHUNK = 1024 * 1024
MAX_FILENAME_COMPONENT = 120
MSG_UNSAFE_TS_NAME = "ファイル名に使えないテクスチャセット名です"
WIN_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM{0}".format(i) for i in range(1, 10)]
    + ["LPT{0}".format(i) for i in range(1, 10)]
)
_UNSAFE_FILENAME_CHAR_RE = re.compile(r'[<>"|?*\x00-\x1f]')
_OS_NAME = os.name


# ---------------------------------------------------------------------------
# 例外(終了コードに対応)
# ---------------------------------------------------------------------------
class _Abort(Exception):
    exit_code = EXIT_FAILED
    result_status = "failed"
    state = "FAILED"


class Blocked(_Abort):
    exit_code = EXIT_BLOCKED
    result_status = "blocked"
    state = "BLOCKED"


class Superseded(_Abort):
    exit_code = EXIT_SUPERSEDED
    result_status = "superseded"
    state = None  # 古い世代は status.json に触れない


class RetryExhausted(_Abort):
    exit_code = EXIT_RETRY_EXHAUSTED
    result_status = "retry_exhausted"
    state = "FAILED"


class JobFailed(_Abort):
    pass


class JobTimeout(JobFailed):
    pass


class Interrupted(JobFailed):
    pass


class PublishError(JobFailed):
    pass


class LockTimeout(JobFailed):
    pass


# ---------------------------------------------------------------------------
# 小さな共通関数
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


_HOME_RE = re.compile(r"(?i)(?:[a-z]:)?[\\/](?:home|users)[\\/][^\\/\s'\"<>|:]+")


def _scrub(text) -> str:
    """ログ/結果に載せる文字列から、ホームディレクトリ(ユーザー名を含む)の絶対パスを除く。"""
    text = str(text)
    try:
        home = os.path.expanduser("~")
    except Exception:
        home = ""
    if home and len(home) > 1 and home not in ("/", "\\"):
        text = text.replace(home, "~")
    return _HOME_RE.sub("~", text)


def _log(text: str) -> None:
    try:
        print("[uvremap_worker] {0}".format(_scrub(text)), flush=True)
    except Exception:
        pass


def sha1_file(path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _norm(path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_under(child: str, parent: str) -> bool:
    """child が parent と同じ、または配下か(``_norm`` 済みの文字列同士)。"""
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def _jsonable(obj, _depth: int = 0):
    """result.json に載せられる形へ変換する(numpy / dataclass / NaN などを吸収)。"""
    if _depth > 8:
        return repr(obj)
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if np.isfinite(f) else None
    if isinstance(obj, np.ndarray):
        if obj.size > 64:
            return {"ndarray": list(obj.shape), "dtype": str(obj.dtype)}
        return _jsonable(obj.tolist(), _depth + 1)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = list(obj)
        return [_jsonable(v, _depth + 1) for v in items[:200]]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _jsonable(getattr(obj, k), _depth + 1) for k in obj.__dataclass_fields__}
    if isinstance(obj, Path):
        return obj.name
    return repr(obj)


def write_json_atomic(path, obj, *, tmp_suffix: str | None = None, fsync: bool = True) -> None:
    """一時ファイル -> ``replace_file`` で原子的に JSON を書く(読み取り側は壊れたJSONを見ない)。"""
    p = Path(path)
    suffix = tmp_suffix if tmp_suffix is not None else ".{0}.tmp".format(os.getpid())
    tmp = p.with_name(p.name + suffix)
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        uvremap_io.replace_file(tmp, p)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return default
    return json.loads(raw.decode("utf-8-sig"))


def _format_exc(exc: BaseException) -> tuple[str, str]:
    """(1行の要約, 簡易トレース)。トレースはファイル名のみ(絶対パスを含めない)。"""
    import traceback as _tb
    summary = _scrub("{0}: {1}".format(type(exc).__name__, exc))
    frames = _tb.extract_tb(exc.__traceback__)[-8:]
    lines = ["{0}:{1} in {2}".format(os.path.basename(fr.filename), fr.lineno, fr.name) for fr in frames]
    return summary, " <- ".join(reversed(lines))


# ---------------------------------------------------------------------------
# ファイルロック(I5)
# ---------------------------------------------------------------------------
def _lock_msvcrt(fh, mod) -> None:
    fh.seek(0)
    mod.locking(fh.fileno(), mod.LK_NBLCK, 1)


def _unlock_msvcrt(fh, mod) -> None:
    fh.seek(0)
    mod.locking(fh.fileno(), mod.LK_UNLCK, 1)


def _lock_fcntl(fh) -> None:
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fcntl(fh) -> None:
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class FileLock:
    """OSレベルの排他ロック(プロセスが死ねばOSが解放する)。

    Windows は ``msvcrt.locking``(Maya側 ``_config_file_lock`` と同じ流儀)、
    それ以外は ``fcntl.flock``。ノンブロッキングで ``timeout`` 秒までポーリングし、
    取れなければ ``LockTimeout``。
    """

    def __init__(self, path, timeout: float, poll: float = LOCK_POLL_S, label: str = "lock"):
        self.path = Path(path)
        self.timeout = float(timeout)
        self.poll = poll
        self.label = label
        self._fh = None

    def _try(self, fh) -> None:
        if msvcrt is not None:
            _lock_msvcrt(fh, msvcrt)
        else:
            _lock_fcntl(fh)

    def _unlock(self, fh) -> None:
        if msvcrt is not None:
            _unlock_msvcrt(fh, msvcrt)
        else:
            _unlock_fcntl(fh)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")  # msvcrt.locking がロックするバイト範囲を確保
                fh.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._try(fh)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout("{0} の取得がタイムアウトしました({1}秒)".format(
                            self.path.name, self.timeout))
                    time.sleep(self.poll)
        except BaseException:
            fh.close()
            raise
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            self._unlock(fh)
        except Exception:
            pass
        finally:
            fh.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ---------------------------------------------------------------------------
# job.json の検証
# ---------------------------------------------------------------------------
_REQUIRED_STR = ("job_id", "ts_name", "baseline_dir", "current_snapshot_dir",
                 "source_dir", "output_dir", "uvremap_root")
_COLORSPACES = ("srgb", "raw")
_KINDS = ("color", "scalar", "normal")


def is_windows_reserved_name(name: str) -> bool:
    """Windows の予約デバイス名(CON / NUL / COM1 等)。拡張子より前だけを見る。"""
    stem = name.split(".")[0].upper() if name else ""
    return stem in WIN_RESERVED_NAMES


def has_trailing_dot_or_space(name: str) -> bool:
    return bool(name) and name[-1] in " ."


def filename_component_is_unsafe(name: str) -> bool:
    """予約名・末尾ドット/空白・``<>"|?*``・長さ。``:`` は含めない(呼び出し側で拒否)。"""
    if not isinstance(name, str) or not name:
        return True
    if len(name) > MAX_FILENAME_COMPONENT:
        return True
    if has_trailing_dot_or_space(name):
        return True
    if is_windows_reserved_name(name):
        return True
    if _UNSAFE_FILENAME_CHAR_RE.search(name):
        return True
    return False


def is_safe_output_name(name) -> bool:
    """台帳 ``outputs`` のキー。パス区切りや ``..`` を含む名前は使わない。"""
    if not isinstance(name, str) or not name:
        return False
    if Path(name).name != name:
        return False
    if ".." in name or "/" in name or "\\" in name or "\0" in name:
        return False
    return True


def _safe_component(value, what: str) -> str:
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise JobFailed("job.json の {0} が不正です".format(what))
    if any(c in value for c in ("/", "\\", ":", "\0")):
        raise JobFailed("job.json の {0} にパス区切り文字は使えません: {1!r}".format(what, value))
    if filename_component_is_unsafe(value):
        if what == "ts_name":
            raise Blocked(MSG_UNSAFE_TS_NAME)
        raise JobFailed("job.json の {0} がファイル名に使えません: {1!r}".format(what, value))
    return value


def _num(value, default: float, what: str, minimum: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JobFailed("job.json の {0} が数値ではありません".format(what))
    if value < minimum:
        raise JobFailed("job.json の {0} が小さすぎます".format(what))
    return float(value)


def parse_job(raw) -> dict:
    if not isinstance(raw, dict):
        raise JobFailed("job.json の最上位がオブジェクトではありません")
    if raw.get("schema", SCHEMA) != SCHEMA:
        raise JobFailed("job.json の schema が未対応です: {0!r}".format(raw.get("schema")))
    job = dict(raw)
    for key in _REQUIRED_STR:
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise JobFailed("job.json に必須項目がありません: {0}".format(key))
    _safe_component(raw["job_id"], "job_id")
    _safe_component(raw["ts_name"], "ts_name")
    gen = raw.get("generation")
    if isinstance(gen, bool) or not isinstance(gen, int):
        raise JobFailed("job.json の generation が整数ではありません")
    job["generation"] = gen
    chans = raw.get("channels")
    if not isinstance(chans, list) or not chans:
        raise JobFailed("job.json の channels が空です")
    seen = set()
    norm_ch = []
    for ch in chans:
        if not isinstance(ch, dict):
            raise JobFailed("job.json の channels の要素が不正です")
        suffix = _safe_component(ch.get("suffix"), "channels[].suffix")
        if suffix in seen:
            raise JobFailed("job.json の channels に重複があります: {0}".format(suffix))
        seen.add(suffix)
        if ch.get("colorspace") not in _COLORSPACES:
            raise JobFailed("channels[{0}].colorspace が不正です".format(suffix))
        if ch.get("kind") not in _KINDS:
            raise JobFailed("channels[{0}].kind が不正です".format(suffix))
        norm_ch.append({
            "suffix": suffix, "colorspace": ch["colorspace"], "kind": ch["kind"],
            "normal_green_down": bool(ch.get("normal_green_down", False)),
        })
    job["channels"] = norm_ch
    job["udim"] = bool(raw.get("udim", True))
    job["dilate_px"] = int(_num(raw.get("dilate_px"), 16, "dilate_px"))
    job["normal_mode"] = raw.get("normal_mode", "uv") or "uv"
    job["timeout_s"] = _num(raw.get("timeout_s"), DEFAULT_TIMEOUT_S, "timeout_s", 0.01)
    job["copy_retries"] = int(_num(raw.get("copy_retries"), DEFAULT_COPY_RETRIES, "copy_retries"))
    job["lock_timeout_s"] = _num(raw.get("lock_timeout_s"), job["timeout_s"], "lock_timeout_s")
    job["hard_kill_grace_s"] = _num(raw.get("hard_kill_grace_s"), DEFAULT_HARD_KILL_GRACE_S,
                                    "hard_kill_grace_s")
    out_res = raw.get("out_res")
    if isinstance(out_res, dict):
        try:
            out_res = {int(k): (int(v[0]), int(v[1])) for k, v in out_res.items()}
        except Exception:
            raise JobFailed("job.json の out_res が不正です")
    elif out_res is not None and (isinstance(out_res, bool) or not isinstance(out_res, int)):
        raise JobFailed("job.json の out_res が不正です")
    job["out_res"] = out_res
    if raw.get("source_flag") is not None and not isinstance(raw.get("source_flag"), str):
        raise JobFailed("job.json の source_flag が不正です")
    if raw.get("latest_generation_path") is not None and not isinstance(raw.get("latest_generation_path"), str):
        raise JobFailed("job.json の latest_generation_path が不正です")
    return job


# ---------------------------------------------------------------------------
# core の遅延 import
# ---------------------------------------------------------------------------
_CORE_REQUIRED = ("load_snapshot", "classify", "convert_channel", "probe_verify", "ChannelSpec")


def _import_core():
    try:
        core = importlib.import_module("uvremap_core")
    except Exception as exc:
        raise JobFailed("uvremap_core を読み込めません: {0}: {1}".format(type(exc).__name__, exc))
    missing = [n for n in _CORE_REQUIRED if not hasattr(core, n)]
    if missing:
        raise JobFailed("uvremap_core に必要な API がありません: {0}".format(", ".join(missing)))
    return core


def _accepts_kwarg(func, name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _core_error_kind(exc: BaseException) -> str | None:
    """uvremap_core の例外の種類("ConversionStopped" 等)。core の例外でなければ None。

    core は遅延 import でスタブに差し替えられることもあるため、クラスの実体ではなく
    「uvremap_core モジュールで定義された例外クラスの名前」で見分ける。
    """
    for cls in type(exc).__mro__:
        if getattr(cls, "__module__", "") == "uvremap_core" and cls.__name__ in (
                "ConversionStopped", "TextureSetNotFound", "SnapshotError", "ChannelFormatError"):
            return cls.__name__
    return None


def _translate_core_error(exc: BaseException) -> "_Abort | None":
    """core の例外を、契約の終了コード(11 停止 / 12 失敗)に対応する ``_Abort`` へ写す。

    ConversionStopped(Decision が STOP、変換できる面が無い)、TextureSetNotFound、
    ChannelFormatError(入力テクスチャの形式が変換できない)は 11。SnapshotError は 12。
    それ以外の core の例外は呼び出し側の「予期しない例外」(12)に任せる。
    """
    kind = _core_error_kind(exc)
    if kind == "ConversionStopped":
        reasons = getattr(exc, "reasons", None) or [str(exc)]
        return Blocked("変換を停止しました(理由: {0})".format(", ".join(str(r) for r in reasons)))
    if kind == "TextureSetNotFound":
        return Blocked("テクスチャセットがスナップショットにありません(ts_missing): {0}".format(exc))
    if kind == "ChannelFormatError":
        return Blocked("入力テクスチャの形式が変換できません: {0}".format(exc))
    if kind == "SnapshotError":
        return JobFailed("スナップショットを読めません/不正です: {0}".format(exc))
    return None


# ---------------------------------------------------------------------------
# ジョブ本体
# ---------------------------------------------------------------------------
class _Run:
    def __init__(self, job_path):
        self.job_path = Path(job_path)
        self.job: dict | None = None
        self.messages: list[dict] = []
        self.timings: dict[str, float] = {}
        self.probe: dict = {}
        self.stats: dict = {}
        self.files: list[dict] = []
        self.method: str | None = None
        self.generation: int | None = None
        self.job_id: str | None = None
        self.ts_name: str | None = None
        self.root: Path | None = None
        self.job_dir: Path | None = None
        self.exit_code: int | None = None
        self._final_lock = threading.Lock()
        self._finalized = False
        # タイムアウト / ウォッチドッグ
        self._deadline: float | None = None
        self._timed_out = False
        self._in_publish = False
        self._phase_lock = threading.Lock()
        self._wd_stop = threading.Event()
        self._wd_thread: threading.Thread | None = None
        self._src_norm: str | None = None
        self._status_prev = None
        self._status_prev_captured = False
        self._core = None

    # ------------------------------------------------------------ messages
    def msg(self, level: str, text: str) -> None:
        text = _scrub(text)
        self.messages.append({"level": level, "text": text})
        _log("{0}: {1}".format(level, text))

    def _tick(self) -> None:
        """協調的なタイムアウト確認(各ステップの境界で呼ぶ)。"""
        if self._timed_out or (self._deadline is not None and time.monotonic() > self._deadline):
            self._timed_out = True
            raise JobTimeout("タイムアウト({0}秒)".format(self.job["timeout_s"]))

    def _may_write(self, path) -> bool:
        """I1: 原本フォルダ配下には何も書かない(ガード違反時の result/status も含む)。"""
        return self._src_norm is None or not _is_under(_norm(path), self._src_norm)

    # --------------------------------------------------------------- status
    def _status_path(self) -> Path:
        return self.root / "status.json"

    def set_status(self, state: str, message: str = "") -> None:
        """status.json の TS 行を更新する(原子的)。古い世代で新しい世代の行を戻さない。"""
        if self.root is None or self.ts_name is None or not self._may_write(self.root):
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with FileLock(self.root / "status.lock", STATUS_LOCK_TIMEOUT_S, label="status"):
                data = read_json(self._status_path(), None)
                if not isinstance(data, dict):
                    data = {"schema": SCHEMA, "ts": {}}
                data.setdefault("schema", SCHEMA)
                table = data.setdefault("ts", {})
                if not isinstance(table, dict):
                    table = data["ts"] = {}
                cur = table.get(self.ts_name)
                if isinstance(cur, dict) and isinstance(cur.get("generation"), int) \
                        and cur["generation"] > self.generation:
                    return
                if not self._status_prev_captured:
                    self._status_prev = cur if isinstance(cur, dict) else None
                    self._status_prev_captured = True
                table[self.ts_name] = {
                    "state": state, "generation": self.generation, "updated": _now_iso(),
                    "message": _scrub(message), "job_id": self.job_id, "pid": os.getpid(),
                }
                write_json_atomic(self._status_path(), data)
        except Exception as exc:  # status を書けなくても、理由は result.json に残す
            summary, _tb = _format_exc(exc)
            self.msg("error", "status.json を更新できません: " + summary)

    def restore_status(self) -> None:
        """破棄(Superseded)時、自分が書いた CONVERTING を元の行へ戻す(古い表示を残さない)。"""
        if not self._status_prev_captured or self.root is None or not self._may_write(self.root):
            return
        try:
            with FileLock(self.root / "status.lock", STATUS_LOCK_TIMEOUT_S, label="status"):
                data = read_json(self._status_path(), None)
                if not isinstance(data, dict) or not isinstance(data.get("ts"), dict):
                    return
                cur = data["ts"].get(self.ts_name)
                if not isinstance(cur, dict) or cur.get("job_id") != self.job_id:
                    return  # 既に別ジョブが書き換えている
                if self._status_prev is None:
                    del data["ts"][self.ts_name]
                else:
                    data["ts"][self.ts_name] = self._status_prev
                write_json_atomic(self._status_path(), data)
        except Exception as exc:
            summary, _tb = _format_exc(exc)
            self.msg("warn", "status.json を元に戻せません: " + summary)

    # --------------------------------------------------------------- result
    def _result_dict(self, exit_code: int, status: str) -> dict:
        return {
            "schema": SCHEMA, "job_id": self.job_id, "generation": self.generation,
            "status": status, "exit_code": exit_code, "ts_name": self.ts_name,
            "method": self.method, "files": self.files, "probe": self.probe,
            "stats": _jsonable(self.stats), "messages": self.messages,
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
        }

    def _write_results(self, exit_code: int, status: str) -> None:
        data = self._result_dict(exit_code, status)
        targets = []
        if self.job_dir is not None:
            targets.append(self.job_dir / "result.json")
        targets.append(self.job_path.with_name("result.json"))
        done = set()
        for t in targets:
            key = _norm(t)
            if key in done or not self._may_write(t):
                continue
            done.add(key)
            try:
                t.parent.mkdir(parents=True, exist_ok=True)
                write_json_atomic(t, data)
            except Exception as exc:
                summary, _tb = _format_exc(exc)
                _log("error: result.json を書けません: " + summary)

    def finish(self, exit_code: int, result_status: str, state: str | None, text: str = "") -> int:
        """結果の確定。1回だけ実行される(ウォッチドッグとの競合を避ける)。"""
        with self._final_lock:
            if self._finalized:
                return self.exit_code if self.exit_code is not None else exit_code
            self._finalized = True
            self.exit_code = exit_code
        self._write_results(exit_code, result_status)
        if state is not None:
            self.set_status(state, text)
        return exit_code

    # ------------------------------------------------------------ watchdog
    def _start_watchdog(self) -> None:
        timeout = self.job["timeout_s"]
        self._deadline = time.monotonic() + timeout
        self._wd_thread = threading.Thread(target=self._watchdog_main, name="uvremap-watchdog",
                                           daemon=True)
        self._wd_thread.start()

    def _stop_watchdog(self) -> None:
        self._wd_stop.set()
        t = self._wd_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)

    def _hard_exit(self, code: int) -> None:  # テストで差し替え可能
        os._exit(code)

    def _watchdog_main(self) -> None:
        remaining = self._deadline - time.monotonic()
        if self._wd_stop.wait(max(0.0, remaining)):
            return
        self._timed_out = True
        if self._wd_stop.wait(self.job["hard_kill_grace_s"]):
            return
        # 協調的に止まらなかった(巨大な numpy 呼び出しの最中など)。公開中でなければ強制終了。
        while not self._wd_stop.is_set():
            with self._phase_lock:
                if not self._in_publish:
                    text = "タイムアウト({0}秒)後も処理が終わらないため強制終了します".format(
                        self.job["timeout_s"])
                    self.msg("error", text)
                    self.finish(EXIT_FAILED, "failed", "FAILED", text)
                    self._hard_exit(EXIT_FAILED)
                    return
            self._wd_stop.wait(0.2)  # 公開中は短時間なので終わるのを待つ

    # ------------------------------------------------------------- guards
    def _guard_paths(self) -> None:
        j = self.job
        src = _norm(j["source_dir"])
        out = _norm(j["output_dir"])
        root = _norm(j["uvremap_root"])
        if _is_under(out, src):
            raise Blocked("出力先が原本フォルダ(source_dir)配下です。原本は変更しません(I1)")
        if _is_under(src, out):
            raise Blocked("原本フォルダが出力先配下にあります。原本は変更しません(I1)")
        if _is_under(root, src):
            raise Blocked("uvremap_root が原本フォルダ配下です。原本は変更しません(I1)")
        if _is_under(src, os.path.join(root, "output")):
            raise Blocked("入力が uvremap_root/output 配下です。変換結果は入力にできません(多重変換防止)")

    # ----------------------------------------------------------- ledger io
    def _ledger_path(self) -> Path:
        return self.root / "ledger.json"

    def _read_ledger(self) -> tuple[dict, bytes | None]:
        try:
            raw = self._ledger_path().read_bytes()
        except FileNotFoundError:
            return {"schema": SCHEMA, "ts": {}}, None
        try:
            obj = json.loads(raw.decode("utf-8-sig"))
        except Exception as exc:
            raise JobFailed("ledger.json を読めません(壊れている可能性): {0}".format(exc))
        if not isinstance(obj, dict):
            raise JobFailed("ledger.json の形式が不正です")
        if not isinstance(obj.get("ts"), dict):
            obj["ts"] = {}
        obj.setdefault("schema", SCHEMA)
        return obj, raw

    def _ledger_entry(self, ledger: dict) -> dict:
        e = ledger["ts"].get(self.ts_name)
        return e if isinstance(e, dict) else {}

    def _check_generation(self, entry: dict) -> None:
        led_gen = entry.get("generation", 0)
        if isinstance(led_gen, int) and not isinstance(led_gen, bool) and led_gen > self.generation:
            raise Superseded("世代 {0} は台帳の世代 {1} より古いため破棄します".format(
                self.generation, led_gen))
        req = self._requested_generation()
        if req is not None and req > self.generation:
            raise Superseded("世代 {0} は要求済みの世代 {1} より古いため破棄します".format(
                self.generation, req))

    def _requested_generation(self) -> int | None:
        """任意: job の ``latest_generation_path``(Maya が最新要求の世代を書く)を読む。"""
        path = self.job.get("latest_generation_path")
        if not path:
            return None
        try:
            raw = Path(path).read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            return None
        except Exception as exc:
            self._warn_once("latest_generation_path を読めません: {0}".format(exc))
            return None
        try:
            if raw.startswith("{"):
                value = json.loads(raw).get("generation")
            else:
                value = int(raw)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("整数ではありません")
            return value
        except Exception as exc:
            self._warn_once("latest_generation_path の内容が不正です: {0}".format(exc))
            return None

    def _warn_once(self, text: str) -> None:
        if not any(m["text"] == _scrub(text) for m in self.messages):
            self.msg("warn", text)

    # ------------------------------------------------------------ pipeline
    def execute(self) -> None:
        j = self.job
        self._guard_paths()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(self.root / "worker.lock", j["lock_timeout_s"], label="worker"):
                self._start_watchdog()
                try:
                    self._locked_body()
                finally:
                    self._stop_watchdog()
        except LockTimeout:
            req = self._requested_generation()
            if req is not None and req > self.generation:
                raise Superseded("世代 {0} は要求済みの世代 {1} より古いため破棄します".format(
                    self.generation, req))
            raise

    def _locked_body(self) -> None:
        j = self.job
        ledger, _raw = self._read_ledger()
        entry = self._ledger_entry(ledger)
        self._check_generation(entry)  # I4: 古ければ何も書かず 10
        if entry.get("frozen"):
            raise Blocked("このテクスチャセットは確定(frozen)されているため自動変換しません")
        self.set_status("CONVERTING", "")
        self._tick()

        t0 = time.monotonic()
        input_dir, found, sources = self._copy_inputs()
        self.timings["copy"] = time.monotonic() - t0
        self._tick()

        core = _import_core()
        self._core = core
        t1 = time.monotonic()
        base = self._load_snapshot(core, j["baseline_dir"], "基準")
        cur = self._load_snapshot(core, j["current_snapshot_dir"], "現行")
        decision = core.classify(base, cur, self.ts_name)
        self.method = getattr(decision, "method", None)
        for w in getattr(decision, "warnings", None) or []:
            self.msg("warn", "判定の警告: {0}".format(w))
        if self.method not in ("F", "F_FLIPPED"):
            reasons = ", ".join(str(r) for r in (getattr(decision, "reasons", None) or [])) or "unknown"
            raise Blocked("変換を停止しました(判定 {0}): {1}".format(self.method, reasons))
        self._tick()

        staged = self._convert_all(core, base, cur, decision, input_dir, found)
        self.timings["convert"] = time.monotonic() - t1 - self.timings.get("probe", 0.0)

        t2 = time.monotonic()
        self._publish(staged, found, sources, base, cur)
        self.timings["publish"] = time.monotonic() - t2

    def _load_snapshot(self, core, path, label: str):
        try:
            return core.load_snapshot(Path(path))
        except Exception as exc:
            if _core_error_kind(exc) == "SnapshotError":
                raise JobFailed("{0}スナップショットを読めません/不正です: {1}".format(label, exc)) from exc
            raise

    # --------------------------------------------------------- 入力の発見
    def _discover(self) -> dict[str, dict]:
        """source_dir を読み取りだけで走査し {suffix: {"ext": str, "files": {tile: name}}} を返す。"""
        j = self.job
        src = Path(j["source_dir"])
        try:
            names = sorted(os.listdir(src))
        except OSError as exc:
            raise JobFailed("原本フォルダを読めません: {0}".format(exc))
        found: dict[str, dict] = {}
        flags = re.IGNORECASE if _OS_NAME == "nt" else 0
        for ch in j["channels"]:
            suffix = ch["suffix"]
            pat = re.compile(r"^{0}_{1}(?:\.(\d{{4}}))?\.([^.]+)$".format(
                re.escape(self.ts_name), re.escape(suffix)), flags)
            cands = []  # (tile|None, ext, name, mtime_ns)
            for n in names:
                m = pat.match(n)
                if not m:
                    continue
                p = src / n
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not os.path.isfile(p):
                    continue
                cands.append((int(m.group(1)) if m.group(1) else None, m.group(2), n, st.st_mtime_ns))
            if not cands:
                self.msg("info", "チャンネル {0}: 入力ファイルが無いためスキップ".format(suffix))
                continue
            # 拡張子が複数あれば最新のグループ、タイル有無が混在すれば最新のグループを使う
            exts = {c[1].lower() for c in cands}
            if len(exts) > 1:
                best = max(exts, key=lambda e: max(c[3] for c in cands if c[1].lower() == e))
                self.msg("warn", "チャンネル {0}: 拡張子が複数あるため最新の .{1} を使用".format(suffix, best))
                cands = [c for c in cands if c[1].lower() == best]
            tiled = [c for c in cands if c[0] is not None]
            plain = [c for c in cands if c[0] is None]
            if tiled and plain:
                if max(c[3] for c in tiled) >= max(c[3] for c in plain):
                    chosen = tiled
                else:
                    chosen = plain
                self.msg("warn", "チャンネル {0}: タイル付きと無しのファイルが混在。最新側を使用".format(suffix))
            else:
                chosen = tiled or plain
            files = {}
            for tile, _ext, name, _mt in chosen:
                files[tile if tile is not None else 1001] = name
            found[suffix] = {"ext": "." + chosen[0][1], "files": files}
        return found

    # ---------------------------------------------- I2: コピーとflag確認
    @staticmethod
    def _flag_stamp(flag: Path | None):
        if flag is None:
            return None
        try:
            st = os.stat(flag)
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _copy_input(self, src: Path, dst: Path) -> str:
        """原本を読み取り専用で短時間だけ開き、コピーしながら sha1 を返す。"""
        h = hashlib.sha1()
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                chunk = fi.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                fo.write(chunk)
        return h.hexdigest()

    def _copy_inputs(self):
        j = self.job
        src_dir = Path(j["source_dir"])
        flag = Path(j["source_flag"]) if j.get("source_flag") else None
        input_dir = self.job_dir / "input"
        attempts = 1 + j["copy_retries"]
        last_error: Exception | None = None
        if flag is not None and not flag.exists():
            self.msg("warn", "source_flag が存在しません(書き出し完了を確認できないまま続行)")
        for attempt in range(1, attempts + 1):
            self._tick()
            shutil.rmtree(input_dir, ignore_errors=True)
            input_dir.mkdir(parents=True, exist_ok=True)
            before = self._flag_stamp(flag)
            found = self._discover()
            if not found:
                raise Blocked("入力テクスチャが見つかりません(ts={0})".format(self.ts_name))
            sources: dict[str, str] = {}
            try:
                for suffix in sorted(found):
                    for tile, name in sorted(found[suffix]["files"].items()):
                        sources[name] = self._copy_input(src_dir / name, input_dir / name)
            except OSError as exc:
                last_error = exc
                self.msg("warn", "コピーに失敗(試行 {0}/{1}): {2}".format(attempt, attempts, exc))
                time.sleep(0.05)
                continue
            after = self._flag_stamp(flag)
            if before == after:
                if attempt > 1:
                    self.msg("info", "コピーを {0} 回目で確定しました".format(attempt))
                return input_dir, found, sources
            last_error = None
            self.msg("warn", "コピー中に source_flag が変化したためやり直し(試行 {0}/{1})".format(
                attempt, attempts))
            time.sleep(0.05)
        if last_error is not None:
            raise JobFailed("原本のコピーに失敗しました: {0}".format(last_error))
        raise RetryExhausted("原本が変化し続けたため変換できません(試行 {0} 回)".format(attempts))

    # --------------------------------------------------- 変換とプローブ
    def _out_res(self, old_tiles: dict):
        res = self.job["out_res"]
        if res is not None:
            return res
        sizes = {tuple(a.shape[1::-1]) for a in old_tiles.values()}  # (W, H)
        if len(sizes) == 1:
            w, h = next(iter(sizes))
            if w == h:
                return int(w)
        return {t: (int(a.shape[1]), int(a.shape[0])) for t, a in old_tiles.items()}

    @staticmethod
    def _meta_for_output(ref_meta: dict, arr: np.ndarray) -> dict:
        channels = arr.shape[2] if arr.ndim == 3 else 1
        meta = dict(ref_meta)
        bit_depth = int(meta.get("bit_depth", 8))
        if meta.get("mode") not in uvremap_io.MODE_CHANNELS \
                or uvremap_io.MODE_CHANNELS[meta["mode"]] != channels:
            meta["mode"] = uvremap_io.mode_for_channels(bit_depth, channels)
        return meta

    def _convert_all(self, core, base, cur, decision, input_dir: Path, found: dict):
        j = self.job
        staging = self.job_dir / "staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        staged: dict[str, dict[int, dict]] = {}
        probe_failed: list[str] = []
        probe_time = 0.0
        convert_func = core.convert_channel
        extra = {}
        if _accepts_kwarg(convert_func, "udim"):
            extra["udim"] = j["udim"]
        # プローブは変換と同じ udim / normal_mode で検証する(QUESTIONS_core Q4)。
        # 渡さないと、非UDIMや normal_mode="off" の変換が「別の変換」と比べられて誤って fail する。
        probe_extra = {}
        if _accepts_kwarg(core.probe_verify, "udim"):
            probe_extra["udim"] = j["udim"]
        if _accepts_kwarg(core.probe_verify, "normal_mode"):
            probe_extra["normal_mode"] = j["normal_mode"]
        for ch in j["channels"]:
            suffix = ch["suffix"]
            info = found.get(suffix)
            if info is None:
                continue
            self._tick()
            old_tiles: dict[int, np.ndarray] = {}
            metas: dict[int, dict] = {}
            try:
                for tile, name in sorted(info["files"].items()):
                    old_tiles[tile], metas[tile] = uvremap_io.read_image(input_dir / name)
                    self._tick()
            except uvremap_io.UnsupportedFormat as exc:
                raise Blocked(str(exc))
            except uvremap_io.ImageIOError as exc:
                raise JobFailed(str(exc))
            spec = core.ChannelSpec(suffix=suffix, colorspace=ch["colorspace"], kind=ch["kind"],
                                    normal_green_down=ch["normal_green_down"])
            result = convert_func(old_tiles, base, cur, self.ts_name, decision, spec,
                                  self._out_res(old_tiles), dilate_px=j["dilate_px"],
                                  normal_mode=j["normal_mode"], **extra)
            self._tick()
            tiles = getattr(result, "tiles", None)
            if not isinstance(tiles, dict) or not tiles:
                raise JobFailed("チャンネル {0}: convert_channel が出力タイルを返しませんでした".format(suffix))
            self.stats[suffix] = getattr(result, "stats", {}) or {}

            tp = time.monotonic()
            report = core.probe_verify(old_tiles, tiles, base, cur, self.ts_name, spec, **probe_extra)
            probe_time += time.monotonic() - tp
            self.timings["probe"] = probe_time
            self._record_probe(suffix, report, probe_failed)
            self._tick()
            if probe_failed:
                continue  # 失敗が確定したら、残りは検証のみ(ステージングしない)

            ref_tile = min(metas)
            staged[suffix] = {}
            for tile, arr in sorted(tiles.items()):
                arr = np.asarray(arr)
                meta = self._meta_for_output(metas[ref_tile], arr)
                path = staging / "{0}.{1}.png".format(suffix, int(tile))
                try:
                    uvremap_io.write_image(path, arr, meta)
                except uvremap_io.UnsupportedFormat as exc:
                    raise Blocked(str(exc))
                staged[suffix][int(tile)] = {"path": path, "sha1": sha1_file(path)}
                self._tick()
            del old_tiles, tiles, result
        if probe_failed:
            raise JobFailed("3Dプローブ検証に失敗したため公開しません: {0}".format(", ".join(probe_failed)))
        if not staged:
            raise Blocked("変換対象のチャンネルがありません")
        return staged

    def _record_probe(self, suffix: str, report, failed: list) -> None:
        status = str(getattr(report, "status", "inconclusive"))
        entry = {
            "status": status,
            "n_total": _jsonable(getattr(report, "n_total", None)),
            "n_valid": _jsonable(getattr(report, "n_valid", None)),
            "n_bad": _jsonable(getattr(report, "n_bad", None)),
            "max_err": _jsonable(getattr(report, "max_err", None)),
            "mean_err": _jsonable(getattr(report, "mean_err", None)),
        }
        bad = getattr(report, "bad_points", None)
        if bad:
            entry["bad_points"] = _jsonable(list(bad)[:20])
        self.probe[suffix] = entry
        if status == "fail":
            failed.append(suffix)
            self.msg("error", "プローブ失敗: {0} (不一致 {1}/{2}, 最大誤差 {3})".format(
                suffix, entry["n_bad"], entry["n_valid"], entry["max_err"]))
        elif status != "pass":
            self.msg("warn", "プローブ判定不能(inconclusive): {0}。採用しますが確認してください".format(suffix))

    # ---------------------------------------------------------------- 公開
    def _output_names(self, staged: dict, found: dict) -> dict[tuple[str, int], str]:
        tile_sets = {s: set(t) for s, t in staged.items()}
        union = set().union(*tile_sets.values())
        if any(ts != union for ts in tile_sets.values()):
            self.msg("warn", "チャンネル間で出力タイル集合が異なります: " + "; ".join(
                "{0}={1}".format(s, sorted(t)) for s, t in sorted(tile_sets.items())))
        single = len(union) == 1
        names = {}
        for suffix, tiles in staged.items():
            ext = found[suffix]["ext"]
            for tile in tiles:
                if single:
                    names[(suffix, tile)] = "{0}_{1}{2}".format(self.ts_name, suffix, ext)
                else:
                    names[(suffix, tile)] = "{0}_{1}.{2}{3}".format(self.ts_name, suffix, tile, ext)
        return names

    def _meta_info(self, cur=None) -> tuple[str, str]:
        """(uv_hash, baseline_id)。job > core.compute_uv_hash(cur, ts) > スナップショットの meta.json の順。

        meta.json の ``uv_hash`` はメッシュ全体の値で、別のテクスチャセットの UV を編集しても変わる。
        D-3(現行UVハッシュとの比較)は TS 単位の値で行うので、job が渡さないときは
        契約 §3 の ``compute_uv_hash(snap, ts_name)`` で TS 単位の値を求める。
        """
        j = self.job
        uv_hash = j.get("uv_hash")
        baseline_id = j.get("baseline_id")
        fn = getattr(self._core, "compute_uv_hash", None)
        if not uv_hash and cur is not None and callable(fn):
            try:
                uv_hash = fn(cur, self.ts_name)
            except Exception as exc:
                self.msg("warn", "TS単位の uv_hash を計算できません(メッシュ全体の値を使います): {0}".format(exc))
                uv_hash = None
        if not uv_hash:
            try:
                meta = read_json(Path(j["current_snapshot_dir"]) / "meta.json", {}) or {}
                uv_hash = meta.get("uv_hash")
            except Exception:
                uv_hash = None
        if not baseline_id:
            try:
                meta = read_json(Path(j["baseline_dir"]) / "meta.json", {}) or {}
                baseline_id = meta.get("baseline_id")
            except Exception:
                baseline_id = None
        return uv_hash or "", baseline_id or Path(j["baseline_dir"]).name

    @staticmethod
    def _unique_aside(path: Path, stamp: str) -> Path:
        cand = path.with_name("{0}.edited_{1}".format(path.name, stamp))
        n = 1
        while cand.exists():
            n += 1
            cand = path.with_name("{0}.edited_{1}_{2}".format(path.name, stamp, n))
        return cand

    def _journal_path(self) -> Path:
        return self.root / "publish_journal.{0}.json".format(self.ts_name)

    def _output_root_norm(self) -> str:
        return _norm(Path(self.job["uvremap_root"]) / "output")

    def _confine_output_path(self, out: Path, name: str, *, warn: bool = True):
        """台帳キーや出力名を ``uvremap_root/output`` 配下のファイル名に閉じ込める。"""
        if not is_safe_output_name(name):
            if warn:
                self.msg("warn", "台帳の出力名を無視しました(安全でない名前): {0}".format(name))
            return None
        dest = out / name
        if not _is_under(_norm(dest), self._output_root_norm()):
            if warn:
                self.msg("warn", "台帳の出力名を無視しました(出力フォルダの外): {0}".format(name))
            return None
        return dest

    def _backup(self, target: Path, tag: int) -> Path:
        bdir = self.job_dir / "backup"
        bdir.mkdir(parents=True, exist_ok=True)
        dst = bdir / "{0:03d}_{1}".format(tag, target.name)
        shutil.copy2(target, dst)
        return dst

    @staticmethod
    def _restore(backup: Path, target: Path) -> None:
        tmp = target.with_name(target.name + TMP_SUFFIX)
        shutil.copy2(backup, tmp)
        uvremap_io.replace_file(tmp, target)

    def _publish(self, staged: dict, found: dict, sources: dict, base, cur) -> None:
        j = self.job
        out = Path(j["output_dir"])
        names = self._output_names(staged, found)
        new_files: dict[str, dict] = {}  # name -> {"staged": Path, "sha1": str, "tile": int}
        for (suffix, tile), name in names.items():
            s = staged[suffix][tile]
            new_files[name] = {"staged": s["path"], "sha1": s["sha1"], "tile": tile}
        uv_hash, baseline_id = self._meta_info(cur)

        # ここから先はタイムアウトで強制終了されない(公開は短時間)
        with self._phase_lock:
            if self._timed_out:
                raise JobTimeout("タイムアウト({0}秒)".format(j["timeout_s"]))
            self._in_publish = True

        with FileLock(self.root / "ledger.lock", LEDGER_LOCK_TIMEOUT_S, label="ledger"):
            ledger, ledger_raw = self._read_ledger()
            entry = self._ledger_entry(ledger)
            self._check_generation(entry)  # I4: 完了直前の再確認
            if entry.get("frozen"):
                raise Blocked("このテクスチャセットは確定(frozen)されているため公開しません")
            led_outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
            journal = {}
            try:
                journal = (read_json(self._journal_path(), {}) or {}).get("outputs", {}) or {}
            except Exception:
                journal = {}

            out.mkdir(parents=True, exist_ok=True)
            for stale_tmp in out.glob("*" + TMP_SUFFIX):  # 以前の強制終了の残骸
                try:
                    stale_tmp.unlink()
                except OSError:
                    pass

            # I6: 手編集の検出(読み取りのみ)。台帳キーは出力フォルダ内のファイル名だけ使う。
            edited: list[Path] = []
            skipped_ledger = set()
            for name in sorted(set(led_outputs) | set(new_files)):
                if name in led_outputs and name not in new_files:
                    confined = self._confine_output_path(out, name)
                    if confined is None:
                        skipped_ledger.add(name)
                        continue
                    p = confined
                else:
                    p = out / name
                    if name in new_files and self._confine_output_path(out, name, warn=False) is None:
                        raise PublishError("出力先が uvremap_root/output の外です: {0}".format(name))
                if not p.is_file():
                    continue
                sha = sha1_file(p)
                known = {led_outputs.get(name), journal.get(name)} - {None}
                if sha in known or (name in new_files and sha == new_files[name]["sha1"]):
                    continue
                edited.append(p)

            # 一時ファイルを出力フォルダ内に作る(同一ボリュームなので os.replace が原子的)
            tmp_paths: dict[str, Path] = {n: out / (n + TMP_SUFFIX) for n in new_files}
            try:
                for name, nf in new_files.items():
                    got = self._copy_input(nf["staged"], tmp_paths[name])
                    if got != nf["sha1"]:
                        raise PublishError("コピーの検証に失敗: {0}".format(name))
            except Exception:
                for tmp in tmp_paths.values():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                raise

            new_outputs = {n: nf["sha1"] for n, nf in new_files.items()}
            write_json_atomic(self._journal_path(), {
                "schema": SCHEMA, "job_id": self.job_id, "generation": self.generation,
                "ts": self.ts_name, "outputs": new_outputs, "started": _now_iso()})

            undo: list = []
            tag = 0
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            flag_path = out / FLAG_NAME
            try:
                for p in edited:
                    aside = self._unique_aside(p, stamp)
                    uvremap_io.replace_file(p, aside)
                    undo.append(lambda p=p, aside=aside: uvremap_io.replace_file(aside, p))
                    self.msg("warn", "出力 {0} が手編集されていたため {1} へ退避しました".format(p.name, aside.name))

                if flag_path.exists():  # 公開中に「完了」と見えないよう、先に外す(失敗時は戻す)
                    tag += 1
                    bk = self._backup(flag_path, tag)
                    undo.append(lambda bk=bk: self._restore(bk, flag_path))
                    flag_path.unlink()

                for name in sorted(new_files):
                    target = out / name
                    if target.exists():
                        tag += 1
                        bk = self._backup(target, tag)
                        undo.append(lambda bk=bk, t=target: self._restore(bk, t))
                    else:
                        undo.append(lambda t=target: t.unlink() if t.exists() else None)
                    uvremap_io.replace_file(tmp_paths[name], target)

                for name in sorted(set(led_outputs) - set(new_files)):  # タイル数変更で不要になった旧出力
                    if name in skipped_ledger:
                        continue
                    stale = self._confine_output_path(out, name)
                    if stale is None or not stale.is_file():
                        continue
                    tag += 1
                    bk = self._backup(stale, tag)
                    undo.append(lambda bk=bk, t=stale: self._restore(bk, t))
                    stale.unlink()
                    self.msg("info", "不要になった旧出力を削除しました: {0}".format(name))

                prev_gen = entry.get("generation", 0)
                prev_gen = prev_gen if isinstance(prev_gen, int) and not isinstance(prev_gen, bool) else 0
                new_entry = dict(entry)
                new_entry.update({
                    "generation": max(self.generation, prev_gen),
                    "last_good_generation": self.generation, "uv_hash": uv_hash,
                    "baseline_id": baseline_id, "method": self.method,
                    "frozen": bool(entry.get("frozen", False)),
                    "sources": dict(sorted(sources.items())),
                    "outputs": dict(sorted(new_outputs.items())),
                    "last_job_id": self.job_id, "updated": _now_iso(),
                })
                ledger["ts"][self.ts_name] = new_entry
                write_json_atomic(self._ledger_path(), ledger)
                if ledger_raw is None:
                    undo.append(lambda: self._ledger_path().unlink() if self._ledger_path().exists() else None)
                else:
                    undo.append(lambda raw=ledger_raw: self._write_bytes_atomic(self._ledger_path(), raw))

                write_json_atomic(flag_path, {  # 最後: flag
                    "generation": self.generation, "uv_hash": uv_hash,
                    "files": sorted(new_outputs)}, tmp_suffix=TMP_SUFFIX)
            except Exception as exc:
                summary, tb = _format_exc(exc)
                self.msg("error", "公開中に失敗したためロールバックします: " + summary)
                undo_failed = False
                for fn in reversed(undo):
                    try:
                        fn()
                    except Exception as exc2:
                        undo_failed = True
                        s2, _t2 = _format_exc(exc2)
                        self.msg("error", "ロールバックに失敗: " + s2)
                if undo_failed:
                    try:
                        if flag_path.exists():
                            flag_path.unlink()
                    except OSError:
                        pass
                    self.msg("error", "ロールバックに失敗しました。出力が不整合の可能性があります")
                for tmp in tmp_paths.values():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                try:
                    self._journal_path().unlink()
                except OSError:
                    pass
                if isinstance(exc, _Abort):
                    raise
                if undo_failed:
                    raise PublishError(
                        "公開に失敗しました。ロールバックに失敗しました。出力が不整合の可能性があります"
                    ) from exc
                raise PublishError("公開に失敗しました(ロールバック済み): " + summary) from exc

            try:
                self._journal_path().unlink()
            except OSError:
                pass

        for name in sorted(new_files):
            self.files.append({"name": name, "sha1": new_files[name]["sha1"], "tile": new_files[name]["tile"]})

    @staticmethod
    def _write_bytes_atomic(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".{0}.tmp".format(os.getpid()))
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        uvremap_io.replace_file(tmp, path)

    # ------------------------------------------------------------ cleanup
    def cleanup(self, success: bool) -> None:
        if self.job_dir is None:
            return
        for sub in ("staging", "backup") + (("input",) if success else ()):
            shutil.rmtree(self.job_dir / sub, ignore_errors=True)

    # ----------------------------------------------------------------- run
    def run(self) -> int:
        try:
            try:
                raw = read_json(self.job_path, None)
                if raw is None:
                    raise JobFailed("job.json が見つかりません")
            except JobFailed:
                raise
            except Exception as exc:
                raise JobFailed("job.json を読めません: {0}: {1}".format(type(exc).__name__, exc))
            self.job = parse_job(raw)
            j = self.job
            self.job_id = j["job_id"]
            self.ts_name = j["ts_name"]
            self.generation = j["generation"]
            self.root = Path(j["uvremap_root"])
            self.job_dir = self.root / "jobs" / self.job_id
            self._src_norm = _norm(j["source_dir"])
            _log("job {0} (ts={1}, generation={2}) 開始".format(self.job_id, self.ts_name, self.generation))
            try:
                self.execute()
            except _Abort:
                raise
            except Exception as exc:
                mapped = _translate_core_error(exc)
                if mapped is None:
                    raise
                raise mapped from exc
        except _Abort as exc:
            level = "info" if isinstance(exc, Superseded) else "error"
            self.msg(level, str(exc))
            if isinstance(exc, Superseded):
                self.restore_status()
            self.cleanup(False)
            return self.finish(exc.exit_code, exc.result_status, exc.state, str(exc))
        except Exception as exc:
            summary, tb = _format_exc(exc)
            self.msg("error", "予期しない例外: " + summary)
            if tb:
                self.msg("error", "トレース: " + tb)
            self.cleanup(False)
            return self.finish(EXIT_FAILED, "failed", "FAILED", summary)
        self.cleanup(True)
        warn_count = sum(1 for m in self.messages if m["level"] == "warn")
        self.msg("info", "完了: {0} ファイルを公開しました".format(len(self.files)))
        return self.finish(EXIT_OK, "ok", "CONVERTED_OK",
                           "警告 {0} 件".format(warn_count) if warn_count else "")


def run_job(job_path) -> int:
    """job.json を実行して終了コードを返す(CLI と同じ処理。テストからも使う)。"""
    return _Run(job_path).run()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _install_signal_handlers() -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def _handler(signum, _frame):
        try:
            signal.signal(signum, signal.SIG_DFL)  # 2回目は即終了できるようにする
        except Exception:
            pass
        raise Interrupted("シグナル {0} を受信して中断しました".format(signum))

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="uvremap_worker.py", description="UV再マップ ワーカー")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="job.json を実行する")
    p_run.add_argument("job", help="job.json のパス")
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if args.cmd != "run":
        parser.print_usage(sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    _install_signal_handlers()
    return run_job(args.job)


if __name__ == "__main__":
    sys.exit(main())
