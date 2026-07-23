"""Code intelligence indexer — AST for Python, regex for JS/TS.

Builds a queryable index of a repository's symbols:
  classes, functions, methods, imports, API routes, ORM models.

Python is parsed with the `ast` module (accurate). JS/TS is parsed with
conservative regexes (good enough for navigation, never throws). The
resulting `CodeIndex` renders either a compact summary or a full dict,
and supports `find()` / `routes_summary()` for the query tools.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional


_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".pytest_cache", ".mypy_cache", "target", ".next",
    "out", "bin", "obj", "vendor", "coverage", ".idea", ".vscode",
}

_PY_ROUTE_DECOS = re.compile(r"\b(get|post|put|patch|delete|route|websocket)\b", re.I)
# A class is an ORM/data model only if a base's *leaf* name is one of these —
# matched exactly so "BaseProvider" / "BaseModelManager" don't false-positive.
_MODEL_BASES = {"Model", "BaseModel", "Base", "Document", "Schema", "Table", "DeclarativeBase"}


def _is_model_base(base: str) -> bool:
    return base.split(".")[-1] in _MODEL_BASES

# JS/TS regexes (conservative; navigation aid, not a parser)
_JS_CLASS = re.compile(r"\b(?:export\s+)?(?:abstract\s+)?(?:class|interface)\s+([A-Za-z_$][\w$]*)")
_JS_FUNC = re.compile(
    r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"          # function foo
    r"|\b(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"  # const foo = () =>
)
_JS_IMPORT = re.compile(r"""import\s+(?:.+?\s+from\s+)?['"]([^'"]+)['"]""")
_JS_ROUTE = re.compile(
    r"""\b(?:app|router|api)\.(get|post|put|patch|delete|use)\s*\(\s*['"`]([^'"`]+)['"`]""", re.I)


@dataclass
class Symbol:
    name: str
    kind: str            # class | function | method | async_function | model
    file: str
    line: int
    signature: str = ""
    docstring: str = ""
    decorators: list[str] = field(default_factory=list)


@dataclass
class Route:
    method: str
    path: str
    handler: str
    file: str
    line: int


@dataclass
class CodeIndex:
    root_path: str
    classes: list[Symbol] = field(default_factory=list)
    functions: list[Symbol] = field(default_factory=list)
    methods: list[Symbol] = field(default_factory=list)
    models: list[Symbol] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    imports: dict[str, int] = field(default_factory=dict)     # module -> count
    files_indexed: int = 0
    parse_errors: int = 0

    # ── queries used by the tools ────────────────────────────────

    def all_symbols(self):
        return self.classes + self.functions + self.methods + self.models

    def find(self, name: str) -> list[Symbol]:
        name_l = name.lower()
        exact = [s for s in self.all_symbols() if s.name.lower() == name_l]
        if exact:
            return exact
        return [s for s in self.all_symbols() if name_l in s.name.lower()][:20]

    def to_dict(self) -> dict:
        return {
            "root_path": self.root_path,
            "counts": {
                "classes": len(self.classes), "functions": len(self.functions),
                "methods": len(self.methods), "models": len(self.models),
                "routes": len(self.routes), "imports": len(self.imports),
            },
            "classes": [asdict(s) for s in self.classes],
            "functions": [asdict(s) for s in self.functions],
            "models": [asdict(s) for s in self.models],
            "routes": [asdict(r) for r in self.routes],
            "top_imports": sorted(self.imports.items(), key=lambda kv: -kv[1])[:25],
            "files_indexed": self.files_indexed,
            "parse_errors": self.parse_errors,
        }

    def summary(self) -> str:
        top_imports = ", ".join(
            m for m, _ in sorted(self.imports.items(), key=lambda kv: -kv[1])[:10]) or "none"
        lines = [
            f"Code index for {self.root_path}",
            f"Indexed {self.files_indexed} files "
            f"({self.parse_errors} parse errors).",
            f"Classes: {len(self.classes)} · Functions: {len(self.functions)} · "
            f"Methods: {len(self.methods)} · Models: {len(self.models)} · "
            f"Routes: {len(self.routes)}",
        ]
        if self.classes:
            names = ", ".join(s.name for s in self.classes[:15])
            lines.append(f"Key classes: {names}")
        if self.models:
            names = ", ".join(s.name for s in self.models[:15])
            lines.append(f"Models: {names}")
        if self.routes:
            lines.append(f"API routes: {len(self.routes)} (e.g. " + ", ".join(
                f"{r.method.upper()} {r.path}" for r in self.routes[:6]) + ")")
        lines.append(f"Most-used imports: {top_imports}")
        return "\n".join(lines)


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str, index: CodeIndex):
        self.file = rel_path
        self.index = index

    @staticmethod
    def _decorators(node) -> list[str]:
        out = []
        for d in node.decorator_list:
            try:
                out.append(ast.unparse(d))
            except Exception:
                out.append(getattr(d, "id", "?"))
        return out

    @staticmethod
    def _signature(node) -> str:
        try:
            args = ast.unparse(node.args)
        except Exception:
            args = ", ".join(a.arg for a in node.args.args)
        return f"{node.name}({args})"

    def visit_ClassDef(self, node):
        bases = []
        for b in node.bases:
            try:
                bases.append(ast.unparse(b))
            except Exception:
                pass
        is_model = any(_is_model_base(b) for b in bases)
        sym = Symbol(
            name=node.name, kind="model" if is_model else "class",
            file=self.file, line=node.lineno,
            signature=f"{node.name}({', '.join(bases)})" if bases else node.name,
            docstring=(ast.get_docstring(node) or "")[:200],
            decorators=self._decorators(node),
        )
        (self.index.models if is_model else self.index.classes).append(sym)

        # methods + routes declared on methods
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.index.methods.append(Symbol(
                    name=f"{node.name}.{item.name}", kind="method",
                    file=self.file, line=item.lineno,
                    signature=self._signature(item),
                    docstring=(ast.get_docstring(item) or "")[:160],
                    decorators=self._decorators(item)))
                self._maybe_route(item, handler=f"{node.name}.{item.name}")
        # don't generic_visit into the class again for funcs (handled above)

    def visit_FunctionDef(self, node):
        self._function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node):
        self._function(node, is_async=True)

    def _function(self, node, is_async: bool):
        self.index.functions.append(Symbol(
            name=node.name, kind="async_function" if is_async else "function",
            file=self.file, line=node.lineno,
            signature=self._signature(node),
            docstring=(ast.get_docstring(node) or "")[:160],
            decorators=self._decorators(node)))
        self._maybe_route(node, handler=node.name)

    def _maybe_route(self, node, handler: str):
        for d in node.decorator_list:
            try:
                text = ast.unparse(d)
            except Exception:
                continue
            m = _PY_ROUTE_DECOS.search(text)
            if not m:
                continue
            method = m.group(1).lower()
            path_m = re.search(r"""['"]([^'"]+)['"]""", text)
            self.index.routes.append(Route(
                method=method, path=path_m.group(1) if path_m else "",
                handler=handler, file=self.file, line=node.lineno))

    def visit_Import(self, node):
        for alias in node.names:
            root = alias.name.split(".")[0]
            self.index.imports[root] = self.index.imports.get(root, 0) + 1

    def visit_ImportFrom(self, node):
        if node.module:
            root = node.module.split(".")[0]
            self.index.imports[root] = self.index.imports.get(root, 0) + 1


class CodeIndexer:
    def __init__(self, root_path: str, on_event: Optional[Callable[[dict], None]] = None):
        self.root_path = os.path.abspath(os.path.expanduser(root_path or "."))
        self.on_event = on_event or (lambda e: None)
        self.index = CodeIndex(root_path=self.root_path)

    def _emit(self, event_type: str, **data):
        self.on_event({"type": event_type, **data})

    def build(self) -> CodeIndex:
        self._emit("index_started", path=self.root_path)
        try:
            for dirpath, dirnames, filenames in os.walk(self.root_path):
                dirnames[:] = [d for d in dirnames
                               if d not in _SKIP_DIRS and not d.startswith(".")]
                for name in filenames:
                    ext = os.path.splitext(name)[1].lower()
                    full = os.path.join(dirpath, name)
                    if ext in (".py", ".pyi"):
                        self._index_python(full)
                    elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
                        self._index_js(full)
            self._emit("index_completed", **self.index.to_dict()["counts"])
        except Exception as e:
            self._emit("index_error", error=f"{type(e).__name__}: {e}")
            raise
        return self.index

    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self.root_path).replace("\\", "/")

    def _index_python(self, path: str):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                source = f.read()
        except OSError:
            return
        self.index.files_indexed += 1
        try:
            tree = ast.parse(source)
        except SyntaxError:
            self.index.parse_errors += 1
            return
        _PyVisitor(self._rel(path), self.index).visit(tree)

    def _index_js(self, path: str):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            return
        self.index.files_indexed += 1
        rel = self._rel(path)

        def line_of(pos: int) -> int:
            return content.count("\n", 0, pos) + 1

        for m in _JS_CLASS.finditer(content):
            self.index.classes.append(Symbol(
                name=m.group(1), kind="class", file=rel, line=line_of(m.start())))
        for m in _JS_FUNC.finditer(content):
            name = m.group(1) or m.group(2)
            if name:
                self.index.functions.append(Symbol(
                    name=name, kind="function", file=rel, line=line_of(m.start())))
        for m in _JS_IMPORT.finditer(content):
            root = m.group(1)
            self.index.imports[root] = self.index.imports.get(root, 0) + 1
        for m in _JS_ROUTE.finditer(content):
            self.index.routes.append(Route(
                method=m.group(1).lower(), path=m.group(2), handler="",
                file=rel, line=line_of(m.start())))
