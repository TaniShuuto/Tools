# -*- coding: utf-8 -*-
"""Maya リベースの純関数(依頼・状態・取り消し・表示・完了判定・文言・掃除)。

# 2026.10.19(RB-a): 新規。画面にも Maya にも依存しない。
Painter のメッシュを新しい UV で読み直し、基準 UV を切り替える処理のうち、
ファイルの書式と判定だけをここに置く。Maya の cmds、Qt、maya_live_sync は使わない。
uvremap_runtime / uvremap_maya も import しない。

時計と乱数は引数で受け取る。このモジュールは現在時刻を読まない。
契約が決めていない点はコメントに「契約に無い。仮」と書き、
``docs/uvremap/QUESTIONS_rb_a.md`` にも書いてある。
"""
from __future__ import annotations

import json
import math
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from uvremap_worker import write_json_atomic

__all__ = (
    "SCHEMA",
    "REBASE_DIRNAME",
    "REQUESTS_DIRNAME",
    "STATUS_DIRNAME",
    "MESHES_DIRNAME",
    "STATES",
    "TERMINAL_STATES",
    "CANCELABLE_STATES",
    "LOCKED_STATES",
    "REASON_CODES",
    "COMPLETION_REASONS",
    "DEFAULT_TTL_S",
    "TTL_MIN_S",
    "TTL_MAX_S",
    "DEFAULT_POLL_S",
    "POLL_MIN_S",
    "POLL_MAX_S",
    "CLEANUP_AGE_S",
    "MESSAGE_MAX",
    "MIN_PLUGIN_VERSION",
    "REQUEST_RELOAD",
    "REQUEST_ID_RE",
    "MSG_WAITING",
    "MSG_PREPARING",
    "MSG_AWAITING_APPROVAL",
    "MSG_WORKING_LOCKED",
    "MSG_DONE",
    "MSG_FAILED_FMT",
    "MSG_DECLINED",
    "MSG_REJECTED",
    "MSG_EXPIRED",
    "MSG_SUPERSEDED",
    "MSG_CANCELING",
    "MSG_CANNOT_CANCEL",
    "MSG_STATUS_UNREADABLE",
    "MSG_PLUGIN_UPDATE_REQUIRED",
    "MSG_DONE_GUIDANCE",
    "StatusRead",
    "RebaseDisplay",
    "CompletionDecision",
    "generate_request_id",
    "is_valid_request_id",
    "rebase_dir",
    "requests_dir",
    "status_dir",
    "request_path",
    "cancel_path",
    "status_path",
    "sync_result_path",
    "normalize_rebase_settings",
    "build_request",
    "parse_request",
    "write_request",
    "read_request",
    "list_requests",
    "write_cancel",
    "cancel_exists",
    "parse_status",
    "read_status",
    "parse_sync_result",
    "read_sync_result",
    "parse_version",
    "plugin_supports_rebase",
    "decide_display",
    "is_request_open",
    "find_open_requests",
    "judge_completion",
    "select_cleanup_targets",
    "cleanup_rebase_files",
)

SCHEMA = 1

REBASE_DIRNAME = "rebase"
REQUESTS_DIRNAME = "requests"
STATUS_DIRNAME = "status"
MESHES_DIRNAME = "meshes"
_REQUEST_EXT = ".json"
_CANCEL_EXT = ".cancel"
_SYNC_RESULT_NAME = "_sync_result.json"

# 状態名。終端・取り消し可・ロックは互いに重ならない。
# backing_up は取り消せない(R-3 の表と R-8 の表示表)。
# R-8 本文の「reloading より前」とは扱いが違う。QUESTIONS_rb_a.md。
STATES = (
    "received",
    "awaiting_approval",
    "preparing",
    "backing_up",
    "reloading",
    "exporting",
    "done",
    "failed",
    "declined",
    "rejected",
    "expired",
    "superseded",
)
TERMINAL_STATES = (
    "done",
    "failed",
    "declined",
    "rejected",
    "expired",
    "superseded",
)
CANCELABLE_STATES = (
    "received",
    "awaiting_approval",
    "preparing",
)
LOCKED_STATES = (
    "backing_up",
    "reloading",
    "exporting",
)
_STATE_SET = frozenset(STATES)
_TERMINAL = frozenset(TERMINAL_STATES)
_CANCELABLE = frozenset(CANCELABLE_STATES)
_LOCKED = frozenset(LOCKED_STATES)

# 理由コード。このモジュールが分岐に使うのは完了判定(judge_completion)だけ。
REASON_CODES = (
    "disabled",
    "bad_request",
    "bad_settings",
    "project_mismatch",
    "unsaved_project",
    "mesh_missing",
    "mesh_hash_mismatch",
    "busy",
    "auto_unwrap_unverified",
    "unsaved_changes",
    "cancelled",
    "declined",
    "expired",
    "superseded",
    "backup_failed",
    "reload_error",
    "reload_timeout",
    "project_locked",
    "export_failed",
    "interrupted",
)

COMPLETION_REASONS = (
    "already_completed",
    "not_done",
    "terminal_not_done",
    "result_missing",
    "result_not_ok",
    "result_request_id_mismatch",
    "killswitch_off",
    "scene_changed",
    "not_full_final",
    "ok",
)

DEFAULT_TTL_S = 1800
TTL_MIN_S = 60
TTL_MAX_S = 86400
DEFAULT_POLL_S = 1.0
# 契約に無い。仮: poll の範囲は指示書に無いので 0.5〜30.0 秒に丸める。
POLL_MIN_S = 0.5
POLL_MAX_S = 30.0
CLEANUP_AGE_S = 7 * 86400
MESSAGE_MAX = 500
MIN_PLUGIN_VERSION = (1, 3, 0)

# 依頼の reload。使うときは必ずコピーを返す(この辞書をそのまま入れない)。
REQUEST_RELOAD = {
    "preserve_strokes": True,
    "import_cameras": False,
    "auto_unwrap": False,
}

REQUEST_ID_RE = re.compile(r"^rb-\d{8}-\d{6}-[0-9a-f]{4}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:[-+].*)?$")

# --- 文言(契約の仮の文。決定は decide_display) ---
MSG_WAITING = "Painter の応答を待っています"
MSG_PREPARING = "Painter が準備しています"
MSG_AWAITING_APPROVAL = "Painter で確認待ちです"
MSG_WORKING_LOCKED = "Painter が読み直しています(取り消せません)"
MSG_DONE = "リベースが完了しました"
MSG_FAILED_FMT = "リベースに失敗しました。{message}"
MSG_DECLINED = "Painter で見送られました"
MSG_REJECTED = "Painter が依頼を受け付けませんでした"
MSG_EXPIRED = "期限が切れました"
MSG_SUPERSEDED = "新しい依頼に置き換えられました"
MSG_CANCELING = "取り消し中です"
MSG_CANNOT_CANCEL = "読み直しが始まったため取り消せません"
MSG_STATUS_UNREADABLE = "状態を読めません"
MSG_PLUGIN_UPDATE_REQUIRED = "Painter プラグインの更新が必要です"
MSG_DONE_GUIDANCE = (
    "リベースが完了しました。ストロークは新しい UV に再投影されています。"
    "次を Painter で確認してください: 2D ビューのストローク、UV 投影のフィル、"
    "ベイク済みのメッシュマップ(再ベイクが必要な場合があります)"
)

# 契約に無い。仮: 状態ごとの phase。QUESTIONS_rb_a.md。
# MSG_CANNOT_CANCEL と MSG_PLUGIN_UPDATE_REQUIRED は定数のみ。
# ロック中に取り消しを求めても文は MSG_WORKING_LOCKED のまま。
_STATE_PHASE = {
    "received": "working",
    "awaiting_approval": "waiting",
    "preparing": "working",
    "backing_up": "locked",
    "reloading": "locked",
    "exporting": "locked",
    "done": "done",
    "failed": "failed",
    "declined": "ended",
    "rejected": "ended",
    "expired": "ended",
    "superseded": "ended",
}
_STATE_TEXT = {
    "received": MSG_PREPARING,
    "awaiting_approval": MSG_AWAITING_APPROVAL,
    "preparing": MSG_PREPARING,
    "backing_up": MSG_WORKING_LOCKED,
    "reloading": MSG_WORKING_LOCKED,
    "exporting": MSG_WORKING_LOCKED,
    "done": MSG_DONE,
    "declined": MSG_DECLINED,
    "expired": MSG_EXPIRED,
    "superseded": MSG_SUPERSEDED,
}

_RAND = random.SystemRandom()
_REQUEST_KEYS = (
    "schema",
    "request_id",
    "project_key",
    "created_at",
    "expires_at",
    "mesh",
    "baseline_id",
    "reload",
    "final_full",
    "ts_names",
)
_SYNC_REQUIRED = (
    "schema",
    "plugin_version",
    "export_id",
    "status",
    "ok",
    "force_full",
    "request_id",
)


# ---------------------------------------------------------------------------
# 小さな共通
# ---------------------------------------------------------------------------
def _is_finite_number(value) -> bool:
    """bool ではない有限の int / float か。``True`` は int のサブクラスなので先に除く。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _as_utc(now_utc: datetime) -> datetime:
    if not isinstance(now_utc, datetime):
        raise ValueError("now_utc は datetime です")
    tzinfo = now_utc.tzinfo
    if tzinfo is None or tzinfo.utcoffset(now_utc) is None:
        return now_utc
    return now_utc.astimezone(timezone.utc)


def _default_rand() -> int:
    return _RAND.randrange(0, 65536)


def _rand_bits(rand_fn) -> int:
    value = rand_fn()
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("rand_fn は 0 から 65535 の整数を返す必要があります")
    return value & 0xFFFF


def _filename_only(value):
    """区切りを含むときは最後の要素だけ。ファイル名にならなければ None。"""
    if not isinstance(value, str) or value == "":
        return None
    normalized = value.replace("\\", "/").rstrip("/")
    if normalized == "":
        return None
    base = normalized.rsplit("/", 1)[-1]
    if base in ("", ".", ".."):
        return None
    return base


def _clip_message(value) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) > MESSAGE_MAX:
        return value[:MESSAGE_MAX]
    return value


def _optional_text(value):
    if isinstance(value, str):
        return value
    return None


def _read_json_safe(path) -> object | None:
    """壊れた JSON・空・BOM 付き・UTF-8 でないバイト列は None。例外は出さない。

    ``uvremap_worker.read_json`` は ``FileNotFoundError`` しか握らず、
    UTF-8 BOM も ``utf-8-sig`` で外す。ここでは BOM を外さない
    (指示書のテスト: BOM 付きは読めないものとして None)。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, ValueError, UnicodeError):
        return None


def _write_empty_atomic(path: Path) -> None:
    """空ファイルを一時ファイル経由で置く。既にあっても置き換えて成功する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("{0}.{1}.tmp".format(path.name, os.getpid()))
    try:
        with open(tmp, "wb") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _try_remove(path: Path, remove_fn) -> bool:
    if not path.is_file():
        return False
    try:
        remove_fn(os.fspath(path))
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# 設定の丸め
# ---------------------------------------------------------------------------
def _coerce_ttl_s(value) -> int:
    """60〜86400 の int。型違い(bool を含む)は 1800。整数値の float だけ int にする。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TTL_S
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return DEFAULT_TTL_S
        value = int(value)
    if value < TTL_MIN_S:
        return TTL_MIN_S
    if value > TTL_MAX_S:
        return TTL_MAX_S
    return int(value)


def _coerce_poll_s(value) -> float:
    """型違い(bool・NaN・inf を含む)は 1.0。契約に無い。仮: 0.5〜30.0 に丸める。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_POLL_S
    number = float(value)
    if not math.isfinite(number):
        return DEFAULT_POLL_S
    if number < POLL_MIN_S:
        return POLL_MIN_S
    if number > POLL_MAX_S:
        return POLL_MAX_S
    return number


def _coerce_triangulate(value) -> bool:
    if isinstance(value, bool):
        return value
    return False


def normalize_rebase_settings(cfg) -> dict:
    """Maya の設定を丸める。dict でなくても例外を出さない。

    設定キーは ``uvremap_rebase_ttl_s``(R-6)。``rebase_request_ttl_s`` は見ない。
    """
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "uvremap_rebase_ttl_s": _coerce_ttl_s(cfg.get("uvremap_rebase_ttl_s", DEFAULT_TTL_S)),
        "uvremap_rebase_triangulate": _coerce_triangulate(cfg.get("uvremap_rebase_triangulate", False)),
        "uvremap_rebase_poll_s": _coerce_poll_s(cfg.get("uvremap_rebase_poll_s", DEFAULT_POLL_S)),
    }


# ---------------------------------------------------------------------------
# request_id とパス
# ---------------------------------------------------------------------------
def generate_request_id(now_utc, rand_fn=None) -> str:
    """``rb-<YYYYMMDD>-<HHMMSS>-<4桁の小文字16進>``。時計は読まない。

    ``now_utc`` に tzinfo があれば UTC へ変換する。無ければ UTC の壁時計とみなす。
    ``rand_fn`` は 0〜65535 の整数を返す。省略時は標準ライブラリの乱数。
    """
    if rand_fn is None:
        rand_fn = _default_rand
    when = _as_utc(now_utc)
    return "rb-{0}-{1:04x}".format(when.strftime("%Y%m%d-%H%M%S"), _rand_bits(rand_fn))


def is_valid_request_id(value) -> bool:
    return isinstance(value, str) and REQUEST_ID_RE.fullmatch(value) is not None


def _checked_request_id(request_id) -> str:
    if not is_valid_request_id(request_id):
        raise ValueError("request_id が不正です")
    return request_id


def rebase_dir(root) -> Path:
    return Path(root) / REBASE_DIRNAME


def requests_dir(root) -> Path:
    return rebase_dir(root) / REQUESTS_DIRNAME


def status_dir(root) -> Path:
    return rebase_dir(root) / STATUS_DIRNAME


def request_path(root, request_id) -> Path:
    rid = _checked_request_id(request_id)
    return requests_dir(root) / "{0}{1}".format(rid, _REQUEST_EXT)


def cancel_path(root, request_id) -> Path:
    rid = _checked_request_id(request_id)
    return requests_dir(root) / "{0}{1}".format(rid, _CANCEL_EXT)


def status_path(root, request_id) -> Path:
    rid = _checked_request_id(request_id)
    return status_dir(root) / "{0}{1}".format(rid, _REQUEST_EXT)


def sync_result_path(final_subfolder_dir) -> Path:
    return Path(final_subfolder_dir) / _SYNC_RESULT_NAME


# ---------------------------------------------------------------------------
# 依頼
# ---------------------------------------------------------------------------
def _parse_mesh(mesh):
    """正規化した mesh か None。sha1 は小文字で持つ。"""
    if not isinstance(mesh, dict):
        return None
    path = mesh.get("path")
    size = mesh.get("size")
    sha1 = mesh.get("sha1")
    if not isinstance(path, str) or path == "":
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    if not isinstance(sha1, str) or _SHA1_RE.fullmatch(sha1.lower()) is None:
        return None
    return {"path": path, "size": size, "sha1": sha1.lower()}


def _parse_ts_names(value):
    if not isinstance(value, list) or len(value) == 0:
        return None
    names = []
    for item in value:
        if not isinstance(item, str) or item == "":
            return None
        names.append(item)
    return names


def _reload_problem(value):
    """一致なら None。dict でなければ bad_request。値の違いなら bad_settings。"""
    if not isinstance(value, dict):
        return "bad_request"
    for key, want in REQUEST_RELOAD.items():
        if key not in value or value[key] is not want:
            return "bad_settings"
    return None


def _copy_reload() -> dict:
    return {
        "preserve_strokes": REQUEST_RELOAD["preserve_strokes"],
        "import_cameras": REQUEST_RELOAD["import_cameras"],
        "auto_unwrap": REQUEST_RELOAD["auto_unwrap"],
    }


def parse_request(raw) -> tuple[dict | None, str | None]:
    """正規化した依頼か ``(None, 理由コード)``。例外は出さない。

    不明なキーは落とす。``maya_version`` / ``tool_version`` は文字列のときだけ残す。
    ファイル名との一致は見ない(``read_request`` が確認する)。
    """
    if not isinstance(raw, dict):
        return None, "bad_request"
    for key in _REQUEST_KEYS:
        if key not in raw:
            return None, "bad_request"
    schema = raw.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA:
        return None, "bad_request"
    request_id = raw.get("request_id")
    if not is_valid_request_id(request_id):
        return None, "bad_request"
    project_key = raw.get("project_key")
    # 契約に無い。仮: 空の project_key / baseline_id は bad_request。
    if not isinstance(project_key, str) or project_key == "":
        return None, "bad_request"
    created_at = raw.get("created_at")
    expires_at = raw.get("expires_at")
    if not _is_finite_number(created_at) or not _is_finite_number(expires_at):
        return None, "bad_request"
    mesh = _parse_mesh(raw.get("mesh"))
    if mesh is None:
        return None, "bad_request"
    baseline_id = raw.get("baseline_id")
    if not isinstance(baseline_id, str) or baseline_id == "":
        return None, "bad_request"
    reload_problem = _reload_problem(raw.get("reload"))
    if reload_problem == "bad_request":
        return None, "bad_request"
    final_full = raw.get("final_full")
    if not isinstance(final_full, bool):
        return None, "bad_request"
    ts_names = _parse_ts_names(raw.get("ts_names"))
    if ts_names is None:
        return None, "bad_request"
    if reload_problem is not None:
        return None, reload_problem
    out = {
        "schema": SCHEMA,
        "request_id": request_id,
        "project_key": project_key,
        "created_at": created_at,
        "expires_at": expires_at,
        "mesh": mesh,
        "baseline_id": baseline_id,
        "reload": _copy_reload(),
        "final_full": final_full,
        "ts_names": ts_names,
    }
    for key in ("maya_version", "tool_version"):
        if key in raw and isinstance(raw[key], str):
            out[key] = raw[key]
    return out, None


def build_request(
    *,
    request_id,
    project_key,
    created_at,
    ttl_s,
    mesh,
    baseline_id,
    ts_names,
    final_full=True,
    maya_version=None,
    tool_version=None,
) -> dict:
    """依頼の辞書を作る。``expires_at = created_at + ttl``。ttl は設定と同じ丸め。

    ``maya_version`` / ``tool_version`` が None のときはキー自体を出さない。
    型が違えば ``ValueError``。``reload`` は ``REQUEST_RELOAD`` のコピー。
    """
    if not is_valid_request_id(request_id):
        raise ValueError("request_id が不正です")
    if not isinstance(project_key, str) or project_key == "":
        raise ValueError("project_key が不正です")
    if not _is_finite_number(created_at):
        raise ValueError("created_at が不正です")
    ttl = _coerce_ttl_s(ttl_s)
    parsed_mesh = _parse_mesh(mesh)
    if parsed_mesh is None:
        raise ValueError("mesh が不正です")
    if not isinstance(baseline_id, str) or baseline_id == "":
        raise ValueError("baseline_id が不正です")
    names = _parse_ts_names(ts_names)
    if names is None:
        raise ValueError("ts_names が不正です")
    if not isinstance(final_full, bool):
        raise ValueError("final_full が不正です")
    if maya_version is not None and not isinstance(maya_version, str):
        raise ValueError("maya_version が不正です")
    if tool_version is not None and not isinstance(tool_version, str):
        raise ValueError("tool_version が不正です")
    out = {
        "schema": SCHEMA,
        "request_id": request_id,
        "project_key": project_key,
        "created_at": created_at,
        "expires_at": created_at + ttl,
        "mesh": parsed_mesh,
        "baseline_id": baseline_id,
        "reload": _copy_reload(),
        "final_full": final_full,
        "ts_names": names,
    }
    if maya_version is not None:
        out["maya_version"] = maya_version
    if tool_version is not None:
        out["tool_version"] = tool_version
    return out


def write_request(root, request) -> Path:
    """検査してから ``requests/<request_id>.json`` へ原子的に書く。不正なら ValueError。"""
    parsed, reason = parse_request(request)
    if parsed is None:
        raise ValueError(reason or "bad_request")
    path = request_path(root, parsed["request_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, parsed)
    return path


def read_request(root, request_id) -> dict | None:
    """無い・壊れている・中身が不正・ファイル名と ID が違うときは None。"""
    if not is_valid_request_id(request_id):
        return None
    raw = _read_json_safe(request_path(root, request_id))
    if raw is None:
        return None
    parsed, _reason = parse_request(raw)
    if parsed is None or parsed["request_id"] != request_id:
        return None
    return parsed


def list_requests(root) -> list[dict]:
    """``requests/*.json`` のうち読めたものを created_at、request_id の昇順で返す。

    ``*.tmp`` と ``.json`` で終わらないものは無視する。フォルダが無ければ空。
    """
    directory = requests_dir(root)
    if not directory.is_dir():
        return []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        name = entry.name
        if not name.endswith(_REQUEST_EXT) or name.endswith(".tmp"):
            continue
        if not entry.is_file():
            continue
        rec = read_request(root, name[: -len(_REQUEST_EXT)])
        if rec is not None:
            found.append(rec)
    found.sort(key=lambda rec: (rec["created_at"], rec["request_id"]))
    return found


# ---------------------------------------------------------------------------
# 取り消し
# ---------------------------------------------------------------------------
def write_cancel(root, request_id) -> Path:
    """空の ``.cancel`` を原子的に置く。既にあっても成功する。"""
    path = cancel_path(root, request_id)
    _write_empty_atomic(path)
    return path


def cancel_exists(root, request_id) -> bool:
    if not is_valid_request_id(request_id):
        return False
    try:
        return cancel_path(root, request_id).is_file()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 状態(Maya は書かない。読むだけ)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StatusRead:
    """状態ファイルの読み取り結果。``kind`` は missing / unreadable / ok。"""

    kind: str
    status: dict | None


def parse_status(raw) -> dict | None:
    """必須が欠けている・型が違う・未知の state なら None。例外は出さない。

    未知のキーは落とす(正規化するキーだけを残す)。``backup_name`` はファイル名だけ。
    ``message`` は文字列でなければ空、``MESSAGE_MAX`` で切る。
    """
    if not isinstance(raw, dict):
        return None
    schema = raw.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA:
        return None
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or request_id == "":
        return None
    state = raw.get("state")
    if state not in _STATE_SET:
        return None
    updated_at = raw.get("updated_at")
    if not _is_finite_number(updated_at):
        return None
    return {
        "schema": SCHEMA,
        "request_id": request_id,
        "state": state,
        "updated_at": updated_at,
        "plugin_version": _optional_text(raw.get("plugin_version")),
        "reason": _optional_text(raw.get("reason")),
        "message": _clip_message(raw.get("message")),
        "backup_name": _filename_only(raw.get("backup_name")),
        "result_export_id": _optional_text(raw.get("result_export_id")),
    }


def read_status(root, request_id) -> StatusRead:
    """ファイルが無ければ missing。読めない・解析できない・ID 不一致は unreadable。

    不正な request_id はディスクを見ずに unreadable(フォルダの外を読まない)。
    """
    if not is_valid_request_id(request_id):
        return StatusRead("unreadable", None)
    path = status_path(root, request_id)
    try:
        exists = path.is_file()
    except OSError:
        return StatusRead("unreadable", None)
    if not exists:
        return StatusRead("missing", None)
    raw = _read_json_safe(path)
    if raw is None:
        return StatusRead("unreadable", None)
    parsed = parse_status(raw)
    if parsed is None or parsed["request_id"] != request_id:
        return StatusRead("unreadable", None)
    return StatusRead("ok", parsed)


# ---------------------------------------------------------------------------
# _sync_result.json(Painter が書く。Maya は読むだけ)
# ---------------------------------------------------------------------------
def parse_sync_result(raw) -> dict | None:
    """契約が依存するキーだけ検査する。他のキーは残す。例外は出さない。

    ``ok`` と ``force_full`` は bool だけ。``request_id`` は str か None。
    """
    if not isinstance(raw, dict):
        return None
    for key in _SYNC_REQUIRED:
        if key not in raw:
            return None
    schema = raw.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA:
        return None
    if not isinstance(raw.get("plugin_version"), str):
        return None
    if not isinstance(raw.get("export_id"), str):
        return None
    if not isinstance(raw.get("status"), str):
        return None
    if not isinstance(raw.get("ok"), bool):
        return None
    if not isinstance(raw.get("force_full"), bool):
        return None
    request_id = raw.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        return None
    return dict(raw)


def read_sync_result(final_subfolder_dir) -> dict | None:
    raw = _read_json_safe(sync_result_path(final_subfolder_dir))
    if raw is None:
        return None
    return parse_sync_result(raw)


def parse_version(text) -> tuple[int, ...] | None:
    """``1.3.0`` を ``(1, 3, 0)`` にする。``1.4.0-beta`` は数字の部分だけ。

    数字以外が区切り無しで混ざるもの(``1.2.0a`` や ``v1.3.0``)は None。
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.fullmatch(text.strip())
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def _version_at_least(version: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    # 契約に無い。仮: 足りない桁は 0 とみなす。
    width = max(len(version), len(minimum))
    left = version + (0,) * (width - len(version))
    right = minimum + (0,) * (width - len(minimum))
    return left >= right


def plugin_supports_rebase(sync_result) -> bool:
    """一度も見ていない(None)、版が読めない、1.3.0 未満は False。"""
    if not isinstance(sync_result, dict):
        return False
    version = parse_version(sync_result.get("plugin_version"))
    if version is None:
        return False
    return _version_at_least(version, MIN_PLUGIN_VERSION)


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RebaseDisplay:
    """画面に出す内容。``phase`` は waiting / working / locked / done / failed / ended / unreadable / canceling。"""

    text: str
    phase: str
    can_cancel: bool
    terminal: bool
    show_done_guidance: bool


def _display(text, phase, *, can_cancel=False, terminal=False, show_done_guidance=False) -> RebaseDisplay:
    return RebaseDisplay(
        text=text,
        phase=phase,
        can_cancel=can_cancel,
        terminal=terminal,
        show_done_guidance=show_done_guidance,
    )


def _past_expiry(request, now) -> bool:
    """``now > expires_at`` のときだけ真。同時刻はまだ期限内。"""
    if not isinstance(request, dict) or not _is_finite_number(now):
        return False
    expires_at = request.get("expires_at")
    if not _is_finite_number(expires_at):
        return False
    return float(now) > float(expires_at)


def _failed_text(status: dict) -> str:
    message = _clip_message(status.get("message"))
    text = MSG_FAILED_FMT.format(message=message)
    backup = _filename_only(status.get("backup_name"))
    if backup:
        text = "{0} バックアップ: {1}".format(text, backup)
    return text


def _rejected_text(status: dict) -> str:
    message = _clip_message(status.get("message"))
    if message:
        return "{0} {1}".format(MSG_REJECTED, message)
    return MSG_REJECTED


def _display_for_state(state, status) -> RebaseDisplay:
    if state == "failed":
        text = _failed_text(status)
    elif state == "rejected":
        text = _rejected_text(status)
    else:
        text = _STATE_TEXT.get(state, MSG_STATUS_UNREADABLE)
    phase = _STATE_PHASE.get(state, "unreadable")
    return _display(
        text,
        phase,
        can_cancel=state in _CANCELABLE,
        terminal=state in _TERMINAL,
        show_done_guidance=state == "done",
    )


def _expired_display() -> RebaseDisplay:
    """状態ファイルは書かない。画面だけ期限切れにする。"""
    return _display(MSG_EXPIRED, "ended", terminal=True)


def decide_display(request, status_read, *, now, cancel_requested) -> RebaseDisplay:
    """状態ファイルから画面の文を決める。上が優先。例外は出さない。

    状態が無い間も、期限内なら取り消せる。``backing_up`` 以降は取り消せない。
    契約に無い。仮: 期限切れの表示は、取り消し要求より優先する。
    そのときの ``can_cancel`` は偽。読めない状態の ``can_cancel`` も常に偽。
    """
    kind = getattr(status_read, "kind", "unreadable")
    status = getattr(status_read, "status", None)
    if kind == "ok" and isinstance(status, dict):
        state = status.get("state")
        if state in _CANCELABLE and cancel_requested:
            return _display(MSG_CANCELING, "canceling")
        return _display_for_state(state, status)
    past = _past_expiry(request, now)
    if kind == "missing":
        if past:
            return _expired_display()
        if cancel_requested:
            return _display(MSG_CANCELING, "canceling")
        return _display(MSG_WAITING, "waiting", can_cancel=True)
    if past:
        return _expired_display()
    return _display(MSG_STATUS_UNREADABLE, "unreadable")


def is_request_open(request, status_read, now) -> bool:
    """終端でなければ開いている。状態が無い・読めない間は期限内だけ開いている。"""
    kind = getattr(status_read, "kind", "unreadable")
    status = getattr(status_read, "status", None)
    if kind == "ok" and isinstance(status, dict):
        return status.get("state") not in _TERMINAL
    if not isinstance(request, dict) or not _is_finite_number(now):
        return False
    expires_at = request.get("expires_at")
    if not _is_finite_number(expires_at):
        return False
    return float(now) <= float(expires_at)


def find_open_requests(root, project_key, now) -> list[dict]:
    """同じ project_key で開いている依頼。created_at の昇順。"""
    found = []
    for request in list_requests(root):
        if request.get("project_key") != project_key:
            continue
        if is_request_open(request, read_status(root, request["request_id"]), now):
            found.append(request)
    return found


# ---------------------------------------------------------------------------
# 完了判定
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CompletionDecision:
    """``action`` は complete / wait / skip。``reason`` は COMPLETION_REASONS のいずれか。

    ``done`` なのに結果が合わないとき(result_missing など)の画面の扱いは決めていない。
    この関数は判定だけ返す。
    """

    action: str
    reason: str


def _scene_token(value):
    """None は空文字として比べる。それ以外はそのまま。"""
    if value is None:
        return ""
    return value


def judge_completion(
    *,
    status_read,
    sync_result,
    request,
    enabled,
    scene_id_now,
    scene_id_request,
    already_completed,
) -> CompletionDecision:
    """成功時の完了処理を始めてよいか。上の条件が優先。例外は出さない。"""
    if already_completed:
        return CompletionDecision("skip", "already_completed")
    kind = getattr(status_read, "kind", None)
    status = getattr(status_read, "status", None)
    if kind != "ok" or not isinstance(status, dict):
        return CompletionDecision("wait", "not_done")
    state = status.get("state")
    if state != "done":
        if state in _TERMINAL:
            return CompletionDecision("skip", "terminal_not_done")
        return CompletionDecision("wait", "not_done")
    if not isinstance(sync_result, dict):
        return CompletionDecision("skip", "result_missing")
    if sync_result.get("ok") is not True:
        return CompletionDecision("skip", "result_not_ok")
    request_id = request.get("request_id") if isinstance(request, dict) else None
    if sync_result.get("request_id") != request_id:
        return CompletionDecision("skip", "result_request_id_mismatch")
    if not enabled:
        return CompletionDecision("skip", "killswitch_off")
    if _scene_token(scene_id_now) != _scene_token(scene_id_request):
        return CompletionDecision("skip", "scene_changed")
    final_full = request.get("final_full") if isinstance(request, dict) else None
    if final_full and sync_result.get("force_full") is not True:
        return CompletionDecision("skip", "not_full_final")
    return CompletionDecision("complete", "ok")


# ---------------------------------------------------------------------------
# 掃除
# ---------------------------------------------------------------------------
def select_cleanup_targets(rows, now, max_age_s=CLEANUP_AGE_S) -> list[str]:
    """終端から ``max_age_s`` 以上たった request_id を昇順で返す。

    ``terminal_at`` が None の行は非終端として残す。例外は出さない。
    """
    if not _is_finite_number(now) or not _is_finite_number(max_age_s):
        return []
    if not isinstance(rows, list):
        return []
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        request_id = row.get("request_id")
        if not is_valid_request_id(request_id):
            continue
        terminal_at = row.get("terminal_at")
        if terminal_at is None or not _is_finite_number(terminal_at):
            continue
        if float(now) - float(terminal_at) >= float(max_age_s):
            found.append(request_id)
    return sorted(set(found))


def _json_request_ids(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        name = entry.name
        if not name.endswith(_REQUEST_EXT) or name.endswith(".tmp"):
            continue
        if not entry.is_file():
            continue
        request_id = name[: -len(_REQUEST_EXT)]
        if is_valid_request_id(request_id):
            found.append(request_id)
    return found


def _cleanup_rows(root, now) -> list[dict]:
    """終端とみなせる ID と、その終端時刻。読めないファイルは対象にしない。

    状態が ok なら終端状態の updated_at。状態ファイルが無い依頼は、
    表示上の期限切れ(now > expires_at)に限って expires_at を終端時刻にする。
    """
    rows = []
    seen = set()
    for request_id in _json_request_ids(requests_dir(root)):
        request = read_request(root, request_id)
        if request is None:
            continue
        status_read = read_status(root, request_id)
        seen.add(request_id)
        if status_read.kind == "unreadable":
            continue
        if status_read.kind == "ok":
            status = status_read.status or {}
            if status.get("state") in _TERMINAL:
                rows.append({
                    "request_id": request_id,
                    "terminal_at": status.get("updated_at"),
                })
            continue
        expires_at = request.get("expires_at")
        if _is_finite_number(expires_at) and _is_finite_number(now) and float(now) > float(expires_at):
            rows.append({"request_id": request_id, "terminal_at": expires_at})
    for request_id in _json_request_ids(status_dir(root)):
        if request_id in seen:
            continue
        status_read = read_status(root, request_id)
        if status_read.kind != "ok":
            continue
        status = status_read.status or {}
        if status.get("state") in _TERMINAL:
            rows.append({
                "request_id": request_id,
                "terminal_at": status.get("updated_at"),
            })
    return rows


def _remove_terminal_files(root, request_id, remove_fn) -> bool:
    """読めた依頼 JSON、.cancel、読めた状態 JSON を消す。読めないファイルは残す。"""
    deleted = False
    if read_request(root, request_id) is not None:
        deleted = _try_remove(request_path(root, request_id), remove_fn) or deleted
    deleted = _try_remove(cancel_path(root, request_id), remove_fn) or deleted
    status_read = read_status(root, request_id)
    if status_read.kind == "ok":
        deleted = _try_remove(status_path(root, request_id), remove_fn) or deleted
    return deleted


def cleanup_rebase_files(root, now, *, max_age_s=CLEANUP_AGE_S, remove_fn=os.remove) -> list[str]:
    """終端から ``max_age_s`` 以上の依頼・取り消し・状態を消す。消した ID を返す。

    非終端と、読めないファイルは消さない。削除の失敗は握って続ける。
    ``meshes/`` と ``baseline/`` には触らない。
    """
    targets = select_cleanup_targets(_cleanup_rows(root, now), now, max_age_s=max_age_s)
    removed = []
    for request_id in targets:
        if _remove_terminal_files(root, request_id, remove_fn):
            removed.append(request_id)
    return removed
