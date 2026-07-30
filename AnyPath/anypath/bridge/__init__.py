"""
anypath.bridge  -  AnyPath Bridge層: Maya連携層

設計書 AnyPath_技術設計書_v2.1.md §2.1, §4, §5, §6.4, §6.7, §6.9, §8 より。

Core層（anypath.core）とは異なり、本パッケージは maya.cmds / maya.api を
前提とする。Maya環境外ではimportできない（このパッケージ配下のモジュールを
pytestで直接importするテストは書かない。Core層側にMaya非依存ロジックを
極力切り出し、そちらをpytest対象とする方針）。

モジュール構成:
    registry.py              対象ノード種の明示レジストリ（§8）
    collector.py              ノード収集（§4.1, §4.2）
    applier.py                 属性適用・エフェメラル修復（§4.2〜§4.4）
    reference_repath.py         リファレンス復旧経路B（§5.3）
    xgen_detector.py             XGenパス切れ検出（§5.4）
    logger.py                     修復ログ・レポート書き込み（§6.7）
    root_manager.py                探索ルート追加・削除の設定I/O（§6.4）
    diagnosis_controller.py         自己診断画面のデータ収集配線（§6.9）
    gate0_probe.py                   Gate0 G2/G4観測ログ記録（§1.4）
"""

__version__ = "1.0.0"
