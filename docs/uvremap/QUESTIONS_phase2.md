# 指揮役への質問(P2-a 判定の純関数)

契約は `CONTRACT_PHASE2.md` v0.2。実装は契約の文言に合わせ、決まっていないところは下に書いた振る舞いを仮に入れてある。変えるときはこの一覧の番号で指示してほしい。

画面に出さない設定(P2-2。キー名・既定・範囲):

| キー | 既定 | 範囲 |
|---|---|---|
| `uvremap_max_parallel` | 2 | 1〜4(整数。範囲外は端へ丸める) |
| `uvremap_follow_settle_s` | 2.0 | 0.5〜30 秒 |
| `uvremap_uv_settle_s` | 3.0 | 1〜60 秒 |
| `uvremap_uv_poll_s` | 2.0 | 1〜30 秒 |

画面に出る 2 つは `uvremap_auto_follow` と `uvremap_auto_follow_uv`。既定はどちらも false。

## Q1. クラス名は `Worker` ではなく `_Run`

契約は `Worker._discover` と書いている。実装のクラスは `_Run`。照合は `_Run._discover` からモジュール関数 `match_source_name` へ切り出した。クラスの改名はしていない。

## Q2. follow.json の空

無い・読めない・JSON でない・最上位がオブジェクトでない・`schema` が整数 1 でない(`true` や `"1"` を含む)・`ts` があるのにオブジェクトでない、ときは次を返す。

```json
{"schema": 1, "flag_mtime_ns": null, "ts": {}}
```

`flag_mtime_ns` を 0 にするかは契約に無い。schema が 1 のファイルは、余分なキーがあってもそのまま返す(往復で落とさない)。

## Q3. 書き込みのロック

P2-3 は「ロックの作法」と書く。ロックファイルの名前と待ち時間は無い。作業指示は `uvremap_io.replace_file` による原子的な置き換えなので、一時ファイルへ書いて `replace_file` するだけにした。`follow.lock` は作っていない。`status.lock` と同じ `FileLock` にするなら、ファイル名と待ち時間の指示がほしい。

`uvremap_root` が無いとき `save_follow` はディレクトリを作らず、書き込みの例外をそのまま出す。

## Q4. `decide_follow` は印を更新しない

P2-4 は「判定後、`flag_mtime_ns` を更新する(対象の TS が無くても)」。純関数の引数と戻り値に、更新後の値は無い。この関数はファイルを書かない。

仮の呼び出し側の規則: `ready` が true で、条件 1 と 2 を通過したあとの判定(変換・保留・「再変換するテクスチャセットがありません」)のときだけ、記録の `flag_mtime_ns` を今回の印の mtime にする。無効・自動追従オフ・基準なしのときは更新しない(更新すると、再び有効にしたときにその書き出しを見落とす)。この規則でよいか。

`ready` は「印が記録より新しく、settle 秒だけ mtime が変わっていない」ことだけを表す。条件 1〜2 で変換しなくても、落ち着いていれば true。印が新しくない、または settle 前は false。

`last_flag_ns` が `None`(空の既定の `flag_mtime_ns`)のときは未処理とみなし、印の mtime が整数なら「より新しい」。

## Q5. `same_scene` の型

条件 5 は TS ごとなので、`dict[TS名, bool]` を受ける。引数名は単数なので、全 TS 共通の bool も受ける。どちらでもない値は、どの TS もシーンが違うものとして扱う。どちらかに固定するなら指示がほしい。

シーンが違う TS は変換も保留もしない。

## Q6. 保留は「sig が違って、他の条件は満たす CONVERTING」だけ

`CONVERTING` は P2-5 の保留にする、と P2-4 にある。条件 1・2・4・5 を満たし、状態が `CONVERTING` のときだけ `ts_to_pend` に入れた。sig が記録と同じとき、シーンが違うときは入れない。`ORIGINAL` / `FAILED` / `BLOCKED` は変換も保留もしない。`CONVERTED_OK` と `STALE` は変換。名前は昇順。

## Q7. `source_sig` の一致

ファイル名の集合と、各 `[size, mtime_ns]` が同じなら一致。list と tuple は同じ値とみなす。両方 `None`(未記録)は一致。`None` と `{}` は不一致。`source_signature` は、チャンネルの suffix に一致する通常ファイルをすべて入れる。ワーカーが変換時に行う「拡張子が複数なら最新だけ」「タイル有無が混在なら最新側だけ」は掛けない。フォルダが読めないときは `{}`。ファイル 1 つの stat 失敗とその場がファイルでない場合は、その名前だけ飛ばす。

## Q8. 設定の型

bool は `True` / `False` だけ。それ以外(1 や `"true"`)は既定値。

`uvremap_max_parallel` は bool 以外の int だけ。float の `2.0` は既定の 2。

float の 3 項目は int も受ける(bool は除く)。JSON では `5` が int になるため。文字列・null・非数(NaN / inf)は既定値。整数を拒んで既定値に落とす方がよければ指示がほしい。

## Q9. STALE の案内文とボタン名

P2-6 の文は『反映する』。ボタン名(P2-6b / P2-8)は「変更を反映する」。定数は契約の原文にした。

- `MSG_INPUT_STALE`: 「Painter の書き出しが新しくなりました。『反映する』を押してください」
- `MSG_UV_STALE`: 「UV が変更されました。『反映する』を押してください」

作業指示の括弧内は『変更を反映する』。ボタン名に揃えるなら指示がほしい。

## Q10. 「変更はありません」に TS 名は無い

P2-5b の文言は「変更はありません」。作業指示の括弧内は「変更はありません({ts})」。定数 `MSG_NO_CHANGE` は契約どおり TS 名なし。足すなら指示がほしい。

P2-8 の次は、契約どおり `({ts})` を持つ。

- 「Painter の書き出しを検知しました。変換を開始します({ts})」
- 「変換中のため、完了後にもう一度変換します({ts})」
- 「順番待ちです({ts})」

## Q11. UV 判定の時刻は秒

`decide_follow` の `now_ns` / `last_change_seen_ns` / 印の mtime はナノ秒。`settle_s` は秒。

`decide_uv_follow` の引数名は `now` と `last_change_at` で、契約に単位が無い。秒として `now - last_change_at >= settle_s` と比較している。ナノ秒に揃えるなら指示がほしい。`last_change_at` が `None` のときは、まだ落ち着いていない(自動変換しない)。

## Q12. 指紋のキーとバイト列

`fingerprints_now` と `fingerprints_recorded` は、どちらもシェイプ名 → `uv_fingerprint` の戻り値。記録が無いシェイプは「違う」とみなさず、STALE にしない。

`uv_fingerprint` は U を float32 にしたバイト列の後に V を連結し、`zlib.crc32` と UV 数(U の要素数)を返す。バイト順は numpy の native(この環境では little-endian)。`<f4` と明示するかは契約に無い。U と V の長さが違うときは `ValueError`。

## Q13. シェイプの「すべて」と、自動変換してよい TS

指紋が記録と違うシェイプについて、`ts_by_shape` に並ぶ TS 名をすべて `ts_to_stale` に入れた。`ORIGINAL` や `CONVERTING` も含む。契約の「すべて」から外していない。呼び出し側が `CONVERTING` を STALE で上書きすると、実行中の表示が消える。状態を見てから書くなら、その指示がほしい。

自動で変換してよい TS(`auto_follow_uv` が真で、settle のあと)は次だけにした。

- 状態がすでに `STALE` の TS(入力側で STALE のものも含む。契約は「STALE の TS」とだけ書く)
- 今回 STALE にする TS のうち、状態が `CONVERTED_OK` または `STALE` のもの

`ORIGINAL` / `FAILED` / `BLOCKED` / `CONVERTING` は自動変換に入れない(P2-4 の条件 3。UV 追従も自動の要求のため)。`enabled` が真でないときは STALE にも変換にもしない。

`ts_to_stale` は「今回の UV の差で STALE にする名前」であり、入力側ですでに STALE の TS を消すリストではない。

## Q14. `reason` の文面

契約に、判定関数の `reason` の文面は無い。変換や保留や STALE 化をするときは空文字。しないときは次の短文。

- 印は更新されていません
- 印の落ち着き待ちです
- UV変換は無効です
- 自動追従はオフです
- 基準UVがありません
- 再変換するテクスチャセットがありません
- UV の変更はありません
- UV の落ち着き待ちです

UV の自動起動時の文言は P2-8 に無い(あるのは Painter の書き出しを検知した文だけ)。別の文が要るなら指示がほしい。

## Q15. 既知の制約の文は P2-9

作業指示の「既知の制約の 1 文」は P2-9 にある。定数 `MSG_AUTO_INPUT_AS_IS` は「自動変換の入力は Painter の書き出し結果をそのまま使います」。
