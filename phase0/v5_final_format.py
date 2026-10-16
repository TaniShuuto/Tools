# -*- coding: utf-8 -*-
"""V5: Final 出力フォルダの画像形式を表にする。単体 Python。

使い方:
    python v5_final_format.py <Finalフォルダ>

Pillow が無ければ PNG ヘッダを自前解析する。
期待値はリポジトリの sp_maya_livesync/sp_live_sync_plugin.py
($textureSet_* 周辺)から読む。
"""
from __future__ import annotations

import argparse
import os
import sys

PHASE0_DIR_OVERRIDE = ""
OUTPUT_JSON_OVERRIDE = ""

_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from common import (  # noqa: E402
    STATUS_FAIL,
    STATUS_INCONCLUSIVE,
    STATUS_PASS,
    anonymize_path,
    collect_env,
    default_results_path,
    exception_observation,
    make_check,
    print_results,
    resolve_phase0_dir,
    write_results,
)
from v5_image import (  # noqa: E402
    analyze_folder,
    extract_plugin_export_spec,
)


def find_plugin_path(phase0_dir):
    root = os.path.dirname(phase0_dir) if phase0_dir else None
    candidates = []
    if root:
        candidates.append(os.path.join(root, "sp_maya_livesync", "sp_live_sync_plugin.py"))
    candidates.append(os.path.join(_HERE, os.pardir, "sp_maya_livesync", "sp_live_sync_plugin.py"))
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isfile(path):
            return path
    return None


def load_expected_spec(phase0_dir):
    plugin = find_plugin_path(phase0_dir)
    if not plugin:
        return None, None
    with open(plugin, "r", encoding="utf-8") as handle:
        text = handle.read()
    return extract_plugin_export_spec(text), plugin


def run_check(folder):
    phase0 = resolve_phase0_dir(PHASE0_DIR_OVERRIDE) or _HERE
    spec, plugin = load_expected_spec(phase0)
    obs = {
        "folder": anonymize_path(os.path.abspath(folder)),
        "plugin_path": anonymize_path(plugin) if plugin else None,
        "expected": spec,
        "analysis": None,
    }
    if not os.path.isdir(folder):
        return make_check(
            STATUS_FAIL,
            "指定フォルダが存在しない",
            obs,
        )
    if spec is None:
        obs["plugin_error"] = "sp_live_sync_plugin.py を読めなかった"
    try:
        analysis = analyze_folder(folder, spec)
    except Exception as exc:
        obs["analysis_error"] = exception_observation(exc)
        return make_check(STATUS_INCONCLUSIVE, "画像解析中に例外", obs)
    obs["analysis"] = analysis
    if not analysis.get("ok"):
        return make_check(STATUS_INCONCLUSIVE, "フォルダを列挙できなかった", obs)
    if analysis.get("n_files", 0) == 0:
        return make_check(
            STATUS_INCONCLUSIVE,
            "フォルダに画像が無い。実機の Final 出力を指定すること",
            obs,
        )
    mismatches = []
    for row in analysis.get("files") or []:
        vs = row.get("vs_expected") or {}
        if vs.get("mismatches"):
            mismatches.append({
                "file": row.get("path"),
                "mismatches": vs["mismatches"],
            })
    obs["mismatches"] = mismatches
    if mismatches:
        return make_check(
            STATUS_FAIL,
            "画像 {0} 件中、期待値と食い違うものが {1} 件".format(
                analysis.get("n_files"), len(mismatches)
            ),
            obs,
        )
    return make_check(
        STATUS_PASS,
        "画像 {0} 件を解析し、読めた範囲でプラグイン期待値と一致".format(
            analysis.get("n_files")
        ),
        obs,
    )


def _output_path():
    phase0 = resolve_phase0_dir(PHASE0_DIR_OVERRIDE) or _HERE
    return default_results_path("misc", phase0, OUTPUT_JSON_OVERRIDE)


def main(argv=None):
    parser = argparse.ArgumentParser(description="V5 Final 出力フォルダの形式調査")
    parser.add_argument("folder", help="Final 出力フォルダのパス")
    parser.add_argument("--out", default="", help="結果JSONの出力先(省略可)")
    args = parser.parse_args(argv)
    global OUTPUT_JSON_OVERRIDE
    if args.out:
        OUTPUT_JSON_OVERRIDE = args.out
    check = run_check(args.folder)
    payload = write_results(_output_path(), {"V5": check}, env=collect_env())
    print_results(payload)
    return 0 if check["status"] in ("PASS", "INCONCLUSIVE", "SKIPPED", "MANUAL") else 1


if __name__ == "__main__":
    raise SystemExit(main())
