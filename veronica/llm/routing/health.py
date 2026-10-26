"""
Per-model health for routing. Four states, cached reads, no network call on the
routing hot path, and NO expensive probe per request.

  AVAILABLE    last probe / last real call succeeded
  UNAVAILABLE  provider.is_available() failed, OR too many consecutive real-call
               failures (model is effectively down right now)
  DEGRADED     one real call failed but not enough to flip UNAVAILABLE yet
  UNKNOWN      never checked (initial state). Routing treats UNKNOWN as usable
               (lazy availability) but records that nothing has confirmed it yet.

Real usage feeds health without extra probes: a successful provider.chat() marks
AVAILABLE; a failed one walks DEGRADED -> UNAVAILABLE. `check_health(force=...)`
uses the provider's cheap is_available() probe with a TTL cache.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


_DEFAULT_COOLDOWN_S = 45.0


def _set_aside_for(error: str) -> float:
    """Seconds to leave a model alone after this error (0 = an ordinary failure)."""
    e = (error or "").lower()
    if "429" in e or "too many requests" in e or "resource_exhausted" in e:
        daily = "quota" in e and ("day" in e or "perday" in e)
        return 3600.0 if daily else 600.0
    if "413" in e or "payload too large" in e or "request too large" in e:
        return 3600.0
    return 0.0


class HealthState(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    DEGRADED = "DEGRADED"


@dataclass
class ModelHealth:
    model_id: str
    state: HealthState = HealthState.UNKNOWN
    method: str = "assumed"                 # how we know: assumed | probe | observed
    checked_at: Optional[float] = None
    last_success_at: Optional[float] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    total_calls: int = 0
    successful_calls: int = 0
    degraded_threshold: int = 1
    unavailable_threshold: int = 2
    cooldown_s: float = 45.0      # after a failure, give the model another chance this much later
    # Called after any observed state change so a store can persist the set-aside; never
    # persisted itself, and must not raise.
    on_change: Optional[Callable[["ModelHealth"], None]] = field(default=None, compare=False, repr=False)

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:  # noqa: BLE001 — persistence must never break routing
                pass

    def set_aside_until(self) -> Optional[float]:
        """When a parked model may be tried again (None = not parked by a timed set-aside)."""
        if self.state is HealthState.UNAVAILABLE and self.method == "observed" and self.checked_at is not None:
            return self.checked_at + self.cooldown_s
        return None

    def to_state(self) -> dict:
        """The fields worth keeping across a restart (a spent daily quota shouldn't be
        re-hammered just because she restarted)."""
        return {"state": self.state.value, "method": self.method, "checked_at": self.checked_at,
                "cooldown_s": self.cooldown_s, "last_error": self.last_error,
                "consecutive_failures": self.consecutive_failures}

    def restore(self, data: dict) -> bool:
        """Re-apply a saved set-aside, but only while its cooldown still has time left.
        Returns True if the model is still parked; False means it's due a fresh try."""
        try:
            checked_at = data.get("checked_at")
            cooldown = float(data.get("cooldown_s") or _DEFAULT_COOLDOWN_S)
            if checked_at is None or data.get("state") != HealthState.UNAVAILABLE.value:
                return False
            if time.time() - float(checked_at) >= cooldown:
                return False
            self.state = HealthState.UNAVAILABLE
            self.method = "observed"
            self.checked_at = float(checked_at)
            self.cooldown_s = cooldown
            self.last_error = data.get("last_error")
            self.consecutive_failures = int(data.get("consecutive_failures") or self.unavailable_threshold)
            return True
        except (TypeError, ValueError):
            return False

    def recently_failed(self, now: Optional[float] = None) -> bool:
        """Failed within the cooldown: route around it for now."""
        if self.state not in (HealthState.DEGRADED, HealthState.UNAVAILABLE) or self.checked_at is None:
            return False
        return ((now or time.time()) - self.checked_at) < self.cooldown_s

    def retry_due(self, now: Optional[float] = None) -> bool:
        """An observed outage is temporary: after the cooldown the model may be tried again."""
        return self.checked_at is not None and ((now or time.time()) - self.checked_at) >= self.cooldown_s

    def record_success(self, latency_s: Optional[float] = None) -> None:
        self.cooldown_s = _DEFAULT_COOLDOWN_S
        self.total_calls += 1
        self.successful_calls += 1
        self.consecutive_failures = 0
        self.last_success_at = time.time()
        self.checked_at = time.time()
        self.last_error = None
        self.method = "observed"
        self.state = HealthState.AVAILABLE
        self._changed()

    def record_failure(self, error: str) -> None:
        self.total_calls += 1
        self.consecutive_failures += 1
        self.checked_at = time.time()
        self.method = "observed"
        self.last_error = _short(error)
        set_aside = _set_aside_for(error)
        if set_aside:
            # Not a blip: a spent daily allowance (429 "quota") or a request this model can't
            # take (413). Seen live: every step of one request re-tried the same spent
            # models and it took 137 s. Off the list for a while, then tried again.
            self.cooldown_s = set_aside
            self.state = HealthState.UNAVAILABLE
        elif self.consecutive_failures >= self.unavailable_threshold:
            self.state = HealthState.UNAVAILABLE
        else:
            self.state = HealthState.DEGRADED
        self._changed()

    def mark_unavailable(self, error: str) -> None:
        """E.g. is_available() probe failed — we know it's down, no call needed."""
        self.checked_at = time.time()
        self.method = "probe"
        self.last_error = _short(error)
        self.state = HealthState.UNAVAILABLE
        self._changed()

    def mark_available(self) -> None:
        """E.g. is_available() probe succeeded."""
        self.checked_at = time.time()
        self.method = "probe"
        self.last_error = None
        self.state = HealthState.AVAILABLE
        self._changed()

    def usable(self) -> bool:
        """Routing filter: only explicitly-UNAVAILABLE models are off-limits. UNKNOWN
        and DEGRADED are still selectable (lazy availability); DEGRADED is penalized."""
        return self.state is not HealthState.UNAVAILABLE

    def summary(self) -> dict:
        return {
            "model_id": self.model_id,
            "state": self.state.value,
            "method": self.method,
            "consecutive_failures": self.consecutive_failures,
            "success_rate": (
                round(self.successful_calls / max(self.total_calls, 1) * 100, 1)
            ),
            "last_error": self.last_error,
        }


def _short(message: str, limit: int = 160) -> str:
    message = (message or "").strip()
    return message if len(message) <= limit else message[:limit - 1] + "…"


__all__ = ["HealthState", "ModelHealth"]