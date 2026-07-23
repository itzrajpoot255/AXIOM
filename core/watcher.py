"""System watcher — edge-triggered resource alerts.

One slow poll (30 s default) reading four psutil counters; an alert
fires only when a metric crosses its threshold (rising edge) and
re-arms after recovery or 30 minutes. Idle cost is effectively zero —
this replaces the old 1 Hz vitals poller + 20 fps HUD repaint.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

_REARM_SECONDS = 1800.0


class SystemWatcher:
    def __init__(self, cfg, notify: Callable[[str], None]) -> None:
        self.cfg = cfg
        self.notify = notify
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # metric -> (currently_over, last_alert_ts)
        self._state: dict[str, tuple[bool, float]] = {}

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="system-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        try:
            import psutil
        except ImportError:
            print("[watcher] psutil missing — watcher disabled")
            return
        interval = float(self.cfg.watcher_interval)
        # First cpu_percent call primes the counter.
        psutil.cpu_percent(interval=None)
        while not self._stop.wait(interval):
            try:
                self._check(psutil)
            except Exception:
                pass

    def _check(self, psutil) -> None:
        vm = psutil.virtual_memory()
        self._edge("ram", vm.percent >= self.cfg.watcher_ram_pct,
                   f"Memory pressure: RAM at {vm.percent:.0f}%. "
                   "Consider closing something heavy.")

        cpu = psutil.cpu_percent(interval=None)
        self._edge("cpu", cpu >= self.cfg.watcher_cpu_pct,
                   f"Sustained CPU load: {cpu:.0f}%.")

        try:
            du = psutil.disk_usage("/")
            free_pct = 100.0 - du.percent
            self._edge("disk", free_pct <= self.cfg.watcher_disk_free_pct,
                       f"Low disk space: only {du.free/2**30:.0f} GB free "
                       f"({free_pct:.0f}%).")
        except OSError:
            pass

        batt = getattr(psutil, "sensors_battery", lambda: None)()
        if batt is not None:
            low = batt.percent <= self.cfg.watcher_battery_pct and not batt.power_plugged
            self._edge("battery", low,
                       f"Battery at {batt.percent:.0f}% and discharging, sir.")

    def _edge(self, key: str, over: bool, message: str) -> None:
        was_over, last_alert = self._state.get(key, (False, 0.0))
        now = time.time()
        if over and (not was_over or now - last_alert > _REARM_SECONDS):
            self._state[key] = (True, now)
            self.notify(message)
        elif not over and was_over:
            self._state[key] = (False, last_alert)
