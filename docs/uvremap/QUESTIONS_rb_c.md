# QUESTIONS RB-c(リベース用メッシュ書き出し)

契約本文との差、および実機でしか決められない点。コードは指示書の決め打ちで書いてある。実機確認の手順は PR 本文の表。

## a. `fbxmaya` のロード確認を足した

指示書 3.5 の手順 3 に従い、`cmds.pluginInfo("fbxmaya", query=True, loaded=True)` が偽のときだけ `cmds.loadPlugin("fbxmaya", quiet=True)` する。失敗は `RebaseExportError`(`export_failed`)。契約本文にはこの手順が無い。プラグイン名と `quiet=True` は決め打ち。

## b. 最終ファイルが既にあるときは上書きしない

`meshes/<baseline_id>.fbx` が既にある呼び出しは `RebaseExportError` で終わり、既存ファイルの中身は変えない。MEL も選択も触らない。同じ ID の再書き出しは異常、という指示書 3.5 の手順 2。

前回の失敗で残った同名の一時ファイルは、最終ファイルの有無にかかわらず先に消す。最終ファイルがある場合でも一時ファイルだけは残さない(失敗経路で一時ファイルを残さない、という後条件)。

## c. 一時ファイル名の第一案

`REBASE_FBX_TMP_STYLE = "suffix"`。一時ファイルは `<baseline_id>.fbx.tmp`。MEL の `-f` が拡張子を要求する、または `.fbx` を付け足す場合は定数を `"infix"`(`<baseline_id>.tmp.fbx`)に切り替える。どちらも最終ファイルは `<baseline_id>.fbx`。

## d. 戻り値 `path` の区切り

`path` は `os.path.abspath` の文字列で、区切りは OS 既定のまま(Windows では `\`)。MEL に渡すパスだけ `/` にそろえる。

## e. FBX のバージョン・単位・軸

`FBXExportFileVersion` や単位・軸の指定は契約に無いので送らない。必要ならコマンドを足す。

## f. `meshes/` の掃除

古い FBX を消す処理は契約に無いので、この関数は作らない。書くのは今回の `<baseline_id>` の一時ファイルの削除と、失敗したとき自分で作った最終ファイルの削除だけ。

## g. シェイプのパスでは直接書き出していない

`FBXExport -s` は選択前提のため、`resolved["shape_paths"]` を選んでから MEL を呼ぶ。選択は `finally` で戻す。当初はシェイプを直接選んでいた。

追記(Maya 2027 で確認済みの手順に合わせた): 選ぶのはシェイプではなく親トランスフォーム。フルパスに親が含まれるときはその親を使う。シェイプ名しか無いときは `listRelatives` で親を 1 つ辿る。同じ親は 1 回だけ選ぶ。

## h. `FBXResetExport` は利用者の書き出し設定を初期化する

`FBXResetExport` には、利用者が Maya に保存している FBX 書き出し設定を初期化する副作用がある。RB-d で画面に注意を出すかは後で決める。
