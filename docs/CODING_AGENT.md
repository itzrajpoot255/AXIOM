# Repository-Aware Coding Tools

Code-intelligence tools that plug into AXIOM's existing agent loop and
event stream. The agent already has `read_file` / `edit_file` /
`write_file` (with diff events + confirmation) and `git`; these tools add
*understanding* — scan structure, index symbols, answer
architecture/navigation questions, plan a task, and run the test suite.

Everything is local. Nothing is sent off-device.

## Modules

| File | Role |
|------|------|
| `core/tools/repo_scanner.py` | One filesystem walk → `RepositoryMap` (languages, frameworks, build/test systems, entry points, deps). |
| `core/tools/code_index.py` | AST for Python, conservative regex for JS/TS → `CodeIndex` (classes, functions, methods, ORM models, API routes, imports). |
| `core/tools/coding_agent.py` | `CodingPlanner` (model-grounded plan) + `run_tests` / `detect_test_command`. |
| `plugins/coding_agent.py` | Registers the tools below against the real `ToolContext`. |

Each tool returns a **short text summary** (not raw JSON) so results stay
cheap in the model's context window. The full structured form is
available via `.to_dict()` for the UI.

## Tools

| Tool | What it does |
|------|--------------|
| `repo_scan [path]` | Languages, frameworks, build/test systems, entry points, dependencies. |
| `code_index [path]` | Counts + key classes, models, routes, most-used imports. |
| `explain_architecture [path]` | Prose architecture explanation grounded in scan + index facts. |
| `find_symbol <name> [path]` | Locate a class/function/method → `file:line` (exact, then fuzzy). |
| `trace_calls <function> [path]` | Call sites across the repo with `file:line` and the line of code. |
| `plan_task <task> [path]` | Grounded execution plan (GOAL / FILES / STEPS / TESTS / RISKS). Run before non-trivial edits. |
| `run_tests [path]` | Auto-detect framework (pytest / npm test / go test / cargo test) and run it; trimmed pass/fail report. |

The agent chains these with its existing file/git tools: `plan_task` →
`edit_file` (diffed, confirmed) → `run_tests` → iterate.

## Events

Tools emit events through `on_event`; the plugin relays the meaningful
ones to the AXIOM event bus as `notify` lines:

```
repo_scan_started / repo_scan_completed
index_started / index_completed
plan_started / plan_generated
tests_running / tests_completed
```

These render in the terminal and web frontends like any other notify.

## AI IDE (web UI)

Open it from the left sidebar (the `</>` icon after the workspace IDE),
or it focuses automatically when relevant. It is a **floating, draggable,
resizable** window with three panes:

- **File tree** — backed by the existing `/api/workspace` endpoints.
- **Code view** — read-only, real syntax highlighting (Python / JS / TS /
  Go / Rust / Java / C++ keyword sets). Click a file in the tree to open it.
  Hit **⚡ suggest** to get real model suggestions injected inline beneath
  the referenced source lines (`/api/ide/suggest`). Hit **edit** to push the
  file into the editable workspace IDE.
- **Coding buddy** — a side chat scoped to the open file
  (`/api/ide/analyze`); answers render as markdown and never leak into the
  main conversation.

Status bar shows ready/thinking state, line count, and detected language.

### Backend endpoints

```
POST /api/ide/analyze   {question, path, code} -> {answer}
POST /api/ide/suggest   {path, code}           -> {suggestions:[{line,text}], raw}
```

Both call the local model via `assistant.quick_complete(..., role="code")`
and trim the code to 12 KB so context never overflows.

## Notes & limits

- JS/TS indexing is regex-based (navigation aid, not a full parser).
- `run_tests` shells out; it respects the detected framework only.
- `code_index` skips the usual vendored dirs (`node_modules`, `.venv`,
  `dist`, `build`, `target`, …).
- ORM-model detection keys on base-class leaf names
  (`Model`, `Base`, `BaseModel`, `Document`, `Schema`, …), so generic
  `BaseX` classes are not misclassified.
