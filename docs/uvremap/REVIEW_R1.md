# UV再マップ 独立レビュー R1

対象: `feat/uvremap-integ` 先頭 `4f3b3d2`（PR #18 マージ直後）。  
レビュー日: 2026-10-16。修正はしていない（本ファイルのみ追加）。

既知の食い違い（`status.lock`、`baseline/active.json`、`latest_generation/`、`uvremapTsName`、TS名=シェーダー名）と、既知の未対応（TSごとの並列、配列取得の繰り返し、`_call` の TypeError 再呼び出し、別名保存時の `scene_changed`、窓を閉じて開き直したあとにワーカーが「変換済み」と書く問題）は新規に数えない。影響が記録より大きいものだけ、理由つきで再掲する。

## 要約

- 件数: **Blocker 0 / High 3 / Medium 8 / Low 5 / Info 4**
- 最も重要な 3 点:
  1. **R1-01** `worker.lock` 待ち上限が既定 60 秒のまま、Maya は TS ごとにワーカーを並列起動し `latest_generation` を先に書く。2 回目の「今すぐ変換」や 2 つ目の TS で、公開ゼロ＋パネル `FAILED` になり得る。
  2. **R1-02** 公開の途中失敗で、既に置き換えたファイルのロールバックが失敗しても、古い `_sync_complete.flag` を戻す。新旧混在が「完了した世代」に見える。
  3. **R1-03** ワーカーが `status.json` を書かずに死ぬと、Maya はファイルノードを原本へ戻すが `FAILED` を書かず、同じ窓では `CONVERTING` のまま残る。
- 読めなかった範囲: テスト約 8000 行・実機スクリプト約 1700 行はサンプリング（異常系の網羅表は作ったが全行は未読）。`uvremap_core.py` のラスタライザ内部の全分岐、Maya/Painter 実機、Windows の共有違反そのものは未実行。`UVREMAP_SLOW=1` の遅いテストは未実行。

## 指摘

### R1-01 / High / 観点 1（I4+I5）、2、5

- **場所**: `sp_maya_livesync/uvremap_worker.py:72`, `:744-747`; `sp_maya_livesync/uvremap_runtime.py:846-849`; `sp_maya_livesync/uvremap_integration.py:421-430`
- **内容**: `worker.lock` は変換〜公開の全体を直列化するが、待ち上限の既定は 60 秒。Maya は TS ごとに `Popen` し、起動前に `latest_generation` を更新する。後続ジョブがロック待ちで 12 になると、先に動いていたジョブは公開直前の I4 で 10 になり、何も公開されない。
- **起きる場面**: (a) 変換中にもう一度「今すぐ変換」（同一 TS、世代 N と N+1）。(b) 複数 TS。1 つ目の変換が 60 秒を超えると 2 つ目は `FAILED`。4096² や UDIM 複数タイルでは 60 秒超はあり得る（A1 実測は 2048² 単一スレッドで変換 27 秒。4096² は未測定）。
- **根拠の種類**: **再現済み**（同一 TS、短い `lock_timeout_s` でスタブ core を sleep）。2 つ目の TS が 60 秒で落ちる時間そのものは **推測（実機確認が必要）**。
- **修正案**（費用: 小）: `lock_timeout_s` の既定を `timeout_s`（統合側は 1200）に合わせる。同一 TS の実行中は新しいジョブを作らない。ロック待ち失敗を、飛行中の低い世代より優先する `FAILED` にしない。
- **既知未対応との関係**: `QUESTIONS_integration.md` I10 は並列の CPU・メモリだけを記録している。ロック待ち 60 秒と `latest_generation` の組み合わせで **公開自体が消える**点は、記録より影響が大きい。

再現（一時フォルダ、スタブ core、リポジトリには未コミット）:

```
A exit: 10 status: superseded
B exit: 12 status: failed
status.json Body: state=FAILED, generation=2,
  message=worker.lock の取得がタイムアウトしました(0.4秒)
published files: []
ledger exists: False
```

### R1-02 / High / 観点 1（I3）、2

- **場所**: `sp_maya_livesync/uvremap_worker.py:1173-1247`; `sp_maya_livesync/uvremap_io.py:423-443`
- **内容**: I3 は失敗時に出力と台帳を変えないとしている。公開はファイル単位の `os.replace` で、例外時は undo を回す。undo の失敗はログして握りつぶし、そのあと古い `_sync_complete.flag` を戻す。一部だけ新ファイルのまま、flag は旧世代、という状態を「完了」と誤認し得る。
- **起きる場面**: 2 回目以降の変換で、Maya が `output/final` の PNG を開いたまま（ファイルノードが変換後を指している）→ Windows で `os.replace` が共有違反。1 枚目は置き換わり、2 枚目で失敗、1 枚目の復元も失敗、flag だけ旧世代に戻る。
- **根拠の種類**: ロールバック失敗でも flag を戻すことは **再現済み**（`os.replace` を注入）。Maya が実際にハンドルを持ち続けることは **推測（実機確認が必要）**。
- **修正案**（費用: 小〜中）: undo が 1 件でも失敗したら flag を戻さない。Windows では公開先を世代フォルダにしてポインタだけ替える（Q4 で言及済み、契約変更）。`os.replace` の再試行を共有違反向けに長くする。
- **Q4 との差**: Q4 は強制終了で flag が無いことを前提にしている。こちらは **例外経路が flag を戻す**ため、Q4 より悪い。

再現（注入。1001 は新内容のまま、1002 は旧、flag は世代 1）:

```
gen2 exit: 12 failed
1001 changed from gen1 (new content left behind): True
1002 changed from gen1: False
flag restored to gen1 bytes: True
flag generation: 1
ledger generation: 1
MIXED_FILES_WITH_OLD_FLAG: True
```

### R1-03 / High / 観点 1（D-3 / D-1）、4

- **場所**: `sp_maya_livesync/uvremap_runtime.py:918-1024`, `:669-707`; `sp_maya_livesync/uvremap_integration.py:474-487`, `:650-662`
- **内容**: 終了コードが 0/10/11/12/13 以外、または `result.json` が無いとき、D-3 の原本復帰は行うが `status.json` へ `FAILED` を書かない。ワーカーが `CONVERTING` のまま死ぬと、同じ窓の `poll` では回収されない。`recover_orphaned` は attach / シーン変更時だけで、書くのは `ORIGINAL`（中断メッセージ）であり契約の `FAILED` ではない。
- **起きる場面**: 変換中のタスクマネージャ強制終了、`mayapy` が worker `main` に入る前に落ちる、ウォッチドッグ `os._exit` 前のクラッシュ。パネルは最大 10 分 `CONVERTING`（`CONVERTING_STALE_SECONDS`）に見える。
- **根拠の種類**: **再現済み**（偽 `cmds` + `poll()` が 1 を返す偽プロセス）。実機の mayapy 起動失敗は **推測（実機確認が必要）**。
- **修正案**（費用: 小）: `_finish_job` でワーカーが終端状態を書いていなければ `FAILED` を書く。`poll` 完了時にも孤立 `CONVERTING` を回収する。

再現:

```
status after start: CONVERTING
events: exit_code=1, state=FAILED, reverted=['file_body']
status after poll: CONVERTING msg: (空)
file node path: .../src/Body_BaseColor.png   # ノードは原本へ戻っている
running jobs after poll: []
recover_orphaned (attach 時のみ): ['Body'] → ORIGINAL
```

既知の「窓を閉じて開き直す」経路とは別。同じセッションで `poll` がジョブを捨てたあと、回収が走らない。

### R1-04 / Medium / 観点 3

- **場所**: `sp_maya_livesync/uvremap_worker.py:1139-1202`
- **内容**: 台帳 `outputs` のキーを `output_dir / name` に結合するだけで、`..` や区切りを拒否しない。I6 の手編集退避・不要ファイル削除が `output/final` の外へ出られる。
- **起きる場面**: 壊れた / 手編集された `ledger.json` に `../secret.txt` がある状態で次のジョブが公開する → `uvremap_root/output/secret.txt` が `.edited_*` へ退避され、元の場所から消える。
- **根拠の種類**: **再現済み**
- **修正案**（費用: 小）: 出力名は `Path(name).name == name` かつ `..` 無し。`output_dir` は `uvremap_root/output` 配下に限定する。

再現: 台帳に `"../secret.txt"` を入れて世代 2 を実行 → `output/secret.txt` は消え、`secret.txt.edited_*` が `output/` に残った。ジョブは終了コード 0。

### R1-05 / Medium / 観点 1（D-1）

- **場所**: `sp_maya_livesync/uvremap_bridge.py:410-438`（定義のみ）; `sp_maya_livesync/uvremap_runtime.py:28-32`（`STATE_STALE` 無し）
- **内容**: 契約は Maya が `ORIGINAL` / `STALE` を決める。`decide_display_state` は不一致を `STALE` にするが、本番の呼び出しはテストだけ。変換後に UV を動かしても `status.json` は `CONVERTED_OK` のまま。パネルは変換後テクスチャを「正しい」と見せる。
- **起きる場面**: 手動 MVP で変換したあと UV を編集し、再変換せずにビューポートを見る。
- **根拠の種類**: **コードで確認**
- **修正案**（費用: 中）: 更新または poll で ledger の `uv_hash` と現行を比べ、不一致なら `STALE`。配列取得のコストは既知未対応と重なる。

### R1-06 / Medium / 観点 4

- **場所**: `sp_maya_livesync/maya_live_sync.py:3856-3877`（`detect_quality_by_project`）; 書き換えスキップは `:2771` ほか
- **内容**: 変換後ノードは watch/Final のどちらにも属さない。品質判定のキーが立たず、品質ボタンはスキップ実装のため無操作になり得る。パスの巻き戻しは防いでいる。
- **起きる場面**: 変換成功後に表示品質ボタン、または起動時の品質検出。
- **根拠の種類**: **コードで確認**。見た目は **推測（実機確認が必要）**。`QUESTIONS_integration.md` I3 に「実機必須」と既記。既知リストには無かったため、未修正のままマージされた点として載せる。
- **修正案**（費用: 中）: 判定・シェーダー検索では `uvremapOriginalPath` を使う。5 箇所のスキップを 1 ヘルパにまとめる。

### R1-07 / Medium / 観点 3、4

- **場所**: `sp_maya_livesync/uvremap_maya.py:572-582`; `sp_maya_livesync/uvremap_worker.py:345-350`; `sp_maya_livesync/uvremap_integration.py:84-110`
- **内容**: TS 名はシェーダー名（既知）。名前空間付き `ns:Shader` はワーカーの `_safe_component` が `:` を拒否する（終了 12）。統合の `resolve_targets` は Painter 側のキーと完全一致なので、名前空間付きは対象外にもなる。
- **起きる場面**: 参照アセット / 名前空間付きキャラ。`QUESTIONS_tsname.md` T1 は Painter 側の名前を未確認のまま正規化していない。
- **根拠の種類**: `:` 拒否は **再現済み**。学校アセットで実際に名前空間が付くかは **推測（実機確認が必要）**。
- **修正案**（費用: 小）: ファイル名には短いシェーダー名（`:` より後ろ）を使い、Painter の TS 名と合わせる。

### R1-08 / Medium / 観点 2

- **場所**: `sp_maya_livesync/uvremap_worker.py:807-808`
- **内容**: 入力ファイルの正規表現が大小文字を区別する。Windows のファイルシステムは区別しないことが多いが、一覧の実名とジョブの `ts_name` がずれればチャンネルはスキップされ、全ミスで終了 11。
- **起きる場面**: ディスク上が `body_BaseColor.1001.png`、ジョブが `ts_name=Body`。
- **根拠の種類**: Linux 上で **再現済み**（終了 11「入力テクスチャが見つかりません」）。Painter が実際にケースをずらすかは **推測（実機確認が必要）**。
- **修正案**（費用: 小）: Windows では `re.IGNORECASE`、コピーは `listdir` の実名を使う。

### R1-09 / Medium / 観点 4

- **場所**: `sp_maya_livesync/uvremap_runtime.py:376-448`（`switch_ts_file_nodes` / `revert_ts_file_nodes`）。対比: `maya_live_sync.py:2765-2766`
- **内容**: 既存の再読込は `undoInfo(stateWithoutFlush=...)` で囲む。切替・復帰の `setAttr` / `addAttr` / `deleteAttr` は囲まない。1 秒タイマー完了が Undo スタックに載り、Undo でパスと属性がずれる余地がある。
- **起きる場面**: 変換完了またはキルスイッチ OFF の直後に Undo。
- **根拠の種類**: パターンの差は **コードで確認**。Undo の実際のずれは **推測（実機確認が必要）**。
- **修正案**（費用: 小）: `reload_textures` と同じく、呼び出し前の `stateWithoutFlush` を保存して `finally` で戻す。

### R1-10 / Medium / 観点 4、8

- **場所**: `sp_maya_livesync/install.py:462-526`（`_destroy_stale_livesync_window`）; `maya_live_sync.py:5799` 付近の `closeEvent` → `_uvremap.shutdown()`
- **内容**: 再インストール時の古い窓破棄はシーンコールバック解除と `watcher.stop()` を複製するが、`_uvremap.shutdown()` を呼ばない。poll の `QTimer` はグループボックス付き。C1 と同様、`deleteUI` 後の死んだ QObject、または `closeEvent` 未発火でのタイマー残留があり得る。
- **起きる場面**: Live Sync 窓を開いたまま `install.py` をドラッグ&ドロップ。
- **根拠の種類**: shutdown 欠落は **コードで確認**。どの破棄経路が勝つかは **推測（実機確認が必要）**（過去の C1 と同型）。
- **修正案**（費用: 小）: `watcher.stop()` の隣で `getattr(window, "_uvremap", None)` を `try/except` 付きで `shutdown()`。

### R1-11 / Medium / 観点 4、5

- **場所**: `sp_maya_livesync/uvremap_maya.py:599-635`, `:721-722`; 設計書 5.2（Orig を使う）
- **内容**: 設計はスキン等で中間（Orig）のオブジェクト空間位置を使い、中間シェイプは列挙から除外する。実装はライブシェイプの `getPoints(kObject)` で、Orig は読まない。検出は `geometryFilter`（ほぼ常に `tweak` を含む）なので、通常の履歴付きメッシュでも警告が付きやすい。`MFn.kGeometryFilt` は名前が短く、属性が無ければ OpenMaya 経路は何もしない。
- **起きる場面**: スキン / blendShape を別ポーズで基準と現行に取ると bbox 警告。変換自体は UV 空間なのでテクスチャは動くことが多い。
- **根拠の種類**: Orig 未使用は **コードで確認**。`kGeometryFilt` と tweak の実機挙動は **推測（実機確認が必要）**。
- **修正案**（費用: 小）: skinCluster / blendShape だけを検出する。Orig は `MFnGeometryFilter.inputShapeAt` 等、確定した取り方で読む。

### R1-12 / Low / 観点 2、3

- **場所**: `sp_maya_livesync/uvremap_worker.py:345-350`; 対比 `uvremap_runtime.py:202-221`
- **内容**: ワーカーの `_safe_component` は空・`.` / `..` / `/` `\` `:` NUL だけを拒む。`CON` / `NUL`、末尾ドット/空白、`<>"|?*`、長さは通す。runtime は `latest_generation` 用に予約名を落としているが、ジョブの `ts_name` には使っていない。
- **起きる場面**: 珍しいシェーダー名 → Windows で `OSError` 12。
- **根拠の種類**: 許可リストは **コードで確認**。実アセットで当たるかは **推測**。
- **修正案**（費用: 小）: runtime の予約名チェックをワーカーの出力名にも使う。

### R1-13 / Low / 観点 6

- **場所**: `sp_maya_livesync/uvremap_integration.py:510-515`, `uvremap_panel.py:288-293` vs `maya_live_sync.py:33-40`
- **内容**: Live Sync は PySide2→PySide6、uvremap は PySide6→PySide2。両方 import できるとバインディングが分かれ、`addWidget` が例外 → パネルだけ欠ける（本体は try/except で生きる）。
- **起きる場面**: 両方のバインディングが `sys.path` にある環境。Maya 2022/2025 で片方だけのときは問題にならない。
- **根拠の種類**: **コードで確認**。対象 PC で両方あるかは **推測（実機確認が必要）**。
- **修正案**（費用: 小）: Live Sync と同じ順（PySide2 優先）にする。

### R1-14 / Low / 観点 8

- **場所**: `MSG_INTERRUPTED` ほかが `uvremap_runtime.py:42` と `uvremap_integration.py:28` に重複。`uvremap_runtime.py:6` の「既存の maya_live_sync.py は変更しない」は PR #18 以降不正確。
- **内容**: 操作者向け日本語がファイル間でずれ得る。巨大関数（`_publish` / `convert_now` / `attach`）は触るときだけ分割で足りる。
- **起きる場面**: 文言修正を片方だけしたとき。
- **根拠の種類**: **コードで確認**
- **修正案**（費用: 小）: メッセージを 1 モジュールへ。コメント 1 行を消す。

### R1-15 / Low / 観点 1、5

- **場所**: `sp_maya_livesync/uvremap_core.py:57`, `:1339-1348`
- **内容**: 契約 §3.1.5 は島境界から 2 テクセル除外。実装は縮小格子で 2 セル侵食するため、4096² では約 16 テクセル。偽陽性は減るが、小さい島は `n_valid` が減り inconclusive に寄りやすい。`PROBE_CALIBRATION.md` にはある。
- **起きる場面**: 常時。小さい UV 島。
- **根拠の種類**: **コードで確認**
- **修正案**（費用: 小）: フル解像度で 2 テクセル侵食するか、契約文を実装に合わせる。

### R1-16 / Info / 観点 5

- **場所**: ラスタ `uvremap_core.py:737-739`（`(H,W) int32` + `(H,W,2) float32`）; 変換 `:1047-1051`（sRGB の `lin_cache` 全コピー）; PNG 書き `:391-399`
- **内容**: 設計 4.4「処理中のタイルのみ」に対し、1 チャンネルの全タイルを同時に持つ。4096² RGBA float32 は 1 タイル 256MB。UDIM 4 枚の sRGB では src + lin_cache + 出力で GB 級になり得る（測定なし）。並列 TS（既知）が重なるとさらに増える。
- **起きる場面**: 4096² × 複数 UDIM × BaseColor。
- **根拠の種類**: **推測**（コード上の配列形状からの見積もり。プロファイラ未実行）
- **修正案**（費用: 中）: タイル単位で書いてから捨てる。`lin_cache` を全タイルで持たない。

### R1-17 / Info / 観点 6

- **場所**: `sp_maya_livesync/install.py:30`（Maya 2022 以降）; `uvremap_core.py:29`（`typing.Literal`）
- **内容**: 契約は構文 3.10+。`from __future__ import annotations` があるため注釈の `list[str]` / `X | Y` は実行時に評価されない。実行時に 3.8+ が要るのは `Literal` の import。`match` / `removeprefix` / `zoneinfo` / walrus は uvremap モジュールに無い。Maya 2022（3.7）で core を import すると失敗するが、`maya_live_sync` の attach は try/except なので本体は起動する。対象機は Maya 2026/2027（3.11）。
- **根拠の種類**: **コードで確認**
- **修正案**（費用: 小）: インストーラの対象表記を「Live Sync は 2022+、UV再マップは mayapy 3.10+ / Maya 2025+」と分ける。急がなくてよい。

### R1-18 / Info / 観点 7

- **場所**: `sp_maya_livesync/tests/test_uvremap_runtime.py:663`（`_popen_ok_with_files`）; `uvremap_io.py:438`（`OSError` 全般を再試行）
- **内容**: 実行制御の成功 poll テストは `result.json` と終了 0 だけで、ワーカーが書く `CONVERTED_OK` を断言しない。ディスクフル（`ENOSPC`）は共有違反と同じ 5×0.2s 再試行。公開中の OS キルは in-process `BaseException` で近似（変換中の SIGKILL は別テストあり）。
- **根拠の種類**: **コードで確認**
- **修正案**（費用: 小）: FakePopen が status 行も書く。`ENOSPC` は即失敗。

### R1-19 / Info / 観点 9

- **場所**（値は書かない）:
  - `sp_maya_livesync/Easy_install.md:325` — メールアドレス（既知の疑いどおり）
  - `sp_maya_livesync/README.md:752` — メールアドレス
  - `docs/uvremap/uv_remap_design_v0.2.md:4` — GitHub のユーザー/組織識別子
- **内容**: ソース・テスト・本対象範囲のコミット件名に、ホームディレクトリの絶対パスや認証情報は見つからなかった。ワーカーはログからホームパスを落とす。
- **根拠の種類**: **コードで確認**
- **修正案**（費用: 小）: 連絡先をリポジトリから外すか、個人に紐づかない窓口にする。

## 観点ごとの確認結果（指摘が無い／限定的なもの）

1. **契約**: I1（原本は読むだけ、出力が `source_dir` 配下なら 11）はコードとワーカーテストで満たしている。I2 コピー＋ flag mtime、I5 ロック、I6 手編集退避、I7 例外を `result.json` に残す、I8（ワーカーはサブプロセスを起動しない）は大枠どおり。終了コード 0/10/11/12/13 の対応は実装と一致（CLI 用法ミスだけ argparse の 2）。D-4 キルスイッチ既定 false、OFF で原本復帰、はコードで確認。D-2 プローブの τ=0.10・面積比例・箱型平均は実装済み。I3/I4/D-1/D-3 の穴は R1-01〜03、05。
2. **Windows**: `os.replace` 再試行、`CREATE_NO_WINDOW`、環境変数除去、UTF-8 JSON、stdout `errors="replace"` はある。Maya 側は子プロセスを kill しない（タイムアウトはワーカー内）。共有違反の本番経路は R1-02。
3. **安全**: 統合の `work_folder_is_safe` は root/output が `final_dir` / `watch_dir` / `final_export_dir` の内側なら止める。ワーカー I1 は `source_dir` と `uvremap_root/output` のみで、Live の watch は知らない（統合が先に見る前提）。JSON の `job_id` / `ts_name` / `suffix` はサニタイズする。台帳キーは R1-04。
4. **Maya**: `cmds` はボタンと `QTimer`（GUI スレッド）から。ワーカーは別プロセス。import 隔離: `maya_live_sync` はモジュール先頭で uvremap / numpy を import せず、窓構築時の try/except。複数メッシュは UUID 順。インスタンスは 0 番、不一致は警告。UDIM 切替は `<UDIM>` と `uvTilingMode=3`。中間シェイプは `list_texture_sets` が除外。`collect_arrays` 自体は呼び出し元の path を信じる（本番は list 経由なので実害は小さい）。
5. **数値**: NaN/非有限 UV は除外。ゼロ面積 UV 三角形はラスタでスキップ。測定はしていない。見積もりの根拠は R1-16。
6. **互換**: Pillow / scipy / OIIO 非依存はテスト（本環境は Pillow 無しで 498 件成功）で確認。numpy 1.26.4 で core/io が動く。
7. **テスト**: I1〜I8 の多く、壊れた JSON/PNG、ロック競合、変換中 SIGTERM/SIGKILL、公開失敗ロールバック（成功する undo）、空 TS、プローブ fail はカバーしている。ワーカーテストは stub core、e2e は実 core。時計は注入されているものが多い。抜けは R1-18。
8. **保守**: 重複メッセージとコメントの陳腐化は R1-14。死んだ本番経路は `decide_display_state`（テスト専用）。
9. **個人情報**: R1-19。認証情報は無し。

## 修正の優先順（最大 10）

1. R1-01 — ロック待ちを `timeout_s` に合わせ、同一 TS の二重起動を止める（小、公開ゼロを防ぐ）
2. R1-02 — undo 失敗時は flag を戻さない（小、Windows の 2 回目変換）
3. R1-03 — `_finish_job` が終端状態を書く（小、強制終了後の表示）
4. R1-07 — 名前空間付きシェーダー名の正規化（小、参照アセット）
5. R1-09 — 切替を `undoInfo` で囲む（小、既存 Live Sync と同じ）
6. R1-10 — インストーラで `_uvremap.shutdown()`（小、再 D&D）
7. R1-04 — 台帳キーと `output_dir` の閉じ込め（小）
8. R1-08 — Windows の入力名を大小無視（小）
9. R1-05 / R1-06 — `STALE` と品質判定（中、実機と相談）
10. R1-11 — Orig 位置（小〜中、スキン実アセットが必要になってから）

## 実機で確認すべき項目

1. 学校 PC（Maya 2026）と自宅（Maya 2027）で、キルスイッチ OFF のまま Live Sync が以前と同じこと（D-4 / D-5.4）。
2. 変換中にタスクマネージャで `mayapy` を落とす → 出力が壊れないこと、パネルが `CONVERTING` のまま残らないこと（R1-03、D-5.2）。
3. 変換後テクスチャを表示したまま 2 回目の UV 編集→変換。Windows で `os.replace` 共有違反と、失敗時の出力/flag（R1-02）。
4. テクスチャセット 2 つ以上、2048² と 4096² で「今すぐ変換」。2 つ目が 60 秒で `FAILED` にならないか（R1-01）。所要時間とピークメモリ。
5. 変換中にもう一度「今すぐ変換」。公開ゼロにならないか（R1-01）。
6. 名前空間付きシェーダー / 参照アセットで TS 名が Painter と一致するか、ジョブが `:` で 12 にならないか（R1-07、T1）。
7. 変換後の表示品質ボタンと起動時の品質検出（R1-06、I3）。
8. 変換完了直後の Undo でファイルノードが壊れないか（R1-09）。
9. スキンメッシュで基準と現行を別ポーズにした場合の bbox / 見た目（R1-11）。
10. 窓を開いたまま `install.py` を再ドロップしたあとの poll タイマー（R1-10）。
11. GUI から起動した 2 つ目の `mayapy` がライセンス/初期化で固まらないか（ウォッチドッグ前）。
12. D-5.1: 実アセットで移動・回転・反転を 10 回、プローブ不一致ゼロ。
13. ファイルノード切替後の colorSpace（`QUESTIONS_runtime.md` R5。本レビューでは新規に数えず）。

## テスト結果

除外: `sp_maya_livesync/tests/test_maya_live_sync.py`（Maya 必須）。

| 環境 | 結果 |
|---|---|
| CPython 3.12.3 + numpy 1.26.4、Pillow 無し | **498 passed, 25 skipped**（約 73 秒） |
| CPython 3.11.9 + numpy 1.26.4、Pillow 無し | **498 passed, 25 skipped**（約 72 秒） |

スキップ内訳（両環境とも同じ）: `UVREMAP_SLOW=1` 未設定が 12、PySide6 無しが 9、Pillow 無しが 4。失敗 0。
