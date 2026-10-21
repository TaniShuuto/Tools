"""設定画面の未保存判定などの純関数(Painter / Qt に依存しない)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os

__all__ = [
    "RESOLUTION_CHOICES",
    "_duplicate_folder_warning",
    "PANEL_SETTING_KEYS",
    "_PANEL_SETTING_LABELS",
    "_PANEL_PATH_KEYS",
    "_PANEL_INT_DEFAULTS",
    "_PANEL_FLOAT_DEFAULTS",
    "unsaved_setting_keys",
    "_panel_setting_matches",
    "_panel_paths_match",
]


RESOLUTION_CHOICES = [
    ("256 px", 8),
    ("512 px", 9),
    ("1024 px", 10),
    ("2048 px", 11),
    ("4096 px", 12),
    ("8192 px", 13),
]


def _duplicate_folder_warning(staging_dir, watch_dir, final_export_dir):
    """Phase 3 最適化: staging/watch/final の3フォルダが誤って同じ
    パスに設定されていないかを確認する。特にstaging_dirは書き出しごとに
    一時フォルダを作成・削除するため、watch_dirやfinal_export_dirと
    同じ場所に設定すると、Maya側の監視イベントが不必要に多発したり、
    最終出力ファイルが誤って削除されたりする恐れがある。
    問題があれば警告文を返し、無ければNoneを返す。
    """
    pairs = [
        ("ステージングフォルダ", os.path.normpath(staging_dir)),
        ("監視フォルダ", os.path.normpath(watch_dir)),
        ("最終出力フォルダ", os.path.normpath(final_export_dir)),
    ]
    seen = {}
    for label, path in pairs:
        if path in seen:
            return (
                "「{0}」と「{1}」に同じフォルダが指定されています。"
                "意図しない挙動(監視イベントの多発、ファイルの誤削除等)を"
                "避けるため、それぞれ別のフォルダを指定することを推奨します。"
                "続行しますか?".format(seen[path], label)
            )
        seen[path] = label
    return None


# ---------------------------------------------------------------------------
# 2026.10.19(1.3.1): 画面設定の未保存判定(純関数)
# Qt / Painter に依存しない。書き出し処理には使わない。
# ---------------------------------------------------------------------------

PANEL_SETTING_KEYS = (
    "staging_dir", "watch_dir", "final_export_dir",
    "preview_resolution_log2", "final_resolution_log2",
    "debounce_seconds", "final_cooldown_seconds",
)

# 画面の日本語ラベル。未保存ラベルのツールチップに並べる。
# 折りたたみ設定グループの行ラベルと揃える。
_PANEL_SETTING_LABELS = {
    "staging_dir": "ステージングフォルダ",
    "watch_dir": "監視フォルダ(プレビュー)",
    "final_export_dir": "最終出力フォルダ(保存時)",
    "preview_resolution_log2": "プレビュー解像度",
    "final_resolution_log2": "最終出力解像度(高画質)",
    "debounce_seconds": "デバウンス間隔",
    "final_cooldown_seconds": "Final書き出しのクールダウン",
}

_PANEL_PATH_KEYS = ("staging_dir", "watch_dir", "final_export_dir")
_PANEL_INT_DEFAULTS = {
    "preview_resolution_log2": 9,
    "final_resolution_log2": 12,
}
_PANEL_FLOAT_DEFAULTS = {
    "debounce_seconds": 1.5,
    "final_cooldown_seconds": 5.0,
}


def unsaved_setting_keys(panel_values, saved_config):
    """画面の値と保存済みの値を比べ、食い違うキーを PANEL_SETTING_KEYS の順で返す。"""
    panel_map = panel_values if isinstance(panel_values, dict) else {}
    saved_map = saved_config if isinstance(saved_config, dict) else {}
    dirty = []
    for key in PANEL_SETTING_KEYS:
        if not _panel_setting_matches(key, panel_map, saved_map):
            dirty.append(key)
    return dirty


def _panel_setting_matches(key, panel_map, saved_map):
    try:
        if key in _PANEL_PATH_KEYS:
            return _panel_paths_match(panel_map.get(key), saved_map.get(key))
        if key in _PANEL_INT_DEFAULTS:
            panel_n = int(panel_map[key])
            if key not in saved_map:
                saved_n = _PANEL_INT_DEFAULTS[key]
            else:
                saved_n = int(saved_map[key])
            return panel_n == saved_n
        if key in _PANEL_FLOAT_DEFAULTS:
            panel_n = float(panel_map[key])
            if key not in saved_map:
                saved_n = _PANEL_FLOAT_DEFAULTS[key]
            else:
                saved_n = float(saved_map[key])
            return abs(panel_n - saved_n) <= 1e-6
    except (TypeError, ValueError, KeyError, AttributeError, OSError):
        return False
    return False


def _panel_paths_match(panel_raw, saved_raw):
    panel_text = str(panel_raw).strip()
    saved_text = str(saved_raw).strip()
    if panel_text == "" and saved_text == "":
        return True
    if panel_text == "" or saved_text == "":
        return False
    return (
        os.path.normcase(os.path.normpath(panel_text))
        == os.path.normcase(os.path.normpath(saved_text))
    )
