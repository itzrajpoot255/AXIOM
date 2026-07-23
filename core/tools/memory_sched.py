"""Memory, profile, scheduling, and model-introspection tools."""

from __future__ import annotations

import os


def register(r) -> None:

    # ── User profile (always in the system prompt) ───────────────

    @r.register("profile_set",
                "Save/update durable info about the user (name, preferences, "
                "projects, dates). Stays in context every turn.",
                {"key": "string: short key, e.g. name, editor, timezone",
                 "value": "string: the value"})
    def profile_set(ctx, key: str, value: str) -> str:
        if not key.strip() or not str(value).strip():
            return "Need both a key and a value."
        ctx.memory.profile_set(key, str(value))
        return f"Profile updated: {key.strip().lower()} = {value}"

    @r.register("profile_forget", "Remove a user-profile entry by key",
                {"key": "string: the profile key to remove"})
    def profile_forget(ctx, key: str) -> str:
        ok = ctx.memory.profile_delete(key)
        return f"Removed profile key '{key}'." if ok else f"No profile key '{key}'."

    # ── Chat export ──────────────────────────────────────────────

    @r.register("export_chat",
                "Save the current conversation to a markdown file",
                {"?path": "string: target file or directory; default ~/.axiom/chats"})
    def export_chat(ctx, path: str = "") -> str:
        name, text = ctx.memory.export_markdown()
        target = os.path.expanduser(path.strip() or "~/.axiom/chats")
        if not os.path.splitext(target)[1]:          # directory given
            os.makedirs(target, exist_ok=True)
            target = os.path.join(target, name)
        else:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
        return f"Chat saved to {target} ({len(text)} chars)."

    # ── Long-term memory ─────────────────────────────────────────

    @r.register("remember", "Save a durable fact about the user or their setup",
                {"fact": "string: the fact, self-contained",
                 "?topic": "string: short category"})
    def remember(ctx, fact: str, topic: str = "") -> str:
        if not fact.strip():
            return "Nothing to remember."
        fid = ctx.memory.remember(fact, topic)
        return f"Remembered (#{fid}): {fact}"

    @r.register("recall_memory", "Search saved facts",
                {"query": "string: what to look for"})
    def recall_memory(ctx, query: str) -> str:
        hits = ctx.memory.recall(query, limit=10)
        return ("Relevant facts:\n" + "\n".join(f"- {h}" for h in hits)
                if hits else "No saved facts match.")

    @r.register("forget", "Delete a saved fact by its id",
                {"fact_id": "integer: id from the facts list"})
    def forget(ctx, fact_id: int) -> str:
        ok = ctx.memory.forget(int(fact_id))
        return f"Forgot fact #{fact_id}." if ok else f"No fact #{fact_id}."

    # ── Scheduled tasks ──────────────────────────────────────────

    @r.register("schedule_task",
                "Schedule a prompt to run later: 'in 10 minutes', 'at 18:30', 'every 2 hours'",
                {"when": "string: time expression",
                 "prompt": "string: what AXIOM should do when it fires"})
    def schedule_task(ctx, when: str, prompt: str) -> str:
        return ctx.scheduler.schedule(when, prompt)

    @r.register("list_tasks", "List scheduled tasks", {})
    def list_tasks(ctx) -> str:
        return ctx.scheduler.describe()

    @r.register("cancel_task", "Cancel a scheduled task by id",
                {"task_id": "integer: id from list_tasks"})
    def cancel_task(ctx, task_id: int) -> str:
        return ctx.scheduler.cancel(int(task_id))

    # ── Models ───────────────────────────────────────────────────

    @r.register("list_models", "List locally available AI models and routing", {})
    def list_models(ctx) -> str:
        return ctx.models.summary() if ctx.models else "Model manager unavailable."
