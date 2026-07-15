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


def _add_shelf_button(label, annotation, command):
    """アクティブなシェルフにボタンを1つ追加する。成否を返す。"""
    try:
        current_shelf = mel.eval("tabLayout -q -selectTab $gShelfTopLevel")
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

    # コピーした各ツールについて、以前このセッションでimport済みなら
    # 強制的に再読込する(再D&D時に古いコードのまま動く問題への対処)。
    for name in copied:
        module_name = os.path.splitext(name)[0]
        _reload_if_cached(module_name)

    has_aiss = "sp_to_aiStandardSurface.py" in copied

    livesync_shelf = _add_shelf_button(LIVESYNC_LABEL, LIVESYNC_ANNOTATION, LIVESYNC_COMMAND)
    aiss_shelf = _add_shelf_button(AISS_LABEL, AISS_ANNOTATION, AISS_COMMAND) if has_aiss else False

    auto_ok = _register_autostart()

    # --- 完了メッセージ ---
    lines = ["SP -> Maya Live Sync のインストールが完了しました。", ""]

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
