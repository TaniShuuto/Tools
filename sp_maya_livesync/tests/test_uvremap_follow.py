# -*- coding: utf-8 -*-
"""uvremap_follow のテスト(契約 CONTRACT_PHASE2.md v0.2 の P2-a)。

Maya も Pillow も使わない。時刻は引数で渡す。
"""
from __future__ import annotations

import json
import os
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import uvremap_follow as F  # noqa: E402
import uvremap_io  # noqa: E402
import uvremap_worker as W  # noqa: E402

NS = 1_000_000_000
CHANNELS = (
    {"suffix": "BaseColor", "colorspace": "srgb", "kind": "color"},
    {"suffix": "Normal", "colorspace": "raw", "kind": "normal"},
)


def _follow(**over):
    args = dict(
        now_ns=10 * NS,
        flag_mtime_ns=50,
        last_flag_ns=40,
        last_change_seen_ns=8 * NS,
        settle_s=2.0,
        enabled=True,
        auto_follow=True,
        has_baseline=True,
        ts_states={"Body": F.STATE_CONVERTED_OK, "Head": F.STATE_CONVERTED_OK},
        current_sigs={
            "Body": {"Body_BaseColor.png": [1, 10]},
            "Head": {"Head_BaseColor.png": [2, 20]},
        },
        recorded_sigs={
            "Body": {"Body_BaseColor.png": [1, 9]},
            "Head": {"Head_BaseColor.png": [2, 19]},
        },
        same_scene={"Body": True, "Head": True},
    )
    args.update(over)
    return F.decide_follow(**args)


# ---------------------------------------------------------------------------
# match_source_name
# ---------------------------------------------------------------------------
def test_match_source_name_udim_plain_and_extension():
    assert W.match_source_name("Body", "BaseColor", "Body_BaseColor.1001.png") == (1001, "png")
    assert W.match_source_name("Body", "BaseColor", "Body_BaseColor.png") == (None, "png")
    assert W.match_source_name("Body", "Roughness", "Body_Roughness.jpg") == (None, "jpg")
    assert W.match_source_name("Body", "BaseColor", "Body_BaseColor.1001.tga") == (1001, "tga")
    assert F.match_source_name is W.match_source_name


def test_match_source_name_windows_case_and_prefix():
    name = "body_basecolor.1001.PNG"
    assert W.match_source_name("Body", "BaseColor", name, os_name="nt") == (1001, "PNG")
    assert W.match_source_name("Body", "BaseColor", name, os_name="posix") is None
    assert W.match_source_name("Body", "BaseColor", "BodyB_BaseColor.png") is None
    assert W.match_source_name("BodyB", "BaseColor", "Body_BaseColor.png") is None
    assert W.match_source_name("Body", "BaseColor", "Body_BaseColorX.png") is None
    assert W.match_source_name("Body", "BaseColor", "Body_BaseColorX.1001.png") is None


# ---------------------------------------------------------------------------
# source_signature
# ---------------------------------------------------------------------------
def _touch(path: Path, data: bytes, mtime_ns: int) -> None:
    path.write_bytes(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_source_signature_add_update_delete_and_missing_dir(tmp_path):
    src = tmp_path / "final"
    assert F.source_signature(src, "Body", CHANNELS) == {}
    src.mkdir()
    _touch(src / "Body_BaseColor.png", b"abc", 1000)
    _touch(src / "BodyB_BaseColor.png", b"nope", 1000)
    _touch(src / "Body_Height.png", b"h", 1000)
    (src / "Body_Normal.png").mkdir()
    sig = F.source_signature(src, "Body", CHANNELS)
    assert sig == {"Body_BaseColor.png": [3, 1000]}

    _touch(src / "Body_BaseColor.png", b"abcd", 2000)
    updated = F.source_signature(src, "Body", CHANNELS)
    assert updated == {"Body_BaseColor.png": [4, 2000]}
    assert updated != sig

    _touch(src / "Body_BaseColor.1002.png", b"12", 3000)
    with_tile = F.source_signature(src, "Body", CHANNELS)
    assert with_tile["Body_BaseColor.1002.png"] == [2, 3000]
    assert with_tile["Body_BaseColor.png"] == [4, 2000]

    (src / "Body_BaseColor.png").unlink()
    (src / "Body_BaseColor.1002.png").unlink()
    assert F.source_signature(src, "Body", CHANNELS) == {}


def test_source_signature_os_name_case(tmp_path):
    src = tmp_path / "final"
    src.mkdir()
    _touch(src / "body_basecolor.png", b"z", 5)
    assert F.source_signature(src, "Body", CHANNELS, os_name="posix") == {}
    assert F.source_signature(src, "Body", CHANNELS, os_name="nt") == {
        "body_basecolor.png": [1, 5],
    }


# ---------------------------------------------------------------------------
# follow.json
# ---------------------------------------------------------------------------
def test_follow_roundtrip_corrupt_and_schema(tmp_path, monkeypatch):
    root = tmp_path / "uv"
    root.mkdir()
    assert F.load_follow(root) == F.empty_follow()
    assert F.load_follow(tmp_path / "missing") == F.empty_follow()

    data = {
        "schema": 1,
        "flag_mtime_ns": 1760000000000000000,
        "ts": {
            "Body": {
                "source_sig": {"Body_BaseColor.png": [123456, 1760000000000000000]},
                "last_job_id": "20261018-1",
                "pending": False,
            },
        },
    }
    calls = {}
    real = uvremap_io.replace_file

    def spy(src, dst, *args, **kwargs):
        calls["dst"] = Path(dst).name
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(uvremap_io, "replace_file", spy)
    F.save_follow(root, data)
    assert calls["dst"] == "follow.json"
    assert F.load_follow(root) == data
    assert list(root.glob("*.tmp")) == []

    (root / "follow.json").write_text("{", encoding="utf-8")
    assert F.load_follow(root) == F.empty_follow()

    (root / "follow.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode("utf-8"))
    assert F.load_follow(root) == data

    for payload in (
        {"schema": 2, "flag_mtime_ns": 1, "ts": {"Body": {}}},
        {"schema": "1", "ts": {}},
        {"schema": True, "ts": {}},
        {"flag_mtime_ns": 1, "ts": {}},
        [1, 2],
        {"schema": 1, "ts": []},
    ):
        (root / "follow.json").write_text(json.dumps(payload), encoding="utf-8")
        assert F.load_follow(root) == F.empty_follow()


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------
def test_normalize_follow_settings_defaults_types_and_ranges():
    defaults = F.normalize_follow_settings({})
    assert defaults == {
        "uvremap_auto_follow": False,
        "uvremap_auto_follow_uv": False,
        "uvremap_max_parallel": 2,
        "uvremap_follow_settle_s": 2.0,
        "uvremap_uv_settle_s": 3.0,
        "uvremap_uv_poll_s": 2.0,
    }
    assert F.normalize_follow_settings(None) == defaults
    cfg = {"uvremap_max_parallel": 99, "other": 1}
    assert F.normalize_follow_settings(cfg)["uvremap_max_parallel"] == 4
    assert cfg["uvremap_max_parallel"] == 99
    assert set(F.normalize_follow_settings({"other": True})) == set(defaults)

    assert F.normalize_follow_settings({"uvremap_max_parallel": "2"})["uvremap_max_parallel"] == 2
    assert F.normalize_follow_settings({"uvremap_max_parallel": 2.0})["uvremap_max_parallel"] == 2
    assert F.normalize_follow_settings({"uvremap_max_parallel": True})["uvremap_max_parallel"] == 2
    assert F.normalize_follow_settings({"uvremap_max_parallel": 0})["uvremap_max_parallel"] == 1
    assert F.normalize_follow_settings({"uvremap_max_parallel": 1})["uvremap_max_parallel"] == 1
    assert F.normalize_follow_settings({"uvremap_max_parallel": 4})["uvremap_max_parallel"] == 4

    assert F.normalize_follow_settings({"uvremap_auto_follow": "yes"})["uvremap_auto_follow"] is False
    assert F.normalize_follow_settings({"uvremap_auto_follow": 1})["uvremap_auto_follow"] is False
    assert F.normalize_follow_settings({"uvremap_auto_follow": True})["uvremap_auto_follow"] is True
    assert F.normalize_follow_settings({"uvremap_auto_follow_uv": False})["uvremap_auto_follow_uv"] is False

    assert F.normalize_follow_settings({"uvremap_follow_settle_s": "2"})["uvremap_follow_settle_s"] == 2.0
    assert F.normalize_follow_settings({"uvremap_follow_settle_s": 5})["uvremap_follow_settle_s"] == 5.0
    assert F.normalize_follow_settings({"uvremap_follow_settle_s": 0.4})["uvremap_follow_settle_s"] == 0.5
    assert F.normalize_follow_settings({"uvremap_follow_settle_s": 30.0})["uvremap_follow_settle_s"] == 30.0
    assert F.normalize_follow_settings({"uvremap_follow_settle_s": 30.1})["uvremap_follow_settle_s"] == 30.0
    assert F.normalize_follow_settings({"uvremap_follow_settle_s": float("nan")})["uvremap_follow_settle_s"] == 2.0
    assert F.normalize_follow_settings({"uvremap_uv_settle_s": float("inf")})["uvremap_uv_settle_s"] == 3.0
    assert F.normalize_follow_settings({"uvremap_uv_settle_s": 0.5})["uvremap_uv_settle_s"] == 1.0
    assert F.normalize_follow_settings({"uvremap_uv_settle_s": 60.0})["uvremap_uv_settle_s"] == 60.0
    assert F.normalize_follow_settings({"uvremap_uv_settle_s": 61})["uvremap_uv_settle_s"] == 60.0
    assert F.normalize_follow_settings({"uvremap_uv_poll_s": 0.9})["uvremap_uv_poll_s"] == 1.0
    assert F.normalize_follow_settings({"uvremap_uv_poll_s": 1.0})["uvremap_uv_poll_s"] == 1.0
    assert F.normalize_follow_settings({"uvremap_uv_poll_s": 31.0})["uvremap_uv_poll_s"] == 30.0


# ---------------------------------------------------------------------------
# decide_follow
# ---------------------------------------------------------------------------
def test_decide_follow_converts_only_changed_ts_after_settle():
    got = _follow(recorded_sigs={
        "Body": {"Body_BaseColor.png": [1, 10]},
        "Head": {"Head_BaseColor.png": [2, 19]},
    })
    assert got == F.FollowDecision(True, ("Head",), (), "")


def test_decide_follow_settle_before_and_after():
    before = _follow(now_ns=10 * NS, last_change_seen_ns=9 * NS, settle_s=2.0)
    assert before == F.FollowDecision(False, (), (), F.REASON_SETTLING)
    exact = _follow(now_ns=10 * NS, last_change_seen_ns=8 * NS, settle_s=2.0)
    assert exact.ready is True and exact.ts_to_convert == ("Body", "Head")
    assert _follow(flag_mtime_ns=40, last_flag_ns=40).ready is False
    assert _follow(flag_mtime_ns=40, last_flag_ns=40).reason == F.REASON_FLAG_SAME
    assert _follow(last_flag_ns=None).ready is True


@pytest.mark.parametrize("over,reason", [
    ({"enabled": False}, F.REASON_DISABLED),
    ({"auto_follow": False}, F.REASON_AUTO_OFF),
    ({"has_baseline": False}, F.REASON_NO_BASELINE),
])
def test_decide_follow_global_conditions(over, reason):
    got = _follow(**over)
    assert got.ready is True
    assert got.ts_to_convert == () and got.ts_to_pend == ()
    assert got.reason == reason


def test_decide_follow_states_and_scene_and_pending():
    same = {"Body_BaseColor.png": [1, 10]}
    changed = {"Body_BaseColor.png": [1, 11]}
    for state in (F.STATE_ORIGINAL, F.STATE_FAILED, F.STATE_BLOCKED):
        got = _follow(
            ts_states={"Body": state},
            current_sigs={"Body": changed},
            recorded_sigs={"Body": same},
            same_scene={"Body": True},
        )
        assert got.ts_to_convert == () and got.ts_to_pend == ()

    stale = _follow(
        ts_states={"Body": F.STATE_STALE},
        current_sigs={"Body": changed},
        recorded_sigs={"Body": same},
        same_scene={"Body": True},
    )
    assert stale.ts_to_convert == ("Body",) and stale.ts_to_pend == ()

    pending = _follow(
        ts_states={"Body": F.STATE_CONVERTING, "Head": F.STATE_CONVERTED_OK},
        recorded_sigs={
            "Body": {"Body_BaseColor.png": [1, 9]},
            "Head": {"Head_BaseColor.png": [2, 20]},
        },
    )
    assert pending.ts_to_convert == () and pending.ts_to_pend == ("Body",)

    unchanged_pending = _follow(
        ts_states={"Body": F.STATE_CONVERTING},
        current_sigs={"Body": same},
        recorded_sigs={"Body": same},
        same_scene={"Body": True},
    )
    assert unchanged_pending.ts_to_pend == ()

    other_scene = _follow(same_scene={"Body": True, "Head": False})
    assert other_scene.ts_to_convert == ("Body",) and other_scene.ts_to_pend == ()
    assert _follow(same_scene=False).ts_to_convert == ()
    assert _follow(same_scene=True).ts_to_convert == ("Body", "Head")

    disabled = _follow(enabled=False, ts_states={"Body": F.STATE_CONVERTING})
    assert disabled.ts_to_pend == ()


# ---------------------------------------------------------------------------
# UV fingerprint / UV follow
# ---------------------------------------------------------------------------
def test_uv_fingerprint_stable_and_length():
    u = np.array([0.0, 1.0, 0.5], dtype=np.float64)
    v = np.array([1.0, 0.0, 0.25], dtype=np.float64)
    first = F.uv_fingerprint(u, v)
    assert first == F.uv_fingerprint(u.tolist(), v.tolist())
    assert first[1] == 3
    changed = u.copy()
    changed[1] = 1.5
    assert F.uv_fingerprint(changed, v) != first
    assert F.uv_fingerprint(v, u) != first
    assert F.uv_fingerprint([], []) == (zlib.crc32(b"") & 0xFFFFFFFF, 0)
    with pytest.raises(ValueError):
        F.uv_fingerprint([0.0, 1.0], [0.0])


def _uv(**over):
    args = dict(
        now=12,
        fingerprints_now={"mesh": (2, 4), "other": (3, 4)},
        fingerprints_recorded={"mesh": (1, 4), "other": (3, 4)},
        last_change_at=9,
        settle_s=3,
        ts_by_shape={"mesh": ["Trim", "Body"], "other": ["Head"]},
        ts_states={
            "Body": F.STATE_CONVERTED_OK,
            "Trim": F.STATE_ORIGINAL,
            "Head": F.STATE_CONVERTED_OK,
            "Prop": F.STATE_STALE,
        },
        enabled=True,
        auto_follow_uv=True,
    )
    args.update(over)
    return F.decide_uv_follow(**args)


def test_decide_uv_follow_shape_settle_and_killswitch():
    before = _uv(now=11, last_change_at=9, settle_s=3)
    assert before.ts_to_stale == ("Body", "Trim")
    assert before.ts_to_convert == ()
    assert before.reason == F.REASON_UV_SETTLING

    after = _uv(now=12, last_change_at=9, settle_s=3)
    assert after.ts_to_stale == ("Body", "Trim")
    assert after.ts_to_convert == ("Body", "Prop")
    assert after.reason == ""

    off = _uv(auto_follow_uv=False)
    assert off.ts_to_stale == ("Body", "Trim") and off.ts_to_convert == ()

    stopped = _uv(enabled=False)
    assert stopped == F.UVFollowDecision((), (), F.REASON_DISABLED)

    quiet = _uv(
        fingerprints_now={"mesh": (1, 4)},
        fingerprints_recorded={"mesh": (1, 4)},
        ts_by_shape={"mesh": ["Body"]},
        ts_states={"Body": F.STATE_CONVERTED_OK},
    )
    assert quiet.ts_to_stale == () and quiet.ts_to_convert == ()
    assert quiet.reason == F.REASON_UV_UNCHANGED

    missing = _uv(
        fingerprints_recorded={},
        ts_states={"Body": F.STATE_CONVERTED_OK, "Trim": F.STATE_CONVERTED_OK},
        ts_by_shape={"mesh": ["Body", "Trim"]},
    )
    assert missing.ts_to_stale == ()

    waiting = _uv(last_change_at=None)
    assert waiting.ts_to_convert == () and waiting.reason == F.REASON_UV_SETTLING


# ---------------------------------------------------------------------------
# skip / input stale / fold label
# ---------------------------------------------------------------------------
def test_should_skip_auto_all_branches():
    sig = {"a.png": [1, 2]}
    assert F.should_skip_auto("h", "h", sig, {"a.png": [1, 2]}) is True
    assert F.should_skip_auto("h", "h", sig, {"a.png": (1, 2)}) is True
    assert F.should_skip_auto("h", "x", sig, dict(sig)) is False
    assert F.should_skip_auto("h", "h", sig, {"a.png": [1, 3]}) is False
    assert F.should_skip_auto("h", "x", sig, {"a.png": [9, 9]}) is False
    assert F.should_skip_auto(None, None, None, None) is True
    assert F.should_skip_auto("h", "h", None, {}) is False
    assert F.should_skip_auto("h", "h", {}, {}) is True


def test_input_stale_all_branches():
    now = {"a.png": [1, 2]}
    old = {"a.png": [1, 3]}
    assert F.input_stale(F.STATE_CONVERTED_OK, now, old, False) is True
    assert F.input_stale(F.STATE_CONVERTED_OK, now, old, True) is False
    for state in (
        F.STATE_STALE, F.STATE_ORIGINAL, F.STATE_FAILED,
        F.STATE_BLOCKED, F.STATE_CONVERTING,
    ):
        assert F.input_stale(state, now, old, False) is False
    assert F.input_stale(F.STATE_CONVERTED_OK, now, {"a.png": [1, 2]}, False) is False
    assert F.input_stale(F.STATE_CONVERTED_OK, None, old, False) is True


def test_fold_status_label_p2_priority():
    assert F.fold_status_label_p2(False, True, True, True) == "無効"
    assert F.fold_status_label_p2(True, True, True, True) == "変換中"
    assert F.fold_status_label_p2(True, False, True, True) == "要反映"
    assert F.fold_status_label_p2(True, False, False, True) == "自動追従"
    assert F.fold_status_label_p2(True, False, False, False) == "有効"


def test_copy_constants_match_contract():
    assert F.MSG_PAINTER_START_FMT == "Painter の書き出しを検知しました。変換を開始します({ts})"
    assert F.MSG_PEND_FMT == "変換中のため、完了後にもう一度変換します({ts})"
    assert F.MSG_QUEUE_FMT == "順番待ちです({ts})"
    assert F.MSG_NO_CHANGE == "変更はありません"
    assert F.MSG_INPUT_STALE == "Painter の書き出しが新しくなりました。『反映する』を押してください"
    assert F.MSG_UV_STALE == "UV が変更されました。『反映する』を押してください"
    assert F.LABEL_NEEDS_APPLY == "要反映"
    assert F.LABEL_AUTO_FOLLOW == "自動追従"
    assert F.LABEL_APPLY == "変更を反映する"
    assert F.LABEL_CHECK_PAINTER == "Painter の書き出しに自動で追従する"
    assert F.LABEL_CHECK_UV == "UV の編集が落ち着いたら自動で反映する"
    assert F.MSG_AUTO_INPUT_AS_IS == "自動変換の入力は Painter の書き出し結果をそのまま使います"
    assert F.MSG_PAINTER_START_FMT.format(ts="Body") == (
        "Painter の書き出しを検知しました。変換を開始します(Body)"
    )
    assert F.LABEL_FOLD_OFF == "無効"
    assert F.LABEL_FOLD_BUSY == "変換中"
    assert F.LABEL_FOLD_ON == "有効"
