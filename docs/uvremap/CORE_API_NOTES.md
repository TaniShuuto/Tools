# core API と契約 §3 の差分(`uvremap_core.py`)

契約 `CONTRACT.md` §3(と、それが参照する §2 のファイル形式)の全 API について、実装の有無と
シグネチャの差を記す。実装を読み、`inspect.signature` / `dataclasses.fields` の出力と突き合わせた
(2026-10-16、`feat/uvremap-core`)。差の理由が契約と実装の食い違いに関わるものは
[`QUESTIONS.md`](QUESTIONS.md) の番号を示す。

凡例: 「同一」= 名前・引数・戻り値の型・既定値が契約どおり。呼び出し側(A2 ワーカー、B2 Maya 側)は、
契約どおりの呼び出しがそのまま通る(追加はすべて省略可能なキーワード引数またはフィールド)。

## §3 の API 一覧

| # | API | 実装 | 契約との差 |
|---|---|---|---|
| 1 | `Snapshot` | あり | フィールドは契約の §2 のキーに対応する 7 配列 + `ts_names` + `meta` + `warnings`。**差**: ① `ts_faces: list[ndarray]` を追加(`ts_names[n]` と対応。ファイル上のキー `ts_faces__<n>` と同じ内容)。② メソッド `faces_of(ts_name)`(TS が無ければ `TextureSetNotFound`)、`face_offsets()` を追加。③ `meta` に `warnings` は入らない(`Snapshot.warnings` が持つ。`meta.json` には保存する、下記 §2)。 |
| 2 | `save_snapshot(dirpath, snap) -> None` | あり・同一 | `mesh.npz` と `meta.json` を一時ファイル → `os.replace` で書く(原子的)。`dirpath` は作成する。 |
| 3 | `load_snapshot(dirpath) -> Snapshot` | あり・同一 | 欠落・形式不正・`schema` 不一致は `SnapshotError`(契約に記述なし)。`meta.json` の `warnings` は `Snapshot.warnings` に戻す。 |
| 4 | `triangulate_fan(face_vertex_counts) -> (tri_face, tri_corner)` | あり・同一 | 戻り値は `int32 (T,)` と `int8 (T,3)`。1 フェース 128 頂点超は `SnapshotError`(`tri_corner` が int8 のため)。 |
| 5 | `compute_topology_fingerprint(snap) -> str` | あり・同一 | 各フェースの頂点ID列の巡回正規形(向きを含む)の SHA-1。契約が許す別関数として `compute_flipped_topology_fingerprint(snap)`(全フェースを逆順にした指紋)を追加。 |
| 6 | `compute_uv_hash(snap, ts_name=None) -> str` | あり・同一 | UV だけでなく対象フェースの集合と頂点数も含める。-0.0 と 0.0、NaN のビット差は正規化。TS が無ければ `TextureSetNotFound`。 |
| 7 | `Decision` | あり | フィールドは同一(`method` / `reasons` / `warnings` / `details`)。**差**: `to_dict()` を追加。 |
| 8 | `classify(baseline, current, ts_name, *, bbox_tol=1e-3) -> Decision` | あり・シグネチャ同一 | 指紋一致 → `F`、全フェース逆順 → `F_FLIPPED`(`warnings` に `normals_flipped`)、それ以外 → `STOP`(`topology_changed`)、TS が無い → `STOP`(`ts_missing`)、bbox 不一致 → `warnings`。**契約が決めていない点の実装**: フェースの頂点数列が違えば `STOP`(`topology_changed`)。`warnings` に `ts_faces` の増減、`mesh_uuids_changed`、`uv_set_changed` も載る。指紋はメッシュ全体で取る(設計書 4.3)。 |
| 9 | `ChannelSpec` | あり | フィールド・既定値は同一。**差**: `colorspace` / `kind` が範囲外なら `ValueError`(`__post_init__`)。 |
| 10 | `ConvertResult` | あり・同一 | `tiles` / `coverage` / `stats` の 3 フィールド。`stats["warnings"]` に警告コードの一覧(`uv_overlap`、`udim_straddle`、`faces_no_source` 等)。 |
| 11 | `convert_channel(old_tiles, baseline, current, ts_name, decision, spec, out_res, *, dilate_px=16, normal_mode="uv") -> ConvertResult` | あり | **差**: 追加のキーワード引数 `udim: bool \| None = None`(非UDIM=折り返しの指定。`None` なら `baseline.meta["udim"]`、無ければ `True`)。A2 は job の `udim` を渡すこと(Q4)。`out_res` は契約の `int` / `{tile: (W,H)}` に加え、`(W,H)` と `None`、dict に無いタイル(=旧タイルの解像度を使い `stats["res_fallback_tiles"]` に記録)も受理する。`decision.method == "STOP"` や変換できる面が無いときは `ConversionStopped`(`reasons` を持つ)を投げる(契約に記述なし)。`old_tiles` は uint8 / uint16 も受理。 |
| 12 | `ProbeReport` | あり | 契約の 8 フィールドは同一。**差**: `excluded`(除外理由 → 点数)、`notes`、`params`(判定に使った設定)を追加し、`to_dict()` を追加。 |
| 13 | `probe_verify(old_tiles, new_tiles, baseline, current, ts_name, spec, *, n=512, seed=0) -> ProbeReport` | あり | **差**: 追加のキーワード引数 `udim`、`normal_mode`(変換と同じ値を渡す。`"off"` で変換したら `"off"` を渡す)、`tau`、`max_angle_deg`、`max_bad_fraction`、`min_valid`、`footprint`、`match_density`(Q4)。**仕様の差**: 窓の方式(Q1)、線形光での平均(Q2)、`tau=0.10`(Q3)、`fail` 優先(Q5)。詳細は [`PROBE_CALIBRATION.md`](PROBE_CALIBRATION.md) (a)。例外: TS が無ければ `TextureSetNotFound`、チャンネル数不一致・タイル無しは `ChannelFormatError`、トポロジー不一致は `ConversionStopped`。 |

契約 §3 の 13 項目すべてが実装されている。**未実装はない**。

## §2 ファイル形式の差

| 項目 | 契約 | 実装 |
|---|---|---|
| `mesh.npz` のキー | 8 種(`ts_faces__<n>` を含む) | 同一(dtype も同じ: `int32` / `float32` / `int8`) |
| `meta.json` のキー | `schema`(=1)、`baseline_id`、`uv_set`、`ts_names`、`mesh_uuids`、`topology_fingerprint`、`uv_hash`、`bbox_min`、`bbox_max`、`mesh_file_sha1`(任意)、`created` | 契約のキーをすべて持つ。**追加**: `warnings`(スナップショットの警告。往復で失わないため)。`meta["udim"]` があれば `convert_channel` / `probe_verify` が使う |
| `make_snapshot` が埋める既定値 | (契約に記述なし) | `baseline_id` = `bl_<指紋12桁>_<UVハッシュ8桁>`、`mesh_uuids` = `["mesh0", ...]`、`created` = 現在の UTC(ISO8601)。Maya 側(B2)は実値を渡すこと |
| 非多様体フェース | 検出して `warnings` に載せ、変換対象から除外 | 同一。`non_manifold_faces: N` を `warnings` に。UV 無しは `faces_without_uv: N`、頂点数 3 未満は `degenerate_faces: N` も載る |

## §3.1(3Dプローブ検証)の差

窓の方式の変更は既知(Q1)。ほかに実装が契約と異なる、または契約を補った点: 線形光での平均(Q2)、`tau=0.10`(Q3)、
`n_bad` と `n_valid < 64` の優先順位(Q5)、`low_resolution_probe` 注記、新タイル欠落点の不合格扱い、
除外理由 `udim_out_of_range` / `old_tile_missing` の追加。**手順7 の「U方向 2% のずれ」は平滑なテクスチャでは検出できない**(Q8、未決)。

## 契約外の公開物(`__all__`)

- 関数: `make_snapshot()`(配列から `Snapshot` を組み立て、検証・指紋・ハッシュ・警告・bbox を計算する。B2 の入口にできる)、
  `compute_flipped_topology_fingerprint()`、`find_nonmanifold_faces()`、`find_faces_without_uv()`、
  `udim_tile()`、`udim_origin()`、`srgb_to_linear()`、`linear_to_srgb()`。
- 例外: `UVRemapError`(基底)、`SnapshotError`、`TextureSetNotFound`、`ChannelFormatError`、`ConversionStopped`。
- 定数: `SCHEMA`、`PROBE_TAU`、`PROBE_NORMAL_DEG`、`PROBE_MAX_BAD_FRACTION`、`PROBE_MIN_VALID`、`PROBE_FOOTPRINT`。
