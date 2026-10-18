# 指揮役への質問・記録(レーンA3 統合/E2E)

本物の core と本物のワーカーをつないだ統合テスト(`E2E_REPORT.md`)で見つかった食い違いと、契約と異なる挙動にした箇所の理由。
E1〜E4 は修正済み(最小限の修正)で、理由と、契約のどこが変わったかを記す。E5 以降は未解決で、判断または後続レーン(B2・Phase 2)への申し送り。
「修正前」の挙動は、統合前のワーカー(`3077b42`)に同じ合成データを渡して確かめた値である。

## E1. 画像 I/O が Pillow を必須にしていた(修正済み・契約 §0 / §5 を変更)

**食い違い**: 利用者 PC の mayapy(Python 3.11.9、numpy 1.26.4)には Pillow が無く、通常の Python も入っていない。統合前の `uvremap_io.py` は Pillow が無いと
すべての PNG に対して `UnsupportedFormat` を返した。同じ合成データで、Pillow 無しの環境の統合前ワーカーは、
UDIM 正常系を含む、画像を読む全シナリオが終了コード 11(「未対応形式: Pillow が見つかりません」)になった。さらに `test_uvremap_worker.py` は先頭の `pytest.importorskip("PIL")` で
全件がスキップされ、実機と同じ条件ではワーカーが一度も検証されていなかった。16bit のカラー/アルファ PNG も(Pillow が 8bit へ黙って落とすため)非対応だった(QUESTIONS_worker Q5)。

**修正**: `uvremap_io.py` の PNG 読み書きを `zlib` + `struct` + `numpy` だけの実装に置き換えた。色タイプ 0/2/4/6、8/16bit、非インターレース、
フィルタ 5 種(Average / Paeth は反対角線ごとにまとめて計算)、複数 IDAT、補助チャンクに対応。Pillow は import しない。API・meta のキーは契約 §5 のまま。
契約 §0(依存)と §5(必須対応)を、この内容に書き換えた。`test_uvremap_worker.py` の `importorskip("PIL")` は外し、Pillow が必要なテストだけを個別にスキップする。

**判断してほしい点(残る確認)**:
- 補助チャンクは保持しない(ガンマ、ICC、`sRGB`、`cHRM`、`pHYs`、`tRNS` など)。出力 PNG には色空間タグが付かない。Maya のファイルノードが
  ICC やガンマのタグを見て色空間を決める運用なら、タグ無しの出力で色空間の既定が変わる可能性がある。ファイルノードの色空間を B2 が明示設定するなら問題ない。
  Phase 0 の実機確認で、Painter の書き出しにどのチャンクが入るかと、ファイルノードの色空間の決め方を見てほしい。
- `tRNS`(パレット無しのカラーキー透過)は無視される。Painter は通常これを書かない。

## E2. ワーカーが `probe_verify` に `udim` と `normal_mode` を渡していなかった(修正済み)

**食い違い**: `convert_channel` には `udim` を渡していたが、`probe_verify` には何も渡さなかった。実際の core は `probe_verify` にも `udim` と `normal_mode` を要求する
(QUESTIONS_core Q4。「変換と同じ値を渡さないと不一致になる」)。統合前のワーカーに実 core をつなぐと次のようになった(Pillow のある環境、512²):

| シナリオ | 統合前 | 統合後 |
|---|---|---|
| `udim: false`(UV が 0〜1 の外にあるシェルを折り返す) | BaseColor・Normal とも probe `fail`(不一致 235/411)-> 終了コード 12。正しい変換を公開できない | 全チャンネル `pass`、終了コード 0 |
| `normal_mode: "off"` | Normal が probe `fail`(不一致 326/462)-> 12 | `pass`、0 |

**修正**: `uvremap_worker.py` の `_convert_all` で、`core.probe_verify` が `udim` / `normal_mode` を受け取る場合に、変換と同じ値(job の値)を渡す(`**probe_extra`)。
契約 §3 のシグネチャには無い引数だが、省略可能なキーワード引数で、受け取らない core では渡さない。契約と異なる挙動にはならない。

## E3. core の例外が、停止(11)ではなく「予期しない例外」(12)になっていた(修正済み)

**食い違い**: 契約 §4 の終了コードは `11 停止(ガード・Decision STOP・未対応形式)` / `12 失敗(例外・プローブ失敗・タイムアウト)`。
ワーカーは core の例外クラスを知らず、`ConversionStopped`(変換できる面が無い、Decision が STOP)も `TextureSetNotFound` も
「予期しない例外: ...」と長いトレースを付けて 12(`FAILED`)にしていた。`Decision.method == "STOP"` は 11(`BLOCKED`)になるのに、同じ「停止」の別経路が 12 になる矛盾である。
`SnapshotError`(壊れた/読めないスナップショット)も同じ「予期しない例外」で、原因が分かりにくかった。

**修正**(`uvremap_worker.py` の `_translate_core_error` / `_load_snapshot`):

| core の例外 | 終了コード / status / state | メッセージ |
|---|---|---|
| `ConversionStopped` | 11 / `blocked` / `BLOCKED` | 「変換を停止しました(理由: <reasons>)」 |
| `TextureSetNotFound` | 11 / `blocked` / `BLOCKED` | 「テクスチャセットがスナップショットにありません(ts_missing)」 |
| `ChannelFormatError` | 11 / `blocked` / `BLOCKED` | 「入力テクスチャの形式が変換できません」(例: 法線マップの原本が 1 チャンネル) |
| `SnapshotError`(基準/現行の読み込み) | 12 / `failed` / `FAILED` | 「基準スナップショットを読めません/不正です」 |
| それ以外 | 12(従来どおり「予期しない例外」) | 従来どおり |

core の例外は `uvremap_core` モジュールで定義されたクラス名で見分ける(core は遅延 import で、テストではスタブに差し替わるため、クラスの実体は比べない)。
同名の別の例外(例: 自作の `ConversionStopped`)は写像せず、従来どおり 12 にする。

**契約と異なる挙動になる点**: 契約は `ChannelFormatError` と `TextureSetNotFound` の扱いを決めていない。11 にした理由は、どちらも「入力か指定が合うまで再実行しても同じ結果になる」
ため、利用者に原因の修正を促す `BLOCKED` が適切で、再試行で直る失敗(12)とは区別したいから。`SnapshotError` は書き出し途中のファイルを読んだ場合などの再試行で直る可能性があるので 12 にした。
逆の方針(`ChannelFormatError` も 12)がよければ、`_translate_core_error` の 1 行で変えられる。

## E4. ledger / flag の `uv_hash` がメッシュ全体の値だった(修正済み・QUESTIONS_worker Q2 の決定)

**食い違い**: ワーカーは既定で `current_snapshot_dir/meta.json` の `uv_hash` を ledger と flag に書いていた。これはメッシュ全体の値で、別のテクスチャセットの UV を編集しても変わる。
D-3(「直前の正常世代が現行UVハッシュと一致しない場合は原本へ戻す」)で比べるのはテクスチャセット単位の値のはずで、複数 TS のメッシュで誤って「一致しない」(または一致する)判定になる。

**修正**: job が `uv_hash` を渡せばそれを優先する(従来どおり)。渡さなければ `core.compute_uv_hash(現行, ts_name)`(TS 単位)を使い、それが無い/失敗したときだけ `meta.json` の値に戻る。
テスト(`test_b_uv_hash_in_ledger_and_flag_is_per_texture_set`)は、2 TS のメッシュで ledger と flag の値が TS 単位であることを確かめる。
**B2 への申し送り**: Maya 側が D-3 で比べる値は、同じ関数 `compute_uv_hash(現行, ts_name)` で求めること(job で渡す場合も同じ)。

## E5. 低解像度(1024² 未満)のプローブ偽陽性が、そのまま終了コード 12 になる(未解決)

core は 512² 以下のテクセル単位の細部ノイズで、正しい変換でも `fail` が出得る(QUESTIONS_core Q1 の制約。`ProbeReport.notes` に `low_resolution_probe` が付く)。
ワーカーは `fail` を一律に公開停止(12)にし、`notes` を result.json に出さない。実アセットは 2048² 以上が想定で、統合テストの 512²(平滑な信号)と 1024²・2048²(細部あり)は
偽陽性が出なかったが、512² 以下の細部の多いテクスチャでは運用で止まる可能性がある。
**案**: `low_resolution_probe` 付きの `fail` は警告扱い(公開する)にするか、result.json の `probe` に `notes` を載せて Maya が判断する。どちらも契約の変更を伴うため、実機の Phase 0 で
実際の解像度を確認してから決めたい。

## E6. core の警告(`stats["warnings"]`)は result.json の `stats` にだけ出る(未解決)

UV の重なり(`uv_overlap`)、UDIM 境界またぎ(`udim_straddle`)、元テクスチャの無い面(`faces_no_source`)、法線反転の近似(`normals_flipped_approx`)などの警告は、
result.json の `stats[<suffix>]["warnings"]` に入るが、`messages` には出ない(`Decision.warnings` は `messages` に出る)。パネルが警告を表示するなら、B2 が `stats` を読む必要がある。
ワーカー側で `messages` へ写す変更は小さいが、新機能にあたるため入れていない。

## E7. 大きな解像度での時間とメモリ(未解決・D-5(3) の再設定に必要)

統合テストは 1024² と 2048²(4 タイル x 6 チャンネル)を測った(`E2E_REPORT.md`)。4096² は 4 タイル x 2 チャンネルと 4 タイル x 6 チャンネルを 1 回ずつ測った値が報告書にある。
所要時間は変換(core のラスタライズ)が支配的で、job の既定 `timeout_s`(300 秒)は 4096² の 6 チャンネルでは足りない可能性が高い。ピークメモリも数 GB になる(報告書の表)。
**申し送り**: B2 は解像度とチャンネル数から `timeout_s` を決めること。学校 PC の搭載メモリが分かれば、4096² の可否を判断できる。Phase 2(ラスタライズのテーブル再利用・並列化)の効果の測定は未実施。

## E8. 強制終了の検証範囲(確認)

統合テスト f(`slow`)は、**変換中**に `SIGKILL` した場合を実 core・実 CLI で検証する(出力・ledger・flag が不変、`status` は `CONVERTING` のまま、同じ job の再実行で復帰)。
**公開中**の `SIGKILL` はスタブ core のワーカーテスト(`test_i3_killed_worker_leaves_outputs_intact_and_next_run_recovers`)で検証済みで、公開は core に依存しないため、実 core でも同じ。
Windows の `TerminateProcess` 相当の確認(`SIGKILL` は POSIX 限定)は、実機(B1/B2)で行う必要がある。QUESTIONS_worker Q4(公開中の強制終了で新旧が混在し得る原理的な限界)は未解決のまま。
