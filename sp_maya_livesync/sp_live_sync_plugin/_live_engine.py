"""同期エンジン(LiveSyncEngine)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os
import time
import uuid
import shutil
import hashlib
import functools
import tempfile
import datetime

import substance_painter.event as event
import substance_painter.export as export
import substance_painter.project as project
import substance_painter.textureset as textureset
import substance_painter.exception as sp_exception

from ._qt import QtCore
from ._version import __version__
from ._base import (
    _append_history,
    _is_unsaved_project_key,
    _log,
    _replace_with_retry,
    _update_nested_config_entry,
    build_export_result,
    build_mesh_record,
    export_status_label,
    export_status_name,
    is_valid_rebase_request_id,
    load_config,
    publish_export_outputs,
    save_config,
)
from ._project_key import _current_project_key, _project_subfolder_name
from ._rebase_protocol import REBASE_EXPORT_DEFERRED_STATUS, resolve_uvremap_root
from ._rebase_coordinator import RebaseCoordinator
from ._rebase_job import ExportAttempt
from ._painter_rebase_api import (
    PainterRebaseApi,
    RebaseEngineHooks,
    default_rebase_job_factory,
    make_rebase_approval_dialog,
)

__all__ = [
    "_FINAL_EXPORT_INITIAL_DELAY_MS",
    "_FINAL_EXPORT_QUEUED_DELAY_MS",
    "_FINAL_EXPORT_RETRY_BASE_MS",
    "_FINAL_EXPORT_RETRY_MAX_MS",
    "_FINAL_EXPORT_MAX_RETRIES",
    "_FINAL_EXPORT_GIVEUP_FOLLOWUP_MS",
    "_MESH_RECORD_DELAY_MS",
    "_MESH_RECORD_RETRY_DELAY_MS",
    "_MESH_RECORD_MAX_RETRIES",
    "CHANNEL_SUFFIXES",
    "LiveSyncEngine",
]


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

# ProjectEditionEntered のあと、名前付き単発タイマーでメッシュを記録する
# までの待ち(ミリ秒)。0 はイベントループの次の回。
# この待ちで last_imported_mesh_path() が埋まるかは要実機確認。
_MESH_RECORD_DELAY_MS = 0
# 2026.10.18(1.3.0): パスが空、または Painter が busy のときは 500ms 後に
# 最大 2 回だけ再試行する。メインスレッドは sleep しない。
_MESH_RECORD_RETRY_DELAY_MS = 500
_MESH_RECORD_MAX_RETRIES = 2

# Phase 3: 書き出しファイル名の判定・Maya側との橋渡しで共通して使う
# チャンネルsuffix一覧(maya_live_sync.py の CHANNEL_SUFFIXES と対応)。
CHANNEL_SUFFIXES = ["BaseColor", "Roughness", "Metallic", "Normal", "Height", "Emissive"]


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
                                _replace_with_retry(src, dest)
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
                                _replace_with_retry(tmp_dest, dest)
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
            rebase_root = None
            if (
                not preview
                and result_obj.get("ok") is True
                and is_valid_rebase_request_id(export_request_id)
            ):
                rebase_root = resolve_uvremap_root(cfg, subfolder)
            publish_export_outputs(
                dest_root,
                result_obj,
                write_flag,
                rebase_root=rebase_root,
                rebase_request_id=export_request_id,
            )
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
