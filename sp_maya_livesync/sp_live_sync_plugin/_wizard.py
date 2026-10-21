"""初回セットアップウィザード(SetupWizard)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

from ._qt import QtCore, QtWidgets
from ._settings_model import RESOLUTION_CHOICES, _duplicate_folder_warning

__all__ = [
    "SetupWizard",
]


# ---------------------------------------------------------------------------
# Phase 3: 初回セットアップウィザード
# ---------------------------------------------------------------------------
#
# 他人にこのパイプラインを共有した際、フォルダ設定の意味が分からず
# 迷わないよう、対話形式で最低限の設定を案内するウィザード。
# ここで行える設定はLiveSyncPanelのGUIからいつでも変更でき、ウィザードは
# あくまで初回導入を分かりやすくするための補助という位置づけ。

class SetupWizard(QtWidgets.QWizard):

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("SP Live Sync 初期セットアップ")
        self.setMinimumSize(560, 420)
        # QWizardの既定スタイル(Windowsだと ModernStyle/AeroStyle)は、
        # 上部に白背景のバナー領域を独自描画するため、Painterのダーク
        # テーマと合わずに浮いて見える。ClassicStyleはこのバナー領域を
        # 描画しないため、ホストアプリのテーマに馴染みやすい。
        self.setWizardStyle(QtWidgets.QWizard.ClassicStyle)

        self.addPage(self._welcome_page())
        self.addPage(self._folder_page())
        self.addPage(self._quality_page())
        self.addPage(self._finish_page())

    def _welcome_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("ようこそ")
        layout = QtWidgets.QVBoxLayout(page)
        label = QtWidgets.QLabel(
            "このウィザードでは、Substance Painterで編集したテクスチャを"
            "Mayaへ自動反映するための最低限の設定を行います。\n\n"
            "設定完了後、Maya側でも同じ「監視フォルダ」を指定する必要が"
            "あります。最後の画面に表示されるフォルダパスを、Maya側の"
            "セットアップウィザードにそのまま入力してください。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        return page

    def _folder_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("フォルダ設定")
        page.setSubTitle("既定値のままで問題なければ、そのまま「次へ」を押してください。")
        layout = QtWidgets.QFormLayout(page)

        cfg = self.engine.config
        self.staging_edit = QtWidgets.QLineEdit(cfg.get("staging_dir", ""))
        self.watch_edit = QtWidgets.QLineEdit(cfg.get("watch_dir", ""))
        self.final_edit = QtWidgets.QLineEdit(cfg.get("final_export_dir", ""))

        for label_text, edit in (
            ("ステージングフォルダ", self.staging_edit),
            ("監視フォルダ (★ Maya側と一致させる)", self.watch_edit),
            ("最終出力フォルダ (保存時)", self.final_edit),
        ):
            row = QtWidgets.QHBoxLayout()
            row.addWidget(edit)
            browse_btn = QtWidgets.QPushButton("参照...")
            browse_btn.clicked.connect(lambda checked=False, e=edit: self._browse(e))
            row.addWidget(browse_btn)
            container = QtWidgets.QWidget()
            container.setLayout(row)
            layout.addRow(label_text, container)

        return page

    def _browse(self, line_edit):
        current = line_edit.text() or "C:/"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "フォルダを選択", current)
        if selected:
            line_edit.setText(selected)

    def _quality_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("プレビュー品質")
        layout = QtWidgets.QFormLayout(page)

        self.resolution_combo = QtWidgets.QComboBox()
        for label, _log2 in RESOLUTION_CHOICES:
            self.resolution_combo.addItem(label)
        target_log2 = int(self.engine.config.get("preview_resolution_log2", 9))
        for i, (_label, log2) in enumerate(RESOLUTION_CHOICES):
            if log2 == target_log2:
                self.resolution_combo.setCurrentIndex(i)
                break
        layout.addRow("プレビュー解像度\n(大きいほど同期が重くなります)", self.resolution_combo)

        self.debounce_spin = QtWidgets.QDoubleSpinBox()
        self.debounce_spin.setRange(0.2, 10.0)
        self.debounce_spin.setSingleStep(0.1)
        self.debounce_spin.setSuffix(" 秒")
        self.debounce_spin.setValue(float(self.engine.config.get("debounce_seconds", 1.5)))
        layout.addRow("デバウンス間隔\n(編集停止からMaya反映までの待ち時間)", self.debounce_spin)

        return page

    def _finish_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("完了")
        layout = QtWidgets.QVBoxLayout(page)
        self.summary_label = QtWidgets.QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        page.initializePage = self._update_summary
        return page

    def _update_summary(self):
        watch_dir = self.watch_edit.text().strip() or self.engine.config.get("watch_dir", "")
        self.summary_label.setText(
            "「完了」を押すと設定を保存し、Live Syncを有効化します。\n\n"
            "Maya側のセットアップウィザードでは、次の「監視フォルダ」を"
            "そのまま入力してください:\n\n{0}".format(watch_dir)
        )

    def accept(self):
        new_values = {
            "staging_dir": self.staging_edit.text().strip(),
            "watch_dir": self.watch_edit.text().strip(),
            "final_export_dir": self.final_edit.text().strip(),
            "preview_resolution_log2": RESOLUTION_CHOICES[self.resolution_combo.currentIndex()][1],
            "debounce_seconds": self.debounce_spin.value(),
            "setup_wizard_completed": True,
        }
        for key in ("staging_dir", "watch_dir", "final_export_dir"):
            if not new_values[key]:
                QtWidgets.QMessageBox.warning(self, "Live Sync", "フォルダが未指定です: {0}".format(key))
                return
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
                return
        self.engine.apply_and_save_config(new_values)
        super().accept()
