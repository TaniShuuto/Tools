"""
anypath.core.index  -  AnyPath Core: ファイル名インデックスの構築
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §3.7（インデックス構築とパフォーマンス）

要件:
    - 探索ルート配下を再帰走査し、{ファイル名（小文字）: [絶対パス（昇順）, ...]}
      を構築する
    - 正常なシーン（全パスが S0 で解決）では、インデックスを構築してはならない。
      初めて S4 が必要になった時点で遅延構築する（受け入れ基準§9.3の1秒未満と
      タイムアウト上限15秒を両立させる唯一の方法。要件として明記）
    - 必ずタイムアウトを設ける（既定15秒）
    - タイムアウト時の挙動: 走査済みの部分インデックスで続行し、未走査領域に
      起因する未解決はFAILED側に倒す。インデックスが不完全なセッションでは
      S4のHIGH昇格を禁止し、すべてREVIEWへ落とす（不完全なインデックスで
      HIGHを出すことはP1に反する）。不完全であることをログ・状態表示・
      自己診断画面に必ず明示する
    - 除外パターンを設定可能にする
      （既定: .git, incrementalSave, __pycache__, autosave, .mayaSwatches）
    - 走査時に訪問済みの実パスを記録し、シンボリックリンク／ジャンクションの
      循環を防ぐ

決定論性（§3.4）:
    - インデックスの候補リストは、常に絶対パスの昇順でソートして保持する
      （ファイルシステムの列挙順に依存しない）
    - ディスク上の永続インデックスキャッシュは採用しない

**Maya API を一切 import しない**（§2.1）。ファイルシステム走査（os.walk等）は
Core の責務内（「対象はローカル/共有ファイルシステム」という前提はBridge層でも
Core層でも変わらない汎用I/O）として直接実装する。

変更履歴:
    1.2.0 (2026.07.31):
        - ユーザー指摘対応（「Modo_UV_checke.jpg では
          Modo_UV_checker.jpg の候補が出てこない」）: タイプミス・
          切り詰め・文字違いなど、ファイル名自体が完全一致しない
          （拡張子違いフォールバックでも救えない）ケースに対応する
          あいまい検索 lookup_fuzzy() を新設した。stem（拡張子除く
          ファイル名）同士のレーベンシュタイン編集距離が
          FUZZY_MAX_DISTANCE（既定2）以内のキーに属する候補を、
          距離昇順・同点は絶対パス辞書順で最大FUZZY_MAX_RESULTS
          （既定5）件まで返す。呼び出し側（cascade.py）は必ず
          allow_high=Falseで扱うこと（あいまい一致は自動適用の
          対象にしない。P1）。
        - 後方互換性: MINOR。既存メソッドのシグネチャ・挙動は不変。
    1.1.0 (2026.07.31):
        - ユーザー指摘対応（「ファイル名＋拡張子が完全に同じでなければ
          認識せず候補が出てこないのは正常か」）: 意図した仕様（誤リンク
          ゼロのための保守的な設計）だったが、Substance Painter側で
          書き出し形式を変更した場合（例: wall.png -> wall.exr）に
          全く候補が出ないのは実用上不便との判断で、拡張子違いも
          「候補」としては拾えるようにする。
          FileIndex に拡張子を除いたファイル名（stem）でも引ける
          セカンダリマップ _stem_map を追加し、lookup_by_stem() を
          新設した。既存の lookup()（拡張子完全一致）の動作・戻り値は
          一切変更していない。拡張子違いの候補をどう扱うか（自動適用の
          可否等）はCore層の判定ロジック（scoring.py/cascade.py）側の
          責務とする。
        - 後方互換性: MINOR。既存メソッドのシグネチャ・挙動は不変。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §3.7 に基づき新規作成。
"""

__version__ = "1.2.0"

import fnmatch
import os
import time

from anypath.core.normalize import (
    levenshtein_distance,
    normalize_for_index_key,
    to_posix_slashes,
)

DEFAULT_TIMEOUT_SEC = 15

# あいまい検索（2026.07.31新設）の既定パラメータ。
# max_distance=2: 「Modo_UV_checke」->「Modo_UV_checker」（1文字欠落）の
# ような典型的なタイプミス・切り詰めを確実に拾いつつ、無関係なファイル名
# まで拾いすぎないバランスとして採用（1文字挿入/削除/置換の2回分まで許容）。
FUZZY_MAX_DISTANCE = 2
# 一度に提示する候補数の上限。要確認パネルで縦に並べて表示する前提のため、
# 際限なく増えると選ぶこと自体が負担になる（見せすぎない設計判断）。
FUZZY_MAX_RESULTS = 5


def _stem_key(filename):
    """
    ファイル名から拡張子を除いた部分を、インデックスキー用に正規化して返す。
    拡張子が無いファイル名はそのまま（小文字化のみ）を返す。
    """
    normalized = normalize_for_index_key(filename)
    dot_idx = normalized.rfind(".")
    if dot_idx <= 0:  # 先頭が"."のドットファイル（隠しファイル）は拡張子扱いしない
        return normalized
    return normalized[:dot_idx]


class FileIndex:
    """
    ファイル名 → 絶対パス一覧（昇順ソート済み） のインデックス。

    is_complete が False の場合、タイムアウトにより走査が途中で打ち切られた
    ことを示す。呼び出し側（cascade.py の S4 判定）はこのフラグを見て、
    HIGH への昇格を禁止しなければならない（§3.7）。
    """

    def __init__(self):
        self._map = {}
        self._stem_map = {}
        self.is_complete = True
        self.scanned_roots = []
        self.elapsed_sec = 0.0
        self.file_count = 0
        self.timed_out = False

    def add(self, filename, abs_path):
        key = normalize_for_index_key(filename)
        self._map.setdefault(key, []).append(abs_path)
        stem = _stem_key(filename)
        self._stem_map.setdefault(stem, []).append(abs_path)

    def finalize(self):
        """全走査完了後、各キーの候補リストを絶対パス昇順へソートする（決定論性）。"""
        for key in self._map:
            self._map[key] = sorted(self._map[key])
        for key in self._stem_map:
            self._stem_map[key] = sorted(self._stem_map[key])

    def lookup(self, filename):
        """ファイル名（拡張子込み）の完全一致で候補を検索する。"""
        key = normalize_for_index_key(filename)
        return list(self._map.get(key, []))

    def lookup_by_stem(self, filename):
        """
        拡張子を除いたファイル名（stem）で候補を検索する
        （2026.07.31新設）。lookup() で見つからない場合の
        フォールバック用途を想定。拡張子が完全一致する候補も
        stemが同じであれば含まれるため、呼び出し側で
        「拡張子が一致する候補」と「拡張子違いの候補」を
        区別したい場合は、name_part の拡張子と各候補パスの拡張子を
        比較すること（judge_s4がこの区別を行う）。
        """
        key = _stem_key(filename)
        return list(self._stem_map.get(key, []))

    def lookup_fuzzy(self, filename, max_distance=FUZZY_MAX_DISTANCE, max_results=FUZZY_MAX_RESULTS):
        """
        ファイル名のタイプミス・切り詰め・文字違い等を許容するあいまい検索
        （2026.07.31新設。ユーザー指摘対応: 「Modo_UV_checke.jpg では
        Modo_UV_checker.jpg の候補が出てこない」）。

        拡張子を除いたファイル名（stem）同士のレーベンシュタイン編集距離を
        インデックス上の全stemキーに対して計算し、max_distance以内の
        キーに属する候補パスをすべて集める。呼び出し側（judge_s4/
        cascade.py）はこの候補を必ずREVIEW止まりで扱うこと（構造スコアが
        どれだけ高くても自動適用は禁止する。P1: あいまい一致した時点で
        「中身が同一のファイルである」保証は一切無い）。

        戻り値: [(abs_path, distance), ...]
            distance昇順、同点は絶対パス辞書順（決定論性）。
            max_results件を超えた分は切り捨てる。

        パフォーマンス配慮: ファイル名長の差がmax_distanceを超える
        キーは編集距離計算そのものをスキップする（編集距離は文字列長差
        以上には縮まらない、という下限の性質を利用した早期枝刈り）。
        """
        query_stem = _stem_key(filename)
        if not query_stem:
            return []
        query_len = len(query_stem)

        scored = []
        for key, paths in self._stem_map.items():
            if key == query_stem:
                continue  # 完全一致は lookup_by_stem() の担当であり、ここでは対象外
            if abs(len(key) - query_len) > max_distance:
                continue  # 長さ差だけで足切りできる（編集距離の下限）
            distance = levenshtein_distance(query_stem, key)
            if distance <= max_distance:
                for path in paths:
                    scored.append((path, distance))

        scored.sort(key=lambda item: (item[1], item[0]))
        return scored[:max_results]

    def __len__(self):
        return self.file_count


def _matches_any_exclude(name, exclude_patterns):
    for pattern in exclude_patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def build_index(search_roots, exclude_patterns=None, timeout_sec=DEFAULT_TIMEOUT_SEC,
                 clock=time.monotonic, listdir_fn=None, isdir_fn=None, islink_fn=None,
                 realpath_fn=None):
    """
    探索ルート配下を再帰走査してFileIndexを構築する。

    search_roots は決定論的な順序で渡されることを前提とする（呼び出し側
    ＝Bridge層が §6.4 の列挙順で並べる責務を持つ）。ここでは受け取った順に
    走査するのみで、順序の並べ替えは行わない。

    clock/listdir_fn/isdir_fn/islink_fn/realpath_fn はテスト用の差し替え
    フック（既定は time.monotonic / os.listdir / os.path.isdir /
    os.path.islink / os.path.realpath）。
    """
    if exclude_patterns is None:
        exclude_patterns = []
    if listdir_fn is None:
        listdir_fn = os.listdir
    if isdir_fn is None:
        isdir_fn = os.path.isdir
    if islink_fn is None:
        islink_fn = os.path.islink
    if realpath_fn is None:
        realpath_fn = os.path.realpath

    index = FileIndex()
    start = clock()
    deadline = start + timeout_sec
    visited_real_dirs = set()

    timed_out = False

    for root in search_roots:
        if not root:
            continue
        posix_root = to_posix_slashes(root)
        if not isdir_fn(posix_root):
            continue
        index.scanned_roots.append(posix_root)

        # 明示スタックによるBFS/DFS（os.walkではなく手動走査にすることで、
        # タイムアウトチェックとシンボリックリンク循環防止を細かく制御する）。
        stack = [posix_root]
        while stack:
            if clock() >= deadline:
                timed_out = True
                break
            current_dir = stack.pop()

            real = realpath_fn(current_dir)
            if real in visited_real_dirs:
                continue
            visited_real_dirs.add(real)

            try:
                entries = listdir_fn(current_dir)
            except OSError:
                continue

            for entry in sorted(entries):
                if _matches_any_exclude(entry, exclude_patterns):
                    continue
                full = current_dir.rstrip("/") + "/" + entry
                try:
                    is_dir = isdir_fn(full)
                except OSError:
                    continue
                if is_dir:
                    # シンボリックリンク/ジャンクションでも実体パスで
                    # visited判定するため、ここでは無条件にスタックへ積む
                    # （循環はrealpath突合せで防止される）。
                    stack.append(full)
                else:
                    index.add(entry, full)
                    index.file_count += 1

            if clock() >= deadline:
                timed_out = True
                break
        if timed_out:
            break

    index.finalize()
    index.is_complete = not timed_out
    index.timed_out = timed_out
    index.elapsed_sec = clock() - start
    return index
