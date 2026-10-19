"""
sp_live_sync_plugin.py
Substance 3D Painter 側プラグイン(SP Live Sync, B案:自前パイプライン)

変更履歴はリポジトリルートの CHANGELOG.md を参照。
"""

# バージョン情報。SP起動時に必ずPythonログへ出力し、「今動いているのが
# どの版か」を即座に確認できるようにする(maya_live_sync.py と同じ方式)。
#
# 変更履歴(日付入り)はリポジトリルートの CHANGELOG.md を参照。
__version__ = "1.4.0"

import os
import re
import json
import copy
import stat
import time
import uuid
import shutil
import hashlib
import math
import inspect
import functools
import unicodedata
import tempfile
import datetime
import contextlib
from collections import namedtuple
try:
    import msvcrt
except ImportError:
    # このツールはWindows専用設計(C:/SPMayaLiveSync のハードコード等)
    # だが、念のためimport失敗時は設定ファイルロックを無効化して
    # 動作は継続できるようにする(_config_file_lock()参照)。
    msvcrt = None

import substance_painter.event as event
import substance_painter.export as export
import substance_painter.project as project
import substance_painter.textureset as textureset
import substance_painter.logging as sp_log
import substance_painter.ui as ui
import substance_painter.exception as sp_exception

# Substance Painter 10.1 以降は内部UIがPySide6へ移行しており、
# PySide2 モジュールが存在しない環境がある(逆に10.1未満はPySide6が無い)。
# そのため両対応させるためのフォールバックを行う。
try:
    from PySide2 import QtCore, QtWidgets, QtGui
except ImportError:
    from PySide6 import QtCore, QtWidgets, QtGui


# ---------------------------------------------------------------------------
# 設定の保存先(GUIから編集されるため、通常は直接触らなくてよい)
# ---------------------------------------------------------------------------

CONFIG_DIR = os.environ.get("SPMAYA_CONFIG_DIR", "C:/SPMayaLiveSync")
CONFIG_PATH = os.path.join(CONFIG_DIR, "live_sync_config.json")

# ---------------------------------------------------------------------------
# 2026.07.19-01: 保存直後のFinal書き出しに関する再試行パラメータ
# ---------------------------------------------------------------------------
# Painterは保存直後、内部の保存ロックが残っていることがあり(公式には
# BusyStatusChanged/is_busy() では検知できないことをAdobe公式コミュニティ
# でも確認済み)、その状態でexport_project_textures()を呼ぶと
# substance_painter.exception.ProjectError が送出される。従来は固定500ms
# 待機で回避していたが、環境によって足りない/長すぎることがあるため、
# 実際にProjectErrorが発生した場合にのみ、間隔を倍々に広げながら
# 再試行する方式に変更した。
_FINAL_EXPORT_INITIAL_DELAY_MS = 200     # 保存イベントから最初の試行までの遅延
_FINAL_EXPORT_QUEUED_DELAY_MS = 200      # 保存連打でキューされた再実行までの遅延
_FINAL_EXPORT_RETRY_BASE_MS = 200        # 再試行間隔の基準値(倍々に広がる)
_FINAL_EXPORT_RETRY_MAX_MS = 6400        # 再試行間隔の上限
_FINAL_EXPORT_MAX_RETRIES = 6            # 最大再試行回数(初回200ms+再試行分の累計で最大約19秒粘る)
# 2026.07.24-02: 上記の再試行(最大約19秒)を使い切って打ち切った後、
# 一度だけ自動的に追加でもう一度試みるまでの遅延。保存直後のロックが
# 通常より長引いた(重いプロジェクト・低速ディスク等)場合でも、
# ユーザーが手動で再保存/「今すぐ全量同期」しなくても自己回復できる
# ようにするための猶予。値は実機テストで調整可能。
_FINAL_EXPORT_GIVEUP_FOLLOWUP_MS = 30000

# 2026.10.18(1.3.0): 書き出し結果ファイルと、読み込んだメッシュの記録。
# 結果ファイルは _sync_complete.flag と同じフォルダへ、印より先に書く。
# 印の条件(1件以上移動したとき)と中身(time.time() の文字列)は変えない。
# 読み手は印の mtime だけを見ている。
SYNC_RESULT_NAME = "_sync_result.json"
SYNC_FLAG_NAME = "_sync_complete.flag"
EXPORT_RESULT_SCHEMA = 1
EXPORT_MESSAGE_MAX_CHARS = 500
# メッシュの sha1 は 1MB 単位で読む(_file_digest と同じチャンク)。
# この大きさを超えるファイルは sha1 を null にする(「超える」なので
# ちょうどこの大きさは計算する)。
_HASH_CHUNK_BYTES = 1024 * 1024
MESH_SHA1_MAX_BYTES = 1024 * 1024 * 1024
# ProjectEditionEntered のあと、名前付き単発タイマーでメッシュを記録する
# までの待ち(ミリ秒)。0 はイベントループの次の回。
# この待ちで last_imported_mesh_path() が埋まるかは要実機確認。
_MESH_RECORD_DELAY_MS = 0
# 2026.10.18(1.3.0): パスが空、または Painter が busy のときは 500ms 後に
# 最大 2 回だけ再試行する。メインスレッドは sleep しない。
_MESH_RECORD_RETRY_DELAY_MS = 500
_MESH_RECORD_MAX_RETRIES = 2

_EXPORT_STATUS_TOKENS = {
    "success": "success",
    "warning": "warning",
    "error": "error",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "exception": "exception",
}
_EXPORT_STATUS_LABELS = {
    "success": "成功",
    "warning": "警告",
    "error": "エラー",
    "cancelled": "キャンセル",
    "exception": "例外",
}

DEFAULT_CONFIG = {
    "staging_dir": "C:/SPMayaLiveSync/staging",
    "watch_dir": "C:/SPMayaLiveSync/live",
    "final_export_dir": "C:/SPMayaLiveSync/final",
    "preview_resolution_log2": 9,
    "final_resolution_log2": 12,
    "debounce_seconds": 1.5,
    "file_format": "png",
    "bit_depth": "8",
    "texture_sets": [],
    "renderer": "arnold",
    "raw_colorspace_suffixes": ["Roughness", "Metallic", "Normal", "Height", "AO"],
    # Phase 2 最適化: プロジェクトごとに既知のテクスチャセット一覧を
    # 分けて保持する。キーはプロジェクトファイルパス(未保存の場合は
    # "__unsaved__")。
    "known_texture_sets_by_project": {},
    # 2026.07.15-01: 以前はテクスチャセット名だけをキーにしたフラットな
    # 辞書 "texture_set_shading_engine_map" だったが、これはMaya側専用の
    # データ(SP側は書き込まない)であるため、SP側のDEFAULT_CONFIGからは
    # 削除した(旧キーがMaya側の設定ファイルに残っていても、Maya側の
    # load_config()が自動移行する)。
    # Phase 3: 複数SPプロジェクトを横断して作業した場合に、Maya側が
    # 「どのプロジェクトのテクスチャセット一覧を見ればよいか」を機械的に
    # 判別できるよう、現在アクティブなプロジェクトのキーを共有する。
    "active_project_key": None,
    # 複数プロジェクト対応: Final は <final_export_dir>/<subfolder>/ に
    # 書き出す。現在アクティブなプロジェクトの Final サブフォルダ名を
    # 共有し、Maya側(aiSSボタン)が正しい取り込み先を自動入力できるようにする。
    "active_final_subfolder": None,
    # 複数プロジェクト対応(所有権問題回避、2026.07.14-02): Live/Preview は
    # <watch_dir>/<subfolder>/ に書き出す。現在アクティブなプロジェクトの
    # 監視先サブフォルダ名を共有し、Maya側(監視処理)が追従できるようにする。
    "active_watch_subfolder": None,
    # 2026.07.21(Phase 1, 複数プロジェクト並行対応の根治): 上記の
    # active_watch_subfolder / active_final_subfolder は「SP側が今
    # 開いている1プロジェクト」でグローバル上書きされるスカラー値の
    # ため、複数のSPプロジェクトを同一Mayaシーンに並行して紐付けて
    # 作業すると、後から開いた方の値で前の方が上書きされてしまい、
    # Maya側が同時に追跡できるプロジェクトが実質1つに限定される
    # 不具合があった(このリポジトリで報告された緊急バグの根本原因)。
    # 対策として、texture_set_export_prefix_by_project と同じ
    # 「project_key -> 値」のネスト辞書形式でサブフォルダ名を記録する
    # キーを新設した。既存の active_watch_subfolder / active_final_
    # subfolder は後方互換のため残置し、以下の役割に限定する:
    #   - 未対応の古いMaya側との互換性を保つためのフォールバック値
    #   - aiSS(sp_to_aiStandardSurface.py)のフォルダ欄自動入力用の
    #     「代表1件(直近アクティブなプロジェクト)」の値
    # 複数プロジェクトの並行追跡そのものは、Maya側がこちらの
    # by_project 辞書を正として参照するようになる。
    "watch_subfolder_by_project": {},
    "final_subfolder_by_project": {},
    # Phase 3: SP側が実際に書き出したファイル名のprefixを記録する。
    # Maya側はテクスチャセット名を自前で安全化(_safe_name)して予測する
    # 代わりに、この値があれば最優先で使うことで、スペースや日本語を
    # 含む名前でのズレを防ぐ。
    # 2026.07.15-01: known_texture_sets_by_project と同様、プロジェクト
    # キーでネストする形式に変更した(別プロジェクトの同名テクスチャ
    # セットでprefixを取り違える不具合の対策):
    #   { project_key: { texture_set_name: prefix文字列 } }
    "texture_set_export_prefix_by_project": {},
    # 2026.10.18(1.3.0): Painter が今読み込んでいるメッシュ(G-03)。
    # 後続のリベース(1.4.0)が、基準メッシュと突き合わせるために使う。
    # { project_key: {path, size, mtime_ns, sha1, recorded_at, plugin_version} }
    # 未保存プロジェクト(__unsaved__ 系)は記録しない。
    "painter_mesh_by_project": {},
    # Phase 3: 初回セットアップウィザードを完了したかどうか。
    "setup_wizard_completed": False,
    # 2026.07.19-03(フェーズ2): 保存連打への対策として、前回のFinal
    # 書き出し完了からこの秒数が経過するまでは次のFinalを起こさない
    # (要素技術C: クールダウン)。「今すぐ全量同期」ボタンからの手動
    # 実行はこのクールダウンを無視して即座に実行される。
    "final_cooldown_seconds": 5.0,
    # 2026.10.19(1.4.0 PB-a): リベース(Painter 側)。画面には出さない。
    # rebase_approval = "auto" は設定ファイルの直接編集でのみ。
    # "auto" は 3D ビューでのみ描く運用向け(2D の内容が崩れても
    # 承認の場面が無く気付きにくいため)。
    "rebase_enabled": True,
    "rebase_approval": "ask",
    "rebase_auto_idle_s": 10.0,
    "rebase_reload_timeout_s": 600.0,
    "rebase_retry_max": 5,
    "rebase_wait_max_s": 1800.0,
    "rebase_reproject_uv_fills": True,
}

# Phase 3: 書き出しファイル名の判定・Maya側との橋渡しで共通して使う
# チャンネルsuffix一覧(maya_live_sync.py の CHANNEL_SUFFIXES と対応)。
CHANNEL_SUFFIXES = ["BaseColor", "Roughness", "Metallic", "Normal", "Height", "Emissive"]

RESOLUTION_CHOICES = [
    ("256 px", 8),
    ("512 px", 9),
    ("1024 px", 10),
    ("2048 px", 11),
    ("4096 px", 12),
    ("8192 px", 13),
]


def _log(level, message):
    # substance_painter.logging には Info/Warning/Error のような大文字定数は無く、
    # info()/warning()/error() という小文字の関数が用意されている
    # (level には "info"/"warning"/"error" の文字列を渡す)。
    try:
        log_fn = getattr(sp_log, level, None)
        if log_fn is not None:
            log_fn("[LiveSync] {0}".format(message))
        else:
            sp_log.info("[LiveSync] {0}".format(message))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 5: 同期履歴ログ(Maya側と共通のファイルに追記する)
# ---------------------------------------------------------------------------

HISTORY_LOG_PATH = os.path.join(CONFIG_DIR, "livesync_history.log")
_HISTORY_MAX_BYTES = 512 * 1024  # 500KB。超えたら直近1000行のみ残す。


def _append_history(source, text):
    """SP/Maya共通の同期履歴ログに1行追記する(maya_live_sync.pyの
    _append_history()と同一フォーマット)。失敗しても同期処理自体は
    継続できるよう、例外は握りつぶす。
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = "[{0}] [{1}] {2}\n".format(stamp, source, text)
        if os.path.isfile(HISTORY_LOG_PATH) and os.path.getsize(HISTORY_LOG_PATH) > _HISTORY_MAX_BYTES:
            with open(HISTORY_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            with open(HISTORY_LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines[-1000:])
        with open(HISTORY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 2026.07.19-01: ステージングフォルダの自己修復(クラッシュ対策フェーズ1)
# ---------------------------------------------------------------------------

def _cleanup_orphaned_staging_dirs(stage_root, max_age_hours=24):
    """前回のセッションがクラッシュ等で異常終了し、_do_export() の
    finally節(shutil.rmtree)が実行されなかった場合に staging_dir 配下へ
    残り続ける "sp_live_*" 一時フォルダを、起動時に自動回収する。

    安全のため、対象は "sp_live_" プレフィックスを持つフォルダに限定し、
    かつ最終更新から一定時間(既定24時間)以上経過したものだけを削除する。
    これにより、実行中の別セッションが今まさに使っている一時フォルダを
    誤って削除する事故を避ける。start_plugin() から呼ばれる想定で、
    失敗しても起動処理自体は継続できるよう、例外は呼び出し側で
    握りつぶす前提とする。
    """
    if not stage_root or not os.path.isdir(stage_root):
        return
    now = time.time()
    removed = []
    for name in os.listdir(stage_root):
        if not name.startswith("sp_live_"):
            continue
        path = os.path.join(stage_root, name)
        try:
            if not os.path.isdir(path):
                continue
            age_hours = (now - os.path.getmtime(path)) / 3600.0
            if age_hours >= max_age_hours:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(name)
        except OSError:
            continue
    if removed:
        _log("info", "起動時クリーンアップ: 残留ステージングフォルダを{0}件削除しました。".format(
            len(removed)))


# ---------------------------------------------------------------------------
# 2026.07.25(緊急バグ修正): 共有フォルダのクラウド同期検出
# ---------------------------------------------------------------------------
#
# 背景: 実機調査で C:/SPMayaLiveSync 直下に ".tmp.driveupload"(Google
# Drive デスクトップ版が能動的に同期している際に作る一時フォルダ)が
# 見つかった。live_sync_config.json.lock による msvcrt.locking の
# アドバイザリロックと、一時ファイル+os.replace()によるアトミックな
# 置き換えは、いずれも「このフォルダをファイルシステム以外の何かが
# 横から書き換えない」ことを前提にしている。クラウド同期クライアントは
# 独自にファイルハンドルを保持し、同期のたびにファイルを差し替える/
# 巻き戻すため、この前提が崩れる。実際に調査中、live_sync_config.json
# を数分内に2回読んだだけで内容が別物(古い内容への巻き戻り)だった
# ことを確認している。save_config()はロック取得タイムアウトを警告ログ
# のみで飲み込んで続行する設計のため、この種の巻き戻りは検知されずに
# 通ってしまう。
#
# 対策方針(相談の結果、以下に確定): コード側から同期を止めることは
# できない(ユーザー側の設定変更が必要)ため、起動時に検出して状態バー・
# 共有履歴ログへ警告するに留める。強制的な変更は行わない。
# ---------------------------------------------------------------------------

_CLOUD_SYNC_TEMP_MARKERS = (
    ".tmp.driveupload",   # Google Drive for desktop(能動的にアップロード中)
    ".dropbox.cache",     # Dropbox
    "#recycle",           # Dropbox
    ".365_shell",         # OneDriveの一部バージョン
)
_CLOUD_SYNC_PATH_MARKERS = (
    "google drive", "googledrive", "onedrive", "dropbox",
    "icloud drive", "icloudドライブ",
)


def _detect_cloud_sync_risk(directory):
    """directory(共有フォルダのルート)がクラウド同期クライアントの
    管理下にあるらしいかどうかを推測し、該当すればその根拠を短い日本語
    文字列で返す(該当しなければNone)。

    判定材料(いずれか該当で警告、詳細は上のコメント参照):
      1. 同期クライアントの一時フォルダの存在
      2. パスが既知の同期フォルダ名を含む
      3. ディレクトリ自体がreparse point(クラウドのプレースホルダ/
         シンボリックリンク等)として認識されている

    誤検知(false positive)の可能性は残るが、警告を出すだけで実害は
    無いため、多少広めに倒して判定する。
    """
    if not directory:
        return None
    try:
        entries = os.listdir(directory)
    except OSError:
        entries = []
    for marker in _CLOUD_SYNC_TEMP_MARKERS:
        if marker in entries:
            return "同期クライアントの一時フォルダ({0})が見つかりました".format(marker)

    normalized = directory.replace("\\", "/").lower()
    for marker in _CLOUD_SYNC_PATH_MARKERS:
        if marker in normalized:
            return "パスに既知のクラウド同期フォルダ名({0})が含まれています".format(marker)

    try:
        attrs = os.lstat(directory).st_file_attributes
    except (OSError, AttributeError):
        attrs = None
    if attrs is not None and (attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        return "フォルダがreparse point(クラウドのプレースホルダ等)として認識されています"

    return None


# ---------------------------------------------------------------------------
# 設定の読み書き
# ---------------------------------------------------------------------------

def load_config():
    # 2026.07.24(緊急バグ修正): (1) 読み込み失敗時にログが一切出ておらず
    # (このファイルの他の例外処理は全て_log()を呼んでいる)、設定ファイルが
    # 破損していても無言で既定値にフォールバックしていたため原因調査が
    # 困難だった。(2) dict(DEFAULT_CONFIG)は浅いコピーのため、
    # DEFAULT_CONFIG内のネストした辞書(watch_subfolder_by_project等)が
    # このフォールバック経路を通るたびに同じオブジェクトとして共有されて
    # おり、呼び出し元がその場でin-place更新すると、モジュールレベルの
    # "定数"であるはずのDEFAULT_CONFIGを汚染しうる不具合があった。
    # copy.deepcopy()に変更して解消する。
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg.update(loaded)
        return cfg
    except Exception as e:
        if os.path.exists(CONFIG_PATH):
            _log("warning", "設定ファイルの読み込みに失敗したため、既定値にフォールバックします: {0}".format(e))
        return copy.deepcopy(DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# 2026.07.24: 設定ファイルの同時書き込み競合対策(ファイルロック)
# ---------------------------------------------------------------------------
#
# maya_live_sync.py 側に実装した _config_file_lock() と同じ設計・同じ
# ロックファイルパス・同じタイムアウト値を、共有モジュールが無いため
# ここでも独立実装する(詳細な背景・設計理由はmaya_live_sync.py側の
# コメント参照)。専用ロックファイル(CONFIG_PATH + ".lock")に対する
# msvcrt.locking()はOSレベルのロックのため、保持中にプロセスが
# クラッシュしてもOSがプロセス終了時に自動解放する(手動でのstale判定・
# 強制解除が不要)。取得はノンブロッキングでポーリングしつつタイムアウト
# (既定3秒)を設け、取得できなければロック無しで保存を続行する。
CONFIG_LOCK_PATH = CONFIG_PATH + ".lock"
_CONFIG_LOCK_TIMEOUT_SECONDS = 3.0
_CONFIG_LOCK_POLL_INTERVAL_SECONDS = 0.05


class _ConfigLockTimeout(Exception):
    """設定ファイルロックの取得がタイムアウトした場合に送出する。"""
    pass


@contextlib.contextmanager
def _config_file_lock():
    if msvcrt is None:
        yield
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    lock_file = open(CONFIG_LOCK_PATH, "a+b")
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        deadline = time.time() + _CONFIG_LOCK_TIMEOUT_SECONDS
        locked = False
        while True:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.time() >= deadline:
                    break
                time.sleep(_CONFIG_LOCK_POLL_INTERVAL_SECONDS)
        if not locked:
            raise _ConfigLockTimeout(
                "設定ファイルロックの取得がタイムアウトしました: {0}".format(CONFIG_LOCK_PATH))
        try:
            yield
        finally:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
    finally:
        lock_file.close()


def _merge_and_write_config(cfg):
    """ディスク上の最新内容を読み込んでcfgをマージし、一時ファイル経由で
    原子的に書き戻す(save_config()の実処理本体、ロック取得の成否に
    関わらず共通で使う)。

    2026.07.24(緊急バグ修正): 設定ファイルが存在するのに読み込みに失敗
    した(破損・書き込み途中との衝突等)場合、従来はmerged={}のまま
    書き込みを強行しており、他アプリ(Maya側)が保持している全キーを
    丸ごと消してしまう恐れがあった。ファイルが存在するのに読めない
    ケースでは、空のマージ結果で書き込みを強行するより「今回の保存を
    諦める」方が安全なため、ここで中断する(呼び出し元の変更は失われる
    が、次回の保存機会に委ねる。ファイルがそもそも存在しない初回起動時は
    この分岐に入らず従来通りmerged={}のまま進む)。
    """
    merged = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                merged = json.load(f)
        except Exception as e:
            _log("error", "設定ファイルの読み込みに失敗したため保存を中断しました: {0}".format(e))
            return
    merged.update(cfg)
    # Phase 3 最適化: 一時ファイルに書いてから os.replace() で原子的に
    # 置き換える。SPとMayaが同時に保存/読込を行った際に、書き込み途中の
    # 不完全なJSONを相手側が読んでしまう(torn read)のを防ぐため。
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CONFIG_PATH)


def save_config(cfg):
    """設定を保存する。呼び出し直前にディスク上の最新内容を読み込んで
    からマージして書き込むことで、Maya側が別途保存した値
    (texture_set_shading_engine_map 等)を、こちらの古いメモリ上の値で
    意図せず上書き・消去してしまわないようにする。
    呼び出し側は、変更したキーだけを渡すこと(全体を渡すと、渡した側が
    保持している値が古い場合に他アプリの変更を巻き戻す恐れがある)。

    2026.07.24: 読み込み〜書き込み区間全体を _config_file_lock() で
    挟み、Maya側との同時書き込みによるlost updateを防ぐ。ロックが
    タイムアウトした場合はロック無しで続行する(UIスレッドを無期限に
    ブロックしないことを優先)。
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with _config_file_lock():
            _merge_and_write_config(cfg)
    except _ConfigLockTimeout as e:
        _log("warning", "{0} ロック無しで保存を続行します。".format(e))
        _merge_and_write_config(cfg)


def _update_nested_config_entry(top_key, sub_key, value):
    """2026.10.18(1.3.0): ネストした辞書の 1 エントリだけを更新する。

    save_config() はトップレベルの浅いマージなので、メモリ上の古い
    painter_mesh_by_project を丸ごと渡すと、他プロジェクトの記録を
    消す。ロックを持ったまま「ディスクから読む → 該当キーだけ変える
    → 書く」を行う。読めないときは _merge_and_write_config() と同じく
    保存を中断する。

    戻り値: 書き込んだネスト辞書。中断したときは None。
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)

    def _apply():
        merged = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    merged = json.load(f)
            except Exception as exc:
                _log("error", "設定ファイルの読み込みに失敗したため保存を中断しました: {0}".format(exc))
                return None
            if not isinstance(merged, dict):
                _log("error", "設定ファイルの最上位がオブジェクトではないため保存を中断しました。")
                return None
        existing = merged.get(top_key, {})
        if existing is None:
            existing = {}
        if not isinstance(existing, dict):
            _log("error", "設定 {0} がオブジェクトではないため保存を中断しました。".format(top_key))
            return None
        nested = dict(existing)
        nested[sub_key] = copy.deepcopy(value)
        merged[top_key] = nested
        write_json_atomic(CONFIG_PATH, merged)
        return nested

    try:
        with _config_file_lock():
            return _apply()
    except _ConfigLockTimeout as exc:
        _log("warning", "{0} ロック無しで保存を続行します。".format(exc))
        return _apply()


def _status_token(status_obj):
    """ExportStatus や文字列から比較用のトークンを取り出す。取れなければ None。"""
    if isinstance(status_obj, str):
        token = status_obj.strip()
    else:
        name = getattr(status_obj, "name", None)
        if not isinstance(name, str) or not name.strip():
            return None
        token = name.strip()
    if "." in token:
        token = token.rsplit(".", 1)[-1]
    return token.lower()


def _match_known_export_status(status_obj):
    """name が無いオブジェクトを、ExportStatus の各値との一致で判定する。
    未知、または API が無いときは error。
    """
    try:
        enum_cls = export.ExportStatus
    except Exception:
        return "error"
    for attr, name in (
        ("Success", "success"),
        ("Warning", "warning"),
        ("Error", "error"),
        ("Cancelled", "cancelled"),
    ):
        member = getattr(enum_cls, attr, None)
        if member is None:
            continue
        try:
            if status_obj is member or status_obj == member:
                return name
        except Exception:
            continue
    return "error"


def export_status_name(status_obj):
    """2026.10.18(1.3.0): ExportStatus を結果ファイル用の文字列にする。

    Success → success、Warning → warning、Error → error、
    Cancelled → cancelled。Python の例外は呼び出し側が "exception" を
    渡す。将来の未知の値は error。
    """
    token = _status_token(status_obj)
    if token is None:
        return _match_known_export_status(status_obj)
    return _EXPORT_STATUS_TOKENS.get(token, "error")


def export_status_label(status_name):
    """画面とログに出す状態名。未知のトークンはそのまま出す。"""
    return _EXPORT_STATUS_LABELS.get(status_name, status_name)


def build_export_result(
    plugin_version,
    export_id,
    kind,
    force_full,
    status,
    message,
    moved_count,
    skipped_unchanged_count,
    texture_sets,
    flag_written,
    started_at,
    finished_at,
    request_id=None,
):
    """2026.10.18(1.3.0): _sync_result.json (schema 1) の辞書を組み立てる。

    ok は status が success のときだけ真。warning(一部失敗)は失敗。
    message は 500 文字で切る。無ければ空文字。
    texture_sets は実際に 1 ファイル以上置き換えた名前を昇順で並べる。
    request_id は 1.4.0 のリベース用。1.3.0 の呼び出し側は null。
    """
    status_name = export_status_name(status)
    if message is None:
        text = ""
    elif isinstance(message, str):
        text = message
    else:
        text = str(message)
    if len(text) > EXPORT_MESSAGE_MAX_CHARS:
        text = text[:EXPORT_MESSAGE_MAX_CHARS]
    names = []
    seen = set()
    for name in texture_sets or []:
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        names.append(name)
    names.sort()
    return {
        "schema": EXPORT_RESULT_SCHEMA,
        "plugin_version": plugin_version,
        "export_id": export_id,
        "kind": kind,
        "force_full": bool(force_full),
        "status": status_name,
        "ok": status_name == "success",
        "message": text,
        "moved_count": int(moved_count),
        "skipped_unchanged_count": int(skipped_unchanged_count),
        "texture_sets": names,
        "flag_written": bool(flag_written),
        "started_at": started_at,
        "finished_at": finished_at,
        "request_id": request_id,
    }


def write_json_atomic(path, obj):
    """一時ファイルへ書いてから os.replace() で原子的に置き換える。
    失敗したときは一時ファイルを残さない。
    """
    tmp_path = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_sync_flag(flag_path):
    """_sync_complete.flag を書く。中身は time.time() の文字列。

    2026.07.24 と同じ「一時ファイル → os.replace()」。読み手は印の
    mtime だけを見るので、中身の形式は変えない。
    """
    tmp_flag_path = flag_path + ".tmp"
    with open(tmp_flag_path, "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    os.replace(tmp_flag_path, flag_path)


def publish_export_outputs(dest_root, result_obj, write_flag):
    """2026.10.18(1.3.0): 結果ファイルを先に書き、その後に印を書く。

    write_flag が偽のときは印を書かない(1件も移動していないとき等、
    従来の条件のまま)。結果ファイルの失敗はログに残して書き出しを
    止めない。印の失敗は呼び出し元へ伝える。
    """
    result_path = os.path.join(dest_root, SYNC_RESULT_NAME)
    try:
        write_json_atomic(result_path, result_obj)
    except Exception as exc:
        _log("error", "書き出し結果ファイルの保存に失敗しました(書き出し自体は続行します): {0}".format(exc))
    if write_flag:
        write_sync_flag(os.path.join(dest_root, SYNC_FLAG_NAME))


def _hash_file_hex(path, algorithm, chunk_size=_HASH_CHUNK_BYTES):
    """ファイルをチャンク単位で読みながらハッシュする(_file_digest と同じ作法)。"""
    hasher = hashlib.new(algorithm)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def build_mesh_record(path, prev_record, now, max_hash_bytes=None):
    """2026.10.18(1.3.0): 読み込んだメッシュ 1 件分の記録を作る。

    path が空、またはファイルを stat できないときは None(記録しない。
    既存の値は呼び出し側が消さない)。
    path と size と mtime_ns が前回と同じなら sha1 を計算し直さない。
    1GB 以下の sha1 はこの関数を呼んだスレッド(メインスレッド)で計算する。
    max_hash_bytes を超えるファイルは sha1 を null にする。
    既定の上限は 1GB。テストは引数で小さくする。
    """
    if max_hash_bytes is None:
        max_hash_bytes = MESH_SHA1_MAX_BYTES
    if not isinstance(path, str) or path == "":
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    size = int(st.st_size)
    try:
        mtime_ns = int(st.st_mtime_ns)
    except (AttributeError, TypeError, ValueError):
        return None
    sha1 = None
    reused = False
    if isinstance(prev_record, dict):
        if (prev_record.get("path") == path
                and prev_record.get("size") == size
                and prev_record.get("mtime_ns") == mtime_ns
                and "sha1" in prev_record):
            sha1 = prev_record.get("sha1")
            reused = True
    if not reused:
        if size > int(max_hash_bytes):
            sha1 = None
        else:
            try:
                sha1 = _hash_file_hex(path, "sha1")
            except OSError:
                return None
    return {
        "path": path,
        "size": size,
        "mtime_ns": mtime_ns,
        "sha1": sha1,
        "recorded_at": now,
        "plugin_version": __version__,
    }


def _is_unsaved_project_key(key):
    """未保存プロジェクトのキー(__unsaved__ 系、テンプレート .spt)なら真。"""
    if not isinstance(key, str) or not key:
        return True
    if key == "__unsaved__" or key.startswith("__unsaved__"):
        return True
    if key.lower().endswith(".spt"):
        return True
    return False


# ---------------------------------------------------------------------------
# 2026.10.19(1.4.0 PB-a): リベース(純関数)
# Painter / Qt に触れない。新規 .py は作らない(plugins では各 .py が別プラグイン)。
# Maya の uvremap_rebase は import しない(Painter 側に無い)。書式はバイト単位で合わせる。
# ---------------------------------------------------------------------------

REBASE_SCHEMA = 1
REBASE_DIRNAME = "rebase"
REBASE_REQUESTS_DIRNAME = "requests"
REBASE_STATUS_DIRNAME = "status"
REBASE_STATES = (
    "received",
    "awaiting_approval",
    "preparing",
    "backing_up",
    "reloading",
    "exporting",
    "done",
    "failed",
    "declined",
    "rejected",
    "expired",
    "superseded",
)
REBASE_TERMINAL_STATES = (
    "done",
    "failed",
    "declined",
    "rejected",
    "expired",
    "superseded",
)
REBASE_CANCELABLE_STATES = (
    "received",
    "awaiting_approval",
    "preparing",
)
REBASE_LOCKED_STATES = (
    "backing_up",
    "reloading",
    "exporting",
)
REBASE_REASONS = (
    "disabled",
    "bad_request",
    "bad_settings",
    "project_mismatch",
    "unsaved_project",
    "mesh_missing",
    "mesh_hash_mismatch",
    "busy",
    "auto_unwrap_unverified",
    "unsaved_changes",
    "cancelled",
    "declined",
    "expired",
    "superseded",
    "backup_failed",
    "reload_error",
    "reload_timeout",
    "project_locked",
    "export_failed",
    "interrupted",
)
REBASE_APPROVAL_ONLY_REASONS = (
    "no_project",
    "recent_edit",
    "needs_approval",
    "auto_approved",
)
REBASE_MESSAGE_MAX_CHARS = 500
DEFAULT_UVREMAP_ROOT_DIR = "C:/SPMayaLiveSync/uvremap"
REBASE_BACKUP_DIRNAME = "rebase_backup"
REBASE_RELOAD_EXPECTED = {
    "preserve_strokes": True,
    "import_cameras": False,
    "auto_unwrap": False,
}
REBASE_REQUEST_ID_RE = re.compile(r"^rb-\d{8}-\d{6}-[0-9a-f]{4}$")
REBASE_BASELINE_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,79}$")
# 「save_as_copy が未保存の変更を含む」ことの確認結果(RB-0。強い傍証あり・目視確認待ち)。
# 確認できるまで False。True に変えるのは出荷前に指揮役が決める。PB-a / PB-b では変えない。
BACKUP_INCLUDES_UNSAVED = False
# 2026.10.19(1.4.0 PB-a): 依頼の任意キー uv_old(Maya の RB-a2 と同じ規則)
REBASE_UV_OLD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REBASE_UV_OLD_NAME_MAX = 64
REBASE_UV_OLD_INDEX_MIN = 1
REBASE_UV_OLD_INDEX_MAX = 7
_REBASE_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_REBASE_STATE_SET = frozenset(REBASE_STATES)
_REBASE_TERMINAL = frozenset(REBASE_TERMINAL_STATES)
_REBASE_REASON_SET = frozenset(REBASE_REASONS)
_REBASE_REQUEST_KEYS = (
    "schema",
    "request_id",
    "project_key",
    "created_at",
    "expires_at",
    "mesh",
    "baseline_id",
    "reload",
    "final_full",
    "ts_names",
)
_REBASE_WIN_BAD_FILENAME = '<>:"/\\|?*'
# 契約に無い。仮: rebase_wait_max_s の範囲(QUESTIONS_pb_a.md)。
_REBASE_WAIT_MAX_MIN_S = 60.0
_REBASE_WAIT_MAX_MAX_S = 86400.0
_REBASE_TRANSITIONS = {
    None: frozenset(("received", "rejected")),
    "received": frozenset((
        "received", "rejected", "awaiting_approval", "preparing",
        "declined", "expired", "superseded", "failed",
    )),
    "awaiting_approval": frozenset((
        "awaiting_approval", "preparing", "declined", "expired",
        "superseded", "failed",
    )),
    "preparing": frozenset((
        "preparing", "backing_up", "declined", "superseded", "failed",
    )),
    "backing_up": frozenset(("backing_up", "reloading", "failed")),
    "reloading": frozenset(("reloading", "exporting", "failed")),
    "exporting": frozenset(("exporting", "done", "failed")),
}
_PAINTER_STATE_DEFAULTS = {
    "project_open": False,
    "mesh_exists": False,
    "busy": True,
    "exporting": True,
    "can_disable_auto_unwrap": False,
    "backup_includes_unsaved": False,
    "needs_saving": False,
    "superseded": False,
    "cancelled": False,
    "mesh_sha1_ok": None,
    "last_edit_age_s": None,
    "saved_path": None,
    "project_key": None,
}
_PAINTER_STATE_KEYS = (
    "now",
    "project_open",
    "project_key",
    "saved_path",
    "busy",
    "exporting",
    "needs_saving",
    "backup_includes_unsaved",
    "mesh_exists",
    "mesh_sha1_ok",
    "superseded",
    "cancelled",
    "can_disable_auto_unwrap",
    "last_edit_age_s",
)
_REBASE_UV_FILLS_KEYS = ("enabled", "switched", "skipped", "failed", "rolled_back")

ApprovalDecision = namedtuple("ApprovalDecision", ["action", "reason"])
RebaseScan = namedtuple("RebaseScan", ["requests", "cancels"])
RebaseScanDiff = namedtuple("RebaseScanDiff", ["changed_ids", "new_cancel_ids"])

REBASE_INTERRUPTED_MESSAGE = (
    "Painter が終了または中断されたため、依頼は完了しませんでした"
)
REBASE_REASON_MESSAGES = {
    "disabled": "リベースは無効です",
    "bad_request": "依頼の内容が不正です",
    "bad_settings": "読み直しの設定が不正です",
    "project_mismatch": "開いているプロジェクトが依頼と一致しません",
    "unsaved_project": "未保存のプロジェクトではリベースできません",
    "mesh_missing": "依頼のメッシュファイルが見つかりません",
    "mesh_hash_mismatch": "メッシュファイルの内容が依頼と一致しません",
    "busy": "Painter が処理中です",
    "auto_unwrap_unverified": "自動ラップ解除を止められるか確認できません",
    "unsaved_changes": "未保存の変更があります",
    "cancelled": "依頼は取り消されました",
    "declined": "リベースを見送りました",
    "expired": "依頼の期限が切れました",
    "superseded": "新しい依頼に置き換えられました",
    "backup_failed": "バックアップに失敗しました",
    "reload_error": "メッシュの読み直しに失敗しました",
    "reload_timeout": "メッシュの読み直しが時間内に終わりませんでした",
    "project_locked": "プロジェクトがロックされています",
    "export_failed": (
        "Final の書き出しに失敗しました。"
        "メッシュは読み直し済み。Final の書き出しをやり直してください"
    ),
    "interrupted": REBASE_INTERRUPTED_MESSAGE,
}
_REBASE_BACKUP_MESSAGE_REASONS = frozenset((
    "reload_error",
    "reload_timeout",
    "project_locked",
    "backup_failed",
    "export_failed",
    "interrupted",
))

# 2026.10.19(1.4.0 PB-a): 確認ダイアログと案内の文言(R-8)。常時表示の注意文は省略できない。
REBASE_DIALOG_TITLE = "Live Sync — UV リベース"
REBASE_CONFIRM_LEAD = (
    "Maya から UV のリベースの依頼が届きました。"
    "Painter のメッシュを新しい UV で読み直します。"
    "バックアップを作ってから実行します。"
)
REBASE_CAUTION_TEXT = (
    "3D ビューで描いた内容は新しい UV に合わせて描き直されます。"
    "2D ビューで描いた内容、UV 投影のフィルやマスク、読み込んだ画像は崩れます。"
    "元に戻すにはバックアップ({backup_dir})を開いてください"
)
REBASE_CAUTION_TEXT_REPROJECT = (
    "3D ビューで描いた内容は新しい UV に合わせて描き直されます。"
    "UV 投影のフィル(マスクのフィルを含む)は、旧 UV セット経由に切り替えて"
    "見た目を保ちます(切り替えた数は完了後に表示します)。"
    "2D ビューで描いた内容は追従せず、崩れます。"
    "元に戻すにはバックアップ({backup_dir})を開いてください"
)
REBASE_UNSAVED_NOTE = "未保存の変更もバックアップに含まれます。"
REBASE_AUTO_UNWRAP_NOTE = (
    "プロジェクト設定の自動ラップ解除で、シーム・UVアイランド・パッキングが"
    "『見つからないデータのみを生成』になっていることを確認してください。"
    "『すべて再計算』の場合はUVが作り直されます(バックアップから戻せます)。"
)
REBASE_WORKFLOW_GUIDANCE = (
    "2D ビューで描いた内容を残したいときは、実行前にフィルやビットマップの"
    "レイヤーに変換しておくか、今後は 3D ビューで描くようにしてください。"
)
REBASE_DONE_GUIDANCE = (
    "リベースが完了しました。ストロークは新しい UV に再投影されています。"
    "次を Painter で確認してください: 2D ビューのストローク、UV 投影のフィル、"
    "ベイク済みのメッシュマップ(再ベイクが必要な場合があります)。"
)
REBASE_BUTTON_RUN = "実行する"
REBASE_BUTTON_CANCEL = "やめる"


# --- 3.1 定数とパス ---
def _rebase_is_finite_number(value):
    """bool ではない有限の int / float か。True は int のサブクラスなので先に除く。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _rebase_filename_only(value):
    """区切りを含むときは最後の要素だけ。ファイル名にならなければ None。"""
    if not isinstance(value, str) or value == "":
        return None
    normalized = value.replace("\\", "/").rstrip("/")
    if normalized == "":
        return None
    base = normalized.rsplit("/", 1)[-1]
    if base in ("", ".", ".."):
        return None
    return base


def _rebase_clip_message(value):
    if not isinstance(value, str):
        return ""
    if len(value) > REBASE_MESSAGE_MAX_CHARS:
        return value[:REBASE_MESSAGE_MAX_CHARS]
    return value


def _rebase_optional_text(value):
    if isinstance(value, str):
        return value
    return None


def _rebase_copy_reload():
    return {
        "preserve_strokes": REBASE_RELOAD_EXPECTED["preserve_strokes"],
        "import_cameras": REBASE_RELOAD_EXPECTED["import_cameras"],
        "auto_unwrap": REBASE_RELOAD_EXPECTED["auto_unwrap"],
    }


def normalize_project_key_for_storage(key):
    """空なら入力をそのまま返す。区切りを / にし NFC。大文字小文字は保つ。"""
    if not key:
        return key
    if not isinstance(key, str):
        return key
    return unicodedata.normalize("NFC", key.replace("\\", "/"))


def normalize_project_key_for_compare(key):
    """保存形を casefold()。空はそのまま。"""
    stored = normalize_project_key_for_storage(key)
    if not stored:
        return stored
    if not isinstance(stored, str):
        return stored
    return stored.casefold()


def project_keys_match(a, b):
    """両方が空でない文字列で、compare 形が等しいときだけ True。None どうしは False。"""
    if not isinstance(a, str) or not isinstance(b, str) or a == "" or b == "":
        return False
    return normalize_project_key_for_compare(a) == normalize_project_key_for_compare(b)


def is_valid_rebase_request_id(value):
    return isinstance(value, str) and REBASE_REQUEST_ID_RE.fullmatch(value) is not None


def uvremap_root_for_project(root_dir, final_subfolder):
    """作業フォルダ <root_dir>/<final_subfolder>。欠けていれば None。"""
    if not root_dir or not final_subfolder:
        return None
    return os.path.normpath(os.path.join(str(root_dir), str(final_subfolder)))


def resolve_uvremap_root(cfg, final_subfolder):
    """cfg の uvremap_root_dir、無ければ DEFAULT。dict でなくても例外を出さない。

    DEFAULT_CONFIG に uvremap_root_dir は足さない(Maya が持つキー。Painter は読むだけ)。
    """
    root_dir = DEFAULT_UVREMAP_ROOT_DIR
    if isinstance(cfg, dict):
        value = cfg.get("uvremap_root_dir")
        if isinstance(value, str) and value:
            root_dir = value
    return uvremap_root_for_project(root_dir, final_subfolder)


def _checked_rebase_request_id(request_id):
    if not is_valid_rebase_request_id(request_id):
        raise ValueError("request_id が不正です")
    return request_id


def rebase_requests_dir(root):
    return os.path.join(str(root), REBASE_DIRNAME, REBASE_REQUESTS_DIRNAME)


def rebase_status_dir(root):
    return os.path.join(str(root), REBASE_DIRNAME, REBASE_STATUS_DIRNAME)


def rebase_request_path(root, rid):
    rid = _checked_rebase_request_id(rid)
    return os.path.join(rebase_requests_dir(root), rid + ".json")


def rebase_cancel_path(root, rid):
    rid = _checked_rebase_request_id(rid)
    return os.path.join(rebase_requests_dir(root), rid + ".cancel")


def rebase_status_path(root, rid):
    rid = _checked_rebase_request_id(rid)
    return os.path.join(rebase_status_dir(root), rid + ".json")


# --- 3.2 設定の丸め(R-6) ---
def _rebase_coerce_float(value, default, min_value, max_value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default)
    number = float(value)
    if not math.isfinite(number):
        return float(default)
    if number < min_value:
        return float(min_value)
    if number > max_value:
        return float(max_value)
    return number


def _rebase_coerce_retry_max(value):
    default = 5
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return default
        value = int(value)
    if value < 0:
        return 0
    if value > 10:
        return 10
    return int(value)


def _rebase_coerce_bool(value, default):
    if isinstance(value, bool):
        return value
    return default


def normalize_rebase_settings(cfg):
    """Painter のリベース設定を丸める。dict でなくても例外を出さない。"""
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "rebase_enabled": _rebase_coerce_bool(cfg.get("rebase_enabled", True), True),
        "rebase_approval": (
            "auto" if cfg.get("rebase_approval") == "auto" else "ask"
        ),
        "rebase_auto_idle_s": _rebase_coerce_float(
            cfg.get("rebase_auto_idle_s", 10.0), 10.0, 1.0, 600.0),
        "rebase_reload_timeout_s": _rebase_coerce_float(
            cfg.get("rebase_reload_timeout_s", 600.0), 600.0, 30.0, 3600.0),
        "rebase_retry_max": _rebase_coerce_retry_max(cfg.get("rebase_retry_max", 5)),
        # 契約に無い。仮: 60〜86400 に丸める(QUESTIONS_pb_a.md)。
        "rebase_wait_max_s": _rebase_coerce_float(
            cfg.get("rebase_wait_max_s", 1800.0),
            1800.0,
            _REBASE_WAIT_MAX_MIN_S,
            _REBASE_WAIT_MAX_MAX_S,
        ),
        "rebase_reproject_uv_fills": _rebase_coerce_bool(
            cfg.get("rebase_reproject_uv_fills", True), True),
    }


def effective_rebase_approval(settings):
    if isinstance(settings, dict) and settings.get("rebase_approval") == "auto":
        return "auto"
    return "ask"


def needs_saving_for_rebase(snap, settings):
    """needs_saving が読めないとき、ask は偽、auto だけ真(待つ)。

    読めた値は従来どおり bool にする。設定が auto でも effective が ask なら偽。
    """
    if not isinstance(snap, dict):
        snap = {}
    if snap.get("needs_saving_missing"):
        return effective_rebase_approval(settings) == "auto"
    value = snap.get("needs_saving")
    if value is None:
        return False
    return bool(value)


# --- 3.3 依頼の読み取り(R-2.4) ---
def read_json_safe(path):
    """壊れた JSON・空・BOM 付き・UTF-8 でないバイト列は None。例外は出さない。

    BOM は外さない(BOM 付きは読めないものとして None)。Maya の _read_json_safe と同じ。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        return json.loads(text)
    except (OSError, ValueError, UnicodeError, TypeError):
        return None


def _parse_rebase_mesh(mesh):
    if not isinstance(mesh, dict):
        return None
    path = mesh.get("path")
    size = mesh.get("size")
    sha1 = mesh.get("sha1")
    if not isinstance(path, str) or path == "":
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    if not isinstance(sha1, str) or _REBASE_SHA1_RE.fullmatch(sha1.lower()) is None:
        return None
    return {"path": path, "size": size, "sha1": sha1.lower()}


def _parse_rebase_ts_names(value):
    if not isinstance(value, list) or len(value) == 0:
        return None
    names = []
    for item in value:
        if not isinstance(item, str) or item == "":
            return None
        names.append(item)
    return names


def _rebase_reload_problem(value):
    """一致なら None。dict でなければ bad_request。値の違いなら bad_settings。"""
    if not isinstance(value, dict):
        return "bad_request"
    for key, want in REBASE_RELOAD_EXPECTED.items():
        if key not in value or value[key] is not want:
            return "bad_settings"
    return None


def _parse_rebase_uv_old(value):
    """旧 UV セット記述を検査する。例外は出さない。

    None は有効で値は None。dict で name / index が規則どおりならその 2 キーだけ残す。
    """
    if value is None:
        return True, None
    if not isinstance(value, dict):
        return False, None
    if "name" not in value or "index" not in value:
        return False, None
    name = value.get("name")
    index = value.get("index")
    if not isinstance(name, str) or name == "":
        return False, None
    if len(name) > REBASE_UV_OLD_NAME_MAX:
        return False, None
    if REBASE_UV_OLD_NAME_RE.fullmatch(name) is None:
        return False, None
    if isinstance(index, bool) or not isinstance(index, int):
        return False, None
    if index < REBASE_UV_OLD_INDEX_MIN or index > REBASE_UV_OLD_INDEX_MAX:
        return False, None
    return True, {"name": name, "index": index}


def parse_rebase_request(raw):
    """正規化した依頼か (None, 理由コード)。Maya の parse_request と同じ戻り。

    不明なキーは落とす。maya_version / tool_version は文字列のときだけ残す。
    uv_old の不正は reload の値違い(bad_settings)より先。
    """
    if not isinstance(raw, dict):
        return None, "bad_request"
    for key in _REBASE_REQUEST_KEYS:
        if key not in raw:
            return None, "bad_request"
    schema = raw.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != REBASE_SCHEMA:
        return None, "bad_request"
    request_id = raw.get("request_id")
    if not is_valid_rebase_request_id(request_id):
        return None, "bad_request"
    project_key = raw.get("project_key")
    if not isinstance(project_key, str) or project_key == "":
        return None, "bad_request"
    created_at = raw.get("created_at")
    expires_at = raw.get("expires_at")
    if not _rebase_is_finite_number(created_at) or not _rebase_is_finite_number(expires_at):
        return None, "bad_request"
    if not expires_at > created_at:
        return None, "bad_request"
    mesh = _parse_rebase_mesh(raw.get("mesh"))
    if mesh is None:
        return None, "bad_request"
    baseline_id = raw.get("baseline_id")
    if not isinstance(baseline_id, str) or REBASE_BASELINE_ID_RE.fullmatch(baseline_id) is None:
        return None, "bad_request"
    reload_problem = _rebase_reload_problem(raw.get("reload"))
    if reload_problem == "bad_request":
        return None, "bad_request"
    final_full = raw.get("final_full")
    if not isinstance(final_full, bool):
        return None, "bad_request"
    ts_names = _parse_rebase_ts_names(raw.get("ts_names"))
    if ts_names is None:
        return None, "bad_request"
    # 2026.10.19(1.4.0 PB-a): uv_old は任意。不正は bad_request。
    # reload の値違い(bad_settings)より先に見る。無いのは不正ではない。
    has_uv_old = "uv_old" in raw
    uv_old = None
    if has_uv_old:
        uv_ok, uv_old = _parse_rebase_uv_old(raw.get("uv_old"))
        if not uv_ok:
            return None, "bad_request"
    if reload_problem is not None:
        return None, reload_problem
    out = {
        "schema": REBASE_SCHEMA,
        "request_id": request_id,
        "project_key": project_key,
        "created_at": created_at,
        "expires_at": expires_at,
        "mesh": mesh,
        "baseline_id": baseline_id,
        "reload": _rebase_copy_reload(),
        "final_full": final_full,
        "ts_names": ts_names,
    }
    if has_uv_old:
        out["uv_old"] = uv_old
    for key in ("maya_version", "tool_version"):
        if key in raw and isinstance(raw[key], str):
            out[key] = raw[key]
    return out, None


def read_rebase_request_with_reason(root, rid):
    """読めない・ID 不一致は (None, bad_request)。parse の理由も返す。"""
    if not is_valid_rebase_request_id(rid):
        return None, "bad_request"
    try:
        path = rebase_request_path(root, rid)
    except ValueError:
        return None, "bad_request"
    raw = read_json_safe(path)
    if raw is None:
        return None, "bad_request"
    parsed, reason = parse_rebase_request(raw)
    if parsed is None:
        return None, reason or "bad_request"
    if parsed.get("request_id") != rid:
        return None, "bad_request"
    return parsed, None


def read_rebase_request(root, rid):
    parsed, _reason = read_rebase_request_with_reason(root, rid)
    return parsed


def scan_rebase_dir(root, scandir_fn=os.scandir):
    """requests/ を 1 回見て、正しい形の .json と .cancel だけ拾う。内容は読まない。"""
    requests = {}
    cancels = {}
    directory = rebase_requests_dir(root)
    try:
        entries = scandir_fn(directory)
    except OSError:
        return RebaseScan(requests, cancels)
    closer = getattr(entries, "close", None)
    try:
        for entry in entries:
            try:
                name = getattr(entry, "name", None)
                if not isinstance(name, str):
                    continue
                is_file = getattr(entry, "is_file", None)
                if callable(is_file):
                    if not entry.is_file():
                        continue
                if name.endswith(".tmp"):
                    continue
                st = entry.stat()
                if name.endswith(".json"):
                    rid = name[:-5]
                    if not is_valid_rebase_request_id(rid):
                        continue
                    requests[rid] = (int(st.st_mtime_ns), int(st.st_size))
                elif name.endswith(".cancel"):
                    rid = name[:-7]
                    if not is_valid_rebase_request_id(rid):
                        continue
                    cancels[rid] = int(st.st_mtime_ns)
            except OSError:
                continue
    except OSError:
        pass
    finally:
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
    return RebaseScan(requests, cancels)


def diff_rebase_scan(prev, cur):
    """新規または (mtime_ns, size) が変わった依頼と、新規の .cancel。ID は昇順。"""
    if prev is None:
        prev_requests = {}
        prev_cancels = {}
    else:
        prev_requests = getattr(prev, "requests", None) or {}
        prev_cancels = getattr(prev, "cancels", None) or {}
    if cur is None:
        cur_requests = {}
        cur_cancels = {}
    else:
        cur_requests = getattr(cur, "requests", None) or {}
        cur_cancels = getattr(cur, "cancels", None) or {}
    changed = []
    for rid, meta in cur_requests.items():
        if rid not in prev_requests or prev_requests[rid] != meta:
            changed.append(rid)
    changed.sort()
    new_cancels = sorted(rid for rid in cur_cancels if rid not in prev_cancels)
    return RebaseScanDiff(changed, new_cancels)


def find_superseded_ids(requests):
    """同じプロジェクトで、より新しい依頼がある依頼の ID。古い方が superseded。"""
    groups = {}
    if not isinstance(requests, list):
        return set()
    for rec in requests:
        if not isinstance(rec, dict):
            continue
        key = rec.get("project_key")
        compare = normalize_project_key_for_compare(key)
        if not isinstance(compare, str) or compare == "":
            continue
        groups.setdefault(compare, []).append(rec)
    superseded = set()
    for items in groups.values():
        ranked = sorted(
            items,
            key=lambda rec: (rec.get("created_at"), rec.get("request_id")),
        )
        for rec in ranked[:-1]:
            rid = rec.get("request_id")
            if isinstance(rid, str):
                superseded.add(rid)
    return superseded


# --- 3.4 状態の読み書き(R-2.5、R-3) ---
def _normalize_uv_fills(value):
    enabled = False
    switched = 0
    skipped = 0
    failed = 0
    rolled_back = 0
    if isinstance(value, dict):
        raw_enabled = value.get("enabled", False)
        enabled = raw_enabled if isinstance(raw_enabled, bool) else False

        def _as_int(raw):
            if isinstance(raw, bool) or not isinstance(raw, int):
                return 0
            return raw

        switched = _as_int(value.get("switched", 0))
        skipped = _as_int(value.get("skipped", 0))
        failed = _as_int(value.get("failed", 0))
        rolled_back = _as_int(value.get("rolled_back", 0))
    return {
        "enabled": enabled,
        "switched": switched,
        "skipped": skipped,
        "failed": failed,
        "rolled_back": rolled_back,
    }


def build_rebase_status(
    request_id,
    state,
    now,
    plugin_version,
    *,
    reason=None,
    message="",
    backup_name=None,
    result_export_id=None,
    uv_fills=None,
):
    if not is_valid_rebase_request_id(request_id):
        raise ValueError("request_id が不正です")
    # 2026.10.19(1.4.0 PB-b 0.5): state / reason が str でないときは
    # TypeError ではなく ValueError(frozenset が unhashable を弾くため)。
    if not isinstance(state, str) or state not in _REBASE_STATE_SET:
        raise ValueError("state が不正です")
    if reason is not None and (
            not isinstance(reason, str) or reason not in _REBASE_REASON_SET):
        raise ValueError("reason が不正です")
    if not _rebase_is_finite_number(now):
        raise ValueError("updated_at が不正です")
    out = {
        "schema": REBASE_SCHEMA,
        "request_id": request_id,
        "state": state,
        "updated_at": now,
        "plugin_version": plugin_version,
        "reason": reason,
        "message": _rebase_clip_message(message),
        "backup_name": _rebase_filename_only(backup_name),
        "result_export_id": result_export_id if isinstance(result_export_id, str) else None,
    }
    # uv_fills は PB-b の追加キー。None のときはキー自体を出さない。
    # Maya の parse_status は未知のキーを落とすので互換。契約 R-2.5 への追記は QUESTIONS。
    if uv_fills is not None:
        if isinstance(uv_fills, dict):
            out["uv_fills"] = _normalize_uv_fills(uv_fills)
        else:
            out["uv_fills"] = _normalize_uv_fills({})
    return out


def parse_rebase_status(raw):
    """Maya の parse_status と同じ検査・同じ正規化。uv_fills は落としてよい。"""
    if not isinstance(raw, dict):
        return None
    schema = raw.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != REBASE_SCHEMA:
        return None
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or request_id == "":
        return None
    state = raw.get("state")
    # 2026.10.19(1.4.0 PB-b 0.5): list / dict など unhashable な state で
    # TypeError を出さない(1 秒タイマーから read → write する経路)。
    if not isinstance(state, str) or state not in _REBASE_STATE_SET:
        return None
    updated_at = raw.get("updated_at")
    if not _rebase_is_finite_number(updated_at):
        return None
    return {
        "schema": REBASE_SCHEMA,
        "request_id": request_id,
        "state": state,
        "updated_at": updated_at,
        "plugin_version": _rebase_optional_text(raw.get("plugin_version")),
        "reason": _rebase_optional_text(raw.get("reason")),
        "message": _rebase_clip_message(raw.get("message")),
        "backup_name": _rebase_filename_only(raw.get("backup_name")),
        "result_export_id": _rebase_optional_text(raw.get("result_export_id")),
    }


def read_rebase_status(root, rid):
    if not is_valid_rebase_request_id(rid):
        return None
    try:
        path = rebase_status_path(root, rid)
    except ValueError:
        return None
    raw = read_json_safe(path)
    if raw is None:
        return None
    parsed = parse_rebase_status(raw)
    if parsed is None or parsed.get("request_id") != rid:
        return None
    return parsed


def is_valid_rebase_transition(prev_state, new_state):
    # 2026.10.19(1.4.0 PB-b 0.5): unhashable な prev_state / new_state で
    # TypeError を出さない。
    if prev_state is not None and not isinstance(prev_state, str):
        return False
    if not isinstance(new_state, str):
        return False
    allowed = _REBASE_TRANSITIONS.get(prev_state)
    if allowed is None:
        return False
    return new_state in allowed


def write_rebase_status(root, status):
    """遷移が不正なら書かずに False。壊れた既存ファイルは復旧のため書いてよい。"""
    parsed = parse_rebase_status(status)
    if parsed is None:
        _log("error", "rebase: 状態の内容が不正なため書きません")
        return False
    rid = parsed["request_id"]
    if not is_valid_rebase_request_id(rid):
        _log("error", "rebase: request_id が不正なため状態を書きません")
        return False
    try:
        path = rebase_status_path(root, rid)
    except ValueError:
        _log("error", "rebase: 状態パスを作れません")
        return False
    existing = read_rebase_status(root, rid)
    if existing is None:
        try:
            exists = os.path.isfile(path)
        except OSError:
            exists = False
        if exists:
            # 既存が壊れていて読めないときは書いてよい(復旧)。
            pass
        elif not is_valid_rebase_transition(None, parsed["state"]):
            _log(
                "warning",
                "rebase: 不正な遷移 None -> {0}".format(parsed["state"]),
            )
            return False
    elif not is_valid_rebase_transition(existing.get("state"), parsed["state"]):
        _log(
            "warning",
            "rebase: 不正な遷移 {0} -> {1}".format(
                existing.get("state"), parsed["state"]),
        )
        return False
    payload = dict(parsed)
    if isinstance(status, dict) and "uv_fills" in status and status.get("uv_fills") is not None:
        payload["uv_fills"] = _normalize_uv_fills(status.get("uv_fills"))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json_atomic(path, payload)
        return True
    except Exception as exc:
        _log("error", "rebase: 状態の書き込みに失敗しました: {0}".format(exc))
        return False


def build_interrupted_status(prev_status, now, plugin_version):
    """非終端なら failed / interrupted。終端なら None。起動時の中断の土台。"""
    if not isinstance(prev_status, dict):
        return None
    state = prev_status.get("state")
    # 2026.10.19(1.4.0 PB-b 0.5): unhashable な state で例外を出さない。
    if not isinstance(state, str) or state not in _REBASE_STATE_SET or state in _REBASE_TERMINAL:
        return None
    request_id = prev_status.get("request_id")
    return build_rebase_status(
        request_id,
        "failed",
        now,
        plugin_version,
        reason="interrupted",
        message=REBASE_INTERRUPTED_MESSAGE,
        backup_name=prev_status.get("backup_name"),
        result_export_id=prev_status.get("result_export_id"),
    )


def rebase_failure_message(reason, *, backup_name=None, detail=None):
    # 2026.10.19(1.4.0 PB-b 0.5): reason が str でないときは既定文。
    if not isinstance(reason, str):
        text = ""
    else:
        text = REBASE_REASON_MESSAGES.get(reason, "")
    if not text:
        text = "リベースに失敗しました"
    name = _rebase_filename_only(backup_name)
    if isinstance(reason, str) and reason in _REBASE_BACKUP_MESSAGE_REASONS and name:
        text = "{0} バックアップ: {1}".format(text, name)
    if isinstance(detail, str) and detail:
        text = "{0} {1}".format(text, detail)
    return _rebase_clip_message(text)


# --- 3.5 承認判定 decide_approval(R-5.2) ---
def _painter_state_get(painter_state, key):
    if not isinstance(painter_state, dict) or key not in painter_state:
        return _PAINTER_STATE_DEFAULTS[key]
    return painter_state[key]


def decide_approval(request, painter_state, settings):
    """R-5.2 の 15 行。上が優先。action は ask / run / wait / reject。"""
    if not isinstance(request, dict) or not _rebase_is_finite_number(request.get("expires_at")):
        return ApprovalDecision("reject", "bad_request")
    if not isinstance(painter_state, dict) or not _rebase_is_finite_number(painter_state.get("now")):
        return ApprovalDecision("reject", "bad_request")
    rounded = normalize_rebase_settings(settings)
    now = painter_state.get("now")
    expires_at = request.get("expires_at")
    if rounded.get("rebase_enabled") is False:
        return ApprovalDecision("reject", "disabled")
    if bool(_painter_state_get(painter_state, "cancelled")):
        return ApprovalDecision("reject", "cancelled")
    if bool(_painter_state_get(painter_state, "superseded")):
        return ApprovalDecision("reject", "superseded")
    if now > expires_at:
        return ApprovalDecision("reject", "expired")
    if not bool(_painter_state_get(painter_state, "project_open")):
        return ApprovalDecision("wait", "no_project")
    if not project_keys_match(
            request.get("project_key"),
            _painter_state_get(painter_state, "project_key")):
        return ApprovalDecision("wait", "project_mismatch")
    saved_path = _painter_state_get(painter_state, "saved_path")
    if saved_path is None or saved_path == "":
        return ApprovalDecision("reject", "unsaved_project")
    if not bool(_painter_state_get(painter_state, "mesh_exists")):
        return ApprovalDecision("reject", "mesh_missing")
    mesh_sha1_ok = _painter_state_get(painter_state, "mesh_sha1_ok")
    if mesh_sha1_ok is False:
        return ApprovalDecision("reject", "mesh_hash_mismatch")
    if bool(_painter_state_get(painter_state, "busy")) or bool(
            _painter_state_get(painter_state, "exporting")):
        return ApprovalDecision("wait", "busy")
    approval = effective_rebase_approval(rounded)
    can_disable = bool(_painter_state_get(painter_state, "can_disable_auto_unwrap"))
    if approval == "auto" and not can_disable:
        return ApprovalDecision("ask", "auto_unwrap_unverified")
    idle_s = rounded.get("rebase_auto_idle_s", 10.0)
    last_edit_age_s = _painter_state_get(painter_state, "last_edit_age_s")
    if approval == "auto":
        if last_edit_age_s is None or not _rebase_is_finite_number(last_edit_age_s):
            return ApprovalDecision("wait", "recent_edit")
        if last_edit_age_s < idle_s:
            return ApprovalDecision("wait", "recent_edit")
    needs_saving = bool(_painter_state_get(painter_state, "needs_saving"))
    backup_includes = bool(_painter_state_get(painter_state, "backup_includes_unsaved"))
    if needs_saving and (approval == "auto" or not backup_includes):
        return ApprovalDecision("wait", "unsaved_changes")
    if approval == "auto":
        return ApprovalDecision("run", "auto_approved")
    return ApprovalDecision("ask", "needs_approval")


def wait_timed_out(first_wait_at, now, settings):
    """True になった依頼は PB-b が expired(理由 expired)にする。契約に無い。仮。"""
    if first_wait_at is None:
        return False
    if not _rebase_is_finite_number(first_wait_at) or not _rebase_is_finite_number(now):
        return False
    rounded = normalize_rebase_settings(settings)
    return (now - first_wait_at) > rounded.get("rebase_wait_max_s", 1800.0)


# --- 3.6 事前チェックの入力の組み立て(R-5.3) ---
def mesh_precheck(mesh, cache, *, stat_fn=os.stat, hash_fn=None):
    """(mesh_exists, mesh_sha1_ok, new_cache)。同じ size/mtime_ns では hash を再計算しない。"""
    if not isinstance(mesh, dict):
        return False, None, None
    path = mesh.get("path")
    if not isinstance(path, str) or path == "":
        return False, None, None
    try:
        st = stat_fn(path)
    except Exception:
        return False, None, None
    try:
        mode = st.st_mode
        if not stat.S_ISREG(mode):
            return False, None, None
        size = int(st.st_size)
        mtime_ns = int(st.st_mtime_ns)
    except (AttributeError, TypeError, ValueError):
        return False, None, None
    if (
        isinstance(cache, dict)
        and cache.get("path") == path
        and cache.get("size") == size
        and cache.get("mtime_ns") == mtime_ns
        and "sha1_ok" in cache
    ):
        return True, cache.get("sha1_ok"), cache
    want_size = mesh.get("size")
    if want_size != size:
        new_cache = {
            "path": path,
            "size": size,
            "mtime_ns": mtime_ns,
            "sha1_ok": False,
        }
        return True, False, new_cache
    if hash_fn is None:
        def hash_fn(hash_path):
            return _hash_file_hex(hash_path, "sha1")
    try:
        digest = hash_fn(path)
    except OSError:
        return False, None, None
    want_sha1 = mesh.get("sha1")
    if not isinstance(digest, str) or not isinstance(want_sha1, str):
        sha1_ok = False
    else:
        sha1_ok = digest.lower() == want_sha1.lower()
    new_cache = {
        "path": path,
        "size": size,
        "mtime_ns": mtime_ns,
        "sha1_ok": sha1_ok,
    }
    return True, sha1_ok, new_cache


# 2026.10.19(1.4.0 PB-b 0.5): mesh_precheck のキャッシュは依頼ごとに持つ。
# 同じファイルを指す別依頼(sha1 / size が違う)へ古い sha1_ok を渡さない。
# 関数 mesh_precheck 自体の挙動は変えない。
_REBASE_PRECHECK_CACHE_MAX = 32


class RebasePrecheckCaches(object):
    """request_id -> mesh_precheck の cache。終端・閉じる・shutdown で捨てる。"""

    def __init__(self, max_entries=_REBASE_PRECHECK_CACHE_MAX):
        self._by_id = {}
        self._max_entries = max(1, int(max_entries))

    def get(self, request_id):
        return self._by_id.get(request_id)

    def put(self, request_id, cache):
        if not isinstance(request_id, str) or request_id == "":
            return
        if request_id in self._by_id:
            self._by_id.pop(request_id, None)
        self._by_id[request_id] = cache
        while len(self._by_id) > self._max_entries:
            oldest = next(iter(self._by_id))
            self._by_id.pop(oldest, None)

    def drop(self, request_id):
        self._by_id.pop(request_id, None)

    def drop_if_terminal(self, request_id, state):
        if isinstance(state, str) and state in _REBASE_TERMINAL:
            self.drop(request_id)

    def clear(self):
        self._by_id.clear()

    def __contains__(self, request_id):
        return request_id in self._by_id

    def __len__(self):
        return len(self._by_id)


def supports_auto_unwrap_settings(settings_cls):
    """MeshReloadingSettings が auto_unwrap_settings を持てるか。判定できなければ False。"""
    if settings_cls is None:
        return False
    try:
        signature = inspect.signature(settings_cls)
        if "auto_unwrap_settings" in signature.parameters:
            return True
    except Exception:
        pass
    try:
        if hasattr(settings_cls, "auto_unwrap_settings"):
            return True
    except Exception:
        return False
    return False


def compute_last_edit_age(now, last_edit_at):
    if last_edit_at is None or not _rebase_is_finite_number(last_edit_at):
        return None
    if not _rebase_is_finite_number(now):
        return None
    return max(0.0, float(now) - float(last_edit_at))


def build_painter_state(
    *,
    now,
    is_open,
    in_edition,
    project_key,
    saved_path,
    busy,
    exporting,
    needs_saving,
    backup_includes_unsaved,
    mesh_exists,
    mesh_sha1_ok,
    superseded,
    cancelled,
    can_disable_auto_unwrap,
    last_edit_at,
):
    """decide_approval の入力 14 キー。project_open = is_open and in_edition。"""
    if mesh_sha1_ok is True or mesh_sha1_ok is False or mesh_sha1_ok is None:
        sha1_ok = mesh_sha1_ok
    else:
        sha1_ok = None
    return {
        "now": now,
        "project_open": bool(is_open) and bool(in_edition),
        "project_key": project_key,
        "saved_path": saved_path,
        "busy": bool(busy),
        "exporting": bool(exporting),
        "needs_saving": bool(needs_saving),
        "backup_includes_unsaved": bool(backup_includes_unsaved),
        "mesh_exists": bool(mesh_exists),
        "mesh_sha1_ok": sha1_ok,
        "superseded": bool(superseded),
        "cancelled": bool(cancelled),
        "can_disable_auto_unwrap": bool(can_disable_auto_unwrap),
        "last_edit_age_s": compute_last_edit_age(now, last_edit_at),
    }


# --- 3.7 バックアップ・再試行・読み直し ---
def _rebase_safe_stem(name):
    cleaned = []
    for char in name:
        if char in _REBASE_WIN_BAD_FILENAME:
            cleaned.append("_")
        else:
            cleaned.append(char)
    stem = "".join(cleaned)
    return stem or "project"


def rebase_backup_path(spp_path, request_id, exists_fn=os.path.exists):
    """(フルパス, ファイル名, バックアップのフォルダ)。戻りは / 区切り。"""
    if not isinstance(spp_path, str) or spp_path == "":
        raise ValueError("spp_path が不正です")
    if not is_valid_rebase_request_id(request_id):
        raise ValueError("request_id が不正です")
    normalized = spp_path.replace("\\", "/")
    if "/" in normalized:
        parent, filename = normalized.rsplit("/", 1)
    else:
        parent = ""
        filename = normalized
    if filename.lower().endswith(".spp"):
        stem = filename[:-4]
    else:
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = _rebase_safe_stem(stem)
    if parent:
        folder = parent + "/" + REBASE_BACKUP_DIRNAME
    else:
        folder = REBASE_BACKUP_DIRNAME
    name = "{0}_before_rebase_{1}.spp".format(stem, request_id)
    full = folder + "/" + name
    suffix = 2
    while exists_fn(full):
        name = "{0}_before_rebase_{1}_{2}.spp".format(stem, request_id, suffix)
        full = folder + "/" + name
        suffix += 1
        if suffix > 10000:
            raise ValueError("バックアップ名を作れません")
    return full, name, folder


def rebase_retry_delay(attempt, retry_max):
    """これから行う再試行の番号(1 始まり)。上限超過または retry_max=0 なら None。"""
    rounded_max = _rebase_coerce_retry_max(retry_max)
    if not isinstance(attempt, bool) and isinstance(attempt, (int, float)):
        if isinstance(attempt, float) and (not math.isfinite(attempt) or not attempt.is_integer()):
            return None
        attempt = int(attempt)
    else:
        return None
    if attempt > rounded_max:
        return None
    if attempt < 1:
        return None
    return min(0.5 * (2 ** (attempt - 1)), 8.0)


def classify_reload_status(status):
    """ReloadMeshStatus の name を success / error / other に分ける。"""
    if status is None:
        return "other"
    name = getattr(status, "name", None)
    token = None
    if isinstance(name, str) and name:
        token = name
    else:
        text = str(status)
        if "." in text:
            token = text.rsplit(".", 1)[-1]
        else:
            token = text
    token = token.lower()
    if token == "success":
        return "success"
    if token == "error":
        return "error"
    return "other"


def build_reload_settings_kwargs(request, supports_auto_unwrap):
    """mesh_settings は渡さない。auto_unwrap_settings は止められる版だけ None を足す。"""
    if not isinstance(request, dict):
        raise ValueError("bad_settings")
    reload_problem = _rebase_reload_problem(request.get("reload"))
    if reload_problem is not None:
        raise ValueError("bad_settings")
    out = {
        "import_cameras": False,
        "preserve_strokes": True,
    }
    if supports_auto_unwrap:
        out["auto_unwrap_settings"] = None
    return out


# --- 3.8 文言(R-8) ---
def build_rebase_approval_text(
    request,
    *,
    backup_dir,
    needs_saving=False,
    can_disable_auto_unwrap=True,
    backup_includes_unsaved=False,
    uv_fill_reproject=False,
):
    """確認ダイアログの本文。注意文と運用案内は常に入れる。uv_old は見ない。"""
    parts = [REBASE_CONFIRM_LEAD]
    ts_names = None
    if isinstance(request, dict):
        ts_names = request.get("ts_names")
    if isinstance(ts_names, list):
        names = [name for name in ts_names if isinstance(name, str) and name]
        if names:
            parts.append("対象: {0}".format(", ".join(names)))
    caution = REBASE_CAUTION_TEXT_REPROJECT if uv_fill_reproject else REBASE_CAUTION_TEXT
    parts.append(caution.format(backup_dir=backup_dir))
    parts.append(REBASE_WORKFLOW_GUIDANCE)
    if needs_saving and backup_includes_unsaved:
        parts.append(REBASE_UNSAVED_NOTE)
    if not can_disable_auto_unwrap:
        parts.append(REBASE_AUTO_UNWRAP_NOTE)
    return "\n".join(parts)


def _current_project_key():
    """現在のプロジェクトを一意に識別するキーを返す。
    未保存プロジェクトの場合は固定文字列を使う(この場合、複数の未保存
    プロジェクトを区別できない点は既知の制限とする)。

    2026.07.16(緊急修正): project.file_path() は公式ドキュメント上
    「プロジェクトが未保存ならNoneを返す」とされているが、実機では
    テンプレートから新規プロジェクトを作成した直後、一度も保存して
    いない状態で、Noneではなく作成元テンプレート(.spt)のパスを
    返すケースが確認された。この値をそのままプロジェクトキーとして
    使うと、Maya側の紐付けUIに「TEST_1」ではなく
    「PBR - Metallic Roughness Alpha-blend.spt」のようなテンプレート名が
    表示されてしまい、実際のプロジェクトと異なるキーで紐付けが行われる
    (ドキュメントに明記されていない、実装依存の挙動と考えられる)。

    対策として、公式に「未保存なら確実にNoneを返す」と明記されている
    project.name() を先に確認し、これがNoneであれば file_path() の値を
    信頼せず "__unsaved__" にフォールバックするようにした。

    2026.07.25(緊急バグ修正): 上記の project.name() ガードだけでは
    このテンプレート漏れを捕まえきれないケースが実機で確認された
    (project.name() が真になった状態でも file_path() がテンプレートの
    .sptパスを返し続けることがある)。file_path() の拡張子が .spt
    (大文字小文字を区別しない)の場合も "__unsaved__" として扱うガードを
    追加した。これにより、テンプレート由来の未保存プロジェクトの
    キーが watch_subfolder_by_project/known_texture_sets_by_project 等の
    *_by_project 辞書へ本来のプロジェクト名の代わりに紛れ込む不具合
    (実機のlive_sync_config.jsonで
    ".../PBR - Metallic Roughness Alpha-blend.spt" というキーの残留を
    確認)を防ぐ。この状態は既存の "__unsaved__" ガード(シーン紐付け・
    create_shader_network()の拒否)が正しく機能する前提でもある。
    """
    try:
        name = project.name()
    except Exception:
        name = None

    if not name:
        # project.name() がNoneを返す = 公式仕様上、確実に未保存。
        # file_path() が(ドキュメントの契約に反して)何らかのパスを
        # 返していても、それは信頼できないため無視する。
        return "__unsaved__"

    try:
        path = project.file_path()
    except Exception:
        path = None

    if path and path.lower().endswith(".spt"):
        # テンプレートファイル自体のパスが漏れてきたケース。実プロジェクト
        # として保存されるまでは "__unsaved__" 扱いにする。
        return "__unsaved__"

    return path or "__unsaved__"


def _project_subfolder_name():
    """現在のプロジェクト用の Final サブフォルダ名を返す。

    形式: <ファイル名(拡張子なし)>_<フルパスのハッシュ先頭6文字>
      例: C:/work/ProjectA/proA.spp -> "proA_a1b2c3"

    - ファイル名部分で人が見て判別でき、ハッシュ部分で「別の場所にある
      同名プロジェクト」同士の衝突を防ぐ(両立方式)。
    - 未保存プロジェクトはキーが "__unsaved__" 固定のため、フォルダ名も
      "__unsaved__" になる(未保存同士は区別できないという既存の制限を
      そのまま引き継ぐ)。この場合ファイル名部分は付けない。
    - ファイル名部分は、フォルダ名として使えない文字を除去して安全化する。
    """
    key = _current_project_key()
    if key == "__unsaved__":
        return "__unsaved__"

    stem = os.path.splitext(os.path.basename(key))[0]
    # フォルダ名に使えない文字を除去(Windows/一般で不正な文字を _ に)
    safe_stem = re.sub(r'[<>:"/\\|?*\s]', "_", stem).strip("_") or "project"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:6]
    return "{0}_{1}".format(safe_stem, digest)


def _duplicate_folder_warning(staging_dir, watch_dir, final_export_dir):
    """Phase 3 最適化: staging/watch/final の3フォルダが誤って同じ
    パスに設定されていないかを確認する。特にstaging_dirは書き出しごとに
    一時フォルダを作成・削除するため、watch_dirやfinal_export_dirと
    同じ場所に設定すると、Maya側の監視イベントが不必要に多発したり、
    最終出力ファイルが誤って削除されたりする恐れがある。
    問題があれば警告文を返し、無ければNoneを返す。
    """
    pairs = [
        ("ステージングフォルダ", os.path.normpath(staging_dir)),
        ("監視フォルダ", os.path.normpath(watch_dir)),
        ("最終出力フォルダ", os.path.normpath(final_export_dir)),
    ]
    seen = {}
    for label, path in pairs:
        if path in seen:
            return (
                "「{0}」と「{1}」に同じフォルダが指定されています。"
                "意図しない挙動(監視イベントの多発、ファイルの誤削除等)を"
                "避けるため、それぞれ別のフォルダを指定することを推奨します。"
                "続行しますか?".format(seen[path], label)
            )
        seen[path] = label
    return None


# ---------------------------------------------------------------------------
# 2026.10.19(1.4.0 PB-b): リベースの状態機械と UV フィル(Qt 非依存)
# ---------------------------------------------------------------------------
# --- 2026.10.19(1.4.0 PB-b): 拒否の状態、UV フィル、ジョブ、司令塔 ---
REBASE_EXPORT_DEFERRED_STATUS = (
    "リベース中のため、書き出しはリベースの終了後に行います"
)
REBASE_UV_FILL_PROGRESS = (
    "UV 投影のフィルを旧 UV セット経由に切り替えています"
)
REBASE_AUTO_UNWRAP_UNSUPPORTED_LOG = (
    "この版は自動展開の設定を渡せません。プロジェクトの自動ラップ解除の設定に従います"
)
REBASE_TERMINAL_WRITE_MAX = 10
_REBASE_CFG_RELOAD_S = 5.0
_UV_FILL_PARAM_ATTRS = ("filtering_mode", "uv_wrapping_mode", "uv_transformation")
_UV_FILL_GATE_MESSAGES = {
    "disabled_by_setting": "UV 投影のフィル切り替えは設定で無効です",
    "no_api": "UV 投影フィルの API が使えません",
    "uvold_absent": "依頼に旧 UV セットが無いため、UV 投影のフィルは切り替えていません",
    "no_matching_texture_set": "依頼のテクスチャセットに一致するものがありません",
}


def rebase_reject_state(reason, prev_state):
    """decide_approval の reject 理由と今の状態から、書く (state, reason) を決める。

    今の状態が None のときは理由によらず rejected(None からは received / rejected だけ)。
    """
    if not isinstance(reason, str) or reason == "":
        reason = "bad_request"
    if prev_state is None:
        return ("rejected", reason)
    if reason == "cancelled":
        return ("declined", "cancelled")
    if reason == "superseded":
        return ("superseded", "superseded")
    if reason == "expired":
        if prev_state in ("received", "awaiting_approval"):
            return ("expired", "expired")
        return ("failed", "expired")
    if prev_state == "received":
        return ("rejected", reason)
    if prev_state in ("awaiting_approval", "preparing"):
        return ("failed", reason)
    return ("failed", reason)


def get_uv_old_index(request):
    """依頼の uv_old.index。1〜7 の int だけ。無ければ None。例外は出さない。"""
    if not isinstance(request, dict):
        return None
    uv_old = request.get("uv_old")
    if not isinstance(uv_old, dict):
        return None
    index = uv_old.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if index < REBASE_UV_OLD_INDEX_MIN or index > REBASE_UV_OLD_INDEX_MAX:
        return None
    return index


def get_uv_old_name(request):
    """ログ用。照合には使わない(要実機確認: Painter は UV セット名を API で取れない)。"""
    if not isinstance(request, dict):
        return None
    uv_old = request.get("uv_old")
    if not isinstance(uv_old, dict):
        return None
    name = uv_old.get("name")
    if isinstance(name, str) and name:
        return name
    return None


UvFillApi = namedtuple(
    "UvFillApi",
    ["get_root_layer_nodes", "ProjectionMode", "UVSetToUVSetProjectionParams"],
)


def make_uv_fill_api(layerstack_module):
    """必須 3 点が揃わなければ None。未確認の名前は getattr で守る(要実機確認)。"""
    if layerstack_module is None:
        return None
    get_roots = getattr(layerstack_module, "get_root_layer_nodes", None)
    mode = getattr(layerstack_module, "ProjectionMode", None)
    params_cls = getattr(layerstack_module, "UVSetToUVSetProjectionParams", None)
    if not callable(get_roots) or mode is None or params_cls is None:
        return None
    if not hasattr(mode, "UV") or not hasattr(mode, "UVSetToUVSet"):
        return None
    return UvFillApi(get_roots, mode, params_cls)


def node_type_token(node):
    try:
        raw = node.get_type()
    except Exception:
        return ""
    name = getattr(raw, "name", None)
    if isinstance(name, str) and name:
        token = name
    else:
        token = str(raw)
    if "." in token:
        token = token.rsplit(".", 1)[-1]
    return token.lower()


def _safe_call(obj, name, *args, **kwargs):
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _iter_named_children(node, names):
    for name in names:
        got = _safe_call(node, name)
        if got is None:
            continue
        if isinstance(got, (list, tuple)):
            return list(got)
        try:
            return list(got)
        except TypeError:
            continue
    return None


def _node_has_projection_api(node):
    for name in (
        "get_projection_mode",
        "set_projection_mode",
        "get_projection_parameters",
        "set_projection_parameters",
    ):
        if not callable(getattr(node, name, None)):
            return False
    return True


def _uv_fill_visit_key(node):
    """訪問済み判定に使う安定した識別子。無ければ None。

    id() は使わない。呼ぶたびに新しいラッパーを返す API では、
    解放されたラッパーの id が次の未訪問ノードに再利用される。
    """
    uid_fn = getattr(node, "uid", None)
    if not callable(uid_fn):
        return None
    try:
        value = uid_fn()
    except Exception:
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        hash(value)
    except TypeError:
        return None
    return value


def iter_uv_fill_nodes(roots, report, *, max_depth=32, _seen=None, _depth=0):
    """深さ優先。1 ノードの失敗で全体を止めない。

    訪問済みは uid() があるノードだけ。無いノードは判定せず、深さで打ち切る。
    """
    if _seen is None:
        _seen = set()
    if roots is None:
        return
    try:
        nodes = list(roots)
    except TypeError:
        nodes = [roots]
    for node in nodes:
        key = _uv_fill_visit_key(node)
        if key is not None:
            if key in _seen:
                continue
            _seen.add(key)
        if _depth > max_depth:
            continue
        try:
            token = node_type_token(node)
            if "paint" in token and not _node_has_projection_api(node):
                report["paint_layers"] = report.get("paint_layers", 0) + 1
            if _node_has_projection_api(node):
                yield node
            children = _iter_named_children(
                node,
                ("sub_layers", "get_sub_layers", "children", "get_children", "get_nodes"),
            )
            is_group = (
                "group" in token
                or children is not None
                or any(
                    callable(getattr(node, name, None))
                    for name in ("sub_layers", "get_sub_layers", "children", "get_children")
                )
            )
            if is_group and children is None:
                report["groups_unreadable"] = report.get("groups_unreadable", 0) + 1
            if children:
                for child in iter_uv_fill_nodes(
                    children, report, max_depth=max_depth, _seen=_seen, _depth=_depth + 1
                ):
                    yield child
            has_mask = _safe_call(node, "has_mask")
            if has_mask:
                effects = _iter_named_children(node, ("mask_effects", "get_mask_effects"))
                if effects:
                    for child in iter_uv_fill_nodes(
                        effects, report, max_depth=max_depth, _seen=_seen, _depth=_depth + 1
                    ):
                        yield child
            content = _iter_named_children(
                node, ("content_effects", "get_content_effects"))
            if content:
                for child in iter_uv_fill_nodes(
                    content, report, max_depth=max_depth, _seen=_seen, _depth=_depth + 1
                ):
                    yield child
        except Exception:
            continue


def switch_uv_fill_node(node, api, source_uv_set):
    """戻りは (結果, 記録または理由)。結果は switched / skipped / failed。"""
    record = {
        "old_mode": None,
        "old_params": None,
        "copied_attrs": [],
        "missing_attrs": [],
        "name": None,
    }
    try:
        record["name"] = node.get_name()
    except Exception:
        record["name"] = None
    try:
        mode = node.get_projection_mode()
    except Exception as exc:
        return "failed", {"reason": "get_mode", "error": "{0}".format(exc)}
    if mode != api.ProjectionMode.UV:
        return "skipped", "not_uv"
    record["old_mode"] = mode
    try:
        old_params = node.get_projection_parameters()
    except Exception as exc:
        return "failed", {"reason": "get_params", "error": "{0}".format(exc)}
    record["old_params"] = old_params
    try:
        new_params = api.UVSetToUVSetProjectionParams()
    except Exception as exc:
        return "failed", {"reason": "make_params", "error": "{0}".format(exc)}
    for attr in _UV_FILL_PARAM_ATTRS:
        if old_params is not None and hasattr(old_params, attr) and hasattr(new_params, attr):
            try:
                setattr(new_params, attr, getattr(old_params, attr))
                record["copied_attrs"].append(attr)
            except Exception:
                record["missing_attrs"].append(attr)
        else:
            record["missing_attrs"].append(attr)
    try:
        new_params.source_uv_set = source_uv_set
    except Exception as exc:
        return "failed", {"reason": "set_source", "error": "{0}".format(exc)}
    # 要実機確認: set_projection_mode と set_projection_parameters の順序。
    try:
        node.set_projection_mode(api.ProjectionMode.UVSetToUVSet)
        node.set_projection_parameters(new_params)
    except Exception as exc:
        rollback_uv_fill_node(node, record, api)
        return "failed", {"reason": "set", "error": "{0}".format(exc), "record": record}
    try:
        new_mode = node.get_projection_mode()
        if new_mode != api.ProjectionMode.UVSetToUVSet:
            rollback_uv_fill_node(node, record, api)
            return "failed", {"reason": "verify_mode", "record": record}
        read_params = node.get_projection_parameters()
        source = getattr(read_params, "source_uv_set", None)
        if source is not None and source != source_uv_set:
            rollback_uv_fill_node(node, record, api)
            return "failed", {"reason": "verify_source", "record": record}
    except Exception as exc:
        rollback_uv_fill_node(node, record, api)
        return "failed", {"reason": "verify", "error": "{0}".format(exc), "record": record}
    return "switched", record


def rollback_uv_fill_node(node, record, api):
    if not isinstance(record, dict):
        return False
    ok = True
    old_mode = record.get("old_mode")
    old_params = record.get("old_params")
    try:
        if old_mode is not None and callable(getattr(node, "set_projection_mode", None)):
            node.set_projection_mode(old_mode)
    except Exception:
        ok = False
    try:
        if old_params is not None and callable(getattr(node, "set_projection_parameters", None)):
            node.set_projection_parameters(old_params)
    except Exception:
        ok = False
    return ok


class UvFillStackReport(object):
    def __init__(self, label=""):
        self.label = label
        self.visited = 0
        self.uv_fills_found = 0
        self.switched = 0
        self.skipped_not_uv = 0
        self.failed = 0
        self.rolled_back = 0
        self.paint_layers = 0
        self.groups_unreadable = 0
        self.needs_manual_check = False
        self.missing_attrs = []
        self.switched_names = []


def _remember_missing_attrs(report, detail):
    if not isinstance(detail, dict):
        return
    raw = detail.get("missing_attrs")
    if not isinstance(raw, list):
        record = detail.get("record")
        if isinstance(record, dict):
            raw = record.get("missing_attrs")
    if not isinstance(raw, list):
        return
    for attr in raw:
        if isinstance(attr, str) and attr and attr not in report.missing_attrs:
            report.missing_attrs.append(attr)


def switch_uv_fills_in_stack(stack, api, label, source_uv_set):
    report = UvFillStackReport(label)
    walk = {"paint_layers": 0, "groups_unreadable": 0}
    try:
        roots = api.get_root_layer_nodes(stack)
    except Exception:
        report.needs_manual_check = True
        return report
    applied = []
    failed = False
    for node in iter_uv_fill_nodes(roots, walk):
        report.visited += 1
        report.uv_fills_found += 1
        result, detail = switch_uv_fill_node(node, api, source_uv_set)
        _remember_missing_attrs(report, detail)
        if result == "switched":
            report.switched += 1
            name = None
            if isinstance(detail, dict):
                name = detail.get("name")
                applied.append((node, detail))
            else:
                applied.append((node, {}))
            if isinstance(name, str) and name:
                report.switched_names.append(name)
        elif result == "skipped":
            report.skipped_not_uv += 1
        else:
            report.failed += 1
            failed = True
            break
    report.paint_layers = walk.get("paint_layers", 0)
    report.groups_unreadable = walk.get("groups_unreadable", 0)
    if failed:
        for node, rec in applied:
            if rollback_uv_fill_node(node, rec, api):
                report.rolled_back += 1
            else:
                report.needs_manual_check = True
        report.switched = 0
        report.switched_names = []
    return report


class UvFillSummary(object):
    def __init__(self):
        self.enabled = False
        self.gate = None
        self.per_texture_set = {}
        self.switched = 0
        self.skipped = 0
        self.failed = 0
        self.rolled_back = 0
        self.needs_manual_check = False
        self.missing_attrs = []

    def to_status_dict(self):
        return {
            "enabled": bool(self.enabled),
            "switched": int(self.switched),
            "skipped": int(self.skipped),
            "failed": int(self.failed),
            "rolled_back": int(self.rolled_back),
        }

    def to_message(self):
        parts = []
        if self.switched >= 1:
            parts.append(
                "UV 投影のフィル {0} 件を旧 UV セット経由に切り替えました".format(
                    self.switched)
            )
        elif self.gate:
            parts.append(_UV_FILL_GATE_MESSAGES.get(self.gate, self.gate))
        if self.failed:
            parts.append("失敗 {0} 件".format(self.failed))
        if self.rolled_back:
            parts.append("ロールバック {0} 件".format(self.rolled_back))
        if self.needs_manual_check or self.missing_attrs:
            parts.append("一部は目視確認が必要です")
        cleaned = []
        for part in parts:
            if not isinstance(part, str) or part == "":
                continue
            cleaned.append(part.rstrip("。"))
        text = "。".join(cleaned)
        if text:
            text += "。"
        return _rebase_clip_message(text)


def _texture_set_name(ts):
    for name in ("name", "get_name"):
        fn = getattr(ts, name, None)
        if callable(fn):
            try:
                value = fn()
            except Exception:
                continue
            if isinstance(value, str):
                return value
        elif isinstance(fn, str):
            return fn
    if isinstance(ts, str):
        return ts
    return None


def _texture_set_key(name):
    if not isinstance(name, str):
        return ""
    return unicodedata.normalize("NFC", name).casefold()


def _iter_texture_set_stacks(ts):
    # 要実機確認: ts.all_stacks() / ts.get_stack()
    stacks = _safe_call(ts, "all_stacks")
    if stacks:
        try:
            return list(stacks)
        except TypeError:
            pass
    stack = _safe_call(ts, "get_stack")
    if stack is not None:
        return [stack]
    return []


def switch_uv_fills_for_project(api, texture_sets, ts_names, *, enabled, uv_old_index, log):
    summary = UvFillSummary()
    summary.enabled = bool(enabled)
    if not enabled:
        summary.gate = "disabled_by_setting"
        return summary
    if api is None:
        summary.gate = "no_api"
        return summary
    if uv_old_index is None:
        summary.gate = "uvold_absent"
        return summary
    wanted = set()
    if isinstance(ts_names, (list, tuple)):
        for name in ts_names:
            key = _texture_set_key(name)
            if key:
                wanted.add(key)
    # 空の依頼は全テクスチャセットに広げない。
    if not wanted:
        summary.gate = "no_matching_texture_set"
        return summary
    matched = []
    skipped_not_in_request = 0
    for ts in texture_sets or []:
        name = _texture_set_name(ts)
        key = _texture_set_key(name)
        if key not in wanted:
            skipped_not_in_request += 1
            continue
        matched.append((name or key, ts))
    if not matched:
        summary.gate = "no_matching_texture_set"
        summary.skipped += skipped_not_in_request
        return summary
    summary.skipped += skipped_not_in_request
    for name, ts in matched:
        counts = {
            "found": 0,
            "switched": 0,
            "skipped_not_uv": 0,
            "failed": 0,
            "rolled_back": 0,
        }
        for stack in _iter_texture_set_stacks(ts):
            report = switch_uv_fills_in_stack(stack, api, name, uv_old_index)
            counts["found"] += report.uv_fills_found
            counts["switched"] += report.switched
            counts["skipped_not_uv"] += report.skipped_not_uv
            counts["failed"] += report.failed
            counts["rolled_back"] += report.rolled_back
            summary.switched += report.switched
            summary.skipped += report.skipped_not_uv
            summary.failed += report.failed
            summary.rolled_back += report.rolled_back
            if report.needs_manual_check:
                summary.needs_manual_check = True
            for attr in report.missing_attrs:
                if attr not in summary.missing_attrs:
                    summary.missing_attrs.append(attr)
            if report.switched_names and callable(log):
                log("UV フィル切り替え レイヤー: {0}".format(
                    ", ".join(report.switched_names)))
        summary.per_texture_set[name] = counts
        if callable(log):
            missing = ", ".join(report.missing_attrs) if report.missing_attrs else "-"
            log(
                "UV フィル切り替え [{0}] 発見 {1} / 切り替え {2} / UV 以外 {3} / 失敗 {4} / ロールバック {5} / needs_manual_check={6} / missing_attrs={7}".format(
                    name,
                    counts["found"],
                    counts["switched"],
                    counts["skipped_not_uv"],
                    counts["failed"],
                    counts["rolled_back"],
                    "true" if report.needs_manual_check else "false",
                    missing,
                )
            )
    return summary


class ExportAttempt(object):
    def __init__(
        self,
        executed=False,
        ok=False,
        status=None,
        export_id=None,
        message="",
        locked_error=None,
        error=None,
    ):
        self.executed = bool(executed)
        self.ok = bool(ok)
        self.status = status
        self.export_id = export_id
        self.message = message if isinstance(message, str) else "{0}".format(message)
        self.locked_error = locked_error
        self.error = error


class RebaseReporter(object):
    def __init__(
        self,
        root,
        request_id,
        clock,
        log_fn,
        emit_status_fn,
        shutting_down_fn,
        plugin_version,
    ):
        self.root = root
        self.request_id = request_id
        self.clock = clock
        self._log_fn = log_fn
        self._emit_status_fn = emit_status_fn
        self._shutting_down_fn = shutting_down_fn
        self.plugin_version = plugin_version
        self.last_state = None
        self._pending_terminal = None
        self._terminal_tries = 0
        self.outcome = None

    def _shutting_down(self):
        if not callable(self._shutting_down_fn):
            return False
        try:
            return bool(self._shutting_down_fn())
        except Exception:
            return False

    def log(self, text):
        line = "{0}".format(text)
        if callable(self._log_fn):
            try:
                self._log_fn("info", line)
            except Exception:
                pass
        if self._shutting_down():
            try:
                _append_history("SP", line)
            except Exception:
                pass
            return
        if callable(self._emit_status_fn):
            try:
                self._emit_status_fn(line)
            except Exception:
                pass

    def state(self, name, **fields):
        try:
            status = build_rebase_status(
                self.request_id,
                name,
                self.clock(),
                self.plugin_version,
                reason=fields.get("reason"),
                message=fields.get("message", ""),
                backup_name=fields.get("backup_name"),
                result_export_id=fields.get("result_export_id"),
                uv_fills=fields.get("uv_fills"),
            )
        except ValueError as exc:
            self.log("warning: rebase 状態を組み立てられませんでした: {0}".format(exc))
            return False
        ok = write_rebase_status(self.root, status)
        if name in _REBASE_TERMINAL:
            if ok:
                self._pending_terminal = None
                self.last_state = name
                return True
            self._pending_terminal = status
            self._terminal_tries += 1
            if self._terminal_tries >= REBASE_TERMINAL_WRITE_MAX:
                self.log("error: 終端状態の書き込みを {0} 回失敗しました".format(
                    REBASE_TERMINAL_WRITE_MAX))
            else:
                self.log("warning: 終端状態の書き込みに失敗しました。再試行します")
            return False
        if not ok:
            self.log("warning: 状態ファイルの書き込みに失敗しました({0})".format(name))
            return False
        self.last_state = name
        return True

    def retry_terminal(self):
        if self._pending_terminal is None:
            return True
        if self._terminal_tries >= REBASE_TERMINAL_WRITE_MAX:
            return False
        ok = write_rebase_status(self.root, self._pending_terminal)
        self._terminal_tries += 1
        if ok:
            self.last_state = self._pending_terminal.get("state")
            self._pending_terminal = None
            return True
        if self._terminal_tries >= REBASE_TERMINAL_WRITE_MAX:
            self.log("error: 終端状態の書き込みを {0} 回失敗しました".format(
                REBASE_TERMINAL_WRITE_MAX))
        return False

    def finished(self, outcome):
        self.outcome = outcome


class RebaseJob(object):
    def __init__(self, request, reporter, api, settings, *, clock=time.time, approved=True):
        self.request = request if isinstance(request, dict) else {}
        self.reporter = reporter
        self.api = api
        self.settings = normalize_rebase_settings(settings)
        self.clock = clock
        self.approved = bool(approved)
        self.state = None
        self.finished = False
        self.outcome = None
        self.uv_fills = None
        self.backup_name = None
        self._reload_status = None
        self._reload_started_at = None
        self._reload_busy_since = None
        self._reload_called = False
        self._export_started_at = None
        self._next_action_at = None
        self._retry_attempt = 0
        self._retry_stage = None
        self._stage_entered_at = None
        self._auto_unwrap_logged = False
        self._cancel_ignored = False

    def _now(self, now=None):
        if now is not None:
            return now
        if callable(self.clock):
            return self.clock()
        return time.time()

    def _request_id(self):
        return self.request.get("request_id")

    def _write(self, name, **fields):
        if self.backup_name and "backup_name" not in fields:
            fields["backup_name"] = self.backup_name
        if self.uv_fills is not None and "uv_fills" not in fields:
            try:
                fields["uv_fills"] = self.uv_fills.to_status_dict()
            except Exception:
                pass
        self.reporter.state(name, **fields)
        self.state = name

    def _fail(self, reason, message=None, detail=None):
        if self.finished:
            return
        text = message
        if text is None:
            text = rebase_failure_message(
                reason, backup_name=self.backup_name, detail=detail)
        self._write("failed", reason=reason, message=text)
        self.finished = True
        self.outcome = "failed"
        self.reporter.finished("failed")

    def _done(self, result_export_id):
        uv_dict = None
        extra = ""
        if self.uv_fills is not None:
            try:
                uv_dict = self.uv_fills.to_status_dict()
                extra = self.uv_fills.to_message()
            except Exception:
                uv_dict = None
        # 案内文がすでに「リベースが完了しました。」で始まる。先頭には重ねない。
        parts = [REBASE_DONE_GUIDANCE]
        if extra:
            parts.append(extra)
        text = _rebase_clip_message(" ".join(part for part in parts if part))
        self._write(
            "done",
            message=text,
            result_export_id=result_export_id,
            uv_fills=uv_dict,
            backup_name=self.backup_name,
        )
        self.reporter.log(REBASE_DONE_GUIDANCE)
        self.finished = True
        self.outcome = "done"
        self.reporter.finished("done")

    def _end_reject(self, reason, prev_state):
        new_state, new_reason = rebase_reject_state(reason, prev_state)
        text = rebase_failure_message(new_reason, backup_name=self.backup_name)
        self._write(new_state, reason=new_reason, message=text)
        self.finished = True
        self.outcome = new_state
        self.reporter.finished(new_state)

    def run(self):
        self.state = "preparing"
        self._stage_entered_at = self._now()
        self._write("preparing")

    def on_reload_status(self, status):
        if self.finished:
            return
        if self.api is not None and getattr(self.api, "closed", False):
            return
        if self._reload_status is not None:
            self.reporter.log("reload_mesh の追加のコールバックを無視します")
            return
        self._reload_status = status

    def abort(self, reason, message):
        if self.finished:
            return
        text = message
        if not text:
            text = rebase_failure_message(reason, backup_name=self.backup_name)
        self._write("failed", reason=reason, message=text)
        self.finished = True
        self.outcome = "failed"
        self.reporter.finished("failed")

    def ignore_cancel(self):
        self._cancel_ignored = True

    def tick(self, now=None):
        try:
            self._tick_impl(now)
        except Exception as exc:
            try:
                self.reporter.log("error: RebaseJob.tick: {0}".format(exc))
            except Exception:
                pass

    def _tick_impl(self, now):
        now = self._now(now)
        if self.finished:
            self.reporter.retry_terminal()
            return
        if self.api is not None and getattr(self.api, "closed", False):
            return
        if self._next_action_at is not None and now < self._next_action_at:
            return
        if self.state == "preparing":
            self._tick_preparing(now)
        elif self.state == "backing_up":
            self._tick_backing_up(now)
        elif self.state == "reloading":
            self._tick_reloading(now)
        elif self.state == "exporting":
            self._tick_exporting(now)

    def _tick_preparing(self, now):
        decision = self.api.recheck(self.request)
        action = getattr(decision, "action", None)
        reason = getattr(decision, "reason", None)
        if action in ("ask", "run"):
            self._retry_attempt = 0
            self._write("backing_up")
            return
        if action == "wait":
            return
        if action == "reject":
            self._end_reject(reason, "preparing")
            return

    def _schedule_retry(self, now, stage):
        self._retry_attempt += 1
        delay = rebase_retry_delay(
            self._retry_attempt, self.settings.get("rebase_retry_max"))
        if delay is None:
            return False
        self._retry_stage = stage
        self._next_action_at = now + delay
        self.reporter.log(
            "プロジェクトがロック中のため {0} 秒後に再試行します({1})".format(
                delay, self._retry_attempt)
        )
        return True

    def _tick_backing_up(self, now):
        spp_before = self.api.project_file_path()
        try:
            full, name, backup_dir = rebase_backup_path(
                spp_before, self._request_id(), self.api.file_exists)
        except ValueError:
            self._fail("backup_failed")
            return
        self.backup_name = name
        self._write("backing_up", backup_name=name)
        try:
            self.api.make_dirs(backup_dir)
            self.api.save_as_copy(full)
        except Exception as exc:
            if _is_project_error(exc):
                if not self._schedule_retry(now, "backing_up"):
                    self._fail("project_locked")
                return
            self._fail("backup_failed", detail="{0}".format(exc))
            return
        if not self.api.file_exists(full):
            self._fail("backup_failed")
            return
        size_fn = getattr(self.api, "file_size", None)
        try:
            if callable(size_fn):
                size = size_fn(full)
            else:
                size = os.path.getsize(full)
        except Exception:
            size = None
        if size is not None and size <= 0:
            self._fail("backup_failed")
            return
        spp_after = self.api.project_file_path()
        if spp_after != spp_before:
            self._fail("backup_failed")
            return
        self.reporter.log("バックアップ: {0}".format(name))
        self._retry_attempt = 0
        self._write("reloading")

    def _reload_busy_wait_exceeded(self, now, timeout_s):
        if self._reload_busy_since is None:
            self._reload_busy_since = now
            return False
        return (now - self._reload_busy_since) > timeout_s

    def _tick_reloading(self, now):
        timeout_s = self.settings.get("rebase_reload_timeout_s", 600.0)
        if not self._reload_called:
            if self.api.is_busy():
                if self._reload_busy_wait_exceeded(now, timeout_s):
                    self._fail("reload_timeout")
                return
            self._reload_busy_since = None
            try:
                supports = self.api.supports_auto_unwrap()
            except Exception:
                supports = False
            if not supports and not self._auto_unwrap_logged:
                self.reporter.log(REBASE_AUTO_UNWRAP_UNSUPPORTED_LOG)
                self._auto_unwrap_logged = True
            try:
                kwargs = build_reload_settings_kwargs(self.request, supports)
            except ValueError:
                self._fail("bad_settings")
                return
            try:
                settings_obj = self.api.make_reload_settings(**kwargs)
                mesh = self.request.get("mesh") or {}
                path = mesh.get("path")
                self.api.reload_mesh(path, settings_obj, self.on_reload_status)
            except Exception as exc:
                if _is_project_error(exc):
                    if not self._schedule_retry(now, "reloading"):
                        self._fail("project_locked")
                    return
                self._fail("reload_error", detail="{0}".format(exc))
                return
            self._reload_called = True
            self._reload_started_at = now
            self._reload_busy_since = None
            self._write("reloading")
            return
        if self._reload_status is None:
            if self._reload_started_at is not None and (now - self._reload_started_at) > timeout_s:
                self._fail("reload_timeout")
            return
        kind = classify_reload_status(self._reload_status)
        if kind == "success":
            if self.api.is_busy():
                if self._reload_busy_wait_exceeded(now, timeout_s):
                    self._fail("reload_timeout")
                return
            self._reload_busy_since = None
            self._switch_uv_fills()
            self._retry_attempt = 0
            self._write("exporting")
            return
        if kind == "error":
            self._fail("reload_error")
            return
        self._fail("reload_timeout")

    def _switch_uv_fills(self):
        self._write("reloading", message=REBASE_UV_FILL_PROGRESS)
        try:
            ctx = self.api.uv_fill_context()
        except Exception as exc:
            self.reporter.log("UV フィル切り替えをスキップしました: {0}".format(exc))
            self.uv_fills = UvFillSummary()
            self.uv_fills.gate = "no_api"
            return
        fill_api = None
        texture_sets = []
        if isinstance(ctx, dict):
            fill_api = ctx.get("api")
            texture_sets = ctx.get("texture_sets") or []
        elif isinstance(ctx, (tuple, list)) and ctx:
            fill_api = ctx[0]
            if len(ctx) > 1:
                texture_sets = ctx[1] or []
        # 空の ts_names をプロジェクト全件で埋めない。
        request_names = self.request.get("ts_names")
        try:
            self.uv_fills = switch_uv_fills_for_project(
                fill_api,
                texture_sets,
                request_names,
                enabled=bool(self.settings.get("rebase_reproject_uv_fills")),
                uv_old_index=get_uv_old_index(self.request),
                log=self.reporter.log,
            )
        except Exception as exc:
            self.reporter.log("UV フィル切り替えで例外: {0}".format(exc))
            self.uv_fills = UvFillSummary()
            self.uv_fills.gate = "no_api"

    def _tick_exporting(self, now):
        if self._export_started_at is None:
            self._export_started_at = now
            self._write("exporting")
        if not self.request.get("final_full"):
            self._finish_success(None)
            return
        timeout_s = self.settings.get("rebase_reload_timeout_s", 600.0)
        try:
            attempt = self.api.run_final_export(self._request_id())
        except Exception as exc:
            if _is_project_error(exc):
                if not self._schedule_retry(now, "exporting"):
                    self._fail(
                        "project_locked",
                        detail="メッシュは読み直し済み。Final の書き出しをやり直してください",
                    )
                return
            self._fail("export_failed", detail="{0}".format(exc))
            return
        if attempt is None:
            attempt = ExportAttempt(executed=False)
        if not attempt.executed:
            if (now - self._export_started_at) > timeout_s:
                self._fail("export_failed")
            return
        if attempt.locked_error is not None:
            if not self._schedule_retry(now, "exporting"):
                self._fail(
                    "project_locked",
                    detail="メッシュは読み直し済み。Final の書き出しをやり直してください",
                )
            return
        if (not attempt.ok) or attempt.error is not None:
            detail = attempt.message
            if detail and (
                "メッシュは読み直し済み" in detail
                or "やり直してください" in detail
            ):
                detail = None
            self._fail("export_failed", detail=detail)
            return
        self._finish_success(attempt.export_id)

    def _finish_success(self, export_id):
        try:
            ok = self.api.record_painter_mesh(self.request)
            if ok is False:
                self.reporter.log("warning: メッシュ記録に失敗しました。リベースは完了とします")
        except Exception as exc:
            self.reporter.log(
                "warning: メッシュ記録に失敗しました。リベースは完了とします: {0}".format(exc)
            )
        self._done(export_id)


def _is_project_error(exc):
    try:
        return isinstance(exc, sp_exception.ProjectError)
    except Exception:
        return type(exc).__name__ == "ProjectError"


class RebaseCoordinator(object):
    def __init__(self, api, dialog_factory, job_factory, clock=time.time, hooks=None):
        self.api = api
        self.dialog_factory = dialog_factory
        self.job_factory = job_factory
        self.clock = clock if callable(clock) else time.time
        self.hooks = hooks
        self._precheck = RebasePrecheckCaches()
        self._job = None
        self._job_request_id = None
        self._dialog = None
        self._dialog_rid = None
        self._dialog_action = None
        self._entries = {}
        self._cancelled = set()
        self._superseded = set()
        self._prev_scan = None
        self._recover_key = None
        self._cfg = None
        self._settings = None
        self._last_cfg_at = None
        self._shutting_down = False
        self._root = None
        self._last_wait_reason = {}
        self._needs_saving_missing_logged = False
        self._can_disable_cache = None
        self._read_counts = {}

    def _now(self, now=None):
        if now is not None:
            return now
        return self.clock()

    def _log(self, level, text):
        if self.hooks is not None and callable(getattr(self.hooks, "log", None)):
            try:
                self.hooks.log(level, text)
                return
            except Exception:
                pass
        _log(level, text)

    def _emit(self, text):
        if self._shutting_down:
            self._log("info", text)
            try:
                _append_history("SP", text)
            except Exception:
                pass
            return
        if self.hooks is not None and callable(getattr(self.hooks, "emit_status", None)):
            try:
                self.hooks.emit_status(text)
                return
            except Exception:
                pass
        _log("info", text)

    def _is_closed(self):
        if self._shutting_down:
            return True
        if self.api is not None and getattr(self.api, "closed", False):
            return True
        if self.hooks is not None and callable(getattr(self.hooks, "is_shutting_down", None)):
            try:
                return bool(self.hooks.is_shutting_down())
            except Exception:
                return False
        return False

    def _set_blocks(self, value):
        if self.hooks is not None and callable(getattr(self.hooks, "set_blocks_exports", None)):
            try:
                self.hooks.set_blocks_exports(bool(value))
            except Exception:
                pass

    def _resume_blocked(self):
        if self.hooks is not None and callable(getattr(self.hooks, "resume_blocked_exports", None)):
            try:
                self.hooks.resume_blocked_exports()
            except Exception:
                pass

    def _load_settings(self, now):
        if self._last_cfg_at is not None and (now - self._last_cfg_at) < _REBASE_CFG_RELOAD_S:
            return
        loader = load_config
        if self.hooks is not None and callable(getattr(self.hooks, "load_config", None)):
            loader = self.hooks.load_config
        try:
            self._cfg = loader()
        except Exception as exc:
            self._log("error", "rebase: 設定の再読込に失敗しました: {0}".format(exc))
            if self._cfg is None:
                self._cfg = {}
        self._settings = normalize_rebase_settings(self._cfg)
        self._last_cfg_at = now

    def _project_open(self):
        if self.api is not None and callable(getattr(self.api, "project_open", None)):
            try:
                return bool(self.api.project_open())
            except Exception:
                return False
        return False

    def _current_key(self):
        if self.hooks is not None and callable(getattr(self.hooks, "current_project_key", None)):
            try:
                return self.hooks.current_project_key()
            except Exception:
                return None
        if self.api is not None and callable(getattr(self.api, "current_project_key", None)):
            try:
                return self.api.current_project_key()
            except Exception:
                return None
        return None

    def _subfolder(self):
        if self.hooks is not None and callable(getattr(self.hooks, "project_subfolder", None)):
            try:
                return self.hooks.project_subfolder()
            except Exception:
                return None
        return None

    def tick(self, now=None):
        try:
            self._tick_impl(now)
        except Exception as exc:
            self._log("error", "RebaseCoordinator.tick: {0}".format(exc))

    def _tick_impl(self, now):
        now = self._now(now)
        if self._is_closed():
            return
        if not self._project_open():
            return
        self._load_settings(now)
        if self._job is not None:
            self._decline_preparing_if_cancelled()
            if self._job is not None:
                self._job.tick(now)
            if self._job is not None and self._job.finished:
                if self._terminal_write_pending():
                    return
                self._on_job_finished()
            if self._job is not None:
                return
        key = self._current_key()
        if _is_unsaved_project_key(key):
            return
        subfolder = self._subfolder()
        root = resolve_uvremap_root(self._cfg, subfolder)
        self._root = root
        if not root:
            return
        requests_dir = rebase_requests_dir(root)
        if not os.path.isdir(requests_dir):
            return
        self._recover_if_needed(root, key, now)
        self._scan_and_ingest(root, now)
        self._apply_cancels(root)
        self._apply_superseded()
        if self._dialog_action:
            self._handle_dialog_action(now)
        self._process_active(root, now)

    def _terminal_write_pending(self):
        """終端の書き込みが失敗したまま、再試行の上限に達していなければ真。"""
        job = self._job
        if job is None:
            return False
        reporter = getattr(job, "reporter", None)
        if reporter is None:
            return False
        if getattr(reporter, "_pending_terminal", None) is None:
            return False
        tries = getattr(reporter, "_terminal_tries", 0)
        return tries < REBASE_TERMINAL_WRITE_MAX

    def _decline_preparing_if_cancelled(self):
        """preparing までのジョブは .cancel を見て declined で止める。"""
        job = self._job
        if job is None or job.finished:
            return
        state = getattr(job, "state", None)
        if state not in REBASE_CANCELABLE_STATES:
            return
        rid = self._job_request_id
        root = self._root
        if not isinstance(rid, str) or not root:
            return
        cancelled = rid in self._cancelled
        if not cancelled:
            try:
                cancelled = os.path.isfile(rebase_cancel_path(root, rid))
            except OSError:
                cancelled = False
        if not cancelled:
            return
        self._cancelled.add(rid)
        try:
            job._end_reject("cancelled", state)
        except Exception as exc:
            self._log("error", "rebase: preparing 中の取り消しに失敗しました: {0}".format(exc))

    def _on_job_finished(self):
        rid = self._job_request_id
        if rid:
            state = getattr(self._job, "state", None)
            self._precheck.drop_if_terminal(rid, state)
            entry = self._entries.get(rid)
            if entry is not None:
                entry["state"] = state
        self._job = None
        self._job_request_id = None
        self._set_blocks(False)
        self._resume_blocked()

    def _recover_if_needed(self, root, key, now):
        if self._recover_key == key:
            return
        self._recover_key = key
        for rid in _iter_status_ids(root):
            if self._job is not None and rid == self._job_request_id:
                continue
            prev = read_rebase_status(root, rid)
            if prev is None:
                continue
            state = prev.get("state")
            if state in ("preparing", "backing_up", "reloading", "exporting"):
                interrupted = build_interrupted_status(prev, now, __version__)
                if interrupted is not None:
                    write_rebase_status(root, interrupted)
                    self._precheck.drop(rid)
            elif state in _REBASE_TERMINAL:
                continue

    def _scan_and_ingest(self, root, now):
        scan = scan_rebase_dir(root)
        diff = diff_rebase_scan(self._prev_scan, scan)
        self._prev_scan = scan
        for rid in diff.new_cancel_ids:
            entry = self._entries.get(rid)
            state = entry.get("state") if entry else None
            if self._job is not None and rid == self._job_request_id:
                job_state = getattr(self._job, "state", None)
                if job_state in REBASE_LOCKED_STATES:
                    continue
            if state in REBASE_CANCELABLE_STATES or state is None:
                self._cancelled.add(rid)
        for rid in diff.changed_ids:
            existing = read_rebase_status(root, rid)
            if existing is not None and existing.get("state") in _REBASE_TERMINAL:
                self._precheck.drop(rid)
                continue
            request, reason = read_rebase_request_with_reason(root, rid)
            self._read_counts[rid] = self._read_counts.get(rid, 0) + 1
            if request is None:
                if existing is None:
                    reject_reason = reason or "bad_request"
                    self._write_once(
                        root, rid, "rejected", now, reason=reject_reason,
                        message=rebase_failure_message(reject_reason),
                    )
                continue
            entry = self._entries.get(rid) or {}
            entry["request"] = request
            if existing is None:
                if entry.get("state") is None:
                    self._write_once(root, rid, "received", now)
                    entry["state"] = "received"
            else:
                entry["state"] = existing.get("state")
            self._entries[rid] = entry

    def _apply_cancels(self, root):
        for rid in list(self._cancelled):
            entry = self._entries.get(rid)
            if entry is None:
                continue
            state = entry.get("state")
            if state not in REBASE_CANCELABLE_STATES:
                continue
            if self._job is not None and rid == self._job_request_id:
                if getattr(self._job, "state", None) in REBASE_LOCKED_STATES:
                    continue
            self._finish_request(
                root, rid, "declined", reason="cancelled",
                message=rebase_failure_message("cancelled"),
            )
            if self._dialog_rid == rid:
                self._close_dialog()

    def _apply_superseded(self):
        live = []
        for rid, entry in self._entries.items():
            state = entry.get("state")
            if state in _REBASE_TERMINAL:
                continue
            request = entry.get("request")
            if request:
                live.append(request)
        if not live:
            return
        for rid in find_superseded_ids(live):
            entry = self._entries.get(rid)
            if entry is None:
                continue
            state = entry.get("state")
            if self._job is not None and rid == self._job_request_id:
                if getattr(self._job, "state", None) in REBASE_LOCKED_STATES:
                    continue
            if state not in REBASE_CANCELABLE_STATES:
                continue
            self._superseded.add(rid)
            self._finish_request(
                self._root, rid, "superseded", reason="superseded",
                message=rebase_failure_message("superseded"),
            )
            if self._dialog_rid == rid:
                self._close_dialog()

    def _handle_dialog_action(self, now):
        action = self._dialog_action
        rid = self._dialog_rid
        self._dialog_action = None
        if not rid:
            return
        entry = self._entries.get(rid)
        if action == "run":
            self._close_dialog()
            if entry and entry.get("request"):
                self._start_job(entry["request"], approved=True)
            return
        self._close_dialog()
        if entry and entry.get("state") not in _REBASE_TERMINAL:
            self._finish_request(
                self._root, rid, "declined", reason="declined",
                message=rebase_failure_message("declined"),
            )

    def _process_active(self, root, now):
        if self._job is not None:
            return
        active = self._newest_nonterminal()
        if active is None:
            return
        rid, entry = active
        request = entry.get("request")
        if request is None:
            return
        mesh = request.get("mesh")
        exists, sha1_ok, new_cache = mesh_precheck(
            mesh, cache=self._precheck.get(rid))
        self._precheck.put(rid, new_cache)
        painter_state = self._build_state(
            request, now, exists, sha1_ok, rid)
        decision = decide_approval(request, painter_state, self._settings)
        action = decision.action
        reason = decision.reason
        current = entry.get("state")
        if action == "reject":
            new_state, new_reason = rebase_reject_state(reason, current)
            self._finish_request(
                root, rid, new_state, reason=new_reason,
                message=rebase_failure_message(new_reason),
            )
            if self._dialog_rid == rid:
                self._close_dialog()
            return
        if action == "wait":
            self._note_wait(rid, reason, now, root, current)
            return
        self._last_wait_reason.pop(rid, None)
        entry.pop("first_wait_at", None)
        if action == "ask":
            if self._dialog is not None and self._dialog_rid == rid:
                self._refresh_open_dialog(root, rid, request, painter_state, now)
                return
            if current != "awaiting_approval":
                self._write_once(root, rid, "awaiting_approval", now)
                entry["state"] = "awaiting_approval"
            self._open_dialog(request, painter_state)
            return
        if action == "run":
            self._start_job(request, approved=True)

    def _refresh_open_dialog(self, root, rid, request, painter_state, now):
        if bool(painter_state.get("cancelled")):
            self._finish_request(
                root, rid, "declined", reason="cancelled",
                message=rebase_failure_message("cancelled"),
            )
            self._close_dialog()
            return
        if bool(painter_state.get("superseded")):
            self._finish_request(
                root, rid, "superseded", reason="superseded",
                message=rebase_failure_message("superseded"),
            )
            self._close_dialog()
            return
        expires_at = request.get("expires_at")
        if _rebase_is_finite_number(expires_at) and now > expires_at:
            self._finish_request(
                root, rid, "expired", reason="expired",
                message=rebase_failure_message("expired"),
            )
            self._close_dialog()
            return
        rounded = self._settings or {}
        if rounded.get("rebase_enabled") is False:
            self._finish_request(
                root, rid, "failed", reason="disabled",
                message=rebase_failure_message("disabled"),
            )
            self._close_dialog()

    def _note_wait(self, rid, reason, now, root, current):
        entry = self._entries.get(rid) or {}
        if entry.get("first_wait_at") is None:
            entry["first_wait_at"] = now
            self._entries[rid] = entry
        prev_reason = self._last_wait_reason.get(rid)
        if prev_reason != reason:
            self._last_wait_reason[rid] = reason
            self._emit("rebase 待機: {0}".format(reason))
        if wait_timed_out(entry.get("first_wait_at"), now, self._settings):
            if current in ("received", "awaiting_approval"):
                self._finish_request(
                    root, rid, "expired", reason="expired",
                    message=rebase_failure_message("expired"),
                )
                if self._dialog_rid == rid:
                    self._close_dialog()

    def _newest_nonterminal(self):
        best = None
        best_created = None
        for rid, entry in self._entries.items():
            if entry.get("state") in _REBASE_TERMINAL:
                continue
            request = entry.get("request")
            if not request:
                continue
            created = request.get("created_at")
            if best is None or (
                _rebase_is_finite_number(created)
                and (best_created is None or created > best_created)
            ):
                best = (rid, entry)
                best_created = created
        return best

    def _build_state(self, request, now, mesh_exists, mesh_sha1_ok, rid):
        snap = {}
        if self.api is not None and callable(getattr(self.api, "snapshot", None)):
            try:
                snap = self.api.snapshot() or {}
            except Exception:
                snap = {}
        is_open = snap.get("is_open")
        if is_open is None:
            is_open = False
        in_edition = snap.get("in_edition")
        if in_edition is None:
            in_edition = False
        project_key = snap.get("project_key")
        saved_path = snap.get("saved_path")
        busy = snap.get("busy")
        if busy is None:
            busy = True
        exporting = snap.get("exporting")
        if exporting is None:
            exporting = True
        if snap.get("needs_saving_missing") and not self._needs_saving_missing_logged:
            self._needs_saving_missing_logged = True
            if effective_rebase_approval(self._settings) == "auto":
                self._log(
                    "warning",
                    "project.needs_saving が読めないため、auto では未保存として待ちます。要実機確認。",
                )
            else:
                self._log(
                    "warning",
                    "project.needs_saving が読めないため、未保存判定は偽です。要実機確認。",
                )
        needs_saving = needs_saving_for_rebase(snap, self._settings)
        can_disable = snap.get("can_disable_auto_unwrap")
        if can_disable is None:
            can_disable = False
        last_edit_at = snap.get("last_edit_at")
        if not mesh_exists:
            mesh_sha1_ok = None
        return build_painter_state(
            now=now,
            is_open=is_open,
            in_edition=in_edition,
            project_key=project_key,
            saved_path=saved_path,
            busy=busy,
            exporting=exporting,
            needs_saving=needs_saving,
            backup_includes_unsaved=BACKUP_INCLUDES_UNSAVED,
            mesh_exists=mesh_exists,
            mesh_sha1_ok=mesh_sha1_ok,
            superseded=rid in self._superseded,
            cancelled=rid in self._cancelled,
            can_disable_auto_unwrap=can_disable,
            last_edit_at=last_edit_at,
        )

    def _write_once(self, root, rid, state, now, reason=None, message=""):
        entry = self._entries.get(rid) or {}
        if entry.get("state") == state and entry.get("reason") == reason:
            return False
        status = build_rebase_status(
            rid, state, now, __version__, reason=reason, message=message)
        ok = write_rebase_status(root, status)
        if not ok:
            self._log("warning", "rebase: 状態 {0} の書き込みに失敗しました".format(state))
        entry["state"] = state
        entry["reason"] = reason
        self._entries[rid] = entry
        if state in _REBASE_TERMINAL:
            self._precheck.drop(rid)
        self._emit("rebase {0}: {1}".format(rid, state))
        return ok

    def _finish_request(self, root, rid, state, reason=None, message=""):
        now = self._now()
        self._write_once(root, rid, state, now, reason=reason, message=message)
        self._precheck.drop_if_terminal(rid, state)

    def _open_dialog(self, request, painter_state):
        if not callable(self.dialog_factory):
            return
        rid = request.get("request_id")
        backup_dir = REBASE_BACKUP_DIRNAME
        try:
            spp = self.api.project_file_path() if self.api is not None else None
            _full, _name, backup_dir = rebase_backup_path(
                spp, rid, self.api.file_exists if self.api is not None else os.path.exists)
        except Exception:
            backup_dir = REBASE_BACKUP_DIRNAME
        uv_fill_reproject = bool(
            (self._settings or {}).get("rebase_reproject_uv_fills")
        ) and get_uv_old_index(request) is not None
        text = build_rebase_approval_text(
            request,
            backup_dir=backup_dir,
            needs_saving=bool(painter_state.get("needs_saving")),
            can_disable_auto_unwrap=bool(
                painter_state.get("can_disable_auto_unwrap")),
            backup_includes_unsaved=BACKUP_INCLUDES_UNSAVED,
            uv_fill_reproject=uv_fill_reproject,
        )
        self._dialog_action = None
        self._dialog_rid = rid
        self._dialog = self.dialog_factory(text, self._on_dialog_result)

    def _on_dialog_result(self, action):
        if self._is_closed():
            return
        self._dialog_action = action

    def _close_dialog(self):
        dialog = self._dialog
        self._dialog = None
        self._dialog_rid = None
        if dialog is None:
            return
        for name in ("disconnect_all", "disconnect"):
            fn = getattr(dialog, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        for name in ("close", "deleteLater"):
            fn = getattr(dialog, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    def _start_job(self, request, approved=True):
        if self._job is not None:
            return
        if not callable(self.job_factory):
            return
        rid = request.get("request_id")
        reporter = RebaseReporter(
            root=self._root,
            request_id=rid,
            clock=self.clock,
            log_fn=self._log,
            emit_status_fn=self._emit,
            shutting_down_fn=self._is_closed,
            plugin_version=__version__,
        )
        cache = self._precheck.get(rid)
        job = self.job_factory(
            request, reporter, self.api, self._settings,
            clock=self.clock, approved=approved,
        )
        if cache is not None and hasattr(job, "_precheck_cache"):
            job._precheck_cache = cache
        self._job = job
        self._job_request_id = rid
        self._set_blocks(True)
        entry = self._entries.get(rid) or {}
        entry["state"] = "preparing"
        self._entries[rid] = entry
        self._emit("rebase ジョブ開始: {0}".format(rid))
        job.run()

    def shutdown(self):
        self._shutting_down = True
        self._close_dialog()
        if self._job is not None and not self._job.finished:
            try:
                self._job.abort(
                    "interrupted",
                    rebase_failure_message(
                        "interrupted", backup_name=getattr(self._job, "backup_name", None)),
                )
            except Exception:
                pass
        self._job = None
        self._job_request_id = None
        self.api = None
        self._precheck.clear()
        self._entries.clear()
        self._set_blocks(False)

    def on_project_closing(self):
        self._close_dialog()
        if self._job is not None and not self._job.finished:
            try:
                self._job.abort(
                    "interrupted",
                    rebase_failure_message(
                        "interrupted", backup_name=getattr(self._job, "backup_name", None)),
                )
            except Exception:
                pass
            self._on_job_finished()
        self._precheck.clear()
        self._prev_scan = None
        self._entries.clear()
        self._cancelled.clear()
        self._superseded.clear()
        self._recover_key = None
        self._last_wait_reason.clear()


def _iter_status_ids(root):
    directory = rebase_status_dir(root)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue
        rid = name[:-5]
        if is_valid_rebase_request_id(rid):
            yield rid


# ---------------------------------------------------------------------------
# 同期ロジック本体
# ---------------------------------------------------------------------------

class LiveSyncEngine(QtCore.QObject):

    status_changed = QtCore.Signal(str)      # ログ1行を通知
    stats_changed = QtCore.Signal(dict)       # 統計情報の更新を通知
    # Phase 2: (現在の全テクスチャセット名一覧, 新規追加分, 消失確定分) を通知
    structure_changed = QtCore.Signal(list, list, list)
    # 2026.07.19-02: 書き出し(SP終了処理と衝突しうる後処理を含む)が
    # 進行中かどうかをUI側へ通知する。SPネイティブの「書き出し中」
    # ダイアログはexport_project_textures()の実行中しか保護しないため、
    # ダイアログが消えた後の後処理区間も含めてUI側で「まだ閉じないで」と
    # 警告できるようにする(True=開始、False=完全に終了)。
    exporting_changed = QtCore.Signal(bool)
    # 2026.07.19-03(フェーズ2): 「保存内容がまだFinalに反映されていない」
    # 状態(=要求済みだが未完了、クールダウン待ち・リトライ待ちを含む)を
    # UI側へ通知する。
    final_pending_changed = QtCore.Signal(bool)

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.enabled = False
        self.exporting = False
        self.pending_reexport = False
        # Phase 6: exporting中に高画質(preview=False)の要求が来た場合、
        # 再試行時にプレビューへ格下げされてしまわないよう区別して覚えておく。
        self.pending_final = False
        self.last_hashes = {}
        # 2026.07.19-02: _unchanged() のサイズ事前チェック用(詳細は
        # _unchanged() のdocstring参照)。
        self.last_sizes = {}
        self.dirty_stack_ids = set()
        # 2026.07.19-03(フェーズ2): Final書き出し専用の差分集合。
        # dirty_stack_ids(Preview用)とは別に、Final書き出しが成功する
        # までの間、変更のあったstack_idを蓄積し続ける(Preview側の
        # 同期が先にdirty_stack_idsを消費しても影響を受けない)。
        self.dirty_stack_ids_final = set()
        # このセッション(このプロジェクトを開いてから)で、Finalの差分
        # 書き出しの基準となる「全量書き出し」を一度でも成功させたか。
        # Falseの間はforce_full指定が無くても常に全量書き出しへ
        # フォールバックする(安全側)。プロジェクトを開き直すたびに
        # on_project_edition_entered()でFalseへ戻す。
        self.final_baseline_established = False
        self._last_final_completed_at = None
        # 「今すぐ全量同期」ボタン、またはリトライ・busy再試行の連鎖の
        # 途中で立てられる、次に発火するFinal書き出しを強制的に全量に
        # するフラグ。成功または再試行諦めまでクリアされない
        # (_request_final_export()を経由する他の呼び出しに上書きされて
        # 消えないようにするため)。
        self._final_export_force_full = False
        # Phase 2 最適化: 消失検知の猶予カウンタ(誤検知抑制用)
        self._missing_streak = {}

        self.stats = {
            "sync_count": 0,
            "skip_count": 0,
            "error_count": 0,
            "last_sync_at": None,
            "last_duration_ms": None,
        }

        self.debounce_timer = QtCore.QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.timeout.connect(self.request_export)

        # 2026.07.19-01: 保存直後(および保存連打時にキューされた)Final
        # 書き出しの遅延実行を、この名前付きQTimer1つに統一する。
        # 匿名の QtCore.QTimer.singleShot() だと、後から「まだ発火して
        # いないものをキャンセルする」ことができず、shutdown() 側で
        # 終了処理と重なって発火するのを防げなかったため、明示的に
        # stop() できるインスタンスとして保持する。
        self._final_export_timer = QtCore.QTimer(self)
        self._final_export_timer.setSingleShot(True)
        self._final_export_timer.timeout.connect(self._on_final_export_timer_fired)
        self._final_export_retry_attempt = 0
        # 2026.07.24-02: 保存直後ロックの再試行(最大約19秒)を使い切って
        # 一度打ち切った後、自動フォローアップ(_FINAL_EXPORT_GIVEUP_
        # FOLLOWUP_MS後にもう一度だけ試す)を既にスケジュール済みかどうか。
        # これが無いと、フォローアップも失敗した場合に無限に再スケジュール
        # し続けてしまう(無限ループ防止のガード)。
        self._final_export_giveup_followup_pending = False

        # 2026.10.18(1.3.0): 直近の書き出しで実際に置き換えたファイル数。
        # sync_count は、この値が 1 以上の書き出しが終わったときだけ増やす。
        self._export_moved_count = 0
        # last_imported_mesh_path が無い環境では、プロジェクトを開くたびに
        # 同じ警告を出さない。
        self._mesh_api_missing_logged = False
        # 空パス / busy の再試行を何回スケジュールしたか。新規の記録予約で 0 に戻す。
        self._mesh_record_retries = 0

        # 2026.10.18(1.3.0): プロジェクトを開いた直後に、読み込んだメッシュを
        # 共有設定へ記録する。名前付き単発タイマーなので shutdown() から
        # stop() できる(匿名の singleShot は終了処理と重なっても止められない)。
        self._mesh_record_timer = QtCore.QTimer(self)
        self._mesh_record_timer.setSingleShot(True)
        self._mesh_record_timer.timeout.connect(self._on_mesh_record_timer)

        # 2026.10.19(1.4.0 PB-b): Save As 後の Preview 再書き出し。
        # 匿名 singleShot は shutdown から止められない(S2)。
        self._preview_migration_timer = QtCore.QTimer(self)
        self._preview_migration_timer.setSingleShot(True)
        self._preview_migration_timer.timeout.connect(self._on_preview_migration_timer)

        # 2026.07.19-01: shutdown() 後に上記タイマーが万一発火しても
        # 実処理へ進ませないための最終ガード(タイマーのstop()と合わせた
        # 二重の安全策)。
        self._shutting_down = False

        # 2026.10.19(1.4.0 PB-b): リベースの 1 秒タイマーと司令塔。
        self._rebase_blocks_exports = False
        self._rebase_block_status_emitted = False
        self._last_texture_event_at = None
        self._last_export_attempt = None
        self._export_last_meta = None
        self._rebase_tick_guard = False
        self._needs_saving_missing_logged = False
        self._rebase_timer = QtCore.QTimer(self)
        if hasattr(self._rebase_timer, "setObjectName"):
            self._rebase_timer.setObjectName("rebase_watch")
        self._rebase_timer.timeout.connect(self._on_rebase_timer)
        self._rebase_api = PainterRebaseApi(self)
        self._rebase = RebaseCoordinator(
            self._rebase_api,
            make_rebase_approval_dialog,
            default_rebase_job_factory,
            clock=time.time,
            hooks=RebaseEngineHooks(self),
        )

        event.DISPATCHER.connect_strong(event.TextureStateEvent, self.on_texture_state)
        event.DISPATCHER.connect_strong(event.ProjectSaved, self.on_project_saved)
        event.DISPATCHER.connect_strong(event.ProjectAboutToClose, self.on_project_closing)
        event.DISPATCHER.connect_strong(event.ProjectEditionEntered, self.on_project_edition_entered)

    # -- 後始末 ---------------------------------------------------------

    def shutdown(self):
        """イベント購読を解除し、タイマーを停止する。
        connect_strong() は強参照でコールバックを保持し自動解放されない
        ため、close_plugin() から明示的に本メソッドを呼ばないと、
        プラグインの無効化→再有効化の際に古いエンジンのハンドラが残り、
        イベントが多重発火する(同期処理が重複実行される)。公式サンプルに
        倣い、接続時と同じ (event_cls, callback) の組で disconnect する。

        2026.07.19-01: 従来はここで self.debounce_timer のみ停止して
        いたが、on_project_saved() 由来の遅延Final書き出しが匿名の
        QTimer.singleShot() で仕込まれていたため、shutdown() 実行後も
        キャンセルされずに残り、SP終了処理と重なって発火することで
        ウィンドウを閉じた際のクラッシュレポート増加に関与していたと
        考えられる。self._shutting_down フラグを最初に立てて以降の
        新規スケジューリングを止めた上で、既にスケジュール済みの
        self._final_export_timer も明示的に停止する。
        """
        self._shutting_down = True
        try:
            self._rebase_timer.stop()
        except Exception:
            pass
        try:
            self._rebase_timer.timeout.disconnect(self._on_rebase_timer)
        except Exception:
            pass
        try:
            if self._rebase is not None:
                self._rebase.shutdown()
        except Exception as exc:
            _log("warning", "rebase 司令塔の後始末に失敗しました: {0}".format(exc))
        self._break_rebase_cycles()
        pairs = [
            (event.TextureStateEvent, self.on_texture_state),
            (event.ProjectSaved, self.on_project_saved),
            (event.ProjectAboutToClose, self.on_project_closing),
            (event.ProjectEditionEntered, self.on_project_edition_entered),
        ]
        for evt_cls, cb in pairs:
            try:
                event.DISPATCHER.disconnect(evt_cls, cb)
                _log("info", "イベント購読を解除しました({0})".format(
                    getattr(evt_cls, "__name__", evt_cls)))
            except Exception as e:
                _log("warning", "イベント購読解除に失敗しました({0}): {1}".format(
                    getattr(evt_cls, "__name__", evt_cls), e))
        for timer in (
            self.debounce_timer,
            self._final_export_timer,
            self._mesh_record_timer,
            self._preview_migration_timer,
        ):
            try:
                timer.stop()
            except Exception:
                pass

    def _break_rebase_cycles(self):
        """engine とフック・API の相互参照を切る。司令塔自体は残す。"""
        api = self._rebase_api
        coord = self._rebase
        if api is not None:
            api.closed = True
            api.engine = None
        if coord is not None:
            hooks = getattr(coord, "hooks", None)
            if hooks is not None:
                hooks.engine = None
            held = getattr(coord, "api", None)
            if held is not None and held is not api:
                held_engine = getattr(held, "engine", None)
                if held_engine is self:
                    held.engine = None

    # -- 設定 -----------------------------------------------------------

    def reload_config(self):
        self.config = load_config()
        self._emit_status("設定を再読込しました。")

    def apply_and_save_config(self, new_values):
        """ユーザー操作(フォルダ変更等)による保存。変更されたキーだけを
        ディスクに反映する(save_config()のマージ仕様と合わせ、Maya側が
        書き込んだ値を巻き戻さないようにするため)。
        """
        self.config.update(new_values)
        save_config(new_values)
        self._emit_status("設定を保存しました。")

    def _emit_status(self, text):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = "[{0}] {1}".format(stamp, text)
        _log("info", text)
        if not self._shutting_down:
            self.status_changed.emit(line)
        _append_history("SP", text)

    # -- 有効/無効 --------------------------------------------------------

    def set_enabled(self, value):
        self.enabled = bool(value)
        self._emit_status("Live Sync {0}".format("有効化" if self.enabled else "無効化"))
        if self.enabled and project.is_open():
            self.debounce_timer.start(200)
            self._missing_streak = {}
            self.check_texture_set_structure()

    # -- イベントハンドラ --------------------------------------------------

    def start_rebase_watch(self):
        """1 秒タイマーを開始する。rebase_enabled が偽でも動かす。"""
        if self._shutting_down:
            return
        try:
            self._rebase_timer.start(1000)
        except Exception as exc:
            _log("warning", "rebase タイマーの開始に失敗しました: {0}".format(exc))

    def _on_rebase_timer(self):
        if self._shutting_down:
            return
        if self._rebase_tick_guard:
            return
        self._rebase_tick_guard = True
        try:
            if self._rebase is not None:
                self._rebase.tick()
        finally:
            self._rebase_tick_guard = False

    def _on_preview_migration_timer(self):
        if self._shutting_down:
            return
        self._safe_export(preview=True, force_full=True)

    def run_rebase_final_export(self, request_id):
        """リベース用の全量 Final。Live Sync の有効/無効は見ない。"""
        empty = ExportAttempt(executed=False, ok=False, message="skipped")
        try:
            if not project.is_open() or project.is_busy() or self.exporting:
                self._last_export_attempt = empty
                return empty
        except Exception:
            self._last_export_attempt = empty
            return empty

        def _capture_locked(_error):
            return None

        self._safe_export(
            preview=False,
            _on_project_locked=_capture_locked,
            force_full=True,
            export_request_id=request_id,
        )
        return self._last_export_attempt or empty

    def _emit_rebase_block_status(self):
        if self._rebase_block_status_emitted:
            return
        self._rebase_block_status_emitted = True
        self._emit_status(REBASE_EXPORT_DEFERRED_STATUS)

    def _set_rebase_blocks_exports(self, value):
        self._rebase_blocks_exports = bool(value)
        if not value:
            self._rebase_block_status_emitted = False

    def _resume_blocked_exports(self):
        if self._shutting_down:
            return
        if self.pending_final:
            self._request_final_export(_FINAL_EXPORT_QUEUED_DELAY_MS)
        elif self.pending_reexport:
            try:
                self.debounce_timer.start(200)
            except Exception:
                pass

    def on_texture_state(self, evt):
        if self._shutting_down:
            return
        self._last_texture_event_at = time.time()
        if not self.enabled:
            return
        if evt.action not in (
            event.TextureStateEventAction.ADD,
            event.TextureStateEventAction.UPDATE,
        ):
            return
        self.dirty_stack_ids.add(evt.stack_id)
        # 2026.07.19-03(フェーズ2): Preview用集合とは別に、Final専用の
        # 差分集合にも同じstack_idを積む(Previewが先に消費しても
        # Final側は影響を受けない)。
        self.dirty_stack_ids_final.add(evt.stack_id)
        interval_ms = int(float(self.config.get("debounce_seconds", 1.5)) * 1000)
        self.debounce_timer.start(interval_ms)

    def on_project_saved(self, evt):
        if self._shutting_down:
            return
        # 2026.07.16(緊急修正): on_project_edition_entered() は
        # プロジェクトを開いた/作成した"その瞬間"の _current_project_key()
        # しか記録しない。テンプレートから新規プロジェクトを作成した
        # 直後はまだ未保存("__unsaved__")のため、その後実際に名前を
        # 付けて保存しても、active_project_key はここを更新する処理が
        # 無ければ "__unsaved__" のまま(または不正確な値のまま)取り
        # 残される。
        # 一方 _do_export() 内の active_watch_subfolder は、エクスポート
        # のたびに _project_subfolder_name() 経由で _current_project_key()
        # を都度再計算していたため、保存後は正しいプロジェクト名を
        # 反映していた。この非対称性により、「監視フォルダ名は正しい
        # プロジェクト名になっているのに、Maya側の状態バー(紐付け表示)
        # だけがテンプレート名や__unsaved__のまま」という矛盾が
        # 実機で確認された。
        # 対策として、保存イベントのタイミングでも active_project_key を
        # 明示的に再計算・保存するようにした。
        old_key = self.config.get("active_project_key")
        key = _current_project_key()
        key_migrated = False
        if old_key != key:
            self.config["active_project_key"] = key
            save_config({"active_project_key": key})
            # 2026.07.25(緊急バグ修正、D-2): 「名前を付けて保存」で
            # project_keyが暫定キー("__unsaved__"、またはテンプレート
            # 漏れの.sptパス)から実プロジェクトの.sppパスへ切り替わった
            # 場合、*_by_project辞書に暫定キーの下で既に記録されていた
            # 実績(サブフォルダ・既知テクスチャセット等)を新キーへ
            # 引き継がないと、Maya側から見て「Finalは存在するのにLiveが
            # 見つからない」ような非対称な状態になる(暫定キー配下の
            # 実データが、誰からも参照されない新キーの下に取り残される
            # ため)。詳細は _migrate_project_key_entries() 参照。
            is_transitional_old = old_key == "__unsaved__" or (
                isinstance(old_key, str) and old_key.lower().endswith(".spt")
            )
            if is_transitional_old and key and key != "__unsaved__":
                key_migrated = self._migrate_project_key_entries(old_key, key)
                # 2026.10.18(1.3.0): 未保存のまま開いたプロジェクトは
                # ProjectEditionEntered では記録しない。名前を付けて保存
                # したあと、キー移行の次にメッシュを取り直す。
                self._schedule_mesh_record()

        if not self.enabled:
            return

        if key_migrated:
            # 移行後はPreview/Finalとも「暫定キーの下で書き出した実績」を
            # 引き継いだだけで、新キーのサブフォルダには実ファイルが
            # 存在しない(ファイルの移動はせず、辞書のキー名だけを
            # 差し替える設計のため、詳細は _migrate_project_key_entries()
            # 参照)。両方を強制全量再書き出しして実体を追いつかせる。
            self._emit_status(
                "「名前を付けて保存」を検出しました。保存前の作業内容を"
                "正しいプロジェクト名へ引き継ぐため、Preview/Finalの両方を"
                "全量で再書き出しします。"
            )
            # 2026.07.25: Final側と同じ理由(on_project_saved()直後は
            # Painter内部の保存ロックが残っている場合がある、上記
            # 2026.07.19-01のコメント参照)で、Preview側もここで即座に
            # 呼ばず _FINAL_EXPORT_INITIAL_DELAY_MS だけ遅延させる。
            # Finalのような指数バックオフ再試行までは行わないが
            # (Save-Asはまれなイベントであり、失敗時も_safe_export()の
            # 既存の仕組みで「プロジェクトがロック中のため書き出せません
            # でした」と状態バーに表示されるため、ユーザーは「今すぐ同期」
            # で手動リトライできる)、shutdown()後に発火しても実処理へ
            # 進まないようガードする。
            try:
                self._preview_migration_timer.start(_FINAL_EXPORT_INITIAL_DELAY_MS)
            except Exception as exc:
                _log("warning", "Save As 後の Preview 予約に失敗しました: {0}".format(exc))
            self._request_final_export(0, bypass_cooldown=True, force_full=True)
            return

        # 2026.07.19-01: 従来は固定500ms待機(QTimer.singleShot)で
        # Painter内部の保存ロック解除を待っていたが、(a) shutdown()側で
        # キャンセルできず終了処理と重なるリスクがあり、(b) 500msという
        # 数値自体、環境によって足りない/長すぎることがあった
        # (BusyStatusChanged/is_busy()では保存直後のロック状態を検知
        # できないことをAdobe公式コミュニティでも確認済み)。
        # 名前付きQTimerと指数バックオフ再試行(_request_final_export)に
        # 置き換える。
        self._request_final_export(_FINAL_EXPORT_INITIAL_DELAY_MS)

    def _migrate_project_key_entries(self, old_key, new_key):
        """2026.07.25(緊急バグ修正、D-2)で新設。

        watch_subfolder_by_project/final_subfolder_by_project/
        known_texture_sets_by_project/texture_set_export_prefix_by_project
        /painter_mesh_by_project
        の辞書について、old_key(暫定キー)のエントリが存在すれば
        new_key(実プロジェクトの.sppパス)へ付け替える(old_keyのエントリは
        削除)。書き出し済みファイルそのものはフォルダ間で移動しない
        (移動は途中失敗時の一貫性が担保できず、Maya側のポーリングとも
        競合するため。呼び出し元がこの後に強制全量再書き出しを行うことで
        実体を揃える設計)。

        戻り値: 1件以上のエントリを移行した場合True、old_keyが
        どの辞書にも存在しなかった(＝暫定キーの下でまだ何も書き出して
        いなかった)場合はFalse。
        """
        by_project_keys = (
            "watch_subfolder_by_project",
            "final_subfolder_by_project",
            "known_texture_sets_by_project",
            "texture_set_export_prefix_by_project",
            # 2026.10.18(1.3.0): 読み込んだメッシュの記録も暫定キーから
            # 実プロジェクトのキーへ付け替える。
            "painter_mesh_by_project",
        )
        update = {}
        migrated_any = False
        for map_key in by_project_keys:
            by_project = dict(self.config.get(map_key, {}) or {})
            if old_key in by_project:
                by_project[new_key] = by_project.pop(old_key)
                update[map_key] = by_project
                migrated_any = True
        if update:
            self.config.update(update)
            save_config(update)
        return migrated_any

    def on_project_closing(self, evt):
        if self._shutting_down:
            try:
                if self._rebase is not None:
                    self._rebase.on_project_closing()
            except Exception:
                pass
            return
        try:
            if self._rebase is not None:
                self._rebase.on_project_closing()
        except Exception as exc:
            _log("warning", "rebase のプロジェクト閉鎖処理に失敗しました: {0}".format(exc))
        self.debounce_timer.stop()
        # Phase 2 最適化: プロジェクトが切り替わるので、猶予カウンタを
        # リセットしておく(次のプロジェクトの検出に前プロジェクトの
        # 状態を持ち越さないため)。
        self._missing_streak = {}
        # 2026.07.19-03(フェーズ2): 差分書き出し用の状態もプロジェクト
        # 単位でリセットする。これを怠ると、別プロジェクトに切り替えた
        # 直後の最初のFinal書き出しが、前のプロジェクトのbaseline済み
        # フラグのせいで誤って差分扱いになり、大部分のテクスチャセットが
        # 書き出されないまま「完了」してしまう恐れがある。
        self.dirty_stack_ids_final = set()
        self.final_baseline_established = False
        self._final_export_force_full = False
        # 2026.07.24-02: 前プロジェクトでのリトライ打ち切り後フォローアップ
        # 予約状態を次のプロジェクトへ持ち越さないようにリセットする。
        self._final_export_giveup_followup_pending = False
        # 2026.07.23(緊急修正: Preview差分集合のプロジェクト間持ち越し):
        # 上記のdirty_stack_ids_final同様、Preview用のself.dirty_stack_ids
        # もプロジェクト単位でリセットする必要があったが、これまで漏れて
        # いた。stack_idはPainter内部でドキュメントをまたいで一意である
        # 保証が無いため、直前の編集(debounce_timer始動直後)を残したまま
        # プロジェクトを切り替えると、上のdebounce_timer.stop()で
        # 予定されていたrequest_export()自体はキャンセルされる一方、
        # dirty_stack_idsの中身は消費されずに残ってしまう。次のプロジェクト
        # で_resolve_stack_names()がこの古いstack_idを(たまたま新ドキュ
        # メント側でも有効なidとして)解決できてしまった場合、_build_
        # export_config()のPreview差分フィルタが誤った/無関係なテクスチャ
        # セット名で汚染され、実際に編集したテクスチャセットが対象から
        # 漏れる、または無関係なセットが余分に対象になる恐れがある。
        # dirty_stack_idsが既に空の場合はresetしても単なる無害な
        # no-opなので、常に無条件でクリアする。
        self.dirty_stack_ids = set()
        # 2026.07.19-04(フェーズ3): _unchanged()用のキャッシュ
        # (last_hashes/last_sizes)はdest_pathがプロジェクト別サブ
        # フォルダを含む絶対パスのため、他プロジェクトのエントリは
        # 二度と参照されないまま増え続けるだけになる。長時間・複数
        # プロジェクトを横断するセッションでの緩やかなメモリ増加を
        # 防ぐため、プロジェクトを閉じるタイミングで刈り込む。次に
        # 同じプロジェクトへ戻ってきた際は、最初の1回だけ保守的に
        # 「変更あり」判定になる(実害はなく、安全側の挙動)。
        self.last_hashes = {}
        self.last_sizes = {}
        try:
            self._final_export_timer.stop()
        except Exception:
            pass
        # 2026.10.18(1.3.0): 閉じたあとにメッシュ記録が走ると、閉じて
        # いるプロジェクトのパスを書いてしまう。
        try:
            self._mesh_record_timer.stop()
        except Exception:
            pass
        try:
            self._preview_migration_timer.stop()
        except Exception:
            pass
        if self._shutting_down:
            return
        self.final_pending_changed.emit(False)
        # Phase 3: プロジェクトが閉じられたので、Maya側に「今SPで開いている
        # プロジェクトは無い」ことを伝える。
        self.config["active_project_key"] = None
        save_config({"active_project_key": None})

    def on_project_edition_entered(self, evt):
        if self._shutting_down:
            return
        try:
            if self._rebase is not None:
                self._rebase._recover_key = None
                self._rebase._precheck.clear()
        except Exception:
            pass
        # 新しいプロジェクト(または既存プロジェクト)を開いた直後に
        # 基準となるテクスチャセット構成を記録する。
        # Phase 3: どのプロジェクトを開いたかをMaya側と共有する
        # (複数プロジェクトを横断して作業する際の一覧混在を防ぐため)。
        key = _current_project_key()
        self.config["active_project_key"] = key
        save_config({"active_project_key": key})
        # 2026.07.19-03(フェーズ2): 新しいプロジェクトの編集に入るタイミング
        # でも念のためリセットする(on_project_closing()を経由しない
        # 遷移が万一あった場合の保険)。
        self.dirty_stack_ids_final = set()
        self.final_baseline_established = False
        # 2026.07.23(緊急修正): on_project_closing()と同じ理由で、Preview
        # 用のself.dirty_stack_idsもここで念のためリセットする(詳細は
        # on_project_closing()内の同日コメント参照)。
        self.dirty_stack_ids = set()
        # 2026.07.19-04(フェーズ3): 同上の理由でキャッシュも念のため
        # ここでも刈り込む。
        self.last_hashes = {}
        self.last_sizes = {}
        if self.enabled:
            self._missing_streak = {}
            self.check_texture_set_structure()
        # 2026.10.18(1.3.0): 編集に入った直後はメッシュパスがまだ空の
        # ことがあるため、名前付きタイマーで遅延して記録する。
        # 未保存キーでは _record_painter_mesh() が何もしない。
        # 待ち時間でパスが取れるかは要実機確認。
        self._schedule_mesh_record()

    def _schedule_mesh_record(self):
        if self._shutting_down:
            return
        self._mesh_record_retries = 0
        try:
            self._mesh_record_timer.start(_MESH_RECORD_DELAY_MS)
        except Exception as exc:
            _log("warning", "メッシュ記録の予約に失敗しました: {0}".format(exc))

    def _on_mesh_record_timer(self):
        if self._shutting_down:
            return
        try:
            outcome = self._record_painter_mesh()
        except Exception as exc:
            _log("warning", "読み込んだメッシュの記録に失敗しました(既存の記録は残します): {0}".format(exc))
            return
        if outcome != "retry":
            return
        if self._mesh_record_retries >= _MESH_RECORD_MAX_RETRIES:
            _log(
                "info",
                "読み込んだメッシュは記録しませんでした(再試行後もパスが空、"
                "または Painter が busy です)。既存の記録は残します。",
            )
            return
        self._mesh_record_retries += 1
        try:
            self._mesh_record_timer.start(_MESH_RECORD_RETRY_DELAY_MS)
        except Exception as exc:
            _log("warning", "メッシュ記録の再試行を予約できませんでした: {0}".format(exc))

    def _record_painter_mesh(self):
        """2026.10.18(1.3.0): Painter が読み込んでいるメッシュを共有設定へ書く。

        パスは project.last_imported_mesh_path()(12.1.5 で存在を確認済み)。
        関数が無い・例外のときは記録しない。既存の値も消さない。
        パスが空、または project.is_busy() が真のときは "retry" を返す。
        呼び出し側が 500ms 後に最大 2 回だけ再試行する(sleep はしない)。
        未保存プロジェクト(__unsaved__ 系)では記録しない。
        要実機確認: 12.0.0 にこの関数があるか、再試行でパスが埋まるか。
        """
        try:
            if not project.is_open():
                return None
        except Exception:
            return None
        key = _current_project_key()
        if _is_unsaved_project_key(key):
            return None
        getter = getattr(project, "last_imported_mesh_path", None)
        if not callable(getter):
            if not self._mesh_api_missing_logged:
                self._mesh_api_missing_logged = True
                _log(
                    "warning",
                    "project.last_imported_mesh_path が無いため、読み込んだメッシュは"
                    "記録しません。要実機確認: Painter 12.0.0 では未提供の可能性が"
                    "あります(12.1.5 では存在を確認済み)。",
                )
            return None
        try:
            busy = bool(project.is_busy())
        except Exception:
            busy = False
        if busy:
            return "retry"
        try:
            mesh_path = getter()
        except Exception as exc:
            _log("warning", "読み込んだメッシュのパス取得に失敗したため記録しません(既存の記録は残します): {0}".format(exc))
            return None
        if not isinstance(mesh_path, str) or mesh_path == "":
            return "retry"
        by_project = self.config.get("painter_mesh_by_project")
        prev = by_project.get(key) if isinstance(by_project, dict) else None
        try:
            record = build_mesh_record(mesh_path, prev, time.time())
        except Exception as exc:
            _log("warning", "メッシュの記録を作れなかったため、既存の記録は残します: {0}".format(exc))
            return
        if record is None:
            _log("warning", "メッシュファイルを記録できなかったため、既存の記録は残します。")
            return
        written = _update_nested_config_entry("painter_mesh_by_project", key, record)
        if written is None:
            return
        self.config["painter_mesh_by_project"] = written

    # -- Phase 2: テクスチャセット構造の変化検出 -----------------------------

    def check_texture_set_structure(self):
        """現在のテクスチャセット一覧を、同一プロジェクトについて前回記録
        した一覧と比較する。追加は即時反映するが、削除(リネームは削除+
        追加として現れる)は2回連続で検出された場合のみ確定させ、
        ベイク処理中などの一時的なちらつきによる誤検知を防ぐ。
        """
        if not project.is_open():
            return
        # busy中(ベイク等)は一覧が一時的に不完全なことがあるためスキップする。
        if project.is_busy():
            return

        try:
            current = set(ts.name() for ts in textureset.all_texture_sets())
        except Exception as e:
            self._emit_status("テクスチャセット一覧の取得に失敗しました: {0}".format(e))
            return

        key = _current_project_key()
        by_project = self.config.get("known_texture_sets_by_project", {})
        known = set(by_project.get(key, []))

        added = current - known
        missing_now = known - current

        # 消失候補の猶予カウントを更新
        confirmed_removed = []
        for name in list(self._missing_streak.keys()):
            if name not in missing_now:
                # 復活した(=誤検知だった)ので猶予カウンタを消す
                del self._missing_streak[name]
        for name in missing_now:
            self._missing_streak[name] = self._missing_streak.get(name, 0) + 1
            if self._missing_streak[name] >= 2:
                confirmed_removed.append(name)

        changed = bool(added) or bool(confirmed_removed) or (key not in by_project)

        if changed:
            if added:
                self._emit_status("新しいテクスチャセットを検出: {0}".format(", ".join(sorted(added))))
            if confirmed_removed:
                self._emit_status(
                    "テクスチャセットが削除/リネームされた可能性があります: {0}".format(
                        ", ".join(sorted(confirmed_removed))
                    )
                )
            new_known = (known | added) - set(confirmed_removed)
            by_project[key] = sorted(new_known)
            self.config["known_texture_sets_by_project"] = by_project
            save_partial = {"known_texture_sets_by_project": by_project}

            # Phase 3 最適化: 削除が確定したテクスチャセットについては、
            # texture_set_export_prefix_by_project に残ったままだと不要な
            # エントリが溜まり続けるため、あわせて掃除する(実害は無いが、
            # 設定ファイルが際限なく肥大化するのを防ぐ)。
            # 2026.07.15-01: プロジェクトキーでネストした構造に変更した
            # ため、対象は by_project[key] のみに限定する(他プロジェクトの
            # 同名エントリを誤って消さないため)。
            if confirmed_removed:
                prefix_by_project = dict(self.config.get("texture_set_export_prefix_by_project", {}))
                prefix_map = dict(prefix_by_project.get(key, {}))
                removed_any_prefix = False
                for name in confirmed_removed:
                    if name in prefix_map:
                        del prefix_map[name]
                        removed_any_prefix = True
                if removed_any_prefix:
                    prefix_by_project[key] = prefix_map
                    self.config["texture_set_export_prefix_by_project"] = prefix_by_project
                    save_partial["texture_set_export_prefix_by_project"] = prefix_by_project

            save_config(save_partial)
            for name in confirmed_removed:
                self._missing_streak.pop(name, None)

        self.structure_changed.emit(
            sorted(current), sorted(added), sorted(confirmed_removed)
        )

    # -- 書き出しトリガー ---------------------------------------------------

    def request_export(self):
        if not self.enabled or not project.is_open():
            return
        self._safe_export(preview=True)

    def force_sync_now(self):
        if not project.is_open():
            self._emit_status("プロジェクトが開かれていません。")
            return
        self._safe_export(preview=True)

    def force_full_final_sync(self):
        """2026.07.19-03(フェーズ2): 「今すぐ全量同期」ボタンから呼ばれる。
        差分書き出しの状態がMaya側の実際のファイルとズレてしまった場合の
        手動リセット手段として、クールダウンを無視して即座に全量の
        Final書き出しを要求する。
        """
        if not project.is_open():
            self._emit_status("プロジェクトが開かれていません。")
            return
        self._emit_status("手動操作: Final全量同期を要求しました。")
        self._request_final_export(0, bypass_cooldown=True, force_full=True)

    # -- 2026.07.19-01/03: 保存直後のFinal書き出し(再試行・クールダウン付き遅延実行) --

    def _request_final_export(self, delay_ms, bypass_cooldown=False, force_full=False):
        """delay_ms後にFinal書き出しを試みるようスケジュールする。
        on_project_saved() と、_safe_export() の finally節(保存連打で
        キューされた再実行)、force_full_final_sync() の3箇所から呼ばれる
        単一の入口。
        ProjectError(プロジェクトロック中)で失敗した場合は
        _handle_final_export_locked() が指数バックオフで再スケジュール
        する。

        2026.07.19-03(フェーズ2):
        - bypass_cooldown=False(既定)の場合、前回のFinal書き出し完了
          からfinal_cooldown_seconds未満しか経っていなければ、実行を
          その残り時間まで遅らせる(保存連打の間引き)。手動の全量同期
          (force_full_final_sync)はTrueを渡してこれを無視する。
        - force_full=Trueが渡された場合のみ self._final_export_force_full
          をTrueにする(Falseを渡しても既存のTrueは消さない。busy中の
          衝突で後から再スケジュールされた際に、force_full要求が
          消えてしまわないようにするため)。
        """
        if self._shutting_down:
            return
        self._final_export_retry_attempt = 0
        # 2026.07.24-02: これは新規の要求(保存/手動全量同期/保存連打の
        # 再実行)なので、前回サイクルのリトライ打ち切り後フォローアップ
        # 予約状態を持ち越さず、新しいサイクルとして仕切り直す。
        self._final_export_giveup_followup_pending = False
        if force_full:
            self._final_export_force_full = True

        effective_delay = float(delay_ms)
        if not bypass_cooldown and self._last_final_completed_at is not None:
            cooldown_ms = float(self.config.get("final_cooldown_seconds", 5.0)) * 1000.0
            elapsed_ms = (time.time() - self._last_final_completed_at) * 1000.0
            remaining_ms = cooldown_ms - elapsed_ms
            if remaining_ms > effective_delay:
                effective_delay = remaining_ms

        self._final_export_timer.start(max(0, int(effective_delay)))
        self.final_pending_changed.emit(True)

    def _on_final_export_timer_fired(self):
        if self._shutting_down:
            return
        self._safe_export(
            preview=False,
            _on_project_locked=self._handle_final_export_locked,
            force_full=self._final_export_force_full,
        )

    def _handle_final_export_locked(self, error):
        if self._shutting_down:
            return
        self._final_export_retry_attempt += 1
        if self._final_export_retry_attempt <= _FINAL_EXPORT_MAX_RETRIES:
            delay = min(
                _FINAL_EXPORT_RETRY_BASE_MS * (2 ** self._final_export_retry_attempt),
                _FINAL_EXPORT_RETRY_MAX_MS,
            )
            self._emit_status(
                "プロジェクトがロック中のため{0}ms後に再試行します({1}/{2}): {3}".format(
                    delay, self._final_export_retry_attempt, _FINAL_EXPORT_MAX_RETRIES, error))
            # 2026.07.19-03: クールダウンは初回要求時に既に考慮済みなので、
            # ロック解除待ちの再試行そのものはクールダウンを再適用しない
            # (でないと再試行のたびに待ち時間が余計に伸びてしまう)。
            self._final_export_timer.start(delay)
        elif not self._final_export_giveup_followup_pending:
            # 2026.07.24-02: いきなり諦めず、長めの遅延を置いて自動で
            # もう一度だけ試行する(保存直後ロックが通常より長引いた
            # 場合の自己回復)。force_fullの意図(全量書き出しが必要)は
            # ここでは取り下げない。実際に成功した時点で_do_export()側が
            # 改めてFalseへリセットする。二重スケジュール防止のガードを
            # 先に立ててからattemptをリセットする。
            self._final_export_giveup_followup_pending = True
            self._final_export_retry_attempt = 0
            self._emit_status(
                "警告: 保存直後のFinal書き出しがロック解除待ちで{0}回再試行しましたが"
                "諦めました。{1}秒後に自動でもう一度だけ試みます。".format(
                    _FINAL_EXPORT_MAX_RETRIES, _FINAL_EXPORT_GIVEUP_FOLLOWUP_MS // 1000))
            self._final_export_timer.start(_FINAL_EXPORT_GIVEUP_FOLLOWUP_MS)
        else:
            # フォローアップも力尽きた。無限ループを避けるためここで
            # 最終的に諦める。
            self._final_export_force_full = False
            self.final_pending_changed.emit(False)
            self._emit_status(
                "警告: 保存直後のFinal書き出しが自動フォローアップも含めて"
                "ロック解除待ちで諦めました。「今すぐ全量同期」ボタンから"
                "手動で再試行できます。")

    def _resume_safe_export_after_busy(self, preview, on_locked, force_full, export_request_id):
        """busy 解除後の再開。引数は呼び出しごとの partial が持つ。"""
        if self._shutting_down:
            return
        self._safe_export(
            preview=preview,
            _on_project_locked=on_locked,
            force_full=force_full,
            export_request_id=export_request_id,
        )

    def _safe_export(self, preview=True, _on_project_locked=None, force_full=False, export_request_id=None):
        """
        _on_project_locked: 省略時(None)は従来通りの挙動。
        substance_painter.exception.ProjectError が発生した場合も他の
        例外と同様にログへ出して握りつぶし、何もしない。
        コールバックを渡した場合のみ、ProjectError発生時にそのコール
        バックを呼び出す(呼び出し元が再試行するかどうかを判断できる
        フック)。他の種類の例外は、コールバックの有無に関わらず従来通り
        握りつぶす。
        2026.07.19-01: request_export()/force_sync_now() など既存の
        呼び出し元は _on_project_locked を渡さないため、挙動は完全に
        従来通りである。新しいリトライ経路(_on_final_export_timer_fired)
        だけがこの引数を利用する。

        force_full: 2026.07.19-03(フェーズ2)。Trueの場合、Final書き出し
        (preview=False)で差分の有無に関わらず全テクスチャセットを
        書き出す。「今すぐ全量同期」ボタン、および差分書き出しの基準が
        まだ確立していない(このセッションで一度もFinalに成功していない)
        場合の自動フォールバックで使われる。
        export_request_id: 2026.10.19(1.4.0 PB-b)。リベースの全量 Final で
        結果ファイルへ依頼 ID を最初から入れる。1.3.0 の呼び出しは None。
        """
        self._last_export_attempt = None
        skipped = ExportAttempt(executed=False, ok=False, message="skipped")
        if self._shutting_down:
            self._last_export_attempt = skipped
            return skipped
        if self._rebase_blocks_exports and export_request_id is None:
            if preview:
                self.pending_reexport = True
            else:
                self.pending_final = True
            self._emit_rebase_block_status()
            self._last_export_attempt = skipped
            return skipped
        if not project.is_open():
            self._last_export_attempt = skipped
            return skipped
        if project.is_busy():
            self._emit_status("Painterがbusyのため、解除後に再試行します。")
            project.execute_when_not_busy(functools.partial(
                self._resume_safe_export_after_busy,
                preview,
                _on_project_locked,
                force_full,
                export_request_id,
            ))
            self._last_export_attempt = skipped
            return skipped
        if self.exporting:
            self.pending_reexport = True
            if not preview:
                self.pending_final = True
            self.debounce_timer.start(300)
            self._last_export_attempt = skipped
            return skipped

        dirty_ids = self.dirty_stack_ids
        self.dirty_stack_ids = set()

        self.exporting = True
        self.exporting_changed.emit(True)
        t0 = time.time()
        locked_error = None
        # 2026.10.18(1.3.0): 同期回数は開始時ではなく、1件以上置き換えた
        # 書き出しが終わったときに増やす。早期スキップ・キャンセル・
        # 全部スキップでは増やさない。
        self._export_moved_count = 0
        self._export_last_meta = None
        other_error = None
        try:
            self._do_export(
                preview=preview,
                dirty_stack_ids=dirty_ids,
                force_full=force_full,
                export_request_id=export_request_id,
            )
        except sp_exception.ProjectError as e:
            self.stats["error_count"] += 1
            locked_error = e
            if _on_project_locked is None:
                self._emit_status("エラー: プロジェクトがロック中のため書き出せませんでした ({0})".format(e))
        except Exception as e:
            self.stats["error_count"] += 1
            other_error = e
            self._emit_status("エラー: {0}".format(e))
        finally:
            # 2026.07.19-02: self.exporting=False をここ(finally先頭)では
            # なく後段に移した。以前はここで先にFalseへ戻していたため、
            # 「exportingフラグを見て書き出し中を警告する」という仕組みを
            # 仮に用意しても、この後まだ実行される後処理(check_texture_
            # set_structure()等、Painter APIを追加で叩く処理)の間は
            # 警告が消えてしまい、実際に危険な区間をカバーできていな
            # かった。exporting_changed シグナルをここで一度emitし、
            # 「後処理はまだ続いている」ことをUI側が正しく把握できる
            # ようにする(_on_export_finished_uiのような明示的な完了通知
            # は、後処理が全て終わった後、finallyの最後で行う)。
            if self._export_moved_count >= 1:
                self.stats["sync_count"] += 1
            self.stats["last_duration_ms"] = int((time.time() - t0) * 1000)
            self.stats["last_sync_at"] = datetime.datetime.now().strftime("%H:%M:%S")
            self.stats_changed.emit(dict(self.stats))
            if self.pending_final:
                # 高画質書き出しの要求を優先して処理する(プレビューに
                # すり替わって消えてしまわないようにするため)。
                self.pending_reexport = False
                self.pending_final = False
                # 2026.07.19-01: 匿名singleShotから、shutdown()で停止
                # できる名前付きタイマー経由の呼び出しへ変更。
                self._request_final_export(_FINAL_EXPORT_QUEUED_DELAY_MS)
            elif self.pending_reexport:
                self.pending_reexport = False
                self.debounce_timer.start(200)
            # 2026.07.19-02(クラッシュ再現テストを受けた追加対応):
            # このcheck_texture_set_structure()自体もPainter APIを叩く
            # 処理であり、Final書き出し直後の「まだ危険な後処理区間」の
            # 末尾に位置していた。Preview側の同期はテクスチャ編集の
            # たびに高頻度で走り、その都度この構造検知も行われているため、
            # Final書き出し直後に限ってはここを省略しても構成一覧の
            # 鮮度が実用上大きく落ちることはないと判断し、preview=True
            # の場合のみ呼ぶよう変更した。
            if preview:
                self.check_texture_set_structure()
            # 2026.07.19-02: ここまで全ての後処理が終わった時点で初めて
            # exporting=Falseにする(上記コメント参照)。
            self.exporting = False
            self.exporting_changed.emit(False)

        # 2026.07.19-01: locked_errorはfinally節の後(状態管理が全て完了
        # した後)に呼び出し元へ通知する。呼び出し元が渡したコールバック
        # の中身(再試行のスケジューリング等)がここでの後始末と競合
        # しないようにするため。
        meta = self._export_last_meta or {}
        self._last_export_attempt = ExportAttempt(
            executed=True,
            ok=bool(meta.get("ok")) and locked_error is None and other_error is None,
            status=meta.get("status"),
            export_id=meta.get("export_id"),
            message=meta.get("message") or "",
            locked_error=locked_error,
            error=other_error,
        )
        if locked_error is not None and _on_project_locked is not None:
            _on_project_locked(locked_error)
        return self._last_export_attempt

    def _export_status_of(self, result):
        """ExportStatus を結果ファイル用の名前にする。

        2026.10.18(1.3.0): 名前が取れるときは export_status_name()。
        既存の ExportStatus との == が取れたときはそちらを優先する
        (従来のキャンセル・成功判定と同じ)。未知は error。
        """
        status_obj = getattr(result, "status", None)
        status_name = export_status_name(status_obj)
        try:
            if status_obj == export.ExportStatus.Cancelled:
                return "cancelled"
            if status_obj == export.ExportStatus.Success:
                return "success"
            if status_obj == export.ExportStatus.Warning:
                return "warning"
            if status_obj == export.ExportStatus.Error:
                return "error"
        except Exception:
            pass
        return status_name

    def _emit_export_outcome(self, preview, ok, status_name, moved_count, export_config, force_full):
        """完了文は ok のときだけ出す。件数は置き換えたファイル数。

        2026.10.18(1.3.0): Warning と Error は失敗として「完了しませんでした」
        と出す。キャンセルと例外は、既存のキャンセル文・エラー文と重複
        するので、ここでは出さない。プレビューの完了文は、従来どおり
        1 件以上動いた成功時だけ。
        """
        label = export_status_label(status_name)
        if preview:
            if ok and moved_count >= 1:
                self._emit_status("プレビュー同期完了({0}件更新)".format(moved_count))
            elif not ok and status_name not in ("cancelled", "exception"):
                self._emit_status("プレビュー同期は完了しませんでした(状態: {0})。".format(label))
            return
        if ok:
            set_count = 0
            if isinstance(export_config, dict):
                set_count = len(export_config.get("exportList", []))
            self._emit_status(
                "フル解像度の最終書き出しが完了しました({0}件、対象{1}テクスチャセット、{2})。".format(
                    moved_count,
                    set_count,
                    "全量" if (force_full or not self.final_baseline_established) else "差分",
                )
            )
            return
        if status_name in ("cancelled", "exception"):
            return
        self._emit_status("最終書き出しは完了しませんでした(状態: {0})。".format(label))

    def _do_export(self, preview=True, dirty_stack_ids=None, force_full=False, export_request_id=None):
        cfg = self.config

        # 2026.07.19-03(フェーズ2): Final専用の差分集合をこの時点で
        # スナップショットして即座にクリアする(Previewのdirty_stack_ids
        # と同じパターン)。書き出し中に新たな編集が入っても、そのぶんは
        # self.dirty_stack_ids_final に積まれ続け、次回のFinalへ正しく
        # 持ち越される。この書き出しが失敗した場合は、finally節で
        # このスナップショットを差し戻す(取りこぼしを防ぐため)。
        final_dirty_snapshot = None
        final_export_succeeded = False
        # 2026.10.18(1.3.0): 呼び出し直下でも 0 から始める。早期スキップは
        # ファイルを置かないので、この 0 のまま _safe_export へ戻る。
        self._export_moved_count = 0
        if not preview:
            final_dirty_snapshot = set(self.dirty_stack_ids_final)
            self.dirty_stack_ids_final = set()
            if not force_full and self.final_baseline_established and not final_dirty_snapshot:
                # 前回のFinal成功以降、テクスチャに変更が無い。書き出す
                # 対象が無いため、SPネイティブAPI(export_project_
                # textures())を一切呼ばずに即座に終了する。危険な後処理
                # 区間そのものに入らずに済む、フェーズ2で最も効果の
                # 大きい経路。
                self._emit_status("Final書き出し: 前回以降の変更が無いためスキップしました。")
                self.final_pending_changed.emit(False)
                return

        subfolder = _project_subfolder_name()
        # 2026.07.21(Phase 1): 以下のブロックすべてで project_key を
        # 使うため、_project_subfolder_name() 内部で計算済みの値を
        # 二重に計算しないよう、ここで一度だけ取得しておく。
        project_key = _current_project_key()
        if preview:
            # 複数プロジェクト対応(所有権問題の回避、2026.07.14-02):
            # 当初はライブプレビューを watch_dir 直下に一律で書き出して
            # いたが、共有PC環境では、過去に別のWindowsユーザーが
            # watch_dir 直下に残したファイルにNTFSの所有権(ACL)が付いて
            # おり、別ユーザーからは PermissionError で読み込めなくなる
            # 実害が確認された。Finalと同じ「プロジェクト別サブフォルダ」
            # 方式にすることで、常に自分が新規作成したフォルダ配下だけを
            # 使うことになり、この種の所有権衝突が原理上起こらなくなる。
            # 例: <watch_dir>/proA_a1b2c3/
            dest_root = os.path.join(cfg["watch_dir"], subfolder)
            # Maya 側(監視処理)が「現在アクティブなプロジェクトの監視先
            # サブフォルダ」を追従できるよう、共有設定に記録する。
            # 2026.07.21(Phase 1, 複数プロジェクト並行対応の根治):
            # 従来はこの active_watch_subfolder(スカラー値)しか記録して
            # おらず、複数のSPプロジェクトを行き来しながら作業すると
            # 後から開いた方の値で前の方が上書きされ、Maya側が同時に
            # 追跡できるプロジェクトが実質1つに限定される不具合が
            # あった。既存の active_watch_subfolder は後方互換のため
            # 引き続き更新しつつ(未対応の古いMaya側・aiSSの代表値用)、
            # 新たに project_key をキーとしたネスト辞書
            # (watch_subfolder_by_project)へも記録する。Maya側の
            # 監視・reload処理はこちらの辞書を正として複数プロジェクトを
            # 並行追跡できるようになる。
            if cfg.get("active_watch_subfolder") != subfolder:
                cfg["active_watch_subfolder"] = subfolder
                save_config({"active_watch_subfolder": subfolder})
            watch_by_project = dict(cfg.get("watch_subfolder_by_project", {}))
            if watch_by_project.get(project_key) != subfolder:
                watch_by_project[project_key] = subfolder
                cfg["watch_subfolder_by_project"] = watch_by_project
                save_config({"watch_subfolder_by_project": watch_by_project})
        else:
            # Final は複数プロジェクトが同じフォルダに混在・上書きするのを
            # 防ぐため、プロジェクトごとのサブフォルダへ書き出す。
            # 例: <final_export_dir>/proA_a1b2c3/
            dest_root = os.path.join(cfg["final_export_dir"], subfolder)
            # Maya 側(aiSSボタン)が「現在アクティブなプロジェクトのFinal
            # フォルダ」を自動入力できるよう、サブフォルダ名を共有設定に記録。
            # 2026.07.21(Phase 1): Preview側と同じ理由で、project_key を
            # キーとしたネスト辞書(final_subfolder_by_project)へも記録する。
            if cfg.get("active_final_subfolder") != subfolder:
                cfg["active_final_subfolder"] = subfolder
                save_config({"active_final_subfolder": subfolder})
            final_by_project = dict(cfg.get("final_subfolder_by_project", {}))
            if final_by_project.get(project_key) != subfolder:
                final_by_project[project_key] = subfolder
                cfg["final_subfolder_by_project"] = final_by_project
                save_config({"final_subfolder_by_project": final_by_project})
        stage_root = cfg["staging_dir"]
        # 2026.10.18(1.3.0): ここまでで dest_root は決まっている。
        # これより前の失敗(変更なしスキップ、フォルダ名の計算前)では
        # 結果ファイルを書かない。ここから先の成功・Warning・Error・
        # キャンセル・例外では、印と同じフォルダへ _sync_result.json を
        # 書く。印を書く条件(1件以上移動)と中身は変えない。結果ファイル
        # を印より先に書く。要実機確認: Maya 側が印の mtime を見た時点で
        # 結果ファイルが読めているか。
        moved_count = 0
        skipped_unchanged_count = 0
        replaced_texture_sets = set()
        tmp_dir = None
        started_at = time.time()
        export_id = uuid.uuid4().hex
        export_config = {}
        outcome_status = "exception"
        outcome_message = ""
        error = None
        published = False
        try:
            os.makedirs(stage_root, exist_ok=True)
            os.makedirs(dest_root, exist_ok=True)

            tmp_dir = tempfile.mkdtemp(prefix="sp_live_", dir=stage_root)
            # Phase 3: 実際に書き出したファイル名から、テクスチャセット名 ->
            # ファイル名prefix の対応を逆算して記録する(_safe_name() による
            # Maya側の予測とのズレを無くすため)。
            export_prefix_updates = {}
            try:
                export_config = self._build_export_config(
                    tmp_dir, preview, dirty_stack_ids, final_dirty_snapshot, force_full)
                result = export.export_project_textures(export_config)
                status_name = self._export_status_of(result)
                raw_message = getattr(result, "message", None)
                if isinstance(raw_message, str):
                    message = raw_message
                elif raw_message is None:
                    message = ""
                else:
                    message = str(raw_message)

                if status_name == "cancelled":
                    self._emit_status("書き出しがキャンセルされました。")
                else:
                    if status_name != "success":
                        self._emit_status("書き出しステータス: {0}".format(result.message))

                    for key_tuple, files in result.textures.items():
                        # result.textures のキーは (テクスチャセット名, スタック名)
                        # のタプル(Painter公式APIドキュメントで確認済み)。
                        texture_set_name = None
                        if isinstance(key_tuple, (tuple, list)) and key_tuple:
                            texture_set_name = key_tuple[0]
                        replaced_this_set = False
                        for src in files:
                            if not os.path.isfile(src):
                                continue
                            dest = os.path.join(dest_root, os.path.basename(src))
                            if self._unchanged(src, dest):
                                self.stats["skip_count"] += 1
                                skipped_unchanged_count += 1
                                continue
                            try:
                                # os.replace()はWindows上で上書きも原子的に行うため、
                                # 直前にos.remove(dest)する必要は無い(それを行うと
                                # destが一瞬存在しない窓ができ、Maya側のポーリングが
                                # 「消えた」と誤認する余地を作ってしまう)。
                                os.replace(src, dest)
                            except OSError:
                                # 2026.07.24(緊急バグ修正): ステージングと監視/Final
                                # フォルダが別ボリューム(共有PCでネットワークドライブを
                                # 指定する運用等)の場合、os.replace()はクロスデバイス
                                # エラーでOSErrorを送出する。従来はここで直接destへ
                                # shutil.copyfile()していたため、Maya側のポーリングが
                                # 書き込み途中のファイル(torn read)を読む可能性が
                                # あった。dest と同じディレクトリに一時ファイルとして
                                # コピーしてから os.replace() で置き換えることで、
                                # 同一ボリューム内のrenameとして常に原子的にする。
                                tmp_dest = dest + ".tmp"
                                shutil.copyfile(src, tmp_dest)
                                os.replace(tmp_dest, dest)
                                os.remove(src)
                            moved_count += 1
                            replaced_this_set = True

                            if texture_set_name and preview:
                                base = os.path.basename(dest)
                                for suffix in CHANNEL_SUFFIXES:
                                    marker = "_{0}.".format(suffix)
                                    if marker in base:
                                        export_prefix_updates[texture_set_name] = base.split(marker)[0]
                                        break
                        if replaced_this_set and isinstance(texture_set_name, str) and texture_set_name:
                            replaced_texture_sets.add(texture_set_name)

                    if export_prefix_updates:
                        # 2026.07.15-01: 別プロジェクトの同名テクスチャセットに
                        # prefixを取り違えて上書きしないよう、プロジェクトキーで
                        # ネストした構造(texture_set_export_prefix_by_project)に
                        # 保存する。
                        project_key = _current_project_key()
                        prefix_by_project = dict(self.config.get("texture_set_export_prefix_by_project", {}))
                        current_map = dict(prefix_by_project.get(project_key, {}))
                        if any(current_map.get(k) != v for k, v in export_prefix_updates.items()):
                            current_map.update(export_prefix_updates)
                            prefix_by_project[project_key] = current_map
                            self.config["texture_set_export_prefix_by_project"] = prefix_by_project
                            save_config({"texture_set_export_prefix_by_project": prefix_by_project})

                outcome_status = status_name
                outcome_message = message
            except Exception as exc:
                error = exc
                outcome_status = "exception"
                outcome_message = "{0}".format(exc)

            # 印より先に結果ファイル。印は 1 件以上移動した通常終了のときだけ。
            # 例外のときは印を書かない(従来、例外は印の前で抜けていた)。
            finished_at = time.time()
            write_flag = error is None and moved_count >= 1
            result_obj = build_export_result(
                __version__,
                export_id,
                "preview" if preview else "final",
                bool(force_full),
                outcome_status,
                outcome_message,
                moved_count,
                skipped_unchanged_count,
                replaced_texture_sets,
                write_flag,
                started_at,
                finished_at,
                export_request_id,
            )
            self._export_last_meta = {
                "ok": bool(result_obj.get("ok")),
                "status": result_obj.get("status"),
                "export_id": result_obj.get("export_id"),
                "message": result_obj.get("message") or "",
            }
            publish_export_outputs(dest_root, result_obj, write_flag)
            published = True
            ok = bool(result_obj.get("ok"))
            self._emit_export_outcome(
                preview, ok, result_obj.get("status") or outcome_status,
                moved_count, export_config, force_full,
            )
            if error is None and ok and not preview:
                # 2026.07.19-03(フェーズ2): 成功したのでbaselineを確立
                # (または再確立)し、次回以降の差分書き出しを有効にする。
                # 2026.10.18(1.3.0): ok(status が success)のときだけ。
                # Warning / Error では差し戻しが働くよう、ここを通さない。
                final_export_succeeded = True
                self.final_baseline_established = True
                self._last_final_completed_at = time.time()
                self._final_export_force_full = False
                self.final_pending_changed.emit(False)
            if error is not None:
                raise error
        except Exception as exc:
            # makedirs や印の書き込みなど、上の結果ファイルを書く前に
            # 落ちたときも、dest_root が決まっていれば結果を残す。
            # すでに書いたあとの再送出では上書きしない。
            if not published:
                try:
                    publish_export_outputs(
                        dest_root,
                        build_export_result(
                            __version__,
                            export_id,
                            "preview" if preview else "final",
                            bool(force_full),
                            "exception",
                            "{0}".format(exc),
                            moved_count,
                            skipped_unchanged_count,
                            replaced_texture_sets,
                            False,
                            started_at,
                            time.time(),
                            export_request_id,
                        ),
                        False,
                    )
                    self._export_last_meta = {
                        "ok": False,
                        "status": "exception",
                        "export_id": export_id,
                        "message": "{0}".format(exc),
                    }
                except Exception as write_exc:
                    _log("error", "書き出し結果ファイルの保存に失敗しました(書き出し自体は続行します): {0}".format(write_exc))
                self._emit_export_outcome(
                    preview, False, "exception", moved_count, export_config, force_full)
            raise
        finally:
            self._export_moved_count = moved_count
            if not preview and not final_export_succeeded and final_dirty_snapshot:
                # 失敗した(例外・キャンセル・Warning・Error)ので、今回対象に
                # していた差分を失わないよう差し戻す。書き出し中に新たに
                # 積まれた分(finally節実行時点のself.dirty_stack_ids_final)は
                # そのまま残し、そこへ合流させる。
                self.dirty_stack_ids_final |= final_dirty_snapshot
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # 2026.07.19-04(フェーズ3: 一般的な軽量化)
    _HASH_CHUNK_SIZE = 1024 * 1024  # 1MB単位で読みながらハッシュを更新する

    def _file_digest(self, path):
        """ファイル全体を一度にメモリへ読み込まず、チャンク単位で読みながら
        MD5を更新する。計算結果(判定結果)はf.read()で一括読み込みする
        場合と完全に同一だが、ピークメモリ使用量を抑えられる。
        4096px級のテクスチャを複数チャンネル・複数テクスチャセット分
        まとめて処理する後処理区間(危険な区間、2026.07.19-02の項参照)の
        メモリ負荷軽減が狙い。
        """
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(self._HASH_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _unchanged(self, src_path, dest_path):
        """2026.07.19-02(クラッシュ再現テストを受けた追加対応): 保存直後の
        Final書き出しでは、export_project_textures() が返った後(=SP
        ネイティブの「書き出し中」ダイアログが消えた後)、こちらの
        Pythonコードがこの後処理ループの中でまだ動き続けている。
        この区間はSP側の保護対象外で、実機テストでも「ダイアログが
        消えた後、数秒はまだ閉じるとクラッシュする」ことが確認された。

        従来は対象ファイル全てを毎回フル読み込みしてMD5計算しており、
        この後処理区間の長さに直結していた。ここでは、まずファイル
        サイズだけを比較する軽量な事前チェックを行い、サイズが違う
        (=内容が違うことが確定する)場合はハッシュ計算そのものを
        省略する。サイズが同じ場合のみ、従来通りMD5で内容を比較する
        (「サイズは同じだが内容だけ違う」ケースを見逃さないため)。
        判定結果(変更あり/なし)自体はどちらの経路でも同じになるため、
        既存の挙動を変えない安全な最適化である。

        2026.07.19-04(フェーズ3): ハッシュ計算自体も、ファイル全体を
        一括読み込みする方式からチャンク単位の読み込みに変更した
        (_file_digest()参照)。判定結果は変わらない。
        """
        try:
            size = os.path.getsize(src_path)
        except OSError:
            return False

        prev_size = self.last_sizes.get(dest_path)
        self.last_sizes[dest_path] = size
        if prev_size is not None and prev_size != size:
            # サイズが違う時点で「変更あり」が確定するため、ハッシュ計算
            # (ファイル全体の読み込み)を省略する。次回サイズが偶然一致
            # した場合に古いハッシュで誤って「変更なし」と判定しないよう、
            # 古いハッシュ値も破棄しておく。
            self.last_hashes.pop(dest_path, None)
            return False

        try:
            digest = self._file_digest(src_path)
        except Exception:
            return False
        prev_hash = self.last_hashes.get(dest_path)
        self.last_hashes[dest_path] = digest
        return prev_hash is not None and prev_hash == digest

    def _resolve_stack_names(self, stack_ids):
        """stack_idの集合から、対応するテクスチャセット名の集合を解決する。
        2026.07.19-03(フェーズ2): Preview・Final双方の差分フィルタで
        共通して使うため、_build_export_config()内にあった処理を
        切り出した(挙動は変更していない)。
        """
        names = set()
        for stack_id in stack_ids:
            try:
                stack = textureset.Stack(stack_id)
                names.add(stack.material().name())
            except Exception as e:
                self._emit_status(
                    "stack_id={0} からテクスチャセットを特定できませんでした: {1}".format(stack_id, e)
                )
        return names

    def _build_export_config(self, out_dir, preview, dirty_stack_ids=None,
                              final_dirty_stack_ids=None, force_full=False):
        cfg = self.config
        size_log2 = (
            int(cfg.get("preview_resolution_log2", 9))
            if preview
            else int(cfg.get("final_resolution_log2", 12))
        )

        configured_sets = cfg.get("texture_sets") or None

        dirty_set_names = None
        if not force_full:
            if preview:
                if dirty_stack_ids:
                    names = self._resolve_stack_names(dirty_stack_ids)
                    if names:
                        dirty_set_names = names
            else:
                # 2026.07.19-03(フェーズ2): baseline(このセッションで
                # 一度でもFinalに成功した実績)が無い場合は、差分の有無に
                # 関わらず全量書き出しにフォールバックする(安全側)。
                # baseline確立済みで、かつ前回のFinal成功以降に変更が
                # あった場合のみ差分に絞る。
                if self.final_baseline_established and final_dirty_stack_ids:
                    names = self._resolve_stack_names(final_dirty_stack_ids)
                    if names:
                        dirty_set_names = names

        if dirty_set_names is not None:
            if configured_sets:
                target_sets = [s for s in configured_sets if s in dirty_set_names]
                if not target_sets:
                    target_sets = configured_sets
            else:
                target_sets = sorted(dirty_set_names)
        else:
            target_sets = configured_sets or [ts.name() for ts in textureset.all_texture_sets()]

        # 2026.07.17 修正: fileName に $udim トークンが無かったため、UDIM
        # (複数UVタイル)を含むテクスチャセットを書き出すと、$textureSet_
        # BaseColor のようなテンプレートが全タイルで同じ文字列に解決されて
        # しまい、Painter側が名前衝突を避けるために独自の連番
        # (_1, _2, ...)を末尾に付与していた。これはタイル番号(1001,
        # 1002...)ではなく単なる重複回避カウンタのため、udim_setup.py の
        # タイル検出(末尾の4桁UDIM番号を期待)や sp_to_aiStandardSurface.py
        # のプレフィックス抽出が正しく機能しない原因になっていた。
        # 括弧で囲むことで、Painter公式ドキュメントの仕様通り「UDIM
        # タイルが複数ある場合のみ .<UDIM番号> を付与し、単一タイルの
        # 場合は何も付与しない」条件付きトークンとして働く。
        # (Live/Final の両方でこの _build_export_config() を共有している
        # ため、この修正は自動的に両方に適用される)
        maps = [
            {"fileName": "$textureSet_BaseColor(.$udim)", "channels": [
                {"destChannel": c, "srcChannel": c, "srcMapType": "documentMap", "srcMapName": "basecolor"}
                for c in ("R", "G", "B")
            ]},
            {"fileName": "$textureSet_Roughness(.$udim)", "channels": [
                {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "roughness"}
            ]},
            {"fileName": "$textureSet_Metallic(.$udim)", "channels": [
                {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "metallic"}
            ]},
            {"fileName": "$textureSet_Normal(.$udim)", "channels": [
                {"destChannel": c, "srcChannel": c, "srcMapType": "virtualMap", "srcMapName": "Normal_OpenGL"}
                for c in ("R", "G", "B")
            ]},
            {"fileName": "$textureSet_Height(.$udim)", "channels": [
                {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "height"}
            ]},
            {"fileName": "$textureSet_Emissive(.$udim)", "channels": [
                {"destChannel": c, "srcChannel": c, "srcMapType": "documentMap", "srcMapName": "emissive"}
                for c in ("R", "G", "B")
            ]},
        ]

        export_parameters = [{
            "parameters": {
                "fileFormat": cfg.get("file_format", "png"),
                "bitDepth": cfg.get("bit_depth", "8"),
                "dithering": True,
                "paddingAlgorithm": "infinite",
                "sizeLog2": size_log2,
            }
        }]
        # 2026.07.17: 上の maps 側 fileName に (.$udim) を追加したため、
        # このフィルタも同じテンプレート文字列(UDIMトークン込み)で
        # 指定しないと対象マップにマッチしなくなる(outputMapsは
        # fileNameテンプレート文字列そのものを見て絞り込むため)。
        for suffix in cfg.get("raw_colorspace_suffixes", []):
            export_parameters.append({
                "filter": {"outputMaps": ["$textureSet_{0}(.$udim)".format(suffix)]},
                "parameters": {
                    "fileFormat": cfg.get("file_format", "png"),
                    "bitDepth": cfg.get("bit_depth", "8"),
                    "dithering": False,
                    "paddingAlgorithm": "infinite",
                    "sizeLog2": size_log2,
                }
            })

        return {
            "exportPath": out_dir,
            "exportShaderParams": False,
            "exportPresets": [{"name": "livesync_preset", "maps": maps}],
            "defaultExportPreset": "livesync_preset",
            "exportList": [{"rootPath": ts} for ts in target_sets],
            "exportParameters": export_parameters,
        }


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


# ---------------------------------------------------------------------------
# 2026.10.19(1.3.1): 画面設定の未保存判定(純関数)
# Qt / Painter に依存しない。書き出し処理には使わない。
# ---------------------------------------------------------------------------

PANEL_SETTING_KEYS = (
    "staging_dir", "watch_dir", "final_export_dir",
    "preview_resolution_log2", "final_resolution_log2",
    "debounce_seconds", "final_cooldown_seconds",
)

# 画面の日本語ラベル。未保存ラベルのツールチップに並べる。
# 折りたたみ設定グループの行ラベルと揃える。
_PANEL_SETTING_LABELS = {
    "staging_dir": "ステージングフォルダ",
    "watch_dir": "監視フォルダ(プレビュー)",
    "final_export_dir": "最終出力フォルダ(保存時)",
    "preview_resolution_log2": "プレビュー解像度",
    "final_resolution_log2": "最終出力解像度(高画質)",
    "debounce_seconds": "デバウンス間隔",
    "final_cooldown_seconds": "Final書き出しのクールダウン",
}

_PANEL_PATH_KEYS = ("staging_dir", "watch_dir", "final_export_dir")
_PANEL_INT_DEFAULTS = {
    "preview_resolution_log2": 9,
    "final_resolution_log2": 12,
}
_PANEL_FLOAT_DEFAULTS = {
    "debounce_seconds": 1.5,
    "final_cooldown_seconds": 5.0,
}


def unsaved_setting_keys(panel_values, saved_config):
    """画面の値と保存済みの値を比べ、食い違うキーを PANEL_SETTING_KEYS の順で返す。"""
    panel_map = panel_values if isinstance(panel_values, dict) else {}
    saved_map = saved_config if isinstance(saved_config, dict) else {}
    dirty = []
    for key in PANEL_SETTING_KEYS:
        if not _panel_setting_matches(key, panel_map, saved_map):
            dirty.append(key)
    return dirty


def _panel_setting_matches(key, panel_map, saved_map):
    try:
        if key in _PANEL_PATH_KEYS:
            return _panel_paths_match(panel_map.get(key), saved_map.get(key))
        if key in _PANEL_INT_DEFAULTS:
            panel_n = int(panel_map[key])
            if key not in saved_map:
                saved_n = _PANEL_INT_DEFAULTS[key]
            else:
                saved_n = int(saved_map[key])
            return panel_n == saved_n
        if key in _PANEL_FLOAT_DEFAULTS:
            panel_n = float(panel_map[key])
            if key not in saved_map:
                saved_n = _PANEL_FLOAT_DEFAULTS[key]
            else:
                saved_n = float(saved_map[key])
            return abs(panel_n - saved_n) <= 1e-6
    except (TypeError, ValueError, KeyError, AttributeError, OSError):
        return False
    return False


def _panel_paths_match(panel_raw, saved_raw):
    panel_text = str(panel_raw).strip()
    saved_text = str(saved_raw).strip()
    if panel_text == "" and saved_text == "":
        return True
    if panel_text == "" or saved_text == "":
        return False
    return (
        os.path.normcase(os.path.normpath(panel_text))
        == os.path.normcase(os.path.normpath(saved_text))
    )


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


# ---------------------------------------------------------------------------
# 2026.10.19(1.4.0 PB-b): Painter API と確認ダイアログ(engine 依存)
# ---------------------------------------------------------------------------
try:
    import substance_painter.layerstack as layerstack
except Exception:
    layerstack = None


class RebaseEngineHooks(object):
    def __init__(self, engine):
        self.engine = engine

    def is_shutting_down(self):
        return bool(getattr(self.engine, "_shutting_down", False))

    def emit_status(self, text):
        if self.is_shutting_down():
            _log("info", text)
            try:
                _append_history("SP", text)
            except Exception:
                pass
            return
        self.engine._emit_status(text)

    def log(self, level, text):
        _log(level, text)

    def set_blocks_exports(self, value):
        self.engine._set_rebase_blocks_exports(value)

    def resume_blocked_exports(self):
        self.engine._resume_blocked_exports()

    def load_config(self):
        return load_config()

    def current_project_key(self):
        return _current_project_key()

    def project_subfolder(self):
        return _project_subfolder_name()


class PainterRebaseApi(object):
    def __init__(self, engine):
        self.engine = engine
        self.closed = False
        self._can_disable = None

    def _alive(self):
        return not self.closed and self.engine is not None and not getattr(
            self.engine, "_shutting_down", False)

    def now(self):
        return time.time()

    def project_open(self):
        if not self._alive():
            return False
        try:
            return bool(project.is_open())
        except Exception:
            return False

    def current_project_key(self):
        if not self._alive():
            return None
        try:
            return _current_project_key()
        except Exception:
            return None

    def snapshot(self):
        defaults = dict(_PAINTER_STATE_DEFAULTS)
        out = {
            "is_open": False,
            "in_edition": False,
            "project_key": defaults.get("project_key"),
            "saved_path": defaults.get("saved_path"),
            "busy": defaults.get("busy"),
            "exporting": defaults.get("exporting"),
            "needs_saving": defaults.get("needs_saving"),
            "needs_saving_missing": False,
            "can_disable_auto_unwrap": defaults.get("can_disable_auto_unwrap"),
            "last_edit_at": None,
        }
        if not self._alive():
            return out
        try:
            out["is_open"] = bool(project.is_open())
        except Exception:
            out["is_open"] = False
        try:
            fn = getattr(project, "is_in_edition_state", None)
            out["in_edition"] = bool(fn()) if callable(fn) else False
        except Exception:
            out["in_edition"] = False
        try:
            out["project_key"] = _current_project_key()
        except Exception:
            out["project_key"] = None
        if _is_unsaved_project_key(out["project_key"]):
            out["saved_path"] = None
        else:
            try:
                out["saved_path"] = project.file_path()
            except Exception:
                out["saved_path"] = None
        try:
            out["busy"] = bool(project.is_busy())
        except Exception:
            out["busy"] = True
        out["exporting"] = bool(getattr(self.engine, "exporting", True))
        needs_fn = getattr(project, "needs_saving", None)
        if not callable(needs_fn):
            out["needs_saving"] = False
            out["needs_saving_missing"] = True
        else:
            try:
                out["needs_saving"] = bool(needs_fn())
            except Exception:
                out["needs_saving"] = False
                out["needs_saving_missing"] = True
        if self._can_disable is None:
            self._can_disable = supports_auto_unwrap_settings(
                getattr(project, "MeshReloadingSettings", None))
        out["can_disable_auto_unwrap"] = bool(self._can_disable)
        out["last_edit_at"] = getattr(self.engine, "_last_texture_event_at", None)
        return out

    def recheck(self, request):
        if not self._alive():
            return ApprovalDecision("wait", "busy")
        snap = self.snapshot()
        mesh = request.get("mesh") if isinstance(request, dict) else None
        cache = None
        coord = getattr(self.engine, "_rebase", None)
        rid = request.get("request_id") if isinstance(request, dict) else None
        if coord is not None and rid:
            cache = coord._precheck.get(rid)
        exists, sha1_ok, new_cache = mesh_precheck(mesh, cache=cache)
        if coord is not None and rid:
            coord._precheck.put(rid, new_cache)
        cancelled = False
        superseded = False
        if coord is not None and rid:
            cancelled = rid in coord._cancelled
            superseded = rid in coord._superseded
        if not exists:
            sha1_ok = None
        settings = None
        if coord is not None:
            settings = coord._settings
        if snap.get("needs_saving_missing") and self.engine is not None:
            if not getattr(self.engine, "_needs_saving_missing_logged", False):
                self.engine._needs_saving_missing_logged = True
                if effective_rebase_approval(settings) == "auto":
                    _log(
                        "warning",
                        "project.needs_saving が読めないため、auto では未保存として待ちます。要実機確認。",
                    )
                else:
                    _log(
                        "warning",
                        "project.needs_saving が読めないため、未保存判定は偽です。要実機確認。",
                    )
        painter_state = build_painter_state(
            now=self.now() if not callable(getattr(self.engine, "_rebase", None)) else (
                self.engine._rebase.clock() if self.engine._rebase is not None else self.now()
            ),
            is_open=snap.get("is_open"),
            in_edition=snap.get("in_edition"),
            project_key=snap.get("project_key"),
            saved_path=snap.get("saved_path"),
            busy=snap.get("busy"),
            exporting=snap.get("exporting"),
            needs_saving=needs_saving_for_rebase(snap, settings),
            backup_includes_unsaved=BACKUP_INCLUDES_UNSAVED,
            mesh_exists=exists,
            mesh_sha1_ok=sha1_ok,
            superseded=superseded,
            cancelled=cancelled,
            can_disable_auto_unwrap=snap.get("can_disable_auto_unwrap"),
            last_edit_at=snap.get("last_edit_at"),
        )
        return decide_approval(request, painter_state, settings)

    def project_file_path(self):
        if not self._alive():
            return None
        try:
            return project.file_path()
        except Exception:
            return None

    def is_busy(self):
        if not self._alive():
            return True
        try:
            return bool(project.is_busy())
        except Exception:
            return True

    def file_exists(self, path):
        try:
            return os.path.isfile(path)
        except OSError:
            return False

    def file_size(self, path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def make_dirs(self, path):
        if not self._alive():
            return
        os.makedirs(path, exist_ok=True)

    def save_as_copy(self, path):
        if not self._alive():
            raise RuntimeError("closed")
        mode = getattr(project, "ProjectSaveMode", None)
        full = getattr(mode, "Full", None) if mode is not None else None
        if full is None:
            raise RuntimeError("ProjectSaveMode.Full がありません")
        project.save_as_copy(path, full)

    def supports_auto_unwrap(self):
        if self._can_disable is None:
            self._can_disable = supports_auto_unwrap_settings(
                getattr(project, "MeshReloadingSettings", None))
        return bool(self._can_disable)

    def make_reload_settings(self, **kwargs):
        if not self._alive():
            raise RuntimeError("closed")
        cls = getattr(project, "MeshReloadingSettings", None)
        if cls is None:
            raise RuntimeError("MeshReloadingSettings がありません")
        return cls(**kwargs)

    def reload_mesh(self, path, settings, callback):
        if not self._alive():
            return
        project.reload_mesh(path, settings, callback)

    def uv_fill_context(self):
        if not self._alive():
            return {"api": None, "texture_sets": [], "ts_names": []}
        fill_api = make_uv_fill_api(layerstack)
        sets = []
        names = []
        try:
            sets = list(textureset.all_texture_sets())
        except Exception:
            sets = []
        for ts in sets:
            name = _texture_set_name(ts)
            if name:
                names.append(name)
        return {"api": fill_api, "texture_sets": sets, "ts_names": names}

    def run_final_export(self, request_id):
        if not self._alive():
            return ExportAttempt(executed=False, message="closed")
        return self.engine.run_rebase_final_export(request_id)

    def last_imported_mesh_path(self):
        if not self._alive():
            return None
        getter = getattr(project, "last_imported_mesh_path", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def record_painter_mesh(self, request):
        if not self._alive():
            return False
        key = _current_project_key()
        if _is_unsaved_project_key(key):
            return False
        path = self.last_imported_mesh_path()
        mesh = request.get("mesh") if isinstance(request, dict) else None
        if not isinstance(path, str) or path == "":
            if isinstance(mesh, dict):
                path = mesh.get("path")
        if not isinstance(path, str) or path == "":
            return False
        prev = None
        if isinstance(mesh, dict) and mesh.get("path") == path:
            prev = {
                "path": path,
                "size": mesh.get("size"),
                "mtime_ns": mesh.get("mtime_ns"),
                "sha1": mesh.get("sha1"),
            }
        else:
            by_project = self.engine.config.get("painter_mesh_by_project")
            if isinstance(by_project, dict):
                prev = by_project.get(key)
        try:
            record = build_mesh_record(path, prev, time.time())
        except Exception:
            return False
        if record is None:
            return False
        written = _update_nested_config_entry("painter_mesh_by_project", key, record)
        if written is None:
            return False
        self.engine.config["painter_mesh_by_project"] = written
        return True


def approval_action_from_role(role, accept_role):
    """ボタンの役割から action を決める。AcceptRole のときだけ "run"。

    None、RejectRole、未知の値、比較の例外は "cancel"。
    ボタンのラッパー同士の同一性や、ラベルの文字列では比べない。
    AcceptRole は 0 なので、値の真偽では判定しない。
    """
    if role is None:
        return "cancel"
    try:
        matched = role == accept_role
    except Exception:
        return "cancel"
    if matched:
        return "run"
    return "cancel"


class RebaseApprovalDialogHost(object):
    """非モーダルの確認ダイアログ。シグナルはメソッドへつなぎ、shutdown で切る。"""

    def __init__(self, text, on_result):
        self._on_result = on_result
        self._closed = False
        parent = None
        getter = getattr(ui, "get_main_window", None)
        if callable(getter):
            try:
                parent = getter()
            except Exception:
                parent = None
        # 親ラッパーを保持しないと、PySide が子ボタンを無効にする(12.1.5)。
        self._parent = parent
        if parent is not None:
            self.box = QtWidgets.QMessageBox(parent)
        else:
            self.box = QtWidgets.QMessageBox()
        self.box.setWindowTitle(REBASE_DIALOG_TITLE)
        self.box.setText(text)
        self._run_btn = self.box.addButton(
            REBASE_BUTTON_RUN, QtWidgets.QMessageBox.AcceptRole)
        self._cancel_btn = self.box.addButton(
            REBASE_BUTTON_CANCEL, QtWidgets.QMessageBox.RejectRole)
        self.box.setDefaultButton(self._run_btn)
        self.box.setEscapeButton(self._cancel_btn)
        self.box.finished.connect(self._on_finished)
        # 既定は ApplicationModal。仕様は出したまま操作できる非モーダル。
        self.box.setWindowModality(QtCore.Qt.NonModal)
        self.box.show()

    def _on_finished(self, _result=None):
        if self._closed:
            return
        action = "cancel"
        try:
            clicked = self.box.clickedButton()
            if clicked is not None:
                action = approval_action_from_role(
                    self.box.buttonRole(clicked),
                    QtWidgets.QMessageBox.AcceptRole,
                )
        except Exception:
            action = "cancel"
        if callable(self._on_result):
            self._on_result(action)

    def disconnect_all(self):
        try:
            self.box.finished.disconnect(self._on_finished)
        except Exception:
            pass

    def close(self):
        # finished より先。終了処理の close を declined として扱わない。
        self._closed = True
        try:
            self.box.close()
        except Exception:
            pass
        self._parent = None

    def deleteLater(self):
        fn = getattr(self.box, "deleteLater", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
        self._parent = None


def make_rebase_approval_dialog(text, on_result):
    return RebaseApprovalDialogHost(text, on_result)


def default_rebase_job_factory(request, reporter, api, settings, clock=None, approved=True):
    return RebaseJob(
        request, reporter, api, settings,
        clock=clock or time.time, approved=approved,
    )


# ---------------------------------------------------------------------------
# プラグインのライフサイクル(Substance Painterが自動的に呼び出す)
# ---------------------------------------------------------------------------

_engine = None
_panel = None


def start_plugin():
    global _engine, _panel
    # SP側・Maya側でバージョンがズレたまま運用してしまい、片方だけ古い
    # ロジックで動いていることに気付けなかった経緯があるため、起動の
    # 最初に必ずバージョンをログへ出す(Pythonログに出力される)。
    _log("info", "[sp_live_sync_plugin] loaded version: {0}  (file: {1})".format(
        __version__, os.path.abspath(__file__)))
    # 2026.07.24(緊急バグ修正): 従来は既存の_engineの有無を確認せず
    # 無条件で新規LiveSyncEngine()を作成していた。SPのプラグイン無効化→
    # 再有効化を経由せずstart_plugin()が二重に呼ばれた場合(Mayaの
    # reload()後にshow_ui()を呼ぶと旧インスタンスがリークしていた不具合
    # と同じクラス)、旧エンジンのevent.DISPATCHER購読(connect_strongの
    # 強参照)が解除されないまま残り、同期処理が多重発火する恐れが
    # あった。close_plugin()と同じ後始末を先に行ってから差し替える。
    if _engine is not None:
        try:
            _engine.shutdown()
        except Exception as e:
            _log("error", "既存エンジンの後始末に失敗しました: {0}".format(e))
        _engine = None
    if _panel is not None:
        try:
            ui.delete_ui_element(_panel)
        except Exception as e:
            _log("error", "既存パネルの後始末に失敗しました: {0}".format(e))
        _panel = None
    try:
        _engine = LiveSyncEngine()
        try:
            _engine.start_rebase_watch()
        except Exception as e:
            _log("warning", "rebase タイマーの開始に失敗しました: {0}".format(e))
        # 2026.07.19-01: クラッシュ等でfinally節が実行されず残った
        # ステージングフォルダの残骸を、起動のたびに自動回収する。
        # 失敗しても起動処理自体は継続させたいため、専用のtry/exceptで
        # 囲む(_cleanup_orphaned_staging_dirs自体は内部でOSErrorを
        # 個別に握りつぶしているが、stage_rootの取得自体が失敗する
        # ケースも念のため保護する)。
        try:
            _cleanup_orphaned_staging_dirs(
                _engine.config.get("staging_dir", DEFAULT_CONFIG["staging_dir"]))
        except Exception as e:
            _log("warning", "ステージングフォルダの自動掃除に失敗しました: {0}".format(e))
        # 2026.07.25(緊急バグ修正): 共有フォルダがクラウド同期対象に
        # 入っていると、設定ファイルの原子的な置き換え・アドバイザリ
        # ロックの前提が崩れる(詳細は _detect_cloud_sync_risk() 上の
        # コメント参照)。強制はせず、起動のたびに検出して知らせる。
        try:
            cloud_sync_reason = _detect_cloud_sync_risk(CONFIG_DIR)
            if cloud_sync_reason:
                _engine._emit_status(
                    "警告: 共有フォルダ({0})がクラウド同期(Google Drive/OneDrive/"
                    "Dropbox等)の対象になっている可能性があります({1})。"
                    "同期クライアントがファイルを横から書き換えることで、"
                    "設定ファイルの巻き戻りや同期の無言失敗につながる恐れが"
                    "あります。可能であればこのフォルダを同期対象から"
                    "除外してください。".format(CONFIG_DIR, cloud_sync_reason)
                )
        except Exception as e:
            _log("warning", "クラウド同期の検出に失敗しました: {0}".format(e))
        _panel = LiveSyncPanel(_engine)
        ui.add_dock_widget(_panel)
        if not _engine.config.get("setup_wizard_completed"):
            _panel.open_setup_wizard()
        _log("info", "SP -> Maya Live Sync (Phase 1 + Phase 2最適化 + Phase 3) を起動しました。")
    except Exception as e:
        _log("error", "プラグインの起動に失敗しました: {0}".format(e))


def close_plugin():
    global _engine, _panel
    # 2026.10.19(1.4.0 PB-b S3): 先にグローバルを退避して None にし、
    # 後始末は個別の try/except で守る。delete_ui_element(_panel) は残す
    # (#38 のパネル接続を片付ける唯一の経路)。
    engine = _engine
    panel = _panel
    _engine = None
    _panel = None
    if engine is not None:
        try:
            engine.shutdown()
        except Exception as e:
            _log("error", "エンジンの後始末に失敗しました: {0}".format(e))
    if panel is not None:
        try:
            ui.delete_ui_element(panel)
        except Exception as e:
            _log("error", "パネルの後始末に失敗しました: {0}".format(e))
    # S4: 循環を切ったあと、ローカルの panel も外してから回収を確認する。
    # deleteLater はまだ入れない。
    ref = None
    if engine is not None:
        try:
            import weakref
            ref = weakref.ref(engine)
        except Exception:
            ref = None
    engine = None
    panel = None
    if ref is not None:
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        if ref() is None:
            _log("info", "LiveSyncEngine は解放されました")
        else:
            _log(
                "warning",
                "LiveSyncEngine が残っています(要実機確認 S4)。deleteLater は入れていません",
            )
    _log("info", "SP -> Maya Live Sync プラグインを停止しました。")