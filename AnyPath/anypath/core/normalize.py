"""
anypath.core.normalize  -  AnyPath Core: 文字列正規化
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §3.8（文字列正規化の落とし穴）

対象の落とし穴（個別テストケース必須、§9.1）:
    - 区切り文字（\\ と / の混在、\\\\ のエスケープ）
    - UNC パス（\\\\server\\share\\...）―― ドライブレターの概念がない
    - 末尾スラッシュの有無
    - 日本語・全角を含むパス（UTF-8 で統一。cp932 混入の検出）
    - Windows の 260 文字パス長制限
    - 相対パス（./、../）の解釈基準 → 「現在の Maya Project ルート」に固定する
    - 環境変数が未定義のまま $VAR が残留しているケース

**Maya API を一切 import しない**（§2.1）。

変更履歴:
    1.1.0 (2026.07.31):
        - ユーザー指摘対応（「Modo_UV_checke.jpg では
          Modo_UV_checker.jpg の候補が出てこない」）: あいまいファイル名
          マッチング（S4の第3フォールバック、cascade.py/index.py側で
          利用）向けに levenshtein_distance() を新設した。
        - 後方互換性: MINOR。既存関数への変更なし。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 に基づき新規作成。
"""

__version__ = "1.1.0"

import os
import re

# Windows のパス長制限（ドライブレター込みの一般的な上限）。
# \\?\ プレフィックスによる長パス対応は本システムの対象外とする
# （設計書に明記はないが、対象環境がドラッグ&ドロップで導入する
#  非エンジニア向けツールであるため、OS標準の挙動に委ねる）。
WINDOWS_MAX_PATH = 260

# 未解決の環境変数トークンを検出する正規表現（$VAR / ${VAR} / %VAR% 形式）。
_ENV_VAR_PATTERN = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%")

# UNC パス判定（\\server\share\... または //server/share/...）。
_UNC_PATTERN = re.compile(r"^(\\\\|//)[^\\/]+[\\/][^\\/]+")

# cp932 特有の後方互換文字（波ダッシュ〜/全角チルダ等、UTF-8混入検出のヒント）。
# 完全な判定はできないため、あくまで「怪しい」を検出する簡易ヒューリスティックとする。
_CP932_SUSPECT_CHARS = ("\uff5e", "\u2225", "\uffe0", "\uffe1", "\uff0d")


def to_posix_slashes(path):
    """
    区切り文字をすべて '/' へ統一する。

    Windows の '\\' と '/' が混在したパス、および '\\\\'（エスケープされた
    バックスラッシュがそのまま文字列に残っているケース）の両方を吸収する。
    UNC パスの先頭 '\\\\server\\share' は '//server/share' へ変換される
    （ドライブレターの概念がないことに注意。is_unc_path() で別途判定する）。
    """
    if path is None:
        return path
    # 連続するバックスラッシュ（\\\\ のような二重エスケープ跡）を単一の
    # 区切りへ畳み込んでから '/' へ統一する。ただし UNC の先頭 '\\\\' だけは
    # 「サーバ区切り」として意味を持つため、後段の正規化で個別に扱う。
    normalized = path.replace("\\", "/")
    # UNC の場合、先頭の // が1個に潰れないようにする
    if path.startswith("\\\\") or path.startswith("//"):
        rest = normalized.lstrip("/")
        normalized = "//" + rest
    # 連続スラッシュを1つに畳む（ただし先頭のUNC用 // は保持する）
    if normalized.startswith("//"):
        collapsed = "//" + re.sub(r"/{2,}", "/", normalized[2:])
    else:
        collapsed = re.sub(r"/{2,}", "/", normalized)
    return collapsed


def is_unc_path(path):
    """UNC パス（\\\\server\\share\\... または //server/share/...）かどうか判定する。"""
    if not path:
        return False
    return bool(_UNC_PATTERN.match(path))


def strip_trailing_slash(path):
    """末尾スラッシュを除去する（ルート '/' や UNC のサーバ直下は保持する）。"""
    if not path:
        return path
    posix = path
    if len(posix) <= 1:
        return posix
    if posix.endswith("/") or posix.endswith("\\"):
        trimmed = posix.rstrip("/\\")
        # UNC のサーバ直下 (//server) や ドライブ直下 (C:/) まで削り切らない
        if trimmed == "" or re.match(r"^[A-Za-z]:$", trimmed):
            return trimmed + "/"
        if trimmed.startswith("//") and trimmed.count("/") <= 2:
            return trimmed + "/"
        return trimmed
    return posix


def detect_cp932_mojibake(path):
    """
    UTF-8 のパスに cp932 混入の疑いがある文字が含まれるかを簡易検出する。

    完全な判定は不可能（テキストの意味を解析しない限り確実な判別はできない）
    ため、既知の文字化けパターン（機種依存文字・全角記号の異常な混入等）を
    ヒューリスティックに検出するに留める。検出時は呼び出し側でログへの
    警告記録に用いる想定（Coreはログ出力自体は行わない）。
    """
    if not path:
        return False
    return any(ch in path for ch in _CP932_SUSPECT_CHARS)


def has_unresolved_env_token(path):
    """環境変数トークン（$VAR / ${VAR} / %VAR%）が展開されずに残っているか判定する。"""
    if not path:
        return False
    return bool(_ENV_VAR_PATTERN.search(path))


def find_env_tokens(path):
    """パス中の環境変数トークンをすべて列挙する（展開処理向け）。"""
    if not path:
        return []
    return _ENV_VAR_PATTERN.findall(path)


def exceeds_windows_max_path(path):
    """Windows の260文字パス長制限を超えているか判定する。"""
    if not path:
        return False
    return len(path) > WINDOWS_MAX_PATH


def is_relative_path(path):
    """相対パス（'./'、'../'、あるいは先頭がドライブ/UNC/絶対スラッシュでない）かどうか判定する。"""
    if not path:
        return False
    posix = to_posix_slashes(path)
    if posix.startswith("./") or posix.startswith("../") or posix in (".", ".."):
        return True
    if is_unc_path(posix):
        return False
    if re.match(r"^[A-Za-z]:/", posix):
        return False
    if posix.startswith("/"):
        return False
    # 上記いずれにも該当しない裸の文字列（例: "foo/bar.png"）も相対パス扱いとする
    return True


def resolve_relative_path(path, project_root):
    """
    相対パスを解決する。

    設計書 §3.8: 「相対パス（./、../）の解釈基準 → 『現在の Maya Project
    ルート』に固定する」。project_root が None/空の場合は解決できないため
    元の文字列をそのまま返す（呼び出し側で S0 の実在確認が失敗し、後続の
    段へフォールバックする）。
    """
    if not is_relative_path(path) or not project_root:
        return path
    posix_path = to_posix_slashes(path)
    posix_root = to_posix_slashes(project_root)
    joined = posix_root.rstrip("/") + "/" + posix_path
    return normpath_posix(joined)


def normpath_posix(path):
    """
    os.path.normpath 相当だが、常に '/' 区切りで返す（プラットフォーム非依存）。
    Core はテスト実行環境（Linux/Windows いずれの pytest でも）で同じ結果を
    返す必要がある（P2: 決定論性）ため、os.path.normpath に丸投げしない。
    """
    if not path:
        return path
    posix = to_posix_slashes(path)
    is_abs_unc = posix.startswith("//")
    is_abs_drive = bool(re.match(r"^[A-Za-z]:/", posix))
    is_abs_slash = posix.startswith("/") and not is_abs_unc

    if is_abs_unc:
        prefix = "//"
        remainder = posix[2:]
    elif is_abs_drive:
        prefix = posix[:3]
        remainder = posix[3:]
    elif is_abs_slash:
        prefix = "/"
        remainder = posix[1:]
    else:
        prefix = ""
        remainder = posix

    parts = [p for p in remainder.split("/") if p != ""]
    stack = []
    for part in parts:
        if part == ".":
            continue
        elif part == "..":
            if stack and stack[-1] != "..":
                stack.pop()
            elif not prefix:
                # 相対パスの先頭を突き抜ける ".." は保持する
                stack.append(part)
            # 絶対パスの場合、ルートより上への ".." は無視する
        else:
            stack.append(part)

    result = prefix + "/".join(stack)
    if not result:
        result = prefix if prefix else "."
    return result


def normalize_for_index_key(filename):
    """
    インデックスのキー用にファイル名を正規化する（§3.2: 大文字小文字を
    無視した一致は「インデックスのキーを常に小文字化することで構造的に吸収」）。
    """
    if filename is None:
        return filename
    return filename.lower()


def split_dir_and_name(path):
    """パスをディレクトリ部とファイル名部に分割する（'/'区切り前提）。"""
    posix = to_posix_slashes(path) if path else path
    if not posix:
        return "", ""
    idx = posix.rfind("/")
    if idx == -1:
        return "", posix
    return posix[:idx], posix[idx + 1:]


def path_segments(path):
    """パスをディレクトリ区切りでセグメント配列へ分解する（空要素は除く）。"""
    if not path:
        return []
    posix = to_posix_slashes(path)
    return [seg for seg in posix.split("/") if seg != ""]


def full_normalize(path):
    """
    実在確認・比較の前段として使う、パスの基本正規化をまとめて行う。
    （区切り文字統一 → 末尾スラッシュ除去）
    環境変数展開・相対パス解決・大文字小文字は文脈依存のため、ここでは行わない
    （呼び出し側のカスケード各段が明示的に行う）。
    """
    if path is None:
        return path
    posix = to_posix_slashes(path)
    posix = strip_trailing_slash(posix)
    return posix


def levenshtein_distance(a, b):
    """
    2026.07.31新設（ユーザー指摘対応: 「Modo_UV_checke.jpg では
    Modo_UV_checker.jpg の候補が出てこない」）。

    2文字列間のレーベンシュタイン編集距離（挿入・削除・置換の最小回数）を
    computeする、あいまいファイル名マッチング（S4フォールバック）向けの
    純粋関数。空間計算量O(min(len(a), len(b)))の1次元DPで実装する
    （ファイル名程度の長さであれば十分高速）。

    決定論性: 入力が同じであれば常に同じ整数を返す（P2）。
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    # 短い方をbにして、DP配列の長さを最小化する。
    if len(a) < len(b):
        a, b = b, a

    previous_row = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current_row = [i]
        for j, cb in enumerate(b, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            substitute_cost = previous_row[j - 1] + (0 if ca == cb else 1)
            current_row.append(min(insert_cost, delete_cost, substitute_cost))
        previous_row = current_row
    return previous_row[-1]
