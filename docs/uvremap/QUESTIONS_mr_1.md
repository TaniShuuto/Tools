# QUESTIONS MR-1

連続リベースで崩れる UV 投影フィルの安全策(Painter プラグイン 1.4.5)。指示と現物がずれた点、仮に埋めた点。

## 1. 列挙の戻り

`inspect_uv_fills_for_approval` の戻りは namedtuple `UvFillApproval`。
フィールドは `direction_count`, `direction_names`, `prior_count`, `prior_unknown`, `prior_names`。
`inspect_direction_uv_fills` はこれを呼び、従来どおり `(direction_count, direction_names)` を返す。

警告文と `awaiting_approval` の `message` の `{n}` は `prior_count + prior_unknown`。
番号を読めないフィルも警告側に数える、という指示に合わせた。
`prior_names` はその両方の名前で、走査順、最大 10 件、各 64 字(`_direction_fill_name` と同じ切り方)。

`stop_after` は bool 以外の int のときだけ有効。合計が達した次のノードの手前で打ち切る。
方向チャンネルの数は、その時点までしか数えない。

## 2. `prior_uvset` と状態の `prior`

`switch_uv_fill_node` が `prior_uvset` にするのは、`UVSetToUVSet` かつ `source_uv_set` が依頼の番号と一致して読めたときだけ。
番号が違う、または読めない(例外・属性無し・整数でない)ものは、今までどおり `skipped` / `not_uv`。
`UvFillStackReport.prior` と `uv_fills.prior` は `prior_uvset` の件数。
`skipped` には `prior_uvset` も今までどおり含める。既存のキーの意味は変えていない。

## 3. `auto_unwrap_unverified` の早期 return は変えていない

`decide_approval` の `auto_unwrap_unverified` は、`prior_uv_fills` の格下げより前に return する。未保存の `wait` など表の続きを飛ばす穴は、今回も残っている。

## 4. 承認方式を ask に置き換えたあとの未保存

格下げは `approval` を `ask` に置き換えてから表の続きを評価する。
`backup_includes_unsaved` が真のとき、未保存の待ちは `approval == "auto"` の側だけなので、格下げ後は待たない。
出荷している `BACKUP_INCLUDES_UNSAVED` は偽なので、実運用では未保存なら今までどおり待つ。
テストの「auto + 1 + 未保存 → wait」は、この既定(偽)で見ている。

## 5. 公開名と `plugin_public_names.txt`

新しい名前は各部品の `__all__` と `__init__.py` の明示 import に足した。
`tests/data/plugin_public_names.txt` は足していない。
分割の構造テストは、ファイルに書いてある名前がプラグインから参照できることだけを見て、ファイルに無い新しい名前は要求しない。

`__all__` は部品のトップレベル名と一致させる必要があるため、次も公開名になっている。
`_read_source_uv_set`, `_empty_uv_fill_approval`, `_prior_scan_limit_reached`, `_append_capped_fill_name`, `_prior_uv_fills_block_auto`。

## 6. 文書

`README.md` と `Easy_install.md` にリベースの説明は無かった。
Painter 側の注意として、指示の 2 文を足した。焼き込みなどの回避手順は書いていない。

## 7. 確認文の位置

警告文は、注意文(崩れるフィルがあるときは「UV 投影のまま残っているフィルは見た目を保ちます」)の直後に足した。
既定ボタンの `default_cancel` は、警告があるときだけダイアログ工場へ渡す。引数を省いた既存の呼び出しはそのまま。
