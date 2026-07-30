"""
anypath.shell.review_panel  -  Shell層: 要確認パネル（§6.5）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §6.5（三状態モデルと要確認パネル）
再設計計画書: AnyPath_再設計計画書_v1.md §2 柱1・柱2、§3 シナリオB・C、§4.1

対象環境は Maya 2026 以降で PySide6 / Qt6 固定（設計書 §1.2、CLAUDE.md）の
ため、PySide2互換レイヤは持たない。

三状態モデル:
    全て正常          -> 何も表示しない
    自動修復のみ完了   -> 非モーダルの控えめな通知
    要確認あり         -> モーダルではないパネルを開き、1画面で完結させる

再設計方針（v1.1、重要。旧v1.0からの変更点）:
    旧実装は各行に「候補一覧（ドロップダウン）」を出し、利用者に複数候補
    から正しいものを選ばせる「決定」を求めていた。再設計計画書の指摘
    （「原則OKボタンや選択ボタンを押すだけで良いようにしたい」）に基づき、
    これを「確認」に置き換える:
        - AnyPathが最有力と判断した1件（candidates[0]）だけを大きく表示
        - 「これでOK」ボタンを押すだけで確定（＝候補を比較させない）
        - 「違う」ボタンを押した時だけファイル選択ダイアログを開く
    複数候補の一覧比較UIはこの画面から無くなった（比較させること自体が
    「考えさせる」行為であるため）。

要確認パネルの要件（実装必須。番号は設計書の番号。※印は再設計での変更点）:
    1. 専門用語を出さない（§6.10の対訳表を参照）
    2. ※各行に「見つからないファイル名」「AnyPathの一番の候補」
       「これでOK／違う」ボタンを配置する（候補ドロップダウンは廃止）
    3. 「見つからない参照シーン」を独立セクションとして常設する（§5.3）
    4. ※一括操作は「すべてこれでOK」のみ（推奨候補での一括適用。
       「このフォルダを探索先に追加して再試行」は自己診断画面の
       第1階層に集約し、この画面からは撤去した。追加後の再試行は
       自動で行われるため、利用者がこの画面とあちらを往復する必要はない）
    5. サムネイルプレビュー（画像のみ・遅延読込）
    6. 「あとで」で閉じられること。閉じたことでシーンが壊れてはならない
    7. 「差し替え」：自動適用済みの項目も含め、パネルを再度開いて別候補へ
       切り替えられること（「違う」ボタンが同じ役割を果たす）
    8. FAILEDの行には「次に何をすればよいか」を必ず1行で示す（§6.8）
    9. レポートをテキストファイルへ書き出せること（§6.7。ただし導線は
       目立たない位置へ移動した。§4.1参照）

「ロールバック」は搭載しない（§6.5末尾の通り）。メニューから1クリックで
mode="off"へ一時切り替えできるようにし、実質的な「効果の取り消し」とする
（AnyPath.pyの_toggle_offが既に提供している）。

変更履歴:
    1.4.0 (2026.07.31):
        - ユーザー指摘対応（「Modo_UV_checke.jpg では
          Modo_UV_checker.jpg の候補が出てこない」＋
          「候補と適応ボタンと除外ボタンを設置して選べるように
          したい」）: ReviewRow を、候補が2件以上ある場合は縦に並べて
          各々に個別の「適用」ボタンを持つレイアウトへ拡張した
          （candidatesが1件の場合は従来通り「これでOK／違う」の
          2択のまま。あいまいマッチング(anypath.core.index.
          lookup_fuzzy)が複数候補を返すケースで主に使われる）。
          候補ごとの「除外」ボタンは設けず、行全体で1つの
          「どれも違う（ファイルを選ぶ）」ボタンに統一した（個々の
          候補を除外する操作と別ファイルを選ぶ操作が実質同じ結果に
          なるため、選択肢を増やして複雑にしない設計判断）。
          あいまいマッチの候補には score_detail["edit_distance"]
          があれば「似た名前・差分n文字相当」という補足を添えるように
          した。
        - 後方互換性: MINOR。候補1件の既存ケースの表示・シグナルは
          一切変更していない。
    1.4.1 (2026.08.01):
        - ユーザー指摘対応（「候補の表示が薄くて見にくい」）:
          _build_candidate_row() の候補行に背景色・枠線付きのカード
          スタイルを追加した。パス本体は白系（#f0f0f0）の太字・12px、
          編集距離の補足（似た名前・差分n文字相当）はパスと別行に分け、
          10pxの控えめなグレー（#bbbbbb）にして視覚的に分離した。
          「適用」ボタンにも背景色（#4a90d9系の青、hover/pressedで
          色を変化）を付け、候補行の中で押せる場所を分かりやすくした。
          Qtのデフォルト文字色のままだとMayaのダークテーマ上でコントラ
          ストが弱く読みにくかったための対応。
        - 後方互換性: PATCH。見た目のみの変更で、シグナル・構造・
          public APIは一切変更していない。
    1.5.0 (2026.08.01):
        - ユーザー報告対応（「適用ボタンを押してもちゃんと反映は
          されるのですが表示が消えない」）: set_review_items() で
          作った行は、confirm_requested/reject_requested を発火する
          だけでパネル自身の一覧から消す処理がどこにも実装されて
          いなかった（呼び出し元のAnyPath.py側でも適用は行うが行の
          削除はしていなかった）。ReviewPanel.remove_row(node_key) を
          新設し、呼び出し元が適用成功後に呼べば該当行をレイアウトから
          取り除きdeleteLater()するようにした。ReferenceSectionWidget
          にも対になる remove_reference_row(ref_node) を新設し、
          ReviewPanel.remove_reference_row() から委譲する形にした
          （全件消えた場合は「見つからない参照シーンはありません」の
          プレースホルダー表示に戻す）。
        - 後方互換性: MINOR（API追加のみ）。既存メソッドの挙動・
          シグナルは変更していない。呼び出し元がremove_row()を呼ばない
          限り、従来通り行は残り続ける（呼ぶかどうかは呼び出し元の
          任意）。
    1.3.0 (2026.07.31):
        - ユーザー指摘対応（「そもそもプロジェクトファイルmbなども
          対象なのか」）: ReferenceSectionWidget.set_unresolved_references()
          を改修した。UnresolvedReference.candidates
          （reference_repath.py 1.1.0で新設）があれば、テクスチャの
          ReviewRowと同じ「これでOK／違う」の2択を表示するようにした
          （候補が無い場合は従来通り「参照...」のみ）。
        - 後方互換性: MINOR。candidatesを持たない（空リストの）
          UnresolvedReferenceを渡した場合は従来通り「参照...」のみが
          表示されるため、既存呼び出し元に影響しない。
    1.2.0 (2026.07.31):
        - ユーザー指摘対応: show_non_modal_toast() の duration_ms に
          None を渡すと自動で消えなくなるようにした（既定値4000は
          維持。呼び出し元AnyPath.pyがNoneを明示的に渡すことで
          「Scriptエディタの他のログに埋もれて気づけない」問題に
          対応した）。
        - 後方互換性: PATCH。デフォルト引数を変えていないため、
          呼び出し元を変更しない限り既存動作に影響しない。
    1.1.0 (2026.07.30):
        - 再設計計画書 v1 §2 柱2 に基づき、ReviewRow を候補ドロップダウン
          方式から「これでOK / 違う」の2択ボタン方式へ全面改修した。
        - 一括操作から「このフォルダを探索先に追加して再試行」ボタンを
          撤去（自己診断画面第1階層に統合。diagnosis_panel.py側で提供）。
          「すべて推奨候補で修正」は「すべてこれでOK」に文言変更のうえ維持。
          レポート書き出しボタンは footer の控えめな位置へ移動。
        - ReviewRow.candidate_changed シグナルを廃止し、
          confirm_requested(node_key) / reject_requested(node_key) の
          2シグナルへ置き換えた。ReviewPanel側の
          candidate_replace_requested は「これでOK」時にも
          candidates[0] のパスを添えて発火させることで、呼び出し元
          （diagnosis_controller等）のAPIを極力変えずに済ませている。
        - 後方互換性: MINOR相当の破壊的UI変更。呼び出し元シグナル名の
          一部変更を伴うため、AnyPath.py側の配線も同時に更新する。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §6.5 に基づき新規作成。
"""

__version__ = "1.5.0"

from PySide6 import QtCore, QtGui, QtWidgets

from anypath.core.types import Confidence
from anypath.shell.messages import build_failed_message, build_review_message


class ThumbnailLoader(QtCore.QObject):
    """
    §6.5-5: サムネイルプレビュー（画像のみ・遅延読込）。
    QThreadPool経由でファイルI/Oをバックグラウンド化し、UIスレッドを
    ブロックしない（§6.6: 「応答なしと見える状態を作らない」の一環）。
    """
    thumbnail_ready = QtCore.Signal(str, QtGui.QPixmap)

    _IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".exr", ".bmp")

    def __init__(self, parent=None):
        super(ThumbnailLoader, self).__init__(parent)
        self._pool = QtCore.QThreadPool.globalInstance()

    def is_image_path(self, path):
        if not path:
            return False
        lower = path.lower()
        return any(lower.endswith(ext) for ext in self._IMAGE_EXTENSIONS)

    def request(self, path):
        if not self.is_image_path(path):
            return
        runnable = _ThumbnailRunnable(path, self.thumbnail_ready)
        self._pool.start(runnable)


class _ThumbnailRunnable(QtCore.QRunnable):
    def __init__(self, path, signal):
        super(_ThumbnailRunnable, self).__init__()
        self._path = path
        self._signal = signal

    def run(self):
        # EXR等、Qt標準がデコードできない形式は静かに諦める（P4: 個別失敗で
        # パネル全体を壊さない）。サムネイル取得はあくまで補助機能。
        try:
            pixmap = QtGui.QPixmap(self._path)
            if pixmap.isNull():
                return
            scaled = pixmap.scaled(
                64, 64, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            self._signal.emit(self._path, scaled)
        except Exception:
            pass


class ReviewRow(QtWidgets.QWidget):
    """
    §6.5-2（再設計後）: 要確認パネルの1行。

    候補が1件の場合（従来通り）:
        「見つからないファイル名」「AnyPathの一番の候補」
        「これでOK／違う」の2択ボタンを配置する。

    候補が2件以上の場合（2026.07.31新設。ユーザー指摘対応:
    「Modo_UV_checke.jpg では候補が出てこない」への対応で、あいまい
    マッチングが複数候補を返すようになったため）:
        候補を縦に並べ、各々に個別の「適用」ボタンを配置する
        （ユーザー指示: 「候補と適応ボタンと除外ボタンを設置して
        選べるようにしたい」）。どれも合わない場合のために、行全体に
        対する「どれも違う（ファイルを選ぶ）」ボタンを1つ下部に置く
        （候補ごとの「除外」は、実質的にこの1つのボタンで代替できる
        ―― 個々の候補を積極的に「除外する」意味のある操作は無く、
        結局「別のファイルを選ぶ」という同じ行動になるため、ボタンを
        候補数分増やして選択肢を複雑にしない設計とした）。

    候補が無い（FAILED）行は「ファイルを選ぶ」ボタンのみを大きく置く
    （従来通り）。
    """
    confirm_requested = QtCore.Signal(str, str)   # (node_key, confirmed_path)
    reject_requested = QtCore.Signal(str)          # (node_key) -> ファイル選択へ

    def __init__(self, node_key, display_name, candidates, failed_message=None, parent=None):
        super(ReviewRow, self).__init__(parent)
        self.node_key = node_key
        self._candidates = candidates
        self._top_candidate = candidates[0] if candidates else None
        self._is_multi_candidate = len(candidates) > 1

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(2)

        top_row = QtWidgets.QHBoxLayout()
        self.thumb_label = QtWidgets.QLabel()
        self.thumb_label.setFixedSize(40, 40)
        self.thumb_label.setScaledContents(True)
        top_row.addWidget(self.thumb_label)

        text_col = QtWidgets.QVBoxLayout()
        name_label = QtWidgets.QLabel("<b>{0}</b>".format(display_name))
        text_col.addWidget(name_label)

        if self._is_multi_candidate:
            hint_label = QtWidgets.QLabel(
                "似ている名前のファイルが {0} 件見つかりました。"
                "合っているものがあれば「適用」を押してください。".format(
                    len(candidates)))
            hint_label.setWordWrap(True)
            hint_label.setStyleSheet("color: #555;")
            text_col.addWidget(hint_label)
        elif self._top_candidate is not None:
            suggestion_label = QtWidgets.QLabel(self._top_candidate.abs_path)
            suggestion_label.setWordWrap(True)
            suggestion_label.setStyleSheet("color: #555;")
            text_col.addWidget(suggestion_label)
        elif failed_message:
            msg_label = QtWidgets.QLabel(failed_message)
            msg_label.setWordWrap(True)
            msg_label.setStyleSheet("color: #a55;")
            text_col.addWidget(msg_label)

        top_row.addLayout(text_col, stretch=1)
        outer.addLayout(top_row)

        if self._is_multi_candidate:
            self.confirm_btn = None
            self._candidate_rows = []
            for candidate in candidates:
                outer.addWidget(self._build_candidate_row(candidate))

            reject_row = QtWidgets.QHBoxLayout()
            reject_row.addStretch(1)
            self.reject_btn = QtWidgets.QPushButton("どれも違う（ファイルを選ぶ）")
            self.reject_btn.clicked.connect(
                lambda: self.reject_requested.emit(self.node_key))
            reject_row.addWidget(self.reject_btn)
            outer.addLayout(reject_row)
        else:
            button_row = QtWidgets.QHBoxLayout()
            button_row.addStretch(1)

            if self._top_candidate is not None:
                self.confirm_btn = QtWidgets.QPushButton("これでOK")
                self.confirm_btn.setDefault(True)
                self.confirm_btn.clicked.connect(self._on_confirm)
                button_row.addWidget(self.confirm_btn)

                self.reject_btn = QtWidgets.QPushButton("違う")
                self.reject_btn.clicked.connect(
                    lambda: self.reject_requested.emit(self.node_key))
                button_row.addWidget(self.reject_btn)
            else:
                self.confirm_btn = None
                self.reject_btn = QtWidgets.QPushButton("ファイルを選ぶ")
                self.reject_btn.clicked.connect(
                    lambda: self.reject_requested.emit(self.node_key))
                button_row.addWidget(self.reject_btn)

            outer.addLayout(button_row)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet("color: #ddd;")
        outer.addWidget(line)

    def _build_candidate_row(self, candidate):
        """
        複数候補モード（2026.07.31新設）の1候補分の行。
        パス表示（あいまいマッチの場合は編集距離のヒントを添える）＋
        個別の「適用」ボタン。

        2026.08.01改修: 候補パスの文字がQtのデフォルト文字色のままだと
        Mayaのダークテーマ上でコントラストが弱く読みにくいというユーザー
        指摘への対応。行全体をカード状（背景色＋枠線＋角丸）にして候補
        同士の境界を分かりやすくし、パス本体は白系の太字・大きめフォント
        で強調、編集距離の補足は別行に分けて控えめなグレーで表示する
        （パスと補足情報を視覚的に分離し、パス自体の視認性を優先）。
        """
        row = QtWidgets.QWidget()
        row.setStyleSheet(
            "QWidget { background-color: #3a3a3a; border: 1px solid #565656; "
            "border-radius: 4px; }"
        )
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(10, 8, 8, 8)
        row_layout.setSpacing(8)

        text_col = QtWidgets.QVBoxLayout()
        text_col.setSpacing(2)

        path_label = QtWidgets.QLabel(candidate.abs_path)
        path_label.setWordWrap(True)
        path_label.setStyleSheet(
            "color: #f0f0f0; font-size: 12px; font-weight: bold; "
            "background: transparent; border: none;"
        )
        text_col.addWidget(path_label)

        edit_distance = candidate.score_detail.get("edit_distance")
        if edit_distance is not None:
            hint = QtWidgets.QLabel(
                "似た名前・差分{0}文字相当".format(edit_distance))
            hint.setStyleSheet(
                "color: #bbbbbb; font-size: 10px; "
                "background: transparent; border: none;"
            )
            text_col.addWidget(hint)

        row_layout.addLayout(text_col, stretch=1)

        apply_btn = QtWidgets.QPushButton("適用")
        apply_btn.setMinimumWidth(64)
        apply_btn.setStyleSheet(
            "QPushButton { background-color: #4a90d9; color: white; "
            "font-weight: bold; padding: 4px 12px; border-radius: 3px; "
            "border: none; }"
            "QPushButton:hover { background-color: #5b9fe3; }"
            "QPushButton:pressed { background-color: #3d7bbf; }"
        )
        apply_btn.clicked.connect(
            lambda _checked=False, p=candidate.abs_path:
            self.confirm_requested.emit(self.node_key, p))
        row_layout.addWidget(apply_btn)

        self._candidate_rows.append(row)
        return row

    def _on_confirm(self):
        if self._top_candidate is not None:
            self.confirm_requested.emit(self.node_key, self._top_candidate.abs_path)

    def set_thumbnail(self, pixmap):
        self.thumb_label.setPixmap(pixmap)

    def selected_candidate(self):
        """後方互換: 「これでOK」時に確定するパス（=最有力候補）を返す。"""
        if self._top_candidate is not None:
            return self._top_candidate.abs_path
        return None


class ReferenceSectionWidget(QtWidgets.QGroupBox):
    """
    §6.5-3, §5.3: 「見つからない参照シーン」を独立セクションとして常設する。
    テクスチャと同じ行に混ぜてはならない。

    2026.07.31（ユーザー指摘対応: 「プロジェクトファイルmbなども
    対象なのか」）: 未解決リファレンスにもCore解決の候補
    （UnresolvedReference.candidates）が付くようになったため、候補が
    あればテクスチャの行と同じ「これでOK／違う」の2択を、無ければ
    「参照...」のみを表示するよう改修した。
    """
    reload_requested = QtCore.Signal(str, str)  # (ref_node, new_file_path)

    def __init__(self, parent=None):
        super(ReferenceSectionWidget, self).__init__("見つからない参照しているシーン", parent)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._rows = {}

    def set_unresolved_references(self, unresolved_refs):
        # 既存行をクリアしてから再構築する。
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows = {}

        if not unresolved_refs:
            placeholder = QtWidgets.QLabel("見つからない参照シーンはありません。")
            self._layout.addWidget(placeholder)
            self.setVisible(False)
            return

        self.setVisible(True)
        for ref in unresolved_refs:
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)

            candidates = getattr(ref, "candidates", None) or []
            top_candidate = candidates[0] if candidates else None

            label_text = ref.raw_path
            if top_candidate is not None:
                label_text = "{0}\n候補: {1}".format(ref.raw_path, top_candidate.abs_path)
            label = QtWidgets.QLabel(label_text)
            label.setWordWrap(True)
            row_layout.addWidget(label, stretch=1)

            if top_candidate is not None:
                confirm_btn = QtWidgets.QPushButton("これでOK")
                confirm_btn.clicked.connect(
                    lambda _checked=False, r=ref.ref_node, p=top_candidate.abs_path:
                    self.reload_requested.emit(r, p))
                row_layout.addWidget(confirm_btn)

                reject_btn = QtWidgets.QPushButton("違う")
                reject_btn.clicked.connect(
                    lambda _checked=False, r=ref.ref_node: self._on_browse(r))
                row_layout.addWidget(reject_btn)
            else:
                browse_btn = QtWidgets.QPushButton("参照...")
                browse_btn.clicked.connect(
                    lambda _checked=False, r=ref.ref_node: self._on_browse(r))
                row_layout.addWidget(browse_btn)

            self._layout.addWidget(row)
            self._rows[ref.ref_node] = row

    def _on_browse(self, ref_node):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "参照するシーンファイルを選択")
        if path:
            self.reload_requested.emit(ref_node, path)

    def remove_reference_row(self, ref_node):
        """
        2026.08.01新設: リファレンス再読込が成功した1件をUIから取り除く
        （ユーザー報告「適用しても表示が消えない」への対応。テクスチャ側の
        ReviewPanel.remove_row()と対になる、参照シーンセクション版）。

        全件消えた場合は set_unresolved_references([]) と同じ
        「見つからない参照シーンはありません」プレースホルダー表示に戻す。
        """
        row = self._rows.pop(ref_node, None)
        if row is None:
            return
        self._layout.removeWidget(row)
        row.deleteLater()

        if not self._rows:
            placeholder = QtWidgets.QLabel("見つからない参照シーンはありません。")
            self._layout.addWidget(placeholder)
            self.setVisible(False)


class ReviewPanel(QtWidgets.QDialog):
    """
    §6.5: 要確認パネル本体。モーダルではない（非モーダルダイアログとして
    表示する。setModal(False)）。

    再設計後（v1.1）: 一括操作は「すべてこれでOK」のみ。探索ルート追加は
    自己診断画面（第1階層）へ集約したため、この画面には置かない
    （再設計計画書 v1 §4.1, §4.3）。
    """

    apply_all_recommended_requested = QtCore.Signal()
    export_report_requested = QtCore.Signal()
    later_requested = QtCore.Signal()
    candidate_replace_requested = QtCore.Signal(str, str)  # (node_key, new_path)
    browse_requested = QtCore.Signal(str)                   # (node_key)
    reference_reload_requested = QtCore.Signal(str, str)    # (ref_node, path)

    def __init__(self, parent=None):
        super(ReviewPanel, self).__init__(parent)
        self.setWindowTitle("AnyPath - 確認が必要な項目")
        self.setModal(False)  # §6.5: モーダルではないパネル
        self.resize(680, 560)

        self._thumbnail_loader = ThumbnailLoader(self)
        self._thumbnail_loader.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._rows_by_node_key = {}

        root_layout = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            "それぞれの候補が合っているか確認してください。"
            "「これでOK」を押すだけで直ります。")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #444;")
        root_layout.addWidget(intro)

        # --- 一括操作（§6.5-4。再設計後は「すべてこれでOK」のみ） ---
        bulk_layout = QtWidgets.QHBoxLayout()
        self.apply_all_btn = QtWidgets.QPushButton("すべてこれでOK")
        self.apply_all_btn.clicked.connect(self.apply_all_recommended_requested.emit)
        bulk_layout.addWidget(self.apply_all_btn)
        bulk_layout.addStretch(1)
        root_layout.addLayout(bulk_layout)

        # --- 見つからない参照シーン（§6.5-3, §5.3。独立セクション常設） ---
        self.reference_section = ReferenceSectionWidget()
        self.reference_section.reload_requested.connect(
            self.reference_reload_requested.emit)
        root_layout.addWidget(self.reference_section)

        # --- テクスチャ等の要確認一覧 ---
        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        self._rows_container = QtWidgets.QWidget()
        self._rows_layout = QtWidgets.QVBoxLayout(self._rows_container)
        self._rows_layout.addStretch(1)
        scroll_area.setWidget(self._rows_container)
        root_layout.addWidget(scroll_area, stretch=1)

        # --- フッター（§6.5-6: 「あとで」。レポート書き出しは控えめに同居） ---
        footer_layout = QtWidgets.QHBoxLayout()
        self.export_btn = QtWidgets.QPushButton("レポートを書き出す")
        self.export_btn.setFlat(True)
        self.export_btn.clicked.connect(self.export_report_requested.emit)
        footer_layout.addWidget(self.export_btn)
        footer_layout.addStretch(1)
        self.later_btn = QtWidgets.QPushButton("あとで")
        self.later_btn.clicked.connect(self._on_later)
        footer_layout.addWidget(self.later_btn)
        root_layout.addLayout(footer_layout)

    def _on_later(self):
        """
        §6.5-6: 「あとで」で閉じられること。閉じたことでシーンが壊れては
        ならない ―― この操作はUIを閉じるだけで、既に適用済みの修復や
        シーンの状態には一切触れない。
        """
        self.later_requested.emit()
        self.hide()

    def set_review_items(self, review_results, node_display_names=None):
        """
        review_results: [ResolveResult, ...]（confidence == REVIEW または
        FAILED のもの）を渡す。node_display_names: {node_key: 表示名}
        （§6.10の対訳表を通した後の、専門用語を含まない表示名）。
        """
        node_display_names = node_display_names or {}

        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows_by_node_key = {}

        for result in review_results:
            display_name = node_display_names.get(result.node_key, result.node_key)
            if result.confidence == Confidence.FAILED:
                msg = build_failed_message(display_name).to_text()
                row = ReviewRow(result.node_key, display_name, [], failed_message=msg)
            else:
                row = ReviewRow(result.node_key, display_name, result.candidates)

            row.confirm_requested.connect(self.candidate_replace_requested.emit)
            row.reject_requested.connect(self.browse_requested.emit)

            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
            self._rows_by_node_key[result.node_key] = row

            if result.raw_path:
                self._thumbnail_loader.request(result.raw_path)

    def set_unresolved_references(self, unresolved_refs):
        self.reference_section.set_unresolved_references(unresolved_refs)

    def remove_row(self, node_key):
        """
        2026.08.01新設: ユーザー報告対応（「適用ボタンを押してもちゃんと
        反映はされるのですが表示が消えない」）。

        テクスチャ等の要確認一覧から、適用（またはリファレンス以外の
        修復）が完了した1件をUI上からも取り除く。set_review_items()
        で作られる行はこのパネルが生存している間ずっと残り続け、
        「これでOK」「適用」を押しても行自体を削除する処理がどこにも
        無かったため、適用は正しく効いているのに画面上は残ったままに
        見えていた（node_keyが分かっていれば呼び出し元は都度これを
        呼んで消す）。

        呼び出し元（AnyPath.py）が把握していないnode_keyを渡しても
        単に何もしない（P4: 個別失敗で全体を止めない）。
        """
        row = self._rows_by_node_key.pop(node_key, None)
        if row is None:
            return
        self._rows_layout.removeWidget(row)
        row.deleteLater()

    def remove_reference_row(self, ref_node):
        """
        2026.08.01新設: 参照シーン（.mb/.ma）セクション側の1件を、
        リファレンス再読込が成功した後にUIから取り除く。
        実処理はReferenceSectionWidget.remove_reference_row()へ委譲する。
        """
        self.reference_section.remove_reference_row(ref_node)

    def _on_thumbnail_ready(self, path, pixmap):
        for row in self._rows_by_node_key.values():
            row.set_thumbnail(pixmap)


def show_non_modal_toast(parent, message, duration_ms=4000):
    """
    §6.5: 「自動修復のみ完了」状態向けの、非モーダルの控えめな通知。
    「テクスチャ12件のリンクを自動で修正しました」＋詳細を開くリンク、
    という形の簡易実装（詳細を開くリンクはコールバックで拡張可能にする）。

    duration_ms に None を渡すと自動では消えなくなる（利用者が明示的に
    クリックする/閉じるまで残り続ける）。再設計計画書 v1 §3 シナリオB
    向けの「要確認」通知は、Scriptエディタの他のログ等に紛れて見逃される
    ことを避けるため、この無期限表示を使う（2026.07.31 ユーザー指摘に
    基づく変更。詳細はAnyPath.pyの変更履歴を参照）。
    """
    toast = QtWidgets.QLabel(message, parent)
    toast.setWindowFlags(QtCore.Qt.ToolTip)
    toast.setStyleSheet(
        "background-color: #333; color: white; padding: 8px 12px; "
        "border-radius: 4px;")
    toast.setAttribute(QtCore.Qt.WA_DeleteOnClose)
    toast.adjustSize()
    if parent is not None:
        geo = parent.geometry()
        toast.move(geo.right() - toast.width() - 20, geo.bottom() - toast.height() - 40)
    toast.show()
    if duration_ms is not None:
        QtCore.QTimer.singleShot(duration_ms, toast.close)
    return toast
