"""
anypath.shell.diagnosis_panel  -  Shell層: 自己診断画面（§6.9）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §6.9（自己診断画面。必須機能）
再設計計画書: AnyPath_再設計計画書_v1.md §2 柱1・柱3、§3 シナリオD、§4.3

「非エンジニアが自力で状況を切り分けられる唯一の手段です。.mb環境では
人力での確認手段が存在しないため（§1.3）、必須機能とします。」

表示すること（項目 -> 表示内容）:
    動作状態       現在のモード、有効／無効、バージョン、エラー停止時はその旨
    Mayaバージョン  検証済み／未検証の判定結果
    設定の読込結果  どのファイルが読まれたか、パースエラー・フォールバック・
                   値のクランプが発生したか（§6.3）
    探索ルート      使用中のルート一覧、各ルートの実在確認、概算ファイル数、
                   個別削除ボタン（§6.4-5）
    マッピング表    各マッピングのtoが実在するか
    インデックス    構築済みか、ファイル数、タイムアウトが発生したか
    dirmap         現在のマッピングと有効状態。ドライラン欄（dirmap -cd の
                   GUI化）
    ログ           出力先パスと「フォルダを開く」ボタン
    重複インストール 複数の.modが検出されていないか

「すべての項目に『問題なし／要注意』のいずれかを明示すること。」

再設計方針（v1.1、重要。旧v1.0からの変更点）:
    旧実装は上記8項目を1画面に並列表示していた。再設計計画書の指摘
    （「自己診断画面の情報量が多すぎる」）に基づき、2階層構成へ再編する。

    第1階層（常に見える。普段クリエイターが必要とする最小限）:
        - 状態バッジ（正常／確認が必要／停止中。build_status_summary()）
        - 直近の自動修復件数（何をしたかの一言サマリ）
        - 探す場所（search_roots）の一覧 ―― フォルダ追加ボタンもここに
          集約する（旧実装では要確認パネルと自己診断画面の2箇所に
          追加導線が分散していたため、再設計計画書 §4.3 に基づき
          ここへ一本化）
        - 困ったときは（ログを開く／レポートの場所）

    第2階層（「もっと詳しく」を押した場合のみ展開）:
        - 設定の読込結果・マッピング表・インデックス・dirmap・
          重複インストール・Gate0観測状況
        既存のセクション実装（set_config_report 等）はほぼそのまま
        流用し、配置先のみ変更する（作り直しコストを抑える方針は
        計画書 §6 の通り）。

変更履歴:
    1.2.0 (2026.07.31):
        - ユーザー指摘対応（「確認は右下の通知からのみしかアクセス
          できない」）: 第1階層「直近に自動で直した内容」セクションに、
          確認が必要な項目がある場合だけ表示される「確認する（n件）」
          ボタンを新設した。open_review_panel_requested シグナルを
          追加し、set_recent_summary() 呼び出し時にボタンの表示/非表示
          と件数表示を切り替えるようにした。
        - 後方互換性: MINOR。set_recent_summary()のシグネチャ自体は
          変更していない（ボタンの出し分けは既存の引数から導出する）。
    1.1.0 (2026.07.30):
        - 再設計計画書 v1 §4.3 に基づき、8セクション一覧表示を
          2階層構成（第1階層3項目＋第2階層6項目、「もっと詳しく」で
          開閉）へ再構築した。
        - 探索ルートのお試し/恒久の区別表示を廃止（root_manager.py
          v1.1のLRU自動管理化に追従）。使用日時の表示を追加。
        - 後方互換性: MINOR相当のUI変更。set_status/set_config_report等の
          既存メソッドのシグネチャは維持しつつ、新規に
          set_recent_summary() / set_search_roots()（引数形状を
          LRU対応に変更）を追加。呼び出し元（diagnosis_controller.py）
          も同時に更新する。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §6.9 に基づき新規作成。
"""

__version__ = "1.2.0"

from PySide6 import QtCore, QtGui, QtWidgets


class StatusBadge(QtWidgets.QLabel):
    """「問題なし／要注意」を明示するバッジ（§6.9末尾の必須要件）。"""

    def __init__(self, ok, parent=None):
        text = "問題なし" if ok else "要注意"
        super(StatusBadge, self).__init__(text, parent)
        color = "#2a7a2a" if ok else "#b05a00"
        self.setStyleSheet(
            "color: white; background-color: {0}; padding: 2px 8px; "
            "border-radius: 3px; font-weight: bold;".format(color))
        self.setAlignment(QtCore.Qt.AlignCenter)


def _section_box(title):
    box = QtWidgets.QGroupBox(title)
    layout = QtWidgets.QVBoxLayout(box)
    return box, layout


class CollapsibleSection(QtWidgets.QWidget):
    """
    再設計後に新設: 第2階層をまとめて折りたたむための入れ物。
    既定は閉じた状態（＝普段は見えない。再設計計画書 §2 柱1
    「見せない」をデフォルトにする、の実装）。
    """

    def __init__(self, title, parent=None):
        super(CollapsibleSection, self).__init__(parent)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._toggle_btn = QtWidgets.QToolButton()
        self._toggle_btn.setText(title)
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setChecked(False)
        self._toggle_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._toggle_btn.setArrowType(QtCore.Qt.RightArrow)
        self._toggle_btn.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self._toggle_btn.toggled.connect(self._on_toggled)
        outer.addWidget(self._toggle_btn)

        self._content = QtWidgets.QWidget()
        self._content_layout = QtWidgets.QVBoxLayout(self._content)
        self._content.setVisible(False)
        outer.addWidget(self._content)

    def _on_toggled(self, checked):
        self._toggle_btn.setArrowType(
            QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)
        self._content.setVisible(checked)

    def add_widget(self, widget):
        self._content_layout.addWidget(widget)


class DiagnosisPanel(QtWidgets.QDialog):
    """
    §6.9: 自己診断画面本体。メニュー（§6.1）の「AnyPath の状態を確認」
    から開く。

    このクラス自体はMaya APIに直接依存しない値の受け渡しのみを行う
    （呼び出し側=AnyPath.pyが実際のcmds呼び出し結果を辞書として渡す）。
    こうすることでUIレイアウトの単体テスト（表示崩れの有無等）を
    Maya外でも一定行いやすくする。
    """

    remove_search_root_requested = QtCore.Signal(str, bool)  # (path, is_trial)。is_trialは常にFalse固定（後方互換で残す）
    open_log_folder_requested = QtCore.Signal()
    dirmap_dry_run_requested = QtCore.Signal(str)             # (input_path)
    add_search_root_requested = QtCore.Signal()
    # 2026.07.31新設: 「確認する」ボタン。要確認パネルを直接開く要求。
    open_review_panel_requested = QtCore.Signal()

    def __init__(self, parent=None):
        super(DiagnosisPanel, self).__init__(parent)
        self.setWindowTitle("AnyPath - 状態を確認")
        self.resize(640, 640)

        outer_layout = QtWidgets.QVBoxLayout(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        outer_layout.addWidget(scroll)

        content = QtWidgets.QWidget()
        self._content_layout = QtWidgets.QVBoxLayout(content)
        scroll.setWidget(content)

        # --- 第1階層: 常に見える最小限 ---
        self._build_status_section()
        self._build_recent_summary_section()
        self._build_search_roots_section()
        self._build_help_section()

        # --- 第2階層: 「もっと詳しく」の中に格納 ---
        self._details = CollapsibleSection("もっと詳しく")
        self._content_layout.addWidget(self._details)

        self._build_config_section()
        self._build_mappings_section()
        self._build_index_section()
        self._build_dirmap_section()
        self._build_duplicate_install_section()
        self._build_gate0_section()

        self._content_layout.addStretch(1)

        close_btn = QtWidgets.QPushButton("閉じる")
        close_btn.clicked.connect(self.close)
        outer_layout.addWidget(close_btn)

    # ============================================================
    # 第1階層
    # ============================================================

    # --- 動作状態（簡潔な1行サマリ＋バッジのみ。技術用語は出さない） ---
    def _build_status_section(self):
        box, layout = _section_box("今の状態")
        self._status_label = QtWidgets.QLabel("-")
        self._status_label.setWordWrap(True)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._status_label, stretch=1)
        self._status_badge_placeholder = QtWidgets.QVBoxLayout()
        row.addLayout(self._status_badge_placeholder)
        layout.addLayout(row)
        self._content_layout.addWidget(box)

    def set_status(self, status, mode, version, maya_version_verified, last_error=None,
                   summary_headline=None, summary_ok=None):
        """
        summary_headline / summary_ok: messages.build_status_summary() の
        戻り値をそのまま渡す想定（第1階層に出す1行サマリ）。渡されない
        場合は旧来の技術寄り文言にフォールバックする（後方互換）。
        """
        if summary_headline is not None:
            self._status_label.setText(summary_headline)
            ok = bool(summary_ok) if summary_ok is not None else (status != "error")
        else:
            lines = [
                "状態: {0}".format(status),
                "モード: {0}".format(mode),
                "バージョン: {0}".format(version),
                "Mayaバージョン: {0}".format(
                    "検証済み" if maya_version_verified else "未検証"),
            ]
            if last_error:
                lines.append("直近のエラー（詳細はログ参照）: {0}".format(
                    type(last_error).__name__ if not isinstance(last_error, str) else "記録あり"))
            self._status_label.setText("\n".join(lines))
            ok = (status != "error") and maya_version_verified
        self._replace_badge(self._status_badge_placeholder, ok)
        # バージョン等、技術情報は第2階層の「設定の読込結果」付近に
        # 出したい呼び出し元のために保持だけしておく。
        self._raw_status_info = {
            "status": status, "mode": mode, "version": version,
            "maya_version_verified": maya_version_verified, "last_error": last_error,
        }

    def _replace_badge(self, layout, ok):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        layout.addWidget(StatusBadge(ok))

    # --- 直近の自動修復件数（再設計で新設） ---
    def _build_recent_summary_section(self):
        box, layout = _section_box("直近に自動で直した内容")
        self._recent_summary_label = QtWidgets.QLabel(
            "まだシーンを開いていません。")
        self._recent_summary_label.setWordWrap(True)
        layout.addWidget(self._recent_summary_label)

        # 2026.07.31（ユーザー指摘対応）: 要確認パネルへのアクセス手段が
        # 右下トースト通知のクリックのみだったため、「Scriptエディタの
        # 他のログに埋もれて気づけない/見失う」という指摘を受け、
        # 自己診断画面からもいつでも開けるようこのボタンを新設した。
        # 確認が必要な項目が無い時は非表示にする。
        self._open_review_btn = QtWidgets.QPushButton("確認する")
        self._open_review_btn.setVisible(False)
        self._open_review_btn.clicked.connect(self.open_review_panel_requested.emit)
        layout.addWidget(self._open_review_btn)

        self._content_layout.addWidget(box)

    def set_recent_summary(self, auto_fixed_count, review_count, unresolved_reference_count=0):
        parts = []
        if auto_fixed_count > 0:
            parts.append("{0}件を自動で直しました".format(auto_fixed_count))
        if review_count > 0:
            parts.append("{0}件は確認をお願いしています".format(review_count))
        if unresolved_reference_count > 0:
            parts.append("参照シーン{0}件が見つかっていません".format(
                unresolved_reference_count))
        if not parts:
            text = "直すものは見つかりませんでした（すべて正常です）。"
        else:
            text = "、".join(parts) + "。"
        self._recent_summary_label.setText(text)

        needs_review = (review_count > 0) or (unresolved_reference_count > 0)
        self._open_review_btn.setVisible(needs_review)
        if needs_review:
            total = review_count + unresolved_reference_count
            self._open_review_btn.setText("確認する（{0}件）".format(total))

    # --- 探す場所（探索ルート。追加導線をここに一本化） ---
    def _build_search_roots_section(self):
        box, layout = _section_box("探す場所")

        note = QtWidgets.QLabel(
            "テクスチャ等が見つからない場合、ここにフォルダを追加すると"
            "自動で見つかりやすくなります（例: プロジェクトの sourceimages）。"
            "追加すると自動的に覚えます。件数がいっぱいになると、"
            "しばらく使っていない場所から自動で入れ替わります。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        layout.addWidget(note)

        add_btn = QtWidgets.QPushButton("フォルダを追加...")
        add_btn.clicked.connect(self.add_search_root_requested.emit)
        layout.addWidget(add_btn)

        self._search_roots_layout = QtWidgets.QVBoxLayout()
        layout.addLayout(self._search_roots_layout)
        self._content_layout.addWidget(box)

    def set_search_roots(self, root_infos):
        """
        root_infos: [{"path":..., "exists": bool, "estimated_count": int,
                      "last_used_at": str_or_None}, ...]
        （is_trial は再設計により廃止。渡されても無視する後方互換のみ維持）
        """
        while self._search_roots_layout.count():
            item = self._search_roots_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not root_infos:
            self._search_roots_layout.addWidget(
                QtWidgets.QLabel("まだ何も追加されていません。"))
            return

        for info in root_infos:
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(2, 2, 2, 2)

            label = QtWidgets.QLabel(info["path"])
            label.setWordWrap(True)
            row_layout.addWidget(label, stretch=1)

            exists_badge = StatusBadge(info.get("exists", False))
            row_layout.addWidget(exists_badge)

            count_label = QtWidgets.QLabel(
                "約{0}件".format(info.get("estimated_count", "?")))
            row_layout.addWidget(count_label)

            remove_btn = QtWidgets.QPushButton("削除")
            remove_btn.clicked.connect(
                lambda _checked=False, p=info["path"]:
                self.remove_search_root_requested.emit(p, False))
            row_layout.addWidget(remove_btn)

            self._search_roots_layout.addWidget(row)

    # --- 困ったときは ---
    def _build_help_section(self):
        box, layout = _section_box("困ったときは")
        self._log_label = QtWidgets.QLabel("-")
        self._log_label.setWordWrap(True)
        layout.addWidget(self._log_label)
        open_btn = QtWidgets.QPushButton("記録のフォルダを開く")
        open_btn.clicked.connect(self.open_log_folder_requested.emit)
        layout.addWidget(open_btn)
        self._content_layout.addWidget(box)

    def set_log_dir(self, log_dir):
        self._log_label.setText(
            "うまく直らない場合、この場所に記録が残っています: {0}".format(log_dir))

    # ============================================================
    # 第2階層（「もっと詳しく」の中）
    # ============================================================

    # --- 設定の読込結果 ---
    def _build_config_section(self):
        box, layout = _section_box("設定の読込結果")
        self._config_label = QtWidgets.QLabel("-")
        self._config_label.setWordWrap(True)
        layout.addWidget(self._config_label)
        self._config_badge_layout = QtWidgets.QVBoxLayout()
        layout.addLayout(self._config_badge_layout)
        self._details.add_widget(box)

    def set_config_report(self, sources, parse_errors, clamp_events):
        lines = []
        lines.append("読み込まれたファイル: {0}".format(
            ", ".join(sources) if sources else "（なし。既定値を使用中）"))
        if parse_errors:
            lines.append("")
            lines.append("パースエラー（既定値へフォールバック）:")
            for path, err in parse_errors:
                lines.append("  - {0}: {1}".format(path, err))
        if clamp_events:
            lines.append("")
            lines.append("設定値を調整しました:")
            for ev in clamp_events:
                lines.append("  - {0}".format(ev))
        self._config_label.setText("\n".join(lines))
        ok = not parse_errors and not clamp_events
        self._replace_badge(self._config_badge_layout, ok)

    # --- マッピング表 ---
    def _build_mappings_section(self):
        box, layout = _section_box("マッピング表（フォルダの読み替え設定）")
        self._mappings_layout = QtWidgets.QVBoxLayout()
        layout.addLayout(self._mappings_layout)
        self._details.add_widget(box)

    def set_mappings(self, mapping_infos):
        """mapping_infos: [{"from":..., "to":..., "to_exists": bool}, ...]"""
        while self._mappings_layout.count():
            item = self._mappings_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not mapping_infos:
            self._mappings_layout.addWidget(QtWidgets.QLabel("マッピングは設定されていません。"))
            return

        for info in mapping_infos:
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            label = QtWidgets.QLabel("{0}  ->  {1}".format(info["from"], info["to"]))
            label.setWordWrap(True)
            row_layout.addWidget(label, stretch=1)
            row_layout.addWidget(StatusBadge(info.get("to_exists", False)))
            self._mappings_layout.addWidget(row)

    # --- インデックス ---
    def _build_index_section(self):
        box, layout = _section_box("インデックス（ファイル一覧の作成）")
        self._index_label = QtWidgets.QLabel("-")
        layout.addWidget(self._index_label)
        self._index_badge_layout = QtWidgets.QVBoxLayout()
        layout.addLayout(self._index_badge_layout)
        self._details.add_widget(box)

    def set_index_status(self, built, file_count, timed_out):
        if not built:
            text = "まだ構築されていません（正常なシーンでは構築されないのが仕様です）。"
        else:
            text = "構築済み: {0} 件{1}".format(
                file_count, "（タイムアウトにより不完全）" if timed_out else "")
        self._index_label.setText(text)
        self._replace_badge(self._index_badge_layout, not timed_out)

    # --- dirmap ---
    def _build_dirmap_section(self):
        box, layout = _section_box("dirmap（フォルダの読み替え設定・実行時）")
        self._dirmap_label = QtWidgets.QLabel("-")
        layout.addWidget(self._dirmap_label)

        dry_run_layout = QtWidgets.QHBoxLayout()
        self._dry_run_input = QtWidgets.QLineEdit()
        self._dry_run_input.setPlaceholderText("確認したいパスを入力...")
        dry_run_layout.addWidget(self._dry_run_input, stretch=1)
        dry_run_btn = QtWidgets.QPushButton("置換結果を確認")
        dry_run_btn.clicked.connect(
            lambda: self.dirmap_dry_run_requested.emit(self._dry_run_input.text()))
        dry_run_layout.addWidget(dry_run_btn)
        layout.addLayout(dry_run_layout)

        self._dry_run_result = QtWidgets.QLabel("")
        self._dry_run_result.setWordWrap(True)
        layout.addWidget(self._dry_run_result)

        self._details.add_widget(box)

    def set_dirmap_status(self, enabled, mapping_count):
        self._dirmap_label.setText(
            "有効: {0} / 登録件数: {1}".format("はい" if enabled else "いいえ", mapping_count))

    def set_dry_run_result(self, result_path):
        self._dry_run_result.setText("結果: {0}".format(result_path))

    # --- 重複インストール ---
    def _build_duplicate_install_section(self):
        box, layout = _section_box("重複インストール")
        self._duplicate_label = QtWidgets.QLabel("-")
        self._duplicate_label.setWordWrap(True)
        layout.addWidget(self._duplicate_label)
        self._duplicate_badge_layout = QtWidgets.QVBoxLayout()
        layout.addLayout(self._duplicate_badge_layout)
        self._details.add_widget(box)

    def set_duplicate_installs(self, mod_paths):
        ok = len(mod_paths) <= 1
        if ok:
            self._duplicate_label.setText("重複は検出されませんでした。")
        else:
            self._duplicate_label.setText(
                "複数の AnyPath.mod が検出されました:\n" + "\n".join(mod_paths))
        self._replace_badge(self._duplicate_badge_layout, ok)

    # --- Gate 0 観測状況（拡張セクション） ---
    def _build_gate0_section(self):
        box, layout = _section_box("Gate 0 観測状況（実測確認用・任意）")
        note = QtWidgets.QLabel(
            "この画面はGate 0の結論を自動判定しません。実機で操作した際の"
            "観測データが溜まっているかどうかのみを表示します。"
            "詳細は Gate0_検証手順.md を参照してください。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        layout.addWidget(note)

        self._gate0_label = QtWidgets.QLabel("-")
        self._gate0_label.setWordWrap(True)
        font = QtGui.QFont("Courier New")
        self._gate0_label.setFont(font)
        layout.addWidget(self._gate0_label)

        self._details.add_widget(box)

    def set_gate0_report(self, report_text):
        self._gate0_label.setText(report_text or "観測データがまだありません。")
