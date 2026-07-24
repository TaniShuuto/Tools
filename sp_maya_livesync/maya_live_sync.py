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
import glob
import uuid
import base64
import datetime
import subprocess
import unicodedata
import contextlib
try:
    import msvcrt
except ImportError:
    # このツールはWindows専用設計(C:/SPMayaLiveSync のハードコード等)
    # だが、念のためimport失敗時は設定ファイルロックを無効化して
    # 動作は継続できるようにする(_config_file_lock()参照)。
    msvcrt = None

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


# --- [DIAG-B2] -------------------------------------------------------------
# B-2仮説(シーン⇔SPプロジェクト不一致による自動停止)の切り分け用ログの
# 表示/非表示切り替え。仮説の再現性確認自体はまだ済んでいないため計装は
# 残すが、通常運用時はエディタ/コンソールを圧迫しないよう既定でFalse
# (非表示)にしておく。再度切り分けが必要になった場合はTrueにする。
#
# 2026.07.24(診断ビルド 1.4.4.1、切り分け完了): 「fileノードは実在する
# のに『見つからない』とログが出て自動反映が無効化される」という報告の
# 切り分けのため、診断ビルド1.4.4.1(テストバージョン、x.x.x.x形式)
# 限定で既定Trueにして出荷した。実機ログでlinked_keys/active_keyの
# match=Trueが確認され、原因は別箇所(watch_subfolder_by_project等の
# プロジェクト別辞書の記録漏れ、実体はSP側プラグインのバージョン差異)と
# 判明したため、本来の既定値であるFalseに戻す。
_DIAG_B2_VERBOSE = False


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
# 2026.07.20: バージョン表記をセマンティックバージョニング(MAJOR.MINOR.PATCH)
# へ移行。ツール群として初めて正式にバージョン番号を割り当てる区切りとして
# 1.0.0 からスタートする(このコミット以前は日付ベースの独自表記
# "2026.07.17-02" だった。旧番号との対応はREADME.mdの「バージョン履歴」
# 節を参照)。以降は SemVer のルールに従う:
#   MAJOR: 設定ファイル形式の変更など、既存環境で互換性が崩れる変更
#   MINOR: 後方互換のある機能追加
#   PATCH: 後方互換のあるバグ修正
# 2026.07.20-02(SPプロジェクト自動追従): SP側で開いているプロジェクトが
#     切り替わった際、現在のシーンに登録済みのlinkの中に一致するものが
#     あれば、ドロップダウンの手動選択を待たずに active_link_id を自動的に
#     切り替えるようにした(_try_auto_switch_active_link())。
#     - 検知自体は既存の _refresh_dynamic_config()(project_poll_timer
#       経由、監視ON/OFFに関わらずウィンドウを開いていれば3秒間隔で常時
#       実行)にそのまま乗せており、新たなポーリングは追加していない。
#     - 未登録のSPプロジェクトを開いた場合は自動追加せず、従来通り
#       _refresh_scene_link_label() の不一致警告に委ねる(相談の結果、
#       この挙動を明示的に選択)。
#     - _set_active_link() は呼ぶたびに無条件で書き込みを行う(保存済み
#       シーンなら cmds.fileInfo() 経由でシーンを「変更あり」にマークする)
#       ため、切り替え先が現在のactive_link_idと同じ場合は呼ばないよう
#       ガードしている(3秒ごとに無駄な書き込みでシーンが常時未保存扱いに
#       なることを防ぐ)。
#     後方互換のある機能追加のため、SemVerのルールに従いMINORを上げる。
# 2026.07.21-01(Phase 1, 緊急修正: 複数SPプロジェクト並行対応の根治):
#     以下の症状が実機で報告され、原因調査の結果いずれも
#     「active_watch_subfolder/active_final_subfolder(SP側が今開いている
#     1プロジェクトのみを表すスカラー値)を追跡の起点にしている」という
#     共通の設計限界に起因すると判明した:
#       (1) 複数SPプロジェクトを動的に追跡できず、最初に設定したSP
#           プロジェクトのみを追跡している
#       (2) リアルタイム追従ができなくなっている(Finalのみ追従できる)
#       (3) Fileノードが存在するのに、一度Maya再起動しないとマテリアルを
#           認識しない
#     対策として、既存の texture_set_export_prefix_by_project 等と同じ
#     「project_key -> 値」のネスト辞書パターンを踏襲した新設定キー
#     watch_subfolder_by_project / final_subfolder_by_project を導入し、
#     監視・reload・マテリアル一覧・マッピング判定のすべてを「シーンに
#     紐付けられた全プロジェクトの集合」ベースに作り替えた。主な変更点:
#       - _active_watch_dir()/_active_final_dir()(単数形、代表1件のみ)
#         とは別に、_active_watch_dirs()/_active_final_dirs()(複数形、
#         全プロジェクト分の集合)を新設。監視登録・_process_pending_
#         changes()・reload_textures()/reload_final_textures()・
#         _managed_dirs()・detect_current_quality() はすべて複数形へ
#         差し替えた。単数形は後方互換フォールバックとaiSSのフォルダ欄
#         自動入力用の「代表1件」としての役割に限定した。
#       - _process_pending_changes() の完了flag判定を、単一のmtime比較値
#         からディレクトリ単位のmtime辞書(_flag_mtimes_watch/_final)へ
#         変更。あるプロジェクトの新しいflagが、別プロジェクトのmtimeとの
#         大小比較で「まだ古い」と誤判定される問題を解消した(症状(2)の
#         直接の修正箇所)。
#       - シーンに紐付いたSPプロジェクトとSP側の現在プロジェクトが
#         食い違う場合の安全防御(自動停止)ロジックを、「アクティブな
#         1linkとの比較」から「シーンに紐付いた“いずれかの”linkとの
#         比較」に修正。旧ロジックのままだと、複数プロジェクトを並行
#         紐付けている状態でSP側がプロジェクトを切り替えるたびに、
#         正しく紐付いているにもかかわらず監視全体が誤って停止する
#         欠陥があった(実装レビュー中に発見、複数プロジェクト運用の
#         根治と直接矛盾するため必須の修正として対応)。
#       - get_known_texture_sets()を「シーンに紐付いた全プロジェクトの
#         テクスチャセット名の和集合」を返すよう変更(論点2案A)。
#         同名テクスチャセットが複数プロジェクトに存在する場合の区別用に
#         get_known_texture_sets_detailed()を新設し、マテリアル構造タブに
#         「SPプロジェクト」列を追加した。is_texture_set_mapped()/
#         create_shader_network() は project_key 引数を新設し、同名
#         テクスチャセット間の取り違えを防いだ(症状(3)の修正箇所)。
#       - reload_textures()/reload_final_textures() の「古いサブフォルダを
#         参照したまま取り残されたノード」の補正先を、従来の「現在の1つの
#         サブフォルダ」から「ファイル名prefixから逆引きした、該当
#         プロジェクトのサブフォルダ」に変更(_match_known_texture_set_
#         project()を新設)。
#       - 設定ファイルの正規化キー空間の不一致(links側はスラッシュ統一済み、
#         SP側書き込みは未正規化)を _export_prefix()/get_shading_engine_
#         map()/save_shading_engine_mapping() 側で吸収するよう修正。
#       - load_config() に、旧単一値からby_project辞書への非破壊
#         マイグレーション(_migrate_legacy_active_subfolder())を追加。
#     設計上の注記: switch_texture_quality()(表示品質の手動切り替え
#     ボタン)は、シーン内の全fileノードを一括で1つの品質へ収束させる
#     既存仕様を維持するため、意図的に単数形(代表1件)のまま据え置いた。
#     複数プロジェクトが混在するシーンでプロジェクト単位の品質切り替えが
#     必要になった場合は、別途の機能追加として設計を検討すること。
#     症状(4)「Mayaのシーン切り替えでウィンドウが初期化されない」は
#     本バージョンの対象外(Phase 2で対応予定)。
#     設定ファイルへのキー追加のみで既存キーの意味は変えておらず、
#     後方互換を維持しているため、SemVerのルールに従いMINORを上げる。
# 2026.07.21-02(緊急バグ修正: シーン切替時に前の作業対象が残留する不具合):
#     1.2.0リリース後の実機報告で、「新しいMayaシーンに切り替えても、
#     マテリアル一覧・状態バーの作業対象表示が前のシーンのままになる」
#     不具合が確認された。原因は get_known_texture_sets()/
#     get_known_texture_sets_detailed()/get_active_project_label() が、
#     「このシーンにSPプロジェクトの紐付け(link)が1件も無い」場合の
#     後方互換フォールバックとして self.config["active_project_key"]
#     (SP側が今開いているプロジェクトを表すだけで、Mayaのシーンを
#     切り替えてもメモリ上に残り続ける値)を参照していたこと。
#     「紐付け未設定」と「シーンを切り替えて紐付けの文脈が変わった」を
#     区別できておらず、シーン切替直後は必ず一瞬 linked_keys が
#     0件になり得るため、そのたびに前のシーンの active_project_key へ
#     意図せず先祖返りしていた。
#     このフォールバックを廃止し、シーンにlinkが1件も無い場合は
#     空(一覧は空リスト、ラベルは「このシーンにはまだSPプロジェクトが
#     紐付けられていません」)を返すよう修正した。
#     あわせて、点検の過程で find_orphan_file_nodes() が同種の理由で
#     _export_prefix() をproject_key省略(active_project_key基準)で
#     呼んでおり、複数プロジェクト混在時に他プロジェクトのテクスチャ
#     セットを誤って孤立ノード扱いする恐れがあった不具合も発見し、
#     _match_known_texture_set_project() へ委譲する形に修正した。
#     いずれも重大なバグ修正であり後方互換を維持しているため、
#     SemVerのルールに従いPATCHを上げる。
# 2026.07.21-03(未保存シーンでの作業対象引き継ぎに関する案内を追加):
#     複数SPプロジェクトの紐付け情報は、保存済みシーンなら
#     cmds.fileInfo()(シーンファイル自体に紐付く)へ保存されるが、
#     未保存シーンにはファイル実体が無いため、代わりに共有設定ファイル
#     上の固定スロット last_scene_project_links["__unsaved__"] を経由する
#     (1.1.0から存在する設計)。この固定スロットは「今開いている未保存
#     シーンがどれか」を区別できないため、未保存シーンAで設定した作業
#     対象が、保存せずに別の未保存シーンBへ切り替えた際にも引き継がれて
#     しまう。これはMayaの未保存シーンに安定した識別子が存在しないという
#     構造的な限界であり、実装のバグではないと判断した(保存済みシーン
#     同士の切替は1.2.1時点で正しく動作することを確認済み)。
#     実機報告を受け、この限界そのものの解消(未保存シーンの個別識別)は
#     見送り、代わりにユーザーへの案内を追加した: _on_scene_changed()で、
#     切り替わった先が未保存シーンかつ、引き継がれた紐付けが実際に
#     存在する場合、ポップアップで状況を説明し、対処法(紐付けの設定し
#     直し、または保存によるシーンごとの区別の有効化)を案内する。
#     シーンコールバック内で直接モーダルダイアログを出すとMaya本体の
#     シーン読み込み処理をブロックする恐れがあるため、
#     QTimer.singleShot(0, ...)でイベントループへ処理を戻した直後に
#     表示するようにした。
#     後方互換のある機能追加のため、SemVerのルールに従いMINORを上げる。
# 2026.07.21-04(緊急バグ修正: 未保存シーン切替時のポップアップが毎回
#     表示される):
#     1.3.0で追加したポップアップ案内は、「未保存シーン用の共有スロット
#     (last_scene_project_links["__unsaved__"])に前の未保存シーンの
#     紐付けが残り続ける」という根本原因そのものには対処しておらず、
#     案内を出すだけに留めていた。そのため、新規シーン(New Scene)を
#     作成するたびに引き継ぎ自体は毎回発生し、ポップアップも毎回表示
#     され続けてしまう不具合が実機で確認された。
#     「新規シーン(New Scene)」は「白紙から始める」という明確な
#     ユーザー意図の操作であり、前の未保存シーンの紐付けを引き継ぐ
#     べき理由が無いため、kAfterNewを検知した際に共有スロットを
#     _clear_unsaved_scene_link_slot() で自動的にクリアするようにした
#     (kAfterOpen(既存ファイルを開いた)の場合も、次に未保存シーンへ
#     移った際に古い情報を拾わないよう同様にクリアする)。
#     これにより、新規シーンは常に「未設定」から始まるようになり、
#     引き継ぎ自体が起きなくなるため、ポップアップも(この経路では)
#     表示されなくなる。ポップアップ機構自体は、この自動クリアでは
#     対処しきれない残余の経路への保険として残している。
#     kAfterOpen/kAfterNewを区別する必要があるため、コールバック登録を
#     ラムダ経由でis_new_sceneを明示的に渡す方式に変更した。
#     重大なバグ修正であり後方互換を維持しているため、SemVerのルールに
#     従いPATCHを上げる。
#
# 2026.07.22(表示品質のプロジェクト別管理化):
#     複数のSPプロジェクトを1つのシーンで扱う運用で、表示品質切替
#     ボタンがシーン全体で1つのグローバルな品質しか持てず、選択中で
#     ない作業対象のノードまで巻き込んで切り替わり・再読込されて
#     しまう不具合を修正した。self.using_final_quality(単一bool)を
#     self.quality_by_project(project_key単位の辞書)へ置き換え、
#     switch_texture_quality()/detect_quality_by_project()を含む
#     関連関数をプロジェクト単位で動作するよう変更した。「▸ プロジェクト
#     連携」欄に、選択中の作業対象の現在の表示品質を示すラベルを追加した。
#     既存の設定ファイル・シーン紐付けとの互換性は維持しつつ、既定の
#     挙動(全体を1つの品質に揃える)が変わる後方互換の機能追加のため、
#     SemVerのルールに従いMINORを上げる。
#
# 2026.07.23(シーン再オープン時のUDIMプレビュー未生成対応):
#     シーンを閉じて開き直すと、UDIM(複数UVタイル)テクスチャを使っている
#     オブジェクトが未割り当てのようにグレー表示される不具合を修正した。
#     原因はViewport 2.0がUDIMのプレビュー画像をシーンを開いた直後には
#     自動生成しない仕様(_flush_viewport_cache()のコメント参照)である
#     一方、そのプレビュー再生成(generateAllUvTilePreviews)はこれまで
#     表示品質切替ボタンを押した時(_flush_with_settle()経由)にしか
#     呼ばれておらず、シーン読み込みコールバック(_on_scene_changed())側
#     には対応する処理が無かったこと。_on_scene_changed()内で
#     generateAllUvTilePreviewsのみを軽量に呼ぶようにした(cmds.ogs(
#     reset=True)相当の重いキャッシュ全体破棄はシーンを開いた直後には
#     不要と判断し、あえて呼んでいない)。既存の動作を壊さない不具合
#     修正のため、SemVerのルールに従いPATCHを上げる。
#
# 2026.07.23-02(UDIM対応の再修正: シーン再オープンでも直らない場合が
# あった件・Reference Editorでの参照読み込みにも対応):
#     上記のシーン再オープン対応が実機で効果を発揮しない場合があると
#     再報告された。原因は、kAfterOpen/kAfterNew等のシーンコールバックが
#     発火する時点ではMayaのシーン読み込み処理自体がまだ完了しきって
#     いない場合があり、その場でgenerateAllUvTilePreviewsを同期的に
#     呼んでも効果が無かったため。_on_scene_changed()内の別の警告
#     ポップアップが既に採用しているQTimer.singleShot(0, ...)による
#     遅延実行パターンに倣い、UDIMプレビュー再生成を独立した
#     _regenerate_udim_previews_deferred()へ切り出し、遅延実行に変更した。
#     あわせて、Reference Editorで別シーンから参照(reference)を持ち込んだ
#     場合も同じ症状(kAfterOpen/kAfterNewが発火しないため従来は救済
#     対象外だった)が報告されたため、kAfterCreateReference/
#     kAfterLoadReferenceからも同じ再生成処理を呼ぶようにした。
#     既存の動作を壊さない不具合修正のため、SemVerのルールに従い
#     PATCHを上げる。
#
# 2026.07.23-03(緊急修正: generateAllUvTilePreviewsが「プロシージャが
# 見つかりません」で失敗していた根本原因への対応):
#     上記07.23-02の対策後もUDIM不具合が直らないと再々報告され、
#     実機のMaya2027(mayapyバッチセッション)で直接検証した結果、
#     generateAllUvTilePreviews自体がMEL側で「プロシージャ
#     "generateAllUvTilePreviews" が見つかりません」というエラーで
#     失敗していたことを確認した。このprocは
#     others/generateUvTilePreview.mel で定義されているが、Maya起動直後
#     や特定のUI操作を経ていないタイミングでは自動ソースされておらず、
#     従来のコードは呼び出し失敗を例外として握りつぶしログに警告を
#     出すだけだったため、原因が長らく見えていなかった。
#     _flush_viewport_cache()と_regenerate_udim_previews_deferred()の
#     両方で、呼び出し前に明示的に
#     mel.eval('source "generateUvTilePreview.mel";') を実行するよう
#     修正し、Mayaの自動ロードタイミングに依存せず確実にproc定義済みの
#     状態にした(mayapyで単体動作を再現・修正確認済み)。既存の動作を
#     壊さない不具合修正のため、SemVerのルールに従いPATCHを上げる。
#
# 2026.07.23-04(根治修正: 上記3回の対策がいずれも効かなかった真因への
# 対応): 07.23/-02/-03のUDIM対策後も直らないとの再々報告を受け実機調査
# した結果、これら3回の対策は全て LiveSyncWindow.__init__() 内でしか
# 登録されないシーンコールバック(kAfterOpen等)に依存しており、
# LiveSyncWindow が一度も生成されていなければ丸ごと効かないという
# 共通の弱点があることが判明した。install.py の既定設定
# (register_user_setup(auto_open=False))では、userSetup.py は
# Maya起動時に maya_live_sync をimportするだけでshow_ui()を呼ばない
# ため、「Mayaを再起動 → LiveSyncパネルを開く前に対象シーンを開く」
# という通常の操作順序では、最初の kAfterOpen 発火時点でリスナーが
# 1つも登録されていなかった(uvTileProxyQuality属性のガード条件や
# SP側・udim_setup.py側は原因ではないことも実機・mayapyで確認済み)。
# 対策として、UDIMプレビュー再生成の実処理をモジュールレベル関数
# _regenerate_udim_previews() へ切り出し、LiveSyncWindow の生成有無に
# 関わらずモジュールimport時点で無条件にシーンコールバックを登録する
# ようにした(ファイル末尾の登録ブロック参照)。LiveSyncWindow側の
# 既存登録は冗長になるが実害が無いためそのまま残した。既存の動作を
# 壊さない不具合修正のため、SemVerのルールに従いPATCHを上げる。
#
# 2026.07.24(診断ビルド、1.4.4.1): 「fileノードは実在するのに『見つから
# ない』とログが出て自動反映が無効化される」という報告を受け、原因切り
# 分けのためだけの診断ビルド。_DIAG_B2_VERBOSE を既定でTrueにし、
# _ensure_active_dirs_watched() の不一致判定材料(linked_keys/
# active_key/一致結果)を毎回ログへ出すようにした。動作自体の変更は
# 無く、ログ出力を有効化しただけのため、SemVerのバージョン番号は
# 正式なPATCHではなく4桁目(ビルド識別用サフィックス)を追加するに
# 留めた。
#
# 【新ルール】バージョン表記の桁数について: この診断ビルドを機に、
# 「テスト/診断目的の暫定ビルドは x.x.x.x(4桁、末尾がビルド識別用
# サフィックス)」「通常のリリースは x.x.x(3桁、SemVer)」と表記を
# 使い分けることとした。4桁のバージョンはこのファイル・履歴ログ・
# エディタのタイトルバー等で一目で「検証用の暫定ビルドである」と
# 判別できるようにするためのもので、正式リリースにこの形式を使っては
# ならない。
#
# 2026.07.24(恒久修正一式、上記診断ビルドの原因判明後の本対応):
# 上記診断ビルドのログにより、B-2仮説(シーン⇔SPプロジェクトキーの
# 大文字小文字/Unicode正規化不足による不一致誤判定)は実機では発生して
# いないことを確認したため _DIAG_B2_VERBOSE を既定Falseへ戻した。
# その後、客観的な視点での不具合監査(5並列のコード監査)を実施し、
# 実際にトレースして再現条件まで特定できた不具合のうち高+中優先度の
# ものをまとめて修正した。詳細な経緯・個別の判断根拠は各修正箇所の
# コメントを参照。要旨:
#   - _migrate_legacy_flat_maps(): 兄弟関数_migrate_legacy_active_
#     subfolder()と同じ「by_project辞書が空の時のみ移行」ガードを追加し、
#     再実行のたびに他プロジェクトのデータを誤って上書きしうるバグを修正。
#   - _normalize_project_key_for_compare(): 大文字小文字・Unicode正規化
#     (NFC)を追加し、表記ゆれによる誤ったプロジェクト不一致判定を防止。
#   - _match_known_texture_set_project(): 最長一致方式に変更し、接頭辞が
#     互いに包含関係にあるプロジェクト同士の誤判定を防止。
#   - reload_textures()/reload_final_textures(): 古いサブフォルダの
#     パス補正に実在チェック(UDIM対応)を追加し、存在しないパスへの
#     誤った書き換えを防止。
#   - switch_texture_quality(): 一部ノードの切り替えに失敗した場合、
#     品質フラグを誤って「完了」にしないよう修正。
#   - show_ui()/_window_instance: shiboken生死判定を追加し、破棄済み
#     ウィンドウへの無反応呼び出しを解消。reload時にインスタンス単位の
#     シーンコールバックがリークする不具合も、モジュールレベルの
#     _EXITING_CALLBACK_IDと同じglobals()保持パターンで修正。
#   - save_config(): msvcrt.lockingによるOSレベルのファイルロックを
#     追加し、Maya側・SP側の同時書き込みによるlost updateを防止
#     (プロセスクラッシュ時もOSが自動的にロックを解放するため、
#     ロックの固着は発生しない設計であることを実機テストで確認済み)。
# いずれも既存の動作を壊さない不具合修正のため、SemVerのルールに従い
# PATCHを上げる。
#
# 2026.07.24-02(緊急バグ修正: 初回同期の取りこぼし、5並列の不具合監査で
# 特定): ユーザーから「一発で同期されない(SP側で保存/エクスポートした
# 直後は反映されず、もう一度操作すると反映される)」という報告があり、
# 4並列(観点: Maya側監視ロジック/SP側エクスポートトリガー/SP⇔Maya
# ハンドシェイク/ファイルI/O原子性)+2並列(ダブルチェック)の計6回の
# コード監査で原因を特定した。
# 根本原因: _ensure_active_watch_watched()/_ensure_active_final_watched()
# (新規プロジェクトのサブフォルダをproject_poll_timer経由で発見した際)
# および start()(監視ON時)は、新規フォルダを self.fs_watcher.addPath()
# で登録するだけで、登録時点で既に存在するファイル・_sync_complete.flag
# を一切スキャンしていなかった。QFileSystemWatcherのdirectoryChanged
# シグナルはaddPath()「後」に発生した変更にしか反応しない仕様のため、
# SP側の書き出し(flag書き込みまで完了)がMaya側のフォルダ登録より先に
# 完了していた場合、その回の同期は検知されず、次にSP側が同じフォルダへ
# 書き込んで初めてdirectoryChangedが発火するまで永久に取りこぼされて
# いた。
# 対策として、_ensure_active_dirs_watched()の末尾と start()の
# addPath()完了直後に、それぞれ self._process_pending_changes() を
# 明示的に1回呼ぶようにした。この関数は呼ばれるたびに監視中の全
# アクティブフォルダのflag(_sync_complete.flag)のmtimeを再走査して
# 判定する設計のため、directoryChanged以外から呼んでも安全(冪等)で
# あり、新規addPath直後の「登録前に既に起きていた変更」を即座に
# キャッチアップできる。既存の動作を壊さない不具合修正のため、SemVer
# のルールに従いPATCHを上げる。
__version__ = "1.4.6"

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
    # 2026.07.21(Phase 1, 複数プロジェクト並行対応の根治):
    # active_watch_subfolder / active_final_subfolder はスカラー値の
    # ため「SP側が今開いている1プロジェクト」しか表現できず、複数の
    # SPプロジェクトを同一シーンに紐付けて並行作業すると、後から開いた
    # 方の値で前の方が上書きされてしまい、Mayaが同時に追跡できる
    # プロジェクトが実質1つに限定される不具合があった(このリポジトリで
    # 報告された緊急バグの根本原因)。texture_set_export_prefix_by_project
    # と同じ「project_key -> サブフォルダ名」のネスト辞書をSP側が新たに
    # 記録するようになったため、Maya側もこちらを正として複数プロジェクトを
    # 並行追跡する。上記の active_watch_subfolder / active_final_subfolder
    # (スカラー値)は、未対応の古いSP側との後方互換フォールバック、および
    # aiSSのフォルダ欄自動入力用の「代表1件」の値として引き続き利用する。
    "watch_subfolder_by_project": {},
    "final_subfolder_by_project": {},
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

    2026.07.24(緊急バグ修正): 従来はby_project辞書の中身に関わらず
    load_config()のたびに無条件で移行を試みていたが、これは
    _migrate_legacy_active_subfolder()で既に発見・修正済みの
    バグと同じクラスだった。「1回限りの移行」を意図したコードが
    毎回実行されるため、レガシーのフラットキー(texture_set_export_
    prefix等)が(何らかの理由で)残ったままだと、その時点でたまたま
    アクティブなプロジェクトへ、guessした(不正確な可能性がある)
    値を毎回上書きし続けてしまう。_migrate_legacy_active_subfolder()
    と同じ方針(by_project辞書が完全に空の場合、＝まだ一度もこの
    形式が使われていない真にレガシーな設定ファイルの場合にのみ
    移行する)に揃え、一度でも実際のexportでby_project辞書に何らかの
    内容が記録された後は、レガシーのフラットキーを移行元として
    信頼しないようにした。
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
    if legacy_prefix and not prefix_by_project:
        for name, prefix in legacy_prefix.items():
            key = _guess_project_key(name)
            if not key:
                continue
            prefix_by_project.setdefault(key, {})
            prefix_by_project[key][name] = prefix
        cfg["texture_set_export_prefix_by_project"] = prefix_by_project

    sg_by_project = dict(cfg.get("texture_set_shading_engine_map_by_project", {}))
    if legacy_sg and not sg_by_project:
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


def _migrate_legacy_active_subfolder(cfg):
    """2026.07.21(Phase 1, 複数プロジェクト並行対応の根治):
    旧形式(active_watch_subfolder / active_final_subfolder のスカラー値)
    しか記録されていない設定ファイルを、新形式(watch_subfolder_by_project /
    final_subfolder_by_project のネスト辞書)へ非破壊で移行する。

    _migrate_legacy_flat_maps() と同じ「読み取り時に変換 -> 呼び出し側が
    必要なら保存で確定」パターンを踏襲する。

    移行方針: 旧スカラー値は「その時点でSP側がアクティブだったプロジェクト
    (active_project_key)のサブフォルダ」だったはずなので、by_project辞書に
    その1エントリとして書き込む。active_project_key が無い(未対応の
    さらに古い設定、または一度もSPと接続していない)場合は、対応付けようが
    ないため移行せず、by_project辞書は空のまま(新しいexportが行われた
    タイミングでSP側が正しく埋める)にフォールバックする。

    既にby_project辞書に何らかの内容がある場合は、旧スカラー値による
    上書きは行わない(新形式のほうが情報として優先されるべきであり、
    旧スカラー値は既に古い可能性があるため)。

    この関数は破壊的にcfgを書き換えず、移行後のcfgを新たに返す。
    移行が発生した場合は cfg["_migrated_active_subfolder"] = True を立てる
    (呼び出し側が保存要否を判断できるようにするための内部マーカー)。
    """
    legacy_watch = cfg.get("active_watch_subfolder")
    legacy_final = cfg.get("active_final_subfolder")
    if not legacy_watch and not legacy_final:
        return cfg

    active_key = cfg.get("active_project_key")
    if not active_key:
        # 対応付けるプロジェクトキーが分からないため、安全側に倒して
        # 移行しない(次回SP側がexportした際に by_project 辞書へ
        # 正しく記録されるのを待つ)。
        return cfg

    migrated = False

    # 2026.07.23(緊急バグ修正): 従来は「active_key が by_project辞書に
    # まだ無ければ」を条件に移行していたが、これは「1回限りの移行」を
    # 意図したコードであるにもかかわらず load_config() のたびに実行される
    # ため、複数のSPプロジェクトを行き来する運用では、まだ一度も自分自身の
    # exportを行っていない新しいプロジェクトがアクティブになるたびに、
    # その時点でたまたま残っていた「直前にアクティブだった別プロジェクト
    # 由来」のスカラー値を誤って書き込んでしまっていた。
    # 実機で、"Poker_Table.spp" と "Poker_Table_StandUnder.spp" という
    # 別々のプロジェクトが、by_project辞書上で全く同じサブフォルダ名を
    # 指してしまう(後者のexport結果を前者が誤って引き継ぐ)形で
    # このデータ汚染が確認された。
    # 本来の「レガシー設定ファイルからの1回限りの移行」という意図に
    # 忠実に、by_project辞書が完全に空の場合(＝まだ一度もこの形式が
    # 使われていない、真にレガシーな設定ファイル)にのみ移行するよう
    # 修正した。一度でも実際のexportでby_project辞書に何らかの内容が
    # 記録された後は、スカラー値は「直近にどれかのプロジェクトが
    # exportした一時的な値」でしかなく、任意の別プロジェクトへの
    # 移行元として信頼できないため。
    watch_by_project = dict(cfg.get("watch_subfolder_by_project", {}))
    if legacy_watch and not watch_by_project:
        watch_by_project[active_key] = legacy_watch
        cfg["watch_subfolder_by_project"] = watch_by_project
        migrated = True

    final_by_project = dict(cfg.get("final_subfolder_by_project", {}))
    if legacy_final and not final_by_project:
        final_by_project[active_key] = legacy_final
        cfg["final_subfolder_by_project"] = final_by_project
        migrated = True

    if migrated:
        cfg["_migrated_active_subfolder"] = True
    return cfg


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(loaded)
        cfg = _migrate_legacy_flat_maps(cfg)
        cfg = _migrate_legacy_active_subfolder(cfg)
        save_partial = {}
        if cfg.pop("_migrated_legacy_maps", False):
            save_partial["texture_set_export_prefix_by_project"] = cfg["texture_set_export_prefix_by_project"]
            save_partial["texture_set_shading_engine_map_by_project"] = cfg["texture_set_shading_engine_map_by_project"]
            save_partial["texture_set_export_prefix"] = {}
            save_partial["texture_set_shading_engine_map"] = {}
        if cfg.pop("_migrated_active_subfolder", False):
            save_partial["watch_subfolder_by_project"] = cfg["watch_subfolder_by_project"]
            save_partial["final_subfolder_by_project"] = cfg["final_subfolder_by_project"]
        if save_partial:
            # 移行結果をディスクへ書き戻す(次回以降は移行処理をスキップ
            # できるようにするため)。保存に失敗しても致命的ではないので
            # 例外は握りつぶす。
            try:
                save_config(save_partial)
            except Exception:
                pass
        return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# 2026.07.24: 設定ファイルの同時書き込み競合対策(ファイルロック)
# ---------------------------------------------------------------------------
#
# 背景: save_config() は「ディスク上の最新内容を読み込む -> 呼び出し元の
# 変更をマージ -> 一時ファイル経由で書き戻す」という読み取り〜書き込み
# 区間を持つが、この区間自体はロックされていなかった。Maya側・SP側
# (別プロセス)がほぼ同時にこの区間を実行すると、後から書き込んだ側が
# 「自分が読んだ後に相手が書いた差分」を認識できず、その差分を丸ごと
# 巻き戻してしまう(lost update)。実機で、watch_subfolder_by_project
# 辞書のエントリが片方の保存で消えるという形でこの実害を確認している。
#
# 対策方針: 専用のロックファイル(CONFIG_PATH + ".lock")に対して
# msvcrt.locking() でOSレベルの排他ロックを取得し、この区間全体を
# 挟む。これは「ロックファイルが存在するかどうか」を自前でチェックする
# 素朴な実装(セッションロックで使っている方式)とは異なり、開いた
# ファイルディスクリプタに対するOS自体のロックのため、ロック保持中に
# プロセスがクラッシュしてもOSがプロセス終了時に自動的に解放する
# (スタックしたロックが残り続けて以後の保存が全て止まる、という
# 事態が起こらない)。
# ロック取得はノンブロッキング(LK_NBLCK)でポーリングしつつ、
# タイムアウト(既定3秒)を設ける。相手側がロックを長時間離さない
# 異常事態でもMaya/SPのUIスレッドを無期限にブロックしないためで、
# タイムアウトした場合は _ConfigLockTimeout を送出し、呼び出し元
# (save_config())が「ロック無しで今回の保存だけ進める」フォールバックを
# 選べるようにする。
CONFIG_LOCK_PATH = CONFIG_PATH + ".lock"
_CONFIG_LOCK_TIMEOUT_SECONDS = 3.0
_CONFIG_LOCK_POLL_INTERVAL_SECONDS = 0.05


class _ConfigLockTimeout(Exception):
    """設定ファイルロックの取得がタイムアウトした場合に送出する。"""
    pass


@contextlib.contextmanager
def _config_file_lock():
    """live_sync_config.json の読み取り〜書き込み区間を、Maya側・SP側
    双方から排他制御するためのコンテキストマネージャ。sp_live_sync_
    plugin.py側にも同名の仕組みを独立実装しており、ロックファイルの
    パス・タイムアウト値を完全に一致させている(共有モジュールが無い
    構成のため、ロジック自体はやむを得ず重複実装)。
    """
    if msvcrt is None:
        # importに失敗する環境(Windows専用設計のため通常は起こらない)
        # では排他制御自体を諦め、従来通りロック無しで進める。
        yield
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    lock_file = open(CONFIG_LOCK_PATH, "a+b")
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            # msvcrt.locking() がロックするバイト範囲を確保するため、
            # 空ファイルの場合は1バイトだけ書いておく。
            lock_file.write(b"0")
            lock_file.flush()
        deadline = time.time() + _CONFIG_LOCK_TIMEOUT_SECONDS
        locked = False
        while True:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.time() >= deadline:
                    break
                time.sleep(_CONFIG_LOCK_POLL_INTERVAL_SECONDS)
        if not locked:
            raise _ConfigLockTimeout(
                "設定ファイルロックの取得がタイムアウトしました: {0}".format(CONFIG_LOCK_PATH))
        try:
            yield
        finally:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
    finally:
        lock_file.close()


def _merge_and_write_config(cfg):
    """ディスク上の最新内容を読み込んでcfgをマージし、一時ファイル経由で
    原子的に書き戻す(save_config()の実処理本体、ロック取得の成否に
    関わらず共通で使う)。
    """
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


def save_config(cfg):
    """設定を保存する(監視の再起動は行わない、副作用の無いバージョン)。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with _config_file_lock():
            _merge_and_write_config(cfg)
    except _ConfigLockTimeout as e:
        # 2026.07.24: ロックが取得できなかった場合、UIスレッドを止めない
        # ことを優先し、ロック無し(従来の挙動)で今回の保存を進める。
        # 呼び出し元は変更したキーだけを渡す設計のため、1回このフォール
        # バックを踏んでも実害は小さい。
        print("[maya_live_sync] {0} ロック無しで保存を続行します。".format(e))
        _merge_and_write_config(cfg)


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

# 2026.07.19-03(複数SPプロジェクト対応、フェーズ1): 1シーンにつき
# SPプロジェクトを1つしか紐付けられなかった従来の制限を撤廃するための
# 新キー。天板/脚のようにオブジェクトごとにSPプロジェクトを分けている
# 作例では、作業対象を切り替えるたびに旧キー(_SCENE_LINK_FILEINFO_KEY)を
# 上書きする必要があり、直前の紐付けが失われてしまっていた。
#
# 新形式は「複数のlinkのリスト + 現在アクティブなlinkのid」を持つ:
#   {
#     "links": [
#       {"id": "link_xxxxxx", "sp_project_key": "...", "label": "天板",
#        "bound_nodes": []},
#       ...
#     ],
#     "active_link_id": "link_xxxxxx"
#   }
#
# "bound_nodes" は現時点では常に空配列(未使用)。将来「Maya側で選択した
# オブジェクトから対応するSPPを自動判定する」拡張を行う際に、この
# データ構造自体を作り直さずに済むよう、フィールドだけ先に確保している。
#
# 新旧キーは別名のため共存可能。旧キーのみが存在する古いシーンを開いた
# 場合は _migrate_legacy_scene_link() が読み取り時に非破壊で新形式へ
# 変換する(実際にシーンへ書き戻すのは次回保存時)。
_SCENE_LINKS_FILEINFO_KEY = "sp_live_sync_project_links"


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

    2026.07.24(見落とし修正): スラッシュ区切りの統一しか行っておらず、
    (1) Windowsのドライブレター・パスは大文字小文字を区別しないにも
    かかわらず文字列としては区別してしまう、(2) 日本語パスがNFC/NFD
    いずれの正規化形式で渡ってくるかはOS・入力元次第で保証が無く、
    見た目は同じでも文字列としては不一致になりうる、という2点を
    吸収できていなかった。実機で「シーンに紐付いたSPプロジェクトと
    SP側が今開いているプロジェクトが一致しない」という誤判定・
    「fileノードは実在するのに監視対象から漏れる」という誤判定の
    両方が、この正規化不足に起因する可能性がある(表記ゆれにより
    本来同一のプロジェクトキーが別物として比較されるため)。
    この関数の戻り値は比較専用(辞書のキーとして一時的に組み直す
    用途を含む)であり、共有設定ファイル上の生のキー自体を書き換える
    ものではないため、Unicode正規化(NFC)と大文字小文字の統一
    (casefold)をここに追加しても、呼び出し元が既に両辺をこの関数
    経由で比較している限り安全(ディスク上のキー形式には影響しない)。
    """
    if not key:
        return key
    normalized = key.replace("\\", "/")
    normalized = unicodedata.normalize("NFC", normalized)
    return normalized.casefold()


def _generate_link_id():
    """新規linkのための短いID("link_xxxxxx")を発行する。

    衝突確認は行わない: 1シーンに同時に持つlink数はごく少数
    (作例のオブジェクト分割数程度)であり、6桁の16進数(約1677万通り)で
    実運用上の衝突確率は無視できる。万一衝突しても、次にlinkを追加する
    タイミングで別のIDが振られるだけで実害は無い。
    """
    return "link_{0}".format(uuid.uuid4().hex[:6])


def _empty_scene_links_payload():
    """linkが1件も無い状態のペイロードを返す(新規シーン等)。"""
    return {"links": [], "active_link_id": None}


def _migrate_legacy_scene_link(legacy_key):
    """2026.07.19-03より前の単一キー形式(_SCENE_LINK_FILEINFO_KEY /
    last_scene_project_links の文字列値)を、新links形式へ変換する。

    非破壊: 呼び出し元が新形式を明示的に保存しない限り、旧キー自体は
    そのまま残る(ロールバック時の保険、および万一の旧バージョン混在
    時の最低限の動作を維持するため)。

    legacy_key が空/Noneの場合は空のペイロードを返す。
    """
    normalized = _normalize_project_key_for_compare(legacy_key)
    if not normalized:
        return _empty_scene_links_payload()

    link = {
        "id": _generate_link_id(),
        "sp_project_key": normalized,
        "label": "(移行済み) {0}".format(_project_display_name(normalized) or normalized),
        "bound_nodes": [],
    }
    return {"links": [link], "active_link_id": link["id"]}


def _get_scene_project_links(_diag=None):
    """現在Mayaで開いているシーンに紐付けられた、複数SPプロジェクトの
    link一覧とアクティブlinkのidを返す。

    戻り値: {"links": [...], "active_link_id": "..." or None}
    新形式のfileInfo/共有設定に情報が無い場合は、旧形式(単一キー)から
    自動的に移行して返す(シーンへの書き戻しはここでは行わない。
    呼び出し元が明示的に _set_scene_project_links() を呼ぶまでは
    ディスク上は旧形式のままで良い)。

    2026.07.19-04(緊急修正): 当初はJSON文字列をそのまま
    cmds.fileInfo() の値として渡していたが、fileInfoはMEL文字列として
    保存する際に独自のエスケープ処理を行うため、ダブルクォートを
    大量に含むJSON文字列(1リンクあたり4組、複数リンクでは10組以上)を
    往復させると値が破損し、書き込み直後のクエリでも
    json.loads() が失敗する不具合があった(2026.07.16の
    _normalize_project_key_for_compare と同種、バックスラッシュではなく
    ダブルクォートで再発したケース)。
    _set_scene_project_links() 側でBase64エンコードしてから
    fileInfoへ渡すようにしたため、ここではデコードしてから
    json.loads() する。ASCII文字のみのBase64文字列はfileInfoの
    エスケープと衝突しないため、往復での破損が起きない。

    _diag: 呼び出し元がUI状態ログに「壊れた値からのフォールバックが
    発生したかどうか」を表示できるようにするための、書き込み可能な
    dict(任意)。渡された場合、フォールバックが発生すると
    _diag["fallback"] = True をセットする。モジュールレベル関数から
    は self._emit_status() を直接呼べない(LiveSyncWatcherインスタンス
    に紐付いていないため)ため、この形で呼び出し元に伝播する。
    """
    try:
        scene_path = cmds.file(query=True, sceneName=True)
    except Exception:
        scene_path = ""

    if scene_path:
        try:
            info = cmds.fileInfo(_SCENE_LINKS_FILEINFO_KEY, query=True)
        except Exception:
            info = None
        if info and info[0]:
            payload = _decode_scene_links_fileinfo_value(info[0])
            if payload is not None:
                return payload
            # デコード/パース失敗時。サイレントに旧形式へフォールバック
            # すると、既にlinksを追加したはずなのに毎回「見つからない」
            # ことになり、原因不明のまま同じ不具合を再発させてしまう。
            # Mayaコンソールへの警告に加え、_diagで呼び出し元(UI)にも
            # 伝える。
            try:
                om.MGlobal.displayWarning(
                    "SP Live Sync: シーンの複数SPプロジェクト情報の読み取りに"
                    "失敗しました(壊れた値の可能性)。旧形式からの復元を試みます。"
                )
            except Exception:
                pass
            if isinstance(_diag, dict):
                _diag["fallback"] = True

        # 新形式が無い/壊れている場合は旧形式から移行する。
        try:
            legacy_info = cmds.fileInfo(_SCENE_LINK_FILEINFO_KEY, query=True)
        except Exception:
            legacy_info = None
        legacy_value = legacy_info[0] if legacy_info else None
        return _migrate_legacy_scene_link(legacy_value)

    # 未保存シーン: 共有設定ファイル側のセッション内フォールバックを見る。
    cfg = load_config()
    all_links = cfg.get("last_scene_project_links", {})
    entry = all_links.get("__unsaved__")
    if isinstance(entry, dict) and "links" in entry:
        validated = _validate_scene_links_payload(entry)
        if validated is not None:
            return validated
        if isinstance(_diag, dict):
            _diag["fallback"] = True
    # 旧形式(文字列)からの移行、またはエントリ自体が無い/壊れている場合。
    return _migrate_legacy_scene_link(entry if isinstance(entry, str) else None)


def _encode_scene_links_fileinfo_value(payload):
    """payload(dict)をJSON化した上でBase64(ASCII)エンコードして返す。

    fileInfoの値をダブルクォート/日本語混じりのJSON文字列そのままに
    しないための変換。base64.b64encodeの出力は英数字+"+/="のみで、
    fileInfoのエスケープと衝突する文字を一切含まない。
    """
    raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _validate_scene_links_payload(payload):
    """{"links": [...], "active_link_id": ...} 形式のpayloadを検証し、
    壊れた要素を取り除いた安全な形で返す。

    トップレベルの形だけでなく、links配列の各要素が辞書で
    id/sp_project_keyを持つかも検証する。手動でのシーン/設定ファイル
    編集や、将来のバージョン間の非互換なデータが紛れ込んだ場合、
    _find_link()等が link.get(...) を呼んだ時点でAttributeErrorを
    起こし、状態バーの描画自体が止まってしまう(例外がUIスレッドに
    伝播するとウィンドウ操作不能になりうる)ため、ここで壊れた要素
    だけを除外し、機能全体を止めないようにする。

    トップレベルの形自体が不正(dictでない、"links"が無い等)な場合は
    Noneを返す。
    """
    if not isinstance(payload, dict) or "links" not in payload:
        return None

    raw_links = payload.get("links")
    if not isinstance(raw_links, list):
        return None

    valid_links = []
    for link in raw_links:
        if not isinstance(link, dict):
            continue
        if not link.get("id") or not link.get("sp_project_key"):
            continue
        valid_links.append({
            "id": link.get("id"),
            "sp_project_key": link.get("sp_project_key"),
            "label": link.get("label") or "",
            "bound_nodes": link.get("bound_nodes") if isinstance(link.get("bound_nodes"), list) else [],
        })

    active_link_id = payload.get("active_link_id")
    if active_link_id is not None and not any(l["id"] == active_link_id for l in valid_links):
        # アクティブとして記録されていたIDが有効なlink群に無い場合
        # (上の検証で除外された、または元データが壊れていた場合)、
        # 残ったlinkの先頭にフォールバックする。
        active_link_id = valid_links[0]["id"] if valid_links else None

    return {"links": valid_links, "active_link_id": active_link_id}


def _decode_scene_links_fileinfo_value(encoded_value):
    """_encode_scene_links_fileinfo_value() の逆変換。

    デコードまたはJSONパースに失敗した場合はNoneを返す(例外を外に
    漏らさない。呼び出し元が旧形式へのフォールバックを判断できる
    ようにするため)。構造検証は _validate_scene_links_payload() に
    委譲する。
    """
    if not encoded_value:
        return None
    try:
        raw = base64.b64decode(encoded_value.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return _validate_scene_links_payload(payload)


def _set_scene_project_links(payload):
    """_get_scene_project_links() と対になる書き込み関数。

    payload は {"links": [...], "active_link_id": "..." or None} 形式。
    保存済みシーンなら新形式のfileInfoキーへ(次回シーン保存時に
    ファイルへ永続化される)、未保存シーンなら共有設定ファイルの
    last_scene_project_links["__unsaved__"] へ書き込む。

    2026.07.16の教訓(_normalize_project_key_for_compare 参照)を踏まえ、
    各link内のsp_project_keyは保存前に必ず正規化する。

    2026.07.19-04(緊急修正): fileInfoへの書き込みはJSON文字列を直接
    渡さず、Base64エンコードしたASCII文字列を渡す
    (_get_scene_project_links の同日コメント参照)。
    """
    normalized_links = []
    for link in payload.get("links", []):
        normalized_links.append({
            "id": link.get("id") or _generate_link_id(),
            "sp_project_key": _normalize_project_key_for_compare(link.get("sp_project_key")),
            "label": link.get("label") or "",
            "bound_nodes": list(link.get("bound_nodes", [])),
        })
    normalized_payload = {
        "links": normalized_links,
        "active_link_id": payload.get("active_link_id"),
    }

    try:
        scene_path = cmds.file(query=True, sceneName=True)
    except Exception:
        scene_path = ""

    if scene_path:
        try:
            cmds.fileInfo(_SCENE_LINKS_FILEINFO_KEY, _encode_scene_links_fileinfo_value(normalized_payload))
            return True
        except Exception:
            return False

    cfg = load_config()
    all_links = dict(cfg.get("last_scene_project_links", {}))
    all_links["__unsaved__"] = normalized_payload
    save_config({"last_scene_project_links": all_links})
    return True


def _clear_unsaved_scene_link_slot():
    """共有設定ファイル上の last_scene_project_links["__unsaved__"]
    (未保存シーン用の共有スロット、_get_scene_project_links()/
    _set_scene_project_links() 参照)を空にする。

    2026.07.21 追加(緊急、実機報告): このスロットは「今開いている
    未保存シーンがどれか」を一切区別しない、Mayaプロセス全体で共有の
    固定キーであるため、未保存シーンAで設定した紐付けが、保存せずに
    別の未保存シーンBへ切り替えた際にもそのまま残り続けてしまう。
    特に「新規シーン(New Scene)」は「白紙から始める」という明確な
    ユーザー意図の操作であり、前の未保存シーンの紐付けを引き継ぐべき
    理由が無いため、_on_scene_changed() が kAfterNew を検知した際に
    この関数を呼び、スロットを毎回リセットする。
    保存済みファイルを開いた場合(kAfterOpen)も、次に未保存シーンへ
    移った際に古い情報を拾わないよう、念のため同様にクリアする。
    """
    cfg = load_config()
    all_links = dict(cfg.get("last_scene_project_links", {}))
    if "__unsaved__" not in all_links:
        return
    del all_links["__unsaved__"]
    save_config({"last_scene_project_links": all_links})


def _find_link(payload, link_id):
    """payload["links"]の中からidが一致するlinkを返す(無ければNone)。"""
    for link in payload.get("links", []):
        if link.get("id") == link_id:
            return link
    return None


# 2026.07.19-05: 1シーンに保持できるlink数の上限。fileInfoの値自体に
# 明文化された長さ制限は無いが、Maya ASCII保存時に巨大な文字列属性が
# クラッシュを誘発する既知の問題が報告されており、念のため現実的な
# 運用(作例のオブジェクト分割数はせいぜい数個〜10個程度)を大きく
# 超える異常な蓄積を防ぐガードを設ける。上限に達した場合は追加を
# 拒否し、不要なlinkを削除するよう促す。
_MAX_SCENE_PROJECT_LINKS = 30


def _add_scene_project_link(sp_project_key, label=None):
    """現在のシーンに新しいlinkを追加し、追加したlinkをアクティブにする。

    既に同じsp_project_key(正規化後)のlinkが存在する場合は、重複追加
    せずにそのlinkをアクティブにするだけに留める(「＋追加」ボタンを
    連打しても増殖しないようにするため)。

    戻り値: 追加(または既存採用)されたlinkのdict。上限超過で追加でき
    なかった場合は None。
    """
    normalized_key = _normalize_project_key_for_compare(sp_project_key)
    payload = _get_scene_project_links()

    existing = None
    for link in payload.get("links", []):
        if link.get("sp_project_key") == normalized_key:
            existing = link
            break

    if existing is not None:
        payload = dict(payload)
        payload["active_link_id"] = existing["id"]
        _set_scene_project_links(payload)
        return existing

    if len(payload.get("links", [])) >= _MAX_SCENE_PROJECT_LINKS:
        return None

    new_link = {
        "id": _generate_link_id(),
        "sp_project_key": normalized_key,
        "label": label or _project_display_name(normalized_key) or "",
        "bound_nodes": [],
    }
    payload = dict(payload)
    payload["links"] = list(payload.get("links", [])) + [new_link]
    payload["active_link_id"] = new_link["id"]
    _set_scene_project_links(payload)
    return new_link


def _remove_scene_project_link(link_id):
    """指定したlinkをシーンの紐付けから削除する。

    削除対象がアクティブlinkだった場合、残ったlinkの先頭を新たに
    アクティブにする(残りが無ければ active_link_id は None になる)。
    """
    payload = _get_scene_project_links()
    remaining = [link for link in payload.get("links", []) if link.get("id") != link_id]

    new_active = payload.get("active_link_id")
    if new_active == link_id:
        new_active = remaining[0]["id"] if remaining else None

    _set_scene_project_links({"links": remaining, "active_link_id": new_active})


def _set_active_link(link_id):
    """状態バーのドロップダウン選択などから呼ばれる、アクティブlinkの
    切り替え。link_id が既存linksに存在しない場合は何もしない
    (存在しないidを誤って選択状態にしてしまう事故を防ぐため)。
    """
    payload = _get_scene_project_links()
    if link_id is not None and _find_link(payload, link_id) is None:
        return False
    payload = dict(payload)
    payload["active_link_id"] = link_id
    _set_scene_project_links(payload)
    return True


def _get_current_scene_project_link():
    """[互換ラッパー] 現在アクティブなlinkのsp_project_keyだけを返す。

    2026.07.19-03より前は「シーンにつき1つの紐付け」という設計だった
    ため、多数の呼び出し元がこの関数の戻り値(文字列またはNone)を
    直接前提にしている。複数link対応後もそれらの呼び出し元を一度に
    全置換すると変更範囲が広がり過ぎるため、当面は「現在アクティブな
    linkのキーを返す」薄いラッパーとして残す。新規コードは
    _get_scene_project_links() を直接使うこと。
    """
    payload = _get_scene_project_links()
    active_link = _find_link(payload, payload.get("active_link_id"))
    if active_link is None:
        return None
    return active_link.get("sp_project_key") or None


def _set_current_scene_project_link(sp_project_key):
    """[互換ラッパー] _add_scene_project_link() を呼び、渡された
    sp_project_key をアクティブなlinkとして紐付ける。

    既存呼び出し元(「SPプロジェクトを設定」ボタン等)は「1つを
    上書きする」つもりで呼んでいるが、新形式では「無ければ追加、
    既にあれば選択」という挙動になる。天板→脚のように複数プロジェクトを
    行き来する運用では、この変更によって直前の紐付けが消えなくなる
    (これが今回の複数プロジェクト対応の主眼)。

    2026.07.19-05(不具合修正): 以前は _add_scene_project_link() の
    戻り値を見ずに常に True を返していたため、上限(_MAX_SCENE_PROJECT_
    LINKS)到達で追加に失敗した場合でも呼び出し元に「成功した」と
    伝わってしまい、実際には紐付いていないのに「紐付けました」という
    誤った成功ログが表示される不具合があった。戻り値をそのまま
    真偽値として伝播させる。
    """
    return _add_scene_project_link(sp_project_key) is not None


def _is_scene_unsaved():
    """今開いているMayaシーンが未保存(一度も名前を付けて保存されて
    いない、または新規シーンのまま)かどうかを返す。

    2026.07.21 追加(緊急、実機報告): 複数SPプロジェクトの紐付け情報は
    保存済みシーンなら cmds.fileInfo()(シーンファイル自体に紐付く、
    シーンごとに独立)へ保存されるが、未保存シーンにはファイル実体が
    無いため、代わりに共有設定ファイル上の固定スロット
    last_scene_project_links["__unsaved__"] へ仮置きする設計になっている
    (_get_scene_project_links()/_set_scene_project_links() 参照)。
    この固定スロットは「今開いている未保存シーンがどれか」を区別
    できないため、未保存シーンAで紐付けた作業対象が、そのシーンを
    保存せずに別の未保存シーンBへ切り替えた際にもそのまま引き継がれて
    しまう(Mayaの未保存シーンには安定した識別子が存在しないという
    制約に起因する構造的な限界であり、実装のバグではない)。
    この関数は、上記の限界をユーザーに案内するための判定に使う
    (_on_scene_changed() 参照)。
    """
    try:
        scene_path = cmds.file(query=True, sceneName=True)
    except Exception:
        return True
    return not scene_path


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


def _link_display_name(link):
    """状態バー・ドロップダウン表示用の、link1件分の短い名前を返す。

    label が設定されていれば "label（basename）" 形式、無ければ
    従来通り basename のみを返す(_project_display_name と同じ体裁)。

    2026.07.19-05: ユーザーが「＋現在のSPプロジェクトを追加」の名前
    入力ダイアログで、意図せずファイル名をそのまま(拡張子込みで)
    入力した場合、"Poker_Table_Stand.spp（Poker_Table_Stand.spp）"の
    ように同じ名前が二重表示される不具合があった(実機ログで確認)。
    label と basename が実質同一(拡張子の有無だけの違いを含む)の
    場合は、basename側のみを表示する。
    """
    if not link:
        return None
    project_key = link.get("sp_project_key")
    basename = _project_display_name(project_key)
    label = (link.get("label") or "").strip()
    if label and basename:
        label_stem, _ = os.path.splitext(label)
        basename_stem, _ = os.path.splitext(basename)
        if label == basename or label_stem == basename_stem:
            return basename
        return "{0}（{1}）".format(label, basename)
    return label or basename


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

    2026.07.21(Phase 1): project_key には (a) 省略時の
    cfg["active_project_key"](SP側由来、未正規化)と、(b) 呼び出し元が
    links由来(_normalize_project_key_for_compare 通過後)で渡すキーの
    2種類がありうる。texture_set_export_prefix_by_project のキー自体は
    常に(a)と同じ未正規化空間のため、辞書側のキーを正規化してから
    引くことで両方に対応する。
    """
    key = project_key if project_key is not None else cfg.get("active_project_key")
    by_project = cfg.get("texture_set_export_prefix_by_project", {})
    if key:
        normalized_by_project = {
            _normalize_project_key_for_compare(k): v for k, v in by_project.items()
        }
        prefix_map = normalized_by_project.get(_normalize_project_key_for_compare(key), {})
    else:
        prefix_map = {}
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
        # 2026.07.21(Phase 1, 複数プロジェクト並行対応の根治):
        # _last_flag_mtime_watch/_final は元々「現在アクティブな1つの
        # フォルダ」だけを前提にしたスカラー比較値だったため、複数の
        # SPプロジェクトを並行して監視するようになると、あるプロジェクトの
        # 新しいflagのmtimeが、別プロジェクトで記録済みの(たまたま数値が
        # 大きい)mtimeより小さいというだけで「まだ古い」と誤判定され、
        # 自動反映が一切トリガーされない不具合があった(症状:
        # 「リアルタイム追従ができず、Finalのみ追従できる」の主要因)。
        # ディレクトリパスをキーにしたmtime辞書に変更し、プロジェクトごと
        # に独立して新旧を判定できるようにした。
        # 2026.07.15-06で導入された _last_watch_dir_for_flag /
        # _last_final_dir_for_flag(「フォルダ自体が変わったか」を検知する
        # ための直近フォルダパス記録)は、ディレクトリ単位辞書化によって
        # 意味を失う(辞書の各エントリが自然にフォルダ単位で独立するため)
        # ので、この2変数とあわせて撤去した。
        self._flag_mtimes_watch = {}
        self._flag_mtimes_final = {}
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
        #
        # 2026.07.22(表示品質のプロジェクト別管理化): 従来は単一のboolで
        # シーン全体の品質を1つに揃える仕様だったが、複数のSPプロジェクトを
        # 1つのシーンで扱う運用(天板/脚など)では、無関係なプロジェクトの
        # ノードまで毎回一緒に切り替わり・再読込されてしまう不具合の
        # 原因になっていた。正規化済みproject_key(scene linksが1件も
        # 無いレガシー運用ではNone)をキーにした辞書へ変更し、
        # プロジェクトごとに独立して品質を保持できるようにした。
        # 未登録のプロジェクトはPreview(False)扱いとみなす
        # (quality_for_project() 参照)。
        self.quality_by_project = {}

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

        2026.07.21追加(Phase 1, 複数プロジェクト並行対応の根治):
          - watch_subfolder_by_project / final_subfolder_by_project を
            対象に追加した。これらが無いと、SP側が新しいプロジェクトの
            サブフォルダ名を記録しても、Mayaのメモリ上には反映されず
            _active_watch_dirs()/_active_final_dirs()(複数形、後述)が
            いつまでも古い集合しか返せなくなる(このリポジトリで報告
            された「複数SPプロジェクトを動的に追跡できない」不具合の
            直接的な修正箇所の1つ)。
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
            "watch_subfolder_by_project",
            "final_subfolder_by_project",
        )
        for key in dynamic_keys:
            if key in latest:
                self.config[key] = latest[key]

        if self.config.get("active_project_key") != prev_active_project_key:
            # 2026.07.20(SPプロジェクト自動追従): SP側で開いているプロジェクトが
            # 切り替わり、かつそれが現在のシーンに既にlink登録済みの
            # プロジェクトである場合、ユーザーがドロップダウンを手動操作
            # しなくても active_link_id を自動的に追従させる。
            # 「以前ベースは作ってある」との確認の通り、active_project_key の
            # 変化検知自体(このブロック)は既存の仕組みであり、今回追加したのは
            # 一致するlinkを探して自動切替する _try_auto_switch_active_link()
            # の呼び出しのみ。
            self._try_auto_switch_active_link()
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

    def _try_auto_switch_active_link(self):
        """2026.07.20(SPプロジェクト自動追従): SP側の active_project_key が
        変化した際に呼ばれる。現在のシーンに登録済みのlinkの中に、SP側が
        今開いているプロジェクトと一致するものがあれば、ユーザーの手動
        操作(ドロップダウン選択)を待たずに active_link_id を自動的に
        切り替える。

        設計方針(相談の結果、以下に確定):
        - 有効範囲: 監視(自動反映)ON/OFFに関わらず、ウィンドウを開いていれば
          常時追従する。_refresh_dynamic_config() 自体が project_poll_timer
          (監視状態に関わらず常時起動)から呼ばれる既存の仕組みに乗るため、
          このメソッド固有の追加条件分岐は不要。
        - 未登録のSPプロジェクトを開いた場合: 自動でlinkを新規追加すること
          はしない。従来通り _refresh_scene_link_label() 側の不一致警告
          (赤字表示)に委ねる。ユーザーが必要と判断した場合のみ、既存の
          「＋現在のSPプロジェクトを追加」ボタンで明示的に追加する。

        実装上の注意: _set_active_link() は呼ぶたびに無条件で
        _set_scene_project_links() を実行し、保存済みシーンの場合は
        cmds.fileInfo() への書き込みを伴う(=シーンが「変更あり」状態に
        マークされる)。3秒間隔のポーリングのたびに、既に一致している
        linkへ向けて無駄な書き込みを繰り返すと、実質何も変わっていない
        のにシーンが常に未保存扱いになり続けてしまう。そのため、
        「切り替え先が現在のactive_link_idと異なる場合のみ」呼び出す
        ガードを設けている。
        """
        active_key = _normalize_project_key_for_compare(self.config.get("active_project_key"))
        if not active_key or active_key == "__unsaved__":
            # SP未起動/未検出、または未保存プロジェクトは自動切替の対象外
            # (未保存プロジェクトは複数を区別できないという既知の制限が
            # あり、誤ったlinkへ切り替えてしまう恐れがあるため)。
            return

        payload = _get_scene_project_links()
        links = payload.get("links", [])
        if not links:
            return

        matched = None
        for link in links:
            if link.get("sp_project_key") == active_key:
                matched = link
                break
        if matched is None:
            # 未登録のプロジェクト。自動追加はせず、状態バーの不一致警告に委ねる。
            return

        if payload.get("active_link_id") == matched.get("id"):
            # 既に一致している(無駄な書き込みを避ける)。
            return

        if _set_active_link(matched.get("id")):
            self._emit_status(
                "[連携] SP側のプロジェクト切り替えを検知し、作業対象を「{0}」へ自動的に切り替えました。".format(
                    _link_display_name(matched))
            )

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

        2026.07.21(Phase 1): project_key に正規化済みキー(links由来)が
        渡された場合でも、texture_set_export_prefix_by_project と同じ
        「SP側が書き込む未正規化キー」空間へ正しく書き込めるよう、
        まず既存辞書の中に正規化後一致する既存キーが無いか探し、あれば
        そのキー(＝SP側が実際に使っている表記)を再利用する。無ければ
        渡されたキーをそのまま新規キーとして使う(次回SP側がexportした
        際に、SP側の表記のキーで別エントリができても実害はない:
        get_shading_engine_map()側で正規化して引くため両方とも読める)。
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
        normalized_key = _normalize_project_key_for_compare(key)
        actual_key = key
        for existing_key in by_project:
            if _normalize_project_key_for_compare(existing_key) == normalized_key:
                actual_key = existing_key
                break
        project_map = dict(by_project.get(actual_key, {}))
        project_map[texture_set_name] = shading_engine_name
        by_project[actual_key] = project_map
        self.save_mapping_only({"texture_set_shading_engine_map_by_project": by_project})

    # -- 開始/停止 --------------------------------------------------------

    def _ensure_active_watch_watched(self):
        """このシーンに紐付けられた「全て」のSPプロジェクトについて、
        Live/Previewサブフォルダ(所有権問題回避のため2026.07.14-02で
        導入)がWatcherに登録済みか確認し、未登録(＝SP側でプロジェクトが
        切り替わり新しいサブフォルダが誕生した、または初回)であれば
        フォルダを作成して監視に加える。
        _ensure_active_final_watched() のLive/Preview版で、同じタイマー
        から呼ばれる。

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治): 従来は
        _active_watch_dir()(単数形、代表1件のみ)を対象にしていたため、
        シーンに複数のSPプロジェクトを紐付けていても、実際に監視登録
        されるフォルダは常に1つに限定されていた(このリポジトリで報告
        された緊急バグの直接原因)。_active_watch_dirs()(複数形、この
        シーンに紐付けられた全プロジェクト分の集合)へ差し替え、集合内の
        未登録フォルダをすべて登録するループに変更した。
        """
        if not self.enabled:
            return
        active_watch_dirs = self._active_watch_dirs()
        if not active_watch_dirs:
            return
        # 2026.07.15-04: サブフォルダが切り替わった瞬間の「直前の
        # アクティブサブフォルダ」を記録しておく。reload_textures() が
        # 「古いサブフォルダを参照したまま取り残されたノード」を補正する
        # 際、watch_dir 直下の全サブフォルダ(＝他の無関係なプロジェクトの
        # ものも含む)を対象にすると誤爆の危険があるため、対象は
        # この「直前に自分が監視していたサブフォルダ」だけに限定する。
        # (2026.07.21時点: この変数はどこからも参照されないデッドコードで
        # あることが判明しているが、Phase 1では既存の記録動作自体は
        # 変更せず据え置く。撤去はPhase 2で行う。)
        prev_dirs = [d for d in self.fs_watcher.directories()
                     if os.path.dirname(d) == os.path.normpath(self.config.get("watch_dir") or "")]
        if prev_dirs:
            self._last_active_watch_dir = prev_dirs[0]

        for active_watch_dir in active_watch_dirs:
            if active_watch_dir in self.fs_watcher.directories():
                continue
            try:
                os.makedirs(active_watch_dir, exist_ok=True)
                ok = self.fs_watcher.addPath(active_watch_dir)
                if ok:
                    # UI導線改善(ご相談対応): 前回追加した「作業対象の自動切替」
                    # ログ([連携]プレフィックス)と、こちらのフォルダ監視追従
                    # ログが、どちらも「プロジェクトの切り替えを検知し」で
                    # 始まるため、ログを流し読みした際に別々の仕組みだと
                    # 気づきにくかった。[追従]プレフィックスを付け、区別できる
                    # ようにする(挙動自体は変更していない)。
                    self._emit_status("[追従] プロジェクトの切り替えを検知し、監視フォルダを更新しました: {0}".format(active_watch_dir))
            except Exception as e:
                self._emit_status("警告: 監視フォルダの追加に失敗しました: {0}".format(e))

    def _ensure_active_final_watched(self):
        """このシーンに紐付けられた「全て」のSPプロジェクトについて、
        Finalサブフォルダがwatcherに登録済みか確認し、未登録(＝SP側で
        プロジェクトが切り替わり新しいサブフォルダが誕生した、または
        初回)であればフォルダを作成して監視に加える。
        3秒間隔の軽量ポーリングタイマーからのみ呼ばれる。

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治):
        _ensure_active_watch_watched() と同じ理由で、_active_final_dir()
        (単数形)から _active_final_dirs()(複数形)へ差し替えた。
        """
        if not self.enabled:
            return
        active_final_dirs = self._active_final_dirs()
        if not active_final_dirs:
            return
        # 2026.07.15-04: reload_final_textures() の古いサブフォルダ補正が
        # 対象を絞り込めるよう、直前のアクティブFinalサブフォルダを
        # 記録しておく(詳細は _ensure_active_watch_watched() 参照)。
        # (2026.07.21時点: デッドコードと判明しているが、撤去はPhase 2で
        # 行う方針のため、Phase 1では記録動作自体は据え置く。)
        prev_dirs = [d for d in self.fs_watcher.directories()
                     if os.path.dirname(d) == os.path.normpath(self.config.get("final_export_dir") or "")]
        if prev_dirs:
            self._last_active_final_dir = prev_dirs[0]

        for active_final_dir in active_final_dirs:
            if active_final_dir in self.fs_watcher.directories():
                continue
            try:
                os.makedirs(active_final_dir, exist_ok=True)
                ok = self.fs_watcher.addPath(active_final_dir)
                if ok:
                    self._emit_status("[追従] プロジェクトの切り替えを検知し、Finalフォルダの監視先を更新しました: {0}".format(active_final_dir))
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

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治で修正):
        従来はこの不一致判定に _get_current_scene_project_link()(＝
        現在アクティブな1linkのみ)を使っていたため、天板(proA)・脚
        (proB)のように複数プロジェクトを1シーンに紐付けて運用している
        場合、SP側でproAからproBへ切り替えた瞬間に「アクティブlinkは
        まだproAのまま」と誤判定され、実際にはシーンに正しく紐付いている
        にもかかわらず監視全体が強制停止してしまう欠陥があった(複数
        プロジェクト運用そのものと矛盾する、Phase 1のレビューで発見)。
        判定を「SP側の現在プロジェクトが、シーンに紐付いた“いずれかの”
        link と一致するか」に修正した。事故防止(未登録の全く無関係な
        プロジェクトを開いてしまった場合の防御)という本来の意図は
        変えていない: linked_keys が1件も無い、またはSP側の
        active_project_keyがどのlinkとも一致しない場合のみ、従来通り
        安全側に倒して監視を自動停止する。
        """
        self._refresh_dynamic_config()

        if self.enabled:
            linked_keys = set(self._linked_sp_project_keys())
            active_key = _normalize_project_key_for_compare(self.config.get("active_project_key"))
            # [DIAG-B2] 一次切り分け用: 不一致判定の材料を毎回ログに残す。
            # 「追跡できない」がここでの自動停止によるものかどうかを、
            # linked_keys(シーンに紐付いた全プロジェクト)とactive_key
            # (SP側現在値)の実際の値を突き合わせて確定させる。
            # (_DIAG_B2_VERBOSE = True にすると再度表示される)
            if _DIAG_B2_VERBOSE:
                print("[DIAG-B2] linked_keys={0!r} active_key={1!r} match={2}".format(
                    linked_keys, active_key, (active_key in linked_keys) if (linked_keys and active_key) else "N/A"))
            if linked_keys and active_key and active_key not in linked_keys:
                if _DIAG_B2_VERBOSE:
                    print("[DIAG-B2] 不一致検出 -> 監視を自動停止します。これがB-2仮説の再現です。")
                self.stop(reason="scene_change")
                self._emit_status(
                    "警告: このシーンに紐付けられたどのSPプロジェクトとも、SP側が今開いている"
                    "プロジェクトが一致しないため、事故防止のため監視を自動停止しました。"
                    "SP側で正しいプロジェクトを開くか、状態バーから紐付けを"
                    "設定し直してください。"
                )
                return

        self._ensure_active_watch_watched()
        self._ensure_active_final_watched()

        # 2026.07.24-02(緊急バグ修正): 上記2つが新規フォルダを
        # addPath()で登録した回であっても、Qtの仕様上、登録時点で
        # 既に存在するファイル・flagについてはdirectoryChangedが
        # 発火せず、次にSP側が書き込むまで永久に取りこぼされる
        # (初回同期が反映されない不具合の直接原因)。
        # _process_pending_changes()は監視中の全アクティブフォルダの
        # flagを毎回re-scanして判定する設計のため、ここで明示的に
        # 1回呼ぶことで新規登録直後の取りこぼしを解消する。
        # enabled=False中はreload_textures()等を誤って呼ばないよう
        # ガードする。
        if self.enabled:
            self._process_pending_changes()

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
        #
        # 2026.07.21(Phase 1, 複数プロジェクト並行対応の根治): 従来は
        # _active_watch_dir()/_active_final_dir()(単数形、代表1件のみ)を
        # 初期登録していたため、複数のSPプロジェクトが紐付いている場合、
        # 代表以外のプロジェクトの監視登録が project_poll_timer の次回
        # 実行(最大3秒)まで遅延していた。実害は軽微だが、監視開始の
        # 瞬間から全プロジェクトを一貫して対象にするため、複数形
        # _active_watch_dirs()/_active_final_dirs() へ差し替えた。
        watch_targets = [watch_dir, final_dir]
        for d in self._active_watch_dirs():
            d = os.path.normpath(d)
            os.makedirs(d, exist_ok=True)
            watch_targets.append(d)
        for d in self._active_final_dirs():
            d = os.path.normpath(d)
            os.makedirs(d, exist_ok=True)
            watch_targets.append(d)
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

        # 2026.07.24-02(緊急バグ修正): 上記のaddPath()ループは、監視
        # 開始時点で既に存在するファイル・_sync_complete.flagについては
        # directoryChangedが発火しないため取りこぼす
        # (_ensure_active_dirs_watched()と同じ理由)。監視開始直後に
        # 1回だけ既存状態をキャッチアップする。
        self._process_pending_changes()
        self._emit_status("監視を開始しました: {0}".format(", ".join(sorted(watch_targets)) or watch_dir))


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
        # 2026.07.21(Phase 1, 複数プロジェクト並行対応の根治): 従来は
        # _active_watch_dir()/_active_final_dir()(単数形、代表1件のみ)を
        # 基準にしていたため、このシーンに複数のSPプロジェクトが紐付いて
        # いても、実際に監視・flag判定されるフォルダは常に1つに限定
        # されていた。_active_watch_dirs()/_active_final_dirs()(複数形、
        # 全プロジェクト分の集合)へ差し替え、以降の処理をすべて集合
        # ベースのループに変更した。
        active_watch_dirs = self._active_watch_dirs()
        watch_root_fallback = os.path.normpath(self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"])
        watch_dirs = {os.path.normpath(d) for d in active_watch_dirs} or {watch_root_fallback}
        if self.enabled:
            for watch_dir in watch_dirs:
                if watch_dir not in self.fs_watcher.directories():
                    try:
                        os.makedirs(watch_dir, exist_ok=True)
                        self.fs_watcher.addPath(watch_dir)
                    except Exception as e:
                        self._emit_status("警告: 監視フォルダの追加に失敗しました: {0}".format(e))

        # 複数プロジェクト対応: 「Finalフォルダ」は固定の final_export_dir
        # 直下ではなく、シーンに紐付いた各プロジェクトのアクティブな
        # サブフォルダを指す。ここで最新の値を読み直し、まだ監視対象に
        # 入っていなければ動的に追加する(start() は監視開始時の1回しか
        # 対象を確定しないため、開始後にSP側でプロジェクトを切り替えて
        # 新しいサブフォルダができた場合、ここで追従しないと新フォルダの
        # 変更を一切検知できない)。
        active_final_dirs = self._active_final_dirs()
        final_root_fallback = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        final_dirs = {os.path.normpath(d) for d in active_final_dirs} or {final_root_fallback}
        if self.enabled:
            for final_dir in final_dirs:
                if final_dir not in self.fs_watcher.directories():
                    try:
                        os.makedirs(final_dir, exist_ok=True)
                        self.fs_watcher.addPath(final_dir)
                    except Exception as e:
                        self._emit_status("警告: Finalフォルダの監視追加に失敗しました: {0}".format(e))

        # 2026.07.15-06(緊急修正、2026.07.21のPhase 1でディレクトリ単位
        # 辞書へ発展): _last_flag_mtime_watch/_final は元々「現在
        # アクティブな1つのフォルダ」だけを前提にしたスカラー比較値
        # だったため、複数のSPプロジェクトを並行して監視するようになると、
        # あるプロジェクトの新しいflagのmtimeが、別プロジェクトで記録済み
        # の(たまたま数値が大きい)mtimeより小さいというだけで「まだ古い」
        # と誤判定され、自動反映が一切トリガーされない不具合があった
        # (症状: 「リアルタイム追従ができず、Finalのみ追従できる」の
        # 主要因)。_flag_mtimes_watch/_final(ディレクトリパスをキーにした
        # mtime辞書)に変更し、プロジェクトごとに独立して新旧を判定できる
        # ようにした。存在しなくなった(＝もうこのシーンに紐付いていない)
        # フォルダのエントリは、辞書が際限なく肥大化しないようここで
        # 刈り込む。
        stale_watch_keys = set(self._flag_mtimes_watch) - watch_dirs
        for k in stale_watch_keys:
            del self._flag_mtimes_watch[k]
        stale_final_keys = set(self._flag_mtimes_final) - final_dirs
        for k in stale_final_keys:
            del self._flag_mtimes_final[k]

        # いずれかのプロジェクトのLive/Preview完了flagが更新されていれば
        # reload_textures()を1回呼ぶ(reload_textures()自体がシーン内の
        # 全fileノードを対象に、現在のwatch_dirs集合を基準として処理する
        # ため、複数フォルダが同時に更新されていても1回の呼び出しで
        # まとめて処理できる)。
        watch_reload_needed = False
        for watch_dir in watch_dirs:
            watch_flag = os.path.join(watch_dir, "_sync_complete.flag")
            if not os.path.isfile(watch_flag):
                continue
            try:
                mtime = os.path.getmtime(watch_flag)
            except OSError:
                continue
            if mtime > self._flag_mtimes_watch.get(watch_dir, 0.0):
                self._flag_mtimes_watch[watch_dir] = mtime
                watch_reload_needed = True
        if watch_reload_needed:
            self.reload_textures()

        # Finalフォルダ側の完了flag。表示品質がFinalになっているプロジェクト
        # の分だけ、該当ノードを強制再読込する(プレビュー表示中の
        # プロジェクトについては、Finalの更新を反映する必要が無いため
        # 何もしない)。
        #
        # 2026.07.22(表示品質のプロジェクト別管理化): 従来はシーン全体で
        # 1つの self.using_final_quality だけを見ていたため、あるプロジェクト
        # がFinal表示中に別プロジェクトのFinalが更新されると、Preview表示の
        # ままの他プロジェクトまで巻き込んで再読込されてしまっていた。
        # フォルダ→project_keyの逆引きを作り、更新があった各フォルダについて
        # 「そのフォルダの持ち主のプロジェクトが今Final表示かどうか」を
        # 個別に判定するよう変更した。
        final_by_project_normalized = self._final_subfolder_by_project_normalized()
        final_dir_to_project = {
            os.path.normpath(os.path.join(final_root_fallback, subfolder)): key
            for key, subfolder in final_by_project_normalized.items()
        }
        final_reload_dirs = set()
        for final_dir in final_dirs:
            final_flag = os.path.join(final_dir, "_sync_complete.flag")
            if not os.path.isfile(final_flag):
                continue
            try:
                mtime = os.path.getmtime(final_flag)
            except OSError:
                continue
            if mtime > self._flag_mtimes_final.get(final_dir, 0.0):
                self._flag_mtimes_final[final_dir] = mtime
                project_key = final_dir_to_project.get(final_dir)
                if self.quality_for_project(project_key):
                    final_reload_dirs.add(final_dir)
        if final_reload_dirs:
            self.reload_final_textures(only_dirs=final_reload_dirs)

    # -- 再読込処理 ---------------------------------------------------------

    def reload_textures(self):
        # 単独呼び出し(手動リロード等)でも最新のサブフォルダ名を
        # 確実に掴めるよう、念のためここでも読み直す。
        self._refresh_dynamic_config()

        # 2026.07.21(Phase 1, 複数プロジェクト並行対応の根治): 従来は
        # _active_watch_dir()(単数形、代表1件のみ)を基準にしていたため、
        # このシーンに複数のSPプロジェクトが紐付いていても、実際に
        # reloadされるのは常に1プロジェクト分のfileノードに限定されて
        # いた(このリポジトリで報告された緊急バグの直接原因)。
        # _active_watch_dirs()(複数形、全プロジェクト分の集合)を
        # 基準にし、いずれかの監視対象フォルダを参照しているノードは
        # すべてreload対象に含める。
        watch_root = os.path.normpath(self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"])
        active_watch_dirs = self._active_watch_dirs()
        watch_dirs = {os.path.normpath(d) for d in active_watch_dirs}
        if not watch_dirs:
            watch_dirs = {watch_root}

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
        #
        # 2026.07.21(Phase 1): 複数プロジェクト対応により、「現在
        # アクティブでない」の基準が「watch_dirs集合に含まれない」に
        # 変わった。また、補正先も「現在の1つのサブフォルダ」ではなく
        # 「ファイル名prefixから逆引きした、該当プロジェクトのサブ
        # フォルダ」に変更した(_match_known_texture_set_project() が
        # プロジェクトキーまで特定できるようになったため)。該当
        # プロジェクトの watch_subfolder_by_project エントリがまだ無い
        # (一度もプレビュー書き出しされていない)場合は、安全側に倒して
        # 補正しない。
        stale_subfolder_dirs = set()
        try:
            if os.path.isdir(watch_root):
                for entry in os.listdir(watch_root):
                    candidate = os.path.normpath(os.path.join(watch_root, entry))
                    if os.path.isdir(candidate) and candidate not in watch_dirs:
                        stale_subfolder_dirs.add(candidate)
        except OSError:
            pass

        file_nodes = cmds.ls(type="file") or []
        if not file_nodes:
            self._emit_status("シーン内に file ノードが見つかりません。")
            return

        watch_by_project_normalized = self._watch_subfolder_by_project_normalized()

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
                if tex_dir in watch_dirs:
                    pass  # 既にいずれかの監視対象サブフォルダを参照している通常ケース
                elif tex_dir in stale_subfolder_dirs:
                    # 古いプロジェクト用サブフォルダを参照したまま取り残
                    # されたノード。ファイル名がどのプロジェクトの既知
                    # テクスチャセットのprefixと一致するかを特定し
                    # (2026.07.15-05の安全確認を複数プロジェクト対応に
                    # 拡張)、該当プロジェクトの現在のサブフォルダへ
                    # パスを補正する。一致するプロジェクトが無い、または
                    # 一致はしたがそのプロジェクトがまだ一度もプレビュー
                    # 書き出しをしていない(watch_subfolder_by_projectに
                    # エントリが無い)場合は、無関係な他プロジェクトを
                    # 誤補正しないよう何もしない。
                    matched_key = self._match_known_texture_set_project(os.path.basename(tex_path))
                    subfolder = watch_by_project_normalized.get(matched_key) if matched_key else None
                    if not subfolder:
                        continue
                    base = os.path.basename(tex_path)
                    new_path = os.path.join(
                        os.path.normpath(os.path.join(watch_root, subfolder)),
                        base,
                    )
                    # 2026.07.24(緊急バグ修正): switch_texture_quality()には
                    # 補正先ファイルの実在チェックがあるのに、この補正処理
                    # には無く、実在しないパスへも無条件でsetAttrしていた。
                    # switch_texture_quality()と同じ、<UDIM>トークンを含む
                    # 場合はグロブで少なくとも1タイルの実在を確認するチェックを
                    # 移植する。実在しない場合は補正せず(誤ったパスへ書き換え
                    # ない)、そのノードは今回のreload対象から外す
                    # (「見つからない」旨は既存のログにまとめて計上される)。
                    if "<UDIM>" in base:
                        udim_glob = new_path.replace("<UDIM>", "[0-9][0-9][0-9][0-9]")
                        if not glob.glob(udim_glob):
                            continue
                    elif not os.path.isfile(new_path):
                        continue
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
                self._flush_with_settle(reloaded)
                self.stats["reload_count"] += 1
                self.stats["last_reload_at"] = _now()
                self.stats["last_node_count"] = len(reloaded)
                self.stats_changed.emit(dict(self.stats))
                self._emit_status("{0} 個のテクスチャを再読込しました。".format(len(reloaded)))
            elif not any(self.quality_by_project.values()):
                # Phase 6: 高画質表示中のプロジェクトがある場合、そのぶんの
                # file ノードは監視フォルダを参照していないのが正常な状態
                # なので、この場合は「見つからない」という誤解を招くログを
                # 出さない。2026.07.22(表示品質のプロジェクト別管理化):
                # 品質はプロジェクトごとに独立して持つようになったため、
                # 「シーン内のどれか1つでもFinal表示中のプロジェクトが
                # あるか」を緩い近似として使う(全プロジェクトがPreviewの
                # 時だけこのログを出す)。
                self._emit_status("監視フォルダを参照する file ノードが見つかりませんでした。")
        finally:
            cmds.undoInfo(stateWithoutFlush=prev_undo_state)

    def reload_final_textures(self, only_dirs=None):
        """Finalフォルダ配下のfileノードを強制再読込する
        (reload_textures()のFinal版)。表示品質をFinalにしたまま
        SP側で保存・高画質書き出しが行われても、Live⇔Finalを
        往復切り替えしなくても自動的に反映されるようにするためのもの。

        only_dirs: 2026.07.22(表示品質のプロジェクト別管理化)で追加。
        指定した場合、実際に再読込の対象にするフォルダをこの集合との
        積集合に絞り込む(古いサブフォルダの検出・補正自体は従来通り
        final_root全体を対象に行うが、実際に強制再読込するのは
        呼び出し元が「今Final表示中」と判断したプロジェクトのフォルダ
        だけにする)。呼び出し元(_process_pending_changes)は、
        quality_for_project() がTrueのプロジェクトのフォルダだけを
        渡す。Noneの場合は全フォルダを対象にする(手動呼び出し用、
        後方互換)。

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

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治): 従来の
        _active_final_dir()(単数形、代表1件のみ)から _active_final_dirs()
        (複数形、このシーンに紐付いた全プロジェクト分の集合)へ差し替えた。
        補正先も、ファイル名prefixから逆引きした該当プロジェクトの
        サブフォルダへ変更した(詳細は reload_textures() のコメント参照)。
        """
        final_root = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        active_final_dirs = self._active_final_dirs()
        all_final_dirs = {os.path.normpath(d) for d in active_final_dirs}
        if not all_final_dirs:
            all_final_dirs = {final_root}

        # 2026.07.22: 古いサブフォルダの検出("stale"判定)は、必ず
        # all_final_dirs(このシーンに紐付いた全プロジェクト分)を基準に
        # 行う。only_dirsで絞り込んだ後の集合を基準にしてしまうと、
        # 「今回はFinal表示ではないため対象外にしただけ」の別プロジェクトの
        # 正当なフォルダまで「古い」と誤判定し、無関係な補正が走ってしまう。
        final_dirs = all_final_dirs
        if only_dirs is not None:
            final_dirs = all_final_dirs & {os.path.normpath(d) for d in only_dirs}
            if not final_dirs:
                return

        stale_subfolder_dirs = set()
        try:
            if os.path.isdir(final_root):
                for entry in os.listdir(final_root):
                    candidate = os.path.normpath(os.path.join(final_root, entry))
                    if os.path.isdir(candidate) and candidate not in all_final_dirs:
                        stale_subfolder_dirs.add(candidate)
        except OSError:
            pass

        file_nodes = cmds.ls(type="file") or []
        if not file_nodes:
            return

        final_by_project_normalized = self._final_subfolder_by_project_normalized()

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
                if tex_dir in final_dirs:
                    pass
                elif tex_dir in stale_subfolder_dirs:
                    # 2026.07.15-05・2026.07.21: ファイル名がどのプロジェ
                    # クトの既知テクスチャセットのprefixと一致するかを
                    # 特定し(無関係な他プロジェクトを誤補正しないための
                    # 安全確認)、該当プロジェクトの現在のFinalサブ
                    # フォルダへ補正する。一致するプロジェクトが無い、
                    # またはそのプロジェクトがまだ一度もFinal書き出しを
                    # していない場合は何もしない。
                    matched_key = self._match_known_texture_set_project(os.path.basename(tex_path))
                    subfolder = final_by_project_normalized.get(matched_key) if matched_key else None
                    if not subfolder:
                        continue
                    base = os.path.basename(tex_path)
                    new_path = os.path.join(
                        os.path.normpath(os.path.join(final_root, subfolder)),
                        base,
                    )
                    # 2026.07.24(緊急バグ修正): reload_textures()と同じ理由
                    # で、補正先ファイルの実在チェック(switch_texture_
                    # quality()と同じ<UDIM>グロブ対応)を追加する。
                    if "<UDIM>" in base:
                        udim_glob = new_path.replace("<UDIM>", "[0-9][0-9][0-9][0-9]")
                        if not glob.glob(udim_glob):
                            continue
                    elif not os.path.isfile(new_path):
                        continue
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
                self._flush_with_settle(reloaded)
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
                # 2026.07.17 追加: arnoldFlushCache(textures=True) は
                # Arnold内部のテクスチャキャッシュを破棄するだけで、
                # 既に開いて動作中の Arnold RenderView(IPR)セッションには
                # 反映されない不具合が実機で確認された(ARVを一度閉じて
                # 開き直すまで古いテクスチャが表示され続ける)。
                # Arnold公式ドキュメント(Arnold for Maya User Guide)は
                # これと同じ状況のためにRenderViewの「Update Full Scene」
                # 機能(Ctrl-U相当)を用意しており、"This avoids to close
                # and re-open the RenderView." と明記している。
                # -option フラグでメニュー名を直接指定することで、
                # スクリプトから同じ操作を発行できる(Solid Angle公式
                # フォーラムで確認済みの構文)。
                #
                # 安全性について: ARVが開いていない状態(=通常のLive Sync
                # 運用中の大半)でこの呼び出しが失敗しても、以後の同期
                # 動作(テクスチャ再読込・ビューポート更新)には一切
                # 影響させたくないため、例外は黙って握りつぶす
                # (_emit_status は呼ばない = 毎回の同期のたびにステータス
                # 欄がエラーで埋まる事態を避ける)。
                try:
                    cmds.arnoldRenderView(option=["Update Full Scene", "1"])
                except Exception:
                    pass
            else:
                self._emit_status("mtoa 未ロードのため Arnold キャッシュフラッシュをスキップしました。")
        elif renderer == "redshift":
            self._emit_status("Redshift: mtime自動検知に依存(要検証)。")
        elif renderer == "vray":
            self._emit_status("V-Ray: 専用フラッシュコマンド未確定。IPR再起動を推奨(要検証)。")

    def _flush_with_settle(self, file_nodes):
        """
        _flush_renderer_caches() / _flush_viewport_cache() を呼ぶ前に、
        Mayaのイベントループへ明示的に制御を戻す猶予を作るラッパー。

        2026.07.17: 当初は「マテリアルが割り当ててあるオブジェクトを
        選択している状態だと反映が速い」という報告から、コード側で
        該当オブジェクトを cmds.select() する対策を試みた。しかし
        実機検証の結果、スクリプトからの選択では効果がなく、実際に
        人間がクリックして選択した場合(たとえ一瞬の選択でも)にのみ
        反映が速くなることが確認された。

        これは「選択されているかどうか」自体が本質ではなく、クリックと
        いう実際のUIイベントをMayaのイベントループ(Qt)が処理する
        タイミングが間に挟まることで、保留中のシェーディングネット
        ワークの評価がその間に完了しているためではないか、と考え直した
        (file_nodes 引数は現在この仮説の下では未使用だが、将来また
        オブジェクト単位の対策に戻す可能性を考慮してシグネチャは
        維持している)。

        対策として、実際のクリック操作を模して:
          1. cmds.refresh(force=True) でビューポートの再描画を強制する
             (クリック時に発生する再描画パスを疑似的に再現する)
          2. QCoreApplication.processEvents() でMayaのイベントループに
             滞留しているイベント・遅延評価を処理させる(人間の操作の
             後に自然に生じる「間」を、コード側で明示的に作る)
        の2段階を、Arnoldのキャッシュフラッシュ/Update Full Sceneより
        前に挟む。
        """
        try:
            cmds.refresh(force=True)
        except Exception:
            pass
        try:
            QtCore.QCoreApplication.processEvents()
        except Exception:
            pass

        self._flush_renderer_caches()
        self._flush_viewport_cache()

    def _flush_viewport_cache(self):
        try:
            cmds.ogs(reset=True)
        except Exception as e:
            self._emit_status("ogs(reset=True) に失敗: {0}".format(e))
        # 2026.07.17 追加: Viewport 2.0 はUDIM(複数UVタイル)のfileノードに
        # 対して、シーンを開いた直後やテクスチャ差し替え直後はプレビュー
        # 画像を自動生成しない(Autodesk公式ドキュメントに明記された仕様。
        # Windows > Settings/Preferences > Preferences > Display の
        # "Generate UV tile previews on scene load" が既定でオフ)。
        # このためUDIM対応オブジェクトが「マテリアル未割り当て」のように
        # 見え、実際にはオブジェクトを選択する等でMaya側のプレビュー生成が
        # 走って初めて正しく表示される、という体感の不具合を生む。
        # Arnoldでのレンダリング自体は無関係(Arnoldは独自にテクスチャを
        # 読むため、この問題の影響を受けない)。
        # generateAllUvTilePreviews は上記プレビュー生成をスクリプトから
        # 明示的に行うMELコマンド。ここで毎回呼んでおくことで、選択操作を
        # 挟まなくてもビューポート表示が正しく更新されるようにする。
        #
        # 2026.07.23-03(緊急修正): generateAllUvTilePreviews は
        # Maya本体の others/generateUvTilePreview.mel で定義されている
        # グローバルprocだが、この定義ファイルはMaya起動直後や、UI操作を
        # 何も経由していないタイミングでは自動ソースされておらず、
        # 「プロシージャ "generateAllUvTilePreviews" が見つかりません」と
        # いうMELエラーで失敗することが実機(mayapy バッチセッション)で
        # 確認された。従来はこの呼び出しが例外を握りつぶしてログに警告を
        # 出すだけだったため、失敗に気付きにくかった。呼び出し前に明示的に
        # 定義元のMELファイルをsourceすることで、Maya側の自動ロード
        # タイミングに依存せず確実にprocが定義された状態にする
        # (source自体は既にロード済みでも安全に再実行できる)。
        try:
            import maya.mel as mel
            mel.eval('source "generateUvTilePreview.mel";')
            mel.eval("generateAllUvTilePreviews;")
        except Exception as e:
            self._emit_status("UVタイルプレビューの再生成に失敗: {0}".format(e))
        cmds.refresh(force=True)

    # -- Phase 2: テクスチャセット構造への対応 -------------------------------

    def get_known_texture_sets(self):
        """マテリアル構造タブに表示するテクスチャセット名の一覧を返す。

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治・論点2案A):
        従来は self.config["active_project_key"](SP側が今開いている
        1プロジェクトのみ)を基準にしていたため、シーンに複数プロジェクト
        を紐付けていても、SP側で今開いていない方のテクスチャセットは
        一覧から漏れ、is_texture_set_mapped() 経由でマテリアル未対応と
        誤判定される一因になっていた(症状: 「Fileノードが存在するのに
        一度再起動しなければマテリアルを認識しない」)。
        このシーンに紐付けられた全プロジェクトのテクスチャセットの
        和集合を返すよう変更した(論点2で合意した案A)。
        同名テクスチャセットが複数プロジェクトに存在する場合、この
        メソッドの戻り値(名前のみのリスト)では区別できない点に注意:
        UI側でプロジェクトごとの内訳を示したい場合は
        get_known_texture_sets_detailed() を使うこと。

        2026.07.21 追加修正(緊急、実機報告): 当初はシーンにlinkが1件も
        無い場合、後方互換のため _active_texture_sets()(self.config
        ["active_project_key"]基準)へフォールバックしていた。しかし
        active_project_key はMaya側のメモリ上でシーンをまたいで保持され
        続ける値(SP側が今開いているプロジェクトを表すだけで、シーン
        固有の情報ではない)であるため、「シーンAでプロジェクトXの
        テクスチャセットを表示 → 紐付け未設定のシーンBへ切り替え」た
        際にも、前のシーンで表示していたXの一覧が残り続けてしまう
        欠陥があった(実機で「シーンを切り替えても作業対象が残ったまま」
        として報告・再現)。
        シーン固有でない値へのフォールバックはこの欠陥を再発させるため
        廃止し、このシーンにlinkが1件も無い場合は空リストを返すように
        変更した。これにより、シーンを切り替えた直後は(まだ「SP
        プロジェクトを設定」等で紐付けていない限り)一覧が正しく空になる。
        """
        linked_keys = self._linked_sp_project_keys()
        if not linked_keys:
            return []

        by_project = self.config.get("known_texture_sets_by_project", {}) or {}
        # known_texture_sets_by_project のキーはSP側が書き込む未正規化の
        # project_key。links側のsp_project_keyは正規化済みのため、
        # 突き合わせ用に辞書キー側を正規化してから引く。
        normalized_by_project = {
            _normalize_project_key_for_compare(k): v for k, v in by_project.items()
        }
        names = set()
        for key in linked_keys:
            names.update(normalized_by_project.get(key, []))
        return sorted(names)

    def get_known_texture_sets_detailed(self):
        """マテリアル一覧UI向けに、テクスチャセット名だけでなく
        「どのプロジェクトのものか」も併せて返す(論点2案A)。

        戻り値: [(texture_set_name, project_key(正規化済み),
                  project_display_name), ...]
        同名のテクスチャセットが複数プロジェクトに存在する場合は、
        プロジェクトごとに別エントリとして列挙される(名前だけでは
        区別できないため、テーブルには両方を表示しプロジェクト名列で
        見分けられるようにする)。

        2026.07.21 追加修正(緊急、実機報告): シーンにlinkが1件も無い
        場合、get_known_texture_sets() が空リストを返すようになった
        (シーン切替時に前のシーンの一覧が残留する不具合の修正、詳細は
        get_known_texture_sets() のdocstring参照)ため、この関数も
        自然に空リストを返す。以前は「後方互換」としてactive_project_key
        基準の一覧を1件ずつ返していたが、これが残留バグの一因だった
        ため廃止した。
        """
        linked_keys = self._linked_sp_project_keys()
        if not linked_keys:
            return [(name, None, None) for name in self.get_known_texture_sets()]

        by_project = self.config.get("known_texture_sets_by_project", {}) or {}
        normalized_by_project = {
            _normalize_project_key_for_compare(k): v for k, v in by_project.items()
        }
        payload = _get_scene_project_links()
        # プロジェクトキー -> このシーンでの表示名(labelがあればそちら優先)
        display_name_by_key = {}
        for link in payload.get("links", []):
            key = link.get("sp_project_key")
            if key:
                display_name_by_key[key] = _link_display_name(link) or _project_display_name(key)

        rows = []
        for key in linked_keys:
            names = sorted(normalized_by_project.get(key, []))
            display_name = display_name_by_key.get(key) or _project_display_name(key)
            for name in names:
                rows.append((name, key, display_name))
        return rows

    def get_active_project_label(self):
        """マテリアル構造タブに表示する「今どのプロジェクトの一覧を
        見ているか」の説明文を返す。SP側が未対応(active_project_keyが
        無い)場合はその旨を明示し、誤解を防ぐ。

        2026.07.15-01: このシーンに紐付けられたSPプロジェクトと、
        SP側が今実際に開いているプロジェクトが食い違っている場合は
        その旨も明示する(状態バー上部の表示と合わせて、一覧が
        「別プロジェクトのものかもしれない」と気づけるようにするため)。

        2026.07.21(Phase 1, 論点2案A): get_known_texture_sets() が
        和集合を返すようになったため、この説明文も「和集合表示中」で
        あることが伝わるよう文言を調整した(複数プロジェクトが紐付いて
        いる場合のみ)。

        2026.07.21 追加修正(緊急、実機報告): 従来は linked_keys が0件
        または1件の場合に active_key(SP側が今開いているプロジェクト、
        シーンをまたいでメモリに残り続ける値)を無条件で使ってラベルを
        組み立てていたため、「このシーンにはまだ何も紐付けていない」の
        に、前のシーンで開いていたSPプロジェクトの名前が表示され続ける
        欠陥があった(get_known_texture_sets() と同一の根本原因。
        実機で「シーンを切り替えても作業対象が残ったまま」として報告・
        再現)。linked_keys が0件の場合を明示的に分岐させ、
        「このシーンには紐付けが無い」ことが伝わるラベルを返すように
        変更した。active_key ベースのラベルは、このシーンに紐付けが
        1件だけある場合に限定する。
        """
        by_project = self.config.get("known_texture_sets_by_project", {})
        active_key = self.config.get("active_project_key")
        linked_key = _get_current_scene_project_link()
        linked_keys = self._linked_sp_project_keys()

        if len(linked_keys) >= 2:
            return "このシーンに紐付けられた {0} 件のSPプロジェクト分をまとめて表示中".format(len(linked_keys))

        if not linked_keys:
            return "このシーンにはまだSPプロジェクトが紐付けられていません。"

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

        2026.07.21(Phase 1): project_key には2種類の呼ばれ方がある。
        (a) 省略時のself.config["active_project_key"](SP側由来、未正規化)
        (b) 呼び出し元がget_known_texture_sets_detailed()等から得た
            links由来のキー(_normalize_project_key_for_compare 通過後)
        texture_set_shading_engine_map_by_project のキー自体は常に(a)と
        同じ未正規化空間のため、(b)のキーをそのまま辞書に渡すと
        Windows環境で区切り文字の違いにより一致しないことがある。
        双方を吸収するため、辞書側のキーを正規化してから引く。
        """
        key = project_key if project_key is not None else self.config.get("active_project_key")
        if not key:
            return {}
        by_project = self.config.get("texture_set_shading_engine_map_by_project", {})
        normalized_by_project = {
            _normalize_project_key_for_compare(k): v for k, v in by_project.items()
        }
        return dict(normalized_by_project.get(_normalize_project_key_for_compare(key), {}))

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

        2026.07.21(Phase 1)以降、真偽値ではなく後方互換のため引き続き
        真偽値を返す(呼び出し元を壊さないため)。プロジェクトキーまで
        必要な呼び出し元は _match_known_texture_set_project() を使う。
        """
        return self._match_known_texture_set_project(filename) is not None

    def _match_known_texture_set_project(self, filename):
        """ファイル名が、このシーンに紐付けられた「いずれかの」SP
        プロジェクトの既知テクスチャセットのprefixと一致するかを確認し、
        一致した場合はそのプロジェクトキー(正規化済み)を返す。
        一致しなければNoneを返す。

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治): 従来の
        _matches_known_texture_set_prefix()(真偽値のみ)は
        get_known_texture_sets()(旧: アクティブ1件のみ)と
        _export_prefix(self.config, name)(project_key省略 = active_
        project_key基準)の組み合わせのため、実質「今SP側が開いている
        1プロジェクト」しか照合できなかった。reload_textures()/
        reload_final_textures() の「古いサブフォルダを参照したまま
        取り残されたノード」の補正先を、複数プロジェクトの中から正しく
        1つ特定できるよう、プロジェクトキーごとに get_known_texture_
        sets_detailed() のエントリとexport_prefixを照合する形に拡張した。

        注意(正規化キー空間の不一致): get_known_texture_sets_detailed()
        が返す project_key は links 由来で正規化済み(_normalize_
        project_key_for_compare 通過後)。_export_prefix() 自体が
        (2026.07.21のPhase 1改修で)辞書側のキーを都度正規化してから
        引くようになったため、ここでは正規化済みキーをそのまま渡せば
        よい(自前での正規化辞書構築は不要になった)。

        2026.07.24(緊急バグ修正・接頭辞衝突): 従来は最初にマッチした
        候補を無条件で返していたが、あるプロジェクトの書き出し
        接頭辞(prefix)が別プロジェクトの接頭辞の先頭部分と一致する
        場合(例: "table" と "table_legs")、"table_legs_BaseColor.png"
        のようなファイルが detailed の走査順で先に来た "table" 側に
        誤って一致してしまう不具合があった。全候補を走査し、一致した
        中で最も長い(＝最も具体的な)prefixを持つプロジェクトを採用
        するよう修正した。同じ長さのprefixが異なるプロジェクトから
        複数一致した場合は、どちらを選ぶべきか一意に決められないため、
        誤って別プロジェクトへ補正してしまうより安全側に倒してNoneを
        返す(=呼び出し元は「一致しない」扱いとして何もしない)。
        """
        detailed = self.get_known_texture_sets_detailed()
        best_prefix_len = -1
        best_project_key = None
        ambiguous = False
        for name, project_key, _display_name in detailed:
            prefix = _export_prefix(self.config, name, project_key=project_key)
            if not filename.startswith(prefix + "_"):
                continue
            prefix_len = len(prefix)
            if prefix_len > best_prefix_len:
                best_prefix_len = prefix_len
                best_project_key = project_key
                ambiguous = False
            elif prefix_len == best_prefix_len and project_key != best_project_key:
                ambiguous = True
        if ambiguous:
            return None
        return best_project_key

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

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治): 単数形の
        _active_watch_dir()/_active_final_dir()(代表1件)ではなく、
        複数形の _active_watch_dirs()/_active_final_dirs()(このシーンに
        紐付けられた全プロジェクト分の集合)を対象に含めるよう変更した。
        これにより、is_texture_set_mapped() のフォールバック走査が
        「今SP側でアクティブなプロジェクト以外」のfileノードも正しく
        管理対象と認識できるようになる(症状: 「Fileノードが存在するのに
        一度再起動しなければマテリアルを認識しない」の根治)。
        """
        dirs = set()
        watch_dir = self.config.get("watch_dir")
        if watch_dir:
            dirs.add(os.path.normpath(watch_dir))
        dirs |= self._active_watch_dirs()
        final_dir = self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"]
        if final_dir:
            dirs.add(os.path.normpath(final_dir))
        dirs |= self._active_final_dirs()
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

        2026.07.21(Phase 1)以降、この関数(単数形)は「UI表示・aiSSの
        フォルダ欄自動入力用に代表1件を返す」役割に限定される。監視・
        reload・マッピング判定など、複数プロジェクトを同時に扱う必要が
        ある処理は _active_final_dirs()(複数形、下記)を使うこと。
        """
        final_dir = self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"]
        if not final_dir:
            return None
        subfolder = self.config.get("active_final_subfolder")
        if subfolder:
            return os.path.join(final_dir, subfolder)
        return final_dir

    def _watch_subfolder_for_project_key(self, raw_project_key):
        """SP側が書き込む未正規化のproject_key(watch_subfolder_by_project
        のキー)から、監視先サブフォルダ名を引く。内部ヘルパー。
        """
        by_project = self.config.get("watch_subfolder_by_project", {}) or {}
        return by_project.get(raw_project_key)

    def _final_subfolder_for_project_key(self, raw_project_key):
        """SP側が書き込む未正規化のproject_key(final_subfolder_by_project
        のキー)から、Final書き出し先サブフォルダ名を引く。内部ヘルパー。
        """
        by_project = self.config.get("final_subfolder_by_project", {}) or {}
        return by_project.get(raw_project_key)

    def _watch_dir_for_project(self, project_key):
        """指定した1つのSPプロジェクト(未正規化・正規化済みいずれのキーでも
        可、Noneも可)のLive/Preview書き出し先の実パスを返す。

        2026.07.22(表示品質のプロジェクト別管理化)で新設。
        _active_watch_dir()(単数形、「今SP側が開いているプロジェクト」
        固定)と異なり、任意のプロジェクトを明示的に指定できる。
        create_shader_network() と同じ方針で、そのプロジェクトがまだ
        プレビュー書き出しの実績が無い(サブフォルダ未登録)場合は
        watch_dir 直下へフォールバックする(_active_watch_dirs()複数形の
        ように「未登録なら追跡対象から外す」設計とは意図的に違う。
        単一プロジェクトを明示的に対象にするこの関数では、フォールバック
        先が無いよりは watch_dir 直下を暫定候補として返す方が親切なため)。
        """
        watch_dir = os.path.normpath(self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"])
        normalized_key = _normalize_project_key_for_compare(project_key) if project_key else None
        subfolder = self._watch_subfolder_by_project_normalized().get(normalized_key) if normalized_key else None
        return os.path.normpath(os.path.join(watch_dir, subfolder)) if subfolder else watch_dir

    def _final_dir_for_project(self, project_key):
        """_watch_dir_for_project() のFinal版。"""
        final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
        normalized_key = _normalize_project_key_for_compare(project_key) if project_key else None
        subfolder = self._final_subfolder_by_project_normalized().get(normalized_key) if normalized_key else None
        return os.path.normpath(os.path.join(final_dir, subfolder)) if subfolder else final_dir

    def quality_for_project(self, project_key):
        """指定したSPプロジェクトの現在の表示品質を返す
        (True=高画質/Final、False=プレビュー)。

        2026.07.22(表示品質のプロジェクト別管理化)で新設。
        quality_by_project に未登録のプロジェクト(まだ一度も
        switch_texture_quality()で明示的に切り替えていない)はプレビュー
        (False)扱いとみなす。project_keyがNone(scene linksが1件も無い
        レガシー運用)の場合は、専用のNoneキーに保存された値を返す。
        """
        normalized_key = _normalize_project_key_for_compare(project_key) if project_key else None
        return self.quality_by_project.get(normalized_key, False)

    def _linked_sp_project_keys(self):
        """現在のシーンに紐付けられた全linkのsp_project_key(正規化済み、
        _normalize_project_key_for_compare 通過後)を重複無く返す。

        2026.07.21(Phase 1): 複数プロジェクト並行対応の根治で新設。
        従来は self.config["active_project_key"](SP側が今開いている
        1プロジェクトのみ)を追跡の起点にしていたが、これだと「シーンに
        紐付いているが今SP側では開いていない、もう一方のプロジェクト」の
        監視が抜け落ちる。ここでは _get_scene_project_links() の
        全linkを起点にすることで、シーンに紐付いた全プロジェクトを
        等しく対象にする(SP側の現在の開閉状態には依存しない)。
        """
        try:
            payload = _get_scene_project_links()
        except Exception:
            return []
        keys = []
        seen = set()
        for link in payload.get("links", []):
            key = link.get("sp_project_key")
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
        return keys

    def _watch_subfolder_by_project_normalized(self):
        """watch_subfolder_by_project(SP側の未正規化キー)を、
        _normalize_project_key_for_compare() 通過後のキーに揃え直した
        辞書として返す。links側のsp_project_key(正規化済み)と直接
        突き合わせられるようにするための内部ヘルパー。

        同一プロジェクトが正規化前後で衝突するケースは実質無い
        (正規化はスラッシュ区切りの統一のみで、値の実体は変えない)ため、
        単純な上書きで問題ない。
        """
        by_project = self.config.get("watch_subfolder_by_project", {}) or {}
        return {
            _normalize_project_key_for_compare(k): v
            for k, v in by_project.items()
        }

    def _final_subfolder_by_project_normalized(self):
        """final_subfolder_by_project版の _watch_subfolder_by_project_normalized()。"""
        by_project = self.config.get("final_subfolder_by_project", {}) or {}
        return {
            _normalize_project_key_for_compare(k): v
            for k, v in by_project.items()
        }

    def _active_watch_dirs(self):
        """現在のシーンに紐付けられた「全て」のSPプロジェクトについて、
        Live(プレビュー)監視先の実パスを集合で返す。

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治): このリポジトリで
        報告された緊急バグ(「複数SPプロジェクトを動的に追跡できず、最初に
        設定したSPプロジェクトのみを追跡している」)の直接の修正箇所。

        従来の _active_watch_dir()(単数形)は self.config["active_watch_
        subfolder"](SP側が今開いている1プロジェクトのみを指すスカラー値)
        を参照していたため、複数のSPプロジェクトを1シーンに紐付けて
        並行作業すると、後から開いた方のサブフォルダで前の方が上書きされ、
        Maya側が同時に追跡できるプロジェクトが実質1つに限定されていた。

        この関数は「今SP側で何が開いているか」ではなく「このシーンに
        紐付けられている全プロジェクト」を起点にし、watch_subfolder_
        by_project(プロジェクトキー単位のネスト辞書、SP側がexportの
        たびに更新)からそれぞれのサブフォルダを引いて実パスを組み立てる。
        該当プロジェクトがまだ辞書に無い(一度もプレビュー書き出しを
        行っていない)場合は、そのプロジェクト分は watch_dir 直下への
        フォールバックとせず、スキップする(直下フォルダを複数プロジェクト
        分のフォールバック先として共有すると、かえって混線の原因になる
        ため。直下フォルダは後方互換の1プロジェクト運用専用に残す)。

        シーンにlinkが1件も無い場合(従来通りの単一プロジェクト運用、
        または紐付け未設定)は、後方互換のため単数形 _active_watch_dir()
        の結果1件を返す。
        """
        watch_dir = self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"]
        if not watch_dir:
            return set()

        linked_keys = self._linked_sp_project_keys()
        if not linked_keys:
            # 紐付けが無い(または旧バージョンのシーン)場合の後方互換:
            # 従来通り単数形の代表1件のみを返す。
            single = self._active_watch_dir()
            return {os.path.normpath(single)} if single else set()

        by_project_normalized = self._watch_subfolder_by_project_normalized()
        dirs = set()
        for key in linked_keys:
            subfolder = by_project_normalized.get(key)
            if not subfolder:
                # このプロジェクトはまだプレビュー書き出しの実績が無い。
                # watch_dir直下を共有フォールバック先にすると別プロジェクト
                # 同士が混線するため、追跡対象に含めない(次にSP側で
                # exportされた時点で自動的に追跡対象へ加わる)。
                continue
            dirs.add(os.path.normpath(os.path.join(watch_dir, subfolder)))
        return dirs

    def _active_final_dirs(self):
        """_active_watch_dirs() のFinal版。現在のシーンに紐付けられた
        全SPプロジェクトについて、Final書き出し先の実パスを集合で返す。
        設計方針・フォールバック方針は _active_watch_dirs() と同一
        (詳細はそちらのdocstring参照)。
        """
        final_dir = self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"]
        if not final_dir:
            return set()

        linked_keys = self._linked_sp_project_keys()
        if not linked_keys:
            single = self._active_final_dir()
            return {os.path.normpath(single)} if single else set()

        by_project_normalized = self._final_subfolder_by_project_normalized()
        dirs = set()
        for key in linked_keys:
            subfolder = by_project_normalized.get(key)
            if not subfolder:
                continue
            dirs.add(os.path.normpath(os.path.join(final_dir, subfolder)))
        return dirs

    def is_texture_set_mapped(self, name, project_key=None):
        """このテクスチャセットに対応するシェーディンググループが
        シーン内に既に存在するかどうかを判定する(副作用として監視を
        再起動しない: Phase 2最適化で save_mapping_only() に変更済み)。

        2026.07.15-01: get_shading_engine_map() が現在アクティブな
        SPプロジェクトにスコープされたため、別プロジェクトの同名
        テクスチャセット(例: 別プロジェクトの "Body")に割り当てられた
        シェーダーを誤って「対応済み」と判定することが無くなった。

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治・論点2案A):
        project_key を新設した。get_known_texture_sets_detailed() が
        同名テクスチャセットを複数プロジェクトから返しうるようになった
        ため、name だけでは一意に特定できない(例: proAの"Body"とproBの
        "Body")。project_key を明示することで、該当プロジェクトの
        シェーダー割当・prefixだけを見て判定する。省略時は従来通り
        self.config["active_project_key"] 基準にフォールバックする
        (後方互換)。
        """
        mapping = self.get_shading_engine_map(project_key=project_key)
        sg_name = mapping.get(name)
        if sg_name and cmds.objExists(sg_name):
            return True, sg_name

        managed_dirs = self._managed_dirs()
        prefix = _export_prefix(self.config, name, project_key=project_key) + "_"
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
                    self.save_shading_engine_mapping(name, found_sg, project_key=project_key)
                return True, found_sg
        return False, None

    def find_orphan_file_nodes(self):
        """managed_dirs配下を参照しているが、既知のどのテクスチャセットの
        prefixとも一致しないfileノード(＝孤立ノード)を列挙する。

        2026.07.21 追加修正(緊急、実機報告関連の点検で発見): 従来は
        _export_prefix(self.config, name) を project_key省略(＝
        active_project_key基準)で呼んでいたため、known(このシーンに
        紐付いた全プロジェクトのテクスチャセット名の和集合)に複数
        プロジェクト分の名前が混在する場合、SP側で今開いていない方の
        プロジェクトの名前には誤ったprefix(_safe_name()による予測値)が
        使われてしまい、実際には対応済みのノードを誤って「孤立」と
        判定する恐れがあった。_match_known_texture_set_project()
        (プロジェクトキーごとに正しいprefixで照合する、Phase 1で導入済み)
        に委譲するよう修正した。
        """
        managed_dirs = self._managed_dirs()
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
            if self._match_known_texture_set_project(base) is None:
                orphans.append(node)
        return orphans

    def detect_quality_by_project(self):
        """シーン内のfileノードが実際に監視フォルダ(プレビュー)と
        Finalフォルダのどちらを参照しているかを、このシーンに紐付いた
        プロジェクトごとに調べる。
        Maya再起動をまたぐとGUIの表示品質ボタンは初期化されるが、
        fileノードのパス自体はシーンファイルに保存されたまま残るため、
        起動直後にここで実態を検出し、ボタンの見た目を合わせることで
        「ボタンはプレビュー表示なのに実際はFinalのまま切り替えられない」
        というズレを防ぐ。

        2026.07.22(表示品質のプロジェクト別管理化): 従来の
        detect_current_quality()(シーン全体で1つの判定結果しか返せず、
        「全体を1つの品質に揃える」という旧仕様を前提にしていた)を、
        プロジェクトごとの辞書を返す形に置き換えた。

        戻り値: {project_key(正規化済み、scene linksが無ければNone):
        True(Finalのみ参照)/False(Previewのみ参照)} の辞書。
        fileノードが無い/両方混在している等で判別できないプロジェクトは
        キー自体を含めない(既定のPreview扱いのまま据え置く)。
        """
        linked_keys = self._linked_sp_project_keys()
        keys_to_check = linked_keys if linked_keys else [None]

        file_nodes = cmds.ls(type="file") or []
        node_dirs = []
        for node in file_nodes:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if not tex_path:
                continue
            node_dirs.append(os.path.normpath(os.path.dirname(tex_path)))

        result = {}
        for key in keys_to_check:
            final_dir = self._final_dir_for_project(key)
            watch_dir = self._watch_dir_for_project(key)
            found_final = any(d == final_dir for d in node_dirs)
            found_watch = any(d == watch_dir for d in node_dirs)
            if found_final and not found_watch:
                result[key] = True
            elif found_watch and not found_final:
                result[key] = False
        return result

    def switch_texture_quality(self, use_final, project_key=None):
        """file ノードを監視フォルダ(プレビュー)⇔Finalフォルダ(高画質)の
        間で明示的に切り替える。両フォルダで書き出しファイル名
        (prefix_suffix.ext)は共通のため、フォルダ部分だけを付け替える。
        自動切り替えは行わない(切り替わったタイミングが分かりにくく
        なることを避けるため、常にGUIのボタン操作からのみ呼び出される)。

        project_key: 2026.07.22(表示品質のプロジェクト別管理化)で追加。
        切り替え対象のSPプロジェクトを明示する。呼び出し元
        (LiveSyncWindow._on_quality_toggled)は、状態バーで選択中の
        作業対象(active_link_id)のsp_project_keyを渡す。

        背景: 従来はシーン全体で1つの品質に揃える仕様で、切り替え先の
        判定にも「今SP側で開いているプロジェクト」という単一の
        グローバルな値(_active_watch_dir()/_active_final_dir())を
        使っていた。複数のSPプロジェクトを1つのシーンで扱う運用
        (天板/脚など)では、この値がMaya側でユーザーが選んでいる
        「作業対象」と一致するとは限らず、意図しない(選んでいない)
        プロジェクトのノードまで一緒に切り替わり・再読込されてしまう
        不具合の原因になっていた。project_keyを必須相当の引数にし、
        指定した1プロジェクト分のノードだけを対象にするよう変更した。
        project_keyを省略した場合(シーンにscene linksが1件も無い、
        従来通りの単一プロジェクト運用)は、後方互換のため旧来の
        「今SP側で開いているプロジェクト」基準にフォールバックする。

        戻り値: 実際に切り替えたノード数。
        """
        # ユーザーがボタンを押した瞬間、直近のタイマー実行(最大3秒前)から
        # SP側でプロジェクトが切り替わっている可能性があるため、ここでも
        # 念のため最新のactive_watch_subfolder/active_final_subfolderを
        # 読み直しておく(_ensure_active_dirs_watchedのタイマー任せだと
        # 最大3秒のズレが生じうるため)。
        self._refresh_dynamic_config()

        linked_keys = self._linked_sp_project_keys()
        if project_key is None and not linked_keys:
            # 後方互換: scene linksが1件も無い(従来通りの単一プロジェクト
            # 運用)場合は、旧来通り「今SP側で開いているプロジェクト」を
            # 対象にする。managed_dirsにwatch_dir/final_dir直下も含めるのは、
            # 旧バージョンでfinal_export_dir/watch_dir直下に書き出された
            # ノードを救済するため(この運用ではプロジェクトが実質1つしか
            # 無いため、直下フォルダを共有しても他プロジェクトとの混線は
            # 起こらない)。
            watch_dir = os.path.normpath(self.config["watch_dir"])
            active_watch_dir = self._active_watch_dir()
            active_watch_dir = os.path.normpath(active_watch_dir) if active_watch_dir else watch_dir
            final_dir = os.path.normpath(self.config.get("final_export_dir") or DEFAULT_CONFIG["final_export_dir"])
            active_final_dir = self._active_final_dir()
            active_final_dir = os.path.normpath(active_final_dir) if active_final_dir else final_dir
            dest_dir = active_final_dir if use_final else active_watch_dir
            managed_dirs = {watch_dir, active_watch_dir, final_dir, active_final_dir}
        else:
            # 複数プロジェクト運用: 指定された1プロジェクトのフォルダ
            # だけをmanaged_dirsに含める。他プロジェクトのフォルダを
            # 意図的に含めないことで、無関係なノードが巻き込まれる
            # (今回修正した)不具合を防ぐ。
            watch_dir_for_this = self._watch_dir_for_project(project_key)
            final_dir_for_this = self._final_dir_for_project(project_key)
            dest_dir = final_dir_for_this if use_final else watch_dir_for_this
            managed_dirs = {watch_dir_for_this, final_dir_for_this}

        nodes = []
        for node in cmds.ls(type="file") or []:
            try:
                tex_path = cmds.getAttr(node + ".fileTextureName")
            except Exception:
                continue
            if tex_path and os.path.normpath(os.path.dirname(tex_path)) in managed_dirs:
                nodes.append(node)

        # 2026.07.23(緊急バグ修正): watch_subfolder_by_project/
        # final_subfolder_by_project の記録内容が(_migrate_legacy_
        # active_subfolder()の過去のバグ等により)不正確な場合、上記の
        # フォルダ一致判定だけでは対象のノードを1件も拾えないことが
        # 実機で確認された。reload_textures()/reload_final_textures()の
        # 「古いサブフォルダ補正」と同じ安全策として、フォルダ一致で
        # 何も見つからなかった場合に限り、このシーンに紐付いた全プロ
        # ジェクトの監視対象フォルダ(_managed_dirs())の中から、ファイル名
        # のprefixが指定プロジェクトの既知テクスチャセットと一致する
        # ノードを拾う経路を追加した。フォルダの記録内容がどうであれ、
        # 「このファイル名は間違いなくこのプロジェクトが書き出した
        # ものだ」という、より確実な手がかりで対象を特定できる。
        if not nodes and project_key is not None:
            normalized_target = _normalize_project_key_for_compare(project_key)
            managed_dirs_all = self._managed_dirs()
            for node in cmds.ls(type="file") or []:
                try:
                    tex_path = cmds.getAttr(node + ".fileTextureName")
                except Exception:
                    continue
                if not tex_path:
                    continue
                if os.path.normpath(os.path.dirname(tex_path)) not in managed_dirs_all:
                    continue
                if self._match_known_texture_set_project(os.path.basename(tex_path)) == normalized_target:
                    nodes.append(node)

        if not nodes:
            self._emit_status(
                "切り替え対象の file ノードが見つかりませんでした"
                "(選択中の作業対象が参照している監視フォルダ・Finalフォルダの"
                "いずれのノードもありません)。"
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
                # 2026.07.17 修正: UDIM対応(uvTilingMode)のfileノードは
                # fileTextureName に実在しない <UDIM> トークン込みの文字列
                # (例: Body_BaseColor.<UDIM>.png)を保持している。これを
                # そのまま os.path.isfile() で存在確認すると、対応する
                # 実タイルファイル(.1001.png 等)が実際には揃っていても
                # 「見つからない」と誤判定され続ける不具合があった。
                # <UDIM> を含む場合は、4桁のタイル番号に対するワイルド
                # カードで少なくとも1タイルが実在するかを確認する。
                if "<UDIM>" in base:
                    udim_glob = new_path.replace("<UDIM>", "[0-9][0-9][0-9][0-9]")
                    if not glob.glob(udim_glob):
                        missing.append(base)
                        continue
                elif not os.path.isfile(new_path):
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
            self._flush_with_settle(switched)
            normalized_key = _normalize_project_key_for_compare(project_key) if project_key else None
            # 2026.07.24(緊急バグ修正): missingが1件でもある(＝切り替え先の
            # 画像が無く一部ノードが旧品質のまま据え置かれた)場合にも
            # quality_by_project へ無条件でuse_finalを立てていたため、
            # UIの品質ボタン/ラベルが「全てFinalに切り替わった」かのように
            # 誤表示していた。missingが無い(=全対象ノードが実際に目的の
            # 品質へ切り替わった、または既にその品質だった)場合のみ
            # フラグを更新する。部分的な失敗はこの下の missing ブロックの
            # ログでユーザーに伝わるため、フラグ自体は直前の状態を維持する
            # (実態より「進んでいる」と偽るより、更新を保留する方が安全)。
            if not missing:
                self.quality_by_project[normalized_key] = use_final
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

    def create_shader_network(self, texture_set_name, channels=None, project_key=None):
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

        2026.07.21(Phase 1, 複数プロジェクト並行対応の根治・論点2案A):
        project_key を新設した。マテリアル一覧が複数プロジェクト分の
        テクスチャセットを同時に表示するようになったため(get_known_
        texture_sets_detailed())、どのプロジェクト向けにシェーダーを
        生成するかを明示できる必要がある。省略時は従来通り
        self.config["active_project_key"] 基準にフォールバックする
        (後方互換)。
        """
        active_key = project_key if project_key is not None else self.config.get("active_project_key")
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
        export_prefix = _export_prefix(self.config, texture_set_name, project_key=active_key)

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
        # file ノードは、固定の watch_dir 直下ではなく該当プロジェクトの
        # サブフォルダを参照するようにする。
        # 2026.07.21(Phase 1): 従来の _active_watch_dir()(単数形、代表
        # 1件のみ)は「今SP側で開いているプロジェクト」に固定されており、
        # project_key で明示的に別プロジェクトを指定してもそちらの
        # フォルダは考慮されなかった。watch_subfolder_by_project から
        # active_key に対応するサブフォルダを直接引くよう変更した。
        # まだそのプロジェクトのプレビュー書き出し実績が無い(エントリが
        # 無い)場合は、後方互換として watch_dir 直下にフォールバックする。
        watch_root = os.path.normpath(self.config.get("watch_dir") or DEFAULT_CONFIG["watch_dir"])
        by_project_normalized = self._watch_subfolder_by_project_normalized()
        normalized_active_key = _normalize_project_key_for_compare(active_key) if active_key else None
        subfolder = by_project_normalized.get(normalized_active_key) if normalized_active_key else None
        watch_dir = os.path.normpath(os.path.join(watch_root, subfolder)) if subfolder else watch_root
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

        self.save_shading_engine_mapping(texture_set_name, sg, project_key=active_key)

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
#
# 2026.07.24(緊急バグ修正・reload時のコールバックリーク): 従来はここで
# 無条件に _window_instance = None としており、reload(maya_live_sync)を
# 実行してから show_ui() を呼ぶと、直前まで生きていた旧インスタンスへの
# 参照が closeEvent() を一切経由せずに破棄されていた。closeEvent() は
# self._scene_callback_ids に登録されたシーンコールバック
# (kAfterOpen/kAfterNew等)を解除する役目を持つため、この経路では
# 解除されないまま新しいインスタンスが作られ、reload+show_ui()を
# 繰り返すたびにコールバック登録が際限なく蓄積していた(ファイル末尾の
# _EXITING_CALLBACK_ID/_UDIM_SCENE_CALLBACK_IDS で既に対策済みの
# 「reloadのたびに重複登録される」バグと同じクラス)。
# それらと同じ、globals()経由でreloadをまたいで前回のインスタンスを
# 引き継ぐパターンを踏襲し、まだ有効な(_shiboken_is_validでtrueな)
# 旧インスタンスがあれば、破棄前にそのシーンコールバックを解除する。
_window_instance = globals().get("_window_instance", None)
if _window_instance is not None:
    try:
        if _shiboken_is_valid(_window_instance):
            for _cb_id in getattr(_window_instance, "_scene_callback_ids", []):
                try:
                    om.MMessage.removeCallback(_cb_id)
                except Exception:
                    pass
    except Exception:
        pass
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

        # UI導線改善(フェーズ1再々実施・ご相談対応): 「プロジェクト連携」は
        # 複数SPプロジェクト対応後、可変長の状態文(不一致時は特に長くなる)
        # とボタン列が1つの枠に詰め込まれ、狭いウィンドウ幅では右側の
        # ボタンが見切れる問題があった。
        #
        # 対応方針(ご相談の結果): 折りたたみ化する。ただし「今どの
        # プロジェクトと繋がっているか」は常時ひと目で分かる必要がある
        # ため、完全に隠すのではなく「要約1行を常時表示 + 詳細操作は
        # 展開時のみ」という構成にする。
        #   - 要約行: 色付きドット(一致=緑/不一致=赤/未設定=黄)+
        #     プロジェクト名(+件数)。クリックで開閉。
        #   - 詳細セクション: 従来通りの状態文・設定ボタン・作業対象
        #     切り替えドロップダウンを縦積みで配置(横並びをやめたため
        #     見切れが起きない)。
        #   - 不一致検知時は自動的に展開する(orphan_section/log_view等の
        #     「エラー検知で自動的に開く」パターンを踏襲)。
        #
        # 既存のウィジェット参照名(self.scene_link_label 等)・シグナル
        # 接続は一切変更していないため、_refresh_scene_link_label() の
        # 判定ロジック自体は変更不要。末尾で要約表示の更新のみ追加する。
        #
        # 実装注意: status_bar_group(「状態」というタイトル付きの外枠)の
        # 内側に入れ子になるため、QGroupBoxではなくQWidgetを使う
        # (二重の枠線を避けるため)。折りたたみの区切りは要約行の
        # クリック可能な見た目自体で表現する。
        link_group = QtWidgets.QWidget()
        link_group_layout = QtWidgets.QVBoxLayout(link_group)
        link_group_layout.setContentsMargins(0, 0, 0, 0)
        link_group_layout.setSpacing(4)

        # 要約行(常時表示・クリックで開閉)。
        # 実装注意: QPushButtonはリッチテキスト(HTMLの<span>等)を
        # サポートしないため、色付きドットは別途QLabelに分離する
        # (QLabelはsetTextFormat(Qt.RichText)で正式にHTML表示できる)。
        summary_row = QtWidgets.QHBoxLayout()
        summary_row.setSpacing(6)
        self.link_summary_dot = QtWidgets.QLabel("●")
        self.link_summary_dot.setTextFormat(QtCore.Qt.RichText)
        self.link_summary_dot.setStyleSheet("color: #888888;")
        self.link_summary_btn = QtWidgets.QPushButton("▸ プロジェクト連携")
        self.link_summary_btn.setFlat(True)
        self.link_summary_btn.setStyleSheet(
            "QPushButton { text-align: left; padding: 2px; border: none; }"
        )
        self.link_summary_btn.setToolTip("クリックで詳細を開閉します。")
        self.link_summary_btn.clicked.connect(self._on_link_summary_clicked)
        summary_row.addWidget(self.link_summary_dot)
        summary_row.addWidget(self.link_summary_btn, stretch=1)
        link_group_layout.addLayout(summary_row)

        # 詳細セクション(既定は折りたたみ)。
        self.link_detail_section = QtWidgets.QWidget()
        link_detail_layout = QtWidgets.QVBoxLayout(self.link_detail_section)
        link_detail_layout.setContentsMargins(4, 4, 0, 0)
        link_detail_layout.setSpacing(6)

        # 行1: シーン ⇔ SPプロジェクトの対応表示(単独行。幅いっぱいで
        # 折り返せるため、不一致時の長文でも横方向の見切れが起きない)。
        self.scene_link_label = QtWidgets.QLabel("(確認中...)")
        self.scene_link_label.setWordWrap(True)
        link_detail_layout.addWidget(self.scene_link_label)

        # 行2: 設定ボタン(横並び対象を無くし単独行に)。
        #
        # UI導線改善(ご相談対応): 自動追従機能(_try_auto_switch_active_link,
        # v1.1.0)の実装により、SP側のプロジェクト切り替えは通常このボタンを
        # 押さなくても作業対象ドロップダウンへ自動的に反映されるように
        # なった。このボタンが必要なのは「まだ一度もこのシーンに登録して
        # いないSPプロジェクトを初めて紐付ける」場合と「__unsaved__の
        # ままシーンに焼き付いてしまった紐付けをやり直す」場合に限られる。
        # 出番が減った操作のため、他の主要ボタン(enable_btn等)と同じ
        # 見た目のまま最上位の視覚的重みを持ち続けるのは導線として不整合
        # だった。色による区別は既存方針により避け、高さを抑えることで
        # 「常用の操作ではない」ことを表現する。
        self.scene_link_btn = QtWidgets.QPushButton("SPプロジェクトを設定")
        self.scene_link_btn.setMaximumHeight(24)
        self.scene_link_btn.setToolTip(
            "今SPで開いているプロジェクトを、このMayaシーンの対応先として"
            "登録します。\n通常はSP側のプロジェクト切り替えを検知して自動的に"
            "作業対象が切り替わるため、このボタンは主に「まだ登録していない"
            "新しいSPプロジェクトを初めて追加する」場合に使います。"
        )
        self.scene_link_btn.clicked.connect(self._on_link_scene_to_sp_project)
        link_detail_layout.addWidget(self.scene_link_btn)

        # 2026.07.19-03(複数SPプロジェクト対応、フェーズ1): 行3:
        # このシーンに登録済みのSPプロジェクト一覧から、今作業している
        # 対象(天板/脚など)を選ぶドロップダウン。天板→脚のように頻繁に
        # 切り替える運用では、都度「設定し直す」で上書きしていた従来の
        # 挙動だと直前の紐付けが消えてしまっていたため、複数件を
        # 保持したまま選択だけを切り替えられるようにする。
        scene_link_switch_row = QtWidgets.QHBoxLayout()
        switch_label = QtWidgets.QLabel("作業対象:")
        # UI導線改善(ご相談対応): 「作業対象:」ラベルを太字にして、
        # 直後のコンボボックスと視覚的に結びつける。
        switch_label_font = switch_label.font()
        switch_label_font.setBold(True)
        switch_label.setFont(switch_label_font)

        # UI導線改善(ご相談対応・案B): 「クリックして出てくることが
        # 分かりにくい」との相談を受け、コンボボックス自体ではなく
        # 外側のQFrameに枠線を付けて「操作エリア全体」を視覚的に
        # ひとかたまりのクリック可能領域として見せる。
        #
        # QComboBox自体にはスタイルシートを一切適用しない。QComboBoxの
        # ような複合ウィジェットは、一部プロパティにでもスタイルシートを
        # 当てると他のサブコントロール(ドロップダウン矢印の描画等)の
        # デフォルト表示まで巻き添えで崩れることがQtの既知の注意点として
        # あり(Mayaのバージョンや PySide2/6 の違いで挙動差が出やすい)、
        # 一度検討した「▾ボタンを隣に追加する案」も標準の矢印と重複して
        # 分かりにくくなる懸念があったため見送った経緯がある。
        # QFrameという別ウィジェットを外側に足すだけなら、
        # QComboBox自体の描画には一切干渉しない。
        combo_frame = QtWidgets.QFrame()
        # UI導線改善(ご相談対応・案B、安全性優先の再修正): スタイルシート
        # のborder指定(palette()構文含む)は実機での見え方を確実に検証
        # できないため、Qt標準のフレーム描画APIに完全に委ねる。
        # StyledPanel + Sunken の組み合わせは、Mayaのテーマに関わらず
        # 「くぼんだ枠」として描画され、ボタンなど他の凹んだ操作領域と
        # 見た目の一貫性が保てる(スタイルシート不使用のため、
        # QComboBox側の描画に影響する余地も一切ない)。
        combo_frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        combo_frame.setFrameShadow(QtWidgets.QFrame.Sunken)
        combo_frame_layout = QtWidgets.QHBoxLayout(combo_frame)
        combo_frame_layout.setContentsMargins(2, 2, 2, 2)

        self.scene_link_combo = QtWidgets.QComboBox()
        self.scene_link_combo.setToolTip(
            "このシーンに登録済みのSPプロジェクトから、今作業している"
            "対象を選びます。\n選択はシーンに保存され、次回このシーンを"
            "開いた時も復元されます。"
        )
        combo_frame_layout.addWidget(self.scene_link_combo)

        # 2026.07.19-03: プログラムからのsetCurrentIndex()呼び出し
        # (_refresh_scene_link_label内での一覧再構築時)で意図せず
        # currentIndexChangedが発火し、再帰的にactive_link_idの書き込みと
        # 再描画が連鎖するのを避けるため、シグナル接続はコンボボックス
        # 構築後にまとめて行い、更新中は _updating_scene_link_combo
        # フラグで一時的に無効化する(_refresh_scene_link_labelを参照)。
        self._updating_scene_link_combo = False
        self.scene_link_combo.currentIndexChanged.connect(self._on_scene_link_combo_changed)
        scene_link_switch_row.addWidget(switch_label)
        scene_link_switch_row.addWidget(combo_frame, stretch=1)

        # 2026.07.22(表示品質のプロジェクト別管理化): 表示品質(Preview/Final)
        # がプロジェクトごとに独立するようになったため、「今ドロップダウンで
        # 選んでいる作業対象は今どちらの品質か」が一目で分かるよう、
        # 作業対象の右側に小さなラベルを追加する。表示品質ボタン自体は
        # このシーン全体ではなく「選択中の作業対象」だけを対象にする点も、
        # このラベルと合わせて理解しやすくなる。
        self.link_quality_label = QtWidgets.QLabel("[Preview]")
        self.link_quality_label.setToolTip(
            "選択中の作業対象(SPプロジェクト)の現在の表示品質です。\n"
            "下の「表示品質」ボタンは、このシーン全体ではなく選択中の"
            "作業対象だけを切り替えます。"
        )
        scene_link_switch_row.addWidget(self.link_quality_label)
        link_detail_layout.addLayout(scene_link_switch_row)

        # UI導線改善: 「追加」「削除」を独立した行にすることで、
        # コンボボックス行の幅を圧迫しないようにする(以前はこの2ボタンが
        # コンボボックスと同じ行にあり、狭い幅では見切れの原因になって
        # いた)。
        scene_link_manage_row = QtWidgets.QHBoxLayout()
        self.scene_link_add_btn = QtWidgets.QPushButton("＋現在のSPプロジェクトを追加")
        self.scene_link_add_btn.setToolTip(
            "今SPで開いているプロジェクトを、このシーンの新しい作業対象"
            "として追加します。\n既存の紐付けは上書きされません。"
        )
        self.scene_link_add_btn.clicked.connect(self._on_add_scene_project_link)
        self.scene_link_remove_btn = QtWidgets.QPushButton("削除")
        self.scene_link_remove_btn.setToolTip(
            "ドロップダウンで選択中の作業対象を、このシーンの紐付けから"
            "削除します。"
        )
        self.scene_link_remove_btn.clicked.connect(self._on_remove_scene_project_link)
        scene_link_manage_row.addWidget(self.scene_link_add_btn, stretch=1)
        scene_link_manage_row.addWidget(self.scene_link_remove_btn)
        link_detail_layout.addLayout(scene_link_manage_row)

        # 2026.07.20(ご要望対応): 「前のバージョンで最初からプロジェクト
        # 連携設定系が見えるようにしてほしい」という要望を受け、詳細
        # セクションの初期表示状態を開いた状態に変更する。折りたたみ
        # 機能自体(クリックで開閉、不一致検知時の自動展開)はそのまま
        # 維持し、初期値のみ変更する。
        # 対応するchevron記号(summary_row側の▸/▾)は、この直後に必ず
        # 呼ばれる _refresh_scene_link_label() -> _update_link_summary()
        # 内で、現在のisVisible()を見て正しく揃えられるため、ここで
        # 個別に書き換える必要はない。
        self.link_detail_section.setVisible(True)
        link_group_layout.addWidget(self.link_detail_section)

        status_bar_layout.addWidget(link_group)

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

        # --- 層1: 最頻操作 ---------------------------------------------
        # UI導線改善(フェーズ1): このウィンドウを開いて最初に目に入り、
        # かつ作業中いちばん多く触る操作を「層1」として最上段に固定し、
        # 他の操作より一回り大きく・高さのあるボタンにする。判断基準は
        # 「1セッション中に何度も押すか(層1)」「初回設定時など、たまに
        # 触る程度か(層2)」「導入時に一度触ればよいか(層3)」の3段階。
        # 色による区別はMayaの配色設定によって破綻しやすいため使わず、
        # サイズと配置順序だけで頻度差を表現する。
        self.enable_btn = QtWidgets.QPushButton("監視（自動反映）: OFF")
        self.enable_btn.setCheckable(True)
        self.enable_btn.setMinimumHeight(36)
        enable_font = self.enable_btn.font()
        enable_font.setBold(True)
        self.enable_btn.setFont(enable_font)
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
        #
        # UI導線改善(ご相談対応): enable_btn(層1・監視ON/OFF)と隣接して
        # 並ぶため、同じ高さ・同じ太さのボタンだと「対になるトグル」に
        # 見えてしまうが、両者は無関係な軸(監視するか/どちらの品質を
        # 見るか)である。色による区別は既存方針(3378行目付近のコメント
        # 参照)により避け、代わりに高さを明示的に enable_btn より
        # 低く抑えることで、層1/層2のサイズ差を意図通り広げる。
        self.quality_btn = QtWidgets.QPushButton("表示品質: プレビュー(リアルタイム)")
        self.quality_btn.setCheckable(True)
        self.quality_btn.setMaximumHeight(24)
        self.quality_btn.setToolTip(
            "ONにすると、SP側で保存時に書き出された高画質版(Finalフォルダ)に"
            "file ノードを切り替えます。OFFに戻すとリアルタイムプレビューに戻ります。"
        )
        self.quality_btn.toggled.connect(self._on_quality_toggled)
        layout.addWidget(self.quality_btn)

        # --- 層2: 初回セットアップ時に主に触る設定 -----------------------
        # UI導線改善(フェーズ1): 監視フォルダ・レンダラーは通常、初回の
        # セットアップウィザード実行時に決めればそれ以降ほとんど変更しない
        # 項目のため、折りたたみ可能なグループ(QGroupBox, checkable)に
        # まとめる。setChecked(True)はQGroupBoxの初期値(未指定時は
        # チェック済み)と同じ値のため、ここではtoggledシグナルは発火
        # しない。sp_live_sync_plugin.py の settings_group と同じ
        # 折りたたみ方式に揃え、SP側・Maya側で操作感覚を統一する。
        self.folder_group = QtWidgets.QGroupBox("同期フォルダ・レンダラー設定（通常は初回のみ）")
        self.folder_group.setCheckable(True)
        self.folder_group.setChecked(True)
        self.folder_group.toggled.connect(self._on_folder_group_toggled)
        form = QtWidgets.QFormLayout(self.folder_group)
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
        layout.addWidget(self.folder_group)

        # --- 層2: 日常的に使う操作 / 層3: 導入時に一度だけの操作 ----------
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

        self.material_table = QtWidgets.QTableWidget(0, 4)
        self.material_table.setHorizontalHeaderLabels(["テクスチャセット", "状態", "シェーディンググループ", "SPプロジェクト"])
        # 2026.07.21(Phase 1, 論点2案A): このシーンに複数のSPプロジェクトが
        # 紐付いている場合、同じテクスチャセット名(例: "Body")が複数
        # プロジェクトから並んで表示されうる。名前だけでは区別できないため、
        # 4列目にプロジェクト名(表示名)を追加し、一覧上で見分けられる
        # ようにした。単一プロジェクト運用時(紐付けが1件以下)はこの列は
        # 常に空欄になる(get_known_texture_sets_detailed() が
        # project_key=None を返すため)。
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

        # UI導線改善(フェーズ1): 「未使用ファイルノード」の確認は、
        # モデル名変更やクリーンアップ作業時にだけ必要になる層3寄りの
        # 操作。通常のマテリアル作成フローでは触らないため、既定では
        # 折りたたんでおき、必要な時だけボタンで開けるようにする。
        self.orphan_toggle_btn = QtWidgets.QPushButton("▸ 使われていないファイルノードを確認")
        self.orphan_toggle_btn.setFlat(True)
        self.orphan_toggle_btn.setToolTip(
            "モデル名やマテリアル名の変更・削除によって、どのマテリアルにも\n"
            "つながらなくなって取り残された file ノードを確認できます。\n"
            "誤削除を防ぐため、このツールが自動で消すことはありません。"
        )
        # Qtのclickedシグナルはbool(チェック状態相当)を引数として渡すため、
        # 引数なしの_on_orphan_toggle_clickedを間に挟み、トグル本体の
        # _set_orphan_section_visible(bool)へは明示的な値のみを渡す。
        # こうすることでクリックのたびに意図しないbool値が force_visible
        # 相当の引数へ流れ込む(常に閉じる方向にしか動かなくなる)事故を
        # 構造的に防ぐ。
        self.orphan_toggle_btn.clicked.connect(self._on_orphan_toggle_clicked)
        mat_layout.addWidget(self.orphan_toggle_btn)

        self.orphan_section = QtWidgets.QWidget()
        orphan_section_layout = QtWidgets.QVBoxLayout(self.orphan_section)
        orphan_section_layout.setContentsMargins(0, 0, 0, 0)
        self.orphan_list = QtWidgets.QListWidget()
        self.orphan_list.setMaximumHeight(100)
        orphan_section_layout.addWidget(self.orphan_list)
        self.refresh_orphan_btn = QtWidgets.QPushButton("使われていないノードを探す")
        self.refresh_orphan_btn.clicked.connect(self._refresh_orphan_list)
        orphan_section_layout.addWidget(self.refresh_orphan_btn)
        self.orphan_section.setVisible(False)
        mat_layout.addWidget(self.orphan_section)

        self._load_values_from_config()

        # Phase 6不具合修正: 起動時にシーンの実際の状態を検出し、
        # 表示品質ボタンをそれに合わせておく(常にプレビュー扱いで
        # 初期化すると、実際はFinalのままの場合に切り替え不能になる)。
        # 2026.07.22(表示品質のプロジェクト別管理化): 品質はプロジェクト
        # ごとの辞書になったため、まず全プロジェクト分をまとめて検出して
        # quality_by_project へ反映し、ボタンの見た目の更新自体は
        # (選択中の作業対象に応じて出し分ける必要があるため)後段の
        # _refresh_scene_link_label() 内の _refresh_quality_display() に
        # 委ねる。
        self.watcher.quality_by_project.update(self.watcher.detect_quality_by_project())

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
        # 2026.07.21 追加(緊急、実機報告): kAfterNew(新規シーン作成)の
        # 場合のみ、未保存シーン用の共有スロットを自動クリアする必要が
        # あるため(_on_scene_changed()のis_new_scene引数、詳細は
        # そちらのdocstring参照)、MSceneMessage.addCallback自体は
        # イベント種別を引数でコールバックへ渡さない仕様のため、
        # kAfterOpen/kAfterNewそれぞれ専用のラムダを介して
        # is_new_sceneを明示的に渡すようにした。
        #
        # 2026.07.24(見落とし修正): 従来はこの2件をリスト内包で一括登録
        # しており、1件目の登録後・2件目の登録中に例外が発生した場合、
        # 1件目のコールバックIDがどこにも保持されずリークする(後段の
        # closeEvent()でも解除できない)経路があった。直後の
        # kAfterCreateReference/kAfterLoadReferenceループと同じスタイル
        # (1件ずつtry/exceptで囲み、成功した分だけappendする)に揃える。
        self._scene_callback_ids = []
        for event_const, is_new_scene in (
            (om.MSceneMessage.kAfterOpen, False),
            (om.MSceneMessage.kAfterNew, True),
        ):
            try:
                self._scene_callback_ids.append(
                    om.MSceneMessage.addCallback(
                        event_const,
                        lambda *_a, _is_new=is_new_scene: self._on_scene_changed(is_new_scene=_is_new))
                )
            except Exception as e:
                print("[maya_live_sync] シーン切り替えコールバックの登録に失敗しました: {0}".format(e))

        # 2026.07.23 追加(実機報告: Reference Editorで別シーンから
        # 参照(reference)として持ち込んだオブジェクトも、UDIMテクスチャが
        # 未割り当てのようにグレー表示される):
        # 参照の読み込みは kAfterOpen/kAfterNew を発火しないため、上記の
        # シーン切替コールバックだけでは救済できない。UDIMプレビュー
        # 再生成(_regenerate_udim_previews_deferred())だけを、参照が
        # 新規作成された時(kAfterCreateReference)・既存の参照を
        # 読み込み直した時(kAfterLoadReference)にも呼ぶ。
        # _on_scene_changed()自体(監視の停止・シーン紐付け表示の更新等)
        # は「現在開いているシーン」が変わったわけではない参照読み込みでは
        # 呼ばない。
        # Mayaのバージョンによってこの定数が無い可能性を考慮し、
        # getattr()で存在確認してから登録する(無ければ静かにスキップし、
        # 他の機能には影響させない)。
        for const_name in ("kAfterCreateReference", "kAfterLoadReference"):
            event_const = getattr(om.MSceneMessage, const_name, None)
            if event_const is None:
                continue
            try:
                self._scene_callback_ids.append(
                    om.MSceneMessage.addCallback(
                        event_const,
                        lambda *_a: self._regenerate_udim_previews_deferred())
                )
            except Exception as e:
                print("[maya_live_sync] {0}コールバックの登録に失敗しました: {1}".format(const_name, e))

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

    def _on_folder_group_toggled(self, checked):
        # UI導線改善(フェーズ1): チェックを外すと中身を折りたたんで
        # (非表示にして)場所を取らないようにする。QGroupBoxの標準動作
        # (チェック解除時に子をdisableするだけ)とは別に、行そのものを
        # 隠すことで見た目もすっきりさせる。
        # sp_live_sync_plugin.py の LiveSyncPanel._on_settings_group_toggled
        # と同じ方式に揃え、SP側・Maya側で操作感覚を統一している。
        form = self.folder_group.layout()
        for row in range(form.rowCount()):
            for role in (QtWidgets.QFormLayout.LabelRole, QtWidgets.QFormLayout.FieldRole):
                item = form.itemAt(row, role)
                if item is not None and item.widget() is not None:
                    item.widget().setVisible(checked)

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

    def _on_link_summary_clicked(self):
        # UI導線改善(ご相談対応): QPushButton.clicked は bool を渡すが、
        # ここでは受け取らずに現在の表示状態を見て自前で反転する。
        # maya_live_sync.py の _on_orphan_toggle_clicked、
        # sp_live_sync_plugin.py の _on_structure_toggle_clicked と
        # 同じ設計(引数なしのクリック専用ハンドラ + 明示的なbool値のみを
        # 受け取る実処理、という分離により、clickedのbool引数が意図せず
        # 表示状態の決定に混入する事故を構造的に防ぐ)。
        self._set_link_detail_visible(not self.link_detail_section.isVisible())

    def _set_link_detail_visible(self, visible):
        self.link_detail_section.setVisible(visible)
        chevron = "▾" if visible else "▸"
        # 現在の要約テキストはボタンのプロパティに保持しておき、
        # 開閉時は先頭のchevron記号だけを付け替える(本文は
        # _refresh_scene_link_label 側で更新される)。
        current_summary = self.link_summary_btn.property("_summary_text") or "プロジェクト連携"
        self.link_summary_btn.setText("{0} {1}".format(chevron, current_summary))

    def _update_link_summary(self, kind, text, auto_expand=False):
        """要約行(常時表示)のドット色と文言を更新する。

        kind: "ok"(一致・緑) / "warn"(不一致・赤) / "neutral"(未設定/
              SP未起動など・黄〜グレー) のいずれか。ドットの色でのみ
              状態を示し、詳細な文言は展開時にしか見せない(要約行を
              長文化させないため、text はここで十分に短く要約した
              ものを渡すこと)。
        auto_expand: True の場合、詳細セクションが閉じていれば自動的に
              開く(不一致など、見落とすと困る状態の時に使う)。
              udim_setup.py の _print() が [WARN]/[NG] 検知時に
              自動でログを展開するのと同じ考え方。

        実装注意: QPushButton(link_summary_btn)はリッチテキストを
        サポートしないため、色は隣接する QLabel(link_summary_dot、
        setTextFormat(Qt.RichText)対応)側のスタイルシートでのみ
        表現する。ボタン側にはHTMLタグを含まないプレーンテキストのみ
        渡す。
        """
        dot_color = {"ok": "#2a9c4a", "warn": "#c94a2a", "neutral": "#c9822a"}.get(kind, "#888888")
        self.link_summary_dot.setStyleSheet("color: {0};".format(dot_color))

        summary_text = "プロジェクト連携  {0}".format(text)
        self.link_summary_btn.setProperty("_summary_text", summary_text)

        if auto_expand and not self.link_detail_section.isVisible():
            self._set_link_detail_visible(True)
        else:
            # 開閉状態はそのまま維持し、chevron記号+要約文言だけ更新する。
            visible = self.link_detail_section.isVisible()
            chevron = "▾" if visible else "▸"
            self.link_summary_btn.setText("{0} {1}".format(chevron, summary_text))

    def _current_quality_target_project_key(self):
        """表示品質ボタンが対象とすべき、現在ドロップダウンで選択中の
        作業対象のsp_project_keyを返す。

        2026.07.22(表示品質のプロジェクト別管理化)で新設。
        作業対象が1件も登録されていない(scene linksが無い、従来通りの
        単一プロジェクト運用)場合はNoneを返す
        (watcher.switch_texture_quality()側でNoneを後方互換のレガシー
        フォールバックとして扱う)。
        """
        payload = _get_scene_project_links()
        links = payload.get("links", [])
        if not links:
            return None
        active_link_id = payload.get("active_link_id")
        selected_link = _find_link(payload, active_link_id) or links[0]
        return selected_link.get("sp_project_key")

    def _refresh_quality_display(self, project_key):
        """選択中の作業対象(project_key)の表示品質を、表示品質ボタンと
        「▸ プロジェクト連携」欄の品質ラベルへ反映する。

        2026.07.22(表示品質のプロジェクト別管理化)で新設。呼び出し元:
        - _refresh_scene_link_label()(自動追従・手動でのドロップダウン
          選択変更の両方でここ経由になる)
        - _on_quality_toggled()(ボタン操作直後の見た目更新)
        """
        is_final = self.watcher.quality_for_project(project_key)
        self.quality_btn.blockSignals(True)
        self.quality_btn.setChecked(is_final)
        self.quality_btn.setText(
            "表示品質: {0}".format("高画質(Final)" if is_final else "プレビュー(リアルタイム)")
        )
        self.quality_btn.blockSignals(False)
        if hasattr(self, "link_quality_label"):
            self.link_quality_label.setText("[{0}]".format("Final" if is_final else "Preview"))

    def _refresh_scene_link_label(self):
        """状態バー上部の「シーン ⇔ SPプロジェクト」表示と、複数SP
        プロジェクト切り替え用ドロップダウンを更新する。

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

        2026.07.19-03(複数SPプロジェクト対応、フェーズ1): 従来は
        「シーンにつき1つの紐付け」しか無かったため、ここでの比較は
        linked_key(唯一の紐付け先)とactive_key(SP側の現在値)を単純に
        比べるだけで済んでいた。複数link対応後は「現在ドロップダウンで
        選択されているlink」に対してのみ同じ比較を行う。一致判定の
        意味自体は変わらない(比較対象が「唯一の紐付け」から「選択中の
        紐付け」に変わっただけ)ため、過去の緊急修正(上記)で得られた
        正規化・タイミングの教訓はそのまま活きる。
        """
        if getattr(self, "_refreshing_scene_link_label", False):
            return
        self._refreshing_scene_link_label = True
        try:
            self.watcher._refresh_dynamic_config()
        finally:
            self._refreshing_scene_link_label = False

        scene_name = _scene_display_name()
        diag = {}
        payload = _get_scene_project_links(_diag=diag)
        if diag.get("fallback"):
            self.watcher._emit_status(
                "警告: 保存済みの複数SPプロジェクト情報を正しく読み取れず、"
                "以前の紐付けから復元しました。追加した作業対象がドロップ"
                "ダウンに反映されない場合は、お手数ですが再度「＋現在の"
                "SPプロジェクトを追加」からやり直してください。"
            )
        links = payload.get("links", [])
        active_link_id = payload.get("active_link_id")
        active_key = _normalize_project_key_for_compare(self.watcher.config.get("active_project_key"))

        self._rebuild_scene_link_combo(links, active_link_id)

        # 2026.07.22(表示品質のプロジェクト別管理化): このメソッドは
        # ドロップダウンの選択が変わるたび(自動追従・手動選択の両方)に
        # 呼ばれるため、ここで選択中の作業対象の表示品質をボタン・ラベルへ
        # 反映しておく。以降の分岐(未設定/未保存/不一致/正常)いずれの
        # 場合でも実行してよい(品質表示は紐付けの状態文言とは独立した情報
        # のため)。
        selected_link_for_quality = _find_link(payload, active_link_id) or (links[0] if links else None)
        selected_key_for_quality = (
            selected_link_for_quality.get("sp_project_key") if selected_link_for_quality else None
        )
        self._refresh_quality_display(selected_key_for_quality)

        if not links:
            self.scene_link_label.setText(
                "このシーン「{0}」のSPプロジェクトは未設定です。".format(scene_name)
            )
            self.scene_link_label.setStyleSheet("color: #c9822a;")
            self.scene_link_btn.setText("SPプロジェクトを設定")
            self.scene_link_btn.setEnabled(True)
            self.scene_link_remove_btn.setEnabled(False)
            self._update_link_summary("neutral", "未設定")
            return

        self.scene_link_remove_btn.setEnabled(True)
        selected_link = _find_link(payload, active_link_id) or links[0]
        linked_key = selected_link.get("sp_project_key")

        if linked_key == "__unsaved__":
            # 2026.07.16(緊急修正): SP側が未保存の段階で紐付けボタンを
            # 押してしまい、"__unsaved__" がそのままシーンに焼き付いた
            # ケース。_on_link_scene_to_sp_project() 側で今後はこの状態
            # での新規紐付けをブロックするが、既に焼き付いてしまった
            # 過去の紐付けは自動修復できないため、再設定を明示的に促す。
            self.scene_link_label.setText(
                "このシーン「{0}」の選択中の作業対象は未保存だったSPプロジェクトに"
                "紐付けられています。SP側で保存後、もう一度"
                "「SPプロジェクトを設定」を押してください。".format(scene_name)
            )
            self.scene_link_label.setStyleSheet("color: #c9822a;")
            self.scene_link_btn.setText("SPプロジェクトを設定し直す")
            self.scene_link_btn.setEnabled(True)
            # 再設定が必要な状態のため、見落とし防止のため自動展開する。
            self._update_link_summary("neutral", "要再設定(未保存だったプロジェクト)", auto_expand=True)
            return

        linked_name = _link_display_name(selected_link)

        if not active_key:
            # SP側が起動していない、またはプロジェクトを何も開いていない。
            # 紐付け自体は設定済みなので、それを踏まえた文言にする
            # (緑=正常でも赤=不一致でもない、独立した状態)。
            self.scene_link_label.setText(
                "このシーン「{0}」の選択中の作業対象は {1} です(SP側は現在"
                "未起動/未検出のため一致確認はできません)。".format(scene_name, linked_name)
            )
            self.scene_link_label.setStyleSheet("color: #888888;")
            self.scene_link_btn.setText("SPプロジェクトを設定し直す")
            self.scene_link_btn.setEnabled(True)
            # SP未起動は異常ではなくよくある状態のため、自動展開はしない。
            self._update_link_summary("neutral", "{0} (SP未起動)".format(linked_name))
            return

        if active_key != linked_key:
            # SP側は今、選択中の作業対象とは別のプロジェクトを開いている。
            # 2026.07.19-03: 複数link対応後は、これは必ずしも「上書き
            # 忘れ」ではなく「単にドロップダウンの選択がSP側と違う」
            # だけの場合もある(天板を選んだままSP側で脚を開いた等)。
            # そのため文言も「選択し直す」ニュアンスに寄せる。
            active_name = _project_display_name(active_key)
            self.scene_link_label.setText(
                "選択中の作業対象「{0}」の対応先は {1} ですが、SP側は今 {2} を"
                "開いています。ドロップダウンの選択もご確認ください。".format(
                    scene_name, linked_name, active_name
                )
            )
            self.scene_link_label.setStyleSheet("color: #c94a2a; font-weight: bold;")
            # 不一致は見落とすとテクスチャがズレたまま作業を続けることに
            # なりかねないため、詳細セクションが閉じていれば自動的に開く
            # (ご相談対応: udim_setup.py の警告時自動展開と同じ考え方)。
            self._update_link_summary(
                "warn", "{0} ⇔ SP側は{1}".format(linked_name, active_name), auto_expand=True
            )
        else:
            self.scene_link_label.setText(
                "このシーン「{0}」 ⇔ SPプロジェクト「{1}」".format(scene_name, linked_name)
            )
            self.scene_link_label.setStyleSheet("color: #2a9c4a;")
            # UI導線改善(ご相談対応): 2件以上登録されている場合のみ件数を
            # 添える(1件の時にまで「(1件登録)」と出すと冗長なため)。
            summary_text = linked_name
            if len(links) >= 2:
                summary_text = "{0} ({1}件登録)".format(linked_name, len(links))
            self._update_link_summary("ok", summary_text)
        self.scene_link_btn.setText("SPプロジェクトを設定し直す")
        self.scene_link_btn.setEnabled(True)

    def _rebuild_scene_link_combo(self, links, active_link_id):
        """複数SPプロジェクト切り替え用ドロップダウンの選択肢を、現在の
        links配列で作り直す。

        setCurrentIndex() 等による選択肢の再構築中に
        currentIndexChanged が発火して _on_scene_link_combo_changed が
        呼ばれると、再帰的にactive_link_idの書き込み→再描画が連鎖して
        しまうため、_updating_scene_link_combo フラグで一時的に
        シグナルハンドラを無効化する。
        """
        self._updating_scene_link_combo = True
        try:
            self.scene_link_combo.clear()
            if not links:
                self.scene_link_combo.setEnabled(False)
                return
            self.scene_link_combo.setEnabled(True)
            selected_index = 0
            for i, link in enumerate(links):
                self.scene_link_combo.addItem(_link_display_name(link) or "(名称未設定)", link.get("id"))
                if link.get("id") == active_link_id:
                    selected_index = i
            self.scene_link_combo.setCurrentIndex(selected_index)
        finally:
            self._updating_scene_link_combo = False

    def _on_scene_link_combo_changed(self, index):
        """ドロップダウンでの作業対象切り替え。_rebuild_scene_link_combo
        によるプログラム的な再構築中は無視する(再入防止)。
        """
        if getattr(self, "_updating_scene_link_combo", False):
            return
        if index < 0:
            return
        link_id = self.scene_link_combo.itemData(index)
        _set_active_link(link_id)
        self._refresh_scene_link_label()

    def _on_add_scene_project_link(self):
        """「＋現在のSPプロジェクトを追加」ボタン: SP側が今開いている
        プロジェクトを、このシーンの新しい作業対象として追加する
        (既存の紐付けは上書きしない)。
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
                "この状態で追加すると「未保存」のままシーンに固定されてしまい、"
                "後で保存しても自動更新されません。\n"
                "SP側で一度「名前を付けて保存」してから、もう一度お試しください。"
            )
            return

        active_name = _project_display_name(active_key)
        label, ok = QtWidgets.QInputDialog.getText(
            self, "Live Sync",
            "この作業対象の名前を入力してください(例: 天板、脚)。\n"
            "空欄のままでもプロジェクト名で登録できます。"
        )
        if not ok:
            return

        new_link = _add_scene_project_link(active_key, label=label.strip() if label else None)
        if new_link is None:
            QtWidgets.QMessageBox.warning(
                self, "Live Sync",
                "このシーンに登録できる作業対象の上限({0}件)に達しています。\n"
                "使わなくなった作業対象をドロップダウンから選択し、"
                "「削除」で整理してからお試しください。".format(_MAX_SCENE_PROJECT_LINKS)
            )
            return
        self.watcher._emit_status(
            "シーン「{0}」にSPプロジェクト「{1}」を追加しました。".format(
                _scene_display_name(), _link_display_name(new_link)
            )
        )
        self._refresh_scene_link_label()

    def _on_remove_scene_project_link(self):
        """「削除」ボタン: ドロップダウンで選択中の作業対象を、このシーン
        の紐付けから削除する。
        """
        index = self.scene_link_combo.currentIndex()
        if index < 0:
            return
        link_id = self.scene_link_combo.itemData(index)
        link_name = self.scene_link_combo.currentText()
        reply = QtWidgets.QMessageBox.question(
            self, "Live Sync",
            "作業対象「{0}」をこのシーンの紐付けから削除します。"
            "よろしいですか？".format(link_name),
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        _remove_scene_project_link(link_id)
        self.watcher._emit_status("作業対象「{0}」を削除しました。".format(link_name))
        self._refresh_scene_link_label()

    def _on_link_scene_to_sp_project(self):
        """「SPプロジェクトを設定」ボタン: SP側が今開いているプロジェクトを
        ワンクリックで現在のMayaシーンの選択中作業対象に紐付ける。

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

        2026.07.19-03(複数SPプロジェクト対応、フェーズ1): 従来は
        「シーンの紐付けを1件だけ上書きする」ボタンだったが、
        _set_current_scene_project_link() が内部的に
        _add_scene_project_link() を呼ぶようになったため、既存の
        紐付けが1件も無ければ新規追加、既に同じSPプロジェクトの
        紐付けがあればそれをアクティブにするだけに変わった
        (複数プロジェクトが並存している状態で誤って他の紐付けを
        消してしまわないようにするため)。複数の作業対象を「追加」
        したい場合は「＋現在のSPプロジェクトを追加」ボタンを使う。
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
            self.watcher._emit_status(
                "紐付けの保存に失敗しました(このシーンに登録できる作業対象の"
                "上限({0}件)に達している可能性があります。使わなくなった"
                "作業対象を「削除」で整理してください)。".format(_MAX_SCENE_PROJECT_LINKS)
            )
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

    def _regenerate_udim_previews_deferred(self):
        """UDIM(複数UVタイル)のfileノードについて、Viewport 2.0の
        タイルプレビュー画像を再生成する。generateAllUvTilePreviews
        (MEL)の実行自体を、Mayaのイベントループへ一度制御を戻した直後
        まで遅延させる。

        2026.07.23 追加(実機報告): シーンを閉じて開き直すとUDIM
        テクスチャを使っているオブジェクトが未割り当てのように
        グレー表示されてしまう不具合の対策として新設。背景は
        _flush_viewport_cache()のコメントを参照
        (Viewport 2.0はUDIMのプレビュー画像をシーンを開いた直後には
        自動生成しない、というAutodesk公式仕様)。

        2026.07.23 再修正(実機再報告: 「シーンを開き直すと直らない」):
        当初は _on_scene_changed() の中でこの再生成処理を同期的に
        (シーンコールバックが発火したその場で)実行していたが、
        実機で改善が確認できなかった。原因は、kAfterOpen/kAfterNew等の
        シーンコールバックが発火する時点では、Mayaのシーン読み込み処理
        自体(DGの評価やシェーディング割り当て等)がまだ完了しきって
        いない場合があるためと考えられる。_on_scene_changed()内の
        「未保存シーンの紐付け引き継ぎ」警告ポップアップが、全く同じ
        理由から既に QtCore.QTimer.singleShot(0, ...) で遅延実行して
        いるのに倣い、ここでも同じパターンを適用した。

        2026.07.23 追加(実機報告: Reference Editorで別シーンから
        参照(reference)を持ち込んだ場合も同じ症状が出る):
        参照の読み込みは kAfterOpen/kAfterNew を一切発火しないため、
        従来の _on_scene_changed() 経由の対策では救済できなかった。
        本メソッドを独立した関数として切り出し、__init__ での
        コールバック登録箇所で kAfterCreateReference/kAfterLoadReference
        からも呼ぶようにした(詳細は __init__ 内のコールバック登録
        コメント参照)。

        2026.07.23-04(根治修正): 実処理(旧・内部関数 _do_regenerate)は
        self を一切使っていなかったため、モジュールレベル関数
        _regenerate_udim_previews() へ切り出した。LiveSyncWindow の
        インスタンスに依存しないモジュールレベルのシーンコールバック
        (ファイル末尾の登録ブロック参照)からも同じ処理を呼べるようにする
        ためで、詳細な経緯はそちらのコメントを参照。
        """
        QtCore.QTimer.singleShot(0, _regenerate_udim_previews)

    def _on_scene_changed(self, is_new_scene=False, *_args):
        """2026.07.15-01: MSceneMessage(kAfterOpen/kAfterNew)から呼ばれる。
        シーンが切り替わったら、直前のシーン向けの監視を安全に停止し、
        新しいシーンの状態(紐付け・一覧)でUIを更新する。監視の自動再開は
        行わない(紐付いたSPプロジェクトが無いまま再開すると、前のシーンの
        設定を引き継いでしまう危険があるため、ユーザーの一手を挟む)。

        is_new_scene: kAfterNew(新規シーン作成)からの呼び出しならTrue、
        kAfterOpen(既存ファイルを開いた)ならFalse。登録元
        (open_setup_wizard()呼び出し箇所付近のコールバック登録)で
        ラムダ経由により明示的に渡される。

        2026.07.21 追加(緊急、実機報告): 複数SPプロジェクトの紐付け
        情報は、未保存シーンでは共有設定ファイル上の固定スロット
        (last_scene_project_links["__unsaved__"])を経由するため、
        直前に別の未保存シーンで設定していた作業対象がそのまま
        引き継がれてしまうことがある(構造的な限界、詳細は
        _is_scene_unsaved() のdocstring参照)。

        2026.07.21 追加修正(緊急、実機報告: 「新規シーンを作成すると
        必ず出る」): 当初はポップアップでの案内のみを行っていたが、
        新規シーン作成のたびに引き継ぎ自体は起き続けるため、ポップアップ
        も毎回表示され続けてしまっていた。「新規シーン(New Scene)」は
        「白紙から始める」という明確なユーザー意図の操作であり、前の
        未保存シーンの紐付けを引き継ぐべき理由が無いため、is_new_scene
        の場合は _clear_unsaved_scene_link_slot() で共有スロットを
        まず自動クリアしてから、通常の更新処理(_refresh_scene_link_
        label()等)を行うようにした。これにより新規シーンでは常に
        「未設定」から始まるため、引き継ぎ自体が起きなくなり、
        ポップアップも(新規シーン作成の直後は)表示されなくなる。
        kAfterOpen(既存ファイルを開いた)の場合もクリア対象にしている
        (次に未保存シーンへ移った際に古い情報を拾わないための予防)。
        ポップアップ機構自体は、上記の自動クリアでは対処しきれない
        経路(例: 既存の未保存シーンのまま複数回SPプロジェクトの紐付け
        操作を行った後、保存せずに別の未保存シーンを開いた場合など)の
        保険として残している。
        シーンコールバックの内部で直接モーダルダイアログ(exec_()で
        ブロックするもの)を出すと、Maya本体のシーン読み込み処理や他の
        プラグインの後続コールバックをブロックする恐れがあるため、
        QTimer.singleShot(0, ...) でMayaのイベントループへ一度処理を
        戻した直後に出すようにしている(この関数自体は即座にreturnする)。
        """
        try:
            if is_new_scene or not _is_scene_unsaved():
                # 新規シーン、または保存済みファイルを開いた場合は
                # 未保存スロットをクリアする(詳細は上記docstring参照)。
                # 「保存済みシーンを開いた場合もクリアする」のは、次に
                # 未保存シーンへ移った際に古い情報を拾わないための予防。
                _clear_unsaved_scene_link_slot()

            if self.watcher.enabled:
                self.watcher.stop(reason="scene_change")
                self.enable_btn.blockSignals(True)
                self.enable_btn.setChecked(False)
                self.enable_btn.setText("監視（自動反映）: OFF")
                self.enable_btn.blockSignals(False)
            self._refresh_scene_link_label()
            self._refresh_material_table()

            # 2026.07.23 追加(実機報告: シーンを閉じて開き直すとUDIM
            # テクスチャを使っているオブジェクトが未割り当てのように
            # グレー表示されてしまう):
            # 詳細・再修正の経緯は _regenerate_udim_previews_deferred()
            # のdocstringを参照(当初ここで同期的にgenerateAllUvTile
            # Previewsを呼んでいたが、シーン読み込み直後は効果が無い
            # ことが実機で再確認されたため、シングルショットタイマー
            # 経由の遅延実行に変更した)。
            self._regenerate_udim_previews_deferred()

            if _is_scene_unsaved():
                try:
                    payload = _get_scene_project_links()
                except Exception:
                    payload = _empty_scene_links_payload()
                if payload.get("links"):
                    # 案内文言はここで組み立てておき、singleShot後の
                    # クロージャに渡す(コールバック終了後にシーンが
                    # さらに切り替わっている可能性を考慮し、表示直前に
                    # 再取得はしない: 「切り替わった直後の状態」を
                    # 案内する目的のため、この時点の情報で十分)。
                    names = ", ".join(
                        _link_display_name(link) or "(不明なプロジェクト)"
                        for link in payload.get("links", [])
                    )
                    QtCore.QTimer.singleShot(0, lambda: self._show_unsaved_scene_link_warning(names))
        except Exception as e:
            # コールバック内で例外を外に漏らすとMaya本体が不安定になる
            # ことがあるため、ここでは握りつぶしてログにのみ残す。
            print("[maya_live_sync] _on_scene_changed でエラー: {0}".format(e))

    def _show_unsaved_scene_link_warning(self, names):
        """_on_scene_changed() から遅延呼び出しされる、未保存シーンでの
        作業対象引き継ぎ案内ポップアップの本体。
        """
        try:
            QtWidgets.QMessageBox.warning(
                self, "Live Sync",
                "このシーンはまだ保存されていません。\n\n"
                "未保存のシーンでは、作業対象(SPプロジェクトの紐付け)を"
                "シーンごとに区別して記憶できないため、直前に別の未保存"
                "シーンで設定していた作業対象「{0}」がこのシーンにも"
                "引き継がれています。\n\n"
                "このシーン専用の作業対象にしたい場合は、状態バーから"
                "紐付けを設定し直してください。一度「名前を付けて保存」"
                "すれば、以降はシーンごとに区別して記憶されます。".format(names)
            )
        except Exception as e:
            print("[maya_live_sync] _show_unsaved_scene_link_warning でエラー: {0}".format(e))

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
        # 2026.07.22(表示品質のプロジェクト別管理化): このシーン全体では
        # なく、今ドロップダウンで選択中の作業対象1件だけを対象にする。
        project_key = self._current_quality_target_project_key()
        switched = self.watcher.switch_texture_quality(checked, project_key)
        if switched:
            self._refresh_quality_display(project_key)
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
        # 2026.07.21(Phase 1, 複数プロジェクト並行対応の根治・論点2案A):
        # get_known_texture_sets()(名前のみのリスト)から
        # get_known_texture_sets_detailed()((名前, project_key,
        # プロジェクト表示名)のリスト)へ切り替えた。同名テクスチャ
        # セットが複数プロジェクトに存在する場合も、行ごとに正しい
        # project_keyでマッピング状況を判定できるようにするため。
        detailed = self.watcher.get_known_texture_sets_detailed()
        self.material_table.setRowCount(len(detailed))
        for row, (name, project_key, project_display_name) in enumerate(detailed):
            mapped, sg_name = self.watcher.is_texture_set_mapped(name, project_key=project_key)
            status = "対応済み" if mapped else "未対応"
            name_item = QtWidgets.QTableWidgetItem(name)
            # project_key(正規化済み、Noneの場合もある)を選択時に
            # 再取得できるよう、0列目アイテムのUserRoleに保持しておく。
            # _on_create_shader_clicked() がここから読み出して
            # create_shader_network()/is_texture_set_mapped() へ渡す。
            name_item.setData(QtCore.Qt.UserRole, project_key)
            self.material_table.setItem(row, 0, name_item)
            self.material_table.setItem(row, 1, QtWidgets.QTableWidgetItem(status))
            self.material_table.setItem(row, 2, QtWidgets.QTableWidgetItem(sg_name or "-"))
            self.material_table.setItem(row, 3, QtWidgets.QTableWidgetItem(project_display_name or "-"))

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
            # 2026.07.21(Phase 1, 論点2案A): _refresh_material_table() が
            # 0列目アイテムのUserRoleに格納したproject_keyを読み出し、
            # 同名テクスチャセットが複数プロジェクトに存在する場合でも
            # 正しいプロジェクト向けにシェーダーを生成できるようにする。
            project_key = item.data(QtCore.Qt.UserRole)
            try:
                self.watcher.create_shader_network(name, channels=channels, project_key=project_key)
                created.append(name)
            except Exception as e:
                failed.append("{0}: {1}".format(name, e))
        self._refresh_material_table()
        if created:
            self.watcher._emit_status("シェーダーを生成しました: {0}".format(", ".join(created)))
        if failed:
            QtWidgets.QMessageBox.warning(self, "Live Sync", "生成に失敗しました:\n" + "\n".join(failed))

    def _on_orphan_toggle_clicked(self):
        # UI導線改善(フェーズ1): QPushButton.clicked は bool(checked相当)を
        # 渡してくるが、ここでは引数を一切受け取らないことで、その値が
        # 意図せず表示状態の決定に混入することを構造的に防ぐ。実際の
        # 表示/非表示の決定は現在の状態を見て自前で反転させる。
        self._set_orphan_section_visible(not self.orphan_section.isVisible())

    def _set_orphan_section_visible(self, visible):
        # udim_setup.py の詳細設定/ログの折りたたみパターンと同じ
        # 「▸/▾ + テキスト書き換え」方式に揃える。visible は必ず明示的な
        # bool値のみを受け取り、シグナルの生の引数を直接渡さない。
        self.orphan_section.setVisible(visible)
        self.orphan_toggle_btn.setText(
            "▾ 使われていないファイルノードを確認" if visible
            else "▸ 使われていないファイルノードを確認"
        )

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
    # 2026.07.24(緊急バグ修正): _window_instanceはQtウィジェットが実際に
    # 破棄された(hideではなく、deleteUIやレイアウトリセット等でC++側の
    # 実体が消えた)後も None に戻らないままだった。この状態で
    # show_ui()を呼ぶと、既に無効なオブジェクトへ.show()/.raise_()を
    # 呼ぶだけで(いずれもbare exceptで握りつぶされる)何も起きず、
    # モジュールをreloadするまでシェルフボタンが無反応になる不具合が
    # あった。既にimport済みの_shiboken_is_valid()で実体の生死を確認し、
    # 破棄済みなら新規生成分岐へフォールバックする。
    if _window_instance is not None and not _shiboken_is_valid(_window_instance):
        _window_instance = None
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
# 2026.07.23-04: UDIMプレビュー再生成(モジュールレベル共通処理)
# ---------------------------------------------------------------------------
#
# 経緯: 従来 LiveSyncWindow._regenerate_udim_previews_deferred() の内部に
# ネストして定義されていた処理(_do_regenerate)を、self を一切使って
# いなかったためモジュールレベル関数として独立させた。LiveSyncWindow が
# 一度も生成されていないMayaセッションでも同じ処理を呼べるようにする
# ためで、詳細な背景はファイル末尾のモジュールレベル・シーンコールバック
# 登録ブロックのコメントを参照。
def _regenerate_udim_previews():
    """UDIM(複数UVタイル)のfileノードについて、Viewport 2.0のタイル
    プレビュー画像を再生成する。呼び出し側(LiveSyncWindowのインスタンス
    メソッド・モジュールレベルのシーンコールバックの両方)が
    QTimer.singleShot(0, ...) 経由で遅延実行することを前提とする
    (シーンコールバックが発火した時点ではシーン読み込み処理自体が
    まだ完了しきっていない場合があるため)。

    2026.07.23-03(緊急修正): generateAllUvTilePreviews は定義元のMEL
    ファイル(others/generateUvTilePreview.mel)が自動ソースされていない
    タイミングでは「プロシージャが見つかりません」というMELエラーで
    失敗することが実機(mayapyバッチセッション)で確認された。呼び出し前に
    明示的にsourceすることで確実にproc定義済みの状態にする。
    """
    try:
        import maya.mel as mel
        mel.eval('source "generateUvTilePreview.mel";')
        mel.eval("generateAllUvTilePreviews;")
    except Exception as e:
        print("[maya_live_sync] UVタイルプレビューの再生成に失敗: {0}".format(e))
    try:
        cmds.refresh(force=True)
    except Exception:
        pass


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


# ---------------------------------------------------------------------------
# 2026.07.23-04(根治修正): UDIMプレビュー再生成のモジュールレベル・
# シーンコールバック
# ---------------------------------------------------------------------------
#
# 背景: 2026.07.23/-02/-03 の3回にわたるUDIM対策(MELの明示的source、
# QTimer.singleShotによる遅延実行、kAfterCreateReference/
# kAfterLoadReferenceの追加)は、いずれも LiveSyncWindow.__init__() 内で
# しか登録されないシーンコールバックに乗っていた。しかし install.py の
# _register_autostart() は register_user_setup(auto_open=False) を
# 使っているため、userSetup.py は Maya起動時に "import maya_live_sync"
# するだけで show_ui()(≒LiveSyncWindow の生成)を呼ばない。
# ユーザーの通常の操作順序である「Mayaを再起動 → (LiveSyncパネルを
# 開く前に)対象シーンを開く」では、この最初の kAfterOpen が発火する
# 時点でリスナーが1つも登録されておらず、UDIMプレビュー再生成が
# 一切実行されないままだった。これが実機報告(3回の対策後も直らない)の
# 真因であり、他の2ファイル(install.py / sp_live_sync_plugin.py /
# udim_setup.py)側には原因が無いことも確認済み。
#
# 対策: LiveSyncWindow の生成有無に関わらず、モジュールが import された
# 時点(＝maya_live_sync が一度でも使われた時点)で無条件にこれらの
# コールバックを登録する。LiveSyncWindow.__init__() 側の既存登録は
# そのまま残す(_on_scene_changed() はUDIM再生成以外にも監視停止・UI
# 更新等の処理を担っているため)。ウィンドウが開いている状態では
# UDIM再生成がモジュールレベル分と合わせて二重に走ることになるが、
# generateAllUvTilePreviews + cmds.refresh は冪等かつ軽量なため実害はない。
#
# reload-safety は直前の kMayaExiting ブロックと同じパターン
# (モジュールグローバルにIDを保持し、reload() のたびに前回分を解除して
# から登録し直す)を踏襲する。
def _on_udim_scene_event(*_args):
    QtCore.QTimer.singleShot(0, _regenerate_udim_previews)


_UDIM_SCENE_CALLBACK_IDS = globals().get("_UDIM_SCENE_CALLBACK_IDS", None)

if _UDIM_SCENE_CALLBACK_IDS:
    for _cb_id in _UDIM_SCENE_CALLBACK_IDS:
        try:
            om.MSceneMessage.removeCallback(_cb_id)
        except Exception as _e:
            print("[maya_live_sync] Could not remove previous UDIM scene callback (id={0}): {1}".format(
                _cb_id, _e))
    print("[maya_live_sync] Removed {0} previous UDIM scene callback(s) before re-registering.".format(
        len(_UDIM_SCENE_CALLBACK_IDS)))

_UDIM_SCENE_CALLBACK_IDS = []
for _const_name in ("kAfterOpen", "kAfterNew", "kAfterCreateReference", "kAfterLoadReference"):
    _event_const = getattr(om.MSceneMessage, _const_name, None)
    if _event_const is None:
        continue
    try:
        _UDIM_SCENE_CALLBACK_IDS.append(
            om.MSceneMessage.addCallback(_event_const, _on_udim_scene_event))
    except Exception as _e:
        print("[maya_live_sync] Could not register module-level {0} callback: {1}".format(_const_name, _e))