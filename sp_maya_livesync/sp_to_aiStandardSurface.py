# ============================================================
#  sp_to_aiStandardSurface.py
#  Substance Painter textures -> aiStandardSurface auto assign
#  v2.1  Prefix auto-detection + checklist UI
#        + Displacement re-centering (remapValue) fix
#
#  Requirements : Maya 2022+ / Arnold 7+
#  Usage        : Paste into Maya's Script Editor (Python tab) and run,
#                 or save to ~/Documents/maya/scripts/ and call from a shelf
#
#  Note: All cmds-facing strings (UI labels, dialogs, print logs) are kept
#  in plain ASCII English on purpose. On some Windows + Japanese-locale
#  Maya setups, non-ASCII strings passed through cmds/MEL get mangled into
#  "?" characters. Keeping these strings ASCII-only avoids that entirely.
#  Comments (this text) are not affected since Python parses the source
#  file as UTF-8 regardless of OS locale.
#
#  ------------------------------------------------------------
#  v2.1 change log (displacement fix):
#    - Previously the height/displacement file node's outColorR was wired
#      directly into displacementShader.displacement. A standard 0-1
#      grayscale height map uses 0.5 as the "no displacement" mid value,
#      so feeding the raw value shifts the whole surface outward by roughly
#      (mean brightness * scale), which showed up as the object looking
#      uniformly "puffed up".
#    - Now a remapValue node re-maps 0-1 into -0.5..0.5 before the
#      displacementShader, so 0.5 becomes the zero-displacement baseline.
#      This matches the approach already used in maya_live_sync.py's
#      create_shader_network(), keeping both tools consistent.
#    - displacementShader.scale is set to a conservative starting value
#      (0.1) when the attribute exists, instead of leaving Maya's default
#      of 1.0 (which is usually far too strong for test geometry). Tune it
#      in the render view afterwards.
#    - A short reminder is printed: Arnold displacement also needs the mesh
#      shape's Subdivision (aiSubdivType = catclark/linear, iterations) and
#      Bounds Padding (aiDispPadding) to be set. This script intentionally
#      does not touch the mesh shape, so that step stays manual.
# ============================================================

import os
import re
import maya.cmds as cmds

# -------------------------------------------------------
#  Displacement tuning defaults (v2.1)
# -------------------------------------------------------
#  Starting scale for the displacementShader. 1.0 (Maya default) is almost
#  always too strong for small test meshes; start low and raise in the
#  render view. Set to None to leave Maya's default untouched.
DISPLACEMENT_SCALE_DEFAULT = 0.1

# -------------------------------------------------------
#  Naming patterns per texture type
# -------------------------------------------------------
TEXTURE_PATTERNS = {
    "baseColor": {
        "patterns": [r"basecolor", r"base_color", r"albedo", r"diffuse",
                     r"color(?!.*normal)"],
        "attr": "baseColor",
        "colorSpace": "sRGB",
        "nodeType": "file",
    },
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
        "patterns": [r"specular(?!.*roughness)(?!.*color)", r"spec(?!.*roughness)"],
        "attr": "specular",
        "colorSpace": "Raw",
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
    v2.1: Wire a height/displacement file node into the material's shading
    group THROUGH a remapValue node that re-centers the 0-1 grayscale range
    to -0.5..0.5. This makes 0.5 (mid gray) the zero-displacement baseline,
    which removes the uniform "puffed up" offset that a direct connection
    produces.

    A conservative scale is set on the displacementShader as a starting
    point. The mesh-side Subdivision / Bounds Padding still has to be set
    manually (this function does not touch the mesh shape on purpose).

    Returns the created displacementShader node.
    """
    # 0-1 -> -0.5..0.5 re-centering.
    remap = cmds.shadingNode("remapValue", asUtility=True)
    cmds.setAttr(f"{remap}.inputMin", 0.0)
    cmds.setAttr(f"{remap}.inputMax", 1.0)
    cmds.setAttr(f"{remap}.outputMin", -0.5)
    cmds.setAttr(f"{remap}.outputMax", 0.5)
    cmds.connectAttr(f"{fn}.outColorR", f"{remap}.inputValue", force=True)

    disp = cmds.shadingNode("displacementShader", asShader=True)
    cmds.connectAttr(f"{remap}.outValue", f"{disp}.displacement", force=True)

    # Conservative starting scale (Maya default 1.0 is usually too strong).
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

    print("[SP-to-aiSS] Displacement connected via remapValue "
          "(0-1 -> -0.5..0.5), scale="
          f"{DISPLACEMENT_SCALE_DEFAULT}. "
          "Remember to set the mesh Subdivision (aiSubdivType) and "
          "Bounds Padding (aiDispPadding) on the shape node.")
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
    Create (or reuse) one aiStandardSurface for the given prefix and
    connect all matching textures to it.
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
        # v2.1: re-centering + conservative scale moved into a helper.
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

        # --- Standard ---
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
                        cmds.warning(f"[SP-to-aiSS] Failed to connect {info['attr']}: {e}")

            for extra_attr, val in info.get("also_set", {}).items():
                try:
                    cmds.setAttr(f"{mat}.{extra_attr}", val)
                except Exception:
                    pass

            assigned[tex_type] = fn

    # --- AO multiply ---
    if ao_multiply and ao_node and "baseColor" in assigned:
        mult = cmds.shadingNode("aiMultiply", asUtility=True)
        existing = cmds.listConnections(f"{mat}.baseColor", plugs=True, source=True)
        src = existing[0] if existing else f"{assigned['baseColor']}.outColor"
        cmds.connectAttr(src,                   f"{mult}.input1",    force=True)
        cmds.connectAttr(f"{ao_node}.outColor", f"{mult}.input2",    force=True)
        cmds.connectAttr(f"{mult}.outColor",    f"{mat}.baseColor",  force=True)
        print(f"[SP-to-aiSS] [{prefix}] AO multiplied into baseColor.")

    # --- Assign to selected meshes ---
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
}

WIN_MAIN = "spToAiSSWindow"


def show_ui():
    if cmds.window(WIN_MAIN, exists=True):
        cmds.deleteUI(WIN_MAIN)

    cmds.window(WIN_MAIN, title="SP -> aiStandardSurface  v2.1",
                widthHeight=(500, 420), sizeable=True)
    cmds.columnLayout("mainCol", adjustableColumn=True,
                      rowSpacing=6, columnOffset=["both", 10])
    cmds.separator(h=8, style="none")

    # ---- Texture folder ----
    cmds.text(label="Texture folder", align="left", font="boldLabelFont")
    cmds.rowLayout(numberOfColumns=2, columnWidth2=(370, 100),
                   adjustableColumn=1,
                   columnAttach=[(1,"both",0),(2,"both",4)])
    dir_field = cmds.textField(placeholderText="Type a path or click Browse...")
    cmds.button(label="Browse...", command=lambda _: _browse_dir(dir_field))
    cmds.setParent("..")

    cmds.button(label="Scan folder for texture sets",
                height=32, backgroundColor=(0.25, 0.45, 0.65),
                command=lambda _: _scan_and_build_checklist(dir_field))

    _gui_state["dir_field"] = dir_field

    cmds.separator(h=4, style="in")

    # ---- Prefix list (scroll) ----
    cmds.text(label="Detected texture sets (check the ones to create)",
              align="left", font="boldLabelFont")

    cmds.scrollLayout(height=140, childResizable=True)
    col = cmds.columnLayout(adjustableColumn=True, rowSpacing=2)
    cmds.text(label="-- Scan a folder first --", align="left",
              font="smallObliqueLabelFont")
    cmds.setParent("..")          # col
    cmds.setParent("..")          # scroll

    _gui_state["scroll_col"] = col

    cmds.separator(h=4, style="in")

    # ---- Options ----
    cmds.text(label="Options", align="left", font="boldLabelFont")
    ao_cb  = cmds.checkBox(label="Multiply AO into BaseColor",   value=True)
    sel_cb = cmds.checkBox(label="Assign to selected meshes", value=False)

    _gui_state["ao_cb"]  = ao_cb
    _gui_state["sel_cb"] = sel_cb

    cmds.separator(h=8, style="in")

    # ---- Run button ----
    cmds.button(label="Create checked materials", height=40,
                backgroundColor=(0.2, 0.6, 0.35),
                command=lambda _: _run_all())

    cmds.separator(h=4, style="none")
    cmds.text(label="* See Script Editor for the log output", align="left",
              font="smallObliqueLabelFont")
    cmds.separator(h=8, style="none")

    cmds.showWindow(WIN_MAIN)


def _browse_dir(field):
    result = cmds.fileDialog2(fileMode=3, caption="Select texture folder")
    if result:
        cmds.textField(field, edit=True, text=result[0])


def _scan_and_build_checklist(dir_field):
    """Scan the folder and rebuild the checklist of detected prefixes."""
    tex_dir = cmds.textField(dir_field, query=True, text=True).strip()
    if not tex_dir or not os.path.isdir(tex_dir):
        cmds.confirmDialog(title="Error",
                           message="Please specify a valid texture folder.",
                           button=["OK"])
        return

    groups = scan_prefixes(tex_dir)
    _gui_state["prefix_files"] = groups
    _gui_state["prefix_cbs"]   = {}

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
        cmds.text(label="No texture files found.",
                  align="left", font="smallObliqueLabelFont")
        return

    # Select all / Deselect all row
    cmds.rowLayout(numberOfColumns=2, columnWidth2=[110, 110],
                   columnAttach=[(1,"both",2),(2,"both",2)])
    cmds.button(label="Select all",
                command=lambda _: _set_all_checks(True))
    cmds.button(label="Deselect all",
                command=lambda _: _set_all_checks(False))
    cmds.setParent("..")

    cmds.separator(h=4, style="none")

    # one checkbox per prefix + texture-count label
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

    print(f"[SP-to-aiSS] Scan complete: {len(groups)} set(s) found")


def _tex_type_label(fpath):
    """Return a short label for the texture type of a file."""
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
    tex_dir    = cmds.textField(_gui_state["dir_field"], query=True, text=True).strip()

    targets = [
        prefix for prefix, cb in _gui_state["prefix_cbs"].items()
        if cmds.checkBox(cb, query=True, value=True)
        and prefix != UNKNOWN_GROUP
    ]

    if not targets:
        cmds.confirmDialog(title="Notice",
                           message="No texture sets are checked.",
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

    msg = f"Created {len(created)} material(s):\n" + "\n".join(created)
    cmds.confirmDialog(title="Done", message=msg, button=["OK"])
    print(f"\n[SP-to-aiSS] Done: {created}")


# ============================================================
#  Entry point
# ============================================================
show_ui()
