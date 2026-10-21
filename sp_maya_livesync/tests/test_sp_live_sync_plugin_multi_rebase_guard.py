# -*- coding: utf-8 -*-
"""1.4.5: 連続リベースで崩れる UV 投影フィルの安全策。"""
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
from test_sp_live_sync_plugin_rebase_job import (  # noqa: E402
    FakeDialog,
    _coord,
    _patch_approval_qt,
    _request,
    _settings as job_settings,
    _write_req,
)
from test_sp_live_sync_plugin_rebase_pure import (  # noqa: E402
    _ok_request,
    _ok_state,
    _settings,
)
from test_sp_live_sync_plugin_uvfills import (  # noqa: E402
    ChannelType,
    FakeNode,
    Mode,
    TextureSetFake,
    UVSetToUVSetProjectionParams,
    _api,
)

RID = "rb-20261019-053400-3fa2"
WARNING = "前回のリベースで切り替えた UV 投影のフィルが"
AWAITING = "前回切り替えた UV 投影のフィルが"
OLD_KEEP = "UV 投影のフィル(マスクのフィルを含む)は"
NEW_KEEP = "UV 投影のまま残っているフィル"


class _RaisingSource(object):
    @property
    def source_uv_set(self):
        raise RuntimeError("unreadable")


class _NoSource(object):
    pass


class _StringSource(object):
    source_uv_set = "1"


def _params(index):
    params = UVSetToUVSetProjectionParams()
    params.source_uv_set = index
    return params


def _uvset(name, params):
    return FakeNode("FillLayerNode", Mode.UVSetToUVSet, params, name=name)


def _direction(name):
    return FakeNode(
        "FillLayerNode",
        Mode.UV,
        name=name,
        active_channels_marker=[ChannelType.Normal],
    )


def _sample_tree():
    mask_prior = _uvset("mask-prior", _params(1))
    group_prior = _uvset("group-prior", _params(1))
    holder = FakeNode(
        "FillLayerNode", Mode.Fill, name="holder",
        has_mask=True, mask_effects=[mask_prior],
    )
    group = FakeNode("GroupLayerNode", name="group", children=[group_prior])
    long_prior = _uvset("L" * 80, _params(1))
    other = _uvset("other-index", _params(4))
    unknown = _uvset("unknown-src", _RaisingSource())
    string_src = _uvset("string-src", _StringSource())
    missing = _uvset("missing-src", _NoSource())
    roots = [
        _direction("dir-a"),
        holder,
        group,
        long_prior,
        other,
        unknown,
        string_src,
        missing,
        _direction("dir-b"),
    ]
    api = _api(roots)
    sets = [TextureSetFake("Body", ["stack"])]
    return api, sets


def test_inspect_counts_prior_direction_groups_and_masks():
    api, sets = _sample_tree()
    found = plugin.inspect_uv_fills_for_approval(api, sets, 1)
    assert found.direction_count == 2
    assert found.direction_names == ["dir-a", "dir-b"]
    assert found.prior_count == 3
    assert found.prior_unknown == 3
    assert found.prior_names == [
        "mask-prior",
        "group-prior",
        "L" * 64,
        "unknown-src",
        "string-src",
        "missing-src",
    ]
    assert plugin.inspect_direction_uv_fills(api, sets) == (
        found.direction_count, found.direction_names)

    none_old = plugin.inspect_uv_fills_for_approval(api, sets, None)
    assert none_old.prior_count == 0
    assert none_old.prior_unknown == 0
    assert none_old.prior_names == []
    assert (none_old.direction_count, none_old.direction_names) == (
        plugin.inspect_direction_uv_fills(api, sets))

    stopped = plugin.inspect_uv_fills_for_approval(api, sets, 1, stop_after=1)
    assert stopped.prior_count + stopped.prior_unknown == 1
    assert stopped.prior_names == ["mask-prior"]
    assert stopped.direction_count == 1
    assert stopped.direction_names == ["dir-a"]
    assert plugin.inspect_uv_fills_for_approval(None, sets, 1) == (
        plugin.UvFillApproval(0, [], 0, 0, []))


def test_inspect_caps_prior_names_at_ten():
    nodes = [_uvset("p{0}".format(i), _params(1)) for i in range(12)]
    found = plugin.inspect_uv_fills_for_approval(
        _api(nodes), [TextureSetFake("Body", ["stack"])], 1)
    assert found.prior_count == 12
    assert found.prior_names == ["p{0}".format(i) for i in range(10)]


def test_switch_distinguishes_prior_uvset_and_keeps_skipped_total():
    uv = FakeNode("FillLayerNode", Mode.UV, name="uv")
    tri = FakeNode("FillLayerNode", Mode.Triplanar, name="tri")
    match = _uvset("match", _params(1))
    other = _uvset("other", _params(2))
    unknown = _uvset("unknown", _RaisingSource())
    api = _api([uv, tri, match, other, unknown])

    assert plugin.switch_uv_fill_node(match, api, 1) == ("skipped", "prior_uvset")
    assert plugin.switch_uv_fill_node(other, api, 1) == ("skipped", "not_uv")
    assert plugin.switch_uv_fill_node(unknown, api, 1) == ("skipped", "not_uv")
    assert plugin.switch_uv_fill_node(tri, api, 1) == ("skipped", "not_uv")

    fresh = [
        FakeNode("FillLayerNode", Mode.UV, name="uv"),
        FakeNode("FillLayerNode", Mode.Triplanar, name="tri"),
        _uvset("match", _params(1)),
        _uvset("other", _params(2)),
        _uvset("unknown", _RaisingSource()),
    ]
    report = plugin.switch_uv_fills_in_stack("stack", _api(fresh), "Body", 1)
    assert report.switched == 1
    assert report.prior == 1
    assert report.skipped_not_uv == 4
    summary = plugin.UvFillSummary()
    summary.skipped = report.skipped_not_uv
    summary.prior = report.prior
    summary.switched = report.switched
    status = summary.to_status_dict()
    assert status["prior"] == 1
    assert status["skipped"] == 4
    assert status["skipped"] == report.skipped_not_uv
    written = plugin.build_rebase_status(
        "rb-20261019-000001-0001", "done", 1, plugin.__version__,
        uv_fills=status,
    )
    assert written["uv_fills"]["prior"] == 1
    assert written["uv_fills"]["skipped"] == 4


def test_approval_text_warns_only_when_prior_fills_exist():
    request = _ok_request()
    plain = plugin.build_rebase_approval_text(
        request, backup_dir="backup", uv_fill_reproject=True)
    assert OLD_KEEP in plain
    assert NEW_KEEP not in plain
    assert WARNING not in plain

    warned = plugin.build_rebase_approval_text(
        request,
        backup_dir="backup",
        uv_fill_reproject=True,
        prior_fill_count=2,
        prior_fill_names=["alpha", "beta"],
    )
    assert NEW_KEEP in warned
    assert OLD_KEEP not in warned
    assert plugin.REBASE_PRIOR_FILL_WARNING_FMT.format(
        n=2, names="alpha, beta") in warned
    assert "崩れてもよい場合だけ「実行する」を押してください。" in warned


def test_decide_approval_demotes_auto_without_skipping_unsaved():
    request = _ok_request()
    assert plugin.decide_approval(
        request, _ok_state(prior_uv_fills=1), _settings("auto"),
    ) == plugin.ApprovalDecision("ask", "prior_uv_fills")
    assert plugin.decide_approval(
        request,
        _ok_state(prior_uv_fills=1, needs_saving=True),
        _settings("auto"),
    ) == plugin.ApprovalDecision("wait", "unsaved_changes")
    assert plugin.decide_approval(
        request, _ok_state(prior_uv_fills="unknown"), _settings("auto"),
    ) == plugin.ApprovalDecision("ask", "prior_uv_fills")
    assert plugin.decide_approval(
        request, _ok_state(prior_uv_fills=0), _settings("auto"),
    ) == plugin.ApprovalDecision("run", "auto_approved")
    assert plugin.decide_approval(
        request, _ok_state(prior_uv_fills=None), _settings("auto"),
    ) == plugin.ApprovalDecision("run", "auto_approved")
    assert plugin.decide_approval(
        request, _ok_state(), _settings("auto"),
    ) == plugin.ApprovalDecision("run", "auto_approved")
    assert plugin.decide_approval(
        request, _ok_state(prior_uv_fills=1), _settings("ask"),
    ) == plugin.ApprovalDecision("ask", "needs_approval")
    state = plugin.build_painter_state(
        now=1, is_open=True, in_edition=True, project_key="k", saved_path="k",
        busy=False, exporting=False, needs_saving=False,
        backup_includes_unsaved=False, mesh_exists=True, mesh_sha1_ok=True,
        superseded=False, cancelled=False, can_disable_auto_unwrap=True,
        last_edit_at=None,
    )
    assert "prior_uv_fills" not in state
    with_zero = plugin.build_painter_state(
        now=1, is_open=True, in_edition=True, project_key="k", saved_path="k",
        busy=False, exporting=False, needs_saving=False,
        backup_includes_unsaved=False, mesh_exists=True, mesh_sha1_ok=True,
        superseded=False, cancelled=False, can_disable_auto_unwrap=True,
        last_edit_at=None, prior_uv_fills=0,
    )
    assert with_zero["prior_uv_fills"] == 0


def test_default_button_is_cancel_only_when_asked(monkeypatch):
    _patch_approval_qt(monkeypatch)
    plain = plugin.RebaseApprovalDialogHost("確認", lambda _action: None)
    default = [call for call in plain.box.calls if call[0] == "setDefaultButton"]
    assert default[-1][1] is plain._run_btn
    warned = plugin.make_rebase_approval_dialog(
        "確認", lambda _action: None, default_cancel=True)
    default = [call for call in warned.box.calls if call[0] == "setDefaultButton"]
    assert default[-1][1] is warned._cancel_btn


def _spy_inspect(monkeypatch):
    original = plugin.inspect_uv_fills_for_approval
    calls = []

    def spy(api, texture_sets, uv_old_index, *, stop_after=None):
        calls.append(stop_after)
        return original(api, texture_sets, uv_old_index, stop_after=stop_after)

    patch_plugin_attr(
        monkeypatch, plugin, "inspect_uv_fills_for_approval", spy)
    return calls


def _bind_fills(api, roots):
    api.uv_ctx = {
        "api": _api(roots),
        "texture_sets": [TextureSetFake("Body", ["stack"])],
    }


def test_dialog_warns_and_sets_awaiting_message(tmp_path, monkeypatch):
    calls = _spy_inspect(monkeypatch)
    recorded = []
    coord, api, _hooks, clock, dialogs, jobs = _coord(tmp_path)
    def factory(text, on_result, default_cancel=False):
        recorded.append(default_cancel)
        box = FakeDialog(text, on_result)
        dialogs.append(box)
        return box
    coord.dialog_factory = factory
    _bind_fills(api, [_uvset("old-fill", _params(1)), _direction("still-uv")])
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    status = plugin.read_rebase_status(root, RID)
    assert status["state"] == "awaiting_approval"
    assert status["message"] == plugin.REBASE_PRIOR_AWAITING_MESSAGE_FMT.format(n=1)
    assert AWAITING in status["message"]
    assert recorded == [True]
    assert dialogs
    assert WARNING in dialogs[0].text
    assert NEW_KEEP in dialogs[0].text
    assert OLD_KEEP not in dialogs[0].text
    assert jobs == []
    assert calls == [None]


def test_dialog_without_prior_keeps_old_wording(tmp_path, monkeypatch):
    calls = _spy_inspect(monkeypatch)
    coord, api, _hooks, clock, dialogs, jobs = _coord(tmp_path)
    _bind_fills(api, [_direction("still-uv")])
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    coord.tick(clock())
    status = plugin.read_rebase_status(root, RID)
    assert status["state"] == "awaiting_approval"
    assert "崩れます" not in (status.get("message") or "")
    assert dialogs
    assert OLD_KEEP in dialogs[0].text
    assert WARNING not in dialogs[0].text
    assert jobs == []
    assert calls == [None]


def test_dialog_opens_when_scan_raises(tmp_path):
    coord, api, _hooks, clock, dialogs, _jobs = _coord(tmp_path)

    def boom():
        raise RuntimeError("scan failed")

    api.uv_fill_context = boom
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    assert plugin.read_rebase_status(root, RID)["state"] == "awaiting_approval"
    assert dialogs
    assert WARNING not in dialogs[0].text


def test_auto_scans_once_and_demotes_when_prior_exists(tmp_path, monkeypatch):
    calls = _spy_inspect(monkeypatch)
    recorded = []
    coord, api, _hooks, clock, dialogs, jobs = _coord(
        tmp_path, settings=job_settings(rebase_approval="auto"))

    def factory(text, on_result, default_cancel=False):
        recorded.append(default_cancel)
        box = FakeDialog(text, on_result)
        dialogs.append(box)
        return box

    coord.dialog_factory = factory
    _bind_fills(api, [_uvset("old-fill", _params(1))])
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    coord.tick(clock())
    coord.tick(clock())
    assert jobs == []
    assert recorded == [True]
    assert plugin.read_rebase_status(root, RID)["state"] == "awaiting_approval"
    assert calls == [1, None]


def test_auto_without_prior_scans_once_and_runs(tmp_path, monkeypatch):
    calls = _spy_inspect(monkeypatch)
    coord, api, _hooks, clock, _dialogs, jobs = _coord(
        tmp_path, settings=job_settings(rebase_approval="auto"))
    _bind_fills(api, [_direction("still-uv")])
    request = _request(tmp_path)
    _write_req(tmp_path, request)
    coord.tick(clock())
    coord.tick(clock())
    coord.tick(clock())
    assert jobs
    assert calls == [1]


def test_auto_unknown_scan_asks_instead_of_running(tmp_path):
    coord, api, _hooks, clock, dialogs, jobs = _coord(
        tmp_path, settings=job_settings(rebase_approval="auto"))
    seen = {"n": 0}

    def boom():
        seen["n"] += 1
        raise RuntimeError("scan failed")

    api.uv_fill_context = boom

    def factory(text, on_result, default_cancel=False):
        box = FakeDialog(text, on_result)
        dialogs.append(box)
        return box

    coord.dialog_factory = factory
    request = _request(tmp_path)
    root = _write_req(tmp_path, request)
    coord.tick(clock())
    coord.tick(clock())
    assert jobs == []
    assert dialogs
    assert plugin.read_rebase_status(root, RID)["state"] == "awaiting_approval"
    assert seen["n"] == 2
    again = seen["n"]
    coord.tick(clock())
    assert seen["n"] == again
