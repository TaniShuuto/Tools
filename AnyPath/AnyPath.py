"""
AnyPath.py  -  AnyPath Maya プラグイン本体（Boot層: autoload プラグイン）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §2.2, §2.3, §4.1

採用方式（§2.2）:
    1. インストーラが <Maya アプリケーションディレクトリ>/modules/AnyPath.mod を生成する
    2. .mod が scripts/ と plug-ins/ を検索パスへ追加する
    3. plug-ins/AnyPath.py（本ファイル）を autoload 登録する
    4. initializePlugin() 内でコールバックを登録し、dirmap を設定する

起動層の絶対要件（P4・§2.3、実装必須）:
    - initializePlugin 全体を try/except Exception で包み、いかなる場合も
      Maya の起動を妨げない
    - 登録するすべてのコールバック関数の本体も、個別に try/except Exception
      で包む（実際に例外が出るのはコールバック本体である）
    - 例外時はログへ記録し、モーダルダイアログは出さない。ただし状態表示を
      「エラー停止中」へ変更する（P4とP5の解決ルール。§0.1）
    - コールバックIDをモジュールグローバルに保持し、登録前に既存IDの有無を
      必ず確認して二重登録を防ぐ
    - 起動時にMayaのバージョンを検知する。サポート下限（Maya 2026）未満
      なら登録を行わず、理由をログへ記録して停止する。想定より新しい場合は
      「未検証バージョン」としてログへ警告を残す（機能は動作させる）
    - パスは一切ハードコードせず cmds.internalVar 系から取得する
      （MAYA_APP_DIR のカスタム設定に対応）
    - 同名の .mod が複数の MAYA_MODULE_PATH 上に存在する場合、起動時に
      検出してログと自己診断画面へ警告を出す
    - バッチモードでは UI を一切生成しない。cmds.about(batch=True) で
      分岐し、結果はログのみへ出力する

登録するコールバック（3種で確定・§4.1）:
    - MSceneMessage.kAfterOpen                        : 主たる解決処理
    - MSceneMessage.kAfterImport / kAfterReference     : 追加読込時の解決
    - MSceneMessage.kMayaExiting                       : コールバック解除、ログのフラッシュ

kBeforeOpen は登録しない（§1.3により事前解決が実装不可能）。kBeforeSave も
登録しない（§7.2により保存時相対化を非搭載）。

状態表示メニュー（§6.1・§12-5、実装必須）:
    「メニュー作成処理は本体処理から分離し、本体が停止してもメニューだけは
    残るように実装すること」。このため _create_menu_safely() は
    _initialize_engine() の成否に関わらず必ず呼び出す。

本ファイルは Bridge層（anypath.bridge.* ）と Shell層（anypath.shell.*）を
_run_resolution_pass() / _open_self_diagnosis() から呼び出す形で統合して
いる。Core層と同様、AnyPath.py 自身は薄い配線役に留め、実処理ロジックは
各層のモジュールに置く方針を踏襲する。

再設計計画書 AnyPath_再設計計画書_v1.md も参照。§2 柱1〜3の要点:
    柱1: 全て自動で直った場合は何も見せない（既定を「見せない」にする）。
    柱2: 要確認は「決定」ではなく「確認」（候補比較をやめ、OK/違うの2択）。
    柱3: モード・探索ルート等の概念を統合し、隠す（2モード化・LRU自動管理）。

変更履歴:
    1.4.3 (2026.08.01):
        - ユーザー報告対応（「SourceImagesの中身をすべて別の場所に移動
          させて実験したところ、普通のプロジェクトでは正常動作したが、
          大量にリファレンスが集まっているプロジェクトではMayaが毎回
          必ずクラッシュする」。切り分けの結果「シーンを開こうとすると
          問答無用でクラッシュ」と判明）: 従来、_run_resolution_pass()
          （内部で未解決リファレンスの数だけ reference_repath.
          reload_reference、すなわち cmds.file(loadReference=...) を
          連続実行する）を kAfterOpen コールバックの中から直接・同期的に
          呼んでいた。シーンオープンの完了通知であるkAfterOpenの最中に
          さらに参照操作を行うのはMayaの既知の不安定要因（オープン処理が
          完全に片付いていない状態への再入操作になる）で、リファレンス
          数が少ない通常のプロジェクトでは表面化しなかったが、大量の
          reload_reference連続呼び出しにより確率的にクラッシュしていた
          と考えられる。_on_after_open() / _on_after_import_or_reference()
          を改修し、maya.utils.executeDeferred() で実処理
          （_run_resolution_pass_safely、新設のtry/except保護付き
          ラッパー）を1フレーム後方へ退避させるようにした。これにより
          コールバック自体は即座に完了し、Mayaがオープン処理・参照周りの
          内部状態を完全に片付け終えたアイドルタイミングで解決処理が
          走るようになる。executeDeferred自体が使えない異常環境では
          従来通りの同期実行にフォールバックする。
        - 後方互換性: PATCH。挙動としては解決処理のタイミングが
          「シーンオープン完了と同フレーム」から「その直後のアイドル
          タイミング」へ1フレーム程度遅延するのみで、処理内容・結果は
          変わらない。
    1.4.2 (2026.08.01):
        - ユーザー報告対応（「TESTをリファレンス参照しているTEST2で、
          TEST移動後にTEST2を開いても全て正常だと出てしまう」→
          原因切り分けの結果「先に出てくるMayaの純正機能のリファレンス
          ポップアップをスキップを押さなければ認識できない」と判明）:
          Mayaは参照先ファイルが見つからないシーンを開く際、標準の
          モーダルダイアログ（Reference Not Found）を出し、ユーザーが
          応答するまで MSceneMessage.kAfterOpen 自体が発火しない。
          このためダイアログに気づかず放置すると、AnyPathの解決フロー
          （_run_resolution_pass）が一度も走らないまま「全て正常」に
          見えていた。_suppress_file_reference_prompts() を新設し、
          _initialize_engine() の中（mode!="off"の場合のみ）で
          cmds.file(prompt=False) を設定するようにした。これにより
          この種の確認ダイアログ自体がMayaセッション全体で出なくなり、
          シーンを開いた直後にAnyPathの自動解決フローが即座に走る
          ようになる。
        - 副作用の明記: promptの抑制はMayaセッション全体に効くため、
          AnyPath以外のシーンオープン時にも同種のダイアログが出なく
          なる。uninitializePlugin側では意図的に元の値へ戻していない
          （他ツールが既にFalseにしていた可能性を考慮し、安易な復元は
          避けた）。
        - 後方互換性: PATCH。設定ファイルの形式・既存APIには影響しない。
          挙動差はMaya標準ダイアログの表示有無のみ。
    1.4.1 (2026.08.01):
        - ユーザー報告対応（「適用ボタンを押してもちゃんと反映は
          されるのですが表示が消えない」）: _apply_single_resolution()
          が適用に成功した際、開いたままの要確認パネル
          (_review_panel_instance) があれば review_panel.py 1.4.1で
          新設した ReviewPanel.remove_row() を呼び、その行をUI上から
          取り除くようにした。「これでOK」「複数候補の適用」「違う
          （ファイルを選ぶ）」「すべてこれでOK」は全て
          _apply_single_resolution() を経由するため、この1箇所の修正で
          4操作すべてに効く。_on_reference_reload() も同様に、成功時
          ReviewPanel.remove_reference_row() を呼んで参照シーン
          セクションの行を消すようにした。
        - 後方互換性: PATCH。UI更新はtry/exceptで保護しており、
          パネルが閉じている場合や更新に失敗した場合も適用処理自体の
          成否には影響しない。
    1.4.0 (2026.07.31):
        - ユーザー指摘対応（「そもそもプロジェクトファイルmbなども
          対象なのか」）: 未解決リファレンス(.mb/.ma)にも、テクスチャと
          全く同じCore解決カスケード(S0〜S5)を適用するようにした。
          新設した _resolve_unresolved_references() が
          list_unresolved_references() の結果をCoreの resolve_all() へ
          通し、EXACT/HIGHは自動的に reload_reference() で復旧する。
          REVIEW/FAILEDのみ要確認パネルへ残り、UnresolvedReference.
          candidates に解決候補が入った状態で表示される（review_panel.py
          1.3.0の ReferenceSectionWidget 改修により、テクスチャの行と
          同じ「これでOK／違う」の2択で表示されるようになった）。
          これにより、「マテリアル（テクスチャ）は自動で候補が出るのに
          シーン参照は候補すら出ず毎回手動」という非対称な状態が解消
          された。
        - 後方互換性: MINOR。_run_resolution_pass() の内部処理順序に
          リファレンス解決のCore呼び出しが追加されたが、外部インター
          フェース（コールバック登録・.mod形式・設定ファイル形式）には
          変更なし。
    1.3.0 (2026.07.31):
        - ユーザー指摘対応（「確認は右下の通知からのみしかアクセス
          できない。Scriptエディタの他のエラーや警告に上書きされて
          しまいアクセスできなくなる」）:
          (1) 通知トースト（_maybe_notify_review）を自動で消えない
              仕様に変更した（duration_ms=8000 -> None）。利用者が
              明示的にクリックする（開く）か右クリックする（閉じる）
              まで画面右下に残り続ける。同じ内容のトーストが積み
              重ならないよう、新しい通知を出す前に前回分を閉じるように
              した。
          (2) 自己診断画面（第1階層、「直近に自動で直した内容」
              セクション）に、確認が必要な項目がある場合だけ
              「確認する（n件）」ボタンを表示するようにした
              （diagnosis_panel.py 1.2.0の set_recent_summary 拡張、
              open_review_panel_requested シグナル新設）。
              diagnosis_controller.open_and_populate() に
              on_open_review_panel コールバックを追加し、
              _open_self_diagnosis() から _open_review_panel を渡す
              ようにした。これで通知を見逃した／閉じてしまった後でも、
              メニューの「AnyPath の状態を確認」から自己診断画面を開けば
              いつでも要確認パネルへアクセスできる。
          (3) _open_review_panel() は呼ばれた際に残っている通知トースト
              があれば併せて閉じるようにした（役目が重複するため）。
        - 後方互換性: MINOR。show_non_modal_toast()のduration_ms引数、
          open_and_populate()のon_open_review_panel引数はいずれも
          省略可能なキーワード引数として追加した。
    1.2.1 (2026.07.31):
        - 緊急バグ修正（ユーザー報告: 「探す場所でフォルダを追加すると
          一度ウィンドウが一瞬で再表示され何度選択しても追加されない」）:
          diagnosis_controller.open_and_populate() が探索ルートの追加/
          削除のたびにパネルを閉じて再帰的に自分自身を呼び出しており、
          その再帰呼び出しに渡す config_report が最初にパネルを開いた
          時点のスナップショットのままだったため、新しく作られたパネルにも
          「追加前」の一覧しか表示されなかった（詳細は
          diagnosis_controller.py 1.2.1 の変更履歴を参照）。
          _open_self_diagnosis() から open_and_populate() を呼ぶ際、
          config_report_provider=_load_core_config を渡すよう変更した。
          これにより追加/削除後は _load_core_config() で設定を読み直し、
          既存のパネルの探索ルート表示だけをその場で更新するようになり、
          パネルの開閉自体が発生しなくなった。
        - 後方互換性: PATCH。
    1.2.0 (2026.07.30):
        - 再設計計画書 v1 に基づく大幅改修。
          (1) メニューを「一時的に停止する(off)」「停止を解除して
              再開する(auto)」の2項目から、トグル1項目
              「AnyPathを一時停止する」（チェック可能メニュー項目）へ
              統合した。§2 柱3「モードは実質2つに削減」に対応。
              report_only（検証モード）は日常導線から排除し、環境変数
              ANYPATH_MODE経由でのみ有効化可能な内部モードとした
              （_load_core_config自体は変更なし。UIから触れなくした
              だけであり、既存の環境変数上書き機構はそのまま機能する）。
          (2) _run_resolution_pass() の末尾に要確認パネルの自動起動
              配線を新設した（_maybe_show_review()）。旧実装は
              review_panel.py が存在するにもかかわらず AnyPath.py 側に
              一切の呼び出し配線が無く、要確認が発生しても利用者には
              メニューラベルの件数表示以外に気づく手段が無かった
              （バグに近い未実装状態）。再設計計画書 §3 シナリオBの
              「小さな通知→クリックで2行だけの確認画面」を実現する
              ため、全て自動解決した場合は何も出さず、REVIEW/FAILED/
              未解決参照が1件でもあれば非モーダルの控えめな通知
              （show_non_modal_toast）を出し、クリックで要確認パネルを
              開く導線を実装した。
          (3) 要確認パネルの「これでOK」「違う」「すべてこれでOK」
              各アクションに対応するBridge側ハンドラ
              （_apply_single_review_result 等）を新設し、適用結果を
              ログへ記録するようにした（§6.7の記録要件を要確認パネル
              経由の修復にも適用する）。
        - 後方互換性: MINOR。コールバック登録・.mod形式・設定ファイル
          形式には変更なし。メニュー項目名の変更を伴うため、既存の
          自動化スクリプト等がメニューラベル文字列に依存していた場合は
          影響を受けうる（が、そのような外部依存は想定されていない）。
    1.1.2 (2026.07.31):
        - 緊急バグ修正（ユーザー報告: 「自己診断画面で状態=off、モード=off
          のまま何をしても反応しない」）: メニューの「一時的に停止する
          (off)」(_toggle_off) は _state["mode"]="off" を設定するのみで、
          これを元に戻すメニュー項目が存在しなかった。_state["mode"]が
          "off"のままだと _run_resolution_pass() の冒頭で即returnする
          ため、探索ルート追加・要確認パネル・自動修復など一切の機能が
          動かなくなり、しかも利用者が自力で復帰する手段がUI上に
          無かった（§6.5「メニューから1クリックでoffへ一時切り替え
          できるようにし、実質的な『効果の取り消し』とする」の実装時、
          "取り消しの取り消し"を提供し忘れていた）。
          _toggle_on() を新設し、メニューへ「停止を解除して再開する
          (auto)」項目を追加した。設定ファイルを再読込し、そこに
          書かれているモード（既定はauto）へ復帰する。
        - 後方互換性: PATCH。既存の設定ファイル形式・コールバック仕様・
          .mod形式への影響なし。
    1.1.1 (2026.07.31):
        - 緊急バグ修正（ユーザー報告: 「メニューバー自体が見つからない」）:
          initializePlugin() が maya.api.OpenMaya（新API/OpenMaya2）の
          MFnPlugin を使っているにもかかわらず、Mayaのプラグイン
          ローダーへ「新APIを使う」ことを伝える maya_useNewAPI() マーカー
          関数が無かったため、ローダーが旧API形式の MObject を渡して
          しまい、initializePlugin() 内の
          om2.MFnPlugin(mobject, ...) が
          "TypeError: argument 1 must be OpenMaya.MObject, not MObject"
          で失敗していた。Script Editorには
          "Failed to call script initialize function" とだけ表示され、
          原因が非常に分かりにくい形で発現していた。
          この失敗により initializePlugin() 全体が Maya のプラグイン
          ローダー側の例外ハンドラで止まり、_create_menu_safely() まで
          到達しなかったため、§12-5で要求している「本体が停止しても
          メニューだけは残る」設計が機能していなかった
          （本体側のtry/exceptはinitializePlugin関数の"中"でしか効かず、
          MFnPlugin生成の失敗はそれより前の、Mayaローダー側のC++層で
          発生していたため、Python側のtry/exceptで捕捉できる範囲の
          外側だった）。
          maya_useNewAPI() を追加し、Mayaローダーが新API形式の
          MObject を渡すようにして解消した。
        - 後方互換性: PATCH。既存の設定ファイル形式・コールバック仕様・
          .mod形式への影響なし。
    1.1.0 (2026.07.30):
        - Bridge層（収集・解決・適用・ログ）とShell層（自己診断画面）を
          統合。_on_after_open / _on_after_import_or_reference から
          _run_resolution_pass() を呼び出し、実際に
          collector.collect_all() -> snapshot_after_open() ->
          Gate0 G2観測 -> core.resolve_all() -> applier.apply_resolutions()
          -> logger.log_apply_outcomes() -> 未解決リファレンス/XGen検出 ->
          Gate0 G4観測、という一連の解決フローが動くようにした。
          _open_self_diagnosis() は anypath.shell.diagnosis_panel の
          PySide6画面をanypath.bridge.diagnosis_controller経由で開く
          ように変更し、失敗時は従来のconfirmDialog簡易表示
          （_open_self_diagnosis_fallback）へ後方互換的にフォールバック
          する。_state辞書に last_results/last_outcomes/
          last_unresolved_references/last_xgen_issues を追加。
        - 後方互換性: MINOR。既存の起動シーケンス・コールバック登録・
          .mod形式には変更なし。設定ファイル形式にも変更なし。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §2.2〜§2.4, §4.1, §6.1 に基づき新規作成。
"""

__version__ = "1.4.3"

import os
import sys
import traceback

import maya.api.OpenMaya as om2
import maya.cmds as cmds

# --- サポート対象バージョン（設計書 §1.2: Maya 2026 以降） -----------------
MIN_SUPPORTED_MAYA_VERSION = 2026

# --- プラグイン情報 ----------------------------------------------------------
PLUGIN_VENDOR = "AnyPath"
PLUGIN_VERSION = __version__

# --- モジュールグローバル状態 ------------------------------------------------
# コールバックIDの保持（二重登録防止・確実な解除のため）。
_callback_ids = []

# 現在の動作状態（§6.1の状態表示ラベルに反映される）。
#   "normal"   : 正常動作中
#   "review"   : 要確認項目が残っている（件数は _state["review_count"]）
#   "off"      : 利用者が意図的に停止した
#   "error"    : 例外により機能停止した
#   "report_only" : 検証モードで動作中
_state = {
    "status": "normal",
    "review_count": 0,
    "mode": "auto",
    "last_error": None,
    "maya_version_supported": True,
    "maya_version_verified": True,
    # 直近の解決パスの結果一式（Shell層の要確認パネル・自己診断画面が参照）。
    "last_results": [],
    "last_outcomes": [],
    "last_unresolved_references": [],
    "last_xgen_issues": [],
}

_MENU_NAME = "AnyPathMainMenu"


# --- ログ -------------------------------------------------------------------
def _log(message, level="INFO"):
    """
    最低限のログ出力。詳細な永続ログ（JSON Lines・§6.7）はBridge層/Core層の
    責務だが、Boot層自身の起動・停止イベントはScript Editorへ必ず残す
    （§4章のような複雑なロジックを持たないため、専用ロガーは持たず
    print+cmds.scriptEditorInfoに委ねるシンプルな実装とする）。
    """
    try:
        print("[AnyPath][{0}] {1}".format(level, message))
    except Exception:
        pass


def _safe_call(func, *args, **kwargs):
    """
    §2.3: 「登録するすべてのコールバック関数の本体も、個別に try/except
    Exception で包む」を徹底するための共通ラッパー。
    例外時は状態を "error" へ変更し、メニューラベルを更新する。
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        _state["status"] = "error"
        _state["last_error"] = "{0}: {1}".format(type(e).__name__, e)
        _log("コールバック内で例外が発生し機能を停止しました: {0}\n{1}".format(
            e, traceback.format_exc()), level="ERROR")
        _update_menu_label_safely()
        return None


# --- Mayaバージョン検知（§2.3） ----------------------------------------------
def _detect_maya_version():
    """
    cmds.about(version=True) からMayaバージョン（西暦4桁）を取得する。
    取得できない/解釈できない場合は None を返す。
    """
    try:
        raw = cmds.about(version=True)
        digits = "".join(ch for ch in raw if ch.isdigit())
        # "20260" のように余分な桁が付くことがあるため先頭4桁を使う
        if len(digits) >= 4:
            return int(digits[:4])
    except Exception as e:
        _log("Mayaバージョンの取得に失敗しました: {0}".format(e), level="WARNING")
    return None


def _check_maya_version_support():
    """
    サポート下限（Maya 2026）未満なら登録を行わない。
    想定より新しい場合は「未検証バージョン」として警告するが機能は動作させる。
    戻り値: bool（登録を続行してよいか）
    """
    version = _detect_maya_version()
    if version is None:
        # 取得不能な場合は安全側に倒し、動作は許可しつつ「未検証」扱いとする
        _state["maya_version_verified"] = False
        _log("Mayaバージョンを判定できませんでした。未検証として動作を継続します。",
             level="WARNING")
        return True

    if version < MIN_SUPPORTED_MAYA_VERSION:
        _state["maya_version_supported"] = False
        _log(
            "サポート対象外のMayaバージョンです（検出: {0}、必要: {1}以降）。"
            "AnyPathの登録をスキップします。".format(version, MIN_SUPPORTED_MAYA_VERSION),
            level="ERROR",
        )
        return False

    if version > MIN_SUPPORTED_MAYA_VERSION:
        _state["maya_version_verified"] = False
        _log(
            "未検証のMayaバージョンです（検出: {0}）。動作させますが、"
            "問題があれば自己診断画面から状況を確認してください。".format(version),
            level="WARNING",
        )
    return True


# --- パス取得（ハードコード禁止・§2.3） --------------------------------------
def get_maya_app_dir():
    """MAYA_APP_DIR（カスタム設定を含む）を取得する。ハードコードしない。"""
    return cmds.internalVar(userAppDir=True)


def get_user_scripts_dir():
    return cmds.internalVar(userScriptDir=True)


def get_user_prefs_dir():
    return cmds.internalVar(userPrefDir=True)


def get_documents_dir():
    """
    <ユーザーの Documents>/maya/anypath/ 系のログ・マシン固有設定の出力先
    （§6.3, §6.7）。userAppDir は通常 "<Documents>/maya/" を指すため、
    その親をDocumentsとして扱う。
    """
    app_dir = get_maya_app_dir().rstrip("/\\")
    # userAppDir は ".../Documents/maya/" 形式なのが通常のため、
    # 1階層上をDocumentsとみなす。取得できない場合は app_dir 自体を使う。
    parent = os.path.dirname(app_dir)
    return parent if parent else app_dir


def get_anypath_log_dir():
    """
    設計書 §6.7: 「出力先はユーザーローカルに固定する：
    <ユーザーの Documents>/maya/anypath/logs/」
    """
    app_dir = get_maya_app_dir().rstrip("/\\")
    return app_dir.replace("\\", "/") + "/anypath/logs"


def get_anypath_machine_config_path():
    """§6.3: マシン固有設定 -- <ユーザーの Documents>/maya/anypath/machine.json"""
    app_dir = get_maya_app_dir().rstrip("/\\")
    return app_dir.replace("\\", "/") + "/anypath/machine.json"


# --- 重複 .mod 検出（§2.3, §6.9） -------------------------------------------
def detect_duplicate_mod_files():
    """
    同名の .mod（AnyPath.mod）が複数の MAYA_MODULE_PATH 上に存在するか検出する。
    戻り値: 見つかった AnyPath.mod のフルパス一覧（0件または1件が正常、
    2件以上は重複インストールの警告対象）。
    """
    found = []
    module_path_env = os.environ.get("MAYA_MODULE_PATH", "")
    if not module_path_env:
        return found
    separator = ";" if os.name == "nt" else ":"
    for entry in module_path_env.split(separator):
        entry = entry.strip()
        if not entry:
            continue
        candidate = os.path.join(entry, "AnyPath.mod")
        if os.path.isfile(candidate):
            found.append(candidate.replace("\\", "/"))
    return found


# --- 状態表示メニュー（§6.1、本体処理から分離。必ず作成される） -------------
def _menu_label_for_state():
    """§6.1 の表に従いラベルを決定する。"""
    status = _state["status"]
    if status == "error":
        return "AnyPath (! エラー)"
    if status == "off":
        return "AnyPath (停止中)"
    if _state["mode"] == "report_only":
        return "AnyPath (検証モード)"
    if _state["review_count"] > 0:
        return "AnyPath (確認 {0} 件)".format(_state["review_count"])
    return "AnyPath"


_diagnosis_panel_instance = None


def _open_self_diagnosis(*_args):
    """
    自己診断画面を開く（§6.9）。
    Shell層(anypath.shell.diagnosis_panel.DiagnosisPanel)を
    Bridge層(anypath.bridge.diagnosis_controller)経由で実データに接続する。
    PySide6のimportや接続処理で問題が起きても、confirmDialogベースの
    簡易フォールバックへ落ちるようにし、この操作自体がP4に反して
    Mayaを不安定にしないようにする。
    """
    global _diagnosis_panel_instance
    try:
        from anypath.bridge.diagnosis_controller import open_and_populate

        state_for_panel = dict(_state)
        state_for_panel["version"] = PLUGIN_VERSION
        state_for_panel["duplicate_mods"] = detect_duplicate_mod_files()
        state_for_panel["machine_config_path"] = get_anypath_machine_config_path()

        try:
            from PySide6 import QtWidgets
            main_window_widget = None
            for w in QtWidgets.QApplication.topLevelWidgets():
                if w.objectName() == "MayaWindow":
                    main_window_widget = w
                    break
        except Exception:
            main_window_widget = None

        report = _load_core_config()
        _diagnosis_panel_instance = open_and_populate(
            main_window_widget, state_for_panel, report,
            config_report_provider=_load_core_config,
            on_open_review_panel=lambda mw=main_window_widget: _open_review_panel(mw))
    except Exception as e:
        _log("自己診断画面（PySide6版）の表示に失敗したため、簡易表示へ"
             "切り替えます: {0}".format(e), level="WARNING")
        _open_self_diagnosis_fallback()


def _open_self_diagnosis_fallback(*_args):
    """
    PySide6版の自己診断画面が何らかの理由で開けない場合のフォールバック。
    §6.8のエラーメッセージ規約に沿った最小限の情報のみを提示する。
    """
    lines = [
        "AnyPath 自己診断（簡易表示）",
        "",
        "バージョン: {0}".format(PLUGIN_VERSION),
        "状態: {0}".format(_state["status"]),
        "モード: {0}".format(_state["mode"]),
        "Mayaバージョン検証: {0}".format(
            "検証済み" if _state["maya_version_verified"] else "未検証"),
    ]
    if _state["last_error"]:
        lines.append("直近のエラー: 詳細はログを確認してください。")
    duplicates = detect_duplicate_mod_files()
    if len(duplicates) > 1:
        lines.append("")
        lines.append("[要注意] 複数の AnyPath.mod が検出されました:")
        for d in duplicates:
            lines.append("  - {0}".format(d))
    try:
        cmds.confirmDialog(
            title="AnyPath - Self Diagnosis", message="\n".join(lines), button=["OK"])
    except Exception as e:
        _log("自己診断画面の表示に失敗しました: {0}".format(e), level="WARNING")


# --- 要確認パネル（§6.5）の起動・配線 ----------------------------------------
# 再設計計画書 v1 §2 柱1・柱2、§3 シナリオB: 全て自動で直った場合は何も
# 出さない。要確認が1件でもあれば、控えめな通知（トースト）を出し、
# クリックで初めて要確認パネルを開く。パネル内では「これでOK／違う」の
# 2択のみで完結させる。

_review_panel_instance = None
_review_toast_instance = None


def _maybe_notify_review(results, unresolved_refs):
    """
    _run_resolution_pass() の末尾から呼ばれる。REVIEW/FAILEDの結果、または
    未解決の参照シーンが1件でもあれば、非モーダルの控えめな通知を出す。
    通知が無い＝全て自動で直った、という状態を利用者が意識しなくて済む
    ようにするための唯一の分岐点。

    2026.07.31（ユーザー指摘対応）: 従来はduration_ms=8000で自動的に
    消えていたが、「Scriptエディタの他のエラーや警告に上書きされて
    アクセスできなくなる」という指摘を受け、自動では消えない
    （duration_ms=None）仕様へ変更した。利用者が明示的にクリックする
    まで画面右下に残り続ける。合わせて、この通知を見逃した／閉じて
    しまった場合の代替導線として、自己診断画面（第1階層）にも
    「確認する」ボタンを新設した（_open_self_diagnosis経由。
    diagnosis_panel.py 1.2.0 / diagnosis_controller.py 1.3.0参照）。
    通知はあくまで「気づくきっかけ」であり、唯一のアクセス経路では
    なくなっている。
    """
    if cmds.about(batch=True):
        return

    review_results = [
        r for r in results if r.confidence.value in ("REVIEW", "FAILED")]

    if not review_results and not unresolved_refs:
        return  # §2 柱1: 全て正常なら何も表示しない

    try:
        from PySide6 import QtCore, QtWidgets
        from anypath.shell.review_panel import show_non_modal_toast

        main_window_widget = None
        for w in QtWidgets.QApplication.topLevelWidgets():
            if w.objectName() == "MayaWindow":
                main_window_widget = w
                break

        # 前回シーンを開いた際の通知がまだ残っていれば閉じてから出し直す
        # （同じ内容の通知が画面に積み重なるのを防ぐ）。
        global _review_toast_instance
        if _review_toast_instance is not None:
            try:
                _review_toast_instance.close()
            except Exception:
                pass
            _review_toast_instance = None

        count = len(review_results) + len(unresolved_refs)
        message = (
            "確認をお願いしたい項目が {0} 件あります。\n"
            "クリックで開く（右クリックで閉じる）".format(count))

        toast = show_non_modal_toast(main_window_widget, message, duration_ms=None)
        toast.setCursor(QtCore.Qt.PointingHandCursor)
        toast.mousePressEvent = lambda _event, mw=main_window_widget: (
            _open_review_panel(mw), toast.close())
        toast.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        toast.customContextMenuRequested.connect(lambda _pos: toast.close())
        _review_toast_instance = toast
    except Exception as e:
        _log("要確認の通知表示に失敗しました（自己診断画面から確認できます）: "
             "{0}".format(e), level="WARNING")


def _open_review_panel(main_window_widget=None):
    """
    要確認パネルを開き、直近の解決結果（_state["last_results"]等）を
    流し込む。呼び出し元は3箇所ある:
        1. 通知トーストのクリック（_maybe_notify_review内）
        2. 自己診断画面の「確認する」ボタン（2026.07.31新設。
           diagnosis_controller.open_and_populateのon_open_review_panel
           経由）
        3. 将来メニューへ直接の項目を追加する場合の拡張性のため、
           自己診断画面が無くても直接この関数を叩けば開ける
    """
    global _review_panel_instance, _review_toast_instance
    try:
        from PySide6 import QtWidgets
        from anypath.core.types import Confidence
        from anypath.shell.messages import translate_term
        from anypath.shell.review_panel import ReviewPanel

        # 通知トーストが残っていれば閉じる（要確認パネルを開けば
        # トースト自体の役目は終わるため）。
        if _review_toast_instance is not None:
            try:
                _review_toast_instance.close()
            except Exception:
                pass
            _review_toast_instance = None

        if main_window_widget is None:
            for w in QtWidgets.QApplication.topLevelWidgets():
                if w.objectName() == "MayaWindow":
                    main_window_widget = w
                    break

        panel = ReviewPanel(main_window_widget)

        results = _state.get("last_results", [])
        review_results = [
            r for r in results if r.confidence in (Confidence.REVIEW, Confidence.FAILED)]

        display_names = {}
        for r in review_results:
            # node_key は "フルパス.属性名" 形式。専門用語（ノード名/属性名）
            # を出さず、ファイル名相当だけを表示する（§6.10）。
            base = r.raw_path or r.node_key
            display_names[r.node_key] = base.rsplit("/", 1)[-1] if base else r.node_key

        panel.set_review_items(review_results, display_names)
        panel.set_unresolved_references(_state.get("last_unresolved_references", []))

        panel.candidate_replace_requested.connect(_on_review_confirm)
        panel.browse_requested.connect(_on_review_browse)
        panel.apply_all_recommended_requested.connect(_on_review_apply_all)
        panel.reference_reload_requested.connect(_on_reference_reload)
        panel.export_report_requested.connect(_on_review_export_report)

        panel.show()
        _review_panel_instance = panel
        return panel
    except Exception as e:
        _log("要確認パネルの表示に失敗しました。自己診断画面をご利用ください: "
             "{0}".format(e), level="WARNING")
        return None


def _apply_single_resolution(node_key, new_path):
    """
    要確認パネルで「これでOK」または「違う」でファイルを選び直した1件を、
    実際にノード属性へ適用する（§4.3のエフェメラル修復ルールに従う。
    applier.apply_resolutions と同じ適用経路を1件分だけ通す）。
    戻り値: bool（適用できたか）。

    2026.08.01改修: ユーザー報告「適用ボタンを押してもちゃんと反映は
    されるのですが表示が消えない」への対応。適用に成功した場合、
    開いたままの要確認パネル（_review_panel_instance）が存在すれば
    remove_row() でその行をUIから取り除く。「これでOK」「複数候補の
    適用」「違う（ファイルを選ぶ）」「すべてこれでOK」は全てこの関数を
    経由するため、ここ1箇所の修正で4つの操作すべてに効く。
    """
    try:
        from anypath.bridge.collector import collect_all
        from anypath.bridge.registry import merge_node_registry
        from anypath.bridge.applier import apply_resolutions
        from anypath.bridge.logger import log_apply_outcomes
        from anypath.core.types import Confidence, ResolveResult, Strategy

        report = _load_core_config()
        config = report.config if report is not None else {}
        registry = merge_node_registry(config.get("node_registry", {}))
        collected = collect_all(registry)
        collected_by_key = {c.node_key: c for c in collected}

        if node_key not in collected_by_key:
            return False

        result = ResolveResult(
            node_key=node_key,
            raw_path=collected_by_key[node_key].raw_value,
            confidence=Confidence.HIGH,
            strategy=Strategy.S4_INDEX_MATCH,
            resolved_path=new_path,
        )
        outcomes = apply_resolutions(collected_by_key, [result])
        scene_path = cmds.file(query=True, sceneName=True) or "<unsaved>"
        log_apply_outcomes(scene_path, outcomes)

        # 探索ルートのLRU使用日時を更新する（このファイルがどこから
        # 見つかったかを推定できる場合のみ。§4.3のroot_manager連携）。
        try:
            from anypath.bridge.root_manager import touch_root_used
            machine_config_path = get_anypath_machine_config_path()
            touch_root_used(new_path, machine_config_path)
        except Exception:
            pass

        applied = any(o.applied for o in outcomes)
        if applied and _state["review_count"] > 0:
            _state["review_count"] -= 1
            _update_menu_label_safely()

        if applied and _review_panel_instance is not None:
            try:
                _review_panel_instance.remove_row(node_key)
            except Exception:
                # パネル側の表示更新に失敗しても、適用自体は既に成功して
                # いるので握りつぶす（P4: 個別失敗で全体を止めない）。
                pass

        return applied
    except Exception as e:
        _log("修復の適用に失敗しました: {0}".format(e), level="ERROR")
        return False


def _on_review_confirm(node_key, confirmed_path):
    """「これでOK」ボタン。confirmed_pathはReviewRowが保持する最有力候補。"""
    _apply_single_resolution(node_key, confirmed_path)


def _on_review_browse(node_key):
    """「違う」（またはFAILED行の「ファイルを選ぶ」）ボタン。"""
    try:
        from PySide6 import QtWidgets
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            None, "正しいファイルを選択")
        if path:
            _apply_single_resolution(node_key, path)
    except Exception as e:
        _log("ファイル選択に失敗しました: {0}".format(e), level="WARNING")


def _on_review_apply_all(*_args):
    """「すべてこれでOK」ボタン。REVIEW（候補ありでFAILEDでない）を一括適用する。"""
    from anypath.core.types import Confidence
    results = _state.get("last_results", [])
    for r in results:
        if r.confidence == Confidence.REVIEW and r.candidates:
            _apply_single_resolution(r.node_key, r.candidates[0].abs_path)


def _on_reference_reload(ref_node, new_path):
    """
    2026.08.01改修: テクスチャ側と同様、成功時にパネルの該当行
    （参照シーンセクション）をUIから取り除くようにした
    （ユーザー報告「適用しても表示が消えない」への対応）。
    """
    try:
        from anypath.bridge.reference_repath import reload_reference
        ok, err = reload_reference(ref_node, new_path)
        if not ok:
            _log("参照シーンの読み込みに失敗しました: {0}".format(err), level="WARNING")
            return
        if _state["review_count"] > 0:
            _state["review_count"] -= 1
            _update_menu_label_safely()
        if _review_panel_instance is not None:
            try:
                _review_panel_instance.remove_reference_row(ref_node)
            except Exception:
                pass
    except Exception as e:
        _log("参照シーンの読み込み処理に失敗しました: {0}".format(e), level="ERROR")


def _on_review_export_report(*_args):
    try:
        from anypath.bridge.logger import write_text_report
        from anypath.bridge.applier import ApplyOutcome

        outcomes = _state.get("last_outcomes", [])
        outcomes_as_dicts = [{
            "node": o.node_full_path, "attr": o.attr_name,
            "before": o.before, "after": o.after,
            "strategy": o.strategy.value if o.strategy else None,
            "confidence": o.confidence.value if o.confidence else None,
            "applied": o.applied, "skip_reason": o.skip_reason,
        } for o in outcomes]
        scene_path = cmds.file(query=True, sceneName=True) or "<unsaved>"
        out_path = write_text_report(scene_path, outcomes_as_dicts)
        if out_path:
            cmds.confirmDialog(
                title="AnyPath", message="レポートを書き出しました:\n{0}".format(out_path),
                button=["OK"])
    except Exception as e:
        _log("レポートの書き出しに失敗しました: {0}".format(e), level="WARNING")


def _toggle_off(*_args):
    """§6.5: メニューから1クリックで mode='off' へ一時切り替え（実質的な効果の取り消し）。"""
    _state["status"] = "off"
    _state["mode"] = "off"
    _update_menu_label_safely()
    _log("利用者操作によりモードを off へ切り替えました。")


def _toggle_on(*_args):
    """
    2026.07.31(緊急バグ修正、ユーザー報告: 「自己診断画面で状態がoffのまま
    何もできない」): _toggle_off() で mode="off" にした後、それを元へ戻す
    メニュー項目が存在しなかった。_state["mode"]="off" は
    _run_resolution_pass() の先頭で「offなら即return」する分岐に
    引っかかるため、一度offにすると設定ファイル側がauto/report_onlyの
    ままでも、次にシーンを開いた際に自動的にautoへ復帰することはない
    （_load_core_config()は毎回呼ばれるが、_state["mode"]への反映は
    _run_resolution_pass()の冒頭で行われる。ただし現状の実装では
    「off固定」を意図的に選んだ利用者の状態を勝手に上書きしないよう、
    _toggle_off()相当の明示的な解除操作がなければ抜けられない設計に
    なっていた。§6.5「メニューから1クリックでoffへ一時切り替えできる
    ようにし、実質的な『効果の取り消し』とする」の裏側として、
    元に戻す操作も同様に1クリックで提供する必要があった）。

    設定ファイルを再読込し、そこに書かれているモード（既定はauto）へ
    復帰する。
    """
    report = _load_core_config()
    if report is not None:
        _state["mode"] = report.config.get("mode", "auto")
    else:
        _state["mode"] = "auto"
    _state["status"] = "normal"
    _update_menu_label_safely()
    _log("利用者操作によりモードを {0} へ復帰しました。".format(_state["mode"]))


def _toggle_pause_menu_item(checked):
    """
    再設計計画書 v1 §2 柱3・§4.2: メニューを「一時的に停止する(off)」
    「停止を解除して再開する(auto)」の2項目から、チェック可能な
    トグル1項目「AnyPathを一時停止する」へ統合する。
    checked=True で _toggle_off 相当、False で _toggle_on 相当を呼ぶ。
    """
    if checked:
        _toggle_off()
    else:
        _toggle_on()


_PAUSE_MENU_ITEM_NAME = "AnyPathPauseToggleMenuItem"


def _create_menu_safely():
    """
    §6.1, §12-5: 「メニュー作成処理は本体処理から分離し、本体が停止しても
    メニューだけは残るように実装すること」。この関数自体も例外を飲み込み、
    メニュー生成の失敗が initializePlugin 全体を壊さないようにする。
    バッチモードでは作成しない。

    再設計計画書 v1 §4.2: メニュー項目を2つに削減する。
        1. AnyPath の状態を確認
        2. AnyPath を一時停止する（チェック可能。オン=停止中）
    report_only（検証モード）はここには置かない（§4.2: 「クリエイターの
    日常導線に置く理由がない」。必要な場合は環境変数 ANYPATH_MODE で
    有効化する）。
    """
    if cmds.about(batch=True):
        return
    try:
        if cmds.menu(_MENU_NAME, exists=True):
            cmds.deleteUI(_MENU_NAME)
        main_window = "MayaWindow"
        cmds.menu(
            _MENU_NAME, parent=main_window, tearOff=False,
            label=_menu_label_for_state(),
        )
        cmds.menuItem(
            label="AnyPath の状態を確認", parent=_MENU_NAME,
            command=_open_self_diagnosis,
        )
        cmds.menuItem(divider=True, parent=_MENU_NAME)
        cmds.menuItem(
            _PAUSE_MENU_ITEM_NAME, label="AnyPath を一時停止する",
            parent=_MENU_NAME, checkBox=(_state["mode"] == "off"),
            command=_toggle_pause_menu_item,
        )
        _log("状態表示メニューを作成しました。")
    except Exception as e:
        _log("状態表示メニューの作成に失敗しました: {0}".format(e), level="ERROR")


def _update_menu_label_safely():
    """状態変化時にメニューラベル・一時停止チェック状態を即時更新する（§6.1）。"""
    if cmds.about(batch=True):
        return
    try:
        if cmds.menu(_MENU_NAME, exists=True):
            cmds.menu(_MENU_NAME, edit=True, label=_menu_label_for_state())
        if cmds.menuItem(_PAUSE_MENU_ITEM_NAME, exists=True):
            cmds.menuItem(
                _PAUSE_MENU_ITEM_NAME, edit=True,
                checkBox=(_state["mode"] == "off"))
    except Exception as e:
        _log("メニューラベルの更新に失敗しました: {0}".format(e), level="WARNING")


# --- dirmap 設定（initializePlugin 内で毎回行う・§2.2, §5.2） ---------------
def _apply_dirmap_from_config(path_mappings):
    """
    設定の path_mappings を dirmap のマッピングとして登録する（§5.3 経路A）。
    dirmap はセッション間で保持されないため、起動ごとに必ず設定する。
    """
    try:
        cmds.dirmap(enable=True)
        for mapping in path_mappings:
            frm = mapping.get("from")
            to = mapping.get("to")
            if frm and to:
                cmds.dirmap(mapDirectory=(frm, to))
        _log("dirmap マッピングを {0} 件登録しました。".format(len(path_mappings)))
    except Exception as e:
        _log("dirmap の設定に失敗しました: {0}".format(e), level="ERROR")


# --- Core呼び出し配線（Bridge層の実処理は別ファイルに分離される想定） -------
def _load_core_config():
    """
    anypath.core.config.load_layered_config を用いて設定を読み込む。
    プロジェクト設定・マシン固有設定のパスは、それぞれ現在のMaya
    Projectルート、および <Documents>/maya/anypath/machine.json から取得する。
    """
    try:
        from anypath.core.config import load_layered_config
    except Exception as e:
        _log("anypath.core.config の import に失敗しました: {0}".format(e), level="ERROR")
        return None

    project_root = None
    try:
        project_root = cmds.workspace(query=True, rootDirectory=True)
    except Exception:
        pass

    project_config_path = None
    if project_root:
        project_config_path = project_root.rstrip("/\\").replace("\\", "/") + "/anypath/config.json"

    machine_config_path = get_anypath_machine_config_path()

    report = load_layered_config(
        project_config_path=project_config_path,
        machine_config_path=machine_config_path,
    )
    if report.clamp_events:
        for ev in report.clamp_events:
            _log("設定値をクランプしました: {0}".format(ev), level="WARNING")
    if report.parse_errors:
        for path, err in report.parse_errors:
            _log("設定ファイルの読み込みに失敗しました（既定値にフォールバック）: "
                 "{0} ({1})".format(path, err), level="WARNING")
    return report


def _resolve_unresolved_references(unresolved_refs, config, project_root,
                                    resolve_all_fn, reload_reference_fn):
    """
    2026.07.31新設: 未解決リファレンス(.mb/.ma)にもテクスチャと同じCore
    解決カスケード(S0〜S5)を適用する（ユーザー指摘対応: 「そもそも
    プロジェクトファイルmbなども対象なのか」）。

    unresolved_refs: reference_repath.list_unresolved_references() の
                     戻り値（UnresolvedReferenceのリスト）。
    resolve_all_fn / reload_reference_fn: テスト時の差し替えを容易にする
                     ため、呼び出し元（_run_resolution_pass）からCore/
                     Bridge関数を注入させる。

    戻り値: 新しい [UnresolvedReference, ...]（EXACT/HIGHで自動解決できた
            ものは除外され、REVIEW/FAILEDのみが残る。REVIEWのものは
            candidatesに解決候補が入った状態で返る）。
    """
    if not unresolved_refs:
        return unresolved_refs

    from anypath.core.types import Confidence, ResolveInput

    resolve_inputs = [
        ResolveInput(node_key=ref.ref_node, raw_path=ref.raw_path)
        for ref in unresolved_refs
    ]
    results = resolve_all_fn(resolve_inputs, config, project_root=project_root)
    results_by_key = {r.node_key: r for r in results}

    still_unresolved = []
    auto_fixed_count = 0
    for ref in unresolved_refs:
        result = results_by_key.get(ref.ref_node)
        if result is None:
            still_unresolved.append(ref)
            continue

        if result.confidence.auto_apply and result.resolved_path:
            ok, err = reload_reference_fn(ref.ref_node, result.resolved_path)
            if ok:
                auto_fixed_count += 1
                continue
            # 自動適用に失敗した場合は要確認へフォールバック（P4: 個別失敗で
            # 全体を止めない。候補自体は提示できるので次善策として残す）。
            _log("参照シーンの自動復旧に失敗しました（{0}）。要確認へ回します: "
                 "{1}".format(ref.ref_node, err), level="WARNING")

        ref.candidates = result.candidates
        still_unresolved.append(ref)

    if auto_fixed_count:
        _log("参照シーン {0} 件を自動で直しました。".format(auto_fixed_count))

    return still_unresolved


def _run_resolution_pass(trigger_label):
    """
    主たる解決処理の本体。kAfterOpen / kAfterImport / kAfterReference の
    いずれからも呼ばれる共通フロー（§4.1: 「追加読込時の解決」も同じ
    パイプラインを通す）。

    処理順序（§6.7の記録要件を満たす順序が重要）:
        1. 設定読込・クランプ
        2. dirmap再設定（§2.2: 起動ごとに必ず設定する。ただし実際には
           initializePlugin時に設定済みのため、ここでは設定変更が
           あった場合の再適用として働く）
        3. ノード収集（collector.collect_all）
        4. §6.7: 「kAfterOpenの最初の処理として...スナップショットとして
           記録する」→ ここでの収集直後に snapshot_after_open を呼ぶ
        5. Gate 0 G2観測（実測はしないが、判断材料をログへ記録する）
        6. Core呼び出し（anypath.core.resolve_all）
        7. 属性適用（bridge.applier.apply_resolutions）
        8. 適用結果のログ記録（bridge.logger.log_apply_outcomes）
        9. 未解決リファレンス・XGenパス切れの検出
        10. 状態更新（要確認件数の反映）・メニューラベル更新

    P4: どこか1ステップが失敗しても後続を止めない方針は、この関数の
    呼び出し元（_safe_callでラップされたコールバック）が担保する
    最外殻のtry/exceptに委ねる。ここでは大枠のフローのみを記述する。
    """
    report = _load_core_config()
    if report is None:
        return
    config = report.config
    _state["mode"] = config.get("mode", "auto")

    if _state["mode"] == "off":
        _update_menu_label_safely()
        return

    if _state["status"] != "error":
        _apply_dirmap_from_config(config.get("path_mappings", []))

    from anypath.bridge.registry import merge_node_registry
    from anypath.bridge.collector import collect_all, to_resolve_inputs
    from anypath.bridge.logger import (
        get_log_dir, log_apply_outcomes, snapshot_after_open,
    )
    from anypath.bridge.gate0_probe import observe_g2_dirmap, observe_g4_filepatheditor
    from anypath.bridge.applier import apply_resolutions
    from anypath.bridge.reference_repath import list_unresolved_references, reload_reference
    from anypath.bridge.xgen_detector import detect_xgen_path_issues
    from anypath.core.cascade import resolve_all

    registry = merge_node_registry(config.get("node_registry", {}))
    collected = collect_all(registry)

    # §6.7: kAfterOpenの最初の処理として実測値をスナップショット記録する。
    snapshot_after_open(collected)

    # Gate 0 G2観測（実測はしない。判断材料の記録のみ）。
    try:
        dirmap_enabled = bool(cmds.dirmap(query=True, enable=True))
    except Exception:
        dirmap_enabled = None
    observe_g2_dirmap(
        collected, get_log_dir(), dirmap_enabled, config.get("path_mappings", []))

    project_root = None
    try:
        project_root = cmds.workspace(query=True, rootDirectory=True)
    except Exception:
        pass

    resolve_inputs = to_resolve_inputs(collected)
    results = resolve_all(resolve_inputs, config, project_root=project_root)

    collected_by_key = {c.node_key: c for c in collected}

    def _progress_cb(current, total, node_key):
        _log("進捗: {0}/{1} ({2})".format(current, total, node_key))

    outcomes = apply_resolutions(collected_by_key, results, progress_callback=_progress_cb)

    scene_path = cmds.file(query=True, sceneName=True) or "<unsaved>"
    log_apply_outcomes(scene_path, outcomes)

    # 2026.07.31新設（ユーザー指摘対応: 「そもそもプロジェクトファイルmbなど
    # も対象なのか」）: 未解決リファレンス(.mb/.ma)もテクスチャと全く同じ
    # Core解決カスケード(S0〜S5)に通す。EXACT/HIGHは自動でreload_reference
    # を呼んで復旧し、REVIEW/FAILEDのみ要確認パネルへ候補付きで残す。
    unresolved_refs = _resolve_unresolved_references(
        list_unresolved_references(), config, project_root, resolve_all, reload_reference)
    xgen_issues = detect_xgen_path_issues()

    review_count = sum(
        1 for r in results if r.confidence.value in ("REVIEW", "FAILED")
    ) + len(unresolved_refs)

    _state["review_count"] = review_count
    _state["last_results"] = results
    _state["last_outcomes"] = outcomes
    _state["last_unresolved_references"] = unresolved_refs
    _state["last_xgen_issues"] = xgen_issues

    if xgen_issues:
        _log("XGen のパス切れを {0} 件検出しました（自動修復はしません。"
             "手動確認をお願いします）。".format(len(xgen_issues)), level="WARNING")

    # Gate 0 G4観測（filePathEditor網羅性。頻度を抑えるためモード起動時
    # 一度に限定してもよいが、実装簡略化のためここでは毎回実行する。
    # コスト懸念があれば確認手順書側で頻度を案内する）。
    try:
        observe_g4_filepatheditor(registry, get_log_dir())
    except Exception as e:
        _log("Gate0 G4観測に失敗しました: {0}".format(e), level="WARNING")

    _update_menu_label_safely()
    _log("{0}: 解決 {1} 件、要確認 {2} 件。".format(
        trigger_label, len(results), review_count))

    # 再設計計画書 v1 §2 柱1・§3 シナリオA/B: 全て自動で直った場合は
    # 何も出さない。要確認が1件でもあれば、控えめな通知経由で
    # 要確認パネルを開けるようにする（旧実装にはこの導線自体が
    # 存在しなかった）。
    _maybe_notify_review(results, unresolved_refs)


def _run_resolution_pass_safely(trigger_label):
    """
    2026.08.01新設（ユーザー報告対応: 「大量にリファレンスが集まっている
    プロジェクトではMayaが毎回必ずクラッシュする」）。

    _run_resolution_pass() 自体を try/except で保護してから呼ぶラッパー。
    executeDeferred() 経由で呼ばれるコールバックはMaya本体の通常のエラー
    ハンドリング経路から外れる（コールバック登録時のtry/exceptの外）ため、
    ここで確実に例外を捕まえてログへ残す必要がある（P4: 個別失敗で
    Mayaの安定動作全体を止めない、という既存方針をexecuteDeferred経由の
    実行でも維持するため）。
    """
    try:
        _run_resolution_pass(trigger_label)
    except Exception as e:
        _log("{0}: 解決処理中に例外が発生しました。今回のオープンでは"
             "自動修復をスキップします: {1}\n{2}".format(
                 trigger_label, e, traceback.format_exc()), level="ERROR")


def _on_after_open(*_args):
    """
    kAfterOpen: 主たる解決処理。

    2026.08.01改修（ユーザー報告対応: 「大量にリファレンスが集まっている
    プロジェクトではMayaが毎回必ずクラッシュする」）: 従来は
    _run_resolution_pass() をkAfterOpenコールバックの中から同期的に
    直接呼んでいた。この関数は内部で未解決リファレンスの数だけ
    cmds.file(loadReference=...)（reference_repath.reload_reference）を
    連続実行するが、シーンオープンの完了通知であるkAfterOpenコールバックの
    最中に、さらにcmds.file(loadReference=...)のような参照操作を行うのは
    Mayaの既知の不安定要因である（オープン処理自体がまだ完全に片付いて
    いない状態への再入操作になるため）。リファレンス数が少ない通常の
    プロジェクトでは問題が表面化しなかったが、大量のリファレンスを
    連続でreload_referenceする状況で確率的にクラッシュしていたと考えられる。

    対策として、実処理をmaya.utils.executeDeferred()で1フレーム後方へ
    退避させる。これによりkAfterOpenコールバック自体は何もせず即座に
    完了し、Mayaがオープン処理・リファレンス周りの内部状態を完全に
    片付け終えたアイドルタイミングで、AnyPathの解決処理（reload_reference
    の連続呼び出しを含む）が実行されるようになる。
    """
    try:
        from maya.utils import executeDeferred
        executeDeferred(_run_resolution_pass_safely, "kAfterOpen")
    except Exception as e:
        # executeDeferred自体が使えない異常環境向けのフォールバック
        # （P4: 個別失敗で全体を止めない。同期実行になり従来のリスクは
        # 残るが、AnyPathが完全に無効化されるよりはましという判断）。
        _log("executeDeferredの利用に失敗したため、同期実行にフォール"
             "バックします: {0}".format(e), level="WARNING")
        _run_resolution_pass_safely("kAfterOpen")


def _on_after_import_or_reference(*_args):
    """
    kAfterImport / kAfterReference: 追加読込時の解決。

    2026.08.01改修: _on_after_open()と同じ理由で、こちらも
    executeDeferred()経由の遅延実行に変更した（reference_repath.
    reload_referenceの連続呼び出しをコールバックの外へ退避させるため）。
    """
    if _state["mode"] == "off":
        return
    try:
        from maya.utils import executeDeferred
        executeDeferred(
            _run_resolution_pass_safely, "kAfterImport/kAfterReference")
    except Exception as e:
        _log("executeDeferredの利用に失敗したため、同期実行にフォール"
             "バックします: {0}".format(e), level="WARNING")
        _run_resolution_pass_safely("kAfterImport/kAfterReference")


def _on_maya_exiting(*_args):
    """kMayaExiting: コールバック解除、ログのフラッシュ。"""
    _unregister_all_callbacks()
    _log("Maya終了検知によりコールバックを解除しました。")


# --- コールバック登録・解除（二重登録防止・§2.3） ---------------------------
def _unregister_all_callbacks():
    global _callback_ids
    for cb_id in _callback_ids:
        try:
            om2.MMessage.removeCallback(cb_id)
        except Exception as e:
            _log("コールバック解除に失敗しました: {0}".format(e), level="WARNING")
    _callback_ids = []


def _register_callbacks():
    """
    3種のコールバックを登録する（§4.1）。
    登録前に既存IDの有無を必ず確認して二重登録を防ぐ（プラグインreloadで
    処理がN回走る事故が典型のため）。
    """
    global _callback_ids
    if _callback_ids:
        _log("コールバックは既に登録済みのため、二重登録を回避しました。",
             level="WARNING")
        return

    try:
        cb1 = om2.MSceneMessage.addCallback(
            om2.MSceneMessage.kAfterOpen,
            lambda *a: _safe_call(_on_after_open, *a))
        cb2 = om2.MSceneMessage.addCallback(
            om2.MSceneMessage.kAfterImport,
            lambda *a: _safe_call(_on_after_import_or_reference, *a))
        cb3 = om2.MSceneMessage.addCallback(
            om2.MSceneMessage.kAfterReference,
            lambda *a: _safe_call(_on_after_import_or_reference, *a))
        cb4 = om2.MSceneMessage.addCallback(
            om2.MSceneMessage.kMayaExiting,
            lambda *a: _safe_call(_on_maya_exiting, *a))
        _callback_ids = [cb1, cb2, cb3, cb4]
        _log("コールバックを {0} 件登録しました "
             "(kAfterOpen, kAfterImport, kAfterReference, kMayaExiting)。".format(
                 len(_callback_ids)))
    except Exception as e:
        _log("コールバック登録に失敗しました: {0}".format(e), level="ERROR")
        raise


# --- initializePlugin / uninitializePlugin ----------------------------------
def _initialize_engine():
    """
    initializePlugin の実処理本体。例外はこの関数の外側（initializePlugin）
    で一括して捕捉するが、コールバック登録失敗のような致命的な問題は
    ここでログへ残した上で再送出し、_state を "error" にする。
    """
    is_batch = cmds.about(batch=True)

    if not _check_maya_version_support():
        return  # サポート下限未満: 登録せず理由はログ済み

    duplicates = detect_duplicate_mod_files()
    if len(duplicates) > 1:
        _log("複数の AnyPath.mod が検出されました（重複インストールの疑い）: {0}".format(
            duplicates), level="WARNING")

    _register_callbacks()

    report = _load_core_config()
    if report is not None:
        _state["mode"] = report.config.get("mode", "auto")
        if _state["mode"] != "off":
            _apply_dirmap_from_config(report.config.get("path_mappings", []))
            _suppress_file_reference_prompts()

    if is_batch:
        _log("バッチモードで起動しました。UIは生成しません。")
    _state["status"] = "off" if _state["mode"] == "off" else "normal"


def _suppress_file_reference_prompts():
    """
    2026.08.01新設（ユーザー報告対応: 「先に出てくるMayaの純正機能の
    リファレンスポップアップをスキップを押さなければ認識できない」）。

    Mayaはシーンを開く際、参照先ファイルが見つからないと標準の
    「Reference Not Found」ダイアログ（モーダル）を出す。ユーザーが
    そのダイアログへ応答するまでMSceneMessage.kAfterOpenが発火せず、
    AnyPathの解決フロー（_run_resolution_pass）自体が始まらないため、
    ダイアログに気づかず放置すると「全て正常」に見えてしまっていた
    （実際にはAnyPathがまだ一度も走っていないだけ）。

    cmds.file(prompt=False) を起動時に一度設定することで、この種の
    確認ダイアログをMaya全体で抑制する。以後シーンを開いた際は
    ダイアログでブロックされずにオープン処理が完了し、直後の
    kAfterOpenでAnyPathの自動解決フローが即座に走るようになる。

    副作用の注意: これはAnyPath内部だけでなくMayaセッション全体の
    設定であり、以後は他のツール・手動オープン時も含めて同種の
    確認ダイアログが出なくなる。mode="off"の場合はこの関数自体を
    呼ばない（AnyPathを止めているならMaya標準の挙動も変えない）。
    uninitializePlugin側では意図的に元の値へ戻さない ―― promptの
    値はMayaセッション全体の共有状態であり、他のツールが既に
    Falseにしていた可能性もあるため、AnyPath側で安易にTrueへ戻すと
    別の副作用を生みかねないため。
    """
    try:
        cmds.file(prompt=False)
    except Exception as e:
        _log("参照ダイアログの抑制設定に失敗しました（無視して続行します）: "
             "{0}".format(e), level="WARNING")


def maya_useNewAPI():
    """
    2026.07.31(緊急バグ修正、ユーザー報告): このマーカー関数が無いと、
    Mayaのプラグインローダーは initializePlugin() に旧API
    (maya.OpenMaya / maya.OpenMayaMPx) 用の MObject を渡す。しかし本
    ファイルは import maya.api.OpenMaya as om2 という新API(OpenMaya2)を
    使っているため、om2.MFnPlugin(mobject, ...) に旧API形式のmobjectを
    渡すと "argument 1 must be OpenMaya.MObject, not MObject" という
    TypeError で initializePlugin() 自体が失敗していた
    (Script Editorに "Failed to call script initialize function" と
    表示され、プラグインのロードが失敗 -> _create_menu_safely() まで
    到達せず、メニューが一切作られない、という形で発現した)。

    maya_useNewAPI() の存在をMayaが検出すると、ローダーは
    initializePlugin() / uninitializePlugin() の両方に新API形式の
    MObject を渡すようになる(公式に推奨されている新APIプラグインの
    書き方。中身は空でよく、関数が存在すること自体がシグナルになる)。
    """
    pass


def initializePlugin(mobject):
    """
    Maya autoload プラグインのエントリポイント。
    §2.3: 「initializePlugin 全体を try/except Exception で包み、いかなる
    場合も Maya の起動を妨げない」。
    §12-5: メニュー作成は本体処理の成否に関わらず必ず実行する。
    """
    plugin_fn = om2.MFnPlugin(mobject, PLUGIN_VENDOR, PLUGIN_VERSION)

    try:
        _initialize_engine()
    except Exception as e:
        _state["status"] = "error"
        _state["last_error"] = "{0}: {1}".format(type(e).__name__, e)
        _log("initializePlugin で例外が発生しました。AnyPathは機能停止状態で"
             "起動を継続します: {0}\n{1}".format(e, traceback.format_exc()),
             level="ERROR")

    # 本体処理の成否に関わらず、メニューだけは必ず作成する。
    try:
        _create_menu_safely()
    except Exception as e:
        _log("メニュー作成処理自体で予期しない例外が発生しました: {0}".format(e),
             level="ERROR")


def uninitializePlugin(mobject):
    """
    Maya プラグインのアンロード時エントリポイント。
    コールバックの解除とメニューの破棄を行う。ここも例外で
    Maya の終了・プラグインアンロード自体を妨げてはならない。
    """
    try:
        _unregister_all_callbacks()
    except Exception as e:
        _log("uninitializePlugin でコールバック解除中に例外が発生しました: {0}".format(e),
             level="ERROR")

    try:
        if not cmds.about(batch=True) and cmds.menu(_MENU_NAME, exists=True):
            cmds.deleteUI(_MENU_NAME)
    except Exception as e:
        _log("uninitializePlugin でメニュー削除中に例外が発生しました: {0}".format(e),
             level="WARNING")
