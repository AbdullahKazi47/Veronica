"""
Phase 9B — Action Proposal + Permission Gate (observation-to-action bridge).

Vision proposes; PermissionGate decides; ComputerController acts ONLY after
explicit approval + re-observe/re-verify (TOCTOU defense). No direct vision→action
path exists.
"""
from __future__ import annotations

import time
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List

from veronica.vision.visual_grounding import GroundingStatus, VisualTargetCandidate, TargetCorrelationResult
from veronica.vision.coordinate_safety import CoordinateSafetyValidator, create_coordinate_validator, ValidationOutcome
from veronica.safety.permissions import PermissionGate, PermissionLevel, PermissionDecision

logger = __import__("veronica.logging_setup").logging_setup.get_logger("vision.action_proposal")

class ActionType(str, Enum):
    CLICK = "CLICK"
    TYPE_TEXT = "TYPE_TEXT"
    OPEN_APPLICATION = "OPEN_APPLICATION"

class ActionRisk(str, Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    BLOCKED = "BLOCKED"

class ActionProposalStatus(str, Enum):
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"
    EXECUTED = "EXECUTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTED_UNVERIFIED = "EXECUTED_UNVERIFIED"
    # NOT a final success state; verification still required
    OPEN_APP_VERIFIED = "OPEN_APP_VERIFIED"

VISION_ACTION_TYPES = {ActionType.CLICK, ActionType.TYPE_TEXT}

@dataclass
class ActionProposal:
    proposal_id: str
    action_type: ActionType
    target_reference: Optional[object] = None  # existing TargetReference (reuse)
    target_name: str = ""
    target_window: Optional[str] = None
    target_process: Optional[str] = None
    target_point: Optional[tuple] = None
    # OPEN_APPLICATION target
    application_name: Optional[str] = None
    application_id: Optional[str] = None
    text_to_type: Optional[str] = None
    risk_level: ActionRisk = ActionRisk.MODERATE
    source: str = "brain/planner"
    grounding_result: Optional[TargetCorrelationResult] = None
    confidence: float = 0.0
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    expiration: float = 30.0  # seconds
    status: ActionProposalStatus = ActionProposalStatus.PROPOSED
    audit: dict = field(default_factory=dict)  # non-sensitive metadata only

    def is_expired(self, now: float = None) -> bool:
        now = now or time.time()
        return (now - self.created_at) > self.expiration

    def safe_audit(self) -> dict:
        a = dict(self.audit)
        # Never include payload; include only length/redacted
        if self.text_to_type is not None:
            a["text_length"] = len(self.text_to_type)
            # Redacted representation for diagnostics; never full payload
            a["text_redacted"] = self.text_to_type[:2] + "***" if len(self.text_to_type) > 2 else "***"
            # Secure hash (not reversible) for correlation, not secret exposure
            a["text_sha256"] = hashlib.sha256(self.text_to_type.encode()).hexdigest()[:16]
        a["proposal_id"] = self.proposal_id
        a["action_type"] = self.action_type.value
        a["risk_level"] = self.risk_level.value
        a["grounding_status"] = (self.grounding_result.status.value
                                 if self.grounding_result else "NONE")
        a["permission_decision"] = a.get("permission_decision", "NONE")
        a["execution_result"] = a.get("execution_result", "NONE")
        a["timestamp"] = time.time()
        return a


def create_action_proposal(
    proposal_id: str,
    action_type: ActionType,
    target_reference: Optional[object] = None,
    grounding_result: Optional[TargetCorrelationResult] = None,
    text_to_type: Optional[str] = None,
    application_name: Optional[str] = None,
    risk_level: Optional[ActionRisk] = None,
    reason: str = "",
) -> ActionProposal:
    # Create a proposal for any action type. Vision actions (CLICK, TYPE_TEXT) require
    # VERIFIED grounding_result to be considered valid. Non-vision actions do not require grounding_result.

    # Risk classification is now the responsibility of the caller (planner) and should be
    # passed in via the `risk_level` parameter. If not provided, the ActionProposal
    # will use its default risk_level (MODERATE).

    # For OPEN_APPLICATION: reject arbitrary command strings embedded in application_name
    if action_type == ActionType.OPEN_APPLICATION:
        if application_name is None or not isinstance(application_name, str):
            # Must specify a safe application target; arbitrary strings rejected
            # Set to proposed but will be blocked at validation unless resolved safely
            pass  # We'll allow creation but block execution if not safe
        # Prevent shell command embedding: reject strings that contain command separators or shell markers
        forbidden_patterns = ["cmd.exe", "powershell", "subprocess", "os.system", "/c ", "&&", "||", ";", "|", "`", "$(", "${"]
        if application_name and any(p.lower() in application_name.lower() for p in forbidden_patterns):
            # Block arbitrary command strings embedded in the target
            p = ActionProposal(
                proposal_id=proposal_id,
                action_type=action_type,
                target_reference=target_reference,
                target_name=application_name,
                risk_level=ActionRisk.BLOCKED if risk_level is None else risk_level,
                source="brain/planner",
                reason="BLOCKED: arbitrary command string detected in application target",
                status=ActionProposalStatus.BLOCKED,
            )
            return p
    status = ActionProposalStatus.PROPOSED
    # Use the provided reason, but may override for invalid vision actions
    final_reason = reason

    if action_type in VISION_ACTION_TYPES:
        if grounding_result is None or grounding_result.status != GroundingStatus.VERIFIED:
            status = ActionProposalStatus.INVALID
            final_reason = "REJECTED: target not VERIFIED"

    # Expiration set (section 10)
    p = ActionProposal(
        proposal_id=proposal_id,
        action_type=action_type,
        target_reference=target_reference,
        target_name=getattr(target_reference, "name", "") if target_reference else (application_name or ""),
        target_window=getattr(target_reference, "window_title", None) if target_reference else None,
        target_process=getattr(target_reference, "process_name", None) if target_reference else None,
        grounding_result=grounding_result,
        text_to_type=text_to_type,
        application_name=application_name,
        risk_level=risk_level if risk_level is not None else ActionRisk.MODERATE,
        source="brain/planner",
        reason=final_reason,
        status=status,
    )
    # Set audit and confidence from grounding_result if available and verified (for vision actions)
    if grounding_result and action_type in VISION_ACTION_TYPES and grounding_result.status == GroundingStatus.VERIFIED:
        p.target_name = grounding_result.candidate.semantic_name
        p.target_window = grounding_result.candidate.window_title
        p.target_process = grounding_result.candidate.process_id
        p.target_point = grounding_result.candidate.target_point
        p.confidence = grounding_result.score
        p.audit["created_from"] = "brain/planner + verified_target"
    else:
        p.audit["created_from"] = "brain/planner"
    return p


def evaluate_proposal_with_permission(
    proposal: ActionProposal,
    permission_gate: PermissionGate,
    tool_name: str = "vision_action_proposal",
    description: str = "",
) -> ActionProposalStatus:
    # Section 8 — integrate with existing PermissionGate; do NOT replace/duplicate
    if proposal.is_expired():
        proposal.audit["permission_decision"] = "BLOCKED (expired)"
        proposal.status = ActionProposalStatus.EXPIRED
        return ActionProposalStatus.EXPIRED

    if proposal.status == ActionProposalStatus.INVALID:
        proposal.audit["permission_decision"] = "BLOCKED (invalid proposal)"
        return ActionProposalStatus.INVALID

    # Only APPROVED + VALIDATED reaches controller (section 13 / 17)
    # Map proposal risk to permission level
    if proposal.risk_level == ActionRisk.BLOCKED or proposal.action_type not in (ActionType.CLICK, ActionType.TYPE_TEXT, ActionType.OPEN_APPLICATION):
        proposal.audit["permission_decision"] = "BLOCKED (risk/action not allowed)"
        proposal.status = ActionProposalStatus.BLOCKED
        return proposal.status

    level = PermissionLevel.MODERATE
    if proposal.risk_level == ActionRisk.HIGH:
        level = PermissionLevel.SENSITIVE
    elif proposal.risk_level == ActionRisk.LOW:
        level = PermissionLevel.SAFE

    desc = description or f"{proposal.action_type.value} on '{proposal.target_name}' (proposal {proposal.proposal_id})"
    decision: PermissionDecision = permission_gate.check(tool_name, level, desc)

    proposal.audit["permission_decision"] = ("APPROVED" if decision.allowed else f"DENIED/REQUIRES_CONFIRMATION: {decision.reason}")

    if decision.allowed:
        proposal.status = ActionProposalStatus.APPROVED
    elif decision.reason.startswith("needs") or (permission_gate.confirm_callback is not None and not decision.allowed):
        proposal.status = ActionProposalStatus.REQUIRES_CONFIRMATION
    elif "dry-run" in str(decision.reason):
        proposal.status = ActionProposalStatus.BLOCKED
    else:
        proposal.status = ActionProposalStatus.DENIED

    # Section 9 — approval bound to this proposal ID; never reused
    proposal.audit["approval_bound_to_proposal_id"] = proposal.proposal_id
    return proposal.status


def validate_for_execution(proposal: ActionProposal) -> bool:
    # Section 11 — TOCTOU defense: re-observe/re-verify before execution
    # This is the gate; actual re-observation must be done by caller (controller / observer)
    if proposal.is_expired():
        proposal.status = ActionProposalStatus.EXPIRED
        proposal.audit["execution_precheck"] = "FAILED (expired)"
        return False
    # Must be APPROVED
    if proposal.status != ActionProposalStatus.APPROVED:
        proposal.audit["execution_precheck"] = f"FAILED (status={proposal.status.value}, not APPROVED)"
        return False
    # For vision actions (CLICK, TYPE_TEXT), must have verified target
    if proposal.action_type in VISION_ACTION_TYPES:
        if proposal.grounding_result is None or proposal.grounding_result.status != GroundingStatus.VERIFIED:
            proposal.status = ActionProposalStatus.INVALID
            proposal.audit["execution_precheck"] = "FAILED (grounding not VERIFIED)"
            return False
    # Coordinate safety must have passed (reuse existing validator result)
    # We do not re-validate geometry here; caller must use coordinate_validator
    proposal.audit["execution_precheck"] = "PASSED (re-observe/re-verify required by caller before execute)"
    return True
