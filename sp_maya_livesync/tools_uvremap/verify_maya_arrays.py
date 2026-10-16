# -*- coding: utf-8 -*-
"""collect_arrays / list_texture_sets の実機検証(mayapy バッチ)。

phase0/ からは import しない。結果JSONの形(checks → status/summary/observations)
と匿名化は phase0/common.py (feat/uvremap-phase0, 93cedd4) と同じ方針を、
このファイル内の最小処理として持つ。

実行(学校PC。システムの Python は使わない):
  mayapy.exe verify_maya_arrays.py
  PowerShell: & $mayapy verify_maya_arrays.py

PIL / scipy / OpenImageIO には依存しない。numpy は mayapy 側のものを使う。
既存の名前付きシーンは触らない。
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
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
INITIAL_SG = "initialShadingGroup"

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
    """文字列・数値・真偽値・None は深さに関係なく残す。入れ子上限は dict/list のみ。"""
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
            return {
                "dtype": str(value.dtype),
                "shape": [int(x) for x in value.shape],
            }
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


def new_scratch_scene(cmds):
    cmds.file(new=True, force=True)
    return True


def default_results_path():
    if OUTPUT_JSON_OVERRIDE:
        return os.path.abspath(OUTPUT_JSON_OVERRIDE)
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or os.environ.get("TMPDIR")
    if not tmp:
        tmp = os.path.abspath(".")
    return os.path.join(tmp, "verify_maya_arrays_results.json")


def _make_shader(cmds, name):
    shader = cmds.shadingNode("lambert", asShader=True, name=name)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name + "SG")
    cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader", force=True)
    return shader, sg


def _shape_of(cmds, transform):
    shapes = cmds.listRelatives(transform, shapes=True, fullPath=True) or []
    if not shapes:
        raise RuntimeError("no shape")
    return shapes[0]


def _face_range(transform, start, end_inclusive):
    if start == end_inclusive:
        return "{0}.f[{1}]".format(transform, start)
    return "{0}.f[{1}:{2}]".format(transform, start, end_inclusive)


def _assign_two_materials(cmds, transform, unassign_last=False):
    shape = _shape_of(cmds, transform)
    face_count = int(cmds.polyEvaluate(shape, face=True))
    shader_a, sg_a = _make_shader(cmds, transform + "_matA")
    shader_b, sg_b = _make_shader(cmds, transform + "_matB")
    mid = max(1, face_count // 2)
    faces_a = _face_range(transform, 0, mid - 1) if face_count else None
    last = None
    faces_b = None
    if unassign_last and face_count:
        last = _face_range(transform, face_count - 1, face_count - 1)
        if mid <= face_count - 2:
            faces_b = _face_range(transform, mid, face_count - 2)
    elif face_count and mid <= face_count - 1:
        faces_b = _face_range(transform, mid, face_count - 1)
    if faces_a:
        cmds.sets(faces_a, edit=True, forceElement=sg_a)
    if faces_b:
        cmds.sets(faces_b, edit=True, forceElement=sg_b)
    if last:
        for sg in (sg_a, sg_b, INITIAL_SG):
            try:
                cmds.sets(last, edit=True, remove=sg)
            except Exception:
                pass
    unassigned_idx = face_count - 1 if last else None
    return {
        "transform": transform,
        "shape": shape,
        "face_count": face_count,
        "sg_a": sg_a,
        "sg_b": sg_b,
        "shader_a": shader_a,
        "shader_b": shader_b,
        "mid": mid,
        "unassigned_idx": unassigned_idx,
    }


def _pass_or_fail(ok, summary, observations):
    return make_check(STATUS_PASS if ok else STATUS_FAIL, summary, observations)


def _array_consistency(np, arrays):
    counts = arrays["face_vertex_counts"]
    corner_n = int(counts.sum()) if counts.size else 0
    face_n = int(counts.size)
    obs = {
        "C": corner_n,
        "F": face_n,
        "T": int(arrays["tri_face"].size),
        "shapes": {key: list(arrays[key].shape) for key in arrays},
        "dtypes": {key: str(arrays[key].dtype) for key in arrays},
    }
    ok = True
    reasons = []
    if arrays["corner_vid"].shape != (corner_n,):
        ok = False
        reasons.append("corner_vid")
    if arrays["corner_pos"].shape != (corner_n, 3):
        ok = False
        reasons.append("corner_pos")
    if arrays["corner_uv"].shape != (corner_n, 2):
        ok = False
        reasons.append("corner_uv")
    if arrays["face_mesh"].shape != (face_n,):
        ok = False
        reasons.append("face_mesh")
    if arrays["tri_face"].shape != (obs["T"],):
        ok = False
        reasons.append("tri_face")
    if arrays["tri_corner"].shape != (obs["T"], 3):
        ok = False
        reasons.append("tri_corner")
    obs["fail_keys"] = reasons
    return ok, obs


def _tri_in_range(np, arrays):
    counts = arrays["face_vertex_counts"]
    tri_face = arrays["tri_face"]
    tri_corner = arrays["tri_corner"]
    if tri_face.size == 0:
        return True, {"T": 0}
    if np.any(tri_face < 0) or np.any(tri_face >= counts.size):
        return False, {"bad_tri_face": True}
    n_local = counts[tri_face]
    ok = bool(np.all(tri_corner >= 0) and np.all(tri_corner < n_local[:, None]))
    return ok, {"T": int(tri_face.size), "ok": ok}


def _faces_covered(np, arrays, info):
    face_n = int(arrays["face_vertex_counts"].size)
    excluded = set(info.get("nonmanifold_faces") or [])
    excluded.update(info.get("untriangulated_faces") or [])
    covered = set(int(i) for i in arrays["tri_face"].tolist())
    expected = set(range(face_n)) - excluded
    missing = sorted(expected - covered)
    extra = sorted(covered & excluded)
    ok = not missing and not extra
    return ok, {
        "F": face_n,
        "n_excluded": len(excluded),
        "n_missing": len(missing),
        "missing_head": missing[:20],
        "extra_excluded_head": extra[:20],
    }


def _ts_partition(np, arrays, info, setup_by_shape):
    ts_names = list(info.get("ts_names") or [])
    face_n = int(arrays["face_vertex_counts"].size)
    owned = {}
    overlap = []
    for index, name in enumerate(ts_names):
        key = "ts_faces__{0}".format(index)
        ids = [int(i) for i in arrays[key].tolist()]
        for fid in ids:
            if fid in owned and owned[fid] != name:
                overlap.append({"face": fid, "a": owned[fid], "b": name})
            owned[fid] = name
        out_of_range = [fid for fid in ids if fid < 0 or fid >= face_n]
        if out_of_range:
            return False, {"out_of_range_head": out_of_range[:20], "ts": name}
    return not overlap, {
        "n_owned": len(owned),
        "n_overlap": len(overlap),
        "overlap_head": overlap[:12],
        "ts_names": ts_names,
        "setup": setup_by_shape,
    }


def run_maya_cases(cmds):
    import numpy as np  # type: ignore
    import uvremap_maya as um  # type: ignore

    new_scratch_scene(cmds)
    cube = cmds.polyCube(name="uvremap_arr_cube")[0]
    sphere = cmds.polySphere(name="uvremap_arr_sphere", sx=8, sy=6)[0]
    cube_info = _assign_two_materials(cmds, cube, unassign_last=True)
    sphere_info = _assign_two_materials(cmds, sphere, unassign_last=False)
    cube_shape = cube_info["shape"]
    sphere_shape = sphere_info["shape"]

    listed = um.list_texture_sets()
    default_entries = listed.get(INITIAL_SG) or []
    cube_default = None
    for entry in default_entries:
        if cube_shape in str(entry.get("shape") or "") or str(entry.get("shape") or "").endswith(
            cube_shape.split("|")[-1]
        ):
            cube_default = entry
            break
        if cube in str(entry.get("shape") or ""):
            cube_default = entry
            break
    if cube_default is None and default_entries:
        for entry in default_entries:
            if "cube" in str(entry.get("shape") or "").lower():
                cube_default = entry
                break

    ts_names = [cube_info["sg_a"], cube_info["sg_b"], INITIAL_SG]
    arrays, info = um.collect_arrays(
        [cube_shape, sphere_shape], ts_names, uv_set=None,
    )

    cons_ok, cons_obs = _array_consistency(np, arrays)
    range_ok, range_obs = _tri_in_range(np, arrays)
    cover_ok, cover_obs = _faces_covered(np, arrays, info)
    part_ok, part_obs = _ts_partition(np, arrays, info, {
        "cube": cube_info,
        "sphere": sphere_info,
    })

    no_uv_ids = list(info.get("no_uv_faces") or [])
    nan_ok = True
    nan_obs = {"n_no_uv_faces": len(no_uv_ids)}
    if no_uv_ids:
        counts = arrays["face_vertex_counts"]
        starts = np.zeros(counts.size, dtype=np.int64)
        if counts.size > 1:
            starts[1:] = np.cumsum(counts[:-1])
        for fid in no_uv_ids:
            n = int(counts[fid])
            sl = arrays["corner_uv"][int(starts[fid]):int(starts[fid]) + n]
            if not np.any(np.isnan(sl)):
                nan_ok = False
                nan_obs["face_without_nan"] = int(fid)
                break

    vid_before = np.array(arrays["corner_vid"], copy=True)
    uv_before = np.array(arrays["corner_uv"], copy=True)
    cmds.polyEditUV(cube + ".map[*]", u=0.07, v=-0.04)
    arrays_edit, _info_edit = um.collect_arrays(
        [cube_shape, sphere_shape], ts_names, uv_set=info.get("uv_set"),
    )
    uv_after = arrays_edit["corner_uv"]
    finite = np.isfinite(uv_before).all(axis=1) & np.isfinite(uv_after).all(axis=1)
    cube_faces = arrays["face_mesh"] == 0
    # コーナーがキューブのものか: face_mesh をコーナーへ展開
    corner_mesh = np.repeat(arrays["face_mesh"], arrays["face_vertex_counts"])
    cube_corners = (corner_mesh == 0) & finite
    uv_delta = uv_after - uv_before
    if np.any(cube_corners):
        mean_du = float(uv_delta[cube_corners, 0].mean())
        mean_dv = float(uv_delta[cube_corners, 1].mean())
        uv_moved = abs(mean_du - 0.07) < 0.02 and abs(mean_dv + 0.04) < 0.02
    else:
        mean_du = None
        mean_dv = None
        uv_moved = False
    vid_same_after_edit = bool(np.array_equal(arrays_edit["corner_vid"], vid_before))

    cmds.polyMapCut(cube + ".e[*]", ch=False)
    arrays_cut, _info_cut = um.collect_arrays(
        [cube_shape, sphere_shape], ts_names, uv_set=info.get("uv_set"),
    )
    vid_same_after_cut = bool(np.array_equal(arrays_cut["corner_vid"], vid_before))
    try:
        cmds.polyMapSew(cube + ".e[*]", ch=False)
        arrays_sew, _info_sew = um.collect_arrays(
            [cube_shape, sphere_shape], ts_names, uv_set=info.get("uv_set"),
        )
        vid_same_after_sew = bool(np.array_equal(arrays_sew["corner_vid"], vid_before))
        sew_error = None
    except Exception as exc:
        vid_same_after_sew = None
        sew_error = exception_observation(exc)

    inst = cmds.instance(cube, name="uvremap_arr_cube_inst")[0]
    arrays_inst, info_inst = um.collect_arrays(
        [cube_shape, sphere_shape], ts_names, uv_set=info.get("uv_set"),
    )
    has_mismatch = um.WARN_INSTANCE in (info_inst.get("warnings") or [])

    listed_flags = []
    for entry in default_entries:
        listed_flags.append({
            "shape": entry.get("shape"),
            "is_default_sg": entry.get("is_default_sg"),
            "surface_shader": entry.get("surface_shader"),
            "face_ids": entry.get("face_ids"),
            "uuid": entry.get("uuid"),
        })
    face_ids_ok = False
    if cube_default is not None:
        face_ids_ok = cube_info["unassigned_idx"] in (cube_default.get("face_ids") or [])
        default_flag_ok = bool(cube_default.get("is_default_sg") is True)
    else:
        default_flag_ok = False

    checks = {
        "list_texture_sets_default_sg": _pass_or_fail(
            bool(default_entries) and default_flag_ok and face_ids_ok,
            "未割当フェースが initialShadingGroup (is_default_sg) として出る",
            {
                "n_default_entries": len(default_entries),
                "entries": listed_flags,
                "cube_unassigned_idx": cube_info["unassigned_idx"],
                "face_ids_ok": face_ids_ok,
                "is_default_sg_ok": default_flag_ok,
            },
        ),
        "array_consistency": _pass_or_fail(
            cons_ok,
            "配列の形(総コーナー数・フェース数・三角形数)が整合する",
            cons_obs,
        ),
        "tri_corner_range": _pass_or_fail(
            range_ok,
            "tri_corner がフェース内ローカル範囲に収まる",
            range_obs,
        ),
        "faces_covered": _pass_or_fail(
            cover_ok,
            "除外以外の全フェースが三角形でカバーされる",
            cover_obs,
        ),
        "ts_faces": _pass_or_fail(
            part_ok and um.WARN_DEFAULT_SG in (info.get("warnings") or []),
            "ts_faces が TS のフェース分割になり、default SG は除外しない",
            {
                "partition": part_obs,
                "warnings": info.get("warnings"),
                "ts_names": info.get("ts_names"),
            },
        ),
        "no_uv_nan": _pass_or_fail(
            nan_ok,
            "UV 無しフェースの corner_uv に NaN がある",
            nan_obs,
        ),
        "uv_edit_reflected": _pass_or_fail(
            uv_moved and vid_same_after_edit,
            "polyEditUV の移動が corner_uv に反映され corner_vid は不変",
            {
                "mean_du": mean_du,
                "mean_dv": mean_dv,
                "n_cube_corners": int(np.count_nonzero(cube_corners)),
                "vid_same": vid_same_after_edit,
            },
        ),
        "corner_vid_stable": _pass_or_fail(
            vid_same_after_cut and (vid_same_after_sew is not False),
            "UV 切断・縫合でも corner_vid が不変",
            {
                "after_cut": vid_same_after_cut,
                "after_sew": vid_same_after_sew,
                "sew_error": sew_error,
            },
        ),
        "instance_mismatch": _pass_or_fail(
            has_mismatch,
            "インスタンス作成後に warnings へ instance_mismatch が入る",
            {
                "instance": inst,
                "warnings": info_inst.get("warnings"),
            },
        ),
    }
    extra = {
        "info": {
            "uv_set": info.get("uv_set"),
            "mesh_uuids": info.get("mesh_uuids"),
            "warnings": info.get("warnings"),
            "bbox_min": info.get("bbox_min"),
            "bbox_max": info.get("bbox_max"),
            "ts_names": info.get("ts_names"),
        },
        "list_keys": sorted(listed.keys()),
        "n_listed_ts": len(listed),
    }
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
        payload = write_results(default_results_path(), checks, env=collect_env(), extra={"runner": "verify_maya_arrays"})
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
