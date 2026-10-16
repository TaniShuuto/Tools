# -*- coding: utf-8 -*-
"""V2: Substance 3D Painter Python API の実在確認。

Painter の Python コンソール、または python/plugins に置いて有効化して実行する。
1ファイルで完結。未確認の API 名は使わず、dir / inspect / getattr で確認してから触る。

根拠(公式):
- project モジュール / MeshReloadingSettings / reload_mesh / ReloadMeshStatus:
  https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/project
- application.version / version_info:
  https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/application
- プラグインの置き場と Python メニューからの有効化:
  https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/tutorials/create-a-python-plugin
"""
from __future__ import annotations

import inspect
import json
import os
import sys
from datetime import datetime, timezone

# コンソールから exec した場合は、phase0 フォルダをここに書く。
PHASE0_DIR_OVERRIDE = ""
OUTPUT_JSON_OVERRIDE = ""

# ---------------------------------------------------------------------------
# 最小の結果書き出し(common.py が読めればそちらを使う)
# ---------------------------------------------------------------------------

def _try_common():
    here = None
    if PHASE0_DIR_OVERRIDE:
        here = PHASE0_DIR_OVERRIDE
    elif "__file__" in globals():
        here = os.path.dirname(os.path.abspath(__file__))
    if here and here not in sys.path:
        sys.path.insert(0, here)
    try:
        import common as c  # noqa: WPS433
        return c
    except Exception:
        return None


def _safe_dir(obj):
    try:
        return sorted(str(n) for n in dir(obj))
    except Exception as exc:
        return {"_dir_error": type(exc).__name__, "message": str(exc)}


def _safe_sig(obj):
    try:
        return str(inspect.signature(obj))
    except Exception as exc:
        return {"_signature_error": type(exc).__name__, "message": str(exc)}


def _safe_getattr_value(obj, name):
    try:
        value = getattr(obj, name)
    except Exception as exc:
        return {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    return {"ok": True, "value": _brief(value)}


def _brief(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    name = getattr(value, "name", None)
    raw = getattr(value, "value", None)
    if name is not None and raw is not None and not callable(name):
        return {"name": str(name), "value": raw, "type": type(value).__name__}
    if isinstance(value, type):
        return {"_class": value.__name__, "module": getattr(value, "__module__", None)}
    if callable(value):
        return {"_callable": getattr(value, "__name__", str(value)), "signature": _safe_sig(value)}
    try:
        return repr(value)
    except Exception as exc:
        return {"_repr_error": type(exc).__name__, "message": str(exc)}


def _enum_members(cls):
    out = []
    names = _safe_dir(cls)
    if isinstance(names, dict):
        return names
    for name in names:
        if name.startswith("_"):
            continue
        try:
            member = getattr(cls, name)
        except Exception as exc:
            out.append({"name": name, "error": {"type": type(exc).__name__, "message": str(exc)}})
            continue
        if member is cls:
            continue
        item = {"name": name, "type": type(member).__name__}
        if hasattr(member, "value") and not callable(getattr(member, "value", None)):
            try:
                item["value"] = member.value
            except Exception as exc:
                item["value_error"] = {"type": type(exc).__name__, "message": str(exc)}
        out.append(item)
    return out


def inspect_painter():
    obs = {
        "docs": [
            "https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/project",
            "https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/application",
        ],
        "substance_painter": None,
        "version_candidates": {},
        "project_public_names": None,
        "MeshReloadingSettings": None,
        "reload_mesh": None,
        "ReloadMeshStatus": None,
        "auto_unwrap_settings": None,
    }
    try:
        import substance_painter
    except Exception as exc:
        obs["import_error"] = {"type": type(exc).__name__, "message": str(exc)}
        return obs

    obs["substance_painter"] = {
        "dir": _safe_dir(substance_painter),
        "file": getattr(substance_painter, "__file__", None),
        "package": getattr(substance_painter, "__package__", None),
    }
    # 版情報を取れる手段を dir で探す。公式は application.version / version_info。
    # __version__ は公式ページで確認していないので、dir に出たときだけ読む。
    names = obs["substance_painter"]["dir"]
    if isinstance(names, list) and "__version__" in names:
        obs["version_candidates"]["substance_painter.__version__"] = _safe_getattr_value(
            substance_painter, "__version__"
        )

    if "application" in (names or []):
        try:
            import substance_painter.application as app
            app_names = _safe_dir(app)
            obs["version_candidates"]["application.dir"] = app_names
            if isinstance(app_names, list):
                if "version" in app_names:
                    ver = getattr(app, "version")
                    if callable(ver):
                        try:
                            obs["version_candidates"]["application.version()"] = ver()
                        except Exception as exc:
                            obs["version_candidates"]["application.version()"] = {
                                "error": {"type": type(exc).__name__, "message": str(exc)}
                            }
                    else:
                        obs["version_candidates"]["application.version"] = _brief(ver)
                if "version_info" in app_names:
                    vi = getattr(app, "version_info")
                    if callable(vi):
                        try:
                            obs["version_candidates"]["application.version_info()"] = list(vi())
                        except Exception as exc:
                            obs["version_candidates"]["application.version_info()"] = {
                                "error": {"type": type(exc).__name__, "message": str(exc)}
                            }
        except Exception as exc:
            obs["version_candidates"]["application_error"] = {
                "type": type(exc).__name__, "message": str(exc)
            }

    try:
        import substance_painter.project as project
    except Exception as exc:
        obs["project_import_error"] = {"type": type(exc).__name__, "message": str(exc)}
        return obs

    obs["project_public_names"] = _safe_dir(project)
    proj_names = obs["project_public_names"] if isinstance(obs["project_public_names"], list) else []

    if "MeshReloadingSettings" in proj_names:
        cls = getattr(project, "MeshReloadingSettings")
        attrs = _safe_dir(cls)
        fields = []
        if isinstance(attrs, list):
            for name in attrs:
                if name.startswith("_"):
                    continue
                fields.append(name)
        init_sig = _safe_sig(cls)
        has_auto = False
        if isinstance(attrs, list) and "auto_unwrap_settings" in attrs:
            has_auto = True
        if isinstance(init_sig, str) and "auto_unwrap_settings" in init_sig:
            has_auto = True
        obs["MeshReloadingSettings"] = {
            "type": type(cls).__name__,
            "dir": attrs,
            "public_names": fields,
            "signature": init_sig,
            "doc_present": bool(getattr(cls, "__doc__", None)),
        }
        obs["auto_unwrap_settings"] = {
            "present_on_class_or_signature": has_auto,
            "hasattr_instance": None,
        }
        # インスタンスをデフォルト引数で作れるか(プロジェクトは変更しない)
        try:
            inst = cls()
            obs["auto_unwrap_settings"]["hasattr_instance"] = hasattr(inst, "auto_unwrap_settings")
            if hasattr(inst, "auto_unwrap_settings"):
                try:
                    obs["auto_unwrap_settings"]["instance_value"] = _brief(inst.auto_unwrap_settings)
                except Exception as exc:
                    obs["auto_unwrap_settings"]["instance_value_error"] = {
                        "type": type(exc).__name__, "message": str(exc)
                    }
        except Exception as exc:
            obs["MeshReloadingSettings"]["ctor_error"] = {
                "type": type(exc).__name__, "message": str(exc)
            }
    else:
        obs["MeshReloadingSettings"] = {"present": False}

    if "reload_mesh" in proj_names:
        fn = getattr(project, "reload_mesh")
        obs["reload_mesh"] = {
            "present": True,
            "signature": _safe_sig(fn),
            "doc_head": (fn.__doc__ or "")[:400],
        }
    else:
        obs["reload_mesh"] = {"present": False}

    if "ReloadMeshStatus" in proj_names:
        cls = getattr(project, "ReloadMeshStatus")
        obs["ReloadMeshStatus"] = {
            "present": True,
            "dir": _safe_dir(cls),
            "members": _enum_members(cls),
        }
    else:
        obs["ReloadMeshStatus"] = {"present": False}

    return obs


def run_check():
    obs = inspect_painter()
    if obs.get("import_error"):
        return {
            "status": "INCONCLUSIVE",
            "summary": "substance_painter を import できない(Painter 外で実行した可能性)",
            "observations": obs,
        }
    have = []
    if (obs.get("MeshReloadingSettings") or {}).get("public_names") or (
        obs.get("MeshReloadingSettings") or {}
    ).get("signature"):
        have.append("MeshReloadingSettings")
    if (obs.get("reload_mesh") or {}).get("present"):
        have.append("reload_mesh")
    if (obs.get("ReloadMeshStatus") or {}).get("present"):
        have.append("ReloadMeshStatus")
    if have:
        return {
            "status": "PASS",
            "summary": "dir/inspect で確認できた: {0}".format(", ".join(have)),
            "observations": obs,
        }
    return {
        "status": "INCONCLUSIVE",
        "summary": "project は読めたが対象名を dir で確認できなかった",
        "observations": obs,
    }


def _write(check):
    common = _try_common()
    checks = {"V2": check}
    if common is not None:
        phase0 = common.resolve_phase0_dir(PHASE0_DIR_OVERRIDE)
        path = common.default_results_path("painter", phase0, OUTPUT_JSON_OVERRIDE)
        payload = common.write_results(path, checks, env=common.collect_env())
        common.print_results(payload)
        return payload
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    path = OUTPUT_JSON_OVERRIDE or os.path.join(tmp, "phase0_results_painter.json")
    payload = {
        "schema": 1,
        "env": {"note": "common.py 未使用のフォールバック"},
        "checks": checks,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        payload["path"] = path
    except Exception as exc:
        payload["write_error"] = {"type": type(exc).__name__, "message": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def start_plugin():
    """Painter プラグインとして有効化したときに走る。"""
    _write(run_check())


def close_plugin():
    return None


def main():
    _write(run_check())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
