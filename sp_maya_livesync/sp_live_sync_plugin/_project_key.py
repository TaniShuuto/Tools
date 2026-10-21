"""現在のプロジェクトのキーと、Final サブフォルダ名。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import os
import re
import hashlib

import substance_painter.project as project

__all__ = [
    "_current_project_key",
    "_project_subfolder_name",
]


def _current_project_key():
    """現在のプロジェクトを一意に識別するキーを返す。
    未保存プロジェクトの場合は固定文字列を使う(この場合、複数の未保存
    プロジェクトを区別できない点は既知の制限とする)。

    2026.07.16(緊急修正): project.file_path() は公式ドキュメント上
    「プロジェクトが未保存ならNoneを返す」とされているが、実機では
    テンプレートから新規プロジェクトを作成した直後、一度も保存して
    いない状態で、Noneではなく作成元テンプレート(.spt)のパスを
    返すケースが確認された。この値をそのままプロジェクトキーとして
    使うと、Maya側の紐付けUIに「TEST_1」ではなく
    「PBR - Metallic Roughness Alpha-blend.spt」のようなテンプレート名が
    表示されてしまい、実際のプロジェクトと異なるキーで紐付けが行われる
    (ドキュメントに明記されていない、実装依存の挙動と考えられる)。

    対策として、公式に「未保存なら確実にNoneを返す」と明記されている
    project.name() を先に確認し、これがNoneであれば file_path() の値を
    信頼せず "__unsaved__" にフォールバックするようにした。

    2026.07.25(緊急バグ修正): 上記の project.name() ガードだけでは
    このテンプレート漏れを捕まえきれないケースが実機で確認された
    (project.name() が真になった状態でも file_path() がテンプレートの
    .sptパスを返し続けることがある)。file_path() の拡張子が .spt
    (大文字小文字を区別しない)の場合も "__unsaved__" として扱うガードを
    追加した。これにより、テンプレート由来の未保存プロジェクトの
    キーが watch_subfolder_by_project/known_texture_sets_by_project 等の
    *_by_project 辞書へ本来のプロジェクト名の代わりに紛れ込む不具合
    (実機のlive_sync_config.jsonで
    ".../PBR - Metallic Roughness Alpha-blend.spt" というキーの残留を
    確認)を防ぐ。この状態は既存の "__unsaved__" ガード(シーン紐付け・
    create_shader_network()の拒否)が正しく機能する前提でもある。
    """
    try:
        name = project.name()
    except Exception:
        name = None

    if not name:
        # project.name() がNoneを返す = 公式仕様上、確実に未保存。
        # file_path() が(ドキュメントの契約に反して)何らかのパスを
        # 返していても、それは信頼できないため無視する。
        return "__unsaved__"

    try:
        path = project.file_path()
    except Exception:
        path = None

    if path and path.lower().endswith(".spt"):
        # テンプレートファイル自体のパスが漏れてきたケース。実プロジェクト
        # として保存されるまでは "__unsaved__" 扱いにする。
        return "__unsaved__"

    return path or "__unsaved__"


def _project_subfolder_name():
    """現在のプロジェクト用の Final サブフォルダ名を返す。

    形式: <ファイル名(拡張子なし)>_<フルパスのハッシュ先頭6文字>
      例: C:/work/ProjectA/proA.spp -> "proA_a1b2c3"

    - ファイル名部分で人が見て判別でき、ハッシュ部分で「別の場所にある
      同名プロジェクト」同士の衝突を防ぐ(両立方式)。
    - 未保存プロジェクトはキーが "__unsaved__" 固定のため、フォルダ名も
      "__unsaved__" になる(未保存同士は区別できないという既存の制限を
      そのまま引き継ぐ)。この場合ファイル名部分は付けない。
    - ファイル名部分は、フォルダ名として使えない文字を除去して安全化する。
    """
    key = _current_project_key()
    if key == "__unsaved__":
        return "__unsaved__"

    stem = os.path.splitext(os.path.basename(key))[0]
    # フォルダ名に使えない文字を除去(Windows/一般で不正な文字を _ に)
    safe_stem = re.sub(r'[<>:"/\\|?*\s]', "_", stem).strip("_") or "project"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:6]
    return "{0}_{1}".format(safe_stem, digest)
