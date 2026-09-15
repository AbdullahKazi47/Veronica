"""
VERONICA Learning & Adaptation v1.

An experience-based learning subsystem that records what VERONICA does, what happened,
what succeeded/failed, and what feedback the user gave — separate from memory
("what happened / what do we know?") because its purpose is different:
"what should VERONICA do differently next time?"

v1 scope:
  * captures Experiences from the TaskExecutor lifecycle,
  * persists them durably (JSONL store, sanitized),
  * extracts structured learning signals (success / failure / recovery / retry /
    cancellation / user correction),
  * tracks evidence-based confidence per strategy.

Phase 3 (adaptive planning):
  * retrieves relevant past experiences and lessons before planning,
  * feeds them to the planner as advisory context (never as replayed plans),
  * the planner LLM still decides the fresh plan.

Phase 4 (controlled behavioral adaptation):
  * transforms a proposed plan based on validated, high-confidence lessons,
  * applies only under strong policy (≥ 0.90 + evidence ≥ 3, or user correction),
  * safety-sensitive tools are NEVER adapted; explicit user instructions always override.
  * adapter NEVER calls ToolRegistry.dispatch() or any tool; it only transforms the plan.
  * all adaptations are reversible: original plan preserved; new step dicts are created.

Learning is optional and fault-tolerant: if the store, engine, or retrieval fails, task
execution and planning continue normally. Learning can never bypass the safety/permission
gates, can never execute a tool by itself, and can never rewrite source code.
"""
from veronica.learning.experience import (
    Experience,
    ExperienceOutcome,
    VerificationResult,
    redact_experience,
    redact_value,
)
from veronica.learning.store import ExperienceStore
from veronica.learning.learner import LearningEngine, LessonSignal, SignalType
from veronica.learning.confidence import ConfidenceTracker
from veronica.learning.feedback import (
    FeedbackRecorder,
    FeedbackKind,
    UserFeedback,
    classify_feedback,
)
from veronica.learning.retrieval import (
    RankedExperience,
    rank_experiences,
    retrieve_relevant_experiences,
)
from veronica.learning.lesson import (
    Lesson,
    format_lessons_for_prompt,
    get_learning_context,
    get_relevant_lessons,
    merge_lessons,
)
from veronica.learning.policy import (
    BehaviorPolicy,
    PolicyDecision,
    PolicyMode,
    PolicyThresholds,
)
from veronica.learning.strategy import (
    StrategyEvolution,
    StrategyInfo,
    StrategyState,
    StrategyEvidence,
)
from veronica.learning.metrics import (
    LearningMetrics,
    LearningHealth,
    TaskMetrics,
    AdaptationMetrics,
    SafetyMetrics,
    TrendBucket,
    EvidenceThresholds,
)
from veronica.learning.adaptation import (
    AdaptationResult,
    AppliedAdaptation,
    BehavioralAdapter,
    RejectedAdaptation,
)

__all__ = [
    "Experience", "ExperienceOutcome", "VerificationResult", "redact_experience", "redact_value",
    "ExperienceStore", "LearningEngine", "LessonSignal", "SignalType",
    "ConfidenceTracker", "FeedbackRecorder", "FeedbackKind", "UserFeedback", "classify_feedback",
    "RankedExperience", "rank_experiences", "retrieve_relevant_experiences",
    "Lesson", "format_lessons_for_prompt", "get_learning_context", "get_relevant_lessons", "merge_lessons",
    # Phase 4
    "BehaviorPolicy", "PolicyDecision", "PolicyMode", "PolicyThresholds",
    "AdaptationResult", "AppliedAdaptation", "BehavioralAdapter", "RejectedAdaptation",
    # Phase 5 — metrics
    "LearningMetrics", "LearningHealth", "TaskMetrics", "AdaptationMetrics",
    "SafetyMetrics", "TrendBucket", "EvidenceThresholds",
    # Phase 5 — strategy evolution
    "StrategyEvolution", "StrategyInfo", "StrategyState", "StrategyEvidence",
]
