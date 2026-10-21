# -*- coding: utf-8 -*-
"""sp_live_sync_plugin の分割の割り当て表(名前 -> 部品)と、部品の層。

分割前の __init__.py のトップレベル名は、ここに載せた部品のどれかへ移る。
表に無い名前があると split_plugin.py はエラーで止まる(行番号では決めない)。

層の規則: 下の層は上の層を import しない。同じ層どうしは、循環しない
向きだけ import してよい。
"""
from __future__ import annotations

# 部品名 -> 層
LAYERS = {
    "_version": 0,
    "_qt": 0,
    "_base": 1,
    "_project_key": 2,
    "_settings_model": 2,
    "_rebase_protocol": 3,
    "_rebase_policy": 3,
    "_rebase_uvfill": 3,
    "_rebase_job": 4,
    "_rebase_coordinator": 4,
    "_painter_rebase_api": 5,
    "_live_engine": 6,
    "_wizard": 7,
    "_live_panel": 7,
    "__init__": 8,
}

# 純関数の部品。substance_painter / PySide / _qt を import しない。
PURE_PARTS = (
    "_settings_model",
    "_rebase_protocol",
    "_rebase_policy",
    "_rebase_uvfill",
)

# 各部品の先頭に付ける説明(docstring)。
PART_DOCS = {
    "_version": "プラグインの版。版の唯一の置き場所(ここだけを書き換える)。",
    "_qt": "Qt の import を集約する(PySide2 / PySide6 の切り替え)。",
    "_base": (
        "設定の読み書きとロック、ログ、履歴、ステージング掃除、クラウド同期の検出、\n"
        "原子的なファイル書き込み、書き出し結果ファイル、メッシュ記録、\n"
        "リベースの依頼 ID と結果ファイルのパス。"
    ),
    "_project_key": "現在のプロジェクトのキーと、Final サブフォルダ名。",
    "_settings_model": "設定画面の未保存判定などの純関数(Painter / Qt に依存しない)。",
    "_rebase_protocol": (
        "リベースの定数と文言、依頼・状態ファイルの解析と書き込み(Painter / Qt に依存しない)。"
    ),
    "_rebase_policy": (
        "リベースの承認判定、メッシュ事前確認、Painter 状態、バックアップ名、再試行、\n"
        "承認文言(Painter / Qt に依存しない)。"
    ),
    "_rebase_uvfill": (
        "UV 投影フィルの列挙・切り替え・巻き戻し。Painter の API は引数で受ける\n"
        "(Painter / Qt に依存しない)。"
    ),
    "_rebase_job": "リベース 1 件ぶんの実行(ExportAttempt / RebaseReporter / RebaseJob)。",
    "_rebase_coordinator": "リベースの司令塔(RebaseCoordinator)。",
    "_painter_rebase_api": (
        "リベースが使う Painter API、承認ダイアログ、エンジン用フック、\n"
        "UV フィル API の組み立て。"
    ),
    "_live_engine": "同期エンジン(LiveSyncEngine)。",
    "_wizard": "初回セットアップウィザード(SetupWizard)。",
    "_live_panel": "ドックパネル(LiveSyncPanel)。",
}

# 部品名 -> その部品へ移すトップレベル名(元のファイルでの並び順)。
# __init__ に残る名前も含める。import 文は含めない(使う部品へ自動で付く)。
PARTS = {
    "_version": [
        "__version__",
    ],
    "_qt": [
        "QtCore", "QtGui", "QtWidgets",
    ],
    "_base": [
        "msvcrt", "CONFIG_DIR", "CONFIG_PATH", "SYNC_RESULT_NAME", "SYNC_FLAG_NAME",
        "EXPORT_RESULT_SCHEMA", "EXPORT_MESSAGE_MAX_CHARS", "_HASH_CHUNK_BYTES",
        "MESH_SHA1_MAX_BYTES", "_EXPORT_STATUS_TOKENS", "_EXPORT_STATUS_LABELS",
        "DEFAULT_CONFIG", "_log", "HISTORY_LOG_PATH", "_HISTORY_MAX_BYTES",
        "_append_history", "_cleanup_orphaned_staging_dirs", "_CLOUD_SYNC_TEMP_MARKERS",
        "_CLOUD_SYNC_PATH_MARKERS", "_detect_cloud_sync_risk", "load_config",
        "CONFIG_LOCK_PATH", "_CONFIG_LOCK_TIMEOUT_SECONDS",
        "_CONFIG_LOCK_POLL_INTERVAL_SECONDS", "_ConfigLockTimeout", "_config_file_lock",
        "_merge_and_write_config", "save_config", "_update_nested_config_entry",
        "_status_token", "_match_known_export_status", "export_status_name",
        "export_status_label", "build_export_result", "write_json_atomic",
        "write_sync_flag", "publish_export_outputs", "write_rebase_result_copy",
        "_hash_file_hex", "build_mesh_record", "_is_unsaved_project_key",
        "REBASE_DIRNAME", "REBASE_RESULTS_DIRNAME", "REBASE_REQUEST_ID_RE",
        "is_valid_rebase_request_id", "_checked_rebase_request_id",
        "rebase_results_dir", "rebase_result_path",
    ],
    "_project_key": [
        "_current_project_key", "_project_subfolder_name",
    ],
    "_settings_model": [
        "RESOLUTION_CHOICES", "_duplicate_folder_warning", "PANEL_SETTING_KEYS",
        "_PANEL_SETTING_LABELS", "_PANEL_PATH_KEYS", "_PANEL_INT_DEFAULTS",
        "_PANEL_FLOAT_DEFAULTS", "unsaved_setting_keys", "_panel_setting_matches",
        "_panel_paths_match",
    ],
    "_rebase_protocol": [
        "REBASE_SCHEMA", "REBASE_REQUESTS_DIRNAME", "REBASE_STATUS_DIRNAME",
        "REBASE_STATES", "REBASE_TERMINAL_STATES", "REBASE_CANCELABLE_STATES",
        "REBASE_LOCKED_STATES", "REBASE_REASONS", "REBASE_APPROVAL_ONLY_REASONS",
        "REBASE_MESSAGE_MAX_CHARS", "DEFAULT_UVREMAP_ROOT_DIR", "REBASE_BACKUP_DIRNAME",
        "REBASE_RELOAD_EXPECTED", "REBASE_BASELINE_ID_RE", "BACKUP_INCLUDES_UNSAVED",
        "REBASE_UV_OLD_NAME_RE", "REBASE_UV_OLD_NAME_MAX", "REBASE_UV_OLD_INDEX_MIN",
        "REBASE_UV_OLD_INDEX_MAX", "_REBASE_SHA1_RE", "_REBASE_STATE_SET",
        "_REBASE_TERMINAL", "_REBASE_REASON_SET", "_REBASE_STATUS_REASON_SET",
        "_REBASE_REQUEST_KEYS", "_REBASE_WIN_BAD_FILENAME", "_REBASE_WAIT_MAX_MIN_S",
        "_REBASE_WAIT_MAX_MAX_S", "_REBASE_TRANSITIONS", "_PAINTER_STATE_DEFAULTS",
        "_PAINTER_STATE_KEYS", "_REBASE_UV_FILLS_KEYS", "ApprovalDecision",
        "RebaseScan", "RebaseScanDiff", "REBASE_INTERRUPTED_MESSAGE",
        "REBASE_REASON_MESSAGES", "_REBASE_BACKUP_MESSAGE_REASONS",
        "REBASE_DIALOG_TITLE", "REBASE_CONFIRM_LEAD", "REBASE_CAUTION_TEXT",
        "REBASE_CAUTION_TEXT_REPROJECT", "REBASE_UNSAVED_NOTE",
        "REBASE_AUTO_UNWRAP_NOTE", "REBASE_WORKFLOW_GUIDANCE", "REBASE_DONE_GUIDANCE",
        "REBASE_DIRECTION_APPROVAL_NOTE", "REBASE_DIRECTION_WARNING_FMT",
        "REBASE_WAIT_MESSAGES", "REBASE_BUTTON_RUN", "REBASE_BUTTON_CANCEL",
        "_rebase_is_finite_number", "_rebase_filename_only", "_rebase_clip_message",
        "_rebase_optional_text", "_rebase_copy_reload",
        "normalize_project_key_for_storage", "normalize_project_key_for_compare",
        "project_keys_match", "uvremap_root_for_project", "resolve_uvremap_root",
        "rebase_requests_dir", "rebase_status_dir", "rebase_request_path",
        "rebase_cancel_path", "rebase_status_path", "_rebase_coerce_float",
        "_rebase_coerce_retry_max", "_rebase_coerce_bool", "normalize_rebase_settings",
        "effective_rebase_approval", "read_json_safe", "_parse_rebase_mesh",
        "_parse_rebase_ts_names", "_rebase_reload_problem", "_parse_rebase_uv_old",
        "parse_rebase_request", "read_rebase_request_with_reason",
        "read_rebase_request", "scan_rebase_dir", "diff_rebase_scan",
        "find_superseded_ids", "_normalize_uv_fills", "build_rebase_status",
        "parse_rebase_status", "read_rebase_status", "is_valid_rebase_transition",
        "write_rebase_status", "build_interrupted_status", "rebase_failure_message",
        "_painter_state_get", "REBASE_EXPORT_DEFERRED_STATUS",
        "REBASE_UV_FILL_PROGRESS", "REBASE_AUTO_UNWRAP_UNSUPPORTED_LOG",
        "REBASE_TERMINAL_WRITE_MAX", "rebase_reject_state", "get_uv_old_index",
        "get_uv_old_name",
    ],
    "_rebase_policy": [
        "needs_saving_for_rebase", "decide_approval", "wait_timed_out", "mesh_precheck",
        "_REBASE_PRECHECK_CACHE_MAX", "RebasePrecheckCaches",
        "supports_auto_unwrap_settings", "compute_last_edit_age", "build_painter_state",
        "_rebase_safe_stem", "rebase_backup_path", "rebase_retry_delay",
        "classify_reload_status", "build_reload_settings_kwargs",
        "build_rebase_approval_text", "rebase_wait_message",
    ],
    "_rebase_uvfill": [
        "_UV_FILL_PARAM_ATTRS", "_UV_FILL_GATE_MESSAGES", "UvFillApi",
        "node_type_token", "_safe_call", "uv_fill_has_direction_channel",
        "_uv_fill_direction_state", "_direction_fill_name",
        "inspect_direction_uv_fills", "_iter_named_children",
        "_node_has_projection_api", "_uv_fill_visit_key", "iter_uv_fill_nodes",
        "switch_uv_fill_node", "rollback_uv_fill_node", "UvFillStackReport",
        "_remember_missing_attrs", "switch_uv_fills_in_stack", "UvFillSummary",
        "_texture_set_name", "_texture_set_key", "_iter_texture_set_stacks",
        "switch_uv_fills_for_project",
    ],
    "_rebase_job": [
        "ExportAttempt", "RebaseReporter", "RebaseJob", "_is_project_error",
    ],
    "_rebase_coordinator": [
        "_REBASE_CFG_RELOAD_S", "RebaseCoordinator", "_iter_status_ids",
    ],
    "_painter_rebase_api": [
        "make_uv_fill_api", "layerstack", "RebaseEngineHooks", "PainterRebaseApi",
        "approval_action_from_role", "RebaseApprovalDialogHost",
        "make_rebase_approval_dialog", "default_rebase_job_factory",
    ],
    "_live_engine": [
        "_FINAL_EXPORT_INITIAL_DELAY_MS", "_FINAL_EXPORT_QUEUED_DELAY_MS",
        "_FINAL_EXPORT_RETRY_BASE_MS", "_FINAL_EXPORT_RETRY_MAX_MS",
        "_FINAL_EXPORT_MAX_RETRIES", "_FINAL_EXPORT_GIVEUP_FOLLOWUP_MS",
        "_MESH_RECORD_DELAY_MS", "_MESH_RECORD_RETRY_DELAY_MS",
        "_MESH_RECORD_MAX_RETRIES", "CHANNEL_SUFFIXES", "LiveSyncEngine",
    ],
    "_wizard": [
        "SetupWizard",
    ],
    "_live_panel": [
        "LiveSyncPanel",
    ],
    "__init__": [
        "_engine", "_panel", "STALE_SINGLE_FILE_PLUGIN_WARNING",
        "stale_single_file_plugin_path", "start_plugin", "close_plugin",
    ],
}


# 分割前の __init__.py のトップレベル名のうち、所有する部品が表のとおりでなく
# 「__init__ が互換のために持ち続ける」だけの名前(標準ライブラリ等)は、
# split_plugin.py が元の import 文から自動で扱う。ここには書かない。


def name_to_part():
    """名前 -> 部品 の辞書を返す。同じ名前が 2 つの部品にあればエラー。"""
    table = {}
    for part, names in PARTS.items():
        for name in names:
            if name in table:
                raise ValueError("名前が重複しています: {0} ({1}, {2})".format(
                    name, table[name], part))
            table[name] = part
    unknown = sorted(set(PARTS) - set(LAYERS))
    if unknown:
        raise ValueError("層の表に無い部品: {0}".format(unknown))
    return table
