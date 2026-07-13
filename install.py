"""
install.py  -  SP -> Maya Live Sync : Maya drag-and-drop installer
================================================================================
使い方:
    この install.py を、maya_live_sync.py と同じフォルダに置いたまま、
    Maya のビューポート(3D画面)へドラッグ&ドロップするだけ。

やっていること(自動):
    1. 同じフォルダにある maya_live_sync.py を Maya の scripts フォルダへコピー
    2. アクティブなシェルフに起動ボタンを1つ追加
    3. Maya 起動時に自動で読み込まれるよう userSetup.py へ登録
    4. 完了メッセージを表示

対象: Maya 2022 以降(ドラッグ&ドロップ実行自体は 2017 Update 3 以降で対応)

NOTE:
    UI文字列は、Windows + 日本語ロケール環境での文字化けを避けるため
    ASCII(英語)に統一しています(コメントは UTF-8 のまま影響ありません)。
"""

import os
import sys
import shutil
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


TOOL_FILE = "maya_live_sync.py"
SHELF_BUTTON_LABEL = "LiveSync"
SHELF_BUTTON_COMMAND = "import maya_live_sync\nmaya_live_sync.show_ui()"


def _scripts_dir():
    """Maya のユーザ scripts フォルダ(全バージョン共通)のパスを返す。"""
    return cmds.internalVar(userScriptDir=True)


def _copy_tool(source_dir):
    """maya_live_sync.py を scripts フォルダへコピーする。"""
    src = os.path.join(source_dir, TOOL_FILE)
    if not os.path.isfile(src):
        raise RuntimeError(
            "'{0}' がインストーラと同じフォルダに見つかりません。"
            "install.py と maya_live_sync.py を同じフォルダに置いてください。".format(TOOL_FILE)
        )
    dst_dir = _scripts_dir()
    if not os.path.isdir(dst_dir):
        os.makedirs(dst_dir)
    dst = os.path.join(dst_dir, TOOL_FILE)
    shutil.copy2(src, dst)
    print("[Live Sync Installer] Copied: {0} -> {1}".format(src, dst))
    return dst


def _ensure_importable():
    """コピー直後のセッションでも import できるよう scripts を sys.path に追加。"""
    d = _scripts_dir()
    if d not in sys.path:
        sys.path.append(d)


def _add_shelf_button():
    """アクティブなシェルフに起動ボタンを追加する。"""
    try:
        current_shelf = mel.eval("tabLayout -q -selectTab $gShelfTopLevel")
        cmds.shelfButton(
            parent=current_shelf,
            label=SHELF_BUTTON_LABEL,
            annotation="SP -> Maya Live Sync",
            image="pythonFamily.png",
            command=SHELF_BUTTON_COMMAND,
            sourceType="python",
        )
        print("[Live Sync Installer] Shelf button added to: {0}".format(current_shelf))
        return True
    except Exception as e:
        print("[Live Sync Installer] Shelf button skipped: {0}".format(e))
        return False


def _register_autostart():
    """
    maya_live_sync 側の自動起動登録機能を呼ぶ。
    (register_user_setup が存在しない古いツール版でも失敗しないよう握る)
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

    _copy_tool(source_dir)
    _ensure_importable()
    shelf_ok = _add_shelf_button()
    auto_ok = _register_autostart()

    lines = [
        "SP -> Maya Live Sync のインストールが完了しました。",
        "",
        "- ツール本体を scripts フォルダへコピーしました。",
    ]
    lines.append(
        "- シェルフに '{0}' ボタンを追加しました。".format(SHELF_BUTTON_LABEL)
        if shelf_ok else
        "- シェルフボタンの追加はスキップされました(手動で作成できます)。"
    )
    lines.append(
        "- Maya 起動時の自動読み込みを登録しました。"
        if auto_ok else
        "- 自動起動の登録はスキップされました(必要なら後で設定できます)。"
    )
    lines += [
        "",
        "今すぐ使うには、シェルフの '{0}' ボタンを押すか、".format(SHELF_BUTTON_LABEL),
        "スクリプトエディタ(Python)で次を実行してください:",
        "    import maya_live_sync",
        "    maya_live_sync.show_ui()",
    ]
    message = "\n".join(lines)
    print("[Live Sync Installer]\n" + message)
    cmds.confirmDialog(title="Live Sync Installer", message=message, button=["OK"])
