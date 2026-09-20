"""
Phase 10E — Bounded Multi-Step Autonomous Workflows.

Reuses 10C (AutonomousExecutionPath / ExecutionAuthorization) and 10D
(AutonomousExecutionBudget / escalation) without redesign.
Per-step proposal + independent authorization; shared session budget;
verification required before advancement; recovery via existing path.
No destructive autonomy; autonomy disabled by default preserved.
No 10F components.
"""
from __future__ import annotations

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from veronica.vision.autonomous_execution import (
    AutonomousExecutionPath, AutonomousExecutionResult, ExecutionStatus,
)
from veronica.vision.autonomous_budget import AutonomousExecutionBudget
from veronica.vision.autonomy_policy import AutonomyPolicy
from veronica.vision.action_proposal import ActionProposal
from veronica.safety.permissions import PermissionGate


class WorkflowStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    ESCALATED = "ESCALATED"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


@dataclass
class WorkflowStep:
    step_id: str
    description: str
    proposal: Optional[ActionProposal] = None
    authorization: Optional[Any] = None  # proposal-bound ExecutionAuthorization
    result: Optional[AutonomousExecutionResult] = None
    verification_status: Optional[str] = None  # VERIFIED / UNKNOWN / FAILED
    status: StepStatus = StepStatus.PENDING


@dataclass
class AutonomousWorkflow:
    workflow_id: str
    goal: str
    steps: List[WorkflowStep]
    current_step_index: int = 0
    completed_steps: List[str] = field(default_factory=list)
    current_result: Optional[AutonomousExecutionResult] = None
    budget: Optional[AutonomousExecutionBudget] = None
    cancellation_state: bool = False
    escalation_state: Optional[str] = None
    status: WorkflowStatus = WorkflowStatus.PENDING

    def __post_init__(self):
        if self.budget is None:
            self.budget = AutonomousExecutionBudget()
        if not self.steps:
            raise ValueError("Workflow must have at least one step")


def execute_workflow(
    workflow: AutonomousWorkflow,
    *,
    cancellation_token: Optional[Any] = None,
    path: Optional[AutonomousExecutionPath] = None,
    budget: Optional[AutonomousExecutionBudget] = None,
) -> AutonomousWorkflow:
    if budget is not None:
        workflow.budget = budget
    if workflow.budget is not None:
        workflow.budget.start()
    if cancellation_token is not None and getattr(cancellation_token, "cancelled", False):
        workflow.cancellation_state = True
        workflow.status = WorkflowStatus.CANCELLED
        return workflow
    path = path or AutonomousExecutionPath(autonomy_policy=AutonomyPolicy(enabled=False))
    workflow.status = WorkflowStatus.RUNNING
    for idx in range(workflow.current_step_index, len(workflow.steps)):
        if workflow.cancellation_state or (cancellation_token is not None and getattr(cancellation_token, "cancelled", False)):
            workflow.cancellation_state = True
            workflow.status = WorkflowStatus.CANCELLED
            for s in workflow.steps[idx:]:
                s.status = StepStatus.CANCELLED
            return workflow
        step = workflow.steps[idx]
        run_step(workflow, step, cancellation_token=cancellation_token, path=path)
        workflow.current_result = step.result
        if step.result is not None:
            # Advance only if verified; unknown stops, failed invokes recovery
            if step.status == StepStatus.VERIFIED:
                if step.step_id not in workflow.completed_steps:
                    workflow.completed_steps.append(step.step_id)
                workflow.current_step_index = idx + 1
                if workflow.current_step_index >= len(workflow.steps):
                    workflow.status = WorkflowStatus.COMPLETED
                else:
                    workflow.status = WorkflowStatus.RUNNING
            elif step.status == StepStatus.UNKNOWN:
                workflow.status = WorkflowStatus.ESCALATED
                workflow.escalation_state = "unknown_verification"
                # Stop workflow; never advance
                return workflow
            elif step.status == StepStatus.BLOCKED:
                workflow.status = WorkflowStatus.BLOCKED
                return workflow
            elif step.status == StepStatus.FAILED:
                # Recovery / replan: new proposal + authorization via existing recovery path
                recovery = handle_recovery(workflow, step, cancellation_token=cancellation_token, path=path)
                # After recovery attempt, if still not verified, treat as escalated/partial
                if recovery.status in (WorkflowStatus.COMPLETED, WorkflowStatus.RUNNING):
                    # Recovery produced verified step and may allow continuation; loop will pick up at idx
                    pass
                else:
                    if workflow.status not in (WorkflowStatus.COMPLETED, WorkflowStatus.RUNNING):
                        workflow.status = WorkflowStatus.ESCALATED if workflow.escalation_state else WorkflowStatus.FAILED
                # Do not auto-advance; next loop iteration handles it if recovered
                # If the step was re-run, current_result reflects new result
            elif step.status == StepStatus.WAITING_CONFIRMATION:
                workflow.status = WorkflowStatus.WAITING_CONFIRMATION
                return workflow
            elif step.status == StepStatus.CANCELLED:
                workflow.status = WorkflowStatus.CANCELLED
                return workflow
        else:
            # Should not occur; defensive
            workflow.status = WorkflowStatus.FAILED
            return workflow
    # After loop, verify all completed steps verified
    all_verified = all(
        s.status == StepStatus.VERIFIED for s in workflow.steps[:workflow.current_step_index]
    )
    if all_verified and workflow.current_step_index >= len(workflow.steps):
        workflow.status = WorkflowStatus.COMPLETED
    elif all_verified:
        workflow.status = WorkflowStatus.RUNNING
    else:
        if workflow.status not in (WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED, WorkflowStatus.BLOCKED, WorkflowStatus.WAITING_CONFIRMATION):
            workflow.status = WorkflowStatus.PARTIAL
    return workflow


def run_step(
    workflow: AutonomousWorkflow,
    step: WorkflowStep,
    *,
    cancellation_token: Optional[Any] = None,
    path: Optional[AutonomousExecutionPath] = None,
) -> WorkflowStep:
    path = path or AutonomousExecutionPath(autonomy_policy=AutonomyPolicy(enabled=False))
    # Cancellation check before any execution attempt
    if workflow.cancellation_state or (cancellation_token is not None and getattr(cancellation_token, "cancelled", False)):
        step.status = StepStatus.CANCELLED
        step.result = AutonomousExecutionResult(
            proposal_id=step.step_id,
            status=ExecutionStatus.CANCELLED,
            decision="CANCELLED",
            executed=False,
            verified=False,
            reason="cancelled before step execution",
        )
        workflow.cancellation_state = True
        return step
    # Budget check before execution attempt (reused across entire workflow)
    budget = workflow.budget or AutonomousExecutionBudget()
    budget.start()
    if budget.is_cancelled():
        step.status = StepStatus.CANCELLED
        step.result = AutonomousExecutionResult(
            proposal_id=step.step_id,
            status=ExecutionStatus.CANCELLED,
            decision="CANCELLED",
            executed=False,
            verified=False,
            reason="budget cancelled",
        )
        workflow.cancellation_state = True
        return step
    if budget.is_action_budget_exceeded():
        step.status = StepStatus.BLOCKED
        step.result = AutonomousExecutionResult(
            proposal_id=step.step_id,
            status=ExecutionStatus.FAILED,
            decision="BLOCKED",
            executed=False,
            verified=False,
            reason=f"action budget exceeded: {budget.actions_used}/{budget.max_actions}",
            audit={"budget_exceeded": "action_count", "budget": budget.budget_state()},
        )
        workflow.status = WorkflowStatus.BLOCKED
        workflow.escalation_state = "budget_exceeded"
        return step
    if budget.is_duration_budget_exceeded():
        step.status = StepStatus.BLOCKED
        step.result = AutonomousExecutionResult(
            proposal_id=step.step_id,
            status=ExecutionStatus.FAILED,
            decision="BLOCKED",
            executed=False,
            verified=False,
            reason=f"duration budget exceeded",
            audit={"budget_exceeded": "duration", "budget": budget.budget_state()},
        )
        workflow.status = WorkflowStatus.BLOCKED
        workflow.escalation_state = "budget_exceeded"
        return step
    # Proposal validation (reuse existing; must have own proposal per step)
    if step.proposal is None:
        step.status = StepStatus.BLOCKED
        step.result = AutonomousExecutionResult(
            proposal_id=step.step_id,
            status=ExecutionStatus.BLOCKED,
            decision="BLOCKED",
            executed=False,
            verified=False,
            reason="step missing proposal",
        )
        return step
    # Fresh proposal-bound authorization per step (never reused across steps or workflows)
    from veronica.vision.computer_use import ExecutionAuthorization
    proposal_id = step.proposal.proposal_id or step.step_id
    auth = ExecutionAuthorization(
        mode="POLICY_AUTO",
        proposal_id=proposal_id,
    )
    step.authorization = auth
    # Policy evaluation per step (reuse 9C; disabled by default preserved)
    policy_result = path.policy.evaluate(step.proposal, context={"budget": budget})
    # BLOCKED: stop workflow permanently
    if policy_result.decision.value == "BLOCKED":
        step.status = StepStatus.BLOCKED
        step.result = AutonomousExecutionResult(
            proposal_id=proposal_id,
            status=ExecutionStatus.BLOCKED,
            decision="BLOCKED",
            executed=False,
            verified=False,
            reason=policy_result.reason or "blocked by autonomy policy",
            audit={"policy_decision": policy_result.decision.value, "blocked_reason": getattr(policy_result, "blocked_reason", None)},
        )
        workflow.status = WorkflowStatus.BLOCKED
        return step
    # CONFIRM: pause at step; use existing PermissionGate
    if policy_result.decision.value == "PERMISSION_REQUIRED":
        # Existing gate check (reuse 9B)
        gate_result = path.gate.check(
            tool_name="autonomous_action",
            level=__import__("veronica.safety.permissions", fromlist=["PermissionLevel"]).PermissionLevel.MODERATE,
            description=f"Workflow step {step.step_id}: {step.proposal.action_type.value if step.proposal.action_type else 'unknown'} — requires confirmation",
        )
        if gate_result.allowed:
            # Approved: proceed to execution with HUMAN_APPROVED authorization bound to this proposal
            auth = ExecutionAuthorization(
                mode="HUMAN_APPROVED",
                proposal_id=proposal_id,
                policy_version=path.policy.policy_version,
            )
            step.authorization = auth
            # Ensure loop authorization reflects this proposal-bound authorization
            path.loop.authorization = auth
            res = path.execute_proposal(step.proposal, context={"budget": budget})
            step.result = res
            if res.status == ExecutionStatus.EXECUTED_VERIFIED and res.verified:
                step.status = StepStatus.VERIFIED
                step.verification_status = "VERIFIED"
            elif res.status == ExecutionStatus.EXECUTED_UNVERIFIED:
                step.status = StepStatus.UNKNOWN
                step.verification_status = "UNKNOWN"
            elif res.status == ExecutionStatus.FAILED:
                step.status = StepStatus.FAILED
                step.verification_status = "FAILED"
            elif res.status == ExecutionStatus.CANCELLED:
                step.status = StepStatus.CANCELLED
            elif res.status == ExecutionStatus.BLOCKED:
                step.status = StepStatus.BLOCKED
            else:
                step.status = StepStatus.UNKNOWN
                step.verification_status = "UNKNOWN"
            # If CONFIRM approved and executed, treat as running until verified
            if step.status == StepStatus.VERIFIED:
                workflow.status = WorkflowStatus.RUNNING if workflow.current_step_index < len(workflow.steps) - 1 else WorkflowStatus.COMPLETED
            else:
                if step.status == StepStatus.UNKNOWN:
                    workflow.status = WorkflowStatus.ESCALATED
                    workflow.escalation_state = "unknown_verification"
                elif step.status == StepStatus.BLOCKED:
                    workflow.status = WorkflowStatus.BLOCKED
                else:
                    workflow.status = WorkflowStatus.ESCALATED if step.status != StepStatus.VERIFIED else WorkflowStatus.RUNNING
            return step
        else:
            # Rejected — safe denial; step stops; workflow stops (never downgraded)
            step.status = StepStatus.BLOCKED
            step.result = AutonomousExecutionResult(
                proposal_id=proposal_id,
                status=ExecutionStatus.PERMISSION_REQUIRED,
                decision="CONFIRM",
                executed=False,
                verified=False,
                reason=gate_result.reason or "permission denied",
                audit={"permission": "denied", "gate_decision": gate_result.reason},
            )
            workflow.status = WorkflowStatus.BLOCKED
            return step
    # AUTO_APPROVED: execute directly with proposal-bound authorization
    path.loop.authorization = auth
    res = path.execute_proposal(step.proposal, context={"budget": budget})
    step.result = res
    # Determine step status from result + verification preservation
    if res.status == ExecutionStatus.EXECUTED_VERIFIED and res.verified:
        step.status = StepStatus.VERIFIED
        step.verification_status = "VERIFIED"
    elif res.status == ExecutionStatus.EXECUTED_UNVERIFIED:
        step.status = StepStatus.UNKNOWN
        step.verification_status = "UNKNOWN"
    elif res.status == ExecutionStatus.FAILED:
        step.status = StepStatus.FAILED
        step.verification_status = "FAILED"
    elif res.status == ExecutionStatus.BLOCKED:
        step.status = StepStatus.BLOCKED
    elif res.status == ExecutionStatus.CANCELLED:
        step.status = StepStatus.CANCELLED
    elif res.status == ExecutionStatus.PERMISSION_REQUIRED:
        step.status = StepStatus.WAITING_CONFIRMATION
    else:
        # Defensive: preserve verification if available
        if res.verification_result is not None:
            if res.verification_result.status.value == "VERIFIED":
                step.status = StepStatus.VERIFIED
                step.verification_status = "VERIFIED"
            elif res.verification_result.status.value == "UNKNOWN":
                step.status = StepStatus.UNKNOWN
                step.verification_status = "UNKNOWN"
            elif res.verification_result.status.value == "FAILED":
                step.status = StepStatus.FAILED
                step.verification_status = "FAILED"
        else:
            step.status = StepStatus.UNKNOWN
            step.verification_status = "UNKNOWN"
    # Unknown never advances; stop/escalate
    if step.status == StepStatus.UNKNOWN:
        workflow.status = WorkflowStatus.ESCALATED
        workflow.escalation_state = "unknown_verification"
    elif step.status == StepStatus.BLOCKED:
        workflow.status = WorkflowStatus.BLOCKED
    elif step.status == StepStatus.CANCELLED:
        workflow.status = WorkflowStatus.CANCELLED
    return step


def handle_recovery(
    workflow: AutonomousWorkflow,
    failed_step: WorkflowStep,
    *,
    cancellation_token: Optional[Any] = None,
    path: Optional[AutonomousExecutionPath] = None,
) -> AutonomousWorkflow:
    # Recovery creates NEW proposal + NEW authorization (never reuse)
    # Existing recovery/replanner from autonomous path; here we create a new proposal based on step description.
    path = path or AutonomousExecutionPath(autonomy_policy=AutonomyPolicy(enabled=False))
    budget = workflow.budget or AutonomousExecutionBudget()
    budget.start()
    # Cancellation guard
    if workflow.cancellation_state or (cancellation_token is not None and getattr(cancellation_token, "cancelled", False)):
        failed_step.status = StepStatus.CANCELLED
        failed_step.result = AutonomousExecutionResult(
            proposal_id=failed_step.step_id,
            status=ExecutionStatus.CANCELLED,
            decision="CANCELLED",
            executed=False,
            verified=False,
            reason="cancelled during recovery",
        )
        workflow.status = WorkflowStatus.CANCELLED
        return workflow
    # Budget check for recovery attempt (shared budget, never reset per step)
    if budget.is_action_budget_exceeded() or budget.is_duration_budget_exceeded() or budget.is_cancelled():
        failed_step.status = StepStatus.BLOCKED
        failed_step.result = AutonomousExecutionResult(
            proposal_id=failed_step.step_id,
            status=ExecutionStatus.FAILED,
            decision="BLOCKED",
            executed=False,
            verified=False,
            reason="budget exceeded during recovery",
            audit={"budget": budget.budget_state()},
        )
        workflow.status = WorkflowStatus.BLOCKED
        workflow.escalation_state = "budget_exceeded_during_recovery"
        return workflow
    # Create new proposal (reuse action_proposal create)
    from veronica.vision.action_proposal import create_action_proposal, ActionType, ActionRisk
    # Use original proposal attributes where available
    original = failed_step.proposal
    new_prop = create_action_proposal(
        proposal_id=f"{original.proposal_id}_recovery" if original and original.proposal_id else f"{failed_step.step_id}_recovery",
        action_type=getattr(original, "action_type", ActionType.CLICK) if original else ActionType.CLICK,
        risk_level=getattr(original, "risk_level", ActionRisk.LOW) if original else ActionRisk.LOW,
    )
    # Independent authorization bound to new proposal
    from veronica.vision.computer_use import ExecutionAuthorization
    new_auth = ExecutionAuthorization(
        mode="POLICY_AUTO",
        proposal_id=new_prop.proposal_id,
        policy_version=path.policy.policy_version,
    )
    failed_step.proposal = new_prop
    failed_step.authorization = new_auth
    # Execute new proposal through existing path (budget continues)
    res = path.execute_proposal(new_prop, context={"budget": budget})
    failed_step.result = res
    # Update verification status from result
    if res.status == ExecutionStatus.EXECUTED_VERIFIED and res.verified:
        failed_step.status = StepStatus.VERIFIED
        failed_step.verification_status = "VERIFIED"
    elif res.status == ExecutionStatus.EXECUTED_UNVERIFIED:
        failed_step.status = StepStatus.UNKNOWN
        failed_step.verification_status = "UNKNOWN"
    elif res.status == ExecutionStatus.FAILED:
        failed_step.status = StepStatus.FAILED
        failed_step.verification_status = "FAILED"
    elif res.status == ExecutionStatus.CANCELLED:
        failed_step.status = StepStatus.CANCELLED
    elif res.status == ExecutionStatus.BLOCKED:
        failed_step.status = StepStatus.BLOCKED
    else:
        failed_step.status = StepStatus.UNKNOWN
        failed_step.verification_status = "UNKNOWN"
    # Budget consumption for recovery attempt (same shared budget)
    if budget is not None:
        budget.record_action()
    # After recovery, if verified, allow workflow to continue from this step; else escalate
    if failed_step.status == StepStatus.VERIFIED:
        workflow.status = WorkflowStatus.RUNNING
    elif failed_step.status == StepStatus.UNKNOWN:
        workflow.status = WorkflowStatus.ESCALATED
        workflow.escalation_state = "recovery_unknown"
    elif failed_step.status == StepStatus.BLOCKED:
        workflow.status = WorkflowStatus.BLOCKED
    elif failed_step.status == StepStatus.CANCELLED:
        workflow.status = WorkflowStatus.CANCELLED
    else:
        workflow.status = WorkflowStatus.ESCALATED
        if not workflow.escalation_state:
            workflow.escalation_state = "recovery_failed"
    return workflow
