# QUESTIONS_pb_a

指示書(ORDER_PB_a v2)と契約の食い違い、指示書が決めていない点、実機でしか確かめられない点。
コード側で仮に埋めたところは `sp_live_sync_plugin.py` に「契約に無い。仮」と書いてある。
この発注は純関数だけで、タイマー・ダイアログ・Painter API には触れていない。

## a. 契約 R-2.2 の「既存の `_normalize_project_key_for_compare`」

契約は Painter プラグインに既存の `_normalize_project_key_for_compare` があると書いている。
プラグインには無く、`maya_live_sync.py` にだけある。
PB-a で独立に `normalize_project_key_for_storage` / `normalize_project_key_for_compare` / `project_keys_match` を実装した。
規則は Maya と同じ(区切りを `/`、NFC、比較だけ casefold。空はそのまま。`None` どうしは不一致)。
`maya_live_sync` はプラグイン本体から import していない。

## b. `rebase_wait_max_s` の範囲と `wait_timed_out` の解釈

契約に範囲が無い。仮に **60〜86400 秒**へ丸める(既定 1800)。
型違い(bool、NaN、inf、数値でないもの)は 1800。範囲外は端に寄せる。

`wait_timed_out` が真のときの状態は契約が「期限が先に来る」としか書いていない。
指示書どおり、PB-b は True を見たら `expired`(理由 `expired`)にする。`failed` にはしない。
使い方は PB-b。

## c. 状態ファイルの追加キー `uv_fills`

`build_rebase_status(..., uv_fills=...)` が dict のときだけキーを出す。
形は `{"enabled": bool, "switched": int, "skipped": int, "failed": int, "rolled_back": int}`。
欠け・型違いは 0 / False。`None` のときはキー自体を出さない。
Maya の `parse_status` は未知のキーを落とすので、今のまま読める。
契約 R-2.5 へ追記するかどうかは未決。PB-b が書く。

## d. Maya の `MIN_PLUGIN_VERSION = (1, 3, 0)`

これは結果ファイル(`_sync_result.json`)用。リベースを受けられる 1.4.0 とは別。
Maya 側のバージョンゲート(リベース用に `(1, 4, 0)`)は契約 v0.4 の未決事項に
「RB-e / RB-d2」として積んである。PB-a では触らない。

## e. `__version__` を PB-a で変えなかった理由

タイマーも検知も無い。利用者に見える挙動が無い。
1.4.0 への変更は PB-b。`__version__` は 1.3.0 のまま。

## f. `REBASE_CAUTION_TEXT_REPROJECT`

契約の常時表示の注意文から、UV 投影のフィルの扱いだけを変えた文。
PB-b が「設定 `rebase_reproject_uv_fills` が真、かつ依頼に `uv_old` がある」ときだけ使う。
この関数自体は依頼の `uv_old` を見ない。使ってよいか確認が要る。

## f2. `uv_old` の検査と文言

`parse_rebase_request` は Maya の `parse_request` と同じ入力に同じ戻りを返す
(キー無し、明示の `null`、`{name, index}`、不正は `bad_request`。
不正は `reload` の値違いより先)。テストで突き合わせた。

`REBASE_AUTO_UNWRAP_NOTE` は v2 の文
(「見つからないデータのみを生成」「すべて再計算」)。
旧い「自動展開を止められない可能性があります。」は使っていない。

`REBASE_WORKFLOW_GUIDANCE` は確認ダイアログ本文に常に入れる
(1.4.0 の 2D ペイント方針: 常時の注意文 + バックアップ + 運用の案内)。
オプトインの安全モードと層ごとの救済は 1.4.0 の範囲外。設定キーも作っていない。

## g. そのほか判断した点

1. **自動展開を止められない版(12.0.0)**。`can_disable_auto_unwrap` が false でも `reject` にはしない。
   B2(`"ask"`)はそのまま進める。`"auto"` は第 11 行で `ask`(`auto_unwrap_unverified`)に格下げ。
   判定は `supports_auto_unwrap_settings`(属性 / 初期化引数の有無)。API の版文字列では区別できない。
2. **`BACKUP_INCLUDES_UNSAVED = False`**。据え置き。True にするのは出荷前に指揮役が決める。PB-a / PB-b では変えない。
3. **`rebase_backup_path` の連番**。既存があると `_2`、`_3` …。10000 を超えたら `ValueError`(契約に無い。仮。通常は到達しない)。
4. **壊れた状態ファイル**。`write_rebase_status` は既存が読めないときは遷移検査をせず書いてよい(復旧)。
5. **`DEFAULT_CONFIG` に `uvremap_root_dir` は足さない**。Maya が持つキー。Painter は読むだけ。
6. **画面は触っていない**。`LiveSyncPanel` / `SetupWizard` の差分は 0。新しい設定キーは `DEFAULT_CONFIG` のみ。
