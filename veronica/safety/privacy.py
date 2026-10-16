"""
Privacy guard: scrub secrets and sensitive identifiers out of everything VERONICA sends
to a cloud language model (Gemini and any other hosted provider).

The honest boundary: VERONICA's reasoning happens in the cloud model, so your actual
requests and the facts you asked her to remember DO go to it — that is how she answers
you. What this guard guarantees is that a narrow, high-risk set never leaves the machine,
even if you say one aloud, it is on your screen, or it is in a file she reads:

  * API keys, tokens, JWTs, bearer/authorization headers, private keys, passwords
  * card numbers (Luhn-checked), CVV, bank account / routing / IFSC numbers
  * government IDs: US SSN, India Aadhaar and PAN
  * anything matching a pattern you add in the `privacy:` config

Each is replaced with ``[REDACTED]`` before the network call. The guard is on by default
and fails safe (an unconfigured guard still redacts). It only counts what it removes —
never stores the values — so "did you redact anything?" can be answered without keeping
the very data it just protected.

Design limit, on purpose: this guard sanitizes outbound text. It is not a licence to
send more; it never weakens the permission gate, and the screen/camera image path still
only runs when you explicitly ask her to look.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from veronica.learning.experience import _SECRET_VALUE_PATTERNS
from veronica.logging_setup import get_logger

logger = get_logger("safety.privacy")

REDACTED = "[REDACTED]"

# Financial / government identifiers, each (category, pattern). Ordered so the most
# specific run first. Card numbers are Luhn-checked separately to avoid false positives.
# Spoken/written form "my password is X", "passcode: 1234" — keep the label, drop the value.
# (The shared secret patterns only catch the `password=value` form.)
_LABELED_SECRET = re.compile(
    r"\b(pass\s?word|pass\s?code|pass\s?phrase|secret|api\s*key|access\s*key|private\s*key|security\s*key)\b"
    r"\s*(?:is|was|=|:|->)\s*([^\s,.;]+)", re.IGNORECASE)
_LABELED = re.compile(
    r"\b(?:cvv|cvc|card\s*verification|pin|otp|passcode|security\s*code|account\s*(?:number|no|#)|"
    r"routing\s*(?:number|no)|sort\s*code)\b\s*(?:is|are|=|:|number|no\.?)?\s*[:#]?\s*([0-9][0-9\- ]{2,})",
    re.IGNORECASE)
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pan", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),               # India PAN
    ("ifsc", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),              # India IFSC
    ("ssn", re.compile(r"\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b")),          # US SSN
    # India Aadhaar: 3 groups of 4, not embedded in a longer run (so a 16-digit card
    # number can't be mistaken for one — cards are handled separately, before this).
    ("aadhaar", re.compile(r"(?<!\d)(?<!\d )[2-9][0-9]{3}\s[0-9]{4}\s[0-9]{4}(?!\s?\d)")),
]
# A run of 13–19 digits (card-like), maybe grouped by spaces/hyphens. Luhn-gated.
_CARD = re.compile(r"\b(?:[0-9][ -]?){13,19}\b")


def _luhn(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class PrivacyConfig:
    enabled: bool = True
    redact_financial: bool = True
    redact_government_ids: bool = True
    log_redactions: bool = True           # log (locally) that N items of a category were removed
    extra_patterns: tuple[str, ...] = ()  # user-supplied regexes, from config


class PrivacyGuard:
    def __init__(self, cfg: Optional[PrivacyConfig] = None,
                 on_redact: Optional[Callable[[str], None]] = None) -> None:
        self.cfg = cfg or PrivacyConfig()
        self._on_redact = on_redact
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}          # category -> times removed (never the values)
        self._extra = []
        for pat in self.cfg.extra_patterns:
            try:
                self._extra.append(re.compile(pat))
            except re.error:
                logger.warning("Ignoring invalid privacy.extra_patterns regex: %r", pat)

    def _hit(self, category: str) -> None:
        with self._lock:
            self.counts[category] = self.counts.get(category, 0) + 1
        if self.cfg.log_redactions:
            logger.info("Privacy guard removed a %s before sending to the cloud model.", category)
        if self._on_redact:
            try:
                self._on_redact(category)
            except Exception:  # noqa: BLE001
                pass

    def scrub_text(self, text: str) -> str:
        if not self.cfg.enabled or not text:
            return text
        out = text
        # 1a. Spoken "my password is X" — keep the label, redact the value.
        def _labeled_secret(m):
            self._hit("secret")
            return m.group(0).replace(m.group(2), REDACTED)
        out = _LABELED_SECRET.sub(_labeled_secret, out)
        # 1b. Credentials/keys/tokens (shared with the experience redactor).
        for pat in _SECRET_VALUE_PATTERNS + self._extra:
            def _repl(m, p=pat):
                self._hit("secret" if p in _SECRET_VALUE_PATTERNS else "custom")
                return REDACTED
            out = pat.sub(_repl, out)
        # 2. Card numbers (Luhn-checked) FIRST, so a 16-digit card is caught as a card and
        #    never mistaken for a 12-digit government ID.
        if self.cfg.redact_financial:
            def _card(m):
                token = m.group(0)
                if _luhn(token):
                    self._hit("card")
                    return REDACTED
                return token
            out = _CARD.sub(_card, out)
        # 3. Government IDs.
        if self.cfg.redact_government_ids:
            for category, pat in _PATTERNS:
                def _gov(m, c=category):
                    self._hit(c)
                    return REDACTED
                out = pat.sub(_gov, out)
        # 4. Labeled financial codes (CVV / PIN / account / routing ...).
        if self.cfg.redact_financial:
            def _labeled(m):                      # keep the label, redact only the value
                self._hit("financial")
                return m.group(0).replace(m.group(1), REDACTED)
            out = _LABELED.sub(_labeled, out)
        return out

    def scrub_wire(self, messages: list[dict]) -> list[dict]:
        """Scrub the `content` of each outbound chat message just before it is sent.
        Handles plain-string content and the vision list-of-parts form. Never mutates
        the caller's objects."""
        if not self.cfg.enabled:
            return messages
        cleaned = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                m = {**m, "content": self.scrub_text(content)}
            elif isinstance(content, list):
                m = {**m, "content": [
                    {**part, "text": self.scrub_text(part["text"])}
                    if isinstance(part, dict) and isinstance(part.get("text"), str) else part
                    for part in content]}
            cleaned.append(m)
        return cleaned

    def report(self) -> dict[str, int]:
        with self._lock:
            return dict(self.counts)


# ---- module-level active guard (fail-safe: redacts even before configure()) ----------
_guard = PrivacyGuard()
_cfg_lock = threading.Lock()


def get_guard() -> PrivacyGuard:
    return _guard


def configure(config) -> PrivacyGuard:
    """Build the active guard from config.privacy (called once at startup)."""
    global _guard
    raw = getattr(config, "privacy", None)
    if raw is None:
        return _guard
    cfg = PrivacyConfig(
        enabled=getattr(raw, "enabled", True),
        redact_financial=getattr(raw, "redact_financial", True),
        redact_government_ids=getattr(raw, "redact_government_ids", True),
        log_redactions=getattr(raw, "log_redactions", True),
        extra_patterns=tuple(getattr(raw, "extra_patterns", ()) or ()),
    )
    with _cfg_lock:
        _guard = PrivacyGuard(cfg)
    logger.info("Privacy guard configured (enabled=%s).", cfg.enabled)
    return _guard
