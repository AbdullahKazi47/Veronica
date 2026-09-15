"""
Observation layer: structured capture of "what actually happened" vs what was expected.

This module implements the VERONICA Phase 6 observation layer. The observer is
deterministic where possible and produces structured observations that drive the
diagnosis → replan cycle.

Key principle: The observer MUST NOT claim causation. It only records the
discrepancy between expected and observed outcomes, categorizes the failure
mode, and assesses evidence sufficiency.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from veronica.automation.task_executor import TaskStep, MultiStepTask, TASK_STATUS
from veronica.logging_setup import get_logger

logger = get_logger("automation.observation")


class FailureCategory(Enum):
    """Deterministic failure categories for diagnosis and same-failure detection."""
    WRONG_ASSUMPTION = "wrong_assumption"
    MISSING_RESOURCE = "missing_resource"
    INVALID_PATH = "invalid_path"
    TOOL_FAILURE = "tool_failure"
    MALFORMED_ARGS = "malformed_args"
    PERMISSION_DENIAL = "permission_denial"
    EXTERNAL_SERVICE_FAILURE = "external_service_failure"
    TRANSIENT_FAILURE = "transient_failure"
    STATE_CHANGED = "state_changed"
    VERIFICATION_FAILURE = "verification_failure"
    UNKNOWN_FAILURE = "unknown_failure"


class EvidenceSufficiency(Enum):
    """Whether we have enough evidence to act on this observation."""
    SUFFICIENT = "sufficient"        # Clear signal, can proceed to diagnosis/replan
    INSUFFICIENT = "insufficient"    # Need more data, may need retry or human input
    AMBIGUOUS = "ambiguous"          # Conflicting signals, needs human review


@dataclass
class ExpectedOutcome:
    """What the plan expected to happen for this step."""
    description: str
    tool_name: str
    args: Dict[str, Any]
    success_criteria: List[str] = field(default_factory=list)  # e.g., ["file_exists", "url_valid"]


@dataclass
class ObservedOutcome:
    """What actually happened during execution."""
    step_index: int
    tool_name: str
    args: Dict[str, Any]
    success: bool
    result: Any
    error: str = ""
    verified: bool = False
    verification_note: str = ""
    attempts: int = 1
    duration_ms: float = 0.0


@dataclass
class StepObservation:
    """Complete observation for one step: expected vs observed, with categorization."""
    expected: ExpectedOutcome
    observed: ObservedOutcome
    category: FailureCategory = FailureCategory.UNKNOWN_FAILURE
    evidence: EvidenceSufficiency = EvidenceSufficiency.INSUFFICIENT
    notes: List[str] = field(default_factory=list)
    # Signature for same-failure detection
    failure_signature: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expected": {
                "description": self.expected.description,
                "tool_name": self.expected.tool_name,
                "args": self.expected.args,
                "success_criteria": self.expected.success_criteria,
            },
            "observed": {
                "step_index": self.observed.step_index,
                "tool_name": self.observed.tool_name,
                "args": self.observed.args,
                "success": self.observed.success,
                "error": self.observed.error,
                "verified": self.observed.verified,
                "verification_note": self.observed.verification_note,
                "attempts": self.observed.attempts,
                "duration_ms": self.observed.duration_ms,
            },
            "category": self.category.value,
            "evidence": self.evidence.value,
            "notes": self.notes,
            "failure_signature": self.failure_signature,
        }


@dataclass
class TaskObservation:
    """Aggregated observation for the entire task execution."""
    task_id: str
    goal: str
    step_observations: List[StepObservation]
    overall_status: str  # "success", "failed", "partial", "cancelled"
    completed_steps: int
    total_steps: int
    failed_step_indices: List[int]
    timestamp: float = field(default_factory=time.time)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "step_observations": [so.to_dict() for so in self.step_observations],
            "overall_status": self.overall_status,
            "completed_steps": self.completed_steps,
            "total_steps": self.total_steps,
            "failed_step_indices": self.failed_step_indices,
            "timestamp": self.timestamp,
            "notes": self.notes,
        }


def _compute_failure_signature(step_obs: StepObservation) -> str:
    """Compute a deterministic failure signature for same-failure detection.

    The signature combines: tool_name, failure_category, and key error characteristics.
    This allows detecting when the SAME failure recurs across retries or replans.
    """
    parts = [
        step_obs.expected.tool_name,
        step_obs.category.value,
        step_obs.observed.error[:80] if step_obs.observed.error else "no_error",
        step_obs.observed.verification_note[:80] if step_obs.observed.verification_note else "no_verification",
    ]
    signature_str = "|".join(parts)
    return hashlib.sha256(signature_str.encode()).hexdigest()[:16]


def _categorize_failure(expected: ExpectedOutcome, observed: ObservedOutcome) -> FailureCategory:
    """Deterministically categorize the failure based on observed vs expected."""
    if observed.success:
        # Success — but we still check if verification failed
        if not observed.verified:
            return FailureCategory.VERIFICATION_FAILURE
        return FailureCategory.UNKNOWN_FAILURE  # Not really a failure

    error_lower = (observed.error or "").lower()
    verification_lower = (observed.verification_note or "").lower()

    # Permission denial patterns
    if any(p in error_lower for p in ["permission", "denied", "unauthorized", "forbidden", "not permitted", "declined"]):
        return FailureCategory.PERMISSION_DENIAL

    # Malformed args patterns (check BEFORE missing resource to avoid "missing required" being misclassified)
    if any(p in error_lower for p in ["invalid argument", "bad argument", "malformed", "type error", "missing required", "unexpected keyword"]):
        return FailureCategory.MALFORMED_ARGS

    # Missing resource patterns
    if any(p in error_lower for p in ["not found", "no such file", "no such directory", "does not exist", "missing"]):
        return FailureCategory.MISSING_RESOURCE

    # Invalid path patterns
    if any(p in error_lower for p in ["invalid path", "bad path", "path not valid", "illegal path"]):
        return FailureCategory.INVALID_PATH

    # External service patterns
    if any(p in error_lower for p in ["timeout", "connection", "network", "unreachable", "dns", "ssl", "certificate", "rate limit", "429", "502", "503", "504"]):
        return FailureCategory.EXTERNAL_SERVICE_FAILURE

    # Tool failure (tool itself crashed or returned explicit failure)
    if "tool" in error_lower or "dispatch" in error_lower or "failed unexpectedly" in error_lower:
        return FailureCategory.TOOL_FAILURE

    # State changed patterns
    if any(p in verification_lower for p in ["missing", "changed", "different", "not found", "gone"]):
        return FailureCategory.STATE_CHANGED

    # Verification failure (tool said success but verification says no)
    if observed.success and not observed.verified:
        return FailureCategory.VERIFICATION_FAILURE

    # Transient failure indicators (retryable by nature)
    if any(p in error_lower for p in ["transient", "temporary", "retry", "busy", "locked", "try again"]):
        return FailureCategory.TRANSIENT_FAILURE

    # Wrong assumption: the plan assumed something that wasn't true
    # This is a catch-all for when the tool "succeeded" but didn't do what was expected
    if "assumption" in error_lower or "expected" in error_lower:
        return FailureCategory.WRONG_ASSUMPTION

    return FailureCategory.UNKNOWN_FAILURE


def _assess_evidence(category: FailureCategory, observed: ObservedOutcome) -> EvidenceSufficiency:
    """Assess whether we have enough evidence to act on this failure."""
    # Categories with clear, actionable evidence
    if category in (
        FailureCategory.PERMISSION_DENIAL,
        FailureCategory.MALFORMED_ARGS,
        FailureCategory.MISSING_RESOURCE,
        FailureCategory.INVALID_PATH,
    ):
        return EvidenceSufficiency.SUFFICIENT

    # Categories that might be transient
    if category in (
        FailureCategory.EXTERNAL_SERVICE_FAILURE,
        FailureCategory.TRANSIENT_FAILURE,
    ):
        # If we've retried multiple times, evidence is sufficient
        if observed.attempts >= 2:
            return EvidenceSufficiency.SUFFICIENT
        return EvidenceSufficiency.INSUFFICIENT

    # Tool failure and state changed — depends on context
    if category in (FailureCategory.TOOL_FAILURE, FailureCategory.STATE_CHANGED):
        return EvidenceSufficiency.SUFFICIENT

    # Verification failure — depends on what the verification said
    if category == FailureCategory.VERIFICATION_FAILURE:
        if "missing" in observed.verification_note.lower() or "not found" in observed.verification_note.lower():
            return EvidenceSufficiency.SUFFICIENT
        return EvidenceSufficiency.AMBIGUOUS

    # Wrong assumption and unknown — usually ambiguous
    if category in (FailureCategory.WRONG_ASSUMPTION, FailureCategory.UNKNOWN_FAILURE):
        return EvidenceSufficiency.AMBIGUOUS

    return EvidenceSufficiency.INSUFFICIENT


def _generate_notes(expected: ExpectedOutcome, observed: ObservedOutcome, category: FailureCategory) -> List[str]:
    """Generate human-readable notes explaining the observation."""
    notes = []

    if observed.success and observed.verified:
        notes.append(f"Step succeeded and verified: {observed.verification_note}")
        return notes

    if observed.success and not observed.verified:
        notes.append(f"Tool reported success but verification failed: {observed.verification_note}")

    if not observed.success:
        notes.append(f"Tool failed: {observed.error}")

    # Add category-specific notes
    if category == FailureCategory.PERMISSION_DENIAL:
        notes.append("Permission was denied — the step cannot proceed without user approval.")
    elif category == FailureCategory.MISSING_RESOURCE:
        notes.append("Expected resource (file/directory/URL) does not exist.")
    elif category == FailureCategory.MALFORMED_ARGS:
        notes.append("Arguments passed to the tool were invalid or incomplete.")
    elif category == FailureCategory.EXTERNAL_SERVICE_FAILURE:
        notes.append("External service (network/API) failure — may be transient.")
    elif category == FailureCategory.TRANSIENT_FAILURE:
        notes.append("Failure appears transient — retry may succeed.")
    elif category == FailureCategory.STATE_CHANGED:
        notes.append("Environment state changed between plan and execution.")
    elif category == FailureCategory.VERIFICATION_FAILURE:
        notes.append("Verification detected a discrepancy between tool result and expected state.")
    elif category == FailureCategory.WRONG_ASSUMPTION:
        notes.append("The plan's assumption about the environment or tool behavior was incorrect.")
    elif category == FailureCategory.TOOL_FAILURE:
        notes.append("The tool itself encountered an internal error.")

    # Add retry info
    if observed.attempts > 1:
        notes.append(f"Retried {observed.attempts - 1} time(s) before this observation.")

    return notes


def observe_step(
    step: TaskStep,
    step_index: int,
    expected_description: str,
) -> StepObservation:
    """Observe a single step's execution and produce a structured observation.

    This is the core observation function — called after each step completes
    (or fails after retries) to capture what happened vs what was expected.
    """
    expected = ExpectedOutcome(
        description=expected_description,
        tool_name=step.tool_name,
        args=step.args or {},
        success_criteria=_derive_success_criteria(step.tool_name, step.args),
    )

    observed = ObservedOutcome(
        step_index=step_index,
        tool_name=step.tool_name,
        args=step.args or {},
        success=step.status == TASK_STATUS["COMPLETED"],
        result=step.result,
        error=step.error or "",
        verified=step.verified,
        verification_note=step.verification_note or "",
        attempts=step.attempts,
        duration_ms=(step.end_time - step.start_time) * 1000 if step.end_time and step.start_time else 0.0,
    )

    category = _categorize_failure(expected, observed)
    evidence = _assess_evidence(category, observed)
    notes = _generate_notes(expected, observed, category)

    step_obs = StepObservation(
        expected=expected,
        observed=observed,
        category=category,
        evidence=evidence,
        notes=notes,
    )
    step_obs.failure_signature = _compute_failure_signature(step_obs)

    logger.info(
        "Observation: step=%d tool=%s category=%s evidence=%s signature=%s",
        step_index, step.tool_name, category.value, evidence.value, step_obs.failure_signature
    )

    return step_obs


def _derive_success_criteria(tool_name: str, args: Dict[str, Any]) -> List[str]:
    """Derive expected success criteria from tool name and args."""
    criteria = []

    if tool_name in {"read_file", "write_file", "delete_file"}:
        path = args.get("file_path") or args.get("path") or ""
        if path:
            criteria.append("file_exists")
    elif tool_name == "list_directory":
        path = args.get("path") or args.get("folder_path") or ""
        if path:
            criteria.append("dir_exists")
    elif tool_name in {"open_website", "fetch_webpage"}:
        url = args.get("url", "")
        if url:
            criteria.append("url_valid")
    elif tool_name == "run_shell_command":
        criteria.append("exit_code_0")

    return criteria


def observe_task(task: MultiStepTask) -> TaskObservation:
    """Observe an entire task execution and produce a complete TaskObservation."""
    step_observations = []
    failed_step_indices = []

    for i, step in enumerate(task.steps):
        step_obs = observe_step(step, i, step.description)
        step_observations.append(step_obs)

        if step.status == TASK_STATUS["FAILED"]:
            failed_step_indices.append(i)

    completed = sum(1 for s in task.steps if s.status == TASK_STATUS["COMPLETED"])

    if task.status == TASK_STATUS["COMPLETED"]:
        overall = "success"
    elif task.status == TASK_STATUS["FAILED"]:
        overall = "failed"
    elif task.status == TASK_STATUS["CANCELLED"]:
        overall = "cancelled"
    elif completed > 0:
        overall = "partial"
    else:
        overall = "failed"

    # Add overall notes
    notes = []
    if failed_step_indices:
        notes.append(f"Failed at step(s): {', '.join(str(i+1) for i in failed_step_indices)}")
    if overall == "success":
        notes.append("All steps completed successfully.")
    elif overall == "partial":
        notes.append(f"Completed {completed}/{len(task.steps)} steps before failure/cancellation.")

    return TaskObservation(
        task_id=task.task_id,
        goal=task.goal,
        step_observations=step_observations,
        overall_status=overall,
        completed_steps=completed,
        total_steps=len(task.steps),
        failed_step_indices=failed_step_indices,
        notes=notes,
    )