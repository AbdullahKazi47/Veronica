"""Tests for VERONICA Learning & Adaptation Phase 4: CONTROLLED BEHAVIORAL ADAPTATION.

Covers: the BehaviorPolicy (confidence/evidence/safety gate), the BehavioralAdapter
(plan transformation; NEVER tool execution), explicit-context priority, user-correction
strength, reversible/fallback behavior, experience audit trail fields, and end-to-end
orchestrator wiring.

Architectural invariants asserted throughout:
  * The adapter never dispatches tools or executes anything — it only transforms a copy
    of the plan.
  * The original plan is always preserved (reversibility).
  * An adapter/policy failure discards adaptation and falls back to the original plan.
  * Safety-sensitive tools are never adapted.
  * Explicit current context overrides learned preference.
  * No new confidence calculation is introduced — only ConfidenceTracker values pass
    through the policy.

Baseline: 232 tests pass. This file adds the Phase 4 behavioral-adaptation coverage.
"""
from __future__ import annotations

import copy
import time

import pytest

from veronica.automation.task_executor import TaskExecutor
from veronica.config import Config
from veronica.core.context import SessionContext
from veronica.core.orchestrator import Orchestrator
from veronica.learning import (
    ConfidenceTracker,
    Experience,
    ExperienceOutcome,
    ExperienceStore,
    LearningEngine,
)
from veronica.learning.adaptation import BehavioralAdapter
from veronica.learning.lesson import Lesson
from veronica.learning.policy import (
    BehaviorPolicy,
    PolicyMode,
    PolicyThresholds,
    SAFETY_SENSITIVE_TOOLS,
)
from veronica.llm.base import LLMProvider, LLMResponse
from veronica.memory.long_term import LongTermMemory
from veronica.memory.short_term import ShortTermMemory
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def _lesson(strategy="find_file", *, confidence=0.95, evidence=3, outcome="success",
            is_user_correction=False, age_days=0.0) -> Lesson:
    """Build a Lesson with Phase 4 metadata defaults."""
    return Lesson(
        strategy=strategy,
        reason="previous experience",
        confidence=confidence,
        evidence_count=evidence,
        outcome=outcome,
        is_user_correction=is_user_correction,
        age_days=age_days,
    )


def _policy(**kw) -> BehaviorPolicy:
    return BehaviorPolicy(PolicyThresholds(**kw))


class _FakeGate:
    def check(self, tool_name, level, description=""):
        class _V:
            allowed = True
            reason = ""
        return _V()


class _ReadFileTool(Tool):
    name = "read_file"
    description = "Read a file."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}

    def run(self, file_path="", **kwargs) -> ToolResult:
        return ToolResult.ok(f"read {file_path}", file_path=file_path)


class _FindFileTool(Tool):
    name = "find_file"
    description = "Find a file by name."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def run(self, query="", **kwargs) -> ToolResult:
        return ToolResult.ok(f"found {query}", query=query)


class _DeleteFileTool(Tool):
    name = "delete_file"
    description = "Delete a file."
    permission = PermissionLevel.SENSITIVE
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}

    def run(self, file_path="", **kwargs) -> ToolResult:
        return ToolResult.ok(f"deleted {file_path}", file_path=file_path)


class _SearchWorkspaceTool(Tool):
    name = "search_workspace"
    description = "Search the project workspace by name."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def run(self, query="", **kwargs) -> ToolResult:
        return ToolResult.ok(f"searched {query}", query=query)


def _registry(tools=None):
    reg = ToolRegistry(_FakeGate())
    for t in tools or [_ReadFileTool(), _FindFileTool(), _DeleteFileTool()]:
        reg.register(t)
    return reg


# ---------------------------------------------------------------------------
# Policy: confidence / evidence / safety gate
# ---------------------------------------------------------------------------

class TestBehaviorPolicy:
    def test_below_min_confidence_is_none(self):
        lesson = _lesson(confidence=0.79, evidence=10)
        p = _policy().evaluate(lesson)
        assert p.mode == PolicyMode.NONE
        assert p.apply is False

    def test_advisory_band_is_advisory_only(self):
        lesson = _lesson(confidence=0.85, evidence=2)
        p = _policy().evaluate(lesson)
        assert p.mode == PolicyMode.ADVISORY
        assert p.apply is False  # informational only

    def test_min_confidence_exact_is_advisory(self):
        lesson = _lesson(confidence=0.80, evidence=2)
        p = _policy().evaluate(lesson)
        assert p.mode == PolicyMode.ADVISORY

    def test_high_confidence_without_evidence_is_advisory(self):
        lesson = _lesson(confidence=0.95, evidence=2)  # below min_evidence=3
        p = _policy().evaluate(lesson)
        assert p.mode == PolicyMode.ADVISORY
        assert "insufficient evidence" in p.reason

    def test_strong_requires_confidence_and_evidence(self):
        lesson = _lesson(confidence=0.95, evidence=3)
        p = _policy().evaluate(lesson)
        assert p.mode == PolicyMode.STRONG
        assert p.apply is True

    def test_strong_requires_exactly_min_evidence(self):
        lesson = _lesson(confidence=0.90, evidence=3)
        p = _policy().evaluate(lesson)
        assert p.mode == PolicyMode.STRONG

    def test_safety_always_blocks(self):
        lesson = _lesson(confidence=0.99, evidence=100)
        p = _policy().evaluate(lesson, safety_sensitive=True)
        assert p.mode == PolicyMode.NONE
        assert p.safety_blocked is True
        assert p.apply is False

    def test_user_correction_strong_at_high_confidence(self):
        lesson = _lesson(confidence=0.90, evidence=1, is_user_correction=True)
        p = _policy().evaluate(lesson, is_user_correction=True)
        assert p.mode == PolicyMode.STRONG

    def test_user_correction_advisory_at_advisory_confidence(self):
        lesson = _lesson(confidence=0.85, evidence=1, is_user_correction=True)
        p = _policy().evaluate(lesson, is_user_correction=True)
        assert p.mode == PolicyMode.ADVISORY

    def test_user_correction_below_min_is_none(self):
        lesson = _lesson(confidence=0.5, evidence=1, is_user_correction=True)
        p = _policy().evaluate(lesson, is_user_correction=True)
        assert p.mode == PolicyMode.NONE

    def test_no_evidence_is_none(self):
        lesson = _lesson(confidence=0.85, evidence=0)
        p = _policy().evaluate(lesson)
        assert p.mode == PolicyMode.NONE

    def test_uses_passed_confidence_not_new_calculation(self):
        """The policy must consume caller-provided tracker values, never fabricate a
        confidence itself."""
        p = _policy().evaluate(_lesson(), confidence=0.95, evidence=3)
        assert p.mode == PolicyMode.STRONG
        assert p.confidence == 0.95


# ---------------------------------------------------------------------------
# Adapter: reversible plan transformation (never execution)
# ---------------------------------------------------------------------------

class TestBehavioralAdapterBasics:
    def test_adapts_tool_within_same_group(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="search_workspace", confidence=0.95, evidence=3)
        plan = [{"description": "locate config", "tool": "find_file", "args": {"query": "unknown"}}]
        res = adapter.adapt(plan, [lesson], goal="find the config file")
        assert res.adaptation_applied is True
        assert res.adapted_plan[0]["tool"] == "search_workspace"

    def test_original_plan_never_mutated(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="search_workspace", confidence=0.95, evidence=3)
        plan = [{"description": "locate", "tool": "find_file", "args": {"query": "x"}}]
        original = [
            {"description": "locate", "tool": "find_file", "args": {"query": "x"}},
        ]
        res = adapter.adapt(plan, [lesson], goal="find")
        assert plan == original                       # input untouched
        assert res.original_plan == original          # preserved copy
        assert res.adapted_plan is not plan           # separate object (reversible)
        assert res.recover_original() == original

    def test_weak_confidence_applies_nothing(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="search_workspace", confidence=0.79, evidence=10)
        plan = [{"description": "locate", "tool": "find_file", "args": {}}]
        res = adapter.adapt(plan, [lesson], goal="find")
        assert res.adaptation_applied is False
        assert res.adapted_plan == plan

    def test_advisory_confidence_informs_but_never_changes(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="search_workspace", confidence=0.85, evidence=2)
        plan = [{"description": "locate", "tool": "find_file", "args": {}}]
        res = adapter.adapt(plan, [lesson], goal="find")
        assert res.adaptation_applied is False
        assert res.adapted_plan[0]["tool"] == "find_file"
        # The advisory lesson is still surfaced for audit / explanation.
        assert any(r.blocked_by == "advisory_only" for r in res.rejected)

    def test_not_substitutable_is_rejected(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="open_website", confidence=0.95, evidence=3)  # different group
        plan = [{"description": "locate", "tool": "find_file", "args": {}}]
        res = adapter.adapt(plan, [lesson], goal="find")
        assert res.adaptation_applied is False

    def test_empty_lessons_noop(self):
        adapter = BehavioralAdapter()
        plan = [{"description": "locate", "tool": "find_file", "args": {}}]
        res = adapter.adapt(plan, [], goal="find")
        assert res.adaptation_applied is False
        assert res.adapted_plan == plan

    def test_same_tool_preference_is_noop(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="find_file", confidence=0.95, evidence=3)
        plan = [{"description": "locate", "tool": "find_file", "args": {}}]
        res = adapter.adapt(plan, [lesson], goal="find")
        assert res.adaptation_applied is False

    def test_exception_falls_back_to_original(self):
        adapter = BehavioralAdapter()

        class BoomLesson(Lesson):
            def __getattribute__(self, name):
                if name == "strategy":
                    raise RuntimeError("boom")
                return super().__getattribute__(name)

        plan = [{"description": "locate", "tool": "find_file", "args": {}}]
        original = copy.deepcopy(plan)
        res = adapter.adapt(plan, [BoomLesson(strategy="x", reason="r", confidence=0.99, evidence_count=3)],
                            goal="find")
        assert res.adaptation_applied is False
        assert res.adapted_plan == original
        assert "ADAPTER FAILURE" in res.decision_summary


# ---------------------------------------------------------------------------
# Safety and explicit-context priority
# ---------------------------------------------------------------------------

class TestAdapterSafetyAndPriority:
    def test_safety_sensitive_tool_never_adapted(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="find_file", confidence=0.95, evidence=3)
        plan = [
            {"description": "delete temp", "tool": "delete_file", "args": {"file_path": "C:/tmp/x"}},
        ]
        res = adapter.adapt(plan, [lesson], goal="delete temp file")
        assert res.adaptation_applied is False
        assert res.adapted_plan[0]["tool"] == "delete_file"
        assert any(r.blocked_by == "safety" for r in res.rejected)

    def test_all_safety_sensitive_tools_protected(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="find_file", confidence=0.99, evidence=9)
        for dangerous in ("delete_file", "run_shell_command", "write_file", "install_package"):
            plan = [{"description": "x", "tool": dangerous, "args": {}}]
            res = adapter.adapt(plan, [lesson], goal="do x")
            assert res.adaptation_applied is False, f"{dangerous} must never be adapted"

    def test_explicit_current_context_args_not_overridden(self):
        """A step with a concrete non-placeholder path (current context) is not
        replaced by a learned tool preference."""
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="search_workspace", confidence=0.95, evidence=3)
        plan = [{"description": "read", "tool": "find_file", "args": {"query": "C:/Projects/VERONICA/README.md"}}]
        res = adapter.adapt(plan, [lesson], goal="find the readme")
        assert res.adaptation_applied is False
        assert res.adapted_plan[0]["tool"] == "find_file"
        assert any(r.blocked_by == "explicit_context" for r in res.rejected)

    def test_goal_explicitly_naming_tool_is_preserved(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="search_workspace", confidence=0.95, evidence=3)
        plan = [{"description": "find", "tool": "find_file", "args": {"query": "unknown"}}]
        res = adapter.adapt(plan, [lesson], goal="use find_file to locate the config")
        assert res.adaptation_applied is False
        assert res.adapted_plan[0]["tool"] == "find_file"
        assert any(r.blocked_by == "explicit_context" for r in res.rejected)

    def test_only_target_step_changed_not_other_steps(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="search_workspace", confidence=0.95, evidence=3)
        plan = [
            {"description": "locate", "tool": "find_file", "args": {"query": "unknown"}},
            {"description": "read", "tool": "read_file", "args": {"file_path": "C:/x.md"}},
        ]
        res = adapter.adapt(plan, [lesson], goal="find and read")
        assert res.adapted_plan[0]["tool"] == "search_workspace"
        assert res.adapted_plan[1]["tool"] == "read_file"   # other step untouched


# ---------------------------------------------------------------------------
# User corrections
# ---------------------------------------------------------------------------

class TestAdapterUserCorrection:
    def test_user_correction_preferred_tool_applied(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="list_directory", confidence=0.92, evidence=1,
                         is_user_correction=True)
        plan = [{"description": "list", "tool": "find_file", "args": {"query": "unknown"}}]
        res = adapter.adapt(plan, [lesson], goal="show me the files")
        assert res.adaptation_applied is True
        assert res.adapted_plan[0]["tool"] == "list_directory"
        assert res.applied[0].kind == "user_preference"


# ---------------------------------------------------------------------------
# Experience audit trail
# ---------------------------------------------------------------------------

class TestExperienceAdaptationFields:
    def test_roundtrip_preserves_adaptation_fields(self, tmp_path):
        exp = Experience(
            goal="find the readme",
            outcome=ExperienceOutcome.SUCCESS,
            tool_calls=["search_workspace"],
            original_plan=[{"tool": "find_file", "args": {}}],
            adaptations=[{"kind": "tool_preference", "from_value": "find_file",
                          "to_value": "search_workspace", "reason": "strong evidence"}],
            adaptation_applied=True,
            adaptation_success=True,
        )
        restored = Experience.from_dict(exp.to_dict())
        assert restored.original_plan == exp.original_plan
        assert restored.adaptations == exp.adaptations
        assert restored.adaptation_applied is True
        assert restored.adaptation_success is True

    def test_roundtrip_defaults(self):
        restored = Experience.from_dict({"goal": "g"})
        assert restored.original_plan is None
        assert restored.adaptations == []
        assert restored.adaptation_applied is False
        assert restored.adaptation_success is None

    def test_store_redacts_adaptation_fields(self, tmp_path):
        store = ExperienceStore(tmp_path / "e.jsonl")
        exp = Experience(
            goal="g",
            original_plan=[{"tool": "read_file", "args": {"file_path": "C:/secret/api_key.txt"}}],
            adaptations=[{"to_value": "api_key_sk-ABCDEF123456"}],
            adaptation_applied=True,
        )
        assert store.record(exp) is True
        lines = (tmp_path / "e.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert "[REDACTED]" in lines[-1]


# ---------------------------------------------------------------------------
# TaskExecutor: records adaptation metadata into Experience
# ---------------------------------------------------------------------------

class TestTaskExecutorAdaptationMetadata:
    def test_experience_records_adaptation(self, tmp_path):
        store = ExperienceStore(tmp_path / "e.jsonl")
        captured: list[Experience] = []
        executor = TaskExecutor(
            tools=_registry([_FindFileTool(), _ReadFileTool()]),
            experience_callback=lambda exp: (store.record(exp), captured.append(exp)),
        )
        task = executor.create_task(
            goal="find the readme",
            steps=[{"description": "find", "tool": "read_file", "args": {"file_path": "C:/x"}}],
            adaptation_applied=True,
        )
        # Set the adaptation metadata manually — the executor stores it on the task.
        task._adaptation_meta = {
            "adaptation_applied": True,
            "original_plan": [{"tool": "find_file", "args": {}}],
            "adaptations": [{"kind": "tool_preference", "to_value": "read_file", "confidence": 0.95}],
        }
        outcome = executor.execute_task(task)
        assert outcome["success"] is True
        captured = captured[-1]
        assert captured.adaptation_applied is True
        assert captured.original_plan == [{"tool": "find_file", "args": {}}]
        assert captured.adaptations[0]["kind"] == "tool_preference"
        assert captured.adaptation_success is True

    def test_no_adaptation_metadata_is_none_fields(self, tmp_path):
        captured: list[Experience] = []
        executor = TaskExecutor(
            tools=_registry([_ReadFileTool()]),
            experience_callback=lambda exp: captured.append(exp),
        )
        task = executor.create_task(
            goal="read",
            steps=[{"description": "read", "tool": "read_file", "args": {"file_path": "C:/x"}}],
        )
        executor.execute_task(task)
        exp = captured[-1]
        assert exp.adaptation_applied is False
        assert exp.adaptation_success is None
        assert exp.adaptations == []


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------

class _PlanLLM(LLMProvider):
    name = "plan_phase4"

    def __init__(self, plan_steps):
        self.plan_steps = plan_steps
        self.prompts: list[str] = []

    def chat(self, messages, tools=None, temperature=0.4):
        self.prompts.append("\n".join(
            m.content for m in messages if m.role in ("user", "system")
        ))
        return LLMResponse(content=__import__("json").dumps(self.plan_steps))


class _SingleLLM(LLMProvider):
    """For the fall-through path when the plan is single-step."""
    name = "single"

    def chat(self, messages, tools=None, temperature=0.4):
        return LLMResponse(content="Done.")


def _orchestrator(planner_llm, *, with_adapter=True, learning_enabled=True, tmp_path=None):
    from veronica.automation.planner import MultiStepPlanner
    from veronica.learning.adaptation import BehavioralAdapter

    config = Config()
    config.learning.enabled = learning_enabled

    store = ExperienceStore(tmp_path / "exp.jsonl")
    engine = LearningEngine(confidence=ConfidenceTracker())
    adapter = BehavioralAdapter() if with_adapter else None

    reg = _registry([_ReadFileTool(), _FindFileTool(), _SearchWorkspaceTool()])

    def _on_experience(exp) -> None:
        store.record(exp)
        engine.process(exp)

    task_executor = TaskExecutor(
        tools=reg,
        experience_callback=_on_experience if learning_enabled else None,
    )
    orchestrator = Orchestrator(
        config=config,
        llm=planner_llm,
        tools=reg,
        long_term_memory=LongTermMemory(tmp_path / "mem.json"),
        planner=MultiStepPlanner(),
        task_executor=task_executor,
        learning_store=store if learning_enabled else None,
        learning_engine=engine if learning_enabled else None,
        behavioral_adapter=adapter if learning_enabled else None,
    )
    return orchestrator, store, engine


class TestOrchestratorAdaptation:
    COMPLEX = "plan and run: locate the project config and then open it"

    def test_disabled_when_no_adapter(self, tmp_path):
        llm = _PlanLLM([
            {"description": "find", "tool": "find_file", "args": {"query": "x"}},
            {"description": "open", "tool": "read_file", "args": {"file_path": "C:/x.md"}},
        ])
        orchestrator, _, _ = _orchestrator(llm, with_adapter=False, tmp_path=tmp_path)
        resp = orchestrator.handle(self.COMPLEX, SessionContext(short_term=ShortTermMemory()))
        # No adapter → no transformation, task still executed normally.
        assert resp.task is not None
        assert resp.task["steps"][0]["tool"] == "find_file"

    def test_adapts_plan_and_records_experience(self, tmp_path):

        llm = _PlanLLM([
            {"description": "find", "tool": "find_file", "args": {"query": "unknown"}},
            {"description": "open", "tool": "read_file", "args": {"file_path": "C:/x.md"}},
        ])
        orchestrator, store, engine = _orchestrator(llm, tmp_path=tmp_path)

        # Pre-seed a STRONG lesson: search_workspace preferred (same group as find_file).
        # Three experiences of the same strategy + high ConfidenceTracker value.
        for _ in range(3):
            store.record(Experience(
                goal="locate the project config",
                outcome=ExperienceOutcome.SUCCESS,
                tool_calls=["search_workspace"],
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            ))
            engine.confidence.record("search_workspace", True)
        for _ in range(8):
            engine.confidence.record("search_workspace", True)  # push confidence to 0.90

        session = SessionContext(short_term=ShortTermMemory())
        resp = orchestrator.handle(self.COMPLEX, session)
        assert resp.task is not None
        assert resp.task["steps"][0]["tool"] == "search_workspace"

        # The executed task recorded the adaptation trail into the experience store.
        experiences = store.list_recent(10)
        adapted = [e for e in experiences if e.adaptation_applied]
        assert adapted, "expected an adaptation-applied experience in the store"
        recorded = adapted[-1]
        assert recorded.adaptation_applied is True
        assert recorded.original_plan is not None

    def test_adaptation_never_executes_tools_directly(self, tmp_path):
        """Security invariant: even with a strong lesson, tool dispatch must go
        through TaskExecutor→registry. We assert the plan transform produced the
        adapted plan but no tool was executed outside the executor."""
        from veronica.learning.adaptation import AdaptationResult

        llm = _PlanLLM([{"description": "find", "tool": "find_file", "args": {"query": "unknown"}}])
        orchestrator, _, _ = _orchestrator(llm, tmp_path=tmp_path)

        executed: list[str] = []
        original_dispatch = orchestrator.task_executor.tools.dispatch
        def spy_dispatch(name, **kwargs):
            executed.append(name)
            return original_dispatch(name, **kwargs)
        orchestrator.task_executor.tools.dispatch = spy_dispatch

        lesson = _lesson(strategy="search_workspace", confidence=0.95, evidence=3)
        result = orchestrator.behavioral_adapter.adapt(
            [{"description": "find", "tool": "find_file", "args": {"query": "unknown"}}],
            [lesson],
            goal=self.COMPLEX,
        )
        assert result.adaptation_applied is True
        # Calling adapt() must NOT trigger any dispatch — the spy stays quiet.
        assert executed == []
        # Only the later task execution (through the executor) dispatches.
        orchestrator.behavioral_adapter = None  # no-op safety; end of test scope


# ---------------------------------------------------------------------------
# Staleness / age handling
# ---------------------------------------------------------------------------

class TestLessonStaleness:
    def test_age_days_preserved_in_lesson(self):
        lesson = _lesson(age_days=45.0)
        assert lesson.age_days == 45.0

    def test_old_lesson_still_advisory(self):
        adapter = BehavioralAdapter()
        lesson = _lesson(strategy="search_workspace", confidence=0.95, evidence=3, age_days=200.0)
        plan = [{"description": "locate", "tool": "find_file", "args": {"query": "unknown"}}]
        res = adapter.adapt(plan, [lesson], goal="find")
        # Stale lessons still inform but are capped to advisory (never applied blindly).
        assert res.adaptation_applied is False
        assert res.adapted_plan[0]["tool"] == "find_file"


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------

class TestConfigWiring:
    def test_config_defaults(self):
        config = Config()
        assert config.learning.adaptation_enabled is True
        assert config.learning.adaptation_min_confidence == 0.80
        assert config.learning.adaptation_strong_confidence == 0.90
        assert config.learning.adaptation_min_evidence == 3

    def test_adaptation_disabled_when_learning_disabled(self, tmp_path):
        llm = _PlanLLM([{"description": "find", "tool": "find_file", "args": {"query": "x"}}])
        orchestrator, _, _ = _orchestrator(llm, with_adapter=True,
                                           learning_enabled=False, tmp_path=tmp_path)
        assert orchestrator.behavioral_adapter is None
        assert orchestrator.learning_store is None


# ---------------------------------------------------------------------------
# Lesson staleness enrichment (Phase 4 metadata flows from merge)
# ---------------------------------------------------------------------------

class TestLessonMetadataFromMerge:
    def test_merge_lessons_carries_phase4_metadata(self, tmp_path):
        from veronica.learning.lesson import merge_lessons
        from veronica.learning.retrieval import RankedExperience

        # Two experiences with explicit user feedback → merged lesson is_user_correction
        exps = [
            Experience(goal="find files", outcome=ExperienceOutcome.SUCCESS,
                       tool_calls=["list_directory"], timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
                       user_feedback={"kind": "correction", "subject": "list_directory"}),
            Experience(goal="find files", outcome=ExperienceOutcome.SUCCESS,
                       tool_calls=["list_directory"], timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
                       user_feedback={"kind": "preference", "subject": "list_directory"}),
        ]
        ranked = [RankedExperience(exp, score=0.9) for exp in exps]
        lessons = merge_lessons(ranked, lambda tool: 0.95)
        assert lessons
        lesson = lessons[0]
        assert lesson.is_user_correction is True
        assert "list_directory" in lesson.reason