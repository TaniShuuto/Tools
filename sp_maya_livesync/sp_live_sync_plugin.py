"""
sp_live_sync_plugin.py  (Phase 1: GUI版 + Phase 1/2 最適化)
================================================================================
Substance 3D Painter 側プラグイン(B案:自前パイプライン)

Phase 1:
    フォルダパス等の設定をドッキングパネル(GUI)から行えるようにした。

Phase 1 最適化:
    - stack_id を蓄積し、実際に変化のあったテクスチャセットだけを
      プレビュー書き出しの対象に絞り込む。
    - Height チャンネルの書き出し漏れを修正。
    - raw_colorspace_suffixes の設定が反映されていなかった不具合を修正。

Phase 2(構造変化検出・SP側):
    テクスチャセットの追加/削除をプロジェクトを開くたびに検出し、Maya側と
    共有する(詳細は maya_live_sync.py を参照)。

Phase 2 最適化(今回追加分・実機テスト前の自己レビューで発見した不具合修正):
    - known_texture_sets がプロジェクト単位ではなくグローバルな1つの値
      だったため、別のプロジェクトを開くたびに「全セットが削除され、
      全セットが新規追加された」という誤検知が大量発生していた。
      これをプロジェクト(ファイルパス)ごとに分離して記録するよう修正した。
    - busy中(ベイク等)にテクスチャセット一覧を取得すると一時的に不完全な
      状態を「削除」と誤検知する恐れがあったため、busy中はチェックを
      スキップするようにした。
    - 消失を検知しても即座に「削除」と確定せず、2回連続で検出された
      場合のみ確定するように変更し、一時的なちらつき(誤検知)を抑制した。

Phase 3(実機テストのフィードバックを受けた恒久対応 + GUI直感化):
    - save_config() をMaya側と同様「ディスク最新内容とのマージ書き込み」
      方式に変更し、SP/Maya間の保存タイミング次第で相手の変更を上書き
      消去してしまう競合を解消した。
    - active_project_key を共有設定に書き込み、複数のSPプロジェクトを
      横断して作業した場合でも、Maya側が「今開いているプロジェクト」を
      機械的に判別できるようにした(従来は全プロジェクト分が混在表示
      されていた)。
    - 実際に書き出したファイル名から texture_set_export_prefix を記録し、
      Maya側の _safe_name() による予測とのズレ(スペース・日本語等を
      含むテクスチャセット名で発生しうる)を根本的に解消した。
    - 初回セットアップウィザード(QWizard)を追加し、他人に共有した際にも
      フォルダ設定等を対話形式で迷わず行えるようにした。

複数プロジェクト対応(2026.07.14):
    - Final書き出し先を final_export_dir 直下から
      <final_export_dir>/<プロジェクト別サブフォルダ>/ に変更した。
      サブフォルダ名は「ファイル名+ハッシュ6桁」の併用方式
      (_project_subfolder_name())で、人が見て判別できることと、
      別の場所にある同名プロジェクト同士の衝突回避を両立している。
    - Maya側(aiSSボタン)が現在アクティブなプロジェクトのFinalフォルダを
      自動入力できるよう、共有設定に active_final_subfolder を書き込む
      ようにした。
    - Maya側とバージョンがズレたまま(片方だけ更新)で運用してしまい、
      「Finalにサブフォルダが作られない」という不具合として実機で誤認
      されたことがあったため、SP側・Maya側の双方に __version__ を追加。
      Substance Painterの起動時に Python ログへ必ずバージョンを出力する
      ようにした。今後この付近を変更する際は日付を上げること。

2026.07.14-02:
    - Live/Preview も Final と同じく <watch_dir>/<プロジェクト別
      サブフォルダ>/ へ書き出すよう変更した(_do_export の preview分岐)。
      共有設定に active_watch_subfolder を追加し、Maya側が追従できる
      ようにした。
    - 背景: 学校の共用PC環境で、watch_dir 直下に過去の別Windowsユーザー
      が残したファイルが混在していると、NTFSの所有権(ACL)により別
      ユーザーからは読めず(PermissionError)、Maya側でプレビュー
      テクスチャが一切表示されない不具合が実機で確認された。Finalは
      既にプロジェクト別サブフォルダ化されていたため発生しておらず、
      同じ方式をLiveにも適用することで解消した。

2026.07.15-01:
    - texture_set_export_prefix をプロジェクトキーでネストした構造
      (texture_set_export_prefix_by_project)に変更した。以前はテクスチャ
      セット名だけをキーにしたフラットな辞書だったため、別プロジェクトに
      同名のテクスチャセット(例: 複数プロジェクトで共通して使う "Body"
      など)が存在すると、Maya側が誤ったprefixでファイル名を予測して
      しまう可能性があった。Maya側の対応する変更と対になっている。
    - texture_set_shading_engine_map はMaya側専用のデータ(SP側は元々
      書き込んでいない)だったため、SP側のDEFAULT_CONFIGからは削除した。

2026.07.19-01(クラッシュ率上昇対策 フェーズ1):
    背景: プラグイン導入状態でのSPクラッシュ率上昇について調査した結果、
    (1) 保存(Ctrl+S)のたびにFinalが全テクスチャセット・全チャンネル・
    フル解像度で書き出される重い処理であること、(2) 保存直後は
    Painter内部の保存ロックが残っていることがあり、それを検知する
    公式な手段が無いため固定500ms待機に頼っていたこと、(3) ウィンドウを
    閉じるボタンでの終了時、保存直後に仕込まれた遅延タイマーが
    shutdown()側でキャンセルされず、終了処理と重なって発火しうること、
    (4) クラッシュでfinally節が実行されなかった場合にステージング
    フォルダの一時ファイルが残り続けること、の4点が複合的に関与している
    と判断した。本バージョンでは書き出し量そのもの(フェーズ2で対応予定)
    には手を付けず、まず低リスクな安全網のみを導入する:
    - 保存直後の遅延実行を、固定500ms待機から
      substance_painter.exception.ProjectError を捕捉した指数バックオフ
      再試行方式に変更した(_request_final_export/_on_final_export_timer_
      fired/_handle_final_export_locked)。ロック解除が速ければ従来より
      早く反映され、遅い環境でも打ち切らずに再試行する。
    - 上記の遅延実行および「保存連打時にキューされる再実行」を、いずれも
      名前付きQTimer(self._final_export_timer)に統一した。shutdown()から
      明示的に停止できるようにし、self._shutting_down フラグと合わせて、
      終了処理が始まった後は遅延実行が発火しても即座に何もせず戻るように
      した。
    - start_plugin() 起動時に、staging_dir 配下に残る "sp_live_" 接頭辞
      付き一時フォルダのうち一定時間以上前のものを自動削除するように
      した(_cleanup_orphaned_staging_dirs)。過去のクラッシュ等で
      shutil.rmtree()によるfinally節クリーンアップが実行されなかった
      場合のディスク圧迫を、次回起動時に自己回復する。
    - Final書き出し完了時のステータスメッセージに、書き出したファイル数を
      追記した(Preview側と同様の形式に統一し、履歴ログから所要時間・
      件数を追跡しやすくした)。
    - _safe_export() 自体の既定の挙動(request_export()/force_sync_now()
      からの呼び出し)は変更していない。新しいリトライ経路は
      _on_project_locked コールバックを明示的に渡した場合のみ有効になる
      ため、既存の呼び出し元への影響はない。
    フェーズ2(Final書き出しの差分化・頻度の間引き)は別途対応予定。

2026.07.19-02(実機クラッシュ再現テストを受けた追加対応):
    フェーズ1(2026.07.19-01)導入後、「保存から1秒以内にウィンドウを
    閉じるボタンで終了する」操作を3回連続で実施したところ、毎回
    クラッシュレポートが発生することが実機で確認された。ログの
    last_duration_ms(2190ms)から、この時点では書き出し処理自体が
    まだ実行中である(=フェーズ1で対策した「未発火の遅延タイマー」の
    窓ではなく、export_project_textures()呼び出し中、またはそれが
    返った直後の後処理中の窓)と判断した。
    さらに実機での追加確認により、SPネイティブの「書き出し中」ダイアログ
    (キャンセル可能・閉じるボタンをブロック)がexport_project_textures()
    の実行中は既に保護していることが分かった一方、このダイアログが
    消えた後もなお数秒(実測で概ね処理時間相当)は閉じるとクラッシュ
    することが確認された。これは _do_export() 内、export_project_
    textures() が返った後の後処理(ファイルのハッシュ比較・移動、
    check_texture_set_structure()の呼び出し等)がSP側の保護対象外の
    まま実行され続けていることが原因と判断した。
    アプリケーション終了そのものを一時保留する公式なAPIは
    substance_painter.event に存在しない(Export/Project/Shelf系の
    イベントのみで、終了関連のイベントは無い)ため、この窓を完全に
    無くすことはできない。今回は窓の長さそのものを縮める方向で対応する:
    - _unchanged() に、内容比較(MD5ハッシュ、フル読み込みが必要)の前段
      としてファイルサイズだけの軽量な事前チェックを追加した。サイズが
      異なる時点で「変更あり」が確定するため、その場合はハッシュ計算
      (ファイル全体の読み込み)を省略する。判定結果自体は変えない
      純粋な最適化。
    - Final書き出し(preview=False)完了後は check_texture_set_
      structure() を呼ばないようにした。Preview側の同期がテクスチャ
      編集のたびに高頻度で同じ検知を行っているため、Final直後に限って
      省略しても実用上の鮮度低下は小さいと判断した。
    - self.exporting=False にするタイミングを、上記の後処理が全て
      終わった後(finally節の最後)に変更した。従来はfinally節の先頭で
      Falseに戻しており、「exportingを見て警告する」という仕組みを
      仮に用意しても、実際に危険な後処理区間の間は警告が消えてしまう
      構造だった。
    - exporting_changed シグナルを新設し、書き出し開始から上記の後処理
      完了までの全区間、パネルに「閉じないでください」の警告を表示する
      ようにした。クラッシュそのものを防ぐものではなく、人為的な事故を
      減らすための保険。
    根本的な解決(この窓を完全にゼロにすること)は公式APIの制約上難しい
    と考えられるため、フェーズ2(差分化・頻度の間引き)でこの窓に
    入る頻度と、窓の中で処理するファイル数自体を減らす方向で
    引き続き対応する。

2026.07.19-03(フェーズ2: Final書き出しの差分化・頻度の間引き):
    実機テストで「Final同期待ちの警告が消えた後に閉じれば安全」ことが
    確認できた(0/3)一方、「危険な区間そのものは残り続ける(警告表示中は
    3/3でクラッシュ)」ことも確認された。フェーズ1の枠内でできる後処理の
    軽量化はやり切ったため、区間の長さそのもの・区間に入る頻度を
    本質的に下げる、以前から計画していたフェーズ2に着手した。
    - Final専用の差分追跡集合(dirty_stack_ids_final)を新設した。
      Preview用のdirty_stack_idsとは独立して、変更のあったstack_idを
      蓄積し続け、Final書き出しが成功するまで消費されない。
    - このセッションでFinalの全量書き出しに一度も成功していない場合
      (final_baseline_established=False)は、差分の有無に関わらず
      常に全量書き出しにフォールバックする(安全側)。baseline確立後は、
      前回のFinal成功以降に変更のあったテクスチャセットだけを対象にする。
      変更が無い場合はSPネイティブAPIを一切呼ばずに即座にスキップする
      (危険な後処理区間そのものに入らない、最も効果の大きい経路)。
    - 書き出しが失敗(例外・キャンセル)した場合は、対象にしていた差分を
      失わないよう self.dirty_stack_ids_final に差し戻す。
    - プロジェクトを閉じる/切り替えるタイミング(on_project_closing/
      on_project_edition_entered)で上記の差分状態を必ずリセットする。
      これを怠ると、別プロジェクトに切り替えた直後のFinalが誤って
      差分扱いになり、大部分のテクスチャセットが書き出されないまま
      「完了」してしまう恐れがあったため。
    - 保存イベントのクールダウン(final_cooldown_seconds、既定5秒)を
      追加した。前回のFinal完了からこの秒数未満しか経っていない場合、
      次のFinal書き出しをその残り時間まで遅らせる(保存連打時の直列
      キューイングを緩和)。ロック解除待ちの再試行自体にはクールダウンを
      再適用しない(初回要求時に既に考慮済みのため、再試行のたびに
      待ち時間が余計に伸びるのを防ぐ)。
    - 「今すぐ全量同期」ボタンを追加した。差分書き出しの状態がMaya側の
      実ファイルとズレたと感じた場合に、クールダウンを無視して即座に
      全量のFinal書き出しを行う手動の逃げ道。
    - 「Final(高画質)同期待ち」のパネル表示を追加した。要求済みだが
      未完了(クールダウン待ち・ロック解除待ちの再試行を含む)の間、
      表示され続ける。閉じても危険ではないため、書き出し中警告
      (exporting_warning_label)とは異なる配色にしている。
    差分状態とMaya側の実ファイルが将来的にズレる可能性(バグ・手動での
    ファイル削除等)は理論上ゼロにはできないため、上記の手動リセット
    手段(今すぐ全量同期ボタン)を必ず残す設計とした。

2026.07.19-04(フェーズ3: 全般的な軽量化):
    実機テストで、フェーズ1・2導入後も「Final同期待ちの表示中に閉じると
    確定でクラッシュする」という関係自体は変わらず安定して再現すること
    (運用ルールとしては信頼できること)が確認できた。表示中の危険性
    そのものを公式APIの制約なしに無くすことは引き続き難しいと判断し、
    ここからは実装計画のフェーズ3(緊急性は低いが、セッション全体としての
    安定性を底上げする一般的な軽量化)に予定通り着手した。
    - _unchanged() のハッシュ計算を、ファイル全体を一括読み込みする
      方式(f.read())から、1MB単位のチャンクで読みながらMD5を更新する
      方式に変更した(_file_digest())。判定結果(変更あり/なしの結論)は
      従来と完全に同一で、ピークメモリ使用量だけを抑える最適化。
    - last_hashes/last_sizes(_unchanged()用のキャッシュ辞書)を、
      プロジェクトを閉じる/切り替えるタイミングで刈り込むようにした。
      これらはdest_pathをキーにしており、dest_pathはプロジェクト別
      サブフォルダを含む絶対パスのため、他プロジェクトのエントリは
      二度と参照されないまま増え続けるだけだった。長時間・複数
      プロジェクトを横断するセッションでの緩やかなメモリ増加を防ぐ。
      刈り込み後、同じプロジェクトへ戻った際の最初の1回だけ保守的に
      「変更あり」判定になるが、実害は無い(安全側の挙動)。
    - 設定ファイル(save_config())の書き込み頻度も見直したが、既存の
      呼び出し箇所はいずれも「値が実際に変化した場合のみ書き込む」
      ガードが既に入っており、追加の変更は不要と判断した。
"""

# バージョン情報。SP起動時に必ずPythonログへ出力し、「今動いているのが
# どの版か」を即座に確認できるようにする(maya_live_sync.py と同じ方式)。
#
# 2026.07.16-01 緊急修正:
#     _current_project_key() が project.file_path() の戻り値をそのまま
#     信頼していたため、テンプレートから新規プロジェクトを作成した
#     直後(まだ一度も保存していない状態)に、公式ドキュメントの契約
#     ("未保存ならNoneを返す")に反してテンプレートファイル(.spt)の
#     パスが返ってくるケースで、それをプロジェクトキーとして採用して
#     しまう不具合があった。Maya側の紐付けUIに実際のプロジェクト名
#     ("TEST_1"等)ではなくテンプレート名が表示される、という形で
#     顕在化した。
#     対策として、公式に「未保存なら確実にNoneを返す」と明記されている
#     project.name() を先に確認し、これがNoneであれば file_path() の
#     値を信頼しない(必ず "__unsaved__" にフォールバックする)よう
#     二段構えの判定に変更した。
#
# 2026.07.16-02 緊急修正:
#     v2026.07.16-01の修正(project.name()での二段構え判定)は「未保存の
#     間に誤ったキーを掴む」ケースは解決したが、別の見落としが残って
#     いた: on_project_edition_entered() はプロジェクトを開いた/作成
#     した"その瞬間"の active_project_key しか記録せず、その後
#     実際に名前を付けて保存しても再計算されない。一方 _do_export()
#     内の active_watch_subfolder はエクスポートのたびに都度再計算
#     されるため、「監視フォルダ名(TEST_1_5b5599等)は正しいのに、
#     Maya側の状態バーの紐付け表示だけがテンプレート名や__unsaved__の
#     まま」という矛盾が実機で確認された。
#     対策として、on_project_saved() でも active_project_key を
#     明示的に再計算・保存するようにした。
#
# 2026.07.19-01 クラッシュ率上昇対策フェーズ1:
#     保存直後の固定500ms待機を「ProjectError捕捉+指数バックオフ再試行」
#     に置き換え、終了処理時に遅延タイマーが取り残される問題を解消し、
#     起動時にステージングフォルダの残骸を自動回収するようにした。
#     詳細は本ファイル冒頭のdocstring「2026.07.19-01」の項を参照。
#
# 2026.07.19-02 実機クラッシュ再現テストを受けた追加対応:
#     書き出し完了ダイアログが消えた後の後処理区間(SP側の保護対象外)を
#     縮め、その区間を通してUIに警告を出すようにした。
#     詳細は本ファイル冒頭のdocstring「2026.07.19-02」の項を参照。
#
# 2026.07.19-03 フェーズ2(Final書き出しの差分化・頻度の間引き):
#     Final書き出しを、前回成功以降に変更のあったテクスチャセットだけに
#     絞り込むようにし(初回は全量、失敗時は差分を保持して次回へ持ち越す)、
#     保存連打を間引くクールダウンを追加した。「今すぐ全量同期」ボタンで
#     差分状態を手動リセットできる。詳細は本ファイル冒頭のdocstring
#     「2026.07.19-03」の項を参照。
#
# 2026.07.19-04 フェーズ3(全般的な軽量化):
#     ハッシュ計算をチャンク単位の読み込みに変更してピークメモリ使用量を
#     抑え、_unchanged()用のキャッシュ辞書をプロジェクト切替時に刈り込む
#     ようにした。設定ファイルI/Oの頻度も見直したが、既存の書き込みは
#     いずれも値が変化した場合のみ実行されるよう既にガードされており、
#     追加の変更は不要と判断した。詳細は本ファイル冒頭のdocstring
#     「2026.07.19-04」の項を参照。
# 2026.07.20: バージョン表記をセマンティックバージョニング(MAJOR.MINOR.PATCH)
# へ移行。ツール群として初めて正式にバージョン番号を割り当てる区切りとして
# 1.0.0 からスタートする(このコミット以前は日付ベースの独自表記
# "2026.07.19-04" だった。旧番号との対応はREADME.mdの「バージョン履歴」
# 節を参照)。以降は SemVer のルールに従う(maya_live_sync.py側と同じ基準):
#   MAJOR: 設定ファイル形式の変更など、既存環境で互換性が崩れる変更
#   MINOR: 後方互換のある機能追加
#   PATCH: 後方互換のあるバグ修正
__version__ = "1.0.0"

import os
import re
import json
import time
import shutil
import hashlib
import tempfile
import datetime

import substance_painter.event as event
import substance_painter.export as export
import substance_painter.project as project
import substance_painter.textureset as textureset
import substance_painter.logging as sp_log
import substance_painter.ui as ui
import substance_painter.exception as sp_exception

# Substance Painter 10.1 以降は内部UIがPySide6へ移行しており、
# PySide2 モジュールが存在しない環境がある(逆に10.1未満はPySide6が無い)。
# そのため両対応させるためのフォールバックを行う。
try:
    from PySide2 import QtCore, QtWidgets, QtGui
except ImportError:
    from PySide6 import QtCore, QtWidgets, QtGui


# ---------------------------------------------------------------------------
# 設定の保存先(GUIから編集されるため、通常は直接触らなくてよい)
# ---------------------------------------------------------------------------

CONFIG_DIR = "C:/SPMayaLiveSync"
CONFIG_PATH = os.path.join(CONFIG_DIR, "live_sync_config.json")

# ---------------------------------------------------------------------------
# 2026.07.19-01: 保存直後のFinal書き出しに関する再試行パラメータ
# ---------------------------------------------------------------------------
# Painterは保存直後、内部の保存ロックが残っていることがあり(公式には
# BusyStatusChanged/is_busy() では検知できないことをAdobe公式コミュニティ
# でも確認済み)、その状態でexport_project_textures()を呼ぶと
# substance_painter.exception.ProjectError が送出される。従来は固定500ms
# 待機で回避していたが、環境によって足りない/長すぎることがあるため、
# 実際にProjectErrorが発生した場合にのみ、間隔を倍々に広げながら
# 再試行する方式に変更した。
_FINAL_EXPORT_INITIAL_DELAY_MS = 200     # 保存イベントから最初の試行までの遅延
_FINAL_EXPORT_QUEUED_DELAY_MS = 200      # 保存連打でキューされた再実行までの遅延
_FINAL_EXPORT_RETRY_BASE_MS = 200        # 再試行間隔の基準値(倍々に広がる)
_FINAL_EXPORT_RETRY_MAX_MS = 6400        # 再試行間隔の上限
_FINAL_EXPORT_MAX_RETRIES = 6            # 最大再試行回数(初回200ms+再試行分の累計で最大約19秒粘る)

DEFAULT_CONFIG = {
    "staging_dir": "C:/SPMayaLiveSync/staging",
    "watch_dir": "C:/SPMayaLiveSync/live",
    "final_export_dir": "C:/SPMayaLiveSync/final",
    "preview_resolution_log2": 9,
    "final_resolution_log2": 12,
    "debounce_seconds": 1.5,
    "file_format": "png",
    "bit_depth": "8",
    "texture_sets": [],
    "renderer": "arnold",
    "raw_colorspace_suffixes": ["Roughness", "Metallic", "Normal", "Height", "AO"],
    # Phase 2 最適化: プロジェクトごとに既知のテクスチャセット一覧を
    # 分けて保持する。キーはプロジェクトファイルパス(未保存の場合は
    # "__unsaved__")。
    "known_texture_sets_by_project": {},
    # 2026.07.15-01: 以前はテクスチャセット名だけをキーにしたフラットな
    # 辞書 "texture_set_shading_engine_map" だったが、これはMaya側専用の
    # データ(SP側は書き込まない)であるため、SP側のDEFAULT_CONFIGからは
    # 削除した(旧キーがMaya側の設定ファイルに残っていても、Maya側の
    # load_config()が自動移行する)。
    # Phase 3: 複数SPプロジェクトを横断して作業した場合に、Maya側が
    # 「どのプロジェクトのテクスチャセット一覧を見ればよいか」を機械的に
    # 判別できるよう、現在アクティブなプロジェクトのキーを共有する。
    "active_project_key": None,
    # 複数プロジェクト対応: Final は <final_export_dir>/<subfolder>/ に
    # 書き出す。現在アクティブなプロジェクトの Final サブフォルダ名を
    # 共有し、Maya側(aiSSボタン)が正しい取り込み先を自動入力できるようにする。
    "active_final_subfolder": None,
    # 複数プロジェクト対応(所有権問題回避、2026.07.14-02): Live/Preview は
    # <watch_dir>/<subfolder>/ に書き出す。現在アクティブなプロジェクトの
    # 監視先サブフォルダ名を共有し、Maya側(監視処理)が追従できるようにする。
    "active_watch_subfolder": None,
    # Phase 3: SP側が実際に書き出したファイル名のprefixを記録する。
    # Maya側はテクスチャセット名を自前で安全化(_safe_name)して予測する
    # 代わりに、この値があれば最優先で使うことで、スペースや日本語を
    # 含む名前でのズレを防ぐ。
    # 2026.07.15-01: known_texture_sets_by_project と同様、プロジェクト
    # キーでネストする形式に変更した(別プロジェクトの同名テクスチャ
    # セットでprefixを取り違える不具合の対策):
    #   { project_key: { texture_set_name: prefix文字列 } }
    "texture_set_export_prefix_by_project": {},
    # Phase 3: 初回セットアップウィザードを完了したかどうか。
    "setup_wizard_completed": False,
    # 2026.07.19-03(フェーズ2): 保存連打への対策として、前回のFinal
    # 書き出し完了からこの秒数が経過するまでは次のFinalを起こさない
    # (要素技術C: クールダウン)。「今すぐ全量同期」ボタンからの手動
    # 実行はこのクールダウンを無視して即座に実行される。
    "final_cooldown_seconds": 5.0,
}

# Phase 3: 書き出しファイル名の判定・Maya側との橋渡しで共通して使う
# チャンネルsuffix一覧(maya_live_sync.py の CHANNEL_SUFFIXES と対応)。
CHANNEL_SUFFIXES = ["BaseColor", "Roughness", "Metallic", "Normal", "Height", "Emissive"]

RESOLUTION_CHOICES = [
    ("256 px", 8),
    ("512 px", 9),
    ("1024 px", 10),
    ("2048 px", 11),
    ("4096 px", 12),
    ("8192 px", 13),
]


def _log(level, message):
    # substance_painter.logging には Info/Warning/Error のような大文字定数は無く、
    # info()/warning()/error() という小文字の関数が用意されている
    # (level には "info"/"warning"/"error" の文字列を渡す)。
    try:
        log_fn = getattr(sp_log, level, None)
        if log_fn is not None:
            log_fn("[LiveSync] {0}".format(message))
        else:
            sp_log.info("[LiveSync] {0}".format(message))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 5: 同期履歴ログ(Maya側と共通のファイルに追記する)
# ---------------------------------------------------------------------------

HISTORY_LOG_PATH = os.path.join(CONFIG_DIR, "livesync_history.log")
_HISTORY_MAX_BYTES = 512 * 1024  # 500KB。超えたら直近1000行のみ残す。


def _append_history(source, text):
    """SP/Maya共通の同期履歴ログに1行追記する(maya_live_sync.pyの
    _append_history()と同一フォーマット)。失敗しても同期処理自体は
    継続できるよう、例外は握りつぶす。
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = "[{0}] [{1}] {2}\n".format(stamp, source, text)
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
# 2026.07.19-01: ステージングフォルダの自己修復(クラッシュ対策フェーズ1)
# ---------------------------------------------------------------------------

def _cleanup_orphaned_staging_dirs(stage_root, max_age_hours=24):
    """前回のセッションがクラッシュ等で異常終了し、_do_export() の
    finally節(shutil.rmtree)が実行されなかった場合に staging_dir 配下へ
    残り続ける "sp_live_*" 一時フォルダを、起動時に自動回収する。

    安全のため、対象は "sp_live_" プレフィックスを持つフォルダに限定し、
    かつ最終更新から一定時間(既定24時間)以上経過したものだけを削除する。
    これにより、実行中の別セッションが今まさに使っている一時フォルダを
    誤って削除する事故を避ける。start_plugin() から呼ばれる想定で、
    失敗しても起動処理自体は継続できるよう、例外は呼び出し側で
    握りつぶす前提とする。
    """
    if not stage_root or not os.path.isdir(stage_root):
        return
    now = time.time()
    removed = []
    for name in os.listdir(stage_root):
        if not name.startswith("sp_live_"):
            continue
        path = os.path.join(stage_root, name)
        try:
            if not os.path.isdir(path):
                continue
            age_hours = (now - os.path.getmtime(path)) / 3600.0
            if age_hours >= max_age_hours:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(name)
        except OSError:
            continue
    if removed:
        _log("info", "起動時クリーンアップ: 残留ステージングフォルダを{0}件削除しました。".format(
            len(removed)))


# ---------------------------------------------------------------------------
# 設定の読み書き
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(loaded)
        return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    """設定を保存する。呼び出し直前にディスク上の最新内容を読み込んで
    からマージして書き込むことで、Maya側が別途保存した値
    (texture_set_shading_engine_map 等)を、こちらの古いメモリ上の値で
    意図せず上書き・消去してしまわないようにする。
    呼び出し側は、変更したキーだけを渡すこと(全体を渡すと、渡した側が
    保持している値が古い場合に他アプリの変更を巻き戻す恐れがある)。
    """
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


def _current_project_key():
    """現在のプロジェクトを一意に識別するキーを返す。
    未保存プロジェクトの場合は固定文字列を使う(この場合、複数の未保存
    プロジェクトを区別できない点は既知の制限とする)。

    2026.07.16(緊急修正): project.file_path() は公式ドキュメント上
    「プロジェクトが未保存ならNoneを返す」とされているが、実機では
    テンプレートから新規プロジェクトを作成した直後、一度も保存して
    いない状態で、Noneではなく作成元テンプレート(.spt)のパスを
    返すケースが確認された。この値をそのままプロジェクトキーとして
    使うと、Maya側の紐付けUIに「TEST_1」ではなく
    「PBR - Metallic Roughness Alpha-blend.spt」のようなテンプレート名が
    表示されてしまい、実際のプロジェクトと異なるキーで紐付けが行われる
    (ドキュメントに明記されていない、実装依存の挙動と考えられる)。

    対策として、公式に「未保存なら確実にNoneを返す」と明記されている
    project.name() を先に確認し、これがNoneであれば file_path() の値を
    信頼せず "__unsaved__" にフォールバックするようにした。
    """
    try:
        name = project.name()
    except Exception:
        name = None

    if not name:
        # project.name() がNoneを返す = 公式仕様上、確実に未保存。
        # file_path() が(ドキュメントの契約に反して)何らかのパスを
        # 返していても、それは信頼できないため無視する。
        return "__unsaved__"

    try:
        path = project.file_path()
    except Exception:
        path = None
    return path or "__unsaved__"


def _project_subfolder_name():
    """現在のプロジェクト用の Final サブフォルダ名を返す。

    形式: <ファイル名(拡張子なし)>_<フルパスのハッシュ先頭6文字>
      例: C:/work/ProjectA/proA.spp -> "proA_a1b2c3"

    - ファイル名部分で人が見て判別でき、ハッシュ部分で「別の場所にある
      同名プロジェクト」同士の衝突を防ぐ(両立方式)。
    - 未保存プロジェクトはキーが "__unsaved__" 固定のため、フォルダ名も
      "__unsaved__" になる(未保存同士は区別できないという既存の制限を
      そのまま引き継ぐ)。この場合ファイル名部分は付けない。
    - ファイル名部分は、フォルダ名として使えない文字を除去して安全化する。
    """
    key = _current_project_key()
    if key == "__unsaved__":
        return "__unsaved__"

    stem = os.path.splitext(os.path.basename(key))[0]
    # フォルダ名に使えない文字を除去(Windows/一般で不正な文字を _ に)
    safe_stem = re.sub(r'[<>:"/\\|?*\s]', "_", stem).strip("_") or "project"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:6]
    return "{0}_{1}".format(safe_stem, digest)


def _duplicate_folder_warning(staging_dir, watch_dir, final_export_dir):
    """Phase 3 最適化: staging/watch/final の3フォルダが誤って同じ
    パスに設定されていないかを確認する。特にstaging_dirは書き出しごとに
    一時フォルダを作成・削除するため、watch_dirやfinal_export_dirと
    同じ場所に設定すると、Maya側の監視イベントが不必要に多発したり、
    最終出力ファイルが誤って削除されたりする恐れがある。
    問題があれば警告文を返し、無ければNoneを返す。
    """
    pairs = [
        ("ステージングフォルダ", os.path.normpath(staging_dir)),
        ("監視フォルダ", os.path.normpath(watch_dir)),
        ("最終出力フォルダ", os.path.normpath(final_export_dir)),
    ]
    seen = {}
    for label, path in pairs:
        if path in seen:
            return (
                "「{0}」と「{1}」に同じフォルダが指定されています。"
                "意図しない挙動(監視イベントの多発、ファイルの誤削除等)を"
                "避けるため、それぞれ別のフォルダを指定することを推奨します。"
                "続行しますか?".format(seen[path], label)
            )
        seen[path] = label
    return None


# ---------------------------------------------------------------------------
# 同期ロジック本体
# ---------------------------------------------------------------------------

class LiveSyncEngine(QtCore.QObject):

    status_changed = QtCore.Signal(str)      # ログ1行を通知
    stats_changed = QtCore.Signal(dict)       # 統計情報の更新を通知
    # Phase 2: (現在の全テクスチャセット名一覧, 新規追加分, 消失確定分) を通知
    structure_changed = QtCore.Signal(list, list, list)
    # 2026.07.19-02: 書き出し(SP終了処理と衝突しうる後処理を含む)が
    # 進行中かどうかをUI側へ通知する。SPネイティブの「書き出し中」
    # ダイアログはexport_project_textures()の実行中しか保護しないため、
    # ダイアログが消えた後の後処理区間も含めてUI側で「まだ閉じないで」と
    # 警告できるようにする(True=開始、False=完全に終了)。
    exporting_changed = QtCore.Signal(bool)
    # 2026.07.19-03(フェーズ2): 「保存内容がまだFinalに反映されていない」
    # 状態(=要求済みだが未完了、クールダウン待ち・リトライ待ちを含む)を
    # UI側へ通知する。
    final_pending_changed = QtCore.Signal(bool)

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.enabled = False
        self.exporting = False
        self.pending_reexport = False
        # Phase 6: exporting中に高画質(preview=False)の要求が来た場合、
        # 再試行時にプレビューへ格下げされてしまわないよう区別して覚えておく。
        self.pending_final = False
        self.last_hashes = {}
        # 2026.07.19-02: _unchanged() のサイズ事前チェック用(詳細は
        # _unchanged() のdocstring参照)。
        self.last_sizes = {}
        self.dirty_stack_ids = set()
        # 2026.07.19-03(フェーズ2): Final書き出し専用の差分集合。
        # dirty_stack_ids(Preview用)とは別に、Final書き出しが成功する
        # までの間、変更のあったstack_idを蓄積し続ける(Preview側の
        # 同期が先にdirty_stack_idsを消費しても影響を受けない)。
        self.dirty_stack_ids_final = set()
        # このセッション(このプロジェクトを開いてから)で、Finalの差分
        # 書き出しの基準となる「全量書き出し」を一度でも成功させたか。
        # Falseの間はforce_full指定が無くても常に全量書き出しへ
        # フォールバックする(安全側)。プロジェクトを開き直すたびに
        # on_project_edition_entered()でFalseへ戻す。
        self.final_baseline_established = False
        self._last_final_completed_at = None
        # 「今すぐ全量同期」ボタン、またはリトライ・busy再試行の連鎖の
        # 途中で立てられる、次に発火するFinal書き出しを強制的に全量に
        # するフラグ。成功または再試行諦めまでクリアされない
        # (_request_final_export()を経由する他の呼び出しに上書きされて
        # 消えないようにするため)。
        self._final_export_force_full = False
        # Phase 2 最適化: 消失検知の猶予カウンタ(誤検知抑制用)
        self._missing_streak = {}

        self.stats = {
            "sync_count": 0,
            "skip_count": 0,
            "error_count": 0,
            "last_sync_at": None,
            "last_duration_ms": None,
        }

        self.debounce_timer = QtCore.QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.timeout.connect(self.request_export)

        # 2026.07.19-01: 保存直後(および保存連打時にキューされた)Final
        # 書き出しの遅延実行を、この名前付きQTimer1つに統一する。
        # 匿名の QtCore.QTimer.singleShot() だと、後から「まだ発火して
        # いないものをキャンセルする」ことができず、shutdown() 側で
        # 終了処理と重なって発火するのを防げなかったため、明示的に
        # stop() できるインスタンスとして保持する。
        self._final_export_timer = QtCore.QTimer(self)
        self._final_export_timer.setSingleShot(True)
        self._final_export_timer.timeout.connect(self._on_final_export_timer_fired)
        self._final_export_retry_attempt = 0

        # 2026.07.19-01: shutdown() 後に上記タイマーが万一発火しても
        # 実処理へ進ませないための最終ガード(タイマーのstop()と合わせた
        # 二重の安全策)。
        self._shutting_down = False

        event.DISPATCHER.connect_strong(event.TextureStateEvent, self.on_texture_state)
        event.DISPATCHER.connect_strong(event.ProjectSaved, self.on_project_saved)
        event.DISPATCHER.connect_strong(event.ProjectAboutToClose, self.on_project_closing)
        event.DISPATCHER.connect_strong(event.ProjectEditionEntered, self.on_project_edition_entered)

    # -- 後始末 ---------------------------------------------------------

    def shutdown(self):
        """イベント購読を解除し、タイマーを停止する。
        connect_strong() は強参照でコールバックを保持し自動解放されない
        ため、close_plugin() から明示的に本メソッドを呼ばないと、
        プラグインの無効化→再有効化の際に古いエンジンのハンドラが残り、
        イベントが多重発火する(同期処理が重複実行される)。公式サンプルに
        倣い、接続時と同じ (event_cls, callback) の組で disconnect する。

        2026.07.19-01: 従来はここで self.debounce_timer のみ停止して
        いたが、on_project_saved() 由来の遅延Final書き出しが匿名の
        QTimer.singleShot() で仕込まれていたため、shutdown() 実行後も
        キャンセルされずに残り、SP終了処理と重なって発火することで
        ウィンドウを閉じた際のクラッシュレポート増加に関与していたと
        考えられる。self._shutting_down フラグを最初に立てて以降の
        新規スケジューリングを止めた上で、既にスケジュール済みの
        self._final_export_timer も明示的に停止する。
        """
        self._shutting_down = True
        pairs = [
            (event.TextureStateEvent, self.on_texture_state),
            (event.ProjectSaved, self.on_project_saved),
            (event.ProjectAboutToClose, self.on_project_closing),
            (event.ProjectEditionEntered, self.on_project_edition_entered),
        ]
        for evt_cls, cb in pairs:
            try:
                event.DISPATCHER.disconnect(evt_cls, cb)
            except Exception as e:
                _log("warning", "イベント購読解除に失敗しました({0}): {1}".format(
                    getattr(evt_cls, "__name__", evt_cls), e))
        try:
            self.debounce_timer.stop()
        except Exception:
            pass
        try:
            self._final_export_timer.stop()
        except Exception:
            pass

    # -- 設定 -----------------------------------------------------------

    def reload_config(self):
        self.config = load_config()
        self._emit_status("設定を再読込しました。")

    def apply_and_save_config(self, new_values):
        """ユーザー操作(フォルダ変更等)による保存。変更されたキーだけを
        ディスクに反映する(save_config()のマージ仕様と合わせ、Maya側が
        書き込んだ値を巻き戻さないようにするため)。
        """
        self.config.update(new_values)
        save_config(new_values)
        self._emit_status("設定を保存しました。")

    def _emit_status(self, text):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = "[{0}] {1}".format(stamp, text)
        _log("info", text)
        self.status_changed.emit(line)
        _append_history("SP", text)

    # -- 有効/無効 --------------------------------------------------------

    def set_enabled(self, value):
        self.enabled = bool(value)
        self._emit_status("Live Sync {0}".format("有効化" if self.enabled else "無効化"))
        if self.enabled and project.is_open():
            self.debounce_timer.start(200)
            self._missing_streak = {}
            self.check_texture_set_structure()

    # -- イベントハンドラ --------------------------------------------------

    def on_texture_state(self, evt):
        if not self.enabled:
            return
        if evt.action not in (
            event.TextureStateEventAction.ADD,
            event.TextureStateEventAction.UPDATE,
        ):
            return
        self.dirty_stack_ids.add(evt.stack_id)
        # 2026.07.19-03(フェーズ2): Preview用集合とは別に、Final専用の
        # 差分集合にも同じstack_idを積む(Previewが先に消費しても
        # Final側は影響を受けない)。
        self.dirty_stack_ids_final.add(evt.stack_id)
        interval_ms = int(float(self.config.get("debounce_seconds", 1.5)) * 1000)
        self.debounce_timer.start(interval_ms)

    def on_project_saved(self, evt):
        # 2026.07.16(緊急修正): on_project_edition_entered() は
        # プロジェクトを開いた/作成した"その瞬間"の _current_project_key()
        # しか記録しない。テンプレートから新規プロジェクトを作成した
        # 直後はまだ未保存("__unsaved__")のため、その後実際に名前を
        # 付けて保存しても、active_project_key はここを更新する処理が
        # 無ければ "__unsaved__" のまま(または不正確な値のまま)取り
        # 残される。
        # 一方 _do_export() 内の active_watch_subfolder は、エクスポート
        # のたびに _project_subfolder_name() 経由で _current_project_key()
        # を都度再計算していたため、保存後は正しいプロジェクト名を
        # 反映していた。この非対称性により、「監視フォルダ名は正しい
        # プロジェクト名になっているのに、Maya側の状態バー(紐付け表示)
        # だけがテンプレート名や__unsaved__のまま」という矛盾が
        # 実機で確認された。
        # 対策として、保存イベントのタイミングでも active_project_key を
        # 明示的に再計算・保存するようにした。
        key = _current_project_key()
        if self.config.get("active_project_key") != key:
            self.config["active_project_key"] = key
            save_config({"active_project_key": key})

        if not self.enabled:
            return
        # 2026.07.19-01: 従来は固定500ms待機(QTimer.singleShot)で
        # Painter内部の保存ロック解除を待っていたが、(a) shutdown()側で
        # キャンセルできず終了処理と重なるリスクがあり、(b) 500msという
        # 数値自体、環境によって足りない/長すぎることがあった
        # (BusyStatusChanged/is_busy()では保存直後のロック状態を検知
        # できないことをAdobe公式コミュニティでも確認済み)。
        # 名前付きQTimerと指数バックオフ再試行(_request_final_export)に
        # 置き換える。
        self._request_final_export(_FINAL_EXPORT_INITIAL_DELAY_MS)

    def on_project_closing(self, evt):
        self.debounce_timer.stop()
        # Phase 2 最適化: プロジェクトが切り替わるので、猶予カウンタを
        # リセットしておく(次のプロジェクトの検出に前プロジェクトの
        # 状態を持ち越さないため)。
        self._missing_streak = {}
        # 2026.07.19-03(フェーズ2): 差分書き出し用の状態もプロジェクト
        # 単位でリセットする。これを怠ると、別プロジェクトに切り替えた
        # 直後の最初のFinal書き出しが、前のプロジェクトのbaseline済み
        # フラグのせいで誤って差分扱いになり、大部分のテクスチャセットが
        # 書き出されないまま「完了」してしまう恐れがある。
        self.dirty_stack_ids_final = set()
        self.final_baseline_established = False
        self._final_export_force_full = False
        # 2026.07.19-04(フェーズ3): _unchanged()用のキャッシュ
        # (last_hashes/last_sizes)はdest_pathがプロジェクト別サブ
        # フォルダを含む絶対パスのため、他プロジェクトのエントリは
        # 二度と参照されないまま増え続けるだけになる。長時間・複数
        # プロジェクトを横断するセッションでの緩やかなメモリ増加を
        # 防ぐため、プロジェクトを閉じるタイミングで刈り込む。次に
        # 同じプロジェクトへ戻ってきた際は、最初の1回だけ保守的に
        # 「変更あり」判定になる(実害はなく、安全側の挙動)。
        self.last_hashes = {}
        self.last_sizes = {}
        try:
            self._final_export_timer.stop()
        except Exception:
            pass
        self.final_pending_changed.emit(False)
        # Phase 3: プロジェクトが閉じられたので、Maya側に「今SPで開いている
        # プロジェクトは無い」ことを伝える。
        self.config["active_project_key"] = None
        save_config({"active_project_key": None})

    def on_project_edition_entered(self, evt):
        # 新しいプロジェクト(または既存プロジェクト)を開いた直後に
        # 基準となるテクスチャセット構成を記録する。
        # Phase 3: どのプロジェクトを開いたかをMaya側と共有する
        # (複数プロジェクトを横断して作業する際の一覧混在を防ぐため)。
        key = _current_project_key()
        self.config["active_project_key"] = key
        save_config({"active_project_key": key})
        # 2026.07.19-03(フェーズ2): 新しいプロジェクトの編集に入るタイミング
        # でも念のためリセットする(on_project_closing()を経由しない
        # 遷移が万一あった場合の保険)。
        self.dirty_stack_ids_final = set()
        self.final_baseline_established = False
        # 2026.07.19-04(フェーズ3): 同上の理由でキャッシュも念のため
        # ここでも刈り込む。
        self.last_hashes = {}
        self.last_sizes = {}
        if self.enabled:
            self._missing_streak = {}
            self.check_texture_set_structure()

    # -- Phase 2: テクスチャセット構造の変化検出 -----------------------------

    def check_texture_set_structure(self):
        """現在のテクスチャセット一覧を、同一プロジェクトについて前回記録
        した一覧と比較する。追加は即時反映するが、削除(リネームは削除+
        追加として現れる)は2回連続で検出された場合のみ確定させ、
        ベイク処理中などの一時的なちらつきによる誤検知を防ぐ。
        """
        if not project.is_open():
            return
        # busy中(ベイク等)は一覧が一時的に不完全なことがあるためスキップする。
        if project.is_busy():
            return

        try:
            current = set(ts.name() for ts in textureset.all_texture_sets())
        except Exception as e:
            self._emit_status("テクスチャセット一覧の取得に失敗しました: {0}".format(e))
            return

        key = _current_project_key()
        by_project = self.config.get("known_texture_sets_by_project", {})
        known = set(by_project.get(key, []))

        added = current - known
        missing_now = known - current

        # 消失候補の猶予カウントを更新
        confirmed_removed = []
        for name in list(self._missing_streak.keys()):
            if name not in missing_now:
                # 復活した(=誤検知だった)ので猶予カウンタを消す
                del self._missing_streak[name]
        for name in missing_now:
            self._missing_streak[name] = self._missing_streak.get(name, 0) + 1
            if self._missing_streak[name] >= 2:
                confirmed_removed.append(name)

        changed = bool(added) or bool(confirmed_removed) or (key not in by_project)

        if changed:
            if added:
                self._emit_status("新しいテクスチャセットを検出: {0}".format(", ".join(sorted(added))))
            if confirmed_removed:
                self._emit_status(
                    "テクスチャセットが削除/リネームされた可能性があります: {0}".format(
                        ", ".join(sorted(confirmed_removed))
                    )
                )
            new_known = (known | added) - set(confirmed_removed)
            by_project[key] = sorted(new_known)
            self.config["known_texture_sets_by_project"] = by_project
            save_partial = {"known_texture_sets_by_project": by_project}

            # Phase 3 最適化: 削除が確定したテクスチャセットについては、
            # texture_set_export_prefix_by_project に残ったままだと不要な
            # エントリが溜まり続けるため、あわせて掃除する(実害は無いが、
            # 設定ファイルが際限なく肥大化するのを防ぐ)。
            # 2026.07.15-01: プロジェクトキーでネストした構造に変更した
            # ため、対象は by_project[key] のみに限定する(他プロジェクトの
            # 同名エントリを誤って消さないため)。
            if confirmed_removed:
                prefix_by_project = dict(self.config.get("texture_set_export_prefix_by_project", {}))
                prefix_map = dict(prefix_by_project.get(key, {}))
                removed_any_prefix = False
                for name in confirmed_removed:
                    if name in prefix_map:
                        del prefix_map[name]
                        removed_any_prefix = True
                if removed_any_prefix:
                    prefix_by_project[key] = prefix_map
                    self.config["texture_set_export_prefix_by_project"] = prefix_by_project
                    save_partial["texture_set_export_prefix_by_project"] = prefix_by_project

            save_config(save_partial)
            for name in confirmed_removed:
                self._missing_streak.pop(name, None)

        self.structure_changed.emit(
            sorted(current), sorted(added), sorted(confirmed_removed)
        )

    # -- 書き出しトリガー ---------------------------------------------------

    def request_export(self):
        if not self.enabled or not project.is_open():
            return
        self._safe_export(preview=True)

    def force_sync_now(self):
        if not project.is_open():
            self._emit_status("プロジェクトが開かれていません。")
            return
        self._safe_export(preview=True)

    def force_full_final_sync(self):
        """2026.07.19-03(フェーズ2): 「今すぐ全量同期」ボタンから呼ばれる。
        差分書き出しの状態がMaya側の実際のファイルとズレてしまった場合の
        手動リセット手段として、クールダウンを無視して即座に全量の
        Final書き出しを要求する。
        """
        if not project.is_open():
            self._emit_status("プロジェクトが開かれていません。")
            return
        self._emit_status("手動操作: Final全量同期を要求しました。")
        self._request_final_export(0, bypass_cooldown=True, force_full=True)

    # -- 2026.07.19-01/03: 保存直後のFinal書き出し(再試行・クールダウン付き遅延実行) --

    def _request_final_export(self, delay_ms, bypass_cooldown=False, force_full=False):
        """delay_ms後にFinal書き出しを試みるようスケジュールする。
        on_project_saved() と、_safe_export() の finally節(保存連打で
        キューされた再実行)、force_full_final_sync() の3箇所から呼ばれる
        単一の入口。
        ProjectError(プロジェクトロック中)で失敗した場合は
        _handle_final_export_locked() が指数バックオフで再スケジュール
        する。

        2026.07.19-03(フェーズ2):
        - bypass_cooldown=False(既定)の場合、前回のFinal書き出し完了
          からfinal_cooldown_seconds未満しか経っていなければ、実行を
          その残り時間まで遅らせる(保存連打の間引き)。手動の全量同期
          (force_full_final_sync)はTrueを渡してこれを無視する。
        - force_full=Trueが渡された場合のみ self._final_export_force_full
          をTrueにする(Falseを渡しても既存のTrueは消さない。busy中の
          衝突で後から再スケジュールされた際に、force_full要求が
          消えてしまわないようにするため)。
        """
        if self._shutting_down:
            return
        self._final_export_retry_attempt = 0
        if force_full:
            self._final_export_force_full = True

        effective_delay = float(delay_ms)
        if not bypass_cooldown and self._last_final_completed_at is not None:
            cooldown_ms = float(self.config.get("final_cooldown_seconds", 5.0)) * 1000.0
            elapsed_ms = (time.time() - self._last_final_completed_at) * 1000.0
            remaining_ms = cooldown_ms - elapsed_ms
            if remaining_ms > effective_delay:
                effective_delay = remaining_ms

        self._final_export_timer.start(max(0, int(effective_delay)))
        self.final_pending_changed.emit(True)

    def _on_final_export_timer_fired(self):
        if self._shutting_down:
            return
        self._safe_export(
            preview=False,
            _on_project_locked=self._handle_final_export_locked,
            force_full=self._final_export_force_full,
        )

    def _handle_final_export_locked(self, error):
        if self._shutting_down:
            return
        self._final_export_retry_attempt += 1
        if self._final_export_retry_attempt <= _FINAL_EXPORT_MAX_RETRIES:
            delay = min(
                _FINAL_EXPORT_RETRY_BASE_MS * (2 ** self._final_export_retry_attempt),
                _FINAL_EXPORT_RETRY_MAX_MS,
            )
            self._emit_status(
                "プロジェクトがロック中のため{0}ms後に再試行します({1}/{2}): {3}".format(
                    delay, self._final_export_retry_attempt, _FINAL_EXPORT_MAX_RETRIES, error))
            # 2026.07.19-03: クールダウンは初回要求時に既に考慮済みなので、
            # ロック解除待ちの再試行そのものはクールダウンを再適用しない
            # (でないと再試行のたびに待ち時間が余計に伸びてしまう)。
            self._final_export_timer.start(delay)
        else:
            self._final_export_force_full = False
            self.final_pending_changed.emit(False)
            self._emit_status(
                "警告: 保存直後のFinal書き出しがロック解除待ちで{0}回再試行しましたが"
                "諦めました。「今すぐ全量同期」ボタンから手動で再試行できます。".format(
                    _FINAL_EXPORT_MAX_RETRIES))

    def _safe_export(self, preview=True, _on_project_locked=None, force_full=False):
        """
        _on_project_locked: 省略時(None)は従来通りの挙動。
        substance_painter.exception.ProjectError が発生した場合も他の
        例外と同様にログへ出して握りつぶし、何もしない。
        コールバックを渡した場合のみ、ProjectError発生時にそのコール
        バックを呼び出す(呼び出し元が再試行するかどうかを判断できる
        フック)。他の種類の例外は、コールバックの有無に関わらず従来通り
        握りつぶす。
        2026.07.19-01: request_export()/force_sync_now() など既存の
        呼び出し元は _on_project_locked を渡さないため、挙動は完全に
        従来通りである。新しいリトライ経路(_on_final_export_timer_fired)
        だけがこの引数を利用する。

        force_full: 2026.07.19-03(フェーズ2)。Trueの場合、Final書き出し
        (preview=False)で差分の有無に関わらず全テクスチャセットを
        書き出す。「今すぐ全量同期」ボタン、および差分書き出しの基準が
        まだ確立していない(このセッションで一度もFinalに成功していない)
        場合の自動フォールバックで使われる。
        """
        if not project.is_open():
            return
        if project.is_busy():
            self._emit_status("Painterがbusyのため、解除後に再試行します。")
            project.execute_when_not_busy(
                lambda: self._safe_export(preview, _on_project_locked, force_full))
            return
        if self.exporting:
            self.pending_reexport = True
            if not preview:
                self.pending_final = True
            self.debounce_timer.start(300)
            return

        dirty_ids = self.dirty_stack_ids
        self.dirty_stack_ids = set()

        self.exporting = True
        self.exporting_changed.emit(True)
        t0 = time.time()
        locked_error = None
        try:
            self._do_export(preview=preview, dirty_stack_ids=dirty_ids, force_full=force_full)
            self.stats["sync_count"] += 1
        except sp_exception.ProjectError as e:
            self.stats["error_count"] += 1
            locked_error = e
            if _on_project_locked is None:
                self._emit_status("エラー: プロジェクトがロック中のため書き出せませんでした ({0})".format(e))
        except Exception as e:
            self.stats["error_count"] += 1
            self._emit_status("エラー: {0}".format(e))
        finally:
            # 2026.07.19-02: self.exporting=False をここ(finally先頭)では
            # なく後段に移した。以前はここで先にFalseへ戻していたため、
            # 「exportingフラグを見て書き出し中を警告する」という仕組みを
            # 仮に用意しても、この後まだ実行される後処理(check_texture_
            # set_structure()等、Painter APIを追加で叩く処理)の間は
            # 警告が消えてしまい、実際に危険な区間をカバーできていな
            # かった。exporting_changed シグナルをここで一度emitし、
            # 「後処理はまだ続いている」ことをUI側が正しく把握できる
            # ようにする(_on_export_finished_uiのような明示的な完了通知
            # は、後処理が全て終わった後、finallyの最後で行う)。
            self.stats["last_duration_ms"] = int((time.time() - t0) * 1000)
            self.stats["last_sync_at"] = datetime.datetime.now().strftime("%H:%M:%S")
            self.stats_changed.emit(dict(self.stats))
            if self.pending_final:
                # 高画質書き出しの要求を優先して処理する(プレビューに
                # すり替わって消えてしまわないようにするため)。
                self.pending_reexport = False
                self.pending_final = False
                # 2026.07.19-01: 匿名singleShotから、shutdown()で停止
                # できる名前付きタイマー経由の呼び出しへ変更。
                self._request_final_export(_FINAL_EXPORT_QUEUED_DELAY_MS)
            elif self.pending_reexport:
                self.pending_reexport = False
                self.debounce_timer.start(200)
            # 2026.07.19-02(クラッシュ再現テストを受けた追加対応):
            # このcheck_texture_set_structure()自体もPainter APIを叩く
            # 処理であり、Final書き出し直後の「まだ危険な後処理区間」の
            # 末尾に位置していた。Preview側の同期はテクスチャ編集の
            # たびに高頻度で走り、その都度この構造検知も行われているため、
            # Final書き出し直後に限ってはここを省略しても構成一覧の
            # 鮮度が実用上大きく落ちることはないと判断し、preview=True
            # の場合のみ呼ぶよう変更した。
            if preview:
                self.check_texture_set_structure()
            # 2026.07.19-02: ここまで全ての後処理が終わった時点で初めて
            # exporting=Falseにする(上記コメント参照)。
            self.exporting = False
            self.exporting_changed.emit(False)

        # 2026.07.19-01: locked_errorはfinally節の後(状態管理が全て完了
        # した後)に呼び出し元へ通知する。呼び出し元が渡したコールバック
        # の中身(再試行のスケジューリング等)がここでの後始末と競合
        # しないようにするため。
        if locked_error is not None and _on_project_locked is not None:
            _on_project_locked(locked_error)

    def _do_export(self, preview=True, dirty_stack_ids=None, force_full=False):
        cfg = self.config

        # 2026.07.19-03(フェーズ2): Final専用の差分集合をこの時点で
        # スナップショットして即座にクリアする(Previewのdirty_stack_ids
        # と同じパターン)。書き出し中に新たな編集が入っても、そのぶんは
        # self.dirty_stack_ids_final に積まれ続け、次回のFinalへ正しく
        # 持ち越される。この書き出しが失敗した場合は、finally節で
        # このスナップショットを差し戻す(取りこぼしを防ぐため)。
        final_dirty_snapshot = None
        final_export_succeeded = False
        if not preview:
            final_dirty_snapshot = set(self.dirty_stack_ids_final)
            self.dirty_stack_ids_final = set()
            if not force_full and self.final_baseline_established and not final_dirty_snapshot:
                # 前回のFinal成功以降、テクスチャに変更が無い。書き出す
                # 対象が無いため、SPネイティブAPI(export_project_
                # textures())を一切呼ばずに即座に終了する。危険な後処理
                # 区間そのものに入らずに済む、フェーズ2で最も効果の
                # 大きい経路。
                self._emit_status("Final書き出し: 前回以降の変更が無いためスキップしました。")
                self.final_pending_changed.emit(False)
                return

        subfolder = _project_subfolder_name()
        if preview:
            # 複数プロジェクト対応(所有権問題の回避、2026.07.14-02):
            # 当初はライブプレビューを watch_dir 直下に一律で書き出して
            # いたが、共有PC環境では、過去に別のWindowsユーザーが
            # watch_dir 直下に残したファイルにNTFSの所有権(ACL)が付いて
            # おり、別ユーザーからは PermissionError で読み込めなくなる
            # 実害が確認された。Finalと同じ「プロジェクト別サブフォルダ」
            # 方式にすることで、常に自分が新規作成したフォルダ配下だけを
            # 使うことになり、この種の所有権衝突が原理上起こらなくなる。
            # 例: <watch_dir>/proA_a1b2c3/
            dest_root = os.path.join(cfg["watch_dir"], subfolder)
            # Maya 側(監視処理)が「現在アクティブなプロジェクトの監視先
            # サブフォルダ」を追従できるよう、共有設定に記録する。
            if cfg.get("active_watch_subfolder") != subfolder:
                cfg["active_watch_subfolder"] = subfolder
                save_config({"active_watch_subfolder": subfolder})
        else:
            # Final は複数プロジェクトが同じフォルダに混在・上書きするのを
            # 防ぐため、プロジェクトごとのサブフォルダへ書き出す。
            # 例: <final_export_dir>/proA_a1b2c3/
            dest_root = os.path.join(cfg["final_export_dir"], subfolder)
            # Maya 側(aiSSボタン)が「現在アクティブなプロジェクトのFinal
            # フォルダ」を自動入力できるよう、サブフォルダ名を共有設定に記録。
            if cfg.get("active_final_subfolder") != subfolder:
                cfg["active_final_subfolder"] = subfolder
                save_config({"active_final_subfolder": subfolder})
        stage_root = cfg["staging_dir"]
        os.makedirs(stage_root, exist_ok=True)
        os.makedirs(dest_root, exist_ok=True)

        tmp_dir = tempfile.mkdtemp(prefix="sp_live_", dir=stage_root)
        # Phase 3: 実際に書き出したファイル名から、テクスチャセット名 ->
        # ファイル名prefix の対応を逆算して記録する(_safe_name() による
        # Maya側の予測とのズレを無くすため)。
        export_prefix_updates = {}
        try:
            export_config = self._build_export_config(
                tmp_dir, preview, dirty_stack_ids, final_dirty_snapshot, force_full)
            result = export.export_project_textures(export_config)

            if result.status == export.ExportStatus.Cancelled:
                self._emit_status("書き出しがキャンセルされました。")
                return
            if result.status != export.ExportStatus.Success:
                self._emit_status("書き出しステータス: {0}".format(result.message))

            moved_any = False
            for key_tuple, files in result.textures.items():
                # result.textures のキーは (テクスチャセット名, スタック名)
                # のタプル(Painter公式APIドキュメントで確認済み)。
                texture_set_name = None
                if isinstance(key_tuple, (tuple, list)) and key_tuple:
                    texture_set_name = key_tuple[0]
                for src in files:
                    if not os.path.isfile(src):
                        continue
                    dest = os.path.join(dest_root, os.path.basename(src))
                    if self._unchanged(src, dest):
                        self.stats["skip_count"] += 1
                        continue
                    try:
                        if os.path.exists(dest):
                            os.remove(dest)
                        os.replace(src, dest)
                        moved_any = True
                    except OSError:
                        shutil.copyfile(src, dest)
                        os.remove(src)
                        moved_any = True

                    if texture_set_name and preview:
                        base = os.path.basename(dest)
                        for suffix in CHANNEL_SUFFIXES:
                            marker = "_{0}.".format(suffix)
                            if marker in base:
                                export_prefix_updates[texture_set_name] = base.split(marker)[0]
                                break

            if export_prefix_updates:
                # 2026.07.15-01: 別プロジェクトの同名テクスチャセットに
                # prefixを取り違えて上書きしないよう、プロジェクトキーで
                # ネストした構造(texture_set_export_prefix_by_project)に
                # 保存する。
                project_key = _current_project_key()
                prefix_by_project = dict(self.config.get("texture_set_export_prefix_by_project", {}))
                current_map = dict(prefix_by_project.get(project_key, {}))
                if any(current_map.get(k) != v for k, v in export_prefix_updates.items()):
                    current_map.update(export_prefix_updates)
                    prefix_by_project[project_key] = current_map
                    self.config["texture_set_export_prefix_by_project"] = prefix_by_project
                    save_config({"texture_set_export_prefix_by_project": prefix_by_project})

            if preview:
                if moved_any:
                    flag_path = os.path.join(dest_root, "_sync_complete.flag")
                    with open(flag_path, "w", encoding="utf-8") as f:
                        f.write(str(time.time()))
                    self._emit_status("プレビュー同期完了({0}件更新)".format(
                        sum(len(v) for v in result.textures.values())
                    ))
            else:
                # Maya側がFinalフォルダの更新を検知できるよう、こちらにも
                # 同期完了フラグを書き込む(以前はpreview時にしか書いて
                # おらず、表示品質をFinalにしたままだと新しい高画質版が
                # 自動反映されない不具合があったため追加)。
                if moved_any:
                    flag_path = os.path.join(dest_root, "_sync_complete.flag")
                    with open(flag_path, "w", encoding="utf-8") as f:
                        f.write(str(time.time()))
                # 2026.07.19-01: クラッシュとの相関を後から検証しやすい
                # よう、Preview側と同様に書き出し件数をログへ残す
                # (所要時間は _safe_export() 側で stats に記録済みで、
                # 履歴ログには _emit_status() 経由で別途出力される)。
                self._emit_status("フル解像度の最終書き出しが完了しました({0}件、対象{1}テクスチャセット、{2})。".format(
                    sum(len(v) for v in result.textures.values()),
                    len(export_config.get("exportList", [])),
                    "全量" if (force_full or not self.final_baseline_established) else "差分",
                ))
                # 2026.07.19-03(フェーズ2): 成功したのでbaselineを確立
                # (または再確立)し、次回以降の差分書き出しを有効にする。
                final_export_succeeded = True
                self.final_baseline_established = True
                self._last_final_completed_at = time.time()
                self._final_export_force_full = False
                self.final_pending_changed.emit(False)
        finally:
            if not preview and not final_export_succeeded and final_dirty_snapshot:
                # 失敗した(例外・キャンセル)ので、今回対象にしていた
                # 差分を失わないよう差し戻す。書き出し中に新たに積まれた
                # 分(finally節実行時点のself.dirty_stack_ids_final)は
                # そのまま残し、そこへ合流させる。
                self.dirty_stack_ids_final |= final_dirty_snapshot
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # 2026.07.19-04(フェーズ3: 一般的な軽量化)
    _HASH_CHUNK_SIZE = 1024 * 1024  # 1MB単位で読みながらハッシュを更新する

    def _file_digest(self, path):
        """ファイル全体を一度にメモリへ読み込まず、チャンク単位で読みながら
        MD5を更新する。計算結果(判定結果)はf.read()で一括読み込みする
        場合と完全に同一だが、ピークメモリ使用量を抑えられる。
        4096px級のテクスチャを複数チャンネル・複数テクスチャセット分
        まとめて処理する後処理区間(危険な区間、2026.07.19-02の項参照)の
        メモリ負荷軽減が狙い。
        """
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(self._HASH_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _unchanged(self, src_path, dest_path):
        """2026.07.19-02(クラッシュ再現テストを受けた追加対応): 保存直後の
        Final書き出しでは、export_project_textures() が返った後(=SP
        ネイティブの「書き出し中」ダイアログが消えた後)、こちらの
        Pythonコードがこの後処理ループの中でまだ動き続けている。
        この区間はSP側の保護対象外で、実機テストでも「ダイアログが
        消えた後、数秒はまだ閉じるとクラッシュする」ことが確認された。

        従来は対象ファイル全てを毎回フル読み込みしてMD5計算しており、
        この後処理区間の長さに直結していた。ここでは、まずファイル
        サイズだけを比較する軽量な事前チェックを行い、サイズが違う
        (=内容が違うことが確定する)場合はハッシュ計算そのものを
        省略する。サイズが同じ場合のみ、従来通りMD5で内容を比較する
        (「サイズは同じだが内容だけ違う」ケースを見逃さないため)。
        判定結果(変更あり/なし)自体はどちらの経路でも同じになるため、
        既存の挙動を変えない安全な最適化である。

        2026.07.19-04(フェーズ3): ハッシュ計算自体も、ファイル全体を
        一括読み込みする方式からチャンク単位の読み込みに変更した
        (_file_digest()参照)。判定結果は変わらない。
        """
        try:
            size = os.path.getsize(src_path)
        except OSError:
            return False

        prev_size = self.last_sizes.get(dest_path)
        self.last_sizes[dest_path] = size
        if prev_size is not None and prev_size != size:
            # サイズが違う時点で「変更あり」が確定するため、ハッシュ計算
            # (ファイル全体の読み込み)を省略する。次回サイズが偶然一致
            # した場合に古いハッシュで誤って「変更なし」と判定しないよう、
            # 古いハッシュ値も破棄しておく。
            self.last_hashes.pop(dest_path, None)
            return False

        try:
            digest = self._file_digest(src_path)
        except Exception:
            return False
        prev_hash = self.last_hashes.get(dest_path)
        self.last_hashes[dest_path] = digest
        return prev_hash is not None and prev_hash == digest

    def _resolve_stack_names(self, stack_ids):
        """stack_idの集合から、対応するテクスチャセット名の集合を解決する。
        2026.07.19-03(フェーズ2): Preview・Final双方の差分フィルタで
        共通して使うため、_build_export_config()内にあった処理を
        切り出した(挙動は変更していない)。
        """
        names = set()
        for stack_id in stack_ids:
            try:
                stack = textureset.Stack(stack_id)
                names.add(stack.material().name())
            except Exception as e:
                self._emit_status(
                    "stack_id={0} からテクスチャセットを特定できませんでした: {1}".format(stack_id, e)
                )
        return names

    def _build_export_config(self, out_dir, preview, dirty_stack_ids=None,
                              final_dirty_stack_ids=None, force_full=False):
        cfg = self.config
        size_log2 = (
            int(cfg.get("preview_resolution_log2", 9))
            if preview
            else int(cfg.get("final_resolution_log2", 12))
        )

        configured_sets = cfg.get("texture_sets") or None

        dirty_set_names = None
        if not force_full:
            if preview:
                if dirty_stack_ids:
                    names = self._resolve_stack_names(dirty_stack_ids)
                    if names:
                        dirty_set_names = names
            else:
                # 2026.07.19-03(フェーズ2): baseline(このセッションで
                # 一度でもFinalに成功した実績)が無い場合は、差分の有無に
                # 関わらず全量書き出しにフォールバックする(安全側)。
                # baseline確立済みで、かつ前回のFinal成功以降に変更が
                # あった場合のみ差分に絞る。
                if self.final_baseline_established and final_dirty_stack_ids:
                    names = self._resolve_stack_names(final_dirty_stack_ids)
                    if names:
                        dirty_set_names = names

        if dirty_set_names is not None:
            if configured_sets:
                target_sets = [s for s in configured_sets if s in dirty_set_names]
                if not target_sets:
                    target_sets = configured_sets
            else:
                target_sets = sorted(dirty_set_names)
        else:
            target_sets = configured_sets or [ts.name() for ts in textureset.all_texture_sets()]

        # 2026.07.17 修正: fileName に $udim トークンが無かったため、UDIM
        # (複数UVタイル)を含むテクスチャセットを書き出すと、$textureSet_
        # BaseColor のようなテンプレートが全タイルで同じ文字列に解決されて
        # しまい、Painter側が名前衝突を避けるために独自の連番
        # (_1, _2, ...)を末尾に付与していた。これはタイル番号(1001,
        # 1002...)ではなく単なる重複回避カウンタのため、udim_setup.py の
        # タイル検出(末尾の4桁UDIM番号を期待)や sp_to_aiStandardSurface.py
        # のプレフィックス抽出が正しく機能しない原因になっていた。
        # 括弧で囲むことで、Painter公式ドキュメントの仕様通り「UDIM
        # タイルが複数ある場合のみ .<UDIM番号> を付与し、単一タイルの
        # 場合は何も付与しない」条件付きトークンとして働く。
        # (Live/Final の両方でこの _build_export_config() を共有している
        # ため、この修正は自動的に両方に適用される)
        maps = [
            {"fileName": "$textureSet_BaseColor(.$udim)", "channels": [
                {"destChannel": c, "srcChannel": c, "srcMapType": "documentMap", "srcMapName": "basecolor"}
                for c in ("R", "G", "B")
            ]},
            {"fileName": "$textureSet_Roughness(.$udim)", "channels": [
                {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "roughness"}
            ]},
            {"fileName": "$textureSet_Metallic(.$udim)", "channels": [
                {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "metallic"}
            ]},
            {"fileName": "$textureSet_Normal(.$udim)", "channels": [
                {"destChannel": c, "srcChannel": c, "srcMapType": "virtualMap", "srcMapName": "Normal_OpenGL"}
                for c in ("R", "G", "B")
            ]},
            {"fileName": "$textureSet_Height(.$udim)", "channels": [
                {"destChannel": "L", "srcChannel": "L", "srcMapType": "documentMap", "srcMapName": "height"}
            ]},
            {"fileName": "$textureSet_Emissive(.$udim)", "channels": [
                {"destChannel": c, "srcChannel": c, "srcMapType": "documentMap", "srcMapName": "emissive"}
                for c in ("R", "G", "B")
            ]},
        ]

        export_parameters = [{
            "parameters": {
                "fileFormat": cfg.get("file_format", "png"),
                "bitDepth": cfg.get("bit_depth", "8"),
                "dithering": True,
                "paddingAlgorithm": "infinite",
                "sizeLog2": size_log2,
            }
        }]
        # 2026.07.17: 上の maps 側 fileName に (.$udim) を追加したため、
        # このフィルタも同じテンプレート文字列(UDIMトークン込み)で
        # 指定しないと対象マップにマッチしなくなる(outputMapsは
        # fileNameテンプレート文字列そのものを見て絞り込むため)。
        for suffix in cfg.get("raw_colorspace_suffixes", []):
            export_parameters.append({
                "filter": {"outputMaps": ["$textureSet_{0}(.$udim)".format(suffix)]},
                "parameters": {
                    "fileFormat": cfg.get("file_format", "png"),
                    "bitDepth": cfg.get("bit_depth", "8"),
                    "dithering": False,
                    "paddingAlgorithm": "infinite",
                    "sizeLog2": size_log2,
                }
            })

        return {
            "exportPath": out_dir,
            "exportShaderParams": False,
            "exportPresets": [{"name": "livesync_preset", "maps": maps}],
            "defaultExportPreset": "livesync_preset",
            "exportList": [{"rootPath": ts} for ts in target_sets],
            "exportParameters": export_parameters,
        }


# ---------------------------------------------------------------------------
# Phase 3: 初回セットアップウィザード
# ---------------------------------------------------------------------------
#
# 他人にこのパイプラインを共有した際、フォルダ設定の意味が分からず
# 迷わないよう、対話形式で最低限の設定を案内するウィザード。
# ここで行える設定はLiveSyncPanelのGUIからいつでも変更でき、ウィザードは
# あくまで初回導入を分かりやすくするための補助という位置づけ。

class SetupWizard(QtWidgets.QWizard):

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("SP Live Sync 初期セットアップ")
        self.setMinimumSize(560, 420)
        # QWizardの既定スタイル(Windowsだと ModernStyle/AeroStyle)は、
        # 上部に白背景のバナー領域を独自描画するため、Painterのダーク
        # テーマと合わずに浮いて見える。ClassicStyleはこのバナー領域を
        # 描画しないため、ホストアプリのテーマに馴染みやすい。
        self.setWizardStyle(QtWidgets.QWizard.ClassicStyle)

        self.addPage(self._welcome_page())
        self.addPage(self._folder_page())
        self.addPage(self._quality_page())
        self.addPage(self._finish_page())

    def _welcome_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("ようこそ")
        layout = QtWidgets.QVBoxLayout(page)
        label = QtWidgets.QLabel(
            "このウィザードでは、Substance Painterで編集したテクスチャを"
            "Mayaへ自動反映するための最低限の設定を行います。\n\n"
            "設定完了後、Maya側でも同じ「監視フォルダ」を指定する必要が"
            "あります。最後の画面に表示されるフォルダパスを、Maya側の"
            "セットアップウィザードにそのまま入力してください。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        return page

    def _folder_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("フォルダ設定")
        page.setSubTitle("既定値のままで問題なければ、そのまま「次へ」を押してください。")
        layout = QtWidgets.QFormLayout(page)

        cfg = self.engine.config
        self.staging_edit = QtWidgets.QLineEdit(cfg.get("staging_dir", ""))
        self.watch_edit = QtWidgets.QLineEdit(cfg.get("watch_dir", ""))
        self.final_edit = QtWidgets.QLineEdit(cfg.get("final_export_dir", ""))

        for label_text, edit in (
            ("ステージングフォルダ", self.staging_edit),
            ("監視フォルダ (★ Maya側と一致させる)", self.watch_edit),
            ("最終出力フォルダ (保存時)", self.final_edit),
        ):
            row = QtWidgets.QHBoxLayout()
            row.addWidget(edit)
            browse_btn = QtWidgets.QPushButton("参照...")
            browse_btn.clicked.connect(lambda checked=False, e=edit: self._browse(e))
            row.addWidget(browse_btn)
            container = QtWidgets.QWidget()
            container.setLayout(row)
            layout.addRow(label_text, container)

        return page

    def _browse(self, line_edit):
        current = line_edit.text() or "C:/"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "フォルダを選択", current)
        if selected:
            line_edit.setText(selected)

    def _quality_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("プレビュー品質")
        layout = QtWidgets.QFormLayout(page)

        self.resolution_combo = QtWidgets.QComboBox()
        for label, _log2 in RESOLUTION_CHOICES:
            self.resolution_combo.addItem(label)
        target_log2 = int(self.engine.config.get("preview_resolution_log2", 9))
        for i, (_label, log2) in enumerate(RESOLUTION_CHOICES):
            if log2 == target_log2:
                self.resolution_combo.setCurrentIndex(i)
                break
        layout.addRow("プレビュー解像度\n(大きいほど同期が重くなります)", self.resolution_combo)

        self.debounce_spin = QtWidgets.QDoubleSpinBox()
        self.debounce_spin.setRange(0.2, 10.0)
        self.debounce_spin.setSingleStep(0.1)
        self.debounce_spin.setSuffix(" 秒")
        self.debounce_spin.setValue(float(self.engine.config.get("debounce_seconds", 1.5)))
        layout.addRow("デバウンス間隔\n(編集停止からMaya反映までの待ち時間)", self.debounce_spin)

        return page

    def _finish_page(self):
        page = QtWidgets.QWizardPage()
        page.setTitle("完了")
        layout = QtWidgets.QVBoxLayout(page)
        self.summary_label = QtWidgets.QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        page.initializePage = self._update_summary
        return page

    def _update_summary(self):
        watch_dir = self.watch_edit.text().strip() or self.engine.config.get("watch_dir", "")
        self.summary_label.setText(
            "「完了」を押すと設定を保存し、Live Syncを有効化します。\n\n"
            "Maya側のセットアップウィザードでは、次の「監視フォルダ」を"
            "そのまま入力してください:\n\n{0}".format(watch_dir)
        )

    def accept(self):
        new_values = {
            "staging_dir": self.staging_edit.text().strip(),
            "watch_dir": self.watch_edit.text().strip(),
            "final_export_dir": self.final_edit.text().strip(),
            "preview_resolution_log2": RESOLUTION_CHOICES[self.resolution_combo.currentIndex()][1],
            "debounce_seconds": self.debounce_spin.value(),
            "setup_wizard_completed": True,
        }
        for key in ("staging_dir", "watch_dir", "final_export_dir"):
            if not new_values[key]:
                QtWidgets.QMessageBox.warning(self, "Live Sync", "フォルダが未指定です: {0}".format(key))
                return
        warning = _duplicate_folder_warning(
            new_values["staging_dir"], new_values["watch_dir"], new_values["final_export_dir"]
        )
        if warning:
            reply = QtWidgets.QMessageBox.warning(
                self, "Live Sync", warning,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return
        self.engine.apply_and_save_config(new_values)
        super().accept()


# ---------------------------------------------------------------------------
# GUI: ドッキングパネル
# ---------------------------------------------------------------------------

class LiveSyncPanel(QtWidgets.QWidget):

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setObjectName("SPMayaLiveSyncPanel")
        # パネルのタイトルにもバージョンを出し、ログを遡らなくても
        # 現在動作中のバージョンが一目で分かるようにする
        # (maya_live_sync.py のウィンドウタイトルと同じ方式)。
        self.setWindowTitle("Live Sync to Maya  (v{0})".format(__version__))

        layout = QtWidgets.QVBoxLayout(self)

        # --- 層1: 最頻操作 -----------------------------------------------
        # UI導線改善(フェーズ1): 「1セッション中に何度も触るか」を基準に
        # 層1(最頻・大きく最上段固定)/層2(時々・常時表示だが控えめ)/
        # 層3(稀・折りたたみやメニューの奥)の3段階に分ける。
        # maya_live_sync.py の LiveSyncWindow と同じ考え方で揃えることで、
        # SP側・Maya側の操作感覚を統一する。色による区別は環境依存の
        # 配色崩れを避けるため使わず、サイズと配置順序のみで表現する。
        self.enable_checkbox = QtWidgets.QCheckBox("Live Sync を有効にする")
        enable_font = self.enable_checkbox.font()
        enable_font.setBold(True)
        enable_font.setPointSize(enable_font.pointSize() + 1)
        self.enable_checkbox.setFont(enable_font)
        self.enable_checkbox.toggled.connect(self.engine.set_enabled)
        layout.addWidget(self.enable_checkbox)

        # 2026.07.19-02: 書き出し中(SPネイティブの「書き出し中」ダイアログが
        # 消えた後の後処理を含む)であることを示す警告ラベル。実機の
        # クラッシュ再現テストで、このダイアログが消えた後もしばらく
        # 閉じるとクラッシュすることが確認されたため、Pythonコードが
        # 動いている区間全体を通して表示する。根本的な解決ではないが、
        # 人がうっかり閉じてしまう事故を減らすための保険。
        self.exporting_warning_label = QtWidgets.QLabel(
            "⚠ 書き出し処理中です。完了までPainterを閉じないでください。"
        )
        self.exporting_warning_label.setStyleSheet(
            "color: #ffffff; background-color: #b34700; padding: 4px; border-radius: 3px;"
        )
        self.exporting_warning_label.setVisible(False)
        layout.addWidget(self.exporting_warning_label)

        # 2026.07.19-03(フェーズ2): 「保存内容がまだFinal(高画質)側に
        # 反映されていない」ことを示す、より控えめな情報表示。クールダウン
        # 待ち・ロック解除待ちの再試行中も含めて表示される。上の警告
        # (書き出し処理中)とは異なり、閉じても危険ではないので警告色は
        # 使わない。
        self.final_pending_label = QtWidgets.QLabel(
            "⏳ Final(高画質)同期待ち: 保存内容がまだMaya側に反映されていません。"
        )
        self.final_pending_label.setStyleSheet(
            "color: #ffffff; background-color: #3a5a78; padding: 4px; border-radius: 3px;"
        )
        self.final_pending_label.setVisible(False)
        layout.addWidget(self.final_pending_label)

        # UI導線改善(フェーズ1): 「今すぐ同期」は保存を待たずに手元の
        # 変更をすぐ反映したい時に押す操作で、層1の有効化チェックボックスに
        # 次いで頻度が高い。他のボタン群より一段階目立つ高さにして
        # 単独行に配置する。engine.force_sync_now は引数を取らないメソッド
        # のため、Qtのclicked(bool)シグナルの引数はそのまま無視される
        # (force_sync_now側でboolを受け取る仮引数を持たないため安全)。
        self.sync_now_btn = QtWidgets.QPushButton("今すぐ同期")
        self.sync_now_btn.setMinimumHeight(32)
        self.sync_now_btn.clicked.connect(self.engine.force_sync_now)
        layout.addWidget(self.sync_now_btn)

        # フォルダパス・解像度・デバウンス間隔は基本的に初回セットアップ時に
        # 一度決めれば済む項目のため、折りたたみ可能なグループにまとめて
        # 日常的に使う操作(監視ON/OFF・保存・同期履歴)から視覚的に分離する。
        self.settings_group = QtWidgets.QGroupBox("フォルダ・詳細設定(通常は初回のみ)")
        self.settings_group.setCheckable(True)
        self.settings_group.setChecked(True)
        self.settings_group.toggled.connect(self._on_settings_group_toggled)
        form = QtWidgets.QFormLayout(self.settings_group)
        self.staging_edit = self._make_path_row(form, "ステージングフォルダ")
        self.watch_edit = self._make_path_row(form, "監視フォルダ(プレビュー)")
        self.final_edit = self._make_path_row(form, "最終出力フォルダ(保存時)")

        self.resolution_combo = QtWidgets.QComboBox()
        for label, _ in RESOLUTION_CHOICES:
            self.resolution_combo.addItem(label)
        form.addRow("プレビュー解像度", self.resolution_combo)

        self.final_resolution_combo = QtWidgets.QComboBox()
        for label, _ in RESOLUTION_CHOICES:
            self.final_resolution_combo.addItem(label)
        self.final_resolution_combo.setToolTip(
            "保存時にfinal_export_dirへ書き出す高画質版の解像度です。"
            "プロジェクト自体の書き出し設定とは独立した、このパイプライン専用の設定です。"
        )
        form.addRow("最終出力解像度(高画質)", self.final_resolution_combo)

        self.debounce_spin = QtWidgets.QDoubleSpinBox()
        self.debounce_spin.setRange(0.2, 10.0)
        self.debounce_spin.setSingleStep(0.1)
        self.debounce_spin.setSuffix(" 秒")
        form.addRow("デバウンス間隔", self.debounce_spin)

        # 2026.07.19-03(フェーズ2): 保存連打の間引き用クールダウン。
        self.final_cooldown_spin = QtWidgets.QDoubleSpinBox()
        self.final_cooldown_spin.setRange(0.0, 60.0)
        self.final_cooldown_spin.setSingleStep(1.0)
        self.final_cooldown_spin.setSuffix(" 秒")
        self.final_cooldown_spin.setToolTip(
            "前回のFinal(高画質)書き出し完了からこの秒数が経過するまで、"
            "次のFinal書き出しを保留します。保存を連打した際に、重い書き出しが"
            "連続で走り続けるのを防ぎます。0にするとクールダウンなし(従来の挙動)。"
        )
        form.addRow("Final書き出しのクールダウン", self.final_cooldown_spin)

        layout.addWidget(self.settings_group)

        # --- 層2: 時々使う操作 / 層3: 稀な操作 ----------------------------
        # UI導線改善(フェーズ1): 保存・履歴・全量同期は「たまに触る」層2、
        # ウィザードは「初回セットアップ時に一度触ればよい」層3として、
        # maya_live_sync.py の more_btn と同じ「その他の設定」メニュー方式に
        # まとめる。今すぐ同期(層1)を主要ボタン列から独立させたことで、
        # このボタン列は全て層2以下の操作のみになる。
        btn_row = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("設定を保存")
        self.save_btn.clicked.connect(self._on_save_clicked)
        self.history_btn = QtWidgets.QPushButton("同期履歴を開く")
        self.history_btn.setToolTip("SP側/Maya側共通の同期履歴ログ(テキストファイル)を開きます。")
        self.history_btn.clicked.connect(self._open_history_log)
        # 2026.07.19-03(フェーズ2): Final書き出しを差分化したことに伴う
        # 手動の逃げ道。差分状態がMaya側の実ファイルとズレたと感じた場合、
        # このボタンでクールダウンを無視した即時の全量Final書き出しができる。
        self.force_full_btn = QtWidgets.QPushButton("今すぐ全量同期(Final)")
        self.force_full_btn.setToolTip(
            "差分に関わらず、全テクスチャセットのFinal(高画質)書き出しを"
            "クールダウン無視で即座に実行します。差分書き出しの内容が"
            "Maya側とズレていると感じた場合の手動リセット用です。"
        )
        self.force_full_btn.clicked.connect(self.engine.force_full_final_sync)

        self.more_btn = QtWidgets.QToolButton()
        self.more_btn.setText("その他の設定")
        self.more_btn.setToolTip("セットアップウィザートなど、初回導入時に主に使う操作です。")
        self.more_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.more_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        more_menu = QtWidgets.QMenu(self.more_btn)
        wizard_action = more_menu.addAction("セットアップウィザートを開く")
        wizard_action.setToolTip("フォルダ設定・解像度などを対話形式で見直せます。")
        wizard_action.triggered.connect(self.open_setup_wizard)
        self.more_btn.setMenu(more_menu)

        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.history_btn)
        btn_row.addWidget(self.force_full_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.more_btn)
        layout.addLayout(btn_row)

        stats_group = QtWidgets.QGroupBox("同期状況")
        stats_layout = QtWidgets.QFormLayout(stats_group)
        self.last_sync_label = QtWidgets.QLabel("-")
        self.sync_count_label = QtWidgets.QLabel("0")
        self.skip_count_label = QtWidgets.QLabel("0")
        self.error_count_label = QtWidgets.QLabel("0")
        self.duration_label = QtWidgets.QLabel("-")
        stats_layout.addRow("最終同期時刻", self.last_sync_label)
        stats_layout.addRow("同期回数", self.sync_count_label)
        stats_layout.addRow("スキップ回数(差分無し)", self.skip_count_label)
        stats_layout.addRow("エラー回数", self.error_count_label)
        stats_layout.addRow("直近の処理時間", self.duration_label)
        layout.addWidget(stats_group)

        # UI導線改善(フェーズ1再実施): 「テクスチャセット構成」は
        # Maya側との対応関係に疑問を感じた時に確認する層2寄りの情報で、
        # 常時展開しておく必要性は同期状況ほど高くない。
        # maya_live_sync.py 側の material_tab に既に常設のテーブルが
        # あるため、SP側はあくまで補助確認用として折りたたみにする。
        self.structure_toggle_btn = QtWidgets.QPushButton("▸ テクスチャセット構成を確認（Maya側の対応確認用）")
        self.structure_toggle_btn.setFlat(True)
        self.structure_toggle_btn.clicked.connect(self._on_structure_toggle_clicked)
        layout.addWidget(self.structure_toggle_btn)

        self.structure_section = QtWidgets.QWidget()
        structure_section_layout = QtWidgets.QVBoxLayout(self.structure_section)
        structure_section_layout.setContentsMargins(0, 0, 0, 0)
        self.structure_list = QtWidgets.QListWidget()
        self.structure_list.setMaximumHeight(120)
        structure_section_layout.addWidget(self.structure_list)
        recheck_btn = QtWidgets.QPushButton("構成を今すぐチェック")
        recheck_btn.clicked.connect(self.engine.check_texture_set_structure)
        structure_section_layout.addWidget(recheck_btn)
        self.structure_section.setVisible(False)
        layout.addWidget(self.structure_section)

        # UI導線改善(フェーズ1再実施): ログは常時大きく表示する必要は
        # なく、udim_setup.py の折りたたみログと同じ考え方で既定は
        # 閉じておく。ただしエラー発生時に見落とすと困るため、
        # status_changed で受け取ったメッセージにエラーを示す文言が
        # 含まれる場合は自動的に開く(_on_log_message側で判定)。
        self.log_toggle_btn = QtWidgets.QPushButton("▸ ログを表示")
        self.log_toggle_btn.setFlat(True)
        self.log_toggle_btn.clicked.connect(self._on_log_toggle_clicked)
        layout.addWidget(self.log_toggle_btn)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setVisible(False)
        layout.addWidget(self.log_view, stretch=1)

        self._load_values_from_config()
        self._refresh_structure_list([], [], [])

        self.engine.status_changed.connect(self._on_log_message)
        self.engine.stats_changed.connect(self._on_stats_changed)
        self.engine.structure_changed.connect(self._refresh_structure_list)
        self.engine.exporting_changed.connect(self.exporting_warning_label.setVisible)
        self.engine.final_pending_changed.connect(self.final_pending_label.setVisible)

    def _make_path_row(self, form, label):
        row = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QLineEdit()
        browse_btn = QtWidgets.QPushButton("参照...")
        browse_btn.clicked.connect(lambda: self._browse_folder(edit))
        row.addWidget(edit)
        row.addWidget(browse_btn)
        container = QtWidgets.QWidget()
        container.setLayout(row)
        form.addRow(label, container)
        return edit

    def _on_settings_group_toggled(self, checked):
        # チェックを外すと中身を折りたたんで(非表示にして)場所を取らないように
        # する。QGroupBoxの標準動作(チェック解除時に子をdisableするだけ)とは
        # 別に、行そのものを隠すことで見た目もすっきりさせる。
        form = self.settings_group.layout()
        for row in range(form.rowCount()):
            for role in (QtWidgets.QFormLayout.LabelRole, QtWidgets.QFormLayout.FieldRole):
                item = form.itemAt(row, role)
                if item is not None and item.widget() is not None:
                    item.widget().setVisible(checked)

    def _on_structure_toggle_clicked(self):
        # UI導線改善(フェーズ1再実施): QPushButton.clicked は bool を
        # 渡すが、ここでは受け取らずに現在の表示状態を見て自前で反転する。
        # maya_live_sync.py の _on_orphan_toggle_clicked と同じ設計。
        self._set_structure_section_visible(not self.structure_section.isVisible())

    def _set_structure_section_visible(self, visible):
        self.structure_section.setVisible(visible)
        self.structure_toggle_btn.setText(
            "▾ テクスチャセット構成を確認（Maya側の対応確認用）" if visible
            else "▸ テクスチャセット構成を確認（Maya側の対応確認用）"
        )

    def _on_log_toggle_clicked(self):
        self._set_log_visible(not self.log_view.isVisible())

    def _set_log_visible(self, visible):
        self.log_view.setVisible(visible)
        self.log_toggle_btn.setText("▾ ログを表示" if visible else "▸ ログを表示")

    def _on_log_message(self, message):
        # UI導線改善(フェーズ1再実施): ログは既定で折りたたんでいるが、
        # エラーや警告を見落とすとトラブル対応が遅れるため、該当する
        # 文言を含むメッセージを受け取った場合は自動的に開く。
        # udim_setup.py の _print() が [WARN]/[NG] で自動展開する
        # パターンと同じ考え方。
        self.log_view.appendPlainText(message)
        if not self.log_view.isVisible():
            lowered = message.lower()
            if "エラー" in message or "失敗" in message or "warning" in lowered or "error" in lowered:
                self._set_log_visible(True)

    def _browse_folder(self, line_edit):
        current = line_edit.text() or "C:/"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "フォルダを選択", current)
        if selected:
            line_edit.setText(selected)

    def _load_values_from_config(self):
        cfg = self.engine.config
        self.staging_edit.setText(cfg.get("staging_dir", ""))
        self.watch_edit.setText(cfg.get("watch_dir", ""))
        self.final_edit.setText(cfg.get("final_export_dir", ""))

        target_log2 = int(cfg.get("preview_resolution_log2", 9))
        for i in range(len(RESOLUTION_CHOICES)):
            log2 = RESOLUTION_CHOICES[i][1]
            if log2 == target_log2:
                self.resolution_combo.setCurrentIndex(i)
                break

        target_final_log2 = int(cfg.get("final_resolution_log2", 12))
        for i in range(len(RESOLUTION_CHOICES)):
            log2 = RESOLUTION_CHOICES[i][1]
            if log2 == target_final_log2:
                self.final_resolution_combo.setCurrentIndex(i)
                break

        self.debounce_spin.setValue(float(cfg.get("debounce_seconds", 1.5)))
        self.final_cooldown_spin.setValue(float(cfg.get("final_cooldown_seconds", 5.0)))

    def _on_save_clicked(self):
        selected_log2 = RESOLUTION_CHOICES[self.resolution_combo.currentIndex()][1]
        selected_final_log2 = RESOLUTION_CHOICES[self.final_resolution_combo.currentIndex()][1]
        new_values = {
            "staging_dir": self.staging_edit.text().strip(),
            "watch_dir": self.watch_edit.text().strip(),
            "final_export_dir": self.final_edit.text().strip(),
            "preview_resolution_log2": selected_log2,
            "final_resolution_log2": selected_final_log2,
            "debounce_seconds": self.debounce_spin.value(),
            "final_cooldown_seconds": self.final_cooldown_spin.value(),
        }
        for key in ("staging_dir", "watch_dir", "final_export_dir"):
            if not new_values[key]:
                QtWidgets.QMessageBox.warning(self, "Live Sync", "フォルダが未指定です: {0}".format(key))
                return
        warning = _duplicate_folder_warning(
            new_values["staging_dir"], new_values["watch_dir"], new_values["final_export_dir"]
        )
        if warning:
            reply = QtWidgets.QMessageBox.warning(
                self, "Live Sync", warning,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return
        self.engine.apply_and_save_config(new_values)

    def open_setup_wizard(self):
        wizard = SetupWizard(self.engine, self)
        if wizard.exec_() == QtWidgets.QDialog.Accepted:
            self._load_values_from_config()
            # ウィザード完了時にLive Syncを自動的に有効化する。
            # (engine.set_enabled()を直接呼ぶのではなく、チェックボックス側を
            # 更新することで、GUI表示と実際の有効/無効状態のズレを防ぐ)
            self.enable_checkbox.setChecked(True)

    def _open_history_log(self):
        if not os.path.isfile(HISTORY_LOG_PATH):
            QtWidgets.QMessageBox.information(
                self, "Live Sync", "同期履歴はまだありません(同期を開始すると記録され始めます)。"
            )
            return
        try:
            os.startfile(HISTORY_LOG_PATH)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, "Live Sync",
                "履歴ファイルを開けませんでした: {0} (パス: {1})".format(e, HISTORY_LOG_PATH)
            )

    def _on_stats_changed(self, stats):
        self.last_sync_label.setText(stats.get("last_sync_at") or "-")
        self.sync_count_label.setText(str(stats.get("sync_count", 0)))
        self.skip_count_label.setText(str(stats.get("skip_count", 0)))
        self.error_count_label.setText(str(stats.get("error_count", 0)))
        dur = stats.get("last_duration_ms")
        self.duration_label.setText("{0} ms".format(dur) if dur is not None else "-")

    def _refresh_structure_list(self, current, added, removed):
        self.structure_list.clear()
        added_set = set(added)
        removed_set = set(removed)
        for name in current:
            label = "{0} (新規)".format(name) if name in added_set else name
            self.structure_list.addItem(label)
        for name in removed_set:
            self.structure_list.addItem("{0} (削除された可能性)".format(name))


# ---------------------------------------------------------------------------
# プラグインのライフサイクル(Substance Painterが自動的に呼び出す)
# ---------------------------------------------------------------------------

_engine = None
_panel = None


def start_plugin():
    global _engine, _panel
    # SP側・Maya側でバージョンがズレたまま運用してしまい、片方だけ古い
    # ロジックで動いていることに気付けなかった経緯があるため、起動の
    # 最初に必ずバージョンをログへ出す(Pythonログに出力される)。
    _log("info", "[sp_live_sync_plugin] loaded version: {0}  (file: {1})".format(
        __version__, os.path.abspath(__file__)))
    try:
        _engine = LiveSyncEngine()
        # 2026.07.19-01: クラッシュ等でfinally節が実行されず残った
        # ステージングフォルダの残骸を、起動のたびに自動回収する。
        # 失敗しても起動処理自体は継続させたいため、専用のtry/exceptで
        # 囲む(_cleanup_orphaned_staging_dirs自体は内部でOSErrorを
        # 個別に握りつぶしているが、stage_rootの取得自体が失敗する
        # ケースも念のため保護する)。
        try:
            _cleanup_orphaned_staging_dirs(
                _engine.config.get("staging_dir", DEFAULT_CONFIG["staging_dir"]))
        except Exception as e:
            _log("warning", "ステージングフォルダの自動掃除に失敗しました: {0}".format(e))
        _panel = LiveSyncPanel(_engine)
        ui.add_dock_widget(_panel)
        if not _engine.config.get("setup_wizard_completed"):
            _panel.open_setup_wizard()
        _log("info", "SP -> Maya Live Sync (Phase 1 + Phase 2最適化 + Phase 3) を起動しました。")
    except Exception as e:
        _log("error", "プラグインの起動に失敗しました: {0}".format(e))


def close_plugin():
    global _engine, _panel
    # エンジンのイベント購読を先に解除してから参照を手放す。
    # (connect_strong は自動解放されないため、明示的な解除が必須)
    if _engine is not None:
        try:
            _engine.shutdown()
        except Exception as e:
            _log("error", "エンジンの後始末に失敗しました: {0}".format(e))
    if _panel is not None:
        ui.delete_ui_element(_panel)
        _panel = None
    _engine = None
    _log("info", "SP -> Maya Live Sync プラグインを停止しました。")