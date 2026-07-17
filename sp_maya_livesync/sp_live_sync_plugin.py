"""
sp_live_sync_plugin.py  (Phase 1: GUI版 + Phase 1/2 最適化)
================================================================================
Substance 3D Painter 側プラグイン(B案:自前パイプライン)

Phase 1:
    フォルダパス等の設定をドッキングパネル(GUI)から行えるようにした。

Phase 1 最適化:
    - stack_id を蓄積し、実際に変化のあったテクスチャセットだけを
      プレビュー書き出しの対象に絞り込む。
    - Height チャンネルの書き出し漏れを修正。
    - raw_colorspace_suffixes の設定が反映されていなかった不具合を修正。

Phase 2(構造変化検出・SP側):
    テクスチャセットの追加/削除をプロジェクトを開くたびに検出し、Maya側と
    共有する(詳細は maya_live_sync.py を参照)。

Phase 2 最適化(今回追加分・実機テスト前の自己レビューで発見した不具合修正):
    - known_texture_sets がプロジェクト単位ではなくグローバルな1つの値
      だったため、別のプロジェクトを開くたびに「全セットが削除され、
      全セットが新規追加された」という誤検知が大量発生していた。
      これをプロジェクト(ファイルパス)ごとに分離して記録するよう修正した。
    - busy中(ベイク等)にテクスチャセット一覧を取得すると一時的に不完全な
      状態を「削除」と誤検知する恐れがあったため、busy中はチェックを
      スキップするようにした。
    - 消失を検知しても即座に「削除」と確定せず、2回連続で検出された
      場合のみ確定するように変更し、一時的なちらつき(誤検知)を抑制した。

Phase 3(実機テストのフィードバックを受けた恒久対応 + GUI直感化):
    - save_config() をMaya側と同様「ディスク最新内容とのマージ書き込み」
      方式に変更し、SP/Maya間の保存タイミング次第で相手の変更を上書き
      消去してしまう競合を解消した。
    - active_project_key を共有設定に書き込み、複数のSPプロジェクトを
      横断して作業した場合でも、Maya側が「今開いているプロジェクト」を
      機械的に判別できるようにした(従来は全プロジェクト分が混在表示
      されていた)。
    - 実際に書き出したファイル名から texture_set_export_prefix を記録し、
      Maya側の _safe_name() による予測とのズレ(スペース・日本語等を
      含むテクスチャセット名で発生しうる)を根本的に解消した。
    - 初回セットアップウィザード(QWizard)を追加し、他人に共有した際にも
      フォルダ設定等を対話形式で迷わず行えるようにした。

複数プロジェクト対応(2026.07.14):
    - Final書き出し先を final_export_dir 直下から
      <final_export_dir>/<プロジェクト別サブフォルダ>/ に変更した。
      サブフォルダ名は「ファイル名+ハッシュ6桁」の併用方式
      (_project_subfolder_name())で、人が見て判別できることと、
      別の場所にある同名プロジェクト同士の衝突回避を両立している。
    - Maya側(aiSSボタン)が現在アクティブなプロジェクトのFinalフォルダを
      自動入力できるよう、共有設定に active_final_subfolder を書き込む
      ようにした。
    - Maya側とバージョンがズレたまま(片方だけ更新)で運用してしまい、
      「Finalにサブフォルダが作られない」という不具合として実機で誤認
      されたことがあったため、SP側・Maya側の双方に __version__ を追加。
      Substance Painterの起動時に Python ログへ必ずバージョンを出力する
      ようにした。今後この付近を変更する際は日付を上げること。

2026.07.14-02:
    - Live/Preview も Final と同じく <watch_dir>/<プロジェクト別
      サブフォルダ>/ へ書き出すよう変更した(_do_export の preview分岐)。
      共有設定に active_watch_subfolder を追加し、Maya側が追従できる
      ようにした。
    - 背景: 学校の共用PC環境で、watch_dir 直下に過去の別Windowsユーザー
      が残したファイルが混在していると、NTFSの所有権(ACL)により別
      ユーザーからは読めず(PermissionError)、Maya側でプレビュー
      テクスチャが一切表示されない不具合が実機で確認された。Finalは
      既にプロジェクト別サブフォルダ化されていたため発生しておらず、
      同じ方式をLiveにも適用することで解消した。

2026.07.15-01:
    - texture_set_export_prefix をプロジェクトキーでネストした構造
      (texture_set_export_prefix_by_project)に変更した。以前はテクスチャ
      セット名だけをキーにしたフラットな辞書だったため、別プロジェクトに
      同名のテクスチャセット(例: 複数プロジェクトで共通して使う "Body"
      など)が存在すると、Maya側が誤ったprefixでファイル名を予測して
      しまう可能性があった。Maya側の対応する変更と対になっている。
    - texture_set_shading_engine_map はMaya側専用のデータ(SP側は元々
      書き込んでいない)だったため、SP側のDEFAULT_CONFIGからは削除した。
"""

# バージョン情報。SP起動時に必ずPythonログへ出力し、「今動いているのが
# どの版か」を即座に確認できるようにする(maya_live_sync.py と同じ方式)。
#
# 2026.07.16-01 緊急修正:
#     _current_project_key() が project.file_path() の戻り値をそのまま
#     信頼していたため、テンプレートから新規プロジェクトを作成した
#     直後(まだ一度も保存していない状態)に、公式ドキュメントの契約
#     ("未保存ならNoneを返す")に反してテンプレートファイル(.spt)の
#     パスが返ってくるケースで、それをプロジェクトキーとして採用して
#     しまう不具合があった。Maya側の紐付けUIに実際のプロジェクト名
#     ("TEST_1"等)ではなくテンプレート名が表示される、という形で
#     顕在化した。
#     対策として、公式に「未保存なら確実にNoneを返す」と明記されている
#     project.name() を先に確認し、これがNoneであれば file_path() の
#     値を信頼しない(必ず "__unsaved__" にフォールバックする)よう
#     二段構えの判定に変更した。
#
# 2026.07.16-02 緊急修正:
#     v2026.07.16-01の修正(project.name()での二段構え判定)は「未保存の
#     間に誤ったキーを掴む」ケースは解決したが、別の見落としが残って
#     いた: on_project_edition_entered() はプロジェクトを開いた/作成
#     した"その瞬間"の active_project_key しか記録せず、その後
#     実際に名前を付けて保存しても再計算されない。一方 _do_export()
#     内の active_watch_subfolder はエクスポートのたびに都度再計算
#     されるため、「監視フォルダ名(TEST_1_5b5599等)は正しいのに、
#     Maya側の状態バーの紐付け表示だけがテンプレート名や__unsaved__の
#     まま」という矛盾が実機で確認された。
#     対策として、on_project_saved() でも active_project_key を
#     明示的に再計算・保存するようにした。
__version__ = "2026.07.16-02"

import os
import re
import json
import time
import shutil
import hashlib
import tempfile
import datetime

import substance_painter.event as event
import substance_painter.export as export
import substance_painter.project as project
import substance_painter.textureset as textureset
import substance_painter.logging as sp_log
import substance_painter.ui as ui

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

CONFIG_DIR = "C:/SPMayaLiveSync"
CONFIG_PATH = os.path.join(CONFIG_DIR, "live_sync_config.json")

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
    # Phase 3: SP側が実際に書き出したファイル名のprefixを記録する。
    # Maya側はテクスチャセット名を自前で安全化(_safe_name)して予測する
    # 代わりに、この値があれば最優先で使うことで、スペースや日本語を
    # 含む名前でのズレを防ぐ。
    # 2026.07.15-01: known_texture_sets_by_project と同様、プロジェクト
    # キーでネストする形式に変更した(別プロジェクトの同名テクスチャ
    # セットでprefixを取り違える不具合の対策):
    #   { project_key: { texture_set_name: prefix文字列 } }
    "texture_set_export_prefix_by_project": {},
    # Phase 3: 初回セットアップウィザードを完了したかどうか。
    "setup_wizard_completed": False,
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
# 設定の読み書き
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(loaded)
        return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    """設定を保存する。呼び出し直前にディスク上の最新内容を読み込んで
    からマージして書き込むことで、Maya側が別途保存した値
    (texture_set_shading_engine_map 等)を、こちらの古いメモリ上の値で
    意図せず上書き・消去してしまわないようにする。
    呼び出し側は、変更したキーだけを渡すこと(全体を渡すと、渡した側が
    保持している値が古い場合に他アプリの変更を巻き戻す恐れがある)。
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)
    merged = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            merged = json.load(f)
    except Exception:
        pass
    merged.update(cfg)
    # Phase 3 最適化: 一時ファイルに書いてから os.replace() で原子的に
    # 置き換える。SPとMayaが同時に保存/読込を行った際に、書き込み途中の
    # 不完全なJSONを相手側が読んでしまう(torn read)のを防ぐため。
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CONFIG_PATH)


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
# 同期ロジック本体
# ---------------------------------------------------------------------------

class LiveSyncEngine(QtCore.QObject):

    status_changed = QtCore.Signal(str)      # ログ1行を通知
    stats_changed = QtCore.Signal(dict)       # 統計情報の更新を通知
    # Phase 2: (現在の全テクスチャセット名一覧, 新規追加分, 消失確定分) を通知
    structure_changed = QtCore.Signal(list, list, list)

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
        self.dirty_stack_ids = set()
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
        """
        pairs = [
            (event.TextureStateEvent, self.on_texture_state),
            (event.ProjectSaved, self.on_project_saved),
            (event.ProjectAboutToClose, self.on_project_closing),
            (event.ProjectEditionEntered, self.on_project_edition_entered),
        ]
        for evt_cls, cb in pairs:
            try:
                event.DISPATCHER.disconnect(evt_cls, cb)
            except Exception as e:
                _log("warning", "イベント購読解除に失敗しました({0}): {1}".format(
                    getattr(evt_cls, "__name__", evt_cls), e))
        try:
            self.debounce_timer.stop()
        except Exception:
            pass

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

    def on_texture_state(self, evt):
        if not self.enabled:
            return
        if evt.action not in (
            event.TextureStateEventAction.ADD,
            event.TextureStateEventAction.UPDATE,
        ):
            return
        self.dirty_stack_ids.add(evt.stack_id)
        interval_ms = int(float(self.config.get("debounce_seconds", 1.5)) * 1000)
        self.debounce_timer.start(interval_ms)

    def on_project_saved(self, evt):
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
        key = _current_project_key()
        if self.config.get("active_project_key") != key:
            self.config["active_project_key"] = key
            save_config({"active_project_key": key})

        if not self.enabled:
            return
        # 保存直後はPainter内部の保存処理がまだロックを保持していることが
        # あり、即座にexport_project_textures()を呼ぶと「The project is
        # locked」で失敗することがある。少し待ってから実行することで
        # このすれ違いを避ける。
        QtCore.QTimer.singleShot(500, lambda: self._safe_export(preview=False))

    def on_project_closing(self, evt):
        self.debounce_timer.stop()
        # Phase 2 最適化: プロジェクトが切り替わるので、猶予カウンタを
        # リセットしておく(次のプロジェクトの検出に前プロジェクトの
        # 状態を持ち越さないため)。
        self._missing_streak = {}
        # Phase 3: プロジェクトが閉じられたので、Maya側に「今SPで開いている
        # プロジェクトは無い」ことを伝える。
        self.config["active_project_key"] = None
        save_config({"active_project_key": None})

    def on_project_edition_entered(self, evt):
        # 新しいプロジェクト(または既存プロジェクト)を開いた直後に
        # 基準となるテクスチャセット構成を記録する。
        # Phase 3: どのプロジェクトを開いたかをMaya側と共有する
        # (複数プロジェクトを横断して作業する際の一覧混在を防ぐため)。
        key = _current_project_key()
        self.config["active_project_key"] = key
        save_config({"active_project_key": key})
        if self.enabled:
            self._missing_streak = {}
            self.check_texture_set_structure()

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

    def _safe_export(self, preview=True):
        if not project.is_open():
            return
        if project.is_busy():
            self._emit_status("Painterがbusyのため、解除後に再試行します。")
            project.execute_when_not_busy(lambda: self._safe_export(preview))
            return
        if self.exporting:
            self.pending_reexport = True
            if not preview:
                self.pending_final = True
            self.debounce_timer.start(300)
            return

        dirty_ids = self.dirty_stack_ids
        self.dirty_stack_ids = set()

        self.exporting = True
        t0 = time.time()
        try:
            self._do_export(preview=preview, dirty_stack_ids=dirty_ids)
            self.stats["sync_count"] += 1
        except Exception as e:
            self.stats["error_count"] += 1
            self._emit_status("エラー: {0}".format(e))
        finally:
            self.exporting = False
            self.stats["last_duration_ms"] = int((time.time() - t0) * 1000)
            self.stats["last_sync_at"] = datetime.datetime.now().strftime("%H:%M:%S")
            self.stats_changed.emit(dict(self.stats))
            if self.pending_final:
                # 高画質書き出しの要求を優先して処理する(プレビューに
                # すり替わって消えてしまわないようにするため)。
                self.pending_reexport = False
                self.pending_final = False
                QtCore.QTimer.singleShot(200, lambda: self._safe_export(preview=False))
            elif self.pending_reexport:
                self.pending_reexport = False
                self.debounce_timer.start(200)
            self.check_texture_set_structure()

    def _do_export(self, preview=True, dirty_stack_ids=None):
        cfg = self.config
        subfolder = _project_subfolder_name()
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
            if cfg.get("active_watch_subfolder") != subfolder:
                cfg["active_watch_subfolder"] = subfolder
                save_config({"active_watch_subfolder": subfolder})
        else:
            # Final は複数プロジェクトが同じフォルダに混在・上書きするのを
            # 防ぐため、プロジェクトごとのサブフォルダへ書き出す。
            # 例: <final_export_dir>/proA_a1b2c3/
            dest_root = os.path.join(cfg["final_export_dir"], subfolder)
            # Maya 側(aiSSボタン)が「現在アクティブなプロジェクトのFinal
            # フォルダ」を自動入力できるよう、サブフォルダ名を共有設定に記録。
            if cfg.get("active_final_subfolder") != subfolder:
                cfg["active_final_subfolder"] = subfolder
                save_config({"active_final_subfolder": subfolder})
        stage_root = cfg["staging_dir"]
        os.makedirs(stage_root, exist_ok=True)
        os.makedirs(dest_root, exist_ok=True)

        tmp_dir = tempfile.mkdtemp(prefix="sp_live_", dir=stage_root)
        # Phase 3: 実際に書き出したファイル名から、テクスチャセット名 ->
        # ファイル名prefix の対応を逆算して記録する(_safe_name() による
        # Maya側の予測とのズレを無くすため)。
        export_prefix_updates = {}
        try:
            export_config = self._build_export_config(tmp_dir, preview, dirty_stack_ids)
            result = export.export_project_textures(export_config)

            if result.status == export.ExportStatus.Cancelled:
                self._emit_status("書き出しがキャンセルされました。")
                return
            if result.status != export.ExportStatus.Success:
                self._emit_status("書き出しステータス: {0}".format(result.message))

            moved_any = False
            for key_tuple, files in result.textures.items():
                # result.textures のキーは (テクスチャセット名, スタック名)
                # のタプル(Painter公式APIドキュメントで確認済み)。
                texture_set_name = None
                if isinstance(key_tuple, (tuple, list)) and key_tuple:
                    texture_set_name = key_tuple[0]
                for src in files:
                    if not os.path.isfile(src):
                        continue
                    dest = os.path.join(dest_root, os.path.basename(src))
                    if self._unchanged(src, dest):
                        self.stats["skip_count"] += 1
                        continue
                    try:
                        if os.path.exists(dest):
                            os.remove(dest)
                        os.replace(src, dest)
                        moved_any = True
                    except OSError:
                        shutil.copyfile(src, dest)
                        os.remove(src)
                        moved_any = True

                    if texture_set_name and preview:
                        base = os.path.basename(dest)
                        for suffix in CHANNEL_SUFFIXES:
                            marker = "_{0}.".format(suffix)
                            if marker in base:
                                export_prefix_updates[texture_set_name] = base.split(marker)[0]
                                break

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

            if preview:
                if moved_any:
                    flag_path = os.path.join(dest_root, "_sync_complete.flag")
                    with open(flag_path, "w", encoding="utf-8") as f:
                        f.write(str(time.time()))
                    self._emit_status("プレビュー同期完了({0}件更新)".format(
                        sum(len(v) for v in result.textures.values())
                    ))
            else:
                # Maya側がFinalフォルダの更新を検知できるよう、こちらにも
                # 同期完了フラグを書き込む(以前はpreview時にしか書いて
                # おらず、表示品質をFinalにしたままだと新しい高画質版が
                # 自動反映されない不具合があったため追加)。
                if moved_any:
                    flag_path = os.path.join(dest_root, "_sync_complete.flag")
                    with open(flag_path, "w", encoding="utf-8") as f:
                        f.write(str(time.time()))
                self._emit_status("フル解像度の最終書き出しが完了しました。")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _unchanged(self, src_path, dest_path):
        try:
            with open(src_path, "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()
        except Exception:
            return False
        prev = self.last_hashes.get(dest_path)
        self.last_hashes[dest_path] = digest
        return prev is not None and prev == digest

    def _build_export_config(self, out_dir, preview, dirty_stack_ids=None):
        cfg = self.config
        size_log2 = (
            int(cfg.get("preview_resolution_log2", 9))
            if preview
            else int(cfg.get("final_resolution_log2", 12))
        )

        configured_sets = cfg.get("texture_sets") or None

        dirty_set_names = None
        if preview and dirty_stack_ids:
            names = set()
            for stack_id in dirty_stack_ids:
                try:
                    stack = textureset.Stack(stack_id)
                    names.add(stack.material().name())
                except Exception as e:
                    self._emit_status(
                        "stack_id={0} からテクスチャセットを特定できませんでした: {1}".format(stack_id, e)
                    )
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
# GUI: ドッキングパネル
# ---------------------------------------------------------------------------

class LiveSyncPanel(QtWidgets.QWidget):

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setObjectName("SPMayaLiveSyncPanel")
        # パネルのタイトルにもバージョンを出し、ログを遡らなくても
        # 現在動作中のバージョンが一目で分かるようにする
        # (maya_live_sync.py のウィンドウタイトルと同じ方式)。
        self.setWindowTitle("Live Sync to Maya  (v{0})".format(__version__))

        layout = QtWidgets.QVBoxLayout(self)

        self.enable_checkbox = QtWidgets.QCheckBox("Live Sync を有効にする")
        self.enable_checkbox.toggled.connect(self.engine.set_enabled)
        layout.addWidget(self.enable_checkbox)

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

        layout.addWidget(self.settings_group)

        btn_row = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("設定を保存")
        self.save_btn.clicked.connect(self._on_save_clicked)
        self.sync_now_btn = QtWidgets.QPushButton("今すぐ同期")
        self.sync_now_btn.clicked.connect(self.engine.force_sync_now)
        self.wizard_btn = QtWidgets.QPushButton("セットアップウィザートを開く")
        self.wizard_btn.setToolTip("フォルダ設定・解像度などを対話形式で見直せます。")
        self.wizard_btn.clicked.connect(self.open_setup_wizard)
        self.history_btn = QtWidgets.QPushButton("同期履歴を開く")
        self.history_btn.setToolTip("SP側/Maya側共通の同期履歴ログ(テキストファイル)を開きます。")
        self.history_btn.clicked.connect(self._open_history_log)
        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.sync_now_btn)
        btn_row.addWidget(self.wizard_btn)
        btn_row.addWidget(self.history_btn)
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

        structure_group = QtWidgets.QGroupBox("テクスチャセット構成(Maya側の対応確認用)")
        structure_layout = QtWidgets.QVBoxLayout(structure_group)
        self.structure_list = QtWidgets.QListWidget()
        self.structure_list.setMaximumHeight(120)
        structure_layout.addWidget(self.structure_list)
        recheck_btn = QtWidgets.QPushButton("構成を今すぐチェック")
        recheck_btn.clicked.connect(self.engine.check_texture_set_structure)
        structure_layout.addWidget(recheck_btn)
        layout.addWidget(structure_group)

        layout.addWidget(QtWidgets.QLabel("ログ"))
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        layout.addWidget(self.log_view, stretch=1)

        self._load_values_from_config()
        self._refresh_structure_list([], [], [])

        self.engine.status_changed.connect(self.log_view.appendPlainText)
        self.engine.stats_changed.connect(self._on_stats_changed)
        self.engine.structure_changed.connect(self._refresh_structure_list)

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

    def _browse_folder(self, line_edit):
        current = line_edit.text() or "C:/"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "フォルダを選択", current)
        if selected:
            line_edit.setText(selected)

    def _load_values_from_config(self):
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

    def _on_save_clicked(self):
        selected_log2 = RESOLUTION_CHOICES[self.resolution_combo.currentIndex()][1]
        selected_final_log2 = RESOLUTION_CHOICES[self.final_resolution_combo.currentIndex()][1]
        new_values = {
            "staging_dir": self.staging_edit.text().strip(),
            "watch_dir": self.watch_edit.text().strip(),
            "final_export_dir": self.final_edit.text().strip(),
            "preview_resolution_log2": selected_log2,
            "final_resolution_log2": selected_final_log2,
            "debounce_seconds": self.debounce_spin.value(),
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

    def open_setup_wizard(self):
        wizard = SetupWizard(self.engine, self)
        if wizard.exec_() == QtWidgets.QDialog.Accepted:
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
    try:
        _engine = LiveSyncEngine()
        _panel = LiveSyncPanel(_engine)
        ui.add_dock_widget(_panel)
        if not _engine.config.get("setup_wizard_completed"):
            _panel.open_setup_wizard()
        _log("info", "SP -> Maya Live Sync (Phase 1 + Phase 2最適化 + Phase 3) を起動しました。")
    except Exception as e:
        _log("error", "プラグインの起動に失敗しました: {0}".format(e))


def close_plugin():
    global _engine, _panel
    # エンジンのイベント購読を先に解除してから参照を手放す。
    # (connect_strong は自動解放されないため、明示的な解除が必須)
    if _engine is not None:
        try:
            _engine.shutdown()
        except Exception as e:
            _log("error", "エンジンの後始末に失敗しました: {0}".format(e))
    if _panel is not None:
        ui.delete_ui_element(_panel)
        _panel = None
    _engine = None
    _log("info", "SP -> Maya Live Sync プラグインを停止しました。")
