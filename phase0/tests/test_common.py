# -*- coding: utf-8 -*-
import json
import os

import common


def test_anonymize_path_replaces_home(tmp_path, monkeypatch):
    home = str(tmp_path / "Someone")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("USER", "Someone")
    monkeypatch.setenv("USERNAME", "Someone")
    target = os.path.join(home, "Documents", "file.png")
    out = common.anonymize_path(target)
    assert "Someone" not in out
    assert out.startswith("~")
    assert "file.png" in out.replace("\\", "/")


def test_anonymize_text_strips_email_and_user(tmp_path, monkeypatch):
    home = str(tmp_path / "Pat")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("USER", "Pat")
    monkeypatch.setenv("USERNAME", "Pat")
    text = "boom at {0} contact a.b@example.com".format(home)
    out = common.anonymize_text(text)
    assert "example.com" not in out
    assert "<email>" in out
    assert home not in out
    assert "Pat" not in out


def test_safe_getattr_and_dir():
    class Box:
        x = 3

        def __dir__(self):
            return ["x", "y"]

    box = Box()
    assert common.safe_getattr(box, "x") == 3
    err = common.safe_getattr(box, "missing")
    assert err["_getattr_error"]["type"] == "AttributeError"
    names = common.safe_dir(box)
    assert "x" in names


def test_exception_observation_has_type_and_message():
    try:
        raise ValueError("nope")
    except ValueError as exc:
        obs = common.exception_observation(exc)
    assert obs["type"] == "ValueError"
    assert "nope" in obs["message"]


def test_make_check_and_write_results_merge(tmp_path):
    path = str(tmp_path / "out.json")
    first = common.make_check(common.STATUS_PASS, "one", {"n": 1})
    common.write_results(path, {"V1": first}, env={"python_version": "3.x"})
    second = common.make_check(common.STATUS_SKIPPED, "two", {"n": 2})
    payload = common.write_results(path, {"V8": second}, env={"python_version": "3.x"})
    assert payload["schema"] == 1
    assert "V1" in payload["checks"]
    assert "V8" in payload["checks"]
    assert payload["checks"]["V1"]["status"] == "PASS"
    assert payload["path"]
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["checks"]["V8"]["status"] == "SKIPPED"


def test_collect_env_has_no_raw_home():
    env = common.collect_env()
    home = os.path.expanduser("~")
    dumped = json.dumps(env)
    # ホーム絶対パスが残っていたら失敗
    if len(home) > 3:
        assert home.replace("\\", "/") not in dumped.replace("\\", "/")


def test_probe_module_missing():
    info = common.probe_module("definitely_not_a_real_module_phase0")
    assert info["installed"] is False
    assert info["error"]["type"]


def test_json_safe_enum_like():
    class E:
        name = "SUCCESS"
        value = 1

    out = common.json_safe(E())
    assert out["name"] == "SUCCESS"
    assert out["value"] == 1
