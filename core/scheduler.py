"""Scheduled tasks — event-driven via threading.Timer, persisted in SQLite.

No polling loop: each enabled task gets one Timer armed for its due
time. When it fires, the prompt is handed to the agent runner the app
provides. Repeating tasks re-arm themselves.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

_IN_RX = re.compile(r"in\s+(\d+(?:\.\d+)?)\s*(second|sec|s|minute|min|m|hour|hr|h|day|d)s?\b", re.I)
_AT_RX = re.compile(r"at\s+(\d{1,2}):(\d{2})\s*(am|pm)?", re.I)
_EVERY_RX = re.compile(r"every\s+(\d+(?:\.\d+)?)?\s*(second|sec|minute|min|hour|hr|day)s?\b", re.I)

_UNIT_SECONDS = {"second": 1, "sec": 1, "s": 1, "minute": 60, "min": 60, "m": 60,
                 "hour": 3600, "hr": 3600, "h": 3600, "day": 86400, "d": 86400}


def parse_when(expr: str) -> tuple[Optional[float], Optional[float]]:
    """'in 10 minutes' / 'at 18:30' / 'every 2 hours' -> (due_ts, repeat_s)."""
    expr = expr.strip().lower()
    now = time.time()

    m = _EVERY_RX.search(expr)
    if m:
        qty = float(m.group(1) or 1)
        interval = qty * _UNIT_SECONDS[m.group(2)]
        interval = max(30.0, interval)
        return now + interval, interval

    m = _IN_RX.search(expr)
    if m:
        delay = float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
        return now + max(5.0, delay), None

    m = _AT_RX.search(expr)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if m.group(3) and m.group(3).lower() == "pm" and hour < 12:
            hour += 12
        if m.group(3) and m.group(3).lower() == "am" and hour == 12:
            hour = 0
        target = datetime.now().replace(hour=hour % 24, minute=minute,
                                        second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp(), None

    return None, None


class Scheduler:
    def __init__(self, memory, run_prompt: Callable[[str], None]) -> None:
        self.memory = memory
        self.run_prompt = run_prompt        # app injects: feeds prompt to agent
        self._timers: dict[int, threading.Timer] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        """Arm timers for everything persisted (catch up on overdue ones)."""
        for task_id, due_ts, repeat_s, prompt in self.memory.enabled_tasks():
            self._arm(task_id, due_ts, repeat_s, prompt)

    def stop(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()

    def schedule(self, when: str, prompt: str) -> str:
        due_ts, repeat_s = parse_when(when)
        if due_ts is None:
            return ("Couldn't parse the time. Use 'in 10 minutes', "
                    "'at 18:30', or 'every 2 hours'.")
        task_id = self.memory.add_task(due_ts, prompt, repeat_s)
        self._arm(task_id, due_ts, repeat_s, prompt)
        nice = time.strftime("%H:%M:%S", time.localtime(due_ts))
        rep = f", repeating every {repeat_s:g}s" if repeat_s else ""
        return f"Task #{task_id} scheduled for {nice}{rep}: {prompt}"

    def cancel(self, task_id: int) -> str:
        with self._lock:
            timer = self._timers.pop(task_id, None)
        if timer:
            timer.cancel()
        ok = self.memory.disable_task(task_id)
        return f"Task #{task_id} cancelled." if ok or timer else f"No task #{task_id}."

    def describe(self) -> str:
        tasks = self.memory.enabled_tasks()
        if not tasks:
            return "No scheduled tasks."
        lines = []
        for task_id, due_ts, repeat_s, prompt in tasks:
            nice = time.strftime("%a %H:%M:%S", time.localtime(due_ts))
            rep = f" (every {repeat_s:g}s)" if repeat_s else ""
            lines.append(f"  #{task_id} {nice}{rep}: {prompt}")
        return "Scheduled tasks:\n" + "\n".join(lines)

    # ── Internals ────────────────────────────────────────────────

    def _arm(self, task_id: int, due_ts: float, repeat_s: Optional[float],
             prompt: str) -> None:
        delay = max(0.5, due_ts - time.time())
        timer = threading.Timer(delay, self._fire, args=(task_id, repeat_s, prompt))
        timer.daemon = True
        with self._lock:
            old = self._timers.pop(task_id, None)
            if old:
                old.cancel()
            self._timers[task_id] = timer
        timer.start()

    def _fire(self, task_id: int, repeat_s: Optional[float], prompt: str) -> None:
        with self._lock:
            self._timers.pop(task_id, None)
        if repeat_s:
            next_due = time.time() + repeat_s
            self.memory.update_task_due(task_id, next_due)
            self._arm(task_id, next_due, repeat_s, prompt)
        else:
            self.memory.disable_task(task_id)
        try:
            self.run_prompt(prompt)
        except Exception as e:
            print(f"[scheduler] task #{task_id} failed: {e}")
