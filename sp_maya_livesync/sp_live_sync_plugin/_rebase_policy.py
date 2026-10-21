"""リベースの承認判定、メッシュ事前確認、Painter 状態、バックアップ名、再試行、
承認文言(Painter / Qt に依存しない)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os
import stat
import math
import inspect

from ._base import _hash_file_hex, is_valid_rebase_request_id
from ._rebase_protocol import (
    ApprovalDecision,
    REBASE_AUTO_UNWRAP_NOTE,
    REBASE_BACKUP_DIRNAME,
    REBASE_CAUTION_TEXT,
    REBASE_CAUTION_TEXT_REPROJECT,
    REBASE_CAUTION_TEXT_REPROJECT_PRIOR,
    REBASE_CONFIRM_LEAD,
    REBASE_DIRECTION_APPROVAL_NOTE,
    REBASE_PRIOR_FILL_WARNING_FMT,
    REBASE_UNSAVED_NOTE,
    REBASE_WAIT_MESSAGES,
    REBASE_WORKFLOW_GUIDANCE,
    _REBASE_TERMINAL,
    _REBASE_WIN_BAD_FILENAME,
    _painter_state_get,
    _rebase_coerce_retry_max,
    _rebase_is_finite_number,
    _rebase_reload_problem,
    effective_rebase_approval,
    normalize_rebase_settings,
    project_keys_match,
)

__all__ = [
    "needs_saving_for_rebase",
    "decide_approval",
    "_prior_uv_fills_block_auto",
    "wait_timed_out",
    "mesh_precheck",
    "_REBASE_PRECHECK_CACHE_MAX",
    "RebasePrecheckCaches",
    "supports_auto_unwrap_settings",
    "compute_last_edit_age",
    "build_painter_state",
    "_rebase_safe_stem",
    "rebase_backup_path",
    "rebase_retry_delay",
    "classify_reload_status",
    "build_reload_settings_kwargs",
    "build_rebase_approval_text",
    "rebase_wait_message",
]


def needs_saving_for_rebase(snap, settings):
    """needs_saving が読めないとき、ask は偽、auto だけ真(待つ)。

    読めた値は従来どおり bool にする。設定が auto でも effective が ask なら偽。
    """
    if not isinstance(snap, dict):
        snap = {}
    if snap.get("needs_saving_missing"):
        return effective_rebase_approval(settings) == "auto"
    value = snap.get("needs_saving")
    if value is None:
        return False
    return bool(value)


def decide_approval(request, painter_state, settings):
    """R-5.2 の 15 行。上が優先。action は ask / run / wait / reject。"""
    if not isinstance(request, dict) or not _rebase_is_finite_number(request.get("expires_at")):
        return ApprovalDecision("reject", "bad_request")
    if not isinstance(painter_state, dict) or not _rebase_is_finite_number(painter_state.get("now")):
        return ApprovalDecision("reject", "bad_request")
    rounded = normalize_rebase_settings(settings)
    now = painter_state.get("now")
    expires_at = request.get("expires_at")
    if rounded.get("rebase_enabled") is False:
        return ApprovalDecision("reject", "disabled")
    if bool(_painter_state_get(painter_state, "cancelled")):
        return ApprovalDecision("reject", "cancelled")
    if bool(_painter_state_get(painter_state, "superseded")):
        return ApprovalDecision("reject", "superseded")
    if now > expires_at:
        return ApprovalDecision("reject", "expired")
    if not bool(_painter_state_get(painter_state, "project_open")):
        return ApprovalDecision("wait", "no_project")
    if not project_keys_match(
            request.get("project_key"),
            _painter_state_get(painter_state, "project_key")):
        return ApprovalDecision("wait", "project_mismatch")
    saved_path = _painter_state_get(painter_state, "saved_path")
    if saved_path is None or saved_path == "":
        return ApprovalDecision("reject", "unsaved_project")
    if not bool(_painter_state_get(painter_state, "mesh_exists")):
        return ApprovalDecision("reject", "mesh_missing")
    mesh_sha1_ok = _painter_state_get(painter_state, "mesh_sha1_ok")
    if mesh_sha1_ok is False:
        return ApprovalDecision("reject", "mesh_hash_mismatch")
    if bool(_painter_state_get(painter_state, "busy")) or bool(
            _painter_state_get(painter_state, "exporting")):
        return ApprovalDecision("wait", "busy")
    approval = effective_rebase_approval(rounded)
    can_disable = bool(_painter_state_get(painter_state, "can_disable_auto_unwrap"))
    if approval == "auto" and not can_disable:
        return ApprovalDecision("ask", "auto_unwrap_unverified")
    # 前回切り替えた UV 投影フィルがあると auto を ask に落とす。
    # ここで return しない。未保存の wait などを飛ばさないため。
    prior_uv_fills = _painter_state_get(painter_state, "prior_uv_fills")
    demoted_for_prior = False
    if approval == "auto" and _prior_uv_fills_block_auto(prior_uv_fills):
        approval = "ask"
        demoted_for_prior = True
    idle_s = rounded.get("rebase_auto_idle_s", 10.0)
    last_edit_age_s = _painter_state_get(painter_state, "last_edit_age_s")
    if approval == "auto":
        if last_edit_age_s is None or not _rebase_is_finite_number(last_edit_age_s):
            return ApprovalDecision("wait", "recent_edit")
        if last_edit_age_s < idle_s:
            return ApprovalDecision("wait", "recent_edit")
    needs_saving = bool(_painter_state_get(painter_state, "needs_saving"))
    backup_includes = bool(_painter_state_get(painter_state, "backup_includes_unsaved"))
    if needs_saving and (approval == "auto" or not backup_includes):
        return ApprovalDecision("wait", "unsaved_changes")
    if approval == "auto":
        return ApprovalDecision("run", "auto_approved")
    if demoted_for_prior:
        return ApprovalDecision("ask", "prior_uv_fills")
    return ApprovalDecision("ask", "needs_approval")


def _prior_uv_fills_block_auto(value):
    """auto を ask に落とす prior_uv_fills か。None と 0 は落とさない。"""
    if value == "unknown":
        return True
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= 1


def wait_timed_out(first_wait_at, now, settings):
    """True になった依頼は PB-b が expired(理由 expired)にする。契約に無い。仮。"""
    if first_wait_at is None:
        return False
    if not _rebase_is_finite_number(first_wait_at) or not _rebase_is_finite_number(now):
        return False
    rounded = normalize_rebase_settings(settings)
    return (now - first_wait_at) > rounded.get("rebase_wait_max_s", 1800.0)


# --- 3.6 事前チェックの入力の組み立て(R-5.3) ---
def mesh_precheck(mesh, cache, *, stat_fn=os.stat, hash_fn=None):
    """(mesh_exists, mesh_sha1_ok, new_cache)。同じ size/mtime_ns では hash を再計算しない。"""
    if not isinstance(mesh, dict):
        return False, None, None
    path = mesh.get("path")
    if not isinstance(path, str) or path == "":
        return False, None, None
    try:
        st = stat_fn(path)
    except Exception:
        return False, None, None
    try:
        mode = st.st_mode
        if not stat.S_ISREG(mode):
            return False, None, None
        size = int(st.st_size)
        mtime_ns = int(st.st_mtime_ns)
    except (AttributeError, TypeError, ValueError):
        return False, None, None
    if (
        isinstance(cache, dict)
        and cache.get("path") == path
        and cache.get("size") == size
        and cache.get("mtime_ns") == mtime_ns
        and "sha1_ok" in cache
    ):
        return True, cache.get("sha1_ok"), cache
    want_size = mesh.get("size")
    if want_size != size:
        new_cache = {
            "path": path,
            "size": size,
            "mtime_ns": mtime_ns,
            "sha1_ok": False,
        }
        return True, False, new_cache
    if hash_fn is None:
        def hash_fn(hash_path):
            return _hash_file_hex(hash_path, "sha1")
    try:
        digest = hash_fn(path)
    except OSError:
        return False, None, None
    want_sha1 = mesh.get("sha1")
    if not isinstance(digest, str) or not isinstance(want_sha1, str):
        sha1_ok = False
    else:
        sha1_ok = digest.lower() == want_sha1.lower()
    new_cache = {
        "path": path,
        "size": size,
        "mtime_ns": mtime_ns,
        "sha1_ok": sha1_ok,
    }
    return True, sha1_ok, new_cache


# 2026.10.19(1.4.0 PB-b 0.5): mesh_precheck のキャッシュは依頼ごとに持つ。
# 同じファイルを指す別依頼(sha1 / size が違う)へ古い sha1_ok を渡さない。
# 関数 mesh_precheck 自体の挙動は変えない。
_REBASE_PRECHECK_CACHE_MAX = 32


class RebasePrecheckCaches(object):
    """request_id -> mesh_precheck の cache。終端・閉じる・shutdown で捨てる。"""

    def __init__(self, max_entries=_REBASE_PRECHECK_CACHE_MAX):
        self._by_id = {}
        self._max_entries = max(1, int(max_entries))

    def get(self, request_id):
        return self._by_id.get(request_id)

    def put(self, request_id, cache):
        if not isinstance(request_id, str) or request_id == "":
            return
        if request_id in self._by_id:
            self._by_id.pop(request_id, None)
        self._by_id[request_id] = cache
        while len(self._by_id) > self._max_entries:
            oldest = next(iter(self._by_id))
            self._by_id.pop(oldest, None)

    def drop(self, request_id):
        self._by_id.pop(request_id, None)

    def drop_if_terminal(self, request_id, state):
        if isinstance(state, str) and state in _REBASE_TERMINAL:
            self.drop(request_id)

    def clear(self):
        self._by_id.clear()

    def __contains__(self, request_id):
        return request_id in self._by_id

    def __len__(self):
        return len(self._by_id)


def supports_auto_unwrap_settings(settings_cls):
    """MeshReloadingSettings が auto_unwrap_settings を持てるか。判定できなければ False。"""
    if settings_cls is None:
        return False
    try:
        signature = inspect.signature(settings_cls)
        if "auto_unwrap_settings" in signature.parameters:
            return True
    except Exception:
        pass
    try:
        if hasattr(settings_cls, "auto_unwrap_settings"):
            return True
    except Exception:
        return False
    return False


def compute_last_edit_age(now, last_edit_at):
    if last_edit_at is None or not _rebase_is_finite_number(last_edit_at):
        return None
    if not _rebase_is_finite_number(now):
        return None
    return max(0.0, float(now) - float(last_edit_at))


def build_painter_state(
    *,
    now,
    is_open,
    in_edition,
    project_key,
    saved_path,
    busy,
    exporting,
    needs_saving,
    backup_includes_unsaved,
    mesh_exists,
    mesh_sha1_ok,
    superseded,
    cancelled,
    can_disable_auto_unwrap,
    last_edit_at,
    prior_uv_fills=None,
):
    """decide_approval の入力。project_open = is_open and in_edition。

    prior_uv_fills が None のときはキーを出さない(既存の 14 キーを保つ)。
    """
    if mesh_sha1_ok is True or mesh_sha1_ok is False or mesh_sha1_ok is None:
        sha1_ok = mesh_sha1_ok
    else:
        sha1_ok = None
    state = {
        "now": now,
        "project_open": bool(is_open) and bool(in_edition),
        "project_key": project_key,
        "saved_path": saved_path,
        "busy": bool(busy),
        "exporting": bool(exporting),
        "needs_saving": bool(needs_saving),
        "backup_includes_unsaved": bool(backup_includes_unsaved),
        "mesh_exists": bool(mesh_exists),
        "mesh_sha1_ok": sha1_ok,
        "superseded": bool(superseded),
        "cancelled": bool(cancelled),
        "can_disable_auto_unwrap": bool(can_disable_auto_unwrap),
        "last_edit_age_s": compute_last_edit_age(now, last_edit_at),
    }
    if prior_uv_fills is not None:
        state["prior_uv_fills"] = prior_uv_fills
    return state


# --- 3.7 バックアップ・再試行・読み直し ---
def _rebase_safe_stem(name):
    cleaned = []
    for char in name:
        if char in _REBASE_WIN_BAD_FILENAME:
            cleaned.append("_")
        else:
            cleaned.append(char)
    stem = "".join(cleaned)
    return stem or "project"


def rebase_backup_path(spp_path, request_id, exists_fn=os.path.exists):
    """(フルパス, ファイル名, バックアップのフォルダ)。戻りは / 区切り。"""
    if not isinstance(spp_path, str) or spp_path == "":
        raise ValueError("spp_path が不正です")
    if not is_valid_rebase_request_id(request_id):
        raise ValueError("request_id が不正です")
    normalized = spp_path.replace("\\", "/")
    if "/" in normalized:
        parent, filename = normalized.rsplit("/", 1)
    else:
        parent = ""
        filename = normalized
    if filename.lower().endswith(".spp"):
        stem = filename[:-4]
    else:
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = _rebase_safe_stem(stem)
    if parent:
        folder = parent + "/" + REBASE_BACKUP_DIRNAME
    else:
        folder = REBASE_BACKUP_DIRNAME
    name = "{0}_before_rebase_{1}.spp".format(stem, request_id)
    full = folder + "/" + name
    suffix = 2
    while exists_fn(full):
        name = "{0}_before_rebase_{1}_{2}.spp".format(stem, request_id, suffix)
        full = folder + "/" + name
        suffix += 1
        if suffix > 10000:
            raise ValueError("バックアップ名を作れません")
    return full, name, folder


def rebase_retry_delay(attempt, retry_max):
    """これから行う再試行の番号(1 始まり)。上限超過または retry_max=0 なら None。"""
    rounded_max = _rebase_coerce_retry_max(retry_max)
    if not isinstance(attempt, bool) and isinstance(attempt, (int, float)):
        if isinstance(attempt, float) and (not math.isfinite(attempt) or not attempt.is_integer()):
            return None
        attempt = int(attempt)
    else:
        return None
    if attempt > rounded_max:
        return None
    if attempt < 1:
        return None
    return min(0.5 * (2 ** (attempt - 1)), 8.0)


def classify_reload_status(status):
    """ReloadMeshStatus の name を success / error / other に分ける。"""
    if status is None:
        return "other"
    name = getattr(status, "name", None)
    token = None
    if isinstance(name, str) and name:
        token = name
    else:
        text = str(status)
        if "." in text:
            token = text.rsplit(".", 1)[-1]
        else:
            token = text
    token = token.lower()
    if token == "success":
        return "success"
    if token == "error":
        return "error"
    return "other"


def build_reload_settings_kwargs(request, supports_auto_unwrap):
    """mesh_settings は渡さない。auto_unwrap_settings は止められる版だけ None を足す。"""
    if not isinstance(request, dict):
        raise ValueError("bad_settings")
    reload_problem = _rebase_reload_problem(request.get("reload"))
    if reload_problem is not None:
        raise ValueError("bad_settings")
    out = {
        "import_cameras": False,
        "preserve_strokes": True,
    }
    if supports_auto_unwrap:
        out["auto_unwrap_settings"] = None
    return out


# --- 3.8 文言(R-8) ---
def build_rebase_approval_text(
    request,
    *,
    backup_dir,
    needs_saving=False,
    can_disable_auto_unwrap=True,
    backup_includes_unsaved=False,
    uv_fill_reproject=False,
    has_direction_fills=False,
    prior_fill_count=0,
    prior_fill_names=None,
):
    """確認ダイアログの本文。注意文と運用案内は常に入れる。uv_old は見ない。"""
    parts = [REBASE_CONFIRM_LEAD]
    ts_names = None
    if isinstance(request, dict):
        ts_names = request.get("ts_names")
    if isinstance(ts_names, list):
        names = [name for name in ts_names if isinstance(name, str) and name]
        if names:
            parts.append("対象: {0}".format(", ".join(names)))
    has_prior = (
        isinstance(prior_fill_count, int)
        and not isinstance(prior_fill_count, bool)
        and prior_fill_count >= 1
    )
    if uv_fill_reproject and has_prior:
        caution = REBASE_CAUTION_TEXT_REPROJECT_PRIOR
    elif uv_fill_reproject:
        caution = REBASE_CAUTION_TEXT_REPROJECT
    else:
        caution = REBASE_CAUTION_TEXT
    parts.append(caution.format(backup_dir=backup_dir))
    if has_prior:
        shown = []
        if isinstance(prior_fill_names, (list, tuple)):
            for name in prior_fill_names:
                if isinstance(name, str) and name:
                    shown.append(name[:64])
                if len(shown) >= 10:
                    break
        label = ", ".join(shown) if shown else "名前不明"
        parts.append(REBASE_PRIOR_FILL_WARNING_FMT.format(
            n=prior_fill_count, names=label))
    parts.append(REBASE_WORKFLOW_GUIDANCE)
    if has_direction_fills:
        parts.append(REBASE_DIRECTION_APPROVAL_NOTE)
    if needs_saving and backup_includes_unsaved:
        parts.append(REBASE_UNSAVED_NOTE)
    if not can_disable_auto_unwrap:
        parts.append(REBASE_AUTO_UNWRAP_NOTE)
    return "\n".join(parts)


def rebase_wait_message(reason):
    """待機理由に対応する利用者向け文言。未知の理由は空文字。"""
    if not isinstance(reason, str):
        return ""
    return REBASE_WAIT_MESSAGES.get(reason, "")
