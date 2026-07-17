"""
udim_setup.py  v1.0  ―  SP → Maya UDIM テクスチャ自動セットアップツール
==========================================================================
改善点 (v4):
  ・検知エンジン全面刷新
      - _ / . 区切りを自動判別（タイル番号前・チャンネル名前の両方）
      - 1001 タイル以外も代表タイルとして認識（最小タイル番号を自動選択）
      - 全タイル数をカウントしてモデルごとに表示
      - ファジーマッチング（アンダースコア・ハイフン除去後に照合）
      - 未対応チャンネルも警告付きで File ノードを作成（接続はスキップ）
  ・GUI 全面刷新
      - スキャン後にモデル一覧をチェックボックスで選択
      - 全選択 / 全解除ボタン
      - 既知チャンネル / ファジー / 未対応を色分け表示
      - タイル数・範囲を一覧表示
      - 選択されたモデルのみセットアップ実行

動作環境: Maya 2024 以降 / MtoA (Arnold for Maya) 導入済み

使い方 (Maya Script Editor):
    import sys, importlib
    sys.path.insert(0, r"C:/YourTools")
    import udim_setup
    importlib.reload(udim_setup)
    udim_setup.launch_gui()
"""

import os
import re
import glob
import subprocess
from collections import defaultdict

import maya.cmds as cmds

# ── PySide2 / PySide6 両対応 ────────────────────────────────────────────────
try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from PySide6.QtCore import Qt
    _PYSIDE = 6
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    from PySide2.QtCore import Qt
    _PYSIDE = 2

import maya.OpenMayaUI as omui
try:
    from shiboken6 import wrapInstance
except ImportError:
    try:
        from shiboken2 import wrapInstance
    except ImportError:
        wrapInstance = None

# ===========================================================================
# チャンネルマップ
# attr       : aiStandardSurface の接続先アトリビュート (None = 手動接続)
# colorSpace : Maya Color Space
# outChannel : outColor or outColorR
# useNormal  : True → aiNormalMap 経由で接続
# ===========================================================================
CHANNEL_VARIANTS = {
    # Base Color
    "BaseColor":        {"attr": "baseColor",         "colorSpace": "sRGB", "outChannel": "outColor"},
    "Base_Color":       {"attr": "baseColor",         "colorSpace": "sRGB", "outChannel": "outColor"},
    "Albedo":           {"attr": "baseColor",         "colorSpace": "sRGB", "outChannel": "outColor"},
    "Diffuse":          {"attr": "baseColor",         "colorSpace": "sRGB", "outChannel": "outColor"},
    # Roughness
    "Roughness":        {"attr": "specularRoughness", "colorSpace": "Raw",  "outChannel": "outColorR"},
    "Rough":            {"attr": "specularRoughness", "colorSpace": "Raw",  "outChannel": "outColorR"},
    # Metallic
    "Metallic":         {"attr": "metalness",         "colorSpace": "Raw",  "outChannel": "outColorR"},
    "Metalness":        {"attr": "metalness",         "colorSpace": "Raw",  "outChannel": "outColorR"},
    "Metal":            {"attr": "metalness",         "colorSpace": "Raw",  "outChannel": "outColorR"},
    # Normal
    "Normal":           {"attr": "normalCamera",      "colorSpace": "Raw",  "outChannel": "outColor", "useNormal": True},
    "Normal_OpenGL":    {"attr": "normalCamera",      "colorSpace": "Raw",  "outChannel": "outColor", "useNormal": True},
    "Normal_DirectX":   {"attr": "normalCamera",      "colorSpace": "Raw",  "outChannel": "outColor", "useNormal": True, "invertY": True},
    # Emissive
    "Emissive":         {"attr": "emissionColor",     "colorSpace": "sRGB", "outChannel": "outColor"},
    "Emission":         {"attr": "emissionColor",     "colorSpace": "sRGB", "outChannel": "outColor"},
    # AO
    "AO":               {"attr": None, "colorSpace": "Raw",  "outChannel": "outColorR",
                         "note": "LayeredTexture 等で BaseColor に手動合成してください"},
    "Ambient_Occlusion":{"attr": None, "colorSpace": "Raw",  "outChannel": "outColorR",
                         "note": "LayeredTexture 等で BaseColor に手動合成してください"},
    "Mixed_AO":         {"attr": None, "colorSpace": "Raw",  "outChannel": "outColorR",
                         "note": "LayeredTexture 等で BaseColor に手動合成してください"},
    # Height / Displacement
    "Height":           {"attr": None, "colorSpace": "Raw",  "outChannel": "outColorR",
                         "note": "Displacement Shader に手動接続してください"},
    "Displacement":     {"attr": None, "colorSpace": "Raw",  "outChannel": "outColorR",
                         "note": "Displacement Shader に手動接続してください"},
    # Opacity
    "Opacity":          {"attr": "opacity",           "colorSpace": "Raw",  "outChannel": "outColorR"},
    "Alpha":            {"attr": "opacity",           "colorSpace": "Raw",  "outChannel": "outColorR"},
    # Specular
    "Specular":         {"attr": "specular",          "colorSpace": "Raw",  "outChannel": "outColorR"},
    "SpecularColor":    {"attr": "specularColor",     "colorSpace": "sRGB", "outChannel": "outColor"},
    # Subsurface
    "Subsurface":       {"attr": "subsurface",        "colorSpace": "Raw",  "outChannel": "outColorR"},
    "SSS":              {"attr": "subsurface",        "colorSpace": "Raw",  "outChannel": "outColorR"},
    "SubsurfaceColor":  {"attr": "subsurfaceColor",   "colorSpace": "sRGB", "outChannel": "outColor"},
    # Transmission
    "Transmission":     {"attr": "transmission",      "colorSpace": "Raw",  "outChannel": "outColorR"},
    # Coat
    "Coat":             {"attr": "coat",              "colorSpace": "Raw",  "outChannel": "outColorR"},
    "CoatRoughness":    {"attr": "coatRoughness",     "colorSpace": "Raw",  "outChannel": "outColorR"},
    "Sheen":            {"attr": "sheen",             "colorSpace": "Raw",  "outChannel": "outColorR"},
}

TEXTURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".exr", ".hdr")

MAKETX_CANDIDATES = [
    r"C:\Program Files\Autodesk\Arnold\maya2024\bin\maketx.exe",
    r"C:\Program Files\Autodesk\Arnold\maya2025\bin\maketx.exe",
    r"C:\Program Files\Autodesk\Arnold\maya2026\bin\maketx.exe",
    r"C:\solidangle\mtoadeploy\2024\bin\maketx.exe",
    r"/opt/autodesk/arnold/bin/maketx",
]

# ===========================================================================
# 検知エンジン
# ===========================================================================

# チャンネルキーを長い順にソート（長いものを優先マッチ）
_CH_KEYS_SORTED = sorted(CHANNEL_VARIANTS.keys(), key=len, reverse=True)

def _normalize_ch(name: str) -> str:
    """チャンネル名を照合用に正規化する（記号除去 + 方言統一）"""
    n = re.sub(r"[_\-\s]", "", name.lower())
    n = n.replace("colour", "color")  # British English 対応
    n = n.replace("grey",   "gray")   # British English 対応
    return n


# ファジーマップ: 正規化後の文字列 → チャンネルキー（長いキーを優先登録）
_CH_FUZZY: dict = {}
for _k in _CH_KEYS_SORTED:   # 長い順なのでより具体的なキーが優先される
    _kn = _normalize_ch(_k)
    if _kn not in _CH_FUZZY:
        _CH_FUZZY[_kn] = _k


def _extract_tile(filename: str):
    """
    ファイル名から (stem, separator, tile_number, extension) を抽出する。

    対応形式:
      Name_Channel_1001.png   → ('Name_Channel', '_', 1001, '.png')
      Name_Channel.1001.png   → ('Name_Channel', '.', 1001, '.png')

    拡張子が対象外・UDIM タイルが見つからない場合は None を返す。
    """
    name_no_ext, real_ext = os.path.splitext(filename)
    if real_ext.lower() not in TEXTURE_EXTENSIONS:
        return None

    # 末尾の [_.]\d{4} を検索
    m = re.search(r"([_.])([\d]{4})$", name_no_ext)
    if not m:
        return None

    sep       = m.group(1)
    tile_num  = int(m.group(2))
    stem      = name_no_ext[: m.start()]
    return stem, sep, tile_num, real_ext.lower()


def _match_channel(raw_name: str):
    """
    チャンネル名を 3 段階で照合する。

    1. 完全一致（大文字小文字無視）
    2. 正規化一致（アンダースコア・ハイフン除去後）
    3. 前方一致 / 後方一致の部分マッチ

    Returns: (channel_key, status)
      status: "known" | "fuzzy" | None（未対応）
    """
    # 1. 完全一致
    for key in _CH_KEYS_SORTED:
        if key.lower() == raw_name.lower():
            return key, "known"

    # 2. 正規化一致
    normalized = _normalize_ch(raw_name)
    if normalized in _CH_FUZZY:
        return _CH_FUZZY[normalized], "fuzzy"

    # 3. 部分マッチ（長いキーを優先）
    for key in _CH_KEYS_SORTED:
        key_norm = _normalize_ch(key)
        if normalized == key_norm[:len(normalized)] or key_norm == normalized[:len(key_norm)]:
            return key, "fuzzy"

    return None, None


def _build_udim_path(fpath: str) -> str:
    """ファイルパスの UDIM タイル番号を <UDIM> トークンに置換する。
    Maya はパス区切りにスラッシュを要求するため、バックスラッシュを正規化する。
    Fix: Windows 環境での os.path.join バックスラッシュ問題を修正。
    """
    dirname  = os.path.dirname(fpath)
    basename = os.path.basename(fpath)
    name_no_ext, ext = os.path.splitext(basename)
    name_replaced = re.sub(r"([_.])([\d]{4})$", r"\1<UDIM>", name_no_ext)
    result = os.path.join(dirname, name_replaced + ext)
    return result.replace("\\", "/")   # Fix: バックスラッシュ → スラッシュ


def _detect_tile_gaps(tiles: list) -> list:
    """
    タイル欠損チェック
    ソート済みタイルリストから連番の抜けを検出して返す。
    例: [1001, 1002, 1004, 1005] → [1003]
    """
    if len(tiles) <= 1:
        return []
    full = set(range(min(tiles), max(tiles) + 1))
    return sorted(full - set(tiles))


def scan_textures(texture_dir: str, recursive: bool = False) -> dict:
    """
    ディレクトリを走査して UDIM テクスチャを収集する。

    Returns
    -------
    dict: {
        base_name: {
            "channels": {
                channel_key: {
                    "info"        : CHANNEL_VARIANTS エントリ or None,
                    "status"      : "known" | "fuzzy" | "unknown",
                    "original"    : ファイル名上のチャンネル名,
                    "representative": 代表タイルの絶対パス,
                    "udim_path"   : <UDIM> トークン付き絶対パス,
                    "tiles"       : ソート済みタイル番号リスト,
                    "sep"         : タイル区切り文字 ("_" or "."),
                }
            },
            "tile_min"   : int,
            "tile_max"   : int,
            "tile_count" : int,  # チャンネル中の最大タイル数
        }
    }
    """
    # ファイル収集
    if recursive:
        all_files = []
        for root, _, files in os.walk(texture_dir):
            for f in files:
                all_files.append(os.path.join(root, f))
    else:
        all_files = [
            os.path.join(texture_dir, f)
            for f in os.listdir(texture_dir)
            if os.path.isfile(os.path.join(texture_dir, f))
        ]

    # 一時バッファ: key=(base_name, channel_key) → {tiles, paths, meta}
    buf = defaultdict(lambda: {
        "tiles": [], "paths": {}, "sep": "_",
        "info": None, "status": "known",
        "original": "", "channel_key": "",
    })

    unknown_list = []

    for fpath in sorted(all_files):
        fname = os.path.basename(fpath)
        tile_info = _extract_tile(fname)
        if tile_info is None:
            continue

        stem, sep, tile_num, _ = tile_info

        # ── チャンネルを末尾から逆引き ────────────────────────────────
        matched_ch    = None
        matched_base  = None
        match_status  = None
        original_name = None

        # パス 1: 既知チャンネルキーを末尾から照合（_ / . どちらも試す）
        for ch_key in _CH_KEYS_SORTED:
            for ch_sep in ("_", "."):
                suffix = f"{ch_sep}{ch_key}"
                if stem.lower().endswith(suffix.lower()):
                    matched_ch   = ch_key
                    matched_base = stem[:-len(suffix)]
                    match_status = "known"
                    original_name = ch_key
                    break
            if matched_ch:
                break

        # パス 2: 末尾 _ / . で分割してファジーマッチ
        if matched_ch is None:
            for splitter in ("_", "."):
                if splitter in stem:
                    cand_base, cand_ch = stem.rsplit(splitter, 1)
                    ch_key, status = _match_channel(cand_ch)
                    if ch_key:
                        matched_ch   = ch_key
                        matched_base = cand_base
                        match_status = status
                        original_name = cand_ch
                        break

        # パス 3: 未対応チャンネルも警告付きで記録
        if matched_ch is None:
            for splitter in ("_", "."):
                if splitter in stem:
                    cand_base, cand_ch = stem.rsplit(splitter, 1)
                    matched_ch   = cand_ch  # そのまま使用
                    matched_base = cand_base
                    match_status = "unknown"
                    original_name = cand_ch
                    break

        if matched_ch is None or matched_base is None:
            continue

        key = (matched_base, matched_ch)
        g = buf[key]
        g["tiles"].append(tile_num)
        g["paths"][tile_num] = fpath
        g["sep"]          = sep
        g["channel_key"]  = matched_ch
        g["info"]         = CHANNEL_VARIANTS.get(matched_ch)
        g["status"]       = match_status
        g["original"]     = original_name

        if match_status == "unknown":
            if fname not in unknown_list:
                unknown_list.append(fname)

    # ── 最終出力に整形 ───────────────────────────────────────────────
    result = {}

    for (base_name, ch_key), g in buf.items():
        if base_name not in result:
            result[base_name] = {
                "channels": {},
                "tile_min": None,
                "tile_max": None,
                "tile_count": 0,
            }

        sorted_tiles = sorted(set(g["tiles"]))
        rep_tile     = sorted_tiles[0]
        rep_path     = g["paths"][rep_tile]
        udim_path    = _build_udim_path(rep_path)

        result[base_name]["channels"][ch_key] = {
            "info":           g["info"],
            "status":         g["status"],
            "original":       g["original"],
            "representative": rep_path,
            "udim_path":      udim_path,
            "tiles":          sorted_tiles,
            "sep":            g["sep"],
            "gaps":           _detect_tile_gaps(sorted_tiles),  # 欠損タイル
        }

        mn = result[base_name]["tile_min"]
        mx = result[base_name]["tile_max"]
        result[base_name]["tile_min"]   = min(sorted_tiles[0],  mn) if mn else sorted_tiles[0]
        result[base_name]["tile_max"]   = max(sorted_tiles[-1], mx) if mx else sorted_tiles[-1]
        result[base_name]["tile_count"] = max(result[base_name]["tile_count"], len(sorted_tiles))

    if unknown_list:
        print("[UDIM Scan] [WARN] 未対応チャンネル (File ノードのみ作成・手動接続が必要):")
        for f in unknown_list:
            print(f"            {f}")

    return result


# ===========================================================================
# Maya セットアップエンジン
# ===========================================================================

def _connect_place2d(file_node: str, p2d: str):
    """place2dTexture → File ノードの標準接続を一括で行う"""
    pairs = [
        ("coverage", "coverage"), ("translateFrame", "translateFrame"),
        ("rotateFrame", "rotateFrame"), ("mirrorU", "mirrorU"),
        ("mirrorV", "mirrorV"), ("stagger", "stagger"),
        ("wrapU", "wrapU"), ("wrapV", "wrapV"),
        ("repeatUV", "repeatUV"), ("offset", "offset"),
        ("rotateUV", "rotateUV"), ("noiseUV", "noiseUV"),
        ("vertexUvOne", "vertexUvOne"), ("vertexUvTwo", "vertexUvTwo"),
        ("vertexUvThree", "vertexUvThree"), ("vertexCameraOne", "vertexCameraOne"),
        ("outUV", "uv"), ("outUvFilterSize", "uvFilterSize"),
    ]
    for src_a, dst_a in pairs:
        src, dst = f"{p2d}.{src_a}", f"{file_node}.{dst_a}"
        if not cmds.isConnected(src, dst):
            try:
                cmds.connectAttr(src, dst, force=True)
            except Exception:
                pass


def setup_udim_material(
    scan_results: dict,
    selected_models: list = None,
    material_prefix: str = "M_",
    create_tx: bool = False,
    use_relative_path: bool = False,  # プロジェクト相対パス
    layout_hypershade: bool = False,  # Hypershade 自動レイアウト
) -> list:
    """
    スキャン結果から aiStandardSurface マテリアルを生成・接続する。

    Parameters
    ----------
    scan_results    : scan_textures() の返り値
    selected_models : 処理するモデル名リスト (None = 全モデル)
    material_prefix : マテリアル名プレフィックス (デフォルト "M_")
    create_tx       : True の場合 .tx 変換も実行
    """
    # ── Fix: Arnold プラグイン確認 ──────────────────────────────────────
    try:
        is_mtoa_loaded = cmds.pluginInfo("mtoa", query=True, loaded=True)
    except Exception:
        is_mtoa_loaded = False

    if not is_mtoa_loaded:
        try:
            cmds.loadPlugin("mtoa")
            print("[UDIM Setup] MtoA プラグインをロードしました")
        except Exception as e:
            print(f"[UDIM Setup] [NG] Arnold (MtoA) が見つかりません: {e}")
            print("[UDIM Setup]    Window > Settings/Preferences > Plug-in Manager"
                  " で MtoA を有効にしてください。")
            return []

    targets = selected_models if selected_models else list(scan_results.keys())
    created = []
    tx_dirs: set = set()  # Fix: TX 変換用ユニーク dir を収集（ループ外で実行）

    for base_name in targets:
        if base_name not in scan_results:
            print(f"[UDIM Setup] '{base_name}' がスキャン結果に存在しません (スキップ)")
            continue

        model_data = scan_results[base_name]

        # ── Fix: mat_name のサニタイズ（ドット・空白等の不正文字を除去）───
        mat_name_raw = f"{material_prefix}{base_name}"
        mat_name = re.sub(r"[^A-Za-z0-9_]", "_", mat_name_raw)
        if mat_name and mat_name[0].isdigit():
            mat_name = "_" + mat_name  # 先頭が数字の場合はアンダースコアを補う

        # マテリアル作成 / 既存取得
        if not cmds.objExists(mat_name):
            shader = cmds.shadingNode("aiStandardSurface", asShader=True, name=mat_name)
            sg_name = f"{mat_name}SG"
            sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=sg_name)
            cmds.connectAttr(f"{shader}.outColor", f"{sg}.surfaceShader", force=True)
            print(f"[UDIM Setup] マテリアル作成: {mat_name}")
        else:
            # Fix: 既存ノードが別の型でないかチェック
            node_type = cmds.nodeType(mat_name)
            if node_type != "aiStandardSurface":
                print(f"[UDIM Setup] [WARN] '{mat_name}' は aiStandardSurface ではありません"
                      f" (実際の型: {node_type})。スキップします。")
                continue
            shader = mat_name
            print(f"[UDIM Setup] 既存マテリアル使用: {mat_name}")

        # ── Fix: 同一 attr に接続しようとするチャンネルを事前検出 ──────────
        # 先に登録したチャンネルを優先し、重複を警告してスキップする
        attr_winner: dict = {}  # target_attr → 最初に登録した ch_key
        for ch_key, ch_data in model_data["channels"].items():
            info = ch_data.get("info")
            if not info:
                continue
            attr = info.get("attr")
            if not attr:
                continue
            if attr in attr_winner:
                print(
                    f"[UDIM Setup] [WARN] 接続競合: '{ch_key}' と '{attr_winner[attr]}' が"
                    f" 同じ属性 '{shader}.{attr}' に接続しようとしています。"
                    f" '{attr_winner[attr]}' を優先し '{ch_key}' をスキップします。"
                )
            else:
                attr_winner[attr] = ch_key

        for ch_key, ch_data in model_data["channels"].items():
            info      = ch_data["info"]
            status    = ch_data["status"]
            udim_path = ch_data["udim_path"]
            node_id   = re.sub(r"[^A-Za-z0-9_]", "_", f"{base_name}_{ch_key}")

            # ── Fix: 既存 File ノードの再利用（再実行でも増殖しない）────────
            file_node_name = f"file_{node_id}"
            if cmds.objExists(file_node_name):
                file_node = file_node_name
                print(f"[UDIM Setup] {ch_key}: 既存 File ノードを更新: {file_node}")
            else:
                file_node = cmds.shadingNode(
                    "file", asTexture=True, isColorManaged=True, name=file_node_name
                )
                p2d_name = f"p2d_{node_id}"
                p2d = (p2d_name if cmds.objExists(p2d_name)
                       else cmds.shadingNode("place2dTexture", asUtility=True, name=p2d_name))
                _connect_place2d(file_node, p2d)

            # UDIM 設定（相対パスオプションに応じてパスを切り替え）
            cmds.setAttr(f"{file_node}.uvTilingMode", 3)   # 3 = UDIM (Mari)
            path_to_use = (_to_project_relative(udim_path)
                           if use_relative_path else udim_path)
            cmds.setAttr(f"{file_node}.fileTextureName", path_to_use, type="string")

            # カラースペース
            cs = info.get("colorSpace", "Raw") if info else "Raw"
            cmds.setAttr(f"{file_node}.colorSpace", cs, type="string")

            # 未対応チャンネルは File ノードのみ作成
            if status == "unknown" or info is None:
                print(f"[UDIM Setup] [WARN] {ch_key}: 未対応チャンネル → '{file_node}' のみ作成")
                continue

            target_attr = info.get("attr")
            if target_attr is None:
                note = info.get("note", "手動接続が必要です")
                print(f"[UDIM Setup] {ch_key}: '{file_node}' 作成 (接続スキップ) ← {note}")
                continue

            # Fix: 競合で負けたチャンネルはスキップ
            if attr_winner.get(target_attr) != ch_key:
                continue

            out_ch = info.get("outChannel", "color")

            if info.get("useNormal"):
                nmap_name = f"aiNormalMap_{node_id}"
                # Fix: 既存 aiNormalMap の再利用
                nmap = (nmap_name if cmds.objExists(nmap_name)
                        else cmds.shadingNode("aiNormalMap", asUtility=True, name=nmap_name))
                if info.get("invertY"):
                    cmds.setAttr(f"{nmap}.invertY", 1)
                cmds.connectAttr(f"{file_node}.outColor", f"{nmap}.input", force=True)
                cmds.connectAttr(f"{nmap}.outValue", f"{shader}.{target_attr}", force=True)
                print(f"[UDIM Setup] {ch_key}: → aiNormalMap → {shader}.{target_attr}")
            else:
                cmds.connectAttr(f"{file_node}.{out_ch}", f"{shader}.{target_attr}", force=True)
                print(f"[UDIM Setup] {ch_key}: {file_node}.{out_ch} → {shader}.{target_attr}")

        created.append(mat_name)

        # Fix: TX dir を収集（ループ内では変換しない）
        if create_tx:
            for ch_data in model_data["channels"].values():
                tx_dirs.add(
                    os.path.dirname(ch_data["representative"]).replace("\\", "/")
                )

    # ── Fix: TX 変換をループ外で一括実行（同一 dir は 1 回のみ）─────────
    if create_tx:
        for tx_dir in sorted(tx_dirs):
            print(f"\n[TX Convert] ディレクトリ: {tx_dir}")
            batch_tx_convert(tx_dir)

    # ── Hypershade 自動レイアウト ──────────────────────────────────────
    if layout_hypershade and created:
        _layout_hypershade_nodes(created)

    print(f"\n[UDIM Setup] 完了。作成/更新: {created}")
    return created


def batch_tx_convert(texture_dir: str, skip_existing: bool = True) -> None:
    """指定ディレクトリの画像を Arnold .tx 形式に一括変換する"""
    import shutil
    maketx = next((p for p in MAKETX_CANDIDATES if os.path.isfile(p)), None) \
             or shutil.which("maketx")
    if not maketx:
        print("[TX Convert] maketx が見つかりません。MAKETX_CANDIDATES を確認してください。")
        return

    targets = []
    for ext in TEXTURE_EXTENSIONS:
        targets.extend(glob.glob(os.path.join(texture_dir, f"*{ext}")))

    print(f"[TX Convert] {len(targets)} ファイルを変換...")
    for src in targets:
        tx = os.path.splitext(src)[0] + ".tx"
        if skip_existing and os.path.isfile(tx):
            continue
        r = subprocess.run([maketx, "-v", "--oiio", src, "-o", tx],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[TX Convert] エラー: {r.stderr.strip()}")
        else:
            print(f"[TX Convert] 完了: {os.path.basename(tx)}")


# ===========================================================================
# 追加機能ユーティリティ
# ===========================================================================

def _to_project_relative(abs_path: str) -> str:
    """
    プロジェクト相対パス変換
    Maya ワークスペースルートからの相対パスを返す。
    プロジェクト外のパスはそのまま返す。
    """
    try:
        root = cmds.workspace(q=True, rootDirectory=True)
        if not root:
            return abs_path
        root_norm = root.replace("\\", "/").rstrip("/") + "/"
        path_norm = abs_path.replace("\\", "/")
        if path_norm.startswith(root_norm):
            return path_norm[len(root_norm):]
    except Exception:
        pass
    return abs_path


def _layout_hypershade_nodes(mat_names: list) -> None:
    """
    グラフ自動表示・レイアウト(Maya バージョン適応型)

    Maya 2025+ では hyperShadePanelMenuCommand 配下のコマンドと
    HypershadeLayoutGraph / layoutHyperShade が全廃されている。
    そのため Maya バージョンで分岐し、2025+ では
    cmds.nodeEditor(Maya 2013〜の安定 Python API)を使用する。
    """
    import maya.mel as mel

    try:
        maya_ver = int(cmds.about(majorVersion=True))
    except Exception:
        maya_ver = 2024

    # ── Maya 2024 以前: 旧 Hypershade MEL API ───────────────────────────
    if maya_ver < 2025:
        try:
            mel.eval("HypershadeWindow;")
            cmds.select(mat_names, noExpand=True)
            mel.eval(
                'hyperShadePanelMenuCommand("hyperShadePanel1",'
                ' "graphMaterialsOnSelectedObjects");'
            )
            mel.eval("HypershadeLayoutGraph;")
            print("[UDIM Setup] Hypershade レイアウト完了")
            return
        except Exception as e:
            print(f"[UDIM Setup] 旧 Hypershade API 失敗: {e}")
            # 失敗した場合は Node Editor にフォールバック

    # ── Maya 2025+: Node Editor(cmds.nodeEditor) ─────────────────────
    # hyperShadePanelMenuCommand / HypershadeLayoutGraph が全廃されているため
    # cmds.nodeEditor を使用する(Maya 2013+ で利用可能・安定 API)
    try:
        mel.eval("NodeEditorWindow;")
        ned_panels = cmds.getPanel(type="nodeEditorPanel") or []

        if not ned_panels:
            raise RuntimeError("nodeEditorPanel が見つかりません")

        ned = ned_panels[0]

        # ── グラフをクリア ──────────────────────────────────────────────
        for clr_flag in ("clearGraph", "removeAllNodes"):
            try:
                cmds.nodeEditor(ned, e=True, **{clr_flag: True})
                break
            except Exception:
                pass

        # ── ノードを収集 ────────────────────────────────────────────────
        # マテリアル + 上流ノード(File / place2dTexture / aiNormalMap 等)
        # + 下流ノード(ShadingGroup)
        all_nodes = set()
        for mat in mat_names:
            all_nodes.add(mat)
            all_nodes.update(cmds.listHistory(mat) or [])
            all_nodes.update(
                cmds.listConnections(
                    f"{mat}.outColor", type="shadingEngine"
                ) or []
            )

        added = 0
        for node in all_nodes:
            try:
                cmds.nodeEditor(ned, e=True, addNode=node)
                added += 1
            except Exception:
                pass

        # ── レイアウト → フレームオール ─────────────────────────────────
        for kw in ({"layout": True}, {"frameAll": True}):
            try:
                cmds.nodeEditor(ned, e=True, **kw)
            except Exception:
                pass

        print(f"[UDIM Setup] Node Editor レイアウト完了({added} ノード)")

    except Exception as e:
        print(f"[UDIM Setup] Node Editor 失敗: {e}")
        print("[UDIM Setup]    手動でグラフを更新してください。")


def _score_mesh(base_name: str, mesh_name: str) -> int:
    """
    ベース名とメッシュ名の類似スコアを返す（0–100）。
    Maya のデフォルト接頭語（p/SM_/geo_）を除去してから照合する。
    短い方のトークンセットを基準にするため、詳細なベース名でも短いメッシュ名にヒットする。
    """
    def _strip_prefix(name: str) -> str:
        """pBox→Box, SM_Box→Box, geo_box→box などの共通接頭語を除去する"""
        name = re.sub(r"^[pn](?=[A-Z])", "", name)          # pBox→Box, nSphere→Sphere
        name = re.sub(r"^(?:SM|geo|mesh|prop)_", "",        # SM_Box→Box
                      name, flags=re.IGNORECASE)
        return name

    bn_clean = re.sub(r"[^a-z0-9]", "", _strip_prefix(base_name).lower())
    mn_clean = re.sub(r"[^a-z0-9]", "", _strip_prefix(mesh_name).lower())

    if not bn_clean or not mn_clean:
        return 0
    if bn_clean == mn_clean:
        return 100
    if bn_clean in mn_clean:
        return int(70 + 30 * len(bn_clean) / len(mn_clean))
    if mn_clean in bn_clean:
        return int(70 + 30 * len(mn_clean) / len(bn_clean))

    bn_tok = {t for t in re.split(r"[_\-\s]", _strip_prefix(base_name).lower())
              if len(t) > 1}
    mn_tok = {t for t in re.split(r"[_\-\s]", _strip_prefix(mesh_name).lower())
              if len(t) > 1}
    common = bn_tok & mn_tok
    if not common:
        return 0
    # 短い方のセットを基準に（ベース名が詳細でも短いメッシュ名にヒットしやすくする）
    return int(len(common) / min(len(bn_tok), len(mn_tok)) * 70)


def find_mesh_candidates(base_name: str, threshold: int = 30) -> list:
    """
    シーン内メッシュから候補を返す（score >= threshold）。
    Returns: [{"transform": str, "short_name": str, "score": int}, ...]
    """
    all_meshes = cmds.ls(type="mesh", long=True) or []
    seen: set = set()
    candidates = []
    for mesh in all_meshes:
        parents = cmds.listRelatives(mesh, parent=True, fullPath=True) or []
        for parent in parents:
            if parent in seen:
                continue
            seen.add(parent)
            short = parent.split("|")[-1]
            score = _score_mesh(base_name, short)
            if score >= threshold:
                candidates.append({"transform": parent, "short_name": short, "score": score})
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def assign_material_to_meshes(mat_name: str, transforms: list) -> int:
    """
    マテリアルをメッシュリストに割り当てる。割り当て件数を返す。
    """
    # ShadingGroup を探す
    sg: str | None = None
    sg_candidate = f"{mat_name}SG"
    if cmds.objExists(sg_candidate):
        sg = sg_candidate
    else:
        conns = cmds.listConnections(f"{mat_name}.outColor", type="shadingEngine") or []
        if conns:
            sg = conns[0]
    if not sg:
        print(f"[Assign] [NG] ShadingGroup が見つかりません: {mat_name}")
        return 0
    assigned = 0
    for t in transforms:
        try:
            cmds.sets(t, e=True, forceElement=sg)
            print(f"[Assign] [OK] {mat_name} → {t.split('|')[-1]}")
            assigned += 1
        except Exception as e:
            print(f"[Assign] [NG] {t.split('|')[-1]}: {e}")
    return assigned


# ===========================================================================
# GUI
# ===========================================================================

_STATUS_COLOR = {
    "known":   None,                         # デフォルト色
    "fuzzy":   QtGui.QColor(200, 140, 40),   # 橙
    "unknown": QtGui.QColor(200, 60,  60),   # 赤
}
_STATUS_LABEL = {
    "known":   "[OK] ",
    "fuzzy":   "[WARN] ファジー",
    "unknown": "[NG] 未対応",
}


class ModelTreeWidget(QtWidgets.QWidget):
    """スキャン結果をチェックボックスツリーで表示するウィジェット"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # ─ ヘッダー行 ──────────────────────────────────────────────
        hdr = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("検知されたモデル")
        lbl.setStyleSheet("font-weight: bold; color: #c0c0c0;")
        hdr.addWidget(lbl)
        hdr.addStretch()

        self._btn_all  = QtWidgets.QPushButton("全選択")
        self._btn_none = QtWidgets.QPushButton("全解除")
        for b in (self._btn_all, self._btn_none):
            b.setFixedWidth(64)
            b.setStyleSheet("padding: 2px 6px;")
        self._btn_all.clicked.connect(self._select_all)
        self._btn_none.clicked.connect(self._deselect_all)
        hdr.addWidget(self._btn_all)
        hdr.addWidget(self._btn_none)
        lay.addLayout(hdr)

        # ─ ツリー ──────────────────────────────────────────────────
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["モデル / チャンネル", "タイル", "範囲", "状態"])
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 55)
        self.tree.setColumnWidth(2, 90)
        self.tree.setColumnWidth(3, 90)
        self.tree.setAlternatingRowColors(True)
        self.tree.setStyleSheet(
            "QTreeWidget { background: #1c1c1c; color: #d0d0d0; "
            "               font-family: Consolas, monospace; font-size: 11px; }"
            "QTreeWidget::item:selected { background: #2a6496; }"
            "QTreeWidget::item:alternate { background: #222222; }"
        )
        self.tree.setMinimumHeight(200)
        lay.addWidget(self.tree)

        # 凡例
        legend = QtWidgets.QLabel(
            "  [OK] 既知チャンネル  "
            "<span style='color:#c88c28;'>[WARN] ファジーマッチ</span>  "
            "<span style='color:#c83c3c;'>[NG] 未対応（手動接続）</span>"
        )
        legend.setTextFormat(Qt.RichText)
        legend.setStyleSheet("font-size: 10px; color: #888;")
        lay.addWidget(legend)

    def populate(self, scan_results: dict):
        """スキャン結果をツリーに反映する"""
        self.tree.clear()
        for base_name, data in sorted(scan_results.items()):
            tile_range = (f"{data['tile_min']}–{data['tile_max']}"
                          if data['tile_min'] != data['tile_max']
                          else str(data['tile_min']))

            # ── モデル行（チェックボックスあり） ────────────────────
            model_item = QtWidgets.QTreeWidgetItem()
            model_item.setText(0, base_name)
            model_item.setText(1, str(data["tile_count"]))
            model_item.setText(2, tile_range)
            model_item.setText(3, f"{len(data['channels'])} ch")
            model_item.setCheckState(0, Qt.Checked)
            model_item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
            )
            f = model_item.font(0)
            f.setBold(True)
            model_item.setFont(0, f)
            self.tree.addTopLevelItem(model_item)

            # ── チャンネル行（情報のみ） ─────────────────────────────
            for ch_key, ch in sorted(data["channels"].items()):
                ch_item = QtWidgets.QTreeWidgetItem()
                orig = ch["original"]
                label = f"  {ch_key}" if orig == ch_key else f"  {ch_key}  ({orig})"
                ch_item.setText(0, label)
                ch_item.setText(1, str(len(ch["tiles"])))
                ch_item.setText(2, f"{ch['tiles'][0]}–{ch['tiles'][-1]}"
                                   if len(ch["tiles"]) > 1 else str(ch["tiles"][0]))

                # 欠損タイル表示
                gaps = ch.get("gaps", [])
                if gaps:
                    gap_str = ", ".join(str(g) for g in gaps[:4])
                    if len(gaps) > 4:
                        gap_str += f"... (+{len(gaps)-4})"
                    status_text = f"[WARN] 欠損: {gap_str}"
                    ch_item.setForeground(3, QtGui.QBrush(QtGui.QColor(200, 80, 80)))
                else:
                    status_text = _STATUS_LABEL.get(ch["status"], ch["status"])

                ch_item.setText(3, status_text)
                ch_item.setFlags(Qt.ItemIsEnabled)   # チェックボックスなし

                color = _STATUS_COLOR.get(ch["status"])
                if color and not gaps:
                    ch_item.setForeground(0, QtGui.QBrush(color))
                    ch_item.setForeground(3, QtGui.QBrush(color))

                model_item.addChild(ch_item)

            model_item.setExpanded(True)

        self.tree.resizeColumnToContents(3)

    def get_selected(self) -> list:
        """チェックが入っているモデル名を返す"""
        return [
            self.tree.topLevelItem(i).text(0)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).checkState(0) == Qt.Checked
        ]

    def _select_all(self):
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, Qt.Checked)

    def _deselect_all(self):
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, Qt.Unchecked)


class MeshAssignDialog(QtWidgets.QDialog):
    """
    メッシュ自動割り当てダイアログ（別ウィンドウ）
    スキャン結果のベース名を使ってシーン内メッシュを検索し、
    チェックボックスで選択したメッシュにマテリアルを割り当てる。
    """

    TITLE = "メッシュ自動割り当て  v1.0"

    def __init__(self, scan_results: dict = None, parent=None):
        if wrapInstance and parent is None:
            ptr    = omui.MQtUtil.mainWindow()
            parent = wrapInstance(int(ptr), QtWidgets.QWidget)
        super().__init__(parent)
        self.setWindowTitle(self.TITLE)
        self.setMinimumWidth(540)
        self.setMinimumHeight(520)
        # メインウィンドウをブロックしない（モードレス）
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._scan_results = scan_results or {}
        self._candidates: list = []
        self._build_ui()
        self._refresh_materials()

    # ── UI 構築 ─────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        title = QtWidgets.QLabel("メッシュ自動割り当て")
        title.setStyleSheet("font-size: 13px; font-weight: bold; color: #e0e0e0;")
        root.addWidget(title)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        sep.setStyleSheet("color: #444;")
        root.addWidget(sep)

        # ─ マテリアル選択 ─────────────────────────────────────────────
        mat_grp = QtWidgets.QGroupBox("マテリアル")
        mat_lay = QtWidgets.QFormLayout(mat_grp)

        mat_row = QtWidgets.QHBoxLayout()
        self._mat_combo = QtWidgets.QComboBox()
        self._mat_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        refresh_btn = QtWidgets.QPushButton("↺")
        refresh_btn.setFixedWidth(28)
        refresh_btn.setToolTip("シーン内マテリアルを再読み込み")
        refresh_btn.clicked.connect(self._refresh_materials)
        mat_row.addWidget(self._mat_combo)
        mat_row.addWidget(refresh_btn)
        mat_lay.addRow("割り当てるマテリアル:", mat_row)

        # 検索キー（デフォルトはベース名の先頭トークン / 編集可能）
        search_row = QtWidgets.QHBoxLayout()
        self._search_edit = QtWidgets.QLineEdit()
        self._search_edit.setPlaceholderText("マテリアル選択時に自動入力されます")
        self._mat_combo.currentIndexChanged.connect(self._auto_fill_search)
        search_row.addWidget(self._search_edit)
        mat_lay.addRow("検索キー:", search_row)
        root.addWidget(mat_grp)

        # ─ スキャン設定 ──────────────────────────────────────────────
        opt_grp = QtWidgets.QGroupBox("スキャン設定")
        opt_lay = QtWidgets.QFormLayout(opt_grp)

        thresh_row = QtWidgets.QHBoxLayout()
        self._thresh_slider = QtWidgets.QSlider(Qt.Horizontal)
        self._thresh_slider.setRange(0, 100)
        self._thresh_slider.setValue(30)
        self._thresh_label = QtWidgets.QLabel("30%")
        self._thresh_label.setFixedWidth(36)
        self._thresh_slider.valueChanged.connect(
            lambda v: self._thresh_label.setText(f"{v}%")
        )
        thresh_row.addWidget(self._thresh_slider)
        thresh_row.addWidget(self._thresh_label)
        opt_lay.addRow("一致閾値:", thresh_row)

        note = QtWidgets.QLabel("70% 以上は自動チェック / 69% 以下は手動チェックが必要")
        note.setStyleSheet("font-size: 10px; color: #888;")
        opt_lay.addRow("", note)
        root.addWidget(opt_grp)

        # ─ スキャンボタン ────────────────────────────────────────────
        scan_btn = QtWidgets.QPushButton(" シーンをスキャン")
        scan_btn.setStyleSheet("padding: 5px; font-weight: bold;")
        scan_btn.clicked.connect(self._scan_scene)
        root.addWidget(scan_btn)

        # ─ 候補リスト ────────────────────────────────────────────────
        list_grp = QtWidgets.QGroupBox("候補メッシュ")
        list_lay = QtWidgets.QVBoxLayout(list_grp)

        list_hdr = QtWidgets.QHBoxLayout()
        self._result_label = QtWidgets.QLabel("—")
        self._result_label.setStyleSheet("color:#888; font-size:10px;")
        list_hdr.addWidget(self._result_label)
        list_hdr.addStretch()
        all_btn  = QtWidgets.QPushButton("全選択")
        none_btn = QtWidgets.QPushButton("全解除")
        for b in (all_btn, none_btn):
            b.setFixedWidth(64)
            b.setStyleSheet("padding: 2px 6px;")
        all_btn.clicked.connect(self._select_all)
        none_btn.clicked.connect(self._deselect_all)
        list_hdr.addWidget(all_btn)
        list_hdr.addWidget(none_btn)
        list_lay.addLayout(list_hdr)

        self._mesh_tree = QtWidgets.QTreeWidget()
        self._mesh_tree.setHeaderLabels(["メッシュ名", "フルパス", "スコア"])
        self._mesh_tree.setColumnWidth(0, 180)
        self._mesh_tree.setColumnWidth(1, 230)
        self._mesh_tree.setColumnWidth(2, 60)
        self._mesh_tree.setAlternatingRowColors(True)
        self._mesh_tree.setMinimumHeight(200)
        self._mesh_tree.setStyleSheet(
            "QTreeWidget { background: #1c1c1c; color: #d0d0d0;"
            "  font-family: Consolas, monospace; font-size: 11px; }"
            "QTreeWidget::item:selected { background: #2a6496; }"
            "QTreeWidget::item:alternate { background: #222222; }"
        )
        list_lay.addWidget(self._mesh_tree)

        # スコア凡例
        legend = QtWidgets.QLabel(
            "<span style='color:#3cc85a;'>■ 90–100%</span>  "
            "<span style='color:#c8c83c;'>■ 70–89%</span>  "
            "<span style='color:#c88c28;'>■ 50–69%</span>  "
            "<span style='color:#c83c3c;'>■ &lt;50%</span>"
        )
        legend.setTextFormat(Qt.RichText)
        legend.setStyleSheet("font-size: 10px;")
        list_lay.addWidget(legend)
        root.addWidget(list_grp)

        # ─ ログ ──────────────────────────────────────────────────────
        self._log = QtWidgets.QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(72)
        self._log.setStyleSheet(
            "background:#1a1a1a; color:#b8b8b8;"
            "font-family:Consolas,monospace; font-size:11px;"
        )
        root.addWidget(self._log)

        # ─ 割り当てボタン ────────────────────────────────────────────
        assign_btn = QtWidgets.QPushButton("▶  チェックしたメッシュに割り当て")
        assign_btn.setStyleSheet(
            "background:#1a5276; color:white; font-weight:bold; padding:6px;"
        )
        assign_btn.clicked.connect(self._assign)
        root.addWidget(assign_btn)

    # ── スロット ─────────────────────────────────────────────────────────
    def _refresh_materials(self):
        """シーン内の aiStandardSurface をコンボに反映する"""
        self._mat_combo.clear()

        # スキャン結果から推定されるマテリアル名（base_name 付き）
        scan_mat_map: dict = {}
        for base_name in self._scan_results:
            mat = re.sub(r"[^A-Za-z0-9_]", "_", f"M_{base_name}")
            if mat and mat[0].isdigit():
                mat = "_" + mat
            scan_mat_map[mat] = base_name

        # シーン内の全 aiStandardSurface
        scene_mats = cmds.ls(type="aiStandardSurface") or []

        added: set = set()
        # スキャン結果マテリアルを先頭に（ベース名情報付き）
        for mat, base_name in scan_mat_map.items():
            if mat in scene_mats:
                self._mat_combo.addItem(
                    f"{mat}  ← {base_name}",
                    userData={"mat": mat, "base": base_name},
                )
                added.add(mat)
        # 残りのシーンマテリアル
        for mat in scene_mats:
            if mat not in added:
                self._mat_combo.addItem(mat, userData={"mat": mat, "base": None})

        self._auto_fill_search()

    def _auto_fill_search(self):
        """マテリアル選択が変わったとき、検索キーをベース名の先頭トークンで自動設定する"""
        _, base_name = self._current_mat()
        if base_name:
            # 先頭トークン（例: "Box_openPBR_shader1" → "Box"）
            first_token = re.split(r"[_\-\s]", base_name)[0]
            self._search_edit.setText(first_token)
        else:
            self._search_edit.clear()

    def _current_mat(self):
        d = self._mat_combo.currentData()
        return (d["mat"], d["base"]) if d else (None, None)

    def _print(self, msg: str):
        self._log.appendPlainText(msg)
        QtWidgets.QApplication.processEvents()

    def _scan_scene(self):
        mat_name, base_name = self._current_mat()
        if not mat_name:
            QtWidgets.QMessageBox.warning(self, "未選択", "マテリアルを選択してください。")
            return
        # 検索キー欄の入力を優先、空の場合はベース名 or マテリアル名でフォールバック
        search = self._search_edit.text().strip() or base_name or re.sub(r"^M_", "", mat_name)
        threshold = self._thresh_slider.value()

        self._candidates = find_mesh_candidates(search, threshold)
        self._populate_list()
        self._result_label.setText(
            f"'{search}' に対して {len(self._candidates)} 件検出"
            f"（閾値 {threshold}%）"
        )

    def _populate_list(self):
        self._mesh_tree.clear()
        for c in self._candidates:
            item = QtWidgets.QTreeWidgetItem()
            item.setText(0, c["short_name"])
            item.setText(1, c["transform"])
            item.setText(2, f"{c['score']}%")
            item.setData(0, Qt.UserRole, c["transform"])
            # 70% 以上は自動チェック
            item.setCheckState(0, Qt.Checked if c["score"] >= 70 else Qt.Unchecked)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            # スコアで色分け
            s = c["score"]
            color = (QtGui.QColor(60, 200, 90)  if s >= 90 else
                     QtGui.QColor(200, 200, 60)  if s >= 70 else
                     QtGui.QColor(200, 140, 40)  if s >= 50 else
                     QtGui.QColor(200, 60, 60))
            item.setForeground(2, QtGui.QBrush(color))
            self._mesh_tree.addTopLevelItem(item)

    def _select_all(self):
        for i in range(self._mesh_tree.topLevelItemCount()):
            self._mesh_tree.topLevelItem(i).setCheckState(0, Qt.Checked)

    def _deselect_all(self):
        for i in range(self._mesh_tree.topLevelItemCount()):
            self._mesh_tree.topLevelItem(i).setCheckState(0, Qt.Unchecked)

    def _assign(self):
        mat_name, _ = self._current_mat()
        if not mat_name:
            return
        selected = [
            self._mesh_tree.topLevelItem(i).data(0, Qt.UserRole)
            for i in range(self._mesh_tree.topLevelItemCount())
            if self._mesh_tree.topLevelItem(i).checkState(0) == Qt.Checked
        ]
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "未選択", "割り当てるメッシュをチェックしてください。"
            )
            return

        import sys, io
        class _R(io.StringIO):
            def __init__(self, cb): super().__init__(); self._cb = cb
            def write(self, s):
                if s.strip(): self._cb(s.rstrip())
            def flush(self): pass

        old = sys.stdout
        sys.stdout = _R(self._print)
        try:
            count = assign_material_to_meshes(mat_name, selected)
            names = [t.split("|")[-1] for t in selected]
            preview = ", ".join(names[:3]) + ("..." if len(names) > 3 else "")
            self._print(f"\n[OK] {mat_name} → {preview} ({count} 件割り当て完了)")
        except Exception as e:
            self._print(f"[エラー] {e}")
        finally:
            sys.stdout = old


class UDIMSetupDialog(QtWidgets.QDialog):
    """UDIM テクスチャ自動セットアップ メインダイアログ"""

    TITLE = "UDIM Auto Setup  v1.0  ―  SP → Maya Arnold"

    def __init__(self, parent=None):
        if wrapInstance and parent is None:
            ptr    = omui.MQtUtil.mainWindow()
            parent = wrapInstance(int(ptr), QtWidgets.QWidget)
        super().__init__(parent)
        self.setWindowTitle(self.TITLE)
        self.setMinimumWidth(600)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self._scan_results = {}
        self._build_ui()

    # ── UI 構築 ─────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        # タイトル
        title = QtWidgets.QLabel("UDIM Texture Auto Setup  <small>v1.0</small>")
        title.setStyleSheet("font-size: 14px; font-weight: bold; color: #e0e0e0;")
        title.setTextFormat(Qt.RichText)
        root.addWidget(title)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        sep.setStyleSheet("color: #444;")
        root.addWidget(sep)

        # ─ フォルダ ────────────────────────────────────────────────────
        dir_grp = QtWidgets.QGroupBox("テクスチャフォルダ")
        dir_lay = QtWidgets.QHBoxLayout(dir_grp)
        self._dir_edit = QtWidgets.QLineEdit()
        self._dir_edit.setPlaceholderText("テクスチャフォルダを選択してください...")
        dir_lay.addWidget(self._dir_edit)
        browse_btn = QtWidgets.QPushButton("参照...")
        browse_btn.setFixedWidth(72)
        browse_btn.clicked.connect(self._browse)
        dir_lay.addWidget(browse_btn)
        root.addWidget(dir_grp)

        # ─ オプション ──────────────────────────────────────────────────
        opt_grp = QtWidgets.QGroupBox("オプション")
        opt_lay = QtWidgets.QFormLayout(opt_grp)

        self._prefix_edit = QtWidgets.QLineEdit("M_")
        self._prefix_edit.setFixedWidth(120)
        opt_lay.addRow("マテリアル名プレフィックス:", self._prefix_edit)

        self._tx_chk        = QtWidgets.QCheckBox(".tx 変換も実行する (maketx が必要)")
        self._recursive_chk = QtWidgets.QCheckBox("サブフォルダも再帰的に検索する")
        self._relpath_chk   = QtWidgets.QCheckBox("Maya プロジェクト相対パスを使用する")
        self._layout_chk    = QtWidgets.QCheckBox("Hypershade を自動レイアウトする")
        opt_lay.addRow("TX 変換:", self._tx_chk)
        opt_lay.addRow("再帰検索:", self._recursive_chk)
        opt_lay.addRow("パス形式:", self._relpath_chk)
        opt_lay.addRow("レイアウト:", self._layout_chk)
        root.addWidget(opt_grp)

        # ─ スキャンボタン ──────────────────────────────────────────────
        scan_row = QtWidgets.QHBoxLayout()
        scan_btn = QtWidgets.QPushButton(" スキャン実行")
        scan_btn.setStyleSheet("padding: 5px; font-weight: bold;")
        scan_btn.clicked.connect(self._scan)
        scan_row.addWidget(scan_btn)
        scan_row.addStretch()
        root.addLayout(scan_row)

        # ─ モデルツリー ────────────────────────────────────────────────
        self._model_tree = ModelTreeWidget()
        root.addWidget(self._model_tree)

        # ─ ログ ────────────────────────────────────────────────────────
        log_grp = QtWidgets.QGroupBox("ログ")
        log_lay = QtWidgets.QVBoxLayout(log_grp)
        self._log = QtWidgets.QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(130)
        self._log.setStyleSheet(
            "background:#1a1a1a; color:#b8b8b8; "
            "font-family:Consolas,monospace; font-size:11px;"
        )
        log_lay.addWidget(self._log)
        root.addWidget(log_grp)

        # ─ 実行ボタン ──────────────────────────────────────────────────
        btn_row = QtWidgets.QHBoxLayout()
        run_btn = QtWidgets.QPushButton("▶  選択モデルをセットアップ")
        run_btn.setStyleSheet(
            "background:#1a5276; color:white; font-weight:bold; padding:6px;"
        )
        run_btn.clicked.connect(self._run)
        tx_btn = QtWidgets.QPushButton(".tx のみ変換")
        tx_btn.clicked.connect(self._tx_only)
        mesh_btn = QtWidgets.QPushButton("メッシュ割り当て...")
        mesh_btn.setStyleSheet("padding: 5px;")
        mesh_btn.setToolTip("別ウィンドウでシーン内メッシュへの自動割り当てを実行")
        mesh_btn.clicked.connect(self._open_mesh_assign)
        clr_btn = QtWidgets.QPushButton("ログをクリア")
        clr_btn.clicked.connect(self._log.clear)
        btn_row.addWidget(run_btn)
        btn_row.addWidget(tx_btn)
        btn_row.addWidget(mesh_btn)
        btn_row.addStretch()
        btn_row.addWidget(clr_btn)
        root.addLayout(btn_row)

    # ── スロット ─────────────────────────────────────────────────────────
    def _browse(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "テクスチャフォルダを選択",
            self._dir_edit.text() or os.path.expanduser("~")
        )
        if path:
            self._dir_edit.setText(path)

    def _get_dir(self) -> str:
        d = self._dir_edit.text().strip()
        if not d:
            QtWidgets.QMessageBox.warning(self, "入力エラー", "テクスチャフォルダを指定してください。")
        return d

    def _print(self, msg: str):
        self._log.appendPlainText(msg)
        QtWidgets.QApplication.processEvents()

    def _redirect_and_call(self, fn):
        """print() を GUI ログにリダイレクトして fn を実行する"""
        import sys, io

        class _Redir(io.StringIO):
            def __init__(self, cb):
                super().__init__()
                self._cb = cb
            def write(self, s):
                if s.strip():
                    self._cb(s.rstrip())
            def flush(self):
                pass

        old = sys.stdout
        sys.stdout = _Redir(self._print)
        try:
            fn()
        except Exception as e:
            self._print(f"[エラー] {e}")
        finally:
            sys.stdout = old

    def _scan(self):
        d = self._get_dir()
        if not d:
            return
        self._log.clear()
        self._print(f"スキャン中: {d}")

        def _do():
            self._scan_results = scan_textures(
                d, recursive=self._recursive_chk.isChecked()
            )
            if not self._scan_results:
                self._print(
                    "テクスチャが検知されませんでした。\n"
                    "チェックポイント:\n"
                    "  - ファイル名が <ベース名>_<チャンネル>_1001.ext 等の形式か\n"
                    "  - タイル番号が 4 桁の数字か\n"
                    "  - サブフォルダにある場合は「再帰検索」を ON に"
                )
                return
            self._model_tree.populate(self._scan_results)
            n_models = len(self._scan_results)
            n_ch     = sum(len(v["channels"]) for v in self._scan_results.values())
            self._print(f"[OK] {n_models} モデル / {n_ch} チャンネル を検知しました")

        self._redirect_and_call(_do)

    def _run(self):
        if not self._scan_results:
            self._print("先にスキャンを実行してください。")
            return
        selected = self._model_tree.get_selected()
        if not selected:
            QtWidgets.QMessageBox.information(self, "選択なし", "モデルを 1 つ以上チェックしてください。")
            return
        self._print(f"\nセットアップ開始: {selected}")
        self._redirect_and_call(lambda: setup_udim_material(
            self._scan_results,
            selected_models=selected,
            material_prefix=self._prefix_edit.text(),
            create_tx=self._tx_chk.isChecked(),
            use_relative_path=self._relpath_chk.isChecked(),   # 
            layout_hypershade=self._layout_chk.isChecked(),    # 
        ))

    def _open_mesh_assign(self):
        """メッシュ割り当てダイアログを別ウィンドウで開く"""
        dlg = MeshAssignDialog(scan_results=self._scan_results, parent=self)
        dlg.show()

    def _tx_only(self):
        d = self._get_dir()
        if not d:
            return
        self._print(f"\n.tx 変換開始: {d}")
        self._redirect_and_call(lambda: batch_tx_convert(d))


# ===========================================================================
# エントリポイント
# ===========================================================================

def launch_gui():
    """メインセットアップ GUI を起動する"""
    for w in QtWidgets.QApplication.topLevelWidgets():
        if isinstance(w, UDIMSetupDialog):
            w.close(); w.deleteLater()
    dlg = UDIMSetupDialog()
    dlg.show()
    return dlg


def launch_mesh_assign_gui(scan_results: dict = None):
    """メッシュ割り当て GUI を単独で起動する"""
    for w in QtWidgets.QApplication.topLevelWidgets():
        if isinstance(w, MeshAssignDialog):
            w.close(); w.deleteLater()
    dlg = MeshAssignDialog(scan_results=scan_results)
    dlg.show()
    return dlg


if __name__ == "__main__":
    launch_gui()
