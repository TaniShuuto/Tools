# ワーカー実装メモ(レーンA2)

契約 `CONTRACT.md` §4・§5 を実装した `uvremap_worker.py` / `uvremap_io.py` の、契約が定めていない決定の記録。
契約と食い違う点は `QUESTIONS.md` 側に書いた。ここは「契約の範囲内で補った部分」。

## 1. job.json の追加フィールド(すべて任意。無ければ既定値)

| キー | 既定 | 意味 |
|---|---|---|
| `source_flag` | なし | 契約どおり。無い/ファイルが存在しない場合は警告して続行(コピー前後の変化検知は行えない) |
| `lock_timeout_s` | 60 | `worker.lock` の待機上限(超過で終了コード 12) |
| `hard_kill_grace_s` | 15 | `timeout_s` 超過後、協調的に止まらない処理を強制終了するまでの猶予 |
| `latest_generation_path` | なし | Maya が「最新に要求した世代」を書く小さなファイル(整数、または `{"generation": N}`)。N > job.generation なら、開始時と公開直前に 10 で破棄する |
| `uv_hash` | `core.compute_uv_hash(現行, ts_name)`(TS 単位)。core に無ければ `current_snapshot_dir/meta.json` の `uv_hash` | ledger / flag に書く UV ハッシュ。job が渡せばそれを優先(A3 で既定を TS 単位に変更、QUESTIONS_e2e.md E4) |
| `baseline_id` | `baseline_dir/meta.json` の `baseline_id`、無ければフォルダ名 | ledger の `baseline_id` |
| `channels[].normal_green_down` | false | `ChannelSpec.normal_green_down` へそのまま渡す |

`udim` は、`core.convert_channel` と `core.probe_verify` が `udim` キーワード引数(または `**kwargs`)を受け取る場合に渡す。`normal_mode` も `probe_verify` に渡す(変換と同じ値でなければプローブが別の変換と比べてしまう。A3 で追加、QUESTIONS_e2e.md E2)。契約 §3 のシグネチャには無いが、実際の core は受け取る(QUESTIONS_core Q4)。

## 2. 意味の補足

- **`copy_retries`**: 「最初の1回 + やり直し `copy_retries` 回」。既定 3 なら最大 4 回コピーし、すべて source_flag が変化していれば 13。`0` なら 1 回だけ。
- **世代比較**: `job.generation < ledger.generation` のときだけ破棄(10)。同じ世代の再実行は許可。ledger の `generation` は成功時に `max(job.generation, 旧値)` へ更新、`last_good_generation` は job の世代。失敗では ledger を変更しない。
- **出力ファイル名**: 全チャンネルの新UVタイル集合の和が 1 つなら `<ts>_<suffix><ext>`、複数なら `<ts>_<suffix>.<tile><ext>`。拡張子は原本(最新側)と同じ。入力タイル番号は出力名に影響しない。チャンネル間でタイル集合が違う場合は警告。
- **入力の探索**: 拡張子はチャンネルごとに最新のものを採用(複数あれば警告)。タイル付きと無印が混在する場合は新しい側を採用して警告。タイル無し入力は 1001 として扱う。
- **ロック**: `worker.lock`(全体)、`ledger.lock`(公開区間)、`status.lock`(status.json 更新)。いずれも OS レベルのロックなので、プロセスが死ねば自動解放される(stale lock の掃除は不要)。
- **結果ファイルの置き場所**: `result.json` は `jobs/<job_id>/result.json` と `job.json` の隣(通常は同じ場所)。`job.json` 自体が読めない場合も、`job.json` の隣に書く。**原本フォルダ配下には、ガード違反時も含めて何も書かない**。
- **個人情報**: ログ・result.json・status.json の文言からはホームディレクトリ形式の絶対パスを `~` に置換する。例外トレースはファイル名と行番号のみ。
- **status.json の追加項目**: 各 TS 行に `job_id` と `pid` を追加(契約の項目に加えるだけ)。Maya が「ワーカーが死んで CONVERTING のまま残った」状態を判定するために使える。新しい世代の行(`generation` が自分より大きい)は書き戻さない。破棄(10)の場合は、自分が書いた CONVERTING を元の行へ戻す。
- **ジョブ作業フォルダ**: `jobs/<job_id>/{input,staging,backup}`。成功時は `input` も消す。失敗時は診断用に `input` だけ残す(`staging`/`backup` は消す)。古い `jobs/` の掃除は Maya 側(設計書 5.1)。

## 3. 公開(I3/I6)の手順と障害時の振る舞い

1. 全チャンネル変換 → プローブ全チャンネル → ステージング(`jobs/<id>/staging`)。この間は出力フォルダ・ledger に一切触れない。
2. `ledger.lock` を取り、世代を再確認(I4)。出力フォルダ内に `*.uvremap-tmp` として一時ファイルを作り、sha1 を検証。
3. `publish_journal.<ts>.json`(新出力の sha1)を `uvremap_root` に書く。
4. 手編集の退避(I6) → 旧 flag を外す → 出力を `os.replace`(Windows の失敗は最大 5 回×0.2 秒再試行) → 不要になった旧タイル出力を削除 → ledger 更新 → **flag を最後に**書く。
5. 手順 4 の途中で例外が出たら、バックアップから出力・flag・ledger・退避ファイルを元に戻して 12。

**プロセスごと死んだ場合(SIGKILL・電源断)**: 手順 4 の途中なら出力が新旧混在し得るが、flag は外れたままなので「完了」には見えない。次の実行は、残った一時ファイルを掃除し、`publish_journal` に書かれた sha1 は「手編集ではない」と認めるため、誤って `.edited_` へ退避することなく新しい完全な世代を公開する。変換中(手順 1〜2)に死んだ場合は出力・ledger とも無変更。

## 4. 画像 I/O(`uvremap_io.py`、A3 で Pillow 非依存の自前実装に置き換え)

実機の mayapy(3.11 / numpy 1.26.4)には Pillow が無いため、PNG の読み書きは `zlib` + `struct` + `numpy` だけで行う(QUESTIONS_e2e.md E1)。
Pillow が入っている環境でも使わない(`PIL` を import しない)ので、環境による結果の差は出ない。Pillow はテストでの照合にだけ使う。

- **対応**: 色タイプ 0 / 2 / 4 / 6、ビット深度 8 と 16、非インターレース、フィルタ 5 種(行ごとに混在可)、複数 IDAT(長さ 0 も可)、
  補助チャンク(読み飛ばす。CRC は検査する)、IEND 後の余分なバイト(無視)。
- **対応外**: パレット、インターレース、ビット深度 1/2/4、未知の重要チャンク(先頭が大文字)、PNG 以外 -> `UnsupportedFormat`(終了コード 11)。
  ヘッダが PNG として正しく、本体が壊れている(CRC 不一致、チャンク欠落・途中切れ、zlib 破損、データ量の不一致、フィルタ種別が不正)-> `ImageIOError`(12)。
  IHDR の CRC 不一致も、署名と IHDR の形が正しければ 12。
- **読み込みの速度**: Sub は `cumsum`、Up は行の加算、None は何もしない。Average / Paeth は画素 (y, x) が (y, x-1)・(y-1, x)・(y-1, x-1) だけに依存するので、
  **反対角線(y + x が同じ画素)ごとにまとめて**計算する(同じ反対角線上の画素は互いに独立で、依存先は前の 2 本にある)。画素単位の Python ループは無い。
  結果は PNG 仕様 9.2 そのままの素朴な参照実装(`tests/png_testkit.py`)とビット一致する(`tests/test_uvremap_io.py`)。
  4096² RGBA 8bit で、Paeth 全行の読み込みは約 3 秒(E2E_REPORT.md の表)。
- **書き込み**: 原本(`meta`)と同じビット深度・モード。16bit は big-endian。値は 0..1 にクリップ(NaN は 0)して `rint` で丸める。
  フィルタは行ごとに None / Sub / Up のうち「絶対値の和が最小」を選び、zlib は level 4、IDAT は 1 MB ごとに分割する。
- **保持しないもの**: 補助チャンク(`gAMA` / `cHRM` / `sRGB` / `iCCP` / `pHYs` / `tEXt` / `tIME` / `bKGD` / `tRNS` など)は読み飛ばし、書き出さない。
  出力 PNG は IHDR + IDAT + IEND だけで、色空間のタグ(ICC・ガンマ)を持たない。画素値は原本と同じ規約のまま(sRGB / raw は job の `colorspace` で決まる)。
  `tRNS`(パレット無しのカラーキー透過)も無視される。Painter は通常これらを出力しないが、Maya のファイルノードが ICC を参照する運用がある場合は要確認(QUESTIONS_e2e.md E1)。
- **メモリ**: 4096² RGBA 8bit の読み込みでピーク約 400 MB(float32 の配列 268 MB を含む)、書き込みでピーク約 440 MB。

