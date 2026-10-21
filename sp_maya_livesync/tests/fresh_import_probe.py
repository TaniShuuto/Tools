# -*- coding: utf-8 -*-
"""新しいインタープリタで本体モジュールを import し、禁止モジュールが増えるかを調べる。

テストプロセスの ``sys.modules`` には、先に走った別のテストが読み込んだ本物の
PySide6 / PIL などが残る。そのため「import しても Maya・Qt・Pillow を読み込まない」
という検査は、現在のプロセスではなく別プロセスで行う。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_SCRIPT = r"""
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
modules = sys.argv[2:]


def forbidden():
    found = set()
    for name in sys.modules:
        if name == "maya" or name.startswith("maya."):
            found.add(name)
        elif name == "PIL" or name.startswith("PIL."):
            found.add(name)
        elif name in ("PySide2", "PySide6") or name.startswith(("PySide2.", "PySide6.")):
            found.add(name)
    return found


before = forbidden()
for module in modules:
    importlib.import_module(module)
print(json.dumps({
    "imported": all(module in sys.modules for module in modules),
    "new_forbidden": sorted(forbidden() - before),
}))
"""


def new_forbidden_modules(pkg_dir, modules):
    """``modules`` を新しい Python で import し、新たに読み込まれた禁止モジュール名の一覧を返す。"""
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(pkg_dir)] + list(modules),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=120,
        env=dict(os.environ),
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["imported"] is True
    return report["new_forbidden"]
