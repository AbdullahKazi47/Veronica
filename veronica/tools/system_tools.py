"""
System-interaction tools. Cross-platform where reasonably possible; Windows-specific
volume control (the one thing that genuinely needs a platform API) degrades gracefully
on other OSes instead of crashing, unlike the reference repos.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.system")

_OPEN = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?(open|launch|start|run|go\s+to|navigate\s+to|visit|"
    r"bring\s+up|pull\s+up)\s+(?:up\s+)?(?:the\s+)?(.+?)(?:\s+(?:in|on|with|using)\s+(?:the\s+)?"
    r"([a-z][\w\s]{0,30}?))?\s*(?:please)?\s*[.!?]*\s*$", re.I)
_SITE_VERBS = {"open", "go to", "navigate to", "visit", "bring up", "pull up"}
_TRAILING_KIND = re.compile(r"\s+(?:website|site|web\s*site|app|application|program|folder)$", re.I)


def _resolves(host: str) -> bool:
    """A domain-looking word is a website only if it resolves ('notes.txt' does not)."""
    import socket
    try:
        socket.getaddrinfo(host.split("/")[0].replace("https://", "").replace("http://", ""), 443)
        return True
    except OSError:
        return False


def parse_open_request(text: str, index) -> Optional[dict]:
    """'open youtube in brave' -> {'kind': 'site', 'target': 'youtube', 'browser': 'brave'}.
    kind is 'folder', 'app' or 'site'; None when it's not an open request we can route
    without the LLM. Everything is matched against what is installed, never a list."""
    m = _OPEN.match(text or "")
    if not m:
        return None
    verb = re.sub(r"\s+", " ", m.group(1).lower())
    target = _TRAILING_KIND.sub("", m.group(2).strip())
    via = (m.group(3) or "").strip()
    if not target:
        return None
    if via:
        if index.find_browser(via) is None:
            return None  # "open X on <something else>" — let the LLM interpret it
        return {"kind": "site", "target": target, "browser": via}
    if index.looks_like_domain(target):
        return {"kind": "site", "target": target, "browser": ""} if _resolves(target) else None
    if target.lower().startswith("my "):
        return None  # "open my to-do list" is not a website or an app name
    if index.find_folder(target) is not None:
        return {"kind": "folder", "target": target}
    if index.find_app(target) is not None:
        return {"kind": "app", "target": target}
    # A bare name is a website only if that site really exists ("go to netflix").
    # A misheard sentence ("open camera and america") is left to the LLM, which can
    # tell what was meant, instead of being turned into a made-up web address.
    if verb in _SITE_VERBS and len(target.split()) <= 3 and index.site_for(target) is not None:
        return {"kind": "site", "target": target, "browser": ""}
    return None


class _OpenRouting:
    """Shared parse for the two 'open' tools (each fast-path check would otherwise
    repeat the app lookup)."""

    def __init__(self, config=None, index=None) -> None:
        from veronica.tools.app_index import shared_index
        self.index = index or shared_index(config)
        self._last: tuple[str, Optional[dict]] = ("", None)

    def parse(self, text: str) -> Optional[dict]:
        if self._last[0] != text:
            self._last = (text, parse_open_request(text, self.index))
        return self._last[1]


class OpenApplicationTool(Tool):
    name = "open_application"
    description = ("Open any installed application (anything in the Start menu, e.g. 'WhatsApp', "
                   "'Spotify', 'Brave', 'Settings') or a folder in the user's home (e.g. 'Downloads').")
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"app_name": {"type": "string", "description": "Application or folder name."}},
        "required": ["app_name"],
    }

    def __init__(self, config=None, index=None, routing: Optional[_OpenRouting] = None) -> None:
        self.routing = routing or _OpenRouting(config, index)
        self.index = self.routing.index

    def matches(self, text: str) -> bool:
        parsed = self.routing.parse(text)
        return bool(parsed) and parsed["kind"] in ("app", "folder")

    def extract_arguments(self, text: str) -> dict[str, Any]:
        parsed = self.routing.parse(text)
        return {"app_name": parsed["target"]} if parsed else {}

    def run(self, app_name: str = "", **kwargs: Any) -> ToolResult:
        name = (app_name or "").strip()
        if not name:
            return ToolResult.fail("Which app should I open?")
        folder = self.index.find_folder(name)
        if folder is not None:
            try:
                os.startfile(str(folder)) if platform.system() == "Windows" else subprocess.Popen(["xdg-open", str(folder)])
                return ToolResult.ok(f"Opening your {folder.name} folder.", path=str(folder))
            except Exception as e:
                return ToolResult.fail(f"Couldn't open {folder.name}: {e}")
        app = self.index.find_app(name)
        if app is None:
            return ToolResult.fail(f"I couldn't find an app called {name} on this PC.")
        try:
            self.index.launch_app(app)
            return ToolResult.ok(f"Opening {app['name']}.", app=app["name"])
        except Exception as e:
            logger.exception("Failed to open %s", app["name"])
            return ToolResult.fail(f"Couldn't open {app['name']}: {e}")


def _endpoint_volume():
    """The default output device's volume control (pycaw), plus its name."""
    from pycaw.pycaw import AudioUtilities
    dev = AudioUtilities.GetSpeakers()
    vol = getattr(dev, "EndpointVolume", None)
    if vol is None:  # older pycaw: activate the interface by hand
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume
        vol = cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
    return vol, getattr(dev, "FriendlyName", "") or "the default output"


def output_status() -> Optional[dict]:
    """{'device', 'level', 'muted'} for the default output, or None if unavailable."""
    if platform.system() != "Windows":
        return None
    try:
        vol, name = _endpoint_volume()
        return {"device": name, "level": round(vol.GetMasterVolumeLevelScalar() * 100), "muted": bool(vol.GetMute())}
    except Exception as e:  # noqa: BLE001
        logger.debug("Output status unavailable: %s", e)
        return None


_VOL_SET = re.compile(r"\b(?:set\s+(?:the\s+)?)?volume\s+(?:to|at)\s+(\d{1,3})\s*(?:%|percent)?", re.I)
_VOL_STEP = re.compile(r"\b(?:turn\s+(?:it|the\s+volume|volume)\s+(up|down)|volume\s+(up|down)|"
                       r"(louder|quieter|softer)|(?:increase|raise)\s+(?:the\s+)?volume|(?:decrease|lower|reduce)\s+(?:the\s+)?volume)\b", re.I)
_VOL_MUTE = re.compile(r"^\s*(?:please\s+)?(un)?mute(?:\s+(?:the\s+)?(?:sound|volume|audio|speakers?|it))?\s*[.!]?\s*$", re.I)
_STEP = 10


class SetVolumeTool(Tool):
    name = "set_volume"
    description = ("Control the system output volume: set a level (0-100), step it up/down, "
                   "mute or unmute. action: 'set' (with level), 'up', 'down', 'mute', 'unmute'.")
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "up", "down", "mute", "unmute"]},
            "level": {"type": "integer", "description": "Volume percentage 0-100 (for 'set')."},
        },
        "required": [],
    }

    def matches(self, text: str) -> bool:
        return bool(_VOL_SET.search(text) or _VOL_STEP.search(text) or _VOL_MUTE.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _VOL_SET.search(text)
        if m:
            return {"action": "set", "level": int(m.group(1))}
        m = _VOL_MUTE.match(text)
        if m:
            return {"action": "unmute" if m.group(1) else "mute"}
        down = re.search(r"\b(down|quieter|softer|decrease|lower|reduce)\b", text, re.I)
        return {"action": "down" if down else "up"}

    def run(self, action: str = "", level: Optional[int] = None, **kwargs: Any) -> ToolResult:
        action = (action or ("set" if level is not None else "")).lower()
        if action not in ("set", "up", "down", "mute", "unmute"):
            return ToolResult.fail("Say a level like 'volume to 50', or 'volume up', 'mute', 'unmute'.")
        system = platform.system()
        try:
            if system == "Windows":
                try:
                    vol, device = _endpoint_volume()
                except ImportError:
                    return ToolResult.fail("Volume control needs `pip install pycaw`.")
                if action in ("mute", "unmute"):
                    vol.SetMute(action == "mute", None)
                    return ToolResult.ok("Muted." if action == "mute" else "Sound is on.", device=device)
                current = round(vol.GetMasterVolumeLevelScalar() * 100)
                target = {"set": level if level is not None else current,
                          "up": current + _STEP, "down": current - _STEP}[action]
                target = max(0, min(100, int(target)))
                vol.SetMute(False, None)  # changing the volume implies wanting to hear it
                vol.SetMasterVolumeLevelScalar(target / 100.0, None)
                return ToolResult.ok(f"Volume {target} percent.", level=target, device=device)
            if action in ("mute", "unmute", "up", "down"):
                return ToolResult.fail(f"'{action}' is only supported on Windows; set a level instead.")
            target = max(0, min(100, int(level or 50)))
            if system == "Darwin":
                subprocess.run(["osascript", "-e", f"set volume output volume {target}"], check=True)
            elif shutil.which("pactl"):
                subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{target}%"], check=True)
            else:
                return ToolResult.fail("No supported volume backend found (install pactl/PulseAudio).")
            return ToolResult.ok(f"Volume {target} percent.", level=target)
        except Exception as e:
            logger.exception("Failed to change volume")
            return ToolResult.fail(f"Couldn't change the volume: {e}")


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
