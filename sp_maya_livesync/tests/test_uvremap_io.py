# -*- coding: utf-8 -*-
"""uvremap_io の単体テスト(契約 CONTRACT.md §5): 自前の PNG 読み書き(numpy + zlib + struct のみ)。

実行(Maya 不要。標準ライブラリ + numpy + pytest。Pillow は不要で、入っていれば照合テストが追加で走る):

    python -m pytest sp_maya_livesync/tests/test_uvremap_io.py -q

    # 4096^2 の速度計測(合否は 8bit RGBA の読み 15 秒・書き 10 秒のみ)。表は -s で見える
    UVREMAP_SLOW=1 python -m pytest sp_maya_livesync/tests/test_uvremap_io.py -q -s -m slow

構成:
    (a) 素朴な参照実装(png_testkit.ref_*。画素単位の Python ループ、PNG 仕様 9.2 そのまま)との
        ビット一致: 全フィルタ種別・全色タイプ・8/16bit、行ごとにフィルタ種別が混在する場合、乱数の網羅
    (b) 行ごとにフィルタ種別がバラバラな PNG を自前エンコーダ(png_testkit)で作って読む
    (c) Pillow があれば: Pillow が書いた PNG を読んで一致 / 本実装が書いた PNG を Pillow で読んで一致
        (Pillow は照合にだけ使う。本実装は Pillow を import しない)
    (d) 壊れた PNG: ヘッダ不正 / 未対応形式 = UnsupportedFormat(ワーカーで 11)、
        CRC 不一致・チャンク欠落・zlib 破損 = ImageIOError(12)
    (e) 速度(slow): 4096^2 の読み込み・書き込み
    replace_file の再試行
"""
from __future__ import annotations

import ast
import os
import struct
import subprocess
import sys
import time
import warnings
import zlib
from pathlib import Path

import numpy as np
import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from png_testkit import (CHANNELS_OF, PNG_SIG, build_png, expected_float, fast_encode_png,  # noqa: E402
                         filter_patterns, make_png_bytes, png_chunk, png_chunks, random_rows, ref_unfilter)
import uvremap_io  # noqa: E402

_RUN_SLOW = bool(os.environ.get("UVREMAP_SLOW"))
warnings.filterwarnings("ignore", category=pytest.PytestUnknownMarkWarning)   # slow は設定ファイル無しで使う


def slow(fn):
    """重いテスト: UVREMAP_SLOW=1 のときだけ実行する(-m "not slow" でも除外できる)。"""
    fn = pytest.mark.slow(fn)
    return fn if _RUN_SLOW else pytest.mark.skip(reason="slow test: set UVREMAP_SLOW=1")(fn)


MODE_FOR_C = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}


def make_array(h: int, w: int, c: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    q = rng.integers(0, 256, size=(h, w, c), dtype=np.uint8)
    return q.astype(np.float32) / 255.0


# ---------------------------------------------------------------------------
# 画像 I/O(契約 §5): 自前の PNG 実装(numpy + zlib + struct のみ)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("color_type", [0, 2, 4, 6])
@pytest.mark.parametrize("bit_depth", [8, 16])
def test_png_read_matches_naive_reference_for_every_filter(tmp_path, color_type, bit_depth):
    """5 種のフィルタ(行ごとに混在も)・8/16bit・各色タイプで、素朴な参照デコーダと完全一致する。"""
    channels = CHANNELS_OF[color_type]
    bpp = channels * bit_depth // 8
    rng = np.random.default_rng(100 * color_type + bit_depth)
    p = tmp_path / "t.png"
    for (w, h) in [(1, 1), (1, 7), (9, 1), (2, 3), (11, 8), (16, 15)]:
        for name, ftypes in filter_patterns(rng, h).items():
            for kind in ("noise", "smooth"):
                rows = random_rows(rng, w, h, bpp, kind)
                data = build_png(rows, w, h, bit_depth, color_type, ftypes)
                # 参照デコーダ自身の検証: 組み立てたストリームを素朴に戻すと元の画素になる
                raw = zlib.decompress(b"".join(b for t, b in png_chunks(data) if t == b"IDAT"))
                assert ref_unfilter(raw, w, h, bpp) == rows
                p.write_bytes(data)
                arr, meta = uvremap_io.read_image(p)
                want = expected_float(rows, w, h, bit_depth, channels)
                assert arr.dtype == np.float32 and arr.shape == (h, w, channels)
                assert np.array_equal(arr, want), (w, h, name, kind)
                assert meta["bit_depth"] == bit_depth and meta["ext"] == ".png"
                expect_mode = {(0, 8): "L", (0, 16): "I;16", (4, 8): "LA", (4, 16): "LA", (2, 8): "RGB",
                               (2, 16): "RGB", (6, 8): "RGBA", (6, 16): "RGBA"}[(color_type, bit_depth)]
                assert meta["mode"] == expect_mode


def test_png_read_larger_image_mixed_filters_matches_reference(tmp_path):
    """反対角線処理の端(先頭/末尾の行、細長い画像、Average/Paeth が一部の行だけ)も参照実装と一致する。"""
    rng = np.random.default_rng(7)
    p = tmp_path / "t.png"
    for (w, h, ct, bd) in [(70, 41, 6, 8), (45, 60, 2, 16), (3, 90, 6, 16), (90, 3, 4, 8), (33, 33, 0, 16)]:
        bpp = CHANNELS_OF[ct] * bd // 8
        for name, ftypes in filter_patterns(rng, h).items():
            rows = random_rows(rng, w, h, bpp, "smooth" if name in ("paeth", "average") else "noise")
            p.write_bytes(build_png(rows, w, h, bd, ct, ftypes))
            arr, _ = uvremap_io.read_image(p)
            assert np.array_equal(arr, expected_float(rows, w, h, bd, CHANNELS_OF[ct])), (w, h, ct, bd, name)


def test_png_read_multiple_idat_ancillary_chunks_and_palette_hint(tmp_path):
    rng = np.random.default_rng(3)
    w, h, bpp = 12, 10, 3
    rows = random_rows(rng, w, h, bpp)
    extra = [(b"gAMA", struct.pack(">I", 45455)), (b"tEXt", b"Software\x00x"), (b"sRGB", b"\x00"),
             (b"pHYs", struct.pack(">IIB", 2835, 2835, 1)), (b"zzZz", b"private ancillary")]
    data = build_png(rows, w, h, 8, 2, [int(x) for x in rng.integers(0, 5, h)], idat_pieces=5, extra=extra,
                     plte=bytes(range(9)))                  # RGB でも PLTE(推奨パレット)は許される
    assert sum(1 for t, _b in png_chunks(data) if t == b"IDAT") > 1
    p = tmp_path / "multi.png"
    p.write_bytes(data)
    arr, meta = uvremap_io.read_image(p)
    assert np.array_equal(arr, expected_float(rows, w, h, 8, 3)) and meta["mode"] == "RGB"


def test_png_read_trailing_bytes_after_iend_are_ignored(tmp_path):
    rows = random_rows(np.random.default_rng(0), 4, 4, 1)
    p = tmp_path / "t.png"
    p.write_bytes(build_png(rows, 4, 4, 8, 0, [0] * 4) + b"\x00" * 7)
    assert np.array_equal(uvremap_io.read_image(p)[0], expected_float(rows, 4, 4, 8, 1))


def _png_with(data: bytes, tmp_path, name="x.png"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_png_unsupported_variants_raise_unsupported_format(tmp_path):
    ok = build_png(random_rows(np.random.default_rng(0), 4, 4, 3), 4, 4, 8, 2, [0] * 4)

    def ihdr_variant(**kw):
        f = dict(w=4, h=4, depth=8, ctype=2, comp=0, filt=0, interlace=0)
        f.update(kw)
        body = struct.pack(">IIBBBBB", f["w"], f["h"], f["depth"], f["ctype"], f["comp"], f["filt"], f["interlace"])
        return PNG_SIG + png_chunk(b"IHDR", body) + png_chunk(b"IDAT", zlib.compress(b"\x00" * 100)) \
            + png_chunk(b"IEND", b"")

    cases = {
        "palette": ihdr_variant(ctype=3), "depth4": ihdr_variant(depth=4, ctype=0), "depth1": ihdr_variant(depth=1, ctype=0),
        "depth2": ihdr_variant(depth=2, ctype=0), "interlaced": ihdr_variant(interlace=1),
        "bad color type": ihdr_variant(ctype=5), "zero width": ihdr_variant(w=0), "zero height": ihdr_variant(h=0),
        "bad compression": ihdr_variant(comp=1), "bad filter method": ihdr_variant(filt=1),
        "not a png": b"GIF89a" + b"\x00" * 60, "empty": b"", "short signature only": PNG_SIG,
        "wrong first chunk": PNG_SIG + png_chunk(b"IDAT", b"\x00" * 13) + b"\x00" * 20,
    }
    # 未知の重要チャンク(先頭が大文字)は解釈できない
    pieces = png_chunks(ok)
    cases["unknown critical chunk"] = PNG_SIG + b"".join(
        png_chunk(t, b) for t, b in pieces[:1] + [(b"ZZZZ", b"?")] + pieces[1:])
    for name, data in cases.items():
        with pytest.raises(uvremap_io.UnsupportedFormat):
            uvremap_io.read_image(_png_with(data, tmp_path, "case.png"))
    assert uvremap_io.read_image(_png_with(ok, tmp_path, "ok.png"))[0].shape == (4, 4, 3)


def test_png_corrupt_bodies_raise_image_io_error(tmp_path):
    """ヘッダが PNG として正しく、本体が壊れている -> ImageIOError(ワーカーでは 12)。"""
    rng = np.random.default_rng(5)
    rows = random_rows(rng, 10, 12, 3)
    good = build_png(rows, 10, 12, 8, 2, [int(x) for x in rng.integers(0, 5, 12)], idat_pieces=3)
    chunks = png_chunks(good)
    idats = [b for t, b in chunks if t == b"IDAT"]
    comp = b"".join(idats)

    def rebuild(idat_data, *, drop_iend=False, ihdr=None, between=()):
        parts = [PNG_SIG, png_chunk(b"IHDR", ihdr or chunks[0][1])]
        parts += [png_chunk(t, b) for t, b in between]
        parts += [png_chunk(b"IDAT", d) for d in idat_data]
        if not drop_iend:
            parts.append(png_chunk(b"IEND", b""))
        return b"".join(parts)

    bad_crc_idat = bytearray(good)
    pos = good.index(b"IDAT") + 6
    bad_crc_idat[pos] ^= 0xFF                                    # IDAT の本体を 1 バイト変える(CRC が合わない)
    bad_crc_ihdr = bytearray(good)
    bad_crc_ihdr[33 - 4] ^= 0x01                                 # IHDR の CRC を壊す
    cases = {
        "idat crc": bytes(bad_crc_idat),
        "ihdr crc": bytes(bad_crc_ihdr),
        "truncated mid-idat": good[:len(good) // 2],
        "truncated before iend": good[:-12],
        "truncated in chunk header": good[:-5],
        "missing iend": rebuild(idats, drop_iend=True),
        "no idat": rebuild([]),
        "zlib garbage": rebuild([b"this is not zlib data at all" * 3]),
        "zlib truncated": rebuild([comp[:len(comp) // 2]]),
        "too little data": rebuild([zlib.compress(b"\x00" * (10 * 3 + 1) * 5)]),
        "too much data": rebuild([zlib.compress(b"\x00" * ((10 * 3 + 1) * 12 + 50))]),
        "bad filter byte": rebuild([zlib.compress((b"\x07" + bytes(30)) * 12)]),
        "non-contiguous idat": PNG_SIG + png_chunk(b"IHDR", chunks[0][1]) + png_chunk(b"IDAT", idats[0])
        + png_chunk(b"tEXt", b"k\x00v") + png_chunk(b"IDAT", b"".join(idats[1:])) + png_chunk(b"IEND", b""),
        "zero-filled tail": good[:-30] + b"\x00" * 30,
    }
    for name, data in cases.items():
        with pytest.raises(uvremap_io.ImageIOError):
            uvremap_io.read_image(_png_with(data, tmp_path, "bad.png"))
        # 壊れ方がどうであれ UnsupportedFormat とは区別される(ヘッダ不正 = 11 と本体破損 = 12 の分類)
    assert uvremap_io.read_image(_png_with(good, tmp_path, "good.png"))[0].shape == (12, 10, 3)


def test_png_missing_file_is_io_error(tmp_path):
    with pytest.raises(uvremap_io.ImageIOError):
        uvremap_io.read_image(tmp_path / "nope.png")


@pytest.mark.parametrize("c", [1, 2, 3, 4])
def test_io_8bit_png_round_trip_is_exact(tmp_path, c):
    arr = make_array(7, 5, c, seed=c)
    p = tmp_path / "t.png"
    uvremap_io.write_image(p, arr, {"bit_depth": 8, "mode": MODE_FOR_C[c], "ext": ".png"})
    back, meta = uvremap_io.read_image(p)
    assert back.dtype == np.float32 and back.shape == (7, 5, c)
    assert meta == {"bit_depth": 8, "mode": MODE_FOR_C[c], "ext": ".png"}
    assert np.array_equal(np.rint(back * 255), np.rint(arr * 255))


MODES_16 = {1: "I;16", 2: "LA", 3: "RGB", 4: "RGBA"}


@pytest.mark.parametrize("c", [1, 2, 3, 4])
def test_io_16bit_round_trip_all_color_types(tmp_path, c):
    rng = np.random.default_rng(c)
    q = rng.integers(0, 65536, size=(6, 9, c)).astype(np.float32) / 65535.0
    p = tmp_path / "h.png"
    uvremap_io.write_image(p, q, {"bit_depth": 16, "mode": MODES_16[c], "ext": ".png"})
    back, meta = uvremap_io.read_image(p)
    assert meta["bit_depth"] == 16 and meta["mode"] == MODES_16[c] and back.shape == (6, 9, c)
    assert np.abs(back - q).max() < 1e-5
    assert uvremap_io.mode_for_channels(16, c) == MODES_16[c] and uvremap_io.mode_for_channels(8, c) == MODE_FOR_C[c]


def test_io_16bit_gray_accepts_mode_l_as_well(tmp_path):
    q = np.linspace(0, 1, 12, dtype=np.float32).reshape(3, 4, 1)
    uvremap_io.write_image(tmp_path / "a.png", q, {"bit_depth": 16, "mode": "L", "ext": ".png"})
    back, meta = uvremap_io.read_image(tmp_path / "a.png")
    assert meta["bit_depth"] == 16 and np.abs(back - q).max() < 1e-5


def test_io_values_are_clipped_and_nan_becomes_zero(tmp_path):
    arr = np.array([[[-0.5], [1.5], [np.nan], [0.5]]], dtype=np.float32)
    p = tmp_path / "c.png"
    uvremap_io.write_image(p, arr, {"bit_depth": 8, "mode": "L", "ext": ".png"})
    back, _ = uvremap_io.read_image(p)
    assert np.rint(back[0, :, 0] * 255).tolist() == [0, 255, 0, 128]
    arr2 = np.array([[[np.inf], [-np.inf], [np.nan], [0.25]]], dtype=np.float32)
    uvremap_io.write_image(p, arr2, {"bit_depth": 16, "mode": "I;16", "ext": ".png"})
    assert np.rint(uvremap_io.read_image(p)[0][0, :, 0] * 65535).tolist() == [65535, 0, 0, 16384]


@pytest.mark.parametrize("bit_depth,channels", [(8, 1), (8, 2), (8, 3), (8, 4), (16, 1), (16, 2), (16, 3), (16, 4)])
def test_png_write_is_valid_for_a_naive_decoder_and_picks_filters(tmp_path, bit_depth, channels):
    """書いた PNG を、自前の読み込みではなく素朴な参照デコーダで戻して画素が一致する。

    なだらかな画像では Sub / Up が選ばれる(フィルタ選択が働いている)ことも確かめる。
    """
    rng = np.random.default_rng(bit_depth + channels)
    h, w = 37, 29
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    grad = np.stack([(xx / w + k * 0.1) % 1.0 for k in range(channels)], axis=2)         # 横方向に滑らか
    grad[20:] = np.stack([(yy[20:] / h + k * 0.2) % 1.0 for k in range(channels)], axis=2)   # 縦方向に滑らか
    noise = rng.random((h, w, channels)).astype(np.float32)
    img = np.concatenate([grad[:25], noise[25:]], axis=0)
    mode = MODES_16[channels] if bit_depth == 16 else MODE_FOR_C[channels]
    p = tmp_path / "w.png"
    uvremap_io.write_image(p, img, {"bit_depth": bit_depth, "mode": mode, "ext": ".png"})
    data = p.read_bytes()
    chunks = png_chunks(data)
    assert [t for t, _b in chunks][0] == b"IHDR" and chunks[-1] == (b"IEND", b"")
    assert {t for t, _b in chunks} == {b"IHDR", b"IDAT", b"IEND"}               # 余計なチャンクを書かない
    ihdr = struct.unpack(">IIBBBBB", chunks[0][1])
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    assert ihdr == (w, h, bit_depth, ctype, 0, 0, 0)
    raw = zlib.decompress(b"".join(b for t, b in chunks if t == b"IDAT"))
    bpp = channels * bit_depth // 8
    rows = ref_unfilter(raw, w, h, bpp)
    scale = 65535.0 if bit_depth == 16 else 255.0
    q = np.rint(np.clip(img, 0, 1) * np.float32(scale)).astype(np.float32)
    want = (q.astype(">u2") if bit_depth == 16 else q.astype(np.uint8)).tobytes()
    assert b"".join(rows) == want
    used = {raw[i * (w * bpp + 1)] for i in range(h)}
    assert used <= {0, 1, 2}
    assert used & {1, 2}, "なだらかな画像なのに Sub/Up が選ばれていない"


def test_png_write_is_deterministic_and_independent_of_input_layout(tmp_path):
    arr = np.random.default_rng(0).random((20, 30, 4)).astype(np.float32)
    meta = {"bit_depth": 8, "mode": "RGBA", "ext": ".png"}
    uvremap_io.write_image(tmp_path / "a.png", arr, meta)
    uvremap_io.write_image(tmp_path / "b.png", np.asfortranarray(arr), meta)
    uvremap_io.write_image(tmp_path / "c.png", arr[:, :, :].copy(), meta)
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes() == (tmp_path / "c.png").read_bytes()


def test_png_write_mode_channel_mismatch_and_bad_arguments(tmp_path):
    z = np.zeros((2, 2, 3), np.float32)
    with pytest.raises(ValueError):
        uvremap_io.write_image(tmp_path / "a.png", z, {"bit_depth": 8, "mode": "RGBA", "ext": ".png"})
    with pytest.raises(ValueError):
        uvremap_io.write_image(tmp_path / "a.png", np.zeros((2, 2, 3, 1), np.float32), {"bit_depth": 8, "ext": ".png"})
    with pytest.raises(ValueError):
        uvremap_io.write_image(tmp_path / "a.png", np.zeros((0, 2, 3), np.float32), {"bit_depth": 8, "ext": ".png"})
    with pytest.raises(uvremap_io.UnsupportedFormat):
        uvremap_io.write_image(tmp_path / "a.png", z, {"bit_depth": 12, "mode": "RGB", "ext": ".png"})
    with pytest.raises(uvremap_io.UnsupportedFormat):
        uvremap_io.write_image(tmp_path / "a.png", z, {"bit_depth": 8, "mode": "CMYK", "ext": ".png"})
    with pytest.raises(uvremap_io.UnsupportedFormat):
        uvremap_io.write_image(tmp_path / "a.png", np.zeros((2, 2, 1), np.float32), {"bit_depth": 8, "mode": "I;16", "ext": ".png"})
    # mode 省略 -> チャンネル数から決まる。2 次元配列は 1 チャンネル扱い
    uvremap_io.write_image(tmp_path / "g.png", np.full((3, 5), 0.5, np.float32), {"bit_depth": 8, "ext": ".png"})
    back, meta = uvremap_io.read_image(tmp_path / "g.png")
    assert back.shape == (3, 5, 1) and meta["mode"] == "L"
    assert not list(tmp_path.glob("a.png"))


def test_io_non_png_formats_are_unsupported(tmp_path):
    for name in ("a.exr", "a.tif", "a.jpg", "a.tga", "noext"):
        f = tmp_path / name
        f.write_bytes(b"junk")
        with pytest.raises(uvremap_io.UnsupportedFormat):
            uvremap_io.read_image(f)
        with pytest.raises(uvremap_io.UnsupportedFormat):
            uvremap_io.write_image(tmp_path / ("out_" + name), np.zeros((2, 2, 3), np.float32),
                                   {"bit_depth": 8, "mode": "RGB", "ext": f.suffix})


def test_io_png_header_garbage_is_unsupported_but_broken_body_is_io_error(tmp_path):
    """現行ワーカーの扱いを維持する: ヘッダ不正(PNGではない) = 11、ヘッダが正しく本体が壊れた = 12。"""
    p = tmp_path / "bad.png"
    p.write_bytes(b"not a png at all, just text" * 3)
    with pytest.raises(uvremap_io.UnsupportedFormat):
        uvremap_io.read_image(p)
    p.write_bytes(make_png_bytes(8, 8, bit_depth=8, color_type=2)[:-30] + b"\x00" * 30)
    with pytest.raises(uvremap_io.ImageIOError):
        uvremap_io.read_image(p)


def test_no_pillow_import_anywhere_in_the_runtime_modules():
    for fname in ("uvremap_io.py", "uvremap_worker.py"):
        tree = ast.parse((_PKG / fname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n.split(".")[0] in ("PIL", "scipy", "OpenImageIO", "cv2", "imageio") for n in names), (fname, names)


# ---- Pillow が入っている環境だけ: 互換性(Pillow は比較にだけ使う。テスト内で importorskip) ----
def _pillow_png_cases():
    rng = np.random.default_rng(42)
    h, w = 48, 64
    yy, xx = np.mgrid[0:h, 0:w]
    smooth = ((np.sin(xx / 5.0) + np.cos(yy / 7.0)) * 60 + 128)[..., None]
    out = {}
    for mode, c in (("L", 1), ("LA", 2), ("RGB", 3), ("RGBA", 4)):
        base = np.concatenate([smooth + 25 * k for k in range(c)], axis=2) + rng.normal(0, 4, (h, w, c))
        out[mode] = np.clip(base, 0, 255).astype(np.uint8)
    return out


@pytest.mark.parametrize("optimize", [False, True])
def test_pillow_written_png_reads_identically_with_our_reader(tmp_path, optimize):
    Image = pytest.importorskip("PIL.Image")
    for mode, a in _pillow_png_cases().items():
        im = Image.fromarray(a[:, :, 0] if mode == "L" else a, mode=mode)
        p = tmp_path / "pil_{0}.png".format(mode)
        im.save(p, format="PNG", optimize=optimize, compress_level=9 if optimize else 6)
        ours, meta = uvremap_io.read_image(p)
        assert meta["mode"] == mode and meta["bit_depth"] == 8
        with Image.open(p) as pim:
            theirs = np.asarray(pim.convert(mode)).reshape(a.shape)
        assert np.array_equal(np.rint(ours * 255).astype(np.uint8), theirs), mode


def test_pillow_written_16bit_gray_reads_identically(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    rng = np.random.default_rng(1)
    a = np.clip(np.cumsum(rng.integers(-300, 300, (40, 50)), axis=1) + 30000, 0, 65535).astype(np.uint16)
    p = tmp_path / "g16.png"
    Image.fromarray(a).save(p)
    ours, meta = uvremap_io.read_image(p)
    assert meta["bit_depth"] == 16 and meta["mode"] == "I;16"
    assert np.array_equal(np.rint(ours[:, :, 0] * 65535).astype(np.uint16), a)


def test_our_written_png_reads_identically_with_pillow(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    for mode, a in _pillow_png_cases().items():
        arr = a.astype(np.float32) / 255.0
        p = tmp_path / "ours_{0}.png".format(mode)
        uvremap_io.write_image(p, arr, {"bit_depth": 8, "mode": mode, "ext": ".png"})
        with Image.open(p) as pim:
            pim.load()
            assert pim.mode == mode and pim.size == (a.shape[1], a.shape[0])
            theirs = np.asarray(pim).reshape(a.shape)
        assert np.array_equal(theirs, a), mode
    g16 = np.random.default_rng(2).integers(0, 65536, (21, 17)).astype(np.uint16)
    uvremap_io.write_image(tmp_path / "g16.png", (g16.astype(np.float32) / 65535.0)[:, :, None],
                           {"bit_depth": 16, "mode": "I;16", "ext": ".png"})
    with Image.open(tmp_path / "g16.png") as pim:
        pim.load()
        assert np.array_equal(np.asarray(pim), g16)


def test_png_read_every_small_shape_and_pure_filter_matches_reference(tmp_path):
    """幅・高さ 1〜6 の全組み合わせ x 5 種のフィルタ(全行同じ)x 4 色タイプ x 8/16bit。

    反対角線の端(幅 1・高さ 1、W > H、H > W)で起きやすい添字の誤りを網羅する。
    """
    rng = np.random.default_rng(1234)
    p = tmp_path / "s.png"
    n = 0
    for color_type in (0, 2, 4, 6):
        for bit_depth in (8, 16):
            channels = CHANNELS_OF[color_type]
            bpp = channels * bit_depth // 8
            for w in range(1, 7):
                for h in range(1, 7):
                    for ft in range(5):
                        rows = random_rows(rng, w, h, bpp, "noise")
                        p.write_bytes(build_png(rows, w, h, bit_depth, color_type, [ft] * h))
                        arr, _ = uvremap_io.read_image(p)
                        assert np.array_equal(arr, expected_float(rows, w, h, bit_depth, channels)), \
                            (color_type, bit_depth, w, h, ft)
                        n += 1
    assert n == 4 * 2 * 36 * 5


def test_png_read_random_fuzz_matches_reference(tmp_path):
    """乱数の画像サイズ・色タイプ・ビット深度・行ごとのフィルタ種別・IDAT 分割で参照実装と一致する。"""
    rng = np.random.default_rng(20261016)
    p = tmp_path / "f.png"
    for i in range(150):
        color_type = int(rng.choice([0, 2, 4, 6]))
        bit_depth = int(rng.choice([8, 16]))
        w, h = int(rng.integers(1, 60)), int(rng.integers(1, 60))
        channels = CHANNELS_OF[color_type]
        bpp = channels * bit_depth // 8
        probs = rng.dirichlet(np.ones(5) * 0.6)                       # 種別の偏りも変える
        ftypes = [int(x) for x in rng.choice(5, size=h, p=probs)]
        rows = random_rows(rng, w, h, bpp, "noise" if rng.random() < 0.5 else "smooth")
        p.write_bytes(build_png(rows, w, h, bit_depth, color_type, ftypes, idat_pieces=int(rng.integers(1, 6))))
        arr, meta = uvremap_io.read_image(p)
        assert np.array_equal(arr, expected_float(rows, w, h, bit_depth, channels)), (i, color_type, bit_depth, w, h)
        assert meta["bit_depth"] == bit_depth


def test_png_read_idat_split_at_arbitrary_bytes_including_empty_chunks(tmp_path):
    rng = np.random.default_rng(9)
    w, h = 13, 11
    rows = random_rows(rng, w, h, 4)
    chunks = png_chunks(build_png(rows, w, h, 8, 6, [int(x) for x in rng.integers(0, 5, h)]))
    comp = b"".join(b for t, b in chunks if t == b"IDAT")
    cuts = sorted({0, 1, 2, 3, 17, len(comp) // 2, len(comp) - 1, len(comp)})
    pieces = [comp[a:b] for a, b in zip(cuts[:-1], cuts[1:])]
    pieces.insert(2, b"")                                         # 長さ 0 の IDAT
    pieces.append(b"")
    data = PNG_SIG + png_chunk(b"IHDR", chunks[0][1]) + b"".join(png_chunk(b"IDAT", d) for d in pieces) \
        + png_chunk(b"IEND", b"")
    p = tmp_path / "split.png"
    p.write_bytes(data)
    assert np.array_equal(uvremap_io.read_image(p)[0], expected_float(rows, w, h, 8, 4))


def test_16bit_samples_are_big_endian(tmp_path):
    """0x0102 -> 258/65535。バイト順を取り違えると 513/65535 になる。"""
    rows = [bytes([0x01, 0x02, 0xFF, 0x00, 0x00, 0xFF])]
    p = tmp_path / "be.png"
    p.write_bytes(build_png(rows, 3, 1, 16, 0, [0]))
    arr, meta = uvremap_io.read_image(p)
    assert meta["bit_depth"] == 16 and meta["mode"] == "I;16"
    assert np.rint(arr[0, :, 0] * 65535).astype(int).tolist() == [0x0102, 0xFF00, 0x00FF]


def test_importing_uvremap_io_never_imports_pillow_even_when_installed():
    """Pillow の有無で結果が変わらないよう、本実装は Pillow を import しない(入っている環境でも)。"""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import uvremap_io, numpy; "
            "assert 'PIL' not in sys.modules, sorted(m for m in sys.modules if m.startswith('PIL')); print('ok')")
    p = subprocess.run([sys.executable, "-c", code, str(_PKG)], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == "ok", p.stderr


def test_pillow_written_png_with_every_filter_reads_identically(tmp_path):
    """Pillow(libpng)が書いた PNG を読む。libpng の適応フィルタは Paeth / Average も多用する。"""
    Image = pytest.importorskip("PIL.Image")
    rng = np.random.default_rng(77)
    h, w = 120, 150
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.stack([np.sin(xx / 9.0 + k) * 50 + np.cos(yy / 6.0) * 50 + 128 for k in range(4)], axis=2)
    img = np.clip(base + rng.normal(0, 6, base.shape), 0, 255).astype(np.uint8)
    for mode, c in (("L", 1), ("LA", 2), ("RGB", 3), ("RGBA", 4)):
        a = img[:, :, :c]
        p = tmp_path / "pil_{0}.png".format(mode)
        Image.fromarray(a[:, :, 0] if c == 1 else a, mode=mode).save(p, optimize=True)
        raw = zlib.decompress(b"".join(b for t, b in png_chunks(p.read_bytes()) if t == b"IDAT"))
        stride = w * c + 1
        used = {raw[i * stride] for i in range(h)}
        assert used & {3, 4}, "Pillow がこの画像で Average/Paeth を使っていない(テストの意味がない): {0}".format(used)
        ours, meta = uvremap_io.read_image(p)
        assert meta["mode"] == mode
        assert np.array_equal(np.rint(ours * 255).astype(np.uint8), a), mode


# ---- 性能(slow): 4096² RGBA8 の読み込み 15 秒以内・書き込み 10 秒以内(契約外の目標。docs/uvremap/E2E_REPORT.md) ----
@slow
@pytest.mark.parametrize("filters,bit_depth,channels", [
    ("paeth", 8, 4), ("mixed", 8, 4), ("average", 8, 4), ("paeth", 8, 3), ("mixed", 16, 4), ("paeth", 16, 1)])
def test_png_performance_4096(tmp_path, filters, bit_depth, channels, capsys):
    n = 4096
    rng = np.random.default_rng(0)
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    top = 255.0 if bit_depth == 8 else 65535.0
    img = np.stack([(np.sin(xx / 37 + k) * 0.23 + np.cos(yy / 53) * 0.23 + 0.5
                     + rng.normal(0, 0.03, (n, n))) * top for k in range(channels)], axis=2)
    img = np.clip(img, 0, top).astype(np.uint8 if bit_depth == 8 else np.uint16)
    ftypes = {"paeth": np.full(n, 4), "average": np.full(n, 3), "mixed": rng.integers(0, 5, n)}[filters]
    p = tmp_path / "big.png"
    p.write_bytes(fast_encode_png(img, ftypes, bit_depth=bit_depth))
    t0 = time.perf_counter()
    arr, meta = uvremap_io.read_image(p)
    t_read = time.perf_counter() - t0
    assert arr.shape == (n, n, channels) and meta["bit_depth"] == bit_depth
    assert np.array_equal(np.rint(arr * np.float32(top)).astype(img.dtype), img)
    t0 = time.perf_counter()
    uvremap_io.write_image(tmp_path / "out.png", arr, meta)
    t_write = time.perf_counter() - t0
    assert np.array_equal(uvremap_io.read_image(tmp_path / "out.png")[0], arr)
    with capsys.disabled():
        print("\n[png 4096^2 {0}ch {1}bit {2:7s}] read {3:5.2f}s write {4:5.2f}s (file {5:.1f}MB -> {6:.1f}MB)".format(
            channels, bit_depth, filters, t_read, t_write, p.stat().st_size / 1e6,
            (tmp_path / "out.png").stat().st_size / 1e6))
    if (bit_depth, channels) == (8, 4):         # 目標(E2E_REPORT.md): 4096^2 RGBA8 の読み 15 秒以内・書き 10 秒以内
        assert t_read < 15.0 and t_write < 10.0


# ---------------------------------------------------------------------------
# replace_file の再試行(契約 §5)
# ---------------------------------------------------------------------------
class _FakeTime:
    def __init__(self):
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)


def test_replace_file_retries_then_succeeds(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("new")
    b.write_text("old")
    real = os.replace
    calls = []

    def flaky(s, d):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "used by another process")
        return real(s, d)

    ft = _FakeTime()
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(uvremap_io, "time", ft)
    uvremap_io.replace_file(a, b)
    assert len(calls) == 3 and ft.sleeps == [0.2, 0.2] and b.read_text() == "new"


def test_replace_file_default_is_five_tries_then_raises(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("new")
    b.write_text("old")
    calls = []

    def always(s, d):
        calls.append(1)
        raise PermissionError(13, "used by another process")

    ft = _FakeTime()
    monkeypatch.setattr(os, "replace", always)
    monkeypatch.setattr(uvremap_io, "time", ft)
    with pytest.raises(uvremap_io.ReplaceFailed) as ei:
        uvremap_io.replace_file(a, b)
    assert isinstance(ei.value, OSError) and len(calls) == 5 and ft.sleeps == [0.2] * 4
    assert b.read_text() == "old" and a.read_text() == "new"  # どちらも壊さない


def test_replace_file_missing_source_is_not_retried(tmp_path, monkeypatch):
    calls = []
    real = os.replace

    def counting(s, d):
        calls.append(1)
        return real(s, d)

    monkeypatch.setattr(os, "replace", counting)
    with pytest.raises(FileNotFoundError):
        uvremap_io.replace_file(tmp_path / "none", tmp_path / "b")
    assert len(calls) == 1
