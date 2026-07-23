"""Development tools — git and repository inspection."""

from __future__ import annotations

import os
import shutil
import subprocess

_GIT_SAFE = {"status", "log", "diff", "branch", "show", "add", "commit", "push",
             "pull", "fetch", "stash", "remote", "rev-parse", "blame", "checkout",
             "switch", "restore", "merge", "rebase", "tag", "describe", "ls-files",
             "clone", "init", "reset", "cherry-pick", "reflog"}

_GH_SAFE = {"repo", "pr", "issue", "release", "gist", "auth", "api",
            "browse", "status", "search", "label", "variable"}

_PROJECT_MARKERS = {
    "package.json": "node", "pyproject.toml": "python", "requirements.txt": "python",
    "Cargo.toml": "rust", "go.mod": "go", "composer.json": "php",
    "pom.xml": "java/maven", "build.gradle": "java/gradle", "Gemfile": "ruby",
    "CMakeLists.txt": "c/c++", "platformio.ini": "embedded/platformio",
}

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
              "build", ".next", "vendor", ".idea"}


def _fix_win_quotes(cmd: str) -> str:
    """Convert single-quoted segments to double-quoted for Windows cmd.exe.

    git log --pretty=format:'%h %s'  ->  git log --pretty=format:"%h %s"
    Windows cmd.exe does not treat single quotes as string delimiters,
    so they become literal characters and break arguments.
    """
    if os.name != "nt":
        return cmd
    import re
    return re.sub(r"'([^']*)'", r'"\1"', cmd)


def _run_git(args: str, repo: str) -> str:
    repo = os.path.abspath(os.path.expanduser(repo or "."))
    if not os.path.isdir(repo):
        return f"Invalid repo path: {repo}"
    sub = args.strip().split()
    if not sub:
        return "Empty git command."
    if sub[0] not in _GIT_SAFE:
        return (f"git {sub[0]} is not in the allowed set "
                f"({', '.join(sorted(_GIT_SAFE))}). Use run_command if you must.")
    cmd = _fix_win_quotes(f"git {args}")
    try:
        result = subprocess.run(cmd, shell=True, cwd=repo,
                                capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return f"git {args} timed out."
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    if len(out) > 10000:
        out = out[:10000] + "\n...[truncated]"
    return out or f"(no output, exit {result.returncode})"


def _run_gh(args: str, cwd: str = ".") -> str:
    """Run a GitHub CLI command. Returns output or install instructions."""
    if not shutil.which("gh"):
        return ("GitHub CLI (gh) is not installed.\n"
                "Install it with: winget install GitHub.cli\n"
                "Then authenticate: gh auth login")
    sub = args.strip().split()
    if not sub:
        return "Empty gh command."
    if sub[0] not in _GH_SAFE:
        return (f"gh {sub[0]} is not in the allowed set "
                f"({', '.join(sorted(_GH_SAFE))}). Use run_command if you must.")
    cwd = os.path.abspath(os.path.expanduser(cwd or "."))
    cmd = _fix_win_quotes(f"gh {args}")
    try:
        result = subprocess.run(cmd, shell=True, cwd=cwd,
                                capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return f"gh {args} timed out."
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    if len(out) > 10000:
        out = out[:10000] + "\n...[truncated]"
    return out or f"(no output, exit {result.returncode})"


def register(r) -> None:

    @r.register("git", "Run a git command (status/log/diff/commit/push/pull/clone/...)",
                {"args": "string: e.g. 'status --short' or 'commit -m \"msg\"'",
                 "?repo": "string: repository path, default cwd"})
    def git(ctx, args: str, repo: str = ".") -> str:
        return _run_git(args, repo)

    @r.register("github", "Run a GitHub CLI (gh) command — repos, PRs, issues, releases",
                {"args": "string: e.g. 'repo list' or 'pr create --fill'",
                 "?cwd": "string: working directory, default cwd"})
    def github(ctx, args: str, cwd: str = ".") -> str:
        return _run_gh(args, cwd)

    @r.register("github_repos", "List the authenticated user's GitHub repositories",
                {"?limit": "integer: max repos to show, default 20",
                 "?owner": "string: GitHub username or org (default: your account)"})
    def github_repos(ctx, limit: int = 20, owner: str = "") -> str:
        limit = max(1, min(100, int(limit or 20)))
        target = owner.strip()
        if target:
            return _run_gh(f"repo list {target} --limit {limit}")
        return _run_gh(f"repo list --limit {limit}")

    @r.register("github_clone", "Clone a GitHub repository",
                {"repo": "string: owner/repo or just repo name",
                 "?dest": "string: destination directory"})
    def github_clone(ctx, repo: str, dest: str = "") -> str:
        repo = repo.strip()
        if not repo:
            return "No repo specified."
        cmd = f"repo clone {repo}"
        if dest.strip():
            cmd += f" {dest.strip()}"
        return _run_gh(cmd)

    @r.register("repo_map", "Overview of a code repository: tree, project type, git state",
                {"?path": "string: repo root, default cwd",
                 "?depth": "integer: tree depth, default 2"})
    def repo_map(ctx, path: str = ".", depth: int = 2) -> str:
        root = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(root):
            return f"Not a directory: {root}"
        depth = max(1, min(4, int(depth or 2)))

        kinds = [k for marker, k in _PROJECT_MARKERS.items()
                 if os.path.exists(os.path.join(root, marker))]
        lines = [f"Repository: {root}",
                 f"Type: {', '.join(kinds) if kinds else 'unknown'}"]

        if os.path.isdir(os.path.join(root, ".git")):
            branch = _run_git("rev-parse --abbrev-ref HEAD", root)
            status = _run_git("status --short", root)
            recent = _run_git("log --oneline -5", root)
            dirty = len(status.splitlines()) if "(no output" not in status else 0
            lines.append(f"Git: branch {branch.strip()}, {dirty} changed file(s)")
            lines.append("Recent commits:\n" + "\n".join(
                f"  {l}" for l in recent.splitlines()[:5]))

        lines.append("Tree:")
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            level = 0 if rel == "." else rel.count(os.sep) + 1
            if level >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            indent = "  " * (level + 1)
            if rel != ".":
                lines.append(f"{indent[:-2]}{os.path.basename(dirpath)}/")
            for fn in sorted(filenames)[:30]:
                lines.append(f"{indent}{fn}")
                count += 1
                if count > 150:
                    lines.append(f"{indent}...")
                    return "\n".join(lines)
        return "\n".join(lines)

