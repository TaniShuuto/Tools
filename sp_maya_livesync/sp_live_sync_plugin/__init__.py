"""
sp_live_sync_plugin フォルダー形式 (sp_live_sync_plugin/__init__.py)
Substance 3D Painter 側プラグイン(SP Live Sync, B案:自前パイプライン)

変更履歴はリポジトリルートの CHANGELOG.md を参照。
"""

# 分割前のトップレベル名を保つための import(互換)。
import os
import re
import json
import copy
import stat
import time
import uuid
import shutil
import hashlib
import math
import inspect
import functools
import unicodedata
import tempfile
import datetime
import contextlib
from collections import namedtuple
import sys
import importlib

import substance_painter.event as event
import substance_painter.export as export
import substance_painter.project as project
import substance_painter.textureset as textureset
import substance_painter.logging as sp_log
import substance_painter.ui as ui
import substance_painter.exception as sp_exception

# 部品を最初にまとめて import する(遅延 import はしない)。下の層から順に読む。
# 分割前のトップレベル名はここから参照できる(互換の再エクスポート)。
from ._qt import QtCore, QtGui, QtWidgets
from ._version import __version__
from ._base import (
    CONFIG_DIR,
    CONFIG_LOCK_PATH,
    CONFIG_PATH,
    DEFAULT_CONFIG,
    EXPORT_MESSAGE_MAX_CHARS,
    EXPORT_RESULT_SCHEMA,
    HISTORY_LOG_PATH,
    MESH_SHA1_MAX_BYTES,
    REBASE_DIRNAME,
    REBASE_REQUEST_ID_RE,
    REBASE_RESULTS_DIRNAME,
    SYNC_FLAG_NAME,
    SYNC_RESULT_NAME,
    _CLOUD_SYNC_PATH_MARKERS,
    _CLOUD_SYNC_TEMP_MARKERS,
    _CONFIG_LOCK_POLL_INTERVAL_SECONDS,
    _CONFIG_LOCK_TIMEOUT_SECONDS,
    _ConfigLockTimeout,
    _EXPORT_STATUS_LABELS,
    _EXPORT_STATUS_TOKENS,
    _HASH_CHUNK_BYTES,
    _HISTORY_MAX_BYTES,
    _append_history,
    _checked_rebase_request_id,
    _cleanup_orphaned_staging_dirs,
    _config_file_lock,
    _detect_cloud_sync_risk,
    _hash_file_hex,
    _is_unsaved_project_key,
    _log,
    _match_known_export_status,
    _merge_and_write_config,
    _status_token,
    _update_nested_config_entry,
    build_export_result,
    build_mesh_record,
    export_status_label,
    export_status_name,
    is_valid_rebase_request_id,
    load_config,
    msvcrt,
    publish_export_outputs,
    rebase_result_path,
    rebase_results_dir,
    save_config,
    write_json_atomic,
    write_rebase_result_copy,
    write_sync_flag,
)
from ._project_key import _current_project_key, _project_subfolder_name
from ._settings_model import (
    PANEL_SETTING_KEYS,
    RESOLUTION_CHOICES,
    _PANEL_FLOAT_DEFAULTS,
    _PANEL_INT_DEFAULTS,
    _PANEL_PATH_KEYS,
    _PANEL_SETTING_LABELS,
    _duplicate_folder_warning,
    _panel_paths_match,
    _panel_setting_matches,
    unsaved_setting_keys,
)
from ._rebase_policy import (
    RebasePrecheckCaches,
    _REBASE_PRECHECK_CACHE_MAX,
    _rebase_safe_stem,
    build_painter_state,
    build_rebase_approval_text,
    build_reload_settings_kwargs,
    classify_reload_status,
    compute_last_edit_age,
    decide_approval,
    mesh_precheck,
    needs_saving_for_rebase,
    rebase_backup_path,
    rebase_retry_delay,
    rebase_wait_message,
    supports_auto_unwrap_settings,
    wait_timed_out,
)
from ._rebase_protocol import (
    ApprovalDecision,
    BACKUP_INCLUDES_UNSAVED,
    DEFAULT_UVREMAP_ROOT_DIR,
    REBASE_APPROVAL_ONLY_REASONS,
    REBASE_AUTO_UNWRAP_NOTE,
    REBASE_AUTO_UNWRAP_UNSUPPORTED_LOG,
    REBASE_BACKUP_DIRNAME,
    REBASE_BASELINE_ID_RE,
    REBASE_BUTTON_CANCEL,
    REBASE_BUTTON_RUN,
    REBASE_CANCELABLE_STATES,
    REBASE_CAUTION_TEXT,
    REBASE_CAUTION_TEXT_REPROJECT,
    REBASE_CONFIRM_LEAD,
    REBASE_DIALOG_TITLE,
    REBASE_DIRECTION_APPROVAL_NOTE,
    REBASE_DIRECTION_WARNING_FMT,
    REBASE_DONE_GUIDANCE,
    REBASE_EXPORT_DEFERRED_STATUS,
    REBASE_INTERRUPTED_MESSAGE,
    REBASE_LOCKED_STATES,
    REBASE_MESSAGE_MAX_CHARS,
    REBASE_REASONS,
    REBASE_REASON_MESSAGES,
    REBASE_RELOAD_EXPECTED,
    REBASE_REQUESTS_DIRNAME,
    REBASE_SCHEMA,
    REBASE_STATES,
    REBASE_STATUS_DIRNAME,
    REBASE_TERMINAL_STATES,
    REBASE_TERMINAL_WRITE_MAX,
    REBASE_UNSAVED_NOTE,
    REBASE_UV_FILL_PROGRESS,
    REBASE_UV_OLD_INDEX_MAX,
    REBASE_UV_OLD_INDEX_MIN,
    REBASE_UV_OLD_NAME_MAX,
    REBASE_UV_OLD_NAME_RE,
    REBASE_WAIT_MESSAGES,
    REBASE_WORKFLOW_GUIDANCE,
    RebaseScan,
    RebaseScanDiff,
    _PAINTER_STATE_DEFAULTS,
    _PAINTER_STATE_KEYS,
    _REBASE_BACKUP_MESSAGE_REASONS,
    _REBASE_REASON_SET,
    _REBASE_REQUEST_KEYS,
    _REBASE_SHA1_RE,
    _REBASE_STATE_SET,
    _REBASE_STATUS_REASON_SET,
    _REBASE_TERMINAL,
    _REBASE_TRANSITIONS,
    _REBASE_UV_FILLS_KEYS,
    _REBASE_WAIT_MAX_MAX_S,
    _REBASE_WAIT_MAX_MIN_S,
    _REBASE_WIN_BAD_FILENAME,
    _normalize_uv_fills,
    _painter_state_get,
    _parse_rebase_mesh,
    _parse_rebase_ts_names,
    _parse_rebase_uv_old,
    _rebase_clip_message,
    _rebase_coerce_bool,
    _rebase_coerce_float,
    _rebase_coerce_retry_max,
    _rebase_copy_reload,
    _rebase_filename_only,
    _rebase_is_finite_number,
    _rebase_optional_text,
    _rebase_reload_problem,
    build_interrupted_status,
    build_rebase_status,
    diff_rebase_scan,
    effective_rebase_approval,
    find_superseded_ids,
    get_uv_old_index,
    get_uv_old_name,
    is_valid_rebase_transition,
    normalize_project_key_for_compare,
    normalize_project_key_for_storage,
    normalize_rebase_settings,
    parse_rebase_request,
    parse_rebase_status,
    project_keys_match,
    read_json_safe,
    read_rebase_request,
    read_rebase_request_with_reason,
    read_rebase_status,
    rebase_cancel_path,
    rebase_failure_message,
    rebase_reject_state,
    rebase_request_path,
    rebase_requests_dir,
    rebase_status_dir,
    rebase_status_path,
    resolve_uvremap_root,
    scan_rebase_dir,
    uvremap_root_for_project,
    write_rebase_status,
)
from ._rebase_uvfill import (
    UvFillApi,
    UvFillStackReport,
    UvFillSummary,
    _UV_FILL_GATE_MESSAGES,
    _UV_FILL_PARAM_ATTRS,
    _direction_fill_name,
    _iter_named_children,
    _iter_texture_set_stacks,
    _node_has_projection_api,
    _remember_missing_attrs,
    _safe_call,
    _texture_set_key,
    _texture_set_name,
    _uv_fill_direction_state,
    _uv_fill_visit_key,
    inspect_direction_uv_fills,
    iter_uv_fill_nodes,
    node_type_token,
    rollback_uv_fill_node,
    switch_uv_fill_node,
    switch_uv_fills_for_project,
    switch_uv_fills_in_stack,
    uv_fill_has_direction_channel,
)
from ._rebase_coordinator import (
    RebaseCoordinator,
    _REBASE_CFG_RELOAD_S,
    _iter_status_ids,
)
from ._rebase_job import ExportAttempt, RebaseJob, RebaseReporter, _is_project_error
from ._painter_rebase_api import (
    PainterRebaseApi,
    RebaseApprovalDialogHost,
    RebaseEngineHooks,
    approval_action_from_role,
    default_rebase_job_factory,
    layerstack,
    make_rebase_approval_dialog,
    make_uv_fill_api,
)
from ._live_engine import (
    CHANNEL_SUFFIXES,
    LiveSyncEngine,
    _FINAL_EXPORT_GIVEUP_FOLLOWUP_MS,
    _FINAL_EXPORT_INITIAL_DELAY_MS,
    _FINAL_EXPORT_MAX_RETRIES,
    _FINAL_EXPORT_QUEUED_DELAY_MS,
    _FINAL_EXPORT_RETRY_BASE_MS,
    _FINAL_EXPORT_RETRY_MAX_MS,
    _MESH_RECORD_DELAY_MS,
    _MESH_RECORD_MAX_RETRIES,
    _MESH_RECORD_RETRY_DELAY_MS,
)
from ._live_panel import LiveSyncPanel
from ._wizard import SetupWizard


# ---------------------------------------------------------------------------
# プラグインのライフサイクル(Substance Painterが自動的に呼び出す)
# ---------------------------------------------------------------------------

_engine = None
_panel = None


# フォルダー版(sp_live_sync_plugin/__init__.py)へ移したあと、plugins フォルダーに
# 古い1ファイル版が残っていると、どちらが読まれているか分かりにくい。
# 検出だけ行い、ファイルは削除しない。
STALE_SINGLE_FILE_PLUGIN_WARNING = (
    "古い sp_live_sync_plugin.py が plugins フォルダーに残っています({path})。"
    "このフォルダー版が使われていますが、混乱を防ぐため古いファイルを削除してください。"
)


def stale_single_file_plugin_path(init_file):
    """__init__.py の絶対パスから、古い1ファイル版のパスを返す。

    返すのは <init_file の親の親>/sp_live_sync_plugin.py がファイルとして
    存在するときだけ。無い、同名のフォルダーだけ、パスが壊れている、の
    いずれでも None。例外は出さない。この1パスの存在確認しかしない。
    """
    try:
        if isinstance(init_file, bytes):
            init_file = os.fsdecode(init_file)
        elif hasattr(init_file, "__fspath__"):
            init_file = os.fspath(init_file)
        if not isinstance(init_file, str) or not init_file:
            return None
        package_dir = os.path.dirname(init_file)
        plugins_dir = os.path.dirname(package_dir)
        if not plugins_dir:
            return None
        candidate = os.path.join(plugins_dir, "sp_live_sync_plugin.py")
        if os.path.isfile(candidate):
            return candidate
    except Exception:
        return None
    return None


def start_plugin():
    global _engine, _panel
    # SP側・Maya側でバージョンがズレたまま運用してしまい、片方だけ古い
    # ロジックで動いていることに気付けなかった経緯があるため、起動の
    # 最初に必ずバージョンをログへ出す(Pythonログに出力される)。
    _log("info", "[sp_live_sync_plugin] loaded version: {0}  (file: {1})".format(
        __version__, os.path.abspath(__file__)))
    try:
        stale_path = stale_single_file_plugin_path(os.path.abspath(__file__))
        if stale_path:
            stale_message = STALE_SINGLE_FILE_PLUGIN_WARNING.format(path=stale_path)
            _log("warning", stale_message)
            _append_history("SP", stale_message)
    except Exception as stale_error:
        _log("warning", "古い1ファイル版プラグインの確認に失敗しました: {0}".format(stale_error))
    # 2026.07.24(緊急バグ修正): 従来は既存の_engineの有無を確認せず
    # 無条件で新規LiveSyncEngine()を作成していた。SPのプラグイン無効化→
    # 再有効化を経由せずstart_plugin()が二重に呼ばれた場合(Mayaの
    # reload()後にshow_ui()を呼ぶと旧インスタンスがリークしていた不具合
    # と同じクラス)、旧エンジンのevent.DISPATCHER購読(connect_strongの
    # 強参照)が解除されないまま残り、同期処理が多重発火する恐れが
    # あった。close_plugin()と同じ後始末を先に行ってから差し替える。
    if _engine is not None:
        try:
            _engine.shutdown()
        except Exception as e:
            _log("error", "既存エンジンの後始末に失敗しました: {0}".format(e))
        _engine = None
    if _panel is not None:
        try:
            ui.delete_ui_element(_panel)
        except Exception as e:
            _log("error", "既存パネルの後始末に失敗しました: {0}".format(e))
        _panel = None
    try:
        _engine = LiveSyncEngine()
        try:
            _engine.start_rebase_watch()
        except Exception as e:
            _log("warning", "rebase タイマーの開始に失敗しました: {0}".format(e))
        # 2026.07.19-01: クラッシュ等でfinally節が実行されず残った
        # ステージングフォルダの残骸を、起動のたびに自動回収する。
        # 失敗しても起動処理自体は継続させたいため、専用のtry/exceptで
        # 囲む(_cleanup_orphaned_staging_dirs自体は内部でOSErrorを
        # 個別に握りつぶしているが、stage_rootの取得自体が失敗する
        # ケースも念のため保護する)。
        try:
            _cleanup_orphaned_staging_dirs(
                _engine.config.get("staging_dir", DEFAULT_CONFIG["staging_dir"]))
        except Exception as e:
            _log("warning", "ステージングフォルダの自動掃除に失敗しました: {0}".format(e))
        # 2026.07.25(緊急バグ修正): 共有フォルダがクラウド同期対象に
        # 入っていると、設定ファイルの原子的な置き換え・アドバイザリ
        # ロックの前提が崩れる(詳細は _detect_cloud_sync_risk() 上の
        # コメント参照)。強制はせず、起動のたびに検出して知らせる。
        try:
            cloud_sync_reason = _detect_cloud_sync_risk(CONFIG_DIR)
            if cloud_sync_reason:
                _engine._emit_status(
                    "警告: 共有フォルダ({0})がクラウド同期(Google Drive/OneDrive/"
                    "Dropbox等)の対象になっている可能性があります({1})。"
                    "同期クライアントがファイルを横から書き換えることで、"
                    "設定ファイルの巻き戻りや同期の無言失敗につながる恐れが"
                    "あります。可能であればこのフォルダを同期対象から"
                    "除外してください。".format(CONFIG_DIR, cloud_sync_reason)
                )
        except Exception as e:
            _log("warning", "クラウド同期の検出に失敗しました: {0}".format(e))
        _panel = LiveSyncPanel(_engine)
        ui.add_dock_widget(_panel)
        if not _engine.config.get("setup_wizard_completed"):
            _panel.open_setup_wizard()
        _log("info", "SP -> Maya Live Sync (Phase 1 + Phase 2最適化 + Phase 3) を起動しました。")
    except Exception as e:
        _log("error", "プラグインの起動に失敗しました: {0}".format(e))


def close_plugin():
    global _engine, _panel
    # 2026.10.19(1.4.0 PB-b S3): 先にグローバルを退避して None にし、
    # 後始末は個別の try/except で守る。delete_ui_element(_panel) は残す
    # (#38 のパネル接続を片付ける唯一の経路)。
    engine = _engine
    panel = _panel
    _engine = None
    _panel = None
    if engine is not None:
        try:
            engine.shutdown()
        except Exception as e:
            _log("error", "エンジンの後始末に失敗しました: {0}".format(e))
    if panel is not None:
        try:
            ui.delete_ui_element(panel)
        except Exception as e:
            _log("error", "パネルの後始末に失敗しました: {0}".format(e))
    # S4: 循環を切ったあと、ローカルの panel も外してから回収を確認する。
    # deleteLater はまだ入れない。
    ref = None
    if engine is not None:
        try:
            import weakref
            ref = weakref.ref(engine)
        except Exception:
            ref = None
    engine = None
    panel = None
    if ref is not None:
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        if ref() is None:
            _log("info", "LiveSyncEngine は解放されました")
        else:
            _log(
                "warning",
                "LiveSyncEngine が残っています(要実機確認 S4)。deleteLater は入れていません",
            )
    _log("info", "SP -> Maya Live Sync プラグインを停止しました。")


# ---------------------------------------------------------------------------
# 再読み込み(Painter の substance_painter_plugins.reload_plugin が呼ぶフック)
# ---------------------------------------------------------------------------
# Adobe の説明では、このフックは close_plugin() のあと、start_plugin() の前に
# 呼ばれ、パッケージ形式のプラグインが「部品(サブモジュール)を手で読み直す」ための
# ものとされている。本体(この __init__)を importlib.reload するのがフックより前か
# 後かは資料からは確定できないため、どちらの順でも部品が新しくなるようにしてある。
#   - sys.modules から部品を取り除く: 本体の再読み込みがこのあとに来る場合、
#     本体は新しく読み直された部品を import する。
#   - 本体をここでも読み込み直す: 本体の再読み込みが先に済んでいて古い部品を
#     指したままの場合に、新しい部品へ付け替える(順序が逆でも二重になるだけで害は無い)。
# 要実機確認: Painter 実機での呼ばれる順序と、再読み込み後に部品が新しくなること。
def reload_plugin(*_args, **_kwargs):
    package = sys.modules[__name__]
    prefix = __name__ + "."
    for module_name in [name for name in sys.modules if name.startswith(prefix)]:
        del sys.modules[module_name]
    importlib.reload(package)
