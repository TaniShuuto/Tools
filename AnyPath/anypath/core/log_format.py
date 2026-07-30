"""
anypath.core.log_format  -  AnyPath: 修復ログ・レポートのフォーマット（Maya非依存）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §6.7（ログとレポート）

このモジュールは JSON Lines のレコード組み立て・ログファイル名の決定・
レポートのテキスト化ロジックのみを担う純粋関数群であり、実際のファイル
I/O（書き込み先の解決・追記処理）は Bridge層（logger.py）が担当する。
Core層に置くことで、フォーマットの妥当性を Maya 非依存で pytest 検証できる
ようにしている（§2.1の精神を、ログ機能についても可能な範囲で踏襲する）。

要件（§6.7）:
    - 全修復を JSON Lines 形式でローカルへ追記記録する
      {timestamp, scene, node, attr, before, after, strategy, confidence}
    - before（修復前の元パス）の記録は必須要件
    - 複数 Maya インスタンスの同時起動を想定し、ログファイル名に
      プロセスIDを含める
    - レポートのテキストエクスポート（v2.1新設・必須）

パス匿名化（§6.7）:
    「UI上に『パスを匿名化して表示／コピー』オプションを設ける」
    ―― anonymize_path() がその変換ロジックを提供する。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §6.7 に基づき新規作成。
"""

__version__ = "1.0.0"

import json
import re

from anypath.core.types import Confidence, Strategy


def log_filename(pid, date_str):
    """
    §6.7: 「複数 Maya インスタンスの同時起動を想定し、ログファイル名に
    プロセスIDを含める」。日付込みにすることでログローテーションを兼ねる
    （無限に1ファイルへ追記し続けることを避ける）。
    """
    return "anypath_{0}_pid{1}.jsonl".format(date_str, pid)


def build_log_record(timestamp, scene, node, attr, before, after, strategy, confidence):
    """
    JSON Lines 1行分のレコードを組み立てる。
    §6.7: {timestamp, scene, node, attr, before, after, strategy, confidence}

    before は必須（Noneを許容しない。§1.3: 「.mbでは他に元パスを知る手段が
    なく、これを失うと復旧の起点が消える」）。呼び出し側が必ず値を渡すこと。
    """
    if before is None:
        raise ValueError(
            "before（修復前の元パス）は必須です。.mb環境では唯一の復旧の"
            "起点であり、Noneを記録してはなりません（設計書§1.3, §6.7）。")

    strategy_value = strategy.value if isinstance(strategy, Strategy) else strategy
    confidence_value = confidence.value if isinstance(confidence, Confidence) else confidence

    return {
        "timestamp": timestamp,
        "scene": scene,
        "node": node,
        "attr": attr,
        "before": before,
        "after": after,
        "strategy": strategy_value,
        "confidence": confidence_value,
    }


def serialize_log_line(record):
    """レコード(dict)をJSON Lines の1行（末尾改行なし）へシリアライズする。"""
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def parse_log_line(line):
    """JSON Lines の1行をレコード(dict)へパースする。壊れた行はNoneを返す。"""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


# --- パス匿名化（§6.7: 「パスを匿名化して表示／コピー」オプション） --------
# Windowsのユーザーディレクトリパターン（C:/Users/<name>/...）を検出し、
# ユーザー名部分を置換する。UNC共有パス上のユーザーディレクトリ
# （//server/Users/<name>/...）にも対応する。
_USER_DIR_PATTERN = re.compile(
    r"((?:[A-Za-z]:|//[^/\\]+)[/\\][Uu]sers[/\\])([^/\\]+)", re.UNICODE)


def anonymize_path(path):
    """
    パス中のユーザー名部分を "<user>" に置換して返す。
    スクリーンショットやIssue添付でユーザー名を含むフルパスが漏れることを
    防ぐための表示専用の変換（実際の解決処理には一切使わない）。
    """
    if not path:
        return path
    return _USER_DIR_PATTERN.sub(lambda m: m.group(1) + "<user>", path)


# --- レポートのテキストエクスポート（v2.1新設・必須） -----------------------
def build_text_report(scene_path, outcomes, generated_at, anonymize=False):
    """
    要確認パネルの内容を、人間可読なテキストファイルとして書き出すための
    本文を組み立てる（§6.7: 「Maya内UIへ到達できない事態でも参照できる
    形で残すため」）。

    outcomes: dict のリスト。各要素は最低限
        {"node": str, "attr": str, "before": str, "after": str or None,
         "strategy": str, "confidence": str}
      を持つことを期待する（bridge.applier.ApplyOutcome から変換して渡す
      想定。ここでは辞書として受け取ることでMaya非依存を保つ）。
    """
    lines = []
    lines.append("AnyPath レポート")
    lines.append("生成日時: {0}".format(generated_at))
    lines.append("シーン: {0}".format(
        anonymize_path(scene_path) if anonymize else scene_path))
    lines.append("")

    by_confidence = {"EXACT": [], "HIGH": [], "REVIEW": [], "FAILED": []}
    for o in outcomes:
        conf = o.get("confidence", "FAILED")
        by_confidence.setdefault(conf, []).append(o)

    auto_count = len(by_confidence.get("EXACT", [])) + len(by_confidence.get("HIGH", []))
    lines.append("[自動修復] {0} 件".format(auto_count))
    lines.append("[要確認] {0} 件".format(len(by_confidence.get("REVIEW", []))))
    lines.append("[見つかりません] {0} 件".format(len(by_confidence.get("FAILED", []))))
    lines.append("")

    for conf_label, header in (
        ("REVIEW", "=== 確認が必要 ==="),
        ("FAILED", "=== 見つかりませんでした ==="),
        ("HIGH", "=== 自動で直しました（構造照合あり） ==="),
        ("EXACT", "=== 自動で直しました ==="),
    ):
        items = by_confidence.get(conf_label, [])
        if not items:
            continue
        lines.append(header)
        for o in sorted(items, key=lambda x: (x.get("node", ""), x.get("attr", ""))):
            before = o.get("before")
            after = o.get("after")
            if anonymize:
                before = anonymize_path(before)
                after = anonymize_path(after)
            lines.append("  ノード: {0}.{1}".format(o.get("node"), o.get("attr")))
            lines.append("    元のパス: {0}".format(before))
            if after:
                lines.append("    修正後  : {0}".format(after))
            lines.append("")

    return "\n".join(lines)


# --- マッピング表の妥当性検証（§6.7末尾） ------------------------------------
def validate_path_mappings(path_mappings, exists_fn):
    """
    設定されたマッピング表の妥当性そのものを検証する
    （「古いマッピングが今は間違った場所を指している」問題への対応）。
    各マッピングの to が実在するかを確認し、実在しないものを警告対象として返す。

    戻り値: [{"from":..., "to":..., "to_exists": bool}, ...]
    """
    results = []
    for mapping in path_mappings:
        to = mapping.get("to")
        exists = bool(to) and exists_fn(to)
        results.append({
            "from": mapping.get("from"),
            "to": to,
            "to_exists": exists,
        })
    return results
