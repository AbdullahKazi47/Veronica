"""
Words VERONICA expects to hear, handed to Whisper as a spelling hint.

Whisper gets local names right far more often when it's told they may come up: in a test
on this PC the hint took word errors from 15% to 8% ("Naginahale" -> "Naganahalli",
"Kuramangala" -> "Koramangala", "Leily" -> "Layali"). Nothing here is fixed: it's built
from the user's own data — her name, Jev, the user's area, their routine names, abilities
and the words they taught her ("when I say X, I mean Y") — and refreshed every minute.
"""
from __future__ import annotations

import re
import time
from typing import Optional

_cache: dict[str, tuple[float, str]] = {}
_TTL_S = 60
_MAX_WORDS = 60


def build_vocabulary(config) -> str:
    key = str(id(config))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL_S:
        return hit[1]
    words: list[str] = []

    def add(text: Optional[str]) -> None:
        for part in re.split(r"[,;]", text or ""):
            part = part.strip()
            if part and part.lower() not in {w.lower() for w in words}:
                words.append(part)

    add(getattr(getattr(config, "assistant", None), "name", "VERONICA"))
    add("Jev")
    try:
        from veronica.tools.productivity_tools import Profile
        add(Profile(config).home_city())
    except Exception:  # noqa: BLE001
        pass
    try:
        from veronica.core.routines import RoutineStore
        for r in RoutineStore(config.resolve_path("data/routines.json")).all():
            add(r.name)
    except Exception:  # noqa: BLE001
        pass
    try:
        from veronica.speech.corrections import Corrections
        rel = getattr(config.stt, "corrections_path", "data/stt_corrections.json")
        for meant in Corrections(config.resolve_path(rel)).all().values():
            add(meant)
    except Exception:  # noqa: BLE001
        pass
    try:
        from veronica.core import jsonsafe
        records, _ = jsonsafe.read_json(config.resolve_path("data/abilities/approved.json"), default={})
        for name in (records or {}):
            add(name.replace("_", " "))
    except Exception:  # noqa: BLE001
        pass
    text = ", ".join(words[:_MAX_WORDS])
    _cache[key] = (time.time(), text)
    return text
