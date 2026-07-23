"""Agent — the reasoning loop.

One user turn = stream model output, execute any tool calls, feed
results back, repeat until the model answers in plain text (or the
iteration cap hits). Works two ways:

  native    — model supports tool calling: schemas go in the `tools`
              parameter, calls come back structured.
  fallback  — model lacks tool support: a compact protocol is injected
              into the system prompt and JSON tool calls are parsed
              from the reply.

Capability is discovered, then corrected at runtime: if a server
rejects `tools`, the model is remembered as fallback-only.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import ModelInfo, ModelManager
from .persona import build_system_prompt
from .providers import ProviderError, ToolsUnsupportedError

_CODE_RX = re.compile(
    r"\b(code|coding|program|script|function|class|method|bug|debug|refactor|"
    r"compile|implement|regex|sql|python|javascript|typescript|rust|java|c\+\+|"
    r"exception|traceback|stack trace|unit test|api endpoint|repo|repository)\b", re.I)

_THINK_RX = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# Fallback protocol: a JSON object with a "tool" key, possibly fenced.
_FALLBACK_CALL_RX = re.compile(
    r"```(?:json|tool)?\s*(\{.*?\})\s*```|(\{[^{}]*\"tool\"[^{}]*(?:\{[^{}]*\}[^{}]*)?\})",
    re.DOTALL)


@dataclass
class TurnUI:
    """Callbacks for rendering a turn. All optional."""
    on_token: Callable[[str], None] = lambda s: None
    on_thinking: Callable[[str], None] = lambda s: None
    on_tool_start: Callable[[str, dict], None] = lambda n, a: None
    on_tool_result: Callable[[str, str], None] = lambda n, r: None
    on_status: Callable[[str], None] = lambda s: None


@dataclass
class TurnResult:
    text: str = ""
    tool_log: list = field(default_factory=list)   # (name, args, result)
    model: str = ""
    error: Optional[str] = None


class Agent:
    def __init__(self, cfg, models: ModelManager, registry, memory) -> None:
        self.cfg = cfg
        self.models = models
        self.registry = registry
        self.memory = memory
        self.cancel = threading.Event()

    # ── Routing ──────────────────────────────────────────────────

    def _route(self, text: str, role_hint: Optional[str]) -> Optional[ModelInfo]:
        if role_hint:
            return self.models.pick(role_hint)
        if self.cfg.auto_route and _CODE_RX.search(text):
            return self.models.pick("code")
        return self.models.pick("chat")

    # ── Main loop ────────────────────────────────────────────────

    def run_turn(self, user_text: str, ui: TurnUI,
                 role_hint: Optional[str] = None) -> TurnResult:
        result = TurnResult()
        self.cancel.clear()

        if not self.models.ensure_ready(timeout=20):
            result.error = "Model discovery hasn't finished — try again in a moment."
            return result
        # If the model server came up after we did, find it now.
        self.models.maybe_rediscover()
        model = self._route(user_text, role_hint)
        if model is None:
            hint = "; ".join(self.models.errors) or "no models installed"
            result.error = (f"No usable model found ({hint}). "
                            "Install one with e.g. `ollama pull qwen3:8b`.")
            return result
        result.model = model.name

        self.memory.add_message("user", user_text)
        history = self.memory.recent_messages(self.cfg.max_history_messages)
        native_tools = self.models._tools_ok(model)
        messages = self._build_messages(history, user_text, native_tools)

        options = {"temperature": self.cfg.temperature,
                   "num_predict": self.cfg.num_predict,
                   "num_ctx": self.cfg.num_ctx}
        # Thinking models reason before answering; stream that reasoning to
        # the UI (show_thinking) or turn it off entirely to save tokens.
        think = bool(self.cfg.show_thinking) if model.has("thinking") else None

        visible_parts: list[str] = []
        call_counts: dict[tuple, int] = {}
        answered = False
        for iteration in range(self.cfg.max_tool_iterations):
            if self.cancel.is_set():
                break
            text, tool_calls, err = self._stream_once(
                model, messages, native_tools, options, think, ui)

            if err == "tools_unsupported":
                # Re-issue this same round with the fallback protocol.
                self.models.mark_tools_unsupported(model.name)
                native_tools = False
                messages = self._build_messages(history, user_text, native_tools,
                                                carry=messages)
                continue
            if err:
                result.error = err
                if not visible_parts:
                    ui.on_token(err)
                    visible_parts.append(err)
                break

            pseudo = False
            if not tool_calls:
                # Some models write the call as text even when native tools
                # are offered — catch it rather than losing the action.
                tool_calls, text = self._parse_fallback_calls(text)
                pseudo = bool(tool_calls)

            if text.strip():
                visible_parts.append(text.strip())

            if not tool_calls:
                answered = True
                break

            # Execute tools, append results, loop for the model's next step.
            if native_tools and not pseudo:
                messages.append({"role": "assistant", "content": text,
                                 "tool_calls": [{"function": {"name": c["name"],
                                                              "arguments": c["arguments"]}}
                                                for c in tool_calls]})
            elif text.strip():
                messages.append({"role": "assistant", "content": text})

            for call in tool_calls:
                if self.cancel.is_set():
                    break
                name, args = call["name"], call["arguments"]
                ui.on_tool_start(name, args)
                key = (name, json.dumps(args, sort_keys=True, default=str))
                call_counts[key] = call_counts.get(key, 0) + 1
                if call_counts[key] >= 3:
                    output = ("[repeat blocked] You already ran this exact call; "
                              "the result has not changed. Use different arguments, "
                              "a different tool, or give your final answer now.")
                else:
                    output = self.registry.execute(name, args)
                    if len(output) > self.cfg.max_tool_result_chars:
                        output = output[:self.cfg.max_tool_result_chars] + "\n...[truncated]"
                ui.on_tool_result(name, output)
                result.tool_log.append((name, args, output))
                if native_tools and not pseudo:
                    messages.append({"role": "tool", "content": output,
                                     "tool_name": name})
                else:
                    messages.append({"role": "user",
                                     "content": f"[result of {name}]:\n{output}"})

        if not answered and not self.cancel.is_set() and result.error is None:
            # Tool budget spent without a final reply — force a closing
            # answer with no tools on offer so the user never gets silence.
            messages.append({"role": "user", "content":
                             "[tool budget exhausted — answer the original question "
                             "now from what you have; no more tool calls]"})
            text, _calls, err = self._stream_once(model, messages, False, options,
                                                  think, ui)
            if err:
                result.error = err
            _ignored, text = self._parse_fallback_calls(text)
            if text.strip():
                visible_parts.append(text.strip())

        result.text = "\n".join(visible_parts).strip()
        self._persist_assistant(result)
        return result

    # ── Helpers ──────────────────────────────────────────────────

    def _build_messages(self, history: list[dict], user_text: str,
                        native_tools: bool, carry: Optional[list] = None) -> list[dict]:
        facts = self.memory.recall(user_text, limit=8)
        docs = None if native_tools else self.registry.docs()
        system = build_system_prompt(facts, fallback_tool_docs=docs,
                                     profile=self.memory.profile_all())
        msgs: list[dict] = [{"role": "system", "content": system}]
        if carry:
            # Tools turned out unsupported mid-turn: keep accumulated turn
            # state but swap the system prompt and strip native tool fields.
            for m in carry[1:]:
                if m.get("role") == "tool":
                    msgs.append({"role": "user",
                                 "content": f"[result of {m.get('tool_name', 'tool')}]:\n"
                                            f"{m.get('content', '')}"})
                else:
                    msgs.append({"role": m["role"], "content": m.get("content", "")})
            return msgs
        msgs.extend(history)  # history already includes the just-saved user msg
        if not history or history[-1].get("content") != user_text:
            msgs.append({"role": "user", "content": user_text})
        return msgs

    def _stream_once(self, model: ModelInfo, messages: list[dict],
                     native_tools: bool, options: dict, think,
                     ui: TurnUI) -> tuple[str, list[dict], Optional[str]]:
        """One model request. Returns (text, tool_calls, error)."""
        tools = self.registry.specs() if native_tools else None
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        suppress_think = False
        try:
            kwargs = {"tools": tools, "options": options, "cancel": self.cancel}
            if model.provider.api == "ollama":
                kwargs["think"] = think
            for kind, payload in model.provider.stream_chat(
                    model.name, messages, **kwargs):
                if kind == "thinking":
                    ui.on_thinking(payload)
                elif kind == "token":
                    tok = payload
                    if "<think>" in tok:
                        # Some models think inline in the content stream;
                        # route it to the thinking channel, not the answer.
                        pre, _, tok = tok.partition("<think>")
                        if pre:
                            text_parts.append(pre)
                            ui.on_token(pre)
                        suppress_think = True
                    if suppress_think:
                        if "</think>" in tok:
                            inner, _, tok = tok.partition("</think>")
                            if inner:
                                ui.on_thinking(inner)
                            suppress_think = False
                        else:
                            if tok:
                                ui.on_thinking(tok)
                            continue
                    if tok:
                        text_parts.append(tok)
                        ui.on_token(tok)
                elif kind == "tool_calls":
                    tool_calls = payload
        except ToolsUnsupportedError:
            return "", [], "tools_unsupported"
        except ProviderError as e:
            return "".join(text_parts), [], str(e)
        text = _THINK_RX.sub("", "".join(text_parts))
        return text, tool_calls, None

    def _coerce_call(self, obj) -> Optional[dict]:
        """Accept {"tool": n, "args": a} and the {"name": n, "arguments": a}
        shape some models emit as text instead of a native call. Only names
        that are actually registered count — avoids eating ordinary JSON."""
        if not isinstance(obj, dict):
            return None
        name = obj.get("tool") or obj.get("name")
        if not isinstance(name, str) or name not in self.registry.names():
            return None
        args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {}
        return {"name": name, "arguments": args if isinstance(args, dict) else {}}

    def _parse_fallback_calls(self, text: str) -> tuple[list[dict], str]:
        """Extract a tool call a model wrote as plain text."""
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                call = self._coerce_call(json.loads(stripped))
                if call:
                    return [call], ""
            except json.JSONDecodeError:
                pass
        for m in _FALLBACK_CALL_RX.finditer(text):
            raw = m.group(1) or m.group(2)
            try:
                call = self._coerce_call(json.loads(raw))
            except json.JSONDecodeError:
                continue
            if call:
                cleaned = (text[:m.start()] + text[m.end():]).strip()
                return [call], cleaned
        return [], text

    def _persist_assistant(self, result: TurnResult) -> None:
        # The tool log goes in `meta`, never in the message content: stored
        # content is replayed into model context on later turns, and models
        # start imitating raw tool dumps in their replies if they see them.
        meta = ""
        if result.tool_log:
            meta = "; ".join(
                f"{name}({json.dumps(args, ensure_ascii=False)[:120]}) -> {out[:200]}"
                for name, args, out in result.tool_log)
        if result.text.strip():
            self.memory.add_message("assistant", result.text, meta=meta)
