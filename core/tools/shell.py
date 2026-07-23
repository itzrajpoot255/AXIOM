"""Shell and application tools."""

from __future__ import annotations

import os
import shutil
import subprocess

_APP_ALIASES = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "cmd": "cmd.exe", "powershell": "powershell.exe",
    "terminal": "wt.exe", "explorer": "explorer.exe", "files": "explorer.exe",
    "task manager": "taskmgr.exe", "vs code": "code", "vscode": "code",
    "code": "code", "chrome": "chrome.exe", "edge": "msedge.exe",
    "firefox": "firefox.exe", "word": "winword.exe", "excel": "excel.exe",
}

_KILL_ALIASES = {
    "chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe",
    "vscode": "Code.exe", "vs code": "Code.exe", "code": "Code.exe",
    "notepad": "notepad.exe", "calculator": "CalculatorApp.exe",
    "terminal": "WindowsTerminal.exe", "word": "winword.exe", "excel": "excel.exe",
}


def register(r) -> None:

    @r.register("run_command", "Run a shell command and return its output",
                {"cmd": "string: the command",
                 "?cwd": "string: working directory",
                 "?timeout": "integer: seconds, default 60"},
                needs_confirm=bool(r.ctx.cfg and r.ctx.cfg.confirm_shell_commands))
    def run_command(ctx, cmd: str, cwd: str = "", timeout: int = 60) -> str:
        if not cmd.strip():
            return "Empty command."
        workdir = os.path.abspath(os.path.expanduser(cwd)) if cwd else os.getcwd()
        if not os.path.isdir(workdir):
            return f"Invalid cwd: {workdir}"
        try:
            t = max(5, min(600, int(timeout)))
        except (TypeError, ValueError):
            t = 60
        try:
            result = subprocess.run(cmd, shell=True, cwd=workdir,
                                    capture_output=True, text=True, timeout=t)
        except subprocess.TimeoutExpired:
            return f"Timed out after {t}s: {cmd}"
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        parts = [p for p in (out, err and f"[stderr] {err}") if p]
        body = "\n".join(parts) or f"(no output, exit {result.returncode})"
        if result.returncode != 0:
            body += f"\n[exit code {result.returncode}]"
        if len(body) > 12000:
            body = body[:12000] + "\n...[truncated]"
        return body

    @r.register("open_app", "Launch an application or open a file/folder/URL with its default app",
                {"target": "string: app name, file path, or URL"})
    def open_app(ctx, target: str) -> str:
        target = target.strip()
        if not target:
            return "Nothing to open."
        exe = _APP_ALIASES.get(target.lower())
        if exe:
            resolved = shutil.which(exe) or exe
            try:
                subprocess.Popen([resolved], shell=False,
                                 creationflags=subprocess.DETACHED_PROCESS)
                return f"Launched {target}."
            except OSError:
                pass
        # Fall back to the OS shell association (files, folders, URLs, store apps).
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return f"Opened {target}."
        except OSError:
            try:
                subprocess.Popen(f'start "" "{target}"', shell=True)
                return f"Asked Windows to open {target}."
            except OSError as e:
                return f"Could not open {target}: {e}"

    @r.register("close_app", "Force-close an application by name",
                {"name": "string: app name, e.g. notepad"})
    def close_app(ctx, name: str) -> str:
        target = _KILL_ALIASES.get(name.strip().lower(), name.strip())
        if not target.lower().endswith(".exe"):
            target += ".exe"
        result = subprocess.run(f'taskkill /IM "{target}" /F', shell=True,
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return f"Closed {name}."
        msg = (result.stderr or result.stdout or "").strip() or "process not found"
        return f"Could not close {name}: {msg}"
