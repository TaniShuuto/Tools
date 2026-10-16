# -*- coding: utf-8 -*-
"""V8: Windows で開いているファイルへの os.replace が失敗するかを表にする。

別プロセスが
  (a) PIL.Image.open(path) (遅延読み込みでハンドルが残る)
  (b) open(path, 'rb') したまま
  (c) numpy で全読み込み後に閉じた
の各状態で、os.replace(tmp, path) が成功するか。

Windows 以外は SKIPPED。Pillow が無ければ (a) は SKIPPED。
インストールはしない。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

PHASE0_DIR_OVERRIDE = ""
OUTPUT_JSON_OVERRIDE = ""

_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from common import (  # noqa: E402
    STATUS_INCONCLUSIVE,
    STATUS_PASS,
    STATUS_SKIPPED,
    anonymize_path,
    anonymize_text,
    collect_env,
    default_results_path,
    exception_observation,
    make_check,
    print_results,
    resolve_phase0_dir,
    write_results,
)


def _write_bytes(path, data):
    with open(path, "wb") as handle:
        handle.write(data)


def try_replace(src, dest):
    """os.replace の成否。例外は握りつぶさず記録する。"""
    started = time.time()
    try:
        os.replace(src, dest)
        return {
            "replaced": True,
            "error": None,
            "seconds": round(time.time() - started, 4),
        }
    except Exception as exc:
        return {
            "replaced": False,
            "error": exception_observation(exc),
            "traceback": anonymize_text(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            ),
            "seconds": round(time.time() - started, 4),
        }


def _child_hold_script():
    """子プロセスが標準入力待ちしている間、ハンドルを保持する。"""
    return r"""
import os, sys
mode = sys.argv[1]
path = sys.argv[2]
ready = sys.argv[3]
handles = []
img = None
arr = None
if mode == "a":
    from PIL import Image
    img = Image.open(path)
    # 遅延読み込みのままヘッダだけ触る
    _ = img.size
elif mode == "b":
    handles.append(open(path, "rb"))
elif mode == "c":
    import numpy
    arr = numpy.fromfile(path, dtype=numpy.uint8)
else:
    raise SystemExit("bad mode")
with open(ready, "w", encoding="utf-8") as h:
    h.write("ready\n")
sys.stdin.read(1)
if handles:
    for h in handles:
        h.close()
if img is not None:
    img.close()
"""


def _spawn_holder(mode, target, script_path, ready_path):
    import subprocess

    env = os.environ.copy()
    # 子に個人情報を増やして渡さない。既存環境をそのまま使う。
    proc = subprocess.Popen(
        [sys.executable, script_path, mode, target, ready_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if os.path.isfile(ready_path) and os.path.getsize(ready_path) > 0:
            return proc, None
        if proc.poll() is not None:
            out = (proc.stdout.read() if proc.stdout else b"") + (proc.stderr.read() if proc.stderr else b"")
            return None, {
                "child_exited": True,
                "returncode": proc.returncode,
                "output": anonymize_text(out.decode("utf-8", "replace")[-400:]),
            }
        time.sleep(0.05)
    proc.kill()
    return None, {"child_timeout": True}


def run_lock_cases(work_dir=None):
    """表を返す。Windows 以外では cases は空、skipped_reason を付ける。"""
    table = {
        "os_name": os.name,
        "platform": sys.platform,
        "cases": [],
        "skipped_reason": None,
    }
    if os.name != "nt":
        table["skipped_reason"] = "not_windows"
        return table

    pillow = None
    numpy_mod = None
    try:
        from PIL import Image as pillow  # noqa: F401
        pillow_ok = True
    except Exception as exc:
        pillow_ok = False
        pillow_err = exception_observation(exc)
    try:
        import numpy as numpy_mod  # noqa: F401
        numpy_ok = True
    except Exception as exc:
        numpy_ok = False
        numpy_err = exception_observation(exc)

    own_dir = False
    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="phase0_v8_")
        own_dir = True
    script_path = os.path.join(work_dir, "_hold.py")
    with open(script_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_child_hold_script())

    payload = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 64)

    def one_case(key, mode, available, skip_reason):
        row = {
            "key": key,
            "mode": mode,
            "skipped": not available,
            "skip_reason": skip_reason if not available else None,
            "replaced": None,
            "error": None,
        }
        if not available:
            return row
        target = os.path.join(work_dir, "target_{0}.bin".format(key))
        tmp = os.path.join(work_dir, "tmp_{0}.bin".format(key))
        ready = os.path.join(work_dir, "ready_{0}.txt".format(key))
        _write_bytes(target, payload)
        _write_bytes(tmp, payload + b"NEW")
        proc, spawn_err = _spawn_holder(mode, target, script_path, ready)
        if spawn_err:
            row["replaced"] = None
            row["error"] = spawn_err
            row["status"] = "INCONCLUSIVE"
            return row
        try:
            result = try_replace(tmp, target)
            row["replaced"] = result["replaced"]
            row["error"] = result.get("error")
            row["seconds"] = result.get("seconds")
            row["status"] = "ran"
        finally:
            try:
                if proc.stdin:
                    proc.stdin.write(b"x")
                    proc.stdin.close()
                proc.wait(timeout=8)
            except Exception as exc:
                row["child_close_error"] = exception_observation(exc)
                try:
                    proc.kill()
                except Exception:
                    pass
        return row

    table["cases"].append(one_case(
        "a_pil_open", "a", pillow_ok,
        None if pillow_ok else "pillow_missing",
    ))
    if not pillow_ok:
        table["cases"][-1]["pillow_error"] = pillow_err
    table["cases"].append(one_case("b_open_rb", "b", True, None))
    table["cases"].append(one_case(
        "c_numpy_read_closed", "c", numpy_ok,
        None if numpy_ok else "numpy_missing",
    ))
    if not numpy_ok:
        table["cases"][-1]["numpy_error"] = numpy_err

    if own_dir:
        table["work_dir"] = anonymize_path(work_dir)
    return table


def run_check(work_dir=None):
    table = run_lock_cases(work_dir)
    if table.get("skipped_reason") == "not_windows":
        return make_check(
            STATUS_SKIPPED,
            "Windows 以外のため未実施(このOSではファイルロックの意味が違う)",
            table,
        )
    ran = [c for c in table.get("cases") if c.get("status") == "ran"]
    if not ran:
        return make_check(STATUS_INCONCLUSIVE, "実施できたケースが無い", table)
    return make_check(
        STATUS_PASS,
        "Windows の os.replace 表を作成。実施 {0} 件".format(len(ran)),
        table,
    )


def _output_path():
    phase0 = resolve_phase0_dir(PHASE0_DIR_OVERRIDE) or _HERE
    return default_results_path("misc", phase0, OUTPUT_JSON_OVERRIDE)


def main():
    check = run_check()
    payload = write_results(_output_path(), {"V8": check}, env=collect_env())
    print_results(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
