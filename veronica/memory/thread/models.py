"""
Thread Memory V1 — data model.

A ``MemoryRecord`` is the atomic unit of Thread memory. It is deliberately a plain,
serializable dataclass (stdlib-only) so it can be persisted, exported, imported, and
passed between layers without coupling VERONICA to any vector backend.

Memory types are a *controlled vocabulary*, not a rigid enum: new types are added by
writing one more string constant into ``MemoryType`` and using it. The retriever and
consolidator treat unknown types as opaque tags (they never switch exclusively on the
enum), so a future phase can add e.g. ``PATTERN`` or ``DORMANT`` without editing the
matching algorithm.

Privacy is a first-class concern (see ``veronica.memory.thread.privacy``): every record
that will be persisted is passed through ``sanitize_memory_record`` so keys/values that
look like secrets are replaced with ``[REDACTED]`` before writing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


class MemoryType:
    """Controlled vocabulary of memory kinds. Extensible — new types are new string
    constants, not enum members, so retriever/consolidator code never has to enumerate
    a closed set."""

    CONVERSATION = "conversation"
    FACT = "fact"
    PREFERENCE = "preference"
    EVENT = "event"
    EXPERIENCE = "experience"
    PROCEDURE = "procedure"
    PROJECT = "project"
    IDEA = "idea"
    RELATIONSHIP = "relationship"

    # The initial vocabulary, for validation/logging. Not an exhaustive constraint.
    KNOWN = frozenset({
        CONVERSATION, FACT, PREFERENCE, EVENT, EXPERIENCE,
        PROCEDURE, PROJECT, IDEA, RELATIONSHIP,
    })


def now_iso() -> str:
    """Current timestamp in the project's ISO format (second precision)."""
    return datetime.now().isoformat(timespec="seconds")


def new_memory_id() -> str:
    """Short unique id (same 12-hex pattern as Experience.experience_id and Fact ids)."""
    return uuid.uuid4().hex[:12]


@dataclass
class MemoryRecord:
    """One durable memory.

    ``embedding_ref`` is an opaque reference to whatever the vector store holds for this
    record (e.g. a collection/id pair). Layer boundaries: the store owns the vector;
    the record only *mentions* it so export/import and debugging can reconnect them.
    """

    content: str
    memory_type: str = MemoryType.CONVERSATION
    id: str = field(default_factory=new_memory_id)
    timestamp: str = field(default_factory=now_iso)          # when the memory refers to
    created_at: str = field(default_factory=now_iso)          # when VERONICA recorded it
    updated_at: str = field(default_factory=now_iso)
    importance: float = 0.5                                   # 0.0 (trivial) .. 1.0 (core)
    confidence: float = 1.0                                   # 0.0 (uncertain) .. 1.0 (certain)
    provenance: str = ""                                      # e.g. "user", "assistant", "experience", "consolidation"
    source: str = ""                                          # e.g. "conversation", "task:T123", "note"
    tags: list[str] = field(default_factory=list)
    project: str = ""                                         # optional project name
    chapter: str = ""                                         # optional narrative/life chapter (future)
    related_memory_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)    # free-form, redacted on persist
    embedding_ref: Optional[str] = None
    deleted: bool = False                                     # soft-delete flag

    # -- serialization ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "memory_type": self.memory_type,
            "content": self.content,
            "timestamp": self.timestamp,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "importance": self.importance,
            "confidence": self.confidence,
            "provenance": self.provenance,
            "source": self.source,
            "tags": list(self.tags),
            "project": self.project,
            "chapter": self.chapter,
            "related_memory_ids": list(self.related_memory_ids),
            "metadata": self.metadata,
            "embedding_ref": self.embedding_ref,
            "deleted": self.deleted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        """Rebuild from a dict. Tolerant of missing keys (defaults) but never fabricates
        content. Raises ValueError if content is absent/not a string — a corrupted record
        is a data problem, not a silent empty memory."""
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("MemoryRecord requires non-empty string 'content'")
        return cls(
            content=content,
            memory_type=data.get("memory_type", MemoryType.CONVERSATION),
            id=str(data.get("id") or new_memory_id()),
            timestamp=str(data.get("timestamp") or now_iso()),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            importance=float(data.get("importance", 0.5)),
            confidence=float(data.get("confidence", 1.0)),
            provenance=str(data.get("provenance", "") or ""),
            source=str(data.get("source", "") or ""),
            tags=list(data.get("tags", []) or []),
            project=str(data.get("project", "") or ""),
            chapter=str(data.get("chapter", "") or ""),
            related_memory_ids=list(data.get("related_memory_ids", []) or []),
            metadata=dict(data.get("metadata", {}) or {}),
            embedding_ref=data.get("embedding_ref"),
            deleted=bool(data.get("deleted", False)),
        )

    # -- convenience -------------------------------------------------------------
    def touch(self) -> None:
        """Mark the record updated. Called whenever the record changes."""
        self.updated_at = now_iso()

    def short_text(self, limit: int = 80) -> str:
        c = self.content.replace("\n", " ")
        return c if len(c) <= limit else c[: limit - 3] + "..."


# --- small value objects -----------------------------------------------------------


@dataclass
class Episode:
    """A structured 'one thing happened' record — the persistent form of episodic memory.

    Kept separate from MemoryRecord because an episode carries fields (goal, tools,
    result, verification, errors, recovery) that a general memory does not need, and it
    is *converted* into one-or-more MemoryRecords at consolidation time. Raw internal
    data is never persisted — use ``Episode.to_memory_records()`` to build persistable
    records and ``privacy.sanitize_memory_record`` on the way in.
    """

    goal: str
    actions: list[str] = field(default_factory=list)       # step descriptions
    tools_used: list[str] = field(default_factory=list)
    result: str = ""                                       # short human summary
    success: Optional[bool] = None                         # None = unknown
    verification: str = ""                                 # "passed" | "failed" | "unverifiable" | ""
    errors: list[str] = field(default_factory=list)
    recovery_attempts: int = 0
    timestamp: str = field(default_factory=now_iso)
    duration_sec: float = 0.0
    tags: list[str] = field(default_factory=list)
    project: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # Optional identity when the episode came from an Experience record.
    linked_experience_id: str = ""

    # -- Episodic Memory Integration V1 (all optional, backward compatible) -------
    # ``episode_status`` is the authoritative outcome (EpisodeStatus vocabulary:
    # success / failed / blocked / cancelled / unknown). ``execution_status`` and
    # ``verification_status`` preserve the EXECUTED vs VERIFIED distinction — an action
    # that ran but could not be verified is recorded honestly as execution=executed,
    # verification=unknown (NEVER promoted to success).
    episode_id: str = field(default_factory=new_memory_id)
    episode_status: str = ""            # EpisodeStatus value
    execution_status: str = ""          # executed | not_executed | partial | unknown | ""
    verification_status: str = ""       # verified | failed | unknown | not_applicable | ""
    task_type: str = ""                 # GENERAL / CHAT / CODING / ... (TaskAnalyzer vocab)
    plan_summary: str = ""              # compressed plan (never the full runtime plan)
    observations: list[str] = field(default_factory=list)
    verification_results: list[dict] = field(default_factory=list)   # per-action verification
    failures: list[dict] = field(default_factory=list)               # failed actions + category
    recoveries: list[dict] = field(default_factory=list)             # recovery attempts + results
    model_id: str = ""
    provider: str = ""
    routing_role: str = ""
    started_at: str = ""                # ISO start (empty => same as timestamp)

    def to_memory_records(self, *, importance: float = 0.6, confidence: float = 0.7) -> list[MemoryRecord]:
        """Project this episode into persistable MemoryRecords:
          - one EXPERIENCE record describing the whole episode
          - one FACT/PROCEDURE record if there was a verified success path (a strategy
            that worked) — procedural knowledge for later reuse.

        Never contains the raw action/error strings as-is unless they were already
        sanitized; callers should run ``privacy.sanitize_memory_record`` on the result
        before writing."""
        base_meta: dict[str, Any] = {
            "duration_sec": self.duration_sec,
            "recovery_attempts": self.recovery_attempts,
        }
        if self.linked_experience_id:
            base_meta["experience_id"] = self.linked_experience_id

        # Episodic Integration V1: persist the authoritative status vocabulary so future
        # retrieval can rank by outcome ("last time UIA failed, visual fallback worked")
        # without storing raw runtime objects. Unknown stays unknown — never inferred.
        for key, val in (
            ("episode_status", self.episode_status),
            ("execution_status", self.execution_status),
            ("verification_status", self.verification_status),
            ("task_type", self.task_type),
            ("model_id", self.model_id),
            ("provider", self.provider),
            ("routing_role", self.routing_role),
        ):
            if val:
                base_meta[key] = val
        if self.failures:
            base_meta["failure_categories"] = [f.get("category") for f in self.failures if f.get("category")]
        if self.recoveries:
            base_meta["recovery_count"] = len(self.recoveries)

        # 1. The episode's own record.
        summary = self.goal or "Untitled episode"
        if self.result:
            summary = f"{self.goal} → {self.result}" if self.goal else self.result
        episode_record = MemoryRecord(
            memory_type=MemoryType.EXPERIENCE,
            content=summary,
            timestamp=self.timestamp,
            importance=importance,
            confidence=confidence,
            provenance="experience",
            source=f"episode:{self.linked_experience_id}" if self.linked_experience_id else "episode",
            tags=list(self.tags),
            project=self.project,
            metadata={
                **base_meta,
                "success": self.success,
                "verification": self.verification,
                "tools_used": list(self.tools_used),
                "num_errors": len(self.errors),
            },
        )

        records = [episode_record]

        # 2. A procedural record when there was a verified success path worth reusing.
        if self.success and self.verification == "passed" and self.tools_used:
            steps = self.actions or [f"use {t}" for t in self.tools_used]
            proc = MemoryRecord(
                memory_type=MemoryType.PROCEDURE,
                content="To " + (self.goal or "perform this task") + ": "
                        + " then ".join(s.strip() for s in steps if s.strip()),
                timestamp=self.timestamp,
                importance=max(0.35, importance - 0.15),
                confidence=confidence,
                provenance="experience",
                source=episode_record.source,
                tags=[t for t in self.tags if t not in ("episode",)] or ["procedure"],
                project=self.project,
                metadata={**base_meta, "success": True, "tools_used": list(self.tools_used)},
                related_memory_ids=[episode_record.id],
            )
            records.append(proc)

        return records

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "actions": list(self.actions),
            "tools_used": list(self.tools_used),
            "result": self.result,
            "success": self.success,
            "verification": self.verification,
            "errors": list(self.errors),
            "recovery_attempts": self.recovery_attempts,
            "timestamp": self.timestamp,
            "duration_sec": self.duration_sec,
            "tags": list(self.tags),
            "project": self.project,
            "metadata": self.metadata,
            "linked_experience_id": self.linked_experience_id,
            "episode_id": self.episode_id,
            "episode_status": self.episode_status,
            "execution_status": self.execution_status,
            "verification_status": self.verification_status,
            "task_type": self.task_type,
            "plan_summary": self.plan_summary,
            "observations": list(self.observations),
            "verification_results": self.verification_results,
            "failures": self.failures,
            "recoveries": self.recoveries,
            "model_id": self.model_id,
            "provider": self.provider,
            "routing_role": self.routing_role,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Episode":
        return cls(
            goal=str(data.get("goal", "") or ""),
            actions=list(data.get("actions", []) or []),
            tools_used=list(data.get("tools_used", []) or []),
            result=str(data.get("result", "") or ""),
            success=data.get("success"),
            verification=str(data.get("verification", "") or ""),
            errors=list(data.get("errors", []) or []),
            recovery_attempts=int(data.get("recovery_attempts", 0) or 0),
            timestamp=str(data.get("timestamp") or now_iso()),
            duration_sec=float(data.get("duration_sec", 0.0) or 0.0),
            tags=list(data.get("tags", []) or []),
            project=str(data.get("project", "") or ""),
            metadata=dict(data.get("metadata", {}) or {}),
            linked_experience_id=str(data.get("linked_experience_id", "") or ""),
            episode_id=str(data.get("episode_id") or new_memory_id()),
            episode_status=str(data.get("episode_status", "") or ""),
            execution_status=str(data.get("execution_status", "") or ""),
            verification_status=str(data.get("verification_status", "") or ""),
            task_type=str(data.get("task_type", "") or ""),
            plan_summary=str(data.get("plan_summary", "") or ""),
            observations=list(data.get("observations", []) or []),
            verification_results=list(data.get("verification_results", []) or []),
            failures=list(data.get("failures", []) or []),
            recoveries=list(data.get("recoveries", []) or []),
            model_id=str(data.get("model_id", "") or ""),
            provider=str(data.get("provider", "") or ""),
            routing_role=str(data.get("routing_role", "") or ""),
            started_at=str(data.get("started_at", "") or ""),
        )


__all__ = [
    "MemoryType", "MemoryRecord", "Episode", "now_iso", "new_memory_id",
]