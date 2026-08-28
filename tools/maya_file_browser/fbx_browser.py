# -*- coding: utf-8 -*-
"""
fbx_browser.py  (rev. 2026.08.21-04)
------------------------------------
Maya内にドッキング可能なアセットブラウザパネル。

rev.04 での追加内容:
    追加. 画像プレビュー（Mayaでプレビュー表示）に矢印キー（←→）での
          前後移動を追加。フォルダ内の対象画像一覧を末尾/先頭で
          循環するため、一度閉じて隣の画像を開き直す手間が無くなる。

rev.03 での追加内容:
    追加. 選択項目（対象外拡張子・フォルダ含む）をOSのごみ箱へ移動する削除機能
          （右クリックメニュー / Deleteキー。完全削除ではなく復元可能な形を既定とする）
    追加. エクスプローラー等の外部からのドラッグ&ドロップによるファイルコピー配置
          （ドロップ元は変更しない。QFileSystemModel既定の「移動」処理は使わない）

rev.02 での修正内容（実務投入時のリスク対応）:
    [高] 1. フォルダ空判定スキャンの打ち切り制限（UIスレッドの長時間ブロック回避）
    [高] 2. Maya起動時の復元処理を遅延実行化＋到達性チェック（起動ハング回避）
    [中] 3. FBXインポート設定の明示的リセット（グローバル状態依存の排除）
    [中] 4. ダブルクリックの既定アクション即実行（オプションで切替可能）
    [中] 5. プレビューダイアログのモードレス化
    [中] 6. アンドゥチャンクによる操作の一括取り消し対応
    [中] 7. 重複Reference・名前空間衝突の検出
         8. .mb の扱いを「対象ファイル」に統一（文言の不整合解消）
         9. ソート時にフォルダを常に先頭へ配置
        10. ファイル名インクリメンタルフィルタの追加
       追加. お気に入りフォルダ機能（プロジェクト切替の実用性向上）
       追加. hideEvent での設定保存を廃止（タブ切替時の過剰書き込み回避）

使い方:
        import fbx_browser
        fbx_browser.show_ui()

    ※ Maya再起動時の自動復元には、本ファイルが PYTHONPATH
      （例: Documents/maya/scripts）上に配置されている必要があります。
"""

import os
import sys
import json
import ctypes
import shutil
import threading
import traceback
import contextlib
import subprocess

# ---------------------------------------------------------------------------
# PySide2 / PySide6 両対応インポート
# ---------------------------------------------------------------------------
try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import wrapInstance
    _QT_BINDING = "PySide6"
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    from shiboken2 import wrapInstance
    _QT_BINDING = "PySide2"

# [追加] QShortcut は Qt6(PySide6)ではQtGuiへ移動しているため、両対応で解決する。
_QShortcutClass = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut

import maya.cmds as cmds
import maya.mel as mel
import maya.OpenMayaUI as omui

try:
    from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
except ImportError:
    class MayaQWidgetDockableMixin(object):
        pass


WINDOW_OBJECT_NAME = "FBXBrowserPanelWindow"
WORKSPACE_CONTROL_NAME = WINDOW_OBJECT_NAME + "WorkspaceControl"
WINDOW_TITLE = "Asset Browser"

SETTINGS_ORG = "NanoTools"
SETTINGS_APP = "AssetBrowser"

COLOR_INFO = "#4073a6"
COLOR_RUN = "#339959"
COLOR_WARN = "#c9822a"
COLOR_ERROR = "#c9432a"

LIVE_SYNC_CONFIG_PATH = "C:/SPMayaLiveSync/live_sync_config.json"

# --- 対象ファイル種別 ---
# [修正8] .mb は mayaBinary として正しくImport/Reference可能なため対象に含める。
# rev.01ではコメント・UIヒント側で「対象外ファイルの例」として誤記されていた。
FBX_EXTENSIONS = {"fbx"}
MAYA_SCENE_EXTENSIONS = {"ma", "mb"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "tga", "exr", "tif", "tiff", "bmp"}
TEXT_EXTENSIONS = {"txt", "json", "py", "md", "xml", "ini", "log", "cfg"}

ALL_TARGET_EXTENSIONS = (
    FBX_EXTENSIONS | MAYA_SCENE_EXTENSIONS | IMAGE_EXTENSIONS | TEXT_EXTENSIONS
)
NAME_FILTER_PATTERNS = ["*.{}".format(ext) for ext in sorted(ALL_TARGET_EXTENSIONS)]

CATEGORY_FBX = "fbx"
CATEGORY_MAYA_SCENE = "maya_scene"
CATEGORY_IMAGE = "image"
CATEGORY_TEXT = "text"
CATEGORY_UNKNOWN = "unknown"

# [修正1] 空判定スキャンで走査するエントリ数の上限。
# これを超えた時点で「判定不能」として打ち切り、通常表示（グレーアウトしない）に倒す。
# Megascansライブラリのような数万ファイル規模のフォルダで描画スレッドが
# 長時間ブロックされる事態を防ぐための安全弁。
DIR_SCAN_ENTRY_LIMIT = 800

# [修正2] フォルダ到達性チェックのタイムアウト秒数。
# 切断済みのネットワークドライブ／UNCパスに対する os.path.isdir() は
# OS側のリトライにより数十秒ブロックすることがあるため、別スレッドで実行し
# この秒数を超えたら「到達不能」として扱う。
PATH_REACHABILITY_TIMEOUT_SEC = 2.0


def _get_file_category(file_path):
    ext = os.path.splitext(file_path)[1].lstrip(".").lower()
    if ext in FBX_EXTENSIONS:
        return CATEGORY_FBX
    if ext in MAYA_SCENE_EXTENSIONS:
        return CATEGORY_MAYA_SCENE
    if ext in IMAGE_EXTENSIONS:
        return CATEGORY_IMAGE
    if ext in TEXT_EXTENSIONS:
        return CATEGORY_TEXT
    return CATEGORY_UNKNOWN


def _get_maya_file_type(file_path):
    ext = os.path.splitext(file_path)[1].lstrip(".").lower()
    if ext in FBX_EXTENSIONS:
        return "FBX"
    if ext == "mb":
        return "mayaBinary"
    return "mayaAscii"


def _sanitize_namespace(name):
    """Mayaの名前空間として使用可能な文字列へ変換する。"""
    sanitized = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    # 名前空間は数字始まりを許容しないため接頭辞を付与する
    if sanitized and sanitized[0].isdigit():
        sanitized = "ns_" + sanitized
    return sanitized or "asset"


def _normalize_for_compare(path):
    """パス比較用の正規化（大文字小文字・区切り文字の差異を吸収）。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _is_path_reachable(path, timeout_sec=PATH_REACHABILITY_TIMEOUT_SEC):
    """
    [修正2] os.path.isdir() を別スレッドで実行し、タイムアウト付きで判定する。

    切断されたネットワークパスに対して isdir() がブロックしても、呼び出し元
    （＝Mayaのメインスレッド）は timeout_sec で必ず制御を取り戻せる。
    ワーカースレッドはデーモンとして放置されるが、OS側の応答後に自然終了する。
    """
    result = {"ok": False}

    def _worker():
        try:
            result["ok"] = os.path.isdir(path)
        except Exception:
            result["ok"] = False

    thread = threading.Thread(target=_worker)
    thread.daemon = True
    thread.start()
    thread.join(timeout_sec)
    if thread.is_alive():
        return False  # タイムアウト＝到達不能とみなす
    return result["ok"]


@contextlib.contextmanager
def _undo_chunk(chunk_name):
    """
    [修正6] 複数のMayaコマンドをアンドゥ1回分にまとめるコンテキストマネージャ。

    例外発生時も必ず closeChunk しないと以降のアンドゥ履歴が壊れ、
    Maya全体の取り消しが効かなくなるため finally で閉じる。
    """
    cmds.undoInfo(openChunk=True, chunkName=chunk_name)
    try:
        yield
    finally:
        cmds.undoInfo(closeChunk=True)


def _guess_default_folder():
    try:
        if os.path.isfile(LIVE_SYNC_CONFIG_PATH):
            with open(LIVE_SYNC_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for key in ("final_dir", "project_dir", "active_project", "root_dir"):
                val = cfg.get(key)
                if val and os.path.isdir(val):
                    return val
    except Exception:
        pass

    try:
        proj_dir = cmds.workspace(q=True, rootDirectory=True)
        if proj_dir and os.path.isdir(proj_dir):
            return proj_dir
    except Exception:
        pass

    return ""


# ---------------------------------------------------------------------------
# [追加] ごみ箱への移動（削除機能）
# ---------------------------------------------------------------------------
# pip install不可の環境（Maya同梱Pythonのみ）のため、send2trash等は使わず
# 標準ライブラリ（ctypes / subprocess）のみでOSごとに実装する。
# いずれの実装も失敗時は例外を送出するだけで、完全削除へのフォールバックは
# 行わない（ごみ箱に移せないなら削除自体を中止するのが安全側の方針）。

def _send_to_trash_windows(path):
    """SHFileOperationW(FO_DELETE, FOF_ALLOWUNDO)でごみ箱へ移動する。"""

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 0x0003
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_NOERRORUI = 0x0400
    FOF_SILENT = 0x0004

    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    # pFrom はダブルNUL終端のリスト形式。単一パスでも末尾にNULが1つ必要。
    op.pFrom = path + "\0"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    op.fAnyOperationsAborted = 0
    op.hNameMappings = None
    op.lpszProgressTitle = None

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0:
        raise OSError("SHFileOperationW failed (code=0x{:x})".format(result))
    if op.fAnyOperationsAborted:
        raise OSError("ごみ箱への移動が中断されました")


def _send_to_trash_mac(path):
    """Finder経由でゴミ箱へ移動する。"""
    script = 'tell application "Finder" to delete POSIX file "{}"'.format(
        path.replace("\\", "\\\\").replace('"', '\\"')
    )
    proc = subprocess.Popen(
        ["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    _out, err = proc.communicate()
    if proc.returncode != 0:
        raise OSError((err or b"").decode("utf-8", "ignore").strip() or "osascript failed")


def _send_to_trash_linux(path):
    """gio trash経由でごみ箱へ移動する（コマンドが無い環境では失敗させる）。"""
    try:
        proc = subprocess.Popen(
            ["gio", "trash", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError:
        raise OSError("gio コマンドが見つかりません（ごみ箱への移動に非対応の環境です）")
    _out, err = proc.communicate()
    if proc.returncode != 0:
        raise OSError((err or b"").decode("utf-8", "ignore").strip() or "gio trash failed")


def _send_to_trash(path):
    """
    OSのごみ箱へファイル/フォルダを移動する。

    完全削除ではなくごみ箱への移動を既定とすることで、誤操作時にOS側から
    復元できるようにする。プラットフォーム別の実装詳細は上記の
    _send_to_trash_windows/_mac/_linux を参照。
    """
    if sys.platform.startswith("win"):
        _send_to_trash_windows(path)
    elif sys.platform == "darwin":
        _send_to_trash_mac(path)
    else:
        _send_to_trash_linux(path)


class AssetFileFilterProxyModel(QtCore.QSortFilterProxyModel):
    """
    表示制御を担当するプロキシモデル。

    責務:
        - [修正10] ファイル名フィルタ（フォルダは常に表示し、ファイルのみ絞り込む）
        - [修正9]  ソート時にフォルダを常に先頭へ配置
        - [修正1]  グレーアウト判定（走査は上限付きで打ち切る）
    """

    def __init__(self, parent=None):
        super(AssetFileFilterProxyModel, self).__init__(parent)
        # 値は True / False / None(判定不能) の3状態を取る
        self._contains_target_cache = {}
        self.include_subfolders = True
        self._name_filter = ""

    def set_include_subfolders(self, include_subfolders):
        if self.include_subfolders != include_subfolders:
            self.include_subfolders = include_subfolders
            self.invalidate_cache()
            self.invalidateFilter()

    def set_name_filter(self, text):
        """[修正10] ファイル名の部分一致フィルタ（大文字小文字を区別しない）。"""
        new_filter = (text or "").strip().lower()
        if self._name_filter != new_filter:
            self._name_filter = new_filter
            self.invalidateFilter()

    def invalidate_cache(self):
        self._contains_target_cache = {}

    def _dir_contains_target(self, dir_path):
        """
        [修正1] 上限付きの空判定スキャン。

        戻り値:
            True  - 対象ファイルが存在する
            False - 上限内で走査し切ったが対象ファイルは存在しなかった
            None  - 走査量が上限を超えたため判定不能（グレーアウトしない）

        rev.01では QDirIterator に name filter を渡していたため、
        「対象ファイルが1つも無い巨大フォルダ」ほど全走査が必要になり、
        最悪ケースで描画が長時間停止する構造になっていた。
        ここではフィルタを外して自前で判定し、走査件数で打ち切る。
        """
        cache_key = (dir_path, self.include_subfolders)
        if cache_key in self._contains_target_cache:
            return self._contains_target_cache[cache_key]

        result = False
        try:
            iterator_flags = (
                QtCore.QDirIterator.Subdirectories
                if self.include_subfolders
                else QtCore.QDirIterator.NoIteratorFlags
            )
            it = QtCore.QDirIterator(
                dir_path,
                QtCore.QDir.Files | QtCore.QDir.NoDotAndDotDot,
                iterator_flags,
            )
            scanned = 0
            while it.hasNext():
                it.next()
                scanned += 1
                ext = os.path.splitext(it.fileName())[1].lstrip(".").lower()
                if ext in ALL_TARGET_EXTENSIONS:
                    result = True
                    break
                if scanned >= DIR_SCAN_ENTRY_LIMIT:
                    result = None  # 判定不能として打ち切る
                    break
        except Exception:
            # スキャン失敗時は安全側（通常表示）に倒す
            result = None

        self._contains_target_cache[cache_key] = result
        return result

    def filterAcceptsRow(self, source_row, source_parent):
        """
        フォルダは常に表示する（rev.01のコメント通り、ルート表示中のフォルダが
        弾かれると setRootIndex が壊れるため、この方針は維持する）。
        ファイルのみ名前フィルタの対象とする。
        """
        if not self._name_filter:
            return True

        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        if not index.isValid():
            return True
        if model.isDir(index):
            return True
        return self._name_filter in model.fileName(index).lower()

    def lessThan(self, left, right):
        """[修正9] どの列でソートしてもフォルダを常にファイルより上へ配置する。"""
        model = self.sourceModel()
        left_is_dir = model.isDir(left)
        right_is_dir = model.isDir(right)
        if left_is_dir != right_is_dir:
            # 昇順・降順のどちらでもフォルダを上に保つため、
            # ソート順に応じて返す真偽を反転させる
            if self.sortOrder() == QtCore.Qt.AscendingOrder:
                return left_is_dir
            return right_is_dir
        return super(AssetFileFilterProxyModel, self).lessThan(left, right)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.ForegroundRole:
            source_index = self.mapToSource(index)
            model = self.sourceModel()
            if model.isDir(source_index):
                # None（判定不能）は通常表示のままにする
                if self._dir_contains_target(model.filePath(source_index)) is False:
                    return QtGui.QColor(140, 140, 140)
            else:
                ext = os.path.splitext(model.fileName(source_index))[1].lstrip(".").lower()
                if ext not in ALL_TARGET_EXTENSIONS:
                    return QtGui.QColor(140, 140, 140)
        return super(AssetFileFilterProxyModel, self).data(index, role)


FBXFileFilterProxyModel = AssetFileFilterProxyModel  # 後方互換エイリアス


class _DropEnabledTreeView(QtWidgets.QTreeView):
    """
    [追加] 外部(エクスプローラー等)からのファイルドロップのみを受け付けるツリービュー。

    QFileSystemModelの既定のドロップ処理はモデル自身のdropMimeData()に
    委譲され、内部的には「移動(QFile::rename)」になる。ドライブを跨ぐと
    失敗するうえ、エクスプローラーからのドラッグでも元ファイルが移動して
    しまい意図しないデータ消失につながるため、ここでは使わない。
    ドロップイベント自体は横取りしてシグナルとして送出するだけにとどめ、
    実際のコピー処理はFBXBrowserWidget側（_on_files_dropped）で行う。
    """

    filesDropped = QtCore.Signal(list)

    def __init__(self, parent=None):
        super(_DropEnabledTreeView, self).__init__(parent)
        # Qt内部のアイテムドラッグ&ドロップ（並べ替え等）は使わない。
        # [不具合対応] setDragDropMode()は内部でsetAcceptDrops()を呼び直すため、
        # 先にNoDragDropへ設定してから最後にsetAcceptDrops(True)しないと、
        # 外部からのドロップ受付自体が無効化されてしまう（イベントが一切来ない）。
        self.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        self.setDropIndicatorShown(False)
        self.setAcceptDrops(True)

    @staticmethod
    def _local_file_paths(mime_data):
        if not mime_data.hasUrls():
            return []
        return [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event):
        if self._local_file_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._local_file_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._local_file_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.filesDropped.emit(paths)


class _ImagePreviewDialog(QtWidgets.QDialog):
    """
    [rev.04追加] 画像プレビュー用ダイアログ。

    従来はプレビュー対象の1枚だけを表示し、隣のファイルを見るには
    一度閉じてダブルクリックし直す必要があった。ここでは表示中の
    ファイルと同じフォルダ内の画像一覧・現在位置を保持しておき、
    左右矢印キーで前後の画像へ即座に切り替えられるようにする
    （末尾/先頭では反対側へ循環する）。
    """

    MAX_PREVIEW_SIZE = QtCore.QSize(800, 600)

    def __init__(self, image_paths, current_index, parent=None):
        super(_ImagePreviewDialog, self).__init__(parent)
        self._image_paths = image_paths
        self._index = current_index

        layout = QtWidgets.QVBoxLayout(self)

        self._image_label = QtWidgets.QLabel()
        self._image_label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self._image_label)

        self._info_label = QtWidgets.QLabel()
        self._info_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self._info_label)

        if len(self._image_paths) > 1:
            hint_label = QtWidgets.QLabel("← → キーで隣の画像へ移動（循環）")
            hint_label.setStyleSheet("color: gray; font-size: 10px;")
            layout.addWidget(hint_label)

        # キーイベントを受け取るためフォーカス可能にしておく。
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._show_current_image()

    def _show_current_image(self):
        file_path = self._image_paths[self._index]
        pixmap = QtGui.QPixmap(file_path)

        if pixmap.isNull():
            self._image_label.setPixmap(QtGui.QPixmap())
            self._image_label.setText(
                "この形式はMaya内プレビューに対応していない可能性があります。"
            )
            self._info_label.setText("")
        else:
            original_size = pixmap.size()
            if (
                original_size.width() > self.MAX_PREVIEW_SIZE.width()
                or original_size.height() > self.MAX_PREVIEW_SIZE.height()
            ):
                pixmap = pixmap.scaled(
                    self.MAX_PREVIEW_SIZE,
                    QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation,
                )
            self._image_label.setPixmap(pixmap)
            self._info_label.setText(
                "{} x {}px".format(original_size.width(), original_size.height())
            )

        self.setWindowTitle(
            "{}  ({}/{})".format(
                os.path.basename(file_path), self._index + 1, len(self._image_paths)
            )
        )
        # 画像サイズが変わっても毎回ウィンドウがフィットするようにする。
        self.adjustSize()

    def _navigate(self, step):
        if len(self._image_paths) <= 1:
            return
        self._index = (self._index + step) % len(self._image_paths)
        self._show_current_image()

    def keyPressEvent(self, event):
        key = event.key()
        if key == QtCore.Qt.Key_Right:
            self._navigate(1)
            event.accept()
            return
        if key == QtCore.Qt.Key_Left:
            self._navigate(-1)
            event.accept()
            return
        super(_ImagePreviewDialog, self).keyPressEvent(event)


class FBXBrowserWidget(QtWidgets.QWidget):
    """アセットブラウザ本体のウィジェット。"""

    def __init__(self, parent=None):
        super(FBXBrowserWidget, self).__init__(parent)
        self.setObjectName("FBXBrowserWidgetRoot")

        self._fs_model = None
        self._proxy_model = None

        self._history = []
        self._history_index = -1
        self._suppress_history_push = False

        # [修正5] モードレスプレビューダイアログの参照保持用
        self._preview_dialogs = []

        self._settings = QtCore.QSettings(SETTINGS_ORG, SETTINGS_APP)
        self._loading_settings = True

        # [修正10] フィルタ入力の遅延適用（1文字ごとの再フィルタを避ける）
        self._filter_timer = QtCore.QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)

        self._build_ui()
        self._connect_signals()
        self._load_settings()
        self._loading_settings = False

    # ------------------------------------------------------------------
    # UI構築
    # ------------------------------------------------------------------
    def _build_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 2)
        main_layout.setSpacing(4)

        # --- 行1: ナビゲーション + パンくず + お気に入り + オプション ---
        nav_layout = QtWidgets.QHBoxLayout()
        nav_layout.setSpacing(2)

        def _tool_button(text, tooltip, enabled=True):
            btn = QtWidgets.QToolButton()
            btn.setText(text)
            btn.setToolTip(tooltip)
            btn.setEnabled(enabled)
            btn.setAutoRaise(True)
            return btn

        self.back_button = _tool_button("◀", "戻る", False)
        self.forward_button = _tool_button("▶", "進む", False)
        self.up_button = _tool_button("▲", "上の階層へ", False)
        self.refresh_button = _tool_button("⟲", "再読み込み")

        for btn in (self.back_button, self.forward_button, self.up_button, self.refresh_button):
            nav_layout.addWidget(btn)
        nav_layout.addSpacing(6)

        self.breadcrumb_scroll_area = QtWidgets.QScrollArea()
        self.breadcrumb_scroll_area.setWidgetResizable(True)
        # [不具合対応] ScrollBarAsNeeded だと、コード側から
        # horizontalScrollBar().setValue(...) を呼ぶだけでバーが常時居座り、
        # 高さ28pxの細い帯の中でボタンと場所を奪い合って操作性が悪化する。
        # スクロール自体はQScrollArea内部の座標操作で行うため、
        # バーの見た目は常にオフでよい。
        self.breadcrumb_scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.breadcrumb_scroll_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.breadcrumb_scroll_area.setFixedHeight(28)
        self.breadcrumb_scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)

        self.breadcrumb_container = QtWidgets.QWidget()
        self.breadcrumb_layout = QtWidgets.QHBoxLayout(self.breadcrumb_container)
        self.breadcrumb_layout.setContentsMargins(2, 0, 2, 0)
        self.breadcrumb_layout.setSpacing(0)
        self.breadcrumb_layout.addStretch(1)
        self.breadcrumb_scroll_area.setWidget(self.breadcrumb_container)
        nav_layout.addWidget(self.breadcrumb_scroll_area, 1)

        # [追加] お気に入りフォルダ
        self.favorites_button = QtWidgets.QToolButton()
        self.favorites_button.setText("★")
        self.favorites_button.setToolTip("お気に入りフォルダ")
        self.favorites_button.setAutoRaise(True)
        self.favorites_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.favorites_menu = QtWidgets.QMenu(self)
        self.favorites_button.setMenu(self.favorites_menu)
        nav_layout.addWidget(self.favorites_button)

        # オプションメニュー
        self.options_button = QtWidgets.QToolButton()
        self.options_button.setText("⚙")
        self.options_button.setToolTip("オプション")
        self.options_button.setAutoRaise(True)
        self.options_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)

        self.options_menu = QtWidgets.QMenu(self)

        self.recursive_action = self.options_menu.addAction("サブフォルダを空判定に含める")
        self.recursive_action.setCheckable(True)
        self.recursive_action.setChecked(True)

        self.namespace_action = self.options_menu.addAction("Import時にファイル名を名前空間にする")
        self.namespace_action.setCheckable(True)
        self.namespace_action.setChecked(True)

        # [修正3] FBXインポート設定のリセット可否
        self.fbx_reset_action = self.options_menu.addAction(
            "FBXインポート設定を毎回リセットする（推奨）"
        )
        self.fbx_reset_action.setCheckable(True)
        self.fbx_reset_action.setChecked(True)
        self.fbx_reset_action.setToolTip(
            "Mayaのグローバルなインポート設定（単位・軸・スムージング）は\n"
            "セッションを跨いで残るため、既定値へ明示的にリセットしてから\n"
            "読み込みます。スケール100倍などの事故を防止します。"
        )

        # [修正4] ダブルクリック挙動
        self.default_action_action = self.options_menu.addAction(
            "ダブルクリックで既定アクションを即実行"
        )
        self.default_action_action.setCheckable(True)
        self.default_action_action.setChecked(False)
        self.default_action_action.setToolTip(
            "オン: FBX/MA/MB=Import、画像/テキスト=OS標準アプリ を即実行します。\n"
            "オフ: 従来通りアクション選択メニューを表示します。\n"
            "（オンの場合、意図しないImportが走るリスクがあります）"
        )

        self.options_menu.addSeparator()

        self.show_path_action = self.options_menu.addAction("パス入力欄を表示")
        self.show_path_action.setCheckable(True)
        self.show_path_action.setChecked(False)

        self.show_log_action = self.options_menu.addAction("ログパネルを表示")
        self.show_log_action.setCheckable(True)
        self.show_log_action.setChecked(False)

        self.options_menu.addSeparator()
        self.reset_settings_action = self.options_menu.addAction("保存された設定をリセット")

        self.options_button.setMenu(self.options_menu)
        nav_layout.addWidget(self.options_button)
        main_layout.addLayout(nav_layout)

        # --- 行2: ファイル名フィルタ ---
        self.filter_line_edit = QtWidgets.QLineEdit()
        self.filter_line_edit.setPlaceholderText("ファイル名で絞り込み（部分一致）")
        self.filter_line_edit.setClearButtonEnabled(True)
        main_layout.addWidget(self.filter_line_edit)

        # --- 行3: フォルダパス入力（既定では非表示） ---
        self.path_widget = QtWidgets.QWidget()
        folder_layout = QtWidgets.QHBoxLayout(self.path_widget)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.addWidget(QtWidgets.QLabel("Folder:"))
        self.folder_line_edit = QtWidgets.QLineEdit()
        self.folder_line_edit.setPlaceholderText("フォルダパスを入力してください")
        self.browse_button = QtWidgets.QPushButton("Browse...")
        self.browse_button.setStyleSheet(
            "QPushButton { background-color: %s; color: white; padding: 3px 10px; }" % COLOR_INFO
        )
        folder_layout.addWidget(self.folder_line_edit, 1)
        folder_layout.addWidget(self.browse_button)
        self.path_widget.setVisible(False)
        main_layout.addWidget(self.path_widget)

        # --- 中央: ツリービュー / ログパネル ---
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        self.tree_view = _DropEnabledTreeView()
        self.tree_view.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree_view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree_view.setSortingEnabled(True)
        self.tree_view.setRootIsDecorated(False)
        self.tree_view.setItemsExpandable(False)
        self.tree_view.setIndentation(0)
        self.tree_view.setAlternatingRowColors(True)
        self.splitter.addWidget(self.tree_view)

        self.log_text_edit = QtWidgets.QPlainTextEdit()
        self.log_text_edit.setReadOnly(True)
        self.log_text_edit.setMinimumHeight(60)
        self.log_text_edit.setVisible(False)
        self.splitter.addWidget(self.log_text_edit)

        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        main_layout.addWidget(self.splitter, 1)

        self.status_label = QtWidgets.QLabel(
            "ダブルクリック: フォルダ=移動 / ファイル=アクション選択"
        )
        self.status_label.setStyleSheet("color: gray; font-size: 10px;")
        main_layout.addWidget(self.status_label)

    def _connect_signals(self):
        self.browse_button.clicked.connect(self._on_browse_clicked)
        self.folder_line_edit.editingFinished.connect(self._on_folder_line_edit_changed)
        self.tree_view.doubleClicked.connect(self._on_item_double_clicked)
        self.tree_view.customContextMenuRequested.connect(self._on_context_menu_requested)
        self.tree_view.filesDropped.connect(self._on_files_dropped)

        # [追加] Deleteキーでの削除
        self._delete_shortcut = _QShortcutClass(QtGui.QKeySequence.Delete, self.tree_view)
        self._delete_shortcut.setContext(QtCore.Qt.WidgetShortcut)
        self._delete_shortcut.activated.connect(self._on_delete_shortcut)

        self.back_button.clicked.connect(self._on_back_clicked)
        self.forward_button.clicked.connect(self._on_forward_clicked)
        self.up_button.clicked.connect(self._on_up_clicked)
        self.refresh_button.clicked.connect(self._on_refresh_clicked)

        self.recursive_action.toggled.connect(self._on_recursive_toggled)
        self.namespace_action.toggled.connect(lambda _c: self._save_settings())
        self.fbx_reset_action.toggled.connect(lambda _c: self._save_settings())
        self.default_action_action.toggled.connect(self._on_default_action_toggled)
        self.show_path_action.toggled.connect(self._on_show_path_toggled)
        self.show_log_action.toggled.connect(self._on_show_log_toggled)
        self.reset_settings_action.triggered.connect(self._on_reset_settings)

        self.splitter.splitterMoved.connect(lambda *_a: self._save_settings())

        self.filter_line_edit.textChanged.connect(lambda _t: self._filter_timer.start())
        self._filter_timer.timeout.connect(self._apply_name_filter)

        self.favorites_menu.aboutToShow.connect(self._rebuild_favorites_menu)

    # ------------------------------------------------------------------
    # 設定の保存 / 復元
    # ------------------------------------------------------------------
    def _save_settings(self):
        if self._loading_settings:
            return
        try:
            s = self._settings
            s.setValue("lastFolder", self.folder_line_edit.text().strip())
            s.setValue("includeSubfolders", self.recursive_action.isChecked())
            s.setValue("useNamespace", self.namespace_action.isChecked())
            s.setValue("resetFbxImport", self.fbx_reset_action.isChecked())
            s.setValue("defaultActionOnDoubleClick", self.default_action_action.isChecked())
            s.setValue("showPathBar", self.show_path_action.isChecked())
            s.setValue("showLog", self.show_log_action.isChecked())
            s.setValue("splitterState", self.splitter.saveState())
            if self.tree_view.model() is not None:
                s.setValue("headerState", self.tree_view.header().saveState())
            s.sync()
        except Exception:
            traceback.print_exc()

    def _load_settings(self):
        s = self._settings

        def _to_bool(value, default):
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("true", "1")

        self.recursive_action.setChecked(_to_bool(s.value("includeSubfolders"), True))
        self.namespace_action.setChecked(_to_bool(s.value("useNamespace"), True))
        self.fbx_reset_action.setChecked(_to_bool(s.value("resetFbxImport"), True))
        self.default_action_action.setChecked(
            _to_bool(s.value("defaultActionOnDoubleClick"), False)
        )

        show_path = _to_bool(s.value("showPathBar"), False)
        self.show_path_action.setChecked(show_path)
        self.path_widget.setVisible(show_path)

        show_log = _to_bool(s.value("showLog"), False)
        self.show_log_action.setChecked(show_log)
        self.log_text_edit.setVisible(show_log)

        splitter_state = s.value("splitterState")
        if splitter_state:
            try:
                self.splitter.restoreState(splitter_state)
            except Exception:
                pass

        # [修正2] フォルダの復元はイベントループが回り始めてから実行する。
        # Maya起動シーケンス中に同期実行すると、到達不能パスの判定や
        # ディレクトリスキャンでMaya本体の起動がハングする恐れがある。
        last_folder = s.value("lastFolder") or ""
        QtCore.QTimer.singleShot(0, lambda: self._deferred_initial_navigate(last_folder))

    def _deferred_initial_navigate(self, last_folder):
        """[修正2] 起動後の遅延フォルダ復元。到達性チェック付き。"""
        try:
            if last_folder:
                if _is_path_reachable(last_folder):
                    self.folder_line_edit.setText(last_folder)
                    self._navigate_to(last_folder)
                    self._log("前回のフォルダを復元しました: {}".format(last_folder), level="info")
                    self._restore_header_state()
                    return
                self._log(
                    "前回のフォルダに到達できないため既定値を使用します: {}".format(last_folder),
                    level="warn",
                )

            fallback = _guess_default_folder()
            if fallback:
                self.folder_line_edit.setText(fallback)
                self._navigate_to(fallback)
            self._restore_header_state()
        except Exception:
            traceback.print_exc()

    def _restore_header_state(self):
        header_state = self._settings.value("headerState")
        if header_state and self.tree_view.model() is not None:
            try:
                self.tree_view.header().restoreState(header_state)
            except Exception:
                pass

    def _on_reset_settings(self):
        reply = QtWidgets.QMessageBox.question(
            self,
            "設定のリセット",
            "保存されているフォルダ・オプション・列幅・お気に入りを全て削除します。\n"
            "よろしいですか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        self._settings.clear()
        self._settings.sync()
        self._log("保存された設定を削除しました。次回起動時から既定値になります。", level="warn")

    # ------------------------------------------------------------------
    # お気に入り
    # ------------------------------------------------------------------
    def _get_favorites(self):
        try:
            raw = self._settings.value("favorites") or "[]"
            favorites = json.loads(raw)
            return [p for p in favorites if isinstance(p, str)]
        except Exception:
            return []

    def _set_favorites(self, favorites):
        self._settings.setValue("favorites", json.dumps(favorites, ensure_ascii=False))
        self._settings.sync()

    def _rebuild_favorites_menu(self):
        self.favorites_menu.clear()
        favorites = self._get_favorites()

        if favorites:
            for path in favorites:
                label = os.path.basename(path.rstrip("/\\")) or path
                action = self.favorites_menu.addAction(label)
                action.setToolTip(path)
                action.triggered.connect(
                    lambda checked=False, p=path: self._navigate_to(p)
                )
            self.favorites_menu.addSeparator()
        else:
            placeholder = self.favorites_menu.addAction("(お気に入りは未登録です)")
            placeholder.setEnabled(False)
            self.favorites_menu.addSeparator()

        add_action = self.favorites_menu.addAction("現在のフォルダを追加")
        add_action.triggered.connect(self._on_add_favorite)

        if favorites:
            remove_menu = self.favorites_menu.addMenu("削除")
            for path in favorites:
                action = remove_menu.addAction(path)
                action.triggered.connect(
                    lambda checked=False, p=path: self._on_remove_favorite(p)
                )

    def _on_add_favorite(self):
        current = self.folder_line_edit.text().strip()
        if not current:
            return
        favorites = self._get_favorites()
        if current in favorites:
            self._log("既に登録済みです: {}".format(current), level="info")
            return
        favorites.append(current)
        self._set_favorites(favorites)
        self._log("お気に入りに追加しました: {}".format(current), level="run")

    def _on_remove_favorite(self, path):
        favorites = [p for p in self._get_favorites() if p != path]
        self._set_favorites(favorites)
        self._log("お気に入りから削除しました: {}".format(path), level="info")

    # ------------------------------------------------------------------
    # ログ / ステータス / フィルタ
    # ------------------------------------------------------------------
    def _log(self, message, level="info"):
        color_map = {
            "info": COLOR_INFO,
            "run": COLOR_RUN,
            "warn": COLOR_WARN,
            "error": COLOR_ERROR,
        }
        color = color_map.get(level, COLOR_INFO)
        prefix = {"info": "[INFO]", "run": "[OK]", "warn": "[WARN]", "error": "[NG]"}.get(
            level, "[INFO]"
        )
        safe_msg = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.log_text_edit.appendHtml(
            '<span style="color:{c};">{p}</span> {m}'.format(c=color, p=prefix, m=safe_msg)
        )

        if level in ("warn", "error") and not self.show_log_action.isChecked():
            self.show_log_action.setChecked(True)

    def _on_show_log_toggled(self, checked):
        self.log_text_edit.setVisible(checked)
        self._save_settings()

    def _on_show_path_toggled(self, checked):
        self.path_widget.setVisible(checked)
        self._save_settings()

    def _on_default_action_toggled(self, _checked):
        self._update_status()
        self._save_settings()

    def _apply_name_filter(self):
        if self._proxy_model is not None:
            self._proxy_model.set_name_filter(self.filter_line_edit.text())
            self._update_status()

    def _update_status(self):
        try:
            model = self._proxy_model
            if model is None:
                return
            total = model.rowCount(self.tree_view.rootIndex())
            filter_note = "（フィルタ適用中）" if self.filter_line_edit.text().strip() else ""
            hint = (
                "ダブルクリック: 既定アクション実行"
                if self.default_action_action.isChecked()
                else "ダブルクリック: アクション選択"
            )
            self.status_label.setText(
                "{} 項目{}　|　{}　|　右クリック: 全操作".format(total, filter_note, hint)
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # ナビゲーション
    # ------------------------------------------------------------------
    def _on_browse_clicked(self):
        start_dir = self.folder_line_edit.text() or os.path.expanduser("~")
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "フォルダを選択", start_dir)
        if selected:
            self._navigate_to(selected)

    def _on_folder_line_edit_changed(self):
        path = self.folder_line_edit.text().strip()
        if path and _is_path_reachable(path):
            self._navigate_to(path)
        elif path:
            self._log("指定されたフォルダが存在しません: {}".format(path), level="warn")

    def _on_recursive_toggled(self, _checked):
        current = self.folder_line_edit.text().strip()
        if current and os.path.isdir(current):
            self._refresh_current_view()
        self._save_settings()

    def _on_back_clicked(self):
        if self._history_index > 0:
            self._history_index -= 1
            self._navigate_with_history_suppressed(self._history[self._history_index])

    def _on_forward_clicked(self):
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._navigate_with_history_suppressed(self._history[self._history_index])

    def _navigate_with_history_suppressed(self, target):
        self._suppress_history_push = True
        try:
            self._navigate_to(target)
        finally:
            self._suppress_history_push = False

    def _on_up_clicked(self):
        current = self.folder_line_edit.text().strip()
        if not current:
            return
        parent = os.path.dirname(current.rstrip("/\\"))
        if parent and os.path.isdir(parent) and parent != current:
            self._navigate_to(parent)

    def _on_refresh_clicked(self):
        self._refresh_current_view()

    def _on_breadcrumb_clicked(self, path):
        self._navigate_to(path)

    def _navigate_to(self, folder_path):
        folder_path = os.path.normpath(folder_path)
        # [修正2] 到達性はタイムアウト付きで判定する
        if not _is_path_reachable(folder_path):
            self._log("フォルダに到達できません: {}".format(folder_path), level="warn")
            return

        self.folder_line_edit.setText(folder_path)
        self._build_tree_model(folder_path)
        self._update_breadcrumb(folder_path)

        if not self._suppress_history_push:
            if self._history_index < len(self._history) - 1:
                self._history = self._history[: self._history_index + 1]
            if not self._history or self._history[self._history_index] != folder_path:
                self._history.append(folder_path)
                self._history_index = len(self._history) - 1

        self._update_nav_button_states(folder_path)
        self._update_status()
        self._save_settings()

    def _refresh_current_view(self):
        current = self.folder_line_edit.text().strip()
        if current and os.path.isdir(current):
            self._build_tree_model(current)
            self._update_status()
            self._log("再読み込みしました: {}".format(current), level="info")

    def _update_nav_button_states(self, folder_path):
        self.back_button.setEnabled(self._history_index > 0)
        self.forward_button.setEnabled(self._history_index < len(self._history) - 1)
        parent = os.path.dirname(folder_path.rstrip("/\\"))
        self.up_button.setEnabled(
            bool(parent) and os.path.isdir(parent) and parent != folder_path
        )

    def _update_breadcrumb(self, folder_path):
        while self.breadcrumb_layout.count() > 0:
            item = self.breadcrumb_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        drive, tail = os.path.splitdrive(folder_path)
        parts = [p for p in tail.replace("\\", "/").split("/") if p]

        segments = []
        if drive:
            drive_root = drive + os.sep
            segments.append((drive_root, drive_root))
            accumulated = drive_root
        else:
            accumulated = "/"
            segments.append(("/", "/"))

        for part in parts:
            accumulated = os.path.join(accumulated, part)
            segments.append((part, accumulated))

        for i, (label, full_path) in enumerate(segments):
            btn = QtWidgets.QToolButton()
            btn.setText(label)
            btn.setAutoRaise(True)
            btn.setStyleSheet(
                "QToolButton { padding: 2px 4px; } "
                "QToolButton:hover { background-color: rgba(64,115,166,60); }"
            )
            btn.clicked.connect(
                lambda checked=False, p=full_path: self._on_breadcrumb_clicked(p)
            )
            self.breadcrumb_layout.addWidget(btn)
            if i < len(segments) - 1:
                sep_label = QtWidgets.QLabel("›")
                sep_label.setStyleSheet("color: gray;")
                self.breadcrumb_layout.addWidget(sep_label)

        self.breadcrumb_layout.addStretch(1)

        # [不具合対応] 深い階層のとき現在地（末尾）が見えるよう右端へスクロールする。
        # スクロールバー自体は setHorizontalScrollBarPolicy(ScrollBarAlwaysOff) で
        # 常に非表示にしている。setValue() の呼び出し自体はバーを表示させないため、
        # 見た目への副作用は無い（以前の不具合はポリシーがAsNeededだったことが原因）。
        QtCore.QTimer.singleShot(0, self._scroll_breadcrumb_to_end)

    def _scroll_breadcrumb_to_end(self):
        """パンくずの表示位置を右端（＝現在地）へスクロールする。バーは常に非表示。"""
        try:
            bar = self.breadcrumb_scroll_area.horizontalScrollBar()
            bar.setValue(bar.maximum())
        except Exception:
            pass

    def _build_tree_model(self, folder_path):
        try:
            header_state = None
            if self.tree_view.model() is not None:
                header_state = self.tree_view.header().saveState()

            self._fs_model = QtWidgets.QFileSystemModel()
            self._fs_model.setRootPath(folder_path)

            self._proxy_model = AssetFileFilterProxyModel()
            self._proxy_model.set_include_subfolders(self.recursive_action.isChecked())
            self._proxy_model.set_name_filter(self.filter_line_edit.text())
            self._proxy_model.setSourceModel(self._fs_model)

            self.tree_view.setModel(self._proxy_model)
            source_root_index = self._fs_model.index(folder_path)
            self.tree_view.setRootIndex(self._proxy_model.mapFromSource(source_root_index))

            header = self.tree_view.header()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
            for col in range(1, self._fs_model.columnCount()):
                self.tree_view.showColumn(col)
                header.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeToContents)

            if header_state:
                try:
                    header.restoreState(header_state)
                except Exception:
                    pass

            self._fs_model.directoryLoaded.connect(lambda _p: self._update_status())
            self._log("フォルダを読み込みました: {}".format(folder_path), level="info")
        except Exception as exc:
            self._log("フォルダ読み込み中にエラー: {}".format(exc), level="error")
            traceback.print_exc()

    # ------------------------------------------------------------------
    # ファイル操作
    # ------------------------------------------------------------------
    def _get_selected_target_paths(self):
        paths = []
        for index in self.tree_view.selectionModel().selectedRows():
            source_index = self._proxy_model.mapToSource(index)
            if self._fs_model.isDir(source_index):
                continue
            file_path = self._fs_model.filePath(source_index)
            ext = os.path.splitext(file_path)[1].lstrip(".").lower()
            if ext in ALL_TARGET_EXTENSIONS:
                paths.append(file_path)
        return paths

    def _get_selected_all_paths(self):
        """[追加] 拡張子を問わず、選択中の全項目のパスを返す（削除機能用）。"""
        if self._proxy_model is None or self._fs_model is None:
            return []
        selection_model = self.tree_view.selectionModel()
        if selection_model is None:
            return []
        paths = []
        for index in selection_model.selectedRows():
            source_index = self._proxy_model.mapToSource(index)
            paths.append(self._fs_model.filePath(source_index))
        return paths

    def _on_delete_shortcut(self):
        paths = self._get_selected_all_paths()
        if paths:
            self._delete_paths(paths)

    def _delete_paths(self, paths):
        """[追加] 選択項目をOSのごみ箱へ移動する（完全削除はしない）。"""
        paths = [p for p in dict.fromkeys(paths) if p]
        if not paths:
            return

        dirs = [p for p in paths if os.path.isdir(p)]

        # 現在のシーン/参照中ファイルと一致するものが含まれていれば警告を出す
        # （ブロックはしない。Mayaの参照はシーン保存/リロードまで維持されるため）。
        referenced_norm = set()
        try:
            scene_path = cmds.file(q=True, sceneName=True) or ""
            if scene_path:
                referenced_norm.add(_normalize_for_compare(scene_path))
            for ref_path in (cmds.file(q=True, reference=True) or []):
                referenced_norm.add(_normalize_for_compare(ref_path))
        except Exception:
            pass
        in_use = [
            p for p in paths
            if p not in dirs and _normalize_for_compare(p) in referenced_norm
        ]

        preview_names = [os.path.basename(p.rstrip("/\\")) for p in paths[:10]]
        message = "以下をごみ箱へ移動します。よろしいですか？\n\n{}".format(
            "\n".join(preview_names)
        )
        if len(paths) > 10:
            message += "\n…他{}件".format(len(paths) - 10)
        if dirs:
            message += "\n\n※ フォルダは中身ごと移動されます。"
        if in_use:
            message += (
                "\n\n⚠ 現在のシーンで使用中/参照中のファイルが含まれています。\n"
                "移動しても開いているシーン自体はそのままですが、\n"
                "参照リンクが無効になる可能性があります。"
            )

        reply = QtWidgets.QMessageBox.question(
            self,
            "削除の確認",
            message,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            self._log("削除を中止しました。", level="info")
            return

        succeeded = []
        failed = []
        for p in paths:
            try:
                _send_to_trash(p)
                succeeded.append(p)
            except Exception as exc:
                failed.append((p, exc))

        if succeeded:
            succeeded_norm = set(_normalize_for_compare(p) for p in succeeded)
            favorites = self._get_favorites()
            new_favorites = [
                f for f in favorites if _normalize_for_compare(f) not in succeeded_norm
            ]
            if len(new_favorites) != len(favorites):
                self._set_favorites(new_favorites)

        if self._proxy_model is not None:
            self._proxy_model.invalidate_cache()
        self._refresh_current_view()

        if succeeded:
            self._log("{}件をごみ箱へ移動しました。".format(len(succeeded)), level="run")
        for p, exc in failed:
            self._log("削除に失敗しました: {} ({})".format(p, exc), level="error")

    def _on_files_dropped(self, source_paths):
        """[追加] 外部(エクスプローラー等)からドロップされたファイルを現在のフォルダへコピーする。"""
        dest_folder = self.folder_line_edit.text().strip()
        if not dest_folder or not os.path.isdir(dest_folder):
            self._log(
                "コピー先のフォルダが開かれていません。先にフォルダを開いてください。",
                level="warn",
            )
            return

        dest_norm = _normalize_for_compare(dest_folder)

        plan = []
        skipped_dirs = 0
        skipped_same = 0
        for src in dict.fromkeys(source_paths):
            if os.path.isdir(src):
                skipped_dirs += 1
                continue
            if not os.path.isfile(src):
                continue
            if _normalize_for_compare(os.path.dirname(src)) == dest_norm:
                skipped_same += 1
                continue
            plan.append((src, os.path.join(dest_folder, os.path.basename(src))))

        if skipped_dirs:
            self._log(
                "フォルダのドロップは未対応のため{}件スキップしました。".format(skipped_dirs),
                level="warn",
            )
        if skipped_same:
            self._log(
                "既に表示中のフォルダにあるため{}件スキップしました。".format(skipped_same),
                level="info",
            )

        if not plan:
            return

        overwrite_all = None  # None=毎回確認 / True/False=残り全件へ適用
        succeeded = []
        failed = []
        for src, dst in plan:
            if os.path.exists(dst):
                do_overwrite = overwrite_all
                if do_overwrite is None:
                    box = QtWidgets.QMessageBox(self)
                    box.setWindowTitle("ファイルの上書き確認")
                    box.setText(
                        "同名のファイルが既に存在します。上書きしますか？\n\n{}".format(
                            os.path.basename(dst)
                        )
                    )
                    yes_btn = box.addButton("上書き", QtWidgets.QMessageBox.YesRole)
                    yes_all_btn = box.addButton("すべて上書き", QtWidgets.QMessageBox.YesRole)
                    box.addButton("スキップ", QtWidgets.QMessageBox.NoRole)
                    no_all_btn = box.addButton("すべてスキップ", QtWidgets.QMessageBox.NoRole)
                    cancel_btn = box.addButton("中止", QtWidgets.QMessageBox.RejectRole)
                    box.exec_()
                    clicked = box.clickedButton()

                    if clicked == cancel_btn:
                        self._log("コピーを中止しました。", level="info")
                        break
                    if clicked == yes_all_btn:
                        overwrite_all = True
                        do_overwrite = True
                    elif clicked == no_all_btn:
                        overwrite_all = False
                        do_overwrite = False
                    elif clicked == yes_btn:
                        do_overwrite = True
                    else:
                        do_overwrite = False

                if not do_overwrite:
                    continue

            try:
                shutil.copy2(src, dst)
                succeeded.append(dst)
            except Exception as exc:
                failed.append((src, exc))

        if self._proxy_model is not None:
            self._proxy_model.invalidate_cache()
        self._refresh_current_view()

        if succeeded:
            self._log("{}件のファイルをコピーしました。".format(len(succeeded)), level="run")
        for src, exc in failed:
            self._log(
                "コピーに失敗しました: {} ({})".format(os.path.basename(src), exc),
                level="error",
            )

    def _on_item_double_clicked(self, index):
        source_index = self._proxy_model.mapToSource(index)
        if self._fs_model.isDir(source_index):
            self._navigate_to(self._fs_model.filePath(source_index))
            return

        file_path = self._fs_model.filePath(source_index)
        ext = os.path.splitext(file_path)[1].lstrip(".").lower()
        if ext not in ALL_TARGET_EXTENSIONS:
            return

        # [修正4] 既定アクションの即実行（オプションで切替）
        if self.default_action_action.isChecked():
            self._run_default_action(file_path)
        else:
            self._show_action_menu_for_file(file_path)

    def _run_default_action(self, file_path):
        """[修正4] ファイル種別ごとの既定アクションを実行する。"""
        category = _get_file_category(file_path)
        if category in (CATEGORY_FBX, CATEGORY_MAYA_SCENE):
            self._import_asset(file_path)
        elif category in (CATEGORY_IMAGE, CATEGORY_TEXT):
            self._open_with_os_default(file_path)
        else:
            self._reveal_in_explorer(file_path)

    def _show_action_menu_for_file(self, file_path):
        category = _get_file_category(file_path)
        menu = QtWidgets.QMenu(self)
        actions = {}

        if category in (CATEGORY_FBX, CATEGORY_MAYA_SCENE):
            actions["import"] = menu.addAction("Import")
            actions["reference"] = menu.addAction("Reference")
            menu.addSeparator()
        elif category == CATEGORY_IMAGE:
            actions["os_viewer"] = menu.addAction("OS標準ビューアで開く")
            actions["maya_preview"] = menu.addAction("Mayaでプレビュー表示")
            actions["file_node"] = menu.addAction("Fileノードとして読み込む")
            menu.addSeparator()
        elif category == CATEGORY_TEXT:
            actions["external_editor"] = menu.addAction("外部エディタで開く")
            actions["panel_preview"] = menu.addAction("パネル内でプレビュー表示")
            menu.addSeparator()

        actions["explorer"] = menu.addAction("エクスプローラーで開く")
        menu.addSeparator()
        actions["delete"] = menu.addAction("削除（ごみ箱へ）")

        chosen = menu.exec_(QtGui.QCursor.pos())
        if chosen is None:
            return

        handlers = {
            "import": self._import_asset,
            "reference": self._reference_asset,
            "os_viewer": self._open_with_os_default,
            "maya_preview": self._preview_image_in_maya,
            "file_node": self._create_file_node,
            "external_editor": self._open_with_os_default,
            "panel_preview": self._preview_text_in_panel,
            "explorer": self._reveal_in_explorer,
            "delete": lambda p: self._delete_paths([p]),
        }
        for key, action in actions.items():
            if chosen == action:
                handlers[key](file_path)
                return

    def _on_context_menu_requested(self, pos):
        index = self.tree_view.indexAt(pos)
        if not index.isValid():
            return

        source_index = self._proxy_model.mapToSource(index)

        if self._fs_model.isDir(source_index):
            menu = QtWidgets.QMenu(self)
            open_action = menu.addAction("このフォルダを開く")
            fav_action = menu.addAction("お気に入りに追加")
            menu.addSeparator()
            explorer_action = menu.addAction("エクスプローラーで開く")
            menu.addSeparator()
            delete_action = menu.addAction("削除（ごみ箱へ）")
            chosen = menu.exec_(self.tree_view.viewport().mapToGlobal(pos))
            if chosen is None:
                return
            fp = self._fs_model.filePath(source_index)
            if chosen == open_action:
                self._navigate_to(fp)
            elif chosen == fav_action:
                favorites = self._get_favorites()
                if fp not in favorites:
                    favorites.append(fp)
                    self._set_favorites(favorites)
                    self._log("お気に入りに追加しました: {}".format(fp), level="run")
            elif chosen == explorer_action:
                self._reveal_in_explorer(fp)
            elif chosen == delete_action:
                self._delete_paths([fp])
            return

        clicked_path = self._fs_model.filePath(source_index)
        clicked_ext = os.path.splitext(clicked_path)[1].lstrip(".").lower()

        if clicked_ext not in ALL_TARGET_EXTENSIONS:
            menu = QtWidgets.QMenu(self)
            explorer_only = menu.addAction("エクスプローラーで開く")
            menu.addSeparator()
            delete_only = menu.addAction("削除（ごみ箱へ）")
            chosen = menu.exec_(self.tree_view.viewport().mapToGlobal(pos))
            if chosen == explorer_only:
                self._reveal_in_explorer(clicked_path)
            elif chosen == delete_only:
                self._delete_paths([clicked_path])
            return

        selected_paths = self._get_selected_target_paths()
        if clicked_path not in selected_paths:
            selected_paths = [clicked_path]

        if len(selected_paths) == 1:
            self._show_action_menu_for_file(selected_paths[0])
            return

        menu = QtWidgets.QMenu(self)
        import_action = menu.addAction("Import (選択項目)")
        reference_action = menu.addAction("Reference (選択項目)")
        menu.addSeparator()
        explorer_action = menu.addAction("エクスプローラーで開く (先頭項目)")
        menu.addSeparator()
        delete_action = menu.addAction("削除（選択項目、ごみ箱へ）")

        chosen = menu.exec_(self.tree_view.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen == import_action:
            # [修正6] 複数Importを1回のアンドゥでまとめて取り消せるようにする
            with _undo_chunk("AssetBrowser Import (multi)"):
                for p in selected_paths:
                    if _get_file_category(p) in (CATEGORY_FBX, CATEGORY_MAYA_SCENE):
                        self._import_asset(p, manage_undo=False)
        elif chosen == reference_action:
            with _undo_chunk("AssetBrowser Reference (multi)"):
                for p in selected_paths:
                    if _get_file_category(p) in (CATEGORY_FBX, CATEGORY_MAYA_SCENE):
                        self._reference_asset(p, manage_undo=False)
        elif chosen == explorer_action:
            self._reveal_in_explorer(selected_paths[0])
        elif chosen == delete_action:
            # 対象外拡張子(.psd等)も含め、選択されている全項目を削除対象にする
            self._delete_paths(self._get_selected_all_paths())

    # ------------------------------------------------------------------
    # Import / Reference
    # ------------------------------------------------------------------
    def _prepare_fbx_import_options(self):
        """
        [修正3] FBXインポート設定を既定値へ明示的にリセットする。

        cmds.file(i=True, type="FBX") は FBXImport系のグローバル設定を参照する。
        これらはFBXダイアログでの操作内容がセッションを跨いで残るため、
        「同じFBXなのに日によってスケールが100倍違う」という事故の原因になる。
        読み込み直前にリセットし必要項目を明示指定することで再現性を確保する。
        """
        if not self.fbx_reset_action.isChecked():
            return
        try:
            if not cmds.pluginInfo("fbxmaya", q=True, loaded=True):
                cmds.loadPlugin("fbxmaya", quiet=True)

            mel.eval("FBXResetImport")
            mel.eval('FBXImportMode -v "add"')
            mel.eval("FBXImportUpAxis y")
            mel.eval("FBXImportScaleFactor 1.0")
            mel.eval('FBXImportConvertUnitString "cm"')
            mel.eval("FBXImportSetLockedAttribute -v false")
            mel.eval("FBXImportUnlockNormals -v false")
            mel.eval("FBXImportHardEdges -v false")
            mel.eval("FBXImportCameras -v false")
            mel.eval("FBXImportLights -v false")
        except Exception as exc:
            # 設定リセットに失敗してもImport自体は続行させる（警告のみ）
            self._log(
                "FBXインポート設定のリセットに失敗しました（既存設定のまま続行）: {}".format(exc),
                level="warn",
            )

    def _import_asset(self, file_path, manage_undo=True):
        file_type = _get_maya_file_type(file_path)

        if file_type == "FBX":
            self._prepare_fbx_import_options()

        try:
            kwargs = dict(
                i=True, type=file_type, ignoreVersion=True, mergeNamespacesOnClash=False
            )
            if self.namespace_action.isChecked():
                base_name = os.path.splitext(os.path.basename(file_path))[0]
                kwargs["namespace"] = _sanitize_namespace(base_name)

            # [修正6] 単体実行時のみここでチャンクを開く（複数実行時は呼び出し側が管理）
            if manage_undo:
                with _undo_chunk("AssetBrowser Import"):
                    cmds.file(file_path, **kwargs)
            else:
                cmds.file(file_path, **kwargs)

            self._log("Import完了: {}".format(file_path), level="run")
        except Exception as exc:
            self._log("Import失敗: {} ({})".format(file_path, exc), level="error")
            traceback.print_exc()

    def _reference_asset(self, file_path, manage_undo=True):
        file_type = _get_maya_file_type(file_path)

        # [修正7] 既に同じファイルが参照済みでないかを確認する
        try:
            existing_refs = cmds.file(q=True, reference=True) or []
            target_norm = _normalize_for_compare(file_path)
            for ref_path in existing_refs:
                if _normalize_for_compare(ref_path) == target_norm:
                    reply = QtWidgets.QMessageBox.question(
                        self,
                        "重複Referenceの確認",
                        "このファイルは既にリファレンスされています。\n\n"
                        "{}\n\n二重に読み込みますか？".format(file_path),
                        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                        QtWidgets.QMessageBox.No,
                    )
                    if reply != QtWidgets.QMessageBox.Yes:
                        self._log("Referenceを中止しました: {}".format(file_path), level="info")
                        return
                    break
        except Exception:
            # 参照一覧の取得に失敗しても処理自体は続行する
            traceback.print_exc()

        base_name = os.path.splitext(os.path.basename(file_path))[0]
        namespace = _sanitize_namespace(base_name)

        # [修正7] 名前空間の衝突警告
        # （chair-01.fbx と chair_01.fbx がサニタイズ後に同一名となる等）
        #
        # [不具合対応] renderSetup関連ノードは、名前空間を削除しても
        # 「ロックされた子または読み取り専用の子があるため削除できません」という
        # Maya内部の制約により孤立して残ることがある。この状態の名前空間へ
        # 同名でReferenceしようとすると、cmds.file()実行中にMaya本体が
        # 内部クリーンアップを試みて `// エラー:` を直接出力する
        # （Pythonの例外としては送出されないため、下のtry/exceptでは捕捉できない）。
        # 事前に検出し、続行するかどうかを確認させることで、原因不明のまま
        # 中途半端な状態でReferenceが完了する事態を避ける。
        try:
            if cmds.namespace(exists=namespace):
                stale_render_setup_nodes = []
                try:
                    full_ns = ":" + namespace
                    ns_contents = cmds.namespaceInfo(
                        full_ns, listNamespace=True, dagPath=False
                    ) or []
                    stale_render_setup_nodes = [
                        n for n in ns_contents if ":renderSetup" in n or n.endswith("renderSetup")
                    ]
                except Exception:
                    pass

                if stale_render_setup_nodes:
                    self._log(
                        "名前空間 '{}' に renderSetup 関連の残留ノードがあります: {}".format(
                            namespace, ", ".join(stale_render_setup_nodes)
                        ),
                        level="warn",
                    )
                    reply = QtWidgets.QMessageBox.warning(
                        self,
                        "残留ノードの確認",
                        "名前空間 '{}' 内に、以前の読み込みで残った可能性のある\n"
                        "renderSetup関連ノードが検出されました。\n\n"
                        "{}\n\n"
                        "このまま続行すると「ロックされた子または読み取り専用の子が"
                        "あるため削除できません」というエラーが発生し、"
                        "Referenceが不完全な状態になる可能性があります。\n\n"
                        "続行しますか？（推奨: いいえ。一度シーンをクリーンな状態に"
                        "してから再試行してください）".format(
                            namespace, "\n".join(stale_render_setup_nodes)
                        ),
                        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                        QtWidgets.QMessageBox.No,
                    )
                    if reply != QtWidgets.QMessageBox.Yes:
                        self._log(
                            "残留ノードの検出によりReferenceを中止しました: {}".format(file_path),
                            level="info",
                        )
                        return
                else:
                    self._log(
                        "名前空間 '{}' は既に使用されています。Mayaが連番を付与します。".format(
                            namespace
                        ),
                        level="warn",
                    )
        except Exception:
            pass

        try:
            def _do_reference():
                cmds.file(
                    file_path,
                    r=True,
                    type=file_type,
                    namespace=namespace,
                    ignoreVersion=True,
                    # [不具合対応] renderSetupノードのロックエラー対策。
                    #
                    # Maya標準の File > Reference ダイアログは、参照ノードを
                    # 名前空間付きグループ（<namespace>RN）の配下に隔離した状態で
                    # 読み込む。このツールは従来 groupReference を指定しておらず、
                    # 参照内容がシーンルートへ直接展開されていた。
                    #
                    # renderSetupは参照解決時にシーン内の既存renderSetupノードと
                    # 自動マージを試みるため、グループによる隔離が無いと
                    # 参照先と参照元のrenderSetupノードが同一階層で衝突し、
                    # 「ロックされた子または読み取り専用の子があるため削除できません」
                    # というエラーに繋がっていた。groupReference=Trueにすることで
                    # 標準UIと同じ隔離構造にし、この衝突を避ける。
                    groupReference=True,
                    groupName="{}RN".format(namespace),
                    # 標準UIが暗黙に付与しているオプションも明示しておく。
                    # 未指定時の既定値はMaya環境設定に依存するため、
                    # 挙動を固定して再現性を確保する。
                    mergeNamespacesOnClash=False,
                )

            if manage_undo:
                with _undo_chunk("AssetBrowser Reference"):
                    _do_reference()
            else:
                _do_reference()

            # [不具合対応] cmds.file()がPython例外を出さずに正常終了しても、
            # Maya本体が内部で `// エラー:` を直接出力しているだけのケース
            # （renderSetup絡みの削除失敗等）があり、その場合は参照が
            # 実際には未登録・不完全な状態になっていることがある。
            # 例外に頼らず、参照一覧に実際に載ったかを確認する。
            try:
                refs_after = cmds.file(q=True, reference=True) or []
                target_norm = _normalize_for_compare(file_path)
                actually_referenced = any(
                    _normalize_for_compare(r) == target_norm for r in refs_after
                )
            except Exception:
                actually_referenced = True  # 確認自体に失敗した場合は判定しない

            if actually_referenced:
                self._log("Reference完了: {}".format(file_path), level="run")
            else:
                self._log(
                    "Reference処理は例外を出さずに終了しましたが、参照一覧に"
                    "登録されていません。Maya本体側でエラーが出ていないか"
                    "Script Editorをご確認ください: {}".format(file_path),
                    level="warn",
                )
        except Exception as exc:
            self._log("Reference失敗: {} ({})".format(file_path, exc), level="error")
            traceback.print_exc()

    # ------------------------------------------------------------------
    # 外部アプリ / プレビュー
    # ------------------------------------------------------------------
    def _open_with_os_default(self, file_path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(file_path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", file_path])
            else:
                subprocess.Popen(["xdg-open", file_path])
            self._log("OS標準アプリで開きました: {}".format(file_path), level="info")
        except Exception as exc:
            self._log("OS標準アプリで開けませんでした: {} ({})".format(file_path, exc), level="error")
            traceback.print_exc()

    def _register_modeless_dialog(self, dialog):
        """
        [修正5] モードレスダイアログの参照を保持し、閉じられたら解放する。

        Python側で参照を持たないと生成直後にGCされてウィンドウが消えるため
        リストで保持し、destroyedシグナルで取り除く。
        """
        dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self._preview_dialogs.append(dialog)

        def _on_destroyed(*_args):
            if dialog in self._preview_dialogs:
                self._preview_dialogs.remove(dialog)

        dialog.destroyed.connect(_on_destroyed)
        dialog.show()

    def _get_sibling_image_paths(self, file_path):
        """
        [rev.04追加] file_path と同じフォルダ内にある画像ファイルの一覧を、
        表示名の昇順（大文字小文字を区別しない）で返す。矢印キー移動の
        循環対象リストとして使う。取得に失敗した場合は file_path 自身のみの
        1件リストにフォールバックする。
        """
        folder = os.path.dirname(file_path)
        try:
            entries = os.listdir(folder)
        except OSError:
            return [file_path]

        image_paths = []
        for name in entries:
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            if ext not in IMAGE_EXTENSIONS:
                continue
            full_path = os.path.join(folder, name)
            if os.path.isfile(full_path):
                image_paths.append(full_path)

        if not image_paths:
            return [file_path]

        image_paths.sort(key=lambda p: os.path.basename(p).lower())
        return image_paths

    def _preview_image_in_maya(self, file_path):
        try:
            image_paths = self._get_sibling_image_paths(file_path)
            target_norm = _normalize_for_compare(file_path)
            current_index = 0
            for i, p in enumerate(image_paths):
                if _normalize_for_compare(p) == target_norm:
                    current_index = i
                    break
            else:
                image_paths = [file_path]

            # [修正5] exec_() ではなく show() でモードレス表示し、
            # Maya本体を操作しながら参照できるようにする
            dialog = _ImagePreviewDialog(image_paths, current_index, parent=self)
            self._register_modeless_dialog(dialog)
            dialog.activateWindow()
            dialog.setFocus(QtCore.Qt.OtherFocusReason)
        except Exception as exc:
            self._log("プレビュー表示に失敗しました: {} ({})".format(file_path, exc), level="error")
            traceback.print_exc()

    def _preview_text_in_panel(self, file_path):
        try:
            max_bytes = 200000
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_bytes + 1)

            truncated = len(content) > max_bytes
            if truncated:
                content = content[:max_bytes]

            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(os.path.basename(file_path))
            dialog.resize(700, 500)
            layout = QtWidgets.QVBoxLayout(dialog)

            text_edit = QtWidgets.QPlainTextEdit()
            text_edit.setReadOnly(True)
            text_edit.setPlainText(content)
            text_edit.setFont(QtGui.QFont("Consolas", 10))
            layout.addWidget(text_edit)

            if truncated:
                warn_label = QtWidgets.QLabel(
                    "※ ファイルサイズが大きいため、先頭 {} 文字のみ表示しています。".format(max_bytes)
                )
                warn_label.setStyleSheet("color: {};".format(COLOR_WARN))
                layout.addWidget(warn_label)

            self._register_modeless_dialog(dialog)
        except Exception as exc:
            self._log("テキストプレビューに失敗しました: {} ({})".format(file_path, exc), level="error")
            traceback.print_exc()

    def _create_file_node(self, file_path):
        try:
            # [修正6] ノード生成と18本の接続をアンドゥ1回にまとめる
            with _undo_chunk("AssetBrowser Create File Node"):
                file_node = cmds.shadingNode("file", asTexture=True, isColorManaged=True)
                place2d_node = cmds.shadingNode("place2dTexture", asUtility=True)

                connections = [
                    ("outUV", "uvCoord"),
                    ("outUvFilterSize", "uvFilterSize"),
                    ("vertexUvOne", "vertexUvOne"),
                    ("vertexUvTwo", "vertexUvTwo"),
                    ("vertexUvThree", "vertexUvThree"),
                    ("vertexCameraOne", "vertexCameraOne"),
                    ("coverage", "coverage"),
                    ("mirrorU", "mirrorU"),
                    ("mirrorV", "mirrorV"),
                    ("noiseUV", "noiseUV"),
                    ("offset", "offset"),
                    ("repeatUV", "repeatUV"),
                    ("rotateFrame", "rotateFrame"),
                    ("rotateUV", "rotateUV"),
                    ("stagger", "stagger"),
                    ("translateFrame", "translateFrame"),
                    ("wrapU", "wrapU"),
                    ("wrapV", "wrapV"),
                ]
                for src_attr, dst_attr in connections:
                    cmds.connectAttr(
                        "{}.{}".format(place2d_node, src_attr),
                        "{}.{}".format(file_node, dst_attr),
                        force=True,
                    )

                cmds.setAttr(
                    "{}.fileTextureName".format(file_node), file_path, type="string"
                )

            self._log("Fileノードを作成しました: {} ({})".format(file_node, file_path), level="run")
        except Exception as exc:
            self._log("Fileノード作成に失敗しました: {} ({})".format(file_path, exc), level="error")
            traceback.print_exc()

    def _reveal_in_explorer(self, path):
        try:
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            if sys.platform.startswith("win"):
                os.startfile(folder)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            self._log("エクスプローラーで開けませんでした: {}".format(exc), level="warn")

    # ------------------------------------------------------------------
    # 終了時処理
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        # [自己指摘対応] hideEvent での保存は、ワークスペースのタブ切替でも
        # 発火して書き込み頻度が過剰になるため廃止した。
        # 各操作時に都度保存しているため closeEvent のみで十分。
        self._save_settings()
        super(FBXBrowserWidget, self).closeEvent(event)


class FBXBrowserDockableWindow(MayaQWidgetDockableMixin, QtWidgets.QWidget):
    """ドッキング可能なウィンドウのラッパー。"""

    def __init__(self, parent=None):
        super(FBXBrowserDockableWindow, self).__init__(parent=parent)
        self.setObjectName(WINDOW_OBJECT_NAME)
        self.setWindowTitle(WINDOW_TITLE)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.browser_widget = FBXBrowserWidget(self)
        layout.addWidget(self.browser_widget)


_fbx_browser_window_instance = None


def show_ui(restore=False):
    """
    アセットブラウザパネルを表示する。

    restore=True はMaya起動時にworkspaceControlから自動的に呼ばれる復元経路であり、
    ユーザーが直接指定する必要はない。

    [不具合対応] 手動呼び出し時（restore=False）であっても、workspaceControlが
    既に存在する場合（uiScriptによる自動生成後に手動実行された、前回セッションの
    残骸が残っている等）は、そのまま新規show()を呼ぶと
    「オブジェクト名が固有ではありません」というRuntimeErrorになる。
    既存コントロールが有るかどうかで経路を分け、有るならrestore経路と同様に
    既存コントロールへ差し込む。
    """
    global _fbx_browser_window_instance

    control_exists = cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True)

    if _fbx_browser_window_instance is None:
        # [不具合対応] Maya再起動によりPython側のグローバル参照は失われるが、
        # workspaceControlはレイアウトファイル経由でMaya側に残っている場合がある。
        # この状態で素朴に新規QWidgetを作ると、同名ウィジェットの二重登録により
        # 「オブジェクト名が固有ではありません」が発生し得るため、
        # 既存の孤立ウィジェット（Qt側にだけ残っている残骸）を先に削除しておく。
        stale_ptr = omui.MQtUtil.findControl(WINDOW_OBJECT_NAME)
        if stale_ptr is not None:
            try:
                stale_widget = wrapInstance(int(stale_ptr), QtWidgets.QWidget)
                stale_widget.setParent(None)
                stale_widget.deleteLater()
            except Exception:
                traceback.print_exc()

        _fbx_browser_window_instance = FBXBrowserDockableWindow()

    if restore or control_exists:
        if restore:
            # uiScriptからの呼び出し。Mayaが直前にworkspaceControlを生成済みで、
            # そのカレントの親として渡している。
            restored_control = omui.MQtUtil.getCurrentParent()
        else:
            # 手動呼び出しだが既存コントロールが残っていたケース。
            # そのコントロール自体をQtウィジェットとして取得し、親として使う。
            restored_control = omui.MQtUtil.findControl(WORKSPACE_CONTROL_NAME)

        mixin_ptr = omui.MQtUtil.findControl(_fbx_browser_window_instance.objectName())
        omui.MQtUtil.addWidgetToMayaLayout(int(mixin_ptr), int(restored_control))

        if not restore:
            # 手動呼び出し時は、既存コントロールへ差し込んだ後に
            # 可視化・フォーカスを明示しておく（自動生成直後は非表示のことがある）
            cmds.workspaceControl(WORKSPACE_CONTROL_NAME, e=True, visible=True)
            cmds.evalDeferred(
                lambda: cmds.workspaceControl(WORKSPACE_CONTROL_NAME, e=True, r=True)
            )
    else:
        _fbx_browser_window_instance.show(
            dockable=True,
            area="right",
            floating=False,
            uiScript="import fbx_browser\nfbx_browser.show_ui(restore=True)",
        )

    return _fbx_browser_window_instance


def delete_ui():
    """パネルとworkspaceControlを完全に削除する（レイアウト記憶もリセット）。"""
    global _fbx_browser_window_instance
    if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True):
        cmds.deleteUI(WORKSPACE_CONTROL_NAME, control=True)
    _fbx_browser_window_instance = None


if __name__ == "__main__":
    show_ui()
