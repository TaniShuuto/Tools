# SPLIT-1 確認事項

指示と実装で揃えていない点です。勝手に範囲を広げて直してはいません。

- 補助関数 `plugin_source_files` / `plugin_source_text` は、渡したフォルダー（省略時は `sp_maya_livesync/sp_live_sync_plugin`）の配下を再帰的に歩き、`__pycache__` と名前が `.` で始まるディレクトリを除いたすべての `.py` を対象にする。この段の想定一覧は `{"__init__.py"}` だけ。トップレベルだけを見る意図だった場合は直す。
- 古いファイルの確認に失敗したときのログ文言は指示に無かった。近くの失敗ログに合わせ、`古い1ファイル版プラグインの確認に失敗しました: {0}` とした。ファイルは削除しない。親の親が空になる壊れたパスは、作業フォルダーの同名ファイルを見に行かず `None` を返す。
- `README.md` の有効化の表示名は以前 `SP Live Sync` だった。導入手順は指示どおり `Python > sp_live_sync_plugin` に合わせた。メニューに出る実名は要実機確認 (1)。
- `README.md` の「移行時点（1.0.0）の対応表」と `CHANGELOG.md` の見出し `## sp_maya_livesync/sp_live_sync_plugin.py` は、当時のファイル名の記録なので変えていない。現在のファイル一覧（`Easy_install.md` の表、`README.md` の Tools）はフォルダーに合わせた。
- `test_sp_live_sync_plugin_rebase_pure.py` の `PLUGIN_VERSION = "1.4.0"` は状態ファイルの例に渡す文字列で、プラグインの `__version__` のピンではない。指示の 2 か所（`"1.4.1"` のアサート）だけを `"1.4.2"` にした。
