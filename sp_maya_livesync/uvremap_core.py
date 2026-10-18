# -*- coding: utf-8 -*-
"""
uvremap_core.py
UV再マップ機能の変換エンジン(Maya非依存、標準ライブラリ + numpy のみ)

旧UV(基準)で描かれたテクスチャを、新UV(現行)のレイアウトへ載せ替える。
方式F(面対応、同一トポロジー)のみを実装する。トポロジーが変わった場合は
Decision.method = "STOP" で報告し、変換しない(方式P・最近接探索は Phase 2)。

仕様の出典は docs/uvremap/CONTRACT.md(§1 画像・座標の規約, §2 スナップショット,
§3 core API, §3.1 3Dプローブ検証)と uv_remap_design_v0.2.md(§4)。

画像・座標の規約
    * 画像は float32 (H, W, C)、値域 [0, 1]、行0が上。
    * テクセル(x, y)の中心は u=(x+0.5)/W, v=1-(y+0.5)/H。
    * タイルは UDIM 番号 1001 + floor(u) + 10*floor(v) をキーにした dict。
      非UDIMは単一キー 1001 とし、UV は小数部で折り返す(udim=False)。
    * sRGBカラーは線形化してから補間し、再エンコードする。スカラー/法線は生値。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import numpy as np

__all__ = [
    "SCHEMA",
    "UVRemapError", "SnapshotError", "TextureSetNotFound", "ChannelFormatError",
    "ConversionStopped",
    "Snapshot", "make_snapshot", "save_snapshot", "load_snapshot",
    "triangulate_fan", "compute_topology_fingerprint",
    "compute_flipped_topology_fingerprint", "compute_uv_hash",
    "find_nonmanifold_faces", "find_faces_without_uv",
    "Decision", "classify",
    "ChannelSpec", "ConvertResult", "convert_channel",
    "ProbeReport", "probe_verify",
    "udim_tile", "udim_origin", "srgb_to_linear", "linear_to_srgb",
    "PROBE_TAU", "PROBE_NORMAL_DEG", "PROBE_MAX_BAD_FRACTION", "PROBE_MIN_VALID",
    "PROBE_FOOTPRINT",
]

SCHEMA = 1

# 3Dプローブ検証の既定しきい値(CONTRACT §3.1。根拠は docs/uvremap/PROBE_CALIBRATION.md)
PROBE_TAU = 0.10               # color/scalar: チャンネル最大の絶対差。契約の既定は 0.08 だが 0.10 に調整した
                               # (sRGB の白色ノイズで 0.08 では 40 シード中 2 回が偽陽性。PROBE_CALIBRATION.md)
PROBE_NORMAL_DEG = 8.0         # normal: 角度[deg]
PROBE_MAX_BAD_FRACTION = 0.02  # n_bad / n_valid がこれを超えたら fail
PROBE_MIN_VALID = 64           # n_valid がこれ未満なら inconclusive
PROBE_BORDER_TEXELS = 2        # 島の境界から除外するテクセル数(縮小後の解像度)
# 比較窓の方式。"centered" = プローブ点を中心とする s x s の箱型平均(新旧のテクセル密度比で窓を換算)、
# "lattice" = 画像の格子に固定した箱型縮小(契約文言どおり)。"lattice" は格子の位相差だけで細部
# テクスチャを誤検出するため既定は "centered"(docs/uvremap/PROBE_CALIBRATION.md、QUESTIONS.md Q1)。
PROBE_FOOTPRINT = "centered"

_UDIM_MAX_U = 9
_UDIM_MAX_V = 99
_EPS_TILE = 1e-9      # タイル境界(上端)の半開区間扱い
_EPS_IN = 1e-9        # ラスタライズの辺上許容
_EPS_STRICT = 1e-6    # 重なり判定の厳密内部
_NUDGE = 1e-6         # 旧テクスチャ参照時のタイル境界寄せ(三角形重心側へ)
_DEGENERATE_UV_AREA = 1e-14


# ----------------------------------------------------------------------------
# 例外
# ----------------------------------------------------------------------------

class UVRemapError(Exception):
    """core が投げる例外の基底。"""


class SnapshotError(UVRemapError, ValueError):
    """スナップショットの形式・内容が不正。"""


class TextureSetNotFound(UVRemapError, KeyError):
    """指定したテクスチャセット名がスナップショットに無い。"""

    def __str__(self) -> str:  # KeyError は repr を返してしまうため
        return Exception.__str__(self)


class ChannelFormatError(UVRemapError, ValueError):
    """テクスチャ配列やチャンネル指定が不正(形状、チャンネル数 等)。"""


class ConversionStopped(UVRemapError):
    """Decision が STOP、または変換できる面が無い。reasons に理由コードを持つ。"""

    def __init__(self, reasons: Sequence[str], message: str = ""):
        self.reasons = list(reasons)
        super().__init__(message or ", ".join(self.reasons))


# ----------------------------------------------------------------------------
# UDIM / 色空間ヘルパー
# ----------------------------------------------------------------------------

def udim_tile(u, v):
    """UV座標に対する UDIM 番号: 1001 + floor(u) + 10*floor(v)。"""
    u = np.asarray(u, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    return (1001 + np.floor(u) + 10 * np.floor(v)).astype(np.int64)


def udim_origin(tile: int) -> tuple[int, int]:
    """UDIM タイルの左下UV (u0, v0)。"""
    t = int(tile) - 1001
    if t < 0:
        raise ValueError(f"invalid UDIM tile {tile}")
    return t % 10, t // 10


def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float32)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(c):
    c = np.clip(np.asarray(c, dtype=np.float32), 0, None)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055).astype(np.float32)


# ----------------------------------------------------------------------------
# スナップショット
# ----------------------------------------------------------------------------

@dataclass
class Snapshot:
    """メッシュ全体 + TSごとのフェース集合(CONTRACT §2)。

    コーナー(フェース頂点)単位の配列を持つ。ts_names[n] と ts_faces[n] が対応する。
    """
    face_vertex_counts: np.ndarray   # int32 (F,)
    corner_vid: np.ndarray           # int32 (C,)
    corner_pos: np.ndarray           # float32 (C,3)
    corner_uv: np.ndarray            # float32 (C,2)  UV無しフェースは NaN
    face_mesh: np.ndarray            # int32 (F,)
    tri_face: np.ndarray             # int32 (T,)
    tri_corner: np.ndarray           # int8 (T,3)
    ts_names: list = field(default_factory=list)
    ts_faces: list = field(default_factory=list)   # list[int32 ndarray]
    meta: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def faces_of(self, ts_name: str) -> np.ndarray:
        try:
            return self.ts_faces[self.ts_names.index(ts_name)]
        except ValueError:
            raise TextureSetNotFound(f"texture set not in snapshot: {ts_name!r}") from None

    def face_offsets(self) -> np.ndarray:
        """各フェースの先頭コーナー番号(int64, (F,))。"""
        c = self.face_vertex_counts.astype(np.int64)
        return np.cumsum(c) - c


def _face_of_corner(counts: np.ndarray) -> np.ndarray:
    return np.repeat(np.arange(len(counts), dtype=np.int64), counts.astype(np.int64))


def triangulate_fan(face_vertex_counts) -> tuple[np.ndarray, np.ndarray]:
    """ファン三角形分割。(tri_face int32 (T,), tri_corner int8 (T,3)) を返す。

    テスト/合成用。実機の三角形分割は Maya 側(getTriangles)が基準側で固定する。
    """
    counts = np.asarray(face_vertex_counts, dtype=np.int64)
    if counts.size and int(counts.max()) > 128:
        raise SnapshotError("face with more than 128 corners (tri_corner is int8)")
    n_tri = np.maximum(counts - 2, 0)
    total = int(n_tri.sum())
    tri_face = np.repeat(np.arange(len(counts), dtype=np.int32), n_tri)
    first = np.cumsum(n_tri) - n_tri
    k = np.arange(total, dtype=np.int64) - np.repeat(first, n_tri)
    tri_corner = np.stack([np.zeros(total, np.int64), k + 1, k + 2], axis=1).astype(np.int8)
    return tri_face, tri_corner


def find_nonmanifold_faces(snap: Snapshot) -> np.ndarray:
    """同一フェース内で頂点IDが重複するフェースの bool マスク (F,)。"""
    counts = snap.face_vertex_counts
    foc = _face_of_corner(counts)
    key = (foc << 32) | snap.corner_vid.astype(np.int64)
    order = np.argsort(key, kind="stable")
    ks = key[order]
    dup = ks[1:] == ks[:-1]
    out = np.zeros(len(counts), dtype=bool)
    out[foc[order][1:][dup]] = True
    return out


def find_faces_without_uv(snap: Snapshot) -> np.ndarray:
    """UVが NaN/inf のコーナーを1つでも持つフェース(UV無しフェース)の bool マスク (F,)。"""
    counts = snap.face_vertex_counts
    bad_corner = ~np.isfinite(snap.corner_uv).all(axis=1)
    foc = _face_of_corner(counts)
    n_bad = np.bincount(foc, weights=bad_corner.astype(np.float64), minlength=len(counts))
    return n_bad > 0


def _bbox(corner_pos: np.ndarray) -> tuple[list, list]:
    p = np.asarray(corner_pos, dtype=np.float64)
    p = p[np.isfinite(p).all(axis=1)]
    if p.size == 0:
        return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    return [float(x) for x in p.min(axis=0)], [float(x) for x in p.max(axis=0)]


def make_snapshot(face_vertex_counts, corner_vid, corner_pos, corner_uv, *,
                  ts_faces: Mapping[str, Sequence[int]] | None = None,
                  face_mesh=None, tri_face=None, tri_corner=None,
                  baseline_id: str | None = None, uv_set: str = "map1",
                  mesh_uuids: Sequence[str] | None = None,
                  created: str | None = None,
                  mesh_file_sha1: str | None = None) -> Snapshot:
    """配列から Snapshot を組み立てる(型の正規化・検証・指紋/ハッシュ・警告の生成)。

    tri_face/tri_corner を省略するとファン分割を使う。現行側のスナップショットを作る
    ときは、基準側の tri_face/tri_corner を渡すこと(CONTRACT §2: 三角形分割は基準側で固定)。
    """
    counts = np.ascontiguousarray(face_vertex_counts, dtype=np.int32).reshape(-1)
    vid = np.ascontiguousarray(corner_vid, dtype=np.int32).reshape(-1)
    pos = np.ascontiguousarray(corner_pos, dtype=np.float32)
    uv = np.ascontiguousarray(corner_uv, dtype=np.float32)
    n_corner = int(counts.astype(np.int64).sum())
    if (counts < 0).any():
        raise SnapshotError("negative face_vertex_counts")
    if vid.shape != (n_corner,) or pos.shape != (n_corner, 3) or uv.shape != (n_corner, 2):
        raise SnapshotError(
            f"corner array shapes do not match counts: C={n_corner}, vid={vid.shape}, "
            f"pos={pos.shape}, uv={uv.shape}")
    if n_corner and int(vid.min()) < 0:
        raise SnapshotError("negative vertex id")
    n_face = len(counts)
    fm = (np.zeros(n_face, np.int32) if face_mesh is None
          else np.ascontiguousarray(face_mesh, dtype=np.int32).reshape(-1))
    if fm.shape != (n_face,):
        raise SnapshotError("face_mesh shape mismatch")
    if tri_face is None or tri_corner is None:
        tf, tc = triangulate_fan(counts)
    else:
        tf = np.ascontiguousarray(tri_face, dtype=np.int32).reshape(-1)
        tc = np.ascontiguousarray(tri_corner, dtype=np.int8).reshape(-1, 3)
    if tf.shape[0] != tc.shape[0]:
        raise SnapshotError("tri_face / tri_corner length mismatch")
    if tf.size:
        if int(tf.min()) < 0 or int(tf.max()) >= n_face:
            raise SnapshotError("tri_face out of range")
        if int(tc.min()) < 0 or (tc.astype(np.int64) >= counts[tf][:, None]).any():
            raise SnapshotError("tri_corner out of range for its face")

    if ts_faces is None:
        ts_faces = {"default": np.arange(n_face)}
    names, faces = [], []
    for name, ids in ts_faces.items():
        arr = np.unique(np.asarray(ids, dtype=np.int64).reshape(-1))
        if arr.size and (int(arr[0]) < 0 or int(arr[-1]) >= n_face):
            raise SnapshotError(f"ts_faces[{name!r}] has face id out of range")
        names.append(str(name))
        faces.append(arr.astype(np.int32))

    snap = Snapshot(counts, vid, pos, uv, fm, tf, tc, names, faces, {}, [])

    warnings: list[str] = []
    nm = int(find_nonmanifold_faces(snap).sum())
    if nm:
        warnings.append(f"non_manifold_faces: {nm}")
    nouv = int(find_faces_without_uv(snap).sum())
    if nouv:
        warnings.append(f"faces_without_uv: {nouv}")
    small = int((counts < 3).sum())
    if small:
        warnings.append(f"degenerate_faces: {small}")
    snap.warnings = warnings

    bmin, bmax = _bbox(pos)
    fp = compute_topology_fingerprint(snap)
    uvh = compute_uv_hash(snap)
    meta = {
        "schema": SCHEMA,
        "baseline_id": baseline_id or f"bl_{fp[:12]}_{uvh[:8]}",
        "uv_set": str(uv_set),
        "ts_names": list(names),
        "mesh_uuids": list(mesh_uuids) if mesh_uuids is not None
        else [f"mesh{i}" for i in range(int(fm.max()) + 1 if fm.size else 0)],
        "topology_fingerprint": fp,
        "uv_hash": uvh,
        "bbox_min": bmin,
        "bbox_max": bmax,
        "created": created or datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0).isoformat(),
    }
    if mesh_file_sha1:
        meta["mesh_file_sha1"] = mesh_file_sha1
    snap.meta = meta
    return snap


_NPZ_KEYS = ("face_vertex_counts", "corner_vid", "corner_pos", "corner_uv",
             "face_mesh", "tri_face", "tri_corner")


def _atomic_write_bytes(path: Path, writer) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            writer(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def save_snapshot(dirpath: Path, snap: Snapshot) -> None:
    """baseline/<baseline_id>/ に mesh.npz と meta.json を書く(一時ファイル → os.replace)。

    meta.json には snap.meta に加えて `warnings` を保存する(往復で失わないため)。
    """
    dirpath = Path(dirpath)
    dirpath.mkdir(parents=True, exist_ok=True)
    arrays = {k: getattr(snap, k) for k in _NPZ_KEYS}
    for n, ids in enumerate(snap.ts_faces):
        arrays[f"ts_faces__{n}"] = np.asarray(ids, dtype=np.int32)
    _atomic_write_bytes(dirpath / "mesh.npz", lambda fh: np.savez_compressed(fh, **arrays))
    meta = dict(snap.meta)
    meta["ts_names"] = list(snap.ts_names)
    meta["warnings"] = list(snap.warnings)
    text = json.dumps(meta, ensure_ascii=False, indent=2)
    _atomic_write_bytes(dirpath / "meta.json", lambda fh: fh.write(text.encode("utf-8")))


def load_snapshot(dirpath: Path) -> Snapshot:
    """save_snapshot の逆。形式不正・欠落・schema 不一致は SnapshotError。"""
    dirpath = Path(dirpath)
    try:
        meta = json.loads((dirpath / "meta.json").read_text(encoding="utf-8"))
        with np.load(dirpath / "mesh.npz", allow_pickle=False) as z:
            data = {k: z[k] for k in z.files}
    except (OSError, ValueError, KeyError) as exc:
        raise SnapshotError(f"cannot read snapshot {dirpath}: {exc}") from exc
    if not isinstance(meta, dict) or meta.get("schema") != SCHEMA:
        raise SnapshotError(f"unsupported snapshot schema: {meta.get('schema')!r}"
                            if isinstance(meta, dict) else "meta.json is not an object")
    missing = [k for k in _NPZ_KEYS if k not in data]
    if missing:
        raise SnapshotError(f"mesh.npz is missing keys: {missing}")
    names = list(meta.get("ts_names", []))
    try:
        faces = [np.asarray(data[f"ts_faces__{n}"], dtype=np.int32) for n in range(len(names))]
    except KeyError as exc:
        raise SnapshotError(f"mesh.npz is missing {exc}") from exc
    warnings = list(meta.pop("warnings", []))
    return Snapshot(
        data["face_vertex_counts"].astype(np.int32, copy=False),
        data["corner_vid"].astype(np.int32, copy=False),
        data["corner_pos"].astype(np.float32, copy=False),
        data["corner_uv"].astype(np.float32, copy=False),
        data["face_mesh"].astype(np.int32, copy=False),
        data["tri_face"].astype(np.int32, copy=False),
        data["tri_corner"].astype(np.int8, copy=False),
        names, faces, meta, warnings)


# ----------------------------------------------------------------------------
# 指紋 / ハッシュ
# ----------------------------------------------------------------------------

def _canonical_vids(counts: np.ndarray, vids: np.ndarray, reverse: bool = False) -> np.ndarray:
    """各フェースの頂点ID列を巡回の正規形(最小IDが先頭)にして返す(向きは保存)。

    reverse=True なら各フェースの並びを逆順にしてから正規化する(法線反転の判定用)。
    最小IDが重複する(非多様体)フェースは、全回転の辞書順最小で決める。
    """
    counts = counts.astype(np.int64)
    vids = vids.astype(np.int64)
    offs = np.cumsum(counts) - counts
    out = np.empty_like(vids)
    for n in np.unique(counts):
        n = int(n)
        if n == 0:
            continue
        rows = np.flatnonzero(counts == n)
        idx = offs[rows][:, None] + np.arange(n)[None, :]
        seq = vids[idx]
        if reverse:
            seq = seq[:, ::-1]
        start = seq.argmin(axis=1)
        ties = np.flatnonzero((seq == seq.min(axis=1, keepdims=True)).sum(axis=1) > 1)
        for r in ties:
            row = seq[r]
            best = min(range(n), key=lambda k: tuple(np.roll(row, -k)))
            start[r] = best
        rot = (start[:, None] + np.arange(n)[None, :]) % n
        out[idx] = np.take_along_axis(seq, rot, axis=1)
    return out


def _hash_topology(counts: np.ndarray, canon: np.ndarray) -> str:
    h = hashlib.sha1()
    h.update(b"uvremap-topo-1")
    h.update(counts.astype("<i4").tobytes())
    h.update(canon.astype("<i4").tobytes())
    return h.hexdigest()


def compute_topology_fingerprint(snap: Snapshot) -> str:
    """トポロジー指紋: 各フェースの頂点ID列の巡回正規形(向きを含む)のSHA-1。

    UVの変更(切断・縫合・移動)では変わらない。頂点ID列の巡回回転でも変わらない。
    頂点ID列を逆順にする(法線反転)と変わる。
    """
    canon = _canonical_vids(snap.face_vertex_counts, snap.corner_vid)
    return _hash_topology(snap.face_vertex_counts, canon)


def compute_flipped_topology_fingerprint(snap: Snapshot) -> str:
    """全フェースの頂点ID列を逆順にしたトポロジーの指紋(F_FLIPPED の判定用)。"""
    canon = _canonical_vids(snap.face_vertex_counts, snap.corner_vid, reverse=True)
    return _hash_topology(snap.face_vertex_counts, canon)


def compute_uv_hash(snap: Snapshot, ts_name: str | None = None) -> str:
    """UVのハッシュ。TS指定時はそのTSのフェース(集合 + UV)のみ。

    トポロジーが同じでUVだけ変わったときに変化する。-0.0 と 0.0、NaN のビット差は正規化する。
    """
    counts = snap.face_vertex_counts
    n_face = len(counts)
    faces = np.arange(n_face, dtype=np.int32) if ts_name is None else snap.faces_of(ts_name)
    sel = np.zeros(n_face, dtype=bool)
    sel[faces] = True
    uv = snap.corner_uv[sel[_face_of_corner(counts)]].astype(np.float64)
    nan = ~np.isfinite(uv).all(axis=1)
    uv = np.where(nan[:, None], 0.0, uv).astype("<f4") + np.float32(0.0)
    h = hashlib.sha1()
    h.update(b"uvremap-uv-1")
    h.update(np.asarray(faces, dtype="<i4").tobytes())
    h.update(counts[faces].astype("<i4").tobytes())
    h.update(nan.tobytes())
    h.update(uv.tobytes())
    return h.hexdigest()


# ----------------------------------------------------------------------------
# 判定
# ----------------------------------------------------------------------------

@dataclass
class Decision:
    """変換方式の判定結果。method は "F" / "F_FLIPPED" / "STOP"("P" は Phase 2 で追加)。"""
    method: Literal["F", "F_FLIPPED", "STOP"]
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"method": self.method, "reasons": list(self.reasons),
                "warnings": list(self.warnings), "details": dict(self.details)}


def classify(baseline: Snapshot, current: Snapshot, ts_name: str, *,
             bbox_tol: float = 1e-3) -> Decision:
    """基準と現行のスナップショットから変換方式を決める(CONTRACT §3)。

    * 指紋一致 → "F"。全フェースの頂点ID列が逆順で一致 → "F_FLIPPED"。
    * それ以外 → "STOP"(reasons に "topology_changed")。TS名が無い → "STOP"("ts_missing")。
    * bbox不一致(対角線比 > bbox_tol)・TSのフェース集合の変化・メッシュUUIDの変化は warnings。
    * 指紋はメッシュ全体で取る(設計書 4.3)。TSのフェース集合の差は指紋に影響しない。
    """
    warnings: list[str] = []
    details: dict[str, Any] = {}

    missing_in = [label for label, s in (("baseline", baseline), ("current", current))
                  if ts_name not in s.ts_names]
    if missing_in:
        details["ts_missing_in"] = missing_in
        details["ts_name"] = ts_name
        return Decision("STOP", ["ts_missing"], warnings, details)

    bc, cc = baseline.face_vertex_counts, current.face_vertex_counts
    details["face_count"] = {"baseline": int(len(bc)), "current": int(len(cc))}
    if bc.shape != cc.shape or not np.array_equal(bc, cc):
        details["mismatch"] = "face_vertex_counts"
        return Decision("STOP", ["topology_changed"], warnings, details)

    canon_b = _canonical_vids(bc, baseline.corner_vid)
    fp_b = _hash_topology(bc, canon_b)
    canon_c = _canonical_vids(cc, current.corner_vid)
    fp_c = _hash_topology(cc, canon_c)
    details["fingerprint"] = {"baseline": fp_b, "current": fp_c}

    method: str
    reasons: list[str] = []
    if fp_c == fp_b:
        method = "F"
    else:
        canon_f = _canonical_vids(cc, current.corner_vid, reverse=True)
        if _hash_topology(cc, canon_f) == fp_b:
            method = "F_FLIPPED"
            warnings.append("normals_flipped: tangent-space normal maps are approximate")
        else:
            method = "STOP"
            reasons.append("topology_changed")
            foc = _face_of_corner(bc)
            n_face = len(bc)
            same = np.bincount(foc, weights=(canon_c != canon_b).astype(np.float64),
                               minlength=n_face) == 0
            flipped = np.bincount(foc, weights=(canon_f != canon_b).astype(np.float64),
                                  minlength=n_face) == 0
            details["faces_same"] = int(same.sum())
            details["faces_flipped"] = int((flipped & ~same).sum())
            details["faces_changed"] = int((~same & ~flipped).sum())
            if details["faces_flipped"] and not details["faces_changed"]:
                details["partially_flipped"] = True

    if method != "STOP":
        diag = _bbox_check(baseline, current, bbox_tol, warnings, details)
        details["bbox_diag"] = diag
        _ts_face_diff(baseline, current, ts_name, warnings, details)
    if list(baseline.meta.get("mesh_uuids", [])) != list(current.meta.get("mesh_uuids", [])):
        warnings.append("mesh_uuids_changed")
    if baseline.meta.get("uv_set") != current.meta.get("uv_set"):
        warnings.append("uv_set_changed")
    if baseline.warnings:
        details["baseline_warnings"] = list(baseline.warnings)
    if current.warnings:
        details["current_warnings"] = list(current.warnings)
    return Decision(method, reasons, warnings, details)


def _bbox_check(baseline: Snapshot, current: Snapshot, tol: float,
                warnings: list, details: dict) -> float:
    bmin, bmax = _bbox(baseline.corner_pos)
    cmin, cmax = _bbox(current.corner_pos)
    bmin, bmax, cmin, cmax = (np.asarray(x) for x in (bmin, bmax, cmin, cmax))
    diag = float(np.linalg.norm(bmax - bmin))
    scale = diag if diag > 0 else 1.0
    rel = float(max(np.abs(bmin - cmin).max(), np.abs(bmax - cmax).max()) / scale)
    details["bbox_rel_diff"] = rel
    if rel > tol:
        warnings.append(f"bbox_changed: relative difference {rel:.3g} > {tol:g}")
    return diag


def _ts_face_diff(baseline: Snapshot, current: Snapshot, ts_name: str,
                  warnings: list, details: dict) -> None:
    b = set(baseline.faces_of(ts_name).tolist())
    c = set(current.faces_of(ts_name).tolist())
    if b != c:
        details["ts_faces_added"] = len(c - b)
        details["ts_faces_removed"] = len(b - c)
        warnings.append(f"ts_faces_changed: +{len(c - b)} / -{len(b - c)}")


# ----------------------------------------------------------------------------
# 三角形集合の収集(変換とプローブで共有する)
# ----------------------------------------------------------------------------

@dataclass
class _TriSet:
    face: np.ndarray        # (T,) フェースID
    tri: np.ndarray         # (T,) 基準の三角形配列へのインデックス
    old_uv: np.ndarray      # (T,3,2) float64
    new_uv: np.ndarray      # (T,3,2) float64
    pos: np.ndarray         # (T,3,3) float64(基準側の頂点位置)
    stats: dict


def _corner_map(base: Snapshot, cur: Snapshot) -> tuple[np.ndarray, np.ndarray]:
    """基準のコーナー番号 → 現行のコーナー番号(同一フェース内の頂点ID一致で対応付ける)。

    巡回回転・逆順のどちらでも正しく対応する。(map, found) を返す。
    """
    foc = _face_of_corner(base.face_vertex_counts)
    kb = (foc << 32) | base.corner_vid.astype(np.int64)
    kc = (foc << 32) | cur.corner_vid.astype(np.int64)
    order = np.argsort(kc, kind="stable")
    ks = kc[order]
    if ks.size == 0:
        return order, np.zeros(0, dtype=bool)
    pos = np.minimum(np.searchsorted(ks, kb), len(ks) - 1)
    return order[pos], ks[pos] == kb


def _tri_area_uv(uv: np.ndarray) -> np.ndarray:
    e1 = uv[:, 1] - uv[:, 0]
    e2 = uv[:, 2] - uv[:, 0]
    return 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e2[:, 0] * e1[:, 1])


def _collect_triangles(base: Snapshot, cur: Snapshot, ts_name: str) -> _TriSet:
    """TSの変換対象三角形を集める。除外した面は stats に件数を残す。

    除外: UV無し(基準または現行)、非多様体(同一フェース内の頂点ID重複)、
    基準TSに無い面(元テクスチャ無し)。三角形分割は基準側のものを両側で使う。
    """
    bf = base.faces_of(ts_name)
    cf = cur.faces_of(ts_name)
    if (base.face_vertex_counts.shape != cur.face_vertex_counts.shape
            or not np.array_equal(base.face_vertex_counts, cur.face_vertex_counts)):
        raise ConversionStopped(["topology_changed"], "face_vertex_counts differ")
    n_face = len(base.face_vertex_counts)
    cmap, found = _corner_map(base, cur)
    nonman = find_nonmanifold_faces(base) | find_nonmanifold_faces(cur)
    foc = _face_of_corner(base.face_vertex_counts)
    if (~found[~nonman[foc]]).any():
        raise ConversionStopped(["topology_changed"], "vertex ids differ within a face")
    nouv = find_faces_without_uv(base) | find_faces_without_uv(cur)

    in_b = np.zeros(n_face, dtype=bool)
    in_b[bf] = True
    in_c = np.zeros(n_face, dtype=bool)
    in_c[cf] = True
    union = in_b | in_c
    ok = in_b & in_c & ~nonman & ~nouv
    small = base.face_vertex_counts < 3

    sel = np.flatnonzero(ok[base.tri_face])
    boff = base.face_offsets()
    bco = boff[base.tri_face[sel]][:, None] + base.tri_corner[sel].astype(np.int64)
    cco = cmap[bco]
    old_uv = base.corner_uv[bco].astype(np.float64)
    new_uv = cur.corner_uv[cco].astype(np.float64)
    pos = base.corner_pos[bco].astype(np.float64)

    stats = {
        "faces_total_ts": int(in_c.sum()),
        "faces_converted": int((ok & ~small).sum()),
        "faces_no_source": int((in_c & ~in_b).sum()),
        "faces_dropped": int((in_b & ~in_c).sum()),
        "faces_without_uv": int((nouv & union).sum()),
        "faces_non_manifold": int((nonman & union).sum()),
        "tris_converted": int(len(sel)),
        "tris_degenerate_new": int((_tri_area_uv(new_uv) < _DEGENERATE_UV_AREA).sum()),
        "tris_degenerate_old": int((_tri_area_uv(old_uv) < _DEGENERATE_UV_AREA).sum()),
    }
    return _TriSet(base.tri_face[sel].astype(np.int64), sel.astype(np.int64),
                   old_uv, new_uv, pos, stats)


def _resolve_udim(udim: bool | None, baseline: Snapshot) -> bool:
    """udim の決定順: 引数 > baseline.meta["udim"] > True(UDIM)。"""
    if udim is not None:
        return bool(udim)
    m = baseline.meta.get("udim")
    return True if m is None else bool(m)


# ----------------------------------------------------------------------------
# レイアウト(三角形 → タイルの割り当て)とラスタライズ
# ----------------------------------------------------------------------------

def _tile_pieces(uv: np.ndarray, udim: bool, max_span: int = 64):
    """各三角形が触れるタイルの(三角形, iu, iv, タイルキー)を列挙する。

    バウンディングボックスの上端は半開区間(- 1e-9)で扱い、ちょうど u=1.0 で終わる
    シェルが隣のタイルを触れないようにする。UDIMモードでは範囲外(u<0, u>9, v<0)を捨てて
    その三角形を oor(範囲外)に記録する。非UDIMモードでは全部を同一キー 1001 にする。
    """
    n_tri = uv.shape[0]
    lo = np.floor(uv.min(axis=1)).astype(np.int64)
    hi = np.maximum(np.floor(uv.max(axis=1) - _EPS_TILE).astype(np.int64), lo)
    span = hi - lo + 1
    straddle = (span[:, 0] > 1) | (span[:, 1] > 1)
    oor = (span[:, 0] > max_span) | (span[:, 1] > max_span)
    cnt = np.where(oor, 0, span[:, 0] * span[:, 1])
    total = int(cnt.sum())
    rep = np.repeat(np.arange(n_tri), cnt)
    first = np.cumsum(cnt) - cnt
    local = np.arange(total) - np.repeat(first, cnt)
    su = span[rep, 0]
    iu = lo[rep, 0] + local % su
    iv = lo[rep, 1] + local // su
    if udim:
        ok = (iu >= 0) & (iu <= _UDIM_MAX_U) & (iv >= 0) & (iv <= _UDIM_MAX_V)
        bad = np.zeros(n_tri, dtype=bool)
        bad[rep[~ok]] = True
        oor = oor | bad
        rep, iu, iv = rep[ok], iu[ok], iv[ok]
        key = 1001 + iu + 10 * iv
    else:
        key = np.full(len(rep), 1001, dtype=np.int64)
    return rep, iu, iv, key, straddle, oor


@dataclass
class _Raster:
    tri: np.ndarray          # (H,W) int32 三角形インデックス(-1 = 空)
    bary: np.ndarray         # (H,W,2) float32 重心座標 l0, l1(l2 = 1-l0-l1)
    overlap_idx: np.ndarray  # 厳密内部が2枚以上の三角形に入るテクセルの平坦インデックス
    degenerate: np.ndarray   # 退化(UV面積0)でスキップした三角形(入力内のインデックス)


def _tri_setup(uv: np.ndarray, W: int, H: int):
    px = uv[..., 0] * W - 0.5
    py = (1.0 - uv[..., 1]) * H - 0.5
    x0, x1, x2 = px[:, 0], px[:, 1], px[:, 2]
    y0, y1, y2 = py[:, 0], py[:, 1], py[:, 2]
    D = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    scale = np.maximum(1.0, np.abs(px).max(1) + np.abs(py).max(1)) ** 2
    degenerate = np.abs(D) <= 1e-12 * scale
    Ds = np.where(degenerate, 1.0, D)
    a0 = (y1 - y2) / Ds
    b0 = (x2 - x1) / Ds
    c0 = -(a0 * x2 + b0 * y2)
    a1 = (y2 - y0) / Ds
    b1 = (x0 - x2) / Ds
    c1 = -(a1 * x2 + b1 * y2)
    bx0 = np.clip(np.ceil(px.min(1)), 0, W).astype(np.int64)
    bx1 = np.clip(np.floor(px.max(1)), -1, W - 1).astype(np.int64)
    by0 = np.clip(np.ceil(py.min(1)), 0, H).astype(np.int64)
    by1 = np.clip(np.floor(py.max(1)), -1, H - 1).astype(np.int64)
    coef = np.stack([a0, b0, c0, a1, b1, c1], 1)
    return coef, bx0, bx1, by0, by1, degenerate


def _rasterize(uv: np.ndarray, W: int, H: int, chunk_elems: int = 1 << 21) -> _Raster:
    """タイル内ローカルUVの三角形 (P,3,2) を W x H にラスタライズする。

    テクセル中心ごとに、それを含む三角形と重心座標を求める。共有辺上のテクセルは後に書いた
    三角形が取る(入力が同じなら結果も同じ)。バウンディングボックスが小さい三角形は
    2のべき乗サイズごとにまとめて処理し、巨大な三角形は行帯に分けて処理する(メモリ抑制)。
    """
    uv = np.asarray(uv, dtype=np.float64)
    tri = np.full((H, W), -1, dtype=np.int32)
    bary = np.zeros((H, W, 2), dtype=np.float32)
    n_tri = uv.shape[0]
    empty = np.zeros(0, dtype=np.int64)
    if n_tri == 0:
        return _Raster(tri, bary, empty, empty)
    coef, bx0, bx1, by0, by1, degen = _tri_setup(uv, W, H)
    bw = bx1 - bx0 + 1
    bh = by1 - by0 + 1
    live = (~degen) & (bw > 0) & (bh > 0)
    big = live & (bw * bh > chunk_elems)
    small_ids = np.flatnonzero(live & ~big)
    strict_idx: list[np.ndarray] = []

    def commit(m, l0, l1, l2, X, Y, ids_of_row, valid=None):
        gy = Y[m]
        gx = X[m]
        tri[gy, gx] = ids_of_row[m]
        bary[gy, gx, 0] = l0[m]
        bary[gy, gx, 1] = l1[m]
        s = (l0 > _EPS_STRICT) & (l1 > _EPS_STRICT) & (l2 > _EPS_STRICT)
        if valid is not None:
            s = s & valid
        if s.any():
            strict_idx.append((Y[s] * W + X[s]).astype(np.int64))

    if small_ids.size:
        pw = 1 << np.ceil(np.log2(np.maximum(bw[small_ids], 1))).astype(np.int64)
        ph = 1 << np.ceil(np.log2(np.maximum(bh[small_ids], 1))).astype(np.int64)
        key = pw * (1 << 20) + ph
        order = np.argsort(key, kind="stable")
        ids, pw, ph, key = small_ids[order], pw[order], ph[order], key[order]
        starts = np.r_[0, np.nonzero(np.diff(key))[0] + 1, len(ids)]
        for s0, s1 in zip(starts[:-1], starts[1:]):
            bwp, bhp = int(pw[s0]), int(ph[s0])
            per = max(1, chunk_elems // (bwp * bhp))
            gx = np.arange(bwp, dtype=np.int64)[None, None, :]
            gy = np.arange(bhp, dtype=np.int64)[None, :, None]
            for c0_ in range(s0, s1, per):
                sel = ids[c0_:min(s1, c0_ + per)]
                shape = (len(sel), bhp, bwp)
                X = np.broadcast_to(bx0[sel][:, None, None] + gx, shape)
                Y = np.broadcast_to(by0[sel][:, None, None] + gy, shape)
                valid = (X <= bx1[sel][:, None, None]) & (Y <= by1[sel][:, None, None])
                cf = coef[sel]
                Xf = X.astype(np.float64)
                Yf = Y.astype(np.float64)
                l0 = cf[:, 0, None, None] * Xf + cf[:, 1, None, None] * Yf + cf[:, 2, None, None]
                l1 = cf[:, 3, None, None] * Xf + cf[:, 4, None, None] * Yf + cf[:, 5, None, None]
                l2 = 1.0 - l0 - l1
                m = valid & (l0 >= -_EPS_IN) & (l1 >= -_EPS_IN) & (l2 >= -_EPS_IN)
                if not m.any():
                    continue
                row_ids = np.broadcast_to(sel[:, None, None], shape)
                commit(m, l0, l1, l2, X, Y, row_ids, valid)

    for f in np.flatnonzero(big):
        band = max(1, chunk_elems // int(bw[f]))
        xs = np.arange(bx0[f], bx1[f] + 1, dtype=np.int64)
        a0, b0, c0, a1, b1, c1 = coef[f]
        for y_start in range(int(by0[f]), int(by1[f]) + 1, band):
            ys = np.arange(y_start, min(int(by1[f]) + 1, y_start + band), dtype=np.int64)
            X = np.broadcast_to(xs[None, :], (len(ys), len(xs)))
            Y = np.broadcast_to(ys[:, None], (len(ys), len(xs)))
            l0 = a0 * X + b0 * Y + c0
            l1 = a1 * X + b1 * Y + c1
            l2 = 1.0 - l0 - l1
            m = (l0 >= -_EPS_IN) & (l1 >= -_EPS_IN) & (l2 >= -_EPS_IN)
            if m.any():
                commit(m, l0, l1, l2, X, Y, np.full(X.shape, f, dtype=np.int64))

    if strict_idx:
        flat = np.concatenate(strict_idx)
        flat.sort()
        dup = flat[1:] == flat[:-1]
        overlap_idx = np.unique(flat[1:][dup])
    else:
        overlap_idx = empty
    return _Raster(tri, bary, overlap_idx, np.flatnonzero(degen))


class _Layout:
    """UVレイアウト(三角形集合)とタイルへの割り当て。"""

    def __init__(self, uv: np.ndarray, udim: bool):
        self.uv = uv
        self.udim = udim
        (self.rep, self.iu, self.iv, self.key,
         self.straddle, self.oor) = _tile_pieces(uv, udim)

    def tile_keys(self) -> np.ndarray:
        return np.unique(self.key)

    def rasterize(self, key: int, W: int, H: int):
        """タイル key を W x H で描く。(三角形マップ(-1=空), 重心座標(H,W,2), overlap_idx)。"""
        sel = np.flatnonzero(self.key == key)
        tri_of_piece = self.rep[sel]
        local = self.uv[tri_of_piece] - np.stack([self.iu[sel], self.iv[sel]], axis=1)[:, None, :]
        r = _rasterize(local, W, H)
        tri_map = np.where(r.tri >= 0, tri_of_piece[np.maximum(r.tri, 0)], -1).astype(np.int32)
        return tri_map, r.bary, r.overlap_idx


# ----------------------------------------------------------------------------
# サンプリング / 膨張 / 法線補正
# ----------------------------------------------------------------------------

def _bilinear_xy(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """ピクセル座標(テクセル中心が整数)でのバイリニア補間(端はクランプ)。(N,C) を返す。"""
    H, W = img.shape[:2]
    x = np.clip(x, 0, W - 1)
    y = np.clip(y, 0, H - 1)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.minimum(x0 + 1, W - 1)
    y1 = np.minimum(y0 + 1, H - 1)
    fx = (x - x0).astype(np.float32)[:, None]
    fy = (y - y0).astype(np.float32)[:, None]
    a, b, c, d = img[y0, x0], img[y0, x1], img[y1, x0], img[y1, x1]
    top = a + (b - a) * fx
    bot = c + (d - c) * fx
    return top + (bot - top) * fy


def _sample_bilinear(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """タイル内ローカルUVでのバイリニア補間(中心規約: x = u*W-0.5, y = (1-v)*H-0.5)。"""
    H, W = img.shape[:2]
    x = np.asarray(u, dtype=np.float64) * W - 0.5
    y = (1.0 - np.asarray(v, dtype=np.float64)) * H - 0.5
    return _bilinear_xy(img, x, y)


_NEIGHBORS = tuple((dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0))


def _dilate(img: np.ndarray, mask: np.ndarray, pixels: int) -> tuple[np.ndarray, np.ndarray]:
    """エッジ膨張(パディング): 有効テクセルの外側へ pixels だけ広げる。

    空テクセルのうち8近傍に有効テクセルがあるものを、その近傍の平均で埋める操作を
    pixels 回繰り返す。フロンティアのみを更新するので、画像全体の平均を取るより速い。
    """
    out = np.array(img, dtype=np.float32, copy=True)
    m = np.array(mask, dtype=bool, copy=True)
    if pixels <= 0 or m.all() or not m.any():
        return out, m
    H, W = m.shape
    C = out.shape[2]
    flat = out.reshape(H * W, C)
    mflat = m.reshape(-1)
    for _ in range(int(pixels)):
        pad = np.zeros((H + 2, W + 2), dtype=np.uint8)
        pad[1:-1, 1:-1] = m
        cnt = np.zeros((H, W), dtype=np.uint8)
        for dy in range(3):
            for dx in range(3):
                if (dy, dx) != (1, 1):
                    cnt += pad[dy:dy + H, dx:dx + W]
        idx = np.flatnonzero((~m) & (cnt > 0))
        if idx.size == 0:
            break
        ys, xs = np.divmod(idx, W)
        acc = np.zeros((idx.size, C), dtype=np.float32)
        num = np.zeros(idx.size, dtype=np.float32)
        for dy, dx in _NEIGHBORS:
            yy = ys + dy
            xx = xs + dx
            inside = (yy >= 0) & (yy < H) & (xx >= 0) & (xx < W)
            yc = np.clip(yy, 0, H - 1)
            xc = np.clip(xx, 0, W - 1)
            use = inside & m[yc, xc]
            acc[use] += flat[yc[use] * W + xc[use]]
            num[use] += 1.0
        flat[idx] = acc / num[:, None]
        mflat[idx] = True
    return out, m


def _polar_Q(old_uv: np.ndarray, new_uv: np.ndarray) -> np.ndarray:
    """三角形ごとの 2x2 直交行列 Q: 旧UVの接空間XY → 新UVの接空間XY。

    UV空間のヤコビアン L = [f1 f2][e1 e2]^-1(e: 旧UV辺, f: 新UV辺)の直交極因子。
    回転・反転・一様スケールのシェルでは厳密。det(Q) = -1 は反転。退化三角形は単位行列。
    """
    e1 = old_uv[:, 1] - old_uv[:, 0]
    e2 = old_uv[:, 2] - old_uv[:, 0]
    f1 = new_uv[:, 1] - new_uv[:, 0]
    f2 = new_uv[:, 2] - new_uv[:, 0]
    Eo = np.stack([e1, e2], axis=2)
    En = np.stack([f1, f2], axis=2)
    ok = (np.abs(np.linalg.det(Eo)) > 1e-18) & (np.abs(np.linalg.det(En)) > 1e-18)
    Q = np.tile(np.eye(2), (len(old_uv), 1, 1))
    if ok.any():
        L = En[ok] @ np.linalg.inv(Eo[ok])
        U, _, Vt = np.linalg.svd(L)
        Q[ok] = U @ Vt
    return Q


def _rotate_normals(enc: np.ndarray, Q: np.ndarray, green_down: bool) -> np.ndarray:
    """エンコード済み接空間法線 (N,C>=3) の XY を Q で回して正規化し、再エンコードする。"""
    n = enc[:, :3].astype(np.float32) * 2.0 - 1.0
    if green_down:
        n[:, 1] = -n[:, 1]
    xy = np.einsum("nij,nj->ni", Q.astype(np.float32), n[:, :2])
    n = np.concatenate([xy, n[:, 2:3]], axis=1)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    if green_down:
        n[:, 1] = -n[:, 1]
    out = enc.astype(np.float32).copy()
    out[:, :3] = n * 0.5 + 0.5
    return out


# ----------------------------------------------------------------------------
# 変換
# ----------------------------------------------------------------------------

@dataclass
class ChannelSpec:
    suffix: str
    colorspace: Literal["srgb", "raw"]
    kind: Literal["color", "scalar", "normal"]
    normal_green_down: bool = False   # OpenGL 規約が既定(Normal_OpenGL)

    def __post_init__(self):
        if self.colorspace not in ("srgb", "raw"):
            raise ValueError(f"colorspace must be 'srgb' or 'raw': {self.colorspace!r}")
        if self.kind not in ("color", "scalar", "normal"):
            raise ValueError(f"kind must be 'color', 'scalar' or 'normal': {self.kind!r}")


@dataclass
class ConvertResult:
    tiles: dict      # {tile: float32 (H,W,C)} 新UVのタイルのみ
    coverage: dict   # {tile: bool (H,W)} ラスタライズされたテクセル(膨張前)
    stats: dict      # 警告集計(重なり、UDIM境界またぎ、元テクスチャ無し面 等)


def _to_float_image(arr, tile: int) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim == 2:
        a = a[:, :, None]
    if a.ndim != 3 or a.shape[0] == 0 or a.shape[1] == 0 or a.shape[2] == 0:
        raise ChannelFormatError(f"tile {tile}: expected (H, W, C) array, got shape {a.shape}")
    if a.dtype == np.uint8:
        return a.astype(np.float32) / 255.0
    if a.dtype == np.uint16:
        return a.astype(np.float32) / 65535.0
    return a.astype(np.float32, copy=False)


def _color_channels(n_channels: int) -> slice:
    """線形化するチャンネル: RGB(A)ならRGB、L/LAなら先頭1つ(アルファは生値)。"""
    return slice(0, 3) if n_channels >= 3 else slice(0, 1)


def _prepare_tiles(tiles: Mapping[int, Any]) -> dict:
    if not tiles:
        raise ChannelFormatError("no source tiles")
    out = {int(t): _to_float_image(a, int(t)) for t, a in tiles.items()}
    chans = {a.shape[2] for a in out.values()}
    if len(chans) != 1:
        raise ChannelFormatError(f"source tiles have different channel counts: {sorted(chans)}")
    return out


def _resolve_res(out_res, tile: int, old: Mapping[int, np.ndarray]) -> tuple[int, int, bool]:
    """出力タイルの (W, H, fallback_used)。dict に無いタイルは旧タイルの解像度を使う。"""
    spec = out_res
    if isinstance(out_res, Mapping):
        spec = out_res.get(tile)
    if spec is None:
        ref = old.get(tile)
        if ref is None:
            ref = old[sorted(old)[0]]
        return int(ref.shape[1]), int(ref.shape[0]), True
    if isinstance(spec, (int, np.integer)):
        w = h = int(spec)
    else:
        w, h = int(spec[0]), int(spec[1])
    if w <= 0 or h <= 0:
        raise ChannelFormatError(f"invalid output resolution for tile {tile}: {spec!r}")
    return w, h, False


def _sample_old_tiles(tiles: Mapping[int, np.ndarray], lin: dict, linearize: bool,
                      pts: np.ndarray, cent: np.ndarray, udim: bool, n_chan: int):
    """旧UV上の点で旧テクスチャを引く。(値 (N,C), 欠落 (N,))。

    各点を所属三角形のUV重心側へ 1e-6 寄せてからタイルを決める(タイル境界ちょうどの
    UVが隣のタイルを読まないように)。非UDIMは全点を単一タイル 1001 の小数部で読む。
    """
    q = pts + (cent - pts) * _NUDGE
    fu = np.floor(q[:, 0]).astype(np.int64)
    fv = np.floor(q[:, 1]).astype(np.int64)
    out = np.zeros((len(pts), n_chan), dtype=np.float32)
    missing = np.zeros(len(pts), dtype=bool)
    if udim:
        in_range = (fu >= 0) & (fu <= _UDIM_MAX_U) & (fv >= 0)
        key = np.where(in_range, 1001 + fu + 10 * fv, -1)
    else:
        key = np.full(len(pts), 1001, dtype=np.int64)
    for k in np.unique(key):
        sel = key == k
        if k < 0 or int(k) not in tiles:
            missing |= sel
            continue
        k = int(k)
        img = tiles[k]
        if linearize:
            if k not in lin:
                lin[k] = img.copy()
                ch = _color_channels(n_chan)
                lin[k][..., ch] = srgb_to_linear(img[..., ch])
            img = lin[k]
        out[sel] = _sample_bilinear(img, pts[sel, 0] - fu[sel], pts[sel, 1] - fv[sel])
    return out, missing


def convert_channel(old_tiles, baseline: Snapshot, current: Snapshot, ts_name: str,
                    decision: Decision, spec: ChannelSpec, out_res, *,
                    dilate_px: int = 16, normal_mode: Literal["uv", "off"] = "uv",
                    udim: bool | None = None) -> ConvertResult:
    """旧UVのテクスチャ(old_tiles)を現行UVのレイアウトへ変換する(方式F)。

    新UVの各テクセルについて、そのテクセルを含む三角形と重心座標を求め、基準スナップ
    ショットの同じ三角形・同じ重心座標の旧UV位置で旧テクスチャをバイリニアに引く。

    引数
        old_tiles: {タイル番号: 画像}。float32 (H,W,C)[0,1] を想定(uint8/uint16 も受理)。
        decision: classify の結果。"STOP" なら ConversionStopped を投げる。
        out_res: int、(W,H)、{タイル: int|(W,H)}、None のいずれか。dict に無いタイルや
            None は旧タイルの解像度を使う(stats["res_fallback_tiles"] に記録)。
        udim: True=UDIM、False=非UDIM(単一タイル 1001、UVは小数部で折り返す)。
            契約の署名に無い追加引数。None なら baseline.meta["udim"]、それも無ければ True。
        normal_mode: "uv" = UVヤコビアンによる接空間補正、"off" = 補正なし。

    戻り値の tiles は新UVが触れるタイルのみ。非多様体フェース・UV無しフェース・
    元テクスチャ無しの面は変換対象から除外し、stats に件数を載せる。
    """
    if decision.method not in ("F", "F_FLIPPED"):
        raise ConversionStopped(decision.reasons or [str(decision.method)],
                                f"decision is {decision.method}")
    if normal_mode not in ("uv", "off"):
        raise ValueError(f"normal_mode must be 'uv' or 'off': {normal_mode!r}")
    udim = _resolve_udim(udim, baseline)
    src = _prepare_tiles(old_tiles)
    n_chan = next(iter(src.values())).shape[2]
    if spec.kind == "normal" and n_chan < 3:
        raise ChannelFormatError("normal map needs at least 3 channels")
    linearize = spec.colorspace == "srgb" and spec.kind != "normal"

    ts = _collect_triangles(baseline, current, ts_name)
    stats: dict[str, Any] = dict(ts.stats)
    stats.update({"method": decision.method, "udim": udim, "normal_mode": normal_mode})
    codes: list[str] = []
    if ts.stats["tris_converted"] == 0:
        raise ConversionStopped(["no_convertible_faces"],
                                f"no convertible faces in texture set {ts_name!r}")

    layout = _Layout(ts.new_uv, udim)
    old_layout = _Layout(ts.old_uv, udim)
    face_of_tri = ts.face

    def face_ids(mask: np.ndarray, cap: int = 5000) -> list:
        return sorted(set(int(f) for f in face_of_tri[mask]))[:cap]

    stats["straddle_faces"] = int(len(set(face_of_tri[layout.straddle].tolist())))
    stats["straddle_face_ids"] = face_ids(layout.straddle)
    stats["old_straddle_faces"] = int(len(set(face_of_tri[old_layout.straddle].tolist())))
    stats["faces_out_of_udim_range"] = int(len(set(face_of_tri[layout.oor].tolist())))

    Q = None
    if spec.kind == "normal" and normal_mode == "uv":
        Q = _polar_Q(ts.old_uv, ts.new_uv)
    if spec.kind == "normal" and decision.method == "F_FLIPPED":
        codes.append("normals_flipped_approx")

    old_cent = ts.old_uv.mean(axis=1)
    lin_cache: dict[int, np.ndarray] = {}
    tiles_out: dict[int, np.ndarray] = {}
    cover_out: dict[int, np.ndarray] = {}
    overlap: dict[int, int] = {}
    missing_texels: dict[int, int] = {}
    empty_dropped: list[int] = []
    res_fallback: list[int] = []

    for key in layout.tile_keys():
        key = int(key)
        W, H, fb = _resolve_res(out_res, key, src)
        tri_map, bary, overlap_idx = layout.rasterize(key, W, H)
        mask = tri_map >= 0
        if not mask.any():
            empty_dropped.append(key)
            continue
        if fb:
            res_fallback.append(key)
        tri_ids = tri_map[mask].astype(np.int64)
        b = bary[mask].astype(np.float64)
        l2 = 1.0 - b[:, 0] - b[:, 1]
        uvt = ts.old_uv[tri_ids]
        p_old = b[:, 0:1] * uvt[:, 0] + b[:, 1:2] * uvt[:, 1] + l2[:, None] * uvt[:, 2]
        vals, miss = _sample_old_tiles(src, lin_cache, linearize, p_old, old_cent[tri_ids],
                                       udim, n_chan)
        if Q is not None:
            vals = _rotate_normals(vals, Q[tri_ids], spec.normal_green_down)
        if spec.kind == "normal" and n_chan >= 3:
            img = np.zeros((H, W, n_chan), dtype=np.float32)
            img[..., 0:2] = 0.5
            img[..., 2] = 1.0     # 未書き込みテクセルは平坦な法線にしておく
        else:
            img = np.zeros((H, W, n_chan), dtype=np.float32)
        img[mask] = vals
        cov = mask.copy()
        if miss.any():
            yy, xx = np.nonzero(mask)
            cov[yy[miss], xx[miss]] = False
        if linearize:
            ch = _color_channels(n_chan)
            img[..., ch] = linear_to_srgb(img[..., ch])
        if dilate_px and dilate_px > 0:
            img, _ = _dilate(img, cov, int(dilate_px))
        tiles_out[key] = img
        cover_out[key] = cov
        overlap[key] = int(len(overlap_idx))
        missing_texels[key] = int(miss.sum())

    stats["overlap_texels"] = overlap
    stats["overlap_texels_total"] = int(sum(overlap.values()))
    stats["missing_tile_texels"] = missing_texels
    stats["missing_tile_texels_total"] = int(sum(missing_texels.values()))
    stats["empty_tiles_dropped"] = empty_dropped
    stats["res_fallback_tiles"] = res_fallback
    if stats["overlap_texels_total"]:
        codes.append("uv_overlap")
    if stats["straddle_faces"]:
        codes.append("udim_straddle" if udim else "wrap_straddle")
    if stats["old_straddle_faces"]:
        codes.append("old_straddle")
    if stats["faces_out_of_udim_range"]:
        codes.append("out_of_udim_range")
    if stats["missing_tile_texels_total"]:
        codes.append("source_tile_missing")
    if stats["faces_no_source"]:
        codes.append("faces_no_source")
    if stats["faces_without_uv"]:
        codes.append("faces_without_uv")
    if stats["faces_non_manifold"]:
        codes.append("non_manifold_faces")
    if stats["tris_degenerate_new"]:
        codes.append("degenerate_new_uv")
    if res_fallback:
        codes.append("res_fallback")
    stats["warnings"] = codes
    if not tiles_out:
        raise ConversionStopped(["no_convertible_faces"],
                                "new UV layout covers no texels at the requested resolution")
    return ConvertResult(tiles_out, cover_out, stats)


# ----------------------------------------------------------------------------
# 3Dプローブ検証(CONTRACT §3.1)
# ----------------------------------------------------------------------------

@dataclass
class ProbeReport:
    """プローブ結果。誤差の単位は kind による(color/scalar: 値、normal: 度)。"""
    status: Literal["pass", "fail", "inconclusive"]
    n_total: int
    n_valid: int
    n_bad: int
    max_err: float
    mean_err: float
    bad_points: list = field(default_factory=list)
    excluded: dict = field(default_factory=dict)   # 除外理由 → 点数
    notes: list = field(default_factory=list)
    params: dict = field(default_factory=dict)     # 判定に使った設定(しきい値、窓方式 等)

    def to_dict(self) -> dict:
        return {"status": self.status, "n_total": self.n_total, "n_valid": self.n_valid,
                "n_bad": self.n_bad, "max_err": self.max_err, "mean_err": self.mean_err,
                "bad_points": list(self.bad_points), "excluded": dict(self.excluded),
                "notes": list(self.notes), "params": dict(self.params)}


def _ds_factor(h: int, w: int) -> int:
    return max(1, min(w, h) // 256)


def _box_down(img: np.ndarray, s: int) -> np.ndarray:
    if s <= 1:
        return img
    H, W, C = img.shape
    hd, wd = H // s, W // s
    return img[:hd * s, :wd * s].reshape(hd, s, wd, s, C).mean(axis=(1, 3), dtype=np.float32)


def _erode(mask: np.ndarray, k: int) -> np.ndarray:
    """8近傍でk回の収縮(画像の外は無効扱い)。"""
    m = mask
    H, W = m.shape
    for _ in range(k):
        pad = np.zeros((H + 2, W + 2), dtype=bool)
        pad[1:-1, 1:-1] = m
        out = m.copy()
        for dy in range(3):
            for dx in range(3):
                out &= pad[dy:dy + H, dx:dx + W]
        m = out
    return m


def _dilate_bool(mask: np.ndarray, k: int) -> np.ndarray:
    m = mask
    H, W = m.shape
    for _ in range(k):
        pad = np.zeros((H + 2, W + 2), dtype=bool)
        pad[1:-1, 1:-1] = m
        out = m.copy()
        for dy in range(3):
            for dx in range(3):
                out |= pad[dy:dy + H, dx:dx + W]
        m = out
    return m


def _box_mean_at(img: np.ndarray, cx: np.ndarray, cy: np.ndarray,
                 wx: np.ndarray, wy: np.ndarray, chunk: int = 256,
                 pre=None, post=None) -> np.ndarray:
    """連続ピクセル座標 (cx, cy)(テクセルの縁が整数)を中心とする wx x wy の箱型平均。

    窓は小数幅でよく、縁のテクセルは重なり面積で重み付けする(画像の外は端の値で延長)。
    格子に固定した縮小画像と違い、窓が常にプローブ点を中心にするので、旧/新で格子の位相が
    ずれても同じ領域を平均する。pre/post は平均の前後に掛ける変換(sRGB の線形化と再符号化)。
    (N,C) を返す。
    """
    H, W, C = img.shape
    out = np.empty((len(cx), C), dtype=np.float32)
    for a in range(0, len(cx), chunk):
        sl = slice(a, a + chunk)
        x_lo, x_hi = cx[sl] - wx[sl] / 2, cx[sl] + wx[sl] / 2
        y_lo, y_hi = cy[sl] - wy[sl] / 2, cy[sl] + wy[sl] / 2
        mx = int(np.ceil((x_hi - x_lo).max())) + 1
        my = int(np.ceil((y_hi - y_lo).max())) + 1
        ix = np.floor(x_lo).astype(np.int64)[:, None] + np.arange(mx)[None, :]
        iy = np.floor(y_lo).astype(np.int64)[:, None] + np.arange(my)[None, :]
        wxs = np.clip(np.minimum(ix + 1, x_hi[:, None]) - np.maximum(ix, x_lo[:, None]), 0, None)
        wys = np.clip(np.minimum(iy + 1, y_hi[:, None]) - np.maximum(iy, y_lo[:, None]), 0, None)
        block = img[np.clip(iy, 0, H - 1)[:, :, None], np.clip(ix, 0, W - 1)[:, None, :]]
        if pre is not None:
            block = pre(block)
        acc = np.einsum("nyxc,ny,nx->nc", block, wys.astype(np.float32), wxs.astype(np.float32))
        mean = acc / (wxs.sum(1) * wys.sum(1))[:, None].astype(np.float32)
        out[sl] = post(mean) if post is not None else mean
    return out


def _srgb_decode(a: np.ndarray) -> np.ndarray:
    """RGB(A)/L(A) の色チャンネルだけを線形化する(アルファは生値)。最後の軸がチャンネル。"""
    out = np.array(a, dtype=np.float32, copy=True)
    ch = _color_channels(out.shape[-1])
    out[..., ch] = srgb_to_linear(out[..., ch])
    return out


def _srgb_encode(a: np.ndarray) -> np.ndarray:
    out = np.array(a, dtype=np.float32, copy=True)
    ch = _color_channels(out.shape[-1])
    out[..., ch] = linear_to_srgb(out[..., ch])
    return out


class _ProbeSide:
    """プローブ側の1レイアウト(旧または新): 縮小画像と境界/重なりマスクをタイルごとに遅延生成する。"""

    def __init__(self, tiles: Mapping[int, np.ndarray], layout: _Layout, srgb: bool = False):
        self.tiles = tiles
        self.layout = layout
        self.srgb = srgb          # True: 窓平均を線形光で取り、符号化し直して返す
        self._ds: dict[int, tuple] = {}
        self._maps: dict[int, tuple] = {}

    def has(self, key: int) -> bool:
        return key in self.tiles

    def dims(self, key: int):
        """(s, W, H, 縮小後の幅, 縮小後の高さ)"""
        H, W = self.tiles[key].shape[:2]
        s = _ds_factor(H, W)
        return s, W, H, max(1, W // s), max(1, H // s)

    def ds(self, key: int):
        """(縮小画像, s, W, H): 画像の格子に固定した s x s の箱型縮小(契約どおりの方式)。"""
        if key not in self._ds:
            img = self.tiles[key]
            H, W = img.shape[:2]
            s = _ds_factor(H, W)
            small = _box_down(_srgb_decode(img) if self.srgb else img, s)
            self._ds[key] = (small, s, W, H)
        return self._ds[key]

    def maps(self, key: int):
        """(境界から離れているか(縮小後解像度), 重なりの近傍か)"""
        if key not in self._maps:
            _, _, _, wd, hd = self.dims(key)
            tri_map, _, overlap_idx = self.layout.rasterize(key, wd, hd)
            cover = tri_map >= 0
            ov = np.zeros(hd * wd, dtype=bool)
            ov[overlap_idx] = True
            self._maps[key] = (_erode(cover, PROBE_BORDER_TEXELS),
                               _dilate_bool(ov.reshape(hd, wd), 1))
        return self._maps[key]

    def sample(self, key: int, u_local: np.ndarray, v_local: np.ndarray) -> np.ndarray:
        """格子固定の縮小画像をバイリニアで引く(footprint="lattice")。"""
        small, s, W, H = self.ds(key)
        x = u_local * W / s - 0.5
        y = (1.0 - v_local) * H / s - 0.5
        out = _bilinear_xy(small, x, y)
        return _srgb_encode(out) if self.srgb else out

    def sample_centered(self, key: int, u_local: np.ndarray, v_local: np.ndarray,
                        size: np.ndarray) -> np.ndarray:
        """点を中心とする size テクセル四方の箱型平均(footprint="centered")。"""
        img = self.tiles[key]
        H, W = img.shape[:2]
        return _box_mean_at(img, u_local * W, (1.0 - v_local) * H, size, size,
                            pre=_srgb_decode if self.srgb else None,
                            post=_srgb_encode if self.srgb else None)

    def lookup(self, key: int, which: int, u_local: np.ndarray, v_local: np.ndarray) -> np.ndarray:
        """maps()[which] を点の位置で引く。"""
        _, _, _, wd, hd = self.dims(key)
        m = self.maps(key)[which]
        xi = np.clip(np.floor(u_local * wd).astype(np.int64), 0, wd - 1)
        yi = np.clip(np.floor((1.0 - v_local) * hd).astype(np.int64), 0, hd - 1)
        return m[yi, xi]


def _point_keys(pts: np.ndarray, udim: bool):
    fu = np.floor(pts[:, 0]).astype(np.int64)
    fv = np.floor(pts[:, 1]).astype(np.int64)
    if udim:
        ok = (fu >= 0) & (fu <= _UDIM_MAX_U) & (fv >= 0) & (fv <= _UDIM_MAX_V)
        key = np.where(ok, 1001 + fu + 10 * fv, -1)
    else:
        ok = np.ones(len(pts), dtype=bool)
        key = np.full(len(pts), 1001, dtype=np.int64)
    return key, pts[:, 0] - fu, pts[:, 1] - fv, ok


def _normal_error_deg(old: np.ndarray, new: np.ndarray, Q: np.ndarray, green_down: bool):
    a = old[:, :3].astype(np.float64) * 2.0 - 1.0
    if green_down:
        a[:, 1] = -a[:, 1]
    a = np.concatenate([np.einsum("nij,nj->ni", Q, a[:, :2]), a[:, 2:3]], axis=1)
    if green_down:
        a[:, 1] = -a[:, 1]
    b = new[:, :3].astype(np.float64) * 2.0 - 1.0
    na = np.linalg.norm(a, axis=1)
    nb = np.linalg.norm(b, axis=1)
    cosang = (a * b).sum(axis=1) / np.maximum(na * nb, 1e-12)
    err = np.degrees(np.arccos(np.clip(cosang, -1.0, 1.0)))
    err[(na < 1e-6) | (nb < 1e-6)] = 180.0     # ゼロベクトル = 法線として無効
    return err


def probe_verify(old_tiles, new_tiles, baseline: Snapshot, current: Snapshot, ts_name: str,
                 spec: ChannelSpec, *, n: int = 512, seed: int = 0,
                 udim: bool | None = None, normal_mode: Literal["uv", "off"] = "uv",
                 tau: float | None = None, max_angle_deg: float | None = None,
                 max_bad_fraction: float | None = None,
                 min_valid: int | None = None,
                 footprint: Literal["lattice", "centered"] = PROBE_FOOTPRINT,
                 match_density: bool = True) -> ProbeReport:
    """3Dプローブ検証: 変換結果が新UVの正しい位置に載っているかを、変換とは別経路で確認する。

    TSのフェース上から面積比例で n 点(固定シード)を選び、各点の旧UV位置と新UV位置を
    同じ三角形・同じ重心座標から求める。旧テクスチャを旧UVで、新テクスチャを新UVで読み、
    縮小(箱型, s = max(1, min(W,H)//256))してから比較する。sRGB のカラーは窓平均を線形光で
    取り、符号化し直した値で比較する(変換が線形光で補間するため)。箱型の窓は既定ではプローブ点を
    中心に置く(footprint="centered")。格子固定の縮小(footprint="lattice")は新旧の格子の位相差で
    細部テクスチャを誤検出するため既定にしていない。

    除外: 島の境界から2テクセル(縮小後)以内、UDIM境界またぎ/範囲外の面、新UVの重なり、
    旧タイルが無い点。新タイルが無い点は不合格として数える。
    判定: 誤差は color/scalar でチャンネル最大の絶対差(tau)、normal で角度(max_angle_deg)。
    n_bad / n_valid > max_bad_fraction なら fail。それ以外で n_valid < min_valid なら
    inconclusive(契約が曖昧な両条件の同時成立は fail を優先する)。
    """
    tau = PROBE_TAU if tau is None else tau
    max_angle = PROBE_NORMAL_DEG if max_angle_deg is None else max_angle_deg
    max_bad_frac = PROBE_MAX_BAD_FRACTION if max_bad_fraction is None else max_bad_fraction
    min_valid = PROBE_MIN_VALID if min_valid is None else min_valid
    if footprint not in ("lattice", "centered"):
        raise ValueError(f"footprint must be 'lattice' or 'centered': {footprint!r}")
    centered = footprint == "centered"
    udim = _resolve_udim(udim, baseline)
    is_normal = spec.kind == "normal"
    ts = _collect_triangles(baseline, current, ts_name)
    old_src = _prepare_tiles(old_tiles)
    new_src = _prepare_tiles(new_tiles)
    notes: list[str] = []
    excluded: dict[str, int] = {}

    def inconclusive(why: str) -> ProbeReport:
        return ProbeReport("inconclusive", int(n), 0, 0, 0.0, 0.0, [], excluded, notes + [why])

    if ts.stats["tris_converted"] == 0:
        return inconclusive("no_convertible_faces")

    # 1. 面積比例で点を選ぶ(3D面積。UV面積が0の三角形は候補から外す)
    p = ts.pos
    area3d = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    cand = ((area3d > 0) & (_tri_area_uv(ts.old_uv) > _DEGENERATE_UV_AREA)
            & (_tri_area_uv(ts.new_uv) > _DEGENERATE_UV_AREA))
    if not cand.any():
        return inconclusive("no_candidate_triangles")
    weights = np.where(cand, area3d, 0.0)
    cum = np.cumsum(weights)
    rng = np.random.default_rng(seed)
    r = rng.random(n) * cum[-1]
    ti = np.minimum(np.searchsorted(cum, r, side="right"), len(cum) - 1)
    u1 = rng.random(n)
    u2 = rng.random(n)
    s1 = np.sqrt(u1)
    bc = np.stack([1.0 - s1, s1 * (1.0 - u2), s1 * u2], axis=1)

    # 2. 旧UV位置と新UV位置
    old_pts = np.einsum("nk,nkj->nj", bc, ts.old_uv[ti])
    new_pts = np.einsum("nk,nkj->nj", bc, ts.new_uv[ti])

    old_layout = _Layout(ts.old_uv[cand], udim)
    new_layout = _Layout(ts.new_uv[cand], udim)
    # layout は候補のみで作るので、点の三角形を候補内インデックスに直す
    remap = np.cumsum(cand) - 1
    ti_c = remap[ti]
    linear_avg = spec.colorspace == "srgb" and not is_normal
    old_side = _ProbeSide(old_src, old_layout, linear_avg)
    new_side = _ProbeSide(new_src, new_layout, linear_avg)

    valid = np.ones(n, dtype=bool)
    force_bad = np.zeros(n, dtype=bool)

    def drop(mask: np.ndarray, why: str):
        mask = mask & valid
        if mask.any():
            excluded[why] = excluded.get(why, 0) + int(mask.sum())
            valid[mask] = False

    drop(old_layout.straddle[ti_c] | new_layout.straddle[ti_c], "tile_straddle")
    drop(old_layout.oor[ti_c] | new_layout.oor[ti_c], "udim_out_of_range")

    ko, uo, vo, ok_o = _point_keys(old_pts, udim)
    kn, un, vn, ok_n = _point_keys(new_pts, udim)
    drop(~ok_o | ~ok_n, "udim_out_of_range")
    has_o = np.zeros(n, dtype=bool)
    has_n = np.zeros(n, dtype=bool)
    for k in np.unique(ko[ok_o]):
        has_o[ko == k] = old_side.has(int(k))
    for k in np.unique(kn[ok_n]):
        has_n[kn == k] = new_side.has(int(k))
    drop(~has_o, "old_tile_missing")
    force_bad = valid & ~has_n          # 新タイルが無い = 必要なタイルを出力していない
    check = valid & has_n

    # 3. 境界・重なりの除外
    for side, keys, ul, vl, which, why in (
            (old_side, ko, uo, vo, 0, "island_border"),
            (new_side, kn, un, vn, 0, "island_border"),
            (new_side, kn, un, vn, 1, "uv_overlap")):
        for k in np.unique(keys[check & valid]):
            sel = check & valid & (keys == k)
            hit = side.lookup(int(k), which, ul[sel], vl[sel])
            bad = ~hit if which == 0 else hit
            idx = np.flatnonzero(sel)[bad]
            m = np.zeros(n, dtype=bool)
            m[idx] = True
            drop(m, why)
    check = check & valid

    # 4. 比較
    err = np.zeros(n, dtype=np.float64)
    q_all = None
    if is_normal and normal_mode == "uv":
        q_all = _polar_Q(ts.old_uv[cand], ts.new_uv[cand])
    area_old = _tri_area_uv(ts.old_uv[cand])
    area_new = _tri_area_uv(ts.new_uv[cand])
    old_vals: dict[int, np.ndarray] = {}
    new_vals: dict[int, np.ndarray] = {}
    for k in np.unique(ko[check]):
        for kn_ in np.unique(kn[check & (ko == k)]):
            sel = np.flatnonzero(check & (ko == k) & (kn == kn_))
            if centered:
                s_new = float(new_side.dims(int(kn_))[0])
                size_new = np.full(len(sel), s_new)
                size_old = np.full(len(sel), float(old_side.dims(int(k))[0]))
                if match_density:
                    # 新旧の窓が同じ表面の長さを覆うよう、テクセル密度の比で旧窓を換算する
                    _, Wo, Ho, _, _ = old_side.dims(int(k))
                    _, Wn, Hn, _, _ = new_side.dims(int(kn_))
                    rho = np.sqrt((area_new[ti_c[sel]] * Wn * Hn)
                                  / (area_old[ti_c[sel]] * Wo * Ho))
                    size_old = np.clip(s_new / rho, 1.0, 4.0 * s_new)
                a = old_side.sample_centered(int(k), uo[sel], vo[sel], size_old)
                b = new_side.sample_centered(int(kn_), un[sel], vn[sel], size_new)
            else:
                a = old_side.sample(int(k), uo[sel], vo[sel])
                b = new_side.sample(int(kn_), un[sel], vn[sel])
            if is_normal:
                if a.shape[1] < 3 or b.shape[1] < 3:
                    raise ChannelFormatError("normal map needs at least 3 channels")
                q = q_all[ti_c[sel]] if q_all is not None else np.tile(np.eye(2), (len(sel), 1, 1))
                err[sel] = _normal_error_deg(a, b, q, spec.normal_green_down)
            else:
                if a.shape[1] != b.shape[1]:
                    raise ChannelFormatError("old/new tiles have different channel counts")
                err[sel] = np.abs(a - b).max(axis=1)
            for j, i in enumerate(sel):
                old_vals[int(i)] = a[j]
                new_vals[int(i)] = b[j]
    limit = max_angle if is_normal else tau
    top = 180.0 if is_normal else 1.0
    err[force_bad] = top
    judged = check | force_bad
    bad = judged & (err > limit)
    n_valid = int(judged.sum())
    n_bad = int(bad.sum())
    max_err = float(err[judged].max()) if n_valid else 0.0
    mean_err = float(err[judged].mean()) if n_valid else 0.0

    bad_points = []
    for i in np.flatnonzero(bad)[np.argsort(-err[bad])][:32]:
        i = int(i)
        bad_points.append({
            "face": int(ts.face[cand][ti_c[i]]), "bary": [float(x) for x in bc[i]],
            "old_uv": [float(x) for x in old_pts[i]], "new_uv": [float(x) for x in new_pts[i]],
            "old_tile": int(ko[i]), "new_tile": int(kn[i]), "err": float(err[i]),
            "old": [float(x) for x in old_vals.get(i, [])],
            "new": [float(x) for x in new_vals.get(i, [])],
            "missing_new_tile": bool(force_bad[i]),
        })

    if n_valid == 0:
        status = "inconclusive"
    elif n_bad / n_valid > max_bad_frac:
        status = "fail"
    elif n_valid < min_valid:
        status = "inconclusive"
        notes.append(f"n_valid {n_valid} < {min_valid}")
    else:
        status = "pass"
    s_min = min([old_side.dims(k)[0] for k in old_src] + [new_side.dims(k)[0] for k in new_src])
    if s_min < 4 and (status == "fail" or n_bad):
        notes.append(f"low_resolution_probe: s={s_min} (<4); texel-level detail can raise false alarms")
    params = {"tau": tau, "max_angle_deg": max_angle, "max_bad_fraction": max_bad_frac,
              "min_valid": min_valid, "footprint": footprint, "match_density": bool(match_density),
              "seed": int(seed), "n": int(n), "linear_light_average": bool(linear_avg)}
    return ProbeReport(status, int(n), n_valid, n_bad, max_err, mean_err, bad_points,
                       excluded, notes, params)
