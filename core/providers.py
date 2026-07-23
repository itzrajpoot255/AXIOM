"""Model providers — Ollama native API plus OpenAI-compatible servers.

One streaming interface for both wire formats. ``stream_chat`` yields
(kind, payload) events:
    ("token", str)        — content delta
    ("thinking", str)     — reasoning delta (thinking models)
    ("tool_calls", list)  — [{"name": str, "arguments": dict}, ...]
Raises ProviderError on transport/HTTP failures so callers can degrade.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Generator, Iterable, Optional

import httpx


class ProviderError(RuntimeError):
    pass


class ToolsUnsupportedError(ProviderError):
    """Server rejected the request because the model lacks tool support."""


_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)


class BaseProvider:
    """Shared HTTP plumbing. One pooled client per provider."""

    api = "base"

    def __init__(self, name: str, base_url: str) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.last_error: str = ""
        self._client: Optional[httpx.Client] = None
        self._lock = threading.Lock()

    @property
    def client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                # trust_env=False: local model servers must never be routed
                # through HTTP(S)_PROXY, and env like SSLKEYLOGFILE must not
                # be able to break plain-HTTP requests.
                self._client = httpx.Client(timeout=_TIMEOUT, trust_env=False)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def is_alive(self, timeout: float = 1.5) -> bool:
        raise NotImplementedError

    def list_models(self) -> list[dict]:
        raise NotImplementedError

    def stream_chat(self, model, messages, tools=None, options=None,
                    cancel=None) -> Generator[tuple[str, Any], None, None]:
        raise NotImplementedError


class OllamaProvider(BaseProvider):
    api = "ollama"

    def is_alive(self, timeout: float = 1.5) -> bool:
        try:
            r = self.client.get(f"{self.base_url}/api/version", timeout=timeout)
            return r.status_code == 200
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def list_models(self) -> list[dict]:
        """Raw model entries from /api/tags (name, size, details...)."""
        try:
            r = self.client.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            return r.json().get("models", [])
        except Exception as e:
            raise ProviderError(f"Ollama unreachable at {self.base_url}: {e}") from e

    def show_model(self, name: str) -> dict:
        """Details incl. `capabilities` (completion/tools/vision/thinking)."""
        try:
            r = self.client.post(f"{self.base_url}/api/show", json={"model": name})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise ProviderError(f"show({name}) failed: {e}") from e

    def stream_chat(self, model: str, messages: list[dict],
                    tools: Optional[list[dict]] = None,
                    options: Optional[dict] = None,
                    cancel: Optional[threading.Event] = None,
                    think: Optional[bool] = None,
                    ) -> Generator[tuple[str, Any], None, None]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        if think is not None:
            payload["think"] = think

        tool_calls: list[dict] = []
        try:
            with self.client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", errors="ignore")
                    if "does not support tools" in body:
                        raise ToolsUnsupportedError(body[:200])
                    raise ProviderError(f"HTTP {resp.status_code}: {body[:300]}")
                for line in resp.iter_lines():
                    if cancel is not None and cancel.is_set():
                        break
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = data.get("message", {})
                    thinking = msg.get("thinking", "")
                    if thinking:
                        yield ("thinking", thinking)
                    token = msg.get("content", "")
                    if token:
                        yield ("token", token)
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        tool_calls.append({"name": fn.get("name", ""), "arguments": args or {}})
        except (ToolsUnsupportedError, ProviderError):
            raise
        except httpx.ConnectError as e:
            raise ProviderError(
                f"Cannot reach Ollama at {self.base_url} — start it with `ollama serve`."
            ) from e
        except Exception as e:
            raise ProviderError(f"stream failed: {e}") from e

        if tool_calls:
            yield ("tool_calls", tool_calls)

    def chat_image(self, model: str, prompt: str, image_b64: str, timeout: float = 120.0) -> str:
        """One-shot vision request (screenshot analysis etc.)."""
        payload = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
        }
        try:
            r = self.client.post(f"{self.base_url}/api/chat", json=payload,
                                 timeout=httpx.Timeout(timeout))
            r.raise_for_status()
            return r.json().get("message", {}).get("content", "")
        except Exception as e:
            raise ProviderError(f"vision request failed: {e}") from e


class OpenAIProvider(BaseProvider):
    """Minimal OpenAI-compatible client (LM Studio, llama.cpp, gateways,
    or a free-tier hosted API like Groq — anything speaking the
    `/chat/completions` wire format). Pass `api_key` for providers that
    require a bearer token; local servers can leave it unset."""

    api = "openai"

    def __init__(self, name: str, base_url: str, api_key: Optional[str] = None) -> None:
        super().__init__(name, base_url)
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def is_alive(self, timeout: float = 1.5) -> bool:
        try:
            r = self.client.get(f"{self.base_url}/models", headers=self._headers(), timeout=timeout)
            return r.status_code == 200
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def list_models(self) -> list[dict]:
        try:
            r = self.client.get(f"{self.base_url}/models", headers=self._headers())
            r.raise_for_status()
            return [{"name": m.get("id", "")} for m in r.json().get("data", [])]
        except Exception as e:
            raise ProviderError(f"{self.name} unreachable: {e}") from e

    def stream_chat(self, model: str, messages: list[dict],
                    tools: Optional[list[dict]] = None,
                    options: Optional[dict] = None,
                    cancel: Optional[threading.Event] = None,
                    think: Optional[bool] = None,
                    ) -> Generator[tuple[str, Any], None, None]:
        # Translate Ollama-style tool-result messages to OpenAI format.
        oa_messages = []
        for m in messages:
            if m.get("role") == "tool":
                oa_messages.append({"role": "tool", "content": m.get("content", ""),
                                    "tool_call_id": m.get("tool_name", "call")})
            else:
                oa_messages.append({k: v for k, v in m.items() if k != "tool_calls"})

        payload: dict[str, Any] = {"model": model, "messages": oa_messages, "stream": True}
        if tools:
            payload["tools"] = tools
        opts = options or {}
        if "temperature" in opts:
            payload["temperature"] = opts["temperature"]
        if "num_predict" in opts:
            payload["max_tokens"] = opts["num_predict"]

        # name -> partial args text, accumulated across deltas
        pending: dict[int, dict] = {}
        try:
            with self.client.stream("POST", f"{self.base_url}/chat/completions",
                                    json=payload, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", errors="ignore")
                    raise ProviderError(f"HTTP {resp.status_code}: {body[:300]}")
                for line in resp.iter_lines():
                    if cancel is not None and cancel.is_set():
                        break
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        delta = json.loads(chunk)["choices"][0].get("delta", {})
                    except Exception:
                        continue
                    # LM Studio / DeepSeek-style servers stream reasoning here.
                    if delta.get("reasoning_content"):
                        yield ("thinking", delta["reasoning_content"])
                    if delta.get("content"):
                        yield ("token", delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = pending.setdefault(idx, {"name": "", "args": ""})
                        fn = tc.get("function", {})
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["args"] += fn.get("arguments") or ""
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"{self.name} stream failed: {e}") from e

        if pending:
            calls = []
            for slot in pending.values():
                try:
                    args = json.loads(slot["args"]) if slot["args"] else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append({"name": slot["name"], "arguments": args})
            yield ("tool_calls", calls)


def build_providers(cfg) -> list[BaseProvider]:
    """Instantiate the Ollama primary plus configured OpenAI endpoints."""
    providers: list[BaseProvider] = [OllamaProvider("ollama", cfg.ollama_url)]
    for ep in cfg.openai_endpoints:
        try:
            import os
            key = ep.get("api_key") or (os.environ.get(ep["api_key_env"]) if ep.get("api_key_env") else None)
            providers.append(OpenAIProvider(ep["name"], ep["base_url"], key))
        except (KeyError, TypeError):
            print(f"[providers] skipping malformed endpoint entry: {ep!r}")
    return providers
