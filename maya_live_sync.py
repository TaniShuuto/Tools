"""
maya_live_sync.py  (Phase 1: GUI版 + Phase 2: シェーダー自動生成/マッピングパネル + Phase 2最適化)
========================================================================================================
Maya 側スクリプト(B案:自前パイプライン)

Phase 1:
    監視ON/OFF・監視フォルダ・レンダラー選択・shelfボタン設置をGUIから
    行えるようにした。

Phase 2:
    SP側が検出した「known_texture_sets」をもとに、Maya側で対応する
    シェーダーネットワークが無いテクスチャセットを一覧表示し、
    ワンクリックで Arnold 用の標準的なシェーダーネットワークを
    自動生成できるようにした。

Phase 2 最適化(今回追加分・実機テスト前の自己レビューで発見した不具合修正):
    - マッピング状況を確認するだけの is_texture_set_mapped() が、内部で
      「設定を保存する」処理(監視の停止・再起動を伴う)を呼び出して
      しまっていた。単に一覧を表示したいだけなのに毎回監視が再起動される
      不具合だったため、監視を再起動しない専用の保存関数に分離した。
    - シェーダー生成中に例外が発生した場合、作成途中のノードがシーンに
      残ってしまう問題があったため、生成した全ノードを記録しておき、
      失敗時には自動的にロールバック(削除)するようにした。
    - 同名のシェーダー/シェーディンググループが既に存在する場合、Mayaが
      自動的に別名にリネームしてしまい、意図しないノードができる恐れが
      あったため、事前にチェックして名前が衝突する場合は生成前にエラーを
      出すようにした。
    - どのチャンネルを生成するかをGUI上のチェックボックスで選べるように
      した(すべてのテクスチャセットにHeight/Emissiveが必要とは限らない
      ため)。
    - displacementShaderのscale属性を、存在すればベストエフォートで
      設定できるようにした(Arnoldでの実際の見え方は環境依存のため、
      要現場検証)。

    共通して、known_texture_sets はSP側のPhase 2最適化によりプロジェクト
    単位で管理されるようになったため、Maya側もそれに合わせて
    known_texture_sets_by_project から現在のプロジェクトに対応する一覧を
    参照する(ただし現状Mayaはどの.spp由来かを直接知る手段がないため、
    設定ファイル内の全プロジェクト分をまとめて表示する簡易対応とする。
    複数プロジェクトを横断して作業する場合は誤って別プロジェクトの
    テクスチャセットが表示されうる点に注意。詳細は企画書を参照)。

Phase 3(実機テストのフィードバックを受けた恒久対応 + GUI直感化):
    - AEfileTextureReloadCmd(MEL)への依存をやめ、fileTextureNameを
      一旦空にしてから書き戻す方式でテクスチャを強制再読込するように
      変更した(アトリビュートエディタ未使用環境での失敗を解消)。
    - Height変位のセンタリング漏れを修正(remapValueで0-1を-0.5-0.5に
      変換してからdisplacementShaderへ接続)。
    - SP側が書き込む active_project_key を優先して参照するようにし、
      複数SPプロジェクト混在時の一覧の曖昧さを解消した(active_project_key
      が無い場合は従来通りの全プロジェクト合算にフォールバック)。
    - SP側が記録する texture_set_export_prefix を優先して使うようにし、
      _safe_name() の予測とのズレを解消した。
    - colorSpace設定を複数の候補名を順に試す方式にし、カラーマネジメント
      設定(ACES/カスタムOCIO等)による失敗に対して頑健にした。
    - 初回セットアップウィザード(QWizard)を追加し、監視フォルダ・
      レンダラー設定と接続テストを対話形式で行えるようにした。
"""

import os
import re
import json
import datetime

import maya.cmds as cmds
import maya.OpenMayaUI as omui

try:
    from PySide2 import QtCore, QtWidgets
    from shiboken2 import wrapInstance
except ImportError:
    from PySide6 import QtCore, QtWidgets
    from shiboken6 import wrapInstance

from maya.app.general.mayaMixin import MayaQWidgetDockableMixin


CONFIG_DIR = "C:/SPMayaLiveSync"
CONFIG_PATH = os.path.join(CONFIG_DIR, "live_sync_config.json")

DEFAULT_CONFIG = {
    "watch_dir": "C:/SPMayaLiveSync/live",
    # Phase 6: SP側が「今すぐ同期」または「プロジェクト保存」時に高画質版を
    # 書き出すフォルダ。ライブ監視の対象ではなく、手動での品質切り替え
    # (switch_texture_quality())でのみ参照する。
    "final_export_dir": "C:/SPMayaLiveSync/final",
    "renderer": "arnold",
    "file_format": "png",
    "raw_colorspace_suffixes": ["Roughness", "Metallic", "Normal", "Height", "AO"],
    "known_texture_sets_by_project": {},
    "texture_set_shading_engine_map": {},
    # Phase 3: SP側が「今開いているプロジェクト」を書き込むキー。
    # これが分かれば、複数プロジェクトを横断して作業した場合でも
    # 別プロジェクトのテクスチャセットが一覧に混在しなくなる。
    "active_project_key": None,
    # Phase 3: SP側が実際に書き出したファイル名のprefix
    # (テクスチャセット名 -> prefix文字列)。分かっていればこちらを
    # 最優先で使い、_safe_name() による予測はフォールバックとする。
    "texture_set_export_prefix": {},
    "setup_wizard_completed": False,
    # Phase 5: 前回終了時の監視ON/OFF状態を覚えておき、次回起動時に
    # 自動的に復元する(毎回手動でONを押す手間を無くすため)。
    "watch_enabled": False,
}

# Phase 3: file ノードのcolorSpaceに設定する値の候補。カラーマネジメント
# 設定(ACES/OCIOカスタム構成等)によって使える名称が異なるため、
# 前から順に試して最初に成功したものを採用する(要現場検証)。
RAW_COLORSPACE_CANDIDATES = ["Raw", "Utility - Raw", "Non-Color Data", "Utility - Linear - sRGB"]
SRGB_COLORSPACE_CANDIDATES = ["sRGB", "Utility - sRGB - Texture", "sRGB Texture", "scene-linear Rec.709-sRGB"]

RENDERER_CHOICES = ["arnold", "vray", "redshift", "none"]

CHANNEL_SUFFIXES = ["BaseColor", "Roughness", "Metallic", "Normal", "Height", "Emissive"]

PLACE2D_ATTR_PAIRS = [
    ("coverage", "coverage"), ("translateFrame", "translateFrame"),
    ("rotateFrame", "rotateFrame"), ("mirrorU", "mirrorU"), ("mirrorV", "mirrorV"),
    ("stagger", "stagger"), ("wrapU", "wrapU"), ("wrapV", "wrapV"),
    ("repeatUV", "repeatUV"), ("offset", "offset"), ("rotateUV", "rotateUV"),
    ("noiseUV", "noiseUV"), ("vertexUvOne", "vertexUvOne"),
    ("vertexUvTwo", "vertexUvTwo"), ("vertexUvThree", "vertexUvThree"),
    ("vertexCameraOne", "vertexCameraOne"),
]


def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


# ---------------------------------------------------------------------------
# Phase 5: 同期履歴ログ(SP側と共通のファイルに追記する)
# ---------------------------------------------------------------------------

HISTORY_LOG_PATH = os.path.join(CONFIG_DIR, "livesync_history.log")
_HISTORY_MAX_BYTES = 512 * 1024  # 500KB。超えたら直近1000行のみ残す。


def _append_history(source, text):
    """SP/Maya共通の同期履歴ログに1行追記する。無駄に複雑な仕組みに
    せず、通常は追記のみ(安価)、肥大化した場合だけ間引く(高価だが
    稀にしか発生しない)。失敗しても同期処理自体は継続できるよう、
    例外は握りつぶす。
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        line = "[{0}] [{1}] {2}\n".format(_now(), source, text)
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
# Phase 5: 複数Mayaセッションの検知(非ブロッキングな通知のみ)
# ---------------------------------------------------------------------------

SESSION_LOCK_PATH = os.path.join(CONFIG_DIR, "maya_session.lock")


def _write_session_lock():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SESSION_LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started_at": _now()}, f)
    except Exception:
        pass


def _clear_session_lock_if_own():
    try:
        if os.path.isfile(SESSION_LOCK_PATH):
            with open(SESSION_LOCK_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("pid") == os.getpid():
                os.remove(SESSION_LOCK_PATH)
    except Exception:
        pass


def _check_other_session():
    """他のMayaプロセスが既に監視中とみられる場合、その情報を返す。
    プロセスが実際に生きているかまでは確認しない軽量な実装であり、
    誤検知があっても単なる通知であって動作をブロックすることはない。
    """
    try:
        if not os.path.isfile(SESSION_LOCK_PATH):
            return None
        with open(SESSION_LOCK_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("pid") == os.getpid():
            return None
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Phase 5: userSetup.py への自動登録(バックアップ付き)
# ---------------------------------------------------------------------------

REGISTER_MARKER = "# === SP_LIVE_SYNC_AUTO_REGISTER ==="


def _user_setup_path():
    return os.path.join(cmds.internalVar(userScriptDir=True), "userSetup.py")


def register_user_setup(auto_open=False):
    """userSetup.pyに maya_live_sync の import を追記する。
    既に登録済み(マーカーコメントが存在する)場合は二重登録しない。
    既存ファイルがある場合は、追記前にタイムスタンプ付きでバックアップ
    してから追記する(ユーザーの既存userSetup.py内容を壊さないため)。
    戻り値: (成功したか, メッセージ)
    """
    path = _user_setup_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = ""
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
            if REGISTER_MARKER in existing:
                return False, "既に登録済みです: {0}".format(path)
            backup_path = "{0}.bak_{1}".format(path, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(existing)

        lines = ["\n", REGISTER_MARKER + "\n", "try:\n", "    import maya_live_sync\n"]
        if auto_open:
            lines += [
                "    import maya.utils as _sp_live_sync_utils\n",
                "    _sp_live_sync_utils.executeDeferred(maya_live_sync.show_ui)\n",
            ]
        lines += ["except Exception:\n", "    pass\n"]

        with open(path, "a", encoding="utf-8") as f:
            f.writelines(lines)
        return True, "userSetup.pyに登録しました: {0}".format(path)
    except Exception as e:
        return False, "登録に失敗しました: {0}".format(e)


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
    """設定を保存する(監視の再起動は行わない、副作用の無いバージョン)。"""
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


def _all_known_texture_sets(cfg):
    """known_texture_sets_by_project の全プロジェクト分をまとめて返す
    (active_project_key が無い/古いSP側を使っている場合のフォールバック)。
    """
    by_project = cfg.get("known_texture_sets_by_project", {})
    names = set()
    for project_names in by_project.values():
        names.update(project_names)
    return sorted(names)


def _active_texture_sets(cfg):
    """Phase 3: SP側が書き込む active_project_key を優先して、現在
    アクティブなプロジェクトのテクスチャセット一覧だけを返す。
    active_project_key が無い(未対応の古い設定ファイル)、または該当
    プロジェクトの記録が無い場合は、全プロジェクト分をまとめて返す
    従来の簡易動作にフォールバックする。
    """
    by_project = cfg.get("known_texture_sets_by_project", {})
    active_key = cfg.get("active_project_key")
    if active_key and active_key in by_project:
        return sorted(by_project[active_key])
    return _all_known_texture_sets(cfg)


def _export_prefix(cfg, texture_set_name):
    """テクスチャセット名から、実際のエクスポートファイル名のprefixを
    求める。SP側が実際に書き出した結果から記録したprefix
    (texture_set_export_prefix)が分かっていればそれを最優先で使い、
    まだ一度もエクスポートされていない場合のみ _safe_name() による
    予測値にフォールバックする(スペースや日本語を含む名前でのズレを
    防ぐため)。
    """
    prefix_map = cfg.get("texture_set_export_prefix", {})
    return prefix_map.get(texture_set_name) or _safe_name(texture_set_name)


# ---------------------------------------------------------------------------
# 監視エンジン本体
# ---------------------------------------------------------------------------

class LiveSyncWatcher(QtCore.QObject):

    status_changed = QtCore.Signal(str)
    stats_changed = QtCore.Signal(dict)

    def __init__(self, parent=None):
        super(LiveSyncWatcher, self).__init__(parent)
        self.config = load_config()
        self.enabled = False
        self._last_flag_mtime_watch = 0.0
        self._last_flag_mtime_final = 0.0
        # Phase 6: 現在file ノードがプレビュー(監視フォルダ)と高画質版
        # (Finalフォルダ)のどちらを参照しているか。自動切り替えは行わず、
        # switch_texture_quality() の明示的な呼び出しでのみ変化する。
        self.using_final_quality = False

        self.stats = {
            "reload_count": 0,
            "last_reload_at": None,
            "last_node_count": 0,
        }

        self.fs_watcher = QtCore.QFileSystemWatcher(self)
        self.fs_watcher.directoryChanged.connect(self._on_dir_changed)

        self.debounce_timer = QtCore.QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(400)
        self.debounce_timer.timeout.connect(self._process_pending_changes)

    def _emit_status(self, text):
        line = "[{0}] {1}".format(_now(), text)
        print("[LiveSync] {0}".format(text))
        self.status_changed.emit(line)
        _append_history("Maya", text)

    # -- 設定 -----------------------------------------------------------

    def reload_config(self):
        self.config = load_config()
        self._emit_status("設定を再読込しました。")

    def apply_and_save_config(self, new_values):
        """ユーザー操作(監視フォルダ変更等)による保存。監視の再起動を伴う。"""
        self.config.update(new_values)
        save_config(new_values)
        self._emit_status("設定を保存しました。")
        if self.enabled:
            self.stop()
            self.start()

    def save_mapping_only(self, new_values):
        """マッピング情報等、監視の再起動が不要な軽微な保存。
        Phase 2 最適化: is_texture_set_mapped() のような「確認のついでに
        記録する」処理から監視の停止/再開を誘発しないよう分離した。
        """
        self.config.update(new_values)
        save_config(new_values)

    # -- 開始/停止 --------------------------------------------------------

    def start(self):
        other = _check_other_session()
        if other:
            self._emit_status(
                "警告: 他のMayaセッション(PID {0}, 開始 {1})も同時に監視している"
                "可能性があります。二重に同じフォルダを監視すると反映処理が"
                "重複するだけで実害はありませんが、念のためご確認ください。".format(
                    other.get("pid"), other.get("started_at")
                )
            )
        watch_dir = os.path.normpath(self.config["watch_dir"])
        final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        os.makedirs(watch_dir, exist_ok=True)
        os.makedirs(final_dir, exist_ok=True)
        # Finalフォルダも監視対象に加える(表示品質をFinalにしたまま
        # SP側で保存しても自動反映されない不具合の修正。プレビュー
        # フォルダと同様、更新を検知したら再読込のトリガーにする)。
        for d in (watch_dir, final_dir):
            if d not in self.fs_watcher.directories():
                ok = self.fs_watcher.addPath(d)
                if not ok:
                    self._emit_status("警告: 監視フォルダの登録に失敗しました: {0}".format(d))
        self.enabled = True
        _write_session_lock()
        save_config({"watch_enabled": True})
        self.config["watch_enabled"] = True
        self._emit_status("監視を開始しました: {0}".format(watch_dir))

    def stop(self):
        for d in list(self.fs_watcher.directories()):
            self.fs_watcher.removePath(d)
        self.debounce_timer.stop()
        self.enabled = False
        _clear_session_lock_if_own()
        save_config({"watch_enabled": False})
        self.config["watch_enabled"] = False
        self._emit_status("監視を停止しました。")

    # -- イベントハンドラ ---------------------------------------------------

    def _on_dir_changed(self, path):
        if not self.enabled:
            return
        if path not in self.fs_watcher.directories():
            self.fs_watcher.addPath(path)
        self.debounce_timer.start()

    def _process_pending_changes(self):
        watch_dir = os.path.normpath(self.config["watch_dir"])
        final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])

        watch_flag = os.path.join(watch_dir, "_sync_complete.flag")
        if os.path.isfile(watch_flag):
            try:
                mtime = os.path.getmtime(watch_flag)
            except OSError:
                mtime = None
            if mtime is not None and mtime > self._last_flag_mtime_watch:
                self._last_flag_mtime_watch = mtime
                self.reload_textures()

        # Finalフォルダ側の完了フラグ。表示品質がFinalの間のみ、
        # 該当ノードを強制再読込する(プレビュー表示中はFinalの
        # 更新を反映する必要が無いため何もしない)。
        final_flag = os.path.join(final_dir, "_sync_complete.flag")
        if os.path.isfile(final_flag):
            try:
                mtime = os.path.getmtime(final_flag)
            except OSError:
                mtime = None
            if mtime is not None and mtime > self._last_flag_mtime_final:
                self._last_flag_mtime_final = mtime
                if self.using_final_quality:
                    self.reload_final_textures()

    # -- 再読込処理 ---------------------------------------------------------

    def reload_textures(self):
        watch_dir = os.path.normpath(self.config["watch_dir"])
        file_nodes = cmds.ls(type="file") or []
        if not file_nodes:
            self._emit_status("シーン内に file ノードが見つかりません。")
            return

        reloaded = []
        # v2.2 修正: stateWithoutFlush は「これから設定したいUndo有効/無効の
        # 値」を渡す引数であり、Trueが有効化・Falseが無効化にあたる。
        # 従来は開始時にTrue(有効化)、finally節でFalse(無効化)を渡しており、
        # この関数を一度でも呼ぶとUndoがオフのまま戻らなくなるバグがあった
        # (実機で "The undo queue is turned off" として再現・確認済み)。
        # 呼び出し前の実際の状態を保存しておき、finally節ではその値へ
        # 確実に復帰させる(呼び出し元が既にUndoをオフにしていた場合でも
        # 壊さないようにするため、決め打ちのTrueには戻さない)。
        prev_undo_state = cmds.undoInfo(query=True, stateWithoutFlush=True)
        cmds.undoInfo(stateWithoutFlush=True)
        try:
            for node in file_nodes:
                try:
                    tex_path = cmds.getAttr(node + ".fileTextureName")
                except Exception:
                    continue
                if not tex_path:
                    continue
                tex_dir = os.path.normpath(os.path.dirname(tex_path))
                if tex_dir != watch_dir:
                    continue
                try:
                    # AEfileTextureReloadCmd (MEL) はアトリビュートエディタが
                    # 一度でも開かれてMELが遅延ソースされていないと
                    # 「プロシージャが見つかりません」になる不安定な依存先
                    # だったため使用をやめた。
                    # 代わりに fileTextureName を一旦空にしてから同じパスへ
                    # 戻すことで、値が実際に変化したとMayaに認識させ、
                    # ディスク上の画像を強制的に再読込させる(パスが同一の
                    # ままだと setAttr が実質no-opになり再読込されないため)。
                    cmds.setAttr(node + ".fileTextureName", "", type="string")
                    cmds.setAttr(node + ".fileTextureName", tex_path, type="string")
                    reloaded.append(node)
                except Exception as e:
                    self._emit_status("再読込に失敗: {0} ({1})".format(node, e))

            if reloaded:
                self._flush_renderer_caches()
                self._flush_viewport_cache()
                self.stats["reload_count"] += 1
                self.stats["last_reload_at"] = _now()
                self.stats["last_node_count"] = len(reloaded)
                self.stats_changed.emit(dict(self.stats))
                self._emit_status("{0} 個のテクスチャを再読込しました。".format(len(reloaded)))
            elif not self.using_final_quality:
                # Phase 6: 高画質表示中は file ノードが監視フォルダを
                # 参照していないのが正常な状態のため、この場合は
                # 「見つからない」という誤解を招くログを出さない。
                self._emit_status("監視フォルダを参照する file ノードが見つかりませんでした。")
        finally:
            cmds.undoInfo(stateWithoutFlush=prev_undo_state)

    def reload_final_textures(self):
        """Finalフォルダ配下のfileノードを強制再読込する
        (reload_textures()のFinal版)。表示品質をFinalにしたまま
        SP側で保存・高画質書き出しが行われても、Live⇔Finalを
        往復切り替えしなくても自動的に反映されるようにするためのもの。
        呼び出し元(_process_pending_changes)でusing_final_qualityが
        True の場合のみ呼ばれる想定。
        """
        final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        file_nodes = cmds.ls(type="file") or []
        if not file_nodes:
            return

        reloaded = []
        # v2.2 修正: reload_textures() と同様の理由で、呼び出し前の状態を
        # 保存してから復帰させる(詳細は reload_textures() のコメント参照)。
        prev_undo_state = cmds.undoInfo(query=True, stateWithoutFlush=True)
        cmds.undoInfo(stateWithoutFlush=True)
        try:
            for node in file_nodes:
                try:
                    tex_path = cmds.getAttr(node + ".fileTextureName")
                except Exception:
                    continue
                if not tex_path:
                    continue
                tex_dir = os.path.normpath(os.path.dirname(tex_path))
                if tex_dir != final_dir:
                    continue
                try:
                    cmds.setAttr(node + ".fileTextureName", "", type="string")
                    cmds.setAttr(node + ".fileTextureName", tex_path, type="string")
                    reloaded.append(node)
                except Exception as e:
                    self._emit_status("再読込に失敗: {0} ({1})".format(node, e))

            if reloaded:
                self._flush_renderer_caches()
                self._flush_viewport_cache()
                self.stats["reload_count"] += 1
                self.stats["last_reload_at"] = _now()
                self.stats["last_node_count"] = len(reloaded)
                self.stats_changed.emit(dict(self.stats))
                self._emit_status("{0} 個の高画質(Final)テクスチャを再読込しました。".format(len(reloaded)))
        finally:
            cmds.undoInfo(stateWithoutFlush=prev_undo_state)

    def _flush_renderer_caches(self):
        renderer = (self.config.get("renderer") or "none").lower()
        if renderer == "arnold":
            if cmds.pluginInfo("mtoa", q=True, loaded=True):
                try:
                    cmds.arnoldFlushCache(textures=True)
                except Exception as e:
                    self._emit_status("Arnold キャッシュフラッシュに失敗: {0}".format(e))
            else:
                self._emit_status("mtoa 未ロードのため Arnold キャッシュフラッシュをスキップしました。")
        elif renderer == "redshift":
            self._emit_status("Redshift: mtime自動検知に依存(要検証)。")
        elif renderer == "vray":
            self._emit_status("V-Ray: 専用フラッシュコマンド未確定。IPR再起動を推奨(要検証)。")

    def _flush_viewport_cache(self):
        try:
            cmds.ogs(reset=True)
        except Exception as e:
            self._emit_status("ogs(reset=True) に失敗: {0}".format(e))
        cmds.refresh(force=True)

    # -- Phase 2: テクスチャセット構造への対応 -------------------------------

    def get_known_texture_sets(self):
        return _active_texture_sets(self.config)

    def get_active_project_label(self):
        """マテリアル構造タブに表示する「今どのプロジェクトの一覧を
        見ているか」の説明文を返す。SP側が未対応(active_project_keyが
        無い)場合はその旨を明示し、誤解を防ぐ。
        """
        by_project = self.config.get("known_texture_sets_by_project", {})
        active_key = self.config.get("active_project_key")
        if active_key and active_key in by_project:
            name = os.path.basename(active_key) if active_key != "__unsaved__" else "(未保存のプロジェクト)"
            return "現在のSPプロジェクト: {0}".format(name)
        return "SP側のプロジェクト情報が未取得のため、記録済み全プロジェクト分を表示中"

    def get_shading_engine_map(self):
        return dict(self.config.get("texture_set_shading_engine_map", {}))

    def _managed_dirs(self):
        """ライブ同期パイプラインが把握しているフォルダ(監視用の
        プレビューフォルダ・保存時の高画質Finalフォルダ)の集合を返す。
        Phase 6: 品質切り替え後もマッピング状況・孤立ノード判定が
        正しく機能するよう、両方のフォルダを対象にする。
        """
        dirs = set()
        watch_dir = self.config.get("watch_dir")
        if watch_dir:
            dirs.add(os.path.normpath(watch_dir))
        final_dir = self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"]
        if final_dir:
            dirs.add(os.path.normpath(final_dir))
        return dirs

    def is_texture_set_mapped(self, name):
        """このテクスチャセットに対応するシェーディンググループが
        シーン内に既に存在するかどうかを判定する(副作用として監視を
        再起動しない: Phase 2最適化で save_mapping_only() に変更済み)。
        """
        mapping = self.get_shading_engine_map()
        sg_name = mapping.get(name)
        if sg_name and cmds.objExists(sg_name):
            return True, sg_name

        managed_dirs = self._managed_dirs()
        prefix = _export_prefix(self.config, name) + "_"
        for node in cmds.ls(type="file") or []:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if not tex_path:
                continue
            if os.path.normpath(os.path.dirname(tex_path)) not in managed_dirs:
                continue
            if os.path.basename(tex_path).startswith(prefix):
                sgs = cmds.listConnections(node, type="shadingEngine") or []
                found_sg = sgs[0] if sgs else None
                if found_sg:
                    mapping[name] = found_sg
                    self.save_mapping_only({"texture_set_shading_engine_map": mapping})
                return True, found_sg
        return False, None

    def find_orphan_file_nodes(self):
        managed_dirs = self._managed_dirs()
        known = set(self.get_known_texture_sets())
        orphans = []
        for node in cmds.ls(type="file") or []:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if not tex_path:
                continue
            if os.path.normpath(os.path.dirname(tex_path)) not in managed_dirs:
                continue
            base = os.path.basename(tex_path)
            matched = False
            for name in known:
                if base.startswith(_export_prefix(self.config, name) + "_"):
                    matched = True
                    break
            if not matched:
                orphans.append(node)
        return orphans

    def detect_current_quality(self):
        """シーン内のfileノードが実際に監視フォルダ(プレビュー)と
        Finalフォルダのどちらを参照しているかを調べる。
        Maya再起動をまたぐとGUIの表示品質ボタンは初期化されるが、
        fileノードのパス自体はシーンファイルに保存されたまま残るため、
        起動直後にここで実態を検出し、ボタンの見た目を合わせることで
        「ボタンはプレビュー表示なのに実際はFinalのまま切り替えられない」
        というズレを防ぐ。
        戻り値: Finalのみを参照していればTrue、監視フォルダのみを参照
        していればFalse、fileノードが無い/両方混在している等で判別
        できない場合はNone。
        """
        final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        watch_dir = os.path.normpath(self.config.get("watch_dir") or "")
        found_final = False
        found_watch = False
        for node in cmds.ls(type="file") or []:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if not tex_path:
                continue
            d = os.path.normpath(os.path.dirname(tex_path))
            if d == final_dir:
                found_final = True
            elif d == watch_dir:
                found_watch = True
        if found_final and not found_watch:
            return True
        if found_watch and not found_final:
            return False
        return None

    def switch_texture_quality(self, use_final):
        """file ノードを監視フォルダ(プレビュー)⇔Finalフォルダ(高画質)の
        間で明示的に切り替える。両フォルダで書き出しファイル名
        (prefix_suffix.ext)は共通のため、フォルダ部分だけを付け替える。
        自動切り替えは行わない(切り替わったタイミングが分かりにくく
        なることを避けるため、常にGUIのボタン操作からのみ呼び出される)。
        戻り値: 実際に切り替えたノード数。
        """
        watch_dir = os.path.normpath(self.config["watch_dir"])
        final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        dest_dir = final_dir if use_final else watch_dir
        managed_dirs = {watch_dir, final_dir}

        # Phase 6不具合修正: 以前はボタンの状態から推測した「切り替え元」
        # フォルダのノードだけを対象にしていたが、Maya再起動でボタンの
        # 見た目は初期化されてもシーン内のfileノードのパスはそのまま
        # 保存されているため、両者がズレて一切切り替えられなくなる
        # 不具合があった。監視フォルダ・Finalフォルダのどちらを参照して
        # いるノードも対象に含め、常に希望の状態(dest_dir)へ収束させる。
        nodes = []
        for node in cmds.ls(type="file") or []:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if tex_path and os.path.normpath(os.path.dirname(tex_path)) in managed_dirs:
                nodes.append(node)

        if not nodes:
            self._emit_status(
                "切り替え対象の file ノードが見つかりませんでした"
                "(監視フォルダ・Finalフォルダのいずれを参照しているノードもありません)。"
            )
            return 0

        switched = []
        missing = []
        already = 0
        # v2.2 修正: reload_textures() と同様の理由で、呼び出し前の状態を
        # 保存してから復帰させる(詳細は reload_textures() のコメント参照)。
        prev_undo_state = cmds.undoInfo(query=True, stateWithoutFlush=True)
        cmds.undoInfo(stateWithoutFlush=True)
        try:
            for node in nodes:
                try:
                    tex_path = cmds.getAttr(node + ".fileTextureName")
                except Exception:
                    continue
                if os.path.normpath(os.path.dirname(tex_path)) == dest_dir:
                    already += 1
                    continue
                base = os.path.basename(tex_path)
                new_path = os.path.join(dest_dir, base)
                if not os.path.isfile(new_path):
                    missing.append(base)
                    continue
                try:
                    cmds.setAttr(node + ".fileTextureName", "", type="string")
                    cmds.setAttr(node + ".fileTextureName", new_path, type="string")
                    switched.append(node)
                except Exception as e:
                    self._emit_status("切り替えに失敗: {0} ({1})".format(node, e))
        finally:
            cmds.undoInfo(stateWithoutFlush=prev_undo_state)

        if switched or already:
            self._flush_renderer_caches()
            self._flush_viewport_cache()
            self.using_final_quality = use_final
            label = "高画質版(Final)" if use_final else "リアルタイムプレビュー"
            if switched and already:
                self._emit_status(
                    "{0} 個のノードを{1}に切り替えました({2} 個は既にこの状態でした)。".format(
                        len(switched), label, already
                    )
                )
            elif switched:
                self._emit_status("{0} 個のノードを{1}に切り替えました。".format(len(switched), label))
            else:
                self._emit_status("{0} 個のノードは既に{1}でした。".format(already, label))
        if missing:
            hint = "SP側でプロジェクトを保存すると高画質版が生成されます。" if use_final else ""
            shown = ", ".join(missing[:5]) + ("..." if len(missing) > 5 else "")
            self._emit_status(
                "{0} 件は切り替え先に画像が見つからなかったため据え置きました({1})。{2}".format(
                    len(missing), shown, hint
                )
            )
        return len(switched) + already

    def create_shader_network(self, texture_set_name, channels=None):
        """Arnold 用の標準シェーダーネットワークを自動生成する。
        戻り値: 作成した shadingEngine 名。
        どのジオメトリに割り当てるかはここでは行わない(手動作業)。
        失敗時には作成済みのノードをロールバック(削除)する。
        """
        renderer = (self.config.get("renderer") or "none").lower()
        if renderer != "arnold":
            raise RuntimeError(
                "現在自動生成に対応しているのは Arnold のみです"
                "(V-Ray / Redshift は Phase 3 で対応予定)。"
            )
        if not cmds.pluginInfo("mtoa", q=True, loaded=True):
            raise RuntimeError("mtoa プラグインがロードされていません。")

        channels = channels or CHANNEL_SUFFIXES
        # ノード名の生成には _safe_name() (Mayaのノード名として妥当な
        # 文字列)を使う。実テクスチャファイルのパス生成には、SP側が
        # 実際に書き出した prefix が分かっていればそちらを使う
        # (export_prefix)。両者は初回エクスポート前は同じ値になりうるが、
        # スペース・日本語等を含む名前では異なる場合がある。
        safe = _safe_name(texture_set_name)
        export_prefix = _export_prefix(self.config, texture_set_name)

        # Phase 2 最適化: 名前の衝突を事前チェックする。Mayaに自動リネーム
        # させると意図しない名前のノードができてマッピングが崩れるため。
        mat_name = "{0}_mat".format(safe)
        sg_name = "{0}SG".format(safe)
        if cmds.objExists(mat_name) or cmds.objExists(sg_name):
            raise RuntimeError(
                "'{0}' または '{1}' という名前のノードが既に存在します。"
                "手動で整理してから再実行してください。".format(mat_name, sg_name)
            )

        watch_dir = os.path.normpath(self.config["watch_dir"])
        ext = self.config.get("file_format", "png")
        raw_suffixes = set(self.config.get("raw_colorspace_suffixes", []))

        created_nodes = []  # Phase 2 最適化: 失敗時ロールバック用

        try:
            mat = cmds.shadingNode("aiStandardSurface", asShader=True, name=mat_name)
            created_nodes.append(mat)
            sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=sg_name)
            created_nodes.append(sg)
            cmds.connectAttr(mat + ".outColor", sg + ".surfaceShader", force=True)

            file_nodes = {}
            for suffix in channels:
                file_node = cmds.shadingNode("file", asTexture=True, isColorManaged=True,
                                              name="{0}_{1}_file".format(safe, suffix))
                created_nodes.append(file_node)
                tex_path = os.path.join(watch_dir, "{0}_{1}.{2}".format(export_prefix, suffix, ext))
                cmds.setAttr(file_node + ".fileTextureName", tex_path, type="string")
                # Phase 3: colorSpaceの名称はカラーマネジメント設定(Legacy/
                # ACES/カスタムOCIO等)によって使える文字列が異なるため、
                # 候補を順に試し、すべて失敗した場合のみログに警告を出す。
                candidates = RAW_COLORSPACE_CANDIDATES if suffix in raw_suffixes else SRGB_COLORSPACE_CANDIDATES
                applied_space = None
                for candidate in candidates:
                    try:
                        cmds.setAttr(file_node + ".colorSpace", candidate, type="string")
                        applied_space = candidate
                        break
                    except Exception:
                        continue
                if applied_space is None:
                    self._emit_status(
                        "警告: {0} のcolorSpace設定に失敗しました(候補: {1})。"
                        "現在のカラーマネジメント設定に合わせて手動で設定してください。".format(
                            file_node, ", ".join(candidates)
                        )
                    )
                p2d = self._connect_place2d(file_node)
                created_nodes.append(p2d)
                file_nodes[suffix] = file_node

            if "BaseColor" in file_nodes:
                cmds.connectAttr(file_nodes["BaseColor"] + ".outColor", mat + ".baseColor", force=True)
            if "Roughness" in file_nodes:
                cmds.connectAttr(file_nodes["Roughness"] + ".outColorR", mat + ".specularRoughness", force=True)
            if "Metallic" in file_nodes:
                cmds.connectAttr(file_nodes["Metallic"] + ".outColorR", mat + ".metalness", force=True)
            if "Emissive" in file_nodes:
                cmds.connectAttr(file_nodes["Emissive"] + ".outColor", mat + ".emissionColor", force=True)
                cmds.setAttr(mat + ".emission", 1.0)

            if "Normal" in file_nodes:
                try:
                    normal_map_node = cmds.shadingNode("aiNormalMap", asUtility=True, name="{0}_normalMap".format(safe))
                    created_nodes.append(normal_map_node)
                    cmds.connectAttr(file_nodes["Normal"] + ".outColor", normal_map_node + ".input", force=True)
                    cmds.connectAttr(normal_map_node + ".outValue", mat + ".normalCamera", force=True)
                except Exception as e:
                    self._emit_status("aiNormalMap の接続に失敗しました(手動接続が必要です): {0}".format(e))

            if "Height" in file_nodes:
                try:
                    # SPのHeightチャンネルは 0-1 の範囲で「0.5 = 変位なし」を
                    # 意味するグレースケール値だが、displacementShader の
                    # .displacement は入力値をそのまま変位量として扱うため、
                    # 生の値を直結すると常に "0.5 * scale" 分だけ全体が
                    # 一方向に膨らみ、さらに値のムラがそのまま歪みとして
                    # 出てしまう(実機テストで確認された不具合)。
                    # remapValue で 0-1 -> -0.5-0.5 に変換し、0.5(中間値)を
                    # 変位ゼロの基準点として再センタリングしてから接続する。
                    remap_node = cmds.shadingNode("remapValue", asUtility=True, name="{0}_heightRemap".format(safe))
                    created_nodes.append(remap_node)
                    cmds.setAttr(remap_node + ".inputMin", 0.0)
                    cmds.setAttr(remap_node + ".inputMax", 1.0)
                    cmds.setAttr(remap_node + ".outputMin", -0.5)
                    cmds.setAttr(remap_node + ".outputMax", 0.5)
                    cmds.connectAttr(file_nodes["Height"] + ".outColorR", remap_node + ".inputValue", force=True)

                    disp_node = cmds.shadingNode("displacementShader", asShader=True, name="{0}_disp".format(safe))
                    created_nodes.append(disp_node)
                    cmds.connectAttr(remap_node + ".outValue", disp_node + ".displacement", force=True)
                    cmds.connectAttr(disp_node + ".displacement", sg + ".displacementShader", force=True)
                    # ベストエフォート: scale属性が存在する場合のみ設定する。
                    # 実際の変位「量」(何cm膨らむか等)はモデルのスケールや
                    # 素材の質感次第で変わるため、まずは控えめな値から始め、
                    # レンダービューを見ながら現場で調整することを推奨する
                    # (要現場検証。0.5に再センタリングしても、値自体の
                    # 大小=変位量はscaleで決まる)。
                    if cmds.attributeQuery("scale", node=disp_node, exists=True):
                        cmds.setAttr(disp_node + ".scale", 0.1)

                    # Phase 3' 追加: シェーダー側の再センタリングだけでは
                    # 不十分で、割り当て先メッシュのシェイプノード側で
                    # Arnold Subdivision(aiSubdivType/aiSubdivIterations)と
                    # aiDispPadding を別途設定しないと、変位が「頂点だけが
                    # 動く歪み」や「クリッピングによる欠け」として見える
                    # ことが実機テストで確認された(スクリプト側はどの
                    # メッシュに割り当てるか関知しないため自動設定しない)。
                    self._emit_status(
                        "注意: Heightチャンネルの変位を正しく表示するには、"
                        "割り当て先メッシュのシェイプノードで Arnold の "
                        "Subdivision(aiSubdivType=catclark等)と aiDispPadding "
                        "を別途設定してください。未設定のままだと、変位が "
                        "頂点単位のカクカクした歪みに見えることがあります。"
                    )
                except Exception as e:
                    self._emit_status("displacementShader の接続に失敗しました(手動接続が必要です): {0}".format(e))

        except Exception:
            # ロールバック: 作成済みのノードを削除して中途半端な状態を残さない
            existing = [n for n in created_nodes if n and cmds.objExists(n)]
            if existing:
                try:
                    cmds.delete(existing)
                except Exception:
                    pass
            raise

        mapping = self.get_shading_engine_map()
        mapping[texture_set_name] = sg
        self.save_mapping_only({"texture_set_shading_engine_map": mapping})

        self._emit_status(
            "'{0}' 用のシェーダーを生成しました({1})。"
            "ジオメトリへの割り当ては手動で行ってください。".format(texture_set_name, sg)
        )
        return sg

    def _connect_place2d(self, file_node):
        p2d = cmds.shadingNode("place2dTexture", asUtility=True, name=file_node + "_p2d")
        for src, dst in PLACE2D_ATTR_PAIRS:
            try:
                cmds.connectAttr(p2d + "." + src, file_node + "." + dst, force=True)
            except Exception:
                pass
        cmds.connectAttr(p2d + ".outUV", file_node + ".uvCoord", force=True)
        cmds.connectAttr(p2d + ".outUvFilterSize", file_node + ".uvFilterSize", force=True)
        return p2d


# ---------------------------------------------------------------------------
# Phase 3: 初回セットアップウィザード
# ---------------------------------------------------------------------------
#
# 他人にこのパイプラインを共有した際、SP側との対応関係(特に監視フォルダの
# パスを一致させる必要があること)が分かりにくいため、対話形式で案内する
# ウィザード。接続テストページで、SP側から実際にファイルが届いているかを
# その場で確認できるようにしてある。

class SetupWizard(QtWidgets.QWizard):

    def __init__(self, watcher, parent=None):
        super(SetupWizard, self).__init__(parent)
        self.watcher = watcher
        self.setWindowTitle("SP Live Sync 初期セットアップ")
        self.setMinimumSize(560, 420)
        # QWizardの既定スタイル(Windowsだと ModernStyle/AeroStyle)は、
        # 上部に白背景のバナー領域を独自描画するため、Mayaのダーク
        # テーマと合わずに浮いて見える。ClassicStyleはこのバナー領域を
        # 描画しないため、ホストアプリのテーマに馴染みやすい。
        self.setWizardStyle(QtWidgets.QWizard.ClassicStyle)

        self.addPage(self._welcome_page())
        self.addPage(self._folder_page())
        self.addPage(self._test_page())

    def _welcome_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("ようこそ")
        layout = QtWidgets.QVBoxLayout(page)
        label = QtWidgets.QLabel(
            "このウィザードでは、Substance Painterから送られてくるテクスチャを"
            "Mayaへ自動反映するための最低限の設定を行います。\n\n"
            "「監視フォルダ」は、Substance Painter側のセットアップウィザードに"
            "表示された「監視フォルダ」と完全に同じパスを指定してください。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        return page

    def _folder_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("監視フォルダとレンダラー")
        layout = QtWidgets.QFormLayout(page)

        cfg = self.watcher.config
        row = QtWidgets.QHBoxLayout()
        self.watch_edit = QtWidgets.QLineEdit(cfg.get("watch_dir", ""))
        browse_btn = QtWidgets.QPushButton("参照...")
        browse_btn.clicked.connect(self._browse)
        row.addWidget(self.watch_edit)
        row.addWidget(browse_btn)
        row_widget = QtWidgets.QWidget()
        row_widget.setLayout(row)
        layout.addRow("監視フォルダ (★ SP側と一致させる)", row_widget)

        self.renderer_combo = QtWidgets.QComboBox()
        self.renderer_combo.addItems(RENDERER_CHOICES)
        renderer = cfg.get("renderer", "arnold")
        if renderer in RENDERER_CHOICES:
            self.renderer_combo.setCurrentIndex(RENDERER_CHOICES.index(renderer))
        layout.addRow("レンダラー", self.renderer_combo)

        return page

    def _browse(self):
        current = self.watch_edit.text() or "C:/"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "監視フォルダを選択", current)
        if selected:
            self.watch_edit.setText(selected)

    def _test_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("接続テスト")
        layout = QtWidgets.QVBoxLayout(page)
        self.test_result_label = QtWidgets.QLabel("「テストを実行」を押してください。")
        self.test_result_label.setWordWrap(True)
        layout.addWidget(self.test_result_label)
        test_btn = QtWidgets.QPushButton("テストを実行")
        test_btn.clicked.connect(self._run_test)
        layout.addWidget(test_btn)
        layout.addStretch(1)
        return page

    def _run_test(self):
        watch_dir = self.watch_edit.text().strip()
        if not watch_dir:
            self.test_result_label.setText("監視フォルダが未指定です。")
            return
        norm = os.path.normpath(watch_dir)
        if not os.path.isdir(norm):
            self.test_result_label.setText(
                "フォルダが存在しません: {0}\n"
                "SP側で一度「今すぐ同期」を実行すると自動的に作成されます。".format(norm)
            )
            return
        try:
            entries = os.listdir(norm)
        except Exception as e:
            self.test_result_label.setText("フォルダの読み取りに失敗しました: {0}".format(e))
            return
        flag_exists = "_sync_complete.flag" in entries
        image_exts = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr")
        texture_count = len([e for e in entries if e.lower().endswith(image_exts)])
        if flag_exists or texture_count > 0:
            self.test_result_label.setText(
                "OK: フォルダを確認できました。テクスチャファイル {0} 件、"
                "同期完了フラグ: {1}\n\n"
                "「完了」を押して設定を保存してください。".format(
                    texture_count, "あり" if flag_exists else "なし(まだSP側で同期されていない可能性があります)"
                )
            )
        else:
            self.test_result_label.setText(
                "フォルダは存在しますが、まだテクスチャファイルがありません。\n"
                "SP側で「Live Sync を有効にする」をONにし、一度「今すぐ同期」を"
                "実行してから、このテストを再実行してください。"
            )

    def accept(self):
        new_values = {
            "watch_dir": self.watch_edit.text().strip(),
            "renderer": self.renderer_combo.currentText(),
            "setup_wizard_completed": True,
        }
        if not new_values["watch_dir"]:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "監視フォルダが未指定です。")
            return
        self.watcher.apply_and_save_config(new_values)
        super(SetupWizard, self).accept()


# ---------------------------------------------------------------------------
# GUI: ドッキング可能なウィンドウ
# ---------------------------------------------------------------------------

# ユーザーセットアップ/shelfボタンから呼ばれる、
# ウィンドウのシングルトンインスタンス。
_window_instance = None


class LiveSyncWindow(MayaQWidgetDockableMixin, QtWidgets.QWidget):

    def __init__(self, parent=None):
        super(LiveSyncWindow, self).__init__(parent=parent)
        self.setObjectName("MayaLiveSyncWindow")
        self.setWindowTitle("SP Live Sync")

        self.watcher = LiveSyncWatcher(self)
        self.channel_checkboxes = {}

        outer_layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        outer_layout.addWidget(tabs)

        # --- タブ1: 同期設定 ---
        sync_tab = QtWidgets.QWidget()
        tabs.addTab(sync_tab, "同期")
        layout = QtWidgets.QVBoxLayout(sync_tab)

        self.enable_btn = QtWidgets.QPushButton("監視: OFF")
        self.enable_btn.setCheckable(True)
        self.enable_btn.toggled.connect(self._on_toggle)
        layout.addWidget(self.enable_btn)

        # Phase 6: 表示品質の手動切り替え(プレビュー⇔Final高画質)。
        # 自動切り替えは行わない(どちらを表示中か分かりにくくなる
        # ことを避けるため)。切り替え後は、明示的にこのボタンを
        # もう一度押すまでリアルタイム更新の対象から外れる。
        self.quality_btn = QtWidgets.QPushButton("表示品質: プレビュー(リアルタイム)")
        self.quality_btn.setCheckable(True)
        self.quality_btn.setToolTip(
            "ONにすると、SP側で保存時に書き出された高画質版(Finalフォルダ)に"
            "file ノードを切り替えます。OFFに戻すとリアルタイムプレビューに戻ります。"
        )
        self.quality_btn.toggled.connect(self._on_quality_toggled)
        layout.addWidget(self.quality_btn)

        form = QtWidgets.QFormLayout()
        row = QtWidgets.QHBoxLayout()
        self.watch_edit = QtWidgets.QLineEdit()
        browse_btn = QtWidgets.QPushButton("参照...")
        browse_btn.clicked.connect(self._browse_watch_dir)
        row.addWidget(self.watch_edit)
        row.addWidget(browse_btn)
        row_widget = QtWidgets.QWidget()
        row_widget.setLayout(row)
        form.addRow("監視フォルダ", row_widget)

        self.renderer_combo = QtWidgets.QComboBox()
        self.renderer_combo.addItems(RENDERER_CHOICES)
        form.addRow("レンダラー", self.renderer_combo)
        layout.addLayout(form)

        # 日常的に使う操作(設定保存・履歴確認)と、初回導入時に一度だけ
        # 行えばよい操作(shelf設置・ウィザート・userSetup.py登録)を
        # 視覚的に分離する。後者は使用頻度が低いため「その他の設定」に
        # まとめ、主要な操作列を圧迫しないようにした。
        btn_row = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("設定を保存")
        self.save_btn.clicked.connect(self._on_save_clicked)
        self.history_btn = QtWidgets.QPushButton("同期履歴を開く")
        self.history_btn.setToolTip("SP側/Maya側共通の同期履歴ログ(テキストファイル)を開きます。")
        self.history_btn.clicked.connect(self._open_history_log)

        self.more_btn = QtWidgets.QToolButton()
        self.more_btn.setText("その他の設定")
        self.more_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.more_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        more_menu = QtWidgets.QMenu(self.more_btn)

        wizard_action = more_menu.addAction("セットアップウィザートを開く")
        wizard_action.setToolTip("監視フォルダ・レンダラーの設定と接続テストを対話形式で見直せます。")
        wizard_action.triggered.connect(self.open_setup_wizard)

        shelf_action = more_menu.addAction("shelfボタンを設置")
        shelf_action.triggered.connect(self._install_shelf_button)

        register_action = more_menu.addAction("userSetup.pyに登録")
        register_action.setToolTip("Maya起動時に自動でこのスクリプトをimportするよう登録します(任意)。")
        register_action.triggered.connect(self._on_register_user_setup)

        self.more_btn.setMenu(more_menu)

        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.history_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.more_btn)
        layout.addLayout(btn_row)

        stats_group = QtWidgets.QGroupBox("反映状況")
        stats_layout = QtWidgets.QFormLayout(stats_group)
        self.last_reload_label = QtWidgets.QLabel("-")
        self.reload_count_label = QtWidgets.QLabel("0")
        self.node_count_label = QtWidgets.QLabel("0")
        stats_layout.addRow("最終反映時刻", self.last_reload_label)
        stats_layout.addRow("反映回数", self.reload_count_label)
        stats_layout.addRow("直近の対象ノード数", self.node_count_label)
        layout.addWidget(stats_group)

        layout.addWidget(QtWidgets.QLabel("ログ"))
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        layout.addWidget(self.log_view, stretch=1)

        # --- タブ2: マテリアル構造(Phase 2) ---
        material_tab = QtWidgets.QWidget()
        tabs.addTab(material_tab, "マテリアル構造")
        mat_layout = QtWidgets.QVBoxLayout(material_tab)

        mat_layout.addWidget(QtWidgets.QLabel(
            "SP側で検出されたテクスチャセットと、Maya側の対応状況です。\n"
            "「未対応」の行を選択して自動生成すると、Arnoldの標準シェーダー\n"
            "ネットワークが作成されます(ジオメトリへの割り当ては手動で行ってください)。"
        ))
        # Phase 3: 今どのSPプロジェクトの一覧を見ているかを明示する
        # (複数プロジェクト混在時の誤解を防ぐため)。
        self.active_project_label = QtWidgets.QLabel("-")
        self.active_project_label.setStyleSheet("color: gray;")
        mat_layout.addWidget(self.active_project_label)

        # Phase 2 最適化: 生成するチャンネルを選択できるようにする
        channel_group = QtWidgets.QGroupBox("生成するチャンネル")
        channel_layout = QtWidgets.QHBoxLayout(channel_group)
        for suffix in CHANNEL_SUFFIXES:
            cb = QtWidgets.QCheckBox(suffix)
            cb.setChecked(True)
            channel_layout.addWidget(cb)
            self.channel_checkboxes[suffix] = cb
        mat_layout.addWidget(channel_group)

        self.material_table = QtWidgets.QTableWidget(0, 3)
        self.material_table.setHorizontalHeaderLabels(["テクスチャセット", "状態", "シェーディンググループ"])
        self.material_table.horizontalHeader().setStretchLastSection(True)
        self.material_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.material_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        mat_layout.addWidget(self.material_table)

        mat_btn_row = QtWidgets.QHBoxLayout()
        self.refresh_material_btn = QtWidgets.QPushButton("状態を更新")
        self.refresh_material_btn.clicked.connect(self._refresh_material_table)
        self.create_shader_btn = QtWidgets.QPushButton("選択行のシェーダーを自動生成")
        self.create_shader_btn.clicked.connect(self._on_create_shader_clicked)
        mat_btn_row.addWidget(self.refresh_material_btn)
        mat_btn_row.addWidget(self.create_shader_btn)
        mat_layout.addLayout(mat_btn_row)

        mat_layout.addWidget(QtWidgets.QLabel("孤立ノード(リネーム/削除で取り残されたfileノード。自動削除はしません)"))
        self.orphan_list = QtWidgets.QListWidget()
        self.orphan_list.setMaximumHeight(100)
        mat_layout.addWidget(self.orphan_list)
        self.refresh_orphan_btn = QtWidgets.QPushButton("孤立ノードを一覧表示")
        self.refresh_orphan_btn.clicked.connect(self._refresh_orphan_list)
        mat_layout.addWidget(self.refresh_orphan_btn)

        self._load_values_from_config()

        # Phase 6不具合修正: 起動時にシーンの実際の状態を検出し、
        # 表示品質ボタンをそれに合わせておく(常にプレビュー扱いで
        # 初期化すると、実際はFinalのままの場合に切り替え不能になる)。
        detected_final = self.watcher.detect_current_quality()
        if detected_final is not None:
            self.watcher.using_final_quality = detected_final
            self.quality_btn.blockSignals(True)
            self.quality_btn.setChecked(detected_final)
            self.quality_btn.setText(
                "表示品質: {0}".format("高画質(Final)" if detected_final else "プレビュー(リアルタイム)")
            )
            self.quality_btn.blockSignals(False)

        self.watcher.status_changed.connect(self.log_view.appendPlainText)
        self.watcher.stats_changed.connect(self._on_stats_changed)

        self._refresh_material_table()

        # Phase 5: 前回終了時に監視がONだった場合は自動的に再開する
        # (毎回手動でONを押す手間を無くすため)。setChecked(True)が
        # _on_toggle経由でwatcher.start()を呼ぶ。
        if self.watcher.config.get("watch_enabled"):
            self.enable_btn.setChecked(True)

    def _load_values_from_config(self):
        cfg = self.watcher.config
        self.watch_edit.setText(cfg.get("watch_dir", ""))
        renderer = cfg.get("renderer", "arnold")
        if renderer in RENDERER_CHOICES:
            self.renderer_combo.setCurrentIndex(RENDERER_CHOICES.index(renderer))

    def _browse_watch_dir(self):
        current = self.watch_edit.text() or "C:/"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "監視フォルダを選択", current)
        if selected:
            self.watch_edit.setText(selected)

    def _on_toggle(self, checked):
        self.enable_btn.setText("監視: {0}".format("ON" if checked else "OFF"))
        if checked:
            self.watcher.start()
        else:
            self.watcher.stop()

    def _on_quality_toggled(self, checked):
        switched = self.watcher.switch_texture_quality(checked)
        if switched:
            self.quality_btn.setText(
                "表示品質: {0}".format("高画質(Final)" if checked else "プレビュー(リアルタイム)")
            )
        elif checked:
            # 切り替えが1件も行われなかった場合、ボタンだけがONの
            # 見た目になって実態とズレるのを避けるため元に戻す。
            self.quality_btn.blockSignals(True)
            self.quality_btn.setChecked(False)
            self.quality_btn.blockSignals(False)

    def _on_save_clicked(self):
        new_values = {
            "watch_dir": self.watch_edit.text().strip(),
            "renderer": self.renderer_combo.currentText(),
        }
        if not new_values["watch_dir"]:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "監視フォルダが未指定です。")
            return
        self.watcher.apply_and_save_config(new_values)

    def open_setup_wizard(self):
        wizard = SetupWizard(self.watcher, self)
        if wizard.exec_() == QtWidgets.QDialog.Accepted:
            self._load_values_from_config()
            self._refresh_material_table()

    def _refresh_material_table(self):
        self.active_project_label.setText(self.watcher.get_active_project_label())
        known = self.watcher.get_known_texture_sets()
        self.material_table.setRowCount(len(known))
        for row, name in enumerate(known):
            mapped, sg_name = self.watcher.is_texture_set_mapped(name)
            status = "対応済み" if mapped else "未対応"
            self.material_table.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            self.material_table.setItem(row, 1, QtWidgets.QTableWidgetItem(status))
            self.material_table.setItem(row, 2, QtWidgets.QTableWidgetItem(sg_name or "-"))

    def _on_create_shader_clicked(self):
        rows = sorted(set(idx.row() for idx in self.material_table.selectedIndexes()))
        if not rows:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "テクスチャセットの行を選択してください。")
            return
        channels = [suffix for suffix, cb in self.channel_checkboxes.items() if cb.isChecked()]
        created, failed = [], []
        for row in rows:
            item = self.material_table.item(row, 0)
            if item is None:
                continue
            name = item.text()
            try:
                self.watcher.create_shader_network(name, channels=channels)
                created.append(name)
            except Exception as e:
                failed.append("{0}: {1}".format(name, e))
        self._refresh_material_table()
        if created:
            self.watcher._emit_status("シェーダーを生成しました: {0}".format(", ".join(created)))
        if failed:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "生成に失敗しました:\n" + "\n".join(failed))

    def _refresh_orphan_list(self):
        self.orphan_list.clear()
        for node in self.watcher.find_orphan_file_nodes():
            self.orphan_list.addItem(node)

    def _on_stats_changed(self, stats):
        self.last_reload_label.setText(stats.get("last_reload_at") or "-")
        self.reload_count_label.setText(str(stats.get("reload_count", 0)))
        self.node_count_label.setText(str(stats.get("last_node_count", 0)))

    def _install_shelf_button(self):
        try:
            current_shelf = cmds.tabLayout("ShelfLayout", query=True, selectTab=True)
        except Exception:
            current_shelf = None

        if not current_shelf:
            QtWidgets.QMessageBox.warning(
                self, "Live Sync",
                "アクティブなshelfが見つかりませんでした。Mayaのshelf UIが表示された状態で実行してください。"
            )
            return

        command = "import maya_live_sync\nmaya_live_sync.show_ui()\n"
        try:
            cmds.shelfButton(
                parent=current_shelf,
                label="SPLiveSync",
                annotation="SP Live Sync ウィンドウを開く",
                image1="render_arnold.png",
                command=command,
                sourceType="python",
            )
            self.log_view.appendPlainText("[{0}] shelfボタンを設置しました: {1}".format(_now(), current_shelf))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "shelfボタンの設置に失敗しました: {0}".format(e))

    def _open_history_log(self):
        if not os.path.isfile(HISTORY_LOG_PATH):
            QtWidgets.QMessageBox.information(
                self, "Live Sync", "同期履歴はまだありません(監視を開始すると記録され始めます)。"
            )
            return
        try:
            os.startfile(HISTORY_LOG_PATH)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "履歴ファイルを開けませんでした: {0}\nパス: {1}".format(e, HISTORY_LOG_PATH))

    def _on_register_user_setup(self):
        reply = QtWidgets.QMessageBox.question(
            self, "Live Sync",
            "Maya起動時にこのウィンドウも自動的に開きますか?\n"
            "「いいえ」の場合、起動時にモジュールをimportするだけで\n"
            "ウィンドウは自動表示されません(shelfボタン等から手動で開けます)。",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        auto_open = (reply == QtWidgets.QMessageBox.Yes)
        ok, message = register_user_setup(auto_open=auto_open)
        self.log_view.appendPlainText("[{0}] {1}".format(_now(), message))
        if ok:
            QtWidgets.QMessageBox.information(self, "Live Sync", message)
        else:
            QtWidgets.QMessageBox.warning(self, "Live Sync", message)

# ---------------------------------------------------------------------------
# 外部公開API: userSetup.py や shelfボタンから呼ばれるエントリーポイント
# ---------------------------------------------------------------------------

def show_ui():
    global _window_instance
    first_creation = False
    if _window_instance is None:
        _window_instance = LiveSyncWindow()
        first_creation = True
    try:
        _window_instance.show(dockable=True)
    except Exception:
        pass
    if first_creation:
        if not _window_instance.watcher.config.get("setup_wizard_completed"):
            _window_instance.open_setup_wizard()
    return _window_instance
