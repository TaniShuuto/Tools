"""Qt の import を集約する(PySide2 / PySide6 の切り替え)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

__all__ = [
    "QtCore",
    "QtGui",
    "QtWidgets",
]


# Substance Painter 10.1 以降は内部UIがPySide6へ移行しており、
# PySide2 モジュールが存在しない環境がある(逆に10.1未満はPySide6が無い)。
# そのため両対応させるためのフォールバックを行う。
try:
    from PySide2 import QtCore, QtWidgets, QtGui
except ImportError:
    from PySide6 import QtCore, QtWidgets, QtGui
