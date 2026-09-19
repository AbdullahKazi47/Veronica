"""
Tests for Episodic Memory Integration V1.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from veronica.memory.thread import (
    EpisodeBuilder,
    EpisodeRecorder,
    EpisodeRetriever,
    EpisodeStatus,
    ExecutionStatus,
    ProcedureCandidate,
    ProcedureState,
    ProcedureStore,
    VerificationStatus,
    derive_episode_status,
    derive_execution_status,
    derive_verification_status,
    map_task_status,
)
from veronica.memory.thread.models import Episode, MemoryRecord, MemoryType
from veronica.memory.thread.thread_memory import ThreadMemory
from veronica.learning.experience import Experience, ExperienceOutcome, VerificationResult


# Helper to build a minimal Experience
def build_experience(
    goal: str = "test goal",
    outcome: ExperienceOutcome = ExperienceOutcome.SUCCESS,
    final_status: str = "completed",
    steps: List[Dict[str, Any]] | None = None,
    tool_calls: List[str] | None = None,
    verification: VerificationResult = VerificationResult.PASSED,
    retries: int = 0,
    recovery_actions: List[str] | None = None,
    duration: float = 0.1,
    failed_step: str = "",
    failure_reason: str = "",
    adaptation_applied: bool = False,
) -> Experience:
    if steps is None:
        steps = [{"description": "step", "tool": "test_tool", "args": {}, "status": "completed", "verified": True, "attempts": 1}]
    if tool_calls is None:
        tool_calls = ["test_tool"]
    if recovery_actions is None:
        recovery_actions = []
    exp = Experience(
        goal=goal,
        outcome=outcome,
        task_id="T1",
        final_status=final_status,
        plan=steps,
        actions=steps,
        tool_calls=tool_calls,
        verification=verification,
        retries=retries,
        recovery_actions=recovery_actions,
        duration=duration,
        failed_step=failed_step,
        failure_reason=failure_reason,
        original_plan=None,
        adaptations=[],
        adaptation_applied=adaptation_applied,
        adaptation_success=None,
    )
    return exp


def test_status_vocabularies():
    assert EpisodeStatus.SUCCESS == "success"
    assert EpisodeStatus.FAILED == "failed"
    assert EpisodeStatus.STREAM_FAILURE == "stream_failure"   # distinct from task FAILED (Phase 20)
    assert EpisodeStatus.BLOCKED == "blocked"
    assert EpisodeStatus.CANCELLED == "cancelled"
    assert EpisodeStatus.UNKNOWN == "unknown"

    assert ExecutionStatus.EXECUTED == "executed"
    assert ExecutionStatus.PARTIAL == "partial"
    assert ExecutionStatus.NOT_EXECUTED == "not_executed"
    assert ExecutionStatus.UNKNOWN == "unknown"

    assert VerificationStatus.VERIFIED == "verified"
    assert VerificationStatus.FAILED == "failed"
    assert VerificationStatus.UNKNOWN == "unknown"
    assert VerificationStatus.NOT_APPLICABLE == "not_applicable"


def test_map_task_status():
    assert map_task_status("completed") == EpisodeStatus.SUCCESS
    assert map_task_status("failed") == EpisodeStatus.FAILED
    assert map_task_status("blocked") == EpisodeStatus.BLOCKED
    assert map_task_status("cancelled") == EpisodeStatus.CANCELLED
    assert map_task_status("stream_failed") == EpisodeStatus.STREAM_FAILURE  # Phase 20
    assert map_task_status("stream_error") == EpisodeStatus.STREAM_FAILURE   # Phase 20
    assert map_task_status("unknown") == EpisodeStatus.UNKNOWN
    assert map_task_status("needs_review") == EpisodeStatus.UNKNOWN
    assert map_task_status("") == EpisodeStatus.UNKNOWN
    assert map_task_status(None) == EpisodeStatus.UNKNOWN
    assert map_task_status("random") == EpisodeStatus.UNKNOWN


def test_derive_execution_status():
    assert derive_execution_status([]) == ExecutionStatus.UNKNOWN
    assert derive_execution_status([{"status": "completed"}]) == ExecutionStatus.EXECUTED
    assert derive_execution_status([{"status": "failed"}]) == ExecutionStatus.EXECUTED
    assert derive_execution_status([{"status": "completed"}, {"status": "failed"}]) == ExecutionStatus.PARTIAL
    assert derive_execution_status([{"status": "pending"}, {"status": "running"}]) == ExecutionStatus.NOT_EXECUTED


def test_derive_verification_status():
    assert derive_verification_status([]) == VerificationStatus.UNKNOWN
    assert derive_verification_status([{"status": "completed", "verified": True}]) == VerificationStatus.VERIFIED
    assert derive_verification_status([{"status": "completed", "verified": False}]) == VerificationStatus.FAILED
    assert derive_verification_status([{"status": "completed", "verified": True}, {"status": "completed", "verified": False}]) == VerificationStatus.UNKNOWN
    assert derive_verification_status([{"status": "pending"}]) == VerificationStatus.NOT_APPLICABLE
    assert derive_verification_status([{"status": "completed", "verified": True}, {"status": "pending"}]) == VerificationStatus.VERIFIED


def test_derive_episode_status():
    # Explicit CANCELLED/BLOCKED/STREAM_FAILURE win
    assert derive_episode_status("cancelled", "not_executed", "unknown") == EpisodeStatus.CANCELLED
    assert derive_episode_status("blocked", "not_executed", "unknown") == EpisodeStatus.BLOCKED
    # Phase 20: a stream failure is its own status, never collapsed into task FAILED
    assert derive_episode_status("stream_failed", "not_executed", "unknown") == EpisodeStatus.STREAM_FAILURE
    assert derive_episode_status("stream_failed", "executed", "verified") == EpisodeStatus.STREAM_FAILURE
    assert derive_episode_status("stream_error", "not_executed", "unknown") == EpisodeStatus.STREAM_FAILURE

    # Nothing executed -> FAILED if system says failed, else UNKNOWN
    assert derive_episode_status("failed", "not_executed", "unknown") == EpisodeStatus.FAILED
    assert derive_episode_status("unknown", "not_executed", "unknown") == EpisodeStatus.UNKNOWN

    # Executed + verified -> SUCCESS
    assert derive_episode_status("completed", "executed", "verified") == EpisodeStatus.SUCCESS

    # Executed + failed -> FAILED
    assert derive_episode_status("failed", "executed", "failed") == EpisodeStatus.FAILED
    assert derive_episode_status("completed", "executed", "failed") == EpisodeStatus.FAILED  # execution failed overrides

    # Executed + verification unknown -> UNKNOWN (never promoted)
    assert derive_episode_status("completed", "executed", "unknown") == EpisodeStatus.UNKNOWN
    assert derive_episode_status("completed", "executed", "") == EpisodeStatus.UNKNOWN

    # UNKNOWN stays UNKNOWN
    assert derive_episode_status("unknown", "unknown", "unknown") == EpisodeStatus.UNKNOWN


def test_episode_builder_basic():
    exp = build_experience(goal="make tea", outcome=ExperienceOutcome.SUCCESS, final_status="completed")
    builder = EpisodeBuilder()
    episode = builder.build(exp, task_type="GENERAL", model_id="test:model", provider="test", routing_role="fast")
    assert episode.goal == "make tea"
    assert episode.episode_id and len(episode.episode_id) == 12
    assert episode.episode_status == EpisodeStatus.SUCCESS
    assert episode.execution_status == ExecutionStatus.EXECUTED
    assert episode.verification_status == VerificationStatus.VERIFIED
    assert episode.task_type == "GENERAL"
    assert episode.plan_summary == "test_tool: step"
    assert episode.actions == ["step"]
    assert episode.tools_used == ["test_tool"]
    assert episode.result == "completed"  # falls back to final_status when no explicit result_summary
    assert episode.success is True
    assert episode.verification == "passed"
    assert episode.model_id == "test:model"
    assert episode.provider == "test"
    assert episode.routing_role == "fast"


def test_episode_builder_failure():
    exp = build_experience(
        goal="fail task",
        outcome=ExperienceOutcome.FAILED,
        final_status="failed",
        steps=[{"description": "failing step", "tool": "fail_tool", "args": {}, "status": "failed", "verified": False, "attempts": 1, "error": "boom"}],
        verification=VerificationResult.FAILED,
    )
    builder = EpisodeBuilder()
    episode = builder.build(exp)
    assert episode.episode_status == EpisodeStatus.FAILED
    assert episode.execution_status == ExecutionStatus.EXECUTED
    assert episode.verification_status == VerificationStatus.FAILED
    assert len(episode.failures) == 1
    assert episode.failures[0]["action"] == "failing step"
    assert episode.failures[0]["tool"] == "fail_tool"
    assert episode.failures[0]["error"] == "boom"
    assert episode.failures[0]["category"] == "execution_failed"  # default category
    assert episode.observations == []
    assert episode.verification_results == []


def test_episode_builder_recovery():
    exp = build_experience(
        goal="recover task",
        outcome=ExperienceOutcome.RECOVERED,
        final_status="completed",
        steps=[
            {"description": "first try", "tool": "tool1", "args": {}, "status": "failed", "verified": False, "attempts": 2, "error": "boom"},
            {"description": "second try", "tool": "tool2", "args": {}, "status": "completed", "verified": True, "attempts": 1},
        ],
        verification=VerificationResult.PASSED,
        retries=1,
        recovery_actions=["tool2"],
    )
    builder = EpisodeBuilder()
    episode = builder.build(exp)
    # Sanity check: one failed + one completed step is a PARTIAL execution, which the
    # derivation rule maps to FAILED even though the task eventually recovered. The
    # recovery evidence is recorded separately in `recoveries`.
    assert episode.episode_status == EpisodeStatus.FAILED
    assert episode.execution_status == ExecutionStatus.PARTIAL  # one step failed, one completed
    assert episode.verification_status == VerificationStatus.VERIFIED
    assert episode.recovery_attempts == 1
    assert len(episode.recoveries) == 1
    assert episode.recoveries[0]["attempt"] == 1
    assert episode.recoveries[0]["tool"] == "tool2"
    assert episode.recoveries[0]["result"] == "succeeded"
    assert episode.actions == ["first try", "second try"]
    assert episode.tools_used == ["tool1", "tool2"]


def test_episode_builder_size_limits():
    # Too many actions
    many_actions = [{"description": f"step {i}", "tool": f"tool{i}", "args": {}, "status": "completed", "verified": True, "attempts": 1} for i in range(60)]
    exp = build_experience(goal="x" * 600, steps=many_actions)
    builder = EpisodeBuilder()
    episode = builder.build(exp)
    assert len(episode.actions) == 50  # capped
    assert len(episode.tools_used) == 50
    assert len(episode.goal) == 500  # capped
    assert len(episode.plan_summary) <= 1200  # capped at MAX; not padded to fill it
    # observations and verification_results also capped via failures length (same as actions)


def test_episode_to_memory_records():
    exp = build_experience(goal="test", outcome=ExperienceOutcome.SUCCESS, final_status="completed")
    builder = EpisodeBuilder()
    episode = builder.build(exp, task_type="TEST")
    records = episode.to_memory_records()
    assert len(records) >= 1  # at least the episode record
    exp_rec = next(r for r in records if r.memory_type == MemoryType.EXPERIENCE)
    assert exp_rec.content == "test → " or exp_rec.content.startswith("test")
    assert exp_rec.metadata["episode_status"] == EpisodeStatus.SUCCESS
    assert exp_rec.metadata["execution_status"] == ExecutionStatus.EXECUTED
    assert exp_rec.metadata["verification_status"] == VerificationStatus.VERIFIED
    assert exp_rec.metadata["task_type"] == "TEST"
    # Procedural record added when success + verified + tools used
    if episode.success and episode.verification == "passed" and episode.tools_used:
        assert len(records) == 2
        proc_rec = next(r for r in records if r.memory_type == MemoryType.PROCEDURE)
        assert proc_rec.metadata["success"] is True


def test_episode_recorder_fault_isolation():
    # When thread_memory is None, recorder returns [] and does not raise
    recorder = EpisodeRecorder(None)
    exp = build_experience()
    records = recorder.record(exp)
    assert records == []


def test_episode_recorder_records_and_emits():
    emitted = []
    def callback(signal, args):
        emitted.append((signal, args))

    # Build a thread_memory with temp paths
    import tempfile
    with tempfile.NamedTemporaryFile() as records_file, tempfile.NamedTemporaryFile() as vector_file:
        tm = ThreadMemory.build(
            records_path=records_file.name,
            vector_path=vector_file.name,
        )
        recorder = EpisodeRecorder(tm, event_callback=callback)
        exp = build_experience(goal="record me", outcome=ExperienceOutcome.SUCCESS, final_status="completed")
        records = recorder.record(exp)
        assert len(records) >= 1
        # Check that events were emitted
        signals = [sig for sig, _ in emitted]
        assert "episode_started" in signals
        assert "episode_completed" in signals
        assert "memory_written" in signals
        # Last episode stored on recorder
        assert recorder.last_episode is not None
        assert recorder.last_episode.goal == "record me"


def test_episode_retriever_fault_isolation():
    retriever = EpisodeRetriever(None)
    assert retriever.retrieve("anything") == []


def test_episode_retriever_returns_formatted():
    import tempfile
    with tempfile.NamedTemporaryFile() as records_file, tempfile.NamedTemporaryFile() as vector_file:
        tm = ThreadMemory.build(
            records_path=records_file.name,
            vector_path=vector_file.name,
        )
        # Store an experience record directly to simulate a past episode
        tm.remember(
            content="past goal → past result",
            memory_type=MemoryType.EXPERIENCE,
            importance=0.6,
            confidence=0.7,
            provenance="experience",
            source="episode:test",
            tags=[],
            project="",
            metadata={
                "episode_status": "success",
                "execution_status": "executed",
                "verification_status": "verified",
                "task_type": "GENERAL",
                "model_id": "test:m",
                "provider": "p",
                "routing_role": "fast",
                "success": True,
                "verification": "passed",
                "tools_used": ["tool1"],
                "num_errors": 0,
            },
        )
        retriever = EpisodeRetriever(tm, default_top_k=3)
        picked = retriever.retrieve("past goal", top_k=2)
        assert len(picked) == 1  # only one matching
        mem, score = picked[0]
        assert mem.content == "past goal → past result"
        assert score > 0
        ctx = retriever.format_context(picked)
        assert "[RETRIEVED EXPERIENCE" in ctx
        assert "[success/verified]" in ctx
        assert "past goal → past result" in ctx
        assert "score=" in ctx


def test_procedure_candidate_basic():
    cand = ProcedureCandidate()
    assert cand.state == ProcedureState.OBSERVED
    assert cand.success_count == 0
    assert cand.failure_count == 0
    assert cand.confidence == 0.0
    # to_record/from_record roundtrip
    rec = cand.to_record()
    cand2 = ProcedureCandidate.from_record(rec)
    assert cand2.procedure_id == cand.procedure_id
    assert cand2.goal_pattern == cand.goal_pattern
    assert cand2.steps == cand.steps
    assert cand2.tools == cand.tools
    assert cand2.success_count == cand.success_count
    assert cand2.failure_count == cand.failure_count
    assert cand2.state == cand.state


def test_procedure_candidate_promotion():
    cand = ProcedureCandidate(goal_pattern="test goal", steps=["step1"], tools=["tool1"])
    # First success -> still OBSERVED (need 2 for PROMISING)
    cand.success_count = 1
    cand._recompute()
    assert cand.state == ProcedureState.OBSERVED
    # Second success -> PROMISING
    cand.success_count = 2
    cand._recompute()
    assert cand.state == ProcedureState.PROMISING
    # Third success -> VALIDATED (if no failures)
    cand.success_count = 3
    cand._recompute()
    assert cand.state == ProcedureState.VALIDATED
    # Add a failure -> DEGRADED (failures >= successes and >= degrade_at)
    cand.failure_count = 3
    cand._recompute()
    assert cand.state == ProcedureState.DEGRADED


def test_procedure_store_fault_isolation():
    store = ProcedureStore(None)
    exp = build_experience()
    builder = EpisodeBuilder()
    episode = builder.build(exp)
    # Should not raise, returns None
    assert store.update_from(episode) is None


def test_procedure_store_records_and_emits():
    import tempfile
    with tempfile.NamedTemporaryFile() as records_file, tempfile.NamedTemporaryFile() as vector_file:
        tm = ThreadMemory.build(
            records_path=records_file.name,
            vector_path=vector_file.name,
        )
        emitted = []
        def callback(signal, args):
            emitted.append((signal, args))
        store = ProcedureStore(tm, event_callback=callback)
        # Three successful episodes with same goal and tool should create a VALIDATED procedure
        exp = build_experience(goal="proc goal", outcome=ExperienceOutcome.SUCCESS, final_status="completed", steps=[{"description": "step", "tool": "proc_tool", "args": {}, "status": "completed", "verified": True, "attempts": 1}])
        builder = EpisodeBuilder()
        for _ in range(3):
            episode = builder.build(exp, task_type="TEST")
            store.update_from(episode)
        # Now retrieve procedures for the goal
        cand = store.get_candidates("proc goal", top_k=1)[0]
        assert cand.state == ProcedureState.VALIDATED
        assert cand.goal_pattern == "proc goal"
        assert cand.tools == ["proc_tool"]
        assert cand.success_count == 3
        assert cand.failure_count == 0
        # Events emitted
        signals = [sig for sig, _ in emitted]
        assert "procedure_promoted" in signals


def test_orchestrator_episodic_wiring():
    from veronica.config import Config, load_config
    from veronica.main import build_app
    config = load_config()
    config.memory_thread.enabled = True
    app = build_app(config, interactive=False)
    # Orchestrator should have the episodic components attached
    assert hasattr(app.orchestrator, "episode_recorder")
    assert hasattr(app.orchestrator, "episode_retriever")
    assert hasattr(app.orchestrator, "procedure_store")
    assert app.orchestrator.episode_recorder is not None
    assert app.orchestrator.episode_retriever is not None
    assert app.orchestrator.procedure_store is not None


if __name__ == "__main__":
    # Run a quick smoke
    test_status_vocabularies()
    test_map_task_status()
    test_derive_execution_status()
    test_derive_verification_status()
    test_derive_episode_status()
    test_episode_builder_basic()
    test_episode_builder_failure()
    test_episode_builder_recovery()
    test_episode_builder_size_limits()
    test_episode_to_memory_records()
    test_episode_recorder_fault_isolation()
    test_episode_recorder_records_and_emits()
    test_episode_retriever_fault_isolation()
    test_episode_retriever_returns_formatted()
    test_procedure_candidate_basic()
    test_procedure_candidate_promotion()
    test_procedure_store_fault_isolation()
    test_procedure_store_records_and_emits()
    test_orchestrator_episodic_wiring()
    print("All internal tests passed.")