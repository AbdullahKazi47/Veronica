"""GPS by voice: "use my phone as GPS", "stop using my phone's GPS", "where am I?",
"is the GPS working?". See integrations/gps.py for where fixes come from."""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from veronica.integrations import gps as gps_mod
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_PHONE_ON = re.compile(
    r"\b(?:use|connect|set\s*up|link|pair|turn\s+on|start)\b.{0,25}\b(?:phone|mobile)\b.{0,25}\b(?:gps|location)\b|"
    r"\b(?:gps|location)\b.{0,15}\bfrom\s+my\s+(?:phone|mobile)\b", re.I)
_PHONE_OFF = re.compile(
    r"\b(?:stop|don'?t|disconnect|turn\s+off|unlink)\b.{0,25}\b(?:phone|mobile)\b.{0,25}\b(?:gps|location)\b", re.I)
_STATUS = re.compile(
    r"^\s*(?:hey\s+)?(?:where\s+am\s+i(?:\s+(?:right\s+)?now)?|what(?:'s|\s+is)\s+my\s+(?:current\s+)?location|"
    r"(?:is\s+(?:the|my)\s+)?gps\s+(?:status|working|on)|do\s+you\s+have\s+(?:a\s+)?gps(?:\s+fix)?|"
    r"(?:check|test)\s+(?:the\s+)?gps)\s*[?.!]*\s*$", re.I)


def _fix_words(fix: gps_mod.Fix) -> str:
    acc = round(fix.accuracy_m)
    sats = f", {fix.satellites} satellites" if fix.satellites else ""
    return f"to within about {acc} metres{sats}, from {fix.source}"


def setup_steps(ip: Optional[str], port: int) -> str:
    where = f"{ip} port {port}" if ip else f"this PC's address, port {port}"
    return (f"On your phone, install a GPS-to-network app: 'Share GPS' or 'GPSd Forwarder' on Android, "
            f"'GPS2IP' on iPhone. Set it to send NMEA over UDP to {where}, with the phone on the same "
            f"Wi-Fi as this laptop. If Windows asks whether Python may use the network, allow private networks.")


class UsePhoneGpsTool(Tool):
    name = "use_phone_gps"
    description = ("Start or stop using the user's phone as VERONICA's GPS (the phone app sends its position "
                   "over the local Wi-Fi). Use when the user asks to use or stop using their phone's GPS.")
    # Listening for the phone on the local network; asked for by the user, nothing leaves the PC.
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"on": {"type": "boolean", "description": "true to start, false to stop."}},
        "required": [],
    }

    def __init__(self, config=None, hub_fn: Optional[Callable[[], Any]] = None,
                 ip_fn: Callable[[], Optional[str]] = gps_mod.lan_ip, save: Optional[Callable] = None) -> None:
        self.config = config
        self._hub_fn = hub_fn or (lambda: gps_mod.get_hub(config))
        self._ip = ip_fn
        self._save = save or (lambda on: gps_mod.set_phone_wanted(config, on))

    def matches(self, text: str) -> bool:
        return bool(_PHONE_ON.search(text) or _PHONE_OFF.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        return {"on": not _PHONE_OFF.search(text)}

    def run(self, on: bool = True, **kwargs: Any) -> ToolResult:
        hub = self._hub_fn()
        if not on:
            self._save(False)
            hub.stop_udp()
            return ToolResult.ok("Okay, I've stopped listening for your phone's GPS.")
        if not hub.start_udp():
            return ToolResult.fail(f"I couldn't listen for the phone on port {hub.udp_port}: {hub.udp_error}")
        self._save(True)
        fix = hub.latest()
        if fix is not None:
            return ToolResult.ok(f"Your phone's GPS is already coming through, {_fix_words(fix)}.")
        return ToolResult.ok("I'm listening for your phone's GPS now. " + setup_steps(self._ip(), hub.udp_port)
                             + " Once it's sending, ask me 'where am I?'.")


class WhereAmITool(Tool):
    name = "where_am_i"
    description = ("Say where the user is right now and how sure VERONICA is (live GPS if there is one, "
                   "else what the user told her, else a rough guess). Use for 'where am I' or GPS status.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, config=None, profile=None, hub_fn: Optional[Callable[[], Any]] = None) -> None:
        from veronica.tools.productivity_tools import Profile
        self.config = config
        self.profile = profile or Profile(config)
        self._hub_fn = hub_fn or (lambda: gps_mod.get_hub(config, start=False))

    def matches(self, text: str) -> bool:
        return bool(_STATUS.match(text))

    def run(self, **kwargs: Any) -> ToolResult:
        live = self.profile.gps_place()
        hub = self._hub_fn()
        if live:
            fix = hub.latest() if hub is not None else None
            how = f", {_fix_words(fix)}" if fix is not None else ""
            return ToolResult.ok(f"You're in {live[0]}{how}.", data={"lat": live[1], "lon": live[2]})
        city, source = self.profile.city_and_source()
        if hub is not None and hub.udp_on:
            gps_note = " I'm listening for your phone's GPS but nothing has arrived yet; check the app is sending."
        else:
            gps_note = (" I have no GPS: this laptop has none built in. Plug in a USB GPS, or say "
                        "'use my phone as GPS'.")
        if source == "user":
            return ToolResult.ok(f"You told me you're in {city}.{gps_note}")
        if city:
            return ToolResult.ok(f"Roughly {city}, going by Windows or your internet connection, which is only "
                                 f"good to a few kilometres.{gps_note}")
        return ToolResult.ok("I don't know where you are yet." + gps_note)
