# -*- coding: utf-8 -*-
"""Live Sync 窓への UV 再マップ組み込み(基準記録・今すぐ変換・原本に戻す)。

Maya / Qt は関数の中だけ。純粋部分はどちらも無しでテストできる。
失敗しても呼び出し元(maya_live_sync)が Live Sync 本体を止めないこと。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from uvremap_follow import (
    MSG_AUTO_INPUT_AS_IS,
    MSG_INPUT_STALE,
    MSG_PAINTER_START_FMT,
    MSG_PEND_FMT,
    MSG_QUEUE_FMT,
    decide_follow,
    fold_status_label_p2,
    input_stale,
    load_follow,
    normalize_follow_settings,
    save_follow,
    source_signature,
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

DEFAULT_UVREMAP_ROOT = "C:/SPMayaLiveSync/uvremap"
CONVERT_TIMEOUT_S = 1200
POLL_INTERVAL_MS = 1000

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
    ):
        self.get_config = get_config
        self.save_config_fn = save_config_fn
        self.project_key_fn = project_key_fn
        self.known_ts_fn = known_ts_fn
        self.final_dir_fn = final_dir_fn
        self.final_subfolder_fn = final_subfolder_fn
        self.list_texture_sets_fn = list_texture_sets_fn
        self.history_fn = history_fn
        self._follow_last_flag_mtime_ns = None
        self._follow_last_change_seen_ns = None
        self._auto_input_as_is_emitted = False
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
            cfg = self._safe_config()
            if isinstance(cfg, dict):
                cfg["uvremap_enabled"] = enabled
            self.save_config_fn({"uvremap_enabled": enabled})
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
            cfg = self._safe_config()
            if isinstance(cfg, dict):
                cfg["uvremap_auto_follow"] = enabled
            self.save_config_fn({"uvremap_auto_follow": enabled})
            message = ""
            if enabled and not self._auto_input_as_is_emitted:
                message = MSG_AUTO_INPUT_AS_IS
                self._write_history(message)
                self._auto_input_as_is_emitted = True
            return {"ok": True, "message": message, "auto_follow": enabled}
        except Exception:
            return {"ok": False, "message": "設定の更新に失敗しました"}

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
                job_id = self.runtime.convert_now(
                    resolved["project_key"],
                    ts_name,
                    resolved["shape_paths"],
                    source_dir=resolved["final_dir"],
                    output_dir=out,
                    quality="final",
                    timeout_s=CONVERT_TIMEOUT_S,
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

    def follow_tick(self, now_ns):
        """Painter の印を見て、自動変換または入力側 STALE を進める。例外は出さない。"""
        try:
            return self._follow_tick_impl(now_ns)
        except Exception:
            return {"ok": False, "message": ""}

    def _follow_tick_impl(self, now_ns):
        enabled = bool(self._enabled_from_config())
        if not enabled:
            return {"ok": True, "message": ""}
        resolved = self._resolve_context()
        if not isinstance(resolved, dict) or not resolved.get("ok"):
            return {"ok": True, "message": ""}
        final_dir = resolved.get("final_dir")
        root = resolved.get("root")
        project_key = resolved.get("project_key")
        if not final_dir or not root or not project_key:
            return {"ok": True, "message": ""}
        flag_path = Path(final_dir) / "_sync_complete.flag"
        try:
            flag_mtime_ns = int(flag_path.stat().st_mtime_ns)
        except OSError:
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
                job_id = self.runtime.convert_now(
                    project_key,
                    ts_name,
                    shape_paths,
                    source_dir=final_dir,
                    output_dir=out,
                    quality="final",
                    timeout_s=CONVERT_TIMEOUT_S,
                    origin="auto",
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
        return {"ok": True, "message": messages[-1] if messages else ""}

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

    poll の後に ``follow_tick`` を呼ぶ。follow の例外は外へ出さない。
    """
    try:
        result = controller.poll()
    except Exception:
        result = None
    text = ""
    if isinstance(result, dict):
        raw = result.get("message") or ""
        text = raw if isinstance(raw, str) else ""
    follow_result = None
    try:
        if now_ns is None:
            now_ns = time.time_ns()
        follow_result = controller.follow_tick(now_ns)
    except Exception:
        follow_result = None
    if isinstance(follow_result, dict):
        raw = follow_result.get("message") or ""
        follow_text = raw if isinstance(raw, str) else ""
        if follow_text:
            text = follow_text
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
        getter = getattr(controller, "_auto_follow_from_config", None)
        if callable(getter):
            try:
                auto_any = bool(getter())
            except Exception:
                auto_any = False
    return fold_status_label_p2(enabled, converting, needs_apply, auto_any)


def attach(window, parent_layout, controller=None, confirm_fn=None, inform_fn=None):
    """「UV再マップ(試験運用)」欄を parent_layout に足す。"""
    QtCore, QtWidgets = _load_qt()
    if controller is None:
        controller = _controller_from_window(window)

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
    fold_layout.addWidget(fold_btn)
    fold_layout.addWidget(fold_status)
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

    def on_convert_now():
        result = controller.convert_now()
        show_message(result.get("message") or "")
        refresh_open_views()

    def on_revert():
        result = controller.revert()
        show_message(result.get("message") or "")
        refresh_open_views()

    panel = UVRemapPanel(
        parent=detail,
        on_toggle_enabled=on_toggle_enabled,
        on_convert_now=on_convert_now,
        on_revert=on_revert,
        get_enabled=get_enabled,
        get_status_path=get_status_path,
        get_auto_follow=get_auto_follow,
        on_toggle_auto_follow=on_toggle_auto_follow,
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
    parent_layout.addWidget(group)

    timer = QtCore.QTimer(group)
    timer.setObjectName("uvremap_poll_timer")
    timer.setInterval(POLL_INTERVAL_MS)

    def refresh_fold_status():
        try:
            fold_status.setText(_current_fold_status(controller))
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
