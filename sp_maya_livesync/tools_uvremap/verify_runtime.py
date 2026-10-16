# -*- coding: utf-8 -*-
"""UVRemapRuntime の実機検証(mayapy バッチ)。未実行(実機必須)。

phase0/ からは import しない。結果JSONの形(checks → status/summary/observations)
と匿名化は verify_maya_arrays.py (PR #13) / phase0/common.py と同じ方針。

実行(学校PC。システムの Python は使わない):
  mayapy.exe verify_runtime.py
  PowerShell: & $mayapy verify_runtime.py

PIL / scipy / OpenImageIO には依存しない。numpy は mayapy 側のものを使う。
既存の名前付きシーンは触らない。バッチ以外は空の未保存シーンのみ許可。
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone

OUTPUT_JSON_OVERRIDE = ""

_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
_PKG_DIR = os.path.dirname(_HERE)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

SCHEMA = 1
JSON_SAFE_MAX_DEPTH = 8
JSON_SAFE_MAX_LIST = 200
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"
STATUS_MANUAL = "MANUAL"
STATUS_SKIPPED = "SKIPPED"
TS_NAME = "Body"
RES = 256

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _home_paths():
    homes = []
    for key in ("HOME", "USERPROFILE"):
        value = os.environ.get(key)
        if value:
            homes.append(os.path.abspath(value))
    try:
        homes.append(os.path.abspath(os.path.expanduser("~")))
    except Exception:
        pass
    uniq = []
    for item in homes:
        if item and item not in uniq:
            uniq.append(item)
    uniq.sort(key=len, reverse=True)
    return uniq


def _usernames():
    names = []
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(key)
        if value and len(value) > 1 and value not in names:
            names.append(value)
    try:
        home = os.path.expanduser("~")
        base = os.path.basename(os.path.abspath(home).rstrip("\\/"))
        if base and len(base) > 1 and base not in names:
            names.append(base)
    except Exception:
        pass
    names.sort(key=len, reverse=True)
    return names


def anonymize_text(text):
    if text is None:
        return None
    out = str(text)
    for home in _home_paths():
        home_fwd = home.replace("\\", "/")
        home_bwd = home.replace("/", "\\")
        out = re.sub(re.escape(home_fwd), "~", out, flags=re.IGNORECASE)
        out = re.sub(re.escape(home_bwd), "~", out, flags=re.IGNORECASE)
    out = _EMAIL_RE.sub("<email>", out)
    for name in _usernames():
        out = re.sub(re.escape(name), "<user>", out, flags=re.IGNORECASE)
    return out


def anonymize_path(path):
    if path is None:
        return None
    text = os.fspath(path)
    if not text:
        return text
    norm = text.replace("\\", "/")
    for home in _home_paths():
        home_norm = home.replace("\\", "/")
        if norm.lower().startswith(home_norm.lower()):
            rest = norm[len(home_norm):]
            if rest.startswith("/") or rest == "":
                norm = "~" + rest
                break
    return anonymize_text(norm)


def exception_observation(exc):
    return {
        "type": type(exc).__name__,
        "module": getattr(type(exc), "__module__", None),
        "message": anonymize_text(str(exc)),
    }


def format_exception(exc):
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return anonymize_text(text)


def json_safe(value, depth=0):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return anonymize_text(value)
    if isinstance(value, bytes):
        return {"_bytes_hex": value[:64].hex(), "n": len(value)}
    if depth > JSON_SAFE_MAX_DEPTH:
        return {"_truncated": True, "type": type(value).__name__}
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[str(key)] = json_safe(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        total = len(items)
        out = [json_safe(item, depth + 1) for item in items[:JSON_SAFE_MAX_LIST]]
        if total > JSON_SAFE_MAX_LIST:
            return {"_items": out, "_total": total}
        return out
    try:
        import numpy as np  # type: ignore
        if isinstance(value, np.ndarray):
            return {"dtype": str(value.dtype), "shape": [int(x) for x in value.shape]}
        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass
    try:
        return anonymize_text(repr(value))
    except Exception as exc:
        return {"_repr_error": exception_observation(exc), "type": type(value).__name__}


def safe_call(fn, *args, **kwargs):
    try:
        return {"ok": True, "value": fn(*args, **kwargs)}
    except Exception as exc:
        return {"ok": False, "error": exception_observation(exc)}


def collect_maya_about():
    info = {
        "available": False,
        "about": {"version": None, "apiVersion": None, "product": None},
        "error": None,
    }
    cmds = sys.modules.get("maya.cmds")
    if cmds is None:
        try:
            import maya.cmds as cmds  # type: ignore
        except Exception as exc:
            info["error"] = exception_observation(exc)
            return info
    try:
        about = {}
        for key in ("version", "apiVersion", "product"):
            called = safe_call(cmds.about, **{key: True})
            about[key] = called.get("value") if called.get("ok") else called
        info["about"] = about
        info["available"] = True
        return info
    except Exception as exc:
        info["error"] = exception_observation(exc)
        return info


def collect_env():
    return {
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "python_build": list(platform.python_build()),
        "platform": sys.platform,
        "os_name": os.name,
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "executable": anonymize_path(sys.executable),
        "cwd": anonymize_path(os.getcwd()),
        "argv0": anonymize_path(sys.argv[0]) if sys.argv else None,
        "maya": collect_maya_about(),
    }


def make_check(status, summary, observations=None):
    if status not in (
        STATUS_PASS, STATUS_FAIL, STATUS_INCONCLUSIVE, STATUS_MANUAL, STATUS_SKIPPED
    ):
        raise ValueError("unknown status: {0}".format(status))
    return {
        "status": status,
        "summary": anonymize_text(summary),
        "observations": json_safe(observations or {}),
    }


def write_results(path, checks, env=None, extra=None):
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "env": json_safe(env or collect_env()),
        "checks": json_safe(checks),
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "path": anonymize_path(path),
    }
    if extra:
        payload["extra"] = json_safe(extra)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)
    return payload


def print_results(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def try_import_maya():
    meta = {
        "available": False,
        "initialized_standalone": False,
        "batch": None,
        "error": None,
    }
    try:
        import maya.cmds as cmds  # type: ignore
        try:
            meta["batch"] = cmds.about(batch=True)
            meta["available"] = True
            return cmds, meta
        except Exception:
            pass
    except Exception as exc:
        meta["error"] = exception_observation(exc)

    try:
        import maya.standalone  # type: ignore
        maya.standalone.initialize(name="python")
        import maya.cmds as cmds  # type: ignore
        meta["initialized_standalone"] = True
        meta["batch"] = cmds.about(batch=True)
        meta["available"] = True
        meta["error"] = None
        return cmds, meta
    except Exception as exc:
        meta["error"] = exception_observation(exc)
        return None, meta


def scratch_scene_ok(cmds):
    info = {"ok": False, "reason": None, "scene_name": None, "batch": None}
    try:
        info["batch"] = cmds.about(batch=True)
        scene = cmds.file(query=True, sceneName=True) or ""
        info["scene_name"] = anonymize_path(scene) if scene else ""
        meshes = cmds.ls(type="mesh") or []
        info["mesh_count"] = len(meshes)
        if info["batch"]:
            info["ok"] = True
            info["reason"] = "maya_batch"
            return info
        if scene:
            info["reason"] = "named_scene_open_refused"
            return info
        if meshes:
            info["reason"] = "mesh_already_present_refused"
            return info
        info["ok"] = True
        info["reason"] = "empty_untitled_gui"
        return info
    except Exception as exc:
        info["reason"] = "scratch_check_failed"
        info["error"] = exception_observation(exc)
        return info


def default_results_path():
    if OUTPUT_JSON_OVERRIDE:
        return os.path.abspath(OUTPUT_JSON_OVERRIDE)
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or os.environ.get("TMPDIR")
    if not tmp:
        tmp = os.path.abspath(".")
    return os.path.join(tmp, "verify_runtime_results.json")


def _pass_or_fail(ok, summary, observations):
    return make_check(STATUS_PASS if ok else STATUS_FAIL, summary, observations)


def _write_smooth_png(path, res=RES):
    import numpy as np
    import uvremap_io
    yy, xx = np.meshgrid(
        np.linspace(0.0, 1.0, res, dtype=np.float32),
        np.linspace(0.0, 1.0, res, dtype=np.float32),
        indexing="ij",
    )
    img = np.stack([xx, yy, 0.5 * (xx + yy)], -1)
    uvremap_io.write_image(path, img, {"bit_depth": 8, "mode": "RGB", "ext": ".png"})


def _shape_of(cmds, transform):
    shapes = cmds.listRelatives(transform, shapes=True, fullPath=True) or []
    if not shapes:
        raise RuntimeError("no shape")
    return shapes[0]


def _make_shader(cmds, name):
    shader = cmds.shadingNode("lambert", asShader=True, name=name)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name + "SG")
    cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader", force=True)
    return shader, sg


def _poll_until(runtime, timeout_s=120.0):
    deadline = time.monotonic() + timeout_s
    events = []
    while time.monotonic() < deadline:
        events.extend(runtime.poll())
        if not runtime.running_job_ids():
            return events, None
        time.sleep(0.1)
    return events, "timeout"


def _cleanup_work(work):
    try:
        shutil.rmtree(work, ignore_errors=True)
        if os.path.isdir(work):
            return False
        return True
    except Exception:
        return False


def run_maya_cases(cmds):
    import uvremap_runtime as urm

    work = tempfile.mkdtemp(prefix="uvremap_vr_")
    checks = None
    extra = None
    try:
        root = os.path.join(work, "uvremap", "proj")
        src = os.path.join(work, "src")
        out = os.path.join(root, "output", "final")
        os.makedirs(src, exist_ok=True)
        os.makedirs(out, exist_ok=True)
        png_name = "{0}_BaseColor.png".format(TS_NAME)
        png_path = os.path.join(src, png_name)
        _write_smooth_png(png_path)
        flag_path = os.path.join(src, "_sync_complete.flag")
        with open(flag_path, "w", encoding="utf-8") as handle:
            handle.write("{}")

        cmds.file(new=True, force=True)
        cube = cmds.polyCube(name="uvremap_rt_cube")[0]
        cube_shape = _shape_of(cmds, cube)
        shader, sg = _make_shader(cmds, TS_NAME)
        cmds.sets(cube, edit=True, forceElement=sg)
        file_node = cmds.shadingNode("file", asTexture=True, name="uvremap_rt_file")
        cmds.setAttr(file_node + ".fileTextureName", png_path, type="string")
        cmds.setAttr(file_node + ".uvTilingMode", 0)
        original_path = cmds.getAttr(file_node + ".fileTextureName")
        original_tiling = int(cmds.getAttr(file_node + ".uvTilingMode"))

        runtime = urm.UVRemapRuntime(
            get_enabled=lambda: True,
            get_root=lambda _key: root,
            cmds_fn=lambda: cmds,
        )
        baseline_id = runtime.record_baseline("proj", [TS_NAME], [cube_shape])
        cmds.polyEditUV(cube + ".map[*]", u=0.07, v=-0.04)
        job_id = runtime.convert_now(
            "proj", TS_NAME, [cube_shape], src, out, quality="final",
            channels=[{"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"}],
            udim=False, out_res=RES, timeout_s=180, copy_retries=1, lock_timeout_s=30,
        )
        events, wait_err = _poll_until(runtime)
        after_path = cmds.getAttr(file_node + ".fileTextureName")
        after_tiling = int(cmds.getAttr(file_node + ".uvTilingMode"))
        has_orig = cmds.attributeQuery(urm.ATTR_ORIGINAL_PATH, node=file_node, exists=True)
        converted_name = os.path.basename(after_path or "")
        switched = bool(job_id) and events and events[0].get("exit_code") == 0
        switched = switched and converted_name == "{0}_BaseColor.png".format(TS_NAME)
        switched = switched and after_path != original_path

        runtime.revert_to_original(TS_NAME)
        reverted_path = cmds.getAttr(file_node + ".fileTextureName")
        reverted_tiling = int(cmds.getAttr(file_node + ".uvTilingMode"))
        still_has_orig = cmds.attributeQuery(urm.ATTR_ORIGINAL_PATH, node=file_node, exists=True)
        reverted = (reverted_path == original_path and reverted_tiling == original_tiling and not still_has_orig)

        mayapy = urm.resolve_mayapy()
        child_obs = {
            "mayapy": anonymize_path(mayapy) if mayapy else None,
            "unverified_note": "Maya GUI から mayapy を子プロセス起動する経路は実機未検証",
        }
        child_ok = False
        if mayapy:
            env = urm.worker_env()
            kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "env": env,
                "timeout": 30,
            }
            if os.name == "nt":
                kwargs["creationflags"] = urm.CREATE_NO_WINDOW
            try:
                proc = subprocess.run(
                    [mayapy, "-c", "import sys; print('child-ok')"],
                    **kwargs,
                )
                text = (proc.stdout or b"")
                if isinstance(text, bytes):
                    text = text.decode("utf-8", errors="replace")
                child_obs["returncode"] = proc.returncode
                child_obs["stdout"] = anonymize_text((text or "").strip()[:200])
                child_obs["pythonhome_stripped"] = "PYTHONHOME" not in env
                child_obs["pythonpath_stripped"] = "PYTHONPATH" not in env
                child_obs["pythonnousersite"] = env.get("PYTHONNOUSERSITE") == "1"
                child_ok = proc.returncode == 0 and "child-ok" in (text or "")
            except Exception as exc:
                child_obs["error"] = exception_observation(exc)

        checks = {
            "record_baseline": _pass_or_fail(
                bool(baseline_id),
                "基準UVを記録できる",
                {"baseline_id": baseline_id},
            ),
            "convert_and_switch": _pass_or_fail(
                switched,
                "convert_now → poll のあとファイルノードが変換後パスを指す",
                {
                    "job_id": job_id,
                    "events": events,
                    "wait_error": wait_err,
                    "original_basename": os.path.basename(original_path or ""),
                    "after_basename": converted_name,
                    "tiling": after_tiling,
                    "has_original_attr": has_orig,
                },
            ),
            "revert_to_original": _pass_or_fail(
                reverted,
                "revert_to_original で原本パスと tiling が戻り属性が消える",
                {
                    "reverted_basename": os.path.basename(reverted_path or ""),
                    "reverted_tiling": reverted_tiling,
                    "attr_removed": not still_has_orig,
                },
            ),
            "mayapy_child_process": make_check(
                STATUS_PASS if child_ok else (STATUS_FAIL if mayapy else STATUS_INCONCLUSIVE),
                "mayapy を PYTHONHOME/PYTHONPATH 無し・PYTHONNOUSERSITE=1 で子プロセス起動できる(実機未検証の項目)",
                child_obs,
            ),
        }
        extra = {
            "work_dir": "tmp",
            "ts_name": TS_NAME,
            "res": RES,
        }
    finally:
        if not _cleanup_work(work) and isinstance(checks, dict):
            for check in checks.values():
                if isinstance(check, dict) and isinstance(check.get("observations"), dict):
                    check["observations"]["cleanup"] = "削除できなかった"
    return checks, extra


def run_all(cmds, meta, scratch):
    try:
        checks, extra = run_maya_cases(cmds)
    except Exception as exc:
        checks = {
            "run": make_check(
                STATUS_INCONCLUSIVE,
                "Maya 操作中に例外",
                {"error": exception_observation(exc), "traceback": format_exception(exc)},
            )
        }
        extra = {}
    extra = dict(extra or {})
    extra["maya"] = meta
    extra["scratch"] = scratch
    return checks, extra


def main():
    cmds, meta = try_import_maya()
    if cmds is None:
        checks = {
            "run": make_check(
                STATUS_INCONCLUSIVE,
                "Maya が無い(この環境では未実行)",
                {"maya": meta},
            )
        }
        payload = write_results(
            default_results_path(), checks, env=collect_env(), extra={"runner": "verify_runtime"},
        )
        print_results(payload)
        return 0
    scratch = scratch_scene_ok(cmds)
    if not scratch.get("ok"):
        checks = {
            "run": make_check(
                STATUS_INCONCLUSIVE,
                "既存シーンを触らないため中止。mayapy か空の新規シーンで実行する",
                {"maya": meta, "scratch": scratch},
            )
        }
        payload = write_results(default_results_path(), checks, env=collect_env())
        print_results(payload)
        return 0
    try:
        checks, extra = run_all(cmds, meta, scratch)
    finally:
        try:
            cmds.file(new=True, force=True)
        except Exception:
            pass
    payload = write_results(
        default_results_path(),
        checks,
        env=collect_env(),
        extra=extra,
    )
    print_results(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
