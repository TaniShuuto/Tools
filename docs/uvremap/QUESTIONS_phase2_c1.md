# 指揮役への質問・記録(P2-c1 Painter 追従の画面と配線)

拠り所は `CONTRACT_PHASE2.md` v0.2 の P2-1〜P2-5b、P2-6 の入力側、P2-8、P2-9 と作業指示書 P2-c1。
ここで勝手に決めず、食い違いと実装で取った扱いを記す。

## 指揮役の回答(P2-a)の反映

- Q4: `ready` が真で条件 1・2 を通った判定のときだけ、follow.json の `flag_mtime_ns` を今回の印の mtime にする。無効・自動追従オフ・基準なし・settle 前は更新しない。
- Q5: `same_scene` は `dict[TS名, bool]` に固定した。dict 以外(bool を含む)は、どの TS もシーンが違うものとして扱う。
- Q9: `MSG_INPUT_STALE` / `MSG_UV_STALE` の『反映する』を『変更を反映する』に直した。
- Q10: `MSG_NO_CHANGE` を「変更はありません({ts})」にした。
- Q3・Q7・Q8・Q11・Q12・Q14・Q15: 現状のまま。Q13 は P2-c2。

## C1. follow.json の `scene_id`(契約 P2-3 の追補)

P2-3 の例には `source_sig` / `last_job_id` / `pending` だけがある。
条件 5 のシーン一致のため、ランタイムがジョブを起動した直後に、その TS の記録へ `scene_id` を足す。
手動・自動とも書く。書き込みの失敗は変換を止めない(例外は握り、履歴には残さない)。

記録が無い TS、または `scene_id` が無い記録は、シーンが違うものとして扱う(自動変換しない)。
既存の変換済み TS は、一度でも起動すれば記録が付く。

## C2. 印が無いときの入力側 STALE

作業指示は「印が無い → 何もしない」と、入力側 STALE を `follow_tick` の中で行う、の両方を書く。
印が無いときは STALE にもしない(①に合わせた)。印があるときだけ、自動追従オフの TS を `input_stale` で見る。
settle は STALE には使わない(契約 P2-6「poll ごとに行ってよい」)。

## C3. 予約 ID の「保留」と「順番待ち」

戻り値が `queued:` で始まるとき、`has_running_job_for_ts` が真なら保留(`MSG_PEND_FMT`)、
偽なら順番待ち(`MSG_QUEUE_FMT`)。全 TS が予約だけなら「変換を開始しました」は出さない。
`ok` は予約が 1 件でもあれば True(拒否ではなく受け付けたため)。

## C4. 直した既存テスト

P2-b の「手動の実行中は拒否」を前提にした次を、今回の方針(手動も保留 1 件)に合わせて直した。

- `tests/test_uvremap_runtime.py` の `test_auto_pending_coalesces_manual_still_rejected` → `test_auto_and_manual_pending_coalesce`(手動の戻り値を予約 ID に変更)
- `tests/test_uvremap_runtime.py` の `test_same_ts_running_job_is_not_restarted`(2 回目の手動要求を拒否から保留の予約 ID に変更。実行中ジョブは増やさない)
- `tests/test_uvremap_integration.py` の `test_convert_now_already_running_returns_japanese_message` → `test_convert_now_already_running_returns_pending_message`
- `tests/test_uvremap_integration.py` の `test_convert_now_all_already_running_is_not_started_message` → `test_convert_now_all_reserved_does_not_say_started`
- `tests/test_uvremap_follow.py` の `same_scene=True` が全 TS 一致だった箇所(Q5 により dict 以外は不一致)
- `tests/test_uvremap_follow.py` の `MSG_NO_CHANGE` / `MSG_INPUT_STALE` / `MSG_UV_STALE` の原文比較

統合側から `MSG_ALREADY_RUNNING` を返す経路は外した。定数の再エクスポートは残してある。

## C5. 閉じた欄の古い `fold_status_label`

2 引数の `fold_status_label(enabled, converting)` は残した(既存テスト用)。
画面の閉じた欄は `fold_status_label_p2` を使う。今回の `needs_apply` はいずれかの TS が `STALE`、`auto_any` は Painter 追従がオン。UV 追従は P2-c2。

## C6. 未実行(実機必須)

- Maya の画面での見え方(チェックの位置・グレーアウト・閉じた欄の「要反映」「自動追従」)
- Painter の Final 書き出しへの実際の追従(P2-10 項 1〜4)
- 「変更を反映する」ボタン(P2-6b。今回の範囲外)
- UV 側の指紋・UV 追従(P2-c2)
- 学校 PC / 自宅 Maya での mayapy 子プロセス

## C7. 確認したいこと

1. 記録に `scene_id` が無い既存 TS を、初回だけ同じシーンとみなしてよいか。今は不一致として自動変換しない。
2. 印が無いとき、入力側 STALE も見ない扱いでよいか。
3. 予約だけの「今すぐ変換」を `ok: True` にしてよいか。

## C8. 毎秒のシーン走査をやめた

`follow_tick` は毎秒、キルスイッチ・プロジェクトのキー・Final フォルダ・`_sync_complete.flag` の mtime だけを見る。
シーン走査、follow.json、status.json、`source_signature`、入力側 STALE は、印の mtime がこの画面で最後に重い段階を済ませたときと違うとき、落ち着き待ちの settle が過ぎたとき、Painter 追従のチェックかキルスイッチ ON の直後、窓を開いた直後の 1 回だけ行う。
落ち着き待ち(`ready` が偽で、印が記録より新しい)で終わったときは、その mtime をメモリに覚えない。毎秒のシーン走査で Maya の操作が引っかかるのを避けるためで、どの TS を変換するかは変えていない。
