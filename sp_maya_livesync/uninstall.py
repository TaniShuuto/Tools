"""
uninstall.py  -  SP -> Maya Live Sync : Maya drag-and-drop uninstaller
================================================================================
使い方:
    この uninstall.py を、Maya のビューポート(3D画面)へドラッグ&ドロップ
    するだけ。install.py と同じフォルダに置く必要はない(scripts フォルダに
    導入済みのツールを対象に動作するため)。

やっていること(自動、install.py の逆操作):
    1. 現在開いている LiveSync ウィンドウ/workspaceControl を閉じて破棄
    2. アクティブシェルフ + 全シェルフタブから起動ボタンを削除
         - [LiveSync]
         - [aiSS]
         - [UDIM]
    3. userSetup.py に登録された自動起動ブロックを削除
         (SP_LIVE_SYNC_AUTO_REGISTER マーカーで囲まれた範囲のみ。
          ユーザーが書いた他の内容には触れない)
    4. Maya の scripts フォルダから下記3ファイルを削除
         - maya_live_sync.py
         - sp_to_aiStandardSurface.py
         - udim_setup.py
    5. Maya の icons フォルダから3ツール分のシェルフアイコン
       (maya_live_sync_icon.png / sp_to_aiStandardSurface_icon.png /
        udim_setup_icon.png)を削除
         (install.py がコピーした場合のみ存在する任意ファイルのため、
          見つからなくてもエラーにはしない)
    6. 完了メッセージを表示

**削除しないもの(意図的):**
    C:/SPMayaLiveSync 配下の設定ファイル・live/final フォルダ内の
    テクスチャデータには一切触れない。これらは SP 側と共有している
    データであり、誤って削除するとやり直しが効かないため、
    アンインストールの対象から常に除外する。
    (完全に消したい場合は、このフォルダを手動で削除してください、と
    完了メッセージ内で案内するに留める)

    なお、このMayaセッション自身が作成したセッションロックファイル
    (maya_session.lock)だけは例外的に解放する(残したままだと、
    別のMayaセッションで身に覚えのない警告が出続けるため)。
    他プロセスが作成したロックには一切触れない。

対象: Maya 2022 以降(ドラッグ&ドロップ実行自体は 2017 Update 3 以降で対応)

NOTE:
    UI文字列は、Windows + 日本語ロケール環境での文字化けを避けるため
    ASCII(英語)に統一しています(コメントは UTF-8 のまま影響ありません)。

2026.07.20(見落とし修正): これまで udim_setup.py 導入前に作られたまま
更新されておらず、TOOL_FILE_NAMES / シェルフボタン削除の対象に
udim_setup.py・UDIMシェルフボタン・アイコン画像が一切含まれていなかった。
install.py 側で udim_setup.py を導入した環境でアンインストールを実行しても、
UDIM関連だけが残り続ける不具合があったため、install.py 側の定義
(UDIM_LABEL 等)と対になるよう追加した。

変更履歴:
    1.0.0 (2026.07.24):
        - このファイルにも install.py と同じ SemVer での __version__
          管理を導入した(従来はバージョン番号を持たず、日付ベースの
          コメントのみだった)。導入と同時に、install.py がコピーする
          3つのアイコンのうち udim_setup_icon.png しか削除対象になって
          おらず、maya_live_sync_icon.png / sp_to_aiStandardSurface_
          icon.png がアンインストール後も残り続けていた見落としを修正
          した(install.py の LIVESYNC_ICON_NAME / AISS_ICON_NAME と
          同じ値をここでも独立して持ち、3つとも削除するようにした)。
          この初回バージョンには、上記2026.07.20の修正も含まれている。
"""

__version__ = "1.0.0"

import os
import sys
import traceback

import maya.cmds as cmds
import maya.mel as mel


# ドラッグ&ドロップ実行のために Maya が要求するエントリポイント。
def onMayaDroppedPythonFile(*args, **kwargs):
    try:
        _run()
    except Exception as e:
        traceback.print_exc()
        cmds.confirmDialog(
            title="Live Sync Uninstaller",
            message="Uninstall failed:\n{0}\n\nSee the Script Editor for details.".format(e),
            button=["OK"],
        )


# install.py の TOOL_FILES / シェルフラベルと同じ定義を、依存を減らす
# ため独立して持つ(install.py が既に削除された後でもこのファイル単体で
# 動作できるようにするため)。
# 2026.07.20(見落とし修正): udim_setup.py を追加。install.py の
# TOOL_FILES と UDIM_LABEL / UDIM_ICON_NAME に対応。
TOOL_FILE_NAMES = ["maya_live_sync.py", "sp_to_aiStandardSurface.py", "udim_setup.py"]
LIVESYNC_LABEL = "LiveSync"
AISS_LABEL = "aiSS"
UDIM_LABEL = "UDIM"
# 2026.07.24(見落とし修正): install.py がコピーする3つのアイコンのうち
# udim_setup_icon.png しか削除対象になっておらず、maya_live_sync_icon.png /
# sp_to_aiStandardSurface_icon.png がアンインストール後も残り続けていた。
# install.py の LIVESYNC_ICON_NAME / AISS_ICON_NAME と同じ値をここでも
# 独立して持つ(このファイルの他の定数と同じ理由: install.py が既に
# 削除された後でも単体で動作できるようにするため)。
LIVESYNC_ICON_NAME = "maya_live_sync_icon.png"
AISS_ICON_NAME = "sp_to_aiStandardSurface_icon.png"
UDIM_ICON_NAME = "udim_setup_icon.png"
REGISTER_MARKER = "# === SP_LIVE_SYNC_AUTO_REGISTER ==="

# maya_live_sync.py 側の定義と一致させる必要がある定数。ここでも
# ハードコードせず、可能なら実モジュールから読むようにしている
# (_destroy_livesync_window 参照)。ここではフォールバック用の値として
# 保持する。
_FALLBACK_WINDOW_OBJECT_NAME = "MayaLiveSyncWindow"
_FALLBACK_WORKSPACE_CONTROL_NAME = _FALLBACK_WINDOW_OBJECT_NAME + "WorkspaceControl"


def _scripts_dir():
    """Maya のユーザ scripts フォルダ(全バージョン共通)のパスを返す。"""
    return cmds.internalVar(userScriptDir=True)


def _icons_dir():
    """
    Maya のユーザ prefs 配下の icons フォルダのパスを返す。
    install.py の _icons_dir() と同じ場所(コピー先)を指す必要があるため、
    同じロジックを独立して持つ。
    """
    return os.path.join(cmds.internalVar(userPrefDir=True), "icons")


def _remove_icon_files():
    """
    install.py が任意でコピーしたシェルフアイコン画像を icons フォルダから
    削除する。install.py 自体がコピーを試みて失敗している場合(画像が
    同梱されていなかった等)は、そもそもファイルが存在しないため、
    見つからなくてもエラーとせず静かにスキップする。
    削除できたファイル名の一覧を返す。
    """
    dst_dir = _icons_dir()
    removed = []
    for name in [LIVESYNC_ICON_NAME, AISS_ICON_NAME, UDIM_ICON_NAME]:
        path = os.path.join(dst_dir, name)
        if not os.path.isfile(path):
            print("[Live Sync Uninstaller] Icon not found (already removed or never installed?): {0}".format(path))
            continue
        try:
            os.remove(path)
            removed.append(name)
            print("[Live Sync Uninstaller] Removed icon: {0}".format(path))
        except Exception as e:
            print("[Live Sync Uninstaller] Could not remove icon '{0}': {1}".format(path, e))
    return removed


def _release_session_lock_directly():
    """
    maya_live_sync が一度もimportされていない(このMayaセッションでは
    未使用の)場合でも、セッションロックファイルのパス自体は
    CONFIG_DIR から計算できる。念のため直接ファイルの中身を確認し、
    自分自身のpidであれば削除する(他プロセスのロックは誤って
    消さないようにpid照合を行う)。

    通常は _destroy_livesync_window() 内の watcher.stop() 経由で
    解放されるため、これはその経路が使えない場合の保険。
    """
    config_dir = "C:/SPMayaLiveSync"
    lock_path = os.path.join(config_dir, "maya_session.lock")
    if not os.path.isfile(lock_path):
        return
    try:
        import json
        with open(lock_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("pid") == os.getpid():
            os.remove(lock_path)
            print("[Live Sync Uninstaller] Released this session's lock file directly.")
    except Exception as e:
        print("[Live Sync Uninstaller] Could not inspect/release session lock: {0}".format(e))


def _destroy_livesync_window():
    """
    現在開いている LiveSync ウィンドウ/workspaceControl を閉じて破棄する。
    install.py の _destroy_stale_livesync_window() と対になる処理。

    maya_live_sync がまだ import されていない(一度も使われていない)
    場合は、ウィンドウの破棄自体は不要だが、セッションロックの解放は
    別途 _release_session_lock_directly() で試みる。
    """
    try:
        import maya_live_sync
    except Exception:
        print("[Live Sync Uninstaller] maya_live_sync is not currently loaded; skipping window cleanup.")
        _release_session_lock_directly()
        return

    control_name = getattr(maya_live_sync, "WORKSPACE_CONTROL_NAME", _FALLBACK_WORKSPACE_CONTROL_NAME)
    window = getattr(maya_live_sync, "_window_instance", None)

    if window is not None:
        # シーンコールバック(kAfterOpen/kAfterNew)の解除。closeEvent に
        # 任せず、ここでも明示的に行う(install.py 側と同じ理由)。
        try:
            import maya.OpenMaya as om
            for cb_id in getattr(window, "_scene_callback_ids", []):
                try:
                    om.MMessage.removeCallback(cb_id)
                except Exception:
                    pass
            window._scene_callback_ids = []
        except Exception as e:
            print("[Live Sync Uninstaller] Could not unregister scene callbacks: {0}".format(e))

        # watcher(タイマー・フォルダ監視)を明示的に停止する。
        try:
            watcher = getattr(window, "watcher", None)
            if watcher is not None and getattr(watcher, "enabled", False):
                watcher.stop(reason="manual")
                print("[Live Sync Uninstaller] Stopped active watcher.")
        except Exception as e:
            print("[Live Sync Uninstaller] Could not stop watcher: {0}".format(e))

    # 2026.07.16(見落とし修正): 以前は watcher.enabled が True の場合、
    # つまり「監視がONの状態でアンインストールした場合」にしか
    # セッションロックが解放されなかった。監視がOFFの状態(ウィンドウは
    # 開いているが監視ボタンは押していない)でアンインストールすると、
    # ロックファイルが残ったままになり、次回別のMayaセッションを
    # 開いた際に「他のセッションが監視中」という誤警告が再発する
    # 可能性があった。watcher.enabled の状態に関わらず、常に
    # (自分自身のロックであれば)解放を試みるようにする。
    _release_session_lock_directly()

    if control_name:
        try:
            if cmds.workspaceControl(control_name, query=True, exists=True):
                cmds.deleteUI(control_name, control=True)
                print("[Live Sync Uninstaller] Destroyed workspaceControl: {0}".format(control_name))
        except Exception as e:
            print("[Live Sync Uninstaller] Could not destroy workspaceControl '{0}': {1}".format(control_name, e))

    try:
        maya_live_sync._window_instance = None
    except Exception:
        pass


def _remove_shelf_buttons(label):
    """
    指定ラベルを持つシェルフボタンを、全シェルフタブを横断して削除する。
    install.py の _remove_existing_shelf_buttons() と同じロジック
    (アクティブなタブに限定すると、別タブに残ったボタンを見落とすため)。
    見つけて削除した個数を返す。
    """
    removed = 0
    try:
        shelf_top_level = mel.eval("$temp = $gShelfTopLevel")
        all_shelves = cmds.tabLayout(shelf_top_level, query=True, childArray=True) or []
    except Exception as e:
        print("[Live Sync Uninstaller] Could not enumerate shelf tabs: {0}".format(e))
        return removed

    for shelf in all_shelves:
        try:
            children = cmds.shelfLayout(shelf, query=True, childArray=True) or []
        except Exception:
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
                    removed += 1
                    print("[Live Sync Uninstaller] Removed shelf button '{0}' from shelf: {1}".format(label, shelf))
                except Exception as e:
                    print("[Live Sync Uninstaller] Could not remove shelf button '{0}': {1}".format(label, e))
    return removed


def _user_setup_path():
    return os.path.join(cmds.internalVar(userScriptDir=True), "userSetup.py")


def _remove_autostart_block():
    """
    userSetup.py に register_user_setup() が追記した自動起動ブロックを
    削除する。REGISTER_MARKER から、対になる "except Exception:\\n    pass\\n"
    の直後までを1ブロックとして取り除く。

    ユーザーが userSetup.py に書いた他の内容には一切触れない
    (マーカーで囲まれた範囲だけを対象にする)。
    削除前にタイムスタンプ付きでバックアップを取る(install.py側の
    登録時バックアップと同じ方針で、誤操作からの回復手段を残すため)。

    戻り値: (成功したか, メッセージ)
    """
    path = _user_setup_path()
    if not os.path.isfile(path):
        return False, "userSetup.py が見つかりません: {0}".format(path)

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return False, "userSetup.py の読み込みに失敗しました: {0}".format(e)

    if REGISTER_MARKER not in content:
        return False, "自動起動の登録が見つかりませんでした(既に未登録か、手動編集された可能性があります)。"

    start = content.index(REGISTER_MARKER)
    # register_user_setup() は必ず REGISTER_MARKER の直前に "\n" を1つ
    # 追加してから書き込んでいるため、その空行ごと取り除く。
    if start > 0 and content[start - 1] == "\n":
        start -= 1

    # ブロックは "except Exception:\n    pass\n" で終わる形式に固定されて
    # いる(register_user_setup() 参照)。マーカー以降で最初に現れるその
    # パターンまでを削除範囲とする。
    end_marker = "except Exception:\n    pass\n"
    end_idx = content.find(end_marker, start)
    if end_idx == -1:
        return False, "登録ブロックの終端が見つからず、安全のため削除を中止しました。手動でご確認ください。"
    end = end_idx + len(end_marker)

    new_content = content[:start] + content[end:]

    try:
        import datetime
        backup_path = "{0}.bak_{1}".format(path, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return False, "バックアップの作成に失敗したため、安全のため削除を中止しました: {0}".format(e)

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return False, "userSetup.py への書き込みに失敗しました: {0}".format(e)

    return True, "自動起動の登録を削除しました: {0}".format(path)


def _remove_tool_files():
    """
    scripts フォルダから TOOL_FILE_NAMES を削除する。
    見つからなかったファイルはスキップ(既に削除済みとみなす)。
    削除できたファイル名の一覧を返す。
    """
    dst_dir = _scripts_dir()
    removed = []
    for name in TOOL_FILE_NAMES:
        path = os.path.join(dst_dir, name)
        if not os.path.isfile(path):
            print("[Live Sync Uninstaller] Not found (already removed?): {0}".format(path))
            continue
        try:
            os.remove(path)
            removed.append(name)
            print("[Live Sync Uninstaller] Removed: {0}".format(path))
        except Exception as e:
            print("[Live Sync Uninstaller] Could not remove '{0}': {1}".format(path, e))
    return removed


def _run():
    # install.py と同じ方式: 実行のたびに必ずバージョンをログへ出す。
    print("[Live Sync Uninstaller] version: {0}".format(__version__))

    # 1. ウィンドウ/workspaceControlの破棄(ファイル削除より前に行う。
    #    削除対象のモジュールがまだメモリ上で動いているうちに片付ける)。
    _destroy_livesync_window()

    # 2. シェルフボタンの削除。
    # 2026.07.20(見落とし修正): UDIMシェルフボタンの削除が漏れていたため追加。
    livesync_removed = _remove_shelf_buttons(LIVESYNC_LABEL)
    aiss_removed = _remove_shelf_buttons(AISS_LABEL)
    udim_removed = _remove_shelf_buttons(UDIM_LABEL)

    # 3. userSetup.py の自動起動登録を削除。
    autostart_ok, autostart_msg = _remove_autostart_block()

    # 4. ツール本体ファイルの削除(TOOL_FILE_NAMESにudim_setup.pyを追加済み)。
    removed_files = _remove_tool_files()

    # 5. シェルフアイコン画像の削除。
    # 2026.07.20(見落とし修正): install.pyがコピーしたudim_setup_icon.png
    # がicons フォルダに残り続けていたため追加。
    # 2026.07.24(見落とし修正): 上記対応がUDIM分のみで、maya_live_sync_icon.png
    # / sp_to_aiStandardSurface_icon.png の2つがicons フォルダに残り続けて
    # いた(install.pyは3つともコピーするが、こちらは1つしか削除していな
    # かった非対称性)。LIVESYNC_ICON_NAME / AISS_ICON_NAME を追加し、
    # _remove_icon_files() 側で3つとも削除するよう修正。
    removed_icons = _remove_icon_files()

    # sys.modules に残っているキャッシュも掃除しておく(ファイルは
    # 消えても、既にimport済みのモジュールオブジェクトはメモリ上に
    # 残るため。次に誰かが誤って import しても、ディスク上に実体が
    # 無ければ ImportError になるのが自然だが、念のため明示的に外す)。
    for name in TOOL_FILE_NAMES:
        module_name = os.path.splitext(name)[0]
        sys.modules.pop(module_name, None)

    # --- 完了メッセージ ---
    lines = ["SP -> Maya Live Sync のアンインストールが完了しました。", ""]

    lines.append("[削除したツール]")
    if removed_files:
        for name in removed_files:
            lines.append("  - {0}".format(name))
    else:
        lines.append("  - 該当ファイルは見つかりませんでした(既に削除済みの可能性があります)。")

    lines.append("")
    lines.append("[シェルフボタン]")
    lines.append("  - '{0}' : {1} 個削除".format(LIVESYNC_LABEL, livesync_removed))
    lines.append("  - '{0}' : {1} 個削除".format(AISS_LABEL, aiss_removed))
    lines.append("  - '{0}' : {1} 個削除".format(UDIM_LABEL, udim_removed))

    if removed_icons:
        lines.append("")
        lines.append("[シェルフアイコン]")
        for name in removed_icons:
            lines.append("  - {0} を削除".format(name))

    lines.append("")
    lines.append("[自動起動の登録]")
    lines.append("  - {0}".format(autostart_msg))

    lines += [
        "",
        "[削除していないもの]",
        "  設定ファイル・テクスチャの同期データ(C:/SPMayaLiveSync 以下)は",
        "  SPと共有しているデータのため、このアンインストーラでは削除して",
        "  いません。完全に削除したい場合は、このフォルダを手動で削除して",
        "  ください。",
        "",
        "[再インストールしたい場合]",
        "  install.py を同じ手順でドラッグ&ドロップしてください。",
    ]

    message = "\n".join(lines)
    print("[Live Sync Uninstaller]\n" + message)
    cmds.confirmDialog(title="Live Sync Uninstaller", message=message, button=["OK"])