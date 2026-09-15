"""
Failure diagnosis engine: determines WHY a step failed and suggests recovery actions.

The diagnosis is deterministic where possible and produces structured diagnoses that
drive the replan cycle. It uses the observation layer's categorized failures and
the learning subsystem's retrieved experience to propose recovery actions.

Key principle: Diagnosis NEVER executes tools. It only proposes recovery actions
that must pass through the existing permission system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from veronica.automation.observation import (
    FailureCategory,
    StepObservation,
    TaskObservation,
    EvidenceSufficiency,
)
from veronica.learning.lesson import Lesson
from veronica.logging_setup import get_logger

logger = get_logger("automation.diagnosis")


class DiagnosisConfidence(Enum):
    """Confidence level in the diagnosis and proposed recovery."""
    HIGH = "high"           # Clear failure category, strong evidence, known recovery pattern
    MEDIUM = "medium"       # Reasonable diagnosis, some evidence, plausible recovery
    LOW = "low"             # Ambiguous failure, weak evidence, speculative recovery
    NEEDS_HUMAN = "needs_human"  # Cannot diagnose automatically, requires human input


@dataclass
class RecoveryAction:
    """A proposed recovery action to address a diagnosed failure."""
    description: str                    # Human-readable description
    action_type: str                    # "retry", "arg_change", "tool_substitute", "step_insert", "step_skip", "human_intervention"
    target_step_index: int              # Which step this recovery applies to
    details: Dict[str, Any] = field(default_factory=dict)  # Specifics: new_args, substitute_tool, etc.
    requires_permission: bool = False   # Whether this action needs permission gate
    confidence: DiagnosisConfidence = DiagnosisConfidence.LOW
    source: str = "diagnosis"           # "diagnosis", "experience", "heuristic"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "description": self.description,
            "action_type": self.action_type,
            "target_step_index": self.target_step_index,
            "details": self.details,
            "requires_permission": self.requires_permission,
            "confidence": self.confidence.value,
            "source": self.source,
        }


@dataclass
class StepDiagnosis:
    """Diagnosis for a single failed step."""
    step_index: int
    observation: StepObservation
    root_cause: str                     # Brief explanation of why it failed
    confidence: DiagnosisConfidence
    recovery_actions: List[RecoveryAction] = field(default_factory=list)
    # Learning integration
    relevant_lessons: List[Lesson] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_index": self.step_index,
            "root_cause": self.root_cause,
            "confidence": self.confidence.value,
            "recovery_actions": [ra.to_dict() for ra in self.recovery_actions],
            "relevant_lessons": [
                {"strategy": l.strategy, "reason": l.reason, "confidence": l.confidence}
                for l in self.relevant_lessons
            ],
            "notes": self.notes,
        }


@dataclass
class TaskDiagnosis:
    """Complete diagnosis for a failed task."""
    task_id: str
    goal: str
    step_diagnoses: List[StepDiagnosis]
    overall_confidence: DiagnosisConfidence
    recommended_approach: str           # "replan", "retry", "skip_failed", "human_intervention"
    # If replanning, these are the steps to keep (completed successfully)
    preserved_step_indices: List[int] = field(default_factory=list)
    # If replanning, this is the remaining goal to achieve
    remaining_goal: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "step_diagnoses": [sd.to_dict() for sd in self.step_diagnoses],
            "overall_confidence": self.overall_confidence.value,
            "recommended_approach": self.recommended_approach,
            "preserved_step_indices": self.preserved_step_indices,
            "remaining_goal": self.remaining_goal,
            "notes": self.notes,
        }


# --- Recovery action generators per failure category ---------------------------

def _recovery_for_permission_denial(obs: StepObservation, lessons: Optional[List[Lesson]] = None) -> List[RecoveryAction]:
    """Recovery actions for permission denial."""
    return [
        RecoveryAction(
            description="Request user permission for this step",
            action_type="human_intervention",
            target_step_index=obs.observed.step_index,
            details={"reason": "permission_denied", "tool": obs.expected.tool_name},
            requires_permission=True,
            confidence=DiagnosisConfidence.HIGH,
            source="diagnosis",
        )
    ]


def _recovery_for_missing_resource(obs: StepObservation, lessons: List[Lesson]) -> List[RecoveryAction]:
    """Recovery actions for missing resource (file/dir not found)."""
    actions = []
    tool = obs.expected.tool_name
    args = obs.expected.args
    path = args.get("file_path") or args.get("path") or args.get("folder_path") or ""

    # Check lessons for alternative paths or tools
    for lesson in lessons:
        if "search" in lesson.strategy.lower() or "find" in lesson.strategy.lower():
            actions.append(RecoveryAction(
                description=f"Try {lesson.strategy} to locate the resource first",
                action_type="step_insert",
                target_step_index=obs.observed.step_index,
                details={"new_step": {
                    "description": f"Search for {path}",
                    "tool": lesson.strategy,
                    "args": {"query": path} if lesson.strategy == "web_search" else {"file_path": path}
                }},
                requires_permission=False,
                confidence=DiagnosisConfidence.MEDIUM,
                source="experience",
            ))

    # Default: try to find the resource
    if tool == "read_file":
        actions.append(RecoveryAction(
            description="Search for the file before reading",
            action_type="step_insert",
            target_step_index=obs.observed.step_index,
            details={"new_step": {
                "description": f"Find file: {path}",
                "tool": "find_file",
                "args": {"file_path": path}
            }},
            requires_permission=False,
            confidence=DiagnosisConfidence.MEDIUM,
            source="heuristic",
        ))
    else:
        # Generic recovery for other tools with missing resource
        actions.append(RecoveryAction(
            description="Verify resource exists before retrying",
            action_type="arg_change",
            target_step_index=obs.observed.step_index,
            details={"original_args": args, "suggestion": f"Check that resource exists: {path or 'unknown'}"},
            requires_permission=False,
            confidence=DiagnosisConfidence.MEDIUM,
            source="heuristic",
        ))

    return actions


def _recovery_for_invalid_path(obs: StepObservation, lessons: Optional[List[Lesson]] = None) -> List[RecoveryAction]:
    """Recovery actions for invalid path."""
    path = obs.expected.args.get("file_path") or obs.expected.args.get("path") or ""
    return [
        RecoveryAction(
            description="Fix the path and retry",
            action_type="arg_change",
            target_step_index=obs.observed.step_index,
            details={"original_path": path, "suggestion": "Check path spelling, use absolute path, or search for correct path"},
            requires_permission=False,
            confidence=DiagnosisConfidence.MEDIUM,
            source="diagnosis",
        )
    ]


def _recovery_for_malformed_args(obs: StepObservation, lessons: List[Lesson]) -> List[RecoveryAction]:
    """Recovery actions for malformed arguments."""
    actions = []

    # Check lessons for correct argument patterns
    for lesson in lessons:
        if lesson.strategy == obs.expected.tool_name and lesson.confidence > 0.7:
            actions.append(RecoveryAction(
                description=f"Use learned argument pattern for {obs.expected.tool_name}",
                action_type="arg_change",
                target_step_index=obs.observed.step_index,
                details={"suggestion": lesson.reason, "learned_confidence": lesson.confidence},
                requires_permission=False,
                confidence=DiagnosisConfidence.MEDIUM,
                source="experience",
            ))

    actions.append(RecoveryAction(
        description="Review and correct the arguments",
        action_type="arg_change",
        target_step_index=obs.observed.step_index,
        details={"original_args": obs.expected.args, "error": obs.observed.error},
        requires_permission=False,
        confidence=DiagnosisConfidence.MEDIUM,
        source="diagnosis",
    ))

    return actions


def _recovery_for_external_service_failure(obs: StepObservation, lessons: Optional[List[Lesson]] = None) -> List[RecoveryAction]:
    """Recovery actions for external service failure."""
    if obs.observed.attempts >= 2:
        return [
            RecoveryAction(
                description="Service appears unavailable; try alternative approach",
                action_type="tool_substitute",
                target_step_index=obs.observed.step_index,
                details={"original_tool": obs.expected.tool_name, "reason": "service_unavailable_after_retries"},
                requires_permission=False,
                confidence=DiagnosisConfidence.MEDIUM,
                source="diagnosis",
            ),
            RecoveryAction(
                description="Wait and retry later (human decision)",
                action_type="human_intervention",
                target_step_index=obs.observed.step_index,
                details={"reason": "external_service_unavailable"},
                requires_permission=True,
                confidence=DiagnosisConfidence.HIGH,
                source="diagnosis",
            ),
        ]
    else:
        return [
            RecoveryAction(
                description="Retry the step (may be transient)",
                action_type="retry",
                target_step_index=obs.observed.step_index,
                details={"max_additional_retries": 2},
                requires_permission=False,
                confidence=DiagnosisConfidence.HIGH,
                source="diagnosis",
            )
        ]


def _recovery_for_transient_failure(obs: StepObservation, lessons: Optional[List[Lesson]] = None) -> List[RecoveryAction]:
    """Recovery actions for transient failure."""
    if obs.observed.attempts >= 3:
        return [
            RecoveryAction(
                description="Transient failure persisted after retries; try alternative",
                action_type="tool_substitute",
                target_step_index=obs.observed.step_index,
                details={"original_tool": obs.expected.tool_name, "reason": "transient_failure_persisted"},
                requires_permission=False,
                confidence=DiagnosisConfidence.MEDIUM,
                source="diagnosis",
            )
        ]
    return [
        RecoveryAction(
            description="Retry the step (transient failure)",
            action_type="retry",
            target_step_index=obs.observed.step_index,
            details={"max_additional_retries": 2},
            requires_permission=False,
            confidence=DiagnosisConfidence.HIGH,
            source="diagnosis",
        )
    ]


def _recovery_for_state_changed(obs: StepObservation, lessons: List[Lesson]) -> List[RecoveryAction]:
    """Recovery actions for state changed between plan and execution."""
    actions = [
        RecoveryAction(
            description="Re-verify current state and adjust plan",
            action_type="step_insert",
            target_step_index=obs.observed.step_index,
            details={"new_step": {
                "description": "Check current state before proceeding",
                "tool": "list_directory" if "list" in obs.expected.tool_name else "read_file",
                "args": {"path": obs.expected.args.get("path", obs.expected.args.get("file_path", ""))}
            }},
            requires_permission=False,
            confidence=DiagnosisConfidence.MEDIUM,
            source="diagnosis",
        )
    ]

    # Check for learned recovery patterns
    for lesson in lessons:
        if "recover" in lesson.reason.lower() or lesson.outcome == "recovered":
            actions.append(RecoveryAction(
                description=f"Apply learned recovery: {lesson.reason}",
                action_type="tool_substitute",
                target_step_index=obs.observed.step_index,
                details={"substitute_tool": lesson.strategy, "learned_reason": lesson.reason},
                requires_permission=False,
                confidence=DiagnosisConfidence.MEDIUM,
                source="experience",
            ))

    return actions


def _recovery_for_verification_failure(obs: StepObservation) -> List[RecoveryAction]:
    """Recovery actions for verification failure (tool succeeded but verification didn't)."""
    actions = []

    # If file missing, try to find it
    if "missing" in obs.observed.verification_note.lower() or "not found" in obs.observed.verification_note.lower():
        path = obs.expected.args.get("file_path") or obs.expected.args.get("path") or ""
        actions.append(RecoveryAction(
            description="Search for the expected output",
            action_type="step_insert",
            target_step_index=obs.observed.step_index,
            details={"new_step": {
                "description": f"Find created output: {path}",
                "tool": "find_file",
                "args": {"file_path": path}
            }},
            requires_permission=False,
            confidence=DiagnosisConfidence.MEDIUM,
            source="diagnosis",
        ))

    # If URL invalid, try to fix it
    if "invalid" in obs.observed.verification_note.lower() and "url" in obs.observed.verification_note.lower():
        actions.append(RecoveryAction(
            description="Fix the URL and retry",
            action_type="arg_change",
            target_step_index=obs.observed.step_index,
            details={"original_url": obs.expected.args.get("url", ""), "error": obs.observed.verification_note},
            requires_permission=False,
            confidence=DiagnosisConfidence.MEDIUM,
            source="diagnosis",
        ))

    actions.append(RecoveryAction(
        description="Proceed despite verification warning (human decision)",
        action_type="human_intervention",
        target_step_index=obs.observed.step_index,
        details={"verification_note": obs.observed.verification_note},
        requires_permission=True,
        confidence=DiagnosisConfidence.HIGH,
        source="diagnosis",
    ))

    return actions


def _recovery_for_wrong_assumption(obs: StepObservation, lessons: List[Lesson]) -> List[RecoveryAction]:
    """Recovery actions for wrong assumption."""
    actions = []

    # Check lessons for alternative strategies
    for lesson in lessons:
        if lesson.confidence > 0.6 and lesson.strategy != obs.expected.tool_name:
            actions.append(RecoveryAction(
                description=f"Try alternative strategy from experience: {lesson.strategy}",
                action_type="tool_substitute",
                target_step_index=obs.observed.step_index,
                details={"substitute_tool": lesson.strategy, "learned_reason": lesson.reason},
                requires_permission=False,
                confidence=DiagnosisConfidence.MEDIUM,
                source="experience",
            ))

    actions.append(RecoveryAction(
        description="Re-plan from this step with corrected assumptions",
        action_type="human_intervention",
        target_step_index=obs.observed.step_index,
        details={"original_tool": obs.expected.tool_name, "assumption_error": obs.observed.error},
        requires_permission=True,
        confidence=DiagnosisConfidence.MEDIUM,
        source="diagnosis",
    ))

    return actions


def _recovery_for_tool_failure(obs: StepObservation, lessons: List[Lesson]) -> List[RecoveryAction]:
    """Recovery actions for tool internal failure."""
    actions = []

    # Check lessons for alternative tools
    for lesson in lessons:
        if lesson.confidence > 0.6 and lesson.strategy != obs.expected.tool_name:
            actions.append(RecoveryAction(
                description=f"Substitute with alternative tool: {lesson.strategy}",
                action_type="tool_substitute",
                target_step_index=obs.observed.step_index,
                details={"substitute_tool": lesson.strategy, "learned_reason": lesson.reason},
                requires_permission=False,
                confidence=DiagnosisConfidence.MEDIUM,
                source="experience",
            ))

    actions.append(RecoveryAction(
        description="Retry with different parameters or alternative tool",
        action_type="tool_substitute",
        target_step_index=obs.observed.step_index,
        details={"original_tool": obs.expected.tool_name, "reason": "tool_internal_failure"},
        requires_permission=False,
        confidence=DiagnosisConfidence.LOW,
        source="diagnosis",
    ))

    return actions


def _recovery_for_unknown_failure(obs: StepObservation, lessons: List[Lesson]) -> List[RecoveryAction]:
    """Recovery actions for unknown failure category."""
    actions = []

    # Check all lessons for any relevant recovery
    for lesson in lessons:
        if lesson.confidence > 0.6:
            actions.append(RecoveryAction(
                description=f"Consider learned approach: {lesson.strategy}",
                action_type="tool_substitute",
                target_step_index=obs.observed.step_index,
                details={"substitute_tool": lesson.strategy, "learned_reason": lesson.reason},
                requires_permission=False,
                confidence=DiagnosisConfidence.LOW,
                source="experience",
            ))

    actions.append(RecoveryAction(
        description="Unable to diagnose automatically; human review needed",
        action_type="human_intervention",
        target_step_index=obs.observed.step_index,
        details={"error": obs.observed.error, "verification": obs.observed.verification_note},
        requires_permission=True,
        confidence=DiagnosisConfidence.HIGH,
        source="diagnosis",
    ))

    return actions


# --- Main diagnosis functions --------------------------------------------------

CATEGORY_RECOVERY_FNS = {
    FailureCategory.PERMISSION_DENIAL: _recovery_for_permission_denial,
    FailureCategory.MISSING_RESOURCE: _recovery_for_missing_resource,
    FailureCategory.INVALID_PATH: _recovery_for_invalid_path,
    FailureCategory.MALFORMED_ARGS: _recovery_for_malformed_args,
    FailureCategory.EXTERNAL_SERVICE_FAILURE: _recovery_for_external_service_failure,
    FailureCategory.TRANSIENT_FAILURE: _recovery_for_transient_failure,
    FailureCategory.STATE_CHANGED: _recovery_for_state_changed,
    FailureCategory.VERIFICATION_FAILURE: _recovery_for_verification_failure,
    FailureCategory.WRONG_ASSUMPTION: _recovery_for_wrong_assumption,
    FailureCategory.TOOL_FAILURE: _recovery_for_tool_failure,
    FailureCategory.UNKNOWN_FAILURE: _recovery_for_unknown_failure,
}


def _compute_diagnosis_confidence(category: FailureCategory, evidence: EvidenceSufficiency, lessons: List[Lesson]) -> DiagnosisConfidence:
    """Compute overall confidence in the diagnosis."""
    if evidence == EvidenceSufficiency.SUFFICIENT:
        if category in (FailureCategory.PERMISSION_DENIAL, FailureCategory.MALFORMED_ARGS, FailureCategory.MISSING_RESOURCE):
            return DiagnosisConfidence.HIGH
        if lessons and any(l.confidence > 0.7 for l in lessons):
            return DiagnosisConfidence.HIGH
        return DiagnosisConfidence.MEDIUM
    elif evidence == EvidenceSufficiency.AMBIGUOUS:
        if lessons and any(l.confidence > 0.7 for l in lessons):
            return DiagnosisConfidence.MEDIUM
        return DiagnosisConfidence.LOW
    else:  # INSUFFICIENT
        if category in (FailureCategory.TRANSIENT_FAILURE, FailureCategory.EXTERNAL_SERVICE_FAILURE):
            return DiagnosisConfidence.MEDIUM  # Retry is reasonable
        return DiagnosisConfidence.LOW


def diagnose_step(obs: StepObservation, lessons: List[Lesson] = None) -> StepDiagnosis:
    """Diagnose a single step's failure and propose recovery actions."""
    lessons = lessons or []

    category = obs.category
    evidence = obs.evidence

    # Generate root cause description
    root_cause_map = {
        FailureCategory.PERMISSION_DENIAL: "Permission was denied by the user or system policy",
        FailureCategory.MISSING_RESOURCE: f"Required resource not found: {obs.expected.args.get('file_path') or obs.expected.args.get('path') or 'unknown'}",
        FailureCategory.INVALID_PATH: f"Invalid path provided: {obs.expected.args.get('file_path') or obs.expected.args.get('path') or 'unknown'}",
        FailureCategory.MALFORMED_ARGS: f"Arguments invalid for {obs.expected.tool_name}: {obs.observed.error}",
        FailureCategory.EXTERNAL_SERVICE_FAILURE: "External service (network/API) failed or unreachable",
        FailureCategory.TRANSIENT_FAILURE: "Transient failure — may succeed on retry",
        FailureCategory.STATE_CHANGED: "Environment state changed between plan and execution",
        FailureCategory.VERIFICATION_FAILURE: f"Tool succeeded but verification failed: {obs.observed.verification_note}",
        FailureCategory.WRONG_ASSUMPTION: f"Plan assumption incorrect: {obs.observed.error}",
        FailureCategory.TOOL_FAILURE: f"Tool {obs.expected.tool_name} encountered internal error",
        FailureCategory.UNKNOWN_FAILURE: f"Unknown failure: {obs.observed.error or obs.observed.verification_note}",
    }
    root_cause = root_cause_map.get(category, "Unknown failure")

    # Get recovery actions for this category
    recovery_fn = CATEGORY_RECOVERY_FNS.get(category, _recovery_for_unknown_failure)
    recovery_actions = recovery_fn(obs, lessons)

    # Compute confidence
    confidence = _compute_diagnosis_confidence(category, evidence, lessons)

    # Filter relevant lessons (those mentioning the failed tool or similar context)
    relevant_lessons = [
        l for l in lessons
        if l.strategy == obs.expected.tool_name
        or l.strategy in obs.expected.args.values()
        or any(kw in l.reason.lower() for kw in ["fail", "recover", "retry", "alternative"])
    ][:3]  # Limit to top 3

    notes = list(obs.notes)
    if not recovery_actions:
        notes.append("No automatic recovery actions available for this failure type.")

    return StepDiagnosis(
        step_index=obs.observed.step_index,
        observation=obs,
        root_cause=root_cause,
        confidence=confidence,
        recovery_actions=recovery_actions,
        relevant_lessons=relevant_lessons,
        notes=notes,
    )


def _determine_overall_approach(step_diagnoses: List[StepDiagnosis]) -> tuple[str, DiagnosisConfidence]:
    """Determine the overall recommended approach based on all step diagnoses."""
    if not step_diagnoses:
        return "none", DiagnosisConfidence.HIGH

    # Count high-confidence recoveries
    high_conf = sum(1 for sd in step_diagnoses if sd.confidence == DiagnosisConfidence.HIGH)
    med_conf = sum(1 for sd in step_diagnoses if sd.confidence == DiagnosisConfidence.MEDIUM)
    needs_human = any(sd.confidence == DiagnosisConfidence.NEEDS_HUMAN for sd in step_diagnoses)

    # If any step needs human, overall needs human
    if needs_human:
        return "human_intervention", DiagnosisConfidence.HIGH

    # If most steps have high/medium confidence recovery, replan
    if high_conf + med_conf >= len(step_diagnoses) * 0.7:
        return "replan", DiagnosisConfidence.MEDIUM

    # If only low confidence, need human
    if high_conf == 0 and med_conf == 0:
        return "human_intervention", DiagnosisConfidence.HIGH

    return "replan", DiagnosisConfidence.LOW


def diagnose_task(task_obs: TaskObservation, lessons: List[Lesson] = None) -> TaskDiagnosis:
    """Diagnose an entire task's failure and produce a complete TaskDiagnosis."""
    lessons = lessons or []

    step_diagnoses = []
    for step_obs in task_obs.step_observations:
        if step_obs.category != FailureCategory.UNKNOWN_FAILURE or not step_obs.observed.success:
            # Only diagnose steps that actually failed or had issues
            step_diag = diagnose_step(step_obs, lessons)
            step_diagnoses.append(step_diag)

    # Determine preserved steps (completed successfully before failure)
    preserved = [i for i, s in enumerate(task_obs.step_observations)
                 if s.observed.success and s.observed.verified]

    # Determine remaining goal
    if task_obs.failed_step_indices:
        first_failed = task_obs.failed_step_indices[0]
        remaining_steps = task_obs.step_observations[first_failed:]
        remaining_goal = "Continue from step " + str(first_failed + 1) + ": " + "; ".join(
            s.expected.description for s in remaining_steps
        )
    else:
        remaining_goal = task_obs.goal

    approach, overall_confidence = _determine_overall_approach(step_diagnoses)

    notes = []
    if approach == "human_intervention":
        notes.append("Automatic recovery not confident; human review required.")
    elif approach == "replan":
        notes.append(f"Recommended: replan from step {preserved[-1] + 2 if preserved else 1} with {len(step_diagnoses)} diagnosed failure(s).")

    logger.info(
        "Task diagnosis: task=%s approach=%s confidence=%s failed_steps=%d",
        task_obs.task_id, approach, overall_confidence.value, len(step_diagnoses)
    )

    return TaskDiagnosis(
        task_id=task_obs.task_id,
        goal=task_obs.goal,
        step_diagnoses=step_diagnoses,
        overall_confidence=overall_confidence,
        recommended_approach=approach,
        preserved_step_indices=preserved,
        remaining_goal=remaining_goal,
        notes=notes,
    )