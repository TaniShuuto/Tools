# -*- coding: utf-8 -*-
"""UV再マップ用の画像I/O(契約 CONTRACT.md §5)。

- ``read_image`` / ``write_image``: float32 (H, W, C) [0, 1] の配列として入出力する。
- PNG は **numpy + zlib + struct だけ** で読み書きする(Pillow・scipy・OpenImageIO は使わない。
  Pillow が入っている環境でも使わないので、環境による結果の差は出ない)。
    読み: 色タイプ 0(グレー)/ 2(RGB)/ 4(グレー+アルファ)/ 6(RGBA)、ビット深度 8 と 16、
          非インターレース。フィルタ 5 種(None / Sub / Up / Average / Paeth)すべて、複数 IDAT、
          補助チャンク(無視)、CRC 検査。
    書き: 読んだ形式・ビット深度のまま(行ごとに None / Sub / Up を選ぶ)。
- 対応外(``UnsupportedFormat``): パレット、インターレース、ビット深度 1/2/4、PNG 以外
  (EXR / TIFF / JPEG 等)、PNG として読めないヘッダ。
- 壊れた PNG(ヘッダは PNG だが CRC 不一致・データ欠落・zlib 破損 等)は ``ImageIOError``。
  ワーカーは前者を終了コード 11、後者を 12 に分ける。
- ``replace_file``: Windows の共有違反(``PermissionError`` / winerror 32・33)だけ
  合計約 5 秒まで再試行する(間隔約 0.25 秒)。ディスクフル(``ENOSPC``)など他の
  ``OSError`` は再試行せずすぐ失敗。尽きたら ``ReplaceFailed``。

meta は ``{"bit_depth": 8|16, "mode": "L"|"LA"|"RGB"|"RGBA"|"I;16", "ext": ".png"}``。
16bit のグレーは従来どおり ``"I;16"``、16bit のその他は ``"LA"`` / ``"RGB"`` / ``"RGBA"``
(``bit_depth`` が 16)。

性能: Sub は cumsum、Up は行加算でベクトル化する。Average / Paeth は行内に依存があるが、
画素 (y, x) は (y, x-1)・(y-1, x)・(y-1, x-1) だけに依存するので、反対角線(y + x が同じ画素)
ごとにまとめて計算できる(反対角線は前の 2 本にしか依存しない)。画素単位の Python ループは無い。

標準ライブラリ + numpy のみ。Maya 非依存。Python 3.10+ / numpy 1.2x でも 2.x でも動く。
"""
from __future__ import annotations

import os
import struct
import time
import zlib
from pathlib import Path

import numpy as np


class UnsupportedFormat(Exception):
    """未対応の画像形式・ビット深度・モード、または PNG として読めないヘッダ。"""


class ImageIOError(Exception):
    """対応形式だが読み書きに失敗した(破損ファイル等)。"""


class ReplaceFailed(OSError):
    """``replace_file`` が再試行の末に失敗した。"""


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# PNG の color type -> チャンネル数(パレット(3)は非対応なので含めない)
_PNG_CHANNELS = {0: 1, 4: 2, 2: 3, 6: 4}
_MODE_8BIT = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}
_MODE_16BIT = {1: "I;16", 2: "LA", 3: "RGB", 4: "RGBA"}
_COLOR_TYPE = {1: 0, 2: 4, 3: 2, 4: 6}          # チャンネル数 -> color type
_MODE_CHANNELS = {"L": 1, "LA": 2, "RGB": 3, "RGBA": 4, "I;16": 1}
MODE_CHANNELS = _MODE_CHANNELS  # ワーカーから参照する公開名

_IDAT_CHUNK = 1 << 20          # 書き出し時の IDAT 1 個あたりの最大バイト数
_ROW_BLOCK_BYTES = 1 << 22     # 書き出し時にまとめて処理する行ブロックの大きさ(目安)
ZLIB_LEVEL = 4


# ---------------------------------------------------------------------------
# PNG の読み込み
# ---------------------------------------------------------------------------
def _parse_ihdr(head: bytes, name: str) -> tuple[int, int, int, int]:
    """先頭バイト列から (width, height, bit_depth, color_type) を取り出し、対応形式か検査する。

    PNG として読めない・対応外の形式は ``UnsupportedFormat``(ワーカーでは 11)。
    CRC の検査は本体の読み込み側で行う(壊れた本体は ``ImageIOError``)。
    """
    if len(head) < 29 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR" \
            or struct.unpack(">I", head[8:12])[0] != 13:
        raise UnsupportedFormat("未対応形式: PNGではありません({0})".format(name))
    width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(">IIBBBBB", head[16:29])
    if width == 0 or height == 0 or width > 0x7FFFFFFF or height > 0x7FFFFFFF:
        raise UnsupportedFormat("未対応形式: PNGのヘッダが不正です(画像サイズ {0}x{1}, {2})".format(
            width, height, name))
    if color_type == 3:
        raise UnsupportedFormat("未対応形式: パレットPNG ({0})".format(name))
    if color_type not in _PNG_CHANNELS:
        raise UnsupportedFormat("未対応形式: PNG color type {0} ({1})".format(color_type, name))
    if bit_depth not in (8, 16):
        raise UnsupportedFormat("未対応形式: {0}bit PNG ({1})".format(bit_depth, name))
    if comp != 0 or filt != 0:
        raise UnsupportedFormat("未対応形式: PNGの圧縮/フィルタ方式が不正です ({0})".format(name))
    if interlace != 0:
        raise UnsupportedFormat("未対応形式: インターレースPNG ({0})".format(name))
    return width, height, bit_depth, color_type


def _read_png_chunks(data: bytes, name: str) -> bytes:
    """チャンネルを走査して CRC を検査し、連結した IDAT のデータを返す。

    補助チャンクは読み飛ばす(CRC は検査する)。未知の重要チャンクは ``UnsupportedFormat``。
    """
    pos = 8
    end = len(data)
    idat: list[bytes] = []
    seen_ihdr = False
    seen_iend = False
    idat_closed = False
    while pos < end:
        if end - pos < 12:
            raise ImageIOError("PNGが途中で終わっています(チャンクの先頭が欠けている): {0}".format(name))
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        if length > 0x7FFFFFFF or pos + 12 + length > end:
            raise ImageIOError("PNGが途中で終わっています(チャンク {0!r} が欠けている): {1}".format(
                tag.decode("latin-1"), name))
        body_end = pos + 8 + length
        crc_stored = struct.unpack(">I", data[body_end:body_end + 4])[0]
        crc = zlib.crc32(data[pos + 4:body_end]) & 0xFFFFFFFF
        if crc != crc_stored:
            raise ImageIOError("PNGのCRCが一致しません(チャンク {0!r}): {1}".format(
                tag.decode("latin-1"), name))
        if tag == b"IHDR":
            if seen_ihdr:
                raise ImageIOError("PNGにIHDRが複数あります: {0}".format(name))
            seen_ihdr = True
        elif tag == b"IDAT":
            if idat_closed:
                raise ImageIOError("PNGのIDATが連続していません: {0}".format(name))
            idat.append(data[pos + 8:body_end])
        else:
            if idat:
                idat_closed = True
            if tag == b"IEND":
                seen_iend = True
                break
            if not (tag[0] & 0x20) and tag not in (b"PLTE",):
                # 先頭が大文字 = 重要チャンク。IHDR / PLTE / IDAT / IEND 以外は解釈できない
                raise UnsupportedFormat("未対応形式: 未知の重要チャンク {0!r} ({1})".format(
                    tag.decode("latin-1"), name))
        pos = body_end + 4
    if not seen_iend:
        raise ImageIOError("PNGが途中で終わっています(IENDがない): {0}".format(name))
    if not idat:
        raise ImageIOError("PNGに画像データ(IDAT)がありません: {0}".format(name))
    return b"".join(idat)


def _paeth_pred(a, b, c):
    """Paeth 予測子(a, b, c は uint8 の配列)。結果は uint8。"""
    a16 = a.astype(np.int16)
    b16 = b.astype(np.int16)
    c16 = c.astype(np.int16)
    pa = np.abs(b16 - c16)
    pb = np.abs(a16 - c16)
    pc = np.abs(a16 + b16 - c16 - c16)
    use_a = (pa <= pb) & (pa <= pc)
    use_b = pb <= pc
    return np.where(use_a, a, np.where(use_b, b, c))


def _avg_pred(a, b):
    return ((a.astype(np.uint16) + b) >> 1).astype(np.uint8)


def _unfilter(raw: bytes, height: int, width: int, bpp: int) -> np.ndarray:
    """フィルタ済みの走査線(各行の先頭 1 バイトがフィルタ種別)を元の画素値へ戻す。

    戻り値は (height, width, bpp) の uint8(``bpp`` = 1 画素のバイト数)。

    作業用に上と左へ 1 画素ぶんのゼロの縁を付けた配列 ``P`` (height+1, width+1, bpp) を使う。
    縁がゼロなので、PNG の「左端の a と c は 0、先頭行の b と c は 0」の規則がそのまま成り立つ。
    Sub/Up/None の行は 1 行ずつ numpy で処理し、Average/Paeth を含む行の範囲は反対角線ごとに
    処理する(画素 (Y, X) = P の添字は D = Y + X で分けると、同じ D の画素は互いに独立で、
    依存先 a=(Y,X-1) b=(Y-1,X) は D-1、c=(Y-1,X-1) は D-2 にある)。
    """
    stride = width * bpp
    buf = np.frombuffer(raw, dtype=np.uint8).reshape(height, stride + 1)
    ftypes = buf[:, 0]
    if int(ftypes.max()) > 4:
        raise ImageIOError("PNGのフィルタ種別が不正です")
    P = np.zeros((height + 1, width + 1, bpp), dtype=np.uint8)
    P[1:, 1:, :] = buf[:, 1:].reshape(height, width, bpp)

    heavy = np.flatnonzero(ftypes >= 3)          # Average / Paeth の行
    if heavy.size:
        r0, r1 = int(heavy[0]), int(heavy[-1])   # 反対角線で処理する行の範囲(両端を含む)
    else:
        r0, r1 = height, height - 1              # 範囲なし(全行を単純に処理)

    def simple_rows(lo: int, hi: int) -> None:   # 実際の行番号 lo..hi-1 を 1 行ずつ
        for y in range(lo, hi):
            t = ftypes[y]
            if t == 1:
                row = P[y + 1, 1:, :]
                row[...] = np.cumsum(row, axis=0, dtype=np.uint8)
            elif t == 2:
                P[y + 1, 1:, :] += P[y, 1:, :]

    simple_rows(0, r0)
    if heavy.size:
        _unfilter_wavefront(P, ftypes, r0, r1, width, bpp)
    simple_rows(r1 + 1, height)
    return P[1:, 1:, :]


def _unfilter_wavefront(P: np.ndarray, ftypes: np.ndarray, r0: int, r1: int, width: int, bpp: int) -> None:
    """実際の行 r0..r1 を、反対角線(Y + X = D)ごとにまとめて復元する。P を直接書き換える。"""
    W = width
    Pf = P.reshape(-1, bpp)                       # 画素の通し番号 Y*(W+1)+X の並び(コピーなし)
    rows = ftypes[r0:r1 + 1]
    # 種別ごとの累積個数: cnt[t][k] = band 内の先頭 k 行に含まれる種別 t の行数
    cnt = [np.concatenate(([0], np.cumsum(rows == t))).tolist() for t in range(5)]
    base = r0                                     # band 内の行番号 = y - base
    for D in range(r0 + 2, r1 + 1 + W + 1):
        ymin = max(r0 + 1, D - W)                 # P の行 Y の範囲
        ymax = min(r1 + 1, D - 1)
        k0, k1 = ymin - 1 - base, ymax - base     # band 内の行 [k0, k1)
        present = [t for t in range(5) if cnt[t][k1] - cnt[t][k0] > 0]
        out = Pf[D + ymin * W: D + ymax * W + 1: W]
        if present == [0]:
            continue
        a = Pf[D - 1 + ymin * W: D + ymax * W: W]
        b = Pf[D - 1 + (ymin - 1) * W: D + (ymax - 1) * W: W]
        c = None
        if 4 in present:
            c = Pf[D - 2 + (ymin - 1) * W: D - 1 + (ymax - 1) * W: W]

        def pred(t):
            if t == 1:
                return a
            if t == 2:
                return b
            if t == 3:
                return _avg_pred(a, b)
            return _paeth_pred(a, b, c)

        if len(present) == 1:
            out += pred(present[0])
        else:
            ft = rows[k0:k1][:, None]
            acc = np.zeros_like(out)
            for t in present:
                if t == 0:
                    continue
                acc = np.where(ft == t, pred(t), acc)
            out += acc


def _decode_png(data: bytes, name: str) -> tuple[np.ndarray, int, int]:
    """PNG のバイト列 -> (float32 (H, W, C), bit_depth, color_type)。"""
    width, height, bit_depth, color_type = _parse_ihdr(data[:33], name)
    channels = _PNG_CHANNELS[color_type]
    bpp = channels * bit_depth // 8
    idat = _read_png_chunks(data, name)
    expected = height * (width * bpp + 1)
    try:
        d = zlib.decompressobj()
        raw = d.decompress(idat, expected + 1)
    except zlib.error as exc:
        raise ImageIOError("PNGの画像データ(zlib)が壊れています: {0}: {1}".format(name, exc)) from exc
    if len(raw) < expected:
        raise ImageIOError("PNGの画像データが足りません({0} < {1} バイト): {2}".format(len(raw), expected, name))
    if len(raw) > expected:
        raise ImageIOError("PNGの画像データが多すぎます: {0}".format(name))
    del idat
    pix = _unfilter(raw, height, width, bpp)
    del raw
    if bit_depth == 8:
        arr = pix.astype(np.float32)
        arr /= np.float32(255.0)
        arr = arr.reshape(height, width, channels)
    else:
        be = np.ascontiguousarray(pix).view(">u2")        # (H, W, channels) 大きい桁が先
        arr = be.astype(np.float32)
        arr /= np.float32(65535.0)
        arr = arr.reshape(height, width, channels)
    return arr, bit_depth, color_type


def read_image(path) -> tuple[np.ndarray, dict]:
    """画像を float32 (H, W, C) [0, 1] で読む。meta={"bit_depth","mode","ext"}。"""
    p = Path(path)
    ext = p.suffix.lower()
    if ext != ".png":
        raise UnsupportedFormat("未対応形式: {0} ({1})".format(ext or "(拡張子なし)", p.name))
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise ImageIOError("画像を開けません: {0}: {1}".format(p.name, exc)) from exc
    try:
        arr, bit_depth, color_type = _decode_png(data, p.name)
    except (UnsupportedFormat, ImageIOError):
        raise
    except Exception as exc:  # numpy 側の想定外(破損データ等)も、読めなかったことに変わりはない
        raise ImageIOError("画像の読み込みに失敗: {0}: {1}: {2}".format(
            p.name, type(exc).__name__, exc)) from exc
    channels = _PNG_CHANNELS[color_type]
    mode = (_MODE_8BIT if bit_depth == 8 else _MODE_16BIT)[channels]
    meta = {"bit_depth": bit_depth, "mode": mode, "ext": ext}
    return np.ascontiguousarray(arr, dtype=np.float32), meta


# ---------------------------------------------------------------------------
# PNG の書き出し
# ---------------------------------------------------------------------------
def mode_for_channels(bit_depth: int, channels: int) -> str:
    """ビット深度とチャンネル数から meta["mode"] を決める。"""
    if bit_depth == 16 and channels in _MODE_16BIT:
        return _MODE_16BIT[channels]
    if bit_depth == 8 and channels in _MODE_8BIT:
        return _MODE_8BIT[channels]
    raise UnsupportedFormat("未対応形式: {0}bit {1}ch".format(bit_depth, channels))


def _chunk(tag: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(body, zlib.crc32(tag)) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)


_COST_TABLE = np.minimum(np.arange(256), 256 - np.arange(256)).astype(np.uint8)   # 符号付きと見た絶対値


def _filter_rows(q: np.ndarray, bpp: int) -> np.ndarray:
    """(H, stride) の uint8 -> (H, 1 + stride)。行ごとに None / Sub / Up を、絶対値の和が小さい順に選ぶ。"""
    height, stride = q.shape
    out = np.empty((height, stride + 1), dtype=np.uint8)
    rows_per_block = max(1, _ROW_BLOCK_BYTES // max(stride, 1))
    for lo in range(0, height, rows_per_block):
        hi = min(height, lo + rows_per_block)
        blk = q[lo:hi]
        prev = q[lo - 1:hi - 1] if lo > 0 else np.concatenate(
            [np.zeros((1, stride), np.uint8), q[:hi - 1]], axis=0)
        up = blk - prev
        sub = blk.copy()
        if stride > bpp:
            sub[:, bpp:] = blk[:, bpp:] - blk[:, :-bpp]
        costs = np.stack([
            _COST_TABLE[blk].sum(axis=1, dtype=np.int64),
            _COST_TABLE[sub].sum(axis=1, dtype=np.int64),
            _COST_TABLE[up].sum(axis=1, dtype=np.int64)])
        sel = np.argmin(costs, axis=0)                      # 0 = None, 1 = Sub, 2 = Up(同点は小さい番号)
        out[lo:hi, 0] = sel.astype(np.uint8)
        chosen = np.where((sel == 1)[:, None], sub, np.where((sel == 2)[:, None], up, blk))
        out[lo:hi, 1:] = chosen
    return out


def _quantize_rows(a: np.ndarray, bit_depth: int) -> np.ndarray:
    """float32 (h, W, C) -> 量子化済みの (h, W*C*bytes) uint8(16bit は大きい桁が先)。"""
    x = np.nan_to_num(a.astype(np.float32, copy=True), nan=0.0, posinf=1.0, neginf=0.0)
    np.clip(x, 0.0, 1.0, out=x)
    if bit_depth == 16:
        x *= np.float32(65535.0)
        np.rint(x, out=x)
        q = x.astype(">u2")
    else:
        x *= np.float32(255.0)
        np.rint(x, out=x)
        q = x.astype(np.uint8)
    return np.ascontiguousarray(q).view(np.uint8).reshape(a.shape[0], -1)


def write_image(path, arr, meta) -> None:
    """float32 (H, W, C) [0, 1] を meta の形式・ビット深度で書く(直接 ``path`` へ)。

    原子的な置き換えは呼び出し側(ワーカー)が一時ファイル + ``replace_file`` で行う。
    """
    p = Path(path)
    ext = str(meta.get("ext", p.suffix)).lower()
    if ext != ".png":
        raise UnsupportedFormat("未対応形式: {0} への書き出し".format(ext))
    bit_depth = int(meta.get("bit_depth", 8))
    a = np.asarray(arr)
    if a.ndim == 2:
        a = a[:, :, None]
    if a.ndim != 3:
        raise ValueError("画像配列は (H, W, C) である必要があります: shape={0}".format(a.shape))
    height, width, channels = a.shape
    if height == 0 or width == 0 or channels == 0:
        raise ValueError("空の画像は書き出せません: shape={0}".format(a.shape))
    mode = meta.get("mode") or mode_for_channels(bit_depth, channels)
    if mode not in _MODE_CHANNELS:
        raise UnsupportedFormat("未対応形式: モード {0}".format(mode))
    if _MODE_CHANNELS[mode] != channels:
        raise ValueError("モード {0} とチャンネル数 {1} が一致しません".format(mode, channels))
    if bit_depth not in (8, 16):
        raise UnsupportedFormat("未対応形式: {0}bit".format(bit_depth))
    if mode == "I;16" and bit_depth != 16:
        raise UnsupportedFormat("未対応形式: モード I;16 は 16bit のみ")
    bpp = channels * bit_depth // 8
    stride = width * bpp

    try:
        q = np.empty((height, stride), dtype=np.uint8)
        step = max(1, _ROW_BLOCK_BYTES // max(width * channels * 4, 1))     # float32 の作業領域が 4MB 程度
        for lo in range(0, height, step):
            hi = min(height, lo + step)
            q[lo:hi] = _quantize_rows(a[lo:hi], bit_depth)
        filtered = _filter_rows(q, bpp)
        del q
        comp = zlib.compress(filtered.reshape(-1), ZLIB_LEVEL)
        del filtered
        parts = [_PNG_SIGNATURE,
                 _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, _COLOR_TYPE[channels], 0, 0, 0))]
        for i in range(0, len(comp), _IDAT_CHUNK):
            parts.append(_chunk(b"IDAT", comp[i:i + _IDAT_CHUNK]))
        parts.append(_chunk(b"IEND", b""))
        with open(p, "wb") as f:
            for part in parts:
                f.write(part)
    except (UnsupportedFormat, ValueError):
        raise
    except Exception as exc:
        raise ImageIOError("画像の書き込みに失敗: {0}: {1}: {2}".format(
            p.name, type(exc).__name__, exc)) from exc


# ---------------------------------------------------------------------------
# os.replace の再試行(Windows で他プロセスが対象を開いていると失敗する)
# ---------------------------------------------------------------------------
REPLACE_TIMEOUT_S = 5.0
REPLACE_DELAY_S = 0.25
# None なら時間上限。テストが回数を指定するときだけ整数を入れる。
REPLACE_RETRIES = None
WINERR_SHARING_VIOLATION = 32
WINERR_LOCK_VIOLATION = 33


def is_retryable_replace_error(exc: BaseException) -> bool:
    """共有違反だけ再試行する。``ENOSPC`` など他の OSError は対象外。"""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (
        WINERR_SHARING_VIOLATION, WINERR_LOCK_VIOLATION,
    ):
        return True
    return False


def replace_file(
    src, dst, *,
    retries: int | None = None,
    delay: float | None = None,
    timeout_s: float | None = None,
    sleep_fn=None,
    clock_fn=None,
) -> None:
    """``os.replace(src, dst)`` を共有違反に限って再試行する。

    既定は合計 ``REPLACE_TIMEOUT_S`` 秒まで、間隔 ``REPLACE_DELAY_S`` 秒。
    ``retries`` を渡す(または ``REPLACE_RETRIES`` が整数)ときは回数上限。
    ``FileNotFoundError`` と共有違反以外の ``OSError`` はすぐ送出する。
    時間/回数を使い切ったら ``ReplaceFailed``。
    ``sleep_fn`` / ``clock_fn`` で壁時計を注入できる。
    """
    sleep = time.sleep if sleep_fn is None else sleep_fn
    clock = time.monotonic if clock_fn is None else clock_fn
    delay_s = REPLACE_DELAY_S if delay is None else float(delay)
    retry_n = REPLACE_RETRIES if retries is None else retries
    last: Exception | None = None

    if retry_n is not None:
        attempts = max(1, int(retry_n))
        for i in range(attempts):
            try:
                os.replace(src, dst)
                return
            except FileNotFoundError:
                raise
            except OSError as exc:
                last = exc
                if not is_retryable_replace_error(exc):
                    raise
                if i + 1 < attempts:
                    sleep(delay_s)
        raise ReplaceFailed("os.replace が {0} 回失敗しました: {1}: {2}".format(
            attempts, os.path.basename(str(dst)), last)) from last

    limit = REPLACE_TIMEOUT_S if timeout_s is None else float(timeout_s)
    deadline = clock() + limit
    attempts = 0
    while True:
        attempts += 1
        try:
            os.replace(src, dst)
            return
        except FileNotFoundError:
            raise
        except OSError as exc:
            last = exc
            if not is_retryable_replace_error(exc):
                raise
            now = clock()
            if now >= deadline:
                raise ReplaceFailed("os.replace が {0} 回失敗しました: {1}: {2}".format(
                    attempts, os.path.basename(str(dst)), last)) from last
            sleep(delay_s)
            later = clock()
            if later <= now and delay_s <= 0:
                raise ReplaceFailed("os.replace が {0} 回失敗しました: {1}: {2}".format(
                    attempts, os.path.basename(str(dst)), last)) from last
