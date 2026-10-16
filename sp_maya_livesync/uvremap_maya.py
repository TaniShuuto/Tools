# -*- coding: utf-8 -*-
"""Maya シーンから UV 再マップ用のメッシュ配列を取り出す。

公開関数の戻り値のキー名と形は docs/uvremap/CONTRACT.md §2 に合わせる。
変換エンジン(uvremap_core)とは依存しない(キー名と形だけの契約)。

Maya の import はこのモジュールの読み込み時には行わない。
関数の中で初めて maya.api.OpenMaya / maya.cmds を読む。
numpy は Maya 非依存のためモジュール先頭で読む。
"""
from __future__ import annotations

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
