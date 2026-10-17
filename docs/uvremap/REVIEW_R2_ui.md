# 画面の状態の伝わり方 横断調査 R2

対象: `feat/uvremap-d2f-ui` 先頭 `f2de12b`。読み取りのみ。修正はしていない（本ファイルのみ追加）。

D2f で直した次の4点は、新規の件数に入れていない。コード上、意図どおり別経路になっていることを確認した。

1. 正常完了・失敗がメッセージ欄に残る（`completion_message` / `run_poll_tick`）。
2. 「基準UVを記録」は `check_record_ready` のあとで確認ダイアログ。
3. キルスイッチ ON かつ status パスが無いとき、全体は「全体: プロジェクト未設定」。
4. キルスイッチ OFF は、戻したノードが空の一覧なら「UV変換を無効にしました」。例外時や戻り値が一覧でないときは「UV変換を無効にし、原本に戻しました」（`docs/uvremap/QUESTIONS_integration.md` I16 の記載どおり。ここは再指摘しない）。

Painter の最終出力解像度プルダウン（画面は未保存の 512、書き出しは保存済みの 4096、未保存表示が無い）は既知のため ID を付けない。同じ保存待ちであることは R2-08 に書く。

## 要約

- 件数: UV再マップ 7（High 2 / Medium 4 / Low 1）、Live Sync Maya 4（High 1 / Medium 3）、Live Sync Painter 3（High 2 / Medium 1）、導入 1（Low 1）。
- 最も重要な 3 点:
  1. **R2-08 / R2-11** 画面のフォルダ・解像度・監視設定は「設定を保存」まで処理に使われず、未保存である表示も無い。
  2. **R2-01 / R2-02** 「原本に戻す」と変換失敗の文言が、戻したノードの有無を見ずに「原本に戻しました」と言う。
  3. **R2-09** Painter の書き出し完了文と「N件更新」「同期回数」が、スキップ・キャンセル・非成功でも成功側に寄る。
- 読めなかった範囲: 実機は未実行。`maya_live_sync.py` はボタン・`QMessageBox`（出現 24）・監視開始/停止・表示品質・保存・バックアップ入口と、ログに出る `_emit_status` を読んだ。シェーダーグラフ接続と再読込のノード走査の全行は未読。`uvremap_runtime.py` は利用者に出る文言と、それを書く `record_baseline` / `revert_to_original` / `convert_now` / `poll` / `_finish_job` を読んだ。ファイル名一致ヘルパの全行は未読。`sp_live_sync_plugin.py` はパネル・ウィザード・有効化・書き出し結果の文言を読んだ。プロジェクトイベントの全行は未読。`install.py` / `uninstall.py` は完了ダイアログに載る成否を読んだ。コピーとシェルフ削除の実装全行は未読。

## 指摘

### R2-01 / UV再マップ / 観点 4, D / High

- **場所**: `sp_maya_livesync/uvremap_integration.py:568-574` `UVRemapController.revert`。ボタンは `uvremap_panel.py:398-400`「原本に戻す」→ `uvremap_integration.py:902-904` `on_revert`。
- **現象**: 「原本に戻す」を押すと、戻した file ノードが 0 件でもメッセージ欄は「原本に戻しました」になる。
- **根拠**: `revert_to_original` は属性の無いノードを触らず、戻したノード名の一覧を返す（`uvremap_runtime.py:798-808`, `445-481`）。`revert` はその一覧の長さを見ず、例外が無いだけで固定文を返す。キルスイッチ OFF だけが `disable_message` で件数を見ている。
- **修正案**（費用: 小）: 戻りが一覧なら 0 件は「戻す対象はありませんでした」、1 件以上は件数付き。例外のときは戻したと言わない。

### R2-02 / UV再マップ / 観点 4, D, A / High

- **場所**: `sp_maya_livesync/uvremap_integration.py:208-230` `completion_message`。戻すかどうかは `sp_maya_livesync/uvremap_runtime.py:558-565` `_should_revert_d3`、`1066-1075` `_finish_job`。
- **現象**: 変換が `FAILED` で終わると、原本へ戻していなくてもメッセージ欄は「変換に失敗し、原本に戻しました」になる。`BLOCKED`（終了コード 11）で原本へ戻した場合は、そのことが文に出ない。
- **根拠**: D-3 は直前の正常世代の UV ハッシュが現行と一致するとき戻さない（`_should_revert_d3` が False）。戻すときも属性付きノードが無ければ一覧は空。`completion_message` は `state == "FAILED"` だけで復帰文を作り、イベントの `reverted` を見ない。終了コード 11 は `BLOCKED` にしたあと、同じ条件で `revert_ts_file_nodes` を呼ぶ。`BLOCKED` の文は「変換できませんでした」のみ。
- **修正案**（費用: 中）: 文言は `reverted` の件数と、戻す処理を呼んだかで分ける。戻していない失敗は「変換に失敗しました」。戻した `BLOCKED` はその旨を足す。

### R2-03 / UV再マップ / 観点 4, D / Medium

- **場所**: `sp_maya_livesync/uvremap_integration.py:52`, `194-195` `warning_message`。切替は `sp_maya_livesync/uvremap_runtime.py:420-428` `switch_ts_file_nodes`、`1109-1111` `_finish_job`。
- **現象**: 一部のチャンネルだけ変換後ファイルが欠けているとき、切り替わったノードがあっても「変換後のファイルが見つからないため、切り替えを見送りました」と出る。
- **根拠**: 欠けた suffix だけスキップし、他は `switched` に入る。`missing` が空でなければ警告は `WARN_FILE_MISSING`。`warning_message` はこの警告を、`switched` の有無に関係なく固定文 `MSG_FILE_MISSING` にする。
- **修正案**（費用: 小）: `switched` が空でないときは「一部を切り替え、欠けたチャンネルは見送りました」にする。

### R2-04 / UV再マップ / 観点 5, E, C / Medium

- **場所**: `sp_maya_livesync/uvremap_integration.py:828-830`, `953-956` `attach` / `on_group_toggled`。実際の有効フラグは `449-460` `set_enabled` とパネル内チェック「UV変換を有効にする」。
- **現象**: 欄の見出し「UV再マップ(試験運用)」のチェックを外しても、保存済みの `uvremap_enabled` は変わらない。窓を開いた直後はこのチェックが常に外れ、中の有効チェックが見えない。有効のままジョブが終わると、欄を閉じた見た目のまま切替が進み得る。
- **根拠**: `setChecked(False)` のあと、`on_group_toggled` は `detail.setVisible` だけ。`set_enabled` は呼ばない。poll タイマーは `attach` の最後で始まる。
- **修正案**（費用: 小）: 折りたたみはチェックボックスにしない（ボタンで開閉する）。有効/無効は中のチェックだけにする。

### R2-05 / UV再マップ / 観点 1, A / Medium

- **場所**: `sp_maya_livesync/uvremap_integration.py:700-715` `UVRemapAttachment.on_scene_changed`。
- **現象**: シーンを切り替えたあと、新しいシーンにプロジェクトがあり孤立回収も無いと、前のシーンの「変換を開始しました」「変換が完了しました」などがメッセージ欄に残る。
- **根拠**: 文言を書くのは、回収があったときと `status_path()` が失敗したときだけ。`status_snapshot` はファイルが無くてもパスを返す（`uvremap_runtime.py:722-724`）ので、`ok` は True になり得る。パネル行は新しい status で描き直す。
- **修正案**（費用: 小）: シーン変更時はメッセージ欄を空にするか、「シーンが切り替わりました」に置き換える。

### R2-06 / UV再マップ / 観点 F / Medium

- **場所**: `sp_maya_livesync/uvremap_runtime.py:40-42` `MSG_NO_MAYAPY`。参照は `843-846` `convert_now`。設定キーは `uvremap_integration.py:335` の `uvremap_mayapy_path`。パネルと Live Sync 窓にこの入力欄は無い。
- **現象**: mayapy が見つからないと「mayapy_path で指定してください」と出る。画面のどこを押せばよいかは書いていない。
- **根拠**: `resolve_mayapy`（`123-142`）は、パスが空なら実行ファイルの隣を探し、指定パスがファイルでなければ隣を探さず None。そのとき status は `FAILED` とこの文。`uvremap_panel.py` / `uvremap_integration.py` のウィジェットに mayapy 用の欄は無い。
- **修正案**（費用: 中）: 文を「Maya の bin にある mayapy が見つかりません」に変えるか、パス入力を設定欄に足す。

### R2-07 / UV再マップ / 観点 F / Low

- **場所**: `sp_maya_livesync/uvremap_panel.py:141-146` `overall_status_label`、`486-489` `_render`。`sp_maya_livesync/uvremap_runtime.py:39` `MSG_KILLSWITCH`。
- **現象**: プロジェクト未設定のとき、全体は「全体: プロジェクト未設定」、その下の空欄は「情報なし」。無効時の status メッセージは「キルスイッチがオフ」で、画面の操作名は「UV変換を有効にする」「UV変換は無効」。
- **根拠**: `_render` は行が無いとき常に `LABEL_NO_INFO`。無効化時の status 文は `MSG_KILLSWITCH` 固定。契約 D-1 の状態名（`CONVERTED_OK` 等）を英語のまま出すのは契約どおりで、ここでは指摘しない。
- **修正案**（費用: 小）: 未設定の空欄も「プロジェクト未設定」。status の「キルスイッチ」を「UV変換は無効」に揃える。

### R2-08 / Live Sync Painter / 観点 5, E, A / High

- **場所**: 画面 `sp_maya_livesync/sp_live_sync_plugin.py:1871-1904`、保存 `2095-2122` `_on_save_clicked`。使用側は `_build_export_config` `1537-1540`（プレビュー解像度）、`_do_export` `1239`, `1277`, `1303`（フォルダ）、`on_texture_state` `745-746`（デバウンス）、`_request_final_export` `1080-1081`（クールダウン）。保存結果の文言は `704-711`、ログ折りたたみは `2054-2064`。
- **現象**: プレビュー解像度、ステージング/監視/最終出力フォルダ、デバウンス、Final クールダウンを変えても、「設定を保存」まで書き出し・待ち時間は保存済みの値のまま。未保存である印は無い。保存に成功しても、折りたたみ中のログに「設定を保存しました」と出るだけで、欄の見た目は変わらない。最終出力解像度は既知の同じ経路（`final_resolution_log2` は保存値だけを読む）。
- **根拠**: コンボとスピンの値を `config` に書くのは `_on_save_clicked` とウィザードの `accept`（`1763-1787`）だけ。書き出し・タイマーは `self.config`。ウィジェットの `textChanged` 等で未保存表示を出す接続は無い。`_on_log_message` がログを開くのは「エラー」「失敗」「warning」「error」を含む文だけ。
- **修正案**（費用: 中）: 保存済みと違うあいだ「未保存」と出し、書き出し前に保存を促す。保存成功はログを開くか、ボタン隣に短く出す。

### R2-09 / Live Sync Painter / 観点 4, D, A / High

- **場所**: `sp_maya_livesync/sp_live_sync_plugin.py:1139-1182` `_safe_export`、`1252-1260` と `1330-1348` と `1395-1436` `_do_export`。
- **現象**: 「今すぐ全量同期(Final)」や保存後の Final で、書き出しステータスが成功でなくても「フル解像度の最終書き出しが完了しました」と出ることがある。プレビューは、1 件でも置き換わると「プレビュー同期完了(N件更新)」の N が、差分無しでスキップしたファイルを含む。変更が無くても「同期回数」は増える。Final の「変更が無いのでスキップ」は「スキップ回数」を増やさない。
- **根拠**: `Cancelled` だけ `return` する。それ以外の非成功は「書き出しステータス」を出したあと処理を続け、preview でなければ完了文を出し `final_export_succeeded = True` にする。完了の件数は `result.textures` のファイル数。`_unchanged` で飛ばしたファイルは `skip_count` のみ。`moved_any` が False のプレビューは完了文が無い。`sync_count` は `_do_export` が例外なく戻れば増える（早期スキップとキャンセルを含む）。早期スキップは `skip_count` を触らない。
- **修正案**（費用: 中）: 非成功とキャンセルは完了文を出さず `sync_count` も増やさない。更新件数は置き換えた数。差分無しはスキップ回数と「変更はありません」にする。

### R2-10 / Live Sync Painter / 観点 3, C / Medium

- **場所**: `sp_maya_livesync/sp_live_sync_plugin.py:1030-1046` `force_sync_now` / `force_full_final_sync`。表示は `2054-2064` `_on_log_message`。
- **現象**: プロジェクトを開いていないときに「今すぐ同期」または「今すぐ全量同期(Final)」を押すと、画面の数値も警告ラベルも変わらず、折りたたみログの中だけ「プロジェクトが開かれていません。」となる。
- **根拠**: 両ボタンは `project.is_open()` が False なら `_emit_status` して戻る。この文はログ自動オープンの語（エラー / 失敗 / warning / error）に含まれない。`stats_changed` は呼ばれない。プロジェクトが開いているときの全量同期は `final_pending` ラベルが出るので、こちらの無反応には含めない。
- **修正案**（費用: 小）: この文はダイアログにするか、ログを開く条件に入れる。

### R2-11 / Live Sync Maya / 観点 5, E, C / High

- **場所**: 画面 `sp_maya_livesync/maya_live_sync.py:4801-4836`、保存 `5820-5829` `_on_save_clicked`。使用側は `start` `2391`（監視フォルダ）、`create_shader_network` `4146`（レンダラー）、`backup_final_textures_to_sourceimages` `3539-3542`（sourceimages バックアップ）。
- **現象**: 監視フォルダ、レンダラー、「シーン保存時に Final を sourceimages へバックアップ」を変えて「設定を保存」を押さないと、監視 ON・マテリアル作成・シーン保存は保存済みの値で動く。未保存の表示は無い。バックアップのチェックだけ入れてシーンを保存すると、Live Sync のログには何も出ない（設定が OFF のまま即 return するため）。
- **根拠**: 3 項目を `config` に書くのは `_on_save_clicked` とウィザード `accept`（`4447-4456`、ウィザードはバックアップを含まない）。`start` は `self.config["watch_dir"]`。シェーダーは `self.config.get("renderer")`。バックアップは `sourceimages_backup_enabled` が偽なら `_emit_status` せず return する。開始ログには実際の監視パスが出るので、ログを読めばパスの差には気づける。欄自体に未保存印は無い。
- **修正案**（費用: 中）: 未保存のあいだ印を出す。監視 ON の前に、欄のパスと保存済みパスが違えば保存を促す。バックアップ OFF でチェックだけ付いているときは、その旨をログに出す。

### R2-12 / Live Sync Maya / 観点 3, C, 5 / Medium

- **場所**: `sp_maya_livesync/maya_live_sync.py:5806-5818` `_on_quality_toggled`。0 件のときのログは `3959-4020` `switch_texture_quality`。
- **現象**: 「表示品質」を高画質からプレビューへ戻そうとして、切り替え対象が 0 件だと、ボタンの押下だけが外れ、ボタン文言は「表示品質: 高画質(Final)」、隣のラベルは「[Final]」のまま残る。file ノードは切り替わっていない。高画質へ入れる失敗のときだけ、押下状態を戻す。
- **根拠**: `switch_texture_quality` は対象 0 件で 0 を返す。`_on_quality_toggled` は戻りが真のときだけ `_refresh_quality_display`。偽かつ `checked` が真のときだけ `setChecked(False)`。外す操作（`checked` が偽）では見た目を戻さない。ログには「file ノードが見つかりませんでした」等が出る（同期タブのログは常時表示）。
- **修正案**（費用: 小）: 戻りが 0 のときは ON/OFF どちらでも `_refresh_quality_display` で押下と文言を保存済みの品質に戻す。

### R2-13 / Live Sync Maya / 観点 4, D / Medium

- **場所**: `sp_maya_livesync/maya_live_sync.py:5611-5615` `_on_clear_other_session`。削除本体は `378-390` `_force_clear_session_lock`。
- **現象**: 「無視して削除」を押すと、ロックファイルが消せなくても警告が消え、「他セッションのロック情報を削除しました。」とログに出る。
- **根拠**: `_force_clear_session_lock` は例外時 False。呼び出し側は戻り値を見ず、`other_session_info` を None にしてラベルを隠し、固定の成功文を出す。
- **修正案**（費用: 小）: False のときは警告を残し、「削除できませんでした」にする。

### R2-14 / Live Sync Maya / 観点 4, D / Medium

- **場所**: 追加 `sp_maya_livesync/maya_live_sync.py:5501-5505` `_on_add_scene_project_link`、削除 `5524-5526` `_on_remove_scene_project_link`、設定 `5582-5586` `_on_link_scene_to_sp_project`。書き込みは `1654` `_add_scene_project_link`、`1671` `_remove_scene_project_link`、成否は `1547-1557` `_set_scene_project_links`。
- **現象**: 保存済みシーンで fileInfo への書き込みが失敗しても、「追加しました」「削除しました」「紐付けました」とログに出る。
- **根拠**: `_set_scene_project_links` は fileInfo の例外で False を返す。`_add_scene_project_link` はそれを見ずに link を返す。`_set_current_scene_project_link` は「None でない」だけを成功にする（上限超過は別。こちらは見ている）。`_remove_scene_project_link` は戻り値を捨てる。削除と追加のハンドラは、そのあと固定の成功文を `_emit_status` する。
- **修正案**（費用: 小）: `_set_scene_project_links` の False を上まで返し、失敗文にする。成功文の前に再読込して一覧と一致するか見る。

### R2-15 / 導入 / 観点 4, D / Low

- **場所**: `sp_maya_livesync/install.py:722` `_run` の先頭行。本文のスキップ記載は `759-786`。`sp_maya_livesync/uninstall.py:435`。ファイル 0 件は `440-442`。
- **現象**: シェルフ追加や自動起動登録がスキップされても、ダイアログの先頭は「インストールが完了しました。」。アンインストールは、ツールファイルが 1 つも無くても先頭が「アンインストールが完了しました。」。
- **根拠**: 先頭行は成否フラグを見ない。インストール本文はシェルフと自動起動のスキップを続けて書く。アンインストールは続けて「該当ファイルは見つかりませんでした」と書く。先頭だけ読むと、全部終わったように見える。
- **修正案**（費用: 小）: スキップや 0 件のときは先頭を「一部をスキップして終わりました」にする。

## 確認した範囲と結論

- **UV再マップ（ボタン）**: 「基準UVを記録」は前提（未リンク、Final 無し、対象 TS 無し、作業フォルダが監視/Final の内側）のあとで確認。既にある基準は確認ではなく案内。キャンセルは「記録を取り消しました」。記録成功は `record_baseline` が例外を出さないときだけ「基準UVを記録しました」。「今すぐ変換」は無効・未リンク・基準無し・起動 0 件をそれぞれ別の文にする。正常完了は poll が「変換が完了しました」に置き換える（D2f）。終了コード 10 は打ち切りで、メッセージ欄を空の完了文で上書きしない。キルスイッチ OFF の件数付き文言は D2f のとおり。変換・原本復帰・更新は、無効のときボタンが無効。更新は有効なら `refresh` で全体ラベルを描き直す。
- **UV再マップ（状態名）**: 行の状態名が英語なのは契約 D-1 の列挙をそのまま出している。指摘にはしていない。
- **Live Sync Maya（確認の順序）**: 「＋現在のSPプロジェクトを追加」「SPプロジェクトを設定」は、SP 未検出と未保存をダイアログの前に弾く。「削除」は選択が無いと確認を出さない（インデックスが 0 未満なら return。リンクが無いとき削除ボタンは無効）。「Maya起動時に自動で開く」は質問のあと `register_user_setup` の成否をダイアログにする。シェルフ追加は、shelf が無いときと例外のときに警告。履歴が無いときは案内。マテリアル生成は行未選択を先に警告し、失敗は警告、成功名はログ（同期タブのログは常時表示）。表示品質の成功時は件数付きログ。一部だけ画像が無いときは品質フラグを更新せず、ボタンは `_refresh_quality_display` で前の品質に戻る（0 件で OFF にしたときだけ R2-12）。
- **Live Sync Maya（監視）**: ON/OFF はボタン文言と「監視を開始しました」「監視を停止しました」。シーン切替の自動停止は理由付き。起動時に `watch_enabled` でもシーン紐付けが無いと自動再開しない。その理由は起動直後の画面には出ない（シーン切替時の文とは別）。ボタンは OFF で、監視していないこと自体はボタンと一致する。実機で「設定が消えた」と読むかは未確認。推測のため ID は付けない。
- **Live Sync Painter（確認の順序）**: 設定保存とウィザードは、空フォルダの警告のあと、重複フォルダの Yes/No。有効チェックは押下と「Live Sync 有効化/無効化」が対応する（プロジェクト未オープンでもフラグは立つ。同期そのものは `project.is_open()` まで走らない）。プロジェクトが開いているときの全量同期は、要求ログに加え `final_pending` と書き出し中ラベルが出る。書き出し中の再入は警告ラベルが見えている前提で、追加の文は無い。履歴が無いときは案内ダイアログ。構成チェックの失敗は「失敗」を含むのでログが開く。
- **折りたたみ用のグループチェック**: Maya の「同期フォルダ・レンダラー設定」と Painter の「フォルダ・詳細設定」は、初期チェック付きで中身の表示だけを切り替える。機能の ON/OFF ではない。UV 再マップ欄（R2-04）と違い、初期状態で機能がオフに見える見出しではないため、別 ID にはしていない。
- **導入**: 失敗時の `confirmDialog`（install の例外、uninstall の入口）は失敗文。完了ダイアログの本文はスキップ件数を含む。先頭行だけが R2-15。

## 修正の優先順

### UV再マップ

1. R2-01 「原本に戻す」の固定成功文
2. R2-02 失敗・BLOCKED の文言を、戻した件数に合わせる
3. R2-03 一部だけ切り替えたときの「見送りました」
4. R2-04 見出しチェックと有効フラグの分離
5. R2-05 シーン切替後のメッセージ欄

### Live Sync Maya

1. R2-11 未保存の監視フォルダ・レンダラー・バックアップ
2. R2-12 表示品質を戻せなかったときのボタン
3. R2-14 紐付けの保存失敗と成功文
4. R2-13 「無視して削除」の失敗

### Live Sync Painter

1. R2-08 最終出力解像度以外の未保存（既知の解像度と同じ保存ボタン）
2. R2-09 完了文と件数と同期回数
3. R2-10 プロジェクト未オープン時の同期ボタン

### 導入

1. R2-15 完了ダイアログの先頭行
