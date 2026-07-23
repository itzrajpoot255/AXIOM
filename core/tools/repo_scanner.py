"""Repository scanner — builds a repo map and identifies the tech stack.

A single filesystem walk classifies every file, then cheap marker-file
checks identify frameworks, build systems, test runners and entry points.
Returns a `RepositoryMap` that can render either a compact human summary
(for the agent / chat) or a full dict (for the IDE UI).

Designed for the AXIOM event-driven runtime: pass an `on_event`
callback to stream `repo_scan_started` / `repo_scan_completed` events.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional


# Extension -> language. One source of truth for counting + detection.
_EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".cs": "csharp", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".c": "c", ".h": "c", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".scala": "scala",
    ".sh": "shell", ".bash": "shell", ".sql": "sql", ".lua": "lua", ".r": "r",
}

# Marker file -> build system.
_BUILD_MARKERS = {
    "Makefile": "make", "makefile": "make", "GNUmakefile": "make",
    "CMakeLists.txt": "cmake",
    "build.gradle": "gradle", "build.gradle.kts": "gradle", "settings.gradle": "gradle",
    "pom.xml": "maven",
    "package.json": "npm/yarn/pnpm",
    "Cargo.toml": "cargo",
    "go.mod": "go modules",
    "pyproject.toml": "python (pep517)", "setup.py": "setuptools",
    "Pipfile": "pipenv", "poetry.lock": "poetry",
    "composer.json": "composer", "Gemfile": "bundler",
    "Dockerfile": "docker", "docker-compose.yml": "docker-compose",
}

# Substring (in a dependency manifest) -> framework label.
_FRAMEWORK_HINTS = {
    # python
    "django": "django", "flask": "flask", "fastapi": "fastapi",
    "sqlalchemy": "sqlalchemy", "pydantic": "pydantic", "pytest": "pytest",
    "uvicorn": "uvicorn", "celery": "celery", "torch": "pytorch",
    "tensorflow": "tensorflow", "numpy": "numpy", "pandas": "pandas",
    # js/ts
    "react": "react", "next": "next.js", "vue": "vue", "@angular/core": "angular",
    "svelte": "svelte", "express": "express", "nestjs": "nestjs",
    "@nestjs/core": "nestjs", "jest": "jest", "vitest": "vitest",
    "webpack": "webpack", "vite": "vite", "tailwindcss": "tailwind",
    # go / rust
    "gin-gonic": "gin", "labstack/echo": "echo", "fiber": "fiber",
    "actix-web": "actix", "tokio": "tokio", "axum": "axum", "rocket": "rocket",
}

# Common entry-point filenames, scored by how strong a signal they are.
_ENTRY_NAMES = {
    "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "__main__.py",
    "index.js", "index.ts", "server.js", "server.ts", "app.js", "app.ts",
    "main.go", "main.rs", "Main.java", "Program.cs", "index.php",
}

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "target", ".next", "out", "bin", "obj", ".vscode", ".idea",
    "vendor", ".gradle", "coverage", ".nuxt", ".svelte-kit", "__snapshots__",
}

_CONFIG_NAMES = {
    ".env", ".env.example", ".env.local", ".gitignore", ".dockerignore",
    "tsconfig.json", "jsconfig.json", ".eslintrc", ".eslintrc.js",
    ".eslintrc.json", ".prettierrc", "babel.config.js", "setup.cfg",
    "tox.ini", "pytest.ini", ".editorconfig", "Dockerfile",
    "docker-compose.yml", ".pre-commit-config.yaml",
}


@dataclass
class RepositoryMap:
    """Complete repository snapshot."""
    root_path: str
    languages: dict[str, int] = field(default_factory=dict)      # lang -> file count
    frameworks: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    build_systems: list[str] = field(default_factory=list)
    test_frameworks: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    key_directories: dict[str, str] = field(default_factory=dict)
    total_files: int = 0
    code_files: int = 0
    total_lines: int = 0

    def primary_language(self) -> str:
        if not self.languages:
            return "unknown"
        return max(self.languages.items(), key=lambda kv: kv[1])[0]

    def to_dict(self) -> dict:
        return {
            "root_path": self.root_path,
            "primary_language": self.primary_language(),
            "languages": self.languages,
            "frameworks": self.frameworks,
            "entry_points": self.entry_points,
            "dependencies": self.dependencies,
            "build_systems": self.build_systems,
            "test_frameworks": self.test_frameworks,
            "config_files": self.config_files,
            "key_directories": self.key_directories,
            "total_files": self.total_files,
            "code_files": self.code_files,
            "total_lines": self.total_lines,
        }

    def summary(self) -> str:
        """Compact, token-cheap text summary for the model / chat."""
        langs = ", ".join(f"{k} ({v})" for k, v in
                          sorted(self.languages.items(), key=lambda kv: -kv[1])[:5]) or "none detected"
        lines = [
            f"Repository: {self.root_path}",
            f"Primary language: {self.primary_language()}",
            f"Languages: {langs}",
            f"Frameworks: {', '.join(self.frameworks) or 'none detected'}",
            f"Build systems: {', '.join(self.build_systems) or 'none'}",
            f"Test frameworks: {', '.join(self.test_frameworks) or 'none'}",
            f"Entry points: {', '.join(self.entry_points[:6]) or 'none found'}",
            f"Size: {self.code_files} code files, ~{self.total_lines:,} lines "
            f"({self.total_files} files total)",
        ]
        if self.key_directories:
            kd = ", ".join(f"{k}={v}" for k, v in self.key_directories.items())
            lines.append(f"Key directories: {kd}")
        if self.dependencies:
            for manifest, deps in self.dependencies.items():
                shown = ", ".join(deps[:12])
                more = f" (+{len(deps) - 12} more)" if len(deps) > 12 else ""
                lines.append(f"Deps [{manifest}]: {shown}{more}")
        return "\n".join(lines)


class RepositoryScanner:
    def __init__(self, root_path: str, on_event: Optional[Callable[[dict], None]] = None):
        self.root_path = os.path.abspath(os.path.expanduser(root_path or "."))
        self.on_event = on_event or (lambda e: None)
        self.repo_map = RepositoryMap(root_path=self.root_path)
        self._dir_names: set[str] = set()
        self._root_files: set[str] = set()

    def _emit(self, event_type: str, **data):
        self.on_event({"type": event_type, **data})

    def scan(self) -> RepositoryMap:
        self._emit("repo_scan_started", path=self.root_path)
        try:
            self._walk()              # single pass: counts, langs, entries, configs
            self._detect_build_and_tests()
            self._detect_frameworks_and_deps()
            self._pick_key_directories()
            self._emit("repo_scan_completed", **self.repo_map.to_dict())
        except Exception as e:
            self._emit("repo_scan_error", error=f"{type(e).__name__}: {e}")
            raise
        return self.repo_map

    # ── one filesystem walk does most of the work ────────────────

    def _walk(self):
        rm = self.repo_map
        test_signal = False
        for dirpath, dirnames, filenames in os.walk(self.root_path):
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP_DIRS and not d.startswith(".")]
            for d in dirnames:
                self._dir_names.add(d.lower())

            at_root = (dirpath == self.root_path)
            for name in filenames:
                rm.total_files += 1
                if at_root:
                    self._root_files.add(name)
                if name in _CONFIG_NAMES:
                    rm.config_files.append(name)

                ext = os.path.splitext(name)[1].lower()
                lang = _EXT_LANG.get(ext)
                if lang:
                    rm.code_files += 1
                    rm.languages[lang] = rm.languages.get(lang, 0) + 1
                    rm.total_lines += self._count_lines(os.path.join(dirpath, name))

                # entry points (skip ones buried in tests)
                if name in _ENTRY_NAMES and "test" not in dirpath.lower():
                    rel = os.path.relpath(os.path.join(dirpath, name), self.root_path)
                    rm.entry_points.append(rel.replace("\\", "/"))

                # test signal: pytest/unittest style filenames, *_test.go, *.test.js
                low = name.lower()
                if (low.startswith("test_") or low.endswith("_test.py")
                        or low.endswith("_test.go") or ".test." in low
                        or ".spec." in low):
                    test_signal = True

        # Root-level Python scripts with a __main__ guard are entry points too.
        for name in self._root_files:
            if name.endswith(".py"):
                try:
                    with open(os.path.join(self.root_path, name),
                              encoding="utf-8", errors="ignore") as f:
                        if '__name__' in f.read(8000):
                            rm.entry_points.append(name)
                except OSError:
                    pass

        rm.config_files = sorted(set(rm.config_files))
        rm.entry_points = sorted(set(rm.entry_points))
        self._test_filename_signal = test_signal

    @staticmethod
    def _count_lines(path: str) -> int:
        try:
            with open(path, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    # ── marker-file driven detection (cheap, no extra walks) ─────

    def _detect_build_and_tests(self):
        rm = self.repo_map
        builds: list[str] = []
        for marker, system in _BUILD_MARKERS.items():
            if marker in self._root_files and system not in builds:
                builds.append(system)
        # requirements.txt implies pip even without a build backend.
        if "requirements.txt" in self._root_files and not any(
                b.startswith(("python", "poetry", "pipenv")) for b in builds):
            builds.append("pip (requirements.txt)")
        rm.build_systems = builds

        tests: list[str] = []
        if "pytest.ini" in self._root_files or "tox.ini" in self._root_files:
            tests.append("pytest")
        if "conftest.py" in self._root_files:
            tests.append("pytest")
        if self._test_filename_signal and "python" in rm.languages and "pytest" not in tests:
            tests.append("pytest/unittest")
        if "go.mod" in self._root_files and any(
                f.endswith("_test.go") for f in self._root_files) or (
                "go" in rm.languages and self._test_filename_signal):
            tests.append("go test")
        if "Cargo.toml" in self._root_files:
            tests.append("cargo test")
        # jest/vitest/mocha resolved from package.json in deps step
        rm.test_frameworks = sorted(set(tests))

    def _detect_frameworks_and_deps(self):
        rm = self.repo_map
        frameworks: set[str] = set()

        # package.json — deps + js test runners + frameworks
        pkg = os.path.join(self.root_path, "package.json")
        if os.path.isfile(pkg):
            try:
                with open(pkg, encoding="utf-8") as f:
                    data = json.load(f)
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                rm.dependencies["package.json"] = sorted(deps.keys())
                blob = " ".join(deps.keys()).lower()
                for hint, label in _FRAMEWORK_HINTS.items():
                    if hint in blob:
                        frameworks.add(label)
                for runner in ("jest", "vitest", "mocha", "@playwright/test", "cypress"):
                    if runner in deps:
                        rm.test_frameworks.append(runner.split("/")[-1])
                scripts = data.get("scripts", {})
                if scripts:
                    rm.dependencies["package.json scripts"] = sorted(scripts.keys())
            except (OSError, json.JSONDecodeError, ValueError):
                pass

        # python manifests
        for manifest in ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py"):
            p = os.path.join(self.root_path, manifest)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            low = text.lower()
            for hint, label in _FRAMEWORK_HINTS.items():
                if hint in low:
                    frameworks.add(label)
            if manifest == "requirements.txt":
                names = []
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        names.append(__import__("re").split(r"[=<>!~ \[]", line)[0])
                if names:
                    rm.dependencies["requirements.txt"] = names

        # go.mod
        gomod = os.path.join(self.root_path, "go.mod")
        if os.path.isfile(gomod):
            try:
                with open(gomod, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                for hint, label in _FRAMEWORK_HINTS.items():
                    if hint in text:
                        frameworks.add(label)
            except OSError:
                pass

        # Cargo.toml
        cargo = os.path.join(self.root_path, "Cargo.toml")
        if os.path.isfile(cargo):
            try:
                with open(cargo, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                for hint, label in _FRAMEWORK_HINTS.items():
                    if hint in text:
                        frameworks.add(label)
            except OSError:
                pass

        rm.frameworks = sorted(frameworks)
        rm.test_frameworks = sorted(set(rm.test_frameworks))

    def _pick_key_directories(self):
        rm = self.repo_map
        wanted = {
            "source": ["src", "lib", "app", "core"],
            "tests": ["tests", "test", "__tests__", "spec"],
            "docs": ["docs", "doc", "documentation"],
            "config": ["config", "conf", "settings"],
        }
        for kind, candidates in wanted.items():
            for cand in candidates:
                if cand in self._dir_names:
                    rm.key_directories[kind] = cand
                    break
