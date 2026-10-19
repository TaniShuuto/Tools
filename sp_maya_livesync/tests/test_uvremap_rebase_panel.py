# -*- coding: utf-8 -*-
"""リベース欄の純関数と、PySide6 があるときのウィジェット。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_MODULE_DIR = Path(__file__).resolve().parents[1]
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from uvremap_panel import (  # noqa: E402
    COLOR_ATTENTION,
    COLOR_MUTED,
    COLOR_OK,
    COLOR_WARN,
    LABEL_REBASE_CANCEL,
    LABEL_REBASE_SEND,
    color_for_rebase_phase,
    normalize_rebase_view,
)


def test_rebase_phase_colors():
    assert color_for_rebase_phase("done") == COLOR_OK
    assert color_for_rebase_phase("failed") == COLOR_WARN
    for phase in ("waiting", "working", "locked", "canceling"):
        assert color_for_rebase_phase(phase) == COLOR_ATTENTION
    assert color_for_rebase_phase("ended") == COLOR_MUTED
    assert color_for_rebase_phase("") == COLOR_MUTED


def test_normalize_rebase_view_rejects_bad_payloads():
    assert normalize_rebase_view(None) == {
        "text": "", "phase": "", "can_send": False, "can_cancel": False,
    }
    assert normalize_rebase_view(["nope"])["text"] == ""
    assert normalize_rebase_view({"text": 3, "phase": 1, "can_send": "x"}) == {
        "text": "", "phase": "", "can_send": True, "can_cancel": False,
    }
    assert normalize_rebase_view({
        "text": "待っています",
        "phase": "waiting",
        "can_send": True,
        "can_cancel": 0,
    }) == {
        "text": "待っています",
        "phase": "waiting",
        "can_send": True,
        "can_cancel": False,
    }


def test_labels():
    assert LABEL_REBASE_SEND == "Painterへ送る"
    assert LABEL_REBASE_CANCEL == "依頼を取り消す"


@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip("PySide6 を import できない: {0}".format(exc))
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_omitted_arguments_hide_the_rebase_section(qapp):
    from PySide6 import QtWidgets

    from uvremap_panel import UVRemapPanel

    panel = UVRemapPanel()
    assert panel.findChild(QtWidgets.QLabel, "rebase_status") is None
    assert panel.findChild(QtWidgets.QPushButton, "btn_rebase_send") is None
    assert panel.findChild(QtWidgets.QPushButton, "btn_rebase_cancel") is None
    assert panel.findChild(QtWidgets.QPushButton, "btn_convert") is not None


def test_rebase_section_reflects_view_and_clicks(qapp):
    from PySide6 import QtWidgets

    from uvremap_panel import UVRemapPanel

    calls = []
    view = {
        "text": "Painter の応答を待っています",
        "phase": "waiting",
        "can_send": True,
        "can_cancel": False,
    }
    panel = UVRemapPanel(
        on_rebase_send=lambda: calls.append("send"),
        on_rebase_cancel=lambda: calls.append("cancel"),
        get_rebase_view=lambda: dict(view),
    )
    label = panel.findChild(QtWidgets.QLabel, "rebase_status")
    send = panel.findChild(QtWidgets.QPushButton, "btn_rebase_send")
    cancel = panel.findChild(QtWidgets.QPushButton, "btn_rebase_cancel")
    assert label is not None and send is not None and cancel is not None
    assert label.text() == view["text"]
    assert label.wordWrap() is True
    assert send.text() == LABEL_REBASE_SEND
    assert cancel.text() == LABEL_REBASE_CANCEL
    assert send.isEnabled() is True
    assert cancel.isEnabled() is False
    send.click()
    assert calls == ["send"]

    view["text"] = "Painter が読み直しています(取り消せません)"
    view["phase"] = "locked"
    view["can_send"] = False
    view["can_cancel"] = False
    panel.refresh()
    qapp.processEvents()
    assert label.text() == view["text"]
    assert send.isEnabled() is False
    assert cancel.isEnabled() is False
    assert label.property("state_color") == COLOR_ATTENTION

    view["phase"] = "done"
    view["text"] = "リベースが完了しました"
    view["can_send"] = True
    view["can_cancel"] = True
    panel.refresh()
    assert label.property("state_color") == COLOR_OK
    cancel.click()
    assert calls == ["send", "cancel"]


def test_bad_view_and_callback_do_not_crash(qapp):
    from PySide6 import QtWidgets

    from uvremap_panel import UVRemapPanel

    def boom_view():
        raise RuntimeError("view")

    def boom_send():
        raise RuntimeError("send")

    panel = UVRemapPanel(
        on_rebase_send=boom_send,
        on_rebase_cancel=boom_send,
        get_rebase_view=boom_view,
    )
    panel.refresh()
    label = panel.findChild(QtWidgets.QLabel, "rebase_status")
    send = panel.findChild(QtWidgets.QPushButton, "btn_rebase_send")
    assert label.text() == ""
    send.click()
    panel.findChild(QtWidgets.QPushButton, "btn_rebase_cancel").click()

    panel._get_rebase_view = lambda: "nope"
    panel.refresh()
    assert label.text() == ""
