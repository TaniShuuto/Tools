# -*- coding: utf-8 -*-
"""V1: Maya Python の実行環境(numpy 等)を調査する。

mayapy でも Maya Script Editor でも実行できる。
numpy が無くてもインストールしない。pip モジュールの有無だけ報告する。
PATH 上の別 Python 3 と、その numpy の有無も subprocess で試す。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

PHASE0_DIR_OVERRIDE = ""
OUTPUT_JSON_OVERRIDE = ""

try:
    from common import (
        STATUS_INCONCLUSIVE,
        STATUS_PASS,
        anonymize_path,
        anonymize_text,
        collect_env,
        default_results_path,
        exception_observation,
        make_check,
        print_results,
        probe_module,
        resolve_phase0_dir,
        safe_call,
        try_import_maya,
        write_results,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd())
    from common import (
        STATUS_INCONCLUSIVE,
        STATUS_PASS,
        anonymize_path,
        anonymize_text,
        collect_env,
        default_results_path,
        exception_observation,
        make_check,
        print_results,
        probe_module,
        resolve_phase0_dir,
        safe_call,
        try_import_maya,
        write_results,
    )


MODULE_NAMES = ("numpy", "PIL", "scipy", "OpenImageIO", "pip")


def _run_cmd(args, timeout=12):
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": anonymize_text((proc.stdout or "").strip()),
            "stderr": anonymize_text((proc.stderr or "").strip()),
            "argv": [anonymize_path(a) for a in args],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": exception_observation(exc),
            "argv": [anonymize_path(a) for a in args],
        }


def probe_path_pythons():
    """PATH 上の python / python3 / py を調べる。失敗は記録するだけ。"""
    found = []
    seen = set()
    candidates = []
    for name in ("python", "python3", "py"):
        path = shutil.which(name)
        if path:
            candidates.append((name, path))
    # Windows の py ランチャ
    py = shutil.which("py")
    extra_cmds = []
    if py:
        extra_cmds.append(("py -3", [py, "-3"]))

    for name, path in candidates:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        version = _run_cmd([path, "--version"])
        numpy_probe = _run_cmd(
            [path, "-c", "import numpy; print(getattr(numpy, '__version__', 'unknown'))"]
        )
        found.append({
            "name": name,
            "path": anonymize_path(path),
            "version": version,
            "numpy": numpy_probe,
        })
    for label, args in extra_cmds:
        version = _run_cmd(args + ["--version"])
        numpy_probe = _run_cmd(
            args + ["-c", "import numpy; print(getattr(numpy, '__version__', 'unknown'))"]
        )
        found.append({
            "name": label,
            "path": anonymize_path(args[0]),
            "version": version,
            "numpy": numpy_probe,
        })
    return found


def collect_v1_observations():
    obs = {
        "this_interpreter": {
            "version": sys.version.split()[0],
            "full": anonymize_text(sys.version),
            "platform": sys.platform,
            "executable": anonymize_path(sys.executable),
        },
        "modules": {},
        "path_pythons": probe_path_pythons(),
        "maya": {"available": False},
    }
    for name in MODULE_NAMES:
        obs["modules"][name] = probe_module(name)

    cmds, meta = try_import_maya()
    obs["maya"] = {
        "available": meta.get("available"),
        "initialized_standalone": meta.get("initialized_standalone"),
        "batch": meta.get("batch"),
        "error": meta.get("error"),
    }
    if cmds is not None:
        about = {}
        for key, kwargs in (
            ("version", {"version": True}),
            ("apiVersion", {"apiVersion": True}),
            ("product", {"product": True}),
            ("windows", {"windows": True}),
            ("mac", {"mac": True}),
            ("linux", {"linux": True}),
            ("batch", {"batch": True}),
            ("qtVersion", {"qtVersion": True}),
        ):
            called = safe_call(cmds.about, **kwargs)
            about[key] = called.get("value") if called.get("ok") else called
        obs["maya"]["about"] = about
    return obs


def run_check():
    obs = collect_v1_observations()
    numpy_ok = bool(obs["modules"].get("numpy", {}).get("installed"))
    maya_ok = bool(obs["maya"].get("available"))
    if maya_ok:
        status = STATUS_PASS
        summary = "Maya 実行環境の調査完了。numpy={0}".format(
            "あり" if numpy_ok else "なし(インストールはしていない)"
        )
    else:
        status = STATUS_INCONCLUSIVE
        summary = "Maya に接続できなかった。このインタプリタのモジュール有無のみ記録。"
    return make_check(status, summary, obs)


def _output_path():
    phase0 = resolve_phase0_dir(PHASE0_DIR_OVERRIDE)
    return default_results_path("maya", phase0, OUTPUT_JSON_OVERRIDE)


def main():
    check = run_check()
    payload = write_results(_output_path(), {"V1": check}, env=collect_env())
    print_results(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
