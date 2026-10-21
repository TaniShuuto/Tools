# tools/split_plugin

`sp_live_sync_plugin/__init__.py`(分割前の 1 ファイル)を、役割ごとの部品へ機械的に分割する台本です。コードの中身は書き換えません。

1. 分割前のファイルを用意する: `git show 95f277c:sp_maya_livesync/sp_live_sync_plugin/__init__.py > before_init.py`
2. 分割する: `python tools/split_plugin/split_plugin.py before_init.py <出力先フォルダー> [--set-version 1.4.3]`(何度実行しても同じ出力。出力は CRLF。版を上げるときだけ `--set-version`)
3. 検査する: `python tools/split_plugin/verify_split.py before_init.py <出力先フォルダー> [--set-version 1.4.3]`(すべて `[OK]` なら合格。不合格だと終了コード 1)
4. どの名前をどの部品へ移すかは `assignment.py` の表で決める。表に無い名前があると `split_plugin.py` はエラーで止まる。
5. 層の規則(下の層は上の層を import しない。同じ層でも循環させない)に反する割り当ては、エラーで止まる。
6. 各部品の先頭の import(標準ライブラリ、`substance_painter`、`_qt`、部品間の相対 import)と `__all__` は自動で付く。
7. `__init__.py` は全部品を最初にまとめて import し、分割前のトップレベル名を明示列挙で再エクスポートする(`import *` と遅延 import は使わない)。
8. 分割前には無い `reload_plugin`(再読み込みフック)だけは、台本が `__init__.py` の末尾へ足す。
9. 検査台本はそのほか、`--names-out <ファイル>` で分割前のトップレベル名の一覧を書き出せる。
10. 分割は 1.4.3 で済んでいる。以後の変更は、分割後の部品を直接直す(この台本は再実行のためだけに残してある)。
