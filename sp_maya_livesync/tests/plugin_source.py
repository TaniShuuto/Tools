# -*- coding: utf-8 -*-
"""sp_live_sync_plugin パッケージ内の .py を、構造テストから読む。

パッケージを引数に取れる。省略時はリポジトリの
sp_maya_livesync/sp_live_sync_plugin を読む。__pycache__ は含めない。
"""
from __future__ import annotations

import os

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_TESTS)
DEFAULT_PACKAGE_DIR = os.path.join(_PKG, "sp_live_sync_plugin")


def plugin_source_files(package_dir=None):
    """パッケージ配下のすべての .py のパスを、相対順で返す。"""
    root = DEFAULT_PACKAGE_DIR if package_dir is None else os.fspath(package_dir)
    found = []
    if not os.path.isdir(root):
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if name != "__pycache__" and not name.startswith(".")
        ]
        for name in filenames:
            if name.endswith(".py"):
                found.append(os.path.join(dirpath, name))
    found.sort()
    return found


def plugin_source_text(package_dir=None):
    """plugin_source_files() の中身を、ファイルごとに改行で区切って連結する。"""
    parts = []
    for path in plugin_source_files(package_dir):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        if text and not text.endswith("\n"):
            text += "\n"
        parts.append(text)
    return "".join(parts)
