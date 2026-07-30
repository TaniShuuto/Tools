"""
gate0_check.py  -  Gate 0 (G2/G3/G4) 観測ログ集計スクリプト
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §1.4（Gate 0）

**このスクリプトはGate 0の実測結論を出さない。** 溜まったログを集計し、
「§1.4のG2/G3/G4表を実際に埋めるための材料が揃っているか」を人間可読な
形で報告するだけである。表の実際の記入・実測の可否判断は人間が行う。

Maya API に一切依存しない（ログは既にJSON Linesとしてディスクに書き
出されているため、これを読むだけならMayaもmayapyも不要。通常のpython3で
実行できる）。ただし、AnyPathのログ出力先の既定値
（<Documents>/maya/anypath/logs/）を再現するため、Windows環境の
Documentsパスを既定の探索先として使う。

使い方:
    python gate0_check.py
        既定のログフォルダ（%USERPROFILE%/Documents/maya/anypath/logs）
        を自動探索して集計する。

    python gate0_check.py --log-dir "C:/MayaPrefs/anypath/logs"
        MAYA_APP_DIRをカスタム設定している場合など、ログフォルダを
        明示指定する。

    python gate0_check.py --log-dir "..." --output "gate0_report.txt"
        集計結果をテキストファイルへも書き出す。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。ユーザー指示（Gate 0は実測しない。確認手順・確認スクリプト
          のみ用意する）に基づき新規作成。
"""

__version__ = "1.0.0"

import argparse
import os
import sys

# anypath.core を import できるようにする（このファイルは
# AnyPath/anypath/gate0/ に置かれる想定 -> 2階層上が AnyPath/）。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANYPATH_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
if _ANYPATH_ROOT not in sys.path:
    sys.path.insert(0, _ANYPATH_ROOT)

from anypath.core.gate0_format import gate0_readiness_report, parse_record  # noqa: E402


def _default_log_dir():
    """
    <ユーザーの Documents>/maya/anypath/logs の既定推定。
    MAYA_APP_DIRをカスタム設定している環境では --log-dir で明示指定する
    必要がある（本スクリプトはMaya外で動くため cmds.internalVar が
    使えず、自動検出できない。CLAUDE.md記載の「MAYA_APP_DIRをカスタム
    設定した環境を含む」という前提により、既定値はあくまで一般的な
    Windows環境向けの推測に留める）。
    """
    home = os.path.expanduser("~")
    return os.path.join(home, "Documents", "maya", "anypath", "logs").replace("\\", "/")


def load_all_gate0_records(log_dir):
    records = []
    if not os.path.isdir(log_dir):
        return records
    for filename in sorted(os.listdir(log_dir)):
        if not (filename.startswith("gate0_") and filename.endswith(".jsonl")):
            continue
        full_path = os.path.join(log_dir, filename)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                for line in f:
                    record = parse_record(line)
                    if record is not None:
                        records.append(record)
        except OSError as e:
            print("[gate0_check] 読み込みに失敗しました: {0} ({1})".format(full_path, e))
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="AnyPath Gate 0 (G2/G3/G4) 観測ログ集計スクリプト。"
                     "実測の結論は出さず、材料が揃っているかを報告する。")
    parser.add_argument(
        "--log-dir", default=None,
        help="Gate0ログの探索先フォルダ（既定: ~/Documents/maya/anypath/logs）")
    parser.add_argument(
        "--output", default=None,
        help="集計結果を書き出すテキストファイルのパス（省略時は標準出力のみ）")
    args = parser.parse_args(argv)

    log_dir = args.log_dir or _default_log_dir()
    print("[gate0_check] ログ探索先: {0}".format(log_dir))

    records = load_all_gate0_records(log_dir)
    print("[gate0_check] 読み込んだ観測レコード数: {0}".format(len(records)))
    print("")

    report = gate0_readiness_report(records)
    print(report)

    if args.output:
        try:
            tmp_path = args.output + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(report)
            os.replace(tmp_path, args.output)
            print("")
            print("[gate0_check] レポートを書き出しました: {0}".format(args.output))
        except OSError as e:
            print("[gate0_check] レポートの書き出しに失敗しました: {0}".format(e))
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
