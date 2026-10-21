# -*- coding: utf-8 -*-
"""plugin の名前を差し替えるテスト用の補助関数。

プラグインは役割ごとの部品(sp_live_sync_plugin._base など)に分かれていて、
部品は ``from ._base import write_json_atomic`` のように名前を取り込む。
そのため ``monkeypatch.setattr(plugin, "write_json_atomic", ...)`` だけでは、
パッケージ本体の属性しか差し替わらず、部品の中の呼び出しには効かない。

patch_plugin_attr() は、パッケージ本体と各部品のうち、その名前が「差し替え前と
同じオブジェクト」を指しているものすべてを差し替える。元に戻すのは monkeypatch に任せる。
本体のコードは、テストのために書き換えない。
"""
from __future__ import annotations

import sys

_MISSING = object()


def plugin_modules(plugin):
    """パッケージ本体と、sys.modules にある部品(``<名前>.`` で始まるもの)を返す。"""
    prefix = plugin.__name__ + "."
    modules = [plugin]
    for name in sorted(sys.modules):
        module = sys.modules[name]
        if name.startswith(prefix) and module is not None:
            modules.append(module)
    return modules


def patch_plugin_attr(monkeypatch, plugin, name, value):
    """``plugin.<name>`` と同じオブジェクトを持つ、すべてのモジュールの ``<name>`` を差し替える。

    差し替えたモジュールの一覧を返す。どこにも無い名前、差し替え先が 1 つも
    無い場合は AssertionError(typo でテストが空振りしないように)。
    """
    original = getattr(plugin, name)
    targets = [
        module for module in plugin_modules(plugin)
        if module.__dict__.get(name, _MISSING) is original
    ]
    assert targets, "差し替え先がありません: {0}".format(name)
    for module in targets:
        monkeypatch.setattr(module, name, value)
    return targets
