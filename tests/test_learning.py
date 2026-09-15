"""Tests for VERONICA Learning & Adaptation v1: the Experience model, the
ExperienceStore (JSONL), the LearningEngine, confidence, user feedback, redaction,
and TaskExecutor integration. Dependency-free fakes, no network, no third-party deps.
"""
from __future__ import annotations

import json

import pytest

from veronica.learning import (
    ConfidenceTracker,
    Experience,
    ExperienceOutcome,
    ExperienceStore,
    FeedbackKind,
    FeedbackRecorder,
    LearningEngine,
    SignalType,
    VerificationResult,
    classify_feedback,
    redact_experience,
    redact_value,
)
from veronica.automation.task_executor import TaskExecutor
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

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


class _OpenWebTool(Tool):
    name = "open_website"
    description = "Open a website."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def run(self, url="", **kwargs) -> ToolResult:
        return ToolResult.ok(f"opened {url}", url=url)


class _FlakyTool(Tool):
    name = "flaky"
    description = "Fails then succeeds."
    permission = PermissionLevel.SAFE

    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.calls = 0

    def run(self, **kwargs) -> ToolResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return ToolResult.fail("transient failure")
        return ToolResult.ok("recovered")


class _AlwaysFailTool(Tool):
    name = "always_fail"
    description = "Always fails."
    permission = PermissionLevel.SAFE

    def run(self, **kwargs) -> ToolResult:
        return ToolResult.fail("nope")


def _build_registry():
    reg = ToolRegistry(_FakeGate())
    reg.register(_ReadFileTool())
    reg.register(_OpenWebTool())
    reg.register(_FlakyTool())
    reg.register(_AlwaysFailTool())
    return reg


# ---------------------------------------------------------------------------
# Experience model
# ---------------------------------------------------------------------------

class TestExperienceModel:
    def test_creation_defaults(self):
        e = Experience(goal="do a thing")
        assert e.experience_id
        assert e.outcome == ExperienceOutcome.SUCCESS
        assert e.tool_calls == []
        assert e.retries == 0

    def test_serialization_roundtrip(self):
        e = Experience(
            goal="open readme",
            outcome=ExperienceOutcome.RECOVERED,
            task_id="T123",
            tool_calls=["open_file", "workspace_search"],
            retries=2,
            recovery_actions=["workspace_search"],
            failed_step="open_file",
            failure_reason="file_not_found",
            user_feedback={"kind": "correction", "subject": "Chrome"},
        )
        data = e.to_dict()
        assert data["goal"] == "open readme"
        assert data["outcome"] == "recovered"
        restored = Experience.from_dict(data)
        assert restored.goal == e.goal
        assert restored.outcome == e.outcome
        assert restored.tool_calls == e.tool_calls
        assert restored.user_feedback == e.user_feedback

    def test_explicit_outcome_vocabulary(self):
        assert ExperienceOutcome.SUCCESS == "success"
        assert ExperienceOutcome.FAILED == "failed"
        assert ExperienceOutcome.RECOVERED == "recovered"
        assert ExperienceOutcome.CANCELLED == "cancelled"
        assert VerificationResult.PASSED == "passed"
        assert VerificationResult.FAILED == "failed"


# ---------------------------------------------------------------------------
# Redaction / privacy
# ---------------------------------------------------------------------------

class TestRedaction:
    def test_api_key_redacted(self):
        assert redact_value({"api_key": "sk-abcdef1234567890"}) == {"api_key": "[REDACTED]"}

    def test_authorization_header_redacted(self):
        assert redact_value({"headers": {"Authorization": "Bearer xyz123token"}}) == \
            {"headers": {"Authorization": "[REDACTED]"}}

    def test_password_redacted(self):
        assert redact_value({"config": {"password": "hunter2"}}) == {"config": {"password": "[REDACTED]"}}

    def test_env_secret_name_redacted(self):
        assert redact_value({"VERONICA_LLM_API_KEY": "sk-abc", "keep": "fine"}) == \
            {"VERONICA_LLM_API_KEY": "[REDACTED]", "keep": "fine"}

    def test_secret_value_pattern_redacted(self):
        # A bearer token under a non-secret key is still caught by value pattern.
        assert redact_value({"data": "Bearer abcdefghijklmnopqrstuvwxyz0123456789"}) == \
            {"data": "[REDACTED]"}

    def test_private_key_redacted(self):
        val = redact_value({"key": "-----BEGIN RSA PRIVATE KEY-----\nAAAA"})
        assert val["key"] == "[REDACTED]"

    def test_non_secret_preserved(self):
        assert redact_value({"city": "London", "count": 3, "nested": {"ok": True}}) == \
            {"city": "London", "count": 3, "nested": {"ok": True}}

    def test_redact_experience_never_mutates_input(self):
        e = Experience(goal="x", actions=[{"api_key": "sk-abc"}], context={"password": "pw"})
        original_actions = list(e.actions)
        safe = redact_experience(e)
        assert safe.actions[0]["api_key"] == "[REDACTED]"
        assert safe.context["password"] == "[REDACTED]"
        # Input untouched.
        assert e.actions == original_actions
        assert e.context["password"] == "pw"

    def test_store_never_persists_secrets(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        e = Experience(
            goal="login",
            tool_calls=["fetch_webpage"],
            actions=[{"tool": "fetch_webpage", "args": {"url": "https://x.com", "auth": "Bearer abc"}}],
            context={"VERONICA_LLM_API_KEY": "sk-secret123"},
        )
        store.record(e)
        line = (tmp_path / "exp.jsonl").read_text(encoding="utf-8")
        assert "[REDACTED]" in line
        assert "sk-secret123" not in line
        assert "Bearer abc" not in line


# ---------------------------------------------------------------------------
# ExperienceStore
# ---------------------------------------------------------------------------

class TestExperienceStore:
    def _exp(self, goal="goal", outcome="success", tool_calls=None, **kw):
        return Experience(goal=goal, outcome=outcome, tool_calls=tool_calls or [], **kw)

    def test_record_and_get(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        e = self._exp("open readme", tool_calls=["open_file"])
        assert store.record(e) is True
        got = store.get(e.experience_id)
        assert got is not None
        assert got.goal == "open readme"

    def test_get_missing_returns_none(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        assert store.get("nope") is None

    def test_persistence_across_restarts(self, tmp_path):
        path = tmp_path / "exp.jsonl"
        store1 = ExperienceStore(path)
        store1.record(self._exp("persist me", outcome="failed", failure_reason="x"))
        # New instance reads the same file (simulated process restart).
        store2 = ExperienceStore(path)
        assert store2.count() == 1
        assert store2.list_recent()[0].goal == "persist me"

    def test_list_recent_orders_newest_first(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        store.record(self._exp("a", outcome="success"))
        store.record(self._exp("b", outcome="failed"))
        store.record(self._exp("c", outcome="recovered"))
        recent = store.list_recent(2)
        assert len(recent) == 2

    def test_search_by_goal(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        store.record(self._exp("find the readme file", tool_calls=["find_file"]))
        store.record(self._exp("open the weather", tool_calls=["get_weather"]))
        assert len(store.search("readme")) == 1
        assert store.search("readme")[0].goal == "find the readme file"

    def test_filter_by_outcome_and_tool(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        store.record(self._exp("g1", outcome="success", tool_calls=["open_file"]))
        store.record(self._exp("g2", outcome="failed", tool_calls=["open_file"]))
        store.record(self._exp("g3", outcome="failed", tool_calls=["open_website"]))
        assert len(store.filter(outcome="failed")) == 2
        assert len(store.filter(outcome="failed", tool="open_file")) == 1
        assert len(store.filter(tool="open_website")) == 1

    def test_count_by_outcome(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        store.record(self._exp("a", outcome="success"))
        store.record(self._exp("b", outcome="failed"))
        store.record(self._exp("c", outcome="recovered"))
        counts = store.count_by_outcome()
        assert counts["success"] == 1
        assert counts["failed"] == 1
        assert counts["recovered"] == 1
        assert counts["cancelled"] == 0

    def test_missing_file_is_empty(self, tmp_path):
        store = ExperienceStore(tmp_path / "does_not_exist.jsonl")
        assert store.count() == 0
        assert store.list_recent() == []
        assert store.get("x") is None

    def test_corrupted_storage_is_skipped(self, tmp_path):
        path = tmp_path / "exp.jsonl"
        path.write_text(
            '{"goal": "good one", "outcome": "success", "experience_id": "abc"}\n'
            'THIS IS NOT VALID JSON\n'
            '{"goal": "another good", "outcome": "failed", "experience_id": "def"}\n',
            encoding="utf-8",
        )
        store = ExperienceStore(path)
        assert store.count() == 2
        assert store.get("abc") is not None
        assert store.get("def") is not None

    def test_reset(self, tmp_path):
        path = tmp_path / "exp.jsonl"
        store = ExperienceStore(path)
        store.record(self._exp("a"))
        assert store.count() == 1
        assert store.reset() is True
        assert store.count() == 0


# ---------------------------------------------------------------------------
# LearningEngine — signal detection
# ---------------------------------------------------------------------------

class TestLearningEngine:
    def test_success_signal(self):
        eng = LearningEngine()
        signals = eng.process(Experience(goal="g", outcome="success", tool_calls=["read_file"]))
        assert any(s.type == SignalType.STRATEGY_SUCCESS and s.subject == "read_file" for s in signals)

    def test_failure_signal(self):
        eng = LearningEngine()
        signals = eng.process(Experience(
            goal="g", outcome="failed", tool_calls=["open_file"],
            failed_step="open_file", failure_reason="file_not_found"))
        assert any(s.type == SignalType.STRATEGY_FAILURE and s.subject == "open_file" for s in signals)

    def test_recovery_signal(self):
        eng = LearningEngine()
        signals = eng.process(Experience(
            goal="g", outcome="recovered", tool_calls=["open_file"],
            recovery_actions=["workspace_search"], retries=1))
        assert any(s.type == SignalType.RECOVERY and s.subject == "workspace_search" for s in signals)

    def test_retry_signal(self):
        eng = LearningEngine()
        signals = eng.process(Experience(goal="g", outcome="success", tool_calls=["open_file"], retries=2))
        assert any(s.type == SignalType.RETRY for s in signals)

    def test_cancellation_signal(self):
        eng = LearningEngine()
        signals = eng.process(Experience(goal="g", outcome="cancelled"))
        assert any(s.type == SignalType.CANCELLATION for s in signals)

    def test_user_correction_signal(self):
        eng = LearningEngine()
        exp = Experience(goal="g", outcome="success", tool_calls=["open_file"],
                         user_feedback={"kind": "correction", "subject": "Chrome", "text": "use Chrome instead"})
        signals = eng.process(exp)
        assert any(s.type == SignalType.USER_CORRECTION and s.subject == "Chrome" for s in signals)

    def test_signal_carries_evidence_id(self):
        eng = LearningEngine()
        exp = Experience(goal="g", outcome="success", tool_calls=["read_file"])
        signals = eng.process(exp)
        assert all(s.evidence_experience_id == exp.experience_id for s in signals)

    def test_confidence_increases_with_success(self):
        tracker = ConfidenceTracker()
        tracker.record("read_file", True)
        c1 = tracker.confidence("read_file")
        for _ in range(10):
            tracker.record("read_file", True)
        assert tracker.confidence("read_file") > c1

    def test_confidence_decreases_with_failure(self):
        tracker = ConfidenceTracker()
        for _ in range(10):
            tracker.record("web_search", True)
        high = tracker.confidence("web_search")
        for _ in range(8):
            tracker.record("web_search", False)
        assert tracker.confidence("web_search") < high

    def test_confidence_unobserved_is_neutral(self):
        tracker = ConfidenceTracker()
        assert tracker.confidence("never_seen") == 0.5

    def test_engine_updates_confidence_and_ranks(self):
        eng = LearningEngine()
        for _ in range(5):
            eng.process(Experience(goal="g", outcome="success", tool_calls=["read_file"]))
        eng.process(Experience(goal="g", outcome="failed", tool_calls=["read_file"], failed_step="read_file"))
        best = eng.best_strategies()
        assert best and best[0]["subject"] == "read_file"
        assert best[0]["successes"] == 5
        assert best[0]["failures"] == 1


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

class TestFeedback:
    def test_correction_parsed(self):
        fb = classify_feedback("No, use Chrome instead.")
        assert fb is not None
        assert fb.kind == FeedbackKind.CORRECTION
        assert fb.subject == "Chrome"

    def test_preference_parsed(self):
        fb = classify_feedback("I prefer using VS Code.")
        assert fb is not None
        assert fb.kind == FeedbackKind.PREFERENCE

    def test_dont_do_that_parsed(self):
        fb = classify_feedback("Don't open Spotify.")
        assert fb is not None
        assert fb.kind == FeedbackKind.DONT_DO_THAT

    def test_positive_parsed(self):
        fb = classify_feedback("Great job, perfect.")
        assert fb is not None
        assert fb.kind == FeedbackKind.POSITIVE

    def test_conversational_noise_is_ignored(self):
        assert classify_feedback("what's the weather like today") is None
        assert classify_feedback("") is None

    def test_feedback_recorder_creates_signal(self):
        rec = FeedbackRecorder()
        fb = rec.process("use Chrome instead", experience_id="exp1")
        assert fb is not None
        assert fb.signal is not None
        assert fb.signal["type"] == "user_correction"
        assert rec.recorded == [fb]

    def test_feedback_recorder_ignores_noise(self):
        rec = FeedbackRecorder()
        assert rec.process("tell me a joke") is None
        assert rec.recorded == []


# ---------------------------------------------------------------------------
# TaskExecutor integration
# ---------------------------------------------------------------------------

class TestExecutorIntegration:
    def _capture(self):
        experiences = []
        return experiences, (lambda e: experiences.append(e))

    def test_success_creates_experience(self):
        ex = TaskExecutor(_build_registry())
        seen = []
        ex.experience_callback = seen.append
        task = ex.create_task("read a file", [
            {"description": "read", "tool": "read_file", "args": {"file_path": "/tmp/a.txt"}}])
        ex.execute_task(task)
        assert len(seen) == 1
        assert seen[0].outcome == ExperienceOutcome.SUCCESS
        assert seen[0].goal == "read a file"
        assert "read_file" in seen[0].tool_calls

    def test_failed_task_creates_failure_experience(self):
        ex = TaskExecutor(_build_registry())
        seen = []
        ex.experience_callback = seen.append
        task = ex.create_task("doomed", [
            {"description": "fail", "tool": "always_fail", "args": {}}])
        ex.execute_task(task)
        assert len(seen) == 1
        assert seen[0].outcome == ExperienceOutcome.FAILED
        assert seen[0].failed_step == "always_fail"
        assert seen[0].retries > 0

    def test_recovered_task_creates_recovery_experience(self):
        ex = TaskExecutor(_build_registry())
        seen = []
        ex.experience_callback = seen.append
        task = ex.create_task("recover", [
            {"description": "flaky", "tool": "flaky", "args": {}}])
        ex.execute_task(task)
        assert len(seen) == 1
        assert seen[0].outcome == ExperienceOutcome.RECOVERED
        assert "flaky" in seen[0].recovery_actions

    def test_cancelled_task_creates_cancellation_experience(self):
        ex = TaskExecutor(_build_registry())
        seen = []
        ex.experience_callback = seen.append
        task = ex.create_task("cancel", [
            {"description": "one", "tool": "read_file", "args": {"file_path": "/a"}}])
        ex.cancel_task(task.task_id)
        ex.execute_task(task)
        assert len(seen) == 1
        assert seen[0].outcome == ExperienceOutcome.CANCELLED

    def test_learning_failure_does_not_fail_task(self):
        def boom(exp):
            raise RuntimeError("learning backend exploded")
        ex = TaskExecutor(_build_registry(), experience_callback=boom)
        task = ex.create_task("still runs", [
            {"description": "read", "tool": "read_file", "args": {"file_path": "/tmp/a.txt"}}])
        outcome = ex.execute_task(task)
        # Task completes normally despite the learning hook raising.
        assert outcome["success"] is True
        assert task.status == "completed"

    def test_end_to_end_store_and_engine_wired(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp.jsonl")
        engine = LearningEngine(ConfidenceTracker())
        ex = TaskExecutor(_build_registry(), experience_callback=lambda e: (store.record(e), engine.process(e)))
        task = ex.create_task("research", [
            {"description": "read", "tool": "read_file", "args": {"file_path": "/a.txt"}},
            {"description": "open", "tool": "open_website", "args": {"url": "https://example.com"}},
        ])
        ex.execute_task(task)
        assert store.count() == 1
        recorded = store.list_recent()[0]
        assert recorded.outcome == ExperienceOutcome.SUCCESS
        assert recorded.tool_calls == ["read_file", "open_website"]
        # Reload from disk (persistence across restart).
        store2 = ExperienceStore(tmp_path / "exp.jsonl")
        assert store2.count() == 1
        assert engine.signals_of_type(SignalType.STRATEGY_SUCCESS)
