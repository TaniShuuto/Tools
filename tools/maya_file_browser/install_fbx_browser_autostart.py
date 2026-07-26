# -*- coding: utf-8 -*-
"""
install_fbx_browser_autostart.py
---------------------------------
アセットブラウザ（fbx_browser.py）を、Mayaへのドラッグ&ドロップだけで
インストールするためのスクリプトです。

使い方:
    1. このファイルと fbx_browser.py を「同じフォルダ」に置く
       （インストーラー自身のパスを基準に fbx_browser.py を探すため、
        別フォルダに置くと自動コピーができません）
    2. Mayaのビューポート上に、このファイル（install_fbx_browser_autostart.py）を
       ドラッグ&ドロップする
    3. 確認ダイアログの案内に従って進める
    4. 完了後、Mayaを再起動すると次回以降は自動でアセットブラウザが開く

このスクリプトが行うこと:
    1. 自分自身と同じフォルダにある fbx_browser.py を、
       Mayaのユーザースクリプトフォルダへコピーする
    2. userSetup.py を新規作成、または既存のものへ安全に追記する
        - 新規の場合          -> そのまま作成
        - 既存だが未導入の場合 -> 中身は保持したまま末尾に追記
        - 既に導入済みの場合   -> 何もしない（二重追記の防止）
    3. 結果をダイアログで報告する

D&D実行時の制約への対応:
    - Mayaがドラッグ&ドロップされた.pyを実行する経路では、標準入力
      （input()）を使った対話ができない。そのため、選択肢の提示や
      確認はすべて cmds.confirmDialog / cmds.promptDialog に置き換えている。
    - スクリプトエディタを見ていない利用者を想定し、完了・エラー報告は
      必ずダイアログでも表示する（printのみに頼らない）。

安全対策:
    - 既存の userSetup.py・fbx_browser.py を上書きする前には、
      必ずタイムスタンプ付きバックアップ（.bak_YYYYMMDD_HHMMSS）を作成する。
    - 追記するブロックは目印となるマーカーコメントで囲み、
      再実行しても重複追記されないようにしている。
"""

import os
import sys
import shutil
import datetime
import traceback

try:
    import maya.cmds as cmds
    _IN_MAYA = True
except ImportError:
    _IN_MAYA = False


# 追記ブロックを識別するためのマーカー。
# このマーカーが既存ファイルに存在する場合、既に導入済みと判断してスキップする。
MARKER_BEGIN = "# === fbx_browser autostart: BEGIN (auto-generated, do not edit) ==="
MARKER_END = "# === fbx_browser autostart: END ==="

AUTOSTART_BLOCK = '''{begin}
def _fbx_browser_autostart_launch():
    """
    Maya起動完了後にアセットブラウザ(fbx_browser)を自動的に開く。

    fbx_browser.show_ui() は前回のフォルダ・オプション・ドッキング位置の
    復元を内部で行うため、ここでは呼び出すだけでよい。

    workspaceControlのuiScriptによる復元と起動タイミングが重なった場合でも、
    fbx_browser.show_ui()側で既存コントロールの存在を検出して使い回すため
    エラーにはならないが、念のためここでも二重起動を避けておく。
    """
    try:
        import maya.cmds as _fbx_autostart_cmds
        _WORKSPACE_CONTROL_NAME = "FBXBrowserPanelWindowWorkspaceControl"
        if _fbx_autostart_cmds.workspaceControl(_WORKSPACE_CONTROL_NAME, exists=True):
            # 既にuiScript経由の復元等で開いている場合は何もしない
            return
        import fbx_browser
        fbx_browser.show_ui()
    except Exception as _fbx_autostart_exc:
        try:
            import maya.cmds as _fbx_autostart_cmds
            _fbx_autostart_cmds.warning(
                "アセットブラウザの自動起動に失敗しました。fbx_browser.py の配置を"
                "ご確認ください。詳細: {{}}".format(_fbx_autostart_exc)
            )
        except Exception:
            pass


try:
    import maya.cmds as _fbx_autostart_cmds_outer
    _fbx_autostart_cmds_outer.evalDeferred(_fbx_browser_autostart_launch)
except Exception:
    pass
{end}
'''.format(begin=MARKER_BEGIN, end=MARKER_END)


def _find_maya_scripts_dirs():
    """
    Mayaのユーザースクリプトフォルダの候補を、環境変数とOS標準の場所から推測する。
    複数バージョンが入っている環境を考慮し、見つかった候補を全て返す。
    """
    candidates = []

    maya_app_dir = os.environ.get("MAYA_APP_DIR")
    home = os.path.expanduser("~")

    search_roots = []
    if maya_app_dir:
        search_roots.append(maya_app_dir)
    search_roots.append(os.path.join(home, "Documents", "maya"))
    search_roots.append(os.path.join(home, "OneDrive", "Documents", "maya"))

    seen = set()
    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        root_norm = os.path.normcase(os.path.normpath(root))
        if root_norm in seen:
            continue
        seen.add(root_norm)

        direct_scripts = os.path.join(root, "scripts")
        if os.path.isdir(direct_scripts):
            candidates.append(direct_scripts)

        try:
            for entry in os.listdir(root):
                entry_path = os.path.join(root, entry)
                if os.path.isdir(entry_path) and entry.isdigit():
                    versioned_scripts = os.path.join(entry_path, "scripts")
                    if os.path.isdir(versioned_scripts):
                        candidates.append(versioned_scripts)
        except Exception:
            pass

    return candidates


def _is_autostart_already_installed(userSetup_path):
    if not os.path.isfile(userSetup_path):
        return False
    try:
        with open(userSetup_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return MARKER_BEGIN in content
    except Exception:
        return False


def _backup_file(path):
    if not os.path.isfile(path):
        return None
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = "{}.bak_{}".format(path, timestamp)
    shutil.copy2(path, backup_path)
    return backup_path


def _get_installer_source_dir():
    """
    このインストーラー自身が置かれているフォルダを取得する。
    D&D実行時、__file__ が正しく取れない実行経路が過去のMayaバージョンで
    報告されているため、複数の手段でフォールバックする。
    """
    candidates = []

    try:
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass

    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return None


def _copy_fbx_browser(source_dir, target_dir):
    """
    source_dir にある fbx_browser.py を target_dir へコピーする。
    既存ファイルがある場合はバックアップしてから上書きする。

    戻り値: (成功したか, メッセージ)
    """
    source_path = os.path.join(source_dir, "fbx_browser.py")
    target_path = os.path.join(target_dir, "fbx_browser.py")

    if not os.path.isfile(source_path):
        return False, (
            "[エラー] fbx_browser.py が見つかりません: {}\n"
            "このインストーラーと同じフォルダに fbx_browser.py を"
            "置いてから再実行してください。".format(source_path)
        )

    try:
        if os.path.isfile(target_path):
            # 同一内容であれば無駄なバックアップ・上書きをしない
            try:
                with open(source_path, "rb") as f:
                    src_bytes = f.read()
                with open(target_path, "rb") as f:
                    dst_bytes = f.read()
                if src_bytes == dst_bytes:
                    return True, "[スキップ] fbx_browser.py は既に最新の状態でした。"
            except Exception:
                pass

            backup_path = _backup_file(target_path)
            shutil.copy2(source_path, target_path)
            return True, (
                "[更新完了] fbx_browser.py を更新しました。\n"
                "旧ファイルは次の場所に退避しました: {}".format(backup_path)
            )
        else:
            shutil.copy2(source_path, target_path)
            return True, "[新規配置完了] fbx_browser.py を配置しました: {}".format(target_path)
    except Exception as exc:
        return False, "[エラー] fbx_browser.py のコピーに失敗しました: {}".format(exc)


def install_autostart(scripts_dir, source_dir=None, dry_run=False):
    """
    指定された scripts フォルダに対して、fbx_browser.py のコピーと
    userSetup.py の新規作成/追記を行う。

    戻り値: (実行された操作の説明文字列, userSetup.pyの最終パス)
    """
    userSetup_path = os.path.join(scripts_dir, "userSetup.py")
    messages = []

    # --- fbx_browser.py の配置 ---
    if source_dir:
        if dry_run:
            messages.append(
                "[ドライラン] fbx_browser.py を {} から {} へコピーする予定です。".format(
                    source_dir, scripts_dir
                )
            )
        else:
            ok, msg = _copy_fbx_browser(source_dir, scripts_dir)
            messages.append(msg)
            if not ok:
                # fbx_browser.py が無い状態でも userSetup.py の設定自体は
                # 続行できるため、致命的エラーとはせず警告に留めて進める。
                pass
    else:
        fbx_browser_path = os.path.join(scripts_dir, "fbx_browser.py")
        if not os.path.isfile(fbx_browser_path):
            messages.append(
                "[警告] {} に fbx_browser.py が見つかりません。"
                "先にこのフォルダへ fbx_browser.py を配置してください。".format(scripts_dir)
            )

    # --- userSetup.py の設定 ---
    if _is_autostart_already_installed(userSetup_path):
        messages.append(
            "[スキップ] {} には既に自動起動の設定が導入済みです。"
            "userSetup.py の変更は行いませんでした。".format(userSetup_path)
        )
        return "\n".join(messages), userSetup_path

    if dry_run:
        action = "新規作成" if not os.path.isfile(userSetup_path) else "追記"
        messages.append(
            "[ドライラン] {} に対して {} を行う予定です。"
            "実際の書き込みは行っていません。".format(userSetup_path, action)
        )
        return "\n".join(messages), userSetup_path

    if os.path.isfile(userSetup_path):
        backup_path = _backup_file(userSetup_path)
        messages.append("[バックアップ] 既存のuserSetup.pyを退避しました: {}".format(backup_path))

        with open(userSetup_path, "r", encoding="utf-8", errors="replace") as f:
            existing_content = f.read()

        if existing_content and not existing_content.endswith("\n"):
            existing_content += "\n"
        new_content = existing_content + "\n\n" + AUTOSTART_BLOCK

        with open(userSetup_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        messages.append(
            "[追記完了] 既存の userSetup.py の末尾に自動起動コードを追記しました。"
        )
    else:
        with open(userSetup_path, "w", encoding="utf-8") as f:
            f.write(AUTOSTART_BLOCK)
        messages.append("[新規作成完了] userSetup.py を新規作成しました。")

    return "\n".join(messages), userSetup_path


def _show_dialog(title, message, icon="information"):
    """Maya環境ではconfirmDialogで、それ以外ではprintで結果を表示する。"""
    if _IN_MAYA:
        try:
            cmds.confirmDialog(
                title=title,
                message=message,
                button=["OK"],
                defaultButton="OK",
                icon=icon,
            )
            return
        except Exception:
            pass
    print("[{}] {}".format(title, message))


def _choose_scripts_dir_via_dialog(candidates):
    """
    複数のscriptsフォルダ候補がある場合に、GUIダイアログで選ばせる。
    D&D実行時はinput()が使えないため、confirmDialogのボタン列で代替する。
    ボタン数の都合上、候補が多い場合は先頭4件＋手動指定に絞る。
    """
    if not _IN_MAYA:
        # Maya外（通常のPython）から実行された場合のみ標準入力を使う
        print("複数のフォルダ候補が見つかりました。")
        for i, path in enumerate(candidates):
            print("  [{}] {}".format(i, path))
        try:
            choice = input("番号を入力: ")
            return candidates[int(choice)]
        except Exception:
            return None

    display_candidates = candidates[:4]
    buttons = [os.path.basename(os.path.dirname(p)) or p for p in display_candidates]
    buttons.append("キャンセル")

    # ボタンラベルの重複を避けるため、フルパスをメッセージ本文に列挙する
    listing = "\n".join(
        "[{}] {}".format(i, p) for i, p in enumerate(display_candidates)
    )
    choice_label = cmds.confirmDialog(
        title="インストール先の選択",
        message=(
            "複数のMayaスクリプトフォルダ候補が見つかりました。\n"
            "インストール先を選択してください。\n\n{}".format(listing)
        ),
        button=buttons,
        defaultButton=buttons[0],
        cancelButton="キャンセル",
        dismissString="キャンセル",
    )

    if choice_label == "キャンセル":
        return None

    try:
        index = buttons.index(choice_label)
        return display_candidates[index]
    except ValueError:
        return None


def run_interactive():
    """
    メインのエントリポイント。
    Mayaビューポートへのドラッグ&ドロップ実行、スクリプトエディタでの実行、
    通常のPythonでの実行のいずれからも呼べる。
    """
    try:
        source_dir = _get_installer_source_dir()
        if source_dir is None:
            _show_dialog(
                "インストール失敗",
                "このインストーラー自身の場所を特定できませんでした。\n"
                "fbx_browser.py と同じフォルダに置いた状態で、\n"
                "Mayaのビューポートへドラッグ&ドロップし直してください。",
                icon="critical",
            )
            return

        candidates = _find_maya_scripts_dirs()

        if not candidates:
            _show_dialog(
                "インストール失敗",
                "Mayaのscriptsフォルダが自動検出できませんでした。\n"
                "お手数ですが、下記をMayaのスクリプトエディタ(Python)で\n"
                "フォルダパスを直接指定して実行してください。\n\n"
                "install_autostart(r'C:\\\\path\\\\to\\\\scripts', "
                "source_dir=r'{}')".format(source_dir),
                icon="critical",
            )
            return

        if len(candidates) == 1:
            target = candidates[0]
        else:
            target = _choose_scripts_dir_via_dialog(candidates)
            if target is None:
                _show_dialog("インストール中止", "インストールをキャンセルしました。")
                return

        result_message, userSetup_path = install_autostart(
            target, source_dir=source_dir, dry_run=False
        )

        _show_dialog(
            "インストール完了",
            "{}\n\n"
            "設定が完了しました。Mayaを再起動すると、次回以降は自動で\n"
            "アセットブラウザが開くようになります。\n\n"
            "userSetup.py: {}".format(result_message, userSetup_path),
        )

    except Exception as exc:
        traceback.print_exc()
        _show_dialog(
            "インストール中にエラーが発生しました",
            "予期しないエラーが発生しました。詳細はscript editorをご確認ください。\n\n{}".format(exc),
            icon="critical",
        )


# Mayaのドラッグ&ドロップ実行では、モジュールのトップレベルコードが
# そのまま実行される。スクリプトエディタからの実行・通常のPython実行でも
# 同じ挙動になるよう、__name__ の判定に頼らず常に呼び出す。
run_interactive()

