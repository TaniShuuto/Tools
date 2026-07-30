"""
anypath.shell.messages  -  Shell層: エラーメッセージ規約と用語対訳
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §6.8（エラーメッセージ規約）、§6.10（用語の対訳表）
再設計計画書: AnyPath_再設計計画書_v1.md §2 柱2, §4.1

§6.8 エラーメッセージ規約:
    UIにPythonのトレースバック、例外クラス名、ノード名、属性名を表示しては
    ならない。これらはログにのみ記録する。

    利用者へ表示するメッセージは、必ず以下の3要素で構成すること:
        1. 何が起きたか（事実を平易に）
        2. なぜか（分かる場合のみ。推測は書かない）
        3. 次に何をすればよいか（必ず1つ以上の具体的な行動を示す）

    3番目を省略してはならない。原因が不明な場合でも、最低限「自己診断
    画面を開く」を行動として提示する。

§6.10 用語の対訳表（UI実装時に必ず参照）。

このモジュールは文字列組み立てのみを行う純粋関数群であり、Maya API に
依存しない（Shell層だが、UI実装本体とは切り離して pytest 検証できる
ようにする）。

変更履歴:
    1.1.0 (2026.07.30):
        - 再設計計画書 v1 §2 柱2「選択肢を『決定』ではなく『確認』に
          変える」に基づき、要確認パネルを候補ドロップダウン方式から
          「これでOK / 違う」の2択方式へ変更するのに伴い、
          build_review_confirm_message() を新設した。旧
          build_review_message()（候補件数を提示する文言）は、複数
          候補の一覧比較UIが無くなったことに伴い非推奨とするが、
          後方互換のため関数自体は残す。
        - 自己診断画面の2階層化（§4.3）に伴い、第1階層に表示する
          簡潔な状態文言 build_status_summary() を新設した。
        - 後方互換性: MINOR。既存関数のシグネチャ・戻り値は変更しない。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §6.8, §6.10 に基づき新規作成。
"""

__version__ = "1.1.0"


# --- §6.10 用語の対訳表 ------------------------------------------------------
TERM_TRANSLATIONS = {
    # 内部用語 -> UI表示
    "node": None,                     # ノード / 属性 -> （表示しない）
    "attribute": None,
    "fileTextureName": "テクスチャ",
    "reference": "参照しているシーン",
    "Alembic": "キャッシュファイル",
    "GPU Cache": "キャッシュファイル",
    "resolve": "見つける／リンクを直す",
    "EXACT": "自動で直しました",
    "HIGH": "自動で直しました",
    "REVIEW": "確認が必要です",
    "FAILED": "見つかりませんでした",
    "search_root": "探す場所",
    "index": "ファイル一覧の作成",
    "dirmap": "フォルダの読み替え設定",
}


def translate_term(internal_term):
    """
    内部用語をUI表示用の日本語へ変換する（§6.10）。
    対訳が「表示しない」と定義されている場合は None を返す
    （呼び出し側でその項目自体を表示から除外する）。
    未登録の用語はそのまま返す（変換ミスで無表示になるより安全）。
    """
    if internal_term in TERM_TRANSLATIONS:
        return TERM_TRANSLATIONS[internal_term]
    return internal_term


def confidence_label(confidence_value):
    """確信度(EXACT/HIGH/REVIEW/FAILED)をUI表示ラベルへ変換する。"""
    mapping = {
        "EXACT": "自動で直しました",
        "HIGH": "自動で直しました",
        "REVIEW": "確認が必要です",
        "FAILED": "見つかりませんでした",
    }
    return mapping.get(confidence_value, confidence_value)


# --- §6.8 エラーメッセージ規約 -----------------------------------------------
class UserMessage:
    """
    利用者へ表示するメッセージ（§6.8の3要素構成を強制する）。
    what: 何が起きたか（必須）
    why: なぜか（任意。分かる場合のみ。Noneなら表示しない）
    next_action: 次に何をすればよいか（必須。1つ以上の具体的な行動）
    """
    __slots__ = ("what", "why", "next_action")

    def __init__(self, what, next_action, why=None):
        if not what:
            raise ValueError("what（何が起きたか）は必須です（§6.8）。")
        if not next_action:
            raise ValueError(
                "next_action（次に何をすればよいか）は必須です（§6.8）。"
                "原因不明でも最低限「自己診断画面を開く」を提示してください。")
        self.what = what
        self.why = why
        self.next_action = next_action

    def to_text(self):
        """UIへそのまま渡せる複数行テキストに変換する。"""
        lines = ["何が起きたか: {0}".format(self.what)]
        if self.why:
            lines.append("")
            lines.append("なぜか: {0}".format(self.why))
        lines.append("")
        lines.append("次に何をすればよいか: {0}".format(self.next_action))
        return "\n".join(lines)


# 原因不明時のフォールバック行動（§6.8: 「最低限『自己診断画面を開く』を
# 行動として提示してください」）。
FALLBACK_NEXT_ACTION = "メニューの「AnyPath の状態を確認」から自己診断画面を開いてください。"


def build_failed_message(file_display_name, reason_hint=None):
    """
    確信度 FAILED（見つかりませんでした）の行に対する、§6.8準拠のメッセージ。
    reason_hint が None の場合は「なぜか」を省略し、次の行動には
    FALLBACK_NEXT_ACTION を用いる。
    """
    return UserMessage(
        what="「{0}」が見つかりませんでした。".format(file_display_name),
        why=reason_hint,
        next_action=(
            "「参照…」からファイルを選ぶか、「このフォルダを探索先に追加」を"
            "お試しください。それでも直らない場合は、{0}".format(FALLBACK_NEXT_ACTION)
        ),
    )


def build_review_message(file_display_name, candidate_count):
    """
    確信度 REVIEW（確認が必要）の行に対する、§6.8準拠のメッセージ。

    非推奨（v1.1.0）: 再設計計画書 v1 §2 柱2 により、要確認パネルは
    複数候補の一覧比較UIから「これでOK / 違う」の2択方式へ変更された。
    新規実装では build_review_confirm_message() を使うこと。
    後方互換のためこの関数自体は残す。
    """
    return UserMessage(
        what="「{0}」の候補が {1} 件見つかりましたが、自動では決められません。".format(
            file_display_name, candidate_count),
        why="正しさを機械的に保証できないため、確認をお願いしています。",
        next_action="候補一覧から正しいものを選ぶか、「参照…」から直接選んでください。",
    )


def build_review_confirm_message(file_display_name):
    """
    再設計後の要確認パネル向けメッセージ（§2 柱2: 「決定」ではなく「確認」）。

    複数候補があっても、利用者へ提示するのは常に「AnyPathが一番良いと
    判断した1件」のみ。文言も「選んでください」ではなく「合っているか
    確認してください」に統一し、比較や決定を求めるニュアンスを避ける。
    """
    return UserMessage(
        what="「{0}」らしきファイルを見つけました。".format(file_display_name),
        why=None,
        next_action="合っていれば「これでOK」を、違っていれば「違う」から"
                    "ファイルを選んでください。",
    )


def build_root_evicted_note(evicted_display_name):
    """
    §4.3: 探索ルートの自動LRU入れ替えが発生した際の、控えめな一言。
    エラーでも警告でもないため3要素構成は使わず、通知的な一文のみとする。
    """
    return "探す場所の数がいっぱいだったため、しばらく使っていなかった" \
        "「{0}」を自動的に外しました。".format(evicted_display_name)


def build_status_summary(state):
    """
    自己診断画面・第1階層（再設計計画書 v1 §4.3）向けの簡潔な状態文言。
    技術用語（mode/status等の内部値）をそのまま出さず、3値程度に要約する。

    state: {"status":..., "mode":..., "review_count":...} 形式の辞書
           （AnyPath.py の _state をそのまま渡せる想定）。
    戻り値: (headline: str, ok: bool)
        headline: 1行で表す現在の状態。
        ok: 「問題なし」バッジを出してよいか。
    """
    status = state.get("status")
    mode = state.get("mode")
    review_count = state.get("review_count", 0)

    if status == "error":
        return "問題を検知したため、一時停止しています。", False
    if mode == "off" or status == "off":
        return "現在、AnyPathは停止中です。", True
    if review_count and review_count > 0:
        return "確認をお願いしたい項目が {0} 件あります。".format(review_count), False
    return "すべて正常です。何もする必要はありません。", True


def build_engine_stopped_message():
    """
    §0.1, §6.1: AnyPathがエラー停止した場合の通知メッセージ。
    トレースバック・例外クラス名は含めない（詳細はログのみに記録する）。
    """
    return UserMessage(
        what="AnyPath が問題を検知し、機能を一時停止しました。",
        why=None,
        next_action=FALLBACK_NEXT_ACTION,
    )


def build_search_root_too_large_message(estimated_count, threshold):
    """
    §6.4: 探索ルート追加時のガード警告メッセージ。
    「より深いフォルダを選び直す」を第一の選択肢として提示する。

    再設計計画書 v1 §4.3 により「お試し」追加の概念は廃止された
    （常に自動永続化・LRU入れ替え）ため、next_actionの文言からも
    「お試し」という表現を除いている。
    """
    return UserMessage(
        what="このフォルダには非常に多くのファイルがあります"
             "（約 {0} 件）。".format(_format_count_ja(estimated_count)),
        why="追加すると、シーンを開くたびに時間がかかるようになります"
            "（目安: {0} 件を超えると動作が遅くなります）。".format(
                _format_count_ja(threshold)),
        next_action="より深いフォルダ（対象のテクスチャに近い場所）を"
                    "選び直すことをおすすめします。それでもこのフォルダを"
                    "使いたい場合は、そのまま追加することもできます。",
    )


def _format_count_ja(n):
    """件数を「約12万件」のような日本語表記へ整形する（大きい桁は万単位）。"""
    if n >= 10000:
        man = n / 10000.0
        if man == int(man):
            return "{0}万".format(int(man))
        return "{0:.1f}万".format(man)
    return str(n)
