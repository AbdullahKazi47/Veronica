"""
Episodic Memory Integration V1 — connect VERONICA's real execution lifecycle to Thread.

The rule this module is built on (spec Phase 29):

    EXECUTION → RESULT (authoritative Experience) → MEMORY (records it) → LEARNING → PLANNING

There is ONE authoritative source of task outcome: the existing agent/task system
(``TaskExecutor`` builds an ``Experience`` at every terminal state). This module only
*consumes* that result:

    EpisodeBuilder    — turns an authoritative ``Experience`` into a richer ``Episode``
    EpisodeRecorder   — persists episodes through the existing ``ThreadMemory.create_episode``
    EpisodeRetriever  — pulls experiences back as labeled, relevance-filtered context
    ProcedureStore    — deterministic evidence tracking for repeated procedures (advisory)

Truthfulness is first-class: an action that ran but could not be verified is recorded as
execution=executed / verification=unknown, and its episode_status is UNKNOWN — never a
converted success. No episode ever grants permission; ``PermissionGate`` stays the sole
authorization path. Every failure here is logged and swallowed: if memory fails, the task
keeps running with its current context (spec Phase 18).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence

from veronica.learning.experience import Experience, redact_value
from veronica.logging_setup import get_logger
from veronica.memory.thread.models import (
    Episode,
    MemoryRecord,
    MemoryType,
    new_memory_id,
    now_iso,
)

logger = get_logger("memory.thread.episodes")

# --------------------------------------------------------------------------- #
# Controlled vocabularies (spec: explicit status values, no arbitrary strings)
# --------------------------------------------------------------------------- #


class EpisodeStatus:
    """Authoritative episode outcome. UNKNOWN must remain UNKNOWN."""
    SUCCESS = "success"
    FAILED = "failed"
    # Streaming LLM V1: explicitly separate a provider/timeout/resource stream failure
    # from a task-execution failure (spec Phase 20) so memory analytics never conflate
    # a partial-output stream error with a task that ran and then failed verification.
    STREAM_FAILURE = "stream_failure"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    KNOWN = frozenset({SUCCESS, FAILED, STREAM_FAILURE, BLOCKED, CANCELLED, UNKNOWN})


class ExecutionStatus:
    """Did the actions actually RUN? (spec: EXECUTED does not mean VERIFIED.)"""
    EXECUTED = "executed"
    PARTIAL = "partial"
    NOT_EXECUTED = "not_executed"
    UNKNOWN = "unknown"


class VerificationStatus:
    """Did the real world confirm the result?"""
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


# --- status derivation (deterministic, testable) ------------------------------

_TASK_TO_EPISODE: Dict[str, str] = {
    "completed": EpisodeStatus.SUCCESS,
    "failed": EpisodeStatus.FAILED,
    "blocked": EpisodeStatus.BLOCKED,
    "needs_review": EpisodeStatus.UNKNOWN,      # authoritative-but-unresolved
    "cancelled": EpisodeStatus.CANCELLED,
    # Streaming LLM V1 (Phase 20): a dedicated vocabulary value so provider/timeout/
    # resource stream errors are not silently lumped into FAILED (which implies the
    # task executed and failed verification, not that it never produced complete output).
    "stream_failed": EpisodeStatus.STREAM_FAILURE,
    "stream_error": EpisodeStatus.STREAM_FAILURE,
}


def map_task_status(task_status: Optional[str]) -> str:
    """Map a low-level task.status to the episode vocabulary. Anything unrecognised maps
    to UNKNOWN — never silently to SUCCESS."""
    if not task_status:
        return EpisodeStatus.UNKNOWN
    return _TASK_TO_EPISODE.get(str(task_status).strip().lower(), EpisodeStatus.UNKNOWN)


def _step_statuses(steps: Optional[Sequence[dict]]) -> list[str]:
    if not steps:
        return []
    out = []
    for s in steps:
        if isinstance(s, dict):
            out.append(str(s.get("status") or "").lower())
        else:
            out.append("")
    return out


def derive_execution_status(steps: Optional[Sequence[dict]]) -> str:
    """EXECUTED at least one step ran to a result; PARTIAL some completed and some never
    did (mixed/failed); NOT_EXECUTED nothing ran; UNKNOWN no step data."""
    ss = _step_statuses(steps)
    if not ss:
        return ExecutionStatus.UNKNOWN
    ran = [s for s in ss if s in ("completed", "failed", "recovered")]
    if not ran:
        return ExecutionStatus.NOT_EXECUTED
    failed = any(s == "failed" for s in ss)
    if failed and any(s in ("completed", "recovered") for s in ss):
        return ExecutionStatus.PARTIAL
    return ExecutionStatus.EXECUTED


def derive_verification_status(steps: Optional[Sequence[dict]]) -> str:
    """Verification evidence from a step trace. VERIFIED all completed steps confirmed;
    FAILED some completed step's verification failed; UNKNOWN unconfirmed/mixed;
    NOT_APPLICABLE nothing completed to verify. UNKNOWN is never upgraded to VERIFIED."""
    if not steps:
        return VerificationStatus.UNKNOWN
    completed = [s for s in steps if isinstance(s, dict) and s.get("status") in ("completed", "recovered")]
    if not completed:
        return VerificationStatus.NOT_APPLICABLE
    flags = [bool(s.get("verified")) for s in completed]
    if all(flags):
        return VerificationStatus.VERIFIED
    if not any(flags):
        return VerificationStatus.FAILED
    # some confirmed some not — evidence is contradictory, so we cannot claim verified
    return VerificationStatus.UNKNOWN


def derive_episode_status(
    explicit_status: str = "",
    execution_status: str = "",
    verification_status: str = "",
) -> str:
    """The outcome derivation rule (spec Phases 3-4, 26). Order:
      1. A genuinely terminal, distinct status (CANCELLED / BLOCKED / STREAM_FAILURE) wins.
      2. Nothing executed -> FAILED when the system reports it failed, else UNKNOWN.
      3. Executed + verified   -> SUCCESS
      4. Executed + failed     -> FAILED
      5. Executed + unverified -> UNKNOWN  (never promoted to SUCCESS)

    ``explicit_status`` is a low-level task.status; it is mapped conservatively and only
    SUCCESS when verification agrees. A STREAM_FAILURE maps to the distinct stream_failure
    episode status — never conflated with a task that ran and failed verification."""
    mapped = map_task_status(explicit_status)
    if mapped in (EpisodeStatus.CANCELLED, EpisodeStatus.BLOCKED, EpisodeStatus.STREAM_FAILURE):
        return mapped

    ex = execution_status or ExecutionStatus.UNKNOWN
    vf = verification_status or VerificationStatus.UNKNOWN

    if ex == ExecutionStatus.NOT_EXECUTED:
        return EpisodeStatus.FAILED if mapped == EpisodeStatus.FAILED else EpisodeStatus.UNKNOWN
    if ex == ExecutionStatus.PARTIAL:
        return EpisodeStatus.FAILED
    if ex == ExecutionStatus.EXECUTED:
        if vf == VerificationStatus.VERIFIED:
            return EpisodeStatus.SUCCESS
        if vf == VerificationStatus.FAILED:
            return EpisodeStatus.FAILED
        if mapped == EpisodeStatus.FAILED:
            return EpisodeStatus.FAILED
        return EpisodeStatus.UNKNOWN          # executed but verification unknown
    if mapped in (EpisodeStatus.SUCCESS, EpisodeStatus.FAILED):
        return mapped if vf != VerificationStatus.UNKNOWN else EpisodeStatus.UNKNOWN
    return EpisodeStatus.UNKNOWN


def _map_experience_verification(v: str) -> str:
    v = (v or "").strip().lower()
    if v == "passed":
        return VerificationStatus.VERIFIED
    if v == "failed":
        return VerificationStatus.FAILED
    if v in ("not_applicable", "not applicable"):
        return VerificationStatus.NOT_APPLICABLE
    return VerificationStatus.UNKNOWN


# --------------------------------------------------------------------------- #
# Episode size control (spec Phase 21)
# --------------------------------------------------------------------------- #

MAX_ACTIONS = 50
MAX_OBSERVATIONS = 50
MAX_FAILURES = 20
MAX_RECOVERIES = 20
MAX_TOOLS = 50
MAX_GOAL_CHARS = 500
MAX_SUMMARY_CHARS = 1200
MAX_FIELD_CHARS = 240
MAX_RESULT_CHARS = 400


def _clip_list(items: Sequence[str], limit: int) -> list[str]:
    clipped = [str(i) for i in items if i is not None]
    return [t[:MAX_FIELD_CHARS] for t in clipped][:limit]


def _clip_text(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    if not text:
        return ""
    text = str(text).replace("\x00", "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# EpisodeBuilder — authoritative Experience -> rich Episode
# --------------------------------------------------------------------------- #


class EpisodeBuilder:
    """Turn an authoritative ``Experience`` into a Thread ``Episode``.

    This is the ONLY place raw execution data becomes episodic data. It copies the
    stable serialized representation (goal, summaries, per-action status) — never whole
    runtime objects — applies redaction and size limits, and derives the truthful
    episode_status from execution + verification evidence.
    """

    def build(
        self,
        experience: Experience,
        *,
        task_type: str = "",
        model_id: str = "",
        provider: str = "",
        routing_role: str = "",
        task_dict: Optional[dict] = None,
        project: str = "",
        tags: Sequence[str] = (),
    ) -> Episode:
        goal = _clip_text(experience.goal, MAX_GOAL_CHARS) or "Untitled episode"

        # Richer per-step detail when the caller has a task.to_dict(); otherwise fall
        # back to the Experience's own action trace (which carries status + verified).
        steps: list[dict] = []
        if task_dict and isinstance(task_dict.get("steps"), list):
            steps = [
                {
                    "description": str(s.get("description", "")),
                    "tool": str(s.get("tool", "") or ""),
                    "status": str(s.get("status", "") or "").lower(),
                    "verified": bool(s.get("verified")),
                    "verification_note": str(s.get("verification_note", "") or ""),
                    "error": str(s.get("error", "") or ""),
                    "attempts": int(s.get("attempts", 0) or 0),
                }
                for s in task_dict["steps"]
            ]
        elif experience.actions:
            steps = [
                {
                    "description": str(a.get("description", a.get("step", "")) or ""),
                    "tool": str(a.get("tool", a.get("tool_name", "")) or ""),
                    "status": str(a.get("status", "") or "").lower(),
                    "verified": bool(a.get("verified")),
                    "attempts": int(a.get("attempts", 0) or 0),
                    "error": str(a.get("error", "") or ""),
                }
                for a in experience.actions
            ]

        # Model / task metadata: explicit kwargs win, else whatever the orchestrator
        # enriched onto the Experience's metadata before it reached us.
        meta_src = experience.metadata or {}
        task_type = task_type or str(meta_src.get("task_type", "") or "")
        model_id = model_id or str(meta_src.get("model_id", "") or "")
        provider = provider or str(meta_src.get("provider", "") or "")
        routing_role = routing_role or str(meta_src.get("routing_role", "") or "")

        execution = derive_execution_status(steps)
        verification = derive_verification_status(steps)
        # A richer/trustworthy verification signal can also come from the Experience verb.
        if experience.verification:
            exp_vf = _map_experience_verification(experience.verification)
            if exp_vf != VerificationStatus.NOT_APPLICABLE or verification == VerificationStatus.NOT_APPLICABLE:
                verification = exp_vf
        status = derive_episode_status(experience.final_status, execution, verification)

        actions = _clip_list([s.get("description") for s in steps if s.get("description")], MAX_ACTIONS)
        tools = _clip_list(
            [s.get("tool") for s in steps if s.get("tool")] or list(experience.tool_calls),
            MAX_TOOLS,
        )

        # Failures — deterministic categorization (spec Phase 8).
        failures: list[dict] = []
        for s in steps:
            if s.get("status") == "failed" or s.get("error"):
                failures.append({
                    "action": _clip_text(s.get("description", ""), 160),
                    "tool": _clip_text(s.get("tool", ""), 80),
                    "error": _clip_text(s.get("error", ""), MAX_FIELD_CHARS),
                    "category": _categorize_error(s.get("error", "")),
                })
        if not failures and experience.failed_step:
            failures.append({
                "action": _clip_text(experience.failed_step, 160),
                "tool": _clip_text(experience.failed_step, 80),
                "error": _clip_text(experience.failure_reason, MAX_FIELD_CHARS),
                "category": _categorize_error(experience.failure_reason),
            })
        failures = failures[:MAX_FAILURES]

        # Recovery attempts (spec Phase 8: recovery attempt + recovery result).
        recoveries: list[dict] = []
        retried_tools = [t for t in (experience.recovery_actions or []) if t]
        for i, tool in enumerate(retried_tools[:MAX_RECOVERIES]):
            # A retried tool that ended completed is a succeeded recovery; a retried tool
            # whose step still failed is a failed recovery. We only know the attempt's
            # terminal step from the actions trace.
            success = _recovery_ended_ok(tool, steps)
            recoveries.append({
                "attempt": i + 1,
                "tool": _clip_text(tool, 80),
                "result": "succeeded" if success else "failed",
            })

        observations = _clip_list(experience.recovery_actions, 0)  # placeholder replaced below
        # Reuse the per-step verification notes as structured observations (no raw
        # screenshots / raw screen text are ever persisted here).
        verification_results: list[dict] = []
        observations_list: list[str] = []
        for s in steps:
            note = s.get("verification_note") or ""
            if note and s.get("status") in ("completed", "recovered"):
                verification_results.append({
                    "step": _clip_text(s.get("description", ""), 140),
                    "result": "verified" if s.get("verified") else "unconfirmed",
                    "note": _clip_text(note, MAX_FIELD_CHARS),
                })
                observations_list.append(_clip_text(note, MAX_FIELD_CHARS))
        verification_results = verification_results[:MAX_FAILURES]
        observations_list = _clip_list(observations_list, MAX_OBSERVATIONS)

        # Plan summary: a compressed, redacted outline of the plan (never full args).
        plan_summary = _build_plan_summary(steps)

        result = _clip_text(experience.metadata.get("result_summary", experience.final_status or experience.outcome or ""),
                            MAX_RESULT_CHARS)

        # Confidence stays evidence-derived and advisory (spec Phases 7/11); a stored
        # episode is information, never consent.
        confidence = _episode_confidence(status, experience)

        meta_out: dict[str, Any] = {
            "outcome": experience.outcome,
            "retries": int(experience.retries or 0),
            "verified_success_path": bool(status == EpisodeStatus.SUCCESS),
            "duration_sec": round(float(experience.duration or 0.0), 2),
        }
        if experience.metadata:
            # Never store secrets; redact any enriched extra metadata.
            meta_out["extra"] = redact_value(experience.metadata)

        return Episode(
            goal=goal,
            actions=actions,
            tools_used=tools,
            result=result,
            success=(status == EpisodeStatus.SUCCESS),
            verification="passed" if status == EpisodeStatus.SUCCESS else (
                "failed" if status == EpisodeStatus.FAILED else "unverifiable"
            ),
            errors=_clip_list([f.get("error", "") for f in failures], MAX_FAILURES),
            recovery_attempts=len(recoveries),
            duration_sec=round(float(experience.duration or 0.0), 2),
            tags=list(tags or ()),
            project=project,
            metadata=redact_value(meta_out),
            linked_experience_id=experience.experience_id or "",
            episode_id=new_memory_id(),
            episode_status=status,
            execution_status=execution,
            verification_status=verification,
            task_type=task_type,
            plan_summary=plan_summary,
            observations=observations_list,
            verification_results=verification_results,
            failures=redact_value(failures),
            recoveries=redact_value(recoveries),
            model_id=model_id,
            provider=provider,
            routing_role=routing_role,
            started_at=experience.timestamp or now_iso(),
        )


_ERROR_CATEGORIES: Dict[str, str] = {
    "permission": "permission_denied",
    "not permitted": "permission_denied",
    "unavailable": "capability_unavailable",
    "timeout": "timeout",
    "not found": "not_found",
    "missing": "not_found",
    "connection": "connection_failed",
    "invalid": "invalid_input",
    "fail": "execution_failed",
}


def _categorize_error(error: str) -> str:
    """Deterministic, lightweight failure categorization for failure memory (Phase 8).
    Unknown errors are 'execution_failed' — never falsely a specific cause."""
    err = (error or "").lower()
    for needle, category in _ERROR_CATEGORIES.items():
        if needle in err:
            return category
    return "execution_failed"


def _recovery_ended_ok(tool: str, steps: Sequence[dict]) -> bool:
    """True when the retried tool's last step reached a completed/recovered state."""
    matching = [s for s in steps if s.get("tool") == tool]
    return bool(matching) and all(s.get("status") in ("completed", "recovered") for s in matching)


def _build_plan_summary(steps: Sequence[dict]) -> str:
    """Compressed, redacted plan outline: 'tool: description' lines, bounded size."""
    lines = []
    for s in steps:
        desc = _clip_text(s.get("description", ""), 120)
        tool = _clip_text(s.get("tool", ""), 60)
        if desc:
            lines.append(f"{tool}: {desc}" if tool else desc)
        if len(lines) >= MAX_ACTIONS:
            break
    summary = " | ".join(lines)
    return summary[:MAX_SUMMARY_CHARS]


def _episode_confidence(status: str, experience: Experience) -> float:
    """Evidence-derived confidence — a descriptor, never authorization.
    Verified success + few retries ' confident; executed-but-unknown ' low."""
    if status == EpisodeStatus.SUCCESS:
        base = 0.8
    elif status == EpisodeStatus.FAILED:
        base = 0.6
    else:
        base = 0.3
    penalty = min(0.3, (experience.retries or 0) * 0.1)
    return round(max(0.1, base - penalty), 2)


# --------------------------------------------------------------------------- #
# EpisodeRecorder — persist the authoritative result into Thread (fault-isolated)
# --------------------------------------------------------------------------- #


class EpisodeRecorder:
    """Writes one episode per authoritative experience. NEVER raises into the caller:
    if Thread fails, episode creation fails, serialization fails, or vector indexing
    fails, the episode is dropped/logged and task execution is unaffected (Phase 18).

    ``event_callback`` mirrors the closed-loop/computer-use signal pattern so front-ends
    (Stitch) can display episode events without owning any memory logic (Phase 24).
    """

    def __init__(
        self,
        thread_memory,
        *,
        importance: float = 0.6,
        confidence: float = 0.7,
        event_callback: Optional[Callable[[str, dict], None]] = None,
        builder: Optional[EpisodeBuilder] = None,
    ) -> None:
        self.thread_memory = thread_memory
        self.importance = importance
        self.confidence = confidence
        self.event_callback = event_callback
        self.builder = builder or EpisodeBuilder()
        # The most recently built episode (or None when nothing recorded or on failure).
        # Exposed so concurrent consumers (e.g. ProcedureStore) reuse the same episode
        # instead of re-deriving it from the raw Experience.
        self.last_episode: Optional[Episode] = None

    def record(
        self,
        experience: Experience,
        *,
        task_dict: Optional[dict] = None,
        **episode_ctx: Any,
    ) -> list[MemoryRecord]:
        if self.thread_memory is None:
            return []
        self._emit("episode_started", {"experience_id": experience.experience_id or "",
                                       "goal": _clip_text(experience.goal, 120)})
        try:
            episode = self.builder.build(experience, task_dict=task_dict, **episode_ctx)
            self.last_episode = episode
            self._emit("episode_completed", {"episode_id": episode.episode_id,
                                             "status": episode.episode_status,
                                             "verification_status": episode.verification_status})
            records = self.thread_memory.create_episode(
                episode, importance=self.importance, confidence=self.confidence
            )
            if records:
                self._emit("memory_written", {"count": len(records),
                                              "episode_id": episode.episode_id,
                                              "status": episode.episode_status})
            logger.info(
                "episode persisted id=%s status=%s verification=%s records=%d",
                episode.episode_id, episode.episode_status,
                episode.verification_status, len(records),
            )
            return records
        except Exception:  # noqa: BLE001 — memory must never break the task
            self.last_episode = None
            logger.exception(
                "episode recording failed for experience %s — task result is unaffected",
                getattr(experience, "experience_id", ""),
            )
            return []

    def _emit(self, signal: str, args: dict) -> None:
        if self.event_callback is None:
            return
        try:
            self.event_callback(signal, args)
        except Exception:  # noqa: BLE001
            logger.warning("episode event callback failed for %s", signal)


# --------------------------------------------------------------------------- #
# EpisodeRetriever — relevance-filtered experience context (reuses Thread)
# --------------------------------------------------------------------------- #


class EpisodeRetriever:
    """Retrieve prior experiences through the EXISTING Thread retrieval architecture —
    no second vector store, no second embedding implementation (Phases 12-13, 22).

    Only EXPERIENCE-type records are candidates; optional post-filters on outcome /
    task_type keep the injected context relevant; ``top_k`` bounds the context budget.
    """

    DEFAULT_TOP_K = 3

    def __init__(
        self,
        thread_memory,
        *,
        default_top_k: int = DEFAULT_TOP_K,
        event_callback: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self.thread_memory = thread_memory
        self.default_top_k = max(1, int(default_top_k))
        self.event_callback = event_callback

    def retrieve(
        self,
        text: str,
        *,
        top_k: Optional[int] = None,
        outcome: Optional[str] = None,
        task_type: Optional[str] = None,
        exclude_ids: Sequence[str] = (),
    ) -> list[tuple[MemoryRecord, float]]:
        if self.thread_memory is None:
            return []
        k = max(1, top_k or self.default_top_k)
        try:
            from veronica.memory.thread import RetrievalQuery
            query = RetrievalQuery(
                query=text,
                memory_type=MemoryType.EXPERIENCE,
                top_k=min(k * 3, 20),          # fetch a wider pool, then filter+rank
                exclude_ids=tuple(exclude_ids or ()),
            )
            result = self.thread_memory.retrieve(query)
            memories, scores = result.memories, list(result.scores)
            picked: list[tuple[MemoryRecord, float]] = []
            for i, mem in enumerate(memories[:k * 3]):
                score = scores[i] if i < len(scores) else 0.0
                md = mem.metadata or {}
                if outcome and md.get("episode_status") != outcome:
                    continue
                if task_type and md.get("task_type") != task_type:
                    continue
                picked.append((mem, score))
            picked.sort(key=lambda pair: pair[1], reverse=True)
            picked = picked[:k]
            self._emit("experience_retrieved", {"count": len(picked), "query": _clip_text(text, 120)})
            logger.info("episode retrieval: %d relevant experience(s) for query=%r", len(picked), text[:60])
            return picked
        except Exception:  # noqa: BLE001 — retrieval must never break the turn
            logger.exception("episode retrieval failed; continuing without experience context.")
            return []

    def format_context(self, picked: Sequence[tuple[MemoryRecord, float]], limit: Optional[int] = None) -> str:
        """A clearly-labeled RETRIEVED EXPERIENCE block — separate from the current user
        request, current context, and current observations. Informational only."""
        picked = list(picked)[: (limit or self.default_top_k)]
        if not picked:
            return ""
        lines = [
            "[RETRIEVED EXPERIENCE — what happened in similar past tasks. "
            "INFORMATIONAL ONLY: it NEVER authorizes an action and it does not describe "
            "the current state of the world. Permissions are decided separately.]"
        ]
        for i, (mem, score) in enumerate(picked):
            md = mem.metadata or {}
            outcome = md.get("episode_status", "?")
            verification = md.get("verification_status", "?")
            extra = []
            if md.get("task_type"):
                extra.append(f"task={md['task_type']}")
            if md.get("model_id"):
                extra.append(f"model={md['model_id']}")
            if md.get("recovery_count"):
                extra.append(f"recoveries={md['recovery_count']}")
            tag = (" (" + ", ".join(extra) + ")") if extra else ""
            lines.append(
                f"- [{outcome}/{verification}] {_clip_text(mem.content, 220)}"
                f"{tag} score={score:.2f}"
            )
        return "\n".join(lines)

    def _emit(self, signal: str, args: dict) -> None:
        if self.event_callback is None:
            return
        try:
            self.event_callback(signal, args)
        except Exception:  # noqa: BLE001
            logger.warning("episode retrieval event callback failed for %s", signal)


# --------------------------------------------------------------------------- #
# ProcedureCandidate — deterministic evidence for repeated procedures (Phase 10/11)
# --------------------------------------------------------------------------- #


class ProcedureState:
    """Controlled promotion — one verified success is NOT a procedure (Phase 11)."""
    OBSERVED = "observed"
    PROMISING = "promising"
    VALIDATED = "validated"
    DEGRADED = "degraded"

    KNOWN = frozenset({OBSERVED, PROMISING, VALIDATED, DEGRADED})


@dataclass
class ProcedureCandidate:
    procedure_id: str = field(default_factory=new_memory_id)
    goal_pattern: str = ""
    steps: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    confidence: float = 0.0
    state: str = ProcedureState.OBSERVED
    source_episode_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    # Evidence thresholds (advisory only — a procedure is evidence, never permission
    # and never a guarantee of future success).
    promote_at: int = 2          # >= 2 verified successes -> PROMISING
    validate_at: int = 3         # >= 3 verified successes (no failures) -> VALIDATED
    degrade_at: int = 2          # >= 2 failures (and failures >= successes) -> DEGRADED

    def to_dict(self) -> dict:
        return {
            "procedure_id": self.procedure_id,
            "goal_pattern": self.goal_pattern,
            "steps": list(self.steps),
            "tools": list(self.tools),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "confidence": self.confidence,
            "state": self.state,
            "source_episode_ids": list(self.source_episode_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, record: MemoryRecord) -> "ProcedureCandidate":
        md = record.metadata or {}
        return cls(
            procedure_id=md.get("procedure_id") or record.id or new_memory_id(),
            goal_pattern=str(md.get("goal_pattern", "") or ""),
            steps=list(md.get("steps", []) or []),
            tools=list(md.get("tools", []) or []),
            success_count=int(md.get("success_count", 0) or 0),
            failure_count=int(md.get("failure_count", 0) or 0),
            confidence=float(md.get("confidence", 0.0) or 0.0),
            state=str(md.get("state", ProcedureState.OBSERVED) or ProcedureState.OBSERVED),
            source_episode_ids=list(md.get("source_episode_ids", []) or []),
            created_at=str(md.get("created_at", "") or record.created_at),
            updated_at=str(md.get("updated_at", "") or record.updated_at),
            promote_at=int(md.get("promote_at", 2) or 2),
            validate_at=int(md.get("validate_at", 3) or 3),
            degrade_at=int(md.get("degrade_at", 2) or 2),
        )

    def to_record(self) -> MemoryRecord:
        return MemoryRecord(
            content="Procedure (evidence): " + _clip_text(self.goal_pattern, 220),
            memory_type=MemoryType.PROCEDURE,
            importance=0.5,
            confidence=self.confidence,
            provenance="procedure",
            source="experience",
            tags=["procedure"],
            metadata={
                "procedure_id": self.procedure_id,
                "goal_pattern": self.goal_pattern,
                "steps": list(self.steps),
                "tools": list(self.tools),
                "success_count": self.success_count,
                "failure_count": self.failure_count,
                "confidence": self.confidence,
                "state": self.state,
                "source_episode_ids": list(self.source_episode_ids),
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "promote_at": self.promote_at,
                "validate_at": self.validate_at,
                "degrade_at": self.degrade_at,
            },
        )

    def _recompute(self) -> None:
        total = self.success_count + self.failure_count
        self.confidence = round(self.success_count / (total + 1), 2) if total else 0.0
        if self.failure_count >= self.degrade_at and self.failure_count >= self.success_count:
            self.state = ProcedureState.DEGRADED
        elif self.success_count >= self.validate_at and self.failure_count == 0:
            self.state = ProcedureState.VALIDATED
        elif self.success_count >= self.promote_at:
            self.state = ProcedureState.PROMISING
        else:
            self.state = ProcedureState.OBSERVED
        self.updated_at = now_iso()


def _goal_pattern(goal: str) -> str:
    """A compact deterministic key for 'same kind of task' matching. Lowercased, first
    60 chars, whitespace collapsed, redaction-safe (never stores secrets verbatim)."""
    g = (goal or "").lower().strip().replace("\x00", "")
    return " ".join(g.split())[:60]


def _same_procedure(a: ProcedureCandidate, episode: Episode) -> bool:
    """Deterministic match: same goal pattern (normalized) OR overlapping tool set."""
    if a.goal_pattern and a.goal_pattern == _goal_pattern(episode.goal):
        return True
    shared = set(a.tools) & set(episode.tools_used)
    return len(a.tools) > 0 and len(shared) >= min(2, len(a.tools))


class ProcedureStore:
    """Deterministic evidence tracking for repeated procedures, persisted as
    MemoryRecord(PROCEDURE) THROUGH Thread — no second database or embedding (Phase 13).

    It never changes behavior: retrieval treats a candidate as evidence with a state,
    and nothing here can approve or execute anything."""

    def __init__(
        self,
        thread_memory,
        *,
        event_callback: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self.thread_memory = thread_memory
        self.event_callback = event_callback

    def update_from(self, episode: Episode) -> Optional[ProcedureCandidate]:
        """Merge one episode's evidence. A verified success increments a matching
        candidate's success_count; a failure increments failure_count. Never fabricates
        a match — an episode with no tools yields no candidate."""
        if self.thread_memory is None:
            return None
        try:
            if not episode.tools_used or not episode.goal:
                return None
            cand = self._find_or_create(episode)
            start_state = cand.state
            if episode.episode_status == EpisodeStatus.SUCCESS and \
                    episode.verification_status == VerificationStatus.VERIFIED:
                cand.success_count += 1
                cand.confidence_step = "success"  # type: ignore[attr-defined]
            elif episode.episode_status in (EpisodeStatus.FAILED, EpisodeStatus.UNKNOWN):
                cand.failure_count += 1
            else:
                return cand                       # cancelled / no outcome -> no evidence
            if episode.episode_id and episode.episode_id not in cand.source_episode_ids:
                cand.source_episode_ids.append(episode.episode_id)
            cand._recompute()
            self._persist(cand)
            if cand.state != start_state:
                self._emit("procedure_promoted", {"procedure_id": cand.procedure_id,
                                                  "state": cand.state,
                                                  "goal": _clip_text(cand.goal_pattern, 100)})
            logger.info("procedure candidate %s (%s) successes=%d failures=%d",
                        cand.procedure_id[:8], cand.state, cand.success_count, cand.failure_count)
            return cand
        except Exception:  # noqa: BLE001 — evidence must never break anything
            logger.exception("procedure evidence update failed; skipping.")
            return None

    def _find_or_create(self, episode: Episode) -> ProcedureCandidate:
        existing = self._load_matching(episode)
        if existing is not None:
            return existing
        return ProcedureCandidate(
            goal_pattern=_goal_pattern(episode.goal),
            steps=list(episode.actions)[:MAX_ACTIONS],
            tools=list(episode.tools_used)[:MAX_TOOLS],
        )

    def _load_matching(self, episode: Episode) -> Optional[ProcedureCandidate]:
        try:
            from veronica.memory.thread import RetrievalQuery
            result = self.thread_memory.retrieve(
                RetrievalQuery(query=episode.goal, memory_type=MemoryType.PROCEDURE, top_k=8)
            )
            for mem in result.memories:
                md = mem.metadata or {}
                if md.get("procedure_id") is None:
                    continue
                cand = ProcedureCandidate.from_record(mem)
                if _same_procedure(cand, episode):
                    return cand
            return None
        except Exception:  # noqa: BLE001
            logger.exception("procedure lookup failed; treating as new.")
            return None

    def _persist(self, cand: ProcedureCandidate) -> None:
        record = cand.to_record()
        # Use _store_record which accepts a pre-constructed MemoryRecord with its own ID.
        # This is an internal method but in the same package; ThreadMemory.create_episode
        # uses the same path. Using remember() would generate a new ID.
        safe = self.thread_memory._store_record(record)
        if safe is None:
            logger.warning("procedure candidate %s could not be persisted.", cand.procedure_id[:8])

    def get_candidates(self, goal: str, top_k: int = 3) -> list[ProcedureCandidate]:
        """Advisory retrieval of procedure evidence relevant to a goal."""
        if self.thread_memory is None:
            return []
        try:
            from veronica.memory.thread import RetrievalQuery
            result = self.thread_memory.retrieve(
                RetrievalQuery(query=goal, memory_type=MemoryType.PROCEDURE, top_k=top_k * 2)
            )
            out: list[ProcedureCandidate] = []
            for mem in result.memories:
                md = mem.metadata or {}
                if md.get("procedure_id") is None:
                    continue
                cand = ProcedureCandidate.from_record(mem)
                if _same_procedure(cand, Episode(goal=goal, tools_used=list(cand.tools),
                                                 episode_status=EpisodeStatus.UNKNOWN)):
                    out.append(cand)
                if len(out) >= top_k:
                    break
            self._emit("experience_retrieved", {"count": len(out), "query": _clip_text(goal, 120)})
            return out
        except Exception:  # noqa: BLE001
            logger.exception("procedure retrieval failed; returning none.")
            return []

    def _emit(self, signal: str, args: dict) -> None:
        if self.event_callback is None:
            return
        try:
            self.event_callback(signal, args)
        except Exception:  # noqa: BLE001
            logger.warning("procedure event callback failed for %s", signal)


__all__ = [
    "EpisodeStatus", "ExecutionStatus", "VerificationStatus",
    "ProcedureState", "ProcedureCandidate", "ProcedureStore",
    "map_task_status", "derive_execution_status", "derive_verification_status",
    "derive_episode_status", "EpisodeBuilder", "EpisodeRecorder", "EpisodeRetriever",
]