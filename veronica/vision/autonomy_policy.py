"""
Phase 9C — Risk-Based Autonomy Policy Layer.

Independent of vision, LLM, model router, controller. Consumes ActionProposal
and produces a structured AutonomyDecision (not a bare boolean).

STARK boundary:
- LOW-RISK / ROUTINE -> AUTO_APPROVED (autonomous execution permitted)
- MODERATE / HIGH-RISK -> PERMISSION_REQUIRED (existing PermissionGate must approve)
- BLOCKED / destructive / arbitrary_shell / credential / security -> BLOCKED

Vision never authorizes. LLM never authorizes. Screen text never authorizes.
PermissionGate remains sole permission authority.
Autonomy disabled by default; when disabled, existing behavior preserved (no autonomous actions).
"""
from __future__ import annotations

import time
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any

from veronica.vision.action_proposal import (
    ActionProposal, ActionType, ActionRisk, ActionProposalStatus,
    create_action_proposal, evaluate_proposal_with_permission, validate_for_execution,
)
from veronica.safety.permissions import PermissionGate, PermissionLevel, PermissionDecision
from veronica.vision.visual_grounding import GroundingStatus, TargetCorrelationResult
from veronica.logging_setup import get_logger

logger = get_logger("vision.autonomy_policy")


class AutonomyDecision(str, Enum):
    AUTO_APPROVED = "AUTO_APPROVED"          # autonomous allowed (low-risk, verified)
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"  # must pass existing PermissionGate
    BLOCKED = "BLOCKED"                        # never autonomous; may still require gate or be denied


@dataclass
class AutonomyResult:
    decision: AutonomyDecision
    reason: str = ""
    risk: str = ""
    action_type: str = ""
    policy_rule: str = ""
    requires_confirmation: bool = False
    timestamp: float = field(default_factory=time.time)
    policy_version: str = "9C-v1"
    blocked_reason: Optional[str] = None
    proposal_id: Optional[str] = None
    audit_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "risk": self.risk,
            "action_type": self.action_type,
            "policy_rule": self.policy_rule,
            "requires_confirmation": self.requires_confirmation,
            "timestamp": self.timestamp,
            "policy_version": self.policy_version,
            "blocked_reason": self.blocked_reason,
            "proposal_id": self.proposal_id,
            "audit_metadata": dict(self.audit_metadata),
        }


# Hard BLOCKED categories — never autonomous (security invariants)
BLOCKED_ACTION_TYPES = {"SHELL_EXECUTION", "ARBITRARY_SUBPROCESS", "CREDENTIAL_ACCESS",
                        "SECURITY_DISABLE", "DESTRUCTIVE_SYSTEM", "FINANCIAL_TRANSACTION"}

# HIGH-RISK categories that always require permission (never autonomous by default)
HIGH_RISK_TYPES = {"DELETE_FILE", "DELETE_FOLDER", "UNINSTALL", "INSTALL",
                   "WRITE_SYSTEM", "MODIFY_SYSTEM_SETTINGS", "CHANGE_PASSWORD",
                   "EXECUTE_ARBITRARY_SHELL", "UPLOAD_SENSITIVE", "SEND_EXTERNAL"}

# LOW-RISK / ROUTINE categories eligible for AUTO_APPROVED (only when proposal verified)
LOW_RISK_TYPES = {"OPEN_APP", "CLOSE_APP", "NAVIGATE", "CLICK",
                  "TYPE_TEXT", "READ_SCREEN", "INSPECT_UI",
                  "SCROLL", "SEARCH_ORDINARY", "SELECT_ORDINARY",
                  "NAVIGATE_FOLDER", "CREATE_DRAFT", "DOWNLOAD_ORDINARY"}


class AutonomyPolicy:
    """
    Risk-based autonomy policy independent of vision/LLM/controller.
    """
    def __init__(
        self,
        enabled: bool = False,
        max_risk: str = "low",
        max_autonomous_actions: int = 20,
        max_autonomous_seconds: int = 120,
        max_consecutive_failures: int = 3,
        max_replans: int = 5,
        blocked_actions: Optional[List[str]] = None,
        allowed_apps: Optional[List[str]] = None,
        blocked_apps: Optional[List[str]] = None,
    ):
        self.enabled = enabled
        self.max_risk = max_risk.lower()
        # Budget limits (reuse closed-loop limits conceptually; no duplication of counters)
        self.max_autonomous_actions = max_autonomous_actions
        self.max_autonomous_seconds = max_autonomous_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.max_replans = max_replans
        # Configurable allow/deny lists
        self.blocked_actions = set(blocked_actions or [])
        self.allowed_apps = set(allowed_apps or [])
        self.blocked_apps = set(blocked_apps or [])
        self.policy_version = "9C-v1"

        # Ensure destructive categories always blocked
        self.blocked_actions.update(HIGH_RISK_TYPES)
        # Arbitrary shell execution always blocked regardless of risk label
        self.blocked_actions.add("ARBITRARY_SHELL")
        # Security / destructive system always blocked
        self.blocked_actions.update({"DESTRUCTIVE_SYSTEM", "SECURITY_DISABLE"})

    def evaluate(self, proposal: ActionProposal, context: Optional[Dict[str, Any]] = None) -> AutonomyResult:
        if not self.enabled:
            # Disabled by default: preserve existing behavior — no autonomous execution.
            return self._permission_result(proposal, "autonomy disabled (default)")

        # 1. Hard BLOCK categories always BLOCKED
        if proposal.action_type.value in BLOCKED_ACTION_TYPES or proposal.action_type.name in BLOCKED_ACTION_TYPES:
            return self._blocked(proposal, "blocked action category")

        # 2. Explicit blocked actions (configurable deny list)
        if proposal.action_type.name in self.blocked_actions or proposal.action_type.value in self.blocked_actions:
            return self._blocked(proposal, "explicit block list")

        # 3. Risk escalation: only LOW-RISK eligible for AUTO when verified
        if proposal.risk_level == ActionRisk.HIGH or proposal.risk_level == ActionRisk.BLOCKED:
            # HIGH-RISK always requires permission; BLOCKED never proceeds
            return self._permission_result(proposal, "high/blocked risk requires permission")

        # 4. Only verified targets proceed; others already INVALID (handled by proposal creation)
        if proposal.grounding_result is None or proposal.grounding_result.status != GroundingStatus.VERIFIED:
            # This should not reach policy if proposal was properly created/rejected,
            # but defense in depth.
            return self._blocked(proposal, "target not verified")

        # 5. Low-risk autonomous eligibility (verified + LOW risk + action in LOW-RISK_TYPES)
        if proposal.risk_level == ActionRisk.LOW and proposal.action_type.value in LOW_RISK_TYPES:
            # Verify budget limits if context provided (optional, not duplicated from loop guard)
            if context and "autonomous_actions_so_far" in context:
                if context.get("autonomous_actions_so_far", 0) >= self.max_autonomous_actions:
                    return self._permission_result(proposal, "autonomous action budget exceeded")
            if context and "autonomous_start_time" in context:
                elapsed = time.time() - context.get("autonomous_start_time", time.time())
                if elapsed > self.max_autonomous_seconds:
                    return self._permission_result(proposal, "autonomous time budget exceeded")
            # Auto-approved for verified low-risk
            return AutonomyResult(
                decision=AutonomyDecision.AUTO_APPROVED,
                reason=f"verified low-risk {proposal.action_type.value} autonomous",
                risk=proposal.risk_level.value,
                action_type=proposal.action_type.value,
                policy_rule="verified_low_risk_autonomous",
                requires_confirmation=False,
                proposal_id=proposal.proposal_id,
                audit_metadata={
                    "enabled": self.enabled,
                    "policy_version": self.policy_version,
                    "budget_actions_remaining": max(0, self.max_autonomous_actions - context.get("autonomous_actions_so_far", 0)) if context else self.max_autonomous_actions,
                },
            )

        # 6. MODERATE risk -> permission required (default safe behavior)
        if proposal.risk_level == ActionRisk.MODERATE:
            # Some moderate actions may be allowed if in LOW-RISK_TYPES and verified; handled above.
            # Otherwise, require permission.
            return self._permission_result(proposal, "moderate risk requires permission")

        # 7. Default fallback — safe denial; never silently allow unknowns
        return self._permission_result(proposal, "unknown risk profile; permission required by default")

    def _blocked(self, proposal: ActionProposal, reason: str) -> AutonomyResult:
        logger.info("Autonomy BLOCKED proposal %s: %s (action=%s risk=%s)",
                    proposal.proposal_id, reason, proposal.action_type.value, proposal.risk_level.value)
        return AutonomyResult(
            decision=AutonomyDecision.BLOCKED,
            reason=reason,
            risk=proposal.risk_level.value,
            action_type=proposal.action_type.value,
            policy_rule="blocked_by_policy",
            requires_confirmation=False,  # blocked means no execution; no confirmation needed
            proposal_id=proposal.proposal_id,
            blocked_reason=reason,
            audit_metadata={
                "enabled": self.enabled,
                "policy_version": self.policy_version,
            },
        )

    def _permission_result(self, proposal: ActionProposal, reason: str) -> AutonomyResult:
        logger.info("Autonomy PERMISSION_REQUIRED proposal %s: %s (action=%s risk=%s)",
                    proposal.proposal_id, reason, proposal.action_type.value, proposal.risk_level.value)
        return AutonomyResult(
            decision=AutonomyDecision.PERMISSION_REQUIRED,
            reason=reason,
            risk=proposal.risk_level.value,
            action_type=proposal.action_type.value,
            policy_rule="permission_required",
            requires_confirmation=True,
            proposal_id=proposal.proposal_id,
            audit_metadata={
                "enabled": self.enabled,
                "policy_version": self.policy_version,
            },
        )

    def is_action_allowed_autonomously(self, proposal: ActionProposal, context: Optional[Dict[str, Any]] = None) -> bool:
        result = self.evaluate(proposal, context)
        return result.decision == AutonomyDecision.AUTO_APPROVED

    def is_action_blocked(self, proposal: ActionProposal, context: Optional[Dict[str, Any]] = None) -> bool:
        result = self.evaluate(proposal, context)
        return result.decision == AutonomyDecision.BLOCKED


# Convenience factory for reuse
def create_autonomy_policy(**kwargs) -> AutonomyPolicy:
    return AutonomyPolicy(**kwargs)
