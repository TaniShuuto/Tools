# 指揮役への質問・記録(レーン D2b Live Sync 組み込み)

契約 `CONTRACT.md` §6、`QUESTIONS_runtime.md`、既存の `uvremap_runtime.py` /
`uvremap_panel.py` / `uvremap_maya.py` の公開部分と、`maya_live_sync.py` の
指定箇所を読んで組み込んだ。既存 Live Sync の監視・再読込・品質切替の意味は
変えない方針。ここで勝手に決めず、確認結果と判断を残す。

## I1. `save_config` のマージ(他キーを消さない)

`save_config(cfg)` は「ディスク上の最新 JSON を読む → `merged.update(cfg)` →
一時ファイル経由で原子置換」(`_merge_and_write_config`)。呼び出し元が渡した
キーだけが上書きされ、ディスク上の他キーは残る。

`load_config()` は `dict(DEFAULT_CONFIG)` の上にディスク内容を `update` する。
ディスクに無い新キー(`uvremap_enabled` 等)は、メモリ上では DEFAULT の値が入る。
ディスクへは、こちらが `save_config({"uvremap_enabled": ...})` したときだけ書かれる。

キルスイッチの保存は `LiveSyncWatcher.save_mapping_only`(メモリ更新 +
`save_config`、監視の再起動なし)を使う。Painter 側と共有する設定ファイルなので、
全キーの上書き保存はしていない。

ロック取得がタイムアウトしたときは、従来どおりロック無しで同じマージを 1 回行う。

## I2. 未保存シーンの識別子は空文字同士で一致する

`UVRemapRuntime` の `scene_id_fn` 既定は `cmds.file(query=True, sceneName=True)`。
未保存は空文字。ジョブ記録時と `poll()` 完了処理時を比べ、不一致なら切替も
原本復帰もせず、`warning=scene_changed`、当該 TS の status を `ORIGINAL`、
メッセージ「シーンが変わったため切り替えを見送りました。もう一度『今すぐ変換』を
押してください」。

未保存のシーン同士は空文字同士になるため区別できない。別の未保存シーンへ
切り替えても、完了処理は通常どおりファイルノードを切り替える。
保存済みパス同士、または保存済みと未保存の組み合わせだけが不一致になる。

## I3. 干渉の確認(fileTextureName を書き換える処理)

読んだ関数と結論。属性名 `uvremapOriginalPath` は runtime 側の公開名のまま。

書き換えがある(スキップを入れた):

- `LiveSyncWatcher.reload_textures`(ループ先頭、`file_nodes` 走査)
- `LiveSyncWatcher.reload_final_textures`(同上)
- `LiveSyncWatcher.switch_texture_quality`(対象収集の 2 ループと、実際の
  `setAttr` ループ。3 か所)

変換後ノードの `fileTextureName` は既定では
`<uvremap_root_dir>/<Finalサブフォルダ>/output/final` を指し、
`watch_dir` / `final_export_dir` の外になる。この場合、フォルダ一致だけでは
上記ループに入らない。ただし作業フォルダを監視/Final の内側に置いた誤設定や、
ファイル名 prefix のフォールバック経路では書き換え得るため、属性がある
ノードは触らないようにした。各所 5 行以内。

書き換えが無い(変更していない):

- `backup_final_textures_to_sourceimages`: ファイルコピーのみ。file ノードの
  パスは変えない。
- `create_shader_network`(fileTextureName を書く 4177 行付近): 新規ノード作成。
  変換後ノードを上書きする処理ではない。
- `is_texture_set_mapped`: 読み取りのみ。

判断に迷ったため変更していない:

- `find_orphan_file_nodes`(3784 行付近): 書き換えはしない。変換後パスは
  `managed_dirs`(watch/Final)の外なので、既定では孤立にも載らない。
  もし作業フォルダが `managed_dirs` の内側だと、ファイル名は
  `<ts>_<suffix>` のままなので prefix 一致で孤立にはならない想定。
  誤検出の実機確認は未実行。
- `detect_quality_by_project`(3818 行付近): 読み取りのみ。変換後ノードは
  watch/Final のどちらにも属さないため、全ノードが変換後だと品質判定の
  キーが立たない可能性がある。品質ボタンの見た目への影響は実機必須。
- `is_texture_set_mapped` のパス照合: 変換後パスは `managed_dirs` 外なので、
  シェーダー対応の記録が無いと「未対応」に見えうる。SG マップがあれば先に
  True を返す既存経路がある。実機必須。

## I4. 基準UVは自動記録しない。force 再記録は UI に出さない

「基準UVを記録」は利用者が押す。確認ダイアログは画面側
(`handle_record_request` / `decide_baseline_prompt`)。コントローラは確認を
持たない。既にあれば上書きせず、「基準は記録済みです。作り直すには…」と返す。
`force=True` の再記録ボタンは、このタスクでは出していない。

## I5. 対象 TS とフォルダ

- 作業対象: `LiveSyncWindow._current_quality_target_project_key()`(正規化済み)。
  None なら操作せず、パネルメッセージに理由を出す。
- 既知 TS: そのプロジェクトの `known_texture_sets_by_project` だけ。
  シーン全体の和集合は使わない。
- シーン側: `uvremap_maya.list_texture_sets()` のキー(シェーダー名 = TS名)。
  無い TS は対象外にして名前を出す。0 件なら何もしない。
- メッシュ: 対象 TS の `shape` の和集合(昇順)。
- 入力(原本テクスチャ): `watcher._final_dir_for_project(project_key)`。
  None なら「このプロジェクトの Final 書き出し先が未登録です。…」。
- 作業フォルダ: `<uvremap_root_dir>/<Finalサブフォルダ名>`。
- 出力: `<uvremap_root>/output/final`。
  `final_export_dir` / `watch_dir` の内側なら書き込まず中止する。

`convert_now` は TS ごとに `runtime.convert_now(..., timeout_s=1200)`。
`out_res` は取らない。

## I6. キルスイッチと poll の警告

OFF のとき変換は何もしない。OFF にした瞬間に `revert_to_original()`。
基準UVの記録は OFF でも許可する。

`poll()` の `warning`(`no_file_nodes_switched` / `converted_file_missing` /
`scene_changed`)は日本語でパネルのメッセージ行へ出し、
`_append_history("uvremap", ...)` にも残す。

`converted_file_missing` で一部だけ切り替わった場合、runtime は
`CONVERTED_OK` のまま warning と `missing` を付ける(QUESTIONS_runtime.md
D2c-3)。こちらは status.json を上書きせず、メッセージ行にだけ出す。
ワーカー/runtime が書いた状態と矛盾させない。

## I7. バージョン整合

`maya_live_sync.__version__` を `1.7.0` にした。`install.py` の
`_collect_installed_versions` は、ロード済みモジュールの `__version__` を
一覧するだけで、他ファイルが同じ文字列であることは要求しない。
uvremap_* は `required: False` のため、無くても Live Sync 本体は入る。
`install.py` 自身の `__version__`(インストーラ 1.0.4)は変えていない。

## I8. 未実行(実機必須)

このクラウド環境に Maya は無い。次は学校 PC で確認すること。

1. `feat/uvremap-integration` を入れ、`install.py` をドラッグ&ドロップする。
2. Live Sync 窓の「同期」タブに「UV再マップ(試験運用)」があること。
   既定は折りたたみ。展開できること。import 失敗時も Live Sync が従来どおり動くこと。
3. プロジェクト未リンク / Final 未登録のとき、操作が拒否されメッセージが出ること。
4. Painter へメッシュを送った直後に「基準UVを記録」。確認ダイアログのあと 1 回だけ
   記録され、再押しでは上書きせず案内だけになること。
5. キルスイッチ ON で「今すぐ変換」。完了後にファイルノードが変換後パスになること。
   監視フォルダ・Final フォルダの中に変換結果が書かれないこと。
6. 「原本に戻す」で `uvremapOriginalPath` 経由に戻り、属性が消えること。
7. キルスイッチ OFF で変換が動かず、OFF にした瞬間に原本へ戻ること。OFF でも
   基準記録はできること。
8. 変換中に別の保存済みシーンを開いたとき、切替が見送られ
   `scene_changed` の案内が出ること。未保存同士は区別できないこと。
9. 変換後にプレビュー再読込・Final 再読込・表示品質切替を行い、変換後ノードの
   パスが watch/Final 側へ巻き戻されないこと。孤立ノード一覧に誤って出ないこと。

## I9. D2b レビュー指摘の修正(キルスイッチ / 開始失敗メッセージ / 孤立 CONVERTING)

D2b マージ前に直した 3 点。

1. キルスイッチが実行中ジョブに効かない: `convert_now` だけが `get_enabled()` を見ていた。
   OFF にした瞬間に `revert_to_original()` で原本へ戻るが、ワーカー終了後の `_finish_job`
   がファイルノードを変換後へ切り替えてしまい、契約 §6 D-4 に反した。
   `_finish_job` の先頭でキルスイッチを見る(シーン確認より先)。OFF、または
   `get_enabled()` が例外なら、切替も D-3 の原本復帰もしない。status は `ORIGINAL` +
   `MSG_KILLSWITCH`、イベントは `warning=uvremap_disabled`。
   D-3 を行わない理由: OFF 時点ですでに原本へ戻している。失敗ジョブの D-3 は
   「変換後のまま残さない」ためであり、いま原本なので再復帰は不要。ここで
   切替や D-3 を行うと、原本へ戻した状態が変換後パスへ巻き戻る。
2. ジョブが 1 件も始まらないとき、`convert_now` が必ず `MSG_NO_BASELINE` を返していた。
   基準の有無は直前に確認済みで、実際の理由は mayapy 欠損・ワーカー脚本なし・
   キルスイッチ競合など。空の `job_ids` では `MSG_NOT_STARTED` を返す。
3. 窓を閉じると `shutdown()` でタイマーが止まり、新しい窓の runtime は古い `_jobs`
   を知らない。`status.json` が `CONVERTING` のまま残る。`recover_orphaned` で、
   `_jobs` に無い CONVERTING 行だけを `ORIGINAL` にし、`attach` のタイマー開始前と
   シーン変更時に 1 回呼ぶ。実行中(_jobs にある)の TS は触らない。別プロセスの
   ワーカーが生きていれば、次の「今すぐ変換」で世代が上がり古いジョブは終了コード 10。

## I10. 未対応(記録のみ): 変換の並列と配列取得の繰り返し

`convert_now` は TS ごとに mayapy を並列で起動する。TS が多い、または解像度が
高い場合の CPU・メモリ負荷は未測定。各ジョブが同じ全シェイプの `collect_arrays`
を Maya のメイン画面スレッドで繰り返す。後続(並列数の上限、配列取得の一括化、
`E2E_REPORT.md` の測定)で扱う。コードは変えていない。

## I11. 未対応(記録のみ): `_call` の TypeError 再呼び出し

`UVRemapController._call` は、`TypeError` が出ると引数なしで再呼び出しする。
関数の内部で起きた本物の `TypeError` も隠し得る。実機の呼び出し先はすべて
引数の数が固定なので、後続で整理する。コードは変えていない。

## I12. 変換中の「別名で保存」と上書き保存

変換中に「別名で保存」するとシーンの識別子が変わり、完了時に切り替えが見送られる
(`scene_changed`)。同じシーンのまま保存し直した場合も、Maya が返すシーン名が
ジョブ記録時と完了時で食い違えば同様に見送る。

## I13. R1 指摘 1〜10 の修正(D2e)

独立レビュー R1 の修正対象(High/Medium/Low の指定分)を直した。実機は未実行。

1. ロック待ち: `lock_timeout_s` の既定をジョブの `timeout_s` にした(統合は 1200)。
   同一 TS が `_jobs` にあるときは新しいジョブを作らず、status は変えず空文字を返す。
   統合は `MSG_ALREADY_RUNNING`(このテクスチャセットは変換中です)。他 TS は続行。
   ロック待ち失敗で `FAILED` を書く前に `latest_generation` を見る。自分より新しければ
   status は書かず終了コード 10(I4 と同じ「より新しい世代を残す」)。同じ世代なら
   従来どおり 12。
2. 公開ロールバック: undo が 1 件でも失敗したら `_sync_complete.flag` を戻さず削除する。
   無いことを「未完了」とし、次の変換が強制終了後と同じ経路で復帰できるようにする。
   戻すと新旧混在が旧世代の完了印に見えるため。`os.replace` の再試行は共有違反だけ、
   合計約 5 秒。`ENOSPC` はすぐ失敗。
3. ワーカーが status を書かずに死んだとき、`_finish_job` が `FAILED`(終了コード N)を書く。
   終端状態を同じ世代以上で書いていれば上書きしない。終了コード 10 は触らない。
   `poll` の最後に `_jobs` に無い CONVERTING を `recover_orphaned` と同じ考えで回収する。
4. 台帳 `outputs` のキーはファイル名だけ。`..` や区切りは無視して警告。解決後は
   `uvremap_root/output` 配下に限る。
5. Windows では入力名を大小無視。コピーは一覧の実名。他 OS は従来どおり。
6. ファイルノード切替/復帰を `undoInfo(stateWithoutFlush=False)` で囲む
   (`reload_textures` と同じ保存→戻す)。偽物の cmds では通す。
7. 再インストールの窓破棄で `_uvremap.shutdown()` を try/except 付きで呼ぶ。
8. 予約名/末尾ドット空白/`<>"|?*`/長さをワーカーの `_safe_component` でも拒否
   (終了 11)。名前空間の `:` は従来どおり 12。
9. Qt は `maya_live_sync.py` と同じく PySide2 → PySide6。R1 の「PySide6 が先」は
   uvremap 側の当時の順で、Live Sync 本体は当初から PySide2 が先だった。
10. 操作者向けの重複文言は `uvremap_runtime.py` に集め、統合は import。runtime 冒頭の
    「maya_live_sync.py は変更しない」は PR #18 で最小変更済み、と直した。
    成功 poll の偽 Popen は `status.json` に `CONVERTED_OK` を書く。

## I14. 記録のみ: ロック待ちを長くしたあとの合計時間

ロック取得は `timeout_s`(ウォッチドッグ)に含まれない。TS を並列起動しても
`worker.lock` で直列化するため、TS が多いと待ち+変換の合計が `timeout_s` を
超え得る。並列数の上限や一括化は後続(I10 と同じ)。

## I15. 記録のみ: このタスクでやらなかった R1 項目

- R1-07 名前空間付きシェーダー名: Painter 側の TS 名が未確認。推測で正規化しない。
- R1-05 `STALE` 表示、R1-06 品質判定、R1-11 Orig 位置: 設計判断または実機が先。
- R1-15 プローブ侵食幅、R1-16 メモリ、R1-17 最小バージョン表記: 同上。
- R1-19 個人情報(メール等): 導入改善の別作業。
- `maya_live_sync.py` / `sp_live_sync_plugin.py` は変更していない。
