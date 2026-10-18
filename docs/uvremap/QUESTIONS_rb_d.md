# QUESTIONS_rb_d

RB-d(Maya の画面と配線)で、指示書と食い違う点・契約に無く仮に決めた点・実機でしか確かめられない点。
コードを変える判断はここで止めてある。Painter 側(PB-a / PB-b)と RB-a / RB-b / RB-c の公開関数は変えていない。

## 1. 候補の記録を書き出しより先にした

契約 R-4.1 は 3 がメッシュの書き出し、4 が候補の記録、という順に読める。
実装は逆にした。書き出すファイル名が `meshes/<baseline_id>.fbx` で、`baseline_id` は候補を作らないと決まらないため。
失敗したときは候補を破棄し、依頼ファイルは作らない。

## 2. 「Painterへ送る」を無効にする条件

契約 R-8 は、非終端の依頼があればボタンを無効にする、と読める。
契約 R-4.1 は、開いている依頼が取り消せる段階なら確認を 1 回出して置き換える、と読める。
実装は R-4.1 を優先し、`locked`(`backing_up` / `reloading` / `exporting`)のときだけボタンを無効にした。
取り消せる段階ではボタンは有効で、押すと確認が 1 回出る。

## 3. 使った UV セット名は依頼に入っていない

依頼の書式は RB-a の `parse_request` / `build_request` が固定していて、未知のキーは書かれない。
`uvOld` の名前を入れる欄は無い。使った名前は履歴・画面のメッセージ・`rebase_send` の戻り値にだけ残している。
Painter は 2 番目の UV セットを名前ではなく番号 1 で参照する、という前提でよいかを確認したい。
依頼に任意の項目 `uv_old`(`{"name", "index"}`)を足すなら、RB-a の変更(別発注)が要る。

## 4. 複数 UV セットのシェイプには付けない

`uvOld` が FBX の UV セット並びで 2 番目になるのは、元の UV セットがちょうど 1 つのときに限られる。
いずれかのシェイプが 2 つ以上なら、全シェイプで旧 UV なしにする。`polyUVSet -reorder` はシーンを変えるので使っていない。
この制限でよいか、複数セットを許して「番号」を Painter に伝える方式にするか。

## 5. 全シェイプで付けるか、全く付けないか

一部のシェイプだけに付けると、Painter 側のメッシュ内で UV セット数が揃わない。
対象シェイプは全部に同じ名前で付けるか、1 つも付けないかの二択にした。

## 6. 旧 UV の出どころは現行の基準

旧 UV は、今作った新基準候補ではなく、Maya が既に保存している現行(active)の基準
(`baseline/<active_id>/mesh.npz` のコーナー UV)から取る。
読み出しの公開関数が無いので、RB-b の内部関数 `_read_active_baseline` を使っている。
公開の読み出しがあると、内部関数に依存しなくて済む。
トポロジーは、基準と候補の `mesh_uuids`(順序を含む)・`face_vertex_counts`・`corner_vid`・`face_mesh` が配列として完全一致すること。
指紋(`topology_fingerprint`)は頂点 ID 列の巡回に不変なので、コーナーの並びが回転していると UV を載せ違える。使っていない。

## 7. Undo は記録を止めて戻す

`undoInfo(query=True, stateWithoutFlush=True)` で現在の状態を取り、`stateWithoutFlush=False` で記録だけ止める。
キューは消さない。書き出しの `finally` の最後で元の状態へ戻す。
`undoInfo` が無い、または失敗するときは、そのまま進めて `uvold_info` には残さない。
`undoChunk` に包んで終了後に undo する方式は、利用者の Redo 履歴を壊すので採らなかった。
要実機確認: 送信の直前の操作が、送信のあと Ctrl+Z で取り消せること。

## 8. project_key は比較用に正規化した値のまま

依頼の `project_key` は、コントローラの `_project_key()`(Live Sync の比較用に正規化した形)をそのまま書いている。
契約の文言は「保存形式」。Painter は比較時に正規化するので同じ ID になる想定。要実機確認。

## 9. done なのに結果が合わないときは候補を消さない

`judge_completion` の理由が `result_missing` / `result_not_ok` / `result_request_id_mismatch` / `not_full_final` のとき、
`complete_rebase` は呼ばない。表示は `MSG_RESULT_UNCONFIRMED`(「結果を確認できません」)。基準は切り替えない。候補も消さない。
`decide_display` は `done` を「リベースが完了しました」と出し続けるので、画面では上書きしている。
理由: 完了後に利用者が別の Final を書き出すと `_sync_result.json` の `request_id` が null で上書きされ、
Maya が `done` を見る前に取り違える可能性がある。
状態ファイルの `result_export_id` を使った救済は、判定の変更(RB-a)になるので、この発注ではやっていない。後続で検討するか。

## 10. locked の依頼は置き換えない

`backing_up` 以降は確認も出さず拒否する。古い依頼が後から `done` になると、基準の切り替えが 2 本走るのを避けるため。

## 11. 完了処理の再試行は仮に 5 回

`partial` と `complete_rebase` が返す `skipped` / `job_running` は、次の poll で同じ依頼を再実行する(RB-b は冪等)。
`REBASE_COMPLETE_RETRY_MAX = 5`。超えたら再試行をやめ、画面と履歴に「手動で確認してください」を 1 回出す。
`killswitch_off` と `scene_changed` は `judge_completion` が `complete` を返さないので `complete_rebase` は呼ばず、回数には数えない。
この上限と、`partial` のときの文(「完了処理が途中で止まりました({step})。もう一度試します」)は仮。

## 12. FBXResetExport の副作用は画面に出していない

RB-c の質問 h のとおり、`FBXResetExport` は利用者の FBX 書き出し設定を初期化する。
この発注では画面に注意を出していない。出すかは未決。要実機確認(送信後に FBX 書き出しオプションが初期化されるか)。

## 13. 依頼の書き込みに失敗すると rebase_local.json の行が残る

`register_rebase` は依頼ファイルより前に呼ぶ。そのあと `write_request` が失敗したときは、候補の破棄と FBX の削除はする。
`rebase_local.json` の行を消す公開関数は無いので、登録だけ残る。実害は小さい(完了処理は状態が `done` で結果が合うときだけ進む)。
削除の公開関数が要るか。

## 14. 設定キーは get_config 経由。DEFAULT_CONFIG は触っていない

`uvremap_rebase_ttl_s` / `uvremap_rebase_triangulate` / `uvremap_rebase_poll_s` は
`rb.normalize_rebase_settings(self._safe_config())` で読む。画面には出していない。
`maya_live_sync.py` の `DEFAULT_CONFIG` には足していない。未知のキーは `load_config` が保持する、という既存の挙動に乗っている。
本体(`maya_live_sync.py`)の差分は無い。要実機確認: 設定ファイルに手書きしたキーが読まれること。
