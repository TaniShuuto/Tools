"""
anypath.bridge.collector  -  Bridge層: ノード収集
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §4.1, §4.2, §3.4-1, §8

役割:
    シーン中のノードをレジストリ（§8）に基づいて走査し、Core層へ渡す
    ResolveInput のリストを構築する。あわせて、リファレンス内ノード・
    ロック属性・コネクト済み属性など「そもそも書き換えてはいけない/
    書き換えられない」ノードをここで判定し、後続の適用フェーズ
    （applier.py）へ引き継ぐための CollectedAttr を作る。

決定論性（§3.4-1・実装必須）:
    「Bridge の収集層は、ノードのフルパス名で昇順ソートしてから Core へ渡す」
    ―― collect_all() の戻り値は常にフルパス名（node.attr形式）の昇順で
    ソートされている。

技術的制約への対処（§4.2）:
    - リファレンス内ノードは編集不可 -> cmds.referenceQuery(isNodeReferenced)
      で判定しスキップ（リファレンス自体のrepath経路は別モジュール
      reference_repath.py へ回す）
    - 属性のロック/コネクト済み -> getAttr(lock=True)、connectionInfoを
      確認しスキップしてレポートへ記載
    - ノード名の名前空間 -> 常にフルパス名を用い、短縮名を使わない

本モジュールは maya.cmds に依存する（Bridge層はMaya API依存が前提。
Core層のみがMaya非依存という設計書 §2.1 の分離を踏襲する）。

変更履歴:
    1.1.0 (2026.08.01):
        - ユーザー報告対応（「これでOKを押すと『不明なオブジェクト
          タイプ: bifrostFluidShape』という表示が出る」）: registry.py
          のDEFAULT_NODE_REGISTRYに含まれるbifrost系タイプ名は、Maya
          バージョンやBifrostプラグインの導入状況によって実在しない
          場合がある。cmds.ls(type=<存在しないタイプ名>)はPython例外を
          投げず警告のみ出して空リストを返すため、既存のtry/exceptでは
          抑止できていなかった。_known_node_types()を新設し、
          cmds.allNodeTypes()で実在するノードタイプ名を1回だけ収集・
          キャッシュした上で、collect_all()のループ内で登録タイプが
          実在するか確認してからcmds.ls()を呼ぶようにした。実在しない
          タイプは黙ってスキップされ、警告自体が出なくなる。
          allNodeTypes()自体が失敗する異常環境ではNoneのまま従来通り
          ls()を試みるフォールバックを残した（P4: 個別失敗で止めない）。
        - 後方互換性: PATCH相当。収集結果（実在するノードタイプの
          収集内容）は変更していない。実在しないタイプが収集対象から
          外れるようになった点のみが挙動差だが、そもそも実在しない
          ノードは収集できていなかったので実質的な差は無い。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §4.1, §4.2, §8 に基づき新規作成。
"""

__version__ = "1.1.0"

import maya.cmds as cmds

from anypath.bridge.registry import attrs_for_node_type, registered_node_types
from anypath.core.types import ResolveInput


class CollectedAttr:
    """
    収集された1属性の情報。ResolveInput.node_key として
    "<フルパス名>.<属性名>" を用いる。

    skip_reason が None でなければ、この属性はCoreへ渡さず
    「スキップされた」項目としてレポートに記載する対象（§4.2）。
    """
    __slots__ = ("node_full_path", "attr_name", "node_type", "raw_value",
                 "is_referenced", "skip_reason")

    def __init__(self, node_full_path, attr_name, node_type, raw_value,
                 is_referenced=False, skip_reason=None):
        self.node_full_path = node_full_path
        self.attr_name = attr_name
        self.node_type = node_type
        self.raw_value = raw_value
        self.is_referenced = is_referenced
        self.skip_reason = skip_reason

    @property
    def node_key(self):
        return "{0}.{1}".format(self.node_full_path, self.attr_name)

    def __repr__(self):
        return "CollectedAttr({0}, referenced={1}, skip={2!r})".format(
            self.node_key, self.is_referenced, self.skip_reason)


_known_node_types_cache = None


def _known_node_types():
    """
    Mayaに実在するノードタイプ名の集合を返す（呼び出し1回目にキャッシュ）。

    2026.08.01新設: registry.pyのDEFAULT_NODE_REGISTRYには
    bifrostGraphShape/bifrostFluidShape等、Mayaバージョンや
    プラグイン導入状況によって実在しないノードタイプ名が含まれる。
    cmds.ls(type=<存在しないタイプ名>) はPython例外を投げず、
    「# 警告: 不明なオブジェクト タイプ: <タイプ名>」という警告を
    Scriptエディタへ出すだけで空リストを返すため、既存のtry/exceptでは
    抑止できていなかった（ユーザー報告: 「これでOK」を押すたびに
    bifrostFluidShapeの警告が出る）。cmds.allNodeTypes()で実在確認して
    から ls(type=...) を呼ぶことで、この警告自体を出さないようにする。
    """
    global _known_node_types_cache
    if _known_node_types_cache is None:
        try:
            _known_node_types_cache = frozenset(cmds.allNodeTypes() or [])
        except Exception:
            # allNodeTypes自体が使えない異常環境では、フィルタを諦めて
            # 常にTrueを返す集合非対応モードにする（従来動作へフォールバック。
            # P4: 個別失敗で全体を止めない）。
            _known_node_types_cache = None
    return _known_node_types_cache


def _is_node_referenced(node_full_path):
    """§4.2: リファレンス内ノードは編集不可のため判定してスキップする。"""
    try:
        return bool(cmds.referenceQuery(node_full_path, isNodeReferenced=True))
    except Exception:
        # referenceQuery自体が使えないノード種（一部の特殊ノード）は
        # リファレンスされていないものとして扱う（安全側: 適用を試み、
        # 適用時に別のエラーとして捕捉される）。
        return False


def _is_attr_locked_or_connected(node_full_path, attr_name):
    """
    §4.2: 属性のロック/コネクト済みを確認する。
    戻り値: (should_skip: bool, reason: str or None)
    """
    plug = "{0}.{1}".format(node_full_path, attr_name)
    try:
        if cmds.getAttr(plug, lock=True):
            return True, "attribute_locked"
    except Exception:
        pass
    try:
        connected = cmds.connectionInfo(plug, isDestination=True)
        if connected:
            return True, "attribute_connected"
    except Exception:
        pass
    return False, None


def collect_all(registry, include_referenced_for_report=True):
    """
    レジストリ（§8）に基づき、シーン中の全対象ノード・属性を走査する。

    registry: {ノードタイプ: [属性名]}（bridge.registry.merge_node_registry
              の戻り値を渡す想定）。
    include_referenced_for_report: True の場合、リファレンス内ノードも
              CollectedAttr として収集する（skip_reason="referenced"付き。
              レポート上に「スキップされた」件数として出すため）。
              False の場合はそもそも収集しない。

    戻り値: CollectedAttr のリスト。node_key（フルパス名.属性名）の
            昇順でソート済み（§3.4-1 決定論性要件）。
    """
    collected = []
    known_types = _known_node_types()

    for node_type in registered_node_types(registry):
        attrs = attrs_for_node_type(registry, node_type)
        if not attrs:
            continue

        # 2026.08.01: known_typesが取得できている場合のみ実在確認する
        # （取得できなかった異常環境ではNoneのまま従来通りls()を試みる）。
        # 未登録タイプ名（bifrostFluidShape等、プラグイン未導入や
        # Mayaバージョン差で存在しない場合）は、cmds.ls()を呼ぶこと
        # 自体を避けて「不明なオブジェクト タイプ」警告を出さないように
        # する。P4: 個々の失敗（ここでは「そもそも存在しない」）で
        # 全体を止めない。
        if known_types is not None and node_type not in known_types:
            continue

        try:
            nodes = cmds.ls(type=node_type, long=True) or []
        except Exception:
            # 未知のノードタイプ（プラグイン未ロード等）はスキップする。
            # P4: 個々の失敗でシーンオープン全体を止めない。
            continue

        for node_full_path in nodes:
            is_referenced = _is_node_referenced(node_full_path)

            for attr_name in attrs:
                plug = "{0}.{1}".format(node_full_path, attr_name)
                if not cmds.attributeQuery(attr_name, node=node_full_path, exists=True):
                    continue

                if is_referenced:
                    if include_referenced_for_report:
                        try:
                            raw_value = cmds.getAttr(plug)
                        except Exception:
                            raw_value = None
                        collected.append(CollectedAttr(
                            node_full_path=node_full_path, attr_name=attr_name,
                            node_type=node_type, raw_value=raw_value,
                            is_referenced=True, skip_reason="referenced",
                        ))
                    continue

                should_skip, skip_reason = _is_attr_locked_or_connected(
                    node_full_path, attr_name)

                try:
                    raw_value = cmds.getAttr(plug)
                except Exception:
                    # 値を読めない属性はそもそも対象外（P4: 個別失敗で止めない）
                    continue

                collected.append(CollectedAttr(
                    node_full_path=node_full_path, attr_name=attr_name,
                    node_type=node_type, raw_value=raw_value,
                    is_referenced=False, skip_reason=skip_reason,
                ))

    # 決定論性要件（§3.4-1）: フルパス名で昇順ソート。
    # node_key は "フルパス.属性名" なので、フルパス→属性名の順で安定ソート
    # されるよう明示的にタプルキーを使う。
    collected.sort(key=lambda c: (c.node_full_path, c.attr_name))
    return collected


def to_resolve_inputs(collected_attrs):
    """
    スキップ対象を除いた CollectedAttr のリストから、Core へ渡す
    ResolveInput のリストを構築する（既にnode_key昇順ソート済みの
    collected_attrsをそのままの順で変換するため、決定論性は保たれる）。
    """
    inputs = []
    for c in collected_attrs:
        if c.skip_reason is not None:
            continue
        if not c.raw_value:
            continue
        inputs.append(ResolveInput(node_key=c.node_key, raw_path=c.raw_value))
    return inputs
