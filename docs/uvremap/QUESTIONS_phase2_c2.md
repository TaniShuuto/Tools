# 指揮役への質問・記録(P2-c2 UV 編集への追従)

拠り所は `CONTRACT_PHASE2.md` v0.2 の P2-5b、P2-6 の UV 側、P2-6b、P2-8、P2-0' の結果と、作業指示書 P2-c2。
ここで勝手に決めず、食い違いと実装で取った扱いを記す。

## 反映した指揮役の回答

- Q12: `uv_fingerprint` のバイト列を `<f4`(リトルエンディアンの float32)に固定した。ビッグエンディアンの配列を渡しても同じ値になる。
- Q13: UV の差で `STALE` にするのは状態が `CONVERTED_OK` の TS だけ。`ORIGINAL` / `CONVERTING` / `FAILED` / `BLOCKED` は `ts_to_stale` に入れない。
- Q13: `fingerprints_recorded` は `{TS名: {シェイプ名: 指紋}}`。その TS の記録にあるシェイプのうち 1 つでも現在の指紋と違えば、その TS を対象にする。記録が無い TS は対象にしない。
- Q14: `MSG_UV_START_FMT = "UV の変更を検知しました。変換を開始します({ts})"` を足した。自動変換の開始時にメッセージ欄と履歴へ出す。保留は `MSG_PEND_FMT`、順番待ちは `MSG_QUEUE_FMT`。

## C2-1. `uv_fingerprints` の戻り値

`(fingerprints, elapsed_s)` にした。`fingerprints` は `{シェイプのフルパス: (crc, UV数)}`。キーは `MFnMesh` の `fullPathName()` で、`list_texture_sets` の `shape` と同じ。
`uv_set` が無いときは、そのシェイプの `currentUVSetName()` を `getUVs` に渡す。指定セットが無いシェイプ、UV セットが無いシェイプ、解決に失敗したシェイプは結果に入れない。
Maya 自体を import できないときは関数が例外を出す。呼び出し側(ランタイムと `uv_tick`)が握り、変換は止めない。
`elapsed_s` は呼び出し全体の秒(`time.perf_counter`)。

## C2-2. 指紋の比較

follow.json の `uv_fp` は JSON なので `[crc, UV数]` の list で書く。判定は int の組にし、list と tuple を同じ値とみなす。
現在の指紋が無いシェイプは「違い」に数えない。記録の中身が指紋の組でない TS は対象にしない。

## C2-3. `decide_uv_follow` の `ts_by_shape`

引数は残した(呼び出しの並びを変えないため)。対象の決定には使わない。どのシェイプを見るかは、その TS の `uv_fp` だけで決める。

自動変換の対象は、Q13 のまま次だけ。

- すでに `STALE` の TS
- 今回 `ts_to_stale` に入った TS(`CONVERTED_OK` だけ)

`auto_follow_uv` が真で、最後の変化から `settle_s` 秒経ったときだけ。`enabled` が真でないときは何もしない。

## C2-4. 直した既存テスト

`decide_uv_follow` の記録の形と、`ORIGINAL` を STALE にしない規則に合わせて直した。

- `tests/test_uvremap_follow.py` の `_uv`(記録を TS ごとの辞書にした)
- `tests/test_uvremap_follow.py` の `test_decide_uv_follow_shape_settle_and_killswitch`
  - `ts_to_stale` は `("Body", "Trim")` から `("Body",)` へ(`Trim` は `ORIGINAL`)
  - 一致の例の `fingerprints_recorded` を `{"Body": {"mesh": [1, 4]}}` にした(list と tuple は同じ)
- 期待が変わっていないテスト: `ts_to_convert` の `("Body", "Prop")`、キルスイッチ、記録が空の TS、`last_change_at is None`

追加したテストは `test_decide_uv_follow_per_ts_record_and_state_filter`、`test_uv_fingerprint_is_little_endian_f4`、`test_apply_button_visible_only_when_stale`。

## C2-5. 最初の UV 確認と settle の起点

`uv_tick` を開いてからの最初の呼び出しは、間隔の起点にするだけで指紋を取らない。次に `uvremap_uv_poll_s` 以上経ってから取る。
前回の tick の指紋が無い初回の確認で、記録と違えば、その時刻を「最後に変化した時刻」にする。同じなら変化なし(`last_change_at` は None のまま、自動変換しない)。
2 回目以降は、前回の tick の指紋と違えばその時刻に更新する。

## C2-6. 200 ミリ秒を超えたとき

1 回の指紋取得が 0.2 秒を超えたら、そのシーンの間隔を 10 秒にする。ちょうど 0.2 秒は超えないので間隔は変えない。
履歴へ出す文は `MSG_UV_SLOW`(「UV の確認に時間がかかるため、確認の間隔を 10 秒にしました」)。シーンごとに 1 度。
シーンが切り替わったら間隔を `uvremap_uv_poll_s` に戻し、遅い案内の「1 度」も戻す。前回の tick の指紋は捨てる(別シーンの値で変化とみなさない)。

## C2-7. 基準 UV が無いときの自動変換

指紋の差で `mark_stale` はする。自動の `convert_now` は、基準 UV が無いときは呼ばない。呼ぶとランタイムが `BLOCKED` を書き、STALE の表示が消えるため。

## C2-8. `ts_by_shape` を作るとき

`list_texture_sets` の結果からメモリに持つ。作るのは次だけ。

- `follow_tick` の重い段階(`_resolve_context`)
- 手動の変換と「変更を反映する」(`_resolve_context`)
- 画面を開いた直後(`attach` → `refresh_shape_map`)
- シーンの切り替え(`on_scene_changed` → `note_scene_changed`)

`uv_tick` はこのメモリを使い、`list_texture_sets` を呼ばない。対応がまだ空なら、指紋は取らない。

## C2-9. 重い段階が早く戻ったとき

`_resolve_context` が失敗したとき(対象の TS が無い、Final フォルダや作業フォルダが取れない、作業フォルダが Final の内側、など)も、その印の mtime を「重い段階を済ませた」として覚える。印が変わったとき、Painter 追従または UV 追従のチェックを切り替えたとき、キルスイッチを ON にしたとき、シーンを切り替えたときは、もう一度判定する。
印の mtime 自体が取れない軽い段階(プロジェクトや Final が無い)では、覚える mtime が無いので毎秒プローブだけ続ける。シーン走査はしない。

## C2-10. `uv_fp` の取得失敗

起動してワーカーを開始できたあと、`_write_follow_launch` でその TS のシェイプ(`uv_fp_shapes`。無ければジョブのシェイプ)の指紋を書く。
取得が例外のときは `uv_fp` を書かず、既にあれば残す。`source_sig` / `last_job_id` / `scene_id` は従来どおり書く。変換は止めない。
空振りの省略(P2-5b)はランタイムの既存の実装のまま。省略でジョブを作らなかったときは `uv_fp` も更新しない。

## C2-11. 「変更を反映する」の色

ボタンの色は、STALE 行と同じ `COLOR_ATTENTION`(`#c9822a`)。失敗用の `COLOR_WARN` は使っていない。
ボタンは開閉行に置き、いずれかの TS が `STALE` のときだけ見える。判定は純関数 `apply_button_visible`。
押したときの文言は「今すぐ変換」と同じ(`変換を開始しました` / `MSG_PEND_FMT` / `MSG_QUEUE_FMT`)。対象はシーンにある `STALE` の TS だけ。`origin` は `manual`。

## C2-12. 閉じた欄の「自動追従」

`fold_status_label_p2` はそのまま。`auto_any` は Painter 追従または UV 追従のどちらかがオンのとき。キルスイッチ OFF の間は「無効」が優先する。

## C2-13. 未実行(実機必須)

- Maya の画面での UV 編集(移動・回転・縫合・切断・自動展開)の直後に、この `uv_tick` が STALE と「変更を反映する」を出すこと
- 画面上のマニピュレータ操作(P2-0' はバッチのコマンドで代用済み。画面操作は未確認)
- 指紋取得の実時間と、200 ミリ秒超で間隔が 10 秒になることの体感
- ボタンの色・閉じた欄の「要反映」「自動追従」・UV 追従チェックのグレーアウトの見た目
- UV 追従オンで、編集をやめてから settle 後に自動変換され、表示が最後の UV に合うこと
- 学校 PC / 自宅 Maya での mayapy 子プロセス
