"""Daily briefing — one command that stitches together tools AXIOM already
has (weather, news, hardware, open notes, scheduled tasks) into a single
morning summary. No new dependency, no paid API, no extra model calls
beyond the one the chat model already makes to read the result to you.

Tool added:
  daily_briefing(?city)  -> weather + headlines + machine health + open notes

Pair it with the existing scheduler for a real "morning briefing" habit,
e.g. in chat: "every day at 8am give me my daily briefing".
"""

from __future__ import annotations


def register(registry, ctx) -> None:

    @registry.register(
        "daily_briefing",
        "One-shot morning summary: weather, top news, machine health and open notes",
        {"?city": "string: city for weather (default: config news_region / auto)"},
    )
    def daily_briefing(ctx, city: str = "") -> str:
        sections = []

        weather = registry.execute("weather", {"city": city} if city else {})
        sections.append(f"Weather:\n{weather}")

        news = registry.execute("news_headlines", {})
        sections.append(f"Top headlines:\n{news}")

        hw = registry.execute("hardware_report", {})
        sections.append(f"Machine health:\n{hw}")

        notes = registry.execute("list_notes", {"show_done": False})
        sections.append(f"Open notes:\n{notes}")

        tasks = registry.execute("list_tasks", {})
        sections.append(f"Scheduled tasks:\n{tasks}")

        return "\n\n".join(sections)
