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
# Whisper tokenizes initial_prompt and keeps at most 224 tokens, so the hint has a real
# ceiling. The user's own words go in first and the app names fill what is left: a name
# she will never otherwise get right is worth more than one more app.
_MAX_WORDS = 110
_MAX_APPS = 55

# Start Menu entries nobody says out loud: help pages, uninstallers, manuals, admin tools.
_NOT_SPOKEN = re.compile(
    r"\b(help|faqs?|manual|documentation|docs|release notes|licen[cs]e|readme|uninstall|"
    r"website|command prompt|troubleshoot|diagnostics?|initiator|odbc|defragment|"
    r"configuration|policy|viewer|services|management|cleanup|recovery|installer|"
    r"additional tools|private browsing)\b", re.I)
# "Google Chrome" is said as "Chrome", "Microsoft Teams" as "Teams".
_VENDORS = re.compile(r"^(google|microsoft|intel\w*|adobe|apple|nvidia|oracle|mozilla)\s+", re.I)


def _spoken_app_names(index) -> list[str]:
    """App and browser names in the form someone would actually say them.

    The Start Menu is full of entries that are never spoken ("Git FAQs (Frequently Asked
    Questions)"), and of vendor-prefixed ones that are always shortened in speech. Both
    matter here: "close Chrome" was being heard as "close Crow" purely because Whisper
    had never been told the word.
    """
    names: list[str] = []
    try:
        entries = list(index.browsers()) + list(index.apps())   # browsers first: said most
    except Exception:  # noqa: BLE001 - no app index is not an error
        return names
    for entry in entries:
        raw = (entry.get("name") or "").strip()
        if not raw or "(" in raw or _NOT_SPOKEN.search(raw):
            continue
        short = _VENDORS.sub("", raw).strip()
        if len(short.split()) > 2:          # administrative mouthfuls, not spoken names
            continue
        for candidate in (short, raw):      # "Chrome" and "Google Chrome" both get heard
            if candidate and candidate.lower() not in {n.lower() for n in names}:
                names.append(candidate)
            if candidate == raw:
                break
    return names[:_MAX_APPS]


def build_echo_terms(config) -> str:
    """The subset of the hint that signals a hallucination when it comes back alone.

    Whisper given silence tends to parrot the rarest words it was primed with: live, noise
    came back as "SILENCIA, Jev, Bengaluru". whisper_junk() uses that to reject invented
    text — but only these terms may arm it. App names must not: "close Microsoft Teams" is
    a real command made almost entirely of hint words, and arming the filter with app names
    made her reject correctly-heard commands as echoes.
    """
    return _build(config, with_apps=False)


def build_vocabulary(config) -> str:
    """Everything worth priming Whisper with, including the names of installed apps."""
    return _build(config, with_apps=True)


def _build(config, with_apps: bool) -> str:
    key = f"{id(config)}:{with_apps}"
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
    try:
        # What "my notes" was taught to mean — the word she has to get right is the target.
        from veronica.core.understanding import Aliases
        for meant in Aliases(config.resolve_path("data/aliases.json")).all().values():
            add(meant)
    except Exception:  # noqa: BLE001
        pass
    # Last, because it is the one group that can be rebuilt from the machine itself: the
    # names of installed apps, which is what most commands are actually made of.
    if with_apps:
        try:
            from veronica.tools.app_index import shared_index
            for name in _spoken_app_names(shared_index(config)):
                add(name)
        except Exception:  # noqa: BLE001
            pass
    text = ", ".join(words[:_MAX_WORDS])
    _cache[key] = (time.time(), text)
    return text
