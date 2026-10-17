# -*- coding: utf-8 -*-
"""UV再マップの状態表示パネル(Maya非依存)。

契約: docs/uvremap/CONTRACT.md §4.5(status.json)、§6 D-1 / D-3 / D-4。

このモジュールは表示とコールバック呼び出しだけを担う。設定ファイルや
Maya API には触れない。組み込み(MayaQWidgetDockableMixin 等)は後続作業。

PySide2 を優先し、無ければ PySide6(maya_live_sync.py と同じ順)。
どちらも無い環境でも `import uvremap_panel` 自体は失敗しない(Qt 部分は遅延)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

# --- 状態と色(契約 D-1。色だけに頼らず状態名も必ず表示する) ---------------

STATE_ORIGINAL = "ORIGINAL"
STATE_STALE = "STALE"
STATE_CONVERTING = "CONVERTING"
STATE_CONVERTED_OK = "CONVERTED_OK"
STATE_FAILED = "FAILED"
STATE_BLOCKED = "BLOCKED"

KNOWN_STATES = (
    STATE_ORIGINAL,
    STATE_STALE,
    STATE_CONVERTING,
    STATE_CONVERTED_OK,
    STATE_FAILED,
    STATE_BLOCKED,
)

# 色トークン: ok / attention / warn / muted
COLOR_OK = "#2a9c4a"
COLOR_ATTENTION = "#c9822a"
COLOR_WARN = "#c94a2a"
COLOR_MUTED = "#888888"

STATE_COLORS = {
    STATE_CONVERTED_OK: COLOR_OK,
    STATE_CONVERTING: COLOR_ATTENTION,
    STATE_STALE: COLOR_ATTENTION,
    STATE_FAILED: COLOR_WARN,
    STATE_BLOCKED: COLOR_WARN,
    STATE_ORIGINAL: COLOR_MUTED,
}

# 最悪状態: FAILED/BLOCKED > STALE/CONVERTING > CONVERTED_OK > ORIGINAL
# 同順位のときは左側を優先する。
_RANK_FAILED = 3
_RANK_ATTENTION = 2
_RANK_OK = 1
_RANK_ORIGINAL = 0

STATE_RANK = {
    STATE_FAILED: _RANK_FAILED,
    STATE_BLOCKED: _RANK_FAILED,
    STATE_STALE: _RANK_ATTENTION,
    STATE_CONVERTING: _RANK_ATTENTION,
    STATE_CONVERTED_OK: _RANK_OK,
    STATE_ORIGINAL: _RANK_ORIGINAL,
}

_TIE_BREAK = {
    STATE_FAILED: 0,
    STATE_BLOCKED: 1,
    STATE_STALE: 2,
    STATE_CONVERTING: 3,
    STATE_CONVERTED_OK: 4,
    STATE_ORIGINAL: 5,
}

CONVERTING_STALE_SECONDS = 10 * 60
STALLED_MESSAGE = "停止した可能性"

LABEL_ENABLE = "UV変換を有効にする"
LABEL_CONVERT = "今すぐ変換"
LABEL_REVERT = "原本に戻す"
LABEL_REFRESH = "更新"
LABEL_DISABLED = "UV変換は無効(従来のLive Syncの動作)"
LABEL_NO_INFO = "情報なし"
LABEL_NO_PROJECT = "プロジェクト未設定"
LABEL_OVERALL_PREFIX = "全体"

REFRESH_INTERVAL_MS = 2000


@dataclass(frozen=True)
class StatusRow:
    """1テクスチャセットぶんの表示データ。"""

    ts_name: str
    state: str
    generation: int | None
    updated: str
    message: str
    stalled: bool = False

    @property
    def color(self) -> str:
        if self.stalled:
            return COLOR_WARN
        return color_for_state(self.state)


@dataclass(frozen=True)
class StatusView:
    """status.json 1回分の読み取り結果。壊れていても例外は出さない。"""

    rows: tuple[StatusRow, ...]
    worst_state: str
    available: bool

    @property
    def worst_color(self) -> str:
        if not self.available:
            return COLOR_MUTED
        if any(row.stalled for row in self.rows):
            worst_rank = STATE_RANK.get(self.worst_state, _RANK_ATTENTION)
            if worst_rank < _RANK_FAILED:
                return COLOR_WARN
        return color_for_state(self.worst_state)


def color_for_state(state: str) -> str:
    """既知の状態は規約色。未知の状態名は取り違え防止のため注意色。"""
    return STATE_COLORS.get(state, COLOR_ATTENTION)


def empty_rows_label(status_path) -> str:
    """行が無いときの空欄。status パスが None ならプロジェクト未設定。"""
    if status_path is None:
        return LABEL_NO_PROJECT
    return LABEL_NO_INFO


def overall_status_label(enabled: bool, view: StatusView, status_path) -> tuple[str, str]:
    """全体ラベルの(文言, 色)。Qt 無しで決められる。

    キルスイッチ ON かつ status パスが None のときだけプロジェクト未設定。
    パスはあるが読めないときは情報なし。OFF のときは従来の無効表示。
    """
    if not enabled:
        return LABEL_DISABLED, COLOR_MUTED
    if status_path is None:
        return f"{LABEL_OVERALL_PREFIX}: {LABEL_NO_PROJECT}", COLOR_MUTED
    if not view.available:
        return f"{LABEL_OVERALL_PREFIX}: {LABEL_NO_INFO}", COLOR_MUTED
    return f"{LABEL_OVERALL_PREFIX}: {view.worst_state}", view.worst_color


def _parse_updated(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _as_generation(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _row_rank(row: StatusRow) -> int:
    if row.stalled:
        return _RANK_FAILED
    return STATE_RANK.get(row.state, _RANK_ATTENTION)


def _pick_worst(rows: Iterable[StatusRow]) -> str:
    best: StatusRow | None = None
    best_key: tuple[int, int] | None = None
    for row in rows:
        key = (_row_rank(row), -_TIE_BREAK.get(row.state, 10))
        if best is None or best_key is None or key > best_key:
            best = row
            best_key = key
    return best.state if best is not None else ""


def _compose_message(raw: str, stalled: bool) -> str:
    if not stalled:
        return raw
    if not raw:
        return STALLED_MESSAGE
    if STALLED_MESSAGE in raw:
        return raw
    return f"{raw} / {STALLED_MESSAGE}"


class StatusModel:
    """status.json を読み、表示用の行と最悪状態を返す(Qt 非依存)。"""

    def __init__(
        self,
        path: str | Path | None,
        *,
        converting_stale_s: float = CONVERTING_STALE_SECONDS,
        now: datetime | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self.converting_stale_s = float(converting_stale_s)
        self._now = now
        self._now_fn = now_fn if now_fn is not None else datetime.now

    def load(self, *, now: datetime | None = None) -> StatusView:
        if now is not None:
            clock = now
        elif self._now is not None:
            clock = self._now
        else:
            clock = self._now_fn()
        payload = self._read_payload()
        if payload is None:
            return StatusView(rows=(), worst_state="", available=False)

        ts_map = payload.get("ts")
        if not isinstance(ts_map, dict):
            return StatusView(rows=(), worst_state="", available=False)

        rows = []
        for name in sorted(ts_map.keys(), key=lambda item: str(item)):
            entry = ts_map[name]
            if not isinstance(entry, dict):
                continue
            row = self._row_from_entry(str(name), entry, clock)
            if row is not None:
                rows.append(row)

        if not rows:
            return StatusView(rows=(), worst_state="", available=False)

        return StatusView(
            rows=tuple(rows),
            worst_state=_pick_worst(rows),
            available=True,
        )

    def _read_payload(self) -> dict | None:
        if self.path is None:
            return None
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _row_from_entry(
        self, ts_name: str, entry: dict, clock: datetime
    ) -> StatusRow | None:
        if not ts_name:
            return None
        state = _as_text(entry.get("state")).strip() or STATE_ORIGINAL
        updated = _as_text(entry.get("updated")).strip()
        raw_message = _as_text(entry.get("message"))
        stalled = self._is_stalled(state, updated, clock)
        return StatusRow(
            ts_name=ts_name,
            state=state,
            generation=_as_generation(entry.get("generation")),
            updated=updated,
            message=_compose_message(raw_message, stalled),
            stalled=stalled,
        )

    def _is_stalled(self, state: str, updated: str, clock: datetime) -> bool:
        if state != STATE_CONVERTING:
            return False
        parsed = _parse_updated(updated)
        if parsed is None:
            return True
        try:
            age = (clock - parsed).total_seconds()
        except TypeError:
            return True
        return age >= self.converting_stale_s


def _load_qt():
    try:
        from PySide2 import QtCore, QtWidgets
    except ImportError:
        from PySide6 import QtCore, QtWidgets
    return QtCore, QtWidgets


_panel_cls = None


def _uvremap_panel_class():
    """Qt を遅延 import して UVRemapPanel クラスを組み立てる。"""
    global _panel_cls
    if _panel_cls is not None:
        return _panel_cls

    QtCore, QtWidgets = _load_qt()

    class UVRemapPanel(QtWidgets.QWidget):
        """TSごとの状態とキルスイッチを常時見せるパネル。"""

        def __init__(
            self,
            parent=None,
            *,
            on_toggle_enabled: Callable[[bool], None] | None = None,
            on_convert_now: Callable[[], None] | None = None,
            on_revert: Callable[[], None] | None = None,
            get_enabled: Callable[[], bool] | None = None,
            get_status_path: Callable[[], str | Path | None] | None = None,
            converting_stale_s: float = CONVERTING_STALE_SECONDS,
            now_fn: Callable[[], datetime] | None = None,
        ) -> None:
            super().__init__(parent)
            self._on_toggle_enabled = on_toggle_enabled
            self._on_convert_now = on_convert_now
            self._on_revert = on_revert
            self._get_enabled = get_enabled
            self._get_status_path = get_status_path
            self._converting_stale_s = converting_stale_s
            self._now_fn = now_fn if now_fn is not None else datetime.now

            self.setObjectName("uvremap_panel")
            self.setWindowTitle("UV再マップ")
            self.setMinimumWidth(420)

            root = QtWidgets.QVBoxLayout(self)
            root.setContentsMargins(8, 8, 8, 8)
            root.setSpacing(8)

            self._overall = QtWidgets.QLabel(LABEL_NO_INFO)
            self._overall.setObjectName("overall_status")
            self._overall.setWordWrap(True)
            self._overall.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            root.addWidget(self._overall)

            header = QtWidgets.QWidget()
            header.setObjectName("ts_header")
            header_layout = QtWidgets.QGridLayout(header)
            header_layout.setContentsMargins(4, 0, 4, 0)
            header_layout.setHorizontalSpacing(8)
            for col, text in enumerate(("TS", "状態", "世代", "更新", "メッセージ")):
                label = QtWidgets.QLabel(text)
                label.setObjectName(f"ts_header_{col}")
                header_layout.addWidget(label, 0, col)
            header_layout.setColumnStretch(4, 1)
            root.addWidget(header)

            self._rows_host = QtWidgets.QWidget()
            self._rows_host.setObjectName("ts_rows")
            self._rows_layout = QtWidgets.QVBoxLayout(self._rows_host)
            self._rows_layout.setContentsMargins(0, 0, 0, 0)
            self._rows_layout.setSpacing(4)
            self._empty_label = QtWidgets.QLabel(LABEL_NO_INFO)
            self._empty_label.setObjectName("ts_empty")
            self._rows_layout.addWidget(self._empty_label)

            scroll = QtWidgets.QScrollArea()
            scroll.setObjectName("ts_scroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setWidget(self._rows_host)
            root.addWidget(scroll, 1)

            self._kill_switch = QtWidgets.QCheckBox(LABEL_ENABLE)
            self._kill_switch.setObjectName("kill_switch")
            self._kill_switch.toggled.connect(self._on_kill_toggled)
            root.addWidget(self._kill_switch)

            buttons = QtWidgets.QHBoxLayout()
            self._btn_convert = QtWidgets.QPushButton(LABEL_CONVERT)
            self._btn_convert.setObjectName("btn_convert")
            self._btn_convert.clicked.connect(self._emit_convert)
            self._btn_revert = QtWidgets.QPushButton(LABEL_REVERT)
            self._btn_revert.setObjectName("btn_revert")
            self._btn_revert.clicked.connect(self._emit_revert)
            self._btn_refresh = QtWidgets.QPushButton(LABEL_REFRESH)
            self._btn_refresh.setObjectName("btn_refresh")
            self._btn_refresh.clicked.connect(self.refresh)
            buttons.addWidget(self._btn_convert)
            buttons.addWidget(self._btn_revert)
            buttons.addStretch(1)
            buttons.addWidget(self._btn_refresh)
            root.addLayout(buttons)

            self._timer = QtCore.QTimer(self)
            self._timer.setObjectName("refresh_timer")
            self._timer.setInterval(REFRESH_INTERVAL_MS)
            self._timer.timeout.connect(self.refresh)

            self.refresh()

        def showEvent(self, event) -> None:
            super().showEvent(event)
            if not self._timer.isActive():
                self._timer.start()
            self.refresh()

        def hideEvent(self, event) -> None:
            if self._timer.isActive():
                self._timer.stop()
            super().hideEvent(event)

        def refresh(self) -> None:
            enabled = self._read_enabled()
            self._apply_enabled(enabled)
            status_path = self._read_status_path()
            view = StatusModel(
                status_path,
                converting_stale_s=self._converting_stale_s,
                now_fn=self._now_fn,
            ).load()
            self._render(view, enabled, status_path)

        def _read_enabled(self) -> bool:
            if self._get_enabled is None:
                return False
            try:
                return bool(self._get_enabled())
            except Exception:
                return False

        def _read_status_path(self) -> str | Path | None:
            if self._get_status_path is None:
                return None
            try:
                return self._get_status_path()
            except Exception:
                return None

        def _apply_enabled(self, enabled: bool) -> None:
            blocked = self._kill_switch.blockSignals(True)
            self._kill_switch.setChecked(enabled)
            self._kill_switch.blockSignals(blocked)
            self._btn_convert.setEnabled(enabled)
            self._btn_revert.setEnabled(enabled)
            self._btn_refresh.setEnabled(enabled)

        def _on_kill_toggled(self, checked: bool) -> None:
            if self._on_toggle_enabled is not None:
                try:
                    self._on_toggle_enabled(bool(checked))
                except Exception:
                    pass
            self.refresh()

        def _emit_convert(self) -> None:
            if self._on_convert_now is not None:
                self._on_convert_now()

        def _emit_revert(self) -> None:
            if self._on_revert is not None:
                self._on_revert()

        def _render(self, view: StatusView, enabled: bool, status_path=None) -> None:
            overall_text, overall_color = overall_status_label(
                enabled, view, status_path,
            )
            self._overall.setText(overall_text)
            self._apply_state_style(self._overall, overall_color)

            self._clear_rows()
            if not view.rows:
                self._empty_label.setText(empty_rows_label(status_path))
                self._empty_label.show()
                return
            self._empty_label.hide()
            for row in view.rows:
                self._rows_layout.addWidget(self._make_row_widget(row))

        def _clear_rows(self) -> None:
            while self._rows_layout.count() > 1:
                item = self._rows_layout.takeAt(1)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

        def _make_row_widget(self, row: StatusRow):
            frame = QtWidgets.QFrame()
            safe = _safe_object_name(row.ts_name)
            frame.setObjectName(f"ts_row_{safe}")
            layout = QtWidgets.QGridLayout(frame)
            layout.setContentsMargins(4, 2, 4, 2)
            layout.setHorizontalSpacing(8)

            name_label = QtWidgets.QLabel(row.ts_name)
            name_label.setObjectName(f"ts_name_{safe}")
            state_label = QtWidgets.QLabel(row.state)
            state_label.setObjectName(f"ts_state_{safe}")
            gen_text = "" if row.generation is None else str(row.generation)
            gen_label = QtWidgets.QLabel(gen_text)
            gen_label.setObjectName(f"ts_generation_{safe}")
            updated_label = QtWidgets.QLabel(row.updated)
            updated_label.setObjectName(f"ts_updated_{safe}")
            message_label = QtWidgets.QLabel(row.message)
            message_label.setObjectName(f"ts_message_{safe}")
            message_label.setWordWrap(True)

            self._apply_state_style(state_label, row.color)
            layout.addWidget(name_label, 0, 0)
            layout.addWidget(state_label, 0, 1)
            layout.addWidget(gen_label, 0, 2)
            layout.addWidget(updated_label, 0, 3)
            layout.addWidget(message_label, 0, 4)
            layout.setColumnStretch(4, 1)
            return frame

        @staticmethod
        def _apply_state_style(widget, color: str) -> None:
            widget.setStyleSheet(
                f"background-color: {color}; color: #ffffff; padding: 4px;"
            )
            widget.setProperty("state_color", color)

    _panel_cls = UVRemapPanel
    return _panel_cls


def _safe_object_name(name: str) -> str:
    chars = []
    for ch in name:
        if ch.isalnum() or ch in ("_", "-"):
            chars.append(ch)
        else:
            chars.append("_")
    return "".join(chars) or "ts"


def __getattr__(name: str):
    if name == "UVRemapPanel":
        return _uvremap_panel_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
