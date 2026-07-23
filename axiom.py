"""AXIOM — local AI operating-system assistant.

Usage:
  python axiom.py              # text REPL (fast start, no heavy deps)
  python axiom.py --voice      # also start always-on voice
  python axiom.py --resume     # continue the previous conversation
  python axiom.py --once "git status of this repo"
  python axiom.py --check      # diagnose environment and exit
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def check() -> int:
    print("AXIOM environment check")
    print(f"  python: {sys.version.split()[0]}")

    deps = {
        "httpx (required)": "httpx",
        "psutil (system tools)": "psutil",
        "sounddevice (voice)": "sounddevice",
        "numpy (voice)": "numpy",
        "faster_whisper (voice)": "faster_whisper",
        "edge_tts (voice)": "edge_tts",
        "pyttsx3 (offline TTS)": "pyttsx3",
        "ddgs (web search)": "ddgs",
        "PIL (screenshots)": "PIL",
        "pyautogui (input automation)": "pyautogui",
        "prompt_toolkit (nicer REPL)": "prompt_toolkit",
    }
    import importlib.util
    for label, mod in deps.items():
        ok = importlib.util.find_spec(mod) is not None
        print(f"  {'ok ' if ok else 'MISSING'} {label}")

    from core.config import config, CONFIG_PATH
    from core.providers import build_providers, OllamaProvider
    print(f"  config: {CONFIG_PATH}")
    for p in build_providers(config):
        alive = p.is_alive()
        line = f"  {'ok ' if alive else '-- '} provider {p.name} @ {p.base_url}"
        if alive and isinstance(p, OllamaProvider):
            try:
                names = [m.get("name", "?") for m in p.list_models()]
                line += f" ({len(names)} models: {', '.join(names[:6])})"
            except Exception as e:
                line += f" (list failed: {e})"
        print(line)
        p.close()
    email_ok = bool(config.email.get("user")) if config.email else False
    print(f"  {'ok ' if email_ok else '-- '} email {'configured' if email_ok else 'not configured (optional)'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AXIOM — local AI assistant")
    parser.add_argument("--voice", action="store_true", help="start with voice on")
    parser.add_argument("--resume", action="store_true", help="continue last session")
    parser.add_argument("--once", metavar="PROMPT", help="run one prompt and exit")
    parser.add_argument("--check", action="store_true", help="environment diagnostics")
    parser.add_argument("--server", action="store_true", help="launch web UI (FastAPI + uvicorn)")
    parser.add_argument("--host", default="127.0.0.1", help="server bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 0)),
                        help="server port (default: $PORT env var if set, else auto from config)")
    parser.add_argument("--no-browser", action="store_true", help="don't open browser automatically")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.check:
        return check()

    from core.assistant import Assistant
    assistant = Assistant(voice=args.voice, resume=args.resume)

    if args.server:
        from core import server
        server.run(assistant, host=args.host, port=args.port,
                   open_browser=not args.no_browser)
        return 0

    from core.cli import CliFrontend
    front = CliFrontend(assistant)
    if args.once:
        front.run_once(args.once)
    else:
        front.repl()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
