# QUESTIONS_pc_1

指示書 PC-1 と実装で揃えていない点、解釈した点。

## 1. `__init__.py` を編集した

指示の編集可能ファイルに `sp_live_sync_plugin/__init__.py` は無い。
一方、構造テスト `test_all_lists_match_definitions_and_reexports_are_identical` は、
部品のトップレベル名と `__all__` が一致し、かつ `__all__` の各名前が
パッケージ本体と同じオブジェクトであることを要求する。

`_replace_with_retry` と `REPLACE_RETRY_DELAYS_S` を `_base` に足すと、
`_base` の `__all__` と `__init__.py` の明示 import に足さないとこのテストが落ちる。
指示 3 の「既存の決まり(`_base` の `__all__`、`__init__` の明示列挙)」に合わせ、
この 2 名だけ `__init__.py` の `from ._base import` に追加した。

`tests/data/plugin_public_names.txt` は分割前の名前の一覧なので増やしていない。
`test_every_pre_split_top_level_name_is_reachable` は、その一覧にある名前が
参照できることだけを見る(新しい名前が載っていなくても落ちない)。

## 2. 失敗時の一時ファイル

`write_sync_flag` と設定の保存(`_merge_and_write_config`)は、これまで
`os.replace` が失敗すると一時ファイルを残していた。
その「残す」挙動に依存するテストは無かったので、`write_json_atomic` と同じく
消してから例外を送出するように変えた。

## 3. 待ちの回数

`REPLACE_RETRY_DELAYS_S` の 5 要素は、失敗の直後に待つ秒数。
2 回 `PermissionError` のあと 3 回目で成功するときは、待ちは 0.05 と 0.1 の 2 回。
ずっと失敗するときは 6 回目の `os.replace` で、5 回(合計 1.0 秒)待ったあとに
最後の `PermissionError` を型を変えずに送出する。6 回目の前には待たない。

## 4. テクスチャ移動のテスト

既存のエンジンの偽物(`LiveSyncEngine` と `export_project_textures` の差し替え)で、
振る舞いとして書いた。

- 共有違反(winerror 32)を 1 回出したあと、テクスチャの移動が成功する。
- 別ボリューム(winerror 17)は待たず、既存のコピー経路へ進む。

エンジンは `sleep_fn` を渡さない。この 2 テストだけ `time.sleep` を偽物に差し替えた。
`_replace_with_retry` 自体のテストは `sleep_fn` を注入し、本物の `time.sleep` が
呼ばれたら失敗するようにしてある。

## 5. 成功時のログ文言

再試行の末に成功したときだけ、次の 1 行を info で出す。フルパスは出さない。

`共有違反のため置き換えを再試行し、N回目で成功しました: ファイル名`

`_log` が先頭に付ける `[LiveSync] ` は従来どおり。初回で成功したときは出さない。
失敗し切ったときも出さない(例外が呼び出し側の既存の `except` に届く)。
