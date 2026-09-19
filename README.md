# VERONICA

**V**oice-**E**nabled **R**easoning **O**rchestrator for **N**atural **I**nteraction, **C**ontrol & **A**utomation.

A modular, JARVIS-inspired personal AI assistant. VERONICA is designed as a real piece of
software with clean layers — not a single monolithic script — so new capabilities (tools,
agents, vision, model providers) can be added later without rewriting the core.

See `docs/ARCHITECTURE.md` for the full design rationale and the repository study that led
to it, and `docs/GETTING_STARTED.md` to install and run it on your machine.

## Why it looks like this

VERONICA's design is a synthesis of several existing JARVIS/Ultron-style projects (see
`docs/ARCHITECTURE.md` for the full comparison table), taking the strongest idea from each
and rebuilding it on a cleaner foundation:

- **Fast intent routing before LLM fallback** (pattern used by Prodigy-AI's
  `dispatcher.py`/`intents.py`) — cheap, deterministic commands never wait on a model.
- **A real tool/plugin registry with declared permission levels** (inspired by, and
  stricter than, the ad-hoc command files in Prodigy-AI and J.A.R.V.I.S) — every capability
  is a self-describing `Tool` subclass the LLM can call safely.
- **Local-first model routing with an easy cloud fallback** (the core thesis of
  OpenJarvis) — VERONICA talks to a local Ollama model by default and never requires a
  paid API key; a cloud provider is a config flag away when you want one.
- **Layered short-term + persistent long-term memory** (Prodigy-AI's `semantic_memory.py`
  idea, rebuilt with a proper store interface instead of hand-rolled JSON parsing).
- **Async, non-blocking TTS and a swappable STT backend** (Prodigy-AI's `speaker.py`/
  `listener.py`), so voice never freezes the rest of the assistant.
- **A UI layer that is a thin client over the core**, not the core itself — the reference
  CLI here can be swapped for a GUI (à la Prodigy-AI's `customtkinter` GUI, or a 3D HUD like
  `ultron-by-sagar-builds`' orb) without touching orchestration logic.

## Quick start

```bash
cd veronica
python -m venv .venv
source .venv/bin/activate      # .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp config/.env.example config/.env     # fill in only what you actually want to use
python -m veronica.main
```

By default VERONICA runs in **text mode** with **no external API key required**. Voice
input/output and a local LLM (via [Ollama](https://ollama.com)) are opt-in — see
`docs/GETTING_STARTED.md`.

## Project layout

```
veronica/
├── veronica/
│   ├── main.py              # entry point / CLI loop + build_app() assembly
│   ├── config.py            # typed config loader (YAML + .env)
│   ├── logging_setup.py     # structured logging + diagnostics
│   ├── core/
│   │   ├── orchestrator.py  # the "brain": routes input -> tool/LLM -> output, + agentic path
│   │   └── context_manager/ # Context Window Management V1: per-request selection, budget, compression
│   ├── automation/
│   │   ├── planner.py       # goal -> ordered steps (multi-step planning)
│   │   ├── task_executor.py # plan -> execute -> verify, with retry + learning hook
│   │   ├── observation.py   # Phase 6: structured "expected vs observed" capture
│   │   ├── diagnosis.py     # Phase 6: failure categorization + recovery actions
│   │   ├── failure_memory.py# Phase 6: same-failure signature detection
│   │   ├── replanner.py     # Phase 6: preserves goal + regenerates remaining steps
│   │   ├── closed_loop.py   # Phase 6: ClosedLoopController (safety-bounded)
│   │   └── domino.py        # declarative sequential tool pipelines
│   ├── learning/
│   │   ├── experience.py    # Experience model + secret redaction
│   │   ├── store.py         # durable JSONL experience store
│   │   ├── learner.py       # LearningEngine: extract structured learning signals
│   │   ├── confidence.py    # evidence-based per-strategy confidence
│   │   ├── feedback.py      # explicit user-feedback classification
│   │   ├── retrieval.py     # relevance-scored experience retrieval (Phase 3)
│   │   ├── lesson.py        # lessons + advisory planner context (Phase 3)
│   │   ├── policy.py        # behavioral-adaptation policy gate (Phase 4)
│   │   └── adaptation.py    # BehavioralAdapter: draws a plan copy, never executes (Phase 4)
│   │   ├── context.py       # conversation session state
│   │   ├── capability.py    # semantic capability layer over the tool registry
│   │   ├── compound.py      # "close X and open Y and remind me about Z" splitter
│   │   └── loop_guard.py    # detects identical / ping-pong tool-call loops
│   ├── runtime/
│   │   ├── llm_health.py    # per-provider health monitor (skips sick providers)
│   │   └── circuit_breaker.py  # closed/open/half-open breaker for LLM + TTS
│   ├── memory/
│   │   ├── short_term.py    # rolling in-session conversation buffer
│   │   ├── long_term.py     # persistent fact store (JSON-backed, swappable)
│   │   ├── compress.py      # conversation compression for small-context models
│   │   └── thread/          # Phase 8 (Thread Memory V1): persistent episodic memory
│   │       ├── thread_memory.py  # ThreadMemory facade: the only surface the brain sees
│   │       ├── models.py         # MemoryRecord / Episode / MemoryType model layer
│   │       ├── store.py          # JSONL record catalog (defense-in-depth redaction)
│   │       ├── vector_store.py   # VectorStore interface + LocalVectorStore (pure-Python)
│   │       ├── embeddings.py     # EmbeddingProvider interface + local hashed provider
│   │       ├── retrieval.py      # deterministic weighted retrieval (semantic + metadata)
│   │       ├── consolidation.py  # rule-based consolidation (merge/link/degrade)
│   │       ├── serialization.py  # versioned JSON export / conflict-safe import
│   │       ├── privacy.py        # secret redaction (reuses learning/experience rules)
│   │       └── episodes.py    # Episodic Memory V1: episode build/record/retrieve + procedures
│   ├── speech/
│   │   ├── stt.py           # speech-to-text backends (Google free / Vosk / faster-whisper)
│   │   ├── tts.py           # text-to-speech backends (edge-tts / piper / pyttsx3)
│   │   └── wake_word.py     # lightweight wake-word / push-to-talk listener
│   ├── llm/
│   │   ├── base.py                  # LLMProvider interface + ChatMessage/ToolCall types
│   │   ├── ollama_provider.py       # local, free, default
│   │   ├── openai_compatible_provider.py  # optional cloud/OpenRouter, off by default
│   │   ├── router.py                # single-model provider construction from config
│   │   └── routing/                 # Multi-Model Routing V1 (opt-in): ModelRouter, registry,
│   │                                #   TaskAnalyzer, ModelProfile/ModelHealth, builder
│   ├── automation/
│   │   └── domino.py        # sequential tool pipelines ({previous}/{text} feed-forward)
│   ├── tools/
│   │   ├── base.py          # Tool base class, PermissionLevel, ToolResult
│   │   ├── registry.py      # plugin registry: discovery + safe dispatch
│   │   ├── system_tools.py  # open apps, volume, screenshot, processes
│   │   ├── file_tools.py    # find/list/read files
│   │   ├── web_tools.py     # web search, open a URL
│   │   ├── youtube_tools.py # play something on YouTube
│   │   ├── weather_tools.py # weather via a free, keyless API
│   │   └── utility_tools.py # calculator, time/date, notes
│   ├── vision/
│   │   ├── screen.py        # screenshot + OCR (extensible vision entry point)
│   │   ├── screen_state.py  # Phase 7: ScreenState / UIElement / WindowInfo + fingerprinting
│   │   ├── uia_adapter.py   # Phase 7: Windows UI Automation adapter (semantic perception)
│   │   ├── perception.py    # Phase 7: PerceptionLayer + UIA/Screen/OCR providers
│   │   ├── change_detection.py  # Phase 7: fingerprint-based change strategies
│   │   ├── computer_controller.py  # Phase 7: semantic-first computer control
│   │   ├── screen_content_defense.py  # Phase 7: screen text = untrusted data
│   │   └── computer_use.py  # Phase 7: OBSERVE→UNDERSTAND→SELECT→PERMISSION→EXECUTE→VERIFY
│   ├── safety/
│   │   └── permissions.py   # permission levels + confirmation gate
│   └── ui/
│       ├── cli.py           # text/voice CLI front-end
│       └── stitch/          # Stitch command-center GUI (HTML + HTTP/SSE bridge)
├── tests/                   # unit tests (712 pass + 1 skipped, no hardware/network needed)
├── config/
│   ├── config.yaml           # non-secret settings
│   └── .env.example          # secret/optional keys, all commented out
└── requirements.txt
```

## Status

The **foundation layer** runs end-to-end today: orchestrator, memory (with conversation
compression), the tool registry, the loop guard, compound-command splitting, LLM-health
monitoring with a circuit breaker, the semantic capability layer, Domino chain automation,
pluggable STT (Google free / Vosk / faster-whisper) and TTS (edge-tts / piper / pyttsx3),
permissions, logging, and **712 passing tests** — all passing today, including the full 18-step Phase 5 end-to-end validation loop.. On top of that, VERONICA is now an
**agent**: the planner decomposes goals into ordered steps and the task executor runs them
with per-step retry and self-verification (plan → execute → verify), with live progress in
the GUI. A **Learning & Adaptation v1** subsystem records what VERONICA does, persists
sanitized experiences, extracts learning signals, and tracks evidence-based confidence —
while staying strictly observation-only (it can never bypass permissions or change
behavior on its own). **Adaptive planning (Phase 3)** closes the loop: before planning a
multi-step goal, VERONICA retrieves relevant past experience as *advisory* context so the
planner learns from what worked — but it always generates a fresh plan and never replays
old ones, and failures are surfaced only as cautionary evidence. **Controlled behavioral adaptation
(Phase 4)** goes further: after planning, validated high-confidence lessons may
*transform* the execution plan (e.g. substitute a proven tool within the same group) —
but only above a 0.90 confidence floor with at least 3 repeated successes, never for
safety-sensitive operations, never overriding explicit instructions or concrete step args,
and always reversibly. The learning system still **never executes tools** — every action
stays gated behind TaskExecutor → ToolRegistry → permissions. **Phase 5: Learning Effectiveness & Strategy Evolution** adds a deterministic metrics subsystem and evidence-based strategy tracking — every metric is a pure function over the experience store (no second source of truth, no Redis, no external analytics), and strategy state transitions (OBSERVED → PROMISING → VALIDATED → DEGRADED) follow configurable evidence thresholds and recent-window decay. Adaptation effectiveness is measured as an **observed difference** between baseline and adapted outcomes; Phase 5 explicitly does not claim causation. The safety invariant (unsafe adaptations applied = 0) is continuously monitored; `learning.enabled: false` disables everything. Three new data classes (`metrics.py`, `strategy.py`, evidence thresholds) expose the dashboard-quality structured data for the next UI phase. The Stitch GUI benefits automatically through the shared execution pipeline. **Phase 6: Closed-Loop Intelligence** completes the agent stack with a bounded observe → diagnose → replan cycle: after a step fails, structured observations (deterministic failure categorization, expected-vs-observed, evidence sufficiency) feed a diagnosis engine that proposes recovery actions (retry, arg-change, tool-substitute, step-insert, human-intervention); same-failure detection (SHA-256 failure signatures) prevents repeating identical failures; and the replanner preserves the original goal plus completed steps while generating fresh plans for the remainder. It is **not** unrestricted autonomy: every recovery action still passes through the existing PermissionGate, `UNKNOWN` never becomes `SUCCESS`, identical failures block after configured thresholds, `SafetyConfig` bounds replans/retries/steps/seconds, and stored experience remains advisory data — never executable instructions. Runs in the existing agentic path (executor still used as fallback) with new SSE events (`task_recovery_started`, `task_diagnosing`, `task_replanned`, `task_blocked`, …) driving the GUI's recovery lifecycle. **Phase 7: Continuous Computer Perception & Control (core layer)** gives VERONICA reliable, local, permission-aware perception of — and controlled interaction with — the Windows desktop. A hybrid perception stack (Windows UI Automation → screen metadata → OCR) is normalized into a serializable `ScreenState` (windows, UI elements, text regions, focused element, cursor) with SHA-256 fingerprint + weighted change analysis for configurable change-detection strategies (idle/active/waiting/action/error). A `ComputerController` performs semantic-first actions (find button named X → click), escalating to coordinates only as a fallback, and a `ComputerUseLoop` runs the OBSERVE→UNDERSTAND→SELECT→PERMISSION→EXECUTE→OBSERVE→VERIFY cycle with cancellation and graceful dependency degradation. **Screen content is treated as UNTRUSTED DATA throughout**: `ScreenContentDefense` filters injection patterns (forged commands, permission-bypass phrasing, credential-shaped text) before screen text can influence decisions, and it can never override the permission system. Every computer action still passes through the existing `PermissionGate`, `UNKNOWN` never auto-becomes `SUCCESS`, and failures feed the **existing** Phase 6 `ClosedLoopController` — no second recovery system. New SSE events (`action_observing`, `action_executing`, `action_verifying`, `action_completed`, …) plus `requestScreenState` / `executeComputerAction` expose the loop in the Stitch GUI. Core layer only — continuous background observation, the full vision-model provider, and the full 40-test surface are deferred to later Phase 7 steps. **Phase 8 (Thread Memory V1)** adds a persistent, extensible episodic-memory layer behind the mandated abstraction `ThreadMemory` → `VectorStore` interface → `LocalVectorStore` (pure-Python, zero dependencies; ChromaDB/FAISS can be swapped in via factory without rewriting the brain). Records are typed (`CONVERSATION` / `FACT` / `PREFERENCE` / `EVENT` / `EXPERIENCE` / `PROCEDURE` / `PROJECT` / `IDEA` / `RELATIONSHIP`), carry importance/confidence/provenance/timestamps, and are stored in a sanitized JSONL catalog with deterministic hashed embeddings + relevance-weighted retrieval (semantic similarity, importance, confidence, recency, project/tag context — deterministic, no ML). Structured episodes project into EXPERIENCE + reusable PROCEDURE records; a rule-based consolidator merges exact duplicates, links project clusters, and drops empty records without ever rewriting a user fact. **Memory is part of the security boundary**: every record is redacted before persistence (reusing the project's canonical secret vocabulary), secret-looking content is masked, nothing raw is auto-persisted, and a retrieved memory is explicitly labeled *informational only* — it never grants permission, and `PermissionGate` remains the sole authorization path. The feature is opt-in (`memory_thread.enabled`); any failure degrades to keyword search, so Thread can never crash or block the assistant. See `docs/THREAD_MEMORY.md`. **Phase 9 (Multi-Model Routing V1)** turns VERONICA's LLM layer into a deterministic router: a `TaskAnalyzer` classifies each request into one of nine task types (no second LLM is required to choose the model), and a `ModelRouter` picks the best configured model — fast/reasoning/coding/vision/general — by explicit instruction > required capability > task type > context fit > availability > configured priority > latency > cost > default. Routing is opt-in (`routing.enabled: true`) and byte-identical to the single-model path when disabled; a capability-preserving fallback chain never fabricates success, knows nothing about the rest of the brain (the orchestrator asks "which model?", the router answers, no provider types leak upward), and no routing decision grants permission — `PermissionGate` stays authoritative. See `docs/MODEL_ROUTING.md` for the architecture and configuration. **Episodic Memory Integration V1** closes the loop between execution and memory: every terminal task outcome — the executor's authoritative `Experience` — is consumed by an `EpisodeRecorder` that derives a structured **Episode** (goal, actions, tools, failures/recoveries, verification) using a deterministic, conservative status vocabulary (`SUCCESS`/`FAILED`/`BLOCKED`/`CANCELLED`/`UNKNOWN`; executed+unverified is *never* promoted to success) and persists it into Thread (EXPERIENCE record). An `EpisodeRetriever` pulls prior episodes back as clearly-labeled `[RETRIEVED EXPERIENCE]` context for the next plan (reusing Thread retrieval — no second vector DB), and a `ProcedureStore` tracks repeated-work evidence with the same OBSERVED → PROMISING → VALIDATED → DEGRADED progression. None of this can alter behavior: an episode can never approve a tool, elevate a permission, or bypass `PermissionGate`; memory-write failure never fails the task; and API keys / tokens / passwords are redacted before persistence. See `docs/EPISODIC_MEMORY.md`. **Streaming LLM V1** delivers LLM replies progressively: a single `StreamController` coordinates the pure-LLM turn (one resolver picks the streaming provider before any text flows, so there is never a mid-stream model switch), a reader thread pulls chunks into a bounded queue with true backpressure (final text is the exact concatenation of every received delta — no chunk is ever dropped), and coalesced SSE events (`stream_started` / `response_delta` / `stream_status` / `stream_completed` / `stream_cancelled` / `stream_error`) drive the GUI. Lifecycle is exactly-one-terminal (`STREAM_COMPLETED` | `STREAM_CANCELLED` | `STREAM_FAILED`), with first-token / idle / overall timeouts (cancel wins over a timeout) and an explicit `truncated=True` resource cap at `max_response_chars` — never silent truncation. Streaming is opt-in (`streaming.enabled`), honours the user's explicit "stream it" ask or a `preferred` policy, falls back to the classic `chat()` path when nothing can stream (never a fake stream), records only the final accumulated text into conversation and episodic memory (partials are diagnostics-only), and a stream transport failure is remembered distinctly as `EpisodeStatus.STREAM_FAILURE` — never collapsed into task `FAILED`. See `docs/STREAMING_LLM.md`. **Context Window Management V1** adds the dedicated context-selection layer: for every request it answers "what should be supplied to the model THIS turn?" — a deterministic pipeline (normalize → estimate → score → sort → select → assemble) that budgets each source against the model's `context_window`, scores relevance without any second embedding system, drops / summarizes compressible content only when budgets demand, and assembles one request-scoped `ContextPackage` (there is **no** global current context). The current user request and the safety boundary are always included and **never** compressed; if even they cannot fit, the layer raises `ContextOverflowError` and the orchestrator falls back to the legacy assembly with the user's full text intact — never silent truncation. Memory recall and external/tool content are labeled and framed in the prompt itself (informational only, untrusted), tool schemas are budgeted but still handed to the model through its own `tools=` kwarg, and context selection never touches stored memory or grants a permission. Opt-in (`context.enabled`), disabled behavior is byte-identical, and any internal failure degrades to the legacy path. See `docs/CONTEXT_WINDOW.md`. Two front-ends share the same `Orchestrator.handle()`: the text/voice
CLI and the **Stitch command-center GUI** (a cinematic glassmorphism UI with a Three.js
particle core, served over plain HTTP + SSE — no PyQt). Voice and vision are wired up with
clean interfaces and sensible fallbacks, ready for you to flesh out on your own machine
where a microphone, speakers, and a local Ollama install are actually available (this
build/test environment has none of those, so voice paths are exercised with mocks in
`tests/`).

See `docs/SOURCE_COMPARISON.md` for the integration's source-level review and
`docs/THIRD_PARTY.md` for attribution and licenses.
#   V e r o n i c a  
 