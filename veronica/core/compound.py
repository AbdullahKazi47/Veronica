"""
Compound command splitting: "close Spotify and open Chrome and remind me to
check email" becomes three clauses the orchestrator can run sequentially.

The splitter mirrors the compound-command handling in the user's private ULTRON
project (project_ultron/core/brain_orchestrator.py), adapted to VERONICA's
deterministic-before-LLM philosophy: a conjunction only starts a new clause
when what follows is clearly a new command, so genuine conjunctive *objects*
never get split ("search for cats and dogs", "what is the weather in London and
Paris" stay single turns). Not for redistribution outside this project.

Boundary rules (all evaluated against the original, case-preserved text):

  * ``,`` / ``;`` — split only when the next clause starts with a command verb.
  * ``then`` (and ``and then``) — unconditional boundary; "then" by definition
    starts a new clause.
  * ``and`` / ``&`` — split only when the next clause starts with a command verb
    (after filler words like "please" are skipped).
"""
from __future__ import annotations

import re

# Command verbs in rough frequency order. Multiword verbs must appear before
# their first word repeats in a shorter one (turn on/off before… itself); the
# matcher tries longest-match first.
_COMMAND_VERBS: tuple[str, ...] = (
    "turn on", "turn off", "set reminder", "remind me", "shut down",
    "look up", "go to", "open", "launch", "start", "run", "stop", "close",
    "kill", "terminate", "quit", "exit", "set", "create", "add", "delete",
    "remove", "rename", "copy", "move", "search", "find", "look", "check",
    "show", "list", "get", "send", "email", "play", "pause", "resume",
    "read", "write", "recall", "remember", "forget", "enable", "disable",
    "restart", "reboot", "change", "update", "install", "uninstall",
    "browse", "navigate", "calculate", "translate", "summarize", "zoom",
    "maximize", "minimize", "screenshot", "take", "increase", "decrease",
    "mute", "unmute", "volume", "start", "note", "tell me about",
)

_FILLERS: tuple[str, ...] = ("please", "could you", "can you", "would you")
_FILLER_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(f) for f in _FILLERS) + r")\s*",
    re.IGNORECASE,
)

# Multiword verbs sorted longest first so "turn on" wins over "turn".
_VERBS_SORTED: tuple[str, ...] = tuple(
    sorted(_COMMAND_VERBS, key=len, reverse=True)
)


# A lone "No," / "Okay," / "Well," at the start is how people talk, not a request of its
# own ("No, show me a camera" was answered as two requests: "No" and the camera).
_INTERJECTION = re.compile(r"^\s*(?:no|nope|nah|yes|yeah|yep|yup|ok(?:ay)?|well|so|hmm+|um+|uh+|actually|alright|"
                           r"all\s+right|sure|fine|oh|right|cool|great|nice|wait|hey|listen|look)\s*[,.!?]*\s*$", re.I)


def split_compound_command(text: str) -> list[str]:
    """Split a natural-language command into clauses, or ``[text]`` when it is a
    single action. Empty clauses are dropped; trailing conjunctions trimmed."""
    clauses = _split_compound_command(text)
    kept = [c for c in clauses if not _INTERJECTION.match(c)]
    return kept if kept else clauses


def _split_compound_command(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []

    comma_or_semicolon = r",|;"
    conj = r"\band\b|\bthen\b|&"

    # A token is a real boundary if it's "then" (unconditional), or if the
    # clause that follows STARTS with a command verb after fillers are skipped.
    split_at: list[tuple[int, int]] = []
    for m in re.finditer(rf"({comma_or_semicolon})|({conj})", stripped, re.IGNORECASE):
        token = m.group(0).strip().lower()
        after = _strip_fillers(stripped[m.end():].strip())
        if not after:
            continue  # trailing conjunction — trim it from the segment instead
        if token == "then":
            split_at.append((m.start(), m.end()))
        elif _starts_with_verb(after):
            split_at.append((m.start(), m.end()))

    if not split_at:
        return [_trim_segment(stripped)]

    clauses: list[str] = []
    prev = 0
    for start, end in split_at:
        segment = _trim_segment(stripped[prev:start])
        if segment:
            clauses.append(segment)
        prev = end
    # Final segment: everything after the last boundary.
    tail = _trim_segment(stripped[prev:])
    if tail:
        clauses.append(tail)
    return clauses


def _starts_with_verb(text: str) -> bool:
    """True when ``text`` (already filler-stripped) begins with a command verb."""
    lower = text.lower().lstrip(".,")
    for verb in _VERBS_SORTED:
        if lower.startswith(verb):
            # Boundary check: verb must be followed by whitespace/end/punctuation,
            # so "opened" never matches "open".
            rest = lower[len(verb):]
            if not rest or rest[0] in " \t.,;!?'\"":
                return True
    return False


def _strip_fillers(text: str) -> str:
    return _FILLER_RE.sub("", text, count=1)


def _trim_segment(segment: str) -> str:
    """Remove filler words, trailing conjunctions, and stray punctuation."""
    seg = segment.strip()
    seg = _FILLER_RE.sub("", seg, count=1).strip()
    # Drop a trailing conjunction ("open Chrome and", "open Chrome,").
    seg = re.sub(
        r"\s*(?:\band\b|\bthen\b|[,;]|&)\s*$", "", seg, flags=re.IGNORECASE
    ).strip()
    seg = re.sub(r"[;,\s]+$", "", seg).strip()
    return seg


__all__ = ["split_compound_command"]