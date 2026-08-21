# ============================================================
#  sp_to_aiStandardSurface.py
#  Substance Painterのテクスチャ -> aiStandardSurfaceへの自動接続
#  (旧表記 v2.1相当。2026.07.20にSemVerへ移行、現在のバージョンは
#   ファイル内の __version__ を参照。詳細はREADME.mdの
#   「バージョン履歴」節を参照)
#  プレフィックス自動検出 + チェックリストUI
#  + Displacement再センタリング(remapValue)修正
#
#  必要環境 : Maya 2022+ / Arnold 7+
#  使い方   : Mayaのスクリプトエディタ(Pythonタブ)に貼り付けて実行するか、
#             ~/Documents/maya/scripts/ に保存してシェルフから呼び出す
#
#  注記: 以前はシェルフボタンにこのファイルの中身を丸ごと貼り付けて
#  MEL/cmds経由で実行していたため、日本語などの非ASCII文字列が
#  「?」に化ける不具合があった。現在は本ファイルを単体のPythonスクリプト
#  としてimport/exec経由で呼び出す運用に切り替えたため、その制約は
#  解消されている(このコメント含めPythonソース自体はUTF-8として
#  正しく読み込まれる)。UIラベル・ダイアログ・ログは日本語で統一する。
#
#  ------------------------------------------------------------
#  旧v2.1 変更履歴 (displacement修正):
#    - 以前はheight/displacement用のfileノードのoutColorRを
#      displacementShader.displacementへ直接接続していた。標準的な
#      0-1のグレースケールハイトマップは0.5が「変位なし」の中間値と
#      なるため、生の値をそのまま渡すと (平均輝度 * scale) 分だけ
#      サーフェス全体が外側へ膨張して見える不具合があった。
#    - remapValueノードで0-1を-0.5..0.5へ再マップしてから
#      displacementShaderへ渡すことで、0.5が変位ゼロの基準値になる。
#      これはmaya_live_sync.pyのcreate_shader_network()で既に
#      採用している方式と同じで、両ツール間の一貫性を保っている。
#    - displacementShader.scaleは、属性が存在する場合はMayaの既定値
#      1.0(テスト用ジオメトリには強すぎることが多い)のままにせず、
#      控えめな初期値(0.1)に設定する。レンダービューで確認しながら
#      後から調整すること。
#    - 短いリマインダーを表示する: Arnoldのdisplacementは、メッシュ
#      シェイプ側のSubdivision(aiSubdivType = catclark/linear,
#      iterations)とBounds Padding(aiDispPadding)の設定も必要。
#      本スクリプトは意図的にメッシュシェイプには触れないため、
#      その設定は手動で行うこと。
# ============================================================

import os
import re
import maya.cmds as cmds

# 2026.07.20: バージョン表記をセマンティックバージョニング
# (MAJOR.MINOR.PATCH)へ移行。ツール群として初めて正式にバージョン番号を
# 割り当てる区切りとして 1.0.0 からスタートする(このコミット以前は
# コメント内ハードコードの独自表記 "v2.1" だった。旧番号との対応は
# README.mdの「バージョン履歴」節を参照)。以降は他3ファイルと同じ
# SemVer のルールに従う:
#   MAJOR: 設定ファイル形式の変更など、既存環境で互換性が崩れる変更
#   MINOR: 後方互換のある機能追加
#   PATCH: 後方互換のあるバグ修正
#
# 2026.07.24(緊急バグ修正):
#   (1) TEXTURE_PATTERNS の baseColor が辞書の先頭にあり、その
#       catch-all正規表現がemissive/specularより先に判定されるため、
#       "..._EmissiveColor.png"/"..._SpecularColor.png" のような
#       ファイルが誤ってbaseColorとして配線されていた。baseColorの
#       catch-allパターンを辞書の最後へ移動し、正規表現自体にも
#       specular/emissiveを除外する否定先読みを追加した。
#   (2) _connect_displacement() がremapValue/displacementShaderを
#       無条件で新規作成しており、同じマテリアルに対してassign_
#       textures_for_prefix()を2回実行するとノードが二重生成され、
#       古いものがシーンに孤児として残っていた。_get_or_create_
#       material()と同じ「決定的な名前で既存ノードを再利用する」
#       方式に揃えた。
# いずれも既存の動作を壊さない不具合修正のため、SemVerのルールに従い
# PATCHを上げる。
#
# 2026.07.30(機能追加・不具合対応): テクスチャフォルダ欄の自動入力が
# 「install.pyのシェルフボタン(AISS_COMMAND)を押した瞬間の1回きり」
# だったため、以下の症状が実機で報告されていた:
#   - SP側で「プロジェクト保存 -> Final書き出し完了」が終わる前に
#     ボタンを押すと、maya_live_sync.py側のactive_final_subfolderが
#     まだ更新されておらず、古いプロジェクトのフォルダが入る。
#   - ボタンを押した後にSP側でプロジェクトを切り替えても、ウィンドウを
#     閉じて開き直すまで追従しない。
# maya_live_sync.py側は「監視ON/OFFやreload処理のたびに共有設定を
# 読み直して追従する」設計であるのに対し、本ファイルだけが「外部
# (install.py)からの一度きりの注入」に依存しており非対称だった。
# 対応として、本ファイル自体に以下を追加した:
#   - show_ui() でウィンドウを開いた瞬間に maya_live_sync.load_config()
#     を読み、Finalフォルダ(final_export_dir + active_final_subfolder)
#     を自動入力する(_get_final_target_from_live_sync())。
#   - ウィンドウを開いている間、idle scriptJob(parent=WIN_MAINで
#     ウィンドウと連動して自動破棄)で定期的に共有設定を読み直し、
#     フォルダ欄が自動入力由来のままなら追従させる
#     (_idle_refresh_dir_field() / _refresh_dir_field_from_config())。
#     これにより「ウィンドウを開けっぱなしにしていれば、SP側の同期
#     スタートやプロジェクト保存に自動で追従する」を実現する
#     (maya_live_sync.py側の変更は不要で、共有設定ファイルを読むだけ)。
#   - ユーザーがフォルダ欄を手動編集(直接入力または「参照...」)したら
#     自動追従を止め、ステータスラベルで「手動入力中」と明示する
#     (_on_dir_field_manually_changed() / _mark_as_manual())。
#     「自動入力に戻す」ボタン(_reset_to_auto_path())で解除できる。
#   - 「フォルダをスキャン」実行時は、必ず一度 maya_live_sync の最新値で
#     フォルダ欄を確定させてから、その確定後の値でスキャンする
#     (読み込みとスキャンを1つの関数呼び出しの中で連続実行することで、
#     「表示上は新しいパスだが実際にスキャンしたのは古いパス」という
#     食い違いを防ぐ)。実際にスキャンしたパスは popup(inViewMessage)
#     とログの両方に表示する。スキャンで使ったフォルダは scanned_dir
#     として固定保持し、ウィンドウを開いたままの自動追従でフォルダ欄が
#     後から変わっても、「マテリアルを作成」ボタンはスキャン時と同じ
#     フォルダを対象にする(スキャン結果と作成対象の食い違いを防ぐ)。
# GUIの追加(ステータスラベル・ボタン)のみで、assign_textures_for_prefix()
# 等のマテリアル生成ロジック自体には手を入れていないため、SemVerの
# ルールに従いMINORを上げる。
__version__ = "1.1.0"

# -------------------------------------------------------
#  Displacement tuning defaults (旧v2.1)
# -------------------------------------------------------
#  Starting scale for the displacementShader. 1.0 (Maya default) is almost
#  always too strong for small test meshes; start low and raise in the
#  render view. Set to None to leave Maya's default untouched.
DISPLACEMENT_SCALE_DEFAULT = 0.1

# -------------------------------------------------------
#  Naming patterns per texture type
# -------------------------------------------------------
# 2026.07.24(緊急バグ修正): 従来はbaseColorが辞書の先頭にあり、その
# catch-all正規表現 r"color(?!.*normal)" が "emissivecolor"/
# "specularcolor" のような他チャンネルの名前にもマッチしてしまうため、
# _detect_texture_type()の走査順(Python 3.7+の辞書は挿入順で走査)で
# emissive/specularより先にbaseColorが判定され、誤ってbaseColorとして
# 配線されていた(本来のemissive/specular接続は行われないまま、
# baseColorの接続だけが上書きされる)。baseColorのcatch-allパターンを
# 辞書の最後(＝他の全ての具体的なチャンネル名を先に判定した後)に
# 移動し、あわせてcatch-all正規表現自体にもspecular/emissiveを除外する
# 否定先読みを追加した(辞書の順序変更だけに依存しない、二重の安全策)。
TEXTURE_PATTERNS = {
    "metalness": {
        "patterns": [r"metallic", r"metalness", r"metal(?!.*normal)"],
        "attr": "metalness",
        "colorSpace": "Raw",
        "nodeType": "file",
    },
    "roughness": {
        "patterns": [r"roughness", r"rough(?!.*normal)", r"glossiness", r"gloss"],
        "attr": "specularRoughness",
        "colorSpace": "Raw",
        "nodeType": "file",
    },
    "normal": {
        "patterns": [r"normal", r"nor(?!.*metal)", r"nrm"],
        "attr": "normalCamera",
        "colorSpace": "Raw",
        "nodeType": "aiNormalMap",
    },
    "height": {
        "patterns": [r"height", r"displacement", r"disp"],
        "attr": "__displacement__",
        "colorSpace": "Raw",
        "nodeType": "displacementShader",
    },
    "emissive": {
        "patterns": [r"emissive", r"emission", r"emissivecolor"],
        "attr": "emissionColor",
        "colorSpace": "sRGB",
        "nodeType": "file",
        "also_set": {"emission": 1.0},
    },
    "opacity": {
        "patterns": [r"opacity", r"alpha", r"transparency"],
        "attr": "opacity",
        "colorSpace": "Raw",
        "nodeType": "file",
    },
    "ao": {
        "patterns": [r"ambientocclusion", r"ao(?!.*normal)", r"occlusion"],
        "attr": "__ao__",
        "colorSpace": "Raw",
        "nodeType": "file",
    },
    "specular": {
        "patterns": [r"specularcolor", r"specular(?!.*roughness)(?!.*color)", r"spec(?!.*roughness)"],
        "attr": "specular",
        "colorSpace": "Raw",
        "nodeType": "file",
    },
    "baseColor": {
        "patterns": [r"basecolor", r"base_color", r"albedo", r"diffuse",
                     r"color(?!.*normal)(?!.*specular)(?!.*emissive)"],
        "attr": "baseColor",
        "colorSpace": "sRGB",
        "nodeType": "file",
    },
}

COLORSPACE_ALIASES = {
    "sRGB": ["sRGB", "Input - sRGB - Texture"],
    "Raw":  ["Raw", "Utility - Raw"],
}

SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".tga", ".exr", ".hdr"}

UNKNOWN_GROUP = "(unrecognized)"


# ============================================================
#  Prefix detection
# ============================================================

def _detect_texture_type(basename_lower):
    """
    Return (tex_type, info, matched_pattern) for a filename
    (lowercase, no extension), or (None, None, None) if no pattern matches.
    """
    for tex_type, info in TEXTURE_PATTERNS.items():
        for pattern in info["patterns"]:
            if re.search(pattern, basename_lower, re.IGNORECASE):
                return tex_type, dict(info), pattern
    return None, None, None


def _extract_prefix(filename, pattern):
    """
    Strip the texture-type suffix from a filename to get the material prefix,
    using the SAME pattern that _detect_texture_type matched. This keeps
    prefix extraction consistent with type detection so e.g. BaseColor and
    Normal maps for the same material end up with the same prefix.
    e.g. M_Glass_BaseColor.png -> "M_Glass"
         Wood_normal.png       -> "Wood"
    """
    base = os.path.splitext(os.path.basename(filename))[0]
    base_lower = base.lower()

    m = re.search(r"[_\-\.\s]?" + pattern + r"[_\-\.\s]?$", base_lower)
    if m:
        prefix = base[:m.start()].rstrip("_-. ")
        return prefix if prefix else base
    return base


def scan_prefixes(tex_dir):
    """
    Scan a directory and return { prefix: [filepath, ...], ... }.
    Files whose texture type can't be identified go into UNKNOWN_GROUP.
    """
    groups = {}
    for fname in os.listdir(tex_dir):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in SUPPORTED_EXT:
            continue
        full = os.path.join(tex_dir, fname)
        base_lower = os.path.splitext(fname)[0].lower()
        tex_type, _, pattern = _detect_texture_type(base_lower)
        if tex_type:
            prefix = _extract_prefix(fname, pattern)
        else:
            prefix = UNKNOWN_GROUP
        groups.setdefault(prefix, []).append(full)
    return groups


# ============================================================
#  Texture connection logic (internal)
# ============================================================

def _set_colorspace(node, space_key):
    """file ノードの colorSpace を明示的に設定する。

    Maya本体には、fileTextureName の設定/リロード時に
    colorManagementFileRules(拡張子ベースのルール)を再適用し、
    こちらが明示的に設定した colorSpace を勝手に上書きしてしまう
    既知の挙動がある(2017年から報告されている: Autodeskコミュニティ
    "Reload texture resetting color space attribute bug still in 2020")。
    このプロジェクトの環境では .png 拡張子に対するルールが登録されて
    おらず "Default" ルール(=sRGBにフォールバック)が適用されるため、
    Height/Metallic/Normal/Roughness のような Raw であるべきチャンネル
    まで sRGB に戻ってしまい、Arnoldでの見た目が破綻する不具合として
    実機で確認された。
    ignoreColorSpaceFileRules を先に True にしておくことで、この
    自動再適用そのものを無効化し、明示的に設定した colorSpace を
    確実に維持させる。
    """
    try:
        cmds.setAttr(f"{node}.ignoreColorSpaceFileRules", True)
    except Exception:
        pass  # 属性が無いMayaバージョンでも致命的ではないため握りつぶす

    for alias in COLORSPACE_ALIASES.get(space_key, [space_key]):
        try:
            cmds.setAttr(f"{node}.colorSpace", alias, type="string")
            return
        except Exception:
            pass


def _get_or_create_material(mat_name):
    if cmds.objExists(mat_name) and cmds.nodeType(mat_name) == "aiStandardSurface":
        return mat_name
    mat = cmds.shadingNode("aiStandardSurface", asShader=True, name=mat_name)
    sg  = cmds.sets(renderable=True, noSurfaceShader=True, empty=True,
                    name=f"{mat_name}SG")
    cmds.connectAttr(f"{mat}.outColor", f"{sg}.surfaceShader")
    return mat


def _connect_place2d(place_node, file_node):
    connections = [
        ("coverage","coverage"),("translateFrame","translateFrame"),
        ("rotateFrame","rotateFrame"),("mirrorU","mirrorU"),("mirrorV","mirrorV"),
        ("stagger","stagger"),("wrapU","wrapU"),("wrapV","wrapV"),
        ("repeatUV","repeatUV"),("offset","offset"),("rotateUV","rotateUV"),
        ("noiseUV","noiseUV"),("vertexUvOne","vertexUvOne"),
        ("vertexUvTwo","vertexUvTwo"),("vertexUvThree","vertexUvThree"),
        ("vertexCameraOne","vertexCameraOne"),("outUV","uv"),
        ("outUvFilterSize","uvFilterSize"),
    ]
    for src, dst in connections:
        cmds.connectAttr(f"{place_node}.{src}", f"{file_node}.{dst}", force=True)


def _create_file_node(tex_path, space_key, place_node):
    node = cmds.shadingNode("file", asTexture=True, isColorManaged=True)
    # 先に ignoreColorSpaceFileRules を立てて colorSpace を確定させてから
    # fileTextureName を設定する。逆順(先にファイルパスを設定)だと、
    # そのタイミングで colorManagementFileRules が一度 colorSpace を
    # 決定してしまい、後から Raw に設定してもリロード等のはずみで
    # sRGBへ再び戻ることがある(_set_colorspace のコメント参照)。
    _set_colorspace(node, space_key)
    cmds.setAttr(f"{node}.fileTextureName", tex_path, type="string")
    # fileTextureName 設定できっかけになる自動ルール適用に備え、直後に
    # もう一度確定させておく(念のための保険)。
    _set_colorspace(node, space_key)
    _connect_place2d(place_node, node)
    return node


def _connect_displacement(mat, fn):
    """
    v2.1: height/displacement用のfileノードを、0-1のグレースケール
    レンジを-0.5..0.5へ再センタリングするremapValueノードを経由して
    マテリアルのシェーディンググループへ接続する。これにより0.5
    (中間グレー)が変位ゼロの基準値になり、直接接続した場合に生じる
    一様な「膨張」オフセットが解消される。

    displacementShaderには控えめな初期scaleを設定する。メッシュ側の
    Subdivision / Bounds Paddingは引き続き手動設定が必要
    (本関数は意図的にメッシュシェイプには触れない)。

    生成したdisplacementShaderノードを返す。

    2026.07.24(緊急バグ修正・冪等性): 従来はremapValue/displacement
    Shaderを無条件で新規作成しており、同じマテリアルに対してassign_
    textures_for_prefix()(本関数の呼び出し元)を2回実行するとノードが
    二重生成され、古いものがシーンに孤児として残っていた。
    _get_or_create_material()と同じ「決定的な名前で既存ノードを
    再利用する」方式に揃え、マテリアル名から導出した名前で既存の
    remapValue/displacementShaderを再利用するようにした。
    """
    # 0-1 -> -0.5..0.5 への再センタリング。
    remap_name = f"{mat}_dispRemap"
    remap = (remap_name if cmds.objExists(remap_name)
             else cmds.shadingNode("remapValue", asUtility=True, name=remap_name))
    cmds.setAttr(f"{remap}.inputMin", 0.0)
    cmds.setAttr(f"{remap}.inputMax", 1.0)
    cmds.setAttr(f"{remap}.outputMin", -0.5)
    cmds.setAttr(f"{remap}.outputMax", 0.5)
    cmds.connectAttr(f"{fn}.outColorR", f"{remap}.inputValue", force=True)

    disp_name = f"{mat}_dispShader"
    disp = (disp_name if cmds.objExists(disp_name)
            else cmds.shadingNode("displacementShader", asShader=True, name=disp_name))
    cmds.connectAttr(f"{remap}.outValue", f"{disp}.displacement", force=True)

    # 控えめな初期scale(Mayaの既定値1.0は強すぎることが多い)。
    if (DISPLACEMENT_SCALE_DEFAULT is not None
            and cmds.attributeQuery("scale", node=disp, exists=True)):
        try:
            cmds.setAttr(f"{disp}.scale", DISPLACEMENT_SCALE_DEFAULT)
        except Exception:
            pass

    sg = cmds.listConnections(f"{mat}.outColor", d=True, type="shadingEngine")
    if sg:
        cmds.connectAttr(f"{disp}.displacement",
                          f"{sg[0]}.displacementShader", force=True)

    print("[SP-to-aiSS] displacementをremapValue経由で接続しました "
          "(0-1 -> -0.5..0.5)、scale="
          f"{DISPLACEMENT_SCALE_DEFAULT}。"
          "メッシュシェイプ側のSubdivision(aiSubdivType)と"
          "Bounds Padding(aiDispPadding)も忘れずに設定してください。")
    return disp


def _verify_and_fix_colorspaces(file_nodes):
    """
    生成済みの file ノード群に対し、期待される colorSpace になっているかを
    最後にもう一度検証し、ズレていれば直す「最終防衛ライン」。

    _create_file_node() 内で ignoreColorSpaceFileRules を立てた上で
    colorSpace を設定しているため通常は不要なはずだが、Maya本体には
    fileTextureName の設定/リロード時に colorManagementFileRules を
    再適用し明示的に設定した colorSpace を上書きしてしまう既知の挙動が
    ある(Autodeskコミュニティで2017年から報告)。万一それでも上書きが
    起きた場合に備え、ファイル名から再度チャンネル種別を判定し直し、
    実際の colorSpace が想定と異なっていれば強制的に直す。
    """
    fixed = []
    for node in file_nodes:
        try:
            path = cmds.getAttr(f"{node}.fileTextureName")
        except Exception:
            continue
        if not path:
            continue
        base_lower = os.path.splitext(os.path.basename(path))[0].lower()
        tex_type, info, _pattern = _detect_texture_type(base_lower)
        if tex_type is None:
            continue
        # 対象の node 自体は常に file ノード(呼び出し元でfile_like_nodesとして
        # 絞り込み済み)。info["nodeType"] は「このチャンネルが最終的に
        # 何のノードとしてシェーダーへ繋がるか」(aiNormalMap/
        # displacementShader等)を表すものであり、fileノードの種別とは
        # 別物のため、ここでは判定に使わない(以前は誤って除外条件に
        # 使っており、Normalチャンネルが検証対象から漏れていた)。
        expected = info["colorSpace"]
        try:
            current = cmds.getAttr(f"{node}.colorSpace")
        except Exception:
            continue
        aliases = COLORSPACE_ALIASES.get(expected, [expected])
        if current not in aliases:
            _set_colorspace(node, expected)
            fixed.append((node, current, expected))
    if fixed:
        print(f"[SP-to-aiSS] colorSpaceのズレを{len(fixed)}件検出し修正しました:")
        for node, before, after in fixed:
            print(f"    {node}: '{before}' -> '{after}'系")
    return fixed


def assign_textures_for_prefix(tex_dir, prefix, files, mat_name_override=None,
                                ao_multiply=True, assign_to_selected=False):
    """
    指定プレフィックスに対応するaiStandardSurfaceを作成(または既存を再利用)し、
    一致する全テクスチャを接続する。
    """
    mat_name = mat_name_override if mat_name_override else prefix
    mat   = _get_or_create_material(mat_name)
    place = cmds.shadingNode("place2dTexture", asUtility=True)

    assigned = {}
    ao_node  = None
    # 生成した file ノードを漏れなく記録する。assigned はチャンネルの
    # 最終接続ノード(aiNormalMap/displacementShader等、file自体ではない
    # 場合がある)を保持する辞書であり、そこから file ノードだけを後から
    # 辿ることはできないため、別途ここで直接収集する
    # (colorSpace最終検証で全チャンネルを漏れなく対象にするため)。
    created_file_nodes = []

    for fpath in files:
        fname      = os.path.basename(fpath)
        base_lower = os.path.splitext(fname)[0].lower()
        tex_type, info, _pattern = _detect_texture_type(base_lower)
        if tex_type is None:
            continue

        print(f"[SP-to-aiSS] [{prefix}] {tex_type:14s} <- {fname}")

        # --- Normal ---
        if info["nodeType"] == "aiNormalMap":
            fn = _create_file_node(fpath, "Raw", place)
            created_file_nodes.append(fn)
            nm = cmds.shadingNode("aiNormalMap", asUtility=True)
            cmds.connectAttr(f"{fn}.outColor",  f"{nm}.input",       force=True)
            cmds.connectAttr(f"{nm}.outValue",  f"{mat}.normalCamera", force=True)
            assigned[tex_type] = nm

        # --- Displacement ---
        # v2.1: 再センタリング + 控えめなscaleはヘルパー関数に移設済み。
        elif info["attr"] == "__displacement__":
            fn   = _create_file_node(fpath, "Raw", place)
            created_file_nodes.append(fn)
            disp = _connect_displacement(mat, fn)
            assigned[tex_type] = disp

        # --- AO ---
        elif info["attr"] == "__ao__":
            ao_node = _create_file_node(fpath, "Raw", place)
            created_file_nodes.append(ao_node)
            assigned[tex_type] = ao_node

        # --- 標準チャンネル ---
        else:
            fn = _create_file_node(fpath, info["colorSpace"], place)
            created_file_nodes.append(fn)
            base_lower2 = os.path.splitext(os.path.basename(fpath))[0].lower()
            is_gloss = bool(re.search(r"gloss", base_lower2))

            if is_gloss:
                rev = cmds.shadingNode("reverse", asUtility=True)
                cmds.connectAttr(f"{fn}.outColor", f"{rev}.input", force=True)
                cmds.connectAttr(f"{rev}.outputX", f"{mat}.{info['attr']}", force=True)
            else:
                try:
                    cmds.connectAttr(f"{fn}.outColor",  f"{mat}.{info['attr']}", force=True)
                except Exception:
                    try:
                        cmds.connectAttr(f"{fn}.outColorR", f"{mat}.{info['attr']}", force=True)
                    except Exception as e:
                        cmds.warning(f"[SP-to-aiSS] {info['attr']} の接続に失敗しました: {e}")

            for extra_attr, val in info.get("also_set", {}).items():
                try:
                    cmds.setAttr(f"{mat}.{extra_attr}", val)
                except Exception:
                    pass

            assigned[tex_type] = fn

    # --- AO乗算 ---
    if ao_multiply and ao_node and "baseColor" in assigned:
        mult = cmds.shadingNode("aiMultiply", asUtility=True)
        existing = cmds.listConnections(f"{mat}.baseColor", plugs=True, source=True)
        src = existing[0] if existing else f"{assigned['baseColor']}.outColor"
        cmds.connectAttr(src,                   f"{mult}.input1",    force=True)
        cmds.connectAttr(f"{ao_node}.outColor", f"{mult}.input2",    force=True)
        cmds.connectAttr(f"{mult}.outColor",    f"{mat}.baseColor",  force=True)
        print(f"[SP-to-aiSS] [{prefix}] AOをbaseColorに乗算しました。")

    # --- 選択中メッシュへの割り当て ---
    if assign_to_selected:
        sel = cmds.ls(selection=True, dag=True, shapes=True, type="mesh")
        if sel:
            sg = cmds.listConnections(f"{mat}.outColor", d=True, type="shadingEngine")
            if sg:
                cmds.sets(sel, edit=True, forceElement=sg[0])

    # --- 最終防衛ライン: colorSpaceのズレを再検証・修正 ---
    # created_file_nodes には生成した file ノードが Normal/Height/AO も
    # 含めて漏れなく記録されているため、これをそのまま渡す。
    _verify_and_fix_colorspaces(created_file_nodes)

    return mat


# ============================================================
#  GUI
# ============================================================

# Shared state across UI callbacks
_gui_state = {
    "dir_field":    None,
    "prefix_cbs":   {},   # { prefix: checkBox_control }
    "prefix_files": {},   # { prefix: [filepath, ...] }
    "ao_cb":        None,
    "sel_cb":       None,
    "scroll_col":   None,
    # 2026.07.30(不具合修正・自動追従): 以下、フォルダ欄の自動入力/追従
    # まわりの状態。詳細は _refresh_dir_field_from_config() のdocstring
    # を参照。
    "path_status_text":  None,  # 「このパスを自動追跡中/手動入力中」ラベル
    "user_edited_dir":   False, # Trueなら手動編集済み、自動更新はしない
    "idle_job":          None,  # scriptJobのID(ウィンドウと連動して破棄)
    "last_auto_path":    None,  # 直近に自動入力した値(手動編集検知の基準)
    "scanned_dir":       None,  # 直近のスキャンで実際に使ったフォルダ
}

WIN_MAIN = "spToAiSSWindow"


def show_ui():
    if cmds.window(WIN_MAIN, exists=True):
        cmds.deleteUI(WIN_MAIN)

    cmds.window(WIN_MAIN, title="SP -> aiStandardSurface  v{0}".format(__version__),
                widthHeight=(500, 460), sizeable=True)
    cmds.columnLayout("mainCol", adjustableColumn=True,
                      rowSpacing=6, columnOffset=["both", 10])
    cmds.separator(h=8, style="none")

    # ---- テクスチャフォルダ ----
    cmds.text(label="テクスチャフォルダ", align="left", font="boldLabelFont")
    cmds.rowLayout(numberOfColumns=2, columnWidth2=(370, 100),
                   adjustableColumn=1,
                   columnAttach=[(1,"both",0),(2,"both",4)])
    dir_field = cmds.textField(placeholderText="パスを入力するか「参照...」をクリック",
                               changeCommand=lambda _: _on_dir_field_manually_changed())
    cmds.button(label="参照...", command=lambda _: _browse_dir(dir_field))
    cmds.setParent("..")

    # 2026.07.30: 現在フォルダ欄が「自動追跡中」か「手動入力中」かを
    # 示す小さなステータス行 + 自動入力へ戻すボタン。
    cmds.rowLayout(numberOfColumns=2, columnWidth2=(370, 100),
                   adjustableColumn=1,
                   columnAttach=[(1,"both",0),(2,"both",4)])
    path_status_text = cmds.text(label="", align="left", font="smallObliqueLabelFont")
    cmds.button(label="自動入力に戻す",
                command=lambda _: _reset_to_auto_path())
    cmds.setParent("..")

    _gui_state["dir_field"]        = dir_field
    _gui_state["path_status_text"] = path_status_text

    cmds.button(label="フォルダをスキャンしてテクスチャセットを検出",
                height=32,
                command=lambda _: _scan_and_build_checklist())

    cmds.separator(h=4, style="in")

    # ---- プレフィックス一覧(スクロール) ----
    cmds.text(label="検出されたテクスチャセット（作成するものにチェック）",
              align="left", font="boldLabelFont")

    cmds.scrollLayout(height=140, childResizable=True)
    col = cmds.columnLayout(adjustableColumn=True, rowSpacing=2)
    cmds.text(label="-- まずフォルダをスキャンしてください --", align="left",
              font="smallObliqueLabelFont")
    cmds.setParent("..")          # col
    cmds.setParent("..")          # scroll

    _gui_state["scroll_col"] = col

    cmds.separator(h=4, style="in")

    # ---- オプション ----
    cmds.text(label="オプション", align="left", font="boldLabelFont")
    ao_cb  = cmds.checkBox(label="AOをBaseColorに乗算する",   value=True)
    sel_cb = cmds.checkBox(label="選択中のメッシュに割り当てる", value=False)

    _gui_state["ao_cb"]  = ao_cb
    _gui_state["sel_cb"] = sel_cb

    cmds.separator(h=8, style="in")

    # ---- 実行ボタン ----
    cmds.button(label="チェックしたセットのマテリアルを作成", height=40,
                command=lambda _: _run_all())

    cmds.separator(h=4, style="none")
    cmds.text(label="※ ログの詳細はScript Editorを確認してください", align="left",
              font="smallObliqueLabelFont")
    cmds.separator(h=8, style="none")

    cmds.showWindow(WIN_MAIN)

    # 2026.07.30(不具合修正・自動追従): ウィンドウを開いた瞬間に必ず
    # 最新のFinalフォルダを取得する(install.pyのAISS_COMMANDが「ボタンを
    # 押した瞬間の1回きり」でしか自動入力できなかった問題への対応。
    # 詳細は _refresh_dir_field_from_config() のdocstring参照)。
    _gui_state["user_edited_dir"] = False
    _refresh_dir_field_from_config(show_status=False)

    # ウィンドウを開いている間、maya_live_sync側の共有設定(SP側の同期
    # スタート・プロジェクト保存によるFinal書き出し完了など)をidle時に
    # 定期的に読み直し、フォルダ欄が自動入力由来のままなら追従させる。
    # parent=WIN_MAIN を指定することで、ウィンドウが閉じられた際に
    # Maya側がscriptJobを自動的に破棄してくれる(明示的なkillJobが
    # 漏れてもリーク・多重登録しない)。
    job_id = cmds.scriptJob(
        event=["idle", _idle_refresh_dir_field],
        parent=WIN_MAIN,
        protected=True,
    )
    _gui_state["idle_job"] = job_id


def _browse_dir(field):
    result = cmds.fileDialog2(fileMode=3, caption="テクスチャフォルダを選択")
    if result:
        cmds.textField(field, edit=True, text=result[0])
        # ユーザーが明示的に選んだ値なので手動編集扱いにする。
        _mark_as_manual(result[0])


# ------------------------------------------------------------------
#  maya_live_sync連携: フォルダ欄の自動入力・自動追従
# ------------------------------------------------------------------
# 2026.07.30(不具合修正): 従来、フォルダ欄の自動入力は本ファイルの外側
# (install.pyのシェルフボタンAISS_COMMAND)が「ボタンを押した瞬間に
# 一度だけ」担っていた。この方式には2つの制約があった:
#   1. SP側の「プロジェクト保存 -> Final書き出し完了」を待たないと
#      active_final_subfolder が最新化されないため、ボタンを押すタイミング
#      によっては古いプロジェクトのフォルダのままになる。
#   2. ボタンを押した後にSP側でプロジェクトを切り替えても、ウィンドウを
#      閉じて開き直すまで一切追従しない。
# 本体側(このファイル)にも自動入力・追従の仕組みを持たせ、
#   - ウィンドウを開いた瞬間に必ず最新値を取得
#   - 開いている間はidle scriptJobで定期的に追従
#   - ユーザーが手動でパスを編集したら追従を止める(誤って上書きしない)
#   - 「自動入力に戻す」ボタンで手動編集状態を解除できる
# ようにする。install.py側のAISS_COMMANDは変更不要
# (最初の自動入力がここでの処理と重複しても、同じ値を書き込むだけで
# 副作用は無い)。
def _get_final_target_from_live_sync():
    """maya_live_syncの共有設定から、現在アクティブなFinalフォルダの
    実パスを返す。取得できない場合はNoneを返す(例外は握りつぶす)。
    """
    try:
        import maya_live_sync as _mls
    except Exception:
        return None
    try:
        cfg = _mls.load_config()
        final_dir = cfg.get("final_export_dir") or ""
        subfolder = cfg.get("active_final_subfolder")
        if not final_dir:
            return None
        return os.path.join(final_dir, subfolder) if subfolder else final_dir
    except Exception:
        return None


def _mark_as_manual(current_text):
    """フォルダ欄がユーザーによって手動編集されたことを記録する。
    以後、自動追従(idle scriptJob)はこのフォルダ欄を上書きしない。
    """
    _gui_state["user_edited_dir"] = True
    _update_path_status_label(manual=True, path=current_text)


def _on_dir_field_manually_changed():
    """textFieldのchangeCommand。ユーザーがキーボードで直接編集した
    場合にも手動編集扱いにする(_browse_dir()経由の「参照...」ボタンと
    合わせて、フォルダ欄への変更経路を漏れなく捕捉する)。
    """
    dir_field = _gui_state.get("dir_field")
    if not dir_field or not cmds.textField(dir_field, exists=True):
        return
    current = cmds.textField(dir_field, query=True, text=True).strip()
    # 自動入力側が書き込んだ直後にもchangeCommandは発火するため、
    # 直近の自動入力値と同じであれば手動編集とはみなさない。
    if current == (_gui_state.get("last_auto_path") or ""):
        return
    _mark_as_manual(current)


def _reset_to_auto_path():
    """「自動入力に戻す」ボタン。手動編集フラグを解除し、即座に
    maya_live_syncの最新値でフォルダ欄を上書きする。
    """
    _gui_state["user_edited_dir"] = False
    _refresh_dir_field_from_config(show_status=True, force=True)


def _update_path_status_label(manual, path):
    label_ctrl = _gui_state.get("path_status_text")
    if not label_ctrl or not cmds.text(label_ctrl, exists=True):
        return
    if manual:
        text = "手動入力中(自動追従は停止しています)"
    elif path:
        text = "SP Live Syncの現在のFinalフォルダを自動追跡中"
    else:
        text = "自動取得できませんでした。パスを入力するか「参照...」を使用してください"
    cmds.text(label_ctrl, edit=True, label=text)


def _refresh_dir_field_from_config(show_status=False, force=False):
    """maya_live_syncの共有設定から最新のFinalフォルダを取得し、
    フォルダ欄がまだ自動入力由来(ユーザーが手動編集していない)であれば
    書き込む。

    show_status=Trueの場合、変化の有無をステータスラベルに反映する
    (「自動入力に戻す」ボタン押下時や、後述のスキャン実行時に使う)。
    force=Trueの場合、user_edited_dir を無視して強制的に上書きする
    (「自動入力に戻す」ボタン用)。

    戻り値: 反映後のフォルダ欄の文字列(取得できなければ現在の値)。
    """
    dir_field = _gui_state.get("dir_field")
    if not dir_field or not cmds.textField(dir_field, exists=True):
        return ""

    if _gui_state.get("user_edited_dir") and not force:
        # 手動編集中は上書きしない。ステータスラベルだけ最新化する。
        if show_status:
            current = cmds.textField(dir_field, query=True, text=True).strip()
            _update_path_status_label(manual=True, path=current)
        return cmds.textField(dir_field, query=True, text=True).strip()

    target = _get_final_target_from_live_sync()
    if target:
        cmds.textField(dir_field, edit=True, text=target)
        _gui_state["last_auto_path"] = target
        _update_path_status_label(manual=False, path=target)
    elif show_status:
        _update_path_status_label(manual=False, path=None)

    return cmds.textField(dir_field, query=True, text=True).strip()


def _idle_refresh_dir_field(*_args):
    """idle scriptJobから呼ばれる。例外はここで握りつぶし、Mayaの
    アイドル処理自体を止めないようにする。
    """
    try:
        _refresh_dir_field_from_config(show_status=False)
    except Exception:
        pass


def _scan_and_build_checklist():
    """フォルダをスキャンし、検出したプレフィックスのチェックリストを再構築する。

    2026.07.30(不具合修正): スキャン実行時は必ず一度、maya_live_syncの
    最新設定でフォルダ欄を確定させてから、その確定後の値でスキャンする
    (読み込みとスキャンの間に他の処理を挟まない一連の呼び出しにする
    ことで、「表示上は新しいパスなのに実際にスキャンしたのは古い
    パス」という食い違いを構造的に防ぐ)。ユーザーが手動編集済みの
    場合は _refresh_dir_field_from_config() 内で何もしないため、
    その場合は手動入力された値がそのまま使われる。
    """
    tex_dir = _refresh_dir_field_from_config(show_status=True)

    if not tex_dir or not os.path.isdir(tex_dir):
        cmds.confirmDialog(title="エラー",
                           message="有効なテクスチャフォルダを指定してください。",
                           button=["OK"])
        return

    # 2026.07.30: 実際にどのパスをスキャンしたかを、ポップアップと
    # ウィンドウ内ステータス行の両方で明示する。自動追従によって
    # ユーザーの想定と違うフォルダが使われていた、という事故を防ぐため。
    print("[SP-to-aiSS] 次のフォルダをスキャンします: {0}".format(tex_dir))
    cmds.inViewMessage(
        amg="<hl>[SP-to-aiSS]</hl> このパスでスキャンしています:<br>{0}".format(tex_dir),
        pos="topCenter", fade=True, fadeStayTime=2500)

    groups = scan_prefixes(tex_dir)
    _gui_state["prefix_files"] = groups
    _gui_state["prefix_cbs"]   = {}
    # 2026.07.30: このスキャンで実際に使ったフォルダを固定で覚えておく。
    # ウィンドウを開いたまま(idle scriptJobで)フォルダ欄が後から自動更新
    # されても、_run_all() は「スキャン結果と同じフォルダ」から作成する
    # ことを保証するため、フォルダ欄の現在値ではなくこちらを使う。
    _gui_state["scanned_dir"] = tex_dir

    col = _gui_state["scroll_col"]

    # remove existing children
    children = cmds.columnLayout(col, query=True, childArray=True) or []
    for c in children:
        try:
            cmds.deleteUI(c)
        except Exception:
            pass

    cmds.setParent(col)

    if not groups:
        cmds.text(label="テクスチャファイルが見つかりませんでした。",
                  align="left", font="smallObliqueLabelFont")
        return

    # 全選択 / 全解除 の行
    cmds.rowLayout(numberOfColumns=2, columnWidth2=[110, 110],
                   columnAttach=[(1,"both",2),(2,"both",2)])
    cmds.button(label="全選択",
                command=lambda _: _set_all_checks(True))
    cmds.button(label="全解除",
                command=lambda _: _set_all_checks(False))
    cmds.setParent("..")

    cmds.separator(h=4, style="none")

    # プレフィックスごとにチェックボックス + テクスチャ枚数ラベルを表示
    for prefix, files in sorted(groups.items()):
        cmds.rowLayout(numberOfColumns=2,
                       columnWidth2=[280, 200],
                       adjustableColumn=1,
                       columnAttach=[(1,"both",0),(2,"both",4)])
        cb = cmds.checkBox(label=prefix,
                           value=(prefix != UNKNOWN_GROUP))
        tex_list = ", ".join(
            _tex_type_label(f) for f in sorted(files)
        )
        cmds.text(label=f"[{len(files)}]  {tex_list}",
                  align="left", font="smallPlainLabelFont")
        cmds.setParent("..")
        _gui_state["prefix_cbs"][prefix] = cb

    print(f"[SP-to-aiSS] スキャン完了: {len(groups)} 件のセットを検出しました")


def _tex_type_label(fpath):
    """ファイルのテクスチャ種別を表す短いラベルを返す。"""
    base = os.path.splitext(os.path.basename(fpath))[0].lower()
    tex_type, _, _pattern = _detect_texture_type(base)
    short = {
        "baseColor": "Base", "metalness": "Metal", "roughness": "Rough",
        "normal": "Nrm", "height": "Disp", "emissive": "Emis",
        "opacity": "Opac", "ao": "AO", "specular": "Spec",
    }
    return short.get(tex_type, "?")


def _set_all_checks(value):
    for cb in _gui_state["prefix_cbs"].values():
        try:
            cmds.checkBox(cb, edit=True, value=value)
        except Exception:
            pass


def _run_all():
    ao_mult    = cmds.checkBox(_gui_state["ao_cb"],  query=True, value=True)
    assign_sel = cmds.checkBox(_gui_state["sel_cb"], query=True, value=True)
    # 2026.07.30: フォルダ欄の「今の」値ではなく、直近のスキャンで実際に
    # 使ったフォルダ(scanned_dir)を使う。ウィンドウを開いたままidle
    # scriptJobがフォルダ欄を後から自動更新しても、スキャン結果
    # (prefix_files)と作成対象フォルダが食い違わないようにするため。
    tex_dir = _gui_state.get("scanned_dir")
    if not tex_dir:
        tex_dir = cmds.textField(_gui_state["dir_field"], query=True, text=True).strip()

    targets = [
        prefix for prefix, cb in _gui_state["prefix_cbs"].items()
        if cmds.checkBox(cb, query=True, value=True)
        and prefix != UNKNOWN_GROUP
    ]

    if not targets:
        cmds.confirmDialog(title="お知らせ",
                           message="チェックされているテクスチャセットがありません。",
                           button=["OK"])
        return

    created = []
    for prefix in targets:
        files = _gui_state["prefix_files"].get(prefix, [])
        mat = assign_textures_for_prefix(
            tex_dir, prefix, files,
            ao_multiply=ao_mult,
            assign_to_selected=assign_sel,
        )
        created.append(mat)

    msg = f"{len(created)} 件のマテリアルを作成しました:\n" + "\n".join(created)
    cmds.confirmDialog(title="完了", message=msg, button=["OK"])
    print(f"\n[SP-to-aiSS] 完了: {created}")


# ============================================================
#  エントリーポイント
# ============================================================
# 2026.07.20(不具合修正): これまでモジュールレベルで show_ui() を
# 直接呼んでいたため、install.py が内部的に(バージョン確認や
# sys.modules への登録のために) import sp_to_aiStandardSurface を
# 行っただけで、ユーザーが意図しないタイミングでウィンドウが勝手に
# 開いてしまう不具合があった(maya_live_sync.py・udim_setup.py は
# import だけでは何も表示されない設計のため、aiSS だけが目立って
# 前面に出てしまい、install.py 実行直後に LiveSync ウィンドウが
# 表示され続けてほしいという要望とも相性が悪かった)。
#
# udim_setup.py の if __name__ == "__main__": launch_gui() と同じ
# 考え方に揃え、「単体のPythonスクリプトとして直接実行された場合のみ
# 自動でUIを開く」設計に変更する。シェルフボタン側(install.py の
# AISS_COMMAND)は元々 `import sp_to_aiStandardSurface as _sp` の後に
# 明示的に `_sp.show_ui()` を呼んでおり、この変更後もシェルフボタンの
# 動作は変わらない(むしろ、従来はimport時の自動呼び出しとこの明示
# 呼び出しが二重に実行されていたため、この修正でその重複も解消される)。
if __name__ == "__main__":
    show_ui()