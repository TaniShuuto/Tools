# -*- coding: utf-8 -*-
from common import STATUS_PASS, make_check, write_results


def test_result_schema_keys(tmp_path):
    path = str(tmp_path / "phase0_results_misc.json")
    check = make_check(STATUS_PASS, "ok", {"k": 1})
    payload = write_results(path, {"V5": check}, env={"python_version": "3"})
    assert set(payload.keys()) >= {"schema", "env", "checks"}
    assert payload["schema"] == 1
    assert payload["checks"]["V5"]["status"] == "PASS"
    assert "summary" in payload["checks"]["V5"]
    assert "observations" in payload["checks"]["V5"]
