# QUESTIONS RB-d2

Maya のバージョンゲート(1.4.0 以上)と完了表示の補足。指示と現物がずれた点、仮に埋めた点。

## 1. CHANGELOG の節

RB-d のコミット(`0eb9d4c`, `4aa3e3d`)は `CHANGELOG.md` を更新していない。
PB-c の Maya 側(`2b8c66c`)は、`## sp_maya_livesync/sp_live_sync_plugin.py` のコードフェンス内
`2026.10.20(1.4.1)` に、プラグイン側の変更と一緒に 3 行で書かれている。
今回はプラグインの版を上げていないので、その節の続きには足していない。
同じ「日付 + 箇条書き 3 行」の書き方で、フェンスの外に
`## sp_maya_livesync/uvremap_rebase.py` を新しく足した。

## 2. `plugin_supports_rebase` の呼び出し

本番の呼び出しは `uvremap_integration.py` の `_rebase_check_ready_impl` の 1 か所。
`rebase_send` は `_rebase_send_impl` 経由でここを通る。`rebase_check_ready` も同じ関数を使う。
ほかの本番呼び出しは無い。
テストでは `test_uvremap_rebase.py` と、`test_sp_live_sync_plugin_rebase_job.py` の 2 箇所
(プラグイン版 1.4.3 の結果で真であること)。相談が要る本番の呼び出しは無い。

## 3. 「結果を確認できません」は変えない

`_REBASE_UNCONFIRMED` の依頼は、Painter 側が後から結果を書く可能性があるため、今回は変えない。

## 4. 完了表示のつなぎ

指示は、完了表示に `MSG_DONE_DIRECTION_FMT` を「続けて」出す、とだけある。区切りは書いていない。
画面の `QLabel` は折り返すので、改行 1 つで続けた。履歴は別の 1 行(既存の `_rebase_note_once`)。
契約に無い。仮。

## 5. プラグインの状態パーサとの突き合わせ

`parse_status` は常に `direction_fills` を返す。Painter 側の `parse_rebase_status` は返さない
(指示はプラグインを触らない)。
`test_sp_live_sync_plugin_rebase_pure.py` の `test_parse_status_matches_maya` と
`test_parse_status_drops_uv_fills_and_maya_still_reads` は、両方の辞書が一致することを要求する。
許可リストの外だが、この 2 件だけ比較時に `direction_fills` を外すようにした。プラグインの書き出しは変えていない。
外さないと、全テストを通せない。

## 6. 上限到達と追従

`_rebase_is_settled` に上限到達を入れると、状態ファイルを読まないことに加え、
`_rebase_request_blocks_follow` も追従を止めなくなる。
RB-d の回答は、再試行上限のあと 60 秒は追従を止める、だった。
指示が settled 扱いと言っているので、60 秒を待たず追従を再開する。表示は上限の文言のまま。
既存の `test_follow_block_releases_except_open_and_recent_done` のその部分を直した。

## 7. `MIN_PLUGIN_VERSION`

値は `(1, 3, 0)` のまま。`plugin_supports_rebase` はもう見ない。
結果ファイルの読み取り(`parse_sync_result` など)も、この定数では比べていない。指示どおり定数は残した。
