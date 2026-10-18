"""
The live mic meter shown while she listens: dots when it's quiet, rising bars as you
speak, like a voice-mode waveform.

    ● listening  · · · ▂▅▇▆▃▂ · ·

Levels are drawn against the room's own background level, so a noisy room doesn't
look like speech. Falls back to plain ASCII on a terminal that can't show the bars.
"""
from __future__ import annotations

import math
import sys
from typing import Optional

_BARS = "▁▂▃▄▅▆▇█"
_ASCII = ".:-=+*#%"


def _fancy_ok() -> bool:
    try:
        ("●" + _BARS + "·").encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def render(levels: list[float], ambient: Optional[float], fancy: Optional[bool] = None) -> str:
    """One line: a marker and one cell per recent level (oldest first)."""
    fancy = _fancy_ok() if fancy is None else fancy
    bars, dot, mark = (_BARS, "·", "●") if fancy else (_ASCII, ".", "*")
    floor = max(float(ambient or 0.0), 60.0)
    cells = []
    for level in levels:
        ratio = (level or 0.0) / floor
        if ratio < 1.4:
            cells.append(dot)
            continue
        # 1.4x the room -> lowest bar, ~12x -> full bar (log scale, like a VU meter)
        step = int((math.log(ratio) - math.log(1.4)) / (math.log(12.0) - math.log(1.4)) * (len(bars) - 1))
        cells.append(bars[max(0, min(len(bars) - 1, step))])
    return f"  {mark} listening  " + " ".join(cells)
