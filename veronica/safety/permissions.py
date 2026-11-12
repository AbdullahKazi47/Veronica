"""
Permission and safety controls.

None of the six reference repositories had anything like this — every one of them will
happily delete a file, send an email, or shell out to the OS the instant a regex matches
or an LLM decides to. VERONICA treats that as the biggest gap to close.

Every Tool declares a PermissionLevel. Anything above SAFE must be confirmed before it
executes — by the human in an interactive session, or rejected outright in contexts
(like scheduled/autonomous runs) where no human is present to confirm.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("safety")


class PermissionLevel(IntEnum):
    SAFE = 0        # read-only, no side effects (e.g. "what time is it")
    MODERATE = 1    # local side effects, easily reversible (open an app, play a video)
    SENSITIVE = 2   # touches files, sends data out, costs a little (send email, delete file)
    CRITICAL = 3    # irreversible / high impact (system shutdown, bulk delete, payments)


@dataclass
class PermissionDecision:
    allowed: bool
    reason: str = ""


class PermissionGate:
    """
    Decides whether a tool call may proceed. In interactive mode, anything above the
    configured threshold is routed to a confirmation callback (e.g. "ask the user").
    In non-interactive/autonomous mode (no confirmation callback registered),
    anything above the threshold is refused by default rather than silently allowed.
    """

    def __init__(
        self,
        threshold: PermissionLevel = PermissionLevel.MODERATE,
        confirm_callback: Optional[Callable[[str, PermissionLevel], bool]] = None,
        dry_run: bool = False,
    ) -> None:
        self.threshold = threshold
        self.confirm_callback = confirm_callback
        self.dry_run = dry_run
        import threading
        self._strict = threading.local()

    def stricter(self, reason: str):
        """For a stretch of work (one turn): every action at MODERATE or above must be confirmed,
        even ones that normally run without asking. Only ever raises the bar: the threshold used
        is the LOWER of the two, and dry-run and refusals behave exactly as before."""
        import contextlib
        gate = self

        @contextlib.contextmanager
        def scope():
            before = getattr(gate._strict, "reason", "")
            gate._strict.reason = reason or "please confirm"
            try:
                yield
            finally:
                gate._strict.reason = before
        return scope()

    def check(self, tool_name: str, level: PermissionLevel, description: str) -> PermissionDecision:
        if self.dry_run and level >= PermissionLevel.MODERATE:
            logger.info("DRY RUN: would require permission for %s (%s)", tool_name, level.name)
            return PermissionDecision(allowed=False, reason="dry-run mode: action simulated, not executed")

        strict = getattr(getattr(self, "_strict", None), "reason", "")
        threshold = min(self.threshold, PermissionLevel.MODERATE) if strict else self.threshold
        if level < threshold:
            return PermissionDecision(allowed=True)
        if strict:
            description = f"{strict} {description}"

        if self.confirm_callback is None:
            logger.warning(
                "Refusing '%s' (%s): no confirmation channel available for a "
                "permission at or above %s.", tool_name, description, threshold.name
            )
            return PermissionDecision(
                allowed=False,
                reason=f"'{tool_name}' needs {level.name} permission but nothing can confirm it here.",
            )

        approved = self.confirm_callback(description, level)
        if approved:
            logger.info("User approved '%s' (%s)", tool_name, level.name)
        else:
            logger.info("User declined '%s' (%s)", tool_name, level.name)
        return PermissionDecision(allowed=approved, reason="" if approved else "user declined")

    @staticmethod
    def level_from_name(name: str) -> PermissionLevel:
        return PermissionLevel[name.upper()]
