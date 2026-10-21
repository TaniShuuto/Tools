"""ドックパネル(LiveSyncPanel)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os
import datetime

import substance_painter.project as project

from ._qt import QtCore, QtWidgets
from ._version import __version__
from ._base import HISTORY_LOG_PATH, _log
from ._settings_model import (
    RESOLUTION_CHOICES,
    _PANEL_SETTING_LABELS,
    _duplicate_folder_warning,
    unsaved_setting_keys,
)
from ._wizard import SetupWizard

__all__ = [
    "LiveSyncPanel",
]


# ---------------------------------------------------------------------------
# GUI: ドッキングパネル
# ---------------------------------------------------------------------------

class LiveSyncPanel(QtWidgets.QWidget):

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        # 2026.10.19(1.3.1): 設定読込中は未保存判定を止める。保存成功時刻は起動直後は空。
        self._loading_panel_values = False
        self._last_save_time = None
        self.setObjectName("SPMayaLiveSyncPanel")
        # パネルのタイトルにもバージョンを出し、ログを遡らなくても
        # 現在動作中のバージョンが一目で分かるようにする
        # (maya_live_sync.py のウィンドウタイトルと同じ方式)。
        self.setWindowTitle("Live Sync to Maya  (v{0})".format(__version__))

        layout = QtWidgets.QVBoxLayout(self)

        # --- 層1: 最頻操作 -----------------------------------------------
        # UI導線改善(フェーズ1): 「1セッション中に何度も触るか」を基準に
        # 層1(最頻・大きく最上段固定)/層2(時々・常時表示だが控えめ)/
        # 層3(稀・折りたたみやメニューの奥)の3段階に分ける。
        # maya_live_sync.py の LiveSyncWindow と同じ考え方で揃えることで、
        # SP側・Maya側の操作感覚を統一する。色による区別は環境依存の
        # 配色崩れを避けるため使わず、サイズと配置順序のみで表現する。
        self.enable_checkbox = QtWidgets.QCheckBox("Live Sync を有効にする")
        enable_font = self.enable_checkbox.font()
        enable_font.setBold(True)
        enable_font.setPointSize(enable_font.pointSize() + 1)
        self.enable_checkbox.setFont(enable_font)
        self.enable_checkbox.toggled.connect(self.engine.set_enabled)
        layout.addWidget(self.enable_checkbox)

        # 2026.07.19-02: 書き出し中(SPネイティブの「書き出し中」ダイアログが
        # 消えた後の後処理を含む)であることを示す警告ラベル。実機の
        # クラッシュ再現テストで、このダイアログが消えた後もしばらく
        # 閉じるとクラッシュすることが確認されたため、Pythonコードが
        # 動いている区間全体を通して表示する。根本的な解決ではないが、
        # 人がうっかり閉じてしまう事故を減らすための保険。
        self.exporting_warning_label = QtWidgets.QLabel(
            "⚠ 書き出し処理中です。完了までPainterを閉じないでください。"
        )
        self.exporting_warning_label.setStyleSheet(
            "color: #ffffff; background-color: #b34700; padding: 4px; border-radius: 3px;"
        )
        self.exporting_warning_label.setVisible(False)
        layout.addWidget(self.exporting_warning_label)

        # 2026.07.19-03(フェーズ2): 「保存内容がまだFinal(高画質)側に
        # 反映されていない」ことを示す、より控えめな情報表示。クールダウン
        # 待ち・ロック解除待ちの再試行中も含めて表示される。上の警告
        # (書き出し処理中)とは異なり、閉じても危険ではないので警告色は
        # 使わない。
        self.final_pending_label = QtWidgets.QLabel(
            "⏳ Final(高画質)同期待ち: 保存内容がまだMaya側に反映されていません。"
        )
        self.final_pending_label.setStyleSheet(
            "color: #ffffff; background-color: #3a5a78; padding: 4px; border-radius: 3px;"
        )
        self.final_pending_label.setVisible(False)
        layout.addWidget(self.final_pending_label)

        # UI導線改善(フェーズ1): 「今すぐ同期」は保存を待たずに手元の
        # 変更をすぐ反映したい時に押す操作で、層1の有効化チェックボックスに
        # 次いで頻度が高い。他のボタン群より一段階目立つ高さにして
        # 単独行に配置する。
        # 2026.10.19(1.3.1): 未オープン案内と未保存確認を挟むため、
        # engine.force_sync_now を直接接続せずパネル側ハンドラ経由にする。
        self.sync_now_btn = QtWidgets.QPushButton("今すぐ同期")
        self.sync_now_btn.setMinimumHeight(32)
        self.sync_now_btn.clicked.connect(self._on_sync_now_clicked)
        layout.addWidget(self.sync_now_btn)

        # フォルダパス・解像度・デバウンス間隔は基本的に初回セットアップ時に
        # 一度決めれば済む項目のため、折りたたみ可能なグループにまとめて
        # 日常的に使う操作(監視ON/OFF・保存・同期履歴)から視覚的に分離する。
        self.settings_group = QtWidgets.QGroupBox("フォルダ・詳細設定(通常は初回のみ)")
        self.settings_group.setCheckable(True)
        self.settings_group.setChecked(True)
        self.settings_group.toggled.connect(self._on_settings_group_toggled)
        form = QtWidgets.QFormLayout(self.settings_group)
        self.staging_edit = self._make_path_row(form, "ステージングフォルダ")
        self.watch_edit = self._make_path_row(form, "監視フォルダ(プレビュー)")
        self.final_edit = self._make_path_row(form, "最終出力フォルダ(保存時)")

        self.resolution_combo = QtWidgets.QComboBox()
        for label, _ in RESOLUTION_CHOICES:
            self.resolution_combo.addItem(label)
        form.addRow("プレビュー解像度", self.resolution_combo)

        self.final_resolution_combo = QtWidgets.QComboBox()
        for label, _ in RESOLUTION_CHOICES:
            self.final_resolution_combo.addItem(label)
        self.final_resolution_combo.setToolTip(
            "保存時にfinal_export_dirへ書き出す高画質版の解像度です。"
            "プロジェクト自体の書き出し設定とは独立した、このパイプライン専用の設定です。"
        )
        form.addRow("最終出力解像度(高画質)", self.final_resolution_combo)

        self.debounce_spin = QtWidgets.QDoubleSpinBox()
        self.debounce_spin.setRange(0.2, 10.0)
        self.debounce_spin.setSingleStep(0.1)
        self.debounce_spin.setSuffix(" 秒")
        form.addRow("デバウンス間隔", self.debounce_spin)

        # 2026.07.19-03(フェーズ2): 保存連打の間引き用クールダウン。
        self.final_cooldown_spin = QtWidgets.QDoubleSpinBox()
        self.final_cooldown_spin.setRange(0.0, 60.0)
        self.final_cooldown_spin.setSingleStep(1.0)
        self.final_cooldown_spin.setSuffix(" 秒")
        self.final_cooldown_spin.setToolTip(
            "前回のFinal(高画質)書き出し完了からこの秒数が経過するまで、"
            "次のFinal書き出しを保留します。保存を連打した際に、重い書き出しが"
            "連続で走り続けるのを防ぎます。0にするとクールダウンなし(従来の挙動)。"
        )
        form.addRow("Final書き出しのクールダウン", self.final_cooldown_spin)

        layout.addWidget(self.settings_group)

        # --- 層2: 時々使う操作 / 層3: 稀な操作 ----------------------------
        # UI導線改善(フェーズ1): 保存・履歴・全量同期は「たまに触る」層2、
        # ウィザードは「初回セットアップ時に一度触ればよい」層3として、
        # maya_live_sync.py の more_btn と同じ「その他の設定」メニュー方式に
        # まとめる。今すぐ同期(層1)を主要ボタン列から独立させたことで、
        # このボタン列は全て層2以下の操作のみになる。
        btn_row = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("設定を保存")
        self.save_btn.clicked.connect(self._on_save_clicked)
        # 2026.10.19(1.3.1): 設定グループを折りたたんでも見えるよう、
        # 保存ボタン列(btn_row)の save_btn 直後に未保存/保存済みの状態を置く。
        self.save_state_label = QtWidgets.QLabel("")
        self.save_state_label.setWordWrap(True)
        self.history_btn = QtWidgets.QPushButton("同期履歴を開く")
        self.history_btn.setToolTip("SP側/Maya側共通の同期履歴ログ(テキストファイル)を開きます。")
        self.history_btn.clicked.connect(self._open_history_log)
        # 2026.07.19-03(フェーズ2): Final書き出しを差分化したことに伴う
        # 手動の逃げ道。差分状態がMaya側の実ファイルとズレたと感じた場合、
        # このボタンでクールダウンを無視した即時の全量Final書き出しができる。
        self.force_full_btn = QtWidgets.QPushButton("今すぐ全量同期(Final)")
        self.force_full_btn.setToolTip(
            "差分に関わらず、全テクスチャセットのFinal(高画質)書き出しを"
            "クールダウン無視で即座に実行します。差分書き出しの内容が"
            "Maya側とズレていると感じた場合の手動リセット用です。"
        )
        # 2026.10.19(1.3.1): 未オープン案内と未保存確認を挟むため、
        # engine.force_full_final_sync を直接接続せずパネル側ハンドラ経由にする。
        self.force_full_btn.clicked.connect(self._on_force_full_clicked)

        self.more_btn = QtWidgets.QToolButton()
        self.more_btn.setText("その他の設定")
        self.more_btn.setToolTip("セットアップウィザートなど、初回導入時に主に使う操作です。")
        self.more_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.more_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        more_menu = QtWidgets.QMenu(self.more_btn)
        wizard_action = more_menu.addAction("セットアップウィザートを開く")
        wizard_action.setToolTip("フォルダ設定・解像度などを対話形式で見直せます。")
        wizard_action.triggered.connect(self.open_setup_wizard)
        self.more_btn.setMenu(more_menu)

        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.save_state_label)
        btn_row.addWidget(self.history_btn)
        btn_row.addWidget(self.force_full_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.more_btn)
        layout.addLayout(btn_row)

        stats_group = QtWidgets.QGroupBox("同期状況")
        stats_layout = QtWidgets.QFormLayout(stats_group)
        self.last_sync_label = QtWidgets.QLabel("-")
        self.sync_count_label = QtWidgets.QLabel("0")
        self.skip_count_label = QtWidgets.QLabel("0")
        self.error_count_label = QtWidgets.QLabel("0")
        self.duration_label = QtWidgets.QLabel("-")
        stats_layout.addRow("最終同期時刻", self.last_sync_label)
        stats_layout.addRow("同期回数", self.sync_count_label)
        stats_layout.addRow("スキップ回数(差分無し)", self.skip_count_label)
        stats_layout.addRow("エラー回数", self.error_count_label)
        stats_layout.addRow("直近の処理時間", self.duration_label)
        layout.addWidget(stats_group)

        # UI導線改善(フェーズ1再実施): 「テクスチャセット構成」は
        # Maya側との対応関係に疑問を感じた時に確認する層2寄りの情報で、
        # 常時展開しておく必要性は同期状況ほど高くない。
        # maya_live_sync.py 側の material_tab に既に常設のテーブルが
        # あるため、SP側はあくまで補助確認用として折りたたみにする。
        self.structure_toggle_btn = QtWidgets.QPushButton("▸ テクスチャセット構成を確認（Maya側の対応確認用）")
        self.structure_toggle_btn.setFlat(True)
        self.structure_toggle_btn.clicked.connect(self._on_structure_toggle_clicked)
        layout.addWidget(self.structure_toggle_btn)

        self.structure_section = QtWidgets.QWidget()
        structure_section_layout = QtWidgets.QVBoxLayout(self.structure_section)
        structure_section_layout.setContentsMargins(0, 0, 0, 0)
        self.structure_list = QtWidgets.QListWidget()
        self.structure_list.setMaximumHeight(120)
        structure_section_layout.addWidget(self.structure_list)
        recheck_btn = QtWidgets.QPushButton("構成を今すぐチェック")
        recheck_btn.clicked.connect(self.engine.check_texture_set_structure)
        structure_section_layout.addWidget(recheck_btn)
        self.structure_section.setVisible(False)
        layout.addWidget(self.structure_section)

        # UI導線改善(フェーズ1再実施): ログは常時大きく表示する必要は
        # なく、udim_setup.py の折りたたみログと同じ考え方で既定は
        # 閉じておく。ただしエラー発生時に見落とすと困るため、
        # status_changed で受け取ったメッセージにエラーを示す文言が
        # 含まれる場合は自動的に開く(_on_log_message側で判定)。
        self.log_toggle_btn = QtWidgets.QPushButton("▸ ログを表示")
        self.log_toggle_btn.setFlat(True)
        self.log_toggle_btn.clicked.connect(self._on_log_toggle_clicked)
        layout.addWidget(self.log_toggle_btn)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setVisible(False)
        layout.addWidget(self.log_view, stretch=1)

        self._connect_unsaved_watchers()
        self._load_values_from_config()
        self._refresh_structure_list([], [], [])

        self.engine.status_changed.connect(self._on_log_message)
        self.engine.stats_changed.connect(self._on_stats_changed)
        self.engine.structure_changed.connect(self._refresh_structure_list)
        self.engine.exporting_changed.connect(self.exporting_warning_label.setVisible)
        self.engine.final_pending_changed.connect(self.final_pending_label.setVisible)

    def _make_path_row(self, form, label):
        row = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QLineEdit()
        browse_btn = QtWidgets.QPushButton("参照...")
        browse_btn.clicked.connect(lambda: self._browse_folder(edit))
        row.addWidget(edit)
        row.addWidget(browse_btn)
        container = QtWidgets.QWidget()
        container.setLayout(row)
        form.addRow(label, container)
        return edit

    def _on_settings_group_toggled(self, checked):
        # チェックを外すと中身を折りたたんで(非表示にして)場所を取らないように
        # する。QGroupBoxの標準動作(チェック解除時に子をdisableするだけ)とは
        # 別に、行そのものを隠すことで見た目もすっきりさせる。
        form = self.settings_group.layout()
        for row in range(form.rowCount()):
            for role in (QtWidgets.QFormLayout.LabelRole, QtWidgets.QFormLayout.FieldRole):
                item = form.itemAt(row, role)
                if item is not None and item.widget() is not None:
                    item.widget().setVisible(checked)

    def _on_structure_toggle_clicked(self):
        # UI導線改善(フェーズ1再実施): QPushButton.clicked は bool を
        # 渡すが、ここでは受け取らずに現在の表示状態を見て自前で反転する。
        # maya_live_sync.py の _on_orphan_toggle_clicked と同じ設計。
        self._set_structure_section_visible(not self.structure_section.isVisible())

    def _set_structure_section_visible(self, visible):
        self.structure_section.setVisible(visible)
        self.structure_toggle_btn.setText(
            "▾ テクスチャセット構成を確認（Maya側の対応確認用）" if visible
            else "▸ テクスチャセット構成を確認（Maya側の対応確認用）"
        )

    def _on_log_toggle_clicked(self):
        self._set_log_visible(not self.log_view.isVisible())

    def _set_log_visible(self, visible):
        self.log_view.setVisible(visible)
        self.log_toggle_btn.setText("▾ ログを表示" if visible else "▸ ログを表示")

    def _on_log_message(self, message):
        # UI導線改善(フェーズ1再実施): ログは既定で折りたたんでいるが、
        # エラーや警告を見落とすとトラブル対応が遅れるため、該当する
        # 文言を含むメッセージを受け取った場合は自動的に開く。
        # udim_setup.py の _print() が [WARN]/[NG] で自動展開する
        # パターンと同じ考え方。
        self.log_view.appendPlainText(message)
        if not self.log_view.isVisible():
            lowered = message.lower()
            if "エラー" in message or "失敗" in message or "warning" in lowered or "error" in lowered:
                self._set_log_visible(True)

    def _browse_folder(self, line_edit):
        current = line_edit.text() or "C:/"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "フォルダを選択", current)
        if selected:
            line_edit.setText(selected)

    def _load_values_from_config(self):
        # 2026.10.19(1.3.1): 読込中の setText 等で未保存判定が走らないよう止める。
        self._loading_panel_values = True
        try:
            cfg = self.engine.config
            self.staging_edit.setText(cfg.get("staging_dir", ""))
            self.watch_edit.setText(cfg.get("watch_dir", ""))
            self.final_edit.setText(cfg.get("final_export_dir", ""))

            target_log2 = int(cfg.get("preview_resolution_log2", 9))
            for i in range(len(RESOLUTION_CHOICES)):
                log2 = RESOLUTION_CHOICES[i][1]
                if log2 == target_log2:
                    self.resolution_combo.setCurrentIndex(i)
                    break

            target_final_log2 = int(cfg.get("final_resolution_log2", 12))
            for i in range(len(RESOLUTION_CHOICES)):
                log2 = RESOLUTION_CHOICES[i][1]
                if log2 == target_final_log2:
                    self.final_resolution_combo.setCurrentIndex(i)
                    break

            self.debounce_spin.setValue(float(cfg.get("debounce_seconds", 1.5)))
            self.final_cooldown_spin.setValue(float(cfg.get("final_cooldown_seconds", 5.0)))
        finally:
            self._loading_panel_values = False
        self._refresh_unsaved_state()

    def _collect_panel_values(self):
        # 2026.10.19(1.3.1): 保存と未保存判定で同じ画面値を使う。
        selected_log2 = RESOLUTION_CHOICES[self.resolution_combo.currentIndex()][1]
        selected_final_log2 = RESOLUTION_CHOICES[self.final_resolution_combo.currentIndex()][1]
        return {
            "staging_dir": self.staging_edit.text().strip(),
            "watch_dir": self.watch_edit.text().strip(),
            "final_export_dir": self.final_edit.text().strip(),
            "preview_resolution_log2": selected_log2,
            "final_resolution_log2": selected_final_log2,
            "debounce_seconds": self.debounce_spin.value(),
            "final_cooldown_seconds": self.final_cooldown_spin.value(),
        }

    def _connect_unsaved_watchers(self):
        # 2026.10.19(1.3.1): 画面の値が変わったら未保存表示を更新する。
        self.staging_edit.textChanged.connect(self._refresh_unsaved_state)
        self.watch_edit.textChanged.connect(self._refresh_unsaved_state)
        self.final_edit.textChanged.connect(self._refresh_unsaved_state)
        self.resolution_combo.currentIndexChanged.connect(self._refresh_unsaved_state)
        self.final_resolution_combo.currentIndexChanged.connect(self._refresh_unsaved_state)
        self.debounce_spin.valueChanged.connect(self._refresh_unsaved_state)
        self.final_cooldown_spin.valueChanged.connect(self._refresh_unsaved_state)

    def _refresh_unsaved_state(self, *args):
        if self._loading_panel_values:
            return
        dirty = unsaved_setting_keys(self._collect_panel_values(), self.engine.config)
        if dirty:
            self.save_state_label.setText(
                "● 未保存の変更があります(書き出しには保存済みの値が使われます)"
            )
            # 文字色のみ。色だけで意味を伝えないよう、文言に「未保存」を含む。
            self.save_state_label.setStyleSheet("color: #e67e22;")
            labels = [_PANEL_SETTING_LABELS.get(key, key) for key in dirty]
            self.save_state_label.setToolTip("、".join(labels))
            return
        if self._last_save_time:
            self.save_state_label.setText("保存しました({0})".format(self._last_save_time))
            self.save_state_label.setStyleSheet("")
            self.save_state_label.setToolTip("")
            return
        self.save_state_label.setText("")
        self.save_state_label.setStyleSheet("")
        self.save_state_label.setToolTip("")

    def _on_save_clicked(self):
        new_values = self._collect_panel_values()
        for key in ("staging_dir", "watch_dir", "final_export_dir"):
            if not new_values[key]:
                QtWidgets.QMessageBox.warning(self, "Live Sync", "フォルダが未指定です: {0}".format(key))
                return False
        warning = _duplicate_folder_warning(
            new_values["staging_dir"], new_values["watch_dir"], new_values["final_export_dir"]
        )
        if warning:
            reply = QtWidgets.QMessageBox.warning(
                self, "Live Sync", warning,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return False
        try:
            self.engine.apply_and_save_config(new_values)
        except Exception as e:
            # 2026.10.19(1.3.1): 失敗はダイアログとログに出す。未保存表示は残す。
            # Qt スロットから再送出すると Painter が不安定になる恐れがあるため、
            # 握りつぶさず表面化したうえで False を返す。
            _log("error", "設定を保存できませんでした: {0}".format(e))
            QtWidgets.QMessageBox.warning(
                self, "Live Sync",
                "設定を保存できませんでした: {0}".format(e),
            )
            return False
        self._last_save_time = datetime.datetime.now().strftime("%H:%M:%S")
        self._refresh_unsaved_state()
        return True

    def _on_sync_now_clicked(self):
        # 2026.10.19(1.3.1): 未オープン案内と未保存確認を挟んでから同期する。
        self._run_manual_sync(self.engine.force_sync_now)

    def _on_force_full_clicked(self):
        # 2026.10.19(1.3.1): 未オープン案内と未保存確認を挟んでから全量同期する。
        self._run_manual_sync(self.engine.force_full_final_sync)

    def _run_manual_sync(self, action):
        if not project.is_open():
            QtWidgets.QMessageBox.information(
                self, "Live Sync",
                "プロジェクトが開かれていません。プロジェクトを開いてから実行してください。",
            )
            action()
            return
        dirty = unsaved_setting_keys(self._collect_panel_values(), self.engine.config)
        if dirty:
            if not self._confirm_unsaved_manual_sync():
                return
        action()

    def _confirm_unsaved_manual_sync(self):
        """未保存のまま手動同期するときの3択。実行してよければ True。"""
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Live Sync")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(
            "設定に未保存の変更があります。書き出しには保存済みの値が使われます。"
        )
        save_btn = box.addButton("保存して実行", QtWidgets.QMessageBox.AcceptRole)
        skip_btn = box.addButton("保存せずに実行", QtWidgets.QMessageBox.DestructiveRole)
        cancel_btn = box.addButton("キャンセル", QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return bool(self._on_save_clicked())
        if clicked is skip_btn:
            return True
        return False

    def open_setup_wizard(self):
        wizard = SetupWizard(self.engine, self)
        if wizard.exec_() == QtWidgets.QDialog.Accepted:
            # 2026.10.19(1.3.1): ウィザード完了は保存済みなので、未保存表示を戻す。
            self._last_save_time = datetime.datetime.now().strftime("%H:%M:%S")
            self._load_values_from_config()
            # ウィザード完了時にLive Syncを自動的に有効化する。
            # (engine.set_enabled()を直接呼ぶのではなく、チェックボックス側を
            # 更新することで、GUI表示と実際の有効/無効状態のズレを防ぐ)
            self.enable_checkbox.setChecked(True)

    def _open_history_log(self):
        if not os.path.isfile(HISTORY_LOG_PATH):
            QtWidgets.QMessageBox.information(
                self, "Live Sync", "同期履歴はまだありません(同期を開始すると記録され始めます)。"
            )
            return
        try:
            os.startfile(HISTORY_LOG_PATH)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, "Live Sync",
                "履歴ファイルを開けませんでした: {0} (パス: {1})".format(e, HISTORY_LOG_PATH)
            )

    def _on_stats_changed(self, stats):
        self.last_sync_label.setText(stats.get("last_sync_at") or "-")
        self.sync_count_label.setText(str(stats.get("sync_count", 0)))
        self.skip_count_label.setText(str(stats.get("skip_count", 0)))
        self.error_count_label.setText(str(stats.get("error_count", 0)))
        dur = stats.get("last_duration_ms")
        self.duration_label.setText("{0} ms".format(dur) if dur is not None else "-")

    def _refresh_structure_list(self, current, added, removed):
        self.structure_list.clear()
        added_set = set(added)
        removed_set = set(removed)
        for name in current:
            label = "{0} (新規)".format(name) if name in added_set else name
            self.structure_list.addItem(label)
        for name in removed_set:
            self.structure_list.addItem("{0} (削除された可能性)".format(name))
