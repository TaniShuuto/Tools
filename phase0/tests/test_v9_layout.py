# -*- coding: utf-8 -*-
"""V9b: UV 配置とマテリアル割り当ての手順を、Maya 無しで確認する(偽の cmds)。"""
import v9_triangulated_fbx as v9


def test_bbox_helpers():
    assert v9.uv_bbox([], []) is None
    assert v9.uv_bbox([0.2, 0.5], [0.1, 0.9]) == (0.2, 0.1, 0.5, 0.9)
    a = (0.05, 0.05, 0.45, 0.45)
    b = (0.55, 0.55, 0.95, 0.95)
    assert v9.bboxes_overlap(a, b) is False
    assert v9.bboxes_overlap(a, (0.4, 0.4, 0.9, 0.9)) is True
    # 辺が接するだけなら重ならない
    assert v9.bboxes_overlap((0, 0, 0.5, 0.5), (0.5, 0, 1, 0.5)) is False
    assert v9.bboxes_overlap(None, a) is None
    assert v9.bbox_inside_unit(a) and not v9.bbox_inside_unit((0, 0, 1.2, 1))
    assert not v9.bbox_inside_unit(None)


class FakeCmds(object):
    """run_maya_cases が呼ぶ関数だけを模す。"""

    def __init__(self):
        self.log = []
        self.uv_edits = {}  # transform -> [("scale", s, pivot) / ("move", du, dv)]
        self.sg_of = {}

    def file(self, **kw):
        self.log.append(("file", kw))

    def polyPlane(self, **kw):
        return [kw["name"], "polyPlane1"]

    def polyCube(self, **kw):
        return [kw["name"], "polyCube1"]

    def shadingNode(self, kind, asShader, name):
        self.log.append(("shadingNode", kind, name))
        return name

    def sets(self, *args, **kw):
        if kw.get("empty"):
            self.log.append(("sg", kw["name"]))
            return kw["name"]
        if kw.get("forceElement"):
            self.sg_of[args[0]] = kw["forceElement"]
        return None

    def connectAttr(self, src, dst, force=False):
        self.log.append(("connect", src, dst))

    def polyEditUV(self, uvs, **kw):
        node = uvs.split(".")[0]
        edits = self.uv_edits.setdefault(node, [])
        if "scaleU" in kw:
            assert kw["scaleU"] == kw["scaleV"]
            edits.append(("scale", kw["scaleU"], (kw["pivotU"], kw["pivotV"])))
        else:
            edits.append(("move", kw["u"], kw["v"]))


def _expected_uvs(edits):
    """既定 UV(0〜1 の四隅)に、記録した編集を順に適用した U・V の値。"""
    pts = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
    for e in edits:
        if e[0] == "scale":
            _, s, (pu, pv) = e
            pts = [(pu + (u - pu) * s, pv + (v - pv) * s) for u, v in pts]
        else:
            pts = [(u + e[1], v + e[2]) for u, v in pts]
    return [p[0] for p in pts], [p[1] for p in pts]


def test_run_maya_cases_layout_and_materials(monkeypatch, tmp_path):
    fake = FakeCmds()
    monkeypatch.setattr(v9, "new_scratch_scene", lambda cmds: True)
    monkeypatch.setattr(v9, "face_vertex_counts", lambda cmds, node: ([4, 4, 4, 4], "fake"))
    monkeypatch.setattr(v9, "uv_values", lambda cmds, node: _expected_uvs(fake.uv_edits[node]))
    exported = {}

    def fake_export(cmds, dest, nodes):
        exported["dest"] = dest
        exported["nodes"] = list(nodes)
        return {"ok": True}

    monkeypatch.setattr(v9, "export_triangulated_fbx", fake_export)
    obs = v9.run_maya_cases(fake, str(tmp_path))

    assert obs["materials"]["plane"] == {"shader": "phase0_matA", "shading_group": "phase0_sgA"}
    assert obs["materials"]["cube"] == {"shader": "phase0_matB", "shading_group": "phase0_sgB"}
    assert fake.sg_of["phase0_v9_plane"] == "phase0_sgA"
    assert fake.sg_of["phase0_v9_cube"] == "phase0_sgB"
    # シェーダー名とシェーディンググループ名は別(名前の対応を確かめるため)
    assert v9.PLANE_SHADER != v9.PLANE_SG and v9.CUBE_SHADER != v9.CUBE_SG

    layout = obs["uv_layout"]
    assert layout["overlap"] is False
    assert layout["inside_0_1"] is True
    pb, cb = layout["plane"]["bbox"], layout["cube"]["bbox"]
    assert [round(x, 6) for x in pb] == [0.05, 0.05, 0.45, 0.45]
    assert [round(x, 6) for x in cb] == [0.55, 0.55, 0.95, 0.95]
    assert exported["dest"].endswith(v9.FBX_NAME)
    assert exported["nodes"] == ["phase0_v9_plane", "phase0_v9_cube"]


def test_check_fails_when_overlapping(monkeypatch, tmp_path):
    """配置が重なっていたら PASS にしない。"""
    fake = FakeCmds()
    monkeypatch.setattr(v9, "try_import_maya", lambda: (fake, {"fake": True}))
    monkeypatch.setattr(v9, "scratch_scene_ok", lambda cmds: {"ok": True})
    monkeypatch.setattr(v9, "resolve_phase0_dir", lambda override="": str(tmp_path))

    def overlapped(cmds, scratch_dir):
        return {
            "plane_has_quads": True,
            "cube_has_quads": True,
            "export": {"ok": True},
            "uv_layout": {"overlap": True, "inside_0_1": True},
        }

    monkeypatch.setattr(v9, "run_maya_cases", overlapped)
    assert v9.run_check()["status"] == v9.STATUS_FAIL

    def separated(cmds, scratch_dir):
        d = overlapped(cmds, scratch_dir)
        d["uv_layout"] = {"overlap": False, "inside_0_1": True}
        return d

    monkeypatch.setattr(v9, "run_maya_cases", separated)
    assert v9.run_check()["status"] == v9.STATUS_PASS
