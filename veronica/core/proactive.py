"""
Proactive VERONICA: speaks up on her own when something deserves attention, the way
Jarvis does — without being asked.

Checks (each with its own cooldown, all optional in `proactive:` config):
  * battery low / critical while unplugged
  * processor or memory pinned high for several minutes (names the culprit)
  * C: drive nearly full
  * rain likely within the hour at the user's location
  * long unbroken session at the PC (suggests a break)
  * a greeting (time, weather, top headlines) whenever the laptop is opened: at login,
    on waking from sleep, or when the user is back after being away — no wake word

Manners: never talks over the user (waits while they speak), holds non-urgent alerts
while a fullscreen app (game, movie) is in front, and only urgent alerts during quiet
hours. "Pause alerts" / "resume alerts" by voice.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("core.proactive")


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (Windows); 0 if unknown."""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
            return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0  # type: ignore[attr-defined]
    except Exception:
        pass
    return 0.0


def fullscreen_app_in_front() -> bool:
    """A game or video filling the screen: hold non-urgent alerts."""
    try:
        import win32api
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or win32gui.GetClassName(hwnd) in ("Progman", "WorkerW"):
            return False
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        mon = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd))["Monitor"]
        return (left, top, right, bottom) == tuple(mon)
    except Exception:
        return False


class ProactiveMonitor:
    def __init__(self, config, speak: Callable[[str, bool], None], *,
                 collect=None, rain_probability=None, idle=idle_seconds, fullscreen=fullscreen_app_in_front,
                 briefing: Optional[Callable[[], str]] = None, clock=time.time, now=dt.datetime.now,
                 state_path=None) -> None:
        from veronica.tools.diagnostics_tools import collect as default_collect
        self.cfg = config
        self.speak = speak                       # speak(text, urgent)
        self._collect = collect or default_collect
        self._rain = rain_probability
        self._idle, self._fullscreen, self._briefing = idle, fullscreen, briefing
        self._clock, self._now = clock, now
        self._last: dict[str, float] = {}        # alert kind -> last spoken (clock)
        self._streak: dict[str, int] = {}        # consecutive high readings
        self._armed = {"battery_low": True, "battery_critical": True}
        self._session_start: Optional[float] = None
        self.paused_until = 0.0
        self._held: list[tuple[str, str]] = []   # non-urgent alerts held during fullscreen
        self._stop = threading.Event()
        # The last greeting survives restarts: restarting VERONICA five times in an hour
        # used to greet five times (and spend voice credits on each one).
        self._state_path = state_path
        try:
            if state_path is not None:
                from veronica.core import jsonsafe
                saved, _ = jsonsafe.read_json(state_path, default={})
                if saved and saved.get("last_greeting"):
                    self._last["greeting"] = float(saved["last_greeting"])
        except Exception:  # noqa: BLE001
            pass

    # -- control ------------------------------------------------------------------------
    def pause(self, minutes: float = 60) -> None:
        self.paused_until = self._clock() + minutes * 60

    def resume(self) -> None:
        self.paused_until = 0.0

    def start(self) -> None:
        threading.Thread(target=self._loop, name="proactive", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    _FAST_S = 5.0                 # "laptop opened" detection cadence
    _SLEEP_GAP_S = 90.0           # a wall-clock jump this big between checks = it was asleep

    def _loop(self) -> None:
        self._stop.wait(3)        # let the voice come up
        self.on_open("startup")   # logged in / VERONICA started: greet right away
        while not self._stop.wait(self._FAST_S):
            try:
                self.poll()
            except Exception:
                logger.exception("Proactive check failed; continuing.")

    def poll(self) -> None:
        """One fast check: was the laptop just opened? and, once a minute, the alerts."""
        wall = self._clock()
        last_wall = getattr(self, "_last_wall", wall)
        if wall - last_wall > self._SLEEP_GAP_S:
            self.on_open("resumed from sleep")                     # lid opened
        away = self._idle() >= float(getattr(self.cfg, "away_minutes", 15)) * 60
        if getattr(self, "_was_away", False) and not away:
            self.on_open("back after being away")                   # unlocked / sat back down
        self._was_away, self._last_wall = away, wall
        if wall - getattr(self, "_last_tick", -1e12) >= float(getattr(self.cfg, "check_every_s", 60)):
            self._last_tick = wall
            self.tick()

    def on_open(self, why: str) -> None:
        """The user just opened / came back to the laptop: greet them (no wake word),
        at most once per `greet_cooldown_minutes`."""
        if (self._briefing is None or not getattr(self.cfg, "greet_on_open", True)
                or self._clock() < self.paused_until):
            return
        if not self._cool("greeting", float(getattr(self.cfg, "greet_cooldown_minutes", 60))):
            return
        logger.info("Greeting the user (%s)", why)
        self._last["greeting"] = self._clock()
        if self._state_path is not None:
            try:
                from veronica.core import jsonsafe
                jsonsafe.write_json(self._state_path, {"last_greeting": self._last["greeting"]})
            except Exception:  # noqa: BLE001
                pass
        self._session_start = self._clock()
        try:
            text = self._briefing()
        except Exception:
            logger.exception("Greeting failed")
            return
        if text:
            self.speak(text, False)  # never held back: they just sat down and expect it

    # -- one round of checks -----------------------------------------------------------
    def _cool(self, kind: str, minutes: float) -> bool:
        return self._clock() - self._last.get(kind, -1e12) >= minutes * 60

    def _quiet_hours(self) -> bool:
        start, end = int(getattr(self.cfg, "quiet_start_hour", 23)), int(getattr(self.cfg, "quiet_end_hour", 7))
        h = self._now().hour
        return (h >= start or h < end) if start > end else (start <= h < end)

    def _say(self, kind: str, text: str, urgent: bool = False) -> None:
        self._last[kind] = self._clock()
        if not urgent and self._fullscreen():
            self._held.append((kind, text))      # tell them when the game/movie ends
            return
        self.speak(text, urgent)

    def tick(self) -> None:
        if self._clock() < self.paused_until:
            # Paused by the user: everything waits except a critically low battery.
            r = self._collect(sample_s=0.2, check_online=False)
            if r.battery_percent is not None and not r.plugged_in and r.battery_percent <= 10:
                self._battery(r)
            return
        quiet = self._quiet_hours()
        if self._held and not self._fullscreen() and not quiet:
            held, self._held = self._held, []
            self.speak("While you were busy: " + " ".join(t for _, t in held[-3:]), False)
        reading = self._collect(sample_s=0.5, check_online=False)
        self._battery(reading)
        if quiet:
            return  # only urgent (battery) alerts at night
        self._load(reading)
        self._disk(reading)
        self._session()
        self._rain_soon()

    def _battery(self, r) -> None:
        if r.battery_percent is None or not getattr(self.cfg, "battery", True):
            return
        if r.plugged_in or r.battery_percent > 30:
            self._armed = {"battery_low": True, "battery_critical": True}  # re-arm after charging
            return
        if r.battery_percent <= 10 and self._armed["battery_critical"]:
            self._armed["battery_critical"] = self._armed["battery_low"] = False
            self._say("battery_critical", f"Battery critical: {r.battery_percent} percent. Plug in now or "
                                          "save your work.", urgent=True)
        elif r.battery_percent <= 20 and self._armed["battery_low"]:
            self._armed["battery_low"] = False
            left = f", about {r.battery_minutes_left} minutes left" if r.battery_minutes_left else ""
            self._say("battery_low", f"Heads up, battery's at {r.battery_percent} percent{left}.", urgent=True)

    def _load(self, r) -> None:
        if not getattr(self.cfg, "performance", True):
            return
        for kind, value, limit, who in (("cpu", r.cpu_percent, 90, r.top_cpu),
                                        ("memory", r.memory_percent, 92, r.top_memory)):
            self._streak[kind] = self._streak.get(kind, 0) + 1 if value >= limit else 0
            if self._streak[kind] >= 3 and self._cool(kind, 30):
                culprit = f" {who[0][0]} is the main one." if who else ""
                what = "processor" if kind == "cpu" else "memory"
                self._say(kind, f"Your {what} has been above {limit} percent for a few minutes.{culprit}")

    def _disk(self, r) -> None:
        if getattr(self.cfg, "disk", True) and r.disk_free_gb and r.disk_free_gb < 5 and self._cool("disk", 24 * 60):
            self._say("disk", f"Your C drive is almost full, only {r.disk_free_gb} gigabytes left.")

    def _session(self) -> None:
        idle = self._idle()
        now = self._clock()
        if idle >= 10 * 60:                      # stepped away: a new session starts on return
            self._session_start = None
            return
        if self._session_start is None:
            self._session_start = now
        minutes = float(getattr(self.cfg, "break_after_minutes", 120) or 0)
        if minutes and now - self._session_start >= minutes * 60 and self._cool("break", minutes):
            hours = round((now - self._session_start) / 3600, 1)
            self._say("break", f"You've been at it for {hours:g} hours straight. Good moment to stretch "
                               "and grab some water.")

    def _rain_soon(self) -> None:
        if self._rain is None or not getattr(self.cfg, "rain", True) or not self._cool("rain", 180):
            return
        if not self._cool("rain_check", 15):
            return
        self._last["rain_check"] = self._clock()
        try:
            found = self._rain()                 # (probability %, minutes from now) or None
        except Exception:
            return
        if found and found[0] >= 70 and found[1] > 0:
            self._say("rain", f"Heads up: rain is likely in about {found[1]} minutes. Grab an umbrella if "
                              "you're heading out.")


def rain_probability_for(profile) -> Optional[Callable[[], Optional[tuple[int, int]]]]:
    """Build the 'is rain coming within the hour?' check for the user's location."""
    def check() -> Optional[tuple[int, int]]:
        import requests
        coords = profile.detected_coords()
        if coords is None:
            city = profile.home_city()
            if not city:
                return None
            geo = requests.get("https://geocoding-api.open-meteo.com/v1/search", timeout=8,
                               params={"name": city.split(",")[0], "count": 1}).json().get("results") or []
            if not geo:
                return None
            coords = (geo[0]["latitude"], geo[0]["longitude"])
        data = requests.get("https://api.open-meteo.com/v1/forecast", timeout=8, params={
            "latitude": coords[0], "longitude": coords[1], "timezone": "auto", "forecast_days": 1,
            "minutely_15": "precipitation_probability,precipitation"}).json().get("minutely_15") or {}
        times, probs, rain = data.get("time", []), data.get("precipitation_probability", []), data.get("precipitation", [])
        now = dt.datetime.now()
        upcoming = [(dt.datetime.fromisoformat(t), p, r) for t, p, r in zip(times, probs, rain)
                    if p is not None and dt.datetime.fromisoformat(t) >= now]
        if not upcoming or (upcoming[0][2] or 0) > 0:
            return None                          # no data, or it's already raining
        for when, p, _ in upcoming:
            minutes = int((when - now).total_seconds() // 60)
            if minutes > 60:
                break
            if p >= 70:
                return int(p), max(1, minutes)
        return None
    return check
