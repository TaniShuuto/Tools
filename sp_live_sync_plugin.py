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
"""

import os
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
    "texture_set_shading_engine_map": {},
    # Phase 3: 複数SPプロジェクトを横断して作業した場合に、Maya側が
    # 「どのプロジェクトのテクスチャセット一覧を見ればよいか」を機械的に
    # 判別できるよう、現在アクティブなプロジェクトのキーを共有する。
    "active_project_key": None,
    # Phase 3: SP側が実際に書き出したファイル名のprefixを記録する。
    # Maya側はテクスチャセット名を自前で安全化(_safe_name)して予測する
    # 代わりに、この値があれば最優先で使うことで、スペースや日本語を
    # 含む名前でのズレを防ぐ。
    "texture_set_export_prefix": {},
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
    """
    try:
        path = project.file_path()
    except Exception:
        path = None
    return path or "__unsaved__"


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
            # texture_set_export_prefix に残ったままだと不要なエントリが
            # 溜まり続けるため、あわせて掃除する(実害は無いが、設定
            # ファイルが際限なく肥大化するのを防ぐ)。
            if confirmed_removed:
                prefix_map = dict(self.config.get("texture_set_export_prefix", {}))
                removed_any_prefix = False
                for name in confirmed_removed:
                    if name in prefix_map:
                        del prefix_map[name]
                        removed_any_prefix = True
                if removed_any_prefix:
                    self.config["texture_set_export_prefix"] = prefix_map
                    save_partial["texture_set_export_prefix"] = prefix_map

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
        dest_root = cfg["watch_dir"] if preview else cfg["final_export_dir"]
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
                current_map = dict(self.config.get("texture_set_export_prefix", {}))
                if any(current_map.get(k) != v for k, v in export_prefix_updates.items()):
                    current_map.update(export_prefix_updates)
                    self.config["texture_set_export_prefix"] = current_map
                    save_config({"texture_set_export_prefix": current_map})

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

        maps = [
            {"fileName": "$textureSet_BaseColor", "channels": [
                {"destChannel": c, "srcChannel": c, "srcMapType": "documentMap", "srcMapName": "basecolor"}
                for c in ("R", "G", "B")
            ]},
            {"fileName": "$textureSet_Roughness", "channels": [
                {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "roughness"}
            ]},
            {"fileName": "$textureSet_Metallic", "channels": [
                {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "metallic"}
            ]},
            {"fileName": "$textureSet_Normal", "channels": [
                {"destChannel": c, "srcChannel": c, "srcMapType": "virtualMap", "srcMapName": "Normal_OpenGL"}
                for c in ("R", "G", "B")
            ]},
            {"fileName": "$textureSet_Height", "channels": [
                {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "height"}
            ]},
            {"fileName": "$textureSet_Emissive", "channels": [
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
        for suffix in cfg.get("raw_colorspace_suffixes", []):
            export_parameters.append({
                "filter": {"outputMaps": ["$textureSet_{0}".format(suffix)]},
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
        self.setWindowTitle("Live Sync to Maya")

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
    if _panel is not None:
        ui.delete_ui_element(_panel)
        _panel = None
    _engine = None
    _log("info", "SP -> Maya Live Sync プラグインを停止しました。")
