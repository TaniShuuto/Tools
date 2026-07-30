"""
userSetup_probe.py  -  Gate 0 G3 検証用の診断プローブ
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §1.4（G3: userSetup.py の実行挙動確認）
        §2.2 の訂正（v1からの変更）: 「userSetup.py は検索パス上で発見された
        全ファイルが順に実行される」という記述の実機確認用。

**このファイルはAnyPath本体の一部ではない。** AnyPath自体は.mod+autoload
プラグイン方式を採用しており、userSetup.py経路には一切依存しない
（§2.2: 「userSetup.mel は採用しません」「.mod方式を採用する理由」）。

このプローブは、設計書 §2.2 の記述（「単一のファイルしか実行しないのは
userSetup.mel であり、userSetup.py は検索パス上で発見された全ファイルが
順に実行される」）が対象Mayaバージョンで実際に成り立つかどうかを、
Gate0_検証手順.md の指示に従って人間が確認するための使い捨てスクリプト。

使い方（Gate0_検証手順.md からの抜粋。詳細は同ファイルを参照）:
    1. このファイルを、複数の異なる scripts フォルダ（例:
       <MAYA_APP_DIR>/scripts/userSetup.py と
       <MAYA_APP_DIR>/<version>/scripts/userSetup.py の両方）へ
       コピーし、それぞれ import 文の後ろに一意な識別子を渡して呼び出す
       よう1行加える（下記の「使用例」参照）。
    2. Mayaを起動する。
    3. 自己診断画面（§6.9）の「Gate 0 観測状況」セクション、または
       gate0_check.py を実行し、G3の呼び出し回数を確認する。
    4. 確認後、このプローブ用に追加した userSetup.py の内容は必ず元に
       戻す（本番運用でAnyPathのuserSetup.py依存を作らないため）。

使用例（scripts/userSetup.py へ追記する1行、識別子は配置場所ごとに変える）:
    import userSetup_probe; userSetup_probe.record_invocation("user_scripts_dir")

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。ユーザー指示（Gate 0は実測しない。確認手順・確認スクリプト
          のみ用意する）に基づき新規作成。
"""

__version__ = "1.0.0"

import datetime
import os

# このプロセス内での呼び出し回数（複数回importされた場合に増える。
# ただしPythonのモジュールキャッシュにより、通常は同一パスからのimportは
# 1回しかモジュール本体が実行されないため、複数の"異なる"userSetup.pyから
# それぞれ record_invocation を呼ぶことで検証する設計になっている）。
_invocation_count = 0


def _log_dir():
    try:
        import maya.cmds as cmds
        app_dir = cmds.internalVar(userAppDir=True).rstrip("/\\").replace("\\", "/")
        return app_dir + "/anypath/logs"
    except Exception:
        # Maya外や取得失敗時のフォールバック（診断用スクリプトなので
        # 動作継続を優先する）。
        return os.path.expanduser("~") + "/anypath_gate0_fallback_logs"


def record_invocation(script_identifier):
    """
    userSetup.py 経路から呼ばれる想定の記録関数。
    G3観測レコード(anypath.core.gate0_format.build_g3_record)を組み立てて
    ログへ追記する。
    """
    global _invocation_count
    _invocation_count += 1

    try:
        import maya.cmds as cmds
        maya_version = cmds.about(version=True)
        is_batch = cmds.about(batch=True)
    except Exception:
        maya_version = "unknown"
        is_batch = None

    from anypath.core.gate0_format import build_g3_record, serialize_record

    timestamp = datetime.datetime.now().isoformat()
    record = build_g3_record(
        timestamp=timestamp,
        script_path=script_identifier,
        invocation_index=_invocation_count,
        maya_version=maya_version,
        is_batch=is_batch,
    )

    log_dir = _log_dir()
    try:
        os.makedirs(log_dir, exist_ok=True)
        pid = os.getpid()
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        path = "{0}/gate0_{1}_pid{2}.jsonl".format(log_dir, date_str, pid)
        with open(path, "a", encoding="utf-8") as f:
            f.write(serialize_record(record))
            f.write("\n")
        print("[AnyPath Gate0 Probe] record_invocation({0}) -> #{1} をログへ記録しました。"
              .format(script_identifier, _invocation_count))
    except Exception as e:
        print("[AnyPath Gate0 Probe] ログ記録に失敗しました: {0}".format(e))
