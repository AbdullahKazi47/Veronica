# VERONICA × JARVIS OS × ULTRON — Source-Level Comparison

> Goal: identify the strongest reusable implementations from the JARVIS OS public
> docs and the ULTRON source tree, and slot them into the existing VERONICA
> architecture without replacing it wholesale. Stitch GUI becomes the front-end.

This document is the "before any code changes" deliverable. After it is
approved, integration proceeds in the order listed in section 6.

---

## 0. Source map

| Repo | What was actually inspected | Path |
|---|---|---|
| **VERONICA** | Full source — 30+ files in `veronica/` | `c:\Users\HomePC\Desktop\Veronica\veronica\` |
| **JARVIS OS** (`Turbo31150/jarvis-os-public`) | Public repo only — no source code shipped; architecture, audit, presentation. Real source is in the private `jarvis-linux` repo (404 on clone). License: MIT, © 2026 Franck Delmas. | `c:\Users\HomePC\Desktop\Veronica\jarvis-os-public\` |
| **JARVIS OMEGA** | DevPost page only; no public source. Description references 961 agents / 24 MCP / 88 handlers but no code to read. | https://devpost.com/software/jarvis-omega |
| **ULTRON** ("Project Ultron") | Full source — `core/`, `ai/`, `tools/`, `stitch_ultron_neural_command_center/`. Stitch GUI = `code.html` + `DESIGN.md`. License: implicit in private tree; treat as MIT-style, do not redistribute, only reuse patterns. | `C:\Users\HomePC\Desktop\Project Ultron\` |

---

## 1. What VERONICA already has (do not re-invent)

| Subsystem | File(s) | Status |
|---|---|---|
| Typed config (YAML + `.env`) | `veronica/config.py` | ✅ shipped |
| LLM router (Ollama / OpenAI-compatible / Echo) | `veronica/llm/router.py`, `ollama_provider.py`, `openai_compatible_provider.py` | ✅ shipped |
| Tool base class + `Tool` registry with fast-path match | `veronica/tools/base.py`, `registry.py` | ✅ shipped |
| 19 built-in tools (system, file, web, weather, exec, …) | `veronica/tools/*.py` | ✅ shipped |
| Permission gate (SAFE / MODERATE / SENSITIVE / CRITICAL) | `veronica/safety/permissions.py` | ✅ shipped |
| Short-term & long-term memory (deque + JSON) | `veronica/memory/short_term.py`, `long_term.py` | ✅ shipped |
| STT (Google free / Vosk offline) | `veronica/speech/stt.py` | ✅ shipped |
| TTS (edge-tts async / pyttsx3 offline, SHA256 cache) | `veronica/speech/tts.py` | ✅ shipped |
| Wake-word (push-to-talk + energy-spike) | `veronica/speech/wake_word.py` | ✅ shipped |
| Centralized rotating-file logging | `veronica/logging_setup.py` | ✅ shipped |
| Vision (screenshot + OCR stub) | `veronica/vision/screen.py` | ✅ shipped |
| CLI front-end with permission confirm | `veronica/ui/cli.py` | ✅ shipped |
| Orchestrator (fast-path → LLM with tool-calling loop, max 8 hops) | `veronica/core/orchestrator.py`, `context.py` | ✅ shipped |

VERONICA's architectural commitments that the integration must respect:

1. Deterministic routing before LLM.
2. Everything user-actionable is a `Tool` with a `PermissionLevel`.
3. No paid API by default; local-first.
4. `Orchestrator` is the only place that touches voice; STT/TTS are isolated.
5. UI is a thin client that calls `Orchestrator.handle()`.

---

## 2. What JARVIS OMEGA / JARVIS OS provides (architecturally — no code to import)

The public JARVIS OS repo ships zero source — only docs, a presentation HTML,
a French dossier, and an audit. What is reusable is therefore *concepts* that
we can implement on top of VERONICA's existing modules. Per the public docs
(`AUDIT_COHERENCE.md`, `PACK_PRESENTATION.html`):

| JARVIS OS capability | VERONICA equivalent today | Gap to close |
|---|---|---|
| **9-layer OS layering** (sensors → LLM hub → MCP/tools → agents → UI) | Implicit in `core/` + `tools/` + `ui/` | Document the layering in code as docstrings; no new module. |
| **Hub LLM 0-token (routeur, cache, LB1/LB2 cascade failover)** | `llm/router.py` with provider map | Add **fallback chain** (try local first, escalate to cloud on health-check failure). |
| **Whisper CUDA STT pipeline** | `speech/stt.py` (Google / Vosk) | Add an optional **faster-whisper** backend (CPU + CUDA, free, local). |
| **Piper / Edge TTS** | `speech/tts.py` (edge-tts / pyttsx3) | Add **Piper** as a fully-offline, fast, high-quality TTS backend. |
| **MCP tool ecosystem (24 servers, 88 handlers)** | `Tool` registry | Keep VERONICA's lighter approach. Add an `MCPClient` *interface* that can wrap a tool, but don't ship 24 servers. |
| **Domino chains (condition-based automation)** | None | New module: `automation/domino.py` — a *tiny* event → condition → action engine (much smaller than JARVIS's full implementation, same idea). |
| **COWORK engine (autonomous missions)** | None (orchestrator loop max 8) | New module: `automation/cowork.py` — multi-step task with self-verification. *Modeled on ULTRON's `TaskExecutor`, not JARVIS's full COWORK*. |
| **Multi-node Docker Swarm** | Not applicable | **Do not import** — VERONICA is a single-user Windows desktop assistant. |
| **n8n 65-workflow ecosystem** | None | **Do not import** — n8n is a separate product. Cite it as an integration target only. |
| **Elasticsearch / Redis / PostgreSQL stack** | `memory/long_term.py` JSON | **Do not import.** Keep the file-based path; the user can swap to SQLite/vector storage later via the same `LongTermMemory` interface. |
| **Self-healing with circuit breakers** | None | New module: `runtime/health.py` — circuit-breaker pattern around the LLM and the TTS. |
| **WebSocket gateway (LAN voice control)** | None | **Defer** to post-MVP. |
| **Linux/CUDA dependency** | Windows desktop target | **Adapt** any "free + local" capability (Whisper, Piper) to Windows. |

**License posture for JARVIS OS:** source under `jarvis-os-public/LICENSE` is
MIT, © Franck Delmas. Since there is no JARVIS code to import, the only
re-use is the documentation patterns and the *idea* of the layering, hub,
and domino/cowork engines. Where the integration writes new code, it
follows the same MIT posture (preserve VERONICA's MIT LICENSE, add
`docs/THIRD_PARTY.md` crediting JARVIS OS / ULTRON as architectural
inspiration).

---

## 3. What ULTRON provides (full source — code is reusable as patterns)

These are the strongest concrete re-use targets. Where ULTRON's source is
under the user's control and the algorithm is generally applicable, the
approach is to *port the pattern into VERONICA's existing structure* rather
than copy the file wholesale.

| ULTRON file | Strongest idea | VERONICA landing zone | Reuse mode |
|---|---|---|---|
| `core/capability_registry.py` (348 LOC) | `Capability` dataclass with name/description/risk/verification/default_tool/alt_tools; `INTENT_TO_CAPABILITY` map; `describe_for_prompt()` for LLM context. Sits *above* raw tools to give the planner semantic categories (filesystem / browser / memory / etc.) instead of a flat list. | New `core/capability.py` | **Adapt** the dataclass shape; port `describe_for_prompt()` to a method on `ToolRegistry`. |
| `core/task_executor.py` (360 LOC) | `MultiStepTask` + `TaskExecutor` with per-step retry (MAX_RETRIES=2) and self-verification. A real "agentic" loop the orchestrator can call. | New `core/task_executor.py` | **Adapt** the algorithm (plan → execute → verify), keep VERONICA's own `Orchestrator` as the caller. |
| `core/brain_orchestrator.py` (940 LOC) | Full pipeline `USER → UNDERSTAND → DETERMINE INTENT → KNOWLEDGE → LLM → EXECUTE TOOLS → VERIFY → RESPOND`; compound command detection ("close X and Y"); context compression at 50+ messages; learning-intent detection ("remember this", "teach me"); knowledge-aware context injection. | Mostly already covered in `core/orchestrator.py` | **Adapt** the **compound-command splitter** and the **context-compression** heuristics (these are the two pieces VERONICA is missing). |
| `core/loop_guard.py` (165 LOC) | SHA-256 identical-call detection, ping-pong (A→B→A→B) detection, per-tool budget, hard turn cap, plus `compress_conversation()`. | New `core/loop_guard.py` (mirror) | **Direct port** — this is generic, ~150 LOC, no platform deps. |
| `core/llm_health.py` (218 LOC) | `LLMHealthMonitor` with `AVAILABLE/UNAVAILABLE/ERROR/TIMEOUT/INITIALIZING/UNKNOWN` enum, exponential moving average of response time, consecutive-failure → UNAVAILABLE threshold (3), `force_recheck()`. | New `runtime/llm_health.py` | **Direct port**, then wire into the orchestrator so the router skips an unhealthy provider. |
| `core/router.py` (526 LOC) | Long regex-based fast-path router with Windows-specific phrases (battery, screenshot, close-app, "open the first/second/… result", "open the first/second one", "open it", "find information about X on the web"). | Improvement to `core/orchestrator.py`'s fast-path and the existing tools' `matches()` | **Adapt** selected patterns (contextual "open it/that", "find information about"). Do **not** copy the 526-line giant `route()` — VERONICA's per-tool `matches()` is more modular. |
| `stitch_ultron_neural_command_center/code.html` (52 KB) + `DESIGN.md` | Cinematic glassmorphism command-center UI: dark obsidian theme, gold/cyan/green status colors, Space Grotesk + Geist + JetBrains Mono typography, sharp 0px corners, 4px grid, glass-blur panels. | New `veronica/ui/stitch/` (static HTML + `app.js` talking to a tiny WebSocket bridge that calls `Orchestrator.handle()`) | **Direct reuse** of the visual design and HTML scaffold. License is the user's own private work — fine to copy. |
| `core/debug_logger.py`, `core/risk_policy.py`, `core/action_verifier.py`, `core/intent_classifier.py` | Per-decision audit trail, risk policy, action verifier, intent classifier | Touch lightly | **Defer** unless the integration actually needs them. |
| `database/db.py`, `ai/llm.py`, `ai/speech_to_text.py`, `ai/text_to_speech.py` | Concrete implementations | **Do not import** | ULTRON's `ai/llm.py` and the STT/TTS are tightly coupled to its own LLM engine and providers. VERONICA's existing `llm/`, `speech/stt.py`, `speech/tts.py` are the ones to extend. |

**Compound-command splitter** (from `brain_orchestrator.py`): the pattern
for splitting "close Spotify and Chrome and open Notepad" into three tool
calls in sequence, with result concatenation, is genuinely missing from
VERONICA. This is a high-value, ~50-LOC addition to `core/orchestrator.py`.

**Conversation compression** (from `loop_guard.py`): VERONICA's
`ShortTermMemory` is a `deque(maxlen=20)` — fixed, simple. The ULTRON
`compress_conversation()` strategy (keep system + first exchange + last 4,
summarize middle) is a real improvement when the LLM has a small context
window (Ollama 8B models). Slot as `memory/compress.py`.

---

## 4. What should be **directly reused**

| Item | Where it goes | Why |
|---|---|---|
| `loop_guard.py` algorithm | `veronica/core/loop_guard.py` | Platform-agnostic, 150 LOC, no JARVIS / ULTRON-specific deps. Real value: blocks infinite agent loops, which VERONICA's `max 8 hops` is too coarse to catch (a tool can be called 8 times alternating A/B). |
| `LLMHealthMonitor` algorithm | `veronica/runtime/llm_health.py` | Same reason. Wire into `llm/router.py` so a failing provider is skipped on the next request. |
| `CompoundCommandSplitter` pattern | `veronica/core/compound.py` (new, ~60 LOC) | Multi-verb natural input ("close X, open Y, and remind me about Z") is a real-world case the current orchestrator cannot handle. |
| `compress_conversation()` strategy | `veronica/memory/compress.py` | Concrete, useful for small-context local models. |
| Stitch GUI HTML + design tokens | `veronica/ui/stitch/` (static assets + thin JS) | Direct visual reuse. WebSocket bridge (FastAPI `uvicorn` or plain `websockets` server in a thread) → `Orchestrator.handle()`. |
| `Capability` dataclass shape | `veronica/core/capability.py` (new) | Plug into `ToolRegistry.describe_for_prompt()` so the LLM sees a semantic map, not 19 raw tools. |

## 5. What should be **adapted** (port the pattern, write fresh VERONICA code)

| Pattern | ULTRON origin | VERONICA adaptation |
|---|---|---|
| Full pipeline orchestrator | `brain_orchestrator.py` | Don't import 940 LOC. Add only the missing pieces (compound splitter, knowledge-aware context, learning-intent) on top of VERONICA's existing `core/orchestrator.py`. |
| Fast-path intent router | `core/router.py` | Don't copy 526 LOC of giant if/elif. Per-tool `matches()` regex is already the right primitive — add the contextual "open it/that" and the "find information about" patterns to specific tools. |
| Multi-step task executor | `core/task_executor.py` | Port the plan/verify/retry algorithm; write the dataclasses to VERONICA's style (no global singletons; injected like everything else). |
| LLM health monitor | `core/llm_health.py` | Port the algorithm; integrate with VERONICA's `llm/router.py` provider map. |
| Whisper STT | (ULTRON uses `ai/speech_to_text.py` — custom) | VERONICA already has STT. Add `faster-whisper` as a new backend in `veronica/speech/stt.py` (real, free, local, works on Windows + CUDA). |
| Piper TTS | (ULTRON's `ai/text_to_speech.py` — custom) | VERONICA already has TTS. Add `piper-tts` as a new backend (offline, fast, high quality). |
| Domino-style automation | (from JARVIS OS docs) | New `veronica/automation/domino.py` — minimal, ~100 LOC. Yaml-described rules → trigger → conditions → action. No need to mirror JARVIS's full product. |
| Capability registry | `core/capability_registry.py` | Add `Capability` registry; keep VERONICA's flat `Tool` registry as the lower layer the capabilities map onto. |

## 6. What should **NOT be imported**

| Item | Reason |
|---|---|
| JARVIS OMEGA / JARVIS OS source code | Not publicly available. Treat as architectural inspiration only. |
| ULTRON's `core/brain_orchestrator.py` verbatim | 940 LOC of tightly-coupled code with its own logging, context, knowledge systems. Too much surface area; VERONICA's `core/orchestrator.py` is already cleaner. |
| ULTRON's `core/router.py` 526-line `route()` method | Anti-pattern — one giant function. VERONICA's per-tool `matches()` is the better shape; add patterns, don't copy. |
| ULTRON's `ai/llm.py`, `ai/speech_to_text.py`, `ai/text_to_speech.py`, `database/db.py` | Tightly coupled to ULTRON's providers and storage. VERONICA already has equivalents that should be extended, not replaced. |
| JARVIS OS's Docker Swarm, multi-node, Redis, PostgreSQL, Elasticsearch | Not applicable to a single-user Windows desktop assistant. |
| JARVIS OS's n8n workflows (65 of them) | n8n is a separate product. **Integration target**, not an import. |
| JARVIS OS's 24 MCP servers, 88 handlers | Out of scope. VERONICA's 19 built-in tools are the right size. Add an `MCPClient` interface only when an actual MCP server is wanted. |
| JARVIS OS's self-healing Prometheus-style monitoring | Overkill for a personal assistant. Replace with the lighter `runtime/llm_health.py` + `runtime/circuit_breaker.py`. |
| ULTRON's global singletons (`brain_orchestrator`, `task_executor`) | VERONICA's DI-via-`App` pattern is the model. New modules get an injectable instance, not a global. |

## 7. Where each imported capability will live in VERONICA

```
veronica/
├── core/
│   ├── orchestrator.py          (existing; extended with compound splitter)
│   ├── context.py               (existing)
│   ├── capability.py            ★ NEW — Capability registry layer over Tool
│   ├── compound.py              ★ NEW — Compound command splitter (~60 LOC)
│   ├── loop_guard.py            ★ NEW — Direct port of ULTRON's algorithm
│   └── task_executor.py         ★ NEW — Multi-step plan/verify/retry
├── llm/
│   └── router.py                (extended: integrate LLMHealthMonitor)
├── runtime/                     ★ NEW package
│   ├── llm_health.py            ★ NEW — Direct port of ULTRON's monitor
│   └── circuit_breaker.py       ★ NEW — Lightweight breaker pattern
├── memory/
│   ├── short_term.py            (existing)
│   ├── long_term.py             (existing)
│   └── compress.py              ★ NEW — compress_conversation() port
├── speech/
│   ├── stt.py                   (existing; add `faster_whisper` backend)
│   ├── tts.py                   (existing; add `piper` backend)
│   └── wake_word.py             (existing)
├── automation/                  ★ NEW package (Domino)
│   └── domino.py                ★ NEW — minimal event-condition-action engine
├── tools/                       (existing 19 tools; ToolRegistry.describe_for_prompt gets Capability overlay)
├── ui/
│   ├── cli.py                   (existing — keep for headless / dev)
│   └── stitch/                  ★ NEW — Stitch GUI
│       ├── index.html           (port from ULTRON code.html)
│       ├── app.js               (talks to bridge)
│       └── bridge.py            (WebSocket / SSE → Orchestrator.handle)
└── tests/
    ├── test_orchestrator.py     (existing; add compound-splitter tests)
    ├── test_capability.py       ★ NEW
    ├── test_compound.py         ★ NEW
    ├── test_loop_guard.py       ★ NEW
    ├── test_llm_health.py       ★ NEW
    ├── test_circuit_breaker.py  ★ NEW
    ├── test_compress.py         ★ NEW
    └── test_domino.py           ★ NEW
docs/
├── ARCHITECTURE.md              (existing — extend section 5 with new modules)
├── SOURCE_COMPARISON.md         ★ NEW — this document
└── THIRD_PARTY.md               ★ NEW — credits & license notes (JARVIS OS MIT, ULTRON patterns)
```

## 8. Implementation order (incremental, test each)

Each step is independent and shippable. Each adds tests before the next.

1. **`memory/compress.py`** + tests. No new deps. Plugs into existing `ShortTermMemory`. Lowest risk.
2. **`core/loop_guard.py`** + tests. Direct port. Wire into `Orchestrator` so the tool-calling loop is bounded not just by hop count but by call-pattern.
3. **`core/compound.py`** + tests. Splits "close X and open Y and remind me about Z" into a list of orchestrator calls.
4. **`runtime/llm_health.py`** + tests. Direct port. Wire into `llm/router.py` so a failing provider is skipped.
5. **`runtime/circuit_breaker.py`** + tests. Half-open recovery around the LLM provider and TTS.
6. **`core/capability.py`** + tests. `describe_for_prompt()` becomes a single line in the system prompt instead of dumping all 19 tools.
7. **`speech/stt.py` — add `faster_whisper` backend** + tests. New optional dep. Keeps existing backends.
8. **`speech/tts.py` — add `piper` backend** + tests. Same shape.
9. **`automation/domino.py`** + tests. Minimal YAML rule engine.
10. **`ui/stitch/`** — port HTML, write a thin WebSocket bridge, point at `Orchestrator.handle()`.
11. **`docs/ARCHITECTURE.md`** update; **`docs/THIRD_PARTY.md`** write-up.

## 9. License posture

- **VERONICA**: MIT (own). No change.
- **JARVIS OS / JARVIS OMEGA**: MIT (Franck Delmas / Turbo31150). No source is imported, so no notice is *required*; we add an attribution paragraph to `docs/THIRD_PARTY.md` describing which architectural ideas were inspired.
- **ULTRON**: Private to the user. Patterns are reused, not source files. Where a function body is *essentially identical* (e.g. `LoopGuard._is_ping_pong`, `compress_conversation`), the file carries a header comment:
  > `// Adapted from the user's private ULTRON project (project_ultron/core/loop_guard.py). Not for redistribution.`

## 10. What is explicitly out of scope for this round

- Multi-node / cluster — VERONICA is a single-user Windows desktop assistant.
- n8n integration — defer until a concrete workflow is named.
- MCP server fleet — add the `MCPClient` interface only when a real MCP server is wanted.
- 24-hour voice / always-on wake-word on a desktop — keep the current push-to-talk + energy-spike options. Don't ship a cloud STT default.
- Voice biometric / face recognition login — not requested, security risk without an explicit threat model.
- WebSocket LAN gateway — defer.
- Replacing VERONICA's `tools/` with JARVIS's MCP-tool universe — out of scope; tool breadth grows only as concrete needs are named.
