# QUESTIONS_pb_b

指示書(ORDER_PB_b v4)と契約の食い違い、指示書が決めていない点、実機でしか確かめられない点。

## a. `reporter.confirm` を使わない

契約の `reporter.confirm(text) -> bool` は使っていない。
確認ダイアログは非モーダルで `RebaseCoordinator` が持ち、承認された後に `RebaseJob` を作る。
タイマーの中で `exec()` の入れ子イベントループを回さないため。

## b. `rebase_reject_state`

`awaiting_approval` / `preparing` からの `reject` は、遷移表が許す形に合わせて
`failed`(または `declined` / `superseded` / `expired`)にする。
`expired` が `preparing` で来たときは `failed`(理由 `expired`)。
今の状態が `None` のときは理由によらず `rejected`。

## c. 起動時の中断の回収

`preparing` / `backing_up` / `reloading` / `exporting` は `failed` / `interrupted`。
`received` / `awaiting_approval` は中断にせず、通常の依頼として拾い直す
(期限内ならもう一度確認を出す)。

## d. `rebase_wait_max_s` 超過

`wait_timed_out` が真のとき、`received` / `awaiting_approval` なら `expired`(理由 `expired`)。
PB-a の QUESTIONS と同じ解釈。

## e. `final_full = false`

全量 Final を行わず `done`(`result_export_id` は `None`)。
Maya の完了判定は `request_id` の一致を要求するため、この場合は完了できない。
契約の欠け。

## f. メッシュ記録の失敗

`record_painter_mesh` が失敗してもリベースは `done` にする。
ログに警告を残す。次にプロジェクトを開き直すと既存の記録処理が取り直す。

## g. Live Sync が無効でもリベースする

`run_rebase_final_export` と司令塔は `self.enabled` を見ない。
リベースは Live Sync のチェックボックスと独立。

## h. 状態の追加キー `uv_fills`

状態ファイルに `uv_fills`(`enabled` / `switched` / `skipped` / `failed` / `rolled_back`)を書く。
Maya の `parse_status` は未知のキーを落とす。`_sync_result.json` には足していない。

## i. UV フィルの判断は依頼の `uv_old`

FBX の中身は調べない。`uv_old` が無い(キー無し・`None`・不正)依頼では切り替えない
(`uvold_absent`)。`name` は照合に使えない(ログだけ。Painter は UV セット一覧を API で取れない)。
`ts_names` に一致するテクスチャセットだけを扱う。空のときは全件にしない(l)。

## j. Maya のバージョンゲート

契約 v0.4 の未決事項「RB-e / RB-d2」。Maya は `_sync_result.json` の `plugin_version` を見る。
プラグイン更新直後は結果ファイルが 1.3.1(または更新前)のまま残り、
1 回書き出すまで Maya が「更新が必要」と表示する。PB-b では対処しない。

## j2. 1.3.0 の終了処理 S1〜S7

| # | 結果 |
|---|---|
| S1 | 修正。`_safe_export` 先頭で `_shutting_down` なら何もしない。busy 待ちは名前付きメソッドで、中でも確認する。 |
| S2 | 修正。Save As 経路の匿名 `singleShot` を名前付き `_preview_migration_timer` に置き換え、`shutdown` / `on_project_closing` で止める。ソースに新しい `QTimer.singleShot` は残していない。 |
| S3 | 修正。`close_plugin` はグローバルを先に `None` にし、`shutdown` と `delete_ui_element(_panel)` を個別の `try/except` で守る。 |
| S4 | 修正。`shutdown` で API とフックから engine への参照を外す。`close_plugin` はローカルの `panel` を外してから `gc.collect()` し、そのあと weakref を見る。`deleteLater()` はまだ入れない。要実機確認。 |
| S5 | 修正 + 要実機確認。`_shutting_down` が真なら `final_pending_changed` も設定書き込みも省く。`ProjectAboutToClose` と `close_plugin` の順序は実機で確かめる(7 の 19)。 |
| S6 | 修正 + 要実機確認。4 つの `disconnect` の成功/失敗をログに出す。失敗時は S7 のガードで止める。 |
| S7 | 修正。4 つのイベントハンドラ先頭に `if self._shutting_down: return`。`on_texture_state` はガードの後・`enabled` の前に最後の編集時刻を記録する。 |

#38 で追加されたエンジン側のリソース: なし(確認した)。

## j3. 12.0.0 で `auto_unwrap_settings` が無い

そのまま進める(`auto_unwrap_settings` を渡さない)。承認は必須
(`"auto"` は `decide_approval` が `ask` に格下げ)。
ダイアログに `REBASE_AUTO_UNWRAP_NOTE`(「すべて再計算」)を出す。

## k. そのほか判断した点

1. Live Sync 無効でも `TextureStateEvent` の時刻は記録する(`"auto"` のアイドル判定用)。
2. リベース中の通常書き出しは後回しにし、状態バーに 1 行出す。同じ文はリベースごとに一度だけ。
3. パネルのモーダル(`exec_()`)中も 1 秒タイマーは動く。`tick` は再入ガード付き。確認ダイアログは非モーダル。
4. `engine.config` は同じ dict のまま。5 秒ごとの `load_config()` は別変数。
5. `reload_mesh` のコールバックはフラグだけ。続きは `tick`。
6. 12.1.5 でプロジェクトの自動ラップ解除が「すべて再計算」のとき、`auto_unwrap_settings=None` で新 UV が保たれるかは要実機確認(項目 23)。勝手には決めていない。

## l. 修正依頼 1 で決めたこと

### 終端状態の書き込み

`done` などの終端を書くのに失敗したとき、司令塔は同じ tick ではジョブを捨てない。
次の tick から `retry_terminal` で書き直す。上限は `REBASE_TERMINAL_WRITE_MAX`(10)。
上限を超えたらエラーログを残してジョブを手放す。状態ファイルは最後に書けた状態のまま残る。
そのセッションでは依頼をやり直さない。

### UV フィルの訪問済み

`id(node)` は使わない。`uid()` がハッシュできる値を返すときだけ訪問済みにする。
`uid()` が無いノードは訪問済み判定をせず、深さの上限だけで循環を止める。

### preparing 中の取り消し

ジョブがある間も、その依頼の `.cancel` を見る。
状態が `preparing` まで(取り消し可能な状態)なら `declined`(理由 `cancelled`)にして止める。
バックアップと `reload_mesh` には進まない。`backing_up` 以降は無視する。

### busy 待ちの引数

`_busy_export_args` の 1 スロット共有はやめた。
呼び出しごとに `functools.partial` で引数を持たせ、再開メソッドの中で shutdown を確認する。

### 空の ts_names

依頼の `ts_names` が空(無し、空リスト、名前が空)のときは、全テクスチャセットには切り替えない。
`no_matching_texture_set` にする。コンテキスト側のテクスチャセット名では埋めない。

### needs_saving が読めないとき

`"ask"` は従来どおり偽(待たない)。`"auto"` のときだけ真にし、未保存として待つ。
effective が `ask` に落ちる場合も偽。読めたときの挙動は変えていない。

### そのほかこの修正で入れたもの

- `needs_manual_check` と `missing_attrs` はログに出す。完了メッセージには、どちらかがあれば目視確認の一言を足す。
- 完了メッセージは「リベースが完了しました。」を重ねない。案内文の末尾と UV フィルの文に句点を付ける。
- `reload_mesh` の前と、SUCCESS のあと `is_busy()` が真のあいだは `rebase_reload_timeout_s` を上限に待つ。超えたら `reload_timeout`。コールバックそのものの待ちは従来どおり。
