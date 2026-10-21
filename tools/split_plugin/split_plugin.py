# -*- coding: utf-8 -*-
"""sp_live_sync_plugin の __init__.py(1 ファイル)を、役割ごとの部品へ機械的に分割する。

使い方:
    python tools/split_plugin/split_plugin.py <分割前の __init__.py> <出力先フォルダー> [--set-version X.Y.Z]

- 入力は分割前の 1 ファイルだけ。何度実行しても同じ出力になる。
- 各トップレベル文(関数・クラス・代入・try による import 切り替えなど)は、
  直前のコメントを含めて、元の文字列のまま部品へ移す。中身は書き換えない。
- どの部品へ移すかは assignment.py の「名前の表」で決める。表に無い名前があれば
  エラーで止まる。
- 部品の先頭には、その部品が使う import(標準ライブラリ、substance_painter、_qt、
  部品間の相対 import)を自動で付ける。部品ごとに __all__ を書く。
- 層の規則(下の層は上の層を import しない。同じ層でも循環させない)に反する
  割り当てだと、エラーで止まる。
- 出力の改行は CRLF(プラグイン本体と同じ)。
"""
from __future__ import annotations

import ast
import builtins
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import assignment  # noqa: E402

PACKAGE_INIT = "__init__"

# 新しく足すフック。分割前には無い(5 章)。
RELOAD_PLUGIN_SOURCE = '''\
# ---------------------------------------------------------------------------
# 再読み込み(Painter の substance_painter_plugins.reload_plugin が呼ぶフック)
# ---------------------------------------------------------------------------
# Adobe の説明では、このフックは close_plugin() のあと、start_plugin() の前に
# 呼ばれ、パッケージ形式のプラグインが「部品(サブモジュール)を手で読み直す」ための
# ものとされている。本体(この __init__)を importlib.reload するのがフックより前か
# 後かは資料からは確定できないため、どちらの順でも部品が新しくなるようにしてある。
#   - sys.modules から部品を取り除く: 本体の再読み込みがこのあとに来る場合、
#     本体は新しく読み直された部品を import する。
#   - 本体をここでも読み込み直す: 本体の再読み込みが先に済んでいて古い部品を
#     指したままの場合に、新しい部品へ付け替える(順序が逆でも二重になるだけで害は無い)。
# 要実機確認: Painter 実機での呼ばれる順序と、再読み込み後に部品が新しくなること。
def reload_plugin(*_args, **_kwargs):
    package = sys.modules[__name__]
    prefix = __name__ + "."
    for module_name in [name for name in sys.modules if name.startswith(prefix)]:
        del sys.modules[module_name]
    importlib.reload(package)
'''

# __init__ が互換のため・再読み込みのために新しく持つ import(分割前には無い)
INIT_EXTRA_IMPORTS = ("import sys", "import importlib")


class SplitError(Exception):
    pass


# ---------------------------------------------------------------------------
# 名前の解析(関数・クラスの中で束縛される名前を除いた、モジュール全体の参照)
# ---------------------------------------------------------------------------
def _scope_bindings(body_nodes):
    """関数・クラスの本体(入れ子の関数・クラスの中は除く)で束縛される名前。"""
    out = set()

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for child in ast.walk(node):
                if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                    out.add(child.target.id)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for node in body_nodes:
        visit(node)
    return out


def _arg_names(args):
    names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def module_level_refs(node):
    """トップレベル文 node が、モジュールのグローバルとして参照する名前の集合。

    関数の引数・ローカル変数、内包表記の変数は除く。global 宣言された名前は含める。
    """
    refs = set()

    def walk(n, scopes):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                walk(d, scopes)
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x]:
                walk(d, scopes)
            every = n.args.posonlyargs + n.args.args + n.args.kwonlyargs
            every += [x for x in (n.args.vararg, n.args.kwarg) if x]
            for a in every:
                if a.annotation:
                    walk(a.annotation, scopes)
            if n.returns:
                walk(n.returns, scopes)
            declared_global = set()
            for x in ast.walk(n):
                if isinstance(x, ast.Global):
                    declared_global |= set(x.names)
            bound = (_arg_names(n.args) | _scope_bindings(n.body)) - declared_global
            for stmt in n.body:
                walk(stmt, scopes + [bound])
            return
        if isinstance(n, ast.Lambda):
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x]:
                walk(d, scopes)
            walk(n.body, scopes + [_arg_names(n.args)])
            return
        if isinstance(n, ast.ClassDef):
            for d in n.decorator_list + n.bases + [k.value for k in n.keywords]:
                walk(d, scopes)
            class_bound = _scope_bindings(n.body)
            for stmt in n.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk(stmt, scopes)
                else:
                    walk(stmt, scopes + [class_bound])
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            local = set()
            for g in n.generators:
                for x in ast.walk(g.target):
                    if isinstance(x, ast.Name):
                        local.add(x.id)
            for x in ast.walk(n):
                if isinstance(x, ast.NamedExpr) and isinstance(x.target, ast.Name):
                    local.add(x.target.id)
            first = True
            for g in n.generators:
                walk(g.iter, scopes if first else scopes + [local])
                first = False
                for cond in g.ifs:
                    walk(cond, scopes + [local])
            if isinstance(n, ast.DictComp):
                walk(n.key, scopes + [local])
                walk(n.value, scopes + [local])
            else:
                walk(n.elt, scopes + [local])
            return
        if isinstance(n, ast.Name):
            if not any(n.id in s for s in scopes):
                refs.add(n.id)
            return
        for child in ast.iter_child_nodes(n):
            walk(child, scopes)

    walk(node, [])
    for x in ast.walk(node):
        if isinstance(x, ast.Global):
            refs |= set(x.names)
    return refs


def defined_names(node):
    """トップレベル文 node が定義する名前(代入先、def/class 名、try 内の import 名)。"""
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [x.id for t in targets for x in ast.walk(t) if isinstance(x, ast.Name)]
    if isinstance(node, ast.Try):
        found = set()
        for x in ast.walk(node):
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                found.add(x.id)
            elif isinstance(x, (ast.Import, ast.ImportFrom)):
                for alias in x.names:
                    found.add(alias.asname or alias.name.split(".")[0])
        return sorted(found)
    return []


# ---------------------------------------------------------------------------
# 分割前のファイルを文ごとに切る
# ---------------------------------------------------------------------------
class Chunk(object):
    """トップレベル文 1 つ(直前のコメント・空行を含む)。"""

    def __init__(self, index, node, lead_blanks, text):
        self.index = index
        self.node = node
        self.lead_blanks = lead_blanks   # 直前の空行の数(コメントより前)
        self.text = text                 # 先頭の空行を除いた、元の文字列
        self.names = defined_names(node)
        self.refs = module_level_refs(node)
        self.part = None

    @property
    def is_def(self):
        return isinstance(self.node, (ast.FunctionDef, ast.ClassDef))


class Source(object):
    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        tree = ast.parse(text)
        self.tree = tree
        self.docstring = None
        self.header_imports = []   # [(alias, statement_text)] 元の並び順
        self.chunks = []
        prev_end = 0
        first = True
        for node in tree.body:
            start = prev_end + 1
            end = node.end_lineno
            block = self.lines[start - 1:end]
            lead = 0
            while lead < len(block) and block[lead].strip() == "":
                lead += 1
            body_text = "\n".join(block[lead:])
            own_text = "\n".join(self.lines[node.lineno - 1:end])
            prev_end = end
            if first and isinstance(node, ast.Expr) and isinstance(
                    getattr(node, "value", None), ast.Constant):
                self.docstring = body_text
                first = False
                continue
            first = False
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                # import 文の直前にコメントがあると捨ててしまうので止める。
                if body_text != own_text:
                    raise SplitError(
                        "import 文の直前にコメントがあります(line {0})".format(node.lineno))
                for alias in node.names:
                    bound = (alias.asname or alias.name).split(".")[0]
                    self.header_imports.append((bound, own_text))
                continue
            self.chunks.append(Chunk(len(self.chunks), node, lead, body_text))
        if prev_end < len(self.lines):
            tail = [ln for ln in self.lines[prev_end:] if ln.strip()]
            if tail:
                raise SplitError("最後の文のあとにコメントなどが残っています")
        # 同じ import 文を表す alias の重複を除く(import a, b のような 1 文複数名)
        seen = set()
        unique = []
        for alias, text_ in self.header_imports:
            if alias in seen:
                raise SplitError("import した名前が重複しています: " + alias)
            seen.add(alias)
            unique.append((alias, text_))
        self.header_imports = unique
        self.version = "?"
        for chunk in self.chunks:
            if chunk.names == ["__version__"] and isinstance(chunk.node.value, ast.Constant):
                self.version = str(chunk.node.value.value)


# ---------------------------------------------------------------------------
# 割り当て・依存の解決
# ---------------------------------------------------------------------------
def assign_chunks(source, name_part):
    problems = []
    for chunk in source.chunks:
        if not chunk.names:
            problems.append("名前を定義しない文があります(line {0})".format(chunk.node.lineno))
            continue
        parts = {name_part.get(n) for n in chunk.names}
        if None in parts:
            missing = [n for n in chunk.names if n not in name_part]
            problems.append("表に無い名前: {0} (line {1})".format(missing, chunk.node.lineno))
            continue
        if len(parts) != 1:
            problems.append("1 つの文が複数の部品に割り当てられています: {0} (line {1})".format(
                chunk.names, chunk.node.lineno))
            continue
        chunk.part = parts.pop()
    defined = {n for c in source.chunks for n in c.names}
    for name in name_part:
        if name not in defined:
            problems.append("表にあるが元のファイルに無い名前: " + name)
    if problems:
        raise SplitError("割り当てエラー:\n  " + "\n  ".join(problems))


def resolve_dependencies(source, name_part):
    """部品ごとの (必要な部品間 import, 必要な元の import 文) を求める。"""
    owner = {}
    for chunk in source.chunks:
        for name in chunk.names:
            owner[name] = chunk
    header_aliases = {alias for alias, _ in source.header_imports}
    known = set(owner) | header_aliases | set(dir(builtins)) | {"__file__", "__name__"}
    cross = {}      # part -> {other_part: set(names)}
    header = {}     # part -> set(alias)
    errors = []
    for chunk in source.chunks:
        part = chunk.part
        for ref in sorted(chunk.refs):
            if ref in chunk.names and ref not in owner:
                continue
            if ref in owner:
                other = owner[ref].part
                if other != part:
                    cross.setdefault(part, {}).setdefault(other, set()).add(ref)
                continue
            if ref in header_aliases:
                header.setdefault(part, set()).add(ref)
                continue
            if ref in known:
                continue
            errors.append("{0}: 未定義の名前 {1} (line {2})".format(
                part, ref, chunk.node.lineno))
    if errors:
        raise SplitError("\n".join(errors))
    # __init__ は互換のため、元の import 文をすべて持つ
    header[PACKAGE_INIT] = set(header_aliases)
    return cross, header


def check_layers(cross):
    layers = assignment.LAYERS
    problems = []
    graph = {}
    for part, deps in cross.items():
        for other, names in deps.items():
            graph.setdefault(part, set()).add(other)
            if layers[other] > layers[part]:
                problems.append("上の層を import しています: {0}(層{1}) -> {2}(層{3}) {4}".format(
                    part, layers[part], other, layers[other], sorted(names)))
            elif layers[other] == layers[part]:
                pass
    # 循環(同じ層どうしを含む)
    color = {}

    def dfs(node, path):
        color[node] = 1
        for nxt in sorted(graph.get(node, ())):
            if color.get(nxt) == 1:
                problems.append("循環があります: " + " -> ".join(path + [node, nxt]))
            elif color.get(nxt) is None:
                dfs(nxt, path + [node])
        color[node] = 2

    for node in sorted(graph):
        if color.get(node) is None:
            dfs(node, [])
    if problems:
        raise SplitError("層の規則に反します:\n  " + "\n  ".join(problems))


def check_pure_parts(cross, header):
    problems = []
    for part in assignment.PURE_PARTS:
        bad = sorted(a for a in header.get(part, ()) if a in _IMPURE_ALIASES)
        if bad:
            problems.append("{0} が Painter / Qt の名前を使っています: {1}".format(part, bad))
        if "_qt" in cross.get(part, {}):
            problems.append("{0} が _qt を import する必要があります".format(part))
        for other in cross.get(part, {}):
            if other in ("_qt",):
                continue
    if problems:
        raise SplitError("純関数の部品の規則に反します:\n  " + "\n  ".join(problems))


_IMPURE_ALIASES = {
    "event", "export", "project", "textureset", "sp_log", "ui", "sp_exception",
}


# ---------------------------------------------------------------------------
# 出力の組み立て
# ---------------------------------------------------------------------------
def _format_names_import(module, names):
    names = sorted(names)
    line = "from {0} import {1}".format(module, ", ".join(names))
    if len(line) <= 88:
        return line
    body = "".join("    {0},\n".format(n) for n in names)
    return "from {0} import (\n{1})".format(module, body)


def _all_block(names):
    if not names:
        return "__all__ = []"
    return "__all__ = [\n{0}]".format("".join('    "{0}",\n'.format(n) for n in names))


def _doc_literal(text):
    return '"""{0}"""'.format(text)


def _part_order(parts):
    layers = assignment.LAYERS
    return sorted(parts, key=lambda p: (layers[p], p))


def join_chunks(chunks):
    out = []
    prev = None
    for chunk in chunks:
        if prev is None:
            sep = 0
        elif chunk.index == prev.index + 1:
            sep = chunk.lead_blanks
        else:
            sep = max(chunk.lead_blanks, 2 if (chunk.is_def or prev.is_def) else 1)
        out.append("\n" * sep + chunk.text + "\n" if prev is not None else chunk.text + "\n")
        prev = chunk
    return "".join(out)


def render_part(part, source, chunks, cross, header):
    stdlib_lines = []
    painter_lines = []
    needed = header.get(part, set())
    for alias, text in source.header_imports:
        if alias not in needed:
            continue
        if text.startswith("import substance_painter"):
            painter_lines.append(text)
        else:
            stdlib_lines.append(text)
    relative = []
    deps = cross.get(part, {})
    for other in _part_order(deps):
        relative.append(_format_names_import("." + other, deps[other]))
    groups = []
    if stdlib_lines:
        groups.append("\n".join(stdlib_lines))
    if painter_lines:
        groups.append("\n".join(painter_lines))
    if relative:
        groups.append("\n".join(relative))
    names = [n for c in chunks for n in c.names]
    doc = assignment.PART_DOCS[part]
    pieces = [_doc_literal(
        "{0}\n\nsp_live_sync_plugin {1} の __init__.py から、内容を変えずに移した部分。".format(
            doc, source.version))]
    if groups:
        pieces.append("\n\n".join(groups))
    pieces.append(_all_block(names))
    return "\n\n".join(pieces) + "\n\n\n" + join_chunks(chunks)


def render_init(source, chunks, parts_defined, cross, header):
    pieces = [source.docstring]
    # 分割前のトップレベル名(os、time、ui など)を保つため、元の import 文をそのまま持つ。
    plain = []
    painter = []
    for alias, text in source.header_imports:
        (painter if text.startswith("import substance_painter") else plain).append(text)
    plain.extend(INIT_EXTRA_IMPORTS)
    imports = "\n".join(plain) + "\n\n" + "\n".join(painter)
    pieces.append("# 分割前のトップレベル名を保つための import(互換)。\n" + imports)
    lines = [
        "# 部品を最初にまとめて import する(遅延 import はしない)。下の層から順に読む。",
        "# 分割前のトップレベル名はここから参照できる(互換の再エクスポート)。",
    ]
    for part in _part_order(parts_defined):
        names = parts_defined[part]
        if part == PACKAGE_INIT or not names:
            continue
        lines.append(_format_names_import("." + part, names))
    pieces.append("\n".join(lines))
    body = join_chunks(chunks)
    return "\n\n".join(pieces) + "\n\n\n" + body + "\n\n" + RELOAD_PLUGIN_SOURCE


def build(source_text, set_version=None):
    """分割前のファイルの文字列(LF)から {ファイル名: 内容(LF)} を作る。

    set_version を渡すと、__version__ の文字列だけを書き換える(版を上げる唯一の操作)。
    """
    source = Source(source_text)
    if set_version is not None:
        bump_version(source, set_version)
    name_part = assignment.name_to_part()
    assign_chunks(source, name_part)
    cross, header = resolve_dependencies(source, name_part)
    # __init__ は他の部品を import する(下記 render_init)。層の検査は部品どうしだけ。
    check_layers({p: d for p, d in cross.items() if p != PACKAGE_INIT})
    check_pure_parts(cross, header)

    by_part = {}
    for chunk in source.chunks:
        by_part.setdefault(chunk.part, []).append(chunk)
    parts_defined = {}
    for part, chunks in by_part.items():
        parts_defined[part] = [n for c in chunks for n in c.names]

    files = {}
    for part in assignment.LAYERS:
        if part == PACKAGE_INIT:
            continue
        chunks = by_part.get(part)
        if not chunks:
            raise SplitError("部品に名前がありません: " + part)
        files[part + ".py"] = render_part(part, source, chunks, cross, header)
    # __init__ が必要とする他部品の名前は、再エクスポートで足りている
    # (部品の __all__ を全部 import するため)。
    files[PACKAGE_INIT + ".py"] = render_init(
        source, by_part.get(PACKAGE_INIT, []), parts_defined, cross, header)
    return files


def bump_version(source, new_version):
    for chunk in source.chunks:
        if chunk.names == ["__version__"]:
            old = '__version__ = "{0}"'.format(source.version)
            if chunk.text.count(old) != 1:
                raise SplitError("__version__ の行が見つかりません")
            chunk.text = chunk.text.replace(old, '__version__ = "{0}"'.format(new_version))
            return
    raise SplitError("__version__ が見つかりません")


def read_source(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise SplitError("BOM 付きのファイルは想定していません")
    text = raw.decode("utf-8")
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise SplitError("CRLF 以外の復帰文字があります")
    if not text.endswith("\n"):
        text += "\n"
    return text


def write_package(files, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    expected = set(files)
    for name in os.listdir(out_dir):
        if name.endswith(".py") and name not in expected:
            raise SplitError("出力先に、生成物ではない .py があります: " + name)
    for name, text in sorted(files.items()):
        data = text.replace("\n", "\r\n").encode("utf-8")
        with open(os.path.join(out_dir, name), "wb") as handle:
            handle.write(data)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    set_version = None
    if "--set-version" in argv:
        index = argv.index("--set-version")
        set_version = argv[index + 1]
        del argv[index:index + 2]
    if len(argv) != 2:
        print(__doc__)
        return 2
    source_path, out_dir = argv
    try:
        text = read_source(source_path)
        files = build(text, set_version)
        write_package(files, out_dir)
    except SplitError as exc:
        print("エラー: {0}".format(exc))
        return 1
    for name in sorted(files):
        print("{0:<28}{1:>6} 行".format(name, files[name].count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
