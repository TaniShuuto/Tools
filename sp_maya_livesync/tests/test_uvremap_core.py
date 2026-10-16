# -*- coding: utf-8 -*-
"""uvremap_core.py の単体テスト(Maya非依存。numpy と pytest のみ)。

実行方法:

    python -m pytest sp_maya_livesync/tests/test_uvremap_core.py -q

    # 重いテストも含める
    UVREMAP_SLOW=1 python -m pytest sp_maya_livesync/tests/test_uvremap_core.py -q

    # プローブのしきい値の測定表(docs/uvremap/PROBE_CALIBRATION.md の元データ)を出力
    python sp_maya_livesync/tests/test_uvremap_core.py --calibrate

設計:
    * 合成メッシュ(球、複数板、複数メッシュ)に解析的な信号(滑らか、細部、ノイズ、法線)を焼き込んで
      地上真値(GT)にする。焼き込みはこのファイル内の独立したラスタライザで行い、core の
      ラスタライズ/変換とは経路を共有しない(core のバグが GT と相殺しないように)。
    * 乱数は固定シード。テスト対象の解像度は小さく(256〜1024)し、既定で全体 3 分以内に収める。
    * 同じ階層の test_maya_live_sync.py は mayapy 専用のため、このファイルは単独で実行する。
"""

import json
import os
import sys
import time
import warnings

import numpy as np
import pytest

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

import uvremap_core as uvc  # noqa: E402


# 重いテストは UVREMAP_SLOW=1 のときだけ実行する(マーカー slow。-m "not slow" でも除外できる)
_RUN_SLOW = bool(os.environ.get("UVREMAP_SLOW"))
warnings.filterwarnings("ignore", category=pytest.PytestUnknownMarkWarning)   # slow は設定ファイル無しで使う


def slow(fn):
    fn = pytest.mark.slow(fn)
    return fn if _RUN_SLOW else pytest.mark.skip(reason="slow test: set UVREMAP_SLOW=1")(fn)


# ============================================================================
# 合成メッシュ
# ============================================================================

_CUBE_FACES = [  # (法線, a, b) 右手系: a x b = 法線
    ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
    ((0, 1, 0), (0, 0, 1), (1, 0, 0)),
    ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
    ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
    ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
]


class Mesh:
    """合成メッシュ(コーナー単位)。shell は面ごとのシェル番号、loc はシェル内 [0,1]^2 座標。"""

    def __init__(self, counts, vid, pos, loc, shell):
        self.counts = np.asarray(counts, np.int32)
        self.vid = np.asarray(vid, np.int32)
        self.pos = np.asarray(pos, np.float64)      # (C,3)
        self.loc = np.asarray(loc, np.float64)      # (C,2)
        self.shell = np.asarray(shell, np.int32)    # (F,)

    @property
    def n_face(self):
        return len(self.counts)

    def corner_shell(self):
        return np.repeat(self.shell, self.counts)


def cube_sphere(n=10):
    """立方体球。6つのシェル(立方体の面ごと)、三角形 12*n*n 枚。"""
    pos, vid, loc, shell = [], [], [], []
    off = 0
    g = np.linspace(-1, 1, n + 1)
    S, T = np.meshgrid(g, g, indexing="xy")
    for k, (nn, a, b) in enumerate(_CUBE_FACES):
        nn, a, b = map(np.asarray, (nn, a, b))
        P = nn + S[..., None] * a + T[..., None] * b
        P /= np.linalg.norm(P, axis=-1, keepdims=True)
        P = P.reshape(-1, 3)
        q = np.stack([(S + 1) / 2, (T + 1) / 2], -1).reshape(-1, 2)
        idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
        v00, v10 = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel()
        v01, v11 = idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
        f = np.concatenate([np.stack([v00, v10, v11], 1), np.stack([v00, v11, v01], 1)])
        pos.append(P[f].reshape(-1, 3))
        loc.append(q[f].reshape(-1, 2))
        vid.append((f + off).ravel())
        shell.append(np.full(len(f), k))
        off += len(P)
    nf = sum(len(s) for s in shell)
    return Mesh(np.full(nf, 3), np.concatenate(vid), np.concatenate(pos),
                np.concatenate(loc), np.concatenate(shell))


def plates(n_plates=1, nx=4, ny=4, quads=True, size=1.0, gap=0.25):
    """板(平面)を n_plates 枚。1枚が1シェル。quads=True で四角形、False で三角形。

    板 k は x 方向に (size+gap)*k だけずらして置く。位置は (x, y, 0.15*sin(...)) の緩い曲面にして
    法線が一様にならないようにする。
    """
    pos, vid, loc, shell, counts = [], [], [], [], []
    off = 0
    for k in range(n_plates):
        gx, gy = np.meshgrid(np.linspace(0, 1, nx + 1), np.linspace(0, 1, ny + 1), indexing="xy")
        P = np.stack([gx * size + (size + gap) * k, gy * size,
                      0.15 * np.sin(2.5 * gx + k) * np.cos(2.0 * gy)], -1).reshape(-1, 3)
        q = np.stack([gx, gy], -1).reshape(-1, 2)
        idx = np.arange((nx + 1) * (ny + 1)).reshape(ny + 1, nx + 1)
        v00, v10 = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel()
        v01, v11 = idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
        if quads:
            f = np.stack([v00, v10, v11, v01], 1)
        else:
            f = np.concatenate([np.stack([v00, v10, v11], 1), np.stack([v00, v11, v01], 1)])
        pos.append(P[f].reshape(-1, 3))
        loc.append(q[f].reshape(-1, 2))
        vid.append((f + off).ravel())
        counts.append(np.full(len(f), f.shape[1]))
        shell.append(np.full(len(f), k))
        off += len(P)
    return Mesh(np.concatenate(counts), np.concatenate(vid), np.concatenate(pos),
                np.concatenate(loc), np.concatenate(shell))


# ---- レイアウト: シェルごとの (変換, (sx, sy), (u0, v0)) -----------------------

def _xf(q, kind):
    x, y = q[..., 0], q[..., 1]
    table = {
        "id": q,
        "rot90": np.stack([1 - y, x], -1),
        "rot180": np.stack([1 - x, 1 - y], -1),
        "rot270": np.stack([y, 1 - x], -1),
        "mirror": np.stack([1 - x, y], -1),
        "flipv": np.stack([x, 1 - y], -1),
        "rot270_mirror": np.stack([1 - y, 1 - x], -1),
    }
    return table[kind]


def apply_layout(mesh: Mesh, spec):
    """spec: {shell: (kind, (sx, sy), (u0, v0))} -> corner_uv (C,2) float64。"""
    uv = np.zeros_like(mesh.loc)
    cs = mesh.corner_shell()
    for s, (kind, sc, org) in spec.items():
        m = cs == s
        uv[m] = _xf(mesh.loc[m], kind) * np.asarray(sc) + np.asarray(org)
    return uv


LAYOUT_A = {  # 球: 全シェルがタイル 1001 の 3x2 グリッド、向きそのまま
    0: ("id", (0.30, 0.30), (0.02, 0.05)),
    1: ("id", (0.30, 0.30), (0.35, 0.05)),
    2: ("id", (0.30, 0.30), (0.68, 0.05)),
    3: ("id", (0.30, 0.30), (0.02, 0.55)),
    4: ("id", (0.30, 0.30), (0.35, 0.55)),
    5: ("id", (0.30, 0.30), (0.68, 0.55)),
}
LAYOUT_B = {  # 球: 移動・回転・拡大・反転、1001 と 1002 に分かれる
    0: ("id", (0.45, 0.45), (0.03, 0.03)),
    1: ("rot90", (0.45, 0.45), (0.52, 0.03)),
    2: ("rot180", (0.30, 0.30), (0.10, 0.60)),
    3: ("mirror", (0.45, 0.45), (1.03, 0.03)),
    4: ("rot270_mirror", (0.40, 0.40), (1.55, 0.05)),
    5: ("rot90", (0.45, 0.45), (1.30, 0.52)),
}


def uniform_layout(kind, scale=0.3):
    """LAYOUT_A と同じ位置・大きさで、全シェルに同じ変換 kind を掛ける。"""
    return {s: (kind, v[1], v[2]) for s, v in LAYOUT_A.items()} if scale == 0.3 else {
        s: (kind, (scale, scale), v[2]) for s, v in LAYOUT_A.items()}


# ---- スナップショット組み立て ------------------------------------------------

def snap_of(mesh: Mesh, corner_uv, *, ts=None, base=None, **kw):
    """Mesh + UV から Snapshot を作る。base を渡すとその三角形分割を使う(現行側)。"""
    ts = ts or {"Body": np.arange(mesh.n_face)}
    extra = {}
    if base is not None:
        extra = {"tri_face": base.tri_face, "tri_corner": base.tri_corner}
    return uvc.make_snapshot(mesh.counts, mesh.vid, mesh.pos, corner_uv, ts_faces=ts,
                             face_mesh=kw.pop("face_mesh", None), **extra, **kw)


def with_uv(snap, corner_uv, **kw):
    """snap と同じトポロジーで UV だけ差し替えた現行スナップショット。"""
    return uvc.make_snapshot(snap.face_vertex_counts, snap.corner_vid, snap.corner_pos,
                             corner_uv, ts_faces=dict(zip(snap.ts_names, snap.ts_faces)),
                             face_mesh=snap.face_mesh, tri_face=snap.tri_face,
                             tri_corner=snap.tri_corner, mesh_uuids=snap.meta["mesh_uuids"],
                             uv_set=snap.meta["uv_set"], **kw)


def reverse_faces(snap, **kw):
    """全フェースのコーナー順を逆順にした Snapshot(法線反転)。UVや位置も一緒に並べ替える。"""
    counts = snap.face_vertex_counts.astype(np.int64)
    offs = snap.face_offsets()
    order = np.concatenate([offs[f] + np.arange(counts[f])[::-1] for f in range(len(counts))])
    return uvc.make_snapshot(snap.face_vertex_counts, snap.corner_vid[order],
                             snap.corner_pos[order], snap.corner_uv[order],
                             ts_faces=dict(zip(snap.ts_names, snap.ts_faces)),
                             face_mesh=snap.face_mesh, tri_face=snap.tri_face,
                             tri_corner=snap.tri_corner, mesh_uuids=snap.meta["mesh_uuids"],
                             uv_set=snap.meta["uv_set"], **kw)


def rotate_faces(snap, shift=1, **kw):
    """各フェースのコーナー列を巡回回転した Snapshot(頂点ID列の巡回回転)。"""
    counts = snap.face_vertex_counts.astype(np.int64)
    offs = snap.face_offsets()
    order = np.concatenate([offs[f] + (np.arange(counts[f]) + shift) % counts[f]
                            for f in range(len(counts))])
    return uvc.make_snapshot(snap.face_vertex_counts, snap.corner_vid[order],
                             snap.corner_pos[order], snap.corner_uv[order],
                             ts_faces=dict(zip(snap.ts_names, snap.ts_faces)),
                             face_mesh=snap.face_mesh, tri_face=snap.tri_face,
                             tri_corner=snap.tri_corner, mesh_uuids=snap.meta["mesh_uuids"],
                             uv_set=snap.meta["uv_set"], **kw)


# ============================================================================
# 独立ベイカー(GT 用。core のラスタライザは使わない)
# ============================================================================

def _snap_tris(snap, ts_name):
    """Snapshot の三角形(基準の分割)ごとの (face, pos(3,3), uv(3,2))。TS のフェースのみ。"""
    faces = set(snap.faces_of(ts_name).tolist())
    offs = snap.face_offsets()
    out = []
    for t in range(len(snap.tri_face)):
        f = int(snap.tri_face[t])
        if f not in faces:
            continue
        ci = offs[f] + snap.tri_corner[t].astype(np.int64)
        out.append((f, snap.corner_pos[ci].astype(np.float64), snap.corner_uv[ci].astype(np.float64)))
    return out


def _frames(P, uv):
    """UV ヤコビアンによる接空間フレーム (T, B, N)(試作と同じ定義: T=dP/du, B は T に直交化)。"""
    E1, E2 = P[1] - P[0], P[2] - P[0]
    e1, e2 = uv[1] - uv[0], uv[2] - uv[0]
    det = e1[0] * e2[1] - e2[0] * e1[1]
    T = (E1 * e2[1] - E2 * e1[1]) / det
    B = (E2 * e1[0] - E1 * e2[0]) / det
    N = np.cross(E1, E2)
    N /= np.linalg.norm(N)
    T = T - N * (T @ N)
    T /= np.linalg.norm(T)
    B = B - N * (B @ N) - T * (B @ T)
    B /= np.linalg.norm(B)
    return T, B, N


BUMP_F, BUMP_A = 9.0, 0.045


def bump_grad(p):
    s, c = np.sin(BUMP_F * p), np.cos(BUMP_F * p)
    return BUMP_A * BUMP_F * np.stack([c[..., 0] * s[..., 1] * s[..., 2],
                                       s[..., 0] * c[..., 1] * s[..., 2],
                                       s[..., 0] * s[..., 1] * c[..., 2]], -1)


def bake(snap, ts_name, res, fn, *, udim=True, kind="color", n_chan=3, green_down=False):
    """snap の UV レイアウトに fn(3D点) を焼き込む。

    kind="normal": バンプ場 h(p) = A sin(fx) sin(fy) sin(fz) の接空間法線を、三角形ごとの
    UV フレームでエンコードする。返り値: ({tile: float32 (H,W,C)}, {tile: bool (H,W)})。
    """
    tiles, masks = {}, {}
    W, H = (res, res) if isinstance(res, int) else res
    for f, P, uv in _snap_tris(snap, ts_name):
        if abs((uv[1] - uv[0])[0] * (uv[2] - uv[0])[1] - (uv[2] - uv[0])[0] * (uv[1] - uv[0])[1]) < 1e-14:
            continue
        if kind == "normal":
            T, B, N = _frames(P, uv)
        lo = np.floor(uv.min(0)).astype(int)
        hi = np.maximum(np.floor(uv.max(0) - 1e-9).astype(int), lo)
        for iv in range(lo[1], hi[1] + 1):
            for iu in range(lo[0], hi[0] + 1):
                if udim and (iu < 0 or iu > 9 or iv < 0):
                    continue
                key = 1001 + iu + 10 * iv if udim else 1001
                loc = uv - np.array([iu, iv])
                px = loc[:, 0] * W - 0.5
                py = (1 - loc[:, 1]) * H - 0.5
                x0, x1 = max(int(np.ceil(px.min())), 0), min(int(np.floor(px.max())), W - 1)
                y0, y1 = max(int(np.ceil(py.min())), 0), min(int(np.floor(py.max())), H - 1)
                if x1 < x0 or y1 < y0:
                    continue
                xs = np.arange(x0, x1 + 1)[None, :]
                ys = np.arange(y0, y1 + 1)[:, None]
                d = (py[1] - py[2]) * (px[0] - px[2]) + (px[2] - px[1]) * (py[0] - py[2])
                l0 = ((py[1] - py[2]) * (xs - px[2]) + (px[2] - px[1]) * (ys - py[2])) / d
                l1 = ((py[2] - py[0]) * (xs - px[2]) + (px[0] - px[2]) * (ys - py[2])) / d
                l2 = 1 - l0 - l1
                m = (l0 >= -1e-9) & (l1 >= -1e-9) & (l2 >= -1e-9)
                if not m.any():
                    continue
                yy, xx = np.nonzero(m)
                p = (l0[m][:, None] * P[0] + l1[m][:, None] * P[1] + l2[m][:, None] * P[2])
                if kind == "color":
                    val = fn(p)
                else:
                    g = bump_grad(p)
                    gs = g - N * (g @ N)[:, None]
                    nw = N - gs
                    nw /= np.linalg.norm(nw, axis=1, keepdims=True)
                    ts = np.stack([nw @ T, nw @ B, nw @ N], 1)
                    if green_down:
                        ts[:, 1] = -ts[:, 1]
                    val = ts * 0.5 + 0.5
                if key not in tiles:
                    tiles[key] = np.zeros((H, W, val.shape[1]), np.float32)
                    masks[key] = np.zeros((H, W), bool)
                tiles[key][yy + y0, xx + x0] = val
                masks[key][yy + y0, xx + x0] = True
    return tiles, masks


# ---- 信号 -----------------------------------------------------------------

def f_smooth(p):
    """低周波カラー(幾何の正しさだけを見るための帯域制限信号)。"""
    return np.stack([0.5 + 0.4 * np.sin(2.0 * p[..., 0] + 1.0) * np.cos(1.5 * p[..., 1]),
                     0.5 + 0.4 * np.sin(1.7 * p[..., 1] - 0.5 * p[..., 2]),
                     0.5 + 0.4 * np.cos(2.3 * p[..., 2] + p[..., 0])], -1).astype(np.float32)


def _hash3(c):
    c = c.astype(np.int64)
    h = (c[..., 0] * 73856093) ^ (c[..., 1] * 19349663) ^ (c[..., 2] * 83492791)
    h = (h ^ (h >> 13)) * 1274126177
    return (h ^ (h >> 16)) & 0x7FFFFFFF


def f_detail(p):
    """細部カラー: 3Dチェッカー x グラデーション x 擬似文字(0.04 のセル)。"""
    grad = 0.25 + 0.5 * (p + 1) / 2
    chk = (np.floor(p / 0.1).sum(-1).astype(np.int64) & 1).astype(np.float32)
    col = grad * (0.6 + 0.4 * chk)[..., None]
    g = 0.04
    cell = np.floor(p / g)
    fr = p / g - cell
    ax = np.abs(p).argmax(-1)
    i1 = np.where(ax == 0, 1, 0)
    i2 = np.where(ax == 2, 1, 2)
    a = np.take_along_axis(fr, i1[..., None], -1)[..., 0]
    b = np.take_along_axis(fr, i2[..., None], -1)[..., 0]
    inside = (a > 0.15) & (a < 0.85) & (b > 0.1) & (b < 0.9)
    sx = np.clip(((a - 0.15) / 0.7 * 3).astype(np.int64), 0, 2)
    sy = np.clip(((b - 0.1) / 0.8 * 5).astype(np.int64), 0, 4)
    h = _hash3(cell)
    bit = (h >> (sx + 3 * sy)) & 1
    ink = inside & (bit == 1) & (((h >> 20) & 3) != 0)
    return np.where(ink[..., None], col * 0.15, col).astype(np.float32)


def noise_signal(cell):
    """3D 空間のセル(幅 cell)ごとに独立な白色ノイズ。cell が小さいほど高周波。"""
    def fn(p):
        h = _hash3(np.floor(p / cell))
        r = (h % 1000003) / 1000003.0
        g = ((h // 7) % 999983) / 999983.0
        b = ((h // 13) % 999961) / 999961.0
        return np.stack([r, g, b], -1).astype(np.float32)
    return fn


# ---- 指標 -----------------------------------------------------------------

def _shrink(mask, k):
    m = mask
    H, W = m.shape
    for _ in range(k):
        pad = np.zeros((H + 2, W + 2), bool)
        pad[1:-1, 1:-1] = m
        out = m.copy()
        for dy in range(3):
            for dx in range(3):
                out &= pad[dy:dy + H, dx:dx + W]
        m = out
    return m


def psnr(pred, gt, masks, region="interior", band=2):
    """タイルをまとめた PSNR。interior = 被覆 - 境界 band、seam = 境界 band のみ。"""
    err = []
    for t in gt:
        m = masks[t]
        inner = _shrink(m, band)
        sel = inner if region == "interior" else (m & ~inner if region == "seam" else m)
        err.append((pred[t][sel] - gt[t][sel]).ravel().astype(np.float64))
    d = np.concatenate(err)
    return float(10 * np.log10(1.0 / max(float((d ** 2).mean()), 1e-20)))


def angle_err_deg(a, b):
    na = a[..., :3] * 2 - 1
    nb = b[..., :3] * 2 - 1
    na = na / np.linalg.norm(na, axis=-1, keepdims=True)
    nb = nb / np.linalg.norm(nb, axis=-1, keepdims=True)
    return np.degrees(np.arccos(np.clip((na * nb).sum(-1), -1, 1)))


def pad_tiles(tiles, masks, px=16):
    """Painter の書き出しパディング(infinite)の代わりに膨張させる。"""
    return {t: uvc._dilate(tiles[t], masks[t], px)[0] for t in tiles}


def shell_map(snap, ts_name, mesh, res, udim=True):
    """各テクセルのシェル番号(-1 = 空)。"""
    W = H = res
    out = {}
    for f, P, uv in _snap_tris(snap, ts_name):
        lo = np.floor(uv.min(0)).astype(int)
        hi = np.maximum(np.floor(uv.max(0) - 1e-9).astype(int), lo)
        for iv in range(lo[1], hi[1] + 1):
            for iu in range(lo[0], hi[0] + 1):
                key = 1001 + iu + 10 * iv if udim else 1001
                loc = uv - np.array([iu, iv])
                px = loc[:, 0] * W - 0.5
                py = (1 - loc[:, 1]) * H - 0.5
                x0, x1 = max(int(np.ceil(px.min())), 0), min(int(np.floor(px.max())), W - 1)
                y0, y1 = max(int(np.ceil(py.min())), 0), min(int(np.floor(py.max())), H - 1)
                if x1 < x0 or y1 < y0:
                    continue
                xs = np.arange(x0, x1 + 1)[None, :]
                ys = np.arange(y0, y1 + 1)[:, None]
                d = (py[1] - py[2]) * (px[0] - px[2]) + (px[2] - px[1]) * (py[0] - py[2])
                l0 = ((py[1] - py[2]) * (xs - px[2]) + (px[2] - px[1]) * (ys - py[2])) / d
                l1 = ((py[2] - py[0]) * (xs - px[2]) + (px[0] - px[2]) * (ys - py[2])) / d
                m = (l0 >= -1e-9) & (l1 >= -1e-9) & (1 - l0 - l1 >= -1e-9)
                yy, xx = np.nonzero(m)
                if key not in out:
                    out[key] = np.full((H, W), -1, np.int64)
                out[key][yy + y0, xx + x0] = mesh.shell[f]
    return out


# ============================================================================
# ケースビルダー(旧UVで焼いたテクスチャ → 新UVへ変換)
# ============================================================================

def concat_meshes(a: Mesh, b: Mesh):
    """2つのメッシュを連結する(頂点IDはずらす)。返り値: (Mesh, face_mesh)。"""
    shift = int(a.vid.max()) + 1
    shell_shift = int(a.shell.max()) + 1
    m = Mesh(np.concatenate([a.counts, b.counts]), np.concatenate([a.vid, b.vid + shift]),
             np.concatenate([a.pos, b.pos]), np.concatenate([a.loc, b.loc]),
             np.concatenate([a.shell, b.shell + shell_shift]))
    fm = np.concatenate([np.zeros(a.n_face, np.int32), np.ones(b.n_face, np.int32)])
    return m, fm


class Case:
    """球 + 2つのUVレイアウト + 焼き込み済みテクスチャ一式。重い計算は遅延評価してキャッシュする。"""

    def __init__(self, src, dst, signal, res, *, pad=True, kind="color", green_down=False,
                 n=10, udim=True, colorspace="raw"):
        self.mesh = cube_sphere(n)
        self.res = res
        self.udim = udim
        self.signal = signal
        self.kind = kind
        self.green_down = green_down
        self.A = snap_of(self.mesh, apply_layout(self.mesh, src))
        self.B = with_uv(self.A, apply_layout(self.mesh, dst))
        self.decision = uvc.classify(self.A, self.B, "Body")
        self.pad = pad
        self.spec = uvc.ChannelSpec("Normal" if kind == "normal" else "BaseColor", colorspace,
                                    kind, normal_green_down=green_down)
        self._old = None
        self._gt = None
        self._conv = {}

    def _bake(self, snap):
        if self.kind == "normal":
            fn = None
        elif isinstance(self.signal, str):
            fn = SIGNALS[self.signal]
        else:                       # 数値 = 3D空間のノイズセル幅
            fn = noise_signal(float(self.signal))
        return bake(snap, "Body", self.res, fn, udim=self.udim, kind=self.kind,
                    green_down=self.green_down)

    @property
    def old(self):
        """旧UVのテクスチャ(パディング済み = Painter の書き出し相当)。"""
        if self._old is None:
            t, m = self._bake(self.A)
            self._old_raw, self._old_mask = t, m
            self._old = pad_tiles(t, m) if self.pad else t
        return self._old

    @property
    def gt(self):
        """新UVで直接焼いた地上真値 (tiles, masks)。"""
        if self._gt is None:
            self._gt = self._bake(self.B)
        return self._gt

    def convert(self, **kw):
        key = tuple(sorted(kw.items()))
        if key not in self._conv:
            kw = dict(kw)
            kw.setdefault("udim", self.udim)
            self._conv[key] = uvc.convert_channel(self.old, self.A, self.B, "Body", self.decision,
                                                  self.spec, self.res, **kw)
        return self._conv[key]

    def probe(self, new_tiles, **kw):
        kw.setdefault("udim", self.udim)
        return uvc.probe_verify(self.old, new_tiles, self.A, self.B, "Body", self.spec, **kw)

    def shells(self):
        return shell_map(self.B, "Body", self.mesh, self.res, udim=self.udim)


SIGNALS = {"smooth": f_smooth, "detail": f_detail, "noise": noise_signal(0.02)}

_CASE_CACHE = {}


def get_case(src, dst, signal="smooth", res=256, **kw):
    """src, dst はレイアウト名(LAYOUTS のキー)。同じ引数なら同じ Case を返す。"""
    key = (src, dst, signal, res, tuple(sorted(kw.items())))
    if key not in _CASE_CACHE:
        _CASE_CACHE[key] = Case(LAYOUTS[src], LAYOUTS[dst], signal, res, **kw)
    return _CASE_CACHE[key]


LAYOUT_STRADDLE = dict(LAYOUT_A)       # シェル2が u=0.86..1.16 でタイル境界をまたぐ(他と重ならない)
LAYOUT_STRADDLE[2] = ("id", (0.30, 0.30), (0.86, 0.05))
LAYOUT_OVERLAP = dict(LAYOUT_A)        # シェル1をシェル0に重ねる
LAYOUT_OVERLAP[1] = LAYOUT_OVERLAP[0]
LAYOUT_WRAPPED = {                     # 非UDIM向け: 0..1 の外(正・負)へ出したシェル
    0: ("id", (0.30, 0.30), (1.02, 0.05)),
    1: ("id", (0.30, 0.30), (0.35, 2.05)),
    2: ("id", (0.30, 0.30), (-0.32, 0.05)),
    3: ("id", (0.30, 0.30), (3.02, 5.55)),
    4: ("id", (0.30, 0.30), (0.35, 0.55)),
    5: ("id", (0.30, 0.30), (0.68, 0.55)),
}

LAYOUTS = {
    "A": LAYOUT_A,
    "straddle": LAYOUT_STRADDLE,
    "overlap": LAYOUT_OVERLAP,
    "wrapped": LAYOUT_WRAPPED,
    "B": LAYOUT_B,
    "rot90": uniform_layout("rot90"),
    "rot180": uniform_layout("rot180"),
    "rot270": uniform_layout("rot270"),
    "mirror": uniform_layout("mirror"),
    "flipv": uniform_layout("flipv"),
    "rot270_mirror": uniform_layout("rot270_mirror"),
    # 平行移動 + 一様拡大のみ(回転なし)
    "scaled": {s: ("id", (0.31, 0.31), (0.03 + 0.33 * (s % 3), 0.04 + 0.5 * (s // 3)))
               for s in range(6)},
}


# ---- 故障注入(プローブが検出すべき不具合) ---------------------------------

def inject_shift_u(tiles, frac):
    """出力を U 方向にタイル幅の frac だけずらす(環状)。"""
    out = {}
    for k, t in tiles.items():
        out[k] = np.roll(t, int(round(frac * t.shape[1])), axis=1)
    return out


def inject_flip_shell(tiles, shells, shell):
    """1つのシェルだけを、その外接矩形内で左右反転する。"""
    out = {k: t.copy() for k, t in tiles.items()}
    n_hit = 0
    for k, sm in shells.items():
        ys, xs = np.nonzero(sm == shell)
        if ys.size == 0 or k not in out:
            continue
        y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
        out[k][y0:y1 + 1, x0:x1 + 1] = tiles[k][y0:y1 + 1, x0:x1 + 1][:, ::-1]
        n_hit += 1
    assert n_hit, "inject_flip_shell: shell not found"
    return out


def inject_swap_tiles(tiles, a, b):
    """タイル番号の取り違え(a と b の中身を入れ替える)。"""
    out = dict(tiles)
    out[a], out[b] = tiles[b], tiles[a]
    return out


def inject_relabel_tile(tiles, old, new):
    out = dict(tiles)
    out[new] = out.pop(old)
    return out


def inject_dropout(tiles, coverage, frac, start=0.3):
    """被覆テクセルの約 frac を、縦帯状に黒く欠落させる(全タイルの合計で frac)。"""
    out = {k: t.copy() for k, t in tiles.items()}
    total = sum(int(c.sum()) for c in coverage.values())
    want = frac * total
    done = 0
    for k in sorted(coverage):
        cov = coverage[k]
        col = cov.sum(axis=0)
        cum = np.cumsum(col)
        c0 = int(np.searchsorted(cum, start * cum[-1]))
        c1 = c0
        take = min(want - done, 0.9 * col.sum())
        while c1 < len(col) and col[c0:c1 + 1].sum() < take:
            c1 += 1
        out[k][:, c0:c1 + 1] = 0.0
        done += col[c0:c1 + 1].sum()
        if done >= want:
            break
    return out


# ============================================================================
# テスト: スナップショット / 指紋 / UV ハッシュ
# ============================================================================

PLATE_LAYOUT = {0: ("id", (0.4, 0.4), (0.05, 0.05)), 1: ("id", (0.4, 0.4), (0.55, 0.05))}


def multi_snapshot(with_defects=False):
    """球(TS=Body) + 板2枚(TS=Plate) を1つのスナップショットにしたもの(メッシュ2つ)。"""
    sph, pl = cube_sphere(4), plates(2, 3, 3, quads=True)
    m, fm = concat_meshes(sph, pl)
    uv = np.concatenate([apply_layout(sph, LAYOUT_A), apply_layout(pl, PLATE_LAYOUT)])
    vid = m.vid.copy()
    if with_defects:
        offs = np.cumsum(m.counts) - m.counts
        uv[offs[3]:offs[3] + 3] = np.nan                     # UV 無しフェース(球の face 3)
        f_nm = sph.n_face + 2                                # 板の四角形: 頂点ID重複(非多様体)
        vid[offs[f_nm] + 2] = vid[offs[f_nm]]
    ts = {"Body": np.arange(sph.n_face), "Plate": np.arange(sph.n_face, m.n_face)}
    snap = uvc.make_snapshot(m.counts, vid, m.pos, uv, ts_faces=ts, face_mesh=fm,
                             mesh_uuids=["uuid-sphere", "uuid-plate"], uv_set="map1")
    return m, snap


class TestSnapshot:
    def test_roundtrip_exact(self, tmp_path):
        _, snap = multi_snapshot(with_defects=True)
        d = tmp_path / "baseline" / snap.meta["baseline_id"]
        uvc.save_snapshot(d, snap)
        back = uvc.load_snapshot(d)
        for k in ("face_vertex_counts", "corner_vid", "corner_pos", "corner_uv", "face_mesh",
                  "tri_face", "tri_corner"):
            a, b = getattr(snap, k), getattr(back, k)
            assert a.dtype == b.dtype, k
            np.testing.assert_array_equal(a, b, err_msg=k)      # NaN は同位置で一致とみなす
        assert back.ts_names == snap.ts_names == ["Body", "Plate"]
        for a, b in zip(snap.ts_faces, back.ts_faces):
            np.testing.assert_array_equal(a, b)
        assert back.meta == snap.meta
        assert back.warnings == snap.warnings and back.warnings   # 欠陥の警告が往復で失われない
        # 読み戻したスナップショットで指紋とハッシュを再計算しても同じ
        assert uvc.compute_topology_fingerprint(back) == snap.meta["topology_fingerprint"]
        assert uvc.compute_uv_hash(back) == snap.meta["uv_hash"]
        assert uvc.compute_uv_hash(back, "Plate") == uvc.compute_uv_hash(snap, "Plate")

    def test_file_layout_follows_contract(self, tmp_path):
        _, snap = multi_snapshot()
        uvc.save_snapshot(tmp_path / "bl", snap)
        assert sorted(p.name for p in (tmp_path / "bl").iterdir()) == ["mesh.npz", "meta.json"]
        with np.load(tmp_path / "bl" / "mesh.npz") as z:
            assert set(z.files) == {"face_vertex_counts", "corner_vid", "corner_pos", "corner_uv",
                                    "face_mesh", "tri_face", "tri_corner",
                                    "ts_faces__0", "ts_faces__1"}
            assert z["corner_vid"].dtype == np.int32 and z["corner_pos"].dtype == np.float32
            assert z["tri_corner"].dtype == np.int8 and z["tri_corner"].shape[1] == 3
            c = int(z["face_vertex_counts"].sum())
            assert z["corner_uv"].shape == (c, 2) and z["corner_pos"].shape == (c, 3)
        meta = json.loads((tmp_path / "bl" / "meta.json").read_text(encoding="utf-8"))
        for k in ("schema", "baseline_id", "uv_set", "ts_names", "mesh_uuids",
                  "topology_fingerprint", "uv_hash", "bbox_min", "bbox_max", "created"):
            assert k in meta, k
        assert meta["schema"] == 1 and meta["mesh_uuids"] == ["uuid-sphere", "uuid-plate"]

    def test_save_overwrites_atomically(self, tmp_path):
        _, snap = multi_snapshot()
        uvc.save_snapshot(tmp_path / "bl", snap)
        uvc.save_snapshot(tmp_path / "bl", snap)        # 上書きしても壊れない
        assert not list((tmp_path / "bl").glob("*.tmp"))
        assert uvc.load_snapshot(tmp_path / "bl").meta == snap.meta

    def test_load_errors_are_typed(self, tmp_path):
        with pytest.raises(uvc.SnapshotError):
            uvc.load_snapshot(tmp_path / "missing")
        _, snap = multi_snapshot()
        uvc.save_snapshot(tmp_path / "bl", snap)
        meta = json.loads((tmp_path / "bl" / "meta.json").read_text(encoding="utf-8"))
        meta["schema"] = 99
        (tmp_path / "bl" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(uvc.SnapshotError):
            uvc.load_snapshot(tmp_path / "bl")
        (tmp_path / "bl" / "meta.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(uvc.SnapshotError):
            uvc.load_snapshot(tmp_path / "bl")

    def test_make_snapshot_rejects_inconsistent_arrays(self):
        counts, vid = [3, 3], [0, 1, 2, 2, 1, 3]
        pos, uv = np.zeros((6, 3)), np.zeros((6, 2))
        uvc.make_snapshot(counts, vid, pos, uv)                      # 正常
        with pytest.raises(uvc.SnapshotError):
            uvc.make_snapshot(counts, vid[:-1], pos, uv)
        with pytest.raises(uvc.SnapshotError):
            uvc.make_snapshot(counts, vid, pos[:-1], uv)
        with pytest.raises(uvc.SnapshotError):
            uvc.make_snapshot(counts, vid, pos, uv, ts_faces={"Body": [0, 5]})
        with pytest.raises(uvc.SnapshotError):
            uvc.make_snapshot(counts, vid, pos, uv, tri_face=[0, 9], tri_corner=[[0, 1, 2]] * 2)

    def test_faces_of_unknown_ts(self):
        _, snap = multi_snapshot()
        with pytest.raises(uvc.TextureSetNotFound):
            snap.faces_of("Nope")

    def test_triangulate_fan(self):
        tf, tc = uvc.triangulate_fan([3, 4, 5, 3])
        assert tf.tolist() == [0, 1, 1, 2, 2, 2, 3]
        assert tc.tolist() == [[0, 1, 2], [0, 1, 2], [0, 2, 3], [0, 1, 2], [0, 2, 3], [0, 3, 4], [0, 1, 2]]
        assert tf.dtype == np.int32 and tc.dtype == np.int8
        tf, tc = uvc.triangulate_fan([])
        assert tf.size == 0 and tc.shape == (0, 3)

    def test_defects_are_detected_and_reported(self):
        m, snap = multi_snapshot(with_defects=True)
        assert any(w.startswith("non_manifold_faces: 1") for w in snap.warnings)
        assert any(w.startswith("faces_without_uv: 1") for w in snap.warnings)
        assert uvc.find_faces_without_uv(snap).nonzero()[0].tolist() == [3]
        assert uvc.find_nonmanifold_faces(snap).nonzero()[0].tolist() == [cube_sphere(4).n_face + 2]
        _, clean = multi_snapshot()
        assert clean.warnings == []


class TestFingerprint:
    def test_uv_only_change_keeps_topology_fingerprint(self):
        m = cube_sphere(6)
        A = snap_of(m, apply_layout(m, LAYOUT_A))
        B = with_uv(A, apply_layout(m, LAYOUT_B))
        assert uvc.compute_topology_fingerprint(A) == uvc.compute_topology_fingerprint(B)
        assert A.meta["topology_fingerprint"] == B.meta["topology_fingerprint"]
        assert uvc.compute_uv_hash(A) != uvc.compute_uv_hash(B)

    def test_topology_change_changes_fingerprint(self):
        m = cube_sphere(6)
        A = snap_of(m, apply_layout(m, LAYOUT_A))
        fp = uvc.compute_topology_fingerprint(A)
        vid = m.vid.copy()
        vid[[0, 4]] = vid[[4, 0]]                                  # 2 つの三角形の頂点IDを入れ替え
        S = uvc.make_snapshot(m.counts, vid, m.pos, A.corner_uv)
        assert uvc.compute_topology_fingerprint(S) != fp
        S = uvc.make_snapshot(m.counts, m.vid + 1, m.pos, A.corner_uv)   # 頂点IDの振り直し
        assert uvc.compute_topology_fingerprint(S) != fp
        # フェース数・頂点数の変化: 隣り合う2三角形を1つの四角形にまとめる
        counts = m.counts.tolist()
        counts[:2] = [4]
        vid2 = np.concatenate([m.vid[:3], m.vid[3:6][-1:], m.vid[6:]])
        S = uvc.make_snapshot(counts, vid2, np.zeros((len(vid2), 3)), np.zeros((len(vid2), 2)))
        assert uvc.compute_topology_fingerprint(S) != fp

    @pytest.mark.parametrize("shift", [1, 2])
    def test_cyclic_rotation_of_vertex_ids_is_invariant(self, shift):
        pl = plates(2, 3, 3, quads=True)
        A = snap_of(pl, apply_layout(pl, PLATE_LAYOUT))
        R = rotate_faces(A, shift)
        assert not np.array_equal(R.corner_vid, A.corner_vid)
        assert uvc.compute_topology_fingerprint(R) == uvc.compute_topology_fingerprint(A)

    def test_cyclic_rotation_invariant_for_non_manifold_faces(self):
        _, snap = multi_snapshot(with_defects=True)               # 同じIDを2回含む四角形がある
        for shift in (1, 2, 3):
            R = rotate_faces(snap, shift)
            assert uvc.compute_topology_fingerprint(R) == uvc.compute_topology_fingerprint(snap)

    def test_reversed_face_order_is_a_different_fingerprint(self):
        m = cube_sphere(5)
        A = snap_of(m, apply_layout(m, LAYOUT_A))
        Rv = reverse_faces(A)
        assert uvc.compute_topology_fingerprint(Rv) != uvc.compute_topology_fingerprint(A)
        # 反転後のトポロジーを「逆順にしたもの」の指紋は、元の指紋に一致する
        assert uvc.compute_flipped_topology_fingerprint(Rv) == uvc.compute_topology_fingerprint(A)
        assert uvc.compute_flipped_topology_fingerprint(A) == uvc.compute_topology_fingerprint(Rv)

    def test_fingerprint_ignores_texture_set_membership(self):
        m = cube_sphere(5)
        A = snap_of(m, apply_layout(m, LAYOUT_A))
        half = snap_of(m, apply_layout(m, LAYOUT_A), ts={"Body": np.arange(m.n_face // 2)})
        assert uvc.compute_topology_fingerprint(A) == uvc.compute_topology_fingerprint(half)


class TestUvHash:
    def test_stable_and_sensitive(self):
        m = cube_sphere(5)
        uv = apply_layout(m, LAYOUT_A)
        A = snap_of(m, uv)
        assert uvc.compute_uv_hash(A) == uvc.compute_uv_hash(snap_of(m, uv.copy()))
        uv2 = uv.copy()
        uv2[17, 0] += 1e-3                                         # 1 コーナーだけ動かす
        assert uvc.compute_uv_hash(snap_of(m, uv2)) != uvc.compute_uv_hash(A)
        assert uvc.compute_uv_hash(snap_of(m, uv2), "Body") != uvc.compute_uv_hash(A, "Body")

    def test_per_texture_set_scope(self):
        m, snap = multi_snapshot()
        n_body = cube_sphere(4).n_face
        offs = snap.face_offsets()
        uv2 = snap.corner_uv.copy()
        uv2[offs[n_body]:offs[n_body] + 4] += 0.01                 # 板(Plate)のフェース1つだけ変更
        cur = with_uv(snap, uv2)
        assert uvc.compute_uv_hash(cur, "Body") == uvc.compute_uv_hash(snap, "Body")
        assert uvc.compute_uv_hash(cur, "Plate") != uvc.compute_uv_hash(snap, "Plate")
        assert uvc.compute_uv_hash(cur) != uvc.compute_uv_hash(snap)

    def test_negative_zero_and_nan_are_normalised(self):
        m = plates(1, 2, 2, quads=False)
        uv = apply_layout(m, {0: ("id", (0.5, 0.5), (0.0, 0.0))})
        uv_neg = uv.copy()
        uv_neg[uv_neg == 0.0] = -0.0
        assert uvc.compute_uv_hash(snap_of(m, uv)) == uvc.compute_uv_hash(snap_of(m, uv_neg))
        a, b = uv.copy(), uv.copy()
        a[0:3] = np.nan
        b[0:3] = np.float32("nan")
        assert uvc.compute_uv_hash(snap_of(m, a)) == uvc.compute_uv_hash(snap_of(m, b))
        assert uvc.compute_uv_hash(snap_of(m, a)) != uvc.compute_uv_hash(snap_of(m, uv))

    def test_unknown_ts(self):
        _, snap = multi_snapshot()
        with pytest.raises(uvc.TextureSetNotFound):
            uvc.compute_uv_hash(snap, "Nope")


# ============================================================================
# テスト: classify
# ============================================================================

@pytest.fixture(scope="module")
def sph():
    m = cube_sphere(6)
    A = snap_of(m, apply_layout(m, LAYOUT_A))
    return m, A, with_uv(A, apply_layout(m, LAYOUT_B))


class TestClassify:
    def test_same_topology_is_F(self, sph):
        m, A, B = sph
        d = uvc.classify(A, B, "Body")
        assert d.method == "F" and d.reasons == [] and d.warnings == []
        assert d.details["fingerprint"]["baseline"] == d.details["fingerprint"]["current"]

    def test_identical_snapshots_are_F(self, sph):
        _, A, _ = sph
        assert uvc.classify(A, A, "Body").method == "F"

    def test_cyclic_rotation_is_still_F(self):
        pl = plates(2, 3, 3, quads=True)
        A = snap_of(pl, apply_layout(pl, PLATE_LAYOUT))
        assert uvc.classify(A, rotate_faces(A, 1), "Body").method == "F"

    def test_all_faces_reversed_is_F_FLIPPED(self, sph):
        m, A, _ = sph
        d = uvc.classify(A, reverse_faces(A), "Body")
        assert d.method == "F_FLIPPED" and d.reasons == []
        assert any("normals_flipped" in w for w in d.warnings)

    def test_topology_change_is_STOP(self, sph):
        m, A, _ = sph
        vid = m.vid.copy()
        vid[[0, 4]] = vid[[4, 0]]
        cur = uvc.make_snapshot(m.counts, vid, m.pos, A.corner_uv, ts_faces={"Body": np.arange(m.n_face)},
                                tri_face=A.tri_face, tri_corner=A.tri_corner)
        d = uvc.classify(A, cur, "Body")
        assert d.method == "STOP" and "topology_changed" in d.reasons
        assert d.details["faces_changed"] >= 1

    def test_face_count_change_is_STOP(self, sph):
        m, A, _ = sph
        n = A.corner_vid.size - 3                                # 最後の三角形を削除
        cur = uvc.make_snapshot(m.counts[:-1], m.vid[:n], m.pos[:n], A.corner_uv[:n],
                                ts_faces={"Body": np.arange(m.n_face - 1)})
        d = uvc.classify(A, cur, "Body")
        assert d.method == "STOP" and d.reasons == ["topology_changed"]
        assert d.details["face_count"] == {"baseline": m.n_face, "current": m.n_face - 1}

    def test_partially_flipped_is_STOP_and_says_so(self, sph):
        m, A, _ = sph
        order = np.arange(A.corner_vid.size)
        order[0:3] = order[0:3][::-1]                            # 先頭の1フェースだけ反転
        cur = uvc.make_snapshot(m.counts, A.corner_vid[order], A.corner_pos[order], A.corner_uv[order],
                                ts_faces={"Body": np.arange(m.n_face)},
                                tri_face=A.tri_face, tri_corner=A.tri_corner)
        d = uvc.classify(A, cur, "Body")
        assert d.method == "STOP" and d.reasons == ["topology_changed"]
        assert d.details.get("partially_flipped") is True and d.details["faces_flipped"] == 1

    def test_ts_missing_in_current(self, sph):
        m, A, B = sph
        other = uvc.make_snapshot(B.face_vertex_counts, B.corner_vid, B.corner_pos, B.corner_uv,
                                  ts_faces={"Other": np.arange(m.n_face)},
                                  tri_face=A.tri_face, tri_corner=A.tri_corner)
        d = uvc.classify(A, other, "Body")
        assert d.method == "STOP" and d.reasons == ["ts_missing"]
        assert d.details["ts_missing_in"] == ["current"]

    def test_ts_missing_in_baseline_and_in_both(self, sph):
        _, A, B = sph
        d = uvc.classify(A, B, "Nope")
        assert d.method == "STOP" and d.reasons == ["ts_missing"]
        assert d.details["ts_missing_in"] == ["baseline", "current"]

    def test_bbox_change_is_a_warning_only(self, sph):
        m, A, B = sph
        big = uvc.make_snapshot(B.face_vertex_counts, B.corner_vid, B.corner_pos * 1.05, B.corner_uv,
                                ts_faces={"Body": np.arange(m.n_face)},
                                tri_face=A.tri_face, tri_corner=A.tri_corner)
        d = uvc.classify(A, big, "Body")
        assert d.method == "F" and any(w.startswith("bbox_changed") for w in d.warnings)
        assert d.details["bbox_rel_diff"] > 1e-3
        # 許容内の変化は警告なし。許容を広げれば警告も消える
        tiny = uvc.make_snapshot(B.face_vertex_counts, B.corner_vid, B.corner_pos * 1.0001, B.corner_uv,
                                 ts_faces={"Body": np.arange(m.n_face)},
                                 tri_face=A.tri_face, tri_corner=A.tri_corner)
        assert uvc.classify(A, tiny, "Body").warnings == []
        assert uvc.classify(A, big, "Body", bbox_tol=0.2).warnings == []

    def test_ts_membership_change_stays_F_with_warning(self, sph):
        m, A, B = sph
        half = m.n_face // 2
        cur = uvc.make_snapshot(B.face_vertex_counts, B.corner_vid, B.corner_pos, B.corner_uv,
                                ts_faces={"Body": np.arange(half + 10)},
                                tri_face=A.tri_face, tri_corner=A.tri_corner)
        base = uvc.make_snapshot(A.face_vertex_counts, A.corner_vid, A.corner_pos, A.corner_uv,
                                 ts_faces={"Body": np.arange(half)},
                                 tri_face=A.tri_face, tri_corner=A.tri_corner)
        d = uvc.classify(base, cur, "Body")
        assert d.method == "F"
        assert any(w.startswith("ts_faces_changed") for w in d.warnings)
        assert d.details["ts_faces_added"] == 10

    def test_mesh_uuid_change_is_a_warning(self, sph):
        m, A, B = sph
        cur = uvc.make_snapshot(B.face_vertex_counts, B.corner_vid, B.corner_pos, B.corner_uv,
                                ts_faces={"Body": np.arange(m.n_face)}, mesh_uuids=["renamed"],
                                tri_face=A.tri_face, tri_corner=A.tri_corner)
        d = uvc.classify(A, cur, "Body")
        assert d.method == "F" and "mesh_uuids_changed" in d.warnings

    def test_stop_decision_is_refused_by_convert_and_P_does_not_exist(self, sph):
        m, A, B = sph
        stop = uvc.Decision("STOP", ["topology_changed"])
        spec = uvc.ChannelSpec("BaseColor", "srgb", "color")
        with pytest.raises(uvc.ConversionStopped) as ei:
            uvc.convert_channel({1001: np.zeros((8, 8, 3), np.float32)}, A, B, "Body", stop, spec, 8)
        assert ei.value.reasons == ["topology_changed"]
        # 方式P・最近接探索は実装しない: "P" を渡されても変換せず停止する
        with pytest.raises(uvc.ConversionStopped):
            uvc.convert_channel({1001: np.zeros((8, 8, 3), np.float32)}, A, B, "Body",
                                uvc.Decision("P"), spec, 8)


# ============================================================================
# テスト: convert_channel(幾何)
# ============================================================================

def _dil(mask, k):
    """8近傍の膨張を k 回(画像の外は無効)。"""
    m = mask
    H, W = m.shape
    for _ in range(k):
        pad = np.zeros((H + 2, W + 2), bool)
        pad[1:-1, 1:-1] = m
        out = m.copy()
        for dy in range(3):
            for dx in range(3):
                out |= pad[dy:dy + H, dx:dx + W]
        m = out
    return m


def check_against_gt(case, res=None, *, interior_db=70.0, seam_db=45.0, cov_mismatch=0.002, **kw):
    """変換結果が地上真値(新UVで直接焼いたもの)と一致するか。タイル集合・被覆・PSNR を確認する。"""
    r = res or case.convert(**kw)
    gt, gm = case.gt
    assert sorted(r.tiles) == sorted(gt), "出力タイルの集合が新UVのタイルと一致しない"
    assert sorted(r.coverage) == sorted(gt)
    for t in gt:
        assert r.tiles[t].shape == gt[t].shape and r.tiles[t].dtype == np.float32
        diff = (r.coverage[t] != gm[t]).sum()
        assert diff <= cov_mismatch * gm[t].sum() + 2, f"tile {t}: coverage differs in {diff} texels"
    pi = psnr(r.tiles, gt, gm, "interior")
    ps = psnr(r.tiles, gt, gm, "seam")
    assert pi >= interior_db, f"interior PSNR {pi:.1f} dB < {interior_db}"
    assert ps >= seam_db, f"seam PSNR {ps:.1f} dB < {seam_db}"
    return pi, ps


class TestConvertGeometry:
    @pytest.mark.parametrize("dst", ["A", "scaled", "rot90", "rot180", "rot270", "mirror", "flipv",
                                     "rot270_mirror", "B"])
    def test_shell_transforms_from_layout_A(self, dst):
        """移動・拡大・90°回転・180°回転・反転(左右/上下)・それらの組み合わせ。"""
        check_against_gt(get_case("A", dst, "smooth", 256))

    def test_identity_remap_reproduces_the_image(self):
        c = get_case("A", "A", "smooth", 256)
        r = c.convert()
        gt, gm = c.gt
        for t in gt:
            inner = _shrink(gm[t], 1)
            assert np.abs(r.tiles[t][inner] - c.old[t][inner]).max() < 5e-4

    def test_udim_move_changes_tile_set(self):
        c = get_case("A", "B", "smooth", 256)            # 1 タイル → 2 タイル
        assert sorted(c.convert().tiles) == [1001, 1002]
        c = get_case("B", "A", "smooth", 256)            # 2 タイル → 1 タイル
        r = c.convert()
        assert sorted(r.tiles) == [1001]
        check_against_gt(c)

    def test_shells_stay_on_the_right_tile(self):
        """各シェルのテクセルが、新UVが指すタイルに入っている(別タイルに漏れない)。"""
        c = get_case("A", "B", "smooth", 256)
        r = c.convert()
        sm = c.shells()
        # LAYOUT_B: シェル 3,4,5 は 1002、0,1,2 は 1001
        for t, expect in ((1001, {0, 1, 2}), (1002, {3, 4, 5})):
            got = set(np.unique(sm[t][r.coverage[t]]).tolist()) - {-1}
            assert got == expect, (t, got)

    def test_tile_boundary_uv_exactly_on_the_edge(self):
        """旧・新ともに、シェルがタイル境界(u=1.0)ちょうどで終わる/始まる配置。"""
        pl = plates(2, 4, 4, quads=True)
        old = {0: ("id", (0.5, 0.5), (0.5, 0.0)), 1: ("id", (0.5, 0.5), (1.0, 0.0))}   # [0.5,1] | [1,1.5]
        new = {0: ("id", (0.5, 0.5), (0.0, 0.5)), 1: ("id", (0.5, 0.5), (0.5, 0.5))}   # どちらも 1001
        new2 = {0: ("rot90", (1.0, 1.0), (1.0, 0.0)), 1: ("id", (0.5, 0.5), (0.0, 0.0))}  # シェル0 = 1002 全面
        for dst in (new, new2):
            A = snap_of(pl, apply_layout(pl, old))
            B = with_uv(A, apply_layout(pl, dst))
            c = Case.__new__(Case)                       # 板用の Case(球を使わない)
            c.res, c.udim, c.signal, c.kind, c.green_down, c.pad = 256, True, "smooth", "color", False, True
            c.mesh, c.A, c.B = pl, A, B
            c.decision = uvc.classify(A, B, "Body")
            c.spec = uvc.ChannelSpec("BaseColor", "raw", "color")
            c._old = c._gt = None
            c._conv = {}
            assert c.decision.method == "F"
            r = c.convert()
            keys = sorted(c.gt[0])
            assert sorted(r.tiles) == keys and not (set(r.tiles) & {1003, 1011, 1012}), keys
            check_against_gt(c, r, interior_db=60.0, seam_db=40.0)

    def test_shell_spanning_the_tile_edge_is_split_across_tiles(self):
        """UDIM 境界またぎ: 両方のタイルに正しく描かれ、stats に報告される。"""
        c = get_case("A", "straddle", "smooth", 256)
        r = c.convert()
        assert sorted(r.tiles) == [1001, 1002]
        assert r.stats["straddle_faces"] > 0 and "udim_straddle" in r.stats["warnings"]
        assert r.stats["straddle_face_ids"]
        check_against_gt(c, r, interior_db=60.0, seam_db=40.0)
        # 旧UV側の境界またぎ(旧タイルが2枚に分かれている)も正しく読む
        c2 = get_case("straddle", "A", "smooth", 256)
        r2 = c2.convert()
        assert "old_straddle" in r2.stats["warnings"]
        check_against_gt(c2, r2, interior_db=60.0, seam_db=40.0)

    def test_missing_source_tile_is_reported_not_raised(self):
        c = get_case("B", "A", "smooth", 256)                  # 旧は 1001 + 1002
        old = {1001: c.old[1001]}
        r = uvc.convert_channel(old, c.A, c.B, "Body", c.decision, c.spec, 256)
        assert r.stats["missing_tile_texels_total"] > 0 and "source_tile_missing" in r.stats["warnings"]
        full = c.convert()
        assert r.coverage[1001].sum() < full.coverage[1001].sum()      # 欠けた分は被覆から外れる

    def test_non_udim_wraps_uv_into_the_unit_square(self):
        c = get_case("A", "wrapped", "smooth", 256, udim=False)
        r = c.convert()
        assert sorted(r.tiles) == [1001]                       # タイル扱いしない
        check_against_gt(c, r)
        # 旧UVが 0..1 の外にある(非UDIMの折り返し)場合も、小数部で旧テクスチャを引く
        c2 = get_case("wrapped", "A", "smooth", 256, udim=False)
        check_against_gt(c2)
        # 同じ入力を UDIM として解釈すると、タイルが分かれ、範囲外のシェルは除外される
        r_udim = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, 256, udim=True)
        assert sorted(r_udim.tiles) == [1001, 1002, 1021, 1054]
        assert r_udim.stats["faces_out_of_udim_range"] > 0
        assert "out_of_udim_range" in r_udim.stats["warnings"]

    def test_udim_flag_falls_back_to_baseline_meta(self):
        c = get_case("A", "wrapped", "smooth", 256, udim=False)
        c.A.meta["udim"] = False
        try:
            r = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, 256)
            assert sorted(r.tiles) == [1001] and r.stats["udim"] is False
        finally:
            c.A.meta.pop("udim")

    def test_uv_overlap_is_counted_and_other_shells_stay_correct(self):
        c = get_case("A", "overlap", "smooth", 256)
        r = c.convert()
        assert r.stats["overlap_texels_total"] > 100 and "uv_overlap" in r.stats["warnings"]
        gt, gm = c.gt
        sm = c.shells()
        ok = {t: gm[t] & np.isin(sm[t], [2, 3, 4, 5]) for t in gt}
        ok = {t: _shrink(m, 2) for t, m in ok.items()}
        err = np.concatenate([(r.tiles[t][ok[t]] - gt[t][ok[t]]).ravel() for t in gt])
        assert 10 * np.log10(1 / max((err.astype(np.float64) ** 2).mean(), 1e-20)) > 70
        assert get_case("A", "B", "smooth", 256).convert().stats["overlap_texels_total"] == 0

    def test_out_res_forms(self):
        c = get_case("A", "B", "smooth", 256)
        r = c.convert()
        assert {t: a.shape for t, a in r.tiles.items()} == {1001: (256, 256, 3), 1002: (256, 256, 3)}
        r = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, (128, 64))   # (W, H)
        assert {a.shape for a in r.tiles.values()} == {(64, 128, 3)}
        r = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, {1001: 128, 1002: (64, 32)})
        assert r.tiles[1001].shape == (128, 128, 3) and r.tiles[1002].shape == (32, 64, 3)
        assert r.stats["res_fallback_tiles"] == []
        r = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, {1001: 128})
        assert r.stats["res_fallback_tiles"] == [1002] and r.tiles[1002].shape == (256, 256, 3)
        assert "res_fallback" in r.stats["warnings"]
        r = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, None)
        assert sorted(r.stats["res_fallback_tiles"]) == [1001, 1002]
        with pytest.raises(uvc.ChannelFormatError):
            uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, 0)

    def test_non_square_output_is_accurate(self):
        c = get_case("A", "rot90", "smooth", 256)
        W, H = 384, 192
        r = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, (W, H))
        gt, gm = bake(c.B, "Body", (W, H), f_smooth)
        assert r.tiles[1001].shape == (H, W, 3)
        assert psnr(r.tiles, gt, gm, "interior") > 60

    def test_resolution_change(self):
        c = get_case("A", "B", "smooth", 256)
        for out in (128, 512):
            r = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, out)
            gt, gm = bake(c.B, "Body", out, f_smooth)
            assert psnr(r.tiles, gt, gm, "interior") > 55, out

    def test_polygons_with_more_than_three_corners(self):
        """四角形メッシュでも、基準側の三角形分割を共有して正しく変換できる。"""
        pl = plates(3, 5, 5, quads=True)
        assert pl.counts.max() == 4
        old = {k: ("id", (0.3, 0.3), (0.03 + 0.33 * k, 0.1)) for k in range(3)}
        new = {0: ("rot90", (0.3, 0.3), (0.05, 0.6)), 1: ("mirror", (0.3, 0.3), (1.2, 0.1)),
               2: ("rot180", (0.35, 0.35), (0.5, 0.5))}
        A = snap_of(pl, apply_layout(pl, old))
        B = with_uv(A, apply_layout(pl, new))
        oldt, oldm = bake(A, "Body", 256, f_smooth)
        gt, gm = bake(B, "Body", 256, f_smooth)
        d = uvc.classify(A, B, "Body")
        r = uvc.convert_channel(pad_tiles(oldt, oldm), A, B, "Body", d,
                                uvc.ChannelSpec("BaseColor", "raw", "color"), 256)
        assert sorted(r.tiles) == sorted(gt)
        assert psnr(r.tiles, gt, gm, "interior") > 60

    def test_corner_order_does_not_matter(self):
        """現行側のコーナー列が巡回回転/逆順でも、同じ結果になる(頂点IDで対応付ける)。"""
        pl = plates(2, 4, 4, quads=True)
        A = snap_of(pl, apply_layout(pl, PLATE_LAYOUT))
        B = with_uv(A, apply_layout(pl, {0: ("rot90", (0.4, 0.4), (0.1, 0.5)),
                                         1: ("mirror", (0.4, 0.4), (1.1, 0.1))}))
        oldt, oldm = bake(A, "Body", 128, f_smooth)
        old = pad_tiles(oldt, oldm)
        spec = uvc.ChannelSpec("BaseColor", "raw", "color")
        d0 = uvc.classify(A, B, "Body")
        ref = uvc.convert_channel(old, A, B, "Body", d0, spec, 128)
        for variant, method in ((rotate_faces(B, 1), "F"), (rotate_faces(B, 3), "F"),
                                (reverse_faces(B), "F_FLIPPED")):
            d = uvc.classify(A, variant, "Body")
            assert d.method == method
            r = uvc.convert_channel(old, A, variant, "Body", d, spec, 128)
            assert sorted(r.tiles) == sorted(ref.tiles)
            for t in ref.tiles:
                np.testing.assert_array_equal(r.tiles[t], ref.tiles[t])
                np.testing.assert_array_equal(r.coverage[t], ref.coverage[t])


# ============================================================================
# テスト: convert_channel(色空間・チャンネル種別・パディング・除外面)
# ============================================================================

def _half_texel_case(res=64):
    """左半分が 0、右半分が 1 の1枚板を、半テクセルだけ U 方向へ動かして変換する構成。"""
    pl = plates(1, 1, 1, quads=True)
    d = 0.5 / res
    old = {0: ("id", (0.5, 0.5), (0.25, 0.25))}
    new = {0: ("id", (0.5, 0.5), (0.25 + d, 0.25))}
    A = snap_of(pl, apply_layout(pl, old))
    B = with_uv(A, apply_layout(pl, new))
    tile = np.zeros((res, res, 3), np.float32)
    tile[:, res // 2:] = 1.0
    return A, B, {1001: tile}, uvc.classify(A, B, "Body")


class TestColorspaceAndChannels:
    def test_srgb_interpolates_in_linear_light_and_raw_does_not(self):
        A, B, old, d = _half_texel_case()
        # 新テクセル x=32 は旧の x=31.5 を引くので、0 と 1 の中点になる
        raw = uvc.convert_channel(old, A, B, "Body", d, uvc.ChannelSpec("Roughness", "raw", "scalar"), 64)
        srgb = uvc.convert_channel(old, A, B, "Body", d, uvc.ChannelSpec("BaseColor", "srgb", "color"), 64)
        y = 32
        np.testing.assert_allclose(raw.tiles[1001][y, 32], 0.5, atol=1e-4)
        want = float(uvc.linear_to_srgb(np.float32(0.5)))             # 0.7354
        np.testing.assert_allclose(srgb.tiles[1001][y, 32], want, atol=1e-4)
        assert want - 0.5 > 0.2                                        # 取り違えると大きくずれる
        # 一様領域は色空間に依らず保存される
        np.testing.assert_allclose(srgb.tiles[1001][y, 48], 1.0, atol=1e-5)
        np.testing.assert_allclose(srgb.tiles[1001][y, 20], 0.0, atol=1e-5)

    def test_srgb_transfer_functions_roundtrip(self):
        x = np.linspace(0, 1, 101, dtype=np.float32)
        np.testing.assert_allclose(uvc.linear_to_srgb(uvc.srgb_to_linear(x)), x, atol=2e-6)
        assert uvc.srgb_to_linear(np.float32(0.5)) == pytest.approx(0.2140, abs=1e-3)

    def test_alpha_channel_is_not_linearised(self):
        A, B, old, d = _half_texel_case()
        rgba = np.concatenate([old[1001], old[1001][..., :1]], axis=2)       # A は 0/1 の階段
        r = uvc.convert_channel({1001: rgba}, A, B, "Body", d,
                                uvc.ChannelSpec("BaseColor", "srgb", "color"), 64)
        assert r.tiles[1001].shape[2] == 4
        np.testing.assert_allclose(r.tiles[1001][32, 32, 3], 0.5, atol=1e-4)   # アルファは生値
        assert r.tiles[1001][32, 32, 0] > 0.7

    def test_single_channel_and_2d_and_integer_inputs(self):
        A, B, old, d = _half_texel_case()
        spec = uvc.ChannelSpec("Roughness", "raw", "scalar")
        ref = uvc.convert_channel({1001: old[1001][..., :1]}, A, B, "Body", d, spec, 64)
        assert ref.tiles[1001].shape == (64, 64, 1)
        r2d = uvc.convert_channel({1001: old[1001][..., 0]}, A, B, "Body", d, spec, 64)
        np.testing.assert_array_equal(r2d.tiles[1001], ref.tiles[1001])
        u8 = (old[1001][..., :1] * 255).astype(np.uint8)
        r8 = uvc.convert_channel({1001: u8}, A, B, "Body", d, spec, 64)
        np.testing.assert_allclose(r8.tiles[1001], ref.tiles[1001], atol=1e-6)
        u16 = (old[1001][..., :1] * 65535).astype(np.uint16)
        r16 = uvc.convert_channel({1001: u16}, A, B, "Body", d, spec, 64)
        np.testing.assert_allclose(r16.tiles[1001], ref.tiles[1001], atol=1e-6)

    def test_bad_inputs_raise_typed_errors(self):
        A, B, old, d = _half_texel_case()
        spec = uvc.ChannelSpec("Roughness", "raw", "scalar")
        with pytest.raises(uvc.ChannelFormatError):
            uvc.convert_channel({}, A, B, "Body", d, spec, 64)
        with pytest.raises(uvc.ChannelFormatError):
            uvc.convert_channel({1001: np.zeros((4, 4, 3, 2))}, A, B, "Body", d, spec, 64)
        with pytest.raises(uvc.ChannelFormatError):
            uvc.convert_channel({1001: old[1001], 1002: old[1001][..., :1]}, A, B, "Body", d, spec, 64)
        with pytest.raises(uvc.ChannelFormatError):                    # 法線に 1 チャンネル
            uvc.convert_channel({1001: old[1001][..., :1]}, A, B, "Body", d,
                                uvc.ChannelSpec("Normal", "raw", "normal"), 64)
        with pytest.raises(uvc.TextureSetNotFound):
            uvc.convert_channel(old, A, B, "Nope", d, spec, 64)
        with pytest.raises(ValueError):
            uvc.ChannelSpec("X", "linear", "color")
        with pytest.raises(ValueError):
            uvc.ChannelSpec("X", "raw", "mask")
        with pytest.raises(ValueError):
            uvc.convert_channel(old, A, B, "Body", d, spec, 64, normal_mode="3d")

    def test_flipped_decision_gives_identical_colour_and_flags_normals(self):
        pl = plates(2, 4, 4, quads=True)
        A = snap_of(pl, apply_layout(pl, PLATE_LAYOUT))
        B = with_uv(A, apply_layout(pl, {0: ("rot90", (0.4, 0.4), (0.1, 0.5)),
                                         1: ("id", (0.4, 0.4), (0.5, 0.5))}))
        F = reverse_faces(B)
        oldt, oldm = bake(A, "Body", 64, f_smooth)
        old = pad_tiles(oldt, oldm)
        color = uvc.ChannelSpec("BaseColor", "raw", "color")
        r_f = uvc.convert_channel(old, A, B, "Body", uvc.classify(A, B, "Body"), color, 64)
        r_ff = uvc.convert_channel(old, A, F, "Body", uvc.classify(A, F, "Body"), color, 64)
        np.testing.assert_array_equal(r_f.tiles[1001], r_ff.tiles[1001])
        assert "normals_flipped_approx" not in r_ff.stats["warnings"]
        nrm = uvc.ChannelSpec("Normal", "raw", "normal")
        rn = uvc.convert_channel(old, A, F, "Body", uvc.classify(A, F, "Body"), nrm, 64)
        assert "normals_flipped_approx" in rn.stats["warnings"] and rn.stats["method"] == "F_FLIPPED"


class TestPadding:
    def test_source_padding_is_required_for_accurate_seams(self):
        padded = get_case("A", "B", "smooth", 256)
        bare = get_case("A", "B", "smooth", 256, pad=False)
        gt, gm = padded.gt
        r_pad, r_bare = padded.convert(), bare.convert()
        seam_pad = psnr(r_pad.tiles, gt, gm, "seam")
        seam_bare = psnr(r_bare.tiles, gt, gm, "seam")
        assert seam_pad >= 50, seam_pad                       # 試作の実測 69 dB(1024²)、256² で約 61 dB
        assert seam_bare < 30, seam_bare                      # パディング無しでは大きく劣化する(約 15 dB)
        assert psnr(r_bare.tiles, gt, gm, "interior") >= 70   # 内部は影響を受けない

    def test_output_is_dilated_by_dilate_px(self):
        c = get_case("A", "B", "smooth", 256)
        for px in (0, 4, 16):
            r = c.convert(dilate_px=px)
            for t, cov in r.coverage.items():
                painted = r.tiles[t].sum(axis=-1) != 0
                np.testing.assert_array_equal(painted, _dil(cov, px), err_msg=f"dilate_px={px} tile={t}")
        # 既定は 16 px
        r16, rdef = c.convert(dilate_px=16), c.convert()
        np.testing.assert_array_equal(r16.tiles[1001], rdef.tiles[1001])
        # coverage は膨張前のラスタライズ範囲で、dilate_px に依らない
        np.testing.assert_array_equal(c.convert(dilate_px=0).coverage[1001], rdef.coverage[1001])

    def test_dilation_does_not_touch_covered_texels(self):
        c = get_case("A", "B", "smooth", 256)
        r0, r16 = c.convert(dilate_px=0), c.convert(dilate_px=16)
        for t in r0.tiles:
            np.testing.assert_array_equal(r0.tiles[t][r0.coverage[t]], r16.tiles[t][r0.coverage[t]])

    def test_normal_map_background_is_flat_normal(self):
        c = get_case("A", "B", None, 256, kind="normal")
        r = c.convert(dilate_px=0)
        for t, cov in r.coverage.items():
            bg = r.tiles[t][~cov]
            np.testing.assert_allclose(bg, np.broadcast_to([0.5, 0.5, 1.0], bg.shape))


class TestExcludedFaces:
    @staticmethod
    def _plates_case(nan_in="current", nonmanifold=True):
        pl = plates(2, 3, 3, quads=True)
        old = {0: ("id", (0.4, 0.4), (0.05, 0.05)), 1: ("id", (0.4, 0.4), (0.55, 0.05))}
        new = {0: ("rot90", (0.4, 0.4), (0.05, 0.55)), 1: ("mirror", (0.4, 0.4), (0.55, 0.55))}
        uv_o, uv_n = apply_layout(pl, old), apply_layout(pl, new)
        offs = np.cumsum(pl.counts) - pl.counts
        vid = pl.vid.copy()
        f_nm, f_nan = 4, 12                                    # 板0 の中央 / 板1 の中の1枚
        if nonmanifold:
            vid[offs[f_nm] + 2] = vid[offs[f_nm]]
        (uv_n if nan_in == "current" else uv_o)[offs[f_nan]:offs[f_nan] + 4] = np.nan
        A = uvc.make_snapshot(pl.counts, vid, pl.pos, uv_o)
        B = uvc.make_snapshot(pl.counts, vid, pl.pos, uv_n, tri_face=A.tri_face, tri_corner=A.tri_corner)
        return pl, A, B, uv_n, offs, f_nm, f_nan

    @pytest.mark.parametrize("nan_in", ["current", "baseline"])
    def test_no_uv_and_non_manifold_faces_are_skipped_and_reported(self, nan_in):
        pl, A, B, uv_n, offs, f_nm, f_nan = self._plates_case(nan_in)
        assert uvc.classify(A, B, "default").method == "F"
        oldt, oldm = bake(snap_of(pl, apply_layout(pl, {0: ("id", (0.4, 0.4), (0.05, 0.05)),
                                                        1: ("id", (0.4, 0.4), (0.55, 0.05))})),
                          "Body", 128, f_smooth)
        old = pad_tiles(oldt, oldm)
        r = uvc.convert_channel(old, A, B, "default", uvc.classify(A, B, "default"),
                                uvc.ChannelSpec("BaseColor", "raw", "color"), 128, dilate_px=0)
        st = r.stats
        assert st["faces_without_uv"] == 1 and st["faces_non_manifold"] == 1
        assert st["faces_converted"] == pl.n_face - 2
        assert {"faces_without_uv", "non_manifold_faces"} <= set(st["warnings"])
        # 除外した面の中心テクセルは未被覆。他の面の中心は被覆される。
        cov = r.coverage[1001]
        for f in range(pl.n_face):
            if f == f_nan and nan_in == "current":
                continue                                        # 新UVが無いので位置を決められない
            c = uv_n[offs[f]:offs[f] + 4]
            if not np.isfinite(c).all():
                continue
            u, v = c.mean(axis=0)
            x, y = int(u * 128), int((1 - v) * 128)
            assert cov[y, x] == (f not in (f_nm, f_nan)), f"face {f}"

    def test_faces_without_source_texture_or_dropped(self):
        pl = plates(2, 3, 3, quads=True)
        old = {0: ("id", (0.4, 0.4), (0.05, 0.05)), 1: ("id", (0.4, 0.4), (0.55, 0.05))}
        new = {0: ("rot90", (0.4, 0.4), (0.05, 0.55)), 1: ("id", (0.4, 0.4), (0.55, 0.55))}
        uv_o, uv_n = apply_layout(pl, old), apply_layout(pl, new)
        half, n = pl.n_face // 2, pl.n_face
        base = uvc.make_snapshot(pl.counts, pl.vid, pl.pos, uv_o, ts_faces={"T": np.arange(half)})
        # 現行は TS に 3 面を足し、1 面を外した(マテリアル再割当)
        cur_ids = np.r_[np.arange(1, half), half, half + 1, half + 2]
        cur = uvc.make_snapshot(pl.counts, pl.vid, pl.pos, uv_n, ts_faces={"T": cur_ids},
                                tri_face=base.tri_face, tri_corner=base.tri_corner)
        d = uvc.classify(base, cur, "T")
        assert d.method == "F" and any("ts_faces_changed" in w for w in d.warnings)
        oldt, oldm = bake(base, "T", 64, f_smooth)
        r = uvc.convert_channel(pad_tiles(oldt, oldm), base, cur, "T", d,
                                uvc.ChannelSpec("BaseColor", "raw", "color"), 64)
        assert r.stats["faces_no_source"] == 3 and r.stats["faces_dropped"] == 1
        assert r.stats["faces_converted"] == half - 1 and "faces_no_source" in r.stats["warnings"]

    def test_empty_conversion_raises(self):
        pl = plates(1, 2, 2, quads=True)
        uv = apply_layout(pl, {0: ("id", (0.5, 0.5), (0.1, 0.1))})
        A = uvc.make_snapshot(pl.counts, pl.vid, pl.pos, uv)
        nan = uv.copy()
        nan[:] = np.nan
        B = uvc.make_snapshot(pl.counts, pl.vid, pl.pos, nan, tri_face=A.tri_face, tri_corner=A.tri_corner)
        with pytest.raises(uvc.ConversionStopped) as ei:
            uvc.convert_channel({1001: np.zeros((16, 16, 3), np.float32)}, A, B, "default",
                                uvc.classify(A, B, "default"),
                                uvc.ChannelSpec("Roughness", "raw", "scalar"), 16)
        assert ei.value.reasons == ["no_convertible_faces"]


class TestTextureSetsAndMeshes:
    def test_two_meshes_two_texture_sets_convert_independently(self):
        _, base = multi_snapshot()
        sph, pl = cube_sphere(4), plates(2, 3, 3, quads=True)
        new_sph = apply_layout(sph, LAYOUT_B)
        new_pl = apply_layout(pl, {0: ("rot90", (0.4, 0.4), (0.1, 1.1)), 1: ("mirror", (0.4, 0.4), (1.1, 1.1))})
        cur = with_uv(base, np.concatenate([new_sph, new_pl]))
        assert uvc.classify(base, cur, "Body").method == "F"
        assert uvc.classify(base, cur, "Plate").method == "F"
        for ts, want_tiles in (("Body", {1001, 1002}), ("Plate", {1011, 1012})):
            oldt, oldm = bake(base, ts, 128, f_smooth)
            gt, gm = bake(cur, ts, 128, f_smooth)
            r = uvc.convert_channel(pad_tiles(oldt, oldm), base, cur, ts, uvc.classify(base, cur, ts),
                                    uvc.ChannelSpec("BaseColor", "raw", "color"), 128)
            assert set(r.tiles) == set(gt) == want_tiles, ts      # 他方の TS のタイルが混ざらない
            assert psnr(r.tiles, gt, gm, "interior") > 60, ts
            assert r.stats["faces_total_ts"] == len(base.faces_of(ts))


# ============================================================================
# テスト: 法線マップ
# ============================================================================

def normal_errors(case, result):
    """変換結果と、新UVで直接焼いた法線(GT)の角度誤差 [deg](内部テクセル)。"""
    gt, gm = case.gt
    e = []
    for t in gt:
        inner = _shrink(gm[t], 2)
        e.append(angle_err_deg(result.tiles[t][inner], gt[t][inner]))
    return np.concatenate(e)


class TestNormals:
    @pytest.mark.parametrize("dst,off_min", [("rot90", 10.0), ("rot180", 15.0), ("rot270", 10.0),
                                             ("mirror", 8.0), ("flipv", 8.0),
                                             ("rot270_mirror", 10.0), ("B", 8.0)])
    def test_uv_jacobian_correction_vs_none(self, dst, off_min):
        """補正あり: 平均 2° 以下(試作: 0.05〜1.7°)。補正なし: 回転・反転で大きくずれる。"""
        c = get_case("A", dst, None, 256, kind="normal")
        e_uv = normal_errors(c, c.convert(normal_mode="uv"))
        e_off = normal_errors(c, c.convert(normal_mode="off"))
        assert e_uv.mean() <= 2.0, e_uv.mean()
        assert np.percentile(e_uv, 99) <= 8.0
        assert e_off.mean() >= off_min, e_off.mean()
        assert e_off.mean() > 5 * e_uv.mean()

    def test_translation_and_uniform_scale_need_no_correction(self):
        c = get_case("A", "scaled", None, 256, kind="normal")
        e_uv = normal_errors(c, c.convert(normal_mode="uv"))
        e_off = normal_errors(c, c.convert(normal_mode="off"))
        assert e_uv.mean() < 0.2 and e_off.mean() < 0.2

    def test_reverse_direction_and_chained_layouts(self):
        c = get_case("B", "A", None, 256, kind="normal")
        assert normal_errors(c, c.convert()).mean() <= 2.0

    def test_output_normals_are_unit_length(self):
        c = get_case("A", "rot90", None, 256, kind="normal")
        r = c.convert()
        for t, cov in r.coverage.items():
            n = r.tiles[t][cov] * 2 - 1
            np.testing.assert_allclose(np.linalg.norm(n, axis=1), 1.0, atol=1e-4)

    def test_green_down_convention(self):
        """Normal_DirectX(Y 下向き)のマップは normal_green_down=True で正しく補正される。"""
        c = get_case("A", "rot90", None, 256, kind="normal", green_down=True)
        e_ok = normal_errors(c, c.convert())                       # spec.normal_green_down=True
        assert e_ok.mean() <= 2.0
        wrong = uvc.ChannelSpec("Normal", "raw", "normal", normal_green_down=False)
        r = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, wrong, 256)
        assert normal_errors(c, r).mean() > 5.0                    # 規約を取り違えると補正が逆回りになる

    def test_extra_channel_is_passed_through(self):
        """RGBA の法線マップ: 4つ目のチャンネル(高さ等)は補正せず補間だけする。"""
        c = get_case("A", "rot90", None, 128, kind="normal")
        extra = {t: np.concatenate([a, np.full(a.shape[:2] + (1,), 0.25, np.float32)], axis=2)
                 for t, a in c.old.items()}
        r = uvc.convert_channel(extra, c.A, c.B, "Body", c.decision, c.spec, 128)
        for t, cov in r.coverage.items():
            assert r.tiles[t].shape[2] == 4
            np.testing.assert_allclose(r.tiles[t][cov][:, 3], 0.25, atol=1e-5)


# ============================================================================
# テスト: 精度回帰 / 決定性 / 原本からの変換
# ============================================================================

class TestAccuracyRegression:
    @pytest.mark.parametrize("dst", ["B", "rot90", "mirror", "rot270_mirror"])
    def test_smooth_signal_interior_psnr_at_least_90db(self, dst):
        """設計書 付録A / 契約 D-5.5: 方式F・平滑信号の内部PSNR >= 90 dB(1024²、パディング有)。"""
        c = get_case("A", dst, "smooth", 1024)
        r = c.convert()
        gt, gm = c.gt
        pi = psnr(r.tiles, gt, gm, "interior")
        ps = psnr(r.tiles, gt, gm, "seam")
        assert pi >= 90.0, f"{dst}: interior {pi:.1f} dB"
        assert ps >= 55.0, f"{dst}: seam {ps:.1f} dB"             # 試作: 69 dB

    def test_detail_signal_psnr_floor(self):
        """細部(チェッカー x 擬似文字)は再サンプリングでぼける。試作 22 dB に対し 18 dB を下限とする。"""
        c = get_case("A", "B", "detail", 512)
        r = c.convert()
        gt, gm = c.gt
        assert psnr(r.tiles, gt, gm, "interior") >= 18.0

    def test_higher_source_resolution_helps_detail(self):
        """元解像度を上げると細部の再現が良くなる(試作: 約 +3 dB / 倍)。"""
        lo = get_case("A", "B", "detail", 256)
        hi = get_case("A", "B", "detail", 512)
        gt, gm = lo.gt
        r_hi = uvc.convert_channel(hi.old, hi.A, hi.B, "Body", hi.decision, hi.spec, 256)
        assert psnr(r_hi.tiles, gt, gm, "interior") > psnr(lo.convert().tiles, gt, gm, "interior") + 1.0

    def test_same_input_gives_bit_identical_output(self):
        c = get_case("A", "B", "detail", 256)
        r1 = c.convert()
        r2 = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, 256, udim=True)
        assert sorted(r1.tiles) == sorted(r2.tiles)
        for t in r1.tiles:
            np.testing.assert_array_equal(r1.tiles[t], r2.tiles[t])
            np.testing.assert_array_equal(r1.coverage[t], r2.coverage[t])

    def test_conversion_does_not_modify_its_inputs(self):
        c = get_case("A", "B", "smooth", 128)
        before = {t: a.copy() for t, a in c.old.items()}
        snap_before = (c.A.corner_uv.copy(), c.B.corner_uv.copy())
        uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, c.spec, 128)
        for t in before:
            np.testing.assert_array_equal(c.old[t], before[t])
        np.testing.assert_array_equal(c.A.corner_uv, snap_before[0])
        np.testing.assert_array_equal(c.B.corner_uv, snap_before[1])

    @pytest.mark.parametrize("target,min_gain_db", [("A", 20.0), ("mirror", 3.0)])
    def test_converting_from_the_original_does_not_accumulate_loss(self, target, min_gain_db):
        """A→B→C(連鎖)は A→C(原本から)より大きく劣化する。原本から変換すれば経由した世代に依存しない。

        target="A" は UV 編集を元に戻した場合: 原本から変換すれば(ほぼ)完全に元通りになる。
        """
        res = 512
        m = cube_sphere(10)
        A = snap_of(m, apply_layout(m, LAYOUT_A))
        B = with_uv(A, apply_layout(m, LAYOUT_B))
        C = with_uv(A, apply_layout(m, LAYOUTS[target]))
        spec = uvc.ChannelSpec("BaseColor", "srgb", "color")

        def q8(tiles):                                       # 8bit 保存を模す
            return {t: np.round(a * 255) / 255 for t, a in tiles.items()}

        oldt, oldm = bake(A, "Body", res, f_detail)
        orig = q8(pad_tiles(oldt, oldm))
        gt_c, gm_c = bake(C, "Body", res, f_detail)
        # 原本から C へ(B を一度も経由しない)
        direct = uvc.convert_channel(orig, A, C, "Body", uvc.classify(A, C, "Body"), spec, res)
        # 原本 → B(保存) → C
        r_b = uvc.convert_channel(orig, A, B, "Body", uvc.classify(A, B, "Body"), spec, res)
        chained = uvc.convert_channel(q8(r_b.tiles), B, C, "Body", uvc.classify(B, C, "Body"), spec, res)
        p_direct = psnr(q8(direct.tiles), gt_c, gm_c, "interior")
        p_chain = psnr(q8(chained.tiles), gt_c, gm_c, "interior")
        assert p_direct > p_chain + min_gain_db, (p_direct, p_chain)
        if target == "A":
            assert p_direct > 50.0                           # 元のレイアウトへ戻せば、ほぼ無損失
        # 原本から変換し直しても結果は同じ(世代を重ねても劣化しない)
        again = uvc.convert_channel(orig, A, C, "Body", uvc.classify(A, C, "Body"), spec, res)
        for t in direct.tiles:
            np.testing.assert_array_equal(direct.tiles[t], again.tiles[t])


# ============================================================================
# テスト: probe_verify(契約 §3.1)
# ============================================================================

PROBE_RES = 1024            # s = 4。細部ノイズの偽陽性を調べるには s >= 4 が要る(PROBE_CALIBRATION.md)
NOISE2 = 2 * 6.67 / PROBE_RES     # 3D空間のノイズセル幅 = 約 2 テクセル(1024²で、球のシェル 0.3 タイル相当)

PROBE_SIGNALS = {"smooth": "smooth", "detail": "detail", "noise": NOISE2}
POSITIVE_LAYOUTS = [("A", "B"), ("B", "A"), ("A", "rot90"), ("A", "mirror"),
                    ("B", "rot270_mirror"), ("A", "scaled")]


def probe_case(src, dst, sig, **kw):
    return get_case(src, dst, PROBE_SIGNALS[sig], PROBE_RES, **kw)


class TestProbeNoFalsePositives:
    """正しい変換は、細部・ノイズ・シーム周辺・UDIM移動・法線のいずれでも pass(偽陽性ゼロ)。"""

    @pytest.mark.parametrize("sig", ["smooth", "detail", "noise"])
    @pytest.mark.parametrize("src,dst", POSITIVE_LAYOUTS)
    def test_correct_colour_conversion_passes(self, sig, src, dst):
        c = probe_case(src, dst, sig)
        rep = c.probe(c.convert().tiles)
        assert rep.status == "pass", (rep.n_bad, rep.n_valid, rep.max_err, rep.bad_points[:2])
        assert rep.n_bad == 0 and rep.n_valid >= 300
        # 余裕: tau を 25% 下げても pass する(最大誤差は単独の1点なので尺度にしない)
        assert c.probe(c.convert().tiles, tau=0.075).status == "pass"

    @pytest.mark.parametrize("sig", ["detail", "noise"])
    def test_without_source_padding_still_passes(self, sig):
        """元テクスチャにパディングが無くシームが荒れても、島の境界は除外されるので偽陽性にならない。"""
        c = probe_case("A", "B", sig, pad=False)
        rep = c.probe(c.convert().tiles)
        assert rep.status == "pass" and rep.n_bad == 0
        assert rep.excluded.get("island_border", 0) > 0

    @pytest.mark.parametrize("sig", ["detail", "noise"])
    @pytest.mark.parametrize("src,dst", [("A", "B"), ("A", "rot90"), ("B", "A")])
    def test_srgb_conversion_passes(self, sig, src, dst):
        """sRGB カラーは線形光で補間して変換する。プローブも窓平均を線形光で取るので偽陽性にならない。"""
        c = probe_case(src, dst, sig, colorspace="srgb")
        rep = c.probe(c.convert().tiles, n=2000)       # 点数を増やして割合を安定させる
        assert rep.status == "pass" and rep.params["linear_light_average"] is True
        assert rep.n_bad / rep.n_valid <= 0.01                            # しきい値 2% の半分以下

    def test_srgb_average_matters_for_high_contrast_content(self):
        """符号化値のまま平均すると(spec を raw として検証すると)、正しい sRGB 変換でも誤検出する。"""
        c = probe_case("A", "B", "noise", colorspace="srgb")
        r = c.convert()
        as_raw = uvc.ChannelSpec("BaseColor", "raw", "color")
        rep = uvc.probe_verify(c.old, r.tiles, c.A, c.B, "Body", as_raw)
        assert rep.status == "fail" and rep.params["linear_light_average"] is False

    def test_8bit_quantised_textures_pass(self):
        c = probe_case("A", "B", "detail")
        q = {t: np.round(a * 255) / 255 for t, a in c.old.items()}
        r = uvc.convert_channel(q, c.A, c.B, "Body", c.decision, c.spec, PROBE_RES)
        rep = uvc.probe_verify(q, {t: np.round(a * 255) / 255 for t, a in r.tiles.items()},
                               c.A, c.B, "Body", c.spec)
        assert rep.status == "pass" and rep.n_bad == 0

    @pytest.mark.parametrize("dst", ["B", "rot90", "rot180", "rot270_mirror"])
    @pytest.mark.parametrize("green_down", [False, True])
    def test_corrected_normal_conversion_passes(self, dst, green_down):
        c = get_case("A", dst, None, 512, kind="normal", green_down=green_down)
        rep = c.probe(c.convert().tiles)
        assert rep.status == "pass" and rep.n_bad == 0
        assert c.probe(c.convert().tiles, max_angle_deg=6.0).status == "pass"   # 余裕: 角度しきい値を 25% 下げても pass

    def test_uv_overlap_and_straddle_and_wrap_are_excluded_not_failed(self):
        c = get_case("A", "overlap", PROBE_SIGNALS["detail"], PROBE_RES)
        rep = c.probe(c.convert().tiles)
        assert rep.status == "pass" and rep.excluded.get("uv_overlap", 0) > 0
        c = get_case("A", "straddle", PROBE_SIGNALS["detail"], PROBE_RES)
        rep = c.probe(c.convert().tiles)
        assert rep.status == "pass" and rep.excluded.get("tile_straddle", 0) > 0
        c = get_case("A", "wrapped", PROBE_SIGNALS["smooth"], 512)           # UDIM として解釈 → 範囲外あり
        rep = c.probe(c.convert().tiles)
        assert rep.status == "pass" and rep.excluded.get("udim_out_of_range", 0) > 0
        c = get_case("A", "wrapped", PROBE_SIGNALS["smooth"], 512, udim=False)
        rep = c.probe(c.convert().tiles)
        assert rep.status == "pass" and "udim_out_of_range" not in rep.excluded

    def test_defective_faces_do_not_trigger_alarms(self):
        pl, A, B, uv_n, offs, f_nm, f_nan = TestExcludedFaces._plates_case("current")
        oldt, oldm = bake(snap_of(pl, apply_layout(pl, {0: ("id", (0.4, 0.4), (0.05, 0.05)),
                                                        1: ("id", (0.4, 0.4), (0.55, 0.05))})),
                          "Body", 512, noise_signal(0.01))
        old = pad_tiles(oldt, oldm)
        spec = uvc.ChannelSpec("BaseColor", "raw", "color")
        d = uvc.classify(A, B, "default")
        r = uvc.convert_channel(old, A, B, "default", d, spec, 512)
        rep = uvc.probe_verify(old, r.tiles, A, B, "default", spec, n=1024)
        assert rep.status == "pass" and rep.n_bad == 0

    def test_a_single_hard_edge_texture_passes(self):
        """白黒の鋭いエッジ(半テクセルずれる)を含む変換でも、エッジ近傍の点で偽陽性にならない。"""
        A, B, old, d = _half_texel_case(256)
        old[1001][:, :] = 0
        old[1001][:, 128:] = 1.0
        spec = uvc.ChannelSpec("Roughness", "raw", "scalar")
        r = uvc.convert_channel(old, A, B, "Body", d, spec, 256)
        rep = uvc.probe_verify(old, r.tiles, A, B, "Body", spec, n=512)
        assert rep.status == "pass", (rep.n_bad, rep.n_valid, rep.max_err)


class TestProbeDetectsDefects:
    """故意の不具合は fail: U方向のずれ(>= 2%)、1シェルのみ反転、タイル番号の取り違え、領域の欠落。"""

    @pytest.mark.parametrize("sig", ["detail", "noise"])
    @pytest.mark.parametrize("frac", [0.02, 0.05])
    def test_u_shift_of_two_percent_or_more(self, sig, frac):
        c = probe_case("A", "B", sig)
        rep = c.probe(inject_shift_u(c.convert().tiles, frac))
        assert rep.status == "fail" and rep.n_bad / rep.n_valid > 0.1

    def test_u_shift_on_a_smooth_texture_needs_three_percent(self):
        """平滑なテクスチャはずらしても画素がほとんど変わらないため、検出は 3% 以上(2% は pass し得る)。"""
        c = probe_case("A", "B", "smooth")
        r = c.convert()
        assert c.probe(inject_shift_u(r.tiles, 0.03)).status == "fail"
        assert c.probe(inject_shift_u(r.tiles, 0.05)).status == "fail"

    @pytest.mark.parametrize("sig", ["smooth", "detail", "noise"])
    @pytest.mark.parametrize("shell", [0, 1, 2, 3, 4, 5])
    def test_single_flipped_shell(self, sig, shell):
        c = probe_case("A", "B", sig)
        r = c.convert()
        rep = c.probe(inject_flip_shell(r.tiles, c.shells(), shell))
        assert rep.status == "fail", (shell, rep.n_bad, rep.n_valid)
        assert rep.n_bad / rep.n_valid > 0.05

    @pytest.mark.parametrize("sig", ["smooth", "detail", "noise"])
    def test_tile_numbers_swapped_or_wrong(self, sig):
        c = probe_case("A", "B", sig)
        r = c.convert()
        assert c.probe(inject_swap_tiles(r.tiles, 1001, 1002)).status == "fail"
        rep = c.probe(inject_relabel_tile(r.tiles, 1002, 1003))          # 1002 を 1003 と取り違え
        assert rep.status == "fail" and any(b["missing_new_tile"] for b in rep.bad_points)
        rep = c.probe({1001: r.tiles[1001]})                              # タイル 1002 の欠落
        assert rep.status == "fail" and any(b["missing_new_tile"] for b in rep.bad_points)

    @pytest.mark.parametrize("sig", ["smooth", "detail", "noise"])
    @pytest.mark.parametrize("frac", [0.03, 0.10])
    def test_dropout_regions(self, sig, frac):
        c = probe_case("A", "B", sig)
        r = c.convert()
        rep = c.probe(inject_dropout(r.tiles, r.coverage, frac))
        assert rep.status == "fail", (frac, rep.n_bad, rep.n_valid)

    def test_missing_whole_output(self):
        c = probe_case("A", "B", "detail")
        rep = c.probe({1001: np.zeros_like(c.old[1001])})                 # 全面が黒
        assert rep.status == "fail" and rep.n_bad / rep.n_valid > 0.5

    @pytest.mark.parametrize("fault", ["shift", "flip", "swap", "dropout"])
    def test_normal_map_defects(self, fault):
        c = get_case("A", "B", None, 512, kind="normal")
        r = c.convert()
        tiles = {"shift": lambda: inject_shift_u(r.tiles, 0.02),
                 "flip": lambda: inject_flip_shell(r.tiles, c.shells(), 2),
                 "swap": lambda: inject_swap_tiles(r.tiles, 1001, 1002),
                 "dropout": lambda: inject_dropout(r.tiles, r.coverage, 0.05)}[fault]()
        assert c.probe(tiles).status == "fail"

    def test_uncorrected_normals_are_flagged_unless_correction_is_off(self):
        c = get_case("A", "rot90", None, 512, kind="normal")
        off = c.convert(normal_mode="off")
        assert c.probe(off.tiles).status == "fail"                       # 接空間補正が必要なのに無い
        assert c.probe(off.tiles, normal_mode="off").status == "pass"    # 補正なしを承知で変換した場合
        assert c.probe(c.convert().tiles, normal_mode="off").status == "fail"

    def test_green_channel_convention_mismatch_is_flagged(self):
        c = get_case("A", "rot90", None, 512, kind="normal", green_down=True)
        wrong = uvc.ChannelSpec("Normal", "raw", "normal", normal_green_down=False)
        r = uvc.convert_channel(c.old, c.A, c.B, "Body", c.decision, wrong, 512)
        assert c.probe(r.tiles).status == "fail"


class TestProbeBehaviour:
    def test_report_fields_and_accounting(self):
        c = probe_case("A", "B", "detail")
        rep = c.probe(c.convert().tiles, n=300)
        assert rep.n_total == 300
        assert rep.n_valid + sum(rep.excluded.values()) == rep.n_total
        assert rep.status == "pass" and rep.bad_points == [] and 0 <= rep.mean_err <= rep.max_err
        d = rep.to_dict()
        assert set(d) >= {"status", "n_total", "n_valid", "n_bad", "max_err", "mean_err", "bad_points"}
        json.dumps(d)                                                      # JSON にそのまま書ける
        assert d["params"]["footprint"] == uvc.PROBE_FOOTPRINT and d["params"]["tau"] == uvc.PROBE_TAU

    def test_bad_points_describe_the_failure(self):
        c = probe_case("A", "B", "detail")
        rep = c.probe(inject_shift_u(c.convert().tiles, 0.05))
        assert 0 < len(rep.bad_points) <= 32
        b = rep.bad_points[0]
        assert {"face", "bary", "old_uv", "new_uv", "old_tile", "new_tile", "err"} <= set(b)
        assert b["err"] == max(x["err"] for x in rep.bad_points)           # 誤差の大きい順
        json.dumps(rep.to_dict())

    def test_seed_determinism(self):
        c = probe_case("A", "B", "noise")
        tiles = inject_shift_u(c.convert().tiles, 0.02)
        a, b = c.probe(tiles, seed=3), c.probe(tiles, seed=3)
        assert a.to_dict() == b.to_dict()
        assert c.probe(tiles, seed=4).to_dict() != a.to_dict()
        assert a.params["seed"] == 3
        assert c.probe(tiles).to_dict() == c.probe(tiles, seed=0).to_dict()   # 既定シードは固定

    def test_few_valid_points_is_inconclusive(self):
        c = probe_case("A", "B", "detail")
        rep = c.probe(c.convert().tiles, n=40)
        assert rep.status == "inconclusive" and rep.n_valid < 64
        assert any("n_valid" in s for s in rep.notes)

    def test_fail_takes_precedence_over_inconclusive(self):
        c = probe_case("A", "B", "detail")
        rep = c.probe(inject_shift_u(c.convert().tiles, 0.05), n=40)
        assert rep.n_valid < 64 and rep.status == "fail"

    def test_nothing_to_check_is_inconclusive(self):
        pl = plates(1, 2, 2, quads=True)
        uv = apply_layout(pl, {0: ("id", (0.5, 0.5), (0.1, 0.1))})
        A = uvc.make_snapshot(pl.counts, pl.vid, pl.pos, uv)
        B = uvc.make_snapshot(pl.counts, pl.vid, pl.pos, np.full_like(uv, np.nan),
                              tri_face=A.tri_face, tri_corner=A.tri_corner)
        spec = uvc.ChannelSpec("Roughness", "raw", "scalar")
        z = {1001: np.zeros((64, 64, 1), np.float32)}
        rep = uvc.probe_verify(z, z, A, B, "default", spec)
        assert rep.status == "inconclusive" and rep.n_valid == 0

    def test_missing_old_tile_points_are_excluded(self):
        c = probe_case("B", "A", "detail")
        r = c.convert()
        rep = uvc.probe_verify({1001: c.old[1001]}, r.tiles, c.A, c.B, "Body", c.spec)
        assert rep.excluded.get("old_tile_missing", 0) > 0

    def test_lattice_footprint_is_available_and_documented_to_false_alarm(self):
        """契約文言どおりの格子固定の縮小は、正しい変換でも細部テクスチャで誤検出する(理由は QUESTIONS.md Q1)。"""
        c = probe_case("A", "B", "detail")
        r = c.convert()
        assert c.probe(r.tiles).status == "pass"
        lattice = c.probe(r.tiles, footprint="lattice")
        assert lattice.status == "fail" and lattice.params["footprint"] == "lattice"
        # 平滑なテクスチャなら格子固定でも問題ない
        cs = probe_case("A", "B", "smooth")
        assert cs.probe(cs.convert().tiles, footprint="lattice").status == "pass"
        with pytest.raises(ValueError):
            c.probe(r.tiles, footprint="nearest")

    def test_low_resolution_adds_a_note_when_alarms_are_raised(self):
        c = get_case("A", "B", 6.67 / 256, 256)                          # 256²: s = 1。テクセル単位のノイズ
        rep = c.probe(c.convert().tiles)
        assert rep.n_bad > 0 and any("low_resolution_probe" in s for s in rep.notes)

    def test_thresholds_are_overridable_and_documented_defaults(self):
        # 契約の既定は tau=0.08。0.10 へ調整した根拠は docs/uvremap/PROBE_CALIBRATION.md
        assert (uvc.PROBE_TAU, uvc.PROBE_NORMAL_DEG, uvc.PROBE_MAX_BAD_FRACTION, uvc.PROBE_MIN_VALID) \
            == (0.10, 8.0, 0.02, 64)
        c = probe_case("A", "B", "detail")
        shifted = inject_shift_u(c.convert().tiles, 0.02)
        assert c.probe(shifted, tau=1.0).status == "pass"                  # 緩めれば通る(= 値を使っている)
        assert c.probe(shifted, max_bad_fraction=0.99).status == "pass"

    def test_wrong_texture_set_and_bad_arrays(self):
        c = probe_case("A", "B", "smooth")
        with pytest.raises(uvc.TextureSetNotFound):
            uvc.probe_verify(c.old, c.old, c.A, c.B, "Nope", c.spec)
        with pytest.raises(uvc.ChannelFormatError):
            uvc.probe_verify({}, c.old, c.A, c.B, "Body", c.spec)


class TestSlow:
    """2048² のテスト(UVREMAP_SLOW=1 で実行)。実運用に近い解像度(s = 8)での精度と所要時間。"""

    @slow
    def test_2048_smooth_psnr_and_probe_on_texel_level_noise(self):
        res = 2048
        c = get_case("A", "B", "smooth", res)
        t0 = time.time()
        r = c.convert()
        dt = time.time() - t0
        gt, gm = c.gt
        assert psnr(r.tiles, gt, gm, "interior") >= 100.0          # 試作: 102.6 dB
        assert psnr(r.tiles, gt, gm, "seam") >= 60.0
        assert dt < 60.0, f"convert took {dt:.1f}s for 2 tiles at 2048"
        cn = Case(LAYOUT_A, LAYOUT_B, 6.67 / res, res)               # テクセル単位の白色ノイズ
        rn = cn.convert()
        assert cn.probe(rn.tiles).status == "pass"                   # s = 8 ならテクセル単位でも偽陽性なし
        assert cn.probe(inject_shift_u(rn.tiles, 0.02)).status == "fail"

    @slow
    def test_2048_four_udim_tiles(self):
        wide = {s: ("id", (0.45, 0.45), (0.03 + 1.0 * (s % 4) + 0.5 * (s // 4), 0.05 + 0.5 * (s // 4)))
                for s in range(6)}
        LAYOUTS_LOCAL = {"A": LAYOUT_A, "wide": wide}
        c = Case(LAYOUTS_LOCAL["A"], LAYOUTS_LOCAL["wide"], "smooth", 2048)
        r = c.convert()
        assert len(r.tiles) >= 4
        check_against_gt(c, r, interior_db=95.0, seam_db=55.0)


# ============================================================================
# 較正: プローブのしきい値の測定表(docs/uvremap/PROBE_CALIBRATION.md の元データ)
#   python sp_maya_livesync/tests/test_uvremap_core.py --calibrate [--json out.json]
# ============================================================================

CAL_TAUS = (0.04, 0.06, 0.08, 0.10, 0.12, 0.16)
CAL_ANGLES = (2.0, 4.0, 6.0, 8.0, 10.0, 12.0)
CAL_N = 2000


def _bad_fractions(case, tiles, taus, **kw):
    """しきい値ごとの n_bad / n_valid と、既定しきい値での報告(最大/平均誤差)。"""
    is_normal = case.kind == "normal"
    out = {}
    for t in (CAL_ANGLES if is_normal else taus):
        rep = case.probe(tiles, n=CAL_N, **({"max_angle_deg": t} if is_normal else {"tau": t}), **kw)
        out[t] = rep.n_bad / max(rep.n_valid, 1)
    base = case.probe(tiles, n=CAL_N, **kw)
    return {"bad": out, "n_valid": base.n_valid, "max_err": base.max_err, "mean_err": base.mean_err,
            "status": base.status, "excluded": base.excluded}


def calibration_data():
    """正例(正しい変換)と負例(故意の不具合)を測る。重いので通常のテスト実行には含めない。"""
    pos, neg = [], []
    for sig in ("smooth", "detail", "noise"):
        for cs in (("raw",) if sig == "smooth" else ("raw", "srgb")):
            for src, dst in POSITIVE_LAYOUTS:
                c = probe_case(src, dst, sig, colorspace=cs)
                m = _bad_fractions(c, c.convert().tiles, CAL_TAUS)
                pos.append({"name": f"{sig}/{cs} {src}->{dst}", "kind": "color", **m})
        c = probe_case("A", "B", sig, pad=False)
        pos.append({"name": f"{sig}/raw A->B (unpadded source)", "kind": "color",
                    **_bad_fractions(c, c.convert().tiles, CAL_TAUS)})
    for dst in ("B", "rot90", "rot180", "rot270_mirror"):
        for gd in (False, True):
            c = get_case("A", dst, None, 512, kind="normal", green_down=gd)
            pos.append({"name": f"normal{'/DX' if gd else '/GL'} A->{dst}", "kind": "normal",
                        **_bad_fractions(c, c.convert().tiles, CAL_TAUS)})
    for sig in ("smooth", "detail", "noise"):
        c = probe_case("A", "B", sig)
        r = c.convert()
        sm = c.shells()
        faults = [(f"shift_u {f:.0%}", inject_shift_u(r.tiles, f)) for f in (0.01, 0.02, 0.03, 0.05)]
        faults += [(f"flip shell {k}", inject_flip_shell(r.tiles, sm, k)) for k in range(6)]
        faults += [("tiles swapped", inject_swap_tiles(r.tiles, 1001, 1002)),
                   ("tile 1002 relabelled 1003", inject_relabel_tile(r.tiles, 1002, 1003)),
                   ("tile 1002 missing", {1001: r.tiles[1001]})]
        faults += [(f"dropout {f:.0%}", inject_dropout(r.tiles, r.coverage, f)) for f in (0.02, 0.03, 0.05, 0.10)]
        for name, tiles in faults:
            # 1% のずらしは要求外。平滑なテクスチャの 2% ずらしは画素がほぼ変わらないため要求外(3% 以上が対象)
            req = not (name == "shift_u 1%" or (sig == "smooth" and name == "shift_u 2%"))
            neg.append({"name": f"{sig} {name}", "kind": "color", "required": req,
                        **_bad_fractions(c, tiles, CAL_TAUS)})
    c = get_case("A", "B", None, 512, kind="normal")
    r = c.convert()
    sm = c.shells()
    faults = [(f"shift_u {f:.0%}", inject_shift_u(r.tiles, f)) for f in (0.01, 0.02, 0.05)]
    faults += [(f"flip shell {k}", inject_flip_shell(r.tiles, sm, k)) for k in range(6)]
    faults += [("tiles swapped", inject_swap_tiles(r.tiles, 1001, 1002)),
               ("dropout 5%", inject_dropout(r.tiles, r.coverage, 0.05))]
    for name, tiles in faults:
        neg.append({"name": f"normal {name}", "kind": "normal", "required": name != "shift_u 1%",
                    **_bad_fractions(c, tiles, CAL_TAUS)})
    c = get_case("A", "rot90", None, 512, kind="normal")
    neg.append({"name": "normal uncorrected (normal_mode=off) rot90", "kind": "normal", "required": True,
                **_bad_fractions(c, c.convert(normal_mode="off").tiles, CAL_TAUS)})
    return pos, neg


def seed_sweep(n_seeds=40, n=512, taus=(0.08, 0.10)):
    """既定の n=512 で、シード n_seeds 通りの偽陽性率(正例)と検出率(負例)を tau ごとに調べる。"""
    pos_rows, neg_rows = [], []
    for name, sig, cs, src, dst in (("noise/srgb A->B", "noise", "srgb", "A", "B"),
                                    ("noise/srgb A->scaled", "noise", "srgb", "A", "scaled"),
                                    ("noise/srgb B->rot270_mirror", "noise", "srgb", "B", "rot270_mirror"),
                                    ("noise/raw B->rot270_mirror", "noise", "raw", "B", "rot270_mirror"),
                                    ("detail/srgb A->B", "detail", "srgb", "A", "B")):
        c = probe_case(src, dst, sig, colorspace=cs)
        tiles = c.convert().tiles
        row = {"name": name, "seeds": n_seeds}
        for tau in taus:
            reps = [c.probe(tiles, n=n, seed=sd, tau=tau) for sd in range(n_seeds)]
            fr = [r.n_bad / max(r.n_valid, 1) for r in reps]
            row[tau] = {"fails": sum(r.status == "fail" for r in reps), "worst_bad_pct": 100 * max(fr),
                        "mean_bad_pct": 100 * float(np.mean(fr))}
        pos_rows.append(row)
    for name, sig, make in (("smooth shift_u 3%", "smooth", lambda r, c: inject_shift_u(r.tiles, 0.03)),
                            ("detail shift_u 2%", "detail", lambda r, c: inject_shift_u(r.tiles, 0.02)),
                            ("noise shift_u 2%", "noise", lambda r, c: inject_shift_u(r.tiles, 0.02)),
                            ("smooth dropout 2%", "smooth", lambda r, c: inject_dropout(r.tiles, r.coverage, 0.02)),
                            ("detail dropout 2%", "detail", lambda r, c: inject_dropout(r.tiles, r.coverage, 0.02)),
                            ("noise dropout 2%", "noise", lambda r, c: inject_dropout(r.tiles, r.coverage, 0.02)),
                            ("smooth flip shell 2", "smooth", lambda r, c: inject_flip_shell(r.tiles, c.shells(), 2))):
        c = probe_case("A", "B", sig)
        tiles = make(c.convert(), c)
        row = {"name": name, "seeds": n_seeds}
        for tau in taus:
            reps = [c.probe(tiles, n=n, seed=sd, tau=tau) for sd in range(n_seeds)]
            fr = [r.n_bad / max(r.n_valid, 1) for r in reps]
            row[tau] = {"detected": sum(r.status == "fail" for r in reps), "min_bad_pct": 100 * min(fr),
                        "mean_bad_pct": 100 * float(np.mean(fr))}
        neg_rows.append(row)
    return pos_rows, neg_rows


def resolution_sweep():
    """解像度(= 縮小係数 s)とノイズ粒度で、窓方式 lattice / centered の偽陽性を比べる。A->B, raw, n=2000。"""
    rows = []
    for res in (256, 512, 1024, 2048):
        for k in (1, 2, 4):
            cell = k * 6.67 / res            # k テクセル相当の3Dセル幅
            c = Case(LAYOUT_A, LAYOUT_B, cell, res)
            tiles = c.convert().tiles
            row = {"res": res, "s": max(1, res // 256), "k": k}
            for fp in ("lattice", "centered"):
                rep = c.probe(tiles, n=CAL_N, footprint=fp)
                row[fp] = rep.n_bad / max(rep.n_valid, 1)
            rows.append(row)
    return rows


def _fmt_table(rows, kind):
    keys = list(CAL_ANGLES if kind == "normal" else CAL_TAUS)
    unit = "deg" if kind == "normal" else "tau"
    head = f"| scenario | n_valid | max_err | mean_err | " + " | ".join(f"bad@{unit}={k:g}" for k in keys) + " |"
    lines = [head, "|" + "---|" * (4 + len(keys))]
    for r in rows:
        lines.append(f"| {r['name']} | {r['n_valid']} | {r['max_err']:.3f} | {r['mean_err']:.4f} | "
                     + " | ".join(f"{100 * r['bad'][k]:.1f}%" for k in keys) + " |")
    return "\n".join(lines)


def _calibration_main(argv):
    t0 = time.time()
    pos, neg = calibration_data()
    out = []
    for title, rows in (("Positives (correct conversions; bad% must stay below 2%)", pos),
                        ("Negatives (injected defects; bad% must exceed 2%)", neg)):
        for kind in ("color", "normal"):
            sub = [r for r in rows if r["kind"] == kind]
            if sub:
                out.append(f"\n### {title} - {kind}\n\n{_fmt_table(sub, kind)}")
    summary = ["\n### Threshold sweep summary (worst case over all scenarios)\n",
               "Required negatives only (1% shifts, and 2% shifts of the smooth texture, are sensitivity info).\n",
               "| threshold | worst positive bad% | FP margin (2% / worst) | weakest required negative bad% | detection margin (weakest / 2%) |",
               "|---|---|---|---|---|"]
    for kind, keys in (("color", CAL_TAUS), ("normal", CAL_ANGLES)):
        for k in keys:
            p = [r for r in pos if r["kind"] == kind]
            n = [r for r in neg if r["kind"] == kind and r.get("required", True)]
            wp = max(p, key=lambda r: r["bad"][k])
            wn = min(n, key=lambda r: r["bad"][k])
            fpm = "inf" if wp["bad"][k] == 0 else f"{0.02 / wp['bad'][k]:.1f}x"
            summary.append(f"| {kind} {'deg' if kind == 'normal' else 'tau'}={k:g} | "
                           f"{100 * wp['bad'][k]:.2f}% ({wp['name']}) | {fpm} | "
                           f"{100 * wn['bad'][k]:.1f}% ({wn['name']}) | {wn['bad'][k] / 0.02:.1f}x |")
    out.append("\n".join(summary))
    pos_s, neg_s = seed_sweep()
    ss = ["\n### False-alarm rate of correct conversions, default n=512, 40 seeds\n",
          "| scenario | fail@tau=0.08 | worst bad% | mean bad% | fail@tau=0.10 | worst bad% | mean bad% |",
          "|---|---|---|---|---|---|---|"]
    for r in pos_s:
        a8, a10 = r[0.08], r[0.10]
        ss.append(f"| {r['name']} | {a8['fails']} / {r['seeds']} | {a8['worst_bad_pct']:.2f}% | {a8['mean_bad_pct']:.2f}% | "
                  f"{a10['fails']} / {r['seeds']} | {a10['worst_bad_pct']:.2f}% | {a10['mean_bad_pct']:.2f}% |")
    out.append("\n".join(ss))
    ds = ["\n### Detection rate of injected defects, default n=512, 40 seeds\n",
          "| defect | detected@tau=0.08 | min bad% | mean bad% | detected@tau=0.10 | min bad% | mean bad% |",
          "|---|---|---|---|---|---|---|"]
    for r in neg_s:
        a8, a10 = r[0.08], r[0.10]
        ds.append(f"| {r['name']} | {a8['detected']} / {r['seeds']} | {a8['min_bad_pct']:.2f}% | {a8['mean_bad_pct']:.2f}% | "
                  f"{a10['detected']} / {r['seeds']} | {a10['min_bad_pct']:.2f}% | {a10['mean_bad_pct']:.2f}% |")
    out.append("\n".join(ds))
    rs = ["\n### Window method vs resolution (raw, A->B, noise whose correlation length is k texels; bad%)\n",
          "| res | s | k (texels) | lattice (contract wording) | centered (default) |", "|---|---|---|---|---|"]
    for r in resolution_sweep():
        rs.append(f"| {r['res']} | {r['s']} | {r['k']} | {100 * r['lattice']:.1f}% | {100 * r['centered']:.1f}% |")
    out.append("\n".join(rs))
    out.append(f"\n(measured in {time.time() - t0:.0f} s, n={CAL_N} probe points per scenario unless noted, seed 0)")
    print("\n".join(out))
    if "--json" in argv:
        path = argv[argv.index("--json") + 1]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"positives": pos, "negatives": neg}, fh, indent=1, default=float)


if __name__ == "__main__":
    if "--calibrate" in sys.argv:
        _calibration_main(sys.argv)
    else:
        raise SystemExit(pytest.main([__file__, "-q"] + sys.argv[1:]))
