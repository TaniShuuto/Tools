"""UV 投影フィルの列挙・切り替え・巻き戻し。Painter の API は引数で受ける
(Painter / Qt に依存しない)。

sp_live_sync_plugin 1.4.2 の __init__.py から、内容を変えずに移した部分。"""

import unicodedata
from collections import namedtuple

from ._rebase_protocol import REBASE_DIRECTION_WARNING_FMT, _rebase_clip_message

__all__ = [
    "_UV_FILL_PARAM_ATTRS",
    "_UV_FILL_GATE_MESSAGES",
    "UvFillApi",
    "node_type_token",
    "_safe_call",
    "uv_fill_has_direction_channel",
    "_uv_fill_direction_state",
    "_direction_fill_name",
    "inspect_direction_uv_fills",
    "_iter_named_children",
    "_node_has_projection_api",
    "_uv_fill_visit_key",
    "iter_uv_fill_nodes",
    "switch_uv_fill_node",
    "rollback_uv_fill_node",
    "UvFillStackReport",
    "_remember_missing_attrs",
    "switch_uv_fills_in_stack",
    "UvFillSummary",
    "_texture_set_name",
    "_texture_set_key",
    "_iter_texture_set_stacks",
    "switch_uv_fills_for_project",
]


_UV_FILL_PARAM_ATTRS = ("filtering_mode", "uv_wrapping_mode", "uv_transformation")
_UV_FILL_GATE_MESSAGES = {
    "disabled_by_setting": "UV 投影のフィル切り替えは設定で無効です",
    "no_api": "UV 投影フィルの API が使えません",
    "uvold_absent": "依頼に旧 UV セットが無いため、UV 投影のフィルは切り替えていません",
    "no_matching_texture_set": "依頼のテクスチャセットに一致するものがありません",
}


UvFillApi = namedtuple(
    "UvFillApi",
    [
        "get_root_layer_nodes",
        "ProjectionMode",
        "UVSetToUVSetProjectionParams",
        "direction_channel_types",
    ],
)


def node_type_token(node):
    try:
        raw = node.get_type()
    except Exception:
        return ""
    name = getattr(raw, "name", None)
    if isinstance(name, str) and name:
        token = name
    else:
        token = str(raw)
    if "." in token:
        token = token.rsplit(".", 1)[-1]
    return token.lower()


def _safe_call(obj, name, *args, **kwargs):
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def uv_fill_has_direction_channel(active_channels, normal_types):
    """active_channels が法線・異方性角度を含むか。列挙不明時は安全側で真。"""
    if normal_types is None:
        return True
    try:
        channels = list(active_channels or [])
        direction_types = tuple(normal_types)
    except (TypeError, ValueError):
        return True
    for channel in channels:
        for direction_type in direction_types:
            try:
                if channel == direction_type:
                    return True
            except Exception:
                continue
    return False


def _uv_fill_direction_state(node, normal_types):
    """戻りは (チャンネルを持つ対象か, 方向チャンネルありか)。失敗は不明=あり。"""
    try:
        active_channels = getattr(node, "active_channels")
    except AttributeError:
        # マスク内のフィルエフェクトなど、チャンネルを持たないものは対象外。
        return False, False
    except Exception:
        return True, True
    if callable(active_channels):
        try:
            active_channels = active_channels()
        except Exception:
            return True, True
    return True, uv_fill_has_direction_channel(active_channels, normal_types)


def _direction_fill_name(node):
    try:
        name = node.get_name()
    except Exception:
        name = None
    if not isinstance(name, str) or not name:
        return "名前不明"
    return name[:64]


def inspect_direction_uv_fills(api, texture_sets):
    """現在 UV 投影のフィルにある方向チャンネルを数える。ダイアログ直前用。"""
    count = 0
    names = []
    if api is None:
        return count, names
    for ts in texture_sets or []:
        for stack in _iter_texture_set_stacks(ts):
            walk = {"paint_layers": 0, "groups_unreadable": 0}
            try:
                roots = api.get_root_layer_nodes(stack)
            except Exception:
                continue
            for node in iter_uv_fill_nodes(roots, walk):
                try:
                    if node.get_projection_mode() != api.ProjectionMode.UV:
                        continue
                except Exception:
                    continue
                candidate, has_direction = _uv_fill_direction_state(
                    node, api.direction_channel_types)
                if not candidate or not has_direction:
                    continue
                count += 1
                if len(names) < 10:
                    names.append(_direction_fill_name(node))
    return count, names


def _iter_named_children(node, names):
    for name in names:
        got = _safe_call(node, name)
        if got is None:
            continue
        if isinstance(got, (list, tuple)):
            return list(got)
        try:
            return list(got)
        except TypeError:
            continue
    return None


def _node_has_projection_api(node):
    for name in (
        "get_projection_mode",
        "set_projection_mode",
        "get_projection_parameters",
        "set_projection_parameters",
    ):
        if not callable(getattr(node, name, None)):
            return False
    return True


def _uv_fill_visit_key(node):
    """訪問済み判定に使う安定した識別子。無ければ None。

    id() は使わない。呼ぶたびに新しいラッパーを返す API では、
    解放されたラッパーの id が次の未訪問ノードに再利用される。
    """
    uid_fn = getattr(node, "uid", None)
    if not callable(uid_fn):
        return None
    try:
        value = uid_fn()
    except Exception:
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        hash(value)
    except TypeError:
        return None
    return value


def iter_uv_fill_nodes(roots, report, *, max_depth=32, _seen=None, _depth=0):
    """深さ優先。1 ノードの失敗で全体を止めない。

    訪問済みは uid() があるノードだけ。無いノードは判定せず、深さで打ち切る。
    """
    if _seen is None:
        _seen = set()
    if roots is None:
        return
    try:
        nodes = list(roots)
    except TypeError:
        nodes = [roots]
    for node in nodes:
        key = _uv_fill_visit_key(node)
        if key is not None:
            if key in _seen:
                continue
            _seen.add(key)
        if _depth > max_depth:
            continue
        try:
            token = node_type_token(node)
            if "paint" in token and not _node_has_projection_api(node):
                report["paint_layers"] = report.get("paint_layers", 0) + 1
            if _node_has_projection_api(node):
                yield node
            children = _iter_named_children(
                node,
                ("sub_layers", "get_sub_layers", "children", "get_children", "get_nodes"),
            )
            is_group = (
                "group" in token
                or children is not None
                or any(
                    callable(getattr(node, name, None))
                    for name in ("sub_layers", "get_sub_layers", "children", "get_children")
                )
            )
            if is_group and children is None:
                report["groups_unreadable"] = report.get("groups_unreadable", 0) + 1
            if children:
                for child in iter_uv_fill_nodes(
                    children, report, max_depth=max_depth, _seen=_seen, _depth=_depth + 1
                ):
                    yield child
            has_mask = _safe_call(node, "has_mask")
            if has_mask:
                effects = _iter_named_children(node, ("mask_effects", "get_mask_effects"))
                if effects:
                    for child in iter_uv_fill_nodes(
                        effects, report, max_depth=max_depth, _seen=_seen, _depth=_depth + 1
                    ):
                        yield child
            content = _iter_named_children(
                node, ("content_effects", "get_content_effects"))
            if content:
                for child in iter_uv_fill_nodes(
                    content, report, max_depth=max_depth, _seen=_seen, _depth=_depth + 1
                ):
                    yield child
        except Exception:
            continue


def switch_uv_fill_node(node, api, source_uv_set):
    """戻りは (結果, 記録または理由)。結果は switched / skipped / failed。"""
    record = {
        "old_mode": None,
        "old_params": None,
        "copied_attrs": [],
        "missing_attrs": [],
        "name": None,
    }
    try:
        record["name"] = node.get_name()
    except Exception:
        record["name"] = None
    try:
        mode = node.get_projection_mode()
    except Exception as exc:
        return "failed", {"reason": "get_mode", "error": "{0}".format(exc)}
    if mode != api.ProjectionMode.UV:
        return "skipped", "not_uv"
    record["old_mode"] = mode
    try:
        old_params = node.get_projection_parameters()
    except Exception as exc:
        return "failed", {"reason": "get_params", "error": "{0}".format(exc)}
    record["old_params"] = old_params
    try:
        new_params = api.UVSetToUVSetProjectionParams()
    except Exception as exc:
        return "failed", {"reason": "make_params", "error": "{0}".format(exc)}
    for attr in _UV_FILL_PARAM_ATTRS:
        if old_params is not None and hasattr(old_params, attr) and hasattr(new_params, attr):
            try:
                setattr(new_params, attr, getattr(old_params, attr))
                record["copied_attrs"].append(attr)
            except Exception:
                record["missing_attrs"].append(attr)
        else:
            record["missing_attrs"].append(attr)
    try:
        new_params.source_uv_set = source_uv_set
    except Exception as exc:
        return "failed", {"reason": "set_source", "error": "{0}".format(exc)}
    # 要実機確認: set_projection_mode と set_projection_parameters の順序。
    try:
        node.set_projection_mode(api.ProjectionMode.UVSetToUVSet)
        node.set_projection_parameters(new_params)
    except Exception as exc:
        rollback_uv_fill_node(node, record, api)
        return "failed", {"reason": "set", "error": "{0}".format(exc), "record": record}
    try:
        new_mode = node.get_projection_mode()
        if new_mode != api.ProjectionMode.UVSetToUVSet:
            rollback_uv_fill_node(node, record, api)
            return "failed", {"reason": "verify_mode", "record": record}
        read_params = node.get_projection_parameters()
        source = getattr(read_params, "source_uv_set", None)
        if source is not None and source != source_uv_set:
            rollback_uv_fill_node(node, record, api)
            return "failed", {"reason": "verify_source", "record": record}
    except Exception as exc:
        rollback_uv_fill_node(node, record, api)
        return "failed", {"reason": "verify", "error": "{0}".format(exc), "record": record}
    return "switched", record


def rollback_uv_fill_node(node, record, api):
    if not isinstance(record, dict):
        return False
    ok = True
    old_mode = record.get("old_mode")
    old_params = record.get("old_params")
    try:
        if old_mode is not None and callable(getattr(node, "set_projection_mode", None)):
            node.set_projection_mode(old_mode)
    except Exception:
        ok = False
    try:
        if old_params is not None and callable(getattr(node, "set_projection_parameters", None)):
            node.set_projection_parameters(old_params)
    except Exception:
        ok = False
    return ok


class UvFillStackReport(object):
    def __init__(self, label=""):
        self.label = label
        self.visited = 0
        self.uv_fills_found = 0
        self.switched = 0
        self.skipped_not_uv = 0
        self.failed = 0
        self.rolled_back = 0
        self.paint_layers = 0
        self.groups_unreadable = 0
        self.needs_manual_check = False
        self.missing_attrs = []
        self.switched_names = []
        self.direction_fills = 0
        self.direction_fill_names = []


def _remember_missing_attrs(report, detail):
    if not isinstance(detail, dict):
        return
    raw = detail.get("missing_attrs")
    if not isinstance(raw, list):
        record = detail.get("record")
        if isinstance(record, dict):
            raw = record.get("missing_attrs")
    if not isinstance(raw, list):
        return
    for attr in raw:
        if isinstance(attr, str) and attr and attr not in report.missing_attrs:
            report.missing_attrs.append(attr)


def switch_uv_fills_in_stack(stack, api, label, source_uv_set):
    report = UvFillStackReport(label)
    walk = {"paint_layers": 0, "groups_unreadable": 0}
    try:
        roots = api.get_root_layer_nodes(stack)
    except Exception:
        report.needs_manual_check = True
        return report
    applied = []
    failed = False
    for node in iter_uv_fill_nodes(roots, walk):
        report.visited += 1
        report.uv_fills_found += 1
        direction_candidate, has_direction = _uv_fill_direction_state(
            node, api.direction_channel_types)
        result, detail = switch_uv_fill_node(node, api, source_uv_set)
        _remember_missing_attrs(report, detail)
        if result == "switched":
            report.switched += 1
            name = None
            if isinstance(detail, dict):
                name = detail.get("name")
                applied.append((node, detail))
            else:
                applied.append((node, {}))
            if isinstance(name, str) and name:
                report.switched_names.append(name)
            if direction_candidate and has_direction:
                report.direction_fills += 1
                if len(report.direction_fill_names) < 10:
                    report.direction_fill_names.append(_direction_fill_name(node))
        elif result == "skipped":
            report.skipped_not_uv += 1
        else:
            report.failed += 1
            failed = True
            break
    report.paint_layers = walk.get("paint_layers", 0)
    report.groups_unreadable = walk.get("groups_unreadable", 0)
    if failed:
        for node, rec in applied:
            if rollback_uv_fill_node(node, rec, api):
                report.rolled_back += 1
            else:
                report.needs_manual_check = True
        report.switched = 0
        report.switched_names = []
        report.direction_fills = 0
        report.direction_fill_names = []
    return report


class UvFillSummary(object):
    def __init__(self):
        self.enabled = False
        self.gate = None
        self.per_texture_set = {}
        self.switched = 0
        self.skipped = 0
        self.failed = 0
        self.rolled_back = 0
        self.needs_manual_check = False
        self.missing_attrs = []
        self.direction_fills = 0
        self.direction_fill_names = []

    def to_status_dict(self):
        return {
            "enabled": bool(self.enabled),
            "switched": int(self.switched),
            "skipped": int(self.skipped),
            "failed": int(self.failed),
            "rolled_back": int(self.rolled_back),
            "direction": int(self.direction_fills),
        }

    def to_message(self):
        parts = []
        if self.switched >= 1:
            parts.append(
                "UV 投影のフィル {0} 件を旧 UV セット経由に切り替えました".format(
                    self.switched)
            )
        elif self.gate:
            parts.append(_UV_FILL_GATE_MESSAGES.get(self.gate, self.gate))
        if self.failed:
            parts.append("失敗 {0} 件".format(self.failed))
        if self.rolled_back:
            parts.append("ロールバック {0} 件".format(self.rolled_back))
        if self.needs_manual_check or self.missing_attrs:
            parts.append("一部は目視確認が必要です")
        if self.direction_fills >= 1:
            names = ", ".join(self.direction_fill_names) or "名前不明"
            parts.append(REBASE_DIRECTION_WARNING_FMT.format(
                n=self.direction_fills, names=names))
        cleaned = []
        for part in parts:
            if not isinstance(part, str) or part == "":
                continue
            cleaned.append(part.rstrip("。"))
        text = "。".join(cleaned)
        if text:
            text += "。"
        return _rebase_clip_message(text)


def _texture_set_name(ts):
    for name in ("name", "get_name"):
        fn = getattr(ts, name, None)
        if callable(fn):
            try:
                value = fn()
            except Exception:
                continue
            if isinstance(value, str):
                return value
        elif isinstance(fn, str):
            return fn
    if isinstance(ts, str):
        return ts
    return None


def _texture_set_key(name):
    if not isinstance(name, str):
        return ""
    return unicodedata.normalize("NFC", name).casefold()


def _iter_texture_set_stacks(ts):
    # 要実機確認: ts.all_stacks() / ts.get_stack()
    stacks = _safe_call(ts, "all_stacks")
    if stacks:
        try:
            return list(stacks)
        except TypeError:
            pass
    stack = _safe_call(ts, "get_stack")
    if stack is not None:
        return [stack]
    return []


def switch_uv_fills_for_project(api, texture_sets, ts_names, *, enabled, uv_old_index, log):
    summary = UvFillSummary()
    summary.enabled = bool(enabled)
    if not enabled:
        summary.gate = "disabled_by_setting"
        return summary
    if api is None:
        summary.gate = "no_api"
        return summary
    if uv_old_index is None:
        summary.gate = "uvold_absent"
        return summary
    wanted = set()
    if isinstance(ts_names, (list, tuple)):
        for name in ts_names:
            key = _texture_set_key(name)
            if key:
                wanted.add(key)
    # 空の依頼は全テクスチャセットに広げない。
    if not wanted:
        summary.gate = "no_matching_texture_set"
        return summary
    matched = []
    skipped_not_in_request = 0
    for ts in texture_sets or []:
        name = _texture_set_name(ts)
        key = _texture_set_key(name)
        if key not in wanted:
            skipped_not_in_request += 1
            continue
        matched.append((name or key, ts))
    if not matched:
        summary.gate = "no_matching_texture_set"
        summary.skipped += skipped_not_in_request
        return summary
    summary.skipped += skipped_not_in_request
    for name, ts in matched:
        counts = {
            "found": 0,
            "switched": 0,
            "skipped_not_uv": 0,
            "failed": 0,
            "rolled_back": 0,
            "direction": 0,
        }
        for stack in _iter_texture_set_stacks(ts):
            report = switch_uv_fills_in_stack(stack, api, name, uv_old_index)
            counts["found"] += report.uv_fills_found
            counts["switched"] += report.switched
            counts["skipped_not_uv"] += report.skipped_not_uv
            counts["failed"] += report.failed
            counts["rolled_back"] += report.rolled_back
            counts["direction"] += report.direction_fills
            summary.switched += report.switched
            summary.skipped += report.skipped_not_uv
            summary.failed += report.failed
            summary.rolled_back += report.rolled_back
            summary.direction_fills += report.direction_fills
            for fill_name in report.direction_fill_names:
                if len(summary.direction_fill_names) >= 10:
                    break
                summary.direction_fill_names.append(fill_name[:64])
            if report.needs_manual_check:
                summary.needs_manual_check = True
            for attr in report.missing_attrs:
                if attr not in summary.missing_attrs:
                    summary.missing_attrs.append(attr)
            if report.switched_names and callable(log):
                log("UV フィル切り替え レイヤー: {0}".format(
                    ", ".join(report.switched_names)))
        summary.per_texture_set[name] = counts
        if callable(log):
            missing = ", ".join(report.missing_attrs) if report.missing_attrs else "-"
            log(
                "UV フィル切り替え [{0}] 発見 {1} / 切り替え {2} / UV 以外 {3} / 失敗 {4} / ロールバック {5} / needs_manual_check={6} / missing_attrs={7}".format(
                    name,
                    counts["found"],
                    counts["switched"],
                    counts["skipped_not_uv"],
                    counts["failed"],
                    counts["rolled_back"],
                    "true" if report.needs_manual_check else "false",
                    missing,
                )
            )
    return summary
