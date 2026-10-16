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

import numpy as np
import pytest

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

import uvremap_core as uvc  # noqa: E402


# 重いテストは UVREMAP_SLOW=1 のときだけ実行する(マーカー slow)
_RUN_SLOW = bool(os.environ.get("UVREMAP_SLOW"))
slow = pytest.mark.skipif(not _RUN_SLOW, reason="slow test: set UVREMAP_SLOW=1")


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


LAYOUTS = {
    "A": LAYOUT_A,
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
