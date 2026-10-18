# -*- coding: utf-8 -*-
"""uvremap_panel.py の単体テスト(Maya 非依存)。

StatusModel は Qt 無しで検証する。ウィジェットは PySide6 があるときだけ
`QT_QPA_PLATFORM=offscreen` で実行し、無ければ skip する。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

_MODULE_DIR = Path(__file__).resolve().parents[1]
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from uvremap_follow import LABEL_CHECK_PAINTER, LABEL_CHECK_UV  # noqa: E402
from uvremap_follow import fold_status_label_p2  # noqa: E402
from uvremap_panel import (  # noqa: E402
    COLOR_ATTENTION,
    COLOR_MUTED,
    COLOR_OK,
    COLOR_WARN,
    LABEL_DISABLED,
    LABEL_NO_INFO,
    LABEL_NO_PROJECT,
    LABEL_OVERALL_PREFIX,
    STALLED_MESSAGE,
    StatusModel,
    StatusView,
    color_for_state,
    empty_rows_label,
    overall_status_label,
)


def _write_status(path: Path, ts: dict) -> Path:
    path.write_text(
        json.dumps({"schema": 1, "ts": ts}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _entry(state: str, generation=7, updated="2026-10-16T10:15:00", message=""):
    return {
        "state": state,
        "generation": generation,
        "updated": updated,
        "message": message,
    }


# 既定 updated(10:15) の5分後。CONVERTING は停止扱いにならない。
_NOW_FIVE_MIN_AFTER_UPDATED = datetime(2026, 10, 16, 10, 20, 0)
# 既定 updated(10:15) の11分後。CONVERTING は停止扱いになる。
_NOW_ELEVEN_MIN_AFTER_UPDATED = datetime(2026, 10, 16, 10, 26, 0)


class TestStatusModel:
    def test_normal(self, tmp_path):
        status = _write_status(
            tmp_path / "status.json",
            {
                "Body": _entry("CONVERTED_OK", 7, "2026-10-16T10:15:00", ""),
                "Face": _entry("STALE", 3, "2026-10-16T09:00:00", "old uv"),
            },
        )
        view = StatusModel(status).load()
        assert view.available is True
        assert view.worst_state == "STALE"
        by_name = {row.ts_name: row for row in view.rows}
        assert set(by_name) == {"Body", "Face"}
        assert by_name["Body"].state == "CONVERTED_OK"
        assert by_name["Body"].generation == 7
        assert by_name["Body"].updated == "2026-10-16T10:15:00"
        assert by_name["Body"].message == ""
        assert by_name["Face"].state == "STALE"
        assert by_name["Face"].message == "old uv"
        assert by_name["Body"].stalled is False

    def test_missing_file(self, tmp_path):
        view = StatusModel(tmp_path / "missing.json").load()
        assert view.available is False
        assert view.rows == ()
        assert view.worst_state == ""

    def test_broken_json(self, tmp_path):
        path = tmp_path / "status.json"
        path.write_text('{"schema": 1, "ts": {"Body":', encoding="utf-8")
        view = StatusModel(path).load()
        assert view.available is False
        assert view.rows == ()

    def test_empty_file(self, tmp_path):
        path = tmp_path / "status.json"
        path.write_text("", encoding="utf-8")
        view = StatusModel(path).load()
        assert view.available is False
        assert view.rows == ()

    def test_unknown_state_name(self, tmp_path):
        status = _write_status(
            tmp_path / "status.json",
            {"Body": _entry("WEIRD_STATE", 1, "2026-10-16T10:15:00", "??")},
        )
        view = StatusModel(status).load()
        assert view.available is True
        assert view.rows[0].state == "WEIRD_STATE"
        assert view.worst_state == "WEIRD_STATE"
        assert color_for_state("WEIRD_STATE") == COLOR_ATTENTION

    def test_converting_stalled_after_default_timeout(self, tmp_path):
        now = datetime(2026, 10, 16, 12, 0, 0)
        status = _write_status(
            tmp_path / "status.json",
            {"Body": _entry("CONVERTING", 8, "2026-10-16T11:49:00", "")},
        )
        view = StatusModel(status).load(now=now)
        row = view.rows[0]
        assert row.state == "CONVERTING"
        assert row.stalled is True
        assert STALLED_MESSAGE in row.message
        assert row.color == COLOR_WARN
        assert view.worst_color == COLOR_WARN

    def test_converting_recent_is_not_stalled(self, tmp_path):
        now = datetime(2026, 10, 16, 12, 0, 0)
        status = _write_status(
            tmp_path / "status.json",
            {"Body": _entry("CONVERTING", 8, "2026-10-16T11:51:00", "working")},
        )
        view = StatusModel(status).load(now=now)
        row = view.rows[0]
        assert row.stalled is False
        assert row.message == "working"
        assert row.color == COLOR_ATTENTION

    def test_converting_unparseable_updated_is_stalled(self, tmp_path):
        now = datetime(2026, 10, 16, 12, 0, 0)
        status = _write_status(
            tmp_path / "status.json",
            {"Body": _entry("CONVERTING", 8, "not-a-date", "")},
        )
        view = StatusModel(status).load(now=now)
        assert view.rows[0].stalled is True

    def test_worst_state_order(self, tmp_path):
        cases = [
            ({"A": "FAILED", "B": "ORIGINAL"}, "FAILED"),
            ({"A": "BLOCKED", "B": "STALE"}, "BLOCKED"),
            ({"A": "STALE", "B": "CONVERTED_OK"}, "STALE"),
            ({"A": "CONVERTING", "B": "CONVERTED_OK"}, "CONVERTING"),
            ({"A": "CONVERTED_OK", "B": "ORIGINAL"}, "CONVERTED_OK"),
            ({"A": "ORIGINAL"}, "ORIGINAL"),
            ({"A": "FAILED", "B": "BLOCKED"}, "FAILED"),
            ({"A": "STALE", "B": "CONVERTING"}, "STALE"),
            ({"A": "FAILED", "B": "CONVERTING", "C": "ORIGINAL"}, "FAILED"),
        ]
        for states, expected in cases:
            status = _write_status(
                tmp_path / "status.json",
                {name: _entry(state) for name, state in states.items()},
            )
            view = StatusModel(status).load(now=_NOW_FIVE_MIN_AFTER_UPDATED)
            assert view.worst_state == expected, states

    def test_converting_five_minutes_later_is_not_stalled_stale_wins(self, tmp_path):
        updated = "2026-10-16T10:15:00"
        status = _write_status(
            tmp_path / "status.json",
            {
                "A": _entry("CONVERTING", 1, updated, ""),
                "B": _entry("STALE", 2, updated, ""),
            },
        )
        view = StatusModel(
            status, now_fn=lambda: _NOW_FIVE_MIN_AFTER_UPDATED
        ).load()
        by_name = {row.ts_name: row for row in view.rows}
        assert by_name["A"].stalled is False
        assert by_name["A"].color == COLOR_ATTENTION
        assert STALLED_MESSAGE not in by_name["A"].message
        assert view.worst_state == "STALE"
        assert view.worst_color == COLOR_ATTENTION

    def test_converting_eleven_minutes_later_is_stalled_warn(self, tmp_path):
        updated = "2026-10-16T10:15:00"
        status = _write_status(
            tmp_path / "status.json",
            {"A": _entry("CONVERTING", 1, updated, "working")},
        )
        view = StatusModel(
            status, now_fn=lambda: _NOW_ELEVEN_MIN_AFTER_UPDATED
        ).load()
        row = view.rows[0]
        assert row.state == "CONVERTING"
        assert row.stalled is True
        assert row.color == COLOR_WARN
        assert row.color == "#c94a2a"
        assert STALLED_MESSAGE in row.message
        assert view.worst_color == COLOR_WARN
        assert view.worst_color == "#c94a2a"

    def test_none_path_is_no_info(self):
        view = StatusModel(None).load()
        assert view.available is False
        assert view.worst_state == ""


def test_overall_label_no_project_when_enabled_and_path_is_none():
    view = StatusView(rows=(), worst_state="", available=False)
    text, color = overall_status_label(True, view, None)
    assert text == f"{LABEL_OVERALL_PREFIX}: {LABEL_NO_PROJECT}"
    assert text == "全体: プロジェクト未設定"
    assert color == COLOR_MUTED


def test_overall_label_missing_file_stays_no_info():
    view = StatusView(rows=(), worst_state="", available=False)
    text, color = overall_status_label(True, view, "status.json")
    assert text == f"{LABEL_OVERALL_PREFIX}: {LABEL_NO_INFO}"
    assert LABEL_NO_PROJECT not in text
    assert color == COLOR_MUTED


def test_empty_rows_label_uses_no_project_when_path_is_none():
    assert empty_rows_label(None) == LABEL_NO_PROJECT
    assert empty_rows_label(None) == "プロジェクト未設定"
    assert empty_rows_label("status.json") == LABEL_NO_INFO
    assert empty_rows_label("") == LABEL_NO_INFO


def test_overall_label_kill_switch_off_is_unchanged():
    view = StatusView(rows=(), worst_state="", available=False)
    text, color = overall_status_label(False, view, None)
    assert text == LABEL_DISABLED
    assert color == COLOR_MUTED
    ready = StatusView(rows=(), worst_state="FAILED", available=True)
    assert overall_status_label(False, ready, "status.json")[0] == LABEL_DISABLED


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"PySide6 を import できない: {exc}")

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_panel(qapp, **kwargs):
    from uvremap_panel import UVRemapPanel

    defaults = {
        "on_toggle_enabled": lambda _enabled: None,
        "on_convert_now": lambda: None,
        "on_revert": lambda: None,
        "get_enabled": lambda: False,
        "get_status_path": lambda: None,
    }
    defaults.update(kwargs)
    panel = UVRemapPanel(**defaults)
    panel.show()
    qapp.processEvents()
    return panel


class TestUVRemapPanel:
    def test_kill_switch_calls_callback(self, qapp):
        calls = []
        enabled = {"value": False}

        def on_toggle(value):
            calls.append(value)
            enabled["value"] = value

        panel = _make_panel(
            qapp,
            on_toggle_enabled=on_toggle,
            get_enabled=lambda: enabled["value"],
        )
        try:
            box = panel.findChild(type(panel._kill_switch), "kill_switch")
            assert box is not None
            assert box.isChecked() is False
            box.click()
            qapp.processEvents()
            assert calls == [True]
            assert box.isChecked() is True
            box.click()
            qapp.processEvents()
            assert calls == [True, False]
        finally:
            panel.hide()
            panel.close()

    def test_buttons_disabled_when_kill_switch_off(self, qapp):
        enabled = {"value": False}
        panel = _make_panel(qapp, get_enabled=lambda: enabled["value"])
        try:
            assert panel.findChild(type(panel._btn_convert), "btn_convert").isEnabled() is False
            assert panel.findChild(type(panel._btn_revert), "btn_revert").isEnabled() is False
            assert panel.findChild(type(panel._btn_refresh), "btn_refresh").isEnabled() is False
            assert LABEL_DISABLED in panel.findChild(type(panel._overall), "overall_status").text()

            enabled["value"] = True
            panel.refresh()
            qapp.processEvents()
            assert panel.findChild(type(panel._btn_convert), "btn_convert").isEnabled() is True
            assert panel.findChild(type(panel._btn_revert), "btn_revert").isEnabled() is True
            assert panel.findChild(type(panel._btn_refresh), "btn_refresh").isEnabled() is True
        finally:
            panel.hide()
            panel.close()

    def test_state_color_and_text(self, qapp, tmp_path):
        status = _write_status(
            tmp_path / "status.json",
            {
                "Body": _entry("CONVERTED_OK", 7, "2026-10-16T10:15:00", "ok"),
                "Face": _entry("FAILED", 3, "2026-10-16T10:00:00", "probe"),
                "Trim": _entry("ORIGINAL", 0, "2026-10-16T08:00:00", ""),
            },
        )
        panel = _make_panel(
            qapp,
            get_enabled=lambda: True,
            get_status_path=lambda: status,
        )
        try:
            overall = panel.findChild(type(panel._overall), "overall_status")
            assert "FAILED" in overall.text()
            assert COLOR_WARN in overall.styleSheet()
            assert overall.property("state_color") == COLOR_WARN

            body_state = panel.findChild(type(panel._overall), "ts_state_Body")
            face_state = panel.findChild(type(panel._overall), "ts_state_Face")
            trim_state = panel.findChild(type(panel._overall), "ts_state_Trim")
            assert body_state is not None and body_state.text() == "CONVERTED_OK"
            assert COLOR_OK in body_state.styleSheet()
            assert face_state is not None and face_state.text() == "FAILED"
            assert COLOR_WARN in face_state.styleSheet()
            assert trim_state is not None and trim_state.text() == "ORIGINAL"
            assert COLOR_MUTED in trim_state.styleSheet()

            assert panel.findChild(type(panel._overall), "ts_name_Body").text() == "Body"
            assert panel.findChild(type(panel._overall), "ts_generation_Body").text() == "7"
            assert "probe" in panel.findChild(type(panel._overall), "ts_message_Face").text()
        finally:
            panel.hide()
            panel.close()

    def test_attention_states_use_attention_color(self, qapp, tmp_path):
        status = _write_status(
            tmp_path / "status.json",
            {
                "A": _entry("CONVERTING", 1, "2026-10-16T11:55:00", ""),
                "B": _entry("STALE", 2, "2026-10-16T11:00:00", ""),
            },
        )
        now_fixed = datetime(2026, 10, 16, 12, 0, 0)
        panel = _make_panel(
            qapp,
            get_enabled=lambda: True,
            get_status_path=lambda: status,
            now_fn=lambda: now_fixed,
        )
        try:
            converting = panel.findChild(type(panel._overall), "ts_state_A")
            stale = panel.findChild(type(panel._overall), "ts_state_B")
            assert converting.text() == "CONVERTING"
            assert COLOR_ATTENTION in converting.styleSheet()
            assert stale.text() == "STALE"
            assert COLOR_ATTENTION in stale.styleSheet()
            overall = panel.findChild(type(panel._overall), "overall_status")
            assert "STALE" in overall.text()
            assert COLOR_ATTENTION in overall.styleSheet()

            panel.refresh()
            qapp.processEvents()
            converting = panel.findChild(type(panel._overall), "ts_state_A")
            stale = panel.findChild(type(panel._overall), "ts_state_B")
            overall = panel.findChild(type(panel._overall), "overall_status")
            assert converting.text() == "CONVERTING"
            assert COLOR_ATTENTION in converting.styleSheet()
            assert stale.text() == "STALE"
            assert COLOR_ATTENTION in stale.styleSheet()
            assert "STALE" in overall.text()
            assert COLOR_ATTENTION in overall.styleSheet()
        finally:
            panel.hide()
            panel.close()

    def test_converting_stalled_after_refresh_uses_warn_color(self, qapp, tmp_path):
        updated = "2026-10-16T10:15:00"
        status = _write_status(
            tmp_path / "status.json",
            {"Body": _entry("CONVERTING", 8, updated, "working")},
        )
        panel = _make_panel(
            qapp,
            get_enabled=lambda: True,
            get_status_path=lambda: status,
            now_fn=lambda: _NOW_ELEVEN_MIN_AFTER_UPDATED,
        )
        try:
            state = panel.findChild(type(panel._overall), "ts_state_Body")
            message = panel.findChild(type(panel._overall), "ts_message_Body")
            overall = panel.findChild(type(panel._overall), "overall_status")
            assert state.text() == "CONVERTING"
            assert COLOR_WARN in state.styleSheet()
            assert state.property("state_color") == "#c94a2a"
            assert STALLED_MESSAGE in message.text()
            assert COLOR_WARN in overall.styleSheet()

            panel.refresh()
            qapp.processEvents()
            state = panel.findChild(type(panel._overall), "ts_state_Body")
            message = panel.findChild(type(panel._overall), "ts_message_Body")
            overall = panel.findChild(type(panel._overall), "overall_status")
            assert state.text() == "CONVERTING"
            assert COLOR_WARN in state.styleSheet()
            assert state.property("state_color") == "#c94a2a"
            assert STALLED_MESSAGE in message.text()
            assert COLOR_WARN in overall.styleSheet()
        finally:
            panel.hide()
            panel.close()

    def test_timer_stops_when_hidden(self, qapp):
        panel = _make_panel(qapp, get_enabled=lambda: True)
        try:
            assert panel._timer.isActive() is True
            assert panel._timer.interval() == 2000
            panel.hide()
            qapp.processEvents()
            assert panel._timer.isActive() is False
        finally:
            panel.close()

    def test_convert_and_revert_callbacks(self, qapp):
        events = []
        panel = _make_panel(
            qapp,
            get_enabled=lambda: True,
            on_convert_now=lambda: events.append("convert"),
            on_revert=lambda: events.append("revert"),
        )
        try:
            panel.findChild(type(panel._btn_convert), "btn_convert").click()
            panel.findChild(type(panel._btn_revert), "btn_revert").click()
            qapp.processEvents()
            assert events == ["convert", "revert"]
        finally:
            panel.hide()
            panel.close()

    def test_no_info_label_when_status_missing(self, qapp, tmp_path):
        missing = tmp_path / "missing.json"
        panel = _make_panel(
            qapp, get_enabled=lambda: True, get_status_path=lambda: missing,
        )
        try:
            overall = panel.findChild(type(panel._overall), "overall_status")
            empty = panel.findChild(type(panel._overall), "ts_empty")
            assert LABEL_NO_INFO in overall.text()
            assert LABEL_NO_PROJECT not in overall.text()
            assert empty is not None and empty.text() == LABEL_NO_INFO
            assert COLOR_MUTED in overall.styleSheet()
        finally:
            panel.hide()
            panel.close()

    def test_no_project_label_when_status_path_is_none(self, qapp):
        panel = _make_panel(qapp, get_enabled=lambda: True, get_status_path=lambda: None)
        try:
            overall = panel.findChild(type(panel._overall), "overall_status")
            empty = panel.findChild(type(panel._overall), "ts_empty")
            assert overall.text() == f"{LABEL_OVERALL_PREFIX}: {LABEL_NO_PROJECT}"
            assert empty is not None and empty.text() == LABEL_NO_PROJECT
            assert COLOR_MUTED in overall.styleSheet()
            panel.refresh()
            qapp.processEvents()
            assert overall.text() == f"{LABEL_OVERALL_PREFIX}: {LABEL_NO_PROJECT}"
            assert empty.text() == LABEL_NO_PROJECT
        finally:
            panel.hide()
            panel.close()


def test_queued_converting_is_not_stalled(tmp_path):
    updated = "2026-10-16T10:15:00"
    entry = _entry("CONVERTING", 1, updated, "順番待ち")
    entry["queued"] = True
    status = _write_status(tmp_path / "status.json", {"Body": entry})
    view = StatusModel(status, now_fn=lambda: _NOW_ELEVEN_MIN_AFTER_UPDATED).load()
    row = view.rows[0]
    assert row.state == "CONVERTING"
    assert row.stalled is False
    assert STALLED_MESSAGE not in row.message
    assert row.color == COLOR_ATTENTION


def test_fold_status_label_p2_used_for_closed_row():
    assert fold_status_label_p2(True, False, True, True) == "要反映"
    assert fold_status_label_p2(True, False, False, True) == "自動追従"


class TestPainterFollowCheckbox:
    def test_checkbox_added_and_grayed_when_kill_off(self, qapp):
        enabled = {"value": False}
        auto = {"value": True}
        panel = _make_panel(
            qapp,
            get_enabled=lambda: enabled["value"],
            get_auto_follow=lambda: auto["value"],
        )
        try:
            box = panel.findChild(type(panel._check_painter), "check_painter")
            assert box is not None
            assert box.text() == LABEL_CHECK_PAINTER
            assert box.isEnabled() is False
            assert box.isChecked() is True
            enabled["value"] = True
            panel.refresh()
            qapp.processEvents()
            assert box.isEnabled() is True
            assert box.isChecked() is True
        finally:
            panel.hide()
            panel.close()

    def test_checkbox_calls_callback(self, qapp):
        calls = []
        auto = {"value": False}
        panel = _make_panel(
            qapp,
            get_enabled=lambda: True,
            get_auto_follow=lambda: auto["value"],
            on_toggle_auto_follow=lambda value: calls.append(value) or auto.update(value=value),
        )
        try:
            box = panel.findChild(type(panel._check_painter), "check_painter")
            assert box.isEnabled() is True
            assert box.isChecked() is False
            box.click()
            qapp.processEvents()
            assert calls == [True]
            assert box.isChecked() is True
        finally:
            panel.hide()
            panel.close()


class TestUvFollowCheckbox:
    def test_checkbox_under_painter_and_grayed_when_kill_off(self, qapp):
        enabled = {"value": False}
        auto_uv = {"value": True}
        panel = _make_panel(
            qapp,
            get_enabled=lambda: enabled["value"],
            get_auto_follow_uv=lambda: auto_uv["value"],
        )
        try:
            painter = panel.findChild(type(panel._check_painter), "check_painter")
            box = panel.findChild(type(panel._check_uv), "check_uv")
            assert painter is not None and box is not None
            assert box.text() == LABEL_CHECK_UV
            assert box.isEnabled() is False
            assert box.isChecked() is True
            order = []
            layout = panel.layout()
            for index in range(layout.count()):
                widget = layout.itemAt(index).widget()
                if widget is not None and widget.objectName() in ("check_painter", "check_uv"):
                    order.append(widget.objectName())
            assert order == ["check_painter", "check_uv"]
            enabled["value"] = True
            panel.refresh()
            qapp.processEvents()
            assert box.isEnabled() is True
            assert box.isChecked() is True
        finally:
            panel.hide()
            panel.close()

    def test_checkbox_calls_callback(self, qapp):
        calls = []
        auto_uv = {"value": False}
        panel = _make_panel(
            qapp,
            get_enabled=lambda: True,
            get_auto_follow_uv=lambda: auto_uv["value"],
            on_toggle_auto_follow_uv=lambda value: calls.append(value) or auto_uv.update(value=value),
        )
        try:
            box = panel.findChild(type(panel._check_uv), "check_uv")
            assert box.isEnabled() is True
            assert box.isChecked() is False
            box.click()
            qapp.processEvents()
            assert calls == [True]
            assert box.isChecked() is True
        finally:
            panel.hide()
            panel.close()
