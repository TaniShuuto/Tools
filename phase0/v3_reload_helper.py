# -*- coding: utf-8 -*-
"""V3 補助: reload_mesh を preserve_strokes=True で呼び、status と例外を残す。

利用者の既存プロジェクトでは実行しないこと。テスト用プロジェクトだけ。
実行するには下の PHASE0_ALLOW_RELOAD を True にし、MESH_FBX に
UVを変えたテスト用 FBX のパスを書く。

auto_unwrap_settings が無い環境では hasattr / signature で分岐する。
reload_mesh は非同期。コールバックの status をログと結果JSONに残す。

根拠:
  https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/project
  MeshReloadingSettings(auto_unwrap_settings=None) なら auto-unwrap は無効。
"""
from __future__ import annotations

import inspect
import json
import os
import sys
from datetime import datetime, timezone

PHASE0_DIR_OVERRIDE = ""
OUTPUT_JSON_OVERRIDE = ""

# テスト用プロジェクトでのみ True にする。既定は拒否。
PHASE0_ALLOW_RELOAD = False
# UV を変更したテスト用 FBX。空なら実行しない。
MESH_FBX = ""
# プロジェクト名に含まれるべき印(どれか)。既存プロジェクト誤爆防止。
PROJECT_NAME_MARKERS = ("phase0", "v3_test", "uvremap_test")


def _try_common():
    here = PHASE0_DIR_OVERRIDE or (
        os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else None
    )
    if here and here not in sys.path:
        sys.path.insert(0, here)
    try:
        import common as c  # noqa: WPS433
        return c
    except Exception:
        return None


def _obs_exc(exc):
    return {"type": type(exc).__name__, "message": str(exc)}


def _project_looks_like_test(project):
    path = None
    try:
        if hasattr(project, "file_path"):
            path = project.file_path()
    except Exception as exc:
        return False, {"file_path_error": _obs_exc(exc)}
    if not path:
        # 未保存。テスト用の新規なら許可してよいが、印が無いので拒否。
        return False, {"path": None, "reason": "unsaved_project_refused"}
    name = os.path.basename(str(path)).lower()
    ok = any(m in name for m in PROJECT_NAME_MARKERS)
    return ok, {"path_basename": name, "matched": ok}


def run_reload():
    obs = {
        "allow_flag": PHASE0_ALLOW_RELOAD,
        "mesh_fbx_set": bool(MESH_FBX),
        "settings": None,
        "call": None,
        "callback": [],
        "docs": [
            "https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/project",
        ],
    }
    if not PHASE0_ALLOW_RELOAD:
        return {
            "status": "SKIPPED",
            "summary": "PHASE0_ALLOW_RELOAD が False。既存プロジェクト保護のため何もしない",
            "observations": obs,
        }
    if not MESH_FBX:
        return {
            "status": "INCONCLUSIVE",
            "summary": "MESH_FBX が空",
            "observations": obs,
        }
    if not os.path.isfile(MESH_FBX):
        obs["mesh_missing"] = True
        return {
            "status": "FAIL",
            "summary": "MESH_FBX が存在しない",
            "observations": obs,
        }

    try:
        import substance_painter.project as project
    except Exception as exc:
        obs["import_error"] = _obs_exc(exc)
        return {
            "status": "INCONCLUSIVE",
            "summary": "substance_painter を import できない",
            "observations": obs,
        }

    try:
        opened = project.is_open() if hasattr(project, "is_open") else None
    except Exception as exc:
        opened = None
        obs["is_open_error"] = _obs_exc(exc)
    obs["project_open"] = opened
    if opened is False:
        return {
            "status": "INCONCLUSIVE",
            "summary": "プロジェクトが開いていない",
            "observations": obs,
        }

    test_ok, test_info = _project_looks_like_test(project)
    obs["test_project_gate"] = test_info
    if not test_ok:
        return {
            "status": "SKIPPED",
            "summary": "プロジェクト名に phase0/v3_test/uvremap_test が無いため拒否",
            "observations": obs,
        }

    if not hasattr(project, "MeshReloadingSettings") or not hasattr(project, "reload_mesh"):
        obs["missing"] = {
            "MeshReloadingSettings": hasattr(project, "MeshReloadingSettings"),
            "reload_mesh": hasattr(project, "reload_mesh"),
        }
        return {
            "status": "INCONCLUSIVE",
            "summary": "reload_mesh または MeshReloadingSettings が dir に無い",
            "observations": obs,
        }

    cls = project.MeshReloadingSettings
    kwargs = {}
    try:
        sig = inspect.signature(cls)
        params = sig.parameters
    except Exception as exc:
        sig = None
        params = {}
        obs["signature_error"] = _obs_exc(exc)

    def _maybe(name, value):
        if name in params:
            kwargs[name] = value
            return True
        return hasattr(cls, name)

    used = {}
    used["preserve_strokes"] = _maybe("preserve_strokes", True)
    if used["preserve_strokes"]:
        kwargs["preserve_strokes"] = True
    used["import_cameras"] = _maybe("import_cameras", False)
    if "import_cameras" in params:
        kwargs["import_cameras"] = False
    used["auto_unwrap_settings"] = "auto_unwrap_settings" in params or hasattr(cls, "auto_unwrap_settings")
    if "auto_unwrap_settings" in params:
        kwargs["auto_unwrap_settings"] = None

    obs["settings_kwargs"] = {k: (None if v is None else v) for k, v in kwargs.items()}
    obs["settings_used_fields"] = used
    obs["settings_signature"] = str(sig) if sig is not None else None

    try:
        settings = cls(**kwargs)
    except Exception as exc:
        obs["settings_error"] = _obs_exc(exc)
        return {
            "status": "INCONCLUSIVE",
            "summary": "MeshReloadingSettings の生成に失敗",
            "observations": obs,
        }
    obs["settings_type"] = type(settings).__name__
    if hasattr(settings, "auto_unwrap_settings"):
        try:
            obs["auto_unwrap_settings_value"] = repr(settings.auto_unwrap_settings)
        except Exception as exc:
            obs["auto_unwrap_settings_value"] = _obs_exc(exc)

    def on_status(status):
        row = {"raw": repr(status)}
        for attr in ("name", "value"):
            if hasattr(status, attr):
                try:
                    row[attr] = getattr(status, attr)
                except Exception as exc:
                    row[attr + "_error"] = _obs_exc(exc)
        obs["callback"].append(row)
        print("[V3] reload_mesh status: {0}".format(row))

    try:
        project.reload_mesh(MESH_FBX, settings, on_status)
        obs["call"] = {"ok": True, "async": True}
    except Exception as exc:
        obs["call"] = {"ok": False, "error": _obs_exc(exc)}
        return {
            "status": "FAIL",
            "summary": "reload_mesh が例外を投げた",
            "observations": obs,
        }
    return {
        "status": "MANUAL",
        "summary": "reload_mesh を呼んだ。コールバックと UV/ペイントの目視は v3_reload_protocol.md",
        "observations": obs,
    }


def _write(check):
    common = _try_common()
    checks = {"V3": check}
    if common is not None:
        phase0 = common.resolve_phase0_dir(PHASE0_DIR_OVERRIDE)
        path = common.default_results_path("painter", phase0, OUTPUT_JSON_OVERRIDE)
        payload = common.write_results(path, checks, env=common.collect_env())
        common.print_results(payload)
        return payload
    print(json.dumps({"schema": 1, "checks": checks}, ensure_ascii=False, indent=2))
    return checks


def start_plugin():
    _write(run_reload())


def close_plugin():
    return None


def main():
    _write(run_reload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
