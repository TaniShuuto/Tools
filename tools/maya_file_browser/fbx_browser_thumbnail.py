# -*- coding: utf-8 -*-
"""
fbx_browser_thumbnail.py
-------------------------
fbx_browser.py の画像サムネイル機能を担当する補助モジュール。

設計方針（fbx_browser.py 側の実装計画・不具合洗い出しを踏まえたもの）:

    - サムネイル生成は QThreadPool 上のワーカースレッドで行い、Mayaの
      UIスレッドをブロックしない（ネットワークドライブ上の大量の
      高解像度テクスチャが並ぶフォルダでも操作感を損なわないため）。

    - ワーカースレッドは QPixmap を一切扱わない。QPixmap はGUIスレッド
      専用というQtの制約があり、ワーカー側で生成すると不定動作
      （環境によっては即クラッシュ）になりうる。ワーカーは QImage のみを
      生成し、QPixmapへの変換は必ずメインスレッド側
      （シグナル受信ハンドラ内）で行う。

    - EXR等、Qt標準の画像プラグインが対応していない形式はサムネイル対象外
      とし、汎用表示（デコレーション無し＝既定のファイルアイコンのまま）に
      フォールバックする。Maya API (MImage) 経由でのデコードは、Maya API が
      メインスレッド専用でありワーカースレッドから呼べないため、
      呼び出しをメインスレッドのアイドル時に直列化するキューが別途必要になり
      実装が複雑化する。実用性・保守性を優先し、本フェーズでは対象外とする
      （EXRサムネイルが必要な場合は、別途拡張として検討する）。

    - 一度「生成失敗」と判定した項目は失敗として結果をキャッシュし、
      再描画のたびに再試行しない（EXR等を毎回再生成しようとして
      スレッドプールへ無駄なジョブを積み続ける事態を防ぐ）。

    - ディスクキャッシュは一時ファイル+os.replace()による原子的な書き込みを
      行う。Mayaを複数起動している環境で同時に同じキャッシュファイルへ
      書き込んでも、書きかけの破損した画像が読まれることはない
      （os.replaceは同一ボリューム内でアトミック）。

    - フォルダ切り替え時は世代ID（generation）を進め、古い世代に対する
      ワーカーの結果は破棄する。フォルダを連続で切り替えた際に、
      表示されなくなった旧フォルダ向けのサムネイル生成が積み上がり
      続ける事態を防ぐ。

このモジュールは fbx_browser.py から「サムネイルを表示」オプションが
ONになった時にのみ遅延importされる。本モジュール、あるいはPySide側の
環境に問題があってimportに失敗しても、fbx_browser.py本体の動作
（サムネイル無し）には影響しない設計とすること（呼び出し側でtry/except
すること）。
"""
import os
import hashlib
import tempfile
from collections import OrderedDict

try:
    from PySide6 import QtCore, QtGui
except ImportError:
    from PySide2 import QtCore, QtGui


# メモリキャッシュの最大保持件数（LRU）。
MEMORY_CACHE_LIMIT = 400

# ディスクキャッシュの合計サイズ上限（バイト）。超過分は古い順に削除する。
DISK_CACHE_MAX_BYTES = 200 * 1024 * 1024  # 200MB

# サムネイル生成に使うワーカースレッドの最大数。
# 多すぎるとディスクI/O競合でむしろ遅くなるため、控えめな値にする。
THREAD_POOL_MAX_WORKERS = 2


def _get_disk_cache_dir():
    """OS別の設定ディレクトリ配下にキャッシュ用ディレクトリを用意する。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "NanoTools", "AssetBrowser", "thumbs")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def _cache_key(file_path, size_px):
    """
    ディスクキャッシュのキーを算出する。

    絶対パス・更新時刻・サイズ・サムネイルサイズを含めることで、
    元ファイルが更新されれば自動的に別キーになる（＝古いキャッシュを
    明示的に無効化する処理が不要になる）。
    """
    try:
        stat = os.stat(file_path)
        raw = "{}|{}|{}|{}".format(file_path, stat.st_mtime, stat.st_size, size_px)
    except OSError:
        raw = "{}|{}".format(file_path, size_px)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


def _prune_disk_cache_if_needed(cache_dir):
    """
    [rev.06追加] ディスクキャッシュの合計サイズが上限を超えたら、
    更新時刻の古い順に削除する。呼び出し頻度を抑えるため、
    ワーカー側から書き込みのたびに毎回フルスキャンするのではなく、
    一定確率（ここでは呼び出し元で頻度を制御）でのみ実行される想定。
    """
    try:
        entries = []
        total = 0
        with os.scandir(cache_dir) as it:
            for entry in it:
                if entry.is_file():
                    stat = entry.stat()
                    entries.append((stat.st_mtime, stat.st_size, entry.path))
                    total += stat.st_size

        if total <= DISK_CACHE_MAX_BYTES:
            return

        entries.sort(key=lambda e: e[0])  # 古い順
        for _mtime, size, path in entries:
            if total <= DISK_CACHE_MAX_BYTES:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass
    except Exception:
        pass  # キャッシュ掃除の失敗はサムネイル機能自体を止めるべきではない


class _ThumbnailSignals(QtCore.QObject):
    """QRunnableはQObjectを継承できないため、シグナル専用の橋渡し役。"""

    ready = QtCore.Signal(str, int, object)  # file_path, generation, QImage or None


class _ThumbnailWorker(QtCore.QRunnable):
    """
    [rev.06追加] 1件の画像のサムネイルをバックグラウンドで生成するジョブ。

    重要: このクラスの run() はワーカースレッド上で実行される。
    QPixmapは一切生成・操作しない（QImageのみを扱う）。
    """

    def __init__(self, file_path, size_px, generation, signals):
        super(_ThumbnailWorker, self).__init__()
        self._file_path = file_path
        self._size_px = size_px
        self._generation = generation
        self._signals = signals

    def run(self):
        try:
            image = self._generate()
        except Exception:
            image = None
        self._signals.ready.emit(self._file_path, self._generation, image)

    def _generate(self):
        cache_dir = _get_disk_cache_dir()
        key = _cache_key(self._file_path, self._size_px)
        disk_path = os.path.join(cache_dir, key + ".png")

        if os.path.isfile(disk_path):
            image = QtGui.QImage(disk_path)
            if not image.isNull():
                return image

        reader = QtGui.QImageReader(self._file_path)
        reader.setAutoTransform(True)
        if not reader.canRead():
            # [既知の制約] EXR等、Qt標準の画像プラグインが非対応の形式。
            # モジュールdocstring記載の方針により、ここでは対象外として扱う。
            return None

        original_size = reader.size()
        if (
            original_size.isValid()
            and original_size.width() > 0
            and original_size.height() > 0
        ):
            long_side = max(original_size.width(), original_size.height())
            scale = self._size_px / float(long_side)
            if scale < 1.0:
                reader.setScaledSize(
                    QtCore.QSize(
                        max(1, int(original_size.width() * scale)),
                        max(1, int(original_size.height() * scale)),
                    )
                )

        image = reader.read()
        if image.isNull():
            return None

        try:
            fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
            os.close(fd)
            if image.save(tmp_path, "PNG"):
                # [rev.06不具合対応 B-6] 一時ファイル経由 + os.replace() による
                # 原子的な置き換え。Mayaを複数起動している環境で同時に
                # 同じキャッシュファイルへ書き込んでも、書きかけの破損した
                # 画像が読まれることはない。
                os.replace(tmp_path, disk_path)
            else:
                os.remove(tmp_path)
        except Exception:
            pass  # ディスクキャッシュへの書き込み失敗はサムネイル表示自体を妨げない

        return image


class ThumbnailManager(QtCore.QObject):
    """
    [rev.06追加] 画像サムネイルの生成・キャッシュを管理するクラス。

    使い方:
        manager = ThumbnailManager()
        manager.thumbnail_ready.connect(on_ready)  # on_ready(file_path)

        cached = manager.get_cached_pixmap(file_path, size_px)
        if ThumbnailManager.is_failed(cached):
            ...（サムネイル非対応。既定アイコンのまま）...
        elif cached is not None:
            icon = QtGui.QIcon(cached)
        else:
            manager.request(file_path, size_px)  # 完了時にthumbnail_readyが発火
    """

    thumbnail_ready = QtCore.Signal(str)  # file_path

    # 「生成を試みたが失敗した」ことを示す番兵オブジェクト。
    # None（未生成）と区別するために使う。
    FAILED = object()

    def __init__(self, parent=None):
        super(ThumbnailManager, self).__init__(parent)
        self._memory_cache = OrderedDict()  # (path, size_px) -> QPixmap または FAILED
        self._pending = set()  # (path, size_px)
        self._generation = 0
        self._disk_prune_counter = 0

        self._thread_pool = QtCore.QThreadPool()
        self._thread_pool.setMaxThreadCount(THREAD_POOL_MAX_WORKERS)

        self._signals = _ThumbnailSignals()
        self._signals.ready.connect(self._on_worker_done)

    @classmethod
    def is_failed(cls, value):
        return value is cls.FAILED

    def bump_generation(self):
        """
        フォルダ切り替え時に呼ぶ。実行中/待機中のワーカーの結果は、
        完了時にこの世代番号と一致しなければ無視される
        （＝もう表示されていないフォルダ向けの結果でUIを更新しない）。
        待機中とマークされていたキーもクリアし、再度そのフォルダへ
        戻った際に改めてリクエストできるようにする。
        """
        self._generation += 1
        self._pending.clear()

    def clear_memory_cache(self):
        self._memory_cache.clear()
        self._pending.clear()

    def get_cached_pixmap(self, file_path, size_px):
        key = (file_path, size_px)
        if key in self._memory_cache:
            value = self._memory_cache[key]
            self._memory_cache.move_to_end(key)
            return value
        return None

    def request(self, file_path, size_px):
        """まだキャッシュに無ければ、バックグラウンドでの生成をスケジュールする。"""
        key = (file_path, size_px)
        if key in self._memory_cache or key in self._pending:
            return
        self._pending.add(key)
        worker = _ThumbnailWorker(file_path, size_px, self._generation, self._signals)
        self._thread_pool.start(worker)

    def _on_worker_done(self, file_path, generation, image):
        # [rev.06追加] 古い世代（＝既に離れたフォルダ）向けの結果は破棄する。
        if generation != self._generation:
            return

        # 同一file_pathで複数サイズがpending中の可能性があるため、該当キー全てを処理する。
        matching_keys = [k for k in self._pending if k[0] == file_path]
        if not matching_keys:
            return

        for key in matching_keys:
            self._pending.discard(key)
            if image is None:
                self._memory_cache[key] = ThumbnailManager.FAILED
            else:
                # [rev.06不具合対応 B-1] QPixmapへの変換は必ずここ（メインスレッドで
                # 実行されるシグナルハンドラ内）で行う。ワーカー側はQImageのみを扱う。
                self._memory_cache[key] = QtGui.QPixmap.fromImage(image)
            self._memory_cache.move_to_end(key)

        while len(self._memory_cache) > MEMORY_CACHE_LIMIT:
            self._memory_cache.popitem(last=False)

        self._disk_prune_counter += 1
        if self._disk_prune_counter >= 20:
            self._disk_prune_counter = 0
            _prune_disk_cache_if_needed(_get_disk_cache_dir())

        self.thumbnail_ready.emit(file_path)
