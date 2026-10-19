# -*- coding: utf-8 -*-
"""Maya シーンから UV 再マップ用のメッシュ配列を取り出す。

公開関数の戻り値のキー名と形は docs/uvremap/CONTRACT.md §2 に合わせる。
変換エンジン(uvremap_core)とは依存しない(キー名と形だけの契約)。

Maya の import はこのモジュールの読み込み時には行わない。
関数の中で初めて maya.api.OpenMaya / maya.cmds を読む。
numpy は Maya 非依存のためモジュール先頭で読む。
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import numpy as np

DEFAULT_SHADING_GROUP = "initialShadingGroup"

# collect_arrays の arrays に常に出すキー(ts_faces__<n> は ts_names に追随)
ARRAY_KEYS = (
    "face_vertex_counts",
    "corner_vid",
    "corner_pos",
    "corner_uv",
    "face_mesh",
    "tri_face",
    "tri_corner",
)

WARN_NONMANIFOLD = "nonmanifold_faces"
WARN_UNTRIANGULATED = "untriangulated_faces"
WARN_NO_UV = "no_uv_faces"
WARN_INSTANCE = "instance_mismatch"
WARN_DEFORMER = "deformer_positions_used"
WARN_DEFAULT_SG = "default_shading_group_ts"
WARN_TS_NAME_SG = "ts_name_matched_by_shading_group"


def is_default_sg(name):
    """initialShadingGroup か(名前空間付きの短名末尾も見る)。"""
    if name is None:
        return False
    text = str(name)
    return text == DEFAULT_SHADING_GROUP or text.endswith("|" + DEFAULT_SHADING_GROUP)


def ts_faces_key(index):
    return "ts_faces__{0}".format(int(index))


def group_faces_by_ts_name(sg_faces, sg_to_shader):
    """シェーディンググループ単位のフェースを TS名(シェーダー名)でまとめる。

    Args:
        sg_faces: {sg_name: フェースID列}
        sg_to_shader: {sg_name: surfaceShader ノード名 or None}

    Returns:
        {ts_name: {
            "face_ids": 昇順の和集合,
            "shading_group": 最初に見たグループ名,
            "shading_groups": 昇順のグループ名,
            "surface_shader": シェーダー名 or None,
            "is_default_sg": まとめた中に initialShadingGroup があるか,
            "ts_name_unresolved": True  # シェーダーが無いときだけ
        }}

    シェーダーが接続されていないグループは、グループ名をキーにし
    ts_name_unresolved を付ける。Painter 側の名前はここでは決めない。
    """
    grouped = {}
    sg_to_shader = sg_to_shader or {}
    for sg, faces in (sg_faces or {}).items():
        sg_name = str(sg)
        shader = sg_to_shader.get(sg, sg_to_shader.get(sg_name))
        if shader is None:
            ts_name = sg_name
            unresolved = True
        else:
            ts_name = str(shader)
            unresolved = False
        bucket = grouped.get(ts_name)
        if bucket is None:
            bucket = {
                "face_ids": [],
                "shading_group": sg_name,
                "shading_groups": [],
                "surface_shader": None if unresolved else ts_name,
                "is_default_sg": False,
            }
            grouped[ts_name] = bucket
        bucket["shading_groups"].append(sg_name)
        bucket["face_ids"].extend(int(i) for i in (faces or []))
        if is_default_sg(sg_name):
            bucket["is_default_sg"] = True
        if unresolved:
            if bucket.get("surface_shader") is None:
                bucket["ts_name_unresolved"] = True
        else:
            bucket["surface_shader"] = ts_name
            bucket.pop("ts_name_unresolved", None)

    out = {}
    for ts_name, bucket in grouped.items():
        entry = {
            "face_ids": sorted(set(bucket["face_ids"])),
            "shading_group": bucket["shading_group"],
            "shading_groups": sorted(set(bucket["shading_groups"])),
            "surface_shader": bucket["surface_shader"],
            "is_default_sg": bool(bucket["is_default_sg"]),
        }
        if bucket.get("ts_name_unresolved"):
            entry["ts_name_unresolved"] = True
        out[ts_name] = entry
    return out


def map_ts_names_to_faces(sg_faces, sg_to_shader, ts_names):
    """渡された TS名を、そのメッシュ内のフェースID列へ対応付ける。

    まずシェーダー名として探し、どれとも一致しなければグループ名として探す。
    グループ名で当たったときは WARN_TS_NAME_SG を付ける。
    当たった TS に initialShadingGroup のフェースが含まれるとき
    WARN_DEFAULT_SG を付ける。

    Returns:
        (ts_local_faces, warnings)
        ts_local_faces: {渡された名前: 昇順のフェースID}
    """
    sg_faces = dict(sg_faces or {})
    sg_to_shader = dict(sg_to_shader or {})
    grouped = group_faces_by_ts_name(sg_faces, sg_to_shader)
    shader_keys = set()
    for info in grouped.values():
        shader = info.get("surface_shader")
        if shader is not None and not info.get("ts_name_unresolved"):
            shader_keys.add(str(shader))

    ts_local = {}
    warnings = []
    includes_default = False
    names = [str(name) for name in (ts_names or [])]
    for name in names:
        if name in shader_keys:
            info = grouped.get(name) or {}
            faces = list(info.get("face_ids") or [])
            if info.get("is_default_sg"):
                includes_default = True
        elif name in sg_faces:
            faces = sorted(set(int(i) for i in (sg_faces.get(name) or [])))
            _append_unique(warnings, WARN_TS_NAME_SG)
            if is_default_sg(name):
                includes_default = True
        else:
            faces = []
        ts_local[name] = faces

    if includes_default:
        _append_unique(warnings, WARN_DEFAULT_SG)
    return ts_local, warnings


def ts_names_for_faces(mapped_sgs, sg_to_shader):
    """フェースごとのグループ名を TS名(シェーダー名)に読み替える。

    シェーダーが無いグループはグループ名のまま(未解決)。None は None。
    インスタンス間の不一致判定を TS名単位にするため。
    """
    sg_to_shader = sg_to_shader or {}
    out = []
    for sg in mapped_sgs:
        if sg is None:
            out.append(None)
            continue
        shader = sg_to_shader.get(sg, sg_to_shader.get(str(sg)))
        if shader is None:
            out.append(str(sg))
        else:
            out.append(str(shader))
    return out


def empty_arrays(ts_names):
    """長さ 0 の契約どおりの配列一式。"""
    arrays = {
        "face_vertex_counts": np.zeros((0,), dtype=np.int32),
        "corner_vid": np.zeros((0,), dtype=np.int32),
        "corner_pos": np.zeros((0, 3), dtype=np.float32),
        "corner_uv": np.zeros((0, 2), dtype=np.float32),
        "face_mesh": np.zeros((0,), dtype=np.int32),
        "tri_face": np.zeros((0,), dtype=np.int32),
        "tri_corner": np.zeros((0, 3), dtype=np.int8),
    }
    for index, _name in enumerate(ts_names):
        arrays[ts_faces_key(index)] = np.zeros((0,), dtype=np.int32)
    return arrays


def _append_unique(codes, code):
    if code not in codes:
        codes.append(code)


def map_triangles_to_local_corners(
    face_vertex_counts,
    corner_vid,
    triangle_counts,
    triangle_vertices,
):
    """getTriangles の頂点IDを、フェース内ローカルのコーナー番号へ変換する。

    同一フェース内で頂点IDが重複するフェースは非多様体として除外する。
    三角形が無い、または三角形の頂点がフェースに無いフェースも除外する。
    頂点ID列の並びは補正しない(法線反転で逆順になっていてもそのまま)。

    Returns:
        tri_face: int32 (T,)
        tri_corner: int8 (T, 3)
        extras: {"nonmanifold_faces": list[int], "untriangulated_faces": list[int]}
            フェースIDは入力配列内の番号(連結前ならメッシュ内ローカル)。
    """
    counts = np.asarray(face_vertex_counts, dtype=np.int32).reshape(-1)
    vids = np.asarray(corner_vid, dtype=np.int32).reshape(-1)
    tri_counts = np.asarray(triangle_counts, dtype=np.int32).reshape(-1)
    tri_vids = np.asarray(triangle_vertices, dtype=np.int32).reshape(-1)

    face_n = int(counts.size)
    if tri_counts.size < face_n:
        padded = np.zeros(face_n, dtype=np.int32)
        padded[: tri_counts.size] = tri_counts
        tri_counts = padded

    tri_face_rows = []
    tri_corner_rows = []
    nonmanifold = []
    untriangulated = []

    corner_off = 0
    tri_off = 0
    for face_id in range(face_n):
        n_corner = int(counts[face_id])
        face_vids = vids[corner_off:corner_off + n_corner]
        corner_off += n_corner

        n_tri = int(tri_counts[face_id])
        need = n_tri * 3
        face_tris = tri_vids[tri_off:tri_off + need]
        tri_off += need

        if n_corner <= 0:
            untriangulated.append(int(face_id))
            continue
        if int(np.unique(face_vids).size) != n_corner:
            nonmanifold.append(int(face_id))
            continue
        if n_tri <= 0 or int(face_tris.size) != need:
            untriangulated.append(int(face_id))
            continue

        lut = {int(vid): local for local, vid in enumerate(face_vids.tolist())}
        corners = np.empty((n_tri, 3), dtype=np.int8)
        ok = True
        for tri_i in range(n_tri):
            for k in range(3):
                vid = int(face_tris[tri_i * 3 + k])
                local = lut.get(vid)
                if local is None:
                    ok = False
                    break
                corners[tri_i, k] = local
            if not ok:
                break
        if not ok:
            untriangulated.append(int(face_id))
            continue
        for tri_i in range(n_tri):
            tri_face_rows.append(int(face_id))
            tri_corner_rows.append(corners[tri_i])

    tri_face = np.asarray(tri_face_rows, dtype=np.int32)
    if tri_corner_rows:
        tri_corner = np.stack(tri_corner_rows, axis=0).astype(np.int8, copy=False)
    else:
        tri_corner = np.zeros((0, 3), dtype=np.int8)
    return tri_face, tri_corner, {
        "nonmanifold_faces": nonmanifold,
        "untriangulated_faces": untriangulated,
    }


def assigned_uvs_to_corner_uv(face_vertex_counts, uv_counts, uv_ids, u_values, v_values):
    """getUVs / getAssignedUVs の結果をコーナー UV に載せる。

    割り当てが -1、件数が 0、またはコーナー数と一致しないフェースは
    そのコーナーを NaN にし、フェースIDを no_uv_faces に入れる。
    uv_counts が face_vertex_counts と一致する通常ケースは numpy で一括処理する。
    """
    counts = np.asarray(face_vertex_counts, dtype=np.int32).reshape(-1)
    uv_counts_arr = np.asarray(uv_counts, dtype=np.int32).reshape(-1)
    uv_ids_arr = np.asarray(uv_ids, dtype=np.int32).reshape(-1)
    u_arr = np.asarray(u_values, dtype=np.float32).reshape(-1)
    v_arr = np.asarray(v_values, dtype=np.float32).reshape(-1)

    face_n = int(counts.size)
    corner_n = int(counts.sum()) if face_n else 0
    corner_uv = np.full((corner_n, 2), np.nan, dtype=np.float32)
    n_u = int(u_arr.size)
    n_v = int(v_arr.size)
    n_uv_table = n_u if n_u <= n_v else n_v

    counts_match = (
        int(uv_counts_arr.size) == face_n
        and face_n > 0
        and np.array_equal(uv_counts_arr, counts)
        and int(uv_ids_arr.size) == corner_n
        and not np.any(counts == 0)
    )
    if counts_match:
        valid = (uv_ids_arr >= 0) & (uv_ids_arr < n_uv_table)
        if np.any(valid):
            ids = uv_ids_arr[valid]
            corner_uv[valid, 0] = u_arr[ids]
            corner_uv[valid, 1] = v_arr[ids]
        starts = np.zeros(face_n, dtype=np.int64)
        if face_n > 1:
            starts[1:] = np.cumsum(counts[:-1], dtype=np.int64)
        invalid = np.ones(corner_n, dtype=bool)
        invalid[valid] = False
        face_bad = np.logical_or.reduceat(invalid, starts)
        no_uv_faces = [int(i) for i in np.flatnonzero(face_bad)]
        return corner_uv, no_uv_faces

    no_uv_faces = []
    corner_off = 0
    uv_off = 0
    for face_id in range(face_n):
        n_corner = int(counts[face_id])
        n_uv = int(uv_counts_arr[face_id]) if face_id < int(uv_counts_arr.size) else 0
        ids = uv_ids_arr[uv_off:uv_off + n_uv]
        uv_off += n_uv
        bad = n_uv != n_corner or n_uv == 0
        n_fill = n_corner if n_corner < n_uv else n_uv
        for k in range(n_fill):
            uid = int(ids[k])
            if uid < 0 or uid >= n_uv_table:
                bad = True
                continue
            corner_uv[corner_off + k, 0] = u_arr[uid]
            corner_uv[corner_off + k, 1] = v_arr[uid]
        if n_fill < n_corner:
            bad = True
        if n_uv > n_corner:
            bad = True
        if bad:
            no_uv_faces.append(int(face_id))
        corner_off += n_corner
    return corner_uv, no_uv_faces


def concat_records(records, ts_names, uv_set=""):
    """メッシュ単位の配列を UUID 昇順で連結する。

    records の各要素:
        uuid, face_vertex_counts, corner_vid, corner_pos, corner_uv,
        tri_face (ローカルフェースID), tri_corner,
        ts_local_faces (TS名 → ローカルフェースID列),
        nonmanifold_faces / untriangulated_faces / no_uv_faces (ローカル),
        instance_mismatch / deformer_positions_used (真偽、省略可)
    """
    ts_names = [str(name) for name in ts_names]
    ordered = sorted(list(records or []), key=lambda rec: rec["uuid"])
    if not ordered:
        arrays = empty_arrays(ts_names)
        info = {
            "ts_names": ts_names,
            "mesh_uuids": [],
            "uv_set": uv_set,
            "bbox_min": [0.0, 0.0, 0.0],
            "bbox_max": [0.0, 0.0, 0.0],
            "warnings": [],
            "nonmanifold_faces": [],
            "untriangulated_faces": [],
            "no_uv_faces": [],
        }
        return arrays, info

    counts_parts = []
    vid_parts = []
    pos_parts = []
    uv_parts = []
    mesh_parts = []
    tri_face_parts = []
    tri_corner_parts = []
    ts_parts = {name: [] for name in ts_names}
    mesh_uuids = []
    nonmanifold = []
    untriangulated = []
    no_uv = []
    warnings = []
    face_offset = 0

    for mesh_index, rec in enumerate(ordered):
        counts = np.asarray(rec["face_vertex_counts"], dtype=np.int32).reshape(-1)
        vids = np.asarray(rec["corner_vid"], dtype=np.int32).reshape(-1)
        pos = np.asarray(rec["corner_pos"], dtype=np.float32)
        if pos.ndim != 2 or pos.shape[-1] != 3:
            pos = np.zeros((int(vids.size), 3), dtype=np.float32)
        else:
            pos = pos.reshape(-1, 3).astype(np.float32, copy=False)
        uvs = np.asarray(rec["corner_uv"], dtype=np.float32)
        if uvs.ndim != 2 or uvs.shape[-1] != 2:
            uvs = np.full((int(vids.size), 2), np.nan, dtype=np.float32)
        else:
            uvs = uvs.reshape(-1, 2).astype(np.float32, copy=False)
        tri_face = np.asarray(rec["tri_face"], dtype=np.int32).reshape(-1)
        tri_corner = np.asarray(rec["tri_corner"], dtype=np.int8)
        if tri_corner.ndim != 2 or tri_corner.shape[-1] != 3:
            tri_corner = np.zeros((int(tri_face.size), 3), dtype=np.int8)
        else:
            tri_corner = tri_corner.reshape(-1, 3).astype(np.int8, copy=False)

        face_n = int(counts.size)
        counts_parts.append(counts)
        vid_parts.append(vids)
        pos_parts.append(pos)
        uv_parts.append(uvs)
        mesh_parts.append(np.full(face_n, mesh_index, dtype=np.int32))
        tri_face_parts.append(tri_face + np.int32(face_offset))
        tri_corner_parts.append(tri_corner)
        mesh_uuids.append(str(rec["uuid"]))

        local_ts = rec.get("ts_local_faces") or {}
        for name in ts_names:
            local_ids = local_ts.get(name, [])
            ts_parts[name].extend(int(i) + face_offset for i in local_ids)

        nonmanifold.extend(int(i) + face_offset for i in (rec.get("nonmanifold_faces") or []))
        untriangulated.extend(
            int(i) + face_offset for i in (rec.get("untriangulated_faces") or [])
        )
        no_uv.extend(int(i) + face_offset for i in (rec.get("no_uv_faces") or []))

        if rec.get("nonmanifold_faces"):
            _append_unique(warnings, WARN_NONMANIFOLD)
        if rec.get("untriangulated_faces"):
            _append_unique(warnings, WARN_UNTRIANGULATED)
        if rec.get("no_uv_faces"):
            _append_unique(warnings, WARN_NO_UV)
        if rec.get("instance_mismatch"):
            _append_unique(warnings, WARN_INSTANCE)
        if rec.get("deformer_positions_used"):
            _append_unique(warnings, WARN_DEFORMER)
        for extra_warn in rec.get("ts_resolve_warnings") or []:
            _append_unique(warnings, extra_warn)

        face_offset += face_n

    arrays = {
        "face_vertex_counts": (
            np.concatenate(counts_parts).astype(np.int32, copy=False)
            if counts_parts else np.zeros((0,), dtype=np.int32)
        ),
        "corner_vid": (
            np.concatenate(vid_parts).astype(np.int32, copy=False)
            if vid_parts else np.zeros((0,), dtype=np.int32)
        ),
        "corner_pos": (
            np.concatenate(pos_parts, axis=0).astype(np.float32, copy=False)
            if pos_parts else np.zeros((0, 3), dtype=np.float32)
        ),
        "corner_uv": (
            np.concatenate(uv_parts, axis=0).astype(np.float32, copy=False)
            if uv_parts else np.zeros((0, 2), dtype=np.float32)
        ),
        "face_mesh": (
            np.concatenate(mesh_parts).astype(np.int32, copy=False)
            if mesh_parts else np.zeros((0,), dtype=np.int32)
        ),
        "tri_face": (
            np.concatenate(tri_face_parts).astype(np.int32, copy=False)
            if tri_face_parts else np.zeros((0,), dtype=np.int32)
        ),
        "tri_corner": (
            np.concatenate(tri_corner_parts, axis=0).astype(np.int8, copy=False)
            if tri_corner_parts else np.zeros((0, 3), dtype=np.int8)
        ),
    }
    for index, name in enumerate(ts_names):
        ids = ts_parts[name]
        arrays[ts_faces_key(index)] = np.asarray(ids, dtype=np.int32)

    pos = arrays["corner_pos"]
    if pos.shape[0] == 0:
        bbox_min = [0.0, 0.0, 0.0]
        bbox_max = [0.0, 0.0, 0.0]
    else:
        bbox_min = [float(x) for x in pos.min(axis=0).tolist()]
        bbox_max = [float(x) for x in pos.max(axis=0).tolist()]

    info = {
        "ts_names": ts_names,
        "mesh_uuids": mesh_uuids,
        "uv_set": uv_set,
        "bbox_min": bbox_min,
        "bbox_max": bbox_max,
        "warnings": warnings,
        "nonmanifold_faces": nonmanifold,
        "untriangulated_faces": untriangulated,
        "no_uv_faces": no_uv,
    }
    return arrays, info


def _maya():
    """OpenMaya 2.0 と cmds。モジュール import 時には呼ばない。"""
    import maya.api.OpenMaya as om  # type: ignore
    import maya.cmds as cmds  # type: ignore
    return om, cmds


def _as_int32(seq):
    n = len(seq)
    if n == 0:
        return np.zeros((0,), dtype=np.int32)
    try:
        arr = np.asarray(seq, dtype=np.int32)
        if arr.ndim == 1 and arr.size == n:
            return arr
    except (TypeError, ValueError):
        pass
    out = np.empty(n, dtype=np.int32)
    for i in range(n):
        out[i] = int(seq[i])
    return out


def _as_float32(seq):
    n = len(seq)
    if n == 0:
        return np.zeros((0,), dtype=np.float32)
    try:
        arr = np.asarray(seq, dtype=np.float32)
        if arr.ndim == 1 and arr.size == n:
            return arr
    except (TypeError, ValueError):
        pass
    out = np.empty(n, dtype=np.float32)
    for i in range(n):
        out[i] = float(seq[i])
    return out


def _points_to_nx3(points):
    n = len(points)
    if n == 0:
        return np.zeros((0, 3), dtype=np.float32)
    try:
        arr = np.asarray(points, dtype=np.float64)
        if arr.ndim == 2 and arr.shape[0] == n and arr.shape[1] >= 3:
            return arr[:, :3].astype(np.float32, copy=False)
        if arr.ndim == 1 and arr.size >= n * 3:
            return arr.reshape(-1, arr.size // n)[:n, :3].astype(np.float32, copy=False)
    except (TypeError, ValueError):
        pass
    out = np.empty((n, 3), dtype=np.float32)
    for i in range(n):
        p = points[i]
        out[i, 0] = float(p.x)
        out[i, 1] = float(p.y)
        out[i, 2] = float(p.z)
    return out


def _surface_shader_name(cmds, sg_name):
    """<SG>.surfaceShader の接続元ノード名。無ければ None。"""
    try:
        conns = cmds.listConnections(
            sg_name + ".surfaceShader", source=True, destination=False,
        ) or []
    except Exception:
        return None
    if not conns:
        return None
    return str(conns[0])


def _shader_face_names(om, fn, instance_number):
    """getConnectedShaders(instance) → (SG名のリスト, フェースごとの名前または None)。"""
    shaders, indices = fn.getConnectedShaders(int(instance_number))
    names = [om.MFnDependencyNode(obj).name() for obj in shaders]
    mapped = []
    raw = [int(i) for i in indices]
    for idx in raw:
        if idx < 0 or idx >= len(names):
            mapped.append(None)
        else:
            mapped.append(names[idx])
    return names, mapped, raw


def _mesh_has_geometry_filter(om, node):
    """上流に geometryFilter(デフォーマー基底)があるか。"""
    filt = getattr(om.MFn, "kGeometryFilt", None)
    if filt is None:
        return False
    try:
        it = om.MItDependencyGraph(
            node,
            filt,
            om.MItDependencyGraph.kUpstream,
            om.MItDependencyGraph.kDepthFirst,
            om.MItDependencyGraph.kNodeLevel,
        )
        return not it.isDone()
    except Exception:
        return False


def _mesh_has_deformer(om, cmds, dag):
    """デフォーマーの有無。Orig 頂点は使わず、検出時は警告用に True を返す。

    Orig(中間シェイプ)の頂点の取り方は、この作業では公式資料で確定していない。
    """
    if _mesh_has_geometry_filter(om, dag.node()):
        return True
    try:
        hist = cmds.listHistory(dag.fullPathName()) or []
    except Exception:
        return False
    for node in hist:
        try:
            types = cmds.nodeType(node, inherited=True) or []
        except Exception:
            continue
        if "geometryFilter" in types:
            return True
    return False


def _iter_unique_meshes(om):
    """中間オブジェクトを除いたメッシュを UUID 昇順で返す。"""
    rows = []
    it = om.MItDependencyNodes(om.MFn.kMesh)
    while not it.isDone():
        node = it.thisNode()
        fn_dag = om.MFnDagNode(node)
        if fn_dag.isIntermediateObject:
            it.next()
            continue
        dag = fn_dag.getPath()
        uuid = om.MFnDependencyNode(node).uuid().asString()
        rows.append((uuid, dag))
        it.next()
    rows.sort(key=lambda row: row[0])
    return rows


def _resolve_mesh(om, shape_path):
    sel = om.MSelectionList()
    sel.add(shape_path)
    dag = sel.getDagPath(0)
    if not dag.hasFn(om.MFn.kMesh):
        dag.extendToShape()
    if not dag.hasFn(om.MFn.kMesh):
        raise ValueError("mesh ではないパス")
    uuid = om.MFnDependencyNode(dag.node()).uuid().asString()
    return dag, uuid


def _instance_shader_mismatch(om, cmds, dag, mapped0):
    """全インスタンスの TS名(シェーダー名)列をインスタンス0と比較する。"""
    fn_dag = om.MFnDagNode(dag.node())
    try:
        n_inst = int(fn_dag.instanceCount(False))
    except Exception:
        n_inst = 1
    if n_inst <= 1:
        return False
    try:
        paths = fn_dag.getAllPaths()
    except Exception:
        paths = [dag]
    path_by_inst = {}
    for path in paths:
        try:
            path_by_inst[int(path.instanceNumber())] = path
        except Exception:
            continue

    def _ts_row(mapped):
        table = {}
        for sg in mapped:
            if sg is None or sg in table:
                continue
            table[sg] = _surface_shader_name(cmds, sg)
        return ts_names_for_faces(mapped, table)

    ts0 = _ts_row(mapped0)
    for inst in range(n_inst):
        if inst == 0:
            continue
        path = path_by_inst.get(inst, dag)
        try:
            other_fn = om.MFnMesh(path)
            _names, mapped, _raw = _shader_face_names(om, other_fn, inst)
        except Exception:
            return True
        if _ts_row(mapped) != ts0:
            return True
    return False


def _read_mesh_record(om, cmds, dag, uuid, uv_set):
    fn = om.MFnMesh(dag)
    uv_names = list(fn.getUVSetNames())
    if uv_set and uv_set not in uv_names:
        raise ValueError("指定の UV セットが無い")

    vtx_counts, vtx_ids = fn.getVertices()
    counts = _as_int32(vtx_counts)
    vids = _as_int32(vtx_ids)

    points = fn.getPoints(om.MSpace.kObject)
    pts = _points_to_nx3(points)
    if pts.shape[0] == 0:
        corner_pos = np.zeros((int(vids.size), 3), dtype=np.float32)
    else:
        safe = np.clip(vids, 0, pts.shape[0] - 1)
        corner_pos = pts[safe].astype(np.float32, copy=False)
        if vids.size:
            bad = (vids < 0) | (vids >= pts.shape[0])
            if np.any(bad):
                corner_pos = corner_pos.copy()
                corner_pos[bad] = 0.0

    if not uv_names:
        corner_uv = np.full((int(vids.size), 2), np.nan, dtype=np.float32)
        no_uv_faces = [int(i) for i in range(int(counts.size))]
    else:
        u_values, v_values = fn.getUVs(uv_set)
        uv_counts, uv_ids = fn.getAssignedUVs(uv_set)
        corner_uv, no_uv_faces = assigned_uvs_to_corner_uv(
            counts,
            _as_int32(uv_counts),
            _as_int32(uv_ids),
            _as_float32(u_values),
            _as_float32(v_values),
        )

    tri_counts, tri_verts = fn.getTriangles()
    tri_face, tri_corner, extras = map_triangles_to_local_corners(
        counts, vids, _as_int32(tri_counts), _as_int32(tri_verts),
    )

    _sg_names, mapped0, _raw = _shader_face_names(om, fn, 0)
    sg_faces = {}
    for face_id, sg in enumerate(mapped0):
        if sg is None:
            continue
        sg_faces.setdefault(sg, []).append(int(face_id))
    sg_to_shader = {}
    for sg in sg_faces:
        sg_to_shader[sg] = _surface_shader_name(cmds, sg)

    mismatch = _instance_shader_mismatch(om, cmds, dag, mapped0)
    deformer = _mesh_has_deformer(om, cmds, dag)

    return {
        "uuid": uuid,
        "shape": dag.fullPathName(),
        "face_vertex_counts": counts,
        "corner_vid": vids,
        "corner_pos": corner_pos,
        "corner_uv": corner_uv,
        "tri_face": tri_face,
        "tri_corner": tri_corner,
        "sg_faces": sg_faces,
        "sg_to_shader": sg_to_shader,
        "ts_local_faces": {},
        "nonmanifold_faces": extras["nonmanifold_faces"],
        "untriangulated_faces": extras["untriangulated_faces"],
        "no_uv_faces": no_uv_faces,
        "instance_mismatch": mismatch,
        "deformer_positions_used": deformer,
    }


def uv_fingerprints(shape_paths, uv_set=None):
    """各シェイプの UV 指紋。三角形分割と面の対応は計算しない。

    ``collect_arrays`` と同じく ``MFnMesh.getUVs`` で U・V を取り、
    ``uvremap_follow.uv_fingerprint`` で ``{シェイプのフルパス: (crc, UV数)}`` を作る。
    取れないシェイプは結果に入れない。例外はシェイプ単位で握る。
    Maya 自体を import できないときは例外を外へ出す(呼び出し側が変換を止めない)。

    戻り値は ``(fingerprints, elapsed_s)``。``elapsed_s`` はこの呼び出し全体の秒。
    """
    import time

    from uvremap_follow import uv_fingerprint

    started = time.perf_counter()
    fingerprints = {}
    om, _cmds = _maya()
    chosen_fixed = None if uv_set is None else str(uv_set)
    for path in list(shape_paths or []):
        try:
            dag, _uuid = _resolve_mesh(om, path)
            fn = om.MFnMesh(dag)
            uv_names = list(fn.getUVSetNames())
            if chosen_fixed is None:
                if not uv_names:
                    continue
                chosen = fn.currentUVSetName()
            else:
                if chosen_fixed not in uv_names:
                    continue
                chosen = chosen_fixed
            u_values, v_values = fn.getUVs(chosen)
            fingerprints[dag.fullPathName()] = uv_fingerprint(
                _as_float32(u_values), _as_float32(v_values),
            )
        except Exception:
            continue
    return fingerprints, time.perf_counter() - started


def list_texture_sets():
    """シーン内メッシュの TS名(シェーダー名) → フェース集合。

    戻り値:
        {ts_name: [{"shape", "uuid", "face_ids", "is_default_sg",
                    "surface_shader", "shading_group", "shading_groups"}, ...]}

    インスタンス0の getConnectedShaders(0) を使う。
    initialShadingGroup は除外しない。同じシェーダーを使う複数グループは
    同じ TS にまとめる。シェーダーが無いグループはグループ名をキーにし
    ts_name_unresolved を付ける。
    """
    om, cmds = _maya()
    out = {}
    for uuid, dag in _iter_unique_meshes(om):
        fn = om.MFnMesh(dag)
        _names, mapped, _raw = _shader_face_names(om, fn, 0)
        sg_faces = {}
        for face_id, sg in enumerate(mapped):
            if sg is None:
                continue
            sg_faces.setdefault(sg, []).append(int(face_id))
        sg_to_shader = {}
        for sg in sg_faces:
            sg_to_shader[sg] = _surface_shader_name(cmds, sg)
        grouped = group_faces_by_ts_name(sg_faces, sg_to_shader)
        shape = dag.fullPathName()
        for ts_name, info in grouped.items():
            entry = {
                "shape": shape,
                "uuid": uuid,
                "face_ids": list(info["face_ids"]),
                "is_default_sg": bool(info["is_default_sg"]),
                "surface_shader": info["surface_shader"],
                "shading_group": info["shading_group"],
                "shading_groups": list(info["shading_groups"]),
            }
            if info.get("ts_name_unresolved"):
                entry["ts_name_unresolved"] = True
            out.setdefault(ts_name, []).append(entry)
    return out


def collect_arrays(shape_paths, ts_names, uv_set=None):
    """複数メッシュを UUID 昇順に連結し、契約 §2 の配列を返す。

    arrays のキー:
        face_vertex_counts, corner_vid, corner_pos, corner_uv, face_mesh,
        tri_face, tri_corner, ts_faces__<n>
    info のキー:
        ts_names, mesh_uuids, uv_set, bbox_min, bbox_max, warnings
        (加えて除外フェースID: nonmanifold_faces / untriangulated_faces / no_uv_faces)

    フィンガープリントと UV ハッシュは計算しない。
    頂点ID列は Maya が返した並びのまま(法線反転の補正はしない)。
    ts_names は TS名(シェーダー名)として扱う。どれとも一致せず
    グループ名と一致する要素は、そのグループのフェースを使い
    ts_name_matched_by_shading_group を warnings に入れる。
    initialShadingGroup のフェースが TS に含まれるときは
    default_shading_group_ts を warnings に入れる。
    """
    om, cmds = _maya()
    ts_names = [str(name) for name in (ts_names or [])]
    paths = list(shape_paths or [])
    if not paths:
        arrays, info = concat_records([], ts_names, uv_set="" if uv_set is None else str(uv_set))
        return arrays, info

    resolved = []
    seen = set()
    for path in paths:
        dag, uuid = _resolve_mesh(om, path)
        if uuid in seen:
            continue
        seen.add(uuid)
        resolved.append((uuid, dag))
    resolved.sort(key=lambda row: row[0])

    if uv_set is None:
        first_fn = om.MFnMesh(resolved[0][1])
        resolved_uv = first_fn.currentUVSetName()
    else:
        resolved_uv = str(uv_set)

    records = []
    for uuid, dag in resolved:
        rec = _read_mesh_record(om, cmds, dag, uuid, resolved_uv)
        ts_local, ts_warnings = map_ts_names_to_faces(
            rec.get("sg_faces") or {},
            rec.get("sg_to_shader") or {},
            ts_names,
        )
        rec["ts_local_faces"] = ts_local
        rec["ts_resolve_warnings"] = ts_warnings
        records.append(rec)
    return concat_records(records, ts_names, uv_set=resolved_uv)


# 2026.10.19(RB-c): リベース用メッシュ書き出し
#
# Painter が読み直すメッシュを、基準記録時と同じシェイプから FBX で書く。
# シーンは変更しない(選択だけ退避して戻す)。一時ファイルへ書いてから
# os.replace で確定する。meshes/ の掃除、FBX のバージョン・単位・軸の指定、
# 新基準候補や依頼ファイルの作成はここでは行わない。

REBASE_MESHES_DIRNAME = "meshes"
REBASE_FBX_EXT = ".fbx"
# 要実機確認: 第一案は "suffix" (<baseline_id>.fbx.tmp)。
# MEL の -f が拡張子を要求する、または .fbx を付け足す場合は
# "infix" (<baseline_id>.tmp.fbx) にこの定数だけを切り替える。
REBASE_FBX_TMP_STYLE = "suffix"
REBASE_FBX_CHUNK = 1024 * 1024
REBASE_FBX_PLUGIN = "fbxmaya"
MSG_FBX_EXPORT_FAILED = "メッシュの書き出しに失敗しました"

_BASELINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_TMP_STYLES = ("suffix", "infix")


class RebaseExportError(Exception):
    """リベース用メッシュ書き出しの失敗。

    code は依頼ファイルの失敗理由(既定 ``export_failed``)。
    メッセージは利用者向けの短い文で、絶対パスを含めない。
    元の例外は ``__cause__`` に残す。
    """

    def __init__(self, message=None, code="export_failed"):
        self.code = code or "export_failed"
        super().__init__(message or MSG_FBX_EXPORT_FAILED)


def rebase_fbx_paths(meshes_dir, baseline_id, tmp_style=None):
    """最終 FBX と一時ファイルのパスを返す。

    Args:
        meshes_dir: ``meshes`` ディレクトリ(この関数は
            ``REBASE_MESHES_DIRNAME`` を足さない)。
        baseline_id: 英数字で始まり、英数字と ``-`` ``_`` のみ、80 文字以内。
        tmp_style: ``"suffix"`` または ``"infix"``。省略時は
            ``REBASE_FBX_TMP_STYLE``。

    Returns:
        (最終 Path, 一時 Path)

    Raises:
        ValueError: baseline_id または tmp_style が不正。
    """
    if not isinstance(baseline_id, str) or _BASELINE_ID_RE.fullmatch(baseline_id) is None:
        raise ValueError("基準 ID が不正です")
    style = REBASE_FBX_TMP_STYLE if tmp_style is None else tmp_style
    if style not in _TMP_STYLES:
        raise ValueError("一時ファイルの流儀が不明です")
    root = Path(meshes_dir)
    final_path = root / (baseline_id + REBASE_FBX_EXT)
    if style == "suffix":
        tmp_path = root / (baseline_id + REBASE_FBX_EXT + ".tmp")
    else:
        tmp_path = root / (baseline_id + ".tmp" + REBASE_FBX_EXT)
    return final_path, tmp_path


def escape_mel_string(text):
    """MEL のダブルクォート文字列用にエスケープする。

    先に ``\\`` を ``/`` へ置換し、その後に残った ``\\`` を ``\\\\``、
    ``"`` を ``\\"`` にする。パス区切りの変換は実機の ``FBXExport -f``
    が受け取る形に合わせた決め打ち。
    """
    # 要実機確認: FBXExport -f のパスは / 区切り・引用符付きで渡す。
    normalized = str(text).replace("\\", "/")
    escaped = []
    for ch in normalized:
        if ch == "\\":
            escaped.append("\\\\")
        elif ch == '"':
            escaped.append('\\"')
        else:
            escaped.append(ch)
    return "".join(escaped)


def build_fbx_export_commands(tmp_path, *, triangulate=False):
    """FBX 書き出し MEL を、1 コマンド 1 要素で返す。

    コマンド名と引数は決め打ち。``triangulate`` は bool のみ。
    既定は False(四角形を保ち、基準スナップショットとの面の対応を崩さない)。

    要実機確認: 大きなメッシュの所要時間と、Maya が固まる時間は未計測(合否ではない)。
    要実機確認: Painter が四角形を保ったまま読むかは RB-0/RB-e。この PR の合否ではない。
    FBX のバージョン・単位・軸は契約に無いので送らない。
    """
    if not isinstance(triangulate, bool):
        raise ValueError("triangulate は bool だけを受けます")
    # 要実機確認: FBXExportTriangulate -v false で四角形が残り、true で三角形化されること。
    tri_flag = "true" if triangulate else "false"
    exported = escape_mel_string(os.fspath(tmp_path))
    # 要実機確認: 各コマンドが存在し、名前・-v・小文字の true/false がこのとおりであること。
    # 1. FBXResetExport。利用者の FBX 書き出し設定を初期化する副作用がある
    #    (画面の注意は RB-d で決める)。
    # 2-4. Maya 2027 で確認済み: InAscii / EmbeddedTextures / AnimationOnly は -v false。
    # 5. FBXExportInputConnections -v false(出力に履歴ノードが入らないこと)
    # 6. FBXExportCameras -v false(カメラが出ないこと)
    # 7. FBXExportLights -v false(ライトが出ないこと)
    # 8. FBXExportSmoothingGroups -v true(再読み込みでスムージングが保たれること)
    # 9. FBXExportTriangulate
    # 10. FBXExport -f "<一時パス>" -s(選択した親トランスフォームが出ること)
    return [
        "FBXResetExport;",
        "FBXExportInAscii -v false;",
        "FBXExportEmbeddedTextures -v false;",
        "FBXExportAnimationOnly -v false;",
        "FBXExportInputConnections -v false;",
        "FBXExportCameras -v false;",
        "FBXExportLights -v false;",
        "FBXExportSmoothingGroups -v true;",
        "FBXExportTriangulate -v {0};".format(tri_flag),
        'FBXExport -f "{0}" -s;'.format(exported),
    ]


def export_rebase_mesh(
    shape_paths,
    meshes_dir,
    baseline_id,
    *,
    triangulate=False,
    mel_eval=None,
    cmds=None,
    replace_fn=None,
    remove_fn=None,
    tmp_style=None,
):
    """基準シェイプを FBX にして ``meshes/<baseline_id>.fbx`` へ確定する。

    戻り値は依頼の ``mesh`` にそのまま入る素の dict
    ``{"path", "size", "sha1"}``。``path`` は絶対パスで、区切りは OS 既定。

    ``mel_eval`` / ``cmds`` / ``replace_fn`` / ``remove_fn`` を省略したときは
    ``maya.mel.eval`` / ``maya.cmds`` / ``os.replace`` / ``os.remove``。
    Maya が無い環境で既定の mel または cmds を使うと ``RebaseExportError``。

    選ぶのはシェイプではなく親トランスフォーム。フルパスに親が含まれるときは
    その親を使い、シェイプ名しか無いときは ``listRelatives`` で親を辿る。

    失敗時は一時ファイルを残さない。``replace_fn`` が失敗したときは最終ファイルを
    消さない(この呼び出しが作ったものではない)。replace 成功後にサイズが
    一致しないときだけ、この呼び出しが置いた最終ファイルを消す。呼び出し前から
    あった最終ファイルは上書きしない。``KeyboardInterrupt`` などの
    ``BaseException`` は包まず、一時ファイルを消してから再送出する。

    呼び出し側は ``REBASE_MESHES_DIRNAME`` を付けたディレクトリを ``meshes_dir``
    に渡す。ここから古い FBX は消さない。
    """
    shapes = _require_shape_paths(shape_paths)
    try:
        final_path, tmp_path = rebase_fbx_paths(
            meshes_dir, baseline_id, tmp_style=tmp_style,
        )
    except ValueError as exc:
        raise RebaseExportError(str(exc)) from exc
    except Exception as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc

    if replace_fn is None:
        replace_fn = os.replace
    if remove_fn is None:
        remove_fn = os.remove

    try:
        Path(meshes_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc

    try:
        _unlink(tmp_path, remove_fn)
    except Exception as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    if os.path.lexists(os.fspath(final_path)):
        raise RebaseExportError("同じ名前のメッシュが既にあります")

    try:
        cmds_api = _default_cmds() if cmds is None else cmds
        mel = _default_mel_eval() if mel_eval is None else mel_eval
    except RebaseExportError:
        raise
    except Exception as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc

    try:
        _ensure_fbx_plugin(cmds_api)
        _ensure_shapes_exist(cmds_api, shapes)
        parents = _parent_transforms(cmds_api, shapes)
    except RebaseExportError:
        raise
    except Exception as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc

    selection_saved = False
    previous = []
    try:
        previous = list(cmds_api.ls(selection=True, long=True) or [])
        selection_saved = True
        # Maya 2027 で確認済み: シェイプではなく親トランスフォームを選んで書き出す。
        cmds_api.select(list(parents), replace=True)
        for command in build_fbx_export_commands(tmp_path, triangulate=triangulate):
            mel(command)
        return _publish_rebase_fbx(tmp_path, final_path, replace_fn, remove_fn)
    except RebaseExportError:
        _unlink_quiet(tmp_path, remove_fn)
        raise
    except Exception as exc:
        _unlink_quiet(tmp_path, remove_fn)
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    except BaseException:
        _unlink_quiet(tmp_path, remove_fn)
        raise
    finally:
        if selection_saved:
            _restore_selection(cmds_api, previous)


def _require_shape_paths(shape_paths):
    """文字列のシェイプパス列。空・非文字列は RebaseExportError。"""
    if shape_paths is None or isinstance(shape_paths, (str, bytes)):
        raise RebaseExportError("書き出し対象のシェイプがありません")
    try:
        shapes = list(shape_paths)
    except TypeError as exc:
        raise RebaseExportError("書き出し対象のシェイプがありません") from exc
    if not shapes:
        raise RebaseExportError("書き出し対象のシェイプがありません")
    for item in shapes:
        if not isinstance(item, str):
            raise RebaseExportError("書き出し対象のシェイプが不正です")
    return shapes


def _default_cmds():
    try:
        import maya.cmds as cmds  # type: ignore
    except Exception as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    return cmds


def _default_mel_eval():
    try:
        import maya.mel as mel  # type: ignore
    except Exception as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    return mel.eval


def _ensure_fbx_plugin(cmds_api):
    """未ロードなら fbxmaya を読む。契約に無い追加。"""
    # 要実機確認: プラグインを外した状態でも loadPlugin で読めること。
    try:
        loaded = cmds_api.pluginInfo(REBASE_FBX_PLUGIN, query=True, loaded=True)
    except Exception as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    if loaded:
        return
    try:
        cmds_api.loadPlugin(REBASE_FBX_PLUGIN, quiet=True)
    except Exception as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc


def _parent_in_dag_path(shape_path):
    """フルパスに親が書いてあるとき、そのトランスフォームのパスを返す。"""
    if "|" not in shape_path:
        return None
    parent, _shape_name = shape_path.rsplit("|", 1)
    if not parent:
        return None
    return parent


def _parent_transform(cmds_api, shape_path):
    """書き出しに選ぶ親トランスフォーム。

    フルパスならパス上の親を使う。シェイプ名しか無いときは
    ``listRelatives`` で親を 1 つ辿る。複数ある、または無いときは失敗。
    """
    embedded = _parent_in_dag_path(shape_path)
    if embedded:
        return embedded
    try:
        found = cmds_api.listRelatives(shape_path, parent=True, fullPath=True)
    except Exception as exc:
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    if isinstance(found, str):
        parents = [found]
    elif found:
        try:
            parents = list(found)
        except TypeError as exc:
            raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    else:
        parents = []
    if len(parents) != 1 or not isinstance(parents[0], str) or not parents[0]:
        raise RebaseExportError("書き出し対象の親が見つかりません")
    return parents[0]


def _parent_transforms(cmds_api, shapes):
    """シェイプごとに親を決め、同じ親は 1 回だけ選ぶ。"""
    chosen = []
    seen = set()
    for shape_path in shapes:
        parent = _parent_transform(cmds_api, shape_path)
        if parent in seen:
            continue
        seen.add(parent)
        chosen.append(parent)
    if not chosen:
        raise RebaseExportError("書き出し対象の親が見つかりません")
    return chosen


def _ensure_shapes_exist(cmds_api, shapes):
    for path in shapes:
        try:
            exists = cmds_api.objExists(path)
        except Exception as exc:
            raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
        if not exists:
            raise RebaseExportError("書き出し対象のシェイプが見つかりません")


def _restore_selection(cmds_api, previous):
    """選択を戻す。失敗しても書き出し結果は隠さない。"""
    # 要実機確認: 書き出し後に元の選択へ戻ること。
    try:
        if previous:
            cmds_api.select(previous, replace=True)
        else:
            cmds_api.select(clear=True)
    except Exception:
        return


def _unlink(path, remove_fn):
    target = os.fspath(path)
    if not os.path.lexists(target):
        return
    try:
        remove_fn(target)
    except FileNotFoundError:
        return


def _unlink_quiet(path, remove_fn):
    try:
        _unlink(path, remove_fn)
    except Exception:
        return


def _sha1_stream(path):
    """1 チャンクずつ sha1 を計算する。戻り値は (hex, 読んだバイト数)。"""
    chunk_size = REBASE_FBX_CHUNK
    digest = hashlib.sha1()
    total = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            total += len(block)
            digest.update(block)
    return digest.hexdigest(), total


def _publish_rebase_fbx(tmp_path, final_path, replace_fn, remove_fn):
    """一時ファイルを検査して確定する。

    ``replace_fn`` が失敗したときは一時ファイルだけを消す。最終ファイルは
    この呼び出しが作ったものではないので消さない。replace が成功したあとで
    サイズが違えば、置いた最終ファイルを消す。
    """
    tmp_s = os.fspath(tmp_path)
    final_s = os.fspath(final_path)
    if not os.path.lexists(tmp_s):
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED)
    try:
        tmp_size = os.stat(tmp_s).st_size
    except OSError as exc:
        _unlink_quiet(tmp_path, remove_fn)
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    if tmp_size <= 0:
        _unlink_quiet(tmp_path, remove_fn)
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED)

    try:
        digest, hashed_size = _sha1_stream(tmp_path)
    except Exception as exc:
        _unlink_quiet(tmp_path, remove_fn)
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    if hashed_size != tmp_size or hashed_size <= 0:
        _unlink_quiet(tmp_path, remove_fn)
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED)

    if os.path.lexists(final_s):
        _unlink_quiet(tmp_path, remove_fn)
        raise RebaseExportError("同じ名前のメッシュが既にあります")

    try:
        # 要実機確認: Windows でウイルス対策や同期ソフトが一時ファイルを
        # 掴むと os.replace が失敗することがある。失敗時は一時ファイルだけを
        # 消す。最終ファイルは消さない(再試行はここでは足さない)。
        replace_fn(tmp_s, final_s)
    except Exception as exc:
        _unlink_quiet(tmp_path, remove_fn)
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc

    try:
        final_size = os.stat(final_s).st_size
    except OSError as exc:
        _unlink_quiet(final_path, remove_fn)
        _unlink_quiet(tmp_path, remove_fn)
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED) from exc
    if final_size != tmp_size:
        _unlink_quiet(final_path, remove_fn)
        _unlink_quiet(tmp_path, remove_fn)
        raise RebaseExportError(MSG_FBX_EXPORT_FAILED)

    if os.path.lexists(tmp_s):
        _unlink_quiet(tmp_path, remove_fn)

    return {
        "path": os.path.abspath(final_s),
        "size": int(final_size),
        "sha1": digest,
    }


# ---------------------------------------------------------------------------
# 2026.10.19(RB-d): リベース用 旧 UV セット(uvOld)
#
# Painter は 2 番目の UV セットを名前ではなく番号 1 で参照する。
# そのため uvOld は、元の UV セットがちょうど 1 つのときにだけ一時的に作り、
# 書き出しの成否にかかわらず消す。シーンの UV セットの並べ替えはしない。
# 要実機確認: FBX 上の並び、setUVs / assignUVs の見た目、Undo、履歴ノード。
# ---------------------------------------------------------------------------

UVOLD_BASE_NAME = "uvOld"

UVOLD_NO_BASELINE = "uvold_no_baseline"
UVOLD_BASELINE_UNREADABLE = "uvold_baseline_unreadable"
UVOLD_TOPOLOGY_MISMATCH = "uvold_topology_mismatch"
UVOLD_UUID_UNRESOLVED = "uvold_uuid_unresolved"
UVOLD_MULTI_UV_SET = "uvold_multi_uv_set"
UVOLD_CREATE_FAILED = "uvold_create_failed"
UVOLD_FILL_FAILED = "uvold_fill_failed"
UVOLD_CLEANUP_FAILED = "uvold_cleanup_failed"

# 契約に無い。仮: 警告コードを画面の短い理由にする。QUESTIONS_rb_d.md。
_UVOLD_REASON_TEXT = {
    UVOLD_NO_BASELINE: "基準UVがありません",
    UVOLD_BASELINE_UNREADABLE: "基準UVが読めません",
    UVOLD_TOPOLOGY_MISMATCH: "形が基準と違います",
    UVOLD_UUID_UNRESOLVED: "メッシュの対応が取れません",
    UVOLD_MULTI_UV_SET: "UVセットが複数あります",
    UVOLD_CREATE_FAILED: "UVセットを作れませんでした",
    UVOLD_FILL_FAILED: "UVを埋められませんでした",
    UVOLD_CLEANUP_FAILED: "一時UVセットを消せませんでした",
}


class _UvOldFillError(Exception):
    """埋め込みだけが失敗した印。外には出さない。"""


def uvold_reason_text(warnings) -> str:
    """警告コードを、履歴に出す短い日本語にする。後始末の失敗は理由にしない。"""
    if isinstance(warnings, str):
        warnings = [warnings]
    if not isinstance(warnings, (list, tuple)):
        return "理由不明"
    for code in warnings:
        if code == UVOLD_CLEANUP_FAILED:
            continue
        text = _UVOLD_REASON_TEXT.get(code)
        if text:
            return text
    return "理由不明"


def choose_uvold_name(existing_names_by_shape, base=UVOLD_BASE_NAME) -> str:
    """対象の全シェイプに無い最初の名前。``uvOld``、``uvOld_1``、``uvOld_2`` の順。

    大文字小文字は区別する(Maya の UV セット名は区別する)。
    """
    taken = set()
    mapping = existing_names_by_shape if isinstance(existing_names_by_shape, dict) else {}
    for names in mapping.values():
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, (list, tuple)):
            continue
        for name in names:
            if isinstance(name, str) and name:
                taken.add(name)
    if base not in taken:
        return base
    index = 1
    while True:
        candidate = "{0}_{1}".format(base, index)
        if candidate not in taken:
            return candidate
        index += 1


def corner_uv_to_uv_arrays(face_vertex_counts, corner_uv):
    """コーナー UV を ``setUVs`` / ``assignUVs`` に渡せる配列にする。

    同じ (u, v) は 1 つにまとめる。1 つでも NaN のコーナーを持つフェースは
    ``uv_counts`` を 0 にする(そのフェースは UV 無し)。
    ``uv_ids`` の長さは ``uv_counts`` の合計と一致する。

    Returns:
        (u, v, uv_counts, uv_ids) すべて numpy 配列。
    """
    counts = np.asarray(face_vertex_counts, dtype=np.int64).reshape(-1)
    uv = np.asarray(corner_uv, dtype=np.float32)
    if uv.ndim != 2 or uv.shape[1] != 2:
        uv = np.reshape(uv, (-1, 2)).astype(np.float32, copy=False)
    n_face = int(counts.size)
    n_corner = int(counts.sum()) if n_face else 0
    if uv.shape[0] != n_corner:
        raise ValueError("corner_uv の長さが face_vertex_counts と合いません")
    uv_counts = np.zeros(n_face, dtype=np.int32)
    empty_u = np.zeros((0,), dtype=np.float32)
    empty_v = np.zeros((0,), dtype=np.float32)
    empty_ids = np.zeros((0,), dtype=np.int32)
    if n_face == 0 or n_corner == 0:
        return empty_u, empty_v, uv_counts, empty_ids
    finite = np.isfinite(uv).all(axis=1)
    face_of = np.repeat(np.arange(n_face, dtype=np.int64), counts)
    bad_faces = np.zeros(n_face, dtype=bool)
    if np.any(~finite):
        np.logical_or.at(bad_faces, face_of[~finite], True)
    good_faces = ~bad_faces & (counts > 0)
    uv_counts[good_faces] = counts[good_faces].astype(np.int32)
    good_corners = good_faces[face_of]
    good_uv = uv[good_corners]
    if good_uv.shape[0] == 0:
        return empty_u, empty_v, uv_counts, empty_ids
    unique, inverse = np.unique(good_uv, axis=0, return_inverse=True)
    return (
        np.ascontiguousarray(unique[:, 0], dtype=np.float32),
        np.ascontiguousarray(unique[:, 1], dtype=np.float32),
        uv_counts,
        np.ascontiguousarray(inverse, dtype=np.int32),
    )


def _snapshot_readable(snap) -> bool:
    if snap is None:
        return False
    for attr in ("face_vertex_counts", "corner_vid", "corner_uv", "face_mesh", "meta"):
        if not hasattr(snap, attr):
            return False
    meta = getattr(snap, "meta", None)
    return isinstance(meta, dict) and isinstance(meta.get("mesh_uuids"), (list, tuple))


def _same_topology(baseline, candidate) -> bool:
    """配列としての完全一致。指紋は頂点列の巡回に不変なので使わない。"""
    try:
        if list(baseline.meta.get("mesh_uuids") or []) != list(candidate.meta.get("mesh_uuids") or []):
            return False
        pairs = (
            (baseline.face_vertex_counts, candidate.face_vertex_counts),
            (baseline.corner_vid, candidate.corner_vid),
            (baseline.face_mesh, candidate.face_mesh),
        )
        for left, right in pairs:
            if not np.array_equal(np.asarray(left), np.asarray(right)):
                return False
    except Exception:
        return False
    return True


def _uuid_key(value) -> str:
    return str(value).casefold()


def _mesh_uv_slice(snap, mesh_index):
    """``face_mesh == mesh_index`` のフェースを、並びの順で切り出す。"""
    counts = np.asarray(snap.face_vertex_counts, dtype=np.int64).reshape(-1)
    face_mesh = np.asarray(snap.face_mesh).reshape(-1)
    uv = np.asarray(snap.corner_uv, dtype=np.float32)
    faces = np.flatnonzero(face_mesh == mesh_index)
    if faces.size == 0:
        return (
            np.zeros((0,), dtype=np.int32),
            np.zeros((0, 2), dtype=np.float32),
        )
    offsets = np.zeros(counts.size, dtype=np.int64)
    if counts.size > 1:
        offsets[1:] = np.cumsum(counts[:-1])
    pieces = []
    mesh_counts = []
    for face_index in faces.tolist():
        start = int(offsets[face_index])
        count = int(counts[face_index])
        mesh_counts.append(count)
        pieces.append(uv[start:start + count])
    mesh_uv = np.concatenate(pieces, axis=0) if pieces else np.zeros((0, 2), dtype=np.float32)
    return np.asarray(mesh_counts, dtype=np.int32), mesh_uv


def plan_uvold(baseline_snapshot, candidate_snapshot, shape_uuid_by_path):
    """旧 UV を載せる計画か、警告コード。

    トポロジーは ``mesh_uuids``(順序を含む)・``face_vertex_counts``・
    ``corner_vid``・``face_mesh`` の完全一致。UUID の集合は大文字小文字を
    揃えて比べる。一致したらシェイプごとに基準のコーナー UV を切り出す。

    Returns:
        (plan, None) または (None, 警告コード)。
        plan はシェイプパス → ``{"u","v","uv_counts","uv_ids"}``。
    """
    if not _snapshot_readable(baseline_snapshot) or not _snapshot_readable(candidate_snapshot):
        return None, UVOLD_BASELINE_UNREADABLE
    if not _same_topology(baseline_snapshot, candidate_snapshot):
        return None, UVOLD_TOPOLOGY_MISMATCH
    if not isinstance(shape_uuid_by_path, dict):
        return None, UVOLD_UUID_UNRESOLVED
    by_uuid = {}
    for path, raw_uuid in shape_uuid_by_path.items():
        if not isinstance(raw_uuid, str) or not raw_uuid:
            return None, UVOLD_UUID_UNRESOLVED
        key = _uuid_key(raw_uuid)
        if key in by_uuid:
            return None, UVOLD_UUID_UNRESOLVED
        by_uuid[key] = path
    uuids = list(candidate_snapshot.meta.get("mesh_uuids") or [])
    folded = []
    for raw in uuids:
        if not isinstance(raw, str) or not raw:
            return None, UVOLD_UUID_UNRESOLVED
        folded.append(_uuid_key(raw))
    if len(set(folded)) != len(folded) or set(folded) != set(by_uuid):
        return None, UVOLD_UUID_UNRESOLVED
    plan = {}
    for mesh_index, key in enumerate(folded):
        path = by_uuid[key]
        mesh_counts, mesh_uv = _mesh_uv_slice(baseline_snapshot, mesh_index)
        u_values, v_values, uv_counts, uv_ids = corner_uv_to_uv_arrays(mesh_counts, mesh_uv)
        plan[path] = {
            "u": u_values,
            "v": v_values,
            "uv_counts": uv_counts,
            "uv_ids": uv_ids,
        }
    return plan, None


def _empty_uvold_info():
    return {"applied": False, "name": None, "warnings": [], "shapes": []}


def _append_warning(info, code):
    if code and code not in info["warnings"]:
        info["warnings"].append(code)


def _short_shape_name(shape) -> str:
    text = str(shape).replace("\\", "/").rstrip("/")
    if "|" in text:
        text = text.split("|")[-1]
    if "/" in text:
        text = text.split("/")[-1]
    return text or str(shape)


def _as_name_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        names = []
        for item in value:
            if isinstance(item, str) and item:
                names.append(item)
        return names
    return []


def _as_current_name(value) -> str:
    names = _as_name_list(value)
    return names[0] if names else ""


def _as_uuid_text(value) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, str):
        return value
    return ""


def _pause_undo_recording(cmds):
    """記録だけ止める。キューは消さない。失敗したら (None, False)。

    ``uvremap_runtime._without_undo`` と同じ考え方。import はしない。
    契約に無い。仮: ``undoInfo`` が無い・失敗するときはそのまま進める。
    """
    undo_info = getattr(cmds, "undoInfo", None) if cmds is not None else None
    if not callable(undo_info):
        return None, False
    try:
        prev = undo_info(query=True, stateWithoutFlush=True)
    except Exception:
        return None, False
    try:
        undo_info(stateWithoutFlush=False)
    except Exception:
        return None, False
    return prev, True


def _restore_undo_recording(cmds, prev, stopped):
    if not stopped or cmds is None:
        return
    undo_info = getattr(cmds, "undoInfo", None)
    if not callable(undo_info):
        return
    try:
        undo_info(stateWithoutFlush=prev)
    except Exception:
        pass


def _resolve_cmds(cmds):
    if cmds is not None:
        return cmds
    try:
        import maya.cmds as cmds_mod
    except Exception:
        return None
    return cmds_mod


def _default_shape_uuid(cmds, shape):
    found = cmds.ls(shape, uuid=True)
    return _as_uuid_text(found)


def _default_fill_uvs(cmds, shape, uv_set_name, u_values, v_values, uv_counts, uv_ids):
    """OpenMaya 2 の ``setUVs`` / ``assignUVs``。Maya が無いときは例外。

    要実機確認: UV エディタで基準記録時と同じに見えること。
    """
    import maya.api.OpenMaya as om

    selection = om.MSelectionList()
    selection.add(str(shape))
    dag = selection.getDagPath(0)
    try:
        if dag.apiType() != om.MFn.kMesh:
            dag.extendToShape()
    except Exception:
        pass
    mesh_fn = om.MFnMesh(dag)
    u_array = om.MFloatArray()
    v_array = om.MFloatArray()
    for value in np.asarray(u_values, dtype=np.float64).reshape(-1):
        u_array.append(float(value))
    for value in np.asarray(v_values, dtype=np.float64).reshape(-1):
        v_array.append(float(value))
    mesh_fn.setUVs(u_array, v_array, uv_set_name)
    counts = om.MIntArray()
    ids = om.MIntArray()
    for value in np.asarray(uv_counts, dtype=np.int64).reshape(-1):
        counts.append(int(value))
    for value in np.asarray(uv_ids, dtype=np.int64).reshape(-1):
        ids.append(int(value))
    mesh_fn.assignUVs(counts, ids, uv_set_name)


def _fill_available(fill_uvs_fn) -> bool:
    if fill_uvs_fn is not _default_fill_uvs:
        return True
    try:
        import maya.api.OpenMaya  # noqa: F401
    except Exception:
        return False
    return True


def _query_uv_state(cmds, shape):
    names = _as_name_list(cmds.polyUVSet(shape, query=True, allUVSets=True))
    current = _as_current_name(cmds.polyUVSet(shape, query=True, currentUVSet=True))
    return names, current


def _set_current_uv(cmds, shape, uv_set_name):
    cmds.polyUVSet(shape, currentUVSet=True, uvSet=uv_set_name)


def _attach_export_uvold(exc, info):
    """書き出しが失敗しても、uvOld が残ったことを呼び出し側が読めるようにする。"""
    if exc is None or not isinstance(info, dict):
        return
    warnings = info.get("warnings") or []
    if UVOLD_CLEANUP_FAILED not in warnings:
        return
    shapes = []
    for name in info.get("shapes") or []:
        if isinstance(name, str) and name:
            shapes.append(name)
    codes = []
    for code in warnings:
        if isinstance(code, str) and code and code not in codes:
            codes.append(code)
    name = info.get("name") if isinstance(info.get("name"), str) else None
    try:
        exc.uvold_info = {
            "applied": bool(info.get("applied")),
            "name": name,
            "warnings": codes,
            "shapes": shapes,
        }
    except Exception:
        pass


def _cleanup_created_uvolds(cmds, created, info):
    """作ったセットだけ消す。1 つが失敗しても他は続ける。例外は握る。"""
    if not created or cmds is None:
        return

    def _attempt(entry):
        shape = entry["shape"]
        name = entry["name"]
        current0 = entry["current0"]
        try:
            _set_current_uv(cmds, shape, current0)
        except Exception:
            pass
        try:
            names = _as_name_list(cmds.polyUVSet(shape, query=True, allUVSets=True))
        except Exception:
            names = []
        if name in names:
            try:
                cmds.polyUVSet(shape, delete=True, uvSet=name)
            except Exception:
                pass
        try:
            names_now, current_now = _query_uv_state(cmds, shape)
        except Exception:
            return False
        return list(names_now) == list(entry["names0"]) and current_now == current0

    for entry in created:
        ok = False
        try:
            ok = _attempt(entry)
            if not ok:
                ok = _attempt(entry)
        except Exception:
            ok = False
        if not ok:
            _append_warning(info, UVOLD_CLEANUP_FAILED)
            short = _short_shape_name(entry["shape"])
            if short not in info["shapes"]:
                info["shapes"].append(short)


def _shape_uuid_map(shape_paths, cmds, uuid_fn):
    mapping = {}
    for shape in shape_paths:
        if uuid_fn is None:
            if cmds is None:
                return None
            raw = _default_shape_uuid(cmds, shape)
        else:
            raw = _as_uuid_text(uuid_fn(shape))
        if not raw:
            return None
        mapping[shape] = raw
    return mapping


def export_rebase_mesh_with_uvold(
    shape_paths,
    meshes_dir,
    baseline_id,
    *,
    triangulate=False,
    baseline_snapshot=None,
    candidate_snapshot=None,
    cmds=None,
    uuid_fn=None,
    fill_uvs_fn=None,
    export_fn=None,
    mel_eval=None,
    replace_fn=None,
    remove_fn=None,
    tmp_style=None,
):
    """旧 UV セットを一時的に付けて ``export_rebase_mesh`` を呼ぶ。

    戻り値は ``(mesh_dict, uvold_info)``。``uvold_info`` に絶対パスは入れない。
    旧 UV を付けられないときも書き出しは続ける。書き出しの例外は後始末のあと再送出する。

    要実機確認: ``uvOld`` が FBX の 2 番目に並ぶこと、作成で現在セットが
    切り替わっても戻ること、Undo キューが汚れないこと。
    """
    if export_fn is None:
        export_fn = export_rebase_mesh
    if fill_uvs_fn is None:
        fill_uvs_fn = _default_fill_uvs
    info = _empty_uvold_info()
    created = []
    cmds_api = _resolve_cmds(cmds)
    undo_prev, undo_stopped = _pause_undo_recording(cmds_api)
    export_exc = None
    mesh = None
    try:
        try:
            _attach_uvold(
                shape_paths,
                baseline_snapshot,
                candidate_snapshot,
                cmds_api,
                uuid_fn,
                fill_uvs_fn,
                info,
                created,
            )
        except Exception:
            # 計画や作成の失敗は警告にして書き出しへ進む。
            if not info["warnings"]:
                _append_warning(info, UVOLD_CREATE_FAILED)
            _cleanup_created_uvolds(cmds_api, created, info)
            created.clear()
            info["applied"] = False
        mesh = export_fn(
            shape_paths,
            meshes_dir,
            baseline_id,
            triangulate=triangulate,
            mel_eval=mel_eval,
            cmds=cmds,
            replace_fn=replace_fn,
            remove_fn=remove_fn,
            tmp_style=tmp_style,
        )
    except BaseException as exc:
        export_exc = exc
        raise
    finally:
        try:
            _cleanup_created_uvolds(cmds_api, created, info)
        except Exception:
            _append_warning(info, UVOLD_CLEANUP_FAILED)
        _attach_export_uvold(export_exc, info)
        created.clear()
        _restore_undo_recording(cmds_api, undo_prev, undo_stopped)
    return mesh, info


def _attach_uvold(
    shape_paths,
    baseline_snapshot,
    candidate_snapshot,
    cmds,
    uuid_fn,
    fill_uvs_fn,
    info,
    created,
):
    """旧 UV を付ける。失敗は info に警告を残して戻る。例外は作らない。"""
    shapes = list(shape_paths or [])
    if baseline_snapshot is None:
        _append_warning(info, UVOLD_NO_BASELINE)
        return
    if (
        candidate_snapshot is None
        or not _snapshot_readable(baseline_snapshot)
        or not _snapshot_readable(candidate_snapshot)
    ):
        _append_warning(info, UVOLD_BASELINE_UNREADABLE)
        return
    if not _same_topology(baseline_snapshot, candidate_snapshot):
        _append_warning(info, UVOLD_TOPOLOGY_MISMATCH)
        return
    if cmds is None and uuid_fn is None:
        _append_warning(info, UVOLD_CREATE_FAILED)
        return
    try:
        uuid_map = _shape_uuid_map(shapes, cmds, uuid_fn)
    except Exception:
        uuid_map = None
    if uuid_map is None:
        if cmds is None and uuid_fn is None:
            _append_warning(info, UVOLD_CREATE_FAILED)
        else:
            _append_warning(info, UVOLD_UUID_UNRESOLVED)
        return
    try:
        plan, warning = plan_uvold(baseline_snapshot, candidate_snapshot, uuid_map)
    except Exception:
        plan, warning = None, UVOLD_BASELINE_UNREADABLE
    if warning or not isinstance(plan, dict):
        _append_warning(info, warning or UVOLD_BASELINE_UNREADABLE)
        return
    for shape in shapes:
        if shape not in plan:
            _append_warning(info, UVOLD_UUID_UNRESOLVED)
            return
    if cmds is None or not _fill_available(fill_uvs_fn):
        _append_warning(info, UVOLD_CREATE_FAILED)
        return
    states = []
    try:
        for shape in shapes:
            names0, current0 = _query_uv_state(cmds, shape)
            states.append((shape, names0, current0))
    except Exception:
        _append_warning(info, UVOLD_CREATE_FAILED)
        return
    for _shape, names0, current0 in states:
        if len(names0) != 1 or current0 not in names0:
            _append_warning(info, UVOLD_MULTI_UV_SET)
            return
    name = choose_uvold_name({shape: names0 for shape, names0, _current in states})
    info["name"] = name
    try:
        for shape, names0, current0 in states:
            # 作成したと記録してから作る。途中で落ちても後始末が追える。
            created.append({
                "shape": shape,
                "name": name,
                "names0": list(names0),
                "current0": current0,
            })
            try:
                cmds.polyUVSet(shape, create=True, uvSet=name)
            except Exception:
                raise
            entry = plan[shape]
            try:
                fill_uvs_fn(
                    cmds, shape, name,
                    entry["u"], entry["v"], entry["uv_counts"], entry["uv_ids"],
                )
            except Exception as exc:
                try:
                    _set_current_uv(cmds, shape, current0)
                except Exception:
                    pass
                raise _UvOldFillError(str(exc)) from exc
            try:
                _set_current_uv(cmds, shape, current0)
            except Exception as exc:
                raise _UvOldFillError(str(exc)) from exc
    except _UvOldFillError:
        _append_warning(info, UVOLD_FILL_FAILED)
        _cleanup_created_uvolds(cmds, created, info)
        created.clear()
        info["applied"] = False
        return
    except Exception:
        _append_warning(info, UVOLD_CREATE_FAILED)
        _cleanup_created_uvolds(cmds, created, info)
        created.clear()
        info["applied"] = False
        return
    info["applied"] = True
