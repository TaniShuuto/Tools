"""
anypath.bridge.xgen_detector  -  Bridge層: XGen パス切れの検出（検出と警告のみ）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §5.4（XGen ―― 検出と警告のみ）

パス切れの完全自動修復は行わない。理由は3点（設計書より）:
    1. .xgen はノード属性ではなくテキストファイルであり、属性書き換え
       手法では救えない
    2. 誤修復時の被害（毛の消失等）がテクスチャより深刻であり、P1に
       照らして自動化に踏み込むべきでない
    3. 現行ツール群にXGen連携がなく、投資対効果が低い

責務は「XGenのパス切れを検出し、明確な警告と手動対処手順への案内を
出すこと」までとする。

§5.2の訂正（重要）: dirmapがXGenのパス解決に効く裏付けは確認できて
いない。XGenは.xgen内のテキストと${DESC}等の独自変数でパスを解決して
おり、Mayaのファイルパス解決層を経由しない可能性が高い。dirmapの役割は
リファレンスに限定する（本モジュールはdirmap経由の解決を一切試みない）。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §5.4 に基づき新規作成。
"""

__version__ = "1.0.0"

import os

import maya.cmds as cmds


class XGenPathIssue:
    """検出されたXGenパス切れ1件（警告表示専用。自動修復対象ではない）。"""
    __slots__ = ("palette_name", "description", "raw_path", "reason")

    def __init__(self, palette_name, description, raw_path, reason):
        self.palette_name = palette_name
        self.description = description
        self.raw_path = raw_path
        self.reason = reason

    def __repr__(self):
        return "XGenPathIssue(palette={0!r}, desc={1!r}, path={2!r})".format(
            self.palette_name, self.description, self.raw_path)


def _xgen_available():
    """XGenプラグイン/コマンドがこのMayaセッションで利用可能か判定する。"""
    try:
        return cmds.pluginInfo("xgenToolkit", query=True, loaded=True)
    except Exception:
        return False


def detect_xgen_path_issues():
    """
    シーン中のXGenパレットを走査し、参照先ファイルが見つからないものを
    検出する。**修復は一切行わない**（検出のみ）。

    XGenのパスは ${DESC}, ${PROJECT} 等の独自変数を含みうるため、そのまま
    os.path.isfile() で判定できないケースがある。これらの未展開変数を
    含むパスは「判定不能」として reason="unresolvable_variable" で
    報告し、誤って「壊れている」と警告しないようにする（P1: 誤った
    情報の提示も誤リンクに準じるリスクとして扱う）。

    戻り値: [XGenPathIssue, ...]（palette_name, descriptionの昇順で
            決定論的にソート）
    """
    issues = []

    if not _xgen_available():
        return issues

    try:
        import xgenm
    except Exception:
        return issues

    try:
        palettes = xgenm.palettes() or []
    except Exception:
        return issues

    for palette_name in sorted(palettes):
        try:
            descriptions = xgenm.descriptions(palette_name) or []
        except Exception:
            descriptions = []

        for description in sorted(descriptions):
            try:
                xgen_file = xgenm.xgFileName(palette_name)
            except Exception:
                xgen_file = None

            if not xgen_file:
                continue

            if "${" in xgen_file or "%" in xgen_file:
                issues.append(XGenPathIssue(
                    palette_name=palette_name, description=description,
                    raw_path=xgen_file, reason="unresolvable_variable",
                ))
                continue

            if not os.path.isfile(xgen_file):
                issues.append(XGenPathIssue(
                    palette_name=palette_name, description=description,
                    raw_path=xgen_file, reason="file_not_found",
                ))

    return issues


def build_xgen_warning_message(issues):
    """
    §6.8のエラーメッセージ規約（何が起きたか/なぜか/次に何をすればよいか）
    に沿った、XGen検出結果の警告文を組み立てる。UIへそのまま渡せる文字列。
    """
    if not issues:
        return None

    lines = [
        "何が起きたか: XGen のパレットに {0} 件、ファイルが見つからない".format(len(issues)),
        "設定がありました。",
        "",
        "なぜか: AnyPath は XGen のパスを自動修復しません"
        "（誤修復時の被害が大きいため、検出のみ行う仕様です）。",
        "",
        "次に何をすればよいか: XGen Editor を開き、該当パレットの"
        "ファイルパスを手動で確認・修正してください。",
    ]
    return "\n".join(lines)
