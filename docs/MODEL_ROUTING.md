# Multi-Model Routing V1 (`llm/routing/`)

VERONICA's **model-selection layer**: pick the best configured provider/model for each
request — a fast local model for chat, a reasoning/coding model for hard questions, a
vision-capable model when the task needs it — without the rest of the brain ever knowing
which one answered.

```
USER REQUEST → CONTEXT ENGINE → TASK ANALYSIS → MODEL ROUTER
             → [ FAST | REASONING | CODING | VISION | GENERAL ] providers
             → capability-preserving FALLBACK → RESPONSE
```

**The core promise:** the router decides **which** model; everything else in VERONICA
keeps using the same `LLMProvider` interface it always did. Add a new model to config,
and today's tools, agentic plans, Thread Memory, and front-ends work unchanged — they ask
`which model?`, the router answers, and they don't care what the answer was.

---

## 1. Opt-in and backward compatible

Routing is **disabled by default** (`routing.enabled: false`). When disabled:

- `build_routing(config)` returns `None`; no router, registry, or analyzer is created.
- `Orchestrator` uses the original single-model `llm` provider exactly as before.
- Behavior is **byte-identical** to the pre-routing build (verified by tests).

When enabled **without** `routing.models`, the global `llm.*` block is synthesized into a
single routing default model — and that default explicitly declares `supports_tools: true`
because the single-model path always handed tools to the model (preserving the 1:1
behavior). So routing can be enabled safely on an existing config with no model list.

Bad routing config is rejected **at build time** (`ValueError`), never mid-request:
unknown provider, duplicate `model_id`, or no default. `build_app` catches the failure and
falls back to the single-model path, logging the exception — routing can never prevent
VERONICA from booting.

## 2. The pieces

| File | Responsibility |
|---|---|
| `profiles.py` | `ModelProfile` (frozen dataclass): `model_id`, `provider`, `role`, `capabilities` (vision/tools/streaming), `task_types`, `context_window`, `speed_class`, `reasoning_class`, `cost_class`, `priority`, `default`, `enabled`, per-model `base_url` / `api_key_env` / `temperature` / `timeout_s`. `summary()` never leaks secrets. |
| `task.py` | `TaskAnalyzer` — **deterministic** classification into 9 task types via whole-token keyword vocabularies. No LLM call. |
| `registry.py` | `ModelRegistry` — metadata only: register/unregister, `find_by_role` / `find_capable`, lazy singleton `provider()` factories, `check_health(force=)` TTL caching. |
| `health.py` | `ModelHealth` — `AVAILABLE / UNKNOWN / DEGRADED / UNAVAILABLE`; 1st failure → DEGRADED, 2nd → UNAVAILABLE, success resets. |
| `router.py` | `ModelRouter.route()` + `ModelSelection` (the chosen model **and** its capability-preserving fallback chain) + `RoutedResponse`. |
| `builder.py` | `build_routing(config)` — the **only** place config becomes profiles + provider factories; reuses `llm.router.build_provider_for_model` (ollama / openai_compatible / echo). |

## 3. Task analysis — no LLM to pick the LLM

`TaskAnalyzer.analyze(user_text, ...)` inspects the **current user turn** and produces a
`TaskProfile` with a `task_type` drawn from a fixed vocabulary:

```
GENERAL · CHAT · REASONING · CODING · PLANNING · TOOL_USE · VISION · ANALYSIS · SUMMARIZATION
```

Classification is deterministic whole-token matching over phrase/token vocabularies
(e.g. a phrase containing *"algorithm"* is CODING; *"summarize"* is SUMMARIZATION;
*"implications / why / explain"* leans REASONING/ANALYSIS). The same input always yields
the same `task_type` — no randomness, no model call, inspectable and testable.

The task carries the fields the router needs: `task_type`, `complexity`,
`estimated_context_size`, `latency_sensitive`, `requires_vision`, `requires_tools`
(when tools exist in the registry), and `explicit_model_request`.

## 4. Selection order (deterministic, inspectable)

`ModelRouter.route(task, context)` applies, in strict order:

1. **Explicit user/model instruction** — an `explicit_model_request` on the task wins
   over every other rule (this is the only rule that can override role/capability
   estimates; the spec's rule "explicit current instruction wins").
2. **Required capabilities** — hard filter. If the task needs `vision` (or `tools`
   when tools are available) and no model has it, the result is
   `ModelSelection(satisfied=False)` listing the missing capability — *never a
   silent downgrade*.
3. **Task-type affinity** — explicit `task_types` on a profile pin it first, then the
   preferred `role` for the classified task (coding → coding role; reasoning/analysis →
   reasoning; summarization/chat → fast; vision → vision). *Task affinity dominates raw
   configured `priority`* (implemented as explicit ranking tiers).
4. **Context-fit** — prefer models whose `context_window` fits `estimated_context_size`.
5. **Availability** — health-state tier, but routing never probes a server to learn it
   (see §6).
6. **Configured `priority`** — lower wins, as a tie-break *within* the task-affinity tier.
7. **Latency** — speed-class only when the task is marked `latency_sensitive`.
8. **Cost class** — `local < free < low < medium < high`.
9. **Default fallback** — the configured `default: true` model breaks remaining ties.

Every selection carries a human-readable `reason` list and a `decision` tag
(`routed`, `explicit`, `unsupported_capability:…`, `no_eligible_model`), and the router
logs each decision. Unpredictable routing is impossible by construction.

## 5. Capability-preserving fallback, honest failure

`ModelSelection.chat(messages, tools, temperature)` runs the chosen provider, then — only
on a **genuine failure** — walks the capability-compliant fallback chain (bounded by
`max_fallback_attempts`). A failure is: the provider raised, returned `None`, returned
`ok=False`, or produced an empty response (when `fallback_on_empty_response`).

A wrong-but-plausible answer is **not** a failure and is not overridden. If **every**
attempt fails, the method returns `RoutedResponse(ok=False, succeeded=False)` whose
content is the truthful last-failure content — a controlled error, **never a fabricated
success**. The orchestrator merges this into response metadata as
`succeeded: False`, so "Task succeeded" is never reported over a falling chain.

## 6. Health without network probing

Model health lives in `ModelHealth` (`AVAILABLE / UNKNOWN / DEGRADED / UNAVAILABLE`),
maintained **passively**: `chat()` records `record_failure`/`record_success`, and
`check_health()` is TTL-cached via the registry's lazy singleton provider factories.
Routing never issues a health probe on the hot path; `eager_health_check` (probing
general models at startup) is off by default. `UNAVAILABLE` models are skipped in the
chain only when the state was learned, not "assumed unknown".

## 7. The rest of VERONICA knows nothing about providers

- `Orchestrator` asks the router "which model for this task?" and holds whichever
  `ModelSelection` comes back; per hop it calls `selection.chat(...)`. It never contains
  routing policy, and it never inspects provider objects.
- `ModelSelection` / `RoutedResponse` expose routing observability as **plain strings and
  dicts** (`model_id`, `provider_name`, `reason`, `latency_s`, `attempts`). No
  provider type ever leaks above `llm/`.
- Providers are constructed in exactly one place — `build_provider_for_model` —
  reused by both the single-model path and the routing builder. There is **no second
  implementation** of any provider family.

## 8. Memory is routing *context*, not authorization

Thread Memory participates in routing only as **task context**:
a retrieved memory may inform the task profile, but it can **never** set
`explicit_model_request` — only the current user text can. This preserves the spec rule

> MEMORY RETRIEVAL ≠ MODEL SELECTION AUTHORIZATION

A past session that used a model is information, not instruction. Likewise, model
selection **does not grant tool permission**: routing cannot bypass `PermissionGate`;
the gate remains authoritative and is untouched.

## 9. Config reference

```yaml
# config/config.yaml — disabled by default; enable deliberately.
routing:
  enabled: true
  fallback_enabled: true
  fallback_on_empty_response: true
  max_fallback_attempts: 2
  health_ttl_s: 30
  log_routing_decisions: true
  models:
    - model_id: "llama3.1:8b"
      provider: "ollama"
      role: "fast"              # general | fast | reasoning | coding | vision
      default: true
    - model_id: "qwen2.5-coder:7b"
      provider: "ollama"
      role: "coding"
      task_types: ["coding", "reasoning"]
      context_window: 32768
      priority: 10
      reasoning_class: "heavy"
      # supports_tools / supports_vision / speed_class / cost_class / temperature /
      # timeout_s / base_url / api_key_env all optional
```

Providers supported: **ollama** (local, free, default), **openai_compatible** (needs an
`api_key_env`), **echo** (deterministic, for tests). Omit `models` to register the global
`llm.*` model as the single routed default.

## 10. Non-goals (V1)

- No dynamic/self-optimizing model selection.
- No streaming or vision *execution* — capability *selection* only.
- No A/B testing, multi-user state, or per-user model preferences.
- No hierarchical planner integration.
- Voice/VAD, mobile, and interaction with the routing layer are unchanged.

## 11. Tests

`tests/test_model_routing.py` (68 tests) covers: profile/capability sync, all 9 task
types + determinism, registry TTL-cached health, health transitions, ranking tiers,
explicit/unknown/disabled requests, hard capability gaps, context-fit, every fallback
failure mode (raise / `ok=False` / empty), truthful all-fail responses, provider
construction + `ok=False` on missing keys, thread-memory isolation (memory never sets
`explicit_model_request`; selection metadata carries no permission fields), and
orchestrator integration (routing failures fall back to the primary LLM; routing never
bypasses `PermissionGate`).