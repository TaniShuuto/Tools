# 指揮役への質問・記録(レーン D1 橋渡し)

契約 `CONTRACT.md` §2・§3・§4・§6 と、core / ワーカー / Maya C2 の実装を読んで橋渡しを書いた。
既存ファイルは変更していない。ここで勝手に決めず、判断を仰ぎたい点だけを記す。

## B1. status.json のロックは `status.lock`(ワーカー実装に合わせた)

作業指示は「ワーカーと同じ `worker.lock` の仕組み」と書いてある。
実際の `uvremap_worker.py` は:

- ジョブ全体: `FileLock(uvremap_root/worker.lock)`
- `status.json` の読み→更新→原子置換: `FileLock(uvremap_root/status.lock)`(`_Run.set_status`)

`update_status_entry` はワーカーの `FileLock` を import し、**`status.lock` を使う**。
`worker.lock` にすると、ジョブ終了後の `finish()` → `set_status`(こちらは `worker.lock` の外、`status.lock` の中)と競合し、
両方を順に取るとデッドロックし得る。

ワーカー側を `worker.lock` に揃える変更は、既存ファイルを触らない方針のためしていない。

## B2. 除外フェースの `Snapshot.warnings` への書き方

C2 の `info` は `warnings`(コードの list: `nonmanifold_faces` 等)と、
除外フェースID(`nonmanifold_faces` / `untriangulated_faces` / `no_uv_faces`)を別キーで持つ。
core の `make_snapshot` は `"non_manifold_faces: N"` のように件数だけを自前で付ける。

橋渡しは指示どおり **info を引き継ぎ**、core が付けた警告文字列には置き換えない。

- コードはそのまま(`"nonmanifold_faces"`)
- ID があるキーは `"nonmanifold_faces:1,4"` の形で追加(空リストは書かない)

B2 が件数表記や core 形式への正規化を望むなら、形式を指定してほしい。

## B3. D-3 の「原本へ戻す」と表示状態 `STALE`

契約 §6 D-3 は「直前の正常世代が現行 UV ハッシュと一致しない場合、Maya は原本表示へ戻して `FAILED` を出す」。
指示の `decide_display_state` は不一致を **`STALE`** と定めている(D-1 の表示状態)。

実装は指示の優先順に従った:

1. `enabled is False` → `ORIGINAL`(D-4)
2. `decision.method == "STOP"` → `BLOCKED`
3. ledger に当該 TS が無い(`None` / `uv_hash` キー無し) → `ORIGINAL`
4. `uv_hash` 一致 → `CONVERTED_OK`
5. 不一致 → `STALE`

この関数はワーカーの `FAILED` を受け取らない。変換失敗後に原本へ戻す処理は、
パネルが `status.json` の `FAILED` を見てファイルノードを切り替える側(B2)だと考えた。
`FAILED` をこの関数に混ぜるなら引数が足りない。

## B4. `out_res` が dict のときの `n_tiles`

`choose_timeout_s(out_res, n_channels, n_tiles)` で `out_res` が `{tile: (W,H)}` のときは、
**実サイズの合計ピクセル**を使い、`n_tiles` は見ない。int のときは `n_tiles * out_res²`。
core が受理する `(W, H)` タプルは、指示に無いので `BridgeError` にする。

## B5. `make_snapshot` を組み立てに使った

契約 §3 の公開 API には `make_snapshot` は無い(CORE_API_NOTES では「B2 の入口にできる」とある)。
配列の検証と指紋/ハッシュの計算をここで再実装しないため、`core.make_snapshot` を呼び、
そのあと `compute_topology_fingerprint` / `compute_uv_hash` と info の bbox / ts_names 等で meta を上書きした。
`created` を省略すると core が UTC now を書く。テストは必ず `created=` を渡す。

`SnapshotError` は `BridgeError` に包む(呼び出し側が core の例外型に依存しなくてよいように)。

## B6. job.json の任意項目

- `source_flag` は契約の例にあるが、ワーカーは省略可。`None` なら job に載せない。
- `channels` の既定は契約 §4.1 の 6 チャンネル。
- `timeout_s` の既定は契約の 300。E7 のとおり 4096²・6 ch では足りないので、
  呼び出し側は `choose_timeout_s` の戻りを渡すこと。
- `quality` は `"final"` / `"live"` 以外を `BridgeError`。ワーカーは `quality` を検査していない。
- ワーカーの任意キー(`lock_timeout_s`、`uv_hash`、`latest_generation_path` 等)は `**extra` で通す。

## B7. 既存ファイルを変えなかった点

直したいが、指示どおり触っていない。

- ワーカーの `set_status` は `status.lock`、指示書は `worker.lock`(上記 B1)。
- ワーカーの `_now_iso` はナイーブ現地時刻。core の `make_snapshot` の既定 `created` は UTC。
  橋渡しは status をワーカーに合わせ、Snapshot.created は引数で注入する。
- QUESTIONS_e2e E5(低解像度プローブの偽陽性)はそのまま。一周テストは 256² の平滑信号で、
  `pass` または `inconclusive` を成功とする(契約 I3: 公開はどちらも可)。

## B8. Maya 実機

このレーンでは Maya を import も起動もしない。C2 の配列形は PR #13 の契約と
`feat/uvremap-maya-arrays` の `collect_arrays` docstring を読んだだけ。実機は未実行。
