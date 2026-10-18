# -*- coding: utf-8 -*-
"""PNG のテスト用ヘルパー(テスト専用。uvremap_io の実装には依存しない)。

- 素朴な(画素単位の Python ループによる)参照フィルタ/デコーダ: PNG 仕様 9.2 をそのまま書いたもの。
  ``uvremap_io`` の読み込み(ベクトル化・反対角線処理)が、これと完全に一致することを検証する。
- ``build_png``: 参照フィルタで PNG を組み立てる(行ごとにフィルタ種別 0〜4 を指定できる)。
- ``fast_encode_png``: numpy でベクトル化したエンコーダ(4096² など大きな入力の作成用。8bit 限定)。
"""
from __future__ import annotations

import struct
import zlib

import numpy as np

PNG_SIG = b"\x89PNG\r\n\x1a\n"
CHANNELS_OF = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def png_chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def make_png_bytes(w: int, h: int, *, bit_depth: int, color_type: int, interlace: int = 0) -> bytes:
    """ゼロ埋めの画像本体を持つ PNG を組み立てる(未対応形式・壊れた入力の作成用)。"""
    channels = CHANNELS_OF[color_type]
    row = b"\x00" + bytes((w * channels * bit_depth + 7) // 8)
    return (PNG_SIG + png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bit_depth, color_type, 0, 0, interlace))
            + png_chunk(b"IDAT", zlib.compress(row * h)) + png_chunk(b"IEND", b""))


# ---- 素朴な(画素単位の Python ループによる)参照実装。PNG 仕様 9.2 をそのまま書いたもの ----
def ref_paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def ref_predict(ft: int, a: int, b: int, c: int) -> int:
    if ft == 0:
        return 0
    if ft == 1:
        return a
    if ft == 2:
        return b
    if ft == 3:
        return (a + b) // 2
    return ref_paeth(a, b, c)


def ref_filter(rows: list, bpp: int, ftypes: list) -> bytes:
    """画素の行(bytes のリスト)を、行ごとのフィルタ種別でフィルタした走査線列にする。"""
    out = bytearray()
    prev = bytes(len(rows[0]))
    for y, cur in enumerate(rows):
        ft = ftypes[y]
        out.append(ft)
        for i in range(len(cur)):
            a = cur[i - bpp] if i >= bpp else 0
            c = prev[i - bpp] if i >= bpp else 0
            out.append((cur[i] - ref_predict(ft, a, prev[i], c)) & 255)
        prev = cur
    return bytes(out)


def ref_unfilter(raw: bytes, w: int, h: int, bpp: int) -> list:
    """フィルタ済みの走査線列 -> 画素の行(bytes のリスト)。"""
    stride = w * bpp
    rows, prev, pos = [], bytes(stride), 0
    for _y in range(h):
        ft = raw[pos]
        cur = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        for i in range(stride):
            a = cur[i - bpp] if i >= bpp else 0
            c = prev[i - bpp] if i >= bpp else 0
            cur[i] = (cur[i] + ref_predict(ft, a, prev[i], c)) & 255
        prev = bytes(cur)
        rows.append(prev)
    return rows


def png_chunks(data: bytes):
    """PNG のバイト列を (tag, body) に分ける(CRC も検査する)。"""
    assert data[:8] == PNG_SIG
    pos, out = 8, []
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, "CRC 不一致: {0!r}".format(tag)
        out.append((tag, body))
        pos += 12 + length
    return out


def build_png(rows: list, w: int, h: int, bit_depth: int, color_type: int, ftypes: list, *,
              idat_pieces: int = 1, extra=(), plte: bytes | None = None) -> bytes:
    """素朴なフィルタで PNG を組み立てる(ライブラリ非依存)。extra = [(tag, body)] を IHDR の直後へ入れる。"""
    bpp = CHANNELS_OF[color_type] * bit_depth // 8
    comp = zlib.compress(ref_filter(rows, bpp, ftypes))
    n = max(1, -(-len(comp) // idat_pieces))
    parts = [PNG_SIG, png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bit_depth, color_type, 0, 0, 0))]
    parts += [png_chunk(t, b) for t, b in extra]
    if plte is not None:
        parts.append(png_chunk(b"PLTE", plte))
    parts += [png_chunk(b"IDAT", comp[i:i + n]) for i in range(0, len(comp), n)]
    parts.append(png_chunk(b"IEND", b""))
    return b"".join(parts)


def random_rows(rng, w: int, h: int, bpp: int, kind: str = "noise") -> list:
    if kind == "noise":
        return [bytes(rng.integers(0, 256, w * bpp, dtype=np.uint8)) for _ in range(h)]
    base = np.cumsum(rng.integers(-3, 4, size=(h, w * bpp)), axis=1) + np.arange(h)[:, None]   # なだらかな画像
    return [bytes((r % 256).astype(np.uint8)) for r in base]


def expected_float(rows: list, w: int, h: int, bit_depth: int, channels: int) -> np.ndarray:
    raw = np.frombuffer(b"".join(rows), dtype=np.uint8).reshape(h, w, channels * bit_depth // 8)
    if bit_depth == 16:
        return raw.view(">u2").astype(np.float32).reshape(h, w, channels) / np.float32(65535.0)
    return raw.astype(np.float32) / np.float32(255.0)


def filter_patterns(rng, h: int) -> dict:
    return {
        "none": [0] * h, "sub": [1] * h, "up": [2] * h, "average": [3] * h, "paeth": [4] * h,
        "random": [int(x) for x in rng.integers(0, 5, h)],
        "cycle": [i % 5 for i in range(h)],
        "simple_paeth_simple": ([1, 2, 0] * h)[:max(0, h // 3)] + [4] * (h - 2 * (h // 3)) + ([2, 1, 0] * h)[:h // 3],
        "isolated_heavy": [3 if i == h // 2 else (i % 3) for i in range(h)],
    }


def fast_encode_png(img: np.ndarray, ftypes, *, bit_depth: int = 8, level: int = 1, idat_size: int = 1 << 20) -> bytes:
    """numpy でベクトル化したエンコーダ。img = (H, W, C) の uint8(16bit のときは uint16)。

    フィルタ種別は行ごとに ``ftypes``(長さ H、0〜4)で指定する。エンコードは元の画素だけを使うので、
    Average / Paeth も行間の依存なしにベクトル化できる(デコードの反対角線処理とは独立な経路)。
    """
    h, w, ch = img.shape
    bpp = ch * bit_depth // 8
    q = (img.astype(">u2").view(np.uint8) if bit_depth == 16 else img.astype(np.uint8)).reshape(h, w * bpp)
    ft = np.asarray(ftypes, np.uint8)
    prev = np.vstack([np.zeros((1, w * bpp), np.uint8), q[:-1]])
    a = np.zeros_like(q)
    a[:, bpp:] = q[:, :-bpp]
    c = np.zeros_like(q)
    c[:, bpp:] = prev[:, :-bpp]

    def i16(x):
        return x.astype(np.int16)

    pa, pb, pc = np.abs(i16(prev) - i16(c)), np.abs(i16(a) - i16(c)), np.abs(i16(a) + i16(prev) - 2 * i16(c))
    paeth = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, prev, c))
    preds = [np.zeros_like(q), a, prev, ((a.astype(np.uint16) + prev) >> 1).astype(np.uint8), paeth]
    out = np.empty((h, w * bpp + 1), np.uint8)
    out[:, 0] = ft
    for t in range(5):
        m = ft == t
        if m.any():
            out[m, 1:] = q[m] - preds[t][m]
    ihdr = struct.pack(">IIBBBBB", w, h, bit_depth, {1: 0, 2: 4, 3: 2, 4: 6}[ch], 0, 0, 0)
    comp = zlib.compress(out.reshape(-1), level)
    idats = b"".join(png_chunk(b"IDAT", comp[i:i + idat_size]) for i in range(0, len(comp), idat_size))
    return PNG_SIG + png_chunk(b"IHDR", ihdr) + idats + png_chunk(b"IEND", b"")
