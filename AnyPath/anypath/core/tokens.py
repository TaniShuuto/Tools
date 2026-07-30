"""
anypath.core.tokens  -  AnyPath Core: UDIM / シーケンストークンの扱い
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §3.6

対象トークン: <UDIM>、<f>、<u>_<v>、####、<attr>

方針（設計書より）:
    - トークンを正規表現へ変換し、glob 的に実在確認する専用経路を設ける
    - 「1 枚でも実在すれば、そのディレクトリは正しい」と判定する
    - 適用時はトークン形式を必ず保持する。実ファイル名へ置換してはならない
      （UDIM が全て壊れる）
    - udim_setup.py が生成するパス形式を必ずテスト対象に含める

**Maya API を一切 import しない**（§2.1）。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 に基づき新規作成。
"""

__version__ = "1.0.0"

import os
import re

from anypath.core.normalize import to_posix_slashes, split_dir_and_name

# udim_setup.py（本リポジトリの既存ツール）が生成する代表的なトークン形式。
# 参考: udim_setup.py の _extract_tile / <UDIM> パス構築ロジック。
#   <UDIM>   : Mari/Mudbox系UDIM（1001, 1002, ...の4桁）
#   <u>_<v>  : Zbrush等のUV分割形式
#   <f>      : フレーム番号（連番アニメーション）
#   ####     : 連番のプレースホルダ（桁数は可変）
#   <attr>   : Substance Painter のチャンネル名等、属性名プレースホルダ
_TOKEN_PATTERNS = [
    ("<UDIM>", re.compile(re.escape("<UDIM>"))),
    ("<u>_<v>", re.compile(re.escape("<u>_<v>"))),
    ("<f>", re.compile(re.escape("<f>"))),
    ("<attr>", re.compile(re.escape("<attr>"))),
    # #### は連続する # を桁数不問でまとめて1トークンとして扱う
    ("####", re.compile(r"#+")),
]


def has_token(path):
    """パスにUDIM/シーケンストークンが含まれているかを判定する。"""
    if not path:
        return False
    for _name, pattern in _TOKEN_PATTERNS:
        if pattern.search(path):
            return True
    return False


def token_kinds(path):
    """パスに含まれるトークン種別の一覧を返す（デバッグ・ログ向け）。"""
    if not path:
        return []
    found = []
    for name, pattern in _TOKEN_PATTERNS:
        if pattern.search(path):
            found.append(name)
    return found


def _token_to_glob_regex_fragment(name):
    """トークン1種を、ファイル名内で任意文字列にマッチする正規表現断片へ変換する。"""
    if name == "####":
        return r"\d+"
    if name == "<UDIM>":
        return r"\d{4,}"
    if name == "<u>_<v>":
        return r"[^/\\]+"
    if name == "<f>":
        return r"\d+"
    if name == "<attr>":
        return r"[^/\\]+"
    return r"[^/\\]+"


def token_path_to_regex(path):
    """
    トークンを含むパスを、実ファイル名とのマッチング用正規表現へ変換する。

    ディレクトリ部分にトークンが含まれるケース（稀だが想定しておく）にも
    対応するため、パス全体を1つの正規表現として構築する。
    """
    posix = to_posix_slashes(path)
    # まず既知のトークンをプレースホルダで置換し、残りの文字は re.escape する。
    # 複数トークン種が混在する順序を保つため、パターンを順に走査して
    # 位置ベースで置換を組み立てる。
    combined_pattern = re.compile(
        "|".join(p.pattern for _n, p in _TOKEN_PATTERNS)
    )

    pieces = []
    last_end = 0
    for m in combined_pattern.finditer(posix):
        pieces.append(re.escape(posix[last_end:m.start()]))
        matched_text = m.group(0)
        # マッチしたテキストがどのトークン種かを判定
        if re.match(r"^#+$", matched_text):
            frag = _token_to_glob_regex_fragment("####")
        else:
            frag = _token_to_glob_regex_fragment(matched_text)
        pieces.append(frag)
        last_end = m.end()
    pieces.append(re.escape(posix[last_end:]))

    return "^" + "".join(pieces) + "$"


def find_any_matching_file(path, exists_fn=None, listdir_fn=None):
    """
    トークンを含むパスについて、「1枚でも実在すれば正しい」を判定する。

    ディレクトリ部分にトークンが含まれない前提（実運用の大多数のケース）で、
    ディレクトリの存在確認 + ファイル名部分の正規表現マッチによる走査を行う。
    ディレクトリ自体にトークンが含まれる場合は判定不能として False を返す
    （そのようなケースは事実上存在しないため、安全側に倒す）。

    exists_fn / listdir_fn はテスト用の差し替えフック（既定は os.path.isdir
    / os.listdir）。Core は原則ファイルシステムへ直接触れないが、UDIM判定は
    実在確認そのものが目的のため、この関数のみ明示的にI/Oを行う
    （呼び出し元のcascade.pyがこれをS0/S1/S2/S3各段の実在確認と同列に扱う）。

    戻り値: (matched: bool, sample_path: str or None)
    """
    if exists_fn is None:
        exists_fn = os.path.isdir
    if listdir_fn is None:
        listdir_fn = os.listdir

    dir_part, name_part = split_dir_and_name(path)
    if has_token(dir_part):
        # ディレクトリ側にトークンが含まれる特殊ケースは非対応
        return False, None
    if not dir_part or not exists_fn(dir_part):
        return False, None

    name_regex = re.compile(token_path_to_regex(name_part))
    try:
        entries = sorted(listdir_fn(dir_part))
    except OSError:
        return False, None

    for entry in entries:
        if name_regex.match(entry):
            return True, dir_part + "/" + entry
    return False, None
