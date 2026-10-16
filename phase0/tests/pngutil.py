# -*- coding: utf-8 -*-
"""テスト用の最小 PNG 書き出し(標準ライブラリのみ)。"""
from __future__ import annotations

import struct
import zlib


def chunk(tag, data):
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def write_png(path, width, height, rows, color_type=2, bit_depth=8):
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as handle:
        handle.write(data)
