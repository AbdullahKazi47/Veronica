"""
System-interaction tools. Cross-platform where reasonably possible; Windows-specific
volume control (the one thing that genuinely needs a platform API) degrades gracefully
on other OSes instead of crashing, unlike the reference repos.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.system")

_OPEN_APP_PATTERN = re.compile(r"\bopen\s+([a-z0-9 \-_]+)\b", re.I)

# A small, extensible map of friendly names -> how to launch them per-OS.
_APP_LAUNCHERS: dict[str, dict[str, str]] = {
    "notepad": {"Windows": "notepad.exe"},
    "calculator": {"Windows": "calc.exe", "Darwin": "open -a Calculator", "Linux": "gnome-calculator"},
    "chrome": {"Windows": "start chrome", "Darwin": "open -a 'Google Chrome'", "Linux": "google-chrome"},
    "explorer": {"Windows": "explorer.exe", "Darwin": "open .", "Linux": "xdg-open ."},
    "documents": {"Windows": "explorer.exe shell:Personal", "Darwin": "open ~/Documents", "Linux": "xdg-open ~/Documents"},
    "downloads": {"Windows": "explorer.exe shell:Downloads", "Darwin": "open ~/Downloads", "Linux": "xdg-open ~/Downloads"},
}


class OpenApplicationTool(Tool):
    name = "open_application"
    description = "Open a known application or system folder by friendly name (e.g. 'chrome', 'notepad', 'downloads')."
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"app_name": {"type": "string", "description": "Friendly application/folder name."}},
        "required": ["app_name"],
    }

    def matches(self, text: str) -> bool:
        m = _OPEN_APP_PATTERN.search(text)
        if not m:
            return False
        candidate = m.group(1).strip().lower()
        # avoid stealing "open <url/website>" from OpenWebsiteTool
        return any(candidate.startswith(k) or k in candidate for k in _APP_LAUNCHERS)

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _OPEN_APP_PATTERN.search(text)
        return {"app_name": m.group(1).strip().lower() if m else ""}

    def run(self, app_name: str = "", **kwargs: Any) -> ToolResult:
        key = next((k for k in _APP_LAUNCHERS if app_name.startswith(k) or k in app_name), None)
        if key is None:
            return ToolResult.fail(f"I don't know how to open '{app_name}' yet. Add it to _APP_LAUNCHERS.")

        system = platform.system()
        command = _APP_LAUNCHERS[key].get(system)
        if not command:
            return ToolResult.fail(f"'{key}' has no launcher configured for {system}.")

        try:
            if system == "Windows":
                subprocess.Popen(command, shell=True)
            else:
                subprocess.Popen(command.split())
            return ToolResult.ok(f"Opening {key}.")
        except Exception as e:
            logger.exception("Failed to open %s", key)
            return ToolResult.fail(f"Couldn't open {key}: {e}")


class SetVolumeTool(Tool):
    name = "set_volume"
    description = "Set the system output volume to a percentage (0-100)."
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"level": {"type": "integer", "description": "Volume percentage, 0-100."}},
        "required": ["level"],
    }

    _VOLUME_PATTERN = re.compile(r"\bvolume\s+to\s+(\d{1,3})\b", re.I)

    def matches(self, text: str) -> bool:
        return bool(self._VOLUME_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = self._VOLUME_PATTERN.search(text)
        return {"level": int(m.group(1))} if m else {}

    def run(self, level: int = 50, **kwargs: Any) -> ToolResult:
        level = max(0, min(100, int(level)))
        system = platform.system()
        try:
            if system == "Windows":
                # pycaw is the correct tool here; kept as an optional import so this
                # module still imports cleanly on non-Windows dev/test machines.
                try:
                    from ctypes import cast, POINTER
                    from comtypes import CLSCTX_ALL
                    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

                    devices = AudioUtilities.GetSpeakers()
                    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    volume = cast(interface, POINTER(IAudioEndpointVolume))
                    volume.SetMasterVolumeLevelScalar(level / 100.0, None)
                except ImportError:
                    return ToolResult.fail("pycaw/comtypes not installed; run `pip install pycaw comtypes`.")
            elif system == "Darwin":
                subprocess.run(["osascript", "-e", f"set volume output volume {level}"], check=True)
            elif system == "Linux":
                if shutil.which("pactl"):
                    subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"], check=True)
                else:
                    return ToolResult.fail("No supported volume backend found (install pactl/PulseAudio).")
            return ToolResult.ok(f"Volume set to {level}%.")
        except Exception as e:
            logger.exception("Failed to set volume")
            return ToolResult.fail(f"Couldn't set volume: {e}")


class ScreenshotTool(Tool):
    name = "take_screenshot"
    description = "Take a screenshot and save it to disk."
    permission = PermissionLevel.MODERATE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, config=None) -> None:
        self.config = config
        self.out_dir = config.resolve_path("data/screenshots") if config else Path("data/screenshots")

    def run(self, **kwargs: Any) -> ToolResult:
        try:
            import pyautogui
        except ImportError:
            return ToolResult.fail("pyautogui not installed; run `pip install pyautogui`.")

        self.out_dir.mkdir(parents=True, exist_ok=True)
        import datetime as dt
        out_path = self.out_dir / f"screenshot_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
        try:
            img = pyautogui.screenshot()
            img.save(out_path)
            return ToolResult.ok(f"Screenshot saved to {out_path}.", path=str(out_path))
        except Exception as e:
            logger.exception("Screenshot failed")
            return ToolResult.fail(f"Couldn't take a screenshot: {e}")


class SystemStatusTool(Tool):
    name = "system_status"
    description = "Report CPU, memory, and battery status."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def matches(self, text: str) -> bool:
        return bool(re.search(r"\b(system status|how.?s my (cpu|memory|battery)|battery level)\b", text, re.I))

    def run(self, **kwargs: Any) -> ToolResult:
        try:
            import psutil
        except ImportError:
            return ToolResult.fail("psutil not installed; run `pip install psutil`.")

        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory().percent
        battery = psutil.sensors_battery()
        battery_str = f"{battery.percent}%{' (charging)' if battery.power_plugged else ''}" if battery else "n/a"
        return ToolResult.ok(
            f"CPU {cpu}%, memory {mem}%, battery {battery_str}.",
            cpu=cpu, memory=mem, battery=battery_str,
        )
