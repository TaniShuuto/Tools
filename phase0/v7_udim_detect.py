# -*- coding: utf-8 -*-
"""V7: Painter でプロジェクトが UDIM(UV Tile) か判定できる API を dir で探す。

実在を確認したものだけを報告する。公式で確認した候補:
- ProjectWorkflow (create 時の設定。現行プロジェクトの getter は公式ページに無い)
  https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/project
- TextureSet.has_uv_tiles / all_uv_tiles / uv_tile
  https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/textureset-module/textureset

見つからない場合の代替はテクスチャセット情報からの推定のみを試み、
確認できなければ INCONCLUSIVE。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

PHASE0_DIR_OVERRIDE = ""
OUTPUT_JSON_OVERRIDE = ""

DOC_URLS = [
    "https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/project",
    "https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/textureset-module/textureset",
]

WORKFLOW_HINTS = (
    "workflow", "udim", "uv_tile", "uvtile", "project_workflow", "ProjectWorkflow",
)


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


def _safe_dir(obj):
    try:
        return sorted(str(n) for n in dir(obj))
    except Exception as exc:
        return {"_dir_error": type(exc).__name__, "message": str(exc)}


def _safe_sig(obj):
    try:
        import inspect
        return str(inspect.signature(obj))
    except Exception as exc:
        return {"_signature_error": type(exc).__name__, "message": str(exc)}


def _hint_names(names):
    if not isinstance(names, list):
        return []
    hits = []
    for name in names:
        low = name.lower()
        if any(h.lower() in low for h in WORKFLOW_HINTS):
            hits.append(name)
    return hits


def inspect_udim_api():
    obs = {
        "docs": DOC_URLS,
        "project_public_names": None,
        "project_hint_names": [],
        "project_workflow_enum": None,
        "project_getter_candidates": [],
        "textureset_public_names": None,
        "textureset_class_names": None,
        "open_project": None,
        "texture_set_estimate": None,
        "documented_getter_for_current_workflow": False,
    }
    try:
        import substance_painter.project as project
    except Exception as exc:
        obs["import_error"] = {"type": type(exc).__name__, "message": str(exc)}
        return obs

    names = _safe_dir(project)
    obs["project_public_names"] = names
    obs["project_hint_names"] = _hint_names(names)

    if isinstance(names, list) and "ProjectWorkflow" in names:
        cls = getattr(project, "ProjectWorkflow")
        members = []
        for name in _safe_dir(cls) if isinstance(_safe_dir(cls), list) else []:
            if name.startswith("_"):
                continue
            try:
                member = getattr(cls, name)
                item = {"name": name}
                if hasattr(member, "value") and not callable(member.value):
                    item["value"] = member.value
                members.append(item)
            except Exception as exc:
                members.append({"name": name, "error": type(exc).__name__})
        obs["project_workflow_enum"] = {"present": True, "members": members}

    # 現行プロジェクトの workflow を返す公開名があるか。公式 getter は未記載。
    # dir に出た名前だけを、呼び出し副作用の小さそうな query 系に限って試す。
    query_like = []
    if isinstance(names, list):
        for name in names:
            low = name.lower()
            if "workflow" in low and not low.startswith("_"):
                query_like.append(name)
    for name in query_like:
        obj = getattr(project, name, None)
        row = {"name": name, "callable": callable(obj), "signature": None, "called": False}
        if callable(obj):
            row["signature"] = _safe_sig(obj)
            # 引数無しだけ試す(プロジェクトを変更しない)
            sig = row["signature"]
            can_call = isinstance(sig, str) and sig in ("()", "(self)") or (
                isinstance(sig, str) and "(" in sig and "mesh" not in sig.lower()
                and "path" not in sig.lower() and "create" not in name.lower()
                and "save" not in name.lower() and "close" not in name.lower()
                and "reload" not in name.lower() and "open" not in name.lower()
            )
            if can_call and name.lower() not in ("settings",):
                try:
                    # Settings コンストラクタ等は呼ばない
                    if name[:1].isupper():
                        row["skipped"] = "type_or_enum"
                    else:
                        row["called"] = True
                        row["value"] = repr(obj())
                except Exception as exc:
                    row["called"] = True
                    row["error"] = {"type": type(exc).__name__, "message": str(exc)}
        else:
            row["value"] = repr(obj)
        obs["project_getter_candidates"].append(row)

    try:
        import substance_painter.textureset as textureset
        ts_names = _safe_dir(textureset)
        obs["textureset_public_names"] = ts_names
        if isinstance(ts_names, list) and "TextureSet" in ts_names:
            cls = getattr(textureset, "TextureSet")
            cls_dir = _safe_dir(cls)
            obs["textureset_class_names"] = cls_dir
            obs["textureset_uv_helpers"] = _hint_names(cls_dir) if isinstance(cls_dir, list) else []
    except Exception as exc:
        obs["textureset_error"] = {"type": type(exc).__name__, "message": str(exc)}

    is_open = None
    if isinstance(names, list) and "is_open" in names:
        try:
            is_open = bool(project.is_open())
        except Exception as exc:
            obs["is_open_error"] = {"type": type(exc).__name__, "message": str(exc)}
    obs["open_project"] = is_open

    # 代替: 開いているプロジェクトのテクスチャセットから推定
    if is_open:
        estimate = {"used": False, "sets": []}
        try:
            import substance_painter.textureset as textureset
            all_fn = getattr(textureset, "all_texture_sets", None)
            if callable(all_fn):
                estimate["used"] = True
                for ts in all_fn():
                    row = {"type": type(ts).__name__}
                    for meth in ("name", "has_uv_tiles", "all_uv_tiles"):
                        if hasattr(ts, meth):
                            attr = getattr(ts, meth)
                            try:
                                row[meth] = attr() if callable(attr) else attr
                            except Exception as exc:
                                row[meth] = {"error": type(exc).__name__, "message": str(exc)}
                    tiles = row.get("all_uv_tiles")
                    if isinstance(tiles, list):
                        brief = []
                        for tile in tiles[:16]:
                            brief.append({
                                "u": getattr(tile, "u", None),
                                "v": getattr(tile, "v", None),
                            })
                        row["all_uv_tiles"] = brief
                        row["n_uv_tiles"] = len(tiles)
                    estimate["sets"].append(row)
        except Exception as exc:
            estimate["error"] = {"type": type(exc).__name__, "message": str(exc)}
        obs["texture_set_estimate"] = estimate
    return obs


def run_check():
    obs = inspect_udim_api()
    if obs.get("import_error"):
        return {
            "status": "INCONCLUSIVE",
            "summary": "substance_painter を import できない(Painter 外で実行した可能性)",
            "observations": obs,
        }
    has_enum = bool(obs.get("project_workflow_enum"))
    getters = [
        g for g in obs.get("project_getter_candidates") or []
        if g.get("called") and "error" not in g and g.get("value")
    ]
    estimate = obs.get("texture_set_estimate") or {}
    has_ts = bool(estimate.get("sets"))
    if getters:
        return {
            "status": "PASS",
            "summary": "project 上で workflow 系の呼び出しに成功した",
            "observations": obs,
        }
    if has_enum or has_ts:
        return {
            "status": "INCONCLUSIVE",
            "summary": (
                "ProjectWorkflow または TextureSet.has_uv_tiles は確認した。"
                "現行プロジェクト workflow の公式 getter は見つからず、代替推定のみ"
            ),
            "observations": obs,
        }
    return {
        "status": "INCONCLUSIVE",
        "summary": "UDIM 判定に使える公開名を確定できなかった",
        "observations": obs,
    }


def _write(check):
    common = _try_common()
    checks = {"V7": check}
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
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except Exception:
        pass
    return payload


def start_plugin():
    _write(run_check())


def close_plugin():
    return None


def main():
    _write(run_check())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
