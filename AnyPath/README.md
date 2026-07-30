# AnyPath 実装状況

参照設計書: `AnyPath_技術設計書_v2.1.md`（単一の真実源）
再設計計画書: `AnyPath_再設計計画書_v1.md`（UI・運用フロー刷新の方針。2026.07.30策定、同日実装反映）

## 2026.07.30 UI・運用フロー再設計（v1.1〜1.2系）について

`AnyPath_再設計計画書_v1.md` に基づき、Core解決エンジン（`anypath/core/`）はそのまま維持しつつ、Shell層・Bridge層の一部を「原則OKボタン/選択ボタンを押すだけで良い」体験へ作り替えました。要点は以下の3点です。

1. **要確認パネルを「決定」から「確認」へ**（`shell/review_panel.py`）: 複数候補から選ばせるドロップダウンを廃止し、AnyPathが最有力と判断した1件だけを表示して「これでOK」「違う」の2択にしました。「すべてこれでOK」で一括適用もできます。
2. **探索ルートの「お試し/恒久」区別を廃止**（`bridge/root_manager.py`）: フォルダ追加は常に自動で永続化されます。上限に達したら、最近使われていない場所から自動的に入れ替わります（LRU）。利用者に選ばせません。
3. **自己診断画面を2階層化**（`shell/diagnosis_panel.py`）: 普段見えるのは「今の状態」「直近に自動で直した内容」「探す場所」「困ったときは」の4項目のみ。設定読込結果・マッピング表・dirmap・インデックス・重複インストール・Gate0観測状況は「もっと詳しく」を開いた場合のみ表示されます。
4. **要確認パネルの自動起動を新設**（`AnyPath.py`）: 旧実装は`review_panel.py`が存在するのに呼び出し配線が無く、要確認が発生してもメニュー件数表示以外に気づく手段がありませんでした。今回、全て自動解決した場合は何も表示せず、要確認が1件でもあれば控えめな通知→クリックでパネル、という導線を実装しました。
5. **メニューを2項目に削減**（`AnyPath.py`）: 「AnyPath の状態を確認」「AnyPath を一時停止する」（チェック可能トグル1項目）のみ。`report_only`（検証モード）は日常導線から排除し、環境変数`ANYPATH_MODE`経由でのみ有効化できる内部モードにしました。

### 2026.07.31 緊急バグ修正: 「探す場所」のフォルダ追加が反映されない

ユーザー報告: 「探す場所でフォルダを追加すると一度ウィンドウが一瞬で再表示され何度選択しても追加されない」。

原因: `diagnosis_controller.open_and_populate()` が、フォルダ追加/削除のたびに**パネルを閉じて自分自身を再帰呼び出し**していたが、その再帰呼び出しに渡す `config_report` が**最初にパネルを開いた時点のスナップショットのまま**だった。`add_search_root()` 自体は `machine.json` へ正しく書き込めていたが、再構築される新しいパネルにも「追加前」の一覧しか渡らず、見た目上「追加できない」状態になっていた（体感の「一瞬で再表示」は、この再帰のたびに新パネル生成→即close が繰り返されていたため）。

修正: `open_and_populate()` に `config_report_provider`（呼ぶたびに最新の `ConfigLoadReport` を返すcallable）を追加した。`AnyPath.py` は `_load_core_config` を渡す。追加/削除後は、パネルを閉じて作り直す代わりに `config_report_provider()` で設定を読み直し、**同じパネルインスタンスの探索ルート表示だけ**をその場で更新するようにした（`AnyPath.py` 1.2.1 / `diagnosis_controller.py` 1.2.1）。

### 2026.07.31 ユーザビリティ改善: 要確認パネルへのアクセス手段を追加、通知の消失問題に対応

ユーザー指摘: 「確認は右下の通知からのみしかアクセスできない。Scriptエディタの他のエラーや警告に上書きされてしまいアクセスができなくなってしまう」。自己診断画面からもアクセスできる方が良いのでは、また通知が上書きされないようにできないか、という2点の指摘。

対応した2点:

1. **自己診断画面から要確認パネルを開けるようにした**（`diagnosis_panel.py` 1.2.0 / `diagnosis_controller.py` 1.3.0 / `AnyPath.py` 1.3.0）: 第1階層の「直近に自動で直した内容」セクションに、確認が必要な項目がある場合だけ「確認する（n件）」ボタンが表示されるようにした。メニューの「AnyPath の状態を確認」からいつでもこのボタン経由で要確認パネルへアクセスできる。通知トーストは唯一のアクセス経路ではなくなった。
2. **通知トーストが自動で消えないようにした**（`review_panel.py` 1.2.0 / `AnyPath.py` 1.3.0）: `show_non_modal_toast()` の `duration_ms` に `None` を渡すと自動クローズのタイマーが設定されなくなる。要確認の通知はこの無期限表示を使うようにし、利用者が明示的にクリック（開く）または右クリック（閉じる）するまで画面右下に残り続ける。

なお、このトースト自体はScriptエディタのログとは別物（Mayaメインウィンドウ上に浮かぶ`QLabel`のツールチップ風ウィンドウ）だが、8秒で自動的に消えていたため、その間に見逃す・クリックし損ねるという点はユーザー指摘の通りだった。

### 2026.07.31 機能拡張: 拡張子違いの候補提示、リファレンス(.mb/.ma)へのCore解決適用

ユーザー質問1: 「ファイル名＋拡張子が完全に同じでなければ認識せず候補なども出てこないのは正常か」。

回答: 従来は仕様通り（意図的な保守設計）だった。`anypath/core/index.py`のインデックスはファイル名完全一致（拡張子込み）でしか引けなかったため、`wall.png`の参照が壊れていて実体が`wall.exr`にリネームされていた場合、候補にすら挙がらなかった。ユーザー判断により、拡張子違いも候補として拾えるように変更した。

- `FileIndex`に拡張子を除いたファイル名（stem）でも引けるセカンダリインデックス`_stem_map`を追加し、`lookup_by_stem()`を新設（`index.py` 1.1.0）。
- `judge_s4()`に`allow_high`引数を追加（`scoring.py` 1.1.0）。拡張子違いの候補は構造スコアがどれだけ高くても**自動適用(HIGH)を禁止**し、常にREVIEW止まりにする（誤リンクゼロの原則を維持するための安全策。拡張子が違う=中身が同一である保証は一切得られないため）。
- `cascade.py`のS4処理で、拡張子完全一致が0件の場合のみ拡張子違いへフォールバックするようにした（`cascade.py` 1.1.0）。`ResolveResult.detail`に`extension_mismatch=True`が付与される。

ユーザー質問2: 「今現在マテリアルだけしかテストしていないが、そもそものプロジェクトファイル(.mb)なども対象なのか」。

回答: `.mb`/`.ma`をリファレンスとして読み込んでいる場合、`bridge/reference_repath.py`の`list_unresolved_references()`で検出はしていたが、**Core層のS0〜S5カスケード（自動候補提示）が一切通っておらず、常に手動選択のみ**という非対称な状態だった（テクスチャは自動候補が出るのに、シーン参照は毎回「参照...」から手動で選ぶ必要があった）。ユーザー判断により、リファレンスにもテクスチャと全く同じCore解決を適用するよう変更した。

- `AnyPath.py`に`_resolve_unresolved_references()`を新設。未解決リファレンスの`raw_path`をCoreの`resolve_all()`に通し、EXACT/HIGHは自動的に`reload_reference()`で復旧する。REVIEW/FAILEDのみ要確認パネルに残る（`AnyPath.py` 1.4.0）。
- `UnresolvedReference`に`candidates`フィールドを追加（`reference_repath.py` 1.1.0）。
- `ReferenceSectionWidget`を改修し、候補があればテクスチャの行と同じ「これでOK／違う」の2択を表示するようにした（候補が無ければ従来通り「参照...」のみ）（`review_panel.py` 1.3.0）。

### 2026.07.31 機能拡張: あいまいファイル名マッチング（編集距離）、複数候補の縦並び表示

ユーザー報告: 「本当の名前は`Modo_UV_checker.jpg`なのに`Modo_UV_checke.jpg`（末尾の"r"欠落）では候補が出てこない」。拡張子違いのフォールバック（stem一致）はファイル名本体が完全一致している場合しか救えず、タイプミスや切り詰めによるファイル名自体のズレには対応していなかった。

ユーザー判断（編集距離方式を導入、複数候補は縦に並べて各々に「適用」ボタンを設置）に基づき、以下を実装した:

- `anypath/core/normalize.py`に`levenshtein_distance()`を新設（1次元DPのレーベンシュタイン距離実装）（`normalize.py` 1.1.0）。
- `FileIndex`に`lookup_fuzzy()`を新設。stem完全一致（distance=0）は対象外とし、`FUZZY_MAX_DISTANCE=2`以内・上位`FUZZY_MAX_RESULTS=5`件までを距離昇順→パス辞書順で返す（`index.py` 1.2.0）。
- `judge_s4()`/`rank_candidates()`に`fuzzy_distances`引数を追加し、`score_detail["edit_distance"]`へ記録できるようにした（`scoring.py` 1.2.0）。
- `cascade.py`のS4処理を3段フォールバックに拡張: 完全一致 → stem一致（拡張子違い） → あいまい一致（編集距離）。あいまい一致も拡張子違いと同様に`allow_high=False`を強制し、構造スコアがどれだけ高くても自動適用(HIGH)には昇格しない（P1: 誤リンクゼロを維持）（`cascade.py` 1.2.0）。
- `review_panel.py`の`ReviewRow`を全面改修。候補が1件の場合は従来通り「これでOK／違う」の2択のまま変更なし。候補が2件以上の場合は各候補を縦に並べ、それぞれに個別の「適用」ボタンを配置（編集距離がある候補は「似た名前・差分n文字相当」という補足を表示）、行の末尾に共通の「どれも違う（ファイルを選ぶ）」ボタンを1つ配置する方式にした（`review_panel.py` 1.4.0）。`ReferenceSectionWidget`側もこの`ReviewRow`をそのまま利用しているため同様に対応済み。

これにより、`Modo_UV_checke.jpg`のようなタイプミス・切り詰めによる参照切れも、候補として提示され「適用」ボタン一つで直せるようになった。あいまい一致は誤検出のリスクがstem一致より高いため、常にREVIEW止まり（自動適用なし）で、利用者の最終確認を必須にしている。

### 2026.08.01 見た目改善: 複数候補行の視認性向上

ユーザー指摘: 「候補の表示が薄くて見にくい」。複数候補モードの候補パス表示がQtのデフォルト文字色のままで、Mayaのダークテーマ上でコントラストが弱く読みにくかった。

対応（`review_panel.py` 1.4.1、見た目のみのPATCH変更）:

- 候補行を背景色（`#3a3a3a`）＋枠線＋角丸のカード状に変更し、候補同士の境界を分かりやすくした。
- パス本体を白系（`#f0f0f0`）の太字・12pxで強調表示。編集距離の補足（「似た名前・差分n文字相当」）はパスと別行に分離し、10pxの控えめなグレー（`#bbbbbb`）にして情報の優先度を視覚的に区別した。
- 「適用」ボタンに青系の背景色（hover/pressedで色が変化）を付け、押せる場所を明確にした。
- シグナル配線・データ構造・publicAPIは変更なし。既存pytest 224件は全通過、Qtスモークテストでカードスタイル・文字色・シグナル発火を再確認済み。

### 2026.08.01 バグ修正: 「これでOK」を押すと「不明なオブジェクト タイプ: bifrostFluidShape」という警告が出る

ユーザー報告: 「これでOKを押すと『# 警告: 不明なオブジェクト タイプ: bifrostFluidShape』という表示が出てきます」。

原因: `registry.py`の`DEFAULT_NODE_REGISTRY`には`bifrostGraphShape`/`bifrostFluidShape`というノードタイプ名がハードコードされているが、これらはMayaバージョンやBifrostプラグインの導入状況によって実在しない場合がある（`registry.py`自体のコメントにも「Mayaバージョンによりノードタイプ名が異なりうる」と明記されていた）。`collector.py`の`collect_all()`は`cmds.ls(type=node_type)`を`try/except`で保護していたが、**存在しないノードタイプに対する`cmds.ls(type=...)`はPython例外を投げず、警告だけをScriptエディタへ出力して空リストを返す**ため、既存の例外ハンドリングでは抑止できていなかった。「これでOK」を押すたびに`_apply_single_resolution`が内部で`collect_all()`を全レジストリに対して再実行するため、この警告が毎回表示されていた。

修正: `collector.py`に`_known_node_types()`を新設した。`cmds.allNodeTypes()`でMayaに実在するノードタイプ名の集合を1回だけ取得・キャッシュし、`collect_all()`のループ内で登録タイプが実際に存在するか確認してから`cmds.ls()`を呼ぶようにした。実在しないタイプは黙ってスキップされ、警告自体が発生しなくなる。`allNodeTypes()`自体が失敗する異常環境ではフィルタを諦めて従来通り全タイプで`ls()`を試みるフォールバックも用意した（`collector.py` 1.1.0）。maya.cmdsをモック化した手動検証で、実在しないタイプへ`ls()`が呼ばれなくなること・実在するタイプは従来通り収集されること・フォールバック時は従来動作を維持することを確認済み。

### 2026.08.01 バグ修正: 要確認パネルで「適用」を押しても行が消えない

ユーザー報告: 「適用ボタンを押してもちゃんと反映はされるのですが表示が消えない」。

原因: `ReviewPanel.set_review_items()`が作る各行（`ReviewRow`）は`confirm_requested`/`reject_requested`シグナルを発火するだけで、パネル自身の一覧からその行を取り除く処理がどこにも実装されていなかった。`AnyPath.py`側のハンドラ（`_apply_single_resolution`、`_on_reference_reload`）も、属性への適用・ログ書き込み・メニュー件数の更新は行っていたが、開いたままのパネルのUIを更新する処理が抜けていた。そのため、適用自体は正しく反映されているのに、パネル上は該当行が残ったままに見えていた。

修正:

- `review_panel.py`に`ReviewPanel.remove_row(node_key)`を新設。指定したnode_keyの行をレイアウトから取り除き`deleteLater()`する。`ReferenceSectionWidget`にも対になる`remove_reference_row(ref_node)`を新設し、全件消えた場合は「見つからない参照シーンはありません」のプレースホルダー表示に戻す（`review_panel.py` 1.5.0）。
- `AnyPath.py`の`_apply_single_resolution()`が適用に成功した際、`_review_panel_instance`（開いたままのパネル）があれば`remove_row()`を呼ぶようにした。「これでOK」「複数候補の適用」「違う（ファイルを選ぶ）」「すべてこれでOK」は全てこの関数を経由するため、1箇所の修正で4操作すべてに効く。`_on_reference_reload()`も同様に成功時`remove_reference_row()`を呼ぶ（`AnyPath.py` 1.4.1）。
- Qtスモークテストで、指定した行だけが消え他の行は残ること、未知のnode_keyを渡しても例外にならないこと、全件削除後の表示、パネルが閉じている（`None`）場合も適用自体は成功することを確認済み。

### 2026.08.01 バグ修正: リファレンス移動後の再オープンで「すべて正常」と誤表示される

ユーザー報告: 「TESTをTEST2でリファレンスとして参照していて、TEST移動後にTEST2を開くと、空のリファレンスグループのみが残り完全に読み込めていないのに、AnyPathでは全て正常だと出てしまう」。原因切り分けの結果、さらにユーザーから「先に出てくるMayaの純正機能のリファレンスポップアップをスキップを押さなければ認識できない」という決定的な情報提供があった。

原因は2つ重なっていた。

1. **根本原因（表示側）**: Mayaは参照先ファイルが見つからないシーンを開く際、標準のモーダルダイアログ（Reference Not Found）を表示し、ユーザーがそれへ応答するまで`MSceneMessage.kAfterOpen`自体が発火しない。ダイアログに気づかず放置すると、AnyPathの解決フロー（`_run_resolution_pass`）が一度も走らないまま「全て正常」に見えていた（実際には一度もチェックされていないだけ）。
2. **副次的な検出漏れ（コード側）**: `list_unresolved_references()`が、参照先が完全に見つからない/壊れているリファレンスノードに対して`referenceQuery(isLoaded=True)`や`referenceQuery(filename=True)`が例外を投げるケースを静かに`continue`でスキップしていた。軽度の未解決（ファイルが単に見当たらないだけ）は元々拾えていたが、重度の未解決（Mayaが参照ノードの情報自体をまともに返せないほど壊れている状態）を見逃していた。

修正:

- `AnyPath.py`に`_suppress_file_reference_prompts()`を新設し、`_initialize_engine()`内（`mode != "off"`の場合のみ）で`cmds.file(prompt=False)`を設定するようにした。これによりこの種の確認ダイアログ自体がMayaセッション全体で出なくなり、シーンを開いた直後にAnyPathの自動解決フローが即座に走るようになる（`AnyPath.py` 1.4.2）。**この設定はMayaセッション全体に効くため、AnyPath以外のシーンオープン時にも同種のダイアログが出なくなる点に注意。** `uninitializePlugin`側では意図的に元の値へ戻していない（他ツールが既にFalseにしていた可能性を考慮し、安易な復元は避けた）。
- `list_unresolved_references()`を堅牢化。`filename`取得失敗時は`unresolvedName=True`でのフォールバック取得を試み、それでも`raw_path`が一切取れない場合もref_node名ベースのプレースホルダーパスで検出リストへ含めるようにした。Core解決には掛からず候補ゼロのFAILEDになるが、少なくとも要確認パネルには必ず表示される（`reference_repath.py` 1.2.0）。
- モック検証で、isLoaded例外+filename取得不可のケース（プレースホルダー検出）、filename失敗だがunresolvedNameで回収できるケース、正常ロード時に誤検出しないケースの3パターンを確認済み。`_suppress_file_reference_prompts()`が`cmds.file(prompt=False)`を正しく呼ぶこと、`cmds.file`自体が例外を投げても静かに継続することも確認済み。

### 2026.08.01 重大バグ修正: 大量リファレンス環境でMayaが必ずクラッシュする

ユーザー報告: 「実験のためにSourceImageの中身をすべて別の場所に移動させたところ、普通のプロジェクトでは正常に動作したが、大量にリファレンスが集まっているプロジェクトではMayaが毎回必ずクラッシュする」。切り分けの結果「シーンを開こうとすると問答無用でクラッシュ・クラッシュレポートが出る」ことが判明。

原因: `_run_resolution_pass()`（内部で未解決リファレンスの数だけ`reference_repath.reload_reference()`、すなわち`cmds.file(loadReference=...)`を連続実行する）を、`MSceneMessage.kAfterOpen`コールバックの中から直接・同期的に呼んでいた。シーンオープンの完了通知であるkAfterOpenの最中に、さらに参照操作（`loadReference`）を行うことはMaya側の既知の不安定要因（オープン処理自体がまだ完全に片付いていない状態への再入操作になる）で、リファレンス数が少ない通常のプロジェクトでは表面化しなかったが、大量の`reload_reference`を連続で呼ぶ状況で確実にクラッシュしていたと考えられる。

修正: `_on_after_open()`/`_on_after_import_or_reference()`を改修し、実処理を`maya.utils.executeDeferred()`で1フレーム後方へ退避させるようにした（`_run_resolution_pass_safely()`という新設のtry/except保護付きラッパーを介して呼ぶ）。これによりコールバック自体は即座に完了し、Mayaがオープン処理・参照周りの内部状態を完全に片付け終えたアイドルタイミングで、AnyPathの解決処理（`reload_reference`の連続呼び出しを含む）が実行されるようになる。`executeDeferred`自体が使えない異常環境では、従来通りの同期実行にフォールバックする（`AnyPath.py` 1.4.3）。

モック検証で、`executeDeferred`経由に退避されコールバック自体が即座に完了すること、`mode="off"`では何もしないこと、`executeDeferred`が使えない場合は同期実行にフォールバックすること、`_run_resolution_pass_safely`が内部の例外を捕まえ呼び出し元へ伝播させないことを確認済み。pytest 224件も全通過。

## 実装済みスコープ

- **Core 解決エンジン**（`anypath/core/`）… §3全体、§6.3設定クランプ、§6.4探索ルート概算、§6.7ログフォーマット、§1.4 Gate0観測フォーマット
- **Boot 層**（`AnyPath.py`, `install_anypath.py`, `uninstall_anypath.py`）… §2.2〜§2.4, §4.1
- **Bridge 層**（`anypath/bridge/`）… §4（収集・適用・エフェメラル修復）、§5.3-5.4（リファレンス復旧・XGen検出）、§6.4（探索ルート追加・削除）、§6.7（ログ書き込み）、§6.9（自己診断データ収集）、§1.4（Gate0観測記録）
- **Shell 層**（`anypath/shell/`）… §6.5（要確認パネル）、§6.8（エラーメッセージ規約）、§6.9（自己診断画面）、§6.10（用語対訳）。PySide6固定（Maya 2026以降、CLAUDE.md記載の対象環境）
- **Gate 0 支援ツール**（`anypath/gate0/`）… G2/G3/G4の観測ログ機構＋確認手順書＋集計スクリプト（**実測結論は出さない**。詳細は次項）

## 配置

```
AnyPath/
  AnyPath.py                    -- autoloadプラグイン本体（Boot層。Bridge/Shellを統合配線）
  install_anypath.py             -- ドラッグ&ドロップインストーラ
  uninstall_anypath.py           -- ドラッグ&ドロップアンインストーラ
  anypath/
    __init__.py
    core/                        -- Maya API非依存（§2.1の分離を遵守）
      types.py                    確信度4段階・戦略Enum・データ構造
      normalize.py                 文字列正規化（§3.8）
      tokens.py                     UDIM/シーケンストークン処理（§3.6）
      index.py                      ファイル名インデックス構築・タイムアウト（§3.7）
      scoring.py                     S4構造照合スコアリング（§3.5）
      cascade.py                      S0〜S5カスケード＋2フェーズ解決（§3.2〜§3.4）
      config.py                        設定階層マージ・強制クランプ（§6.3）
      log_format.py                     ログ/レポートのフォーマット（§6.7）
      root_guard.py                      探索ルートのファイル数概算（§6.4）
      gate0_format.py                     Gate0観測ログのフォーマット・集計（§1.4）
    bridge/                       -- Maya API依存（Bridge層）
      registry.py                  対象ノード種の明示レジストリ（§8）
      collector.py                  ノード収集（§4.1, §4.2）
      applier.py                     属性適用・エフェメラル修復（§4.2〜§4.4）
      reference_repath.py             リファレンス復旧経路B（§5.3）
      xgen_detector.py                 XGenパス切れ検出（§5.4）
      logger.py                         修復ログ書き込み（§6.7）
      root_manager.py                    探索ルート追加/削除の設定I/O（§6.4）
      diagnosis_controller.py             自己診断画面のデータ配線（§6.9）
      gate0_probe.py                       Gate0 G2/G4観測記録（§1.4）
    shell/                        -- PySide6 UI（Shell層。Maya API非依存）
      messages.py                  エラーメッセージ規約・用語対訳（§6.8, §6.10）
      review_panel.py                要確認パネル（§6.5）
      diagnosis_panel.py              自己診断画面（§6.9）
    gate0/                        -- Gate0検証支援（AnyPath本体に依存されない）
      Gate0_検証手順.md              G2/G3/G4の実機確認手順書
      userSetup_probe.py             G3確認用の一時的診断プローブ
      gate0_check.py                  観測ログ集計スクリプト（Maya不要）

tests/anypath_core/            -- pytest 224件
```

## Gate 0（実機検証）の扱い — 重要

**ユーザー指示により、Gate 0（G2/G3/G4）は本セッションでは実測しません。** 代わりに以下の3点のみを用意しています。

1. **観測ログ機構**: 実機でMayaを操作した際（シーンを開く、`userSetup.py`を実行する等）に、判断材料となる観測データを自動的にJSON Linesへ記録する仕組み（`bridge/gate0_probe.py`, `gate0/userSetup_probe.py`）。
2. **確認手順書**: `anypath/gate0/Gate0_検証手順.md`。G2・G3・G4それぞれについて、人間が実機Mayaで何を準備し何を操作すればよいかを具体的に記載しています。
3. **集計スクリプト**: `anypath/gate0/gate0_check.py`。溜まったログを読み込み、「§1.4の表を埋める材料が揃ったか」を報告します（Maya不要、通常のpython3で実行可）。

**実測の結論（表を実際に埋める作業）は人間が行ってください。** 自己診断画面（§6.9）にも「Gate 0 観測状況」セクションを追加し、同じ集計結果を表示します。

## テスト結果

```
224 passed
```

内訳: 再設計前の163件（Core層108件＋log_format/root_guard/gate0_format/shell.messages 55件）＋ UI再設計で追加した23件（`test_root_manager.py` 新規・`test_shell_messages.py` 追加分）＋ 拡張子違いフォールバックで追加した14件（`test_index.py`のlookup_by_stem系6件・`test_scoring.py`のallow_high系4件・`test_cascade.py`のS4フォールバック系3件、および既存への追記1件）＋ あいまいファイル名マッチングで追加した24件（`test_normalize.py`のlevenshtein_distance系8件・`test_index.py`のlookup_fuzzy系7件・`test_scoring.py`のfuzzy_distances系4件・`test_cascade.py`のS4あいまい一致フォールバック系5件）。決定論性テストは実際に `PYTHONHASHSEED` 0〜9 の10通りで別プロセスを起動し、全結果が完全一致することを確認済みです。

PySide6 UIコンポーネント（`ReviewPanel`, `DiagnosisPanel`, `ReviewRow`, `ReferenceSectionWidget`, `StatusBadge`, `CollapsibleSection`）は、`QT_QPA_PLATFORM=offscreen` 環境でのスモークテストにより、インスタンス化・データ投入・シグナル配線（2択ボタンのconfirm/reject、折りたたみセクションの開閉、探索ルート追加ボタン、参照シーンのOK/違うボタン等、および複数候補時の縦並び「適用」ボタン各々のconfirm_requested発火・「どれも違う」ボタンのreject_requested発火）が例外なく動作することを確認済みです（pytestの自動テストには含めていません。Qtのオフスクリーン実行にシステムライブラリの追加インストールが必要なため、CI組み込みは別途検討してください）。

`_resolve_unresolved_references()`（AnyPath.py、リファレンスへのCore解決適用）についても、maya.cmdsをモック化した手動スクリプトで、EXACT/HIGHの自動適用・REVIEWの候補保持・FAILEDのそのまま保持・reload_reference失敗時のフォールバック・空リストの早期リターンを個別に確認済みです（AnyPath.py自体がMaya API依存のためpytest自動テストの対象外。テスト手法はREADMEの「動作確認」節と同様の制約）。

## 実装上の主な設計判断

- **`undoInfo(stateWithoutFlush=...)`**: 設計書の記述通りだと誤解しやすいですが、Maya公式ドキュメントで実際の意味（既存Undoキューをフラッシュせずにundo/redoのon/offを切り替えるフラグ）を確認した上で実装しています（`bridge/applier.py`）。
- **`_run_resolution_pass()`**: `AnyPath.py`にBridge層全体のフロー（収集→スナップショット→Gate0観測→解決→適用→ログ→未解決リファレンス/XGen検出→Gate0 G4観測）を配線しました。`kAfterOpen`と`kAfterImport`/`kAfterReference`はこの共通関数を呼び出します。
- **自己診断画面のフォールバック**: PySide6版が何らかの理由で開けない場合、従来の`confirmDialog`簡易表示へ自動的にフォールバックします（P4: 個別機能の失敗でMayaを不安定にしない）。
- **AnyPath.pyのバージョン**: Bridge/Shell層統合に伴いMINORバージョンアップ（1.0.0 → 1.1.0）。CLAUDE.mdのバージョニング規約に従い、ヘッダーへ変更履歴を追記しています。

## 未実装（今回スコープ外として残っているもの）

- 利用者向けドキュメント（§11）: `README_利用者向け.md`
- Maya結合テスト（§9.2、`mayapy`バッチでの実機テスト）— Maya実行環境がないため未実施
- Gate 0の実測結論そのもの（上記の通り、道具立てのみ）
- 再設計計画書 §7 の未決事項（実機での使用感を見ながら判断予定）:
  - 探索ルートのLRU自動入れ替えについて、複数プロジェクトを行き来する実際の使い方で問題が出ないか
  - 自己診断画面「もっと詳しく」内の項目のうち、格納ではなく完全削除してよいものがあるか
  - 初回起動時のオンボーディングメッセージ（「これで自動的に直ります」の一言）の要否 — 今回は未実装
