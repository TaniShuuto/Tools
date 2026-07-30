"""
anypath.bridge.registry  -  Bridge層: 対象ノード種の明示レジストリ
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §8（拡張性 ―― 対象ノードの登録）

要件:
    - 対象パス属性をハードコードしてはならない
    - 明示レジストリを唯一の正とする: {ノードタイプ: [属性名]} を JSON で
      外部化し、未登録のサードパーティ（Yeti、Ornatrix、Redshift、Substance
      ノード等）を利用者が追記できるようにする
    - Maya標準の横断列挙機能（filePathEditor系）は補助として併用する。
      Gate 0のG4で網羅性を実測し、漏れがある場合は明示レジストリのみを
      信頼する（本モジュール実装時点ではG4は未実測のため、既定では
      filePathEditorの併用を無効化しておき、G4確認後に有効化できるように
      している。詳細は gate0/ 配下を参照）

既定レジストリに必ず含めるもの:
    file, aiImage, aiStandIn, aiVolume, imagePlane, AlembicNode, gpuCache,
    audio, cacheFile, bifrost キャッシュ

本モジュールは設定(anypath.core.config)のnode_registryとマージされる形で
動作する。ハードコードされているのはこの「既定値」のみであり、利用者は
設定ファイル経由でいくらでも追記できる（Core層の DEFAULT_CONFIG と対になる
Bridge層側の既定値という位置づけ。Core自体はこのファイルをimportしない
＝Maya非依存の原則を壊さない）。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §8 に基づき新規作成。
"""

__version__ = "1.0.0"

# 設計書 §8: 「既定レジストリに必ず含めるもの」。
# bifrost キャッシュはMayaバージョンによりノードタイプ名が異なりうるため、
# 既知の代表的なタイプ名を列挙する（BifrostBoard等の追加が必要な場合は
# 設定のnode_registryで追記する）。
DEFAULT_NODE_REGISTRY = {
    "file": ["fileTextureName"],
    "aiImage": ["filename"],
    "aiStandIn": ["dso"],
    "aiVolume": ["filename"],
    "imagePlane": ["imageName"],
    "AlembicNode": ["abc_File"],
    "gpuCache": ["cacheFileName"],
    "audio": ["filename"],
    "cacheFile": ["cachePath"],
    "bifrostGraphShape": ["filepath"],
    "bifrostFluidShape": ["cacheDir"],
}


def merge_node_registry(config_registry):
    """
    設定(node_registry)を既定レジストリへマージする。

    設計書に「部分マージ/丸ごと置換」の明記が無いレジストリについては、
    「既定は必ず含める」という §8 の要件を守るため、キー単位でのマージ
    （利用者追記が既定を上書きしない限り両方生きる）とする。
    ただし利用者が既定のノードタイプに対して明示的に別の属性リストを
    指定した場合は、その利用者指定を優先する（意図的な上書きを許容する）。
    """
    merged = {k: list(v) for k, v in DEFAULT_NODE_REGISTRY.items()}
    if not config_registry:
        return merged
    for node_type, attrs in config_registry.items():
        if isinstance(attrs, list) and attrs:
            merged[node_type] = list(attrs)
    return merged


def registered_node_types(registry):
    """レジストリに登録されたノードタイプ一覧を、決定論的な順序（辞書順）で返す。"""
    return sorted(registry.keys())


def attrs_for_node_type(registry, node_type):
    """指定ノードタイプに登録された属性名一覧を返す（未登録なら空リスト）。"""
    return list(registry.get(node_type, []))
