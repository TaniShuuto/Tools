"""
anypath.bridge.logger  -  Bridge層: 修復ログの書き込み（実ファイルI/O）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §6.7（ログとレポート）

フォーマット・匿名化・レポート組み立てロジックは anypath.core.log_format
（Maya非依存）に分離されている。本モジュールは「どこに」「どうやって
安全に」書き込むかを担当する。

要件:
    - 出力先はユーザーローカルに固定する:
      <ユーザーの Documents>/maya/anypath/logs/
      設定で任意指定できるようにすると、Git管理下のプロジェクトフォルダへ
      出力され、個人PCのフルパス情報がコミットされる事故が起こる
    - 複数Mayaインスタンスの同時起動を想定し、ログファイル名にプロセスIDを
      含める

dirmap経由の修復の記録方法（§6.7、重要）:
    dirmapはMaya内部で透過的にパスを書き換えるため、Python側から
    「dirmapが何をしたか」を直接知ることはできない。したがって
    「dirmapに何をさせたか」ではなく「開いた直後の実測値」を記録する。
        1. kAfterOpen の最初の処理として、対象となる全パス属性の実測値を
           スナップショットとして記録する（この時点でdirmapの効果は
           既に反映されている）
        2. AnyPath自身が書き換えた分は、その前後の値を個別に記録する
        3. ログには「オープン直後の状態」と「AnyPathによる変更」が
           明確に区別されて残る
    この方式はシーンファイルを開く前にテキストを読む必要がないため、
    .mbでも同じ精度で機能する（§1.3）。

書き込みの安全性（CLAUDE.md規約）:
    他プロセスが同時に読む可能性のあるファイルシステムへの書き込みは、
    すべて一時ファイル＋os.replace()で行う。ただし本モジュールのログは
    「追記専用」であり、複数プロセスが同一ファイルへ同時追記する状況を
    避けるため、ファイル名にプロセスIDを含めることで書き込み競合そのものを
    構造的に回避している（同一プロセス内での追記はアトミック性の懸念が
    小さいオープンモード"a"で行う）。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §6.7 に基づき新規作成。
"""

__version__ = "1.0.0"

import datetime
import os

import maya.cmds as cmds

from anypath.core.log_format import (
    build_log_record,
    build_text_report,
    log_filename,
    serialize_log_line,
    validate_path_mappings,
)


def get_log_dir():
    """
    §6.7: 出力先はユーザーローカルに固定する
    <ユーザーの Documents>/maya/anypath/logs/

    cmds.internalVar(userAppDir=True) は通常 ".../Documents/maya/" を
    指すため、その配下に anypath/logs を作る（AnyPath.py の
    get_anypath_log_dir() と同じ計算式。循環importを避けるためここでも
    独立して計算する）。
    """
    app_dir = cmds.internalVar(userAppDir=True).rstrip("/\\").replace("\\", "/")
    return app_dir + "/anypath/logs"


def _ensure_log_dir():
    log_dir = get_log_dir()
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def current_log_path():
    """本セッション（本プロセス）が書き込むべきログファイルの絶対パス。"""
    log_dir = _ensure_log_dir()
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    pid = os.getpid()
    return log_dir + "/" + log_filename(pid, date_str)


def append_log_records(records):
    """
    レコード（dict）のリストを JSON Lines として現在のログファイルへ追記する。
    §4章の各種try/exceptと同様、ログ書き込み自体の失敗でMayaの動作を
    妨げてはならない（P4）ため、例外は内部で握りつぶし、標準出力に警告する。
    """
    if not records:
        return
    path = current_log_path()
    try:
        with open(path, "a", encoding="utf-8") as f:
            for record in records:
                f.write(serialize_log_line(record))
                f.write("\n")
    except Exception as e:
        print("[AnyPath][WARNING] ログの書き込みに失敗しました: {0}".format(e))


def log_apply_outcomes(scene_path, apply_outcomes):
    """
    bridge.applier.apply_resolutions() の戻り値（ApplyOutcomeのリスト）を
    JSON Linesレコードへ変換して記録する。

    §6.7: before（修復前の元パス）の記録は必須要件。ApplyOutcome.before は
    常に result.raw_path（Coreへ渡した壊れたパス文字列そのもの）が入って
    いるため、ここでNoneになることはない設計だが、念のため空文字列扱いに
    フォールバックして記録自体は必ず残す（ログを欠落させないことを優先）。
    """
    timestamp = datetime.datetime.now().isoformat()
    records = []
    for outcome in apply_outcomes:
        before = outcome.before if outcome.before is not None else ""
        records.append(build_log_record(
            timestamp=timestamp,
            scene=scene_path,
            node=outcome.node_full_path,
            attr=outcome.attr_name,
            before=before,
            after=outcome.after,
            strategy=outcome.strategy,
            confidence=outcome.confidence,
        ))
    append_log_records(records)
    return records


def snapshot_after_open(collected_attrs):
    """
    §6.7: 「kAfterOpen の最初の処理として、対象となる全パス属性の実測値を
    スナップショットとして記録する（この時点でdirmapの効果は既に反映
    されている）」。

    collector.collect_all() の戻り値をそのまま JSON Lines へ記録する
    （strategy/confidenceはまだ確定していないため "SNAPSHOT" という
    擬似的な種別で記録し、後続の AnyPath による変更ログと明確に区別する。
    §6.7の3.: 「ログには『オープン直後の状態』と『AnyPathによる変更』が
    明確に区別されて残る」を満たすための実装）。
    """
    timestamp = datetime.datetime.now().isoformat()
    scene_path = cmds.file(query=True, sceneName=True) or "<unsaved>"

    records = []
    for c in collected_attrs:
        if not c.raw_value:
            continue
        records.append({
            "timestamp": timestamp,
            "scene": scene_path,
            "node": c.node_full_path,
            "attr": c.attr_name,
            "before": c.raw_value,
            "after": c.raw_value,
            "strategy": "SNAPSHOT_AFTER_OPEN",
            "confidence": "N/A",
        })
    append_log_records(records)
    return records


def write_text_report(scene_path, outcomes_as_dicts, anonymize=False, out_path=None):
    """
    §6.7新設・必須: レポートのテキストエクスポート。
    out_path が None の場合、ログフォルダ配下に日時付きファイル名で書き出す。
    戻り値: 書き出したファイルの絶対パス（失敗時はNone）。
    """
    generated_at = datetime.datetime.now().isoformat()
    text = build_text_report(scene_path, outcomes_as_dicts, generated_at, anonymize=anonymize)

    if out_path is None:
        log_dir = _ensure_log_dir()
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = "{0}/report_{1}_pid{2}.txt".format(log_dir, stamp, os.getpid())

    # CLAUDE.md規約: 一時ファイル＋os.replace()でアトミックに書き込む。
    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, out_path)
        return out_path
    except Exception as e:
        print("[AnyPath][WARNING] レポートの書き出しに失敗しました: {0}".format(e))
        return None


def validate_configured_mappings(path_mappings):
    """
    §6.7末尾: 設定されたマッピング表の妥当性を検証する（自己診断画面向け）。
    """
    return validate_path_mappings(path_mappings, exists_fn=os.path.isdir)
