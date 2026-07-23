"""Assistant core — services, turn execution, event hub. No UI here.

Every frontend (terminal, web, future GUI) is a thin renderer over the
same event stream:

    assistant.submit("do something", source="text")   -> turn_id
    q = assistant.subscribe()                          -> Queue of events

Events are plain dicts with a "type" key:
    user         {turn_id, text, source}
    token        {turn_id, text}
    thinking     {turn_id, text}            (reasoning-model thoughts)
    tool_start   {turn_id, name, args}
    tool_result  {turn_id, name, preview}
    file_edit    {turn_id, path, diff, added, removed}   (agent wrote a file)
    memory       {kind, text}                 (auto-learned profile/fact)
    done         {turn_id, text, model, tools_used}
    error        {turn_id, text}
    notify       {text}                      (watcher / system messages)
    voice        {state}                     (off|on|listening|...)

Memory: after each user turn a background pass re-reads the user's
message with a tiny extraction prompt and saves durable personal facts
(profile keys + free facts) without the chat model having to remember
to call memory tools. Everything saved is injected back into the
system prompt on every later turn, so replies stay personalized.

Turns are serialized through one worker thread, so voice, web, and
scheduled prompts never interleave mid-response.
"""

from __future__ import annotations

import difflib
import itertools
import json
import os
import queue
import re
import threading
import time
from typing import Callable, Optional

from .agent import Agent, TurnUI
from .config import config
from .memory import Memory
from .models import ModelManager
from .providers import build_providers
from .scheduler import Scheduler
from .tools import ToolContext, build_registry
from .watcher import SystemWatcher

_MEM_PROMPT = """You are the memory module of a personal AI assistant. \
Extract durable facts about the user from their message — things worth \
remembering across sessions.

Extract ONLY:
- profile: stable attributes of the user (name, location, occupation, \
preferences, habits, ongoing projects). Short lowercase snake_case keys.
- facts: other lasting information, each a short self-contained sentence.

Do NOT extract: questions, one-off requests, details of the current task, \
temporary states, or facts about anything other than the user themselves. \
Most messages contain nothing durable — then both fields are empty.

Reply with ONLY this JSON, nothing else:
{"profile": {}, "facts": []}"""

_JSON_RX = re.compile(r"\{.*\}", re.DOTALL)
_THINK_RX = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)
_KEY_RX = re.compile(r"[^a-z0-9_ ]+")


def _parse_memory_reply(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse the extractor's JSON, defensively. Returns (profile, facts)."""
    text = _THINK_RX.sub("", text)
    m = _JSON_RX.search(text)
    if not m:
        return {}, []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    profile: dict[str, str] = {}
    raw_profile = data.get("profile")
    if isinstance(raw_profile, dict):
        for k, v in list(raw_profile.items())[:5]:
            key = _KEY_RX.sub("", str(k).strip().lower().replace("-", "_"))[:40].strip()
            val = str(v).strip()[:200]
            if key and val:
                profile[key] = val
    facts: list[str] = []
    raw_facts = data.get("facts")
    if isinstance(raw_facts, list):
        for f in raw_facts[:3]:
            f = str(f).strip()[:300]
            if len(f) > 8:
                facts.append(f)
    return profile, facts


class Assistant:
    def __init__(self, voice: bool = False, resume: bool = False) -> None:
        self.memory = Memory()
        if not (resume and self.memory.resume_last_session()):
            self.memory.new_session()

        self.models = ModelManager(build_providers(config), config)
        self.models.discover_async()

        # Frontends may install an interactive confirm hook(prompt, source).
        self.confirm_hook: Optional[Callable[[str], bool]] = None
        self._current_source = "text"

        # Agent file edits become diff events for the IDE view; keep a
        # short history so the web UI can restore them after a reload.
        self.file_changes: list[dict] = []
        self._active_turn = 0

        # Auto-memory: one background extraction at a time, cancellable.
        self._mem_thread: Optional[threading.Thread] = None
        self._mem_cancel = threading.Event()

        ctx = ToolContext(cfg=config, memory=self.memory, models=self.models,
                          confirm=self._confirm, notify=self.notify,
                          on_file_change=self._on_file_change,
                          complete=self.quick_complete)
        self.registry = build_registry(ctx)
        self.agent = Agent(config, self.models, self.registry, self.memory)

        self.scheduler = Scheduler(
            self.memory, lambda prompt: self.submit(prompt, source="schedule"))
        ctx.scheduler = self.scheduler
        self.scheduler.start()

        self.watcher = SystemWatcher(config, self.notify)
        if config.watcher_enabled:
            self.watcher.start()

        self.voice = None
        self.speak_mode = config.speak_replies          # auto | on | off

        # Inbox watcher: cheap IMAP poll, notify-only (no model calls).
        self.email_watcher = None
        watch_min = float(config.get("email_check_minutes", 0) or 0)
        if watch_min > 0 and (config.email or {}).get("user"):
            from .tools.email_ import InboxWatcher
            summarize = None
            if config.get("email_smart_notify", False):
                summarize = lambda body: self.quick_complete(
                    "Summarize this email in one short sentence (under 18 words). "
                    "No preamble.", body, num_predict=60, temperature=0.2)
            self.email_watcher = InboxWatcher(config, self.notify,
                                              interval_s=watch_min * 60,
                                              summarize=summarize)
            self.email_watcher.start()

        # Event hub
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()

        # Turn pipeline
        self._turn_ids = itertools.count(1)
        self._turn_queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._worker = threading.Thread(target=self._turn_worker, daemon=True,
                                        name="turn-worker")
        self._worker.start()

        if voice or config.voice_enabled:
            self.voice_on()

    # ── Event hub ────────────────────────────────────────────────

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: dict) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # slow consumer loses events rather than blocking turns

    def notify(self, text: str) -> None:
        self.publish({"type": "notify", "text": text})
        if self.voice and self.voice.active and self.speak_mode != "off":
            self.voice.speak(text)

    def _on_file_change(self, path: str, before: str, after: str) -> None:
        rel = os.path.basename(path)
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
        lines = diff.splitlines()
        event = {
            "type": "file_edit",
            "turn_id": self._active_turn,
            "path": path,
            "diff": diff[:60000],
            "added": sum(1 for l in lines if l.startswith("+") and not l.startswith("+++")),
            "removed": sum(1 for l in lines if l.startswith("-") and not l.startswith("---")),
            "ts": time.time(),
        }
        self.file_changes.append(event)
        del self.file_changes[:-50]
        self.publish(event)

    # ── Turns ────────────────────────────────────────────────────

    def submit(self, text: str, source: str = "text") -> int:
        """Queue a user turn. Events stream to all subscribers."""
        turn_id = next(self._turn_ids)
        self._turn_queue.put((turn_id, text, source))
        return turn_id

    def _turn_worker(self) -> None:
        while True:
            item = self._turn_queue.get()
            if item is None:
                return
            turn_id, text, source = item
            try:
                self._run_turn(turn_id, text, source)
            except Exception as e:
                self.publish({"type": "error", "turn_id": turn_id,
                              "text": f"internal error: {e}"})

    def _run_turn(self, turn_id: int, text: str, source: str) -> None:
        self._current_source = source
        self._active_turn = turn_id
        self.publish({"type": "user", "turn_id": turn_id,
                      "text": text, "source": source})

        speak = (self.speak_mode == "on"
                 or (self.speak_mode == "auto" and source == "voice"))
        speak = speak and self.voice is not None

        def on_token(tok: str) -> None:
            self.publish({"type": "token", "turn_id": turn_id, "text": tok})
            if speak:
                self.voice.speak_token(tok)

        def on_thinking(tok: str) -> None:
            self.publish({"type": "thinking", "turn_id": turn_id, "text": tok})

        def on_tool_start(name: str, args: dict) -> None:
            arg_str = ", ".join(f"{k}={str(v)[:50]!r}" for k, v in args.items())
            self.publish({"type": "tool_start", "turn_id": turn_id,
                          "name": name, "args": arg_str})

        def on_tool_result(name: str, output: str) -> None:
            preview = output.strip().replace("\n", " | ")[:200]
            self.publish({"type": "tool_result", "turn_id": turn_id,
                          "name": name, "preview": preview})

        ui = TurnUI(on_token=on_token, on_thinking=on_thinking,
                    on_tool_start=on_tool_start, on_tool_result=on_tool_result)
        result = self.agent.run_turn(text, ui)

        if speak:
            self.voice.flush_speech()
        if result.error and result.error != result.text:
            self.publish({"type": "error", "turn_id": turn_id, "text": result.error})
        self.publish({"type": "done", "turn_id": turn_id, "text": result.text,
                      "model": result.model, "tools_used": len(result.tool_log)})

        if source in ("text", "voice") and result.error is None:
            self._learn_async(text)

    # ── Auto-memory ──────────────────────────────────────────────

    def _learn_async(self, user_text: str) -> None:
        """Fire-and-forget memory extraction; never blocks the next turn."""
        if not config.get("auto_memory", True) or len(user_text.strip()) < 12:
            return
        if self._mem_thread and self._mem_thread.is_alive():
            return                       # still digesting the previous turn
        self._mem_thread = threading.Thread(
            target=self._learn, args=(user_text,), daemon=True, name="auto-memory")
        self._mem_thread.start()

    def _learn(self, user_text: str) -> None:
        try:
            model = self.models.pick("chat")
            if model is None:
                return
            msgs = [{"role": "system", "content": _MEM_PROMPT},
                    {"role": "user", "content": user_text[:2000]}]
            kwargs = {"tools": None, "cancel": self._mem_cancel,
                      "options": {"temperature": 0.0, "num_predict": 300,
                                  "num_ctx": 2048}}
            if model.provider.api == "ollama":
                kwargs["think"] = False
            parts = []
            for kind, payload in model.provider.stream_chat(model.name, msgs, **kwargs):
                if kind == "token":
                    parts.append(payload)
            profile, facts = _parse_memory_reply("".join(parts))
            existing = self.memory.profile_all()
            for key, val in profile.items():
                if existing.get(key) == val:
                    continue
                self.memory.profile_set(key, val)
                self.publish({"type": "memory", "kind": "profile",
                              "text": f"{key} = {val}"})
            known = {content for _id, content, _t in self.memory.all_facts()}
            for fact in facts:
                if fact in known:
                    continue
                self.memory.remember(fact, "auto")
                self.publish({"type": "memory", "kind": "fact", "text": fact})
        except Exception as e:
            # Memory is best-effort: a failed pass must never surface as
            # an error in the conversation.
            print(f"[memory] extraction failed: {type(e).__name__}: {e}")

    # ── One-shot completion (no tools, no history, no memory pass) ──

    def quick_complete(self, system: str, user: str, role: str = "chat",
                       num_predict: int = 400, temperature: float = 0.4) -> str:
        """Single focused model call returning plain text. Used by the
        hardware advisor and email digest — features that want the model's
        judgement on one blob of data without spinning up a full turn."""
        if not self.models.ensure_ready(timeout=20):
            return "Model discovery hasn't finished — try again in a moment."
        self.models.maybe_rediscover()
        model = self.models.pick(role) or self.models.pick("chat")
        if model is None:
            hint = "; ".join(self.models.errors) or "no models installed"
            return f"No usable model found ({hint})."
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user[:8000]}]
        kwargs = {"tools": None,
                  "options": {"temperature": temperature,
                              "num_predict": num_predict, "num_ctx": 4096}}
        if model.provider.api == "ollama":
            kwargs["think"] = False
        parts: list[str] = []
        try:
            for kind, payload in model.provider.stream_chat(model.name, msgs, **kwargs):
                if kind == "token":
                    parts.append(payload)
        except Exception as e:
            return f"Model call failed: {type(e).__name__}: {e}"
        text = "".join(parts)
        return _THINK_RX.sub("", text).strip() or "(no response)"

    def _confirm(self, prompt: str) -> bool:
        if self.confirm_hook and self._current_source == "text":
            return self.confirm_hook(prompt)
        self.notify(f"Action needs confirmation and was skipped: {prompt}. "
                    "Run it from the terminal frontend, or relax the confirm_* "
                    "settings in config.json.")
        return False

    # ── Voice ────────────────────────────────────────────────────

    def voice_on(self) -> str:
        if self.voice and self.voice.active:
            return "Voice is already on."
        from .voice import VoiceIO
        if self.voice is None:
            self.voice = VoiceIO(
                config,
                on_utterance=lambda text: self.submit(text, source="voice"),
                on_state=lambda s: self.publish({"type": "voice", "state": s}))
        msg = self.voice.start()
        self.publish({"type": "voice",
                      "state": "on" if self.voice.active else "unavailable"})
        return msg

    def voice_off(self) -> str:
        if self.voice and self.voice.active:
            self.voice.stop()
            self.publish({"type": "voice", "state": "off"})
            return "Voice off."
        return "Voice was not on."

    @property
    def voice_active(self) -> bool:
        return bool(self.voice and self.voice.active)

    # ── Introspection for frontends ──────────────────────────────

    def status(self) -> dict:
        ready = self.models.ensure_ready(timeout=0.1)
        routes = {}
        if ready:
            for role in ("chat", "code", "vision"):
                picked = self.models.pick(role)
                routes[role] = picked.name if picked else None
        return {
            "models": len(self.models.models) if ready else None,
            "routes": routes,
            "errors": self.models.errors,
            "voice": self.voice_active,
            "speak": self.speak_mode,
            "tools": len(self.registry.names()),
            "session": self.memory.session_id,
            "facts": len(self.memory.all_facts()),
            "profile": len(self.memory.profile_all()),
        }

    def new_session(self) -> int:
        return self.memory.new_session()

    def set_model_override(self, role: str, name: str) -> str:
        if name == "auto":
            config.model_overrides.pop(role, None)
        else:
            config.model_overrides[role] = name
        config.save()
        return f"route[{role}] -> {name}"

    def set_speak_mode(self, mode: str) -> str:
        if mode in ("on", "off", "auto"):
            self.speak_mode = mode
            config.set("speak_replies", mode)
        return f"Speak mode: {self.speak_mode}"

    # ── Lifecycle ────────────────────────────────────────────────

    def shutdown(self) -> None:
        self.agent.cancel.set()
        self._mem_cancel.set()
        self._turn_queue.put(None)
        if self.voice:
            self.voice.stop()
        if self.email_watcher:
            self.email_watcher.stop()
        self.watcher.stop()
        self.scheduler.stop()
        for p in self.models.providers:
            p.close()
        self.memory.close()
