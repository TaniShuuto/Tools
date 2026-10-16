# -*- coding: utf-8 -*-
from v5_image import (
    analyze_folder,
    analyze_image,
    compare_to_expected,
    extract_plugin_export_spec,
    parse_filename,
    parse_png_header,
)
from pngutil import write_png


PLUGIN_SNIPPET = '''
DEFAULT_CONFIG = {
    "file_format": "png",
    "bit_depth": "8",
    "final_resolution_log2": 12,
}
maps = [
    {"fileName": "$textureSet_BaseColor(.$udim)", "channels": [
        {"destChannel": c, "srcChannel": c, "srcMapType": "documentMap", "srcMapName": "basecolor"}
        for c in ("R", "G", "B")
    ]},
    {"fileName": "$textureSet_Roughness(.$udim)", "channels": [
        {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "roughness"}
    ]},
]
'''


def test_parse_filename_udim_and_single():
    tiled = parse_filename("Body_BaseColor.1001.png")
    assert tiled["matched"] is True
    assert tiled["texture_set"] == "Body"
    assert tiled["suffix"] == "BaseColor"
    assert tiled["tile"] == 1001
    assert tiled["udim_named"] is True
    assert tiled["known_suffix"] is True

    single = parse_filename("Body_BaseColor.png")
    assert single["matched"] is True
    assert single["tile"] is None
    assert single["udim_named"] is False

    other = parse_filename("notes.txt")
    assert other["matched"] is False


def test_parse_png_header_rgb_and_gray(tmp_path):
    rgb = tmp_path / "Body_BaseColor.1001.png"
    write_png(
        str(rgb), 2, 2,
        [b"\x10\x20\x30\x11\x21\x31", b"\x12\x22\x32\x13\x23\x33"],
        color_type=2, bit_depth=8,
    )
    info = parse_png_header(str(rgb))
    assert info["ok"] is True
    assert info["width"] == 2
    assert info["height"] == 2
    assert info["bit_depth"] == 8
    assert info["mode"] == "RGB"
    assert info["channels"] == ["R", "G", "B"]

    gray = tmp_path / "Body_Roughness.png"
    write_png(str(gray), 1, 1, [b"\x80"], color_type=0, bit_depth=8)
    g = parse_png_header(str(gray))
    assert g["ok"] is True
    assert g["mode"] == "L"
    assert g["channels"] == ["L"]


def test_parse_png_header_rejects_non_png(tmp_path):
    path = tmp_path / "x.png"
    path.write_bytes(b"not a png")
    info = parse_png_header(str(path))
    assert info["ok"] is False
    assert info["error"]["message"] == "not_png_signature"


def test_extract_plugin_export_spec_from_snippet():
    spec = extract_plugin_export_spec(PLUGIN_SNIPPET)
    suffixes = [m["suffix"] for m in spec["maps"]]
    assert "BaseColor" in suffixes
    assert "Roughness" in suffixes
    assert spec["file_format_default"] == "png"
    assert spec["bit_depth_default"] == "8"
    assert spec["final_resolution_log2_default"] == 12
    assert spec["final_px_default"] == 4096


def test_extract_plugin_export_spec_from_repo():
    import os
    plugin = os.path.abspath(os.path.join(
        os.path.dirname(__file__), os.pardir, os.pardir,
        "sp_maya_livesync", "sp_live_sync_plugin.py",
    ))
    assert os.path.isfile(plugin)
    with open(plugin, "r", encoding="utf-8") as handle:
        spec = extract_plugin_export_spec(handle.read())
    suffixes = [m["suffix"] for m in spec["maps"]]
    assert suffixes == [
        "BaseColor", "Roughness", "Metallic", "Normal", "Height", "Emissive",
    ]
    assert spec["file_format_default"] == "png"
    assert spec["bit_depth_default"] == "8"


def test_analyze_folder_matches_expected(tmp_path):
    spec = extract_plugin_export_spec(PLUGIN_SNIPPET)
    rgb = tmp_path / "Body_BaseColor.1001.png"
    write_png(
        str(rgb), 2, 2,
        [b"\x10\x20\x30\x11\x21\x31", b"\x12\x22\x32\x13\x23\x33"],
        color_type=2, bit_depth=8,
    )
    gray = tmp_path / "Body_Roughness.png"
    write_png(str(gray), 1, 1, [b"\x40"], color_type=0, bit_depth=8)
    result = analyze_folder(str(tmp_path), spec)
    assert result["ok"] is True
    assert result["n_files"] == 2
    mismatches = []
    for row in result["files"]:
        mismatches.extend((row.get("vs_expected") or {}).get("mismatches") or [])
    assert mismatches == []


def test_compare_detects_channel_mismatch(tmp_path):
    spec = extract_plugin_export_spec(PLUGIN_SNIPPET)
    # Roughness なのに RGB
    path = tmp_path / "Body_Roughness.png"
    write_png(str(path), 1, 1, [b"\x01\x02\x03"], color_type=2, bit_depth=8)
    row = analyze_image(str(path), spec)
    assert row["vs_expected"]["mismatches"]


def test_v5_run_check_empty_folder_is_inconclusive(tmp_path):
    from v5_final_format import run_check

    check = run_check(str(tmp_path))
    assert check["status"] == "INCONCLUSIVE"
