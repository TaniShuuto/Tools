"""
anypath.bridge.gate0_probe  -  Gate 0 (G2/G4) 挙動観測（Maya実機での自動記録）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §1.4（Gate 0）

**このモジュールはGate 0の実測結論を出さない。** 実際のMaya環境で人間が
シーンを開いたりプラグインをロードしたりする普段の操作の中で、G2・G4の
判断材料となる観測データを自動収集し、ログへ記録するだけの役割を持つ。
結論（表を埋める作業）は人間が Gate0_検証手順.md に従って行う。

G2（dirmap実効果）:
    kAfterOpen 実行時、レジストリに登録された各ノードタイプについて、
    実際に読めた属性値をそのまま記録する。dirmapが「透過的に」書き換える
    という性質上、Python側からは「書き換え後の値」しか見えないが、これを
    記録し続けることで、人間が「意図的に壊れたパスを持つシーンを開いた
    直後の値」を見比べ、そのノード種にdirmapが効いたかどうかを判断できる
    ようにする。

G4（filePathEditor網羅性）:
    cmds.filePathEditor(query=True, listFiles=...) 等の横断列挙機能で
    実際に取得できるノードタイプと、明示レジストリ（§8）のノードタイプを
    突き合わせ、差分を記録する。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §1.4 に基づき新規作成（ユーザー指示により
          実測ではなく観測ログ記録機能のみを実装）。
"""

__version__ = "1.0.0"

import datetime
import os

import maya.cmds as cmds

from anypath.bridge.registry import registered_node_types
from anypath.core.gate0_format import (
    build_g2_record,
    build_g4_record,
    serialize_record,
)


def _gate0_log_path(log_dir):
    pid = os.getpid()
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    return "{0}/gate0_{1}_pid{2}.jsonl".format(log_dir, date_str, pid)


def _append_gate0_records(log_dir, records):
    if not records:
        return
    os.makedirs(log_dir, exist_ok=True)
    path = _gate0_log_path(log_dir)
    try:
        with open(path, "a", encoding="utf-8") as f:
            for record in records:
                f.write(serialize_record(record))
                f.write("\n")
    except Exception as e:
        print("[AnyPath][WARNING] Gate0観測ログの書き込みに失敗しました: {0}".format(e))


def observe_g2_dirmap(collected_attrs, log_dir, dirmap_enabled, mappings_active):
    """
    G2観測: kAfterOpen 実行時（snapshot_after_openと同じタイミング）に、
    レジストリ対象ノードの実測値を記録する。

    collected_attrs: bridge.collector.collect_all() の戻り値。
    """
    timestamp = datetime.datetime.now().isoformat()
    records = []
    for c in collected_attrs:
        if not c.raw_value:
            continue
        records.append(build_g2_record(
            timestamp=timestamp,
            node_type=c.node_type,
            attr_name=c.attr_name,
            pre_dirmap_path=None,  # Python側からは事前値を取得不能（設計上の制約）
            post_dirmap_path=c.raw_value,
            dirmap_enabled=dirmap_enabled,
            mappings_active=mappings_active,
        ))
    _append_gate0_records(log_dir, records)
    return records


def _list_filepatheditor_node_types():
    """
    cmds.filePathEditor の横断列挙機能で実際に取得できるノードタイプ一覧を
    返す。Mayaバージョンによりオプション名が異なる可能性があるため、複数の
    呼び出し方を試みる（G4の「網羅性実測」そのものがGate 0の目的であり、
    ここでの実装が完全である保証はない。これも含めて人間の確認対象）。
    """
    node_types = set()
    try:
        # queryFilePathEditorEntries系のAPIはMayaバージョンで差異があるため、
        # まず listDirectories / listFiles の代表的な呼び出しを試みる。
        items = cmds.filePathEditor(query=True, listFiles="", withAttribute=True) or []
        # itemsは [nodeName, attrName, nodeName, attrName, ...] の平坦なリスト
        # として返る想定（Mayaのドキュメントに基づく一般的な形式）。
        for i in range(0, len(items) - 1, 2):
            node_name = items[i]
            try:
                node_type = cmds.nodeType(node_name)
                node_types.add(node_type)
            except Exception:
                continue
    except Exception as e:
        print("[AnyPath][WARNING] filePathEditorの列挙に失敗しました"
              "（G4観測はスキップされます）: {0}".format(e))
    return node_types


def observe_g4_filepatheditor(registry, log_dir):
    """
    G4観測: filePathEditor系の横断列挙結果と明示レジストリの差分を記録する。
    """
    filepatheditor_types = _list_filepatheditor_node_types()
    registry_types = set(registered_node_types(registry))

    missing = registry_types - filepatheditor_types
    extra = filepatheditor_types - registry_types

    timestamp = datetime.datetime.now().isoformat()
    record = build_g4_record(
        timestamp=timestamp,
        filepatheditor_node_types=filepatheditor_types,
        registry_node_types=registry_types,
        missing_from_filepatheditor=missing,
        extra_in_filepatheditor=extra,
    )
    _append_gate0_records(log_dir, [record])
    return record
