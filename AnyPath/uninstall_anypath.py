"""
uninstall_anypath.py  -  AnyPath Maya drag-and-drop uninstaller
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §2.4（インストーラ／アンインストーラ要件）

やっていること（自動、install_anypath.py の逆操作）:
    1. 削除するファイルの一覧を事前に表示する（§2.4-2）
    2. ログと設定ファイルを削除するかどうかを個別に選択させる
       （既定は「残す」。トラブル調査に必要なため。§2.4-3）
    3. <MAYA_APP_DIR>/AnyPath/（plug-ins/, scripts/）と
       <MAYA_APP_DIR>/modules/AnyPath.mod を削除する
    4. 削除後、.mod の残骸が残っていないことを検証して報告する（§2.4-4）

**削除しないもの（既定）**:
    <MAYA_APP_DIR>/anypath/logs/ 配下のログ、および
    <MAYA_APP_DIR>/anypath/machine.json（マシン固有設定）は、
    利用者が明示的に「削除する」を選ばない限り残す。
    プロジェクト設定（<プロジェクトルート>/anypath/config.json）は
    プロジェクト固有の資産であり、本アンインストーラの対象外
    （そもそもインストール先とは別の場所にあるため触れない）。

対象: Maya 2026 以降（設計書 §1.2）。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §2.4 に基づき新規作成。
"""

__version__ = "1.0.0"

import os
import shutil
import traceback

import maya.cmds as cmds

MODULE_NAME = "AnyPath"
MOD_FILENAME = "AnyPath.mod"
INSTALL_SUBDIR = "AnyPath"


def onMayaDroppedPythonFile(*args, **kwargs):
    try:
        _run()
    except Exception as e:
        traceback.print_exc()
        _show_user_message(
            title="AnyPath Uninstaller",
            lines=[
                "アンインストールに失敗しました。",
                "",
                "次に何をすればよいか: Script Editor に表示された詳細を"
                "確認するか、もう一度お試しください。",
            ],
        )


def _show_user_message(title, lines):
    try:
        cmds.confirmDialog(title=title, message="\n".join(lines), button=["OK"])
    except Exception:
        print("[AnyPath Uninstaller] " + "\n".join(lines))


def _maya_app_dir():
    return cmds.internalVar(userAppDir=True).rstrip("/\\").replace("\\", "/")


def _modules_dir():
    return _maya_app_dir() + "/modules"


def _install_root():
    return _maya_app_dir() + "/" + INSTALL_SUBDIR


def _mod_path():
    return _modules_dir() + "/" + MOD_FILENAME


def _anypath_data_dir():
    """§6.7のログ出力先・§6.3のマシン固有設定と同じ基点。"""
    return _maya_app_dir() + "/anypath"


def _collect_removal_plan(include_logs_and_config):
    """
    削除対象ファイル/フォルダの一覧を作る（実際にはまだ削除しない。
    §2.4-2: 「削除するファイルの一覧を事前に表示する」ための下調べ）。
    """
    plan = []

    install_root = _install_root()
    if os.path.isdir(install_root):
        plan.append(("インストール本体一式", install_root))

    mod_path = _mod_path()
    if os.path.isfile(mod_path):
        plan.append((".mod ファイル", mod_path))

    if include_logs_and_config:
        data_dir = _anypath_data_dir()
        if os.path.isdir(data_dir):
            plan.append(("ログ・マシン固有設定", data_dir))

    return plan


def _remove_menu_if_present():
    """
    現在のMayaセッションにAnyPathメニューが残っていれば破棄する
    （プラグインがアンロードされないままアンインストールされるケースへの
    保険。AnyPath.py側のuninitializePluginが呼ばれるのが本来の経路だが、
    ここでも念のため直接試みる）。
    """
    try:
        if not cmds.about(batch=True) and cmds.menu("AnyPathMainMenu", exists=True):
            cmds.deleteUI("AnyPathMainMenu")
    except Exception:
        pass


def _unload_plugin_if_loaded():
    """AnyPathプラグインがロードされていればアンロードする。"""
    try:
        if cmds.pluginInfo("AnyPath.py", query=True, loaded=True):
            cmds.unloadPlugin("AnyPath.py", force=True)
    except Exception:
        pass


def _run():
    # --- 削除計画の事前提示（既定は「ログ・設定は残す」）（§2.4-2, §2.4-3）---
    include_logs_default = False
    preview_plan = _collect_removal_plan(include_logs_and_config=True)

    if not preview_plan:
        _show_user_message(
            title="AnyPath Uninstaller",
            lines=["AnyPath はインストールされていないようです。何もしませんでした。"],
        )
        return

    plan_lines = ["削除されるファイル/フォルダ:", ""]
    for label, path in _collect_removal_plan(include_logs_and_config=False):
        plan_lines.append("  - {0}: {1}".format(label, path))
    data_dir = _anypath_data_dir()
    has_data_dir = os.path.isdir(data_dir)
    if has_data_dir:
        plan_lines.append("")
        plan_lines.append("ログ・マシン固有設定（{0}）は既定では残します。".format(data_dir))
        plan_lines.append("トラブル調査に使う場合があるため、削除するかどうかは次の画面で選べます。")

    proceed = cmds.confirmDialog(
        title="AnyPath Uninstaller",
        message="\n".join(plan_lines),
        button=["続ける", "キャンセル"],
        defaultButton="続ける",
        cancelButton="キャンセル",
    )
    if proceed != "続ける":
        _show_user_message(title="AnyPath Uninstaller", lines=["アンインストールをキャンセルしました。"])
        return

    include_logs = False
    if has_data_dir:
        choice = cmds.confirmDialog(
            title="AnyPath Uninstaller",
            message=(
                "ログとマシン固有設定（{0}）も削除しますか？\n\n"
                "既定は「残す」です。トラブル調査に必要な場合があります。"
            ).format(data_dir),
            button=["残す（既定）", "削除する"],
            defaultButton="残す（既定）",
            cancelButton="残す（既定）",
        )
        include_logs = (choice == "削除する")

    # --- 実削除の前に、稼働中のプラグイン/メニューを片付ける ---
    _unload_plugin_if_loaded()
    _remove_menu_if_present()

    removed = []
    errors = []

    install_root = _install_root()
    if os.path.isdir(install_root):
        try:
            shutil.rmtree(install_root)
            removed.append(install_root)
        except Exception as e:
            errors.append((install_root, str(e)))

    mod_path = _mod_path()
    if os.path.isfile(mod_path):
        try:
            os.remove(mod_path)
            removed.append(mod_path)
        except Exception as e:
            errors.append((mod_path, str(e)))

    if include_logs and os.path.isdir(data_dir):
        try:
            shutil.rmtree(data_dir)
            removed.append(data_dir)
        except Exception as e:
            errors.append((data_dir, str(e)))

    # --- 削除後、.mod の残骸が残っていないことを検証（§2.4-4） ---
    residual_mods = []
    module_path_env = os.environ.get("MAYA_MODULE_PATH", "")
    separator = ";" if os.name == "nt" else ":"
    search_dirs = [d.strip() for d in module_path_env.split(separator) if d.strip()]
    if _modules_dir() not in search_dirs:
        search_dirs.append(_modules_dir())
    for d in search_dirs:
        candidate = os.path.join(d, MOD_FILENAME)
        if os.path.isfile(candidate):
            residual_mods.append(candidate.replace("\\", "/"))

    lines = ["AnyPath のアンインストールが完了しました。", ""]
    lines.append("[削除しました]")
    for path in removed:
        lines.append("  - {0}".format(path))
    if not removed:
        lines.append("  （削除対象が見つかりませんでした）")

    if not include_logs and has_data_dir:
        lines.append("")
        lines.append("[残しました] ログ・マシン固有設定: {0}".format(data_dir))

    if errors:
        lines.append("")
        lines.append("[削除できなかったもの]")
        for path, err in errors:
            lines.append("  - {0}: {1}".format(path, err))

    lines.append("")
    if residual_mods:
        lines.append("[要確認] 削除後も AnyPath.mod の残骸が検出されました:")
        for r in residual_mods:
            lines.append("  - {0}".format(r))
        lines.append("手動での確認をお願いします。")
    else:
        lines.append("[確認] .mod の残骸は検出されませんでした。")

    lines.append("")
    lines.append("次に何をすればよいか: Maya を再起動すると完全に反映されます。")

    _show_user_message(title="AnyPath Uninstaller", lines=lines)
