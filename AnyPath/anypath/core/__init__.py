"""
anypath.core  -  AnyPath Core: 解決エンジン層

設計書 AnyPath_技術設計書_v2.1.md §2.1 より:
    「最重要の設計上の分離：Core は Maya API を一切 import しないこと。」
    「Core の入力は『壊れたパス文字列』『探索ルート一覧』『インデックス』
     『設定』のみ、出力は『候補リストと確信度』のみです。」

このパッケージ配下のいかなるモジュールも `maya.*` を import してはならない。
pytest から Maya を起動せず単体テストできることが本パッケージ全体の前提。

公開API:
    resolve_all(resolve_inputs, config, project_root=None, ...) -> [ResolveResult]
        anypath.core.cascade より

    load_layered_config(project_config_path, machine_config_path, environ)
        -> ConfigLoadReport
        anypath.core.config より
"""

__version__ = "1.0.0"

from anypath.core.cascade import resolve_all, ResolveContext
from anypath.core.config import load_layered_config, apply_clamps, DEFAULT_CONFIG
from anypath.core.types import (
    Confidence,
    Strategy,
    ResolveInput,
    ResolveResult,
    Candidate,
)

__all__ = [
    "resolve_all",
    "ResolveContext",
    "load_layered_config",
    "apply_clamps",
    "DEFAULT_CONFIG",
    "Confidence",
    "Strategy",
    "ResolveInput",
    "ResolveResult",
    "Candidate",
]
