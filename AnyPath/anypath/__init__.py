"""
anypath  -  Maya パス可搬性システム（プラグイン名: AnyPath）

設計書: AnyPath_技術設計書_v2.1.md が単一の真実源。

パッケージ構成（設計書 §2.1 の4層構成に対応）:
    anypath.core  -- Core: 解決エンジン層（Maya API非依存の純粋関数群）
    anypath.boot  -- Boot: 起動層（.mod ＋ autoload プラグイン ＋ インストーラ）

Bridge層（Maya連携層）・Shell層（UI層）は本パッケージの対象外
（別途実装される想定。§7.5の通り、本パッケージのCoreはライブラリとして
他ツールからimportして再利用できる）。
"""

__version__ = "1.0.0"
