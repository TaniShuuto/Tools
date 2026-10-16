# -*- coding: utf-8 -*-
"""テスト用ランナー: スタブ core を ``uvremap_core`` として差し込んでワーカーを CLI 同様に実行する。

``python _run_worker_stub.py run <job.json>``  (サブプロセスでの kill / timeout テスト用)
"""
import os
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_TESTS)
for _p in (_PKG, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stub_uvremap_core  # noqa: E402

sys.modules["uvremap_core"] = stub_uvremap_core

import uvremap_worker  # noqa: E402

if __name__ == "__main__":
    sys.exit(uvremap_worker.main())
