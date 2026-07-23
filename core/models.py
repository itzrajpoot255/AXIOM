"""Model discovery and capability-based routing.

Nothing is hardcoded to a specific model. At startup a background
thread asks every reachable provider what it serves, classifies each
model by capability (tools / vision / code / thinking / size), and the
router picks the best fit per role:

    chat   — general conversation, prefers tool support + mid size
    code   — coding tasks, prefers code-tuned families
    vision — image understanding (screenshots)

User overrides in config (`model_overrides`) always win. Loose .gguf
files in configured directories are reported as importable.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .providers import BaseProvider, OllamaProvider, ProviderError

_CODE_HINTS = re.compile(r"coder|code|codestral|starcoder|deepseek-coder|codellama|devstral", re.I)
_VISION_HINTS = re.compile(r"llava|vision|moondream|minicpm-v|gemma3(?!n)|qwen.*vl|pixtral|vl\b", re.I)
_THINK_HINTS = re.compile(r"qwen3|r1|think|reason|qwq|o1", re.I)
_EMBED_HINTS = re.compile(r"embed|bge|nomic|minilm", re.I)


@dataclass
class ModelInfo:
    name: str
    provider: BaseProvider
    size_b: float = 0.0              # parameters, billions (0 = unknown)
    capabilities: set = field(default_factory=set)  # tools/vision/thinking/code/embedding

    def has(self, cap: str) -> bool:
        return cap in self.capabilities

    def describe(self) -> str:
        caps = ",".join(sorted(self.capabilities)) or "chat"
        size = f"{self.size_b:g}B" if self.size_b else "?"
        return f"{self.name:<28} {size:>6}  [{caps}] via {self.provider.name}"


def _parse_size_b(text: str) -> float:
    """'14.8B' / '8b' / '1.5B' -> billions as float."""
    m = re.search(r"([\d.]+)\s*[bB]", text or "")
    try:
        return float(m.group(1)) if m else 0.0
    except ValueError:
        return 0.0


class ModelManager:
    def __init__(self, providers: list[BaseProvider], cfg) -> None:
        self.cfg = cfg
        self.providers = providers
        self.models: list[ModelInfo] = []
        self.gguf_files: list[str] = []
        self.errors: list[str] = []
        self._ready = threading.Event()
        self._last_discovery = 0.0
        self._discover_lock = threading.Lock()
        # Runtime probe results: model name -> native tools actually worked
        self._tools_runtime: dict[str, bool] = {}

    # ── Discovery ────────────────────────────────────────────────

    def discover_async(self) -> None:
        threading.Thread(target=self.discover, daemon=True, name="model-discovery").start()

    def ensure_ready(self, timeout: float = 15.0) -> bool:
        return self._ready.wait(timeout)

    def maybe_rediscover(self) -> None:
        """Re-run discovery if the last one found nothing usable.

        Lets a session recover automatically when Ollama is started
        *after* AXIOM — the next message simply works. Rate-limited.
        """
        if self.models or time.time() - self._last_discovery < 5.0:
            return
        self.discover()

    def discover(self) -> None:
        with self._discover_lock:
            self._last_discovery = time.time()
            self._discover_locked()

    def _discover_locked(self) -> None:
        models: list[ModelInfo] = []
        errors: list[str] = []
        for provider in self.providers:
            if not provider.is_alive():
                if provider.api == "ollama":
                    detail = f" ({provider.last_error})" if provider.last_error else ""
                    errors.append(f"{provider.name} not reachable at "
                                  f"{provider.base_url}{detail} — is `ollama serve` "
                                  "or the Ollama app running?")
                continue
            try:
                for entry in provider.list_models():
                    name = entry.get("name", "")
                    if not name:
                        continue
                    info = ModelInfo(name=name, provider=provider)
                    details = entry.get("details", {}) or {}
                    info.size_b = _parse_size_b(details.get("parameter_size", "")) or _parse_size_b(name)
                    if isinstance(provider, OllamaProvider):
                        self._enrich_from_show(provider, info)
                    self._classify_by_name(info)
                    models.append(info)
            except ProviderError as e:
                errors.append(str(e))

        self.models = models
        self.errors = errors
        self.gguf_files = self._scan_gguf()
        self._ready.set()

    def _enrich_from_show(self, provider: OllamaProvider, info: ModelInfo) -> None:
        """Pull authoritative capabilities from /api/show when available."""
        try:
            detail = provider.show_model(info.name)
        except ProviderError:
            return
        for cap in detail.get("capabilities") or []:
            if cap in ("tools", "vision", "thinking", "embedding"):
                info.capabilities.add(cap)
        details = detail.get("details", {}) or {}
        if not info.size_b:
            info.size_b = _parse_size_b(details.get("parameter_size", ""))

    @staticmethod
    def _classify_by_name(info: ModelInfo) -> None:
        """Heuristic fill-in for providers without capability metadata."""
        if _CODE_HINTS.search(info.name):
            info.capabilities.add("code")
        if _VISION_HINTS.search(info.name):
            info.capabilities.add("vision")
        if _THINK_HINTS.search(info.name):
            info.capabilities.add("thinking")
        if _EMBED_HINTS.search(info.name):
            info.capabilities.add("embedding")

    def _scan_gguf(self) -> list[str]:
        found: list[str] = []
        for raw in self.cfg.gguf_dirs:
            root = os.path.expanduser(raw)
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if fn.lower().endswith(".gguf"):
                        found.append(os.path.join(dirpath, fn))
                if len(found) > 50:
                    return found
        return found

    # ── Routing ──────────────────────────────────────────────────

    def _chat_candidates(self) -> list[ModelInfo]:
        # Exclude embedders always; exclude vision specialists unless they
        # also support tools (then they're fine general models).
        return [m for m in self.models
                if not m.has("embedding") and (not m.has("vision") or m.has("tools"))]

    def pick(self, role: str) -> Optional[ModelInfo]:
        """Best model for a role: chat | code | vision. None if nothing fits."""
        override = self.cfg.model_overrides.get(role)
        if override:
            found = self.find(override)
            if found:
                return found
            print(f"[models] override '{override}' for role '{role}' not installed; auto-routing")

        if not self.models:
            return None

        if role == "vision":
            vis = [m for m in self.models if m.has("vision")]
            return max(vis, key=lambda m: m.size_b) if vis else None

        if role == "code":
            coders = [m for m in self.models if m.has("code")]
            if coders:
                with_tools = [m for m in coders if self._tools_ok(m)]
                pool = with_tools or coders
                return max(pool, key=lambda m: m.size_b)
            return self.pick("chat")

        # chat: prefer tool-capable, non-code-specialised, inside size range
        lo, hi = self.cfg.chat_size_range_b
        pool = [m for m in self._chat_candidates() if not m.has("code")]
        pool = pool or self._chat_candidates() or list(self.models)
        with_tools = [m for m in pool if self._tools_ok(m)]
        pool = with_tools or pool

        in_range = [m for m in pool if lo <= (m.size_b or lo) <= hi]
        if in_range:
            return max(in_range, key=lambda m: m.size_b)
        return min(pool, key=lambda m: abs((m.size_b or lo) - hi))

    def _tools_ok(self, m: ModelInfo) -> bool:
        runtime = self._tools_runtime.get(m.name)
        if runtime is not None:
            return runtime
        return m.has("tools")

    def mark_tools_unsupported(self, name: str) -> None:
        """Runtime learned this model rejects native tools — remember it."""
        self._tools_runtime[name] = False

    def find(self, name: str) -> Optional[ModelInfo]:
        low = name.lower()
        for m in self.models:
            if m.name.lower() == low:
                return m
        for m in self.models:
            if m.name.lower().split(":")[0] == low:
                return m
        return None

    def summary(self) -> str:
        if not self._ready.is_set():
            return "Model discovery still running..."
        lines = []
        if self.models:
            lines.append(f"{len(self.models)} model(s) discovered:")
            lines += [f"  {m.describe()}" for m in sorted(self.models, key=lambda x: -x.size_b)]
            for role in ("chat", "code", "vision"):
                picked = self.pick(role)
                lines.append(f"  route[{role}] -> {picked.name if picked else '(none)'}")
        else:
            lines.append("No models discovered.")
        if self.gguf_files:
            lines.append(f"{len(self.gguf_files)} loose GGUF file(s) found "
                         f"(importable via `ollama create`):")
            lines += [f"  {p}" for p in self.gguf_files[:10]]
        for err in self.errors:
            lines.append(f"  ! {err}")
        return "\n".join(lines)
