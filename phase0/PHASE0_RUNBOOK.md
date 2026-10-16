# Phase 0 実行手順(学校PC / インストール不要)

前提: Maya と Substance 3D Painter。**バージョンは固定しない**(Maya 2026 / 2027 どちらでも可)。**ソフトの追加(pip 含む)はしない。** このフォルダ(`phase0/`)だけ使う。**自分の作品シーンや `.spp` は開かない。**

結果JSONのパスは `common.py` がホームを `~` に置換する。メール・ユーザー名は出さない。指揮役(Claude)には **JSONの中身を貼る**(ファイルそのものを送らなくてよい)。

---

## 1. Maya(推奨: mayapy。今開いているシーンに触れない)

先に `mayapy.exe` の場所を探す(PowerShell):

```
Get-ChildItem "C:\Program Files\Autodesk" -Filter mayapy.exe -Recurse -ErrorAction SilentlyContinue
```

見つかったパスを変数に入れ、実行前に `Test-Path` する。

PowerShell(引用符で囲んだパスの前に `& ` が必要):

```
$mayapy = "C:\Program Files\Autodesk\Maya2026\bin\mayapy.exe"
# 上の年は例。Get-ChildItem で出たパスに置き換える。
Test-Path $mayapy
Set-Location <この phase0 フォルダ>
& $mayapy run_maya_all.py
```

cmd(引用符の前に `&` は不要):

```
cd /d <この phase0 フォルダ>
"C:\Program Files\Autodesk\Maya2026\bin\mayapy.exe" run_maya_all.py
```

`Test-Path` が `False`、または cmd で「見つからない」なら、年号を 2027 など実際のインストールに合わせるか、`Get-ChildItem` の結果を使う。

これで V1 / V4 / V6 / V9(FBX出力まで)が走り、`phase0\results\phase0_results_maya.json` ができる。所要 **約10分**。

Script Editor(Python)で個別実行する場合は、**空の新規シーン**だけ。先頭の `PHASE0_DIR_OVERRIDE` にこの `phase0` フォルダを書く。既存シーンが開いているとスクリプトは拒否する。

| 項目 | 個別ファイル | 目安 |
|---|---|---|
| V1 | `v1_numpy_mayapy.py` | 2分 |
| V4 | `v4_connected_shaders.py` | 3分 |
| V6 | `v6_topology_fingerprint.py` | 5分 |
| V9 Maya | `v9_triangulated_fbx.py` | 3分 |
| V9 Painter | `v9_protocol.md`(手動) | 15分 |

V9のFBXは `phase0\scratch\phase0_v9_triangulated.fbx`。Painter側は `v9_protocol.md`。

---

## 2. Painter(Python コンソールまたはプラグイン)

学校の共用PCでは、**プラグイン導入より Python コンソールへ貼り付けを推奨**する(他利用者の環境を汚さない)。

プラグインを使う場合の置き場と有効化は公式どおり。Windows 7.2 以降の Python プラグインは  
`~\Documents\Adobe\Adobe Substance 3D Painter\python\plugins`  
(公式: https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/tutorials/create-a-python-plugin )。  
`plugins` 直下(JavaScript用)ではなく **`python/plugins`**。Painter を起動し、**Python メニューでファイル名をクリックして有効化**(同ページ)。

Python コンソールは公式チュートリアルが「Python Console window」を開いてコマンドを打つ手順を示している  
(https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/tutorials/load-external-python-modules )。

コンソール例(パスは自分の `phase0` に合わせ、ユーザー名をチャットに貼らない):

```
PHASE0 = r"D:\work\Tools\phase0"
exec(open(PHASE0 + r"\v2_painter_api.py", encoding="utf-8").read())
exec(open(PHASE0 + r"\v7_udim_detect.py", encoding="utf-8").read())
```

V2 / V7 は調査だけ(プロジェクトを書き換えない)。所要 各3分。結果は `phase0\results\phase0_results_painter.json` またはコンソールのJSON。

V3 は手動。`v3_reload_protocol.md` に従い、**名前に `phase0` を含むテスト用プロジェクトだけ**で `v3_reload_helper.py` を使う。所要15分。

---

## 3. 単体 Python(Maya 不要)

```
python v5_final_format.py "<Final出力フォルダ>"
python v8_replace_lock.py
```

V5 は実機の Final フォルダを指定(中身が空だと INCONCLUSIVE)。V8 は Windows のみ意味がある。結果は `phase0\results\phase0_results_misc.json`。各3〜5分。

---

## 4. 学校の共用PC

1. `phase0` はネットワークドライブや他利用者のホームではなく、**自分のローカルドライブ**(例: `D:\work\...`)に置く。
2. 実行後、`phase0\results` と `phase0\scratch` を必要な分だけ回収し、**PC上からは削除する**。
3. Painter はプラグイン導入より、**コンソールへ貼り付け**を推奨する。
4. 指揮役へ結果を貼る前に、ユーザー名・メール・ホームの絶対パス・認証情報がないか確認する。あれば `~` や `<user>` に直す。

---

## 失敗したとき

1. 画面に出た JSON をそのまま貼る。
2. 赤い例外文も貼る(パスにユーザー名があれば `~` に直す)。
3. どのファイルを、mayapy / Script Editor / Painter コンソールのどれで実行したかを一行書く。

推測で PASS/FAIL を書き換えない。分からなければ INCONCLUSIVE のままでよい。
