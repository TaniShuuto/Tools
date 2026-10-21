"""リベースの定数と文言、依頼・状態ファイルの解析と書き込み(Painter / Qt に依存しない)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os
import re
import json
import math
import unicodedata
from collections import namedtuple

from ._base import (
    REBASE_DIRNAME,
    _checked_rebase_request_id,
    _log,
    is_valid_rebase_request_id,
    write_json_atomic,
)

__all__ = [
    "REBASE_SCHEMA",
    "REBASE_REQUESTS_DIRNAME",
    "REBASE_STATUS_DIRNAME",
    "REBASE_STATES",
    "REBASE_TERMINAL_STATES",
    "REBASE_CANCELABLE_STATES",
    "REBASE_LOCKED_STATES",
    "REBASE_REASONS",
    "REBASE_APPROVAL_ONLY_REASONS",
    "REBASE_MESSAGE_MAX_CHARS",
    "DEFAULT_UVREMAP_ROOT_DIR",
    "REBASE_BACKUP_DIRNAME",
    "REBASE_RELOAD_EXPECTED",
    "REBASE_BASELINE_ID_RE",
    "BACKUP_INCLUDES_UNSAVED",
    "REBASE_UV_OLD_NAME_RE",
    "REBASE_UV_OLD_NAME_MAX",
    "REBASE_UV_OLD_INDEX_MIN",
    "REBASE_UV_OLD_INDEX_MAX",
    "_REBASE_SHA1_RE",
    "_REBASE_STATE_SET",
    "_REBASE_TERMINAL",
    "_REBASE_REASON_SET",
    "_REBASE_STATUS_REASON_SET",
    "_REBASE_REQUEST_KEYS",
    "_REBASE_WIN_BAD_FILENAME",
    "_REBASE_WAIT_MAX_MIN_S",
    "_REBASE_WAIT_MAX_MAX_S",
    "_REBASE_TRANSITIONS",
    "_PAINTER_STATE_DEFAULTS",
    "_PAINTER_STATE_KEYS",
    "_REBASE_UV_FILLS_KEYS",
    "ApprovalDecision",
    "RebaseScan",
    "RebaseScanDiff",
    "REBASE_INTERRUPTED_MESSAGE",
    "REBASE_REASON_MESSAGES",
    "_REBASE_BACKUP_MESSAGE_REASONS",
    "REBASE_DIALOG_TITLE",
    "REBASE_CONFIRM_LEAD",
    "REBASE_CAUTION_TEXT",
    "REBASE_CAUTION_TEXT_REPROJECT",
    "REBASE_CAUTION_TEXT_REPROJECT_PRIOR",
    "REBASE_PRIOR_FILL_WARNING_FMT",
    "REBASE_PRIOR_AWAITING_MESSAGE_FMT",
    "REBASE_UNSAVED_NOTE",
    "REBASE_AUTO_UNWRAP_NOTE",
    "REBASE_WORKFLOW_GUIDANCE",
    "REBASE_DONE_GUIDANCE",
    "REBASE_DIRECTION_APPROVAL_NOTE",
    "REBASE_DIRECTION_WARNING_FMT",
    "REBASE_WAIT_MESSAGES",
    "REBASE_BUTTON_RUN",
    "REBASE_BUTTON_CANCEL",
    "_rebase_is_finite_number",
    "_rebase_filename_only",
    "_rebase_clip_message",
    "_rebase_optional_text",
    "_rebase_copy_reload",
    "normalize_project_key_for_storage",
    "normalize_project_key_for_compare",
    "project_keys_match",
    "uvremap_root_for_project",
    "resolve_uvremap_root",
    "rebase_requests_dir",
    "rebase_status_dir",
    "rebase_request_path",
    "rebase_cancel_path",
    "rebase_status_path",
    "_rebase_coerce_float",
    "_rebase_coerce_retry_max",
    "_rebase_coerce_bool",
    "normalize_rebase_settings",
    "effective_rebase_approval",
    "read_json_safe",
    "_parse_rebase_mesh",
    "_parse_rebase_ts_names",
    "_rebase_reload_problem",
    "_parse_rebase_uv_old",
    "parse_rebase_request",
    "read_rebase_request_with_reason",
    "read_rebase_request",
    "scan_rebase_dir",
    "diff_rebase_scan",
    "find_superseded_ids",
    "_normalize_uv_fills",
    "build_rebase_status",
    "parse_rebase_status",
    "read_rebase_status",
    "is_valid_rebase_transition",
    "write_rebase_status",
    "build_interrupted_status",
    "rebase_failure_message",
    "_painter_state_get",
    "REBASE_EXPORT_DEFERRED_STATUS",
    "REBASE_UV_FILL_PROGRESS",
    "REBASE_AUTO_UNWRAP_UNSUPPORTED_LOG",
    "REBASE_TERMINAL_WRITE_MAX",
    "rebase_reject_state",
    "get_uv_old_index",
    "get_uv_old_name",
]


# ---------------------------------------------------------------------------
# 2026.10.19(1.4.0 PB-a): リベース(純関数)
# Painter / Qt に触れない。新規 .py は作らない(plugins では各 .py が別プラグイン)。
# Maya の uvremap_rebase は import しない(Painter 側に無い)。書式はバイト単位で合わせる。
# ---------------------------------------------------------------------------

REBASE_SCHEMA = 1

REBASE_REQUESTS_DIRNAME = "requests"
REBASE_STATUS_DIRNAME = "status"

REBASE_STATES = (
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
REBASE_TERMINAL_STATES = (
    "done",
    "failed",
    "declined",
    "rejected",
    "expired",
    "superseded",
)
REBASE_CANCELABLE_STATES = (
    "received",
    "awaiting_approval",
    "preparing",
)
REBASE_LOCKED_STATES = (
    "backing_up",
    "reloading",
    "exporting",
)
REBASE_REASONS = (
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
REBASE_APPROVAL_ONLY_REASONS = (
    "no_project",
    "recent_edit",
    "needs_approval",
    "auto_approved",
)
REBASE_MESSAGE_MAX_CHARS = 500
DEFAULT_UVREMAP_ROOT_DIR = "C:/SPMayaLiveSync/uvremap"
REBASE_BACKUP_DIRNAME = "rebase_backup"
REBASE_RELOAD_EXPECTED = {
    "preserve_strokes": True,
    "import_cameras": False,
    "auto_unwrap": False,
}

REBASE_BASELINE_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,79}$")
# 「save_as_copy が未保存の変更を含む」ことの確認結果(RB-0。強い傍証あり・目視確認待ち)。
# 確認できるまで False。True に変えるのは出荷前に指揮役が決める。PB-a / PB-b では変えない。
BACKUP_INCLUDES_UNSAVED = False
# 2026.10.19(1.4.0 PB-a): 依頼の任意キー uv_old(Maya の RB-a2 と同じ規則)
REBASE_UV_OLD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REBASE_UV_OLD_NAME_MAX = 64
REBASE_UV_OLD_INDEX_MIN = 1
REBASE_UV_OLD_INDEX_MAX = 7
_REBASE_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_REBASE_STATE_SET = frozenset(REBASE_STATES)
_REBASE_TERMINAL = frozenset(REBASE_TERMINAL_STATES)
_REBASE_REASON_SET = frozenset(REBASE_REASONS)
_REBASE_STATUS_REASON_SET = _REBASE_REASON_SET | frozenset(REBASE_APPROVAL_ONLY_REASONS)
_REBASE_REQUEST_KEYS = (
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
_REBASE_WIN_BAD_FILENAME = '<>:"/\\|?*'
# 契約に無い。仮: rebase_wait_max_s の範囲(QUESTIONS_pb_a.md)。
_REBASE_WAIT_MAX_MIN_S = 60.0
_REBASE_WAIT_MAX_MAX_S = 86400.0
_REBASE_TRANSITIONS = {
    None: frozenset(("received", "rejected")),
    "received": frozenset((
        "received", "rejected", "awaiting_approval", "preparing",
        "declined", "expired", "superseded", "failed",
    )),
    "awaiting_approval": frozenset((
        "awaiting_approval", "preparing", "declined", "expired",
        "superseded", "failed",
    )),
    "preparing": frozenset((
        "preparing", "backing_up", "declined", "superseded", "failed",
    )),
    "backing_up": frozenset(("backing_up", "reloading", "failed")),
    "reloading": frozenset(("reloading", "exporting", "failed")),
    "exporting": frozenset(("exporting", "done", "failed")),
}
_PAINTER_STATE_DEFAULTS = {
    "project_open": False,
    "mesh_exists": False,
    "busy": True,
    "exporting": True,
    "can_disable_auto_unwrap": False,
    "backup_includes_unsaved": False,
    "needs_saving": False,
    "superseded": False,
    "cancelled": False,
    "mesh_sha1_ok": None,
    "last_edit_age_s": None,
    "saved_path": None,
    "project_key": None,
    "prior_uv_fills": None,
}
_PAINTER_STATE_KEYS = (
    "now",
    "project_open",
    "project_key",
    "saved_path",
    "busy",
    "exporting",
    "needs_saving",
    "backup_includes_unsaved",
    "mesh_exists",
    "mesh_sha1_ok",
    "superseded",
    "cancelled",
    "can_disable_auto_unwrap",
    "last_edit_age_s",
)
_REBASE_UV_FILLS_KEYS = (
    "enabled", "switched", "skipped", "failed", "rolled_back", "direction",
    "prior",
)

ApprovalDecision = namedtuple("ApprovalDecision", ["action", "reason"])
RebaseScan = namedtuple("RebaseScan", ["requests", "cancels"])
RebaseScanDiff = namedtuple("RebaseScanDiff", ["changed_ids", "new_cancel_ids"])

REBASE_INTERRUPTED_MESSAGE = (
    "Painter が終了または中断されたため、依頼は完了しませんでした"
)
REBASE_REASON_MESSAGES = {
    "disabled": "リベースは無効です",
    "bad_request": "依頼の内容が不正です",
    "bad_settings": "読み直しの設定が不正です",
    "project_mismatch": "開いているプロジェクトが依頼と一致しません",
    "unsaved_project": "未保存のプロジェクトではリベースできません",
    "mesh_missing": "依頼のメッシュファイルが見つかりません",
    "mesh_hash_mismatch": "メッシュファイルの内容が依頼と一致しません",
    "busy": "Painter が処理中です",
    "auto_unwrap_unverified": "自動ラップ解除を止められるか確認できません",
    "unsaved_changes": "未保存の変更があります",
    "cancelled": "依頼は取り消されました",
    "declined": "リベースを見送りました",
    "expired": "依頼の期限が切れました",
    "superseded": "新しい依頼に置き換えられました",
    "backup_failed": "バックアップに失敗しました",
    "reload_error": "メッシュの読み直しに失敗しました",
    "reload_timeout": "メッシュの読み直しが時間内に終わりませんでした",
    "project_locked": "プロジェクトがロックされています",
    "export_failed": (
        "Final の書き出しに失敗しました。"
        "メッシュは読み直し済み。Final の書き出しをやり直してください"
    ),
    "interrupted": REBASE_INTERRUPTED_MESSAGE,
}
_REBASE_BACKUP_MESSAGE_REASONS = frozenset((
    "reload_error",
    "reload_timeout",
    "project_locked",
    "backup_failed",
    "export_failed",
    "interrupted",
))

# 2026.10.19(1.4.0 PB-a): 確認ダイアログと案内の文言(R-8)。常時表示の注意文は省略できない。
REBASE_DIALOG_TITLE = "Live Sync — UV リベース"
REBASE_CONFIRM_LEAD = (
    "Maya から UV のリベースの依頼が届きました。"
    "Painter のメッシュを新しい UV で読み直します。"
    "バックアップを作ってから実行します。"
)
REBASE_CAUTION_TEXT = (
    "3D ビューで描いた内容は新しい UV に合わせて描き直されます。"
    "2D ビューで描いた内容、UV 投影のフィルやマスク、読み込んだ画像は崩れます。"
    "元に戻すにはバックアップ({backup_dir})を開いてください"
)
REBASE_CAUTION_TEXT_REPROJECT = (
    "3D ビューで描いた内容は新しい UV に合わせて描き直されます。"
    "UV 投影のフィル(マスクのフィルを含む)は、旧 UV セット経由に切り替えて"
    "見た目を保ちます(切り替えた数は完了後に表示します)。"
    "2D ビューで描いた内容は追従せず、崩れます。"
    "元に戻すにはバックアップ({backup_dir})を開いてください"
)
REBASE_CAUTION_TEXT_REPROJECT_PRIOR = (
    "3D ビューで描いた内容は新しい UV に合わせて描き直されます。"
    "UV 投影のまま残っているフィル(マスクのフィルを含む)は、旧 UV セット経由に切り替えて"
    "見た目を保ちます(切り替えた数は完了後に表示します)。"
    "2D ビューで描いた内容は追従せず、崩れます。"
    "元に戻すにはバックアップ({backup_dir})を開いてください"
)
REBASE_PRIOR_FILL_WARNING_FMT = (
    "前回のリベースで切り替えた UV 投影のフィルが {n} 件あります({names})。"
    "今回のリベースでは、これらの見た目が崩れます。"
    "崩れてもよい場合だけ「実行する」を押してください。"
)
REBASE_PRIOR_AWAITING_MESSAGE_FMT = (
    "前回切り替えた UV 投影のフィルが {n} 件あり、このリベースで見た目が崩れます。"
    "Painter の確認画面で判断してください"
)
REBASE_UNSAVED_NOTE = "未保存の変更もバックアップに含まれます。"
REBASE_AUTO_UNWRAP_NOTE = (
    "プロジェクト設定の自動ラップ解除で、シーム・UVアイランド・パッキングが"
    "『見つからないデータのみを生成』になっていることを確認してください。"
    "『すべて再計算』の場合はUVが作り直されます(バックアップから戻せます)。"
)
REBASE_WORKFLOW_GUIDANCE = (
    "2D ビューで描いた内容を残したいときは、実行前にフィルやビットマップの"
    "レイヤーに変換しておくか、今後は 3D ビューで描くようにしてください。"
)
REBASE_DONE_GUIDANCE = (
    "リベースが完了しました。ストロークは新しい UV に再投影されています。"
    "次を Painter で確認してください: 2D ビューのストローク、UV 投影のフィル、"
    "ベイク済みのメッシュマップ(再ベイクが必要な場合があります)。"
)
REBASE_DIRECTION_APPROVAL_NOTE = (
    "法線を含む UV 投影のフィルは、UV を回転・反転すると陰影が狂うことがあります。"
)
REBASE_DIRECTION_WARNING_FMT = (
    "法線(または異方性の角度)を含む UV 投影のフィルが {n} 件あります"
    "({names})。UV を回転・反転した島では陰影が狂うことがあります。"
    "3D ビューで確認し、必要なら法線を描き直すか再ベイクしてください。"
)
REBASE_WAIT_MESSAGES = {
    "unsaved_changes": (
        "Painter に未保存の変更があります。"
        "Painter でプロジェクトを保存すると確認画面が出ます"
    ),
    "busy": "Painter が処理中です。終わるまで待っています",
    "no_project": "Painter で対象のプロジェクトを開いてください",
    "project_mismatch": (
        "Painter で開いているプロジェクトが依頼と違います。"
        "対象のプロジェクトを開いてください"
    ),
    "recent_edit": "Painter での編集が落ち着くのを待っています",
}
REBASE_BUTTON_RUN = "実行する"
REBASE_BUTTON_CANCEL = "やめる"


# --- 3.1 定数とパス ---
def _rebase_is_finite_number(value):
    """bool ではない有限の int / float か。True は int のサブクラスなので先に除く。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _rebase_filename_only(value):
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


def _rebase_clip_message(value):
    if not isinstance(value, str):
        return ""
    if len(value) > REBASE_MESSAGE_MAX_CHARS:
        return value[:REBASE_MESSAGE_MAX_CHARS]
    return value


def _rebase_optional_text(value):
    if isinstance(value, str):
        return value
    return None


def _rebase_copy_reload():
    return {
        "preserve_strokes": REBASE_RELOAD_EXPECTED["preserve_strokes"],
        "import_cameras": REBASE_RELOAD_EXPECTED["import_cameras"],
        "auto_unwrap": REBASE_RELOAD_EXPECTED["auto_unwrap"],
    }


def normalize_project_key_for_storage(key):
    """空なら入力をそのまま返す。区切りを / にし NFC。大文字小文字は保つ。"""
    if not key:
        return key
    if not isinstance(key, str):
        return key
    return unicodedata.normalize("NFC", key.replace("\\", "/"))


def normalize_project_key_for_compare(key):
    """保存形を casefold()。空はそのまま。"""
    stored = normalize_project_key_for_storage(key)
    if not stored:
        return stored
    if not isinstance(stored, str):
        return stored
    return stored.casefold()


def project_keys_match(a, b):
    """両方が空でない文字列で、compare 形が等しいときだけ True。None どうしは False。"""
    if not isinstance(a, str) or not isinstance(b, str) or a == "" or b == "":
        return False
    return normalize_project_key_for_compare(a) == normalize_project_key_for_compare(b)


def uvremap_root_for_project(root_dir, final_subfolder):
    """作業フォルダ <root_dir>/<final_subfolder>。欠けていれば None。"""
    if not root_dir or not final_subfolder:
        return None
    return os.path.normpath(os.path.join(str(root_dir), str(final_subfolder)))


def resolve_uvremap_root(cfg, final_subfolder):
    """cfg の uvremap_root_dir、無ければ DEFAULT。dict でなくても例外を出さない。

    DEFAULT_CONFIG に uvremap_root_dir は足さない(Maya が持つキー。Painter は読むだけ)。
    """
    root_dir = DEFAULT_UVREMAP_ROOT_DIR
    if isinstance(cfg, dict):
        value = cfg.get("uvremap_root_dir")
        if isinstance(value, str) and value:
            root_dir = value
    return uvremap_root_for_project(root_dir, final_subfolder)


def rebase_requests_dir(root):
    return os.path.join(str(root), REBASE_DIRNAME, REBASE_REQUESTS_DIRNAME)


def rebase_status_dir(root):
    return os.path.join(str(root), REBASE_DIRNAME, REBASE_STATUS_DIRNAME)


def rebase_request_path(root, rid):
    rid = _checked_rebase_request_id(rid)
    return os.path.join(rebase_requests_dir(root), rid + ".json")


def rebase_cancel_path(root, rid):
    rid = _checked_rebase_request_id(rid)
    return os.path.join(rebase_requests_dir(root), rid + ".cancel")


def rebase_status_path(root, rid):
    rid = _checked_rebase_request_id(rid)
    return os.path.join(rebase_status_dir(root), rid + ".json")


# --- 3.2 設定の丸め(R-6) ---
def _rebase_coerce_float(value, default, min_value, max_value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default)
    number = float(value)
    if not math.isfinite(number):
        return float(default)
    if number < min_value:
        return float(min_value)
    if number > max_value:
        return float(max_value)
    return number


def _rebase_coerce_retry_max(value):
    default = 5
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return default
        value = int(value)
    if value < 0:
        return 0
    if value > 10:
        return 10
    return int(value)


def _rebase_coerce_bool(value, default):
    if isinstance(value, bool):
        return value
    return default


def normalize_rebase_settings(cfg):
    """Painter のリベース設定を丸める。dict でなくても例外を出さない。"""
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "rebase_enabled": _rebase_coerce_bool(cfg.get("rebase_enabled", True), True),
        "rebase_approval": (
            "auto" if cfg.get("rebase_approval") == "auto" else "ask"
        ),
        "rebase_auto_idle_s": _rebase_coerce_float(
            cfg.get("rebase_auto_idle_s", 10.0), 10.0, 1.0, 600.0),
        "rebase_reload_timeout_s": _rebase_coerce_float(
            cfg.get("rebase_reload_timeout_s", 600.0), 600.0, 30.0, 3600.0),
        "rebase_retry_max": _rebase_coerce_retry_max(cfg.get("rebase_retry_max", 5)),
        # 契約に無い。仮: 60〜86400 に丸める(QUESTIONS_pb_a.md)。
        "rebase_wait_max_s": _rebase_coerce_float(
            cfg.get("rebase_wait_max_s", 1800.0),
            1800.0,
            _REBASE_WAIT_MAX_MIN_S,
            _REBASE_WAIT_MAX_MAX_S,
        ),
        "rebase_reproject_uv_fills": _rebase_coerce_bool(
            cfg.get("rebase_reproject_uv_fills", True), True),
    }


def effective_rebase_approval(settings):
    if isinstance(settings, dict) and settings.get("rebase_approval") == "auto":
        return "auto"
    return "ask"


# --- 3.3 依頼の読み取り(R-2.4) ---
def read_json_safe(path):
    """壊れた JSON・空・BOM 付き・UTF-8 でないバイト列は None。例外は出さない。

    BOM は外さない(BOM 付きは読めないものとして None)。Maya の _read_json_safe と同じ。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        return json.loads(text)
    except (OSError, ValueError, UnicodeError, TypeError):
        return None


def _parse_rebase_mesh(mesh):
    if not isinstance(mesh, dict):
        return None
    path = mesh.get("path")
    size = mesh.get("size")
    sha1 = mesh.get("sha1")
    if not isinstance(path, str) or path == "":
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    if not isinstance(sha1, str) or _REBASE_SHA1_RE.fullmatch(sha1.lower()) is None:
        return None
    return {"path": path, "size": size, "sha1": sha1.lower()}


def _parse_rebase_ts_names(value):
    if not isinstance(value, list) or len(value) == 0:
        return None
    names = []
    for item in value:
        if not isinstance(item, str) or item == "":
            return None
        names.append(item)
    return names


def _rebase_reload_problem(value):
    """一致なら None。dict でなければ bad_request。値の違いなら bad_settings。"""
    if not isinstance(value, dict):
        return "bad_request"
    for key, want in REBASE_RELOAD_EXPECTED.items():
        if key not in value or value[key] is not want:
            return "bad_settings"
    return None


def _parse_rebase_uv_old(value):
    """旧 UV セット記述を検査する。例外は出さない。

    None は有効で値は None。dict で name / index が規則どおりならその 2 キーだけ残す。
    """
    if value is None:
        return True, None
    if not isinstance(value, dict):
        return False, None
    if "name" not in value or "index" not in value:
        return False, None
    name = value.get("name")
    index = value.get("index")
    if not isinstance(name, str) or name == "":
        return False, None
    if len(name) > REBASE_UV_OLD_NAME_MAX:
        return False, None
    if REBASE_UV_OLD_NAME_RE.fullmatch(name) is None:
        return False, None
    if isinstance(index, bool) or not isinstance(index, int):
        return False, None
    if index < REBASE_UV_OLD_INDEX_MIN or index > REBASE_UV_OLD_INDEX_MAX:
        return False, None
    return True, {"name": name, "index": index}


def parse_rebase_request(raw):
    """正規化した依頼か (None, 理由コード)。Maya の parse_request と同じ戻り。

    不明なキーは落とす。maya_version / tool_version は文字列のときだけ残す。
    uv_old の不正は reload の値違い(bad_settings)より先。
    """
    if not isinstance(raw, dict):
        return None, "bad_request"
    for key in _REBASE_REQUEST_KEYS:
        if key not in raw:
            return None, "bad_request"
    schema = raw.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != REBASE_SCHEMA:
        return None, "bad_request"
    request_id = raw.get("request_id")
    if not is_valid_rebase_request_id(request_id):
        return None, "bad_request"
    project_key = raw.get("project_key")
    if not isinstance(project_key, str) or project_key == "":
        return None, "bad_request"
    created_at = raw.get("created_at")
    expires_at = raw.get("expires_at")
    if not _rebase_is_finite_number(created_at) or not _rebase_is_finite_number(expires_at):
        return None, "bad_request"
    if not expires_at > created_at:
        return None, "bad_request"
    mesh = _parse_rebase_mesh(raw.get("mesh"))
    if mesh is None:
        return None, "bad_request"
    baseline_id = raw.get("baseline_id")
    if not isinstance(baseline_id, str) or REBASE_BASELINE_ID_RE.fullmatch(baseline_id) is None:
        return None, "bad_request"
    reload_problem = _rebase_reload_problem(raw.get("reload"))
    if reload_problem == "bad_request":
        return None, "bad_request"
    final_full = raw.get("final_full")
    if not isinstance(final_full, bool):
        return None, "bad_request"
    ts_names = _parse_rebase_ts_names(raw.get("ts_names"))
    if ts_names is None:
        return None, "bad_request"
    # 2026.10.19(1.4.0 PB-a): uv_old は任意。不正は bad_request。
    # reload の値違い(bad_settings)より先に見る。無いのは不正ではない。
    has_uv_old = "uv_old" in raw
    uv_old = None
    if has_uv_old:
        uv_ok, uv_old = _parse_rebase_uv_old(raw.get("uv_old"))
        if not uv_ok:
            return None, "bad_request"
    if reload_problem is not None:
        return None, reload_problem
    out = {
        "schema": REBASE_SCHEMA,
        "request_id": request_id,
        "project_key": project_key,
        "created_at": created_at,
        "expires_at": expires_at,
        "mesh": mesh,
        "baseline_id": baseline_id,
        "reload": _rebase_copy_reload(),
        "final_full": final_full,
        "ts_names": ts_names,
    }
    if has_uv_old:
        out["uv_old"] = uv_old
    for key in ("maya_version", "tool_version"):
        if key in raw and isinstance(raw[key], str):
            out[key] = raw[key]
    return out, None


def read_rebase_request_with_reason(root, rid):
    """読めない・ID 不一致は (None, bad_request)。parse の理由も返す。"""
    if not is_valid_rebase_request_id(rid):
        return None, "bad_request"
    try:
        path = rebase_request_path(root, rid)
    except ValueError:
        return None, "bad_request"
    raw = read_json_safe(path)
    if raw is None:
        return None, "bad_request"
    parsed, reason = parse_rebase_request(raw)
    if parsed is None:
        return None, reason or "bad_request"
    if parsed.get("request_id") != rid:
        return None, "bad_request"
    return parsed, None


def read_rebase_request(root, rid):
    parsed, _reason = read_rebase_request_with_reason(root, rid)
    return parsed


def scan_rebase_dir(root, scandir_fn=os.scandir):
    """requests/ を 1 回見て、正しい形の .json と .cancel だけ拾う。内容は読まない。"""
    requests = {}
    cancels = {}
    directory = rebase_requests_dir(root)
    try:
        entries = scandir_fn(directory)
    except OSError:
        return RebaseScan(requests, cancels)
    closer = getattr(entries, "close", None)
    try:
        for entry in entries:
            try:
                name = getattr(entry, "name", None)
                if not isinstance(name, str):
                    continue
                is_file = getattr(entry, "is_file", None)
                if callable(is_file):
                    if not entry.is_file():
                        continue
                if name.endswith(".tmp"):
                    continue
                st = entry.stat()
                if name.endswith(".json"):
                    rid = name[:-5]
                    if not is_valid_rebase_request_id(rid):
                        continue
                    requests[rid] = (int(st.st_mtime_ns), int(st.st_size))
                elif name.endswith(".cancel"):
                    rid = name[:-7]
                    if not is_valid_rebase_request_id(rid):
                        continue
                    cancels[rid] = int(st.st_mtime_ns)
            except OSError:
                continue
    except OSError:
        pass
    finally:
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
    return RebaseScan(requests, cancels)


def diff_rebase_scan(prev, cur):
    """新規または (mtime_ns, size) が変わった依頼と、新規の .cancel。ID は昇順。"""
    if prev is None:
        prev_requests = {}
        prev_cancels = {}
    else:
        prev_requests = getattr(prev, "requests", None) or {}
        prev_cancels = getattr(prev, "cancels", None) or {}
    if cur is None:
        cur_requests = {}
        cur_cancels = {}
    else:
        cur_requests = getattr(cur, "requests", None) or {}
        cur_cancels = getattr(cur, "cancels", None) or {}
    changed = []
    for rid, meta in cur_requests.items():
        if rid not in prev_requests or prev_requests[rid] != meta:
            changed.append(rid)
    changed.sort()
    new_cancels = sorted(rid for rid in cur_cancels if rid not in prev_cancels)
    return RebaseScanDiff(changed, new_cancels)


def find_superseded_ids(requests):
    """同じプロジェクトで、より新しい依頼がある依頼の ID。古い方が superseded。"""
    groups = {}
    if not isinstance(requests, list):
        return set()
    for rec in requests:
        if not isinstance(rec, dict):
            continue
        key = rec.get("project_key")
        compare = normalize_project_key_for_compare(key)
        if not isinstance(compare, str) or compare == "":
            continue
        groups.setdefault(compare, []).append(rec)
    superseded = set()
    for items in groups.values():
        ranked = sorted(
            items,
            key=lambda rec: (rec.get("created_at"), rec.get("request_id")),
        )
        for rec in ranked[:-1]:
            rid = rec.get("request_id")
            if isinstance(rid, str):
                superseded.add(rid)
    return superseded


# --- 3.4 状態の読み書き(R-2.5、R-3) ---
def _normalize_uv_fills(value):
    enabled = False
    switched = 0
    skipped = 0
    failed = 0
    rolled_back = 0
    direction = 0
    prior = 0
    if isinstance(value, dict):
        raw_enabled = value.get("enabled", False)
        enabled = raw_enabled if isinstance(raw_enabled, bool) else False

        def _as_int(raw):
            if isinstance(raw, bool) or not isinstance(raw, int):
                return 0
            return raw

        switched = _as_int(value.get("switched", 0))
        skipped = _as_int(value.get("skipped", 0))
        failed = _as_int(value.get("failed", 0))
        rolled_back = _as_int(value.get("rolled_back", 0))
        direction = _as_int(value.get("direction", 0))
        prior = _as_int(value.get("prior", 0))
    return {
        "enabled": enabled,
        "switched": switched,
        "skipped": skipped,
        "failed": failed,
        "rolled_back": rolled_back,
        "direction": direction,
        "prior": prior,
    }


def build_rebase_status(
    request_id,
    state,
    now,
    plugin_version,
    *,
    reason=None,
    message="",
    backup_name=None,
    result_export_id=None,
    uv_fills=None,
):
    if not is_valid_rebase_request_id(request_id):
        raise ValueError("request_id が不正です")
    # 2026.10.19(1.4.0 PB-b 0.5): state / reason が str でないときは
    # TypeError ではなく ValueError(frozenset が unhashable を弾くため)。
    if not isinstance(state, str) or state not in _REBASE_STATE_SET:
        raise ValueError("state が不正です")
    if reason is not None and (
            not isinstance(reason, str) or reason not in _REBASE_STATUS_REASON_SET):
        raise ValueError("reason が不正です")
    if not _rebase_is_finite_number(now):
        raise ValueError("updated_at が不正です")
    out = {
        "schema": REBASE_SCHEMA,
        "request_id": request_id,
        "state": state,
        "updated_at": now,
        "plugin_version": plugin_version,
        "reason": reason,
        "message": _rebase_clip_message(message),
        "backup_name": _rebase_filename_only(backup_name),
        "result_export_id": result_export_id if isinstance(result_export_id, str) else None,
    }
    # uv_fills は PB-b の追加キー。None のときはキー自体を出さない。
    # Maya の parse_status は未知のキーを落とすので互換。契約 R-2.5 への追記は QUESTIONS。
    if uv_fills is not None:
        if isinstance(uv_fills, dict):
            out["uv_fills"] = _normalize_uv_fills(uv_fills)
        else:
            out["uv_fills"] = _normalize_uv_fills({})
    return out


def parse_rebase_status(raw):
    """Maya の parse_status と同じ検査・同じ正規化。uv_fills は落としてよい。"""
    if not isinstance(raw, dict):
        return None
    schema = raw.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != REBASE_SCHEMA:
        return None
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or request_id == "":
        return None
    state = raw.get("state")
    # 2026.10.19(1.4.0 PB-b 0.5): list / dict など unhashable な state で
    # TypeError を出さない(1 秒タイマーから read → write する経路)。
    if not isinstance(state, str) or state not in _REBASE_STATE_SET:
        return None
    updated_at = raw.get("updated_at")
    if not _rebase_is_finite_number(updated_at):
        return None
    return {
        "schema": REBASE_SCHEMA,
        "request_id": request_id,
        "state": state,
        "updated_at": updated_at,
        "plugin_version": _rebase_optional_text(raw.get("plugin_version")),
        "reason": _rebase_optional_text(raw.get("reason")),
        "message": _rebase_clip_message(raw.get("message")),
        "backup_name": _rebase_filename_only(raw.get("backup_name")),
        "result_export_id": _rebase_optional_text(raw.get("result_export_id")),
    }


def read_rebase_status(root, rid):
    if not is_valid_rebase_request_id(rid):
        return None
    try:
        path = rebase_status_path(root, rid)
    except ValueError:
        return None
    raw = read_json_safe(path)
    if raw is None:
        return None
    parsed = parse_rebase_status(raw)
    if parsed is None or parsed.get("request_id") != rid:
        return None
    return parsed


def is_valid_rebase_transition(prev_state, new_state):
    # 2026.10.19(1.4.0 PB-b 0.5): unhashable な prev_state / new_state で
    # TypeError を出さない。
    if prev_state is not None and not isinstance(prev_state, str):
        return False
    if not isinstance(new_state, str):
        return False
    allowed = _REBASE_TRANSITIONS.get(prev_state)
    if allowed is None:
        return False
    return new_state in allowed


def write_rebase_status(root, status):
    """遷移が不正なら書かずに False。壊れた既存ファイルは復旧のため書いてよい。"""
    parsed = parse_rebase_status(status)
    if parsed is None:
        _log("error", "rebase: 状態の内容が不正なため書きません")
        return False
    rid = parsed["request_id"]
    if not is_valid_rebase_request_id(rid):
        _log("error", "rebase: request_id が不正なため状態を書きません")
        return False
    try:
        path = rebase_status_path(root, rid)
    except ValueError:
        _log("error", "rebase: 状態パスを作れません")
        return False
    existing = read_rebase_status(root, rid)
    if existing is None:
        try:
            exists = os.path.isfile(path)
        except OSError:
            exists = False
        if exists:
            # 既存が壊れていて読めないときは書いてよい(復旧)。
            pass
        elif not is_valid_rebase_transition(None, parsed["state"]):
            _log(
                "warning",
                "rebase: 不正な遷移 None -> {0}".format(parsed["state"]),
            )
            return False
    elif not is_valid_rebase_transition(existing.get("state"), parsed["state"]):
        _log(
            "warning",
            "rebase: 不正な遷移 {0} -> {1}".format(
                existing.get("state"), parsed["state"]),
        )
        return False
    payload = dict(parsed)
    if isinstance(status, dict) and "uv_fills" in status and status.get("uv_fills") is not None:
        payload["uv_fills"] = _normalize_uv_fills(status.get("uv_fills"))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json_atomic(path, payload)
        return True
    except Exception as exc:
        _log("error", "rebase: 状態の書き込みに失敗しました: {0}".format(exc))
        return False


def build_interrupted_status(prev_status, now, plugin_version):
    """非終端なら failed / interrupted。終端なら None。起動時の中断の土台。"""
    if not isinstance(prev_status, dict):
        return None
    state = prev_status.get("state")
    # 2026.10.19(1.4.0 PB-b 0.5): unhashable な state で例外を出さない。
    if not isinstance(state, str) or state not in _REBASE_STATE_SET or state in _REBASE_TERMINAL:
        return None
    request_id = prev_status.get("request_id")
    return build_rebase_status(
        request_id,
        "failed",
        now,
        plugin_version,
        reason="interrupted",
        message=REBASE_INTERRUPTED_MESSAGE,
        backup_name=prev_status.get("backup_name"),
        result_export_id=prev_status.get("result_export_id"),
    )


def rebase_failure_message(reason, *, backup_name=None, detail=None):
    # 2026.10.19(1.4.0 PB-b 0.5): reason が str でないときは既定文。
    if not isinstance(reason, str):
        text = ""
    else:
        text = REBASE_REASON_MESSAGES.get(reason, "")
    if not text:
        text = "リベースに失敗しました"
    name = _rebase_filename_only(backup_name)
    if isinstance(reason, str) and reason in _REBASE_BACKUP_MESSAGE_REASONS and name:
        text = "{0} バックアップ: {1}".format(text, name)
    if isinstance(detail, str) and detail:
        text = "{0} {1}".format(text, detail)
    return _rebase_clip_message(text)


# --- 3.5 承認判定 decide_approval(R-5.2) ---
def _painter_state_get(painter_state, key):
    if not isinstance(painter_state, dict) or key not in painter_state:
        return _PAINTER_STATE_DEFAULTS[key]
    return painter_state[key]


# ---------------------------------------------------------------------------
# 2026.10.19(1.4.0 PB-b): リベースの状態機械と UV フィル(Qt 非依存)
# ---------------------------------------------------------------------------
# --- 2026.10.19(1.4.0 PB-b): 拒否の状態、UV フィル、ジョブ、司令塔 ---
REBASE_EXPORT_DEFERRED_STATUS = (
    "リベース中のため、書き出しはリベースの終了後に行います"
)
REBASE_UV_FILL_PROGRESS = (
    "UV 投影のフィルを旧 UV セット経由に切り替えています"
)
REBASE_AUTO_UNWRAP_UNSUPPORTED_LOG = (
    "この版は自動展開の設定を渡せません。プロジェクトの自動ラップ解除の設定に従います"
)
REBASE_TERMINAL_WRITE_MAX = 10


def rebase_reject_state(reason, prev_state):
    """decide_approval の reject 理由と今の状態から、書く (state, reason) を決める。

    今の状態が None のときは理由によらず rejected(None からは received / rejected だけ)。
    """
    if not isinstance(reason, str) or reason == "":
        reason = "bad_request"
    if prev_state is None:
        return ("rejected", reason)
    if reason == "cancelled":
        return ("declined", "cancelled")
    if reason == "superseded":
        return ("superseded", "superseded")
    if reason == "expired":
        if prev_state in ("received", "awaiting_approval"):
            return ("expired", "expired")
        return ("failed", "expired")
    if prev_state == "received":
        return ("rejected", reason)
    if prev_state in ("awaiting_approval", "preparing"):
        return ("failed", reason)
    return ("failed", reason)


def get_uv_old_index(request):
    """依頼の uv_old.index。1〜7 の int だけ。無ければ None。例外は出さない。"""
    if not isinstance(request, dict):
        return None
    uv_old = request.get("uv_old")
    if not isinstance(uv_old, dict):
        return None
    index = uv_old.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if index < REBASE_UV_OLD_INDEX_MIN or index > REBASE_UV_OLD_INDEX_MAX:
        return None
    return index


def get_uv_old_name(request):
    """ログ用。照合には使わない(要実機確認: Painter は UV セット名を API で取れない)。"""
    if not isinstance(request, dict):
        return None
    uv_old = request.get("uv_old")
    if not isinstance(uv_old, dict):
        return None
    name = uv_old.get("name")
    if isinstance(name, str) and name:
        return name
    return None
