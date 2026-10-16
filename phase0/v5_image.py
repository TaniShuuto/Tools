# -*- coding: utf-8 -*-
"""Final 出力画像の解析ロジック(Maya/Painter 非依存)。pytest 対象。"""
from __future__ import annotations

import os
import re
import struct

KNOWN_SUFFIXES = (
    "BaseColor",
    "Roughness",
    "Metallic",
    "Normal",
    "Height",
    "Emissive",
)

# $textureSet_<Suffix>(.$udim) → 複数タイル時のみ .<UDIM>
# 例: Body_BaseColor.1001.png / 単一タイル: Body_BaseColor.png
FILENAME_RE = re.compile(
    r"^(?P<ts>.+)_(?P<suffix>[A-Za-z0-9]+)"
    r"(?:\.(?P<tile>\d{4}))?\.(?P<ext>[A-Za-z0-9]+)$"
)

PNG_COLOR = {
    0: {"mode": "L", "channels": ("L",), "name": "grayscale"},
    2: {"mode": "RGB", "channels": ("R", "G", "B"), "name": "truecolor"},
    3: {"mode": "P", "channels": ("P",), "name": "indexed"},
    4: {"mode": "LA", "channels": ("L", "A"), "name": "grayscale_alpha"},
    6: {"mode": "RGBA", "channels": ("R", "G", "B", "A"), "name": "truecolor_alpha"},
}

EXPECTED_CHANNELS = {
    "BaseColor": ("R", "G", "B"),
    "Roughness": ("L",),
    "Metallic": ("L",),
    "Normal": ("R", "G", "B"),
    "Height": ("L",),
    "Emissive": ("R", "G", "B"),
}


def parse_filename(name):
    base = os.path.basename(name)
    match = FILENAME_RE.match(base)
    if not match:
        return {
            "filename": base,
            "matched": False,
            "texture_set": None,
            "suffix": None,
            "tile": None,
            "ext": os.path.splitext(base)[1].lstrip(".").lower() or None,
            "udim_named": False,
            "known_suffix": False,
        }
    suffix = match.group("suffix")
    tile = match.group("tile")
    return {
        "filename": base,
        "matched": True,
        "texture_set": match.group("ts"),
        "suffix": suffix,
        "tile": int(tile) if tile else None,
        "ext": match.group("ext").lower(),
        "udim_named": tile is not None,
        "known_suffix": suffix in KNOWN_SUFFIXES,
    }


def parse_png_header(path):
    """Pillow 無しで IHDR を読む。失敗は error に残す。"""
    info = {
        "ok": False,
        "width": None,
        "height": None,
        "bit_depth": None,
        "color_type": None,
        "mode": None,
        "channels": None,
        "color_name": None,
        "error": None,
    }
    try:
        with open(path, "rb") as handle:
            sig = handle.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                info["error"] = {"type": "ValueError", "message": "not_png_signature"}
                return info
            length = handle.read(4)
            ctype = handle.read(4)
            if len(length) != 4 or ctype != b"IHDR":
                info["error"] = {"type": "ValueError", "message": "missing_ihdr"}
                return info
            n = struct.unpack(">I", length)[0]
            data = handle.read(n)
            if len(data) < 13:
                info["error"] = {"type": "ValueError", "message": "short_ihdr"}
                return info
            width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
            info["width"] = width
            info["height"] = height
            info["bit_depth"] = bit_depth
            info["color_type"] = color_type
            meta = PNG_COLOR.get(color_type)
            if meta:
                info["mode"] = meta["mode"]
                info["channels"] = list(meta["channels"])
                info["color_name"] = meta["name"]
            info["ok"] = True
            return info
    except Exception as exc:
        info["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        return info


def analyze_with_pillow(path):
    try:
        from PIL import Image
    except Exception as exc:
        return {"ok": False, "available": False, "error": {
            "type": type(exc).__name__, "message": str(exc)
        }}
    try:
        with Image.open(path) as img:
            mode = img.mode
            size = img.size
            depth = None
            if hasattr(img, "bits"):
                depth = img.bits
            info = img.info or {}
            if depth is None and "bits" in info:
                depth = info.get("bits")
            return {
                "ok": True,
                "available": True,
                "mode": mode,
                "width": size[0],
                "height": size[1],
                "bit_depth": depth,
                "format": img.format,
                "bands": list(img.getbands()) if hasattr(img, "getbands") else None,
            }
    except Exception as exc:
        return {"ok": False, "available": True, "error": {
            "type": type(exc).__name__, "message": str(exc)
        }}


def extract_plugin_export_spec(plugin_text):
    """sp_live_sync_plugin.py の $textureSet_* 周辺から期待値を抜く。"""
    maps = []
    for match in re.finditer(
        r'\{\s*"fileName":\s*"(\$textureSet_([^"(]+)\(\.\$udim\))"',
        plugin_text,
    ):
        template = match.group(1)
        suffix = match.group(2)
        # 直後の channels ブロック
        tail = plugin_text[match.end(): match.end() + 400]
        dest = re.findall(r'"destChannel":\s*"([A-Z])"', tail)
        src_map = re.findall(r'"srcMapName":\s*"([^"]+)"', tail)
        maps.append({
            "suffix": suffix,
            "template": template,
            "dest_channels": dest,
            "src_map_name": src_map[0] if src_map else None,
        })
    file_format = None
    bit_depth = None
    final_log2 = None
    m_fmt = re.search(r'"file_format":\s*"([^"]+)"', plugin_text)
    m_bit = re.search(r'"bit_depth":\s*"([^"]+)"', plugin_text)
    m_log = re.search(r'"final_resolution_log2":\s*(\d+)', plugin_text)
    if m_fmt:
        file_format = m_fmt.group(1)
    if m_bit:
        bit_depth = m_bit.group(1)
    if m_log:
        final_log2 = int(m_log.group(1))
    return {
        "maps": maps,
        "file_format_default": file_format,
        "bit_depth_default": bit_depth,
        "final_resolution_log2_default": final_log2,
        "final_px_default": (2 ** final_log2) if final_log2 is not None else None,
        "naming_rule": "$textureSet_<Suffix>(.$udim) 複数タイル時のみ .<UDIM>",
        "expected_channels": {k: list(v) for k, v in EXPECTED_CHANNELS.items()},
    }


def compare_to_expected(file_info, spec):
    notes = []
    mismatches = []
    parsed = file_info.get("filename_parse") or {}
    suffix = parsed.get("suffix")
    ext = parsed.get("ext")
    if spec.get("file_format_default") and ext:
        if ext != spec["file_format_default"]:
            mismatches.append("ext_ne_default_{0}".format(spec["file_format_default"]))
        else:
            notes.append("ext_matches_default")
    png = file_info.get("png") or {}
    if png.get("ok") and spec.get("bit_depth_default"):
        try:
            expected_bits = int(spec["bit_depth_default"])
        except (TypeError, ValueError):
            expected_bits = None
        if expected_bits is not None and png.get("bit_depth") != expected_bits:
            mismatches.append("bit_depth_{0}_ne_{1}".format(png.get("bit_depth"), expected_bits))
        elif expected_bits is not None:
            notes.append("bit_depth_matches_default")
    if suffix in EXPECTED_CHANNELS and png.get("channels"):
        got = tuple(png["channels"])
        exp = EXPECTED_CHANNELS[suffix]
        # RGB 期待に RGBA が来た場合はアルファ付きとして記録(不一致扱い)
        if got != exp:
            mismatches.append("channels_{0}_ne_{1}".format("".join(got), "".join(exp)))
        else:
            notes.append("channels_match_export_map")
    return {"notes": notes, "mismatches": mismatches}


def analyze_image(path, spec=None):
    name = os.path.basename(path)
    parsed = parse_filename(name)
    png = None
    pillow = None
    if parsed.get("ext") == "png" or name.lower().endswith(".png"):
        png = parse_png_header(path)
    pillow = analyze_with_pillow(path)
    size_bytes = None
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        size_bytes = None
    row = {
        "path": name,
        "filename_parse": parsed,
        "size_bytes": size_bytes,
        "png": png,
        "pillow": pillow,
    }
    if spec is not None:
        row["vs_expected"] = compare_to_expected(row, spec)
    return row


def analyze_folder(folder, spec=None):
    rows = []
    names = []
    try:
        names = sorted(os.listdir(folder))
    except Exception as exc:
        return {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "files": [],
        }
    for name in names:
        full = os.path.join(folder, name)
        if not os.path.isfile(full):
            continue
        if name.startswith("."):
            continue
        if name.endswith(".flag") or name.endswith(".json"):
            continue
        rows.append(analyze_image(full, spec))
    return {"ok": True, "n_files": len(rows), "files": rows}
