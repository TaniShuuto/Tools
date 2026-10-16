# V9: 三角形化した FBX を Painter で使う(手動)

所要時間の目安: 15分。**利用者の既存プロジェクトでは行わない。**

Maya 側の出力は `v9_triangulated_fbx.py` または `run_maya_all.py` が
`phase0/scratch/phase0_v9_triangulated.fbx` を書く(四角形メッシュを
`FBXExportTriangulate -v true` で出力)。

## Painter での確認

1. Painter で **新規プロジェクト**(テスト専用。保存するなら名前に `phase0` を含める)。
2. メッシュに `phase0_v9_triangulated.fbx` を指定する。自動展開はオフでよい。
3. 読み込めたか、欠けや異常な割れが無いかを見る。
4. 適当にペイントできるか。
5. 書き出し(既存の Live Sync でも、標準の Export でも可)がエラーなく終わるか。

| # | 確認 | 結果(OK / NG / 判断できない) | メモ |
|---|---|---|---|
| 1 | 新規プロジェクトとして開ける |  |  |
| 2 | ペイントできる |  |  |
| 3 | 書き出せる |  |  |

判断できない項目は INCONCLUSIVE。JSON の V9 には Maya の FBX 出力結果だけが入る。
この表の内容は指揮役へ文章で添える。
