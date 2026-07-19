"""
install.py  -  SP -> Maya Live Sync : Maya drag-and-drop installer
================================================================================
使い方:
    この install.py を、maya_live_sync.py / sp_to_aiStandardSurface.py と
    同じフォルダに置いたまま、Maya のビューポート(3D画面)へドラッグ&ドロップ
    するだけ。

やっていること(自動):
    1. 同じフォルダにある下記ファイルを Maya の scripts フォルダへコピー
         - maya_live_sync.py            (ライブ同期 本体・必須)
         - sp_to_aiStandardSurface.py   (Final取り込み・マテリアル一括生成・任意)
         - udim_setup.py                (UDIMテクスチャ自動セットアップ・任意)
       いずれも「同じフォルダに無ければスキップ」なので、必要なツールだけを
       このファイルと同じフォルダに置いた状態でドラッグ&ドロップすればよい。
    2. maya_live_sync_icon.png / sp_to_aiStandardSurface_icon.png /
       udim_setup_icon.png が同じフォルダにあれば、Maya の per-user
       icons フォルダへコピーし、それぞれ対応するシェルフボタン
       (LiveSync / aiSS / UDIM)の見た目に使う(無ければ Maya標準の
       Pythonアイコンにフォールバックするので、無くてもインストール
       自体は問題なく完了する)。
    3. アクティブなシェルフに起動ボタンを追加(コピーできたツールの分だけ)
         - [LiveSync] ... ライブ同期ウィンドウを開く
         - [aiSS]     ... マテリアル生成ツールを開き、Final書き出しフォルダを
                          自動入力する(= Final取り込みへ誘導)
         - [UDIM]     ... UDIMテクスチャ自動セットアップGUIを開く
    4. Maya 起動時に LiveSync が自動読み込みされるよう userSetup.py へ登録
    5. 完了メッセージを表示

対象: Maya 2022 以降(ドラッグ&ドロップ実行自体は 2017 Update 3 以降で対応)

設計メモ:
    sp_to_aiStandardSurface.py 本体には一切手を加えていない(拡張性・既存機能を
    そのまま維持するため)。Final フォルダの自動入力は、このインストーラが登録
    するシェルフボタンの起動コマンド側だけで行っている。フォルダのパスは
    maya_live_sync.load_config() が返す共有設定 (final_export_dir) から取得する
    ため、SP 側の設定と常に一致する。

NOTE:
    UI文字列は、Windows + 日本語ロケール環境での文字化けを避けるため
    ASCII(英語)に統一しています(コメントは UTF-8 のまま影響ありません)。

変更履歴:
    1.0.2 (2026.07.20):
        - シェルフボタンのツールヒント(annotation)を、ホバーしただけで
          機能が伝わるよう全ボタンで文言を統一・具体化。
          「ツール名: 何をするか」の2段構成に揃えた。
            - LiveSync: "SP -> Maya Live Sync : open the live sync
              window" (何が起きるか不明瞭) ->
              "Live Sync: auto-reload SP textures in Maya as you
              paint (open the monitor window)" (SPで塗った内容が
              Mayaへ自動反映される、という挙動を明記)
            - aiSS  : 語順を他ボタンと揃え、"aiSS:" 接頭辞を追加
            - UDIM  : 同上。UDIMタイル単位で処理される点を明記
    1.0.1 (2026.07.20):
        - LiveSync / aiSS シェルフボタン専用のアイコン画像
          (maya_live_sync_icon.png / sp_to_aiStandardSurface_icon.png)
          に対応。従来はこの2つのボタンだけ Maya 標準アイコンのままで
          あった(UDIMボタンのみ専用アイコン対応済みだった)ため統一した。
          専用アイコンが同じフォルダに無い場合は、これまで通り標準の
          pythonFamily.png にフォールバックする。
    1.0.0:
        - 初版(SemVer導入)。
"""

__version__ = "1.0.2"

import os
import sys
import json
import shutil
import importlib
import traceback

import maya.cmds as cmds
import maya.mel as mel


# ドラッグ&ドロップ実行のために Maya が要求するエントリポイント。
# (Maya 2017 Update 3 以降で必要。中身は下の _run() を呼ぶだけ)
def onMayaDroppedPythonFile(*args, **kwargs):
    try:
        _run()
    except Exception as e:
        traceback.print_exc()
        cmds.confirmDialog(
            title="Live Sync Installer",
            message="Install failed:\n{0}\n\nSee the Script Editor for details.".format(e),
            button=["OK"],
        )


# --- コピー対象ファイル ---------------------------------------------------
#  required=True のものが見つからない場合はインストールを中止する。
#  required=False のものは、見つからなければ警告のみでスキップする。
TOOL_FILES = [
    {"name": "maya_live_sync.py", "required": True},
    {"name": "sp_to_aiStandardSurface.py", "required": False},
    {"name": "udim_setup.py", "required": False},
]

# --- シェルフアイコン画像(任意) --------------------------------------------
#  同じフォルダに置いてあれば Maya の per-user icons フォルダへコピーし、
#  対応するシェルフボタンの見た目に使う。見つからない場合は Maya 標準の
#  pythonFamily.png にフォールバックするため、アイコンが無くても
#  インストール自体は問題なく完走する。
ICON_FILES = [
    {"name": "maya_live_sync_icon.png", "required": False},
    {"name": "sp_to_aiStandardSurface_icon.png", "required": False},
    {"name": "udim_setup_icon.png", "required": False},
]

# --- シェルフボタン: LiveSync ---------------------------------------------
LIVESYNC_LABEL = "LiveSync"
LIVESYNC_ANNOTATION = "Live Sync: auto-reload SP textures in Maya as you paint (open the monitor window)"
LIVESYNC_COMMAND = "import maya_live_sync\nmaya_live_sync.show_ui()"
LIVESYNC_ICON_NAME = "maya_live_sync_icon.png"

# --- シェルフボタン: aiSS (Final取り込みへ誘導) ---------------------------
#  ツールを開いた直後に、共有設定の final_export_dir をフォルダ入力欄へ
#  流し込む。これにより「フォルダを探す」手間が消え、Final を書き出した後の
#  取り込みへ自然に誘導できる。sp_to_aiStandardSurface.py には手を加えず、
#  公開されている _gui_state["dir_field"] を使って外側から設定している。
AISS_LABEL = "aiSS"
AISS_ANNOTATION = "aiSS: build aiStandardSurface materials from the SP Final export (folder auto-filled)"
AISS_COMMAND = (
    "import os\n"
    "import maya.cmds as cmds\n"
    "import sp_to_aiStandardSurface as _sp\n"
    "_sp.show_ui()\n"
    "try:\n"
    "    import maya_live_sync as _mls\n"
    "    _cfg = _mls.load_config()\n"
    "    _final = _cfg.get('final_export_dir') or ''\n"
    "    _sub = _cfg.get('active_final_subfolder')\n"
"    # 複数プロジェクト対応: アクティブなプロジェクトのサブフォルダが\n"
    "    # 分かっていれば、それを取り込み先として自動入力する。\n"
    "    if _final and _sub:\n"
    "        _target = os.path.join(_final, _sub)\n"
    "    else:\n"
    "        _target = _final\n"
    "    _fld = _sp._gui_state.get('dir_field')\n"
    "    if _target and _fld and cmds.textField(_fld, exists=True):\n"
    "        cmds.textField(_fld, edit=True, text=_target)\n"
    "        print('[aiSS] Final folder set to: ' + _target)\n"
    "        print('[aiSS] Tip: export Final in SP first, then click Scan -> Create.')\n"
    "    else:\n"
    "        print('[aiSS] Final folder not set automatically; enter it manually.')\n"
    "except Exception as _e:\n"
    "    print('[aiSS] Could not pre-fill Final folder: ' + str(_e))\n"
)
AISS_ICON_NAME = "sp_to_aiStandardSurface_icon.png"

# --- シェルフボタン: UDIM (UDIMテクスチャ自動セットアップ) -----------------
UDIM_LABEL = "UDIM"
UDIM_ANNOTATION = "UDIM: scan a texture folder and auto-build aiStandardSurface materials per UDIM tile"
UDIM_COMMAND = "import udim_setup\nudim_setup.launch_gui()"
UDIM_ICON_NAME = "udim_setup_icon.png"


def _scripts_dir():
    """Maya のユーザ scripts フォルダ(全バージョン共通)のパスを返す。"""
    return cmds.internalVar(userScriptDir=True)


def _icons_dir():
    """
    Maya のユーザ prefs 配下の icons フォルダのパスを返す。
    shelfButton の image フラグにファイル名だけを渡した場合、Maya は
    この icons フォルダ(と製品同梱のicons)を自動的に検索するため、
    ここへコピーしておけばフルパス指定なしでアイコンを参照できる。
    """
    return os.path.join(cmds.internalVar(userPrefDir=True), "icons")


def _copy_icons(source_dir):
    """
    ICON_FILES を Maya の icons フォルダへコピーする。
    コピーできたファイル名の集合を返す(見つからなければ空集合のまま
    静かにスキップし、呼び出し側は標準アイコンへフォールバックする)。
    """
    dst_dir = _icons_dir()
    copied = set()
    for entry in ICON_FILES:
        name = entry["name"]
        src = os.path.join(source_dir, name)
        if not os.path.isfile(src):
            print("[Live Sync Installer] Optional icon not found, skipped: {0}".format(name))
            continue
        try:
            if not os.path.isdir(dst_dir):
                os.makedirs(dst_dir)
            dst = os.path.join(dst_dir, name)
            shutil.copy2(src, dst)
            copied.add(name)
            print("[Live Sync Installer] Copied icon: {0} -> {1}".format(src, dst))
        except Exception as e:
            print("[Live Sync Installer] Icon copy failed for {0}: {1}".format(name, e))
    return copied


def _copy_tools(source_dir):
    """TOOL_FILES を scripts フォルダへコピーする。コピーした名前の一覧を返す。"""
    dst_dir = _scripts_dir()
    if not os.path.isdir(dst_dir):
        os.makedirs(dst_dir)

    copied = []
    for entry in TOOL_FILES:
        name = entry["name"]
        src = os.path.join(source_dir, name)
        if not os.path.isfile(src):
            if entry["required"]:
                raise RuntimeError(
                    "'{0}' がインストーラと同じフォルダに見つかりません。"
                    "install.py と一緒に配置してください。".format(name)
                )
            else:
                print("[Live Sync Installer] Optional file not found, skipped: {0}".format(name))
                continue
        dst = os.path.join(dst_dir, name)
        shutil.copy2(src, dst)
        copied.append(name)
        print("[Live Sync Installer] Copied: {0} -> {1}".format(src, dst))
    return copied


def _ensure_importable():
    """コピー直後のセッションでも import できるよう scripts を sys.path に追加。"""
    d = _scripts_dir()
    if d not in sys.path:
        sys.path.append(d)


def _reload_if_cached(module_name):
    """
    そのMayaセッションで以前に一度でも import 済みのモジュールは、
    sys.modules にキャッシュされたままになる。ファイルを新しい内容で
    上書きコピーしても、単に import しただけでは古いキャッシュが
    返ってしまい、"install.py を再D&Dしても更新されない"ように見える
    原因になる。importlib.reload() で明示的に再読込することで、
    ディスク上の最新コードを確実に反映させる。
    (対象モジュールが未importなら何もしない/初回はimportlib.import_moduleへ)
    """
    if module_name in sys.modules:
        try:
            importlib.reload(sys.modules[module_name])
            print("[Live Sync Installer] Reloaded cached module: {0}".format(module_name))
        except Exception as e:
            print("[Live Sync Installer] Reload failed for {0}: {1}".format(module_name, e))
    else:
        try:
            importlib.import_module(module_name)
        except Exception:
            pass  # モジュール側で読み込み時エラーがあっても、後続の登録処理には委ねる


def _collect_installed_versions():
    """
    2026.07.20(フェーズ2・バージョン整合性): TOOL_FILES に含まれる
    各モジュールの __version__ を一覧取得する。

    このMayaセッションで既に _reload_if_cached() 等を通じて
    sys.modules にロード済みのものだけを対象とする、読み取り専用の
    処理。新規に import や importlib.reload() を行うことはない
    (それらは _run() 側で既に呼び出し順序も含めて慎重に扱われている
    処理であり、ここで重複して呼ぶと不要な副作用のリスクが増えるため
    意図的に避けている)。

    戻り値: [(表示名, バージョン文字列 or None, 取得できたか), ...]
    取得できなかった場合(未import・属性なし等)は None と False を返す。
    """
    results = []
    for entry in TOOL_FILES:
        fname = entry["name"]
        module_name = os.path.splitext(fname)[0]
        version = None
        ok = False
        module = sys.modules.get(module_name)
        if module is not None:
            version = getattr(module, "__version__", None)
            ok = version is not None
        results.append((fname, version, ok))
    return results


def _release_session_lock_directly():
    """
    2026.07.16(見落とし修正): _destroy_stale_livesync_window() 内の
    watcher.stop() は、watcher.enabled(監視ボタンがON)の場合にしか
    呼ばれない。しかし再インストール自体は「ウィンドウは開いているが
    監視はOFF」の状態でも行われうるため、その場合はセッションロックが
    解放されないまま maya_live_sync が reload されてしまい、後の
    セッションで「他のセッションが監視中」という誤警告が再発する
    経路が残っていた(uninstall.py側でも同じ見落としがあり、あわせて
    修正した)。
    watcher経由に頼らず、ロックファイルを直接確認して自分自身のpidで
    あれば解放する(他プロセスのロックは誤って消さないようpid照合)。
    """
    config_dir = "C:/SPMayaLiveSync"
    lock_path = os.path.join(config_dir, "maya_session.lock")
    if not os.path.isfile(lock_path):
        return
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("pid") == os.getpid():
            os.remove(lock_path)
            print("[Live Sync Installer] Released this session's lock file directly.")
    except Exception as e:
        print("[Live Sync Installer] Could not inspect/release session lock: {0}".format(e))


def _destroy_stale_livesync_window():
    """
    2026.07.16: 再インストール時にMaya再起動を不要にするための処理。

    背景: importlib.reload() はPythonのモジュール定義(クラス・関数)を
    最新化するが、Maya本体が管理する workspaceControl(dockableウィンドウ
    の入れ物)は、Qtの objectName を通じて別途Maya側に登録されたまま
    残り続ける。この結果、reload後に show_ui() を呼んでも「新しいクラス
    定義の新規ウィンドウ」ではなく「古いworkspaceControlの再利用」に
    なることがあり、タイトルバーのバージョン表示が更新されない・
    修正内容が反映されないように見える不具合があった
    (これまでの運用では「Mayaを再起動する」ことで回避していた)。

    対策として、再インストールのたびに以下を明示的に行う:
      1. maya_live_sync モジュールから WORKSPACE_CONTROL_NAME /
         WINDOW_OBJECT_NAME を取得する(ハードコードすると将来
         maya_live_sync.py 側で名前が変わった際に追従できなくなるため、
         reload済みの実モジュールから毎回引く)。
      2. 該当する workspaceControl が存在すれば cmds.deleteUI で破棄する。
      3. maya_live_sync._window_instance も明示的に None へリセットする
         (workspaceControlを消しても、Python側が持つ古いインスタンス
         参照は自動では消えないため)。

    こうしておけば、この後の show_ui() 呼び出しは「何も残っていない
    まっさらな状態」から新しいクラス定義でウィンドウを作ることになり、
    Maya再起動をしなくても最新の修正が確実に反映される。
    初期インストール時(まだ何も存在しない)はここで何もすることがなく、
    安全に素通りする。

    2026.07.16: 完了メッセージで新旧バージョンの差分を表示できるよう、
    reload前(＝まだ古いコードのまま)の __version__ を戻り値として返す。
    取得できない場合(初回インストール等)は None を返す。
    """
    # --- [DIAG-A2] -----------------------------------------------------
    # 一次切り分け用の計装: 各破棄ステップが「実行されたか」「成功したか」
    # 「そもそも対象が存在したか」を _diag に記録する。これまでは
    # print + 握り潰しのみで、失敗してもインストール自体は成功したように
    # 見えていた。_diag は _run() 側で完了メッセージに反映し、
    # 「今回の再インストールで実際に何が壊れずに完了したか」を
    # ユーザーに必ず見える形で提示する(修正はまだ行わない)。
    _diag = {
        "old_module_found": False,
        "workspace_control_existed": None,   # None=未確認/属性なし, True/False=確認できた
        "workspace_control_destroyed": None,
        "workspace_control_destroy_error": None,
        "window_instance_existed": None,
        "scene_callbacks_found": None,
        "scene_callbacks_unregister_error": None,
        "watcher_was_enabled": None,
        "watcher_stop_error": None,
        "window_instance_reset": False,
        "window_instance_reset_error": None,
    }

    old_version = None
    try:
        import maya_live_sync
        old_version = getattr(maya_live_sync, "__version__", None)
        _diag["old_module_found"] = True
    except Exception as e:
        # maya_live_sync 自体がまだ一度もimportされていない(=初回
        # インストール)場合はここに来る。破棄すべきものが無いだけなので
        # 正常なケースとして扱う。念のためセッションロックの解放だけは
        # 試みる(前回のセッションが異常終了した場合の残骸対策)。
        print("[Live Sync Installer] No previous maya_live_sync session detected ({0}); skipping cleanup.".format(e))
        _release_session_lock_directly()
        print("[DIAG-A2] {0}".format(_diag))
        return old_version, _diag

    control_name = getattr(maya_live_sync, "WORKSPACE_CONTROL_NAME", None)
    if control_name:
        try:
            exists = cmds.workspaceControl(control_name, query=True, exists=True)
            _diag["workspace_control_existed"] = bool(exists)
            if exists:
                # --- [DIAG-C1] ---------------------------------------------
                # 一次切り分け用: deleteUI 直前の時点でQObject群がまだ
                # 生きていることを確認しておく(deleteUI自体が原因である
                # ことの対照実験)。
                try:
                    _pre_window = getattr(maya_live_sync, "_window_instance", None)
                    if hasattr(maya_live_sync, "diag_c1_check_qobject_validity"):
                        print("[DIAG-C1] deleteUI 実行前の状態:")
                        maya_live_sync.diag_c1_check_qobject_validity(_pre_window)
                except Exception as _diag_e:
                    print("[DIAG-C1] deleteUI前チェック失敗: {0}".format(_diag_e))

                cmds.deleteUI(control_name, control=True)
                _diag["workspace_control_destroyed"] = True
                print("[Live Sync Installer] Destroyed stale workspaceControl: {0}".format(control_name))

                # --- [DIAG-C1] ---------------------------------------------
                # deleteUI 直後、Python側の window/watcher/fs_watcherの
                # Qt C++実体がまだ有効か確認する。ここで既に無効(False)
                # になっていれば、「deleteUIが子オブジェクトごと即座に
                # 破棄している」という仮説が確定する。
                try:
                    if hasattr(maya_live_sync, "diag_c1_check_qobject_validity"):
                        print("[DIAG-C1] deleteUI 実行直後の状態(これがFalseなら仮説確定):")
                        maya_live_sync.diag_c1_check_qobject_validity(_pre_window)
                except Exception as _diag_e:
                    print("[DIAG-C1] deleteUI後チェック失敗: {0}".format(_diag_e))
        except Exception as e:
            _diag["workspace_control_destroyed"] = False
            _diag["workspace_control_destroy_error"] = str(e)
            print("[Live Sync Installer] Could not destroy workspaceControl '{0}': {1}".format(control_name, e))

    if hasattr(maya_live_sync, "_window_instance"):
        window = maya_live_sync._window_instance
        _diag["window_instance_existed"] = window is not None
        # 安全策: cmds.deleteUI(control=True) が workspaceControl を
        # 破棄した際、内部のQWidget(LiveSyncWindow)の closeEvent が
        # 確実に呼ばれる保証はMayaのAPI仕様上明確ではない。closeEvent
        # 側で行っているシーンコールバック(kAfterOpen/kAfterNew)の解除が
        # 万一実行されなかった場合、古いPythonオブジェクトを参照した
        # コールバックがMaya内部に残留してしまう(閉じたはずのウィンドウの
        # メソッドが、シーン切り替えのたびに呼ばれ続ける状態)。
        # これを避けるため、_window_instance を破棄する前に、closeEvent
        # と同じコールバック解除処理をここでも明示的に行っておく。
        if window is not None:
            try:
                import maya.OpenMaya as om
                cb_ids = getattr(window, "_scene_callback_ids", [])
                _diag["scene_callbacks_found"] = len(cb_ids)
                for cb_id in cb_ids:
                    try:
                        om.MMessage.removeCallback(cb_id)
                    except Exception:
                        pass
                window._scene_callback_ids = []
                print("[Live Sync Installer] Unregistered stale scene callbacks.")
            except Exception as e:
                _diag["scene_callbacks_unregister_error"] = str(e)
                print("[Live Sync Installer] Could not unregister scene callbacks: {0}".format(e))
            # 安全策: watcher(project_poll_timer/fs_watcher等)はQtの親子
            # 関係(LiveSyncWatcher(self))により、親ウィジェット破棄時に
            # 連動して破棄されるのが期待される標準動作だが、
            # workspaceControlの破棄がQt標準のウィジェット削除経路を
            # 必ず通るとは限らないため、念のため明示的に停止しておく
            # (タイマーが動いたまま参照先が壊れることによる不安定化を防ぐ)。
            try:
                watcher = getattr(window, "watcher", None)
                watcher_enabled = bool(getattr(watcher, "enabled", False)) if watcher is not None else None
                _diag["watcher_was_enabled"] = watcher_enabled
                if watcher is not None and watcher_enabled:
                    # --- [DIAG-C1] -----------------------------------------
                    # watcher.stop() 呼び出し直前の最終確認。ここで
                    # fs_watcher_valid が False なのに例外が起きるなら、
                    # 「deleteUIで既に破棄済みのオブジェクトに対して
                    # stop()内部でアクセスしている」ことが確定する。
                    try:
                        if hasattr(maya_live_sync, "diag_c1_check_qobject_validity"):
                            print("[DIAG-C1] watcher.stop() 呼び出し直前の状態:")
                            maya_live_sync.diag_c1_check_qobject_validity(window)
                    except Exception as _diag_e:
                        print("[DIAG-C1] stop()直前チェック失敗: {0}".format(_diag_e))
                    watcher.stop(reason="manual")
                    print("[Live Sync Installer] Stopped stale watcher before teardown.")
            except Exception as e:
                _diag["watcher_stop_error"] = str(e)
                print("[Live Sync Installer] Could not stop stale watcher: {0}".format(e))
        try:
            maya_live_sync._window_instance = None
            _diag["window_instance_reset"] = True
            print("[Live Sync Installer] Reset cached _window_instance reference.")
        except Exception as e:
            _diag["window_instance_reset_error"] = str(e)
            print("[Live Sync Installer] Could not reset _window_instance: {0}".format(e))

    # 2026.07.16(見落とし修正): watcher.stop() 経由のセッションロック
    # 解放は watcher.enabled が True の場合にしか行われない。監視が
    # OFFの状態で再インストールした場合でもロックを確実に解放できる
    # よう、watcher の状態に関わらずここで直接解放を試みる。
    _release_session_lock_directly()

    print("[DIAG-A2] _destroy_stale_livesync_window 結果: {0}".format(_diag))
    return old_version, _diag


def _remove_existing_shelf_buttons(label):
    """
    2026.07.16: 再インストールのたびにシェルフボタンが重複して増えて
    いく問題への対処。同じラベルを持つ既存の shelfButton をすべて
    削除してから、呼び出し側が新しいボタンを追加できるようにする。

    以前は「先に手動でボタンを消してからD&Dする」という手順が
    必要だったが、これにより不要になる。

    2026.07.16(再修正): 当初は「今アクティブなシェルフタブ」だけを
    走査していたが、これだと「前回インストール時と別のシェルフタブを
    アクティブにした状態で再インストールする」ケースで、前回タブに
    残った古いボタンが削除されないまま、新しいタブにも新規ボタンが
    追加されてしまい、結果的に複数のシェルフタブにボタンが分散して
    残る不具合があった(「重複防止」を謳いながら別の形で重複が起きる、
    という見落とし)。対策として、アクティブなタブに限定せず、
    $gShelfTopLevel 配下の全シェルフタブを横断して同名ボタンを探し、
    見つかった分はすべて削除するようにした。
    """
    try:
        shelf_top_level = mel.eval("$temp = $gShelfTopLevel")
        all_shelves = cmds.tabLayout(shelf_top_level, query=True, childArray=True) or []
    except Exception as e:
        print("[Live Sync Installer] Could not enumerate shelf tabs: {0}".format(e))
        return

    for shelf in all_shelves:
        try:
            children = cmds.shelfLayout(shelf, query=True, childArray=True) or []
        except Exception:
            # そのタブが空、またはshelfLayoutでない場合はスキップ。
            continue
        for child in children:
            try:
                if cmds.objectTypeUI(child) != "shelfButton":
                    continue
                existing_label = cmds.shelfButton(child, query=True, label=True)
            except Exception:
                continue
            if existing_label == label:
                try:
                    cmds.deleteUI(child)
                    print("[Live Sync Installer] Removed existing shelf button '{0}' from shelf: {1}".format(label, shelf))
                except Exception as e:
                    print("[Live Sync Installer] Could not remove existing shelf button '{0}': {1}".format(label, e))


def _add_shelf_button(label, annotation, command, image="pythonFamily.png", overlay_label=None):
    """アクティブなシェルフにボタンを1つ追加する。成否を返す。
    2026.07.16: 追加の前に、全シェルフタブを横断して同じラベルの
    既存ボタンがあれば削除する(再インストールのたびにボタンが
    重複して増えるのを防ぐため。アクティブなタブに限定すると、
    別タブに残った古いボタンを見落とすため全タブを対象にしている)。

    image: アイコンのファイル名(フルパス不要)。Maya の icons 検索パス
    (_icons_dir() でコピーした先を含む)から解決される。専用アイコンが
    見つからなかった場合は呼び出し側で標準の pythonFamily.png を渡すこと。

    2026.07.20: label はシェルフボタンの内部識別名 + ツールヒント
    (マウスホバー時に annotation と併せて表示される文字列)としては
    機能するが、アイコン画像の上に文字を重ねて表示するには Maya では
    別途 imageOverlayLabel フラグの指定が必要(label だけではアイコン
    上の表示は空欄のままになる)。overlay_label が未指定の場合は
    label をそのまま使う。長すぎるとアイコン上で読みにくくなるため、
    短い略称(例: "LS", "aiSS", "UDIM")を渡すことを推奨する。
    """
    try:
        current_shelf = mel.eval("tabLayout -q -selectTab $gShelfTopLevel")
        _remove_existing_shelf_buttons(label)
        cmds.shelfButton(
            parent=current_shelf,
            label=label,
            annotation=annotation,
            image=image,
            imageOverlayLabel=overlay_label if overlay_label else label,
            command=command,
            sourceType="python",
        )
        print("[Live Sync Installer] Shelf button '{0}' added to: {1} (icon: {2})".format(label, current_shelf, image))
        return True
    except Exception as e:
        print("[Live Sync Installer] Shelf button '{0}' skipped: {1}".format(label, e))
        return False


def _register_autostart():
    """
    maya_live_sync 側の自動起動登録機能を呼ぶ(LiveSyncのみ)。
    sp_to_aiStandardSurface は都度起動する一括ツールのため自動起動しない。
    """
    try:
        import maya_live_sync
        if hasattr(maya_live_sync, "register_user_setup"):
            ok, msg = maya_live_sync.register_user_setup(auto_open=False)
            print("[Live Sync Installer] Autostart: {0} ({1})".format(ok, msg))
            return ok
        else:
            print("[Live Sync Installer] register_user_setup が無いため自動起動登録はスキップ。")
            return False
    except Exception as e:
        print("[Live Sync Installer] Autostart skipped: {0}".format(e))
        return False


def _run():
    source_dir = os.path.dirname(os.path.abspath(__file__))

    copied = _copy_tools(source_dir)
    _ensure_importable()

    # 2026.07.16: reload() する"前"に、まだ古いクラス定義のままの
    # maya_live_sync を使って既存のworkspaceControl/ウィンドウを
    # 破棄しておく。reload後に破棄しようとすると、既にクラス定義が
    # 新しくなった状態で古いworkspaceControlを操作することになり、
    # 不整合が起きる可能性があるため、必ずこの順序で行う。
    old_version, _destroy_diag = _destroy_stale_livesync_window()

    # コピーした各ツールについて、以前このセッションでimport済みなら
    # 強制的に再読込する(再D&D時に古いコードのまま動く問題への対処)。
    for name in copied:
        module_name = os.path.splitext(name)[0]
        _reload_if_cached(module_name)

    # 2026.07.16: 完了メッセージでバージョン差分を表示するため、
    # reload後の新しいバージョンを取得しておく。
    new_version = None
    try:
        import maya_live_sync
        new_version = getattr(maya_live_sync, "__version__", None)
    except Exception:
        pass

    # 2026.07.20(フェーズ2・バージョン整合性): 上記は maya_live_sync.py
    # 単体の新旧差分表示だが、実際にコピーされる全ツールファイルの
    # バージョンが今どうなっているかを一目で確認できるよう、一覧も
    # 別途取得しておく(表示は完了メッセージの後半に追加する)。
    installed_versions = _collect_installed_versions()

    has_aiss = "sp_to_aiStandardSurface.py" in copied
    has_udim = "udim_setup.py" in copied

    copied_icons = _copy_icons(source_dir)
    livesync_icon = LIVESYNC_ICON_NAME if LIVESYNC_ICON_NAME in copied_icons else "pythonFamily.png"
    aiss_icon = AISS_ICON_NAME if AISS_ICON_NAME in copied_icons else "pythonFamily.png"
    udim_icon = UDIM_ICON_NAME if UDIM_ICON_NAME in copied_icons else "pythonFamily.png"

    livesync_shelf = _add_shelf_button(LIVESYNC_LABEL, LIVESYNC_ANNOTATION, LIVESYNC_COMMAND, image=livesync_icon)
    aiss_shelf = _add_shelf_button(AISS_LABEL, AISS_ANNOTATION, AISS_COMMAND, image=aiss_icon) if has_aiss else False
    udim_shelf = _add_shelf_button(UDIM_LABEL, UDIM_ANNOTATION, UDIM_COMMAND, image=udim_icon) if has_udim else False

    auto_ok = _register_autostart()

    # --- 完了メッセージ ---
    lines = ["SP -> Maya Live Sync のインストールが完了しました。", ""]

    # 2026.07.16: バージョンが頻繁に変わる運用実態を踏まえ、今回の
    # インストールで実際に何が変わったか(あるいは変わらなかったか)を
    # 一目で分かるようにする。
    if new_version:
        if old_version and old_version != new_version:
            lines.append("[バージョン] {0} -> {1} に更新しました。".format(old_version, new_version))
        elif old_version and old_version == new_version:
            lines.append("[バージョン] {0}（変更なし。コピー元ファイルが更新されていない可能性があります）".format(new_version))
        else:
            lines.append("[バージョン] {0}（新規インストール）".format(new_version))
        lines.append("")

    # 2026.07.20(フェーズ2・バージョン整合性): インストールされた
    # 全ツールファイルのバージョンを一覧表示する。上記の[バージョン]は
    # maya_live_sync.py単体の新旧差分だが、こちらは「今このセッションで
    # 動いている全ファイルの組み合わせ」を一目で確認できるようにする
    # ためのもの。取得できなかったファイル(未import・__version__属性
    # なし等)は「-」と表示し、原因の切り分けがしやすいようにする。
    lines.append("[バージョン一覧]")
    for fname, version, ok in installed_versions:
        if ok:
            lines.append("  - {0}: v{1}".format(fname, version))
        else:
            lines.append("  - {0}: - (未読込のため確認できません。ウィンドウを一度開くと確認できます)".format(fname))
    lines.append("")

    lines.append("[コピーしたツール]")
    for name in copied:
        lines.append("  - {0}（最新の内容に更新済み）".format(name))
    if not has_aiss:
        lines.append("  * sp_to_aiStandardSurface.py は同じフォルダに無かったため未導入です。")
    if not has_udim:
        lines.append("  * udim_setup.py は同じフォルダに無かったため未導入です。")

    lines.append("")
    lines.append("[シェルフボタン]")
    livesync_icon_note = "専用アイコン" if livesync_icon == LIVESYNC_ICON_NAME else "標準アイコン(専用アイコン画像が見つからなかったため)"
    lines.append(
        "  - '{0}' : ライブ同期ウィンドウ ({1})".format(LIVESYNC_LABEL, livesync_icon_note)
        if livesync_shelf else
        "  - LiveSync ボタンの追加はスキップされました(手動で作成できます)。"
    )
    if has_aiss:
        aiss_icon_note = "専用アイコン" if aiss_icon == AISS_ICON_NAME else "標準アイコン(専用アイコン画像が見つからなかったため)"
        lines.append(
            "  - '{0}' : Final取り込み(押すとFinalフォルダを自動入力) ({1})".format(AISS_LABEL, aiss_icon_note)
            if aiss_shelf else
            "  - aiSS ボタンの追加はスキップされました(手動で作成できます)。"
        )
    if has_udim:
        icon_note = "専用アイコン" if udim_icon == UDIM_ICON_NAME else "標準アイコン(専用アイコン画像が見つからなかったため)"
        lines.append(
            "  - '{0}' : UDIMテクスチャ自動セットアップ ({1})".format(UDIM_LABEL, icon_note)
            if udim_shelf else
            "  - UDIM ボタンの追加はスキップされました(手動で作成できます)。"
        )

    lines.append("")
    lines.append(
        "[自動起動] Maya 起動時に LiveSync を自動読み込みするよう登録しました。"
        if auto_ok else
        "[自動起動] 登録はスキップされました(必要なら後で設定できます)。"
    )

    # --- [DIAG-A2] 完了ダイアログに旧インスタンス破棄の成否を明示 -------
    # これまでは _destroy_stale_livesync_window() 内の失敗はScript Editor
    # ログにしか出ず、確認ダイアログ上は「インストール完了」しか見えな
    # かった。再起動しないと反映されない症状が出た際に、原因がここに
    # あるのかどうかをダイアログだけで即判断できるようにする。
    if _destroy_diag.get("old_module_found"):
        lines.append("")
        lines.append("[DIAG-A2] 旧インスタンス破棄の結果(この再インストールが「反映されない」場合の切り分け用):")
        wc_existed = _destroy_diag.get("workspace_control_existed")
        wc_destroyed = _destroy_diag.get("workspace_control_destroyed")
        wc_err = _destroy_diag.get("workspace_control_destroy_error")
        if wc_existed is None:
            lines.append("  - workspaceControl: 確認できず(WORKSPACE_CONTROL_NAME属性なし)")
        elif wc_existed is False:
            lines.append("  - workspaceControl: 既存なし(破棄不要)")
        elif wc_destroyed:
            lines.append("  - workspaceControl: 検出 -> 破棄 成功")
        else:
            lines.append("  - workspaceControl: 検出 -> 破棄 失敗 ({0}) ← 再起動が必要になる可能性が高い箇所".format(wc_err))

        cb_err = _destroy_diag.get("scene_callbacks_unregister_error")
        if _destroy_diag.get("window_instance_existed"):
            cb_found = _destroy_diag.get("scene_callbacks_found")
            if cb_err:
                lines.append("  - シーンコールバック解除: 失敗 ({0}) ← 残留コールバックの疑い".format(cb_err))
            else:
                lines.append("  - シーンコールバック解除: {0}件 処理".format(cb_found if cb_found is not None else "?"))
            reset_err = _destroy_diag.get("window_instance_reset_error")
            if reset_err:
                lines.append("  - _window_instance リセット: 失敗 ({0}) ← 最有力候補: これが失敗すると show_ui() が古いインスタンスを再利用する".format(reset_err))
            else:
                lines.append("  - _window_instance リセット: 成功")
        else:
            lines.append("  - 旧ウィンドウインスタンス: 無し(破棄対象なし)")

    lines += [
        "",
        "[使い方の流れ]",
        "  1. SPで塗る -> Maya側は '{0}' で監視、ライブ同期で確認".format(LIVESYNC_LABEL),
        "  2. 仕上げ -> SPで Final を書き出す",
        "  3. '{0}' ボタン -> フォルダは自動入力済み -> Scan -> Create".format(AISS_LABEL),
    ]

    message = "\n".join(lines)
    print("[Live Sync Installer]\n" + message)
    cmds.confirmDialog(title="Live Sync Installer", message=message, button=["OK"])