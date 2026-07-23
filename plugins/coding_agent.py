"""Repository-aware coding tools for AXIOM.

Registers code-intelligence tools that plug into the existing agent loop
and event stream. The agent already has read_file / edit_file / write_file
(with diff events + confirmation) and git — these tools add *understanding*:
scan structure, index symbols, answer architecture/navigation questions,
plan a task, and run the test suite.

Conventions honoured (see core/tools/__init__.py):
  - tool signature is (ctx, **kwargs) -> str   (ctx is a ToolContext)
  - ctx.notify(text)        streams a `notify` event to every frontend
  - ctx.complete(sys, usr)  one-shot local-model completion
  - keep return values SHORT — every result rides in the model's context
"""

from __future__ import annotations

import os


def _root(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path.strip() or "."))


def register(registry, ctx):
    # Import here so a failure surfaces in the plugin loader, not at import time.
    from core.tools.repo_scanner import RepositoryScanner
    from core.tools.code_index import CodeIndexer
    from core.tools.coding_agent import CodingPlanner, run_tests, detect_test_command

    # Stream tool events onto the AXIOM event bus as notify lines, but only
    # the meaningful start/finish ones (avoid spamming per-file events).
    _RELAYED = {"repo_scan_started", "repo_scan_completed",
                "index_completed", "plan_generated",
                "tests_running", "tests_completed", "file_modified"}

    def relay(event: dict):
        et = event.get("type", "")
        if et not in _RELAYED:
            return
        if et == "repo_scan_started":
            ctx.notify(f"📂 scanning {event.get('path', '')}…")
        elif et == "repo_scan_completed":
            ctx.notify(f"📂 scan done · {event.get('primary_language', '?')} · "
                       f"{event.get('code_files', 0)} code files")
        elif et == "index_completed":
            ctx.notify(f"🧠 indexed {event.get('classes', 0)} classes, "
                       f"{event.get('functions', 0)} functions")
        elif et == "plan_generated":
            ctx.notify("📝 plan ready")
        elif et == "tests_running":
            ctx.notify(f"🧪 running tests ({event.get('framework', '?')})…")
        elif et == "tests_completed":
            ctx.notify("✅ tests passed" if event.get("passed") else "❌ tests failed")

    # ── repository understanding ─────────────────────────────────

    @registry.register(
        "repo_scan",
        "Map a repository: languages, frameworks, build/test systems, entry points, dependencies",
        {"?path": "string: repo root, default cwd"})
    def repo_scan(ctx, path: str = ".") -> str:
        return RepositoryScanner(_root(path), on_event=relay).scan().summary()

    @registry.register(
        "code_index",
        "Index code symbols (classes, functions, methods, API routes, ORM models, imports)",
        {"?path": "string: repo root, default cwd"})
    def code_index(ctx, path: str = ".") -> str:
        return CodeIndexer(_root(path), on_event=relay).build().summary()

    @registry.register(
        "explain_architecture",
        "High-level architecture explanation: stack + structure + key modules, in prose",
        {"?path": "string: repo root, default cwd"})
    def explain_architecture(ctx, path: str = ".") -> str:
        root = _root(path)
        repo = RepositoryScanner(root, on_event=relay).scan()
        index = CodeIndexer(root, on_event=relay).build()
        facts = repo.summary() + "\n\n" + index.summary()
        system = ("You are a senior engineer. Given these repository facts, explain the "
                  "project's architecture in 4-7 sentences: what it is, the stack, how it's "
                  "structured, and the main components. Be concrete and concise.")
        prose = ctx.complete(system, facts)
        return f"{prose}\n\n---\nFacts:\n{facts}"

    @registry.register(
        "find_symbol",
        "Locate a class/function/method by name and show the file:line where it's defined",
        {"name": "string: symbol name (exact or partial)",
         "?path": "string: repo root, default cwd"})
    def find_symbol(ctx, name: str, path: str = ".") -> str:
        if not name.strip():
            return "Provide a symbol name to find."
        index = CodeIndexer(_root(path)).build()
        hits = index.find(name.strip())
        if not hits:
            return f"No symbol matching '{name}' found in {index.files_indexed} files."
        lines = [f"{len(hits)} match(es) for '{name}':"]
        for s in hits[:20]:
            sig = f" — {s.signature}" if s.signature else ""
            lines.append(f"  {s.kind:14} {s.file}:{s.line}  {s.name}{sig}")
        return "\n".join(lines)

    @registry.register(
        "trace_calls",
        "Find call sites of a function across the repo (text search), with file:line and code",
        {"function": "string: function/method name",
         "?path": "string: repo root, default cwd"})
    def trace_calls(ctx, function: str, path: str = ".") -> str:
        import re
        fn = function.strip()
        if not fn:
            return "Provide a function name to trace."
        root = _root(path)
        pat = re.compile(rf"\b{re.escape(fn)}\s*\(")
        skip = {".git", "__pycache__", "node_modules", ".venv", "venv",
                "dist", "build", "target", ".next"}
        exts = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java"}
        hits = []
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
            for name in files:
                if os.path.splitext(name)[1].lower() not in exts:
                    continue
                full = os.path.join(dp, name)
                try:
                    with open(full, encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if pat.search(line):
                                rel = os.path.relpath(full, root).replace("\\", "/")
                                hits.append(f"  {rel}:{i}  {line.strip()[:100]}")
                                if len(hits) >= 40:
                                    break
                except OSError:
                    pass
            if len(hits) >= 40:
                break
        if not hits:
            return f"No call sites of '{fn}(' found."
        head = f"{len(hits)} call site(s) of '{fn}(':" + (" (showing 40)" if len(hits) >= 40 else "")
        return head + "\n" + "\n".join(hits)

    # ── planning + testing (the autonomous loop's missing pieces) ─

    @registry.register(
        "plan_task",
        "Produce a grounded execution plan for a coding task (files to change, steps, tests). "
        "Run this BEFORE editing for non-trivial work.",
        {"task": "string: what to build/fix/refactor",
         "?path": "string: repo root, default cwd"})
    def plan_task(ctx, task: str, path: str = ".") -> str:
        if not task.strip():
            return "Describe the task to plan."
        planner = CodingPlanner(_root(path), complete=ctx.complete, on_event=relay)
        return planner.plan(task.strip()).text()

    @registry.register(
        "run_tests",
        "Detect the test framework and run the suite; returns a pass/fail report",
        {"?path": "string: repo root, default cwd"})
    def run_tests_tool(ctx, path: str = ".") -> str:
        framework, argv = detect_test_command(_root(path))
        if not argv:
            return "No test framework detected (looked for pytest, npm test, go test, cargo test)."
        report = run_tests(_root(path), on_event=relay)
        return report.text()

    print("[plugins] coding tools ready: repo_scan, code_index, explain_architecture, "
          "find_symbol, trace_calls, plan_task, run_tests")
