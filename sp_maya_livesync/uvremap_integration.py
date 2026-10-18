# -*- coding: utf-8 -*-
"""Live Sync 窓への UV 再マップ組み込み(基準記録・今すぐ変換・原本に戻す)。

Maya / Qt は関数の中だけ。純粋部分はどちらも無しでテストできる。
失敗しても呼び出し元(maya_live_sync)が Live Sync 本体を止めないこと。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from uvremap_follow import (
    LABEL_APPLY,
    MSG_AUTO_INPUT_AS_IS,
    MSG_INPUT_STALE,
    MSG_PAINTER_START_FMT,
    MSG_PEND_FMT,
    MSG_QUEUE_FMT,
    MSG_UV_SLOW,
    MSG_UV_STALE,
    MSG_UV_START_FMT,
    STATE_CONVERTED_OK,
    STATE_STALE,
    _flag_is_newer,
    _settled_ns,
    apply_button_visible,
    decide_follow,
    decide_uv_follow,
    fold_status_label_p2,
    input_stale,
    load_follow,
    normalize_follow_settings,
    save_follow,
    fingerprint_maps_differ,
    source_signature,
    uv_recorded_differs,
)
from uvremap_runtime import (
    MSG_ALREADY_RUNNING,
    MSG_INTERRUPTED,
    MSG_NO_BASELINE,
    MSG_NO_FILE_NODES,
    MSG_SCENE_CHANGED,
    WARN_DISABLED,
    WARN_FILE_MISSING,
    WARN_NO_FILE_NODES,
    WARN_SCENE_CHANGED,
)
import uvremap_bridge as br
import uvremap_rebase as rb

DEFAULT_UVREMAP_ROOT = "C:/SPMayaLiveSync/uvremap"
CONVERT_TIMEOUT_S = 1200
POLL_INTERVAL_MS = 1000
UV_SLOW_LIMIT_S = 0.2
UV_SLOW_POLL_S = 10.0

MSG_NO_PROJECT = (
    "作業対象のプロジェクトがリンクされていません。"
    "先にプロジェクト連携を設定してください"
)
MSG_NO_FINAL = (
    "このプロジェクトの Final 書き出し先が未登録です。"
    "Painter で一度 Final を書き出してください"
)
MSG_NO_TARGETS = "対象のテクスチャセットがありません"
MSG_DISABLED = "UV変換は無効のため、変換は行いません"
MSG_NOT_STARTED = "変換を開始できませんでした。下の状態表示の理由を確認してください"
MSG_DISABLED_SKIP = "UV変換が無効のため、変換結果の切り替えを見送りました"
MSG_BASELINE_EXISTS = (
    "基準は記録済みです。作り直すには、作業フォルダの baseline を消してから"
    "再度「基準UVを記録」を押してください"
)
MSG_CONFIRM_BASELINE = (
    "今のUVを基準にします。Painter に送ったメッシュと同じUVですか?"
)
MSG_HINT_RECORD = "Painter へメッシュを送った直後に、1 回だけ押してください"
MSG_FOLDER_INSIDE = (
    "作業フォルダが Live Sync の監視フォルダまたは Final フォルダの内側になるため、"
    "書き込みを中止しました"
)
MSG_FILE_MISSING = "変換後のファイルが見つからないため、切り替えを見送りました"
MSG_FILE_PARTIAL = (
    "一部のチャンネルだけ切り替えました。変換後のファイルが無いチャンネルは見送りました"
)
MSG_CONVERT_DONE_FMT = "変換が完了しました({ts_name})"
MSG_CONVERT_FAILED_FMT = "変換に失敗しました({ts_name})。{message}"
MSG_CONVERT_FAILED_BARE_FMT = "変換に失敗しました({ts_name})。"
MSG_CONVERT_FAILED_REVERTED_FMT = "変換に失敗し、原本に戻しました({ts_name})。{message}"
MSG_CONVERT_FAILED_REVERTED_BARE_FMT = "変換に失敗し、原本に戻しました({ts_name})。"
MSG_CONVERT_BLOCKED_FMT = "変換できませんでした({ts_name})。{message}"
MSG_CONVERT_BLOCKED_BARE_FMT = "変換できませんでした({ts_name})。"
MSG_CONVERT_BLOCKED_REVERTED_FMT = "変換できませんでした。原本に戻しました({ts_name})。{message}"
MSG_CONVERT_BLOCKED_REVERTED_BARE_FMT = "変換できませんでした。原本に戻しました({ts_name})。"
MSG_REVERT_NONE = "戻す対象はありませんでした"
MSG_REVERT_OK = "原本に戻しました"
MSG_REVERT_OK_FMT = "原本に戻しました({n} 件)"
MSG_REVERT_FAIL = "原本への復帰に失敗しました"
MSG_SCENE_SWITCHED = "シーンが切り替わりました"
LABEL_FOLD_SHOW = "▶ 詳細を表示"
LABEL_FOLD_HIDE = "▼ 詳細を隠す"
LABEL_FOLD_OFF = "無効"
LABEL_FOLD_ON = "有効"
LABEL_FOLD_BUSY = "変換中"
MSG_ENABLED = "UV変換を有効にしました"
MSG_DISABLED_NO_REVERT = "UV変換を無効にしました"
MSG_DISABLED_REVERTED = "UV変換を無効にし、原本に戻しました"
MSG_DISABLED_REVERTED_FMT = "UV変換を無効にし、原本に戻しました({n} 件)"

# 2026.10.19(RB-d): リベースの画面文言。決定は純関数、文言は定数。
MSG_REBASE_NEED_KILLSWITCH = "UV変換が無効のため、Painterへは送れません"
MSG_REBASE_NEED_BASELINE = "基準UVが未記録です。先に『基準UVを記録』を押してください"
MSG_REBASE_NEED_SAVED_SCENE = "シーンを保存してから送ってください"
MSG_REBASE_PLUGIN_UNKNOWN = (
    "Painter の応答を確認できません。Painter で Final を 1 回書き出してから送ってください"
)
MSG_REBASE_CONFIRM_REPLACE = "Painter へ送った依頼がまだ終わっていません。新しい依頼で置き換えますか"
MSG_REBASE_LOCKED_NO_REPLACE = "Painter が読み直しています。終わってから送ってください"
MSG_REBASE_SENT = "Painter へ送りました。Painter で確認してください"
MSG_REBASE_EXPORT_FAILED_FMT = "メッシュの書き出しに失敗したため、送りませんでした。{message}"
MSG_REBASE_CANCEL_SENT = "取り消しを送りました"
# 契約に無い。仮: 確認で送らなかったときの文。
MSG_REBASE_SEND_CANCELLED = "送りませんでした"
# 契約に無い。仮: 想定外の失敗。例外の文と絶対パスは出さない。
MSG_REBASE_SEND_FAILED = "Painterへ送れませんでした"
MSG_REBASE_PARTIAL_FMT = "完了処理が途中で止まりました({step})。もう一度試します"
# 契約に無い。仮: 再試行の上限を超えたときの文。
MSG_REBASE_RETRY_EXHAUSTED = "完了処理を繰り返せませんでした。手動で確認してください"
MSG_UVOLD_ATTACHED_FMT = "旧UVセット({name})を付けて送りました"
MSG_UVOLD_SKIPPED_FMT = "旧UVセットは付けずに送りました({reason})。2D ビューの内容の再投影はできません"
MSG_UVOLD_CLEANUP_FAILED_FMT = (
    "一時的に作った旧UVセット({name})を削除できませんでした。"
    "UV セットエディタで手動で削除してください: {shapes}"
)
# 契約に無い。仮: 完了しているのに切り替えを待っているときの短い文。
MSG_REBASE_DONE_SKIPPED_FMT = {
    "killswitch_off": "UV変換が無効のため、完了を待っています",
    "scene_changed": "シーンが変わったため、完了を待っています",
    "job_running": "変換が終わるまで、完了を待っています",
}
# 契約に無い。仮: 完了処理の再試行はこの回数まで。
REBASE_COMPLETE_RETRY_MAX = 5
REBASE_REQUEST_ID_ATTEMPTS = 5
_REBASE_UNCONFIRMED = frozenset({
    "result_missing",
    "result_not_ok",
    "result_request_id_mismatch",
    "not_full_final",
})
_REBASE_SKIP_WAIT = frozenset({"killswitch_off", "scene_changed"})

WARN_MESSAGES = {
    WARN_NO_FILE_NODES: MSG_NO_FILE_NODES,
    WARN_FILE_MISSING: MSG_FILE_MISSING,
    WARN_SCENE_CHANGED: MSG_SCENE_CHANGED,
    WARN_DISABLED: MSG_DISABLED_SKIP,
}


def known_ts_names(known_ts):
    """既知 TS の名前リスト。list / dict / その他の反復可能を受け付ける。"""
    if not known_ts:
        return []
    if isinstance(known_ts, dict):
        items = list(known_ts.keys())
    else:
        try:
            items = list(known_ts)
        except TypeError:
            return []
    names = []
    seen = set()
    for item in items:
        name = str(item)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def resolve_targets(known_ts, texture_sets):
    """既知 TS のうちシーンに存在するものと、その shape の和集合。

    Returns:
        (ts_names, shape_paths, missing)
    """
    wanted = known_ts_names(known_ts)
    ts_map = texture_sets if isinstance(texture_sets, dict) else {}
    ts_names = []
    missing = []
    shapes = set()
    for name in wanted:
        entries = ts_map.get(name)
        if not entries:
            missing.append(name)
            continue
        ts_names.append(name)
        if not isinstance(entries, (list, tuple)):
            entries = [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            shape = entry.get("shape")
            if shape:
                shapes.add(str(shape))
    return ts_names, sorted(shapes), missing


def ts_by_shape_from_texture_sets(texture_sets):
    """``list_texture_sets`` の結果から ``{シェイプ名: (TS名, ...)}`` を作る。

    TS 名は昇順。シーン走査はしない。``uv_tick`` はこの結果をメモリから使う。
    """
    mapping = {}
    if not isinstance(texture_sets, dict):
        return {}
    for ts_name, entries in texture_sets.items():
        if not isinstance(ts_name, str) or not ts_name:
            continue
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            shape = entry.get("shape")
            if not isinstance(shape, str) or not shape:
                continue
            mapping.setdefault(shape, set()).add(ts_name)
    return {shape: tuple(sorted(names)) for shape, names in sorted(mapping.items())}


def _split_fingerprint_result(result):
    """``(mapping, elapsed_s)`` または mapping。合わなければ ``(None, 0.0)``。"""
    mapping = None
    elapsed = 0.0
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], dict):
        mapping = result[0]
        try:
            elapsed = float(result[1])
        except (TypeError, ValueError):
            elapsed = 0.0
    elif isinstance(result, dict):
        mapping = result
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        elapsed = 0.0
    if elapsed < 0:
        elapsed = 0.0
    return mapping, float(elapsed)


def uvremap_root_for_project(root_dir, final_subfolder):
    """作業フォルダ ``<uvremap_root_dir>/<Finalサブフォルダ名>``。欠けていれば None。"""
    if not root_dir or not final_subfolder:
        return None
    return os.path.normpath(os.path.join(str(root_dir), str(final_subfolder)))


def output_dir_for_root(uvremap_root):
    """変換結果の出力先 ``<uvremap_root>/output/final``。"""
    if not uvremap_root:
        return None
    return os.path.normpath(os.path.join(str(uvremap_root), "output", "final"))


def path_is_under(path, parent):
    """path が parent 自身、またはその内側なら True。"""
    if not path or not parent:
        return False
    p = os.path.normcase(os.path.normpath(str(path)))
    par = os.path.normcase(os.path.normpath(str(parent)))
    if p == par:
        return True
    prefix = par.rstrip("\\/") + os.sep
    return p.startswith(prefix)


def work_folder_is_safe(uvremap_root, final_dir=None, watch_dir=None, final_export_dir=None):
    """作業フォルダが Live Sync の管理フォルダの内側にならないこと。"""
    if not uvremap_root:
        return False
    for parent in (final_dir, watch_dir, final_export_dir):
        if parent and path_is_under(uvremap_root, parent):
            return False
        out = output_dir_for_root(uvremap_root)
        if parent and out and path_is_under(out, parent):
            return False
    return True


def baseline_exists(root):
    """``baseline/active.json`` または ``baseline/*/mesh.npz`` があれば True。"""
    if not root:
        return False
    base = Path(root) / "baseline"
    active = base / "active.json"
    try:
        if active.is_file():
            return True
        if not base.is_dir():
            return False
        for child in base.iterdir():
            if child.is_dir() and (child / "mesh.npz").is_file():
                return True
    except OSError:
        return False
    return False


def decide_baseline_prompt(has_baseline):
    """画面側の確認文言。(kind, message)。kind は already / confirm。"""
    if has_baseline:
        return "already", MSG_BASELINE_EXISTS
    return "confirm", MSG_CONFIRM_BASELINE


def _listed_count(value):
    """list / tuple なら件数。それ以外は None。"""
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


def warning_message(event):
    """poll イベントの warning を、status.json と矛盾しない日本語にする。"""
    if not isinstance(event, dict):
        return ""
    warning = event.get("warning")
    if not warning:
        return ""
    preset = WARN_MESSAGES.get(str(warning))
    text = event.get("message")
    if warning == WARN_FILE_MISSING:
        switched = _listed_count(event.get("switched"))
        if switched is not None and switched > 0:
            return MSG_FILE_PARTIAL
        return preset or MSG_FILE_MISSING
    if isinstance(text, str) and text.strip():
        return text
    return preset or str(warning)


def _event_str(event, key):
    value = event.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _with_optional_detail(bare_fmt, detail_fmt, ts_name, detail):
    """detail が空なら続く文を付けない。句点は 1 文につき 1 つ。"""
    if detail:
        return detail_fmt.format(ts_name=ts_name, message=detail)
    return bare_fmt.format(ts_name=ts_name)


def completion_message(event):
    """警告が無い完了・失敗の文言。出さないときは空文字。

    警告の優先(表示は警告文、履歴は完了文も残す)は poll が決める。
    この関数は state から完了文だけを作る。FAILED / BLOCKED は
    イベントの reverted が 1 件以上のときだけ復帰の文にする。
    """
    if not isinstance(event, dict):
        return ""
    if event.get("exit_code") == 10:
        return ""
    state = event.get("state")
    ts_name = _event_str(event, "ts_name")
    detail = _event_str(event, "message")
    reverted = _listed_count(event.get("reverted"))
    did_revert = reverted is not None and reverted > 0
    try:
        if state == "CONVERTED_OK":
            return MSG_CONVERT_DONE_FMT.format(ts_name=ts_name)
        if state == "FAILED":
            if did_revert:
                return _with_optional_detail(
                    MSG_CONVERT_FAILED_REVERTED_BARE_FMT,
                    MSG_CONVERT_FAILED_REVERTED_FMT,
                    ts_name,
                    detail,
                )
            return _with_optional_detail(
                MSG_CONVERT_FAILED_BARE_FMT,
                MSG_CONVERT_FAILED_FMT,
                ts_name,
                detail,
            )
        if state == "BLOCKED":
            if did_revert:
                return _with_optional_detail(
                    MSG_CONVERT_BLOCKED_REVERTED_BARE_FMT,
                    MSG_CONVERT_BLOCKED_REVERTED_FMT,
                    ts_name,
                    detail,
                )
            return _with_optional_detail(
                MSG_CONVERT_BLOCKED_BARE_FMT,
                MSG_CONVERT_BLOCKED_FMT,
                ts_name,
                detail,
            )
    except (KeyError, IndexError, ValueError):
        return ""
    return ""


def revert_message(reverted):
    """原本に戻すの文言。戻りが一覧のときだけ件数で分ける。"""
    count = _listed_count(reverted)
    if count is None:
        return MSG_REVERT_OK
    if count <= 0:
        return MSG_REVERT_NONE
    try:
        return MSG_REVERT_OK_FMT.format(n=count)
    except (KeyError, IndexError, ValueError):
        return MSG_REVERT_OK


def disable_message(reverted):
    """キルスイッチ OFF の文言。戻したノードの件数があるときだけ件数を付ける。"""
    count = _listed_count(reverted)
    if count is None:
        return MSG_DISABLED_REVERTED
    if count <= 0:
        return MSG_DISABLED_NO_REVERT
    try:
        return MSG_DISABLED_REVERTED_FMT.format(n=count)
    except (KeyError, IndexError, ValueError):
        return MSG_DISABLED_REVERTED


def fold_status_label(enabled, converting):
    """折りたたみボタン横の短い状態。Qt 無し。

    無効が先。有効で変換中のときだけ「変換中」。
    P2-c1 の閉じた欄は ``fold_status_label_p2``(``_current_fold_status``)を使う。
    """
    if not enabled:
        return LABEL_FOLD_OFF
    if converting:
        return LABEL_FOLD_BUSY
    return LABEL_FOLD_ON


def is_reservation_id(job_id):
    """ランタイムの予約 ID(``queued:<連番>``)なら True。"""
    return isinstance(job_id, str) and job_id.startswith("queued:")


def convert_launch_messages(started_ids, pending_names, queued_names, missing=None):
    """今すぐ変換の結果文言。予約だけのときは開始文を付けない。"""
    parts = []
    if started_ids:
        parts.append("変換を開始しました")
    for ts_name in pending_names:
        parts.append(MSG_PEND_FMT.format(ts=ts_name))
    for ts_name in queued_names:
        parts.append(MSG_QUEUE_FMT.format(ts=ts_name))
    if missing:
        parts.append("シーンに無いテクスチャセット: {0}".format("、".join(missing)))
    return "。".join(parts)


def lookup_project_entry(by_project, project_key):
    """known_texture_sets_by_project から、当該プロジェクト分だけを取る。"""
    if not project_key or not isinstance(by_project, dict):
        return []
    if project_key in by_project:
        return by_project[project_key]
    folded = str(project_key).casefold()
    for key, value in by_project.items():
        if str(key).casefold() == folded:
            return value
    return []


def _inform(inform_fn, text):
    if inform_fn is None:
        return
    try:
        inform_fn(text)
    except Exception:
        pass


def handle_record_request(controller, confirm_fn, inform_fn=None):
    """画面側: 記録できる状態を先に見て、確認は準備ができてから出す。"""
    checker = getattr(controller, "check_record_ready", None)
    if callable(checker):
        try:
            ready = checker()
        except Exception:
            ready = {"ok": False, "message": "基準UVの記録に失敗しました"}
        if not isinstance(ready, dict) or not ready.get("ok"):
            text = ""
            if isinstance(ready, dict):
                raw = ready.get("message")
                if isinstance(raw, str) and raw.strip():
                    text = raw
            if not text:
                text = MSG_NO_PROJECT
            _inform(inform_fn, text)
            return {"ok": False, "message": text}
    try:
        exists = bool(controller.has_baseline())
    except Exception:
        exists = False
    kind, text = decide_baseline_prompt(exists)
    if kind == "already":
        _inform(inform_fn, text)
        return {"ok": False, "message": text, "already": True}
    accepted = False
    try:
        accepted = bool(confirm_fn(text))
    except Exception:
        accepted = False
    if not accepted:
        return {"ok": False, "message": "記録を取り消しました", "cancelled": True}
    try:
        return controller.record_baseline()
    except Exception:
        return {"ok": False, "message": "基準UVの記録に失敗しました"}


def handle_rebase_request(controller, confirm_fn, inform_fn=None):
    """画面側: 送れる状態を先に見て、置き換えの確認は準備ができてから 1 回だけ出す。"""
    checker = getattr(controller, "rebase_check_ready", None)
    if not callable(checker):
        return {"ok": False, "message": MSG_REBASE_SEND_FAILED}
    try:
        ready = checker()
    except Exception:
        ready = {"ok": False, "message": MSG_REBASE_SEND_FAILED}
    if not isinstance(ready, dict) or not ready.get("ok"):
        text = ""
        if isinstance(ready, dict):
            raw = ready.get("message")
            if isinstance(raw, str) and raw.strip():
                text = raw
        if not text:
            text = MSG_REBASE_SEND_FAILED
        _inform(inform_fn, text)
        return {"ok": False, "message": text}
    if ready.get("replace"):
        accepted = False
        try:
            accepted = bool(confirm_fn(MSG_REBASE_CONFIRM_REPLACE))
        except Exception:
            accepted = False
        if not accepted:
            return {
                "ok": False,
                "cancelled": True,
                "message": MSG_REBASE_SEND_CANCELLED,
            }
        try:
            return controller.rebase_send(confirmed=True)
        except Exception:
            return {"ok": False, "message": MSG_REBASE_SEND_FAILED}
    try:
        return controller.rebase_send()
    except Exception:
        return {"ok": False, "message": MSG_REBASE_SEND_FAILED}


class UVRemapController:
    """基準記録・変換・原本復帰。どの公開メソッドも例外を外へ出さない。"""

    def __init__(
        self,
        get_config,
        save_config_fn,
        project_key_fn,
        known_ts_fn,
        final_dir_fn,
        final_subfolder_fn,
        list_texture_sets_fn,
        runtime=None,
        history_fn=None,
        uv_fingerprints_fn=None,
        time_fn=None,
        utcnow_fn=None,
        rebase_export_fn=None,
        maya_version_fn=None,
    ):
        self.get_config = get_config
        self.save_config_fn = save_config_fn
        self.project_key_fn = project_key_fn
        self.known_ts_fn = known_ts_fn
        self.final_dir_fn = final_dir_fn
        self.final_subfolder_fn = final_subfolder_fn
        self.list_texture_sets_fn = list_texture_sets_fn
        self.history_fn = history_fn
        self.uv_fingerprints_fn = uv_fingerprints_fn
        self._ts_by_shape = {}
        self._cached_shape_paths = []
        self._uv_last_poll_s = None
        self._uv_prev_fps = None
        self._uv_last_change_at = None
        self._uv_slow = False
        self._uv_slow_noted = False
        self._follow_last_flag_mtime_ns = None
        self._follow_last_change_seen_ns = None
        # 重い段階を最後まで済ませた印。落ち着き待ちでは更新しない。
        self._follow_done_mtime_ns = None
        self._follow_pending_settle = False
        self._follow_pending_settle_mtime_ns = None
        self._follow_pending_settle_s = None
        self._follow_open_pass_done = False
        self._follow_force_heavy = False
        self._follow_enabled_seen = None
        self._auto_input_as_is_emitted = False
        # 2026.10.19(RB-d): 時計と書き出しは注入する。datetime.now() は呼ばない。
        self._time_fn = time_fn if time_fn is not None else time.time
        self._utcnow_fn = utcnow_fn if utcnow_fn is not None else (
            lambda: datetime.now(timezone.utc)
        )
        self._rebase_export_fn = rebase_export_fn
        self._maya_version_fn = maya_version_fn
        self._rebase_blocks_follow = False
        self._rebase_last_tick_s = None
        self._rebase_discarded = set()
        self._rebase_noted = set()
        self._rebase_complete_tries = {}
        self._rebase_exhausted = set()
        self._rebase_guidance = set()
        self._rebase_wait_text = {}
        if runtime is None:
            import uvremap_runtime as _rt
            cfg = self._safe_config()
            mayapy = str(cfg.get("uvremap_mayapy_path") or "").strip() or None
            self.runtime = _rt.UVRemapRuntime(
                get_enabled=self._enabled_from_config,
                get_root=self._root_for_project,
                mayapy_path=mayapy,
                max_parallel_fn=self._max_parallel_from_config,
            )
        else:
            self.runtime = runtime

    def _safe_config(self):
        try:
            cfg = self.get_config()
        except Exception:
            return {}
        return cfg if isinstance(cfg, dict) else {}

    def _enabled_from_config(self):
        return bool(self._safe_config().get("uvremap_enabled", False))

    def _follow_settings_from_config(self):
        """P2-2 の 6 キー。``normalize_follow_settings`` で丸めた値を返す。"""
        return normalize_follow_settings(self._safe_config())

    def _auto_follow_from_config(self):
        return bool(self._follow_settings_from_config()["uvremap_auto_follow"])

    def _auto_follow_uv_from_config(self):
        return self._follow_settings_from_config()["uvremap_auto_follow_uv"] is True

    def _max_parallel_from_config(self):
        return self._follow_settings_from_config()["uvremap_max_parallel"]

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except TypeError:
            if args:
                return fn()
            raise

    def _project_key(self):
        key = self._call(self.project_key_fn)
        if key is None or key == "":
            return None
        return key

    def _root_for_project(self, project_key):
        cfg = self._safe_config()
        root_dir = cfg.get("uvremap_root_dir") or DEFAULT_UVREMAP_ROOT
        sub = self._call(self.final_subfolder_fn, project_key)
        root = uvremap_root_for_project(root_dir, sub)
        if not root:
            raise RuntimeError("uvremap_root が空です")
        return root

    def _root_if_available(self):
        key = self._project_key()
        if not key:
            return None
        try:
            return self._root_for_project(key)
        except Exception:
            return None

    def _resolve_context(self):
        key = self._project_key()
        if not key:
            return {"ok": False, "message": MSG_NO_PROJECT}
        try:
            final_dir = self._call(self.final_dir_fn, key)
        except Exception:
            final_dir = None
        if not final_dir:
            return {"ok": False, "message": MSG_NO_FINAL}
        try:
            sub = self._call(self.final_subfolder_fn, key)
        except Exception:
            sub = None
        cfg = self._safe_config()
        root_dir = cfg.get("uvremap_root_dir") or DEFAULT_UVREMAP_ROOT
        root = uvremap_root_for_project(root_dir, sub)
        if not root:
            return {"ok": False, "message": MSG_NO_FINAL}
        watch_dir = cfg.get("watch_dir")
        final_export_dir = cfg.get("final_export_dir")
        if not work_folder_is_safe(
            root, final_dir=final_dir, watch_dir=watch_dir, final_export_dir=final_export_dir,
        ):
            return {"ok": False, "message": MSG_FOLDER_INSIDE}
        try:
            known = self._call(self.known_ts_fn, key)
        except Exception:
            known = []
        try:
            texture_sets = self._call(self.list_texture_sets_fn)
        except Exception:
            texture_sets = {}
        self._remember_texture_sets(texture_sets, known)
        ts_names, shapes, missing = resolve_targets(known, texture_sets)
        if not ts_names:
            extra = ""
            if missing:
                extra = "（シーンに無い: {0}）".format("、".join(missing))
            return {
                "ok": False,
                "message": MSG_NO_TARGETS + extra,
                "missing": missing,
                "ts_names": [],
                "shape_paths": [],
            }
        return {
            "ok": True,
            "message": "",
            "project_key": key,
            "final_dir": os.path.normpath(str(final_dir)),
            "root": root,
            "ts_names": ts_names,
            "shape_paths": shapes,
            "missing": missing,
        }

    def has_baseline(self):
        try:
            root = self._root_if_available()
            return baseline_exists(root)
        except Exception:
            return False

    def set_enabled(self, enabled):
        try:
            enabled = bool(enabled)
            was_enabled = self._enabled_from_config()
            cfg = self._safe_config()
            if isinstance(cfg, dict):
                cfg["uvremap_enabled"] = enabled
            self.save_config_fn({"uvremap_enabled": enabled})
            if enabled and not was_enabled:
                self._follow_force_heavy = True
            if not enabled:
                message = self._message_after_disable()
            else:
                message = MSG_ENABLED
            return {"ok": True, "message": message, "enabled": enabled}
        except Exception:
            return {"ok": False, "message": "設定の更新に失敗しました"}

    def set_auto_follow(self, enabled):
        """``uvremap_auto_follow`` を部分マージで保存する。初回オンで既知の制約を 1 度出す。"""
        try:
            enabled = bool(enabled)
            was_auto = self._auto_follow_from_config()
            cfg = self._safe_config()
            if isinstance(cfg, dict):
                cfg["uvremap_auto_follow"] = enabled
            self.save_config_fn({"uvremap_auto_follow": enabled})
            if enabled != was_auto:
                self._follow_force_heavy = True
            message = ""
            if enabled and not self._auto_input_as_is_emitted:
                message = MSG_AUTO_INPUT_AS_IS
                self._write_history(message)
                self._auto_input_as_is_emitted = True
            return {"ok": True, "message": message, "auto_follow": enabled}
        except Exception:
            return {"ok": False, "message": "設定の更新に失敗しました"}

    def set_auto_follow_uv(self, enabled):
        """``uvremap_auto_follow_uv`` を部分マージで保存する。"""
        try:
            enabled = bool(enabled)
            was = self._auto_follow_uv_from_config()
            cfg = self._safe_config()
            if isinstance(cfg, dict):
                cfg["uvremap_auto_follow_uv"] = enabled
            self.save_config_fn({"uvremap_auto_follow_uv": enabled})
            if enabled != was:
                self._follow_force_heavy = True
            return {"ok": True, "message": "", "auto_follow_uv": enabled}
        except Exception:
            return {"ok": False, "message": "設定の更新に失敗しました"}

    def _remember_texture_sets(self, texture_sets, known=None):
        """シーン走査の結果をメモリに持つ。``uv_tick`` はここを読む。"""
        self._ts_by_shape = ts_by_shape_from_texture_sets(texture_sets)
        if known is None:
            key = self._project_key()
            if not key:
                known = []
            else:
                try:
                    known = self._call(self.known_ts_fn, key)
                except Exception:
                    known = []
        _ts_names, shapes, _missing = resolve_targets(known, texture_sets)
        self._cached_shape_paths = list(shapes)

    def refresh_shape_map(self):
        """``list_texture_sets`` を 1 回呼び、TS とシェイプの対応を更新する。"""
        try:
            texture_sets = self._call(self.list_texture_sets_fn)
        except Exception:
            texture_sets = {}
        self._remember_texture_sets(texture_sets)
        return self._ts_by_shape

    def note_scene_changed(self):
        """シーン切替。UV の間隔を既定に戻し、シェイプ対応と Painter 判定をやり直す。"""
        self._follow_force_heavy = True
        self._uv_slow = False
        self._uv_slow_noted = False
        self._uv_prev_fps = None
        self._uv_last_change_at = None
        try:
            self.refresh_shape_map()
        except Exception:
            pass

    def _message_after_disable(self):
        try:
            reverted = self.runtime.revert_to_original()
        except Exception:
            return MSG_DISABLED_REVERTED
        return disable_message(reverted)

    def check_record_ready(self):
        """記録の前提(_resolve_context の成否)だけを見る。記録はしない。"""
        try:
            resolved = self._resolve_context()
            if not isinstance(resolved, dict) or not resolved.get("ok"):
                message = ""
                if isinstance(resolved, dict):
                    raw = resolved.get("message")
                    if isinstance(raw, str) and raw.strip():
                        message = raw
                if not message:
                    message = MSG_NO_PROJECT
                return {"ok": False, "message": message}
            return {"ok": True, "message": ""}
        except Exception:
            return {"ok": False, "message": "基準UVの記録に失敗しました"}

    def record_baseline(self):
        try:
            resolved = self._resolve_context()
            if not resolved.get("ok"):
                return {"ok": False, "message": resolved.get("message") or MSG_NO_PROJECT}
            root = resolved["root"]
            if baseline_exists(root):
                return {"ok": False, "message": MSG_BASELINE_EXISTS, "already": True}
            baseline_id = self.runtime.record_baseline(
                resolved["project_key"],
                resolved["ts_names"],
                resolved["shape_paths"],
            )
            return {
                "ok": True,
                "message": "基準UVを記録しました",
                "baseline_id": baseline_id,
            }
        except Exception:
            return {"ok": False, "message": "基準UVの記録に失敗しました"}

    def convert_now(self):
        try:
            if not self._enabled_from_config():
                return {"ok": False, "message": MSG_DISABLED}
            resolved = self._resolve_context()
            if not resolved.get("ok"):
                return {"ok": False, "message": resolved.get("message") or MSG_NO_TARGETS}
            if not baseline_exists(resolved["root"]):
                return {"ok": False, "message": MSG_NO_BASELINE}
            out = output_dir_for_root(resolved["root"])
            job_ids = []
            started = []
            pending = []
            queued = []
            for ts_name in resolved["ts_names"]:
                job_id = self._start_ts(
                    resolved["project_key"],
                    ts_name,
                    resolved["shape_paths"],
                    resolved["final_dir"],
                    out,
                    "manual",
                )
                if not job_id:
                    continue
                job_ids.append(job_id)
                if is_reservation_id(job_id):
                    if self._ts_is_running(ts_name):
                        pending.append(ts_name)
                    else:
                        queued.append(ts_name)
                else:
                    started.append(job_id)
            if not job_ids:
                return {"ok": False, "message": MSG_NOT_STARTED, "job_ids": []}
            message = convert_launch_messages(
                started, pending, queued, missing=resolved.get("missing") or None,
            )
            return {
                "ok": True,
                "message": message,
                "job_ids": job_ids,
                "missing": list(resolved.get("missing") or []),
                "pending": pending,
                "queued": queued,
            }
        except Exception:
            return {"ok": False, "message": "変換の開始に失敗しました"}

    def apply_changes(self):
        """``STALE`` の TS だけを手動の要求として変換する。"""
        try:
            if not self._enabled_from_config():
                return {"ok": False, "message": MSG_DISABLED}
            resolved = self._resolve_context()
            if not resolved.get("ok"):
                return {"ok": False, "message": resolved.get("message") or MSG_NO_TARGETS}
            if not baseline_exists(resolved["root"]):
                return {"ok": False, "message": MSG_NO_BASELINE}
            states = self._status_ts_states(resolved["project_key"])
            names = [
                ts_name for ts_name in resolved["ts_names"]
                if states.get(ts_name) == STATE_STALE
            ]
            out = output_dir_for_root(resolved["root"])
            job_ids = []
            started = []
            pending = []
            queued = []
            for ts_name in names:
                job_id = self._start_ts(
                    resolved["project_key"],
                    ts_name,
                    resolved["shape_paths"],
                    resolved["final_dir"],
                    out,
                    "manual",
                )
                if not job_id:
                    continue
                job_ids.append(job_id)
                if is_reservation_id(job_id):
                    if self._ts_is_running(ts_name):
                        pending.append(ts_name)
                    else:
                        queued.append(ts_name)
                else:
                    started.append(job_id)
            if not job_ids:
                return {"ok": False, "message": MSG_NOT_STARTED, "job_ids": []}
            message = convert_launch_messages(
                started, pending, queued, missing=resolved.get("missing") or None,
            )
            return {
                "ok": True,
                "message": message,
                "job_ids": job_ids,
                "missing": list(resolved.get("missing") or []),
                "pending": pending,
                "queued": queued,
            }
        except Exception:
            return {"ok": False, "message": "変換の開始に失敗しました"}

    def _uv_fp_shapes_for(self, ts_name, fallback):
        found = []
        mapping = self._ts_by_shape if isinstance(self._ts_by_shape, dict) else {}
        for shape, names in mapping.items():
            if isinstance(names, str):
                names = (names,)
            if isinstance(names, (list, tuple)) and ts_name in names:
                found.append(shape)
        if found:
            return sorted(found)
        return list(fallback or [])

    def _start_ts(self, project_key, ts_name, shape_paths, final_dir, output_dir, origin):
        return self.runtime.convert_now(
            project_key,
            ts_name,
            shape_paths,
            source_dir=final_dir,
            output_dir=output_dir,
            quality="final",
            timeout_s=CONVERT_TIMEOUT_S,
            origin=origin,
            uv_fp_shapes=self._uv_fp_shapes_for(ts_name, shape_paths),
        )

    def _ts_is_running(self, ts_name):
        checker = getattr(self.runtime, "has_running_job_for_ts", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(ts_name))
        except Exception:
            return False

    def _follow_job_message(self, ts_name, job_id):
        if not job_id:
            return ""
        if is_reservation_id(job_id):
            if self._ts_is_running(ts_name):
                return MSG_PEND_FMT.format(ts=ts_name)
            return MSG_QUEUE_FMT.format(ts=ts_name)
        return MSG_PAINTER_START_FMT.format(ts=ts_name)

    def _current_scene_id(self):
        getter = getattr(self.runtime, "_scene_id", None)
        if not callable(getter):
            return ""
        try:
            value = getter()
        except Exception:
            return ""
        if value is None:
            return ""
        return str(value)

    def _status_ts_states(self, project_key):
        states = {}
        try:
            path = self.runtime.status_snapshot(project_key)
        except Exception:
            return states
        if path is None:
            return states
        try:
            raw = Path(path).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return states
        table = data.get("ts") if isinstance(data, dict) else None
        if not isinstance(table, dict):
            return states
        for name, entry in table.items():
            if isinstance(entry, dict):
                states[str(name)] = entry.get("state")
        return states

    def _note_flag_mtime(self, flag_mtime_ns, now_ns):
        if self._follow_last_flag_mtime_ns != flag_mtime_ns:
            self._follow_last_change_seen_ns = now_ns
            self._follow_last_flag_mtime_ns = flag_mtime_ns

    def _update_follow_flag_mtime(self, root, flag_mtime_ns):
        data = load_follow(root)
        data["flag_mtime_ns"] = int(flag_mtime_ns)
        save_follow(root, data)

    def _should_record_flag_mtime(self, decision, enabled, auto_follow, has_baseline):
        """Q4: ready が真で条件 1・2 を通った判定のときだけ更新する。"""
        if not getattr(decision, "ready", False):
            return False
        return enabled is True and auto_follow is True and has_baseline is True

    def _probe_follow_flag_mtime(self):
        """軽い段階。プロジェクトのキーと Final フォルダから印の mtime だけ取る。

        シーン走査、follow.json、status.json、``source_signature`` はここではしない。
        印が無い、または場所が取れないときは None。
        """
        key = self._project_key()
        if not key:
            return None
        try:
            final_dir = self._call(self.final_dir_fn, key)
        except Exception:
            final_dir = None
        if not final_dir:
            return None
        flag_path = Path(final_dir) / "_sync_complete.flag"
        try:
            return int(flag_path.stat().st_mtime_ns)
        except (OSError, TypeError, ValueError):
            return None

    def _follow_heavy_due(self, now_ns, flag_mtime_ns):
        """重い段階が必要なときだけ True。

        窓を開いた直後、追従チェックかキルスイッチ ON の直後、
        印の mtime が最後に重い段階を済ませたときと違うとき、
        落ち着き待ちの settle が過ぎたとき。
        """
        if not self._follow_open_pass_done or self._follow_force_heavy:
            return True
        pending_same = (
            self._follow_pending_settle
            and self._follow_pending_settle_mtime_ns == flag_mtime_ns
        )
        if self._follow_done_mtime_ns != flag_mtime_ns and not pending_same:
            return True
        if pending_same and _settled_ns(
            now_ns, self._follow_last_change_seen_ns, self._follow_pending_settle_s,
        ):
            return True
        return False

    def _remember_heavy_flag(self, flag_mtime_ns, waiting, settle_s):
        """重い段階を最後まで済ませた印を覚える。落ち着き待ちでは覚えず、再判定の材料だけ残す。"""
        self._follow_open_pass_done = True
        self._follow_force_heavy = False
        if waiting:
            self._follow_pending_settle = True
            self._follow_pending_settle_mtime_ns = flag_mtime_ns
            self._follow_pending_settle_s = settle_s
            return
        self._follow_pending_settle = False
        self._follow_pending_settle_mtime_ns = None
        self._follow_pending_settle_s = None
        self._follow_done_mtime_ns = flag_mtime_ns

    def follow_tick(self, now_ns):
        """Painter の印を見て、自動変換または入力側 STALE を進める。例外は出さない。

        毎秒はキルスイッチ、プロジェクトのキー、Final フォルダ、印の mtime だけを見る。
        入力側 STALE を含む重い判定は、必要なときだけ行う。
        """
        # 2026.10.19(RB-d): リベース中は Phase 2 の自動追従を止める(R-4.6)。
        if self._rebase_blocks_follow:
            return {"ok": True, "message": ""}
        try:
            return self._follow_tick_impl(now_ns)
        except Exception:
            return {"ok": False, "message": ""}

    def _follow_tick_impl(self, now_ns):
        enabled = bool(self._enabled_from_config())
        if not enabled:
            self._follow_enabled_seen = False
            return {"ok": True, "message": ""}
        if self._follow_enabled_seen is False:
            self._follow_force_heavy = True
        self._follow_enabled_seen = True
        flag_mtime_ns = self._probe_follow_flag_mtime()
        if flag_mtime_ns is None:
            return {"ok": True, "message": ""}
        if not self._follow_heavy_due(now_ns, flag_mtime_ns):
            return {"ok": True, "message": ""}
        return self._follow_tick_heavy(now_ns, flag_mtime_ns)

    def _follow_tick_heavy(self, now_ns, flag_mtime_ns):
        resolved = self._resolve_context()
        if not isinstance(resolved, dict) or not resolved.get("ok"):
            self._remember_heavy_flag(flag_mtime_ns, False, None)
            return {"ok": True, "message": ""}
        final_dir = resolved.get("final_dir")
        root = resolved.get("root")
        project_key = resolved.get("project_key")
        if not final_dir or not root or not project_key:
            self._remember_heavy_flag(flag_mtime_ns, False, None)
            return {"ok": True, "message": ""}
        self._note_flag_mtime(flag_mtime_ns, now_ns)
        settings = self._follow_settings_from_config()
        auto_follow = settings["uvremap_auto_follow"] is True
        has_baseline = bool(baseline_exists(root))
        follow_data = load_follow(root)
        last_flag_ns = follow_data.get("flag_mtime_ns")
        recorded_ts = follow_data.get("ts") if isinstance(follow_data.get("ts"), dict) else {}
        ts_states = self._status_ts_states(project_key)
        channels = [dict(item) for item in br.DEFAULT_CHANNELS]
        current_scene = self._current_scene_id()
        current_sigs = {}
        recorded_sigs = {}
        same_scene = {}
        names = list(ts_states) or list(resolved.get("ts_names") or [])
        for ts_name in names:
            current_sigs[ts_name] = source_signature(final_dir, ts_name, channels)
            rec = recorded_ts.get(ts_name) if isinstance(recorded_ts.get(ts_name), dict) else {}
            recorded_sigs[ts_name] = rec.get("source_sig")
            rec_scene = rec.get("scene_id")
            same_scene[ts_name] = rec_scene is not None and str(rec_scene) == current_scene
        if not auto_follow:
            marker = getattr(self.runtime, "mark_stale", None)
            if callable(marker):
                for ts_name, state in ts_states.items():
                    if input_stale(state, current_sigs.get(ts_name), recorded_sigs.get(ts_name), False):
                        try:
                            marker(project_key, ts_name, MSG_INPUT_STALE)
                        except Exception:
                            pass
            self._remember_heavy_flag(flag_mtime_ns, False, None)
            return {"ok": True, "message": ""}
        decision = decide_follow(
            now_ns,
            flag_mtime_ns,
            last_flag_ns,
            self._follow_last_change_seen_ns,
            settings["uvremap_follow_settle_s"],
            True,
            True,
            has_baseline,
            ts_states,
            current_sigs,
            recorded_sigs,
            same_scene,
        )
        out = output_dir_for_root(root)
        shape_paths = list(resolved.get("shape_paths") or [])
        messages = []
        for ts_name in list(decision.ts_to_convert) + list(decision.ts_to_pend):
            try:
                job_id = self._start_ts(
                    project_key, ts_name, shape_paths, final_dir, out, "auto",
                )
            except Exception:
                job_id = ""
            text = self._follow_job_message(ts_name, job_id)
            if text:
                messages.append(text)
                self._write_history(text)
        if self._should_record_flag_mtime(decision, True, True, has_baseline):
            try:
                self._update_follow_flag_mtime(root, flag_mtime_ns)
            except Exception:
                pass
        waiting = decision.ready is False and _flag_is_newer(flag_mtime_ns, last_flag_ns)
        self._remember_heavy_flag(
            flag_mtime_ns, waiting, settings["uvremap_follow_settle_s"],
        )
        return {"ok": True, "message": messages[-1] if messages else ""}

    def _uv_poll_interval_s(self):
        if self._uv_slow:
            return UV_SLOW_POLL_S
        return float(self._follow_settings_from_config()["uvremap_uv_poll_s"])

    def _as_poll_seconds(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number

    def _watch_states(self, project_key):
        states = self._status_ts_states(project_key)
        watched = {
            name: state for name, state in states.items()
            if state in (STATE_CONVERTED_OK, STATE_STALE)
        }
        return states, watched

    def _shapes_for_watched(self, states):
        shapes = []
        mapping = self._ts_by_shape if isinstance(self._ts_by_shape, dict) else {}
        for shape, names in mapping.items():
            if isinstance(names, str):
                names = (names,)
            if not isinstance(names, (list, tuple)):
                continue
            for ts_name in names:
                if states.get(ts_name) in (STATE_CONVERTED_OK, STATE_STALE):
                    shapes.append(shape)
                    break
        return sorted(set(shapes))

    def _recorded_uv_by_ts(self):
        root = self._root_if_available()
        if not root:
            return {}
        try:
            data = load_follow(root)
        except Exception:
            return {}
        table = data.get("ts") if isinstance(data, dict) else None
        if not isinstance(table, dict):
            return {}
        recorded = {}
        for name, row in table.items():
            if not isinstance(name, str) or not isinstance(row, dict):
                continue
            uv_fp = row.get("uv_fp")
            if isinstance(uv_fp, dict):
                recorded[name] = uv_fp
        return recorded

    def _take_uv_fingerprints(self, shape_paths):
        fn = self.uv_fingerprints_fn
        if fn is None:
            import uvremap_maya
            fn = uvremap_maya.uv_fingerprints
        return fn(list(shape_paths))

    def _uv_launch_context(self):
        """指紋取得のあと、自動変換に使う場所。シーン走査はしない。"""
        key = self._project_key()
        if not key:
            return None
        try:
            final_dir = self._call(self.final_dir_fn, key)
        except Exception:
            return None
        if not final_dir:
            return None
        try:
            root = self._root_for_project(key)
        except Exception:
            return None
        cfg = self._safe_config()
        if not work_folder_is_safe(
            root,
            final_dir=final_dir,
            watch_dir=cfg.get("watch_dir"),
            final_export_dir=cfg.get("final_export_dir"),
        ):
            return None
        if not baseline_exists(root):
            return None
        paths = list(self._cached_shape_paths or [])
        if not paths:
            paths = sorted(self._ts_by_shape) if isinstance(self._ts_by_shape, dict) else []
        return {
            "project_key": key,
            "final_dir": os.path.normpath(str(final_dir)),
            "root": root,
            "shape_paths": paths,
        }

    def _uv_job_message(self, ts_name, job_id):
        if not job_id:
            return ""
        if is_reservation_id(job_id):
            if self._ts_is_running(ts_name):
                return MSG_PEND_FMT.format(ts=ts_name)
            return MSG_QUEUE_FMT.format(ts=ts_name)
        return MSG_UV_START_FMT.format(ts=ts_name)

    def uv_tick(self, now_s):
        """UV 指紋を見て STALE と、任意の自動変換を進める。例外は出さない。"""
        # 2026.10.19(RB-d): リベース中は UV の自動追従も止める(R-4.6)。手動の変換は止めない。
        if self._rebase_blocks_follow:
            return {"ok": True, "message": ""}
        try:
            return self._uv_tick_impl(now_s)
        except Exception:
            return {"ok": False, "message": ""}

    def _uv_tick_impl(self, now_s):
        if not self._enabled_from_config():
            return {"ok": True, "message": ""}
        now = self._as_poll_seconds(now_s)
        if now is None:
            return {"ok": True, "message": ""}
        interval = self._uv_poll_interval_s()
        if self._uv_last_poll_s is None:
            self._uv_last_poll_s = now
            return {"ok": True, "message": ""}
        if (now - self._uv_last_poll_s) < interval:
            return {"ok": True, "message": ""}
        project_key = self._project_key()
        if not project_key:
            self._uv_last_poll_s = now
            return {"ok": True, "message": ""}
        states, watched = self._watch_states(project_key)
        if not watched:
            self._uv_last_poll_s = now
            return {"ok": True, "message": ""}
        shapes = self._shapes_for_watched(states)
        if not shapes:
            self._uv_last_poll_s = now
            return {"ok": True, "message": ""}
        self._uv_last_poll_s = now
        result = self._take_uv_fingerprints(shapes)
        fingerprints, elapsed = _split_fingerprint_result(result)
        if not isinstance(fingerprints, dict):
            return {"ok": True, "message": ""}
        slow_message = ""
        if elapsed > UV_SLOW_LIMIT_S:
            self._uv_slow = True
            if not self._uv_slow_noted:
                self._uv_slow_noted = True
                slow_message = MSG_UV_SLOW
                self._write_history(slow_message)
        recorded = self._recorded_uv_by_ts()
        if self._uv_prev_fps is None:
            changed = uv_recorded_differs(fingerprints, recorded)
        else:
            changed = fingerprint_maps_differ(fingerprints, self._uv_prev_fps)
        if changed:
            self._uv_last_change_at = now
        self._uv_prev_fps = dict(fingerprints)
        settings = self._follow_settings_from_config()
        decision = decide_uv_follow(
            now,
            fingerprints,
            recorded,
            self._uv_last_change_at,
            settings["uvremap_uv_settle_s"],
            self._ts_by_shape,
            states,
            True,
            settings["uvremap_auto_follow_uv"] is True,
        )
        marker = getattr(self.runtime, "mark_stale", None)
        if callable(marker):
            for ts_name in decision.ts_to_stale:
                try:
                    marker(project_key, ts_name, MSG_UV_STALE)
                except Exception:
                    pass
        messages = []
        if decision.ts_to_convert:
            ctx = self._uv_launch_context()
            if ctx is not None:
                out = output_dir_for_root(ctx["root"])
                for ts_name in decision.ts_to_convert:
                    try:
                        job_id = self._start_ts(
                            ctx["project_key"],
                            ts_name,
                            ctx["shape_paths"],
                            ctx["final_dir"],
                            out,
                            "auto",
                        )
                    except Exception:
                        job_id = ""
                    text = self._uv_job_message(ts_name, job_id)
                    if text:
                        messages.append(text)
                        self._write_history(text)
        text = messages[-1] if messages else slow_message
        return {"ok": True, "message": text}

    def revert(self):
        try:
            nodes = self.runtime.revert_to_original()
        except Exception:
            return {"ok": False, "message": MSG_REVERT_FAIL}
        if isinstance(nodes, (list, tuple)):
            reverted = list(nodes)
        else:
            reverted = []
        return {
            "ok": True,
            "message": revert_message(nodes),
            "reverted": reverted,
        }

    def recover_orphaned(self):
        """status.json に残った孤立 CONVERTING を ORIGINAL へ戻す。例外は出さない。"""
        try:
            key = self._project_key()
            if not key:
                return []
            names = self.runtime.recover_orphaned(key)
            recovered = [str(n) for n in list(names or []) if n]
            if recovered:
                self._write_history(MSG_INTERRUPTED)
            return recovered
        except Exception:
            return []

    def poll(self):
        try:
            events = list(self.runtime.poll() or [])
            shown = []
            for event in events:
                text = self._consume_poll_event(event)
                if text:
                    shown.append(text)
            message = shown[-1] if shown else ""
            return {"ok": True, "message": message, "events": events}
        except Exception:
            return {"ok": False, "message": "状態の確認に失敗しました", "events": []}

    def _consume_poll_event(self, event):
        """1 イベントを履歴に残し、メッセージ欄に出す文言を返す。空なら表示を変えない。"""
        warn = warning_message(event)
        done = completion_message(event)
        state = event.get("state") if isinstance(event, dict) else None
        if state == "CONVERTED_OK" and warn and done:
            self._write_history(done)
            self._write_history(warn)
            return warn
        text = warn or done
        if text:
            self._write_history(text)
        return text or ""

    def status_path(self):
        try:
            key = self._project_key()
            if not key:
                return {"ok": False, "message": MSG_NO_PROJECT, "path": None}
            path = self.runtime.status_snapshot(key)
            if path is None:
                return {"ok": False, "message": "状態ファイルの場所を取得できません", "path": None}
            return {"ok": True, "message": "", "path": str(path)}
        except Exception:
            return {"ok": False, "message": "状態ファイルの場所を取得できません", "path": None}

    def _write_history(self, text):
        if self.history_fn is None or not text:
            return
        try:
            self.history_fn("uvremap", text)
        except Exception:
            pass

    # --- 2026.10.19(RB-d): リベースの送信・取り消し・poll 相乗り ----------------

    def rebase_check_ready(self):
        """送る前提だけを見る。依頼も候補も作らない。"""
        try:
            return self._rebase_check_ready_impl()
        except Exception:
            return {"ok": False, "message": MSG_REBASE_SEND_FAILED}

    def rebase_send(self, confirm_fn=None, confirmed=None):
        """新基準候補、メッシュ、依頼の順に作る。例外は外へ出さない。"""
        try:
            return self._rebase_send_impl(confirm_fn, confirmed)
        except Exception:
            return {"ok": False, "message": MSG_REBASE_SEND_FAILED}

    def rebase_cancel(self):
        """この Maya が記録した最新の依頼を、取り消せるときだけ取り消す。"""
        try:
            return self._rebase_cancel_impl()
        except Exception:
            return {"ok": False, "message": MSG_REBASE_SEND_FAILED}

    def rebase_tick(self, now_s):
        """状態ファイルを読み、done なら完了処理、終端なら候補を破棄する。

        ``uvremap_rebase_poll_s`` 秒に 1 回だけ実処理をする。最初の呼び出しは待たない。
        """
        try:
            return self._rebase_tick_impl(now_s)
        except Exception:
            return {"ok": True, "message": ""}

    def rebase_view(self):
        """画面が描く現在の依頼。依頼が無ければ text は空。"""
        try:
            return self._rebase_view_impl()
        except Exception:
            return {
                "text": "",
                "phase": "",
                "can_cancel": False,
                "can_send": False,
                "request_id": None,
                "terminal": False,
            }

    def _rebase_check_ready_impl(self):
        if not self._enabled_from_config():
            return {"ok": False, "message": MSG_REBASE_NEED_KILLSWITCH}
        resolved = self._resolve_context()
        if not isinstance(resolved, dict) or not resolved.get("ok"):
            message = ""
            if isinstance(resolved, dict):
                raw = resolved.get("message")
                if isinstance(raw, str) and raw.strip():
                    message = raw
            return {"ok": False, "message": message or MSG_NO_PROJECT}
        scene_id = self._current_scene_id()
        if not isinstance(scene_id, str) or not scene_id.strip():
            return {"ok": False, "message": MSG_REBASE_NEED_SAVED_SCENE}
        root = resolved["root"]
        active_id = self._read_active_baseline_id(root)
        if not active_id:
            return {"ok": False, "message": MSG_REBASE_NEED_BASELINE}
        final_dir = resolved.get("final_dir")
        try:
            sync = rb.read_sync_result(final_dir)
        except Exception:
            sync = None
        if sync is None:
            return {"ok": False, "message": MSG_REBASE_PLUGIN_UNKNOWN}
        if not rb.plugin_supports_rebase(sync):
            return {"ok": False, "message": rb.MSG_PLUGIN_UPDATE_REQUIRED}
        now = self._time_fn()
        try:
            open_requests = rb.find_open_requests(root, resolved["project_key"], now)
        except Exception:
            open_requests = []
        open_ids = []
        for request in open_requests:
            status_read = rb.read_status(root, request["request_id"])
            cancel_requested = rb.cancel_exists(root, request["request_id"])
            display = rb.decide_display(
                request, status_read, now=now, cancel_requested=cancel_requested,
            )
            if display.phase == "locked":
                return {"ok": False, "message": MSG_REBASE_LOCKED_NO_REPLACE}
            open_ids.append(request["request_id"])
        return {
            "ok": True,
            "message": "",
            "replace": bool(open_ids),
            "open_ids": open_ids,
            "scene_id": scene_id,
            "resolved": resolved,
            "active_id": active_id,
        }

    def _rebase_send_impl(self, confirm_fn, confirmed):
        ready = self._rebase_check_ready_impl()
        if not ready.get("ok"):
            return {
                "ok": False,
                "message": ready.get("message") or MSG_REBASE_SEND_FAILED,
            }
        if ready.get("replace"):
            if confirmed is True:
                accepted = True
            elif confirmed is False:
                accepted = False
            else:
                accepted = False
                if callable(confirm_fn):
                    try:
                        accepted = bool(confirm_fn(MSG_REBASE_CONFIRM_REPLACE))
                    except Exception:
                        accepted = False
            if not accepted:
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": MSG_REBASE_SEND_CANCELLED,
                }
        return self._rebase_send_after_ready(ready)

    def _rebase_send_after_ready(self, ready):
        resolved = ready["resolved"]
        root = resolved["root"]
        project_key = resolved["project_key"]
        scene_id = ready["scene_id"]
        ts_names = list(resolved["ts_names"])
        shape_paths = list(resolved["shape_paths"])
        settings = rb.normalize_rebase_settings(self._safe_config())
        try:
            rb.cleanup_rebase_files(root, self._time_fn())
        except Exception:
            pass
        request_id = self._rebase_new_request_id(root)
        if not request_id:
            return {"ok": False, "message": MSG_REBASE_SEND_FAILED}
        try:
            baseline_id = self.runtime.record_baseline_candidate(
                project_key, ts_names, shape_paths,
            )
        except Exception:
            return {"ok": False, "message": MSG_REBASE_SEND_FAILED}
        meshes_dir = Path(root) / rb.MESHES_DIRNAME
        active_snap = self._load_baseline_snapshot(root, ready.get("active_id"))
        candidate_snap = self._load_baseline_snapshot(root, baseline_id)
        try:
            mesh, uvold = self._call_rebase_export(
                shape_paths,
                meshes_dir,
                baseline_id,
                triangulate=bool(settings.get("uvremap_rebase_triangulate")),
                baseline_snapshot=active_snap,
                candidate_snapshot=candidate_snap,
            )
        except Exception as exc:
            self._discard_candidate(project_key, baseline_id)
            return {"ok": False, "message": self._export_fail_message(exc)}
        try:
            self.runtime.register_rebase(
                project_key,
                request_id,
                baseline_id,
                ts_names,
                scene_id=scene_id,
            )
        except Exception:
            self._discard_candidate(project_key, baseline_id)
            self._unlink_mesh(mesh)
            return {"ok": False, "message": MSG_REBASE_SEND_FAILED}
        try:
            request = rb.build_request(
                request_id=request_id,
                project_key=project_key,
                created_at=self._time_fn(),
                ttl_s=settings["uvremap_rebase_ttl_s"],
                mesh=mesh,
                baseline_id=baseline_id,
                ts_names=ts_names,
                final_full=True,
                maya_version=self._rebase_maya_version(),
                tool_version=None,
            )
            rb.write_request(root, request)
        except Exception:
            self._discard_candidate(project_key, baseline_id)
            self._unlink_mesh(mesh)
            return {"ok": False, "message": MSG_REBASE_SEND_FAILED}
        for old_id in ready.get("open_ids") or []:
            if old_id == request_id:
                continue
            try:
                rb.write_cancel(root, old_id)
            except Exception:
                pass
        history_line = "{0} {1}".format(
            MSG_REBASE_SENT, self._uvold_history_line(uvold),
        ).strip()
        self._write_history(history_line)
        cleanup_line = self._uvold_cleanup_line(uvold)
        message = MSG_REBASE_SENT
        if cleanup_line:
            self._write_history(cleanup_line)
            message = cleanup_line
        self._rebase_blocks_follow = True
        return {
            "ok": True,
            "message": message,
            "request_id": request_id,
            "baseline_id": baseline_id,
            "uvold": uvold,
        }

    def _rebase_cancel_impl(self):
        located = self._rebase_locate()
        if located is None:
            return {"ok": False, "message": MSG_NO_PROJECT}
        project_key, root, _final_dir = located
        now = self._time_fn()
        rows = self._rebase_recorded_requests(root, project_key)
        current = self._rebase_latest(rows)
        if current is None:
            return {"ok": False, "message": ""}
        request, _record = current
        rid = request["request_id"]
        status_read = rb.read_status(root, rid)
        cancel_requested = rb.cancel_exists(root, rid)
        display = rb.decide_display(
            request, status_read, now=now, cancel_requested=cancel_requested,
        )
        if not display.can_cancel:
            if display.phase == "locked":
                return {"ok": False, "message": rb.MSG_CANNOT_CANCEL}
            return {"ok": False, "message": display.text or ""}
        rb.write_cancel(root, rid)
        return {"ok": True, "message": MSG_REBASE_CANCEL_SENT}

    def _rebase_tick_impl(self, now_s):
        due, now = self._rebase_due(now_s)
        if not due:
            return {"ok": True, "message": ""}
        located = self._rebase_locate()
        if located is None:
            self._rebase_blocks_follow = False
            return {"ok": True, "message": ""}
        project_key, root, final_dir = located
        rows = self._rebase_recorded_requests(root, project_key)
        for request, record in rows:
            self._rebase_tick_one(
                project_key, root, final_dir, request, record, now,
            )
        # complete_rebase が completed を立てたあとの記録で、追従停止を更新する。
        rows = self._rebase_recorded_requests(root, project_key)
        self._rebase_update_block(root, rows, now)
        current = self._rebase_latest(rows)
        if current is None:
            return {"ok": True, "message": ""}
        view = self._rebase_present(root, final_dir, current[0], current[1], now)
        return {"ok": True, "message": view.get("text") or ""}

    def _rebase_view_impl(self):
        enabled = bool(self._enabled_from_config())
        view = {
            "text": "",
            "phase": "",
            "can_cancel": False,
            "can_send": enabled,
            "request_id": None,
            "terminal": False,
        }
        located = self._rebase_locate()
        if located is None:
            return view
        project_key, root, final_dir = located
        now = self._time_fn()
        rows = self._rebase_recorded_requests(root, project_key)
        current = self._rebase_latest(rows)
        if current is None:
            return view
        presented = self._rebase_present(
            root, final_dir, current[0], current[1], now,
        )
        presented["can_send"] = enabled and presented.get("phase") != "locked"
        return presented

    def _rebase_due(self, now_s):
        now = self._time_fn() if now_s is None else now_s
        try:
            now = float(now)
        except (TypeError, ValueError):
            return False, None
        if now != now or now in (float("inf"), float("-inf")):
            return False, None
        interval = float(
            rb.normalize_rebase_settings(self._safe_config())["uvremap_rebase_poll_s"]
        )
        last = self._rebase_last_tick_s
        if last is not None and (now - float(last)) < interval:
            return False, now
        self._rebase_last_tick_s = now
        return True, now

    def _rebase_locate(self):
        key = self._project_key()
        if not key:
            return None
        try:
            root = self._root_for_project(key)
        except Exception:
            return None
        final_dir = None
        try:
            final_dir = self._call(self.final_dir_fn, key)
        except Exception:
            final_dir = None
        return key, root, final_dir

    def _rebase_recorded_requests(self, root, project_key):
        try:
            requests = rb.list_requests(root)
        except Exception:
            return []
        rows = []
        for request in requests:
            if not isinstance(request, dict):
                continue
            if request.get("project_key") != project_key:
                continue
            rid = request.get("request_id")
            try:
                record = self.runtime.rebase_record(project_key, rid)
            except Exception:
                record = None
            if not isinstance(record, dict):
                continue
            rows.append((request, record))
        rows.sort(key=lambda item: (
            float(item[0].get("created_at") or 0),
            str(item[0].get("request_id") or ""),
        ))
        return rows

    def _rebase_latest(self, rows):
        if not rows:
            return None
        return rows[-1]

    def _rebase_state(self, status_read):
        if getattr(status_read, "kind", None) != "ok":
            return None
        status = getattr(status_read, "status", None)
        if not isinstance(status, dict):
            return None
        state = status.get("state")
        return state if isinstance(state, str) else None

    def _rebase_tick_one(self, project_key, root, final_dir, request, record, now):
        if record.get("completed"):
            return
        rid = request.get("request_id")
        status_read = rb.read_status(root, rid)
        state = self._rebase_state(status_read)
        if state == "done":
            self._rebase_tick_done(
                project_key, root, final_dir, request, record, status_read,
            )
            return
        cancel_requested = rb.cancel_exists(root, rid)
        display = rb.decide_display(
            request, status_read, now=now, cancel_requested=cancel_requested,
        )
        if display.terminal:
            self._rebase_discard_once(project_key, rid, request.get("baseline_id"))
            self._rebase_note_once((rid, "terminal"), display.text)

    def _rebase_tick_done(self, project_key, root, final_dir, request, record, status_read):
        rid = request.get("request_id")
        if rid in self._rebase_exhausted:
            return
        decision = self._rebase_judge(final_dir, request, record, status_read)
        reason = getattr(decision, "reason", "")
        if getattr(decision, "action", "") == "complete":
            self._rebase_run_complete(project_key, rid)
            return
        if reason in _REBASE_UNCONFIRMED:
            self._rebase_note_once((rid, "unconfirmed"), rb.MSG_RESULT_UNCONFIRMED)

    def _rebase_run_complete(self, project_key, rid):
        tries = int(self._rebase_complete_tries.get(rid, 0))
        if tries >= REBASE_COMPLETE_RETRY_MAX:
            self._rebase_mark_exhausted(rid)
            return
        kwargs = {}
        if self.history_fn is not None:
            kwargs["history_fn"] = self.history_fn
        try:
            result = self.runtime.complete_rebase(project_key, rid, **kwargs)
        except Exception:
            result = {"status": "partial", "reason": "", "failed_step": "不明"}
        if not isinstance(result, dict):
            result = {"status": "partial", "reason": "", "failed_step": "不明"}
        status = result.get("status")
        reason = result.get("reason") or ""
        if status == "completed":
            self._rebase_guidance.add(rid)
            self._rebase_wait_text.pop(rid, None)
            self._rebase_note_once((rid, "guidance"), rb.MSG_DONE_GUIDANCE)
            return
        if status == "already_completed" or (
            status == "skipped" and reason == "no_local_record"
        ):
            self._rebase_wait_text.pop(rid, None)
            return
        if status == "partial" or (status == "skipped" and reason == "job_running"):
            tries += 1
            self._rebase_complete_tries[rid] = tries
            if tries >= REBASE_COMPLETE_RETRY_MAX:
                self._rebase_mark_exhausted(rid)
                return
            if status == "partial":
                step = result.get("failed_step") or "不明"
                self._rebase_wait_text[rid] = MSG_REBASE_PARTIAL_FMT.format(step=step)
            else:
                self._rebase_wait_text[rid] = MSG_REBASE_DONE_SKIPPED_FMT["job_running"]
            return
        if reason in MSG_REBASE_DONE_SKIPPED_FMT:
            self._rebase_wait_text[rid] = MSG_REBASE_DONE_SKIPPED_FMT[reason]

    def _rebase_mark_exhausted(self, rid):
        self._rebase_exhausted.add(rid)
        self._rebase_wait_text.pop(rid, None)
        self._rebase_note_once((rid, "exhausted"), MSG_REBASE_RETRY_EXHAUSTED)

    def _rebase_judge(self, final_dir, request, record, status_read):
        sync = None
        if final_dir:
            try:
                sync = rb.read_sync_result(final_dir)
            except Exception:
                sync = None
        try:
            scene_now = self._current_scene_id()
        except Exception:
            scene_now = ""
        return rb.judge_completion(
            status_read=status_read,
            sync_result=sync,
            request=request,
            enabled=self._enabled_from_config(),
            scene_id_now=scene_now,
            scene_id_request=record.get("scene_id"),
            already_completed=bool(record.get("completed")),
        )

    def _rebase_present(self, root, final_dir, request, record, now):
        rid = request.get("request_id")
        try:
            status_read = rb.read_status(root, rid)
            cancel_requested = rb.cancel_exists(root, rid)
            display = rb.decide_display(
                request, status_read, now=now, cancel_requested=cancel_requested,
            )
        except Exception:
            return {
                "text": "",
                "phase": "",
                "can_cancel": False,
                "terminal": False,
                "request_id": rid,
            }
        text = display.text
        phase = display.phase
        can_cancel = display.can_cancel
        terminal = display.terminal
        state = self._rebase_state(status_read)
        if record.get("completed"):
            if rid in self._rebase_guidance:
                text = rb.MSG_DONE_GUIDANCE
            else:
                text = rb.MSG_DONE
            phase = "done"
            can_cancel = False
            terminal = True
        elif state == "done":
            if rid in self._rebase_exhausted:
                text = MSG_REBASE_RETRY_EXHAUSTED
                phase = "failed"
                can_cancel = False
                terminal = False
            else:
                decision = self._rebase_judge(
                    final_dir, request, record, status_read,
                )
                reason = getattr(decision, "reason", "")
                if reason in _REBASE_UNCONFIRMED:
                    text = rb.MSG_RESULT_UNCONFIRMED
                    phase = "failed"
                    can_cancel = False
                    terminal = False
                elif reason in _REBASE_SKIP_WAIT:
                    text = MSG_REBASE_DONE_SKIPPED_FMT.get(reason, text)
                    phase = "working"
                    can_cancel = False
                    terminal = False
                elif rid in self._rebase_guidance:
                    text = rb.MSG_DONE_GUIDANCE
                    phase = "done"
                    can_cancel = False
                    terminal = True
                elif rid in self._rebase_wait_text:
                    text = self._rebase_wait_text[rid]
                    phase = "working"
                    can_cancel = False
                    terminal = False
        return {
            "text": text,
            "phase": phase,
            "can_cancel": bool(can_cancel),
            "terminal": bool(terminal),
            "request_id": rid,
        }

    def _rebase_update_block(self, root, rows, now):
        blocks = False
        for request, record in rows:
            if record.get("completed"):
                continue
            rid = request.get("request_id")
            try:
                status_read = rb.read_status(root, rid)
            except Exception:
                blocks = True
                continue
            if self._rebase_state(status_read) == "done":
                blocks = True
                continue
            try:
                cancel_requested = rb.cancel_exists(root, rid)
                display = rb.decide_display(
                    request, status_read, now=now, cancel_requested=cancel_requested,
                )
            except Exception:
                blocks = True
                continue
            if not display.terminal:
                blocks = True
        self._rebase_blocks_follow = blocks

    def _rebase_discard_once(self, project_key, rid, baseline_id):
        if rid in self._rebase_discarded:
            return
        self._rebase_discarded.add(rid)
        self._discard_candidate(project_key, baseline_id)

    def _rebase_note_once(self, key, text):
        if key in self._rebase_noted or not text:
            return
        self._rebase_noted.add(key)
        self._write_history(text)

    def _rebase_new_request_id(self, root):
        for _attempt in range(REBASE_REQUEST_ID_ATTEMPTS):
            try:
                request_id = rb.generate_request_id(self._utcnow_fn())
            except Exception:
                return None
            try:
                if rb.request_path(root, request_id).exists():
                    continue
            except Exception:
                continue
            return request_id
        return None

    def _read_active_baseline_id(self, root):
        # 2026.10.19(RB-d): 公開の読み出しが無いので RB-b の内部関数を使う。
        # QUESTIONS_rb_d.md。active.json が指す基準が解決できるときだけ送る。
        try:
            import uvremap_runtime as runtime_mod
            return runtime_mod._read_active_baseline(Path(root))
        except Exception:
            return None

    def _load_baseline_snapshot(self, root, baseline_id):
        if not baseline_id:
            return None
        try:
            import uvremap_core
            return uvremap_core.load_snapshot(Path(root) / "baseline" / str(baseline_id))
        except Exception:
            return None

    def _call_rebase_export(self, shape_paths, meshes_dir, baseline_id, **kwargs):
        fn = self._rebase_export_fn
        if fn is None:
            def fn(paths, directory, bid, **kw):
                import uvremap_maya as um
                return um.export_rebase_mesh_with_uvold(
                    paths, directory, bid, **kw,
                )
        result = fn(shape_paths, meshes_dir, baseline_id, **kwargs)
        if not isinstance(result, tuple) or len(result) != 2:
            raise RuntimeError("export")
        mesh, uvold = result
        if not isinstance(mesh, dict) or not isinstance(uvold, dict):
            raise RuntimeError("export")
        return mesh, uvold

    def _rebase_maya_version(self):
        fn = self._maya_version_fn
        if fn is None:
            try:
                import maya.cmds as cmds
                value = cmds.about(version=True)
            except Exception:
                return None
        else:
            try:
                value = fn()
            except Exception:
                return None
        if isinstance(value, str) and value.strip() and not _text_has_path(value):
            return value
        return None

    def _discard_candidate(self, project_key, baseline_id):
        try:
            self.runtime.discard_baseline_candidate(project_key, baseline_id)
        except Exception:
            pass

    def _unlink_mesh(self, mesh):
        path = mesh.get("path") if isinstance(mesh, dict) else None
        if not isinstance(path, str) or not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass

    def _export_fail_message(self, exc):
        detail = ""
        try:
            import uvremap_maya as um
            if isinstance(exc, um.RebaseExportError):
                detail = str(exc).strip()
        except Exception:
            detail = ""
        if not detail or _text_has_path(detail):
            detail = "メッシュの書き出しに失敗しました"
        return MSG_REBASE_EXPORT_FAILED_FMT.format(message=detail)

    def _uvold_history_line(self, uvold):
        import uvremap_maya as um
        if isinstance(uvold, dict) and uvold.get("applied"):
            return MSG_UVOLD_ATTACHED_FMT.format(name=uvold.get("name") or "uvOld")
        warnings = uvold.get("warnings") if isinstance(uvold, dict) else []
        return MSG_UVOLD_SKIPPED_FMT.format(reason=um.uvold_reason_text(warnings))

    def _uvold_cleanup_line(self, uvold):
        if not isinstance(uvold, dict):
            return ""
        warnings = uvold.get("warnings") or []
        if "uvold_cleanup_failed" not in warnings:
            return ""
        shapes = uvold.get("shapes") or []
        if not isinstance(shapes, (list, tuple)):
            shapes = []
        shown = []
        for name in shapes:
            if isinstance(name, str) and name and not _text_has_path(name):
                shown.append(name)
        return MSG_UVOLD_CLEANUP_FAILED_FMT.format(
            name=uvold.get("name") or "uvOld",
            shapes="、".join(shown),
        )


def _text_has_path(text) -> bool:
    """利用者向けの文に絶対パスが混ざっていないか。"""
    if not isinstance(text, str):
        return True
    if "\\" in text or text.startswith("/") or ":/" in text or ":\\" in text:
        return True
    return False


def _load_qt():
    try:
        from PySide2 import QtCore, QtWidgets
    except ImportError:
        from PySide6 import QtCore, QtWidgets
    return QtCore, QtWidgets


class UVRemapAttachment:
    """attach() が返す、shutdown / on_scene_changed を持つオブジェクト。"""

    def __init__(self, timer, controller, panel=None, message_label=None, group=None,
                 fold_refresh=None):
        self._timer = timer
        self.controller = controller
        self.panel = panel
        self.message_label = message_label
        self.group = group
        self._fold_refresh = fold_refresh

    def timer_is_active(self):
        timer = self._timer
        if timer is None:
            return False
        try:
            return bool(timer.isActive())
        except Exception:
            return False

    def set_message(self, text):
        label = self.message_label
        if label is None:
            return
        try:
            label.setText("" if text is None else str(text))
        except Exception:
            pass

    def _refresh_panel(self):
        panel = self.panel
        if panel is not None:
            try:
                panel.refresh()
            except Exception:
                pass
        self.refresh_fold_status()

    def refresh_fold_status(self):
        fn = self._fold_refresh
        if not callable(fn):
            return
        try:
            fn()
        except Exception:
            pass

    def shutdown(self):
        timer = self._timer
        if timer is None:
            return
        try:
            timer.stop()
        except Exception:
            pass
        try:
            timer.timeout.disconnect()
        except Exception:
            pass
        self._timer = None

    def on_scene_changed(self):
        """表示先(project_key と status.json のパス)だけ更新する。原本へは戻さない。"""
        try:
            note = getattr(self.controller, "note_scene_changed", None)
            if callable(note):
                note()
        except Exception:
            pass
        self.set_message(MSG_SCENE_SWITCHED)
        recovered = []
        try:
            recovered = list(self.controller.recover_orphaned() or [])
        except Exception:
            recovered = []
        try:
            result = self.controller.status_path()
        except Exception:
            result = {"ok": False, "message": MSG_NO_PROJECT, "path": None}
        if recovered:
            self.set_message(MSG_INTERRUPTED)
        elif not result.get("ok"):
            self.set_message(result.get("message") or MSG_NO_PROJECT)
        self._refresh_panel()


def _controller_from_window(window, history_fn=None):
    """LiveSyncWindow からコントローラを組む。window の公開/既存メソッドだけを使う。"""

    def get_config():
        watcher = getattr(window, "watcher", None)
        cfg = getattr(watcher, "config", None)
        return cfg if isinstance(cfg, dict) else {}

    def save_config_fn(partial):
        watcher = getattr(window, "watcher", None)
        saver = getattr(watcher, "save_mapping_only", None)
        if callable(saver):
            saver(partial)
            return
        cfg = getattr(watcher, "config", None)
        if isinstance(cfg, dict) and isinstance(partial, dict):
            cfg.update(partial)
        import maya_live_sync as mls
        mls.save_config(partial)

    def project_key_fn():
        return window._current_quality_target_project_key()

    def known_ts_fn(project_key):
        cfg = get_config()
        by_project = cfg.get("known_texture_sets_by_project") or {}
        return lookup_project_entry(by_project, project_key)

    def final_dir_fn(project_key):
        return window.watcher._final_dir_for_project(project_key)

    def final_subfolder_fn(project_key):
        table = window.watcher._final_subfolder_by_project_normalized() or {}
        if project_key in table:
            return table[project_key]
        folded = str(project_key).casefold() if project_key else ""
        for key, value in table.items():
            if str(key).casefold() == folded:
                return value
        return None

    def list_texture_sets_fn():
        import uvremap_maya as um
        return um.list_texture_sets()

    if history_fn is None:
        def history_fn(source, text):
            import maya_live_sync as mls
            mls._append_history(source, text)

    return UVRemapController(
        get_config=get_config,
        save_config_fn=save_config_fn,
        project_key_fn=project_key_fn,
        known_ts_fn=known_ts_fn,
        final_dir_fn=final_dir_fn,
        final_subfolder_fn=final_subfolder_fn,
        list_texture_sets_fn=list_texture_sets_fn,
        history_fn=history_fn,
    )


def run_poll_tick(controller, handle, now_ns=None):
    """on_tick の本体。poll の message が空でなければ set_message する。

    poll の直後、``follow_tick`` の前に ``rebase_tick`` を呼ぶ。
    続けて ``follow_tick``、``uv_tick``。例外は外へ出さない。
    ``rebase_tick`` が無いコントローラでは呼ばない。
    """
    try:
        result = controller.poll()
    except Exception:
        result = None
    text = ""
    if isinstance(result, dict):
        raw = result.get("message") or ""
        text = raw if isinstance(raw, str) else ""
    if now_ns is None:
        now_ns = time.time_ns()
    try:
        rebase_tick = getattr(controller, "rebase_tick", None)
        if callable(rebase_tick):
            rebase_result = rebase_tick(now_ns / 1_000_000_000)
            if isinstance(rebase_result, dict):
                raw = rebase_result.get("message") or ""
                rebase_text = raw if isinstance(raw, str) else ""
                if rebase_text:
                    text = rebase_text
    except Exception:
        pass
    follow_result = None
    try:
        follow_result = controller.follow_tick(now_ns)
    except Exception:
        follow_result = None
    if isinstance(follow_result, dict):
        raw = follow_result.get("message") or ""
        follow_text = raw if isinstance(raw, str) else ""
        if follow_text:
            text = follow_text
    try:
        uv_tick = getattr(controller, "uv_tick", None)
        if callable(uv_tick):
            now_s = time.time() if now_ns is None else (now_ns / 1_000_000_000)
            uv_result = uv_tick(now_s)
            if isinstance(uv_result, dict):
                raw = uv_result.get("message") or ""
                uv_text = raw if isinstance(raw, str) else ""
                if uv_text:
                    text = uv_text
    except Exception:
        pass
    if text:
        setter = getattr(handle, "set_message", None)
        if callable(setter):
            try:
                setter(text)
            except Exception:
                pass
    refresh = getattr(handle, "_refresh_panel", None)
    if callable(refresh):
        try:
            refresh()
        except Exception:
            pass
    return result


def start_poll_after_recover(controller, timer, set_message_fn=None):
    """孤立 CONVERTING を回収してから poll タイマーを開始する。回収の例外は出さない。"""
    names = []
    try:
        names = list(controller.recover_orphaned() or [])
    except Exception:
        names = []
    if names and set_message_fn is not None:
        try:
            set_message_fn(MSG_INTERRUPTED)
        except Exception:
            pass
    timer.start()
    return names


def _status_has_state(status_path, state_name):
    """status.json の行に指定状態があれば True。読めなければ False。"""
    if not status_path:
        return False
    try:
        from uvremap_panel import StatusModel
        view = StatusModel(status_path).load()
    except Exception:
        return False
    rows = getattr(view, "rows", ()) or ()
    return any(getattr(row, "state", None) == state_name for row in rows)


def _status_has_converting(status_path):
    """status.json の行に CONVERTING があれば True。読めなければ False。"""
    try:
        from uvremap_panel import STATE_CONVERTING
    except Exception:
        return False
    return _status_has_state(status_path, STATE_CONVERTING)


def _status_has_stale(status_path):
    """status.json の行に STALE があれば True。読めなければ False。"""
    try:
        from uvremap_panel import STATE_STALE
    except Exception:
        return False
    return _status_has_state(status_path, STATE_STALE)


def _apply_visible_now(controller):
    """開閉行の「変更を反映する」を出すか。純関数 ``apply_button_visible`` に渡す。"""
    try:
        key = controller._project_key()
        if not key:
            return False
        states = controller._status_ts_states(key)
    except Exception:
        return False
    return apply_button_visible(states)


def _current_fold_status(controller):
    """有効フラグと status から、折りたたみ横の短い文言を決める。"""
    enabled = False
    try:
        enabled = bool(controller._enabled_from_config())
    except Exception:
        enabled = False
    converting = False
    needs_apply = False
    auto_any = False
    if enabled:
        path = None
        try:
            found = controller.status_path()
            if isinstance(found, dict):
                path = found.get("path")
        except Exception:
            path = None
        converting = _status_has_converting(path)
        needs_apply = _status_has_stale(path)
        painter = False
        uv_on = False
        getter = getattr(controller, "_auto_follow_from_config", None)
        if callable(getter):
            try:
                painter = bool(getter())
            except Exception:
                painter = False
        getter_uv = getattr(controller, "_auto_follow_uv_from_config", None)
        if callable(getter_uv):
            try:
                uv_on = bool(getter_uv())
            except Exception:
                uv_on = False
        auto_any = painter or uv_on
    return fold_status_label_p2(enabled, converting, needs_apply, auto_any)


def attach(window, parent_layout, controller=None, confirm_fn=None, inform_fn=None):
    """「UV再マップ(試験運用)」欄を parent_layout に足す。"""
    QtCore, QtWidgets = _load_qt()
    if controller is None:
        controller = _controller_from_window(window)
    try:
        controller.refresh_shape_map()
    except Exception:
        pass

    group = QtWidgets.QGroupBox("UV再マップ(試験運用)")
    group.setObjectName("uvremap_group")
    group.setCheckable(False)
    group.setChecked(False)
    group_layout = QtWidgets.QVBoxLayout(group)
    group_layout.setContentsMargins(8, 8, 8, 8)
    group_layout.setSpacing(6)

    fold_row = QtWidgets.QWidget(group)
    fold_row.setObjectName("uvremap_fold_row")
    fold_layout = QtWidgets.QHBoxLayout(fold_row)
    fold_layout.setContentsMargins(0, 0, 0, 0)
    fold_layout.setSpacing(8)
    fold_btn = QtWidgets.QPushButton(LABEL_FOLD_SHOW)
    fold_btn.setObjectName("btn_uvremap_fold")
    fold_btn.setFlat(True)
    fold_status = QtWidgets.QLabel(LABEL_FOLD_OFF)
    fold_status.setObjectName("uvremap_fold_status")
    from uvremap_panel import COLOR_ATTENTION
    apply_btn = QtWidgets.QPushButton(LABEL_APPLY)
    apply_btn.setObjectName("btn_apply_changes")
    apply_btn.setVisible(False)
    apply_btn.setStyleSheet(
        "QPushButton {{ background-color: {0}; color: #ffffff; padding: 4px; }}".format(
            COLOR_ATTENTION,
        )
    )
    fold_layout.addWidget(fold_btn)
    fold_layout.addWidget(fold_status)
    fold_layout.addWidget(apply_btn)
    fold_layout.addStretch(1)
    group_layout.addWidget(fold_row)

    detail = QtWidgets.QWidget(group)
    detail.setObjectName("uvremap_detail")
    detail_layout = QtWidgets.QVBoxLayout(detail)
    detail_layout.setContentsMargins(0, 0, 0, 0)
    detail_layout.setSpacing(6)

    hint = QtWidgets.QLabel(MSG_HINT_RECORD)
    hint.setObjectName("uvremap_hint")
    hint.setWordWrap(True)
    detail_layout.addWidget(hint)

    record_btn = QtWidgets.QPushButton("基準UVを記録")
    record_btn.setObjectName("btn_record_baseline")
    detail_layout.addWidget(record_btn)

    message = QtWidgets.QLabel("")
    message.setObjectName("uvremap_message")
    message.setWordWrap(True)
    detail_layout.addWidget(message)

    from uvremap_panel import UVRemapPanel

    def get_enabled():
        try:
            return bool(controller._enabled_from_config())
        except Exception:
            return False

    def get_status_path():
        try:
            return controller.status_path().get("path")
        except Exception:
            return None

    attachment_box = {}

    def show_message(text):
        handle = attachment_box.get("handle")
        if handle is not None:
            handle.set_message(text)
        else:
            try:
                message.setText("" if text is None else str(text))
            except Exception:
                pass

    def refresh_open_views():
        panel = attachment_box.get("panel")
        if panel is not None:
            try:
                panel.refresh()
            except Exception:
                pass
        handle = attachment_box.get("handle")
        if handle is not None:
            handle.refresh_fold_status()

    def on_toggle_enabled(checked):
        result = controller.set_enabled(bool(checked))
        show_message(result.get("message") or "")
        refresh_open_views()

    def get_auto_follow():
        getter = getattr(controller, "_auto_follow_from_config", None)
        if not callable(getter):
            return False
        try:
            return bool(getter())
        except Exception:
            return False

    def on_toggle_auto_follow(checked):
        result = controller.set_auto_follow(bool(checked))
        show_message(result.get("message") or "")
        refresh_open_views()

    def get_auto_follow_uv():
        getter = getattr(controller, "_auto_follow_uv_from_config", None)
        if not callable(getter):
            return False
        try:
            return bool(getter())
        except Exception:
            return False

    def on_toggle_auto_follow_uv(checked):
        setter = getattr(controller, "set_auto_follow_uv", None)
        if not callable(setter):
            return
        result = setter(bool(checked))
        show_message(result.get("message") or "")
        refresh_open_views()

    def on_apply_changes():
        result = controller.apply_changes()
        show_message(result.get("message") or "")
        refresh_open_views()

    def on_convert_now():
        result = controller.convert_now()
        show_message(result.get("message") or "")
        refresh_open_views()

    def on_revert():
        result = controller.revert()
        show_message(result.get("message") or "")
        refresh_open_views()

    def on_rebase_send():
        result = handle_rebase_request(controller, qt_confirm, qt_inform)
        show_message(result.get("message") or "")
        refresh_open_views()

    def on_rebase_cancel():
        cancel = getattr(controller, "rebase_cancel", None)
        if not callable(cancel):
            return
        result = cancel()
        show_message(result.get("message") or "")
        refresh_open_views()

    def get_rebase_view():
        getter = getattr(controller, "rebase_view", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    panel = UVRemapPanel(
        parent=detail,
        on_toggle_enabled=on_toggle_enabled,
        on_convert_now=on_convert_now,
        on_revert=on_revert,
        get_enabled=get_enabled,
        get_status_path=get_status_path,
        get_auto_follow=get_auto_follow,
        on_toggle_auto_follow=on_toggle_auto_follow,
        get_auto_follow_uv=get_auto_follow_uv,
        on_toggle_auto_follow_uv=on_toggle_auto_follow_uv,
        on_rebase_send=on_rebase_send,
        on_rebase_cancel=on_rebase_cancel,
        get_rebase_view=get_rebase_view,
    )
    panel.setObjectName("uvremap_panel")
    detail_layout.addWidget(panel)
    attachment_box["panel"] = panel

    def qt_confirm(text):
        if confirm_fn is not None:
            return bool(confirm_fn(text))
        reply = QtWidgets.QMessageBox.question(
            group, "UV再マップ", text,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        return reply == QtWidgets.QMessageBox.Yes

    def qt_inform(text):
        if inform_fn is not None:
            inform_fn(text)
            return
        QtWidgets.QMessageBox.information(group, "UV再マップ", text)

    def on_record():
        result = handle_record_request(controller, qt_confirm, qt_inform)
        show_message(result.get("message") or "")
        refresh_open_views()

    record_btn.clicked.connect(on_record)

    group_layout.addWidget(detail)
    detail.setVisible(False)
    fold_open = {"value": False}

    def on_fold_clicked():
        fold_open["value"] = not fold_open["value"]
        detail.setVisible(fold_open["value"])
        fold_btn.setText(LABEL_FOLD_HIDE if fold_open["value"] else LABEL_FOLD_SHOW)

    fold_btn.clicked.connect(on_fold_clicked)
    apply_btn.clicked.connect(on_apply_changes)
    parent_layout.addWidget(group)

    timer = QtCore.QTimer(group)
    timer.setObjectName("uvremap_poll_timer")
    timer.setInterval(POLL_INTERVAL_MS)

    def refresh_fold_status():
        try:
            fold_status.setText(_current_fold_status(controller))
        except Exception:
            pass
        try:
            apply_btn.setVisible(_apply_visible_now(controller))
        except Exception:
            pass

    handle = UVRemapAttachment(
        timer, controller, panel=panel, message_label=message, group=group,
        fold_refresh=refresh_fold_status,
    )
    attachment_box["handle"] = handle
    refresh_fold_status()

    def on_tick():
        run_poll_tick(controller, handle)

    timer.timeout.connect(on_tick)
    start_poll_after_recover(controller, timer, handle.set_message)
    return handle
