# QUESTIONS RB-a

指示書(ORDER_RB_a)と、指示書が書き写した契約の食い違い、指示書が決めていない点。
コード側で仮に埋めたところは `uvremap_rebase.py` に「契約に無い。仮」と書いてある。
実機(Maya / Painter)がないと決まらない項目は、この発注が純関数なので原則ない。仮の数値は下に挙げた。

## a. `backing_up` の取り消し

指示書の定数は `CANCELABLE_STATES = (received, awaiting_approval, preparing)`。
状態ファイルがまだ無い間も取り消せ、`backing_up` 以降は取り消せない、とある。
これは契約 R-3 の表と R-8 の表示表に合わせたもの、と指示書にある。
一方 R-8 本文の「`reloading` より前」だと `backing_up` はまだ取り消せる、という読みになる。

このモジュールは指示書の表に合わせ、`backing_up` は `LOCKED_STATES` に入れた。
`cancel_requested` が真でも文は「Painter が読み直しています(取り消せません)」のまま。
どちらの表が契約の正かは未決。

## b. 設定キー名

契約は R-2.4 で `rebase_request_ttl_s`、R-6 で `uvremap_rebase_ttl_s` と揺れている。
指示書に従い、設定ファイルのキーは `uvremap_rebase_ttl_s` だけを読む。
`rebase_request_ttl_s` は見ない。三角化は `uvremap_rebase_triangulate`、間隔は `uvremap_rebase_poll_s`。

## c. `poll_s` の範囲

契約に範囲が無い。仮に **0.5〜30.0 秒**へ丸める(`POLL_MIN_S` / `POLL_MAX_S`)。
型違い(bool、NaN、inf、数値でないもの)は `1.0`。int は float として受け、範囲外は端に寄せる。

## d. `done` なのに結果が合わないとき

`judge_completion` は次を返すだけにする。画面の文は変えない。

- 結果が無い → `skip` / `result_missing`
- `ok` が真でない → `skip` / `result_not_ok`
- `request_id` が違う → `skip` / `result_request_id_mismatch`

状態が `done` なら `decide_display` は「リベースが完了しました」のまま。
結果が合わないときの画面(失敗に見せるか、完了のままにするか)は未決。

## e. `request_id` の形

既存の `uvremap_runtime.UVRemapRuntime._next_id` は現地時刻とプロセス内の連番。
リベースの ID はそれとは別に、指示書 3.2 の `rb-<YYYYMMDD>-<HHMMSS>-<4桁小文字16進>`。
時刻は引数の datetime(tz があれば UTC へ変換、無ければ UTC の壁時計とみなす)。乱数も引数。
このモジュールは時計を読まない。

## f. `meshes/` の古いファイル

掃除は `requests/` の `.json` / `.cancel` と `status/` の状態ファイルだけ。
`meshes/` と `baseline/` には触らない。メッシュの古い fbx をいつ消すかは契約に無い。

## g. そのほか、こちらで仮に決めた点

1. **phase**。指示書は phase の名前だけを挙げ、12 状態への割り当ては無い。仮に次のとおり。
   - `received` / `preparing` → `working`
   - `awaiting_approval` → `waiting`
   - `backing_up` / `reloading` / `exporting` → `locked`
   - `done` → `done`
   - `failed` → `failed`
   - `declined` / `rejected` / `expired` / `superseded` → `ended`
2. **期限切れと取り消し要求**。状態が `missing` で `now > expires_at` のときは、`cancel_requested` より期限切れを優先する。`can_cancel` は偽。期限内で取り消し要求が無いときだけ `can_cancel` が真。`now == expires_at` はまだ待ち。
3. **読めない状態**。期限内は「状態を読めません」(`unreadable`)、`can_cancel` は常に偽。期限後は期限切れ表示、`can_cancel` も偽。
4. **`MSG_CANNOT_CANCEL` と `MSG_PLUGIN_UPDATE_REQUIRED`**。定数として持つ。`decide_display` は選ばない。ロック中の取り消し要求は `MSG_WORKING_LOCKED` のまま。プラグインの新旧は `plugin_supports_rebase` の bool だけ。
5. **`rejected` の文**。`message` が空でなければ、`MSG_REJECTED` の後ろに空白 1 つを挟んで足す。
6. **`failed` のバックアップ**。`MSG_FAILED_FMT` の後ろに ` バックアップ: {ファイル名}` を足す。`/` と `\\` は最後の要素だけ。`.` と `..` は名前にしない(None)。
7. **TTL**。bool と数値でないもの、整数でない float(`600.5`、NaN、inf)は 1800。整数値の float(`600.0`)は int にしてから 60〜86400 に丸める。
8. **triangulate**。本物の bool だけ。それ以外は False。
9. **空の識別子**。空の `project_key`、`baseline_id`、メッシュの `path`、空のテクスチャセット名は `bad_request`(書き込みは `ValueError`)。指示書が明示しているのは空の `ts_names`(リストが空)まで。
10. **sha1**。大文字の 16 進も受け、保存は小文字。
11. **未知キー**。依頼と状態は落とす。`_sync_result.json` の未知キーは残す(指示書が「通す」と書いてあるのは結果側だけ)。
12. **BOM**。`uvremap_worker.read_json` は `utf-8-sig` で BOM を外す。このモジュールは UTF-8 のまま読むので、BOM 付き JSON は None。指示書のテスト(BOM 付きは `read_request` が None)に合わせた。
13. **版の比較**。足りない桁は 0 とみなす。`1.4.0-beta` は `-` または `+` より前の数字だけ。`1.2.0a` や `v1.3.0` は解釈できない。
14. **完了判定の真偽**。`ok` は `is True` だけを成功とする。`enabled` は真偽値として見る(`not enabled`)。`final_full` が真で `force_full is True` でないときだけ `not_full_final`。`scene_id` の None と `""` は同じ。
15. **不正な request_id の `read_status`**。ディスクを見ずに `unreadable`(フォルダの外を開かない)。`read_request` / `cancel_exists` は None / False。
16. **掃除の境界**。状態が ok で終端なら `updated_at`。状態ファイルが無い依頼は、`now > expires_at` のあとだけ `expires_at` を終端時刻にする。経過が `max_age_s` 以上で消す。非終端の状態は、`expires_at` が古くても残す(開いているかの判定と同じく、状態ファイルが正)。読めない依頼 JSON や状態 JSON は消さない。壊れた依頼が終端の状態と並んでいても、壊れた側は残る。
17. **開いている依頼**。状態が ok で非終端なら、期限を過ぎていても開いている。`missing` / `unreadable` は `now <= expires_at` の間だけ開いている。
18. **`message`**。500 字(Python の文字数)で切る。省略記号は付けない。文字列でなければ空。
19. **`reload`**。3 つの bool が期待どおりなら、余分なキーは無視する。`reload` が dict でなければ `bad_request`。値が違う、または bool でないときは `bad_settings`。他の必須項目も壊れているときは `bad_request` を先に返す。
20. **完了案内**。`show_done_guidance` が真になるのは状態 `done` だけ。行の文は `MSG_DONE`。案内の本文は `MSG_DONE_GUIDANCE`(画面側が 1 回だけ出す想定。この発注は出さない)。
21. **状態ファイルは Maya から書かない**。期限切れの表示も、ファイルを新しく作らない。
