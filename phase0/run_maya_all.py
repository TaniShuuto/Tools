# -*- coding: utf-8 -*-
"""V1, V4, V6, V9(FBX出力まで)を一括実行し phase0_results_maya.json を書く。

推奨: mayapy から実行する(利用者の開いているシーンに触れない)。
Maya の年号は固定しない。mayapy.exe の場所は PHASE0_RUNBOOK.md を見る。
  PowerShell: & "C:\\Program Files\\Autodesk\\<Maya>\\bin\\mayapy.exe" run_maya_all.py
  cmd:       "C:\\Program Files\\Autodesk\\<Maya>\\bin\\mayapy.exe" run_maya_all.py
"""
from __future__ import annotations

import os
import sys

PHASE0_DIR_OVERRIDE = ""
OUTPUT_JSON_OVERRIDE = ""

_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import v1_numpy_mayapy  # noqa: E402
import v4_connected_shaders  # noqa: E402
import v6_topology_fingerprint  # noqa: E402
import v9_triangulated_fbx  # noqa: E402
from common import (  # noqa: E402
    collect_env,
    default_results_path,
    exception_observation,
    make_check,
    print_results,
    resolve_phase0_dir,
    write_results,
)


def _run_one(label, fn):
    try:
        return fn()
    except Exception as exc:
        return make_check(
            "INCONCLUSIVE",
            "{0} が例外で中断".format(label),
            {"error": exception_observation(exc)},
        )


def run_all():
    return {
        "V1": _run_one("V1", v1_numpy_mayapy.run_check),
        "V4": _run_one("V4", v4_connected_shaders.run_check),
        "V6": _run_one("V6", v6_topology_fingerprint.run_check),
        "V9": _run_one("V9", v9_triangulated_fbx.run_check),
    }


def main():
    phase0 = resolve_phase0_dir(PHASE0_DIR_OVERRIDE) or _HERE
    path = default_results_path("maya", phase0, OUTPUT_JSON_OVERRIDE)
    checks = run_all()
    payload = write_results(path, checks, env=collect_env(), extra={"runner": "run_maya_all"})
    print_results(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
