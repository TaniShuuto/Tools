# SPLIT-2 確認事項

1.4.2 の `sp_live_sync_plugin/__init__.py`(約 7,000 行)を、内容を変えずに役割ごとの部品へ分割した(1.4.3)。指示と実装で揃えていない点、迷った割り当て、相談したい点です。設計は勝手に変えていません。

## 1. 割り当てで迷った名前と決め方

決め方の優先順位は、(1) 依存の向き(循環 0)、(2) 分類の表どおり、(3) ひとつの部品だけが使う定数は、その部品へ置く、(4) 同じ層の複数の部品が使う名前は、一段下の共有できる部品へ置く、とした。

| 名前 | 置き場 | 理由 |
| --- | --- | --- |
| `REBASE_REQUEST_ID_RE`, `is_valid_rebase_request_id`, `_checked_rebase_request_id`, `REBASE_DIRNAME`, `REBASE_RESULTS_DIRNAME`, `rebase_results_dir`, `rebase_result_path` | `_base` | 循環の解消(下の 2 を参照)。依頼 ID と結果ファイルのパスの 7 名だけを動かした |
| `_FINAL_EXPORT_*`(6 個), `_MESH_RECORD_*`(3 個), `CHANNEL_SUFFIXES` | `_live_engine` | `LiveSyncEngine` だけが使う |
| `RESOLUTION_CHOICES` | `_settings_model` | `SetupWizard` と `LiveSyncPanel`(どちらも層 7)の両方が使う。層 7 どうしで import しないため |
| `_PANEL_SETTING_LABELS` | `_settings_model` | `LiveSyncPanel` だけが使うが、`PANEL_SETTING_KEYS` と対の表なので同じ部品に置いた |
| `msvcrt`(try による import 切り替え) | `_base` | `_config_file_lock` だけが使う |
| `layerstack`(try による import 切り替え) | `_painter_rebase_api` | `PainterRebaseApi` だけが使う |
| `UvFillApi`(namedtuple) | `_rebase_uvfill` | UV フィルの関数が引数で受ける型。`make_uv_fill_api` は `textureset` を使うので `_painter_rebase_api` |
| `REBASE_EXPORT_DEFERRED_STATUS`, `REBASE_UV_FILL_PROGRESS`, `REBASE_AUTO_UNWRAP_UNSUPPORTED_LOG`, `REBASE_TERMINAL_WRITE_MAX` | `_rebase_protocol` | リベースの文言・定数。元は UV フィルの節にあったが、使うのはエンジン・ジョブ・司令塔 |
| `_REBASE_CFG_RELOAD_S`, `_iter_status_ids` | `_rebase_coordinator` | 司令塔だけが使う |
| `ApprovalDecision`, `_PAINTER_STATE_KEYS`, `_PAINTER_STATE_DEFAULTS`, `rebase_failure_message`, `rebase_reject_state`, `get_uv_old_index`, `get_uv_old_name`, `effective_rebase_approval` | `_rebase_protocol` | 状態ファイル・依頼の解析と文言に近い。`needs_saving_for_rebase`(Painter の状態を見る)は `_rebase_policy` |
| `normalize_project_key_*`, `project_keys_match` | `_rebase_protocol` | 純関数。`_project_key` は Painter の `project` を使うので別 |
| `_is_unsaved_project_key`, `_hash_file_hex`, `build_mesh_record` | `_base` | 指示どおり(書き出し結果ファイル・メッシュ記録) |
| `_is_project_error` | `_rebase_job` | 元の位置(ジョブの直後) |

## 2. 循環と、その解消

- 既知の循環: `publish_export_outputs`(書き出し結果, 層 1)が `is_valid_rebase_request_id` と `write_rebase_result_copy` → `rebase_result_path` / `rebase_results_dir` / `_checked_rebase_request_id`(リベース側)を呼ぶ。上の 7 名を `_base` に置いて解消した。`_rebase_protocol` は `_base` から取り込む。
- ほかに上の層への依存は見つからなかった(台本が層の規則を検査して止まる仕組みで、割り当てを 1 回直しただけで通った)。

## 3. `_engine` / `_panel` を読む箇所

調べた結果、**`_engine` / `_panel` を読み書きするのは `start_plugin` / `close_plugin`(どちらも `__init__.py`)だけ**だった。`global` 文もこの 2 関数だけ。ほかの部品には無い。`verify_split.py` と構造テストで、これを守る検査を入れた。相談事項は無い。

## 4. 相談したい点

1. **純関数の部品も、`_base` を経由して間接的に `substance_painter.logging` を読み込む。** `_rebase_protocol` などは `_base`(`_log`、依頼 ID)を import し、`_base` は `import substance_painter.logging as sp_log` をする。指示の検査(自分の import に `substance_painter` / `PySide` / `_qt` が無い)は満たしているが、「Painter なしで単独 import できる」とまでは言えない。本当に単独で読めるようにするには、`_log` を差し込める形にするなど設計の変更が要るので、やっていない。
2. **`reload_plugin` の呼ばれる順序は、Adobe の資料だけでは確定できなかった。** 資料にあるのは「フックは close のあと、start の前に実行される」「パッケージ形式のプラグインは、フックで部品を手で読み直す」ことまでで、本体の `importlib.reload` がフックの前か後かは書かれていない。そのため、フックの中で (a) `sys.modules` から部品を取り除き、(b) 本体も `importlib.reload` する実装にした。どちらの順でも部品が新しくなることをテストで確かめた。実機で確認してほしい項目は報告に書いた。
3. **導入文書。** `Easy_install.md` と `README.md` には「`sp_live_sync_plugin` フォルダーごと」コピーする旨が既にあるので変更しなかった。ただし「中の .py を一部だけ置かない」という明示的な禁止の文はない。ファイルが 15 個になったので、1 文足すかは相談したい。
4. **`test_plugin_source_has_no_uvremap_import` を、相対 import の名前だけ緩めた。** 部品から `resolve_uvremap_root` / `uvremap_root_for_project`(関数)を取り込むと、名前に `uvremap_` が含まれて誤検出になるため。`uvremap_core` のようなモジュールの import を禁じる意図は残してある(絶対 import と `from . import x` は従来どおり厳しく見る)。
5. **`__init__.py` は分割前のトップレベル名を保つため、元の標準ライブラリ・`substance_painter` の import をすべて持つ。** 使っていない import が多いのは意図どおり(互換)。新しく `import sys` / `import importlib`(`reload_plugin` 用)が増えた。
6. **計画書とレビュー結果(Projects の `claude/plugin_split_plan.md`、`claude/plugin_split_review_20261021.md`)は、この環境では読めなかった。** 指示文に書かれた仕様(層、部品名、移動する名前)に従った。レビューの 3 章の修正が指示文に反映済みなら問題ない。
