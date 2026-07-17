"""
maya_live_sync.py  (Phase 1: GUI版 + Phase 2: シェーダー自動生成/マッピングパネル + Phase 2最適化)
========================================================================================================
Maya 側スクリプト(B案:自前パイプライン)

Phase 1:
    監視ON/OFF・監視フォルダ・レンダラー選択・shelfボタン設置をGUIから
    行えるようにした。

Phase 2:
    SP側が検出した「known_texture_sets」をもとに、Maya側で対応する
    シェーダーネットワークが無いテクスチャセットを一覧表示し、
    ワンクリックで Arnold 用の標準的なシェーダーネットワークを
    自動生成できるようにした。

Phase 2 最適化(今回追加分・実機テスト前の自己レビューで発見した不具合修正):
    - マッピング状況を確認するだけの is_texture_set_mapped() が、内部で
      「設定を保存する」処理(監視の停止・再起動を伴う)を呼び出して
      しまっていた。単に一覧を表示したいだけなのに毎回監視が再起動される
      不具合だったため、監視を再起動しない専用の保存関数に分離した。
    - シェーダー生成中に例外が発生した場合、作成途中のノードがシーンに
      残ってしまう問題があったため、生成した全ノードを記録しておき、
      失敗時には自動的にロールバック(削除)するようにした。
    - 同名のシェーダー/シェーディンググループが既に存在する場合、Mayaが
      自動的に別名にリネームしてしまい、意図しないノードができる恐れが
      あったため、事前にチェックして名前が衝突する場合は生成前にエラーを
      出すようにした。
    - どのチャンネルを生成するかをGUI上のチェックボックスで選べるように
      した(すべてのテクスチャセットにHeight/Emissiveが必要とは限らない
      ため)。
    - displacementShaderのscale属性を、存在すればベストエフォートで
      設定できるようにした(Arnoldでの実際の見え方は環境依存のため、
      要現場検証)。

    共通して、known_texture_sets はSP側のPhase 2最適化によりプロジェクト
    単位で管理されるようになったため、Maya側もそれに合わせて
    known_texture_sets_by_project から現在のプロジェクトに対応する一覧を
    参照する(ただし現状Mayaはどの.spp由来かを直接知る手段がないため、
    設定ファイル内の全プロジェクト分をまとめて表示する簡易対応とする。
    複数プロジェクトを横断して作業する場合は誤って別プロジェクトの
    テクスチャセットが表示されうる点に注意。詳細は企画書を参照)。

Phase 3(実機テストのフィードバックを受けた恒久対応 + GUI直感化):
    - AEfileTextureReloadCmd(MEL)への依存をやめ、fileTextureNameを
      一旦空にしてから書き戻す方式でテクスチャを強制再読込するように
      変更した(アトリビュートエディタ未使用環境での失敗を解消)。
    - Height変位のセンタリング漏れを修正(remapValueで0-1を-0.5-0.5に
      変換してからdisplacementShaderへ接続)。
    - SP側が書き込む active_project_key を優先して参照するようにし、
      複数SPプロジェクト混在時の一覧の曖昧さを解消した(active_project_key
      が無い場合は従来通りの全プロジェクト合算にフォールバック)。
    - SP側が記録する texture_set_export_prefix を優先して使うようにし、
      _safe_name() の予測とのズレを解消した。
    - colorSpace設定を複数の候補名を順に試す方式にし、カラーマネジメント
      設定(ACES/カスタムOCIO等)による失敗に対して頑健にした。
    - 初回セットアップウィザード(QWizard)を追加し、監視フォルダ・
      レンダラー設定と接続テストを対話形式で行えるようにした。

複数プロジェクト対応(2026.07.14):
    - SP側がFinalを <final_export_dir>/<プロジェクト別サブフォルダ>/ に
      書き出すようになったことに対応。_active_final_dir() を追加し、
      reload_final_textures / _process_pending_changes / _managed_dirs /
      detect_current_quality / switch_texture_quality の全てが
      サブフォルダを考慮するようにした。
    - 上記のうち switch_texture_quality と detect_current_quality は、
      一度目の改修作業がタイムアウトで中断した際にサブフォルダ対応が
      漏れたまま残ってしまい、「Finalへの切り替えが常に失敗する」
      不具合として実機で確認・修正した。この種の版ズレに気付きやすく
      するため、__version__ を追加し、import時・show_ui()実行時・
      ウィンドウタイトルの3箇所に必ずバージョンを表示するようにした。
      今後この関数群を変更する際は、__version__ の日付を上げること。

2026.07.14-02:
    - show_ui() を、シェルフボタンの再クリックで反応が無いように見える
      (エラーも出ない)不具合に対応。原因は、一度ウィンドウを閉じる/
      隠す等でworkspaceControlがvisible=Falseのまま残ると、
      .show(dockable=True) だけではその状態から復帰しないという
      MayaQWidgetDockableMixin側の既知の挙動だった。show_ui() 内で
      workspaceControlの存在とvisible状態を確認し、隠れていれば
      visible=True + restore=True で明示的に前面へ出すようにした。

2026.07.14-03:
    - Live/Preview も Final と同じくプロジェクト別サブフォルダ
      (<watch_dir>/<active_watch_subfolder>/)へ書き出されるように
      なったことに対応。_active_watch_dir() を新設し、start() /
      _process_pending_changes() / reload_textures() / _managed_dirs() /
      detect_current_quality() / switch_texture_quality() /
      create_shader_network() の全てがアクティブサブフォルダを考慮する
      よう修正した。project_poll_timer のコールバックも
      _ensure_active_dirs_watched() に統合し、Live/Final両方の
      プロジェクト切り替え追従を行うようにした。
    - 背景: 学校の共用PC環境で、watch_dir 直下に過去の別Windowsユーザー
      が残したファイルが混在していると、NTFSの所有権(ACL)により別
      ユーザーからは読めず(PermissionError: [Errno 13])、プレビューへ
      切り替えてもテクスチャが一切表示されない不具合が実機で確認された。
      Finalは既にサブフォルダ化されていたため発生せず、Liveのみで
      再現していた。切り替え先を常に「自分が今回新規作成したサブ
      フォルダ」にすることで、この種の所有権衝突を回避する。

2026.07.14-04:
    - v03で active_watch_dir 追従の仕組みを入れたが、self.config が
      LiveSyncWatcher 初期化時の1回(load_config())しか読まれず、以後
      SP側が共有設定ファイルへ書き込む active_watch_subfolder 等の
      更新をMaya側が一切拾えていない不具合が残っていた
      (reload_config()というメソッド自体はあったが、定義されている
      だけでどこからも呼ばれていなかった)。実機では、ディスク上の
      設定ファイルには正しく "active_watch_subfolder": "TEST_d397fd"
      等が書き込まれているのに、_active_watch_dir() がNoneを返し
      続け、switch_texture_quality() がプレビュー切り替え先を
      watch_dir 直下のままにしてしまう(=v03で対策したはずの所有権
      問題が再発する)という形で発現した。
    - _refresh_dynamic_config() を新設。SP側が随時更新する動的な値
      (active_watch_subfolder / active_final_subfolder /
      active_project_key / texture_set_export_prefix)だけをログ無しで
      軽量に読み直す。3秒間隔の _ensure_active_dirs_watched() の冒頭、
      および switch_texture_quality() / reload_textures() の冒頭
      (ユーザー操作やイベント起点で、タイマーの次の実行を待たず
      即座に最新値を掴む必要があるため)から呼ぶようにした。
"""

import os
import re
import json
import time
import datetime
import subprocess

import maya.cmds as cmds
import maya.OpenMayaUI as omui
import maya.OpenMaya as om

try:
    from PySide2 import QtCore, QtWidgets
    from shiboken2 import wrapInstance
    from shiboken2 import isValid as _shiboken_is_valid
except ImportError:
    from PySide6 import QtCore, QtWidgets
    from shiboken6 import wrapInstance
    from shiboken6 import isValid as _shiboken_is_valid

from maya.app.general.mayaMixin import MayaQWidgetDockableMixin


# --- [DIAG-C1] -----------------------------------------------------------
# 一次切り分け用: install.py 側の _destroy_stale_livesync_window() が
# 「Internal C++ object (QFileSystemWatcher) already deleted」を出す
# 不具合の原因確定用ヘルパー。cmds.deleteUI(workspaceControl, control=True)
# の直後、Python側の window / watcher / fs_watcher 各オブジェクトの
# Qt C++実体がまだ生きているか(shiboken.isValid)を個別に確認できるようにする。
# 「deleteUIの時点で子オブジェクトも道連れで破棄される」という仮説を
# 実機で裏付けるための計装であり、まだ修正はしていない。
def diag_c1_check_qobject_validity(window):
    """window / watcher / fs_watcher それぞれの Qt C++ 実体の生存状態を
    dict で返す。install.py の _destroy_stale_livesync_window() から
    deleteUI直後・watcher.stop()呼び出し直前に呼ぶ想定。
    """
    result = {}
    try:
        result["window_valid"] = _shiboken_is_valid(window) if window is not None else None
    except Exception as e:
        result["window_valid"] = "check_error: {0}".format(e)
    try:
        watcher = getattr(window, "watcher", None)
        result["watcher_exists_in_python"] = watcher is not None
        result["watcher_valid"] = _shiboken_is_valid(watcher) if watcher is not None else None
    except Exception as e:
        result["watcher_valid"] = "check_error: {0}".format(e)
    try:
        fs_watcher = getattr(watcher, "fs_watcher", None) if watcher is not None else None
        result["fs_watcher_exists_in_python"] = fs_watcher is not None
        result["fs_watcher_valid"] = _shiboken_is_valid(fs_watcher) if fs_watcher is not None else None
    except Exception as e:
        result["fs_watcher_valid"] = "check_error: {0}".format(e)
    try:
        poll_timer = getattr(watcher, "project_poll_timer", None) if watcher is not None else None
        result["project_poll_timer_valid"] = _shiboken_is_valid(poll_timer) if poll_timer is not None else None
    except Exception as e:
        result["project_poll_timer_valid"] = "check_error: {0}".format(e)
    print("[DIAG-C1] QObject生存確認: {0}".format(result))
    return result


# ============================================================
#  バージョン情報
# ============================================================
#  複数のマシン・複数のタイミングでこのファイルを更新していると、
#  「今Mayaで実際に動いているコードがどの版か」が分からなくなり、
#  切り分けに時間がかかることが実際に起きた(switch_texture_qualityが
#  複数プロジェクト対応"前"の古いロジックのまま残っていたケース)。
#  それを即座に確認できるよう、import された時点で必ずバージョンを
#  Script Editor に出力する。日付を上げ忘れないよう、変更のたびに
#  ここを更新すること。
#
#  2026.07.15-01 変更点(まとめて実施):
#    1. セッションロックの残骸対策: プロセス生存確認(Windows: tasklist)
#       と経過時間フォールバックを追加。Maya終了時(kMayaExiting)に自分の
#       ロックを確実に解除するようにし、UIから手動で無視/削除もできる
#       ようにした。
#    2. Mayaシーン切り替え検知: MSceneMessage の kAfterOpen/kAfterNew に
#       コールバックを追加し、シーンが切り替わったら監視を自動停止する
#       ようにした(切り替え前のシーンを追い続ける不具合の対策)。
#    3. texture_set_export_prefix / texture_set_shading_engine_map を
#       known_texture_sets_by_project と同様、SPプロジェクトキーで
#       分離したネスト構造に変更(同名テクスチャセットが別プロジェクトに
#       存在する場合の誤判定・誤上書きを防ぐ)。旧形式(フラット辞書)の
#       設定ファイルは初回読み込み時に自動移行する。
#    4. Mayaシーン ⇔ SPプロジェクトの紐付けを追加。シーンファイルの
#       fileInfo に紐付け先のSPプロジェクトキーを保存し、シーンを開く
#       たびにその紐付けを見て自動的に対応プロジェクトを判定できる
#       ようにした。
#    5. known_texture_sets_by_project を _refresh_dynamic_config() の
#       対象に追加し、3秒ポーリングでテクスチャセット一覧・マテリアル
#       構造タブが自動更新されるようにした。
#    6. 同期タブ最上部に状態バーを新設(シーン⇔SPプロジェクト対応表示、
#       他セッション警告、一覧の最終更新時刻)。
#
#  2026.07.15-02 緊急修正:
#    project_poll_timer が生成・シグナル接続はされているのに、どこからも
#    start() されておらず一度もタイムアウトが発火しない不具合があった
#    (2026.07.15-01より前から存在した既存バグ)。この結果、上記2〜5が
#    依存する _refresh_dynamic_config() の定期実行が行われず、実質的に
#    機能していなかった。具体的には以下の症状として実機で確認された:
#      - SP側でプロジェクトを切り替えても状態バーの「シーン⇔SP
#        プロジェクト」表示が古いまま追従しない
#      - 同名テクスチャセットが別プロジェクトに存在する場合、
#        active_project_key が古いままシェーダー割当マップを参照して
#        しまい、意図しない(別プロジェクトの)マテリアルが誤って
#        「対応済み」と判定され、そちらが適用されてしまうことがあった
#    watcher.start() で project_poll_timer.start()、watcher.stop() で
#    project_poll_timer.stop() するよう修正した。
#
#  2026.07.15-03 緊急修正:
#    reload_textures() / reload_final_textures() が「fileノードが今まさに
#    現在のアクティブサブフォルダを完全一致で参照しているか」でしか
#    対象を判定しておらず、以下の手順で自動反映が効かなくなる不具合が
#    あった:
#      1. シェーダー生成時点のSPプロジェクト向けサブフォルダを参照する
#         fileノードが作られる
#      2. SP側で別プロジェクトに切り替わり、アクティブなサブフォルダが
#         変わる
#      3. 古いサブフォルダを参照したままのfileノードは、以後
#         「今のwatch_dir/final_dirと不一致」として永久にスキップされ、
#         SP側が更新しても自動反映されなくなる
#    switch_texture_quality()ボタンを押すと直るように見えていたのは、
#    あちらが古いサブフォルダも含めて対象を拾い、パスを強制的に現在の
#    サブフォルダへ書き換える実装だったため(副作用的に直っていた)。
#    reload_textures() / reload_final_textures() 側にも、watch_dir /
#    final_export_dir 直下にある「現在アクティブではないサブフォルダ」を
#    検出し、該当ノードのパスを現在のサブフォルダへ補正してから再読込する
#    処理を追加した。
#
#  2026.07.15-04 安全性の見直し(全体の再監査で発見):
#    上記2026.07.15-03の補正ロジックは、当初 watch_dir/final_export_dir
#    直下の「現在アクティブでない全サブフォルダ」を無差別に補正対象と
#    していた。これだと、同一シーン内に(過去の作業等で)別の無関係な
#    プロジェクト向けのfileノードが混在していた場合、そちらのパスまで
#    誤って現在のプロジェクトのサブフォルダへ書き換えてしまう危険が
#    あった。
#    対策として、_ensure_active_watch_watched() / _ensure_active_
#    final_watched() が「サブフォルダが切り替わる直前に自分が監視して
#    いたサブフォルダ」を記録するようにし(_last_active_watch_dir /
#    _last_active_final_dir)、reload_textures() 側の補正対象はこの
#    「直前の自分自身のサブフォルダ」1つだけに限定するよう変更した。
#    これにより、他プロジェクトのサブフォルダを誤って巻き込むことなく、
#    2026.07.15-03が解決した「プロジェクト切り替え直後の取りこぼし」
#    問題だけを安全に救済する。
#
#  2026.07.15-05 緊急修正(2件):
#    1. 状態バーの「シーン⇔SPプロジェクト」表示で、SP側が未起動/
#       未検出(active_project_keyがNone)の場合でも「一致している」と
#       誤判定して緑色表示になってしまうバグを修正した。SP未起動は
#       一致/不一致とは別の、独立した灰色表示に変更した。
#    2. reload_textures()/reload_final_textures() の古いサブフォルダ
#       補正(2026.07.15-03で追加、07.15-04で「直前の1つに限定」と
#       安全性を見直したもの)が、sp_to_aiStandardSurface.py(aiSS)
#       経由で生成されたfileノードを救済できていなかった。aiSSは
#       シェルフボタンを押した瞬間のFinalフォルダパスを一度だけ
#       自動入力する設計のため、その後SP側でプロジェクトが切り替わると
#       古いプロジェクトのフォルダを参照するノードが生成されてしまう。
#       これは本ウォッチャーの _last_active_watch_dir/
#       _last_active_final_dir(自分が監視した直前のフォルダのみを記録)
#       の追跡対象外だったため、07.15-04時点では補正できなかった。
#       対策として、補正対象を watch_dir/final_export_dir 直下の
#       「現在アクティブでない全サブフォルダ」に戻しつつ、新設した
#       _matches_known_texture_set_prefix() で「ファイル名が現在の
#       プロジェクトの既知テクスチャセットのprefixと一致するか」を
#       確認してから補正するようにした。ディレクトリの一致ではなく
#       ファイル名で安全性を担保するため、無関係な他プロジェクトの
#       サブフォルダを誤って巻き込む心配がない。
#
#  2026.07.15-06 緊急修正:
#    _last_flag_mtime_watch / _last_flag_mtime_final(SP側の書き出し
#    完了フラグ _sync_complete.flag のmtimeを見て、新しい変更が
#    あったかを判定する比較値)が、「どのフォルダを最後に見たか」を
#    区別しない単一のグローバル値だった。SP側でプロジェクトを
#    切り替えて watch_dir/final_dir の実体(アクティブサブフォルダ)が
#    変わっても、この比較値はリセットされないままだったため、
#    新しいプロジェクトの完了フラグの方が(ファイルとしては新しくても)
#    mtime自体は前のプロジェクトで記録した値より小さいことがあり、
#    「まだ古い」と誤判定されて自動反映(reload_textures/
#    reload_final_textures)が一切トリガーされない不具合があった。
#    この状態は表示品質を手動で切り替えることでしか解消されず、
#    「一度直ってもすぐ手動操作頼みに戻る」という体感の不具合として
#    現れていた。
#    対策として、_process_pending_changes() で監視中のフォルダ自体が
#    変わったことを検知したら、対応するmtime比較値を0にリセットする
#    ようにした。
#
#  2026.07.16 緊急修正:
#    Mayaシーン⇔SPプロジェクトの紐付け機能で、実際には同じプロジェクト
#    (例: aaaaa.spp)を指しているのに、状態バーが「対応先とSP側の
#    プロジェクトが異なります」という赤字の不一致警告を出し続ける
#    不具合があった。
#    原因は、紐付け情報の読み取り(_get_current_scene_project_link)で
#    行っていた value.encode("utf-8").decode("unicode_escape") という
#    変換が、Windowsパスのバックスラッシュ区切りを破壊していたこと。
#    "\B" のような並びが制御文字に化けてしまい、cmds.fileInfo に
#    実際に保存された値は正しいのに、読み取り時だけ壊れた文字列に
#    なり、SP側から来る生パス(active_project_key)との文字列比較で
#    常に不一致と判定されていた。
#    対策として、危険な unicode_escape 変換を撤去し、代わりに
#    保存・比較の双方でスラッシュ区切りへの正規化
#    (_normalize_project_key_for_compare)を行うようにした。これにより
#    Windowsのバックスラッシュ区切りパスとSlashパスのどちらが来ても
#    正しく同一と判定できるようになった。
#
#  2026.07.16-02 緊急修正:
#    シミュレーションにより、v01/v02(SP側)の修正後もなお「監視フォルダ名
#    は正しいのに、Maya側の紐付け表示だけがテンプレート名/__unsaved__の
#    まま」という矛盾が起こり得ることが判明した。
#    真因は _set_current_scene_project_link() が「ボタンを押した瞬間の
#    active_project_keyを一度きり書き込むだけ」の設計であること。SP側の
#    プロジェクトがまだ未保存("__unsaved__")の段階で「SPプロジェクトを
#    設定」ボタンを押すと、"__unsaved__"という値がシーンに永久に
#    焼き付けられ、その後SP側で実際に保存してactive_project_keyが
#    正しい値に更新されても、一度焼き付けられた紐付け情報だけは
#    自動更新されずに取り残されていた。
#    対策として (1) active_project_keyが"__unsaved__"の間はボタンでの
#    紐付けをブロックし、先にSP側で保存するよう案内する、(2) 既に
#    "__unsaved__"が焼き付いてしまっている場合は状態バーで明示し、
#    再設定を促す、の2段構えとした。
#
#  2026.07.16-03 追加修正(他箇所へのシミュレーション横展開で発見):
#    create_shader_network() に、SP側プロジェクトが未保存
#    ("__unsaved__")の間はシェーダー生成をブロックするガードを追加した。
#    シミュレーションにより、未保存の段階でシェーダーを生成すると、
#    生成されるfileノードが "__unsaved__" 用の一時サブフォルダを参照
#    したまま、SP側で保存後もパスが更新されず、is_texture_set_mapped()
#    のフォールバック走査にも該当しなくなる(「対応済みのはずが未対応と
#    誤判定され続ける」「再生成しようとすると名前衝突でエラーになる」)
#    ことが判明したため。シーンの紐付け機能(_on_link_scene_to_sp_
#    project)で採用したのと同じ「未保存の間はブロックする」方針を
#    横展開した。
#    なお、同時に実施した他箇所へのシミュレーション(missing_streakの
#    プロジェクト切り替え時リセット、セッションロックのpid照合による
#    多重セッション保護)はいずれも問題なしと確認された。
#    _last_active_watch_dir/_last_active_final_dir は既にv05の設計変更で
#    参照されなくなったデッドコードであることも判明したため、その旨を
#    コメントで明記した(実害なし、削除は任意)。
#
#  2026.07.16-04 緊急修正:
#    register_user_setup() が「マーカーが既に存在する場合は無条件に
#    スキップする」設計だったため、過去に一度でも登録したユーザーは
#    install.py を何度再実行しても userSetup.py の中身(自動起動
#    ロジック)が永久に更新されないという不具合があった。
#    実機で以下の2症状として確認された:
#      1. Maya起動中、メインウィンドウの構築が終わる前にLiveSyncの
#         ウィンドウだけが先行して表示されてしまう(旧バージョンで
#         auto_open=Trueかつ lowestPriority 遅延処理が無い状態のまま
#         登録されていたため)
#      2. シェルフから何度起動し直しても古いバージョンの挙動のまま
#         に見える(userSetup.py経由の起動時importで、古いロジックの
#         ままの状態がセッションを通じて影響し続けていたため)
#    対策として、マーカーが既に存在する場合は「スキップ」ではなく
#    「一致するブロックを取り除いてから、新しい内容で書き直す」方式に
#    変更した(uninstall.pyの_remove_autostart_block()と同じ抽出
#    ロジックを流用)。バックアップの取得対象が「削除後」の内容に
#    なっていた見落としも合わせて修正した。
#    なお install.py 自体は auto_open=False で登録するため、この修正が
#    適用されると次回install.py実行時に「先行して開く」設定自体が
#    除去される(importのみのシンプルなブロックに置き換わる)。
#
#  2026.07.16-05 緊急修正:
#    v2026.07.16-04で「マーカーがあれば書き直す」方式に変更した結果、
#    install.pyを実行するたびに毎回 userSetup.py への書き込みが発生
#    するようになった。これにより、Maya 2026のセキュリティ機能
#    (Secure UserSetup Checksum verification)が毎回反応し、
#    「userSetup.pyの内容が変わりましたが良いですか」という確認
#    ポップアップが再インストールのたびに表示されるようになってしまった
#    (過去のセッションで、このポップアップに正しく応答しないまま
#    作業を進めるとUndo初期化等の起動シーケンスが乱れる不具合が
#    確認されている)。
#    対策として、既存の登録ブロックと、今回生成しようとしている内容が
#    完全に一致する場合は、ファイルへの書き込み自体を行わないように
#    した。これにより、実際にロジックが変わった場合(バージョンアップ
#    等)のみ書き込みが発生し、チェックサム確認ポップアップも本当に
#    必要な時にしか出なくなる。
#
#  2026.07.16-06 緊急修正(PBRテンプレート表示問題の再発を追跡して発見):
#    self.watcher.config(active_project_keyを含む)は、これまで
#    project_poll_timer(3秒間隔)経由の _refresh_dynamic_config() でのみ
#    ディスクから読み直されていた。しかし project_poll_timer は
#    watcher.enabled(監視ON)の間だけ動作するため、監視をOFFにしたまま
#    (あるいは一度もONにしないまま)SP側でプロジェクトを保存しても、
#    Maya側のメモリ上の active_project_key はいつまでも更新されず、
#    状態バーがテンプレート名や古いプロジェクト名を表示し続ける不具合が
#    あった。「監視をONにしたら直った」という報告はこれと一致する。
#    対策として、_refresh_scene_link_label() の呼び出し時には毎回
#    _refresh_dynamic_config() を明示的に呼ぶようにし、状態バーの表示が
#    監視のON/OFFに関わらず常に最新のディスク上の値を反映するようにした
#    (シグナル発火による再入は軽量なフラグで防止)。
#    同じ理由で _refresh_material_table() (マテリアル構造タブ)にも
#    同様の明示的読み直しを追加した。
__version__ = "2026.07.16-06"

# ウィンドウのobjectNameと、Mayaがそこから自動生成するworkspaceControl名。
# 「WorkspaceControl」というsuffixはMaya側の仕様(objectName + "WorkspaceControl")
# であり、show_ui()側の再表示処理と、ここでの命名を一致させておく必要がある。
WINDOW_OBJECT_NAME = "MayaLiveSyncWindow"
WORKSPACE_CONTROL_NAME = WINDOW_OBJECT_NAME + "WorkspaceControl"

print("[maya_live_sync] loaded version: {0}  (file: {1})".format(
    __version__, os.path.abspath(__file__)))


CONFIG_DIR = "C:/SPMayaLiveSync"
CONFIG_PATH = os.path.join(CONFIG_DIR, "live_sync_config.json")

DEFAULT_CONFIG = {
    "watch_dir": "C:/SPMayaLiveSync/live",
    # Phase 6: SP側が「今すぐ同期」または「プロジェクト保存」時に高画質版を
    # 書き出すフォルダ。ライブ監視の対象ではなく、手動での品質切り替え
    # (switch_texture_quality())でのみ参照する。
    "final_export_dir": "C:/SPMayaLiveSync/final",
    "renderer": "arnold",
    "file_format": "png",
    "raw_colorspace_suffixes": ["Roughness", "Metallic", "Normal", "Height", "AO"],
    "known_texture_sets_by_project": {},
    # 2026.07.15-01: 以前はテクスチャセット名をキーにしたフラットな
    # 辞書だったため、別プロジェクトに同名のテクスチャセット(例:
    # "Body")が存在すると、シェーダーの割当状況を誤判定したり、
    # 書き出しファイル名のprefixを取り違えたりする不具合があった。
    # known_texture_sets_by_project と同様、SPプロジェクトキーで
    # 一段ネストする形式に変更した:
    #   { project_key: { texture_set_name: shading_engine_name } }
    # 旧形式(フラット辞書)の設定ファイルは load_config() が自動的に
    # この新形式へ移行する(_migrate_legacy_flat_maps 参照)。
    "texture_set_shading_engine_map_by_project": {},
    # Phase 3: SP側が「今開いているプロジェクト」を書き込むキー。
    # これが分かれば、複数プロジェクトを横断して作業した場合でも
    # 別プロジェクトのテクスチャセットが一覧に混在しなくなる。
    "active_project_key": None,
    # 複数プロジェクト対応: Finalは <final_export_dir>/<subfolder>/ に
    # 書き出される。SP側が現在アクティブなプロジェクトのサブフォルダ名を
    # ここに書き込むので、aiSSボタンはこれを読んで正しい取り込み先を
    # 自動入力する。None のときは final_export_dir 直下を使う(後方互換)。
    "active_final_subfolder": None,
    # 複数プロジェクト対応(所有権問題回避、2026.07.14-02): Live/Preview は
    # <watch_dir>/<subfolder>/ に書き出される。SP側が現在アクティブな
    # プロジェクトのサブフォルダ名をここに書き込むので、Maya側の監視・
    # 反映処理はこれを読んで追従する。None のときは watch_dir 直下を
    # 使う(後方互換)。
    "active_watch_subfolder": None,
    # Phase 3: SP側が実際に書き出したファイル名のprefix
    # (テクスチャセット名 -> prefix文字列)。分かっていればこちらを
    # 最優先で使い、_safe_name() による予測はフォールバックとする。
    # 2026.07.15-01: shading_engine_map と同じ理由でプロジェクトキー
    # ごとにネストする形式へ変更(旧キー "texture_set_export_prefix" は
    # 後方互換のため残しつつ、読み込み時に自動移行する):
    #   { project_key: { texture_set_name: prefix文字列 } }
    "texture_set_export_prefix_by_project": {},
    "setup_wizard_completed": False,
    # Phase 5: 前回終了時の監視ON/OFF状態を覚えておき、次回起動時に
    # 自動的に復元する(毎回手動でONを押す手間を無くすため)。
    "watch_enabled": False,
    # 2026.07.15-01: このMayaシーンファイルが、どのSPプロジェクトに
    # 対応するかは本来シーンファイル自体(fileInfo)に保存するが、
    # 未保存シーンではfileInfoを永続化できないため、直近のセッション
    # 内での紐付けをここにも保持しておく(セッション内フォールバック用)。
    # 恒久的な紐付けは常にシーンファイル側(fileInfo)を正とする。
    "last_scene_project_links": {},
}

# Phase 3: file ノードのcolorSpaceに設定する値の候補。カラーマネジメント
# 設定(ACES/OCIOカスタム構成等)によって使える名称が異なるため、
# 前から順に試して最初に成功したものを採用する(要現場検証)。
RAW_COLORSPACE_CANDIDATES = ["Raw", "Utility - Raw", "Non-Color Data", "Utility - Linear - sRGB"]
SRGB_COLORSPACE_CANDIDATES = ["sRGB", "Utility - sRGB - Texture", "sRGB Texture", "scene-linear Rec.709-sRGB"]

RENDERER_CHOICES = ["arnold", "vray", "redshift", "none"]

CHANNEL_SUFFIXES = ["BaseColor", "Roughness", "Metallic", "Normal", "Height", "Emissive"]

PLACE2D_ATTR_PAIRS = [
    ("coverage", "coverage"), ("translateFrame", "translateFrame"),
    ("rotateFrame", "rotateFrame"), ("mirrorU", "mirrorU"), ("mirrorV", "mirrorV"),
    ("stagger", "stagger"), ("wrapU", "wrapU"), ("wrapV", "wrapV"),
    ("repeatUV", "repeatUV"), ("offset", "offset"), ("rotateUV", "rotateUV"),
    ("noiseUV", "noiseUV"), ("vertexUvOne", "vertexUvOne"),
    ("vertexUvTwo", "vertexUvTwo"), ("vertexUvThree", "vertexUvThree"),
    ("vertexCameraOne", "vertexCameraOne"),
]


def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


# ---------------------------------------------------------------------------
# Phase 5: 同期履歴ログ(SP側と共通のファイルに追記する)
# ---------------------------------------------------------------------------

HISTORY_LOG_PATH = os.path.join(CONFIG_DIR, "livesync_history.log")
_HISTORY_MAX_BYTES = 512 * 1024  # 500KB。超えたら直近1000行のみ残す。


def _append_history(source, text):
    """SP/Maya共通の同期履歴ログに1行追記する。無駄に複雑な仕組みに
    せず、通常は追記のみ(安価)、肥大化した場合だけ間引く(高価だが
    稀にしか発生しない)。失敗しても同期処理自体は継続できるよう、
    例外は握りつぶす。
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        line = "[{0}] [{1}] {2}\n".format(_now(), source, text)
        if os.path.isfile(HISTORY_LOG_PATH) and os.path.getsize(HISTORY_LOG_PATH) > _HISTORY_MAX_BYTES:
            with open(HISTORY_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            with open(HISTORY_LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines[-1000:])
        with open(HISTORY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 5: 複数Mayaセッションの検知(非ブロッキングな通知のみ)
# ---------------------------------------------------------------------------

SESSION_LOCK_PATH = os.path.join(CONFIG_DIR, "maya_session.lock")


#  2026.07.15-01: 以前はロックファイルの削除が「監視ボタンを手動でOFFに
#  した時」にしか行われず、Mayaを閉じる/クラッシュする/シーンを切り替える
#  (次項の自動監視停止を参照)といったケースでロックが残り続け、次回以降の
#  セッション開始のたびに「他のセッションが監視中」という警告が実際には
#  誰も監視していないのに毎回出続ける不具合があった。
#  対策として (1) Maya終了時(kMayaExiting)にも解除を試みる、
#  (2) 起動時に前回分の生存確認を行う、(3) 生存確認できない場合は経過時間
#  で古いロックとみなす、(4) それでも誤検知した場合にユーザーがUIから
#  手動で無視/削除できるようにする、の4段構えとした。

# 生存確認が(何らかの理由で)行えない場合に「古いロック」とみなすまでの
# 経過時間。通常Mayaのセッションがこれより長時間放置されたまま
# 監視だけ有効、というのは考えにくいため、十分安全側の値としている。
_STALE_LOCK_SECONDS = 12 * 60 * 60  # 12時間


def _write_session_lock():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SESSION_LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "started_at": _now(),
                # 経過時間の判定にはエポック秒が必要(_now()は時刻のみで
                # 日付情報を持たないため、日をまたぐと比較できない)。
                "started_at_epoch": time.time(),
            }, f)
    except Exception:
        pass


def _clear_session_lock_if_own():
    try:
        if os.path.isfile(SESSION_LOCK_PATH):
            with open(SESSION_LOCK_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("pid") == os.getpid():
                os.remove(SESSION_LOCK_PATH)
    except Exception:
        pass


def _force_clear_session_lock():
    """所有プロセスに関わらずロックファイルを削除する。UIの
    「このロックを無視して削除」ボタンから呼ばれる、ユーザーの
    明示的な操作専用。誤って自分の有効なロックまで消してしまう
    ケースはUI側で「他プロセスの警告が出ている時だけボタンを表示する」
    ことで避けている。
    """
    try:
        if os.path.isfile(SESSION_LOCK_PATH):
            os.remove(SESSION_LOCK_PATH)
        return True
    except Exception:
        return False


def _pid_is_alive(pid):
    """Windows環境でpidが実在するプロセスかどうかを軽量に確認する。
    tasklistコマンドの出力にpidが含まれるかで判定する(psutil等の
    追加依存を避けるため)。判定できない場合はNoneを返し、呼び出し側で
    経過時間ベースのフォールバックに任せる。
    """
    if os.name != "nt":
        # Mac/Linuxでの利用は現状想定していないが、念のためpsコマンドで
        # 簡易確認する(無ければ例外でNoneにフォールバック)。
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid)], capture_output=True, text=True, timeout=3)
            return str(pid) in result.stdout
        except Exception:
            return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "PID eq {0}".format(pid), "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return str(pid) in result.stdout
    except Exception:
        return None


def _check_other_session():
    """他のMayaプロセスが既に監視中とみられる場合、その情報を返す。

    2026.07.15-01: 以前はpidが自分と違うというだけで無条件に警告して
    いたが、Mayaの異常終了等でロックが残り続けるケースが多く、実際には
    誰も監視していないのに毎回警告が出る問題があった。
    そのため (1) tasklistでpidの生存確認を試み、生きていなければ
    残骸と判断してロックを削除しNoneを返す、 (2) 生存確認自体が
    失敗した場合は started_at_epoch からの経過時間で古いロックとみなす、
    という2段階のフィルタを追加した。それでも誤検知した場合のために
    戻り値には "stale_guess"(生存確認できず経過時間でも判定できない、
    最終的にユーザー判断に委ねるべきケース)を含めている。
    """
    try:
        if not os.path.isfile(SESSION_LOCK_PATH):
            return None
        with open(SESSION_LOCK_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("pid") == os.getpid():
            return None

        alive = _pid_is_alive(data.get("pid"))
        if alive is False:
            # 生存確認が取れて、かつ実在しない -> 確実に残骸。
            # 次回以降も同じ警告を出し続けないよう、この場で削除する。
            _force_clear_session_lock()
            return None

        started_epoch = data.get("started_at_epoch")
        if alive is None and started_epoch is not None:
            age = time.time() - started_epoch
            if age > _STALE_LOCK_SECONDS:
                # 生存確認はできなかったが、十分に古いので残骸とみなす。
                _force_clear_session_lock()
                return None

        data["stale_guess"] = (alive is None)
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Phase 5: userSetup.py への自動登録(バックアップ付き)
# ---------------------------------------------------------------------------

REGISTER_MARKER = "# === SP_LIVE_SYNC_AUTO_REGISTER ==="


def _user_setup_path():
    return os.path.join(cmds.internalVar(userScriptDir=True), "userSetup.py")


def register_user_setup(auto_open=False):
    """userSetup.pyに maya_live_sync の import を追記する。

    2026.07.16(緊急修正): 以前は「マーカーコメントが既に存在する場合は
    無条件にスキップする」設計だった。これにより、過去(kMayaInitialized
    による遅延起動対策が入る前の旧バージョン等)に一度でも登録した
    ユーザーは、install.py を何度再実行しても userSetup.py の中身が
    永久に更新されず、以下の症状が実機で確認された:
      - Maya起動中に、メインウィンドウの構築が終わる前にLiveSyncの
        ウィンドウだけが先行して表示されてしまう(旧バージョンには
        lowestPriorityでの遅延処理が無かったため)
      - install.py を再実行してシェルフボタンを押し直しても、
        起動時に旧userSetup.pyのロジックで一度importされた古い
        モジュールキャッシュの影響で、古いバージョンの挙動のまま
        に見えることがある
    対策として、マーカーが既に存在する場合は「スキップ」ではなく
    「一致するブロックを取り除いてから、新しい内容で書き直す」方式に
    変更した(uninstall.py の _remove_autostart_block() と同じ抽出
    ロジックを使う)。これにより、install.py を再実行するたびに
    userSetup.py 内の自動起動ロジックも常に最新の内容へ更新される。

    既存ファイルがある場合は、書き換え前にタイムスタンプ付きで
    バックアップしてから書き換える(ユーザーの既存userSetup.py内容を
    壊さないため)。

    auto_open=True の場合、ウィンドウを開くタイミングには
    OpenMaya.MSceneMessage.kMayaInitialized コールバックを使う。
    以前は maya.utils.executeDeferred を userSetup.py 実行時点で
    直接呼んでいたが、これは「Mayaのアイドルキューが一度空いた瞬間」
    に実行されるだけで、UI全体の起動が完了したことまでは保証しない。
    実機で、起動ロゴ表示中〜メインウィンドウ構築中にLive Syncの
    ウィンドウが表示されてしまう不具合として確認された。
    kMayaInitialized はMaya本体の初期化が完了した後に一度だけ通知される
    イベントで、そのコールバック内でさらに cmds.evalDeferred(...,
    lowestPriority=True) を挟むことで、「メインウィンドウ・既定
    プロジェクトの表示まで完了した後」まで確実に遅延させる
    (maya.utils.executeDeferred には lowestPriority 相当の引数が無いため、
    こちらは cmds.evalDeferred 側のフラグを利用する)。

    戻り値: (成功したか, メッセージ)
    """
    path = _user_setup_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = ""
        was_updated = False
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
            # バックアップ用に、今回の書き換えで一切手を加えていない
            # 「削除前の完全な内容」を別途保持しておく(existing自体は
            # この後、登録ブロック削除後の値に置き換わるため)。
            original_content = existing

            existing_block = None
            if REGISTER_MARKER in existing:
                # 既存の登録ブロックを取り除く(uninstall.pyの
                # _remove_autostart_block()と同じ抽出方式)。
                start = existing.index(REGISTER_MARKER)
                block_start = start  # マーカー自体の開始位置(前の空行は含まない)
                if start > 0 and existing[start - 1] == "\n":
                    start -= 1
                end_marker = "except Exception:\n    pass\n"
                end_idx = existing.find(end_marker, start)
                if end_idx == -1:
                    # ブロックの終端が見つからない(手動編集された等)場合は
                    # 安全のため書き換えを諦め、従来通りスキップする。
                    return False, "既存の登録ブロックの終端が見つからないため、安全のため更新をスキップしました: {0}".format(path)
                end = end_idx + len(end_marker)
                # 2026.07.16-05: 既存ブロックの中身(マーカー〜末尾)を
                # 後で新しい生成内容と比較するために保持しておく。
                existing_block = existing[block_start:end]
                existing = existing[:start] + existing[end:]
                was_updated = True

            backup_path = "{0}.bak_{1}".format(path, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
            with open(backup_path, "w", encoding="utf-8") as f:
                # バックアップには必ず「削除前の完全な内容」を残す
                # (2026.07.16: 以前はここで既にブロック削除後の existing を
                # 書いてしまっており、バックアップとして不完全だった見落とし
                # を修正)。
                f.write(original_content)

        lines = ["\n", REGISTER_MARKER + "\n", "try:\n", "    import maya_live_sync\n"]
        if auto_open:
            lines += [
                "    import maya.cmds as _sp_live_sync_cmds\n",
                "    import maya.OpenMaya as _sp_live_sync_om\n",
                "\n",
                "    def _sp_live_sync_on_initialized(*_args):\n",
                "        # Maya本体の起動が完了した後、さらにアイドルキューの\n",
                "        # 最後尾(lowestPriority)に回すことで、メインウィンドウや\n",
                "        # 既定プロジェクトの表示まで確実に終えてから開く。\n",
                "        # (maya.utils.executeDeferred には lowestPriority 引数が\n",
                "        # 無いため、cmds.evalDeferred 側のフラグを使う)\n",
                "        _sp_live_sync_cmds.evalDeferred(\n",
                "            maya_live_sync.show_ui, lowestPriority=True)\n",
                "\n",
                "    _sp_live_sync_om.MSceneMessage.addCallback(\n",
                "        _sp_live_sync_om.MSceneMessage.kMayaInitialized,\n",
                "        _sp_live_sync_on_initialized)\n",
            ]
        lines += ["except Exception:\n", "    pass\n"]
        # existing_block はマーカー行から始まる(先頭の空行を含まない)ため、
        # 新しく生成する側も同じ基準(マーカー行始まり)で比較できるよう、
        # lines[0](先頭の"\n")を除いた形に揃える。
        new_block_for_compare = "".join(lines[1:])

        # 2026.07.16-05(緊急修正): userSetup.pyを書き換えるたびに、
        # Maya 2026のセキュリティ機能(Secure UserSetup Checksum
        # verification)が反応し、内容変更の確認ポップアップが毎回
        # 表示されるようになった。このポップアップに正しく応答しないまま
        # 作業を始めると、Undo初期化等の起動シーケンスが乱れる不具合が
        # 過去に確認されている。前回登録した内容と今回生成する内容が
        # 完全に一致する(＝実質的な変更が無い)場合は、ファイルへの
        # 書き込み自体を行わないことで、無用なチェックサム再検証の
        # 発生を防ぐ。
        if was_updated and existing_block is not None and existing_block == new_block_for_compare:
            return True, "userSetup.pyは既に最新の内容のため、書き換えをスキップしました(不要な確認ダイアログの発生を防ぐため): {0}".format(path)

        with open(path, "w" if was_updated else "a", encoding="utf-8") as f:
            if was_updated:
                f.write(existing)
            f.writelines(lines)

        if was_updated:
            return True, "userSetup.pyの登録内容を最新化しました: {0}".format(path)
        return True, "userSetup.pyに登録しました: {0}".format(path)
    except Exception as e:
        return False, "登録に失敗しました: {0}".format(e)



def _migrate_legacy_flat_maps(cfg):
    """2026.07.15-01より前の設定ファイルは texture_set_export_prefix /
    texture_set_shading_engine_map がプロジェクトキーで分離されて
    いない、テクスチャセット名だけをキーにしたフラットな辞書だった。

    このままだと別プロジェクトの同名テクスチャセット同士が値を
    上書きし合ってしまう(例: BoxプロジェクトとCubeプロジェクトの両方に
    "Body" という名前のテクスチャセットがある場合)。

    移行方針: 旧フラット辞書に記録されていた内容は「どのプロジェクトの
    ものか分からない」ため、安全側に倒して active_project_key (無ければ
    known_texture_sets_by_project 内で該当テクスチャセット名を含む
    最初のプロジェクト)に割り当てる。判別できないキーは移行せずに
    捨てる(誤って別プロジェクトに紐付くよりは、生成し直してもらう方が
    安全なため)。

    この関数は破壊的に cfg を書き換えず、移行後のcfgを新たに返す。
    移行が発生した場合は cfg["_migrated_legacy_maps"] = True を立てる
    (呼び出し側が保存要否を判断できるようにするための内部マーカー)。
    """
    legacy_prefix = cfg.get("texture_set_export_prefix")
    legacy_sg = cfg.get("texture_set_shading_engine_map")
    has_legacy = bool(legacy_prefix) or bool(legacy_sg)
    if not has_legacy:
        return cfg

    by_project = cfg.get("known_texture_sets_by_project", {})

    def _guess_project_key(name):
        active_key = cfg.get("active_project_key")
        if active_key and name in by_project.get(active_key, []):
            return active_key
        for key, names in by_project.items():
            if name in names:
                return key
        # プロジェクトが特定できない場合は active_project_key があれば
        # そこへ、無ければ移行を諦める(None を返す)。
        return active_key

    prefix_by_project = dict(cfg.get("texture_set_export_prefix_by_project", {}))
    if legacy_prefix:
        for name, prefix in legacy_prefix.items():
            key = _guess_project_key(name)
            if not key:
                continue
            prefix_by_project.setdefault(key, {})
            prefix_by_project[key][name] = prefix
        cfg["texture_set_export_prefix_by_project"] = prefix_by_project

    sg_by_project = dict(cfg.get("texture_set_shading_engine_map_by_project", {}))
    if legacy_sg:
        for name, sg in legacy_sg.items():
            key = _guess_project_key(name)
            if not key:
                continue
            sg_by_project.setdefault(key, {})
            sg_by_project[key][name] = sg
        cfg["texture_set_shading_engine_map_by_project"] = sg_by_project

    # 旧キーは移行後に空へリセットしておく(次回以降は新形式のみを
    # 正として扱うため。SP側/古いMaya側が万一まだ旧キーへ書き込んで
    # いても、次のload_config()で改めて拾われる)。
    cfg["texture_set_export_prefix"] = {}
    cfg["texture_set_shading_engine_map"] = {}
    cfg["_migrated_legacy_maps"] = True
    return cfg


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(loaded)
        cfg = _migrate_legacy_flat_maps(cfg)
        if cfg.pop("_migrated_legacy_maps", False):
            # 移行結果をディスクへ書き戻す(次回以降は移行処理をスキップ
            # できるようにするため)。保存に失敗しても致命的ではないので
            # 例外は握りつぶす。
            try:
                save_config({
                    "texture_set_export_prefix_by_project": cfg["texture_set_export_prefix_by_project"],
                    "texture_set_shading_engine_map_by_project": cfg["texture_set_shading_engine_map_by_project"],
                    "texture_set_export_prefix": {},
                    "texture_set_shading_engine_map": {},
                })
            except Exception:
                pass
        return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    """設定を保存する(監視の再起動は行わない、副作用の無いバージョン)。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    merged = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            merged = json.load(f)
    except Exception:
        pass
    merged.update(cfg)
    # Phase 3 最適化: 一時ファイルに書いてから os.replace() で原子的に
    # 置き換える。SPとMayaが同時に保存/読込を行った際に、書き込み途中の
    # 不完全なJSONを相手側が読んでしまう(torn read)のを防ぐため。
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CONFIG_PATH)


def _all_known_texture_sets(cfg):
    """known_texture_sets_by_project の全プロジェクト分をまとめて返す
    (active_project_key が無い/古いSP側を使っている場合のフォールバック)。
    """
    by_project = cfg.get("known_texture_sets_by_project", {})
    names = set()
    for project_names in by_project.values():
        names.update(project_names)
    return sorted(names)


def _active_texture_sets(cfg):
    """Phase 3: SP側が書き込む active_project_key を優先して、現在
    アクティブなプロジェクトのテクスチャセット一覧だけを返す。
    active_project_key が無い(未対応の古い設定ファイル)、または該当
    プロジェクトの記録が無い場合は、全プロジェクト分をまとめて返す
    従来の簡易動作にフォールバックする。
    """
    by_project = cfg.get("known_texture_sets_by_project", {})
    active_key = cfg.get("active_project_key")
    if active_key and active_key in by_project:
        return sorted(by_project[active_key])
    return _all_known_texture_sets(cfg)


# ---------------------------------------------------------------------------
# 2026.07.15-01: Mayaシーン ⇔ SPプロジェクトの紐付け
# ---------------------------------------------------------------------------
#
# 背景: これまでMaya側は「SP側が今どのプロジェクトを開いているか
# (active_project_key)」だけを見ており、「今Mayaで開いているシーンが
# そもそもどのSPプロジェクトと対応するべきか」という期待値を一切
# 持っていなかった。そのため、Box.maを開いたままSP側でCube.sppに
# 切り替えても、Maya側はそれに気づかずBoxの監視設定のままCubeの
# テクスチャを反映しようとする、といった事故が起こり得た。
#
# 対策として、Mayaシーンファイル自体(cmds.fileInfo、シーン保存時に
# 一緒に保存される永続的なキー値ストア)に、対応するSPプロジェクトキー
# (_current_project_key() 相当、SP側の .spp フルパス)を記録する。
# シーンファイル側に持たせることで、別PCで同じシーンを開いても
# 対応関係が失われない(共有設定ファイルだけに持たせると、PCを
# 変えた時点で情報が失われてしまうため)。
#
# 未保存シーンはfileInfoを永続化できないため、その場合のみ
# DEFAULT_CONFIG["last_scene_project_links"](共有設定ファイル側、
# セッション内フォールバック)を使う。この場合は「未保存の間だけ」
# 有効で、保存すれば自動的にfileInfo側へ引き継がれる。

_SCENE_LINK_FILEINFO_KEY = "sp_live_sync_project_key"


def _normalize_project_key_for_compare(key):
    """
    2026.07.16(緊急修正): SPプロジェクトキーの比較・保存で使う正規化。

    背景: sp_project_key(SPの.sppフルパス)はWindows環境では
    バックスラッシュ区切り("C:\\Work\\aaaaa.spp")で渡ってくる。
    これをそのまま cmds.fileInfo() に保存すると、Mayaが内部的に
    行うエスケープと、読み取り側で「エスケープを元に戻すため」に
    行っていた value.encode("utf-8").decode("unicode_escape") の
    組み合わせにより、"\\B" のような並びが制御文字(ベル文字 \\x07 等)
    に化けてパスが破壊される不具合があった。結果として、実際に
    保存された値は正しいのに、読み取り時だけ壊れた文字列になり、
    「同じプロジェクトのはずなのに紐付け先とSP側の表示が一致しない」
    という矛盾したエラー表示が起きていた(状態バーがSP未起動でない
    のに不一致警告を出し続ける症状として確認された)。

    対策として、fileInfoへの保存・比較の両方でスラッシュ区切りに
    正規化した文字列を使うようにし、危険な unicode_escape 変換自体を
    撤去した。SP側から来る active_project_key(正規化していない生の
    パス)と比較する際も、必ずこの関数を通してから比較する。
    """
    if not key:
        return key
    return key.replace("\\", "/")


def _get_current_scene_project_link():
    """現在Mayaで開いているシーンに紐付けられたSPプロジェクトキーを
    返す。紐付けが無ければNone。戻り値は常にスラッシュ区切りに
    正規化済み(_normalize_project_key_for_compare参照)。
    """
    try:
        scene_path = cmds.file(query=True, sceneName=True)
    except Exception:
        scene_path = ""

    if scene_path:
        try:
            info = cmds.fileInfo(_SCENE_LINK_FILEINFO_KEY, query=True)
        except Exception:
            info = None
        if info:
            # 2026.07.16: 以前ここで行っていた
            # value.encode("utf-8").decode("unicode_escape") は、
            # Windowsパスのバックスラッシュ区切りを破壊する不具合の
            # 原因だったため撤去した。保存側(_set_current_scene_
            # project_link)が既にスラッシュ区切りで保存するため、
            # 読み取り側で特別なデコードは不要。
            value = info[0]
            return _normalize_project_key_for_compare(value) or None
        return None

    # 未保存シーン: セッション内フォールバックを見る。シーンを一意に
    # 識別できないため、"__unsaved__" 固定のキーで代用する(SP側の
    # _current_project_key() が未保存プロジェクトを区別できないのと
    # 同じ既知の制限)。
    cfg = load_config()
    links = cfg.get("last_scene_project_links", {})
    return _normalize_project_key_for_compare(links.get("__unsaved__"))


def _set_current_scene_project_link(sp_project_key):
    """現在Mayaで開いているシーンに、対応するSPプロジェクトキーを
    紐付けて保存する。保存済みシーンならfileInfo(次回保存時に
    シーンファイルへ永続化される)、未保存シーンなら共有設定ファイルの
    last_scene_project_links に一時的に記録する。

    2026.07.16: 保存前にスラッシュ区切りへ正規化してから書き込む
    (詳細は _normalize_project_key_for_compare のコメント参照)。
    """
    normalized_key = _normalize_project_key_for_compare(sp_project_key)

    try:
        scene_path = cmds.file(query=True, sceneName=True)
    except Exception:
        scene_path = ""

    if scene_path:
        try:
            cmds.fileInfo(_SCENE_LINK_FILEINFO_KEY, normalized_key or "")
            return True
        except Exception:
            return False

    cfg = load_config()
    links = dict(cfg.get("last_scene_project_links", {}))
    links["__unsaved__"] = normalized_key
    save_config({"last_scene_project_links": links})
    return True


def _scene_display_name():
    """状態バー表示用の、今開いているシーンの短い名前を返す。"""
    try:
        scene_path = cmds.file(query=True, sceneName=True)
    except Exception:
        scene_path = ""
    if not scene_path:
        return "(未保存のシーン)"
    return os.path.basename(scene_path)


def _project_display_name(project_key):
    """状態バー表示用の、SPプロジェクトキーの短い名前を返す。"""
    if not project_key:
        return None
    if project_key == "__unsaved__":
        return "(未保存のSPプロジェクト)"
    return os.path.basename(project_key)


def _export_prefix(cfg, texture_set_name, project_key=None):
    """テクスチャセット名から、実際のエクスポートファイル名のprefixを
    求める。SP側が実際に書き出した結果から記録したprefix
    (texture_set_export_prefix_by_project)が分かっていればそれを最優先で
    使い、まだ一度もエクスポートされていない場合のみ _safe_name() による
    予測値にフォールバックする(スペースや日本語を含む名前でのズレを
    防ぐため)。

    2026.07.15-01: プロジェクトキーごとに分離した構造に変更した。
    project_key を省略した場合は cfg["active_project_key"] を使う。
    別プロジェクトに同名のテクスチャセットが存在しても、prefixを
    取り違えないようにするための変更。
    """
    key = project_key if project_key is not None else cfg.get("active_project_key")
    by_project = cfg.get("texture_set_export_prefix_by_project", {})
    prefix_map = by_project.get(key, {}) if key else {}
    return prefix_map.get(texture_set_name) or _safe_name(texture_set_name)


# ---------------------------------------------------------------------------
# 監視エンジン本体
# ---------------------------------------------------------------------------

class LiveSyncWatcher(QtCore.QObject):

    status_changed = QtCore.Signal(str)
    stats_changed = QtCore.Signal(dict)
    # 2026.07.15-01: 状態バー用に新設したシグナル群。
    # other_session_changed: dict(他セッション情報) または None
    other_session_changed = QtCore.Signal(object)
    # scene_link_changed: 現在のシーン⇔SPプロジェクトの紐付け状態が
    # 変わった(シーン切替・紐付け設定・SP側プロジェクト切替を含む)
    scene_link_changed = QtCore.Signal()
    # structure_changed: known_texture_sets_by_project 等、一覧表示に
    # 関わる動的設定がディスク上で更新された(3秒ポーリングで検知)
    structure_changed = QtCore.Signal()

    def __init__(self, parent=None):
        super(LiveSyncWatcher, self).__init__(parent)
        self.config = load_config()
        self.enabled = False
        self.other_session_info = None
        # 2026.07.15-01: シーン切り替えを検知して監視を自動停止した場合に
        # 立てるフラグ。ユーザーが手動でOFFにした場合と区別することで、
        # UI側で「シーンが切り替わったため自動停止しました」という
        # 案内を出し分けられるようにする。
        self._auto_stopped_by_scene_change = False
        self._last_known_texture_sets_snapshot = None
        self._last_flag_mtime_watch = 0.0
        self._last_flag_mtime_final = 0.0
        # 2026.07.15-06: _last_flag_mtime_watch/_final は元々「どの
        # フォルダを最後に見たか」を区別しない単一のグローバル比較値
        # だったため、SP側のプロジェクト切り替えでフォルダ自体が変わる
        # と、フラグの新旧判定を誤ることがあった。フォルダが変わったこと
        # 自体を検知してmtime比較値をリセットできるよう、直近チェックした
        # フォルダパスを記録しておく(詳細は _process_pending_changes()
        # 参照)。
        self._last_watch_dir_for_flag = None
        self._last_final_dir_for_flag = None
        # 2026.07.15-04: reload_textures()/reload_final_textures() の
        # 古いサブフォルダ補正を、無関係な他プロジェクトのサブフォルダ
        # まで巻き込まないよう「直前に自分が監視していたサブフォルダ」
        # だけに限定するための記録として導入した。
        #
        # 2026.07.15-05で、aiSS経由で生成されたノードを救済できない
        # 問題への対処として、補正対象は watch_root/final_export_dir
        # 直下の全サブフォルダ + _matches_known_texture_set_prefix() に
        # よるファイル名安全確認、という方式に置き換えたため、この2つの
        # 変数は現在どこからも参照されない(書き込まれるだけの)デッド
        # コードになっている。
        # 2026.07.16: シミュレーションで、プロジェクトをA->B->Aと複数回
        # 切り替えた場合にこの値が正しい「直前のフォルダ」を追跡できなく
        # なる(fs_watcher.directories()に複数の過去フォルダが蓄積される
        # ため)不具合も見つかったが、実害は無い(参照箇所が無いため)。
        # 実害はないため残しているが、削除しても動作に影響しない。
        self._last_active_watch_dir = None
        self._last_active_final_dir = None
        # Phase 6: 現在file ノードがプレビュー(監視フォルダ)と高画質版
        # (Finalフォルダ)のどちらを参照しているか。自動切り替えは行わず、
        # switch_texture_quality() の明示的な呼び出しでのみ変化する。
        self.using_final_quality = False

        self.stats = {
            "reload_count": 0,
            "last_reload_at": None,
            "last_node_count": 0,
        }

        self.fs_watcher = QtCore.QFileSystemWatcher(self)
        self.fs_watcher.directoryChanged.connect(self._on_dir_changed)

        self.debounce_timer = QtCore.QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(400)
        self.debounce_timer.timeout.connect(self._process_pending_changes)

        # 複数プロジェクト対応: QFileSystemWatcherは「既に監視登録した
        # フォルダの変化」しか通知しない。SP側でプロジェクトを切り替えて
        # 新しい(まだ一度もWatcherに登録していない)Finalサブフォルダが
        # 誕生した場合、directoryChangedは一切発火しないため、
        # _process_pending_changes への動的追従だけでは間に合わない
        # (鶏と卵の問題)。そのため、フォルダ変化とは独立に、数秒おきで
        # 「アクティブなFinalサブフォルダがWatcher登録済みか」だけを
        # 軽量に確認するポーリングタイマーを別途持つ。
        #
        # 2026.07.17 根治修正(B-1): 以前は watcher.start()/stop() から
        # このタイマーも連動してstart/stopしていた(2026.07.15-02の
        # 意図)。これにより、監視ボタンがOFFの間は
        # _refresh_dynamic_config() が一切定期実行されず、SP側が
        # active_project_key 等を共有設定ファイルへ書き込んでも
        # Maya側のメモリに反映されないまま放置される不具合があった。
        # 実機ログ(DIAG-B1計装)で、監視OFF中に呼び出し間隔が148秒に
        # 達することを確認して確定した。
        # 対策として、project_poll_timer は監視ON/OFFに関わらず
        # ウィンドウ生成時(このインスタンス生成時)に常時起動する
        # ことにした。_ensure_active_dirs_watched() 内部の
        # self.enabled チェック(シーン紐付け不一致時の自動停止判定等)
        # は enabled=False の間はスキップされる安全設計のままなので、
        # 監視OFF中に常時回しても副作用は「設定値の読み直しと
        # scene_link_changed/structure_changedシグナルの発火」のみに
        # 限定される。
        # 負荷面: load_config()はローカルJSONの同期読み込みのみで、
        # 3秒間隔・ウィンドウを開いている間常時実行しても実測上軽微と
        # 判断した(簡易実装を優先し、監視ON/OFFでの間隔可変は見送った)。
        self.project_poll_timer = QtCore.QTimer(self)
        self.project_poll_timer.setInterval(3000)
        self.project_poll_timer.timeout.connect(self._ensure_active_dirs_watched)
        self.project_poll_timer.start()


    def _emit_status(self, text):
        line = "[{0}] {1}".format(_now(), text)
        print("[LiveSync] {0}".format(text))
        self.status_changed.emit(line)
        _append_history("Maya", text)

    # -- 設定 -----------------------------------------------------------

    def reload_config(self):
        self.config = load_config()
        self._emit_status("設定を再読込しました。")

    def _refresh_dynamic_config(self):
        """SP側が随時書き込む「動的な値」だけをディスクから読み直す。

        経緯: reload_config() は毎回ログを出す想定のユーザー操作向け
        メソッドで、これまでどこからも定期的に呼ばれていなかった。
        そのため self.config は LiveSyncWatcher 初期化時の1回きり
        (load_config())で固定され、SP側が _do_export() のたびに
        書き込む active_watch_subfolder / active_final_subfolder /
        active_project_key / texture_set_export_prefix が、Mayaの
        メモリ上には一切反映されないという不具合があった。
        実機では、共有設定ファイル(ディスク)には正しい値
        (active_watch_subfolder="TEST_d397fd" 等)が書き込まれている
        のに、_active_watch_dir() が古い(Noneのままの)値を返し、
        switch_texture_quality() がプレビューの切り替え先を
        サブフォルダではなく watch_dir 直下のままにしてしまう
        (共有PCでの所有権問題の再発)という形で顕在化した。
        3秒間隔の project_poll_timer から呼ばれる想定のため、
        reload_config() と違いログは出さない(頻度が高くログが
        埋もれてしまうため)。

        2026.07.15-01追加:
          - known_texture_sets_by_project / texture_set_export_prefix_by_project /
            texture_set_shading_engine_map_by_project も対象に追加した。
            以前はこれらがdynamic_keysに含まれておらず、SP側で新しい
            テクスチャセットが検出されてもMaya側UIが手動更新するまで
            反映されなかった(マテリアル構造タブが古いまま、という
            体感の分かりにくさにつながっていた)。
          - active_project_key の変化を検知したら scene_link_changed
            シグナルを発火し、状態バーの「シーン⇔SPプロジェクト」表示を
            即座に更新できるようにした。
          - known_texture_sets_by_project の中身が変化したら
            structure_changed シグナルを発火し、UIの一覧テーブルと
            最終更新時刻を自動的に更新できるようにした。
        """
        try:
            latest = load_config()
        except Exception:
            return

        prev_active_project_key = self.config.get("active_project_key")
        prev_texture_sets = self.config.get("known_texture_sets_by_project")

        dynamic_keys = (
            "active_watch_subfolder",
            "active_final_subfolder",
            "active_project_key",
            "texture_set_export_prefix_by_project",
            "texture_set_shading_engine_map_by_project",
            "known_texture_sets_by_project",
        )
        for key in dynamic_keys:
            if key in latest:
                self.config[key] = latest[key]

        if self.config.get("active_project_key") != prev_active_project_key:
            self.scene_link_changed.emit()

        if self.config.get("known_texture_sets_by_project") != prev_texture_sets:
            self.structure_changed.emit()

        # 他セッションの状態も定期的に再確認する(残骸ロックが後から
        # 実プロセス終了で解消されたケースなどをUIに反映するため)。
        current_other = _check_other_session()
        prev_other_pid = (self.other_session_info or {}).get("pid") if self.other_session_info else None
        current_other_pid = (current_other or {}).get("pid") if current_other else None
        if current_other_pid != prev_other_pid:
            self.other_session_info = current_other
            self.other_session_changed.emit(current_other)

    def apply_and_save_config(self, new_values):
        """ユーザー操作(監視フォルダ変更等)による保存。監視の再起動を伴う。"""
        self.config.update(new_values)
        save_config(new_values)
        self._emit_status("設定を保存しました。")
        if self.enabled:
            self.stop()
            self.start()

    def save_mapping_only(self, new_values):
        """マッピング情報等、監視の再起動が不要な軽微な保存。
        Phase 2 最適化: is_texture_set_mapped() のような「確認のついでに
        記録する」処理から監視の停止/再開を誘発しないよう分離した。
        """
        self.config.update(new_values)
        save_config(new_values)

    def save_shading_engine_mapping(self, texture_set_name, shading_engine_name, project_key=None):
        """2026.07.15-01: テクスチャセット名 -> シェーディングエンジン名の
        対応を、現在アクティブなSPプロジェクトにスコープして保存する。
        別プロジェクトの同名テクスチャセットを誤って上書きしないよう、
        texture_set_shading_engine_map_by_project[project_key] にのみ
        書き込む。project_key が特定できない(未保存プロジェクト等)場合は
        何もしない(誤った紐付けを残すよりは記録しない方が安全)。
        """
        key = project_key if project_key is not None else self.config.get("active_project_key")
        if not key:
            self._emit_status(
                "警告: SPプロジェクトが未特定のため、'{0}' のシェーダー割当を"
                "記録できませんでした(次回SP側のプロジェクト情報が"
                "共有されると再検出されます)。".format(texture_set_name)
            )
            return
        by_project = dict(self.config.get("texture_set_shading_engine_map_by_project", {}))
        project_map = dict(by_project.get(key, {}))
        project_map[texture_set_name] = shading_engine_name
        by_project[key] = project_map
        self.save_mapping_only({"texture_set_shading_engine_map_by_project": by_project})

    # -- 開始/停止 --------------------------------------------------------

    def _ensure_active_watch_watched(self):
        """アクティブなLive/Previewサブフォルダ(所有権問題回避のため
        2026.07.14-02で導入)がWatcherに登録済みか確認し、未登録
        (＝SP側でプロジェクトが切り替わり新しいサブフォルダが誕生した、
        または初回)であればフォルダを作成して監視に加える。
        _ensure_active_final_watched() のLive/Preview版で、同じタイマー
        から呼ばれる。
        """
        if not self.enabled:
            return
        active_watch_dir = self._active_watch_dir()
        if not active_watch_dir:
            return
        active_watch_dir = os.path.normpath(active_watch_dir)
        if active_watch_dir in self.fs_watcher.directories():
            return
        # 2026.07.15-04: サブフォルダが切り替わった瞬間の「直前の
        # アクティブサブフォルダ」を記録しておく。reload_textures() が
        # 「古いサブフォルダを参照したまま取り残されたノード」を補正する
        # 際、watch_dir 直下の全サブフォルダ(＝他の無関係なプロジェクトの
        # ものも含む)を対象にすると誤爆の危険があるため、対象は
        # この「直前に自分が監視していたサブフォルダ」だけに限定する。
        prev_dirs = [d for d in self.fs_watcher.directories()
                     if os.path.dirname(d) == os.path.normpath(self.config.get("watch_dir") or "")]
        if prev_dirs:
            self._last_active_watch_dir = prev_dirs[0]
        try:
            os.makedirs(active_watch_dir, exist_ok=True)
            ok = self.fs_watcher.addPath(active_watch_dir)
            if ok:
                self._emit_status("プロジェクトの切り替えを検知し、監視フォルダを更新しました: {0}".format(active_watch_dir))
        except Exception as e:
            self._emit_status("警告: 監視フォルダの追加に失敗しました: {0}".format(e))

    def _ensure_active_final_watched(self):
        """アクティブなFinalサブフォルダがWatcherに登録済みか確認し、
        未登録(＝SP側でプロジェクトが切り替わり新しいサブフォルダが
        誕生した、または初回)であればフォルダを作成して監視に加える。
        3秒間隔の軽量ポーリングタイマーからのみ呼ばれる。
        """
        if not self.enabled:
            return
        active_final_dir = self._active_final_dir()
        if not active_final_dir:
            return
        active_final_dir = os.path.normpath(active_final_dir)
        if active_final_dir in self.fs_watcher.directories():
            return
        # 2026.07.15-04: reload_final_textures() の古いサブフォルダ補正が
        # 対象を絞り込めるよう、直前のアクティブFinalサブフォルダを
        # 記録しておく(詳細は _ensure_active_watch_watched() 参照)。
        prev_dirs = [d for d in self.fs_watcher.directories()
                     if os.path.dirname(d) == os.path.normpath(self.config.get("final_export_dir") or "")]
        if prev_dirs:
            self._last_active_final_dir = prev_dirs[0]
        try:
            os.makedirs(active_final_dir, exist_ok=True)
            ok = self.fs_watcher.addPath(active_final_dir)
            if ok:
                self._emit_status("プロジェクトの切り替えを検知し、Finalフォルダの監視先を更新しました: {0}".format(active_final_dir))
        except Exception as e:
            self._emit_status("警告: Finalフォルダの監視追加に失敗しました: {0}".format(e))

    def _ensure_active_dirs_watched(self):
        """Live/Preview・Final両方のアクティブサブフォルダ追従をまとめて
        行う。project_poll_timer からはこちらを呼ぶ。

        呼び出しの最初に _refresh_dynamic_config() で active_watch_
        subfolder 等をディスクから読み直す。これを行わないと、
        SP側が共有設定ファイルへ書き込んだ新しいサブフォルダ名を
        Mayaのメモリ上の self.config が一切拾えず、いつまでも
        watch_dir/final_export_dir 直下のままフォルダ追跡が止まって
        しまう(実機で確認された不具合)。

        2026.07.15-01: このシーンに紐付けられたSPプロジェクトと、SP側が
        今実際に開いているプロジェクトが食い違っている場合は、監視を
        安全のため自動停止する。これは「Box.maを開いたままSP側で
        Cube.sppを開いてしまう」といった事故的な組み合わせのまま
        テクスチャが誤反映される/シェーダーが誤って「対応済み」と
        判定されるのを防ぐための、最終防御ライン。
        """
        self._refresh_dynamic_config()

        if self.enabled:
            linked_key = _get_current_scene_project_link()
            active_key = _normalize_project_key_for_compare(self.config.get("active_project_key"))
            # [DIAG-B2] 一次切り分け用: 不一致判定の材料を毎回ログに残す。
            # 「追跡できない」がここでの自動停止によるものかどうかを、
            # linked_key(シーン紐付け)とactive_key(SP側現在値)の
            # 実際の値を突き合わせて確定させる。
            print("[DIAG-B2] linked_key={0!r} active_key={1!r} match={2}".format(
                linked_key, active_key, (linked_key == active_key) if (linked_key and active_key) else "N/A"))
            if linked_key and active_key and linked_key != active_key:
                print("[DIAG-B2] 不一致検出 -> 監視を自動停止します。これがB-2仮説の再現です。")
                self.stop(reason="scene_change")
                self._emit_status(
                    "警告: このシーンの対応先SPプロジェクトと、SP側が今開いている"
                    "プロジェクトが異なるため、事故防止のため監視を自動停止しました。"
                    "SP側で正しいプロジェクトを開くか、状態バーから紐付けを"
                    "設定し直してください。"
                )
                return

        self._ensure_active_watch_watched()
        self._ensure_active_final_watched()

    def start(self):
        other = _check_other_session()
        self.other_session_info = other
        self.other_session_changed.emit(other)
        if other:
            confidence = "(生存確認できず、経過時間からの推定です)" if other.get("stale_guess") else ""
            self._emit_status(
                "警告: 他のMayaセッション(PID {0}, 開始 {1})も同時に監視している"
                "可能性があります{2}。二重に同じフォルダを監視すると反映処理が"
                "重複するだけで実害はありませんが、身に覚えが無ければ状態バーの"
                "「無視して削除」から消せます。".format(
                    other.get("pid"), other.get("started_at"), confidence
                )
            )
        watch_dir = os.path.normpath(self.config["watch_dir"])
        final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        os.makedirs(watch_dir, exist_ok=True)
        os.makedirs(final_dir, exist_ok=True)
        # Finalフォルダも監視対象に加える(表示品質をFinalにしたまま
        # SP側で保存しても自動反映されない不具合の修正。プレビュー
        # フォルダと同様、更新を検知したら再読込のトリガーにする)。
        #
        # 複数プロジェクト対応: SP側は Final を final_export_dir 直下では
        # なく <final_export_dir>/<アクティブなサブフォルダ>/ に書き出す
        # ため、直下だけでなくアクティブなサブフォルダも監視対象に含める。
        # サブフォルダ自体がまだ存在しない場合(一度もFinalを書き出して
        # いない)は作成してから監視に加える。
        # 2026.07.14-02: 所有権問題回避のため、Live/Preview側も同様に
        # <watch_dir>/<アクティブなサブフォルダ>/ へ書き出されるように
        # なったため、Final同様にアクティブサブフォルダも監視対象に含める。
        watch_targets = [watch_dir, final_dir]
        active_watch_dir = self._active_watch_dir()
        if active_watch_dir:
            active_watch_dir = os.path.normpath(active_watch_dir)
            os.makedirs(active_watch_dir, exist_ok=True)
            watch_targets.append(active_watch_dir)
        active_final_dir = self._active_final_dir()
        if active_final_dir:
            active_final_dir = os.path.normpath(active_final_dir)
            os.makedirs(active_final_dir, exist_ok=True)
            watch_targets.append(active_final_dir)
        for d in watch_targets:
            if d not in self.fs_watcher.directories():
                ok = self.fs_watcher.addPath(d)
                if not ok:
                    self._emit_status("警告: 監視フォルダの登録に失敗しました: {0}".format(d))
        self.enabled = True
        _write_session_lock()
        save_config({"watch_enabled": True})
        self.config["watch_enabled"] = True
        # 2026.07.15-02(緊急修正、経緯として残す): project_poll_timer が
        # どこからも start() されておらず、一度もタイムアウトが発火しない
        # 不具合があったため、当時は「監視ONの間だけ動けばよい」という
        # 前提で start()/stop() 経由の起動・停止を追加した。
        #
        # 2026.07.17(根治修正、B-1): 上記の前提そのものが誤りだった。
        # 監視OFFの間 _refresh_dynamic_config() が一切実行されず、
        # SP側のプロジェクト切り替えをMaya側が長時間(実機ログで148秒)
        # 検知できない不具合の直接原因になっていた。
        # project_poll_timer は __init__ で既に常時起動しているため、
        # ここでの明示的な start() 呼び出しは撤去した(常時起動している
        # タイマーに対して start() を呼んでも実害はないが、
        # 「監視ONで開始する」という誤った意図をコードに残さないため)。
        self._emit_status("監視を開始しました: {0}".format(active_watch_dir or watch_dir))


    def stop(self, reason="manual"):
        """監視を停止する。

        reason:
            "manual"       - ユーザーがOFFボタンを押した(従来通り)。
            "scene_change" - 2026.07.15-01で追加。Mayaのシーンが
                              切り替わったことを検知しての自動停止。
                              この場合はセッションロックを解除しない
                              (Mayaプロセス自体は生きており、別の
                              シーンで改めて監視を始める可能性が
                              あるため)。
        """
        for d in list(self.fs_watcher.directories()):
            self.fs_watcher.removePath(d)
        self.debounce_timer.stop()
        # 2026.07.17(根治修正、B-1): project_poll_timer.stop() は撤去した。
        # このタイマーは __init__ で常時起動する設計に変更したため、
        # 監視OFF時にも動き続ける必要がある(そうしないと、OFF中に
        # SP側でプロジェクトが切り替わった場合の追跡漏れ=B-1が再発する)。
        # fs_watcher・debounce_timer はテクスチャ再読込に直結するため
        # 従来通り監視OFF時に止める。project_poll_timer は
        # 「設定値の読み直し」だけを担う軽量タイマーであり、
        # 停止・再開の対象から意図的に外している。
        self.enabled = False
        if reason == "manual":
            _clear_session_lock_if_own()
            self._emit_status("監視を停止しました。")
        else:
            self._auto_stopped_by_scene_change = True
            self._emit_status(
                "Mayaのシーンが切り替わったため、監視を自動的に停止しました。"
                "このシーンに対応するSPプロジェクトを設定すると、再開できます。"
            )
        save_config({"watch_enabled": False})
        self.config["watch_enabled"] = False

    # -- イベントハンドラ ---------------------------------------------------

    def _on_dir_changed(self, path):
        if not self.enabled:
            return
        if path not in self.fs_watcher.directories():
            self.fs_watcher.addPath(path)
        self.debounce_timer.start()

    def _process_pending_changes(self):
        # 複数プロジェクト対応(所有権問題回避、2026.07.14-02): Live/Preview
        # も固定の watch_dir 直下ではなく、SP側のプロジェクト切り替えに
        # 応じて変わるアクティブなサブフォルダを指す。Finalと同じ理由で、
        # ここで最新の値を読み直し、まだ監視対象に入っていなければ
        # 動的に追加する。
        active_watch_dir = self._active_watch_dir()
        watch_dir = os.path.normpath(active_watch_dir) if active_watch_dir else os.path.normpath(
            self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"])
        if self.enabled and watch_dir not in self.fs_watcher.directories():
            try:
                os.makedirs(watch_dir, exist_ok=True)
                self.fs_watcher.addPath(watch_dir)
            except Exception as e:
                self._emit_status("警告: 監視フォルダの追加に失敗しました: {0}".format(e))

        # 複数プロジェクト対応: 「Finalフォルダ」は固定の final_export_dir
        # 直下ではなく、SP側のプロジェクト切り替えに応じて変わる
        # アクティブなサブフォルダを指す。ここで最新の値を読み直し、
        # まだ監視対象に入っていなければ動的に追加する
        # (start() は監視開始時の1回しか対象を確定しないため、開始後に
        # SP側でプロジェクトを切り替えて新しいサブフォルダができた場合、
        # ここで追従しないと新フォルダの変更を一切検知できない)。
        active_final_dir = self._active_final_dir()
        final_dir = os.path.normpath(active_final_dir) if active_final_dir else os.path.normpath(
            self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        if self.enabled and final_dir not in self.fs_watcher.directories():
            try:
                os.makedirs(final_dir, exist_ok=True)
                self.fs_watcher.addPath(final_dir)
            except Exception as e:
                self._emit_status("警告: Finalフォルダの監視追加に失敗しました: {0}".format(e))

        # 2026.07.15-06(緊急修正): _last_flag_mtime_watch/_final は
        # 「どのフォルダの完了フラグを最後に見たか」を区別しない、
        # 単一のグローバルなmtime比較値だった。SP側でプロジェクトが
        # 切り替わって watch_dir/final_dir(実体はサブフォルダ)が
        # 変わっても、この比較値はリセットされないため、以下の不具合が
        # あった:
        #   - 前のプロジェクトで記録したmtimeの方が新しい値だった場合、
        #     新しいプロジェクトの完了フラグの方が(ファイルとしては
        #     新しく作られていても)mtime自体は小さいことがあり、
        #     「まだ古い」と誤判定されて自動反映が一切トリガーされない
        #   - この状態は表示品質を手動で切り替える(switch_texture_
        #     quality)ことでしか解消されず、「一度直ってもすぐ手動
        #     操作頼みに戻る」という体感の不具合として現れていた
        # 対策として、監視中のフォルダ自体が変わったことを検知したら、
        # 対応するmtime比較値を0にリセットし、新しいフォルダの完了
        # フラグを確実に「新しい」と判定できるようにした。
        if watch_dir != self._last_watch_dir_for_flag:
            self._last_watch_dir_for_flag = watch_dir
            self._last_flag_mtime_watch = 0.0
        if final_dir != self._last_final_dir_for_flag:
            self._last_final_dir_for_flag = final_dir
            self._last_flag_mtime_final = 0.0

        watch_flag = os.path.join(watch_dir, "_sync_complete.flag")
        if os.path.isfile(watch_flag):
            try:
                mtime = os.path.getmtime(watch_flag)
            except OSError:
                mtime = None
            if mtime is not None and mtime > self._last_flag_mtime_watch:
                self._last_flag_mtime_watch = mtime
                self.reload_textures()

        # Finalフォルダ側の完了フラグ。表示品質がFinalの間のみ、
        # 該当ノードを強制再読込する(プレビュー表示中はFinalの
        # 更新を反映する必要が無いため何もしない)。
        final_flag = os.path.join(final_dir, "_sync_complete.flag")
        if os.path.isfile(final_flag):
            try:
                mtime = os.path.getmtime(final_flag)
            except OSError:
                mtime = None
            if mtime is not None and mtime > self._last_flag_mtime_final:
                self._last_flag_mtime_final = mtime
                if self.using_final_quality:
                    self.reload_final_textures()

    # -- 再読込処理 ---------------------------------------------------------

    def reload_textures(self):
        # 単独呼び出し(手動リロード等)でも最新のサブフォルダ名を
        # 確実に掴めるよう、念のためここでも読み直す。
        self._refresh_dynamic_config()
        # 複数プロジェクト対応(所有権問題回避、2026.07.14-02): file ノードは
        # <watch_dir>/<アクティブサブフォルダ>/ を参照しているはずなので、
        # 判定にもそちらを使う。
        active_watch_dir = self._active_watch_dir()
        watch_dir = os.path.normpath(active_watch_dir) if active_watch_dir else os.path.normpath(
            self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"])
        watch_root = os.path.normpath(self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"])

        # 2026.07.15-03(緊急修正): 以前は「fileノードが今まさに現在の
        # watch_dir を参照しているか」の完全一致でしか対象を拾えず、
        # 以下の手順で自動反映が効かなくなる不具合があった。
        #   1. シェーダー生成時点のSPプロジェクト(例: Box)向けの
        #      サブフォルダを参照するfileノードが作られる
        #   2. SP側で別プロジェクト(例: Cube)に切り替わり、
        #      active_watch_subfolder がCube用に変わる
        #   3. Box用のfileノードは古いサブフォルダを参照したままなので
        #      tex_dir(Box用) != watch_dir(Cube用) となり、以後
        #      一切自動反映されなくなる(SP側が更新しても永久にスキップ)
        # switch_texture_quality() ボタンを押すと直ることがあったのは、
        # あちらは managed_dirs(旧watch_dir/Final含む全関連フォルダ)を
        # 対象に含めた上で、パスを強制的に現在のdest_dirへ書き換える
        # 実装だったため。reload_textures() 側にも同様に
        # 「古いプロジェクト別サブフォルダを参照しているノードを検出し、
        # 現在のサブフォルダへパスを補正してから再読込する」処理を
        # 追加した。
        #
        # 2026.07.15-04(安全性の見直し): 当初は watch_dir 直下の
        # 「現在アクティブでない全サブフォルダ」を補正対象にしていたが、
        # これだと同一シーン内に(過去の作業等で)別の無関係なプロジェクト
        # 向けのfileノードが混在していた場合、そちらのパスまで誤って
        # 現在のプロジェクトのサブフォルダへ書き換えてしまう危険が
        # あったため、対象を「直前に自分が監視していたサブフォルダ」
        # 1つに限定した。
        #
        # 2026.07.15-05(再修正): しかし直前1つへの限定では、
        # sp_to_aiStandardSurface.py(aiSS)経由で生成されたfileノードの
        # ように、そもそも本ウォッチャーの監視追従(_last_active_watch_dir)
        # の対象外で作られたノードの古いパスを救済できない不具合が残って
        # いた。aiSSはシェルフボタンを押した瞬間の active_watch_subfolder
        # を一度だけフォルダ欄に自動入力する設計のため、ボタンを押した後に
        # SP側でプロジェクトを切り替えると、古いプロジェクト向けの
        # フォルダを参照するノードが生成されてしまう。
        # 対策として、対象を watch_dir 直下の「現在アクティブでない
        # 全サブフォルダ」に戻しつつ、_matches_known_texture_set_prefix()
        # で「本当に現在のプロジェクトが書き出したファイル名か」を
        # 確認してから補正するようにした。ディレクトリの一致ではなく
        # ファイル名のprefixで安全性を担保するため、無関係な他
        # プロジェクトのサブフォルダを巻き込む心配がない。
        stale_subfolder_dirs = set()
        try:
            if os.path.isdir(watch_root):
                for entry in os.listdir(watch_root):
                    candidate = os.path.normpath(os.path.join(watch_root, entry))
                    if os.path.isdir(candidate) and candidate != watch_dir:
                        stale_subfolder_dirs.add(candidate)
        except OSError:
            pass

        file_nodes = cmds.ls(type="file") or []
        if not file_nodes:
            self._emit_status("シーン内に file ノードが見つかりません。")
            return

        reloaded = []
        # v2.2 修正: stateWithoutFlush は「これから設定したいUndo有効/無効の
        # 値」を渡す引数であり、Trueが有効化・Falseが無効化にあたる。
        # 従来は開始時にTrue(有効化)、finally節でFalse(無効化)を渡しており、
        # この関数を一度でも呼ぶとUndoがオフのまま戻らなくなるバグがあった
        # (実機で "The undo queue is turned off" として再現・確認済み)。
        # 呼び出し前の実際の状態を保存しておき、finally節ではその値へ
        # 確実に復帰させる(呼び出し元が既にUndoをオフにしていた場合でも
        # 壊さないようにするため、決め打ちのTrueには戻さない)。
        prev_undo_state = cmds.undoInfo(query=True, stateWithoutFlush=True)
        cmds.undoInfo(stateWithoutFlush=True)
        corrected = []
        try:
            for node in file_nodes:
                try:
                    tex_path = cmds.getAttr(node + ".fileTextureName")
                except Exception:
                    continue
                if not tex_path:
                    continue
                tex_dir = os.path.normpath(os.path.dirname(tex_path))
                if tex_dir == watch_dir:
                    pass  # 既に最新のサブフォルダを参照している通常ケース
                elif tex_dir in stale_subfolder_dirs and self._matches_known_texture_set_prefix(
                        os.path.basename(tex_path)):
                    # 古いプロジェクト用サブフォルダを参照したまま取り残
                    # されたノード。ファイル名が現在のプロジェクトの
                    # 既知テクスチャセットのprefixと一致することを確認
                    # した上で(2026.07.15-05: 無関係な他プロジェクトを
                    # 誤補正しないための安全確認)、現在のサブフォルダへ
                    # パスを補正する。
                    new_path = os.path.join(watch_dir, os.path.basename(tex_path))
                    tex_path = new_path
                    corrected.append(node)
                else:
                    continue
                try:
                    # AEfileTextureReloadCmd (MEL) はアトリビュートエディタが
                    # 一度でも開かれてMELが遅延ソースされていないと
                    # 「プロシージャが見つかりません」になる不安定な依存先
                    # だったため使用をやめた。
                    # 代わりに fileTextureName を一旦空にしてから同じパスへ
                    # 戻すことで、値が実際に変化したとMayaに認識させ、
                    # ディスク上の画像を強制的に再読込させる(パスが同一の
                    # ままだと setAttr が実質no-opになり再読込されないため)。
                    cmds.setAttr(node + ".fileTextureName", "", type="string")
                    cmds.setAttr(node + ".fileTextureName", tex_path, type="string")
                    reloaded.append(node)
                except Exception as e:
                    self._emit_status("再読込に失敗: {0} ({1})".format(node, e))

            if corrected:
                self._emit_status(
                    "{0} 個のノードが古いプロジェクト用フォルダを参照していたため、"
                    "現在のフォルダへ参照先を補正しました。".format(len(corrected))
                )

            if reloaded:
                self._flush_renderer_caches()
                self._flush_viewport_cache()
                self.stats["reload_count"] += 1
                self.stats["last_reload_at"] = _now()
                self.stats["last_node_count"] = len(reloaded)
                self.stats_changed.emit(dict(self.stats))
                self._emit_status("{0} 個のテクスチャを再読込しました。".format(len(reloaded)))
            elif not self.using_final_quality:
                # Phase 6: 高画質表示中は file ノードが監視フォルダを
                # 参照していないのが正常な状態のため、この場合は
                # 「見つからない」という誤解を招くログを出さない。
                self._emit_status("監視フォルダを参照する file ノードが見つかりませんでした。")
        finally:
            cmds.undoInfo(stateWithoutFlush=prev_undo_state)

    def reload_final_textures(self):
        """Finalフォルダ配下のfileノードを強制再読込する
        (reload_textures()のFinal版)。表示品質をFinalにしたまま
        SP側で保存・高画質書き出しが行われても、Live⇔Finalを
        往復切り替えしなくても自動的に反映されるようにするためのもの。
        呼び出し元(_process_pending_changes)でusing_final_qualityが
        True の場合のみ呼ばれる想定。

        複数プロジェクト対応: 「Finalフォルダ」は final_export_dir 直下
        ではなく、現在アクティブなプロジェクトのサブフォルダ
        (_active_final_dir())を指す。

        2026.07.15-03(緊急修正): reload_textures() と同じ理由で、
        シェーダー生成時点の古いプロジェクト別サブフォルダを参照した
        ままのノードが自動反映から漏れる不具合があったため、同様に
        古いサブフォルダを検出してパスを補正する処理を追加した。

        2026.07.15-04(安全性の見直し): reload_textures() と同じ理由で、
        補正対象は「直前に自分が監視していたFinalサブフォルダ」1つに
        限定していた。

        2026.07.15-05(再修正): sp_to_aiStandardSurface.py(aiSS)経由で
        生成されたfileノードは本ウォッチャーの監視追従の対象外で
        作られるため、直前1つへの限定では救済できない不具合が残って
        いた。reload_textures() と同様、対象を final_export_dir 直下の
        「現在アクティブでない全サブフォルダ」に戻しつつ、
        _matches_known_texture_set_prefix() でファイル名の安全確認を
        行うことで、無関係な他プロジェクトを巻き込まずに救済範囲を
        広げた(詳細は reload_textures() のコメント参照)。
        """
        final_dir = self._active_final_dir()
        final_dir = os.path.normpath(final_dir) if final_dir else os.path.normpath(
            self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        final_root = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])

        stale_subfolder_dirs = set()
        try:
            if os.path.isdir(final_root):
                for entry in os.listdir(final_root):
                    candidate = os.path.normpath(os.path.join(final_root, entry))
                    if os.path.isdir(candidate) and candidate != final_dir:
                        stale_subfolder_dirs.add(candidate)
        except OSError:
            pass

        file_nodes = cmds.ls(type="file") or []
        if not file_nodes:
            return

        reloaded = []
        # v2.2 修正: reload_textures() と同様の理由で、呼び出し前の状態を
        # 保存してから復帰させる(詳細は reload_textures() のコメント参照)。
        prev_undo_state = cmds.undoInfo(query=True, stateWithoutFlush=True)
        cmds.undoInfo(stateWithoutFlush=True)
        corrected = []
        try:
            for node in file_nodes:
                try:
                    tex_path = cmds.getAttr(node + ".fileTextureName")
                except Exception:
                    continue
                if not tex_path:
                    continue
                tex_dir = os.path.normpath(os.path.dirname(tex_path))
                if tex_dir == final_dir:
                    pass
                elif tex_dir in stale_subfolder_dirs and self._matches_known_texture_set_prefix(
                        os.path.basename(tex_path)):
                    # 2026.07.15-05: ファイル名が現在のプロジェクトの
                    # 既知テクスチャセットのprefixと一致することを
                    # 確認した上で補正する(無関係な他プロジェクトを
                    # 誤補正しないための安全確認)。
                    new_path = os.path.join(final_dir, os.path.basename(tex_path))
                    tex_path = new_path
                    corrected.append(node)
                else:
                    continue
                try:
                    cmds.setAttr(node + ".fileTextureName", "", type="string")
                    cmds.setAttr(node + ".fileTextureName", tex_path, type="string")
                    reloaded.append(node)
                except Exception as e:
                    self._emit_status("再読込に失敗: {0} ({1})".format(node, e))

            if corrected:
                self._emit_status(
                    "{0} 個のノードが古いプロジェクト用Finalフォルダを参照していたため、"
                    "現在のフォルダへ参照先を補正しました。".format(len(corrected))
                )

            if reloaded:
                self._flush_renderer_caches()
                self._flush_viewport_cache()
                self.stats["reload_count"] += 1
                self.stats["last_reload_at"] = _now()
                self.stats["last_node_count"] = len(reloaded)
                self.stats_changed.emit(dict(self.stats))
                self._emit_status("{0} 個の高画質(Final)テクスチャを再読込しました。".format(len(reloaded)))
        finally:
            cmds.undoInfo(stateWithoutFlush=prev_undo_state)

    def _flush_renderer_caches(self):
        renderer = (self.config.get("renderer") or "none").lower()
        if renderer == "arnold":
            if cmds.pluginInfo("mtoa", q=True, loaded=True):
                try:
                    cmds.arnoldFlushCache(textures=True)
                except Exception as e:
                    self._emit_status("Arnold キャッシュフラッシュに失敗: {0}".format(e))
            else:
                self._emit_status("mtoa 未ロードのため Arnold キャッシュフラッシュをスキップしました。")
        elif renderer == "redshift":
            self._emit_status("Redshift: mtime自動検知に依存(要検証)。")
        elif renderer == "vray":
            self._emit_status("V-Ray: 専用フラッシュコマンド未確定。IPR再起動を推奨(要検証)。")

    def _flush_viewport_cache(self):
        try:
            cmds.ogs(reset=True)
        except Exception as e:
            self._emit_status("ogs(reset=True) に失敗: {0}".format(e))
        cmds.refresh(force=True)

    # -- Phase 2: テクスチャセット構造への対応 -------------------------------

    def get_known_texture_sets(self):
        return _active_texture_sets(self.config)

    def get_active_project_label(self):
        """マテリアル構造タブに表示する「今どのプロジェクトの一覧を
        見ているか」の説明文を返す。SP側が未対応(active_project_keyが
        無い)場合はその旨を明示し、誤解を防ぐ。

        2026.07.15-01: このシーンに紐付けられたSPプロジェクトと、
        SP側が今実際に開いているプロジェクトが食い違っている場合は
        その旨も明示する(状態バー上部の表示と合わせて、一覧が
        「別プロジェクトのものかもしれない」と気づけるようにするため)。
        """
        by_project = self.config.get("known_texture_sets_by_project", {})
        active_key = self.config.get("active_project_key")
        linked_key = _get_current_scene_project_link()

        if active_key and active_key in by_project:
            name = os.path.basename(active_key) if active_key != "__unsaved__" else "(未保存のプロジェクト)"
            label = "現在のSPプロジェクト: {0}".format(name)
            if linked_key and linked_key != _normalize_project_key_for_compare(active_key):
                label += "  ※このシーンの紐付け先とは異なります"
            return label
        return "SP側のプロジェクト情報が未取得のため、記録済み全プロジェクト分を表示中"

    def get_shading_engine_map(self, project_key=None):
        """現在アクティブなSPプロジェクトに対応するシェーダー割当マップを
        返す。2026.07.15-01: 別プロジェクトの同名テクスチャセットと
        混同しないよう、プロジェクトキーでスコープする。project_key を
        省略した場合は self.config["active_project_key"] を使う。
        紐付けが無い(未対応の古いSP側、または未保存プロジェクト)場合は
        空の辞書を返す(従来の「全部混ぜて返す」フォールバックは、
        誤って別プロジェクトのシェーダーを「対応済み」と誤判定する
        危険の方が大きいため、あえて行わない)。
        """
        key = project_key if project_key is not None else self.config.get("active_project_key")
        if not key:
            return {}
        by_project = self.config.get("texture_set_shading_engine_map_by_project", {})
        return dict(by_project.get(key, {}))

    def _matches_known_texture_set_prefix(self, filename):
        """ファイル名が、現在アクティブなSPプロジェクトの既知テクスチャ
        セットのいずれかのprefixと一致するかを確認する。

        2026.07.15-05: reload_textures()/reload_final_textures() の
        古いサブフォルダ補正を、無関係な他プロジェクトを巻き込まずに
        より広い範囲(sp_to_aiStandardSurface.py 経由で生成され、
        _last_active_watch_dir/_last_active_final_dir の追跡対象外に
        あるノードも含む)に安全に適用できるようにするための判定。
        ディレクトリの一致ではなくファイル名のprefixで判定するため、
        「本当にこのプロジェクトが書き出したファイルか」をより確実に
        確認できる。
        """
        known = self.get_known_texture_sets()
        for name in known:
            prefix = _export_prefix(self.config, name) + "_"
            if filename.startswith(prefix):
                return True
        return False

    def _managed_dirs(self):
        """ライブ同期パイプラインが把握しているフォルダ(監視用の
        プレビューフォルダ・保存時の高画質Finalフォルダ)の集合を返す。
        Phase 6: 品質切り替え後もマッピング状況・孤立ノード判定が
        正しく機能するよう、両方のフォルダを対象にする。

        複数プロジェクト対応: Final は <final_export_dir> 直下ではなく
        <final_export_dir>/<active_final_subfolder>/ に書き出されるため、
        後者も対象に含める(直下のみだと、サブフォルダを参照している
        file ノードが「孤立ノード」や「切り替え対象」として認識されない)。
        2026.07.14-02: 所有権問題回避のため、Live/Preview側も同様に
        <watch_dir>/<active_watch_subfolder>/ へ書き出されるようになった
        ため、Finalと同じくアクティブサブフォルダも対象に含める。
        """
        dirs = set()
        watch_dir = self.config.get("watch_dir")
        if watch_dir:
            dirs.add(os.path.normpath(watch_dir))
            active_watch = self._active_watch_dir()
            if active_watch:
                dirs.add(os.path.normpath(active_watch))
        final_dir = self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"]
        if final_dir:
            dirs.add(os.path.normpath(final_dir))
            active_dir = self._active_final_dir()
            if active_dir:
                dirs.add(os.path.normpath(active_dir))
        return dirs

    def _active_watch_dir(self):
        """現在アクティブなプロジェクトのLive(プレビュー)書き出し先の
        実パスを返す。_active_final_dir() と対になる関数。

        複数プロジェクト対応(所有権問題回避、2026.07.14-02): SP側は
        Live/Preview も <watch_dir>/<active_watch_subfolder>/ に書き出す。
        active_watch_subfolder が共有設定に無ければ(未対応の古いSP側や、
        まだ一度もプレビュー書き出しをしていない場合)、後方互換として
        watch_dir 直下を返す。
        """
        watch_dir = self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"]
        if not watch_dir:
            return None
        subfolder = self.config.get("active_watch_subfolder")
        if subfolder:
            return os.path.join(watch_dir, subfolder)
        return watch_dir

    def _active_final_dir(self):
        """現在アクティブなプロジェクトのFinal書き出し先の実パスを返す。

        複数プロジェクト対応: SP側は Final を
        <final_export_dir>/<active_final_subfolder>/ に書き出す。
        active_final_subfolder が共有設定に無ければ(未対応の古いSP側や、
        まだ一度もFinalを書き出していない場合)、後方互換として
        final_export_dir 直下を返す。
        """
        final_dir = self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"]
        if not final_dir:
            return None
        subfolder = self.config.get("active_final_subfolder")
        if subfolder:
            return os.path.join(final_dir, subfolder)
        return final_dir

    def is_texture_set_mapped(self, name):
        """このテクスチャセットに対応するシェーディンググループが
        シーン内に既に存在するかどうかを判定する(副作用として監視を
        再起動しない: Phase 2最適化で save_mapping_only() に変更済み)。

        2026.07.15-01: get_shading_engine_map() が現在アクティブな
        SPプロジェクトにスコープされたため、別プロジェクトの同名
        テクスチャセット(例: 別プロジェクトの "Body")に割り当てられた
        シェーダーを誤って「対応済み」と判定することが無くなった。
        """
        mapping = self.get_shading_engine_map()
        sg_name = mapping.get(name)
        if sg_name and cmds.objExists(sg_name):
            return True, sg_name

        managed_dirs = self._managed_dirs()
        prefix = _export_prefix(self.config, name) + "_"
        for node in cmds.ls(type="file") or []:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if not tex_path:
                continue
            if os.path.normpath(os.path.dirname(tex_path)) not in managed_dirs:
                continue
            if os.path.basename(tex_path).startswith(prefix):
                sgs = cmds.listConnections(node, type="shadingEngine") or []
                found_sg = sgs[0] if sgs else None
                if found_sg:
                    self.save_shading_engine_mapping(name, found_sg)
                return True, found_sg
        return False, None

    def find_orphan_file_nodes(self):
        managed_dirs = self._managed_dirs()
        known = set(self.get_known_texture_sets())
        orphans = []
        for node in cmds.ls(type="file") or []:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if not tex_path:
                continue
            if os.path.normpath(os.path.dirname(tex_path)) not in managed_dirs:
                continue
            base = os.path.basename(tex_path)
            matched = False
            for name in known:
                if base.startswith(_export_prefix(self.config, name) + "_"):
                    matched = True
                    break
            if not matched:
                orphans.append(node)
        return orphans

    def detect_current_quality(self):
        """シーン内のfileノードが実際に監視フォルダ(プレビュー)と
        Finalフォルダのどちらを参照しているかを調べる。
        Maya再起動をまたぐとGUIの表示品質ボタンは初期化されるが、
        fileノードのパス自体はシーンファイルに保存されたまま残るため、
        起動直後にここで実態を検出し、ボタンの見た目を合わせることで
        「ボタンはプレビュー表示なのに実際はFinalのまま切り替えられない」
        というズレを防ぐ。
        戻り値: Finalのみを参照していればTrue、監視フォルダのみを参照
        していればFalse、fileノードが無い/両方混在している等で判別
        できない場合はNone。

        複数プロジェクト対応: Finalは final_export_dir 直下だけでなく
        現在アクティブなプロジェクトのサブフォルダ(_active_final_dir())
        にも書き出される。直下しか見ないと、サブフォルダを参照している
        (=switch_texture_qualityで正しく用いた)ノードが「Finalではない」
        と誤判定され、Maya再起動後にボタンの見た目がプレビューに戻って
        しまう。両方を「Final」として扱う。
        2026.07.14-02: 所有権問題回避のため、Live/Preview側も同様に
        <watch_dir>/<active_watch_subfolder>/ へ書き出されるようになった
        ため、Watch側も直下+アクティブサブフォルダの両方を対象にする。
        """
        final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        active_final_dir = self._active_final_dir()
        final_dirs = {final_dir}
        if active_final_dir:
            final_dirs.add(os.path.normpath(active_final_dir))
        watch_dir = os.path.normpath(self.config.get("watch_dir") or "")
        watch_dirs = {watch_dir} if watch_dir else set()
        active_watch_dir = self._active_watch_dir()
        if active_watch_dir:
            watch_dirs.add(os.path.normpath(active_watch_dir))
        found_final = False
        found_watch = False
        for node in cmds.ls(type="file") or []:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if not tex_path:
                continue
            d = os.path.normpath(os.path.dirname(tex_path))
            if d in final_dirs:
                found_final = True
            elif d in watch_dirs:
                found_watch = True
        if found_final and not found_watch:
            return True
        if found_watch and not found_final:
            return False
        return None

    def switch_texture_quality(self, use_final):
        """file ノードを監視フォルダ(プレビュー)⇔Finalフォルダ(高画質)の
        間で明示的に切り替える。両フォルダで書き出しファイル名
        (prefix_suffix.ext)は共通のため、フォルダ部分だけを付け替える。
        自動切り替えは行わない(切り替わったタイミングが分かりにくく
        なることを避けるため、常にGUIのボタン操作からのみ呼び出される)。

        複数プロジェクト対応: 「Final」は final_export_dir 直下ではなく
        現在アクティブなプロジェクトのサブフォルダ(_active_final_dir())を
        指す。旧バージョンで final_export_dir 直下に書き出されたノードや、
        別プロジェクトのサブフォルダを参照したままのノードも、切り替え
        対象として拾えるよう managed_dirs には両方を含める。
        2026.07.14-02: 「プレビュー」側も同様に、固定の watch_dir 直下
        ではなく現在アクティブなプロジェクトのサブフォルダ
        (_active_watch_dir())を指すようにした。共有PC環境で、watch_dir
        直下に過去の別Windowsユーザーが残したファイルが混在していると、
        NTFSの所有権(ACL)により別ユーザーからは読めず
        (PermissionError)、プレビューへ切り替えてもテクスチャが
        表示されない不具合として実機で確認された。切り替え先を必ず
        「自分が今回新規作成したサブフォルダ」にすることで、この種の
        所有権衝突を回避する。

        戻り値: 実際に切り替えたノード数。
        """
        # ユーザーがボタンを押した瞬間、直近のタイマー実行(最大3秒前)から
        # SP側でプロジェクトが切り替わっている可能性があるため、ここでも
        # 念のため最新のactive_watch_subfolder/active_final_subfolderを
        # 読み直しておく(_ensure_active_dirs_watchedのタイマー任せだと
        # 最大3秒のズレが生じうるため)。
        self._refresh_dynamic_config()

        watch_dir = os.path.normpath(self.config["watch_dir"])
        active_watch_dir = self._active_watch_dir()
        active_watch_dir = os.path.normpath(active_watch_dir) if active_watch_dir else watch_dir
        final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        active_final_dir = self._active_final_dir()
        active_final_dir = os.path.normpath(active_final_dir) if active_final_dir else final_dir
        dest_dir = active_final_dir if use_final else active_watch_dir
        managed_dirs = {watch_dir, active_watch_dir, final_dir, active_final_dir}

        # Phase 6不具合修正: 以前はボタンの状態から推測した「切り替え元」
        # フォルダのノードだけを対象にしていたが、Maya再起動でボタンの
        # 見た目は初期化されてもシーン内のfileノードのパスはそのまま
        # 保存されているため、両者がズレて一切切り替えられなくなる
        # 不具合があった。監視フォルダ・Finalフォルダ(直下・アクティブ
        # サブフォルダの両方)のどれかを参照しているノードも対象に含め、
        # 常に希望の状態(dest_dir)へ収束させる。
        nodes = []
        for node in cmds.ls(type="file") or []:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if tex_path and os.path.normpath(os.path.dirname(tex_path)) in managed_dirs:
                nodes.append(node)

        if not nodes:
            self._emit_status(
                "切り替え対象の file ノードが見つかりませんでした"
                "(監視フォルダ・Finalフォルダのいずれを参照しているノードもありません)。"
            )
            return 0

        switched = []
        missing = []
        already = 0
        # v2.2 修正: reload_textures() と同様の理由で、呼び出し前の状態を
        # 保存してから復帰させる(詳細は reload_textures() のコメント参照)。
        prev_undo_state = cmds.undoInfo(query=True, stateWithoutFlush=True)
        cmds.undoInfo(stateWithoutFlush=True)
        try:
            for node in nodes:
                try:
                    tex_path = cmds.getAttr(node + ".fileTextureName")
                except Exception:
                    continue
                if os.path.normpath(os.path.dirname(tex_path)) == dest_dir:
                    already += 1
                    continue
                base = os.path.basename(tex_path)
                new_path = os.path.join(dest_dir, base)
                if not os.path.isfile(new_path):
                    missing.append(base)
                    continue
                try:
                    cmds.setAttr(node + ".fileTextureName", "", type="string")
                    cmds.setAttr(node + ".fileTextureName", new_path, type="string")
                    switched.append(node)
                except Exception as e:
                    self._emit_status("切り替えに失敗: {0} ({1})".format(node, e))
        finally:
            cmds.undoInfo(stateWithoutFlush=prev_undo_state)

        if switched or already:
            self._flush_renderer_caches()
            self._flush_viewport_cache()
            self.using_final_quality = use_final
            label = "高画質版(Final)" if use_final else "リアルタイムプレビュー"
            if switched and already:
                self._emit_status(
                    "{0} 個のノードを{1}に切り替えました({2} 個は既にこの状態でした)。".format(
                        len(switched), label, already
                    )
                )
            elif switched:
                self._emit_status("{0} 個のノードを{1}に切り替えました。".format(len(switched), label))
            else:
                self._emit_status("{0} 個のノードは既に{1}でした。".format(already, label))
        if missing:
            hint = "SP側でプロジェクトを保存すると高画質版が生成されます。" if use_final else ""
            shown = ", ".join(missing[:5]) + ("..." if len(missing) > 5 else "")
            self._emit_status(
                "{0} 件は切り替え先に画像が見つからなかったため据え置きました({1})。{2}".format(
                    len(missing), shown, hint
                )
            )
        return len(switched) + already

    def create_shader_network(self, texture_set_name, channels=None):
        """Arnold 用の標準シェーダーネットワークを自動生成する。
        戻り値: 作成した shadingEngine 名。
        どのジオメトリに割り当てるかはここでは行わない(手動作業)。
        失敗時には作成済みのノードをロールバック(削除)する。

        2026.07.16(緊急修正、シミュレーションで発見): SP側のプロジェクトが
        まだ未保存("__unsaved__")の段階でシェーダーを生成すると、
        生成されるfileノードは "__unsaved__" 用の一時サブフォルダを
        参照する。その後SP側で実際に名前を付けて保存すると、
        active_watch_subfolder は正しいプロジェクト名のサブフォルダに
        切り替わるが、既に生成済みのfileノードのパスは自動更新されず、
        is_texture_set_mapped() のフォールバック走査(managed_dirsとの
        一致判定)にも該当しなくなるため、「対応済みのはずなのに未対応と
        誤判定され続ける」「再度シェーダー生成を試みると名前衝突で
        RuntimeErrorになる」という不具合が起こり得る。
        シーンの紐付け機能(_on_link_scene_to_sp_project)で既に採用して
        いるのと同じ方針で、未保存の間はシェーダー生成自体をブロックし、
        先にSP側で保存するよう案内する。
        """
        active_key = self.config.get("active_project_key")
        if active_key == "__unsaved__":
            raise RuntimeError(
                "SP側のプロジェクトがまだ保存されていません。"
                "この状態でシェーダーを生成すると、後で保存した際にテクスチャの"
                "参照先が食い違ってしまいます。SP側で一度「名前を付けて保存」して"
                "から、もう一度お試しください。"
            )

        renderer = (self.config.get("renderer") or "none").lower()
        if renderer != "arnold":
            raise RuntimeError(
                "現在自動生成に対応しているのは Arnold のみです"
                "(V-Ray / Redshift は Phase 3 で対応予定)。"
            )
        if not cmds.pluginInfo("mtoa", q=True, loaded=True):
            raise RuntimeError("mtoa プラグインがロードされていません。")

        channels = channels or CHANNEL_SUFFIXES
        # ノード名の生成には _safe_name() (Mayaのノード名として妥当な
        # 文字列)を使う。実テクスチャファイルのパス生成には、SP側が
        # 実際に書き出した prefix が分かっていればそちらを使う
        # (export_prefix)。両者は初回エクスポート前は同じ値になりうるが、
        # スペース・日本語等を含む名前では異なる場合がある。
        safe = _safe_name(texture_set_name)
        export_prefix = _export_prefix(self.config, texture_set_name)

        # Phase 2 最適化: 名前の衝突を事前チェックする。Mayaに自動リネーム
        # させると意図しない名前のノードができてマッピングが崩れるため。
        mat_name = "{0}_mat".format(safe)
        sg_name = "{0}SG".format(safe)
        if cmds.objExists(mat_name) or cmds.objExists(sg_name):
            raise RuntimeError(
                "'{0}' または '{1}' という名前のノードが既に存在します。"
                "手動で整理してから再実行してください。".format(mat_name, sg_name)
            )

        # 複数プロジェクト対応(所有権問題回避、2026.07.14-02): 生成する
        # file ノードは、固定の watch_dir 直下ではなく現在アクティブな
        # プロジェクトのサブフォルダを参照するようにする。
        active_watch_dir = self._active_watch_dir()
        watch_dir = os.path.normpath(active_watch_dir) if active_watch_dir else os.path.normpath(
            self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"])
        ext = self.config.get("file_format", "png")
        raw_suffixes = set(self.config.get("raw_colorspace_suffixes", []))

        created_nodes = []  # Phase 2 最適化: 失敗時ロールバック用

        try:
            mat = cmds.shadingNode("aiStandardSurface", asShader=True, name=mat_name)
            created_nodes.append(mat)
            sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=sg_name)
            created_nodes.append(sg)
            cmds.connectAttr(mat + ".outColor", sg + ".surfaceShader", force=True)

            file_nodes = {}
            for suffix in channels:
                file_node = cmds.shadingNode("file", asTexture=True, isColorManaged=True,
                                              name="{0}_{1}_file".format(safe, suffix))
                created_nodes.append(file_node)
                tex_path = os.path.join(watch_dir, "{0}_{1}.{2}".format(export_prefix, suffix, ext))
                cmds.setAttr(file_node + ".fileTextureName", tex_path, type="string")
                # Phase 3: colorSpaceの名称はカラーマネジメント設定(Legacy/
                # ACES/カスタムOCIO等)によって使える文字列が異なるため、
                # 候補を順に試し、すべて失敗した場合のみログに警告を出す。
                candidates = RAW_COLORSPACE_CANDIDATES if suffix in raw_suffixes else SRGB_COLORSPACE_CANDIDATES
                applied_space = None
                for candidate in candidates:
                    try:
                        cmds.setAttr(file_node + ".colorSpace", candidate, type="string")
                        applied_space = candidate
                        break
                    except Exception:
                        continue
                if applied_space is None:
                    self._emit_status(
                        "警告: {0} のcolorSpace設定に失敗しました(候補: {1})。"
                        "現在のカラーマネジメント設定に合わせて手動で設定してください。".format(
                            file_node, ", ".join(candidates)
                        )
                    )
                p2d = self._connect_place2d(file_node)
                created_nodes.append(p2d)
                file_nodes[suffix] = file_node

            if "BaseColor" in file_nodes:
                cmds.connectAttr(file_nodes["BaseColor"] + ".outColor", mat + ".baseColor", force=True)
            if "Roughness" in file_nodes:
                cmds.connectAttr(file_nodes["Roughness"] + ".outColorR", mat + ".specularRoughness", force=True)
            if "Metallic" in file_nodes:
                cmds.connectAttr(file_nodes["Metallic"] + ".outColorR", mat + ".metalness", force=True)
            if "Emissive" in file_nodes:
                cmds.connectAttr(file_nodes["Emissive"] + ".outColor", mat + ".emissionColor", force=True)
                cmds.setAttr(mat + ".emission", 1.0)

            if "Normal" in file_nodes:
                try:
                    normal_map_node = cmds.shadingNode("aiNormalMap", asUtility=True, name="{0}_normalMap".format(safe))
                    created_nodes.append(normal_map_node)
                    cmds.connectAttr(file_nodes["Normal"] + ".outColor", normal_map_node + ".input", force=True)
                    cmds.connectAttr(normal_map_node + ".outValue", mat + ".normalCamera", force=True)
                except Exception as e:
                    self._emit_status("aiNormalMap の接続に失敗しました(手動接続が必要です): {0}".format(e))

            if "Height" in file_nodes:
                try:
                    # SPのHeightチャンネルは 0-1 の範囲で「0.5 = 変位なし」を
                    # 意味するグレースケール値だが、displacementShader の
                    # .displacement は入力値をそのまま変位量として扱うため、
                    # 生の値を直結すると常に "0.5 * scale" 分だけ全体が
                    # 一方向に膨らみ、さらに値のムラがそのまま歪みとして
                    # 出てしまう(実機テストで確認された不具合)。
                    # remapValue で 0-1 -> -0.5-0.5 に変換し、0.5(中間値)を
                    # 変位ゼロの基準点として再センタリングしてから接続する。
                    remap_node = cmds.shadingNode("remapValue", asUtility=True, name="{0}_heightRemap".format(safe))
                    created_nodes.append(remap_node)
                    cmds.setAttr(remap_node + ".inputMin", 0.0)
                    cmds.setAttr(remap_node + ".inputMax", 1.0)
                    cmds.setAttr(remap_node + ".outputMin", -0.5)
                    cmds.setAttr(remap_node + ".outputMax", 0.5)
                    cmds.connectAttr(file_nodes["Height"] + ".outColorR", remap_node + ".inputValue", force=True)

                    disp_node = cmds.shadingNode("displacementShader", asShader=True, name="{0}_disp".format(safe))
                    created_nodes.append(disp_node)
                    cmds.connectAttr(remap_node + ".outValue", disp_node + ".displacement", force=True)
                    cmds.connectAttr(disp_node + ".displacement", sg + ".displacementShader", force=True)
                    # ベストエフォート: scale属性が存在する場合のみ設定する。
                    # 実際の変位「量」(何cm膨らむか等)はモデルのスケールや
                    # 素材の質感次第で変わるため、まずは控えめな値から始め、
                    # レンダービューを見ながら現場で調整することを推奨する
                    # (要現場検証。0.5に再センタリングしても、値自体の
                    # 大小=変位量はscaleで決まる)。
                    if cmds.attributeQuery("scale", node=disp_node, exists=True):
                        cmds.setAttr(disp_node + ".scale", 0.1)

                    # Phase 3' 追加: シェーダー側の再センタリングだけでは
                    # 不十分で、割り当て先メッシュのシェイプノード側で
                    # Arnold Subdivision(aiSubdivType/aiSubdivIterations)と
                    # aiDispPadding を別途設定しないと、変位が「頂点だけが
                    # 動く歪み」や「クリッピングによる欠け」として見える
                    # ことが実機テストで確認された(スクリプト側はどの
                    # メッシュに割り当てるか関知しないため自動設定しない)。
                    self._emit_status(
                        "注意: Heightチャンネルの変位を正しく表示するには、"
                        "割り当て先メッシュのシェイプノードで Arnold の "
                        "Subdivision(aiSubdivType=catclark等)と aiDispPadding "
                        "を別途設定してください。未設定のままだと、変位が "
                        "頂点単位のカクカクした歪みに見えることがあります。"
                    )
                except Exception as e:
                    self._emit_status("displacementShader の接続に失敗しました(手動接続が必要です): {0}".format(e))

        except Exception:
            # ロールバック: 作成済みのノードを削除して中途半端な状態を残さない
            existing = [n for n in created_nodes if n and cmds.objExists(n)]
            if existing:
                try:
                    cmds.delete(existing)
                except Exception:
                    pass
            raise

        self.save_shading_engine_mapping(texture_set_name, sg)

        self._emit_status(
            "'{0}' 用のシェーダーを生成しました({1})。"
            "ジオメトリへの割り当ては手動で行ってください。".format(texture_set_name, sg)
        )
        return sg

    def _connect_place2d(self, file_node):
        p2d = cmds.shadingNode("place2dTexture", asUtility=True, name=file_node + "_p2d")
        for src, dst in PLACE2D_ATTR_PAIRS:
            try:
                cmds.connectAttr(p2d + "." + src, file_node + "." + dst, force=True)
            except Exception:
                pass
        cmds.connectAttr(p2d + ".outUV", file_node + ".uvCoord", force=True)
        cmds.connectAttr(p2d + ".outUvFilterSize", file_node + ".uvFilterSize", force=True)
        return p2d


# ---------------------------------------------------------------------------
# Phase 3: 初回セットアップウィザード
# ---------------------------------------------------------------------------
#
# 他人にこのパイプラインを共有した際、SP側との対応関係(特に監視フォルダの
# パスを一致させる必要があること)が分かりにくいため、対話形式で案内する
# ウィザード。接続テストページで、SP側から実際にファイルが届いているかを
# その場で確認できるようにしてある。

class SetupWizard(QtWidgets.QWizard):

    def __init__(self, watcher, parent=None):
        super(SetupWizard, self).__init__(parent)
        self.watcher = watcher
        self.setWindowTitle("SP Live Sync 初期セットアップ")
        self.setMinimumSize(560, 420)
        # QWizardの既定スタイル(Windowsだと ModernStyle/AeroStyle)は、
        # 上部に白背景のバナー領域を独自描画するため、Mayaのダーク
        # テーマと合わずに浮いて見える。ClassicStyleはこのバナー領域を
        # 描画しないため、ホストアプリのテーマに馴染みやすい。
        self.setWizardStyle(QtWidgets.QWizard.ClassicStyle)

        self.addPage(self._welcome_page())
        self.addPage(self._folder_page())
        self.addPage(self._test_page())

    def _welcome_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("ようこそ")
        layout = QtWidgets.QVBoxLayout(page)
        label = QtWidgets.QLabel(
            "このウィザードでは、Substance Painterから送られてくるテクスチャを"
            "Mayaへ自動反映するための最低限の設定を行います。\n\n"
            "「監視フォルダ」は、Substance Painter側のセットアップウィザードに"
            "表示された「監視フォルダ」と完全に同じパスを指定してください。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        return page

    def _folder_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("監視フォルダとレンダラー")
        layout = QtWidgets.QFormLayout(page)

        cfg = self.watcher.config
        row = QtWidgets.QHBoxLayout()
        self.watch_edit = QtWidgets.QLineEdit(cfg.get("watch_dir", ""))
        browse_btn = QtWidgets.QPushButton("参照...")
        browse_btn.clicked.connect(self._browse)
        row.addWidget(self.watch_edit)
        row.addWidget(browse_btn)
        row_widget = QtWidgets.QWidget()
        row_widget.setLayout(row)
        layout.addRow("監視フォルダ (★ SP側と一致させる)", row_widget)

        self.renderer_combo = QtWidgets.QComboBox()
        self.renderer_combo.addItems(RENDERER_CHOICES)
        renderer = cfg.get("renderer", "arnold")
        if renderer in RENDERER_CHOICES:
            self.renderer_combo.setCurrentIndex(RENDERER_CHOICES.index(renderer))
        layout.addRow("レンダラー", self.renderer_combo)

        return page

    def _browse(self):
        current = self.watch_edit.text() or "C:/"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "監視フォルダを選択", current)
        if selected:
            self.watch_edit.setText(selected)

    def _test_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("接続テスト")
        layout = QtWidgets.QVBoxLayout(page)
        self.test_result_label = QtWidgets.QLabel("「テストを実行」を押してください。")
        self.test_result_label.setWordWrap(True)
        layout.addWidget(self.test_result_label)
        test_btn = QtWidgets.QPushButton("テストを実行")
        test_btn.clicked.connect(self._run_test)
        layout.addWidget(test_btn)
        layout.addStretch(1)
        return page

    def _run_test(self):
        watch_dir = self.watch_edit.text().strip()
        if not watch_dir:
            self.test_result_label.setText("監視フォルダが未指定です。")
            return
        norm = os.path.normpath(watch_dir)
        if not os.path.isdir(norm):
            self.test_result_label.setText(
                "フォルダが存在しません: {0}\n"
                "SP側で一度「今すぐ同期」を実行すると自動的に作成されます。".format(norm)
            )
            return
        try:
            entries = os.listdir(norm)
        except Exception as e:
            self.test_result_label.setText("フォルダの読み取りに失敗しました: {0}".format(e))
            return
        flag_exists = "_sync_complete.flag" in entries
        image_exts = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr")
        texture_count = len([e for e in entries if e.lower().endswith(image_exts)])
        if flag_exists or texture_count > 0:
            self.test_result_label.setText(
                "OK: フォルダを確認できました。テクスチャファイル {0} 件、"
                "同期完了フラグ: {1}\n\n"
                "「完了」を押して設定を保存してください。".format(
                    texture_count, "あり" if flag_exists else "なし(まだSP側で同期されていない可能性があります)"
                )
            )
        else:
            self.test_result_label.setText(
                "フォルダは存在しますが、まだテクスチャファイルがありません。\n"
                "SP側で「Live Sync を有効にする」をONにし、一度「今すぐ同期」を"
                "実行してから、このテストを再実行してください。"
            )

    def accept(self):
        new_values = {
            "watch_dir": self.watch_edit.text().strip(),
            "renderer": self.renderer_combo.currentText(),
            "setup_wizard_completed": True,
        }
        if not new_values["watch_dir"]:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "監視フォルダが未指定です。")
            return
        self.watcher.apply_and_save_config(new_values)
        super(SetupWizard, self).accept()


# ---------------------------------------------------------------------------
# GUI: ドッキング可能なウィンドウ
# ---------------------------------------------------------------------------

# ユーザーセットアップ/shelfボタンから呼ばれる、
# ウィンドウのシングルトンインスタンス。
_window_instance = None


class LiveSyncWindow(MayaQWidgetDockableMixin, QtWidgets.QWidget):

    def __init__(self, parent=None):
        super(LiveSyncWindow, self).__init__(parent=parent)
        self.setObjectName(WINDOW_OBJECT_NAME)
        # ウィンドウタイトルにもバージョンを出し、Script Editorのログを
        # 遡らなくても現在動作中のバージョンが一目で分かるようにする。
        self.setWindowTitle("SP Live Sync  (v{0})".format(__version__))

        self.watcher = LiveSyncWatcher(self)
        self.channel_checkboxes = {}
        # 2026.07.16: _refresh_scene_link_label() の再入防止フラグ
        # (詳細はそのメソッドのコメント参照)。
        self._refreshing_scene_link_label = False

        outer_layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        outer_layout.addWidget(tabs)

        # --- タブ1: 同期設定 ---
        sync_tab = QtWidgets.QWidget()
        tabs.addTab(sync_tab, "同期")
        layout = QtWidgets.QVBoxLayout(sync_tab)

        # 2026.07.15-01: 状態バー。よく使う/よく確認する情報を
        # ウィンドウを開いて最初に目に入る最上部にまとめて常設する。
        # 「誰でも分かりやすく、よく使う設定はクリックしやすい位置に」
        # という方針に沿って、これらは全て operate ボタン(監視ON/OFF)
        # より上に配置している。
        status_bar_group = QtWidgets.QGroupBox("状態")
        status_bar_layout = QtWidgets.QVBoxLayout(status_bar_group)
        status_bar_layout.setSpacing(4)

        # 行1: シーン ⇔ SPプロジェクトの対応表示 + 設定ボタン
        scene_link_row = QtWidgets.QHBoxLayout()
        self.scene_link_label = QtWidgets.QLabel("(確認中...)")
        self.scene_link_label.setWordWrap(True)
        self.scene_link_btn = QtWidgets.QPushButton("SPプロジェクトを設定")
        self.scene_link_btn.setToolTip(
            "今SPで開いているプロジェクトを、このMayaシーンの対応先として"
            "登録します。\n登録しておくと、次回このシーンを開いた時に"
            "自動的に同じSPプロジェクトを追跡できます。"
        )
        self.scene_link_btn.clicked.connect(self._on_link_scene_to_sp_project)
        scene_link_row.addWidget(self.scene_link_label, stretch=1)
        scene_link_row.addWidget(self.scene_link_btn)
        status_bar_layout.addLayout(scene_link_row)

        # 行2: 他セッション警告(通常は非表示、検出時のみ出す)
        other_session_row = QtWidgets.QHBoxLayout()
        self.other_session_label = QtWidgets.QLabel("")
        self.other_session_label.setStyleSheet("color: #c9822a;")
        self.other_session_label.setWordWrap(True)
        self.other_session_clear_btn = QtWidgets.QPushButton("無視して削除")
        self.other_session_clear_btn.setToolTip(
            "身に覚えのないセッション警告が出続ける場合、記録されている"
            "ロック情報を削除します。監視中の実害はありません。"
        )
        self.other_session_clear_btn.clicked.connect(self._on_clear_other_session)
        other_session_row.addWidget(self.other_session_label, stretch=1)
        other_session_row.addWidget(self.other_session_clear_btn)
        status_bar_layout.addLayout(other_session_row)
        self.other_session_label.setVisible(False)
        self.other_session_clear_btn.setVisible(False)

        # 行3: テクスチャセット一覧の最終更新時刻
        self.last_structure_update_label = QtWidgets.QLabel("一覧の最終更新: -")
        self.last_structure_update_label.setStyleSheet("color: #888888; font-size: 10px;")
        status_bar_layout.addWidget(self.last_structure_update_label)

        layout.addWidget(status_bar_group)

        self.enable_btn = QtWidgets.QPushButton("監視（自動反映）: OFF")
        self.enable_btn.setCheckable(True)
        self.enable_btn.setToolTip(
            "ONにすると、SP側で塗った内容がMayaへ自動で反映されるようになります。\n"
            "(SPの書き出しフォルダを見張り、更新があるとテクスチャを読み込み直します)"
        )
        self.enable_btn.toggled.connect(self._on_toggle)
        layout.addWidget(self.enable_btn)

        # Phase 6: 表示品質の手動切り替え(プレビュー⇔Final高画質)。
        # 自動切り替えは行わない(どちらを表示中か分かりにくくなる
        # ことを避けるため)。切り替え後は、明示的にこのボタンを
        # もう一度押すまでリアルタイム更新の対象から外れる。
        self.quality_btn = QtWidgets.QPushButton("表示品質: プレビュー(リアルタイム)")
        self.quality_btn.setCheckable(True)
        self.quality_btn.setToolTip(
            "ONにすると、SP側で保存時に書き出された高画質版(Finalフォルダ)に"
            "file ノードを切り替えます。OFFに戻すとリアルタイムプレビューに戻ります。"
        )
        self.quality_btn.toggled.connect(self._on_quality_toggled)
        layout.addWidget(self.quality_btn)

        form = QtWidgets.QFormLayout()
        row = QtWidgets.QHBoxLayout()
        self.watch_edit = QtWidgets.QLineEdit()
        self.watch_edit.setToolTip(
            "SP側が塗った内容を書き出すフォルダです。ここが更新されると\n"
            "Mayaが自動で反映します。通常はSP側の設定と同じ場所になります。"
        )
        browse_btn = QtWidgets.QPushButton("参照...")
        browse_btn.setToolTip("フォルダをダイアログから選びます。")
        browse_btn.clicked.connect(self._browse_watch_dir)
        row.addWidget(self.watch_edit)
        row.addWidget(browse_btn)
        row_widget = QtWidgets.QWidget()
        row_widget.setLayout(row)
        form.addRow("監視フォルダ（SPの書き出し先）", row_widget)

        self.renderer_combo = QtWidgets.QComboBox()
        self.renderer_combo.addItems(RENDERER_CHOICES)
        self.renderer_combo.setToolTip("マテリアルを作るときに使うレンダラーです（通常はArnold）。")
        form.addRow("レンダラー", self.renderer_combo)
        layout.addLayout(form)

        # 日常的に使う操作(設定保存・履歴確認)と、初回導入時に一度だけ
        # 行えばよい操作(shelf設置・ウィザート・userSetup.py登録)を
        # 視覚的に分離する。後者は使用頻度が低いため「その他の設定」に
        # まとめ、主要な操作列を圧迫しないようにした。
        btn_row = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("設定を保存")
        self.save_btn.clicked.connect(self._on_save_clicked)
        self.history_btn = QtWidgets.QPushButton("同期履歴を開く")
        self.history_btn.setToolTip("SP側/Maya側共通の同期履歴ログ(テキストファイル)を開きます。")
        self.history_btn.clicked.connect(self._open_history_log)

        self.more_btn = QtWidgets.QToolButton()
        self.more_btn.setText("その他の設定")
        self.more_btn.setToolTip("自動起動の登録や、棚へのショートカット追加などを行えます。")
        self.more_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.more_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        more_menu = QtWidgets.QMenu(self.more_btn)

        wizard_action = more_menu.addAction("設定をやり直す（ウィザード）")
        wizard_action.setToolTip(
            "監視フォルダやレンダラーの設定を、最初のセットアップと同じ\n"
            "対話形式でやり直せます。接続テストもここから行えます。"
        )
        wizard_action.triggered.connect(self.open_setup_wizard)

        shelf_action = more_menu.addAction("ショートカットを棚（シェルフ）に追加")
        shelf_action.setToolTip(
            "画面上部の棚（シェルフ）に、このウィンドウを1クリックで開く\n"
            "ボタンを追加します。次回から素早く起動できます。"
        )
        shelf_action.triggered.connect(self._install_shelf_button)

        register_action = more_menu.addAction("Maya起動時に自動で開く")
        register_action.setToolTip(
            "次回以降、Mayaを起動したときにこのウィンドウが自動で\n"
            "開くようになります（任意）。いつでも解除できます。"
        )
        register_action.triggered.connect(self._on_register_user_setup)

        self.more_btn.setMenu(more_menu)

        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.history_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.more_btn)
        layout.addLayout(btn_row)

        stats_group = QtWidgets.QGroupBox("反映状況")
        stats_group.setToolTip("自動反映が正しく動いているかの目安です。")
        stats_layout = QtWidgets.QFormLayout(stats_group)
        self.last_reload_label = QtWidgets.QLabel("-")
        self.reload_count_label = QtWidgets.QLabel("0")
        self.node_count_label = QtWidgets.QLabel("0")
        stats_layout.addRow("最後に反映した時刻", self.last_reload_label)
        stats_layout.addRow("これまでの反映回数", self.reload_count_label)
        stats_layout.addRow("直近で反映したテクスチャ数", self.node_count_label)
        layout.addWidget(stats_group)

        layout.addWidget(QtWidgets.QLabel("ログ（動作の記録）"))
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        layout.addWidget(self.log_view, stretch=1)

        # --- タブ2: マテリアル構造(Phase 2) ---
        material_tab = QtWidgets.QWidget()
        tabs.addTab(material_tab, "マテリアル")
        mat_layout = QtWidgets.QVBoxLayout(material_tab)

        mat_layout.addWidget(QtWidgets.QLabel(
            "SP側で見つかったテクスチャセットと、Maya側でマテリアルが\n"
            "できているかの一覧です。\n"
            "「未対応」の行を選んで下のボタンを押すと、Arnold用のマテリアルを\n"
            "自動で作成します（モデルへの割り当ては手動で行ってください）。"
        ))
        # Phase 3: 今どのSPプロジェクトの一覧を見ているかを明示する
        # (複数プロジェクト混在時の誤解を防ぐため)。
        self.active_project_label = QtWidgets.QLabel("-")
        self.active_project_label.setStyleSheet("color: gray;")
        mat_layout.addWidget(self.active_project_label)

        # Phase 2 最適化: 生成するチャンネルを選択できるようにする
        channel_group = QtWidgets.QGroupBox("マテリアルに含めるチャンネル")
        channel_group.setToolTip(
            "マテリアルを作るときに、どの種類のテクスチャをつなぐかを選びます。\n"
            "（BaseColor=色、Roughness=ざらつき、Metallic=金属感、\n"
            " Normal=凹凸の向き、Height=変位、Emissive=発光）"
        )
        channel_layout = QtWidgets.QHBoxLayout(channel_group)
        for suffix in CHANNEL_SUFFIXES:
            cb = QtWidgets.QCheckBox(suffix)
            cb.setChecked(True)
            channel_layout.addWidget(cb)
            self.channel_checkboxes[suffix] = cb
        mat_layout.addWidget(channel_group)

        self.material_table = QtWidgets.QTableWidget(0, 3)
        self.material_table.setHorizontalHeaderLabels(["テクスチャセット", "状態", "シェーディンググループ"])
        self.material_table.horizontalHeader().setStretchLastSection(True)
        self.material_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.material_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        mat_layout.addWidget(self.material_table)

        mat_btn_row = QtWidgets.QHBoxLayout()
        self.refresh_material_btn = QtWidgets.QPushButton("一覧を最新にする")
        self.refresh_material_btn.setToolTip("上の一覧を、現在のSP側・Maya側の状況に合わせて更新します。")
        self.refresh_material_btn.clicked.connect(self._refresh_material_table)
        self.create_shader_btn = QtWidgets.QPushButton("選んだ素材のマテリアルを作る")
        self.create_shader_btn.setToolTip(
            "一覧で選んだテクスチャセットから、Arnold用のマテリアル\n"
            "（シェーダーネットワーク）を自動で組み立てます。\n"
            "作ったマテリアルをどのモデルに割り当てるかは手動です。"
        )
        self.create_shader_btn.clicked.connect(self._on_create_shader_clicked)
        mat_btn_row.addWidget(self.refresh_material_btn)
        mat_btn_row.addWidget(self.create_shader_btn)
        mat_layout.addLayout(mat_btn_row)

        orphan_label = QtWidgets.QLabel("使われていないファイルノード")
        orphan_label.setToolTip(
            "モデル名やマテリアル名の変更・削除によって、どのマテリアルにも\n"
            "つながらなくなって取り残された file ノードの一覧です。\n"
            "誤削除を防ぐため、このツールが自動で消すことはありません。"
        )
        mat_layout.addWidget(orphan_label)
        self.orphan_list = QtWidgets.QListWidget()
        self.orphan_list.setMaximumHeight(100)
        mat_layout.addWidget(self.orphan_list)
        self.refresh_orphan_btn = QtWidgets.QPushButton("使われていないノードを探す")
        self.refresh_orphan_btn.clicked.connect(self._refresh_orphan_list)
        mat_layout.addWidget(self.refresh_orphan_btn)

        self._load_values_from_config()

        # Phase 6不具合修正: 起動時にシーンの実際の状態を検出し、
        # 表示品質ボタンをそれに合わせておく(常にプレビュー扱いで
        # 初期化すると、実際はFinalのままの場合に切り替え不能になる)。
        detected_final = self.watcher.detect_current_quality()
        if detected_final is not None:
            self.watcher.using_final_quality = detected_final
            self.quality_btn.blockSignals(True)
            self.quality_btn.setChecked(detected_final)
            self.quality_btn.setText(
                "表示品質: {0}".format("高画質(Final)" if detected_final else "プレビュー(リアルタイム)")
            )
            self.quality_btn.blockSignals(False)

        self.watcher.status_changed.connect(self.log_view.appendPlainText)
        self.watcher.stats_changed.connect(self._on_stats_changed)
        self.watcher.other_session_changed.connect(self._on_other_session_changed)
        self.watcher.scene_link_changed.connect(self._refresh_scene_link_label)
        self.watcher.structure_changed.connect(self._on_structure_changed)

        self._refresh_material_table()
        self._refresh_scene_link_label()
        self._on_other_session_changed(_check_other_session())

        # 2026.07.15-01: Mayaのシーンが切り替わった(New Scene / Open Scene)
        # ことを検知するコールバックを登録する。以前はこれが存在せず、
        # シーンを切り替えても直前のシーン向けの監視が動き続け、今開いて
        # いるシーンのfileノードを無差別に上書きしようとする不具合が
        # あった。ウィンドウが閉じられてインスタンスが破棄される際に
        # 確実に解除できるよう、コールバックIDをインスタンスに保持する。
        self._scene_callback_ids = [
            om.MSceneMessage.addCallback(om.MSceneMessage.kAfterOpen, self._on_scene_changed),
            om.MSceneMessage.addCallback(om.MSceneMessage.kAfterNew, self._on_scene_changed),
        ]

        # Phase 5: 前回終了時に監視がONだった場合は自動的に再開する
        # (毎回手動でONを押す手間を無くすため)。setChecked(True)が
        # _on_toggle経由でwatcher.start()を呼ぶ。
        # 2026.07.15-01: ただし、このシーンに紐付いたSPプロジェクトが
        # 無い場合は自動再開しない(前のシーン向けの設定のまま監視を
        # 始めてしまう事故を防ぐため)。
        if self.watcher.config.get("watch_enabled") and _get_current_scene_project_link() is not None:
            self.enable_btn.setChecked(True)

    def _load_values_from_config(self):
        cfg = self.watcher.config
        self.watch_edit.setText(cfg.get("watch_dir", ""))
        renderer = cfg.get("renderer", "arnold")
        if renderer in RENDERER_CHOICES:
            self.renderer_combo.setCurrentIndex(RENDERER_CHOICES.index(renderer))

    def _browse_watch_dir(self):
        current = self.watch_edit.text() or "C:/"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "監視フォルダを選択", current)
        if selected:
            self.watch_edit.setText(selected)

    def _on_toggle(self, checked):
        self.enable_btn.setText("監視（自動反映）: {0}".format("ON" if checked else "OFF"))
        if checked:
            self.watcher.start()
        else:
            self.watcher.stop(reason="manual")

    # -- 2026.07.15-01: 状態バー関連 -------------------------------------

    def _refresh_scene_link_label(self):
        """状態バー上部の「シーン ⇔ SPプロジェクト」表示を更新する。

        2026.07.15-05(緊急修正): active_key(SP側が今開いているプロジェクト)
        が None の場合(SPが未起動、またはプロジェクトを何も開いていない)
        でも、`if active_key and active_key != linked_key` という判定が
        False になるため else節(緑=正常)に落ちてしまい、「SP未起動なのに
        正常」と誤表示するバグがあった。SP未起動と「紐付け先と一致」は
        明確に区別し、SP未起動の場合は独立した表示にする。

        2026.07.16(緊急修正): active_key はSP側から来る生のパス
        (Windowsではバックスラッシュ区切り)で、linked_key は
        _get_current_scene_project_link() が返すスラッシュ区切りに
        正規化済みの値だった。区切り文字が違うだけで文字列としては
        不一致になり、実際には同じプロジェクトを指しているのに
        赤字の不一致警告が出続ける不具合があった。比較前に
        active_key 側も _normalize_project_key_for_compare() で
        正規化する。

        2026.07.16(緊急修正、再発分): self.watcher.config(active_project_key
        を含む)は、これまで project_poll_timer(3秒間隔)経由の
        _refresh_dynamic_config() でのみディスクから読み直されていた。
        しかし project_poll_timer は watcher.enabled(監視ON)の間だけ
        動作するため、監視をOFFにしたまま(あるいは一度もONにしないまま)
        SP側でプロジェクトを保存しても、Maya側のメモリ上の
        active_project_key はいつまでも更新されず、状態バーが
        テンプレート名や古いプロジェクト名を表示し続ける不具合が
        あった(「監視をONにしたら直った」という報告と一致する)。
        状態バーの表示は監視のON/OFFに関わらず常に最新であるべきため、
        このメソッドの呼び出し時には毎回 _refresh_dynamic_config() を
        明示的に呼び、ディスク上の最新値を確実に反映する。

        再入防止: _refresh_dynamic_config() が active_project_key の
        変化を検知すると scene_link_changed シグナルを発火し、それが
        このメソッド自身に接続されているため、そのまま呼ぶと1回の
        更新で2回連続実行されてしまう(実害はないが無駄なため、
        フラグで防ぐ)。
        """
        if getattr(self, "_refreshing_scene_link_label", False):
            return
        self._refreshing_scene_link_label = True
        try:
            self.watcher._refresh_dynamic_config()
        finally:
            self._refreshing_scene_link_label = False
        scene_name = _scene_display_name()
        linked_key = _get_current_scene_project_link()
        active_key = _normalize_project_key_for_compare(self.watcher.config.get("active_project_key"))

        if linked_key is None:
            self.scene_link_label.setText(
                "このシーン「{0}」のSPプロジェクトは未設定です。".format(scene_name)
            )
            self.scene_link_label.setStyleSheet("color: #c9822a;")
            self.scene_link_btn.setText("SPプロジェクトを設定")
            self.scene_link_btn.setEnabled(True)
            return

        if linked_key == "__unsaved__":
            # 2026.07.16(緊急修正): SP側が未保存の段階で紐付けボタンを
            # 押してしまい、"__unsaved__" がそのままシーンに焼き付いた
            # ケース。_on_link_scene_to_sp_project() 側で今後はこの状態
            # での新規紐付けをブロックするが、既に焼き付いてしまった
            # 過去の紐付けは自動修復できないため、再設定を明示的に促す。
            self.scene_link_label.setText(
                "このシーン「{0}」は未保存だったSPプロジェクトに紐付けられています。"
                "SP側で保存後、もう一度「SPプロジェクトを設定」を押してください。".format(scene_name)
            )
            self.scene_link_label.setStyleSheet("color: #c9822a;")
            self.scene_link_btn.setText("SPプロジェクトを設定し直す")
            self.scene_link_btn.setEnabled(True)
            return

        linked_name = _project_display_name(linked_key)

        if not active_key:
            # SP側が起動していない、またはプロジェクトを何も開いていない。
            # 紐付け自体は設定済みなので、それを踏まえた文言にする
            # (緑=正常でも赤=不一致でもない、独立した状態)。
            self.scene_link_label.setText(
                "このシーン「{0}」の対応先は {1} です(SP側は現在未起動/"
                "未検出のため一致確認はできません)。".format(scene_name, linked_name)
            )
            self.scene_link_label.setStyleSheet("color: #888888;")
            self.scene_link_btn.setText("SPプロジェクトを設定し直す")
            self.scene_link_btn.setEnabled(True)
            return

        if active_key != linked_key:
            # SP側は今、紐付け先とは別のプロジェクトを開いている。
            active_name = _project_display_name(active_key)
            self.scene_link_label.setText(
                "「{0}」の対応先は {1} ですが、SP側は今 {2} を開いています。".format(
                    scene_name, linked_name, active_name
                )
            )
            self.scene_link_label.setStyleSheet("color: #c94a2a; font-weight: bold;")
        else:
            self.scene_link_label.setText(
                "このシーン「{0}」 ⇔ SPプロジェクト「{1}」".format(scene_name, linked_name)
            )
            self.scene_link_label.setStyleSheet("color: #2a9c4a;")
        self.scene_link_btn.setText("SPプロジェクトを設定し直す")
        self.scene_link_btn.setEnabled(True)

    def _on_link_scene_to_sp_project(self):
        """「SPプロジェクトを設定」ボタン: SP側が今開いているプロジェクトを
        ワンクリックで現在のMayaシーンに紐付ける。

        2026.07.16(緊急修正): このボタンが保存する紐付け情報は、
        押した瞬間の active_project_key を一度きり書き込むだけで、
        その後SP側の状態が変わっても自動更新されない(fileInfoへの
        一発書き)。そのため、SP側のプロジェクトがまだ未保存
        ("__unsaved__")の段階でこのボタンを押すと、"__unsaved__"
        という値がシーンに永久に焼き付けられてしまい、その後SP側で
        実際に名前を付けて保存して active_project_key が正しい値に
        更新されても、一度焼き付けられた紐付け情報だけは古いままに
        取り残される不具合があった(監視フォルダ名は正しいプロジェクト
        名になっているのに、Maya側の紐付け表示だけがテンプレート名や
        __unsaved__のままという矛盾として実機で確認された)。
        対策として、active_project_keyが "__unsaved__" の間はこの
        ボタンでの紐付けをブロックし、先にSP側で保存するよう案内する。
        """
        active_key = self.watcher.config.get("active_project_key")
        if not active_key:
            QtWidgets.QMessageBox.information(
                self, "Live Sync",
                "SP側で今開いているプロジェクトが確認できません。\n"
                "Substance Painterでプロジェクトを開いてから、もう一度お試しください。"
            )
            return
        if active_key == "__unsaved__":
            QtWidgets.QMessageBox.information(
                self, "Live Sync",
                "SP側のプロジェクトがまだ保存されていません。\n"
                "この状態で紐付けると「未保存」のままシーンに固定されてしまい、"
                "後で保存しても自動更新されません。\n"
                "SP側で一度「名前を付けて保存」してから、もう一度お試しください。"
            )
            return
        active_name = _project_display_name(active_key)
        scene_name = _scene_display_name()
        reply = QtWidgets.QMessageBox.question(
            self, "Live Sync",
            "このシーン「{0}」の対応先として\nSPプロジェクト「{1}」を設定します。"
            "よろしいですか？".format(scene_name, active_name),
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        ok = _set_current_scene_project_link(active_key)
        if ok:
            self.watcher._emit_status(
                "シーン「{0}」をSPプロジェクト「{1}」に紐付けました。".format(scene_name, active_name)
            )
        else:
            self.watcher._emit_status("紐付けの保存に失敗しました。")
        self._refresh_scene_link_label()

    def _on_other_session_changed(self, other):
        """他セッション警告の表示/非表示を切り替える。"""
        if other:
            confidence = "(推定)" if other.get("stale_guess") else ""
            self.other_session_label.setText(
                "他のMayaセッション(PID {0}){1}が同時に監視している可能性があります。"
                "実害はありませんが、身に覚えが無ければ削除できます。".format(
                    other.get("pid"), confidence
                )
            )
            self.other_session_label.setVisible(True)
            self.other_session_clear_btn.setVisible(True)
        else:
            self.other_session_label.setVisible(False)
            self.other_session_clear_btn.setVisible(False)

    def _on_clear_other_session(self):
        _force_clear_session_lock()
        self.watcher.other_session_info = None
        self._on_other_session_changed(None)
        self.watcher._emit_status("他セッションのロック情報を削除しました。")

    def _on_structure_changed(self):
        """known_texture_sets_by_project 等の動的設定が更新されたときに
        呼ばれる。一覧テーブルと最終更新時刻を再描画する。
        """
        self._refresh_material_table()
        self.last_structure_update_label.setText(
            "一覧の最終更新: {0}".format(_now())
        )

    def _on_scene_changed(self, *_args):
        """2026.07.15-01: MSceneMessage(kAfterOpen/kAfterNew)から呼ばれる。
        シーンが切り替わったら、直前のシーン向けの監視を安全に停止し、
        新しいシーンの状態(紐付け・一覧)でUIを更新する。監視の自動再開は
        行わない(紐付いたSPプロジェクトが無いまま再開すると、前のシーンの
        設定を引き継いでしまう危険があるため、ユーザーの一手を挟む)。
        """
        try:
            if self.watcher.enabled:
                self.watcher.stop(reason="scene_change")
                self.enable_btn.blockSignals(True)
                self.enable_btn.setChecked(False)
                self.enable_btn.setText("監視（自動反映）: OFF")
                self.enable_btn.blockSignals(False)
            self._refresh_scene_link_label()
            self._refresh_material_table()
        except Exception as e:
            # コールバック内で例外を外に漏らすとMaya本体が不安定になる
            # ことがあるため、ここでは握りつぶしてログにのみ残す。
            print("[maya_live_sync] _on_scene_changed でエラー: {0}".format(e))

    def closeEvent(self, event):
        """2026.07.15-01: ウィンドウが実際に閉じられる(workspaceControlごと
        破棄される)際に、登録済みのシーンコールバックを解除する。
        解除し忘れると、コールバックが解放済みのPythonオブジェクトを
        参照し続けてMaya側でエラーが出る可能性があるため。
        通常はワークスペースを隠すだけ(hide)でこのイベントは発生しない
        想定だが、念のため安全に倒す。
        """
        try:
            for cb_id in getattr(self, "_scene_callback_ids", []):
                try:
                    om.MMessage.removeCallback(cb_id)
                except Exception:
                    pass
            self._scene_callback_ids = []
        except Exception:
            pass
        super(LiveSyncWindow, self).closeEvent(event)

    def _on_quality_toggled(self, checked):
        switched = self.watcher.switch_texture_quality(checked)
        if switched:
            self.quality_btn.setText(
                "表示品質: {0}".format("高画質(Final)" if checked else "プレビュー(リアルタイム)")
            )
        elif checked:
            # 切り替えが1件も行われなかった場合、ボタンだけがONの
            # 見た目になって実態とズレるのを避けるため元に戻す。
            self.quality_btn.blockSignals(True)
            self.quality_btn.setChecked(False)
            self.quality_btn.blockSignals(False)

    def _on_save_clicked(self):
        new_values = {
            "watch_dir": self.watch_edit.text().strip(),
            "renderer": self.renderer_combo.currentText(),
        }
        if not new_values["watch_dir"]:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "監視フォルダが未指定です。")
            return
        self.watcher.apply_and_save_config(new_values)

    def open_setup_wizard(self):
        wizard = SetupWizard(self.watcher, self)
        if wizard.exec_() == QtWidgets.QDialog.Accepted:
            self._load_values_from_config()
            self._refresh_material_table()

    def _refresh_material_table(self):
        # 2026.07.16: _refresh_scene_link_label() と同じ理由で、
        # 監視OFFの間はディスク上の最新値がメモリに反映されない
        # 問題があったため、こちらでも明示的に読み直す。
        self.watcher._refresh_dynamic_config()
        self.active_project_label.setText(self.watcher.get_active_project_label())
        known = self.watcher.get_known_texture_sets()
        self.material_table.setRowCount(len(known))
        for row, name in enumerate(known):
            mapped, sg_name = self.watcher.is_texture_set_mapped(name)
            status = "対応済み" if mapped else "未対応"
            self.material_table.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            self.material_table.setItem(row, 1, QtWidgets.QTableWidgetItem(status))
            self.material_table.setItem(row, 2, QtWidgets.QTableWidgetItem(sg_name or "-"))

    def _on_create_shader_clicked(self):
        rows = sorted(set(idx.row() for idx in self.material_table.selectedIndexes()))
        if not rows:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "テクスチャセットの行を選択してください。")
            return
        channels = [suffix for suffix, cb in self.channel_checkboxes.items() if cb.isChecked()]
        created, failed = [], []
        for row in rows:
            item = self.material_table.item(row, 0)
            if item is None:
                continue
            name = item.text()
            try:
                self.watcher.create_shader_network(name, channels=channels)
                created.append(name)
            except Exception as e:
                failed.append("{0}: {1}".format(name, e))
        self._refresh_material_table()
        if created:
            self.watcher._emit_status("シェーダーを生成しました: {0}".format(", ".join(created)))
        if failed:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "生成に失敗しました:\n" + "\n".join(failed))

    def _refresh_orphan_list(self):
        self.orphan_list.clear()
        for node in self.watcher.find_orphan_file_nodes():
            self.orphan_list.addItem(node)

    def _on_stats_changed(self, stats):
        self.last_reload_label.setText(stats.get("last_reload_at") or "-")
        self.reload_count_label.setText(str(stats.get("reload_count", 0)))
        self.node_count_label.setText(str(stats.get("last_node_count", 0)))

    def _install_shelf_button(self):
        try:
            current_shelf = cmds.tabLayout("ShelfLayout", query=True, selectTab=True)
        except Exception:
            current_shelf = None

        if not current_shelf:
            QtWidgets.QMessageBox.warning(
                self, "Live Sync",
                "アクティブなshelfが見つかりませんでした。Mayaのshelf UIが表示された状態で実行してください。"
            )
            return

        command = "import maya_live_sync\nmaya_live_sync.show_ui()\n"
        try:
            cmds.shelfButton(
                parent=current_shelf,
                label="SPLiveSync",
                annotation="SP Live Sync ウィンドウを開く",
                image1="render_arnold.png",
                command=command,
                sourceType="python",
            )
            self.log_view.appendPlainText("[{0}] shelfボタンを設置しました: {1}".format(_now(), current_shelf))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "shelfボタンの設置に失敗しました: {0}".format(e))

    def _open_history_log(self):
        if not os.path.isfile(HISTORY_LOG_PATH):
            QtWidgets.QMessageBox.information(
                self, "Live Sync", "同期履歴はまだありません(監視を開始すると記録され始めます)。"
            )
            return
        try:
            os.startfile(HISTORY_LOG_PATH)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "履歴ファイルを開けませんでした: {0}\nパス: {1}".format(e, HISTORY_LOG_PATH))

    def _on_register_user_setup(self):
        reply = QtWidgets.QMessageBox.question(
            self, "Live Sync",
            "Maya起動時にこのウィンドウも自動的に開きますか?\n"
            "「いいえ」の場合、起動時にモジュールをimportするだけで\n"
            "ウィンドウは自動表示されません(shelfボタン等から手動で開けます)。",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        auto_open = (reply == QtWidgets.QMessageBox.Yes)
        ok, message = register_user_setup(auto_open=auto_open)
        self.log_view.appendPlainText("[{0}] {1}".format(_now(), message))
        if ok:
            QtWidgets.QMessageBox.information(self, "Live Sync", message)
        else:
            QtWidgets.QMessageBox.warning(self, "Live Sync", message)

# ---------------------------------------------------------------------------
# 外部公開API: userSetup.py や shelfボタンから呼ばれるエントリーポイント
# ---------------------------------------------------------------------------

def show_ui():
    global _window_instance
    print("[maya_live_sync] show_ui() called - version {0}".format(__version__))
    first_creation = False
    if _window_instance is None:
        _window_instance = LiveSyncWindow()
        first_creation = True
    try:
        _window_instance.show(dockable=True)
    except Exception:
        pass

    # 2回目以降の呼び出し(≒シェルフボタンの再クリック)で、過去に
    # ウィンドウを閉じた/隠した際にworkspaceControlがvisible=Falseの
    # まま残ることがある。MayaQWidgetDockableMixin の .show(dockable=True)
    # だけではこの状態から復帰しないことがあるため(Autodeskコミュニティ
    # でも報告されている既知の挙動)、workspaceControl コマンドで
    # 明示的に visible=True にして確実に表示させる。
    # 実機で、Shelfボタンを押しても何も起きない(エラーも出ない)ように
    # 見える不具合として確認された原因がこれだった。
    try:
        if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, query=True, exists=True):
            if not cmds.workspaceControl(WORKSPACE_CONTROL_NAME, query=True, visible=True):
                cmds.workspaceControl(WORKSPACE_CONTROL_NAME, edit=True, visible=True)
                print("[maya_live_sync] workspaceControl was hidden; forced visible=True.")
            # 最前面に持ってくる(タブの奥に隠れているだけのケースにも対応)。
            cmds.workspaceControl(WORKSPACE_CONTROL_NAME, edit=True, restore=True)
        # ウィジェット自身も前面化しておく(他タブの裏に隠れているケースの保険)。
        _window_instance.raise_()
    except Exception as e:
        print("[maya_live_sync] Could not force-show workspaceControl: {0}".format(e))

    if first_creation:
        if not _window_instance.watcher.config.get("setup_wizard_completed"):
            _window_instance.open_setup_wizard()
    return _window_instance


# ---------------------------------------------------------------------------
# 2026.07.15-01: Maya終了時のセッションロック解放
# ---------------------------------------------------------------------------
#
# 背景: 従来 _clear_session_lock_if_own() は「監視ボタンを手動でOFFに
# した時」にしか呼ばれず、Mayaをそのまま終了する(×ボタン/タスクマネージャ
# 終了/クラッシュ)ケースではロックファイルが残り続けていた。これが
# 「次回起動時に毎回、他セッション警告が出る」不具合の主因だった
# (_check_other_session() 側のpid生存確認・経過時間フォールバックと
# 合わせて、正常終了時はこちらで確実に解消することを狙う)。
#
# このコールバックはモジュールが import された時点(＝maya_live_sync が
# 一度でも使われた時点)で一度だけ登録する。show_ui() を呼んだかどうかに
# 関わらず登録するのは、監視を開始した後にウィンドウだけ閉じてMayaは
# そのまま使い続ける、といった操作でもロックが残っていること自体は
# 変わらないため。
def _on_maya_exiting(*_args):
    try:
        _clear_session_lock_if_own()
    except Exception:
        pass


# --- [DIAG-A3 / 2026.07.17 根治修正] ------------------------------------
# 経緯: このモジュールが import/reload されるたびに、下の
# addCallback が無条件で実行され、_on_maya_exiting が重複登録される
# ことを実機ログ(DIAG-A3計装)で確認した(再インストールのたびに
# 1, 2, 3... と累積)。_on_maya_exiting 自体は
# _clear_session_lock_if_own() が冪等なため直接の実害は確認されて
# いないが、将来この中身が変わった場合にN重実行の副作用を生む
# 構造的な問題として、予防的に対応する。
#
# 対策方針: 単純に「一度登録したらスキップ」にはしない。それだと
# reload() のたびに _on_maya_exiting という名前が指す関数オブジェクト
# 自体は新しく作り直されるのに、Maya側に登録済みなのは古い関数
# オブジェクトのままになり、「reloadしても古いコードのまま動く」という
# 今回調査してきた一連の不具合と同じパターンを新たに作ってしまう。
# そのため、reload のたびに「前回登録したコールバックIDが分かれば
# 先に解除し、そのうえで今回の(新しい)関数を登録し直す」方式にした。
# コールバックIDはモジュールグローバル _EXITING_CALLBACK_ID に保持し、
# globals() 経由で reload をまたいで引き継ぐ(DIAG-A3のカウンタ計装と
# 同じ仕組み)。
_EXITING_CALLBACK_ID = globals().get("_EXITING_CALLBACK_ID", None)

if _EXITING_CALLBACK_ID is not None:
    try:
        om.MSceneMessage.removeCallback(_EXITING_CALLBACK_ID)
        print("[maya_live_sync] Removed previous kMayaExiting callback (id={0}) before re-registering.".format(
            _EXITING_CALLBACK_ID))
    except Exception as _e:
        # 解除に失敗しても、古いコールバックが残ったまま新しいものが
        # 追加されるだけなので致命的ではない(従来と同じ状態に留まる)。
        print("[maya_live_sync] Could not remove previous exit callback (id={0}): {1}".format(
            _EXITING_CALLBACK_ID, _e))
    _EXITING_CALLBACK_ID = None

try:
    _EXITING_CALLBACK_ID = om.MSceneMessage.addCallback(om.MSceneMessage.kMayaExiting, _on_maya_exiting)
except Exception as _e:
    print("[maya_live_sync] Could not register exit callback: {0}".format(_e))
