# 指揮役への質問・記録(レーン D2a 実行制御)

契約 `CONTRACT.md` §2〜§6 と D1/C2/C3/ワーカーを読んで `uvremap_runtime.py` を書いた。
既存ファイルは変更していない。ここで勝手に決めず、判断を仰ぎたい点と、実機未検証の点を記す。

## R1. `collect_arrays_fn` をコンストラクタに足した

作業指示のコンストラクタは `get_enabled` / `get_root` / `now_fn` / `popen_fn` / `cmds_fn` / `mayapy_path`。
Maya 無しテストで `record_baseline` / `convert_now` に合成配列を渡すため、任意引数 `collect_arrays_fn` を足した。
省略時は関数内で `uvremap_maya.collect_arrays` を import する。`worker_script` もテスト差し替え用に任意。

公開メソッド名は指示どおり。迷った点は以下。

## R2. 基準が無い / キルスイッチ / mayapy 無しのときの戻り値

`convert_now` の型は `-> str`(job_id)。ジョブを作らないときは空文字 `""` を返し、例外にはしない。
- 基準なし → `status.json` を `BLOCKED`、メッセージは指示どおり「基準UVが未記録です。Painterへ送った時点で記録してください」
- キルスイッチ False → 全ファイルノードを原本へ、全 TS を `ORIGINAL`
- mayapy が無い → `FAILED`、メッセージ「mayapy が見つかりません。…」

空文字でよいか、番台の番兵 job_id がよいか確認したい。

## R3. 現行基準のポインタ `baseline/active.json`

契約に「どの baseline_id が現行か」のファイルは無い。
`record_baseline` が上書きせず、`force=True` のときだけ別 ID を足すので、現行 ID を
`baseline/active.json`(`{"schema":1,"baseline_id":"..."}`)に書いた。
`convert_now` はこの ID を読む。無ければ `baseline/*/mesh.npz` の名前順先頭。
このファイルを契約に載せるか、別名がよいか。

## R4. mayapy 起動は実機未検証

`Popen([mayapy, uvremap_worker.py, "run", job.json])`、stdout/stderr を `jobs/<job_id>/worker.log`、
Windows は `CREATE_NO_WINDOW`。
環境から外したもの: `PYTHONHOME` / `PYTHONPATH` / `PYTHONSTARTUP` / `PYTHONUSERBASE` /
`PYTHONEXECUTABLE` / `__PYVENV_LAUNCHER__`。付けるもの: `PYTHONNOUSERSITE=1`。
場所は `mayapy_path` が無ければ `sys.executable` の隣(`mayapy.exe` / `mayapy`)。
自分が mayapy ならそれを使う。

**学校PCの Maya GUI → 子 mayapy は未実行。** `tools_uvremap/verify_runtime.py` で確認する想定。
外した変数の一覧が足りない/多いがあれば指示してほしい。

## R5. ファイルノード切替で存在確認と色空間をやっていない

既存 Live Sync は切替先の実在を確認し、無いタイルは触らない。
今は `result.json` の `files` を信じてパスと `uvTilingMode`(1タイル=0、複数=3)だけを書く。
存在確認はしていない(偽 cmds の単体テストがファイルを作らないため)。

色空間(`ignoreColorSpaceFileRules` 等)も触っていない。QUESTIONS_e2e.md E1 の「タグ無し PNG で
ファイルノードの色空間が変わる可能性」は残っている。D2b でやる想定。

属性名 `uvremapOriginalPath` / `uvremapOriginalTiling` は指示どおりで変えていない。
既に属性があるノードは元パスを上書きしない(二度目の変換でも原本は最初のパス)。

## R6. D-3 の表示状態はワーカーの status を優先する

失敗(11/12/13)のあと:
- ledger の `uv_hash`(直前の正常世代)と、そのジョブの現行 TS 単位 `uv_hash` を比べる
- 不一致または正常世代が無い → 原本へ戻す
- 一致 → ファイルノードは変換後のまま

status.json はワーカーが書いた `BLOCKED`/`FAILED` を上書きしない。
契約 D-3 の「原本表示へ戻して FAILED を出す」と、終了コード 11 の `BLOCKED` が并存する。
B3(QUESTIONS_bridge.md)どおり、失敗表示はワーカー側、ノード切替だけをこちらがやる形にした。
トポロジー変更だけ(UV ハッシュは直前の正常世代と同じ)なら、D-3 どおり変換結果を維持する。
「トポロジー変更なら常に原本へ」がよければ、判定を変える。

## R7. `poll()` のイベント形と `revert_to_original` の status

`poll()` は終わったジョブごとに次を返す(UI タイマー想定):

```
{"job_id","project_key","ts_name","exit_code","state","message","switched","reverted","updated"}
```

`updated` は `now_fn`。実行中ジョブは返さない。

`revert_to_original(ts_name=None)` に `project_key` が無い。ノードはシーン全体を見る。
status.json は、それまで `get_root` したプロジェクトだけを `ORIGINAL` にする。
D2b で `project_key` を足すなら合わせる。

## R8. `convert_now` の任意キーワード

指示の引数に無いが、ジョブに必要なものを任意キーワードで受けた:
`udim`(既定 True)、`out_res`(None なら timeout 300)、`timeout_s`、`copy_retries`、
`uv_set`、`source_flag`(無ければ `source_dir/_sync_complete.flag` があれば使う)、
`dilate_px`、`normal_mode`、`lock_timeout_s`、`n_tiles`(timeout 見積用)。

`job.json` の `uv_hash` は D1 の `current_uv_hash`(TS 単位)。E4 どおり。
ワーカー Q1 の `latest_generation_path` は書いていない。

基準/現行スナップショットの `created` は `now_fn`。時計に依存するテストを避けるため。

## R9. TS 名の衝突

切替対象は、現在パスまたは `uvremapOriginalPath` のファイル名が `<ts>_<suffix>` で始まるもの。
`suffix` は結果の `files` から取る。`Body` と `BodyAlt` は suffix 付きなら区別できる。
`revert_to_original("Body")` は `<ts>_` 始まりだけを見るので、`BodyAlt_*` も巻き込む余地がある。
TS 名に `_` が含まれる場合も、suffix が分からないと切れ目が曖昧。実アセットの TS 名規則を知りたい。

## R10. ワーカー Q3 の stale CONVERTING / pid 監視は未実装

強制終了で status が `CONVERTING` のまま残る件(QUESTIONS_worker.md Q3)は、
`poll()` では見ていない。プロセスが消えたあとに result.json が無ければ失敗として D-3 する。
pid 生存確認は D2b か別作業。

## R11. Maya 公式でこのレーンから再確認していない点

- `uvTilingMode` 0=off / 3=UDIM は契約 §4.1 と既存 `maya_live_sync.py` のコメントに合わせた。
  Autodesk の現行ページは、この作業中には開いていない。
- `fileTextureName` を一度空にしてから書き直す手順は、既存 Live Sync の実装を踏襲した。
- `addAttr` の `dt="string"` / `at="long"`、`attributeQuery(..., exists=True)`、`deleteAttr` も
  既存コードと Maya cmds の一般的な使い方。実機の属性の型(enum か long か)は未確認。
- Maya GUI の `sys.executable` が `maya.exe` で、隣に `mayapy.exe` があることは通説で、
  この環境では未確認(R4)。

## R12. `verify_runtime.py` は未実行(実機必須)

mayapy バッチで立方体+ファイルノード+合成 PNG を作り、
`record_baseline` → UV 移動 → `convert_now` → `poll` → 切替確認 → `revert_to_original`、
および mayapy 子プロセス起動(R4)を見る。このクラウド環境に Maya は無い。
