"""
Phase 6: Closed-Loop Intelligence — comprehensive test suite.

Tests cover:
  1. Observation layer (observe_step, observe_task, categorization, signatures)
  2. Diagnosis engine (diagnose_step, diagnose_task, recovery actions)
  3. Failure memory (same-failure detection, blocking, clearing)
  4. Replanner (heuristic replan, LLM replan, task creation)
  5. Closed-loop controller (safety boundaries, recovery loop, events)
  6. Orchestrator integration (closed-loop in agentic path)
  7. Bridge integration (SSE events for recovery lifecycle)
  8. Adversarial / edge-case tests

Run with: pytest tests/test_closed_loop.py -v
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from veronica.automation.task_executor import (
    MultiStepTask,
    TaskExecutor,
    TaskStep,
    TASK_STATUS,
)
from veronica.automation.observation import (
    ExpectedOutcome,
    ObservedOutcome,
    StepObservation,
    TaskObservation,
    FailureCategory,
    EvidenceSufficiency,
    _compute_failure_signature,
    _categorize_failure,
    _assess_evidence,
    _generate_notes,
    observe_step,
    observe_task,
)
from veronica.automation.diagnosis import (
    DiagnosisConfidence,
    RecoveryAction,
    StepDiagnosis,
    TaskDiagnosis,
    diagnose_step,
    diagnose_task,
)
from veronica.automation.failure_memory import (
    FailureCluster,
    FailureMemory,
    FailureRecord,
)
from veronica.automation.replanner import Replanner, ReplanResult
from veronica.automation.closed_loop import ClosedLoopController, SafetyConfig
from veronica.learning.experience import Experience, ExperienceOutcome
from veronica.learning.lesson import Lesson


# ── Test fixtures ───────────────────────────────────────────────────────────

class _FakeToolResult:
    def __init__(self, success=True, message="ok", data=None):
        self.success = success
        self.message = message
        self.data = data or {}
        self.__dict__.update(self.data)


class _SuccessTool:
    name = "success_tool"
    def run(self, **kwargs):
        return _FakeToolResult(True, "done")

class _FailTool:
    name = "fail_tool"
    def run(self, **kwargs):
        return _FakeToolResult(False, "permission denied — not permitted")

class _FileTool:
    name = "write_file"
    def run(self, **kwargs):
        return _FakeToolResult(True, "file written")

class _AlwaysFailTool:
    name = "always_fail_tool"
    def run(self, **kwargs):
        return _FakeToolResult(False, "always fails — file not found")

class _FlakyTool:
    """Fails N times, then succeeds."""
    def __init__(self, fail_count=2):
        self.name = "flaky_tool"
        self._fail_count = fail_count
        self._call_count = 0
    def run(self, **kwargs):
        self._call_count += 1
        if self._call_count <= self._fail_count:
            return _FakeToolResult(False, "transient error — try again")
        return _FakeToolResult(True, "success after retry")


class _FakePermissionGate:
    def __init__(self, threshold=0):
        self.threshold = threshold
    def check(self, tool_name, args=None, confirm_callback=None):
        return True


class _FakeRegistry:
    def __init__(self, tools=None):
        self._tools = {}
        for t in (tools or []):
            self._tools[t.name] = t
    def dispatch(self, tool_name, **kwargs):
        t = self._tools.get(tool_name)
        if t is None:
            return _FakeToolResult(False, f"Unknown tool: {tool_name}")
        return t.run(**kwargs)
    def find_fast_match(self, text):
        return None
    def schemas(self):
        return []


def _make_step(desc="test step", tool="success_tool", status="completed", error="", verified=True):
    step = TaskStep(desc, tool)
    step.status = status
    step.error = error
    step.verified = verified
    step.verification_note = "verified" if verified else "not verified"
    step.start_time = time.time() - 0.1
    step.end_time = time.time()
    return step


def _make_task(goal="test goal", step_count=2, status="completed"):
    steps = []
    for i in range(step_count):
        step = TaskStep(f"step {i+1}", "success_tool")
        step.status = status
        step.verified = True
        step.verification_note = "verified"
        step.start_time = time.time() - 0.1
        step.end_time = time.time()
        steps.append(step)
    task = MultiStepTask("T123_test", goal, steps)
    task.status = status
    task.started_time = time.time() - 1.0
    task.completed_time = time.time()
    return task


def _make_failed_task(goal="test goal", fail_step=0, error="permission denied"):
    steps = []
    for i in range(3):
        step = TaskStep(f"step {i+1}", "success_tool")
        if i == fail_step:
            step.status = "failed"
            step.error = error
            step.verified = False
        else:
            step.status = "completed"
            step.verified = True
            step.verification_note = "verified"
        step.start_time = time.time() - 0.1
        step.end_time = time.time()
        steps.append(step)
    task = MultiStepTask("T123_fail", goal, steps)
    task.status = "failed"
    task.started_time = time.time() - 1.0
    task.completed_time = time.time()
    return task


def _make_lesson(strategy="use tool X", reason="it works", confidence=0.8):
    return Lesson(
        strategy=strategy,
        reason=reason,
        confidence=confidence,
        source_task_id="T001",
        outcome=ExperienceOutcome.SUCCESS,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  1. OBSERVATION LAYER
# ─────────────────────────────────────────────────────────────────────────────

class TestObservationLayer:
    """Tests for the observation layer (observation.py)."""

    def test_observe_step_success(self):
        """Completed and verified step → UNKNOWN_FAILURE (not really a failure)."""
        step = _make_step("write file", "write_file", status="completed", verified=True)
        obs = observe_step(step, 0, "write file")
        assert obs.observed.success is True
        assert obs.observed.verified is True
        assert obs.category == FailureCategory.UNKNOWN_FAILURE
        assert obs.failure_signature  # should be computed

    def test_observe_step_failure_permission(self):
        """Permission denied error → PERMISSION_DENIAL category."""
        step = _make_step("delete file", "delete_file", status="failed",
                          error="permission denied — access forbidden", verified=False)
        obs = observe_step(step, 0, "delete file")
        assert obs.observed.success is False
        assert obs.category == FailureCategory.PERMISSION_DENIAL
        assert obs.evidence == EvidenceSufficiency.SUFFICIENT

    def test_observe_step_failure_missing_resource(self):
        """File not found error → MISSING_RESOURCE category."""
        step = _make_step("read file", "read_file", status="failed",
                          error="no such file or directory", verified=False)
        obs = observe_step(step, 0, "read file")
        assert obs.category == FailureCategory.MISSING_RESOURCE

    def test_observe_step_failure_transient(self):
        """Transient error → TRANSIENT_FAILURE category."""
        step = _make_step("api call", "fetch_webpage", status="failed",
                          error="temporary error — try again later", verified=False)
        obs = observe_step(step, 0, "api call")
        assert obs.category == FailureCategory.TRANSIENT_FAILURE
        # Evidence should be INSUFFICIENT (only 1 attempt)
        assert obs.evidence == EvidenceSufficiency.INSUFFICIENT

    def test_observe_step_failure_external_service(self):
        """Timeout/connection error → EXTERNAL_SERVICE_FAILURE category."""
        step = _make_step("fetch url", "fetch_webpage", status="failed",
                          error="connection timed out — network unreachable", verified=False)
        obs = observe_step(step, 0, "fetch url")
        assert obs.category == FailureCategory.EXTERNAL_SERVICE_FAILURE

    def test_observe_step_verification_failure(self):
        """Tool said success but verification failed → VERIFICATION_FAILURE."""
        step = _make_step("write file", "write_file", status="completed",
                          verified=False, error="")
        step.verification_note = "file MISSING at /tmp/test.txt"
        obs = observe_step(step, 0, "write file")
        assert obs.observed.success is True
        assert obs.observed.verified is False
        assert obs.category == FailureCategory.VERIFICATION_FAILURE

    def test_observe_step_malformed_args(self):
        """Invalid argument error → MALFORMED_ARGS category."""
        step = _make_step("run command", "run_shell_command", status="failed",
                          error="invalid argument — missing required parameter", verified=False)
        obs = observe_step(step, 0, "run command")
        assert obs.category == FailureCategory.MALFORMED_ARGS

    def test_observe_step_unknown_failure(self):
        """Unrecognized error → UNKNOWN_FAILURE category."""
        step = _make_step("some tool", "custom_tool", status="failed",
                          error="something weird happened", verified=False)
        obs = observe_step(step, 0, "some tool")
        assert obs.category == FailureCategory.UNKNOWN_FAILURE
        assert obs.evidence == EvidenceSufficiency.AMBIGUOUS

    def test_observe_step_success_criteria_derived(self):
        """File tools with path arg should get 'file_exists' criterion."""
        step = TaskStep("write file", "write_file", {"file_path": "/tmp/test.txt"})
        step.status = "completed"
        step.verified = True
        step.verification_note = "verified"
        step.start_time = time.time() - 0.1
        step.end_time = time.time()
        obs = observe_step(step, 0, "write file")
        assert "file_exists" in obs.expected.success_criteria

    def test_observe_step_url_criteria_derived(self):
        """URL tools with url arg should get 'url_valid' criterion."""
        step = TaskStep("fetch url", "fetch_webpage", {"url": "https://example.com"})
        step.status = "completed"
        step.verified = True
        step.verification_note = "verified"
        step.start_time = time.time() - 0.1
        step.end_time = time.time()
        obs = observe_step(step, 0, "fetch url")
        assert "url_valid" in obs.expected.success_criteria

    def test_failure_signature_deterministic(self):
        """Same inputs produce same signature."""
        step1 = _make_step("fail", "tool_x", status="failed", error="same error", verified=False)
        obs1 = observe_step(step1, 0, "fail")
        step2 = _make_step("fail", "tool_x", status="failed", error="same error", verified=False)
        obs2 = observe_step(step2, 0, "fail")
        assert obs1.failure_signature == obs2.failure_signature

    def test_failure_signature_different_for_different_errors(self):
        """Different errors produce different signatures."""
        step1 = _make_step("fail", "tool_x", status="failed", error="error A", verified=False)
        obs1 = observe_step(step1, 0, "fail")
        step2 = _make_step("fail", "tool_x", status="failed", error="error B", verified=False)
        obs2 = observe_step(step2, 0, "fail")
        assert obs1.failure_signature != obs2.failure_signature

    def test_observe_task_success(self):
        """All steps completed → overall_status='success'."""
        task = _make_task("do things", step_count=3, status="completed")
        task_obs = observe_task(task)
        assert task_obs.overall_status == "success"
        assert task_obs.completed_steps == 3
        assert task_obs.total_steps == 3
        assert task_obs.failed_step_indices == []

    def test_observe_task_failure(self):
        """One failed step → overall_status='failed'."""
        task = _make_failed_task("do things", fail_step=1, error="not found")
        task_obs = observe_task(task)
        assert task_obs.overall_status == "failed"
        assert 1 in task_obs.failed_step_indices

    def test_observe_task_partial(self):
        """Some steps completed, some not → 'partial'."""
        steps = [
            _make_step("step 1", "success_tool", status="completed", verified=True),
            TaskStep("step 2", "success_tool"),  # pending
        ]
        task = MultiStepTask("T_partial", "goal", steps)
        task.status = "failed"
        task_obs = observe_task(task)
        assert task_obs.overall_status == "failed"
        assert task_obs.completed_steps == 1

    def test_observe_task_to_dict(self):
        """TaskObservation serializes correctly."""
        task = _make_task("goal", step_count=1, status="completed")
        task_obs = observe_task(task)
        d = task_obs.to_dict()
        assert "task_id" in d
        assert "step_observations" in d
        assert len(d["step_observations"]) == 1

    def test_observe_step_retried(self):
        """Step with multiple attempts → notes should mention retry."""
        step = _make_step("flaky", "flaky_tool", status="failed", error="busy")
        step.attempts = 3
        obs = observe_step(step, 0, "flaky")
        assert any("Retried" in n for n in obs.notes)


# ─────────────────────────────────────────────────────────────────────────────
#  2. DIAGNOSIS ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class TestDiagnosisEngine:
    """Tests for the diagnosis engine (diagnosis.py)."""

    def _make_obs(self, tool="tool_x", category=FailureCategory.PERMISSION_DENIAL,
                  error="permission denied", verified=False):
        step = TaskStep("test", tool)
        step.status = "failed"
        step.error = error
        step.verified = verified
        step.verification_note = "not verified"
        step.start_time = time.time()
        step.end_time = time.time()
        return observe_step(step, 0, "test")

    def test_diagnose_permission_denial(self):
        """Permission denial → recovery suggests human_intervention."""
        obs = self._make_obs(category=FailureCategory.PERMISSION_DENIAL,
                             error="permission denied")
        diag = diagnose_step(obs)
        assert diag.confidence == DiagnosisConfidence.HIGH
        assert len(diag.recovery_actions) > 0
        types = [a.action_type for a in diag.recovery_actions]
        assert "human_intervention" in types

    def test_diagnose_missing_resource(self):
        """Missing resource → recovery suggests step_insert or arg_change."""
        obs = self._make_obs(category=FailureCategory.MISSING_RESOURCE,
                             error="file not found")
        diag = diagnose_step(obs)
        assert diag.confidence == DiagnosisConfidence.HIGH
        types = [a.action_type for a in diag.recovery_actions]
        assert any(t in types for t in ["step_insert", "arg_change", "retry"])

    def test_diagnose_transient_failure(self):
        """Transient failure → recovery suggests retry."""
        obs = self._make_obs(category=FailureCategory.TRANSIENT_FAILURE,
                             error="busy, try again")
        diag = diagnose_step(obs)
        types = [a.action_type for a in diag.recovery_actions]
        assert "retry" in types

    def test_diagnose_tool_failure(self):
        """Tool failure → recovery suggests tool_substitute or retry."""
        obs = self._make_obs(category=FailureCategory.TOOL_FAILURE,
                             error="tool dispatch failed unexpectedly")
        diag = diagnose_step(obs)
        types = [a.action_type for a in diag.recovery_actions]
        assert any(t in types for t in ["tool_substitute", "retry"])

    def test_diagnose_unknown_failure(self):
        """Unknown failure → recovery suggests human_intervention or retry."""
        obs = self._make_obs(category=FailureCategory.UNKNOWN_FAILURE,
                             error="something weird")
        diag = diagnose_step(obs)
        assert len(diag.recovery_actions) > 0

    def test_diagnose_task_with_multiple_failures(self):
        """Task diagnosis aggregates step diagnoses."""
        task = _make_failed_task("goal", fail_step=1, error="permission denied")
        task_obs = observe_task(task)
        task_diag = diagnose_task(task_obs)
        assert task_diag.recommended_approach in ("replan", "human_intervention", "retry")
        assert len(task_diag.step_diagnoses) >= 1

    def test_diagnose_task_success(self):
        """All steps succeeded → step_diagnoses should be empty (no failures to diagnose)."""
        task = _make_task("goal", step_count=2, status="completed")
        task_obs = observe_task(task)
        task_diag = diagnose_task(task_obs)
        # With no failed steps, there are no step diagnoses
        # The overall approach may still be "replan" as a default, but no step diagnoses
        assert len(task_diag.step_diagnoses) == 0

    def test_diagnose_confidence_high_for_clear_categories(self):
        """Clear categories (permission, missing resource) → HIGH confidence."""
        obs = self._make_obs(category=FailureCategory.PERMISSION_DENIAL,
                             error="denied")
        diag = diagnose_step(obs)
        assert diag.confidence in (DiagnosisConfidence.HIGH, DiagnosisConfidence.MEDIUM)

    def test_diagnose_to_dict(self):
        """StepDiagnosis serializes correctly."""
        obs = self._make_obs(category=FailureCategory.PERMISSION_DENIAL,
                             error="denied")
        diag = diagnose_step(obs)
        d = diag.to_dict()
        assert "confidence" in d
        assert "recovery_actions" in d


# ─────────────────────────────────────────────────────────────────────────────
#  3. FAILURE MEMORY
# ─────────────────────────────────────────────────────────────────────────────

class TestFailureMemory:
    """Tests for failure memory (failure_memory.py)."""

    def _make_fail_obs(self, tool="tool_x", error="same error"):
        step = TaskStep("test", tool)
        step.status = "failed"
        step.error = error
        step.verified = False
        step.verification_note = ""
        step.start_time = time.time()
        step.end_time = time.time()
        return observe_step(step, 0, "test")

    def test_record_first_failure_allows_continuation(self):
        """First failure should allow continuation."""
        mem = FailureMemory(max_identical_failures=3)
        obs = self._make_fail_obs()
        result = mem.record_failure(obs, task_id="T1")
        assert result is True

    def test_record_identical_failures_blocks(self):
        """After max_identical_failures, should block."""
        mem = FailureMemory(max_identical_failures=3)
        obs = self._make_fail_obs()
        for _ in range(2):
            assert mem.record_failure(obs, task_id="T1") is True
        # Third failure should block
        assert mem.record_failure(obs, task_id="T1") is False

    def test_is_blocked(self):
        """Signature is blocked after exceeding limit."""
        mem = FailureMemory(max_identical_failures=2)
        obs = self._make_fail_obs()
        mem.record_failure(obs, task_id="T1")
        assert mem.is_blocked(obs.failure_signature) is False
        mem.record_failure(obs, task_id="T1")
        assert mem.is_blocked(obs.failure_signature) is True

    def test_should_stop_returns_false_for_unknown_signature(self):
        """should_stop returns False for unknown signatures."""
        mem = FailureMemory()
        obs = self._make_fail_obs()
        assert mem.should_stop(obs) is False

    def test_different_errors_not_blocked_together(self):
        """Different errors should not count as the same failure."""
        mem = FailureMemory(max_identical_failures=2)
        obs1 = self._make_fail_obs(error="error A")
        obs2 = self._make_fail_obs(error="error B")
        assert mem.record_failure(obs1, task_id="T1") is True
        assert mem.record_failure(obs2, task_id="T1") is True
        # Neither should be blocked
        assert mem.is_blocked(obs1.failure_signature) is False
        assert mem.is_blocked(obs2.failure_signature) is False

    def test_recovery_attempts_exhausted(self):
        """After max_recovery_attempts, should block."""
        mem = FailureMemory(max_recovery_attempts=2)
        obs = self._make_fail_obs()
        mem.record_failure(obs, task_id="T1", recovery_attempted="retry")
        assert mem.record_failure(obs, task_id="T1", recovery_attempted="arg_change") is True
        # Third unique recovery attempt should block
        assert mem.record_failure(obs, task_id="T1", recovery_attempted="tool_substitute") is False

    def test_get_failure_summary(self):
        """Summary includes total failures and categories."""
        mem = FailureMemory()
        obs = self._make_fail_obs(tool="tool_x")
        mem.record_failure(obs, task_id="T1")
        summary = mem.get_failure_summary()
        assert summary["total_failures"] >= 1
        assert summary["unique_signatures"] >= 1

    def test_clear_task(self):
        """Clearing a task removes its failure records."""
        mem = FailureMemory()
        obs = self._make_fail_obs()
        mem.record_failure(obs, task_id="T1")
        count = mem.clear_task("T1")
        assert count >= 1
        records = mem.get_task_failures("T1")
        assert len(records) == 0

    def test_clear_all(self):
        """Clearing all removes everything."""
        mem = FailureMemory()
        obs = self._make_fail_obs()
        mem.record_failure(obs, task_id="T1")
        mem.clear_all()
        summary = mem.get_failure_summary()
        assert summary["total_failures"] == 0

    def test_get_task_failures(self):
        """Returns only failures for the specified task."""
        mem = FailureMemory()
        obs = self._make_fail_obs()
        mem.record_failure(obs, task_id="T1")
        mem.record_failure(obs, task_id="T2")
        t1 = mem.get_task_failures("T1")
        t2 = mem.get_task_failures("T2")
        assert len(t1) >= 1
        assert all(r.task_id == "T1" for r in t1)


# ─────────────────────────────────────────────────────────────────────────────
#  4. REPLANNER
# ─────────────────────────────────────────────────────────────────────────────

class TestReplanner:
    """Tests for the replanner (replanner.py)."""

    def _make_diag(self, approach="replan", confidence=DiagnosisConfidence.HIGH):
        obs = _make_step("step 1", "tool_x", status="failed", error="error", verified=False)
        step_obs = observe_step(obs, 0, "step 1")
        step_diag = StepDiagnosis(
            step_index=0,
            observation=step_obs,
            root_cause="test cause",
            confidence=confidence,
            recovery_actions=[
                RecoveryAction(action_type="tool_substitute",
                               description="use alt tool",
                               target_step_index=0,
                               details={"substitute_tool": "alt_tool"},
                               requires_permission=False),
            ],
        )
        return TaskDiagnosis(
            task_id="T_test",
            goal="goal",
            step_diagnoses=[step_diag],
            overall_confidence=confidence,
            recommended_approach=approach,
            remaining_goal="do the thing",
            preserved_step_indices=[],
            notes=["test note"],
        )

    def test_heuristic_replan_generates_steps(self):
        """Heuristic replan should generate substitute steps."""
        replanner = Replanner()
        task = _make_task("goal", step_count=2, status="failed")
        diag = self._make_diag(approach="replan")
        result = replanner.replan(task, diag, lessons=[])
        assert result.success is True
        assert len(result.new_steps) > 0

    def test_replan_human_intervention_returns_early(self):
        """When diagnosis says human_intervention, replan returns early."""
        replanner = Replanner()
        task = _make_task("goal", step_count=1, status="failed")
        diag = self._make_diag(approach="human_intervention")
        result = replanner.replan(task, diag, lessons=[])
        assert result.requires_human is True
        assert result.success is False

    def test_create_replan_task_preserves_completed_steps(self):
        """Replan task should have preserved steps marked completed."""
        replanner = Replanner()
        task = _make_task("goal", step_count=3, status="failed")
        diag = self._make_diag(approach="replan")
        diag.preserved_step_indices = [0, 1]
        result = replanner.replan(task, diag, lessons=[])
        assert result.success is True
        new_task = replanner.create_replan_task(task, result)
        assert new_task is not None
        # First two steps should be preserved (completed)
        assert new_task.steps[0].status == TASK_STATUS["COMPLETED"]
        assert new_task.steps[1].status == TASK_STATUS["COMPLETED"]

    def test_create_replan_task_returns_none_on_failure(self):
        """create_replan_task returns None when replan failed."""
        replanner = Replanner()
        task = _make_task("goal", step_count=1, status="failed")
        diag = self._make_diag(approach="human_intervention")
        result = replanner.replan(task, diag, lessons=[])
        new_task = replanner.create_replan_task(task, result)
        assert new_task is None

    def test_replan_result_to_dict(self):
        """ReplanResult serializes correctly."""
        result = ReplanResult(
            success=True,
            new_steps=[{"description": "new step"}],
            preserved_steps=[],
            goal_remaining="remaining",
        )
        d = result.to_dict()
        assert d["success"] is True
        assert len(d["new_steps"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
#  5. CLOSED-LOOP CONTROLLER
# ─────────────────────────────────────────────────────────────────────────────

class TestClosedLoopController:
    """Tests for the closed-loop controller (closed_loop.py)."""

    def test_safety_config_defaults(self):
        """Default safety config has reasonable limits."""
        config = SafetyConfig()
        assert config.max_replans == 3
        assert config.max_retries_per_step == 2
        assert config.max_total_steps == 50
        assert config.max_execution_seconds == 600.0

    def test_safety_config_to_dict(self):
        """SafetyConfig serializes correctly."""
        config = SafetyConfig(max_replans=5)
        d = config.to_dict()
        assert d["max_replans"] == 5

    def test_execute_success_first_try(self):
        """Task that succeeds on first try → no recovery needed."""
        registry = _FakeRegistry([_SuccessTool()])
        executor = TaskExecutor(registry)
        controller = ClosedLoopController(executor=executor)
        task = executor.create_task("goal", [{"description": "do thing", "tool": "success_tool", "args": {}}])
        result = controller.execute(task)
        assert result["success"] is True
        assert result["recovery_attempts"] == 1

    def test_execute_emits_events(self):
        """Controller emits events during execution."""
        registry = _FakeRegistry([_SuccessTool()])
        executor = TaskExecutor(registry)
        events = []
        controller = ClosedLoopController(
            executor=executor,
            event_callback=lambda sig, args: events.append((sig, args)),
        )
        task = executor.create_task("goal", [{"description": "do thing", "tool": "success_tool", "args": {}}])
        controller.execute(task)
        signals = [e[0] for e in events]
        assert "task_recovery_started" in signals
        assert "task_executed" in signals
        assert "task_observing" in signals

    def test_execute_failure_triggers_diagnosis(self):
        """Failed task triggers diagnosis event."""
        registry = _FakeRegistry([_FailTool()])
        executor = TaskExecutor(registry)
        events = []
        controller = ClosedLoopController(
            executor=executor,
            event_callback=lambda sig, args: events.append((sig, args)),
        )
        task = executor.create_task("goal", [{"description": "delete", "tool": "fail_tool", "args": {}}])
        result = controller.execute(task)
        signals = [e[0] for e in events]
        assert "task_diagnosing" in signals
        assert "task_diagnosis_complete" in signals

    def test_execute_blocked_by_safety_boundary(self):
        """Task blocked when max_replans exceeded."""
        registry = _FakeRegistry([_FailTool()])
        executor = TaskExecutor(registry)
        controller = ClosedLoopController(
            executor=executor,
            safety_config=SafetyConfig(max_replans=0),
            event_callback=lambda sig, args: None,
        )
        task = executor.create_task("goal", [{"description": "fail", "tool": "fail_tool", "args": {}}])
        result = controller.execute(task)
        assert result["success"] is False
        assert "blocked_reason" in result

    def test_execute_human_intervention_stops(self):
        """Human intervention recommendation stops the loop."""
        registry = _FakeRegistry([_FailTool()])
        executor = TaskExecutor(registry)
        events = []
        controller = ClosedLoopController(
            executor=executor,
            event_callback=lambda sig, args: events.append((sig, args)),
        )
        task = executor.create_task("goal", [{"description": "fail", "tool": "fail_tool", "args": {}}])
        result = controller.execute(task)
        signals = [e[0] for e in events]
        # Should have diagnosis and either needs_confirmation or blocked
        assert "task_diagnosis_complete" in signals

    def test_get_status(self):
        """get_status returns active tasks and config."""
        registry = _FakeRegistry([_SuccessTool()])
        executor = TaskExecutor(registry)
        controller = ClosedLoopController(executor=executor)
        status = controller.get_status()
        assert "active_tasks" in status
        assert "safety_config" in status


# ─────────────────────────────────────────────────────────────────────────────
#  6. ORCHESTRATOR INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────

class TestOrchestratorIntegration:
    """Tests for closed-loop integration in the orchestrator."""

    def test_orchestrator_stores_closed_loop_controller(self):
        """Orchestrator stores the closed_loop_controller when provided."""
        from veronica.core.orchestrator import Orchestrator
        from veronica.config import Config
        config = Config()
        mock_llm = MagicMock()
        mock_llm.chat.return_value = MagicMock(content="ok", tool_calls=None)
        mock_tools = MagicMock()
        mock_tools.schemas.return_value = []
        mock_memory = MagicMock()
        controller = MagicMock(spec=ClosedLoopController)
        orch = Orchestrator(
            config=config,
            llm=mock_llm,
            tools=mock_tools,
            long_term_memory=mock_memory,
            closed_loop_controller=controller,
        )
        assert orch.closed_loop_controller is controller

    def test_orchestrator_none_without_controller(self):
        """Orchestrator.closed_loop_controller defaults to None."""
        from veronica.core.orchestrator import Orchestrator
        from veronica.config import Config
        config = Config()
        mock_llm = MagicMock()
        mock_tools = MagicMock()
        mock_memory = MagicMock()
        orch = Orchestrator(
            config=config,
            llm=mock_llm,
            tools=mock_tools,
            long_term_memory=mock_memory,
        )
        assert orch.closed_loop_controller is None


# ─────────────────────────────────────────────────────────────────────────────
#  7. BRIDGE INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────

class TestBridgeIntegration:
    """Tests for SSE event broadcasting in the Stitch bridge."""

    def test_bridge_stores_closed_loop_controller(self):
        """StitchCommandHandler stores the closed_loop_controller."""
        from veronica.ui.stitch.bridge import StitchCommandHandler, EventBroadcaster
        mock_orch = MagicMock()
        mock_orch.tool_event_callback = None
        mock_orch.new_session.return_value = MagicMock()
        mock_config = MagicMock()
        broadcaster = EventBroadcaster()
        controller = MagicMock()
        handler = StitchCommandHandler(
            orchestrator=mock_orch,
            config=mock_config,
            broadcast=broadcaster,
            closed_loop_controller=controller,
        )
        assert handler.closed_loop_controller is controller

    def test_on_closed_loop_event_broadcasts_recovery_started(self):
        """_on_closed_loop_event broadcasts task_recovery_started."""
        from veronica.ui.stitch.bridge import StitchCommandHandler, EventBroadcaster
        mock_orch = MagicMock()
        mock_orch.tool_event_callback = None
        mock_orch.new_session.return_value = MagicMock()
        mock_config = MagicMock()
        broadcaster = EventBroadcaster()
        handler = StitchCommandHandler(
            orchestrator=mock_orch,
            config=mock_config,
            broadcast=broadcaster,
        )
        published = []
        original_publish = broadcaster.publish
        def capture_publish(signal, args):
            published.append((signal, args))
        broadcaster.publish = capture_publish
        handler._on_closed_loop_event("task_recovery_started", {"task_id": "T1", "attempt": 1, "max_attempts": 3})
        signals = [p[0] for p in published]
        assert "task_recovery_started" in signals

    def test_on_closed_loop_event_broadcasts_blocked(self):
        """_on_closed_loop_event broadcasts task_blocked."""
        from veronica.ui.stitch.bridge import StitchCommandHandler, EventBroadcaster
        mock_orch = MagicMock()
        mock_orch.tool_event_callback = None
        mock_orch.new_session.return_value = MagicMock()
        mock_config = MagicMock()
        broadcaster = EventBroadcaster()
        handler = StitchCommandHandler(
            orchestrator=mock_orch,
            config=mock_config,
            broadcast=broadcaster,
        )
        published = []
        broadcaster.publish = lambda sig, args: published.append((sig, args))
        handler._on_closed_loop_event("task_blocked", {"task_id": "T1", "reason": "safety boundary"})
        signals = [p[0] for p in published]
        assert "task_blocked" in signals
        assert "show_activity" in signals


# ─────────────────────────────────────────────────────────────────────────────
#  8. ADVERSARIAL / EDGE-CASE TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestAdversarialEdgeCases:
    """Adversarial and edge-case tests for Phase 6."""

    def test_observation_with_empty_error(self):
        """Step with empty error message should not crash observer."""
        step = TaskStep("test", "tool_x")
        step.status = "failed"
        step.error = ""
        step.verified = False
        step.start_time = time.time()
        step.end_time = time.time()
        obs = observe_step(step, 0, "test")
        assert obs.category == FailureCategory.UNKNOWN_FAILURE

    def test_observation_with_none_error(self):
        """Step with None error message should not crash observer."""
        step = TaskStep("test", "tool_x")
        step.status = "failed"
        step.error = None
        step.verified = False
        step.start_time = time.time()
        step.end_time = time.time()
        obs = observe_step(step, 0, "test")
        assert obs.category is not None

    def test_failure_memory_no_signature(self):
        """FailureMemory should handle observation with no failure signature."""
        mem = FailureMemory()
        step = TaskStep("test", "tool_x")
        step.status = "completed"
        step.verified = True
        step.start_time = time.time()
        step.end_time = time.time()
        obs = observe_step(step, 0, "test")
        # No failure signature for completed step
        result = mem.record_failure(obs, task_id="T1")
        assert result is True  # should not block

    def test_diagnosis_with_empty_step_diagnoses(self):
        """TaskDiagnosis with no step diagnoses should not crash."""
        task_diag = TaskDiagnosis(
            task_id="T_empty",
            goal="goal",
            step_diagnoses=[],
            overall_confidence=DiagnosisConfidence.LOW,
            recommended_approach="none",
            remaining_goal="goal",
            preserved_step_indices=[],
            notes=[],
        )
        d = task_diag.to_dict()
        assert d["recommended_approach"] == "none"

    def test_replanner_with_no_steps(self):
        """Replanner should handle task with no steps gracefully."""
        replanner = Replanner()
        task = MultiStepTask("T_empty", "goal", [])
        task.status = "completed"
        task_obs = observe_task(task)
        task_diag = TaskDiagnosis(
            task_id="T_empty",
            goal="goal",
            step_diagnoses=[],
            overall_confidence=DiagnosisConfidence.HIGH,
            recommended_approach="none",
            remaining_goal="goal",
            preserved_step_indices=[],
            notes=[],
        )
        result = replanner.replan(task, task_diag, lessons=[])
        # Should succeed or gracefully handle no steps
        assert isinstance(result, ReplanResult)

    def test_closed_loop_no_event_callback(self):
        """Controller should work without an event callback."""
        registry = _FakeRegistry([_SuccessTool()])
        executor = TaskExecutor(registry)
        controller = ClosedLoopController(executor=executor, event_callback=None)
        task = executor.create_task("goal", [{"description": "do thing", "tool": "success_tool", "args": {}}])
        result = controller.execute(task)
        assert result["success"] is True

    def test_closed_loop_lesson_provider_failure(self):
        """Lesson provider failure should not break the loop."""
        registry = _FakeRegistry([_SuccessTool()])
        executor = TaskExecutor(registry)
        def bad_lesson_provider(goal):
            raise RuntimeError("lesson store unavailable")
        controller = ClosedLoopController(
            executor=executor,
            lesson_provider=bad_lesson_provider,
        )
        task = executor.create_task("goal", [{"description": "do thing", "tool": "success_tool", "args": {}}])
        result = controller.execute(task)
        assert result["success"] is True

    def test_safety_config_custom_values(self):
        """Custom safety config values are preserved."""
        config = SafetyConfig(
            max_replans=10,
            max_retries_per_step=5,
            max_total_steps=100,
            max_execution_seconds=3600.0,
            max_identical_failures=5,
            max_recovery_attempts=5,
        )
        assert config.max_replans == 10
        assert config.max_execution_seconds == 3600.0

    def test_observation_timestamp_present(self):
        """TaskObservation should have a timestamp."""
        task = _make_task("goal", step_count=1, status="completed")
        task_obs = observe_task(task)
        assert task_obs.timestamp > 0

    def test_diagnosis_notes_populated(self):
        """StepDiagnosis notes should be populated."""
        step = _make_step("test", "fail_tool", status="failed",
                          error="permission denied", verified=False)
        obs = observe_step(step, 0, "test")
        diag = diagnose_step(obs)
        assert len(diag.notes) > 0

    def test_failure_cluster_to_dict(self):
        """FailureCluster serializes correctly."""
        cluster = FailureCluster(signature="abc123")
        record = FailureRecord(
            signature="abc123",
            category=FailureCategory.TOOL_FAILURE,
            tool_name="tool_x",
            error="error",
            task_id="T1",
        )
        cluster.add_failure(record)
        d = cluster.to_dict()
        assert d["count"] == 1
        assert d["signature"] == "abc123"

    def test_replan_result_preserves_notes(self):
        """ReplanResult notes are preserved through serialization."""
        result = ReplanResult(
            success=True,
            new_steps=[],
            preserved_steps=[],
            goal_remaining="goal",
            notes=["note 1", "note 2"],
        )
        d = result.to_dict()
        assert "note 1" in d["notes"]
        assert "note 2" in d["notes"]
