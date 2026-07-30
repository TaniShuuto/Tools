"""
anypath.core.gate0_format  -  Gate 0 (G2/G3/G4) 挙動確認ログのフォーマット（Maya非依存）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §1.4（Gate 0 ―― 実装着手前に決着させる検証項目）

**重要: このモジュール自体はGate 0の実測を行わない。**
Gate 0（G2: dirmap実効果の実測、G3: userSetup.py実行挙動の確認、
G4: filePathEditor網羅性の実測）は、実際のMaya環境上で人間が確認する
必要がある（本セッションにはMaya実行環境がないため）。

本モジュール群が提供するのは:
    1. Maya実機での通常運用中に、G2/G3/G4それぞれの判断材料となる
       「観測データ」を自動的に記録するログフォーマット
    2. 記録されたログを読み込み、§1.4の表（G2/G3/G4）を実際に埋められる
       状態になっているかどうかを判定するロジック

人間の実機作業とこのログ機構の役割分担:
    - 人間: Mayaを実際に操作する（プラグインをロードする、dirmapが効く
      はずのシーンを開く、userSetup.pyの構成を変えて起動する等）。
      手順は Gate0_検証手順.md を参照。
    - ログ機構: その操作中に自動的に証拠となるログを記録する。
    - gate0_check.py（Boot層寄りの確認スクリプト）: 溜まったログを集計し、
      「G2の表を埋めるための実測データが揃ったか」を報告する。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。ユーザー指示（Gate 0は実測しない。ログ出力機能と確認手順・
          確認スクリプトのみ用意する）に基づき新規作成。
"""

__version__ = "1.0.0"

import json

# ログレコードの種別。G2/G3/G4 それぞれに対応する。
GATE0_RECORD_TYPES = ("G2_DIRMAP_OBSERVATION", "G3_USERSETUP_OBSERVATION", "G4_FILEPATHEDITOR_OBSERVATION")


def build_g2_record(timestamp, node_type, attr_name, pre_dirmap_path,
                     post_dirmap_path, dirmap_enabled, mappings_active):
    """
    G2: dirmapが実際に効く対象ノード種の実測用レコード。

    「dirmapの効果」はPython側から直接は見えないため（§6.7と同じ制約）、
    kBeforeOpen相当の前段階の値を推測できない。そのため、このレコードは
    以下の2値の比較材料として設計している:
        - pre_dirmap_path: AnyPathがオープン後に最初に読んだ属性値
          （snapshot_after_open由来。dirmapが効いていれば、既に変換後の
          値になっているはず）
        - mappings_active: そのとき設定されていたpath_mappings

    人間が「dirmapに引っかかるはずの壊れたパスを持つシーン」を意図的に
    用意して開いた際に、post_dirmap_path が期待通りの実在パスに変わって
    いるかどうかを、後から目視 or gate0_check.py で確認する想定。
    """
    return {
        "record_type": "G2_DIRMAP_OBSERVATION",
        "timestamp": timestamp,
        "node_type": node_type,
        "attr_name": attr_name,
        "observed_path": post_dirmap_path,
        "dirmap_enabled": dirmap_enabled,
        "mappings_active": mappings_active,
        "pre_dirmap_path_hint": pre_dirmap_path,
    }


def build_g3_record(timestamp, script_path, invocation_index, maya_version,
                     is_batch):
    """
    G3: userSetup.py の実行挙動確認用レコード。

    AnyPath.py は .mod+autoloadプラグイン方式を採用しており、
    userSetup.py 経路には依存しないため、通常運用ではこのレコードは
    生成されない。G3確認用に、確認手順書の指示に従って人間が一時的に
    診断用の userSetup.py 断片を配置した際にのみ、そのスクリプトから
    このレコードを書き込む（gate0/userSetup_probe.py 参照）。

    invocation_index: 同一プロセス内で何回目の呼び出しかを表す連番。
    複数回呼ばれる（＝単一実行ではない）ことを実測するための値。
    """
    return {
        "record_type": "G3_USERSETUP_OBSERVATION",
        "timestamp": timestamp,
        "script_path": script_path,
        "invocation_index": invocation_index,
        "maya_version": maya_version,
        "is_batch": is_batch,
    }


def build_g4_record(timestamp, filepatheditor_node_types, registry_node_types,
                     missing_from_filepatheditor, extra_in_filepatheditor):
    """
    G4: filePathEditor系の網羅性実測用レコード。

    filepatheditor_node_types: cmds.filePathEditor(query=True, ...) 等から
        実際に列挙できたノードタイプの一覧（実測値）。
    registry_node_types: 明示レジストリ（§8, bridge.registry）に登録されて
        いるノードタイプの一覧。
    missing_from_filepatheditor: レジストリにはあるがfilePathEditorには
        現れなかったノードタイプ（filePathEditorの「漏れ」の候補）。
    extra_in_filepatheditor: filePathEditorには現れたがレジストリには
        ないノードタイプ（レジストリ拡充の候補）。
    """
    return {
        "record_type": "G4_FILEPATHEDITOR_OBSERVATION",
        "timestamp": timestamp,
        "filepatheditor_node_types": sorted(filepatheditor_node_types),
        "registry_node_types": sorted(registry_node_types),
        "missing_from_filepatheditor": sorted(missing_from_filepatheditor),
        "extra_in_filepatheditor": sorted(extra_in_filepatheditor),
    }


def serialize_record(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def parse_record(line):
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("record_type") not in GATE0_RECORD_TYPES:
        return None
    return data


def summarize_g2(records):
    """
    G2レコード群から、§1.4の表を埋めるための集計を行う。
    ノードタイプごとに観測件数を出す（「実際に観測できたか」の目安。
    「dirmapが効いたかどうか」自体の自動判定はしない ―― それは人間が
    pre/postを見比べて判断する。ここでは「判断材料が十分に溜まったか」
    だけを報告する）。
    """
    by_node_type = {}
    for r in records:
        if r.get("record_type") != "G2_DIRMAP_OBSERVATION":
            continue
        nt = r.get("node_type", "unknown")
        by_node_type.setdefault(nt, []).append(r)

    summary = {}
    for nt in sorted(by_node_type.keys()):
        summary[nt] = {
            "observation_count": len(by_node_type[nt]),
            "sample": by_node_type[nt][:3],
        }
    return summary


def summarize_g3(records):
    """
    G3レコード群から、「userSetup.pyが単一実行か複数実行か」を判定する
    材料を集計する。同一 script_path に対する invocation_index の最大値が
    1を超えていれば「複数回呼ばれた」ことが実機で確認できたことになる。
    """
    by_script = {}
    for r in records:
        if r.get("record_type") != "G3_USERSETUP_OBSERVATION":
            continue
        path = r.get("script_path", "unknown")
        by_script.setdefault(path, []).append(r.get("invocation_index", 0))

    summary = {}
    for path in sorted(by_script.keys()):
        indices = by_script[path]
        summary[path] = {
            "call_count": len(indices),
            "max_invocation_index": max(indices) if indices else 0,
        }
    return summary


def summarize_g4(records):
    """G4レコード群から最新の観測結果を返す（差分は都度計算済みのものを使う）。"""
    g4_records = [r for r in records if r.get("record_type") == "G4_FILEPATHEDITOR_OBSERVATION"]
    if not g4_records:
        return None
    # timestampの文字列比較で最新を採用（ISO8601形式であれば辞書順=時系列順）。
    latest = max(g4_records, key=lambda r: r.get("timestamp", ""))
    return latest


def gate0_readiness_report(all_records):
    """
    §1.4のG2/G3/G4表を実際に埋められる状態かどうかの、機械的な判定結果を
    組み立てる。「結論を出す」のではなく「結論を出すための材料が揃ったか」
    を報告する点に注意（実測の是非判断そのものは人間が行う）。
    """
    g2 = summarize_g2(all_records)
    g3 = summarize_g3(all_records)
    g4 = summarize_g4(all_records)

    report_lines = []
    report_lines.append("=== Gate 0 準備状況レポート ===")
    report_lines.append("")
    report_lines.append("[G2: dirmap実効果] 観測されたノードタイプ: {0}".format(
        len(g2) if g2 else 0))
    if g2:
        for nt, info in g2.items():
            report_lines.append("  - {0}: 観測 {1} 件".format(nt, info["observation_count"]))
        report_lines.append("  -> §5.2の表を埋める材料が揃っています。"
                             "サンプルのpre/postを見比べて実効果を判断してください。")
    else:
        report_lines.append("  -> まだ観測データがありません。"
                             "Gate0_検証手順.md のG2手順を実施してください。")
    report_lines.append("")

    report_lines.append("[G3: userSetup.py挙動] 観測されたスクリプト: {0}".format(
        len(g3) if g3 else 0))
    if g3:
        for path, info in g3.items():
            report_lines.append("  - {0}: 呼び出し{1}回, 最大連番{2}".format(
                path, info["call_count"], info["max_invocation_index"]))
        report_lines.append("  -> §2.2の記述との整合を確認してください。")
    else:
        report_lines.append("  -> まだ観測データがありません。"
                             "Gate0_検証手順.md のG3手順を実施してください"
                             "（AnyPath自体は.mod方式のためG3非依存です。"
                             "これは設計書記述の裏付け確認用です）。")
    report_lines.append("")

    report_lines.append("[G4: filePathEditor網羅性]")
    if g4:
        missing = g4.get("missing_from_filepatheditor", [])
        extra = g4.get("extra_in_filepatheditor", [])
        report_lines.append("  観測日時: {0}".format(g4.get("timestamp")))
        report_lines.append("  レジストリのみに存在（filePathEditorの漏れ候補）: {0}".format(
            ", ".join(missing) if missing else "なし"))
        report_lines.append("  filePathEditorのみに存在（レジストリ拡充候補）: {0}".format(
            ", ".join(extra) if extra else "なし"))
        if missing:
            report_lines.append("  -> 漏れが確認されたため、§8の方針通り明示レジストリのみを"
                                 "唯一の正として扱ってください。")
        else:
            report_lines.append("  -> 漏れは確認されませんでした。補助併用が可能か検討できます。")
    else:
        report_lines.append("  -> まだ観測データがありません。"
                             "Gate0_検証手順.md のG4手順を実施してください。")

    return "\n".join(report_lines)
