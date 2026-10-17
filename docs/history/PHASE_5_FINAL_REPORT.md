# VERONICA Phase 5: Learning Effectiveness & Strategy Evolution — Final Report

**Date**: 2026-09-08  
**Commit baseline**: 271 passing tests (Phases 1–4 complete)  
**Final status**: **308 passing tests** (271 baseline + 37 Phase 5)  
**Scope**: Deterministic, observational learning subsystem — no autonomy, no fine-tuning, no RL, no self-modifying code.

## Summary

Phase 5 added two core modules:
1. **`veronica/learning/metrics.py`** — Pure functions over the ExperienceStore that compute task, adaptation, strategy, and safety metrics (no second source of truth).
2. **`veronica/learning/strategy.py`** — Deterministic strategy evolution engine with states OBSERVED → PROMISING → VALIDATED → DEGRADED, configurable evidence thresholds, recent-window degradation, and safety-gated unrestricted-use checks.

All existing tests continue to pass. The new test suite (`tests/test_learning_effectiveness.py`) includes 37 focused unit/property tests and a deterministic 18-step end-to-end validation that simulates a full learning-improvement loop.

The learning system remains strictly observational: it **never executes tools**, only advises the planner or adapts the execution plan (reversibly, via step-copying). Actual execution continues through `TaskExecutor → ToolRegistry.dispatch() → permission/safety gate → tool`.

## Files Created

| Path | Purpose |
|------|---------|
| `veronica/learning/metrics.py` | LearningMetrics class — task, adaptation, strategy, safety, trends, health. |
| `veronica/learning/strategy.py` | StrategyEvolution class — state machine, evidence accumulation, promotion/demotion rules. |
| `tests/test_learning_effectiveness.py` | 37 new tests (unit, property, benchmark, end-to-end). |
| `veronica/learning/__init__.py` | Flattened exports for Phase 5 symbols. |

## Files Modified

| Path | Change |
|------|--------|
| `veronica/config.py` | Added `metrics_enabled`, `strategy_evolution_enabled`, evidence thresholds (`evidence_weak/normal/strong/high`). |
| `config/config.yaml` | Added Phase 5 block under `learning:` with defaults. |
| `veronica/main.py` | Wired `LearningMetrics` and `StrategyEvolution` into `App`, gated on config flags. |
| `README.md` | Updated test count (271 → 308), added Phase 5 summary, clarified observational nature. |
| `docs/GETTING_STARTED.md` | Updated test count. |
| `tests/test_learning_effectiveness.py` | Fixed E2E test assertion about adapter behavior on concrete-arg steps (Rule 4: current context overrides learned preference). |

## Core Metrics Implemented

All metrics are **pure functions** over `ExperienceStore.all()` — cached per-call, no incremental counters, no background services.

### TaskMetrics (`veronica/learning/metrics.py`)
- `total`, `successful`, `failed`, `recovered`, `cancelled`
- `avg_retries`, `avg_duration`
- `success_rate`, `recovery_rate`, `retry_rate`

### AdaptationMetrics (observational, not causal)
- `total_adapted`, `adaptation_succeeded`, `adaptation_failed`
- `baseline_total`, `baseline_successful`, `adapted_total`, `adapted_successful`
- `baseline_success_rate`, `adapted_success_rate`
- `observational_difference = round(adapted_success_rate - baseline_success_rate, 4)`

### SafetyMetrics (critical invariant: `unsafe_adaptations_applied == 0`)
- `unsafe_adaptations_applied` (adaptation applied + failed step is safety-sensitive)
- `blocked_safety_sensitive` (adaptations rejected by policy for safety)
- `policy_rejections`, `permission_rejections`, `secret_redaction_events`

### StrategyMetrics (delegated to `strategy_metrics()`)
Per-strategy dict: `successes`, `failures`, `recoveries`, `sample_count`, `success_rate`, `failure_rate`, `recovery_rate`, `avg_duration`.

### Time-Based Trends (`daily_trends(days)`)
List of `TrendBucket` per ISO date (last `days` days): `period`, `total`, `successful`, `failed`, `recovered`, `avg_retries`, `success_rate`.

### LearningHealth
One of: `HEALTHY`, `INSUFFICIENT_DATA` (<3 experiences), `DEGRADED` (recent failure rate >50% or `unsafe_adaptations_applied > 0`), `BLOCKED` (learning disabled).

## Strategy Evolution Model

Deterministic state machine in `StrategyEvolution._compute_state(evidence)`:

1. **OBSERVED** — no counted evidence.
2. **DEGRADED** — if *either*:
   - Recent window: `recent_failures >= RECENT_DEGRADE_FAILURES (3)` **and** `recent_failures > recent_positive`
   - Cumulative: `failures >= 3` **and** `failures > positives`
3. **VALIDATED** — `positives >= STRONG threshold (5)`
4. **PROMISING** — `positives >= WEAK threshold (2)`
5. **OBSERVED** — otherwise.

### Evidence Accumulation
- Counts only non-cancelled experiences involving the strategy.
- `total_samples` = successes + failures + recoveries.
- Recent window: last `RECENT_WINDOW = 5` non-cancelled observations (by timestamp desc).
- `recent_successes`, `recent_recoveries`, `recent_failures` feed `recent_positive` and `recent_success_rate`.
- **Cancellation is neutral** — never counted as success or failure.
- **Recovery is moderate positive** — counted in `recoveries`, not equivalent to success.

### Promotions/Demotions
- **Promotion**: OBSERVED → PROMISING (≥ WEAK), PROMISING → VALIDATED (≥ STRONG).
- **Demotion**: VALIDATED → DEGRADED (per rules above), DEGRADED → PROMISING (if recent window improves).
- All transitions are **reversible** via new evidence.

### Unrestricted-Use Gate
`StrategyEvolution.is_safe_for_unrestricted_use(name)`:
1. If `name in SAFETY_SENSITIVE_TOOLS` → `False` (never unrestricted).
2. Else requires:
   - `state == VALIDATED`
   - `positives >= STRONG threshold (5)`

## Evidence Thresholds (configurable, not hardcoded)

In `EvidenceThresholds` and `LearningConfig`:
- `OBSERVATION = 1` — 1 success: observation only.
- `WEAK = 2` — 2+ successes/recoveries → eligible for PROMISING.
- `NORMAL = 3` — 3+ → eligible for normal confidence.
- `STRONG = 5` — 5+ → eligible for VALIDATED and unrestricted-use consideration.
- `HIGH = 10` — 10+ → high-evidence strategy.

## Decay Behavior (Recent-Window Degradation)

- Only the last `RECENT_WINDOW = 5` non-cancelled observations count toward recent positives/failures.
- A strategy can be demoted to DEGRADED if recent failures ≥ 3 **and** outnumber recent positives, even with a strong cumulative history.
- This prevents one failure from destroying trust while allowing *current* performance to trigger review.

## Adaptation Effectiveness Tracking

`AdaptationMetrics` compares baseline vs adapted groups **observationally**:
- Never claims causation — only reports observed difference.
- `adaptation_success` counts `SUCCESS + RECOVERED` (recovery is moderate positive).
- Safety-sensitive adaptations are never counted as successful if they fail (tracked in `unsafe_adaptations_applied`).

## Safety & Privacy

- **Zero secrets in metrics**: Experience redactor strips API keys, passwords, tokens, etc., before persistence.
- **Safety-sensitive tools never unrestricted**: `is_safe_for_unrestricted_use` blocks them regardless of evidence.
- **Unsafe adaptations applied = 0** invariant: continuously monitored; any nonzero value triggers `LearningHealth.DEGRADED` with critical reason.
- **No external services**: No Redis, no vector DB, no network calls.

## Performance

- Pure functions over in-memory experience list (cached per metric set).
- No incremental counters; full recompute on each metrics call (acceptable for <10k experiences).
- Lazy trend computation: `daily_trends` sorts and buckets on demand.

## Configuration

All Phase 5 features gated by `learning.config`:
- `learning.enabled` — master switch (False disables everything).
- `learning.metrics_enabled` — controls `LearningMetrics` construction.
- `learning.strategy_evolution_enabled` — controls `StrategyEvolution` construction.
- `learning.evidence_weak/normal/strong/high` — thresholds (default 2/3/5/10).

## Test Results

### Before Phase 5
- Baseline: **271 passing tests** (Phases 1–4).

### After Phase 5
- New test file: `tests/test_learning_effectiveness.py` — **37 tests**.
- **36/37 pass on first run**; 1 failure in `test_full_improvement_loop` due to mistaken expectation about adapter behavior on concrete-arg steps (fixed by correcting test to match Rule 4: current context overrides learned preference).
- **Final run**: **37/37 pass**.
- **Full suite**: **308 passing tests**, 0 failed.

### Benchmark & Property Tests
- `TestBenchmark` — synthetic experience loads; metrics compute deterministically.
- `TestInvariants` — e.g., `observational_difference` bounded in [-1.0, 1.0]; safety invariant holds.
- `TestLearningHealth` — transitions HEALTHY → INSUFFICIENT_DATA → DEGRADED → BLOCKED as expected.
- `TestTrends` — daily buckets align with ISO dates and experience timestamps.

### End-to-End Validation (`test_full_improvement_loop`)
18-step deterministic loop:
1. Seed baseline strategy (find_file: 6 success, 4 fail).
2. Seed competing strategy (search_workspace: 10 success).
3. Strategy evolution promotes search_workspace to VALIDATED.
4. Metrics confirm baseline vs adapted totals.
5. Adapter evaluates STRONG lesson on plan copy.
6. **Adapter does NOT apply when step has concrete args** (current context wins).
7. Execute adapted plan via gated `TaskExecutor`.
8. Audit trail records adaptation with correct outcome.
9. Degrade better strategy (4 failures) → state DEGRADED.
10. Safety-sensitive strategy (delete_file) reaches VALIDATED but `is_safe_for_unrestricted_use` returns False.
11. Permission gate remains authoritative (any delete_file step would be SENSITIVE-checked).
12–18. Additional checks on experience recording, reversibility, no tool execution by learning subsystems.

## Known Limitations

1. **Observational only**: Metrics report correlation, never causation. Adapted success may stem from cohort differences, not the adaptation itself.
2. **No neural or statistical models**: Pure counting; no confidence intervals, no regression, no embeddings.
3. **Experience store size**: JSONL appends indefinitely; no built-in rotation or archiving (external logrotate recommended).
4. **Trend computation**: Sorts all experiences on each call; acceptable for modest datasets.
5. **Strategy groups**: Tool substitutability relies on hardcoded `TOOL_GROUPS`; future extensions may need dynamic grouping.
6. **User feedback as strong evidence**: Treated as STRONG if confidence ≥ 0.90, but still subject to relevance/safety/context/policy (no blind application).

## Recommended Next Phase

**Phase 6 — Experience-Driven Planning Tuning**  
*Goal*: Use learned evidence to **bias the planner’s prior** (not to override it) while preserving the deterministic, observational nature of Phases 3–5.

### Proposed Changes
1. **Planner context weighting**: Modify `retrieval.py` to weight lessons by `evidence_count` and `confidence` when forming the advisory context.
2. **Confidence-aware retrieval**: Return top-N lessons by a composite score (evidence × confidence × recency).
3. **Lesson expiration**: Add configurable max age for advisory lessons (beyond which they are dropped from context).
4. **Safety-first retrieval**: Never return lessons for safety-sensitive tools as planner context (only as experiential record).
5. **Metrics hook**: Export a `get_planner_bias()` method in `LearningMetrics` that returns strategy-specific adjustment factors (e.g., log-odds of success rate) for the planner to consume as **advisory only**.
6. **No plan mutation**: The learning system still **never mutates** a planner-generated plan; it only influences the input context.

### Non-Goals (to preserve Phase 5 invariants)
- No direct modification of tool selection or step arguments by learning.
- No reinforcement learning or policy gradients.
- No model fine-tuning or weight updates.
- No autonomous tool execution by learning subsystems.
- All execution remains gated by `TaskExecutor → ToolRegistry → permission/safety gate → tool`.

### Success Criteria
- All existing tests continue to pass.
- New test suite validates that planner context is biased toward high-evidence strategies without overriding explicit instructions or concrete args.
- Safety invariants remain unchanged (`unsafe_adaptations_applied == 0`).
- Determinism preserved: same input experiences → same planner context → same plan distribution (LLM temperature aside).

---
**Phase 5 Complete**. The learning subsystem now delivers deterministic, privacy-safe metrics and strategy evolution while upholding the core architectural boundary: **learning advises; execution executes**.