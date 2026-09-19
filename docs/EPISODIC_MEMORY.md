# EPISODIC MEMORY INTEGRATION V1

> Authoritative spec for connecting the task executor's execution lifecycle to Thread Memory. Every claim below is enforced by code and verified by tests (19 dedicated + full-suite regression at 626 pass / 1 skip, confirmed 3× consecutive).

---

## 1. The cognitive loop (design intent, not code magic)

```
USER GOAL → UNDERSTAND → PLAN → EXECUTE → OBSERVE → VERIFY → SUCCESS / FAILURE
                                    ↓
                           EPISODIC EXPERIENCE
                                    ↓
                           THREAD MEMORY (exposure + procedure)
                                    ↓
                           FUTURE RETRIEVAL → BETTER PLANNING
```

This is the loop the integration exists for, not a guarantee of the loop. The code only guarantees: (a) every terminal `Experience` produces exactly one `Episode`; (b) episodes are stored; (c) episodes are retrievable; (d) none of this can change behavior, approve actions, or grant permission.

---

## 2. Authoritative source — never duplicated

- **One source of task outcome:** `TaskExecutor._emit_experience()` — the only code that knows whether a multi-step execution succeeded, failed, or recovered.
- **Consumption path:** `TaskExecutor` → `experience_callback` (wired by `main.py` when `config.learning.enabled or thread_memory is not None`) → `EpisodeRecorder.record()`.
- **Fault isolation:** every consumer catches its own errors (`EpisodeRecorder.record()` can return `[]`; `ProcedureStore.update_from()` can return `None`; neither raises into `TaskExecutor`). Memory failure is logged, never a task failure.
- **No second source:** the orchestrator never constructs episodes; only the executor's experience produces them.

---

## 3. Vocabulary (controlled, never free-text in persistent fields)

### EpisodeStatus
- `SUCCESS` = executed + verified (or explicit completed + verified)
- `FAILED` = executed + failed verification, or execution failed, or mapped `failed`
- `STREAM_FAILURE` = explicit `stream_failed` / `stream_error` (Phase 20 — a streaming
  turn failed at the transport level; distinct from task `FAILED`, and never collapsed
  into it even when the task also ran)
- `BLOCKED` = explicit `blocked` (wins over all others)
- `CANCELLED` = explicit `cancelled` (wins over all others)
- `UNKNOWN` = ambiguous / unverified / unrecognised / not executed with ambiguous report

### ExecutionStatus
- `EXECUTED` = at least one step completed/recovered/failed
- `PARTIAL` = mixed completed + failed/recovered
- `NOT_EXECUTED` = nothing completed/recovered/failed
- `UNKNOWN` = no step data

### VerificationStatus
- `VERIFIED` = all completed steps have `verified=True`
- `FAILED` = at least one completed step has `verified=False` and none have `verified=True`
- `UNKNOWN` = some completed steps verified, some not (contradictory, never promoted)
- `NOT_APPLICABLE` = nothing completed to verify

---

## 4. Status derivation rules (deterministic, conservative)

```
map_task_status("completed") = SUCCESS
map_task_status("failed")    = FAILED
map_task_status("stream_failed") = STREAM_FAILURE   (Phase 20)
map_task_status("stream_error")  = STREAM_FAILURE   (Phase 20)
map_task_status("blocked")    = BLOCKED
map_task_status("cancelled") = CANCELLED
map_task_status(unknown/empty/random) = UNKNOWN  (never SUCCESS)

derive_execution_status(steps) → EXECUTED / PARTIAL / NOT_EXECUTED / UNKNOWN
derive_verification_status(steps) → VERIFIED / FAILED / UNKNOWN / NOT_APPLICABLE

derive_episode_status(explicit, execution, verification):
  1. CANCELLED/BLOCKED/STREAM_FAILURE wins unconditionally
  2. NOT_EXECUTED → FAILED (if mapped FAILED) else UNKNOWN
  3. PARTIAL → FAILED (always; partial execution is failure)
  4. EXECUTED + VERIFIED → SUCCESS
  5. EXECUTED + FAILED → FAILED
  6. EXECUTED + UNKNOWN → UNKNOWN (never promoted)
```

**Critical invariant:** `derive_episode_status("completed", "executed", "unknown") == UNKNOWN`. Executed-but-unverified stays unknown — no promotion by optimism.

---

## 5. Episode data model

Fields (from `Episode` dataclass + 14 extended fields added to `models.py`):
- `episode_id`: 12-char hex (generated at build time, never reused)
- `goal`: clipped to `MAX_GOAL_CHARS` (500)
- `actions`: list of step descriptions, clipped to `MAX_ACTIONS` (50)
- `tools_used`: list of tool names from steps / `experience.tool_calls`, clipped to 50
- `result`: `result_summary` from experience metadata, or `final_status`, or `outcome`; clipped to 400
- `success`: bool, `True` only when `episode_status == SUCCESS`
- `verification`: `"passed"` / `"failed"` / `"unverifiable"`
- `errors`: list of `{action, tool, error, category}` derived from failed steps; clipped to 20
- `recovery_attempts`: `len(recoveries)`
- `duration_sec`: `experience.duration`; rounded
- `tags`: from `tags` kwarg or experience metadata
- `project`: from `project` kwarg
- `metadata`: redacted observational context (`outcome`, `retries`, `verified_success_path`, `duration_sec`, plus enriched `extra` if provided — redacted through `redact_value()`)
- `linked_experience_id`: `experience.experience_id`
- `episode_status`, `execution_status`, `verification_status`: from derivation
- `task_type`, `plan_summary`: from build context / step trace
- `observations`: verification notes from completed/recovered steps; clipped to 50
- `verification_results`: structured `{step, result, note}`; clipped to 20
- `failures`: structured failures with `action/tool/error/category`; clipped to 20
- `recoveries`: structured `{attempt, tool, result}` from recovery actions; clipped to 20
- `model_id`, `provider`, `routing_role`: from build context / experience metadata
- `started_at`: `experience.timestamp` or current time

---

## 6. Building episodes (`EpisodeBuilder`)

```python
builder = EpisodeBuilder()
episode = builder.build(
    experience,
    task_type="GENERAL",
    model_id="test:model",
    provider="test",
    routing_role="fast",
    task_dict=optional_task_dict,  # enriches steps from task.to_dict()
    project="",
    tags=[],
)
```

**Priority of step data:**
1. `task_dict.get("steps")` (richest — carries `description`, `tool`, `status`, `verified`, `verification_note`, `error`, `attempts`)
2. `experience.actions` (fallback — carries `description`, `tool`, `status`, `verified`; now also preserves `error` after fix)

**Failure extraction:** for any step with `status == "failed"` or `error` present, a `failures` entry is added with deterministic category from `_categorize_error()` (`permission_denied`, `capability_unavailable`, `timeout`, `not_found`, `connection_failed`, `invalid_input`, or default `execution_failed`).

**Recovery extraction:** `experience.recovery_actions` → `recoveries` list; each retry tool is matched against step trace to determine if it ended `completed`/`recovered` (`success=True`) or still `failed`.

**Plan summary:** compressed `"tool: description"` lines joined by `" | "`, clipped to `MAX_SUMMARY_CHARS`.

---

## 7. Recording (`EpisodeRecorder`)

```python
recorder = EpisodeRecorder(thread_memory, event_callback=callback)
records = recorder.record(experience, task_dict=None, **ctx)
```

- If `thread_memory is None` → returns `[]`, emits nothing, never raises.
- Builds `Episode` via `EpisodeBuilder`.
- Persists via `thread_memory.create_episode()` (returns list of stored `MemoryRecord` objects — EXPERIENCE + optional PROCEDURE).
- Stores `recorder.last_episode` (most recent `Episode`, or `None` if failed).
- Emits SSE-style events through `event_callback`: `episode_started`, `episode_completed`, `memory_written`.

---

## 8. Retrieval (`EpisodeRetriever`)

```python
retriever = EpisodeRetriever(thread_memory, default_top_k=3, event_callback=callback)
picked = retriever.retrieve("make tea", top_k=2, outcome="success", task_type="GENERAL", exclude_ids=["e1"])
ctx = retriever.format_context(picked, limit=3)
```

- If `thread_memory is None` → returns `[]`.
- Internally builds `RetrievalQuery(memory_type=MemoryType.EXPERIENCE, ...)` and delegates to `ThreadMemory.retrieve()` — **reuses existing retrieval, no second DB**.
- `format_context()` produces the labeled block with `[RETRIEVED EXPERIENCE]`, outcome/verification notation (`[success/verified]` / `[failed/failed]` / etc.), content excerpt, score, and optional tag notes.
- Emits `experience_retrieved` event with `{"count": len(out), "query": ...}`.

---

## 9. Procedure evidence (`ProcedureStore`)

```python
store = ProcedureStore(thread_memory, event_callback=callback)
store.update_from(episode)          # updates / creates evidence for goal+tools
store.get_candidates("proc goal", top_k=3)  # advisory retrieval
```

- `_find_or_create(episode)` finds existing `MemoryRecord(PROCEDURE)` by `goal_pattern` + `tools`; creates new if missing or persistence failed.
- `_persist()` writes via `ThreadMemory._store_record()` (preserves `MemoryRecord.id` — uses internal path so record identity survives).
- `_recompute()` updates `state`, `confidence`, counts, timestamp deterministically.
- Promotion thresholds (`promote_at=2`, `validate_at=3`, `degrade_at=2`) are fixed fields on the candidate, configurable per-instance.
- Emission: `procedure_promoted` when state changes.

---

## 10. Privacy and isolation guarantees

- Redaction via `redact_value()` applies before persistence (experience.metadata, episode.metadata, procedure metadata all go through it).
- No raw screenshots / screen text / credential-shaped content is persisted in episodes (verification_notes are text-only, clipped).
- No second embedding / vector DB — retrieval delegates to Thread's `ThreadRetriever`.
- No permission granted by retrieval — `format_context()` produces informational labels (`[RETRIEVED EXPERIENCE]`) that the orchestrator injects into the prompt; the planner decides whether to reference them.
- Memory failure never blocks execution (verified: `EpisodeRecorder(None)` returns `[]`; test suite passes 626/626 with Thread disabled).

---

## 11. Integration points (verified)

- **TaskExecutor** (`automation/task_executor.py`): `create_task()` accepts `task_meta`; `_build_experience()` extracts `task_type`, `model_id`, `provider`, `routing_role` into `Experience.metadata`; `experience_callback` wired when `config.learning.enabled or thread_memory is not None`.
- **Orchestrator** (`core/orchestrator.py`): constructor accepts `episode_recorder`, `episode_retriever`, `procedure_store`; `_get_experience_context()` bounded by `episode_context_limit` (default 3); `_build_messages()` injects `RETRIEVED EXPERIENCE` block after `RETRIEVED MEMORY`; `_handle_agentic()` builds `task_meta` with `model_id`/`provider` from `config.llm`.
- **App / build_app** (`main.py`): Thread built early; episodic components created when `thread_memory is not None`; `episode_recorder.record()` and `procedure_store.update_from()` called in `_on_experience()`; `experience_callback` set accordingly.
- **Bridge** (`ui/stitch/bridge.py`): `_on_episode_event()` broadcasts `episode_started`, `episode_completed`, `memory_written`, `experience_retrieved`, `procedure_promoted` to Stitch.

---

## 12. Test verification (as of this session)

- **19 dedicated episodic tests:** all pass (status vocabularies, derivation rules, builder basic/failure/recovery/size-limits, to_memory_records, recorder fault-isolation/emit, retriever fault-isolation/formatted-output, procedure candidate promotion, store fault-isolation/emit, orchestrator wiring).
- **Full suite:** 626 passed, 1 skipped, confirmed over 3 consecutive runs (no regression from the previous 607 baseline + 19 new tests).
- **Safety invariants verified:** `PermissionGate` unchanged; `UNKNOWN` never promoted; SHA-256 identity signatures used (not Python `hash()`); memory retrieval only injects advisory context (no permission elevation); 19 episodic tests cover failure isolation (Thread = None, memory write failure, retrieval failure, procedure persistence failure).

---

*File: `docs/EPISODIC_MEMORY.md` — created as part of Episodic Memory Integration V1 validation. References `veronica/memory/thread/episodes.py` (core), `veronica/memory/thread/models.py` (extended Episode), `tests/test_episodic_memory.py` (verification), `veronica/automation/task_executor.py`, `veronica/core/orchestrator.py`, `veronica/main.py`, `veronica/ui/stitch/bridge.py` (integration).*