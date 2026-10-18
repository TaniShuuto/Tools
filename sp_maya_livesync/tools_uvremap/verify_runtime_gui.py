# -*- coding: utf-8 -*-
"""Maya GUI の Script Editor から mayapy 子プロセス起動を確認する。

シーンは触らない(cmds を呼ばない)。未実行でよい(実機必須)。

使い方:
1. Maya GUI を開き、Script Editor の Python タブを使う。
2. モジュールの通し方:
   - このファイルをディスクから実行できる場合(__file__ がある)、
     親フォルダ(sp_maya_livesync。uvremap_runtime.py がある場所)を
     sys.path に足す。
   - 中身を Script Editor に貼り付けて実行する場合は、先に次を実行する:
       import sys
       sys.path.insert(0, r"<sp_maya_livesync のフォルダ>")
     フォルダは uvremap_runtime.py がある場所を指定する。
     ホームの絶対パスやユーザー名を、結果 JSON に残さないこと。
3. この脚本を実行する。出力は phase0 と同じ
   checks → status / summary / observations の JSON。
4. mayapy は uvremap_runtime.resolve_mayapy() が
   sys.executable の隣の mayapy.exe(非 Windows は mayapy)を探す。
   見つからないときは observations に理由だけを書く。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SCHEMA = 1
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CHILD_CODE = "import sys, zlib, numpy; print(sys.version.split()[0], numpy.__version__)"


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


def _ensure_pkg_path():
    here = None
    if "__file__" in globals():
        try:
            here = os.path.dirname(os.path.abspath(__file__))
        except Exception:
            here = None
    if here:
        pkg = os.path.dirname(here)
        if pkg and pkg not in sys.path:
            sys.path.insert(0, pkg)
        if here not in sys.path:
            sys.path.insert(0, here)


def _make_check(status, summary, observations=None):
    return {
        "status": status,
        "summary": anonymize_text(summary),
        "observations": observations or {},
    }


def run():
    _ensure_pkg_path()
    obs = {
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "os_name": os.name,
        "executable": anonymize_path(sys.executable),
    }
    try:
        import uvremap_runtime as urm
    except Exception as exc:
        payload = {
            "schema": SCHEMA,
            "checks": {
                "mayapy_child": _make_check(
                    STATUS_INCONCLUSIVE,
                    "uvremap_runtime を import できない。sys.path に sp_maya_livesync を通す",
                    {"error_type": type(exc).__name__, "error": anonymize_text(str(exc))},
                ),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    mayapy = urm.resolve_mayapy()
    obs["mayapy"] = anonymize_path(mayapy) if mayapy else None
    if not mayapy:
        payload = {
            "schema": SCHEMA,
            "checks": {
                "mayapy_child": _make_check(
                    STATUS_INCONCLUSIVE,
                    "mayapy が見つからない(sys.executable の隣を探した)",
                    obs,
                ),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    env = urm.worker_env()
    obs["pythonhome_stripped"] = "PYTHONHOME" not in env
    obs["pythonpath_stripped"] = "PYTHONPATH" not in env
    obs["pythonnousersite"] = env.get("PYTHONNOUSERSITE") == "1"
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = urm.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen([mayapy, "-c", _CHILD_CODE], **kwargs)
        raw, _unused = proc.communicate(timeout=30)
        code = proc.returncode
    except Exception as exc:
        obs["error_type"] = type(exc).__name__
        obs["error"] = anonymize_text(str(exc))
        payload = {
            "schema": SCHEMA,
            "checks": {
                "mayapy_child": _make_check(
                    STATUS_FAIL,
                    "mayapy 子プロセスの起動または待ちに失敗",
                    obs,
                ),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    text = raw or b""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = (text or "").strip()
    obs["returncode"] = code
    obs["stdout"] = anonymize_text(text[:200])
    parts = text.split()
    ok = code == 0 and len(parts) >= 2
    payload = {
        "schema": SCHEMA,
        "checks": {
            "mayapy_child": _make_check(
                STATUS_PASS if ok else STATUS_FAIL,
                "mayapy が zlib と numpy を読み、版を返した" if ok else "mayapy 子プロセスが失敗した",
                obs,
            ),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


# ファイル実行でも Script Editor への貼り付けでも、この1回だけ走る。
run()
