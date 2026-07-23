"""Autonomous coding planner + test runner.

This module is deliberately honest about what it does. Editing source
safely is the *agent's* job (it already has read_file / edit_file /
write_file with diff events and confirmation). This module gives the
agent the two things it lacks:

  1. a grounded **plan** — repo facts + code index fed to the model so
     it proposes concrete files and steps before touching anything;
  2. a **test runner** — auto-detects the framework, runs it, returns a
     trimmed pass/fail report the agent can act on.

It does NOT fake edits. The plan names files; the agent edits them.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional

from .repo_scanner import RepositoryScanner
from .code_index import CodeIndexer


_PLAN_SYSTEM = """You are the planning module of a coding agent. Given \
repository facts and a task, produce a SHORT, concrete execution plan.

Output exactly these sections, nothing else:
GOAL: one sentence restating the task.
FILES: bullet list of the specific files most likely to change (use real \
paths from the repo facts; if unsure, name the best candidates).
STEPS: numbered, concrete edits to make — short imperative steps.
TESTS: how to verify (which test command / what to check).
RISKS: one line on the main risk, or "low".

Be terse. No preamble, no markdown headers beyond the labels above."""


@dataclass
class TestReport:
    framework: str
    ran: bool
    passed: bool
    summary: str
    output_tail: str = ""

    def text(self) -> str:
        if not self.ran:
            return f"Tests: not run ({self.summary})."
        status = "PASSED" if self.passed else "FAILED"
        out = [f"Tests ({self.framework}): {status} — {self.summary}"]
        if self.output_tail:
            out.append(self.output_tail)
        return "\n".join(out)


def detect_test_command(root: str) -> tuple[str, list[str]]:
    """Return (framework_label, argv) or ("", []) if none detected."""
    root = os.path.abspath(os.path.expanduser(root or "."))

    def has(name: str) -> bool:
        return os.path.exists(os.path.join(root, name))

    py = "python"
    if has("pytest.ini") or has("conftest.py") or has("tox.ini") or \
       os.path.isdir(os.path.join(root, "tests")):
        return "pytest", [py, "-m", "pytest", "-q", "--no-header"]
    if has("package.json"):
        try:
            import json
            with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
                pkg = json.load(f)
            if "test" in pkg.get("scripts", {}):
                return "npm test", ["npm", "test", "--silent"]
        except Exception:
            pass
    if has("go.mod"):
        return "go test", ["go", "test", "./..."]
    if has("Cargo.toml"):
        return "cargo test", ["cargo", "test", "--quiet"]
    return "", []


def run_tests(root: str, timeout: int = 120,
              on_event: Optional[Callable[[dict], None]] = None) -> TestReport:
    emit = on_event or (lambda e: None)
    root = os.path.abspath(os.path.expanduser(root or "."))
    framework, argv = detect_test_command(root)
    if not argv:
        emit({"type": "tests_running", "framework": "none"})
        return TestReport("none", False, False, "no test framework detected")

    emit({"type": "tests_running", "framework": framework})
    try:
        proc = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                              timeout=timeout, shell=(os.name == "nt"))
    except FileNotFoundError:
        return TestReport(framework, False, False,
                          f"runner not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return TestReport(framework, True, False, f"timed out after {timeout}s")

    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    tail = "\n".join(output.splitlines()[-25:])
    passed = proc.returncode == 0
    # try to surface the pytest/jest summary line
    summary_line = ""
    for line in reversed(output.splitlines()):
        if any(k in line.lower() for k in ("passed", "failed", "error", "ok", "test")):
            summary_line = line.strip()
            break
    report = TestReport(framework, True, passed,
                        summary_line or ("exit 0" if passed else f"exit {proc.returncode}"),
                        tail)
    emit({"type": "tests_completed", "passed": passed, "framework": framework})
    return report


@dataclass
class Plan:
    task: str
    repo_summary: str
    code_summary: str
    plan_text: str

    def text(self) -> str:
        return self.plan_text


class CodingPlanner:
    """Builds a grounded plan by feeding repo facts to the model."""

    def __init__(self, root: str,
                 complete: Callable[..., str],
                 on_event: Optional[Callable[[dict], None]] = None):
        self.root = os.path.abspath(os.path.expanduser(root or "."))
        self.complete = complete
        self.on_event = on_event or (lambda e: None)

    def _emit(self, event_type: str, **data):
        self.on_event({"type": event_type, **data})

    def plan(self, task: str) -> Plan:
        self._emit("plan_started", task=task)

        repo = RepositoryScanner(self.root, on_event=self.on_event).scan()
        index = CodeIndexer(self.root, on_event=self.on_event).build()

        repo_summary = repo.summary()
        code_summary = index.summary()

        user = (f"TASK: {task}\n\n"
                f"REPOSITORY FACTS:\n{repo_summary}\n\n"
                f"CODE INDEX:\n{code_summary}\n\n"
                f"Top-level entry points: {', '.join(repo.entry_points[:6]) or 'unknown'}")

        plan_text = self.complete(_PLAN_SYSTEM, user) or "(planner returned nothing)"
        self._emit("plan_generated", task=task,
                   files_hint=[s.file for s in index.classes[:8]])
        return Plan(task, repo_summary, code_summary, plan_text.strip())
