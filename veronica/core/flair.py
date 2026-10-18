"""
Her character in the quick answers.

Replies from the model already sound like her. The instant ones don't: a tool's result
read out word for word ("It's 15:29 on Saturday, 17 October 2026.", "Opened Spotify.")
made her sound like a machine between the real conversations. This adds the small human
touches the persona asks for, without a model call or any delay:

  * the time said the way a person says it ("It's 6:42 in the evening.");
  * "sir" or "boss" now and then on a short, successful answer, never every time and
    never on an error, a question or a long answer;
  * varied, warm sign-offs and acknowledgements instead of the same "Okay." each time.

It never changes facts, numbers or names in a reply.
"""
from __future__ import annotations

import datetime as dt
import random
import re
from typing import Optional

_rng = random.Random()
ADDRESSES = ("sir", "boss")       # what she calls the user (2026-10-18: the user asked for both)
ADDRESS = ADDRESSES[-1]
_ADDRESSED = re.compile(r"\b(?:" + "|".join(ADDRESSES) + r")\b", re.I)

_GOODBYES = ("Anytime, boss.", "I'll be right here.", "Talk soon.", "Off you go, boss.", "Call me if you need me.",
             "Right you are.", "Okay, boss.", "Anytime, sir.", "Very good, sir.")
_ACKS = ("Done.", "Done, boss.", "Done, sir.", "All set.", "Taken care of.", "There you go.")


def address() -> str:
    """'sir' or 'boss', picked at random."""
    return _rng.choice(ADDRESSES)


def part_of_day(hour: int) -> str:
    if hour < 5:
        return "at night"
    if hour < 12:
        return "in the morning"
    if hour < 17:
        return "in the afternoon"
    if hour < 21:
        return "in the evening"
    return "at night"


def spoken_time(now: Optional[dt.datetime] = None) -> str:
    """'It's 6:42 in the evening, Saturday the 17th.'"""
    now = now or dt.datetime.now()
    hour12 = now.hour % 12 or 12
    clock = f"{hour12}" if now.minute == 0 else f"{hour12}:{now.minute:02d}"
    when = "noon" if (now.hour, now.minute) == (12, 0) else ("midnight" if (now.hour, now.minute) == (0, 0) else None)
    day = now.day
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    head = f"It's {when}" if when else (f"It's {clock} o'clock {part_of_day(now.hour)}" if now.minute == 0
                                        else f"It's {clock} {part_of_day(now.hour)}")
    return f"{head}, {now.strftime('%A')} the {day}{suffix}."


def goodbye() -> str:
    return _rng.choice(_GOODBYES)


def acknowledge() -> str:
    return _rng.choice(_ACKS)


def dress(reply: str, ok: bool = True, chance: float = 0.3) -> str:
    """A short, successful tool reply, now and then addressed to the user."""
    text = (reply or "").strip()
    if not ok or not text or len(text) > 90 or text.endswith("?") or _ADDRESSED.search(text):
        return reply
    if text in ("Done.", "Done"):
        return acknowledge()
    if _rng.random() >= chance or not text.endswith("."):
        return reply
    # "Opening Spotify." -> "Opening Spotify, boss." / "Opening Spotify, sir."
    return text[:-1] + f", {address()}."
