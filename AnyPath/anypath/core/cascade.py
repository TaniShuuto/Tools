"""
anypath.core.cascade  -  AnyPath Core: 解決カスケード本体（S0〜S5）＋2フェーズ解決
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §3.2〜§3.4

解決カスケード（6段。入力パスに対し上から順に試行し、最初に成功した段で確定）:
    S0  そのまま実在確認                          EXACT
    S1  マッピング表による前方一致置換              EXACT（置換後に実在確認必須）
    S2  環境変数・トークンの展開                    EXACT（展開後に実在確認必須）
    S3  アンカーフォルダ相対の再構築                HIGH
    S4  ファイル名インデックス検索＋構造照合         HIGH または REVIEW
    S5  解決不能                                  FAILED

決定論性の要件（P2・§3.4、実装必須）:
    1. Bridge の収集層は、ノードのフルパス名で昇順ソートしてから Core へ渡す
       （＝この関数の呼び出し側の責務。resolve_all() はソート済み入力を前提とする
        が、念のため入力を明示的にソートしてから処理する）
    2. インデックスの候補リストは、常に絶対パスの昇順でソートして保持する
       （anypath.core.index.FileIndex.finalize() が担当）
    3. 結果に影響する箇所で set および順序未保証の辞書反復を使用してはならない
    4. スコア同点時は、絶対パスの辞書順で先頭を推奨候補とする
       （anypath.core.scoring.rank_candidates が担当）
    5. 解決は2フェーズで行う
       - 第1フェーズ：全パスに S0〜S3 を適用し EXACT/HIGH を確定する
       - 第2フェーズ：第1フェーズの結果を「解決済みディレクトリ集合」として
         固定し、それを用いて残りを S4 で判定する
    6. マシンの学習履歴をスコアリングに使用してはならない
       （このモジュールはそもそも学習履歴を入力に取らない設計とすることで
        構造的に担保する）
    7. ディスク上の永続インデックスキャッシュは採用しない
       （呼び出しごとに index.build_index() を呼ぶ運用とし、このモジュール
        自身はインデックスをディスクへ書き出す処理を持たない）

**Maya API を一切 import しない**（§2.1）。

S4の3段フォールバック（2026.07.31新設。上から順に試し、最初に候補が
見つかった段で確定する）:
    1. index.lookup()       拡張子完全一致
    2. index.lookup_by_stem()  拡張子違い（stem完全一致）。
       allow_high=False（誤リンクゼロのため常にREVIEW止まり）
    3. index.lookup_fuzzy()    あいまい一致（編集距離ベース。タイプミス・
       切り詰め・文字違い等）。allow_high=False（同上）、複数候補を
       全て提示する（利用者に選ばせる。ResolveResult.detailに
       fuzzy_match=Trueを付与）

変更履歴:
    1.2.0 (2026.07.31):
        - ユーザー指摘対応（「Modo_UV_checke.jpg では
          Modo_UV_checker.jpg の候補が出てこない」）: S4の3段目の
          フォールバックとして、拡張子違いでも0件だった場合のみ
          あいまいファイル名マッチング（index.lookup_fuzzy）を試す
          経路を追加した。あいまい一致の候補は judge_s4
          (allow_high=False) により常にREVIEWへ落ち、
          ResolveResult.detail に fuzzy_match=True が付与される。
        - 後方互換性: MINOR。拡張子完全一致・拡張子違いのいずれかで
          候補が見つかる既存のケースの挙動・結果は一切変わらない
          （あいまいマッチは前の2段が両方とも0件の場合のみ発動）。
    1.1.0 (2026.07.31):
        - ユーザー指摘対応（「ファイル名＋拡張子が完全に同じでなければ
          候補が出てこない」）: S4のファイル名検索に、拡張子完全一致が
          0件の場合のみ拡張子違いへフォールバックする経路を追加した。
          拡張子違いの候補は judge_s4(allow_high=False) により常に
          REVIEWへ落ちる（自動適用はしない）。ResolveResult.detail に
          extension_mismatch=True を付与し、呼び出し元（Shell層）が
          必要に応じて表示を変えられるようにした。
        - 後方互換性: MINOR。拡張子完全一致がヒットする既存のケースの
          挙動・結果は一切変わらない。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §3.2〜§3.4 に基づき新規作成。
"""

__version__ = "1.2.0"

import os
import re

from anypath.core import tokens as tokens_mod
from anypath.core.index import build_index
from anypath.core.normalize import (
    exceeds_windows_max_path,
    find_env_tokens,
    full_normalize,
    has_unresolved_env_token,
    is_relative_path,
    resolve_relative_path,
    split_dir_and_name,
    to_posix_slashes,
)
from anypath.core.scoring import judge_s4
from anypath.core.types import Confidence, ResolveResult, Strategy


def _exists(path, exists_fn):
    if not path:
        return False
    if tokens_mod.has_token(path):
        matched, _sample = tokens_mod.find_any_matching_file(path)
        return matched
    return exists_fn(path)


def _apply_path_mappings(path, path_mappings):
    """
    S1: マッピング表による前方一致置換。設定の path_mappings は
    [{from, to}, ...] のリストで、リスト順に最初に一致したものを適用する
    （順序依存の挙動をリスト順序で決定論的に定める）。
    """
    posix = to_posix_slashes(path)
    for mapping in path_mappings:
        frm = to_posix_slashes(mapping.get("from", ""))
        to = to_posix_slashes(mapping.get("to", ""))
        if not frm:
            continue
        if posix.startswith(frm):
            return to + posix[len(frm):]
    return None


def _expand_env_tokens(path, environ):
    """
    S2: 環境変数・トークンの展開。$VAR / ${VAR} / %VAR% 形式に対応する。
    未定義変数が残留した場合は展開せず None を返す（次段へフォールバック）。
    """
    tokens_found = find_env_tokens(path)
    if not tokens_found:
        return None

    def _replace(match):
        text = match.group(0)
        if text.startswith("${") and text.endswith("}"):
            name = text[2:-1]
        elif text.startswith("$"):
            name = text[1:]
        elif text.startswith("%") and text.endswith("%"):
            name = text[1:-1]
        else:
            name = text
        return environ.get(name, text)

    pattern = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%")
    expanded = pattern.sub(_replace, path)

    if has_unresolved_env_token(expanded):
        return None
    return expanded


def _rebase_on_anchor(path, anchor_folders, project_root):
    """
    S3: アンカーフォルダ相対の再構築。パス内の最後方のアンカー以降を保持し、
    現プロジェクトルートへ接ぎ木する（§3.3: 最後方一致ルール）。
    project_root が無ければ再構築不能。
    """
    if not project_root:
        return None
    posix = to_posix_slashes(path)
    segments = [s for s in posix.split("/") if s != ""]
    anchor_lower = set(a.lower() for a in anchor_folders)

    anchor_idx = -1
    for idx in range(len(segments) - 1, -1, -1):
        if segments[idx].lower() in anchor_lower:
            anchor_idx = idx
            break
    if anchor_idx == -1:
        return None

    tail = segments[anchor_idx:]
    root_posix = to_posix_slashes(project_root).rstrip("/")
    rebuilt = root_posix + "/" + "/".join(tail)
    return rebuilt


class ResolveContext:
    """
    resolve_all() 呼び出し1回分の共有コンテキスト。
    設定・探索ルート・プロジェクトルート・実在確認/環境変数などのI/Oフックを
    まとめて保持する。テスト時にフックを差し替えることで、実ファイルシステムに
    触れずにCoreを検証できる（§9.1: 「Maya不要」「tmp_pathに仮想ファイルツリー」）。
    """

    def __init__(self, config, project_root=None, environ=None,
                 exists_fn=None, index_builder=None):
        self.config = config
        self.project_root = project_root
        self.environ = environ if environ is not None else dict(os.environ)
        self.exists_fn = exists_fn if exists_fn is not None else os.path.isfile
        self.index_builder = index_builder if index_builder is not None else build_index
        self._index = None
        self.index_built = False

    def get_index(self):
        """S4で初めて必要になった時点での遅延構築（§3.7）。"""
        if not self.index_built:
            self._index = self.index_builder(
                self.config.get("search_roots", []),
                exclude_patterns=self.config.get("exclude_patterns", []),
                timeout_sec=self.config.get("index_timeout_sec", 15),
            )
            self.index_built = True
        return self._index


def _try_phase1(raw_path, ctx):
    """
    S0〜S3 を順に試行する（第1フェーズ）。
    戻り値: ResolveResultのうち node_key を除いた要素のタプル、または None
            （S3までで解決できなかった場合。呼び出し側がS4/S5へ進める）。
    実際には (confidence, strategy, resolved_path) のタプルを返す。
    """
    normalized = full_normalize(raw_path)

    if not normalized:
        return None

    # S0: そのまま実在確認
    if _exists(normalized, ctx.exists_fn):
        return Confidence.EXACT, Strategy.S0_AS_IS, normalized

    # S1: マッピング表による前方一致置換
    mapped = _apply_path_mappings(normalized, ctx.config.get("path_mappings", []))
    if mapped is not None:
        mapped_norm = full_normalize(mapped)
        if _exists(mapped_norm, ctx.exists_fn):
            return Confidence.EXACT, Strategy.S1_PATH_MAPPING, mapped_norm

    # S2: 環境変数・トークンの展開
    expanded = _expand_env_tokens(normalized, ctx.environ)
    if expanded is not None:
        expanded_norm = full_normalize(expanded)
        if _exists(expanded_norm, ctx.exists_fn):
            return Confidence.EXACT, Strategy.S2_ENV_EXPANSION, expanded_norm

    # 相対パスの場合はプロジェクトルート基準で解決してからS0相当の実在確認を行う。
    if is_relative_path(normalized) and ctx.project_root:
        rebased_relative = resolve_relative_path(normalized, ctx.project_root)
        if _exists(rebased_relative, ctx.exists_fn):
            return Confidence.EXACT, Strategy.S0_AS_IS, rebased_relative

    # S3: アンカーフォルダ相対の再構築
    rebased = _rebase_on_anchor(
        normalized, ctx.config.get("anchor_folders", []), ctx.project_root)
    if rebased is not None:
        rebased_norm = full_normalize(rebased)
        if _exists(rebased_norm, ctx.exists_fn):
            return Confidence.HIGH, Strategy.S3_ANCHOR_REBASE, rebased_norm

    return None


def resolve_all(resolve_inputs, config, project_root=None, environ=None,
                 exists_fn=None, index_builder=None):
    """
    ResolveInput のリストを2フェーズで解決する（§3.4-5）。

    決定論性の要件1「Bridge の収集層は、ノードのフルパス名で昇順ソートして
    から Core へ渡す」はBridge層の責務だが、Core自身も自衛的に node_key で
    安定ソートしてから処理する（呼び出し側の実装ミスでも決定論性が崩れない
    ようにするための二重の保険）。

    戻り値: node_key -> ResolveResult の辞書ではなく、入力順（ソート後）を
    保持した ResolveResult のリストを返す（辞書のキー順に処理を依存させない
    ため、あえてリストで返す設計とする）。
    """
    ctx = ResolveContext(
        config=config, project_root=project_root, environ=environ,
        exists_fn=exists_fn, index_builder=index_builder)

    # 決定論性要件1: node_key の昇順で安定ソート
    sorted_inputs = sorted(resolve_inputs, key=lambda r: (r.node_key, r.raw_path))

    # --- 第1フェーズ: S0〜S3 ---
    phase1_results = {}
    resolved_dirs = []  # 決定論性: setではなくlist->最終的にソート済みで固定
    resolved_dirs_seen = {}

    for item in sorted_inputs:
        outcome = _try_phase1(item.raw_path, ctx)
        if outcome is not None:
            confidence, strategy, resolved_path = outcome
            phase1_results[item.node_key] = ResolveResult(
                node_key=item.node_key,
                raw_path=item.raw_path,
                confidence=confidence,
                strategy=strategy,
                resolved_path=resolved_path,
            )
            resolved_dir, _name = split_dir_and_name(resolved_path)
            if resolved_dir and resolved_dir not in resolved_dirs_seen:
                resolved_dirs_seen[resolved_dir] = True
                resolved_dirs.append(resolved_dir)

    # 解決済みディレクトリ集合を固定（決定論性要件3: setを使わず、ソート済み
    # リストから frozenset を1回だけ構築して以後は読み取り専用で使う）。
    resolved_dir_set = frozenset(sorted(resolved_dirs))

    # --- 第2フェーズ: 残りをS4で判定 ---
    results = []
    index_is_complete_flag = {"value": True}
    index_touched = {"value": False}

    for item in sorted_inputs:
        if item.node_key in phase1_results:
            results.append(phase1_results[item.node_key])
            continue

        normalized = full_normalize(item.raw_path)
        if not normalized:
            results.append(ResolveResult(
                node_key=item.node_key, raw_path=item.raw_path,
                confidence=Confidence.FAILED, strategy=Strategy.S5_UNRESOLVED,
                detail={"reason": "empty_path"},
            ))
            continue

        _dir_part, name_part = split_dir_and_name(normalized)
        if not name_part or tokens_mod.has_token(normalized):
            # トークンを含むパスはS4のファイル名インデックス照合の対象外
            # （UDIM等はS0〜S3のglob実在確認で解決済みのはずであり、そこまでで
            #  解決できなければS4では救えない。誤ってトークン文字列のまま
            #  ファイル名検索にかけると絶対にヒットしないため、明示的にFAILEDへ）。
            results.append(ResolveResult(
                node_key=item.node_key, raw_path=item.raw_path,
                confidence=Confidence.FAILED, strategy=Strategy.S5_UNRESOLVED,
                detail={"reason": "token_path_unresolved"},
            ))
            continue

        index = ctx.get_index()
        index_touched["value"] = True
        if not index.is_complete:
            index_is_complete_flag["value"] = False

        # 拡張子完全一致をまず試す（従来通り）。
        candidates = index.lookup(name_part)
        allow_high = True
        extension_mismatch = False
        fuzzy_match = False
        fuzzy_distances = None

        if not candidates:
            # 2026.07.31新設: 拡張子完全一致で0件だった場合のみ、
            # 拡張子を除いたファイル名（stem）でフォールバック検索する
            # （ユーザー指摘: 「ファイル名＋拡張子が完全に同じでなければ
            # 認識せず候補も出てこない」への対応）。拡張子が違う候補は
            # 中身の同一性を保証できないため、judge_s4へ allow_high=False
            # を渡し、構造スコアがどれだけ高くても自動適用(HIGH)へは
            # 絶対に昇格させない（P1: 誤リンクゼロ）。
            stem_candidates = index.lookup_by_stem(name_part)
            if stem_candidates:
                candidates = stem_candidates
                allow_high = False
                extension_mismatch = True

        if not candidates:
            # 2026.07.31新設（第3フォールバック）: 拡張子違いでも0件
            # だった場合のみ、あいまいファイル名マッチング（タイプミス・
            # 切り詰め・文字違い等）へフォールバックする（ユーザー指摘:
            # 「Modo_UV_checke.jpg では Modo_UV_checker.jpg の候補が
            # 出てこない」）。あいまい一致は誤検出のリスクが最も高い
            # ため、常にallow_high=False（自動適用は絶対にしない）で
            # 扱い、複数候補があれば全て要確認パネルへ提示して利用者に
            # 判断してもらう（ユーザー指示: 「候補と適応ボタンと除外
            # ボタンを設置して選べるようにしたい」）。
            fuzzy_results = index.lookup_fuzzy(name_part)
            if fuzzy_results:
                candidates = [path for path, _distance in fuzzy_results]
                fuzzy_distances = {path: distance for path, distance in fuzzy_results}
                allow_high = False
                fuzzy_match = True

        confidence, resolved_path, ranked = judge_s4(
            raw_path=normalized,
            candidate_paths=candidates,
            resolved_dirs=resolved_dir_set,
            anchor_folders=config.get("anchor_folders", []),
            ambiguity_margin=config.get("ambiguity_margin", 0.4),
            index_is_complete=index.is_complete,
            allow_high=allow_high,
            fuzzy_distances=fuzzy_distances,
        )

        detail = {"index_complete": index.is_complete}
        if extension_mismatch:
            detail["extension_mismatch"] = True
        if fuzzy_match:
            detail["fuzzy_match"] = True

        results.append(ResolveResult(
            node_key=item.node_key,
            raw_path=item.raw_path,
            confidence=confidence,
            strategy=Strategy.S4_INDEX_MATCH if candidates else Strategy.S5_UNRESOLVED,
            resolved_path=resolved_path,
            candidates=ranked,
            detail=detail,
        ))

    return results
