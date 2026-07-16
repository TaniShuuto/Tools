"""
install.py  -  SP -> Maya Live Sync : Maya drag-and-drop installer
================================================================================
使い方:
    この install.py を、maya_live_sync.py / sp_to_aiStandardSurface.py と
    同じフォルダに置いたまま、Maya のビューポート(3D画面)へドラッグ&ドロップ
    するだけ。

やっていること(自動):
    1. 同じフォルダにある下記2ファイルを Maya の scripts フォルダへコピー
         - maya_live_sync.py            (ライブ同期 本体)
         - sp_to_aiStandardSurface.py   (Final取り込み・マテリアル一括生成)
    2. アクティブなシェルフに 2 つの起動ボタンを追加
         - [LiveSync] ... ライブ同期ウィンドウを開く
         - [aiSS]     ... マテリアル生成ツールを開き、Final書き出しフォルダを
                          自動入力する(= Final取り込みへ誘導)
    3. Maya 起動時に LiveSync が自動読み込みされるよう userSetup.py へ登録
    4. 完了メッセージを表示

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
"""

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
]

# --- シェルフボタン: LiveSync ---------------------------------------------
LIVESYNC_LABEL = "LiveSync"
LIVESYNC_ANNOTATION = "SP -> Maya Live Sync : open the live sync window"
LIVESYNC_COMMAND = "import maya_live_sync\nmaya_live_sync.show_ui()"

# --- シェルフボタン: aiSS (Final取り込みへ誘導) ---------------------------
#  ツールを開いた直後に、共有設定の final_export_dir をフォルダ入力欄へ
#  流し込む。これにより「フォルダを探す」手間が消え、Final を書き出した後の
#  取り込みへ自然に誘導できる。sp_to_aiStandardSurface.py には手を加えず、
#  公開されている _gui_state["dir_field"] を使って外側から設定している。
AISS_LABEL = "aiSS"
AISS_ANNOTATION = "Build aiStandardSurface from the SP *Final* export (folder auto-filled)"
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


def _scripts_dir():
    """Maya のユーザ scripts フォルダ(全バージョン共通)のパスを返す。"""
    return cmds.internalVar(userScriptDir=True)


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
    old_version = None
    try:
        import maya_live_sync
        old_version = getattr(maya_live_sync, "__version__", None)
    except Exception as e:
        # maya_live_sync 自体がまだ一度もimportされていない(=初回
        # インストール)場合はここに来る。破棄すべきものが無いだけなので
        # 正常なケースとして扱う。念のためセッションロックの解放だけは
        # 試みる(前回のセッションが異常終了した場合の残骸対策)。
        print("[Live Sync Installer] No previous maya_live_sync session detected ({0}); skipping cleanup.".format(e))
        _release_session_lock_directly()
        return old_version

    control_name = getattr(maya_live_sync, "WORKSPACE_CONTROL_NAME", None)
    if control_name:
        try:
            if cmds.workspaceControl(control_name, query=True, exists=True):
                cmds.deleteUI(control_name, control=True)
                print("[Live Sync Installer] Destroyed stale workspaceControl: {0}".format(control_name))
        except Exception as e:
            print("[Live Sync Installer] Could not destroy workspaceControl '{0}': {1}".format(control_name, e))

    if hasattr(maya_live_sync, "_window_instance"):
        window = maya_live_sync._window_instance
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
                for cb_id in getattr(window, "_scene_callback_ids", []):
                    try:
                        om.MMessage.removeCallback(cb_id)
                    except Exception:
                        pass
                window._scene_callback_ids = []
                print("[Live Sync Installer] Unregistered stale scene callbacks.")
            except Exception as e:
                print("[Live Sync Installer] Could not unregister scene callbacks: {0}".format(e))
            # 安全策: watcher(project_poll_timer/fs_watcher等)はQtの親子
            # 関係(LiveSyncWatcher(self))により、親ウィジェット破棄時に
            # 連動して破棄されるのが期待される標準動作だが、
            # workspaceControlの破棄がQt標準のウィジェット削除経路を
            # 必ず通るとは限らないため、念のため明示的に停止しておく
            # (タイマーが動いたまま参照先が壊れることによる不安定化を防ぐ)。
            try:
                watcher = getattr(window, "watcher", None)
                if watcher is not None and getattr(watcher, "enabled", False):
                    watcher.stop(reason="manual")
                    print("[Live Sync Installer] Stopped stale watcher before teardown.")
            except Exception as e:
                print("[Live Sync Installer] Could not stop stale watcher: {0}".format(e))
        try:
            maya_live_sync._window_instance = None
            print("[Live Sync Installer] Reset cached _window_instance reference.")
        except Exception as e:
            print("[Live Sync Installer] Could not reset _window_instance: {0}".format(e))

    # 2026.07.16(見落とし修正): watcher.stop() 経由のセッションロック
    # 解放は watcher.enabled が True の場合にしか行われない。監視が
    # OFFの状態で再インストールした場合でもロックを確実に解放できる
    # よう、watcher の状態に関わらずここで直接解放を試みる。
    _release_session_lock_directly()

    return old_version


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


def _add_shelf_button(label, annotation, command):
    """アクティブなシェルフにボタンを1つ追加する。成否を返す。
    2026.07.16: 追加の前に、全シェルフタブを横断して同じラベルの
    既存ボタンがあれば削除する(再インストールのたびにボタンが
    重複して増えるのを防ぐため。アクティブなタブに限定すると、
    別タブに残った古いボタンを見落とすため全タブを対象にしている)。
    """
    try:
        current_shelf = mel.eval("tabLayout -q -selectTab $gShelfTopLevel")
        _remove_existing_shelf_buttons(label)
        cmds.shelfButton(
            parent=current_shelf,
            label=label,
            annotation=annotation,
            image="pythonFamily.png",
            command=command,
            sourceType="python",
        )
        print("[Live Sync Installer] Shelf button '{0}' added to: {1}".format(label, current_shelf))
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
    old_version = _destroy_stale_livesync_window()

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

    has_aiss = "sp_to_aiStandardSurface.py" in copied

    livesync_shelf = _add_shelf_button(LIVESYNC_LABEL, LIVESYNC_ANNOTATION, LIVESYNC_COMMAND)
    aiss_shelf = _add_shelf_button(AISS_LABEL, AISS_ANNOTATION, AISS_COMMAND) if has_aiss else False

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

    lines.append("[コピーしたツール]")
    for name in copied:
        lines.append("  - {0}（最新の内容に更新済み）".format(name))
    if not has_aiss:
        lines.append("  * sp_to_aiStandardSurface.py は同じフォルダに無かったため未導入です。")

    lines.append("")
    lines.append("[シェルフボタン]")
    lines.append(
        "  - '{0}' : ライブ同期ウィンドウ".format(LIVESYNC_LABEL)
        if livesync_shelf else
        "  - LiveSync ボタンの追加はスキップされました(手動で作成できます)。"
    )
    if has_aiss:
        lines.append(
            "  - '{0}' : Final取り込み(押すとFinalフォルダを自動入力)".format(AISS_LABEL)
            if aiss_shelf else
            "  - aiSS ボタンの追加はスキップされました(手動で作成できます)。"
        )

    lines.append("")
    lines.append(
        "[自動起動] Maya 起動時に LiveSync を自動読み込みするよう登録しました。"
        if auto_ok else
        "[自動起動] 登録はスキップされました(必要なら後で設定できます)。"
    )

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
