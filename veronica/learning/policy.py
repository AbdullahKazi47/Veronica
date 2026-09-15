"""
Behavioral-adaptation policy: decides whether a learned lesson is trustworthy enough to
influence HOW VERONICA executes a plan, and restricts adaptation in safety-sensitive
situations.

This is the safety gate for adaptive behavior. It uses the EXISTING ConfidenceTracker
values (there is no second confidence algorithm) and applies explicit thresholds:

    confidence <  0.80  →  no adaptation
    0.80 ≤ confidence < 0.90  →  advisory preference only
    confidence ≥ 0.90  →  strong preference, IF enough repeated evidence exists

Evidence requirements (the ConfidenceTracker counts successes/failures per strategy):
  * advisory  — at least 1 recorded observation
  * strong    — at least ``min_evidence`` (default 3) repeated independent successes

An EXPLICIT user correction counts as strong evidence by itself (it is the highest-quality
evidence per the quality hierarchy), though it is still subject to the safety gate.

Safety-sensitive operations (delete, shell execution, credentials, irreversible actions,
anything that requires explicit confirmation) are never silently adapted. For these, a
lesson may only inform the planner; it cannot change execution behavior. The existing
permission/confirmation rules remain authoritative.

Confidence is treated as evidence, never as certainty: a policy decision always carries
the underlying confidence and evidence so callers can explain and audit it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from veronica.learning.lesson import Lesson
from veronica.logging_setup import get_logger

logger = get_logger("learning.policy")

# Default thresholds (conservative). Overridable via PolicyThresholds.
MIN_CONFIDENCE = 0.80        # below this: no adaptation
STRONG_CONFIDENCE = 0.90     # at/above this: strong preference (needs evidence)
MIN_EVIDENCE = 3             # repeated independent successes required for strong
ADVISORY_MIN_EVIDENCE = 1    # a single observation is enough for advisory-only

# Tool names / operation kinds that must never be silently behaviorally-adapted.
# These are reversible-irreversible, high-impact, or credential-related operations.
# For these, learned behavior may only appear as advisory INFORMATION, never change
# execution behavior. The permission gate remains authoritative regardless.
SAFETY_SENSITIVE_TOOLS = {
    "delete_file",          # deleting files is irreversible
    "run_shell_command",    # arbitrary shell execution
    "write_file",           # modifying/creating files
    "shutdown", "restart", "reboot", "power_off",
    "install_package", "install_software", "update_system", "update_software",
    "set_security", "change_password", "change_security_config",
    "send_email", "send_message", "transfer_money", "payment", "purchase",
    "credential", "add_credential", "store_credential", "set_credential",
}


@dataclass(frozen=True)
class PolicyThresholds:
    """The thresholds governing behavioral adaptation. Conservative by default."""
    min_confidence: float = MIN_CONFIDENCE
    strong_confidence: float = STRONG_CONFIDENCE
    min_evidence: int = MIN_EVIDENCE
    advisory_min_evidence: int = ADVISORY_MIN_EVIDENCE


class PolicyMode:
    NONE = "none"             # do not adapt
    ADVISORY = "advisory"     # informational only — do not silently change execution
    STRONG = "strong"         # eligible to change execution behavior (safety permitting)


@dataclass
class PolicyDecision:
    """The outcome of evaluating a lesson against policy."""
    mode: str = PolicyMode.NONE
    confidence: float = 0.0
    evidence: int = 0
    safety_blocked: bool = False
    reason: str = ""

    @property
    def apply(self) -> bool:
        """True when the lesson is eligible to influence execution behavior."""
        return self.mode == PolicyMode.STRONG


class BehaviorPolicy:
    """Applies confidence/evidence/safety policy to a Lesson."""

    def __init__(self, thresholds: Optional[PolicyThresholds] = None) -> None:
        self.thresholds = thresholds or PolicyThresholds()

    def evaluate(
        self,
        lesson: Lesson,
        *,
        safety_sensitive: bool = False,
        is_user_correction: bool = False,
        confidence: Optional[float] = None,
        evidence: Optional[int] = None,
    ) -> PolicyDecision:
        """Decide whether *lesson* may influence behavior.

        Parameters
        ----------
        lesson : Lesson
            The advisory lesson under consideration.
        safety_sensitive : bool
            True when the step/operation being considered is safety-sensitive.
        is_user_correction : bool
            True when the lesson derives from an explicit user correction.
        confidence : float, optional
            Override the confidence (normally taken from the lesson).
        evidence : int, optional
            Override the evidence count (normally taken from the lesson).

        Returns
        -------
        PolicyDecision
            ``mode == "strong"`` → eligible to change execution (still safety-gated).
            ``mode == "advisory"`` → informational only.
            ``mode == "none"`` → not eligible.
        """
        conf = confidence if confidence is not None else lesson.confidence
        evidence_count = evidence if evidence is not None else lesson.evidence_count

        # Safety-sensitive operations are never silently adapted. Learned behavior may
        # only inform the planner — never change execution for these.
        if safety_sensitive:
            return PolicyDecision(
                mode=PolicyMode.NONE, confidence=conf, evidence=evidence_count,
                safety_blocked=True,
                reason="safety-sensitive operation; learned behavior may inform but not alter execution",
            )

        # Explicit user corrections are strong evidence (highest quality), but still
        # respect the confidence floor.
        if is_user_correction:
            if conf >= self.thresholds.strong_confidence:
                return PolicyDecision(
                    mode=PolicyMode.STRONG, confidence=conf, evidence=evidence_count,
                    reason="explicit user correction with strong confidence",
                )
            if conf >= self.thresholds.min_confidence:
                return PolicyDecision(
                    mode=PolicyMode.ADVISORY, confidence=conf, evidence=evidence_count,
                    reason="explicit user correction at advisory confidence",
                )
            return PolicyDecision(
                mode=PolicyMode.NONE, confidence=conf, evidence=evidence_count,
                reason="user correction below minimum confidence",
            )

        # General learned strategy: below the confidence floor → no adaptation.
        if conf < self.thresholds.min_confidence:
            return PolicyDecision(
                mode=PolicyMode.NONE, confidence=conf, evidence=evidence_count,
                reason=f"confidence {conf:.2f} below minimum {self.thresholds.min_confidence:.2f}",
            )

        # Strong preference requires BOTH high confidence AND repeated evidence.
        if conf >= self.thresholds.strong_confidence:
            if evidence_count >= self.thresholds.min_evidence:
                return PolicyDecision(
                    mode=PolicyMode.STRONG, confidence=conf, evidence=evidence_count,
                    reason=f"confidence {conf:.2f} and evidence {evidence_count} >= {self.thresholds.min_evidence}",
                )
            return PolicyDecision(
                mode=PolicyMode.ADVISORY, confidence=conf, evidence=evidence_count,
                reason=f"high confidence {conf:.2f} but insufficient evidence {evidence_count} < {self.thresholds.min_evidence}",
            )

        # Advisory band (0.80–0.90): informational only, never silently applied.
        if evidence_count >= self.thresholds.advisory_min_evidence:
            return PolicyDecision(
                mode=PolicyMode.ADVISORY, confidence=conf, evidence=evidence_count,
                reason=f"advisory confidence {conf:.2f}",
            )
        return PolicyDecision(
            mode=PolicyMode.NONE, confidence=conf, evidence=evidence_count,
            reason="no recorded evidence",
        )
