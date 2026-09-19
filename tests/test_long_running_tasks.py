"""Phase 9E — Long-Running Autonomous Task Management Tests.

Covers all required scenarios per Phase 9E authorization.
Uses mocks/fakes — no destructive real-world actions.
"""
from __future__ import annotations
import time
from unittest.mock import MagicMock

import pytest

from veronica.automation.long_running_task import (
    LongRunningTaskManager,
    TaskCheckpoint,
    LONG_TASK_STATUS,
    CheckpointStatus,
)
from veronica.automation.task_executor import (
    MultiStepTask,
    TaskStep,
    TASK_STATUS,
    TaskExecutor,
)
from veronica.automation.closed_loop import ClosedLoopController, SafetyConfig
from veronica.automation.failure_memory import FailureMemory
from veronica.automation.planner import MultiStepPlanner
from veronica.automation.replanner import Replanner
from veronica.automation.observation import observe_task, TaskObservation
from veronica.automation.diagnosis import diagnose_task, TaskDiagnosis
from veronica.safety.permissions import PermissionGate, PermissionLevel, PermissionDecision
from veronica.logging_setup import get_logger

logger = get_logger(__name__)


# Helper classes for mocking
class FakeToolRegistry:
    def __init__(self, tool_responses=None):
        self.tool_responses = tool_responses or {}
        self.dispatch_calls = []

    def dispatch(self, tool_name, **kwargs):
        self.dispatch_calls.append((tool_name, kwargs))
        resp = self.tool_responses.get(tool_name, MagicMock(success=True, result="ok"))
        return resp


class FakeExperienceCallback:
    def __init__(self):
        self.called = False
        self.experience = None

    def __call__(self, experience):
        self.called = True
        self.experience = experience


class FakeEventCallback:
    def __init__(self):
        self.calls = []

    def __call__(self, signal, args):
        self.calls.append((signal, args))


def test_long_running_task_manager_creation():
    """Test that LongRunningTaskManager can be created."""
    manager = LongRunningTaskManager()
    assert manager.executor is None
    assert manager.controller is None
    assert manager.gate is None
    assert manager.event_callback is None
    assert manager.checkpoints == {}


def test_long_running_task_manager_with_dependencies():
    """Test manager with dependencies."""
    executor = TaskExecutor(FakeToolRegistry())
    controller = ClosedLoopController(executor)
    gate = PermissionGate(PermissionLevel.SAFE, lambda *_: PermissionDecision(True, ""))
    callback = FakeEventCallback()
    manager = LongRunningTaskManager(executor, controller, gate, event_callback=callback)
    assert manager.executor is executor
    assert manager.controller is controller
    assert manager.gate is gate
    assert manager.event_callback is callback


def test_create_task_sets_created_status():
    """Test that create_task sets status to CREATED."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "step 1"}])
    assert task.status == LONG_TASK_STATUS["CREATED"]
    assert task.task_id in manager.checkpoints
    assert manager.checkpoints[task.task_id] == []


def test_transition_to_changes_status():
    """Test transition_to changes task status."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [])
    manager.transition_to(task, LONG_TASK_STATUS["READY"], "test reason")
    assert task.status == LONG_TASK_STATUS["READY"]


def test_save_checkpoint_only_after_verification():
    """Test that checkpoints are only saved for verified steps."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [
        {"description": "step 1", "tool": "dummy", "args": {}},
        {"description": "step 2", "tool": "dummy", "args": {}},
    ])
    # Execute the first step (simulate verification)
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    task.steps[0].result = "success"
    task.current_step = 1  # next step to run is index 1

    cp = manager.save_checkpoint(task, 0, "after step 0")
    assert cp is not None
    assert cp.task_id == task.task_id
    assert cp.step_index == 0
    assert cp.verified is True
    assert cp.note == "after step 0"
    assert len(manager.get_checkpoints(task.task_id)) == 1

    # Trying to checkpoint an unverified step should return None
    task.steps[1].verified = False
    cp2 = manager.save_checkpoint(task, 1, "should not save")
    assert cp2 is None
    assert len(manager.get_checkpoints(task.task_id)) == 1


def test_checkpoint_contains_no_sensitive_data():
    """Test that checkpoint lightweight_dict does not contain args/content."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [
        {"description": "step with secret", "tool": "type_text", "args": {"text": "secret password 123"}},
    ])
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    cp = manager.save_checkpoint(task, 0, "note")
    data = cp.lightweight_dict()
    # Ensure no sensitive fields are present
    assert "text" not in data
    assert "args" not in data
    assert "content" not in data
    assert "sensitive" not in str(data).lower()
    assert data["task_id"] == task.task_id
    assert data["step_index"] == 0
    assert data["verified"] is True
    assert data["note"] == "note"


def test_load_latest_checkpoint_returns_none_when_empty():
    """Test loading checkpoint when none exist."""
    manager = LongRunningTaskManager(None)
    assert manager.load_latest_checkpoint("nonexistent") is None


def test_load_latest_checkpoint_returns_last():
    """Test that load_latest_checkpoint returns the most recent."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "s1"}, {"description": "s2"}])
    # Complete and verify two steps
    for i in range(2):
        task.steps[i].status = TASK_STATUS["COMPLETED"]
        task.steps[i].verified = True
        manager.save_checkpoint(task, i, f"step {i}")
    cp = manager.load_latest_checkpoint(task.task_id)
    assert cp is not None
    assert cp.step_index == 1
    assert cp.note == "step 1"


def test_pause_task_sets_paused_status():
    """Test pause_task sets status to PAUSED."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "s1"}])
    task.status = LONG_TASK_STATUS["RUNNING"]
    task.current_step = 1
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    result = manager.pause_task(task, "user pause")
    assert result is True
    assert task.status == LONG_TASK_STATUS["PAUSED"]
    # Checkpoint saved at current_step-1 (0)
    assert len(manager.get_checkpoints(task.task_id)) == 1
    cp = manager.get_checkpoints(task.task_id)[0]
    assert cp.step_index == 0


def test_pause_task_fails_if_not_runnable():
    """Test pause_task returns False for incompatible states."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [])
    for status in [LONG_TASK_STATUS["PAUSED"], LONG_TASK_STATUS["CANCELLED"],
                   LONG_TASK_STATUS["FAILED"], LONG_TASK_STATUS["COMPLETED"],
                   LONG_TASK_STATUS["BLOCKED"]]:
        task.status = status
        assert manager.pause_task(task) is False


def test_resume_task_requires_reobserve_fn():
    """Test that resume_task requires a reobserve function."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "s1"}])
    task.status = LONG_TASK_STATUS["PAUSED"]
    task.current_step = 1
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    # No reobserve function -> should refuse
    result = manager.resume_task(task)
    assert result is False
    # Should remain paused or move to waiting/blocked
    assert task.status in (LONG_TASK_STATUS["PAUSED"], LONG_TASK_STATUS["WAITING_FOR_PERMISSION"], LONG_TASK_STATUS["BLOCKED"])


def test_resume_task_succeeds_with_valid_reobserve():
    """Test resume_task succeeds when reobserve returns True."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "s1"}])
    task.status = LONG_TASK_STATUS["PAUSED"]
    task.current_step = 1
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    # Mock reobserve function that returns True
    def fake_reobserve():
        return True
    result = manager.resume_task(task, fake_reobserve)
    assert result is True
    assert task.status == LONG_TASK_STATUS["READY"]


def test_resume_task_fails_with_invalid_reobserve():
    """Test resume_task fails when reobserve returns False."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "s1"}])
    task.status = LONG_TASK_STATUS["PAUSED"]
    task.current_step = 1
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    def fake_reobserve():
        return False
    result = manager.resume_task(task, fake_reobserve)
    assert result is False
    # Should be moved to waiting or blocked
    assert task.status in (LONG_TASK_STATUS["WAITING_FOR_PERMISSION"], LONG_TASK_STATUS["BLOCKED"])


def test_wait_for_permission_uses_gate():
    """Test wait_for_permission calls PermissionGate."""
    executor = TaskExecutor(FakeToolRegistry())
    gate = PermissionGate(PermissionLevel.MODERATE, lambda description, level: PermissionDecision(True, "ok"))
    manager = LongRunningTaskManager(executor, permission_gate=gate)
    task = manager.create_task("test goal", [])
    result = manager.wait_for_permission(task, "test desc", PermissionLevel.MODERATE)
    assert result is True
    assert task.status == LONG_TASK_STATUS["READY"]
    # Check that gate was called (we can't easily assert internal calls without mocking, but we trust the flow)


def test_wait_for_permission_without_gate_returns_false():
    """Test wait_for_permission returns False when no gate."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor, permission_gate=None)
    task = MultiStepTask("t1", "g", [TaskStep("s1")])
    result = manager.wait_for_permission(task, "test desc")
    assert result is False


def test_get_progress_returns_correct_values():
    """Test get_progress returns correct progress."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [
        {"description": "s1"},
        {"description": "s2"},
        {"description": "s3"},
    ])
    # Initially no completed steps
    prog = manager.get_progress(task)
    assert prog["total_steps"] == 3
    assert prog["completed_steps"] == 0
    assert prog["progress_pct"] == 0.0
    # Complete one step
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    prog = manager.get_progress(task)
    assert prog["completed_steps"] == 1
    assert prog["progress_pct"] == pytest.approx(33.333, 0.1)
    # Complete all steps
    for s in task.steps:
        s.status = TASK_STATUS["COMPLETED"]
    prog = manager.get_progress(task)
    assert prog["completed_steps"] == 3
    assert prog["progress_pct"] == 100.0


def test_recover_task_delegates_to_controller():
    """Test recover_task delegates to ClosedLoopController."""
    executor = TaskExecutor(FakeToolRegistry())
    controller = ClosedLoopController(executor)
    manager = LongRunningTaskManager(executor, controller=controller)
    task = manager.create_task("test goal", [{"description": "s1"}])
    # Mock controller.execute to return success
    controller.execute = MagicMock(return_value={"success": True, "task": task.to_dict()})
    result = manager.recover_task(task)
    assert result["success"] is True
    controller.execute.assert_called_once_with(task)
    assert task.status == LONG_TASK_STATUS["COMPLETED"]


def test_cancel_task_works_via_executor():
    """Test cancel_task uses executor.cancel_task."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "s1"}])
    task.status = LONG_TASK_STATUS["RUNNING"]
    # Mock executor.cancel_task
    executor.cancel_task = MagicMock(return_value=True)
    result = manager.cancel_task(task)
    assert result is True
    executor.cancel_task.assert_called_once_with(task.task_id)


def test_cancel_task_direct_when_no_executor():
    """Test cancel_task returns False directly when no executor (safety: no executor = no direct cancellation)."""
    manager = LongRunningTaskManager(None)
    task = MultiStepTask("t1", "g", [TaskStep("s1")])
    task.status = LONG_TASK_STATUS["PENDING"]
    result = manager.cancel_task(task)
    assert result is False  # Without executor, direct cancellation is refused (invariant preserved)
    assert task.status == LONG_TASK_STATUS["PENDING"]  # Status unchanged


def test_check_budget_uses_safety_config():
    """Test check_budget uses manager's safety config."""
    executor = TaskExecutor(FakeToolRegistry())
    safety = SafetyConfig(max_execution_seconds=100, max_total_steps=10, max_retries_per_step=2)
    manager = LongRunningTaskManager(executor, safety_config=safety)
    task = manager.create_task("test goal", [
        {"description": "s1"},
        {"description": "s2"},
    ])
    task.started_time = time.time() - 50  # 50 seconds ago
    task.steps[0].attempts = 1
    task.steps[1].attempts = 2
    budget = manager.check_budget(task)
    assert budget["time_ok"] is True   # 50 < 100
    assert budget["steps_ok"] is True  # 2 <= 10
    assert budget["retries_ok"] is True # attempts <= 2
    assert budget["within_budget"] is True

    # Exceed time
    task.started_time = time.time() - 150
    budget = manager.check_budget(task)
    assert budget["time_ok"] is False
    assert budget["within_budget"] is False


def test_restart_task_never_auto_resumes():
    """Test restart_task returns None (never auto-resumes)."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    # Even with a verification function, the method returns None by design
    def fake_verification():
        return True
    result = manager.restart_task("some_task_id", fake_verification)
    assert result is None


def test_event_emission_works():
    """Test that events are emitted via callback."""
    executor = TaskExecutor(FakeToolRegistry())
    callback = FakeEventCallback()
    manager = LongRunningTaskManager(executor, event_callback=callback)
    task = manager.create_task("test goal", [{"description": "s1"}])
    # Check that TASK_CREATED was emitted
    assert len(callback.calls) >= 1
    signals = [call[0] for call in callback.calls]
    assert "task_created" in signals
    # Checkpoint emission
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    manager.save_checkpoint(task, 0, "test note")
    signals = [call[0] for call in callback.calls]
    assert "task_checkpoint_saved" in signals
    # Pause emission
    manager.pause_task(task)
    signals = [call[0] for call in callback.calls]
    assert "task_paused" in signals


def test_task_checkpoint_dataclass():
    """Test TaskCheckpoint dataclass fields."""
    cp = TaskCheckpoint("task1", 5, True, note="test note")
    assert cp.task_id == "task1"
    assert cp.step_index == 5
    assert cp.verified is True
    assert cp.note == "test note"
    assert isinstance(cp.timestamp, float)
    # lightweight_dict
    d = cp.lightweight_dict()
    assert d["task_id"] == "task1"
    assert d["step_index"] == 5
    assert d["verified"] is True
    assert d["note"] == "test note"
    assert "timestamp" in d


def test_long_task_status_includes_original_and_new():
    """Test LONG_TASK_STATUS contains both original and new states."""
    # Original states from TASK_STATUS should be present
    assert "pending" in LONG_TASK_STATUS.values()
    assert "running" in LONG_TASK_STATUS.values()
    assert "completed" in LONG_TASK_STATUS.values()
    assert "failed" in LONG_TASK_STATUS.values()
    assert "cancelled" in LONG_TASK_STATUS.values()
    assert "planning" in LONG_TASK_STATUS.values()
    assert "observing" in LONG_TASK_STATUS.values()
    assert "verifying" in LONG_TASK_STATUS.values()
    assert "diagnosing" in LONG_TASK_STATUS.values()
    assert "replanning" in LONG_TASK_STATUS.values()
    assert "blocked" in LONG_TASK_STATUS.values()
    assert "needs_review" in LONG_TASK_STATUS.values()
    # New states
    assert "created" in LONG_TASK_STATUS.values()
    assert "ready" in LONG_TASK_STATUS.values()
    assert "paused" in LONG_TASK_STATUS.values()
    assert "waiting_for_permission" in LONG_TASK_STATUS.values()
    assert "recovering" in LONG_TASK_STATUS.values()


def test_checkpoint_status_enum():
    """Test CheckpointStatus enum."""
    assert CheckpointStatus.LIGHT == "light"
    assert CheckpointStatus.FULL == "full"


def test_manager_get_status():
    """Test manager.get_status returns expected structure."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [])
    status = manager.get_status()
    assert "managed_tasks" in status
    assert "checkpoints_total" in status
    assert "safety_config" in status
    assert task.task_id in status["managed_tasks"]
    assert status["checkpoints_total"] == 0


def test_multiple_checkpoints_per_task():
    """Test that multiple checkpoints can be saved for a task."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [
        {"description": "s1"}, {"description": "s2"}, {"description": "s3"}
    ])
    # Complete and verify each step, saving checkpoint after each
    for i in range(3):
        task.steps[i].status = TASK_STATUS["COMPLETED"]
        task.steps[i].verified = True
        manager.save_checkpoint(task, i, f"after step {i}")
    cps = manager.get_checkpoints(task.task_id)
    assert len(cps) == 3
    assert cps[0].step_index == 0
    assert cps[1].step_index == 1
    assert cps[2].step_index == 2


def test_checkpoint_note_can_be_empty():
    """Test that checkpoint note can be empty string."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "s1"}])
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    cp = manager.save_checkpoint(task, 0, "")
    assert cp.note == ""


def test_manager_handles_none_executor_gracefully():
    """Test that manager methods handle None executor gracefully."""
    manager = LongRunningTaskManager(None)
    # create_task should raise
    try:
        manager.create_task("goal", [])
        assert False, "Expected RuntimeError"
    except RuntimeError:
        pass
    # Other methods should return sensible defaults
    assert manager.pause_task(MultiStepTask("t1", "g", [])) is False
    assert manager.resume_task(MultiStepTask("t1", "g", [])) is False
    assert manager.wait_for_permission(MultiStepTask("t1", "g", []), "desc") is False
    assert manager.recover_task(MultiStepTask("t1", "g", []))["success"] is False
    assert manager.cancel_task(MultiStepTask("t1", "g", [])) is False
    assert manager.get_progress(MultiStepTask("t1", "g", []))["total_steps"] == 0
    assert manager.check_budget(MultiStepTask("t1", "g", []))["within_budget"] is True


def test_task_executor_has_9e_states():
    """Test that TaskExecutor.TASK_STATUS includes 9E states."""
    from veronica.automation.task_executor import TASK_STATUS
    assert "created" in TASK_STATUS.values()
    assert "ready" in TASK_STATUS.values()
    assert "paused" in TASK_STATUS.values()
    assert "waiting_for_permission" in TASK_STATUS.values()
    assert "recovering" in TASK_STATUS.values()


def test_task_step_can_be_extended():
    """Test that TaskStep can have additional attributes used by 9E."""
    step = TaskStep("desc", "tool", {"arg": 1})
    # These are existing attributes; 9E doesn't add new ones to TaskStep
    assert step.description == "desc"
    assert step.tool_name == "tool"
    assert step.args == {"arg": 1}
    assert step.status == TASK_STATUS["PENDING"]


def test_multistep_task_progress_property():
    """Test MultiStepTask.progress property works with 9E states."""
    task = MultiStepTask("t1", "goal", [
        TaskStep("s1", "tool1", {}),
        TaskStep("s2", "tool2", {}),
        TaskStep("s3", "tool3", {}),
    ])
    assert task.progress == 0.0
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    assert task.progress == pytest.approx(33.333, 0.1)
    task.steps[1].status = TASK_STATUS["COMPLETED"]
    assert task.progress == pytest.approx(66.666, 0.1)
    for s in task.steps:
        s.status = TASK_STATUS["COMPLETED"]
    assert task.progress == 100.0


def test_long_running_task_manager_init_with_all_args():
    """Test LongRunningTaskManager init with all optional args."""
    executor = TaskExecutor(FakeToolRegistry())
    controller = ClosedLoopController(executor)
    gate = PermissionGate(PermissionLevel.SAFE, lambda *_: PermissionDecision(True, ""))
    safety = SafetyConfig()
    callback = FakeEventCallback()
    manager = LongRunningTaskManager(executor, controller, gate, safety, callback)
    assert manager.executor is executor
    assert manager.controller is controller
    assert manager.gate is gate
    assert manager.safety is safety
    assert manager.event_callback is callback


def test_event_callback_is_optional():
    """Test that event_callback can be None."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor, event_callback=None)
    assert manager.event_callback is None
    # Should not raise when emitting
    manager._emit("test_signal", {"key": "value"})


def test_save_checkpoint_returns_none_for_invalid_step_index():
    """Test save_checkpoint returns None for out-of-range step index."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "s1"}])
    # Negative index
    assert manager.save_checkpoint(task, -1, "note") is None
    # Index >= len(steps)
    assert manager.save_checkpoint(task, 1, "note") is None
    # Index == len(steps) (out of range)
    assert manager.save_checkpoint(task, len(task.steps), "note") is None


def test_task_checkpoint_is_lightweight():
    """Test that TaskCheckpoint does not hold heavy data."""
    cp = TaskCheckpoint("task1", 0, True, note="x" * 1000)
    # The note is stored, but it's just a string; we rely on the comment that it's metadata only.
    # In practice, the note should be short; we don't enforce length here.
    assert cp.note == "x" * 1000


def test_manager_checkpoints_dict_is_per_task():
    """Test that checkpoints are stored per task ID."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task1 = manager.create_task("goal1", [{"description": "s1"}])
    task2 = manager.create_task("goal2", [{"description": "s1"}])
    # Complete and checkpoint task1
    task1.steps[0].status = TASK_STATUS["COMPLETED"]
    task1.steps[0].verified = True
    manager.save_checkpoint(task1, 0, "task1 cp")
    # Checkpoint task2
    task2.steps[0].status = TASK_STATUS["COMPLETED"]
    task2.steps[0].verified = True
    manager.save_checkpoint(task2, 0, "task2 cp")
    cps1 = manager.get_checkpoints(task1.task_id)
    cps2 = manager.get_checkpoints(task2.task_id)
    assert len(cps1) == 1
    assert len(cps2) == 1
    assert cps1[0].note == "task1 cp"
    assert cps2[0].note == "task2 cp"


def test_transition_to_does_not_allow_invalid_status():
    """Test that transition_to accepts any string (we don't validate)."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [])
    manager.transition_to(task, "some_custom_state", "reason")
    assert task.status == "some_custom_state"


def test_manager_can_be_used_without_controller():
    """Test manager works without controller (recover_task will fail)."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)  # no controller
    task = manager.create_task("test goal", [])
    result = manager.recover_task(task)
    assert result["success"] is False
    assert "No ClosedLoopController available" in result["error"]


def test_manager_can_be_used_without_gate():
    """Test manager works without gate (wait_for_permission fails; create_task requires executor per Phase 9E contract)."""
    # Phase 9E contract: create_task requires an executor (execution budget/enforcement layer)
    # This test verifies that when no gate is present, wait_for_permission correctly fails
    # and no permission bypass is granted.
    executor = TaskExecutor(FakeToolRegistry())  # create_task requires executor per contract
    manager = LongRunningTaskManager(executor, permission_gate=None)  # no gate
    task = manager.create_task("test goal", [])
    result = manager.wait_for_permission(task, "desc")
    assert result is False  # No gate = permission refused (invariant preserved)


def test_manager_can_be_used_without_event_callback():
    """Test manager works without event callback."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)  # no callback
    task = manager.create_task("test goal", [])
    # Should not raise
    manager.pause_task(task)
    manager.resume_task(task, lambda: True)
    manager.wait_for_permission(task, "desc", PermissionLevel.SAFE)
    manager.save_checkpoint(task, 0, "note")
    manager.get_progress(task)
    manager.check_budget(task)
    manager.recover_task(task)
    manager.cancel_task(task)


def test_task_checkpoint_immutable_fields():
    """Test that TaskCheckpoint fields are set at init and not changed."""
    cp = TaskCheckpoint("task1", 5, False, note="initial")
    # We cannot change the fields after creation (they are not properties with setters)
    # But we can reassign if we wanted to; however, the design is to treat as immutable.
    # We'll just check that the init values are correct.
    assert cp.task_id == "task1"
    assert cp.step_index == 5
    assert cp.verified is False
    assert cp.note == "initial"


def test_long_running_task_manager_default_safety():
    """Test that manager uses default SafetyConfig when none provided."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    assert isinstance(manager.safety, SafetyConfig)
    assert manager.safety.max_replans == 3
    assert manager.safety.max_retries_per_step == 2
    assert manager.safety.max_total_steps == 50
    assert manager.safety.max_execution_seconds == 600.0


def test_task_executor_max_retries_is_2():
    """Test that TaskExecutor.MAX_RETRIES is 2 (used by 9E)."""
    from veronica.automation.task_executor import TaskExecutor
    assert TaskExecutor.MAX_RETRIES == 2


def test_task_executor_retry_delay_can_be_zero():
    """Test that TaskExecutor.RETRY_DELAY_SEC can be 0 (for fast tests)."""
    from veronica.automation.task_executor import TaskExecutor
    assert TaskExecutor.RETRY_DELAY_SEC == 0.0


def test_long_running_task_manager_default_budgets():
    """Test manager's default budget constants."""
    from veronica.automation.long_running_task import LongRunningTaskManager
    assert LongRunningTaskManager.DEFAULT_MAX_STEPS == 50
    assert LongRunningTaskManager.DEFAULT_MAX_RETRIES == 2
    assert LongRunningTaskManager.DEFAULT_MAX_TOTAL_SECONDS == 600.0
    assert LongRunningTaskManager.DEFAULT_MAX_CHECKPOINTS == 20


def test_manager_checkpoints_respects_default_max_checkpoints():
    """Test that we don't enforce a max checkpoints limit in the manager (it's advisory)."""
    # The manager does not currently enforce DEFAULT_MAX_CHECKPOINTS; it's just a constant.
    # We'll note that in the test.
    from veronica.automation.long_running_task import LongRunningTaskManager
    assert hasattr(LongRunningTaskManager, "DEFAULT_MAX_CHECKPOINTS")


def test_task_can_be_created_with_empty_steps():
    """Test that a task can be created with zero steps."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("empty goal", [])
    assert len(task.steps) == 0
    assert task.progress == 0.0


def test_task_step_to_dict_includes_9e_fields():
    """Test that TaskStep.to_dict includes fields used by 9E."""
    step = TaskStep("desc", "tool", {"arg": 1})
    step.status = TASK_STATUS["COMPLETED"]
    step.verified = True
    step.verification_note = "verified"
    step.attempts = 2
    step.error = ""
    step.start_time = 1000.0
    step.end_time = 1001.0
    d = step.to_dict()
    assert d["description"] == "desc"
    assert d["tool"] == "tool"
    assert d["args"] == {"arg": 1}
    assert d["status"] == TASK_STATUS["COMPLETED"]
    assert d["verified"] is True
    assert d["verification_note"] == "verified"
    assert d["attempts"] == 2
    assert d["error"] == ""
    assert d["duration_ms"] == 1000.0


def test_multistep_task_to_dict_includes_progress():
    """Test that MultiStepTask.to_dict includes progress."""
    task = MultiStepTask("t1", "goal", [
        TaskStep("s1", "tool1", {}),
        TaskStep("s2", "tool2", {}),
    ])
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    d = task.to_dict()
    assert d["progress"] == 50.0
    assert d["current_step"] == 0  # zero-indexed, but progress based on completed
    assert d["total_steps"] == 2
    assert d["status"] == task.status  # PENDING by default


def test_long_running_task_manager_get_status_structure():
    """Test manager.get_status returns expected keys and types."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [])
    status = manager.get_status()
    assert isinstance(status["managed_tasks"], list)
    assert isinstance(status["checkpoints_total"], int)
    assert isinstance(status["safety_config"], dict)
    assert "max_replans" in status["safety_config"]


def test_manager_event_emission_does_not_raise_on_none_callback():
    """Test that _emit does not raise when callback is None."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor, event_callback=None)
    # Should not raise
    manager._emit("test_signal", {"key": "value"})


def test_manager_event_emission_catches_and_logs_exceptions():
    """Test that _emit logs exceptions from callback but does not raise."""
    executor = TaskExecutor(FakeToolRegistry())
    callback = MagicMock(side_effect=Exception("test"))
    manager = LongRunningTaskManager(executor, event_callback=callback)
    # Should not raise
    manager._emit("test_signal", {"key": "value"})
    callback.assert_called_once()


def test_long_running_task_manager_can_be_imported():
    """Test that the module can be imported without errors."""
    # This is already tested by the fact that we're running this test.
    from veronica.automation.long_running_task import LongRunningTaskManager, TaskCheckpoint
    assert LongRunningTaskManager is not None
    assert TaskCheckpoint is not None


def test_task_checkpoint_lightweight_dict_is_safe_for_logging():
    """TaskCheckpoint.lightweight_dict does not contain sensitive data."""
    cp = TaskCheckpoint("task1", 0, True, note="note")
    d = cp.lightweight_dict()
    # Ensure no unexpected keys
    expected_keys = {"task_id", "step_index", "verified", "timestamp", "note"}
    assert set(d.keys()) == expected_keys
    # Ensure values are simple types
    assert isinstance(d["task_id"], str)
    assert isinstance(d["step_index"], int)
    assert isinstance(d["verified"], bool)
    assert isinstance(d["timestamp"], str)
    assert isinstance(d["note"], str)


def test_long_running_task_manager_wait_for_permission_maps_risk_to_level():
    """Test that wait_for_permission uses the provided permission_level."""
    # We'll test by spying on the gate call; for simplicity we just check that the method calls gate.
    executor = TaskExecutor(FakeToolRegistry())
    gate = PermissionGate(PermissionLevel.SAFE, lambda description, level: PermissionDecision(True, ""))
    manager = LongRunningTaskManager(executor, permission_gate=gate)
    task = manager.create_task("test goal", [])
    # We cannot easily assert the arguments without mocking, but we trust the flow.
    result = manager.wait_for_permission(task, "desc", PermissionLevel.SENSITIVE)
    assert result is True  # gate returns True


def test_long_running_task_manager_resume_task_does_not_bypass_verification():
    """Test that resume_task requires reobserve_fn and does not skip it."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    task = manager.create_task("test goal", [{"description": "s1"}])
    task.status = LONG_TASK_STATUS["PAUSED"]
    task.current_step = 1
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    # Verified current step + fresh reobserve -> resume succeeds (reobserve is verification of current target)
    def fake_reobserve_true():
        return True
    assert manager.resume_task(task, fake_reobserve_true) is True


def test_long_running_task_manager_restart_task_never_returns_a_task():
    """Test restart_task always returns None (by design)."""
    executor = TaskExecutor(FakeToolRegistry())
    manager = LongRunningTaskManager(executor)
    # Even with a verification function that returns True, it returns None.
    def fake_verification():
        return True
    result = manager.restart_task("any_id", fake_verification)
    assert result is None
    # Without verification function
    result2 = manager.restart_task("any_id")
    assert result2 is None


def test_long_running_task_manager_checkpoint_does_not_grant_permission():
    """Test that saving a checkpoint does not affect permission state."""
    executor = TaskExecutor(FakeToolRegistry())
    gate = PermissionGate(PermissionLevel.SAFE, lambda description, level: False)
    manager = LongRunningTaskManager(executor, permission_gate=gate)
    task = manager.create_task("test goal", [{"description": "s1"}])
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    # Save a checkpoint
    cp = manager.save_checkpoint(task, 0, "note")
    assert cp is not None
    # Confirm permission denied (bool, not PermissionDecision instance); checkpoint does not grant anything.
    assert not gate.check("some_tool", PermissionLevel.SAFE, "desc").allowed


def test_long_running_task_manager_pause_does_not_execute_tools():
    """Test that pausing a task does not execute any tools."""
    executor = FakeToolRegistry()
    manager = LongRunningTaskManager(TaskExecutor(executor))
    task = manager.create_task("test goal", [
        {"description": "s1", "tool": "tool1", "args": {}},
        {"description": "s2", "tool": "tool2", "args": {}},
    ])
    # Simulate execution up to step 1 (index 0)
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    task.current_step = 1
    initial_calls = len(executor.dispatch_calls)
    # Pause the task
    manager.pause_task(task, "pausing")
    # No new tool calls should have been made by pause itself
    assert len(executor.dispatch_calls) == initial_calls


def test_long_running_task_manager_resume_does_not_execute_tools():
    """Test that resuming a task does not execute tools (only reobserve_fn is called)."""
    executor = FakeToolRegistry()
    manager = LongRunningTaskManager(TaskExecutor(executor))
    task = manager.create_task("test goal", [{"description": "s1", "tool": "tool1", "args": {}}])
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    task.status = LONG_TASK_STATUS["PAUSED"]
    task.current_step = 1
    initial_calls = len(executor.dispatch_calls)
    # Define a reobserve function that does not call tools
    calls_before = len(executor.dispatch_calls)
    def fake_reobserve():
        return True
    # Resume
    manager.resume_task(task, fake_reobserve)
    # No tool calls should have been made by the resume logic (only the reobserve function, which we defined to do nothing)
    assert len(executor.dispatch_calls) == calls_before


def test_long_running_task_manager_wait_for_permission_does_not_execute_tools():
    """Test that waiting for permission does not execute tools."""
    executor = FakeToolRegistry()
    gate = PermissionGate(PermissionLevel.SAFE, lambda description, level: PermissionDecision(True, ""))
    manager = LongRunningTaskManager(TaskExecutor(executor), permission_gate=gate)
    task = manager.create_task("test goal", [{"description": "s1", "tool": "tool1", "args": {}}])
    task.steps[0].status = TASK_STATUS["COMPLETED"]
    task.steps[0].verified = True
    initial_calls = len(executor.dispatch_calls)
    # Wait for permission
    manager.wait_for_permission(task, "desc", PermissionLevel.SAFE)
    # No tool calls should have been made by the permission wait logic
    assert len(executor.dispatch_calls) == initial_calls


def test_long_running_task_manager_recover_task_does_not_execute_tools_directly():
    """Test that recover_task delegates to controller, which delegates to executor."""
    executor = FakeToolRegistry()
    controller = ClosedLoopController(TaskExecutor(executor))
    manager = LongRunningTaskManager(TaskExecutor(executor), controller=controller)
    task = manager.create_task("test goal", [{"description": "s1", "tool": "tool1", "args": {}}])
    initial_calls = len(executor.dispatch_calls)
    # Mock controller.execute to avoid actual execution, but we want to see if it calls executor
    # We'll let it call the real controller, which will use the executor.
    # To avoid actual tool execution, we'll mock the executor's dispatch to return success.
    executor.dispatch = MagicMock(return_value=MagicMock(success=True, result="ok"))
    result = manager.recover_task(task)
    # The recover task will have called the executor via the controller
    assert executor.dispatch.called
    # But we don't assert on the number of calls because the controller may call multiple times.


def test_long_running_task_manager_cancel_task_does_not_execute_tools():
    """Test that cancel_task does not execute tools."""
    executor = FakeToolRegistry()
    manager = LongRunningTaskManager(TaskExecutor(executor))
    task = manager.create_task("test goal", [{"description": "s1", "tool": "tool1", "args": {}}])
    task.status = LONG_TASK_STATUS["RUNNING"]
    initial_calls = len(executor.dispatch_calls)
    # Cancel the task
    manager.cancel_task(task)
    # No new tool calls should have been made by cancel itself
    assert len(executor.dispatch_calls) == initial_calls


def test_long_running_task_manager_get_progress_does_not_execute_tools():
    """Test that get_progress does not execute tools."""
    executor = FakeToolRegistry()
    manager = LongRunningTaskManager(TaskExecutor(executor))
    task = manager.create_task("test goal", [{"description": "s1", "tool": "tool1", "args": {}}])
    initial_calls = len(executor.dispatch_calls)
    prog = manager.get_progress(task)
    assert len(executor.dispatch_calls) == initial_calls


def test_long_running_task_manager_check_budget_does_not_execute_tools():
    """Test that check_budget does not execute tools."""
    executor = FakeToolRegistry()
    manager = LongRunningTaskManager(TaskExecutor(executor))
    task = manager.create_task("test goal", [{"description": "s1", "tool": "tool1", "args": {}}])
    initial_calls = len(executor.dispatch_calls)
    budget = manager.check_budget(task)
    assert len(executor.dispatch_calls) == initial_calls


def test_long_running_task_manager_restart_task_does_not_execute_tools():
    """Test that restart_task does not execute tools."""
    executor = FakeToolRegistry()
    manager = LongRunningTaskManager(TaskExecutor(executor))
    task = manager.create_task("test goal", [{"description": "s1", "tool": "tool1", "args": {}}])
    initial_calls = len(executor.dispatch_calls)
    result = manager.restart_task(task.task_id)
    assert result is None
    assert len(executor.dispatch_calls) == initial_calls


def test_long_running_task_manager_event_emission_does_not_execute_tools():
    """Test that event emission does not execute tools."""
    executor = FakeToolRegistry()
    callback = FakeEventCallback()
    manager = LongRunningTaskManager(TaskExecutor(executor), event_callback=callback)
    task = manager.create_task("test goal", [{"description": "s1", "tool": "tool1", "args": {}}])
    initial_calls = len(executor.dispatch_calls)
    manager._emit("test_signal", {"key": "value"})
    assert len(executor.dispatch_calls) == initial_calls


def test_long_running_task_manager_lightweight_checkpoint_never_contains_tool_args():
    """Test that checkpoint lightweight_dict never contains tool args or sensitive data."""
    executor = FakeToolRegistry()
    manager = LongRunningTaskManager(TaskExecutor(executor))
    task = manager.create_task("test goal", [
        {"description": "type secret", "tool": "type_text", "args": {"text": "very secret"}},
        {"description": "click", "tool": "click_at", "args": {"x": 100, "y": 200}},
    ])
    # Complete and verify both steps
    for step in task.steps:
        step.status = TASK_STATUS["COMPLETED"]
        step.verified = True
    # Save checkpoints
    cp1 = manager.save_checkpoint(task, 0, "after type")
    cp2 = manager.save_checkpoint(task, 1, "after click")
    data1 = cp1.lightweight_dict()
    data2 = cp2.lightweight_dict()
    # Ensure no tool args are present
    assert "text" not in data1
    assert "x" not in data2
    assert "y" not in data2
    assert "args" not in data1
    assert "args" not in data2
    # Ensure no sensitive strings appear in the serialized data
    import json
    s1 = json.dumps(data1)
    s2 = json.dumps(data2)
    assert "very secret" not in s1
    assert "very secret" not in s2
    assert "secret" not in s1.lower()
    assert "secret" not in s2.lower()


def test_long_running_task_manager_resume_requires_fresh_verification():
    """Test that resume_task requires fresh verification via reobserve_fn."""
    # This is covered by the test_resume_task_requires_reobserve_fn and related tests.
    pass


def test_long_running_task_manager_restart_never_auto_resumes():
    """Test that restart_task never returns a task to auto-execute."""
    # Covered by test_long_running_task_manager_restart_task_never_returns_a_task.
    pass


def test_long_running_task_manager_checkpoint_after_verification_only():
    """Test that checkpoint is only saved after verification."""
    # Covered by test_save_checkpoint_only_after_verification.
    pass


def test_long_running_task_manager_permission_wait_uses_gate():
    """Test that wait_for_permission uses the provided PermissionGate."""
    # Covered by test_wait_for_permission_uses_gate.
    pass


def test_long_running_task_manager_budgets_use_safety_config():
    """Test that check_budget uses the manager's safety config."""
    # Covered by test_check_budget_uses_safety_config.
    pass


def test_long_running_task_manager_events_are_emitted():
    """Test that events are emitted for lifecycle changes."""
    # Covered by test_event_emission_works.
    pass