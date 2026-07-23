"""Quick notes / to-do list — a small, genuinely useful add-on for AXIOM.

100% local, 100% free: notes are just a JSON file in ~/.axiom/notes.json.
No model, no network, no API key involved in storing or listing them —
only reading them back to you goes through the chat model, same as any
other tool result.

Tools added:
  add_note(text)          -> save a note/reminder
  list_notes(?show_done)  -> list open notes (or everything, done included)
  done_note(note_id)      -> mark a note as completed
  delete_note(note_id)    -> remove a note for good
"""

from __future__ import annotations

import json
import os
import time

from core.config import DATA_DIR

_NOTES_PATH = os.path.join(DATA_DIR, "notes.json")


def _load() -> list[dict]:
    try:
        with open(_NOTES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(notes: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_NOTES_PATH, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2)


def register(registry, ctx) -> None:

    @registry.register("add_note", "Save a quick note or reminder for later",
                        {"text": "string: the note text"})
    def add_note(ctx, text: str) -> str:
        notes = _load()
        note_id = (max((n["id"] for n in notes), default=0)) + 1
        notes.append({
            "id": note_id,
            "text": text.strip(),
            "done": False,
            "created": time.strftime("%Y-%m-%d %H:%M"),
        })
        _save(notes)
        return f"Saved note #{note_id}: {text.strip()}"

    @registry.register("list_notes", "List saved notes",
                        {"?show_done": "boolean: include completed notes (default false)"})
    def list_notes(ctx, show_done: bool = False) -> str:
        notes = _load()
        if not show_done:
            notes = [n for n in notes if not n["done"]]
        if not notes:
            return "No notes." if show_done else "No open notes."
        lines = []
        for n in notes:
            mark = "x" if n["done"] else " "
            lines.append(f"[{mark}] #{n['id']} {n['text']}  ({n['created']})")
        return "\n".join(lines)

    @registry.register("done_note", "Mark a note as completed",
                        {"note_id": "integer: the note id"})
    def done_note(ctx, note_id: int) -> str:
        notes = _load()
        for n in notes:
            if n["id"] == int(note_id):
                n["done"] = True
                _save(notes)
                return f"Note #{note_id} marked done."
        return f"No note #{note_id}."

    @registry.register("delete_note", "Permanently delete a note",
                        {"note_id": "integer: the note id"})
    def delete_note(ctx, note_id: int) -> str:
        notes = _load()
        kept = [n for n in notes if n["id"] != int(note_id)]
        if len(kept) == len(notes):
            return f"No note #{note_id}."
        _save(kept)
        return f"Note #{note_id} deleted."
