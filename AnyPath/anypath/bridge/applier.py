"""
anypath.bridge.applier  -  Bridge層: 解決結果の属性適用（エフェメラル修復）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §4.2, §4.3, §4.4, §12-4

**この3点はセットで、どれか1つを崩すと往復運用が壊れるか、利用者が毎回
保存を促される（§12-4）:**
    1. 修復結果を保存しない（P3）
    2. Undoキューへ積まない
    3. 変更フラグ（modified）は退避・復元する

4.3 エフェメラル修復（P3）:
    修復結果をシーンファイルへ自動保存してはならない。開くたびにメモリ上で
    修復する冪等な仕組み。

4.4 変更フラグとUndo（二者択一の明示）:
    - 修復処理の前後で cmds.file(query=True, modified=True) の値を
      退避・復元する
    - 無条件の modified=False は禁止。必ず「退避 -> 復元」で行う
    - 修復処理は Undoキューへ積まない（undoInfo(stateWithoutFlush=...)に
      より除外）
    - v2の決定: 変更フラグの復元を優先し、Undoは非対応とする

4.2 適用時の技術的制約:
    - Alembic/GPU Cacheのrepathは大量のジオメトリ再読込で長時間停止しうる。
      進捗表示を必須とする（§6.6） -> progress_callback フックで対応
    - 属性のロック/コネクト済みノードは applier まで来ない想定
      （collector.py側でskip_reason付きにして除外済み）だが、念のため
      適用直前にも再確認する（多重防御。P1: 誤リンクゼロの徹底）

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §4.2〜§4.4 に基づき新規作成。
"""

__version__ = "1.0.0"

import maya.cmds as cmds

from anypath.core.types import Confidence

# Alembic/GPUCache等、repathが重い可能性のあるノードタイプ。
# これらへの適用時は進捗表示コールバックを必ず呼ぶ（§4.2, §6.6）。
_HEAVY_REPATH_NODE_TYPES = frozenset(["AlembicNode", "gpuCache", "cacheFile"])


class ApplyOutcome:
    """1件の適用結果（ログ・レポート向け。§6.7の修復ログの元になる）。"""
    __slots__ = ("node_key", "node_full_path", "attr_name", "node_type",
                 "before", "after", "strategy", "confidence", "applied",
                 "skip_reason")

    def __init__(self, node_key, node_full_path, attr_name, node_type,
                 before, after, strategy, confidence, applied, skip_reason=None):
        self.node_key = node_key
        self.node_full_path = node_full_path
        self.attr_name = attr_name
        self.node_type = node_type
        self.before = before
        self.after = after
        self.strategy = strategy
        self.confidence = confidence
        self.applied = applied
        self.skip_reason = skip_reason


def _set_attr_string(node_full_path, attr_name, value):
    """
    文字列属性(パス)への設定。多くのファイルパス属性は type="string" だが、
    ノードタイプによって異なる場合があるため、まずtype付きで試み、失敗したら
    typeなしにフォールバックする。
    """
    plug = "{0}.{1}".format(node_full_path, attr_name)
    try:
        cmds.setAttr(plug, value, type="string")
    except Exception:
        cmds.setAttr(plug, value)


def apply_resolutions(collected_by_key, resolve_results, progress_callback=None):
    """
    Core の解決結果を、対応するノード属性へ適用する（EXACT/HIGHのみ自動適用。
    §3.1: 「自動適用されるのはEXACTとHIGHのみ」）。

    collected_by_key: {node_key: CollectedAttr, ...}（collector.collect_all()
                       の結果を node_key でインデックス化したもの。呼び出し側
                       が用意する）
    resolve_results:  Core.resolve_all() の戻り値（ResolveResultのリスト）
    progress_callback: callable(current, total, node_key) または None。
                       Alembic/GPUCache等の重いノードを含む場合に必ず呼ぶ
                       （§4.2, §6.6: 「応答なしに見える状態を作らない」）。

    §4.3/§4.4 の要件をこの関数全体で満たす:
        - modified フラグを退避し、関数の最後で復元する
        - undoInfo(stateWithoutFlush=True) で修復処理全体をUndo対象外にする
        - シーンファイルへの保存は一切行わない（disk書き込みなし）

    戻り値: [ApplyOutcome, ...]（適用・スキップ含む全件、順序は
            resolve_results の順を保持する）
    """
    outcomes = []

    # --- §4.4: 変更フラグの退避 ---
    try:
        was_modified = cmds.file(query=True, modified=True)
    except Exception:
        was_modified = None

    # --- §4.4: 修復処理をUndoキューへ積まない ---
    # undoInfo(stateWithoutFlush=...) は state と同様「undo/redoのon/off」を
    # 切り替えるフラグだが、既存のUndoキューをフラッシュ（破棄）しない点が
    # state と異なる（Maya公式ドキュメント: 「Turns undo/redo on or off
    # without flushing the queue」）。修復処理の間だけこれで一時的にOFFへし、
    # 処理後に元の状態へ戻すことで、利用者が直前まで積んでいたUndo履歴を
    # 破棄せずに済ませる。
    undo_state_was_on = True
    try:
        undo_state_was_on = cmds.undoInfo(query=True, state=True)
        cmds.undoInfo(stateWithoutFlush=False)
    except Exception:
        pass

    total = len(resolve_results)
    heavy_total = sum(
        1 for r in resolve_results
        if r.node_key in collected_by_key
        and collected_by_key[r.node_key].node_type in _HEAVY_REPATH_NODE_TYPES
        and r.confidence.auto_apply
    )
    heavy_done = 0

    try:
        for idx, result in enumerate(resolve_results):
            collected = collected_by_key.get(result.node_key)
            if collected is None:
                continue

            node_type = collected.node_type
            is_heavy = node_type in _HEAVY_REPATH_NODE_TYPES

            if not result.confidence.auto_apply:
                outcomes.append(ApplyOutcome(
                    node_key=result.node_key,
                    node_full_path=collected.node_full_path,
                    attr_name=collected.attr_name,
                    node_type=node_type,
                    before=result.raw_path, after=None,
                    strategy=result.strategy, confidence=result.confidence,
                    applied=False, skip_reason="not_auto_apply",
                ))
                continue

            # 多重防御: 適用直前にもロック/コネクト状態を再確認する
            # （collector.py の判定後にシーン状態が変わっている可能性を考慮）。
            plug = "{0}.{1}".format(collected.node_full_path, collected.attr_name)
            try:
                is_locked = cmds.getAttr(plug, lock=True)
            except Exception:
                is_locked = False
            try:
                is_connected = bool(cmds.connectionInfo(plug, isDestination=True))
            except Exception:
                is_connected = False

            if is_locked or is_connected:
                reason = "attribute_locked" if is_locked else "attribute_connected"
                outcomes.append(ApplyOutcome(
                    node_key=result.node_key,
                    node_full_path=collected.node_full_path,
                    attr_name=collected.attr_name,
                    node_type=node_type,
                    before=result.raw_path, after=None,
                    strategy=result.strategy, confidence=result.confidence,
                    applied=False, skip_reason=reason,
                ))
                continue

            try:
                _set_attr_string(
                    collected.node_full_path, collected.attr_name,
                    result.resolved_path)
                applied = True
                skip_reason = None
            except Exception as e:
                applied = False
                skip_reason = "apply_failed: {0}".format(e)

            outcomes.append(ApplyOutcome(
                node_key=result.node_key,
                node_full_path=collected.node_full_path,
                attr_name=collected.attr_name,
                node_type=node_type,
                before=result.raw_path, after=result.resolved_path,
                strategy=result.strategy, confidence=result.confidence,
                applied=applied, skip_reason=skip_reason,
            ))

            if is_heavy and applied:
                heavy_done += 1
                if progress_callback is not None:
                    try:
                        progress_callback(heavy_done, heavy_total, result.node_key)
                    except Exception:
                        pass
    finally:
        # --- §4.4: Undo状態を元に戻す ---
        try:
            cmds.undoInfo(stateWithoutFlush=undo_state_was_on)
        except Exception:
            pass

        # --- §4.4: 変更フラグの復元（無条件False禁止。退避した値へ戻す）---
        if was_modified is not None:
            try:
                cmds.file(modified=was_modified)
            except Exception:
                pass

    return outcomes
