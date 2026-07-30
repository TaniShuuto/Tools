"""
anypath.bridge.reference_repath  -  Bridge層: リファレンスの復旧経路
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §5.3（リファレンスの復旧経路 ―― 2段構え）

経路A: 設定マッピングによる前方一致置換（起動時）
    設定の path_mappings を、そのまま dirmap のマッピングとして登録する。
    ドライブレター変更・プロジェクトルート移動といった典型ケースはこれで
    解決する。（実装は AnyPath.py の _apply_dirmap_from_config が既に担当）

経路B: オープン後の手動選び直し（常に提供）
    未解決のリファレンスを要確認パネルに列挙し、
    cmds.file(loadReference=..., fileName=...) による
    リファレンスノード単位の選び直しを提供する。

    「この経路Bの優先度を『高』とし、Phase 4の必須完了条件に含めます。
    .mbでは未解決リファレンスの状況把握が不透明になりやすく、また
    前方一致で救えない場合の唯一の手段であるためです。」

経路C（2026.07.31新設）: テクスチャと同じCore解決カスケードの適用
    ユーザー指摘（「プロジェクトファイル(.mb/.ma)も対象なのか」）に基づき、
    未解決リファレンスの raw_path も anypath.core.cascade.resolve_all()
    へ通し、テクスチャ等と全く同じS0〜S5カスケード・構造照合を適用する
    ようにした。EXACT/HIGHの場合はAnyPath.py側が自動的にreload_reference()
    を呼んで復旧する。REVIEW/FAILEDの場合は要確認パネルの「見つからない
    参照シーン」セクションに、テクスチャの行と同じ「これでOK／違う」の
    2択（またはFAILEDなら「参照...」のみ）で表示される。
    このモジュール自体はCoreを呼び出さない（Maya API依存のBridge層の
    責務は検出とreload_referenceの実行のみ）。Core呼び出しの配線は
    AnyPath.py の _run_resolution_pass() が担当する。

UI上の要件（§5.3）:
    「見つからない参照シーン」を独立したセクションとして常設すること。
    テクスチャと同じ行に混ぜてはならない。参照が1つ欠けるとノード構造
    ごと欠落するため、影響範囲の大きさが利用者に伝わらなくなる。
    （本モジュールは未解決リファレンスの検出・選び直しAPIまでを提供する。
    UI上の独立セクション表示は Shell層 review_panel.py の責務）

変更履歴:
    1.2.0 (2026.08.01):
        - ユーザー報告対応（「TEST移動後にTEST2を開いても全て正常だと
          出てしまう」）: list_unresolved_references() が、参照先が
          完全に見つからない/壊れているリファレンスノードに対して
          referenceQuery(isLoaded=True) や referenceQuery(filename=True)
          が例外を投げるケースを、静かに continue でスキップして
          いたのが原因の一つだった（軽度の未解決＝ファイルが単に
          見当たらないだけ、は元々拾えていたが、重度の未解決＝
          Mayaが参照ノードの情報自体をまともに返せないほど壊れている
          ケースを見逃していた）。filename取得失敗時は
          unresolvedName=True でのフォールバック取得を試み、それでも
          raw_pathが一切取れない場合もref_node名ベースのプレースホルダー
          パスで検出リストへ含めるようにした（Core解決には掛からず
          候補ゼロのFAILEDになるが、要確認パネルには必ず表示される）。
        - 後方互換性: PATCH相当。UnresolvedReferenceのフィールド構成は
          変更していない。「今まで検出されていなかった重度の異常」が
          新たに検出されるようになる点のみが挙動差。
    1.1.0 (2026.07.31):
        - ユーザー指摘対応（「そもそもプロジェクトファイルmbなども
          対象なのか」）: UnresolvedReference に candidates
          （Core解決の結果候補。既定は空リスト）フィールドを追加した。
          このモジュール自体はCore層を呼ばず、フィールドの入れ物のみを
          用意する（実際の値の設定はAnyPath.py側が行う）。
        - 後方互換性: MINOR。__slots__へのフィールド追加のみ。
          既存の呼び出し元（candidatesを渡さない）はそのまま動作する
          （既定で空リストが入る）。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §5.3 に基づき新規作成。
"""

__version__ = "1.2.0"

import maya.cmds as cmds


class UnresolvedReference:
    """
    未解決のリファレンス1件（要確認パネルの「見つからない参照シーン」対象）。

    candidates（2026.07.31新設）: Core解決カスケード（resolve_all）に
    raw_path を通した結果の ResolveResult.candidates をそのまま保持する
    （AnyPath.py側が設定する。このモジュール自体はCoreを呼ばないため、
    既定値は空リスト）。要確認パネルはcandidates[0]があれば
    「これでOK／違う」の2択を、無ければ「参照...」のみを表示する。
    """
    __slots__ = ("ref_node", "raw_path", "namespace", "top_level", "candidates")

    def __init__(self, ref_node, raw_path, namespace, top_level, candidates=None):
        self.ref_node = ref_node
        self.raw_path = raw_path
        self.namespace = namespace
        self.top_level = top_level
        self.candidates = candidates if candidates is not None else []

    def __repr__(self):
        return "UnresolvedReference(ref_node={0!r}, raw_path={1!r}, candidates={2})".format(
            self.ref_node, self.raw_path, len(self.candidates))


def list_unresolved_references():
    """
    シーン中の未解決リファレンス（参照先ファイルが実在しない、または
    ロードに失敗している状態）を列挙する。

    2026.08.01改修（ユーザー報告対応: 「TESTを移動後にTEST2を開くと、
    空のリファレンスグループのみが残り完全に読み込めていないのに、
    AnyPathでは全て正常だと出てしまう」）:
    旧実装は次の2箇所で「重度に壊れた参照ノード」を静かに見逃していた。
        1. cmds.referenceQuery(ref_node, isLoaded=True) が、参照先が
           完全に見つからない/壊れているノードに対して例外を投げる
           ことがあり、その場合 continue で検出対象から外れていた
           （「isLoaded」は本来「意図的なunload状態か」を表すフラグで
           あり、ロード失敗の代用指標としては不十分だった）。
        2. cmds.referenceQuery(ref_node, filename=True) も同様に、
           resolved名の解決に失敗すると例外を投げることがあり、
           raw_path = None のまま continue でスキップされていた。
    修正: isLoadedの例外自体を「異常な参照ノード=要確認対象」として
    扱うようにし、filenameの取得失敗時は unresolvedName=True
    （読み込み時に指定された元のパス。存在確認はできないが文字列は
    取れることが多い）でフォールバックを試みる。それでも raw_path が
    一切取れない場合も、ref_node名をプレースホルダーにして検出リストへ
    含める（P1: 誤リンクゼロの原則と同様に「見逃してはいけない異常」を
    静かに握りつぶさない。Core解決に回せない場合は候補ゼロのFAILEDに
    なるだけで、少なくとも要確認パネルには必ず出るようになる）。

    戻り値: [UnresolvedReference, ...]（ref_nodeの昇順ソート済み。
            決定論性のため他のCore結果と同様の並び順規約を踏襲する）
    """
    import os

    unresolved = []
    try:
        ref_nodes = cmds.ls(type="reference", long=True) or []
    except Exception:
        return unresolved

    for ref_node in sorted(ref_nodes):
        # sharedReferenceNode等、実ファイルを持たない内部リファレンスは除外
        # する。ただし「isLoadedクエリ自体が失敗する」ケースは、内部専用の
        # 参照ノードとの見分けがつかないため、まず filename の取得を
        # 試みてから最終判断する（filenameすら取れない場合のみ内部ノードと
        # みなしてスキップする＝従来の挙動を維持）。
        try:
            is_loaded = cmds.referenceQuery(ref_node, isLoaded=True)
            is_loaded_query_failed = False
        except Exception:
            is_loaded = False
            is_loaded_query_failed = True

        raw_path = None
        try:
            raw_path = cmds.referenceQuery(ref_node, filename=True, withoutCopyNumber=True)
        except Exception:
            raw_path = None

        # 文字列以外（Mayaの異常な戻り値、テスト用スタブの想定漏れ等）が
        # 返ってきた場合は「取れなかった」扱いに正規化する（後段の
        # .startswith() 呼び出しでの型エラーを避けるための防御）。
        if raw_path is not None and not isinstance(raw_path, str):
            raw_path = None

        if not raw_path:
            # resolved名が取れない場合、読み込み時に指定された元のパス
            # （unresolvedName）へフォールバックする。存在確認はできない
            # ことが多いが、Core解決（ファイル名インデックス照合）には
            # 十分使える文字列であることが多い。
            try:
                raw_path = cmds.referenceQuery(
                    ref_node, filename=True, unresolvedName=True,
                    withoutCopyNumber=True)
            except Exception:
                raw_path = None
            if raw_path is not None and not isinstance(raw_path, str):
                raw_path = None

        if not raw_path:
            if not is_loaded_query_failed:
                # isLoadedクエリ自体は成功しており、かつfilenameも
                # unresolvedNameも一切取れない場合のみ、実ファイルを
                # 持たない内部リファレンス（sharedReferenceNode等）と
                # みなしてスキップする（従来通りの安全側の挙動）。
                continue
            # isLoadedクエリ自体が失敗し、かつfilenameも取れない場合は、
            # 「壊れているが検出はしたいノード」としてプレースホルダーの
            # raw_pathを与え、検出リストに含める。Core解決には掛からず
            # 候補ゼロのFAILEDになるが、要確認パネルには必ず表示される
            # ようにする（見逃しをゼロにする方を優先）。
            raw_path = "<unknown>/{0}".format(
                ref_node.rsplit("|", 1)[-1].rsplit(":", 1)[-1])

        exists = os.path.isfile(raw_path) if not raw_path.startswith("<unknown>") else False

        if is_loaded and exists and not is_loaded_query_failed:
            continue  # 正常に解決されている

        try:
            namespace = cmds.referenceQuery(ref_node, namespace=True)
        except Exception:
            namespace = None
        try:
            top_level = cmds.referenceQuery(ref_node, isTopReference=True) if hasattr(
                cmds, "referenceQuery") else True
        except Exception:
            top_level = True

        unresolved.append(UnresolvedReference(
            ref_node=ref_node, raw_path=raw_path,
            namespace=namespace, top_level=top_level,
        ))

    return unresolved


def reload_reference(ref_node, new_file_path):
    """
    経路B: リファレンスノード単位の選び直し。
    cmds.file(loadReference=..., fileName=...) を用いて、指定のリファレンス
    ノードの参照先を new_file_path へ切り替えて再ロードする。

    P3（非破壊）: この操作自体はシーンファイルへの書き込みを伴わない
    （リファレンスパスの変更はシーン内のreference節点の状態変更であり、
    ディスク上のファイルには一切触れない。ただし利用者が明示的に
    シーンを保存すれば、他の修復と同様に永続化される点はP3の対象外
    ―― 利用者の意図的な保存操作を妨げるものではない）。

    戻り値: (ok: bool, error_message: str or None)
    """
    import os
    if not new_file_path or not os.path.isfile(new_file_path):
        return False, "指定されたファイルが見つかりません: {0}".format(new_file_path)

    try:
        cmds.file(new_file_path, loadReference=ref_node)
        return True, None
    except Exception as e:
        return False, str(e)
