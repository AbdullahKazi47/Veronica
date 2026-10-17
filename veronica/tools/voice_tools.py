"""Choose her voice by voice: "what voices do you have?", "change your voice to Emma",
"switch to the Irish voice", "try the next voice", "go back to Alice".

The choice is kept in data/voice.json and applied at start-up, over config.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult


@dataclass(frozen=True)
class VoiceOption:
    name: str          # what the user says
    backend: str       # tts backend
    voice: str         # voice id for that backend
    about: str         # spoken description
    words: tuple = ()  # other ways to ask for it


# On this PC (Kokoro) first: instant and free. Then the free Microsoft cloud voices, then ElevenLabs.
OPTIONS: tuple[VoiceOption, ...] = (
    VoiceOption("Veronica", "kokoro", "veronica", "my own voice: a confident British blend, on this PC",
                ("your own", "own voice", "blend")),
    VoiceOption("Emma", "kokoro", "bf_emma", "a calm British woman, on this PC", ("british",)),
    VoiceOption("Isabella", "kokoro", "bf_isabella", "a brighter British woman, on this PC"),
    VoiceOption("Lily", "kokoro", "bf_lily", "a soft, young British woman, on this PC"),
    VoiceOption("Heart", "kokoro", "af_heart", "a warm American woman, Kokoro's best voice, on this PC", ("american",)),
    VoiceOption("Bella", "kokoro", "af_bella", "a lively American woman, on this PC"),
    VoiceOption("Nicole", "kokoro", "af_nicole", "a quiet, breathy American woman, on this PC", ("soft", "whisper")),
    VoiceOption("Emily", "edge_tts", "en-IE-EmilyNeural", "an Irish woman, like FRIDAY; Microsoft's free online voice",
                ("irish", "friday")),
    VoiceOption("Sonia", "edge_tts", "en-GB-SoniaNeural", "a British woman; Microsoft's free online voice"),
    VoiceOption("Ava", "edge_tts", "en-US-AvaMultilingualNeural", "an expressive American woman; Microsoft's free online voice"),
    VoiceOption("Alice", "elevenlabs", "Xb7hH8MSUJpSbSDYk0k2",
                "ElevenLabs' Alice, a confident British woman; uses your ElevenLabs credits", ("eleven labs", "elevenlabs")),
)

_LIST = re.compile(r"\b(?:what|which|list|tell\s+me)\b.{0,20}\bvoices?\b|\bvoice\s+options\b|\bother\s+voices\b", re.I)
_CHANGE = re.compile(
    r"\b(?:change|switch|set|use|try|go\s+back\s+to|give\s+me)\b.{0,30}\b(?:voice|sound\s+like|accent)\b|"
    r"\b(?:next|another|different)\s+voice\b|\bvoice\s+(?:to|number)\b", re.I)


def _state_path(config):
    from pathlib import Path
    return config.resolve_path("data/voice.json") if config is not None else Path("data/voice.json")


def apply_saved_voice(config) -> Optional[VoiceOption]:
    """Called at start-up: the voice the user last chose by voice, if any."""
    from veronica.core import jsonsafe
    data, _err = jsonsafe.read_json(_state_path(config), {})
    name = (data or {}).get("name")
    opt = next((o for o in OPTIONS if o.name == name), None)
    if opt is not None:
        _set(config, opt)
    return opt


def _set(config, opt: VoiceOption) -> None:
    tts = config.tts
    tts.backend = opt.backend
    if opt.backend == "elevenlabs":
        tts.elevenlabs_voice_id = opt.voice
    else:
        tts.voice = opt.voice


def current(config) -> Optional[VoiceOption]:
    tts = config.tts
    for o in OPTIONS:
        if o.backend == tts.backend and (o.voice == (tts.elevenlabs_voice_id if o.backend == "elevenlabs" else tts.voice)):
            return o
    return None


def find_option(text: str) -> Optional[VoiceOption]:
    t = " " + re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()) + " "
    for o in OPTIONS:
        if f" {o.name.lower()} " in t or any(f" {w} " in t for w in o.words):
            return o
    m = re.search(r"\bnumber\s+(\d+)\b|\bvoice\s+(\d+)\b", t)
    if m:
        i = int(m.group(1) or m.group(2)) - 1
        if 0 <= i < len(OPTIONS):
            return OPTIONS[i]
    return None


class ChangeVoiceTool(Tool):
    name = "change_voice"
    description = ("List VERONICA's voices or switch to one (by name, accent, number, or 'next voice'). "
                   "Voices: " + ", ".join(f"{o.name} ({o.about})" for o in OPTIONS) + ".")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"voice": {"type": "string", "description": "Voice name, accent, number, 'next', or empty to list."}},
        "required": [],
    }

    def __init__(self, config=None, available=None) -> None:
        self.config = config
        self._available = available          # (VoiceOption) -> bool, for tests

    def matches(self, text: str) -> bool:
        if re.search(r"\b(?:test|check|volume|louder|quieter|loud|mute|recogni[sz]|typing|training)\w*", text, re.I):
            return False
        return bool(_LIST.search(text) or _CHANGE.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        if re.search(r"\b(?:next|another|different)\s+voice\b", text, re.I):
            return {"voice": "next"}
        opt = find_option(text)
        return {"voice": opt.name} if opt else {"voice": ""}

    def _usable(self, opt: VoiceOption) -> bool:
        if self._available is not None:
            return self._available(opt)
        if opt.backend == "kokoro":
            from veronica.speech import kokoro_voice
            return kokoro_voice.installed(kokoro_voice.model_dir(self.config))
        if opt.backend == "elevenlabs":
            return bool(self.config.secret("ELEVENLABS_API_KEY"))
        return True

    def run(self, voice: str = "", **kwargs: Any) -> ToolResult:
        usable = [o for o in OPTIONS if self._usable(o)]
        now = current(self.config)
        if not voice:
            listing = "; ".join(f"{i}, {o.name}: {o.about}" for i, o in enumerate(usable, 1))
            here = f" I'm using {now.name} now." if now else ""
            return ToolResult.ok(f"I have {len(usable)} voices. {listing}.{here} Say 'change your voice to' and a name.")
        if voice == "next":
            i = usable.index(now) if now in usable else -1
            opt = usable[(i + 1) % len(usable)]
        else:
            opt = find_option(voice)
            if opt is None:
                return ToolResult.fail(f"I don't have a voice called {voice}. Ask me what voices I have.")
            if opt not in usable:
                return ToolResult.fail(f"{opt.name} isn't available on this PC right now.")
        _set(self.config, opt)
        from veronica.core import jsonsafe
        path = _state_path(self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        jsonsafe.write_json(path, {"name": opt.name})
        note = " Note: Alice uses your ElevenLabs credits, about ten minutes of speech a month on the free plan." \
            if opt.backend == "elevenlabs" else ""
        return ToolResult.ok(f"This is {opt.name}, {opt.about}. How do I sound?{note}", voice=opt.name)
