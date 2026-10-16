# -*- coding: utf-8 -*-
"""Phase 0 共通処理。標準ライブラリのみ。

結果JSONの書き込み、パス/文字列の匿名化、環境情報、
安全な getattr/dir/inspect。個人情報(ホーム絶対パス、ユーザー名、
メール、認証情報)を結果に残さない。
"""
from __future__ import annotations

import inspect
import json
import os
import platform
import re
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

SCHEMA = 1

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"
STATUS_MANUAL = "MANUAL"
STATUS_SKIPPED = "SKIPPED"

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SENSITIVE_ENV = {
    "HOME",
    "USER",
    "USERNAME",
    "USERPROFILE",
    "HOMEPATH",
    "HOMEDRIVE",
    "LOGNAME",
    "MAIL",
    "EMAIL",
    "USEREMAIL",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_EMAIL",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_NAME",
}


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
    # 長いパスを先に置換する
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


def anonymize_path(path):
    """ホームを ~ に置換し、残ったユーザー名も隠す。"""
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


def anonymize_text(text):
    """例外メッセージ等からホーム・ユーザー名・メールを除く。"""
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


def safe_getattr(obj, name, default=None):
    try:
        return getattr(obj, name)
    except Exception as exc:
        return {"_getattr_error": exception_observation(exc), "name": name, "default": default}


def safe_dir(obj):
    try:
        names = dir(obj)
        return sorted(str(n) for n in names)
    except Exception as exc:
        return {"_dir_error": exception_observation(exc)}


def safe_signature(obj):
    try:
        return str(inspect.signature(obj))
    except Exception as exc:
        return {"_signature_error": exception_observation(exc)}


def safe_call(fn, *args, **kwargs):
    try:
        return {"ok": True, "value": fn(*args, **kwargs)}
    except Exception as exc:
        return {"ok": False, "error": exception_observation(exc)}


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
    """結果JSONに載せられる形へ落とす。推測で埋めない。"""
    if depth > 6:
        return {"_truncated": True, "type": type(value).__name__}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return anonymize_text(value)
    if isinstance(value, bytes):
        return {"_bytes_hex": value[:64].hex(), "n": len(value)}
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[str(key)] = json_safe(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item, depth + 1) for item in list(value)[:200]]
    if isinstance(value, type):
        return {"_class": getattr(value, "__name__", str(value)),
                "module": getattr(value, "__module__", None)}
    # Enum など
    name = getattr(value, "name", None)
    raw = getattr(value, "value", None)
    if name is not None and raw is not None and not callable(name):
        return {"name": str(name), "value": json_safe(raw, depth + 1),
                "type": type(value).__name__}
    try:
        return anonymize_text(repr(value))
    except Exception as exc:
        return {"_repr_error": exception_observation(exc), "type": type(value).__name__}


def probe_module(name):
    """import して版を取る。無ければ installed=False。インストールはしない。"""
    info = {"name": name, "installed": False, "version": None, "error": None}
    try:
        module = __import__(name)
    except Exception as exc:
        info["error"] = exception_observation(exc)
        return info
    info["installed"] = True
    version = None
    for attr in ("__version__", "VERSION", "version"):
        if hasattr(module, attr):
            raw = safe_getattr(module, attr)
            if not (isinstance(raw, dict) and "_getattr_error" in raw):
                version = raw
                break
    if callable(version):
        called = safe_call(version)
        version = called.get("value") if called.get("ok") else called
    info["version"] = json_safe(version)
    info["module"] = getattr(module, "__name__", name)
    return info


def collect_env():
    exe = anonymize_path(sys.executable)
    cwd = anonymize_path(os.getcwd())
    return {
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "python_build": list(platform.python_build()),
        "platform": sys.platform,
        "os_name": os.name,
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "executable": exe,
        "cwd": cwd,
        "argv0": anonymize_path(sys.argv[0]) if sys.argv else None,
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


def resolve_phase0_dir(override=""):
    if override:
        return os.path.abspath(override)
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return None


def default_results_path(kind, phase0_dir=None, override=""):
    """kind は maya / painter / misc。"""
    if override:
        return os.path.abspath(override)
    name = "phase0_results_{0}.json".format(kind)
    if phase0_dir:
        dest_dir = os.path.join(phase0_dir, "results")
        return os.path.join(dest_dir, name)
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or os.environ.get("TMPDIR")
    if not tmp:
        tmp = os.path.abspath(".")
    return os.path.join(tmp, name)


def write_results(path, checks, env=None, extra=None):
    """既存JSONがあれば checks をマージする。パスは匿名化して書く。"""
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    existing = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except Exception:
            existing = {}

    merged = {}
    if isinstance(existing.get("checks"), dict):
        merged.update(existing["checks"])
    merged.update(checks)

    payload = {
        "schema": SCHEMA,
        "env": json_safe(env or collect_env()),
        "checks": json_safe(merged),
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
    """コンソールへJSONを出す(実機でコピーして指揮役へ渡す用)。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def module_present(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def try_import_maya():
    """maya.cmds を取る。未初期化なら standalone を試みる。

    戻り値: (cmds, meta)
    meta.initialized_standalone が True なら呼び出し側で uninitialize 可。
    失敗しても例外は投げず meta.error に残す。
    """
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
    """利用者の既存シーンを触らない。空の未保存シーンか batch のみ許可。"""
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
