"""Configuration — code defaults overlaid with ~/.axiom/config.json.

Edit the JSON file to change behaviour; missing keys fall back to defaults.
Access values as attributes: ``config.ollama_url``.
"""

from __future__ import annotations

import json
import os
from typing import Any

DATA_DIR = os.path.join(os.path.expanduser("~"), ".axiom")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

_DEFAULTS: dict[str, Any] = {
    # ── Model providers ──────────────────────────────────────────
    "ollama_url": "http://localhost:11434",
    # Extra OpenAI-compatible servers probed at discovery (LM Studio,
    # llama.cpp server, OpenClaw gateway, ...). Add your own here.
    #
    # Groq's free API is included below and stays completely inert until
    # you set a GROQ_API_KEY environment variable (get a free key at
    # console.groq.com/keys, no credit card). Useful for cloud deployment,
    # where there's no local machine to run Ollama on — see README.
    "openai_endpoints": [
        {"name": "lmstudio", "base_url": "http://localhost:1234/v1"},
        {"name": "groq", "base_url": "https://api.groq.com/openai/v1",
         "api_key_env": "GROQ_API_KEY"},
    ],
    # Directories scanned for loose .gguf files (reported, importable
    # into Ollama on request). Non-existent dirs are skipped silently.
    "gguf_dirs": [
        "~/models",
        "~/.openclaw/models",
        "~/.cache/lm-studio/models",
    ],

    # ── Model routing ────────────────────────────────────────────
    # Explicit overrides win over automatic capability-based routing.
    # e.g. {"chat": "qwen3:8b", "code": "qwen2.5-coder:14b"}
    "model_overrides": {},
    # Preferred parameter range (billions) for the default chat model.
    # Smaller = faster on CPU. Routing picks the largest model inside
    # the range, falling back to nearest outside it.
    "chat_size_range_b": [3, 14],
    "auto_route": True,            # route coding questions to a code model
    "num_ctx": 8192,               # context window requested from the model
    "num_predict": 1024,           # max tokens per model reply
    "temperature": 0.6,

    # ── Agent ────────────────────────────────────────────────────
    "show_thinking": True,         # stream thinking-model reasoning to the UI
    "auto_memory": True,           # background pass learns user facts each turn
    "max_tool_iterations": 8,      # tool-call rounds per user turn
    "max_tool_result_chars": 8000, # truncate tool output fed to the model
    "max_history_messages": 30,    # messages of history sent per request
    "confirm_shell_commands": False,
    "confirm_email_send": True,

    # ── Voice ────────────────────────────────────────────────────
    "voice_enabled": False,        # load STT/TTS at startup (--voice overrides)
    "whisper_model": "base",       # tiny/base/small/medium
    "whisper_device": "cpu",
    "whisper_compute": "int8",
    "whisper_beam_size": 1,        # 1 = fastest; raise for accuracy
    "vad_energy_threshold": 0.015,
    "vad_silence_timeout": 1.0,
    "vad_min_speech_duration": 0.4,
    "tts_backend": "auto",         # auto | edge | sapi | off
    "tts_voice": "en-GB-RyanNeural",
    "tts_rate": "+8%",
    "tts_pitch": "-6Hz",
    "speak_replies": "auto",       # auto = speak only voice-initiated turns

    # ── System watcher (event-edge alerts, cheap 30 s poll) ─────
    "watcher_enabled": True,
    "watcher_interval": 30.0,
    "watcher_ram_pct": 92.0,
    "watcher_cpu_pct": 95.0,
    "watcher_disk_free_pct": 5.0,
    "watcher_battery_pct": 15.0,

    # ── Misc tools ───────────────────────────────────────────────
    "news_region": "PK",
    "news_lang": "en",
    # Email: fill in to enable Gmail automation. Use a Google app password
    # (myaccount.google.com -> Security -> App passwords), not your login.
    # {"imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com",
    #  "user": "you@gmail.com", "password": "app-password"}
    "email": {},
    # >0 = poll the inbox every N minutes and notify on new mail.
    "email_check_minutes": 0,
    # When watching, generate a one-line AI summary for each new arrival.
    "email_smart_notify": False,
    # Inbox rules applied to incoming mail by the watcher. Each rule:
    # {"name": "Boss", "from": "boss@", "subject": "", "priority": true}
    # First case-insensitive substring match wins; empty field = any.
    "email_rules": [],
}


class _Config:
    def __init__(self) -> None:
        self._values = dict(_DEFAULTS)
        os.makedirs(DATA_DIR, exist_ok=True)
        self._load()

    def _load(self) -> None:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                self._values.update(user)
        except FileNotFoundError:
            # First run: write a starter file the user can edit.
            self.save()
        except Exception as e:
            print(f"[config] ignoring invalid {CONFIG_PATH}: {e}")

    def save(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._values, f, indent=2)
        except OSError as e:
            print(f"[config] could not write {CONFIG_PATH}: {e}")

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)

    def set(self, name: str, value: Any, persist: bool = True) -> None:
        self._values[name] = value
        if persist:
            self.save()


config = _Config()
