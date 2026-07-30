"""
install_anypath.py  -  AnyPath Maya drag-and-drop installer
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §2.4（インストーラ／アンインストーラ要件）

使い方:
    この install_anypath.py を、AnyPath.py / anypath/（Core パッケージ）と
    同じフォルダに置いたまま、Maya のビューポートへドラッグ＆ドロップする
    だけで完了する（設計書 §2.4-1: 「fbx_browser.py で実績のある方式を流用」。
    本リポジトリでは install.py の実績ある方式を流用している）。

やっていること（自動）:
    1. MAYA_APP_DIR を自動検出する（cmds.internalVar(userAppDir=True)。
       C:\\MayaPrefs のようなカスタム設定でも正しく動作する。パスを
       利用者に入力させない。§2.4-2）
    2. 書き込み権限を事前に確認する。失敗する場合は着手前に理由を提示する
       （§2.4-5）
    3. 既存インストールを検出する。既に AnyPath.mod が存在する場合は
       上書き前に版数を表示し、確認を求める（§2.4-3）
    4. 複数の MAYA_MODULE_PATH 上の重複を検出する。他の場所に AnyPath.mod
       があれば警告し、どちらを残すか選択させる（§2.4-4）
    5. <MAYA_APP_DIR>/modules/AnyPath.mod を生成する
    6. AnyPath.py と anypath/ パッケージ一式を <MAYA_APP_DIR>/AnyPath/
       （.mod の検索パス基点）配下の scripts/ と plug-ins/ へ配置する
    7. インストーラ自身の例外も捕捉し、トレースバックを表示しない
       （§2.4-7、§6.8のエラーメッセージ規約に従う）
    8. 完了時に「次に何をすればよいか」を1行で示す（§2.4-6）

対象: Maya 2026 以降（設計書 §1.2）。ドラッグ＆ドロップ実行自体は
Maya 2017 Update 3 以降の一般機能。

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

# .modが検索パスに追加する scripts/ plug-ins/ の実体を置くディレクトリ名。
# <MAYA_APP_DIR>/AnyPath/{scripts,plug-ins} という構成にする。
INSTALL_SUBDIR = "AnyPath"


def onMayaDroppedPythonFile(*args, **kwargs):
    """ドラッグ&ドロップ実行のために Maya が要求するエントリポイント。"""
    try:
        _run()
    except Exception as e:
        # §6.8: UIにトレースバックを表示しない。Script Editorへは詳細を残す。
        traceback.print_exc()
        _show_user_message(
            title="AnyPath Installer",
            lines=[
                "インストールに失敗しました。",
                "",
                "何が起きたか: セットアップ処理の途中でエラーが発生しました。",
                "次に何をすればよいか: Script Editor に表示された詳細を確認するか、"
                "もう一度ドラッグ＆ドロップをお試しください。",
            ],
        )


def _show_user_message(title, lines):
    try:
        cmds.confirmDialog(title=title, message="\n".join(lines), button=["OK"])
    except Exception:
        print("[AnyPath Installer] " + "\n".join(lines))


# --- パス取得（ハードコード禁止・§2.3/§2.4-2） -------------------------------
def _maya_app_dir():
    """MAYA_APP_DIR をカスタム設定込みで自動検出する。"""
    return cmds.internalVar(userAppDir=True).rstrip("/\\").replace("\\", "/")


def _modules_dir():
    return _maya_app_dir() + "/modules"


def _install_root():
    return _maya_app_dir() + "/" + INSTALL_SUBDIR


def _mod_path():
    return _modules_dir() + "/" + MOD_FILENAME


# --- 書き込み権限の事前確認（§2.4-5） ----------------------------------------
def _check_writable(dir_path):
    """
    dir_path（無ければ作成を試みて）への書き込み権限を事前確認する。
    戻り値: (ok: bool, reason: str or None)
    """
    try:
        os.makedirs(dir_path, exist_ok=True)
    except Exception as e:
        return False, "フォルダを作成できませんでした ({0}): {1}".format(dir_path, e)

    probe_path = os.path.join(dir_path, ".anypath_write_probe.tmp")
    try:
        with open(probe_path, "w") as f:
            f.write("probe")
        os.remove(probe_path)
        return True, None
    except Exception as e:
        return False, "書き込み権限がありません ({0}): {1}".format(dir_path, e)


# --- 既存インストール検出（§2.4-3） ------------------------------------------
def _read_existing_version(mod_path, install_root):
    """
    既存の AnyPath.mod / AnyPath.py から版数を読み取る。
    取得できなければ None。
    """
    plugin_file = os.path.join(install_root, "plug-ins", "AnyPath.py")
    if not os.path.isfile(plugin_file):
        return None
    try:
        with open(plugin_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("__version__"):
                    # __version__ = "1.0.0" の形式から値を抜き出す
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


# --- 複数 MAYA_MODULE_PATH 上の重複検出（§2.4-4） ---------------------------
def _find_all_mod_files():
    """
    MAYA_MODULE_PATH 上に存在する全ての AnyPath.mod を検出する
    （これからインストールしようとしている modules_dir も含む）。
    """
    found = []
    module_path_env = os.environ.get("MAYA_MODULE_PATH", "")
    separator = ";" if os.name == "nt" else ":"
    search_dirs = [d.strip() for d in module_path_env.split(separator) if d.strip()]
    if _modules_dir() not in search_dirs:
        search_dirs.append(_modules_dir())
    for d in search_dirs:
        candidate = os.path.join(d, MOD_FILENAME)
        if os.path.isfile(candidate):
            found.append(candidate.replace("\\", "/"))
    # 決定論的な表示順にするためソート
    return sorted(set(found))


# --- .mod 生成 ----------------------------------------------------------------
def _write_mod_file(mod_path, install_root):
    """
    <install_root>/AnyPath.mod を生成する。
    .mod フォーマット: "+ MODULE_NAME VERSION INSTALL_ROOT"
    scripts/ と plug-ins/ は Maya の規約上、自動的に検索パスへ追加される
    （.mod ファイルの記法上、追加のパス指定行は不要）。
    """
    content = "+ {0} {1} {2}\n".format(MODULE_NAME, __version__, install_root)
    tmp_path = mod_path + ".tmp"
    # CLAUDE.md規約: 他プロセスが同時に読む可能性のあるファイルシステムへの
    # 書き込みは、一時ファイル＋os.replace()で行う。
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, mod_path)


def _copy_payload(source_dir, install_root):
    """
    AnyPath.py と anypath/ パッケージ一式を install_root/plug-ins/ および
    install_root/scripts/ へ配置する。

    plug-ins/AnyPath.py が autoload されるプラグイン本体。
    anypath/ パッケージ（core/等）は import 解決のため scripts/ 配下へ置く
    （.mod が scripts/ を検索パスへ追加するため、"import anypath.core..." が
    そのまま解決できる）。
    """
    plugins_dir = os.path.join(install_root, "plug-ins")
    scripts_dir = os.path.join(install_root, "scripts")
    os.makedirs(plugins_dir, exist_ok=True)
    os.makedirs(scripts_dir, exist_ok=True)

    src_plugin = os.path.join(source_dir, "AnyPath.py")
    if not os.path.isfile(src_plugin):
        # boot/ フォルダ配下からドラッグ&ドロップされた場合、AnyPath.py は
        # 同じフォルダにあるはずだが、リポジトリ構成によって親フォルダに
        # あるケースにも一応対応する。
        alt = os.path.join(os.path.dirname(source_dir), "AnyPath.py")
        if os.path.isfile(alt):
            src_plugin = alt
        else:
            raise RuntimeError(
                "AnyPath.py が見つかりません。install_anypath.py と同じフォルダに"
                "配置してください。")
    shutil.copy2(src_plugin, os.path.join(plugins_dir, "AnyPath.py"))

    src_package = os.path.join(source_dir, "anypath")
    if not os.path.isdir(src_package):
        alt_package = os.path.join(os.path.dirname(source_dir), "anypath")
        if os.path.isdir(alt_package):
            src_package = alt_package
        else:
            raise RuntimeError(
                "anypath パッケージ（core フォルダ一式）が見つかりません。"
                "install_anypath.py と同じ場所に配置してください。")

    dst_package = os.path.join(scripts_dir, "anypath")
    if os.path.isdir(dst_package):
        shutil.rmtree(dst_package)
    shutil.copytree(src_package, dst_package,
                     ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "boot"))

    return True


def _run():
    source_dir = os.path.dirname(os.path.abspath(__file__))

    app_dir = _maya_app_dir()
    modules_dir = _modules_dir()
    install_root = _install_root()
    mod_path = _mod_path()

    # --- 書き込み権限の事前確認（§2.4-5） ---
    ok, reason = _check_writable(modules_dir)
    if not ok:
        _show_user_message(
            title="AnyPath Installer",
            lines=[
                "インストールできません。",
                "",
                "何が起きたか: {0} への書き込みができません。".format(modules_dir),
                "なぜか: {0}".format(reason),
                "次に何をすればよいか: フォルダの権限を確認するか、"
                "管理者に相談してください。",
            ],
        )
        return

    ok2, reason2 = _check_writable(install_root)
    if not ok2:
        _show_user_message(
            title="AnyPath Installer",
            lines=[
                "インストールできません。",
                "",
                "何が起きたか: {0} への書き込みができません。".format(install_root),
                "なぜか: {0}".format(reason2),
                "次に何をすればよいか: フォルダの権限を確認してください。",
            ],
        )
        return

    # --- 既存インストールの検出（§2.4-3） ---
    existing_version = None
    if os.path.isfile(mod_path):
        existing_version = _read_existing_version(mod_path, install_root)
        proceed = cmds.confirmDialog(
            title="AnyPath Installer",
            message=(
                "AnyPath は既にインストールされています"
                "（バージョン: {0}）。\n\n"
                "上書きしてインストールを続けますか？"
            ).format(existing_version or "不明"),
            button=["上書きする", "キャンセル"],
            defaultButton="上書きする",
            cancelButton="キャンセル",
        )
        if proceed != "上書きする":
            _show_user_message(
                title="AnyPath Installer",
                lines=["インストールをキャンセルしました。"],
            )
            return

    # --- 複数 MAYA_MODULE_PATH 上の重複検出（§2.4-4） ---
    duplicates = [p for p in _find_all_mod_files() if p != mod_path.replace("\\", "/")]
    if duplicates:
        dup_list = "\n".join("  - {0}".format(d) for d in duplicates)
        choice = cmds.confirmDialog(
            title="AnyPath Installer",
            message=(
                "他の場所にも AnyPath.mod が見つかりました:\n{0}\n\n"
                "同時に有効なままだと動作が不安定になる可能性があります。\n"
                "このままインストールを続けますか？"
                "（他の場所のファイルは自動では削除されません。"
                "後で自己診断画面から確認できます）"
            ).format(dup_list),
            button=["続ける", "キャンセル"],
            defaultButton="続ける",
            cancelButton="キャンセル",
        )
        if choice != "続ける":
            _show_user_message(
                title="AnyPath Installer",
                lines=["インストールをキャンセルしました。"],
            )
            return

    # --- 配置 ---
    _copy_payload(source_dir, install_root)
    _write_mod_file(mod_path, install_root)

    new_version = __version__

    lines = ["AnyPath のインストールが完了しました。", ""]
    if existing_version and existing_version != new_version:
        lines.append("[バージョン] {0} -> {1} に更新しました。".format(
            existing_version, new_version))
    elif existing_version:
        lines.append("[バージョン] {0}（再インストール）".format(new_version))
    else:
        lines.append("[バージョン] {0}（新規インストール）".format(new_version))
    lines.append("")
    lines.append("[インストール先] {0}".format(install_root))
    lines.append("[モジュールファイル] {0}".format(mod_path))
    if duplicates:
        lines.append("")
        lines.append("[要確認] 他の場所にも AnyPath.mod が見つかっています。"
                      "自己診断画面から状況を確認してください。")
    lines.append("")
    lines.append("次に何をすればよいか: Maya を再起動すると有効になります。")

    _show_user_message(title="AnyPath Installer", lines=lines)
