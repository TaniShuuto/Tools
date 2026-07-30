"""
anypath.core.types  -  AnyPath Core: 共有データ型定義
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §3.1（確信度4段階）、§3.2（解決カスケード）

このモジュールは AnyPath Core 層の一部であり、**Maya API を一切 import しない**
（設計書 §2.1「最重要の設計上の分離」）。純粋な dataclass / Enum のみを定義する。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 に基づき新規作成。
"""

__version__ = "1.0.0"

from enum import Enum
from typing import List, Optional, Dict, Any


class Confidence(Enum):
    """
    確信度4段階（設計書 §3.1）。

    v1 の MEDIUM（自動適用されうるが確証がない帯域）は廃止され REVIEW へ
    統合されている。自動適用されるのは EXACT と HIGH のみ。
    利用者が調整できる確信度の閾値は設けない（内部定数として HIGH に固定）。
    """
    EXACT = "EXACT"      # パスが確実に一意に定まる。自動適用する
    HIGH = "HIGH"         # 一意に定まり、かつ構造的な裏付けがある。自動適用する
    REVIEW = "REVIEW"     # 候補はあるが、正しさを機械的に保証できない。自動適用しない
    FAILED = "FAILED"     # 候補が存在しない。自動適用しない

    @property
    def auto_apply(self):
        """このConfidenceが自動適用の対象かどうか（P1: 誤リンクゼロ）。"""
        return self in (Confidence.EXACT, Confidence.HIGH)


class Strategy(Enum):
    """
    解決カスケードの各段（設計書 §3.2）。
    上から順に試行し、最初に成功した段で確定する。
    """
    S0_AS_IS = "S0_AS_IS"                   # そのまま実在確認
    S1_PATH_MAPPING = "S1_PATH_MAPPING"     # マッピング表による前方一致置換
    S2_ENV_EXPANSION = "S2_ENV_EXPANSION"   # 環境変数・トークンの展開
    S3_ANCHOR_REBASE = "S3_ANCHOR_REBASE"   # アンカーフォルダ相対の再構築
    S4_INDEX_MATCH = "S4_INDEX_MATCH"       # ファイル名インデックス検索＋構造照合
    S5_UNRESOLVED = "S5_UNRESOLVED"         # 解決不能


class ResolveInput:
    """
    Core への入力1件（壊れたパス文字列＋文脈情報）。

    設計書 §2.1: 「Core の入力は『壊れたパス文字列』『探索ルート一覧』
    『インデックス』『設定』のみ」。

    node_key はテストや呼び出し元が結果を対応付けるための不透明な識別子
    （Bridge層ではノードのフルパス名等が入る想定だが、Core自身はその意味を
    解釈しない）。
    """
    __slots__ = ("node_key", "raw_path")

    def __init__(self, node_key, raw_path):
        self.node_key = node_key
        self.raw_path = raw_path

    def __repr__(self):
        return "ResolveInput(node_key={0!r}, raw_path={1!r})".format(
            self.node_key, self.raw_path)


class ResolveResult:
    """
    Core からの出力1件（設計書 §2.1: 「出力は『候補リストと確信度』のみ」）。

    resolved_path: 確信度が EXACT/HIGH の場合の確定パス。REVIEW/FAILED では
                   None（推奨候補は candidates[0] を参照する）。
    candidates:    REVIEW時の候補一覧。構造スコア降順、同点は絶対パス辞書順
                   （§3.4-4 のタイブレークルール）。EXACT/HIGH/FAILEDでは
                   通常0〜1件。
    """
    __slots__ = (
        "node_key", "raw_path", "confidence", "strategy",
        "resolved_path", "candidates", "detail",
    )

    def __init__(self, node_key, raw_path, confidence, strategy,
                 resolved_path=None, candidates=None, detail=None):
        self.node_key = node_key
        self.raw_path = raw_path
        self.confidence = confidence
        self.strategy = strategy
        self.resolved_path = resolved_path
        self.candidates = candidates if candidates is not None else []
        # detail: 構造スコアの内訳等、ログ・自己診断向けの補助情報（dict）。
        # UIへ直接出す文字列は含めない（§6.8 のエラーメッセージ規約は
        # Shell層の責務であり、Coreはここに機微な文言を書かない）。
        self.detail = detail if detail is not None else {}

    def __repr__(self):
        return (
            "ResolveResult(node_key={0!r}, confidence={1}, strategy={2}, "
            "resolved_path={3!r}, candidates={4})"
        ).format(
            self.node_key, self.confidence, self.strategy,
            self.resolved_path, len(self.candidates))


class Candidate:
    """S4構造照合における1候補（絶対パス＋構造スコアの内訳）。"""
    __slots__ = ("abs_path", "score", "score_detail")

    def __init__(self, abs_path, score, score_detail=None):
        self.abs_path = abs_path
        self.score = score
        self.score_detail = score_detail if score_detail is not None else {}

    def __repr__(self):
        return "Candidate(abs_path={0!r}, score={1})".format(
            self.abs_path, self.score)
