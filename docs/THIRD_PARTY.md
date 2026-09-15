# THIRD-PARTY COMPONENTS & ATTRIBUTION

VERONICA is an independent project, but it reuses the *ideas*, *patterns*, and in a few
places the *visual design* of several earlier assistant projects. This file records
exactly what came from where, and under what license. It is the authoritative list —
if a module is adapted from someone else's work, the attribution lives in that module's
docstring *and* is summarized here.

## License posture, in one paragraph

VERONICA itself uses the MIT license. No third-party **source file** is copied
verbatim into `veronica/` — the strongest re-use is (a) new code written to a
third-party's *algorithm* or *design pattern* and (b) the Stitch GUI, which is a
re-styled port of the user's own private ULTRON front-end. Only the two orthogonal
Python packages and the CDN-loaded browser libraries below are actual external
dependencies, and all are permissive.

## 1. External libraries / runtime dependencies

These are real packages or CDN assets VERONICA loads. Each is permissively licensed
(or, for the optional `pygame` fallback, LGPL — referenced, never distributed);
none is vendored into the repo, and all are imported lazily or purely optional.

| Dependency | What it provides | License | Where used |
|---|---|---|---|
| **edge-tts** | Natural neural TTS (default voice backend) | MIT | `veronica/speech/tts.py` |
| **pyttsx3** | Fully-offline TTS fallback | MPL-2.0 | `veronica/speech/tts.py` |
| **pygame** *(optional)* | Cross-platform audio playback — only used when the native Windows players (stdlib `winsound` / winmm MCI via `ctypes`) are unavailable | LGPL-2.1 | `veronica/speech/tts.py`, `_play_pygame()` fallback |
| **SpeechRecognition** | Speech-to-text front-end (default Google free backend) | BSD-3-Clause | `veronica/speech/stt.py` |
| **PyAudio** *(optional)* | Microphone capture for the default `google_free` STT backend only — no prebuilt wheel for every Python version, so never a hard requirement; the offline STT backends capture audio via `sounddevice` instead | MIT | `veronica/speech/stt.py` |
| **sounddevice** *(optional)* | Microphone capture for the offline `faster_whisper`/`vosk` STT backends and the energy-spike wake word | MIT | `veronica/speech/stt.py`, `veronica/speech/wake_word.py` |
| **vosk** *(optional)* | Offline STT model runtime | Apache-2.0 | `veronica/speech/stt.py` |
| **faster-whisper** *(optional)* | Offline Whisper STT via CTranslate2 (CPU/CUDA) | MIT | `veronica/speech/stt.py` — the `faster_whisper` backend |
| **piper-tts** *(optional)* | Offline neural TTS (Piper voice) | MIT | `veronica/speech/tts.py` — the `piper` backend |
| **psutil** | System stats for the Stitch HUD | BSD-3-Clause | `veronica/ui/stitch/bridge.py` |
| **Open-Meteo** weather API | Weather data, keyless | CC BY 4.0 (the provider); client is plain `requests` | `veronica/tools/weather_tools.py` |
| **Ollama** | Local LLM server | MIT | `veronica/llm/ollama_provider.py` |
| **Three.js** *(CDN)* | 3D particle "consciousness core" | MIT | `veronica/ui/stitch/index.html` |
| **Tailwind CSS** *(CDN)* | Utility-first CSS | MIT | `veronica/ui/stitch/index.html` |
| **Material Symbols / Google Fonts** *(CDN)* | Icons + type (Space Grotesk, Geist, JetBrains Mono) | Apache-2.0 / OFL | `veronica/ui/stitch/index.html` |

> Optional deps are never required. `faster-whisper`, `piper-tts`, `vosk`, and their
> friends are documented in `docs/GETTING_STARTED.md`; the backends fall back cleanly
> when the package isn't installed.

## 2. JARVIS OS — `Turbo31150/jarvis-os-public`

**License:** MIT — Copyright (c) 2026 Franck Delmas (Turbo31150). Included in the repo
at `jarvis-os-public/LICENSE` for reference.

**What was used:** no source code — the public repo ships zero source files (only docs,
a presentation, and an audit). What transfers is *architecture and concepts*, which are
ideas, not copyrightable expression. VERONICA's integration implements the concepts
itself on top of its own modules.

| JARVIS OS concept | VERONICA implementation |
|---|---|
| Layered OS-style architecture (sensors → LLM hub → tools → agents → UI) | Documented as the existing `core/` + `tools/` + `llm/` + `ui/` layering |
| LLM hub with 0-token routing + cascade failover | `veronica/llm/router.py` (provider map; health-skipping via `runtime/llm_health.py`) |
| Local Whisper CUDA / Piper TTS pipelines | `faster_whisper` and `piper` backends in `speech/stt.py` / `speech/tts.py` |
| **Domino chains** (sequential tool pipelines) | `veronica/automation/domino.py` — adapted concept, original implementation |
| Self-healing with circuit breakers | `veronica/runtime/circuit_breaker.py` |

Attribution header carried in `veronica/automation/domino.py`: *"Adapted from JARVIS
OS's 'Domino chains' concept"*.

## 3. JARVIS OMEGA — DevPost project (no public source)

**License:** unknown / n/a — no code available.

**What was used:** inspiration only, from the DevPost description (961-agent / 24-MCP /
88-handler architecture). No code, no algorithm, no design. Nothing in `veronica/`
derives from it that isn't also independently present elsewhere.

## 4. ULTRON — the user's private "Project Ultron"

**License:** private to the user; do **not** redistribute any of it. Reuse inside
VERONICA is the user adapting their own prior work into a cleaner codebase.

**Reuse mode:** *patterns and visual design*, not source files. Where a function body is
essentially identical to the ULTRON original (loop-guard detection, conversation
compression), the VERONICA module carries a header comment:
> `Adapted from the user's private ULTRON project (project_ultron/core/loop_guard.py). Not for redistribution.`

| ULTRON component | VERONICA adaptation |
|---|---|
| `core/loop_guard.py` (SHA-256 identical-call + ping-pong detection) | `veronica/core/loop_guard.py` |
| `loop_guard.compress_conversation()` | `veronica/memory/compress.py` |
| `brain_orchestrator.py` compound-command splitting | `veronica/core/compound.py` |
| `core/llm_health.py` (health monitor, EMA, failure threshold) | `veronica/runtime/llm_health.py` |
| `core/capability_registry.py` (semantic capability layer over tools) | `veronica/core/capability.py` |
| **Stitch GUI** — `stitch_ultron_neural_command_center/code.html` + `DESIGN.md` | `veronica/ui/stitch/` (`index.html` + `bridge.py`) |

### 4.1 The Stitch GUI port — the one *visual* re-use

`veronica/ui/stitch/index.html` is a port of the user's own Stitch command-center UI:
the cinematic glassmorphism layout, dark-obsidian design tokens, the gold/cyan/green
status system, the Space Grotesk / Geist / JetBrains Mono typography, the sharp 0px
corners, the orbital rings, and the Three.js particle "consciousness core" all originate
there. Because the copyright holder is the user, reuse is unrestricted within VERONICA.

The **transport** is the one part that changed. The original console talked to Python
through a PyQt `QWebChannel`. VERONICA replaces that with a dependency-free HTTP + SSE
pair in `veronica/ui/stitch/bridge.py`:

- **POST `/api`** — the page sends commands to `Orchestrator.handle()`.
- **GET `/events`** — Server-Sent Events stream `{signal, args}` frames back to the page.

The signal vocabulary is preserved from the original Stitch page (`show_ultron_message`,
`set_core_state`, `show_tool_activity`, `set_voice_state`, …) so the GUI's JS dispatcher
needed no semantic rewrite — only its channel.

## 5. The original VERONICA provenance (pre-existing in this repo)

These are the JARVIS-style projects the *original* VERONICA foundation was synthesized
from, per `docs/ARCHITECTURE.md`. No source files are copied; each contributed a pattern
that VERONICA reimplemented in its own typed modules.

| Project | License | Pattern taken | VERONICA landing zone |
|---|---|---|---|
| **Prodigy-AI** (NishantDas0079) | MIT | Fast intent routing *before* LLM (`intents.py`/`dispatcher.py`); async non-blocking TTS | `ToolRegistry.matches()` fast path in `core/orchestrator.py`; `speech/tts.py` |
| **OpenJarvis** | MIT | Local-first-by-default LLM routing with cloud escape hatch | `veronica/llm/router.py` |
| **hectorg2211/jarvis** | MIT | Spike-detection wake trigger; cached / pre-rendered audio | `speech/wake_word.py`; TTS audio caching |
| **J.A.R.V.I.S** (GauravSingh9356) | MIT | Feature checklist only (weather, notes, YouTube) | Various existing tools |
| **ultron-by-sagar-builds** | MIT | Holographic 3D-orb HUD as a *separate* UI layer (conceptual validation) | Confirms the `ui/` thin-client rule |

## 6. Reproducing the license notices

- Outbound: nothing in `veronica/` embeds third-party code that requires carrying its
  MIT notice; the attribution lives in this file and in the per-module docstrings listed
  above.
- The original JARVIS OS MIT notice `Copyright (c) 2026 Franck Delmas (Turbo31150)` is
  preserved verbatim in this repo at `jarvis-os-public/LICENSE`.
- The `faster-whisper`, `piper-tts`, Three.js, Tailwind, and font package licenses are
  their own projects' responsibility; VERONICA references them, never distributes copies.