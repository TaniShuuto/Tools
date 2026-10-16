# -*- coding: utf-8 -*-
import v1_numpy_mayapy
import v2_painter_api
import v7_udim_detect


def test_v1_collects_modules_without_installing():
    obs = v1_numpy_mayapy.collect_v1_observations()
    assert "numpy" in obs["modules"]
    assert "pip" in obs["modules"]
    assert "PIL" in obs["modules"]
    assert isinstance(obs["path_pythons"], list)
    check = v1_numpy_mayapy.run_check()
    assert check["status"] in ("PASS", "INCONCLUSIVE")
    assert "observations" in check


def test_v2_without_painter_is_inconclusive():
    check = v2_painter_api.run_check()
    assert check["status"] == "INCONCLUSIVE"
    assert "import_error" in check["observations"]


def test_v7_without_painter_is_inconclusive():
    check = v7_udim_detect.run_check()
    assert check["status"] == "INCONCLUSIVE"
    assert "import_error" in check["observations"]


def test_v6_run_check_without_maya():
    import v6_topology_fingerprint

    check = v6_topology_fingerprint.run_check()
    assert check["status"] == "INCONCLUSIVE"
