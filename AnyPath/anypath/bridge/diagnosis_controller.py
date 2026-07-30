"""
anypath.bridge.diagnosis_controller  -  Bridge層: 自己診断画面のデータ収集・配線
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §6.9, §6.4
再設計計画書: AnyPath_再設計計画書_v1.md §2 柱1・柱3、§4.3

anypath.shell.diagnosis_panel.DiagnosisPanel はMaya APIに直接触れない
「表示するだけ」のUIコンポーネント。本モジュールが実際のcmds呼び出し・
設定読込・ファイルシステム確認を行い、その結果をパネルへ流し込む。

変更履歴:
    1.3.0 (2026.07.31):
        - ユーザー指摘対応（「確認は右下の通知からのみしかアクセスでき
          ない。Scriptエディタの他のエラーや警告に上書きされてしまい
          アクセスできなくなる」）: open_and_populate() に
          on_open_review_panel（要確認パネルを開く処理のコールバック。
          AnyPath.py の _open_review_panel を渡す想定）を追加し、
          DiagnosisPanel.open_review_panel_requested シグナル
          （diagnosis_panel.py 1.2.0で新設）に接続した。これにより、
          自己診断画面の第1階層に出る「確認する」ボタンから、通知を
          見逃した後でもいつでも要確認パネルを開けるようになった。
          on_open_review_panel が渡されなかった場合はボタンクリックが
          無視されるだけで例外は出さない（後方互換）。
        - 後方互換性: MINOR。on_open_review_panel はキーワード引数
          （省略可）として追加した。
    1.2.1 (2026.07.31):
        - 緊急バグ修正（ユーザー報告: 「探す場所でフォルダを追加すると
          一度ウィンドウが一瞬で再表示され何度選択しても追加されない」）:
          原因は _on_add_root() / _on_remove_root() が
          open_and_populate() を「同じ config_report 引数のまま」
          再帰呼び出ししていたこと。add_search_root() はファイルへは
          正しく書き込めていたが、再帰呼び出しに渡す config_report は
          最初にパネルを開いた時点のスナップショットのままだったため、
          再構築される新しいパネルにも「追加前」のsearch_rootsしか
          反映されず、見た目上「追加できない」状態になっていた。加えて
          再帰のたびに新しいパネルを生成しては即座にcloseする実装で
          あったため、体感としては「一瞬で再表示される」ように見えて
          いた。
          修正: open_and_populate() に config_report_provider
          （呼び出すたびに最新の ConfigLoadReport を返すcallable）を
          追加した。追加・削除操作後は、新しいパネルを作り直す代わりに
          config_report_provider() で最新設定を取得し、既存のパネルの
          探索ルート表示だけを _refresh_search_roots() で更新する
          （パネルの他の状態や開閉状態を保ったまま更新できる）。
          config_report_provider を渡さない古い呼び出し元との後方互換
          のため、渡されなかった場合は従来通りの再構築（close→再生成）
          にフォールバックする。
        - 後方互換性: PATCH。config_report_provider はキーワード引数
          （省略可）として追加した。
    1.2.0 (2026.07.30):
        - 再設計計画書 v1 §4.3 に基づき、DiagnosisPanel の2階層化
          （v1.1.0）に追従。set_status() へ1行サマリ
          （messages.build_status_summary()）を渡すようにし、
          set_recent_summary() で直近の自動修復件数を第1階層に表示する
          ようにした。
        - root_manager.py v1.1.0 のLRU自動管理化（お試し/恒久の区別
          廃止）に追従。_on_add_root() は add_search_root() 1本を呼ぶ
          フローに簡略化し、ファイル数閾値超過時の警告確認ダイアログのみ
          残した（お試し/恒久の選択ダイアログは廃止）。
        - set_search_roots() へ渡す root_infos に last_used_at を含める
          ようにした（is_trialは常にFalseで渡す。後方互換）。
        - 後方互換性: MINOR相当のUI配線変更。open_and_populate() の
          シグネチャ自体は変更しない。
    1.1.0 (2026.07.31):
        - 緊急バグ修正（ユーザー報告: 「探索ルートをどう追加すれば
          いいか分からない」）: これまで探索ルートの追加経路は要確認
          パネル（review_panel.py）の「このフォルダを探索先に追加して
          再試行」ボタンのみを想定していたが、要確認項目が無い状態
          （全て正常、または一度もパス切れを経験していない状態）では
          要確認パネル自体が開かないため、追加手段がUI上のどこにも
          存在しなかった。
          _on_add_root() を新設し、DiagnosisPanel.add_search_root_
          requested に接続した。フォルダ選択ダイアログ ->
          root_manager.evaluate_candidate_root() によるファイル数概算
          （§6.4-1, §6.4-2）-> 閾値超過時の警告ダイアログ（§6.4-3:
          「より深いフォルダを選び直す」を第一の選択肢として提示）->
          お試し/恒久の選択（§6.4-4、既定はお試し）、という一連の
          フローをここに実装した。
        - 後方互換性: MINOR。既存のシグナル・関数シグネチャに変更なし。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §6.9 に基づき新規作成。
"""

__version__ = "1.3.0"

import os

import maya.cmds as cmds

from anypath.bridge.logger import get_log_dir, validate_configured_mappings
from anypath.bridge.root_manager import (
    add_search_root,
    evaluate_candidate_root,
    get_root_usage_info,
    remove_search_root,
)
from anypath.core.gate0_format import gate0_readiness_report, parse_record
from anypath.core.root_guard import estimate_file_count
from anypath.core.types import Confidence
from anypath.shell.messages import build_search_root_too_large_message, build_status_summary


def _build_root_infos(config, machine_config_path):
    """
    現在の config（search_roots）と machine.json 上の使用日時メタ情報から、
    DiagnosisPanel.set_search_roots() へ渡す root_infos を組み立てる。
    """
    search_roots = list(config.get("search_roots", []))
    usage_by_path = {}
    if machine_config_path:
        for info in get_root_usage_info(machine_config_path):
            usage_by_path[info["path"]] = info
    root_infos = []
    for path in search_roots:
        estimate = estimate_file_count(path)
        usage = usage_by_path.get(path, {})
        root_infos.append({
            "path": path, "exists": os.path.isdir(path),
            "estimated_count": estimate.estimated_count,
            "last_used_at": usage.get("last_used_at"),
        })
    return root_infos


def open_and_populate(parent_widget, state, config_report, panel_module=None,
                       config_report_provider=None, on_open_review_panel=None):
    """
    自己診断画面を開き、現在の状態でデータを流し込む。

    state: AnyPath.py の _state 辞書。
    config_report: anypath.core.config.ConfigLoadReport（直近の読込結果）。
    panel_module: テスト時にDiagnosisPanelクラスを差し替えるためのフック
                  （既定はNoneで実クラスをimportする）。
    config_report_provider: 呼び出すたびに最新の ConfigLoadReport を返す
                  callable（省略可。既定はNoneで、AnyPath.py の
                  _load_core_config を渡す想定）。探索ルートの追加/削除後、
                  この関数で最新設定を取り直してからパネルの表示だけを
                  更新する（パネルを閉じて作り直さない）。
                  省略された場合は後方互換のため、追加/削除のたびに
                  パネルを一度閉じて open_and_populate() 自体を再帰
                  呼び出しする従来動作にフォールバックする。
    on_open_review_panel: 「確認する」ボタン押下時に呼ぶcallable
                  （引数なし。AnyPath.py の _open_review_panel を渡す
                  想定）。省略された場合、第1階層の「確認する」ボタンは
                  表示されるがクリックしても何も起きない
                  （後方互換のため例外は出さない）。
    """
    if panel_module is None:
        from anypath.shell.diagnosis_panel import DiagnosisPanel
        panel_module = DiagnosisPanel

    panel = panel_module(parent_widget)

    headline, ok = build_status_summary(state)
    panel.set_status(
        status=state.get("status"),
        mode=state.get("mode"),
        version=state.get("version", "unknown"),
        maya_version_verified=state.get("maya_version_verified", True),
        last_error=state.get("last_error"),
        summary_headline=headline,
        summary_ok=ok,
    )

    auto_fixed_count, review_count, unresolved_ref_count = _summarize_last_run(state)
    panel.set_recent_summary(auto_fixed_count, review_count, unresolved_ref_count)

    if config_report is not None:
        panel.set_config_report(
            config_report.sources, config_report.parse_errors, config_report.clamp_events)
        config = config_report.config
    else:
        config = {}

    machine_config_path = state.get("machine_config_path")

    panel.set_search_roots(_build_root_infos(config, machine_config_path))

    mapping_infos = validate_configured_mappings(config.get("path_mappings", []))
    panel.set_mappings(mapping_infos)

    panel.set_index_status(built=False, file_count=0, timed_out=False)

    try:
        dirmap_enabled = bool(cmds.dirmap(query=True, enable=True))
    except Exception:
        dirmap_enabled = False
    panel.set_dirmap_status(dirmap_enabled, len(config.get("path_mappings", [])))

    def _on_dry_run(input_path):
        try:
            result = cmds.dirmap(convertDirectory=input_path)
        except Exception as e:
            result = "確認に失敗しました: {0}".format(e)
        panel.set_dry_run_result(result)
    panel.dirmap_dry_run_requested.connect(_on_dry_run)

    log_dir = get_log_dir()
    panel.set_log_dir(log_dir)

    def _on_open_log_folder():
        try:
            if os.name == "nt":
                os.startfile(log_dir)
        except Exception as e:
            print("[AnyPath] ログフォルダを開けませんでした: {0}".format(e))
    panel.open_log_folder_requested.connect(_on_open_log_folder)

    duplicates = state.get("duplicate_mods", [])
    panel.set_duplicate_installs(duplicates)

    gate0_records = _load_gate0_records(log_dir)
    panel.set_gate0_report(gate0_readiness_report(gate0_records))

    def _refresh_search_roots_in_place():
        """
        設定を最新の状態から読み直し、探索ルート一覧の表示だけを
        その場で更新する（パネルを閉じて作り直さない）。
        config_report_provider が渡されている場合のみ使う経路。
        """
        latest_report = config_report_provider()
        latest_config = latest_report.config if latest_report is not None else {}
        panel.set_search_roots(_build_root_infos(latest_config, machine_config_path))

    def _on_remove_root(path, _is_trial):
        # 再設計後は「お試し」概念が無いため is_trial は常に無視する
        # （後方互換のためシグナル自体は (path, bool) のまま残す）。
        if machine_config_path:
            remove_search_root(path, machine_config_path)
        if config_report_provider is not None:
            _refresh_search_roots_in_place()
        else:
            # 後方互換フォールバック（config_report_provider 未指定時）。
            open_and_populate(parent_widget, state, config_report, panel_module)
            panel.close()
    panel.remove_search_root_requested.connect(_on_remove_root)

    def _on_add_root():
        _handle_add_search_root(parent_widget, machine_config_path)
        if config_report_provider is not None:
            _refresh_search_roots_in_place()
        else:
            # 後方互換フォールバック（config_report_provider 未指定時）。
            open_and_populate(parent_widget, state, config_report, panel_module)
            panel.close()
    panel.add_search_root_requested.connect(_on_add_root)

    def _on_open_review_panel():
        if on_open_review_panel is not None:
            on_open_review_panel()
    panel.open_review_panel_requested.connect(_on_open_review_panel)

    panel.show()
    return panel


def _summarize_last_run(state):
    """
    state["last_results"] / state["last_outcomes"] /
    state["last_unresolved_references"] から、第1階層に出す3つの件数
    （自動で直した件数・確認が必要な件数・見つからない参照シーンの件数）
    を集計する。
    """
    outcomes = state.get("last_outcomes") or []
    auto_fixed_count = sum(1 for o in outcomes if getattr(o, "applied", False))

    results = state.get("last_results") or []
    review_count = sum(
        1 for r in results
        if getattr(r, "confidence", None) in (Confidence.REVIEW, Confidence.FAILED)
    )

    unresolved_refs = state.get("last_unresolved_references") or []
    return auto_fixed_count, review_count, len(unresolved_refs)


def _handle_add_search_root(parent_widget, machine_config_path):
    """
    再設計後の探索ルート追加フロー（§4.3）。
    フォルダ選択 -> ファイル数概算 -> 閾値超過時のみ警告確認 ->
    add_search_root() で自動永続化、という最短経路にした。
    「お試し/恒久」を選ばせる工程は廃止済み。
    """
    from PySide6 import QtWidgets

    if not machine_config_path:
        return

    selected_dir = QtWidgets.QFileDialog.getExistingDirectory(
        parent_widget, "探す場所に追加するフォルダを選択")
    if not selected_dir:
        return

    estimate, suggestions = evaluate_candidate_root(selected_dir)

    if estimate.exceeds_threshold:
        msg = build_search_root_too_large_message(
            estimate.estimated_count, _threshold_for_message())
        detail = msg.to_text()
        if suggestions:
            detail += "\n\n候補:\n" + "\n".join("  - {0}".format(s) for s in suggestions)
        choice = cmds.confirmDialog(
            title="AnyPath - 確認",
            message=detail,
            button=["このまま追加する", "やめる"],
            defaultButton="やめる",
            cancelButton="やめる",
        )
        if choice != "このまま追加する":
            return

    ok, message, evicted = add_search_root(selected_dir, machine_config_path)
    if not ok and message:
        cmds.confirmDialog(title="AnyPath", message=message, button=["OK"])
    elif evicted:
        cmds.confirmDialog(
            title="AnyPath",
            message="追加しました。\n\n件数がいっぱいだったため、しばらく"
                    "使っていなかった次の場所を自動的に外しました:\n{0}".format(evicted),
            button=["OK"],
        )


def _threshold_for_message():
    from anypath.core.root_guard import DEFAULT_FILE_COUNT_THRESHOLD
    return DEFAULT_FILE_COUNT_THRESHOLD


def _load_gate0_records(log_dir):
    """
    log_dir 配下の gate0_*.jsonl を全て読み込み、レコードのリストを返す
    （複数プロセス・複数日にまたがるログを横断して集計するため）。
    """
    records = []
    if not os.path.isdir(log_dir):
        return records
    try:
        filenames = sorted(os.listdir(log_dir))
    except OSError:
        return records

    for filename in filenames:
        if not (filename.startswith("gate0_") and filename.endswith(".jsonl")):
            continue
        full_path = log_dir.rstrip("/\\") + "/" + filename
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                for line in f:
                    record = parse_record(line)
                    if record is not None:
                        records.append(record)
        except OSError:
            continue
    return records
