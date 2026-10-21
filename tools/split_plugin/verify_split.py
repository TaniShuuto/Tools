# -*- coding: utf-8 -*-
"""分割前の 1 ファイルと、分割後のパッケージを比べる検査台本。

使い方:
    python tools/split_plugin/verify_split.py <分割前の __init__.py> <分割後のパッケージフォルダー>
    (任意) --names-out <ファイル>  分割前のトップレベル名の一覧を書き出す
    (任意) --set-version X.Y.Z    分割時に版を上げた場合、__version__ だけ期待値を書き換えて比べる

検査する項目:
  (a) 各定義の ast.dump が完全一致する。コメントを含む元の文字列も一致する。
  (b) 分割前のトップレベル名がすべて、分割後の __init__ から参照できる。
  (c) 部品間の依存が層の表どおりで、循環が無い。
  (d) 純関数の部品に substance_painter / PySide / _qt の import が無い。
  (e) 各部品に __all__ があり、定義と一致する。__init__ は部品を最初にまとめて import し、
      遅延 import と import * が無い。_engine / _panel を読む部品が無い。
      すべて CRLF で、コンパイルできる。
  (f) 分割後に新しく増えた名前が、許可した一覧だけである。
"""
from __future__ import annotations

import ast
import collections
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import assignment  # noqa: E402

PACKAGE_INIT = "__init__"
# 分割後に新しく増えてよい名前(分割前には無い)
ALLOWED_NEW_NAMES = {"reload_plugin", "sys", "importlib"}
# 分割後に新しく増えてよい文(名前 -> 説明)。reload_plugin だけ。
ALLOWED_NEW_STATEMENTS = {"reload_plugin"}
IMPURE_PREFIXES = ("substance_painter", "PySide2", "PySide6")


class Report(object):
    def __init__(self):
        self.rows = []

    def add(self, label, ok, detail=""):
        self.rows.append((label, ok, detail))

    @property
    def ok(self):
        return all(ok for _, ok, _ in self.rows)

    def show(self):
        for label, ok, detail in self.rows:
            print("[{0}] {1}".format("OK" if ok else "NG", label))
            if detail:
                for line in str(detail).splitlines():
                    print("       " + line)
        print("")
        print("結果: {0}".format("すべて合格" if self.ok else "不合格あり"))


def read_lf(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    return raw, raw.decode("utf-8").replace("\r\n", "\n")


def chunks(text):
    """[(node, 先頭の空行を除いた元の文字列)] を返す(docstring / import / __all__ も含む)。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    tree = ast.parse(text)
    result = []
    prev_end = 0
    for node in tree.body:
        block = lines[prev_end:node.end_lineno]
        while block and block[0].strip() == "":
            block.pop(0)
        result.append((node, "\n".join(block)))
        prev_end = node.end_lineno
    return tree, result


def bound_names(tree):
    """トップレベルで束縛される名前(import の別名、def、class、代入、try / if の中を含む)。"""
    names = set()

    def collect(statements):
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
            elif isinstance(node, ast.Try):
                collect(node.body)
                for handler in node.handlers:
                    collect(handler.body)
                collect(node.orelse)
                collect(node.finalbody)
            elif isinstance(node, ast.If):
                collect(node.body)
                collect(node.orelse)

    collect(tree.body)
    return names


def statement_label(node):
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return ",".join(sorted(
            n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)))
    return "{0}@{1}".format(type(node).__name__, node.lineno)


def is_definition(node):
    """比べる対象の文。docstring、import、__all__ は除く。"""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return False
    if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
        return False
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
            node.targets[0], ast.Name) and node.targets[0].id == "__all__":
        return False
    return True


def package_files(package_dir):
    found = {}
    for name in sorted(os.listdir(package_dir)):
        if name.endswith(".py"):
            found[name[:-3]] = os.path.join(package_dir, name)
    return found


def relative_imports(tree):
    """[(level, module, names, トップレベルか)] を返す。"""
    top_ids = {id(n) for n in tree.body}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0:
            found.append((node.level, node.module, [a.name for a in node.names],
                          id(node) in top_ids))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "sp_live_sync_plugin":
                    found.append((-1, alias.name, [], id(node) in top_ids))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == (
                "sp_live_sync_plugin"):
            found.append((-1, node.module, [a.name for a in node.names], id(node) in top_ids))
    return found


def find_cycle(graph):
    color = {}
    cycles = []

    def dfs(node, path):
        color[node] = 1
        for nxt in sorted(graph.get(node, ())):
            if color.get(nxt) == 1:
                cycles.append(" -> ".join(path + [node, nxt]))
            elif color.get(nxt) is None:
                dfs(nxt, path + [node])
        color[node] = 2

    for node in sorted(graph):
        if color.get(node) is None:
            dfs(node, [])
    return cycles


def verify(before_path, package_dir, names_out=None, set_version=None):
    report = Report()
    _, before_text = read_lf(before_path)
    if set_version is not None:
        # 版を上げた場合だけ、__version__ の 1 行を期待値の側で書き換えて比べる
        match = re.search(r'^__version__ = "([^"]*)"$', before_text, re.M)
        if match is None:
            raise ValueError("分割前のファイルに __version__ がありません")
        before_text = before_text.replace(
            match.group(0), '__version__ = "{0}"'.format(set_version), 1)
    before_tree, before_chunks = chunks(before_text)
    files = package_files(package_dir)
    expected_files = set(assignment.LAYERS)
    report.add(
        "ファイル一覧が表どおり({0} 個)".format(len(expected_files)),
        set(files) == expected_files,
        "余分: {0} 不足: {1}".format(
            sorted(set(files) - expected_files), sorted(expected_files - set(files))),
    )
    if set(files) != expected_files:
        return report, None

    parsed = {}
    raws = {}
    for part, path in files.items():
        raw, text = read_lf(path)
        raws[part] = raw
        parsed[part] = (text,) + chunks(text)

    # (a) 定義ごとの ast.dump と、コメントを含む元の文字列
    before_defs = collections.Counter()
    before_texts = collections.Counter()
    for node, text in before_chunks:
        if is_definition(node):
            before_defs[ast.dump(node)] += 1
            before_texts[text] += 1
    after_defs = collections.Counter()
    after_texts = collections.Counter()
    after_nodes = {}
    for part, (_text, _tree, part_chunks) in parsed.items():
        for node, text in part_chunks:
            if is_definition(node):
                after_defs[ast.dump(node)] += 1
                after_texts[text] += 1
                after_nodes[ast.dump(node)] = (part, node)
    missing = before_defs - after_defs
    extra = after_defs - before_defs
    extra_labels = sorted(statement_label(after_nodes[d][1]) for d in extra)
    report.add(
        "(a) 定義の ast.dump が完全一致({0} 文)".format(sum(before_defs.values())),
        not missing and set(extra_labels) <= ALLOWED_NEW_STATEMENTS,
        "分割前にあって分割後に無い: {0}\n分割後に増えた文: {1}".format(
            len(missing), extra_labels),
    )
    text_missing = before_texts - after_texts
    text_extra = [t for t in (after_texts - before_texts) if "def reload_plugin(" not in t]
    report.add(
        "(a) コメントを含む元の文字列が一致",
        not text_missing and not text_extra,
        "不一致: 元にあって無い {0} / 増えた {1}".format(len(text_missing), len(text_extra)),
    )

    # (b) 名前
    before_names = bound_names(before_tree)
    init_tree = parsed[PACKAGE_INIT][1]
    after_init_names = bound_names(init_tree)
    lost = sorted(before_names - after_init_names)
    report.add(
        "(b) 分割前のトップレベル名({0} 個)がすべて __init__ から参照できる".format(
            len(before_names)),
        not lost,
        "参照できない: {0}".format(lost),
    )
    # __init__ の from ._x import n の n が、_x の __all__ にある
    all_of = {}
    bad_reexport = []
    for part, (_t, tree, _c) in parsed.items():
        names = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and (
                    node.targets[0].id == "__all__"):
                names = [e.value for e in node.value.elts]
        all_of[part] = names
    for node in init_tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            for alias in node.names:
                if alias.name not in (all_of.get(node.module) or ()):
                    bad_reexport.append("{0}.{1}".format(node.module, alias.name))
    report.add("(b) 再エクスポートの名前が、元の部品の __all__ にある",
               not bad_reexport, "あるべき所に無い: {0}".format(bad_reexport))

    new_names = sorted(after_init_names - before_names)
    report.add(
        "(f) 増えた名前が許可した一覧だけ",
        set(new_names) <= ALLOWED_NEW_NAMES,
        "増えた名前: {0} / 許可: {1}".format(new_names, sorted(ALLOWED_NEW_NAMES)),
    )

    # (c) 依存
    layers = assignment.LAYERS
    graph = {}
    problems = []
    for part, (_t, tree, _c) in parsed.items():
        for level, module, names, at_top in relative_imports(tree):
            if level == -1:
                problems.append("{0}: 絶対 import でパッケージを読んでいます: {1}".format(part, module))
                continue
            if level != 1:
                problems.append("{0}: from {1} import ... は想定外".format(part, "." * level))
                continue
            if module is None:
                problems.append("{0}: from . import {1} は禁止".format(part, names))
                continue
            if module == PACKAGE_INIT or module not in layers:
                problems.append("{0}: 想定外の import .{1}".format(part, module))
                continue
            if part != PACKAGE_INIT:
                graph.setdefault(part, set()).add(module)
                if layers[module] > layers[part]:
                    problems.append(
                        "{0}(層{1}) が上の層 {2}(層{3}) を import".format(
                            part, layers[part], module, layers[module]))
    cycles = find_cycle(graph)
    problems.extend("循環: " + c for c in cycles)
    edges = sum(len(v) for v in graph.values())
    report.add("(c) 部品間の依存が層の表どおりで、循環 0({0} 本)".format(edges),
               not problems, "\n".join(problems))

    # (d) 純関数の部品
    impure = []
    for part in assignment.PURE_PARTS:
        _t, tree, _c = parsed[part]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(IMPURE_PREFIXES):
                        impure.append("{0}: import {1}".format(part, alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(IMPURE_PREFIXES) or (node.level and module == "_qt"):
                    impure.append("{0}: from {1}{2} import".format(part, "." * node.level, module))
    report.add("(d) 純関数の部品({0})に substance_painter / PySide / _qt の import が無い".format(
        ", ".join(assignment.PURE_PARTS)), not impure, "\n".join(impure))

    # (e) __all__ と __init__
    all_problems = []
    for part, (_t, tree, part_chunks) in parsed.items():
        if part == PACKAGE_INIT:
            continue
        defined = []
        for node, _text in part_chunks:
            if is_definition(node):
                defined.extend(sorted(bound_names(ast.Module(body=[node], type_ignores=[]))))
        names = all_of.get(part)
        if names is None:
            all_problems.append("{0}: __all__ が無い".format(part))
        elif sorted(names) != sorted(defined):
            all_problems.append("{0}: __all__ と定義が違う {1}".format(
                part, sorted(set(names) ^ set(defined))))
    report.add("(e) 各部品の __all__ が定義と一致", not all_problems, "\n".join(all_problems))

    init_problems = []
    body = init_tree.body
    part_import_positions = []
    for index, node in enumerate(body):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in layers:
            part_import_positions.append(index)
    imported_parts = {body[i].module for i in part_import_positions}
    wanted = set(layers) - {PACKAGE_INIT}
    if imported_parts != wanted:
        init_problems.append("__init__ が import していない部品: {0}".format(sorted(wanted - imported_parts)))
    non_import_before = [
        i for i, node in enumerate(body[:max(part_import_positions or [0])])
        if not isinstance(node, (ast.Import, ast.ImportFrom))
        and not (isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant))
    ]
    if non_import_before:
        init_problems.append("部品の import より前に import 以外の文があります")
    for part, (_t, tree, _c) in parsed.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                init_problems.append("{0}: import * があります".format(part))
        for level, module, names, at_top in relative_imports(tree):
            if not at_top:
                init_problems.append("{0}: 関数の中などで部品を import しています(遅延 import)".format(part))
    report.add("(e) __init__ は部品を最初にまとめて import し、遅延 import と import * が無い",
               not init_problems, "\n".join(init_problems))

    reads = []
    for part, (_t, tree, _c) in parsed.items():
        if part == PACKAGE_INIT:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in ("_engine", "_panel"):
                reads.append("{0}: line {1} {2}".format(part, node.lineno, node.id))
            if isinstance(node, ast.Global):
                reads.append("{0}: line {1} global {2}".format(part, node.lineno, node.names))
    global_in_init = sorted({
        name for node in ast.walk(init_tree) if isinstance(node, ast.Global) for name in node.names})
    report.add("(e) _engine / _panel を読む部品・global 文が __init__ 以外に無い",
               not reads and set(global_in_init) <= {"_engine", "_panel"},
               "\n".join(reads) + ("" if not global_in_init else
                                   "\n__init__ の global: {0}".format(global_in_init)))

    eol_problems = []
    for part, raw in raws.items():
        if b"\r\n" not in raw or raw.replace(b"\r\n", b"").count(b"\n") or raw.count(b"\r") != raw.count(b"\r\n"):
            eol_problems.append(part)
        if not raw.endswith(b"\r\n"):
            eol_problems.append(part + "(末尾の改行なし)")
    report.add("(e) すべて CRLF", not eol_problems, ", ".join(eol_problems))

    compile_problems = []
    for part, path in files.items():
        try:
            with open(path, "rb") as handle:
                compile(handle.read(), path, "exec")
        except (SyntaxError, ValueError) as exc:
            compile_problems.append("{0}: {1}".format(part, exc))
    report.add("(e) すべてコンパイルできる", not compile_problems, "\n".join(compile_problems))

    flakes = run_pyflakes(package_dir)
    if flakes is not None:
        bad = [line for line in flakes
               if "undefined name" in line or (
                   "imported but unused" in line and not line.startswith(
                       os.path.join(package_dir, "__init__.py")))]
        report.add("(参考) pyflakes: 未定義の名前と、部品の未使用 import が無い", not bad, "\n".join(bad))

    if names_out:
        with open(names_out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(sorted(before_names)) + "\n")
    return report, parsed


def run_pyflakes(package_dir):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", package_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    except Exception:
        return None
    if "No module named pyflakes" in (result.stderr or ""):
        return None
    return [line for line in (result.stdout or "").splitlines() if line.strip()]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    names_out = None
    set_version = None
    if "--set-version" in argv:
        index = argv.index("--set-version")
        set_version = argv[index + 1]
        del argv[index:index + 2]
    if "--names-out" in argv:
        index = argv.index("--names-out")
        names_out = argv[index + 1]
        del argv[index:index + 2]
    if len(argv) != 2:
        print(__doc__)
        return 2
    report, _ = verify(argv[0], argv[1], names_out, set_version)
    report.show()
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
