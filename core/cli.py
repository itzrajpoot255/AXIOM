"""Terminal frontend — renders the assistant's event stream in a REPL."""

from __future__ import annotations

import queue
import sys
import threading

from .assistant import Assistant
from .config import config, CONFIG_PATH, DATA_DIR

BANNER = r"""
     _    _______   _______
    / \  | ____\ \ / / ____|   AXIOM — local AI operating-system assistant
   / _ \ |  _|  \ V /|  _|     text + voice · tools · local models only
  / ___ \| |___  | | | |___    /help for commands
 /_/   \_\_____| |_| |_____|
"""


def _enable_vt() -> bool:
    if not sys.stdout.isatty():
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return sys.platform != "win32"


_COLOR = _enable_vt()


def c(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _COLOR else text


DIM, CYAN, GREEN, YELLOW, RED = "2", "36", "32", "33", "31"


class CliFrontend:
    def __init__(self, assistant: Assistant) -> None:
        self.assistant = assistant
        self.events = assistant.subscribe()
        self._done_flags: dict[int, threading.Event] = {}
        self._thinking = False
        self._renderer = threading.Thread(target=self._render_loop, daemon=True,
                                          name="cli-render")
        self._renderer.start()
        assistant.confirm_hook = self._confirm

    # ── Event rendering (single consumer for ALL sources) ────────

    def _render_loop(self) -> None:
        while True:
            ev = self.events.get()
            if ev is None:
                return
            kind = ev.get("type")
            if kind == "user":
                src = ev.get("source")
                if src == "voice":
                    print("\n" + c(f"heard> {ev['text']}", GREEN))
                elif src == "schedule":
                    print("\n" + c(f"[scheduled] {ev['text']}", YELLOW))
                print(c("axiom> ", CYAN), end="", flush=True)
            elif kind == "thinking":
                if not self._thinking:
                    print(c("\n  [thinking] ", DIM), end="", flush=True)
                    self._thinking = True
                print(c(ev["text"].replace("\n", "\n  "), DIM), end="", flush=True)
            elif kind == "token":
                if self._thinking:
                    print()
                    self._thinking = False
                print(ev["text"], end="", flush=True)
            elif kind == "tool_start":
                self._thinking = False
                print(c(f"\n  > {ev['name']}({ev['args']})", DIM), flush=True)
            elif kind == "tool_result":
                print(c(f"    {ev['preview'][:140]}", DIM), flush=True)
            elif kind == "error":
                print("\n" + c(f"[error] {ev['text']}", RED))
            elif kind == "notify":
                print("\n" + c(f"[axiom] {ev['text']}", YELLOW))
            elif kind == "memory":
                print(c(f"  [memory] {ev['text']}", DIM))
            elif kind == "done":
                self._thinking = False
                model = ev.get("model") or "?"
                tools = ev.get("tools_used") or 0
                suffix = f" · {tools} tool call(s)" if tools else ""
                print("\n" + c(f"  ({model}{suffix})", DIM))
                flag = self._done_flags.pop(ev.get("turn_id"), None)
                if flag:
                    flag.set()

    def _confirm(self, prompt: str) -> bool:
        try:
            answer = input(c(f"  confirm {prompt} [y/N]: ", YELLOW)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")

    def _run_and_wait(self, text: str) -> None:
        turn_id = self.assistant.submit(text, source="text")
        flag = threading.Event()
        self._done_flags[turn_id] = flag
        try:
            while not flag.wait(timeout=0.2):
                pass
        except KeyboardInterrupt:
            self.assistant.agent.cancel.set()
            print(c("\n(interrupted)", DIM))

    # ── Commands ─────────────────────────────────────────────────

    def handle_command(self, line: str) -> bool:
        a = self.assistant
        parts = line[1:].split()
        cmd = parts[0].lower() if parts else "help"
        arg = " ".join(parts[1:])

        if cmd in ("quit", "exit", "q"):
            return False
        elif cmd == "help":
            print(_HELP)
        elif cmd == "new":
            a.new_session()
            print("New session started.")
        elif cmd == "voice":
            print(a.voice_off() if arg == "off" else a.voice_on())
        elif cmd == "speak":
            print(a.set_speak_mode(arg))
        elif cmd == "models":
            if arg == "refresh":
                a.models.discover()
            a.models.ensure_ready()
            print(a.models.summary())
        elif cmd == "model":
            if not arg:
                print(f"Overrides: {config.model_overrides or '(none — automatic routing)'}")
            else:
                bits = arg.split()
                role, name = (bits[0], bits[1]) if len(bits) > 1 else ("chat", bits[0])
                print(a.set_model_override(role, name))
        elif cmd == "tools":
            print("\n".join(f"  {n}" for n in sorted(a.registry.names())))
        elif cmd == "memory":
            profile = a.memory.profile_all()
            if profile:
                print("  profile:")
                for k, v in profile.items():
                    print(f"    {k}: {v}")
            facts = a.memory.all_facts()
            if facts:
                print("  facts:")
                for fid, content, topic in facts:
                    tag = f" [{topic}]" if topic else ""
                    print(f"    #{fid}{tag} {content}")
            if not profile and not facts:
                print("Nothing learned yet — it fills in as you talk "
                      "(or say 'remember that ...').")
        elif cmd == "forget" and arg.isdigit():
            print("Forgotten." if a.memory.forget(int(arg)) else "No such fact.")
        elif cmd == "sessions":
            import time as _t
            for s in a.memory.sessions(limit=20):
                mark = ">" if s["id"] == a.memory.session_id else " "
                when = _t.strftime("%d %b %H:%M", _t.localtime(s["ts"]))
                print(f"  {mark} #{s['id']:<4} {when}  ({s['messages']:>3} msg)  {s['title']}")
        elif cmd == "session" and arg.isdigit():
            ok = a.memory.switch_session(int(arg))
            print(f"Continuing conversation #{arg}." if ok else f"No session #{arg}.")
        elif cmd == "tasks":
            print(a.scheduler.describe())
        elif cmd == "status":
            s = a.status()
            print(f"data dir:  {DATA_DIR}\nconfig:    {CONFIG_PATH}")
            print(f"models:    {s['models'] if s['models'] is not None else '(discovering...)'}"
                  f" | errors: {'; '.join(s['errors']) or 'none'}")
            print(f"routes:    {s['routes']}")
            print(f"voice:     {'on' if s['voice'] else 'off'} | speak: {s['speak']}")
            print(f"tools:     {s['tools']} | session: #{s['session']}"
                  f" | facts: {s['facts']}")
        else:
            print(f"Unknown command: /{cmd} — try /help")
        return True

    # ── Main loop ────────────────────────────────────────────────

    def repl(self) -> None:
        print(BANNER)
        print(c(f"  config: {CONFIG_PATH}", DIM))
        print(c("  discovering models in the background...", DIM))
        print()

        read_line = None
        if sys.stdin.isatty():
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.patch_stdout import patch_stdout
                session = PromptSession()

                def read_line() -> str:
                    with patch_stdout():
                        return session.prompt("you> ")
            except Exception:
                read_line = None  # any console/terminal incompatibility
        if read_line is None:
            def read_line() -> str:
                return input(c("you> ", GREEN) if sys.stdin.isatty() else "")

        while True:
            try:
                line = read_line().strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("/"):
                if not self.handle_command(line):
                    break
                continue
            self._run_and_wait(line)

        print(c("\nShutting down.", DIM))
        self.assistant.shutdown()

    def run_once(self, prompt: str) -> None:
        self.assistant.models.ensure_ready(timeout=30)
        self._run_and_wait(prompt)
        self.assistant.shutdown()


_HELP = """commands:
  /voice [off]        start/stop always-on voice listening
  /speak on|off|auto  speak replies (auto = only voice-initiated)
  /model [role] <name|auto>  pin a model to a role (chat/code/vision)
  /models [refresh]   list discovered models and routing
  /tools              list available tools
  /memory             profile + long-term facts   /forget <id> removes a fact
  /sessions           list past conversations   /session <id> continues one
  /tasks              list scheduled tasks
  /status             component health
  /new                start a fresh conversation session
  /quit               exit
anything else is sent to the assistant."""
