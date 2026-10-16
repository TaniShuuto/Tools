# -*- coding: utf-8 -*-
import os

import v8_replace_lock


def test_run_check_skipped_off_windows(monkeypatch):
    monkeypatch.setattr(v8_replace_lock.os, "name", "posix")
    check = v8_replace_lock.run_check()
    assert check["status"] == "SKIPPED"
    assert check["observations"]["skipped_reason"] == "not_windows"


def test_try_replace_overwrites(tmp_path):
    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"new")
    dest.write_bytes(b"old")
    result = v8_replace_lock.try_replace(str(src), str(dest))
    assert result["replaced"] is True
    assert dest.read_bytes() == b"new"
    assert result["error"] is None


def test_try_replace_records_failure(tmp_path):
    src = tmp_path / "missing.bin"
    dest = tmp_path / "dest.bin"
    dest.write_bytes(b"old")
    result = v8_replace_lock.try_replace(str(src), str(dest))
    assert result["replaced"] is False
    assert result["error"]["type"]


def test_run_lock_cases_table_shape_on_linux():
    table = v8_replace_lock.run_lock_cases()
    if os.name != "nt":
        assert table["cases"] == []
        assert table["skipped_reason"] == "not_windows"
    else:
        keys = [c["key"] for c in table["cases"]]
        assert "a_pil_open" in keys
        assert "b_open_rb" in keys
        assert "c_numpy_read_closed" in keys
