"""
anypath.core.config  -  AnyPath Core: 設定の階層マージとスキーマ検証/強制クランプ
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §6.3（設定の階層と項目 ―― 値の強制クランプ）

優先度の低い順にマージする（後者が前者を上書き）:
    1. 既定値（コード内定数）
    2. プロジェクト設定 -- プロジェクトルート内 anypath/config.json
       （マッピング表・アンカー名）
    3. マシン固有設定 -- <ユーザーの Documents>/maya/anypath/machine.json
       （探索ルート）
    4. 環境変数による上書き -- CI／バッチ用

値の強制クランプ（実装必須）: 設定ファイルの編集によって P1（誤リンクゼロ）を
破壊できてはならない。以下をコード側で強制する。

    ambiguity_margin   : 0.2 〜 1.0（0を指定しても0.2として扱う）
    index_timeout_sec  : 3 〜 120
    search_roots       : 最大8件（超過分は無視）
    anchor_folders     : 最大16件（超過分は無視）
    mode               : 列挙値のみ（不正値は auto として扱う）

設定ファイルの JSON パースエラーで機能を停止させてはならない。スキーマ検証を
行い、不正項目は既定値へフォールバックし、そのことを自己診断画面に明示する
（このモジュールは ClampReport / load report を通じて「何がクランプ/フォール
バックされたか」を呼び出し側（Shell層の自己診断画面）へ伝達する）。

**Maya API を一切 import しない**（§2.1）。ファイルI/O・環境変数の読み取りは
行うが、Maya固有のパス取得（cmds.internalVar等）は呼び出し側（Boot/Bridge層）
の責務とする。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §6.3 に基づき新規作成。
"""

__version__ = "1.0.0"

import json
import os

# --- 既定値（コード内定数） -------------------------------------------------
# 設計書 §6.2: 「この切り替えは、実装上はインストーラが書き出す初期設定ファイル
# の値のみを変える（コードの既定値は auto とする）」。
DEFAULT_CONFIG = {
    "mode": "auto",
    "search_roots": [],
    "path_mappings": [],
    "anchor_folders": ["sourceimages", "assets", "textures", "cache"],
    "ambiguity_margin": 0.4,
    "index_timeout_sec": 15,
    "exclude_patterns": [".git", "incrementalSave", "__pycache__", "autosave", ".mayaSwatches"],
    "node_registry": {},
    "log_level": "INFO",
}

VALID_MODES = ("auto", "report_only", "off")

# --- クランプ範囲 -----------------------------------------------------------
AMBIGUITY_MARGIN_MIN = 0.2
AMBIGUITY_MARGIN_MAX = 1.0
INDEX_TIMEOUT_MIN = 3
INDEX_TIMEOUT_MAX = 120
SEARCH_ROOTS_MAX = 8
ANCHOR_FOLDERS_MAX = 16

# 環境変数による上書き（CI/バッチ用）のキー対応表。
# 値は文字列として渡ってくるため、型変換を行った上でクランプ処理へ渡す。
_ENV_OVERRIDE_KEYS = {
    "ANYPATH_MODE": "mode",
    "ANYPATH_AMBIGUITY_MARGIN": "ambiguity_margin",
    "ANYPATH_INDEX_TIMEOUT_SEC": "index_timeout_sec",
}


class ClampEvent:
    """1件のクランプ/フォールバック発生を表す（自己診断画面への伝達用）。"""
    __slots__ = ("field", "original_value", "clamped_value", "reason")

    def __init__(self, field, original_value, clamped_value, reason):
        self.field = field
        self.original_value = original_value
        self.clamped_value = clamped_value
        self.reason = reason

    def __repr__(self):
        return "ClampEvent(field={0!r}, {1!r} -> {2!r}, reason={3!r})".format(
            self.field, self.original_value, self.clamped_value, self.reason)

    def to_dict(self):
        return {
            "field": self.field,
            "original_value": self.original_value,
            "clamped_value": self.clamped_value,
            "reason": self.reason,
        }


class ConfigLoadReport:
    """
    設定読込1回分の結果報告。§6.9 自己診断画面の「設定の読込結果」項目が
    参照する想定のデータ構造。

    sources: 実際に読み込めたファイルパスの一覧（読めなかった/存在しない
             ものは含まない）。
    parse_errors: (path, error_message) のリスト。JSONパースに失敗した
             ファイルがあれば記録し、そのファイルの内容は無視して既定値へ
             フォールバックする。
    clamp_events: 発生したクランプ/フォールバックの一覧。
    """
    __slots__ = ("config", "sources", "parse_errors", "clamp_events")

    def __init__(self, config, sources, parse_errors, clamp_events):
        self.config = config
        self.sources = sources
        self.parse_errors = parse_errors
        self.clamp_events = clamp_events

    @property
    def had_any_issue(self):
        return bool(self.parse_errors) or bool(self.clamp_events)


def _try_load_json_file(path):
    """
    JSONファイルを読み込む。存在しない場合は (None, None)。
    パースエラーの場合は (None, error_message)。
    成功時は (dict, None)。

    ファイル自体が存在しないことと、存在するが壊れていることを明確に
    区別する（前者は日常的に起こる正常系、後者はログ・自己診断へ
    警告として伝える対象）。
    """
    if not path or not os.path.isfile(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None, "設定ファイルの中身が JSON オブジェクトではありません: {0}".format(path)
        return data, None
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return None, "{0}: {1}".format(path, e)


def _coerce_env_value(key, raw_value):
    """環境変数の文字列値を、対応する設定項目の型へ変換する。変換不能ならNoneを返す。"""
    if key == "mode":
        return raw_value
    if key in ("ambiguity_margin",):
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return None
    if key in ("index_timeout_sec",):
        try:
            return int(float(raw_value))
        except (TypeError, ValueError):
            return None
    return raw_value


def _load_env_overrides(environ):
    """環境変数（辞書）から上書き値を抽出する。"""
    overrides = {}
    for env_key, config_key in sorted(_ENV_OVERRIDE_KEYS.items()):
        if env_key in environ:
            coerced = _coerce_env_value(config_key, environ[env_key])
            if coerced is not None:
                overrides[config_key] = coerced
    return overrides


def _merge_layer(base, layer):
    """
    layer を base の上に浅くマージする（トップレベルキー単位で上書き）。
    ネストした辞書（node_registry等）は丸ごと置き換える（部分マージしない。
    設計書に部分マージの明記がなく、暗黙の部分マージは設定の意図を
    読み違える危険があるため単純な上書きに倒す）。
    未知のキーは無視する（スキーマ外の項目が紛れ込んでも動作に影響させない）。
    """
    merged = dict(base)
    if not layer:
        return merged
    for key in DEFAULT_CONFIG:
        if key in layer:
            merged[key] = layer[key]
    return merged


def _clamp_ambiguity_margin(value, events):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        events.append(ClampEvent(
            "ambiguity_margin", value, DEFAULT_CONFIG["ambiguity_margin"],
            "数値として解釈できないため既定値を使用"))
        return DEFAULT_CONFIG["ambiguity_margin"]
    if numeric < AMBIGUITY_MARGIN_MIN or numeric > AMBIGUITY_MARGIN_MAX:
        clamped = min(max(numeric, AMBIGUITY_MARGIN_MIN), AMBIGUITY_MARGIN_MAX)
        events.append(ClampEvent(
            "ambiguity_margin", numeric, clamped,
            "許容範囲 {0}〜{1} 外のためクランプ（P1保護: 0付近は曖昧な候補の"
            "自動適用を招くため許容しない）".format(AMBIGUITY_MARGIN_MIN, AMBIGUITY_MARGIN_MAX)))
        return clamped
    return numeric


def _clamp_index_timeout(value, events):
    try:
        numeric = int(float(value))
    except (TypeError, ValueError):
        events.append(ClampEvent(
            "index_timeout_sec", value, DEFAULT_CONFIG["index_timeout_sec"],
            "数値として解釈できないため既定値を使用"))
        return DEFAULT_CONFIG["index_timeout_sec"]
    if numeric < INDEX_TIMEOUT_MIN or numeric > INDEX_TIMEOUT_MAX:
        clamped = min(max(numeric, INDEX_TIMEOUT_MIN), INDEX_TIMEOUT_MAX)
        events.append(ClampEvent(
            "index_timeout_sec", numeric, clamped,
            "許容範囲 {0}〜{1} 外のためクランプ".format(INDEX_TIMEOUT_MIN, INDEX_TIMEOUT_MAX)))
        return clamped
    return numeric


def _clamp_list_max(value, field_name, max_items, events):
    if not isinstance(value, list):
        events.append(ClampEvent(
            field_name, value, DEFAULT_CONFIG[field_name],
            "配列として解釈できないため既定値を使用"))
        return list(DEFAULT_CONFIG[field_name])
    if len(value) > max_items:
        clamped = value[:max_items]
        events.append(ClampEvent(
            field_name, "{0}件".format(len(value)), "{0}件".format(max_items),
            "最大{0}件を超過したため超過分を無視".format(max_items)))
        return clamped
    return value


def _clamp_mode(value, events):
    if value in VALID_MODES:
        return value
    events.append(ClampEvent(
        "mode", value, "auto",
        "不正な値のため既定モード auto として扱う"))
    return "auto"


def apply_clamps(merged_config):
    """
    マージ済み設定にクランプを適用する。戻り値: (clamped_config, [ClampEvent, ...])
    """
    events = []
    result = dict(merged_config)

    result["ambiguity_margin"] = _clamp_ambiguity_margin(
        result.get("ambiguity_margin", DEFAULT_CONFIG["ambiguity_margin"]), events)
    result["index_timeout_sec"] = _clamp_index_timeout(
        result.get("index_timeout_sec", DEFAULT_CONFIG["index_timeout_sec"]), events)
    result["search_roots"] = _clamp_list_max(
        result.get("search_roots", []), "search_roots", SEARCH_ROOTS_MAX, events)
    result["anchor_folders"] = _clamp_list_max(
        result.get("anchor_folders", DEFAULT_CONFIG["anchor_folders"]),
        "anchor_folders", ANCHOR_FOLDERS_MAX, events)
    result["mode"] = _clamp_mode(result.get("mode", DEFAULT_CONFIG["mode"]), events)

    # path_mappings のスキーマ検証: [{from, to}, ...] の形式でないエントリは除外する。
    raw_mappings = result.get("path_mappings", [])
    if isinstance(raw_mappings, list):
        valid_mappings = []
        dropped = 0
        for entry in raw_mappings:
            if isinstance(entry, dict) and "from" in entry and "to" in entry:
                valid_mappings.append({"from": entry["from"], "to": entry["to"]})
            else:
                dropped += 1
        if dropped:
            events.append(ClampEvent(
                "path_mappings", "{0}件不正".format(dropped), "{0}件除外".format(dropped),
                "from/to を持たないエントリを除外"))
        result["path_mappings"] = valid_mappings
    else:
        events.append(ClampEvent(
            "path_mappings", raw_mappings, [], "配列として解釈できないため空配列を使用"))
        result["path_mappings"] = []

    if not isinstance(result.get("node_registry"), dict):
        events.append(ClampEvent(
            "node_registry", result.get("node_registry"), {},
            "オブジェクトとして解釈できないため既定値(空)を使用"))
        result["node_registry"] = {}

    if not isinstance(result.get("exclude_patterns"), list):
        events.append(ClampEvent(
            "exclude_patterns", result.get("exclude_patterns"),
            list(DEFAULT_CONFIG["exclude_patterns"]),
            "配列として解釈できないため既定値を使用"))
        result["exclude_patterns"] = list(DEFAULT_CONFIG["exclude_patterns"])

    return result, events


def load_layered_config(project_config_path=None, machine_config_path=None,
                         environ=None):
    """
    設定を4階層でマージし、スキーマ検証・強制クランプを適用して返す。

    優先度（低→高）: 既定値 < プロジェクト設定 < マシン固有設定 < 環境変数。

    戻り値: ConfigLoadReport
    """
    if environ is None:
        environ = os.environ

    sources = []
    parse_errors = []

    merged = dict(DEFAULT_CONFIG)

    project_data, project_err = _try_load_json_file(project_config_path)
    if project_err:
        parse_errors.append((project_config_path, project_err))
    elif project_data is not None:
        sources.append(project_config_path)
        merged = _merge_layer(merged, project_data)

    machine_data, machine_err = _try_load_json_file(machine_config_path)
    if machine_err:
        parse_errors.append((machine_config_path, machine_err))
    elif machine_data is not None:
        sources.append(machine_config_path)
        merged = _merge_layer(merged, machine_data)

    env_overrides = _load_env_overrides(environ)
    if env_overrides:
        sources.append("environment")
        merged = _merge_layer(merged, env_overrides)

    clamped, clamp_events = apply_clamps(merged)

    return ConfigLoadReport(
        config=clamped,
        sources=sources,
        parse_errors=parse_errors,
        clamp_events=clamp_events,
    )
