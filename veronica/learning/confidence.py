"""
ConfidenceTracker — a small, deterministic, evidence-based confidence mechanism.

Confidence here is NOT a claim of mathematical certainty. It is a simple, transparent
estimate of "how often has this strategy/tool worked for us" based on observed
outcomes, so that a *future* phase (adaptive planning) has a principled, auditable
number to rank strategies by. It rises with repeated success and falls with failure.

Design rules:
  * Deterministic and cheap — a formula over integer counts, no randomness, no deps.
  * Recency-aware without being clever: an optional bounded window means old evidence
    naturally fades once enough new evidence accumulates.
  * Never a policy authority — confidence can *rank* options but can never override the
    safety/permission gates. (That guarantee is enforced at the wiring layer, not here.)

The confidence formula (a smoothed empirical rate):
    confidence = (successes + 1) / (successes + failures + 2)
The +1/+2 smoothing keeps a strategy with one success from claiming certainty (1.0),
and keeps an unobserved strategy at 0.5 (neutral) rather than 0.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from veronica.logging_setup import get_logger

logger = get_logger("learning.confidence")


class ConfidenceTracker:
    def __init__(self, window: Optional[int] = None, path: Optional[Path | str] = None) -> None:
        self.window = window          # optional bounded recency window (None = unbounded)
        self._lock = threading.Lock()
        self._counts: dict[str, dict[str, int]] = {}  # subject -> {"successes":n,"failures":n}
        self.path = Path(path) if path else None
        if self.path and self.path.exists():
            self._load()

    # -- observation ---------------------------------------------------------------
    def record(self, subject: str, success: bool) -> None:
        """Record one observed outcome for ``subject`` (a tool/strategy name)."""
        with self._lock:
            bucket = self._counts.setdefault(subject, {"successes": 0, "failures": 0})
            bucket["successes" if success else "failures"] += 1
            # Optional recency window: keep only the last N observations per subject.
            if self.window and self.window > 0:
                total = bucket["successes"] + bucket["failures"]
                if total > self.window:
                    excess = total - self.window
                    if bucket["failures"] >= excess:
                        bucket["failures"] -= excess
                    else:
                        bucket["successes"] -= (excess - bucket["failures"])
                        bucket["failures"] = 0
        if self.path:
            self._save()

    def confidence(self, subject: str) -> float:
        """Empirical smoothed success rate in [0, 1]. 0.5 when never observed."""
        with self._lock:
            bucket = self._counts.get(subject)
        if not bucket:
            return 0.5
        s = bucket["successes"]
        f = bucket["failures"]
        return round((s + 1) / (s + f + 2), 4)

    def stats(self, subject: str) -> dict[str, int]:
        with self._lock:
            b = self._counts.get(subject)
            return {"successes": b["successes"], "failures": b["failures"]} if b else \
                {"successes": 0, "failures": 0}

    def subjects(self) -> list[str]:
        with self._lock:
            return list(self._counts.keys())

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {k: dict(v) for k, v in self._counts.items()}

    # -- persistence ---------------------------------------------------------------
    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self.path.write_text(json.dumps(self._counts, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Failed to persist confidence data to %s", self.path)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._counts = {k: {"successes": v.get("successes", 0), "failures": v.get("failures", 0)}
                            for k, v in raw.items()}
        except (OSError, ValueError, TypeError):
            logger.warning("Failed to load confidence data from %s; starting empty.", self.path)
            self._counts = {}
