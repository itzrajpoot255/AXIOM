"""System awareness and control — status, screen, vision, clipboard, input."""

from __future__ import annotations

import base64
import io
import os
import subprocess
import time


def _vision_describe(ctx, img, prompt: str) -> str:
    """Send a PIL image to the local vision model. Shared by screen + files."""
    vis = ctx.models.pick("vision") if ctx.models else None
    if vis is None:
        return ("No vision-capable model found. Pull one, e.g. "
                "`ollama pull gemma3` or `ollama pull llava`.")
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    from ..providers import OllamaProvider, ProviderError
    if not isinstance(vis.provider, OllamaProvider):
        return f"Vision model {vis.name} is on {vis.provider.name}, which lacks image support here."
    try:
        return vis.provider.chat_image(vis.name, prompt, b64) or "(empty response)"
    except ProviderError as e:
        return str(e)


def register(r) -> None:

    @r.register("system_status", "CPU/RAM/disk/battery/uptime snapshot; detail adds top processes",
                {"?detail": "boolean: include top processes"})
    def system_status(ctx, detail: bool = False) -> str:
        import psutil
        cpu = psutil.cpu_percent(interval=0.4)
        vm = psutil.virtual_memory()
        lines = [
            f"CPU: {cpu:.0f}% ({psutil.cpu_count(logical=True)} threads)",
            f"RAM: {vm.percent:.0f}% used ({vm.used/2**30:.1f}/{vm.total/2**30:.1f} GB)",
        ]
        for part in psutil.disk_partitions(all=False):
            try:
                du = psutil.disk_usage(part.mountpoint)
                lines.append(f"Disk {part.device} {du.percent:.0f}% used "
                             f"({du.free/2**30:.0f} GB free)")
            except OSError:
                continue
        batt = getattr(psutil, "sensors_battery", lambda: None)()
        if batt:
            state = "charging" if batt.power_plugged else "discharging"
            lines.append(f"Battery: {batt.percent:.0f}% ({state})")
        up = time.time() - psutil.boot_time()
        lines.append(f"Uptime: {up/3600:.1f} h")
        if detail:
            procs = sorted(psutil.process_iter(["name", "memory_info"]),
                           key=lambda p: p.info["memory_info"].rss if p.info["memory_info"] else 0,
                           reverse=True)[:8]
            lines.append("Top processes by RAM:")
            for p in procs:
                rss = p.info["memory_info"].rss / 2**20 if p.info["memory_info"] else 0
                lines.append(f"  {p.info['name'] or '?':<28} {rss:7.0f} MB")
        return "\n".join(lines)

    @r.register("screenshot", "Capture the screen to a PNG file, returns the path", {})
    def screenshot(ctx) -> str:
        try:
            from PIL import ImageGrab
        except ImportError:
            return "Pillow not installed — `pip install Pillow`."
        path = os.path.join(os.path.expanduser("~"), "Pictures",
                            f"arc_screen_{int(time.time())}.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ImageGrab.grab().save(path)
        return f"Screenshot saved: {path}"

    @r.register("describe_screen", "Analyse the current screen with a local vision model",
                {"?prompt": "string: what to look for"})
    def describe_screen(ctx, prompt: str = "Describe what is on this screen, concisely.") -> str:
        try:
            from PIL import ImageGrab
        except ImportError:
            return "Pillow not installed — `pip install Pillow`."
        return _vision_describe(ctx, ImageGrab.grab(), prompt)

    @r.register("read_image", "Look at an image file (photo, screenshot, scan, "
                "diagram) with the local vision model and describe or answer about it",
                {"path": "string: image file path",
                 "?prompt": "string: question about the image"})
    def read_image(ctx, path: str,
                   prompt: str = "Describe this image in detail, including any text in it.") -> str:
        try:
            from PIL import Image
        except ImportError:
            return "Pillow not installed — `pip install Pillow`."
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            return f"Not a file: {path}"
        try:
            img = Image.open(path)
        except Exception as e:
            return f"Could not open image: {e}"
        return _vision_describe(ctx, img, prompt)

    @r.register("clipboard_get", "Read the clipboard text", {})
    def clipboard_get(ctx) -> str:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                                capture_output=True, text=True, timeout=10)
        text = (result.stdout or "").strip()
        return text[:8000] if text else "(clipboard is empty or not text)"

    @r.register("clipboard_set", "Put text on the clipboard",
                {"text": "string: text to copy"})
    def clipboard_set(ctx, text: str) -> str:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                           input=text, capture_output=True, text=True, timeout=10)
        return "Copied to clipboard." if p.returncode == 0 else f"Clipboard failed: {p.stderr.strip()}"

    @r.register("set_volume", "Set system volume 0-100",
                {"level": "integer: 0-100"})
    def set_volume(ctx, level: int = 50) -> str:
        try:
            v = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return "Level must be 0-100."
        # 50 volume-down presses floor it, then half-steps up (each press = 2).
        ps = ("$w = New-Object -ComObject WScript.Shell; "
              "1..50 | % { $w.SendKeys([char]174) }; "
              f"1..{v // 2} | "
              "% { $w.SendKeys([char]175) }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15)
        return f"Volume set to ~{v}%."

    @r.register("type_text", "Type text into the focused window",
                {"text": "string: text to type"})
    def type_text(ctx, text: str) -> str:
        try:
            import pyautogui
        except ImportError:
            return "pyautogui not installed — `pip install pyautogui`."
        pyautogui.write(text, interval=0.02)
        return f"Typed {len(text)} chars."

    @r.register("press_keys", "Press a hotkey combination",
                {"keys": "string: e.g. ctrl+s or alt+tab"})
    def press_keys(ctx, keys: str) -> str:
        try:
            import pyautogui
        except ImportError:
            return "pyautogui not installed — `pip install pyautogui`."
        parts = [k.strip().lower() for k in keys.split("+") if k.strip()]
        if not parts:
            return "No keys given."
        pyautogui.hotkey(*parts)
        return f"Pressed {keys}."
