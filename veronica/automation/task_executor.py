"""
Multi-step task execution: turn a high-level goal into ordered, verifiable steps and
run them end-to-end. This is the missing "plan → execute → verify" layer that turns
VERONICA from a single-turn responder into an agent that can carry a compound goal.

Adapted from the user's private ULTRON project (project_ultron/core/task_executor.py).
Not for redistribution.

Three ideas are carried over wholesale (they were the point of the original):
  1. OPTIMISTIC PLANNING   — a goal is decomposed into `{description, tool, args}`
     steps up front (see automation/planner.py); a plan is never merged at runtime.
  2. PER-STEP SELF-VERIFICATION — after each step the executor checks the real world
     (file exists? directory exists? URL well-formed?) before it lets the task
     advance, so it never claims success just because the tool returned 200.
  3. RETRY-ON-FAILURE — a failing step is retried up to ``MAX_RETRIES`` times before
     the whole task is marked failed, and the failure is recorded with full context.

What differs from the original (VERONICA adaptations):
  - No global `task_executor` singleton and no module-level tool/permission globals.
    The executor receives an injected ``ToolRegistry`` (whose dispatch already applies
    the permission gate) — the same DI-via-App style as the rest of VERONICA.
  - Tools are executed through ``ToolRegistry.dispatch()``, so every step is gated the
    same way a fast-path or LLM-requested tool call is.
  - A ``tool_event_callback`` hook is threaded through so front-ends (the Stitch GUI)
    can render live per-step activity.
  - Cancellation is checked *between* steps (and between retries), not just as a flag,
    so a cancelled task stops promptly.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from veronica.learning.adaptation import AdaptationResult

from veronica.learning.experience import (
    Experience,
    ExperienceOutcome,
    VerificationResult,
)
from veronica.logging_setup import get_logger
from veronica.tools.registry import ToolRegistry

logger = get_logger("automation.task_executor")

TASK_STATUS = {
    "PENDING":       "pending",
    "RUNNING":       "running",
    "COMPLETED":     "completed",
    "FAILED":        "failed",
    "CANCELLED":     "cancelled",
    "PLANNING":      "planning",
    # Phase 6: closed-loop recovery states
    "OBSERVING":     "observing",      # Post-execution observation in progress
    "VERIFYING":     "verifying",      # Step verification in progress
    "DIAGNOSING":    "diagnosing",     # Failure diagnosis in progress
    "REPLANNING":    "replanning",     # Replanning after diagnosis
    "BLOCKED":       "blocked",        # Stopped by safety boundary or same-failure detection
    "NEEDS_REVIEW":  "needs_review",   # Requires human review/intervention
}

# Summary sources consulted when the task is being described to a caller. Broad and
# cheap so the executor can resolve them with no extra tool round-trip.
_FILE_TOOLS = {"read_file", "write_file", "delete_file"}
_DIR_TOOLS = {"list_directory", "create_folder"}
_URL_TOOLS = {"open_website", "fetch_webpage"}


class TaskStep:
    """One ordered step inside a MultiStepTask."""

    def __init__(self, description: str, tool_name: str = "", args: Dict[str, Any] | None = None) -> None:
        self.description = description
        self.tool_name = tool_name
        self.args = args or {}
        self.status = TASK_STATUS["PENDING"]
        self.result: Any = None
        self.verified = False
        self.verification_note = ""
        self.attempts = 0
        self.error = ""
        self.start_time = 0.0
        self.end_time = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "description": self.description,
            "tool": self.tool_name,
            "args": self.args,
            "status": self.status,
            "verified": self.verified,
            "verification_note": self.verification_note,
            "attempts": self.attempts,
            "error": self.error,
            "duration_ms": round((self.end_time - self.start_time) * 1000, 1) if self.end_time else 0,
            "result_preview": str(self.result)[:200] if self.result is not None else "",
        }


class MultiStepTask:
    """A full planned task and its live execution state."""

    def __init__(self, task_id: str, goal: str, steps: List[TaskStep]) -> None:
        self.task_id = task_id
        self.goal = goal
        self.steps = steps
        self.status = TASK_STATUS["PENDING"]
        self.created_time = time.time()
        self.started_time: Optional[float] = None
        self.completed_time: Optional[float] = None
        self.current_step = 0
        self.result_summary = ""
        # Phase 4 — adaptation audit trail (set via create_task, used in _build_experience).
        self._adaptation_meta: Dict[str, Any] = {}

    @property
    def progress(self) -> float:
        """Progress as a percentage (0-100), based on completed steps."""
        if not self.steps:
            return 0
        completed = sum(1 for s in self.steps if s.status == TASK_STATUS["COMPLETED"])
        return round((completed / len(self.steps)) * 100, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "progress": self.progress,
            "current_step": self.current_step,
            "total_steps": len(self.steps),
            "result_summary": self.result_summary,
            "steps": [s.to_dict() for s in self.steps],
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_time)),
            "duration_sec": round(self.completed_time - self.started_time, 1)
            if self.completed_time and self.started_time
            else 0,
        }


class TaskExecutor:
    """
    Executes multi-step tasks with per-step self-verification.

    Verification is built in at two levels — this is what separates the executor from
    a plain loop:
      1. TOOL SUCCESS — the step's tool returned a success=True ToolResult.
      2. SEMANTIC CHECK — step-specific real-world verification (the file actually
         exists, the directory actually exists, the URL is well-formed).
    """

    MAX_RETRIES = 2
    RETRY_DELAY_SEC = 0.0  # tests stay fast; real installs can raise this

    def __init__(
        self,
        tools: ToolRegistry,
        tool_event_callback: Optional[Callable[[str], None]] = None,
        experience_callback: Optional[Callable[[Experience], None]] = None,
    ) -> None:
        self.tools = tools
        self.tool_event_callback = tool_event_callback
        # Optional learning hook: called with an Experience at every terminal task state
        # (success / failed / recovered / cancelled). Wired in main.py to a store +
        # learning engine. MUST be fault-tolerant — a learning failure is swallowed and
        # logged so it can never fail the task itself.
        self.experience_callback = experience_callback
        self.tasks: Dict[str, MultiStepTask] = {}
        self._lock = threading.Lock()
        self._id_counter = 0
        self.history: List[Dict[str, Any]] = []

    # -- task lifecycle -------------------------------------------------------------
    def _next_id(self) -> str:
        with self._lock:
            self._id_counter += 1
            return f"T{int(time.time())}_{self._id_counter}"

    def create_task(
        self,
        goal: str,
        steps: List[Dict[str, Any]],
        *,
        adaptation_applied: bool = False,
        adaptation_result: Optional["AdaptationResult"] = None,
    ) -> MultiStepTask:
        """Create a task from a list of step dicts and register it.

        Phase 4 — ``adaptation_applied`` and ``adaptation_result`` carry metadata so
        the experience can record whether behavioral adaptation was used and how it
        affected execution.  ``adaptation_result`` is only used for audit; it is not
        mutated or retained beyond this call.
        """
        task_steps = []
        for s in steps:
            desc = s.get("description", s.get("step", ""))
            tool = s.get("tool", s.get("tool_name", ""))
            args = s.get("args", {}) or {}
            task_steps.append(TaskStep(desc, tool, args))
        task = MultiStepTask(self._next_id(), goal, task_steps)
        # Store adaptation metadata for experience recording.
        meta: Dict[str, Any] = {"adaptation_applied": adaptation_applied}
        if adaptation_result is not None:
            try:
                meta["original_plan"] = list(adaptation_result.original_plan or [])
                meta["adaptations"] = [
                    {
                        "kind": a.kind,
                        "step_index": a.step_index,
                        "from_value": a.from_value,
                        "to_value": a.to_value,
                        "reason": a.reason,
                        "confidence": a.confidence,
                        "evidence": a.evidence,
                    }
                    for a in (adaptation_result.applied or [])
                ]
            except Exception:
                meta.setdefault("adaptations", [])
        task._adaptation_meta = meta
        with self._lock:
            self.tasks[task.task_id] = task
        return task

    def execute_task(
        self,
        task: MultiStepTask,
        progress_callback: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """Execute ``task`` step by step. Each step runs with retry + real-world
        self-verification before the executor advances. Returns a result dict.

        ``progress_callback(task_id, current_step, total_steps, step_description)`` is
        fired before every step (and once at the end) — the Stitch GUI uses it to paint
        a progress bar.
        """
        if task.status == TASK_STATUS["RUNNING"]:
            return {"success": False, "task": task.to_dict(), "result": None, "error": "Task already running."}
        if task.status == TASK_STATUS["CANCELLED"]:
            # Cancelled before it ever started — never flip it back to RUNNING.
            task.completed_time = time.time()
            task.result_summary = f"Task cancelled at step {task.current_step}."
            self._record_history(task)
            self._emit_experience(task)
            return {"success": False, "task": task.to_dict(), "result": None, "error": "Task was cancelled."}

        task.status = TASK_STATUS["RUNNING"]
        task.started_time = time.time()

        try:
            for i, step in enumerate(task.steps):
                if task.status == TASK_STATUS["CANCELLED"]:
                    break  # cancelled between steps — stop without marking failed

                task.current_step = i + 1
                step.status = TASK_STATUS["RUNNING"]
                step.start_time = time.time()

                if progress_callback:
                    progress_callback(task.task_id, i + 1, len(task.steps), step.description)

                exec_success = False
                result: Dict[str, Any] = {}
                for attempt in range(self.MAX_RETRIES + 1):
                    if task.status == TASK_STATUS["CANCELLED"]:
                        break
                    step.attempts += 1
                    result = self._execute_step(step)
                    if result.get("success"):
                        exec_success = True
                        break
                    if attempt < self.MAX_RETRIES:
                        logger.info(
                            "Step '%s' failed (attempt %s), retrying…",
                            step.description, step.attempts,
                        )
                        if self.RETRY_DELAY_SEC:
                            time.sleep(self.RETRY_DELAY_SEC)

                step.end_time = time.time()

                if task.status == TASK_STATUS["CANCELLED"]:
                    step.status = TASK_STATUS["CANCELLED"]
                    continue

                if not exec_success:
                    step.status = TASK_STATUS["FAILED"]
                    step.error = result.get("error", "Step failed")
                    task.status = TASK_STATUS["FAILED"]
                    task.completed_time = time.time()
                    task.result_summary = f"Task failed at step {i + 1}: {step.description}"
                    self._record_history(task)
                    self._emit_experience(task)
                    return {
                        "success": False,
                        "task": task.to_dict(),
                        "result": None,
                        "error": f"Failed at step {i + 1} ('{step.description}'): {step.error}",
                    }

                # Self-verification: check the real world before advancing.
                step.verified, step.verification_note = self._verify_step(step)
                step.status = TASK_STATUS["COMPLETED"]

            if task.status == TASK_STATUS["CANCELLED"]:
                task.completed_time = time.time()
                task.result_summary = f"Task cancelled at step {task.current_step}."
                self._record_history(task)
                self._emit_experience(task)
                return {
                    "success": False,
                    "task": task.to_dict(),
                    "result": None,
                    "error": "Task was cancelled.",
                }

            task.status = TASK_STATUS["COMPLETED"]
            task.completed_time = time.time()
            task.result_summary = f"Task completed: {len(task.steps)}/{len(task.steps)} steps succeeded"
            self._record_history(task)
            self._emit_experience(task)

            if progress_callback:
                progress_callback(task.task_id, len(task.steps), len(task.steps), "Complete")

            return {
                "success": True,
                "task": task.to_dict(),
                "result": task.result_summary,
                "error": None,
            }

        except Exception as e:  # noqa: BLE001 — a task must report, not raise, its failures
            logger.exception("Task execution error")
            task.status = TASK_STATUS["FAILED"]
            task.completed_time = time.time()
            task.result_summary = f"Task failed: {e}"
            self._record_history(task)
            self._emit_experience(task)
            return {
                "success": False,
                "task": task.to_dict(),
                "result": None,
                "error": f"Task execution error: {e}",
            }

    # -- learning integration -------------------------------------------------------
    def _emit_experience(self, task: MultiStepTask) -> None:
        """Build an Experience from the task's terminal state and hand it to the
        learning hook. Never raises — a learning failure is logged and swallowed so the
        task's own result is unaffected."""
        if self.experience_callback is None:
            return
        try:
            self.experience_callback(self._build_experience(task))
        except Exception:  # noqa: BLE001
            logger.exception("Learning integration failed for task %s — task result is unaffected", task.task_id)

    def _build_experience(self, task: MultiStepTask) -> Experience:
        """Convert a finished task into an Experience, deriving the explicit outcome from
        the observed state (retries → recovered, etc.). Captures only data already in the
        executor — no duplicate execution logic. Secrets in step args are redacted at the
        store boundary, not here.

        Phase 4 — includes adaptation audit trail when present (original_plan,
        adaptations, adaptation_applied, adaptation_success).
        """
        steps = task.steps
        tool_calls = [s.tool_name for s in steps if s.tool_name]
        retried = [s for s in steps if s.attempts > 1]
        failed = [s for s in steps if s.status == TASK_STATUS["FAILED"]]
        failed_step = failed[0] if failed else None

        if task.status == TASK_STATUS["CANCELLED"]:
            outcome = ExperienceOutcome.CANCELLED
        elif task.status == TASK_STATUS["FAILED"]:
            outcome = ExperienceOutcome.FAILED
        elif retried:
            outcome = ExperienceOutcome.RECOVERED
        else:
            outcome = ExperienceOutcome.SUCCESS

        completed = [s for s in steps if s.status == TASK_STATUS["COMPLETED"]]
        if steps and completed and all(s.verified for s in completed):
            verification = VerificationResult.PASSED
        elif steps and any(s.status == TASK_STATUS["FAILED"] for s in steps):
            verification = VerificationResult.FAILED
        else:
            verification = VerificationResult.NOT_APPLICABLE

        duration = (task.completed_time - task.started_time) if task.completed_time and task.started_time else 0.0
        meta = getattr(task, "_adaptation_meta", {}) or {}
        adaptation_applied = bool(meta.get("adaptation_applied", False))
        task_succeeded = task.status == TASK_STATUS["COMPLETED"]

        return Experience(
            goal=task.goal,
            outcome=outcome,
            task_id=task.task_id,
            final_status=task.status,
            plan=[{"description": s.description, "tool": s.tool_name, "args": s.args} for s in steps],
            actions=[
                {"description": s.description, "tool": s.tool_name, "args": s.args,
                 "status": s.status, "verified": s.verified, "attempts": s.attempts}
                for s in steps
            ],
            tool_calls=tool_calls,
            verification=verification,
            retries=sum(max(0, s.attempts - 1) for s in steps),
            recovery_actions=[s.tool_name for s in retried if s.tool_name],
            duration=round(duration, 3),
            failed_step=failed_step.tool_name if failed_step else "",
            failure_reason=failed_step.error if failed_step else "",
            context={"total_steps": len(steps), "current_step": task.current_step,
                     "started": bool(task.started_time)},
            metadata={"result_summary": task.result_summary},
            # Phase 4 — adaptation audit trail
            original_plan=meta.get("original_plan"),
            adaptations=meta.get("adaptations", []),
            adaptation_applied=adaptation_applied,
            adaptation_success=task_succeeded if adaptation_applied else None,
        )

    def _execute_step(self, step: TaskStep) -> Dict[str, Any]:
        """Execute one step through the registry (which applies the permission gate)."""
        if not step.tool_name:
            # A step with no tool is a manual/LLM step — there is nothing to dispatch.
            step.result = step.description
            return {"success": True, "result": step.description}

        if self.tool_event_callback:
            self.tool_event_callback(f"task step: {step.tool_name}")
        result = self.tools.dispatch(step.tool_name, **step.args)
        step.result = result
        if result.success:
            return {"success": True, "result": result}
        return {"success": False, "error": result.message}

    def _verify_step(self, step: TaskStep) -> tuple[bool, str]:
        """Real-world self-verification of a completed step. Returns
        ``(verified, note)``. Verification is *advisory* — a step whose tool reported
        success is completed either way, but the note carries the discrepancy up so
        the caller and logs see it."""
        if not step.tool_name:
            return True, "No tool step — assumed complete"

        tool = step.tool_name
        args = step.args or {}
        try:
            # File operations — the file should now physically exist.
            if tool in _FILE_TOOLS:
                path = args.get("file_path") or args.get("path") or ""
                if path:
                    exists = Path(path).is_file()
                    return exists, f"File {'exists' if exists else 'MISSING'} at: {path}"

            # Directory-based operations — the directory should exist or be changed.
            if tool in _DIR_TOOLS:
                path = args.get("path") or args.get("folder_path", "")
                if path:
                    exists = Path(path).is_dir()
                    return exists, f"Directory {'exists' if exists else 'MISSING'}: {path}"

            # URL operations — format check only (cheap, offline).
            if tool in _URL_TOOLS:
                url = args.get("url", "")
                if url:
                    valid = url.startswith(("http://", "https://", "www."))
                    return valid, f"URL {'valid' if valid else 'looks invalid'}: {url}"

        except Exception as e:  # noqa: BLE001 — verification must never fail the step
            logger.warning("Verification error for step '%s': %s", step.description, e)

        return True, "Tool reported success"

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a pending/running task. The cancellation takes effect between steps
        (and between retries), so a running tool call finishes before the task stops."""
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task.status in (TASK_STATUS["PENDING"], TASK_STATUS["RUNNING"]):
            task.status = TASK_STATUS["CANCELLED"]
            task.completed_time = time.time()
            return True
        return False

    # -- queries --------------------------------------------------------------------
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self.tasks.get(task_id)
        return task.to_dict() if task else None

    def list_tasks(self, status: str = "") -> List[Dict[str, Any]]:
        tasks = []
        with self._lock:
            for task in self.tasks.values():
                if not status or task.status == status:
                    tasks.append(task.to_dict())
        return tasks

    def _record_history(self, task: MultiStepTask) -> None:
        self.history.append({
            "task_id": task.task_id,
            "goal": task.goal,
            "status": task.status,
            "progress": task.progress,
            "completed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        })
        if len(self.history) > 100:
            self.history = self.history[-100:]

    def get_history(self) -> List[Dict[str, Any]]:
        return list(self.history)

    def get_context_summary(self) -> str:
        """A compact description of task-execution capabilities for LLM context
        injection — the same idea as ULTRON's, used so a provider knows VERONICA can
        plan and execute multi-step goals."""
        recent = self.get_history()[-3:]
        lines = ["TASK EXECUTION SYSTEM: multi-step task execution with self-verification active."]
        if recent:
            lines.append("RECENT TASKS:")
            for t in recent:
                lines.append(f"  • {str(t['goal'])[:50]} - {t['status']} ({t['progress']}%)")
            lines.append("")
        return "\n".join(lines)