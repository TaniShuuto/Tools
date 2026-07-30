"""
anypath.shell  -  AnyPath Shell層: UI層

設計書 AnyPath_技術設計書_v2.1.md §6 より。

対象環境は Maya 2026 以降で PySide6 / Qt6 固定（設計書 §1.2、CLAUDE.md）。
PySide2互換レイヤは持たない。

モジュール構成:
    messages.py           エラーメッセージ規約・用語対訳（§6.8, §6.10。
                          Maya非依存の純粋関数群）
    review_panel.py         要確認パネル（§6.5。PySide6）
    diagnosis_panel.py       自己診断画面（§6.9。PySide6）
"""

__version__ = "1.0.0"
