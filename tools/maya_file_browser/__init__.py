# -*- coding: utf-8 -*-
"""
maya_file_browser パッケージ。

Maya本体からは、このパッケージ経由ではなく `fbx_browser.py` を
単体でMayaのスクリプトフォルダへ配置し `import fbx_browser` する形で利用する
（詳細は README.md / install_fbx_browser_autostart.py を参照）。

`fbx_browser` はモジュールレベルで `maya.cmds` 等をimportするため、
Maya環境外でこのパッケージをimportしても壊れないよう、
ここでは同モジュールをre-exportしない。
"""

__version__ = "1.0.0"
