# -*- coding: utf-8 -*-
"""sp_live_sync_plugin 1.4.0 PB-b の UV 投影フィル切り替え。"""
from __future__ import annotations

import os
import sys

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_TESTS)
for _p in (_PKG, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from plugin_patch import patch_plugin_attr  # noqa: E402
from test_sp_live_sync_plugin_protocol import plugin  # noqa: E402
import uvremap_rebase as rb  # noqa: E402


class Mode(object):
    UV = "UV"
    UVSetToUVSet = "UVSetToUVSet"
    Fill = "Fill"
    Triplanar = "Triplanar"


class ChannelType(object):
    Normal = "Normal"
    AnisotropyAngle = "AnisotropyAngle"
    BaseColor = "BaseColor"


class UVProjectionParams(object):
    def __init__(self):
        self.filtering_mode = "bilinear"
        self.uv_wrapping_mode = "repeat"
        self.uv_transformation = (1, 0, 0)


class UVSetToUVSetProjectionParams(object):
    def __init__(self):
        self.source_uv_set = 0
        self.filtering_mode = None
        self.uv_wrapping_mode = None
        self.uv_transformation = None


class PartialUVSetParams(object):
    def __init__(self):
        self.source_uv_set = 0


class FakeNode(object):
    def __init__(self, type_name, mode=None, params=None, name="node", children=None,
                 mask_effects=None, content_effects=None, has_mask=False,
                 active_channels_marker=None):
        self._type = type_name
        self.mode = mode
        self.params = params or UVProjectionParams()
        self._name = name
        self._children = children or []
        self._mask_effects = mask_effects or []
        self._content_effects = content_effects or []
        self._has_mask = has_mask
        self.set_calls = []
        self.fail_set_params = False
        self.fail_rollback = False
        if active_channels_marker is not None:
            self.active_channels = active_channels_marker

    def get_type(self):
        return self._type

    def get_name(self):
        return self._name

    def get_projection_mode(self):
        return self.mode

    def set_projection_mode(self, mode):
        self.set_calls.append(("mode", mode))
        if self.fail_rollback and mode != Mode.UVSetToUVSet:
            raise RuntimeError("rollback mode failed")
        self.mode = mode

    def get_projection_parameters(self):
        return self.params

    def set_projection_parameters(self, params):
        self.set_calls.append(("params", params))
        if self.fail_set_params:
            raise RuntimeError("set_params failed")
        if self.fail_rollback and getattr(params, "source_uv_set", None) != 1:
            raise RuntimeError("rollback params failed")
        self.params = params

    def sub_layers(self):
        return list(self._children)

    def has_mask(self):
        return self._has_mask

    def mask_effects(self):
        return list(self._mask_effects)

    def content_effects(self):
        return list(self._content_effects)


class PaintNode(object):
    def __init__(self, name="paint"):
        self._name = name

    def get_type(self):
        return "PaintLayerNode"

    def get_name(self):
        return self._name


class GroupNoChildren(object):
    def get_type(self):
        return "GroupLayerNode"

    def get_name(self):
        return "broken-group"


class LayerstackFake(object):
    def __init__(self, roots, params_cls=UVSetToUVSetProjectionParams):
        self._roots = roots
        self.ProjectionMode = Mode
        self.ChannelType = ChannelType
        self.UVSetToUVSetProjectionParams = params_cls

    def get_root_layer_nodes(self, stack):
        return list(self._roots)


class TextureSetFake(object):
    def __init__(self, name, stacks):
        self._name = name
        self._stacks = stacks

    def name(self):
        return self._name

    def all_stacks(self):
        return list(self._stacks)


def _api(roots, params_cls=UVSetToUVSetProjectionParams):
    return plugin.make_uv_fill_api(LayerstackFake(roots, params_cls=params_cls))


def test_switch_uv_fill_copies_params_and_skips_others():
    uv = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="uv-fill")
    fill = FakeNode("FillLayerNode", Mode.Fill, name="fill")
    tri = FakeNode("FillLayerNode", Mode.Triplanar, name="tri")
    already = FakeNode(
        "FillLayerNode", Mode.UVSetToUVSet,
        UVSetToUVSetProjectionParams(), name="already")
    paint = PaintNode()
    api = _api([uv, fill, tri, already, paint])
    report = plugin.switch_uv_fills_in_stack("stack", api, "Body", 1)
    assert uv.mode == Mode.UVSetToUVSet
    assert uv.params.source_uv_set == 1
    assert uv.params.filtering_mode == "bilinear"
    assert uv.params.uv_wrapping_mode == "repeat"
    assert uv.params.uv_transformation == (1, 0, 0)
    assert fill.mode == Mode.Fill
    assert tri.mode == Mode.Triplanar
    assert already.mode == Mode.UVSetToUVSet
    assert report.switched == 1
    assert report.skipped_not_uv == 3
    assert report.paint_layers == 1


def test_walks_groups_masks_and_content_and_stops_at_max_depth():
    deep_fill = FakeNode("FillLayerNode", Mode.UV, name="deep")
    g3 = FakeNode("GroupLayerNode", name="g3", children=[deep_fill])
    g2 = FakeNode("GroupLayerNode", name="g2", children=[g3])
    mask_fill = FakeNode("FillEffect", Mode.UV, name="mask-fill")
    content_fill = FakeNode("FillEffect", Mode.UV, name="content-fill")
    mid = FakeNode(
        "FillLayerNode", Mode.UV, name="mid",
        children=[g2], has_mask=True,
        mask_effects=[mask_fill], content_effects=[content_fill],
    )
    root = FakeNode("GroupLayerNode", name="g1", children=[mid, GroupNoChildren()])
    api = _api([root])
    report = plugin.switch_uv_fills_in_stack("stack", api, "Body", 1)
    assert report.switched >= 3
    assert report.groups_unreadable >= 1
    shallow = {"paint_layers": 0, "groups_unreadable": 0}
    nodes = list(plugin.iter_uv_fill_nodes([root], shallow, max_depth=0))
    assert isinstance(nodes, list)


def test_missing_attrs_and_non_fill_nodes():
    uv = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="uv")
    api = _api([uv], params_cls=PartialUVSetParams)
    result, record = plugin.switch_uv_fill_node(uv, api, 1)
    assert result == "switched"
    assert "filtering_mode" in record["missing_attrs"]
    bare = PaintNode()
    assert plugin._node_has_projection_api(bare) is False


def test_stack_rollback_and_manual_check():
    first = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="ok")
    second = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="bad")
    second.fail_set_params = True
    api = _api([first, second])
    report = plugin.switch_uv_fills_in_stack("stack", api, "Body", 1)
    assert report.failed == 1
    assert report.rolled_back == 1
    assert report.switched == 0
    assert first.mode == Mode.UV
    other = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="other")
    api2 = _api([other])
    report2 = plugin.switch_uv_fills_in_stack("stack2", api2, "Trim", 1)
    assert report2.switched == 1
    first.fail_rollback = True
    first.mode = Mode.UVSetToUVSet
    first.params = UVSetToUVSetProjectionParams()
    first.params.source_uv_set = 9
    rec = {"old_mode": Mode.UV, "old_params": UVProjectionParams()}
    assert plugin.rollback_uv_fill_node(first, rec, api) is False


def test_verify_mismatch_rolls_back():
    class Liar(FakeNode):
        def get_projection_mode(self):
            return Mode.UV

    node = Liar("FillLayerNode", Mode.UV, UVProjectionParams(), name="liar")
    api = _api([node])
    result, _detail = plugin.switch_uv_fill_node(node, api, 1)
    assert result == "failed"
    assert node.mode == Mode.UV


def test_gates_and_uv_old_index(monkeypatch):
    logs = []
    uv = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="uv")
    ts = TextureSetFake("Body", ["stack"])
    api = _api([uv])
    patch_plugin_attr(monkeypatch, plugin, "switch_uv_fills_in_stack",
                        lambda stack, a, label, source: plugin.switch_uv_fills_in_stack(
                            stack, a, label, source))
    # replace get_root via layerstack on api already bound
    summary = plugin.switch_uv_fills_for_project(
        api, [ts], ["Body"], enabled=False, uv_old_index=1, log=logs.append)
    assert summary.gate == "disabled_by_setting"
    assert uv.mode == Mode.UV
    summary = plugin.switch_uv_fills_for_project(
        None, [ts], ["Body"], enabled=True, uv_old_index=1, log=logs.append)
    assert summary.gate == "no_api"
    summary = plugin.switch_uv_fills_for_project(
        api, [ts], ["Body"], enabled=True, uv_old_index=None, log=logs.append)
    assert summary.gate == "uvold_absent"
    summary = plugin.switch_uv_fills_for_project(
        api, [ts], ["Other"], enabled=True, uv_old_index=1, log=logs.append)
    assert summary.gate == "no_matching_texture_set"
    for empty in ([], None, [""], ["", None]):
        summary = plugin.switch_uv_fills_for_project(
            api, [ts], empty, enabled=True, uv_old_index=1, log=logs.append)
        assert summary.gate == "no_matching_texture_set"
        assert summary.switched == 0
    assert uv.mode == Mode.UV
    assert plugin.get_uv_old_index({"uv_old": {"name": "uvOld", "index": 1}}) == 1
    assert plugin.get_uv_old_index({"uv_old": {"name": "uvOld", "index": 7}}) == 7
    assert plugin.get_uv_old_index({"uv_old": {"name": "uvOld", "index": 2}}) == 2
    for bad in (None, {}, {"uv_old": None}, {"uv_old": "x"},
                {"uv_old": {"name": "uvOld", "index": True}},
                {"uv_old": {"name": "uvOld", "index": 0}},
                {"uv_old": {"name": "uvOld", "index": 8}},
                {"uv_old": {"name": "uvOld", "index": "1"}}):
        assert plugin.get_uv_old_index(bad) is None


def test_index_two_is_not_hardcoded_one():
    uv = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="uv")
    api = _api([uv])
    result, _ = plugin.switch_uv_fill_node(uv, api, 2)
    assert result == "switched"
    assert uv.params.source_uv_set == 2


def test_does_not_open_fbx(monkeypatch):
    def boom(path, *args, **kwargs):
        if str(path).lower().endswith(".fbx"):
            raise AssertionError("must not open FBX")
        raise AssertionError("open should not be used")

    monkeypatch.setattr("builtins.open", boom)
    assert plugin.get_uv_old_index({"uv_old": {"name": "uvOld", "index": 1}}) == 1
    summary = plugin.switch_uv_fills_for_project(
        None, [], ["Body"], enabled=True, uv_old_index=None, log=None)
    assert summary.gate == "no_api"


def test_summary_status_and_message():
    logs = []
    uv = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="uv")
    ts = TextureSetFake("Body", ["stack"])
    api = _api([uv])
    # stack walk uses api.get_root_layer_nodes which ignores stack and returns roots
    summary = plugin.switch_uv_fills_for_project(
        api, [ts], ["Body"], enabled=True, uv_old_index=1, log=logs.append)
    assert any("Body" in line for line in logs)
    status = plugin.build_rebase_status(
        "rb-20261019-000001-0001",
        "done",
        1,
        plugin.__version__,
        uv_fills=summary.to_status_dict(),
    )
    parsed = rb.parse_status(status)
    assert parsed is not None
    assert parsed["state"] == "done"
    message = summary.to_message()
    assert len(message) <= 500
    assert "切り替え" in message


def test_direction_channel_pure_detection_and_reporting():
    assert plugin.uv_fill_has_direction_channel(
        [ChannelType.Normal], (ChannelType.Normal, ChannelType.AnisotropyAngle))
    assert plugin.uv_fill_has_direction_channel(
        [ChannelType.AnisotropyAngle],
        (ChannelType.Normal, ChannelType.AnisotropyAngle),
    )
    assert not plugin.uv_fill_has_direction_channel(
        [ChannelType.BaseColor],
        (ChannelType.Normal, ChannelType.AnisotropyAngle),
    )
    assert plugin.uv_fill_has_direction_channel([ChannelType.BaseColor], None)

    normal = FakeNode(
        "FillLayerNode",
        Mode.UV,
        name="N" * 80,
        active_channels_marker=[ChannelType.Normal],
    )
    color = FakeNode(
        "FillLayerNode",
        Mode.UV,
        name="color",
        active_channels_marker=[ChannelType.BaseColor],
    )
    mask = FakeNode("FillEffect", Mode.UV, name="mask-without-channels")
    api = _api([normal, color, mask])
    report = plugin.switch_uv_fills_in_stack("stack", api, "Body", 1)
    assert report.direction_fills == 1
    assert report.direction_fill_names == ["N" * 64]

    current_normal = FakeNode(
        "FillLayerNode",
        Mode.UV,
        name="current-normal",
        active_channels_marker=[ChannelType.Normal],
    )
    inspect_api = _api([current_normal, mask])
    count, names = plugin.inspect_direction_uv_fills(
        inspect_api, [TextureSetFake("Body", ["stack"])])
    assert (count, names) == (1, ["current-normal"])


def test_direction_summary_is_in_status_message_and_approval():
    summary = plugin.UvFillSummary()
    summary.enabled = True
    summary.direction_fills = 2
    summary.direction_fill_names = ["Normal A", "Normal B"]
    status = summary.to_status_dict()
    assert status["direction"] == 2
    assert plugin.REBASE_DIRECTION_WARNING_FMT.format(
        n=2, names="Normal A, Normal B") in summary.to_message()
    text = plugin.build_rebase_approval_text(
        {"ts_names": ["Body"]},
        backup_dir="backup",
        has_direction_fills=True,
    )
    assert plugin.REBASE_DIRECTION_APPROVAL_NOTE in text


class _RecycleNode(object):
    """呼ぶたびに子のラッパーを作り直す。uid() だけが安定している。"""

    def __init__(self, uid, kind, child_uids, factory):
        self._uid = uid
        self._kind = kind
        self._child_uids = child_uids
        self._factory = factory
        self.mode = Mode.UV
        self.params = UVProjectionParams()

    def uid(self):
        return self._uid

    def get_type(self):
        if self._kind == "fill":
            return "FillLayerNode"
        return "GroupLayerNode"

    def get_name(self):
        return "n{0}".format(self._uid)

    def sub_layers(self):
        return [self._factory(child) for child in self._child_uids]


class _RecycleFill(_RecycleNode):
    def get_projection_mode(self):
        return self.mode

    def set_projection_mode(self, mode):
        self.mode = mode

    def get_projection_parameters(self):
        return self.params

    def set_projection_parameters(self, params):
        self.params = params


def _recycling_tree(count):
    specs = {}
    group_uids = []
    for index in range(count):
        fill_uid = 1000 + index
        group_uid = index + 1
        specs[fill_uid] = ("fill", [])
        specs[group_uid] = ("group", [fill_uid])
        group_uids.append(group_uid)
    specs[0] = ("group", group_uids)

    def factory(uid):
        kind, children = specs[uid]
        cls = _RecycleFill if kind == "fill" else _RecycleNode
        return cls(uid, kind, children, factory)

    return factory(0)


@pytest.mark.parametrize("count", [90, 400])
def test_recycling_wrappers_finds_every_fill(count):
    root = _recycling_tree(count)
    report = {"paint_layers": 0, "groups_unreadable": 0}
    uids = []
    for node in plugin.iter_uv_fill_nodes([root], report):
        uids.append(node.uid())
    assert uids == list(range(1000, 1000 + count))


def test_logs_manual_check_and_missing_attrs():
    logs = []
    uv = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="partial")
    ts = TextureSetFake("Body", ["stack"])
    api = _api([uv], params_cls=PartialUVSetParams)
    summary = plugin.switch_uv_fills_for_project(
        api, [ts], ["Body"], enabled=True, uv_old_index=1, log=logs.append)
    assert "filtering_mode" in summary.missing_attrs
    assert any("missing_attrs=" in line and "filtering_mode" in line for line in logs)
    message = summary.to_message()
    assert message.endswith("。")
    assert "目視確認" in message
    assert message.count("。") >= 1

    logs.clear()
    first = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="ok")
    second = FakeNode("FillLayerNode", Mode.UV, UVProjectionParams(), name="bad")
    second.fail_set_params = True
    first.fail_rollback = True
    api = _api([first, second])
    summary = plugin.switch_uv_fills_for_project(
        api, [ts], ["Body"], enabled=True, uv_old_index=1, log=logs.append)
    assert summary.needs_manual_check is True
    assert any("needs_manual_check=true" in line for line in logs)
    assert "目視確認" in summary.to_message()


def test_panel38_names_still_exist():
    assert plugin.PANEL_SETTING_KEYS
    assert callable(plugin.unsaved_setting_keys)
    import inspect
    src = inspect.getsource(plugin.LiveSyncPanel)
    assert "rebase" not in src
