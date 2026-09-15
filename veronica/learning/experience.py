"""
The Experience model — the atomic unit of VERONICA's learning subsystem.

Conceptual line drawn against memory/:

    MEMORY    — "what happened / what do we know?"  (facts, conversation)
    LEARNING  — "what should VERONICA do differently next time?"

An Experience is a structured, serializable record of ONE task's lifecycle: what was
asked, what was planned, what actually ran, how it turned out, and (when available) what
the user said about it. Experiences are what the LearningEngine turns into signals.

Privacy is a first-class concern here. Nothing should be persisted that a later
ExperienceStore could not safely keep — so `redact_value()`/`redact_experience()` strip
secrets (API keys, tokens, passwords, cookies, private keys, authorization headers,
env-derived secrets) before a record is written to disk.

This module is deliberately dependency-free (stdlib only) and importable on its own, so
the task executor can build an Experience without pulling in the whole learning stack.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

# --- explicit outcome vocabulary (not arbitrary strings) -------------------------
class ExperienceOutcome:
    SUCCESS = "success"
    FAILED = "failed"
    RECOVERED = "recovered"
    CANCELLED = "cancelled"


class VerificationResult:
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


# --- redaction / sanitization -----------------------------------------------------

# Dict keys that, by name, always carry a secret value.
_SECRET_KEY_NAMES = {
    "api_key", "apikey", "api-key",
    "token", "auth_token", "access_token", "refresh_token",
    "password", "passwd", "pwd", "pass",
    "secret", "client_secret", "client_secret_key", "consumer_secret",
    "authorization", "auth", "auth_header", "cookie", "cookies", "session",
    "private_key", "secret_key", "access_key", "secret_access_key",
    "credential", "credentials", "webhook_secret",
}

# Substrings that flag a dict key as sensitive even when not an exact match.
_SECRET_KEY_HINTS = ("key", "token", "secret", "password", "passwd", "credential", "cookie", "authorization")

# Value patterns that are secrets regardless of their key (so we catch values that
# slipped in under a non-secret-looking key, e.g. a long hex string in `data`).
_SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}", re.IGNORECASE),            # OpenAI-style
    re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE),                 # AWS access key
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),                            # GitHub PAT
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]+"),                       # Slack token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),  # PEM private key
    re.compile(r"bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),   # bearer token
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"(?i)(password|passwd|secret)\s*[=:]\s*\S+"),       # password=... literal
]

# Environment-variable style names that are always treated as secret.
_ENV_SECRET_PATTERNS = [
    re.compile(r".*_(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)$", re.IGNORECASE),
    re.compile(r".*(API_?KEY|AUTH_?TOKEN|CLIENT_?SECRET|PRIVATE_?KEY).*", re.IGNORECASE),
]

REDACTED = "[REDACTED]"


def _is_secret_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    if k in _SECRET_KEY_NAMES:
        return True
    if k.endswith(("_key", "_token", "_secret", "_password", "_passwd", "_credential", "_cookie")):
        return True
    return any(hint in k for hint in _SECRET_KEY_HINTS) and len(k) <= 48


def _looks_like_env_secret(key: str) -> bool:
    return any(p.match(key) for p in _ENV_SECRET_PATTERNS)


def _looks_like_secret_value(value: str) -> bool:
    return any(p.search(value) for p in _SECRET_VALUE_PATTERNS)


def redact_value(value: Any, key: str = "") -> Any:
    """Recursively redact secrets from an arbitrary value tree. Strings that look like
    secrets, or values under secret-named keys, become ``[REDACTED]``."""
    if isinstance(value, dict):
        return {k: redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, key) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v, key) for v in value)
    if isinstance(value, str):
        if _is_secret_key(key) or _looks_like_env_secret(key) or _looks_like_secret_value(value):
            return REDACTED
        return value
    return value


def redact_experience(exp: "Experience") -> "Experience":
    """Return a sanitized copy of ``exp`` safe to persist (never mutates the input)."""
    safe = Experience(
        goal=_redact_string(exp.goal),
        outcome=exp.outcome,
        task_id=exp.task_id,
        timestamp=exp.timestamp,
        experience_id=exp.experience_id,
        context=redact_value(exp.context),
        plan=redact_value(exp.plan),
        actions=redact_value(exp.actions),
        tool_calls=list(exp.tool_calls),
        verification=exp.verification,
        retries=exp.retries,
        recovery_actions=list(exp.recovery_actions),
        duration=exp.duration,
        failed_step=exp.failed_step,
        failure_reason=_redact_string(exp.failure_reason),
        final_status=exp.final_status,
        user_feedback=redact_value(exp.user_feedback) if exp.user_feedback else None,
        metadata=redact_value(exp.metadata),
        # Phase 4: adaptation fields (no secrets by design; redact anyway for safety)
        original_plan=redact_value(exp.original_plan) if exp.original_plan else None,
        adaptations=redact_value(exp.adaptations) if exp.adaptations else [],
        adaptation_applied=exp.adaptation_applied,
        adaptation_success=exp.adaptation_success,
    )
    return safe


def _redact_string(s: Optional[str]) -> str:
    if not s:
        return s or ""
    return REDACTED if _looks_like_secret_value(s) else s


# --- the model --------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Experience:
    """One structured record of a task's lifecycle."""

    goal: str
    outcome: str = ExperienceOutcome.SUCCESS      # success | failed | recovered | cancelled
    task_id: str = ""
    timestamp: str = field(default_factory=_now_iso)
    experience_id: str = field(default_factory=_new_id)
    context: dict[str, Any] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)   # planned steps
    actions: list[dict[str, Any]] = field(default_factory=list)  # executed steps (sanitized)
    tool_calls: list[str] = field(default_factory=list)        # tool names in order
    verification: str = VerificationResult.NOT_APPLICABLE
    retries: int = 0
    recovery_actions: list[str] = field(default_factory=list)
    duration: float = 0.0
    failed_step: str = ""
    failure_reason: str = ""
    final_status: str = ""                        # terminal task.status ("completed"|...)
    user_feedback: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Phase 4 — adaptation audit trail (optional, ignored if None).
    original_plan: Optional[list[dict[str, Any]]] = None
    adaptations: list[dict[str, Any]] = field(default_factory=list)
    adaptation_applied: bool = False
    adaptation_success: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "timestamp": self.timestamp,
            "goal": self.goal,
            "task_id": self.task_id,
            "outcome": self.outcome,
            "final_status": self.final_status,
            "context": self.context,
            "plan": self.plan,
            "actions": self.actions,
            "tool_calls": self.tool_calls,
            "verification": self.verification,
            "retries": self.retries,
            "recovery_actions": self.recovery_actions,
            "duration": self.duration,
            "failed_step": self.failed_step,
            "failure_reason": self.failure_reason,
            "user_feedback": self.user_feedback,
            "metadata": self.metadata,
            "original_plan": self.original_plan,
            "adaptations": self.adaptations,
            "adaptation_applied": self.adaptation_applied,
            "adaptation_success": self.adaptation_success,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Experience":
        return cls(
            goal=data.get("goal", ""),
            outcome=data.get("outcome", ExperienceOutcome.SUCCESS),
            task_id=data.get("task_id", ""),
            timestamp=data.get("timestamp", _now_iso()),
            experience_id=data.get("experience_id", _new_id()),
            context=data.get("context", {}),
            plan=data.get("plan", []),
            actions=data.get("actions", []),
            tool_calls=data.get("tool_calls", []),
            verification=data.get("verification", VerificationResult.NOT_APPLICABLE),
            retries=data.get("retries", 0),
            recovery_actions=data.get("recovery_actions", []),
            duration=data.get("duration", 0.0),
            failed_step=data.get("failed_step", ""),
            failure_reason=data.get("failure_reason", ""),
            final_status=data.get("final_status", ""),
            user_feedback=data.get("user_feedback"),
            metadata=data.get("metadata", {}),
            original_plan=data.get("original_plan"),
            adaptations=data.get("adaptations", []),
            adaptation_applied=data.get("adaptation_applied", False),
            adaptation_success=data.get("adaptation_success"),
        )
