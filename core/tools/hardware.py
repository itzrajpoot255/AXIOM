"""Hardware inspection — a rich, structured snapshot of the machine.

`snapshot()` returns a JSON-friendly dict the web dashboard polls a few
times a second: CPU (overall + per-core + freq + temp), memory, swap,
disks (usage + IO), GPUs (via nvidia-smi when present), battery,
network counters, and the heaviest processes. Everything degrades
gracefully — a missing sensor or absent GPU just leaves a field None
rather than raising.

Temperature is the awkward one on Windows: psutil.sensors_temperatures()
is usually empty there, so we fall back to nvidia-smi for the GPU and to
the LibreHardwareMonitor / OpenHardwareMonitor WMI namespace for the CPU
*if* one of those is running. None of that is required; absent it the
dashboard simply hides the temperature gauges.

The chat-facing `hardware_report` tool renders the same data as text so
the model can reason about it and the AI-recommendation endpoint can ask
"what should I do about this?".
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Optional

_GB = 2 ** 30
_MB = 2 ** 20

# nvidia-smi can be slow to spawn; cache its output briefly so a 1 Hz
# dashboard poll doesn't fork a process every tick.
_GPU_CACHE: dict = {"ts": 0.0, "data": None}
_GPU_TTL = 2.0
_CPU_TEMP_CACHE: dict = {"ts": 0.0, "data": None}
_CPU_TEMP_TTL = 5.0


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ── GPU (NVIDIA via nvidia-smi) ──────────────────────────────────────

def _nvidia_smi() -> Optional[list[dict]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    fields = ("name,temperature.gpu,utilization.gpu,utilization.memory,"
              "memory.used,memory.total,power.draw,power.limit,fan.speed")
    try:
        out = subprocess.run(
            [exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue

        def num(v):
            try:
                return float(v)
            except ValueError:
                return None
        gpus.append({
            "name": parts[0],
            "temp": num(parts[1]),
            "util": num(parts[2]),
            "mem_util": num(parts[3]),
            "mem_used_mb": num(parts[4]),
            "mem_total_mb": num(parts[5]),
            "power_w": num(parts[6]),
            "power_limit_w": num(parts[7]),
            "fan_pct": num(parts[8]),
            "vendor": "nvidia",
        })
    return gpus or None


def _gpus() -> Optional[list[dict]]:
    now = time.time()
    if now - _GPU_CACHE["ts"] < _GPU_TTL:
        return _GPU_CACHE["data"]
    data = _nvidia_smi()
    _GPU_CACHE.update(ts=now, data=data)
    return data


# ── CPU temperature (best effort, Windows-aware) ─────────────────────

def _cpu_temp() -> Optional[float]:
    now = time.time()
    if now - _CPU_TEMP_CACHE["ts"] < _CPU_TEMP_TTL:
        return _CPU_TEMP_CACHE["data"]
    temp = _cpu_temp_psutil() or _cpu_temp_wmi()
    _CPU_TEMP_CACHE.update(ts=now, data=temp)
    return temp


def _cpu_temp_psutil() -> Optional[float]:
    import psutil
    fn = getattr(psutil, "sensors_temperatures", None)
    if not fn:
        return None
    try:
        temps = fn()
    except Exception:
        return None
    if not temps:
        return None
    # Prefer a package/core sensor; otherwise take the first reading we see.
    for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        if key in temps and temps[key]:
            return round(max(t.current for t in temps[key] if t.current), 1)
    for entries in temps.values():
        for t in entries:
            if t.current:
                return round(t.current, 1)
    return None


def _cpu_temp_wmi() -> Optional[float]:
    """Read CPU temp from LibreHardwareMonitor / OpenHardwareMonitor if one
    is running (they expose a WMI namespace). Silent no-op otherwise."""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$s=Get-CimInstance -Namespace root/LibreHardwareMonitor -Class Sensor "
        "  -ErrorAction SilentlyContinue;"
        "if(-not $s){$s=Get-CimInstance -Namespace root/OpenHardwareMonitor -Class Sensor "
        "  -ErrorAction SilentlyContinue}"
        "$t=$s|Where-Object{$_.SensorType -eq 'Temperature' -and $_.Name -match 'CPU'}"
        "  |Sort-Object Value -Descending|Select-Object -First 1 -ExpandProperty Value;"
        "if($t){'{0:N1}' -f $t}"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=6)
    except Exception:
        return None
    val = (out.stdout or "").strip()
    try:
        return float(val) if val else None
    except ValueError:
        return None


# ── Snapshot ─────────────────────────────────────────────────────────

def snapshot(top_n: int = 6) -> dict:
    """Full structured hardware reading. JSON-serialisable."""
    import psutil

    cpu_overall = psutil.cpu_percent(interval=0.15)
    per_core = _safe(lambda: psutil.cpu_percent(percpu=True), []) or []
    freq = _safe(lambda: psutil.cpu_freq())
    load = _safe(lambda: psutil.getloadavg())

    vm = psutil.virtual_memory()
    sm = _safe(lambda: psutil.swap_memory())

    disks = []
    for part in _safe(lambda: psutil.disk_partitions(all=False), []) or []:
        du = _safe(lambda: psutil.disk_usage(part.mountpoint))
        if not du:
            continue
        disks.append({
            "device": part.device, "mount": part.mountpoint,
            "fstype": part.fstype, "percent": du.percent,
            "used_gb": round(du.used / _GB, 1), "total_gb": round(du.total / _GB, 1),
            "free_gb": round(du.free / _GB, 1),
        })
    dio = _safe(lambda: psutil.disk_io_counters())
    nio = _safe(lambda: psutil.net_io_counters())

    batt = _safe(lambda: psutil.sensors_battery())
    battery = None
    if batt is not None:
        secs = batt.secsleft
        battery = {
            "percent": round(batt.percent, 0),
            "plugged": bool(batt.power_plugged),
            "secsleft": None if secs in (None, -1, -2) else int(secs),
        }

    procs = []
    for p in _safe(lambda: list(psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_info"])), []) or []:
        info = p.info
        mi = info.get("memory_info")
        procs.append({
            "pid": info.get("pid"),
            "name": info.get("name") or "?",
            "cpu": round(info.get("cpu_percent") or 0.0, 1),
            "ram_mb": round(mi.rss / _MB, 0) if mi else 0,
        })
    top_ram = sorted(procs, key=lambda x: -x["ram_mb"])[:top_n]
    top_cpu = sorted(procs, key=lambda x: -x["cpu"])[:top_n]

    return {
        "ts": time.time(),
        "cpu": {
            "percent": cpu_overall,
            "per_core": [round(c, 0) for c in per_core],
            "cores_physical": _safe(lambda: psutil.cpu_count(logical=False)),
            "cores_logical": _safe(lambda: psutil.cpu_count(logical=True)),
            "freq_mhz": round(freq.current, 0) if freq else None,
            "freq_max_mhz": round(freq.max, 0) if freq and freq.max else None,
            "temp_c": _cpu_temp(),
            "load_avg": [round(x, 2) for x in load] if load else None,
        },
        "memory": {
            "percent": vm.percent,
            "used_gb": round(vm.used / _GB, 1),
            "total_gb": round(vm.total / _GB, 1),
            "available_gb": round(vm.available / _GB, 1),
        },
        "swap": ({"percent": sm.percent, "used_gb": round(sm.used / _GB, 1),
                  "total_gb": round(sm.total / _GB, 1)} if sm and sm.total else None),
        "disks": disks,
        "disk_io": ({"read_mb": round(dio.read_bytes / _MB, 0),
                     "write_mb": round(dio.write_bytes / _MB, 0)} if dio else None),
        "net_io": ({"sent_mb": round(nio.bytes_sent / _MB, 0),
                    "recv_mb": round(nio.bytes_recv / _MB, 0)} if nio else None),
        "gpus": _gpus(),
        "battery": battery,
        "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1),
        "top_ram": top_ram,
        "top_cpu": top_cpu,
    }


def report_text(snap: Optional[dict] = None) -> str:
    """Human/model-readable rendering of a snapshot."""
    s = snap or snapshot()
    c, m = s["cpu"], s["memory"]
    lines = [
        f"CPU: {c['percent']:.0f}% over {c['cores_logical']} threads"
        + (f", {c['freq_mhz']:.0f} MHz" if c.get("freq_mhz") else "")
        + (f", {c['temp_c']:.0f}°C" if c.get("temp_c") else ""),
        f"RAM: {m['percent']:.0f}% ({m['used_gb']:.1f}/{m['total_gb']:.1f} GB, "
        f"{m['available_gb']:.1f} GB free)",
    ]
    if s.get("swap"):
        sw = s["swap"]
        lines.append(f"Swap: {sw['percent']:.0f}% ({sw['used_gb']:.1f}/{sw['total_gb']:.1f} GB)")
    for d in s["disks"]:
        lines.append(f"Disk {d['device']} {d['percent']:.0f}% used "
                     f"({d['free_gb']:.0f} GB free of {d['total_gb']:.0f})")
    for g in s.get("gpus") or []:
        bits = [f"GPU {g['name']}"]
        if g.get("util") is not None:
            bits.append(f"{g['util']:.0f}% util")
        if g.get("mem_used_mb") and g.get("mem_total_mb"):
            bits.append(f"{g['mem_used_mb']/1024:.1f}/{g['mem_total_mb']/1024:.1f} GB VRAM")
        if g.get("temp") is not None:
            bits.append(f"{g['temp']:.0f}°C")
        if g.get("power_w") is not None:
            bits.append(f"{g['power_w']:.0f} W")
        lines.append(", ".join(bits))
    if s.get("battery"):
        b = s["battery"]
        state = "charging" if b["plugged"] else "on battery"
        lines.append(f"Battery: {b['percent']:.0f}% ({state})")
    lines.append(f"Uptime: {s['uptime_h']:.1f} h")
    if s.get("top_ram"):
        lines.append("Top RAM: " + ", ".join(
            f"{p['name']} {p['ram_mb']:.0f}MB" for p in s["top_ram"][:5]))
    return "\n".join(lines)


RECOMMEND_SYSTEM = (
    "You are a hardware-optimization advisor inside a local assistant. "
    "Given a one-shot snapshot of a Windows PC's CPU, GPU, memory, disks, "
    "temperatures and heaviest processes, give concise, concrete advice. "
    "Rules: 3-6 bullet points, each one short sentence starting with an "
    "action verb. Call out anything actually concerning (high temps >85°C, "
    "RAM >90%, a disk under ~10% free, a runaway process). If everything is "
    "healthy, say so in one line and offer at most two optional tweaks. "
    "Name specific processes/drives from the data. No preamble, no markdown "
    "headings — just the bullets."
)


def register(r) -> None:

    @r.register("hardware_report",
                "Detailed hardware snapshot: CPU/GPU/RAM/disk/temps/top processes",
                {})
    def hardware_report(ctx) -> str:
        return report_text()

    @r.register("gpu_status", "GPU utilisation, VRAM, temperature and power (NVIDIA)", {})
    def gpu_status(ctx) -> str:
        gpus = _gpus()
        if not gpus:
            return "No NVIDIA GPU detected (nvidia-smi not found or no card)."
        out = []
        for g in gpus:
            out.append(
                f"{g['name']}: {g.get('util', 0):.0f}% util, "
                f"{(g.get('mem_used_mb') or 0)/1024:.1f}/"
                f"{(g.get('mem_total_mb') or 0)/1024:.1f} GB VRAM"
                + (f", {g['temp']:.0f}°C" if g.get("temp") is not None else "")
                + (f", {g['power_w']:.0f}/{g.get('power_limit_w') or 0:.0f} W"
                   if g.get("power_w") is not None else ""))
        return "\n".join(out)
