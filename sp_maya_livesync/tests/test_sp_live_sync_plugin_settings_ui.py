# -*- coding: utf-8 -*-
"""sp_live_sync_plugin 1.3.1 の未保存判定(純関数)。

Painter と Qt は既存テストが sys.modules に入れた偽物を再利用する。
画面の挙動(ダイアログ・ラベル)は自動テストの対象外。
要実機確認(Painter 12.1.5)
"""
from __future__ import annotations

import os
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_TESTS)
for _p in (_PKG, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_sp_live_sync_plugin_protocol import plugin  # noqa: E402


def _saved(**over):
    values = {
        "staging_dir": "C:/SPMayaLiveSync/staging",
        "watch_dir": "C:/SPMayaLiveSync/live",
        "final_export_dir": "C:/SPMayaLiveSync/final",
        "preview_resolution_log2": 9,
        "final_resolution_log2": 12,
        "debounce_seconds": 1.5,
        "final_cooldown_seconds": 5.0,
    }
    values.update(over)
    return values


def _patch_windows_like_path(monkeypatch):
    """normcase / 区切り文字の差を OS に依存せず検証する。"""

    def normpath(path):
        text = str(path).replace("/", "\\")
        while "\\\\" in text:
            text = text.replace("\\\\", "\\")
        if len(text) > 1 and text.endswith("\\"):
            text = text.rstrip("\\")
        return text

    def normcase(path):
        return str(path).lower()

    monkeypatch.setattr(plugin.os.path, "normpath", normpath)
    monkeypatch.setattr(plugin.os.path, "normcase", normcase)


def test_all_matching_values_are_clean():
    assert plugin.unsaved_setting_keys(_saved(), _saved()) == []
    empty = _saved(staging_dir="", watch_dir="", final_export_dir="")
    assert plugin.unsaved_setting_keys(empty, empty) == []


def test_folder_cosmetic_differences_are_not_unsaved(monkeypatch):
    _patch_windows_like_path(monkeypatch)
    saved = _saved()
    panel = _saved(
        staging_dir="  C:/SPMayaLiveSync/staging  ",
        watch_dir="C:\\SPMayaLiveSync\\live",
        final_export_dir="c:/spmayalivesync/final",
    )
    assert plugin.unsaved_setting_keys(panel, saved) == []
    blank = _saved(staging_dir="  ", watch_dir=" \t ", final_export_dir="")
    empty = _saved(staging_dir="", watch_dir="", final_export_dir="")
    assert plugin.unsaved_setting_keys(blank, empty) == []
    other = _saved(watch_dir="C:/other")
    assert plugin.unsaved_setting_keys(other, saved) == ["watch_dir"]


def test_preview_resolution_only_is_dirty():
    panel = _saved(preview_resolution_log2=10)
    assert plugin.unsaved_setting_keys(panel, _saved()) == ["preview_resolution_log2"]


def test_missing_saved_keys_use_load_defaults():
    folders = {
        "staging_dir": "C:/a",
        "watch_dir": "C:/b",
        "final_export_dir": "C:/c",
    }
    panel_default = _saved()
    panel_default.update(folders)
    assert plugin.unsaved_setting_keys(panel_default, folders) == []

    panel_preview = dict(panel_default)
    panel_preview["preview_resolution_log2"] = 8
    assert plugin.unsaved_setting_keys(panel_preview, folders) == ["preview_resolution_log2"]

    panel_final = dict(panel_default)
    panel_final["final_resolution_log2"] = 13
    assert plugin.unsaved_setting_keys(panel_final, folders) == ["final_resolution_log2"]

    panel_debounce = dict(panel_default)
    panel_debounce["debounce_seconds"] = 2.0
    assert plugin.unsaved_setting_keys(panel_debounce, folders) == ["debounce_seconds"]

    panel_cooldown = dict(panel_default)
    panel_cooldown["final_cooldown_seconds"] = 9.0
    assert plugin.unsaved_setting_keys(panel_cooldown, folders) == ["final_cooldown_seconds"]


def test_float_epsilon_matches_and_point_one_is_dirty():
    saved = _saved()
    assert plugin.unsaved_setting_keys(
        _saved(debounce_seconds=1.5 + 1e-9), saved) == []
    assert plugin.unsaved_setting_keys(
        _saved(final_cooldown_seconds=5.0 + 1e-9), saved) == []
    assert plugin.unsaved_setting_keys(
        _saved(debounce_seconds=1.5 + 1e-6), saved) == []
    assert plugin.unsaved_setting_keys(
        _saved(debounce_seconds=1.6), saved) == ["debounce_seconds"]
    assert plugin.unsaved_setting_keys(
        _saved(final_cooldown_seconds=5.1), saved) == ["final_cooldown_seconds"]


def test_unconvertible_values_are_dirty_without_raising():
    saved = _saved()
    cases = (
        ("preview_resolution_log2", None),
        ("preview_resolution_log2", "abc"),
        ("final_resolution_log2", "abc"),
        ("debounce_seconds", None),
        ("debounce_seconds", "abc"),
        ("final_cooldown_seconds", "abc"),
    )
    for key, bad in cases:
        panel = _saved()
        panel[key] = bad
        result = plugin.unsaved_setting_keys(panel, saved)
        assert key in result
        saved_bad = _saved()
        saved_bad[key] = bad
        result_saved = plugin.unsaved_setting_keys(_saved(), saved_bad)
        assert key in result_saved


def test_dirty_keys_follow_panel_setting_key_order():
    panel = _saved(
        staging_dir="C:/x1",
        watch_dir="C:/x2",
        final_export_dir="C:/x3",
        preview_resolution_log2=8,
        final_resolution_log2=13,
        debounce_seconds=9.0,
        final_cooldown_seconds=0.0,
    )
    assert plugin.unsaved_setting_keys(panel, _saved()) == list(plugin.PANEL_SETTING_KEYS)
    subset = _saved(staging_dir="C:/other", final_cooldown_seconds=0.0)
    assert plugin.unsaved_setting_keys(subset, _saved()) == [
        "staging_dir",
        "final_cooldown_seconds",
    ]
