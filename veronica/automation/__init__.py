"""
VERONICA automation layer — multi-step agentic execution.

Exports the task executor (plan → execute with retry + self-verify) and planner
(LLM-based goal decomposition). Both reuse VERONICA's existing DI (injected
ToolRegistry, optional LLMProvider) and keep the header required for ULTRON
pattern reuse: patterns only, not a redistribution of private code.

Phase 6: Closed-loop intelligence — observe → diagnose → replan cycle.
Exports the closed-loop controller, observation layer, diagnosis engine,
failure memory, and replanner.
"""
from veronica.automation.task_executor import (
    MultiStepTask,
    TaskExecutor,
    TaskStep,
    TASK_STATUS,
)
from veronica.automation.planner import MultiStepPlanner, TaskPlanStep

# Phase 6: closed-loop intelligence
from veronica.automation.observation import (
    StepObservation,
    TaskObservation,
    FailureCategory,
    EvidenceSufficiency,
    ExpectedOutcome,
    ObservedOutcome,
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
from veronica.automation.failure_memory import FailureMemory
from veronica.automation.replanner import Replanner, ReplanResult
from veronica.automation.closed_loop import ClosedLoopController, SafetyConfig

__all__ = [
    "TaskExecutor",
    "TaskStep",
    "MultiStepTask",
    "TASK_STATUS",
    "MultiStepPlanner",
    "TaskPlanStep",
    # Phase 6: closed-loop intelligence
    "ClosedLoopController",
    "SafetyConfig",
    "FailureMemory",
    "Replanner",
    "ReplanResult",
    "StepObservation",
    "TaskObservation",
    "FailureCategory",
    "EvidenceSufficiency",
    "ExpectedOutcome",
    "ObservedOutcome",
    "observe_step",
    "observe_task",
    "DiagnosisConfidence",
    "RecoveryAction",
    "StepDiagnosis",
    "TaskDiagnosis",
    "diagnose_step",
    "diagnose_task",
]
