"""
Behavioral adaptation — transforms an execution plan based on validated, high-confidence
learned preferences.  NEVER executes tools; only recommends/transforms plan structures.

This is the Phase 4 controlled-adaptation layer.  It is fully reversible (copies steps,
never mutates originals) and never operates on its own — it is called by the orchestrator
with an explicit user plan and produced advisory modifications.

Architectural boundary (mandated by user):
  - The adapter NEVER calls ToolRegistry.dispatch(), never performs I/O, never calls LLMs.
  - All actual execution continues through TaskExecutor → ToolRegistry.dispatch() →
    permission/safety gate → tool.
  - If adaptation fails: the original plan is preserved and executed unchanged.

Priority order (explicit > context > planner > learned > weak):
  1. Safety-sensitive tools → never adapted (blocked by policy).
  2. Explicit user instruction (goal mentions tool by name) → preserved, not replaced.
  3. Non-placeholder step args (current context) → preserved, not overwritten.
  4. Planner's fresh plan → used as base.
  5. Learned preference (strong/advisory policy only) → recommended transformation.
  6. Weak evidence (< 0.80) → ignored entirely.

Adaptation types supported:
  - tool_preference         replace step tool with preferred strategy (same group)
  - argument_preference     fill empty args based on past preference
  - recovery_preference     prefer recovery strategy after failure
  - user_preference          honor user correction preference

Confidence thresholds (from policy):
  - confidence < 0.80         → NONE (no adaptation)
  - 0.80 ≤ c < 0.90, evidence ≥ 1  → ADVISORY (informational, not applied by default)
  - confidence ≥ 0.90, evidence ≥ 3 → STRONG (eligible for transformation)
  - user_correction + c ≥ 0.90 → STRONG
  - user_correction + c ≥ 0.80 → ADVISORY

All results are reversible: the adapter creates new step dicts and never mutates the
original plan.  If anything raises, the caller receives the unmodified original.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from veronica.learning.lesson import Lesson
from veronica.learning.policy import (
    BehaviorPolicy,
    PolicyDecision,
    PolicyThresholds,
    PolicyMode,
    SAFETY_SENSITIVE_TOOLS,
)
from veronica.logging_setup import get_logger

logger = get_logger("learning.adaptation")


# --- data model ---------------------------------------------------------------

@dataclass
class AppliedAdaptation:
    """A single transformation applied (or considered) for a step."""
    kind: str                      # "tool_preference" | "argument_preference" | ...
    step_index: int
    from_value: Any                # original value (tool name / arg value / ...)
    to_value: Any                  # new value (if applied) or None
    reason: str                    # human-readable justification
    confidence: float
    evidence: int
    source_lesson_ids: list[str] = field(default_factory=list)

@dataclass
class RejectedAdaptation:
    kind: str
    step_index: int
    from_value: Any
    to_value: Optional[Any] = None
    reason: str = ""
    blocked_by: str = ""            # e.g. "safety", "explicit_context", "weak_evidence"

@dataclass
class AdaptationResult:
    """Outcome of adapting a plan."""
    original_plan: list[dict]
    adapted_plan: list[dict]         # new list; never the same object as original
    applied: list[AppliedAdaptation]
    rejected: list[RejectedAdaptation]
    considered: list[dict]            # lessons evaluated (for audit/debug)
    decision_summary: str             # one-line explanation
    adaptation_applied: bool = False  # True if at least one transformation was applied

    # Reversibility: both plans preserved; original is untouched.
    def recover_original(self) -> list[dict]:
        """Return a deep-copied original plan (guaranteed separate objects)."""
        import copy
        return copy.deepcopy(self.original_plan)


# --- adapter ------------------------------------------------------------------

class BehavioralAdapter:
    """Transforms a proposed plan based on validated lessons — never executes."""

    # Evidence older than this is treated as stale: it still informs planning but is
    # never applied as a strong behavioral change (demoted to advisory).
    STALE_DAYS = 60.0

    # Tool grouping used to decide which tools are substitutable for preference.
    # These are advisory categories; replacements only happen within a group.
    TOOL_GROUPS: dict[str, list[str]] = {
        "file_location": ["find_file", "search_workspace", "locate_file"],
        "directory_listing": ["list_directory", "list_folder", "show_files"],
        "editor": ["read_file", "open_file", "view_text"],
        "browser": ["open_website", "open_url", "launch_browser"],
        "web_search": ["web_search", "search_web", "online_search"],
        # Safety-sensitive tools are excluded from all groups; handled by policy.
    }

    def __init__(
        self,
        thresholds: Optional[PolicyThresholds] = None,
        protected_tools: Optional[set[str]] = None,
        tool_groups: Optional[dict[str, list[str]]] = None,
    ) -> None:
        self.policy = BehaviorPolicy(thresholds=thresholds)
        self.protected_tools = protected_tools or set(SAFETY_SENSITIVE_TOOLS)
        self.protected_tools.update(SAFETY_SENSITIVE_TOOLS)
        self.tool_groups = dict(tool_groups) if tool_groups else dict(self.TOOL_GROUPS)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def adapt(
        self,
        plan: list[dict],
        lessons: list[Lesson],
        *,
        goal: str = "",
        context: Optional[dict] = None,
    ) -> AdaptationResult:
        """Evaluate lessons against *plan* and return an adaptation result.

        The original *plan* is NEVER mutated.  All modifications are made on a deep
        copy.  If any exception occurs, the adapter returns the unmodified original
        and logs the failure so the caller can fall back safely.
        """
        import copy
        context = dict(context or {})
        # Always expose the goal inside the context dict so _try_apply can see it,
        # regardless of whether the caller passed it inline or via context.
        context.setdefault("goal", goal)
        original = copy.deepcopy(plan)
        adapted = copy.deepcopy(plan)

        applied: list[AppliedAdaptation] = []
        rejected: list[RejectedAdaptation] = []
        considered: list[dict] = []

        try:
            for lesson in lessons:
                decision, info = self._evaluate_lesson(lesson, goal=goal, context=context)
                considered.append({
                    "lesson_id": lesson.lesson_id,
                    "strategy": lesson.strategy,
                    "mode": decision.mode,
                    "confidence": decision.confidence,
                    "evidence": decision.evidence,
                    "safety_blocked": decision.safety_blocked,
                    "reason": decision.reason,
                })

                if decision.mode == PolicyMode.NONE:
                    # Nothing to apply; optionally record weak-evidence rejection.
                    if decision.confidence < 0.80:
                        rejected.append(
                            RejectedAdaptation(
                                kind="tool_preference",
                                step_index=-1,   # not tied to a specific step
                                from_value=lesson.strategy,
                                blocked_by="weak_evidence",
                                reason="confidence below 0.80; no adaptation allowed.",
                            )
                        )
                    continue

                # Policy says ADVISORY or STRONG.  Try to apply to applicable step(s).
                for idx, step in enumerate(adapted):
                    result = self._try_apply(
                        step=step,
                        lesson=lesson,
                        decision=decision,
                        step_index=idx,
                        original_plan=original,
                        context=context,
                    )
                    if isinstance(result, AppliedAdaptation):
                        applied.append(result)
                        # Apply to adapted plan (already mutated by _try_apply)
                    elif isinstance(result, RejectedAdaptation):
                        rejected.append(result)
        except Exception:
            logger.exception("Behavioral adaptation failed; discarding adaptations.")
            return AdaptationResult(
                original_plan=original,
                adapted_plan=original,
                applied=applied,
                rejected=rejected,
                considered=considered,
                decision_summary="ADAPTER FAILURE — original preserved",
                adaptation_applied=False,
            )

        # Build explanation string based on applied + rejected.
        summary_parts = []
        if applied:
            summary_parts.append(
                f"Applied {len(applied)} adaptation(s): "
                + ", ".join(a.kind for a in applied)
            )
        else:
            summary_parts.append("No adaptations applied.")
        if any(r.blocked_by == "safety" for r in rejected):
            summary_parts.append("Safety-sensitive operations preserved.")
        decision_summary = "; ".join(summary_parts)

        return AdaptationResult(
            original_plan=original,
            adapted_plan=adapted,
            applied=applied,
            rejected=rejected,
            considered=considered,
            decision_summary=decision_summary,
            adaptation_applied=bool(applied),
        )

    # ------------------------------------------------------------------
    # Lesson evaluation (uses existing policy, no new confidence calc)
    # ------------------------------------------------------------------

    def _evaluate_lesson(
        self,
        lesson: Lesson,
        *,
        goal: str = "",
        context: Optional[dict] = None,
    ) -> tuple[PolicyDecision, dict[str, Any]]:
        """Evaluate a single lesson through the policy gate.

        Returns (PolicyDecision, info_dict).  The info dict is only for audit/debug.
        """
        # Safety-sensitive check: does the lesson's strategy refer to a protected tool?
        strategy = (lesson.strategy or "").strip()
        is_user_correction = getattr(lesson, "is_user_correction", False)
        safety_sensitive = any(
            protected in strategy or strategy == protected
            for protected in self.protected_tools
        )
        # Also block if the goal mentions a protected operation directly.
        goal_lower = goal.lower()
        for protected in self.protected_tools:
            if protected.replace("_", " ") in goal_lower or protected in goal_lower:
                safety_sensitive = True
                break

        decision = self.policy.evaluate(
            lesson,
            safety_sensitive=safety_sensitive,
            is_user_correction=is_user_correction,
        )

        # Staleness cap: old evidence still informs planning, but is never applied
        # blindly as a STRONG behavioral change. Demoted to advisory, reversible.
        if decision.mode == PolicyMode.STRONG and getattr(lesson, "age_days", 0.0) > self.STALE_DAYS:
            decision = PolicyDecision(
                mode=PolicyMode.ADVISORY,
                confidence=decision.confidence,
                evidence=decision.evidence,
                safety_blocked=decision.safety_blocked,
                reason=f"stale evidence ({lesson.age_days:.0f} days old); advisory only",
            )

        return decision, {"lesson_id": lesson.lesson_id, "strategy": strategy}

    # ------------------------------------------------------------------
    # Per-step adaptation (reversible, explicit-context-aware)
    # ------------------------------------------------------------------

    def _try_apply(
        self,
        *,
        step: dict,
        lesson: Lesson,
        decision: PolicyDecision,
        step_index: int,
        original_plan: list[dict],
        context: Optional[dict],
    ) -> AppliedAdaptation | RejectedAdaptation:
        """Attempt to apply *lesson* to a single step.

        Returns either an AppliedAdaptation (mutation already applied to step) or a
        RejectedAdaptation (nothing changed).

        Key rules (in order):
          1. Strong policy only actually changes behavior; advisory is informational.
          2. Safety-sensitive → reject.
          3. Explicit user instruction (goal mentions tool) → reject (preserve).
          4. Step has non-empty, non-placeholder args → don't overwrite with preference.
          5. Only apply to steps whose current tool is substitutable.
        """
        step_tool = (step.get("tool") or "").strip()
        step_args = step.get("args") or {}
        goal = context.get("goal", "") if context else ""

        # Rule 1: policy must allow change, not just advisory, to actually alter.
        if decision.mode != PolicyMode.STRONG:
            # Advisory is recorded as rejected but kept informational.
            return RejectedAdaptation(
                kind="tool_preference",
                step_index=step_index,
                from_value=step_tool,
                to_value=lesson.strategy,
                blocked_by="advisory_only",
                reason=f"Policy mode is '{decision.mode}'; only STRONG changes execution.",
            )

        # Rule 2: never change a safety-sensitive step.
        if step_tool in self.protected_tools:
            return RejectedAdaptation(
                kind="tool_preference",
                step_index=step_index,
                from_value=step_tool,
                to_value=lesson.strategy,
                blocked_by="safety",
                reason=f"Step uses protected tool '{step_tool}'; never adapted.",
            )

        # Rule 3: explicit user instruction overrides learned preference.
        # The orchestrator pre-computes explicit tool mentions in the goal and passes
        # them via context["explicit_tools"]. Also detect phrase-anchored mentions here
        # so the adapter is safe even when called directly without that context.
        explicit_tools = set(context.get("explicit_tools") or []) if context else set()
        if step_tool in explicit_tools:
            return RejectedAdaptation(
                kind="tool_preference",
                step_index=step_index,
                from_value=step_tool,
                blocked_by="explicit_context",
                reason=f"User goal explicitly references '{step_tool}'; learned preference ignored.",
            )
        if goal and step_tool:
            goal_lower = goal.lower()
            for anchor in ("use ", "with ", "run ", "via ", "tool "):
                if f"{anchor}{step_tool}" in goal_lower:
                    return RejectedAdaptation(
                        kind="tool_preference",
                        step_index=step_index,
                        from_value=step_tool,
                        blocked_by="explicit_context",
                        reason=f"User goal explicitly requests '{step_tool}'; learned preference ignored.",
                    )

        # Rule 4: if the step already has concrete non-placeholder args, don't blindly
        # substitute a different tool — current-context specifics win over a learned
        # preference. (An empty/placeholder arg is the opposite: nothing to lose, so a
        # strong preference may fill the approach.)
        explicit_args = {
            k: v for k, v in (step_args or {}).items()
            if v is not None and str(v).strip()
            and str(v).strip().lower() not in ("", "none", "null", "unknown", "auto", "default", "?", "todo")
        }

        # Now apply the adaptation type derived from the lesson strategy.
        kind = self._classify_adaptation_kind(lesson, step)

        if kind == "tool_preference":
            if explicit_args:
                return RejectedAdaptation(
                    kind="tool_preference",
                    step_index=step_index,
                    from_value=step_tool,
                    to_value=lesson.strategy,
                    blocked_by="explicit_context",
                    reason=f"Step has concrete args {list(explicit_args)}; current context overrides learned preference.",
                )
            preferred = lesson.strategy
            # Only replace if preferred is different and is a registered/substitutable tool.
            if preferred == step_tool:
                return RejectedAdaptation(
                    kind="tool_preference",
                    step_index=step_index,
                    from_value=step_tool,
                    blocked_by="same_tool",
                    reason=f"Lesson strategy '{preferred}' is already the step tool.",
                )
            # Check substitutability: preferred must be in same group as step_tool.
            substitutable = False
            for group_tools in self.tool_groups.values():
                if step_tool in group_tools and preferred in group_tools:
                    substitutable = True
                    break
            if not substitutable:
                # Even without exact group match, allow if preferred is a known tool
                # and the lesson is a user correction (strongest evidence).
                if not getattr(lesson, "is_user_correction", False):
                    return RejectedAdaptation(
                        kind="tool_preference",
                        step_index=step_index,
                        from_value=step_tool,
                        to_value=preferred,
                        blocked_by="not_substitutable",
                        reason=f"Preferred '{preferred}' is not in the same tool group as '{step_tool}'.",
                    )

            # Apply: replace tool name in the step (deep-copied already at top).
            step["tool"] = preferred
            return AppliedAdaptation(
                kind="tool_preference",
                step_index=step_index,
                from_value=step_tool,
                to_value=preferred,
                reason=f"Strong confidence ({decision.confidence:.2f}) with evidence {decision.evidence}.",
                confidence=decision.confidence,
                evidence=decision.evidence,
                source_lesson_ids=list(lesson.source_ids or [lesson.lesson_id]),
            )

        if kind == "argument_preference":
            # Fill empty args based on past preference (only when safe).
            if not explicit_args or len(explicit_args) == len(step_args):
                return RejectedAdaptation(
                    kind="argument_preference",
                    step_index=step_index,
                    from_value=dict(step_args),
                    blocked_by="explicit_context",
                    reason="Step has explicit args; not overwritten by preference.",
                )
            # Apply: merge preferred args (from lesson strategy mapping, if any).
            preferred_args = self._derive_preferred_args(lesson, step_args)
            for k, v in preferred_args.items():
                step_args[k] = v
            step["args"] = step_args
            return AppliedAdaptation(
                kind="argument_preference",
                step_index=step_index,
                from_value=dict(original_plan[step_index].get("args", {})),
                to_value=dict(step_args),
                reason=f"Filled missing args from strategy '{lesson.strategy}' (advisory/strong).",
                confidence=decision.confidence,
                evidence=decision.evidence,
                source_lesson_ids=list(lesson.source_ids or [lesson.lesson_id]),
            )

        if kind == "recovery_preference":
            # Prefer a recovery tool only when the current step's tool historically failed.
            # For simplicity: recommend replacement with recovery strategy if present.
            preferred = lesson.strategy
            return RejectedAdaptation(
                kind="recovery_preference",
                step_index=step_index,
                from_value=step_tool,
                blocked_by="not_implemented_detailed",
                reason="Recovery preference not yet fully implemented; preserved for audit.",
            )

        if kind == "user_preference":
            # Explicit user preference: prefer the subject/tool mentioned.
            preferred = lesson.strategy
            if preferred and preferred != step_tool:
                step["tool"] = preferred
                return AppliedAdaptation(
                    kind="user_preference",
                    step_index=step_index,
                    from_value=step_tool,
                    to_value=preferred,
                    reason=f"User correction preference honored (confidence {decision.confidence:.2f}).",
                    confidence=decision.confidence,
                    evidence=decision.evidence,
                    source_lesson_ids=list(lesson.source_ids or [lesson.lesson_id]),
                )
            return RejectedAdaptation(
                kind="user_preference",
                step_index=step_index,
                from_value=step_tool,
                blocked_by="same_as_preference",
                reason="Preference already satisfied.",
            )

        # Fallback: unknown kind.
        return RejectedAdaptation(
            kind="unknown",
            step_index=step_index,
            from_value=step_tool,
            blocked_by="unknown_kind",
            reason=f"Unknown adaptation kind derived from lesson strategy '{lesson.strategy}'.",
        )

    def _classify_adaptation_kind(self, lesson: Lesson, step: dict) -> str:
        """Classify which kind of adaptation a lesson suggests for a step."""
        if getattr(lesson, "is_user_correction", False):
            return "user_preference"
        # If the lesson's strategy is a recovery-related term, treat as recovery.
        strategy = (lesson.strategy or "").lower()
        if any(word in strategy for word in ("recover", "retry", "fallback", "alternative")):
            return "recovery_preference"
        # If the lesson strategy is itself a known substitutable tool, always
        # classify as tool_preference — even on empty-arg steps.  The tool_preference
        # branch in _try_apply handles substitutability, concrete-args guard, etc.
        _all_tools = [t for group in self.tool_groups.values() for t in group]
        if strategy in _all_tools:
            return "tool_preference"
        # Non-tool strategy on an empty-arg step → argument filling.
        step_args = step.get("args") or {}
        empty_args = not any(
            v is not None and str(v).strip() and str(v).strip().lower() not in ("", "none", "null", "auto", "default")
            for v in step_args.values()
        )
        if empty_args:
            return "argument_preference"
        return "tool_preference"

    def _derive_preferred_args(self, lesson: Lesson, current_args: dict) -> dict[str, Any]:
        """Derive preferred argument values from a lesson (placeholder logic)."""
        # For now: return empty dict; future extension can map strategies to preferred args.
        return {}
