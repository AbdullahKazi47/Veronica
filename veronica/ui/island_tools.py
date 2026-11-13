"""
The tools behind the island's hover grid (ui/island.py): music, timer, system monitor,
keep-awake, screenshot. Kept free of drawing code so it can be tested on its own.

Inspired by Fenn's hover notch (fenn.click); written for VERONICA, in her island, so there is
one presence at the top of the screen instead of two fighting over it.

Costs nothing when the grid is closed: stats are sampled and music is polled only while it's open
(the timer only does arithmetic).
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

# -- timer ------------------------------------------------------------------------------------
class Timer:
    """A countdown you tap to start/pause; +1 / +5 add minutes. done() is True exactly once."""

    def __init__(self, minutes: float = 5.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.total = minutes * 60.0
        self.left = self.total
        self.started: Optional[float] = None        # clock() when it last started running
        self._fired = False

    @property
    def running(self) -> bool:
        return self.started is not None

    def remaining(self) -> float:
        if self.started is None:
            return self.left
        return max(0.0, self.left - (self.clock() - self.started))

    def progress(self) -> float:
        """0 at the start, 1 when it's done."""
        return 1.0 - (self.remaining() / self.total if self.total else 0.0)

    def toggle(self) -> None:
        if self.started is None:
            if self.remaining() <= 0:
                self.reset()
            self.started, self._fired = self.clock(), False
        else:
            self.left, self.started = self.remaining(), None

    def add(self, minutes: float) -> None:
        extra = minutes * 60.0
        self.left = self.remaining() + extra
        self.total = max(self.total, self.left) if self.running else self.left
        if self.started is not None:
            self.started = self.clock()
        self._fired = False

    def reset(self, minutes: Optional[float] = None) -> None:
        self.total = (minutes * 60.0) if minutes else 300.0
        self.left, self.started, self._fired = self.total, None, False

    def done(self) -> bool:
        if self.started is not None and self.remaining() <= 0 and not self._fired:
            self._fired = True
            self.left, self.started = 0.0, None
            return True
        return False

    def label(self) -> str:
        s = int(round(self.remaining()))
        return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


# -- system monitor ---------------------------------------------------------------------------
def _rate(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}/s" if unit in ("B", "KB") else f"{n:.1f} {unit}/s"
        n /= 1024
    return f"{n:.1f} GB/s"


class Stats:
    """CPU, memory, battery and network, sampled about once a second while the grid is open."""

    def __init__(self, every: float = 1.0) -> None:
        self.every = every
        self.cpu = self.mem = 0.0
        self.battery: Optional[tuple[float, bool]] = None
        self.down = self.up = ""
        self.history: deque[float] = deque([0.0] * 40, maxlen=40)
        self._last = 0.0
        self._net = None

    def sample(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        if now - self._last < self.every:
            return False
        try:
            import psutil
        except ImportError:
            return False
        first = self._last == 0.0
        self._last = now
        self.cpu = psutil.cpu_percent(None) if not first else psutil.cpu_percent(0.05)
        self.mem = psutil.virtual_memory().percent
        try:
            b = psutil.sensors_battery()
            self.battery = (b.percent, bool(b.power_plugged)) if b else None
        except Exception:  # noqa: BLE001
            self.battery = None
        io = psutil.net_io_counters()
        if self._net is not None:
            t0, r0, s0 = self._net
            span = max(0.2, now - t0)
            self.down, self.up = _rate((io.bytes_recv - r0) / span), _rate((io.bytes_sent - s0) / span)
        self._net = (now, io.bytes_recv, io.bytes_sent)
        self.history.append(self.cpu / 100.0)
        return True


# -- music ------------------------------------------------------------------------------------
_PS1 = Path(__file__).with_name("now_playing.ps1")
_VK = {"previous": 0xB1, "play_pause": 0xB3, "next": 0xB0}


def media_key(name: str) -> None:
    """Press a media key (works with Spotify, YouTube in a browser, any player)."""
    vk = _VK.get(name)
    if vk is None:
        return
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)                  # KEYEVENTF_KEYUP


def parse_now_playing(out: str) -> dict:
    try:
        data = json.loads((out or "").strip().splitlines()[-1]) if (out or "").strip() else {}
    except (ValueError, IndexError):
        return {}
    if not isinstance(data, dict) or not data.get("title"):
        return {}
    app = str(data.get("app") or "")
    nice = ("Spotify" if "spotify" in app.lower() else "Chrome" if "chrome" in app.lower() else
            "Edge" if "edge" in app.lower() else "Firefox" if "firefox" in app.lower() else
            app.split("!")[0].split(".")[-1][:20] if app else "")
    return {"title": str(data["title"])[:120], "artist": str(data.get("artist") or "")[:80],
            "app": nice, "playing": bool(data.get("playing"))}


class NowPlaying:
    """What's playing, from Windows' own media controls; asked at most every few seconds, in the background."""

    def __init__(self, every: float = 3.0, run=subprocess.run) -> None:
        self.every, self._run = every, run
        self.info: dict = {}
        self._last = -1e9
        self._busy = False

    def poll(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if self._busy or now - self._last < self.every:
            return
        self._last, self._busy = now, True
        threading.Thread(target=self._fetch, name="now-playing", daemon=True).start()

    def _fetch(self) -> None:
        try:
            r = self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(_PS1)],
                          capture_output=True, text=True, timeout=5,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.info = parse_now_playing(r.stdout)
        except Exception:  # noqa: BLE001
            self.info = {}
        finally:
            self._busy = False

    def pressed(self, name: str) -> None:
        """A control was clicked: show the change at once, then re-read soon."""
        if name == "play_pause" and self.info:
            self.info = {**self.info, "playing": not self.info.get("playing")}
        self._last = time.monotonic() - self.every + 0.8


# -- keep awake / screenshot ---------------------------------------------------------------------
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002


def keep_awake(on: bool) -> bool:
    """While on, Windows won't sleep or turn the screen off (only while the island is running)."""
    try:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:  # noqa: BLE001
        return False


def screenshot_path(now: Optional[dt.datetime] = None, folder: Optional[Path] = None) -> Path:
    now = now or dt.datetime.now()
    folder = folder or (Path.home() / "Pictures" / "Screenshots")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"VERONICA {now:%Y-%m-%d %H.%M.%S}.png"


def take_screenshot(path: Optional[Path] = None) -> Optional[Path]:
    try:
        from PIL import ImageGrab
        path = path or screenshot_path()
        ImageGrab.grab(all_screens=True).save(path)
        return path
    except Exception:  # noqa: BLE001
        return None
