# Gate 0 検証手順（G2 / G3 / G4）

参照設計書: `AnyPath_技術設計書_v2.1.md` §1.4（Gate 0）

**本書は実測の「結論」を出すものではありません。** 結論（§1.4の表を実際に埋める判断）は、この手順に従って集めた観測データを見ながら人間が行ってください。AnyPath側の自動化機能は「実機操作中に判断材料を自動収集してログへ残すこと」までを担当します。

すべての手順が完了したら `gate0_check.py` を実行し、その出力（またはこの手順書の末尾にある「判定の記入欄」）を使って、設計書 §1.4 の表と本書末尾の記入欄を埋めてください。

---

## 事前準備

1. AnyPath をインストール済みの Maya 2026 以降を用意する（`install_anypath.py` をドラッグ＆ドロップ済みであること）。
2. Maya を起動し、AnyPath メニューが表示されることを確認する（§6.1）。
3. ログ出力先を確認する: メニュー →「AnyPath の状態を確認」→ 自己診断画面の「ログ」セクションに表示されるパスをメモしておく（通常は `<Documents>/maya/anypath/logs/`。`MAYA_APP_DIR` をカスタム設定している環境ではそのパス配下になる）。

---

## G2: dirmap が実際に効く対象ノード種の実測

**検証したいこと**: `cmds.dirmap()` の前方一致置換が、テクスチャ（`file`）以外のノード種（特に Alembic、GPU Cache、XGen）に対して実際に効くかどうか。

### 手順

1. 検証用の作業フォルダを2つ用意する。片方を「正規の場所」（例: `C:/Gate0Test/Real/`）、もう片方を「壊れたパスが指す場所」（例: `D:/Gate0Test/Broken/`。**実在させない**、または中身を空にしておく）とする。
2. 「正規の場所」に、最低限これらのファイルを1つずつ用意する。
   - テクスチャ（`.png` 等）
   - Alembic キャッシュ（`.abc`）
   - GPU Cache（`.abc` または `.mayaCache`。プロジェクトで使っている形式）
   - （可能なら）XGen パレットの `.xgen` ファイル一式
3. Maya で新規シーンを作り、それぞれのファイルを参照するノードを作成する（`file`ノード、`AlembicNode`、`gpuCache`ノード、XGenパレット）。
4. シーンを保存した**後**、各ノードの参照パスを「壊れた場所」（`D:/Gate0Test/Broken/...`）へ手動で書き換える（`setAttr` で直接書き換えるか、Attribute Editor から編集する）。保存し直す。
5. 自己診断画面（またはプロジェクト設定 `anypath/config.json`）で `path_mappings` に以下を追加する。
   ```json
   { "from": "D:/Gate0Test/Broken", "to": "C:/Gate0Test/Real" }
   ```
6. シーンを一度閉じ、再度開く（`kAfterOpen` を発火させる）。
7. 各ノードの参照パスが `C:/Gate0Test/Real/...` に変わっているか（＝dirmapが効いたか）を、Attribute Editor で目視確認する。
8. 自己診断画面 →「Gate 0 観測状況」セクション、または `gate0_check.py` の出力にある `[G2: dirmap実効果]` を確認する。観測されたノードタイプごとの件数が表示されていれば、判断材料は記録されている。
9. ノード種ごとに「効いた／効かなかった」を目視結果から本書末尾の記入欄へ書き込む。

**XGenについて**: 設計書 §5.2 は「dirmapがXGenに効く裏付けは確認できていない」としています。この手順4〜8をXGenパレットについても実施し、実際に効くかどうかを確認してください。効かない場合は §5.2 の記述が実機でも裏付けられたことになります。

---

## G3: `userSetup.py` の実行挙動確認

**検証したいこと**: 対象Mayaバージョンで、`userSetup.py` が「検索パス上で発見された全ファイルが順に実行される」（v2.1での訂正記述）のか、それとも「最初の1つだけ実行される」（v1の誤った記述）のか。

**注意**: AnyPath自体は `.mod` + autoload プラグイン方式のため、この検証結果に依存しません（§2.2）。この手順はあくまで設計書の記述の裏付けを取るためのものです。

### 手順

1. `anypath/gate0/userSetup_probe.py` を確認する。
2. 少なくとも2箇所の異なる `scripts` フォルダを用意する。例:
   - `<MAYA_APP_DIR>/scripts/`（ユーザー共通）
   - `<MAYA_APP_DIR>/<バージョン番号>/scripts/`（バージョン別）
   - 追加で `MAYA_SCRIPT_PATH` 環境変数に別のフォルダを足してもよい
3. `userSetup_probe.py` を、上記それぞれの `scripts` フォルダへコピーする（同じ内容でよい）。
4. 各フォルダの `userSetup.py`（無ければ新規作成）に、以下の1行を追記する。フォルダごとに識別子を変えること。
   ```python
   import userSetup_probe
   userSetup_probe.record_invocation("user_scripts_dir")   # フォルダごとに文字列を変える
   ```
5. Maya を起動する。
6. 自己診断画面の「Gate 0 観測状況」、または `gate0_check.py` の `[G3: userSetup.py挙動]` を確認する。
   - 観測された `script_path`（＝手順4で設定した識別子）が **複数** 表示されていれば「全ファイルが順に実行される」ことが実機で確認できたことになる。
   - 1つしか表示されない、あるいは `max_invocation_index` が全体で1のままなら「最初の1つだけ実行される」可能性がある（さらなる切り分けが必要）。
7. 確認が終わったら、**手順4で追記した内容を必ず削除する**（本番運用でAnyPathがuserSetup.py依存を持たないようにするため。設計書§2.2の方針を壊さないための後片付け）。

---

## G4: `filePathEditor` 系の網羅性実測

**検証したいこと**: Maya標準の横断列挙機能（`cmds.filePathEditor`）が、明示レジストリ（§8, `anypath/bridge/registry.py` の `DEFAULT_NODE_REGISTRY`）に登録された全ノード種（特に Arnold ノード・Alembic・GPU Cache）を実際に列挙できるか。

### 手順

1. G2の手順3で作成したような、レジストリ対象の各ノード種（`file`, `aiImage`, `aiStandIn`, `aiVolume`, `imagePlane`, `AlembicNode`, `gpuCache`, `audio`, `cacheFile`, bifrostキャッシュ）を含むテストシーンを用意する（可能な限り全種類、難しければ主要なものだけでもよい）。
2. シーンを開く（`kAfterOpen`が発火し、AnyPathが自動的にG4観測を実行する。`_run_resolution_pass` 内の `observe_g4_filepatheditor` 呼び出し）。
3. 自己診断画面の「Gate 0 観測状況」、または `gate0_check.py` の `[G4: filePathEditor網羅性]` を確認する。
   - `レジストリのみに存在（filePathEditorの漏れ候補）` に何か表示されていれば、そのノード種は `filePathEditor` で拾えていないことが実機で確認できたことになる。
   - `filePathEditorのみに存在（レジストリ拡充候補）` に何か表示されていれば、レジストリに追加すべきノード種の候補が見つかったことになる。
4. 念のため、Maya の `Windows > General Editors > File Path Editor` を実際に開き、同じシーンでどのノードが一覧表示されるかを目視でも確認し、自動観測結果と付き合わせる。

---

## 完了後: 集計スクリプトの実行

```bash
cd <AnyPathインストール先>/anypath/gate0
python gate0_check.py --log-dir "<自己診断画面で確認したログフォルダ>" --output gate0_report.txt
```

Maya のPythonではなく、通常の `python3`（または Windows の `python`）で実行できます（本スクリプトはMaya APIに依存しません）。

出力された `gate0_report.txt` の内容と、上記手順中の目視確認結果を突き合わせて、以下の記入欄と設計書 §1.4 の表を埋めてください。

---

## 判定の記入欄（実測後、人間が記入する）

| # | 検証項目 | 結果 | 備考 |
|---|---|---|---|
| G2 | file(テクスチャ) | | |
| G2 | AlembicNode | | |
| G2 | gpuCache | | |
| G2 | XGen | | （dirmapの対象外という設計書の想定と一致するか） |
| G3 | userSetup.py は複数実行されるか | | |
| G4 | filePathEditorの網羅性 | | 漏れがあれば明示レジストリのみを正とする（設計書の既定方針） |

記入後、この表の内容を `AnyPath_技術設計書_v2.1.md` §1.4 および §5.2 の該当箇所へ反映してください。
