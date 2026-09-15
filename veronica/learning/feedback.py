"""
User feedback abstraction for the learning subsystem.

Feedback is how VERONICA learns *intent* ("I wanted it done a different way"), which an
outcome record alone cannot capture. This module is deliberately conservative:

  * Only EXPLICIT, strongly-supported feedback becomes a learning signal. A passing
    remark ("that was cool") is not treated as a durable preference.
  * We never try to infer sensitive personal information from conversational text.
  * The classifier returns ``None`` (no signal) unless the text matches a clear,
    imperative feedback pattern — so we don't spam the lesson store with noise.

Feedback kinds (explicit vocabulary):
    positive | negative | correction | preference | dont_do_that | do_it_this_way
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from veronica.learning.experience import _new_id
from veronica.learning.confidence import ConfidenceTracker
from veronica.logging_setup import get_logger

logger = get_logger("learning.feedback")


class FeedbackKind:
    POSITIVE = "positive"
    NEGATIVE = "negative"
    CORRECTION = "correction"
    PREFERENCE = "preference"
    DONT_DO_THAT = "dont_do_that"
    DO_IT_THIS_WAY = "do_it_this_way"


# Explicit, imperative feedback patterns. Only these produce a signal.
_POSITIVE_PATTERNS = [
    re.compile(r"\b(perfect|great|good job|nice|awesome|exactly right|correct|well done)\b", re.IGNORECASE),
]
_NEGATIVE_PATTERNS = [
    re.compile(r"\b(that'?s wrong|not right|incorrect|bad job|terrible|no,? (don'?t|that'?s not))\b", re.IGNORECASE),
]
_DONT_DO_THAT_PATTERNS = [
    re.compile(r"\b(don'?t (do|use|open|run|close|touch)|never (do|use|open|run)|stop (doing|using|opening|closing))\b", re.IGNORECASE),
]
_DO_IT_THIS_WAY_PATTERNS = [
    re.compile(r"\b(use .+ instead|instead (use|of|open|run)|do it (this way|like this|that way)|next time (use|do|try|open)|prefer (using|to use))\b", re.IGNORECASE),
]
_PREFERENCE_PATTERNS = [
    re.compile(r"\b(i prefer|prefer using|always use|from now on use|use .+ from now on|my preferred)\b", re.IGNORECASE),
]
_CORRECTION_PATTERNS = [
    re.compile(r"\b(no,? (use|open|run|do)|use .+ instead of .+|you should have (used|opened|run)|actually (use|open|run|it'?s))\b", re.IGNORECASE),
]


@dataclass
class UserFeedback:
    """A single piece of explicit user feedback, optionally carrying a derived signal."""

    kind: str
    text: str
    experience_id: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    feedback_id: str = field(default_factory=_new_id)
    subject: str = ""                     # e.g. the tool/preference named ("Chrome")
    signal: Optional[dict[str, Any]] = None  # optional derived LessonSignal as a dict

    def to_dict(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "kind": self.kind,
            "text": self.text,
            "experience_id": self.experience_id,
            "timestamp": self.timestamp,
            "subject": self.subject,
            "signal": self.signal,
        }


def classify_feedback(text: str) -> Optional[UserFeedback]:
    """Parse ``text`` into a UserFeedback only when it's explicit. Returns None for
    conversational noise so it never becomes a permanent preference."""
    if not text or not text.strip():
        return None
    t = text.strip()

    def _match(patterns):
        return any(p.search(t) for p in patterns)

    # Priority order: the most specific, imperative/semantic patterns first, then the
    # vaguer sentiment. Without this, "I prefer using X" would wrongly fall through to
    # the generic "use X instead" pattern, and "No, use X instead" to the generic
    # "do it this way" pattern.
    if _match(_DONT_DO_THAT_PATTERNS):
        return UserFeedback(kind=FeedbackKind.DONT_DO_THAT, text=t, subject=_extract_subject(t))
    if _match(_PREFERENCE_PATTERNS):
        return UserFeedback(kind=FeedbackKind.PREFERENCE, text=t, subject=_extract_subject(t))
    if _match(_CORRECTION_PATTERNS):
        return UserFeedback(kind=FeedbackKind.CORRECTION, text=t, subject=_extract_subject(t))
    if _match(_DO_IT_THIS_WAY_PATTERNS):
        return UserFeedback(kind=FeedbackKind.DO_IT_THIS_WAY, text=t, subject=_extract_subject(t))
    if _match(_POSITIVE_PATTERNS):
        return UserFeedback(kind=FeedbackKind.POSITIVE, text=t)
    if _match(_NEGATIVE_PATTERNS):
        return UserFeedback(kind=FeedbackKind.NEGATIVE, text=t)
    return None


def _extract_subject(text: str) -> str:
    """Best-effort pull of the thing being preferred/avoided (e.g. "Chrome" from
    "use Chrome instead"). Returns "" when nothing obvious."""
    m = re.search(
        r"\b(?:use|open|run|launch|close|don'?t use|stop using|prefer|switch to)\s+"
        r"([A-Za-z0-9_][A-Za-z0-9_ .\-]{0,39})",
        text, re.IGNORECASE,
    )
    if not m:
        return ""
    subject = m.group(1).strip()
    # Trim trailing stop-words / punctuation so "Chrome instead." → "Chrome".
    subject = re.sub(r"\s+(?:instead|rather than|from now on|next time).*$", "", subject, flags=re.IGNORECASE)
    return subject.strip().rstrip(".,!?")


class FeedbackRecorder:
    """Attaches explicit feedback to a lesson/experience and records it.

    Small and thin: it classifies the text, persists the feedback, and — only for an
    explicit correction/preference — emits a structured ``user_correction`` signal so
    downstream phases (and tests) can see it. It never mutates global behavior by
    itself; that is a Phase-3 concern gated by confidence and safety."""

    def __init__(self, confidence: Optional[ConfidenceTracker] = None) -> None:
        self.confidence = confidence or ConfidenceTracker()
        self.recorded: list[UserFeedback] = []

    def process(self, text: str, experience_id: Optional[str] = None) -> Optional[UserFeedback]:
        fb = classify_feedback(text)
        if fb is None:
            return None
        fb.experience_id = experience_id
        # A correction/preference becomes a lesson signal subject (only when explicit).
        if fb.kind in (FeedbackKind.CORRECTION, FeedbackKind.DONT_DO_THAT,
                       FeedbackKind.DO_IT_THIS_WAY, FeedbackKind.PREFERENCE):
            conf = self.confidence.confidence(fb.subject) if fb.subject else 0.5
            fb.signal = {
                "type": "user_correction",
                "subject": fb.subject,
                "context": {"feedback_kind": fb.kind},
                "evidence_experience_id": experience_id,
                "confidence": conf,
            }
        self.recorded.append(fb)
        logger.info("feedback recorded kind=%s subject=%r", fb.kind, fb.subject)
        return fb
