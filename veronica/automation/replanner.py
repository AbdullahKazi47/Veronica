"""
Replanning logic: generates a new plan for remaining work after a failure.

The replanner preserves the original goal, completed steps, and observations,
then generates a fresh plan for the remaining work. It uses the learning
subsystem's retrieved experience as advisory context (never as instructions).

Key principle: The replanner NEVER mutates the original plan. It creates a new
plan for the remaining goal, preserving completed work as history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from veronica.automation.diagnosis import (
    DiagnosisConfidence,
    RecoveryAction,
    StepDiagnosis,
    TaskDiagnosis,
)
from veronica.automation.observation import StepObservation, TaskObservation
from veronica.automation.task_executor import MultiStepTask, TaskStep, TASK_STATUS
from veronica.learning.lesson import Lesson
from veronica.llm.base import ChatMessage, LLMProvider
from veronica.logging_setup import get_logger

logger = get_logger("automation.replanner")


@dataclass
class ReplanResult:
    """Result of a replanning attempt."""
    success: bool
    new_steps: List[Dict[str, Any]]
    preserved_steps: List[Dict[str, Any]]  # Steps from original plan that completed
    goal_remaining: str
    recovery_actions_applied: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    requires_human: bool = False
    requires_permission: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "new_steps": self.new_steps,
            "preserved_steps": self.preserved_steps,
            "goal_remaining": self.goal_remaining,
            "recovery_actions_applied": self.recovery_actions_applied,
            "notes": self.notes,
            "requires_human": self.requires_human,
            "requires_permission": self.requires_permission,
        }


class Replanner:
    """Generates a new plan for remaining work after a task failure.

    The replanner:
    1. Preserves completed steps as context
    2. Uses the task diagnosis to understand what went wrong
    3. Retrieves relevant experience from the learning subsystem
    4. Generates a fresh plan for the remaining goal

    The replanner NEVER executes tools. It only produces a plan (list of step
    dicts) that must pass through the existing permission system.
    """

    REPLANNING_PROMPT = (
        "You are replanning a failed multi-step task. The original goal was: {goal}\n\n"
        "COMPLETED STEPS (for context — do NOT re-execute these):\n{completed_text}\n\n"
        "FAILED STEP(s):\n{failed_text}\n\n"
        "DIAGNOSIS:\n{diagnosis_text}\n\n"
        "{learning_text}\n\n"
        "Generate a NEW plan to achieve the remaining goal. Each step must be an object "
        'with exactly: "description" (what to do), "tool" (tool name, or empty if none), '
        '"args" (an object, or {{}} if none).\n\n'
        "IMPORTANT:\n"
        "- Do NOT include steps for already-completed work\n"
        "- Use different tools or approaches than what failed\n"
        "- If the failure was a permission issue, plan for that\n"
        "- Keep the plan minimal — only steps needed for the remaining goal\n"
        "- Return ONLY a JSON array, no other text"
    )

    def __init__(self, llm: Optional[LLMProvider] = None):
        self.llm = llm

    def replan(
        self,
        task: MultiStepTask,
        diagnosis: TaskDiagnosis,
        lessons: List[Lesson] = None,
        llm: Optional[LLMProvider] = None,
    ) -> ReplanResult:
        """Generate a new plan for the remaining goal.

        This is the main entry point for replanning after a task failure.
        """
        lessons = lessons or []
        provider = llm or self.llm

        # Extract information from diagnosis
        preserved_indices = set(diagnosis.preserved_step_indices)
        preserved_steps = []
        new_steps = []
        recovery_applied = []
        notes = []
        requires_human = False
        requires_permission = False

        # Check if human intervention is required
        if diagnosis.recommended_approach == "human_intervention":
            requires_human = True
            notes.append("Diagnosis recommends human intervention.")
            return ReplanResult(
                success=False,
                new_steps=[],
                preserved_steps=[],
                goal_remaining=diagnosis.remaining_goal,
                notes=notes,
                requires_human=True,
                requires_permission=True,
            )

        # Collect preserved steps (completed successfully)
        for i, step in enumerate(task.steps):
            if i in preserved_indices:
                preserved_steps.append({
                    "description": step.description,
                    "tool": step.tool_name,
                    "args": step.args,
                    "status": "completed",
                    "step_index": i,
                })

        # Apply recovery actions from diagnosis
        for step_diag in diagnosis.step_diagnoses:
            for action in step_diag.recovery_actions:
                if action.action_type == "human_intervention":
                    requires_human = True
                    requires_permission = action.requires_permission
                    notes.append(f"Step {step_diag.step_index + 1}: {action.description}")

                elif action.action_type == "step_insert":
                    # Insert a new step before the failed step
                    new_step = action.details.get("new_step", {})
                    if new_step:
                        new_steps.append(new_step)
                        recovery_applied.append(action.to_dict())

                elif action.action_type == "tool_substitute":
                    # Replace the failed tool with an alternative
                    new_step = {
                        "description": f"Retry with alternative: {step_diag.observation.expected.description}",
                        "tool": action.details.get("substitute_tool", ""),
                        "args": step_diag.observation.expected.args,
                    }
                    if new_step["tool"]:
                        new_steps.append(new_step)
                        recovery_applied.append(action.to_dict())

                elif action.action_type == "arg_change":
                    # Fix arguments (may need LLM to determine correct args)
                    notes.append(f"Step {step_diag.step_index + 1}: {action.description}")

                elif action.action_type == "retry":
                    # Retry will be handled by the executor, not replanner
                    notes.append(f"Step {step_diag.step_index + 1}: Retry recommended")

                elif action.action_type == "skip":
                    # Skip the failed step
                    notes.append(f"Step {step_diag.step_index + 1}: Step skipped")

        # If we need human intervention, return early
        if requires_human:
            return ReplanResult(
                success=False,
                new_steps=new_steps,
                preserved_steps=preserved_steps,
                goal_remaining=diagnosis.remaining_goal,
                recovery_actions_applied=recovery_applied,
                notes=notes,
                requires_human=True,
                requires_permission=requires_permission,
            )

        # If we have an LLM, use it to generate a fresh plan for remaining work
        if provider and not requires_human:
            llm_plan = self._llm_replan(
                task=task,
                diagnosis=diagnosis,
                lessons=lessons,
                llm=provider,
            )
            if llm_plan:
                new_steps = llm_plan
                notes.append(f"LLM generated {len(new_steps)} new steps for remaining work.")
            else:
                notes.append("LLM planning failed; using heuristic recovery plan.")

        # If no new steps were generated and no LLM, use heuristic plan
        if not new_steps and not requires_human:
            new_steps = self._heuristic_replan(task, diagnosis)
            notes.append(f"Heuristic generated {len(new_steps)} new steps.")

        return ReplanResult(
            success=len(new_steps) > 0 or len(preserved_steps) > 0,
            new_steps=new_steps,
            preserved_steps=preserved_steps,
            goal_remaining=diagnosis.remaining_goal,
            recovery_actions_applied=recovery_applied,
            notes=notes,
            requires_human=requires_human,
            requires_permission=requires_permission,
        )

    def _llm_replan(
        self,
        task: MultiStepTask,
        diagnosis: TaskDiagnosis,
        lessons: List[Lesson],
        llm: LLMProvider,
    ) -> Optional[List[Dict[str, Any]]]:
        """Use the LLM to generate a fresh plan for remaining work."""
        try:
            # Build completed steps text
            completed_lines = []
            for i, step in enumerate(task.steps):
                if i in diagnosis.preserved_step_indices:
                    completed_lines.append(f"  ✓ Step {i+1}: {step.description} ({step.tool_name})")
            completed_text = "\n".join(completed_lines) if completed_lines else "  (none)"

            # Build failed steps text
            failed_lines = []
            for step_diag in diagnosis.step_diagnoses:
                failed_lines.append(
                    f"  ✗ Step {step_diag.step_index + 1}: {step_diag.observation.expected.description} "
                    f"({step_diag.observation.expected.tool_name}) — {step_diag.root_cause}"
                )
            failed_text = "\n".join(failed_lines) if failed_lines else "  (unknown)"

            # Build diagnosis text
            diagnosis_lines = [
                f"  Approach: {diagnosis.recommended_approach}",
                f"  Confidence: {diagnosis.overall_confidence.value}",
            ]
            for note in diagnosis.notes:
                diagnosis_lines.append(f"  • {note}")
            diagnosis_text = "\n".join(diagnosis_lines)

            # Build learning context
            learning_lines = []
            for lesson in lessons[:3]:  # Top 3 lessons
                learning_lines.append(f"  • {lesson.strategy}: {lesson.reason} (confidence: {lesson.confidence:.2f})")
            learning_text = "RELEVANT EXPERIENCE (advisory):\n" + "\n".join(learning_lines) if learning_lines else ""

            prompt = self.REPLANNING_PROMPT.format(
                goal=task.goal,
                completed_text=completed_text,
                failed_text=failed_text,
                diagnosis_text=diagnosis_text,
                learning_text=learning_text,
            )

            response = llm.chat(
                messages=[
                    ChatMessage(role="system", content="You are replanning a failed task."),
                    ChatMessage(role="user", content=prompt),
                ],
                temperature=0.3,
            )

            text = response.content or ""
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end <= start:
                return None

            raw_steps = json.loads(text[start:end])
            steps = []
            for raw in raw_steps:
                steps.append({
                    "description": raw.get("description", ""),
                    "tool": raw.get("tool", ""),
                    "args": raw.get("args", {}),
                })
            return steps if steps else None

        except Exception as e:
            logger.warning("LLM replanning failed: %s", e)
            return None

    def _heuristic_replan(
        self,
        task: MultiStepTask,
        diagnosis: TaskDiagnosis,
    ) -> List[Dict[str, Any]]:
        """Generate a heuristic plan when LLM is unavailable."""
        new_steps = []

        for step_diag in diagnosis.step_diagnoses:
            # Get the first recovery action with type tool_substitute
            for action in step_diag.recovery_actions:
                if action.action_type == "tool_substitute":
                    new_steps.append({
                        "description": f"Retry with alternative: {step_diag.observation.expected.description}",
                        "tool": action.details.get("substitute_tool", ""),
                        "args": step_diag.observation.expected.args,
                    })
                    break
                elif action.action_type == "step_insert":
                    new_step = action.details.get("new_step", {})
                    if new_step:
                        new_steps.append(new_step)
                    break

        # If no specific recovery, add a generic re-attempt step
        if not new_steps and diagnosis.step_diagnoses:
            failed_diag = diagnosis.step_diagnoses[0]
            new_steps.append({
                "description": f"Re-attempt: {failed_diag.observation.expected.description}",
                "tool": failed_diag.observation.expected.tool_name,
                "args": failed_diag.observation.expected.args,
            })

        return new_steps

    def create_replan_task(
        self,
        original_task: MultiStepTask,
        replan_result: ReplanResult,
    ) -> Optional[MultiStepTask]:
        """Create a new task from a replan result.

        The new task preserves completed steps as context and adds new steps
        for remaining work.
        """
        if not replan_result.success or not replan_result.new_steps:
            return None

        # Build step list: preserved steps (completed) + new steps (pending)
        all_steps = []

        # Add preserved steps as already-completed
        for preserved in replan_result.preserved_steps:
            all_steps.append({
                "description": preserved["description"],
                "tool": preserved["tool"],
                "args": preserved["args"],
                "status": "completed",
            })

        # Add new steps as pending
        for new_step in replan_result.new_steps:
            all_steps.append({
                "description": new_step["description"],
                "tool": new_step.get("tool", ""),
                "args": new_step.get("args", {}),
            })

        # Create new task with prefixed goal to indicate replanning
        new_goal = f"[Replan of {original_task.task_id}] {original_task.goal}"
        task_steps = []
        for s in all_steps:
            task_steps.append(TaskStep(
                description=s["description"],
                tool_name=s.get("tool", ""),
                args=s.get("args", {}),
            ))

        new_task = MultiStepTask(
            task_id="",  # Will be assigned by executor
            goal=new_goal,
            steps=task_steps,
        )

        # Mark preserved steps as completed
        for i, step in enumerate(new_task.steps):
            if i < len(replan_result.preserved_steps):
                step.status = TASK_STATUS["COMPLETED"]
                step.verified = True
                step.verification_note = "Preserved from previous attempt"

        # Copy adaptation metadata
        new_task._adaptation_meta = {
            "replan_from": original_task.task_id,
            "preserved_steps": len(replan_result.preserved_steps),
            "recovery_actions": replan_result.recovery_actions_applied,
        }

        logger.info(
            "Created replan task: original=%s preserved=%d new=%d",
            original_task.task_id, len(replan_result.preserved_steps), len(replan_result.new_steps)
        )

        return new_task