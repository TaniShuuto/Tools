# -*- coding: utf-8 -*-
"""UV再マップ用の画像I/O(契約 CONTRACT.md §5)。

- ``read_image`` / ``write_image``: float32 (H, W, C) [0, 1] の配列として入出力する。
- 必須対応: 8bit PNG(L / LA / RGB / RGBA)。
- 16bit PNG は「グレースケール(Pillow の I;16)」のみ対応。16bit の LA / RGB / RGBA は
  Pillow が 8bit へ黙って落とすため、取り違えないよう ``UnsupportedFormat`` とする。
- EXR / TIFF / JPEG など PNG 以外、パレットPNG、8/16bit 以外のビット深度も
  ``UnsupportedFormat``。
- Pillow は任意依存。無い場合は ``UnsupportedFormat`` で明示的に報告する
  (自前のPNGフォールバックは持たない)。
- ``replace_file``: Windows で他プロセスが開いている場合に ``os.replace`` が失敗する
  ことへの短い再試行(既定 5回 x 0.2秒)。尽きたら ``ReplaceFailed``。

標準ライブラリ + numpy(+任意で Pillow)のみ。Maya 非依存。
"""
from __future__ import annotations

import os
import struct
import time
from pathlib import Path

import numpy as np

try:  # Pillow は任意依存
    from PIL import Image as _Image
except Exception:  # pragma: no cover - 環境依存
    _Image = None


class UnsupportedFormat(Exception):
    """未対応の画像形式・ビット深度・モード、または Pillow 未導入。"""


class ImageIOError(Exception):
    """対応形式だが読み書きに失敗した(破損ファイル等)。"""


class ReplaceFailed(OSError):
    """``replace_file`` が再試行の末に失敗した。"""


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# PNG の color type -> チャンネル数(パレット(3)は非対応なので含めない)
_PNG_CHANNELS = {0: 1, 4: 2, 2: 3, 6: 4}
_MODE_8BIT = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}
_MODE_CHANNELS = {"L": 1, "LA": 2, "RGB": 3, "RGBA": 4, "I;16": 1}
MODE_CHANNELS = _MODE_CHANNELS  # ワーカーから参照する公開名


def _require_pillow():
    if _Image is None:
        raise UnsupportedFormat("未対応形式: Pillow が見つかりません(PNGの読み書きに必要)")
    return _Image


def _parse_png_header(path: Path) -> tuple[int, int, int, int]:
    """(width, height, bit_depth, color_type) を IHDR から読む。PNGでなければ UnsupportedFormat。"""
    try:
        with open(path, "rb") as f:
            head = f.read(33)
    except OSError as exc:
        raise ImageIOError("画像を開けません: {0}: {1}".format(path.name, exc)) from exc
    if len(head) < 29 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        raise UnsupportedFormat("未対応形式: PNGではありません({0})".format(path.name))
    width, height, bit_depth, color_type = struct.unpack(">IIBB", head[16:26])
    return width, height, bit_depth, color_type


def read_image(path) -> tuple[np.ndarray, dict]:
    """画像を float32 (H, W, C) [0, 1] で読む。meta={"bit_depth","mode","ext"}。"""
    p = Path(path)
    ext = p.suffix.lower()
    if ext != ".png":
        raise UnsupportedFormat("未対応形式: {0} ({1})".format(ext or "(拡張子なし)", p.name))
    image_mod = _require_pillow()
    _w, _h, bit_depth, color_type = _parse_png_header(p)
    if color_type == 3:
        raise UnsupportedFormat("未対応形式: パレットPNG ({0})".format(p.name))
    if color_type not in _PNG_CHANNELS:
        raise UnsupportedFormat("未対応形式: PNG color type {0} ({1})".format(color_type, p.name))
    if bit_depth == 8:
        pass
    elif bit_depth == 16:
        if color_type != 0:
            raise UnsupportedFormat(
                "未対応形式: 16bit PNG (カラー/アルファ付き) は Pillow で正確に扱えません ({0})".format(p.name))
    else:
        raise UnsupportedFormat("未対応形式: {0}bit PNG ({1})".format(bit_depth, p.name))

    try:
        with image_mod.open(p) as im:
            im.load()
            mode = im.mode
            if bit_depth == 16:
                if mode != "I;16":
                    raise UnsupportedFormat(
                        "未対応形式: 16bit PNG をモード {0} で読みました ({1})".format(mode, p.name))
                data = np.asarray(im).astype(np.float32) / 65535.0
                arr = data.reshape(data.shape[0], data.shape[1], 1)
            else:
                expected = _MODE_8BIT[_PNG_CHANNELS[color_type]]
                if mode != expected:
                    # tRNS 付きの L/RGB は Pillow が LA/RGBA 扱いにしないが、念のため弾く
                    raise UnsupportedFormat(
                        "未対応形式: 想定外のモード {0} ({1})".format(mode, p.name))
                data = np.asarray(im, dtype=np.uint8).astype(np.float32) / 255.0
                arr = data.reshape(data.shape[0], data.shape[1], -1)
    except UnsupportedFormat:
        raise
    except Exception as exc:
        raise ImageIOError("画像の読み込みに失敗: {0}: {1}: {2}".format(
            p.name, type(exc).__name__, exc)) from exc
    meta = {"bit_depth": bit_depth, "mode": mode, "ext": ext}
    return np.ascontiguousarray(arr, dtype=np.float32), meta


def mode_for_channels(bit_depth: int, channels: int) -> str:
    """ビット深度とチャンネル数から meta["mode"] を決める。"""
    if bit_depth == 16:
        if channels != 1:
            raise UnsupportedFormat("未対応形式: 16bit は1チャンネルのみ対応")
        return "I;16"
    if bit_depth == 8 and channels in _MODE_8BIT:
        return _MODE_8BIT[channels]
    raise UnsupportedFormat("未対応形式: {0}bit {1}ch".format(bit_depth, channels))


def write_image(path, arr, meta) -> None:
    """float32 (H, W, C) [0, 1] を meta の形式・ビット深度で書く(直接 ``path`` へ)。

    原子的な置き換えは呼び出し側(ワーカー)が一時ファイル + ``replace_file`` で行う。
    """
    p = Path(path)
    ext = str(meta.get("ext", p.suffix)).lower()
    if ext != ".png":
        raise UnsupportedFormat("未対応形式: {0} への書き出し".format(ext))
    image_mod = _require_pillow()
    bit_depth = int(meta.get("bit_depth", 8))
    a = np.asarray(arr)
    if a.ndim == 2:
        a = a[:, :, None]
    if a.ndim != 3:
        raise ValueError("画像配列は (H, W, C) である必要があります: shape={0}".format(a.shape))
    channels = a.shape[2]
    mode = meta.get("mode") or mode_for_channels(bit_depth, channels)
    if mode not in _MODE_CHANNELS:
        raise UnsupportedFormat("未対応形式: モード {0}".format(mode))
    if _MODE_CHANNELS[mode] != channels:
        raise ValueError("モード {0} とチャンネル数 {1} が一致しません".format(mode, channels))
    a = np.nan_to_num(a.astype(np.float32, copy=False), nan=0.0, posinf=1.0, neginf=0.0)
    if bit_depth == 16:
        if mode != "I;16":
            raise UnsupportedFormat("未対応形式: 16bit {0}".format(mode))
        q = np.rint(np.clip(a[:, :, 0], 0.0, 1.0) * 65535.0).astype(np.uint16)
        im = image_mod.fromarray(q)
    elif bit_depth == 8:
        q = np.rint(np.clip(a, 0.0, 1.0) * 255.0).astype(np.uint8)
        if channels == 1:
            q = q[:, :, 0]
        im = image_mod.fromarray(q)
    else:
        raise UnsupportedFormat("未対応形式: {0}bit".format(bit_depth))
    try:
        im.save(str(p), format="PNG", compress_level=4)
    except Exception as exc:
        raise ImageIOError("画像の書き込みに失敗: {0}: {1}: {2}".format(
            p.name, type(exc).__name__, exc)) from exc


# ---------------------------------------------------------------------------
# os.replace の再試行(Windows で他プロセスが対象を開いていると失敗する)
# ---------------------------------------------------------------------------
REPLACE_RETRIES = 5
REPLACE_DELAY_S = 0.2


def replace_file(src, dst, *, retries: int | None = None, delay: float | None = None) -> None:
    """``os.replace(src, dst)`` を最大 ``retries`` 回(間隔 ``delay`` 秒)試す。

    ``FileNotFoundError``(src が無い等)は再試行しても無意味なのでそのまま送出する。
    試行を使い切ったら ``ReplaceFailed``(OSError のサブクラス)。
    """
    attempts = max(1, int(REPLACE_RETRIES if retries is None else retries))
    delay = REPLACE_DELAY_S if delay is None else delay
    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except FileNotFoundError:
            raise
        except OSError as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(delay)
    raise ReplaceFailed("os.replace が {0} 回失敗しました: {1}: {2}".format(
        attempts, os.path.basename(str(dst)), last)) from last
