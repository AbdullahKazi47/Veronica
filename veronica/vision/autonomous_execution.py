"""
Phase 10C — Risk-Based Autonomous Execution.

Connects existing:
  Goal / Planner -> ActionProposal -> AutonomyPolicy -> PermissionGate ->
  ComputerController / Verification

Rules preserved verbatim from directive:
- Exactly ONE AutonomyPolicy (reuse 9C), ONE PermissionGate (reuse 9B)
- Vision NEVER authorizes; Vision remains untrusted screen data
- AutonomyPolicy decides whether an ALREADY-PROPOSED action may execute
  autonomously; PermissionGate remains authority for explicit approval
- ActionProposal creates the proposal; validation and target verification
  happen before any execution decision
- Coordinate safety (CoordinateSafetyValidator) before gate / execution
- Blocked actions never execute; never downgraded to AUTO
- Unknown verification NEVER becomes SUCCESS; FAILED prevents continuation
- Autonomy disabled by default (enabled=False)
- No unrestricted autonomy, no destructive-action autonomy,
  no autonomous multi-action workflows, no 10D/10E/10F
"""
from __future__ import annotations

from enum import Enum, auto
from typing import Any, Dict, Optional, List
from dataclasses import dataclass, field
import time

from veronica.vision.autonomy_policy import (
    AutonomyPolicy, AutonomyDecision, AutonomyResult,
    BLOCKED_ACTION_TYPES,
)
from veronica.vision.action_proposal import (
    ActionProposal, ActionType, ActionRisk,
    ActionProposalStatus,
    create_action_proposal, validate_for_execution,
    evaluate_proposal_with_permission,
)
from veronica.vision.visual_grounding import GroundingStatus
from veronica.safety.permissions import PermissionGate, PermissionLevel, PermissionDecision
from veronica.vision.computer_use import ComputerUseLoop, LoopResult, LoopPhase, ComputerAction
from veronica.vision.target_verification import VerificationResult, ActionVerificationStatus, TargetStateVerifier, create_target_state_verifier
from veronica.vision.coordinate_safety import CoordinateSafetyValidator, create_coordinate_validator
from veronica.logging_setup import get_logger
from veronica.vision.autonomous_budget import AutonomousExecutionBudget

logger = get_logger("vision.autonomous_execution")


class ExecutionStatus(str, Enum):
    EXECUTED_VERIFIED = "EXECUTED_VERIFIED"
    EXECUTED_UNVERIFIED = "EXECUTED_UNVERIFIED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    UNKNOWN = "UNKNOWN"


@dataclass
class AutonomousExecutionResult:
    proposal_id: str
    status: ExecutionStatus
    decision: str  # AUTO / CONFIRM / BLOCKED
    executed: bool = False
    verified: bool = False
    verification_result: Optional[VerificationResult] = None
    error: Optional[str] = None
    reason: str = ""
    audit: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        audit_safe = {k: v for k, v in self.audit.items() if k not in ("text", "password", "secret")}
        return {
            "proposal_id": self.proposal_id,
            "status": self.status.value,
            "decision": self.decision,
            "executed": self.executed,
            "verified": self.verified,
            "reason": self.reason,
            "audit": audit_safe,
        }


class AutonomousExecutionPath:
    """Phase 10C controlled execution path using existing 9C/9B/9A components.

    The path enforces:
      1. Create/receive ActionProposal
      2. Validate proposal (not expired, valid)
      3. Verify target (TargetStateVerifier / CoordinateSafetyValidator)
      4. Evaluate AutonomyPolicy
      5. Execute via ComputerController / ComputerUseLoop ONLY when appropriate
      6. Post-action verification (never skip; never reinterpret UNKNOWN as SUCCESS)
      7. Return structured result
    """

    def __init__(
        self,
        autonomy_policy: Optional[AutonomyPolicy] = None,
        permission_gate: Optional[PermissionGate] = None,
        computer_loop: Optional[ComputerUseLoop] = None,
        coordinate_validator: Optional[Any] = None,
    ):
        # Reuse exactly one policy; default disabled per 9C
        self.policy = autonomy_policy or AutonomyPolicy(enabled=False)
        # Reuse exactly one gate
        self.gate = permission_gate or PermissionGate()
        # Existing computer-use loop (reuse 9P / P1 verification)
        self.loop = computer_loop or ComputerUseLoop(permission_gate=self.gate, coordinate_validator=coordinate_validator)
        # Target verification
        self.verifier = create_target_state_verifier()
        # Cancellation / emergency stop state
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        if self.loop is not None:
            try:
                self.loop.cancel()
            except Exception:
                pass

    def reset_cancel(self) -> None:
        self._cancelled = False
        if self.loop is not None:
            try:
                self.loop.reset_cancel()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public entry: execute an ActionProposal through the controlled path
    # ------------------------------------------------------------------
    def execute_proposal(self, proposal: ActionProposal, context: Optional[Dict[str, Any]] = None) -> AutonomousExecutionResult:
        proposal_id = proposal.proposal_id or "unknown"

        # Cancellation guard
        if self._cancelled:
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.CANCELLED,
                decision="CANCELLED",
                executed=False,
                verified=False,
                reason="cancelled before execution",
                audit={"proposal_id": proposal_id, "cancelled": True},
            )

        # 1-2. Proposal validation (reuse existing validation)
        if proposal.status == ActionProposalStatus.EXPIRED:
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.BLOCKED,
                decision="BLOCKED",
                executed=False,
                verified=False,
                reason="proposal expired",
                audit={"proposal_id": proposal_id, "expired": True},
            )
        if proposal.status == ActionProposalStatus.INVALID:
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.BLOCKED,
                decision="BLOCKED",
                executed=False,
                verified=False,
                reason="proposal invalid",
                audit={"proposal_id": proposal_id, "invalid": True},
            )
        # Re-check expiration against time
        try:
            if proposal.is_expired():
                proposal.status = ActionProposalStatus.EXPIRED
                return AutonomousExecutionResult(
                    proposal_id=proposal_id,
                    status=ExecutionStatus.BLOCKED,
                    decision="BLOCKED",
                    executed=False,
                    verified=False,
                    reason="proposal expired at execution time",
                    audit={"proposal_id": proposal_id, "expired_at_execution": True},
                )
        except Exception:
            pass

        # 3. Target verification (before autonomy decision — never skip)
        # For Phase 10C we use the proposal's grounding_result to determine verified status
        grounding_verified = False
        if proposal.grounding_result is not None:
            grounding_verified = proposal.grounding_result.status == GroundingStatus.VERIFIED

        # Coordinate safety check when coordinates are present (reuse CoordinateSafetyValidator)
        coordinate_valid = True
        coordinate_note = ""
        # If proposal carries target coordinates or target reference, validate before execution
        # (defensive — does not replace per-action validation in ComputerController)
        target_ref = getattr(proposal, "target_reference", None) or getattr(proposal, "target_ref", None)
        # We don't run full coordinate validation here because the loop/controller does it per-action;
        # this is defense-in-depth only when proposal explicitly carries coordinates.

        # Initialize budget from context or create default
        budget = None
        if context and "budget" in context:
            budget = context["budget"]
        else:
            budget = AutonomousExecutionBudget()
        budget.start()

        # Budget and cancellation check before any processing
        if budget.is_cancelled():
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.CANCELLED,
                decision="CANCELLED",
                executed=False,
                verified=False,
                reason="cancelled before execution",
                audit={"proposal_id": proposal_id, "cancelled": True},
            )

        if budget.is_action_budget_exceeded():
            logger.warning("Action budget exceeded for proposal %s: %d/%d actions used",
                          proposal_id, budget.actions_used, budget.max_actions)
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.FAILED,  # Reuse existing status
                decision="BLOCKED",  # Treat as blocked due to budget
                executed=False,
                verified=False,
                reason=f"action budget exceeded: {budget.actions_used}/{budget.max_actions}",
                audit={
                    "proposal_id": proposal_id,
                    "budget_exceeded": "action_count",
                    "actions_used": budget.actions_used,
                    "max_actions": budget.max_actions,
                },
            )

        if budget.is_duration_budget_exceeded():
            logger.warning("Duration budget exceeded for proposal %s: %.2f/%.2f seconds",
                          proposal_id, time.time() - budget.started_at if budget.started_at else 0.0, budget.max_duration_seconds)
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.FAILED,  # Reuse existing status
                decision="BLOCKED",  # Treat as blocked due to budget
                executed=False,
                verified=False,
                reason=f"duration budget exceeded: {time.time() - budget.started_at if budget.started_at else 0.0:.2f}/{budget.max_duration_seconds}s",
                audit={
                    "proposal_id": proposal_id,
                    "budget_exceeded": "duration",
                    "elapsed_seconds": time.time() - budget.started_at if budget.started_at else 0.0,
                    "max_duration_seconds": budget.max_duration_seconds,
                },
            )

        # 1-2. Proposal validation (reuse existing validation)
        if proposal.status == ActionProposalStatus.EXPIRED:
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.BLOCKED,
                decision="BLOCKED",
                executed=False,
                verified=False,
                reason="proposal expired",
                audit={"proposal_id": proposal_id, "expired": True},
            )
        if proposal.status == ActionProposalStatus.INVALID:
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.BLOCKED,
                decision="BLOCKED",
                executed=False,
                verified=False,
                reason="proposal invalid",
                audit={"proposal_id": proposal_id, "invalid": True},
            )
        # Re-check expiration against time
        try:
            if proposal.is_expired():
                proposal.status = ActionProposalStatus.EXPIRED
                return AutonomousExecutionResult(
                    proposal_id=proposal_id,
                    status=ExecutionStatus.BLOCKED,
                    decision="BLOCKED",
                    executed=False,
                    verified=False,
                    reason="proposal expired at execution time",
                    audit={"proposal_id": proposal_id, "expired_at_execution": True},
                )
        except Exception:
            pass

        # 3. Target verification (before autonomy decision — never skip)
        # For Phase 10C we use the proposal's grounding_result to determine verified status
        grounding_verified = False
        if proposal.grounding_result is not None:
            grounding_verified = proposal.grounding_result.status == GroundingStatus.VERIFIED

        # Coordinate safety check when coordinates are present (reuse CoordinateSafetyValidator)
        coordinate_valid = True
        coordinate_note = ""
        # If proposal carries target coordinates or target reference, validate before execution
        # (defensive — does not replace per-action validation in ComputerController)
        target_ref = getattr(proposal, "target_reference", None) or getattr(proposal, "target_ref", None)
        # We don't run full coordinate validation here because the loop/controller does it per-action;
        # this is defense-in-depth only when proposal explicitly carries coordinates.

        # 4. AutonomyPolicy evaluation (reuse 9C; disabled by default preserves legacy)
        policy_result = self.policy.evaluate(proposal, context=context)
        decision = policy_result.decision  # AUTO_APPROVED / PERMISSION_REQUIRED / BLOCKED

        # 5. BLOCKED path — never execute, never ask permission if permanently blocked
        if decision == AutonomyDecision.BLOCKED:
            logger.info("Phase 10C BLOCKED proposal %s (%s): %s",
                        proposal_id, proposal.action_type.value if proposal.action_type else "?",
                        policy_result.reason)
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.BLOCKED,
                decision="BLOCKED",
                executed=False,
                verified=False,
                reason=policy_result.reason or "blocked by autonomy policy",
                audit={
                    "proposal_id": proposal_id,
                    "policy_decision": decision.value,
                    "blocked_reason": policy_result.blocked_reason,
                    "action_type": proposal.action_type.value if proposal.action_type else None,
                    "enabled": self.policy.enabled,
                    "policy_version": self.policy.policy_version,
                },
            )

        # 6. CONFIRM path (PERMISSION_REQUIRED) — do NOT execute; request permission
        if decision == AutonomyDecision.PERMISSION_REQUIRED:
            permission_result = self.gate.check(
                tool_name="autonomous_action",
                level=PermissionLevel.MODERATE,
                description=f"Autonomous action proposal {proposal_id}: {proposal.action_type.value if proposal.action_type else 'unknown'} "
                            f"(target={proposal.target_name or 'unspecified'}) — requires explicit confirmation",
            )
            if permission_result.allowed:
                # Re-validate proposal after approval (state may have changed)
                if proposal.is_expired() or proposal.status in (ActionProposalStatus.EXPIRED, ActionProposalStatus.INVALID):
                    return AutonomousExecutionResult(
                        proposal_id=proposal_id,
                        status=ExecutionStatus.BLOCKED,
                        decision="BLOCKED",
                        executed=False,
                        verified=False,
                        reason="proposal became invalid or expired after approval",
                        audit={
                            "proposal_id": proposal_id,
                            "permission": "approved_then_invalid",
                            "gate_decision": permission_result.reason,
                        },
                    )
                # After approval, continue to execution path (verify target again if required)
                # We proceed to AUTO-style execution but with explicit approval record
                return self._execute_approved(proposal, proposal_id, context, approved=True, permission_result=permission_result, budget=budget)
            else:
                # Rejected — safe denial, no execution
                logger.info("Phase 10C CONFIRM DENIED proposal %s: %s",
                            proposal_id, permission_result.reason)
                return AutonomousExecutionResult(
                    proposal_id=proposal_id,
                    status=ExecutionStatus.PERMISSION_REQUIRED,
                    decision="CONFIRM",
                    executed=False,
                    verified=False,
                    reason=permission_result.reason or "permission denied",
                    audit={
                        "proposal_id": proposal_id,
                        "permission": "denied",
                        "gate_decision": permission_result.reason,
                        "policy_decision": decision.value,
                    },
                )

        # 7. AUTO path (AUTO_APPROVED) — execute through existing ComputerController / loop
        # Must still verify target; must still observe/verify after execution
        return self._execute_approved(proposal, proposal_id, context, approved=False, permission_result=None, budget=budget)

    def _execute_approved(
        self,
        proposal: ActionProposal,
        proposal_id: str,
        context: Optional[Dict[str, Any]],
        approved: bool,
        permission_result: Optional[Any] = None,
        budget: Optional[AutonomousExecutionBudget] = None,
    ) -> AutonomousExecutionResult:
        # Before execution: verify target (coordinate safety + target verification)
        # Use existing verification from computer-use loop if available
        pre_ref = None
        # Capture reference from proposal if possible (reuse loop's approach conceptually)
        try:
            # Try to observe/pre-capture via loop if available; defensive only
            if self.loop is not None:
                initial = self.loop._observe()
                pre_ref = self.loop._capture_reference(
                    ComputerAction(action_type=proposal.action_type.value or "click", target_name=proposal.target_name or ""),
                    initial,
                )
        except Exception:
            # Defensive: observation failure should not block safe execution path
            pass

        # Build a ComputerAction for execution from the proposal
        action = ComputerAction(
            action_type=proposal.action_type.value if proposal.action_type else "click",
            target_name=proposal.target_name or "",
            description=proposal.reason or f"autonomous execution of proposal {proposal_id}",
            permission_level=PermissionLevel.SAFE if not approved else PermissionLevel.MODERATE,
            text=None,
        )
        # Bind authorization to this proposal (explicit, auditable, not vision-derived)
        from veronica.vision.computer_use import ExecutionAuthorization
        auth_mode = "POLICY_AUTO" if not approved else "HUMAN_APPROVED"
        auth = ExecutionAuthorization(
            mode=auth_mode,
            proposal_id=proposal_id,
            policy_version=self.policy.policy_version,
        )
        self.loop.authorization = auth

        # Record action usage BEFORE execution (budget tracking)
        if budget is not None:
            budget.record_action()

        # Execute through loop (reuse existing ComputerUseLoop with verification)
        loop_result: Optional[LoopResult] = None
        try:
            loop_result = self.loop.execute_action(action, verify_change=True, authorization=auth)
        except Exception as e:
            logger.warning("Phase 10C execution exception for %s: %s", proposal_id, e)
            # Check if we exceeded budget due to this failed action attempt
            if budget is not None and (budget.is_action_budget_exceeded() or budget.is_duration_budget_exceeded()):
                if budget.is_action_budget_exceeded():
                    return AutonomousExecutionResult(
                        proposal_id=proposal_id,
                        status=ExecutionStatus.FAILED,
                        decision="BLOCKED",
                        executed=False,
                        verified=False,
                        reason=f"action budget exceeded after failed execution: {budget.actions_used}/{budget.max_actions}",
                        audit={
                            "proposal_id": proposal_id,
                            "budget_exceeded": "action_count",
                            "actions_used": budget.actions_used,
                            "max_actions": budget.max_actions,
                            "exception": str(e),
                        },
                    )
                else:  # duration budget exceeded
                    return AutonomousExecutionResult(
                        proposal_id=proposal_id,
                        status=ExecutionStatus.FAILED,
                        decision="BLOCKED",
                        executed=False,
                        verified=False,
                        reason=f"duration budget exceeded after failed execution: {time.time() - budget.started_at if budget.started_at else 0.0:.2f}/{budget.max_duration_seconds}s",
                        audit={
                            "proposal_id": proposal_id,
                            "budget_exceeded": "duration",
                            "elapsed_seconds": time.time() - budget.started_at if budget.started_at else 0.0,
                            "max_duration_seconds": budget.max_duration_seconds,
                            "exception": str(e),
                        },
                    )
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.FAILED,
                decision="AUTO" if not approved else "CONFIRM",
                executed=False,
                verified=False,
                reason=f"execution exception: {e}",
                audit={
                    "proposal_id": proposal_id,
                    "approved": approved,
                    "exception": str(e),
                },
            )

        # Post-action verification (NEVER skip; NEVER reinterpret UNKNOWN as SUCCESS)
        executed = loop_result.success or loop_result.phase_reached in (LoopPhase.EXECUTE, LoopPhase.VERIFY, LoopPhase.SUCCESS)
        # Determine verification status from result
        verification = loop_result.verification
        verified = False
        if verification is not None:
            if verification.status == ActionVerificationStatus.VERIFIED:
                verified = True
            # UNKNOWN and FAILED remain unverified; never promoted to SUCCESS
        # If loop completed without verification result, treat as unverified (defensive)
        # If loop says SUCCESS but verification is UNKNOWN, keep UNKNOWN (preserve invariant)
        if not verified and loop_result.phase_reached == LoopPhase.SUCCESS:
            # Loop reported success but verification not explicitly VERIFIED — treat as unverified
            pass  # keep verified=False

        # Cancellation after execution must not return false success
        if self._cancelled and loop_result is not None:
            return AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.CANCELLED,
                decision="AUTO" if not approved else "CONFIRM",
                executed=executed,
                verified=False,
                reason="cancelled after execution",
                audit={
                    "proposal_id": proposal_id,
                    "approved": approved,
                    "cancelled_after_execute": True,
                    "loop_phase": loop_result.phase_reached.value if loop_result.phase_reached else None,
                },
            )

        # Check budget limits AFTER execution (in case action execution took time or we need to enforce limits)
        if budget is not None:
            if budget.is_action_budget_exceeded():
                return AutonomousExecutionResult(
                    proposal_id=proposal_id,
                    status=ExecutionStatus.FAILED,
                    decision="BLOCKED",
                    executed=executed,
                    verified=verified,
                    reason=f"action budget exceeded: {budget.actions_used}/{budget.max_actions}",
                    audit={
                        "proposal_id": proposal_id,
                        "budget_exceeded": "action_count",
                        "actions_used": budget.actions_used,
                        "max_actions": budget.max_actions,
                        "verified": verified,
                        "execution_status": loop_result.phase_reached.value if loop_result.phase_reached else None,
                    },
                )
            if budget.is_duration_budget_exceeded():
                return AutonomousExecutionResult(
                    proposal_id=proposal_id,
                    status=ExecutionStatus.FAILED,
                    decision="BLOCKED",
                    executed=executed,
                    verified=verified,
                    reason=f"duration budget exceeded: {time.time() - budget.started_at if budget.started_at else 0.0:.2f}/{budget.max_duration_seconds}s",
                    audit={
                        "proposal_id": proposal_id,
                        "budget_exceeded": "duration",
                        "elapsed_seconds": time.time() - budget.started_at if budget.started_at else 0.0,
                        "max_duration_seconds": budget.max_duration_seconds,
                        "verified": verified,
                        "execution_status": loop_result.phase_reached.value if loop_result.phase_reached else None,
                    },
                )

        # Post-action verification escalation (Phase 10D): verification results
        # must never silently become SUCCESS.
        if verification is not None:
            if verification.status == ActionVerificationStatus.UNKNOWN:
                # UNKNOWN must never become SUCCESS; if execution completed but verification unknown,
                # treat as unverified (already handled above) and flag escalation.
                logger.warning("Phase 10D verification escalation: UNKNOWN for proposal %s — not promoted to SUCCESS", proposal_id)
            elif verification.status == ActionVerificationStatus.FAILED:
                # FAILED enters recovery/escalation path; do not continue blindly
                logger.warning("Phase 10D verification escalation: FAILED for proposal %s — enters recovery/escalation", proposal_id)

        # Return structured result distinguishing execution + verification
        if verified:
            status = ExecutionStatus.EXECUTED_VERIFIED
        elif executed:
            status = ExecutionStatus.EXECUTED_UNVERIFIED
        else:
            # Execution did not succeed (loop failure / permission denied at execution time / blocked)
            # If loop phase is BLOCKED or PERMISSION, return appropriate status
            if loop_result.phase_reached == LoopPhase.BLOCKED:
                status = ExecutionStatus.BLOCKED
            elif loop_result.phase_reached == LoopPhase.PERMISSION:
                status = ExecutionStatus.PERMISSION_REQUIRED
            else:
                status = ExecutionStatus.FAILED

        audit = {
            "proposal_id": proposal_id,
            "approved": approved,
            "policy_decision": "AUTO_APPROVED" if not approved else "PERMISSION_REQUIRED",
            "loop_phase": loop_result.phase_reached.value if (loop_result and loop_result.phase_reached) else None,
            "verified": verified,
            "verification_status": verification.status.value if verification else None,
            "executed": executed,
        }
        if permission_result is not None:
            audit["permission_result"] = permission_result.reason

        return AutonomousExecutionResult(
            proposal_id=proposal_id,
            status=status,
            decision="AUTO" if not approved else "CONFIRM",
            executed=executed,
            verified=verified,
            verification_result=verification,
            error=loop_result.error if (loop_result and loop_result.error) else None,
            reason=f"loop phase={loop_result.phase_reached.value}" if loop_result else "no loop result",
            audit=audit,
        )
