# -*- coding: utf-8 -*-
"""1.4.4: os.replace の共有違反だけを、合計 1 秒以内で再試行する。

実際の time.sleep は使わない。_replace_with_retry は sleep_fn を注入し、
それを受け取れない書き込み関数は time.sleep 自体を差し替える。
"""
from __future__ import annotations

import errno
import json
import os
import sys
import time

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_TESTS)
for _p in (_PKG, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from plugin_patch import patch_plugin_attr  # noqa: E402
from test_sp_live_sync_plugin_protocol import _arm_export, plugin  # noqa: E402

_DELAYS = (0.05, 0.1, 0.2, 0.3, 0.35)
_SUCCESS_LOG = "共有違反のため置き換えを再試行し、{0}回目で成功しました: {1}"


def _forbid_real_sleep(monkeypatch):
    def _boom(_seconds):
        raise AssertionError("time.sleep を使った")

    monkeypatch.setattr(time, "sleep", _boom)


def _record_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    return slept


def _patch_replace(monkeypatch, fail_times, exc_factory, *, call_real=False):
    real = os.replace
    calls = {"n": 0}

    def _flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc_factory()
        if call_real:
            return real(src, dst)
        return None

    monkeypatch.setattr(os, "replace", _flaky)
    return calls


def _permission():
    return PermissionError(5, "アクセスが拒否されました")


def _with_winerror(winerror, err_no=13, message="sharing violation"):
    err = OSError(err_no, message)
    err.winerror = winerror
    return err


def test_delays_are_five_steps_totalling_one_second():
    assert plugin.REPLACE_RETRY_DELAYS_S == _DELAYS
    assert sum(plugin.REPLACE_RETRY_DELAYS_S) <= 1.0
    assert sum(plugin.REPLACE_RETRY_DELAYS_S) == 1.0


def test_first_success_does_not_sleep_or_log(monkeypatch):
    logs = []
    sleeps = []
    patch_plugin_attr(
        monkeypatch, plugin, "_log", lambda level, message: logs.append((level, message)))
    _forbid_real_sleep(monkeypatch)
    monkeypatch.setattr(os, "replace", lambda src, dst: None)
    plugin._replace_with_retry("a.tmp", "state.json", sleep_fn=sleeps.append)
    assert sleeps == []
    assert logs == []


def test_two_permission_errors_then_third_attempt_succeeds(tmp_path, monkeypatch):
    logs = []
    sleeps = []
    patch_plugin_attr(
        monkeypatch, plugin, "_log", lambda level, message: logs.append((level, message)))
    _forbid_real_sleep(monkeypatch)
    calls = _patch_replace(monkeypatch, 2, _permission)
    dst = os.path.join(str(tmp_path), "secret", "state.json")
    plugin._replace_with_retry(
        os.path.join(str(tmp_path), "secret", "state.json.tmp"),
        dst,
        sleep_fn=sleeps.append,
    )
    assert calls["n"] == 3
    assert sleeps == [0.05, 0.1]
    assert logs == [("info", _SUCCESS_LOG.format(3, "state.json"))]
    assert "secret" not in logs[0][1]
    assert str(tmp_path) not in logs[0][1]


def test_persistent_permission_error_is_raised_after_five_waits(monkeypatch):
    logs = []
    sleeps = []
    errors = []
    patch_plugin_attr(
        monkeypatch, plugin, "_log", lambda level, message: logs.append((level, message)))
    _forbid_real_sleep(monkeypatch)

    def _always(src, dst):
        err = _permission()
        errors.append(err)
        raise err

    monkeypatch.setattr(os, "replace", _always)
    with pytest.raises(PermissionError) as caught:
        plugin._replace_with_retry("a.tmp", "state.json", sleep_fn=sleeps.append)
    assert caught.value is errors[-1]
    assert type(caught.value) is PermissionError
    assert len(errors) == 6
    assert sleeps == list(_DELAYS)
    assert sum(sleeps) <= 1.0
    assert logs == []


@pytest.mark.parametrize("winerror", [32, 33])
def test_sharing_and_lock_winerrors_are_retried(monkeypatch, winerror):
    sleeps = []
    _forbid_real_sleep(monkeypatch)
    calls = _patch_replace(monkeypatch, 1, lambda: _with_winerror(winerror))
    plugin._replace_with_retry("a.tmp", "state.json", sleep_fn=sleeps.append)
    assert calls["n"] == 2
    assert sleeps == [0.05]


def test_other_oserrors_and_file_not_found_are_immediate(monkeypatch):
    sleeps = []
    _forbid_real_sleep(monkeypatch)
    missing = FileNotFoundError(2, "missing")
    cross = _with_winerror(17, err_no=17, message="not same device")
    exdev = OSError(errno.EXDEV, "Invalid cross-device link")
    full = OSError(errno.ENOSPC, "No space left on device")
    for err in (missing, cross, exdev, full):
        calls = {"n": 0}

        def _raise(src, dst, err=err):
            calls["n"] += 1
            raise err

        monkeypatch.setattr(os, "replace", _raise)
        with pytest.raises(type(err)) as caught:
            plugin._replace_with_retry("a.tmp", "state.json", sleep_fn=sleeps.append)
        assert caught.value is err
        assert calls["n"] == 1
    assert sleeps == []


def test_write_json_atomic_succeeds_after_one_permission_error(tmp_path, monkeypatch):
    path = tmp_path / "out.json"
    path.write_text('{"old": true}', encoding="utf-8")
    slept = _record_sleep(monkeypatch)
    calls = _patch_replace(monkeypatch, 1, _permission, call_real=True)
    plugin.write_json_atomic(str(path), {"schema": 1, "ok": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema": 1, "ok": True}
    assert not (tmp_path / "out.json.tmp").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]
    assert calls["n"] == 2
    assert slept == [0.05]


def test_write_json_atomic_drops_temp_when_permission_error_persists(tmp_path, monkeypatch):
    path = tmp_path / "out.json"
    path.write_text("keep", encoding="utf-8")
    slept = _record_sleep(monkeypatch)
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(_permission()))
    with pytest.raises(PermissionError):
        plugin.write_json_atomic(str(path), {"schema": 1})
    assert path.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "out.json.tmp").exists()
    assert slept == list(_DELAYS)
    assert sum(slept) <= 1.0


def _patch_config_paths(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    patch_plugin_attr(monkeypatch, plugin, "CONFIG_DIR", str(cfg))
    patch_plugin_attr(monkeypatch, plugin, "CONFIG_PATH", str(cfg / "live_sync_config.json"))
    patch_plugin_attr(monkeypatch, plugin, "CONFIG_LOCK_PATH", str(cfg / "live_sync_config.json.lock"))
    patch_plugin_attr(monkeypatch, plugin, "HISTORY_LOG_PATH", str(cfg / "history.log"))
    return cfg


def test_write_sync_flag_succeeds_after_one_permission_error(tmp_path, monkeypatch):
    flag = tmp_path / "_sync_complete.flag"
    slept = _record_sleep(monkeypatch)
    calls = _patch_replace(monkeypatch, 1, _permission, call_real=True)
    plugin.write_sync_flag(str(flag))
    text = flag.read_text(encoding="utf-8")
    assert float(text) > 0
    assert not (tmp_path / "_sync_complete.flag.tmp").exists()
    assert calls["n"] == 2
    assert slept == [0.05]


def test_write_sync_flag_drops_temp_when_permission_error_persists(tmp_path, monkeypatch):
    flag = tmp_path / "_sync_complete.flag"
    slept = _record_sleep(monkeypatch)
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(_permission()))
    with pytest.raises(PermissionError):
        plugin.write_sync_flag(str(flag))
    assert not flag.exists()
    assert not (tmp_path / "_sync_complete.flag.tmp").exists()
    assert slept == list(_DELAYS)


def test_save_config_succeeds_after_one_permission_error(tmp_path, monkeypatch):
    cfg = _patch_config_paths(monkeypatch, tmp_path)
    slept = _record_sleep(monkeypatch)
    calls = _patch_replace(monkeypatch, 1, _permission, call_real=True)
    plugin.save_config({"staging_dir": "C:/SPMayaLiveSync/staging"})
    disk = json.loads((cfg / "live_sync_config.json").read_text(encoding="utf-8"))
    assert disk["staging_dir"] == "C:/SPMayaLiveSync/staging"
    assert not (cfg / "live_sync_config.json.tmp").exists()
    assert calls["n"] == 2
    assert slept == [0.05]


def test_save_config_drops_temp_when_permission_error_persists(tmp_path, monkeypatch):
    cfg = _patch_config_paths(monkeypatch, tmp_path)
    target = cfg / "live_sync_config.json"
    target.write_text('{"keep": 1}', encoding="utf-8")
    slept = _record_sleep(monkeypatch)
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(_permission()))
    with pytest.raises(PermissionError):
        plugin.save_config({"staging_dir": "C:/SPMayaLiveSync/changed"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"keep": 1}
    assert not (cfg / "live_sync_config.json.tmp").exists()
    assert slept == list(_DELAYS)


def _make_engine(tmp_path, monkeypatch):
    _patch_config_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin.project, "is_open", lambda: True)
    monkeypatch.setattr(plugin.project, "is_busy", lambda: False)
    monkeypatch.setattr(plugin.project, "name", lambda: "Demo")
    monkeypatch.setattr(plugin.project, "file_path", lambda: "C:/work/Demo.spp")
    monkeypatch.setattr(plugin.textureset, "all_texture_sets", lambda: [])
    eng = plugin.LiveSyncEngine()
    eng.config["staging_dir"] = str(tmp_path / "staging")
    eng.config["watch_dir"] = str(tmp_path / "watch")
    eng.config["final_export_dir"] = str(tmp_path / "final")
    eng.config["uvremap_root_dir"] = str(tmp_path / "uvremap")
    (tmp_path / "staging").mkdir()
    (tmp_path / "watch").mkdir()
    (tmp_path / "final").mkdir()
    return eng


def test_texture_move_succeeds_after_one_sharing_violation(tmp_path, monkeypatch):
    logs = []
    patch_plugin_attr(
        monkeypatch, plugin, "_log", lambda level, message: logs.append((level, message)))
    eng = _make_engine(tmp_path, monkeypatch)
    slept = _record_sleep(monkeypatch)
    real = os.replace
    failed = {"n": 0}

    def _flaky(src, dst):
        if os.path.basename(dst) == "Body_BaseColor.png" and failed["n"] == 0:
            failed["n"] += 1
            raise _with_winerror(32)
        return real(src, dst)

    monkeypatch.setattr(os, "replace", _flaky)
    _arm_export(monkeypatch, tmp_path, "Success", payload=b"png-bytes")
    eng._do_export(preview=True)
    found = list((tmp_path / "watch").rglob("Body_BaseColor.png"))
    assert len(found) == 1
    assert found[0].read_bytes() == b"png-bytes"
    assert list((tmp_path / "watch").rglob("*.tmp")) == []
    assert slept == [0.05]
    retry_logs = [message for _level, message in logs if "再試行" in message]
    assert retry_logs == [_SUCCESS_LOG.format(2, "Body_BaseColor.png")]
    assert str(tmp_path) not in retry_logs[0]


def test_texture_move_cross_volume_skips_retry_and_copies(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path, monkeypatch)
    slept = _record_sleep(monkeypatch)
    real = os.replace
    failed = {"n": 0}

    def _flaky(src, dst):
        if os.path.basename(dst) == "Body_BaseColor.png" and failed["n"] == 0:
            failed["n"] += 1
            raise _with_winerror(17, err_no=17, message="not same device")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", _flaky)
    _arm_export(monkeypatch, tmp_path, "Success", payload=b"png-bytes")
    eng._do_export(preview=True)
    found = list((tmp_path / "watch").rglob("Body_BaseColor.png"))
    assert len(found) == 1
    assert found[0].read_bytes() == b"png-bytes"
    assert not (tmp_path / "Body_BaseColor.png").exists()
    assert list((tmp_path / "watch").rglob("*.tmp")) == []
    assert slept == []
    assert failed["n"] == 1


def test_texture_move_falls_back_to_copy_when_sharing_violation_persists(tmp_path, monkeypatch):
    # 1 つ目の置き換え(ステージング -> 監視先)が共有違反のまま再試行を使い切ると、
    # 既存の切り替えでコピー -> 2 つ目の置き換え(一時ファイル -> 監視先)へ進む。
    # 待ちは 1 つ目の 5 回(合計 1.0 秒)だけで、1 枚あたり 2.0 秒を超えない。
    eng = _make_engine(tmp_path, monkeypatch)
    slept = _record_sleep(monkeypatch)
    real = os.replace
    direct = {"n": 0}

    def _flaky(src, dst):
        if os.path.basename(dst) == "Body_BaseColor.png" and not str(src).endswith(".tmp"):
            direct["n"] += 1
            raise _with_winerror(32)
        return real(src, dst)

    monkeypatch.setattr(os, "replace", _flaky)
    _arm_export(monkeypatch, tmp_path, "Success", payload=b"png-bytes")
    eng._do_export(preview=True)
    found = list((tmp_path / "watch").rglob("Body_BaseColor.png"))
    assert len(found) == 1
    assert found[0].read_bytes() == b"png-bytes"
    assert list((tmp_path / "watch").rglob("*.tmp")) == []
    assert direct["n"] == len(_DELAYS) + 1
    assert slept == list(_DELAYS)
    assert sum(slept) <= 2.0
