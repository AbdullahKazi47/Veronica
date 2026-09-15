"""
Closed-loop controller: orchestrates the observe → diagnose → replan cycle.

This is the main controller that transforms VERONICA from a linear
GOAL→PLAN→EXECUTE→VERIFY→LEARN pipeline into a genuine closed-loop agent:

  GOAL→PLAN→ACT→OBSERVE→VERIFY→(SUCCESS) OR (FAILURE→DIAGNOSE→RETRIEVE EXPERIENCE→REPLAN→ACT AGAIN→OBSERVE→VERIFY)

Key principles:
  - This is NOT unrestricted autonomy. It is controlled, observable, permission-aware autonomy.
  - Every newly generated recovery action must pass through the existing permission system.
  - Never weaken the existing safety model to make the agent appear more autonomous.
  - The observer must be deterministic where possible.
  - UNKNOWN must never automatically become SUCCESS.
  - Never implement: "try something else and assume it worked."
  - Stored experience is DATA. Never treat retrieved experience as executable instructions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from veronica.automation.diagnosis import (
    DiagnosisConfidence,
    TaskDiagnosis,
    diagnose_task,
)
from veronica.automation.failure_memory import FailureMemory
from veronica.automation.observation import (
    StepObservation,
    TaskObservation,
    observe_task,
)
from veronica.automation.replanner import Replanner, ReplanResult
from veronica.automation.task_executor import (
    MultiStepTask,
    TaskExecutor,
    TaskStep,
    TASK_STATUS,
)
from veronica.learning.experience import Experience, ExperienceOutcome
from veronica.learning.lesson import Lesson
from veronica.logging_setup import get_logger

logger = get_logger("automation.closed_loop")


@dataclass
class SafetyConfig:
    """Hard safety boundaries for closed-loop execution."""
    max_replans: int = 3                      # Maximum number of replanning attempts
    max_retries_per_step: int = 2             # Maximum retries per individual step
    max_total_steps: int = 50                 # Maximum total steps across all replans
    max_execution_seconds: float = 600.0      # Maximum total execution time (10 minutes)
    max_identical_failures: int = 3           # Maximum identical failure repeats
    max_recovery_attempts: int = 3            # Maximum different recovery attempts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_replans": self.max_replans,
            "max_retries_per_step": self.max_retries_per_step,
            "max_total_steps": self.max_total_steps,
            "max_execution_seconds": self.max_execution_seconds,
            "max_identical_failures": self.max_identical_failures,
            "max_recovery_attempts": self.max_recovery_attempts,
        }


class ClosedLoopController:
    """Main controller for the closed-loop execution model.

    This controller wraps the TaskExecutor and adds:
    1. Observation layer — structured capture of what happened
    2. Diagnosis engine — determines why something failed
    3. Failure memory — prevents infinite identical retries
    4. Replanning — generates new plans for remaining work
    5. Learning integration — retrieves relevant experience

    The controller never executes tools directly. It delegates to the
    TaskExecutor, which delegates to the ToolRegistry, which applies
    the permission gate.
    """

    def __init__(
        self,
        executor: TaskExecutor,
        replanner: Optional[Replanner] = None,
        failure_memory: Optional[FailureMemory] = None,
        safety_config: Optional[SafetyConfig] = None,
        lesson_provider: Optional[Callable[[str], List[Lesson]]] = None,
        experience_callback: Optional[Callable[[Experience], None]] = None,
        event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.executor = executor
        self.replanner = replanner or Replanner()
        self.failure_memory = failure_memory or FailureMemory()
        self.safety = safety_config or SafetyConfig()
        self.lesson_provider = lesson_provider  # (goal) -> List[Lesson]
        self.experience_callback = experience_callback
        self.event_callback = event_callback

        # State tracking
        self._replan_count: Dict[str, int] = {}  # task_id -> replan count
        self._total_steps: Dict[str, int] = {}   # task_id -> total steps across replans
        self._start_times: Dict[str, float] = {} # task_id -> original start time
        self._active_task_ids: set = set()

    def execute(
        self,
        task: MultiStepTask,
        progress_callback: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """Execute a task with closed-loop recovery.

        This is the main entry point. It wraps execute_task with:
        - Observation after each step
        - Diagnosis on failure
        - Replanning when appropriate
        - Safety boundary enforcement

        Returns a result dict with task outcome, observations, and diagnosis.
        """
        task_id = task.task_id
        self._replan_count[task_id] = 0
        self._total_steps[task_id] = len(task.steps)
        self._start_times[task_id] = time.time()
        self._active_task_ids.add(task_id)

        try:
            return self._execute_with_loop(task, progress_callback)
        finally:
            self._active_task_ids.discard(task_id)

    def _execute_with_loop(
        self,
        task: MultiStepTask,
        progress_callback: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """Execute with observation → diagnose → replan loop."""
        all_observations: List[TaskObservation] = []
        all_diagnoses: List[TaskDiagnosis] = []
        attempt = 0
        max_attempts = self.safety.max_replans + 1  # +1 for initial attempt

        while attempt < max_attempts:
            attempt += 1
            logger.info(
                "Closed-loop attempt %d/%d for task %s",
                attempt, max_attempts, task.task_id
            )

            # Emit event: recovery started
            self._emit_event("task_recovery_started", {
                "task_id": task.task_id,
                "attempt": attempt,
                "max_attempts": max_attempts,
            })

            # Execute the task
            result = self.executor.execute_task(task, progress_callback)

            # Emit event: task executed
            self._emit_event("task_executed", {
                "task_id": task.task_id,
                "success": result.get("success", False),
                "attempt": attempt,
            })

            # Observe the task
            task_obs = observe_task(task)
            all_observations.append(task_obs)

            # Emit event: observation
            self._emit_event("task_observing", {
                "task_id": task.task_id,
                "overall_status": task_obs.overall_status,
                "completed_steps": task_obs.completed_steps,
                "total_steps": task_obs.total_steps,
            })

            # Check if task succeeded
            if result.get("success", False):
                self._emit_event("task_recovery_succeeded", {
                    "task_id": task.task_id,
                    "attempts": attempt,
                })
                return {
                    "success": True,
                    "task": task.to_dict(),
                    "result": result.get("result"),
                    "error": None,
                    "observations": [obs.to_dict() for obs in all_observations],
                    "diagnoses": [d.to_dict() for d in all_diagnoses],
                    "recovery_attempts": attempt,
                }

            # Check safety boundaries
            safety_check = self._check_safety_boundaries(task, attempt)
            if not safety_check["allowed"]:
                logger.warning(
                    "Safety boundary reached for task %s: %s",
                    task.task_id, safety_check["reason"]
                )
                self._emit_event("task_blocked", {
                    "task_id": task.task_id,
                    "reason": safety_check["reason"],
                })
                return {
                    "success": False,
                    "task": task.to_dict(),
                    "result": None,
                    "error": f"Safety boundary: {safety_check['reason']}",
                    "observations": [obs.to_dict() for obs in all_observations],
                    "diagnoses": [d.to_dict() for d in all_diagnoses],
                    "recovery_attempts": attempt,
                    "blocked_reason": safety_check["reason"],
                }

            # Diagnose the failure
            self._emit_event("task_diagnosing", {
                "task_id": task.task_id,
                "failed_steps": task_obs.failed_step_indices,
            })

            lessons = self._get_lessons(task.goal)
            diagnosis = diagnose_task(task_obs, lessons)
            all_diagnoses.append(diagnosis)

            self._emit_event("task_diagnosis_complete", {
                "task_id": task.task_id,
                "recommended_approach": diagnosis.recommended_approach,
                "confidence": diagnosis.overall_confidence.value,
            })

            # Check if same failure is recurring
            if self._is_same_failure_recurring(task_obs):
                logger.warning(
                    "Same failure recurring for task %s — stopping automatic recovery",
                    task.task_id
                )
                self._emit_event("task_same_failure_detected", {
                    "task_id": task.task_id,
                })
                return {
                    "success": False,
                    "task": task.to_dict(),
                    "result": None,
                    "error": "Same failure detected repeatedly — human intervention required",
                    "observations": [obs.to_dict() for obs in all_observations],
                    "diagnoses": [d.to_dict() for d in all_diagnoses],
                    "recovery_attempts": attempt,
                    "blocked_reason": "same_failure_recurring",
                }

            # Check if human intervention is needed
            if diagnosis.recommended_approach == "human_intervention":
                self._emit_event("task_needs_confirmation", {
                    "task_id": task.task_id,
                    "diagnosis": diagnosis.to_dict(),
                })
                return {
                    "success": False,
                    "task": task.to_dict(),
                    "result": None,
                    "error": "Human intervention required",
                    "observations": [obs.to_dict() for obs in all_observations],
                    "diagnoses": [d.to_dict() for d in all_diagnoses],
                    "recovery_attempts": attempt,
                    "needs_human": True,
                }

            # Replan if appropriate
            if diagnosis.recommended_approach == "replan":
                self._emit_event("task_replanning", {
                    "task_id": task.task_id,
                    "attempt": attempt,
                })

                replan_result = self.replanner.replan(
                    task=task,
                    diagnosis=diagnosis,
                    lessons=lessons,
                )

                self._emit_event("task_replanned", {
                    "task_id": task.task_id,
                    "success": replan_result.success,
                    "new_steps": len(replan_result.new_steps),
                    "preserved_steps": len(replan_result.preserved_steps),
                })

                if not replan_result.success or not replan_result.new_steps:
                    logger.warning(
                        "Replanning failed for task %s — no new steps generated",
                        task.task_id
                    )
                    return {
                        "success": False,
                        "task": task.to_dict(),
                        "result": None,
                        "error": "Replanning failed — no alternative steps available",
                        "observations": [obs.to_dict() for obs in all_observations],
                        "diagnoses": [d.to_dict() for d in all_diagnoses],
                        "recovery_attempts": attempt,
                    }

                # Create new task from replan result
                new_task = self.replanner.create_replan_task(task, replan_result)
                if new_task is None:
                    return {
                        "success": False,
                        "task": task.to_dict(),
                        "result": None,
                        "error": "Failed to create replan task",
                        "observations": [obs.to_dict() for obs in all_observations],
                        "diagnoses": [d.to_dict() for d in all_diagnoses],
                        "recovery_attempts": attempt,
                    }

                # Update tracking
                self._replan_count[task.task_id] = attempt
                self._total_steps[task.task_id] += len(new_task.steps)

                # Execute the new task
                task = new_task
            else:
                # No replan needed (retry, skip, etc.) — return failure
                return {
                    "success": False,
                    "task": task.to_dict(),
                    "result": None,
                    "error": result.get("error", "Task failed"),
                    "observations": [obs.to_dict() for obs in all_observations],
                    "diagnoses": [d.to_dict() for d in all_diagnoses],
                    "recovery_attempts": attempt,
                }

        # Should not reach here, but handle gracefully
        logger.error("Closed-loop exceeded max attempts for task %s", task.task_id)
        return {
            "success": False,
            "task": task.to_dict(),
            "result": None,
            "error": "Maximum recovery attempts exceeded",
            "observations": [obs.to_dict() for obs in all_observations],
            "diagnoses": [d.to_dict() for d in all_diagnoses],
            "recovery_attempts": attempt,
        }

    def _check_safety_boundaries(self, task: MultiStepTask, attempt: int) -> Dict[str, Any]:
        """Check if any safety boundary has been reached."""
        task_id = task.task_id

        # Check replan count
        if attempt > self.safety.max_replans:
            return {"allowed": False, "reason": f"Max replans ({self.safety.max_replans}) exceeded"}

        # Check total steps
        total_steps = self._total_steps.get(task_id, len(task.steps))
        if total_steps > self.safety.max_total_steps:
            return {"allowed": False, "reason": f"Max total steps ({self.safety.max_total_steps}) exceeded"}

        # Check execution time
        start_time = self._start_times.get(task_id, time.time())
        elapsed = time.time() - start_time
        if elapsed > self.safety.max_execution_seconds:
            return {"allowed": False, "reason": f"Max execution time ({self.safety.max_execution_seconds}s) exceeded"}

        return {"allowed": True, "reason": ""}

    def _get_lessons(self, goal: str) -> List[Lesson]:
        """Retrieve relevant lessons from the learning subsystem."""
        if self.lesson_provider is None:
            return []
        try:
            return self.lesson_provider(goal)
        except Exception as e:
            logger.warning("Failed to retrieve lessons: %s", e)
            return []

    def _is_same_failure_recurring(self, task_obs: TaskObservation) -> bool:
        """Check if the same failure is occurring repeatedly."""
        for step_obs in task_obs.step_observations:
            if self.failure_memory.should_stop(step_obs):
                return True
        return False

    def _emit_event(self, signal: str, args: Dict[str, Any]) -> None:
        """Emit an event to the frontend (e.g., Stitch GUI)."""
        if self.event_callback:
            try:
                self.event_callback(signal, args)
            except Exception as e:
                logger.warning("Event callback failed for %s: %s", signal, e)

    def get_status(self) -> Dict[str, Any]:
        """Get the status of all active closed-loop tasks."""
        return {
            "active_tasks": list(self._active_task_ids),
            "replan_counts": dict(self._replan_count),
            "total_steps": dict(self._total_steps),
            "safety_config": self.safety.to_dict(),
            "failure_memory": self.failure_memory.get_failure_summary(),
        }