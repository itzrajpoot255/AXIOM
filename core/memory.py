"""Persistent memory — SQLite, stdlib only.

Tables:
    sessions  — one row per conversation
    messages  — full transcript (history survives restarts, --resume)
    facts     — long-term knowledge the agent saves via memory tools
    profile   — durable key/value facts about the user, always in context
    tasks     — scheduled prompts (core.scheduler)

Fact recall is keyword-overlap scoring: deterministic, zero RAM, no
embedding model required. Swap in embeddings later if one is present —
the interface (`recall`) won't change.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import Optional

from .config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "memory.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts REAL NOT NULL,
    title TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    content TEXT NOT NULL,
    topic TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    due_ts REAL NOT NULL,
    repeat_seconds REAL,
    prompt TEXT NOT NULL,
    enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts REAL NOT NULL
);
"""

_WORD = re.compile(r"[a-zA-Z0-9_]{3,}")
# Older versions appended the tool log to the stored assistant message;
# strip it from anything we hand back so it never re-enters model context.
_TOOL_LOG_RX = re.compile(r"\n\[tools used: .*\]$", re.DOTALL)


class Memory:
    def __init__(self, db_path: str = DB_PATH) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(messages)")]
            if "meta" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT DEFAULT ''")
            self._conn.commit()
        self.session_id: int = 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Sessions / messages ──────────────────────────────────────

    def new_session(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (started_ts) VALUES (?)", (time.time(),))
            self._conn.commit()
            self.session_id = cur.lastrowid or 0
        return self.session_id

    def resume_last_session(self) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            self.session_id = row[0]
            return self.session_id
        return None

    def switch_session(self, session_id: int) -> bool:
        """Continue an earlier conversation. False if it doesn't exist."""
        with self._lock:
            row = self._conn.execute("SELECT id FROM sessions WHERE id=?",
                                     (session_id,)).fetchone()
        if not row:
            return False
        self.session_id = session_id
        return True

    def sessions(self, limit: int = 60) -> list[dict]:
        """Conversations, newest first. Empty sessions are skipped; the
        title falls back to the first user message."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.started_ts, s.title,"
                " (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id),"
                " (SELECT content FROM messages m WHERE m.session_id=s.id"
                "    AND m.role='user' ORDER BY m.id LIMIT 1)"
                " FROM sessions s ORDER BY s.id DESC LIMIT ?", (limit * 3,)).fetchall()
        out = []
        for sid, ts, title, count, first_user in rows:
            if count == 0 and sid != self.session_id:
                continue
            label = (title or (first_user or "").strip().split("\n")[0][:70]
                     or "(empty)")
            out.append({"id": sid, "ts": ts, "title": label, "messages": count})
            if len(out) >= limit:
                break
        return out

    def add_message(self, role: str, content: str, meta: str = "") -> None:
        """`meta` holds out-of-band detail (e.g. the tool log) that must
        never re-enter model context or the visible transcript."""
        if not self.session_id:
            self.new_session()
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (session_id, ts, role, content, meta) "
                "VALUES (?,?,?,?,?)",
                (self.session_id, time.time(), role, content, meta))
            self._conn.commit()

    def recent_messages(self, limit: int, max_chars: int = 4000) -> list[dict]:
        """Last `limit` messages of the current session, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? "
                "ORDER BY id DESC LIMIT ?", (self.session_id, limit)).fetchall()
        out = []
        for role, content in reversed(rows):
            content = _TOOL_LOG_RX.sub("", content)
            if len(content) > max_chars:
                content = content[:max_chars] + "\n...[truncated]"
            out.append({"role": role, "content": content})
        return out

    def session_messages(self, session_id: Optional[int] = None) -> list[dict]:
        """Full transcript of a session (default: current), oldest first."""
        sid = session_id or self.session_id
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, role, content FROM messages WHERE session_id=? "
                "ORDER BY id", (sid,)).fetchall()
        return [{"ts": ts, "role": role, "content": _TOOL_LOG_RX.sub("", content)}
                for ts, role, content in rows]

    def export_markdown(self, session_id: Optional[int] = None) -> tuple[str, str]:
        """Render a session as markdown. Returns (suggested_filename, text)."""
        sid = session_id or self.session_id
        msgs = self.session_messages(sid)
        started = time.strftime("%Y-%m-%d %H:%M",
                                time.localtime(msgs[0]["ts"] if msgs else time.time()))
        lines = [f"# AXIOM — session {sid}", f"_{started}_", ""]
        for m in msgs:
            who = "You" if m["role"] == "user" else "AXIOM"
            at = time.strftime("%H:%M", time.localtime(m["ts"]))
            lines.append(f"**{who}** · {at}\n\n{m['content'].strip()}\n")
        name = f"axiom-session-{sid}-{time.strftime('%Y%m%d-%H%M')}.md"
        return name, "\n".join(lines)

    # ── Facts (long-term) ────────────────────────────────────────

    def remember(self, content: str, topic: str = "") -> int:
        content = content.strip()
        with self._lock:
            dup = self._conn.execute(
                "SELECT id FROM facts WHERE content=?", (content,)).fetchone()
            if dup:
                return dup[0]
            cur = self._conn.execute(
                "INSERT INTO facts (ts, content, topic) VALUES (?,?,?)",
                (time.time(), content, topic.strip()))
            self._conn.commit()
            return cur.lastrowid or 0

    def forget(self, fact_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def all_facts(self) -> list[tuple[int, str, str]]:
        with self._lock:
            return self._conn.execute(
                "SELECT id, content, topic FROM facts ORDER BY id").fetchall()

    def recall(self, query: str, limit: int = 8) -> list[str]:
        """Facts ranked by keyword overlap with the query."""
        facts = self.all_facts()
        if not facts:
            return []
        q_words = set(w.lower() for w in _WORD.findall(query))
        scored = []
        for _id, content, topic in facts:
            f_words = set(w.lower() for w in _WORD.findall(content + " " + topic))
            score = len(q_words & f_words)
            scored.append((score, content))
        scored.sort(key=lambda x: -x[0])
        # Always include a few recent facts even with zero overlap so the
        # model knows the user's standing context (name, role, ...).
        hits = [c for s, c in scored if s > 0][:limit]
        if len(hits) < limit:
            for _id, content, _topic in reversed(facts):
                if content not in hits:
                    hits.append(content)
                if len(hits) >= limit:
                    break
        return hits[:limit]

    # ── User profile (always-in-context personal facts) ──────────

    def profile_set(self, key: str, value: str) -> None:
        key, value = key.strip().lower(), value.strip()
        if not key or not value:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO profile (key, value, ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (key, value, time.time()))
            self._conn.commit()

    def profile_delete(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM profile WHERE key=?",
                                     (key.strip().lower(),))
            self._conn.commit()
            return cur.rowcount > 0

    def profile_all(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM profile ORDER BY key").fetchall()
        return dict(rows)

    # ── Scheduled tasks (used by core.scheduler) ─────────────────

    def add_task(self, due_ts: float, prompt: str, repeat_seconds: Optional[float]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks (due_ts, repeat_seconds, prompt) VALUES (?,?,?)",
                (due_ts, repeat_seconds, prompt))
            self._conn.commit()
            return cur.lastrowid or 0

    def update_task_due(self, task_id: int, due_ts: float) -> None:
        with self._lock:
            self._conn.execute("UPDATE tasks SET due_ts=? WHERE id=?", (due_ts, task_id))
            self._conn.commit()

    def disable_task(self, task_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE tasks SET enabled=0 WHERE id=?", (task_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def enabled_tasks(self) -> list[tuple[int, float, Optional[float], str]]:
        with self._lock:
            return self._conn.execute(
                "SELECT id, due_ts, repeat_seconds, prompt FROM tasks WHERE enabled=1"
            ).fetchall()
