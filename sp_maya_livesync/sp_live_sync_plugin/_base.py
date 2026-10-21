"""設定の読み書きとロック、ログ、履歴、ステージング掃除、クラウド同期の検出、
原子的なファイル書き込み、書き出し結果ファイル、メッシュ記録、
リベースの依頼 ID と結果ファイルのパス。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os
import re
import json
import copy
import stat
import time
import shutil
import hashlib
import datetime
import contextlib

import substance_painter.export as export
import substance_painter.logging as sp_log

from ._version import __version__

__all__ = [
    "msvcrt",
    "CONFIG_DIR",
    "CONFIG_PATH",
    "SYNC_RESULT_NAME",
    "SYNC_FLAG_NAME",
    "EXPORT_RESULT_SCHEMA",
    "EXPORT_MESSAGE_MAX_CHARS",
    "_HASH_CHUNK_BYTES",
    "MESH_SHA1_MAX_BYTES",
    "_EXPORT_STATUS_TOKENS",
    "_EXPORT_STATUS_LABELS",
    "DEFAULT_CONFIG",
    "_log",
    "HISTORY_LOG_PATH",
    "_HISTORY_MAX_BYTES",
    "_append_history",
    "_cleanup_orphaned_staging_dirs",
    "_CLOUD_SYNC_TEMP_MARKERS",
    "_CLOUD_SYNC_PATH_MARKERS",
    "_detect_cloud_sync_risk",
    "load_config",
    "CONFIG_LOCK_PATH",
    "_CONFIG_LOCK_TIMEOUT_SECONDS",
    "_CONFIG_LOCK_POLL_INTERVAL_SECONDS",
    "_ConfigLockTimeout",
    "_config_file_lock",
    "_merge_and_write_config",
    "save_config",
    "_update_nested_config_entry",
    "_status_token",
    "_match_known_export_status",
    "export_status_name",
    "export_status_label",
    "build_export_result",
    "write_json_atomic",
    "write_sync_flag",
    "publish_export_outputs",
    "REPLACE_RETRY_DELAYS_S",
    "_replace_with_retry",
    "write_rebase_result_copy",
    "_hash_file_hex",
    "build_mesh_record",
    "_is_unsaved_project_key",
    "REBASE_DIRNAME",
    "REBASE_RESULTS_DIRNAME",
    "REBASE_REQUEST_ID_RE",
    "is_valid_rebase_request_id",
    "_checked_rebase_request_id",
    "rebase_results_dir",
    "rebase_result_path",
]


try:
    import msvcrt
except ImportError:
    # このツールはWindows専用設計(C:/SPMayaLiveSync のハードコード等)
    # だが、念のためimport失敗時は設定ファイルロックを無効化して
    # 動作は継続できるようにする(_config_file_lock()参照)。
    msvcrt = None


# ---------------------------------------------------------------------------
# 設定の保存先(GUIから編集されるため、通常は直接触らなくてよい)
# ---------------------------------------------------------------------------

CONFIG_DIR = os.environ.get("SPMAYA_CONFIG_DIR", "C:/SPMayaLiveSync")
CONFIG_PATH = os.path.join(CONFIG_DIR, "live_sync_config.json")

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
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        _replace_with_retry(tmp_path, CONFIG_PATH)
    except Exception:
        # 2026.10.21(1.4.4): 失敗時に一時ファイルを残さない
        # (write_json_atomic と同じ)。残す挙動に依存するテストは無い。
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


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


# ---------------------------------------------------------------------------
# 2026.10.21(1.4.4): os.replace の短い再試行
# ---------------------------------------------------------------------------
# Windows では、置き換え先を他プロセス(Maya の読み取り、クラウド同期)が
# 開いていると os.replace が PermissionError(WinError 5)や共有違反
# (winerror 32)・ロック違反(winerror 33)で失敗する。Painter の
# メインスレッドで待つため、待ちの合計は 1 秒を超えない。
# FileNotFoundError と、それ以外の OSError(別ボリュームの winerror 17、
# errno.EXDEV、ディスク満杯など)は再試行しない。
# 待ちの回数を使い切ったら、最後の例外を型を変えずに送出する。
REPLACE_RETRY_DELAYS_S = (0.05, 0.1, 0.2, 0.3, 0.35)  # 合計 1.0 秒


def _replace_with_retry(src, dst, *, delays=REPLACE_RETRY_DELAYS_S, sleep_fn=None):
    """os.replace(src, dst) を、共有違反と思われるエラーのときだけ再試行する。

    delays の各要素は、失敗した直後の待ち秒。既定は 5 回(合計 1.0 秒)。
    その後の失敗は待たずに、その例外をそのまま送出する。
    再試行の末に成功したときは、ファイル名だけを info で 1 行残す。
    sleep_fn はテスト用。既定は time.sleep。
    """
    sleep = time.sleep if sleep_fn is None else sleep_fn
    attempt = 0
    while True:
        attempt += 1
        try:
            os.replace(src, dst)
        except FileNotFoundError:
            raise
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            retryable = isinstance(exc, PermissionError) or winerror in (32, 33)
            if not retryable or attempt > len(delays):
                raise
            sleep(delays[attempt - 1])
        else:
            if attempt > 1:
                _log(
                    "info",
                    "共有違反のため置き換えを再試行し、{0}回目で成功しました: {1}".format(
                        attempt, os.path.basename(dst)),
                )
            return


def write_json_atomic(path, obj):
    """一時ファイルへ書いてから os.replace() で原子的に置き換える。
    共有違反のときは短い再試行をする。
    失敗したときは一時ファイルを残さない。
    """
    tmp_path = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        _replace_with_retry(tmp_path, path)
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
    try:
        with open(tmp_flag_path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        _replace_with_retry(tmp_flag_path, flag_path)
    except Exception:
        try:
            if os.path.isfile(tmp_flag_path):
                os.remove(tmp_flag_path)
        except OSError:
            pass
        raise


def publish_export_outputs(
    dest_root,
    result_obj,
    write_flag,
    *,
    rebase_root=None,
    rebase_request_id=None,
):
    """2026.10.18(1.3.0): 結果ファイルを先に書き、その後に印を書く。

    write_flag が偽のときは印を書かない(1件も移動していないとき等、
    従来の条件のまま)。結果ファイルの失敗はログに残して書き出しを
    止めない。印の失敗は呼び出し元へ伝える。
    """
    result_path = os.path.join(dest_root, SYNC_RESULT_NAME)
    result_written = False
    try:
        write_json_atomic(result_path, result_obj)
        result_written = True
    except Exception as exc:
        _log("error", "書き出し結果ファイルの保存に失敗しました(書き出し自体は続行します): {0}".format(exc))
    if result_written and rebase_root and is_valid_rebase_request_id(rebase_request_id):
        write_rebase_result_copy(rebase_root, rebase_request_id, result_obj)
    if write_flag:
        write_sync_flag(os.path.join(dest_root, SYNC_FLAG_NAME))
    return result_written


def write_rebase_result_copy(root, request_id, result_obj):
    """リベース Final の結果を上書きされない依頼別ファイルへ原子的に保存する。"""
    try:
        path = rebase_result_path(root, request_id)
        write_json_atomic(path, result_obj)
        return True
    except Exception as exc:
        _log(
            "warning",
            "rebase: 結果の控えを書けませんでした(done は続行します): {0}".format(exc),
        )
        return False


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


REBASE_DIRNAME = "rebase"

REBASE_RESULTS_DIRNAME = "results"

REBASE_REQUEST_ID_RE = re.compile(r"^rb-\d{8}-\d{6}-[0-9a-f]{4}$")


def is_valid_rebase_request_id(value):
    return isinstance(value, str) and REBASE_REQUEST_ID_RE.fullmatch(value) is not None


def _checked_rebase_request_id(request_id):
    if not is_valid_rebase_request_id(request_id):
        raise ValueError("request_id が不正です")
    return request_id


def rebase_results_dir(root):
    return os.path.join(str(root), REBASE_DIRNAME, REBASE_RESULTS_DIRNAME)


def rebase_result_path(root, rid):
    rid = _checked_rebase_request_id(rid)
    return os.path.join(rebase_results_dir(root), rid + ".json")
