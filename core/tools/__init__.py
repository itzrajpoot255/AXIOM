"""Tool registry — the agent's hands.

Each tool is a plain function taking (ctx, **kwargs) and returning a
string. Registration captures a terse JSON-schema spec used both for
native model tool-calling and for the prompt-injected fallback
protocol. Descriptions are deliberately short: every tool spec rides
along on every model request.

Plugins: any ``plugins/*.py`` file exporting ``register(registry, ctx)``
is loaded at startup and may add its own tools.
"""

from __future__ import annotations

import importlib.util
import os
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ToolContext:
    """Services tools may need. Wired once in app startup."""
    cfg: Any = None
    memory: Any = None
    models: Any = None          # ModelManager
    scheduler: Any = None
    confirm: Callable[[str], bool] = lambda prompt: True
    notify: Callable[[str], None] = print
    # One-shot model completion (system, user) -> text. Wired by the
    # assistant; lets tools ask the local model to summarize/draft without
    # the chat model having to orchestrate it. Default is a no-op stub.
    complete: Callable[[str, str], str] = lambda system, user: ""
    # Called as (path, before_text, after_text) whenever a tool writes a
    # file — the assistant turns it into a diff event for the IDE view.
    on_file_change: Callable[[str, str, str], None] = lambda path, before, after: None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable
    needs_confirm: bool = False


class ToolRegistry:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, parameters: Optional[dict] = None,
                 needs_confirm: bool = False):
        """Decorator: @registry.register("read_file", "...", {"path": "str: file path"})

        `parameters` maps param name -> "type: description" shorthand,
        with "?" prefix marking optional params.
        """
        def deco(func: Callable) -> Callable:
            props, required = {}, []
            for pname, pdesc in (parameters or {}).items():
                optional = pname.startswith("?")
                key = pname.lstrip("?")
                ptype, _, pdoc = pdesc.partition(":")
                props[key] = {"type": ptype.strip() or "string",
                              "description": pdoc.strip()}
                if not optional:
                    required.append(key)
            schema = {"type": "object", "properties": props}
            if required:
                schema["required"] = required
            self._tools[name] = Tool(name, description, schema, func, needs_confirm)
            return func
        return deco

    # ── Introspection ────────────────────────────────────────────

    def specs(self) -> list[dict]:
        """Native tool-calling format (Ollama / OpenAI compatible)."""
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.parameters}}
                for t in self._tools.values()]

    def docs(self) -> str:
        """Compact docs for the prompt-injected fallback protocol."""
        lines = []
        for t in self._tools.values():
            params = ", ".join(t.parameters.get("properties", {}).keys())
            lines.append(f"- {t.name}({params}): {t.description}")
        return "\n".join(lines)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    # ── Execution ────────────────────────────────────────────────

    def _coerce_args(self, tool: Tool, args: dict) -> dict:
        """Models often send "true"/"5" as strings — coerce per schema."""
        props = tool.parameters.get("properties", {})
        out = {}
        for key, value in args.items():
            ptype = props.get(key, {}).get("type")
            if ptype == "boolean" and isinstance(value, str):
                value = value.strip().lower() in ("true", "1", "yes", "on")
            elif ptype == "integer" and isinstance(value, str):
                try:
                    value = int(float(value))
                except ValueError:
                    pass
            out[key] = value
        return out

    def execute(self, name: str, args: dict) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}. Available: {', '.join(self.names())}"
        args = self._coerce_args(tool, args)
        if tool.needs_confirm:
            summary = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items())
            if not self.ctx.confirm(f"{name}({summary})"):
                return "User declined — action not performed."
        try:
            result = tool.func(self.ctx, **args)
            return result if isinstance(result, str) else repr(result)
        except TypeError as e:
            return f"Bad arguments for {name}: {e}"
        except Exception as e:
            return f"Tool {name} failed: {type(e).__name__}: {e}"


def build_registry(ctx: ToolContext) -> ToolRegistry:
    """Register all built-in tools plus plugins."""
    registry = ToolRegistry(ctx)
    from . import files, shell, system, web, dev, memory_sched, hardware
    files.register(registry)
    shell.register(registry)
    system.register(registry)
    web.register(registry)
    dev.register(registry)
    memory_sched.register(registry)
    hardware.register(registry)

    email_cfg = (ctx.cfg.email or {}) if ctx.cfg else {}
    if email_cfg.get("user") and email_cfg.get("imap_host"):
        from . import email_
        email_.register(registry)

    _load_plugins(registry)
    return registry


def _load_plugins(registry: ToolRegistry) -> None:
    plugins_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "plugins")
    if not os.path.isdir(plugins_dir):
        return
    for fn in sorted(os.listdir(plugins_dir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(plugins_dir, fn)
        try:
            spec = importlib.util.spec_from_file_location(f"axiom_plugin_{fn[:-3]}", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "register"):
                mod.register(registry, registry.ctx)
                print(f"[plugins] loaded {fn}")
        except Exception:
            print(f"[plugins] failed to load {fn}:\n{traceback.format_exc(limit=2)}")
