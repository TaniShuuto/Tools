# V3: reload_mesh で UV が保持されるか(手動)

所要時間の目安: 15分。**利用者の既存 `.spp` では行わない。**

根拠(公式):
- `reload_mesh` / `MeshReloadingSettings` / `auto_unwrap_settings=None` なら auto-unwrap 無効
  https://experienceleague.adobe.com/en/docs/substance-3d-dev/painter-python/api/substancepainter-package/project
- 新規プロジェクト作成時の `ProjectWorkflow` と auto-unwrap
  同上

## 準備(テスト専用)

1. Maya で新規シーンを開き、簡単なメッシュ(キューブで可)を1つ作る。
2. UV を適当に展開し、テスト用 FBX を `phase0/scratch/phase0_v3_base.fbx` へ書き出す。
3. Painter で **新規プロジェクト** を作る。
   - メッシュは上の FBX。
   - **自動展開(auto-unwrap)を使わない**(ダイアログでオフ。API では `auto_unwrap_settings=None`)。
   - 保存名は必ず `phase0_v3_test.spp` のように `phase0` または `v3_test` を含む。
4. わかりやすい色で1面だけ塗る(位置が後で分かる印)。
5. Maya に戻り、**同じメッシュの UV だけ**を大きく動かす(面の並びは変えない)。
6. `phase0/scratch/phase0_v3_changed_uv.fbx` として書き出す。

## 補助スクリプト

1. `v3_reload_helper.py` をテキストエディタで開き、先頭付近を直す。
   - `PHASE0_ALLOW_RELOAD = True`
   - `MESH_FBX = r"…\phase0\scratch\phase0_v3_changed_uv.fbx"`
2. Painter の Python コンソールで実行する(コンソールの出し方は PHASE0_RUNBOOK.md)。
3. コンソールに出た JSON を保存する。`reload_mesh` は非同期なので、status コールバック行(`[V3] reload_mesh status:`)も残す。

## 目視(この2つが本題)

| # | 確認 | 期待 | 実際(自分で記入) |
|---|---|---|---|
| a | Painter 側の UV(2Dビュー)は、変更後の UV になっていない | 基準UVのまま |  |
| b | 塗った印は同じ表面位置に残る | `preserve_strokes=True` |  |

どちらかでも期待と違う、または判断できない場合は INCONCLUSIVE とし、スクリーンショットは個人情報が写らない範囲で指揮役へ渡す。

終わったら `PHASE0_ALLOW_RELOAD` を `False` に戻す。テスト用 `.spp` は残してよい(既存作品と混ぜない)。
