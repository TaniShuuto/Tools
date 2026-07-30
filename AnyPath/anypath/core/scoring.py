"""
anypath.core.scoring  -  AnyPath Core: S4 構造照合スコアリング
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §3.5（S4 の判定 ―― 誤リンクを生まない唯一の関門）

S4 の判定手順:
    1. ファイル名（小文字化）でインデックスを引き、候補リストを得る
    2. 候補が 0 件 → FAILED
    3. 各候補について「構造スコア」を算出する
    4. 候補が 1 件の場合
         構造スコアが閾値以上 → HIGH
         閾値未満             → REVIEW
    5. 候補が 2 件以上の場合
         最上位と次位のスコア差が ambiguity_margin 以上
           かつ 最上位が閾値以上 → HIGH
         それ以外               → REVIEW（スコア降順で全候補を提示）

重要（v1 からの変更）: 「候補 1 件なら無条件で HIGH」は行わない。単一ヒットも
必ず構造照合を通す（§12 実装担当への申し送り 1.）。

構造スコアの構成要素（3つのみ。すべて決定論的）:
    - 同居性: 第1フェーズで確定した「解決済みディレクトリ集合」に候補が
      含まれるか。最も有効な手掛かり（重み: 高）
    - 親ディレクトリ一致長: 元パスの末尾ディレクトリ列と候補の一致段数
      （重み: 中）
    - アンカー整合: 元パスと候補が同じアンカーフォルダ配下にあるか（重み: 中）

除外した要素: ファイルサイズ・更新日時の近似はスコア要素に用いない
（テクスチャは再出力で日時が変わり、圧縮設定でサイズが変わるため信頼できず、
誤った自信を与える要素はP1に反する）。

**Maya API を一切 import しない**（§2.1）。

変更履歴:
    1.2.0 (2026.07.31):
        - ユーザー指摘対応（「Modo_UV_checke.jpg では
          Modo_UV_checker.jpg の候補が出てこない」）: rank_candidates()
          / judge_s4() に fuzzy_distances 引数を追加した。あいまい
          マッチング（index.lookup_fuzzy()経由）で見つかった候補の
          編集距離を Candidate.score_detail["edit_distance"] に記録し、
          UI層が「なぜこの候補が出たか」を示せるようにした。
        - 後方互換性: MINOR。fuzzy_distancesはキーワード引数（既定None）
          として追加したため、既存呼び出し元の挙動は変わらない。
    1.1.0 (2026.07.31):
        - ユーザー指摘対応（拡張子完全一致の件）: judge_s4() に
          allow_high 引数を追加した。拡張子違いの候補（例:
          wall.png の壊れた参照に対して wall.exr がインデックス上に
          見つかった場合）は、ファイルの中身が同一である保証が
          拡張子違いという事実だけでは一切得られないため、P1
          （誤リンクゼロ）を守るために自動適用(HIGH)を常に禁止し、
          候補があれば必ずREVIEWへ落とす。呼び出し元（cascade.py）が
          拡張子完全一致の検索結果を渡す場合は allow_high=True
          （既定値。従来通りの挙動）、拡張子違いの候補を渡す場合は
          allow_high=False を指定する。
        - 後方互換性: MINOR。allow_high はキーワード引数（既定True）
          として追加したため、既存呼び出し元の挙動は変わらない。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §3.5 に基づき新規作成。
"""

__version__ = "1.2.0"

from anypath.core.normalize import path_segments, split_dir_and_name
from anypath.core.types import Candidate, Confidence

# 構造スコアの重み。仕様上「高/中/中」としか定義されていないため、
# 同居性を最も重く、残り2要素を均等に重み付けする実装判断とする
# （この定数を変更する場合は §3.5 の意図: 「同居性が最も有効な手掛かり」を
# 損なわないこと）。
WEIGHT_COOCCURRENCE = 0.5
WEIGHT_PARENT_MATCH = 0.25
WEIGHT_ANCHOR_ALIGNMENT = 0.25

# HIGH判定の構造スコア閾値。ambiguity_marginとは独立した「絶対的な最低ライン」
# として、これ未満なら候補が1件でもREVIEWへ落とす。
HIGH_SCORE_THRESHOLD = 0.5


def _parent_match_length(raw_path, candidate_path):
    """元パスの末尾ディレクトリ列と候補の一致段数を、末尾から比較して数える。"""
    raw_dir, _ = split_dir_and_name(raw_path)
    cand_dir, _ = split_dir_and_name(candidate_path)
    raw_segs = path_segments(raw_dir)
    cand_segs = path_segments(cand_dir)
    count = 0
    i = len(raw_segs) - 1
    j = len(cand_segs) - 1
    while i >= 0 and j >= 0 and raw_segs[i].lower() == cand_segs[j].lower():
        count += 1
        i -= 1
        j -= 1
    return count


def _find_anchor_index(segments, anchor_folders_lower):
    """
    セグメント列の中から、アンカーフォルダ名に一致する最後方の位置を返す
    （§3.3: 「既定ルールを『最後方一致』と定める」）。見つからなければ-1。
    """
    for idx in range(len(segments) - 1, -1, -1):
        if segments[idx].lower() in anchor_folders_lower:
            return idx
    return -1


def _anchor_alignment(raw_path, candidate_path, anchor_folders):
    """元パスと候補が同じアンカーフォルダ配下にあるかを判定する。"""
    anchor_lower = set(a.lower() for a in anchor_folders)
    raw_segs = path_segments(raw_path)
    cand_segs = path_segments(candidate_path)
    raw_anchor_idx = _find_anchor_index(raw_segs, anchor_lower)
    cand_anchor_idx = _find_anchor_index(cand_segs, anchor_lower)
    if raw_anchor_idx == -1 or cand_anchor_idx == -1:
        return False
    raw_anchor_name = raw_segs[raw_anchor_idx].lower()
    cand_anchor_name = cand_segs[cand_anchor_idx].lower()
    return raw_anchor_name == cand_anchor_name


def compute_structural_score(raw_path, candidate_path, resolved_dirs, anchor_folders):
    """
    1候補の構造スコアを算出する（0.0〜1.0）。

    resolved_dirs: 第1フェーズで確定した「解決済みディレクトリ集合」
                   （§3.4-5 の2フェーズ解決）。frozenset[str] または set[str]
                   （中身はディレクトリの絶対パス、比較は正規化済み前提）。
    """
    cand_dir, _ = split_dir_and_name(candidate_path)

    cooccurrence = 1.0 if cand_dir in resolved_dirs else 0.0

    raw_segs = path_segments(split_dir_and_name(raw_path)[0])
    max_possible_parent_match = max(len(raw_segs), 1)
    parent_len = _parent_match_length(raw_path, candidate_path)
    parent_score = min(parent_len / max_possible_parent_match, 1.0)

    anchor_score = 1.0 if _anchor_alignment(raw_path, candidate_path, anchor_folders) else 0.0

    total = (
        WEIGHT_COOCCURRENCE * cooccurrence
        + WEIGHT_PARENT_MATCH * parent_score
        + WEIGHT_ANCHOR_ALIGNMENT * anchor_score
    )

    detail = {
        "cooccurrence": cooccurrence,
        "parent_match_length": parent_len,
        "parent_score": parent_score,
        "anchor_alignment": anchor_score,
    }
    return total, detail


def rank_candidates(raw_path, candidate_paths, resolved_dirs, anchor_folders,
                     fuzzy_distances=None):
    """
    候補パス一覧に構造スコアを付与し、スコア降順・同点は絶対パス辞書順で
    ソートして Candidate のリストを返す（§3.4-4 タイブレークルール）。

    fuzzy_distances (2026.07.31新設): {abs_path: edit_distance} の辞書
    （省略可）。あいまいファイル名マッチング（index.lookup_fuzzy()経由）で
    見つかった候補について、編集距離を score_detail["edit_distance"] へ
    記録する。UI層（review_panel.py）が「なぜこの候補が出たか」を
    示すために使う想定。通常の完全一致・拡張子違い候補には距離情報を
    付けない（キーが存在しない場合は score_detail に含めない）。
    """
    fuzzy_distances = fuzzy_distances or {}
    scored = []
    for path in candidate_paths:
        score, detail = compute_structural_score(raw_path, path, resolved_dirs, anchor_folders)
        if path in fuzzy_distances:
            detail = dict(detail)
            detail["edit_distance"] = fuzzy_distances[path]
        scored.append(Candidate(abs_path=path, score=score, score_detail=detail))

    # ソートキー: スコア降順、同点は絶対パスの辞書順昇順が先頭に来るように。
    scored.sort(key=lambda c: (-c.score, c.abs_path))
    return scored


def judge_s4(raw_path, candidate_paths, resolved_dirs, anchor_folders,
              ambiguity_margin, index_is_complete, allow_high=True,
              fuzzy_distances=None):
    """
    S4 の判定手順本体。

    戻り値: (Confidence, resolved_path_or_None, ranked_candidates)

    index_is_complete が False（インデックスがタイムアウトで不完全）の場合、
    HIGHへの昇格を一切禁止し、候補があれば必ずREVIEWへ落とす（§3.7）。

    allow_high (2026.07.31新設): False を指定すると、構造スコア・
    ambiguity_marginの条件を満たしていてもHIGHへは絶対に昇格させず、
    候補があれば必ずREVIEWへ落とす。拡張子違いの候補（cascade.py が
    lookup_by_stem() 経由で見つけたもの）や、あいまいマッチの候補
    （lookup_fuzzy() 経由で見つけたもの）を渡す場合に False を指定する
    想定（P1: 中身が同一である保証がないため）。

    fuzzy_distances (2026.07.31新設): rank_candidates() へそのまま
    伝播する（省略可）。
    """
    if not candidate_paths:
        return Confidence.FAILED, None, []

    ranked = rank_candidates(
        raw_path, candidate_paths, resolved_dirs, anchor_folders,
        fuzzy_distances=fuzzy_distances)
    can_be_high = allow_high and index_is_complete

    if len(ranked) == 1:
        top = ranked[0]
        if can_be_high and top.score >= HIGH_SCORE_THRESHOLD:
            return Confidence.HIGH, top.abs_path, ranked
        return Confidence.REVIEW, None, ranked

    top = ranked[0]
    second = ranked[1]
    margin = top.score - second.score
    if can_be_high and margin >= ambiguity_margin and top.score >= HIGH_SCORE_THRESHOLD:
        return Confidence.HIGH, top.abs_path, ranked
    return Confidence.REVIEW, None, ranked
