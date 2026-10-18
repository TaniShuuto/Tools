# 指揮役への質問・記録(ORDER_RB_b)

リベース契約 v0.3 の R-4.3 / R-4.4 / R-4.5 / R-6 のうち、Maya 側の候補作成・破棄と完了処理だけを `uvremap_runtime.py` に足した。判定・書き出し・画面は触っていない。ここで勝手に埋めず、確認したい点を書く。

## Q1. 基準が未記録のとき、候補が事実上の基準になる

`active.json` が無く `baseline/` に候補だけがあるとき、`record_baseline`(`force` なし)と `convert_now` は `_list_baseline_ids` の先頭を既存の基準として使う。候補の ID は `bl-` で始まるので、その先頭が候補になる。

「Painter の読み直しが成功するまで今の基準を変えない」は、`active.json` が既にある場合は守れる。基準が一度も記録されていない状態で候補を作ると、候補が事実上の基準になる。挙動は変えていない。テスト `test_candidate_without_active_json_becomes_de_facto_baseline` で固定した。

## Q2. 完了してよいと判定されなかったときの候補

キルスイッチ OFF、またはシーン識別子が記録と違うときは、何も変えずに `skipped` で戻る。候補フォルダも `rebase_local.json` の行も残る。消す・残すは契約に無い。ここでは消していない。`discard_baseline_candidate` を呼ぶのは呼び出し側(RB-d)の判断だと思う。

途中失敗(`partial`)のときも、先に進んだ段階は戻さない。候補が最初から無い(`failed_step == "active"`)場合は、基準もノードも `follow.json` も変えない。

## Q3. `rebase_local.json` の名前と置き場所、記録が増え続ける

ファイル名 `rebase_local.json` と、`<uvremap_root>/` 直下( `rebase/` の外)は仮である。Painter は読まない・書かない想定。中身は `schema` と `requests` だけ。

同じ `request_id` は上書きしない。古い行の削除はしていない。記録は増え続ける。掃除の時期と条件は契約に無い。

## Q4. 切り替え後も `ledger.json` と `output/final` は残る

基準を候補へ切り替えても、古い UV で作った台帳と `output/final` のファイルは削除も書き換えもしない。契約に記述が無いため。

## Q5. `revert_to_original` が状態の世代を 0 にする

既存のままである。完了処理の `reverted` 段階は、記録された各 TS について `revert_to_original(ts_name)` を呼ぶ。対象 TS の `status.json` は `ORIGINAL` になり、世代は 0 になる。変えていない。

## Q6. `follow.json` の「初期化」は、切り替えた TS の行の削除にした

契約は初期化の形を決めていない。仮に、記録された TS の行だけを `ts` から削除した。`source_sig` / `uv_fp` / `last_job_id` / `scene_id` は残さない。`flag_mtime_ns` と、他の TS の行は残す。削除した行が 1 つも無いときは `follow.json` を書かない(ファイルが無ければ作らない)。

## Q7. 古い基準フォルダは消さない

切り替え後も、それまでの `baseline/<旧 ID>/` は残す。候補の破棄は `discard_baseline_candidate` だけで、完了処理は現行ポインタの書き換えしかしない。

## Q8. 履歴は `history_fn` の引数

この発注では `uvremap_integration` に繋がない。`complete_rebase(..., history_fn=None)` の `history_fn` が渡されたときだけ `history_fn("uvremap", 文言)` を呼ぶ。省略時は呼ばず、その段階は成功にする。文言は `MSG_REBASE_HISTORY_DONE` と `MSG_REBASE_HISTORY_PARTIAL_FMT`。

## Q9. 破棄の「active が決まっていない」の読み

削除しない条件 5 の括弧書き「1 と 3 と 4 のいずれも無い」は、条件 1 が「安全でない ID」なので、5 に到達した時点では必ず無い。次のように読んだ(仮)。

- `_read_active_baseline` が ID を返し、それが対象と一致するなら消さない(条件 3)
- `active.json` の生の `baseline_id` が対象と一致するなら消さない(条件 4。`mesh.npz` が無く解決できない場合)
- 解決結果も生の ID も無いときだけ、`_list_baseline_ids` の先頭を消さない(条件 5)

`active.json` に生の ID があり、そのフォルダに `mesh.npz` が無いときは、`record_baseline` / `convert_now` は名前順の先頭を事実上の基準にする。この実装は生の ID のフォルダだけを守り、先頭は守らない。先頭も守るべきなら指示がほしい。

## Q10. 例外要約の型名と、シーン識別子に入り得る絶対パス

失敗時の `error` は `type(exc).__name__` と 1 行の要約である。`RuntimeControlError` はクラス名 `RuntimeError_` の別名なので、要約の型名は `RuntimeError_` になる。絶対パスに見える部分は要約から除く(仮)。

`scene_id` は既存の `_scene_id()` の戻りをそのまま保存する。Maya ではシーンパスになることがあり、その場合 `rebase_local.json` に絶対パスが入る。一致判定に使うため、ここでは削っていない。履歴の文言には入れてない。
