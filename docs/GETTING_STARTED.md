# Getting Started with VERONICA

## 1. Requirements

- Python 3.10+ (tested on 3.12)
- Windows, macOS, or Linux (the OS-specific tools degrade gracefully where a platform
  API isn't available — see `docs/ARCHITECTURE.md`)
- A microphone/speakers only if you want voice mode
- **No paid API key is required** for anything in the default configuration.

## 2. Install

```bash
cd veronica
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp config/.env.example config/.env
```

You don't need to edit `config/.env` at all to get started — it's only for optional paid
providers.

## 3. Run in text mode (works immediately, no setup)

```bash
python -m veronica.main
```

By default `llm.provider` is `ollama`, which needs a local model — see step 4. If you
just want to try the tool system first without any model at all, temporarily set
`llm.provider: echo` in `config/config.yaml`; every fast-path command (weather, time,
calculator, open app, etc.) still works, and anything that would otherwise go to the LLM
just echoes back so you can see the plumbing.

## 4. Set up the free local LLM (Ollama)

1. Install [Ollama](https://ollama.com/download) for your OS.
2. Pull a model (this one is small enough for most laptops):
   ```bash
   ollama pull llama3.1:8b
   ```
   If your machine is limited on RAM, try a smaller model like `ollama pull llama3.2:3b`
   and change `llm.model` in `config/config.yaml` to match.
3. Make sure the Ollama server is running (`ollama serve`, or it's already running as a
   background service after install).
4. Run VERONICA — `config.yaml`'s `llm.provider: ollama` is already the default.

## 5. Voice mode

```bash
python -m veronica.main --voice
```

- **Speech-to-text** defaults to the free Google Web Speech backend via
  `SpeechRecognition` (needs internet, no API key). Its microphone needs **PyAudio**,
  which is *optional* — VERONICA's base install intentionally ships no mic package
  (PyAudio has no prebuilt wheel for every Python version, so requiring it would break
  `pip install -r requirements.txt` on some Pythons). For voice mode run
  `pip install PyAudio` (grab a prebuilt wheel for your Python if pip tries to compile
  it), or skip PyAudio entirely and use an offline backend below, which capture audio
  via `sounddevice`. If PyAudio is missing, voice input is simply unavailable until you
  add it — everything else keeps working. Offline options:
  - **Vosk**: download a [Vosk model](https://alphacephei.com/vosk/models) (the "small"
    English model is a good start), point `stt.vosk_model_path` at it, set
    `stt.backend: vosk` in `config.yaml`, and `pip install vosk sounddevice`.
  - **faster-whisper** (best offline quality, CPU or CUDA): `pip install
    faster-whisper`, then set `stt.backend: faster_whisper`. Models are downloaded
    automatically on first use (`stt.faster_whisper_model`: `tiny` → `base` → `small`
    → `medium` → `large-v3`); CTranslate2 falls back to CPU unless CUDA is available.
- **Text-to-speech** defaults to `edge-tts` (free, natural, needs internet, no key).
  Offline options:
  - `tts.backend: pyttsx3` — fully offline, robotic.
  - `tts.backend: piper` — fully offline neural voices (a big step up from pyttsx3):
    `pip install piper-tts`, then set `tts.piper_model` to a voice you've downloaded
    (e.g. `en_US-lessac-medium`). Uses the `piper` CLI if it's on your PATH, else the
    Python package, and caches rendered audio so repeats are instant.
- **Audio playback needs no extra package on Windows.** Every backend synthesizes a
  file (edge-tts → mp3, piper → wav) and plays it through the standard library:
  `winsound` for WAV and the winmm MCI driver (via `ctypes`) for MP3. pygame is no
  longer required for anything and is only an *optional* player for non-Windows
  platforms (`pip install pygame` if you're on macOS/Linux and want sound there).

## 5.5 Stitch GUI (the command-center front-end)

VERONICA ships a second front-end — the **Stitch GUI**, a cinematic glassmorphism command
center (Three.js particle core, live HUD, voice toggle) served over plain HTTP + SSE.
No PyQt, no paid key.

```bash
python - <<'PY'
from veronica.config import load_config
from veronica.main import build_app
from veronica.ui.stitch.bridge import run_bridge

config = load_config()
app = build_app(config, interactive=False)   # no terminal confirm prompts in the GUI
run_bridge(app.orchestrator, config, host="127.0.0.1", port=7171, open_browser=True)
PY
```

Then open <http://127.0.0.1:7171>. The console accepts text commands (same engine as the
CLI), the mic button runs STT → orchestrator → TTS, and the HUD shows live CPU/RAM/network.
If `psutil` isn't installed the HUD shows `--` for CPU/RAM rather than failing. See
`docs/ARCHITECTURE.md` § 5.1 for how the bridge works.

**Multi-step agentic tasks.** The **Plan & Run** button (the 🌳 tree icon, next to Send)
submits the input as a *planned task* instead of a single command: VERONICA decomposes the
goal into ordered steps and runs them with per-step retry and self-verification. The glass
progress card in the chat panel shows the goal, a step bar, per-step checkmarks, and a
cancel button, while `task_started`/`task_progress`/`task_completed`/`task_failed`/
`task_cancelled` events stream over SSE. A goal that doesn't actually need multiple steps
is reported as such and run as a normal command. This is the `automation/` layer — see
`docs/ARCHITECTURE.md` § 5.2.

## 5.6 Learning & Adaptation

VERONICA records what it does, what happened, and what you told it — so it can get better
at the things it does often. It's fully local, fully offline, and optional.

**How adaptive planning works.** When you send a multi-step goal through the **Plan & Run**
flow, VERONICA first checks its store of past experiences. Anything relevant to the current
goal (similar wording, shared tools, recent and successful, or a prior failure worth
avoiding) is condensed into concise *lessons* and handed to the planner as **advisory
context** — e.g. "workspace_search succeeded repeatedly for locating unknown files;
direct path lookup failed when the filename was unknown." The planner LLM still generates a
fresh plan for the current goal; it may use that evidence or ignore it when the situation
differs. VERONICA **does not replay previous plans** — experience is evidence, not a
script. Failures are surfaced as *cautionary* context, never as absolute prohibitions.

The full loop is:

```
Experience → Lesson → Relevance → Confidence → Planner context → New plan
   → Execution → Verification → New experience
```

Turning it off is a one-line change — set `learning.enabled: false` in
`config/config.yaml`. This disables both experience capture and adaptive planning, and
restores the planner's normal, experience-free behavior. Task execution is unaffected
either way.

**Controlled behavioral adaptation (Phase 4).** At plan time, VERONICA may also transform
*how* a plan executes — but only under strict, conservative conditions and never
unrestrictedly:

- The learning system **never executes tools**. All execution continues through the normal,
  permission-gated pipeline. Adaptation only ever changes which step dicts the executor is
  handed (and it copies the plan, preserving the original).
- **Confidence thresholds** (the *same* `ConfidenceTracker` values as planning): below 0.80
  nothing is adapted at all; 0.80–0.90 is *advisory only* (informs, never silently changes);
  ≥ 0.90 *and* at least 3 repeated independent successes is *strong* and may substitute one
  tool for another within the same tool group (e.g. plan says `find_file`, validated lessons
  say `workspace_search`). An explicit user correction is treated as strong evidence but is
  still gated.
- **Safety first**: `delete_file`, `run_shell_command`, `write_file`, credentials, payments,
  and system/install operations are never silently adapted (their tool names live in
  `policy.SAFETY_SENSITIVE_TOOLS`), and the existing permission/confirmation rules stay
  authoritative.
- **Explicit instructions and current context win** over learned preferences: if your goal
  names the tool to use, or a plan step already has a concrete path/argument, that's left
  alone.
- **Reversible & explainable**: the original plan is always preserved, every applied/rejected
  adaptation is recorded with reason/confidence/evidence, and a failed adaptation discards
  itself and executes the original plan. The Experience log records `original_plan`,
  `adaptations`, `adaptation_applied`, and `adaptation_success` for review.

Behavioral adaptation is configured under `learning:` — `adaptation_enabled` (default true),
`adaptation_min_confidence` (0.80), `adaptation_strong_confidence` (0.90), and
`adaptation_min_evidence` (3). It is fully covered by `learning.enabled: false` (the adapter
is then never created).

**Where the learning data lives** (all under the project `data/` folder by default):

**Where the learning data lives** (all under the project `data/` folder by default):

- `data/experiences.jsonl` — the experience log: one JSON object per line, one per
  finished multi-step task (what was asked, which tools ran, outcome, retries, duration,
  failure info, feedback).
- `data/confidence.json` — per-strategy success/failure counts used to compute an
  evidence-based confidence score.

Both are configured under the `learning:` block in `config/config.yaml`. Set
`learning.enabled: false` to turn learning off entirely — task execution is unaffected.

**How to inspect or reset learning data.** Each line in `experiences.jsonl` is a complete
JSON record you can read with any JSON tool. To reset:

```bash
# Delete the experience log and confidence counts (fresh start):
del data/experiences.jsonl data/confidence.json
```

(Or from Python: `from veronica.learning import ExperienceStore; ExperienceStore("data/experiences.jsonl").reset()`.)

**Privacy / redaction.** Before anything is written to disk, secrets are stripped:
API keys, auth tokens, passwords, cookies, private keys, bearer tokens, JWTs, and
env-style secret names (`*_KEY`, `*_TOKEN`, `*_SECRET`, …) are replaced with
`[REDACTED]`. VERONICA stores *what tools ran and how it went*, not your credentials.
Redaction never changes what's in memory — only what gets persisted.

Learning can never run a tool, bypass the permission gate, or lower a confirmation
requirement; and if learning itself fails, VERONICA keeps working normally. See
`docs/ARCHITECTURE.md` § 5.3 for the full design.

## 5.7 Computer Perception & Control (Phase 7, core layer)

Phase 7 lets VERONICA perceive the Windows desktop and act on it through the same
permission-gated pipeline as everything else. This is the **core layer** — perception +
control + Phase 6 integration; continuous background observation and the vision-model
provider are later steps.

**What it does.**

- **Hybrid perception** (`veronica/vision/`): a `PerceptionLayer` queries providers in
  priority order — Windows UI Automation (semantic windows/controls/text), screen metadata,
  and OCR — and merges them into a single normalized `ScreenState` (serializable, with a
  SHA-256 fingerprint for change detection). UIA auto-disables on non-Windows or missing
  COM; OCR auto-disables when pytesseract is absent, exactly like the existing
  `ScreenshotTool` degrades.
- **Screen-change detection** (`change_detection.py`): configurable strategies
  (`idle` / `active` / `waiting` / `action` / `error`) with per-strategy thresholds, plus a
  `wait_for_change()` helper for observation loops.
- **Semantic-first control** (`computer_controller.py`): `find_and_click("Save")` resolves
  the element by name/type from the live `ScreenState`; direct coordinates are the fallback.
  Mouse moves and scrolls are SAFE; clicks, typing, and hotkeys are MODERATE (or SENSITIVE
  for destructive combos like `alt+f4`) — so they are confirmed by the same
  `PermissionGate` as any tool.
- **Computer-use loop** (`computer_use.py`): OBSERVE → UNDERSTAND → SELECT → PERMISSION →
  EXECUTE → OBSERVE → VERIFY. Execution failures surface `recovery_suggested` and feed the
  **existing** Phase 6 `ClosedLoopController` — there is no second recovery system.
- **Prompt-injection defense** (`screen_content_defense.py`): screen text is **untrusted
  data**. Forged-command patterns, permission-bypass phrasing, and credential-shaped text
  are blocked or filtered before screen content can influence a decision; `filter_perception_state()`
  produces the safe copy used by the loop.
- **Stitch GUI integration**: `requestScreenState`, `executeComputerAction`, and
  `cancelComputerAction` bridge methods plus `action_observing` / `action_executing` /
  `action_verifying` / `action_completed` SSE events.

**Dependencies.** All optional: `comtypes` (UIA), `pyautogui` (control — already in
`requirements.txt`), `Pillow` + `pytesseract` (OCR). On a machine without them the modules
import cleanly and report the providers as unavailable; nothing crashes.

**Safety is unchanged.** Every computer action is a permission-gated decision; screen
content can never override permissions, instructions, or the Phase 6 invariants
(`UNKNOWN` never becomes `SUCCESS`, stored experience is data, no "try it and assume it
worked").

## 6. Permissions — VERONICA asks before doing anything

By default (`safety.require_confirmation_above: SAFE`) **every single action is
confirmed in the terminal first**, including read-only ones like checking the time:

```
[VERONICA needs SAFE permission] get_current_time({})
Proceed? [y/N]

[VERONICA needs CRITICAL permission] run_shell_command({'command': 'pip install requests'})
Proceed? [y/N]
```

This is what makes it safe to give VERONICA a genuinely general-purpose "brain" — see
`tools/execution_tools.py`, which adds:

- `run_shell_command` — run any shell command on your machine
- `read_file` / `write_file` / `delete_file` — read, create, or remove any file
- `fetch_webpage` (in `web_tools.py`) — actually read a page's content, not just open it

Combined with the LLM's tool-calling, this means VERONICA can chain steps to accomplish
open-ended tasks ("read my notes.txt, summarize it, and save the summary as
summary.txt") — but it will stop and ask before every single step along the way. Nothing
runs, is written, or is deleted without an explicit "yes" from you first.

`run_shell_command` and `delete_file` are additionally `CRITICAL` permission (the
highest level) regardless of the threshold you set, and `run_shell_command` refuses a
small set of catastrophic patterns (`rm -rf /`, disk formatting, fork bombs, etc.) even
after you approve it — a second layer, not a substitute for confirmation.

Once you've gotten a feel for the assistant and trust the commands you use daily, loosen
`safety.require_confirmation_above` to `MODERATE` or `SENSITIVE` in `config.yaml` so
routine reads stop interrupting you — the execution tools stay locked at
`SENSITIVE`/`CRITICAL` either way. Set `safety.dry_run: true` (or pass `--dry-run`) to
have VERONICA describe what it *would* do without doing it.

## 7. Running the tests

```bash
pip install pytest
pytest tests/ -v
```

The suite covers everything that doesn't need a microphone, speakers, or a running
Ollama server: the permission gate, tool registry, memory (short-term, long-term, thread,
and conversation compression), the loop guard, compound-command splitting, LLM health and
circuit breaker, the capability layer, the Domino chain engine, the **multi-step task
executor and planner** (create/execute/retry/cancel/verify plus the orchestrator's agentic
path), the **Phase 6 closed-loop stack** (observation, diagnosis, failure memory, replanning,
the `ClosedLoopController`, orchestrator + Stitch-bridge integration, and adversarial edge
cases), the STT/TTS backends (stubbed, so no audio or model downloads are needed), the
Stitch bridge — including a real HTTP round-trip on an ephemeral port and live task
commands — the **Phase 7 perception & control core** (ScreenState serialization,
fingerprint/change detection, perception providers, computer-controller permission
enforcement, computer-use loop phases, prompt-injection defense, and safety invariants),
and the **Phase 8 Thread Memory V1** stack (data model, episodic projection, vector-store +
embedding abstractions, deterministic weighted retrieval with the relevance gate,
consolidation, import/export, redaction/defense-in-depth, the "no backend import" layering
rule, and orchestrator memory-context integration that must never grant permissions).
**539 tests pass (+1 skipped).** It's a good sanity check after adding a new tool.

### Enabling Thread Memory

Thread Memory is opt-in and off by default. In `config/config.yaml`:

```yaml
memory_thread:
  enabled: true
  records_path: data/thread_memory.jsonl
  vector_path: data/thread_vectors.jsonl
  vector_backend: local        # "local" (zero-dependency) — ChromaDB/FAISS swap in later
  embedding_provider: local
  max_context_memories: 5      # how many retrieved memories are injected per turn
  retrieval_threshold: 0.20
```

With it enabled, VERONICA persists explicitly-remembered facts and completed episodes
(sanitized/redacted), and injects a small labeled `RETRIEVED MEMORY` block into the prompt.
It is purely informational: it never grants permission to act. See `docs/THREAD_MEMORY.md`.

## 8. Adding your own tool

1. Create a class in `veronica/tools/` (or a new file) subclassing `tools.base.Tool`.
2. Set `name`, `description`, `permission`, and `parameters` (JSON-schema-style).
3. Implement `run(self, **kwargs) -> ToolResult`.
4. Optionally implement `matches()`/`extract_arguments()` so simple phrasings of the
   command skip the LLM entirely (the "fast path").
5. Register an instance of it in `tools/registry.py`'s `build_default_registry()`.

That's it — the orchestrator, the LLM's tool-calling prompt, and the permission system
all pick it up automatically.

## 9. Known limits of this build

- Voice and hardware-dependent paths (microphone, speakers, Windows volume control via
  `pycaw`, OCR via Tesseract) were built with clean interfaces and sensible fallbacks but
  could only be exercised with mocks/unit tests in the environment this was built in,
  which has no audio hardware and restricted network egress. Test them for real on your
  own machine — if something in a specific tool needs tweaking for your OS, that tool's
  file is short and self-contained, so it should be a small fix.
- `web_search` currently opens a DuckDuckGo results page in your browser rather than
  returning parsed results to the LLM — wiring up a real search API (with parsed results
  fed back into the conversation) is a natural next step; `config/.env.example` already
  reserves a `SERPAPI_API_KEY` slot for it.
- A GUI ships as a second front-end — the **Stitch command center** (§ 5.5), served over
  plain HTTP + SSE by `ui/stitch/bridge.py`. It talks to the same
  `Orchestrator.handle()` as the CLI, so the two front-ends never diverge in behavior.
  The browser page is static HTML + CDN JS (Three.js, Tailwind); it needs internet on the
  machine where you open it to load those assets.
